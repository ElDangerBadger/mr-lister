from __future__ import annotations

import json
from datetime import timedelta
from io import BytesIO
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from mr_lister.judge_cleanup.models import PublicationEvidence
from mr_lister.judge_cleanup.provider import ExactProductTransport, PrintifyCleanupProvider
from mr_lister.judge_cleanup.service import CleanupRunError, ProviderMismatchError
from mr_lister.production.draft_sync import job_correlation_token, printify_etsy_variant_sku
from mr_lister.production.printify import PrintifyAuthenticationError, PrintifyHttpResponse
from mr_lister.production.provider_resources import OwnerPrintifyConnection
from mr_lister.production.provider_secrets import SecretsManagerOwnerPrintifyConnectionResolver
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.fingerprints import (
    canonical_fingerprint,
    publication_snapshot_fingerprint,
)
from mr_lister.publication.provider_boundary import _canonical_product_readback
from tests.test_judge_cleanup import SECRET_ARN, config_for
from tests.test_judge_cleanup import evidence as evidence


def _refingerprint(model, kind, **changes):
    candidate = model.model_copy(update=changes)
    return type(model).model_validate(
        {**candidate.model_dump(), "fingerprint": execution_record_fingerprint(kind, candidate)}
    )


def product_and_evidence(evidence):
    s = evidence.snapshot
    variant = evidence.provider.expected_variant_economics[0]
    payload = {
        "id": s.printify_product_id,
        "shop_id": s.printify_shop_id,
        "title": "Exact judge artwork",
        "description": "Exact judge description",
        "tags": ["judge artwork"],
        "blueprint_id": 145,
        "print_provider_id": 39,
        "variants": [
            {
                "id": variant.variant_id,
                "price": variant.retail_price_cents,
                "is_enabled": True,
                "sku": printify_etsy_variant_sku(
                    correlation_token=job_correlation_token(s.job_id), variant_id=variant.variant_id
                ),
            }
        ],
        "print_areas": [
            {
                "variant_ids": [variant.variant_id],
                "placeholders": [
                    {
                        "position": "front",
                        "images": [
                            {
                                "id": s.printify_image_id,
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
        "images": [
            {
                "src": "https://images.printify.com/product_phase71/front.jpg",
                "position": "front",
                "variant_ids": [variant.variant_id],
                "is_default": True,
            }
        ],
        "external": [
            {
                "id": str(evidence.result.numeric_listing_id),
                "handle": "https://www.etsy.com/listing/123456789",
            }
        ],
        "visible": True,
        "is_locked": False,
    }
    canonical, _ = _canonical_product_readback(
        payload, expected_variant_ids=(variant.variant_id,), job_id=s.job_id
    )
    content_fingerprint = canonical_fingerprint(canonical.model_dump(mode="json"))
    candidate = s.model_copy(update={"product_payload_fingerprint": content_fingerprint})
    snapshot = type(s).model_validate(
        {**candidate.model_dump(), "fingerprint": publication_snapshot_fingerprint(candidate)}
    )
    provider = _refingerprint(
        evidence.provider,
        "provider_authority",
        snapshot_fingerprint=snapshot.fingerprint,
        product_payload_fingerprint=content_fingerprint,
    )
    observation = _refingerprint(
        evidence.observation,
        "product_observation",
        snapshot_fingerprint=snapshot.fingerprint,
        provider_authority_fingerprint=provider.fingerprint,
    )
    result = _refingerprint(
        evidence.result, "publication_result", observation_fingerprint=observation.fingerprint
    )
    aggregate = _refingerprint(
        evidence.aggregate,
        "execution_aggregate",
        snapshot_fingerprint=snapshot.fingerprint,
        last_observation_fingerprint=observation.fingerprint,
    )
    bound = PublicationEvidence(
        job=evidence.job.model_copy(update={"provider_payload_fingerprint": content_fingerprint}),
        aggregate=aggregate,
        snapshot=snapshot,
        provider=provider,
        observation=observation,
        result=result,
    )
    return payload, bound


def setup_provider(evidence, responses, *, active=True):
    config = config_for(evidence, active=active)
    resolver = Mock()
    resolver.resolve_primary.return_value = OwnerPrintifyConnection(
        owner_id=config.primary_owner_id,
        shop_id=config.printify_shop_id,
        api_token="synthetic-provider-secret",
    )
    transport = Mock()
    transport.request.side_effect = [
        PrintifyHttpResponse(status=code, body=json.dumps(payload).encode())
        for code, payload in responses
    ]
    provider = PrintifyCleanupProvider(
        config=config, resolver=resolver, transport=transport, clock=lambda: evidence.deadline
    )
    return provider, resolver, transport


def test_real_canonical_provider_validation_and_exact_get_delete_get(evidence):
    payload, evidence = product_and_evidence(evidence)
    provider, resolver, transport = setup_provider(evidence, [(200, payload), (200, {}), (404, {})])
    assert provider.present(evidence)
    provider.delete(evidence)
    assert provider.present(evidence) is False
    assert [call.kwargs["method"] for call in transport.request.call_args_list] == [
        "GET",
        "DELETE",
        "GET",
    ]
    expected = f"https://api.printify.com/v1/shops/{evidence.snapshot.printify_shop_id}/products/{evidence.snapshot.printify_product_id}.json"
    for call in transport.request.call_args_list:
        assert call.kwargs["url"] == expected and call.kwargs["body"] is None
        assert call.kwargs["timeout_seconds"] == 5
    assert all(
        call.kwargs == {"owner_id": provider.config.primary_owner_id}
        for call in resolver.resolve_primary.call_args_list
    )
    assert resolver.resolve_primary.call_count == 3
    resolver.resolve.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "shop",
        "product",
        "listing",
        "locked",
        "hidden",
        "title",
        "price",
        "sku",
        "image",
        "mockup",
        "malformed_external",
    ],
)
def test_changed_provider_identity_or_publication_content_blocks_cleanup(evidence, change):
    payload, evidence = product_and_evidence(evidence)
    if change == "shop":
        payload["shop_id"] += 1
    elif change == "product":
        payload["id"] = "unrelated"
    elif change == "listing":
        payload["external"][0]["id"] = "999"
    elif change == "locked":
        payload["is_locked"] = True
    elif change == "hidden":
        payload["visible"] = False
    elif change == "title":
        payload["title"] = "Seller edited this after publishing"
    elif change == "price":
        payload["variants"][0]["price"] += 1
    elif change == "sku":
        payload["variants"][0]["sku"] = "another-job"
    elif change == "image":
        payload["print_areas"][0]["placeholders"][0]["images"][0]["id"] = "other-artwork"
    elif change == "mockup":
        payload["images"][0]["src"] = "https://images.printify.com/other.jpg"
    elif change == "malformed_external":
        payload["external"] = {"id": "123456789"}
    provider, resolver, transport = setup_provider(evidence, [(200, payload)])
    if change == "owner":
        resolver.resolve_primary.return_value = resolver.resolve_primary.return_value.model_copy(
            update={"owner_id": "c" * 64}
        )
    with pytest.raises(ProviderMismatchError):
        provider.present(evidence)
    assert all(call.kwargs["method"] == "GET" for call in transport.request.call_args_list)


def test_direct_provider_boundary_rejects_dry_run_early_and_tampered_evidence(evidence):
    payload, evidence = product_and_evidence(evidence)
    provider, resolver, transport = setup_provider(evidence, [], active=False)
    with pytest.raises(ProviderMismatchError):
        provider.delete(evidence)
    provider, resolver, transport = setup_provider(evidence, [])
    provider.clock = lambda: evidence.deadline - timedelta(seconds=1)
    with pytest.raises(ProviderMismatchError):
        provider.delete(evidence)
    tampered = evidence.model_copy(
        update={"job": evidence.job.model_copy(update={"owner_id": "c" * 64})}
    )
    with pytest.raises(ValueError):
        provider.present(tampered)
    assert transport.mock_calls == []


@pytest.mark.parametrize("code", [301, 401, 403, 409, 429, 500])
def test_provider_non_successes_never_become_absence(evidence, code):
    provider, resolver, transport = setup_provider(evidence, [(code, {"error": "private data"})])
    with pytest.raises(CleanupRunError):
        provider.present(evidence)


def test_primary_cleanup_connection_survives_expired_judge_grant(evidence):
    config = config_for(evidence)
    secret = {
        "schema_version": "phase6-printify-owner-v2",
        "owner_id": config.primary_owner_id,
        "shop_id": config.printify_shop_id,
        "api_token": "synthetic-provider-secret",
        "delegated_owner_grants": [
            {"owner_id": config.judge_owner_id, "expires_at": "2026-08-01T00:00:00Z"}
        ],
    }
    client = Mock()
    client.get_secret_value.return_value = {
        "ARN": SECRET_ARN,
        "VersionId": "current-version",
        "VersionStages": ["AWSCURRENT"],
        "SecretString": json.dumps(secret),
    }
    resolver = SecretsManagerOwnerPrintifyConnectionResolver(
        client=client, secret_arn=SECRET_ARN, clock=lambda: evidence.deadline
    )
    connection = resolver.resolve_primary(owner_id=config.primary_owner_id)
    assert (
        connection.owner_id == config.primary_owner_id
        and connection.shop_id == config.printify_shop_id
    )
    with pytest.raises(PrintifyAuthenticationError):
        resolver.resolve(owner_id=config.judge_owner_id)


@pytest.mark.parametrize(
    "method,url,body",
    [
        ("POST", "https://api.printify.com/v1/shops/1/products/a.json", None),
        ("DELETE", "https://api.printify.com/v1/shops/1/products.json", None),
        ("DELETE", "https://evil.example/v1/shops/1/products/a.json", None),
        ("DELETE", "https://api.printify.com/v1/shops/1/products/a.json?other=1", None),
        ("DELETE", "https://api.printify.com/v1/shops/1/products/a.json", b"{}"),
    ],
)
def test_transport_rejects_catalog_other_hosts_and_extra_payloads(method, url, body):
    opener = Mock()
    with pytest.raises(ProviderMismatchError):
        ExactProductTransport(opener).request(
            method=method, url=url, headers={}, body=body, timeout_seconds=5
        )
    opener.open.assert_not_called()


def test_transport_returns_redirect_without_following_and_bounds_response():
    opener = Mock()
    target = "https://api.printify.com/v1/shops/1/products/a.json"
    opener.open.side_effect = HTTPError(
        target, 302, "private", {"Location": "https://evil.example"}, BytesIO(b"{}")
    )
    response = ExactProductTransport(opener).request(
        method="DELETE",
        url=target,
        headers={"Authorization": "Bearer private"},
        body=None,
        timeout_seconds=5,
    )
    assert response.status == 302 and opener.open.call_count == 1
    from mr_lister.judge_cleanup.provider import _NoRedirect

    assert (
        _NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.example") is None
    )
