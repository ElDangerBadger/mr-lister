"""Low-level DynamoDB persistence for offline Phase 7 publication execution.

The adapter accepts an injected client and renders only bounded, exact-CAS reads and writes.  It
does not construct SDK clients, expose provider routes, or compose a runnable publication path.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, TypeVar

from botocore.exceptions import ClientError
from pydantic import BaseModel, ValidationError

from mr_lister.control.models import ControlJobRecord
from mr_lister.control.store import owner_job_sort_key
from mr_lister.publication.dynamodb import DynamoDBPublicationStore
from mr_lister.publication.errors import (
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.evidence_provenance import (
    PublicationProviderEvidenceCommit,
    PublicationProviderEvidenceConsumption,
    PublicationProviderEvidenceStage,
)
from mr_lister.publication.execution_models import (
    ExecutionPublicationAggregate,
    ExecutionPublicationAttempt,
    ExecutionPublicationPermit,
    ExecutionPublicationWork,
    PublicationAggregateTombstone,
    PublicationCallClaim,
    PublicationExecutionAuthority,
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
from mr_lister.publication.execution_store import (
    FreshPublicationCallGrant,
    FreshPublicationMutationGrant,
    PublicationExecutionCommit,
    PublicationExecutionCommitResult,
    PublicationProviderAuditCommit,
    validate_execution_commit,
)
from mr_lister.publication.models import (
    PublicationAggregate,
    PublicationAttempt,
    PublicationJobLink,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
)
from mr_lister.publication.store import PublicationRequestAuthority

MAX_EXECUTION_TRANSACTION_ITEMS = 25
# Exact largest frozen graph: six roots/initial rows, 208 transition event/receipt pairs,
# 104 claims, audits, and stages, 102 consumptions, 99 product observations, four execution
# singletons, and three terminal singleton rows.  The positive terminal variant replaces the
# final deadline event/receipt pair with result/notification and has the same total.
FROZEN_MAX_EXECUTION_AUTHORITY_ITEMS = 6 + (208 * 2) + 104 + 104 + 104 + 102 + 99 + 4 + 3
MAX_EXECUTION_AUTHORITY_ITEMS = 1024
assert FROZEN_MAX_EXECUTION_AUTHORITY_ITEMS == 942
assert FROZEN_MAX_EXECUTION_AUTHORITY_ITEMS <= MAX_EXECUTION_AUTHORITY_ITEMS
MAX_DYNAMODB_ITEM_BYTES = 400 * 1024
MAX_DYNAMODB_TRANSACTION_BYTES = 4 * 1024 * 1024

RecordT = TypeVar("RecordT", bound=BaseModel)


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _bool(value: bool) -> dict[str, bool]:
    return {"BOOL": value}


def _publication_pk(aggregate_id: str) -> str:
    return f"PUBLICATION#{aggregate_id}"


def _job_pk(job_id: str) -> str:
    return f"JOB#{job_id}"


def _payload(record: BaseModel) -> str:
    return record.model_dump_json()


def _record_item(
    aggregate_id: str,
    sort_key: str,
    entity_type: str,
    record: BaseModel,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "PK": _s(_publication_pk(aggregate_id)),
        "SK": _s(sort_key),
        "entity_type": _s(entity_type),
        "contract_version": _s(str(record.contract_version)),
        "payload": _s(_payload(record)),
    }
    if isinstance(record, (PublicationAggregate, ExecutionPublicationAggregate)):
        item.update(
            owner_id=_s(record.owner_id),
            job_id=_s(record.job_id),
            publication_state=_s(record.state.value),
            record_version=_n(record.record_version),
        )
        if isinstance(record, ExecutionPublicationAggregate):
            item["provider_audit_record_version"] = _n(record.provider_audit_record_version)
            item["provider_evidence_record_version"] = _n(record.provider_evidence_record_version)
    if isinstance(record, (PublicationWorkRequest, ExecutionPublicationWork)):
        item.update(
            work_request_id=_s(record.work_request_id),
            work_status=_s(record.status.value),
        )
        if record.next_dispatch_at is not None:
            item.update(
                dispatch_pk=_s("PUBLICATION_WORK_DUE#0"),
                dispatch_sk=_s(
                    f"{int(record.next_dispatch_at.timestamp()):020d}#{record.work_request_id}"
                ),
            )
    return item


def _job_item(job: ControlJobRecord) -> dict[str, Any]:
    return {
        "PK": _s(_job_pk(job.job_id)),
        "SK": _s("META"),
        "entity_type": _s("CONTROL_JOB"),
        "contract_version": _s(job.contract_version),
        "owner_id": _s(job.owner_id),
        "owner_jobs_pk": _s(f"OWNER#{job.owner_id}"),
        "owner_jobs_sk": _s(owner_job_sort_key(job)),
        "state": _s(job.state.value),
        "record_version": _n(job.record_version),
        "event_sequence": _n(job.event_sequence),
        "review_version": _n(job.review_version),
        "cancellation_requested": _bool(job.cancellation_requested_at is not None),
        "payload": _s(job.model_dump_json()),
    }


def _put_new(table_name: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "Put": {
            "TableName": table_name,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(PK)",
        }
    }


def _put_exact(
    table_name: str,
    item: dict[str, Any],
    *,
    expected_entity_type: str,
    expected: BaseModel,
) -> dict[str, Any]:
    return {
        "Put": {
            "TableName": table_name,
            "Item": item,
            "ConditionExpression": (
                "entity_type = :expected_entity_type AND "
                "contract_version = :expected_contract_version AND "
                "payload = :expected_payload"
            ),
            "ExpressionAttributeValues": {
                ":expected_entity_type": _s(expected_entity_type),
                ":expected_contract_version": _s(str(expected.contract_version)),
                ":expected_payload": _s(_payload(expected)),
            },
        }
    }


def _condition_exact(
    table_name: str,
    *,
    partition_key: str,
    sort_key: str,
    entity_type: str,
    expected: BaseModel,
) -> dict[str, Any]:
    return {
        "ConditionCheck": {
            "TableName": table_name,
            "Key": {"PK": _s(partition_key), "SK": _s(sort_key)},
            "ConditionExpression": (
                "entity_type = :expected_entity_type AND "
                "contract_version = :expected_contract_version AND "
                "payload = :expected_payload"
            ),
            "ExpressionAttributeValues": {
                ":expected_entity_type": _s(entity_type),
                ":expected_contract_version": _s(str(expected.contract_version)),
                ":expected_payload": _s(_payload(expected)),
            },
        }
    }


def _transaction_token(items: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        items,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return sha256(encoded).hexdigest()[:32]


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


def _validate_envelope(items: list[dict[str, Any]]) -> None:
    if len(items) > MAX_EXECUTION_TRANSACTION_ITEMS:
        raise ValueError("Publication execution transaction exceeds 25 actions")
    for action in items:
        operation = action.get("Put") or action.get("ConditionCheck")
        if operation is None:
            raise ValueError("Publication execution transaction contains an unknown action")
        item = operation.get("Item")
        if item is not None and _rendered_size(item) >= MAX_DYNAMODB_ITEM_BYTES:
            raise ValueError("Publication execution item exceeds the DynamoDB item bound")
        expected = operation.get("ExpressionAttributeValues", {}).get(":expected_payload")
        if expected is not None and _rendered_size(expected) >= MAX_DYNAMODB_ITEM_BYTES:
            raise ValueError("Publication execution CAS payload exceeds the DynamoDB item bound")
    if _rendered_size(items) >= MAX_DYNAMODB_TRANSACTION_BYTES:
        raise ValueError("Publication execution transaction exceeds the DynamoDB envelope")


def _entity_for_root(record: BaseModel) -> str:
    mapping: tuple[tuple[type[BaseModel], str], ...] = (
        (ExecutionPublicationAggregate, "PUBLICATION_EXECUTION_AGGREGATE"),
        (PublicationAggregate, "PUBLICATION_AGGREGATE"),
        (ExecutionPublicationAttempt, "PUBLICATION_EXECUTION_ATTEMPT"),
        (PublicationAttempt, "PUBLICATION_ATTEMPT"),
        (ExecutionPublicationPermit, "PUBLICATION_EXECUTION_PERMIT"),
        (PublicationPermit, "PUBLICATION_PERMIT"),
        (ExecutionPublicationWork, "PUBLICATION_EXECUTION_WORK"),
        (PublicationWorkRequest, "PUBLICATION_WORK_REQUEST"),
    )
    for model, entity_type in mapping:
        if isinstance(record, model):
            return entity_type
    raise TypeError("Unknown publication execution root")


class DynamoDBPublicationExecutionStore:
    """Strong-read, full-payload-CAS implementation of ``PublicationExecutionStore``."""

    def __init__(self, *, client: Any, table_name: str) -> None:
        self._client = client
        self._table_name = table_name
        self._request_store = DynamoDBPublicationStore(client=client, table_name=table_name)

    def resolve_execution_receipt(
        self,
        owner_id: str,
        aggregate_id: str,
        operation_id: str,
    ) -> PublicationExecutionReceipt | None:
        try:
            self._owner_meta(owner_id, aggregate_id)
        except PublicationNotFoundError:
            return None
        item = self._get(
            _publication_pk(aggregate_id),
            f"EXECUTION_RECEIPT#{operation_id}",
        )
        if item is None:
            return None
        receipt = self._parse(
            item,
            "PUBLICATION_EXECUTION_RECEIPT",
            PublicationExecutionReceipt,
            missing=False,
        )
        if (
            receipt.owner_id != owner_id
            or receipt.aggregate_id != aggregate_id
            or receipt.operation_id != operation_id
        ):
            return None
        return receipt

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        self._owner_meta(owner_id, aggregate_id)
        items = self._query_partition(aggregate_id)
        by_sort_key = {item["SK"]["S"]: item for item in items}
        meta = by_sort_key.get("META")
        if meta is None or meta.get("owner_id", {}).get("S") != owner_id:
            raise PublicationNotFoundError()

        snapshot = self._one_prefix(
            items,
            "SNAPSHOT#",
            "PUBLICATION_SNAPSHOT",
            PublicationSnapshot,
        )
        raw_aggregate = self._parse_union(
            meta,
            {
                "PUBLICATION_AGGREGATE": PublicationAggregate,
                "PUBLICATION_EXECUTION_AGGREGATE": ExecutionPublicationAggregate,
            },
        )
        raw_attempt = self._one_union_prefix(
            items,
            "ATTEMPT#",
            {
                "PUBLICATION_ATTEMPT": PublicationAttempt,
                "PUBLICATION_EXECUTION_ATTEMPT": ExecutionPublicationAttempt,
            },
        )
        raw_permit = self._one_union_prefix(
            items,
            "PERMIT#",
            {
                "PUBLICATION_PERMIT": PublicationPermit,
                "PUBLICATION_EXECUTION_PERMIT": ExecutionPublicationPermit,
            },
        )
        raw_work = self._one_union_prefix(
            items,
            "PUBLICATION_WORK#",
            {
                "PUBLICATION_WORK_REQUEST": PublicationWorkRequest,
                "PUBLICATION_EXECUTION_WORK": ExecutionPublicationWork,
            },
        )
        if not isinstance(raw_aggregate, (PublicationAggregate, ExecutionPublicationAggregate)):
            self._invalid("Publication aggregate row is invalid")
        if isinstance(raw_aggregate, PublicationAggregate):
            if not (
                isinstance(raw_attempt, PublicationAttempt)
                and isinstance(raw_permit, PublicationPermit)
                and isinstance(raw_work, PublicationWorkRequest)
            ):
                self._invalid("Pristine publication roots are mixed with execution rows")
            aggregate = ExecutionPublicationAggregate.from_request(raw_aggregate, snapshot)
            attempt = ExecutionPublicationAttempt.from_request(raw_attempt)
            permit = ExecutionPublicationPermit.from_request(
                raw_permit,
                snapshot.verification_deadline,
            )
            work = ExecutionPublicationWork.from_request(raw_work)
        else:
            if not (
                isinstance(raw_attempt, ExecutionPublicationAttempt)
                and isinstance(raw_permit, ExecutionPublicationPermit)
                and isinstance(raw_work, ExecutionPublicationWork)
            ):
                self._invalid("Evolved publication roots are incomplete")
            aggregate = raw_aggregate
            attempt = raw_attempt
            permit = raw_permit
            work = raw_work

        job = self._load_job(owner_id, aggregate.job_id, aggregate_id)
        request_link = PublicationJobLink(
            owner_id=owner_id,
            job_id=aggregate.job_id,
            expected_record_version=snapshot.expected_record_version,
            result_record_version=snapshot.expected_record_version + 1,
            expected_event_sequence=job.event_sequence,
            result_event_sequence=job.event_sequence,
            publication_aggregate_id=aggregate_id,
            linked_at=snapshot.requested_at,
        )
        claims = tuple(
            sorted(
                self._many_prefix(
                    items,
                    "CALL_CLAIM#",
                    "PUBLICATION_CALL_CLAIM",
                    PublicationCallClaim,
                ),
                key=lambda value: value.resulting_attempt_record_version,
            )
        )
        audits = tuple(
            sorted(
                self._many_prefix(
                    items,
                    "PROVIDER_AUDIT#",
                    "PUBLICATION_PROVIDER_AUDIT",
                    PublicationProviderAuditBinding,
                ),
                key=lambda value: value.durable_call_sequence,
            )
        )
        observations = tuple(
            sorted(
                self._many_prefix(
                    items,
                    "PRODUCT_OBSERVATION#",
                    "PUBLICATION_PRODUCT_OBSERVATION",
                    PublicationProductObservation,
                ),
                key=lambda value: (
                    value.resulting_aggregate_record_version,
                    value.observation_id,
                ),
            )
        )
        return PublicationExecutionAuthority(
            snapshot=snapshot,
            request_job_link=request_link,
            phase6_record_version=job.record_version,
            phase6_event_sequence=job.event_sequence,
            expected_aggregate=raw_aggregate,
            expected_attempt=raw_attempt,
            expected_permit=raw_permit,
            expected_work=raw_work,
            aggregate=aggregate,
            attempt=attempt,
            permit=permit,
            work=work,
            call_claims=claims,
            provider_audits=audits,
            provider_authority=self._optional_static(
                by_sort_key,
                "PROVIDER_AUTHORITY",
                "PUBLICATION_PROVIDER_AUTHORITY",
                PublicationProviderAuthority,
            ),
            preflight_proof=self._optional_static(
                by_sort_key,
                "PREFLIGHT",
                "PUBLICATION_PREFLIGHT",
                PublicationPreflightProof,
            ),
            mutation_claim=self._optional_static(
                by_sort_key,
                "MUTATION",
                "PUBLICATION_MUTATION_CLAIM",
                PublicationMutationClaim,
            ),
            post_observation=self._optional_static(
                by_sort_key,
                "POST_OBSERVATION",
                "PUBLICATION_POST_OBSERVATION",
                PublicationPostObservation,
            ),
            product_observations=observations,
            last_product_observation=observations[-1] if observations else None,
            result=self._optional_static(
                by_sort_key,
                "RESULT",
                "PUBLICATION_RESULT",
                PublicationResult,
            ),
            notification=self._optional_static(
                by_sort_key,
                "NOTIFICATION",
                "PUBLICATION_NOTIFICATION",
                PublicationNotification,
            ),
            report=self._optional_static(
                by_sort_key,
                "REPORT",
                "PUBLICATION_TERMINAL_REPORT",
                PublicationTerminalReport,
            ),
            tombstone=self._optional_static(
                by_sort_key,
                "TOMBSTONE",
                "PUBLICATION_AGGREGATE_TOMBSTONE",
                PublicationAggregateTombstone,
            ),
            terminal_job_link=self._optional_static(
                by_sort_key,
                "TERMINAL_JOB_LINK",
                "PUBLICATION_TERMINAL_JOB_LINK",
                PublicationTerminalJobLink,
            ),
        )

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRequestAuthority:
        meta = self._owner_meta(owner_id, aggregate_id)
        job_id = meta.get("job_id", {}).get("S")
        if not isinstance(job_id, str):
            self._invalid("Publication aggregate lacks its job link")
        authority = self._request_store.load_request_authority(owner_id, job_id)
        if authority.current_job.publication_aggregate_id != aggregate_id:
            raise PublicationNotFoundError()
        return authority

    def load_linked_job(self, owner_id: str, aggregate_id: str) -> ControlJobRecord:
        meta = self._owner_meta(owner_id, aggregate_id)
        job_id = meta.get("job_id", {}).get("S")
        if not isinstance(job_id, str):
            self._invalid("Publication aggregate lacks its job link")
        return self._load_job(owner_id, job_id, aggregate_id)

    def get_provider_evidence_stage(
        self,
        owner_id: str,
        aggregate_id: str,
        stage_id: str,
    ) -> PublicationProviderEvidenceStage:
        self._owner_meta(owner_id, aggregate_id)
        item = self._get(
            _publication_pk(aggregate_id),
            f"PROVIDER_EVIDENCE#{stage_id}",
        )
        stage = self._parse(
            item,
            "PUBLICATION_PROVIDER_EVIDENCE",
            PublicationProviderEvidenceStage,
        )
        if stage.aggregate_id != aggregate_id or stage.stage_id != stage_id:
            raise PublicationNotFoundError()
        return stage

    def list_unconsumed_provider_evidence(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> tuple[PublicationProviderEvidenceStage, ...]:
        self._owner_meta(owner_id, aggregate_id)
        items = self._query_partition(aggregate_id)
        stages = self._many_prefix(
            items,
            "PROVIDER_EVIDENCE#",
            "PUBLICATION_PROVIDER_EVIDENCE",
            PublicationProviderEvidenceStage,
        )
        consumed = {
            value.stage_id
            for value in self._many_prefix(
                items,
                "PROVIDER_EVIDENCE_CONSUMED#",
                "PUBLICATION_PROVIDER_EVIDENCE_CONSUMPTION",
                PublicationProviderEvidenceConsumption,
            )
        }
        unconsumed = tuple(stage for stage in stages if stage.stage_id not in consumed)
        claims = self._many_prefix(
            items,
            "CALL_CLAIM#",
            "PUBLICATION_CALL_CLAIM",
            PublicationCallClaim,
        )
        claim_sequence_by_id = {
            claim.authorization_id: claim.resulting_attempt_record_version for claim in claims
        }
        if len(claim_sequence_by_id) != len(claims) or any(
            stage.call_claim_id not in claim_sequence_by_id for stage in unconsumed
        ):
            self._invalid("Provider evidence stage lacks its durable call claim")
        return tuple(
            sorted(
                unconsumed,
                key=lambda stage: (
                    claim_sequence_by_id[stage.call_claim_id],
                    stage.stage_id,
                ),
            )
        )

    def stage_evidence(
        self,
        commit: PublicationProviderEvidenceCommit,
    ) -> PublicationProviderEvidenceStage:
        try:
            revalidated = PublicationProviderEvidenceCommit.model_validate(
                commit.model_dump(mode="python")
            )
        except (AttributeError, ValidationError, ValueError):
            self._invalid("Provider evidence commit failed strict revalidation")
        if revalidated != commit:
            self._invalid("Provider evidence commit failed strict revalidation")
        stage = commit.stage
        owner_id = commit.expected.snapshot.owner_id
        try:
            existing = self.get_provider_evidence_stage(
                owner_id,
                stage.aggregate_id,
                stage.stage_id,
            )
        except PublicationNotFoundError:
            existing = None
        if existing is not None:
            if existing == stage:
                return existing
            self._concurrent("Provider evidence stage identity was reused")

        expected = commit.expected
        claim = next(
            value for value in expected.call_claims if value.authorization_id == stage.call_claim_id
        )
        audit = next(
            value
            for value in expected.provider_audits
            if value.call_claim_id == stage.call_claim_id
            and value.fingerprint == stage.allowed_audit_binding_fingerprint
        )
        provider_authority = expected.provider_authority
        if provider_authority is None:
            self._invalid("Provider evidence lacks reconstructed authority")
        job = self._load_job(
            owner_id,
            expected.snapshot.job_id,
            stage.aggregate_id,
        )
        if (
            job.record_version != expected.phase6_record_version
            or job.event_sequence != expected.phase6_event_sequence
        ):
            self._concurrent("Phase 6 authority changed before evidence staging")
        pk = _publication_pk(stage.aggregate_id)
        items = [
            _put_exact(
                self._table_name,
                _record_item(
                    stage.aggregate_id,
                    "META",
                    "PUBLICATION_EXECUTION_AGGREGATE",
                    commit.updated_aggregate,
                ),
                expected_entity_type="PUBLICATION_EXECUTION_AGGREGATE",
                expected=commit.expected_aggregate,
            ),
            _condition_exact(
                self._table_name,
                partition_key=pk,
                sort_key=f"CALL_CLAIM#{claim.authorization_id}",
                entity_type="PUBLICATION_CALL_CLAIM",
                expected=claim,
            ),
            _condition_exact(
                self._table_name,
                partition_key=pk,
                sort_key=f"PROVIDER_AUDIT#{audit.durable_call_sequence:020d}",
                entity_type="PUBLICATION_PROVIDER_AUDIT",
                expected=audit,
            ),
            _condition_exact(
                self._table_name,
                partition_key=pk,
                sort_key="PROVIDER_AUTHORITY",
                entity_type="PUBLICATION_PROVIDER_AUTHORITY",
                expected=provider_authority,
            ),
            _condition_exact(
                self._table_name,
                partition_key=_job_pk(job.job_id),
                sort_key="META",
                entity_type="CONTROL_JOB",
                expected=job,
            ),
            _put_new(
                self._table_name,
                _record_item(
                    stage.aggregate_id,
                    f"PROVIDER_EVIDENCE#{stage.stage_id}",
                    "PUBLICATION_PROVIDER_EVIDENCE",
                    stage,
                ),
            ),
        ]
        try:
            self._transact(items)
        except PublicationConflictError:
            try:
                replay = self.get_provider_evidence_stage(
                    owner_id,
                    stage.aggregate_id,
                    stage.stage_id,
                )
            except PublicationNotFoundError:
                replay = None
            if replay == stage:
                return replay
            raise
        return stage

    def commit_execution(
        self,
        commit: PublicationExecutionCommit,
    ) -> PublicationExecutionCommitResult:
        validate_execution_commit(commit)
        receipt = commit.receipt
        try:
            existing = self.resolve_execution_receipt(
                receipt.owner_id,
                receipt.aggregate_id,
                receipt.operation_id,
            )
        except PublicationNotFoundError:
            existing = None
        if existing is not None:
            if existing.request_fingerprint == receipt.request_fingerprint:
                return PublicationExecutionCommitResult(receipt=existing)
            raise PublicationIdempotencyConflictError()
        items = self._execution_transaction_items(commit)
        try:
            self._transact(items)
        except PublicationConflictError:
            try:
                replay = self.resolve_execution_receipt(
                    receipt.owner_id,
                    receipt.aggregate_id,
                    receipt.operation_id,
                )
            except PublicationNotFoundError:
                replay = None
            if replay is not None:
                if replay.request_fingerprint == receipt.request_fingerprint:
                    return PublicationExecutionCommitResult(receipt=replay)
                raise PublicationIdempotencyConflictError() from None
            raise
        if commit.new_call_claim is None:
            return PublicationExecutionCommitResult(receipt=receipt)
        if commit.new_mutation_claim is not None:
            grant: FreshPublicationCallGrant = FreshPublicationMutationGrant._mint(
                commit.new_call_claim,
                commit.new_mutation_claim,
            )
        else:
            grant = FreshPublicationCallGrant._mint(commit.new_call_claim)
        return PublicationExecutionCommitResult(receipt=receipt, fresh_call_grant=grant)

    def commit_provider_audit(
        self,
        commit: PublicationProviderAuditCommit,
    ) -> PublicationProviderAuditBinding:
        try:
            revalidated = PublicationProviderAuditCommit.model_validate(
                commit.model_dump(mode="python")
            )
        except (AttributeError, ValidationError, ValueError):
            self._invalid("Provider audit commit failed strict revalidation")
        if revalidated != commit:
            self._invalid("Provider audit commit failed strict revalidation")
        binding = commit.binding
        pk = _publication_pk(binding.aggregate_id)
        audit_sk = f"PROVIDER_AUDIT#{binding.durable_call_sequence:020d}"
        existing_item = self._get(pk, audit_sk)
        if existing_item is not None:
            existing = self._parse(
                existing_item,
                "PUBLICATION_PROVIDER_AUDIT",
                PublicationProviderAuditBinding,
                missing=False,
            )
            if existing == binding:
                return existing
            self._concurrent("Provider audit sequence already exists")
        claim_item = self._get(pk, f"CALL_CLAIM#{binding.call_claim_id}")
        claim = self._parse(
            claim_item,
            "PUBLICATION_CALL_CLAIM",
            PublicationCallClaim,
        )
        if (
            commit.expected_aggregate.terminal_at is not None
            or claim.aggregate_id != binding.aggregate_id
            or claim.authorization_id != binding.call_claim_id
            or claim.fingerprint != binding.call_claim_fingerprint
            or claim.resulting_attempt_record_version != binding.durable_call_sequence
        ):
            self._concurrent("Provider audit lost its exact nonterminal call authority")
        items = [
            _put_exact(
                self._table_name,
                _record_item(
                    binding.aggregate_id,
                    "META",
                    "PUBLICATION_EXECUTION_AGGREGATE",
                    commit.updated_aggregate,
                ),
                expected_entity_type="PUBLICATION_EXECUTION_AGGREGATE",
                expected=commit.expected_aggregate,
            ),
            _condition_exact(
                self._table_name,
                partition_key=pk,
                sort_key=f"CALL_CLAIM#{binding.call_claim_id}",
                entity_type="PUBLICATION_CALL_CLAIM",
                expected=claim,
            ),
            _put_new(
                self._table_name,
                _record_item(
                    binding.aggregate_id,
                    audit_sk,
                    "PUBLICATION_PROVIDER_AUDIT",
                    binding,
                ),
            ),
        ]
        try:
            self._transact(items)
        except PublicationConflictError:
            replay_item = self._get(pk, audit_sk)
            if replay_item is not None:
                replay = self._parse(
                    replay_item,
                    "PUBLICATION_PROVIDER_AUDIT",
                    PublicationProviderAuditBinding,
                    missing=False,
                )
                if replay == binding:
                    return replay
            raise
        return binding

    def _execution_transaction_items(
        self,
        commit: PublicationExecutionCommit,
    ) -> list[dict[str, Any]]:
        expected = commit.expected
        aggregate_id = expected.aggregate.aggregate_id
        pk = _publication_pk(aggregate_id)
        items = [
            _put_exact(
                self._table_name,
                _record_item(
                    aggregate_id,
                    "META",
                    "PUBLICATION_EXECUTION_AGGREGATE",
                    commit.updated_aggregate,
                ),
                expected_entity_type=_entity_for_root(expected.expected_aggregate),
                expected=expected.expected_aggregate,
            ),
            _put_exact(
                self._table_name,
                _record_item(
                    aggregate_id,
                    f"ATTEMPT#{commit.updated_attempt.attempt_id}",
                    "PUBLICATION_EXECUTION_ATTEMPT",
                    commit.updated_attempt,
                ),
                expected_entity_type=_entity_for_root(expected.expected_attempt),
                expected=expected.expected_attempt,
            ),
            _put_exact(
                self._table_name,
                _record_item(
                    aggregate_id,
                    f"PERMIT#{commit.updated_permit.permit_id}",
                    "PUBLICATION_EXECUTION_PERMIT",
                    commit.updated_permit,
                ),
                expected_entity_type=_entity_for_root(expected.expected_permit),
                expected=expected.expected_permit,
            ),
            _put_exact(
                self._table_name,
                _record_item(
                    aggregate_id,
                    f"PUBLICATION_WORK#{commit.updated_work.work_request_id}",
                    "PUBLICATION_EXECUTION_WORK",
                    commit.updated_work,
                ),
                expected_entity_type=_entity_for_root(expected.expected_work),
                expected=expected.expected_work,
            ),
        ]

        immutable: list[tuple[BaseModel | None, str, str]] = [
            (
                commit.new_call_claim,
                f"CALL_CLAIM#{commit.new_call_claim.authorization_id}"
                if commit.new_call_claim is not None
                else "CALL_CLAIM#",
                "PUBLICATION_CALL_CLAIM",
            ),
            (commit.new_provider_authority, "PROVIDER_AUTHORITY", "PUBLICATION_PROVIDER_AUTHORITY"),
            (commit.new_preflight_proof, "PREFLIGHT", "PUBLICATION_PREFLIGHT"),
            (commit.new_mutation_claim, "MUTATION", "PUBLICATION_MUTATION_CLAIM"),
            (commit.new_post_observation, "POST_OBSERVATION", "PUBLICATION_POST_OBSERVATION"),
            (
                commit.new_product_observation,
                f"PRODUCT_OBSERVATION#{commit.new_product_observation.observation_id}"
                if commit.new_product_observation is not None
                else "PRODUCT_OBSERVATION#",
                "PUBLICATION_PRODUCT_OBSERVATION",
            ),
            (commit.new_result, "RESULT", "PUBLICATION_RESULT"),
            (commit.new_notification, "NOTIFICATION", "PUBLICATION_NOTIFICATION"),
            (commit.new_report, "REPORT", "PUBLICATION_TERMINAL_REPORT"),
            (commit.new_tombstone, "TOMBSTONE", "PUBLICATION_AGGREGATE_TOMBSTONE"),
        ]
        for record, sort_key, entity_type in immutable:
            if record is not None:
                items.append(
                    _put_new(
                        self._table_name,
                        _record_item(aggregate_id, sort_key, entity_type, record),
                    )
                )
        items.extend(
            (
                _put_new(
                    self._table_name,
                    _record_item(
                        aggregate_id,
                        f"EVENT#{commit.event.sequence:020d}",
                        "PUBLICATION_EXECUTION_EVENT",
                        commit.event,
                    ),
                ),
                _put_new(
                    self._table_name,
                    _record_item(
                        aggregate_id,
                        f"EXECUTION_RECEIPT#{commit.receipt.operation_id}",
                        "PUBLICATION_EXECUTION_RECEIPT",
                        commit.receipt,
                    ),
                ),
            )
        )
        for stage, consumption in zip(
            commit.expected_provider_evidence_stages,
            commit.new_provider_evidence_consumptions,
            strict=True,
        ):
            items.extend(
                (
                    _condition_exact(
                        self._table_name,
                        partition_key=pk,
                        sort_key=f"PROVIDER_EVIDENCE#{stage.stage_id}",
                        entity_type="PUBLICATION_PROVIDER_EVIDENCE",
                        expected=stage,
                    ),
                    _put_new(
                        self._table_name,
                        _record_item(
                            aggregate_id,
                            f"PROVIDER_EVIDENCE_CONSUMED#{consumption.stage_id}",
                            "PUBLICATION_PROVIDER_EVIDENCE_CONSUMPTION",
                            consumption,
                        ),
                    ),
                )
            )
        if commit.terminal_job_update is not None:
            update = commit.terminal_job_update
            items.extend(
                (
                    _put_exact(
                        self._table_name,
                        _job_item(update.updated_job),
                        expected_entity_type="CONTROL_JOB",
                        expected=update.expected_job,
                    ),
                    _put_new(
                        self._table_name,
                        _record_item(
                            aggregate_id,
                            "TERMINAL_JOB_LINK",
                            "PUBLICATION_TERMINAL_JOB_LINK",
                            update.link,
                        ),
                    ),
                )
            )
        else:
            job = self._load_job(
                expected.snapshot.owner_id,
                expected.snapshot.job_id,
                aggregate_id,
            )
            if (
                job.record_version != expected.phase6_record_version
                or job.event_sequence != expected.phase6_event_sequence
            ):
                self._concurrent("Phase 6 authority changed before execution commit")
            items.append(
                _condition_exact(
                    self._table_name,
                    partition_key=_job_pk(job.job_id),
                    sort_key="META",
                    entity_type="CONTROL_JOB",
                    expected=job,
                )
            )
        return items

    def _owner_meta(self, owner_id: str, aggregate_id: str) -> dict[str, Any]:
        item = self._get(_publication_pk(aggregate_id), "META")
        if item is None or item.get("owner_id", {}).get("S") != owner_id:
            raise PublicationNotFoundError()
        if item.get("entity_type", {}).get("S") not in {
            "PUBLICATION_AGGREGATE",
            "PUBLICATION_EXECUTION_AGGREGATE",
        }:
            self._invalid("Publication aggregate row is invalid")
        return item

    def _load_job(
        self,
        owner_id: str,
        job_id: str,
        aggregate_id: str,
    ) -> ControlJobRecord:
        item = self._get(_job_pk(job_id), "META")
        job = self._parse(item, "CONTROL_JOB", ControlJobRecord)
        if (
            job.owner_id != owner_id
            or job.job_id != job_id
            or job.publication_aggregate_id != aggregate_id
        ):
            raise PublicationNotFoundError()
        return job

    def _query_partition(self, aggregate_id: str) -> list[dict[str, Any]]:
        partition_key = _publication_pk(aggregate_id)
        items: list[dict[str, Any]] = []
        item_keys: set[tuple[str, str]] = set()
        seen_cursors: set[tuple[str, str]] = set()
        cursor: dict[str, Any] | None = None
        last_sort_key: str | None = None
        page_count = 0
        while True:
            page_count += 1
            if page_count > MAX_EXECUTION_AUTHORITY_ITEMS + 1:
                self._invalid("Publication execution query did not make bounded progress")
            request: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": _s(partition_key)},
                "ConsistentRead": True,
                "Limit": MAX_EXECUTION_AUTHORITY_ITEMS - len(items) + 1,
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._client.query(**request)
            page = response.get("Items", [])
            if not isinstance(page, list):
                self._invalid("Publication execution query returned an invalid page")
            for item in page:
                if not isinstance(item, dict):
                    self._invalid("Publication execution query returned an invalid item")
                key = self._item_key(item)
                if (
                    key[0] != partition_key
                    or key in item_keys
                    or (last_sort_key is not None and key[1] <= last_sort_key)
                ):
                    self._invalid(
                        "Publication execution query returned duplicate or unordered rows"
                    )
                item_keys.add(key)
                last_sort_key = key[1]
                items.append(item)
                if len(items) > MAX_EXECUTION_AUTHORITY_ITEMS:
                    self._invalid("Publication execution authority exceeds its bounded read")

            next_cursor = response.get("LastEvaluatedKey")
            if not next_cursor:
                break
            next_key = self._item_key(next_cursor)
            if not page or next_key != self._item_key(page[-1]) or next_key in seen_cursors:
                self._invalid("Publication execution query cursor did not make progress")
            seen_cursors.add(next_key)
            cursor = next_cursor

        roots = [item for item in items if item.get("SK", {}).get("S") == "META"]
        if len(roots) != 1:
            self._invalid("Publication execution query lacks one exact root")
        if self._get(partition_key, "META") != roots[0]:
            self._concurrent("Publication execution root changed during its bounded read")
        return items

    @staticmethod
    def _item_key(item: dict[str, Any]) -> tuple[str, str]:
        if set(item) >= {"PK", "SK"}:
            partition_value = item.get("PK")
            sort_value = item.get("SK")
            if isinstance(partition_value, dict) and isinstance(sort_value, dict):
                partition_key = partition_value.get("S")
                sort_key = sort_value.get("S")
                if isinstance(partition_key, str) and isinstance(sort_key, str):
                    return partition_key, sort_key
        DynamoDBPublicationExecutionStore._invalid(
            "Publication execution query returned an invalid key"
        )

    def _get(self, partition_key: str, sort_key: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": _s(partition_key), "SK": _s(sort_key)},
            ConsistentRead=True,
        )
        return response.get("Item")

    def _transact(self, items: list[dict[str, Any]]) -> None:
        _validate_envelope(items)
        try:
            self._client.transact_write_items(
                TransactItems=items,
                ClientRequestToken=_transaction_token(items),
            )
        except ClientError as error:
            if not self._is_concurrency(error):
                raise
            self._concurrent("Publication execution authority changed before commit")

    @staticmethod
    def _parse(
        item: dict[str, Any] | None,
        entity_type: str,
        model: type[RecordT],
        *,
        missing: bool = True,
    ) -> RecordT:
        payload = None if item is None else item.get("payload", {}).get("S")
        if item is None or item.get("entity_type", {}).get("S") != entity_type or not payload:
            if missing:
                raise PublicationNotFoundError()
            DynamoDBPublicationExecutionStore._invalid("Publication record is invalid")
        try:
            return model.model_validate_json(payload)
        except (ValidationError, ValueError):
            if missing:
                raise PublicationNotFoundError() from None
            DynamoDBPublicationExecutionStore._invalid("Publication record is invalid")

    @staticmethod
    def _parse_union(
        item: dict[str, Any],
        models: dict[str, type[RecordT]],
    ) -> RecordT:
        entity_type = item.get("entity_type", {}).get("S")
        model = models.get(entity_type)
        payload = item.get("payload", {}).get("S")
        if model is None or not payload:
            DynamoDBPublicationExecutionStore._invalid("Publication record is invalid")
        try:
            return model.model_validate_json(payload)
        except (ValidationError, ValueError):
            DynamoDBPublicationExecutionStore._invalid("Publication record is invalid")

    def _one_prefix(
        self,
        items: list[dict[str, Any]],
        prefix: str,
        entity_type: str,
        model: type[RecordT],
    ) -> RecordT:
        matches = [item for item in items if item.get("SK", {}).get("S", "").startswith(prefix)]
        if len(matches) != 1:
            self._invalid("Publication execution authority is incomplete")
        return self._parse(matches[0], entity_type, model, missing=False)

    def _one_union_prefix(
        self,
        items: list[dict[str, Any]],
        prefix: str,
        models: dict[str, type[RecordT]],
    ) -> RecordT:
        matches = [item for item in items if item.get("SK", {}).get("S", "").startswith(prefix)]
        if len(matches) != 1:
            self._invalid("Publication execution authority is incomplete")
        return self._parse_union(matches[0], models)

    def _many_prefix(
        self,
        items: list[dict[str, Any]],
        prefix: str,
        entity_type: str,
        model: type[RecordT],
    ) -> tuple[RecordT, ...]:
        return tuple(
            self._parse(item, entity_type, model, missing=False)
            for item in items
            if item.get("SK", {}).get("S", "").startswith(prefix)
        )

    def _optional_static(
        self,
        by_sort_key: dict[str, dict[str, Any]],
        sort_key: str,
        entity_type: str,
        model: type[RecordT],
    ) -> RecordT | None:
        item = by_sort_key.get(sort_key)
        if item is None:
            return None
        return self._parse(item, entity_type, model, missing=False)

    @staticmethod
    def _is_concurrency(error: ClientError) -> bool:
        code = error.response.get("Error", {}).get("Code")
        if code == "IdempotentParameterMismatchException":
            return True
        if code != "TransactionCanceledException":
            return False
        reasons = error.response.get("CancellationReasons")
        if not isinstance(reasons, list) or not reasons:
            return False
        reason_codes = {
            reason.get("Code")
            for reason in reasons
            if isinstance(reason, dict) and reason.get("Code") not in {None, "None"}
        }
        return bool(reason_codes) and reason_codes.issubset(
            {"ConditionalCheckFailed", "TransactionConflict"}
        )

    @staticmethod
    def _invalid(message: str) -> None:
        raise PublicationConflictError(PublicationErrorCode.INVALID_AUTHORITY, message)

    @staticmethod
    def _concurrent(message: str) -> None:
        raise PublicationConflictError(PublicationErrorCode.CONCURRENT_WRITE, message)
