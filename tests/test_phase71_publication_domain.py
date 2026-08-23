from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from mr_lister.publication.commands import (
    PublicationCommandReceipt,
    PublicationCommandType,
    PublicationRequestCommit,
    PublicationRequestResponse,
    RequestPublicationCommand,
)
from mr_lister.publication.contract import (
    PublicationPermitState,
    PublicationState,
    phase7_publication_contract,
)
from mr_lister.publication.errors import (
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
)
from mr_lister.publication.fingerprints import (
    canonical_fingerprint,
    idempotency_key_digest,
    publication_aggregate_fingerprint,
    publication_attempt_fingerprint,
    publication_body_fingerprint,
    publication_command_receipt_fingerprint,
    publication_event_fingerprint,
    publication_permit_fingerprint,
    publication_request_fingerprint,
    publication_snapshot_fingerprint,
    publication_work_input_fingerprint,
)
from mr_lister.publication.models import (
    PublicationAggregate,
    PublicationAttempt,
    PublicationDomainEvent,
    PublicationJobLink,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 23, 18, 30, tzinfo=UTC)
DEADLINE = NOW + timedelta(seconds=1800)
OWNER_ID = "a" * 64


def _fp(character: str) -> str:
    return character * 64


def _command(**updates: object) -> RequestPublicationCommand:
    values: dict[str, object] = {
        "owner_id": OWNER_ID,
        "job_id": "job_71",
        "expected_record_version": 12,
        "expected_review_version": 4,
        "expected_review_fingerprint": _fp("b"),
        "expected_review_etag": _fp("c"),
        "expected_approval_decision_id": "decision_4",
        "expected_approval_fingerprint": _fp("d"),
        "confirmation": "publish_exact_approved_listing",
        "idempotency_key": "publish-key-1",
    }
    values.update(updates)
    return RequestPublicationCommand(**values)


def _snapshot(**updates: object) -> PublicationSnapshot:
    authority: dict[str, object] = {
        "owner_id": OWNER_ID,
        "job_id": "job_71",
        "expected_record_version": 12,
        "approval_decision_id": "decision_4",
        "approval_fingerprint": _fp("d"),
        "review_version": 4,
        "review_fingerprint": _fp("b"),
        "product_sync_id": "sync_4",
        "product_sync_fingerprint": _fp("e"),
        "printify_shop_id": 987654,
        "printify_product_id": "product_4",
        "printify_image_id": "image_4",
        "product_payload_fingerprint": _fp("f"),
        "pricing_snapshot_id": "pricing_4",
        "pricing_snapshot_fingerprint": _fp("1"),
        "pricing_evidence_fingerprint": _fp("2"),
        "pricing_fresh_until": NOW + timedelta(hours=1),
        "profile_id": "shirt_profile",
        "profile_version": 3,
        "profile_fingerprint": _fp("3"),
        "expected_sales_channel": "etsy",
        "publication_body_fingerprint": publication_body_fingerprint(),
        "release_manifest_fingerprint": _fp("4"),
        "requested_at": NOW,
        "verification_deadline": DEADLINE,
    }
    authority.update(updates)
    return PublicationSnapshot(
        snapshot_id="snapshot_1",
        fingerprint=publication_snapshot_fingerprint(authority),
        **authority,
    )


def _attempt(snapshot: PublicationSnapshot, **updates: object) -> PublicationAttempt:
    values: dict[str, object] = {
        "attempt_id": "attempt_1",
        "aggregate_id": "publication_1",
        "owner_id": snapshot.owner_id,
        "job_id": snapshot.job_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "root_attempt_number": 1,
        "record_version": 0,
        "shop_get_call_limit": 3,
        "shop_get_call_count": 0,
        "product_get_call_limit": 100,
        "product_get_call_count": 0,
        "publish_post_call_limit": 1,
        "publish_post_call_count": 0,
        "requested_at": snapshot.requested_at,
        "verification_deadline": snapshot.verification_deadline,
    }
    values.update(updates)
    return PublicationAttempt(
        **values,
        fingerprint=publication_attempt_fingerprint(values),
    )


def _permit(snapshot: PublicationSnapshot, **updates: object) -> PublicationPermit:
    values: dict[str, object] = {
        "permit_id": "permit_1",
        "aggregate_id": "publication_1",
        "attempt_id": "attempt_1",
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "owner_id": snapshot.owner_id,
        "job_id": snapshot.job_id,
        "work_request_id": "publication_work_1",
        "status": PublicationPermitState.AVAILABLE,
        "maximum_publish_posts_authorized": 1,
        "record_version": 0,
        "created_at": snapshot.requested_at,
    }
    values.update(updates)
    return PublicationPermit(
        **values,
        fingerprint=publication_permit_fingerprint(values),
    )


