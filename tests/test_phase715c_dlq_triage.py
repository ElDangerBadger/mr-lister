"""Fail-closed tests for Phase 7.15C one-message DLQ tooling."""

from __future__ import annotations

import inspect
import json
import stat
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import md5, sha256
from pathlib import Path
from typing import Any

import pytest

import tools.phase715c_dlq_triage as dlq
from tools.phase715c_dlq_triage import (
    DlqAction,
    DlqAuthority,
    DlqClassification,
    Phase715cDlqError,
    execute_exact_dlq_action,
    required_action_confirmation,
    triage_one_dlq_message,
    write_private_triage_session,
)

ACCOUNT = "123456789012"
REGION = "us-west-2"
DLQ_NAME = "mr-lister-phase7-dev-publication-recovery-dlq"
RECOVERY_NAME = "mr-lister-phase7-dev-publication-recovery"
DLQ_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{DLQ_NAME}"
RECOVERY_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{RECOVERY_NAME}"
DLQ_ARN = f"arn:aws:sqs:{REGION}:{ACCOUNT}:{DLQ_NAME}"
RECOVERY_ARN = f"arn:aws:sqs:{REGION}:{ACCOUNT}:{RECOVERY_NAME}"
MESSAGE_ID = "message_one"
RECEIPT_HANDLE = "private-receipt-handle-never-print"
MACHINE_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:phase7-publication"
EXECUTION_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:execution:phase7-publication:publication_one"
NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)


def _authority() -> DlqAuthority:
    return DlqAuthority(
        dead_letter_queue_url=DLQ_URL,
        dead_letter_queue_arn=DLQ_ARN,
        recovery_queue_url=RECOVERY_URL,
        recovery_queue_arn=RECOVERY_ARN,
        max_receive_count=12,
    )


