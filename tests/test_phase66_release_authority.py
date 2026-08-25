from __future__ import annotations

import base64
import csv
import json
import shutil
import zipfile
from hashlib import sha256
from importlib.machinery import PathFinder
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from mr_lister.release.phase6 import (
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    LINUX_ARM64_TARGET,
    LOCKED_BUILD_REQUEST_FORMAT,
    WHEEL_AUTHORITY_FORMAT,
    Phase6ReleaseAuthorityError,
    render_manifest,
    verify_dependency_build_request,
    verify_linux_arm64_dependency_artifact,
    verify_phase6_packaged_release,
)
from tools.build_phase66_source_bundles import (
    build_linux_arm64_dependencies_from_wheelhouse,
    build_source_bundles,
    capture_wheelhouse_authority_candidate,
    seal_release_bundles,
    verify_phase6_deployment_artifacts,
    verify_source_bundle,
    write_linux_arm64_dependency_manifest,
)

_TEST_VERSIONS = {
    "bedrock-agentcore": "1.22",
    "boto3": "1.43",
    "botocore": "1.43",
    "fastapi": "0.116",
    "pillow": "11.3",
    "pydantic": "2.10",
    "strands-agents": "1.52",
    "uvicorn": "0.35",
}


def _test_version(distribution: str) -> str:
    return _TEST_VERSIONS.get(distribution, "1.0")


def _test_dist_info(distribution: str) -> str:
    return f"{distribution.replace('-', '_')}-{_test_version(distribution)}.dist-info"


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


def _locked_dependencies_and_authority(
    tmp_path: Path,
    *,
    component: str,
    name: str,
) -> tuple[Path, dict[str, object]]:
    required = {
        "lambda": {"awscrt", "boto3", "botocore", "pillow", "pydantic", "pydantic-core"},
        "agentcore": {
            "awscrt",
            "bedrock-agentcore",
            "boto3",
            "botocore",
            "fastapi",
            "pillow",
            "pydantic",
            "pydantic-core",
            "strands-agents",
            "uvicorn",
        },
    }[component]
    root = tmp_path / name
    root.mkdir()
    owned: dict[str, list[Path]] = {}
    dist_infos: dict[str, Path] = {}
    for distribution in sorted(required):
        version = _test_version(distribution)
        package_name = {
            "pillow": "PIL",
            "pydantic-core": "pydantic_core",
        }.get(distribution, distribution.replace("-", "_"))
        package = root / package_name
        package.mkdir()
        initializer = package / "__init__.py"
        initializer.write_text("VERSION = '1.0'\n", encoding="utf-8")
        dist_info = root / _test_dist_info(distribution)
        dist_info.mkdir()
        metadata = dist_info / "METADATA"
        extra_metadata = "Provides-Extra: crt\n" if distribution == "botocore" else ""
        metadata.write_text(
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n{extra_metadata}\n",
            encoding="utf-8",
        )
        wheel = dist_info / "WHEEL"
        native_distribution = distribution in {"awscrt", "pillow", "pydantic-core"}
        tag = (
            "cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64"
            if native_distribution
            else "py3-none-any"
        )
        wheel.write_text(
            "Wheel-Version: 1.0\nGenerator: phase66-test\n"
            f"Root-Is-Purelib: {'false' if native_distribution else 'true'}\n"
            f"Tag: {tag}\n\n",
            encoding="utf-8",
        )
        owned[distribution] = [initializer, metadata, wheel]
        dist_infos[distribution] = dist_info
    native_paths = {
        "awscrt": root / "_awscrt.abi3.so",
        "pillow": root / "PIL/_imaging.cpython-312-aarch64-linux-gnu.so",
        "pydantic-core": root / "pydantic_core/_pydantic_core.cpython-312-aarch64-linux-gnu.so",
    }
    for distribution, native in native_paths.items():
        native.write_bytes(_synthetic_arm64_elf())
        owned[distribution].append(native)
    for distribution in sorted(required):
        record = dist_infos[distribution] / "RECORD"
        rows: list[list[str]] = []
        for path in sorted(owned[distribution]):
            raw = path.read_bytes()
            encoded = base64.urlsafe_b64encode(sha256(raw).digest()).decode().rstrip("=")
            rows.append([path.relative_to(root).as_posix(), f"sha256={encoded}", str(len(raw))])
        rows.append([record.relative_to(root).as_posix(), "", ""])
        output = StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        record.write_text(output.getvalue(), encoding="utf-8")
    files = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_file():
            raw = path.read_bytes()
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                }
            )
    authority = {
        "algorithm": "sha256",
        "component": component,
        "dependency_tree_sha256": sha256(render_manifest({"files": files})).hexdigest(),
        "format": WHEEL_AUTHORITY_FORMAT,
        "target": dict(LINUX_ARM64_TARGET),
        "wheels": [
            {
                "filename": (
                    f"{distribution.replace('-', '_')}-{_test_version(distribution)}"
                    "-py3-none-any.whl"
                ),
                "name": distribution,
                "sha256": sha256(f"{component}:{distribution}".encode()).hexdigest(),
                "version": _test_version(distribution),
            }
            for distribution in sorted(required)
        ],
    }
    return root, authority


