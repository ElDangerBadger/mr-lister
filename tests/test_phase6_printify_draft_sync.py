from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

import pytest

from mr_lister.contracts import Placement, PlacementGroup, ProductProfile
from mr_lister.control.models import PHASE6_MAX_SOURCE_ARTWORK_BYTES
from mr_lister.production.draft_sync import (
    CreateAmbiguityReason,
    CreateReconciliationOutcome,
    DraftSyncOperation,
    PrintifyCreateOutcomeUnknown,
    PrintifyDraftOnlyClient,
    PrintifyDraftSynchronizer,
    PrintifyUpdateOutcomeUnknown,
    PrintifyUploadOutcomeUnknown,
    RedirectSafePrintifyTransport,
    UpdateReconciliationOutcome,
    _RejectRedirectHandler,
    assert_draft_only_route,
    assert_printify_api_url,
    build_canonical_draft,
    job_correlation_token,
)
from mr_lister.production.printify import (
    PrintifyCatalogMismatchError,
    PrintifyHttpResponse,
    PrintifyInputError,
    PrintifyResolvedProfile,
    PrintifyResolvedVariant,
    PrintifyUnavailableError,
)


@dataclass(frozen=True)
class ExpectedRequest:
    method: str
    path: str
    status: int = 200
    payload: object = None
    error: Exception | None = None


class ScriptedTransport:
    def __init__(self, expected: list[ExpectedRequest]) -> None:
        self.expected = expected
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> PrintifyHttpResponse:
        del timeout_seconds
        call = {
            "method": method,
            "path": urlsplit(url).path,
            "query": urlsplit(url).query,
            "headers": headers,
            "body": body,
        }
        self.calls.append(call)
        if not self.expected:
            raise AssertionError(f"Unexpected Printify request: {method} {url}")
        expected = self.expected.pop(0)
        assert method == expected.method
        assert urlsplit(url).path == expected.path
        if expected.error is not None:
            raise expected.error
        return PrintifyHttpResponse(
            status=expected.status,
            body=json.dumps(expected.payload).encode(),
        )


def profile() -> ProductProfile:
    return ProductProfile(
        profile_id="phase6_fixture",
        profile_version=1,
        blueprint_id=145,
        print_provider_id=39,
        colors=("Black",),
        sizes=("S",),
        retail_price_cents=2999,
        placement_groups=(
            PlacementGroup(
                group_id="small",
                sizes=("S",),
                canvas_width=3021,
                canvas_height=3927,
                placement=Placement(x=0.5, y=0.25, scale=0.65),
            ),
        ),
    )


def resolved() -> PrintifyResolvedProfile:
    return PrintifyResolvedProfile(
        profile_id="phase6_fixture",
        profile_version=1,
        shop_id=42,
        blueprint_id=145,
        print_provider_id=39,
        variants=(
            PrintifyResolvedVariant(
                variant_id=1000,
                color="Black",
                size="S",
                placement_group_id="small",
                canvas_width=3021,
                canvas_height=3927,
                retail_price_cents=2999,
            ),
        ),
    )


def canonical_draft(listing, *, job_id: str = "job_phase6_sync"):
    return build_canonical_draft(
        job_id=job_id,
        listing=listing,
        profile=profile(),
        resolved=resolved(),
        image_id="image_1",
    )


def provider_product(draft, *, product_id: str = "product_1", **updates: object):
    product = {
        "id": product_id,
        "shop_id": 42,
        **draft.provider_payload(),
        "is_locked": False,
        # Printify defaults ``visible`` true; it is not evidence of publication.
        "visible": True,
        "external": {},
        "images": [
            {
                "src": f"https://images.printify.com/{product_id}/front.jpg",
                "position": "front",
                "variant_ids": [variant.id for variant in draft.variants],
            }
        ],
    }
    for index, variant in enumerate(product["variants"]):
        variant["cost"] = 1100 + index
    product.update(updates)
    return product


def provider_normalized_product(draft, *, product_id: str = "product_1"):
    """Model Printify's inert expansion to the complete provider catalog."""

    product = provider_product(draft, product_id=product_id)
    product["variants"].append(
        {
            "id": 9000,
            "price": 2999,
            "cost": 1300,
            "is_enabled": False,
            "sku": "",
        }
    )
    product["print_areas"][0]["variant_ids"].append(9000)
    product["print_areas"][0]["placeholders"].append({"position": "back", "images": []})
    return product


def synchronizer(expected: list[ExpectedRequest]):
    transport = ScriptedTransport(expected)
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )
    return PrintifyDraftSynchronizer(client=client, shop_id=42), transport, client


def test_canonical_payload_reuses_phase5_contracts_and_embeds_job_token(listing) -> None:
    draft = canonical_draft(listing)

    assert draft.correlation_token == job_correlation_token("job_phase6_sync")
    assert draft.variants[0].sku == f"{draft.correlation_token}-1000"
    assert draft.correlation_token not in draft.provider_payload()
    assert draft.provider_payload()["print_areas"][0]["variant_ids"] == [1000]
    assert len(draft.payload_fingerprint) == 64


