"""Prepare one deterministic Phase 7.18 seller-web release manifest.

The tool is deliberately local-only.  It validates an already-built ``web/dist`` tree, the
sealed enabled-Lambda descriptor, and the existing public runtime-config upload authority.  It
then emits exact S3 object metadata for a static-only update of the existing private web bucket.
It does not build, invoke Git, import an AWS SDK, upload, invalidate CloudFront, or modify the
Phase 6 stack.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import stat
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Final, cast

from mr_lister.release.phase6 import render_manifest
from mr_lister.release.phase718 import (
    PHASE718_CONTRACT_FINGERPRINT,
    PHASE718_CONTRACT_PATH,
    PHASE718_CONTRACT_VERSION,
)

ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = Path(".mr_lister_private")
WEB_DIST_PATH: Final = Path("web/dist")
WEB_BUCKET: Final = "mr-lister-phase6-web-dev-384627057108-us-west-2"
WEB_DISTRIBUTION_ID: Final = "EXC2KQ0RRVWF0"
WEB_APPLICATION_ORIGIN: Final = "https://massskutiny.com"
WEB_STACK_NAME: Final = "mr-lister-phase6-dev"
WEB_RELEASE_FORMAT: Final = "mr-lister-phase7.18-web-release-v1"
RUNTIME_CONFIG_UPLOAD_FORMAT: Final = "mr-lister-phase6-runtime-config-upload-v1"
ENABLED_DESCRIPTOR_FORMAT: Final = "phase718-enabled-deployment-descriptor-v1"
SERVER_SIDE_ENCRYPTION: Final = "AES256"

_ASSET = re.compile(r"^assets/index-[A-Za-z0-9_-]{8}\.(css|js)$")
_COMMIT = re.compile(r"^[a-f0-9]{40}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_GENERIC_ERROR = "Phase 7.18 web release input is invalid"
_MAX_FILE_BYTES = 4 * 1024 * 1024
_EXPECTED_DIRECTORIES = frozenset({"assets"})
_EXPECTED_DESCRIPTOR_KEYS = frozenset(
    {
        "algorithm",
        "application_release_fingerprint",
        "archive",
        "architecture",
        "binding_sha256",
        "canary_evidence_fingerprint",
        "component",
        "contract_fingerprint",
        "deployment_manifest_sha256",
        "enabled_template_sha256",
        "enablement_evidence_fingerprint",
        "entrypoints",
        "format",
        "profile_fingerprint",
        "release_fingerprint",
        "runtime",
        "s3_binding",
        "source_manifest_sha256",
        "state_table",
    }
)
_RUNTIME_CONFIG_KEYS = frozenset(
    {
        "algorithm",
        "cache_control",
        "content_type",
        "format",
        "object_key",
        "sha256",
        "size_bytes",
    }
)


class Phase718WebReleaseError(RuntimeError):
    """Value-free refusal for unsafe, incomplete, or drifting release input."""


def render_phase718_web_release_manifest(
    dist_directory: Path,
    *,
    enabled_descriptor_path: Path,
    runtime_config_upload_manifest_path: Path,
    source_commit: str,
    repository_root: Path = ROOT,
) -> bytes:
    """Return canonical static-release authority for one exact distribution tree."""

    try:
        repository = _repository(repository_root)
        dist = _required_file_or_directory(
            dist_directory,
            repository=repository,
            directory=True,
        )
        if dist != repository / WEB_DIST_PATH:
            raise ValueError
        if _COMMIT.fullmatch(source_commit) is None or source_commit == "0" * 40:
            raise ValueError

        descriptor_raw, descriptor = _canonical_mapping(
            enabled_descriptor_path,
            repository=repository,
            renderer=render_manifest,
        )
        enabled = _enabled_runtime_authority(descriptor, descriptor_raw=descriptor_raw)
        runtime_raw, runtime = _canonical_mapping(
            runtime_config_upload_manifest_path,
            repository=repository,
            renderer=_compact_json,
        )
        runtime_authority = _runtime_config_authority(runtime, manifest_raw=runtime_raw)

        paths = _dist_inventory(dist)
        records: list[dict[str, object]] = []
        bundle = sha256()
        for relative in _upload_order(paths):
            raw = paths[relative].read_bytes()
            digest = sha256(raw).hexdigest()
            relative_raw = relative.encode("utf-8")
            bundle.update(len(relative_raw).to_bytes(4, "big"))
            bundle.update(relative_raw)
            bundle.update(len(raw).to_bytes(8, "big"))
            bundle.update(bytes.fromhex(digest))
            content_type, cache_control = _object_headers(relative)
            records.append(
                {
                    "cache_control": cache_control,
                    "checksum_sha256_base64": base64.b64encode(bytes.fromhex(digest)).decode(
                        "ascii"
                    ),
                    "content_type": content_type,
                    "key": relative,
                    "sha256": digest,
                    "size_bytes": len(raw),
                    "source_path": paths[relative].relative_to(repository).as_posix(),
                }
            )
        bundle_fingerprint = bundle.hexdigest()
        metadata = {
            "mr-lister-contract-version": PHASE718_CONTRACT_VERSION,
            "mr-lister-enabled-release-fingerprint": enabled["release_fingerprint"],
            "mr-lister-web-bundle-sha256": bundle_fingerprint,
        }
        objects = [{**record, "metadata": metadata} for record in records]
        authority: dict[str, object] = {
            "algorithm": "sha256",
            "application_origin": WEB_APPLICATION_ORIGIN,
            "browser_bundle_sha256": bundle_fingerprint,
            "bucket": WEB_BUCKET,
            "checksum_algorithm": "SHA256",
            "contract": {
                "path": PHASE718_CONTRACT_PATH,
                "sha256": PHASE718_CONTRACT_FINGERPRINT,
                "version": PHASE718_CONTRACT_VERSION,
            },
            "deployment_scope": "static_objects_only_phase6_stack_unchanged",
            "distribution_id": WEB_DISTRIBUTION_ID,
            "enabled_runtime": enabled,
            "file_count": len(objects),
            "format": WEB_RELEASE_FORMAT,
            "objects": objects,
            "runtime_config": runtime_authority,
            "server_side_encryption": SERVER_SIDE_ENCRYPTION,
            "source_commit": source_commit,
            "stack_name": WEB_STACK_NAME,
            "upload_order": [record["key"] for record in objects],
            "versioning_required": True,
        }
        return _with_aggregate(authority)
    except Phase718WebReleaseError:
        raise
    except Exception:
        raise Phase718WebReleaseError(_GENERIC_ERROR) from None


def load_phase718_web_release_manifest(
    manifest_path: Path,
    *,
    repository_root: Path = ROOT,
) -> tuple[bytes, Mapping[str, object]]:
    """Load and validate one canonical manifest, including every current source byte."""

    try:
        repository = _repository(repository_root)
        raw, value = _canonical_mapping(
            manifest_path,
            repository=repository,
            renderer=render_manifest,
        )
        aggregate = value.get("aggregate_sha256")
        authority = {key: item for key, item in value.items() if key != "aggregate_sha256"}
        if (
            not isinstance(aggregate, str)
            or aggregate != sha256(render_manifest(authority)).hexdigest()
            or render_manifest(value) != raw
        ):
            raise ValueError
        _validate_rendered_manifest(value, repository=repository)
        return raw, value
    except Phase718WebReleaseError:
        raise
    except Exception:
        raise Phase718WebReleaseError(_GENERIC_ERROR) from None


def write_phase718_web_release_manifest(
    dist_directory: Path,
    destination: Path,
    *,
    enabled_descriptor_path: Path,
    runtime_config_upload_manifest_path: Path,
    source_commit: str,
    repository_root: Path = ROOT,
) -> Path:
    """Create one mode-0600 private manifest and verify its bytes after writing."""

    try:
        repository = _repository(repository_root)
        raw = render_phase718_web_release_manifest(
            dist_directory,
            enabled_descriptor_path=enabled_descriptor_path,
            runtime_config_upload_manifest_path=runtime_config_upload_manifest_path,
            source_commit=source_commit,
            repository_root=repository,
        )
        target = _private_destination(destination, repository=repository)
        _prepare_private_directory(repository, target.parent)
        with target.open("xb") as stream:
            stream.write(raw)
        target.chmod(0o600)
        if target.read_bytes() != raw or stat.S_IMODE(target.stat().st_mode) != 0o600:
            raise ValueError
        load_phase718_web_release_manifest(target, repository_root=repository)
        return target
    except Phase718WebReleaseError:
        raise
    except Exception:
        raise Phase718WebReleaseError(_GENERIC_ERROR) from None


def _validate_rendered_manifest(value: Mapping[str, object], *, repository: Path) -> None:
    expected_keys = {
        "aggregate_sha256",
        "algorithm",
        "application_origin",
        "browser_bundle_sha256",
        "bucket",
        "checksum_algorithm",
        "contract",
        "deployment_scope",
        "distribution_id",
        "enabled_runtime",
        "file_count",
        "format",
        "objects",
        "runtime_config",
        "server_side_encryption",
        "source_commit",
        "stack_name",
        "upload_order",
        "versioning_required",
    }
    if set(value) != expected_keys:
        raise ValueError
    source_commit = value.get("source_commit")
    bundle = value.get("browser_bundle_sha256")
    objects = value.get("objects")
    if (
        value.get("algorithm") != "sha256"
        or value.get("application_origin") != WEB_APPLICATION_ORIGIN
        or not isinstance(bundle, str)
        or _FINGERPRINT.fullmatch(bundle) is None
        or value.get("bucket") != WEB_BUCKET
        or value.get("checksum_algorithm") != "SHA256"
        or value.get("contract")
        != {
            "path": PHASE718_CONTRACT_PATH,
            "sha256": PHASE718_CONTRACT_FINGERPRINT,
            "version": PHASE718_CONTRACT_VERSION,
        }
        or value.get("deployment_scope") != "static_objects_only_phase6_stack_unchanged"
        or value.get("distribution_id") != WEB_DISTRIBUTION_ID
        or value.get("file_count") != 4
        or value.get("format") != WEB_RELEASE_FORMAT
        or value.get("server_side_encryption") != SERVER_SIDE_ENCRYPTION
        or not isinstance(source_commit, str)
        or _COMMIT.fullmatch(source_commit) is None
        or source_commit == "0" * 40
        or value.get("versioning_required") is not True
        or value.get("stack_name") != WEB_STACK_NAME
        or not isinstance(objects, list)
        or len(objects) != 4
    ):
        raise ValueError
    enabled = value.get("enabled_runtime")
    runtime = value.get("runtime_config")
    if (
        not isinstance(enabled, Mapping)
        or set(enabled)
        != {
            "application_release_fingerprint",
            "canary_evidence_fingerprint",
            "contract_fingerprint",
            "descriptor_sha256",
            "enabled_template_sha256",
            "enablement_evidence_fingerprint",
            "release_fingerprint",
        }
        or enabled.get("contract_fingerprint") != PHASE718_CONTRACT_FINGERPRINT
        or any(
            not isinstance(enabled.get(field), str)
            or _FINGERPRINT.fullmatch(cast(str, enabled[field])) is None
            or enabled[field] == "0" * 64
            for field in enabled
        )
        or not isinstance(runtime, Mapping)
        or set(runtime)
        != {
            "cache_control",
            "content_type",
            "manifest_sha256",
            "object_key",
            "preserve_existing",
            "sha256",
            "size_bytes",
        }
        or runtime.get("cache_control") != "private, no-store, max-age=0"
        or runtime.get("content_type") != "application/json"
        or runtime.get("object_key") != "runtime-config.json"
        or runtime.get("preserve_existing") is not True
        or any(
            not isinstance(runtime.get(field), str)
            or _FINGERPRINT.fullmatch(cast(str, runtime[field])) is None
            or runtime[field] == "0" * 64
            for field in ("manifest_sha256", "sha256")
        )
        or not isinstance(runtime.get("size_bytes"), int)
        or not 1 <= cast(int, runtime["size_bytes"]) <= 16_384
    ):
        raise ValueError
    metadata = {
        "mr-lister-contract-version": PHASE718_CONTRACT_VERSION,
        "mr-lister-enabled-release-fingerprint": enabled.get("release_fingerprint"),
        "mr-lister-web-bundle-sha256": bundle,
    }
    bundle_digest = sha256()
    keys: list[str] = []
    for record in objects:
        if not isinstance(record, Mapping) or set(record) != {
            "cache_control",
            "checksum_sha256_base64",
            "content_type",
            "key",
            "metadata",
            "sha256",
            "size_bytes",
            "source_path",
        }:
            raise ValueError
        key = record.get("key")
        source_path = record.get("source_path")
        digest = record.get("sha256")
        size = record.get("size_bytes")
        if (
            not isinstance(key, str)
            or not isinstance(source_path, str)
            or source_path != f"web/dist/{key}"
            or not isinstance(digest, str)
            or _FINGERPRINT.fullmatch(digest) is None
            or not isinstance(size, int)
            or not 1 <= size <= _MAX_FILE_BYTES
            or record.get("metadata") != metadata
            or (record.get("content_type"), record.get("cache_control")) != _object_headers(key)
            or record.get("checksum_sha256_base64")
            != base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        ):
            raise ValueError
        source = _required_file_or_directory(
            repository / source_path,
            repository=repository,
            directory=False,
        )
        raw = source.read_bytes()
        if len(raw) != size or sha256(raw).hexdigest() != digest:
            raise ValueError
        relative_raw = key.encode("utf-8")
        bundle_digest.update(len(relative_raw).to_bytes(4, "big"))
        bundle_digest.update(relative_raw)
        bundle_digest.update(len(raw).to_bytes(8, "big"))
        bundle_digest.update(bytes.fromhex(digest))
        keys.append(key)
    if keys != _upload_order({key: repository / f"web/dist/{key}" for key in keys}):
        raise ValueError
    if value.get("upload_order") != keys or bundle_digest.hexdigest() != bundle:
        raise ValueError


def _enabled_runtime_authority(
    descriptor: Mapping[str, object],
    *,
    descriptor_raw: bytes,
) -> dict[str, object]:
    if set(descriptor) != _EXPECTED_DESCRIPTOR_KEYS:
        raise ValueError
    fingerprints = (
        "application_release_fingerprint",
        "canary_evidence_fingerprint",
        "contract_fingerprint",
        "enabled_template_sha256",
        "enablement_evidence_fingerprint",
        "release_fingerprint",
    )
    if (
        descriptor.get("algorithm") != "sha256"
        or descriptor.get("architecture") != "arm64"
        or descriptor.get("component") != "phase718-enabled-lambda"
        or descriptor.get("contract_fingerprint") != PHASE718_CONTRACT_FINGERPRINT
        or descriptor.get("format") != ENABLED_DESCRIPTOR_FORMAT
        or descriptor.get("runtime") != "python3.12"
        or descriptor.get("state_table") != "mr-lister-phase6-dev"
        or any(
            not isinstance(descriptor.get(field), str)
            or _FINGERPRINT.fullmatch(cast(str, descriptor[field])) is None
            or descriptor[field] == "0" * 64
            for field in fingerprints
        )
    ):
        raise ValueError
    return {
        "application_release_fingerprint": descriptor["application_release_fingerprint"],
        "canary_evidence_fingerprint": descriptor["canary_evidence_fingerprint"],
        "contract_fingerprint": descriptor["contract_fingerprint"],
        "descriptor_sha256": sha256(descriptor_raw).hexdigest(),
        "enabled_template_sha256": descriptor["enabled_template_sha256"],
        "enablement_evidence_fingerprint": descriptor["enablement_evidence_fingerprint"],
        "release_fingerprint": descriptor["release_fingerprint"],
    }


def _runtime_config_authority(
    value: Mapping[str, object],
    *,
    manifest_raw: bytes,
) -> dict[str, object]:
    if (
        set(value) != _RUNTIME_CONFIG_KEYS
        or value.get("algorithm") != "sha256"
        or value.get("cache_control") != "private, no-store, max-age=0"
        or value.get("content_type") != "application/json"
        or value.get("format") != RUNTIME_CONFIG_UPLOAD_FORMAT
        or value.get("object_key") != "runtime-config.json"
        or not isinstance(value.get("sha256"), str)
        or _FINGERPRINT.fullmatch(cast(str, value["sha256"])) is None
        or value.get("sha256") == "0" * 64
        or not isinstance(value.get("size_bytes"), int)
        or not 1 <= cast(int, value["size_bytes"]) <= 16_384
    ):
        raise ValueError
    return {
        "cache_control": value["cache_control"],
        "content_type": value["content_type"],
        "manifest_sha256": sha256(manifest_raw).hexdigest(),
        "object_key": value["object_key"],
        "preserve_existing": True,
        "sha256": value["sha256"],
        "size_bytes": value["size_bytes"],
    }


def _dist_inventory(dist: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    directories: set[str] = set()
    for path in dist.rglob("*"):
        if path.is_symlink():
            raise ValueError
        relative = path.relative_to(dist).as_posix()
        if path.is_dir():
            directories.add(relative)
        elif path.is_file():
            if not 1 <= path.stat().st_size <= _MAX_FILE_BYTES:
                raise ValueError
            files[relative] = path
        else:
            raise ValueError
    assets = [key for key in files if _ASSET.fullmatch(key)]
    if (
        directories != _EXPECTED_DIRECTORIES
        or set(files) != {*assets, "favicon.svg", "index.html"}
        or len([key for key in assets if key.endswith(".css")]) != 1
        or len([key for key in assets if key.endswith(".js")]) != 1
    ):
        raise ValueError
    order = _upload_order(files)
    index = files["index.html"].read_text(encoding="utf-8")
    if (
        index.count(f"/{order[0]}") != 1
        or index.count(f"/{order[1]}") != 1
        or index.count("/favicon.svg") != 1
        or "runtime-config" in index
    ):
        raise ValueError
    javascript = files[order[1]].read_text(encoding="utf-8")
    for marker in (
        PHASE718_CONTRACT_VERSION,
        "/publication",
        "/publish",
        "data-phase7-publication-workspace",
        "publish_exact_approved_listing",
    ):
        if marker not in javascript:
            raise ValueError
    return files


def _upload_order(paths: Mapping[str, Path]) -> list[str]:
    css = sorted(key for key in paths if key.endswith(".css"))
    javascript = sorted(key for key in paths if key.endswith(".js"))
    if len(css) != 1 or len(javascript) != 1:
        raise ValueError
    return [css[0], javascript[0], "favicon.svg", "index.html"]


def _object_headers(key: str) -> tuple[str, str]:
    if _ASSET.fullmatch(key):
        content_type = (
            "text/css; charset=utf-8" if key.endswith(".css") else "text/javascript; charset=utf-8"
        )
        return content_type, "public, max-age=31536000, immutable"
    if key == "favicon.svg":
        return "image/svg+xml", "private, no-store, max-age=0"
    if key == "index.html":
        return "text/html; charset=utf-8", "private, no-store, max-age=0"
    raise ValueError


def _repository(root: Path) -> Path:
    repository = root.resolve(strict=True)
    if root.is_symlink() or not repository.is_dir():
        raise ValueError
    return repository


def _required_file_or_directory(
    path: Path,
    *,
    repository: Path,
    directory: bool,
) -> Path:
    candidate = path if path.is_absolute() else repository / path
    resolved = candidate.resolve(strict=True)
    if (
        candidate.is_symlink()
        or not resolved.is_relative_to(repository)
        or _path_has_symlink_component(repository, resolved)
        or (not resolved.is_dir() if directory else not resolved.is_file())
    ):
        raise ValueError
    return resolved


def _canonical_mapping(
    path: Path,
    *,
    repository: Path,
    renderer: Callable[[Mapping[str, object]], bytes],
) -> tuple[bytes, Mapping[str, object]]:
    source = _required_file_or_directory(path, repository=repository, directory=False)
    if not 1 <= source.stat().st_size <= _MAX_FILE_BYTES:
        raise ValueError
    raw = source.read_bytes()
    value = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(value, Mapping) or renderer(value) != raw:
        raise ValueError
    return raw, cast(Mapping[str, object], value)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _compact_json(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _with_aggregate(authority: Mapping[str, object]) -> bytes:
    manifest = {
        **authority,
        "aggregate_sha256": sha256(render_manifest(authority)).hexdigest(),
    }
    return render_manifest(manifest)


def _private_destination(path: Path, *, repository: Path) -> Path:
    private = repository / PRIVATE_ROOT
    candidate = path if path.is_absolute() else repository / path
    target = candidate.resolve(strict=False)
    if (
        target == private
        or not target.is_relative_to(private)
        or target.exists()
        or target.is_symlink()
    ):
        raise ValueError
    return target


def _prepare_private_directory(repository: Path, directory: Path) -> None:
    private = repository / PRIVATE_ROOT
    if not directory.is_relative_to(private):
        raise ValueError
    current = repository
    for component in directory.relative_to(repository).parts:
        current = current / component
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise ValueError
        else:
            current.mkdir(mode=0o700)


def _path_has_symlink_component(root: Path, path: Path) -> bool:
    current = root
    for component in path.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--enabled-descriptor", required=True, type=Path)
    parser.add_argument("--runtime-config-upload-manifest", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        written = write_phase718_web_release_manifest(
            arguments.dist,
            arguments.output,
            enabled_descriptor_path=arguments.enabled_descriptor,
            runtime_config_upload_manifest_path=arguments.runtime_config_upload_manifest,
            source_commit=arguments.source_commit,
        )
        print(written)
    except Phase718WebReleaseError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "ENABLED_DESCRIPTOR_FORMAT",
    "RUNTIME_CONFIG_UPLOAD_FORMAT",
    "SERVER_SIDE_ENCRYPTION",
    "WEB_BUCKET",
    "WEB_DISTRIBUTION_ID",
    "WEB_DIST_PATH",
    "WEB_APPLICATION_ORIGIN",
    "WEB_RELEASE_FORMAT",
    "WEB_STACK_NAME",
    "Phase718WebReleaseError",
    "load_phase718_web_release_manifest",
    "render_phase718_web_release_manifest",
    "write_phase718_web_release_manifest",
]


if __name__ == "__main__":
    main()
