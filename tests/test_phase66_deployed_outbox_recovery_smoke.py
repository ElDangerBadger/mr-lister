from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import pytest

from mr_lister.control.dispatch import deterministic_execution_name
from mr_lister.control.models import WorkRequestStatus, WorkType
from tools import phase66_deployed_outbox_recovery_smoke as smoke

NOW_DIGEST: Final = "a" * 64


def _authority() -> smoke.DeploymentAuthority:
    return smoke.DeploymentAuthority(
        table_name="private-table-secret",
        artifact_bucket="private-bucket-secret",
        functions={
            "DispatcherFunction": "dispatcher-secret",
            "SourceVersionRetentionFunction": "retention-secret",
            "StuckExecutionRecoveryFunction": "recovery-secret",
        },
        state_machine_arns={
            WorkType.PREPARE: (
                "arn:aws:states:us-west-2:384627057108:stateMachine:mr-lister-phase6-dev-prepare"
            ),
            WorkType.SYNCHRONIZE_PRODUCT: (
                "arn:aws:states:us-west-2:384627057108:stateMachine:"
                "mr-lister-phase6-dev-synchronize-product"
            ),
            WorkType.RECONCILE_PRODUCT: (
                "arn:aws:states:us-west-2:384627057108:stateMachine:"
                "mr-lister-phase6-dev-reconcile-product"
            ),
            WorkType.REFRESH_ECONOMICS: (
                "arn:aws:states:us-west-2:384627057108:stateMachine:"
                "mr-lister-phase6-dev-refresh-economics"
            ),
        },
    )


def _before() -> smoke.LiveSnapshot:
    return smoke.LiveSnapshot(
        application_record_count=44,
        application_record_digest="1" * 64,
        provider_record_count=0,
        dispatched_work_count=0,
        running_execution_count=0,
        execution_digests=("2" * 64,),
        source_version_count=2,
        source_inventory_digest="3" * 64,
        referenced_version_count=2,
        pinned_version_count=2,
        retention_checkpoint_present=True,
        authority=_authority(),
    )


def _observation() -> smoke.DispatchObservation:
    return smoke.DispatchObservation(
        execution_arn="execution-secret",
        execution_digest="4" * 64,
        status="FAILED",
        exact_name_count=1,
        exact_input=True,
    )


def _after(**changes: Any) -> smoke.LiveSnapshot:
    baseline = _before()
    values: dict[str, Any] = {
        "execution_digests": (*baseline.execution_digests, "4" * 64),
    }
    values.update(changes)
    return replace(baseline, **values)


def _gate_document(before: smoke.LiveSnapshot | None = None) -> dict[str, Any]:
    before = before or _before()
    baseline = before.gate_baseline(synthetic_namespace_absent=True)
    return {
        "authorization_contract": smoke.GATE_CONTRACT,
        "gate_id": smoke.GATE_ID,
        "source_authority_commit": smoke.SOURCE_AUTHORITY_COMMIT,
        "source_authority_commit_digest": smoke.SOURCE_AUTHORITY_COMMIT_DIGEST,
        "deployment_digest": "d" * 64,
        "prerequisite_evidence_run_digest": "e" * 64,
        "method_authorization": dict(smoke._EXPECTED_METHOD_AUTHORIZATION),
        "exact_write_budget": {
            **smoke._FIXED_WRITE_BUDGET,
            "s3_version_tag_writes": baseline["retention_source_version_count"],
        },
        "baseline": baseline,
    }


def _gate(document: dict[str, Any] | None = None) -> smoke.RunGate:
    return smoke.RunGate(digest=NOW_DIGEST, document=document or _gate_document())


def _retention_response() -> dict[str, Any]:
    return {
        "contract_version": "1.0.0",
        "pages_scanned": 1,
        "versions_scanned": 2,
        "delete_markers_skipped": 0,
        "versions_reasserted_pinned": 2,
        "versions_released_to_staged": 0,
        "staged_versions_unchanged": 0,
        "scan_complete": True,
    }


def _recovery_response() -> dict[str, Any]:
    return {
        "contract_version": "1.0.0",
        "candidates_scanned": 1,
        "already_settled": 0,
        "not_due": 0,
        "running_past_bound": 0,
        "recovered_completion": 0,
        "failure_settled": 0,
        "reconciliation_routed": 0,
        "cancellation_settled": 1,
        "authority_conflicts": 0,
        "dependency_unavailable": 0,
        "settlement_exhausted": 0,
        "terminal_executions_observed": 0,
        "executions_missing": 1,
        "batch_limit": 25,
        "batch_limit_reached": False,
        "alarm_signal_count": 0,
        "requires_operator_attention": False,
    }


