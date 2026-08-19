"""Typed workflow errors translated by transport adapters."""


class WorkflowError(Exception):
    """Base error with a stable application code."""

    code = "WORKFLOW_ERROR"


class JobNotFoundError(WorkflowError):
    code = "JOB_NOT_FOUND"


class InvalidArtworkError(WorkflowError):
    code = "INVALID_ARTWORK"


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


class StaleApprovalError(WorkflowError):
    code = "STALE_APPROVAL"


class ProfileNotFoundError(WorkflowError):
    code = "PROFILE_NOT_FOUND"