def _work(snapshot: PublicationSnapshot, **updates: object) -> PublicationWorkRequest:
    values: dict[str, object] = {
        "work_request_id": "publication_work_1",
        "aggregate_id": "publication_1",
        "attempt_id": "attempt_1",
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "permit_id": "permit_1",
        "owner_id": snapshot.owner_id,
        "job_id": snapshot.job_id,
        "receipt_id": "publication_receipt_1",
        "execution_name": "publication_execution_1",
        "record_version": 0,
        "attempt_count": 0,
        "verification_deadline": snapshot.verification_deadline,
        "next_dispatch_at": snapshot.requested_at,
        "created_at": snapshot.requested_at,
        "updated_at": snapshot.requested_at,
    }
    values.update(updates)
    return PublicationWorkRequest(
        **values,
        input_fingerprint=publication_work_input_fingerprint(values),
    )


def _aggregate(snapshot: PublicationSnapshot, **updates: object) -> PublicationAggregate:
    values: dict[str, object] = {
        "aggregate_id": "publication_1",
        "owner_id": snapshot.owner_id,
        "job_id": snapshot.job_id,
        "state": PublicationState.PUBLICATION_REQUESTED,
        "record_version": 0,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "attempt_id": "attempt_1",
        "permit_id": "permit_1",
        "work_request_id": "publication_work_1",
        "receipt_id": "publication_receipt_1",
        "requested_at": snapshot.requested_at,
        "updated_at": snapshot.requested_at,
        "terminal_at": None,
        "source_release_eligible_at": None,
        "operational_expires_at": None,
    }
    values.update(updates)
    return PublicationAggregate(
        **values,
        fingerprint=publication_aggregate_fingerprint(values),
    )


def _event(snapshot: PublicationSnapshot, **updates: object) -> PublicationDomainEvent:
    values: dict[str, object] = {
        "aggregate_id": "publication_1",
        "owner_id": snapshot.owner_id,
        "job_id": snapshot.job_id,
        "sequence": 1,
        "name": "PUBLICATION_REQUESTED",
        "state": PublicationState.PUBLICATION_REQUESTED,
        "snapshot_id": snapshot.snapshot_id,
        "attempt_id": "attempt_1",
        "permit_id": "permit_1",
        "work_request_id": "publication_work_1",
        "occurred_at": snapshot.requested_at,
    }
    values.update(updates)
    return PublicationDomainEvent(
        **values,
        fingerprint=publication_event_fingerprint(values),
    )


def _commit() -> PublicationRequestCommit:
    command = _command()
    snapshot = _snapshot()
    attempt = _attempt(snapshot)
    permit = _permit(snapshot)
    work = _work(snapshot)
    aggregate = _aggregate(snapshot)
    event = _event(snapshot)
    job_link = PublicationJobLink(
        owner_id=OWNER_ID,
        job_id=snapshot.job_id,
        expected_record_version=12,
        result_record_version=13,
        expected_event_sequence=8,
        result_event_sequence=8,
        publication_aggregate_id=aggregate.aggregate_id,
        linked_at=NOW,
    )
    response = PublicationRequestResponse(
        job_id=snapshot.job_id,
        publication_aggregate_id=aggregate.aggregate_id,
        record_version=13,
        review_version=snapshot.review_version,
        work_request_id=work.work_request_id,
        requested_at=NOW,
        verification_deadline=DEADLINE,
    )
    receipt_values: dict[str, object] = {
        "receipt_id": "publication_receipt_1",
        "owner_id": OWNER_ID,
        "job_id": snapshot.job_id,
        "aggregate_id": aggregate.aggregate_id,
        "snapshot_id": snapshot.snapshot_id,
        "attempt_id": attempt.attempt_id,
        "permit_id": permit.permit_id,
        "work_request_id": work.work_request_id,
        "command_type": PublicationCommandType.REQUEST_PUBLICATION,
        "idempotency_key_digest": idempotency_key_digest(command.idempotency_key),
        "request_fingerprint": publication_request_fingerprint(command),
        "response": response,
        "created_at": NOW,
    }
    receipt = PublicationCommandReceipt(
        **receipt_values,
        fingerprint=publication_command_receipt_fingerprint(receipt_values),
    )
    return PublicationRequestCommit(
        job_link=job_link,
        aggregate=aggregate,
        snapshot=snapshot,
        attempt=attempt,
        permit=permit,
        work_request=work,
        event=event,
        receipt=receipt,
    )


