"""Canonical fingerprints used by Phase 6 optimistic-control commands."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from pydantic import BaseModel


def canonical_fingerprint(value: BaseModel | Mapping[str, Any]) -> str:
    """Hash one JSON-compatible value with stable key and separator rules."""

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def command_request_fingerprint(*, command_type: str, payload: Mapping[str, Any]) -> str:
    """Bind an idempotency receipt to its command type and complete request body."""

    return canonical_fingerprint({"command_type": command_type, "payload": dict(payload)})


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
