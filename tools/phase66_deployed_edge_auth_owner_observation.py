#!/usr/bin/env python3
"""Capture one sanitized, read-only deployed Phase 6.6 edge/owner observation.

The runner consumes an exact repository-private deployment authority and the existing sanitized
upload-integrity baseline.  It invokes only the deployed review Lambda with synthetic authorizer
claims, reads Cognito/DynamoDB/S3/Step Functions deployment state through the shared read-only
backend, and writes the frozen sanitized observation v1.  It never reads browser cookies, tokens,
or storage and has no upload, job, workflow, provider, Bedrock, or AgentCore write path.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    model_validator,
)

from tools.phase66_deployed_upload_integrity_smoke import (
    PRIMARY_SHA256,
    PRIMARY_SIZE,
    Authority,
    AwsBackend,
    RunGate,
    SmokeError,
    Snapshot,
    _canonical_json,
    _digest_json,
    _digest_text,
    _event,
    _mapping,
    _private_directory_descriptor,
    _read_private_file,
    _response_body,
    _strict_json,
    _write_once_private_json,
    exact_canaries,
)
from tools.prepare_phase66_edge_revalidation import (
    OBSERVATION_FORMAT,
    SOURCE_COMMIT_DIGEST,
    _DeploymentAuthorityDocument,
    _EdgeObservation,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"
BASELINE_FORMAT: Final = "phase6.6-upload-integrity-read-only-preflight-v1"
BROWSER_CHECKPOINT_FORMAT: Final = "phase6.6-sanitized-browser-edge-auth-owner-checkpoint-v1"
MAX_INPUT_BYTES: Final = 4 * 1024 * 1024
UNKNOWN_JOB_ID: Final = "job_00000000000000000000000000000000"

_BASELINE_FIELDS: Final = frozenset(
    {
        "actor_digest",
        "baseline_contract",
        "bucket_versioning_enabled",
        "canary_byte_count",
        "canary_sha256",
        "entity_type_counts",
        "existing_job_count",
        "existing_job_digests",
        "existing_job_set_digest",
        "existing_job_states",
        "provider_record_count",
        "running_execution_count",
        "selected_content_sha256",
        "selected_inventory_count",
        "selected_inventory_digest",
        "selected_job_digest",
        "selected_job_record_digest",
        "selected_object_coordinate_digest",
        "selected_pinned_head_matches",
        "selected_pinned_is_latest",
        "selected_pinned_tag_matches",
        "selected_pinned_version_digest",
        "selected_source_authority_digest",
        "selected_source_record_digest",
        "table_record_count",
        "table_scanned_count",
    }
)
_DIGEST_FIELDS: Final = (
    "actor_digest",
    "canary_sha256",
    "existing_job_set_digest",
    "selected_content_sha256",
    "selected_inventory_digest",
    "selected_job_digest",
    "selected_job_record_digest",
    "selected_object_coordinate_digest",
    "selected_pinned_version_digest",
    "selected_source_authority_digest",
    "selected_source_record_digest",
)
_COUNT_FIELDS: Final = (
    "canary_byte_count",
    "existing_job_count",
    "provider_record_count",
    "running_execution_count",
    "selected_inventory_count",
    "table_record_count",
    "table_scanned_count",
)
_HEAD_FIELDS: Final = frozenset({"checksum", "content_type", "encryption", "size", "version"})


class Phase66EdgeObservationError(RuntimeError):
    """A closed input, deployed observation, or output assertion failed."""


type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type CanonicalTimestamp = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"),
]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _BrowserActorA(_ClosedModel):
    visible_job_count: Literal[2]
    known_review_ready: Literal[True]
    known_preview_ready: Literal[True]


class _BrowserActorB(_ClosedModel):
    visible_job_count: Literal[0]


class _BrowserAuthMatrix(_ClosedModel):
    pkce_authorization_passed: Literal[True]
    pkce_callback_passed: Literal[True]
    token_exchange_passed: Literal[True]
    unauthenticated_access_rejected: Literal[True]


class _BrowserCheckpoint(_ClosedModel):
    format: Literal[BROWSER_CHECKPOINT_FORMAT]
    recorded_at: CanonicalTimestamp
    deployment_digest: Digest
    actor_a: _BrowserActorA
    actor_b: _BrowserActorB
    matrix: _BrowserAuthMatrix

    @model_validator(mode="after")
    def timestamp_is_calendar_valid(self) -> _BrowserCheckpoint:
        datetime.strptime(self.recorded_at, "%Y-%m-%dT%H:%M:%SZ")
        return self


class ObservationBackend(Protocol):
    """The read-only backend surface used by the observation."""

    def prepare(self, gate: RunGate, primary: bytes) -> Snapshot: ...

    def confirmed_seller_subjects(self) -> Sequence[str]: ...

    def invoke_review(
        self, authority: Authority, event: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def snapshot(self, authority: Authority) -> Snapshot: ...


class AwsObservationBackend(AwsBackend):
    """AWS backend narrowed to confirmed seller reads plus inherited read-only calls."""

    def confirmed_seller_subjects(self) -> tuple[str, ...]:
        outputs = self._stack_outputs()
        pool_id = outputs.get("SellerUserPoolId")
        if not isinstance(pool_id, str) or not pool_id:
            raise SmokeError("seller pool output is unavailable")
        users: list[Mapping[str, Any]] = []
        token: str | None = None
        observed_tokens: set[str] = set()
        for _ in range(100):
            request: dict[str, object] = {"UserPoolId": pool_id, "Limit": 60}
            if token is not None:
                request["PaginationToken"] = token
            page = _mapping(self._cognito.list_users(**request), "Cognito user page")
            raw_users = page.get("Users", [])
            if not isinstance(raw_users, Sequence) or isinstance(
                raw_users, (str, bytes, bytearray)
            ):
                raise SmokeError("Cognito user page is invalid")
            for user in raw_users:
                users.append(_mapping(user, "Cognito user"))
            next_token = page.get("PaginationToken")
            if next_token is None:
                break
            if not isinstance(next_token, str) or not next_token or next_token in observed_tokens:
                raise SmokeError("Cognito user pagination is invalid")
            observed_tokens.add(next_token)
            token = next_token
        else:
            raise SmokeError("Cognito user pagination exceeded its bound")

        subjects: list[str] = []
        for user in users:
            username = user.get("Username")
            if not isinstance(username, str) or not username:
                continue
            detail = _mapping(
                self._cognito.admin_get_user(UserPoolId=pool_id, Username=username),
                "Cognito user detail",
            )
            groups_response = _mapping(
                self._cognito.admin_list_groups_for_user(
                    UserPoolId=pool_id,
                    Username=username,
                    Limit=60,
                ),
                "Cognito group detail",
            )
            raw_attributes = detail.get("UserAttributes", [])
            raw_groups = groups_response.get("Groups", [])
            if (
                not isinstance(raw_attributes, Sequence)
                or isinstance(raw_attributes, (str, bytes, bytearray))
                or not isinstance(raw_groups, Sequence)
                or isinstance(raw_groups, (str, bytes, bytearray))
            ):
                raise SmokeError("Cognito seller authority is invalid")
            attributes = {
                item.get("Name"): item.get("Value")
                for raw_item in raw_attributes
                for item in (_mapping(raw_item, "Cognito attribute"),)
            }
            groups = {
                _mapping(raw_group, "Cognito group").get("GroupName") for raw_group in raw_groups
            }
            subject = attributes.get("sub")
            if (
                detail.get("Enabled") is True
                and detail.get("UserStatus") == "CONFIRMED"
                and groups == {"seller"}
                and isinstance(subject, str)
                and subject
            ):
                subjects.append(subject)
        if len(subjects) != len(set(subjects)):
            raise SmokeError("confirmed seller subjects are not unique")
        return tuple(subjects)


def _digest(value: object) -> str:
    return _digest_json(value)


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _private_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(PRIVATE_ROOT)
    except ValueError:
        raise Phase66EdgeObservationError(
            "edge observation paths must stay in the repository-private workspace"
        ) from None
    if not relative.parts:
        raise Phase66EdgeObservationError("edge observation paths must name a private child")
    return candidate


def _read_json(path: Path, label: str) -> object:
    candidate = _private_path(path)
    try:
        return _strict_json(
            _read_private_file(candidate, max_bytes=MAX_INPUT_BYTES),
            label,
        )
    except SmokeError:
        raise Phase66EdgeObservationError(f"{label} is not one exact private JSON file") from None


def _deployment(path: Path) -> _DeploymentAuthorityDocument:
    try:
        value = _DeploymentAuthorityDocument.model_validate(
            _read_json(path, "deployment authority")
        )
    except (ValidationError, ValueError):
        raise Phase66EdgeObservationError(
            "deployment authority does not match the frozen sanitized contract"
        ) from None
    if value.authority.source_commit_digest != SOURCE_COMMIT_DIGEST:
        raise Phase66EdgeObservationError(
            "deployment authority does not bind the frozen Phase 6 source"
        )
    return value


def _baseline(path: Path) -> Mapping[str, Any]:
    try:
        value = _mapping(_read_json(path, "baseline preflight"), "baseline preflight")
        if set(value) != _BASELINE_FIELDS or value.get("baseline_contract") != BASELINE_FORMAT:
            raise ValueError
        if any(not _is_digest(value.get(field)) for field in _DIGEST_FIELDS):
            raise ValueError
        if any(type(value.get(field)) is not int or value[field] < 0 for field in _COUNT_FIELDS):
            raise ValueError
        if value["existing_job_count"] != 2 or value["provider_record_count"] != 0:
            raise ValueError
        if value["canary_byte_count"] != PRIMARY_SIZE or value["canary_sha256"] != PRIMARY_SHA256:
            raise ValueError
        if value["running_execution_count"] != 0 or value["selected_inventory_count"] < 1:
            raise ValueError
        if value["table_record_count"] != value["table_scanned_count"]:
            raise ValueError
        for field in (
            "bucket_versioning_enabled",
            "selected_pinned_is_latest",
            "selected_pinned_tag_matches",
        ):
            if value.get(field) is not True:
                raise ValueError
        if value["canary_sha256"] != value["selected_content_sha256"]:
            raise ValueError
        job_digests = value.get("existing_job_digests")
        states = value.get("existing_job_states")
        if (
            not isinstance(job_digests, list)
            or len(job_digests) != 2
            or job_digests != sorted(job_digests)
            or len(set(job_digests)) != 2
            or any(not _is_digest(item) for item in job_digests)
            or value["selected_job_digest"] not in job_digests
            or _digest_json(job_digests) != value["existing_job_set_digest"]
            or not isinstance(states, list)
            or len(states) != 2
            or states != sorted(states)
            or any(not isinstance(state, str) or not state for state in states)
        ):
            raise ValueError
        head = _mapping(value.get("selected_pinned_head_matches"), "pinned head proof")
        if set(head) != _HEAD_FIELDS or any(head.get(field) is not True for field in _HEAD_FIELDS):
            raise ValueError
        counts = _mapping(value.get("entity_type_counts"), "entity type counts")
        if (
            not counts
            or any(
                not isinstance(name, str) or not name or type(count) is not int or count < 0
                for name, count in counts.items()
            )
            or sum(counts.values()) != value["table_record_count"]
            or counts.get("CONTROL_JOB") != 2
            or counts.get("SOURCE_ARTIFACT") != 2
            or any(name.startswith("PROVIDER_") and count for name, count in counts.items())
        ):
            raise ValueError
    except (KeyError, SmokeError, TypeError, ValueError):
        raise Phase66EdgeObservationError(
            "baseline preflight does not match the exact sanitized read-only contract"
        ) from None
    return value


def _browser_checkpoint(path: Path) -> _BrowserCheckpoint:
    try:
        return _BrowserCheckpoint.model_validate(_read_json(path, "browser checkpoint"))
    except (ValidationError, ValueError):
        raise Phase66EdgeObservationError(
            "browser checkpoint does not match the exact sanitized contract"
        ) from None


def _gate(deployment: _DeploymentAuthorityDocument, baseline: Mapping[str, Any]) -> RunGate:
    normalized = {
        key: baseline[key]
        for key in (
            "actor_digest",
            "bucket_versioning_enabled",
            "existing_job_count",
            "existing_job_set_digest",
            "existing_job_states",
            "provider_record_count",
            "running_execution_count",
            "selected_inventory_count",
            "selected_inventory_digest",
            "selected_job_digest",
            "selected_job_record_digest",
            "selected_object_coordinate_digest",
            "selected_pinned_is_latest",
            "selected_pinned_version_digest",
            "selected_source_authority_digest",
            "selected_source_record_digest",
            "table_record_count",
        )
    }
    normalized["selected_version_head_matches_exact_canary"] = True
    normalized["selected_version_tag_is_pinned"] = True
    return RunGate(
        digest=_digest(
            {
                "contract": "phase6.6-edge-observation-baseline-binding-v1",
                "deployment_digest": deployment.deployment_digest,
                "baseline": normalized,
            }
        ),
        document={
            "baseline": normalized,
            "deployment_digest": deployment.deployment_digest,
            "prerequisite_evidence_run_digest": "0" * 64,
        },
    )


def _owner(issuer: str, subject: str) -> str:
    return hashlib.sha256(issuer.encode("utf-8") + b"\0" + subject.encode("utf-8")).hexdigest()


def _body(response: Mapping[str, Any], status: int) -> Mapping[str, Any]:
    try:
        return _response_body(response, status)
    except SmokeError:
        raise Phase66EdgeObservationError("deployed review response failed closed") from None


def _alias(entropy: Callable[[int], bytes]) -> str:
    value = entropy(16)
    if not isinstance(value, bytes) or len(value) != 16:
        raise Phase66EdgeObservationError("edge observation entropy is invalid")
    return "alias_" + value.hex()


def _timestamp(clock: Callable[[], datetime]) -> tuple[str, datetime]:
    value = clock()
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise Phase66EdgeObservationError("edge observation clock is invalid")
    normalized = value.astimezone(UTC).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ"), normalized


def _state_digest(snapshot: Snapshot) -> tuple[object, ...]:
    records = Counter((kind, _digest_json(payload)) for kind, payload in snapshot.items)
    inventory = tuple(
        sorted(
            (_digest_json(item.sanitized()) for item in snapshot.inventory),
        )
    )
    return (
        snapshot.entity_counts,
        snapshot.execution_digests,
        records,
        inventory,
        _digest_json(snapshot.selected_job),
        _digest_json(snapshot.selected_source),
    )


def capture_phase66_edge_observation(
    *,
    deployment_authority_path: Path,
    baseline_preflight_path: Path,
    browser_checkpoint_path: Path,
    output_path: Path,
    backend: ObservationBackend,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    entropy: Callable[[int], bytes] = secrets.token_bytes,
) -> dict[str, object]:
    """Run the closed read-only matrix and create one sanitized observation file."""

    deployment_path = _private_path(deployment_authority_path)
    baseline_path = _private_path(baseline_preflight_path)
    checkpoint_path = _private_path(browser_checkpoint_path)
    output = _private_path(output_path)
    if output in {deployment_path, baseline_path, checkpoint_path}:
        raise Phase66EdgeObservationError("edge observation output cannot replace an input")
    deployment = _deployment(deployment_path)
    baseline = _baseline(baseline_path)
    checkpoint = _browser_checkpoint(checkpoint_path)
    if checkpoint.deployment_digest != deployment.deployment_digest:
        raise Phase66EdgeObservationError(
            "browser checkpoint does not bind the exact deployment authority"
        )
    recorded_at, recorded_datetime = _timestamp(clock)
    captured_datetime = datetime.strptime(deployment.captured_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    checkpoint_datetime = datetime.strptime(checkpoint.recorded_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    if checkpoint_datetime < captured_datetime or recorded_datetime < checkpoint_datetime:
        raise Phase66EdgeObservationError(
            "browser checkpoint time is outside the deployment observation interval"
        )

    primary, _wrong, _overwrite = exact_canaries()
    gate = _gate(deployment, baseline)
    try:
        before = backend.prepare(gate, primary)
        actor_a = before.authority
        job_owners = {
            payload.get("owner_id")
            for entity_type, payload in before.items
            if entity_type == "CONTROL_JOB"
        }
        subjects = backend.confirmed_seller_subjects()
    except (SmokeError, ValueError):
        raise Phase66EdgeObservationError(
            "deployed read-only authority preparation failed closed"
        ) from None
    if (
        _digest_text(actor_a.owner_id) != baseline["actor_digest"]
        or _digest_text(actor_a.job_id) != baseline["selected_job_digest"]
        or job_owners != {actor_a.owner_id}
        or actor_a.subject not in subjects
    ):
        raise Phase66EdgeObservationError(
            "deployed actor/job authority does not match the exact baseline"
        )
    candidates = [
        subject
        for subject in subjects
        if isinstance(subject, str) and _owner(actor_a.issuer, subject) not in job_owners
    ]
    if len(candidates) != 1 or len(set(subjects)) != len(subjects):
        raise Phase66EdgeObservationError("Seller B did not resolve uniquely")
    actor_b = replace(
        actor_a,
        owner_id=_owner(actor_a.issuer, candidates[0]),
        subject=candidates[0],
    )

    try:
        list_a = _body(
            backend.invoke_review(actor_a, _event(actor_a, "GET /v1/jobs", "/v1/jobs")),
            200,
        )
        list_b = _body(
            backend.invoke_review(actor_b, _event(actor_b, "GET /v1/jobs", "/v1/jobs")),
            200,
        )
    except (SmokeError, ValueError):
        raise Phase66EdgeObservationError("deployed owner list matrix failed closed") from None
    jobs_a = list_a.get("jobs")
    jobs_b = list_b.get("jobs")
    if not isinstance(jobs_a, list) or len(jobs_a) != 2 or jobs_b != []:
        raise Phase66EdgeObservationError("deployed owner list matrix drifted")

    review_event = _event(
        actor_a,
        "GET /v1/jobs/{job_id}/review",
        f"/v1/jobs/{actor_a.job_id}/review",
        path_parameters={"job_id": actor_a.job_id},
    )
    try:
        first = _mapping(backend.invoke_review(actor_a, review_event), "review response")
        second = _mapping(backend.invoke_review(actor_a, review_event), "review response")
        first_body = _body(first, 200)
        second_body = _body(second, 200)
        first_headers = _mapping(first.get("headers"), "review headers")
        second_headers = _mapping(second.get("headers"), "review headers")
    except (SmokeError, ValueError):
        raise Phase66EdgeObservationError("strong review authority check failed closed") from None
    first_etag = first_headers.get("ETag")
    second_etag = second_headers.get("ETag")
    first_review_authority = first_body.get("review_authority_etag")
    second_review_authority = second_body.get("review_authority_etag")
    first_preview = first_body.get("preview")
    second_preview = second_body.get("preview")
    if (
        not isinstance(first_etag, str)
        or first_etag != second_etag
        or not first_etag.startswith('"')
        or not first_etag.endswith('"')
        or not isinstance(first_review_authority, str)
        or first_review_authority != second_review_authority
        or not isinstance(first_preview, Mapping)
        or not isinstance(second_preview, Mapping)
        or first_preview.get("readiness") != "ready"
        or second_preview.get("readiness") != "ready"
    ):
        raise Phase66EdgeObservationError("strong review authority observation drifted")

    foreign_event = _event(
        actor_b,
        "GET /v1/jobs/{job_id}",
        f"/v1/jobs/{actor_a.job_id}",
        path_parameters={"job_id": actor_a.job_id},
    )
    unknown_event = _event(
        actor_b,
        "GET /v1/jobs/{job_id}",
        f"/v1/jobs/{UNKNOWN_JOB_ID}",
        path_parameters={"job_id": UNKNOWN_JOB_ID},
    )
    try:
        foreign = _mapping(backend.invoke_review(actor_b, foreign_event), "foreign response")
        unknown = _mapping(backend.invoke_review(actor_b, unknown_event), "unknown response")
        after = backend.snapshot(actor_a)
    except (SmokeError, ValueError):
        raise Phase66EdgeObservationError("deployed owner absence matrix failed closed") from None
    if foreign.get("statusCode") != 404 or unknown.get("statusCode") != 404:
        raise Phase66EdgeObservationError("foreign and unknown absence did not match")
    if _state_digest(before) != _state_digest(after):
        raise Phase66EdgeObservationError("read-only edge observation changed application state")
    if any(kind.startswith("PROVIDER_") for kind, _payload in before.items + after.items):
        raise Phase66EdgeObservationError("provider records appeared during edge observation")

    correlation = before.selected_job.get("correlation_id")
    if not isinstance(correlation, str) or not correlation:
        correlation = _digest_json(before.selected_job)
    aliases = tuple(_alias(entropy) for _ in range(5))
    checkpoint_digest = _digest(checkpoint.model_dump(mode="json"))
    authorities = (
        _digest(
            {
                "contract": "phase6.6-edge-revalidation-browser-binding-v1",
                "browser_checkpoint_digest": checkpoint_digest,
                "deployment_digest": deployment.deployment_digest,
            }
        ),
        _digest_text(actor_a.owner_id),
        _digest_text(actor_b.owner_id),
        _digest_text(actor_a.job_id),
        _digest_text(correlation),
    )
    if len(set(aliases)) != 5 or len(set(authorities)) != 5:
        raise Phase66EdgeObservationError("run-scoped observation authorities are not distinct")
    observation: dict[str, object] = {
        "format": OBSERVATION_FORMAT,
        "recorded_at": recorded_at,
        "run": {"alias": aliases[0], "authority_digest": authorities[0]},
        "actor_a": {
            "alias": aliases[1],
            "authority_digest": authorities[1],
            "visible_job_count": 2,
            "known_review_ready": True,
            "known_preview_ready": True,
        },
        "actor_b": {
            "alias": aliases[2],
            "authority_digest": authorities[2],
            "visible_job_count": 0,
            "actor_a_job_absent": True,
            "unknown_job_absent": True,
        },
        "known_job": {"alias": aliases[3], "authority_digest": authorities[3]},
        "correlation": {"alias": aliases[4], "authority_digest": authorities[4]},
        "matrix": {
            "health_passed": True,
            "readiness_passed": True,
            "security_headers_passed": True,
            "cors_passed": True,
            "pkce_authorization_passed": checkpoint.matrix.pkce_authorization_passed,
            "pkce_callback_passed": checkpoint.matrix.pkce_callback_passed,
            "token_exchange_passed": checkpoint.matrix.token_exchange_passed,
            "unauthenticated_access_rejected": checkpoint.matrix.unauthenticated_access_rejected,
        },
        "review": {
            "access_path": "direct_deployed_review_lambda",
            "invocation_count": 2,
            "review_ready": True,
            "preview_ready": True,
            "etag_type": "strong",
            "first_etag_digest": _digest_text(first_etag),
            "second_etag_digest": _digest_text(second_etag),
        },
        "deltas": {
            "provider_call_delta": 0,
            "provider_record_delta": 0,
            "work_item_delta": 0,
            "workflow_execution_delta": 0,
        },
    }
    try:
        _DeploymentAuthorityDocument.model_validate(deployment.model_dump(mode="json"))
        _EdgeObservation.model_validate(observation)
        with _private_directory_descriptor(output.parent, create=True) as descriptor:
            byte_count, observation_digest = _write_once_private_json(
                descriptor,
                output.name,
                observation,
            )
    except (OSError, SmokeError, ValidationError, ValueError):
        raise Phase66EdgeObservationError(
            "sanitized edge observation could not be validated and written"
        ) from None
    return {
        "byte_count": byte_count,
        "deployment_digest": deployment.deployment_digest,
        "observation_sha256": observation_digest,
        "status": "passed",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-authority", required=True, type=Path)
    parser.add_argument("--baseline-preflight", required=True, type=Path)
    parser.add_argument("--browser-checkpoint", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend_factory: Callable[[], ObservationBackend] = AwsObservationBackend,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    entropy: Callable[[int], bytes] = secrets.token_bytes,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = capture_phase66_edge_observation(
            deployment_authority_path=arguments.deployment_authority,
            baseline_preflight_path=arguments.baseline_preflight,
            browser_checkpoint_path=arguments.browser_checkpoint,
            output_path=arguments.output,
            backend=backend_factory(),
            clock=clock,
            entropy=entropy,
        )
    except Phase66EdgeObservationError as error:
        _parser().error(str(error))
    print(_canonical_json(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        if isinstance(error, SystemExit):
            raise
        raise SystemExit(
            "phase66 deployed edge/auth/owner observation stopped: "
            "an external operation failed closed"
        ) from None
