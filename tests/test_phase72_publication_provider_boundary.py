from __future__ import annotations

import inspect
import json
import pickle
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    PublicationCallClaim,
    PublicationCallKind,
    PublicationCallPurpose,
    PublicationMutationClaim,
    PublicationPostOutcome,
    PublicationPreflightProof,
    PublicationProviderAuditCategory,
    PublicationProviderAuditDecision,
    PublicationProviderAuditRecord,
    PublicationReadOutcome,
)
from mr_lister.publication.execution_store import (
    FreshPublicationCallGrant,
    FreshPublicationMutationGrant,
)
from mr_lister.publication.fingerprints import (
    canonical_fingerprint,
    publication_body_fingerprint,
)
from mr_lister.publication.provider_boundary import (
    MAX_PUBLICATION_RESPONSE_BYTES,
    OUTSIDE_ROUTE_TEMPLATE,
    PRODUCT_ROUTE_TEMPLATE,
    PUBLICATION_BODY_BYTES,
    PUBLISH_ROUTE_TEMPLATE,
    SHOP_ROUTE_TEMPLATE,
    ExpectedVariantEconomics,
    ExternalEvidenceState,
    OwnerBoundPrintifyCredential,
    PrintifyProductObservation,
    PrintifyPublicationBoundary,
    PublicationHttpResponse,
    PublicationProviderAuthenticationError,
    PublicationProviderAuthority,
    PublicationProviderInputError,
    PublicationProviderPreflightError,
    PublicationProviderResponseError,
    PublicationProviderUnavailableError,
    PublishResponseCategory,
    RedirectSafePublicationTransport,
    SanitizedPublicationAuditTransport,
    assert_publication_api_url,
    printify_mockup_fingerprint,
)

OWNER = "1" * 64
NOW = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=30)
SNAPSHOT_FINGERPRINT = "2" * 64
CONSUMED_PERMIT_FINGERPRINT = "3" * 64
TOKEN = "owner-bound-printify-token-never-log"

MOCKUP_URL = "https://images.printify.com/mockup/product_1/101/front.png"