class FakeBackend:
    def __init__(self) -> None:
        self.before = _before()
        self.after = _after()
        self.calls: list[str] = []
        self.dispatch_responses = [
            {"attempted": 1, "dispatched": 1},
            {"attempted": 0, "dispatched": 0},
        ]
        self.retention_response = _retention_response()
        self.recovery_response = _recovery_response()
        self.observation = _observation()

    def prepare(self, gate: smoke.RunGate, canary: smoke.CanaryAuthority) -> smoke.LiveSnapshot:
        self.calls.append("prepare")
        assert len(gate.digest) == 64
        assert repr(canary).count("repr=False") == 0
        return self.before

    def invoke_retention(self, authority: smoke.DeploymentAuthority) -> dict[str, Any]:
        self.calls.append("invoke_retention")
        return self.retention_response

    def verify_retention(self, before: smoke.LiveSnapshot) -> None:
        self.calls.append("verify_retention")
        assert before is self.before

    def put_outbox_work(
        self, authority: smoke.DeploymentAuthority, canary: smoke.CanaryAuthority
    ) -> None:
        self.calls.append("put_outbox_work")

    def invoke_dispatcher(self, authority: smoke.DeploymentAuthority) -> dict[str, Any]:
        self.calls.append("invoke_dispatcher")
        return self.dispatch_responses.pop(0)

    def observe_outbox_execution(
        self, authority: smoke.DeploymentAuthority, canary: smoke.CanaryAuthority
    ) -> smoke.DispatchObservation:
        self.calls.append("observe_outbox_execution")
        return self.observation

    def put_recovery_pair(
        self, authority: smoke.DeploymentAuthority, canary: smoke.CanaryAuthority
    ) -> None:
        self.calls.append("put_recovery_pair")

    def invoke_recovery(self, authority: smoke.DeploymentAuthority) -> dict[str, Any]:
        self.calls.append("invoke_recovery")
        return self.recovery_response

    def cleanup_synthetic(self, canary: smoke.CanaryAuthority) -> None:
        self.calls.append("cleanup_synthetic")

    def snapshot(self, authority: smoke.DeploymentAuthority) -> smoke.LiveSnapshot:
        self.calls.append("snapshot")
        return self.after


