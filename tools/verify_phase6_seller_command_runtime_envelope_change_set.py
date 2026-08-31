"""Verify the exact Phase 6 seller-command runtime-envelope change set offline."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from tools.render_phase6_seller_command_runtime_envelope import (
    PREDECESSOR_TEMPLATE_SHA256,
)
from tools.render_phase6_seller_command_runtime_envelope import (
    SELLER_COMMAND_RUNTIME_ENVELOPE_TEMPLATE_SHA256 as TARGET_TEMPLATE_SHA256,
)

FORMAT: Final = "mr-lister-phase6-seller-command-runtime-envelope-change-set-v1"
STACK_ID: Final = (
    "arn:aws:cloudformation:us-west-2:384627057108:stack/mr-lister-phase6-dev/"
    "f3456970-9fdc-11f1-b448-06b81627db1d"
)
EXPECTED_EXECUTION_ROLE_ARN: Final = (
    "arn:aws:iam::384627057108:role/mr-lister-phase6-runtime-cfn-dev"
)
LOGICAL_ID: Final = "SellerCommandApiFunction"
PHYSICAL_ID: Final = "mr-lister-phase6-dev-seller-command-api"
RESOURCE_TYPE: Final = "AWS::Lambda::Function"
EXPECTED_CHANGE_SET_NAME: Final = (
    f"mr-lister-phase6-dev-seller-command-memory-{TARGET_TEMPLATE_SHA256[:12]}"
)

_GENERIC_ERROR = "Phase 6 seller-command runtime-envelope change-set evidence is invalid"
_CHANGE_SET_ID = re.compile(
    r"^arn:aws:cloudformation:us-west-2:384627057108:changeSet/"
    + re.escape(EXPECTED_CHANGE_SET_NAME)
    + r"/[0-9a-f-]{36}$"
)
_STANDARD_RESOURCE_KEYS: Final = {
    "Action",
    "Details",
    "LogicalResourceId",
    "PhysicalResourceId",
    "Replacement",
    "ResourceType",
    "Scope",
}
_PROPERTY_VALUE_RESOURCE_KEYS: Final = _STANDARD_RESOURCE_KEYS | {
    "AfterContext",
    "BeforeContext",
}
_STANDARD_TARGET_KEYS: Final = {
    "Attribute",
    "Name",
    "RequiresRecreation",
}
_PROPERTY_VALUE_TARGET_KEYS: Final = _STANDARD_TARGET_KEYS | {
    "AfterValue",
    "AttributeChangeType",
    "BeforeValue",
    "Path",
}


class Phase6SellerCommandRuntimeEnvelopeChangeSetError(RuntimeError):
    """A value-free seller-command change-set verification failure."""


@dataclass(frozen=True, slots=True)
class VerifiedPhase6SellerCommandRuntimeEnvelopeChangeSet:
    format: str
    change_set_id: str
    change_set_name: str
    predecessor_template_sha256: str
    target_template_sha256: str


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _read_json(path: Path) -> Mapping[str, Any]:
    raw = path.read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError
    return _mapping(json.loads(raw))


def verify_phase6_seller_command_runtime_envelope_change_set(
    *,
    change_set: Mapping[str, Any],
    predecessor_template_sha256: str,
    target_template_sha256: str,
) -> VerifiedPhase6SellerCommandRuntimeEnvelopeChangeSet:
    """Reject anything except the sealed in-place 256 MB to 512 MB modification."""

    try:
        change_set_id = change_set.get("ChangeSetId")
        change_set_name = change_set.get("ChangeSetName")
        if (
            predecessor_template_sha256 != PREDECESSOR_TEMPLATE_SHA256
            or target_template_sha256 != TARGET_TEMPLATE_SHA256
            or not isinstance(change_set_id, str)
            or _CHANGE_SET_ID.fullmatch(change_set_id) is None
            or not isinstance(change_set_name, str)
            or change_set_name != EXPECTED_CHANGE_SET_NAME
            or change_set_name != change_set_id.split("/")[-2]
            or change_set.get("StackId") != STACK_ID
            or change_set.get("StackName") != "mr-lister-phase6-dev"
            or change_set.get("Status") != "CREATE_COMPLETE"
            or change_set.get("ExecutionStatus") != "AVAILABLE"
            or change_set.get("NextToken") is not None
        ):
            raise ValueError
        if "ChangeSetType" in change_set and change_set.get("ChangeSetType") != "UPDATE":
            raise ValueError
        if "RoleARN" in change_set and change_set.get("RoleARN") != EXPECTED_EXECUTION_ROLE_ARN:
            raise ValueError

        changes = change_set.get("Changes")
        if not isinstance(changes, list) or len(changes) != 1:
            raise ValueError
        item = _mapping(changes[0])
        if item.get("Type") != "Resource" or set(item) != {"Type", "ResourceChange"}:
            raise ValueError
        resource = _mapping(item.get("ResourceChange"))
        standard_describe = set(resource) == _STANDARD_RESOURCE_KEYS
        property_value_describe = set(resource) == _PROPERTY_VALUE_RESOURCE_KEYS
        if (
            not (standard_describe or property_value_describe)
            or resource.get("Action") != "Modify"
            or resource.get("LogicalResourceId") != LOGICAL_ID
            or resource.get("PhysicalResourceId") != PHYSICAL_ID
            or resource.get("ResourceType") != RESOURCE_TYPE
            or resource.get("Replacement") != "False"
            or resource.get("Scope") != ["Properties"]
        ):
            raise ValueError

        details = resource.get("Details")
        if not isinstance(details, list) or len(details) != 1:
            raise ValueError
        detail = _mapping(details[0])
        target = _mapping(detail.get("Target"))
        if (
            set(detail) != {"ChangeSource", "Evaluation", "Target"}
            or detail.get("ChangeSource") != "DirectModification"
            or detail.get("Evaluation") != "Static"
            or target.get("Attribute") != "Properties"
            or target.get("Name") != "MemorySize"
            or target.get("RequiresRecreation") != "Never"
        ):
            raise ValueError

        if standard_describe:
            if set(target) != _STANDARD_TARGET_KEYS:
                raise ValueError
        elif (
            set(target) != _PROPERTY_VALUE_TARGET_KEYS
            or target.get("AttributeChangeType") != "Modify"
            or target.get("Path") != "/Properties/MemorySize"
            or target.get("BeforeValue") != "256"
            or target.get("AfterValue") != "512"
            or not isinstance(resource.get("BeforeContext"), str)
            or not resource.get("BeforeContext")
            or not isinstance(resource.get("AfterContext"), str)
            or not resource.get("AfterContext")
            or resource.get("BeforeContext") == resource.get("AfterContext")
        ):
            raise ValueError

        return VerifiedPhase6SellerCommandRuntimeEnvelopeChangeSet(
            format=FORMAT,
            change_set_id=change_set_id,
            change_set_name=change_set_name,
            predecessor_template_sha256=PREDECESSOR_TEMPLATE_SHA256,
            target_template_sha256=TARGET_TEMPLATE_SHA256,
        )
    except Exception:
        raise Phase6SellerCommandRuntimeEnvelopeChangeSetError(_GENERIC_ERROR) from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--change-set-json", type=Path, required=True)
    parser.add_argument("--predecessor-template-sha256", required=True)
    parser.add_argument("--target-template-sha256", required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        verified = verify_phase6_seller_command_runtime_envelope_change_set(
            change_set=_read_json(arguments.change_set_json),
            predecessor_template_sha256=arguments.predecessor_template_sha256,
            target_template_sha256=arguments.target_template_sha256,
        )
        output = (json.dumps(asdict(verified), indent=2, sort_keys=True) + "\n").encode()
        if arguments.output is not None:
            arguments.output.write_bytes(output)
        else:
            print(output.decode(), end="")
    except (OSError, ValueError, Phase6SellerCommandRuntimeEnvelopeChangeSetError) as error:
        print(
            _GENERIC_ERROR
            if not isinstance(error, Phase6SellerCommandRuntimeEnvelopeChangeSetError)
            else str(error)
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
