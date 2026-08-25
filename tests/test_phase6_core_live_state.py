from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

import tools.verify_phase6_core_live_state as live
from tools.verify_phase6_core_live_state import (
    Phase6CoreLiveStateError,
    canonical_phase6_core_live_state,
    verify_phase6_core_live_state,
)

NOW = datetime(2026, 8, 25, 23, 0, 0, tzinfo=UTC)
CAPTURE_TIME = "2026-08-25T22:59:00Z"
PREDECESSOR_TIME = "2026-08-25T22:57:00Z"
SOURCE_COMMIT = live.SOURCE_COMMIT
PREDECESSOR_SHA = "b" * 64

FUNCTION_NAMES = (
    "mr-lister-phase6-dev-dispatcher",
    "mr-lister-phase6-dev-execution-recovery",
    "mr-lister-phase6-dev-preparation-dispatch",
    "mr-lister-phase6-dev-provider-draft",
    "mr-lister-phase6-dev-settlement",
    "mr-lister-phase6-dev-source-retention",
    "mr-lister-phase6-dev-terminal-cleanup",
)
MAINTENANCE_FUNCTIONS = frozenset(
    {
        "mr-lister-phase6-dev-execution-recovery",
        "mr-lister-phase6-dev-source-retention",
        "mr-lister-phase6-dev-terminal-cleanup",
    }
)
STATE_MACHINE_NAMES = (
    "mr-lister-phase6-dev-prepare",
    "mr-lister-phase6-dev-reconcile-product",
    "mr-lister-phase6-dev-refresh-economics",
    "mr-lister-phase6-dev-synchronize-product",
)
LOG_GROUP_NAMES = (
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
TRIGGERS = (
    ("dispatcher-due-work-schedule", "eventbridge-rule"),
    ("dispatcher-operational-state-changes", "dynamodb-stream-mapping"),
    ("source-version-retention-schedule", "eventbridge-rule"),
    ("stuck-execution-recovery-schedule", "eventbridge-rule"),
    ("terminal-operational-cleanup-schedule", "eventbridge-rule"),
)


def _mode_values(mode: str) -> tuple[str, str, bool, bool, int | None, str | None]:
    if mode == "staged":
        return (
            "CORE_RELEASE_BOUND_STAGED",
            "STAGED_FAIL_CLOSED",
            True,
            False,
            0,
            None,
        )
    if mode == "capacity-released-inert":
        return (
            "CORE_CAPACITY_RELEASED_INERT",
            "CAPACITY_RELEASED_INERT",
            True,
            False,
            None,
            "staged",
        )
    if mode == "backend-active-draft-only":
        return (
            "CORE_RUNTIME_ACTIVE_DRAFT_ONLY",
            "ACTIVE_DRAFT_ONLY",
            False,
            True,
            None,
            "capacity-released-inert",
        )
    raise AssertionError("unsupported test mode")


def _predecessor_evidence(mode: str | None) -> dict[str, object] | None:
    if mode is None:
        return None
    return {
        "captured_at": PREDECESSOR_TIME,
        "evidence_sha256": PREDECESSOR_SHA,
        "mode": mode,
    }


def _preflight(captured_at: str = CAPTURE_TIME) -> dict[str, object]:
    return {
        "captured_at": captured_at,
        "running_executions": [
            {"name": name, "running_execution_count": 0} for name in STATE_MACHINE_NAMES
        ],
        "table_scan": {
            "request": {
                "ConsistentRead": True,
                "Select": "COUNT",
                "TableName": "mr-lister-phase6-dev",
            },
            "response": {"Count": 0, "LastEvaluatedKey": None, "ScannedCount": 0},
        },
    }


def _document(mode: str = "staged") -> dict[str, object]:
    readiness, deployment_mode, scaffold, triggers_enabled, concurrency, predecessor = _mode_values(
        mode
    )
    return {
        "account_id": live.ACCOUNT_ID,
        "agentcore": {
            "binding_fingerprint": live.AGENTCORE_BINDING_FINGERPRINT,
            "endpoint_arn": live.AGENTCORE_ENDPOINT_ARN,
            "endpoint_failure_reason": None,
            "endpoint_live_version": "1",
            "endpoint_qualifier": live.AGENTCORE_QUALIFIER,
            "endpoint_status": "READY",
            "endpoint_target_version": None,
            "runtime_arn": live.AGENTCORE_RUNTIME_ARN,
            "runtime_status": "READY",
            "runtime_version": "1",
        },
        "capture_time": CAPTURE_TIME,
        "format": live.EVIDENCE_FORMAT,
        "foundation": {
            "artifact_bucket": {
                "all_public_access_blocks_enabled": True,
                "bucket_policy_is_public": False,
                "cors_allowed_methods": ["GET", "POST"],
                "cors_allowed_origin": "https://massskutiny.com",
                "name": live.ARTIFACT_BUCKET_NAME,
            },
            "table": {
                "billing_mode": "PAY_PER_REQUEST",
                "continuous_backups": "ENABLED",
                "item_count": 0,
                "name": live.TABLE_NAME,
                "point_in_time_recovery": "ENABLED",
                "sse": "ENABLED",
                "status": "ACTIVE",
                "stream_enabled": True,
                "stream_view_type": "KEYS_ONLY",
                "ttl_attribute": "expires_at",
                "ttl_status": "ENABLED",
            },
        },
        "lambda": {
            "function_count": 7,
            "functions": [
                {
                    "architecture": "arm64",
                    "code_sha256_base64": live.LAMBDA_CODE_SHA256_BASE64,
                    "last_update_status": "Successful",
                    "name": name,
                    "release_fingerprint": live.RELEASE_FINGERPRINT,
                    "reserved_concurrency": (
                        concurrency if name in MAINTENANCE_FUNCTIONS else None
                    ),
                    "runtime": "python3.12",
                    "scaffold_only": scaffold,
                    "state": "Active",
                }
                for name in FUNCTION_NAMES
            ],
            "preparation_agentcore_binding": {
                "binding_fingerprint": live.AGENTCORE_BINDING_FINGERPRINT,
                "live_binding_matches_reviewed_template": True,
                "qualifier": live.AGENTCORE_QUALIFIER,
                "runtime_version": "1",
            },
        },
        "logs": {
            "log_group_count": 11,
            "log_groups": [{"name": name, "retention_in_days": 14} for name in LOG_GROUP_NAMES],
        },
        "mode": mode,
        "predecessor_evidence": _predecessor_evidence(predecessor),
        "preflight": _preflight(),
        "public_web_surface": {
            "acm": False,
            "api_gateway": False,
            "cloudfront": False,
            "cognito": False,
            "route53": False,
            "stack_resource_count": 0,
        },
        "recovery_queue": {
            "delayed_messages": 0,
            "in_flight_messages": 0,
            "message_retention_seconds": 1_209_600,
            "name": live.RECOVERY_QUEUE_NAME,
            "queue_count": 1,
            "sqs_managed_sse": True,
            "visible_messages": 0,
        },
        "region": live.REGION,
        "safety": {
            "backend_runtime_activated": triggers_enabled,
            "draft_only_provider_authority": triggers_enabled,
            "fulfillment_authorized": False,
            "order_authorized": False,
            "publication_authorized": False,
            "web_traffic_activated": False,
        },
        "source_commit": SOURCE_COMMIT,
        "stack": {
            "deployment_mode": deployment_mode,
            "id": live.STACK_ID,
            "live_resource_count": 47,
            "name": live.STACK_NAME,
            "non_complete_resource_count": 0,
            "original_template_resource_count": 40,
            "readiness": readiness,
            "service_role_arn": live.SERVICE_ROLE_ARN,
            "status": "UPDATE_COMPLETE",
            "tags": {
                "DeploymentClass": "FOUNDATION_ONLY",
                "Environment": "dev",
                "Project": "MrLister",
            },
            "termination_protection": True,
        },
        "step_functions": {
            "state_machine_count": 4,
            "state_machines": [
                {
                    "include_execution_data": False,
                    "logging_level": "ERROR",
                    "name": name,
                    "running_execution_count": 0,
                    "status": "ACTIVE",
                    "type": "STANDARD",
                }
                for name in STATE_MACHINE_NAMES
            ],
        },
        "triggers": {
            "trigger_count": 5,
            "triggers": [
                {
                    "id": identifier,
                    "state": "ENABLED" if triggers_enabled else "DISABLED",
                    "type": trigger_type,
                }
                for identifier, trigger_type in TRIGGERS
            ],
        },
    }


def _write(tmp_path: Path, document: object, *, name: str = "live-state.json") -> Path:
    path = tmp_path / name
    path.write_bytes(canonical_phase6_core_live_state(document))
    return path


def _set_capture_time(document: dict[str, object], captured_at: str) -> None:
    document["capture_time"] = captured_at
    preflight = document.get("preflight")
    assert isinstance(preflight, dict)
    preflight["captured_at"] = captured_at


def _link_default_predecessor(document: dict[str, object], predecessor_path: Path) -> None:
    acceptance = document.get("predecessor_evidence")
    if not isinstance(acceptance, dict):
        return
    if acceptance.get("evidence_sha256") == PREDECESSOR_SHA:
        acceptance["evidence_sha256"] = sha256(predecessor_path.read_bytes()).hexdigest()


def _write_validation_set(
    tmp_path: Path,
    document: dict[str, object],
) -> tuple[Path, Path | None, Path | None]:
    mode = document.get("mode")
    if mode == "staged":
        return _write(tmp_path, document), None, None
    if mode == "capacity-released-inert":
        staged = _document("staged")
        staged["capture_time"] = PREDECESSOR_TIME
        staged["preflight"] = _preflight(PREDECESSOR_TIME)
        predecessor_path = _write(tmp_path, staged, name="predecessor-staged.json")
        _link_default_predecessor(document, predecessor_path)
        return _write(tmp_path, document), predecessor_path, None
    if mode == "backend-active-draft-only":
        staged = _document("staged")
        staged["capture_time"] = "2026-08-25T22:55:00Z"
        staged["preflight"] = _preflight("2026-08-25T22:55:00Z")
        staged_path = _write(tmp_path, staged, name="ancestor-staged.json")
        capacity = _document("capacity-released-inert")
        capacity["capture_time"] = PREDECESSOR_TIME
        capacity["preflight"] = _preflight(PREDECESSOR_TIME)
        capacity_acceptance = capacity["predecessor_evidence"]
        assert isinstance(capacity_acceptance, dict)
        capacity_acceptance["captured_at"] = staged["capture_time"]
        _link_default_predecessor(capacity, staged_path)
        predecessor_path = _write(tmp_path, capacity, name="predecessor-capacity.json")
        _link_default_predecessor(document, predecessor_path)
        return _write(tmp_path, document), predecessor_path, staged_path
    return _write(tmp_path, document), None, None


def _verify(tmp_path: Path, document: dict[str, object]):
    path, predecessor, ancestor = _write_validation_set(tmp_path, document)
    return verify_phase6_core_live_state(
        path,
        now=NOW,
        predecessor_evidence_path=predecessor,
        staged_ancestor_evidence_path=ancestor,
    )


def _reject(tmp_path: Path, document: object) -> None:
    assert isinstance(document, dict)
    path, predecessor, ancestor = _write_validation_set(tmp_path, document)
    with pytest.raises(Phase6CoreLiveStateError, match="core live-state evidence is invalid"):
        verify_phase6_core_live_state(
            path,
            now=NOW,
            predecessor_evidence_path=predecessor,
            staged_ancestor_evidence_path=ancestor,
        )


@pytest.mark.parametrize(
    ("mode", "scaffold_only", "trigger_state", "maintenance_concurrency"),
    [
        ("staged", True, "DISABLED", 0),
        ("capacity-released-inert", True, "DISABLED", None),
        ("backend-active-draft-only", False, "ENABLED", None),
    ],
)
def test_accepts_each_exact_mode_and_returns_frozen_canonical_record(
    tmp_path: Path,
    mode: str,
    scaffold_only: bool,
    trigger_state: str,
    maintenance_concurrency: int | None,
) -> None:
    document = _document(mode)
    verified = _verify(tmp_path, document)
    path = tmp_path / "live-state.json"

    assert verified.mode == mode
    assert verified.capture_time == datetime(2026, 8, 25, 22, 59, tzinfo=UTC)
    assert verified.stack_id == live.STACK_ID
    assert verified.release_fingerprint == live.RELEASE_FINGERPRINT
    assert verified.agentcore_endpoint_arn == live.AGENTCORE_ENDPOINT_ARN
    assert verified.canonical_sha256 == sha256(path.read_bytes()).hexdigest()
    assert all(
        function["scaffold_only"] is scaffold_only
        for function in document["lambda"]["functions"]  # type: ignore[index]
    )
    assert all(
        trigger["state"] == trigger_state
        for trigger in document["triggers"]["triggers"]  # type: ignore[index]
    )
    maintenance = {
        function["name"]: function["reserved_concurrency"]
        for function in document["lambda"]["functions"]  # type: ignore[index]
        if function["name"] in MAINTENANCE_FUNCTIONS
    }
    assert set(maintenance.values()) == {maintenance_concurrency}
    with pytest.raises(FrozenInstanceError):
        verified.mode = "staged"  # type: ignore[misc]


@pytest.mark.parametrize("mode", ["capacity-released-inert", "backend-active-draft-only"])
def test_successor_modes_require_strong_empty_current_preflight(tmp_path: Path, mode: str) -> None:
    document = _document(mode)
    preflight = document["preflight"]
    assert isinstance(preflight, dict)
    scan = preflight["table_scan"]
    assert isinstance(scan, dict)
    assert scan["request"] == {
        "ConsistentRead": True,
        "Select": "COUNT",
        "TableName": live.TABLE_NAME,
    }
    assert scan["response"] == {"Count": 0, "LastEvaluatedKey": None, "ScannedCount": 0}
    assert "Items" not in scan["response"]
    assert all(item["running_execution_count"] == 0 for item in preflight["running_executions"])
    _verify(tmp_path, document)


@pytest.mark.parametrize(
    "key",
    ["account_id", "predecessor_evidence", "preflight", "region", "source_commit"],
)
def test_rejects_missing_and_extra_top_level_members(tmp_path: Path, key: str) -> None:
    missing = _document()
    missing.pop(key)
    _reject(tmp_path, missing)

    extra = _document()
    extra[f"extra_{key}"] = "forbidden"
    _reject(tmp_path, extra)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("stack", "tags"),
        ("lambda", "functions"),
        ("triggers", "triggers"),
        ("step_functions", "state_machines"),
        ("logs", "log_groups"),
        ("foundation", "table"),
        ("recovery_queue", "visible_messages"),
        ("agentcore", "runtime_arn"),
        ("public_web_surface", "cloudfront"),
        ("preflight", "table_scan"),
        ("safety", "publication_authorized"),
    ],
)
def test_rejects_missing_or_additional_nested_members(
    tmp_path: Path, section: str, key: str
) -> None:
    missing = _document()
    nested = missing[section]
    assert isinstance(nested, dict)
    nested.pop(key)
    _reject(tmp_path, missing)

    extra = _document()
    nested = extra[section]
    assert isinstance(nested, dict)
    nested["unexpected"] = None
    _reject(tmp_path, extra)