def _private_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    private = repository / ".mr_lister_private" / "phase66-acceptance"
    private.mkdir(parents=True, mode=0o700)
    repository.chmod(0o700)
    (repository / ".mr_lister_private").chmod(0o700)
    private.chmod(0o700)
    monkeypatch.setattr(smoke, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(smoke, "PRIVATE_ROOT", private)
    return private


def _write_gate(private: Path, document: dict[str, Any] | None = None) -> tuple[Path, str]:
    path = private / "run" / "gate.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    payload = smoke._canonical_json(document or _gate_document(), pretty=True)
    path.write_bytes(payload)
    path.chmod(0o600)
    return path, smoke._digest_bytes(payload)


def test_default_cli_is_local_only_and_never_constructs_live_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    gate_path, gate_digest = _write_gate(private)
    constructed = False

    def forbidden_factory() -> FakeBackend:
        nonlocal constructed
        constructed = True
        raise AssertionError("default preflight constructed a live backend")

    assert (
        smoke.main(
            ["--gate", str(gate_path), "--gate-sha256", gate_digest],
            backend_factory=forbidden_factory,
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert constructed is False
    assert result["mode"] == "local_preflight"
    assert result["network_calls"] == result["mutations"] == 0


def test_gate_requires_exact_digest_authorities_method_baseline_and_dynamic_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    document = _gate_document()
    gate_path, gate_digest = _write_gate(private, document)
    assert smoke.load_run_gate(gate_path, gate_digest).digest == gate_digest

    with pytest.raises(smoke.SmokeError, match="does not match"):
        smoke.load_run_gate(gate_path, "0" * 64)

    document["source_authority_commit"] = "0" * 40
    gate_path, gate_digest = _write_gate(private, document)
    with pytest.raises(smoke.SmokeError, match="source authority commit"):
        smoke.load_run_gate(gate_path, gate_digest)

    document = _gate_document()
    document["exact_write_budget"]["s3_version_tag_writes"] = 3
    gate_path, gate_digest = _write_gate(private, document)
    with pytest.raises(smoke.SmokeError, match="write budget"):
        smoke.load_run_gate(gate_path, gate_digest)

    document = _gate_document()
    document["baseline"]["existing_dispatched_work_count"] = 1
    gate_path, gate_digest = _write_gate(private, document)
    with pytest.raises(smoke.SmokeError, match="provider-zero bounded"):
        smoke.load_run_gate(gate_path, gate_digest)


def test_live_path_runs_all_seven_assertions_and_emits_only_sanitized_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    backend = FakeBackend()
    output = private / "run" / "evidence"

    result = smoke.run_live(_gate(), backend, output)

    assert result["status"] == "passed"
    assert backend.calls == [
        "prepare",
        "invoke_retention",
        "verify_retention",
        "put_outbox_work",
        "invoke_dispatcher",
        "invoke_dispatcher",
        "observe_outbox_execution",
        "put_recovery_pair",
        "invoke_recovery",
        "cleanup_synthetic",
        "snapshot",
    ]
    summary = json.loads((output / "canary-summary.json").read_text())
    assert list(summary["assertions"]) == [
        "committed_work_is_recovered_by_sweep",
        "deterministic_execution_starts_once",
        "logical_work_is_not_duplicated",
        "privacy_scan_passes",
        "provider_call_count_is_zero",
        "reference_aware_retention_sweep_passes",
        "stuck_execution_recovery_passes",
    ]
    assert all(summary["assertions"].values())
    retained = b"".join(path.read_bytes() for path in sorted(output.iterdir()))
    canary = smoke.derive_canary(NOW_DIGEST)
    for secret in (*canary.sensitive_values, "private-table-secret", str(output)):
        assert secret.encode() not in retained
    assert {path.stat().st_mode & 0o777 for path in output.iterdir()} == {0o600}


def test_failure_after_first_write_always_runs_bounded_cleanup(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.dispatch_responses[1] = {"attempted": 1, "dispatched": 1}

    with pytest.raises(smoke.SmokeError, match="idempotent"):
        smoke.run_live(_gate(), backend, tmp_path)

    assert backend.calls[-1] == "cleanup_synthetic"
    assert "put_recovery_pair" not in backend.calls


def test_retention_release_stops_before_synthetic_writes_and_needs_no_cleanup(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    backend.retention_response["versions_reasserted_pinned"] = 1
    backend.retention_response["versions_released_to_staged"] = 1

    with pytest.raises(smoke.SmokeError, match="retention sweep"):
        smoke.run_live(_gate(), backend, tmp_path)

    assert "put_outbox_work" not in backend.calls
    assert "cleanup_synthetic" not in backend.calls


def test_duplicate_or_nonfailed_execution_fails_closed_and_cleans(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.observation = replace(_observation(), exact_name_count=2)
    with pytest.raises(smoke.SmokeError, match="single"):
        smoke.run_live(_gate(), backend, tmp_path)
    assert backend.calls[-1] == "cleanup_synthetic"


def test_final_audit_rejects_provider_source_or_execution_delta() -> None:
    before = _before()
    observation = _observation()
    smoke._verify_final(before, _after(), observation)

    with pytest.raises(smoke.SmokeError, match="bounded baseline"):
        smoke._verify_final(before, _after(provider_record_count=1), observation)
    with pytest.raises(smoke.SmokeError, match="bounded baseline"):
        smoke._verify_final(before, _after(source_version_count=3), observation)
    with pytest.raises(smoke.SmokeError, match="one retained"):
        smoke._verify_final(
            before,
            _after(execution_digests=(*before.execution_digests, "5" * 64)),
            observation,
        )


def test_canary_authority_repr_and_evidence_never_retain_raw_identifiers() -> None:
    canary = smoke.derive_canary(NOW_DIGEST)
    rendered = repr(canary)
    assert all(value not in rendered for value in canary.sensitive_values)
    with pytest.raises(smoke.SmokeError, match="private authority"):
        smoke._assert_private_payload(
            smoke._canonical_json({"value": canary.outbox_job_id}), canary
        )
    with pytest.raises(smoke.SmokeError, match="private authority"):
        smoke._assert_private_payload(smoke._canonical_json({"token": "redacted"}), canary)


def test_synthetic_models_bind_missing_job_dispatch_and_missing_execution_recovery() -> None:
    canary = smoke.derive_canary(NOW_DIGEST)
    authority = _authority()
    outbox = smoke.AwsBackend._outbox_work(canary, smoke.datetime.now(smoke.UTC))
    recovery_job, recovery_work = smoke.AwsBackend._recovery_models(
        authority, canary, smoke.datetime.now(smoke.UTC)
    )

    assert outbox.status is WorkRequestStatus.PENDING
    assert outbox.work_type is WorkType.PREPARE
    assert outbox.job_id == canary.outbox_job_id
    assert outbox.execution_name == deterministic_execution_name(canary.outbox_work_id)
    assert recovery_job.state is smoke.ControlJobState.CANCEL_REQUESTED
    assert recovery_job.active_work_request_id == recovery_work.work_request_id
    assert recovery_work.status is WorkRequestStatus.DISPATCHED
    assert recovery_work.work_type is WorkType.PREPARE
    assert recovery_work.updated_at < smoke.datetime.now(smoke.UTC) - smoke.timedelta(minutes=20)


def test_backend_constructs_no_provider_bedrock_agentcore_secret_cognito_or_browser_client() -> (
    None
):
    source = inspect.getsource(smoke.AwsBackend.__init__)
    for forbidden in (
        'client("bedrock"',
        'client("bedrock-agentcore"',
        'client("cognito-idp"',
        'client("secretsmanager"',
        "browser",
        "provider",
    ):
        assert forbidden not in source.lower()


def test_dispatch_stream_filter_must_exclude_remove_before_cleanup_is_authorized() -> None:
    class LambdaClient:
        pattern = smoke._SAFE_DISPATCH_STREAM_FILTER

        def list_event_source_mappings(self, *, FunctionName: str) -> dict[str, Any]:
            assert FunctionName == "dispatcher-secret"
            return {
                "EventSourceMappings": [
                    {
                        "State": "Enabled",
                        "BatchSize": 25,
                        "MaximumRetryAttempts": 3,
                        "FilterCriteria": {"Filters": [{"Pattern": json.dumps(self.pattern)}]},
                    }
                ]
            }

    backend = smoke.AwsBackend.__new__(smoke.AwsBackend)
    client = LambdaClient()
    backend._lambda = client
    backend._verify_dispatch_stream_filter("dispatcher-secret")

    client.pattern = {"dynamodb": smoke._SAFE_DISPATCH_STREAM_FILTER["dynamodb"]}
    with pytest.raises(smoke.SmokeError, match="cleanup inert"):
        backend._verify_dispatch_stream_filter("dispatcher-secret")


def test_cleanup_refuses_any_row_outside_exact_synthetic_graph() -> None:
    canary = smoke.derive_canary(NOW_DIGEST)
    backend = smoke.AwsBackend.__new__(smoke.AwsBackend)
    backend._authority = _authority()

    def raw(pk: str, sk: str, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "PK": {"S": pk},
            "SK": {"S": sk},
            "entity_type": {"S": entity},
            "payload": {"S": json.dumps(payload)},
        }

    unexpected = raw(
        f"JOB#{canary.outbox_job_id}",
        "SOURCE",
        "SOURCE_ARTIFACT",
        {"job_id": canary.outbox_job_id},
    )
    backend._query_partition = lambda _table, partition: (
        (unexpected,) if partition.startswith("JOB#") else ()
    )
    backend._cleanup_items = lambda *_args: None

    with pytest.raises(smoke.SmokeError, match="exact canary graph"):
        backend.cleanup_synthetic(canary)


def test_private_gate_rejects_group_readable_file_and_out_of_root_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    gate_path, gate_digest = _write_gate(private)
    gate_path.chmod(0o640)
    with pytest.raises(smoke.SmokeError, match="mode-0600"):
        smoke.load_run_gate(gate_path, gate_digest)

    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    outside.chmod(0o600)
    with pytest.raises(smoke.SmokeError, match="repository workspace"):
        smoke.load_run_gate(outside, smoke._digest_bytes(b"{}"))


def test_live_cli_requires_output_root_and_exact_environment_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    gate_path, gate_digest = _write_gate(private)
    arguments = ["--gate", str(gate_path), "--gate-sha256", gate_digest, "--live"]

    with pytest.raises(smoke.SmokeError, match="output root"):
        smoke.main(arguments, backend_factory=FakeBackend)
    output = private / "output"
    with pytest.raises(smoke.SmokeError, match="environment switch"):
        smoke.main([*arguments, "--output-root", str(output)], backend_factory=FakeBackend)
    monkeypatch.setenv(smoke.LIVE_ENVIRONMENT_SWITCH, smoke.LIVE_ENVIRONMENT_VALUE)
    assert smoke.main([*arguments, "--output-root", str(output)], backend_factory=FakeBackend) == 0
