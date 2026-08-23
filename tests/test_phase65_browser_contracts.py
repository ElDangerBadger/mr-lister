from __future__ import annotations

import json
from pathlib import Path

from mr_lister.cloud.browser_contracts import (
    BROWSER_CONTRACT_VERSION,
    ErrorEnvelope,
    JobProgressProjection,
    UploadRecoveryProjection,
    browser_contract_fixtures,
    browser_contract_schema,
)
from mr_lister.cloud.http import ALL_ROUTE_KEYS
from mr_lister.control.projection_models import SellerReviewProjection
from tools.export_phase65_browser_contracts import (
    DEFAULT_OUTPUT_DIRECTORY,
    drifted_artifacts,
    expected_artifacts,
    export_artifacts,
)


def test_browser_contract_schema_is_deterministic_closed_and_route_addressable() -> None:
    schema = browser_contract_schema()

    assert schema == browser_contract_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["x-mr-lister-contract-version"] == BROWSER_CONTRACT_VERSION
    routes = schema["x-mr-lister-routes"]
    assert set(routes) - {"*"} == set(ALL_ROUTE_KEYS)
    assert "GET /v1/uploads/{upload_id}" in routes
    assert routes["GET /v1/jobs/{job_id}"]["response"].endswith("/JobProgressProjection")
    assert all("publish" not in route.casefold() for route in routes)
    assert json.loads(json.dumps(schema, sort_keys=True)) == schema


def test_upload_recovery_schema_cannot_carry_object_or_credential_authority() -> None:
    recovery_schema = browser_contract_schema()["$defs"]["UploadRecoveryProjection"]
    properties = set(recovery_schema["properties"])

    assert properties.isdisjoint(
        {
            "owner_id",
            "bucket",
            "object_key",
            "content_sha256",
            "checksum_sha256_base64",
            "version_id",
            "url",
            "form_fields",
            "policy",
            "signature",
        }
    )
    assert recovery_schema["additionalProperties"] is False


def test_golden_fixtures_validate_against_their_public_runtime_models() -> None:
    fixtures = browser_contract_fixtures()

    def from_json(model: type, fixture: object):
        return model.model_validate_json(json.dumps(fixture))

    from_json(UploadRecoveryProjection, fixtures["upload_recovery"])
    progress = from_json(JobProgressProjection, fixtures["job_progress"])
    from_json(SellerReviewProjection, fixtures["seller_review_pending"])
    from_json(ErrorEnvelope, fixtures["validation_error"])
    assert progress.authority_notice == "Unpublished — not on Etsy"
    assert "state" not in fixtures["job_progress"]
    assert "content_sha256" not in fixtures["upload_recovery"]
    assert fixtures == browser_contract_fixtures()


def test_checked_in_browser_artifacts_are_an_exact_deterministic_export(tmp_path: Path) -> None:
    expected = expected_artifacts()

    assert drifted_artifacts(DEFAULT_OUTPUT_DIRECTORY) == ()
    assert {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(DEFAULT_OUTPUT_DIRECTORY.glob("phase6.5.*.json"))
    } == expected

    written = export_artifacts(tmp_path)
    assert tuple(path.name for path in written) == tuple(sorted(expected))
    assert {path.name: path.read_text(encoding="utf-8") for path in written} == expected


def test_browser_source_has_no_commerce_or_durable_client_storage_capability() -> None:
    source_root = Path("web/src")
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(source_root.rglob("*"))
        if path.suffix in {".ts", ".tsx"}
    )
    transport = (source_root / "api" / "client.ts").read_text(encoding="utf-8").casefold()

    assert all(
        fragment not in transport
        for fragment in (
            "/publish",
            "/orders",
            "/fulfillment",
            "api.printify.com",
            "etsy.com",
        )
    )
    assert all(
        capability not in source
        for capability in (
            "dangerouslySetInnerHTML",
            "document.cookie",
            "indexedDB",
            "localStorage",
            "navigator.serviceWorker",
            "window.eval",
        )
    )
