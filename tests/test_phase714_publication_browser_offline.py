from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mr_lister.cloud import phase7_entrypoints
from mr_lister.cloud.http import ALL_ROUTE_KEYS
from mr_lister.publication.commands import PublicationRequestResponse
from mr_lister.publication.projection_models import SellerPublicationProjection
from mr_lister.publication.request_api import PublicationRequestBody

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts/publication/phase7.0.1.browser.fixtures.json"
OFFLINE_BROWSER = ROOT / "web/offline/phase7"
ACTIVE_BROWSER = ROOT / "web/src"
ACTIVE_PUBLICATION_BROWSER = ACTIVE_BROWSER / "publication"

FROZEN_OFFLINE_BROWSER_SHA256 = {
    "PublicationWorkspace.tsx": "ff2e1844ce5823be25abc7d4f49a63f7263de960b09f8900008de2ab9e2f4862",
    "activation.ts": "78b911943023d6bd4b03833f766620ceb4ef93e3a83fe948bcbe90f668f700b9",
    "api-client.ts": "f5196e9337d4c11d79ad2e0b17407aaa4d0ca275fa4168711f3172fe320a3d9b",
    "contracts.ts": "931152123a0ea5c7c23b22be9a66fb6eedf7fb5a2062251c97df1bcc37b28b30",
}


def _fixtures() -> dict[str, object]:
    value = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_golden_browser_values_are_exact_phase7_api_models() -> None:
    fixtures = _fixtures()
    body = PublicationRequestBody.model_validate(fixtures["publication_request_body"])
    response = PublicationRequestResponse.model_validate_json(
        json.dumps(fixtures["publication_request_response"])
    )
    raw_projections = fixtures["projections"]
    assert isinstance(raw_projections, dict)
    projections = {
        name: SellerPublicationProjection.model_validate_json(json.dumps(value))
        for name, value in raw_projections.items()
    }

    assert body.confirmation == "publish_exact_approved_listing"
    assert response.record_version == body.expected_record_version + 1
    assert response.review_version == body.expected_review_version
    assert set(projections) == {
        "failed",
        "not_requested",
        "outcome_unknown",
        "preflight",
        "published",
        "publishing",
        "queued",
        "reconciling",
        "verifying",
    }
    assert {projection.stage.value for projection in projections.values()} == {
        "awaiting_activation",
        "complete",
        "preflight",
        "publishing",
        "queued",
        "reconciling",
        "verifying",
    }
    published = projections["published"]
    assert published.safe_listing_url == "https://www.etsy.com/listing/123456789"
    assert published.notification_available is True
    assert published.verified_at is not None
    assert projections["outcome_unknown"].safe_listing_url is None
    assert projections["outcome_unknown"].notification_available is False
    assert all(projection.publication_enabled is False for projection in projections.values())
    assert all(projection.request_enabled is False for projection in projections.values())


def test_frozen_offline_browser_stays_byte_exact_and_outside_the_active_successor() -> None:
    offline_files = sorted(OFFLINE_BROWSER.glob("*.ts*"))
    assert {path.name for path in offline_files} == {
        "PublicationWorkspace.tsx",
        "activation.ts",
        "api-client.ts",
        "contracts.ts",
    }
    offline_source = "\n".join(path.read_text(encoding="utf-8") for path in offline_files)
    active_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(ACTIVE_BROWSER.rglob("*"))
        if path.suffix in {".ts", ".tsx"}
    )

    assert OFFLINE_BROWSER.parent.parent == ROOT / "web"
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in offline_files
    } == FROZEN_OFFLINE_BROWSER_SHA256
    assert "offline/phase7" not in active_source
    assert "../offline" not in active_source
    active_publication_files = sorted(ACTIVE_PUBLICATION_BROWSER.glob("*.ts*"))
    assert {path.name for path in active_publication_files} == {
        "PublicationWorkspace.tsx",
        "api-client.ts",
        "contracts.ts",
    }
    active_publication_source = "\n".join(
        path.read_text(encoding="utf-8") for path in active_publication_files
    )
    assert 'z.literal("7.1.0")' in active_publication_source
    assert "/publish" in active_publication_source
    assert "publish_exact_approved_listing" in active_publication_source
    for forbidden_capability in (
        "@aws-sdk",
        "api.printify.com",
        "phase7_worker",
        "provider_runtime",
        "publish.json",
        "secretsmanager",
    ):
        assert forbidden_capability not in active_publication_source.casefold()
    assert "localStorage" not in offline_source
    assert "sessionStorage" not in offline_source
    assert "indexedDB" not in offline_source
    assert "navigator.serviceWorker" not in offline_source


def test_no_runtime_route_or_entrypoint_is_added_by_the_offline_matrix() -> None:
    assert "POST /v1/jobs/{job_id}/publish" not in ALL_ROUTE_KEYS
    assert "GET /v1/jobs/{job_id}/publication" not in ALL_ROUTE_KEYS
    assert phase7_entrypoints.__all__ == ["publication_query_api_handler"]
    assert not hasattr(phase7_entrypoints, "publication_request_api_handler")
    assert not hasattr(phase7_entrypoints, "publication_worker_handler")
