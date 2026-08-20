"""Typed workflow errors translated by transport adapters."""


class WorkflowError(Exception):
    """Base error with a stable application code."""

    code = "WORKFLOW_ERROR"


class JobNotFoundError(WorkflowError):
    code = "JOB_NOT_FOUND"


class InvalidArtworkError(WorkflowError):
    code = "INVALID_ARTWORK"


class ArtifactIntegrityError(WorkflowError):
    code = "ARTIFACT_INTEGRITY"


class InvalidGeneratedOutputError(WorkflowError):
    code = "INVALID_GENERATED_OUTPUT"


class IntelligenceUnavailableError(WorkflowError):
    """A retryable model-provider failure."""

    code = "INTELLIGENCE_UNAVAILABLE"


class IntelligenceConfigurationError(WorkflowError):
    """A non-retryable provider configuration, authorization, or invocation failure."""

    code = "INTELLIGENCE_CONFIGURATION"


class IdempotencyConflictError(WorkflowError):
    code = "IDEMPOTENCY_CONFLICT"


class InvalidStateError(WorkflowError):
    code = "INVALID_STATE"


class ConcurrentModificationError(WorkflowError):
    """The persisted record changed after the caller read it."""

    code = "CONCURRENT_MODIFICATION"


class ExternalWritePendingError(WorkflowError):
    """A prior claimed write needs reconciliation before it can be retried."""

    code = "EXTERNAL_WRITE_PENDING"


class ApprovalWaitExpiredError(WorkflowError):
    code = "APPROVAL_WAIT_EXPIRED"


class StaleApprovalError(WorkflowError):
    code = "STALE_APPROVAL"


class ProfileNotFoundError(WorkflowError):
    code = "PROFILE_NOT_FOUND"
