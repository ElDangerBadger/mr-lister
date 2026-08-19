"""Private, application-owned diagnostics for Bedrock model invocations.

The public workflow records remain deliberately small and safe.  This module provides
an opt-in diagnostic trail for troubleshooting the model boundary without putting
prompts, artwork bytes, credentials, or raw model output into ordinary reports or
logs.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import uuid4

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

REDACTED = "[REDACTED]"
REDACTED_BINARY = "[REDACTED BINARY]"

_SAFE_TOKEN_COUNT_KEYS = frozenset(
    {
        "inputtoken",
        "inputtokens",
        "outputtoken",
        "outputtokens",
        "totaltoken",
        "totaltokens",
    }
)
_EXACT_SENSITIVE_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "artwork",
        "artworkbytes",
        "authorization",
        "bearertoken",
        "body",
        "content",
        "cookie",
        "credentials",
        "image",
        "imagebytes",
        "images",
        "messages",
        "modeloutput",
        "output",
        "password",
        "prompt",
        "rawmodeloutput",
        "rawoutput",
        "rawrequest",
        "rawresponse",
        "requestbody",
        "responsebody",
        "secret",
        "secretaccesskey",
        "sessiontoken",
        "systemprompt",
        "token",
    }
)
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "apikey",
    "accesskey",
    "sessiontoken",
    "bearertoken",
)
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _normalized_key(key: object) -> str:
    return "".join(character for character in str(key).casefold() if character.isalnum())


def _is_sensitive_key(key: object) -> bool:
    normalized = _normalized_key(key)
    if normalized in _SAFE_TOKEN_COUNT_KEYS:
        return False
    return normalized in _EXACT_SENSITIVE_KEYS or any(
        fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS
    )


def redact_sensitive_data(value: object) -> JsonValue:
    """Return a JSON-safe copy with secrets, prompts, and binary values redacted.

    Metadata is intentionally treated as untrusted.  Sensitive key names are removed
    recursively, and bytes are never serialized even when their surrounding key is
    innocuous.  Token-count keys remain available because they are useful, non-secret
    usage diagnostics.
    """

    if isinstance(value, Mapping):
        redacted: dict[str, JsonValue] = {}
        for key, item in value.items():
            serialized_key = str(key)
            redacted[serialized_key] = (
                REDACTED if _is_sensitive_key(key) else redact_sensitive_data(item)
            )
        return redacted
    if isinstance(value, (bytes, bytearray, memoryview)):
        return REDACTED_BINARY
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, Enum):
        return redact_sensitive_data(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Diagnostic metadata value is not JSON serializable: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class BedrockDiagnosticRecord:
    """A safe model-boundary event with an optional private raw output attachment.

    There are intentionally no prompt, request-body, message, or artwork fields.
    ``raw_model_output`` is hidden from ``repr`` and omitted from serialization unless
    the receiving sink was explicitly configured to retain it.
    """

    operation: str
    model_id: str
    status: str
    prompt_version: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    diagnostic_id: str = field(default_factory=lambda: uuid4().hex)
    attempt: int = 1
    latency_ms: float | None = None
    request_id: str | None = None
    job_id: str | None = None
    artwork_sha256: str | None = None
    response_sha256: str | None = None
    usage: Mapping[str, object] = field(default_factory=dict, repr=False)
    metadata: Mapping[str, object] = field(default_factory=dict, repr=False)
    error_type: str | None = None
    error_code: str | None = None
    error_message: str | None = field(default=None, repr=False)
    raw_model_output: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in ("operation", "model_id", "status", "prompt_version", "diagnostic_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.attempt < 1:
            raise ValueError("attempt must be at least one")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency_ms must not be negative")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        for name in ("artwork_sha256", "response_sha256"):
            digest = getattr(self, name)
            if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.raw_model_output is not None:
            computed_digest = sha256(self.raw_model_output.encode("utf-8")).hexdigest()
            if self.response_sha256 is not None and self.response_sha256 != computed_digest:
                raise ValueError("response_sha256 does not match raw_model_output")
            object.__setattr__(self, "response_sha256", computed_digest)

    def to_payload(self, *, include_raw_output: bool = False) -> dict[str, JsonValue]:
        """Serialize a redacted event, excluding raw model output by default."""

        payload: dict[str, JsonValue] = {
            "artwork_sha256": self.artwork_sha256,
            "attempt": self.attempt,
            "diagnostic_id": self.diagnostic_id,
            "error_code": self.error_code,
            # Provider messages can echo request content or credentials.  The safe class/code
            # above carries the actionable detail; arbitrary message text never reaches disk.
            "error_message": REDACTED if self.error_message is not None else None,
            "error_type": self.error_type,
            "job_id": self.job_id,
            "latency_ms": self.latency_ms,
            "metadata": redact_sensitive_data(self.metadata),
            "model_id": self.model_id,
            "occurred_at": self.occurred_at.isoformat(),
            "operation": self.operation,
            "prompt_version": self.prompt_version,
            "request_id": self.request_id,
            "response_sha256": self.response_sha256,
            "status": self.status,
            "usage": redact_sensitive_data(self.usage),
        }
        if include_raw_output and self.raw_model_output is not None:
            payload["raw_model_output"] = self.raw_model_output
        return payload

    def to_json(self, *, include_raw_output: bool = False) -> str:
        """Return deterministic, standards-compliant JSON for the event."""

        return json.dumps(
            self.to_payload(include_raw_output=include_raw_output),
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )


class DiagnosticSink(Protocol):
    """Application boundary for private Bedrock diagnostic storage."""

    def emit(self, record: BedrockDiagnosticRecord) -> None: ...


class NoOpDiagnosticSink:
    """Discard diagnostics while preserving a stable adapter dependency."""

    def emit(self, record: BedrockDiagnosticRecord) -> None:
        del record

    write = emit


class CompositeDiagnosticSink:
    """Send one diagnostic record to multiple independently configured sinks."""

    def __init__(self, *sinks: DiagnosticSink) -> None:
        if not sinks:
            raise ValueError("CompositeDiagnosticSink requires at least one sink")
        self.sinks = sinks

    def emit(self, record: BedrockDiagnosticRecord) -> None:
        for sink in self.sinks:
            sink.emit(record)

    write = emit


class InMemoryDiagnosticSink:
    """Capture redacted payloads for adapter tests without touching the filesystem."""

    def __init__(self, *, include_raw_output: bool = False) -> None:
        self.include_raw_output = include_raw_output
        self.records: list[dict[str, JsonValue]] = []

    def emit(self, record: BedrockDiagnosticRecord) -> None:
        self.records.append(record.to_payload(include_raw_output=self.include_raw_output))

    write = emit


class FilesystemDiagnosticSink:
    """Write one redacted JSON document per event to a private directory."""

    def __init__(self, directory: Path, *, include_raw_output: bool = False) -> None:
        self.directory = Path(directory)
        self.include_raw_output = include_raw_output
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            self.directory.chmod(0o700)

    def emit(self, record: BedrockDiagnosticRecord) -> None:
        serialized = record.to_json(include_raw_output=self.include_raw_output) + "\n"
        destination = self.directory / f"{_safe_filename(record.diagnostic_id)}.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(destination, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as diagnostic_file:
                diagnostic_file.write(serialized)
        except BaseException:
            # fdopen owns the descriptor once entered; close it only if setup itself failed.
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        if os.name == "posix":
            destination.chmod(0o600)

    write = emit


def _safe_filename(diagnostic_id: str) -> str:
    sanitized = _SAFE_FILENAME.sub("_", diagnostic_id).strip("._")
    return sanitized or "diagnostic"
