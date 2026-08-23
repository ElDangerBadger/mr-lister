from __future__ import annotations

import json
from pathlib import Path

import pytest

from mr_lister.release.phase6 import (
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    LINUX_ARM64_TARGET,
    Phase6ReleaseAuthorityError,
    verify_dependency_build_request,
    verify_linux_arm64_dependency_artifact,
    verify_phase6_packaged_release,
)
from tools.build_phase66_source_bundles import (
    build_source_bundles,
    seal_release_bundles,
    write_linux_arm64_dependency_manifest,
)


def _source_destination(tmp_path: Path, name: str) -> Path:
    return tmp_path / name / "phase6-release"


def _deployment_destination(tmp_path: Path, name: str) -> Path:
    return tmp_path / name / "phase6-deployment"


def _fake_dependencies(tmp_path: Path, request_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    request = verify_dependency_build_request(request_path)
    requirements = request["requirements"]
    assert isinstance(requirements, dict)
    distributions = set(requirements["required_distributions"])
    distributions.add("pydantic-core")
    for distribution in sorted(distributions):
        assert isinstance(distribution, str)
        import_name = distribution.replace("-", "_")
        package = root / import_name
        package.mkdir()
        (package / "__init__.py").write_text("VERSION = '1.0'\n", encoding="utf-8")
        dist_info = root / f"{import_name}-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: 1.0\n\n",
            encoding="utf-8",
        )
        (dist_info / "WHEEL").write_text(
            "Wheel-Version: 1.0\nGenerator: phase66-test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n\n",
            encoding="utf-8",
        )
    native_paths = {
        "awscrt": root / "_awscrt.abi3.so",
        "pillow": root / "PIL/_imaging.cpython-312-aarch64-linux-gnu.so",
        "pydantic-core": (root / "pydantic_core/_pydantic_core.cpython-312-aarch64-linux-gnu.so"),
    }
    for distribution, native_path in native_paths.items():
        dist_info = root / f"{distribution.replace('-', '_')}-1.0.dist-info"
        (dist_info / "WHEEL").write_text(
            "Wheel-Version: 1.0\nGenerator: phase66-test\nRoot-Is-Purelib: false\n"
            "Tag: cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64\n\n",
            encoding="utf-8",
        )
        native_path.parent.mkdir(parents=True, exist_ok=True)
        native_path.write_bytes(_synthetic_arm64_elf())
    write_linux_arm64_dependency_manifest(root, build_request_path=request_path)
    return root


def _synthetic_arm64_elf() -> bytes:
    """Return test-only framing that exercises the static target inspector."""

    value = bytearray(4_096)
    value[:7] = b"\x7fELF\x02\x01\x01"
    value[16:18] = (3).to_bytes(2, "little")
    value[18:20] = (183).to_bytes(2, "little")
    value[20:24] = (1).to_bytes(4, "little")
    value[52:54] = (64).to_bytes(2, "little")
    return bytes(value)


def _sealed_release(
    tmp_path: Path,
    name: str,
) -> tuple[Path, Path, str, Path, Path, Path]:
    source = _source_destination(tmp_path, f"{name}-source")
    lambda_source, agentcore_source = build_source_bundles(source)
    lambda_dependencies = _fake_dependencies(
        tmp_path,
        lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        f"{name}-lambda-dependencies",
    )
    agentcore_dependencies = _fake_dependencies(
        tmp_path,
        agentcore_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        f"{name}-agentcore-dependencies",
    )
    lambda_root, agentcore_root, fingerprint = seal_release_bundles(
        source,
        lambda_dependencies=lambda_dependencies,
        agentcore_dependencies=agentcore_dependencies,
        destination=_deployment_destination(tmp_path, name),
    )
    return (
        lambda_root,
        agentcore_root,
        fingerprint,
        source,
        lambda_dependencies,
        agentcore_dependencies,
    )


def test_source_stage_requests_exact_linux_arm64_artifacts_without_claiming_build(
    tmp_path: Path,
) -> None:
    # Source construction creates a build request, not a dependency artifact or deployment seal.
    # That distinction prevents a macOS test run from being mislabeled as deployable Linux bytes.
    lambda_root, agentcore_root = build_source_bundles(_source_destination(tmp_path, "source"))

    for component, root in (("lambda", lambda_root), ("agentcore", agentcore_root)):
        request = verify_dependency_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
        assert request["component"] == component
        assert request["target"] == LINUX_ARM64_TARGET
        assert not (root / "dependency-artifact.json").exists()
        assert not (root / "deployment-manifest.json").exists()
        assert not (root / "release-manifest.json").exists()


def test_sealed_release_is_reproducible_and_fingerprint_binds_both_components(
    tmp_path: Path,
) -> None:
    first_lambda, first_agentcore, first_fingerprint, *_rest = _sealed_release(tmp_path, "first")
    second_lambda, second_agentcore, second_fingerprint, *_rest_two = _sealed_release(
        tmp_path, "second"
    )

    assert first_fingerprint == second_fingerprint
    assert (first_lambda / "release-manifest.json").read_bytes() == (
        second_lambda / "release-manifest.json"
    ).read_bytes()
    assert (first_agentcore / "deployment-manifest.json").read_bytes() == (
        second_agentcore / "deployment-manifest.json"
    ).read_bytes()
    lambda_binding = verify_phase6_packaged_release(
        {"MR_LISTER_RELEASE_FINGERPRINT": first_fingerprint},
        component="lambda",
        bundle_root=first_lambda,
    )
    agentcore_binding = verify_phase6_packaged_release(
        {"MR_LISTER_RELEASE_FINGERPRINT": first_fingerprint},
        component="agentcore",
        bundle_root=first_agentcore,
    )
    assert lambda_binding.release_fingerprint == agentcore_binding.release_fingerprint
    assert lambda_binding.deployment_manifest_fingerprint != (
        agentcore_binding.deployment_manifest_fingerprint
    )


