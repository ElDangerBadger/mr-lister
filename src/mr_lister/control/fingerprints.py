"""Canonical fingerprints used by Phase 6 optimistic-control commands."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from typing import Any

from pydantic import BaseModel


def canonical_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    """Hash one JSON-compatible value with stable key and separator rules."""

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


_PRODUCT_SYNC_FINGERPRINT_FIELDS = frozenset(
    {
        "job_id",
        "review_version",
        "product_id",
        "image_id",
        "payload_fingerprint",
        "response_fingerprint",
        "mockups",
        "variants",
        "provider_locked",
        "provider_published",
        "synchronized_at",
    }
)


def product_sync_record_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    """Hash exact immutable synchronization evidence, including owner-bound shop authority.

    Records written before Phase 7.1 had no ``printify_shop_id`` field. They remain readable and
    retain their historical fingerprint, but cannot pass the approval boundary. New records must
    include a positive shop identifier and bind it into this fingerprint.
    """

    if isinstance(value, BaseModel):
        payload = value.model_dump(
            mode="json",
            exclude={"contract_version", "sync_id", "fingerprint"},
        )
        synchronized_at = getattr(value, "synchronized_at", None)
        if isinstance(synchronized_at, datetime):
            # Preserve the Phase 6 canonical timestamp spelling used by existing records.
            payload["synchronized_at"] = synchronized_at.isoformat()
    else:
        payload = dict(value)
        if isinstance(payload.get("synchronized_at"), datetime):
            payload["synchronized_at"] = payload["synchronized_at"].isoformat()
    if payload.get("printify_shop_id") is None:
        payload.pop("printify_shop_id", None)
    expected_fields = _PRODUCT_SYNC_FINGERPRINT_FIELDS | (
        {"printify_shop_id"} if "printify_shop_id" in payload else set()
    )
    if set(payload) != expected_fields:
        raise ValueError("Product synchronization fingerprint material has unexpected fields")
    return canonical_fingerprint(payload)


def command_request_fingerprint(*, command_type: str, payload: Mapping[str, Any]) -> str:
    """Bind an idempotency receipt to its command type and complete request body."""

    return canonical_fingerprint({"command_type": command_type, "payload": dict(payload)})


def agent_preparation_evidence_fingerprint(
    *,
    evidence_id: str,
    job_id: str,
    work_request_id: str,
    review_version: int,
    correlation_id: str,
    framework: str,
    agent_id: str,
    controller_model_id: str,
    tool_calls: tuple[str, ...],
    cycles: int,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    decision_fingerprint: str,
    requires_human_approval: bool,
    publication_authorized: bool,
    created_at: datetime,
) -> str:
    """Hash the complete persisted Strands evidence rather than an unavailable command body."""

    return canonical_fingerprint(
        {
            "evidence_id": evidence_id,
            "job_id": job_id,
            "work_request_id": work_request_id,
            "review_version": review_version,
            "correlation_id": correlation_id,
            "framework": framework,
            "agent_id": agent_id,
            "controller_model_id": controller_model_id,
            "tool_calls": tool_calls,
            "cycles": cycles,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "decision_fingerprint": decision_fingerprint,
            "requires_human_approval": requires_human_approval,
            "publication_authorized": publication_authorized,
            "created_at": created_at.isoformat(),
        }
    )


def review_etag(
    *,
    job_id: str,
    review_version: int,
    review_fingerprint: str,
    product_id: str | None,
    product_sync_fingerprint: str | None,
    pricing_snapshot_id: str | None,
    pricing_snapshot_fingerprint: str | None,
) -> str:
    """Return the public composite review authority token.

    Operational job fields are deliberately absent. Any review, synchronized product, or
    economics change produces a different token.
    """

    return canonical_fingerprint(
        {
            "job_id": job_id,
            "review_version": review_version,
            "review_fingerprint": review_fingerprint,
            "product_id": product_id,
            "product_sync_fingerprint": product_sync_fingerprint,
            "pricing_snapshot_id": pricing_snapshot_id,
            "pricing_snapshot_fingerprint": pricing_snapshot_fingerprint,
        }
    )


def idempotency_key_digest(key: str) -> str:
    """Persist only a digest of the caller-supplied idempotency key."""

    return sha256(key.encode()).hexdigest()
