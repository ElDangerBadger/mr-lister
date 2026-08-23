"""Canonical, capability-free fingerprints for Phase 7 publication authority."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel

from mr_lister.publication.contract import (
    PHASE7_PUBLICATION_CONTRACT_VERSION,
    phase7_publication_contract,
)

PUBLICATION_REQUEST_COMMAND_FIELDS = (
    "owner_id",
    "job_id",
    "expected_record_version",
    "expected_review_version",
    "expected_review_fingerprint",
    "expected_review_etag",
    "expected_approval_decision_id",
    "expected_approval_fingerprint",
    "confirmation",
)

PUBLICATION_WORK_INPUT_FIELDS = (
    "owner_id",
    "job_id",
    "aggregate_id",
    "attempt_id",
    "snapshot_id",
    "snapshot_fingerprint",
    "permit_id",
    "work_request_id",
    "receipt_id",
    "execution_name",
    "verification_deadline",
    "created_at",
)


def _json_compatible(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        encoded = value.isoformat()
        return encoded.removesuffix("+00:00") + "Z" if encoded.endswith("+00:00") else encoded
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported canonical fingerprint value: {type(value).__name__}")


def canonical_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    """Hash one JSON-compatible object with stable, explicit serialization rules."""

    payload = _json_compatible(value)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return sha256(encoded).hexdigest()


def idempotency_key_digest(key: str) -> str:
    """Return the only form of the caller idempotency key that may be persisted."""

    return sha256(key.encode()).hexdigest()


def _record_payload(
    value: BaseModel | Mapping[str, Any],
    *,
    excluded_fields: frozenset[str],
) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", exclude=excluded_fields)
    else:
        payload = dict(value)
        for field in excluded_fields:
            payload.pop(field, None)
    return payload


def _namespaced_fingerprint(kind: str, payload: Mapping[str, Any]) -> str:
    return canonical_fingerprint(
        {
            "contract_version": PHASE7_PUBLICATION_CONTRACT_VERSION,
            "kind": kind,
            "payload": dict(payload),
        }
    )


def publication_request_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    """Bind an idempotency receipt to every semantic seller request field.

    The raw idempotency key is an envelope credential. It is excluded here and persisted only as
    :func:`idempotency_key_digest`; every other request field is required and fingerprinted.
    """

    payload = _record_payload(
        value,
        excluded_fields=frozenset({"contract_version", "idempotency_key"}),
    )
    if tuple(payload) != PUBLICATION_REQUEST_COMMAND_FIELDS:
        if set(payload) != set(PUBLICATION_REQUEST_COMMAND_FIELDS):
            raise ValueError("Publication request fingerprint material has unexpected fields")
        payload = {field: payload[field] for field in PUBLICATION_REQUEST_COMMAND_FIELDS}
    return _namespaced_fingerprint("request_publication", payload)


def publication_snapshot_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    """Hash exactly the 25 frozen snapshot authority fields from contract 7.0.1."""

    payload = _record_payload(
        value,
        excluded_fields=frozenset({"contract_version", "snapshot_id", "fingerprint"}),
    )
    expected = phase7_publication_contract().snapshot_fields
    if set(payload) != set(expected):
        raise ValueError("Publication snapshot fingerprint material differs from contract 7.0.1")
    ordered = {field: payload[field] for field in expected}
    return _namespaced_fingerprint("publication_snapshot", ordered)


def publication_body_fingerprint() -> str:
    """Fingerprint the sole provider body authorized by the frozen contract."""

    fields = phase7_publication_contract().publication_body_fields
    return _namespaced_fingerprint(
        "publication_body",
        {field: True for field in fields},
    )


def publication_aggregate_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    payload = _record_payload(
        value,
        excluded_fields=frozenset({"contract_version", "fingerprint"}),
    )
    return _namespaced_fingerprint("publication_aggregate", payload)


def publication_attempt_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    payload = _record_payload(
        value,
        excluded_fields=frozenset({"contract_version", "fingerprint"}),
    )
    return _namespaced_fingerprint("publication_attempt", payload)


def publication_permit_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    payload = _record_payload(
        value,
        excluded_fields=frozenset({"contract_version", "fingerprint"}),
    )
    return _namespaced_fingerprint("publication_permit", payload)


def publication_event_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    payload = _record_payload(
        value,
        excluded_fields=frozenset({"contract_version", "fingerprint"}),
    )
    return _namespaced_fingerprint("publication_domain_event", payload)


def publication_command_receipt_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    """Content-bind every persisted receipt field, including the complete nested response."""

    payload = _record_payload(
        value,
        excluded_fields=frozenset({"contract_version", "fingerprint"}),
    )
    return _namespaced_fingerprint("publication_command_receipt", payload)


def publication_work_input_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    """Bind pending publication work to its complete immutable execution authority."""

    source = _record_payload(
        value,
        excluded_fields=frozenset({"contract_version", "input_fingerprint"}),
    )
    missing = set(PUBLICATION_WORK_INPUT_FIELDS) - set(source)
    if missing:
        raise ValueError("Publication work fingerprint material is incomplete")
    payload = {field: source[field] for field in PUBLICATION_WORK_INPUT_FIELDS}
    return _namespaced_fingerprint("publication_work_input", payload)