@pytest.mark.parametrize(
    ("section", "key", "bad_value"),
    [
        ("stack", "original_template_resource_count", True),
        ("stack", "termination_protection", 1),
        ("lambda", "function_count", True),
        ("triggers", "trigger_count", False),
        ("step_functions", "state_machine_count", True),
        ("logs", "log_group_count", True),
        ("recovery_queue", "queue_count", True),
        ("recovery_queue", "sqs_managed_sse", 1),
        ("public_web_surface", "stack_resource_count", False),
        ("public_web_surface", "cloudfront", 0),
        ("safety", "backend_runtime_activated", 0),
    ],
)
def test_rejects_boolean_integer_coercion(
    tmp_path: Path, section: str, key: str, bad_value: object
) -> None:
    document = _document()
    nested = document[section]
    assert isinstance(nested, dict)
    nested[key] = bad_value
    _reject(tmp_path, document)


def test_rejects_boolean_integer_coercion_inside_repeated_records(tmp_path: Path) -> None:
    document = _document("capacity-released-inert")
    functions = document["lambda"]["functions"]  # type: ignore[index]
    functions[0]["scaffold_only"] = 1
    _reject(tmp_path, document)

    document = _document("capacity-released-inert")
    machines = document["step_functions"]["state_machines"]  # type: ignore[index]
    machines[0]["running_execution_count"] = False
    _reject(tmp_path, document)

    document = _document("capacity-released-inert")
    preflight = document["preflight"]
    assert isinstance(preflight, dict)
    preflight["table_scan"]["request"]["ConsistentRead"] = 1  # type: ignore[index]
    _reject(tmp_path, document)