def _workflow_body() -> str:
    return json.dumps(
        {
            "execution_arn": EXECUTION_ARN,
            "machine_arn": MACHINE_ARN,
            "status": "FAILED",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _deadline_body() -> str:
    return json.dumps(
        {
            "aggregate_id": "publication_one",
            "kind": "pre_dispatch_deadline_elapsed",
            "owner_id": "a" * 64,
            "verification_deadline": NOW.isoformat().replace("+00:00", "Z"),
            "work_request_id": "publication_work_one",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _message(body: str) -> dict[str, Any]:
    return {
        "Attributes": {
            "ApproximateReceiveCount": "12",
            "SentTimestamp": "1788296400000",
        },
        "Body": body,
        "MD5OfBody": md5(body.encode(), usedforsecurity=False).hexdigest(),
        "MessageId": MESSAGE_ID,
        "ReceiptHandle": RECEIPT_HANDLE,
    }


class RecordingSqs:
    def __init__(self, response: object) -> None:
        self.response = response
        self.attribute_calls: list[dict[str, Any]] = []
        self.receive_calls: list[dict[str, Any]] = []
        self.send_calls: list[dict[str, Any]] = []
        self.send_response: object | None = None

    def get_queue_attributes(self, **request: Any) -> dict[str, Any]:
        self.attribute_calls.append(request)
        queue_url = request["QueueUrl"]
        if queue_url == DLQ_URL:
            attributes = {
                "QueueArn": DLQ_ARN,
                "SqsManagedSseEnabled": "true",
            }
        else:
            attributes = {
                "QueueArn": RECOVERY_ARN,
                "SqsManagedSseEnabled": "true",
                "RedrivePolicy": json.dumps(
                    {
                        "deadLetterTargetArn": DLQ_ARN,
                        "maxReceiveCount": "12",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        return {
            "Attributes": attributes,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def receive_message(self, **request: Any) -> Any:
        self.receive_calls.append(request)
        return self.response

    def send_message(self, **request: Any) -> Any:
        self.send_calls.append(request)
        if self.send_response is not None:
            return self.send_response
        body = request["MessageBody"]
        return {
            "MessageId": "destination_message_one",
            "MD5OfMessageBody": md5(
                body.encode(),
                usedforsecurity=False,
            ).hexdigest(),
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }


def _triage(body: str) -> tuple[RecordingSqs, dlq.DlqTriageSession]:
    client = RecordingSqs({"Messages": [_message(body)]})
    return client, triage_one_dlq_message(client=client, authority=_authority())


@pytest.mark.parametrize(
    ("body", "classification"),
    [
        (_workflow_body(), DlqClassification.WORKFLOW_FAILURE),
        (_deadline_body(), DlqClassification.PRE_DISPATCH_DEADLINE),
    ],
)
def test_triage_classifies_only_closed_recovery_envelopes_as_resendable(
    body: str,
    classification: DlqClassification,
) -> None:
    client, session = _triage(body)

    assert session.plan.classification is classification
    assert session.plan.resend_allowed is True
    assert session.plan.replay_boundary == "RECOVERY_HANDLER_STRONG_AUTHORITY_REBIND"
    assert session.plan.blocker is None
    assert session.plan.delete_allowed is False
    assert session.plan.delete_blocker == "DURABLE_RECOVERY_READBACK_NOT_IMPLEMENTED"
    assert session.plan.body_sha256 == sha256(body.encode()).hexdigest()
    public = session.plan.document()
    assert body not in repr(public)
    assert MESSAGE_ID not in repr(public)
    assert RECEIPT_HANDLE not in repr(public)
    assert body not in repr(session)
    assert RECEIPT_HANDLE not in repr(session)
    assert client.attribute_calls == [
        {
            "QueueUrl": DLQ_URL,
            "AttributeNames": ["QueueArn", "SqsManagedSseEnabled"],
        },
        {
            "QueueUrl": RECOVERY_URL,
            "AttributeNames": ["QueueArn", "SqsManagedSseEnabled", "RedrivePolicy"],
        },
    ]
    assert client.receive_calls == [
        {
            "QueueUrl": DLQ_URL,
            "MaxNumberOfMessages": 1,
            "WaitTimeSeconds": 0,
            "VisibilityTimeout": 0,
            "AttributeNames": ["ApproximateReceiveCount", "SentTimestamp"],
        }
    ]


def test_empty_dlq_returns_a_body_free_non_actionable_plan() -> None:
    client = RecordingSqs({})
    session = triage_one_dlq_message(client=client, authority=_authority())

    assert session.plan.classification is DlqClassification.EMPTY
    assert session.plan.resend_allowed is False
    assert session.plan.blocker == "DLQ_EMPTY"
    assert session.body is None
    assert session.message_id is None
    assert session.receipt_handle is None
    assert client.send_calls == []


@pytest.mark.parametrize(
    ("body", "classification", "blocker"),
    [
        ("private malformed body {", DlqClassification.UNSUPPORTED, "MALFORMED_MESSAGE_BODY"),
        (
            json.dumps(
                {
                    "requestContext": {},
                    "requestPayload": {},
                    "responseContext": {},
                    "responsePayload": {},
                    "timestamp": "private timestamp",
                    "version": "1.0",
                }
            ),
            DlqClassification.EVENT_SOURCE_FAILURE,
            "SOURCE_EVENT_REPLAY_AUTHORITY_NOT_IMPLEMENTED",
        ),
        (
            json.dumps({"unknown": "private payload"}),
            DlqClassification.UNSUPPORTED,
            "UNSUPPORTED_MESSAGE_BODY",
        ),
    ],
)
def test_unproven_messages_are_classifier_only_and_never_actionable(
    body: str,
    classification: DlqClassification,
    blocker: str,
) -> None:
    client, session = _triage(body)
    client.attribute_calls.clear()

    assert session.plan.classification is classification
    assert session.plan.resend_allowed is False
    assert session.plan.blocker == blocker
    confirmation = required_action_confirmation(DlqAction.RESEND, session.plan)
    with pytest.raises(Phase715cDlqError, match="refused safely") as captured:
        execute_exact_dlq_action(
            client=client,
            session=session,
            action=DlqAction.RESEND,
            expected_plan_sha256=session.plan.plan_sha256,
            expected_body_sha256=session.plan.body_sha256 or "0" * 64,
            confirmation=confirmation,
        )
    assert captured.value.__cause__ is None
    assert body not in str(captured.value)
    assert client.attribute_calls == []
    assert client.send_calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda message: message.update(MD5OfBody="0" * 32),
        lambda message: message.update(MessageAttributes={}),
        lambda message: message["Attributes"].update(ApproximateReceiveCount="01"),
        lambda message: message.update(Body=""),
    ],
)
def test_triage_rejects_message_drift_without_exposing_raw_values(mutate: Any) -> None:
    body = _workflow_body()
    message = _message(body)
    mutate(message)
    private_marker = "private-receipt-handle-never-print"

    with pytest.raises(Phase715cDlqError, match="failed safely") as captured:
        triage_one_dlq_message(
            client=RecordingSqs({"Messages": [message]}),
            authority=_authority(),
        )
    assert captured.value.__cause__ is None
    assert private_marker not in str(captured.value)
    assert body not in str(captured.value)


def test_triage_rejects_queue_readback_response_expansion() -> None:
    class ExpandedQueueReadback(RecordingSqs):
        def get_queue_attributes(self, **request: Any) -> dict[str, Any]:
            response = super().get_queue_attributes(**request)
            response["unexpected"] = "private expansion"
            return response

    with pytest.raises(Phase715cDlqError, match="failed safely") as captured:
        triage_one_dlq_message(
            client=ExpandedQueueReadback({"Messages": [_message(_workflow_body())]}),
            authority=_authority(),
        )

    assert "private expansion" not in str(captured.value)


def test_exact_resend_is_hash_bound_and_always_retains_the_source() -> None:
    body = _workflow_body()
    client, session = _triage(body)
    confirmation = required_action_confirmation(DlqAction.RESEND, session.plan)
    client.attribute_calls.clear()

    result = execute_exact_dlq_action(
        client=client,
        session=session,
        action=DlqAction.RESEND,
        expected_plan_sha256=session.plan.plan_sha256,
        expected_body_sha256=session.plan.body_sha256 or "",
        confirmation=confirmation,
    )

    assert client.send_calls == [{"QueueUrl": RECOVERY_URL, "MessageBody": body}]
    assert result["acknowledgement"] == "EXACT_BODY_MD5_ACCEPTED_SOURCE_RETAINED"
    assert result["source_deleted"] is False
    assert result["source_disposition"] == "RETAINED_PENDING_DURABLE_RECOVERY_READBACK"
    assert body not in repr(result)
    assert MESSAGE_ID not in repr(result)
    assert RECEIPT_HANDLE not in repr(result)
    assert len(str(result["action_sha256"])) == 64


@pytest.mark.parametrize(
    "gate",
    ["plan", "body", "confirmation", "session_body", "plan_content"],
)
def test_resend_refuses_any_digest_or_confirmation_drift_before_aws_io(gate: str) -> None:
    client, session = _triage(_workflow_body())
    expected_plan = session.plan.plan_sha256
    expected_body = session.plan.body_sha256 or ""
    confirmation = required_action_confirmation(DlqAction.RESEND, session.plan)
    if gate == "plan":
        expected_plan = "0" * 64
    elif gate == "body":
        expected_body = "0" * 64
    elif gate == "confirmation":
        confirmation = "phase7.15c:RESEND:" + "0" * 64
    elif gate == "session_body":
        session = replace(session, body=(session.body or "") + " ")
    else:
        session = replace(session, plan=replace(session.plan, delete_allowed=True))
    client.attribute_calls.clear()

    with pytest.raises(Phase715cDlqError, match="refused safely"):
        execute_exact_dlq_action(
            client=client,
            session=session,
            action=DlqAction.RESEND,
            expected_plan_sha256=expected_plan,
            expected_body_sha256=expected_body,
            confirmation=confirmation,
        )

    assert client.attribute_calls == []
    assert client.send_calls == []


def test_resend_rejects_ambiguous_destination_acknowledgement_and_keeps_source() -> None:
    body = _workflow_body()
    client, session = _triage(body)
    client.send_response = {
        "MessageId": "destination_message_one",
        "MD5OfMessageBody": "0" * 32,
    }

    with pytest.raises(Phase715cDlqError, match="refused safely"):
        execute_exact_dlq_action(
            client=client,
            session=session,
            action=DlqAction.RESEND,
            expected_plan_sha256=session.plan.plan_sha256,
            expected_body_sha256=session.plan.body_sha256 or "",
            confirmation=required_action_confirmation(DlqAction.RESEND, session.plan),
        )

    assert client.send_calls == [{"QueueUrl": RECOVERY_URL, "MessageBody": body}]


def test_delete_and_bulk_move_authority_are_absent() -> None:
    source = inspect.getsource(dlq)

    with pytest.raises(ValueError):
        DlqAction("DELETE")
    assert "delete_message" not in source
    assert "startmessagemovetask" not in source.replace("_", "").lower()
    assert "boto3" not in source


def test_raw_session_can_only_be_created_owner_only_under_private_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    private = repository / ".mr_lister_private" / "phase715c-operations"
    monkeypatch.setattr(dlq, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(dlq, "PRIVATE_ROOT", private)
    _client, session = _triage(_workflow_body())
    output = private / "run-one" / "raw-session.json"

    digest = write_private_triage_session(session, output)

    payload = output.read_bytes()
    assert digest == sha256(payload).hexdigest()
    raw_document = json.loads(payload)
    assert raw_document["body"] == session.body
    assert raw_document["receipt_handle"] == session.receipt_handle
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    with pytest.raises(Phase715cDlqError, match="refused"):
        write_private_triage_session(session, output)
    with pytest.raises(Phase715cDlqError, match="refused"):
        write_private_triage_session(session, repository / "outside.json")
    assert not (repository / "outside.json").exists()


def test_private_writer_refuses_symlinked_directory_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    private = repository / ".mr_lister_private" / "phase715c-operations"
    private.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (private / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(dlq, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(dlq, "PRIVATE_ROOT", private)
    _client, session = _triage(_workflow_body())

    with pytest.raises(Phase715cDlqError, match="refused"):
        write_private_triage_session(session, private / "linked" / "raw.json")

    assert not (outside / "raw.json").exists()
