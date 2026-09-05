"""Credential-free Phase 6 seller-web release manifest tests."""

from __future__ import annotations

import json
import stat
from hashlib import sha256
from pathlib import Path

import pytest

import tools.prepare_phase6_web_release as web_release
from tools.prepare_phase6_web_release import (
    EXPECTED_WEB_RELEASE_FILES,
    MANIFEST_FORMAT,
    Phase6WebReleaseError,
    render_phase6_web_release_manifest,
    write_phase6_web_release_manifest,
)

_FIXTURE_FILES = {
    "assets/index-BiMmzyh5.css": b"body{color:#123456}\n",
    "assets/index-BUBtkush.js": b'console.log("seller web");\n',
    "favicon.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>\n',
    "index.html": b"<!doctype html><html><body></body></html>\n",
}


def _browser_bundle_sha256(files: dict[str, bytes]) -> str:
    digest = sha256()
    for relative, contents in sorted(files.items()):
        relative_bytes = relative.encode()
        digest.update(len(relative_bytes).to_bytes(4, "big"))
        digest.update(relative_bytes)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(sha256(contents).digest())
    return digest.hexdigest()


@pytest.fixture(autouse=True)
def _seal_fixture_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_release,
        "BROWSER_GATE_BUNDLE_SHA256",
        _browser_bundle_sha256(_FIXTURE_FILES),
    )


def _repository(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    repository = tmp_path / "repository"
    dist = repository / "web" / "dist"
    files = dict(_FIXTURE_FILES)
    for relative, contents in files.items():
        path = dist / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    return repository, dist, files


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def test_release_authority_is_the_final_phase6_browser_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    assert web_release.BROWSER_GATE_BUNDLE_SHA256 == (
        "b4763554cc8c99b25ba92662b3db04075d401d3ca8b0e7daa6cd86e1089530c0"
    )
    assert set(EXPECTED_WEB_RELEASE_FILES) == {
        "assets/index-BiMmzyh5.css",
        "assets/index-BUBtkush.js",
        "favicon.svg",
        "index.html",
    }


def test_manifest_is_canonical_closed_and_binds_upload_metadata(tmp_path: Path) -> None:
    repository, dist, contents = _repository(tmp_path)

    raw = render_phase6_web_release_manifest(dist, repository_root=repository)
    manifest = json.loads(raw)

    assert raw == _canonical(manifest)
    assert set(manifest) == {
        "aggregate_sha256",
        "algorithm",
        "browser_gate_bundle_sha256",
        "file_count",
        "files",
        "format",
    }
    assert manifest["algorithm"] == "sha256"
    assert manifest["browser_gate_bundle_sha256"] == _browser_bundle_sha256(contents)
    assert manifest["file_count"] == 4
    assert manifest["format"] == MANIFEST_FORMAT
    assert [record["path"] for record in manifest["files"]] == sorted(EXPECTED_WEB_RELEASE_FILES)
    for record in manifest["files"]:
        path = record["path"]
        content_type, cache_control = EXPECTED_WEB_RELEASE_FILES[path]
        assert record == {
            "cache_control": cache_control,
            "content_type": content_type,
            "path": path,
            "sha256": sha256(contents[path]).hexdigest(),
            "size_bytes": len(contents[path]),
        }
    authority = {key: value for key, value in manifest.items() if key != "aggregate_sha256"}
    assert manifest["aggregate_sha256"] == sha256(_canonical(authority)).hexdigest()


def test_private_manifest_is_create_only_and_mode_0600(tmp_path: Path) -> None:
    repository, dist, _contents = _repository(tmp_path)
    destination = Path(".mr_lister_private/phase6-web-release/release.json")

    written = write_phase6_web_release_manifest(
        dist,
        destination,
        repository_root=repository,
    )
    original = written.read_bytes()

    assert written == repository / destination
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    with pytest.raises(Phase6WebReleaseError, match="Phase 6 web release input is invalid"):
        write_phase6_web_release_manifest(
            dist,
            destination,
            repository_root=repository,
        )
    assert written.read_bytes() == original


@pytest.mark.parametrize(
    "relative",
    (
        "runtime-config.json",
        "runtime-config.example.json",
        "assets/index-BUBtkush.js.map",
        "robots.txt",
        "nested/extra.txt",
    ),
)
def test_unexpected_runtime_source_map_and_other_files_are_rejected(
    tmp_path: Path,
    relative: str,
) -> None:
    repository, dist, _contents = _repository(tmp_path)
    unexpected = dist / relative
    unexpected.parent.mkdir(parents=True, exist_ok=True)
    unexpected.write_text("not part of the sealed release", encoding="utf-8")

    with pytest.raises(Phase6WebReleaseError):
        render_phase6_web_release_manifest(dist, repository_root=repository)


def test_missing_empty_and_renamed_expected_files_are_rejected(tmp_path: Path) -> None:
    repository, dist, _contents = _repository(tmp_path)
    expected = dist / "index.html"

    expected.unlink()
    with pytest.raises(Phase6WebReleaseError):
        render_phase6_web_release_manifest(dist, repository_root=repository)

    expected.write_bytes(b"")
    with pytest.raises(Phase6WebReleaseError):
        render_phase6_web_release_manifest(dist, repository_root=repository)

    expected.write_bytes(b"valid again")
    expected.rename(dist / "index.htm")
    with pytest.raises(Phase6WebReleaseError):
        render_phase6_web_release_manifest(dist, repository_root=repository)


def test_symlinked_input_is_rejected_even_when_target_stays_in_repository(
    tmp_path: Path,
) -> None:
    repository, dist, _contents = _repository(tmp_path)
    target = repository / "replacement.js"
    target.write_bytes(b"replacement")
    expected = dist / "assets" / "index-BUBtkush.js"
    expected.unlink()
    expected.symlink_to(target)

    with pytest.raises(Phase6WebReleaseError):
        render_phase6_web_release_manifest(dist, repository_root=repository)


def test_input_and_output_cannot_escape_repository_private_boundary(tmp_path: Path) -> None:
    repository, dist, _contents = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(Phase6WebReleaseError):
        render_phase6_web_release_manifest(outside, repository_root=repository)
    with pytest.raises(Phase6WebReleaseError):
        write_phase6_web_release_manifest(
            dist,
            repository / "public-release.json",
            repository_root=repository,
        )


def test_unexpected_empty_directory_is_rejected(tmp_path: Path) -> None:
    repository, dist, _contents = _repository(tmp_path)
    (dist / "unexpected").mkdir()

    with pytest.raises(Phase6WebReleaseError):
        render_phase6_web_release_manifest(dist, repository_root=repository)