def _locked_wheelhouse_fixture(
    tmp_path: Path,
    *,
    component: str,
    name: str,
    extra_record_owned_file: tuple[str, str, bytes] | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    seed, authority = _locked_dependencies_and_authority(
        tmp_path,
        component=component,
        name=f"{name}-seed",
    )
    if extra_record_owned_file is not None:
        distribution, relative, content = extra_record_owned_file
        target = seed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        encoded = base64.urlsafe_b64encode(sha256(content).digest()).decode().rstrip("=")
        record = seed / _test_dist_info(distribution) / "RECORD"
        rows = list(csv.reader(StringIO(record.read_text(encoding="utf-8"), newline="")))
        rows.insert(-1, [relative, f"sha256={encoded}", str(len(content))])
        output = StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        record.write_text(output.getvalue(), encoding="utf-8")
        _refresh_tree_authority(seed, authority)
    wheelhouse = tmp_path / f"{name}-wheelhouse"
    wheelhouse.mkdir()
    wheels = authority["wheels"]
    assert isinstance(wheels, list)
    for wheel in wheels:
        distribution = wheel["name"]
        dist_info = _test_dist_info(distribution)
        rows = csv.reader(
            StringIO((seed / dist_info / "RECORD").read_text(encoding="utf-8"), newline="")
        )
        archive_path = wheelhouse / wheel["filename"]
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, _encoded_hash, _size in rows:
                archive.write(seed / relative, arcname=relative)
        wheel["sha256"] = sha256(archive_path.read_bytes()).hexdigest()
    return seed, wheelhouse, authority


def _tree_files(root: Path, *, excluded: frozenset[str] = frozenset()) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and relative not in excluded:
            raw = path.read_bytes()
            files.append(
                {
                    "path": relative,
                    "sha256": sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                }
            )
    return files


def _refresh_tree_authority(root: Path, authority: dict[str, object]) -> None:
    authority["dependency_tree_sha256"] = sha256(
        render_manifest({"files": _tree_files(root)})
    ).hexdigest()


def _rewrite_owned_dependency_file(
    root: Path,
    authority: dict[str, object],
    *,
    distribution: str,
    filename: str,
    content: bytes,
) -> None:
    dist_info = _test_dist_info(distribution)
    relative = f"{dist_info}/{filename}"
    target = root / relative
    target.write_bytes(content)
    encoded = base64.urlsafe_b64encode(sha256(content).digest()).decode().rstrip("=")
    record = root / dist_info / "RECORD"
    rows = list(csv.reader(StringIO(record.read_text(encoding="utf-8"), newline="")))
    for row in rows:
        if row[0] == relative:
            row[1:] = [f"sha256={encoded}", str(len(content))]
            break
    else:  # pragma: no cover - the fixture owns its metadata
        raise AssertionError("fixture ownership record is missing")
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record.write_text(output.getvalue(), encoding="utf-8")
    _refresh_tree_authority(root, authority)


def _rewrite_wheel_metadata(
    wheel: Path,
    *,
    distribution: str,
    appended_headers: str,
) -> None:
    dist_info = _test_dist_info(distribution)
    metadata_name = f"{dist_info}/METADATA"
    record_name = f"{dist_info}/RECORD"
    with zipfile.ZipFile(wheel) as source:
        members = source.infolist()
        content = {member.filename: source.read(member) for member in members}
    metadata = content[metadata_name]
    if not metadata.endswith(b"\n\n"):
        raise AssertionError("fixture metadata is not canonical")
    content[metadata_name] = metadata[:-1] + appended_headers.encode("ascii") + b"\n"
    rows = list(csv.reader(StringIO(content[record_name].decode("utf-8"), newline="")))
    raw_metadata = content[metadata_name]
    encoded = base64.urlsafe_b64encode(sha256(raw_metadata).digest()).decode().rstrip("=")
    for row in rows:
        if row[0] == metadata_name:
            row[1:] = [f"sha256={encoded}", str(len(raw_metadata))]
            break
    else:  # pragma: no cover - the fixture always owns METADATA
        raise AssertionError("fixture METADATA record is missing")
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    content[record_name] = output.getvalue().encode("utf-8")
    archive_bytes = BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            archive.writestr(member, content[member.filename])
    wheel.write_bytes(archive_bytes.getvalue())


def _append_wheel_member(wheel: Path, relative: str) -> None:
    with zipfile.ZipFile(wheel, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(relative, b"case-fold collision\n")


def _sealed_release(
    tmp_path: Path,
    name: str,
) -> tuple[Path, Path, str, Path, Path, Path]:
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name=f"{name}-lambda-dependencies",
    )
    agentcore_dependencies, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name=f"{name}-agentcore-dependencies",
    )
    source = _source_destination(tmp_path, f"{name}-source")
    lambda_source, agentcore_source = build_source_bundles(
        source,
        wheel_authorities={
            "lambda": lambda_authority,
            "agentcore": agentcore_authority,
        },
    )
    write_linux_arm64_dependency_manifest(
        lambda_dependencies,
        build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )
    write_linux_arm64_dependency_manifest(
        agentcore_dependencies,
        build_request_path=agentcore_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )
    with patch(
        "tools.build_phase66_source_bundles._load_checked_wheel_authorities",
        return_value={"lambda": lambda_authority, "agentcore": agentcore_authority},
    ):
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
        assert request["format"] == LOCKED_BUILD_REQUEST_FORMAT
        assert request["target"] == LINUX_ARM64_TARGET
        assert not (root / "dependency-artifact.json").exists()
        assert not (root / "deployment-manifest.json").exists()
        assert not (root / "release-manifest.json").exists()