def test_partial_update_payload_contains_only_seller_editable_listing_fields(listing) -> None:
    draft = canonical_draft(listing)

    assert draft.provider_update_payload() == {
        "title": draft.title,
        "description": draft.description,
        "tags": list(draft.tags),
    }


def test_width_first_placement_preserves_scale_and_source_aspect_ratio(listing) -> None:
    draft = build_canonical_draft(
        job_id="job_phase6_rectangular",
        listing=listing,
        profile=profile(),
        resolved=resolved(),
        image_id="image_rectangular",
        artwork_width=2000,
        artwork_height=800,
    )

    placement = draft.print_areas[0].placeholders[0].images[0]
    assert placement.x == 0.5
    assert placement.y == 0.100008
    assert placement.scale == 0.65


def test_square_geometry_and_legacy_absence_preserve_the_exact_payload_fingerprint(listing) -> None:
    legacy = canonical_draft(listing, job_id="job_phase6_compatible")
    square = build_canonical_draft(
        job_id="job_phase6_compatible",
        listing=listing,
        profile=profile(),
        resolved=resolved(),
        image_id="image_1",
        artwork_width=2400,
        artwork_height=2400,
    )

    assert square.provider_payload() == legacy.provider_payload()
    assert square.payload_fingerprint == legacy.payload_fingerprint


def test_source_geometry_must_be_present_as_a_pair(listing) -> None:
    with pytest.raises(PrintifyInputError, match="present together"):
        build_canonical_draft(
            job_id="job_phase6_unpaired",
            listing=listing,
            profile=profile(),
            resolved=resolved(),
            image_id="image_unpaired",
            artwork_width=2000,
        )


def test_tall_placement_scales_down_and_preserves_aspect_ratio(listing) -> None:
    draft = build_canonical_draft(
        job_id="job_phase6_tall",
        listing=listing,
        profile=profile(),
        resolved=resolved(),
        image_id="image_tall",
        artwork_width=1000,
        artwork_height=2100,
    )

    placement = draft.print_areas[0].placeholders[0].images[0]
    assert placement.x == 0.5
    assert placement.scale == 0.619
    assert placement.y == 0.5
    rendered_width = placement.scale * 3021
    rendered_height = rendered_width * 2100 / 1000
    assert rendered_width / rendered_height == pytest.approx(1000 / 2100)
    assert placement.y == round(rendered_height / 3927 / 2, 6)
    assert rendered_height <= 3927


def test_first_authorized_sync_posts_exactly_once_and_returns_evidence(listing) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                payload=provider_product(draft),
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(draft),
            ),
        ]
    )

    evidence = sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)

    assert evidence.operation is DraftSyncOperation.CREATED
    assert evidence.product_id == "product_1"
    assert evidence.payload_fingerprint == draft.payload_fingerprint
    assert evidence.request_fingerprint == draft.payload_fingerprint
    assert len(evidence.response_fingerprint) == 64
    assert evidence.image_id == "image_1"
    assert evidence.provider_published is False
    assert evidence.variants == evidence.enabled_variant_economics
    assert evidence.enabled_variant_economics[0].variant_id == 1000
    assert evidence.enabled_variant_economics[0].retail_price_cents == 2999
    assert evidence.enabled_variant_economics[0].production_cost_cents == 1100
    assert tuple(mockup.url for mockup in evidence.mockups) == (
        "https://images.printify.com/product_1/front.jpg",
    )
    assert evidence.mockups[0].position == "front"
    assert evidence.mockups[0].variant_ids == (1000,)
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]
    assert json.loads(transport.calls[0]["body"]) == draft.provider_payload()
    assert not transport.expected


def test_initial_create_accepts_disabled_provider_catalog_expansion(listing) -> None:
    draft = canonical_draft(listing)
    product = provider_normalized_product(draft)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest("POST", "/v1/shops/42/products.json", payload=product),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=product,
            ),
        ]
    )

    evidence = sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)

    assert evidence.operation is DraftSyncOperation.CREATED
    assert tuple(item.variant_id for item in evidence.variants) == (1000,)
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_later_revision_gets_then_puts_same_immutable_product_id(listing) -> None:
    draft = canonical_draft(listing)
    changed = draft.model_copy(update={"title": "Seller revised geometric badger shirt"})
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(draft),
            ),
            ExpectedRequest(
                "PUT",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(changed),
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(changed),
            ),
        ]
    )

    evidence = sync.synchronize(
        job_id="job_phase6_sync",
        draft=changed,
        product_id="product_1",
        prior_draft=draft,
    )

    assert evidence.operation is DraftSyncOperation.REPLACED
    assert evidence.product_id == "product_1"
    assert [call["method"] for call in transport.calls] == ["GET", "PUT", "GET"]
    assert all(call["path"].endswith("/product_1.json") for call in transport.calls)
    assert json.loads(transport.calls[1]["body"]) == changed.provider_update_payload()
    assert not transport.expected


