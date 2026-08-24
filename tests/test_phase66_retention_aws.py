from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from hashlib import sha256
from typing import Any

import pytest

from mr_lister.control.fingerprints import (
    canonical_fingerprint,
    publication_terminal_summary_fingerprint,
)
from mr_lister.control.models import ControlJobRecord, ControlJobState, SourceArtifactRecord
from mr_lister.control.publication_retention import (
    PublicationRetentionCompletionAuthority,
    publication_operational_expiry_epoch,
    publication_retention_completion_fingerprint,
)
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.production.retention import (
    PHASE6_SOURCE_PREFIX,
    ReferenceAwareSourceVersionSweeper,
    RetentionBoundaryInvalidError,
    RetentionCheckpoint,
    RetentionDependencyUnavailableError,
)
from mr_lister.production.retention_aws import (
    RETENTION_CHECKPOINT_PARTITION_KEY,
    RETENTION_CHECKPOINT_SORT_KEY,
    DynamoDBRetentionCheckpointStore,
    DynamoDBStrongSourceAuthorityReader,
    S3SourceVersionInventory,
    S3SourceVersionTagStore,
)

NOW = datetime(2026, 8, 23, 5, 0, tzinfo=UTC)
BUCKET = "mr-lister-phase6-artifacts-dev"
BUCKET_OWNER = "123456789012"
TABLE = "MrListerPhase6Control"
OWNER_ID = "a" * 64
JOB_ID = "job_phase66_retention_aws"
OBJECT_KEY = f"private/owners/{OWNER_ID}/jobs/{JOB_ID}/source/source.png"
VERSION_ID = "source-version-exact-1"
NEXT_VERSION_ID = "source-version-next-2"
PUBLICATION_AGGREGATE_ID = "publication_retention_aggregate"
PUBLICATION_REPORT_ID = "publication_retention_report"
PUBLICATION_RESULT_ID = "publication_retention_result"


class RecordingS3InventoryClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)  # type: ignore[return-value]