def test_legacy_range_source_can_never_cross_the_deployment_seal(tmp_path: Path) -> None:
    source = _source_destination(tmp_path, "legacy-unlocked-source")
    lambda_source, agentcore_source = build_source_bundles(source, legacy_source_only=True)
    lambda_dependencies = _fake_dependencies(
        tmp_path,
        lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        "legacy-unlocked-lambda-dependencies",
    )
    agentcore_dependencies = _fake_dependencies(
        tmp_path,
        agentcore_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        "legacy-unlocked-agentcore-dependencies",
    )

    with pytest.raises(Phase6ReleaseAuthorityError):
        seal_release_bundles(
            source,
            lambda_dependencies=lambda_dependencies,
            agentcore_dependencies=agentcore_dependencies,
            destination=_deployment_destination(tmp_path, "legacy-unlocked"),
        )


def test_arbitrary_locked_candidate_authority_cannot_cross_seal(tmp_path: Path) -> None:
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name="candidate-lambda-dependencies",
    )
    agentcore_dependencies, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name="candidate-agentcore-dependencies",
    )
    source = _source_destination(tmp_path, "candidate-source")
    lambda_source, agentcore_source = build_source_bundles(
        source,
        wheel_authorities={"lambda": lambda_authority, "agentcore": agentcore_authority},
    )
    write_linux_arm64_dependency_manifest(
        lambda_dependencies,
        build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )
    write_linux_arm64_dependency_manifest(
        agentcore_dependencies,
        build_request_path=agentcore_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )

    with pytest.raises(ValueError, match="checked wheel authority"):
        seal_release_bundles(
            source,
            lambda_dependencies=lambda_dependencies,
            agentcore_dependencies=agentcore_dependencies,
            destination=_deployment_destination(tmp_path, "candidate"),
        )


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
        _source_destination(tmp_path, "dependency-source"),
        legacy_source_only=True,
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
        _source_destination(tmp_path, "native-source"),
        legacy_source_only=True,
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