class ScriptedTransport:
    def __init__(self, responses: list[PublicationHttpResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, **request: Any) -> PublicationHttpResponse:
        self.calls.append(request)
        if not self.responses:
            raise AssertionError("Unexpected provider wire call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MemoryAudit:
    def __init__(self) -> None:
        self.records: list[PublicationProviderAuditRecord] = []
        self.allowed_claims: list[PublicationCallClaim] = []

    def write_allowed(
        self,
        *,
        record: PublicationProviderAuditRecord,
        call_claim: PublicationCallClaim,
    ) -> None:
        self.records.append(record)
        self.allowed_claims.append(call_claim)

    def write_rejected(self, record: PublicationProviderAuditRecord) -> None:
        self.records.append(record)


class FailingAudit:
    def write_allowed(
        self,
        *,
        record: PublicationProviderAuditRecord,
        call_claim: PublicationCallClaim,
    ) -> None:
        del record, call_claim
        raise RuntimeError("private audit backend detail")

    def write_rejected(self, record: PublicationProviderAuditRecord) -> None:
        del record
        raise RuntimeError("private audit backend detail")


def _json_response(status: int, payload: Any) -> PublicationHttpResponse:
    return PublicationHttpResponse(
        status=status,
        body=json.dumps(payload, separators=(",", ":")).encode(),
    )


def _canonical_payload() -> dict[str, Any]:
    return {
        "title": "Exact approved title",
        "description": "Exact approved description",
        "tags": ["gift", "shirt"],
        "blueprint_id": 145,
        "print_provider_id": 39,
        "variants": [
            {
                "id": 101,
                "price": 2499,
                "is_enabled": True,
                "sku": "ml-abcdef0123456789abcdef01-101",
            }
        ],
        "print_areas": [
            {
                "variant_ids": [101],
                "placeholders": [
                    {
                        "position": "front",
                        "images": [
                            {
                                "id": "image_1",
                                "x": 0.5,
                                "y": 0.5,
                                "scale": 0.9,
                                "angle": 0,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _product(*, external: Any = None) -> dict[str, Any]:
    payload = {
        "id": "product_1",
        "shop_id": 42,
        **_canonical_payload(),
        "variants": [
            {
                **_canonical_payload()["variants"][0],
                "cost": 1200,
                "title": "Black / M",
            }
        ],
        "images": [
            {
                "src": MOCKUP_URL,
                "position": "front",
                "variant_ids": [101],
                "is_default": True,
            }
        ],
        "is_locked": False,
        "visible": True,
    }
    if external is not None:
        payload["external"] = external
    return payload


def _authority(**changes: Any) -> PublicationProviderAuthority:
    values = {
        "provider_authority_id": "provider_authority_1",
        "owner_id": OWNER,
        "job_id": "job_1",
        "aggregate_id": "aggregate_1",
        "attempt_id": "attempt_1",
        "snapshot_id": "snapshot_1",
        "snapshot_fingerprint": SNAPSHOT_FINGERPRINT,
        "permit_id": "permit_1",
        "work_request_id": "work_1",
        "phase6_record_version": 12,
        "approval_fingerprint": "6" * 64,
        "review_fingerprint": "7" * 64,
        "product_sync_fingerprint": "8" * 64,
        "pricing_snapshot_fingerprint": "9" * 64,
        "pricing_evidence_fingerprint": "a" * 64,
        "profile_fingerprint": "b" * 64,
        "release_manifest_fingerprint": "c" * 64,
        "printify_shop_id": 42,
        "printify_product_id": "product_1",
        "printify_image_id": "image_1",
        "product_payload_fingerprint": canonical_fingerprint(_canonical_payload()),
        "expected_variant_economics": (
            ExpectedVariantEconomics(
                variant_id=101,
                retail_price_cents=2499,
                production_cost_cents=1200,
            ),
        ),
        "expected_mockup_fingerprints": (
            printify_mockup_fingerprint(
                url=MOCKUP_URL,
                position="front",
                variant_ids=(101,),
            ),
        ),
        "expected_sales_channel": "etsy",
        "publication_body_fingerprint": publication_body_fingerprint(),
        "pricing_fresh_until": DEADLINE,
        "reconstructed_at": NOW,
        "verification_deadline": DEADLINE,
    }
    values.update(changes)
    fingerprint = values.pop("fingerprint", None)
    if fingerprint is None:
        fingerprint = execution_record_fingerprint("provider_authority", values)
    return PublicationProviderAuthority(**values, fingerprint=fingerprint)


def _claim(
    authority: PublicationProviderAuthority,
    *,
    kind: PublicationCallKind,
    purpose: PublicationCallPurpose,
    suffix: str,
    ordinal: int = 1,
    authorized_at: datetime = NOW,
) -> PublicationCallClaim:
    if kind is PublicationCallKind.SHOP_GET:
        method = "GET"
        route = SHOP_ROUTE_TEMPLATE
        product_id = None
        call_limit = 3
        permit_fingerprint = None
        mutation_authorized = False
    elif kind is PublicationCallKind.PRODUCT_GET:
        method = "GET"
        route = PRODUCT_ROUTE_TEMPLATE
        product_id = authority.printify_product_id
        call_limit = 100
        permit_fingerprint = None
        mutation_authorized = False
    else:
        method = "POST"
        route = PUBLISH_ROUTE_TEMPLATE
        product_id = authority.printify_product_id
        call_limit = 1
        permit_fingerprint = CONSUMED_PERMIT_FINGERPRINT
        mutation_authorized = True
    values = {
        "authorization_id": f"call_{suffix}",
        "operation_id": f"operation_{suffix}",
        "aggregate_id": authority.aggregate_id,
        "attempt_id": authority.attempt_id,
        "snapshot_id": authority.snapshot_id,
        "snapshot_fingerprint": authority.snapshot_fingerprint,
        "permit_id": authority.permit_id,
        "work_request_id": authority.work_request_id,
        "owner_id": authority.owner_id,
        "job_id": authority.job_id,
        "call_kind": kind,
        "method": method,
        "route_template": route,
        "purpose": purpose,
        "printify_shop_id": authority.printify_shop_id,
        "printify_product_id": product_id,
        "ordinal": ordinal,
        "call_limit": call_limit,
        "resulting_attempt_record_version": ordinal,
        "permit_fingerprint": permit_fingerprint,
        "authorized_at": authorized_at,
        "verification_deadline": authority.verification_deadline,
        "mutation_authorized": mutation_authorized,
    }
    return PublicationCallClaim(
        **values,
        fingerprint=execution_record_fingerprint("call_claim", values),
    )


def _preflight_proof(
    authority: PublicationProviderAuthority,
    *,
    proof_id: str = "proof_1",
) -> PublicationPreflightProof:
    values = {
        "proof_id": proof_id,
        "aggregate_id": authority.aggregate_id,
        "attempt_id": authority.attempt_id,
        "snapshot_id": authority.snapshot_id,
        "snapshot_fingerprint": authority.snapshot_fingerprint,
        "provider_authority_id": authority.provider_authority_id,
        "provider_authority_fingerprint": authority.fingerprint,
        "shop_evidence_fingerprint": "d" * 64,
        "product_evidence_fingerprint": "e" * 64,
        "shop_observed_at": NOW,
        "product_observed_at": NOW,
        "shop_call_claim_id": "call_shop",
        "shop_call_claim_fingerprint": "4" * 64,
        "product_call_claim_id": "call_product",
        "product_call_claim_fingerprint": "5" * 64,
        "printify_shop_id": authority.printify_shop_id,
        "printify_product_id": authority.printify_product_id,
        "local_authority_reconstructed": True,
        "shop_connected_to_etsy": True,
        "exact_product_match": True,
        "canonical_content_match": True,
        "exact_variants_match": True,
        "product_unlocked": True,
        "product_unpublished": True,
        "publication_body_fingerprint": authority.publication_body_fingerprint,
        "proven_at": NOW,
        "verification_deadline": authority.verification_deadline,
    }
    return PublicationPreflightProof(
        **values,
        fingerprint=execution_record_fingerprint("preflight_proof", values),
    )


def _mutation(
    authority: PublicationProviderAuthority,
    call_claim: PublicationCallClaim,
    *,
    proof: PublicationPreflightProof | None = None,
) -> PublicationMutationClaim:
    exact_proof = proof or _preflight_proof(authority)
    values = {
        "mutation_claim_id": "mutation_1",
        "call_claim_id": call_claim.authorization_id,
        "call_claim_fingerprint": call_claim.fingerprint,
        "aggregate_id": authority.aggregate_id,
        "attempt_id": authority.attempt_id,
        "snapshot_id": authority.snapshot_id,
        "snapshot_fingerprint": authority.snapshot_fingerprint,
        "permit_id": authority.permit_id,
        "work_request_id": authority.work_request_id,
        "preflight_proof_id": exact_proof.proof_id,
        "preflight_proof_fingerprint": exact_proof.fingerprint,
        "consumed_permit_fingerprint": CONSUMED_PERMIT_FINGERPRINT,
        "publication_body_fingerprint": authority.publication_body_fingerprint,
        "ordinal": 1,
        "authorized_at": NOW + timedelta(seconds=1),
        "verification_deadline": authority.verification_deadline,
    }
    return PublicationMutationClaim(
        **values,
        fingerprint=execution_record_fingerprint("mutation_claim", values),
    )


def _boundary(
    responses: list[PublicationHttpResponse | Exception],
    *,
    authority: PublicationProviderAuthority | None = None,
    clock: Any = None,
) -> tuple[PrintifyPublicationBoundary, ScriptedTransport, MemoryAudit]:
    exact_authority = authority or _authority()
    transport = ScriptedTransport(responses)
    audit = MemoryAudit()
    boundary = PrintifyPublicationBoundary(
        authority=exact_authority,
        credential=OwnerBoundPrintifyCredential(
            owner_id=exact_authority.owner_id, bearer_token=TOKEN
        ),
        transport=transport,
        audit_sink=audit,
        clock=clock or (lambda: NOW + timedelta(seconds=2)),
    )
    return boundary, transport, audit


def _fresh(claim: PublicationCallClaim) -> FreshPublicationCallGrant:
    return FreshPublicationCallGrant._mint(claim)


def _fresh_mutation(
    claim: PublicationCallClaim,
    mutation: PublicationMutationClaim,
) -> FreshPublicationMutationGrant:
    return FreshPublicationMutationGrant._mint(claim, mutation)


def test_exact_shop_product_and_one_publish_flow_has_only_three_sanitized_routes() -> None:
    authority = _authority()
    shop_claim = _claim(
        authority,
        kind=PublicationCallKind.SHOP_GET,
        purpose=PublicationCallPurpose.SHOP_PREFLIGHT,
        suffix="shop",
    )
    product_claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.PRODUCT_PREFLIGHT,
        suffix="product",
    )
    publish_claim = _claim(
        authority,
        kind=PublicationCallKind.PUBLISH_POST,
        purpose=PublicationCallPurpose.PUBLISH,
        suffix="publish",
        authorized_at=NOW + timedelta(seconds=1),
    )
    mutation = _mutation(authority, publish_claim)
    boundary, transport, audit = _boundary(
        [
            _json_response(200, [{"id": 42, "title": "Etsy", "sales_channel": "etsy"}]),
            _json_response(200, _product(external=[])),
            _json_response(200, {}),
        ],
        authority=authority,
    )

    shop = boundary.preflight_shop(call_claim=shop_claim, fresh_grant=_fresh(shop_claim))
    product = boundary.preflight_exact_product(
        call_claim=product_claim,
        fresh_grant=_fresh(product_claim),
    )
    published = boundary.publish_exact_product(
        call_claim=publish_claim,
        mutation_claim=mutation,
        preflight_proof=_preflight_proof(authority),
        fresh_grant=_fresh_mutation(publish_claim, mutation),
    )

    assert shop.sales_channel == "etsy"
    assert product.preflight_satisfied
    assert product.read_outcome is PublicationReadOutcome.NOT_YET_PROVEN
    assert published.outcome is PublicationPostOutcome.DEFINITELY_ACCEPTED
    assert published.response_category is PublishResponseCategory.VALIDATED_2XX
    assert [call["method"] for call in transport.calls] == ["GET", "GET", "POST"]
    assert [call["url"] for call in transport.calls] == [
        "https://api.printify.com/v1/shops.json",
        "https://api.printify.com/v1/shops/42/products/product_1.json",
        "https://api.printify.com/v1/shops/42/products/product_1/publish.json",
    ]
    assert transport.calls[-1]["body"] == PUBLICATION_BODY_BYTES
    assert json.loads(PUBLICATION_BODY_BYTES) == {
        "title": True,
        "description": True,
        "images": True,
        "variants": True,
        "tags": True,
        "keyFeatures": True,
        "shipping_template": True,
    }
    assert [
        (
            record.decision,
            record.method_category,
            record.route_template,
            record.category,
        )
        for record in audit.records
    ] == [
        (
            PublicationProviderAuditDecision.ALLOWED,
            "GET",
            SHOP_ROUTE_TEMPLATE,
            PublicationProviderAuditCategory.SHOP_GET_ALLOWED,
        ),
        (
            PublicationProviderAuditDecision.ALLOWED,
            "GET",
            PRODUCT_ROUTE_TEMPLATE,
            PublicationProviderAuditCategory.PRODUCT_GET_ALLOWED,
        ),
        (
            PublicationProviderAuditDecision.ALLOWED,
            "POST",
            PUBLISH_ROUTE_TEMPLATE,
            PublicationProviderAuditCategory.PUBLISH_POST_ALLOWED,
        ),
    ]
    assert [claim.fingerprint for claim in audit.allowed_claims] == [
        shop_claim.fingerprint,
        product_claim.fingerprint,
        publish_claim.fingerprint,
    ]
    serialized_ledger = "".join(record.model_dump_json() for record in audit.records)
    assert OWNER not in serialized_ledger
    assert TOKEN not in serialized_ledger
    assert "product_1" not in serialized_ledger
    assert "/42/" not in serialized_ledger
    assert "printify_shop_id" not in serialized_ledger


def test_fresh_call_grant_and_session_latch_reject_replay_before_a_second_wire() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.VERIFICATION,
        suffix="poll",
    )
    grant = _fresh(claim)
    boundary, transport, audit = _boundary([_json_response(200, _product(external=[]))])

    boundary.poll_exact_product(call_claim=claim, fresh_grant=grant)
    with pytest.raises(PublicationProviderInputError, match="already used|fresh exact"):
        boundary.poll_exact_product(call_claim=claim, fresh_grant=grant)

    assert len(transport.calls) == 1
    assert audit.records[-1].decision is PublicationProviderAuditDecision.REJECTED
    assert audit.records[-1].category is PublicationProviderAuditCategory.STALE_OR_REPLAYED_GRANT
    assert audit.records[-1].method_category == "FORBIDDEN"
    assert audit.records[-1].route_template == OUTSIDE_ROUTE_TEMPLATE
    with pytest.raises(TypeError, match="serialize"):
        pickle.dumps(grant)


def test_publish_requires_fresh_mutation_grant_not_only_durable_records() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PUBLISH_POST,
        purpose=PublicationCallPurpose.PUBLISH,
        suffix="publish",
        authorized_at=NOW + timedelta(seconds=1),
    )
    mutation = _mutation(authority, claim)
    boundary, transport, audit = _boundary([])

    with pytest.raises(PublicationProviderInputError, match="fresh.*mutation"):
        boundary.publish_exact_product(
            call_claim=claim,
            mutation_claim=mutation,
            preflight_proof=_preflight_proof(authority),
            fresh_grant=_fresh(claim),  # type: ignore[arg-type]
        )

    assert transport.calls == []
    assert audit.records[-1].decision == "rejected"


def test_publish_requires_the_exact_preflight_proof_bound_into_mutation_claim() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PUBLISH_POST,
        purpose=PublicationCallPurpose.PUBLISH,
        suffix="publish",
        authorized_at=NOW + timedelta(seconds=1),
    )
    bound_proof = _preflight_proof(authority, proof_id="proof_bound")
    unrelated_proof = _preflight_proof(authority, proof_id="proof_unrelated")
    mutation = _mutation(authority, claim, proof=bound_proof)
    grant = _fresh_mutation(claim, mutation)
    boundary, transport, audit = _boundary([])

    with pytest.raises(PublicationProviderInputError, match="preflight authority"):
        boundary.publish_exact_product(
            call_claim=claim,
            mutation_claim=mutation,
            preflight_proof=unrelated_proof,
            fresh_grant=grant,
        )

    assert transport.calls == []
    assert audit.records[-1].decision == "rejected"
    active, active_transport, _audit = _boundary([_json_response(200, {})])
    accepted = active.publish_exact_product(
        call_claim=claim,
        mutation_claim=mutation,
        preflight_proof=bound_proof,
        fresh_grant=grant,
    )
    assert accepted.outcome is PublicationPostOutcome.DEFINITELY_ACCEPTED
    assert len(active_transport.calls) == 1


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "https://api.printify.com/v1/shops/42/products.json", b"{}"),
        ("PUT", "https://api.printify.com/v1/shops/42/products/product_1.json", b"{}"),
        ("DELETE", "https://api.printify.com/v1/shops/42/products/product_1.json", None),
        ("GET", "https://api.printify.com/v1/shops/42/products.json", None),
        ("GET", "https://api.printify.com/v1/shops/42/orders.json", None),
        (
            "POST",
            "https://api.printify.com/v1/shops/42/products/product_1/publishing_succeeded.json",
            b"{}",
        ),
        (
            "POST",
            "https://api.printify.com/v1/shops/42/products/product_1/publishing_failed.json",
            b"{}",
        ),
        (
            "POST",
            "https://api.printify.com/v1/shops/42/products/product_1/unpublish.json",
            b"{}",
        ),
        ("get", "https://api.printify.com/v1/shops.json", None),
        ("GET", "http://api.printify.com/v1/shops.json", None),
        ("GET", "https://api.printify.com:443/v1/shops.json", None),
        ("GET", "https://api.printify.com.evil.test/v1/shops.json", None),
        ("GET", "https://api.printify.com/v1/shops.json?limit=1", None),
        ("GET", "https://api.printify.com/v1/shops.json#fragment", None),
        (
            "POST",
            "https://api.printify.com/v1/shops/42/products/product_1/publish.json",
            b'{"title":true}',
        ),
    ],
)
def test_every_forbidden_method_route_origin_query_and_body_is_rejected_and_led(
    method: str,
    url: str,
    body: bytes | None,
) -> None:
    transport = ScriptedTransport([])
    audit = MemoryAudit()
    guarded = SanitizedPublicationAuditTransport(transport=transport, audit_sink=audit)

    with pytest.raises(PublicationProviderInputError, match="outside"):
        guarded.request(
            method=method,
            url=url,
            headers={"Authorization": f"Bearer {TOKEN}"},
            body=body,
            timeout_seconds=15,
        )

    assert transport.calls == []
    assert len(audit.records) == 1
    assert audit.records[0].decision is PublicationProviderAuditDecision.REJECTED
    assert audit.records[0].method_category == "FORBIDDEN"
    assert audit.records[0].route_template == OUTSIDE_ROUTE_TEMPLATE
    expected_category = (
        PublicationProviderAuditCategory.FORBIDDEN_METHOD
        if method not in {"GET", "POST"}
        else PublicationProviderAuditCategory.FORBIDDEN_ROUTE
    )
    assert audit.records[0].category is expected_category
    assert TOKEN not in audit.records[0].model_dump_json()


def test_exact_url_validator_has_only_the_frozen_three_shapes() -> None:
    assert (
        assert_publication_api_url(
            method="GET",
            url="https://api.printify.com/v1/shops.json",
            body=None,
        )
        == SHOP_ROUTE_TEMPLATE
    )
    assert (
        assert_publication_api_url(
            method="GET",
            url="https://api.printify.com/v1/shops/42/products/product_1.json",
            body=None,
        )
        == PRODUCT_ROUTE_TEMPLATE
    )
    assert (
        assert_publication_api_url(
            method="POST",
            url="https://api.printify.com/v1/shops/42/products/product_1/publish.json",
            body=PUBLICATION_BODY_BYTES,
        )
        == PUBLISH_ROUTE_TEMPLATE
    )


def test_audited_transport_rejects_a_different_dynamic_product_path_for_valid_claim() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.VERIFICATION,
        suffix="poll",
    )
    transport = ScriptedTransport([])
    audit = MemoryAudit()
    guarded = SanitizedPublicationAuditTransport(transport=transport, audit_sink=audit)

    with pytest.raises(PublicationProviderInputError, match="durable call claim"):
        guarded.request(
            method="GET",
            url="https://api.printify.com/v1/shops/99/products/other_product.json",
            headers={"Authorization": f"Bearer {TOKEN}"},
            body=None,
            timeout_seconds=15,
            call_claim=claim,
        )

    assert transport.calls == []
    assert audit.records[-1].category is PublicationProviderAuditCategory.CLAIM_MISMATCH


@pytest.mark.parametrize(
    "shops",
    [
        [{"id": 42, "title": "Store", "sales_channel": "Etsy"}],
        [{"id": 42, "title": "Store", "sales_channel": "disconnected"}],
        [{"id": 41, "title": "Other", "sales_channel": "etsy"}],
    ],
)
def test_shop_preflight_requires_the_exact_id_and_exact_etsy_channel(shops: Any) -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.SHOP_GET,
        purpose=PublicationCallPurpose.SHOP_PREFLIGHT,
        suffix="shop",
    )
    boundary, transport, _audit = _boundary([_json_response(200, shops)])

    with pytest.raises(PublicationProviderPreflightError, match="exactly connected"):
        boundary.preflight_shop(call_claim=claim, fresh_grant=_fresh(claim))

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "shops",
    [
        [
            {"id": 42, "title": "A", "sales_channel": "etsy"},
            {"id": 42, "title": "B", "sales_channel": "etsy"},
        ],
        [{"id": True, "title": "A", "sales_channel": "etsy"}],
        [{"id": 42, "title": "A", "sales_channel": 7}],
        {"id": 42, "title": "A", "sales_channel": "etsy"},
    ],
)
def test_shop_response_is_strict_and_duplicate_identity_fails_closed(shops: Any) -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.SHOP_GET,
        purpose=PublicationCallPurpose.SHOP_PREFLIGHT,
        suffix="shop",
    )
    boundary, _transport, _audit = _boundary([_json_response(200, shops)])

    with pytest.raises(PublicationProviderResponseError, match="invalid"):
        boundary.preflight_shop(call_claim=claim, fresh_grant=_fresh(claim))


