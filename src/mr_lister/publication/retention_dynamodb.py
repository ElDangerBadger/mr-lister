"""Offline DynamoDB adapter for Phase 7 operational-retention assignment.

The adapter has no SDK construction, scheduler, provider transport, delete, or source-tagging
capability.  It strongly proves one closed terminal partition, assigns the exact terminal+90-day
TTL in bounded idempotent transactions, then writes the publication-free JOB completion marker
last.  The existing Phase 6 source sweeper remains the only source-tag writer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

from botocore.exceptions import ClientError
from pydantic import BaseModel

from mr_lister.control.models import SourceArtifactRecord
from mr_lister.control.publication_retention import (
    PUBLICATION_RETENTION_ENTITY_TYPE,
    PUBLICATION_RETENTION_SORT_KEY,
    PublicationRetentionCompletionAuthority,
    publication_operational_expiry_epoch,
    publication_retention_completion_fingerprint,
    validate_publication_retention_completion,
)
from mr_lister.control.source_artwork import validate_source_artifact_authority
from mr_lister.publication.commands import PublicationCommandReceipt
from mr_lister.publication.dynamodb import _receipt_item
from mr_lister.publication.errors import (
    PublicationConflictError,
    PublicationErrorCode,
    PublicationNotFoundError,
)
from mr_lister.publication.evidence_provenance import (
    PublicationProviderEvidenceConsumption,
    PublicationProviderEvidenceStage,
)
from mr_lister.publication.execution_dynamodb import (
    FROZEN_MAX_EXECUTION_AUTHORITY_ITEMS,
    MAX_DYNAMODB_ITEM_BYTES,
    MAX_DYNAMODB_TRANSACTION_BYTES,
    DynamoDBPublicationExecutionStore,
    _job_item,
    _record_item,
)
from mr_lister.publication.execution_models import (
    ExecutionPublicationAggregate,
    ExecutionPublicationAttempt,
    ExecutionPublicationPermit,
    ExecutionPublicationWork,
    PublicationAggregateTombstone,
    PublicationCallClaim,
    PublicationExecutionEvent,
    PublicationExecutionOperation,
    PublicationExecutionReceipt,
    PublicationMutationClaim,
    PublicationNotification,
    PublicationPostObservation,
    PublicationPreflightProof,
    PublicationProductObservation,
    PublicationProviderAuditBinding,
    PublicationProviderAuthority,
    PublicationResult,
    PublicationTerminalJobLink,
    PublicationTerminalReport,
)
from mr_lister.publication.fingerprints import canonical_fingerprint
from mr_lister.publication.models import PublicationDomainEvent, PublicationSnapshot
from mr_lister.publication.retention import (
    PublicationRetentionBoundaryInvalidError,
    PublicationRetentionConflictError,
    PublicationRetentionDependencyUnavailableError,
    PublicationTerminalRetentionAuthority,
    build_publication_terminal_retention_authority,
)
from mr_lister.publication.retention_locator import (
    PUBLICATION_REQUEST_RECEIPT_LOCATOR_ENTITY_TYPE,
    PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY,
    PublicationRequestReceiptLocator,
    build_publication_request_receipt_locator,
)

MAX_RETENTION_TRANSACTION_ITEMS = 25
MAX_RETENTION_UPDATE_ITEMS_AFTER_GUARDS = 22
MAX_RETENTION_INITIAL_ADDITIONAL_ITEMS = 22
MAX_RETENTION_PARTITION_ITEMS = FROZEN_MAX_EXECUTION_AUTHORITY_ITEMS
MAX_RETENTION_ASSIGNMENTS = MAX_RETENTION_PARTITION_ITEMS + 2
assert MAX_RETENTION_PARTITION_ITEMS == 943
assert MAX_RETENTION_ASSIGNMENTS == 945


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _publication_pk(aggregate_id: str) -> str:
    return f"PUBLICATION#{aggregate_id}"


def _job_pk(job_id: str) -> str:
    return f"JOB#{job_id}"


def _item_key(item: dict[str, Any]) -> tuple[str, str]:
    if set(item) >= {"PK", "SK"}:
        raw_pk = item.get("PK")
        raw_sk = item.get("SK")
        if isinstance(raw_pk, dict) and isinstance(raw_sk, dict):
            pk = raw_pk.get("S")
            sk = raw_sk.get("S")
            if (
                isinstance(pk, str)
                and isinstance(sk, str)
                and set(raw_pk) == {"S"}
                and set(raw_sk) == {"S"}
            ):
                return pk, sk
    raise PublicationRetentionBoundaryInvalidError("Publication retention item identity is invalid")


def _av_string(item: dict[str, Any], name: str) -> str:
    raw = item.get(name)
    if not isinstance(raw, dict) or set(raw) != {"S"} or not isinstance(raw.get("S"), str):
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention attribute value is invalid"
        )
    return raw["S"]


def _expiry(item: dict[str, Any]) -> int | None:
    raw = item.get("expires_at")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"N"}:
        raise PublicationRetentionBoundaryInvalidError("Publication retention TTL is invalid")
    value = raw.get("N")
    if not isinstance(value, str) or not value.isdigit() or int(value) < 1:
        raise PublicationRetentionBoundaryInvalidError("Publication retention TTL is invalid")
    return int(value)


def _without_expiry(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "expires_at"}


def _strict_item(
    item: dict[str, Any],
    expected: dict[str, Any],
    *,
    expected_expiry: int | None = None,
    allow_expiry: bool = True,
) -> None:
    if _without_expiry(item) != expected:
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention row differs from its exact payload"
        )
    actual_expiry = _expiry(item)
    if not allow_expiry and actual_expiry is not None:
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention authority has an unexpected TTL"
        )
    if expected_expiry is not None and actual_expiry != expected_expiry:
        raise PublicationRetentionConflictError(
            "Publication retention row has a different operational expiry"
        )


def _parse_record(
    item: dict[str, Any],
    *,
    aggregate_id: str,
    sort_key: str,
    entity_type: str,
    model: type[BaseModel],
) -> BaseModel:
    if (
        _item_key(item) != (_publication_pk(aggregate_id), sort_key)
        or _av_string(item, "entity_type") != entity_type
    ):
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention row envelope is invalid"
        )
    payload = _av_string(item, "payload")
    try:
        record = model.model_validate_json(payload, strict=True)
    except Exception:
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention row payload is invalid"
        ) from None
    expected = _record_item(aggregate_id, sort_key, entity_type, record)
    _strict_item(item, expected)
    if _record_sort_key(record) != sort_key:
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention row key differs from its payload identity"
        )
    return record


def _record_sort_key(record: BaseModel) -> str:
    if isinstance(record, ExecutionPublicationAggregate):
        return "META"
    if isinstance(record, PublicationSnapshot):
        return f"SNAPSHOT#{record.snapshot_id}"
    if isinstance(record, ExecutionPublicationAttempt):
        return f"ATTEMPT#{record.attempt_id}"
    if isinstance(record, ExecutionPublicationPermit):
        return f"PERMIT#{record.permit_id}"
    if isinstance(record, ExecutionPublicationWork):
        return f"PUBLICATION_WORK#{record.work_request_id}"
    if isinstance(record, (PublicationDomainEvent, PublicationExecutionEvent)):
        return f"EVENT#{record.sequence:020d}"
    if isinstance(record, PublicationExecutionReceipt):
        return f"EXECUTION_RECEIPT#{record.operation_id}"
    if isinstance(record, PublicationCallClaim):
        return f"CALL_CLAIM#{record.authorization_id}"
    if isinstance(record, PublicationProviderAuditBinding):
        return f"PROVIDER_AUDIT#{record.durable_call_sequence:020d}"
    if isinstance(record, PublicationProductObservation):
        return f"PRODUCT_OBSERVATION#{record.observation_id}"
    if isinstance(record, PublicationProviderEvidenceStage):
        return f"PROVIDER_EVIDENCE#{record.stage_id}"
    if isinstance(record, PublicationProviderEvidenceConsumption):
        return f"PROVIDER_EVIDENCE_CONSUMED#{record.stage_id}"
    static: tuple[tuple[type[BaseModel], str], ...] = (
        (PublicationProviderAuthority, "PROVIDER_AUTHORITY"),
        (PublicationPreflightProof, "PREFLIGHT"),
        (PublicationMutationClaim, "MUTATION"),
        (PublicationPostObservation, "POST_OBSERVATION"),
        (PublicationResult, "RESULT"),
        (PublicationNotification, "NOTIFICATION"),
        (PublicationTerminalReport, "REPORT"),
        (PublicationAggregateTombstone, "TOMBSTONE"),
        (PublicationTerminalJobLink, "TERMINAL_JOB_LINK"),
        (PublicationRequestReceiptLocator, PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY),
    )
    for model, sort_key in static:
        if isinstance(record, model):
            return sort_key
    raise PublicationRetentionBoundaryInvalidError("Publication retention row type is unknown")


def _source_item(source: SourceArtifactRecord) -> dict[str, Any]:
    return {
        "PK": _s(_job_pk(source.job_id)),
        "SK": _s("SOURCE"),
        "entity_type": _s("SOURCE_ARTIFACT"),
        "contract_version": _s(source.contract_version),
        "payload": _s(source.model_dump_json()),
    }


def _completion_item(completion: PublicationRetentionCompletionAuthority) -> dict[str, Any]:
    return {
        "PK": _s(_job_pk(completion.job_id)),
        "SK": _s(PUBLICATION_RETENTION_SORT_KEY),
        "entity_type": _s(PUBLICATION_RETENTION_ENTITY_TYPE),
        "contract_version": _s(completion.contract_version),
        "job_id": _s(completion.job_id),
        "aggregate_id": _s(completion.aggregate_id),
        "job_record_version": _n(completion.job_record_version),
        "terminal_summary_fingerprint": _s(completion.terminal_summary_fingerprint),
        "source_artifact_fingerprint": _s(completion.source_artifact_fingerprint),
        "expires_at": _n(completion.expires_at_epoch_seconds),
        "payload": _s(completion.model_dump_json()),
    }


def _rendered_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    )


def _transaction_token(items: list[dict[str, Any]]) -> str:
    material = json.dumps(
        items,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return sha256(material).hexdigest()[:32]


def _validate_transaction(items: list[dict[str, Any]]) -> None:
    if not items or len(items) > MAX_RETENTION_TRANSACTION_ITEMS:
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention transaction exceeds its bounded envelope"
        )
    for action in items:
        operation = action.get("Put") or action.get("ConditionCheck")
        if operation is None:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention transaction contains an unknown action"
            )
        item = operation.get("Item")
        if item is not None and _rendered_size(item) >= MAX_DYNAMODB_ITEM_BYTES:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention item exceeds its bounded envelope"
            )
    if _rendered_size(items) >= MAX_DYNAMODB_TRANSACTION_BYTES:
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention transaction exceeds its bounded envelope"
        )


def _exact_condition_values(
    item: dict[str, Any],
) -> tuple[str, dict[str, str], dict[str, Any]]:
    clauses: list[str] = []
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    for index, (name, value) in enumerate(sorted(_without_expiry(item).items())):
        name_token = f"#f{index}"
        value_token = f":v{index}"
        names[name_token] = name
        values[value_token] = value
        clauses.append(f"{name_token} = {value_token}")
    return " AND ".join(clauses), names, values


def _put_with_expiry(
    table_name: str,
    item: dict[str, Any],
    expires_at: int,
) -> dict[str, Any]:
    base = _without_expiry(item)
    condition, names, values = _exact_condition_values(base)
    names["#ttl"] = "expires_at"
    values[":ttl"] = _n(expires_at)
    return {
        "Put": {
            "TableName": table_name,
            "Item": {**base, "expires_at": _n(expires_at)},
            "ConditionExpression": (f"{condition} AND (attribute_not_exists(#ttl) OR #ttl = :ttl)"),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


def _condition_exact_item(
    table_name: str,
    item: dict[str, Any],
    *,
    expires_at: int | None = None,
) -> dict[str, Any]:
    condition, names, values = _exact_condition_values(item)
    if expires_at is not None:
        names["#ttl"] = "expires_at"
        values[":ttl"] = _n(expires_at)
        condition = f"{condition} AND #ttl = :ttl"
    return {
        "ConditionCheck": {
            "TableName": table_name,
            "Key": {"PK": item["PK"], "SK": item["SK"]},
            "ConditionExpression": condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


@dataclass(frozen=True, slots=True)
class _Inventory:
    rows: tuple[dict[str, Any], ...]
    receipt_item: dict[str, Any]
    job_item: dict[str, Any]
    source_item: dict[str, Any]
    receipt: PublicationCommandReceipt
    source: SourceArtifactRecord
    inventory_fingerprint: str

    @property
    def targets(self) -> tuple[dict[str, Any], ...]:
        return (*self.rows, self.job_item, self.receipt_item)


class DynamoDBPublicationOperationalRetentionStore:
    """Strong, bounded and replay-safe operational-retention persistence adapter."""

    def __init__(self, *, client: Any, table_name: str) -> None:
        if not isinstance(table_name, str) or not table_name:
            raise ValueError("Publication retention table name is invalid")
        self._client = client
        self._table_name = table_name
        self._execution = DynamoDBPublicationExecutionStore(
            client=client,
            table_name=table_name,
        )

    def load_terminal_retention_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationTerminalRetentionAuthority:
        try:
            execution = self._execution.load_execution_authority(owner_id, aggregate_id)
            job = self._execution.load_linked_job(owner_id, aggregate_id)
            locator = self._load_locator(aggregate_id)
            _, receipt = self._load_receipt(locator)
            if (
                receipt is None
                or receipt.aggregate_id != aggregate_id
                or receipt.receipt_id != locator.receipt_id
                or receipt.fingerprint != locator.receipt_fingerprint
            ):
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication request receipt locator is invalid"
                )
            return build_publication_terminal_retention_authority(execution, job, locator)
        except (
            PublicationRetentionBoundaryInvalidError,
            PublicationRetentionConflictError,
        ):
            raise
        except PublicationNotFoundError:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication terminal retention authority is incomplete"
            ) from None
        except PublicationConflictError as error:
            if error.code is PublicationErrorCode.INVALID_AUTHORITY:
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication terminal retention authority is invalid"
                ) from None
            raise PublicationRetentionConflictError(
                "Publication terminal retention authority changed during its strong read"
            ) from None
        except Exception:
            raise PublicationRetentionDependencyUnavailableError(
                "Publication retention dependency is unavailable"
            ) from None

    def assign_terminal_retention(
        self,
        authority: PublicationTerminalRetentionAuthority,
        *,
        completed_at: datetime,
    ) -> PublicationRetentionCompletionAuthority:
        try:
            exact = PublicationTerminalRetentionAuthority.model_validate(
                authority.model_dump(mode="python"),
                strict=True,
            )
        except Exception:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention authority is invalid"
            ) from None
        if exact != authority:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention authority is invalid"
            )
        terminal_at = exact.aggregate.terminal_at
        operational_expires_at = exact.aggregate.operational_expires_at
        if (
            terminal_at is None
            or operational_expires_at is None
            or not isinstance(completed_at, datetime)
            or completed_at.utcoffset() != timedelta(0)
            or not terminal_at <= completed_at < operational_expires_at
        ):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention completion time is invalid"
            )

        current = self.load_terminal_retention_authority(
            exact.job.owner_id,
            exact.aggregate.aggregate_id,
        )
        if current != exact:
            raise PublicationRetentionConflictError(
                "Publication terminal authority changed before retention assignment"
            )
        inventory = self._load_inventory(current)
        existing = self._load_completion(current, inventory)
        if existing is not None:
            self._prove_expiry(inventory, existing.expires_at_epoch_seconds)
            if (
                existing.publication_row_count != len(inventory.rows)
                or existing.ttl_assignment_count != len(inventory.targets)
                or existing.inventory_fingerprint != inventory.inventory_fingerprint
            ):
                raise PublicationRetentionConflictError(
                    "Publication retention completion differs from its exact inventory"
                )
            return existing

        expires_at = publication_operational_expiry_epoch(operational_expires_at)
        for item in inventory.targets:
            existing_expiry = _expiry(item)
            if existing_expiry not in {None, expires_at}:
                raise PublicationRetentionConflictError(
                    "Publication retention row has a different operational expiry"
                )
        self._assign_ttls(inventory, expires_at)

        stable = self.load_terminal_retention_authority(
            exact.job.owner_id,
            exact.aggregate.aggregate_id,
        )
        if stable != exact:
            raise PublicationRetentionConflictError(
                "Publication terminal authority changed during retention assignment"
            )
        proven = self._load_inventory(stable)
        self._prove_expiry(proven, expires_at)
        if (
            len(proven.rows) != len(inventory.rows)
            or proven.inventory_fingerprint != inventory.inventory_fingerprint
        ):
            raise PublicationRetentionConflictError(
                "Publication retention inventory changed during assignment"
            )
        completion = self._build_completion(
            stable,
            proven,
            completed_at=completed_at,
            expires_at=expires_at,
        )
        marker_items = self._completion_transaction(stable, proven, completion)
        try:
            self._transact(marker_items)
        except (PublicationRetentionConflictError, PublicationRetentionDependencyUnavailableError):
            replay_inventory = self._load_inventory(stable)
            replay = self._load_completion(stable, replay_inventory)
            if replay == completion:
                self._prove_expiry(replay_inventory, expires_at)
                return replay
            raise
        return completion

    def _load_locator(self, aggregate_id: str) -> PublicationRequestReceiptLocator:
        item = self._get(
            _publication_pk(aggregate_id),
            PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY,
        )
        if item is None:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication request receipt locator is missing"
            )
        record = _parse_record(
            item,
            aggregate_id=aggregate_id,
            sort_key=PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY,
            entity_type=PUBLICATION_REQUEST_RECEIPT_LOCATOR_ENTITY_TYPE,
            model=PublicationRequestReceiptLocator,
        )
        assert isinstance(record, PublicationRequestReceiptLocator)
        return record

    def _load_receipt(
        self,
        locator: PublicationRequestReceiptLocator,
    ) -> tuple[dict[str, Any], PublicationCommandReceipt]:
        item = self._get(
            locator.owner_receipt_partition_key,
            locator.owner_receipt_sort_key,
        )
        if item is None:
            raise PublicationRetentionBoundaryInvalidError("Publication request receipt is missing")
        try:
            receipt = PublicationCommandReceipt.model_validate_json(
                _av_string(item, "payload"),
                strict=True,
            )
        except PublicationRetentionBoundaryInvalidError:
            raise
        except Exception:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication request receipt is invalid"
            ) from None
        expected_locator = build_publication_request_receipt_locator(
            aggregate_id=receipt.aggregate_id,
            owner_id=receipt.owner_id,
            job_id=receipt.job_id,
            receipt_id=receipt.receipt_id,
            receipt_fingerprint=receipt.fingerprint,
            idempotency_key_digest=receipt.idempotency_key_digest,
        )
        if expected_locator != locator:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication request receipt differs from its locator"
            )
        _strict_item(item, _receipt_item(receipt))
        return item, receipt

    def _load_inventory(self, authority: PublicationTerminalRetentionAuthority) -> _Inventory:
        aggregate_id = authority.aggregate.aggregate_id
        rows = tuple(self._query_partition(aggregate_id))
        self._validate_partition(rows, authority)
        locator = authority.receipt_locator
        receipt_item, receipt = self._load_receipt(locator)

        job_item = self._get(_job_pk(authority.job.job_id), "META")
        if job_item is None:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention job projection is missing"
            )
        _strict_item(job_item, _job_item(authority.job))

        source_item = self._get(_job_pk(authority.job.job_id), "SOURCE")
        if source_item is None:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention source authority is missing"
            )
        try:
            source = SourceArtifactRecord.model_validate_json(
                _av_string(source_item, "payload"),
                strict=True,
            )
            source = validate_source_artifact_authority(source)
        except PublicationRetentionBoundaryInvalidError:
            raise
        except Exception:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention source authority is invalid"
            ) from None
        if (
            source.job_id != authority.job.job_id
            or source.owner_id != authority.job.owner_id
            or source.fingerprint != authority.job.source_artifact_fingerprint
        ):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention source authority differs from the job"
            )
        expected_source_item = _source_item(source)
        _strict_item(source_item, expected_source_item, allow_expiry=False)

        inventory_fingerprint = canonical_fingerprint(
            {
                "contract_version": "7.0.1",
                "kind": "publication_operational_retention_inventory",
                "aggregate_id": aggregate_id,
                "rows": [
                    {
                        "partition_key": _item_key(item)[0],
                        "sort_key": _item_key(item)[1],
                        "entity_type": _av_string(item, "entity_type"),
                        "item_fingerprint": sha256(
                            json.dumps(
                                _without_expiry(item),
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True,
                                allow_nan=False,
                            ).encode()
                        ).hexdigest(),
                    }
                    for item in (*rows, job_item, receipt_item)
                ],
            }
        )
        return _Inventory(
            rows=rows,
            receipt_item=receipt_item,
            job_item=job_item,
            source_item=source_item,
            receipt=receipt,
            source=source,
            inventory_fingerprint=inventory_fingerprint,
        )

    def _validate_partition(
        self,
        rows: tuple[dict[str, Any], ...],
        authority: PublicationTerminalRetentionAuthority,
    ) -> tuple[BaseModel, ...]:
        aggregate_id = authority.aggregate.aggregate_id
        parsed: list[BaseModel] = []
        for item in rows:
            sort_key = _item_key(item)[1]
            entity_type = _av_string(item, "entity_type")
            model = self._model_for_row(sort_key, entity_type)
            parsed.append(
                _parse_record(
                    item,
                    aggregate_id=aggregate_id,
                    sort_key=sort_key,
                    entity_type=entity_type,
                    model=model,
                )
            )

        def exact_one(model: type[BaseModel]) -> BaseModel:
            values = [value for value in parsed if isinstance(value, model)]
            if len(values) != 1:
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention inventory lacks one exact singleton"
                )
            return values[0]

        expected_singletons: tuple[tuple[type[BaseModel], BaseModel], ...] = (
            (ExecutionPublicationAggregate, authority.aggregate),
            (PublicationSnapshot, authority.execution.snapshot),
            (ExecutionPublicationAttempt, authority.execution.attempt),
            (ExecutionPublicationPermit, authority.execution.permit),
            (ExecutionPublicationWork, authority.execution.work),
            (PublicationRequestReceiptLocator, authority.receipt_locator),
            (PublicationTerminalReport, authority.report),
            (PublicationAggregateTombstone, authority.tombstone),
            (PublicationTerminalJobLink, authority.terminal_job_link),
        )
        for model, expected in expected_singletons:
            if exact_one(model) != expected:
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention inventory differs from terminal authority"
                )

        optional: tuple[tuple[type[BaseModel], BaseModel | None], ...] = (
            (PublicationProviderAuthority, authority.execution.provider_authority),
            (PublicationPreflightProof, authority.execution.preflight_proof),
            (PublicationMutationClaim, authority.execution.mutation_claim),
            (PublicationPostObservation, authority.execution.post_observation),
            (PublicationResult, authority.execution.result),
            (PublicationNotification, authority.execution.notification),
        )
        for model, expected in optional:
            values = [value for value in parsed if isinstance(value, model)]
            if values != ([] if expected is None else [expected]):
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention inventory differs from terminal authority"
                )

        def values(model: type[BaseModel]) -> list[BaseModel]:
            return [value for value in parsed if isinstance(value, model)]

        claims = sorted(
            values(PublicationCallClaim),
            key=lambda value: value.resulting_attempt_record_version,  # type: ignore[attr-defined]
        )
        audits = sorted(
            values(PublicationProviderAuditBinding),
            key=lambda value: value.durable_call_sequence,  # type: ignore[attr-defined]
        )
        observations = sorted(
            values(PublicationProductObservation),
            key=lambda value: (  # type: ignore[attr-defined]
                value.resulting_aggregate_record_version,
                value.observation_id,
            ),
        )
        if (
            tuple(claims) != authority.execution.call_claims
            or tuple(audits) != authority.execution.provider_audits
            or tuple(observations) != authority.execution.product_observations
        ):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention history differs from terminal authority"
            )

        initial = exact_one(PublicationDomainEvent)
        if (
            initial.aggregate_id != aggregate_id  # type: ignore[attr-defined]
            or initial.owner_id != authority.job.owner_id  # type: ignore[attr-defined]
            or initial.job_id != authority.job.job_id  # type: ignore[attr-defined]
            or initial.snapshot_id != authority.execution.snapshot.snapshot_id  # type: ignore[attr-defined]
        ):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention initial event is invalid"
            )
        events = sorted(
            values(PublicationExecutionEvent),
            key=lambda value: value.sequence,  # type: ignore[attr-defined]
        )
        if [event.sequence for event in events] != list(  # type: ignore[attr-defined]
            range(2, authority.aggregate.event_sequence + 1)
        ):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention execution event history is incomplete"
            )
        receipts = values(PublicationExecutionReceipt)
        receipts_by_operation = {
            receipt.operation_id: receipt  # type: ignore[attr-defined]
            for receipt in receipts
        }
        if len(receipts_by_operation) != len(receipts) or len(receipts) != len(events):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention execution receipt history is incomplete"
            )
        previous_time = initial.occurred_at  # type: ignore[attr-defined]
        authority_records = self._authority_records(parsed)
        for event in events:
            receipt = receipts_by_operation.get(event.operation_id)  # type: ignore[attr-defined]
            expected_fingerprint = (
                authority_records.get(receipt.authority_record_id)  # type: ignore[attr-defined]
                if receipt
                else None
            )
            if (
                receipt is not None
                and receipt.authority_record_id  # type: ignore[attr-defined]
                == authority.execution.work.work_request_id
            ):
                # Dispatch is authorized by the then-current mutable work root.  Later exact
                # transitions replace that root, while the immutable event/receipt pair retains
                # the historical fingerprint and must still agree with each other.
                expected_fingerprint = event.authority_fingerprint  # type: ignore[attr-defined]
            if (
                receipt is None
                or event.aggregate_id != aggregate_id  # type: ignore[attr-defined]
                or event.owner_id != authority.job.owner_id  # type: ignore[attr-defined]
                or event.job_id != authority.job.job_id  # type: ignore[attr-defined]
                or receipt.aggregate_id != aggregate_id  # type: ignore[attr-defined]
                or receipt.owner_id != authority.job.owner_id  # type: ignore[attr-defined]
                or receipt.job_id != authority.job.job_id  # type: ignore[attr-defined]
                or receipt.aggregate_state is not event.state  # type: ignore[attr-defined]
                or receipt.created_at != event.occurred_at  # type: ignore[attr-defined]
                or receipt.authority_fingerprint != event.authority_fingerprint  # type: ignore[attr-defined]
                or expected_fingerprint != event.authority_fingerprint  # type: ignore[attr-defined]
                or event.occurred_at < previous_time  # type: ignore[attr-defined]
            ):
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention execution history is invalid"
                )
            previous_time = event.occurred_at  # type: ignore[attr-defined]
        if (
            events[-1].state is not authority.aggregate.state  # type: ignore[attr-defined]
            or events[-1].occurred_at != authority.aggregate.terminal_at  # type: ignore[attr-defined]
        ):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention terminal event is invalid"
            )

        stages = values(PublicationProviderEvidenceStage)
        consumptions = values(PublicationProviderEvidenceConsumption)
        if len(stages) != authority.aggregate.provider_evidence_record_version:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention evidence watermark is invalid"
            )
        stages_by_id = {stage.stage_id: stage for stage in stages}  # type: ignore[attr-defined]
        if len(stages_by_id) != len(stages):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention evidence stages are duplicated"
            )
        claims_by_id = {claim.authorization_id: claim for claim in claims}  # type: ignore[attr-defined]
        audit_fingerprints = {audit.fingerprint for audit in audits}  # type: ignore[attr-defined]
        provider_authority = authority.execution.provider_authority
        for stage in stages:
            claim = claims_by_id.get(stage.call_claim_id)  # type: ignore[attr-defined]
            if (
                claim is None
                or provider_authority is None
                or stage.aggregate_id != aggregate_id  # type: ignore[attr-defined]
                or stage.call_claim_fingerprint != claim.fingerprint  # type: ignore[attr-defined]
                or stage.provider_authority_id != provider_authority.provider_authority_id  # type: ignore[attr-defined]
                or stage.provider_authority_fingerprint != provider_authority.fingerprint  # type: ignore[attr-defined]
                or stage.allowed_audit_binding_fingerprint not in audit_fingerprints  # type: ignore[attr-defined]
            ):
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention evidence stage is invalid"
                )
        consumptions_by_stage = {item.stage_id: item for item in consumptions}  # type: ignore[attr-defined]
        if len(consumptions_by_stage) != len(consumptions):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention evidence consumption is duplicated"
            )
        for stage_id, consumption in consumptions_by_stage.items():
            stage = stages_by_id.get(stage_id)
            receipt = receipts_by_operation.get(consumption.operation_id)  # type: ignore[attr-defined]
            if (
                stage is None
                or receipt is None
                or consumption.aggregate_id != aggregate_id  # type: ignore[attr-defined]
                or consumption.stage_fingerprint != stage.fingerprint  # type: ignore[attr-defined]
                or consumption.call_claim_id != stage.call_claim_id  # type: ignore[attr-defined]
                or consumption.call_claim_fingerprint != stage.call_claim_fingerprint  # type: ignore[attr-defined]
                or consumption.provider_authority_id != stage.provider_authority_id  # type: ignore[attr-defined]
                or consumption.provider_authority_fingerprint  # type: ignore[attr-defined]
                != stage.provider_authority_fingerprint  # type: ignore[attr-defined]
                or consumption.evidence_kind is not stage.evidence_kind  # type: ignore[attr-defined]
                or consumption.evidence_type is not stage.evidence_type  # type: ignore[attr-defined]
                or consumption.evidence_id != stage.evidence_id  # type: ignore[attr-defined]
                or consumption.evidence_fingerprint != stage.evidence_fingerprint  # type: ignore[attr-defined]
                or consumption.allowed_audit_binding_fingerprint  # type: ignore[attr-defined]
                != stage.allowed_audit_binding_fingerprint  # type: ignore[attr-defined]
                or consumption.receipt_id != receipt.receipt_id  # type: ignore[attr-defined]
                or consumption.operation is not receipt.operation  # type: ignore[attr-defined]
                or consumption.consumed_at != receipt.created_at  # type: ignore[attr-defined]
            ):
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention evidence consumption is invalid"
                )
        consumption_counts: dict[str, int] = {}
        for consumption in consumptions:
            operation_id = consumption.operation_id  # type: ignore[attr-defined]
            consumption_counts[operation_id] = consumption_counts.get(operation_id, 0) + 1
        required_consumptions = {
            PublicationExecutionOperation.RECORD_PREFLIGHT: 2,
            PublicationExecutionOperation.RECORD_POST_OUTCOME: 1,
            PublicationExecutionOperation.RECORD_PRODUCT_OBSERVATION: 1,
            PublicationExecutionOperation.SETTLE_DEFINITIVE_PREFLIGHT_FAILURE: 1,
        }
        for receipt in receipts:
            expected_count = required_consumptions.get(receipt.operation, 0)  # type: ignore[attr-defined]
            if consumption_counts.get(receipt.operation_id, 0) != expected_count:  # type: ignore[attr-defined]
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention evidence consumption history is incomplete"
                )
        return tuple(parsed)

    @staticmethod
    def _authority_records(records: list[BaseModel]) -> dict[str, str]:
        identified: tuple[tuple[type[BaseModel], str], ...] = (
            (PublicationProviderAuthority, "provider_authority_id"),
            (PublicationCallClaim, "authorization_id"),
            (PublicationPreflightProof, "proof_id"),
            (PublicationMutationClaim, "mutation_claim_id"),
            (PublicationPostObservation, "observation_id"),
            (PublicationProductObservation, "observation_id"),
            (PublicationTerminalReport, "report_id"),
        )
        result: dict[str, str] = {}
        for record in records:
            fingerprint = getattr(record, "fingerprint", None)
            if not isinstance(fingerprint, str):
                continue
            name = next((name for model, name in identified if isinstance(record, model)), None)
            if name is None:
                continue
            value = getattr(record, name)
            prior = result.setdefault(value, fingerprint)
            if prior != fingerprint:
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention authority identity is reused"
                )
        return result

    @staticmethod
    def _model_for_row(sort_key: str, entity_type: object) -> type[BaseModel]:
        static: dict[tuple[str, str], type[BaseModel]] = {
            ("META", "PUBLICATION_EXECUTION_AGGREGATE"): ExecutionPublicationAggregate,
            ("PROVIDER_AUTHORITY", "PUBLICATION_PROVIDER_AUTHORITY"): PublicationProviderAuthority,
            ("PREFLIGHT", "PUBLICATION_PREFLIGHT"): PublicationPreflightProof,
            ("MUTATION", "PUBLICATION_MUTATION_CLAIM"): PublicationMutationClaim,
            ("POST_OBSERVATION", "PUBLICATION_POST_OBSERVATION"): PublicationPostObservation,
            ("RESULT", "PUBLICATION_RESULT"): PublicationResult,
            ("NOTIFICATION", "PUBLICATION_NOTIFICATION"): PublicationNotification,
            ("REPORT", "PUBLICATION_TERMINAL_REPORT"): PublicationTerminalReport,
            ("TOMBSTONE", "PUBLICATION_AGGREGATE_TOMBSTONE"): PublicationAggregateTombstone,
            ("TERMINAL_JOB_LINK", "PUBLICATION_TERMINAL_JOB_LINK"): PublicationTerminalJobLink,
            (
                PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY,
                PUBLICATION_REQUEST_RECEIPT_LOCATOR_ENTITY_TYPE,
            ): PublicationRequestReceiptLocator,
        }
        if isinstance(entity_type, str) and (sort_key, entity_type) in static:
            return static[(sort_key, entity_type)]
        prefixes: tuple[tuple[str, str, type[BaseModel]], ...] = (
            ("SNAPSHOT#", "PUBLICATION_SNAPSHOT", PublicationSnapshot),
            ("ATTEMPT#", "PUBLICATION_EXECUTION_ATTEMPT", ExecutionPublicationAttempt),
            ("PERMIT#", "PUBLICATION_EXECUTION_PERMIT", ExecutionPublicationPermit),
            ("PUBLICATION_WORK#", "PUBLICATION_EXECUTION_WORK", ExecutionPublicationWork),
            ("EVENT#", "PUBLICATION_EXECUTION_EVENT", PublicationExecutionEvent),
            ("EXECUTION_RECEIPT#", "PUBLICATION_EXECUTION_RECEIPT", PublicationExecutionReceipt),
            ("CALL_CLAIM#", "PUBLICATION_CALL_CLAIM", PublicationCallClaim),
            ("PROVIDER_AUDIT#", "PUBLICATION_PROVIDER_AUDIT", PublicationProviderAuditBinding),
            (
                "PRODUCT_OBSERVATION#",
                "PUBLICATION_PRODUCT_OBSERVATION",
                PublicationProductObservation,
            ),
            (
                "PROVIDER_EVIDENCE#",
                "PUBLICATION_PROVIDER_EVIDENCE",
                PublicationProviderEvidenceStage,
            ),
            (
                "PROVIDER_EVIDENCE_CONSUMED#",
                "PUBLICATION_PROVIDER_EVIDENCE_CONSUMPTION",
                PublicationProviderEvidenceConsumption,
            ),
        )
        if sort_key == "EVENT#00000000000000000001" and entity_type == "PUBLICATION_DOMAIN_EVENT":
            return PublicationDomainEvent
        for prefix, expected_entity, model in prefixes:
            if sort_key.startswith(prefix) and entity_type == expected_entity:
                return model
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention inventory contains an unknown row"
        )

    def _query_partition(self, aggregate_id: str) -> list[dict[str, Any]]:
        partition_key = _publication_pk(aggregate_id)
        items: list[dict[str, Any]] = []
        keys: set[tuple[str, str]] = set()
        cursors: set[tuple[str, str]] = set()
        cursor: dict[str, Any] | None = None
        last_sort_key: str | None = None
        pages = 0
        while True:
            pages += 1
            if pages > MAX_RETENTION_PARTITION_ITEMS + 1:
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention query did not make bounded progress"
                )
            request: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": _s(partition_key)},
                "ConsistentRead": True,
                "Limit": MAX_RETENTION_PARTITION_ITEMS - len(items) + 1,
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            try:
                response = self._client.query(**request)
            except Exception:
                raise PublicationRetentionDependencyUnavailableError(
                    "Publication retention dependency is unavailable"
                ) from None
            if not isinstance(response, dict) or not isinstance(response.get("Items", []), list):
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention query returned an invalid page"
                )
            page = response.get("Items", [])
            for item in page:
                if not isinstance(item, dict):
                    raise PublicationRetentionBoundaryInvalidError(
                        "Publication retention query returned an invalid row"
                    )
                key = _item_key(item)
                if (
                    key[0] != partition_key
                    or key in keys
                    or (last_sort_key is not None and key[1] <= last_sort_key)
                ):
                    raise PublicationRetentionBoundaryInvalidError(
                        "Publication retention query returned duplicate or unordered rows"
                    )
                keys.add(key)
                last_sort_key = key[1]
                items.append(item)
                if len(items) > MAX_RETENTION_PARTITION_ITEMS:
                    raise PublicationRetentionBoundaryInvalidError(
                        "Publication retention inventory exceeds its bounded read"
                    )
            next_cursor = response.get("LastEvaluatedKey")
            if not next_cursor:
                break
            if not isinstance(next_cursor, dict):
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention query cursor is invalid"
                )
            next_key = _item_key(next_cursor)
            if not page or next_key != _item_key(page[-1]) or next_key in cursors:
                raise PublicationRetentionBoundaryInvalidError(
                    "Publication retention query cursor did not make progress"
                )
            cursors.add(next_key)
            cursor = next_cursor
        if not items:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention inventory is empty"
            )
        root = next((item for item in items if _item_key(item)[1] == "META"), None)
        if root is None or self._get(partition_key, "META") != root:
            raise PublicationRetentionConflictError(
                "Publication retention root changed during its bounded read"
            )
        return items

    def _assign_ttls(self, inventory: _Inventory, expires_at: int) -> None:
        root_key = (_publication_pk(inventory.receipt.aggregate_id), "META")
        job_key = (_job_pk(inventory.receipt.job_id), "META")
        targets = list(inventory.targets)
        by_key = {_item_key(item): item for item in targets}
        if len(by_key) != len(targets) or root_key not in by_key or job_key not in by_key:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention assignment inventory is invalid"
            )
        first = [by_key.pop(root_key), by_key.pop(job_key)]
        ordered = [by_key[key] for key in sorted(by_key)]
        first.extend(ordered[:MAX_RETENTION_INITIAL_ADDITIONAL_ITEMS])
        batches: list[list[dict[str, Any]]] = [
            [_put_with_expiry(self._table_name, item, expires_at) for item in first]
        ]
        root = _without_expiry(first[0])
        job = _without_expiry(first[1])
        remaining = ordered[MAX_RETENTION_INITIAL_ADDITIONAL_ITEMS:]
        for offset in range(0, len(remaining), MAX_RETENTION_UPDATE_ITEMS_AFTER_GUARDS):
            chunk = remaining[offset : offset + MAX_RETENTION_UPDATE_ITEMS_AFTER_GUARDS]
            batches.append(
                [
                    _condition_exact_item(
                        self._table_name,
                        root,
                        expires_at=expires_at,
                    ),
                    _condition_exact_item(
                        self._table_name,
                        job,
                        expires_at=expires_at,
                    ),
                    *(_put_with_expiry(self._table_name, item, expires_at) for item in chunk),
                ]
            )
        for batch in batches:
            self._transact(batch)

    @staticmethod
    def _prove_expiry(inventory: _Inventory, expires_at: int) -> None:
        for item in inventory.targets:
            if _expiry(item) != expires_at:
                raise PublicationRetentionConflictError(
                    "Publication retention TTL assignment is incomplete"
                )

    def _build_completion(
        self,
        authority: PublicationTerminalRetentionAuthority,
        inventory: _Inventory,
        *,
        completed_at: datetime,
        expires_at: int,
    ) -> PublicationRetentionCompletionAuthority:
        values: dict[str, Any] = {
            "job_id": authority.job.job_id,
            "aggregate_id": authority.aggregate.aggregate_id,
            "job_record_version": authority.job.record_version,
            "terminal_state": authority.aggregate.state.value,
            "terminal_at": authority.aggregate.terminal_at,
            "terminal_summary_fingerprint": (
                authority.terminal_job_link.terminal_summary_fingerprint
            ),
            "source_artifact_fingerprint": inventory.source.fingerprint,
            "aggregate_fingerprint": authority.aggregate.fingerprint,
            "report_id": authority.report.report_id,
            "report_fingerprint": authority.report.fingerprint,
            "tombstone_fingerprint": authority.tombstone.fingerprint,
            "terminal_job_link_fingerprint": authority.terminal_job_link.fingerprint,
            "source_release_eligible_at": authority.aggregate.source_release_eligible_at,
            "operational_expires_at": authority.aggregate.operational_expires_at,
            "expires_at_epoch_seconds": expires_at,
            "publication_row_count": len(inventory.rows),
            "ttl_assignment_count": len(inventory.targets),
            "inventory_fingerprint": inventory.inventory_fingerprint,
            "completed_at": completed_at,
        }
        try:
            basis = PublicationRetentionCompletionAuthority.model_construct(
                **values,
                fingerprint="0" * 64,
            )
            completion = PublicationRetentionCompletionAuthority(
                **values,
                fingerprint=publication_retention_completion_fingerprint(basis),
            )
            return validate_publication_retention_completion(
                authority.job,
                completion,
                source=inventory.source,
            )
        except Exception:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention completion is invalid"
            ) from None

    def _completion_transaction(
        self,
        authority: PublicationTerminalRetentionAuthority,
        inventory: _Inventory,
        completion: PublicationRetentionCompletionAuthority,
    ) -> list[dict[str, Any]]:
        expires_at = completion.expires_at_epoch_seconds
        by_sort_key = {_item_key(item)[1]: item for item in inventory.rows}
        guarded = (
            by_sort_key["META"],
            inventory.job_item,
            inventory.source_item,
            by_sort_key["REPORT"],
            by_sort_key["TOMBSTONE"],
            by_sort_key["TERMINAL_JOB_LINK"],
            by_sort_key[PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY],
            inventory.receipt_item,
        )
        items = [
            _condition_exact_item(
                self._table_name,
                _without_expiry(item),
                expires_at=None if item is inventory.source_item else expires_at,
            )
            for item in guarded
        ]
        items.append(
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _completion_item(completion),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        )
        if len(items) != 9:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention completion transaction is invalid"
            )
        return items

    def _load_completion(
        self,
        authority: PublicationTerminalRetentionAuthority,
        inventory: _Inventory,
    ) -> PublicationRetentionCompletionAuthority | None:
        item = self._get(_job_pk(authority.job.job_id), PUBLICATION_RETENTION_SORT_KEY)
        if item is None:
            return None
        try:
            completion = PublicationRetentionCompletionAuthority.model_validate_json(
                _av_string(item, "payload"),
                strict=True,
            )
            completion = validate_publication_retention_completion(
                authority.job,
                completion,
                source=inventory.source,
            )
        except Exception:
            raise PublicationRetentionConflictError(
                "Publication retention completion marker is invalid"
            ) from None
        expected = _completion_item(completion)
        if item != expected:
            raise PublicationRetentionConflictError(
                "Publication retention completion marker differs from its payload"
            )
        if (
            completion.aggregate_fingerprint != authority.aggregate.fingerprint
            or completion.report_fingerprint != authority.report.fingerprint
            or completion.tombstone_fingerprint != authority.tombstone.fingerprint
            or completion.terminal_job_link_fingerprint != authority.terminal_job_link.fingerprint
        ):
            raise PublicationRetentionConflictError(
                "Publication retention completion differs from terminal authority"
            )
        return completion

    def _get(self, partition_key: str, sort_key: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={"PK": _s(partition_key), "SK": _s(sort_key)},
                ConsistentRead=True,
            )
        except Exception:
            raise PublicationRetentionDependencyUnavailableError(
                "Publication retention dependency is unavailable"
            ) from None
        if not isinstance(response, dict):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention read response is invalid"
            )
        item = response.get("Item")
        if item is None:
            return None
        if not isinstance(item, dict):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention read response is invalid"
            )
        _item_key(item)
        return item

    def _transact(self, items: list[dict[str, Any]]) -> None:
        _validate_transaction(items)
        try:
            self._client.transact_write_items(
                TransactItems=items,
                ClientRequestToken=_transaction_token(items),
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
                "TransactionConflictException",
            }:
                raise PublicationRetentionConflictError(
                    "Publication retention authority changed during assignment"
                ) from None
            raise PublicationRetentionDependencyUnavailableError(
                "Publication retention dependency is unavailable"
            ) from None
        except Exception:
            raise PublicationRetentionDependencyUnavailableError(
                "Publication retention dependency is unavailable"
            ) from None


__all__ = [
    "MAX_RETENTION_ASSIGNMENTS",
    "MAX_RETENTION_INITIAL_ADDITIONAL_ITEMS",
    "MAX_RETENTION_PARTITION_ITEMS",
    "MAX_RETENTION_TRANSACTION_ITEMS",
    "MAX_RETENTION_UPDATE_ITEMS_AFTER_GUARDS",
    "DynamoDBPublicationOperationalRetentionStore",
]
