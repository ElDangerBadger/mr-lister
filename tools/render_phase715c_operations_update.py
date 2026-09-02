"""Render the temporary provider-free P7.15C operations stack update.

The deployed production-disabled template remains the immutable rollback source.  This renderer
derives a full update template from those exact bytes and changes only the recovery and retention
Lambda code bindings/concurrency plus the four immutable artifact parameters.  It never enables a
mapping, schedule, seller route, dispatcher, worker, provider secret, or publication capability.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, cast

ROOT: Final = Path(__file__).resolve().parents[1]
BASE_TEMPLATE: Final = ROOT / "infra/phase7/production-disabled-template.json"
BASE_TEMPLATE_SHA256: Final = "2a98ab2a7cf3fb04590f9f8cd3a30cc6c2e373421e70c70220be419b80ca7df2"
DEFAULT_OUTPUT: Final = (
    ROOT / ".mr_lister_private/phase715c-operations-update/operations-update-template.json"
)

OPERATIONS_ARCHIVE_KEY: Final = (
    "phase7/operations/${OperationsReleaseFingerprint}/phase715c-operations.zip"
)
OPERATIONS_MODE: Final = "PROVIDER_FREE_OPERATIONS"
RECOVERY_HANDLER: Final = (
    "mr_lister.cloud.phase715c_operations_entrypoints.publication_recovery_handler"
)
RETENTION_HANDLER: Final = (
    "mr_lister.cloud.phase715c_operations_entrypoints.publication_retention_handler"
)

_FINGERPRINT_PATTERN: Final = "^(?!0{64}$)[a-f0-9]{64}$"
_OBJECT_VERSION_PATTERN: Final = "^(?!null$)[A-Za-z0-9._+=/-]{1,1024}$"
_BUCKET_PATTERN: Final = "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
_GENERIC_ERROR: Final = "Phase 7.15C operations update template is invalid"


class Phase715cOperationsUpdateError(RuntimeError):
    """The immutable base or capability-reduced update did not match its contract."""


def render_operations_update_template(
    *,
    base_template_path: Path = BASE_TEMPLATE,
) -> bytes:
    """Return one canonical full SAM template derived from the exact deployed source."""

    try:
        raw = _exact_file(base_template_path)
        if sha256(raw).hexdigest() != BASE_TEMPLATE_SHA256:
            raise ValueError
        parsed = json.loads(raw)
        if not isinstance(parsed, Mapping):
            raise ValueError
        base = cast(dict[str, Any], parsed)
        updated = deepcopy(base)
        _apply_operations_delta(updated)
        _verify_exact_delta(base, updated)
        return _canonical(updated)
    except Phase715cOperationsUpdateError:
        raise
    except Exception:
        raise Phase715cOperationsUpdateError(_GENERIC_ERROR) from None


def verify_operations_update_template(
    template_path: Path,
    *,
    base_template_path: Path = BASE_TEMPLATE,
) -> str:
    """Require byte-for-byte canonical equality and return the template SHA-256."""

    try:
        observed = _exact_file(template_path)
        expected = render_operations_update_template(base_template_path=base_template_path)
        if observed != expected:
            raise ValueError
        return sha256(observed).hexdigest()
    except Phase715cOperationsUpdateError:
        raise
    except Exception:
        raise Phase715cOperationsUpdateError(_GENERIC_ERROR) from None


def write_operations_update_template(destination: Path) -> tuple[Path, str]:
    """Create one new generated template without overwriting any existing path."""

    try:
        output = destination.resolve(strict=False)
        if destination.is_symlink() or output.exists():
            raise ValueError
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if output.parent.is_symlink():
            raise ValueError
        payload = render_operations_update_template()
        output.write_bytes(payload)
        return output, sha256(payload).hexdigest()
    except Phase715cOperationsUpdateError:
        raise
    except Exception:
        raise Phase715cOperationsUpdateError(_GENERIC_ERROR) from None


def _apply_operations_delta(template: dict[str, Any]) -> None:
    parameters = cast(dict[str, Any], template["Parameters"])
    parameters.update(
        {
            "OperationsCodeS3Bucket": {
                "Type": "String",
                "AllowedPattern": _BUCKET_PATTERN,
            },
            "OperationsReleaseFingerprint": {
                "Type": "String",
                "AllowedPattern": _FINGERPRINT_PATTERN,
            },
            "OperationsCodeS3ObjectVersion": {
                "Type": "String",
                "AllowedPattern": _OBJECT_VERSION_PATTERN,
            },
            "ApplicationReleaseFingerprint": {
                "Type": "String",
                "AllowedPattern": _FINGERPRINT_PATTERN,
            },
        }
    )
    resources = cast(dict[str, Any], template["Resources"])
    shared_environment = {
        "MR_LISTER_PHASE715C_OPERATIONS_RELEASE_FINGERPRINT": {
            "Ref": "OperationsReleaseFingerprint"
        },
        "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ApplicationReleaseFingerprint"},
        "MR_LISTER_PHASE715C_OPERATIONS_MODE": OPERATIONS_MODE,
        "MR_LISTER_PUBLICATION_WORKFLOW_ARN": {"Ref": "PublicationWorkflowStateMachine"},
        "MR_LISTER_PHASE7_DISPATCHER_ENABLED": "false",
        "MR_LISTER_PHASE7_WORKER_ENABLED": "false",
    }
    code_uri = {
        "Bucket": {"Ref": "OperationsCodeS3Bucket"},
        "Key": {"Fn::Sub": OPERATIONS_ARCHIVE_KEY},
        "Version": {"Ref": "OperationsCodeS3ObjectVersion"},
    }
    for logical_id, handler in (
        ("PublicationRecoveryFunction", RECOVERY_HANDLER),
        ("PublicationRetentionFunction", RETENTION_HANDLER),
    ):
        properties = cast(dict[str, Any], resources[logical_id]["Properties"])
        properties["CodeUri"] = deepcopy(code_uri)
        properties["Handler"] = handler
        properties["ReservedConcurrentExecutions"] = 1
        properties["Environment"] = {"Variables": deepcopy(shared_environment)}

    outputs = cast(dict[str, Any], template["Outputs"])
    outputs["OperationsRuntimeReadiness"] = {"Value": "PROVIDER_FREE_OPERATIONS_DIRECT_INVOKE_ONLY"}
    outputs["OperationsReleaseFingerprint"] = {"Value": {"Ref": "OperationsReleaseFingerprint"}}


def _verify_exact_delta(base: Mapping[str, Any], updated: Mapping[str, Any]) -> None:
    expected = deepcopy(cast(dict[str, Any], base))
    _apply_operations_delta(expected)
    if updated != expected:
        raise ValueError

    if set(updated) != set(base):
        raise ValueError
    for key in set(base) - {"Parameters", "Outputs", "Resources"}:
        if updated[key] != base[key]:
            raise ValueError

    parameters = cast(Mapping[str, Any], updated["Parameters"])
    base_parameters = cast(Mapping[str, Any], base["Parameters"])
    if {name: parameters[name] for name in base_parameters} != base_parameters or set(
        parameters
    ) - set(base_parameters) != {
        "ApplicationReleaseFingerprint",
        "OperationsCodeS3Bucket",
        "OperationsCodeS3ObjectVersion",
        "OperationsReleaseFingerprint",
    }:
        raise ValueError

    outputs = cast(Mapping[str, Any], updated["Outputs"])
    base_outputs = cast(Mapping[str, Any], base["Outputs"])
    if {name: outputs[name] for name in base_outputs} != base_outputs or set(outputs) - set(
        base_outputs
    ) != {"OperationsReleaseFingerprint", "OperationsRuntimeReadiness"}:
        raise ValueError

    resources = cast(Mapping[str, Mapping[str, Any]], updated["Resources"])
    base_resources = cast(Mapping[str, Mapping[str, Any]], base["Resources"])
    changed_resources = {name for name in resources if resources[name] != base_resources.get(name)}
    if changed_resources != {"PublicationRecoveryFunction", "PublicationRetentionFunction"}:
        raise ValueError
    for resource in resources.values():
        if (
            resource.get("Type") == "AWS::Lambda::EventSourceMapping"
            and resource["Properties"].get("Enabled") is not False
        ):
            raise ValueError
        if (
            resource.get("Type") == "AWS::Events::Rule"
            and resource["Properties"].get("State") != "DISABLED"
        ):
            raise ValueError

    for name in (
        "PublicationQueryFunction",
        "PublicationRequestFunction",
        "PublicationDispatcherFunction",
        "PublicationWorkerFunction",
    ):
        if resources[name] != base_resources[name]:
            raise ValueError

    serialized = _canonical(updated).decode("utf-8")
    for forbidden in (
        "MR_LISTER_PRINTIFY_SECRET_ARN",
        "MR_LISTER_PRINTIFY_API_KEY",
        "MR_LISTER_ETSY_TOKEN",
        "GENERAL_AVAILABILITY_ENABLED",
        "FunctionUrlConfig",
    ):
        if forbidden in serialized:
            raise ValueError
    if any(
        isinstance(resource.get("Type"), str)
        and (
            "ApiGateway" in cast(str, resource["Type"])
            or cast(str, resource["Type"])
            in {"AWS::Lambda::Url", "AWS::Serverless::Api", "AWS::Serverless::HttpApi"}
        )
        for resource in resources.values()
    ):
        raise ValueError


def _exact_file(path: Path) -> bytes:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file():
        raise ValueError
    return resolved.read_bytes()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=False,
        )
        + "\n"
    ).encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.verify is not None:
            fingerprint = verify_operations_update_template(arguments.verify)
            result = {"mode": "verify", "sha256": fingerprint, "status": "passed"}
        else:
            output, fingerprint = write_operations_update_template(arguments.output)
            result = {
                "mode": "render",
                "path": str(output),
                "sha256": fingerprint,
                "status": "created",
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except Phase715cOperationsUpdateError:
        print(json.dumps({"status": "refused"}, sort_keys=True))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "BASE_TEMPLATE",
    "BASE_TEMPLATE_SHA256",
    "DEFAULT_OUTPUT",
    "OPERATIONS_ARCHIVE_KEY",
    "OPERATIONS_MODE",
    "Phase715cOperationsUpdateError",
    "render_operations_update_template",
    "verify_operations_update_template",
    "write_operations_update_template",
]