@pytest.mark.parametrize(
    ("section", "key", "bad_value"),
    [
        ("stack", "name", "other-stack"),
        ("stack", "id", live.STACK_ID.replace("f3456970", "00000000")),
        ("stack", "service_role_arn", live.SERVICE_ROLE_ARN + "-broad"),
        ("stack", "status", "UPDATE_ROLLBACK_COMPLETE"),
        ("stack", "live_resource_count", 46),
        ("stack", "non_complete_resource_count", 1),
    ],
)
def test_rejects_stack_identity_or_resource_drift(
    tmp_path: Path, section: str, key: str, bad_value: object
) -> None:
    document = _document()
    nested = document[section]
    assert isinstance(nested, dict)
    nested[key] = bad_value
    _reject(tmp_path, document)


def test_rejects_tag_drift_or_termination_protection_loss(tmp_path: Path) -> None:
    document = _document()
    document["stack"]["tags"]["DeploymentClass"] = "RUNTIME_UPDATE"  # type: ignore[index]
    _reject(tmp_path, document)

    document = _document()
    document["stack"]["termination_protection"] = False  # type: ignore[index]
    _reject(tmp_path, document)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("state", "Inactive"),
        ("last_update_status", "Failed"),
        ("runtime", "python3.13"),
        ("architecture", "x86_64"),
        ("code_sha256_base64", "A" * 44),
        ("release_fingerprint", "c" * 64),
    ],
)
def test_rejects_lambda_runtime_or_release_drift(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    document = _document()
    document["lambda"]["functions"][0][field] = bad_value  # type: ignore[index]
    _reject(tmp_path, document)


def test_rejects_lambda_inventory_and_agentcore_binding_drift(tmp_path: Path) -> None:
    document = _document()
    document["lambda"]["functions"].reverse()  # type: ignore[index]
    _reject(tmp_path, document)

    document = _document()
    document["lambda"]["preparation_agentcore_binding"]["binding_fingerprint"] = "c" * 64  # type: ignore[index]
    _reject(tmp_path, document)


@pytest.mark.parametrize(
    ("mode", "scaffold", "concurrency", "trigger_state"),
    [
        ("staged", False, 0, "DISABLED"),
        ("staged", True, None, "DISABLED"),
        ("staged", True, 0, "ENABLED"),
        ("capacity-released-inert", False, None, "DISABLED"),
        ("capacity-released-inert", True, 0, "DISABLED"),
        ("capacity-released-inert", True, None, "ENABLED"),
        ("backend-active-draft-only", True, None, "ENABLED"),
        ("backend-active-draft-only", False, 0, "ENABLED"),
        ("backend-active-draft-only", False, None, "DISABLED"),
    ],
)
def test_rejects_mode_specific_scaffold_capacity_or_trigger_drift(
    tmp_path: Path,
    mode: str,
    scaffold: bool,
    concurrency: int | None,
    trigger_state: str,
) -> None:
    document = _document(mode)
    for function in document["lambda"]["functions"]:  # type: ignore[index]
        function["scaffold_only"] = scaffold
        if function["name"] in MAINTENANCE_FUNCTIONS:
            function["reserved_concurrency"] = concurrency
    for trigger in document["triggers"]["triggers"]:  # type: ignore[index]
        trigger["state"] = trigger_state
    _reject(tmp_path, document)


def test_rejects_state_machine_log_or_queue_drift(tmp_path: Path) -> None:
    document = _document()
    document["step_functions"]["state_machines"][0]["status"] = "UPDATING"  # type: ignore[index]
    _reject(tmp_path, document)

    document = _document()
    document["step_functions"]["state_machines"][0]["type"] = "EXPRESS"  # type: ignore[index]
    _reject(tmp_path, document)

    document = _document()
    document["logs"]["log_groups"][0]["retention_in_days"] = 30  # type: ignore[index]
    _reject(tmp_path, document)

    document = _document()
    document["recovery_queue"]["visible_messages"] = 1  # type: ignore[index]
    _reject(tmp_path, document)


def test_rejects_foundation_agentcore_or_public_surface_drift(tmp_path: Path) -> None:
    document = _document()
    document["foundation"]["table"]["item_count"] = 1  # type: ignore[index]
    _reject(tmp_path, document)

    document = _document()
    document["agentcore"]["endpoint_status"] = "UPDATING"  # type: ignore[index]
    _reject(tmp_path, document)

    document = _document()
    document["agentcore"]["endpoint_live_version"] = "2"  # type: ignore[index]
    _reject(tmp_path, document)

    document = _document()
    document["public_web_surface"]["api_gateway"] = True  # type: ignore[index]
    _reject(tmp_path, document)


@pytest.mark.parametrize(
    "authority",
    ["publication_authorized", "order_authorized", "fulfillment_authorized"],
)
def test_rejects_any_commerce_authority(tmp_path: Path, authority: str) -> None:
    document = _document("backend-active-draft-only")
    document["safety"][authority] = True  # type: ignore[index]
    _reject(tmp_path, document)


def test_staged_rejects_predecessor_and_successors_require_exact_predecessor(
    tmp_path: Path,
) -> None:
    staged = _document()
    staged["predecessor_evidence"] = _predecessor_evidence("staged")
    _reject(tmp_path, staged)

    capacity = _document("capacity-released-inert")
    capacity["predecessor_evidence"] = None
    _reject(tmp_path, capacity)

    active = _document("backend-active-draft-only")
    active["predecessor_evidence"]["mode"] = "staged"  # type: ignore[index]
    _reject(tmp_path, active)


def test_successor_requires_supplied_canonical_predecessor_document(tmp_path: Path) -> None:
    capacity = _document("capacity-released-inert")
    path, predecessor, _ancestor = _write_validation_set(tmp_path, capacity)
    assert predecessor is not None
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW)

    staged = _document("staged")
    staged_path = _write(tmp_path, staged, name="standalone-staged.json")
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(
            staged_path,
            now=NOW,
            predecessor_evidence_path=predecessor,
        )


