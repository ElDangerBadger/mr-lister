"""Stable, seller-safe errors for the Phase 6 control boundary."""


class ControlError(Exception):
    """Base error translated by the future owner-scoped API."""

    code = "CONTROL_ERROR"


class NotFoundError(ControlError):
    """Absence and ownership mismatch intentionally share one result."""

    code = "NOT_FOUND"


class InvalidControlStateError(ControlError):
    code = "INVALID_STATE"


class ConcurrentControlModificationError(ControlError):
    code = "VERSION_CONFLICT"


class StaleReviewError(ControlError):
    code = "STALE_REVIEW"


class IdempotencyConflictError(ControlError):
    code = "IDEMPOTENCY_CONFLICT"


class EconomicsStaleError(ControlError):
    code = "ECONOMICS_STALE"


class RetryNotAllowedError(ControlError):
    code = "RETRY_NOT_ALLOWED"


class SyncInProgressError(ControlError):
    code = "SYNC_IN_PROGRESS"


class ReconciliationRequiredError(ControlError):
    code = "RECONCILIATION_REQUIRED"


class WorkNotActiveError(ControlError):
    """A stale worker attempted to complete work that no longer owns the job."""

    code = "WORK_NOT_ACTIVE"