class RecordingS3TaggingClient:
    def __init__(self, get_response: object | None = None) -> None:
        self.get_response = (
            {"TagSet": [{"Key": "mr-lister-state", "Value": "pinned"}]}
            if get_response is None
            else get_response
        )
        self.get_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.get_error: Exception | None = None
        self.put_error: Exception | None = None
        self.put_response: object = {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object_tagging(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(copy.deepcopy(kwargs))
        if self.get_error is not None:
            raise self.get_error
        return copy.deepcopy(self.get_response)  # type: ignore[return-value]

    def put_object_tagging(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(copy.deepcopy(kwargs))
        if self.put_error is not None:
            raise self.put_error
        return copy.deepcopy(self.put_response)  # type: ignore[return-value]


class RecordingDynamoClient:
    def __init__(self) -> None:
        self.transact_response: object = {"Responses": [{}, {}, {}]}
        self.transact_responses: list[object] | None = None
        self.get_response: object = {}
        self.transact_error: Exception | None = None
        self.get_error: Exception | None = None
        self.put_error: Exception | None = None
        self.put_response: object = {"ResponseMetadata": {"HTTPStatusCode": 200}}
        self.transact_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []

    def transact_get_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transact_calls.append(copy.deepcopy(kwargs))
        if self.transact_error is not None:
            raise self.transact_error
        response = (
            self.transact_responses.pop(0)
            if self.transact_responses is not None
            else self.transact_response
        )
        return copy.deepcopy(response)  # type: ignore[return-value]

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(copy.deepcopy(kwargs))
        if self.get_error is not None:
            raise self.get_error
        return copy.deepcopy(self.get_response)  # type: ignore[return-value]

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(copy.deepcopy(kwargs))
        if self.put_error is not None:
            raise self.put_error
        self.get_response = {"Item": copy.deepcopy(kwargs["Item"])}
        return copy.deepcopy(self.put_response)  # type: ignore[return-value]


def _http_date() -> str:
    return format_datetime(NOW, usegmt=True)


def _inventory_page(
    *,
    versions: list[dict[str, Any]] | None = None,
    delete_markers: list[dict[str, Any]] | None = None,
    truncated: bool = False,
    next_key: object | None = None,
    next_version: object | None = None,
    max_keys: int = 1,
    key_marker: object | None = None,
    version_marker: object | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "Name": BUCKET,
        "Prefix": PHASE6_SOURCE_PREFIX,
        "MaxKeys": max_keys,
        "IsTruncated": truncated,
        "Versions": versions or [],
        "DeleteMarkers": delete_markers or [],
        "ResponseMetadata": {"HTTPHeaders": {"date": _http_date()}},
    }
    if key_marker is not None:
        response["KeyMarker"] = key_marker
    if version_marker is not None:
        response["VersionIdMarker"] = version_marker
    if next_key is not None:
        response["NextKeyMarker"] = next_key
    if next_version is not None:
        response["NextVersionIdMarker"] = next_version
    return response


def _version(
    *,
    object_key: object = OBJECT_KEY,
    version_id: object = VERSION_ID,
    last_modified: object = NOW - timedelta(days=3),
) -> dict[str, Any]:
    return {
        "Key": object_key,
        "VersionId": version_id,
        "LastModified": last_modified,
        "IsLatest": True,
        "Size": 128,
    }


def _delete_marker(
    *,
    object_key: object = OBJECT_KEY,
    version_id: object = "delete-marker-1",
) -> dict[str, Any]:
    return {"Key": object_key, "VersionId": version_id, "IsLatest": False}


def _inventory(client: RecordingS3InventoryClient) -> S3SourceVersionInventory:
    return S3SourceVersionInventory(
        client=client,
        artifact_bucket=BUCKET,
        bucket_owner_account_id=BUCKET_OWNER,
    )


def _tags(client: RecordingS3TaggingClient) -> S3SourceVersionTagStore:
    return S3SourceVersionTagStore(
        client=client,
        artifact_bucket=BUCKET,
        bucket_owner_account_id=BUCKET_OWNER,
    )


def _source() -> SourceArtifactRecord:
    material: dict[str, Any] = {
        "job_id": JOB_ID,
        "owner_id": OWNER_ID,
        "bucket": BUCKET,
        "object_key": OBJECT_KEY,
        "version_id": VERSION_ID,
        "content_sha256": "c" * 64,
        "size_bytes": 128,
        "media_type": "image/png",
        "product_profile_id": "gildan_64000_swiftpod",
        "product_profile_version": 2,
        "product_profile_fingerprint": "d" * 64,
        "created_at": NOW - timedelta(days=3),
    }
    return SourceArtifactRecord(
        fingerprint=source_artifact_fingerprint(**material),
        **material,
    )


def _job(source: SourceArtifactRecord | None = None) -> ControlJobRecord:
    exact_source = source or _source()
    return ControlJobRecord.model_validate(
        {
            "owner_id": OWNER_ID,
            "job_id": JOB_ID,
            "record_version": 4,
            "event_sequence": 5,
            "state": ControlJobState.AWAITING_APPROVAL,
            "review_version": 1,
            "review_fingerprint": "e" * 64,
            "review_validated": True,
            "source_artifact_fingerprint": exact_source.fingerprint,
            "created_at": NOW - timedelta(days=3),
            "updated_at": NOW - timedelta(days=1),
        }
    )


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _job_item(job: ControlJobRecord) -> dict[str, Any]:
    return {
        "PK": _s(f"JOB#{job.job_id}"),
        "SK": _s("META"),
        "entity_type": _s("CONTROL_JOB"),
        "contract_version": _s(job.contract_version),
        "owner_id": _s(job.owner_id),
        "record_version": _n(job.record_version),
        "payload": _s(job.model_dump_json()),
    }


def _source_item(source: SourceArtifactRecord) -> dict[str, Any]:
    return {
        "PK": _s(f"JOB#{source.job_id}"),
        "SK": _s("SOURCE"),
        "entity_type": _s("SOURCE_ARTIFACT"),
        "contract_version": _s(source.contract_version),
        "payload": _s(source.model_dump_json()),
    }


def _execution_aggregate_payload(
    *,
    aggregate_id: str,
    job_id: str,
    state: str,
    terminal_at: datetime,
    source_release_eligible_at: datetime,
    operational_expires_at: datetime,
    report_id: str,
    record_version: int = 7,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": "7.0.1",
        "aggregate_id": aggregate_id,
        "owner_id": OWNER_ID,
        "job_id": job_id,
        "state": state,
        "record_version": record_version,
        "event_sequence": record_version + 1,
        "provider_audit_record_version": 0,
        "provider_evidence_record_version": 0,
        "terminal_at": terminal_at.isoformat(),
        "source_release_eligible_at": source_release_eligible_at.isoformat(),
        "operational_expires_at": operational_expires_at.isoformat(),
        "report_id": report_id,
    }
    payload["fingerprint"] = canonical_fingerprint(
        {
            "contract_version": "7.0.1",
            "kind": "execution_aggregate",
            "payload": {
                key: value
                for key, value in payload.items()
                if key not in {"contract_version", "fingerprint"}
            },
        }
    )
    return payload


def _terminal_publication_authority() -> tuple[
    SourceArtifactRecord,
    ControlJobRecord,
    PublicationRetentionCompletionAuthority,
]:
    source = _source()
    terminal_at = NOW - timedelta(days=1)
    source_release_eligible_at = terminal_at + timedelta(days=30)
    operational_expires_at = terminal_at + timedelta(days=90)
    summary_fingerprint = publication_terminal_summary_fingerprint(
        aggregate_id=PUBLICATION_AGGREGATE_ID,
        terminal_state="published",
        terminal_at=terminal_at,
        source_release_eligible_at=source_release_eligible_at,
        operational_expires_at=operational_expires_at,
        report_id=PUBLICATION_REPORT_ID,
        result_id=PUBLICATION_RESULT_ID,
    )
    job_values = _job(source).model_dump(mode="python")
    job_values.update(
        {
            "record_version": 5,
            "state": ControlJobState.APPROVED,
            "product_id": "product_retention",
            "product_sync_id": "sync_retention",
            "synchronized_review_version": 1,
            "product_sync_fingerprint": "f" * 64,
            "pricing_snapshot_id": "pricing_retention",
            "pricing_snapshot_fingerprint": "1" * 64,
            "approval_decision_id": "decision_retention",
            "approved_review_version": 1,
            "approved_review_fingerprint": "e" * 64,
            "approval_fingerprint": "2" * 64,
            "publication_aggregate_id": PUBLICATION_AGGREGATE_ID,
            "publication_terminal_state": "published",
            "publication_terminal_at": terminal_at,
            "publication_source_release_eligible_at": source_release_eligible_at,
            "publication_operational_expires_at": operational_expires_at,
            "publication_report_id": PUBLICATION_REPORT_ID,
            "publication_result_id": PUBLICATION_RESULT_ID,
            "publication_terminal_summary_fingerprint": summary_fingerprint,
            "updated_at": terminal_at,
        }
    )
    job = ControlJobRecord.model_validate(job_values)
    aggregate_payload = _execution_aggregate_payload(
        aggregate_id=PUBLICATION_AGGREGATE_ID,
        job_id=job.job_id,
        state="published",
        terminal_at=terminal_at,
        source_release_eligible_at=source_release_eligible_at,
        operational_expires_at=operational_expires_at,
        report_id=PUBLICATION_REPORT_ID,
    )
    completion_values: dict[str, Any] = {
        "job_id": job.job_id,
        "aggregate_id": PUBLICATION_AGGREGATE_ID,
        "job_record_version": job.record_version,
        "terminal_state": "published",
        "terminal_at": terminal_at,
        "terminal_summary_fingerprint": summary_fingerprint,
        "source_artifact_fingerprint": source.fingerprint,
        "aggregate_fingerprint": aggregate_payload["fingerprint"],
        "report_id": PUBLICATION_REPORT_ID,
        "report_fingerprint": "4" * 64,
        "tombstone_fingerprint": "5" * 64,
        "terminal_job_link_fingerprint": "6" * 64,
        "source_release_eligible_at": source_release_eligible_at,
        "operational_expires_at": operational_expires_at,
        "expires_at_epoch_seconds": publication_operational_expiry_epoch(operational_expires_at),
        "publication_row_count": 20,
        "ttl_assignment_count": 22,
        "inventory_fingerprint": "7" * 64,
        "completed_at": terminal_at + timedelta(days=1),
    }
    completion_basis = PublicationRetentionCompletionAuthority.model_construct(
        **completion_values,
        fingerprint="0" * 64,
    )
    completion = PublicationRetentionCompletionAuthority(
        **completion_values,
        fingerprint=publication_retention_completion_fingerprint(completion_basis),
    )
    return source, job, completion


def _publication_retention_item(
    completion: PublicationRetentionCompletionAuthority,
) -> dict[str, Any]:
    return {
        "PK": _s(f"JOB#{completion.job_id}"),
        "SK": _s("PUBLICATION_RETENTION"),
        "entity_type": _s("PUBLICATION_RETENTION_COMPLETION"),
        "contract_version": _s(completion.contract_version),
        "job_id": _s(completion.job_id),
        "aggregate_id": _s(completion.aggregate_id),
        "job_record_version": _n(completion.job_record_version),
        "terminal_summary_fingerprint": _s(completion.terminal_summary_fingerprint),
        "source_artifact_fingerprint": _s(completion.source_artifact_fingerprint),
        "expires_at": _n(completion.expires_at_epoch_seconds),
        "payload": _s(completion.model_dump_json()),
    }


def _publication_aggregate_item(
    completion: PublicationRetentionCompletionAuthority,
) -> dict[str, Any]:
    record_version = 7
    payload = _execution_aggregate_payload(
        aggregate_id=completion.aggregate_id,
        job_id=completion.job_id,
        state=completion.terminal_state,
        terminal_at=completion.terminal_at,
        source_release_eligible_at=completion.source_release_eligible_at,
        operational_expires_at=completion.operational_expires_at,
        report_id=completion.report_id,
        record_version=record_version,
    )
    assert payload["fingerprint"] == completion.aggregate_fingerprint
    return {
        "PK": _s(f"PUBLICATION#{completion.aggregate_id}"),
        "SK": _s("META"),
        "entity_type": _s("PUBLICATION_EXECUTION_AGGREGATE"),
        "contract_version": _s("7.0.1"),
        "owner_id": _s(OWNER_ID),
        "job_id": _s(completion.job_id),
        "publication_state": _s(completion.terminal_state),
        "record_version": _n(record_version),
        "provider_audit_record_version": _n(payload["provider_audit_record_version"]),
        "provider_evidence_record_version": _n(payload["provider_evidence_record_version"]),
        "expires_at": _n(completion.expires_at_epoch_seconds),
        "payload": _s(json.dumps(payload, sort_keys=True, separators=(",", ":"))),
    }


def _checkpoint_store(client: RecordingDynamoClient) -> DynamoDBRetentionCheckpointStore:
    return DynamoDBRetentionCheckpointStore(client=client, table_name=TABLE)


def test_s3_inventory_uses_exact_prefix_bound_and_canonical_two_marker_cursor() -> None:
    first_response = _inventory_page(
        versions=[_version()],
        delete_markers=[_delete_marker()],
        truncated=True,
        next_key=OBJECT_KEY,
        next_version=NEXT_VERSION_ID,
        max_keys=2,
    )
    client = RecordingS3InventoryClient(
        [
            first_response,
            _inventory_page(
                max_keys=7,
                key_marker=OBJECT_KEY,
                version_marker=NEXT_VERSION_ID,
            ),
        ]
    )
    inventory = _inventory(client)

    first = inventory.list_source_versions(
        source_prefix=PHASE6_SOURCE_PREFIX,
        cursor=None,
        limit=2,
    )

    assert client.calls[0] == {
        "Bucket": BUCKET,
        "Prefix": "private/owners/",
        "MaxKeys": 2,
        "ExpectedBucketOwner": BUCKET_OWNER,
    }
    assert first.observed_at == NOW
    assert [(item.object_key, item.version_id) for item in first.versions] == [
        (OBJECT_KEY, VERSION_ID)
    ]
    assert first.delete_marker_count == 1
    assert first.next_cursor is not None
    assert OBJECT_KEY not in first.next_cursor
    assert NEXT_VERSION_ID not in first.next_cursor
    assert "=" not in first.next_cursor

    second = inventory.list_source_versions(
        source_prefix=PHASE6_SOURCE_PREFIX,
        cursor=first.next_cursor,
        limit=7,
    )

    assert client.calls[1] == {
        "Bucket": BUCKET,
        "Prefix": "private/owners/",
        "MaxKeys": 7,
        "ExpectedBucketOwner": BUCKET_OWNER,
        "KeyMarker": OBJECT_KEY,
        "VersionIdMarker": NEXT_VERSION_ID,
    }
    assert second.next_cursor is None
    with pytest.raises(RetentionBoundaryInvalidError):
        inventory.list_source_versions(
            source_prefix=PHASE6_SOURCE_PREFIX,
            cursor=f"{first.next_cursor}=",
            limit=7,
        )
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "response",
    (
        _inventory_page(versions=[_version(object_key="private/owners/outside.png")]),
        _inventory_page(delete_markers=[_delete_marker(object_key="private/owners/outside.png")]),
        _inventory_page(versions=[_version(), _version(version_id=NEXT_VERSION_ID)]),
        _inventory_page(truncated=True, next_key=OBJECT_KEY),
        _inventory_page(next_key=OBJECT_KEY, next_version=NEXT_VERSION_ID),
        {**_inventory_page(), "IsTruncated": "false"},
        {**_inventory_page(), "ResponseMetadata": {"HTTPHeaders": {"date": "secret"}}},
        {**_inventory_page(), "Name": "other-bucket"},
        {**_inventory_page(), "Prefix": "private/"},
        {**_inventory_page(), "MaxKeys": True},
        {**_inventory_page(), "KeyMarker": OBJECT_KEY},
    ),
)
def test_s3_inventory_rejects_malformed_or_over_bound_pages_without_leaking(
    response: dict[str, Any],
) -> None:
    client = RecordingS3InventoryClient([response])

    with pytest.raises(RetentionBoundaryInvalidError) as captured:
        _inventory(client).list_source_versions(
            source_prefix=PHASE6_SOURCE_PREFIX,
            cursor=None,
            limit=1,
        )

    assert "outside" not in str(captured.value)
    assert "secret" not in str(captured.value)


def test_s3_inventory_rejects_broader_prefix_and_bad_limit_before_call() -> None:
    client = RecordingS3InventoryClient([_inventory_page()])
    inventory = _inventory(client)

    with pytest.raises(RetentionBoundaryInvalidError):
        inventory.list_source_versions(source_prefix="private/", cursor=None, limit=1)
    with pytest.raises(RetentionBoundaryInvalidError):
        inventory.list_source_versions(
            source_prefix=PHASE6_SOURCE_PREFIX,
            cursor=None,
            limit=True,
        )

    assert client.calls == []


def test_s3_inventory_dependency_error_is_stable_and_detached() -> None:
    secret = f"denied for {OBJECT_KEY}?versionId={VERSION_ID}"
    client = RecordingS3InventoryClient([RuntimeError(secret)])

    with pytest.raises(RetentionDependencyUnavailableError) as captured:
        _inventory(client).list_source_versions(
            source_prefix=PHASE6_SOURCE_PREFIX,
            cursor=None,
            limit=1,
        )

    assert str(captured.value) == "Retention dependency is unavailable"
    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_s3_tag_store_reads_and_writes_only_the_exact_version_id() -> None:
    client = RecordingS3TaggingClient()
    tags = _tags(client)

    observed = tags.get_version_tags(object_key=OBJECT_KEY, version_id=VERSION_ID)
    tags.set_version_state(
        object_key=OBJECT_KEY,
        version_id=VERSION_ID,
        state="staged",
    )

    exact_authority = {
        "Bucket": BUCKET,
        "Key": OBJECT_KEY,
        "VersionId": VERSION_ID,
        "ExpectedBucketOwner": BUCKET_OWNER,
    }
    assert observed.tags[0].key == "mr-lister-state"
    assert observed.tags[0].value == "pinned"
    assert client.get_calls == [exact_authority]
    assert client.put_calls == [
        {
            **exact_authority,
            "Tagging": {"TagSet": [{"Key": "mr-lister-state", "Value": "staged"}]},
        }
    ]


@pytest.mark.parametrize(
    "response",
    (
        {},
        {"TagSet": "not-a-list"},
        {"TagSet": [{"Key": "mr-lister-state", "Value": "pinned", "secret": "x"}]},
        {"TagSet": [{"Key": "", "Value": "pinned"}]},
    ),
)
def test_s3_tag_store_rejects_malformed_tag_responses(response: dict[str, Any]) -> None:
    client = RecordingS3TaggingClient(response)

    with pytest.raises(RetentionBoundaryInvalidError):
        _tags(client).get_version_tags(object_key=OBJECT_KEY, version_id=VERSION_ID)

    assert client.put_calls == []


@pytest.mark.parametrize("operation", ("get", "put"))
def test_s3_tag_dependency_errors_are_sanitized(operation: str) -> None:
    secret = f"tag secret {OBJECT_KEY} {VERSION_ID}"
    client = RecordingS3TaggingClient()
    if operation == "get":
        client.get_error = RuntimeError(secret)
    else:
        client.put_error = RuntimeError(secret)
    tags = _tags(client)

    with pytest.raises(RetentionDependencyUnavailableError) as captured:
        if operation == "get":
            tags.get_version_tags(object_key=OBJECT_KEY, version_id=VERSION_ID)
        else:
            tags.set_version_state(
                object_key=OBJECT_KEY,
                version_id=VERSION_ID,
                state="pinned",
            )

    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_s3_tag_write_rejects_a_malformed_success_response() -> None:
    client = RecordingS3TaggingClient()
    client.put_response = {"ResponseMetadata": {"HTTPStatusCode": 204}}

    with pytest.raises(RetentionBoundaryInvalidError):
        _tags(client).set_version_state(
            object_key=OBJECT_KEY,
            version_id=VERSION_ID,
            state="pinned",
        )

    assert len(client.put_calls) == 1


def test_strong_authority_reader_uses_one_transactional_job_source_read() -> None:
    source = _source()
    job = _job(source)
    client = RecordingDynamoClient()
    client.transact_response = {
        "Responses": [{"Item": _job_item(job)}, {"Item": _source_item(source)}, {}]
    }
    reader = DynamoDBStrongSourceAuthorityReader(client=client, table_name=TABLE)

    snapshot = reader.read_source_authority_strong(job_id=JOB_ID)

    assert snapshot.job == job
    assert snapshot.source == source
    assert client.transact_calls == [
        {
            "TransactItems": [
                {
                    "Get": {
                        "TableName": TABLE,
                        "Key": {"PK": _s(f"JOB#{JOB_ID}"), "SK": _s("META")},
                    }
                },
                {
                    "Get": {
                        "TableName": TABLE,
                        "Key": {"PK": _s(f"JOB#{JOB_ID}"), "SK": _s("SOURCE")},
                    }
                },
                {
                    "Get": {
                        "TableName": TABLE,
                        "Key": {
                            "PK": _s(f"JOB#{JOB_ID}"),
                            "SK": _s("PUBLICATION_RETENTION"),
                        },
                    }
                },
            ]
        }
    ]
    assert client.get_calls == []


def test_strong_authority_reader_parses_exact_publication_retention_marker() -> None:
    source, job, completion = _terminal_publication_authority()
    job_row = {"Item": _job_item(job)}
    source_row = {"Item": _source_item(source)}
    marker_row = {"Item": _publication_retention_item(completion)}
    client = RecordingDynamoClient()
    client.transact_responses = [
        {"Responses": [job_row, source_row, marker_row]},
        {
            "Responses": [
                job_row,
                source_row,
                marker_row,
                {"Item": _publication_aggregate_item(completion)},
            ]
        },
    ]
    reader = DynamoDBStrongSourceAuthorityReader(client=client, table_name=TABLE)

    snapshot = reader.read_source_authority_strong(job_id=JOB_ID)

    assert snapshot.job == job
    assert snapshot.source == source
    assert snapshot.publication_retention == completion
    assert len(client.transact_calls) == 2
    assert client.transact_calls[1]["TransactItems"][-1] == {
        "Get": {
            "TableName": TABLE,
            "Key": {
                "PK": _s(f"PUBLICATION#{completion.aggregate_id}"),
                "SK": _s("META"),
            },
        }
    }


@pytest.mark.parametrize(
    "corruption",
    ("missing", "identity", "expiry", "claimed_fingerprint", "semantic_payload"),
)
def test_strong_authority_reader_rejects_missing_or_changed_terminal_aggregate(
    corruption: str,
) -> None:
    source, job, completion = _terminal_publication_authority()
    aggregate_item = _publication_aggregate_item(completion)
    if corruption == "missing":
        aggregate_row: dict[str, Any] = {}
    else:
        if corruption == "identity":
            aggregate_item["PK"] = _s("PUBLICATION#foreign_aggregate")
        elif corruption == "expiry":
            aggregate_item["expires_at"] = _n(completion.expires_at_epoch_seconds + 1)
        elif corruption == "claimed_fingerprint":
            payload = json.loads(aggregate_item["payload"]["S"])
            payload["fingerprint"] = "0" * 64
            aggregate_item["payload"] = _s(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
        else:
            payload = json.loads(aggregate_item["payload"]["S"])
            payload["event_sequence"] += 1
            aggregate_item["payload"] = _s(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
        aggregate_row = {"Item": aggregate_item}
    job_row = {"Item": _job_item(job)}
    source_row = {"Item": _source_item(source)}
    marker_row = {"Item": _publication_retention_item(completion)}
    client = RecordingDynamoClient()
    client.transact_responses = [
        {"Responses": [job_row, source_row, marker_row]},
        {"Responses": [job_row, source_row, marker_row, aggregate_row]},
    ]
    reader = DynamoDBStrongSourceAuthorityReader(client=client, table_name=TABLE)

    with pytest.raises(RetentionBoundaryInvalidError):
        reader.read_source_authority_strong(job_id=JOB_ID)

    assert len(client.transact_calls) == 2


@pytest.mark.parametrize("observed_state", ("staged", "pinned"))
@pytest.mark.parametrize("aggregate_failure", ("missing", "mutated"))
def test_source_sweeper_fails_closed_on_missing_or_mutated_terminal_aggregate(
    observed_state: str,
    aggregate_failure: str,
) -> None:
    source, job, completion = _terminal_publication_authority()
    aggregate_item = _publication_aggregate_item(completion)
    if aggregate_failure == "missing":
        aggregate_row: dict[str, Any] = {}
    else:
        aggregate_item["publication_state"] = _s("publication_failed")
        aggregate_row = {"Item": aggregate_item}
    job_row = {"Item": _job_item(job)}
    source_row = {"Item": _source_item(source)}
    marker_row = {"Item": _publication_retention_item(completion)}
    dynamo = RecordingDynamoClient()
    dynamo.transact_responses = [
        {"Responses": [job_row, source_row, marker_row]},
        {"Responses": [job_row, source_row, marker_row, aggregate_row]},
    ]
    inventory_client = RecordingS3InventoryClient(
        [_inventory_page(versions=[_version()], max_keys=100)]
    )
    tag_client = RecordingS3TaggingClient(
        {"TagSet": [{"Key": "mr-lister-state", "Value": observed_state}]}
    )
    sweeper = ReferenceAwareSourceVersionSweeper(
        inventory=_inventory(inventory_client),
        tags=_tags(tag_client),
        authority=DynamoDBStrongSourceAuthorityReader(client=dynamo, table_name=TABLE),
        checkpoints=_checkpoint_store(dynamo),
        artifact_bucket=BUCKET,
        clock=lambda: NOW,
    )

    with pytest.raises(RetentionDependencyUnavailableError):
        sweeper.sweep()

    expected_puts = (
        [
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "VersionId": VERSION_ID,
                "Tagging": {"TagSet": [{"Key": "mr-lister-state", "Value": "pinned"}]},
                "ExpectedBucketOwner": BUCKET_OWNER,
            }
        ]
        if observed_state == "staged"
        else []
    )
    assert tag_client.put_calls == expected_puts
    assert dynamo.put_calls == []


@pytest.mark.parametrize("corruption", ("malformed", "foreign", "mismatched"))
def test_strong_authority_reader_rejects_invalid_publication_retention_marker(
    corruption: str,
) -> None:
    source, job, completion = _terminal_publication_authority()
    retention_item = _publication_retention_item(completion)
    if corruption == "malformed":
        retention_item["payload"] = _s("{}")
    elif corruption == "foreign":
        retention_item["PK"] = _s("JOB#foreign_publication_job")
    else:
        retention_item["source_artifact_fingerprint"] = _s("0" * 64)
    client = RecordingDynamoClient()
    client.transact_response = {
        "Responses": [
            {"Item": _job_item(job)},
            {"Item": _source_item(source)},
            {"Item": retention_item},
        ]
    }
    reader = DynamoDBStrongSourceAuthorityReader(client=client, table_name=TABLE)

    with pytest.raises(RetentionBoundaryInvalidError):
        reader.read_source_authority_strong(job_id=JOB_ID)


def test_strong_authority_reader_returns_only_both_absent_and_rejects_partial_rows() -> None:
    client = RecordingDynamoClient()
    reader = DynamoDBStrongSourceAuthorityReader(client=client, table_name=TABLE)

    assert reader.read_source_authority_strong(job_id=JOB_ID).job is None
    client.transact_response = {"Responses": [{"Item": _job_item(_job())}, {}, {}]}

    with pytest.raises(RetentionBoundaryInvalidError):
        reader.read_source_authority_strong(job_id=JOB_ID)


def test_strong_authority_reader_rejects_incoherent_payload_and_sanitizes_dependency() -> None:
    source = _source()
    wrong_job = _job(source)
    bad_item = _job_item(wrong_job)
    bad_item["owner_id"] = _s("b" * 64)
    client = RecordingDynamoClient()
    client.transact_response = {
        "Responses": [{"Item": bad_item}, {"Item": _source_item(source)}, {}]
    }
    reader = DynamoDBStrongSourceAuthorityReader(client=client, table_name=TABLE)

    with pytest.raises(RetentionBoundaryInvalidError):
        reader.read_source_authority_strong(job_id=JOB_ID)

    secret = f"dynamo denied {OBJECT_KEY} {VERSION_ID}"
    client.transact_error = RuntimeError(secret)
    with pytest.raises(RetentionDependencyUnavailableError) as captured:
        reader.read_source_authority_strong(job_id=JOB_ID)
    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_checkpoint_load_is_strong_and_absence_maps_to_revision_zero() -> None:
    client = RecordingDynamoClient()
    store = _checkpoint_store(client)

    checkpoint = store.load_checkpoint()

    assert checkpoint == RetentionCheckpoint()
    assert client.get_calls == [
        {
            "TableName": TABLE,
            "Key": {
                "PK": _s(RETENTION_CHECKPOINT_PARTITION_KEY),
                "SK": _s(RETENTION_CHECKPOINT_SORT_KEY),
            },
            "ConsistentRead": True,
        }
    ]


def test_checkpoint_first_save_is_create_only_and_survives_reconstruction() -> None:
    client = RecordingDynamoClient()
    store = _checkpoint_store(client)
    updated = RetentionCheckpoint(revision=1)

    store.save_checkpoint(expected=RetentionCheckpoint(), updated=updated)

    assert client.put_calls == [
        {
            "TableName": TABLE,
            "Item": {
                "PK": _s(RETENTION_CHECKPOINT_PARTITION_KEY),
                "SK": _s(RETENTION_CHECKPOINT_SORT_KEY),
                "entity_type": _s("SOURCE_VERSION_RETENTION_CHECKPOINT"),
                "contract_version": _s("1.0.0"),
                "revision": _n(1),
                "payload": _s(updated.model_dump_json()),
            },
            "ConditionExpression": "attribute_not_exists(PK)",
        }
    ]
    reconstructed = _checkpoint_store(client)
    assert reconstructed.load_checkpoint() == updated


def test_checkpoint_existing_save_cas_binds_revision_and_exact_payload() -> None:
    client = RecordingDynamoClient()
    expected = RetentionCheckpoint(revision=1)
    updated = RetentionCheckpoint(revision=2)

    _checkpoint_store(client).save_checkpoint(expected=expected, updated=updated)

    assert client.put_calls[0]["ConditionExpression"] == (
        "entity_type = :entity_type AND revision = :expected_revision "
        "AND payload = :expected_payload"
    )
    assert client.put_calls[0]["ExpressionAttributeValues"] == {
        ":entity_type": _s("SOURCE_VERSION_RETENTION_CHECKPOINT"),
        ":expected_revision": _n(1),
        ":expected_payload": _s(expected.model_dump_json()),
    }
    assert client.put_calls[0]["Item"]["revision"] == _n(2)


def test_checkpoint_cas_failure_and_load_failure_are_sanitized() -> None:
    secret = "checkpoint conditional response contains private cursor"
    client = RecordingDynamoClient()
    client.put_error = RuntimeError(secret)
    store = _checkpoint_store(client)

    with pytest.raises(RetentionDependencyUnavailableError) as captured:
        store.save_checkpoint(
            expected=RetentionCheckpoint(),
            updated=RetentionCheckpoint(revision=1),
        )

    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    client.get_error = RuntimeError(secret)
    with pytest.raises(RetentionDependencyUnavailableError) as captured_load:
        store.load_checkpoint()
    assert secret not in repr(captured_load.value)
    assert captured_load.value.__context__ is None


def test_checkpoint_write_rejects_malformed_success_and_nondefault_zero_basis() -> None:
    client = RecordingDynamoClient()
    client.put_response = {}
    store = _checkpoint_store(client)

    with pytest.raises(RetentionBoundaryInvalidError):
        store.save_checkpoint(
            expected=RetentionCheckpoint(),
            updated=RetentionCheckpoint(revision=1),
        )

    cursor = "nondefault-zero"
    nondefault_zero = RetentionCheckpoint(
        cursor=cursor,
        seen_cursor_digests=(sha256(cursor.encode()).hexdigest(),),
        scan_pages=1,
        scan_items=1,
    )
    with pytest.raises(RetentionBoundaryInvalidError):
        store.save_checkpoint(
            expected=nondefault_zero,
            updated=RetentionCheckpoint(revision=1),
        )

    assert len(client.put_calls) == 1


def test_checkpoint_rejects_malformed_rows_and_nonincrementing_writes() -> None:
    client = RecordingDynamoClient()
    client.get_response = {
        "Item": {
            "PK": _s(RETENTION_CHECKPOINT_PARTITION_KEY),
            "SK": _s(RETENTION_CHECKPOINT_SORT_KEY),
            "entity_type": _s("SOURCE_VERSION_RETENTION_CHECKPOINT"),
            "contract_version": _s("1.0.0"),
            "revision": _n(99),
            "payload": _s(RetentionCheckpoint(revision=1).model_dump_json()),
        }
    }
    store = _checkpoint_store(client)

    with pytest.raises(RetentionBoundaryInvalidError):
        store.load_checkpoint()
    with pytest.raises(RetentionBoundaryInvalidError):
        store.save_checkpoint(
            expected=RetentionCheckpoint(revision=1),
            updated=RetentionCheckpoint(revision=3),
        )

    assert client.put_calls == []


def test_adapters_expose_no_object_byte_delete_secret_or_provider_surface() -> None:
    inventory = _inventory(RecordingS3InventoryClient([_inventory_page()]))
    tags = _tags(RecordingS3TaggingClient())
    dynamo = RecordingDynamoClient()
    adapters = (
        inventory,
        tags,
        DynamoDBStrongSourceAuthorityReader(client=dynamo, table_name=TABLE),
        _checkpoint_store(dynamo),
    )
    forbidden = {
        "get_object",
        "delete_object",
        "delete_objects",
        "get_secret_value",
        "request",
        "publish",
        "create_order",
        "fulfill",
    }

    for adapter in adapters:
        assert all(not hasattr(adapter, name) for name in forbidden)


def test_recording_aws_adapters_compose_with_reference_aware_core_without_network() -> None:
    inventory_client = RecordingS3InventoryClient(
        [_inventory_page(versions=[_version()], max_keys=100)]
    )
    tag_client = RecordingS3TaggingClient(
        {"TagSet": [{"Key": "mr-lister-state", "Value": "staged"}]}
    )
    dynamo = RecordingDynamoClient()
    sweeper = ReferenceAwareSourceVersionSweeper(
        inventory=_inventory(inventory_client),
        tags=_tags(tag_client),
        authority=DynamoDBStrongSourceAuthorityReader(client=dynamo, table_name=TABLE),
        checkpoints=_checkpoint_store(dynamo),
        artifact_bucket=BUCKET,
        clock=lambda: NOW,
    )

    result = sweeper.sweep()

    assert result.scan_complete is True
    assert result.versions_scanned == 1
    assert result.staged_versions_unchanged == 1
    assert tag_client.put_calls == []
    assert len(dynamo.transact_calls) == 1
    assert len(dynamo.put_calls) == 1
    assert dynamo.put_calls[0]["Item"]["revision"] == _n(1)


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    (
        (
            S3SourceVersionInventory,
            {
                "client": RecordingS3InventoryClient([]),
                "artifact_bucket": "INVALID/BUCKET",
                "bucket_owner_account_id": BUCKET_OWNER,
            },
        ),
        (
            S3SourceVersionTagStore,
            {
                "client": RecordingS3TaggingClient(),
                "artifact_bucket": BUCKET,
                "bucket_owner_account_id": "owner-secret",
            },
        ),
        (
            DynamoDBRetentionCheckpointStore,
            {"client": RecordingDynamoClient(), "table_name": "x"},
        ),
    ),
)
def test_adapter_configuration_is_strict(factory: Any, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        factory(**kwargs)
