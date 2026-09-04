"""Deterministic, provider-free Phase 7.6 guard release authority tests."""

from __future__ import annotations

import base64
import csv
import importlib.util
import json
import shutil
import sys
import zipfile
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pytest

import mr_lister.release.phase7 as phase7_release
from mr_lister.release.phase7 import (
    APPLICATION_RELEASE_FINGERPRINT_ENV,
    CAPABILITY_FREE_PACKAGE_INIT_PATHS,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    GUARD_ENTRYPOINT,
    GUARD_RELEASE_FINGERPRINT_ENV,
    PINNED_GUARD_DISTRIBUTIONS,
    PINNED_GUARD_WHEELS,
    SOURCE_MANIFEST_FILENAME,
    Phase7GuardReleaseAuthorityError,
    inventory,
    render_manifest,
    verify_dependency_build_request,
    verify_linux_arm64_dependency_artifact,
    verify_phase7_guard_release,
    verify_source_manifest,
)
from tools import build_phase76_guard_bundle as phase76_builder
from tools.build_phase76_guard_bundle import (
    Phase76GuardBundleError,
    build_guard_source_bundle,
    build_linux_arm64_dependencies_from_wheelhouse,
    resolve_guard_import_closure,
    seal_guard_release,
    verify_guard_deployment_artifact,
    write_linux_arm64_dependency_manifest,
)
from tools.verify_phase76_guard_deployment import (
    verify_iam_role_observations,
    verify_lambda_configuration_observation,
    verify_lambda_invocation_observation,
    verify_lambda_surface_absence_observations,
    verify_legacy_query_absence_observations,
    verify_phase6_application_release_observation,
    verify_s3_head_observation,
    verify_stack_observation,
    verify_stack_resources_observation,
)

_TREE_AUTHORITY_PATCH: pytest.MonkeyPatch | None = None
APPLICATION_RELEASE_FINGERPRINT = "a" * 64


