from __future__ import annotations

import ast
import json
import os
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from mr_lister.acceptance.evidence_set import Phase66ArtifactFile
from mr_lister.acceptance.phase6 import validate_phase66_evidence
from tools import prepare_phase66_edge_revalidation as revalidation


def _digest(label: str) -> str:
    return sha256(label.encode("ascii")).hexdigest()


def _alias(ordinal: int) -> str:
    return f"alias_{ordinal:032x}"


def _deployment_authority() -> dict[str, object]:
    lambdas = [
        {
            "code_sha256": _digest(f"code-{logical_id}"),
            "configuration_digest": _digest(f"configuration-{logical_id}"),
            "last_update_status": "Successful",
            "logical_id": logical_id,
            "release_fingerprint_digest": _digest(f"release-{logical_id}"),
            "state": "Active",
        }
        for logical_id in revalidation._LAMBDA_LOGICAL_IDS
    ]
    authority = {
        "account_binding_digest": _digest("account"),
        "cognito": {
            "browser_client_configuration_digest": _digest("browser-client"),
            "browser_client_secret_present": False,
            "confirmed_user_count": 2,
            "enabled_user_count": 3,
            "mfa_configuration": "ON",
            "pool_configuration_digest": _digest("pool"),
            "seller_group_member_count": 3,
            # This read-only Cognito aggregate may be zero even when the observed PKCE/token
            # matrix passes; it is deployment posture, not a frozen behavioral assertion.
            "software_token_mfa_user_count": 0,
            "user_count": 3,
        },
        "lambdas": lambdas,
        "readiness": "WEB_EDGE_ACTIVE_DRAFT_ONLY",
        "region": "us-west-2",
        "source_commit_digest": revalidation.SOURCE_COMMIT_DIGEST,
        "stack": {
            "incomplete_resource_count": 0,
            "output_count": 19,
            "outputs_digest": _digest("outputs"),
            "resource_count": 125,
            "resource_inventory_digest": _digest("resources"),
            "stack_status": "UPDATE_COMPLETE",
            "tags_digest": _digest("tags"),
            "template_digest": _digest("template"),
            "termination_protection": True,
        },
        "stack_name": "mr-lister-phase6-dev",
        "web_edge": {
            "alias_count": 1,
            "api_configuration_digest": _digest("api"),
            "api_protocol": "HTTP",
            "application_body_digest": _digest("application"),
            "application_status_code": 200,
            "cors_headers_digest": _digest("cors"),
            "cors_passed": True,
            "cors_status_code": 204,
            "distribution_configuration_digest": _digest("distribution"),
            "distribution_enabled": True,
            "distribution_status": "Deployed",
            "health_body_digest": _digest("health"),
            "health_passed": True,
            "health_status_code": 200,
            "origin_count": 2,
            "route_count": 15,
            "security_header_count": 7,
            "security_headers_digest": _digest("security"),
            "security_headers_passed": True,
        },
    }
    return {
        "authority": authority,
        "captured_at": "2026-08-29T20:00:00Z",
        "deployment_digest": revalidation._digest(authority),
        "format": revalidation.DEPLOYMENT_AUTHORITY_FORMAT,
    }


def _observation() -> dict[str, object]:
    return {
        "format": revalidation.OBSERVATION_FORMAT,
        "recorded_at": "2026-08-29T20:05:00Z",
        "run": {"alias": _alias(1), "authority_digest": _digest("run")},
        "actor_a": {
            "alias": _alias(2),
            "authority_digest": _digest("actor-a"),
            "visible_job_count": 2,
            "known_review_ready": True,
            "known_preview_ready": True,
        },
        "actor_b": {
            "alias": _alias(3),
            "authority_digest": _digest("actor-b"),
            "visible_job_count": 0,
            "actor_a_job_absent": True,
            "unknown_job_absent": True,
        },
        "known_job": {"alias": _alias(4), "authority_digest": _digest("known-job")},
        "correlation": {
            "alias": _alias(5),
            "authority_digest": _digest("correlation"),
        },
        "matrix": {
            "health_passed": True,
            "readiness_passed": True,
            "security_headers_passed": True,
            "cors_passed": True,
            "pkce_authorization_passed": True,
            "pkce_callback_passed": True,
            "token_exchange_passed": True,
            "unauthenticated_access_rejected": True,
        },
        "review": {
            "access_path": "direct_deployed_review_lambda",
            "invocation_count": 2,
            "review_ready": True,
            "preview_ready": True,
            "etag_type": "strong",
            "first_etag_digest": _digest("strong-etag"),
            "second_etag_digest": _digest("strong-etag"),
        },
        "deltas": {
            "provider_call_delta": 0,
            "provider_record_delta": 0,
            "work_item_delta": 0,
            "workflow_execution_delta": 0,
        },
    }


