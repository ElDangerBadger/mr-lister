from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SDIST_INPUTS = ("src", "README.md", "LICENSE", "pyproject.toml")


def test_sdist_has_an_explicit_public_source_traversal_boundary() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    sdist = project["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert tuple(sdist["only-include"]) == PUBLIC_SDIST_INPUTS
    assert all((ROOT / relative).exists() for relative in PUBLIC_SDIST_INPUTS)


def test_wheel_remains_limited_to_the_application_package() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel == {"packages": ["src/mr_lister"]}
