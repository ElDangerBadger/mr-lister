"""Build a narrow, private CodeZip source bundle for the Phase 3 AgentCore canary."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = ROOT / ".mr_lister_private" / "agentcore-bundle"
PYPROJECT = """[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "mr-lister-agentcore-canary"
version = "0.1.0"
requires-python = ">=3.12,<3.14"
dependencies = [
    "bedrock-agentcore>=1.22,<2",
    "boto3>=1.43,<2",
    "botocore[crt]>=1.43,<2",
    "fastapi>=0.116,<1",
    "pillow>=11.3,<13",
    "pydantic>=2.10,<3",
    "python-multipart>=0.0.20,<1",
    "strands-agents>=1.52,<2",
    "uvicorn>=0.35,<1",
]

[tool.hatch.build.targets.wheel]
packages = ["mr_lister"]
"""


def build_bundle(destination: Path) -> Path:
    if destination.name != "agentcore-bundle":
        raise ValueError("AgentCore bundle destination must end in agentcore-bundle")

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(mode=0o700, parents=True)
    shutil.copytree(ROOT / "src" / "mr_lister", destination / "mr_lister")
    shutil.copytree(
        ROOT / "config" / "product_profiles",
        destination / "config" / "product_profiles",
    )
    shutil.copy2(ROOT / "agentcore_runtime.py", destination / "main.py")
    (destination / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")

    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    destination = build_bundle(DEFAULT_DESTINATION)
    print(destination)


if __name__ == "__main__":
    main()
