"""Build narrow, deterministic Phase 6 Lambda and AgentCore source bundles.

This step intentionally does not resolve platform wheels.  It creates auditable source inputs
for the later Linux ARM64 dependency build, writes content manifests, and excludes legacy API,
publication, tests, private evidence, caches, and developer files.
"""

from __future__ import annotations

import argparse
import json
import shutil
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = ROOT / ".mr_lister_private" / "phase6-release"

_COMMON_ROOT_FILES = ("__init__.py", "review_profile.py", "review_security.py")
_LAMBDA_AGENT_FILES = (
    "__init__.py",
    "contracts.py",
    "observability.py",
    "phase6_contracts.py",
)
_LAMBDA_CLOUD_FILES = (
    "__init__.py",
    "api.py",
    "artifacts.py",
    "auth.py",
    "browser_contracts.py",
    "http.py",
    "phase6_composition.py",
    "phase6_entrypoints.py",
    "phase6_machine.py",
    "phase6_machine_composition.py",
    "preview.py",
    "phase6_retention_composition.py",
    "phase6_retention_entrypoint.py",
)
_LAMBDA_PRODUCTION_FILES = (
    "__init__.py",
    "draft_sync.py",
    "economics.py",
    "phase6_worker.py",
    "printify.py",
    "printify_shipping.py",
    "provider_resources.py",
    "provider_secrets.py",
    "retention.py",
    "retention_aws.py",
    "settings.py",
)
_WORKFLOW_FILES = (
    "__init__.py",
    "errors.py",
    "models.py",
    "ports.py",
    "secrets.py",
    "validation.py",
)
_AGENTCORE_AGENT_FILES = (
    "__init__.py",
    "contracts.py",
    "observability.py",
    "phase6.py",
    "phase6_composition.py",
    "phase6_contracts.py",
    "phase6_producer.py",
    "runtime_contracts.py",
)

LAMBDA_REQUIREMENTS = """boto3>=1.43,<2
botocore[crt]>=1.43,<2
pillow>=11.3,<13
pydantic>=2.10,<3
"""
AGENTCORE_REQUIREMENTS = """bedrock-agentcore>=1.22,<2
boto3>=1.43,<2
botocore[crt]>=1.43,<2
fastapi>=0.116,<1
pillow>=11.3,<13
pydantic>=2.10,<3
strands-agents>=1.52,<2
uvicorn>=0.35,<1
"""


def build_source_bundles(destination: Path) -> tuple[Path, Path]:
    destination = destination.resolve(strict=False)
    if destination.name != "phase6-release" or destination.exists():
        raise ValueError("Phase 6 source destination must be a new phase6-release directory")
    destination.mkdir(mode=0o700, parents=True)
    lambda_root = destination / "lambda"
    agentcore_root = destination / "agentcore"
    lambda_root.mkdir(mode=0o700)
    agentcore_root.mkdir(mode=0o700)

    _copy_file(ROOT / "infra/phase6/lambda/phase6_lambda.py", lambda_root / "phase6_lambda.py")
    _copy_common(lambda_root)
    _copy_directory(ROOT / "src/mr_lister/contracts", lambda_root / "mr_lister/contracts")
    _copy_directory(ROOT / "src/mr_lister/control", lambda_root / "mr_lister/control")
    _copy_selected(
        ROOT / "src/mr_lister/agent",
        lambda_root / "mr_lister/agent",
        _LAMBDA_AGENT_FILES,
    )
    _copy_selected(
        ROOT / "src/mr_lister/cloud",
        lambda_root / "mr_lister/cloud",
        _LAMBDA_CLOUD_FILES,
    )
    _copy_selected(
        ROOT / "src/mr_lister/production",
        lambda_root / "mr_lister/production",
        _LAMBDA_PRODUCTION_FILES,
    )
    _copy_selected(
        ROOT / "src/mr_lister/workflow",
        lambda_root / "mr_lister/workflow",
        _WORKFLOW_FILES,
    )
    _copy_file(
        ROOT / "config/product_profiles/gildan_64000_swiftpod.json",
        lambda_root / "config/product_profiles/gildan_64000_swiftpod.json",
    )
    _write_text(lambda_root / "requirements.txt", LAMBDA_REQUIREMENTS)

    _copy_file(ROOT / "agentcore_phase6_runtime.py", agentcore_root / "main.py")
    _copy_common(agentcore_root)
    _copy_directory(ROOT / "src/mr_lister/contracts", agentcore_root / "mr_lister/contracts")
    _copy_directory(ROOT / "src/mr_lister/control", agentcore_root / "mr_lister/control")
    _copy_directory(
        ROOT / "src/mr_lister/intelligence",
        agentcore_root / "mr_lister/intelligence",
    )
    _copy_selected(
        ROOT / "src/mr_lister/agent",
        agentcore_root / "mr_lister/agent",
        _AGENTCORE_AGENT_FILES,
    )
    _copy_selected(
        ROOT / "src/mr_lister/workflow",
        agentcore_root / "mr_lister/workflow",
        _WORKFLOW_FILES,
    )
    _copy_file(
        ROOT / "config/product_profiles/gildan_64000_swiftpod.json",
        agentcore_root / "config/product_profiles/gildan_64000_swiftpod.json",
    )
    _copy_file(
        ROOT / "config/bedrock/google_gemma_3_27b_it.json",
        agentcore_root / "config/bedrock/google_gemma_3_27b_it.json",
    )
    _write_text(agentcore_root / "requirements.txt", AGENTCORE_REQUIREMENTS)

    for root in (lambda_root, agentcore_root):
        _assert_hygiene(root)
        _write_manifest(root)
        verify_source_bundle(root)
    return lambda_root, agentcore_root


