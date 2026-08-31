"""Closed application-boundary tests for the private Phase 7.6 guard."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mr_lister.publication.guard_verification import (
    PublicationGuardOperation,
    PublicationGuardOutcome,
    PublicationGuardRequest,
    PublicationGuardRuntimeActivation,
    PublicationGuardVerificationService,
    PublicationPreCallAuthorityError,
    _source_artifact_fingerprint,
)
from tests.test_phase72_publication_execution import Harness

OWNER_ID = "a" * 64
AGGREGATE_ID = "aggregate_phase76_private"
RELEASE_FINGERPRINT = "b" * 64
PROFILE_FINGERPRINT = "c" * 64


class _Guard:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def require_current(self, *, owner_id: str, aggregate_id: str) -> object:
        self.calls.append((owner_id, aggregate_id))
        if self.error is not None:
            raise self.error
        return object()


def _service(guard: _Guard) -> PublicationGuardVerificationService:
    return PublicationGuardVerificationService(
        guard=guard,
        activation=PublicationGuardRuntimeActivation(),
        guard_release_fingerprint=RELEASE_FINGERPRINT,
        profile_fingerprint=PROFILE_FINGERPRINT,
    )


def test_status_attests_only_sealed_disabled_configuration_without_data_access() -> None:
    guard = _Guard(AssertionError("status must not read authority"))

    result = _service(guard).handle({"operation": "status"})

    assert result.operation is PublicationGuardOperation.STATUS
    assert result.outcome is PublicationGuardOutcome.SEALED_CONFIGURATION
    assert result.approval_authority_current is None
    assert guard.calls == []
    assert result.query_enabled is False
    assert result.request_enabled is False
    assert result.publication_enabled is False
    assert result.provider_calls_authorized == 0


def test_exact_authority_returns_identifier_free_current_attestation() -> None:
    guard = _Guard()

    result = _service(guard).handle(
        {
            "operation": "verify_authority",
            "owner_id": OWNER_ID,
            "aggregate_id": AGGREGATE_ID,
        }
    )

    assert result.outcome is PublicationGuardOutcome.AUTHORITY_CURRENT
    assert result.approval_authority_current is True
    assert guard.calls == [(OWNER_ID, AGGREGATE_ID)]
    rendered = result.model_dump_json()
    assert OWNER_ID not in rendered
    assert AGGREGATE_ID not in rendered
    assert "job_id" not in rendered
    assert "shop_id" not in rendered


def test_guard_fingerprint_replica_preserves_agentcore_v1_source_authority() -> None:
    harness = Harness()
    source = harness.store.load_source_authority(OWNER_ID, harness.aggregate_id).source

    assert _source_artifact_fingerprint(source) == source.fingerprint
    assert "width" not in source.model_dump(mode="json")
    assert "height" not in source.model_dump(mode="json")


@pytest.mark.parametrize(
    "error",
    [
        PublicationPreCallAuthorityError("stale approval"),
        LookupError("missing aggregate"),
        RuntimeError("dynamo endpoint and secret detail"),
    ],
)
def test_all_authority_failures_collapse_to_one_sanitized_closed_result(error: Exception) -> None:
    result = _service(_Guard(error)).handle(
        {
            "operation": "verify_authority",
            "owner_id": OWNER_ID,
            "aggregate_id": AGGREGATE_ID,
        }
    )

    assert result.outcome is PublicationGuardOutcome.AUTHORITY_REJECTED
    assert result.approval_authority_current is False
    rendered = result.model_dump_json()
    assert OWNER_ID not in rendered
    assert AGGREGATE_ID not in rendered
    assert str(error) not in rendered
    assert set(json.loads(rendered)) == {
        "contract_version",
        "contract_fingerprint",
        "guard_release_fingerprint",
        "profile_fingerprint",
        "operation",
        "outcome",
        "approval_authority_current",
        "approval_guard_enabled",
        "query_enabled",
        "request_enabled",
        "publication_enabled",
        "provider_calls_authorized",
        "fingerprint",
    }


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"operation": "status", "owner_id": OWNER_ID},
        {"operation": "verify_authority", "owner_id": OWNER_ID},
        {
            "operation": "verify_authority",
            "owner_id": OWNER_ID,
            "aggregate_id": AGGREGATE_ID,
            "extra": True,
        },
        {
            "operation": "verify_authority",
            "owner_id": OWNER_ID,
            "aggregate_id": f" {AGGREGATE_ID} ",
        },
        {"operation": 1},
    ],
)
def test_request_contract_rejects_missing_extra_cross_operation_and_coerced_input(
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        PublicationGuardRequest.model_validate(value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("scaffold_only", True),
        ("approval_guard_enabled", False),
        ("query_enabled", True),
        ("request_enabled", True),
        ("publication_enabled", True),
        ("publication_enabled", 0),
    ],
)
def test_guard_activation_is_one_exact_nonpublishing_tuple(name: str, value: object) -> None:
    values: dict[str, object] = {
        "scaffold_only": False,
        "approval_guard_enabled": True,
        "query_enabled": False,
        "request_enabled": False,
        "publication_enabled": False,
    }
    values[name] = value
    with pytest.raises(ValidationError):
        PublicationGuardRuntimeActivation.model_validate(values)