def test_release_fingerprint_or_any_deployed_byte_drift_fails_closed(tmp_path: Path) -> None:
    lambda_root, _agentcore_root, fingerprint, *_rest = _sealed_release(tmp_path, "drift")

    with pytest.raises(Phase6ReleaseAuthorityError):
        verify_phase6_packaged_release(
            {"MR_LISTER_RELEASE_FINGERPRINT": "f" * 64},
            component="lambda",
            bundle_root=lambda_root,
        )

    source = lambda_root / "phase6_lambda.py"
    source.write_text(source.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    with pytest.raises(Phase6ReleaseAuthorityError):
        verify_phase6_packaged_release(
            {"MR_LISTER_RELEASE_FINGERPRINT": fingerprint},
            component="lambda",
            bundle_root=lambda_root,
        )


def test_dependency_manifest_tamper_and_non_arm64_wheel_metadata_are_rejected(
    tmp_path: Path,
) -> None:
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, "dependency-source")
    )
    request_path = lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME
    dependencies = _fake_dependencies(tmp_path, request_path, "tampered-dependencies")
    target = dependencies / "boto3" / "__init__.py"
    target.write_text("VERSION = 'attacker'\n", encoding="utf-8")
    with pytest.raises(Phase6ReleaseAuthorityError):
        verify_linux_arm64_dependency_artifact(
            dependencies,
            build_request_path=request_path,
        )

    wrong_platform = _fake_dependencies(tmp_path, request_path, "darwin-dependencies")
    wheel = wrong_platform / "boto3-1.0.dist-info" / "WHEEL"
    wheel.write_text(
        "Wheel-Version: 1.0\nGenerator: phase66-test\nRoot-Is-Purelib: false\n"
        "Tag: cp312-cp312-macosx_14_0_arm64\n\n",
        encoding="utf-8",
    )
    (wrong_platform / "dependency-artifact.json").unlink()
    with pytest.raises(Phase6ReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            wrong_platform,
            build_request_path=request_path,
        )


def test_native_extension_must_be_aarch64_elf_even_with_arm64_wheel_tag(tmp_path: Path) -> None:
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, "native-source")
    )
    request_path = lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME
    dependencies = _fake_dependencies(tmp_path, request_path, "native-dependencies")
    wheel = dependencies / "pillow-1.0.dist-info" / "WHEEL"
    wheel.write_text(
        "Wheel-Version: 1.0\nGenerator: phase66-test\nRoot-Is-Purelib: false\n"
        "Tag: cp312-cp312-manylinux2014_aarch64\n\n",
        encoding="utf-8",
    )
    # Valid ELF framing but e_machine=62 (x86-64), not 183 (AArch64).
    wrong_architecture = bytearray(_synthetic_arm64_elf())
    wrong_architecture[18:20] = (62).to_bytes(2, "little")
    (dependencies / "pillow" / "native.so").write_bytes(wrong_architecture)
    (dependencies / "dependency-artifact.json").unlink()

    with pytest.raises(Phase6ReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            dependencies,
            build_request_path=request_path,
        )

    arm_dependencies = _fake_dependencies(tmp_path, request_path, "arm64-dependencies")
    arm_wheel = arm_dependencies / "pillow-1.0.dist-info" / "WHEEL"
    arm_wheel.write_text(
        "Wheel-Version: 1.0\nGenerator: phase66-test\nRoot-Is-Purelib: false\n"
        "Tag: cp312-cp312-manylinux2014_aarch64\n\n",
        encoding="utf-8",
    )
    (arm_dependencies / "pillow" / "native.so").write_bytes(_synthetic_arm64_elf())
    (arm_dependencies / "dependency-artifact.json").unlink()

    manifest = write_linux_arm64_dependency_manifest(
        arm_dependencies,
        build_request_path=request_path,
    )
    assert manifest.is_file()


@pytest.mark.parametrize(
    "relative",
    [
        "_awscrt.abi3.so",
        "PIL/_imaging.cpython-312-aarch64-linux-gnu.so",
        "pydantic_core/_pydantic_core.cpython-312-aarch64-linux-gnu.so",
    ],
)
def test_required_native_runtime_extension_cannot_be_omitted(
    tmp_path: Path,
    relative: str,
) -> None:
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, f"native-required-{Path(relative).name}")
    )
    request_path = lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME
    dependencies = _fake_dependencies(
        tmp_path,
        request_path,
        f"native-required-dependencies-{Path(relative).name}",
    )
    (dependencies / relative).unlink()
    (dependencies / "dependency-artifact.json").unlink()

    with pytest.raises(Phase6ReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            dependencies,
            build_request_path=request_path,
        )


def test_release_manifest_is_canonical_and_contains_no_host_paths(tmp_path: Path) -> None:
    lambda_root, _agentcore_root, _fingerprint, *_rest = _sealed_release(tmp_path, "canonical")
    raw = (lambda_root / "release-manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)

    assert raw.endswith("\n")
    assert str(tmp_path) not in raw
    assert manifest["target"] == LINUX_ARM64_TARGET
    assert set(manifest["components"]) == {"agentcore", "lambda"}