def verify_source_bundle(root: Path) -> None:
    root = root.resolve(strict=True)
    manifest_path = root / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != {"algorithm", "files", "format"}:
        raise ValueError("Phase 6 source manifest is malformed")
    if manifest["algorithm"] != "sha256" or manifest["format"] != "phase6-source-v1":
        raise ValueError("Phase 6 source manifest authority is unsupported")
    expected = _manifest_files(root)
    if manifest["files"] != expected:
        raise ValueError("Phase 6 source manifest does not match bundle bytes")
    _assert_hygiene(root)


def _copy_common(destination: Path) -> None:
    for name in _COMMON_ROOT_FILES:
        _copy_file(ROOT / "src/mr_lister" / name, destination / "mr_lister" / name)


def _copy_selected(source: Path, destination: Path, names: tuple[str, ...]) -> None:
    for name in names:
        _copy_file(source / name, destination / name)


def _copy_directory(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        _copy_file(path, destination / path.relative_to(source))


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise ValueError("Phase 6 source input is unavailable")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)
    destination.chmod(0o644)


def _write_text(destination: Path, value: str) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(value, encoding="utf-8", newline="\n")
    destination.chmod(0o644)


def _manifest_files(root: Path) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "source-manifest.json":
            continue
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        files.append(
            {
                "path": relative,
                "sha256": sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    return files


def _write_manifest(root: Path) -> None:
    payload = {
        "algorithm": "sha256",
        "files": _manifest_files(root),
        "format": "phase6-source-v1",
    }
    _write_text(
        root / "source-manifest.json",
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
    )


def _assert_hygiene(root: Path) -> None:
    forbidden_parts = {
        ".DS_Store",
        ".env",
        ".git",
        ".mr_lister_private",
        "__pycache__",
        "api",
        "tests",
    }
    forbidden_suffixes = {".map", ".pem", ".key", ".zip"}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in forbidden_parts for part in relative.parts):
            raise ValueError("Phase 6 source bundle contains a forbidden path")
        if path.is_file() and path.suffix.casefold() in forbidden_suffixes:
            raise ValueError("Phase 6 source bundle contains a forbidden file")
    forbidden_files = {
        "mr_lister/production/adapter.py",
        "mr_lister/workflow/service.py",
        "mr_lister/durable/handlers.py",
    }
    if any((root / name).exists() for name in forbidden_files):
        raise ValueError("Phase 6 source bundle contains a legacy broad-capability module")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--verify", type=Path)
    arguments = parser.parse_args()
    if arguments.verify is not None:
        verify_source_bundle(arguments.verify)
        print(arguments.verify.resolve(strict=True))
        return
    lambda_root, agentcore_root = build_source_bundles(arguments.destination)
    print(lambda_root)
    print(agentcore_root)


if __name__ == "__main__":
    main()