def test_predecessor_hash_mode_and_capture_time_must_match_supplied_bytes(
    tmp_path: Path,
) -> None:
    capacity = _document("capacity-released-inert")
    path, predecessor, _ancestor = _write_validation_set(tmp_path, capacity)
    assert predecessor is not None

    capacity["predecessor_evidence"]["evidence_sha256"] = "c" * 64  # type: ignore[index]
    path.write_bytes(canonical_phase6_core_live_state(capacity))
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW, predecessor_evidence_path=predecessor)

    capacity = _document("capacity-released-inert")
    path, predecessor, _ancestor = _write_validation_set(tmp_path, capacity)
    assert predecessor is not None
    capacity["predecessor_evidence"]["captured_at"] = "2026-08-25T22:56:00Z"  # type: ignore[index]
    path.write_bytes(canonical_phase6_core_live_state(capacity))
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW, predecessor_evidence_path=predecessor)

    wrong_mode = _document("capacity-released-inert")
    _set_capture_time(wrong_mode, PREDECESSOR_TIME)
    wrong_path = _write(tmp_path, wrong_mode, name="wrong-mode-predecessor.json")
    capacity = _document("capacity-released-inert")
    acceptance = capacity["predecessor_evidence"]
    assert isinstance(acceptance, dict)
    acceptance["evidence_sha256"] = sha256(wrong_path.read_bytes()).hexdigest()
    path = _write(tmp_path, capacity)
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW, predecessor_evidence_path=wrong_path)