def test_strict_command_contains_only_seller_authority() -> None:
    command = _command()

    assert set(RequestPublicationCommand.model_fields) == {
        "contract_version",
        "owner_id",
        "job_id",
        "expected_record_version",
        "expected_review_version",
        "expected_review_fingerprint",
        "expected_review_etag",
        "expected_approval_decision_id",
        "expected_approval_fingerprint",
        "confirmation",
        "idempotency_key",
    }
    assert "publish-key-1" not in repr(command)
    with pytest.raises(ValidationError):
        _command(confirmation="yes")
    with pytest.raises(ValidationError):
        RequestPublicationCommand(
            **command.model_dump(mode="python"),
            printify_product_id="caller_selected_product",
        )
    with pytest.raises(ValidationError):
        _command(expected_record_version=True)


def test_request_fingerprint_binds_every_semantic_field_and_hides_key() -> None:
    original = _command()
    expected_changes: dict[str, object] = {
        "owner_id": "9" * 64,
        "job_id": "job_72",
        "expected_record_version": 13,
        "expected_review_version": 5,
        "expected_review_fingerprint": _fp("5"),
        "expected_review_etag": _fp("6"),
        "expected_approval_decision_id": "decision_5",
        "expected_approval_fingerprint": _fp("7"),
        "confirmation": "publish_exact_approved_listing",
    }
    original_fingerprint = publication_request_fingerprint(original)
    for field, changed in expected_changes.items():
        if changed == getattr(original, field):
            continue
        assert publication_request_fingerprint(_command(**{field: changed})) != original_fingerprint

    different_key = _command(idempotency_key="another-key")
    assert publication_request_fingerprint(different_key) == original_fingerprint
    assert idempotency_key_digest(different_key.idempotency_key) != idempotency_key_digest(
        original.idempotency_key
    )


def test_snapshot_fields_and_deadline_are_exact_contract_authority() -> None:
    snapshot = _snapshot()
    metadata = {"contract_version", "snapshot_id", "fingerprint"}

    assert tuple(field for field in PublicationSnapshot.model_fields if field not in metadata) == (
        phase7_publication_contract().snapshot_fields
    )
    assert snapshot.verification_deadline == snapshot.requested_at + timedelta(seconds=1800)
    assert snapshot.publication_body_fingerprint == publication_body_fingerprint()
    assert not set(phase7_publication_contract().terminal_settlement_fields).intersection(
        PublicationSnapshot.model_fields
    )


def test_snapshot_fails_closed_on_stale_time_body_or_content_mutation() -> None:
    snapshot = _snapshot()
    changed = snapshot.model_dump(mode="python")
    changed["printify_product_id"] = "different_product"
    with pytest.raises(ValidationError, match="snapshot fingerprint"):
        PublicationSnapshot.model_validate(changed)
    with pytest.raises(ValidationError, match="fresh"):
        _snapshot(pricing_fresh_until=NOW)
    with pytest.raises(ValidationError, match="1800"):
        _snapshot(verification_deadline=DEADLINE + timedelta(seconds=1))
    with pytest.raises(ValidationError, match="body fingerprint"):
        _snapshot(publication_body_fingerprint=_fp("8"))
    with pytest.raises(ValidationError, match="UTC-aware"):
        _snapshot(requested_at=NOW.replace(tzinfo=None))


def test_attempt_permit_and_work_begin_with_no_spent_authority() -> None:
    snapshot = _snapshot()
    attempt = _attempt(snapshot)
    permit = _permit(snapshot)
    work = _work(snapshot)

    assert (
        attempt.root_attempt_number,
        attempt.shop_get_call_limit,
        attempt.product_get_call_limit,
        attempt.publish_post_call_limit,
    ) == (1, 3, 100, 1)
    assert (
        attempt.shop_get_call_count,
        attempt.product_get_call_count,
        attempt.publish_post_call_count,
    ) == (0, 0, 0)
    assert permit.status is PublicationPermitState.AVAILABLE
    assert work.status.value == "pending"
    assert not {"consumed_at", "retired_at", "consumed_work_request_id"}.intersection(
        PublicationPermit.model_fields
    )

    with pytest.raises(ValidationError):
        _permit(snapshot, status=PublicationPermitState.CONSUMED)
    with pytest.raises(ValidationError):
        _attempt(snapshot, publish_post_call_count=1)