@pytest.fixture
def private_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "repository"
    workspace = repository / ".mr_lister_private" / "phase66-acceptance"
    repository.mkdir()
    workspace.mkdir(mode=0o700, parents=True)
    (repository / ".mr_lister_private").chmod(0o700)
    monkeypatch.setattr(revalidation, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(revalidation, "PRIVATE_WORKSPACE_ROOT", workspace)
    return workspace


def _write_private(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = path.parent
    while current.name != ".mr_lister_private":
        current.chmod(0o700)
        current = current.parent
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)


def _prepare(
    workspace: Path,
    *,
    deployment: dict[str, object] | None = None,
    observation: dict[str, object] | None = None,
    run_name: str = "run-output",
) -> tuple[Path, dict[str, object]]:
    inputs = workspace / f"inputs-{run_name}"
    deployment_path = inputs / "deployment.json"
    observation_path = inputs / "observation.json"
    _write_private(deployment_path, deployment or _deployment_authority())
    _write_private(observation_path, observation or _observation())
    run_root = workspace / run_name
    result = revalidation.prepare_phase66_edge_revalidation(
        run_root=run_root,
        source_commit=revalidation.SOURCE_COMMIT,
        source_commit_digest=revalidation.SOURCE_COMMIT_DIGEST,
        deployment_authority_path=deployment_path,
        observation_path=observation_path,
    )
    return run_root, result


def _load(path: Path) -> object:
    return json.loads(path.read_bytes())


def test_assembler_emits_exact_private_valid_frozen_evidence(
    private_workspace: Path,
) -> None:
    run_root, result = _prepare(private_workspace)

    assert {path.name for path in run_root.iterdir()} == set(revalidation._OUTPUT_FILENAMES)
    assert os.stat(run_root, follow_symlinks=False).st_mode & 0o777 == 0o700
    assert all(
        os.stat(run_root / filename, follow_symlinks=False).st_mode & 0o777 == 0o600
        for filename in revalidation._OUTPUT_FILENAMES
    )
    records = _load(run_root / revalidation.RECORDS_FILENAME)
    assert isinstance(records, list) and len(records) == 1
    record = validate_phase66_evidence(records[0])
    assert record.gate_id == revalidation.GATE_ID
    assert record.outcome.value == "passed"
    assert record.source_commit_digest == revalidation.SOURCE_COMMIT_DIGEST
    assert len(record.actor_digests) == 2
    assert record.job_digest is not None
    assert record.correlation_digest is not None
    assert (
        tuple(item.assertion_id for item in record.assertions) == revalidation._EXPECTED_ASSERTIONS
    )
    assert all(item.passed for item in record.assertions)
    assert result == {
        "artifact_count": 3,
        "deployment_digest": _deployment_authority()["deployment_digest"],
        "record_digest": revalidation._digest(record.model_dump(mode="json")),
        "result": "passed",
        "run_digest": record.run_digest,
    }


def test_artifact_index_exactly_binds_canonical_artifact_bytes(
    private_workspace: Path,
) -> None:
    run_root, _ = _prepare(private_workspace)
    index = _load(run_root / revalidation.ARTIFACT_FILES_FILENAME)
    assert isinstance(index, list) and len(index) == 3
    for raw_entry in index:
        entry = Phase66ArtifactFile.model_validate_json(json.dumps(raw_entry))
        contents = (run_root / entry.relative_path).read_bytes()
        assert contents.endswith(b"\n")
        assert sha256(contents).hexdigest() == entry.artifact_digest
        assert (
            json.dumps(json.loads(contents), sort_keys=True, separators=(",", ":")).encode() + b"\n"
            == contents
        )


def test_aliases_are_never_retained_and_new_run_scope_changes_all_bindings(
    private_workspace: Path,
) -> None:
    first_root, _ = _prepare(private_workspace, run_name="first")
    second_observation = _observation()
    second_observation["run"] = {
        "alias": _alias(99),
        "authority_digest": _digest("second-run"),
    }
    second_root, _ = _prepare(
        private_workspace,
        observation=second_observation,
        run_name="second",
    )
    first_bytes = b"".join(path.read_bytes() for path in sorted(first_root.iterdir()))
    second_bytes = b"".join(path.read_bytes() for path in sorted(second_root.iterdir()))
    for alias in (_alias(1), _alias(2), _alias(3), _alias(4), _alias(5), _alias(99)):
        assert alias.encode() not in first_bytes
        assert alias.encode() not in second_bytes
    first_record = _load(first_root / revalidation.RECORDS_FILENAME)[0]
    second_record = _load(second_root / revalidation.RECORDS_FILENAME)[0]
    assert first_record["run_digest"] != second_record["run_digest"]
    assert first_record["actor_digests"] != second_record["actor_digests"]
    assert first_record["job_digest"] != second_record["job_digest"]
    assert first_record["correlation_digest"] != second_record["correlation_digest"]


@pytest.mark.parametrize(
    ("path", "invalid"),
    [
        (("matrix", "health_passed"), False),
        (("matrix", "readiness_passed"), False),
        (("matrix", "pkce_authorization_passed"), False),
        (("matrix", "security_headers_passed"), False),
        (("actor_a", "visible_job_count"), 1),
        (("actor_b", "visible_job_count"), 1),
        (("actor_b", "actor_a_job_absent"), False),
        (("review", "etag_type"), "weak"),
        (("review", "second_etag_digest"), _digest("changed-etag")),
        (("deltas", "provider_call_delta"), 1),
        (("deltas", "work_item_delta"), 1),
    ],
)
def test_every_frozen_assertion_fails_closed_on_incomplete_observation(
    private_workspace: Path,
    path: tuple[str, str],
    invalid: object,
) -> None:
    observation = _observation()
    nested = observation[path[0]]
    assert isinstance(nested, dict)
    nested[path[1]] = invalid

    with pytest.raises(
        revalidation.Phase66EdgeRevalidationError,
        match="closed sanitized contracts",
    ):
        _prepare(private_workspace, observation=observation)


@pytest.mark.parametrize(
    "raw_value",
    [
        "job_8144bc2a0f50b0ae9d113d39132ea74e",
        "seller@example.invalid",
        "https://massskutiny.com/jobs/example",
        "/Users/example/private.json",
        "arn:aws:lambda:us-west-2:000000000000:function:example",
        "Bearer token-value",
        "ordinary free text",
    ],
)
def test_raw_or_free_text_alias_values_are_rejected(
    private_workspace: Path,
    raw_value: str,
) -> None:
    observation = _observation()
    actor_a = observation["actor_a"]
    assert isinstance(actor_a, dict)
    actor_a["alias"] = raw_value

    with pytest.raises(revalidation.Phase66EdgeRevalidationError):
        _prepare(private_workspace, observation=observation)


@pytest.mark.parametrize(
    "raw_field",
    [
        "job_id",
        "email",
        "subject",
        "access_token",
        "url",
        "local_path",
        "bucket_name",
        "object_key",
        "notes",
    ],
)
def test_raw_authority_and_free_text_fields_are_rejected(
    private_workspace: Path,
    raw_field: str,
) -> None:
    observation = _observation()
    observation[raw_field] = "redacted"

    with pytest.raises(revalidation.Phase66EdgeRevalidationError):
        _prepare(private_workspace, observation=observation)


def test_exactly_two_distinct_actors_are_required(private_workspace: Path) -> None:
    missing = _observation()
    del missing["actor_b"]
    with pytest.raises(revalidation.Phase66EdgeRevalidationError):
        _prepare(private_workspace, observation=missing, run_name="missing")

    extra = _observation()
    extra["actor_c"] = deepcopy(extra["actor_b"])
    with pytest.raises(revalidation.Phase66EdgeRevalidationError):
        _prepare(private_workspace, observation=extra, run_name="extra")

    duplicate = _observation()
    actor_a = duplicate["actor_a"]
    actor_b = duplicate["actor_b"]
    assert isinstance(actor_a, dict) and isinstance(actor_b, dict)
    actor_b["alias"] = actor_a["alias"]
    with pytest.raises(revalidation.Phase66EdgeRevalidationError):
        _prepare(private_workspace, observation=duplicate, run_name="duplicate")


def test_source_and_deployment_authorities_are_exact(private_workspace: Path) -> None:
    inputs = private_workspace / "inputs"
    deployment_path = inputs / "deployment.json"
    observation_path = inputs / "observation.json"
    _write_private(deployment_path, _deployment_authority())
    _write_private(observation_path, _observation())

    with pytest.raises(revalidation.Phase66EdgeRevalidationError, match="exact Phase 6 source"):
        revalidation.prepare_phase66_edge_revalidation(
            run_root=private_workspace / "wrong-source",
            source_commit="0" * 40,
            source_commit_digest=_digest("wrong-source"),
            deployment_authority_path=deployment_path,
            observation_path=observation_path,
        )
    deployment = _deployment_authority()
    deployment["deployment_digest"] = _digest("unbound-deployment")
    with pytest.raises(revalidation.Phase66EdgeRevalidationError):
        _prepare(private_workspace, deployment=deployment, run_name="wrong-deployment")


def test_observation_must_follow_deployment_capture(private_workspace: Path) -> None:
    observation = _observation()
    observation["recorded_at"] = "2026-08-29T19:59:59Z"

    with pytest.raises(revalidation.Phase66EdgeRevalidationError, match="predates"):
        _prepare(private_workspace, observation=observation)


def test_duplicate_json_members_are_rejected(private_workspace: Path) -> None:
    inputs = private_workspace / "duplicate-json"
    deployment_path = inputs / "deployment.json"
    observation_path = inputs / "observation.json"
    _write_private(deployment_path, _deployment_authority())
    _write_private(observation_path, _observation())
    observation_path.write_text('{"format":"x","format":"y"}\n', encoding="utf-8")
    observation_path.chmod(0o600)

    with pytest.raises(revalidation.Phase66EdgeRevalidationError, match="strict JSON"):
        revalidation.prepare_phase66_edge_revalidation(
            run_root=private_workspace / "output",
            source_commit=revalidation.SOURCE_COMMIT,
            source_commit_digest=revalidation.SOURCE_COMMIT_DIGEST,
            deployment_authority_path=deployment_path,
            observation_path=observation_path,
        )


def test_inputs_and_outputs_cannot_escape_or_follow_symlinks(private_workspace: Path) -> None:
    inputs = private_workspace / "symlink-input"
    deployment_path = inputs / "deployment.json"
    real_observation = inputs / "real-observation.json"
    linked_observation = inputs / "observation.json"
    _write_private(deployment_path, _deployment_authority())
    _write_private(real_observation, _observation())
    linked_observation.symlink_to(real_observation)

    with pytest.raises(revalidation.Phase66EdgeRevalidationError, match="regular file"):
        revalidation.prepare_phase66_edge_revalidation(
            run_root=private_workspace / "output",
            source_commit=revalidation.SOURCE_COMMIT,
            source_commit_digest=revalidation.SOURCE_COMMIT_DIGEST,
            deployment_authority_path=deployment_path,
            observation_path=linked_observation,
        )
    with pytest.raises(revalidation.Phase66EdgeRevalidationError, match="must stay"):
        revalidation.prepare_phase66_edge_revalidation(
            run_root=private_workspace.parent / "escaped",
            source_commit=revalidation.SOURCE_COMMIT,
            source_commit_digest=revalidation.SOURCE_COMMIT_DIGEST,
            deployment_authority_path=deployment_path,
            observation_path=real_observation,
        )


def test_existing_different_output_is_never_overwritten(private_workspace: Path) -> None:
    run_root = private_workspace / "conflict"
    run_root.mkdir(mode=0o700)
    conflict = run_root / revalidation.CANARY_SUMMARY_FILENAME
    conflict.write_bytes(b"{}\n")
    conflict.chmod(0o600)

    with pytest.raises(revalidation.Phase66EdgeRevalidationError, match="differs"):
        _prepare(private_workspace, run_name="conflict")

    assert conflict.read_bytes() == b"{}\n"
    assert {path.name for path in run_root.iterdir()} == {revalidation.CANARY_SUMMARY_FILENAME}


def test_identical_rerun_is_idempotent(private_workspace: Path) -> None:
    run_root, first = _prepare(private_workspace, run_name="repeat")
    before = {path.name: path.read_bytes() for path in run_root.iterdir()}

    _, second = _prepare(private_workspace, run_name="repeat")

    assert first == second
    assert {path.name: path.read_bytes() for path in run_root.iterdir()} == before


def test_module_has_no_network_cloud_browser_or_provider_imports() -> None:
    tree = ast.parse(Path(revalidation.__file__).read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.partition(".")[0])
    assert imported_roots.isdisjoint(
        {"boto3", "botocore", "urllib", "httpx", "requests", "playwright", "selenium"}
    )
