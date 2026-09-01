from __future__ import annotations

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


def test_offline_browser_is_outside_active_source_and_has_no_durable_client_state() -> None:
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
    assert "offline/phase7" not in active_source
    assert "../offline" not in active_source
    assert "/publish" not in active_source
    assert "publish_exact_approved_listing" not in active_source
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