def test_later_revision_accepts_disabled_provider_catalog_expansion(listing) -> None:
    prior = canonical_draft(listing)
    changed = prior.model_copy(update={"title": "Seller revised geometric badger shirt"})
    prior_product = provider_normalized_product(prior)
    changed_product = provider_normalized_product(changed)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=prior_product,
            ),
            ExpectedRequest(
                "PUT",
                "/v1/shops/42/products/product_1.json",
                payload=changed_product,
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=changed_product,
            ),
        ]
    )

    evidence = sync.synchronize(
        job_id="job_phase6_sync",
        draft=changed,
        product_id="product_1",
        prior_draft=prior,
    )

    assert evidence.operation is DraftSyncOperation.REPLACED
    assert tuple(item.variant_id for item in evidence.variants) == (1000,)
    assert [call["method"] for call in transport.calls] == ["GET", "PUT", "GET"]


@pytest.mark.parametrize(
    "provider_update",
    [
        {"shop_id": 999},
        {"blueprint_id": 999},
        {"print_provider_id": 999},
        {"is_locked": True},
        {"external": {"id": "published_listing_1", "handle": "example"}},
    ],
)
def test_existing_product_identity_and_editability_fail_closed_before_put(
    listing, provider_update
) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(draft, **provider_update),
            )
        ]
    )

    with pytest.raises(PrintifyCatalogMismatchError):
        sync.synchronize(
            job_id="job_phase6_sync",
            draft=draft,
            product_id="product_1",
            prior_draft=draft,
        )

    assert [call["method"] for call in transport.calls] == ["GET"]


@pytest.mark.parametrize(
    "provider_update",
    [
        {"is_locked": "false"},
        {"external": []},
        {"external": ""},
    ],
)
def test_malformed_provider_state_fails_closed_before_put(listing, provider_update) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(draft, **provider_update),
            )
        ]
    )

    with pytest.raises(PrintifyCatalogMismatchError, match="malformed"):
        sync.synchronize(
            job_id="job_phase6_sync",
            draft=draft,
            product_id="product_1",
            prior_draft=draft,
        )

    assert [call["method"] for call in transport.calls] == ["GET"]


@pytest.mark.parametrize(
    "variant_update",
    [
        {"id": True},
        {"price": "2999"},
        {"price": True},
        {"cost": "1100"},
        {"cost": True},
        {"cost": -1},
        {"is_enabled": 1},
        {"is_enabled": False},
    ],
)
def test_provider_variant_economics_are_strict_before_confirmation(listing, variant_update) -> None:
    draft = canonical_draft(listing)
    product = provider_product(draft)
    product["variants"][0].update(variant_update)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                payload=product,
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=product,
            ),
        ]
    )

    with pytest.raises(PrintifyCreateOutcomeUnknown, match="reconcile"):
        sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)

    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_provider_variant_evidence_rejects_duplicate_ids(listing) -> None:
    draft = canonical_draft(listing)
    product = provider_product(draft)
    product["variants"].append(dict(product["variants"][0]))
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                payload=product,
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=product,
            ),
        ]
    )

    with pytest.raises(PrintifyCreateOutcomeUnknown, match="reconcile"):
        sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)

    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_unknown_initial_post_outcome_requires_reconciliation_not_retry(listing) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                error=PrintifyUnavailableError("network outcome unknown"),
            )
        ]
    )

    with pytest.raises(PrintifyCreateOutcomeUnknown, match="without another POST") as raised:
        sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)

    assert not isinstance(raised.value, PrintifyUnavailableError)
    assert [call["method"] for call in transport.calls] == ["POST"]


def test_malformed_successful_create_response_is_an_unknown_outcome(listing) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [ExpectedRequest("POST", "/v1/shops/42/products.json", payload={"images": []})]
    )

    with pytest.raises(PrintifyCreateOutcomeUnknown, match="reconcile"):
        sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)

    assert [call["method"] for call in transport.calls] == ["POST"]


def test_provider_rejection_after_initial_post_still_routes_to_get_only_reconciliation(
    listing,
) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                status=400,
                payload={"message": "provider detail"},
            )
        ]
    )

    with pytest.raises(PrintifyCreateOutcomeUnknown, match="without another POST"):
        sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)

    assert [call["method"] for call in transport.calls] == ["POST"]


def test_put_cannot_change_product_identity_and_never_falls_back_to_post(listing) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(draft),
            ),
            ExpectedRequest(
                "PUT",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(draft, product_id="product_2"),
            ),
        ]
    )

    with pytest.raises(PrintifyUpdateOutcomeUnknown, match="same product"):
        sync.synchronize(
            job_id="job_phase6_sync",
            draft=draft,
            product_id="product_1",
            prior_draft=draft,
        )

    assert [call["method"] for call in transport.calls] == ["GET", "PUT"]
    assert "POST" not in {call["method"] for call in transport.calls}


def test_update_refuses_manual_drift_from_exact_prior_before_put(listing) -> None:
    prior = canonical_draft(listing)
    target = prior.model_copy(update={"title": "Seller revised geometric badger shirt"})
    drifted = prior.model_copy(update={"description": "Manual provider-side edit"})
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(drifted),
            )
        ]
    )

    with pytest.raises(PrintifyCatalogMismatchError, match="exact prior"):
        sync.synchronize(
            job_id="job_phase6_sync",
            draft=target,
            product_id="product_1",
            prior_draft=prior,
        )

    assert [call["method"] for call in transport.calls] == ["GET"]