def test_active_mode_requires_and_validates_staged_ancestor_chain(tmp_path: Path) -> None:
    active = _document("backend-active-draft-only")
    path, predecessor, ancestor = _write_validation_set(tmp_path, active)
    assert predecessor is not None
    assert ancestor is not None

    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW, predecessor_evidence_path=predecessor)

    different_staged = _document("staged")
    _set_capture_time(different_staged, "2026-08-25T22:54:00Z")
    different_path = _write(tmp_path, different_staged, name="different-staged.json")
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(
            path,
            now=NOW,
            predecessor_evidence_path=predecessor,
            staged_ancestor_evidence_path=different_path,
        )

    verified = verify_phase6_core_live_state(
        path,
        now=NOW,
        predecessor_evidence_path=predecessor,
        staged_ancestor_evidence_path=ancestor,
    )
    assert verified.mode == "backend-active-draft-only"


def test_accepts_hours_old_hash_linked_lineage_with_fresh_active_preflight(
    tmp_path: Path,
) -> None:
    staged = _document("staged")
    _set_capture_time(staged, "2026-08-25T18:00:00Z")
    staged_path = _write(tmp_path, staged, name="old-staged.json")

    capacity = _document("capacity-released-inert")
    _set_capture_time(capacity, "2026-08-25T20:00:00Z")
    capacity_link = capacity["predecessor_evidence"]
    assert isinstance(capacity_link, dict)
    capacity_link["captured_at"] = staged["capture_time"]
    capacity_link["evidence_sha256"] = sha256(staged_path.read_bytes()).hexdigest()
    capacity_path = _write(tmp_path, capacity, name="old-capacity.json")

    active = _document("backend-active-draft-only")
    active_link = active["predecessor_evidence"]
    assert isinstance(active_link, dict)
    active_link["captured_at"] = capacity["capture_time"]
    active_link["evidence_sha256"] = sha256(capacity_path.read_bytes()).hexdigest()
    active_path = _write(tmp_path, active, name="fresh-active.json")

    verified = verify_phase6_core_live_state(
        active_path,
        now=NOW,
        predecessor_evidence_path=capacity_path,
        staged_ancestor_evidence_path=staged_path,
    )
    assert verified.mode == "backend-active-draft-only"

    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(
            capacity_path,
            now=NOW,
            predecessor_evidence_path=staged_path,
        )


