"""Focused tests for the dependency-injected Phase 7.8 provider-runtime join."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import SecretStr

from mr_lister.publication.evidence_provenance import PublicationProviderEvidenceStage
from mr_lister.publication.execution_models import (
    PublicationProviderAuditDecision,
    PublicationProviderAuditRecord,
)
from mr_lister.publication.provider_boundary import (
    PublicationProviderInputError,
    StagedPrintifyPublicationBoundary,
)
from mr_lister.publication.provider_credentials import (
    issue_bound_publication_provider_credential,
)
from mr_lister.publication.provider_runtime import (
    PublicationProviderRuntimeError,
    PublicationProviderRuntimeFactory,
)
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness
from tests.test_phase72_publication_provider_boundary import (
    TOKEN,
    ScriptedTransport,
    _json_response,
)

ROOT = Path(__file__).resolve().parents[1]
RELEASE_FINGERPRINT = "b" * 64
USER_AGENT = "MrLister-Phase7/phase78-test"
TIMEOUT_SECONDS = 11.0


class RecordingCredentials:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def resolve_exact(self, *, authority):  # type: ignore[no-untyped-def]
        self.calls.append(authority)
        return issue_bound_publication_provider_credential(
            authority=authority,
            bearer_token=SecretStr(TOKEN),
        )


def _factory(
    harness: Harness,
    transport: ScriptedTransport,
    *,
    credentials: RecordingCredentials | None = None,
    rejected: list[PublicationProviderAuditRecord] | None = None,
    release_fingerprint: str = RELEASE_FINGERPRINT,
) -> tuple[
    PublicationProviderRuntimeFactory,
    RecordingCredentials,
    list[PublicationProviderAuditRecord],
]:
    credential_authority = credentials or RecordingCredentials()
    rejected_records = rejected if rejected is not None else []
    return (
        PublicationProviderRuntimeFactory(
            store=harness.store,
            credentials=credential_authority,
            transport=transport,
            release_manifest_fingerprint=release_fingerprint,
            rejected_audit_writer=rejected_records.append,
            clock=harness.clock,
            timeout_seconds=TIMEOUT_SECONDS,
            user_agent=USER_AGENT,
        ),
        credential_authority,
        rejected_records,
    )


def test_factory_and_boundary_construction_are_io_free() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    authority = harness.authority
    provider_authority = authority.provider_authority
    assert provider_authority is not None
    transport = ScriptedTransport([])
    credentials = RecordingCredentials()
    rejected: list[PublicationProviderAuditRecord] = []

    factory, _, _ = _factory(
        harness,
        transport,
        credentials=credentials,
        rejected=rejected,
    )
    credential = issue_bound_publication_provider_credential(
        authority=provider_authority,
        bearer_token=SecretStr(TOKEN),
    )
    boundary = factory(execution_authority=authority, credential=credential)

    assert isinstance(boundary, StagedPrintifyPublicationBoundary)
    assert credentials.calls == []
    assert transport.calls == []
    assert rejected == []
    assert harness.authority.provider_audits == ()
    assert harness.store.list_unconsumed_provider_evidence(OWNER_ID, harness.aggregate_id) == ()


def test_factory_prepares_before_claim_then_durably_audits_and_stages_one_wire_result() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    authority = harness.authority
    provider_authority = authority.provider_authority
    assert provider_authority is not None
    transport = ScriptedTransport(
        [
            _json_response(
                200, [{"id": provider_authority.printify_shop_id, "sales_channel": "etsy"}]
            )
        ]
    )
    factory, credentials, rejected = _factory(harness, transport)

    credential = factory.prepare_credential(execution_authority=authority)
    result, claim = harness.claim_shop(audit=False)
    assert result.fresh_call_grant is not None
    boundary = factory(execution_authority=harness.authority, credential=credential)
    stage = boundary.preflight_shop(
        call_claim=claim,
        fresh_grant=result.fresh_call_grant,
    )

    assert isinstance(stage, PublicationProviderEvidenceStage)
    assert credentials.calls == [provider_authority]
    assert rejected == []
    assert len(transport.calls) == 1
    request = transport.calls[0]
    assert request["timeout_seconds"] == TIMEOUT_SECONDS
    assert request["headers"]["User-Agent"] == USER_AGENT
    current = harness.authority
    assert len(current.provider_audits) == 1
    assert current.provider_audits[0].call_claim_id == claim.authorization_id
    assert (
        harness.store.get_provider_evidence_stage(
            OWNER_ID,
            harness.aggregate_id,
            stage.stage_id,
        )
        == stage
    )


def test_mismatched_claim_writes_only_sanitized_rejection_and_never_calls_wire() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    authority = harness.authority
    transport = ScriptedTransport([])
    factory, _, rejected = _factory(harness, transport)
    credential = factory.prepare_credential(execution_authority=authority)
    result, shop_claim = harness.claim_shop(audit=False)
    assert result.fresh_call_grant is not None
    boundary = factory(execution_authority=harness.authority, credential=credential)

    with pytest.raises(PublicationProviderInputError, match="differs"):
        boundary.preflight_exact_product(
            call_claim=shop_claim,
            fresh_grant=result.fresh_call_grant,
        )

    assert transport.calls == []
    assert len(rejected) == 1
    record = rejected[0]
    assert record.decision is PublicationProviderAuditDecision.REJECTED
    assert set(type(record).model_fields) == {
        "contract_version",
        "decision",
        "method_category",
        "route_template",
        "category",
        "fingerprint",
    }
    assert harness.authority.provider_audits == ()


def test_release_drift_fails_before_credential_store_or_provider_capability_use() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    transport = ScriptedTransport([])
    factory, credentials, rejected = _factory(
        harness,
        transport,
        release_fingerprint="c" * 64,
    )

    with pytest.raises(PublicationProviderRuntimeError) as captured:
        factory.prepare_credential(execution_authority=harness.authority)

    assert str(captured.value) == "Publication provider runtime authority is invalid"
    assert captured.value.__cause__ is None
    assert credentials.calls == []
    assert transport.calls == []
    assert rejected == []
    assert harness.authority.provider_audits == ()


@pytest.mark.parametrize(
    ("timeout_seconds", "user_agent"),
    [
        (0.0, USER_AGENT),
        (61.0, USER_AGENT),
        (TIMEOUT_SECONDS, "unsafe\nagent"),
    ],
)
def test_invalid_exact_transport_settings_fail_during_io_free_construction(
    timeout_seconds: float,
    user_agent: str,
) -> None:
    harness = Harness()
    transport = ScriptedTransport([])
    credentials = RecordingCredentials()
    rejected: list[PublicationProviderAuditRecord] = []

    with pytest.raises(PublicationProviderRuntimeError) as captured:
        PublicationProviderRuntimeFactory(
            store=harness.store,
            credentials=credentials,
            transport=transport,
            release_manifest_fingerprint=RELEASE_FINGERPRINT,
            rejected_audit_writer=rejected.append,
            clock=harness.clock,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
        )

    assert str(captured.value) == "Publication provider runtime configuration is invalid"
    assert credentials.calls == []
    assert transport.calls == []
    assert rejected == []


def test_runtime_join_imports_no_sdk_secret_route_handler_or_infrastructure_capability() -> None:
    path = ROOT / "src/mr_lister/publication/provider_runtime.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = {
        "boto3",
        "botocore",
        "mr_lister.cloud",
        "mr_lister.production.provider_secrets",
    }

    assert not any(
        imported == capability or imported.startswith(f"{capability}.")
        for imported in imports
        for capability in forbidden
    )
