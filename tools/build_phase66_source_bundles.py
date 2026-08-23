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

from mr_lister.release.phase6 import (
    DEPENDENCY_ARTIFACT_FILENAME,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    DEPLOYMENT_MANIFEST_FILENAME,
    LINUX_ARM64_TARGET,
    RELEASE_MANIFEST_FILENAME,
    inspect_linux_arm64_dependency_artifact,
    render_manifest,
    verify_dependency_build_request,
    verify_linux_arm64_dependency_artifact,
    verify_phase6_packaged_release,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = ROOT / ".mr_lister_private" / "phase6-release"
DEFAULT_DEPLOYMENT_DESTINATION = ROOT / ".mr_lister_private" / "phase6-deployment"

_COMMON_ROOT_FILES = ("__init__.py", "review_profile.py", "review_security.py")
_LAMBDA_AGENT_FILES = (
    "__init__.py",
    "contracts.py",
    "observability.py",
    "phase6_contracts.py",
    "runtime_binding.py",
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
    "phase6_execution_recovery_composition.py",
    "phase6_execution_recovery_entrypoint.py",
    "phase6_machine.py",
    "phase6_machine_composition.py",
    "phase6_operational_cleanup_composition.py",
    "phase6_operational_cleanup_entrypoint.py",
    "preview.py",
    "phase6_retention_composition.py",
    "phase6_retention_entrypoint.py",
)
_LAMBDA_PRODUCTION_FILES = (
    "__init__.py",
    "draft_sync.py",
    "economics.py",
    "operational_cleanup.py",
    "operational_cleanup_aws.py",
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

_REQUIRED_DISTRIBUTIONS = {
    "lambda": ["awscrt", "boto3", "botocore", "pillow", "pydantic"],
    "agentcore": [
        "awscrt",
        "bedrock-agentcore",
        "boto3",
        "botocore",
        "fastapi",
        "pillow",
        "pydantic",
        "strands-agents",
        "uvicorn",
    ],
}


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
    _copy_directory(ROOT / "src/mr_lister/release", lambda_root / "mr_lister/release")
    _write_dependency_build_request(lambda_root, component="lambda")

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
    _copy_directory(ROOT / "src/mr_lister/release", agentcore_root / "mr_lister/release")
    _write_dependency_build_request(agentcore_root, component="agentcore")

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
    try:
        verify_dependency_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
    except Exception:
        raise ValueError("Phase 6 dependency build request is invalid") from None
    _assert_hygiene(root)


def write_linux_arm64_dependency_manifest(
    artifact_root: Path,
    *,
    build_request_path: Path,
) -> Path:
    """Inspect and seal dependency bytes produced by a controlled Linux ARM64 build."""

    artifact_root = artifact_root.resolve(strict=True)
    manifest_path = artifact_root / DEPENDENCY_ARTIFACT_FILENAME
    if manifest_path.exists():
        raise ValueError("Phase 6 dependency artifact manifest already exists")
    manifest = inspect_linux_arm64_dependency_artifact(
        artifact_root,
        build_request_path=build_request_path,
    )
    _write_bytes(manifest_path, render_manifest(manifest))
    verify_linux_arm64_dependency_artifact(
        artifact_root,
        build_request_path=build_request_path,
    )
    return manifest_path


def seal_release_bundles(
    source_release: Path,
    *,
    lambda_dependencies: Path,
    agentcore_dependencies: Path,
    destination: Path,
) -> tuple[Path, Path, str]:
    """Overlay verified dependency trees and seal two runtime-verifiable deployments."""

    source_release = source_release.resolve(strict=True)
    destination = destination.resolve(strict=False)
    if source_release.name != "phase6-release":
        raise ValueError("Phase 6 source release directory is invalid")
    if destination.name != "phase6-deployment" or destination.exists():
        raise ValueError("Phase 6 deployment destination must be a new phase6-deployment directory")
    source_roots = {
        "lambda": source_release / "lambda",
        "agentcore": source_release / "agentcore",
    }
    dependency_roots = {
        "lambda": lambda_dependencies.resolve(strict=True),
        "agentcore": agentcore_dependencies.resolve(strict=True),
    }
    for component, source_root in source_roots.items():
        verify_source_bundle(source_root)
        verify_linux_arm64_dependency_artifact(
            dependency_roots[component],
            build_request_path=source_root / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )

    destination.mkdir(mode=0o700, parents=True)
    deployment_roots: dict[str, Path] = {}
    component_records: dict[str, dict[str, str]] = {}
    for component in ("agentcore", "lambda"):
        deployed = destination / component
        deployed.mkdir(mode=0o700)
        _copy_tree(source_roots[component], deployed)
        _overlay_dependency_tree(dependency_roots[component], deployed)
        deployment = {
            "algorithm": "sha256",
            "component": component,
            "files": _deployment_files(deployed),
            "format": "phase6-deployment-v1",
            "target": dict(LINUX_ARM64_TARGET),
        }
        deployment_path = deployed / DEPLOYMENT_MANIFEST_FILENAME
        _write_bytes(deployment_path, render_manifest(deployment))
        deployment_roots[component] = deployed
        component_records[component] = {
            "dependency_manifest_sha256": _file_fingerprint(
                deployed / DEPENDENCY_ARTIFACT_FILENAME
            ),
            "deployment_manifest_sha256": _file_fingerprint(deployment_path),
            "source_manifest_sha256": _file_fingerprint(deployed / "source-manifest.json"),
        }

    release = {
        "algorithm": "sha256",
        "components": component_records,
        "format": "phase6-release-v1",
        "target": dict(LINUX_ARM64_TARGET),
    }
    release_bytes = render_manifest(release)
    release_fingerprint = sha256(release_bytes).hexdigest()
    for component, deployed in deployment_roots.items():
        _write_bytes(deployed / RELEASE_MANIFEST_FILENAME, release_bytes)
        verify_phase6_packaged_release(
            {"MR_LISTER_RELEASE_FINGERPRINT": release_fingerprint},
            component=component,  # type: ignore[arg-type]
            bundle_root=deployed,
        )
    return deployment_roots["lambda"], deployment_roots["agentcore"], release_fingerprint


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


def _write_bytes(destination: Path, value: bytes) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(value)
    destination.chmod(0o644)


def _write_dependency_build_request(root: Path, *, component: str) -> None:
    requirements = root / "requirements.txt"
    payload = {
        "algorithm": "sha256",
        "component": component,
        "format": "phase6-dependency-build-request-v1",
        "requirements": {
            "path": "requirements.txt",
            "required_distributions": _REQUIRED_DISTRIBUTIONS[component],
            "sha256": _file_fingerprint(requirements),
        },
        "target": dict(LINUX_ARM64_TARGET),
    }
    _write_bytes(root / DEPENDENCY_BUILD_REQUEST_FILENAME, render_manifest(payload))


def _copy_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Phase 6 release input contains a symlink")
        if path.is_file():
            _copy_file(path, destination / path.relative_to(source))


def _overlay_dependency_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Phase 6 dependency artifact contains a symlink")
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        if target.exists():
            raise ValueError("Phase 6 dependency artifact collides with application source")
        _copy_file(path, target)


def _deployment_files(root: Path) -> list[dict[str, object]]:
    return _manifest_files(
        root,
        excluded_names=frozenset({DEPLOYMENT_MANIFEST_FILENAME, RELEASE_MANIFEST_FILENAME}),
    )


def _file_fingerprint(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Phase 6 release input is unavailable")
    return sha256(path.read_bytes()).hexdigest()


def _manifest_files(
    root: Path,
    *,
    excluded_names: frozenset[str] = frozenset({"source-manifest.json"}),
) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded_names:
            continue
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
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--verify", type=Path)
    actions.add_argument("--write-dependency-manifest", type=Path)
    actions.add_argument("--verify-dependency-artifact", type=Path)
    actions.add_argument("--seal-source-release", type=Path)
    parser.add_argument("--build-request", type=Path)
    parser.add_argument("--lambda-dependencies", type=Path)
    parser.add_argument("--agentcore-dependencies", type=Path)
    parser.add_argument(
        "--deployment-destination",
        type=Path,
        default=DEFAULT_DEPLOYMENT_DESTINATION,
    )
    arguments = parser.parse_args()
    if arguments.verify is not None:
        verify_source_bundle(arguments.verify)
        print(arguments.verify.resolve(strict=True))
        return
    if arguments.write_dependency_manifest is not None:
        if arguments.build_request is None:
            parser.error("--write-dependency-manifest requires --build-request")
        manifest = write_linux_arm64_dependency_manifest(
            arguments.write_dependency_manifest,
            build_request_path=arguments.build_request,
        )
        print(manifest)
        return
    if arguments.verify_dependency_artifact is not None:
        if arguments.build_request is None:
            parser.error("--verify-dependency-artifact requires --build-request")
        verify_linux_arm64_dependency_artifact(
            arguments.verify_dependency_artifact,
            build_request_path=arguments.build_request,
        )
        print(arguments.verify_dependency_artifact.resolve(strict=True))
        return
    if arguments.seal_source_release is not None:
        if arguments.lambda_dependencies is None or arguments.agentcore_dependencies is None:
            parser.error(
                "--seal-source-release requires --lambda-dependencies and --agentcore-dependencies"
            )
        lambda_root, agentcore_root, fingerprint = seal_release_bundles(
            arguments.seal_source_release,
            lambda_dependencies=arguments.lambda_dependencies,
            agentcore_dependencies=arguments.agentcore_dependencies,
            destination=arguments.deployment_destination,
        )
        print(lambda_root)
        print(agentcore_root)
        print(fingerprint)
        return
    if any(
        value is not None
        for value in (
            arguments.build_request,
            arguments.lambda_dependencies,
            arguments.agentcore_dependencies,
        )
    ):
        parser.error("dependency options require an explicit dependency or seal action")
    lambda_root, agentcore_root = build_source_bundles(arguments.destination)
    print(lambda_root)
    print(agentcore_root)


if __name__ == "__main__":
    main()