def test_update_without_prior_authority_fails_before_provider_read(listing) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer([])

    with pytest.raises(PrintifyInputError, match="exact prior"):
        sync.synchronize(
            job_id="job_phase6_sync",
            draft=draft,
            product_id="product_1",
        )

    assert transport.calls == []


@pytest.mark.parametrize(
    ("observed", "expected_outcome"),
    [
        ("target", UpdateReconciliationOutcome.APPLIED),
        ("prior", UpdateReconciliationOutcome.PRIOR_PAYLOAD),
        ("conflict", UpdateReconciliationOutcome.CONFLICT),
    ],
)
def test_update_reconciliation_is_get_only_and_classifies_exact_payload(
    listing,
    observed,
    expected_outcome,
) -> None:
    prior = canonical_draft(listing)
    target = prior.model_copy(update={"title": "Seller revised geometric badger shirt"})
    observed_draft = {
        "target": target,
        "prior": prior,
        "conflict": prior.model_copy(update={"title": "Unexpected provider title"}),
    }[observed]
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(observed_draft),
            )
        ]
    )

    result = sync.reconcile_update(
        job_id="job_phase6_sync",
        product_id="product_1",
        target_draft=target,
        prior_draft=prior,
    )

    assert result.outcome is expected_outcome
    assert (result.evidence is not None) is (
        expected_outcome is UpdateReconciliationOutcome.APPLIED
    )
    assert [call["method"] for call in transport.calls] == ["GET"]


@pytest.mark.parametrize(
    ("observed", "expected_outcome"),
    [
        ("target", UpdateReconciliationOutcome.APPLIED),
        ("prior", UpdateReconciliationOutcome.PRIOR_PAYLOAD),
    ],
)
def test_update_reconciliation_accepts_disabled_provider_catalog_expansion(
    listing,
    observed,
    expected_outcome,
) -> None:
    prior = canonical_draft(listing)
    target = prior.model_copy(update={"title": "Seller revised geometric badger shirt"})
    observed_draft = target if observed == "target" else prior
    product = provider_normalized_product(observed_draft)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=product,
            )
        ]
    )

    result = sync.reconcile_update(
        job_id="job_phase6_sync",
        product_id="product_1",
        target_draft=target,
        prior_draft=prior,
    )

    assert result.outcome is expected_outcome
    assert [call["method"] for call in transport.calls] == ["GET"]


def test_create_reconciliation_classifies_zero_with_one_get_and_no_post(listing) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [ExpectedRequest("GET", "/v1/shops/42/products.json", payload={"data": []})]
    )

    result = sync.reconcile_initial_create(job_id="job_phase6_sync", draft=draft)

    assert result.outcome is CreateReconciliationOutcome.ZERO
    assert result.evidence is None
    assert [call["method"] for call in transport.calls] == ["GET"]
    assert transport.calls[0]["query"] == "page=1&limit=50"


def test_create_reconciliation_classifies_one_exact_canonical_match(listing) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products.json",
                payload={"data": [provider_product(draft)]},
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(draft),
            ),
        ]
    )

    result = sync.reconcile_initial_create(job_id="job_phase6_sync", draft=draft)

    assert result.outcome is CreateReconciliationOutcome.ONE
    assert result.evidence is not None
    assert result.evidence.product_id == "product_1"
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


def test_create_reconciliation_accepts_disabled_provider_catalog_expansion(listing) -> None:
    draft = canonical_draft(listing)
    product = provider_normalized_product(draft)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products.json",
                payload={"data": [product]},
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=product,
            ),
        ]
    )

    result = sync.reconcile_initial_create(job_id="job_phase6_sync", draft=draft)

    assert result.outcome is CreateReconciliationOutcome.ONE
    assert result.evidence is not None
    assert tuple(item.variant_id for item in result.evidence.variants) == (1000,)
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


@pytest.mark.parametrize(
    "drift",
    [
        "enabled_extra_variant",
        "unknown_print_area_variant",
        "duplicate_print_area_variant",
        "nonempty_extra_placeholder",
        "duplicate_requested_placeholder",
        "requested_image_placement",
    ],
)
def test_provider_catalog_expansion_rejects_non_inert_drift(listing, drift) -> None:
    draft = canonical_draft(listing)
    product = provider_normalized_product(draft)
    if drift == "enabled_extra_variant":
        product["variants"][-1]["is_enabled"] = True
    elif drift == "unknown_print_area_variant":
        product["print_areas"][0]["variant_ids"].append(9001)
    elif drift == "duplicate_print_area_variant":
        product["print_areas"][0]["variant_ids"].append(9000)
    elif drift == "nonempty_extra_placeholder":
        product["print_areas"][0]["placeholders"][-1]["images"] = [
            dict(product["print_areas"][0]["placeholders"][0]["images"][0])
        ]
    elif drift == "duplicate_requested_placeholder":
        product["print_areas"][0]["placeholders"].append({"position": "front", "images": []})
    else:
        product["print_areas"][0]["placeholders"][0]["images"][0]["x"] = 0.4
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products.json",
                payload={"data": [product]},
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=product,
            ),
        ]
    )

    result = sync.reconcile_initial_create(job_id="job_phase6_sync", draft=draft)

    assert result.outcome is CreateReconciliationOutcome.AMBIGUOUS
    assert result.ambiguity_reason is CreateAmbiguityReason.CANONICAL_CONFLICT
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