def _preflight_product(payload: dict[str, Any]) -> PrintifyProductObservation:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.PRODUCT_PREFLIGHT,
        suffix="product",
    )
    boundary, _transport, _audit = _boundary([_json_response(200, payload)])
    return boundary.preflight_exact_product(call_claim=claim, fresh_grant=_fresh(claim))


def test_product_preflight_reconstructs_complete_canonical_authority() -> None:
    observation = _preflight_product(_product(external=[]))

    assert observation.preflight_satisfied
    assert observation.canonical_content_match
    assert observation.exact_variant_economics
    assert observation.exact_placement_image
    assert observation.exact_mockups
    assert not observation.is_locked
    assert observation.visible
    assert observation.external_evidence is ExternalEvidenceState.ABSENT


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(title="Changed title"),
        lambda value: value.update(blueprint_id=999),
        lambda value: value.update(print_provider_id=999),
        lambda value: value["variants"][0].update(price=2500),
        lambda value: value["variants"][0].update(cost=1201),
        lambda value: value["print_areas"][0]["placeholders"][0]["images"][0].update(
            id="other_image"
        ),
        lambda value: value["images"][0].update(
            src="https://images.printify.com/mockup/changed.png"
        ),
        lambda value: value.update(is_locked=True),
        lambda value: value.update(external=[{"id": "123456789", "handle": "/listing"}]),
    ],
)
def test_any_canonical_economics_image_state_or_prior_publication_drift_blocks_preflight(
    mutation: Any,
) -> None:
    payload = _product(external=[])
    mutation(payload)

    with pytest.raises(PublicationProviderPreflightError, match="failed"):
        _preflight_product(payload)


