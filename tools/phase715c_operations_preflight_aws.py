#!/usr/bin/env python3
"""Run the exact deployed Phase 7.15C operations preflight through narrow AWS reads.

The adapter is fixed to the non-root development profile, account, Region, and Phase 6/7
stacks.  It discovers no arbitrary resource, invokes no Lambda, and exposes only CloudFormation
stack reads, DynamoDB ``DescribeTable``/``Query``, Lambda event-source-mapping reads, and
EventBridge rule reads.  Dynamic resource identifiers are bound into hashes rather than emitted.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, Protocol

from tools.phase715c_operations_preflight import (
    ExpectedEventBridgeRule,
    ExpectedEventSourceMapping,
    OperationsPreflightAuthority,
    run_phase715c_operations_preflight,
)

PROFILE: Final = "mr-lister-dev"
ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
PHASE6_STACK_NAME: Final = "mr-lister-phase6-dev"
PHASE7_STACK_NAME: Final = "mr-lister-phase7-dev"
TABLE_NAME: Final = PHASE6_STACK_NAME
TABLE_ARN: Final = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{TABLE_NAME}"
RECOVERY_QUEUE_NAME: Final = "mr-lister-phase7-dev-publication-recovery"
RECOVERY_QUEUE_ARN: Final = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:{RECOVERY_QUEUE_NAME}"
ADAPTER_FORMAT: Final = "mr-lister-phase7.15c-operations-preflight-aws-v1"

_STACK_ID = re.compile(
    rf"^arn:aws:cloudformation:{re.escape(REGION)}:{ACCOUNT_ID}:"
    r"stack/(?P<name>mr-lister-phase[67]-dev)/[A-Za-z0-9-]{8,128}$"
)
_STREAM_ARN = re.compile(rf"^{re.escape(TABLE_ARN)}/stream/[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T.+$")
_UUID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,127}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")


class Phase715cOperationsPreflightAwsError(RuntimeError):
    """A value-free refusal for AWS authority or readback drift."""


class AwsClientProvider(Protocol):
    """Construct one client from the adapter's closed service allowlist."""

    def client(self, service_name: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class _MappingSpec:
    logical_id: str
    function_name: str
    event_source: str

    @property
    def function_arn(self) -> str:
        return f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{self.function_name}"


@dataclass(frozen=True, slots=True)
class _RuleSpec:
    logical_id: str
    name: str

    @property
    def arn(self) -> str:
        return f"arn:aws:events:{REGION}:{ACCOUNT_ID}:rule/{self.name}"


_MAPPING_SPECS: Final = (
    _MappingSpec(
        logical_id="PublicationDispatcherStreamMapping",
        function_name="mr-lister-phase7-dev-publication-dispatcher",
        event_source="phase6_stream",
    ),
    _MappingSpec(
        logical_id="PublicationRecoveryQueueMapping",
        function_name="mr-lister-phase7-dev-publication-recovery",
        event_source="recovery_queue",
    ),
    _MappingSpec(
        logical_id="PublicationRetentionStreamMapping",
        function_name="mr-lister-phase7-dev-publication-retention",
        event_source="phase6_stream",
    ),
)
_RULE_SPECS: Final = (
    _RuleSpec(
        logical_id="PublicationDueWorkSweepRule",
        name="mr-lister-phase7-dev-publication-due-sweep",
    ),
    _RuleSpec(
        logical_id="PublicationRecoverySweepRule",
        name="mr-lister-phase7-dev-publication-recovery-sweep",
    ),
    _RuleSpec(
        logical_id="PublicationWorkflowFailureRule",
        name="mr-lister-phase7-dev-publication-workflow-failure",
    ),
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _pairs(value: object, *, key: str, field: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for raw in value:
        item = _mapping(raw)
        name = item.get(key)
        content = item.get(field)
        if not isinstance(name, str) or not isinstance(content, str) or name in result:
            raise ValueError
        result[name] = content
    return result


def _stack(client: object, *, name: str) -> Mapping[str, Any]:
    response = _mapping(client.describe_stacks(StackName=name))
    stacks = response.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise ValueError
    stack = _mapping(stacks[0])
    stack_id = stack.get("StackId")
    matched = _STACK_ID.fullmatch(str(stack_id))
    if stack.get("StackName") != name or matched is None or matched.group("name") != name:
        raise ValueError
    return stack


def _phase6_authority(cloudformation: object) -> None:
    stack = _stack(cloudformation, name=PHASE6_STACK_NAME)
    outputs = _pairs(stack.get("Outputs"), key="OutputKey", field="OutputValue")
    if stack.get("StackStatus") != "UPDATE_COMPLETE" or outputs.get("StateTableName") != TABLE_NAME:
        raise ValueError


def _phase7_stream_authority(cloudformation: object) -> str:
    stack = _stack(cloudformation, name=PHASE7_STACK_NAME)
    parameters = _pairs(stack.get("Parameters"), key="ParameterKey", field="ParameterValue")
    outputs = _pairs(stack.get("Outputs"), key="OutputKey", field="OutputValue")
    stream_arn = parameters.get("StateTableStreamArn")
    if (
        stack.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
        or parameters.get("ActivationMode") != "PRODUCTION_DISABLED"
        or parameters.get("EnvironmentName") != "dev"
        or parameters.get("StateTableArn") != TABLE_ARN
        or not isinstance(stream_arn, str)
        or _STREAM_ARN.fullmatch(stream_arn) is None
        or outputs.get("DeploymentReadiness") != "PRODUCTION_DISABLED"
        or outputs.get("ResourceInstantiationPossible") != "true"
        or outputs.get("PublicationQueryRegistered") != "false"
        or outputs.get("PublicationRequestRegistered") != "false"
        or outputs.get("PublicationWorkerTriggered") != "false"
        or outputs.get("SellerPublicationEnabled") != "false"
        or outputs.get("ProviderMutationEnabled") != "false"
    ):
        raise ValueError
    return stream_arn


def _mapping_readbacks(
    client: object,
    *,
    stream_arn: str,
) -> tuple[tuple[ExpectedEventSourceMapping, ...], list[dict[str, object]]]:
    expected: list[ExpectedEventSourceMapping] = []
    observations: list[dict[str, object]] = []
    for spec in _MAPPING_SPECS:
        source_arn = stream_arn if spec.event_source == "phase6_stream" else RECOVERY_QUEUE_ARN
        listed = _mapping(
            client.list_event_source_mappings(
                FunctionName=spec.function_name,
                MaxItems=100,
            )
        )
        rows = listed.get("EventSourceMappings")
        if listed.get("NextMarker") is not None or not isinstance(rows, list) or len(rows) != 1:
            raise ValueError
        summary = _mapping(rows[0])
        uuid = summary.get("UUID")
        if (
            not isinstance(uuid, str)
            or _UUID.fullmatch(uuid) is None
            or summary.get("FunctionArn") != spec.function_arn
            or summary.get("EventSourceArn") != source_arn
            or summary.get("State") != "Disabled"
        ):
            raise ValueError
        detail = _mapping(client.get_event_source_mapping(UUID=uuid))
        if (
            detail.get("UUID") != uuid
            or detail.get("FunctionArn") != spec.function_arn
            or detail.get("EventSourceArn") != source_arn
            or detail.get("State") != "Disabled"
        ):
            raise ValueError
        item = ExpectedEventSourceMapping(
            logical_id=spec.logical_id,
            uuid=uuid,
            function_arn=spec.function_arn,
            event_source_arn=source_arn,
        )
        expected.append(item)
        observations.append(
            {
                "enabled": False,
                "event_source_arn": source_arn,
                "function_arn": spec.function_arn,
                "logical_id": spec.logical_id,
                "state": "Disabled",
                "uuid": uuid,
            }
        )
    return tuple(expected), observations


def _rule_readbacks(
    client: object,
) -> tuple[tuple[ExpectedEventBridgeRule, ...], list[dict[str, object]]]:
    expected: list[ExpectedEventBridgeRule] = []
    observations: list[dict[str, object]] = []
    for spec in _RULE_SPECS:
        response = _mapping(client.describe_rule(Name=spec.name, EventBusName="default"))
        if (
            response.get("Name") != spec.name
            or response.get("Arn") != spec.arn
            or response.get("State") != "DISABLED"
            or response.get("EventBusName", "default") != "default"
        ):
            raise ValueError
        item = ExpectedEventBridgeRule(
            logical_id=spec.logical_id,
            name=spec.name,
            arn=spec.arn,
        )
        expected.append(item)
        observations.append(
            {
                "arn": spec.arn,
                "logical_id": spec.logical_id,
                "name": spec.name,
                "state": "DISABLED",
            }
        )
    return tuple(expected), observations


class _StreamBoundDynamoDB:
    """Expose only the core protocol while binding the table's active stream."""

    __slots__ = ("_client", "_stream_arn")

    def __init__(self, client: object, *, stream_arn: str) -> None:
        self._client = client
        self._stream_arn = stream_arn

    def describe_table(self, **request: Any) -> Mapping[str, Any]:
        response = _mapping(self._client.describe_table(**request))
        table = _mapping(response.get("Table"))
        if table.get("LatestStreamArn") != self._stream_arn:
            raise ValueError
        return response

    def query(self, **request: Any) -> Mapping[str, Any]:
        return _mapping(self._client.query(**request))


def run_phase715c_operations_preflight_aws(
    *,
    provider: AwsClientProvider,
) -> dict[str, object]:
    """Discover the fixed deployed authority and return identifier-free evidence."""

    try:
        if provider is None:
            raise ValueError
        cloudformation = provider.client("cloudformation")
        _phase6_authority(cloudformation)
        stream_arn = _phase7_stream_authority(cloudformation)
        expected_mappings, observed_mappings = _mapping_readbacks(
            provider.client("lambda"),
            stream_arn=stream_arn,
        )
        expected_rules, observed_rules = _rule_readbacks(provider.client("events"))
        authority = OperationsPreflightAuthority(
            table_name=TABLE_NAME,
            table_arn=TABLE_ARN,
            event_source_mappings=expected_mappings,
            eventbridge_rules=expected_rules,
        )
        core = run_phase715c_operations_preflight(
            client=_StreamBoundDynamoDB(
                provider.client("dynamodb"),
                stream_arn=stream_arn,
            ),
            authority=authority,
            trigger_observations={
                "event_source_mappings": observed_mappings,
                "eventbridge_rules": observed_rules,
            },
        )
        evidence_sha256 = core.get("evidence_sha256")
        triggers = core.get("triggers")
        if (
            core.get("result") != "passed"
            or not isinstance(evidence_sha256, str)
            or _FINGERPRINT.fullmatch(evidence_sha256) is None
            or triggers
            != {
                "mode": "DEPLOYED_DISABLED_READBACK",
                "readback_count": len(_MAPPING_SPECS) + len(_RULE_SPECS),
            }
        ):
            raise ValueError
        binding = {
            "event_source_mappings": observed_mappings,
            "eventbridge_rules": observed_rules,
            "phase6_stack": PHASE6_STACK_NAME,
            "phase7_stack": PHASE7_STACK_NAME,
            "table_arn": TABLE_ARN,
        }
        return {
            "authority_sha256": _fingerprint(binding),
            "core_evidence_sha256": evidence_sha256,
            "empty_query_readback_count": 4,
            "event_source_mapping_readback_count": len(_MAPPING_SPECS),
            "eventbridge_rule_readback_count": len(_RULE_SPECS),
            "format": ADAPTER_FORMAT,
            "result": "passed",
            "trigger_readback_count": len(_MAPPING_SPECS) + len(_RULE_SPECS),
        }
    except Phase715cOperationsPreflightAwsError:
        raise
    except Exception:
        raise Phase715cOperationsPreflightAwsError(
            "Phase 7.15C AWS operations preflight failed safely"
        ) from None


class _Boto3Provider:
    """Construct only fixed-profile, fixed-Region read clients."""

    _SERVICES: Final = frozenset({"cloudformation", "dynamodb", "events", "lambda"})

    def __init__(self) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise Phase715cOperationsPreflightAwsError("boto3 is unavailable") from None
        self._session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        self._config = Config(retries={"mode": "standard", "total_max_attempts": 1})

    def client(self, service_name: str) -> Any:
        if service_name not in self._SERVICES:
            raise Phase715cOperationsPreflightAwsError(
                "AWS service is outside the Phase 7.15C preflight boundary"
            )
        return self._session.client(service_name, config=self._config)


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def main(
    argv: Sequence[str] | None = None,
    *,
    provider_factory: Callable[[], AwsClientProvider] = _Boto3Provider,
) -> int:
    _parser().parse_args(argv)
    result = run_phase715c_operations_preflight_aws(provider=provider_factory())
    print(_canonical(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase715cOperationsPreflightAwsError as error:
        raise SystemExit(f"phase715c AWS operations preflight stopped: {error}") from None


__all__ = [
    "ACCOUNT_ID",
    "ADAPTER_FORMAT",
    "AwsClientProvider",
    "PHASE6_STACK_NAME",
    "PHASE7_STACK_NAME",
    "PROFILE",
    "Phase715cOperationsPreflightAwsError",
    "REGION",
    "run_phase715c_operations_preflight_aws",
]
