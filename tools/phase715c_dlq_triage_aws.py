#!/usr/bin/env python3
"""Triage or explicitly resend one Phase 7.15C DLQ message through fixed AWS resources.

The ``triage`` mode performs SQS reads only.  It may optionally persist the core's create-once,
owner-only private session so a later process can perform an exact resend.  The ``resend`` mode
loads only the session named by the supplied plan digest and delegates the hash-, body-, and
confirmation-gated action to the injected core.  Source deletion and bulk movement are absent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Protocol

from tools import phase715c_dlq_triage as core
from tools.phase715c_dlq_triage import (
    DlqAction,
    DlqAuthority,
    DlqClassification,
    DlqTriagePlan,
    DlqTriageSession,
    execute_exact_dlq_action,
    required_action_confirmation,
    triage_one_dlq_message,
    write_private_triage_session,
)

PROFILE: Final = "mr-lister-dev"
ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
STACK_NAME: Final = "mr-lister-phase7-dev"
DEAD_LETTER_QUEUE_NAME: Final = "mr-lister-phase7-dev-publication-operations-dlq"
RECOVERY_QUEUE_NAME: Final = "mr-lister-phase7-dev-publication-recovery"
DEAD_LETTER_QUEUE_URL: Final = (
    f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/{DEAD_LETTER_QUEUE_NAME}"
)
RECOVERY_QUEUE_URL: Final = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/{RECOVERY_QUEUE_NAME}"
DEAD_LETTER_QUEUE_ARN: Final = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:{DEAD_LETTER_QUEUE_NAME}"
RECOVERY_QUEUE_ARN: Final = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:{RECOVERY_QUEUE_NAME}"
TRIAGE_ADAPTER_FORMAT: Final = "mr-lister-phase7.15c-dlq-triage-aws-v1"
ACTION_ADAPTER_FORMAT: Final = "mr-lister-phase7.15c-dlq-action-aws-v1"

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_PRIVATE_DIRECTORY = "aws-adapter"


class Phase715cDlqAwsError(RuntimeError):
    """A value-free refusal for AWS queue authority or private-session drift."""


class AwsClientProvider(Protocol):
    """Construct one client from the adapter's closed service allowlist."""

    def client(self, service_name: str) -> Any: ...


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


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _queue_url(client: object, *, queue_name: str, expected_url: str) -> None:
    response = _mapping(
        client.get_queue_url(
            QueueName=queue_name,
            QueueOwnerAWSAccountId=ACCOUNT_ID,
        )
    )
    if response.get("QueueUrl") != expected_url:
        raise ValueError
    metadata = response.get("ResponseMetadata")
    if metadata is not None and (
        not isinstance(metadata, Mapping) or metadata.get("HTTPStatusCode") != 200
    ):
        raise ValueError


def _authority(client: object) -> DlqAuthority:
    _queue_url(
        client,
        queue_name=DEAD_LETTER_QUEUE_NAME,
        expected_url=DEAD_LETTER_QUEUE_URL,
    )
    _queue_url(
        client,
        queue_name=RECOVERY_QUEUE_NAME,
        expected_url=RECOVERY_QUEUE_URL,
    )
    return DlqAuthority(
        dead_letter_queue_url=DEAD_LETTER_QUEUE_URL,
        dead_letter_queue_arn=DEAD_LETTER_QUEUE_ARN,
        recovery_queue_url=RECOVERY_QUEUE_URL,
        recovery_queue_arn=RECOVERY_QUEUE_ARN,
        max_receive_count=12,
    )


def _session_path(plan_sha256: str, *, private_root: Path) -> Path:
    if _DIGEST.fullmatch(plan_sha256) is None:
        raise ValueError
    return private_root / _PRIVATE_DIRECTORY / f"{plan_sha256}.json"


def _public_plan(
    session: DlqTriageSession,
    *,
    private_session_sha256: str | None,
) -> dict[str, object]:
    plan = session.plan
    confirmation = (
        required_action_confirmation(DlqAction.RESEND, plan) if plan.resend_allowed else None
    )
    return {
        "blocker": plan.blocker,
        "body_sha256": plan.body_sha256,
        "classification": plan.classification.value,
        "core_format": core.TRIAGE_FORMAT,
        "delete_allowed": plan.delete_allowed,
        "delete_blocker": plan.delete_blocker,
        "format": TRIAGE_ADAPTER_FORMAT,
        "message_id_sha256": plan.message_id_sha256,
        "mode": "TRIAGE_READ_ONLY",
        "plan_sha256": plan.plan_sha256,
        "private_session_saved": private_session_sha256 is not None,
        "private_session_sha256": private_session_sha256,
        "receipt_handle_sha256": plan.receipt_handle_sha256,
        "receive_count": plan.receive_count,
        "replay_boundary": plan.replay_boundary,
        "required_confirmation": confirmation,
        "resend_allowed": plan.resend_allowed,
        "result": "passed",
    }


