#!/usr/bin/env python3
"""Capture the sanitized read-only baseline for the Phase 6.6 upload smoke.

The producer accepts one exact SHA-bound deployment authority before constructing its
read-only AWS backend.  Live reads are fixed to the non-root ``mr-lister-dev`` profile,
the Phase 6 development account/Region/stack, and the exact checked 5 MiB PNG canary.
It performs no Lambda invocation and exposes no DynamoDB, S3, workflow, provider, or
identity mutation surface.

The output preserves the historical exactly-two-job prerequisite because that cardinality
is frozen into the current edge/owner consumer.  It is one create-only mode-0600 JSON file
under the repository-private Phase 6.6 acceptance root and contains digests and counts only;
raw identities, job identifiers, object coordinates, VersionIds, and workflow ARNs are never
serialized.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import secrets
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import ValidationError

from mr_lister.control.models import ControlJobRecord, SourceArtifactRecord
from mr_lister.control.source_artwork import (
    SourceArtifactAuthorityError,
    validate_source_artifact_authority,
)
from tools import capture_phase66_deployment_authority as deployment_capture
from tools import phase66_deployed_edge_auth_owner_observation as consumer
from tools import phase66_deployed_upload_integrity_smoke as smoke
from tools.phase66_live_acceptance import Phase66LiveAcceptanceError, exact_phase66_canary_png
from tools.prepare_phase66_edge_revalidation import _DeploymentAuthorityDocument

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"

PROFILE: Final = "mr-lister-dev"
ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
STACK_NAME: Final = "mr-lister-phase6-dev"
EXPECTED_CALLER_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:user/{PROFILE}"
EXPECTED_TABLE_NAME: Final = STACK_NAME
EXPECTED_BUCKET_NAME: Final = f"mr-lister-phase6-artifacts-dev-{ACCOUNT_ID}-{REGION}"
PRIMARY_SIZE: Final = smoke.PRIMARY_SIZE
PRIMARY_SHA256: Final = smoke.PRIMARY_SHA256
BASELINE_FORMAT: Final = consumer.BASELINE_FORMAT
MAX_INPUT_BYTES: Final = 4 * 1024 * 1024
MAX_SCAN_PAGES: Final = 100
MAX_SCAN_RECORDS: Final = 100_000
MAX_INVENTORY_PAGES: Final = 100
MAX_INVENTORY_RECORDS: Final = 10_000
MAX_WORKFLOW_PAGES: Final = 100

_WORKFLOW_OUTPUTS: Final = {
    "PrepareStateMachineArn": "mr-lister-phase6-dev-prepare",
    "ReconcileProductStateMachineArn": "mr-lister-phase6-dev-reconcile-product",
    "RefreshEconomicsStateMachineArn": "mr-lister-phase6-dev-refresh-economics",
    "SynchronizeProductStateMachineArn": "mr-lister-phase6-dev-synchronize-product",
}


class UploadIntegrityPreflightError(RuntimeError):
    """One authority, read-only observation, confinement, or output check failed."""


class AwsClientProvider(Protocol):
    """Minimal injected AWS client factory used by the read-only backend."""

    def client(self, service_name: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    kind: Literal["version", "delete_marker"]
    version_id: str = field(repr=False)
    is_latest: bool
    last_modified: str
    size_bytes: int | None = None
    etag: str | None = field(default=None, repr=False)

    def sanitized(self) -> dict[str, object]:
        value: dict[str, object] = {
            "is_latest": self.is_latest,
            "kind": self.kind,
            "last_modified": self.last_modified,
            "version_digest": smoke._digest_text(self.version_id),
        }
        if self.kind == "version":
            if self.size_bytes is None or self.etag is None:
                raise UploadIntegrityPreflightError("source-version inventory is invalid")
            value["etag_digest"] = smoke._digest_text(self.etag)
            value["size_bytes"] = self.size_bytes
        elif self.size_bytes is not None or self.etag is not None:
            raise UploadIntegrityPreflightError("delete-marker inventory is invalid")
        return value


@dataclass(frozen=True, slots=True)
class BaselineSnapshot:
    table_name: str = field(repr=False)
    artifact_bucket: str = field(repr=False)
    state_machine_arns: tuple[str, ...] = field(repr=False)
    items: tuple[tuple[str, Mapping[str, Any]], ...] = field(repr=False)
    table_record_count: int
    table_scanned_count: int
    inventory: tuple[InventoryEntry, ...] = field(repr=False)
    head_matches: Mapping[str, bool]
    pinned_tag_matches: bool
    bucket_versioning_enabled: bool
    running_execution_count: int


class BaselineBackend(Protocol):
    """The only live boundary used by the producer."""

    def capture_baseline(
        self,
        deployment: _DeploymentAuthorityDocument,
        canary: bytes,
    ) -> BaselineSnapshot: ...


def _private_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(PRIVATE_ROOT)
    except ValueError:
        raise UploadIntegrityPreflightError(
            "preflight authorities must stay in the repository-private acceptance root"
        ) from None
    if not relative.parts:
        raise UploadIntegrityPreflightError("preflight authority path must name one private child")
    return candidate


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_exact_deployment(path: Path, expected_sha256: str) -> _DeploymentAuthorityDocument:
    if not _is_digest(expected_sha256):
        raise UploadIntegrityPreflightError("deployment-authority SHA-256 is invalid")
    candidate = _private_path(path)
    try:
        payload = smoke._read_private_file(candidate, max_bytes=MAX_INPUT_BYTES)
    except smoke.SmokeError:
        raise UploadIntegrityPreflightError(
            "deployment authority must be one stable mode-0600 private file"
        ) from None
    if not secrets.compare_digest(smoke._digest_bytes(payload), expected_sha256):
        raise UploadIntegrityPreflightError(
            "deployment authority changed or does not match its SHA-256"
        )
    try:
        value = smoke._strict_json(payload, "deployment authority")
        deployment = _DeploymentAuthorityDocument.model_validate(value)
    except (ValidationError, ValueError, smoke.SmokeError):
        raise UploadIntegrityPreflightError(
            "deployment authority does not match the exact Phase 6 contract"
        ) from None
    if (
        deployment.authority.account_binding_digest
        != hashlib.sha256(ACCOUNT_ID.encode("ascii")).hexdigest()
    ):
        raise UploadIntegrityPreflightError("deployment authority does not bind the fixed account")
    return deployment


def _exact_canary() -> bytes:
    try:
        canary = exact_phase66_canary_png()
    except Phase66LiveAcceptanceError:
        raise UploadIntegrityPreflightError("exact Phase 6 canary is unavailable") from None
    if len(canary) != PRIMARY_SIZE or smoke._digest_bytes(canary) != PRIMARY_SHA256:
        raise UploadIntegrityPreflightError("exact Phase 6 canary bytes drifted")
    return canary


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise UploadIntegrityPreflightError(f"{label} is invalid")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise UploadIntegrityPreflightError(f"{label} is invalid")
    return value


def _string(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise UploadIntegrityPreflightError(f"{label} is invalid")
    return value


def _exact_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise UploadIntegrityPreflightError(f"{label} is invalid")
    return value


def _attribute_string(item: Mapping[str, Any], name: str) -> str:
    attribute = _mapping(item.get(name), "DynamoDB attribute")
    if set(attribute) != {"S"}:
        raise UploadIntegrityPreflightError("DynamoDB item is not the closed string envelope")
    return _string(attribute.get("S"), "DynamoDB string attribute")


def _parse_payload(payload: str) -> Mapping[str, Any]:
    try:
        return smoke._mapping(
            smoke._strict_json(payload.encode("utf-8"), "DynamoDB payload"),
            "DynamoDB payload",
        )
    except smoke.SmokeError:
        raise UploadIntegrityPreflightError("DynamoDB payload is not strict JSON") from None


def _selected_records(
    items: Sequence[tuple[str, Mapping[str, Any]]],
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
    Mapping[str, Any],
    Mapping[str, Any],
]:
    raw_jobs = [payload for entity, payload in items if entity == "CONTROL_JOB"]
    raw_sources = [payload for entity, payload in items if entity == "SOURCE_ARTIFACT"]
    if len(raw_jobs) != 2 or len(raw_sources) != 2:
        raise UploadIntegrityPreflightError(
            "the frozen baseline requires exactly two jobs and two source records"
        )

    jobs: dict[str, Mapping[str, Any]] = {}
    job_models: dict[str, ControlJobRecord] = {}
    sources: dict[str, Mapping[str, Any]] = {}
    source_models: dict[str, SourceArtifactRecord] = {}
    try:
        for payload in raw_jobs:
            model = ControlJobRecord.model_validate(payload)
            if model.job_id in jobs:
                raise ValueError
            jobs[model.job_id] = payload
            job_models[model.job_id] = model
        for payload in raw_sources:
            model = validate_source_artifact_authority(SourceArtifactRecord.model_validate(payload))
            if model.job_id in sources:
                raise ValueError
            sources[model.job_id] = payload
            source_models[model.job_id] = model
    except (SourceArtifactAuthorityError, ValidationError, ValueError):
        raise UploadIntegrityPreflightError("job/source authority records are invalid") from None

    if set(jobs) != set(sources):
        raise UploadIntegrityPreflightError("job/source authority sets do not match exactly")
    owners = {model.owner_id for model in job_models.values()}
    if len(owners) != 1:
        raise UploadIntegrityPreflightError("the two baseline jobs do not share one owner")
    for job_id, job in job_models.items():
        source = source_models[job_id]
        if (
            source.owner_id != job.owner_id
            or source.job_id != job.job_id
            or job.source_artifact_fingerprint != source.fingerprint
        ):
            raise UploadIntegrityPreflightError("job/source authority binding is inconsistent")

    ordered = sorted(job_models.values(), key=lambda value: value.updated_at, reverse=True)
    if ordered[0].updated_at == ordered[1].updated_at:
        raise UploadIntegrityPreflightError("latest baseline job selection is ambiguous")
    selected_id = ordered[0].job_id
    return jobs, sources, jobs[selected_id], sources[selected_id]


def _inventory_digest(inventory: Sequence[InventoryEntry]) -> str:
    sanitized = sorted(
        (entry.sanitized() for entry in inventory),
        key=lambda value: (str(value["last_modified"]), str(value["version_digest"])),
    )
    return smoke._digest_json(sanitized)


def _baseline_document(snapshot: BaselineSnapshot, canary: bytes) -> Mapping[str, Any]:
    if (
        snapshot.table_name != EXPECTED_TABLE_NAME
        or snapshot.artifact_bucket != EXPECTED_BUCKET_NAME
        or snapshot.table_record_count != len(snapshot.items)
        or snapshot.table_scanned_count != len(snapshot.items)
    ):
        raise UploadIntegrityPreflightError("table/bucket baseline authority is inconsistent")
    expected_workflows = tuple(
        f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:{name}"
        for name in _WORKFLOW_OUTPUTS.values()
    )
    if set(snapshot.state_machine_arns) != set(expected_workflows) or len(
        snapshot.state_machine_arns
    ) != len(expected_workflows):
        raise UploadIntegrityPreflightError("workflow baseline authority is inconsistent")
    if len(canary) != PRIMARY_SIZE or smoke._digest_bytes(canary) != PRIMARY_SHA256:
        raise UploadIntegrityPreflightError("baseline canary authority is inconsistent")

    jobs, sources, selected_job, selected_source = _selected_records(snapshot.items)
    selected_job_id = _string(selected_job.get("job_id"), "selected job authority")
    selected_owner_id = _string(selected_job.get("owner_id"), "selected owner authority")
    selected_bucket = _string(selected_source.get("bucket"), "selected bucket authority")
    selected_key = _string(selected_source.get("object_key"), "selected key authority")
    selected_version = _string(selected_source.get("version_id"), "selected version authority")
    if (
        selected_bucket != snapshot.artifact_bucket
        or any(source.get("bucket") != snapshot.artifact_bucket for source in sources.values())
        or selected_source.get("content_sha256") != PRIMARY_SHA256
        or selected_source.get("size_bytes") != PRIMARY_SIZE
        or selected_source.get("media_type") != "image/png"
    ):
        raise UploadIntegrityPreflightError("selected source is not the exact checked canary")

    counts = Counter(entity for entity, _payload in snapshot.items)
    provider_count = sum(
        count for entity, count in counts.items() if entity.startswith("PROVIDER_")
    )
    if provider_count != 0 or snapshot.running_execution_count != 0:
        raise UploadIntegrityPreflightError(
            "baseline must have zero provider records and zero running workflows"
        )
    if not snapshot.inventory or len(snapshot.inventory) > MAX_INVENTORY_RECORDS:
        raise UploadIntegrityPreflightError("selected source inventory is not bounded")
    if any(entry.kind == "delete_marker" for entry in snapshot.inventory):
        raise UploadIntegrityPreflightError("selected source inventory contains a delete marker")
    identities = [(entry.kind, entry.version_id) for entry in snapshot.inventory]
    if len(set(identities)) != len(identities):
        raise UploadIntegrityPreflightError("selected source inventory contains duplicates")
    selected_entries = [
        entry
        for entry in snapshot.inventory
        if entry.kind == "version" and entry.version_id == selected_version
    ]
    if len(selected_entries) != 1:
        raise UploadIntegrityPreflightError("pinned VersionId is not unique in the inventory")
    if selected_entries[0].size_bytes != PRIMARY_SIZE:
        raise UploadIntegrityPreflightError("pinned inventory entry does not match the canary size")
    if sum(entry.is_latest for entry in snapshot.inventory) != 1:
        raise UploadIntegrityPreflightError("selected source inventory has no unique latest entry")

    expected_head_fields = {"checksum", "content_type", "encryption", "size", "version"}
    if set(snapshot.head_matches) != expected_head_fields or any(
        snapshot.head_matches.get(name) is not True for name in expected_head_fields
    ):
        raise UploadIntegrityPreflightError("pinned VersionId head does not match the canary")
    if (
        snapshot.pinned_tag_matches is not True
        or snapshot.bucket_versioning_enabled is not True
        or selected_entries[0].is_latest is not True
    ):
        raise UploadIntegrityPreflightError("pinned VersionId/tag/versioning proof failed")

    job_digests = sorted(smoke._digest_text(job_id) for job_id in jobs)
    source_authority = {
        key: selected_source[key]
        for key in ("bucket", "object_key", "version_id", "content_sha256", "fingerprint")
    }
    document: Mapping[str, Any] = {
        "actor_digest": smoke._digest_text(selected_owner_id),
        "baseline_contract": BASELINE_FORMAT,
        "bucket_versioning_enabled": snapshot.bucket_versioning_enabled,
        "canary_byte_count": len(canary),
        "canary_sha256": smoke._digest_bytes(canary),
        "entity_type_counts": dict(sorted(counts.items())),
        "existing_job_count": len(jobs),
        "existing_job_digests": job_digests,
        "existing_job_set_digest": smoke._digest_json(job_digests),
        "existing_job_states": sorted(
            _string(job.get("state"), "job state") for job in jobs.values()
        ),
        "provider_record_count": provider_count,
        "running_execution_count": snapshot.running_execution_count,
        "selected_content_sha256": selected_source["content_sha256"],
        "selected_inventory_count": len(snapshot.inventory),
        "selected_inventory_digest": _inventory_digest(snapshot.inventory),
        "selected_job_digest": smoke._digest_text(selected_job_id),
        "selected_job_record_digest": smoke._digest_json(selected_job),
        "selected_object_coordinate_digest": smoke._digest_text(
            selected_bucket + "\0" + selected_key
        ),
        "selected_pinned_head_matches": dict(snapshot.head_matches),
        "selected_pinned_is_latest": selected_entries[0].is_latest,
        "selected_pinned_tag_matches": snapshot.pinned_tag_matches,
        "selected_pinned_version_digest": smoke._digest_text(selected_version),
        "selected_source_authority_digest": smoke._digest_json(source_authority),
        "selected_source_record_digest": smoke._digest_json(selected_source),
        "table_record_count": snapshot.table_record_count,
        "table_scanned_count": snapshot.table_scanned_count,
    }
    try:
        return consumer._validate_baseline_document(document)
    except consumer.Phase66EdgeObservationError:
        raise UploadIntegrityPreflightError(
            "captured baseline was rejected by the current exact consumer"
        ) from None


def _sensitive_values(snapshot: BaselineSnapshot) -> set[str]:
    values = {
        snapshot.table_name,
        snapshot.artifact_bucket,
        *snapshot.state_machine_arns,
        *(entry.version_id for entry in snapshot.inventory),
        *(entry.etag for entry in snapshot.inventory if entry.etag is not None),
    }
    for _entity, payload in snapshot.items:
        for name, value in payload.items():
            if (
                isinstance(value, str)
                and len(value) >= 8
                and (
                    name == "owner_id"
                    or name.endswith("_id")
                    or name.endswith("_fingerprint")
                    or name in {"bucket", "object_key", "version_id", "fingerprint"}
                )
            ):
                values.add(value)
    return values


def _verify_sanitized(document: Mapping[str, Any], snapshot: BaselineSnapshot) -> None:
    payload = smoke._canonical_json(document, pretty=True)
    if any(value.encode("utf-8") in payload for value in _sensitive_values(snapshot)):
        raise UploadIntegrityPreflightError("sanitized baseline retained raw deployed authority")


def _write_once(path: Path, document: Mapping[str, Any]) -> tuple[int, str]:
    candidate = _private_path(path)
    try:
        with smoke._private_directory_descriptor(candidate.parent, create=True) as descriptor:
            return smoke._write_once_private_json(descriptor, candidate.name, document)
    except smoke.SmokeError:
        raise UploadIntegrityPreflightError(
            "preflight output must be one fresh mode-0600 private file"
        ) from None


class AwsReadOnlyBackend:
    """Injected boto3-compatible implementation containing read operations only."""

    def __init__(self, aws_clients: AwsClientProvider):
        self._provider = aws_clients
        self._cloudformation = aws_clients.client("cloudformation")
        self._dynamodb = aws_clients.client("dynamodb")
        self._s3 = aws_clients.client("s3")
        self._sfn = aws_clients.client("stepfunctions")
        self._sts = aws_clients.client("sts")
        if any(
            client is None
            for client in (
                self._cloudformation,
                self._dynamodb,
                self._s3,
                self._sfn,
                self._sts,
            )
        ):
            raise UploadIntegrityPreflightError("read-only AWS client set is unavailable")

    def _deployment_bindings(self, deployment: _DeploymentAuthorityDocument) -> Mapping[str, str]:
        try:
            live_stack, bindings = deployment_capture._stack_capture(self._provider, STACK_NAME)
            live_lambdas = deployment_capture._lambda_capture(self._provider, bindings)
        except Exception:
            raise UploadIntegrityPreflightError(
                "live stack/Lambda state does not match the deployment authority"
            ) from None
        expected_stack = deployment.authority.stack.model_dump(mode="json")
        expected_lambdas = sorted(
            (value.model_dump(mode="json") for value in deployment.authority.lambdas),
            key=lambda value: str(value["logical_id"]),
        )
        if (
            live_stack != expected_stack
            or sorted(live_lambdas, key=lambda value: str(value["logical_id"])) != expected_lambdas
        ):
            raise UploadIntegrityPreflightError(
                "live stack/Lambda state does not match the deployment authority"
            )
        return bindings

    def _scan(self, table_name: str) -> tuple[tuple[tuple[str, Mapping[str, Any]], ...], int, int]:
        items: list[tuple[str, Mapping[str, Any]]] = []
        record_count = 0
        scanned_count = 0
        request: dict[str, Any] = {
            "ConsistentRead": True,
            "ExpressionAttributeNames": {"#e": "entity_type"},
            "Limit": 1_000,
            "ProjectionExpression": "#e,payload",
            "TableName": table_name,
        }
        observed_tokens: set[str] = set()
        for _ in range(MAX_SCAN_PAGES):
            response = _mapping(self._dynamodb.scan(**request), "DynamoDB scan page")
            raw_items = _sequence(response.get("Items", []), "DynamoDB scan items")
            page_count = _exact_int(response.get("Count"), "DynamoDB page count")
            page_scanned = _exact_int(response.get("ScannedCount"), "DynamoDB scanned count")
            if page_count != len(raw_items) or page_scanned != len(raw_items):
                raise UploadIntegrityPreflightError("DynamoDB scan page is not complete")
            for raw_item in raw_items:
                item = _mapping(raw_item, "DynamoDB item")
                entity = _attribute_string(item, "entity_type")
                payload = _parse_payload(_attribute_string(item, "payload"))
                items.append((entity, payload))
            record_count += page_count
            scanned_count += page_scanned
            if len(items) > MAX_SCAN_RECORDS:
                raise UploadIntegrityPreflightError("DynamoDB scan exceeded its record bound")
            token = response.get("LastEvaluatedKey")
            if token in (None, {}):
                return tuple(items), record_count, scanned_count
            token = _mapping(token, "DynamoDB scan pagination key")
            try:
                token_digest = smoke._digest_json(token)
            except smoke.SmokeError:
                raise UploadIntegrityPreflightError(
                    "DynamoDB scan pagination key is invalid"
                ) from None
            if token_digest in observed_tokens:
                raise UploadIntegrityPreflightError("DynamoDB scan pagination repeated")
            observed_tokens.add(token_digest)
            request["ExclusiveStartKey"] = dict(token)
        raise UploadIntegrityPreflightError("DynamoDB scan exceeded its page bound")

    @staticmethod
    def _inventory_timestamp(value: object) -> str:
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise UploadIntegrityPreflightError("S3 inventory timestamp is invalid")
        return value.astimezone(UTC).isoformat()

    def _inventory(self, bucket: str, key: str) -> tuple[InventoryEntry, ...]:
        values: list[InventoryEntry] = []
        request: dict[str, Any] = {
            "Bucket": bucket,
            "ExpectedBucketOwner": ACCOUNT_ID,
            "MaxKeys": 1_000,
            "Prefix": key,
        }
        observed_tokens: set[tuple[str, str]] = set()
        for _ in range(MAX_INVENTORY_PAGES):
            response = _mapping(
                self._s3.list_object_versions(**request), "S3 version inventory page"
            )
            for kind, field_name in (
                ("version", "Versions"),
                ("delete_marker", "DeleteMarkers"),
            ):
                for raw_record in _sequence(
                    response.get(field_name, []), "S3 version inventory records"
                ):
                    record = _mapping(raw_record, "S3 version inventory record")
                    if record.get("Key") != key or type(record.get("IsLatest")) is not bool:
                        raise UploadIntegrityPreflightError(
                            "S3 exact-key inventory contains an invalid record"
                        )
                    version_id = _string(record.get("VersionId"), "S3 VersionId")
                    timestamp = self._inventory_timestamp(record.get("LastModified"))
                    if kind == "version":
                        size = _exact_int(record.get("Size"), "S3 version size")
                        etag = _string(record.get("ETag"), "S3 version ETag")
                    else:
                        size = None
                        etag = None
                    values.append(
                        InventoryEntry(
                            kind=kind,
                            version_id=version_id,
                            is_latest=record["IsLatest"],
                            last_modified=timestamp,
                            size_bytes=size,
                            etag=etag,
                        )
                    )
            if len(values) > MAX_INVENTORY_RECORDS:
                raise UploadIntegrityPreflightError("S3 inventory exceeded its record bound")
            truncated = response.get("IsTruncated", False)
            if type(truncated) is not bool:
                raise UploadIntegrityPreflightError("S3 inventory truncation state is invalid")
            if not truncated:
                return tuple(values)
            next_key = _string(response.get("NextKeyMarker"), "S3 next key marker")
            next_version = _string(response.get("NextVersionIdMarker"), "S3 next VersionId marker")
            token = (next_key, next_version)
            if token in observed_tokens:
                raise UploadIntegrityPreflightError("S3 inventory pagination repeated")
            observed_tokens.add(token)
            request["KeyMarker"] = next_key
            request["VersionIdMarker"] = next_version
        raise UploadIntegrityPreflightError("S3 inventory exceeded its page bound")

    def _running_executions(self, arns: Sequence[str]) -> int:
        running = 0
        for arn in arns:
            request: dict[str, Any] = {
                "maxResults": 100,
                "stateMachineArn": arn,
                "statusFilter": "RUNNING",
            }
            observed_tokens: set[str] = set()
            for _ in range(MAX_WORKFLOW_PAGES):
                response = _mapping(self._sfn.list_executions(**request), "workflow execution page")
                executions = _sequence(response.get("executions", []), "workflow executions")
                for execution in executions:
                    _mapping(execution, "workflow execution")
                running += len(executions)
                token = response.get("nextToken")
                if token is None:
                    break
                token = _string(token, "workflow pagination token")
                if token in observed_tokens:
                    raise UploadIntegrityPreflightError("workflow pagination repeated")
                observed_tokens.add(token)
                request["nextToken"] = token
            else:
                raise UploadIntegrityPreflightError("workflow inventory exceeded its page bound")
        return running

    def capture_baseline(
        self,
        deployment: _DeploymentAuthorityDocument,
        canary: bytes,
    ) -> BaselineSnapshot:
        identity = _mapping(self._sts.get_caller_identity(), "AWS caller identity")
        if identity.get("Account") != ACCOUNT_ID or identity.get("Arn") != EXPECTED_CALLER_ARN:
            raise UploadIntegrityPreflightError(
                "AWS caller is not the fixed non-root mr-lister-dev authority"
            )
        if len(canary) != PRIMARY_SIZE or smoke._digest_bytes(canary) != PRIMARY_SHA256:
            raise UploadIntegrityPreflightError("AWS capture canary authority is invalid")

        bindings = self._deployment_bindings(deployment)
        table_name = bindings.get("StateTableName")
        artifact_bucket = bindings.get("ArtifactBucketName")
        readiness = bindings.get("DeploymentReadiness")
        if (
            table_name != EXPECTED_TABLE_NAME
            or artifact_bucket != EXPECTED_BUCKET_NAME
            or readiness != "WEB_EDGE_ACTIVE_DRAFT_ONLY"
        ):
            raise UploadIntegrityPreflightError("Phase 6 stack outputs drifted")
        state_machine_arns = tuple(
            _string(bindings.get(output), "workflow output") for output in sorted(_WORKFLOW_OUTPUTS)
        )
        for output, name in _WORKFLOW_OUTPUTS.items():
            expected = f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:{name}"
            if bindings.get(output) != expected:
                raise UploadIntegrityPreflightError("Phase 6 workflow output drifted")

        items, record_count, scanned_count = self._scan(table_name)
        _jobs, _sources, _selected_job, selected_source = _selected_records(items)
        bucket = _string(selected_source.get("bucket"), "selected source bucket")
        key = _string(selected_source.get("object_key"), "selected source key")
        version_id = _string(selected_source.get("version_id"), "selected source VersionId")
        if bucket != artifact_bucket:
            raise UploadIntegrityPreflightError("selected source bucket drifted")

        inventory = self._inventory(bucket, key)
        checksum = base64.b64encode(hashlib.sha256(canary).digest()).decode("ascii")
        head = _mapping(
            self._s3.head_object(
                Bucket=bucket,
                ChecksumMode="ENABLED",
                ExpectedBucketOwner=ACCOUNT_ID,
                Key=key,
                VersionId=version_id,
            ),
            "S3 pinned head",
        )
        tags = _mapping(
            self._s3.get_object_tagging(
                Bucket=bucket,
                ExpectedBucketOwner=ACCOUNT_ID,
                Key=key,
                VersionId=version_id,
            ),
            "S3 pinned tags",
        )
        versioning = _mapping(
            self._s3.get_bucket_versioning(
                Bucket=bucket,
                ExpectedBucketOwner=ACCOUNT_ID,
            ),
            "S3 bucket versioning",
        )
        head_matches = {
            "checksum": head.get("ChecksumSHA256") == checksum,
            "content_type": head.get("ContentType") == "image/png",
            "encryption": head.get("ServerSideEncryption") == "AES256",
            "size": head.get("ContentLength") == len(canary),
            "version": head.get("VersionId") == version_id,
        }
        return BaselineSnapshot(
            table_name=table_name,
            artifact_bucket=artifact_bucket,
            state_machine_arns=state_machine_arns,
            items=items,
            table_record_count=record_count,
            table_scanned_count=scanned_count,
            inventory=inventory,
            head_matches=head_matches,
            pinned_tag_matches=tags.get("TagSet")
            == [{"Key": "mr-lister-state", "Value": "pinned"}],
            bucket_versioning_enabled=versioning.get("Status") == "Enabled",
            running_execution_count=self._running_executions(state_machine_arns),
        )


class _Boto3Provider:
    """Construct only fixed-profile, fixed-Region read clients for the CLI."""

    _SERVICES: Final = frozenset(
        {"cloudformation", "dynamodb", "lambda", "s3", "stepfunctions", "sts"}
    )

    def __init__(self) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise UploadIntegrityPreflightError("boto3 is unavailable") from None
        self._session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        self._config = Config(retries={"mode": "standard", "total_max_attempts": 1})

    def client(self, service_name: str) -> Any:
        if service_name not in self._SERVICES:
            raise UploadIntegrityPreflightError("AWS service is outside the read-only boundary")
        return self._session.client(service_name, config=self._config)


def capture_phase66_upload_integrity_preflight(
    *,
    deployment_authority_path: Path,
    deployment_authority_sha256: str,
    output_path: Path,
    backend_factory: Callable[[], BaselineBackend],
    canary_factory: Callable[[], bytes] = _exact_canary,
) -> Mapping[str, object]:
    """Validate authority, capture read-only state, and create one consumer-valid baseline."""

    deployment_path = _private_path(deployment_authority_path)
    output = _private_path(output_path)
    if deployment_path == output:
        raise UploadIntegrityPreflightError("preflight input and output must be distinct")
    deployment = _read_exact_deployment(deployment_path, deployment_authority_sha256)
    canary = canary_factory()
    if (
        not isinstance(canary, bytes)
        or len(canary) != PRIMARY_SIZE
        or smoke._digest_bytes(canary) != PRIMARY_SHA256
    ):
        raise UploadIntegrityPreflightError("canary factory did not return the exact authority")

    # All local authority and canary checks complete before constructing the AWS backend.
    try:
        snapshot = backend_factory().capture_baseline(deployment, canary)
    except UploadIntegrityPreflightError:
        raise
    except Exception:
        raise UploadIntegrityPreflightError("read-only AWS capture failed closed") from None
    document = _baseline_document(snapshot, canary)
    _verify_sanitized(document, snapshot)
    byte_count, output_sha256 = _write_once(output, document)
    return {
        "baseline_sha256": output_sha256,
        "byte_count": byte_count,
        "deployment_digest": deployment.deployment_digest,
        "result": "passed",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-authority", required=True, type=Path)
    parser.add_argument("--deployment-authority-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend_factory: Callable[[], BaselineBackend] | None = None,
    canary_factory: Callable[[], bytes] = _exact_canary,
) -> int:
    arguments = _parser().parse_args(argv)
    factory = (
        (lambda: AwsReadOnlyBackend(_Boto3Provider()))
        if backend_factory is None
        else backend_factory
    )
    result = capture_phase66_upload_integrity_preflight(
        deployment_authority_path=arguments.deployment_authority,
        deployment_authority_sha256=arguments.deployment_authority_sha256,
        output_path=arguments.output,
        backend_factory=factory,
        canary_factory=canary_factory,
    )
    print(smoke._canonical_json(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UploadIntegrityPreflightError as error:
        raise SystemExit(f"phase66 upload-integrity preflight stopped: {error}") from None
