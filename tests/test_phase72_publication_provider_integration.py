"""Join the sealed provider boundary to the Phase 7.2 audit-watermark oracle."""

from __future__ import annotations

from typing import Any

import pytest

from mr_lister.publication.execution_models import (
    PublicationCallClaim,
    PublicationProviderAuditRecord,
)
from mr_lister.publication.execution_store import build_provider_audit_commit
from mr_lister.publication.provider_boundary import (
    OwnerBoundPrintifyCredential,
    PrintifyPublicationBoundary,
    PublicationProviderUnavailableError,
)
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness
from tests.test_phase72_publication_provider_boundary import (
    TOKEN,
    ScriptedTransport,
    _json_response,
)


class StoreBackedAuditSink:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.allowed: list[tuple[PublicationProviderAuditRecord, PublicationCallClaim]] = []
        self.rejected: list[PublicationProviderAuditRecord] = []

    def write_allowed(
        self,
        *,
        record: PublicationProviderAuditRecord,
        call_claim: PublicationCallClaim,
    ) -> None:
        commit = build_provider_audit_commit(self.harness.authority, call_claim, record)
        self.harness.store.commit_provider_audit(commit)
        self.allowed.append((record, call_claim))

    def write_rejected(self, record: PublicationProviderAuditRecord) -> None:
        self.rejected.append(record)


class AuditAssertingTransport(ScriptedTransport):
    def __init__(self, harness: Harness, responses: list[Any]) -> None:
        super().__init__(responses)
        self.harness = harness

    def request(self, **request: Any):  # type: ignore[no-untyped-def]
        authority = self.harness.authority
        assert authority.aggregate.provider_audit_record_version == 1
        assert len(authority.provider_audits) == 1
        return super().request(**request)


def _provider_boundary(
    harness: Harness,
    sink: StoreBackedAuditSink,
    transport: ScriptedTransport,
) -> PrintifyPublicationBoundary:
    authority = harness.authority.provider_authority
    assert authority is not None
    return PrintifyPublicationBoundary(
        authority=authority,
        credential=OwnerBoundPrintifyCredential(
            owner_id=OWNER_ID,
            printify_shop_id=authority.printify_shop_id,
            bearer_token=TOKEN,
        ),
        transport=transport,
        audit_sink=sink,
        clock=harness.clock,
    )


def test_allowed_audit_is_durable_before_wire_and_exact_replay_advances_once() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    claim_result, claim = harness.claim_shop(audit=False)
    assert claim_result.fresh_call_grant is not None
    sink = StoreBackedAuditSink(harness)
    authority = harness.authority.provider_authority
    assert authority is not None
    transport = AuditAssertingTransport(
        harness,
        [_json_response(200, [{"id": authority.printify_shop_id, "sales_channel": "etsy"}])],
    )
    boundary = _provider_boundary(harness, sink, transport)

    evidence = boundary.preflight_shop(
        call_claim=claim,
        fresh_grant=claim_result.fresh_call_grant,
    )

    assert evidence.call_claim_id == claim.authorization_id
    assert len(transport.calls) == 1
    assert len(sink.allowed) == 1
    first_record, first_claim = sink.allowed[0]
    sink.write_allowed(record=first_record, call_claim=first_claim)
    authority = harness.authority
    assert authority.aggregate.provider_audit_record_version == 1
    assert len(authority.provider_audits) == 1


def test_store_audit_failure_prevents_the_provider_wire_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    claim_result, claim = harness.claim_shop(audit=False)
    assert claim_result.fresh_call_grant is not None
    sink = StoreBackedAuditSink(harness)
    authority = harness.authority.provider_authority
    assert authority is not None
    transport = ScriptedTransport(
        [_json_response(200, [{"id": authority.printify_shop_id, "sales_channel": "etsy"}])]
    )
    boundary = _provider_boundary(harness, sink, transport)

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("private store failure")

    monkeypatch.setattr(harness.store, "commit_provider_audit", fail_audit)

    with pytest.raises(PublicationProviderUnavailableError, match="audit is unavailable"):
        boundary.preflight_shop(
            call_claim=claim,
            fresh_grant=claim_result.fresh_call_grant,
        )

    assert transport.calls == []
    assert harness.authority.aggregate.provider_audit_record_version == 0
    assert harness.authority.provider_audits == ()
