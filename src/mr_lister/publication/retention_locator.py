"""Pure immutable locator for the owner-scoped Phase 7 request receipt."""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated

from pydantic import StringConstraints, ValidationError, model_validator

from mr_lister.publication.fingerprints import canonical_fingerprint
from mr_lister.publication.models import Fingerprint, OwnerId, PublicationModel, SafeId

PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY = "REQUEST_RECEIPT_LOCATOR"
PUBLICATION_REQUEST_RECEIPT_LOCATOR_ENTITY_TYPE = "PUBLICATION_REQUEST_RECEIPT_LOCATOR"

PhysicalOwnerPartitionKey = Annotated[
    str,
    StringConstraints(pattern=r"^OWNER#[a-f0-9]{64}$"),
]
PhysicalPublicationReceiptSortKey = Annotated[
    str,
    StringConstraints(pattern=r"^PUBLICATION_RECEIPT#[a-f0-9]{64}$"),
]


def publication_request_receipt_partition_key(owner_id: str) -> str:
    return f"OWNER#{owner_id}"


def publication_request_receipt_sort_key(job_id: str, key_digest: str) -> str:
    material = f"request_publication\0{job_id}\0{key_digest}".encode()
    return f"PUBLICATION_RECEIPT#{sha256(material).hexdigest()}"


def publication_request_receipt_locator_fingerprint(
    value: PublicationRequestReceiptLocator | dict[str, object],
) -> str:
    if isinstance(value, PublicationRequestReceiptLocator):
        payload = value.model_dump(
            mode="json",
            exclude={"contract_version", "fingerprint"},
        )
    else:
        payload = dict(value)
        payload.pop("contract_version", None)
        payload.pop("fingerprint", None)
    return canonical_fingerprint(
        {
            "contract_version": "7.0.1",
            "kind": "publication_request_receipt_locator",
            "payload": payload,
        }
    )


class PublicationRequestReceiptLocator(PublicationModel):
    """Direct physical identity and immutable content binding for one request receipt."""

    aggregate_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    receipt_id: SafeId
    receipt_fingerprint: Fingerprint
    idempotency_key_digest: Fingerprint
    owner_receipt_partition_key: PhysicalOwnerPartitionKey
    owner_receipt_sort_key: PhysicalPublicationReceiptSortKey
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def physical_identity_is_exact(self) -> PublicationRequestReceiptLocator:
        if (
            self.owner_receipt_partition_key
            != publication_request_receipt_partition_key(self.owner_id)
            or self.owner_receipt_sort_key
            != publication_request_receipt_sort_key(self.job_id, self.idempotency_key_digest)
            or self.fingerprint != publication_request_receipt_locator_fingerprint(self)
        ):
            raise ValueError("Publication request receipt locator is invalid")
        return self


def build_publication_request_receipt_locator(
    *,
    aggregate_id: str,
    owner_id: str,
    job_id: str,
    receipt_id: str,
    receipt_fingerprint: str,
    idempotency_key_digest: str,
) -> PublicationRequestReceiptLocator:
    values: dict[str, object] = {
        "aggregate_id": aggregate_id,
        "owner_id": owner_id,
        "job_id": job_id,
        "receipt_id": receipt_id,
        "receipt_fingerprint": receipt_fingerprint,
        "idempotency_key_digest": idempotency_key_digest,
        "owner_receipt_partition_key": publication_request_receipt_partition_key(owner_id),
        "owner_receipt_sort_key": publication_request_receipt_sort_key(
            job_id,
            idempotency_key_digest,
        ),
    }
    try:
        return PublicationRequestReceiptLocator(
            **values,
            fingerprint=publication_request_receipt_locator_fingerprint(values),
        )
    except (TypeError, ValidationError, ValueError):
        raise ValueError("Publication request receipt locator is invalid") from None


__all__ = [
    "PUBLICATION_REQUEST_RECEIPT_LOCATOR_ENTITY_TYPE",
    "PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY",
    "PublicationRequestReceiptLocator",
    "build_publication_request_receipt_locator",
    "publication_request_receipt_locator_fingerprint",
    "publication_request_receipt_partition_key",
    "publication_request_receipt_sort_key",
]
