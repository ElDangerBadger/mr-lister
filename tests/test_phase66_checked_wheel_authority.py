from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import tools.build_phase66_source_bundles as phase66_builder
from mr_lister.release.phase6 import (
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    LOCKED_BUILD_REQUEST_FORMAT,
    render_manifest,
    wheel_authority_from_build_request,
)
from tools.build_phase66_source_bundles import (
    build_source_bundles,
    capture_wheelhouse_authority_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_DIRECTORY = ROOT / "config/release/phase6"


def _checked_path(component: str) -> Path:
    return AUTHORITY_DIRECTORY / f"phase6-{component}-wheel-authority.json"


@pytest.mark.parametrize("component", ["lambda", "agentcore"])
def test_checked_authority_is_canonical_and_matches_private_review_capture(
    component: str,
) -> None:
    checked = _checked_path(component).read_bytes()
    assert render_manifest(json.loads(checked)) == checked
    private_capture = ROOT / f".mr_lister_private/phase6-{component}-wheel-authority.json"
    if private_capture.is_file():
        assert private_capture.read_bytes() == checked


@pytest.mark.parametrize("component", ["lambda", "agentcore"])
def test_real_private_wheelhouse_recaptures_byte_exact_checked_authority(
    tmp_path: Path,
    component: str,
) -> None:
    wheelhouse = ROOT / f".mr_lister_private/phase6-{component}-wheelhouse"
    if not wheelhouse.is_dir():
        pytest.skip("private review wheelhouse is intentionally not committed")
    output = tmp_path / f"phase6-{component}-wheel-authority.json"

    capture_wheelhouse_authority_candidate(
        wheelhouse,
        component=component,  # type: ignore[arg-type]
        output_path=output,
    )

    assert output.read_bytes() == _checked_path(component).read_bytes()


def test_default_source_build_embeds_both_checked_v2_authorities(tmp_path: Path) -> None:
    lambda_root, agentcore_root = build_source_bundles(tmp_path / "phase6-release")
    for component, root in (("lambda", lambda_root), ("agentcore", agentcore_root)):
        request_path = root / DEPENDENCY_BUILD_REQUEST_FILENAME
        request = json.loads(request_path.read_text(encoding="utf-8"))
        assert request["format"] == LOCKED_BUILD_REQUEST_FORMAT
        embedded = wheel_authority_from_build_request(request_path)
        assert embedded == json.loads(_checked_path(component).read_text(encoding="utf-8"))


def test_checked_authority_rejects_current_root_requirement_version_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = phase66_builder._COMPONENT_ROOT_REQUIREMENTS["lambda"]
    monkeypatch.setitem(
        phase66_builder._COMPONENT_ROOT_REQUIREMENTS,
        "lambda",
        ("boto3>=2", *current[1:]),
    )

    with pytest.raises(ValueError, match="checked wheel authority violates root requirements"):
        build_source_bundles(tmp_path / "phase6-release")

    assert not (tmp_path / "phase6-release").exists()


def test_packaging_parser_is_an_exact_declared_build_tool_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "packaging==26.3" in project["project"]["optional-dependencies"]["dev"]
