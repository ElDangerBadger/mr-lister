"""Verify one canonical, freshly captured Phase 6 core live-state document.

This verifier is deliberately offline. It imports no AWS SDK, starts no subprocess, and makes no
network request. Operators normalize read-only AWS observations into the closed
``mr-lister-phase6-core-live-state-v1`` format and write the document with
``canonical_phase6_core_live_state`` before calling :func:`verify_phase6_core_live_state`.

The format has three modes:

``staged``
    The exact release is deployed, every backend trigger is disabled, all functions remain
    scaffold-only, and the three maintenance functions are zero-throttled.
``capacity-released-inert``
    The exact three zero-concurrency settings have been removed, while scaffold mode and all
    triggers remain inert. A fresh, strongly consistent empty-table scan and zero-running-
    execution preflight are mandatory.
``backend-active-draft-only``
    Scaffold mode is removed and only the exact five reviewed backend triggers are enabled.
    Publication, order, fulfillment, and every public web surface remain absent. The same fresh
    safety preflight is mandatory and the document must bind capacity-released evidence.

Every object is closed: missing and additional members fail. The canonical source document is
sorted, two-space-indented JSON with one trailing newline. Its SHA-256 is returned in a frozen
verified record. The preflight scan uses ``Select=COUNT`` and never captures item content. Its
normalized response has exact zero ``Count`` and ``ScannedCount`` values plus an explicit null
``LastEvaluatedKey``. Only the top-level evidence under review must be fresh relative to verifier
time. Hash-bound predecessor and ancestor documents may be older, but remain fully validated and
must never be temporally later than their successors.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal, cast

Phase6CoreLiveMode = Literal[
    "staged",
    "capacity-released-inert",
    "backend-active-draft-only",
]

EVIDENCE_FORMAT: Final = "mr-lister-phase6-core-live-state-v1"
ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
STACK_NAME: Final = "mr-lister-phase6-dev"
STACK_ID: Final = (
    "arn:aws:cloudformation:us-west-2:384627057108:stack/mr-lister-phase6-dev/"
    "f3456970-9fdc-11f1-b448-06b81627db1d"
)
SERVICE_ROLE_ARN: Final = "arn:aws:iam::384627057108:role/mr-lister-phase6-runtime-cfn-dev"
RELEASE_FINGERPRINT: Final = "0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b"
SOURCE_COMMIT: Final = "678ea4f60ad5fd0aba0c8da6e5530959a1bcbb93"
LAMBDA_CODE_SHA256_BASE64: Final = "uvFStzLOhXS2ppJbrnq0/4ScG4PUE3B2xSxmglU/nUg="
AGENTCORE_BINDING_FINGERPRINT: Final = (
    "14b001854285121f34394ce9893c19481f0f844aa6058abc9daca57d86d7c0f6"
)
AGENTCORE_RUNTIME_ARN: Final = (
    "arn:aws:bedrock-agentcore:us-west-2:384627057108:runtime/mr_lister_phase6-4HoPmq2hCI"
)
AGENTCORE_QUALIFIER: Final = "phase6_v1_dev"
AGENTCORE_ENDPOINT_ARN: Final = f"{AGENTCORE_RUNTIME_ARN}/runtime-endpoint/{AGENTCORE_QUALIFIER}"
TABLE_NAME: Final = "mr-lister-phase6-dev"
ARTIFACT_BUCKET_NAME: Final = "mr-lister-phase6-artifacts-dev-384627057108-us-west-2"
RECOVERY_QUEUE_NAME: Final = "mr-lister-phase6-dev-execution-recovery-dlq"

_MAX_FRESHNESS = timedelta(minutes=15)
_MAX_EVIDENCE_BYTES = 256 * 1024
_GENERIC_ERROR = "Phase 6 core live-state evidence is invalid"
_HEX_64 = re.compile(r"^[a-f0-9]{64}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|\$\{[^}\r\n]+}|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)

_FUNCTION_NAMES: Final = (
    "mr-lister-phase6-dev-dispatcher",
    "mr-lister-phase6-dev-execution-recovery",
    "mr-lister-phase6-dev-preparation-dispatch",
    "mr-lister-phase6-dev-provider-draft",
    "mr-lister-phase6-dev-settlement",
    "mr-lister-phase6-dev-source-retention",
    "mr-lister-phase6-dev-terminal-cleanup",
)
_MAINTENANCE_FUNCTIONS: Final = frozenset(
    {
        "mr-lister-phase6-dev-execution-recovery",
        "mr-lister-phase6-dev-source-retention",
        "mr-lister-phase6-dev-terminal-cleanup",
    }
)
_STATE_MACHINE_NAMES: Final = (
    "mr-lister-phase6-dev-prepare",
    "mr-lister-phase6-dev-reconcile-product",
    "mr-lister-phase6-dev-refresh-economics",
    "mr-lister-phase6-dev-synchronize-product",
)
_LOG_GROUP_NAMES: Final = (
    "/aws/lambda/mr-lister-phase6-dev-dispatcher",
    "/aws/lambda/mr-lister-phase6-dev-execution-recovery",
    "/aws/lambda/mr-lister-phase6-dev-preparation-dispatch",
    "/aws/lambda/mr-lister-phase6-dev-provider-draft",
    "/aws/lambda/mr-lister-phase6-dev-settlement",
    "/aws/lambda/mr-lister-phase6-dev-source-retention",
    "/aws/lambda/mr-lister-phase6-dev-terminal-cleanup",
    "/aws/vendedlogs/states/mr-lister-phase6-dev-prepare",
    "/aws/vendedlogs/states/mr-lister-phase6-dev-reconcile-product",
    "/aws/vendedlogs/states/mr-lister-phase6-dev-refresh-economics",
    "/aws/vendedlogs/states/mr-lister-phase6-dev-synchronize-product",
)
_TRIGGERS: Final = (
    ("dispatcher-due-work-schedule", "eventbridge-rule"),
    ("dispatcher-operational-state-changes", "dynamodb-stream-mapping"),
    ("source-version-retention-schedule", "eventbridge-rule"),
    ("stuck-execution-recovery-schedule", "eventbridge-rule"),
    ("terminal-operational-cleanup-schedule", "eventbridge-rule"),
)
_STACK_TAGS: Final = {
    "DeploymentClass": "FOUNDATION_ONLY",
    "Environment": "dev",
    "Project": "MrLister",
}


class Phase6CoreLiveStateError(RuntimeError):
    """Value-free failure for malformed, stale, incomplete, or drifting evidence."""


@dataclass(frozen=True, slots=True)
class VerifiedPhase6CoreLiveState:
    """Closed identity returned only after every live-state assertion passes."""

    format: str
    mode: Phase6CoreLiveMode
    capture_time: datetime
    source_commit: str
    stack_id: str
    release_fingerprint: str
    lambda_code_sha256_base64: str
    agentcore_runtime_arn: str
    agentcore_endpoint_arn: str
    agentcore_binding_fingerprint: str
    canonical_sha256: str


@dataclass(frozen=True, slots=True)
class _ModeContract:
    readiness: str
    deployment_mode: str
    scaffold_only: bool
    triggers_enabled: bool
    maintenance_concurrency: int | None
    predecessor: Phase6CoreLiveMode | None


_MODE_CONTRACTS: Final[dict[Phase6CoreLiveMode, _ModeContract]] = {
    "staged": _ModeContract(
        readiness="CORE_RELEASE_BOUND_STAGED",
        deployment_mode="STAGED_FAIL_CLOSED",
        scaffold_only=True,
        triggers_enabled=False,
        maintenance_concurrency=0,
        predecessor=None,
    ),
    "capacity-released-inert": _ModeContract(
        readiness="CORE_CAPACITY_RELEASED_INERT",
        deployment_mode="CAPACITY_RELEASED_INERT",
        scaffold_only=True,
        triggers_enabled=False,
        maintenance_concurrency=None,
        predecessor="staged",
    ),
    "backend-active-draft-only": _ModeContract(
        readiness="CORE_RUNTIME_ACTIVE_DRAFT_ONLY",
        deployment_mode="ACTIVE_DRAFT_ONLY",
        scaffold_only=False,
        triggers_enabled=True,
        maintenance_concurrency=None,
        predecessor="capacity-released-inert",
    ),
}


def canonical_phase6_core_live_state(value: object) -> bytes:
    """Return the sole accepted byte representation for normalized v1 evidence."""

    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                indent=2,
                separators=(",", ": "),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except Exception:
        raise Phase6CoreLiveStateError(_GENERIC_ERROR) from None


def verify_phase6_core_live_state(
    evidence_path: Path,
    *,
    now: datetime | None = None,
    predecessor_evidence_path: Path | None = None,
    staged_ancestor_evidence_path: Path | None = None,
) -> VerifiedPhase6CoreLiveState:
    """Verify one canonical local observation without contacting AWS.

    Capacity-released evidence requires ``predecessor_evidence_path`` to be the canonical staged
    document it names. Backend-active evidence requires the canonical capacity-released document
    there and its staged predecessor at ``staged_ancestor_evidence_path``. Staged evidence is
    standalone and rejects either additional path.
    """

    try:
        current_time = datetime.now(UTC) if now is None else _utc_datetime(now)
        raw, document, mode, capture_time = _load_validated_document(
            evidence_path,
            now=current_time,
            require_fresh=True,
        )
        _validate_predecessor_documents(
            document,
            mode=mode,
            predecessor_evidence_path=predecessor_evidence_path,
            staged_ancestor_evidence_path=staged_ancestor_evidence_path,
            now=current_time,
        )
        return VerifiedPhase6CoreLiveState(
            format=EVIDENCE_FORMAT,
            mode=mode,
            capture_time=capture_time,
            source_commit=cast(str, document["source_commit"]),
            stack_id=STACK_ID,
            release_fingerprint=RELEASE_FINGERPRINT,
            lambda_code_sha256_base64=LAMBDA_CODE_SHA256_BASE64,
            agentcore_runtime_arn=AGENTCORE_RUNTIME_ARN,
            agentcore_endpoint_arn=AGENTCORE_ENDPOINT_ARN,
            agentcore_binding_fingerprint=AGENTCORE_BINDING_FINGERPRINT,
            canonical_sha256=sha256(raw).hexdigest(),
        )
    except Phase6CoreLiveStateError:
        raise
    except Exception:
        raise Phase6CoreLiveStateError(_GENERIC_ERROR) from None


def _load_validated_document(
    evidence_path: Path,
    *,
    now: datetime,
    require_fresh: bool,
) -> tuple[
    bytes,
    dict[str, object],
    Phase6CoreLiveMode,
    datetime,
]:
    raw = _read_canonical_document(evidence_path)
    document = json.loads(
        raw,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(document, dict) or canonical_phase6_core_live_state(document) != raw:
        raise ValueError
    _reject_placeholder_strings(document)
    mode = _live_mode(document.get("mode"))
    contract = _MODE_CONTRACTS[mode]
    capture_time = _capture_time(document.get("capture_time"))
    if require_fresh:
        _require_fresh(capture_time, now)
    _validate_document(
        document,
        mode,
        contract,
        capture_time,
        now,
        require_fresh=require_fresh,
    )
    return raw, document, mode, capture_time


def _validate_predecessor_documents(
    document: Mapping[str, object],
    *,
    mode: Phase6CoreLiveMode,
    predecessor_evidence_path: Path | None,
    staged_ancestor_evidence_path: Path | None,
    now: datetime,
) -> None:
    if mode == "staged":
        if predecessor_evidence_path is not None or staged_ancestor_evidence_path is not None:
            raise ValueError
        return
    if predecessor_evidence_path is None:
        raise ValueError
    predecessor_raw, predecessor, predecessor_mode, predecessor_time = _load_validated_document(
        predecessor_evidence_path,
        now=now,
        require_fresh=False,
    )
    _require_predecessor_link(
        document,
        predecessor_raw=predecessor_raw,
        predecessor_mode=predecessor_mode,
        predecessor_time=predecessor_time,
    )
    if mode == "capacity-released-inert":
        if predecessor_mode != "staged" or staged_ancestor_evidence_path is not None:
            raise ValueError
        return
    if predecessor_mode != "capacity-released-inert" or staged_ancestor_evidence_path is None:
        raise ValueError
    staged_raw, _staged, staged_mode, staged_time = _load_validated_document(
        staged_ancestor_evidence_path,
        now=now,
        require_fresh=False,
    )
    if staged_mode != "staged":
        raise ValueError
    _require_predecessor_link(
        predecessor,
        predecessor_raw=staged_raw,
        predecessor_mode=staged_mode,
        predecessor_time=staged_time,
    )


def _require_predecessor_link(
    document: Mapping[str, object],
    *,
    predecessor_raw: bytes,
    predecessor_mode: Phase6CoreLiveMode,
    predecessor_time: datetime,
) -> None:
    acceptance = _mapping(document, "predecessor_evidence")
    if (
        acceptance.get("mode") != predecessor_mode
        or acceptance.get("evidence_sha256") != sha256(predecessor_raw).hexdigest()
        or acceptance.get("captured_at") != predecessor_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    ):
        raise ValueError


def _validate_document(
    document: Mapping[str, object],
    mode: Phase6CoreLiveMode,
    contract: _ModeContract,
    capture_time: datetime,
    now: datetime,
    *,
    require_fresh: bool,
) -> None:
    _exact_keys(
        document,
        {
            "account_id",
            "agentcore",
            "capture_time",
            "format",
            "foundation",
            "lambda",
            "logs",
            "mode",
            "predecessor_evidence",
            "preflight",
            "public_web_surface",
            "recovery_queue",
            "region",
            "safety",
            "source_commit",
            "stack",
            "step_functions",
            "triggers",
        },
    )
    if (
        document.get("format") != EVIDENCE_FORMAT
        or document.get("account_id") != ACCOUNT_ID
        or document.get("region") != REGION
        or document.get("mode") != mode
        or document.get("source_commit") != SOURCE_COMMIT
    ):
        raise ValueError
    _validate_stack(_mapping(document, "stack"), contract)
    _validate_lambda(_mapping(document, "lambda"), contract)
    _validate_triggers(_mapping(document, "triggers"), contract)
    _validate_state_machines(_mapping(document, "step_functions"))
    _validate_logs(_mapping(document, "logs"))
    _validate_foundation(_mapping(document, "foundation"))
    _validate_recovery_queue(_mapping(document, "recovery_queue"))
    _validate_agentcore(_mapping(document, "agentcore"))
    _validate_public_web(_mapping(document, "public_web_surface"))
    _validate_safety(_mapping(document, "safety"), mode)
    _validate_predecessor_evidence(
        document.get("predecessor_evidence"),
        contract,
        capture_time,
    )
    _validate_preflight(
        document.get("preflight"),
        capture_time,
        now,
        require_fresh=require_fresh,
    )


def _validate_stack(stack: Mapping[str, object], contract: _ModeContract) -> None:
    _exact_keys(
        stack,
        {
            "deployment_mode",
            "id",
            "live_resource_count",
            "name",
            "non_complete_resource_count",
            "original_template_resource_count",
            "readiness",
            "service_role_arn",
            "status",
            "tags",
            "termination_protection",
        },
    )
    if (
        stack.get("name") != STACK_NAME
        or stack.get("id") != STACK_ID
        or stack.get("status") != "UPDATE_COMPLETE"
        or stack.get("service_role_arn") != SERVICE_ROLE_ARN
        or stack.get("termination_protection") is not True
        or stack.get("readiness") != contract.readiness
        or stack.get("deployment_mode") != contract.deployment_mode
        or _exact_int(stack.get("original_template_resource_count")) != 40
        or _exact_int(stack.get("live_resource_count")) != 47
        or _exact_int(stack.get("non_complete_resource_count")) != 0
        or _mapping(stack, "tags") != _STACK_TAGS
    ):
        raise ValueError


def _validate_lambda(value: Mapping[str, object], contract: _ModeContract) -> None:
    _exact_keys(value, {"function_count", "functions", "preparation_agentcore_binding"})
    functions = _list(value, "functions")
    if _exact_int(value.get("function_count")) != 7 or len(functions) != 7:
        raise ValueError
    expected_functions: list[dict[str, object]] = []
    for name in _FUNCTION_NAMES:
        expected_functions.append(
            {
                "architecture": "arm64",
                "code_sha256_base64": LAMBDA_CODE_SHA256_BASE64,
                "last_update_status": "Successful",
                "name": name,
                "release_fingerprint": RELEASE_FINGERPRINT,
                "reserved_concurrency": (
                    contract.maintenance_concurrency if name in _MAINTENANCE_FUNCTIONS else None
                ),
                "runtime": "python3.12",
                "scaffold_only": contract.scaffold_only,
                "state": "Active",
            }
        )
    if functions != expected_functions:
        raise ValueError
    for function in functions:
        normalized = _mapping_value(function)
        if type(normalized.get("scaffold_only")) is not bool:
            raise ValueError
        concurrency = normalized.get("reserved_concurrency")
        if concurrency is not None:
            _exact_int(concurrency)
    try:
        decoded_code_hash = base64.b64decode(LAMBDA_CODE_SHA256_BASE64, validate=True)
    except Exception:
        raise ValueError from None
    if len(decoded_code_hash) != 32:
        raise ValueError
    binding = _mapping(value, "preparation_agentcore_binding")
    _exact_keys(
        binding,
        {
            "binding_fingerprint",
            "live_binding_matches_reviewed_template",
            "qualifier",
            "runtime_version",
        },
    )
    if binding != {
        "binding_fingerprint": AGENTCORE_BINDING_FINGERPRINT,
        "live_binding_matches_reviewed_template": True,
        "qualifier": AGENTCORE_QUALIFIER,
        "runtime_version": "1",
    }:
        raise ValueError
    if binding.get("live_binding_matches_reviewed_template") is not True:
        raise ValueError


def _validate_triggers(value: Mapping[str, object], contract: _ModeContract) -> None:
    _exact_keys(value, {"trigger_count", "triggers"})
    triggers = _list(value, "triggers")
    expected_state = "ENABLED" if contract.triggers_enabled else "DISABLED"
    expected = [
        {"id": identifier, "state": expected_state, "type": trigger_type}
        for identifier, trigger_type in _TRIGGERS
    ]
    if _exact_int(value.get("trigger_count")) != 5 or triggers != expected:
        raise ValueError


def _validate_state_machines(value: Mapping[str, object]) -> None:
    _exact_keys(value, {"state_machine_count", "state_machines"})
    machines = _list(value, "state_machines")
    expected = [
        {
            "include_execution_data": False,
            "logging_level": "ERROR",
            "name": name,
            "running_execution_count": 0,
            "status": "ACTIVE",
            "type": "STANDARD",
        }
        for name in _STATE_MACHINE_NAMES
    ]
    if _exact_int(value.get("state_machine_count")) != 4 or machines != expected:
        raise ValueError
    for machine in machines:
        normalized = _mapping_value(machine)
        if (
            _exact_int(normalized.get("running_execution_count")) != 0
            or normalized.get("include_execution_data") is not False
        ):
            raise ValueError


def _validate_logs(value: Mapping[str, object]) -> None:
    _exact_keys(value, {"log_group_count", "log_groups"})
    groups = _list(value, "log_groups")
    expected = [{"name": name, "retention_in_days": 14} for name in _LOG_GROUP_NAMES]
    if _exact_int(value.get("log_group_count")) != 11 or groups != expected:
        raise ValueError
    for group in groups:
        if _exact_int(_mapping_value(group).get("retention_in_days")) != 14:
            raise ValueError


def _validate_foundation(value: Mapping[str, object]) -> None:
    _exact_keys(value, {"artifact_bucket", "table"})
    table = _mapping(value, "table")
    _exact_keys(
        table,
        {
            "billing_mode",
            "continuous_backups",
            "item_count",
            "name",
            "point_in_time_recovery",
            "sse",
            "status",
            "stream_enabled",
            "stream_view_type",
            "ttl_attribute",
            "ttl_status",
        },
    )
    expected_table = {
        "billing_mode": "PAY_PER_REQUEST",
        "continuous_backups": "ENABLED",
        "item_count": 0,
        "name": TABLE_NAME,
        "point_in_time_recovery": "ENABLED",
        "sse": "ENABLED",
        "status": "ACTIVE",
        "stream_enabled": True,
        "stream_view_type": "KEYS_ONLY",
        "ttl_attribute": "expires_at",
        "ttl_status": "ENABLED",
    }
    if (
        table != expected_table
        or _exact_int(table.get("item_count")) != 0
        or table.get("stream_enabled") is not True
    ):
        raise ValueError
    bucket = _mapping(value, "artifact_bucket")
    _exact_keys(
        bucket,
        {
            "all_public_access_blocks_enabled",
            "bucket_policy_is_public",
            "cors_allowed_methods",
            "cors_allowed_origin",
            "name",
        },
    )
    if bucket != {
        "all_public_access_blocks_enabled": True,
        "bucket_policy_is_public": False,
        "cors_allowed_methods": ["GET", "POST"],
        "cors_allowed_origin": "https://massskutiny.com",
        "name": ARTIFACT_BUCKET_NAME,
    }:
        raise ValueError
    if (
        bucket.get("all_public_access_blocks_enabled") is not True
        or bucket.get("bucket_policy_is_public") is not False
    ):
        raise ValueError


def _validate_recovery_queue(value: Mapping[str, object]) -> None:
    _exact_keys(
        value,
        {
            "delayed_messages",
            "in_flight_messages",
            "message_retention_seconds",
            "name",
            "queue_count",
            "sqs_managed_sse",
            "visible_messages",
        },
    )
    expected = {
        "delayed_messages": 0,
        "in_flight_messages": 0,
        "message_retention_seconds": 1_209_600,
        "name": RECOVERY_QUEUE_NAME,
        "queue_count": 1,
        "sqs_managed_sse": True,
        "visible_messages": 0,
    }
    if value != expected:
        raise ValueError
    if value.get("sqs_managed_sse") is not True:
        raise ValueError
    for key in (
        "delayed_messages",
        "in_flight_messages",
        "message_retention_seconds",
        "queue_count",
        "visible_messages",
    ):
        _exact_int(value.get(key))


def _validate_agentcore(value: Mapping[str, object]) -> None:
    _exact_keys(
        value,
        {
            "binding_fingerprint",
            "endpoint_arn",
            "endpoint_failure_reason",
            "endpoint_live_version",
            "endpoint_qualifier",
            "endpoint_status",
            "endpoint_target_version",
            "runtime_arn",
            "runtime_status",
            "runtime_version",
        },
    )
    if value != {
        "binding_fingerprint": AGENTCORE_BINDING_FINGERPRINT,
        "endpoint_arn": AGENTCORE_ENDPOINT_ARN,
        "endpoint_failure_reason": None,
        "endpoint_live_version": "1",
        "endpoint_qualifier": AGENTCORE_QUALIFIER,
        "endpoint_status": "READY",
        "endpoint_target_version": None,
        "runtime_arn": AGENTCORE_RUNTIME_ARN,
        "runtime_status": "READY",
        "runtime_version": "1",
    }:
        raise ValueError


def _validate_public_web(value: Mapping[str, object]) -> None:
    _exact_keys(
        value,
        {"acm", "api_gateway", "cloudfront", "cognito", "route53", "stack_resource_count"},
    )
    if (
        value
        != {
            "acm": False,
            "api_gateway": False,
            "cloudfront": False,
            "cognito": False,
            "route53": False,
            "stack_resource_count": 0,
        }
        or _exact_int(value.get("stack_resource_count")) != 0
    ):
        raise ValueError
    if any(
        value.get(key) is not False
        for key in ("acm", "api_gateway", "cloudfront", "cognito", "route53")
    ):
        raise ValueError


def _validate_safety(value: Mapping[str, object], mode: Phase6CoreLiveMode) -> None:
    _exact_keys(
        value,
        {
            "backend_runtime_activated",
            "draft_only_provider_authority",
            "fulfillment_authorized",
            "order_authorized",
            "publication_authorized",
            "web_traffic_activated",
        },
    )
    active = mode == "backend-active-draft-only"
    if value != {
        "backend_runtime_activated": active,
        "draft_only_provider_authority": active,
        "fulfillment_authorized": False,
        "order_authorized": False,
        "publication_authorized": False,
        "web_traffic_activated": False,
    }:
        raise ValueError
    if (
        type(value.get("backend_runtime_activated")) is not bool
        or type(value.get("draft_only_provider_authority")) is not bool
    ):
        raise ValueError
    if any(
        value.get(key) is not False
        for key in (
            "fulfillment_authorized",
            "order_authorized",
            "publication_authorized",
            "web_traffic_activated",
        )
    ):
        raise ValueError


def _validate_predecessor_evidence(
    value: object,
    contract: _ModeContract,
    capture_time: datetime,
) -> None:
    if contract.predecessor is None:
        if value is not None:
            raise ValueError
        return
    if not isinstance(value, Mapping):
        raise ValueError
    _exact_keys(
        value,
        {
            "captured_at",
            "evidence_sha256",
            "mode",
        },
    )
    if (
        value.get("mode") != contract.predecessor
        or _HEX_64.fullmatch(_exact_string(value.get("evidence_sha256"))) is None
        or value.get("evidence_sha256") == "0" * 64
    ):
        raise ValueError
    predecessor_time = _capture_time(value.get("captured_at"))
    if predecessor_time > capture_time:
        raise ValueError


def _validate_preflight(
    value: object,
    capture_time: datetime,
    now: datetime,
    *,
    require_fresh: bool,
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError
    _exact_keys(value, {"captured_at", "running_executions", "table_scan"})
    preflight_time = _capture_time(value.get("captured_at"))
    if preflight_time > capture_time or capture_time - preflight_time > _MAX_FRESHNESS:
        raise ValueError
    if require_fresh:
        _require_fresh(preflight_time, now)
    scan = _mapping(value, "table_scan")
    _exact_keys(scan, {"request", "response"})
    request = _mapping(scan, "request")
    response = _mapping(scan, "response")
    _exact_keys(request, {"ConsistentRead", "Select", "TableName"})
    _exact_keys(response, {"Count", "LastEvaluatedKey", "ScannedCount"})
    if (
        request
        != {
            "ConsistentRead": True,
            "Select": "COUNT",
            "TableName": TABLE_NAME,
        }
        or request.get("ConsistentRead") is not True
    ):
        raise ValueError
    if (
        _exact_int(response.get("Count")) != 0
        or _exact_int(response.get("ScannedCount")) != 0
        or response.get("LastEvaluatedKey") is not None
    ):
        raise ValueError
    running = value.get("running_executions")
    if not isinstance(running, list):
        raise ValueError
    expected_running = [
        {"name": name, "running_execution_count": 0} for name in _STATE_MACHINE_NAMES
    ]
    if running != expected_running:
        raise ValueError
    for machine in running:
        if _exact_int(_mapping_value(machine).get("running_execution_count")) != 0:
            raise ValueError


def _read_canonical_document(path: Path) -> bytes:
    if not isinstance(path, Path) or any(
        candidate.is_symlink() for candidate in (path, *path.parents)
    ):
        raise ValueError
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError
    raw = resolved.read_bytes()
    if not raw or len(raw) > _MAX_EVIDENCE_BYTES or b"\x00" in raw:
        raise ValueError
    return raw


def _capture_time(value: object) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _require_fresh(captured: datetime, now: datetime) -> None:
    if captured > now or now - captured > _MAX_FRESHNESS:
        raise ValueError


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    if value.utcoffset() != timedelta(0):
        raise ValueError
    return value.astimezone(UTC)


def _live_mode(value: object) -> Phase6CoreLiveMode:
    if value not in _MODE_CONTRACTS:
        raise ValueError
    return cast(Phase6CoreLiveMode, value)


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ValueError
    return nested


def _mapping_value(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _list(value: Mapping[str, object], key: str) -> list[object]:
    nested = value.get(key)
    if not isinstance(nested, list):
        raise ValueError
    return nested


def _exact_keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected or not all(isinstance(key, str) for key in value):
        raise ValueError


def _exact_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return cast(int, value)


def _exact_string(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError
    return value


def _reject_placeholder_strings(value: object) -> None:
    if isinstance(value, str):
        if _PLACEHOLDER.search(value) is not None or "\x00" in value:
            raise ValueError
        return
    if isinstance(value, list):
        for nested in value:
            _reject_placeholder_strings(nested)
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            _reject_placeholder_strings(key)
            _reject_placeholder_strings(nested)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError
        value[key] = nested
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--predecessor-evidence", type=Path)
    parser.add_argument("--staged-ancestor-evidence", type=Path)
    arguments = parser.parse_args()
    try:
        verified = verify_phase6_core_live_state(
            arguments.evidence,
            predecessor_evidence_path=arguments.predecessor_evidence,
            staged_ancestor_evidence_path=arguments.staged_ancestor_evidence,
        )
        output = asdict(verified)
        output["capture_time"] = verified.capture_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(json.dumps(output, allow_nan=False, separators=(",", ":"), sort_keys=True))
    except Phase6CoreLiveStateError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