def test_invisible_unpublished_exact_draft_passes_preflight_but_is_not_positive_proof() -> None:
    payload = _product(external=[])
    payload["visible"] = False

    observation = _preflight_product(payload)

    assert observation.preflight_satisfied
    assert not observation.visible
    assert observation.read_outcome is PublicationReadOutcome.NOT_YET_PROVEN
    assert observation.safe_listing_url is None


@pytest.mark.parametrize("external", [None, [], {}])
def test_absent_documented_or_legacy_empty_external_is_only_not_yet_proven(external: Any) -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.VERIFICATION,
        suffix="verify",
    )
    payload = _product(external=external)
    boundary, _transport, _audit = _boundary([_json_response(200, payload)])

    observation = boundary.poll_exact_product(call_claim=claim, fresh_grant=_fresh(claim))

    assert observation.read_outcome is PublicationReadOutcome.NOT_YET_PROVEN
    assert observation.external_evidence is ExternalEvidenceState.ABSENT
    assert observation.numeric_listing_id is None
    assert observation.safe_listing_url is None


def test_single_documented_numeric_external_reference_derives_only_the_safe_etsy_link() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.VERIFICATION,
        suffix="verify",
    )
    malicious_handle = "javascript:alert(document.cookie)"
    boundary, _transport, _audit = _boundary(
        [
            _json_response(
                200,
                _product(
                    external=[
                        {
                            "id": "123456789",
                            "handle": malicious_handle,
                            "shipping_template_id": "provider-value",
                        }
                    ]
                ),
            )
        ]
    )

    observation = boundary.poll_exact_product(call_claim=claim, fresh_grant=_fresh(claim))

    assert observation.read_outcome is PublicationReadOutcome.POSITIVE_PROOF
    assert observation.numeric_listing_id == 123456789
    assert observation.safe_listing_url == "https://www.etsy.com/listing/123456789"
    assert malicious_handle not in repr(observation)
    assert malicious_handle not in observation.model_dump_json()


