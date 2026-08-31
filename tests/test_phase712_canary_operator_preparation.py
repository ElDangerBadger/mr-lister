"""Offline checks for the SHA-bound Phase 7.12 canary operator preparation."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import tools.prepare_phase712_canary_request as operator
from mr_lister.publication.canary_runtime import (
    PublicationCanaryBinding,
    PublicationCanaryInvocation,
    PublicationCanaryMode,
)
from mr_lister.publication.execution_store import InMemoryPublicationExecutionStore
from mr_lister.publication.guard_verification import PublicationPreCallAuthorityError
from mr_lister.publication.store import InMemoryPublicationStore, PublicationRequestTransaction
from tests.test_phase71_publication_service import (
    NOW,
    OWNER_ID,
    RELEASE_FINGERPRINT,
    _authority,
    profile_eligibility_authority,
)
from tools.build_phase711_canary_release import (
    CANARY_SOURCE_DIRECTORY_NAME,
    build_canary_source_bundle,
)


class Backend:
    def __init__(self) -> None:
        self.authority, self.profile = _authority(job_id="job_phase712_canary")
        self.requests = InMemoryPublicationStore((self.authority,))
        self.execution: InMemoryPublicationExecutionStore | None = None
        self.commits = 0
        self.deployment_reads = 0
        self.deployment = operator.DeploymentAuthority(
            account_id=operator.ACCOUNT_ID,
            caller_arn=operator.CALLER_ARN,
            region=operator.REGION,
            table_name=operator.STATE_TABLE,
            table_arn=(
                f"arn:aws:dynamodb:{operator.REGION}:{operator.ACCOUNT_ID}:"
                f"table/{operator.STATE_TABLE}"
            ),
            stack_id=(
                f"arn:aws:cloudformation:{operator.REGION}:{operator.ACCOUNT_ID}:"
                f"stack/{operator.STACK_NAME}/stack-id"
            ),
            stack_status="UPDATE_COMPLETE",
            release_manifest_fingerprint=RELEASE_FINGERPRINT,
        )

    def deployment_authority(self) -> operator.DeploymentAuthority:
        self.deployment_reads += 1
        return self.deployment

    def resolve_request_receipt(self, owner_id: str, job_id: str, key_digest: str):  # type: ignore[no-untyped-def]
        return self.requests.resolve_request_receipt(owner_id, job_id, key_digest)

    def load_request_authority(self, owner_id: str, job_id: str):  # type: ignore[no-untyped-def]
        return self.requests.load_request_authority(owner_id, job_id)

    def commit_request(self, transaction: PublicationRequestTransaction):  # type: ignore[no-untyped-def]
        self.commits += 1
        receipt = self.requests.commit_request(transaction)
        if self.execution is None:
            self.execution = InMemoryPublicationExecutionStore((transaction,))
        return receipt

    def load_execution_authority(self, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        assert self.execution is not None
        return self.execution.load_execution_authority(owner_id, aggregate_id)

    def load_source_authority(self, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        assert self.execution is not None
        return self.execution.load_source_authority(owner_id, aggregate_id)


@pytest.fixture
def workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Backend, Path, Path]:
    private = tmp_path / "phase712-private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(operator, "PRIVATE_ROOT", private)
    backend = Backend()
    monkeypatch.setattr(
        operator,
        "_profile_authorities",
        lambda release: (
            backend.profile,
            profile_eligibility_authority(
                backend.profile,
                release_manifest_fingerprint=release,
            ),
        ),
    )
    target = private / "target.local.json"
    target.write_text(
        json.dumps(
            {
                "format": operator.TARGET_FORMAT,
                "job_id": backend.authority.current_job.job_id,
                "label": "approved-demo-listing",
                "owner_id": backend.authority.current_job.owner_id,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    return backend, private, target


def _inspect(backend: Backend, private: Path, target: Path):  # type: ignore[no-untyped-def]
    prepared = private / "prepared"
    result = operator.inspect_target(
        target_path=target,
        output_root=prepared,
        backend=backend,
        clock=lambda: NOW,
    )
    return prepared, result


def _execute(
    backend: Backend,
    prepared: Path,
    plan_sha256: str,
    output: Path,
    *,
    now=NOW,  # type: ignore[no-untyped-def]
):
    return operator.execute_prepared(
        prepared_root=prepared,
        approval_binding_sha256=plan_sha256,
        confirmation=operator.EXECUTION_CONFIRMATION,
        output_root=output,
        backend_factory=lambda: backend,
        clock=lambda: now,
        environment={
            operator.LIVE_ENVIRONMENT_SWITCH: operator.LIVE_ENVIRONMENT_VALUE,
        },
    )


def test_inspect_is_read_only_and_writes_one_sanitized_plan(
    workspace: tuple[Backend, Path, Path],
) -> None:
    backend, private, target = workspace

    prepared, result = _inspect(backend, private, target)

    assert backend.commits == 0
    assert backend.execution is None
    assert backend.deployment_reads == 1
    assert result["status"] == "ready"
    assert result["target_label"] == "approved-demo-listing"
    plan = (prepared / operator.PLAN_FILENAME).read_bytes()
    command = (prepared / operator.COMMAND_FILENAME).read_bytes()
    assert result["plan_sha256"] == operator._digest(plan)
    assert backend.authority.current_job.owner_id.encode() not in plan
    assert backend.authority.current_job.job_id.encode() not in plan
    assert b"approved-demo-listing" in plan
    assert backend.authority.current_job.owner_id.encode() in command
    assert backend.authority.current_job.job_id.encode() in command
    assert (prepared.stat().st_mode & 0o077) == 0
    assert ((prepared / operator.PLAN_FILENAME).stat().st_mode & 0o077) == 0
    assert ((prepared / operator.COMMAND_FILENAME).stat().st_mode & 0o077) == 0


def test_main_inspect_never_constructs_the_execute_backend(
    workspace: tuple[Backend, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend, private, target = workspace
    execute_backend_constructions = 0

    def forbidden_execute_backend() -> Backend:
        nonlocal execute_backend_constructions
        execute_backend_constructions += 1
        return backend

    result = operator.main(
        [
            "inspect",
            "--target",
            str(target),
            "--output-root",
            str(private / "main-prepared"),
        ],
        inspect_backend_factory=lambda: backend,
        execute_backend_factory=forbidden_execute_backend,
        clock=lambda: NOW,
    )

    assert result == 0
    assert execute_backend_constructions == 0
    assert backend.commits == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_execute_delegates_one_request_and_emits_read_only_binding_and_private_invocation(
    workspace: tuple[Backend, Path, Path],
) -> None:
    backend, private, target = workspace
    prepared, inspected = _inspect(backend, private, target)

    result = _execute(
        backend,
        prepared,
        str(inspected["plan_sha256"]),
        private / "executed",
    )

    assert backend.commits == 1
    assert result["publication_request_transactions"] == 1
    assert result["provider_calls"] == 0
    binding_raw = (private / "executed" / operator.BINDING_FILENAME).read_bytes()
    binding = PublicationCanaryBinding.model_validate_json(binding_raw, strict=True)
    assert binding.mode is PublicationCanaryMode.READ_ONLY_PREFLIGHT
    assert binding.required_preflight_proof_fingerprint is None
    assert backend.authority.current_job.owner_id.encode() not in binding_raw
    assert backend.authority.current_job.job_id.encode() not in binding_raw
    invocation = json.loads((private / "executed" / operator.INVOCATION_FILENAME).read_bytes())
    exact_invocation = PublicationCanaryInvocation.model_validate_json(
        (private / "executed" / operator.INVOCATION_FILENAME).read_bytes(),
        strict=True,
    )
    assert invocation["owner_id"] == OWNER_ID
    assert invocation["aggregate_id"] == next(iter(backend.requests.aggregates))
    assert set(invocation) == {"aggregate_id", "owner_id"}
    assert exact_invocation.owner_id == invocation["owner_id"]
    assert exact_invocation.aggregate_id == invocation["aggregate_id"]
    source = build_canary_source_bundle(
        private / CANARY_SOURCE_DIRECTORY_NAME,
        canary_binding_path=private / "executed" / operator.BINDING_FILENAME,
    )
    assert (source / operator.BINDING_FILENAME).read_bytes() == binding_raw
    assert OWNER_ID not in json.dumps(result)
    assert backend.authority.current_job.job_id not in json.dumps(result)


def test_exact_execute_replay_reads_receipt_and_never_commits_again(
    workspace: tuple[Backend, Path, Path],
) -> None:
    backend, private, target = workspace
    prepared, inspected = _inspect(backend, private, target)
    plan_sha = str(inspected["plan_sha256"])
    first = _execute(backend, prepared, plan_sha, private / "first")

    second = _execute(backend, prepared, plan_sha, private / "second")

    assert backend.commits == 1
    assert first["publication_request_transactions"] == 1
    assert second["publication_request_transactions"] == 0
    assert (private / "first" / operator.BINDING_FILENAME).read_bytes() == (
        private / "second" / operator.BINDING_FILENAME
    ).read_bytes()
    assert (private / "first" / operator.INVOCATION_FILENAME).read_bytes() == (
        private / "second" / operator.INVOCATION_FILENAME
    ).read_bytes()


def test_exact_receipt_replay_can_recover_after_plan_expiry_without_a_second_write(
    workspace: tuple[Backend, Path, Path],
) -> None:
    backend, private, target = workspace
    prepared, inspected = _inspect(backend, private, target)
    plan_sha = str(inspected["plan_sha256"])
    _execute(backend, prepared, plan_sha, private / "first")

    recovered = _execute(
        backend,
        prepared,
        plan_sha,
        private / "recovered",
        now=NOW + operator.PLAN_TTL + timedelta(seconds=1),
    )

    assert backend.commits == 1
    assert recovered["publication_request_transactions"] == 0
    assert (private / "recovered" / operator.BINDING_FILENAME).is_file()


def test_exact_receipt_recovers_after_post_commit_readback_failure(
    workspace: tuple[Backend, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, private, target = workspace
    prepared, inspected = _inspect(backend, private, target)
    plan_sha = str(inspected["plan_sha256"])
    original_read = backend.load_execution_authority
    failures = 1

    def fail_once(owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("simulated readback interruption")
        return original_read(owner_id, aggregate_id)

    monkeypatch.setattr(backend, "load_execution_authority", fail_once)
    with pytest.raises(PublicationPreCallAuthorityError, match="pre-call authority"):
        _execute(backend, prepared, plan_sha, private / "interrupted")

    recovered = _execute(backend, prepared, plan_sha, private / "recovered-after-readback")

    assert backend.commits == 1
    assert recovered["publication_request_transactions"] == 0
    assert (private / "recovered-after-readback" / operator.BINDING_FILENAME).is_file()


def test_exact_receipt_recovery_preserves_binding_after_domain_deadline(
    workspace: tuple[Backend, Path, Path],
) -> None:
    backend, private, target = workspace
    prepared, inspected = _inspect(backend, private, target)
    plan_sha = str(inspected["plan_sha256"])
    _execute(backend, prepared, plan_sha, private / "first")

    recovered = _execute(
        backend,
        prepared,
        plan_sha,
        private / "recovered-after-deadline",
        now=NOW + timedelta(minutes=31),
    )

    assert backend.commits == 1
    assert recovered["publication_request_transactions"] == 0
    assert recovered["deployment_window_sufficient"] is False
    assert recovered["verification_window_remaining_seconds"] == -60
    assert (private / "recovered-after-deadline" / operator.BINDING_FILENAME).is_file()


@pytest.mark.parametrize("failure", ["confirmation", "sha", "environment"])
def test_local_gates_refuse_before_backend_construction(
    workspace: tuple[Backend, Path, Path],
    failure: str,
) -> None:
    backend, private, target = workspace
    prepared, inspected = _inspect(backend, private, target)
    factories = 0

    def factory() -> Backend:
        nonlocal factories
        factories += 1
        return backend

    confirmation = operator.EXECUTION_CONFIRMATION
    plan_sha = str(inspected["plan_sha256"])
    environment = {operator.LIVE_ENVIRONMENT_SWITCH: operator.LIVE_ENVIRONMENT_VALUE}
    if failure == "confirmation":
        confirmation = "wrong"
    elif failure == "sha":
        plan_sha = "0" * 64
    else:
        environment = {}

    with pytest.raises(operator.Phase712CanaryOperatorError):
        operator.execute_prepared(
            prepared_root=prepared,
            approval_binding_sha256=plan_sha,
            confirmation=confirmation,
            output_root=private / f"failed-{failure}",
            backend_factory=factory,
            clock=lambda: NOW,
            environment=environment,
        )

    assert factories == 0
    assert backend.commits == 0


def test_expired_plan_and_deployment_drift_fail_before_mutation(
    workspace: tuple[Backend, Path, Path],
) -> None:
    backend, private, target = workspace
    prepared, inspected = _inspect(backend, private, target)
    plan_sha = str(inspected["plan_sha256"])

    with pytest.raises(operator.Phase712CanaryOperatorError, match="expired"):
        _execute(
            backend,
            prepared,
            plan_sha,
            private / "expired",
            now=NOW + operator.PLAN_TTL + timedelta(seconds=1),
        )
    assert backend.commits == 0

    backend.deployment = replace(backend.deployment, release_manifest_fingerprint="e" * 64)
    with pytest.raises(operator.Phase712CanaryOperatorError, match="drifted"):
        _execute(backend, prepared, plan_sha, private / "drifted")
    assert backend.commits == 0


def test_source_closure_drift_refuses_before_backend_construction(
    workspace: tuple[Backend, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, private, target = workspace
    prepared, inspected = _inspect(backend, private, target)
    factories = 0

    def factory() -> Backend:
        nonlocal factories
        factories += 1
        return backend

    monkeypatch.setattr(operator, "_source_closure_sha256", lambda: "0" * 64)
    with pytest.raises(operator.Phase712CanaryOperatorError, match="source closure"):
        operator.execute_prepared(
            prepared_root=prepared,
            approval_binding_sha256=str(inspected["plan_sha256"]),
            confirmation=operator.EXECUTION_CONFIRMATION,
            output_root=private / "source-drift",
            backend_factory=factory,
            clock=lambda: NOW,
            environment={
                operator.LIVE_ENVIRONMENT_SWITCH: operator.LIVE_ENVIRONMENT_VALUE,
            },
        )

    assert factories == 0
    assert backend.commits == 0


def test_post_readback_clock_reports_the_real_remaining_window_without_losing_recovery(
    workspace: tuple[Backend, Path, Path],
) -> None:
    backend, private, target = workspace
    prepared, inspected = _inspect(backend, private, target)
    values = iter((NOW, NOW, NOW + timedelta(minutes=16)))

    result = operator.execute_prepared(
        prepared_root=prepared,
        approval_binding_sha256=str(inspected["plan_sha256"]),
        confirmation=operator.EXECUTION_CONFIRMATION,
        output_root=private / "stalled",
        backend_factory=lambda: backend,
        clock=lambda: next(values),
        environment={
            operator.LIVE_ENVIRONMENT_SWITCH: operator.LIVE_ENVIRONMENT_VALUE,
        },
    )

    assert backend.commits == 1
    assert result["verification_window_remaining_seconds"] == 14 * 60
    assert result["deployment_window_sufficient"] is False
    assert (private / "stalled" / operator.BINDING_FILENAME).is_file()
    assert (private / "stalled" / operator.INVOCATION_FILENAME).is_file()


def test_private_mode_path_and_no_overwrite_boundaries(
    workspace: tuple[Backend, Path, Path],
    tmp_path: Path,
) -> None:
    backend, private, target = workspace
    target.chmod(0o644)
    with pytest.raises(operator.Phase712CanaryOperatorError, match="mode-0600"):
        _inspect(backend, private, target)
    assert backend.deployment_reads == 0

    target.chmod(0o600)
    prepared, _result = _inspect(backend, private, target)
    with pytest.raises(operator.Phase712CanaryOperatorError, match="fresh private"):
        _inspect(backend, private, target)
    assert prepared.exists()

    outside = tmp_path / "outside"
    with pytest.raises(operator.Phase712CanaryOperatorError, match="repository-private"):
        operator.inspect_target(
            target_path=target,
            output_root=outside,
            backend=backend,
            clock=lambda: NOW,
        )


def test_operator_import_surface_has_no_provider_secret_or_deployment_capability() -> None:
    path = Path(operator.__file__).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(
        value.startswith(prefix)
        for value in imports
        for prefix in (
            "mr_lister.production",
            "mr_lister.publication.provider_boundary",
            "mr_lister.publication.provider_credentials",
            "mr_lister.publication.provider_runtime",
        )
    )
    source = path.read_text(encoding="utf-8")
    assert "commit_request" not in operator.AwsInspectBackend.__dict__
    assert "transact_write_items" not in operator._ReadOnlyDynamoDBClient.__dict__
    assert 'session.client("secretsmanager"' not in source
    assert 'session.client("lambda"' not in source
    assert 'session.client("s3"' not in source
