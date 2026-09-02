"""Adversarial tests for the fixed Phase 7.15C AWS DLQ adapter."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from hashlib import md5
from pathlib import Path
from typing import Any

import pytest

import tools.phase715c_dlq_triage as core
import tools.phase715c_dlq_triage_aws as adapter
from tools.phase715c_dlq_triage_aws import (
    Phase715cDlqAwsError,
    resend_phase715c_dlq_aws,
    triage_phase715c_dlq_aws,
)

PRIVATE_BODY_MARKER = "private-workflow-body-never-print"
MESSAGE_ID = "private_message_one"
RECEIPT_HANDLE = "private-receipt-handle-never-print"


def _workflow_body() -> str:
    return json.dumps(
        {
            "execution_arn": (
                f"arn:aws:states:{adapter.REGION}:{adapter.ACCOUNT_ID}:execution:"
                f"mr-lister-phase7-dev-publication:{PRIVATE_BODY_MARKER}"
            ),
            "machine_arn": (
                f"arn:aws:states:{adapter.REGION}:{adapter.ACCOUNT_ID}:stateMachine:"
                "mr-lister-phase7-dev-publication"
            ),
            "status": "FAILED",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class RecordingSqs:
    def __init__(self, *, body: str | None = None) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_queue_url(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("get_queue_url", request))
        urls = {
            adapter.DEAD_LETTER_QUEUE_NAME: adapter.DEAD_LETTER_QUEUE_URL,
            adapter.RECOVERY_QUEUE_NAME: adapter.RECOVERY_QUEUE_URL,
        }
        return {
            "QueueUrl": urls[request["QueueName"]],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def get_queue_attributes(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("get_queue_attributes", request))
        if request["QueueUrl"] == adapter.DEAD_LETTER_QUEUE_URL:
            attributes = {
                "QueueArn": adapter.DEAD_LETTER_QUEUE_ARN,
                "SqsManagedSseEnabled": "true",
            }
        else:
            attributes = {
                "QueueArn": adapter.RECOVERY_QUEUE_ARN,
                "SqsManagedSseEnabled": "true",
                "RedrivePolicy": json.dumps(
                    {
                        "deadLetterTargetArn": adapter.DEAD_LETTER_QUEUE_ARN,
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

    def receive_message(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("receive_message", request))
        if self.body is None:
            return {"ResponseMetadata": {"HTTPStatusCode": 200}}
        return {
            "Messages": [
                {
                    "Attributes": {
                        "ApproximateReceiveCount": "12",
                        "SentTimestamp": str(
                            int(datetime(2026, 9, 2, tzinfo=UTC).timestamp() * 1000)
                        ),
                    },
                    "Body": self.body,
                    "MD5OfBody": md5(
                        self.body.encode("utf-8"),
                        usedforsecurity=False,
                    ).hexdigest(),
                    "MessageId": MESSAGE_ID,
                    "ReceiptHandle": RECEIPT_HANDLE,
                }
            ],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def send_message(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("send_message", request))
        body = request["MessageBody"]
        return {
            "MessageId": "destination_message_one",
            "MD5OfMessageBody": md5(
                body.encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest(),
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }


class RecordingProvider:
    def __init__(self, sqs: RecordingSqs) -> None:
        self.sqs = sqs
        self.calls: list[str] = []

    def client(self, service_name: str) -> RecordingSqs:
        self.calls.append(service_name)
        if service_name != "sqs":
            raise AssertionError("adapter requested an out-of-bound service")
        return self.sqs


def _private_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    private = repository / ".mr_lister_private" / "phase715c-operations"
    monkeypatch.setattr(core, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(core, "PRIVATE_ROOT", private)
    return private


def test_triage_mode_is_aws_read_only_and_public_output_contains_no_raw_identifiers() -> None:
    body = _workflow_body()
    sqs = RecordingSqs(body=body)
    provider = RecordingProvider(sqs)

    result = triage_phase715c_dlq_aws(provider=provider)

    assert provider.calls == ["sqs"]
    assert [name for name, _request in sqs.calls] == [
        "get_queue_url",
        "get_queue_url",
        "get_queue_attributes",
        "get_queue_attributes",
        "receive_message",
    ]
    assert result["mode"] == "TRIAGE_READ_ONLY"
    assert result["classification"] == "WORKFLOW_FAILURE"
    assert result["resend_allowed"] is True
    assert result["private_session_saved"] is False
    public = json.dumps(result, sort_keys=True)
    for raw in (
        body,
        PRIVATE_BODY_MARKER,
        MESSAGE_ID,
        RECEIPT_HANDLE,
        adapter.ACCOUNT_ID,
        adapter.DEAD_LETTER_QUEUE_URL,
        adapter.RECOVERY_QUEUE_URL,
        adapter.DEAD_LETTER_QUEUE_ARN,
        adapter.RECOVERY_QUEUE_ARN,
    ):
        assert raw not in public


def test_explicit_private_capture_then_hash_bound_resend_retains_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = _private_root(tmp_path, monkeypatch)
    body = _workflow_body()
    sqs = RecordingSqs(body=body)
    provider = RecordingProvider(sqs)
    triage = triage_phase715c_dlq_aws(
        provider=provider,
        save_private_session=True,
        private_root=private,
    )
    plan_sha256 = str(triage["plan_sha256"])
    body_sha256 = str(triage["body_sha256"])
    confirmation = str(triage["required_confirmation"])
    private_path = private / "aws-adapter" / f"{plan_sha256}.json"
    assert private_path.is_file()
    assert triage["private_session_saved"] is True

    sqs.calls.clear()
    provider.calls.clear()
    result = resend_phase715c_dlq_aws(
        provider=provider,
        expected_plan_sha256=plan_sha256,
        expected_body_sha256=body_sha256,
        confirmation=confirmation,
        private_root=private,
    )

    assert provider.calls == ["sqs"]
    assert [name for name, _request in sqs.calls] == [
        "get_queue_url",
        "get_queue_url",
        "get_queue_attributes",
        "get_queue_attributes",
        "send_message",
    ]
    assert sqs.calls[-1][1] == {
        "QueueUrl": adapter.RECOVERY_QUEUE_URL,
        "MessageBody": body,
    }
    assert result["mode"] == "EXACT_RESEND"
    assert result["source_deleted"] is False
    assert result["source_disposition"] == "RETAINED_PENDING_DURABLE_RECOVERY_READBACK"
    public = json.dumps(result, sort_keys=True)
    assert body not in public
    assert MESSAGE_ID not in public
    assert RECEIPT_HANDLE not in public


def test_resend_rejects_gate_drift_before_constructing_any_aws_client(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(RecordingSqs())

    with pytest.raises(Phase715cDlqAwsError, match="refused safely") as captured:
        resend_phase715c_dlq_aws(
            provider=provider,
            expected_plan_sha256="0" * 64,
            expected_body_sha256="1" * 64,
            confirmation="wrong",
            private_root=tmp_path,
        )

    assert captured.value.__cause__ is None
    assert provider.calls == []
    assert provider.sqs.calls == []


def test_private_session_tampering_cannot_turn_unsupported_body_into_resend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = _private_root(tmp_path, monkeypatch)
    sqs = RecordingSqs(body=_workflow_body())
    provider = RecordingProvider(sqs)
    triage = triage_phase715c_dlq_aws(
        provider=provider,
        save_private_session=True,
        private_root=private,
    )
    plan_sha256 = str(triage["plan_sha256"])
    path = private / "aws-adapter" / f"{plan_sha256}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["body"] = json.dumps({"unsupported": PRIVATE_BODY_MARKER})
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    sqs.calls.clear()
    provider.calls.clear()

    with pytest.raises(Phase715cDlqAwsError, match="refused safely") as captured:
        resend_phase715c_dlq_aws(
            provider=provider,
            expected_plan_sha256=plan_sha256,
            expected_body_sha256=str(triage["body_sha256"]),
            confirmation=str(triage["required_confirmation"]),
            private_root=private,
        )

    assert PRIVATE_BODY_MARKER not in str(captured.value)
    assert [name for name, _request in sqs.calls] == ["get_queue_url", "get_queue_url"]
    assert all(name != "send_message" for name, _request in sqs.calls)


def test_adapter_exposes_only_sqs_and_contains_no_delete_or_bulk_move_authority() -> None:
    source = inspect.getsource(adapter)
    normalized = source.replace("_", "").lower()

    assert adapter._Boto3Provider._SERVICES == {"sqs"}
    assert "deletemessage(" not in normalized
    assert "startmessagemovetask(" not in normalized
    assert "cancelmessagemovetask(" not in normalized
    assert "listmessagemovetasks(" not in normalized


def test_cli_modes_emit_sanitized_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sqs = RecordingSqs()
    provider = RecordingProvider(sqs)

    assert adapter.main(["triage"], provider_factory=lambda: provider) == 0

    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["classification"] == "EMPTY"
    assert result["mode"] == "TRIAGE_READ_ONLY"
    assert adapter.ACCOUNT_ID not in output
    assert all(name != "send_message" for name, _request in sqs.calls)