def test_python2_only_wheel_tag_is_rejected_for_cpython312(tmp_path: Path) -> None:
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name="python2-only-lambda",
    )
    boto3_dist_info = _test_dist_info("boto3")
    wheel = lambda_dependencies / boto3_dist_info / "WHEEL"
    wheel.write_text(
        "Wheel-Version: 1.0\nGenerator: phase66-test\nRoot-Is-Purelib: true\nTag: py2-none-any\n\n",
        encoding="utf-8",
    )
    raw = wheel.read_bytes()
    encoded = base64.urlsafe_b64encode(sha256(raw).digest()).decode().rstrip("=")
    record = lambda_dependencies / boto3_dist_info / "RECORD"
    rows = list(csv.reader(StringIO(record.read_text(encoding="utf-8"), newline="")))
    for row in rows:
        if row[0] == f"{boto3_dist_info}/WHEEL":
            row[1:] = [f"sha256={encoded}", str(len(raw))]
            break
    else:  # pragma: no cover - the fixture always owns its WHEEL metadata
        raise AssertionError("fixture WHEEL record is missing")
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record.write_text(output.getvalue(), encoding="utf-8")
    _refresh_tree_authority(lambda_dependencies, lambda_authority)
    _agentcore_dependencies, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name="python2-only-agentcore",
    )
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, "python2-only-source"),
        wheel_authorities={"lambda": lambda_authority, "agentcore": agentcore_authority},
    )

    with pytest.raises(Phase6ReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            lambda_dependencies,
            build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


def test_wheel_tag_set_with_python3_remains_cpython312_compatible(tmp_path: Path) -> None:
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name="python2-and-3-lambda",
    )
    _rewrite_owned_dependency_file(
        lambda_dependencies,
        lambda_authority,
        distribution="boto3",
        filename="WHEEL",
        content=(
            b"Wheel-Version: 1.0\nGenerator: phase66-test\nRoot-Is-Purelib: true\n"
            b"Tag: py2-none-any\nTag: py3-none-any\n\n"
        ),
    )
    _unused, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name="python2-and-3-agentcore",
    )
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, "python2-and-3-source"),
        wheel_authorities={"lambda": lambda_authority, "agentcore": agentcore_authority},
    )

    manifest = write_linux_arm64_dependency_manifest(
        lambda_dependencies,
        build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )

    assert manifest.is_file()


