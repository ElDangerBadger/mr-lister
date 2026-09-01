"""Exact one-message Phase 7.15C DLQ triage and replay controls.

Triage is read-only by default and returns only hashes, counts, and a closed classification.  Raw
message bodies and receipt handles may be persisted only in the ignored repository-private root.
Resend requires the exact plan digest, body digest, and explicit action confirmation.  It is
allowed only for the two closed recovery envelopes already handled by the strong-authority
recovery boundary and always retains the source message.  Delete authority is intentionally
absent until durable recovery readback exists.  Unsupported event-source failures remain
classifier/plan-only.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import md5, sha256
from pathlib import Path
from typing import Any, Final, Protocol

from mr_lister.publication.orchestration_recovery import (
    PublicationPreDispatchDeadlineEnvelope,
    PublicationWorkflowFailureEnvelope,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase715c-operations"

TRIAGE_FORMAT: Final = "mr-lister-phase7.15c-dlq-triage-v1"
ACTION_FORMAT: Final = "mr-lister-phase7.15c-dlq-action-v1"
PRIVATE_SESSION_FORMAT: Final = "mr-lister-phase7.15c-private-dlq-session-v1"
MAX_MESSAGE_BYTES: Final = 256 * 1024
MAX_PRIVATE_SESSION_BYTES: Final = 512 * 1024

_QUEUE_URL = re.compile(
    r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/"
    r"[0-9]{12}/[A-Za-z0-9_-]{1,80}$"
)
_QUEUE_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):sqs:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]{1,80}$"
)
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")


class Phase715cDlqError(RuntimeError):
    """A value-free refusal for malformed, drifting, or unauthorized DLQ work."""


class DlqClassification(StrEnum):
    EMPTY = "EMPTY"
    WORKFLOW_FAILURE = "WORKFLOW_FAILURE"
    PRE_DISPATCH_DEADLINE = "PRE_DISPATCH_DEADLINE"
    EVENT_SOURCE_FAILURE = "EVENT_SOURCE_FAILURE"
    UNSUPPORTED = "UNSUPPORTED"


class DlqAction(StrEnum):
    RESEND = "RESEND"


class SqsDlqReadClient(Protocol):
    def get_queue_attributes(self, **request: Any) -> Mapping[str, Any]: ...

    def receive_message(self, **request: Any) -> Mapping[str, Any]: ...


class SqsDlqActionClient(SqsDlqReadClient, Protocol):
    def send_message(self, **request: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DlqAuthority:
    dead_letter_queue_url: str
    dead_letter_queue_arn: str
    recovery_queue_url: str
    recovery_queue_arn: str
    max_receive_count: int = 12

    def __post_init__(self) -> None:
        try:
            if (
                _QUEUE_URL.fullmatch(self.dead_letter_queue_url) is None
                or _QUEUE_ARN.fullmatch(self.dead_letter_queue_arn) is None
                or _QUEUE_URL.fullmatch(self.recovery_queue_url) is None
                or _QUEUE_ARN.fullmatch(self.recovery_queue_arn) is None
                or self.dead_letter_queue_url == self.recovery_queue_url
                or self.dead_letter_queue_arn == self.recovery_queue_arn
                or type(self.max_receive_count) is not int
                or not 1 <= self.max_receive_count <= 100
                or self.dead_letter_queue_url.rsplit("/", 1)[-1]
                != self.dead_letter_queue_arn.rsplit(":", 1)[-1]
                or self.recovery_queue_url.rsplit("/", 1)[-1]
                != self.recovery_queue_arn.rsplit(":", 1)[-1]
            ):
                raise ValueError
            dead_account = self.dead_letter_queue_arn.split(":")[4]
            recovery_account = self.recovery_queue_arn.split(":")[4]
            dead_region = self.dead_letter_queue_arn.split(":")[3]
            recovery_region = self.recovery_queue_arn.split(":")[3]
            if dead_account != recovery_account or dead_region != recovery_region:
                raise ValueError
        except Exception:
            raise Phase715cDlqError("Phase 7.15C DLQ authority is invalid") from None


@dataclass(frozen=True, slots=True)
class DlqTriagePlan:
    classification: DlqClassification
    source_queue_arn: str
    target_queue_arn: str
    message_id_sha256: str | None
    body_sha256: str | None
    receipt_handle_sha256: str | None
    receive_count: int
    delete_allowed: bool
    delete_blocker: str
    resend_allowed: bool
    replay_boundary: str
    blocker: str | None
    plan_sha256: str

    def document(self, *, include_plan_sha256: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "blocker": self.blocker,
            "body_sha256": self.body_sha256,
            "classification": self.classification.value,
            "delete_allowed": self.delete_allowed,
            "delete_blocker": self.delete_blocker,
            "format": TRIAGE_FORMAT,
            "message_id_sha256": self.message_id_sha256,
            "receive_count": self.receive_count,
            "receipt_handle_sha256": self.receipt_handle_sha256,
            "replay_boundary": self.replay_boundary,
            "resend_allowed": self.resend_allowed,
            "source_queue_arn": self.source_queue_arn,
            "target_queue_arn": self.target_queue_arn,
        }
        if include_plan_sha256:
            value["plan_sha256"] = self.plan_sha256
        return value


@dataclass(frozen=True, slots=True)
class DlqTriageSession:
    authority: DlqAuthority
    plan: DlqTriagePlan
    message_id: str | None = field(repr=False)
    receipt_handle: str | None = field(repr=False)
    body: str | None = field(repr=False)


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


def _digest(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return sha256(payload).hexdigest()


def _strict_json(raw: str) -> object:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, nested in pairs:
            if key in value:
                raise ValueError
            value[key] = nested
        return value

    return json.loads(
        raw,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        object_pairs_hook=unique,
    )


def _queue_attributes(
    client: SqsDlqReadClient,
    authority: DlqAuthority,
) -> None:
    dead_response = client.get_queue_attributes(
        QueueUrl=authority.dead_letter_queue_url,
        AttributeNames=["QueueArn", "SqsManagedSseEnabled"],
    )
    recovery_response = client.get_queue_attributes(
        QueueUrl=authority.recovery_queue_url,
        AttributeNames=["QueueArn", "SqsManagedSseEnabled", "RedrivePolicy"],
    )
    if not isinstance(dead_response, Mapping) or not isinstance(recovery_response, Mapping):
        raise ValueError
    if not set(dead_response).issubset({"Attributes", "ResponseMetadata"}) or not set(
        recovery_response
    ).issubset({"Attributes", "ResponseMetadata"}):
        raise ValueError
    dead = dead_response.get("Attributes")
    recovery = recovery_response.get("Attributes")
    if dead != {
        "QueueArn": authority.dead_letter_queue_arn,
        "SqsManagedSseEnabled": "true",
    } or not isinstance(recovery, Mapping):
        raise ValueError
    if set(recovery) != {"QueueArn", "SqsManagedSseEnabled", "RedrivePolicy"}:
        raise ValueError
    if (
        recovery.get("QueueArn") != authority.recovery_queue_arn
        or recovery.get("SqsManagedSseEnabled") != "true"
        or not isinstance(recovery.get("RedrivePolicy"), str)
    ):
        raise ValueError
    redrive = _strict_json(recovery["RedrivePolicy"])
    if redrive != {
        "deadLetterTargetArn": authority.dead_letter_queue_arn,
        "maxReceiveCount": str(authority.max_receive_count),
    }:
        raise ValueError
    for response in (dead_response, recovery_response):
        metadata = response.get("ResponseMetadata")
        if metadata is not None and (
            not isinstance(metadata, Mapping) or metadata.get("HTTPStatusCode") != 200
        ):
            raise ValueError


def _classification(body: str) -> tuple[DlqClassification, bool, str, str | None]:
    try:
        decoded = _strict_json(body)
    except Exception:
        return (
            DlqClassification.UNSUPPORTED,
            False,
            "CLASSIFIER_PLAN_ONLY",
            "MALFORMED_MESSAGE_BODY",
        )
    if not isinstance(decoded, dict):
        return (
            DlqClassification.UNSUPPORTED,
            False,
            "CLASSIFIER_PLAN_ONLY",
            "UNSUPPORTED_MESSAGE_BODY",
        )
    try:
        if set(decoded) == {"execution_arn", "machine_arn", "status"}:
            PublicationWorkflowFailureEnvelope.model_validate_json(body, strict=True)
            return (
                DlqClassification.WORKFLOW_FAILURE,
                True,
                "RECOVERY_HANDLER_STRONG_AUTHORITY_REBIND",
                None,
            )
        if set(decoded) == {
            "aggregate_id",
            "kind",
            "owner_id",
            "verification_deadline",
            "work_request_id",
        }:
            PublicationPreDispatchDeadlineEnvelope.model_validate_json(body, strict=True)
            return (
                DlqClassification.PRE_DISPATCH_DEADLINE,
                True,
                "RECOVERY_HANDLER_STRONG_AUTHORITY_REBIND",
                None,
            )
    except Exception:
        return (
            DlqClassification.UNSUPPORTED,
            False,
            "CLASSIFIER_PLAN_ONLY",
            "INVALID_RECOVERY_ENVELOPE",
        )
    event_destination_keys = {
        "requestContext",
        "requestPayload",
        "responseContext",
        "responsePayload",
        "timestamp",
        "version",
    }
    if event_destination_keys.issubset(decoded):
        return (
            DlqClassification.EVENT_SOURCE_FAILURE,
            False,
            "CLASSIFIER_PLAN_ONLY",
            "SOURCE_EVENT_REPLAY_AUTHORITY_NOT_IMPLEMENTED",
        )
    return (
        DlqClassification.UNSUPPORTED,
        False,
        "CLASSIFIER_PLAN_ONLY",
        "UNSUPPORTED_MESSAGE_BODY",
    )


def _plan(
    *,
    authority: DlqAuthority,
    classification: DlqClassification,
    message_id_sha256: str | None,
    body_sha256: str | None,
    receipt_handle_sha256: str | None,
    receive_count: int,
    resend_allowed: bool,
    replay_boundary: str,
    blocker: str | None,
) -> DlqTriagePlan:
    values = {
        "blocker": blocker,
        "body_sha256": body_sha256,
        "classification": classification.value,
        "delete_allowed": False,
        "delete_blocker": "DURABLE_RECOVERY_READBACK_NOT_IMPLEMENTED",
        "format": TRIAGE_FORMAT,
        "message_id_sha256": message_id_sha256,
        "receive_count": receive_count,
        "receipt_handle_sha256": receipt_handle_sha256,
        "replay_boundary": replay_boundary,
        "resend_allowed": resend_allowed,
        "source_queue_arn": authority.dead_letter_queue_arn,
        "target_queue_arn": authority.recovery_queue_arn,
    }
    return DlqTriagePlan(
        classification=classification,
        source_queue_arn=authority.dead_letter_queue_arn,
        target_queue_arn=authority.recovery_queue_arn,
        message_id_sha256=message_id_sha256,
        body_sha256=body_sha256,
        receipt_handle_sha256=receipt_handle_sha256,
        receive_count=receive_count,
        delete_allowed=False,
        delete_blocker="DURABLE_RECOVERY_READBACK_NOT_IMPLEMENTED",
        resend_allowed=resend_allowed,
        replay_boundary=replay_boundary,
        blocker=blocker,
        plan_sha256=_digest(_canonical(values)),
    )


def triage_one_dlq_message(
    *,
    client: SqsDlqReadClient,
    authority: DlqAuthority,
) -> DlqTriageSession:
    """Inspect at most one message and return a body-free, digest-bound plan."""

    try:
        if client is None or not isinstance(authority, DlqAuthority):
            raise ValueError
        _queue_attributes(client, authority)
        response = client.receive_message(
            QueueUrl=authority.dead_letter_queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
            VisibilityTimeout=0,
            AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
        )
        if not isinstance(response, Mapping):
            raise ValueError
        if not set(response).issubset({"Messages", "ResponseMetadata"}):
            raise ValueError
        metadata = response.get("ResponseMetadata")
        if metadata is not None and (
            not isinstance(metadata, Mapping) or metadata.get("HTTPStatusCode") != 200
        ):
            raise ValueError
        messages = response.get("Messages", [])
        if not isinstance(messages, list) or len(messages) > 1:
            raise ValueError
        if not messages:
            plan = _plan(
                authority=authority,
                classification=DlqClassification.EMPTY,
                message_id_sha256=None,
                body_sha256=None,
                receipt_handle_sha256=None,
                receive_count=0,
                resend_allowed=False,
                replay_boundary="NO_MESSAGE",
                blocker="DLQ_EMPTY",
            )
            return DlqTriageSession(authority, plan, None, None, None)
        message = messages[0]
        if not isinstance(message, Mapping) or set(message) != {
            "Attributes",
            "Body",
            "MD5OfBody",
            "MessageId",
            "ReceiptHandle",
        }:
            raise ValueError
        message_id = message.get("MessageId")
        receipt_handle = message.get("ReceiptHandle")
        body = message.get("Body")
        attributes = message.get("Attributes")
        if (
            not isinstance(message_id, str)
            or _MESSAGE_ID.fullmatch(message_id) is None
            or not isinstance(receipt_handle, str)
            or not 1 <= len(receipt_handle) <= 8192
            or not isinstance(body, str)
            or not 1 <= len(body.encode("utf-8")) <= MAX_MESSAGE_BYTES
            or not isinstance(attributes, Mapping)
            or set(attributes) != {"ApproximateReceiveCount", "SentTimestamp"}
            or not all(
                isinstance(value, str) and _INTEGER.fullmatch(value)
                for value in attributes.values()
            )
        ):
            raise ValueError
        expected_md5 = md5(body.encode("utf-8"), usedforsecurity=False).hexdigest()
        if not secrets.compare_digest(str(message.get("MD5OfBody", "")), expected_md5):
            raise ValueError
        receive_count = int(attributes["ApproximateReceiveCount"])
        if receive_count < 1:
            raise ValueError
        classification, resend_allowed, replay_boundary, blocker = _classification(body)
        plan = _plan(
            authority=authority,
            classification=classification,
            message_id_sha256=_digest(message_id),
            body_sha256=_digest(body),
            receipt_handle_sha256=_digest(receipt_handle),
            receive_count=receive_count,
            resend_allowed=resend_allowed,
            replay_boundary=replay_boundary,
            blocker=blocker,
        )
        return DlqTriageSession(authority, plan, message_id, receipt_handle, body)
    except Phase715cDlqError:
        raise
    except Exception:
        raise Phase715cDlqError("Phase 7.15C DLQ triage failed safely") from None


def required_action_confirmation(action: DlqAction, plan: DlqTriagePlan) -> str:
    if not isinstance(action, DlqAction) or not isinstance(plan, DlqTriagePlan):
        raise Phase715cDlqError("Phase 7.15C DLQ action authority is invalid")
    return f"phase7.15c:{action.value}:{plan.plan_sha256}:{plan.body_sha256}"


def execute_exact_dlq_action(
    *,
    client: SqsDlqActionClient,
    session: DlqTriageSession,
    action: DlqAction,
    expected_plan_sha256: str,
    expected_body_sha256: str,
    confirmation: str,
) -> dict[str, object]:
    """Perform one exact resend after all digest gates match; source deletion is unavailable."""

    try:
        if (
            client is None
            or not isinstance(session, DlqTriageSession)
            or not isinstance(action, DlqAction)
            or action is not DlqAction.RESEND
            or _DIGEST.fullmatch(expected_plan_sha256) is None
            or _DIGEST.fullmatch(expected_body_sha256) is None
            or session.body is None
            or session.message_id is None
            or session.receipt_handle is None
            or session.plan.classification is DlqClassification.EMPTY
            or not session.plan.resend_allowed
            or session.plan.blocker is not None
            or not secrets.compare_digest(
                session.plan.plan_sha256,
                _digest(_canonical(session.plan.document(include_plan_sha256=False))),
            )
            or session.plan.source_queue_arn != session.authority.dead_letter_queue_arn
            or session.plan.target_queue_arn != session.authority.recovery_queue_arn
            or session.plan.delete_allowed
            or session.plan.delete_blocker != "DURABLE_RECOVERY_READBACK_NOT_IMPLEMENTED"
            or not secrets.compare_digest(session.plan.plan_sha256, expected_plan_sha256)
            or session.plan.body_sha256 is None
            or not secrets.compare_digest(session.plan.body_sha256, expected_body_sha256)
            or not secrets.compare_digest(_digest(session.body), expected_body_sha256)
            or session.plan.message_id_sha256 != _digest(session.message_id)
            or session.plan.receipt_handle_sha256 != _digest(session.receipt_handle)
            or not secrets.compare_digest(
                confirmation,
                required_action_confirmation(action, session.plan),
            )
        ):
            raise ValueError
        _queue_attributes(client, session.authority)
        result: dict[str, object] = {
            "action": action.value,
            "body_sha256": expected_body_sha256,
            "format": ACTION_FORMAT,
            "plan_sha256": expected_plan_sha256,
            "source_disposition": "RETAINED_PENDING_DURABLE_RECOVERY_READBACK",
            "source_deleted": False,
        }
        response = client.send_message(
            QueueUrl=session.authority.recovery_queue_url,
            MessageBody=session.body,
        )
        if not isinstance(response, Mapping):
            raise ValueError
        message_id = response.get("MessageId")
        expected_md5 = md5(session.body.encode("utf-8"), usedforsecurity=False).hexdigest()
        if (
            not isinstance(message_id, str)
            or _MESSAGE_ID.fullmatch(message_id) is None
            or not secrets.compare_digest(
                str(response.get("MD5OfMessageBody", "")),
                expected_md5,
            )
        ):
            raise ValueError
        metadata = response.get("ResponseMetadata")
        if metadata is not None and (
            not isinstance(metadata, Mapping) or metadata.get("HTTPStatusCode") != 200
        ):
            raise ValueError
        if not set(response).issubset({"MD5OfMessageBody", "MessageId", "ResponseMetadata"}):
            raise ValueError
        result.update(
            acknowledgement="EXACT_BODY_MD5_ACCEPTED_SOURCE_RETAINED",
            destination_message_id_sha256=_digest(message_id),
        )
        result["action_sha256"] = _digest(_canonical(result))
        return result
    except Phase715cDlqError:
        raise
    except Exception:
        raise Phase715cDlqError("Phase 7.15C DLQ action refused safely") from None


def _repository_private_path(path: Path) -> tuple[Path, tuple[str, ...]]:
    candidate = Path(os.path.abspath(path))
    repository = Path(os.path.abspath(REPOSITORY_ROOT))
    private = Path(os.path.abspath(PRIVATE_ROOT))
    try:
        candidate.relative_to(private)
        relative = candidate.relative_to(repository)
    except ValueError:
        raise ValueError from None
    if not relative.parts or candidate == private:
        raise ValueError
    return candidate, relative.parts


def _open_repository_root() -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    root = Path(os.path.abspath(REPOSITORY_ROOT))
    descriptor: int | None = os.open(os.sep, flags)
    try:
        for component in root.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _private_parent(path: Path, *, create: bool) -> Iterator[tuple[Path, int]]:
    candidate, components = _repository_private_path(path)
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
            except Exception:
                if next_descriptor is not None:
                    os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        yield candidate, descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_private_triage_session(session: DlqTriageSession, path: Path) -> str:
    """Create one owner-only raw session beneath the ignored private operations root."""

    try:
        if (
            not isinstance(session, DlqTriageSession)
            or session.body is None
            or session.message_id is None
            or session.receipt_handle is None
        ):
            raise ValueError
        document = {
            "body": session.body,
            "format": PRIVATE_SESSION_FORMAT,
            "message_id": session.message_id,
            "plan": session.plan.document(),
            "receipt_handle": session.receipt_handle,
        }
        payload = _canonical(document)
        if len(payload) > MAX_PRIVATE_SESSION_BYTES:
            raise ValueError
        with _private_parent(path, create=True) as (candidate, parent_descriptor):
            descriptor = os.open(
                candidate.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_descriptor,
            )
            try:
                written = 0
                while written < len(payload):
                    written += os.write(descriptor, payload[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent_descriptor)
        return _digest(payload)
    except Phase715cDlqError:
        raise
    except Exception:
        raise Phase715cDlqError("Phase 7.15C private DLQ evidence was refused") from None


__all__ = [
    "ACTION_FORMAT",
    "DlqAction",
    "DlqAuthority",
    "DlqClassification",
    "DlqTriagePlan",
    "DlqTriageSession",
    "PRIVATE_ROOT",
    "Phase715cDlqError",
    "SqsDlqActionClient",
    "SqsDlqReadClient",
    "TRIAGE_FORMAT",
    "execute_exact_dlq_action",
    "required_action_confirmation",
    "triage_one_dlq_message",
    "write_private_triage_session",
]