@pytest.fixture(autouse=True)
def _restore_dependency_tree_authority(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    global _TREE_AUTHORITY_PATCH
    _TREE_AUTHORITY_PATCH = monkeypatch
    yield
    _TREE_AUTHORITY_PATCH = None


def _source(tmp_path: Path, name: str) -> Path:
    return build_guard_source_bundle(tmp_path / name / "phase7-guard-source")


def _synthetic_arm64_elf(*, machine: int = 183) -> bytes:
    value = bytearray(4_096)
    value[:7] = b"\x7fELF\x02\x01\x01"
    value[16:18] = (3).to_bytes(2, "little")
    value[18:20] = machine.to_bytes(2, "little")
    value[20:24] = (1).to_bytes(4, "little")
    value[52:54] = (64).to_bytes(2, "little")
    return bytes(value)


def _dependencies(tmp_path: Path, source: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    request_path = source / DEPENDENCY_BUILD_REQUEST_FILENAME
    request = verify_dependency_build_request(request_path)
    requirements = request["requirements"]
    assert isinstance(requirements, dict)
    assert requirements["required_distributions"] == sorted(
        name for name, _version in PINNED_GUARD_DISTRIBUTIONS
    )
    package_roots = {
        "annotated-types": "annotated_types",
        "boto3": "boto3",
        "botocore": "botocore",
        "jmespath": "jmespath",
        "pydantic": "pydantic",
        "pydantic-core": "pydantic_core",
        "python-dateutil": "dateutil",
        "s3transfer": "s3transfer",
        "six": "six.py",
        "typing-extensions": "typing_extensions.py",
        "typing-inspection": "typing_inspection",
        "urllib3": "urllib3",
    }
    owned: dict[str, list[Path]] = {}
    dist_infos: dict[str, Path] = {}
    for distribution, version in PINNED_GUARD_DISTRIBUTIONS:
        package = root / package_roots[distribution]
        if package.suffix == ".py":
            package.write_text("VERSION = 'test'\n", encoding="utf-8")
            package_files = [package]
        else:
            package.mkdir()
            initializer = package / "__init__.py"
            initializer.write_text("VERSION = 'test'\n", encoding="utf-8")
            package_files = [initializer]
        dist_info = root / f"{distribution.replace('-', '_')}-{version}.dist-info"
        dist_info.mkdir()
        metadata = dist_info / "METADATA"
        metadata.write_text(
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n\n",
            encoding="utf-8",
        )
        tag = (
            "cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64"
            if distribution == "pydantic-core"
            else "py3-none-any"
        )
        wheel = dist_info / "WHEEL"
        wheel.write_text(
            "Wheel-Version: 1.0\nGenerator: phase76-test\n"
            f"Root-Is-Purelib: {'false' if distribution == 'pydantic-core' else 'true'}\n"
            f"Tag: {tag}\n\n",
            encoding="utf-8",
        )
        owned[distribution] = [*package_files, metadata, wheel]
        dist_infos[distribution] = dist_info
    native = root / "pydantic_core/_pydantic_core.cpython-312-aarch64-linux-gnu.so"
    native.write_bytes(_synthetic_arm64_elf())
    owned["pydantic-core"].append(native)
    for distribution, _version in PINNED_GUARD_DISTRIBUTIONS:
        dist_info = dist_infos[distribution]
        record = dist_info / "RECORD"
        rows: list[list[str]] = []
        for path in sorted(owned[distribution]):
            raw = path.read_bytes()
            encoded = base64.urlsafe_b64encode(sha256(raw).digest()).decode("ascii").rstrip("=")
            rows.append([path.relative_to(root).as_posix(), f"sha256={encoded}", str(len(raw))])
        rows.append([record.relative_to(root).as_posix(), "", ""])
        output = StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        record.write_text(output.getvalue(), encoding="utf-8")
    assert _TREE_AUTHORITY_PATCH is not None
    dependency_files = inventory(root, excluded=frozenset({"dependency-artifact.json"}))
    _TREE_AUTHORITY_PATCH.setattr(
        phase7_release,
        "PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT",
        sha256(render_manifest({"files": dependency_files})).hexdigest(),
    )
    write_linux_arm64_dependency_manifest(root, build_request_path=request_path)
    return root


def _sealed(
    tmp_path: Path,
    name: str,
    *,
    application_release_fingerprint: str = APPLICATION_RELEASE_FINGERPRINT,
):  # type: ignore[no-untyped-def]
    source = _source(tmp_path, f"{name}-source")
    dependencies = _dependencies(tmp_path, source, f"{name}-dependencies")
    result = seal_guard_release(
        source,
        application_release_fingerprint=application_release_fingerprint,
        dependencies=dependencies,
        deployment_destination=tmp_path / name / "phase7-guard-deployment",
        artifact_destination=tmp_path / name / "phase7-guard-artifact",
    )
    return source, dependencies, result


def _attestation(
    descriptor: dict[str, object],
    *,
    operation: str,
    outcome: str,
    approval_authority_current: bool | None,
) -> bytes:
    value = {
        "approval_authority_current": approval_authority_current,
        "approval_guard_enabled": True,
        "contract_fingerprint": (
            "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"
        ),
        "contract_version": "7.0.1",
        "guard_release_fingerprint": descriptor["release_fingerprint"],
        "operation": operation,
        "outcome": outcome,
        "profile_fingerprint": descriptor["profile_fingerprint"],
        "provider_calls_authorized": 0,
        "publication_enabled": False,
        "query_enabled": False,
        "request_enabled": False,
    }
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    value["fingerprint"] = sha256(encoded).hexdigest()
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def test_source_bundle_is_the_exact_local_guard_import_closure(tmp_path: Path) -> None:
    closure = resolve_guard_import_closure()
    source = _source(tmp_path, "closure")
    manifest = verify_source_manifest(source)
    paths = {record["path"] for record in manifest["files"]}

    assert GUARD_ENTRYPOINT == (
        "mr_lister.cloud.phase7_guard_entrypoint.publication_guard_verification_handler"
    )
    assert "mr_lister.cloud.phase7_guard_entrypoint" in closure
    assert "mr_lister.cloud.phase7_guard_composition" in closure
    assert "mr_lister.publication.guard_verification" in closure
    assert "mr_lister.release.phase7" in closure
    assert "config/product_profiles/gildan_64000_swiftpod.json" in paths
    assert manifest["profile"]["publish_enabled"] is False
    assert len(manifest["profile"]["fingerprint"]) == 64
    assert all(
        (source / relative).read_bytes() == b"" for relative in CAPABILITY_FREE_PACKAGE_INIT_PATHS
    )
    assert "mr_lister.release.phase6" not in closure

    forbidden = (
        "mr_lister/production/",
        "mr_lister/workflow/",
        "mr_lister/publication/execution_dynamodb.py",
        "mr_lister/publication/execution_service.py",
        "mr_lister/publication/execution_store.py",
        "mr_lister/publication/provider_boundary.py",
        "mr_lister/publication/provider_coordinator.py",
        "mr_lister/publication/provider_credentials.py",
        "mr_lister/publication/service.py",
    )
    assert not any(path.startswith(prefix) for path in paths for prefix in forbidden)


@pytest.mark.parametrize("relative", CAPABILITY_FREE_PACKAGE_INIT_PATHS)
def test_eager_package_initializer_cannot_enter_guard_bundle(
    tmp_path: Path,
    relative: str,
) -> None:
    source = _source(tmp_path, f"package-init-{relative.replace('/', '-')}")
    initializer = source / relative
    initializer.write_text("raise RuntimeError('pre-gate execution')\n", encoding="utf-8")

    with pytest.raises(Phase7GuardReleaseAuthorityError):
        verify_source_manifest(source)


def test_changed_profile_cannot_claim_a_consistently_resealed_fingerprint(tmp_path: Path) -> None:
    source = _source(tmp_path, "profile-drift")
    profile_path = source / "config/product_profiles/gildan_64000_swiftpod.json"
    profile_value = json.loads(profile_path.read_bytes())
    profile_value["retail_price_cents"] += 1
    profile_path.write_text(json.dumps(profile_value, indent=2, sort_keys=True) + "\n")

    canonical_profile = {**profile_value, "placement": None}
    claimed = sha256(
        json.dumps(canonical_profile, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    manifest_path = source / SOURCE_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_bytes())
    manifest["profile"]["fingerprint"] = claimed
    manifest["files"] = inventory(source, excluded=frozenset({SOURCE_MANIFEST_FILENAME}))
    manifest_path.write_bytes(render_manifest(manifest))

    with pytest.raises(Phase7GuardReleaseAuthorityError):
        verify_source_manifest(source)


def test_unlisted_source_or_forbidden_import_cannot_be_sealed(tmp_path: Path) -> None:
    source = _source(tmp_path, "extra")
    unexpected = source / "mr_lister/unlisted_guard_power.py"
    unexpected.write_text("POWER = True\n", encoding="utf-8")
    manifest_path = source / SOURCE_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"] = inventory(source, excluded=frozenset({SOURCE_MANIFEST_FILENAME}))
    manifest_path.write_bytes(render_manifest(manifest))

    with pytest.raises(Phase7GuardReleaseAuthorityError):
        verify_source_manifest(source)

    source = _source(tmp_path, "forbidden-import")
    entrypoint = source / "mr_lister/cloud/phase7_guard_entrypoint.py"
    entrypoint.write_text(
        entrypoint.read_text(encoding="utf-8")
        + (
            "\nfrom mr_lister.publication.provider_boundary "
            "import StagedPublicationProviderBoundary\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(Phase7GuardReleaseAuthorityError):
        verify_source_manifest(source)


def test_linux_arm64_dependency_manifest_requires_native_pydantic_core(tmp_path: Path) -> None:
    source = _source(tmp_path, "native-source")
    dependencies = _dependencies(tmp_path, source, "native-dependencies")
    request = source / DEPENDENCY_BUILD_REQUEST_FILENAME
    verify_linux_arm64_dependency_artifact(dependencies, build_request_path=request)

    native = dependencies / "pydantic_core/_pydantic_core.cpython-312-aarch64-linux-gnu.so"
    native.write_bytes(_synthetic_arm64_elf(machine=62))
    with pytest.raises(Phase7GuardReleaseAuthorityError):
        verify_linux_arm64_dependency_artifact(dependencies, build_request_path=request)


@pytest.mark.parametrize("relative", ["sitecustomize.py", "json.py", "unsafe-startup.pth"])
def test_unowned_startup_hook_or_stdlib_shadow_cannot_be_sealed(
    tmp_path: Path,
    relative: str,
) -> None:
    source = _source(tmp_path, f"shadow-source-{relative.replace('.', '-')}")
    dependencies = _dependencies(
        tmp_path,
        source,
        f"shadow-dependencies-{relative.replace('.', '-')}",
    )
    (dependencies / "dependency-artifact.json").unlink()
    (dependencies / relative).write_text("raise RuntimeError('executed before guard')\n")

    with pytest.raises(Phase7GuardReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            dependencies,
            build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


def test_self_rehashed_allowed_distribution_cannot_be_resealed(tmp_path: Path) -> None:
    source = _source(tmp_path, "self-rehash-source")
    dependencies = _dependencies(tmp_path, source, "self-rehash-dependencies")
    (dependencies / "dependency-artifact.json").unlink()
    target = dependencies / "boto3/__init__.py"
    target.write_text("EXFILTRATE = True\n", encoding="utf-8")
    record_path = dependencies / "boto3-1.43.73.dist-info/RECORD"
    rows = list(csv.reader(StringIO(record_path.read_text(encoding="utf-8"), newline="")))
    raw = target.read_bytes()
    encoded = base64.urlsafe_b64encode(sha256(raw).digest()).decode("ascii").rstrip("=")
    for row in rows:
        if row[0] == "boto3/__init__.py":
            row[1:] = [f"sha256={encoded}", str(len(raw))]
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record_path.write_text(output.getvalue(), encoding="utf-8")

    with pytest.raises(Phase7GuardReleaseAuthorityError):
        write_linux_arm64_dependency_manifest(
            dependencies,
            build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


def test_dependency_builder_extracts_only_the_exact_hash_locked_wheelhouse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, "wheel-source")
    seed = _dependencies(tmp_path, source, "wheel-seed")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    synthetic_wheels: list[tuple[str, str, str, str]] = []
    for distribution, version, filename, _official_fingerprint in PINNED_GUARD_WHEELS:
        dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
        rows = csv.reader(
            StringIO((seed / dist_info / "RECORD").read_text(encoding="utf-8"), newline="")
        )
        wheel_path = wheelhouse / filename
        with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, _encoded_hash, _size in rows:
                archive.write(seed / relative, arcname=relative)
        synthetic_wheels.append(
            (distribution, version, filename, sha256(wheel_path.read_bytes()).hexdigest())
        )
    monkeypatch.setattr(phase76_builder, "PINNED_GUARD_WHEELS", tuple(synthetic_wheels))

    destination = tmp_path / "built" / "linux-arm64-dependencies"
    manifest_path = build_linux_arm64_dependencies_from_wheelhouse(
        wheelhouse,
        destination=destination,
        build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )
    assert manifest_path == destination / "dependency-artifact.json"
    verify_linux_arm64_dependency_artifact(
        destination,
        build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
    )

    tampered_wheelhouse = tmp_path / "tampered-wheelhouse"
    shutil.copytree(wheelhouse, tampered_wheelhouse)
    target = tampered_wheelhouse / synthetic_wheels[1][2]
    target.write_bytes(target.read_bytes() + b"tampered")
    with pytest.raises(Phase76GuardBundleError):
        build_linux_arm64_dependencies_from_wheelhouse(
            tampered_wheelhouse,
            destination=tmp_path / "tampered" / "linux-arm64-dependencies",
            build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )


def test_release_zip_and_descriptor_are_deterministic_and_fully_bound(tmp_path: Path) -> None:
    _source_one, _dependencies_one, first = _sealed(tmp_path, "first")
    _source_two, _dependencies_two, second = _sealed(tmp_path, "second")

    assert first.release_fingerprint == second.release_fingerprint
    assert first.application_release_fingerprint == APPLICATION_RELEASE_FINGERPRINT
    assert first.archive_fingerprint == second.archive_fingerprint
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.descriptor_path.read_bytes() == second.descriptor_path.read_bytes()
    descriptor = verify_guard_deployment_artifact(
        first.deployment_root,
        archive_path=first.archive_path,
        descriptor_path=first.descriptor_path,
    )
    assert descriptor["release_fingerprint"] == first.release_fingerprint
    assert descriptor["application_release_fingerprint"] == APPLICATION_RELEASE_FINGERPRINT
    assert descriptor["s3_binding"]["object_version_required"] is True
    assert descriptor["entrypoint"] == GUARD_ENTRYPOINT

    binding = verify_phase7_guard_release(
        {
            GUARD_RELEASE_FINGERPRINT_ENV: first.release_fingerprint,
            APPLICATION_RELEASE_FINGERPRINT_ENV: APPLICATION_RELEASE_FINGERPRINT,
        },
        bundle_root=first.deployment_root,
    )
    assert binding.release_fingerprint == first.release_fingerprint
    assert binding.application_release_fingerprint == APPLICATION_RELEASE_FINGERPRINT
    assert binding.profile_fingerprint == first.profile_fingerprint


def test_application_binding_does_not_redefine_the_guard_release_or_archive(
    tmp_path: Path,
) -> None:
    _source_one, _dependencies_one, first = _sealed(tmp_path, "application-one")
    other_application_release = "d" * 64
    _source_two, _dependencies_two, second = _sealed(
        tmp_path,
        "application-two",
        application_release_fingerprint=other_application_release,
    )

    assert first.application_release_fingerprint == APPLICATION_RELEASE_FINGERPRINT
    assert second.application_release_fingerprint == other_application_release
    assert first.release_fingerprint == second.release_fingerprint
    assert first.archive_fingerprint == second.archive_fingerprint
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.descriptor_path.read_bytes() != second.descriptor_path.read_bytes()


def test_stale_self_manifested_source_cannot_be_sealed(tmp_path: Path) -> None:
    source = _source(tmp_path, "stale-seal-source")
    dependencies = _dependencies(tmp_path, source, "stale-seal-dependencies")
    embedded_verifier = source / "mr_lister/release/phase7.py"
    embedded_verifier.write_text(
        embedded_verifier.read_text(encoding="utf-8") + "\n# stale verifier bytes\n",
        encoding="utf-8",
    )
    manifest_path = source / SOURCE_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"] = inventory(source, excluded=frozenset({SOURCE_MANIFEST_FILENAME}))
    manifest_path.write_bytes(render_manifest(manifest))
    verify_source_manifest(source)

    with pytest.raises(Phase76GuardBundleError):
        seal_guard_release(
            source,
            application_release_fingerprint=APPLICATION_RELEASE_FINGERPRINT,
            dependencies=dependencies,
            deployment_destination=tmp_path / "stale-seal" / "phase7-guard-deployment",
            artifact_destination=tmp_path / "stale-seal" / "phase7-guard-artifact",
        )


def test_stale_self_consistent_artifact_fails_standalone_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, "stale-verify-source")
    dependencies = _dependencies(tmp_path, source, "stale-verify-dependencies")
    embedded_verifier = source / "mr_lister/release/phase7.py"
    embedded_verifier.write_text(
        embedded_verifier.read_text(encoding="utf-8") + "\n# stale verifier bytes\n",
        encoding="utf-8",
    )
    manifest_path = source / SOURCE_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"] = inventory(source, excluded=frozenset({SOURCE_MANIFEST_FILENAME}))
    manifest_path.write_bytes(render_manifest(manifest))
    verify_source_manifest(source)

    with monkeypatch.context() as stale_builder:
        stale_builder.setattr(
            phase76_builder,
            "_verify_current_repository_source_authority",
            lambda _root: None,
        )
        result = seal_guard_release(
            source,
            application_release_fingerprint=APPLICATION_RELEASE_FINGERPRINT,
            dependencies=dependencies,
            deployment_destination=tmp_path / "stale-verify" / "phase7-guard-deployment",
            artifact_destination=tmp_path / "stale-verify" / "phase7-guard-artifact",
        )

    with pytest.raises(Phase76GuardBundleError):
        verify_guard_deployment_artifact(
            result.deployment_root,
            archive_path=result.archive_path,
            descriptor_path=result.descriptor_path,
        )


def test_final_artifact_executes_its_embedded_release_verifier(tmp_path: Path) -> None:
    _source_root, _dependencies_root, result = _sealed(tmp_path, "embedded-verifier")
    module_name = "_phase76_embedded_release_acceptance"
    spec = importlib.util.spec_from_file_location(
        module_name,
        result.deployment_root / "mr_lister/release/phase7.py",
    )
    assert spec is not None and spec.loader is not None
    embedded = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = embedded
    prior_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(embedded)
        # Production embeds the fixed official tree authority. This test fixture substitutes only
        # its synthetic, RECORD-owned dependency-tree fingerprint before executing the exact
        # embedded parsing and manifest-verification implementation.
        embedded.PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT = (
            phase7_release.PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT
        )
        binding = embedded.verify_phase7_guard_release(
            {
                embedded.GUARD_RELEASE_FINGERPRINT_ENV: result.release_fingerprint,
                embedded.APPLICATION_RELEASE_FINGERPRINT_ENV: (APPLICATION_RELEASE_FINGERPRINT),
            },
            bundle_root=result.deployment_root,
        )
    finally:
        sys.dont_write_bytecode = prior_bytecode_setting
        sys.modules.pop(module_name, None)

    assert binding.release_fingerprint == result.release_fingerprint
    assert binding.application_release_fingerprint == APPLICATION_RELEASE_FINGERPRINT
    assert binding.profile_fingerprint == result.profile_fingerprint


def test_release_requires_independently_valid_specific_and_application_fingerprints(
    tmp_path: Path,
) -> None:
    _source_root, _dependencies_root, result = _sealed(tmp_path, "environment")

    binding = verify_phase7_guard_release(
        {
            GUARD_RELEASE_FINGERPRINT_ENV: result.release_fingerprint,
            APPLICATION_RELEASE_FINGERPRINT_ENV: APPLICATION_RELEASE_FINGERPRINT,
        },
        bundle_root=result.deployment_root,
    )
    assert binding.release_fingerprint == result.release_fingerprint
    assert binding.application_release_fingerprint == APPLICATION_RELEASE_FINGERPRINT

    for environment in (
        {GUARD_RELEASE_FINGERPRINT_ENV: result.release_fingerprint},
        {
            GUARD_RELEASE_FINGERPRINT_ENV: result.release_fingerprint,
            APPLICATION_RELEASE_FINGERPRINT_ENV: "0" * 64,
        },
    ):
        with pytest.raises(Phase7GuardReleaseAuthorityError) as captured:
            verify_phase7_guard_release(environment, bundle_root=result.deployment_root)
        assert str(captured.value) == "Phase 7 guard release authority is invalid"
        assert captured.value.__cause__ is None


def test_any_deployment_or_archive_byte_drift_fails_closed(tmp_path: Path) -> None:
    _source_root, _dependencies_root, result = _sealed(tmp_path, "tamper")
    target = result.deployment_root / "mr_lister/publication/guard_verification.py"
    target.write_text(target.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    with pytest.raises(Phase7GuardReleaseAuthorityError):
        verify_phase7_guard_release(
            {
                GUARD_RELEASE_FINGERPRINT_ENV: result.release_fingerprint,
                APPLICATION_RELEASE_FINGERPRINT_ENV: APPLICATION_RELEASE_FINGERPRINT,
            },
            bundle_root=result.deployment_root,
        )

    _archive_source, _archive_dependencies, archive_result = _sealed(tmp_path, "archive-tamper")
    archive = bytearray(archive_result.archive_path.read_bytes())
    archive[-1] ^= 1
    archive_result.archive_path.write_bytes(archive)
    with pytest.raises(Phase76GuardBundleError):
        verify_guard_deployment_artifact(
            archive_result.deployment_root,
            archive_path=archive_result.archive_path,
            descriptor_path=archive_result.descriptor_path,
        )


def test_descriptor_has_no_host_path_or_provider_authority(tmp_path: Path) -> None:
    _source_root, _dependencies_root, result = _sealed(tmp_path, "descriptor")
    raw = result.descriptor_path.read_text(encoding="utf-8")
    descriptor = json.loads(raw)

    assert str(tmp_path) not in raw
    assert "printify" not in raw.casefold()
    assert "secret" not in raw.casefold()
    assert descriptor["runtime"] == "python3.12"
    assert descriptor["architecture"] == "arm64"


def test_read_only_aws_captures_bind_the_deployed_guard_and_sanitized_invocations(
    tmp_path: Path,
) -> None:
    _source_root, _dependencies_root, result = _sealed(tmp_path, "aws-captures")
    descriptor = dict(
        verify_guard_deployment_artifact(
            result.deployment_root,
            archive_path=result.archive_path,
            descriptor_path=result.descriptor_path,
        )
    )
    environment = "dev"
    region = "us-west-2"
    account = "123456789012"
    stack_name = "mr-lister-phase7-guard-dev"
    function_name = "mr-lister-phase7-dev-guard-verification"
    bucket = "mr-lister-phase7-artifacts-dev"
    key = f"phase7/releases/{result.release_fingerprint}/guard.zip"
    version = "v1.token-2"
    raw_archive = result.archive_path.read_bytes()
    archive_sha256 = sha256(raw_archive).hexdigest()
    head = {
        "ChecksumSHA256": base64.b64encode(sha256(raw_archive).digest()).decode("ascii"),
        "ContentLength": len(raw_archive),
        "ContentType": "application/zip",
        "Metadata": {
            "mr-lister-archive-sha256": archive_sha256,
            "mr-lister-release-fingerprint": result.release_fingerprint,
        },
        "ServerSideEncryption": "AES256",
        "VersionId": version,
    }
    verify_s3_head_observation(
        descriptor,
        head,
        archive_path=result.archive_path,
        bucket=bucket,
        key=key,
        version_id=version,
    )

    parameters = {
        "ApplicationReleaseFingerprint": APPLICATION_RELEASE_FINGERPRINT,
        "EnvironmentName": environment,
        "GuardCodeS3Bucket": bucket,
        "GuardCodeS3Key": key,
        "GuardCodeS3ObjectVersion": version,
        "GuardReleaseFingerprint": result.release_fingerprint,
    }
    function_arn = f"arn:aws:lambda:{region}:{account}:function:{function_name}"
    outputs = {
        "DeploymentReadiness": "READ_ONLY_GUARD",
        "PublicationGuardExternalCallsEnabled": "false",
        "PublicationGuardVerificationEnabled": "true",
        "PublicationGuardVerificationFunctionArn": function_arn,
        "PublicationEnabled": "false",
        "PublicationRequestEnabled": "false",
        "PublicationStatusAlarmTopicArn": (
            f"arn:aws:sns:{region}:{account}:mr-lister-phase7-dev-publication-status-alarms"
        ),
        "PublicationStatusQueryEnabled": "false",
        "PublicationStatusQueryRegistered": "false",
    }
    stack = {
        "Stacks": [
            {
                "Outputs": [
                    {"OutputKey": name, "OutputValue": value} for name, value in outputs.items()
                ],
                "Parameters": [
                    {"ParameterKey": name, "ParameterValue": value}
                    for name, value in parameters.items()
                ],
                "StackId": (
                    f"arn:aws:cloudformation:{region}:{account}:stack/{stack_name}/stack-id"
                ),
                "StackName": stack_name,
                "StackStatus": "UPDATE_COMPLETE",
            }
        ]
    }
    verify_stack_observation(
        descriptor,
        stack,
        stack_name=stack_name,
        environment_name=environment,
        bucket=bucket,
        key=key,
        version_id=version,
        region=region,
        account_id=account,
    )
    phase6_stack_name = f"mr-lister-phase6-{environment}"
    phase6_stack = {
        "Stacks": [
            {
                "Parameters": [
                    {"ParameterKey": "EnvironmentName", "ParameterValue": environment},
                    {
                        "ParameterKey": "ReleaseFingerprint",
                        "ParameterValue": APPLICATION_RELEASE_FINGERPRINT,
                    },
                ],
                "StackId": (
                    f"arn:aws:cloudformation:{region}:{account}:stack/{phase6_stack_name}/stack-id"
                ),
                "StackName": phase6_stack_name,
                "StackStatus": "UPDATE_COMPLETE",
            }
        ]
    }
    verify_phase6_application_release_observation(
        descriptor,
        phase6_stack,
        environment_name=environment,
        region=region,
        account_id=account,
    )
    drifted_phase6_stack = json.loads(json.dumps(phase6_stack))
    drifted_phase6_stack["Stacks"][0]["Parameters"][1]["ParameterValue"] = "d" * 64
    with pytest.raises(ValueError, match="Phase 6 application release observation"):
        verify_phase6_application_release_observation(
            descriptor,
            drifted_phase6_stack,
            environment_name=environment,
            region=region,
            account_id=account,
        )
    with pytest.raises(ValueError, match="CloudFormation observation"):
        verify_stack_observation(
            descriptor,
            stack,
            stack_name="mr-lister-phase7-dev",
            environment_name=environment,
            bucket=bucket,
            key=key,
            version_id=version,
            region=region,
            account_id=account,
        )
    stack_resources = {
        "StackResourceSummaries": [
            {
                "LogicalResourceId": "PublicationGuardVerificationFunctionRole",
                "PhysicalResourceId": f"{function_name}-role",
                "ResourceStatus": "UPDATE_COMPLETE",
                "ResourceType": "AWS::IAM::Role",
            },
            {
                "LogicalResourceId": "PublicationGuardVerificationLogGroup",
                "PhysicalResourceId": f"/aws/lambda/{function_name}",
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::Logs::LogGroup",
            },
            {
                "LogicalResourceId": "PublicationGuardVerificationFunction",
                "PhysicalResourceId": function_name,
                "ResourceStatus": "UPDATE_COMPLETE",
                "ResourceType": "AWS::Lambda::Function",
            },
            {
                "LogicalResourceId": "PublicationStatusAlarmTopicKey",
                "PhysicalResourceId": "11111111-2222-3333-4444-555555555555",
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::KMS::Key",
            },
            {
                "LogicalResourceId": "PublicationStatusAlarmTopic",
                "PhysicalResourceId": (
                    f"arn:aws:sns:{region}:{account}:mr-lister-phase7-dev-publication-status-alarms"
                ),
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::SNS::Topic",
            },
            {
                "LogicalResourceId": "PublicationStatusAlarmTopicPolicy",
                "PhysicalResourceId": "mr-lister-phase7-dev-topic-policy",
                "ResourceStatus": "UPDATE_COMPLETE",
                "ResourceType": "AWS::SNS::TopicPolicy",
            },
            {
                "LogicalResourceId": "PublicationGuardVerificationErrorsAlarm",
                "PhysicalResourceId": ("mr-lister-phase7-dev-publication-status-guard-errors"),
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::CloudWatch::Alarm",
            },
            {
                "LogicalResourceId": "PublicationGuardVerificationThrottlesAlarm",
                "PhysicalResourceId": ("mr-lister-phase7-dev-publication-status-guard-throttles"),
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::CloudWatch::Alarm",
            },
            {
                "LogicalResourceId": "PublicationGuardVerificationDurationAlarm",
                "PhysicalResourceId": ("mr-lister-phase7-dev-publication-status-guard-duration"),
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::CloudWatch::Alarm",
            },
        ]
    }
    verify_stack_resources_observation(
        stack_resources,
        environment_name=environment,
        region=region,
        account_id=account,
    )
    drifted_stack_resources = json.loads(json.dumps(stack_resources))
    drifted_stack_resources["StackResourceSummaries"].append(
        {
            "LogicalResourceId": "PublicationStatusQueryFunction",
            "PhysicalResourceId": "mr-lister-phase7-dev-publication-status-query",
            "ResourceStatus": "CREATE_COMPLETE",
            "ResourceType": "AWS::Lambda::Function",
        }
    )
    with pytest.raises(ValueError, match="stack resource observation"):
        verify_stack_resources_observation(
            drifted_stack_resources,
            environment_name=environment,
            region=region,
            account_id=account,
        )

    legacy_absence = {
        "function_name": "mr-lister-phase7-dev-publication-status-query",
        "get_function": {
            "error_code": "ResourceNotFoundException",
            "http_status_code": 404,
        },
        "get_role": {"error_code": "NoSuchEntity", "http_status_code": 404},
        "role_name": "mr-lister-phase7-dev-publication-status-query-role",
    }
    verify_legacy_query_absence_observations(
        legacy_absence,
        {"CompositeAlarms": [], "LogAlarms": [], "MetricAlarms": []},
        {"logGroups": []},
        environment_name=environment,
    )
    drifted_legacy_absence = json.loads(json.dumps(legacy_absence))
    drifted_legacy_absence["get_role"] = {"error_code": "Success", "http_status_code": 200}
    with pytest.raises(ValueError, match="legacy query absence observation"):
        verify_legacy_query_absence_observations(
            drifted_legacy_absence,
            {"CompositeAlarms": [], "MetricAlarms": []},
            {"logGroups": []},
            environment_name=environment,
        )
    with pytest.raises(ValueError, match="legacy query absence observation"):
        verify_legacy_query_absence_observations(
            legacy_absence,
            {"CompositeAlarms": [], "MetricAlarms": [{"AlarmName": "legacy"}]},
            {"logGroups": []},
            environment_name=environment,
        )
    with pytest.raises(ValueError, match="legacy query absence observation"):
        verify_legacy_query_absence_observations(
            legacy_absence,
            {"CompositeAlarms": [], "LogAlarms": [{"AlarmName": "legacy"}], "MetricAlarms": []},
            {"logGroups": []},
            environment_name=environment,
        )
    with pytest.raises(ValueError, match="legacy query absence observation"):
        verify_legacy_query_absence_observations(
            legacy_absence,
            {"CompositeAlarms": [], "MetricAlarms": []},
            {"logGroups": [{"logGroupName": "/aws/lambda/legacy"}]},
            environment_name=environment,
        )

    variables = {
        "MR_LISTER_AWS_ACCOUNT_ID": account,
        "MR_LISTER_ENVIRONMENT": environment,
        "MR_LISTER_PHASE7_GUARD_ENABLED": "true",
        "MR_LISTER_PHASE7_GUARD_MODE": "approval_version_read_only",
        "MR_LISTER_PHASE7_GUARD_RELEASE_FINGERPRINT": result.release_fingerprint,
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": result.profile_fingerprint,
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_RELEASE_FINGERPRINT": APPLICATION_RELEASE_FINGERPRINT,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
    }
    configuration = {
        "Architectures": ["arm64"],
        "CodeSha256": base64.b64encode(sha256(raw_archive).digest()).decode("ascii"),
        "CodeSize": len(raw_archive),
        "Environment": {"Variables": variables},
        "FunctionArn": function_arn,
        "FunctionName": function_name,
        "Handler": (
            "mr_lister.cloud.phase7_guard_entrypoint.publication_guard_verification_handler"
        ),
        "LastUpdateStatus": "Successful",
        "LoggingConfig": {
            "ApplicationLogLevel": "ERROR",
            "LogFormat": "JSON",
            "LogGroup": f"/aws/lambda/{function_name}",
            "SystemLogLevel": "WARN",
        },
        "MemorySize": 512,
        "PackageType": "Zip",
        "Role": f"arn:aws:iam::{account}:role/{function_name}-role",
        "Runtime": "python3.12",
        "State": "Active",
        "Timeout": 30,
        "Version": "$LATEST",
    }
    verify_lambda_configuration_observation(
        descriptor,
        configuration,
        {"ReservedConcurrentExecutions": 1},
        archive_path=result.archive_path,
        environment_name=environment,
        region=region,
        account_id=account,
    )

    role_name = f"{function_name}-role"
    role = {
        "Role": {
            "Arn": f"arn:aws:iam::{account}:role/{role_name}",
            "AssumeRolePolicyDocument": {
                "Statement": [
                    {
                        "Action": "sts:AssumeRole",
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                    }
                ],
                "Version": "2012-10-17",
            },
            "CreateDate": "2026-08-24T05:00:00+00:00",
            "Description": "",
            "MaxSessionDuration": 3600,
            "Path": "/",
            "RoleId": "A" * 20,
            "RoleName": role_name,
            "Tags": [
                {"Key": "Project", "Value": "MrLister"},
                {"Key": "Environment", "Value": environment},
                {"Key": "Phase", "Value": "7.6-read-only-guard"},
            ],
        }
    }
    inline_policy = {
        "PolicyDocument": {
            "Statement": [
                {
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Effect": "Allow",
                    "Resource": (
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{function_name}:*:*"
                    ),
                    "Sid": "WritePublicationGuardLogs",
                },
                {
                    "Action": ["dynamodb:GetItem", "dynamodb:Query"],
                    "Condition": {
                        "ForAllValues:StringLike": {
                            "dynamodb:LeadingKeys": ["JOB#*", "PUBLICATION#*"]
                        }
                    },
                    "Effect": "Allow",
                    "Resource": (f"arn:aws:dynamodb:{region}:{account}:table/mr-lister-phase6-dev"),
                    "Sid": "ReadExactApprovalPublicationAuthority",
                },
            ],
            "Version": "2012-10-17",
        },
        "PolicyName": "ReadOnlyApprovalPublicationGuard",
        "RoleName": role_name,
    }
    verify_iam_role_observations(
        role,
        inline_policy,
        {"IsTruncated": False, "PolicyNames": ["ReadOnlyApprovalPublicationGuard"]},
        {"AttachedPolicies": [], "IsTruncated": False},
        environment_name=environment,
        region=region,
        account_id=account,
    )
    with pytest.raises(ValueError, match="IAM role observation"):
        verify_iam_role_observations(
            role,
            inline_policy,
            {"IsTruncated": False, "PolicyNames": ["ReadOnlyApprovalPublicationGuard"]},
            {
                "AttachedPolicies": [
                    {
                        "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess",
                        "PolicyName": "AdministratorAccess",
                    }
                ],
                "IsTruncated": False,
            },
            environment_name=environment,
            region=region,
            account_id=account,
        )
    drifted_role = json.loads(json.dumps(role))
    drifted_role["Role"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] = {
        "AWS": f"arn:aws:iam::{account}:root"
    }
    with pytest.raises(ValueError, match="IAM role observation"):
        verify_iam_role_observations(
            drifted_role,
            inline_policy,
            {"IsTruncated": False, "PolicyNames": ["ReadOnlyApprovalPublicationGuard"]},
            {"AttachedPolicies": [], "IsTruncated": False},
            environment_name=environment,
            region=region,
            account_id=account,
        )
    described_role = json.loads(json.dumps(role))
    described_role["Role"]["Description"] = "unexpected"
    with pytest.raises(ValueError, match="IAM role observation"):
        verify_iam_role_observations(
            described_role,
            inline_policy,
            {"IsTruncated": False, "PolicyNames": ["ReadOnlyApprovalPublicationGuard"]},
            {"AttachedPolicies": [], "IsTruncated": False},
            environment_name=environment,
            region=region,
            account_id=account,
        )
    boundary_role = json.loads(json.dumps(role))
    boundary_role["Role"]["PermissionsBoundary"] = {
        "PermissionsBoundaryArn": f"arn:aws:iam::{account}:policy/unreviewed",
        "PermissionsBoundaryType": "Policy",
    }
    with pytest.raises(ValueError, match="IAM role observation"):
        verify_iam_role_observations(
            boundary_role,
            inline_policy,
            {"IsTruncated": False, "PolicyNames": ["ReadOnlyApprovalPublicationGuard"]},
            {"AttachedPolicies": [], "IsTruncated": False},
            environment_name=environment,
            region=region,
            account_id=account,
        )
    verify_lambda_surface_absence_observations(
        {"EventSourceMappings": []},
        {"Versions": [{"FunctionName": function_name, "Version": "$LATEST"}]},
        {"Aliases": []},
        {"FunctionUrlConfigs": []},
        {
            "function_name": function_name,
            "get_function_event_invoke_config": {
                "error_code": "ResourceNotFoundException",
                "http_status_code": 404,
            },
            "get_function_url_config": {
                "error_code": "ResourceNotFoundException",
                "http_status_code": 404,
            },
            "get_policy": {
                "error_code": "ResourceNotFoundException",
                "http_status_code": 404,
            },
        },
        environment_name=environment,
    )
    with pytest.raises(ValueError, match="Lambda surface observation"):
        verify_lambda_surface_absence_observations(
            {"EventSourceMappings": []},
            {"Versions": [{"FunctionName": function_name, "Version": "$LATEST"}]},
            {"Aliases": []},
            {"FunctionUrlConfigs": []},
            {
                "function_name": function_name,
                "get_function_event_invoke_config": {
                    "error_code": "Success",
                    "http_status_code": 200,
                },
                "get_function_url_config": {
                    "error_code": "ResourceNotFoundException",
                    "http_status_code": 404,
                },
                "get_policy": {
                    "error_code": "ResourceNotFoundException",
                    "http_status_code": 404,
                },
            },
            environment_name=environment,
        )
    with pytest.raises(ValueError, match="Lambda surface observation"):
        verify_lambda_surface_absence_observations(
            {"EventSourceMappings": [{"UUID": "unexpected-trigger"}]},
            {
                "Versions": [
                    {"FunctionName": function_name, "Version": "$LATEST"},
                    {"FunctionName": function_name, "Version": "1"},
                ]
            },
            {"Aliases": [{"FunctionVersion": "1", "Name": "public"}]},
            {"FunctionUrlConfigs": [{"AuthType": "NONE", "FunctionArn": function_name}]},
            {
                "function_name": function_name,
                "get_function_event_invoke_config": {
                    "error_code": "ResourceNotFoundException",
                    "http_status_code": 404,
                },
                "get_function_url_config": {
                    "error_code": "ResourceNotFoundException",
                    "http_status_code": 404,
                },
                "get_policy": {
                    "error_code": "ResourceNotFoundException",
                    "http_status_code": 404,
                },
            },
            environment_name=environment,
        )

    invocation = {"ExecutedVersion": "$LATEST", "StatusCode": 200}
    verify_lambda_invocation_observation(
        descriptor,
        {"operation": "status"},
        invocation,
        _attestation(
            descriptor,
            operation="status",
            outcome="sealed_configuration",
            approval_authority_current=None,
        ),
        expected_outcome="sealed_configuration",
    )
    verify_lambda_invocation_observation(
        descriptor,
        {
            "aggregate_id": "phase76_missing_authority_smoke",
            "operation": "verify_authority",
            "owner_id": "d" * 64,
        },
        invocation,
        _attestation(
            descriptor,
            operation="verify_authority",
            outcome="authority_rejected",
            approval_authority_current=False,
        ),
        expected_outcome="authority_rejected",
    )


def test_deployment_captures_reject_mutable_s3_and_unsanitized_lambda_results(
    tmp_path: Path,
) -> None:
    _source_root, _dependencies_root, result = _sealed(tmp_path, "bad-captures")
    descriptor = dict(
        verify_guard_deployment_artifact(
            result.deployment_root,
            archive_path=result.archive_path,
            descriptor_path=result.descriptor_path,
        )
    )
    raw_archive = result.archive_path.read_bytes()
    head = {
        "ChecksumSHA256": base64.b64encode(sha256(raw_archive).digest()).decode("ascii"),
        "ContentLength": len(raw_archive),
        "ContentType": "application/zip",
        "Metadata": {
            "mr-lister-archive-sha256": sha256(raw_archive).hexdigest(),
            "mr-lister-release-fingerprint": result.release_fingerprint,
        },
        "ServerSideEncryption": "AES256",
        "VersionId": "null",
    }
    with pytest.raises(ValueError, match="S3 object observation"):
        verify_s3_head_observation(
            descriptor,
            head,
            archive_path=result.archive_path,
            bucket="mr-lister-phase7-artifacts-dev",
            key=f"phase7/releases/{result.release_fingerprint}/guard.zip",
            version_id="null",
        )

    payload = json.loads(
        _attestation(
            descriptor,
            operation="status",
            outcome="sealed_configuration",
            approval_authority_current=None,
        )
    )
    payload["owner_id"] = "d" * 64
    with pytest.raises(ValueError, match="invocation observation"):
        verify_lambda_invocation_observation(
            descriptor,
            {"operation": "status"},
            {"ExecutedVersion": "$LATEST", "FunctionError": "Unhandled", "StatusCode": 200},
            json.dumps(payload).encode(),
            expected_outcome="sealed_configuration",
        )