def test_external_proof_observed_at_fixed_deadline_is_never_positive() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.VERIFICATION,
        suffix="verify",
    )
    times = iter((DEADLINE - timedelta(microseconds=1), DEADLINE))
    boundary, transport, _audit = _boundary(
        [_json_response(200, _product(external=[{"id": "123456789"}]))],
        clock=lambda: next(times),
    )

    observation = boundary.poll_exact_product(call_claim=claim, fresh_grant=_fresh(claim))

    assert len(transport.calls) == 1
    assert observation.observed_at == DEADLINE
    assert observation.read_outcome is PublicationReadOutcome.NOT_YET_PROVEN
    assert observation.numeric_listing_id is None
    assert observation.safe_listing_url is None


def test_sanitized_product_fingerprint_never_binds_untrusted_external_handle() -> None:
    observations = []
    for suffix, handle in (("a", "https://evil.test/a"), ("b", "javascript:alert(1)")):
        authority = _authority()
        claim = _claim(
            authority,
            kind=PublicationCallKind.PRODUCT_GET,
            purpose=PublicationCallPurpose.VERIFICATION,
            suffix=suffix,
        )
        boundary, _transport, _audit = _boundary(
            [_json_response(200, _product(external=[{"id": "123456789", "handle": handle}]))]
        )
        observations.append(
            boundary.poll_exact_product(call_claim=claim, fresh_grant=_fresh(claim))
        )

    assert (
        observations[0].sanitized_response_fingerprint
        == observations[1].sanitized_response_fingerprint
    )


