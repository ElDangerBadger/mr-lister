"""Canonical fingerprints for the provider-free Phase 7.2 execution domain."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from mr_lister.publication.contract import PHASE7_PUBLICATION_CONTRACT_VERSION
from mr_lister.publication.fingerprints import canonical_fingerprint


def execution_record_fingerprint(
    kind: str,
    value: BaseModel | Mapping[str, Any],
    *,
    excluded_fields: frozenset[str] = frozenset({"contract_version", "fingerprint"}),
) -> str:
    """Content-bind one execution record under an explicit closed namespace."""

    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", exclude=excluded_fields)
    else:
        payload = dict(value)
        for field in excluded_fields:
            payload.pop(field, None)
    return canonical_fingerprint(
        {
            "contract_version": PHASE7_PUBLICATION_CONTRACT_VERSION,
            "kind": kind,
            "payload": payload,
        }
    )


def execution_request_fingerprint(
    operation: str,
    value: BaseModel | Mapping[str, Any],
) -> str:
    """Bind every semantic command field while excluding only envelope metadata."""

    return execution_record_fingerprint(
        f"execution_request_{operation}",
        value,
        excluded_fields=frozenset({"contract_version"}),
    )


def safe_identity_digest(kind: str, value: str) -> str:
    """Digest an operational identity before it enters a sanitized terminal report."""

    return canonical_fingerprint({"kind": kind, "value": value})


def safe_listing_link_fingerprint(numeric_listing_id: int) -> str:
    """Bind the sole application-derived Etsy listing URL form."""

    return canonical_fingerprint({"url": f"https://www.etsy.com/listing/{numeric_listing_id}"})


def publication_mockup_fingerprint(
    *,
    url: str,
    position: str | None,
    variant_ids: tuple[int, ...],
) -> str:
    """Match one already-validated immutable Phase 6 mockup to provider readback."""

    return canonical_fingerprint(
        {
            "url": url,
            "position": position,
            "variant_ids": variant_ids,
        }
    )