def test_reconciliation_compares_every_variant_while_accepting_provider_enrichment(listing) -> None:
    draft = canonical_draft(listing)
    second_variant = draft.variants[0].model_copy(
        update={"id": 1001, "sku": f"{draft.correlation_token}-1001"}
    )
    print_area = draft.print_areas[0].model_copy(update={"variant_ids": (1000, 1001)})
    two_variant_draft = draft.model_copy(
        update={
            "variants": (draft.variants[0], second_variant),
            "print_areas": (print_area,),
        }
    )
    product = provider_product(two_variant_draft)
    for variant in product["variants"]:
        variant.update({"title": "Provider-enriched variant"})
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products.json",
                payload={"data": [product]},
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=product,
            ),
        ]
    )

    result = sync.reconcile_initial_create(
        job_id="job_phase6_sync",
        draft=two_variant_draft,
    )

    assert result.outcome is CreateReconciliationOutcome.ONE
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


def test_response_fingerprint_binds_provider_costs_and_mockups_not_only_request(listing) -> None:
    draft = canonical_draft(listing)
    first_product = provider_product(draft)
    changed_product = provider_product(draft)
    changed_product["variants"][0]["cost"] = 1200
    changed_product["images"] = [{"src": "https://images.printify.com/product_1/side.jpg"}]
    first_sync, _first_transport, _client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                payload=first_product,
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=first_product,
            ),
        ]
    )
    second_sync, _second_transport, _second_client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                payload=changed_product,
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=changed_product,
            ),
        ]
    )

    first = first_sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)
    second = second_sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)

    assert first.request_fingerprint == second.request_fingerprint
    assert first.response_fingerprint != second.response_fingerprint


def test_successful_create_requires_final_exact_get_readback(listing) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                payload=provider_product(draft),
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                error=PrintifyUnavailableError("eventual readback unavailable"),
            ),
        ]
    )

    with pytest.raises(PrintifyCreateOutcomeUnknown, match="without another POST"):
        sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)

    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_mockup_evidence_requires_exact_printify_image_host(listing) -> None:
    draft = canonical_draft(listing)
    off_host = provider_product(draft)
    off_host["images"] = [{"src": "https://images-api.printify.com/product_1/front.jpg"}]
    sync, _transport, _client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                payload=off_host,
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=off_host,
            ),
        ]
    )

    with pytest.raises(PrintifyCreateOutcomeUnknown, match="reconcile"):
        sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)


def test_mockup_evidence_rejects_variant_identity_outside_exact_draft(listing) -> None:
    draft = canonical_draft(listing)
    wrong_variant = provider_product(draft)
    wrong_variant["images"][0]["variant_ids"] = [9999]
    sync, _transport, _client = synchronizer(
        [
            ExpectedRequest(
                "POST",
                "/v1/shops/42/products.json",
                payload=wrong_variant,
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=wrong_variant,
            ),
        ]
    )

    with pytest.raises(PrintifyCreateOutcomeUnknown, match="reconcile"):
        sync.synchronize(job_id="job_phase6_sync", draft=draft, product_id=None)


def test_reconciliation_scans_bounded_pages_for_job_correlation(listing) -> None:
    draft = canonical_draft(listing)
    first_page = []
    for index in range(50):
        product = provider_product(draft, product_id=f"unrelated_{index}")
        product["variants"][0]["sku"] = f"other-{index}"
        first_page.append(product)
    correlated = provider_product(draft)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products.json",
                payload={"data": first_page, "last_page": 2},
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products.json",
                payload={"data": [correlated], "last_page": 2},
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=correlated,
            ),
        ]
    )

    result = sync.reconcile_initial_create(job_id="job_phase6_sync", draft=draft)

    assert result.outcome is CreateReconciliationOutcome.ONE
    assert [call["query"] for call in transport.calls[:2]] == [
        "page=1&limit=50",
        "page=2&limit=50",
    ]


def test_draft_client_uploads_png_through_same_guarded_origin() -> None:
    file_name = f"mr-lister-{'a' * 24}-{'b' * 16}.png"
    transport = ScriptedTransport(
        [
            ExpectedRequest(
                "POST",
                "/v1/uploads/images.json",
                payload={
                    "id": "image_1",
                    "file_name": file_name,
                    "width": 100,
                    "height": 100,
                    "size": 12,
                    "mime_type": "image/png",
                },
            )
        ]
    )
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    uploaded = client.upload_artwork_contents(
        file_name=file_name,
        content_type="image/png",
        content=b"\x89PNG\r\n\x1a\nrest",
    )

    assert uploaded.image_id == "image_1"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer private-token"
    body = json.loads(transport.calls[0]["body"])
    assert body["file_name"] == file_name
    assert "contents" in body


