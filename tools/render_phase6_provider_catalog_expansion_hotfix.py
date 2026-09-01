"""Render the exact provider-only Phase 6 catalog-expansion hotfix.

This offline renderer starts from the byte-sealed post-walkthrough application template and
advances only ``ProviderDraftFunction`` to one sealed Lambda archive.  Its immutable ``CodeUri``
key/version, function-level release fingerprint, and one provenance record are the sole semantic
changes.  Preparation remains bound to the existing AgentCore v4 release, and every other
function, parameter, global, output, and resource remains on the deployed mosaic.

The module never builds or uploads an artifact, calls AWS, creates a change set, or executes one.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

from tools.verify_phase6_s3_release_object import validate_phase6_s3_version_id

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PREDECESSOR_TEMPLATE_SHA256: Final = (
    "17d4c0950de780c5523fe1b42471b9566f583fd8e3710701e9d3ffd893761969"
)
DEFAULT_PREDECESSOR_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-post-walkthrough-hotfix/"
    "template.application-walkthrough-hotfix.local.json"
)
DEFAULT_OUTPUT_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-provider-catalog-expansion-hotfix/"
    "template.provider-catalog-expansion-hotfix.local.json"
)

SOURCE_COMMIT: Final = "81623940b5d867668d4aa92aa4953db7706908bb"
LAMBDA_ARTIFACT_BUCKET: Final = "mr-lister-phase6-artifacts-dev-384627057108-us-west-2"
TARGET_RELEASE_FINGERPRINT: Final = (
    "3bfccee40d144f284827da221df40a787f7d09242698d177b51bbcc1414b7308"
)
TARGET_LAMBDA_ARCHIVE_SHA256: Final = (
    "e5f17ee5063a2a6aaf490df899d7c98d5a79c97b6d29911260214a6ac599cc9b"
)
TARGET_LAMBDA_ARCHIVE_SIZE: Final = 62_708_849
TARGET_LAMBDA_ARTIFACT_KEY: Final = (
    "private/deployments/lambda/releases/"
    f"{TARGET_RELEASE_FINGERPRINT}/"
    f"phase6-lambda-{TARGET_LAMBDA_ARCHIVE_SHA256}.zip"
)

BASE_RELEASE_FINGERPRINT: Final = "0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b"
BASE_LAMBDA_ARCHIVE_SHA256: Final = (
    "baf152b732ce8574b6a6925bae7ab4ff849c1b83d4137076c52c6682553f9d48"
)
BASE_LAMBDA_VERSION_ID: Final = "pHutjLzKNpukwJ75Qs9s8YzXUAvgxZuS"
REVIEW_QUERY_RELEASE_FINGERPRINT: Final = (
    "6e32d16ce16371a65815e2931e0a897a34bbbce5526300438d4fc29061813571"
)
REVIEW_QUERY_LAMBDA_ARCHIVE_SHA256: Final = (
    "122958c1df7ed916de122ca95c5cf9b8a34c385a45b706f396d2907c29cb8f9c"
)
REVIEW_QUERY_LAMBDA_VERSION_ID: Final = "zFS0yxHW0Jm0qZrHjirfQCwYyZwXAeVc"
ARTWORK_CLOSURE_RELEASE_FINGERPRINT: Final = (
    "f34ab73042014fccce2cb3733624f005a4ccc10bb065b39c3e20befd3c33923f"
)
ARTWORK_CLOSURE_LAMBDA_ARCHIVE_SHA256: Final = (
    "bf5ef1a13329814934f73cef81e7ec52153e508f11ed7945921501927ea58d5e"
)
ARTWORK_CLOSURE_LAMBDA_VERSION_ID: Final = "I3w7QnfRAC3jV7uPboS2YvBXJ9Qwag8p"
WALKTHROUGH_RELEASE_FINGERPRINT: Final = (
    "9bc5e1727cfcf68b40847d1a2e416300640779898c9bf884f6f9e442b0225d9e"
)
WALKTHROUGH_LAMBDA_ARCHIVE_SHA256: Final = (
    "db179dc5fb5754619482b13505f7899469e93e820bbe18514953849ac1b959c7"
)
WALKTHROUGH_LAMBDA_VERSION_ID: Final = "lqvoltQajo8lEr9MW_LcsQ4roEXKcA7B"

AGENTCORE_RUNTIME_ARN: Final = (
    "arn:aws:bedrock-agentcore:us-west-2:384627057108:runtime/mr_lister_phase6-4HoPmq2hCI"
)
AGENTCORE_RUNTIME_BINDING_FINGERPRINT: Final = (
    "e1403259a1a1a67ce47b725f0bec2d9a5aa38673fad338924f12b9360880b922"
)
AGENTCORE_RUNTIME_ENDPOINT_ARN: Final = f"{AGENTCORE_RUNTIME_ARN}/runtime-endpoint/phase6_v4_dev"
AGENTCORE_RUNTIME_QUALIFIER: Final = "phase6_v4_dev"
AGENTCORE_RUNTIME_VERSION: Final = "4"

PROVIDER_CATALOG_EXPANSION_HOTFIX_FORMAT: Final = (
    "mr-lister-phase6-provider-catalog-expansion-hotfix-v1"
)
PREDECESSOR_RESOURCE_COUNT: Final = 102
DEPLOYED_PROCESSED_RESOURCE_COUNT: Final = 125
_METADATA_KEY: Final = "MrListerPhase6ProviderCatalogExpansionHotfix"
_PREDECESSOR_METADATA_KEYS: Final = frozenset(
    {
        "MrListerPhase6ApplicationWalkthroughHotfix",
        "MrListerPhase6CoreRuntimeStaging",
        "MrListerPhase6ReviewQueryRuntimeEnvelopeCorrection",
        "MrListerPhase6WebEdgeTransition",
    }
)
_TARGET_FUNCTION_ID: Final = "ProviderDraftFunction"
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
_RELEASE_ENVIRONMENT_KEY: Final = "MR_LISTER_RELEASE_FINGERPRINT"
_LOCKED_PARAMETER_VALUES: Final = {
    "AgentCoreRuntimeArn": AGENTCORE_RUNTIME_ARN,
    "AgentCoreRuntimeBindingFingerprint": AGENTCORE_RUNTIME_BINDING_FINGERPRINT,
    "AgentCoreRuntimeEndpointArn": AGENTCORE_RUNTIME_ENDPOINT_ARN,
    "AgentCoreRuntimeQualifier": AGENTCORE_RUNTIME_QUALIFIER,
    "AgentCoreRuntimeVersion": AGENTCORE_RUNTIME_VERSION,
    "ApplicationCertificateArn": (
        "arn:aws:acm:us-east-1:384627057108:certificate/28b8cddb-a0d7-4dc8-98de-26fd87cb5b79"
    ),
    "ApplicationOrigin": "https://massskutiny.com",
    "EnvironmentName": "dev",
    "PrintifySecretArn": (
        "arn:aws:secretsmanager:us-west-2:384627057108:secret:mr-lister/dev/printify/primary-FO1ZNd"
    ),
    "ReleaseFingerprint": BASE_RELEASE_FINGERPRINT,
}
_GENERIC_ERROR: Final = "Phase 6 provider catalog-expansion hotfix is invalid"


class Phase6ProviderCatalogExpansionHotfixError(RuntimeError):
    """A value-free rendering or confinement failure."""


@dataclass(frozen=True, slots=True)
class Phase6ProviderCatalogExpansionHotfixBinding:
    """The exact immutable target archive version selected for this hotfix."""

    lambda_artifact_version: str

    def __post_init__(self) -> None:
        try:
            validate_phase6_s3_version_id(self.lambda_artifact_version)
        except Exception:
            raise Phase6ProviderCatalogExpansionHotfixError(_GENERIC_ERROR) from None

    @property
    def code_uri(self) -> dict[str, str]:
        """Return the exact content-addressed Lambda object binding."""

        return {
            "Bucket": LAMBDA_ARTIFACT_BUCKET,
            "Key": TARGET_LAMBDA_ARTIFACT_KEY,
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


def _locked_parameter(parameters: Mapping[str, Any], name: str, expected: str) -> None:
    parameter = _mapping(parameters.get(name))
    if parameter.get("Default") != expected or parameter.get("AllowedValues") != [expected]:
        raise ValueError


def _code_uri(release: str, archive: str, version: str) -> dict[str, str]:
    return {
        "Bucket": LAMBDA_ARTIFACT_BUCKET,
        "Key": f"private/deployments/lambda/releases/{release}/phase6-lambda-{archive}.zip",
        "Version": version,
    }


def _expected_predecessor_function_binding(logical_id: str) -> tuple[dict[str, str], str | None]:
    if logical_id in {"PreparationDispatchFunction", "ProviderDraftFunction"}:
        return (
            _code_uri(
                WALKTHROUGH_RELEASE_FINGERPRINT,
                WALKTHROUGH_LAMBDA_ARCHIVE_SHA256,
                WALKTHROUGH_LAMBDA_VERSION_ID,
            ),
            WALKTHROUGH_RELEASE_FINGERPRINT,
        )
    if logical_id == "UploadApiFunction":
        return (
            _code_uri(
                ARTWORK_CLOSURE_RELEASE_FINGERPRINT,
                ARTWORK_CLOSURE_LAMBDA_ARCHIVE_SHA256,
                ARTWORK_CLOSURE_LAMBDA_VERSION_ID,
            ),
            ARTWORK_CLOSURE_RELEASE_FINGERPRINT,
        )
    if logical_id == "ReviewQueryApiFunction":
        return (
            _code_uri(
                REVIEW_QUERY_RELEASE_FINGERPRINT,
                REVIEW_QUERY_LAMBDA_ARCHIVE_SHA256,
                REVIEW_QUERY_LAMBDA_VERSION_ID,
            ),
            REVIEW_QUERY_RELEASE_FINGERPRINT,
        )
    return (
        _code_uri(BASE_RELEASE_FINGERPRINT, BASE_LAMBDA_ARCHIVE_SHA256, BASE_LAMBDA_VERSION_ID),
        None,
    )


def _validate_predecessor(predecessor: dict[str, Any]) -> None:
    parameters = _mapping(predecessor.get("Parameters"))
    if set(parameters) != set(_LOCKED_PARAMETER_VALUES):
        raise ValueError
    for name, expected in _LOCKED_PARAMETER_VALUES.items():
        _locked_parameter(parameters, name, expected)

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
    if len(resources) != PREDECESSOR_RESOURCE_COUNT or actual_functions != set(
        _FUNCTION_LOGICAL_IDS
    ):
        raise ValueError
    for logical_id in _FUNCTION_LOGICAL_IDS:
        resource = _mapping(resources.get(logical_id))
        properties = _mapping(resource.get("Properties"))
        expected_code, expected_release = _expected_predecessor_function_binding(logical_id)
        environment = _mapping(properties.get("Environment", {}))
        variables = _mapping(environment.get("Variables", {}))
        if (
            properties.get("CodeUri") != expected_code
            or variables.get(_RELEASE_ENVIRONMENT_KEY) != expected_release
        ):
            raise ValueError

    metadata = _mapping(predecessor.get("Metadata"))
    outputs = _mapping(predecessor.get("Outputs"))
    readiness = _mapping(outputs.get("DeploymentReadiness"))
    if set(metadata) != _PREDECESSOR_METADATA_KEYS or readiness.get("Value") != (
        "WEB_EDGE_ACTIVE_DRAFT_ONLY"
    ):
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


def _provenance(binding: Phase6ProviderCatalogExpansionHotfixBinding) -> dict[str, object]:
    return {
        "Format": PROVIDER_CATALOG_EXPANSION_HOTFIX_FORMAT,
        "LambdaArtifact": {
            "Bucket": LAMBDA_ARTIFACT_BUCKET,
            "Key": TARGET_LAMBDA_ARTIFACT_KEY,
            "ReleaseFingerprint": TARGET_RELEASE_FINGERPRINT,
            "Sha256": TARGET_LAMBDA_ARCHIVE_SHA256,
            "SizeBytes": TARGET_LAMBDA_ARCHIVE_SIZE,
            "Version": binding.lambda_artifact_version,
        },
        "PredecessorTemplateSha256": PREDECESSOR_TEMPLATE_SHA256,
        "ProcessedResourceCount": DEPLOYED_PROCESSED_RESOURCE_COUNT,
        "SourceCommit": SOURCE_COMMIT,
        "SourceResourceCount": PREDECESSOR_RESOURCE_COUNT,
        "TargetFunctions": [_TARGET_FUNCTION_ID],
    }


def render_phase6_provider_catalog_expansion_hotfix(
    predecessor_raw: bytes,
    binding: Phase6ProviderCatalogExpansionHotfixBinding,
) -> bytes:
    """Return the canonical provider-only application target."""

    try:
        if not isinstance(binding, Phase6ProviderCatalogExpansionHotfixBinding):
            raise ValueError
        predecessor = _document(predecessor_raw)
        _validate_predecessor(predecessor)
        target = deepcopy(predecessor)

        resources = _mapping(target.get("Resources"))
        provider = _mapping(resources.get(_TARGET_FUNCTION_ID))
        properties = _mapping(provider.get("Properties"))
        environment = _mapping(properties.get("Environment"))
        variables = _mapping(environment.get("Variables"))
        properties["CodeUri"] = deepcopy(binding.code_uri)
        variables[_RELEASE_ENVIRONMENT_KEY] = TARGET_RELEASE_FINGERPRINT

        metadata = _mapping(target.get("Metadata"))
        metadata[_METADATA_KEY] = _provenance(binding)

        expected_paths = {
            ("Metadata", _METADATA_KEY),
            ("Resources", _TARGET_FUNCTION_ID, "Properties", "CodeUri", "Key"),
            ("Resources", _TARGET_FUNCTION_ID, "Properties", "CodeUri", "Version"),
            (
                "Resources",
                _TARGET_FUNCTION_ID,
                "Properties",
                "Environment",
                "Variables",
                _RELEASE_ENVIRONMENT_KEY,
            ),
        }
        if _changed_paths(predecessor, target) != expected_paths:
            raise ValueError
        return _canonical(target)
    except Phase6ProviderCatalogExpansionHotfixError:
        raise
    except Exception:
        raise Phase6ProviderCatalogExpansionHotfixError(_GENERIC_ERROR) from None


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


def write_phase6_provider_catalog_expansion_hotfix(
    binding: Phase6ProviderCatalogExpansionHotfixBinding,
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> Path:
    """Create one owner-only private target and refuse every existing destination."""

    try:
        rendered = render_phase6_provider_catalog_expansion_hotfix(
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
    except Phase6ProviderCatalogExpansionHotfixError:
        raise
    except Exception:
        raise Phase6ProviderCatalogExpansionHotfixError(_GENERIC_ERROR) from None


def verify_phase6_provider_catalog_expansion_hotfix(
    binding: Phase6ProviderCatalogExpansionHotfixBinding,
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> Path:
    """Require an existing private target to equal a fresh bounded render byte-for-byte."""

    try:
        expected = render_phase6_provider_catalog_expansion_hotfix(
            _read_repository_file(DEFAULT_PREDECESSOR_PATH),
            binding,
        )
        with _prepare_private_output(output_path, create=False) as (output, _):
            pass
        if _read_repository_file(output) != expected:
            raise ValueError
        return output
    except Phase6ProviderCatalogExpansionHotfixError:
        raise
    except Exception:
        raise Phase6ProviderCatalogExpansionHotfixError(_GENERIC_ERROR) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lambda-artifact-version", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        binding = Phase6ProviderCatalogExpansionHotfixBinding(
            lambda_artifact_version=arguments.lambda_artifact_version,
        )
        output = (
            write_phase6_provider_catalog_expansion_hotfix(
                binding,
                output_path=arguments.output,
            )
            if arguments.write
            else verify_phase6_provider_catalog_expansion_hotfix(
                binding,
                output_path=arguments.output,
            )
        )
        rendered = _read_repository_file(output)
    except Phase6ProviderCatalogExpansionHotfixError as error:
        print(str(error))
        return 2
    print(
        json.dumps(
            {
                "lambda_archive_sha256": TARGET_LAMBDA_ARCHIVE_SHA256,
                "lambda_archive_size": TARGET_LAMBDA_ARCHIVE_SIZE,
                "lambda_artifact_version": binding.lambda_artifact_version,
                "release_fingerprint": TARGET_RELEASE_FINGERPRINT,
                "result": "passed",
                "source_commit": SOURCE_COMMIT,
                "target_byte_count": len(rendered),
                "target_sha256": sha256(rendered).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