def triage_phase715c_dlq_aws(
    *,
    provider: AwsClientProvider,
    save_private_session: bool = False,
    private_root: Path | None = None,
) -> dict[str, object]:
    """Inspect at most one exact DLQ message without making an AWS mutation."""

    try:
        if provider is None or type(save_private_session) is not bool:
            raise ValueError
        sqs = provider.client("sqs")
        session = triage_one_dlq_message(client=sqs, authority=_authority(sqs))
        private_digest: str | None = None
        if save_private_session and session.body is not None:
            root = core.PRIVATE_ROOT if private_root is None else private_root
            private_digest = write_private_triage_session(
                session,
                _session_path(session.plan.plan_sha256, private_root=root),
            )
        return _public_plan(session, private_session_sha256=private_digest)
    except Phase715cDlqAwsError:
        raise
    except Exception:
        raise Phase715cDlqAwsError("Phase 7.15C AWS DLQ triage failed safely") from None


def _read_exact_file(path: Path) -> bytes:
    with core._private_parent(path, create=False) as (candidate, parent_descriptor):
        descriptor = os.open(
            candidate.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or not 1 <= metadata.st_size <= core.MAX_PRIVATE_SESSION_BYTES
            ):
                raise ValueError
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    raise ValueError
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ValueError
            return b"".join(chunks)
        finally:
            os.close(descriptor)


def _strict_private_document(raw: bytes) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
        value = core._strict_json(text)
    except Exception:
        raise ValueError from None
    document = _mapping(value)
    if set(document) != {"body", "format", "message_id", "plan", "receipt_handle"}:
        raise ValueError
    if document.get("format") != core.PRIVATE_SESSION_FORMAT:
        raise ValueError
    return document


def _required_digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError
    return value