def test_rejects_predecessor_captured_after_its_successor(tmp_path: Path) -> None:
    staged = _document("staged")
    _set_capture_time(staged, "2026-08-25T23:00:00Z")
    staged_path = _write(tmp_path, staged, name="future-staged.json")

    capacity = _document("capacity-released-inert")
    capacity_link = capacity["predecessor_evidence"]
    assert isinstance(capacity_link, dict)
    capacity_link["captured_at"] = staged["capture_time"]
    capacity_link["evidence_sha256"] = sha256(staged_path.read_bytes()).hexdigest()
    capacity_path = _write(tmp_path, capacity)

    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(
            capacity_path,
            now=NOW,
            predecessor_evidence_path=staged_path,
        )


@pytest.mark.parametrize(
    ("path", "bad_value"),
    [
        (("table_scan", "request", "ConsistentRead"), False),
        (("table_scan", "request", "Select"), "ALL_ATTRIBUTES"),
        (("table_scan", "request", "TableName"), "other-table"),
        (("table_scan", "response", "Count"), 1),
        (("table_scan", "response", "ScannedCount"), 1),
        (("table_scan", "response", "LastEvaluatedKey"), {"PK": {"S": "JOB#hidden"}}),
    ],
)
def test_rejects_incomplete_or_nonempty_preflight_scan(
    tmp_path: Path, path: tuple[str, ...], bad_value: object
) -> None:
    document = _document("capacity-released-inert")
    target = document["preflight"]
    assert isinstance(target, dict)
    for part in path[:-1]:
        target = target[part]
        assert isinstance(target, dict)
    target[path[-1]] = bad_value
    _reject(tmp_path, document)


