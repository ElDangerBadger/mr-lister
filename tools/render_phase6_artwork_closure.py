"""Render the exact bounded Phase 6 artwork-closure runtime update.

The renderer is deliberately offline.  It starts from the byte-sealed current Phase 6 application
template and updates only the three Lambda boundaries that execute the reconciled artwork contract,
plus the four immutable AgentCore v3 endpoint parameters consumed by preparation.  Unchanged
functions retain their deployed archives, including the independent review-query hotfix.

This module never imports an AWS SDK, uploads an object, creates a change set, or executes one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

from mr_lister.agent.runtime_binding import agentcore_runtime_binding_fingerprint
from tools.verify_phase6_s3_release_object import validate_phase6_s3_version_id

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PREDECESSOR_TEMPLATE_SHA256: Final = (
    "6a6775a01f7c836ba90efb8f0a9259d389daac32b52c5bf553a4752aeb9f8791"
)
DEFAULT_PREDECESSOR_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-seller-command-runtime-envelope/"
    "template.seller-command-runtime-envelope.local.json"
)
DEFAULT_OUTPUT_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-artwork-closure/template.artwork-closure.local.json"
)

ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
ENVIRONMENT: Final = "dev"
LAMBDA_ARTIFACT_BUCKET: Final = "mr-lister-phase6-artifacts-dev-384627057108-us-west-2"
PREDECESSOR_RELEASE_FINGERPRINT: Final = (
    "0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b"
)
PREDECESSOR_LAMBDA_ARCHIVE_SHA256: Final = (
    "baf152b732ce8574b6a6925bae7ab4ff849c1b83d4137076c52c6682553f9d48"
)
PREDECESSOR_LAMBDA_VERSION_ID: Final = "pHutjLzKNpukwJ75Qs9s8YzXUAvgxZuS"
REVIEW_QUERY_RELEASE_FINGERPRINT: Final = (
    "6e32d16ce16371a65815e2931e0a897a34bbbce5526300438d4fc29061813571"
)
REVIEW_QUERY_LAMBDA_ARCHIVE_SHA256: Final = (
    "122958c1df7ed916de122ca95c5cf9b8a34c385a45b706f396d2907c29cb8f9c"
)
REVIEW_QUERY_LAMBDA_VERSION_ID: Final = "zFS0yxHW0Jm0qZrHjirfQCwYyZwXAeVc"
AGENTCORE_RUNTIME_ARN: Final = (
    "arn:aws:bedrock-agentcore:us-west-2:384627057108:runtime/mr_lister_phase6-4HoPmq2hCI"
)
PREDECESSOR_AGENTCORE_ENDPOINT_ARN: Final = (
    f"{AGENTCORE_RUNTIME_ARN}/runtime-endpoint/phase6_v1_dev"
)
PREDECESSOR_AGENTCORE_BINDING_FINGERPRINT: Final = (
    "14b001854285121f34394ce9893c19481f0f844aa6058abc9daca57d86d7c0f6"
)
TARGET_AGENTCORE_RUNTIME_VERSION: Final = "3"
TARGET_AGENTCORE_QUALIFIER: Final = "phase6_v3_dev"
TARGET_AGENTCORE_ENDPOINT_ARN: Final = (
    f"{AGENTCORE_RUNTIME_ARN}/runtime-endpoint/{TARGET_AGENTCORE_QUALIFIER}"
)
TARGET_RELEASE_FINGERPRINT: Final = (
    "f34ab73042014fccce2cb3733624f005a4ccc10bb065b39c3e20befd3c33923f"
)
TARGET_LAMBDA_ARCHIVE_SHA256: Final = (
    "bf5ef1a13329814934f73cef81e7ec52153e508f11ed7945921501927ea58d5e"
)
TARGET_AGENTCORE_BINDING_FINGERPRINT: Final = (
    "d8194386435d2f941d0942b102595830c1efc48e9bc4890457b46e17e0df3196"
)

_FUNCTION_LOGICAL_IDS: Final = (
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
_ARTWORK_CLOSURE_FUNCTION_IDS: Final = (
    "PreparationDispatchFunction",
    "ProviderDraftFunction",
    "UploadApiFunction",
)
_AGENTCORE_PARAMETER_NAMES: Final = (
    "AgentCoreRuntimeBindingFingerprint",
    "AgentCoreRuntimeEndpointArn",
    "AgentCoreRuntimeQualifier",
    "AgentCoreRuntimeVersion",
)
_RELEASE_ENVIRONMENT_KEY: Final = "MR_LISTER_RELEASE_FINGERPRINT"
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_LAMBDA_ARCHIVE_KEY = re.compile(
    r"^private/deployments/lambda/releases/([a-f0-9]{64})/"
    r"phase6-lambda-([a-f0-9]{64})\.zip$"
)
_GENERIC_ERROR: Final = "Phase 6 artwork-closure runtime update is invalid"


class Phase6ArtworkClosureError(RuntimeError):
    """A value-free closure-rendering or confinement failure."""


@dataclass(frozen=True, slots=True)
class Phase6ArtworkClosureBinding:
    """Exact immutable Lambda object and AgentCore v3 endpoint authority."""

    lambda_artifact_bucket: str
    lambda_artifact_key: str
    lambda_artifact_version: str
    release_fingerprint: str
    agentcore_runtime_arn: str
    agentcore_runtime_endpoint_arn: str
    agentcore_runtime_qualifier: str
    agentcore_runtime_version: str
    agentcore_runtime_binding_fingerprint: str

    def __post_init__(self) -> None:
        try:
            values = (
                self.lambda_artifact_bucket,
                self.lambda_artifact_key,
                self.lambda_artifact_version,
                self.release_fingerprint,
                self.agentcore_runtime_arn,
                self.agentcore_runtime_endpoint_arn,
                self.agentcore_runtime_qualifier,
                self.agentcore_runtime_version,
                self.agentcore_runtime_binding_fingerprint,
            )
            if any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or not value.isascii()
                or len(value) > 4096
                for value in values
            ):
                raise ValueError
            key_match = _LAMBDA_ARCHIVE_KEY.fullmatch(self.lambda_artifact_key)
            expected_binding = agentcore_runtime_binding_fingerprint(
                runtime_arn=self.agentcore_runtime_arn,
                endpoint_arn=self.agentcore_runtime_endpoint_arn,
                qualifier=self.agentcore_runtime_qualifier,
                runtime_version=self.agentcore_runtime_version,
                release_fingerprint=self.release_fingerprint,
            )
            if (
                self.lambda_artifact_bucket != LAMBDA_ARTIFACT_BUCKET
                or _FINGERPRINT.fullmatch(self.release_fingerprint) is None
                or self.release_fingerprint != TARGET_RELEASE_FINGERPRINT
                or key_match is None
                or key_match.group(1) != self.release_fingerprint
                or key_match.group(2) != TARGET_LAMBDA_ARCHIVE_SHA256
                or self.agentcore_runtime_arn != AGENTCORE_RUNTIME_ARN
                or self.agentcore_runtime_version != TARGET_AGENTCORE_RUNTIME_VERSION
                or self.agentcore_runtime_qualifier != TARGET_AGENTCORE_QUALIFIER
                or self.agentcore_runtime_endpoint_arn != TARGET_AGENTCORE_ENDPOINT_ARN
                or self.agentcore_runtime_binding_fingerprint
                != TARGET_AGENTCORE_BINDING_FINGERPRINT
                or self.agentcore_runtime_binding_fingerprint != expected_binding
            ):
                raise ValueError
            validate_phase6_s3_version_id(self.lambda_artifact_version)
        except Exception:
            raise Phase6ArtworkClosureError(_GENERIC_ERROR) from None

    @property
    def lambda_archive_sha256(self) -> str:
        match = _LAMBDA_ARCHIVE_KEY.fullmatch(self.lambda_artifact_key)
        if match is None:  # pragma: no cover - construction proves this invariant
            raise Phase6ArtworkClosureError(_GENERIC_ERROR)
        return match.group(2)

    @property
    def code_uri(self) -> dict[str, str]:
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
            ensure_ascii=True,
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


def _predecessor_code_uri(*, review_query: bool = False) -> dict[str, str]:
    if review_query:
        release = REVIEW_QUERY_RELEASE_FINGERPRINT
        archive = REVIEW_QUERY_LAMBDA_ARCHIVE_SHA256
        version = REVIEW_QUERY_LAMBDA_VERSION_ID
    else:
        release = PREDECESSOR_RELEASE_FINGERPRINT
        archive = PREDECESSOR_LAMBDA_ARCHIVE_SHA256
        version = PREDECESSOR_LAMBDA_VERSION_ID
    return {
        "Bucket": LAMBDA_ARTIFACT_BUCKET,
        "Key": (f"private/deployments/lambda/releases/{release}/phase6-lambda-{archive}.zip"),
        "Version": version,
    }


def _locked_parameter(
    parameters: Mapping[str, Any],
    name: str,
    expected_value: str,
) -> dict[str, Any]:
    parameter = _mapping(parameters.get(name))
    if parameter.get("Default") != expected_value or parameter.get("AllowedValues") != [
        expected_value
    ]:
        raise ValueError
    return parameter


def _validate_predecessor(predecessor: dict[str, Any]) -> None:
    parameters = _mapping(predecessor.get("Parameters"))
    _locked_parameter(parameters, "ReleaseFingerprint", PREDECESSOR_RELEASE_FINGERPRINT)
    _locked_parameter(parameters, "AgentCoreRuntimeArn", AGENTCORE_RUNTIME_ARN)
    _locked_parameter(
        parameters,
        "AgentCoreRuntimeEndpointArn",
        PREDECESSOR_AGENTCORE_ENDPOINT_ARN,
    )
    _locked_parameter(parameters, "AgentCoreRuntimeQualifier", "phase6_v1_dev")
    _locked_parameter(parameters, "AgentCoreRuntimeVersion", "1")
    _locked_parameter(
        parameters,
        "AgentCoreRuntimeBindingFingerprint",
        PREDECESSOR_AGENTCORE_BINDING_FINGERPRINT,
    )

    globals_value = _mapping(predecessor.get("Globals"))
    global_function = _mapping(globals_value.get("Function"))
    global_environment = _mapping(global_function.get("Environment"))
    global_variables = _mapping(global_environment.get("Variables"))
    if global_variables.get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") != "false" or global_variables.get(
        _RELEASE_ENVIRONMENT_KEY
    ) != {"Ref": "ReleaseFingerprint"}:
        raise ValueError

    resources = _mapping(predecessor.get("Resources"))
    actual_functions = {
        logical_id
        for logical_id, resource in resources.items()
        if isinstance(resource, Mapping) and resource.get("Type") == "AWS::Serverless::Function"
    }
    if actual_functions != set(_FUNCTION_LOGICAL_IDS):
        raise ValueError
    for logical_id in _FUNCTION_LOGICAL_IDS:
        resource = _mapping(resources.get(logical_id))
        properties = _mapping(resource.get("Properties"))
        review_query = logical_id == "ReviewQueryApiFunction"
        if properties.get("CodeUri") != _predecessor_code_uri(review_query=review_query):
            raise ValueError
        environment = properties.get("Environment", {})
        environment = _mapping(environment)
        variables = _mapping(environment.get("Variables", {}))
        release_override = variables.get(_RELEASE_ENVIRONMENT_KEY)
        expected_override = REVIEW_QUERY_RELEASE_FINGERPRINT if review_query else None
        if release_override != expected_override:
            raise ValueError

    outputs = _mapping(predecessor.get("Outputs"))
    readiness = _mapping(outputs.get("DeploymentReadiness"))
    if readiness.get("Value") != "WEB_EDGE_ACTIVE_DRAFT_ONLY":
        raise ValueError


def _changed_paths(
    before: object,
    after: object,
    prefix: tuple[str, ...] = (),
) -> set[tuple[str, ...]]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths: set[tuple[str, ...]] = set()
        for key in set(before) | set(after):
            path = (*prefix, str(key))
            if key not in before or key not in after:
                paths.add(path)
            else:
                paths.update(_changed_paths(before[key], after[key], path))
        return paths
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        paths = set()
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            paths.update(_changed_paths(left, right, (*prefix, str(index))))
        return paths
    return set() if before == after else {prefix}


def render_phase6_artwork_closure(
    predecessor_raw: bytes,
    binding: Phase6ArtworkClosureBinding,
) -> bytes:
    """Return the canonical three-Lambda plus AgentCore v3 closure target."""

    try:
        if not isinstance(binding, Phase6ArtworkClosureBinding):
            raise ValueError
        predecessor = _document(predecessor_raw)
        _validate_predecessor(predecessor)
        target = deepcopy(predecessor)

        resources = _mapping(target.get("Resources"))
        for logical_id in _ARTWORK_CLOSURE_FUNCTION_IDS:
            resource = _mapping(resources.get(logical_id))
            properties = _mapping(resource.get("Properties"))
            properties["CodeUri"] = deepcopy(binding.code_uri)
            environment = _mapping(properties.setdefault("Environment", {}))
            variables = _mapping(environment.setdefault("Variables", {}))
            variables[_RELEASE_ENVIRONMENT_KEY] = binding.release_fingerprint

        parameters = _mapping(target.get("Parameters"))
        parameter_values = {
            "AgentCoreRuntimeBindingFingerprint": (binding.agentcore_runtime_binding_fingerprint),
            "AgentCoreRuntimeEndpointArn": binding.agentcore_runtime_endpoint_arn,
            "AgentCoreRuntimeQualifier": binding.agentcore_runtime_qualifier,
            "AgentCoreRuntimeVersion": binding.agentcore_runtime_version,
        }
        for name, value in parameter_values.items():
            parameter = _mapping(parameters.get(name))
            parameter["Default"] = value
            parameter["AllowedValues"] = [value]

        expected_paths: set[tuple[str, ...]] = set()
        for logical_id in _ARTWORK_CLOSURE_FUNCTION_IDS:
            expected_paths.update(
                {
                    ("Resources", logical_id, "Properties", "CodeUri", "Key"),
                    ("Resources", logical_id, "Properties", "CodeUri", "Version"),
                    (
                        "Resources",
                        logical_id,
                        "Properties",
                        "Environment",
                        "Variables",
                        _RELEASE_ENVIRONMENT_KEY,
                    ),
                }
            )
        for name in _AGENTCORE_PARAMETER_NAMES:
            expected_paths.update(
                {
                    ("Parameters", name, "AllowedValues", "0"),
                    ("Parameters", name, "Default"),
                }
            )
        if _changed_paths(predecessor, target) != expected_paths:
            raise ValueError
        return _canonical(target)
    except Phase6ArtworkClosureError:
        raise
    except Exception:
        raise Phase6ArtworkClosureError(_GENERIC_ERROR) from None


def _repository_path(path: Path) -> tuple[Path, tuple[str, ...]]:
    candidate = Path(os.path.abspath(path))
    repository_root = Path(os.path.abspath(REPOSITORY_ROOT))
    try:
        relative = candidate.relative_to(repository_root)
    except ValueError:
        raise ValueError from None
    if not relative.parts:
        raise ValueError
    return candidate, relative.parts


def _open_repository_root() -> int:
    """Open the repository through one symlink-free directory chain."""

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    root = Path(os.path.abspath(REPOSITORY_ROOT))
    descriptor: int | None = None
    try:
        metadata = root.lstat()
        if (
            not root.is_absolute()
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
        ):
            raise OSError
        descriptor = os.open(os.sep, flags)
        for component in root.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise OSError
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_repository_file(path: Path) -> bytes:
    candidate, components = _repository_path(path)
    current = REPOSITORY_ROOT
    root_metadata = current.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
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


@contextmanager
def _prepare_private_output(path: Path, *, create: bool) -> Iterator[tuple[Path, int]]:
    candidate, components = _repository_path(path)
    if components[0] != ".mr_lister_private" or len(components) < 2:
        raise ValueError
    if candidate.name in {"", ".", ".."} or "/" in candidate.name or "\x00" in candidate.name:
        raise ValueError
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor: int | None = _open_repository_root()
    try:
        for component in components[:-1]:
            next_descriptor: int | None = None
            try:
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                    raise OSError
            except OSError:
                if next_descriptor is not None:
                    os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        yield candidate, descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_phase6_artwork_closure(
    binding: Phase6ArtworkClosureBinding,
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> Path:
    """Create one owner-only private target and refuse every existing destination."""

    try:
        rendered = render_phase6_artwork_closure(
            _read_repository_file(DEFAULT_PREDECESSOR_PATH),
            binding,
        )
        with _prepare_private_output(output_path, create=True) as (
            output,
            directory_descriptor,
        ):
            directory_identity = os.fstat(directory_descriptor)
            temporary = f".{output.name}.{secrets.token_hex(12)}.tmp"
            descriptor: int | None = None
            linked = False
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    descriptor = None
                    stream.write(rendered)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(
                    temporary,
                    output.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                linked = True
                os.unlink(temporary, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
                with _prepare_private_output(output, create=False) as (
                    _,
                    verification_descriptor,
                ):
                    verified_identity = os.fstat(verification_descriptor)
                    if (directory_identity.st_dev, directory_identity.st_ino) != (
                        verified_identity.st_dev,
                        verified_identity.st_ino,
                    ):
                        raise OSError
            except Exception:
                if linked:
                    try:
                        os.unlink(output.name, dir_fd=directory_descriptor)
                        os.fsync(directory_descriptor)
                    except OSError:
                        pass
                raise
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
        return output
    except Phase6ArtworkClosureError:
        raise
    except Exception:
        raise Phase6ArtworkClosureError(_GENERIC_ERROR) from None


def verify_phase6_artwork_closure(
    binding: Phase6ArtworkClosureBinding,
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> Path:
    """Require an existing private target to equal a fresh bounded render byte-for-byte."""

    try:
        expected = render_phase6_artwork_closure(
            _read_repository_file(DEFAULT_PREDECESSOR_PATH),
            binding,
        )
        with _prepare_private_output(output_path, create=False) as (output, _):
            pass
        if _read_repository_file(output) != expected:
            raise ValueError
        return output
    except Phase6ArtworkClosureError:
        raise
    except Exception:
        raise Phase6ArtworkClosureError(_GENERIC_ERROR) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lambda-artifact-bucket", required=True)
    parser.add_argument("--lambda-artifact-key", required=True)
    parser.add_argument("--lambda-artifact-version", required=True)
    parser.add_argument("--release-fingerprint", required=True)
    parser.add_argument("--agentcore-runtime-arn", required=True)
    parser.add_argument("--agentcore-runtime-endpoint-arn", required=True)
    parser.add_argument("--agentcore-runtime-qualifier", required=True)
    parser.add_argument("--agentcore-runtime-version", required=True)
    parser.add_argument("--agentcore-runtime-binding-fingerprint", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        binding = Phase6ArtworkClosureBinding(
            lambda_artifact_bucket=arguments.lambda_artifact_bucket,
            lambda_artifact_key=arguments.lambda_artifact_key,
            lambda_artifact_version=arguments.lambda_artifact_version,
            release_fingerprint=arguments.release_fingerprint,
            agentcore_runtime_arn=arguments.agentcore_runtime_arn,
            agentcore_runtime_endpoint_arn=arguments.agentcore_runtime_endpoint_arn,
            agentcore_runtime_qualifier=arguments.agentcore_runtime_qualifier,
            agentcore_runtime_version=arguments.agentcore_runtime_version,
            agentcore_runtime_binding_fingerprint=(arguments.agentcore_runtime_binding_fingerprint),
        )
        output = (
            write_phase6_artwork_closure(binding, output_path=arguments.output)
            if arguments.write
            else verify_phase6_artwork_closure(binding, output_path=arguments.output)
        )
        rendered = _read_repository_file(output)
    except Phase6ArtworkClosureError as error:
        print(str(error))
        return 2
    print(
        json.dumps(
            {
                "agentcore_runtime_binding_fingerprint": (
                    binding.agentcore_runtime_binding_fingerprint
                ),
                "lambda_archive_sha256": binding.lambda_archive_sha256,
                "release_fingerprint": binding.release_fingerprint,
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
