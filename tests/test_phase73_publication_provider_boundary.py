"""Phase 7.3 sealed-boundary evidence provenance and negative-classifier gates."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from mr_lister.publication.evidence_provenance import (
    PublicationDefinitivePreflightEvidence,
    PublicationProviderEvidenceKind,
    PublicationProviderEvidenceStage,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    PublicationCallClaim,
    PublicationCallPurpose,
    PublicationExternalEvidenceState,
    PublicationPreflightFailureReason,
    PublicationProductReadEvidence,
    PublicationProviderAuditRecord,
    PublicationReadOutcome,
)
from mr_lister.publication.execution_store import build_provider_audit_commit
from mr_lister.publication.fingerprints import canonical_fingerprint
from mr_lister.publication.provider_boundary import (
    OwnerBoundPrintifyCredential,
    PublicationHttpResponse,
    PublicationProviderAuthenticationError,
    PublicationProviderResponseError,
    PublicationProviderUnavailableError,
    StagedPrintifyPublicationBoundary,
)
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness
from tests.test_phase72_publication_provider_boundary import (
    TOKEN,
    ScriptedTransport,
    _json_response,
)


class DurableAuditSink:
    def __init__(self, harness: Harness, events: list[str]) -> None:
        self._harness = harness
        self._events = events
        self.rejected: list[PublicationProviderAuditRecord] = []

    def write_allowed(
        self,
        *,
        record: PublicationProviderAuditRecord,
        call_claim: PublicationCallClaim,
    ):  # type: ignore[no-untyped-def]
        self._events.append("audit")
        commit = build_provider_audit_commit(self._harness.authority, call_claim, record)
        return self._harness.store.commit_provider_audit(commit)

    def write_rejected(self, record: PublicationProviderAuditRecord) -> None:
        self.rejected.append(record)


class OrderedTransport(ScriptedTransport):
    def __init__(self, responses: list[Any], events: list[str]) -> None:
        super().__init__(responses)
        self._events = events

    def request(self, **request: Any) -> PublicationHttpResponse:
        self._events.append("wire")
        return super().request(**request)


class OrderedExecutionStore:
    """Record staging order while delegating to the one shared execution store."""

    def __init__(self, harness: Harness, events: list[str]) -> None:
        self._harness = harness
        self._events = events

    def stage_evidence(self, commit: Any) -> PublicationProviderEvidenceStage:
        self._events.append("stage")
        return self._harness.store.stage_evidence(commit)


def _reader(harness: Harness) -> Callable[..., Any]:
    def read(*, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        return harness.store.load_execution_authority(owner_id, aggregate_id)

    return read


def _staged_boundary(
    harness: Harness,
    responses: list[Any],
    *,
    events: list[str] | None = None,
    evidence_store: Any | None = None,
) -> tuple[StagedPrintifyPublicationBoundary, ScriptedTransport, list[str]]:
    order = events if events is not None else []
    transport = OrderedTransport(responses, order)
    sink = DurableAuditSink(harness, order)
    provider_store = evidence_store or OrderedExecutionStore(harness, order)
    return (
        StagedPrintifyPublicationBoundary(
            execution_authority=harness.authority,
            credential=OwnerBoundPrintifyCredential(
                owner_id=OWNER_ID,
                bearer_token=TOKEN,
            ),
            transport=transport,
            audit_sink=sink,
            evidence_store=provider_store,
            authority_reader=_reader(harness),
            clock=harness.clock,
        ),
        transport,
        order,
    )


def test_boundary_orders_grant_audit_wire_classify_stage_and_returns_only_stage() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    result, claim = harness.claim_shop(audit=False)
    assert result.fresh_call_grant is not None
    authority = harness.authority.provider_authority
    assert authority is not None
    boundary, transport, events = _staged_boundary(
        harness,
        [_json_response(200, [{"id": authority.printify_shop_id, "sales_channel": "etsy"}])],
    )

    stage = boundary.preflight_shop(
        call_claim=claim,
        fresh_grant=result.fresh_call_grant,
    )

    assert type(stage) is PublicationProviderEvidenceStage
    assert stage.evidence_kind is PublicationProviderEvidenceKind.SHOP_PREFLIGHT
    assert stage.call_claim_id == claim.authorization_id
    assert events == ["audit", "wire", "stage"]
    assert len(transport.calls) == 1
    assert (
        harness.store.get_provider_evidence_stage(
            OWNER_ID,
            harness.aggregate_id,
            stage.stage_id,
        )
        == stage
    )
    serialized = stage.model_dump_json()
    assert TOKEN not in serialized
    assert OWNER_ID not in serialized


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            _json_response(200, [{"id": 999, "sales_channel": "etsy"}]),
            PublicationPreflightFailureReason.SHOP_NOT_CONNECTED_TO_ETSY,
        ),
        (
            _json_response(200, [{"id": 42, "sales_channel": "disconnected"}]),
            PublicationPreflightFailureReason.SHOP_NOT_CONNECTED_TO_ETSY,
        ),
    ],
)
def test_exact_shop_or_channel_mismatch_stages_structured_definitive_negative(
    response: PublicationHttpResponse,
    reason: PublicationPreflightFailureReason,
) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    result, claim = harness.claim_shop(audit=False)
    assert result.fresh_call_grant is not None
    boundary, _transport, events = _staged_boundary(harness, [response])

    stage = boundary.preflight_shop(
        call_claim=claim,
        fresh_grant=result.fresh_call_grant,
    )

    assert stage.evidence_kind is PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE
    assert isinstance(stage.evidence, PublicationDefinitivePreflightEvidence)
    assert stage.evidence.failure_reason is reason
    assert events == ["audit", "wire", "stage"]


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            PublicationHttpResponse(status=404, body=b""),
            PublicationPreflightFailureReason.EXACT_PRODUCT_NOT_FOUND,
        ),
    ],
)
def test_exact_product_closed_negative_classifier_stages_only_structured_reasons(
    response: PublicationHttpResponse,
    reason: PublicationPreflightFailureReason,
) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    result, claim = harness.claim_product(
        PublicationCallPurpose.PRODUCT_PREFLIGHT,
        audit=False,
    )
    assert result.fresh_call_grant is not None
    boundary, _transport, events = _staged_boundary(harness, [response])

    stage = boundary.preflight_exact_product(
        call_claim=claim,
        fresh_grant=result.fresh_call_grant,
    )

    assert stage.evidence_kind is PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE
    assert isinstance(stage.evidence, PublicationDefinitivePreflightEvidence)
    assert stage.evidence.failure_reason is reason
    assert events == ["audit", "wire", "stage"]


def _product_evidence(
    harness: Harness,
    claim: PublicationCallClaim,
    **changes: Any,
) -> PublicationProductReadEvidence:
    provider = harness.authority.provider_authority
    assert provider is not None
    values: dict[str, Any] = {
        "call_claim_id": claim.authorization_id,
        "call_claim_fingerprint": claim.fingerprint,
        "provider_authority_id": provider.provider_authority_id,
        "provider_authority_fingerprint": provider.fingerprint,
        "printify_shop_id": provider.printify_shop_id,
        "printify_product_id": provider.printify_product_id,
        "sanitized_response_fingerprint": canonical_fingerprint({"closed": True}),
        "product_present": True,
        "canonical_payload_fingerprint": provider.product_payload_fingerprint,
        "canonical_content_match": True,
        "exact_variant_economics": True,
        "exact_placement_image": True,
        "exact_mockups": True,
        "is_locked": False,
        "visible": True,
        "external_evidence": PublicationExternalEvidenceState.ABSENT,
        "numeric_listing_id": None,
        "read_outcome": PublicationReadOutcome.NOT_YET_PROVEN,
        "observed_at": harness.clock(),
    }
    values.update(changes)
    return PublicationProductReadEvidence(
        **values,
        fingerprint=execution_record_fingerprint("product_read_evidence", values),
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"is_locked": True},
            PublicationPreflightFailureReason.PRODUCT_LOCKED,
        ),
        (
            {
                "external_evidence": (
                    PublicationExternalEvidenceState.SINGLE_NUMERIC_ETSY_REFERENCE
                ),
                "numeric_listing_id": 123456789,
                "read_outcome": PublicationReadOutcome.POSITIVE_PROOF,
            },
            PublicationPreflightFailureReason.PRODUCT_ALREADY_PUBLISHED,
        ),
        (
            {
                "canonical_content_match": False,
                "read_outcome": PublicationReadOutcome.CONFLICTING_OR_INCOMPLETE,
            },
            PublicationPreflightFailureReason.CANONICAL_CONTENT_MISMATCH,
        ),
        (
            {
                "exact_variant_economics": False,
                "read_outcome": PublicationReadOutcome.CONFLICTING_OR_INCOMPLETE,
            },
            PublicationPreflightFailureReason.VARIANT_AUTHORITY_MISMATCH,
        ),
        (
            {
                "exact_mockups": False,
                "read_outcome": PublicationReadOutcome.CONFLICTING_OR_INCOMPLETE,
            },
            PublicationPreflightFailureReason.VARIANT_AUTHORITY_MISMATCH,
        ),
    ],
)
def test_structured_product_drift_is_staged_as_closed_negative_after_audited_read(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    reason: PublicationPreflightFailureReason,
) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    result, claim = harness.claim_product(
        PublicationCallPurpose.PRODUCT_PREFLIGHT,
        audit=False,
    )
    assert result.fresh_call_grant is not None
    boundary, _transport, events = _staged_boundary(harness, [])
    _commit, audit_binding = harness.audit(claim)
    boundary._boundary._durable_audit_bindings[claim.authorization_id] = audit_binding
    evidence = _product_evidence(harness, claim, **changes)

    def exact_read(**_values: Any) -> PublicationProductReadEvidence:
        result.fresh_call_grant.consume_once(claim)
        return evidence

    monkeypatch.setattr(boundary._boundary, "_read_exact_product", exact_read)

    stage = boundary.preflight_exact_product(
        call_claim=claim,
        fresh_grant=result.fresh_call_grant,
    )

    assert stage.evidence_kind is PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE
    assert isinstance(stage.evidence, PublicationDefinitivePreflightEvidence)
    assert stage.evidence.failure_reason is reason
    assert events == ["stage"]


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (PublicationHttpResponse(status=401, body=b""), PublicationProviderAuthenticationError),
        (PublicationHttpResponse(status=500, body=b""), PublicationProviderUnavailableError),
        (PublicationHttpResponse(status=200, body=b"not-json"), PublicationProviderResponseError),
    ],
)
def test_auth_transient_and_malformed_reads_never_stage_definitive_negative(
    response: PublicationHttpResponse,
    error: type[Exception],
) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    result, claim = harness.claim_product(
        PublicationCallPurpose.PRODUCT_PREFLIGHT,
        audit=False,
    )
    assert result.fresh_call_grant is not None
    boundary, _transport, events = _staged_boundary(harness, [response])

    with pytest.raises(error):
        boundary.preflight_exact_product(
            call_claim=claim,
            fresh_grant=result.fresh_call_grant,
        )

    assert events == ["audit", "wire"]
    assert harness.store.list_unconsumed_provider_evidence(OWNER_ID, harness.aggregate_id) == ()


class MissingDurableAuditSink:
    def write_allowed(self, **_values: Any) -> None:
        return None

    def write_rejected(self, _record: PublicationProviderAuditRecord) -> None:
        return None


def test_missing_durable_audit_binding_blocks_wire_and_staging() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    result, claim = harness.claim_shop(audit=False)
    assert result.fresh_call_grant is not None
    transport = ScriptedTransport([_json_response(200, [])])
    boundary = StagedPrintifyPublicationBoundary(
        execution_authority=harness.authority,
        credential=OwnerBoundPrintifyCredential(owner_id=OWNER_ID, bearer_token=TOKEN),
        transport=transport,
        audit_sink=MissingDurableAuditSink(),
        evidence_store=harness.store,
        authority_reader=_reader(harness),
        clock=harness.clock,
    )

    with pytest.raises(PublicationProviderUnavailableError, match="durable authority"):
        boundary.preflight_shop(
            call_claim=claim,
            fresh_grant=result.fresh_call_grant,
        )

    assert transport.calls == []
    assert harness.store.list_unconsumed_provider_evidence(OWNER_ID, harness.aggregate_id) == ()


class FailingEvidenceStore:
    def stage_evidence(self, _commit: Any) -> PublicationProviderEvidenceStage:
        raise RuntimeError("private persistence detail")


def test_stage_failure_occurs_after_one_wire_but_never_returns_loose_evidence() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    result, claim = harness.claim_shop(audit=False)
    assert result.fresh_call_grant is not None
    boundary, transport, events = _staged_boundary(
        harness,
        [_json_response(200, [{"id": 42, "sales_channel": "etsy"}])],
        evidence_store=FailingEvidenceStore(),
    )

    with pytest.raises(PublicationProviderUnavailableError, match="could not be staged") as failure:
        boundary.preflight_shop(
            call_claim=claim,
            fresh_grant=result.fresh_call_grant,
        )

    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert len(transport.calls) == 1
    assert events == ["audit", "wire"]