def test_draft_client_rejects_phase6_max_plus_one_before_transport() -> None:
    file_name = f"mr-lister-{'a' * 24}-{'b' * 16}.png"
    transport = ScriptedTransport([])
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )
    oversized = b"\x89PNG\r\n\x1a\n" + b"x" * (PHASE6_MAX_SOURCE_ARTWORK_BYTES - 7)

    with pytest.raises(PrintifyInputError, match="safe base64"):
        client.upload_artwork_contents(
            file_name=file_name,
            content_type="image/png",
            content=oversized,
        )

    assert len(oversized) == PHASE6_MAX_SOURCE_ARTWORK_BYTES + 1
    assert transport.calls == []


def test_upload_reconciliation_uses_bounded_list_then_exact_id_get() -> None:
    file_name = f"mr-lister-{'a' * 24}-{'b' * 16}.png"
    upload = {
        "id": "image_1",
        "file_name": file_name,
        "width": 100,
        "height": 100,
        "size": 12,
        "mime_type": "image/png",
    }
    transport = ScriptedTransport(
        [
            ExpectedRequest(
                "GET",
                "/v1/uploads.json",
                payload={"data": [upload], "last_page": 1},
            ),
            ExpectedRequest("GET", "/v1/uploads/image_1.json", payload=upload),
        ]
    )
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    listed = client.list_uploads(file_name=file_name)
    exact = client.get_upload(image_id=listed[0].image_id)

    assert listed == (exact,)
    assert transport.calls[0]["query"] == "page=1&limit=50"
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


def test_upload_reconciliation_ignores_unrelated_mixed_and_malformed_media() -> None:
    file_name = f"mr-lister-{'a' * 24}-{'b' * 16}.png"
    exact_upload = {
        "id": "image_exact",
        "file_name": file_name,
        "width": 100,
        "height": 100,
        "size": 12,
        "mime_type": "image/png",
    }
    transport = ScriptedTransport(
        [
            ExpectedRequest(
                "GET",
                "/v1/uploads.json",
                payload={
                    "data": [
                        {
                            "id": "seller_photo",
                            "file_name": "seller-photo.jpg",
                            "width": 1200,
                            "height": 800,
                            "size": 2048,
                            "mime_type": "image/jpeg",
                        },
                        {"file_name": "legacy-broken.png"},
                        exact_upload,
                    ],
                    "last_page": 1,
                },
            )
        ]
    )
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    listed = client.list_uploads(file_name=file_name)

    assert tuple(upload.image_id for upload in listed) == ("image_exact",)


@pytest.mark.parametrize(
    "malformed",
    [
        {
            "file_name": f"mr-lister-{'a' * 24}-{'b' * 16}.png",
            "width": 100,
            "height": 100,
            "size": 12,
            "mime_type": "image/png",
        },
        {
            "id": "image_exact",
            "file_name": f"mr-lister-{'a' * 24}-{'b' * 16}.png",
            "width": 100,
            "height": 100,
            "size": 12,
            "mime_type": "image/jpeg",
        },
    ],
)
def test_upload_reconciliation_rejects_malformed_exact_name_candidate(
    malformed: dict[str, object],
) -> None:
    file_name = f"mr-lister-{'a' * 24}-{'b' * 16}.png"
    transport = ScriptedTransport(
        [
            ExpectedRequest(
                "GET",
                "/v1/uploads.json",
                payload={"data": [malformed], "last_page": 1},
            )
        ]
    )
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    with pytest.raises(PrintifyCatalogMismatchError, match="invalid upload record"):
        client.list_uploads(file_name=file_name)


def test_upload_reconciliation_preserves_duplicate_exact_name_candidates() -> None:
    file_name = f"mr-lister-{'a' * 24}-{'b' * 16}.png"
    uploads = [
        {
            "id": f"image_{index}",
            "file_name": file_name,
            "width": 100,
            "height": 100,
            "size": 12,
            "mime_type": "image/png",
        }
        for index in range(2)
    ]
    transport = ScriptedTransport(
        [
            ExpectedRequest(
                "GET",
                "/v1/uploads.json",
                payload={"data": uploads, "last_page": 1},
            )
        ]
    )
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    listed = client.list_uploads(file_name=file_name)

    assert tuple(upload.image_id for upload in listed) == ("image_0", "image_1")


def test_upload_reconciliation_paginates_at_printify_maximum_page_size() -> None:
    file_name = f"mr-lister-{'a' * 24}-{'b' * 16}.png"
    unrelated_uploads = [
        {
            "id": f"image_{index}",
            "file_name": f"upload-{index}.png",
            "width": 100,
            "height": 100,
            "size": 12,
            "mime_type": "image/png",
        }
        for index in range(50)
    ]
    exact_upload = {
        "id": "image_exact",
        "file_name": file_name,
        "width": 100,
        "height": 100,
        "size": 12,
        "mime_type": "image/png",
    }
    transport = ScriptedTransport(
        [
            ExpectedRequest("GET", "/v1/uploads.json", payload={"data": unrelated_uploads}),
            ExpectedRequest("GET", "/v1/uploads.json", payload={"data": [exact_upload]}),
        ]
    )
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    listed = client.list_uploads(file_name=file_name)

    assert tuple(upload.image_id for upload in listed) == ("image_exact",)
    assert [call["query"] for call in transport.calls] == [
        "page=1&limit=50",
        "page=2&limit=50",
    ]


