"""Hermetic release, readback, and rollback gates for the Phase 7.18 seller web."""

from __future__ import annotations

import base64
import json
import stat
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from mr_lister.release.phase6 import render_manifest
from mr_lister.release.phase718 import PHASE718_CONTRACT_FINGERPRINT
from tools.prepare_phase718_web_release import (
    WEB_BUCKET,
    WEB_RELEASE_FORMAT,
    Phase718WebReleaseError,
    load_phase718_web_release_manifest,
    render_phase718_web_release_manifest,
    write_phase718_web_release_manifest,
)
from tools.verify_phase718_web_release import (
    LIVE_VERIFICATION_FORMAT,
    READBACK_OBSERVATION_FORMAT,
    ROLLBACK_MANIFEST_FORMAT,
    ROLLBACK_VERIFICATION_FORMAT,
    Phase718WebDeploymentEvidenceError,
    render_phase718_web_live_verification,
    render_phase718_web_rollback_manifest,
    render_phase718_web_rollback_verification,
    write_phase718_web_evidence,
)

SOURCE_COMMIT = "a" * 40
CSS_KEY = "assets/index-NewCss01.css"
JS_KEY = "assets/index-NewJs001.js"
OLD_CSS_KEY = "assets/index-OldCss01.css"
OLD_JS_KEY = "assets/index-OldJs001.js"
RUNTIME = b'{"client_id":"seller-client","scopes":["openid"]}\n'
DIST_FILES = {
    CSS_KEY: b"body{color:#123456}\n",
    JS_KEY: (
        b"7.1.0 /publication /publish data-phase7-publication-workspace "
        b"publish_exact_approved_listing\n"
    ),
    "favicon.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>\n',
    "index.html": (
        f'<!doctype html><link rel="icon" href="/favicon.svg">'
        f'<link rel="stylesheet" href="/{CSS_KEY}"><script src="/{JS_KEY}"></script>\n'
    ).encode(),
}
PREDECESSOR_FILES = {
    OLD_CSS_KEY: b"body{color:#654321}\n",
    OLD_JS_KEY: b"console.log('phase6 seller web');\n",
    "favicon.svg": DIST_FILES["favicon.svg"],
    "index.html": (
        f'<!doctype html><link rel="icon" href="/favicon.svg">'
        f'<link rel="stylesheet" href="/{OLD_CSS_KEY}">'
        f'<script src="/{OLD_JS_KEY}"></script>\n'
    ).encode(),
    "runtime-config.json": RUNTIME,
}


