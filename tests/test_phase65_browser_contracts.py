from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mr_lister.cloud.browser_contracts import (
    BROWSER_CONTRACT_VERSION,
    ClearRecentJobsRequest,
    ClearRecentJobsResponse,
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
    assert routes["POST /v1/jobs/recent/clear"] == {
        "request": "#/$defs/ClearRecentJobsRequest",
        "response": "#/$defs/ClearRecentJobsResponse",
    }
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
    from_json(ClearRecentJobsRequest, {})
    from_json(ClearRecentJobsResponse, fixtures["clear_recent_jobs"])
    progress = from_json(JobProgressProjection, fixtures["job_progress"])
    from_json(SellerReviewProjection, fixtures["seller_review_pending"])
    from_json(ErrorEnvelope, fixtures["validation_error"])
    assert progress.authority_notice == "Unpublished — not on Etsy"
    assert "state" not in fixtures["job_progress"]
    assert "content_sha256" not in fixtures["upload_recovery"]
    assert fixtures == browser_contract_fixtures()


def test_clear_history_contract_carries_only_an_empty_request_and_aware_cutoff() -> None:
    definitions = browser_contract_schema()["$defs"]
    assert definitions["ClearRecentJobsRequest"]["properties"] == {}
    assert definitions["ClearRecentJobsRequest"]["additionalProperties"] is False
    assert set(definitions["ClearRecentJobsResponse"]["properties"]) == {"cleared_before"}
    assert definitions["ClearRecentJobsResponse"]["additionalProperties"] is False
    with pytest.raises(ValueError):
        ClearRecentJobsRequest.model_validate({"owner_id": "a" * 64})
    with pytest.raises(ValueError):
        ClearRecentJobsResponse.model_validate_json('{"cleared_before": "2026-09-14T12:00:00"}')
    normalized = ClearRecentJobsResponse.model_validate_json(
        '{"cleared_before": "2026-09-14T12:00:00-07:00"}'
    )
    assert normalized.model_dump(mode="json") == {"cleared_before": "2026-09-14T19:00:00Z"}


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


def _browser_sources() -> dict[str, str]:
    source_root = Path("web/src")
    return {
        path.relative_to(source_root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(source_root.rglob("*"))
        if path.suffix in {".ts", ".tsx"}
    }


def test_browser_source_has_no_commerce_or_durable_seller_storage_capability() -> None:
    source_root = Path("web/src")
    sources = _browser_sources()
    source = "\n".join(sources.values())
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
            "navigator.serviceWorker",
            "window.eval",
        )
    )

    _assert_durable_storage_boundary(sources)


def _assert_durable_storage_boundary(sources: dict[str, str]) -> None:
    # Display preference and a fixed judge cancellation marker are the only
    # durable-storage exceptions. Neither can contain credentials or seller data.
    # Keep this at individual call sites: excluding whole theme files would also
    # permit them to persist seller data. Behavioral web tests constrain the key
    # and values to mr-lister-display-theme and light/dark/auto.
    display_storage_calls = {
        "theme.ts": "window.localStorage.getItem(THEME_STORAGE_KEY)",
        "components/ThemeControl.tsx": "window.localStorage.setItem(THEME_STORAGE_KEY, value)",
    }
    for path, contents in sources.items():
        if path == "auth/judge-session.ts":
            # Retain the fixed key/value and probe, and constrain every use of the
            # storage handle. Merely excluding its localStorage acquisition would
            # silently permit arbitrary writes through the alias later in the file.
            approved_judge_storage = {
                'export const JUDGE_RESTORE_BLOCK_KEY = "mr-lister.judge-restore-blocked.v1";': 1,
                "const probe = `${JUDGE_RESTORE_BLOCK_KEY}.storage-check`;": 1,
                "const storage = window.localStorage;": 1,
                'storage.setItem(probe, "1")': 1,
                "storage.removeItem(probe)": 1,
                "return storage;": 1,
                "private readonly storage: Storage | null = restorationStorage()": 1,
                "this.storage === null": 2,
                "this.storage.getItem(JUDGE_RESTORE_BLOCK_KEY)": 2,
                'this.storage?.setItem(JUDGE_RESTORE_BLOCK_KEY, "1")': 1,
                "this.storage.removeItem(JUDGE_RESTORE_BLOCK_KEY)": 1,
            }
            for expression, expected_count in approved_judge_storage.items():
                assert contents.count(expression) == expected_count, (path, expression)
                contents = contents.replace(expression, "")
            assert re.search(r"\bstorage\b", contents) is None, path
        permitted_call = display_storage_calls.get(path)
        if permitted_call is not None:
            assert contents.count(permitted_call) == 1, path
            contents = contents.replace(permitted_call, "")
        assert "localStorage" not in contents, path


@pytest.mark.parametrize(
    "extra_source",
    [
        'this.storage?.setItem("seller-data", tokens.access_token);',
        "this.storage?.setItem(JUDGE_RESTORE_BLOCK_KEY, tokens.access_token);",
        "const anotherStore = this.storage;",
        'window.localStorage.setItem("seller-data", "listing");',
    ],
)
def test_judge_storage_exception_cannot_expand_to_seller_or_token_storage(
    extra_source: str,
) -> None:
    sources = _browser_sources()
    sources["auth/judge-session.ts"] += "\n" + extra_source

    with pytest.raises(AssertionError):
        _assert_durable_storage_boundary(sources)