def test_uncertain_upload_response_never_becomes_a_second_post_authority() -> None:
    file_name = f"mr-lister-{'a' * 24}-{'b' * 16}.png"
    transport = ScriptedTransport(
        [
            ExpectedRequest(
                "POST",
                "/v1/uploads/images.json",
                error=PrintifyUnavailableError("connection lost"),
            )
        ]
    )
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    with pytest.raises(PrintifyUploadOutcomeUnknown, match="without another POST"):
        client.upload_artwork_contents(
            file_name=file_name,
            content_type="image/png",
            content=b"\x89PNG\r\n\x1a\nrest",
        )

    assert [call["method"] for call in transport.calls] == ["POST"]


def test_reconciliation_detects_a_conflict_in_any_canonical_variant(listing) -> None:
    draft = canonical_draft(listing)
    second_variant = draft.variants[0].model_copy(
        update={"id": 1001, "sku": f"{draft.correlation_token}-1001"}
    )
    print_area = draft.print_areas[0].model_copy(update={"variant_ids": (1000, 1001)})
    two_variant_draft = draft.model_copy(
        update={
            "variants": (draft.variants[0], second_variant),
            "print_areas": (print_area,),
        }
    )
    product = provider_product(two_variant_draft)
    product["variants"][1]["price"] = 1
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products.json",
                payload={"data": [product]},
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=product,
            ),
        ]
    )

    result = sync.reconcile_initial_create(
        job_id="job_phase6_sync",
        draft=two_variant_draft,
    )

    assert result.outcome is CreateReconciliationOutcome.AMBIGUOUS
    assert result.ambiguity_reason is CreateAmbiguityReason.CANONICAL_CONFLICT
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


def test_create_reconciliation_classifies_multiple_correlated_products_as_ambiguous(
    listing,
) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products.json",
                payload={
                    "data": [
                        provider_product(draft, product_id="product_1"),
                        provider_product(draft, product_id="product_2"),
                    ]
                },
            )
        ]
    )

    result = sync.reconcile_initial_create(job_id="job_phase6_sync", draft=draft)

    assert result.outcome is CreateReconciliationOutcome.AMBIGUOUS
    assert result.ambiguity_reason is CreateAmbiguityReason.MULTIPLE_CORRELATED_PRODUCTS
    assert [call["method"] for call in transport.calls] == ["GET"]


def test_create_reconciliation_classifies_correlated_payload_conflict_as_ambiguous(
    listing,
) -> None:
    draft = canonical_draft(listing)
    sync, transport, _client = synchronizer(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products.json",
                payload={"data": [provider_product(draft, title="Conflicting provider title")]},
            ),
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_1.json",
                payload=provider_product(draft, title="Conflicting provider title"),
            ),
        ]
    )

    result = sync.reconcile_initial_create(job_id="job_phase6_sync", draft=draft)

    assert result.outcome is CreateReconciliationOutcome.AMBIGUOUS
    assert result.ambiguity_reason is CreateAmbiguityReason.CANONICAL_CONFLICT
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "shops/42/products/product_1.json"),
        ("POST", "shops/42/products/product_1/publish.json"),
        ("POST", "shops/42/products/product_1/unpublish.json"),
        ("GET", "shops/42/products/product_1/publishing_succeeded.json"),
        ("POST", "shops/42/orders.json"),
        ("POST", "shops/42/webhooks.json"),
        ("PUT", "shops/42/orders/order_1/send_to_production.json"),
        ("PATCH", "shops/42/products/product_1.json"),
        ("POST", "shops/42/products.json?page=1&limit=50"),
        ("GET", "shops/42/products.json?page=21&limit=50"),
        ("GET", "uploads.json?page=1&limit=100"),
        ("GET", "uploads.json?page=21&limit=50"),
        ("POST", "uploads/image_1/archive.json"),
    ],
)
def test_draft_only_route_guard_rejects_every_non_draft_surface(method: str, path: str) -> None:
    with pytest.raises(PrintifyInputError, match="draft-only"):
        assert_draft_only_route(method=method, path=path)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.printify.com/v1/",
        "https://evil.test/v1/",
        "https://api.printify.com.evil.test/v1/",
        "https://api.printify.com:443/v1/",
        "https://user:secret@api.printify.com/v1/",
        "https://api.printify.com/v2/",
        "https://api.printify.com/v1/../v2/",
        "https://api.printify.com/v1/?redirect=1",
        "https://api.printify.com/v1/#fragment",
    ],
)
def test_client_rejects_every_noncanonical_api_base_before_transport(base_url: str) -> None:
    transport = ScriptedTransport([])

    with pytest.raises(ValueError, match="exact production API"):
        PrintifyDraftOnlyClient(
            token_provider=lambda: "private-token",
            base_url=base_url,
            transport=transport,
        )

    assert transport.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://api.printify.com/v1/shops/42/products.json?page=1&limit=50",
        "https://evil.test/v1/shops/42/products.json?page=1&limit=50",
        "https://api.printify.com.evil.test/v1/shops/42/products.json?page=1&limit=50",
        "https://api.printify.com:443/v1/shops/42/products.json?page=1&limit=50",
        "https://user:secret@api.printify.com/v1/shops/42/products.json?page=1&limit=50",
        "https://api.printify.com/v2/shops/42/products.json?page=1&limit=50",
        "https://api.printify.com/v1/../orders.json",
        "https://api.printify.com/v1/%2e%2e/orders.json",
        "https://api.printify.com/v1//shops/42/products.json?page=1&limit=50",
        "https://api.printify.com/v1/shops/42/products.json?page=1&limit=50#fragment",
    ],
)
def test_absolute_api_guard_rejects_host_scheme_port_credentials_and_path_escape(url: str) -> None:
    with pytest.raises(PrintifyInputError):
        assert_printify_api_url(method="GET", url=url)