@pytest.mark.parametrize(
    "tags",
    [
        "Tag: cp312-cp312-manylinux_2_28_aarch64\n",
        "Tag: cp312-cp312-linux_aarch64\n",
    ],
)
def test_native_wheel_tag_set_requires_manylinux2014_baseline(
    tmp_path: Path,
    tags: str,
) -> None:
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name=f"native-tag-{sha256(tags.encode()).hexdigest()[:8]}",
    )
    _rewrite_owned_dependency_file(
        lambda_dependencies,
        lambda_authority,
        distribution="pillow",
        filename="WHEEL",
        content=(
            f"Wheel-Version: 1.0\nGenerator: phase66-test\nRoot-Is-Purelib: false\n{tags}\n"
        ).encode("ascii"),
    )
    _unused, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name=f"native-tag-agent-{sha256(tags.encode()).hexdigest()[:8]}",
    )
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, f"native-tag-source-{sha256(tags.encode()).hexdigest()[:8]}"),
        wheel_authorities={"lambda": lambda_authority, "agentcore": agentcore_authority},
    )

    with pytest.raises(Phase6ReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            lambda_dependencies,
            build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


def test_native_wheel_tag_set_allows_newer_tag_only_with_baseline_alternative(
    tmp_path: Path,
) -> None:
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name="native-tag-baseline-lambda",
    )
    _rewrite_owned_dependency_file(
        lambda_dependencies,
        lambda_authority,
        distribution="pillow",
        filename="WHEEL",
        content=(
            b"Wheel-Version: 1.0\nGenerator: phase66-test\nRoot-Is-Purelib: false\n"
            b"Tag: cp312-cp312-manylinux_2_28_aarch64\n"
            b"Tag: cp312-cp312-manylinux_2_17_aarch64\n\n"
        ),
    )
    _unused, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name="native-tag-baseline-agentcore",
    )
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, "native-tag-baseline-source"),
        wheel_authorities={"lambda": lambda_authority, "agentcore": agentcore_authority},
    )

    manifest = write_linux_arm64_dependency_manifest(
        lambda_dependencies,
        build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
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
        _source_destination(tmp_path, f"native-required-{Path(relative).name}"),
        legacy_source_only=True,
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


@pytest.mark.parametrize("component", ["lambda", "agentcore"])
def test_locked_wheelhouse_is_hash_exact_and_extracted_without_install(
    tmp_path: Path,
    component: str,
) -> None:
    fixtures = {
        name: _locked_wheelhouse_fixture(
            tmp_path,
            component=name,
            name=f"locked-{component}-{name}",
        )
        for name in ("lambda", "agentcore")
    }
    authorities = {name: fixture[2] for name, fixture in fixtures.items()}
    lambda_source, agentcore_source = build_source_bundles(
        _source_destination(tmp_path, f"locked-{component}-source"),
        wheel_authorities=authorities,
    )
    source = {"lambda": lambda_source, "agentcore": agentcore_source}[component]
    request = verify_dependency_build_request(source / DEPENDENCY_BUILD_REQUEST_FILENAME)
    assert request["format"] == LOCKED_BUILD_REQUEST_FORMAT
    assert all(
        " --hash=sha256:" in line for line in (source / "requirements.txt").read_text().splitlines()
    )

    destination_name = f"phase6-{component}-dependencies"
    destination = tmp_path / f"locked-{component}-output" / destination_name
    manifest_path = build_linux_arm64_dependencies_from_wheelhouse(
        fixtures[component][1],
        destination=destination,
        build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )
    assert manifest_path == destination / "dependency-artifact.json"
    manifest = verify_linux_arm64_dependency_artifact(
        destination,
        build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )
    assert manifest["format"] == "phase6-linux-arm64-dependencies-v2"

    tampered = tmp_path / f"locked-{component}-tampered-wheelhouse"
    shutil.copytree(fixtures[component][1], tampered)
    target = next(tampered.glob("*.whl"))
    target.write_bytes(target.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="wheelhouse authority"):
        build_linux_arm64_dependencies_from_wheelhouse(
            tampered,
            destination=tmp_path / f"locked-{component}-tampered" / destination_name,
            build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


@pytest.mark.parametrize("component", ["lambda", "agentcore"])
def test_predownloaded_wheelhouse_capture_emits_reviewable_canonical_authority(
    tmp_path: Path,
    component: str,
) -> None:
    _seed, wheelhouse, expected = _locked_wheelhouse_fixture(
        tmp_path,
        component=component,
        name=f"capture-{component}",
    )
    output = tmp_path / f"phase6-{component}-wheel-authority.json"
    captured = capture_wheelhouse_authority_candidate(
        wheelhouse,
        component=component,  # type: ignore[arg-type]
        output_path=output,
    )

    assert captured == expected
    assert output.read_bytes() == render_manifest(expected)
    assert str(tmp_path) not in output.read_text(encoding="utf-8")
    wheels = captured["wheels"]
    assert isinstance(wheels, list)
    assert all(
        record["sha256"] == sha256((wheelhouse / record["filename"]).read_bytes()).hexdigest()
        for record in wheels
    )


@pytest.mark.parametrize(
    ("distribution", "header"),
    [
        ("boto3", "Requires-Dist: absent-package>=1\n"),
        ("boto3", "Requires-Dist: pydantic>=3\n"),
        ("botocore", "Requires-Dist: awscrt>=2; extra == 'crt'\n"),
    ],
)
def test_capture_rejects_incomplete_version_drifted_or_extra_activated_closure(
    tmp_path: Path,
    distribution: str,
    header: str,
) -> None:
    _seed, wheelhouse, _authority = _locked_wheelhouse_fixture(
        tmp_path,
        component="lambda",
        name=f"invalid-closure-{distribution}-{sha256(header.encode()).hexdigest()[:8]}",
    )
    wheel = next(wheelhouse.glob(f"{distribution.replace('-', '_')}-*.whl"))
    _rewrite_wheel_metadata(
        wheel,
        distribution=distribution,
        appended_headers=header,
    )

    with pytest.raises(ValueError, match="wheelhouse authority candidate"):
        capture_wheelhouse_authority_candidate(wheelhouse, component="lambda")


def test_capture_ignores_dependency_for_unrequested_extra(tmp_path: Path) -> None:
    _seed, wheelhouse, _authority = _locked_wheelhouse_fixture(
        tmp_path,
        component="lambda",
        name="inactive-extra-closure",
    )
    wheel = next(wheelhouse.glob("boto3-*.whl"))
    _rewrite_wheel_metadata(
        wheel,
        distribution="boto3",
        appended_headers="Requires-Dist: absent-package>=1; extra == 'unused'\n",
    )

    captured = capture_wheelhouse_authority_candidate(wheelhouse, component="lambda")

    assert captured["component"] == "lambda"


def test_locked_build_rechecks_dependency_closure_not_just_candidate_hashes(
    tmp_path: Path,
) -> None:
    fixtures = {
        component: _locked_wheelhouse_fixture(
            tmp_path,
            component=component,
            name=f"build-closure-{component}",
        )
        for component in ("lambda", "agentcore")
    }
    lambda_wheelhouse = fixtures["lambda"][1]
    boto3_wheel = next(lambda_wheelhouse.glob("boto3-*.whl"))
    _rewrite_wheel_metadata(
        boto3_wheel,
        distribution="boto3",
        appended_headers="Requires-Dist: absent-package>=1\n",
    )
    lambda_authority = fixtures["lambda"][2]
    lambda_wheels = lambda_authority["wheels"]
    assert isinstance(lambda_wheels, list)
    for record in lambda_wheels:
        if record["filename"] == boto3_wheel.name:
            record["sha256"] = sha256(boto3_wheel.read_bytes()).hexdigest()
            break
    else:  # pragma: no cover - fixture always includes boto3
        raise AssertionError("fixture boto3 authority is missing")
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, "build-closure-source"),
        wheel_authorities={
            "lambda": lambda_authority,
            "agentcore": fixtures["agentcore"][2],
        },
    )

    with pytest.raises(ValueError, match="locked wheelhouse authority"):
        build_linux_arm64_dependencies_from_wheelhouse(
            lambda_wheelhouse,
            destination=tmp_path / "build-closure-output/phase6-lambda-dependencies",
            build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


@pytest.mark.parametrize("collision_scope", ["within", "across"])
def test_capture_rejects_casefolded_duplicate_archive_paths(
    tmp_path: Path,
    collision_scope: str,
) -> None:
    _seed, wheelhouse, _authority = _locked_wheelhouse_fixture(
        tmp_path,
        component="lambda",
        name=f"casefold-{collision_scope}",
    )
    boto3_wheel = next(wheelhouse.glob("boto3-*.whl"))
    if collision_scope == "within":
        _append_wheel_member(boto3_wheel, "BOTO3/__init__.py")
    else:
        botocore_wheel = next(wheelhouse.glob("botocore-*.whl"))
        _append_wheel_member(boto3_wheel, "Shared/authority.py")
        _append_wheel_member(botocore_wheel, "shared/AUTHORITY.py")

    with pytest.raises(ValueError, match="wheelhouse authority candidate"):
        capture_wheelhouse_authority_candidate(wheelhouse, component="lambda")


def test_predownloaded_wheelhouse_capture_accepts_nested_sitecustomize_module(
    tmp_path: Path,
) -> None:
    _seed, wheelhouse, _expected = _locked_wheelhouse_fixture(
        tmp_path,
        component="agentcore",
        name="capture-agentcore-nested-sitecustomize",
        extra_record_owned_file=(
            "bedrock-agentcore",
            "opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py",
            b"# Imported explicitly by the auto-instrumentation package.\n",
        ),
    )

    captured = capture_wheelhouse_authority_candidate(
        wheelhouse,
        component="agentcore",
    )

    assert captured["component"] == "agentcore"
    assert captured["format"] == WHEEL_AUTHORITY_FORMAT


def test_predownloaded_wheelhouse_capture_accepts_inert_data_script(
    tmp_path: Path,
) -> None:
    seed, wheelhouse, _expected = _locked_wheelhouse_fixture(
        tmp_path,
        component="agentcore",
        name="capture-agentcore-data-script",
        extra_record_owned_file=(
            "bedrock-agentcore",
            "jmespath-1.0.data/scripts/jp.py",
            b"raise RuntimeError('must require explicit script execution')\n",
        ),
    )

    # Raw wheel extraction leaves the script below the namespaced .data directory;
    # adding the extracted dependency root to sys.path cannot resolve it as `jp`.
    assert PathFinder.find_spec("jp", [str(seed)]) is None
    captured = capture_wheelhouse_authority_candidate(
        wheelhouse,
        component="agentcore",
    )

    assert captured["component"] == "agentcore"
    assert captured["format"] == WHEEL_AUTHORITY_FORMAT


@pytest.mark.parametrize(
    "relative",
    [
        "sitecustomize.py",
        "sitecustomize/__init__.py",
        "usercustomize/__init__.py",
        "site/__init__.py",
        "json.py",
        "unsafe-startup.pth",
        "boto3-1.0.data/purelib/runtime_extension.py",
        "boto3-1.0.data/platlib/runtime_extension.py",
    ],
)
def test_locked_dependency_rejects_record_owned_startup_or_stdlib_shadow(
    tmp_path: Path,
    relative: str,
) -> None:
    case_name = relative.replace("/", "-")
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name=f"unsafe-lambda-{case_name}",
    )
    target = lambda_dependencies / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("raise RuntimeError('pre-gate execution')\n", encoding="utf-8")
    raw = target.read_bytes()
    encoded = base64.urlsafe_b64encode(sha256(raw).digest()).decode().rstrip("=")
    record = lambda_dependencies / _test_dist_info("boto3") / "RECORD"
    rows = list(csv.reader(StringIO(record.read_text(encoding="utf-8"), newline="")))
    rows.insert(-1, [relative, f"sha256={encoded}", str(len(raw))])
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record.write_text(output.getvalue(), encoding="utf-8")
    _refresh_tree_authority(lambda_dependencies, lambda_authority)
    agentcore_dependencies, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name=f"unsafe-agentcore-{case_name}",
    )
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, f"unsafe-source-{case_name}"),
        wheel_authorities={
            "lambda": lambda_authority,
            "agentcore": agentcore_authority,
        },
    )
    del agentcore_dependencies

    with pytest.raises(Phase6ReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            lambda_dependencies,
            build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


def test_locked_dependency_requires_exact_record_hash_and_size(tmp_path: Path) -> None:
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name="record-hash-lambda",
    )
    record = lambda_dependencies / _test_dist_info("boto3") / "RECORD"
    rows = list(csv.reader(StringIO(record.read_text(encoding="utf-8"), newline="")))
    rows[0][1] = f"sha256={'f' * 43}"
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record.write_text(output.getvalue(), encoding="utf-8")
    _refresh_tree_authority(lambda_dependencies, lambda_authority)
    _agentcore_dependencies, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name="record-hash-agentcore",
    )
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, "record-hash-source"),
        wheel_authorities={"lambda": lambda_authority, "agentcore": agentcore_authority},
    )

    with pytest.raises(Phase6ReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            lambda_dependencies,
            build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


def test_locked_dependency_rejects_native_file_owned_by_pure_wheel(tmp_path: Path) -> None:
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name="pure-owner-lambda",
    )
    native = lambda_dependencies / "boto3/native.so"
    native.write_bytes(_synthetic_arm64_elf())
    raw = native.read_bytes()
    encoded = base64.urlsafe_b64encode(sha256(raw).digest()).decode().rstrip("=")
    record = lambda_dependencies / _test_dist_info("boto3") / "RECORD"
    rows = list(csv.reader(StringIO(record.read_text(encoding="utf-8"), newline="")))
    rows.insert(-1, ["boto3/native.so", f"sha256={encoded}", str(len(raw))])
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record.write_text(output.getvalue(), encoding="utf-8")
    _refresh_tree_authority(lambda_dependencies, lambda_authority)
    _agentcore_dependencies, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name="pure-owner-agentcore",
    )
    lambda_source, _agentcore_source = build_source_bundles(
        _source_destination(tmp_path, "pure-owner-source"),
        wheel_authorities={"lambda": lambda_authority, "agentcore": agentcore_authority},
    )

    with pytest.raises(Phase6ReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            lambda_dependencies,
            build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


def test_stale_self_manifested_source_cannot_be_sealed(tmp_path: Path) -> None:
    lambda_dependencies, lambda_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="lambda",
        name="stale-lambda-dependencies",
    )
    agentcore_dependencies, agentcore_authority = _locked_dependencies_and_authority(
        tmp_path,
        component="agentcore",
        name="stale-agentcore-dependencies",
    )
    source = _source_destination(tmp_path, "stale-source")
    lambda_source, agentcore_source = build_source_bundles(
        source,
        wheel_authorities={"lambda": lambda_authority, "agentcore": agentcore_authority},
    )
    write_linux_arm64_dependency_manifest(
        lambda_dependencies,
        build_request_path=lambda_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )
    write_linux_arm64_dependency_manifest(
        agentcore_dependencies,
        build_request_path=agentcore_source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )
    verifier = lambda_source / "mr_lister/release/phase6.py"
    verifier.write_text(verifier.read_text(encoding="utf-8") + "\n# stale\n", encoding="utf-8")
    manifest_path = lambda_source / "source-manifest.json"
    manifest = {
        "algorithm": "sha256",
        "files": _tree_files(lambda_source, excluded=frozenset({"source-manifest.json"})),
        "format": "phase6-source-v1",
    }
    manifest_path.write_bytes(render_manifest(manifest))
    verify_source_bundle(lambda_source)

    with (
        patch(
            "tools.build_phase66_source_bundles._load_checked_wheel_authorities",
            return_value={"lambda": lambda_authority, "agentcore": agentcore_authority},
        ),
        pytest.raises(ValueError, match="current repository authority"),
    ):
        seal_release_bundles(
            source,
            lambda_dependencies=lambda_dependencies,
            agentcore_dependencies=agentcore_dependencies,
            destination=_deployment_destination(tmp_path, "stale"),
        )