def test_rejects_item_content_field_or_running_preflight_execution(tmp_path: Path) -> None:
    document = _document("capacity-released-inert")
    response = document["preflight"]["table_scan"]["response"]  # type: ignore[index]
    response["Items"] = []
    _reject(tmp_path, document)

    document = _document("capacity-released-inert")
    running = document["preflight"]["running_executions"]  # type: ignore[index]
    running[0]["running_execution_count"] = 1
    _reject(tmp_path, document)


@pytest.mark.parametrize("bad_hash", ["0" * 64, "A" * 64, "abc", "<SHA256>"])
def test_rejects_malformed_predecessor_fingerprint(tmp_path: Path, bad_hash: str) -> None:
    document = _document("capacity-released-inert")
    document["predecessor_evidence"]["evidence_sha256"] = bad_hash  # type: ignore[index]
    _reject(tmp_path, document)


@pytest.mark.parametrize(
    "capture_time",
    [
        "2026-08-25T22:44:59Z",
        "2026-08-25T23:00:01Z",
        "2026-08-25T22:59:00+00:00",
        "2026-08-25 22:59:00Z",
    ],
)
def test_rejects_stale_future_or_noncanonical_capture_time(
    tmp_path: Path, capture_time: str
) -> None:
    document = _document()
    document["capture_time"] = capture_time
    _reject(tmp_path, document)