@pytest.mark.parametrize("field", ["receipt_id", "execution_name", "created_at"])
def test_work_identity_mutation_breaks_its_unchanged_input_fingerprint(field: str) -> None:
    work = _work(_snapshot())
    payload = work.model_dump(mode="python")
    payload[field] = (
        work.created_at + timedelta(seconds=1) if field == "created_at" else f"changed_{field}"
    )
    if field == "created_at":
        payload["next_dispatch_at"] = payload[field]
        payload["updated_at"] = payload[field]

    with pytest.raises(ValidationError, match="work input fingerprint"):
        PublicationWorkRequest.model_validate(payload)


def test_receipt_nested_response_mutation_breaks_its_content_fingerprint() -> None:
    receipt = _commit().receipt
    payload = receipt.model_dump(mode="python")
    response = dict(payload["response"])
    response["verification_deadline"] = DEADLINE + timedelta(seconds=1)
    payload["response"] = response

    with pytest.raises(ValidationError, match="receipt fingerprint"):
        PublicationCommandReceipt.model_validate(payload)


def test_event_payload_is_separate_and_content_bound() -> None:
    event = _event(_snapshot())
    payload = event.model_dump(mode="python")
    payload["work_request_id"] = "different_work"

    assert event.sequence == 1
    with pytest.raises(ValidationError, match="event fingerprint"):
        PublicationDomainEvent.model_validate(payload)


def test_job_link_increments_only_phase6_record_version() -> None:
    link = _commit().job_link

    assert link.phase6_state == "approved"
    assert link.result_record_version == link.expected_record_version + 1
    assert link.result_event_sequence == link.expected_event_sequence
    with pytest.raises(ValidationError, match="event_sequence"):
        PublicationJobLink.model_validate(
            {
                **link.model_dump(mode="python"),
                "result_event_sequence": link.expected_event_sequence + 1,
            }
        )


def test_request_commit_binds_every_pristine_record() -> None:
    commit = _commit()

    assert commit.aggregate.state is PublicationState.PUBLICATION_REQUESTED
    assert commit.receipt.response.publication_aggregate_id == commit.aggregate.aggregate_id
    assert commit.event.aggregate_id == commit.aggregate.aggregate_id
    assert commit.receipt.request_fingerprint == publication_request_fingerprint(_command())

    foreign_event = _event(commit.snapshot, owner_id="9" * 64)
    with pytest.raises(ValidationError, match="one owner and job"):
        PublicationRequestCommit.model_validate(
            {**commit.model_dump(mode="python"), "event": foreign_event}
        )


def test_commit_rejects_recomputed_but_moved_root_time() -> None:
    commit = _commit()
    later = NOW + timedelta(seconds=1)
    moved_attempt = _attempt(
        commit.snapshot,
        requested_at=later,
        verification_deadline=later + timedelta(seconds=1800),
    )

    with pytest.raises(ValidationError, match="one request timestamp"):
        PublicationRequestCommit.model_validate(
            {**commit.model_dump(mode="python"), "attempt": moved_attempt}
        )


def test_models_are_frozen_strict_and_fingerprint_serialization_is_canonical() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValidationError):
        snapshot.job_id = "mutated"  # type: ignore[misc]

    left = {"b": (2, 3), "a": {"when": NOW}}
    right = {"a": {"when": NOW}, "b": [2, 3]}
    assert canonical_fingerprint(left) == canonical_fingerprint(right)


def test_publication_domain_has_no_external_or_permit_consumption_surface() -> None:
    publication_files = (
        ROOT / "src" / "mr_lister" / "publication" / "commands.py",
        ROOT / "src" / "mr_lister" / "publication" / "errors.py",
        ROOT / "src" / "mr_lister" / "publication" / "fingerprints.py",
        ROOT / "src" / "mr_lister" / "publication" / "models.py",
    )
    forbidden = {
        "boto3",
        "botocore",
        "requests",
        "urllib",
        "mr_lister.cloud",
        "mr_lister.control",
        "mr_lister.production",
        "mr_lister.provider",
        "mr_lister.publication.store",
    }
    imported: set[str] = set()
    names: set[str] = set()
    for path in publication_files:
        tree = ast.parse(path.read_text())
        names.update(node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    assert not any(
        module == blocked or module.startswith(f"{blocked}.")
        for module in imported
        for blocked in forbidden
    )
    assert not any("consume" in name or "publish_post" in name for name in names)


def test_error_codes_are_closed_and_idempotency_message_is_safe() -> None:
    error = PublicationIdempotencyConflictError()

    assert error.code is PublicationErrorCode.IDEMPOTENCY_CONFLICT
    assert "publish-key-1" not in str(error)