@pytest.mark.parametrize(
    "external",
    [
        {"id": "123456789", "handle": "/listing"},
        [{"id": "123"}, {"id": "456"}],
        [{"id": "abc"}],
        [{"id": 123}],
        [{}],
        "123",
    ],
)
def test_nonempty_dict_multiple_or_malformed_external_never_proves_success(external: Any) -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.RECONCILIATION,
        suffix="reconcile",
    )
    boundary, _transport, _audit = _boundary([_json_response(200, _product(external=external))])

    observation = boundary.poll_exact_product(call_claim=claim, fresh_grant=_fresh(claim))

    assert observation.read_outcome is PublicationReadOutcome.CONFLICTING_OR_INCOMPLETE
    assert observation.external_evidence is ExternalEvidenceState.CONFLICTING_OR_INCOMPLETE
    assert observation.numeric_listing_id is None
    assert observation.safe_listing_url is None


@pytest.mark.parametrize(
    ("status", "body", "outcome", "category"),
    [
        (
            200,
            b"",
            PublicationPostOutcome.DEFINITELY_ACCEPTED,
            PublishResponseCategory.VALIDATED_2XX,
        ),
        (
            201,
            b"{}",
            PublicationPostOutcome.DEFINITELY_ACCEPTED,
            PublishResponseCategory.VALIDATED_2XX,
        ),
        (
            204,
            b"",
            PublicationPostOutcome.DEFINITELY_ACCEPTED,
            PublishResponseCategory.VALIDATED_2XX,
        ),
        (200, b"not-json", PublicationPostOutcome.AMBIGUOUS, PublishResponseCategory.MALFORMED_2XX),
        (302, b"redirect", PublicationPostOutcome.AMBIGUOUS, PublishResponseCategory.NON_2XX),
        (
            400,
            b'{"error":"bad"}',
            PublicationPostOutcome.AMBIGUOUS,
            PublishResponseCategory.NON_2XX,
        ),
        (408, b"", PublicationPostOutcome.AMBIGUOUS, PublishResponseCategory.NON_2XX),
        (409, b"", PublicationPostOutcome.AMBIGUOUS, PublishResponseCategory.NON_2XX),
        (429, b"", PublicationPostOutcome.AMBIGUOUS, PublishResponseCategory.NON_2XX),
        (500, b"", PublicationPostOutcome.AMBIGUOUS, PublishResponseCategory.NON_2XX),
    ],
)
def test_publish_response_classifier_accepts_only_bounded_parsed_2xx_and_never_retries(
    status: int,
    body: bytes,
    outcome: PublicationPostOutcome,
    category: PublishResponseCategory,
) -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PUBLISH_POST,
        purpose=PublicationCallPurpose.PUBLISH,
        suffix="publish",
        authorized_at=NOW + timedelta(seconds=1),
    )
    mutation = _mutation(authority, claim)
    boundary, transport, _audit = _boundary([PublicationHttpResponse(status=status, body=body)])

    observation = boundary.publish_exact_product(
        call_claim=claim,
        mutation_claim=mutation,
        preflight_proof=_preflight_proof(authority),
        fresh_grant=_fresh_mutation(claim, mutation),
    )

    assert observation.outcome is outcome
    assert observation.response_category is category
    assert len(transport.calls) == 1
    assert not transport.responses


@pytest.mark.parametrize(
    "transport_error",
    [
        PublicationProviderUnavailableError("closed transport failure"),
        RuntimeError("unexpected transport failure"),
    ],
)
def test_publish_transport_exception_is_ambiguous_and_never_retried(
    transport_error: Exception,
) -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PUBLISH_POST,
        purpose=PublicationCallPurpose.PUBLISH,
        suffix="publish",
        authorized_at=NOW + timedelta(seconds=1),
    )
    mutation = _mutation(authority, claim)
    boundary, transport, _audit = _boundary([transport_error])

    observation = boundary.publish_exact_product(
        call_claim=claim,
        mutation_claim=mutation,
        preflight_proof=_preflight_proof(authority),
        fresh_grant=_fresh_mutation(claim, mutation),
    )

    assert observation.outcome is PublicationPostOutcome.AMBIGUOUS
    assert observation.response_category is PublishResponseCategory.TRANSPORT_FAILURE
    assert observation.sanitized_response_fingerprint is None
    assert len(transport.calls) == 1


def test_publish_audit_failure_is_ambiguous_and_prevents_the_wire_call() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PUBLISH_POST,
        purpose=PublicationCallPurpose.PUBLISH,
        suffix="publish",
        authorized_at=NOW + timedelta(seconds=1),
    )
    proof = _preflight_proof(authority)
    mutation = _mutation(authority, claim, proof=proof)
    transport = ScriptedTransport([_json_response(200, {})])
    boundary = PrintifyPublicationBoundary(
        authority=authority,
        credential=OwnerBoundPrintifyCredential(owner_id=OWNER, bearer_token=TOKEN),
        transport=transport,
        audit_sink=FailingAudit(),
        clock=lambda: NOW + timedelta(seconds=2),
    )

    observation = boundary.publish_exact_product(
        call_claim=claim,
        mutation_claim=mutation,
        preflight_proof=proof,
        fresh_grant=_fresh_mutation(claim, mutation),
    )

    assert observation.outcome is PublicationPostOutcome.AMBIGUOUS
    assert observation.response_category is PublishResponseCategory.TRANSPORT_FAILURE
    assert transport.calls == []


