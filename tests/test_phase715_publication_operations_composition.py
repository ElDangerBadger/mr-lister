"""Composition and containment tests for source-only Phase 7 operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import md5
from pathlib import Path
from typing import Any

import pytest

import mr_lister.cloud.phase7_operations_composition as composition
from mr_lister.cloud.phase7_configuration import load_phase7_read_configuration
from mr_lister.cloud.phase7_operations import (
    Phase7PublicationDispatcherHandler,
    Phase7PublicationRecoveryHandler,
    Phase7PublicationRetentionHandler,
)
from mr_lister.publication.orchestration_recovery import (
    PublicationPreDispatchDeadlineEnvelope,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
MACHINE_ARN = "arn:aws:states:us-west-2:123456789012:stateMachine:mr-lister-phase7-dev-publication"
QUEUE_URL = (
    "https://sqs.us-west-2.amazonaws.com/123456789012/mr-lister-phase7-dev-publication-recovery"
)
NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)


def _environment() -> dict[str, object]:
    return {
        "AWS_REGION": "us-west-2",
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_RELEASE_FINGERPRINT": "a" * 64,
        "MR_LISTER_COGNITO_ISSUER": (
            "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Phase715Pool"
        ),
        "MR_LISTER_COGNITO_CLIENT_ID": "phase715client123",
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": str(PROFILE_PATH),
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
    }


class InertDynamoDB:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_item(self, **_request: Any) -> object:
        self.calls.append("get_item")
        raise AssertionError("composition read DynamoDB")

    def query(self, **_request: Any) -> object:
        self.calls.append("query")
        raise AssertionError("composition queried DynamoDB")

    def transact_write_items(self, **_request: Any) -> object:
        self.calls.append("transact_write_items")
        raise AssertionError("composition wrote DynamoDB")

    def update_item(self, **_request: Any) -> object:
        self.calls.append("update_item")
        raise AssertionError("composition updated DynamoDB")


class InertStepFunctions:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start_execution(self, **_request: Any) -> object:
        self.calls.append("start_execution")
        raise AssertionError("composition started a workflow")

    def describe_execution(self, **_request: Any) -> object:
        self.calls.append("describe_execution")
        raise AssertionError("composition described a workflow")

    def redrive_execution(self, **_request: Any) -> object:
        self.calls.append("redrive_execution")
        raise AssertionError("composition redrove a workflow")


class InertSQS:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_message(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        raise AssertionError("composition sent a recovery message")


class RecordingSQS:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_message(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        body = request["MessageBody"]
        return {
            "MessageId": "deadline-message-one",
            "MD5OfMessageBody": md5(
                body.encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest(),
        }


class BrokenSQS:
    def __init__(self, result: object) -> None:
        self.result = result

    def send_message(self, **_request: Any) -> Any:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_all_three_real_graphs_compose_without_dependency_io() -> None:
    dynamodb = InertDynamoDB()
    step_functions = InertStepFunctions()
    sqs = InertSQS()
    configuration = load_phase7_read_configuration(_environment())

    dispatcher = composition.compose_publication_dispatcher_handler(
        state_table=configuration.state_table,
        state_machine_arn=MACHINE_ARN,
        dynamodb=dynamodb,
        step_functions=step_functions,
        sqs=sqs,
        recovery_queue_url=QUEUE_URL,
        clock=lambda: NOW,
    )
    recovery = composition.compose_publication_recovery_handler(
        state_table=configuration.state_table,
        state_machine_arn=MACHINE_ARN,
        release_manifest_fingerprint=configuration.release_manifest_fingerprint,
        exact_profile=configuration.profile.exact,
        eligibility=configuration.eligibility,
        dynamodb=dynamodb,
        step_functions=step_functions,
        clock=lambda: NOW,
    )
    retention = composition.compose_publication_retention_handler(
        state_table=configuration.state_table,
        dynamodb=dynamodb,
        clock=lambda: NOW,
    )

    assert isinstance(dispatcher, Phase7PublicationDispatcherHandler)
    assert isinstance(recovery, Phase7PublicationRecoveryHandler)
    assert isinstance(retention, Phase7PublicationRetentionHandler)
    assert dynamodb.calls == []
    assert step_functions.calls == []
    assert sqs.calls == []


def test_deadline_sink_emits_only_the_exact_replay_safe_queue_body() -> None:
    sqs = RecordingSQS()
    sink = composition._SqsPublicationDeadlineSettlementSink(
        client=sqs,
        queue_url=QUEUE_URL,
    )
    sink.send(
        PublicationPreDispatchDeadlineEnvelope(
            owner_id="a" * 64,
            aggregate_id="publication_one",
            work_request_id="publication_work_one",
            verification_deadline=NOW,
        )
    )
    assert sqs.calls == [
        {
            "QueueUrl": QUEUE_URL,
            "MessageBody": json.dumps(
                {
                    "kind": "pre_dispatch_deadline_elapsed",
                    "owner_id": "a" * 64,
                    "aggregate_id": "publication_one",
                    "work_request_id": "publication_work_one",
                    "verification_deadline": NOW.isoformat().replace("+00:00", "Z"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    ]


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"MessageId": "deadline-message-one", "MD5OfMessageBody": "0" * 32},
        RuntimeError("private owner payload"),
    ],
)
def test_deadline_sink_treats_missing_or_ambiguous_acknowledgement_as_retryable(
    result: object,
) -> None:
    sink = composition._SqsPublicationDeadlineSettlementSink(
        client=BrokenSQS(result),
        queue_url=QUEUE_URL,
    )
    envelope = PublicationPreDispatchDeadlineEnvelope(
        owner_id="a" * 64,
        aggregate_id="publication_one",
        work_request_id="publication_work_one",
        verification_deadline=NOW,
    )
    with pytest.raises(RuntimeError, match="failed safely") as captured:
        sink.send(envelope)
    assert captured.value.__cause__ is None
    assert "private owner payload" not in str(captured.value)


def test_deadline_sink_rejects_non_aws_queue_url_without_dependency_io() -> None:
    sqs = InertSQS()
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        composition._SqsPublicationDeadlineSettlementSink(
            client=sqs,
            queue_url="https://attacker.example/recovery",
        )
    assert sqs.calls == []


@pytest.mark.parametrize(
    "composer",
    [
        lambda dynamodb, states, sqs, config: composition.compose_publication_dispatcher_handler(
            state_table=config.state_table,
            state_machine_arn=MACHINE_ARN,
            dynamodb=dynamodb,
            step_functions=states,
            sqs=sqs,
            recovery_queue_url=QUEUE_URL,
        ),
        lambda dynamodb, states, _sqs, config: composition.compose_publication_recovery_handler(
            state_table=config.state_table,
            state_machine_arn=MACHINE_ARN,
            release_manifest_fingerprint=config.release_manifest_fingerprint,
            exact_profile=config.profile.exact,
            eligibility=config.eligibility,
            dynamodb=dynamodb,
            step_functions=states,
        ),
        lambda dynamodb, _states, _sqs, config: composition.compose_publication_retention_handler(
            state_table=config.state_table,
            dynamodb=dynamodb,
        ),
    ],
)
def test_missing_dependency_methods_fail_at_construction(composer: Any) -> None:
    configuration = load_phase7_read_configuration(_environment())
    with pytest.raises(RuntimeError, match="dependency is unavailable"):
        composer(object(), object(), object(), configuration)


def test_composition_has_no_default_client_provider_secret_or_runtime_registration() -> None:
    path = ROOT / "src/mr_lister/cloud/phase7_operations_composition.py"
    source = path.read_text(encoding="utf-8")
    lowered = source.casefold()
    for forbidden in (
        "import boto3",
        "provider_boundary",
        "provider_coordinator",
        "provider_credentials",
        "secretsmanager",
        "get_secret_value",
        "default_client",
        "lambda_handler",
    ):
        assert forbidden not in lowered

    active_entrypoints = (ROOT / "src/mr_lister/cloud/phase7_entrypoints.py").read_text(
        encoding="utf-8"
    )
    assert "phase7_operations" not in active_entrypoints
    assert not (ROOT / "src/mr_lister/cloud/phase7_production_entrypoints.py").exists()
