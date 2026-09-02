"""Private read-only composition for the Phase 7 approval-version guard.

This runtime has no seller route, workflow starter, credential resolver, HTTP transport, or
execution transition service.  It constructs one DynamoDB reader whose injected client surface is
reduced to strongly consistent ``GetItem`` and bounded ``Query`` calls, then delegates the exact
authority comparison to :class:`DurablePublicationPreCallGuard`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from mr_lister.contracts import ProductProfile
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ReviewContent,
    ReviewDecisionRecord,
    SourceArtifactRecord,
)
from mr_lister.publication.errors import PublicationNotFoundError
from mr_lister.publication.evidence_provenance import (
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
    PublicationExecutionEvent,
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
from mr_lister.publication.guard_verification import (
    DurablePublicationPreCallGuard,
    PublicationGuardRequest,
    PublicationGuardRuntimeActivation,
    PublicationGuardSourceAuthority,
    PublicationGuardVerificationService,
    validate_publication_guard_source_authority,
)
from mr_lister.publication.models import (
    PublicationAggregate,
    PublicationAttempt,
    PublicationDomainEvent,
    PublicationJobLink,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
)
from mr_lister.publication.profile_eligibility import (
    PinnedPublicationProfileEligibilityAuthority,
    PublicationProfileEligibility,
    build_publication_profile_eligibility,
    require_exact_publication_profile_eligibility,
)
from mr_lister.publication.retention_locator import (
    PUBLICATION_REQUEST_RECEIPT_LOCATOR_ENTITY_TYPE,
    PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY,
    PublicationRequestReceiptLocator,
)
from mr_lister.review_profile import ExactReviewProductProfile

Phase7GuardAwsService = Literal["dynamodb"]

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_TABLE = re.compile(r"^mr-lister-phase6-(?P<environment>[a-z][a-z0-9-]{1,15})$")
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_GENERIC_CONFIGURATION_ERROR = "Phase 7 approval guard configuration is invalid"
_MAX_AUTHORITY_ITEMS = 943
assert _MAX_AUTHORITY_ITEMS == 943


class Phase7GuardConfigurationError(RuntimeError):
    """Value-free failure for malformed, enabled, drifting, or unsealed configuration."""


class Phase7GuardAwsClientFactory(Protocol):
    def __call__(
        self,
        service_name: Phase7GuardAwsService,
        *,
        region_name: str,
    ) -> object: ...


class Phase7GuardHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PinnedGuardProfileConfiguration:
    path: Path
    exact: ExactReviewProductProfile


@dataclass(frozen=True, slots=True)
class Phase7GuardConfiguration:
    region: str
    environment_name: str
    state_table: str
    guard_release_fingerprint: str
    application_release_fingerprint: str
    profile: PinnedGuardProfileConfiguration
    eligibility: PublicationProfileEligibility
    activation: PublicationGuardRuntimeActivation


class _PinnedGuardProfileAuthority:
    __slots__ = ("_exact",)

    def __init__(self, exact: ExactReviewProductProfile) -> None:
        self._exact = exact

    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
    ) -> ExactReviewProductProfile:
        if (
            profile_id != self._exact.profile.profile_id
            or profile_version != self._exact.profile.profile_version
        ):
            raise LookupError("The review product profile was not found")
        return self._exact


class _ReadOnlyDynamoClient:
    """Retain only two bound read callables, never the broader injected SDK object."""

    __slots__ = ("_get_item", "_query")

    def __init__(self, client: object) -> None:
        get_item = getattr(client, "get_item", None)
        query = getattr(client, "query", None)
        if not callable(get_item) or not callable(query):
            raise RuntimeError("Phase 7 approval guard dependency is unavailable")
        self._get_item = get_item
        self._query = query

    def get_item(self, **values: object) -> object:
        return self._get_item(**values)

    def query(self, **values: object) -> object:
        return self._query(**values)


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _bool(value: bool) -> dict[str, bool]:
    return {"BOOL": value}


def _owner_job_sort_key(job: ControlJobRecord) -> str:
    seconds = int(job.updated_at.timestamp())
    epoch_micros = seconds * 1_000_000 + job.updated_at.microsecond
    return f"{epoch_micros:020d}#{job.job_id}"


def _job_item(job: ControlJobRecord) -> dict[str, Any]:
    return {
        "PK": _s(f"JOB#{job.job_id}"),
        "SK": _s("META"),
        "entity_type": _s("CONTROL_JOB"),
        "contract_version": _s(job.contract_version),
        "owner_id": _s(job.owner_id),
        "owner_jobs_pk": _s(f"OWNER#{job.owner_id}"),
        "owner_jobs_sk": _s(_owner_job_sort_key(job)),
        "state": _s(job.state.value),
        "record_version": _n(job.record_version),
        "event_sequence": _n(job.event_sequence),
        "review_version": _n(job.review_version),
        "cancellation_requested": _bool(job.cancellation_requested_at is not None),
        "payload": _s(job.model_dump_json()),
    }


def _job_record_item(
    job_id: str,
    sort_key: str,
    entity_type: str,
    record: BaseModel,
) -> dict[str, Any]:
    return {
        "PK": _s(f"JOB#{job_id}"),
        "SK": _s(sort_key),
        "entity_type": _s(entity_type),
        "contract_version": _s(str(record.contract_version)),
        "payload": _s(record.model_dump_json()),
    }


def _publication_record_item(
    aggregate_id: str,
    sort_key: str,
    entity_type: str,
    record: BaseModel,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "PK": _s(f"PUBLICATION#{aggregate_id}"),
        "SK": _s(sort_key),
        "entity_type": _s(entity_type),
        "contract_version": _s(str(record.contract_version)),
        "payload": _s(record.model_dump_json()),
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


def _publication_record_sort_key(record: BaseModel) -> str:
    if isinstance(record, (PublicationAggregate, ExecutionPublicationAggregate)):
        return "META"
    if isinstance(record, PublicationSnapshot):
        return f"SNAPSHOT#{record.snapshot_id}"
    if isinstance(record, (PublicationAttempt, ExecutionPublicationAttempt)):
        return f"ATTEMPT#{record.attempt_id}"
    if isinstance(record, (PublicationPermit, ExecutionPublicationPermit)):
        return f"PERMIT#{record.permit_id}"
    if isinstance(record, (PublicationWorkRequest, ExecutionPublicationWork)):
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
    raise ValueError("Publication approval guard authority is invalid")


def _publication_model_for_row(sort_key: str, entity_type: str) -> type[BaseModel]:
    static: dict[tuple[str, str], type[BaseModel]] = {
        ("META", "PUBLICATION_AGGREGATE"): PublicationAggregate,
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
    exact = static.get((sort_key, entity_type))
    if exact is not None:
        return exact
    prefixes: tuple[tuple[str, str, type[BaseModel]], ...] = (
        ("SNAPSHOT#", "PUBLICATION_SNAPSHOT", PublicationSnapshot),
        ("ATTEMPT#", "PUBLICATION_ATTEMPT", PublicationAttempt),
        ("ATTEMPT#", "PUBLICATION_EXECUTION_ATTEMPT", ExecutionPublicationAttempt),
        ("PERMIT#", "PUBLICATION_PERMIT", PublicationPermit),
        ("PERMIT#", "PUBLICATION_EXECUTION_PERMIT", ExecutionPublicationPermit),
        ("PUBLICATION_WORK#", "PUBLICATION_WORK_REQUEST", PublicationWorkRequest),
        ("PUBLICATION_WORK#", "PUBLICATION_EXECUTION_WORK", ExecutionPublicationWork),
        ("EVENT#", "PUBLICATION_DOMAIN_EVENT", PublicationDomainEvent),
        ("EVENT#", "PUBLICATION_EXECUTION_EVENT", PublicationExecutionEvent),
        ("EXECUTION_RECEIPT#", "PUBLICATION_EXECUTION_RECEIPT", PublicationExecutionReceipt),
        ("CALL_CLAIM#", "PUBLICATION_CALL_CLAIM", PublicationCallClaim),
        ("PROVIDER_AUDIT#", "PUBLICATION_PROVIDER_AUDIT", PublicationProviderAuditBinding),
        (
            "PRODUCT_OBSERVATION#",
            "PUBLICATION_PRODUCT_OBSERVATION",
            PublicationProductObservation,
        ),
        ("PROVIDER_EVIDENCE#", "PUBLICATION_PROVIDER_EVIDENCE", PublicationProviderEvidenceStage),
        (
            "PROVIDER_EVIDENCE_CONSUMED#",
            "PUBLICATION_PROVIDER_EVIDENCE_CONSUMPTION",
            PublicationProviderEvidenceConsumption,
        ),
    )
    for prefix, expected_entity, model in prefixes:
        if sort_key.startswith(prefix) and entity_type == expected_entity:
            return model
    raise ValueError("Publication approval guard authority is invalid")


class _ReadOnlyPublicationGuardStore:
    """Strong-read reconstruction of exactly the graph consumed by the application guard."""

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: _ReadOnlyDynamoClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        self._owner_meta(owner_id, aggregate_id)
        items = self._query_partition(aggregate_id)
        records = self._validated_publication_inventory(items, aggregate_id)
        raw_aggregate = records.get("META")
        if not isinstance(raw_aggregate, (PublicationAggregate, ExecutionPublicationAggregate)):
            self._invalid()
        if raw_aggregate.owner_id != owner_id:
            raise PublicationNotFoundError()

        snapshots = [
            record for record in records.values() if isinstance(record, PublicationSnapshot)
        ]
        attempts = [
            record
            for record in records.values()
            if isinstance(record, (PublicationAttempt, ExecutionPublicationAttempt))
        ]
        permits = [
            record
            for record in records.values()
            if isinstance(record, (PublicationPermit, ExecutionPublicationPermit))
        ]
        works = [
            record
            for record in records.values()
            if isinstance(record, (PublicationWorkRequest, ExecutionPublicationWork))
        ]
        if not (len(snapshots) == len(attempts) == len(permits) == len(works) == 1):
            self._invalid()
        snapshot = snapshots[0]
        raw_attempt = attempts[0]
        raw_permit = permits[0]
        raw_work = works[0]
        if isinstance(raw_aggregate, PublicationAggregate):
            if not (
                isinstance(raw_attempt, PublicationAttempt)
                and isinstance(raw_permit, PublicationPermit)
                and isinstance(raw_work, PublicationWorkRequest)
            ):
                self._invalid()
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
                self._invalid()
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
                (record for record in records.values() if isinstance(record, PublicationCallClaim)),
                key=lambda value: value.resulting_attempt_record_version,
            )
        )
        audits = tuple(
            sorted(
                (
                    record
                    for record in records.values()
                    if isinstance(record, PublicationProviderAuditBinding)
                ),
                key=lambda value: value.durable_call_sequence,
            )
        )
        observations = tuple(
            sorted(
                (
                    record
                    for record in records.values()
                    if isinstance(record, PublicationProductObservation)
                ),
                key=lambda value: (
                    value.resulting_aggregate_record_version,
                    value.observation_id,
                ),
            )
        )
        try:
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
                provider_authority=self._optional_record(
                    records,
                    "PROVIDER_AUTHORITY",
                    PublicationProviderAuthority,
                ),
                preflight_proof=self._optional_record(
                    records,
                    "PREFLIGHT",
                    PublicationPreflightProof,
                ),
                mutation_claim=self._optional_record(
                    records,
                    "MUTATION",
                    PublicationMutationClaim,
                ),
                post_observation=self._optional_record(
                    records,
                    "POST_OBSERVATION",
                    PublicationPostObservation,
                ),
                product_observations=observations,
                last_product_observation=observations[-1] if observations else None,
                result=self._optional_record(
                    records,
                    "RESULT",
                    PublicationResult,
                ),
                notification=self._optional_record(
                    records,
                    "NOTIFICATION",
                    PublicationNotification,
                ),
                report=self._optional_record(
                    records,
                    "REPORT",
                    PublicationTerminalReport,
                ),
                tombstone=self._optional_record(
                    records,
                    "TOMBSTONE",
                    PublicationAggregateTombstone,
                ),
                terminal_job_link=self._optional_record(
                    records,
                    "TERMINAL_JOB_LINK",
                    PublicationTerminalJobLink,
                ),
            )
        except Exception:
            self._invalid()

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationGuardSourceAuthority:
        meta = self._owner_meta(owner_id, aggregate_id)
        job_id = meta.job_id
        authority = self._load_request_authority(owner_id, job_id)
        if authority.current_job.publication_aggregate_id != aggregate_id:
            raise PublicationNotFoundError()
        return authority

    def _load_request_authority(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationGuardSourceAuthority:
        job = self._parse_job(self._get(f"JOB#{job_id}", "META"), job_id=job_id)
        if job.owner_id != owner_id or job.job_id != job_id:
            raise PublicationNotFoundError()
        if (
            job.state is not ControlJobState.APPROVED
            or job.approval_decision_id is None
            or job.product_sync_id is None
            or job.pricing_snapshot_id is None
        ):
            self._invalid()
        authority = PublicationGuardSourceAuthority(
            current_job=job,
            review=self._authority_record(
                job_id,
                f"REVIEW#{job.review_version:020d}",
                "REVIEW",
                ReviewContent,
            ),
            approval_decision=self._authority_record(
                job_id,
                f"DECISION#{job.approval_decision_id}",
                "REVIEW_DECISION",
                ReviewDecisionRecord,
            ),
            source=self._authority_record(
                job_id,
                "SOURCE",
                "SOURCE_ARTIFACT",
                SourceArtifactRecord,
            ),
            product_sync=self._authority_record(
                job_id,
                f"PRODUCT_SYNC#{job.product_sync_id}",
                "PRODUCT_SYNC",
                ProductSyncRecord,
            ),
            pricing_snapshot=self._authority_record(
                job_id,
                f"PRICING#{job.pricing_snapshot_id}",
                "PRICING_SNAPSHOT",
                PricingSnapshot,
            ),
            pricing_evidence=self._authority_record(
                job_id,
                f"PRICING_EVIDENCE#{job.pricing_snapshot_id}",
                "PRICING_EVIDENCE",
                PricingEvidenceRecord,
            ),
        )
        try:
            validate_publication_guard_source_authority(authority)
        except Exception:
            self._invalid()
        return authority

    def _authority_record(
        self,
        job_id: str,
        sort_key: str,
        entity_type: str,
        model: type[BaseModel],
    ) -> Any:
        return self._parse_job_record(
            self._get(f"JOB#{job_id}", sort_key),
            job_id=job_id,
            sort_key=sort_key,
            entity_type=entity_type,
            model=model,
        )

    def _owner_meta(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationAggregate | ExecutionPublicationAggregate:
        item = self._get(f"PUBLICATION#{aggregate_id}", "META")
        if item is None:
            raise PublicationNotFoundError()
        entity_type = self._av_string(item, "entity_type")
        model = _publication_model_for_row("META", entity_type)
        aggregate = self._parse_publication_record(
            item,
            aggregate_id=aggregate_id,
            sort_key="META",
            entity_type=entity_type,
            model=model,
        )
        if not isinstance(aggregate, (PublicationAggregate, ExecutionPublicationAggregate)):
            self._invalid()
        if aggregate.owner_id != owner_id:
            raise PublicationNotFoundError()
        return aggregate

    def _load_job(
        self,
        owner_id: str,
        job_id: str,
        aggregate_id: str,
    ) -> ControlJobRecord:
        job = self._parse_job(self._get(f"JOB#{job_id}", "META"), job_id=job_id)
        if (
            job.owner_id != owner_id
            or job.job_id != job_id
            or job.publication_aggregate_id != aggregate_id
        ):
            raise PublicationNotFoundError()
        return job

    def _query_partition(self, aggregate_id: str) -> list[dict[str, Any]]:
        partition_key = f"PUBLICATION#{aggregate_id}"
        items: list[dict[str, Any]] = []
        item_keys: set[tuple[str, str]] = set()
        seen_cursors: set[tuple[str, str]] = set()
        cursor: dict[str, Any] | None = None
        last_sort_key: str | None = None
        page_count = 0
        while True:
            page_count += 1
            if page_count > _MAX_AUTHORITY_ITEMS + 1:
                self._invalid()
            request: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": partition_key}},
                "ConsistentRead": True,
                "Limit": _MAX_AUTHORITY_ITEMS - len(items) + 1,
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._client.query(**request)
            if not isinstance(response, Mapping):
                self._invalid()
            page = response.get("Items", [])
            if not isinstance(page, list):
                self._invalid()
            for item in page:
                if not isinstance(item, dict):
                    self._invalid()
                key = self._item_key(item)
                if (
                    key[0] != partition_key
                    or key in item_keys
                    or (last_sort_key is not None and key[1] <= last_sort_key)
                ):
                    self._invalid()
                item_keys.add(key)
                last_sort_key = key[1]
                items.append(item)
                if len(items) > _MAX_AUTHORITY_ITEMS:
                    self._invalid()
            next_cursor = response.get("LastEvaluatedKey")
            if not next_cursor:
                break
            if not isinstance(next_cursor, dict):
                self._invalid()
            next_key = self._item_key(next_cursor)
            if not page or next_key != self._item_key(page[-1]) or next_key in seen_cursors:
                self._invalid()
            seen_cursors.add(next_key)
            cursor = next_cursor
        roots = [item for item in items if item.get("SK", {}).get("S") == "META"]
        if len(roots) != 1 or self._get(partition_key, "META") != roots[0]:
            self._invalid()
        self._validated_publication_inventory(items, aggregate_id)
        return items

    def _get(self, partition_key: str, sort_key: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": {"S": partition_key}, "SK": {"S": sort_key}},
            ConsistentRead=True,
        )
        if not isinstance(response, Mapping):
            self._invalid()
        item = response.get("Item")
        if item is not None and not isinstance(item, dict):
            self._invalid()
        if item is not None and self._item_key(item) != (partition_key, sort_key):
            self._invalid()
        return item

    @staticmethod
    def _item_key(item: dict[str, Any]) -> tuple[str, str]:
        partition = item.get("PK")
        sort = item.get("SK")
        if (
            isinstance(partition, dict)
            and isinstance(sort, dict)
            and set(partition) == {"S"}
            and set(sort) == {"S"}
        ):
            partition_key = partition.get("S")
            sort_key = sort.get("S")
            if isinstance(partition_key, str) and isinstance(sort_key, str):
                return partition_key, sort_key
        _ReadOnlyPublicationGuardStore._invalid()

    @classmethod
    def _validated_publication_inventory(
        cls,
        items: list[dict[str, Any]],
        aggregate_id: str,
    ) -> dict[str, BaseModel]:
        records: dict[str, BaseModel] = {}
        for item in items:
            partition_key, sort_key = cls._item_key(item)
            if partition_key != f"PUBLICATION#{aggregate_id}" or sort_key in records:
                cls._invalid()
            entity_type = cls._av_string(item, "entity_type")
            model = _publication_model_for_row(sort_key, entity_type)
            records[sort_key] = cls._parse_publication_record(
                item,
                aggregate_id=aggregate_id,
                sort_key=sort_key,
                entity_type=entity_type,
                model=model,
            )
        root = records.get("META")
        if not isinstance(root, (PublicationAggregate, ExecutionPublicationAggregate)):
            cls._invalid()
        if any(
            getattr(record, "aggregate_id", aggregate_id) != aggregate_id
            or getattr(record, "owner_id", root.owner_id) != root.owner_id
            or getattr(record, "job_id", root.job_id) != root.job_id
            for record in records.values()
        ):
            cls._invalid()
        return records

    @classmethod
    def _parse_publication_record(
        cls,
        item: dict[str, Any],
        *,
        aggregate_id: str,
        sort_key: str,
        entity_type: str,
        model: type[BaseModel],
    ) -> BaseModel:
        if (
            cls._item_key(item) != (f"PUBLICATION#{aggregate_id}", sort_key)
            or cls._av_string(item, "entity_type") != entity_type
        ):
            cls._invalid()
        payload = cls._av_string(item, "payload")
        try:
            record = model.model_validate_json(payload, strict=True)
        except Exception:
            cls._invalid()
        if _publication_record_sort_key(record) != sort_key or item != _publication_record_item(
            aggregate_id, sort_key, entity_type, record
        ):
            cls._invalid()
        record_aggregate_id = getattr(record, "aggregate_id", aggregate_id)
        if record_aggregate_id != aggregate_id:
            cls._invalid()
        return record

    @classmethod
    def _parse_job(
        cls,
        item: dict[str, Any] | None,
        *,
        job_id: str,
    ) -> ControlJobRecord:
        if item is None:
            raise PublicationNotFoundError()
        if (
            cls._item_key(item) != (f"JOB#{job_id}", "META")
            or cls._av_string(item, "entity_type") != "CONTROL_JOB"
        ):
            cls._invalid()
        try:
            job = ControlJobRecord.model_validate_json(cls._av_string(item, "payload"), strict=True)
        except Exception:
            cls._invalid()
        if job.job_id != job_id or item != _job_item(job):
            cls._invalid()
        return job

    @classmethod
    def _parse_job_record(
        cls,
        item: dict[str, Any] | None,
        *,
        job_id: str,
        sort_key: str,
        entity_type: str,
        model: type[BaseModel],
    ) -> BaseModel:
        if item is None:
            raise PublicationNotFoundError()
        if (
            cls._item_key(item) != (f"JOB#{job_id}", sort_key)
            or cls._av_string(item, "entity_type") != entity_type
        ):
            cls._invalid()
        try:
            record = model.model_validate_json(cls._av_string(item, "payload"), strict=True)
        except Exception:
            cls._invalid()
        expected_sort_key = cls._job_record_sort_key(record)
        if (
            expected_sort_key != sort_key
            or getattr(record, "job_id", job_id) != job_id
            or item != _job_record_item(job_id, sort_key, entity_type, record)
        ):
            cls._invalid()
        return record

    @staticmethod
    def _job_record_sort_key(record: BaseModel) -> str:
        if isinstance(record, ReviewContent):
            return f"REVIEW#{record.review_version:020d}"
        if isinstance(record, ReviewDecisionRecord):
            return f"DECISION#{record.decision_id}"
        if isinstance(record, SourceArtifactRecord):
            return "SOURCE"
        if isinstance(record, ProductSyncRecord):
            return f"PRODUCT_SYNC#{record.sync_id}"
        if isinstance(record, PricingSnapshot):
            return f"PRICING#{record.snapshot_id}"
        if isinstance(record, PricingEvidenceRecord):
            return f"PRICING_EVIDENCE#{record.snapshot_id}"
        raise ValueError("Publication approval guard authority is invalid")

    @staticmethod
    def _optional_record(
        records: Mapping[str, BaseModel],
        sort_key: str,
        model: type[BaseModel],
    ) -> BaseModel | None:
        record = records.get(sort_key)
        if record is not None and not isinstance(record, model):
            _ReadOnlyPublicationGuardStore._invalid()
        return record

    @staticmethod
    def _av_string(item: Mapping[str, Any], name: str) -> str:
        raw = item.get(name)
        if not isinstance(raw, dict) or set(raw) != {"S"} or not isinstance(raw.get("S"), str):
            _ReadOnlyPublicationGuardStore._invalid()
        return raw["S"]

    @staticmethod
    def _invalid() -> Any:
        raise ValueError("Publication approval guard authority is invalid")


class _RejectingGuard:
    def require_current(self, *, owner_id: str, aggregate_id: str) -> object:
        del owner_id, aggregate_id
        raise ValueError("unavailable")


class _PrivatePublicationGuardHandler:
    """Keep status dependency-free and construct the read graph only for a valid authority call."""

    __slots__ = ("_builder", "_configuration", "_delegate", "_lock", "_status")

    def __init__(
        self,
        *,
        configuration: Phase7GuardConfiguration,
        builder: Callable[[], PublicationGuardVerificationService],
    ) -> None:
        self._configuration = _validate_phase7_guard_configuration(configuration)
        self._builder = builder
        self._delegate: PublicationGuardVerificationService | None = None
        self._lock = Lock()
        self._status = PublicationGuardVerificationService(
            guard=_RejectingGuard(),
            activation=self._configuration.activation,
            guard_release_fingerprint=self._configuration.guard_release_fingerprint,
            profile_fingerprint=self._configuration.profile.exact.fingerprint,
        )

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        try:
            request = PublicationGuardRequest.model_validate(event)
        except Exception:
            result = self._status.handle(event)
        else:
            if request.operation.value == "status":
                result = self._status.status()
            else:
                result = self._get().handle(request.model_dump(mode="python"))
        return result.model_dump(mode="json")

    def _get(self) -> PublicationGuardVerificationService:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


def load_phase7_guard_configuration(
    environment: Mapping[str, object],
) -> Phase7GuardConfiguration:
    """Load the exact active-read tuple without granting any other Phase 7 capability."""

    try:
        region = _required(environment, "AWS_REGION")
        if _REGION.fullmatch(region) is None:
            raise ValueError
        state_table = _required(environment, "MR_LISTER_STATE_TABLE")
        table_match = _TABLE.fullmatch(state_table)
        if table_match is None:
            raise ValueError
        release_fingerprint = _required(
            environment,
            "MR_LISTER_PHASE7_GUARD_RELEASE_FINGERPRINT",
        )
        application_release_fingerprint = _required(
            environment,
            "MR_LISTER_RELEASE_FINGERPRINT",
        )
        if (
            _FINGERPRINT.fullmatch(release_fingerprint) is None
            or release_fingerprint == "0" * 64
            or _FINGERPRINT.fullmatch(application_release_fingerprint) is None
            or application_release_fingerprint == "0" * 64
        ):
            raise ValueError
        if (
            _required(environment, "MR_LISTER_PHASE7_SCAFFOLD_ONLY") != "false"
            or _required(environment, "MR_LISTER_PHASE7_GUARD_ENABLED") != "true"
            or _required(environment, "MR_LISTER_PHASE7_GUARD_MODE") != "approval_version_read_only"
            or _required(environment, "MR_LISTER_PHASE7_QUERY_ENABLED") != "false"
            or _required(environment, "MR_LISTER_PHASE7_REQUEST_ENABLED") != "false"
            or _required(environment, "MR_LISTER_PHASE7_PUBLICATION_ENABLED") != "false"
        ):
            raise ValueError
        profile = _profile_configuration(environment)
        if profile.exact.profile.publish_enabled is not False:
            raise ValueError
        eligibility = build_publication_profile_eligibility(
            profile_id=profile.exact.profile.profile_id,
            profile_version=profile.exact.profile.profile_version,
            profile_fingerprint=profile.exact.fingerprint,
            release_manifest_fingerprint=application_release_fingerprint,
            phase6_profile_publish_enabled=profile.exact.profile.publish_enabled,
        )
        configuration = Phase7GuardConfiguration(
            region=region,
            environment_name=table_match.group("environment"),
            state_table=state_table,
            guard_release_fingerprint=release_fingerprint,
            application_release_fingerprint=application_release_fingerprint,
            profile=profile,
            eligibility=eligibility,
            activation=PublicationGuardRuntimeActivation(
                scaffold_only=False,
                approval_guard_enabled=True,
                query_enabled=False,
                request_enabled=False,
                publication_enabled=False,
            ),
        )
        return _validate_phase7_guard_configuration(configuration)
    except Exception:
        raise Phase7GuardConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def compose_publication_guard_service(
    configuration: Phase7GuardConfiguration,
    *,
    client_factory: Phase7GuardAwsClientFactory,
) -> PublicationGuardVerificationService:
    """Construct the one strong-read store after deep configuration validation."""

    configuration = _validate_phase7_guard_configuration(configuration)
    raw_client = client_factory("dynamodb", region_name=configuration.region)
    client = _ReadOnlyDynamoClient(raw_client)
    store = _ReadOnlyPublicationGuardStore(
        client=client,
        table_name=configuration.state_table,
    )
    guard = DurablePublicationPreCallGuard(
        store=store,
        profiles=_PinnedGuardProfileAuthority(configuration.profile.exact),
        eligibility=PinnedPublicationProfileEligibilityAuthority(configuration.eligibility),
        release_manifest_fingerprint=configuration.application_release_fingerprint,
    )
    return PublicationGuardVerificationService(
        guard=guard,
        activation=configuration.activation,
        guard_release_fingerprint=configuration.guard_release_fingerprint,
        profile_fingerprint=configuration.profile.exact.fingerprint,
    )


def build_publication_guard_handler(
    environment: Mapping[str, object],
    *,
    client_factory: Phase7GuardAwsClientFactory | None = None,
) -> Phase7GuardHandler:
    configuration = load_phase7_guard_configuration(environment)
    factory = client_factory or default_aws_client_factory
    return _PrivatePublicationGuardHandler(
        configuration=configuration,
        builder=lambda: compose_publication_guard_service(
            configuration,
            client_factory=factory,
        ),
    )


def default_aws_client_factory(
    service_name: Phase7GuardAwsService,
    *,
    region_name: str,
) -> object:
    if service_name != "dynamodb":
        raise ValueError("Unsupported Phase 7 approval guard AWS client")
    import boto3

    return boto3.client("dynamodb", region_name=region_name)


def _load_exact_profile(
    path: Path,
    *,
    profile_id: str,
    profile_version: int,
) -> ExactReviewProductProfile:
    """Read one named profile file without directory-wide or workflow authority."""

    try:
        profile = ProductProfile.model_validate_json(path.read_text(encoding="utf-8"), strict=True)
        fingerprint = canonical_fingerprint(profile)
    except Exception:
        raise ValueError("Phase 7 approval guard profile is invalid") from None
    if profile.profile_id != profile_id or profile.profile_version != profile_version:
        raise ValueError("Phase 7 approval guard profile is invalid")
    return ExactReviewProductProfile(profile=profile, fingerprint=fingerprint)


def _profile_configuration(
    environment: Mapping[str, object],
) -> PinnedGuardProfileConfiguration:
    profile_id = _required(environment, "MR_LISTER_PRODUCT_PROFILE_ID")
    if _PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError
    version_text = _required(environment, "MR_LISTER_PRODUCT_PROFILE_VERSION")
    if re.fullmatch(r"[1-9][0-9]{0,5}", version_text) is None:
        raise ValueError
    profile_fingerprint = _required(environment, "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT")
    if _FINGERPRINT.fullmatch(profile_fingerprint) is None or profile_fingerprint == "0" * 64:
        raise ValueError
    path_text = _required(environment, "MR_LISTER_PRODUCT_PROFILE_PATH")
    if not path_text.isascii() or len(path_text) > 4_096 or "\\" in path_text:
        raise ValueError
    path = Path(path_text)
    if (
        not path.is_absolute()
        or path.as_posix() != path_text
        or path.name != f"{profile_id}.json"
        or path.resolve(strict=True) != path
        or not path.is_file()
        or not 1 <= path.stat().st_size <= 1024 * 1024
    ):
        raise ValueError
    exact = _load_exact_profile(
        path,
        profile_id=profile_id,
        profile_version=int(version_text),
    )
    if exact.fingerprint != profile_fingerprint:
        raise ValueError
    return PinnedGuardProfileConfiguration(path=path, exact=exact)


def _validate_phase7_guard_configuration(
    configuration: object,
) -> Phase7GuardConfiguration:
    try:
        if not isinstance(configuration, Phase7GuardConfiguration):
            raise ValueError
        table_match = _TABLE.fullmatch(configuration.state_table)
        if (
            _REGION.fullmatch(configuration.region) is None
            or table_match is None
            or table_match.group("environment") != configuration.environment_name
            or _FINGERPRINT.fullmatch(configuration.guard_release_fingerprint) is None
            or configuration.guard_release_fingerprint == "0" * 64
            or _FINGERPRINT.fullmatch(configuration.application_release_fingerprint) is None
            or configuration.application_release_fingerprint == "0" * 64
        ):
            raise ValueError
        profile = configuration.profile
        if (
            not isinstance(profile, PinnedGuardProfileConfiguration)
            or not profile.path.is_absolute()
            or profile.path.resolve(strict=True) != profile.path
            or profile.path.name != f"{profile.exact.profile.profile_id}.json"
            or profile.exact.profile.publish_enabled is not False
        ):
            raise ValueError
        reloaded = _load_exact_profile(
            profile.path,
            profile_id=profile.exact.profile.profile_id,
            profile_version=profile.exact.profile.profile_version,
        )
        if reloaded != profile.exact:
            raise ValueError
        eligibility = require_exact_publication_profile_eligibility(
            configuration.eligibility.model_dump(mode="python"),
            profile_id=reloaded.profile.profile_id,
            profile_version=reloaded.profile.profile_version,
            profile_fingerprint=reloaded.fingerprint,
            expected_sales_channel="etsy",
            release_manifest_fingerprint=configuration.application_release_fingerprint,
            phase6_profile_publish_enabled=reloaded.profile.publish_enabled,
        )
        activation = PublicationGuardRuntimeActivation.model_validate(
            configuration.activation.model_dump(mode="python")
        )
        return Phase7GuardConfiguration(
            region=configuration.region,
            environment_name=configuration.environment_name,
            state_table=configuration.state_table,
            guard_release_fingerprint=configuration.guard_release_fingerprint,
            application_release_fingerprint=configuration.application_release_fingerprint,
            profile=PinnedGuardProfileConfiguration(path=profile.path, exact=reloaded),
            eligibility=eligibility,
            activation=activation,
        )
    except Exception:
        raise Phase7GuardConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError
    return value


__all__ = [
    "Phase7GuardConfiguration",
    "Phase7GuardConfigurationError",
    "build_publication_guard_handler",
    "compose_publication_guard_service",
    "load_phase7_guard_configuration",
]
