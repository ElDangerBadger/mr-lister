"""Adversarial tests for the read-only Phase 7.17 terminal verifier."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import tools.verify_phase717_canary_terminal as verifier
from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import ControlJobRecord
from mr_lister.publication.application import DynamoPublicationProjectionStore
from mr_lister.publication.canary_runtime import (
    PublicationCanaryInvocation,
    PublicationCanaryMode,
    build_publication_canary_binding,
)
from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_commands import (
    RecordPublicationPostOutcomeCommand,
    RecordPublicationProductObservationCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_models import (
    PublicationCallPurpose,
    PublicationExecutionAuthority,
)
from mr_lister.publication.projection import SellerPublicationProjectionService
from mr_lister.publication.projection_models import SellerPublicationProjection
from mr_lister.publication.store import PublicationRequestAuthority
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness


@dataclass
class JobStore:
    job: ControlJobRecord

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        if self.job.owner_id != owner_id or self.job.job_id != job_id:
            raise NotFoundError
        return self.job


@dataclass
class Backend:
    execution: PublicationExecutionAuthority
    source: PublicationRequestAuthority
    projection: SellerPublicationProjection
    calls: list[tuple[str, str, str]]

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        self.calls.append(("execution", owner_id, aggregate_id))
        return self.execution

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRequestAuthority:
        self.calls.append(("source", owner_id, aggregate_id))
        return self.source

    def load_seller_projection(
        self,
        owner_id: str,
        job_id: str,
    ) -> SellerPublicationProjection:
        self.calls.append(("projection", owner_id, job_id))
        return self.projection


@dataclass
class Case:
    harness: Harness
    backend: Backend
    private: Path
    binding_raw: bytes
    invocation_raw: bytes


def _projection(harness: Harness) -> SellerPublicationProjection:
    source = harness.store.load_source_authority(OWNER_ID, harness.aggregate_id)
    store = DynamoPublicationProjectionStore(
        jobs=JobStore(source.current_job),
        execution=harness.store,
    )
    return SellerPublicationProjectionService(store).get(
        owner_id=OWNER_ID,
        job_id=source.current_job.job_id,
    )


def _case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome: str = "published",
) -> Case:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    binding = build_publication_canary_binding(
        harness.authority,
        mode=PublicationCanaryMode.PUBLISH_ONCE,
    )
    invocation = PublicationCanaryInvocation(
        owner_id=OWNER_ID,
        aggregate_id=harness.aggregate_id,
    )
    binding_raw = verifier._canonical_json(binding.model_dump(mode="json"), pretty=True)
    invocation_raw = verifier._canonical_json(
        {"aggregate_id": invocation.aggregate_id, "owner_id": invocation.owner_id},
        pretty=True,
    )

    if outcome == "failed":
        harness.clock.now = harness.authority.snapshot.verification_deadline
        harness.service.settle_deadline(
            harness.command(SettlePublicationDeadlineCommand, "terminal_failed")
        )
    else:
        _, post_claim = harness.claim_publish()
        evidence = harness.publish_evidence(post_claim, accepted=outcome == "published")
        harness.clock.tick()
        harness.service.record_post_outcome(
            harness.command(
                RecordPublicationPostOutcomeCommand,
                "terminal_post",
                evidence=evidence,
            )
        )
        harness.clock.tick()
        purpose = (
            PublicationCallPurpose.VERIFICATION
            if outcome == "published"
            else PublicationCallPurpose.RECONCILIATION
        )
        _, product_claim = harness.claim_product(purpose)
        product = harness.product_evidence(product_claim, positive=outcome == "published")
        if outcome == "unknown":
            harness.clock.now = harness.authority.snapshot.verification_deadline
        else:
            harness.clock.tick()
        harness.service.record_product_observation(
            harness.command(
                RecordPublicationProductObservationCommand,
                "terminal_product",
                evidence=product,
            )
        )

    source = harness.store.load_source_authority(OWNER_ID, harness.aggregate_id)
    backend = Backend(harness.authority, source, _projection(harness), [])
    private_root = tmp_path / "phase717-publish-once"
    private_root.mkdir(mode=0o700)
    private = private_root / "approved"
    private.mkdir(mode=0o700)
    (private / verifier.BINDING_FILENAME).write_bytes(binding_raw)
    (private / verifier.INVOCATION_FILENAME).write_bytes(invocation_raw)
    (private / verifier.BINDING_FILENAME).chmod(0o600)
    (private / verifier.INVOCATION_FILENAME).chmod(0o600)
    monkeypatch.setattr(verifier, "PRIVATE_ROOT", private_root)
    return Case(harness, backend, private, binding_raw, invocation_raw)


def _verify(case: Case) -> dict[str, object]:
    return verifier.verify_terminal(
        publish_once_root=case.private,
        publish_once_binding_sha256=verifier._digest(case.binding_raw),
        private_invocation_sha256=verifier._digest(case.invocation_raw),
        backend_factory=lambda: case.backend,
    )


def test_exact_published_graph_returns_only_sanitized_fingerprints_and_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, monkeypatch)

    result = _verify(case)

    authority = case.harness.authority
    assert case.backend.calls == [
        ("execution", OWNER_ID, case.harness.aggregate_id),
        ("source", OWNER_ID, case.harness.aggregate_id),
        ("projection", OWNER_ID, authority.snapshot.job_id),
    ]
    assert result["status"] == "verified_published"
    assert result["publish_post_call_count"] == 1
    assert result["preflight_proof_fingerprint"] == authority.preflight_proof.fingerprint
    assert result["positive_observation_fingerprint"] == (
        authority.last_product_observation.fingerprint
    )
    assert result["result_fingerprint"] == authority.result.fingerprint
    assert result["notification_fingerprint"] == authority.notification.fingerprint
    assert result["report_fingerprint"] == authority.report.fingerprint
    assert result["tombstone_fingerprint"] == authority.tombstone.fingerprint
    assert result["terminal_job_link_fingerprint"] == authority.terminal_job_link.fingerprint
    rendered = json.dumps(result, sort_keys=True)
    for raw in (
        OWNER_ID,
        case.harness.aggregate_id,
        authority.snapshot.job_id,
        authority.result.safe_listing_url,
        str(authority.result.numeric_listing_id),
        authority.snapshot.printify_product_id,
        authority.snapshot.printify_image_id,
        str(authority.snapshot.printify_shop_id),
    ):
        assert raw not in rendered
    assert all(
        key == "status"
        or key.endswith("_fingerprint")
        or key.endswith("_count")
        or key == "seller_projection_etag"
        for key in result
    )


def test_main_uses_injected_read_backend_and_emits_sanitized_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = _case(tmp_path, monkeypatch)

    assert (
        verifier.main(
            [
                "--publish-once-root",
                str(case.private),
                "--publish-once-binding-sha256",
                verifier._digest(case.binding_raw),
                "--private-invocation-sha256",
                verifier._digest(case.invocation_raw),
            ],
            backend_factory=lambda: case.backend,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "verified_published"


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        ("failed", PublicationState.PUBLICATION_FAILED),
        ("unknown", PublicationState.PUBLICATION_OUTCOME_UNKNOWN),
    ],
)
def test_failed_and_unknown_terminal_graphs_are_explicitly_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    state: PublicationState,
) -> None:
    case = _case(tmp_path, monkeypatch, outcome=outcome)
    assert case.backend.execution.aggregate.state is state

    with pytest.raises(
        verifier.Phase717CanaryTerminalVerificationError,
        match="refused safely",
    ):
        _verify(case)

    assert [call[0] for call in case.backend.calls] == ["execution"]


def test_missing_terminal_record_is_rejected_before_source_or_projection_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, monkeypatch)
    case.backend.execution = case.backend.execution.model_copy(update={"notification": None})

    with pytest.raises(verifier.Phase717CanaryTerminalVerificationError, match="refused safely"):
        _verify(case)

    assert [call[0] for call in case.backend.calls] == ["execution"]


def test_more_than_one_publish_count_is_rejected_by_full_graph_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, monkeypatch)
    attempt = case.backend.execution.attempt.model_copy(update={"publish_post_call_count": 2})
    case.backend.execution = case.backend.execution.model_copy(update={"attempt": attempt})

    with pytest.raises(verifier.Phase717CanaryTerminalVerificationError, match="refused safely"):
        _verify(case)

    assert [call[0] for call in case.backend.calls] == ["execution"]


def test_current_source_authority_must_rebind_terminal_owner_job_and_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, monkeypatch)
    job = case.backend.source.current_job.model_copy(
        update={"publication_terminal_summary_fingerprint": "f" * 64}
    )
    case.backend.source = PublicationRequestAuthority(
        current_job=job,
        review=case.backend.source.review,
        approval_decision=case.backend.source.approval_decision,
        source=case.backend.source.source,
        product_sync=case.backend.source.product_sync,
        pricing_snapshot=case.backend.source.pricing_snapshot,
        pricing_evidence=case.backend.source.pricing_evidence,
    )

    with pytest.raises(verifier.Phase717CanaryTerminalVerificationError, match="refused safely"):
        _verify(case)

    assert [call[0] for call in case.backend.calls] == ["execution", "source"]


def test_seller_projection_must_match_exact_result_and_terminal_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, monkeypatch)
    case.backend.projection = case.backend.projection.model_copy(
        update={"safe_listing_url": "https://www.etsy.com/listing/999"}
    )

    with pytest.raises(verifier.Phase717CanaryTerminalVerificationError, match="refused safely"):
        _verify(case)

    assert [call[0] for call in case.backend.calls] == [
        "execution",
        "source",
        "projection",
    ]


def test_hash_or_private_artifact_drift_refuses_before_backend_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, monkeypatch)
    constructions = 0

    def backend_factory() -> Backend:
        nonlocal constructions
        constructions += 1
        return case.backend

    with pytest.raises(verifier.Phase717CanaryTerminalVerificationError, match="refused safely"):
        verifier.verify_terminal(
            publish_once_root=case.private,
            publish_once_binding_sha256="0" * 64,
            private_invocation_sha256=verifier._digest(case.invocation_raw),
            backend_factory=backend_factory,
        )

    assert constructions == 0
    assert case.backend.calls == []


def test_publish_once_binding_must_match_durable_preflight_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, monkeypatch)
    binding = json.loads(case.binding_raw)
    binding["required_preflight_proof_fingerprint"] = "e" * 64
    # A malformed fingerprinted binding must fail before any read rather than be trusted.
    tampered = verifier._canonical_json(binding, pretty=True)
    (case.private / verifier.BINDING_FILENAME).write_bytes(tampered)
    (case.private / verifier.BINDING_FILENAME).chmod(0o600)

    with pytest.raises(verifier.Phase717CanaryTerminalVerificationError, match="refused safely"):
        verifier.verify_terminal(
            publish_once_root=case.private,
            publish_once_binding_sha256=verifier._digest(tampered),
            private_invocation_sha256=verifier._digest(case.invocation_raw),
            backend_factory=lambda: case.backend,
        )

    assert case.backend.calls == []


def test_source_has_only_fixed_read_aws_and_no_provider_or_mutation_surface() -> None:
    source = Path(verifier.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    services = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "client"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert services == {"dynamodb", "sts"}
    assert called.isdisjoint(
        {
            "delete_item",
            "invoke",
            "put_item",
            "send_message",
            "transact_write_items",
            "update_item",
        }
    )
    assert imports.isdisjoint(
        {
            "mr_lister.publication.credential_resolver",
            "mr_lister.publication.provider_client",
            "mr_lister.publication.provider_transport",
        }
    )
    assert verifier.PROFILE == "mr-lister-dev"
    assert verifier.ACCOUNT_ID == "384627057108"
    assert verifier.REGION == "us-west-2"
    assert verifier.STATE_TABLE == "mr-lister-phase6-dev"