def _optional_string(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError
    return value


def _load_private_session(
    *,
    authority: DlqAuthority,
    expected_plan_sha256: str,
    private_root: Path,
) -> DlqTriageSession:
    document = _strict_private_document(
        _read_exact_file(
            _session_path(expected_plan_sha256, private_root=private_root),
        )
    )
    raw_plan = _mapping(document.get("plan"))
    expected_plan_keys = {
        "blocker",
        "body_sha256",
        "classification",
        "delete_allowed",
        "delete_blocker",
        "format",
        "message_id_sha256",
        "plan_sha256",
        "receive_count",
        "receipt_handle_sha256",
        "replay_boundary",
        "resend_allowed",
        "source_queue_arn",
        "target_queue_arn",
    }
    body = document.get("body")
    message_id = document.get("message_id")
    receipt_handle = document.get("receipt_handle")
    if (
        set(raw_plan) != expected_plan_keys
        or raw_plan.get("format") != core.TRIAGE_FORMAT
        or raw_plan.get("source_queue_arn") != authority.dead_letter_queue_arn
        or raw_plan.get("target_queue_arn") != authority.recovery_queue_arn
        or raw_plan.get("plan_sha256") != expected_plan_sha256
        or not isinstance(body, str)
        or not 1 <= len(body.encode("utf-8")) <= core.MAX_MESSAGE_BYTES
        or not isinstance(message_id, str)
        or _MESSAGE_ID.fullmatch(message_id) is None
        or not isinstance(receipt_handle, str)
        or not 1 <= len(receipt_handle) <= 8192
    ):
        raise ValueError
    classification = DlqClassification(raw_plan.get("classification"))
    blocker = _optional_string(raw_plan.get("blocker"))
    body_sha256 = _required_digest(raw_plan.get("body_sha256"))
    message_id_sha256 = _required_digest(raw_plan.get("message_id_sha256"))
    receipt_handle_sha256 = _required_digest(raw_plan.get("receipt_handle_sha256"))
    receive_count = raw_plan.get("receive_count")
    replay_boundary = raw_plan.get("replay_boundary")
    if (
        classification
        not in {DlqClassification.WORKFLOW_FAILURE, DlqClassification.PRE_DISPATCH_DEADLINE}
        or blocker is not None
        or raw_plan.get("delete_allowed") is not False
        or raw_plan.get("delete_blocker") != "DURABLE_RECOVERY_READBACK_NOT_IMPLEMENTED"
        or raw_plan.get("resend_allowed") is not True
        or type(receive_count) is not int
        or receive_count < 1
        or not isinstance(replay_boundary, str)
        or replay_boundary != "RECOVERY_HANDLER_STRONG_AUTHORITY_REBIND"
        or sha256(body.encode("utf-8")).hexdigest() != body_sha256
        or sha256(message_id.encode("utf-8")).hexdigest() != message_id_sha256
        or sha256(receipt_handle.encode("utf-8")).hexdigest() != receipt_handle_sha256
    ):
        raise ValueError
    classified, resend_allowed, classified_boundary, classified_blocker = core._classification(body)
    if (
        classified is not classification
        or not resend_allowed
        or classified_boundary != replay_boundary
        or classified_blocker is not None
    ):
        raise ValueError
    plan = DlqTriagePlan(
        classification=classification,
        source_queue_arn=authority.dead_letter_queue_arn,
        target_queue_arn=authority.recovery_queue_arn,
        message_id_sha256=message_id_sha256,
        body_sha256=body_sha256,
        receipt_handle_sha256=receipt_handle_sha256,
        receive_count=receive_count,
        delete_allowed=False,
        delete_blocker="DURABLE_RECOVERY_READBACK_NOT_IMPLEMENTED",
        resend_allowed=True,
        replay_boundary=replay_boundary,
        blocker=None,
        plan_sha256=expected_plan_sha256,
    )
    return DlqTriageSession(
        authority=authority,
        plan=plan,
        message_id=message_id,
        receipt_handle=receipt_handle,
        body=body,
    )


def resend_phase715c_dlq_aws(
    *,
    provider: AwsClientProvider,
    expected_plan_sha256: str,
    expected_body_sha256: str,
    confirmation: str,
    private_root: Path | None = None,
) -> dict[str, object]:
    """Resend one persisted exact recovery envelope and always retain the DLQ source."""

    try:
        if (
            provider is None
            or _DIGEST.fullmatch(expected_plan_sha256) is None
            or _DIGEST.fullmatch(expected_body_sha256) is None
            or not isinstance(confirmation, str)
            or confirmation
            != (
                f"phase7.15c:{DlqAction.RESEND.value}:{expected_plan_sha256}:{expected_body_sha256}"
            )
        ):
            raise ValueError
        sqs = provider.client("sqs")
        authority = _authority(sqs)
        root = core.PRIVATE_ROOT if private_root is None else private_root
        session = _load_private_session(
            authority=authority,
            expected_plan_sha256=expected_plan_sha256,
            private_root=root,
        )
        result = execute_exact_dlq_action(
            client=sqs,
            session=session,
            action=DlqAction.RESEND,
            expected_plan_sha256=expected_plan_sha256,
            expected_body_sha256=expected_body_sha256,
            confirmation=confirmation,
        )
        if result.get("source_deleted") is not False:
            raise ValueError
        return {
            "acknowledgement": result.get("acknowledgement"),
            "action": result.get("action"),
            "action_sha256": result.get("action_sha256"),
            "body_sha256": result.get("body_sha256"),
            "core_format": result.get("format"),
            "destination_message_id_sha256": result.get("destination_message_id_sha256"),
            "format": ACTION_ADAPTER_FORMAT,
            "mode": "EXACT_RESEND",
            "plan_sha256": result.get("plan_sha256"),
            "result": "passed",
            "source_deleted": False,
            "source_disposition": result.get("source_disposition"),
        }
    except Phase715cDlqAwsError:
        raise
    except Exception:
        raise Phase715cDlqAwsError("Phase 7.15C AWS DLQ resend refused safely") from None


class _Boto3Provider:
    """Construct only the fixed-profile, fixed-Region SQS client."""

    _SERVICES: Final = frozenset({"sqs"})

    def __init__(self) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise Phase715cDlqAwsError("boto3 is unavailable") from None
        self._session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        self._config = Config(retries={"mode": "standard", "total_max_attempts": 1})

    def client(self, service_name: str) -> Any:
        if service_name not in self._SERVICES:
            raise Phase715cDlqAwsError("AWS service is outside the Phase 7.15C DLQ boundary")
        return self._session.client(service_name, config=self._config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    triage = commands.add_parser("triage", help="inspect at most one DLQ message")
    triage.add_argument(
        "--save-private-session",
        action="store_true",
        help="create an owner-only private session for a later exact resend",
    )
    resend = commands.add_parser("resend", help="resend one persisted exact recovery envelope")
    resend.add_argument("--expected-plan-sha256", required=True)
    resend.add_argument("--expected-body-sha256", required=True)
    resend.add_argument("--confirmation", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    provider_factory: Callable[[], AwsClientProvider] = _Boto3Provider,
    private_root: Path | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    provider = provider_factory()
    if arguments.mode == "triage":
        result = triage_phase715c_dlq_aws(
            provider=provider,
            save_private_session=arguments.save_private_session,
            private_root=private_root,
        )
    else:
        result = resend_phase715c_dlq_aws(
            provider=provider,
            expected_plan_sha256=arguments.expected_plan_sha256,
            expected_body_sha256=arguments.expected_body_sha256,
            confirmation=arguments.confirmation,
            private_root=private_root,
        )
    print(_canonical(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase715cDlqAwsError as error:
        raise SystemExit(f"phase715c AWS DLQ operation stopped: {error}") from None


__all__ = [
    "ACCOUNT_ID",
    "ACTION_ADAPTER_FORMAT",
    "AwsClientProvider",
    "PROFILE",
    "Phase715cDlqAwsError",
    "REGION",
    "STACK_NAME",
    "TRIAGE_ADAPTER_FORMAT",
    "resend_phase715c_dlq_aws",
    "triage_phase715c_dlq_aws",
]