@pytest.mark.parametrize("captured_at", ["2026-08-25T22:44:59Z", "2026-08-25T23:00:00Z"])
def test_rejects_stale_or_future_current_preflight(tmp_path: Path, captured_at: str) -> None:
    document = _document("capacity-released-inert")
    document["preflight"]["captured_at"] = captured_at  # type: ignore[index]
    _reject(tmp_path, document)


def test_rejects_naive_or_non_utc_injected_now(tmp_path: Path) -> None:
    path = _write(tmp_path, _document())
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW.replace(tzinfo=None))
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW.astimezone(timezone(timedelta(hours=-7))))


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("account_id", "000000000000"),
        ("region", "us-east-1"),
        ("source_commit", "a" * 39),
        ("source_commit", "a" * 40),
        ("source_commit", "<SOURCE_COMMIT>"),
        ("mode", "active"),
        ("format", "mr-lister-phase6-core-live-state-v2"),
    ],
)
def test_rejects_malformed_or_placeholder_top_level_identifiers(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    document = _document()
    document[field] = bad_value
    _reject(tmp_path, document)


def test_rejects_placeholder_or_malformed_nested_identifiers(tmp_path: Path) -> None:
    document = _document()
    document["agentcore"]["runtime_arn"] = "<AGENTCORE_RUNTIME_ARN>"  # type: ignore[index]
    _reject(tmp_path, document)

    document = _document()
    document["stack"]["id"] = live.STACK_ID + "/extra"  # type: ignore[index]
    _reject(tmp_path, document)


def test_requires_exact_canonical_json_and_unique_members(tmp_path: Path) -> None:
    document = _document()
    path = tmp_path / "pretty-but-unsorted.json"
    path.write_text(json.dumps(document, indent=4) + "\n", encoding="utf-8")
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW)

    duplicate = canonical_phase6_core_live_state(document).decode("utf-8")
    duplicate = duplicate.replace(
        '  "account_id": "384627057108",',
        '  "account_id": "384627057108",\n  "account_id": "384627057108",',
        1,
    )
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW)

    path.write_text('{"value":NaN}\n', encoding="utf-8")
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(path, now=NOW)


def test_rejects_symlinked_file_or_parent(tmp_path: Path) -> None:
    target = _write(tmp_path, _document(), name="target.json")
    file_link = tmp_path / "file-link.json"
    file_link.symlink_to(target)
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(file_link, now=NOW)

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    nested = _write(real_parent, _document(), name="nested.json")
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(Phase6CoreLiveStateError):
        verify_phase6_core_live_state(parent_link / nested.name, now=NOW)


def test_verifier_source_imports_no_aws_sdk_or_subprocess() -> None:
    source_path = Path(live.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint({"aws_cdk", "boto3", "botocore", "subprocess"})
