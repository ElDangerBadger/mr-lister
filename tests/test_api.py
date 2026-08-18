from __future__ import annotations

from base64 import b64decode

from fastapi.testclient import TestClient

from mr_lister.api.app import create_app
from mr_lister.workflow.service import ListingWorkflow

SYNTHETIC_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+XhC1AAAAAElFTkSuQmCC"
)


def upload(client: TestClient, *, key: str, content: bytes = SYNTHETIC_PNG):
    return client.post(
        "/jobs",
        headers={"Idempotency-Key": key},
        files={"artwork": ("geometric_badger.png", content, "image/png")},
        data={"profile_id": "synthetic_gildan_5000"},
    )


def test_api_exposes_complete_fake_vertical_slice(workflow: ListingWorkflow) -> None:
    client = TestClient(create_app(workflow))

    created = upload(client, key="api-intake-001")
    assert created.status_code == 201
    job_id = created.json()["job_id"]
    assert created.json()["state"] == "awaiting_approval"

    status = client.get(f"/jobs/{job_id}")
    review = client.get(f"/jobs/{job_id}/review")
    assert status.status_code == 200
    assert review.json()["review_version"] == 1
    assert len(review.json()["listing"]["tags"]) == 13

    approved = client.post(f"/jobs/{job_id}/approve", json={"review_version": 1})
    published = client.post(f"/jobs/{job_id}/publish")
    report = client.get(f"/jobs/{job_id}/report")

    assert approved.json()["state"] == "approved"
    assert published.json()["state"] == "verified"
    assert report.json()["job"]["published_listing_id"].startswith("fake-listing-")
    assert report.json()["events"][-1]["name"] == "publication_verified"


def test_api_returns_stable_error_for_invalid_artwork(workflow: ListingWorkflow) -> None:
    client = TestClient(create_app(workflow))

    response = upload(client, key="api-invalid", content=b"not-a-png")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ARTWORK"


def test_api_rejects_conflicting_idempotency_key(workflow: ListingWorkflow) -> None:
    client = TestClient(create_app(workflow))
    assert upload(client, key="api-conflict").status_code == 201

    response = upload(client, key="api-conflict", content=SYNTHETIC_PNG + b"different")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