def test_component_archives_and_descriptor_are_deterministic_and_embedded_verified(
    tmp_path: Path,
) -> None:
    first_lambda, _first_agentcore, first_fingerprint, *_rest = _sealed_release(
        tmp_path, "artifact-first"
    )
    second_lambda, _second_agentcore, second_fingerprint, *_rest_two = _sealed_release(
        tmp_path, "artifact-second"
    )
    first_deployment = first_lambda.parent
    second_deployment = second_lambda.parent
    first_artifacts = first_deployment.parent / "phase6-artifacts"
    second_artifacts = second_deployment.parent / "phase6-artifacts"

    assert first_fingerprint == second_fingerprint
    for filename in ("phase6-lambda.zip", "phase6-agentcore.zip", "deployment-descriptor.json"):
        assert (first_artifacts / filename).read_bytes() == (
            second_artifacts / filename
        ).read_bytes()
    descriptor = verify_phase6_deployment_artifacts(
        first_deployment,
        artifact_root=first_artifacts,
        verify_current_source=False,
    )
    assert descriptor["release_fingerprint"] == first_fingerprint
    assert descriptor["components"]["lambda"]["runtime"] == "python3.12"

    archive = first_artifacts / "phase6-lambda.zip"
    raw = bytearray(archive.read_bytes())
    raw[-1] ^= 1
    archive.write_bytes(raw)
    with pytest.raises(ValueError, match="deployment artifact"):
        verify_phase6_deployment_artifacts(
            first_deployment,
            artifact_root=first_artifacts,
            verify_current_source=False,
        )
