"""Render one exact shared-Lambda archive refresh for the deployed Phase 6 stack.

Phase 6 intentionally deploys one sealed archive to all ten SAM functions.  This renderer starts
from the exact deployed post-runtime-envelope template and changes only those ten ``CodeUri``
objects, the release-fingerprint parameter binding, and one provenance record.  It is local-only:
it never builds or uploads an archive, contacts AWS, or grants deployment authority.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

from tools.verify_phase6_s3_release_object import validate_phase6_s3_version_id

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PREDECESSOR_TEMPLATE_SHA256: Final = (
    "618fbca8d00b1edbfa7412668a6e7d2a0e4e65e23460ee8b9216f92f19dbdfc2"
)
PREDECESSOR_RELEASE_FINGERPRINT: Final = (
    "0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b"
)
PREDECESSOR_LAMBDA_BUCKET: Final = "mr-lister-phase6-artifacts-dev-384627057108-us-west-2"
PREDECESSOR_LAMBDA_KEY: Final = (
    "private/deployments/lambda/releases/"
    f"{PREDECESSOR_RELEASE_FINGERPRINT}/"
    "phase6-lambda-baf152b732ce8574b6a6925bae7ab4ff849c1b83d4137076c52c6682553f9d48.zip"
)
PREDECESSOR_LAMBDA_VERSION: Final = "pHutjLzKNpukwJ75Qs9s8YzXUAvgxZuS"
SHARED_LAMBDA_ARCHIVE_REFRESH_FORMAT: Final = "mr-lister-phase6-shared-lambda-archive-refresh-v1"
DEFAULT_PREDECESSOR_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-runtime-envelope-correction/"
    "template.review-query-runtime-envelope.local.json"
)
DEFAULT_OUTPUT_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-shared-lambda-archive-refresh/"
    "template.shared-lambda-archive-refresh.local.json"
)

_GENERIC_ERROR = "Phase 6 shared Lambda archive refresh is invalid"
_METADATA_KEY = "MrListerPhase6SharedLambdaArchiveRefresh"
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_ARCHIVE_KEY = re.compile(
    r"^private/deployments/lambda/releases/([a-f0-9]{64})/"
    r"phase6-lambda-([a-f0-9]{64})\.zip$"
)
_FUNCTION_LOGICAL_IDS = (
    "DispatcherFunction",
    "PreparationDispatchFunction",
    "ProviderDraftFunction",
    "ReviewQueryApiFunction",
    "SellerCommandApiFunction",
    "SettlementFunction",
    "SourceVersionRetentionFunction",
    "StuckExecutionRecoveryFunction",
    "TerminalOperationalCleanupFunction",
    "UploadApiFunction",
)


class Phase6SharedLambdaArchiveRefreshError(RuntimeError):
    """A value-free shared-archive rendering failure."""


@dataclass(frozen=True, slots=True)
class Phase6SharedLambdaArchiveRefreshBinding:
    """Exact immutable S3 object and release identity for one refresh."""

    lambda_artifact_bucket: str
    lambda_artifact_key: str
    lambda_artifact_version: str
    release_fingerprint: str

    def __post_init__(self) -> None:
        try:
            values = (
                self.lambda_artifact_bucket,
                self.lambda_artifact_key,
                self.lambda_artifact_version,
                self.release_fingerprint,
            )
            if any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 4096
                or "\x00" in value
                for value in values
            ):
                raise ValueError
            key_match = _ARCHIVE_KEY.fullmatch(self.lambda_artifact_key)
            if (
                self.lambda_artifact_bucket != PREDECESSOR_LAMBDA_BUCKET
                or _FINGERPRINT.fullmatch(self.release_fingerprint) is None
                or self.release_fingerprint == "0" * 64
                or self.release_fingerprint == PREDECESSOR_RELEASE_FINGERPRINT
                or key_match is None
                or key_match.group(1) != self.release_fingerprint
                or key_match.group(2) == "0" * 64
                or self.lambda_artifact_key == PREDECESSOR_LAMBDA_KEY
                or self.lambda_artifact_version == PREDECESSOR_LAMBDA_VERSION
            ):
                raise ValueError
            validate_phase6_s3_version_id(self.lambda_artifact_version)
        except Exception:
            raise Phase6SharedLambdaArchiveRefreshError(_GENERIC_ERROR) from None

    @property
    def archive_sha256(self) -> str:
        """Return the archive digest sealed into the validated content-addressed key."""

        match = _ARCHIVE_KEY.fullmatch(self.lambda_artifact_key)
        if match is None:  # pragma: no cover - construction proves this invariant
            raise Phase6SharedLambdaArchiveRefreshError(_GENERIC_ERROR)
        return match.group(2)

    @property
    def code_uri(self) -> dict[str, str]:
        """Return the exact SAM S3 object binding."""

        return {
            "Bucket": self.lambda_artifact_bucket,
            "Key": self.lambda_artifact_key,
            "Version": self.lambda_artifact_version,
        }


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            indent=2,
            separators=(",", ": "),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError
        value[key] = nested
    return value


def _document(raw: bytes) -> dict[str, Any]:
    value = json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_object,
    )
    if (
        not isinstance(value, dict)
        or sha256(raw).hexdigest() != PREDECESSOR_TEMPLATE_SHA256
        or _canonical(value) != raw
    ):
        raise ValueError
    return value


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError
    return value


def _repository_path(path: Path) -> tuple[Path, tuple[str, ...]]:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(REPOSITORY_ROOT)
    except ValueError:
        raise ValueError from None
    if not relative.parts:
        raise ValueError
    return candidate, relative.parts


def _read_repository_file(path: Path) -> bytes:
    candidate, components = _repository_path(path)
    current = REPOSITORY_ROOT
    repository_metadata = current.lstat()
    if not stat.S_ISDIR(repository_metadata.st_mode) or stat.S_ISLNK(repository_metadata.st_mode):
        raise ValueError
    for index, component in enumerate(components):
        current /= component
        metadata = current.lstat()
        if index < len(components) - 1:
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError
        elif (
            current != candidate
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise ValueError
    return candidate.read_bytes()


def _prepare_private_output(path: Path) -> Path:
    candidate, components = _repository_path(path)
    if components[0] != ".mr_lister_private" or len(components) < 2:
        raise ValueError
    current = REPOSITORY_ROOT
    repository_metadata = current.lstat()
    if not stat.S_ISDIR(repository_metadata.st_mode) or stat.S_ISLNK(repository_metadata.st_mode):
        raise ValueError
    for component in components[:-1]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError
        current.chmod(0o700)
    return candidate


def _changed_paths(
    before: object,
    after: object,
    prefix: tuple[str, ...] = (),
) -> set[tuple[str, ...]]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths: set[tuple[str, ...]] = set()
        for key in set(before) | set(after):
            if key not in before or key not in after:
                paths.add((*prefix, str(key)))
            else:
                paths.update(_changed_paths(before[key], after[key], (*prefix, str(key))))
        return paths
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        paths = set()
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            paths.update(_changed_paths(left, right, (*prefix, str(index))))
        return paths
    return set() if before == after else {prefix}


def _validate_predecessor(predecessor: dict[str, Any]) -> None:
    resources = _mapping(predecessor.get("Resources"))
    actual_functions = {
        logical_id
        for logical_id, resource in resources.items()
        if isinstance(resource, Mapping) and resource.get("Type") == "AWS::Serverless::Function"
    }
    if actual_functions != set(_FUNCTION_LOGICAL_IDS):
        raise ValueError

    expected_code_uri = {
        "Bucket": PREDECESSOR_LAMBDA_BUCKET,
        "Key": PREDECESSOR_LAMBDA_KEY,
        "Version": PREDECESSOR_LAMBDA_VERSION,
    }
    for logical_id in _FUNCTION_LOGICAL_IDS:
        resource = _mapping(resources.get(logical_id))
        properties = _mapping(resource.get("Properties"))
        if properties.get("CodeUri") != expected_code_uri:
            raise ValueError

    parameters = _mapping(predecessor.get("Parameters"))
    release = _mapping(parameters.get("ReleaseFingerprint"))
    metadata = _mapping(predecessor.get("Metadata"))
    if (
        release.get("Default") != PREDECESSOR_RELEASE_FINGERPRINT
        or release.get("AllowedValues") != [PREDECESSOR_RELEASE_FINGERPRINT]
        or _METADATA_KEY in metadata
    ):
        raise ValueError


def render_phase6_shared_lambda_archive_refresh(
    predecessor_raw: bytes,
    binding: Phase6SharedLambdaArchiveRefreshBinding,
) -> bytes:
    """Return one canonical template containing only the bounded shared-code refresh."""

    try:
        if not isinstance(binding, Phase6SharedLambdaArchiveRefreshBinding):
            raise ValueError
        predecessor = _document(predecessor_raw)
        _validate_predecessor(predecessor)
        target = deepcopy(predecessor)
        resources = _mapping(target.get("Resources"))
        target_code_uri = binding.code_uri
        for logical_id in _FUNCTION_LOGICAL_IDS:
            resource = _mapping(resources.get(logical_id))
            properties = _mapping(resource.get("Properties"))
            properties["CodeUri"] = deepcopy(target_code_uri)

        parameters = _mapping(target.get("Parameters"))
        release = _mapping(parameters.get("ReleaseFingerprint"))
        release["Default"] = binding.release_fingerprint
        release["AllowedValues"] = [binding.release_fingerprint]

        metadata = _mapping(target.get("Metadata"))
        metadata[_METADATA_KEY] = {
            "ArchiveSha256": binding.archive_sha256,
            "CodeUri": target_code_uri,
            "Format": SHARED_LAMBDA_ARCHIVE_REFRESH_FORMAT,
            "PredecessorTemplateSha256": PREDECESSOR_TEMPLATE_SHA256,
            "ReleaseFingerprint": binding.release_fingerprint,
            "Resources": list(_FUNCTION_LOGICAL_IDS),
        }

        expected_paths = {
            ("Metadata", _METADATA_KEY),
            ("Parameters", "ReleaseFingerprint", "AllowedValues", "0"),
            ("Parameters", "ReleaseFingerprint", "Default"),
        }
        for logical_id in _FUNCTION_LOGICAL_IDS:
            expected_paths.add(("Resources", logical_id, "Properties", "CodeUri", "Key"))
            expected_paths.add(("Resources", logical_id, "Properties", "CodeUri", "Version"))
        if _changed_paths(predecessor, target) != expected_paths:
            raise ValueError
        return _canonical(target)
    except Phase6SharedLambdaArchiveRefreshError:
        raise
    except Exception:
        raise Phase6SharedLambdaArchiveRefreshError(_GENERIC_ERROR) from None


def write_phase6_shared_lambda_archive_refresh(
    binding: Phase6SharedLambdaArchiveRefreshBinding,
) -> Path:
    """Create the private target once, or verify that its existing bytes are identical."""

    try:
        rendered = render_phase6_shared_lambda_archive_refresh(
            _read_repository_file(DEFAULT_PREDECESSOR_PATH),
            binding,
        )
        output_path = _prepare_private_output(DEFAULT_OUTPUT_PATH)
        if output_path.exists() or output_path.is_symlink():
            if _read_repository_file(output_path) != rendered:
                raise ValueError
        else:
            temporary = output_path.with_name(f".{output_path.name}.{secrets.token_hex(12)}.tmp")
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    descriptor = None
                    output.write(rendered)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(output_path)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        output_path.chmod(0o600)
        return output_path
    except Phase6SharedLambdaArchiveRefreshError:
        raise
    except Exception:
        raise Phase6SharedLambdaArchiveRefreshError(_GENERIC_ERROR) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lambda-artifact-bucket", required=True)
    parser.add_argument("--lambda-artifact-key", required=True)
    parser.add_argument("--lambda-artifact-version", required=True)
    parser.add_argument("--release-fingerprint", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        binding = Phase6SharedLambdaArchiveRefreshBinding(
            lambda_artifact_bucket=arguments.lambda_artifact_bucket,
            lambda_artifact_key=arguments.lambda_artifact_key,
            lambda_artifact_version=arguments.lambda_artifact_version,
            release_fingerprint=arguments.release_fingerprint,
        )
        output = write_phase6_shared_lambda_archive_refresh(binding)
        rendered = _read_repository_file(output)
    except Phase6SharedLambdaArchiveRefreshError as error:
        print(str(error))
        return 2
    print(
        json.dumps(
            {
                "result": "passed",
                "target_byte_count": len(rendered),
                "target_sha256": sha256(rendered).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
