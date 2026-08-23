"""Stable, provider-independent Phase 7 publication domain errors."""

from __future__ import annotations

from enum import StrEnum


class PublicationErrorCode(StrEnum):
    NOT_FOUND = "PUBLICATION_NOT_FOUND"
    NOT_APPROVED = "PUBLICATION_NOT_APPROVED"
    ALREADY_REQUESTED = "PUBLICATION_ALREADY_REQUESTED"
    STALE_RECORD = "PUBLICATION_STALE_RECORD"
    STALE_REVIEW = "PUBLICATION_STALE_REVIEW"
    STALE_APPROVAL = "PUBLICATION_STALE_APPROVAL"
    INVALID_AUTHORITY = "PUBLICATION_INVALID_AUTHORITY"
    PRICING_NOT_FRESH = "PUBLICATION_PRICING_NOT_FRESH"
    IDEMPOTENCY_CONFLICT = "PUBLICATION_IDEMPOTENCY_CONFLICT"
    CONCURRENT_WRITE = "PUBLICATION_CONCURRENT_WRITE"


class PublicationDomainError(Exception):
    """Base error carrying only a closed code and a safe application message."""

    def __init__(self, code: PublicationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PublicationNotFoundError(PublicationDomainError):
    def __init__(self, message: str = "Publication authority was not found") -> None:
        super().__init__(PublicationErrorCode.NOT_FOUND, message)


class PublicationAuthorityError(PublicationDomainError):
    pass


class PublicationConflictError(PublicationDomainError):
    pass


class PublicationIdempotencyConflictError(PublicationConflictError):
    def __init__(self, message: str = "Idempotency key was reused with a changed request") -> None:
        super().__init__(PublicationErrorCode.IDEMPOTENCY_CONFLICT, message)