def test_absolute_api_guard_accepts_only_exact_https_draft_route() -> None:
    assert_printify_api_url(
        method="GET",
        url="https://api.printify.com/v1/shops/42/products.json?page=1&limit=50",
    )


@pytest.mark.parametrize(
    "path",
    [
        "//evil.test/v1/shops/42/products.json",
        "https://evil.test/v1/shops/42/products.json",
        "shops/42/products.json#fragment",
    ],
)
def test_relative_route_guard_rejects_absolute_network_paths_and_fragments(path: str) -> None:
    with pytest.raises(PrintifyInputError):
        assert_draft_only_route(method="POST", path=path)


def test_redirect_handler_never_constructs_followup_request_with_bearer() -> None:
    original = Request(
        "https://api.printify.com/v1/shops/42/products.json",
        headers={"Authorization": "Bearer private-token"},
        method="POST",
    )

    redirected = _RejectRedirectHandler().redirect_request(
        original,
        None,
        302,
        "Found",
        {},
        "https://attacker.test/collect",
    )

    assert redirected is None


class RedirectResponseOpener:
    def __init__(self) -> None:
        self.requests: list[Request] = []

    def open(self, request: Request, *, timeout: float):
        del timeout
        self.requests.append(request)
        raise HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": "https://attacker.test/collect"},
            BytesIO(b"redirect refused"),
        )


def test_redirect_safe_transport_returns_3xx_without_second_request_or_token_forwarding() -> None:
    opener = RedirectResponseOpener()
    transport = RedirectSafePrintifyTransport(opener=opener)

    response = transport.request(
        method="POST",
        url="https://api.printify.com/v1/shops/42/products.json",
        headers={"Authorization": "Bearer private-token"},
        body=b"{}",
        timeout_seconds=15,
    )

    assert response.status == 302
    assert response.body == b"redirect refused"
    assert len(opener.requests) == 1
    assert opener.requests[0].host == "api.printify.com"
    assert opener.requests[0].get_header("Authorization") == "Bearer private-token"


def test_redirect_safe_transport_bounds_provider_response_reads() -> None:
    opener = RedirectResponseOpener()
    transport = RedirectSafePrintifyTransport(opener=opener)
    transport.MAX_RESPONSE_BYTES = 4

    with pytest.raises(PrintifyUnavailableError, match="safe read boundary"):
        transport.request(
            method="POST",
            url="https://api.printify.com/v1/shops/42/products.json",
            headers={"Authorization": "Bearer private-token"},
            body=b"{}",
            timeout_seconds=15,
        )


def test_draft_only_client_has_no_destructive_or_downstream_surface() -> None:
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=ScriptedTransport([]),
    )

    for forbidden_method in (
        "delete_product",
        "publish",
        "unpublish",
        "get_publishing_status",
        "create_order",
        "send_to_production",
        "fulfill_order",
        "cancel_order",
        "create_webhook",
        "upload_artwork_url",
    ):
        assert not hasattr(client, forbidden_method)


def test_inherited_request_primitive_cannot_bypass_draft_only_route_guard() -> None:
    transport = ScriptedTransport([])
    client = PrintifyDraftOnlyClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    with pytest.raises(PrintifyInputError, match="draft-only"):
        client._request_json(  # noqa: SLF001 - verifies the internal guard cannot be bypassed
            method="POST",
            path="shops/42/orders.json",
            payload={"unsafe": True},
        )

    assert transport.calls == []


def test_job_correlation_mismatch_fails_before_any_provider_call(listing) -> None:
    draft = canonical_draft(listing, job_id="job_one")
    sync, transport, _client = synchronizer([])

    with pytest.raises(PrintifyInputError, match="does not belong"):
        sync.synchronize(job_id="job_two", draft=draft, product_id=None)

    assert transport.calls == []