def test_prepare_manifest_binds_exact_bundle_runtime_and_enabled_release(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    raw = render_phase718_web_release_manifest(
        fixture["dist"],
        enabled_descriptor_path=fixture["descriptor"],
        runtime_config_upload_manifest_path=fixture["runtime_manifest"],
        source_commit=SOURCE_COMMIT,
        repository_root=fixture["repository"],
    )
    manifest = json.loads(raw)

    assert raw == render_manifest(manifest)
    assert manifest["format"] == WEB_RELEASE_FORMAT
    assert manifest["bucket"] == WEB_BUCKET
    assert manifest["contract"] == {
        "path": "contracts/publication/phase7.1.0.json",
        "sha256": PHASE718_CONTRACT_FINGERPRINT,
        "version": "7.1.0",
    }
    assert manifest["source_commit"] == SOURCE_COMMIT
    assert manifest["upload_order"] == [CSS_KEY, JS_KEY, "favicon.svg", "index.html"]
    assert manifest["upload_order"][-1] == "index.html"
    assert manifest["runtime_config"] == {
        "cache_control": "private, no-store, max-age=0",
        "content_type": "application/json",
        "manifest_sha256": sha256(fixture["runtime_manifest"].read_bytes()).hexdigest(),
        "object_key": "runtime-config.json",
        "preserve_existing": True,
        "sha256": sha256(RUNTIME).hexdigest(),
        "size_bytes": len(RUNTIME),
    }
    assert manifest["enabled_runtime"]["release_fingerprint"] == "f" * 64
    for record in manifest["objects"]:
        assert record["metadata"] == {
            "mr-lister-contract-version": "7.1.0",
            "mr-lister-enabled-release-fingerprint": "f" * 64,
            "mr-lister-web-bundle-sha256": manifest["browser_bundle_sha256"],
        }
        assert (
            record["checksum_sha256_base64"]
            == base64.b64encode(bytes.fromhex(record["sha256"])).decode()
        )


def test_release_manifest_is_create_only_private_and_revalidates_source(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    destination = Path(".mr_lister_private/phase718-web/release.json")

    written = write_phase718_web_release_manifest(
        fixture["dist"],
        destination,
        enabled_descriptor_path=fixture["descriptor"],
        runtime_config_upload_manifest_path=fixture["runtime_manifest"],
        source_commit=SOURCE_COMMIT,
        repository_root=fixture["repository"],
    )

    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    load_phase718_web_release_manifest(written, repository_root=fixture["repository"])
    with pytest.raises(Phase718WebReleaseError):
        write_phase718_web_release_manifest(
            fixture["dist"],
            destination,
            enabled_descriptor_path=fixture["descriptor"],
            runtime_config_upload_manifest_path=fixture["runtime_manifest"],
            source_commit=SOURCE_COMMIT,
            repository_root=fixture["repository"],
        )
    fixture["dist"].joinpath(JS_KEY).write_bytes(b"drift")
    with pytest.raises(Phase718WebReleaseError):
        load_phase718_web_release_manifest(written, repository_root=fixture["repository"])


def test_live_readback_and_rollback_bind_exact_object_versions(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    release_path, release = _release(fixture)
    predecessor_path = _observation(
        fixture,
        "predecessor",
        PREDECESSOR_FILES,
        versions={key: f"phase6-{index}" for index, key in enumerate(PREDECESSOR_FILES)},
        captured_at="2026-09-03T20:00:00Z",
    )
    rollback_raw = render_phase718_web_rollback_manifest(
        release_path,
        predecessor_path,
        repository_root=fixture["repository"],
    )
    rollback_path = _private_json(fixture, "rollback.json", rollback_raw)
    rollback = json.loads(rollback_raw)

    assert rollback["format"] == ROLLBACK_MANIFEST_FORMAT
    assert rollback["runtime_config"]["predecessor_version_id"].startswith("phase6-")
    assert {item["version_id"] for item in rollback["predecessor_objects"]} == {
        f"phase6-{index}" for index in range(5)
    }

    live_files = {
        **PREDECESSOR_FILES,
        **{key: value for key, value in DIST_FILES.items()},
    }
    predecessor_versions = {
        item["key"]: item["version_id"] for item in rollback["predecessor_objects"]
    }
    candidate_versions = {
        key: f"candidate-{index}" for index, key in enumerate(release["upload_order"])
    }
    live_versions = {**predecessor_versions, **candidate_versions}
    candidate_metadata = {item["key"]: item["metadata"] for item in release["objects"]}
    live_path = _observation(
        fixture,
        "live",
        live_files,
        versions=live_versions,
        captured_at="2026-09-03T20:05:00Z",
        metadata=candidate_metadata,
        put_results=[
            {"key": key, "version_id": candidate_versions[key]} for key in release["upload_order"]
        ],
    )
    live_raw = render_phase718_web_live_verification(
        release_path,
        rollback_path,
        live_path,
        repository_root=fixture["repository"],
    )
    live_evidence_path = _private_json(fixture, "live-evidence.json", live_raw)
    live_evidence = json.loads(live_raw)

    assert live_evidence["format"] == LIVE_VERIFICATION_FORMAT
    assert [item["key"] for item in live_evidence["candidate_versions_to_delete"]] == list(
        reversed(release["upload_order"])
    )
    assert live_evidence["runtime_config_version_id"] == predecessor_versions["runtime-config.json"]

    restored_path = _observation(
        fixture,
        "restored",
        PREDECESSOR_FILES,
        versions=predecessor_versions,
        captured_at="2026-09-03T20:10:00Z",
    )
    restored_raw = render_phase718_web_rollback_verification(
        release_path,
        rollback_path,
        live_evidence_path,
        restored_path,
        repository_root=fixture["repository"],
    )
    restored = json.loads(restored_raw)

    assert restored["format"] == ROLLBACK_VERIFICATION_FORMAT
    assert restored["verification"] == "exact_predecessor_versions_restored"
    assert restored["restored_predecessor_versions"] == [
        {"key": key, "version_id": predecessor_versions[key]}
        for key in sorted(predecessor_versions)
    ]


def test_live_verifier_rejects_out_of_order_upload_and_runtime_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    release_path, release = _release(fixture)
    predecessor_path = _observation(
        fixture,
        "predecessor",
        PREDECESSOR_FILES,
        versions={key: f"phase6-{index}" for index, key in enumerate(PREDECESSOR_FILES)},
        captured_at="2026-09-03T20:00:00Z",
    )
    rollback_path = _private_json(
        fixture,
        "rollback.json",
        render_phase718_web_rollback_manifest(
            release_path,
            predecessor_path,
            repository_root=fixture["repository"],
        ),
    )
    rollback = json.loads(rollback_path.read_bytes())
    predecessor_versions = {
        item["key"]: item["version_id"] for item in rollback["predecessor_objects"]
    }
    candidate_versions = {
        key: f"candidate-{index}" for index, key in enumerate(release["upload_order"])
    }
    live_files = {**PREDECESSOR_FILES, **DIST_FILES}
    metadata = {item["key"]: item["metadata"] for item in release["objects"]}
    reversed_puts = [
        {"key": key, "version_id": candidate_versions[key]}
        for key in reversed(release["upload_order"])
    ]
    wrong_order = _observation(
        fixture,
        "wrong-order",
        live_files,
        versions={**predecessor_versions, **candidate_versions},
        captured_at="2026-09-03T20:05:00Z",
        metadata=metadata,
        put_results=reversed_puts,
    )

    with pytest.raises(Phase718WebDeploymentEvidenceError):
        render_phase718_web_live_verification(
            release_path,
            rollback_path,
            wrong_order,
            repository_root=fixture["repository"],
        )

    drifted_versions = {
        **predecessor_versions,
        **candidate_versions,
        "runtime-config.json": "changed-runtime-version",
    }
    runtime_drift = _observation(
        fixture,
        "runtime-drift",
        live_files,
        versions=drifted_versions,
        captured_at="2026-09-03T20:05:00Z",
        metadata=metadata,
        put_results=[
            {"key": key, "version_id": candidate_versions[key]} for key in release["upload_order"]
        ],
    )
    with pytest.raises(Phase718WebDeploymentEvidenceError):
        render_phase718_web_live_verification(
            release_path,
            rollback_path,
            runtime_drift,
            repository_root=fixture["repository"],
        )


def test_rollback_verifier_rejects_any_predecessor_version_substitution(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    release_path, release = _release(fixture)
    predecessor_versions = {key: f"phase6-{index}" for index, key in enumerate(PREDECESSOR_FILES)}
    predecessor_path = _observation(
        fixture,
        "predecessor",
        PREDECESSOR_FILES,
        versions=predecessor_versions,
        captured_at="2026-09-03T20:00:00Z",
    )
    rollback_path = _private_json(
        fixture,
        "rollback.json",
        render_phase718_web_rollback_manifest(
            release_path,
            predecessor_path,
            repository_root=fixture["repository"],
        ),
    )
    candidate_versions = {
        key: f"candidate-{index}" for index, key in enumerate(release["upload_order"])
    }
    live_path = _observation(
        fixture,
        "live",
        {**PREDECESSOR_FILES, **DIST_FILES},
        versions={**predecessor_versions, **candidate_versions},
        captured_at="2026-09-03T20:05:00Z",
        metadata={item["key"]: item["metadata"] for item in release["objects"]},
        put_results=[
            {"key": key, "version_id": candidate_versions[key]} for key in release["upload_order"]
        ],
    )
    live_evidence_path = _private_json(
        fixture,
        "live-evidence.json",
        render_phase718_web_live_verification(
            release_path,
            rollback_path,
            live_path,
            repository_root=fixture["repository"],
        ),
    )
    wrong_versions = {**predecessor_versions, "index.html": "not-the-phase6-version"}
    wrong_rollback = _observation(
        fixture,
        "wrong-rollback",
        PREDECESSOR_FILES,
        versions=wrong_versions,
        captured_at="2026-09-03T20:10:00Z",
    )

    with pytest.raises(Phase718WebDeploymentEvidenceError):
        render_phase718_web_rollback_verification(
            release_path,
            rollback_path,
            live_evidence_path,
            wrong_rollback,
            repository_root=fixture["repository"],
        )


def test_private_evidence_writer_is_create_only(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    raw = render_manifest({"format": "test"})
    destination = Path(".mr_lister_private/phase718-web/evidence.json")

    written = write_phase718_web_evidence(
        raw,
        destination,
        repository_root=fixture["repository"],
    )

    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    with pytest.raises(Phase718WebDeploymentEvidenceError):
        write_phase718_web_evidence(
            raw,
            destination,
            repository_root=fixture["repository"],
        )


def _fixture(tmp_path: Path) -> dict[str, Any]:
    repository = tmp_path / "repository"
    dist = repository / "web" / "dist"
    for key, body in DIST_FILES.items():
        target = dist / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    descriptor = repository / ".mr_lister_private" / "enabled" / "descriptor.json"
    _write_canonical(descriptor, _enabled_descriptor())
    runtime_manifest = repository / ".mr_lister_private" / "runtime" / "runtime.upload.json"
    _write_compact(
        runtime_manifest,
        {
            "algorithm": "sha256",
            "cache_control": "private, no-store, max-age=0",
            "content_type": "application/json",
            "format": "mr-lister-phase6-runtime-config-upload-v1",
            "object_key": "runtime-config.json",
            "sha256": sha256(RUNTIME).hexdigest(),
            "size_bytes": len(RUNTIME),
        },
    )
    return {
        "descriptor": descriptor,
        "dist": dist,
        "repository": repository,
        "runtime_manifest": runtime_manifest,
    }


def _enabled_descriptor() -> dict[str, object]:
    return {
        "algorithm": "sha256",
        "application_release_fingerprint": "1" * 64,
        "archive": {"path": "enabled.zip", "sha256": "2" * 64, "size_bytes": 123},
        "architecture": "arm64",
        "binding_sha256": "3" * 64,
        "canary_evidence_fingerprint": "4" * 64,
        "component": "phase718-enabled-lambda",
        "contract_fingerprint": PHASE718_CONTRACT_FINGERPRINT,
        "deployment_manifest_sha256": "5" * 64,
        "enabled_template_sha256": "6" * 64,
        "enablement_evidence_fingerprint": "7" * 64,
        "entrypoints": ["one", "two"],
        "format": "phase718-enabled-deployment-descriptor-v1",
        "profile_fingerprint": "8" * 64,
        "release_fingerprint": "f" * 64,
        "runtime": "python3.12",
        "s3_binding": {"server_side_encryption": "AES256"},
        "source_manifest_sha256": "9" * 64,
        "state_table": "mr-lister-phase6-dev",
    }


def _release(fixture: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    raw = render_phase718_web_release_manifest(
        fixture["dist"],
        enabled_descriptor_path=fixture["descriptor"],
        runtime_config_upload_manifest_path=fixture["runtime_manifest"],
        source_commit=SOURCE_COMMIT,
        repository_root=fixture["repository"],
    )
    path = _private_json(fixture, "release.json", raw)
    return path, json.loads(raw)


def _observation(
    fixture: dict[str, Any],
    name: str,
    files: dict[str, bytes],
    *,
    versions: dict[str, str],
    captured_at: str,
    metadata: dict[str, dict[str, str]] | None = None,
    put_results: list[dict[str, str]] | None = None,
) -> Path:
    records: list[dict[str, object]] = []
    for key in sorted(files):
        body = files[key]
        body_path = Path(f".mr_lister_private/phase718-web/{name}/{key}")
        absolute = fixture["repository"] / body_path
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(body)
        digest = sha256(body).hexdigest()
        content_type, cache_control = _headers(key)
        records.append(
            {
                "body_path": body_path.as_posix(),
                "cache_control": cache_control,
                "checksum_sha256_base64": base64.b64encode(bytes.fromhex(digest)).decode(),
                "content_type": content_type,
                "etag": f'"{digest[:32]}"',
                "key": key,
                "metadata": (metadata or {}).get(key, {}),
                "server_side_encryption": "AES256",
                "sha256": digest,
                "size_bytes": len(body),
                "version_id": versions[key],
            }
        )
    path = fixture["repository"] / f".mr_lister_private/phase718-web/{name}.json"
    _write_canonical(
        path,
        {
            "bucket": WEB_BUCKET,
            "bucket_versioning": "Enabled",
            "captured_at": captured_at,
            "format": READBACK_OBSERVATION_FORMAT,
            "objects": records,
            "put_results": put_results or [],
        },
    )
    return path


def _private_json(fixture: dict[str, Any], name: str, raw: bytes) -> Path:
    path = fixture["repository"] / ".mr_lister_private" / "phase718-web" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _headers(key: str) -> tuple[str, str]:
    if key.endswith(".css"):
        return "text/css; charset=utf-8", "public, max-age=31536000, immutable"
    if key.endswith(".js"):
        return "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"
    if key == "favicon.svg":
        return "image/svg+xml", "private, no-store, max-age=0"
    if key == "index.html":
        return "text/html; charset=utf-8", "private, no-store, max-age=0"
    return "application/json", "private, no-store, max-age=0"


def _write_canonical(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_manifest(value))


def _write_compact(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8"
    )
