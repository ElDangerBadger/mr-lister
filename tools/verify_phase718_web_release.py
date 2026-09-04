"""Capture and verify the versioned Phase 7.18 seller-web release without AWS access.

AWS operations stay outside this module.  An operator supplies canonical observations containing
the exact PutObject results and version-qualified HeadObject/body readbacks.  This tool preserves
the Phase 6 predecessor versions, verifies the Phase 7.18 candidate, emits the only versions that
may be deleted to roll back, and then proves that rollback exposed the exact predecessor versions.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import stat
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Final, cast

from mr_lister.release.phase6 import render_manifest
from mr_lister.release.phase718 import (
    PHASE718_CONTRACT_FINGERPRINT,
    PHASE718_CONTRACT_VERSION,
)
from tools.prepare_phase718_web_release import (
    PRIVATE_ROOT,
    SERVER_SIDE_ENCRYPTION,
    WEB_BUCKET,
    Phase718WebReleaseError,
    load_phase718_web_release_manifest,
)

ROOT: Final = Path(__file__).resolve().parents[1]
READBACK_OBSERVATION_FORMAT: Final = "mr-lister-phase7.18-web-readback-observation-v1"
ROLLBACK_MANIFEST_FORMAT: Final = "mr-lister-phase7.18-web-rollback-v1"
LIVE_VERIFICATION_FORMAT: Final = "mr-lister-phase7.18-web-live-verification-v1"
ROLLBACK_VERIFICATION_FORMAT: Final = "mr-lister-phase7.18-web-rollback-verification-v1"

_GENERIC_ERROR = "Phase 7.18 web deployment evidence is invalid"
_ASSET = re.compile(r"^assets/index-[A-Za-z0-9_-]{8}\.(css|js)$")
_FINGERPRINT = re.compile(r"^(?!0{64}$)[a-f0-9]{64}$")
_ETAG = re.compile(r'^"[a-f0-9]{32}"$')
_METADATA_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_MAX_FILE_BYTES = 4 * 1024 * 1024


class Phase718WebDeploymentEvidenceError(RuntimeError):
    """Value-free refusal for unsafe, incomplete, or drifting web evidence."""


def render_phase718_web_rollback_manifest(
    release_manifest_path: Path,
    predecessor_observation_path: Path,
    *,
    repository_root: Path = ROOT,
) -> bytes:
    """Preserve the exact currently deployed Phase 6 objects and VersionIds."""

    try:
        repository = _repository(repository_root)
        release_raw, release = load_phase718_web_release_manifest(
            release_manifest_path,
            repository_root=repository,
        )
        _observation_raw, observation = _load_observation(
            predecessor_observation_path,
            repository=repository,
        )
        if observation["put_results"] != []:
            raise ValueError
        records = _records_by_key(cast(list[object], observation["objects"]))
        _validate_predecessor(records, repository=repository)
        runtime = _mapping(release["runtime_config"])
        runtime_record = records["runtime-config.json"]
        if not _matches_runtime(runtime_record, runtime):
            raise ValueError

        predecessor_objects = [_preserved_record(records[key]) for key in sorted(records)]
        authority: dict[str, object] = {
            "algorithm": "sha256",
            "bucket": WEB_BUCKET,
            "candidate_release": _candidate_binding(release_raw, release),
            "captured_at": observation["captured_at"],
            "format": ROLLBACK_MANIFEST_FORMAT,
            "predecessor_objects": predecessor_objects,
            "rollback_action": {
                "delete_order": "reverse_candidate_upload_order",
                "method": "delete_exact_candidate_object_versions",
                "predecessor_version_ids_must_match_after_delete": True,
            },
            "runtime_config": {
                **runtime,
                "predecessor_version_id": runtime_record["version_id"],
                "source_path": runtime_record["body_path"],
            },
            "versioning_required": True,
        }
        return _with_aggregate(authority)
    except (Phase718WebReleaseError, Phase718WebDeploymentEvidenceError):
        raise Phase718WebDeploymentEvidenceError(_GENERIC_ERROR) from None
    except Exception:
        raise Phase718WebDeploymentEvidenceError(_GENERIC_ERROR) from None


def render_phase718_web_live_verification(
    release_manifest_path: Path,
    rollback_manifest_path: Path,
    live_observation_path: Path,
    *,
    repository_root: Path = ROOT,
) -> bytes:
    """Verify all candidate versions, unchanged runtime config, and index-last upload evidence."""

    try:
        repository = _repository(repository_root)
        release_raw, release = load_phase718_web_release_manifest(
            release_manifest_path,
            repository_root=repository,
        )
        rollback_raw, rollback = _load_rollback(rollback_manifest_path, repository=repository)
        candidate_binding = _candidate_binding(release_raw, release)
        if rollback["candidate_release"] != candidate_binding:
            raise ValueError
        _observation_raw, observation = _load_observation(
            live_observation_path,
            repository=repository,
        )
        observed = _records_by_key(cast(list[object], observation["objects"]))
        predecessor = _records_by_key(cast(list[object], rollback["predecessor_objects"]))
        candidate = _release_records(release)
        expected_keys = set(predecessor) | set(candidate)
        if set(observed) != expected_keys:
            raise ValueError

        upload_order = _string_list(release["upload_order"])
        put_results = observation["put_results"]
        if (
            not isinstance(put_results, list)
            or [_mapping(item).get("key") for item in put_results] != upload_order
        ):
            raise ValueError
        put_versions: dict[str, str] = {}
        for item in put_results:
            result = _mapping(item)
            if set(result) != {"key", "version_id"}:
                raise ValueError
            key = _exact_string(result["key"])
            version_id = _version_id(result["version_id"])
            if key in put_versions or observed[key]["version_id"] != version_id:
                raise ValueError
            put_versions[key] = version_id
        if set(put_versions) != set(candidate) or upload_order[-1] != "index.html":
            raise ValueError

        for key, expected in candidate.items():
            actual = observed[key]
            if not _matches_release_object(actual, expected):
                raise ValueError
            prior = predecessor.get(key)
            if prior is not None and actual["version_id"] == prior["version_id"]:
                raise ValueError
        for key in set(predecessor) - set(candidate):
            if not _matches_preserved(observed[key], predecessor[key]):
                raise ValueError
        runtime_key = _exact_string(_mapping(release["runtime_config"])["object_key"])
        if (
            runtime_key in candidate
            or not _matches_preserved(observed[runtime_key], predecessor[runtime_key])
            or not _matches_runtime(observed[runtime_key], _mapping(release["runtime_config"]))
        ):
            raise ValueError

        versions_to_delete = [
            {"key": key, "version_id": put_versions[key]} for key in reversed(upload_order)
        ]
        authority: dict[str, object] = {
            "algorithm": "sha256",
            "bucket": WEB_BUCKET,
            "candidate_release": candidate_binding,
            "candidate_versions_to_delete": versions_to_delete,
            "current_keys": sorted(observed),
            "format": LIVE_VERIFICATION_FORMAT,
            "observed_at": observation["captured_at"],
            "rollback_manifest_aggregate_sha256": rollback["aggregate_sha256"],
            "rollback_manifest_sha256": sha256(rollback_raw).hexdigest(),
            "runtime_config_version_id": observed[runtime_key]["version_id"],
            "verification": "candidate_live_readback_verified",
        }
        return _with_aggregate(authority)
    except (Phase718WebReleaseError, Phase718WebDeploymentEvidenceError):
        raise Phase718WebDeploymentEvidenceError(_GENERIC_ERROR) from None
    except Exception:
        raise Phase718WebDeploymentEvidenceError(_GENERIC_ERROR) from None


def render_phase718_web_rollback_verification(
    release_manifest_path: Path,
    rollback_manifest_path: Path,
    live_verification_path: Path,
    rollback_observation_path: Path,
    *,
    repository_root: Path = ROOT,
) -> bytes:
    """Prove candidate versions were removed and every exact predecessor VersionId is live."""

    try:
        repository = _repository(repository_root)
        release_raw, release = load_phase718_web_release_manifest(
            release_manifest_path,
            repository_root=repository,
        )
        rollback_raw, rollback = _load_rollback(rollback_manifest_path, repository=repository)
        live_raw, live = _load_evidence(
            live_verification_path,
            expected_format=LIVE_VERIFICATION_FORMAT,
            repository=repository,
        )
        candidate_binding = _candidate_binding(release_raw, release)
        if (
            rollback["candidate_release"] != candidate_binding
            or live.get("candidate_release") != candidate_binding
            or live.get("rollback_manifest_aggregate_sha256") != rollback["aggregate_sha256"]
            or live.get("rollback_manifest_sha256") != sha256(rollback_raw).hexdigest()
        ):
            raise ValueError
        candidate = _release_records(release)
        upload_order = _string_list(release["upload_order"])
        predecessor = _records_by_key(cast(list[object], rollback["predecessor_objects"]))
        runtime_key = _exact_string(_mapping(release["runtime_config"])["object_key"])
        if (
            set(live)
            != {
                "aggregate_sha256",
                "algorithm",
                "bucket",
                "candidate_release",
                "candidate_versions_to_delete",
                "current_keys",
                "format",
                "observed_at",
                "rollback_manifest_aggregate_sha256",
                "rollback_manifest_sha256",
                "runtime_config_version_id",
                "verification",
            }
            or live.get("algorithm") != "sha256"
            or live.get("bucket") != WEB_BUCKET
            or live.get("current_keys") != sorted(set(predecessor) | set(candidate))
            or not _timestamp(live.get("observed_at"))
            or live.get("runtime_config_version_id") != predecessor[runtime_key]["version_id"]
            or live.get("verification") != "candidate_live_readback_verified"
        ):
            raise ValueError
        candidate_deletes = live.get("candidate_versions_to_delete")
        if not isinstance(candidate_deletes, list) or [
            _mapping(item).get("key") for item in candidate_deletes
        ] != list(reversed(upload_order)):
            raise ValueError
        if any(
            set(_mapping(item)) != {"key", "version_id"}
            or _mapping(item).get("key") not in candidate
            or _version_id(_mapping(item).get("version_id")) == ""
            for item in candidate_deletes
        ):
            raise ValueError

        _observation_raw, observation = _load_observation(
            rollback_observation_path,
            repository=repository,
        )
        if observation["put_results"] != []:
            raise ValueError
        observed = _records_by_key(cast(list[object], observation["objects"]))
        predecessor = _records_by_key(cast(list[object], rollback["predecessor_objects"]))
        if set(observed) != set(predecessor) or any(
            not _matches_preserved(observed[key], predecessor[key]) for key in predecessor
        ):
            raise ValueError

        authority: dict[str, object] = {
            "algorithm": "sha256",
            "bucket": WEB_BUCKET,
            "candidate_release": candidate_binding,
            "format": ROLLBACK_VERIFICATION_FORMAT,
            "live_verification_aggregate_sha256": live["aggregate_sha256"],
            "live_verification_sha256": sha256(live_raw).hexdigest(),
            "observed_at": observation["captured_at"],
            "restored_predecessor_versions": [
                {"key": key, "version_id": predecessor[key]["version_id"]}
                for key in sorted(predecessor)
            ],
            "rollback_manifest_aggregate_sha256": rollback["aggregate_sha256"],
            "verification": "exact_predecessor_versions_restored",
        }
        return _with_aggregate(authority)
    except (Phase718WebReleaseError, Phase718WebDeploymentEvidenceError):
        raise Phase718WebDeploymentEvidenceError(_GENERIC_ERROR) from None
    except Exception:
        raise Phase718WebDeploymentEvidenceError(_GENERIC_ERROR) from None


def write_phase718_web_evidence(
    raw: bytes,
    destination: Path,
    *,
    repository_root: Path = ROOT,
) -> Path:
    """Create one private, mode-0600 evidence file without overwriting prior authority."""

    try:
        repository = _repository(repository_root)
        document = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(document, Mapping) or render_manifest(document) != raw:
            raise ValueError
        target = _private_destination(destination, repository=repository)
        _prepare_private_directory(repository, target.parent)
        with target.open("xb") as stream:
            stream.write(raw)
        target.chmod(0o600)
        if target.read_bytes() != raw or stat.S_IMODE(target.stat().st_mode) != 0o600:
            raise ValueError
        return target
    except Exception:
        raise Phase718WebDeploymentEvidenceError(_GENERIC_ERROR) from None


def _load_observation(
    path: Path,
    *,
    repository: Path,
) -> tuple[bytes, Mapping[str, object]]:
    raw, value = _load_json(path, repository=repository)
    if (
        set(value)
        != {"bucket", "bucket_versioning", "captured_at", "format", "objects", "put_results"}
        or value.get("bucket") != WEB_BUCKET
        or value.get("bucket_versioning") != "Enabled"
        or value.get("format") != READBACK_OBSERVATION_FORMAT
        or not _timestamp(value.get("captured_at"))
        or not isinstance(value.get("objects"), list)
        or not isinstance(value.get("put_results"), list)
    ):
        raise ValueError
    records = cast(list[object], value["objects"])
    if not 1 <= len(records) <= 9:
        raise ValueError
    prior_key = ""
    for item in records:
        record = _mapping(item)
        _validate_readback_record(record, path_field="body_path", repository=repository)
        key = _exact_string(record["key"])
        if key <= prior_key:
            raise ValueError
        prior_key = key
    return raw, value


def _load_rollback(
    path: Path,
    *,
    repository: Path,
) -> tuple[bytes, Mapping[str, object]]:
    raw, value = _load_evidence(
        path,
        expected_format=ROLLBACK_MANIFEST_FORMAT,
        repository=repository,
    )
    if set(value) != {
        "aggregate_sha256",
        "algorithm",
        "bucket",
        "candidate_release",
        "captured_at",
        "format",
        "predecessor_objects",
        "rollback_action",
        "runtime_config",
        "versioning_required",
    }:
        raise ValueError
    objects = value.get("predecessor_objects")
    if (
        value.get("algorithm") != "sha256"
        or value.get("bucket") != WEB_BUCKET
        or not _timestamp(value.get("captured_at"))
        or value.get("rollback_action")
        != {
            "delete_order": "reverse_candidate_upload_order",
            "method": "delete_exact_candidate_object_versions",
            "predecessor_version_ids_must_match_after_delete": True,
        }
        or value.get("versioning_required") is not True
        or not isinstance(objects, list)
    ):
        raise ValueError
    prior_key = ""
    for item in objects:
        record = _mapping(item)
        _validate_readback_record(record, path_field="source_path", repository=repository)
        key = _exact_string(record["key"])
        if key <= prior_key:
            raise ValueError
        prior_key = key
    records = _records_by_key(objects)
    _validate_predecessor(records, repository=repository)
    runtime = _mapping(value.get("runtime_config"))
    release_runtime = {
        key: item
        for key, item in runtime.items()
        if key not in {"predecessor_version_id", "source_path"}
    }
    runtime_record = records["runtime-config.json"]
    if (
        set(runtime)
        != {
            "cache_control",
            "content_type",
            "manifest_sha256",
            "object_key",
            "predecessor_version_id",
            "preserve_existing",
            "sha256",
            "size_bytes",
            "source_path",
        }
        or runtime.get("predecessor_version_id") != runtime_record["version_id"]
        or runtime.get("source_path") != runtime_record["source_path"]
        or not _matches_runtime(runtime_record, release_runtime)
    ):
        raise ValueError
    _validate_candidate_binding(_mapping(value.get("candidate_release")))
    return raw, value


def _load_evidence(
    path: Path,
    *,
    expected_format: str,
    repository: Path,
) -> tuple[bytes, Mapping[str, object]]:
    raw, value = _load_json(path, repository=repository)
    aggregate = value.get("aggregate_sha256")
    authority = {key: item for key, item in value.items() if key != "aggregate_sha256"}
    if (
        value.get("format") != expected_format
        or not isinstance(aggregate, str)
        or aggregate != sha256(render_manifest(authority)).hexdigest()
    ):
        raise ValueError
    return raw, value


def _load_json(path: Path, *, repository: Path) -> tuple[bytes, Mapping[str, object]]:
    source = _required_file(path, repository=repository, private=False)
    if not 1 <= source.stat().st_size <= _MAX_FILE_BYTES:
        raise ValueError
    raw = source.read_bytes()
    value = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(value, Mapping) or render_manifest(value) != raw:
        raise ValueError
    return raw, cast(Mapping[str, object], value)


def _validate_readback_record(
    record: Mapping[str, object],
    *,
    path_field: str,
    repository: Path,
) -> None:
    if set(record) != {
        "cache_control",
        "checksum_sha256_base64",
        "content_type",
        "etag",
        "key",
        "metadata",
        path_field,
        "server_side_encryption",
        "sha256",
        "size_bytes",
        "version_id",
    }:
        raise ValueError
    key = _exact_string(record["key"])
    digest = _fingerprint(record["sha256"])
    size = record.get("size_bytes")
    metadata = record.get("metadata")
    if (
        not _allowed_key(key)
        or record.get("cache_control") != _headers(key)[1]
        or record.get("checksum_sha256_base64")
        != base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        or record.get("content_type") != _headers(key)[0]
        or not isinstance(record.get("etag"), str)
        or _ETAG.fullmatch(cast(str, record["etag"])) is None
        or not isinstance(metadata, Mapping)
        or record.get("server_side_encryption") != SERVER_SIDE_ENCRYPTION
        or not isinstance(size, int)
        or not 1 <= size <= _MAX_FILE_BYTES
    ):
        raise ValueError
    _version_id(record["version_id"])
    for raw_key, raw_value in metadata.items():
        if (
            not isinstance(raw_key, str)
            or _METADATA_KEY.fullmatch(raw_key) is None
            or not isinstance(raw_value, str)
            or raw_value != raw_value.strip()
            or not raw_value.isascii()
            or not 1 <= len(raw_value) <= 1024
        ):
            raise ValueError
    source_value = _exact_string(record[path_field])
    pure_source = PurePosixPath(source_value)
    if (
        pure_source.as_posix() != source_value
        or any(part in {".", ".."} for part in pure_source.parts)
        or not source_value.startswith(f"{PRIVATE_ROOT.as_posix()}/")
    ):
        raise ValueError
    source = _required_file(Path(source_value), repository=repository, private=True)
    body = source.read_bytes()
    if len(body) != size or sha256(body).hexdigest() != digest:
        raise ValueError


def _validate_predecessor(
    records: Mapping[str, Mapping[str, object]],
    *,
    repository: Path,
) -> None:
    assets = [key for key in records if _ASSET.fullmatch(key)]
    css = [key for key in assets if key.endswith(".css")]
    javascript = [key for key in assets if key.endswith(".js")]
    if (
        set(records) != {*assets, "favicon.svg", "index.html", "runtime-config.json"}
        or len(css) != 1
        or len(javascript) != 1
    ):
        raise ValueError
    index = _record_body(records["index.html"], repository=repository)
    script = _record_body(records[javascript[0]], repository=repository)
    if (
        index.count(f"/{css[0]}".encode()) != 1
        or index.count(f"/{javascript[0]}".encode()) != 1
        or index.count(b"/favicon.svg") != 1
        or PHASE718_CONTRACT_VERSION.encode() in script
        or b"data-phase7-publication-workspace" in script
    ):
        raise ValueError


def _candidate_binding(
    release_raw: bytes,
    release: Mapping[str, object],
) -> dict[str, object]:
    enabled = _mapping(release["enabled_runtime"])
    binding = {
        "application_release_fingerprint": enabled["application_release_fingerprint"],
        "canary_evidence_fingerprint": enabled["canary_evidence_fingerprint"],
        "contract_fingerprint": PHASE718_CONTRACT_FINGERPRINT,
        "enabled_descriptor_sha256": enabled["descriptor_sha256"],
        "enabled_release_fingerprint": enabled["release_fingerprint"],
        "enabled_template_sha256": enabled["enabled_template_sha256"],
        "enablement_evidence_fingerprint": enabled["enablement_evidence_fingerprint"],
        "release_aggregate_sha256": release["aggregate_sha256"],
        "release_manifest_sha256": sha256(release_raw).hexdigest(),
        "source_commit": release["source_commit"],
        "web_bundle_sha256": release["browser_bundle_sha256"],
    }
    _validate_candidate_binding(binding)
    return binding


def _validate_candidate_binding(binding: Mapping[str, object]) -> None:
    fingerprint_fields = {
        "application_release_fingerprint",
        "canary_evidence_fingerprint",
        "contract_fingerprint",
        "enabled_descriptor_sha256",
        "enabled_release_fingerprint",
        "enabled_template_sha256",
        "enablement_evidence_fingerprint",
        "release_aggregate_sha256",
        "release_manifest_sha256",
        "web_bundle_sha256",
    }
    if (
        set(binding) != {*fingerprint_fields, "source_commit"}
        or binding.get("contract_fingerprint") != PHASE718_CONTRACT_FINGERPRINT
        or any(_FINGERPRINT.fullmatch(str(binding.get(key))) is None for key in fingerprint_fields)
        or not isinstance(binding.get("source_commit"), str)
        or re.fullmatch(r"(?!0{40})[a-f0-9]{40}", cast(str, binding["source_commit"])) is None
    ):
        raise ValueError


def _release_records(release: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    objects = release.get("objects")
    if not isinstance(objects, list):
        raise ValueError
    return _records_by_key(objects)


def _records_by_key(records: list[object]) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for item in records:
        record = _mapping(item)
        key = _exact_string(record["key"])
        if key in result:
            raise ValueError
        result[key] = record
    return result


def _preserved_record(record: Mapping[str, object]) -> dict[str, object]:
    return {
        **{key: item for key, item in record.items() if key != "body_path"},
        "source_path": record["body_path"],
    }


def _matches_release_object(
    observed: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    fields = {
        "cache_control",
        "checksum_sha256_base64",
        "content_type",
        "key",
        "metadata",
        "sha256",
        "size_bytes",
    }
    return all(observed.get(field) == expected.get(field) for field in fields) and (
        observed.get("server_side_encryption") == SERVER_SIDE_ENCRYPTION
    )


def _matches_preserved(
    observed: Mapping[str, object],
    predecessor: Mapping[str, object],
) -> bool:
    fields = {
        "cache_control",
        "checksum_sha256_base64",
        "content_type",
        "etag",
        "key",
        "metadata",
        "server_side_encryption",
        "sha256",
        "size_bytes",
        "version_id",
    }
    return all(observed.get(field) == predecessor.get(field) for field in fields)


def _matches_runtime(record: Mapping[str, object], runtime: Mapping[str, object]) -> bool:
    return (
        record.get("key") == runtime.get("object_key")
        and record.get("cache_control") == runtime.get("cache_control")
        and record.get("content_type") == runtime.get("content_type")
        and record.get("sha256") == runtime.get("sha256")
        and record.get("size_bytes") == runtime.get("size_bytes")
    )


def _record_body(record: Mapping[str, object], *, repository: Path) -> bytes:
    field = "body_path" if "body_path" in record else "source_path"
    return (repository / _exact_string(record[field])).read_bytes()


def _allowed_key(key: str) -> bool:
    return _ASSET.fullmatch(key) is not None or key in {
        "favicon.svg",
        "index.html",
        "runtime-config.json",
    }


def _headers(key: str) -> tuple[str, str]:
    if _ASSET.fullmatch(key):
        content_type = (
            "text/css; charset=utf-8" if key.endswith(".css") else "text/javascript; charset=utf-8"
        )
        return content_type, "public, max-age=31536000, immutable"
    if key == "favicon.svg":
        return "image/svg+xml", "private, no-store, max-age=0"
    if key == "index.html":
        return "text/html; charset=utf-8", "private, no-store, max-age=0"
    if key == "runtime-config.json":
        return "application/json", "private, no-store, max-age=0"
    raise ValueError


def _with_aggregate(authority: Mapping[str, object]) -> bytes:
    return render_manifest(
        {**authority, "aggregate_sha256": sha256(render_manifest(authority)).hexdigest()}
    )


def _repository(root: Path) -> Path:
    repository = root.resolve(strict=True)
    if root.is_symlink() or not repository.is_dir():
        raise ValueError
    return repository


def _required_file(path: Path, *, repository: Path, private: bool) -> Path:
    candidate = path if path.is_absolute() else repository / path
    resolved = candidate.resolve(strict=True)
    private_root = repository / PRIVATE_ROOT
    if (
        candidate.is_symlink()
        or not resolved.is_file()
        or not resolved.is_relative_to(private_root if private else repository)
        or _path_has_symlink_component(repository, resolved)
    ):
        raise ValueError
    return resolved


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


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError
    return cast(Mapping[str, object], value)


def _exact_string(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or not value.isascii():
        raise ValueError
    return value


def _fingerprint(value: object) -> str:
    result = _exact_string(value)
    if _FINGERPRINT.fullmatch(result) is None:
        raise ValueError
    return result


def _version_id(value: object) -> str:
    result = _exact_string(value)
    if result == "null" or len(result) > 1024:
        raise ValueError
    return result


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError
    return [_exact_string(item) for item in value]


def _timestamp(value: object) -> bool:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None
    ):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture-rollback")
    capture.add_argument("--release", required=True, type=Path)
    capture.add_argument("--observation", required=True, type=Path)
    capture.add_argument("--output", required=True, type=Path)
    live = subparsers.add_parser("verify-live")
    live.add_argument("--release", required=True, type=Path)
    live.add_argument("--rollback", required=True, type=Path)
    live.add_argument("--observation", required=True, type=Path)
    live.add_argument("--output", required=True, type=Path)
    restored = subparsers.add_parser("verify-rollback")
    restored.add_argument("--release", required=True, type=Path)
    restored.add_argument("--rollback", required=True, type=Path)
    restored.add_argument("--live-verification", required=True, type=Path)
    restored.add_argument("--observation", required=True, type=Path)
    restored.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == "capture-rollback":
            raw = render_phase718_web_rollback_manifest(
                arguments.release,
                arguments.observation,
            )
        elif arguments.command == "verify-live":
            raw = render_phase718_web_live_verification(
                arguments.release,
                arguments.rollback,
                arguments.observation,
            )
        else:
            raw = render_phase718_web_rollback_verification(
                arguments.release,
                arguments.rollback,
                arguments.live_verification,
                arguments.observation,
            )
        print(write_phase718_web_evidence(raw, arguments.output))
    except Phase718WebDeploymentEvidenceError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "LIVE_VERIFICATION_FORMAT",
    "READBACK_OBSERVATION_FORMAT",
    "ROLLBACK_MANIFEST_FORMAT",
    "ROLLBACK_VERIFICATION_FORMAT",
    "Phase718WebDeploymentEvidenceError",
    "render_phase718_web_live_verification",
    "render_phase718_web_rollback_manifest",
    "render_phase718_web_rollback_verification",
    "write_phase718_web_evidence",
]


if __name__ == "__main__":
    main()