def test_deep_claim_validation_closes_model_copy_and_wrong_owner_shop_product_authority() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.VERIFICATION,
        suffix="poll",
    )
    forged = claim.model_copy(update={"printify_shop_id": 99})
    boundary, transport, audit = _boundary([])

    with pytest.raises(PublicationProviderInputError, match="claim differs"):
        boundary.poll_exact_product(call_claim=forged, fresh_grant=_fresh(claim))

    assert transport.calls == []
    assert audit.records[-1].decision == "rejected"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product_payload_fingerprint", "f" * 64),
        ("expected_mockup_fingerprints", ("e" * 64,)),
        ("pricing_fresh_until", DEADLINE + timedelta(seconds=1)),
        ("phase6_record_version", 13),
    ],
)
def test_provider_constructor_revalidates_the_exact_shared_authority_fingerprint(
    field: str,
    value: Any,
) -> None:
    forged = _authority().model_copy(update={field: value})

    with pytest.raises(PublicationProviderInputError, match="authority is invalid"):
        _boundary([], authority=forged)


def test_fixed_deadline_rejects_fresh_claim_before_wire_and_does_not_consume_grant() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.VERIFICATION,
        suffix="poll",
    )
    grant = _fresh(claim)
    expired, expired_transport, _audit = _boundary([], clock=lambda: DEADLINE)

    with pytest.raises(PublicationProviderInputError, match="claim differs"):
        expired.poll_exact_product(call_claim=claim, fresh_grant=grant)

    assert expired_transport.calls == []
    active, active_transport, _audit = _boundary(
        [_json_response(200, _product(external=[]))],
        clock=lambda: NOW,
    )
    active.poll_exact_product(call_claim=claim, fresh_grant=grant)
    assert len(active_transport.calls) == 1


def test_future_dated_durable_claim_cannot_wire_before_its_authorization_time() -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.VERIFICATION,
        suffix="future",
        authorized_at=NOW + timedelta(seconds=1),
    )
    grant = _fresh(claim)
    boundary, transport, audit = _boundary([], authority=authority, clock=lambda: NOW)

    with pytest.raises(PublicationProviderInputError, match="claim differs"):
        boundary.poll_exact_product(call_claim=claim, fresh_grant=grant)

    assert transport.calls == []
    assert audit.records[-1].category is PublicationProviderAuditCategory.CLAIM_MISMATCH


def test_pricing_expiry_after_preflight_blocks_publish_without_consuming_fresh_grant() -> None:
    expired_authority = _authority(
        pricing_fresh_until=NOW + timedelta(seconds=1, microseconds=500_000)
    )
    claim = _claim(
        expired_authority,
        kind=PublicationCallKind.PUBLISH_POST,
        purpose=PublicationCallPurpose.PUBLISH,
        suffix="publish",
        authorized_at=NOW + timedelta(seconds=1),
    )
    proof = _preflight_proof(expired_authority)
    mutation = _mutation(expired_authority, claim, proof=proof)
    grant = _fresh_mutation(claim, mutation)
    expired, expired_transport, expired_audit = _boundary(
        [],
        authority=expired_authority,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    with pytest.raises(PublicationProviderInputError, match="claim differs"):
        expired.publish_exact_product(
            call_claim=claim,
            mutation_claim=mutation,
            preflight_proof=proof,
            fresh_grant=grant,
        )

    assert expired_transport.calls == []
    assert expired_audit.records[-1].category is PublicationProviderAuditCategory.CLAIM_MISMATCH

    active, active_transport, _audit = _boundary(
        [_json_response(200, {})],
        authority=expired_authority,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    observation = active.publish_exact_product(
        call_claim=claim,
        mutation_claim=mutation,
        preflight_proof=proof,
        fresh_grant=grant,
    )
    assert observation.outcome is PublicationPostOutcome.DEFINITELY_ACCEPTED
    assert len(active_transport.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_get_authentication_failures_are_closed_and_sanitized(status: int) -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.SHOP_GET,
        purpose=PublicationCallPurpose.SHOP_PREFLIGHT,
        suffix="shop",
    )
    boundary, _transport, audit = _boundary([PublicationHttpResponse(status=status, body=b"")])

    with pytest.raises(PublicationProviderAuthenticationError):
        boundary.preflight_shop(call_claim=claim, fresh_grant=_fresh(claim))

    assert TOKEN not in "".join(record.model_dump_json() for record in audit.records)


def test_product_get_404_retires_preflight_but_is_only_nonproof_after_consumption() -> None:
    authority = _authority()
    preflight_claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.PRODUCT_PREFLIGHT,
        suffix="preflight",
    )
    preflight, _preflight_transport, _preflight_audit = _boundary(
        [PublicationHttpResponse(status=404, body=b"")]
    )

    with pytest.raises(PublicationProviderPreflightError, match="not found"):
        preflight.preflight_exact_product(
            call_claim=preflight_claim,
            fresh_grant=_fresh(preflight_claim),
        )

    reconciliation_claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.RECONCILIATION,
        suffix="poll",
    )
    reconciliation, _transport, _audit = _boundary([PublicationHttpResponse(status=404, body=b"")])

    observation = reconciliation.poll_exact_product(
        call_claim=reconciliation_claim,
        fresh_grant=_fresh(reconciliation_claim),
    )

    assert not observation.product_present
    assert observation.read_outcome is PublicationReadOutcome.CONFLICTING_OR_INCOMPLETE
    assert observation.numeric_listing_id is None
    assert observation.safe_listing_url is None


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b'{"id":"product_1","id":"other"}',
        b'{"value":NaN}',
        b"[]",
    ],
)
def test_product_json_is_utf8_duplicate_free_finite_and_mapping_only(body: bytes) -> None:
    authority = _authority()
    claim = _claim(
        authority,
        kind=PublicationCallKind.PRODUCT_GET,
        purpose=PublicationCallPurpose.VERIFICATION,
        suffix="poll",
    )
    boundary, _transport, _audit = _boundary([PublicationHttpResponse(status=200, body=body)])

    with pytest.raises(PublicationProviderResponseError):
        boundary.poll_exact_product(call_claim=claim, fresh_grant=_fresh(claim))


class RedirectingOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[Request] = []

    def open(self, request: Request, timeout: float) -> Any:
        del timeout
        self.calls.append(request)
        raise HTTPError(
            request.full_url,
            302,
            "redirect",
            {"Location": "https://evil.test/steal"},
            BytesIO(self.body),
        )


def test_redirect_safe_transport_returns_one_3xx_without_following_or_forwarding() -> None:
    opener = RedirectingOpener(b"redirect refused")
    transport = RedirectSafePublicationTransport(opener=opener)  # type: ignore[arg-type]

    response = transport.request(
        method="GET",
        url="https://api.printify.com/v1/shops.json",
        headers={"Authorization": f"Bearer {TOKEN}"},
        body=None,
        timeout_seconds=15,
    )

    assert response.status == 302
    assert response.body == b"redirect refused"
    assert len(opener.calls) == 1
    assert opener.calls[0].full_url == "https://api.printify.com/v1/shops.json"


class FailingOpener:
    def open(self, request: Request, timeout: float) -> Any:
        del timeout
        raise URLError(f"private dependency detail for {request.full_url}")


def test_transport_failure_detaches_dependency_exception_identity() -> None:
    transport = RedirectSafePublicationTransport(opener=FailingOpener())  # type: ignore[arg-type]

    with pytest.raises(PublicationProviderUnavailableError) as captured:
        transport.request(
            method="GET",
            url="https://api.printify.com/v1/shops.json",
            headers={"Authorization": f"Bearer {TOKEN}"},
            body=None,
            timeout_seconds=15,
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "api.printify.com" not in str(captured.value)
    assert TOKEN not in str(captured.value)


class OversizedResponse:
    status = 200

    def __enter__(self) -> OversizedResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        del args

    def read(self, size: int) -> bytes:
        return b"x" * size


class OversizedOpener:
    def open(self, request: Request, timeout: float) -> OversizedResponse:
        del request, timeout
        return OversizedResponse()


def test_redirect_safe_transport_bounds_response_before_json_or_logging() -> None:
    transport = RedirectSafePublicationTransport(opener=OversizedOpener())  # type: ignore[arg-type]

    with pytest.raises(PublicationProviderUnavailableError, match="exceeded"):
        transport.request(
            method="GET",
            url="https://api.printify.com/v1/shops.json",
            headers={"Authorization": f"Bearer {TOKEN}"},
            body=None,
            timeout_seconds=15,
        )


def test_boundary_is_separate_capability_narrow_and_has_no_forbidden_operation() -> None:
    public = {name for name in vars(PrintifyPublicationBoundary) if not name.startswith("_")}
    assert public == {
        "preflight_shop",
        "preflight_exact_product",
        "poll_exact_product",
        "publish_exact_product",
    }
    forbidden = {
        "create",
        "create_product",
        "update",
        "update_product",
        "delete",
        "delete_product",
        "upload",
        "archive",
        "publishing_succeeded",
        "publishing_failed",
        "unpublish",
        "orders",
        "fulfillment",
        "webhook",
    }
    assert public.isdisjoint(forbidden)
    assert all(
        "production.draft_sync" not in item.__module__
        for item in inspect.getmro(PrintifyPublicationBoundary)
    )
    assert (
        "payload"
        not in inspect.signature(PrintifyPublicationBoundary.publish_exact_product).parameters
    )


def test_owner_bound_credential_is_redacted_and_mismatch_fails_before_wire() -> None:
    credential = OwnerBoundPrintifyCredential(owner_id=OWNER, bearer_token=TOKEN)
    assert TOKEN not in repr(credential)
    assert TOKEN not in credential.model_dump_json()
    transport = ScriptedTransport([])
    audit = MemoryAudit()

    with pytest.raises(PublicationProviderInputError, match="owner authority"):
        PrintifyPublicationBoundary(
            authority=_authority(),
            credential=OwnerBoundPrintifyCredential(owner_id="9" * 64, bearer_token=TOKEN),
            transport=transport,
            audit_sink=audit,
        )

    assert transport.calls == []


def test_audit_record_schema_cannot_store_dynamic_route_identity_or_response_material() -> None:
    with pytest.raises(ValueError):
        PublicationProviderAuditRecord.model_validate(
            {
                "decision": "allowed",
                "method_category": "GET",
                "route_template": "/v1/shops/42/products/product_1.json",
                "category": "product_get_allowed",
                "fingerprint": "0" * 64,
            }
        )
    with pytest.raises(ValueError):
        PublicationProviderAuditRecord.model_validate(
            {
                "decision": "allowed",
                "method_category": "DELETE",
                "route_template": PRODUCT_ROUTE_TEMPLATE,
                "category": "product_get_allowed",
                "fingerprint": "0" * 64,
            }
        )
    assert set(PublicationProviderAuditRecord.model_fields) == {
        "contract_version",
        "decision",
        "method_category",
        "route_template",
        "category",
        "fingerprint",
    }


def test_response_and_body_bounds_are_fixed_not_caller_selected() -> None:
    assert MAX_PUBLICATION_RESPONSE_BYTES == 4 * 1024 * 1024
    assert len(PUBLICATION_BODY_BYTES) < 256
    signature = inspect.signature(RedirectSafePublicationTransport.request)
    assert "maximum_response_bytes" not in signature.parameters
    assert "retry" not in signature.parameters
