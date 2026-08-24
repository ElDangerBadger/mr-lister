"""Offline Phase 7.1 publication-request orchestration.

This service can only freeze approved Phase 6 authority and persist pristine publication intent.
It has no provider client, dispatcher, route, secret access, or permit-consumption capability.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from re import fullmatch
from typing import Protocol

from pydantic import ValidationError

from mr_lister.contracts import ProductProfile
from mr_lister.control.economics import EtsyUsStandardEstimate
from mr_lister.control.fingerprints import (
    canonical_fingerprint as control_fingerprint,
)
from mr_lister.control.fingerprints import (
    product_sync_record_fingerprint,
    review_etag,
)
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    PricingEvidenceRecord,
    ProductSyncRecord,
    ReviewContent,
)
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
)
from mr_lister.publication.errors import (
    PublicationAuthorityError,
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
)
from mr_lister.publication.fingerprints import (
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
    PublicationEventName,
    PublicationJobLink,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
)
from mr_lister.publication.profile_eligibility import (
    PublicationProfileEligibilityAuthority,
    PublicationProfileEligibilityError,
    require_exact_publication_profile_eligibility,
)
from mr_lister.publication.store import (
    PublicationRequestAuthority,
    PublicationRequestTransaction,
    PublicationStore,
    validate_publication_request_authority,
    validate_publication_request_transaction,
)
from mr_lister.review_profile import ExactReviewProductProfile, ReviewProfileNotFoundError


class PublicationProfileAuthority(Protocol):
    """Exact, application-owned product-profile lookup required by the frozen snapshot."""

    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
    ) -> ExactReviewProductProfile: ...


def _authority_error(message: str) -> PublicationAuthorityError:
    return PublicationAuthorityError(PublicationErrorCode.INVALID_AUTHORITY, message)


def _stale(code: PublicationErrorCode, message: str) -> None:
    raise PublicationAuthorityError(code, message)


class PublicationRequestService:
    """Freeze one exact approved listing into a pristine publication aggregate."""

    def __init__(
        self,
        *,
        store: PublicationStore,
        profiles: PublicationProfileAuthority,
        profile_eligibility: PublicationProfileEligibilityAuthority,
        release_manifest_fingerprint: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            fullmatch(r"[a-f0-9]{64}", release_manifest_fingerprint) is None
            or release_manifest_fingerprint == "0" * 64
        ):
            raise ValueError("A nonzero release manifest fingerprint is required")
        self.store = store
        self._profiles = profiles
        self._profile_eligibility = profile_eligibility
        self._release_manifest_fingerprint = release_manifest_fingerprint
        self._clock = clock or (lambda: datetime.now(UTC))

    def request_publication(
        self,
        command: RequestPublicationCommand,
    ) -> PublicationRequestResponse:
        """Persist publication intent without invoking any external capability."""

        request_fingerprint = publication_request_fingerprint(command)
        key_digest = idempotency_key_digest(command.idempotency_key)
        replay = self.store.resolve_request_receipt(
            command.owner_id,
            command.job_id,
            key_digest,
        )
        if replay is not None:
            return self._receipt_response(
                replay,
                command=command,
                key_digest=key_digest,
                request_fingerprint=request_fingerprint,
            )

        authority = self.store.load_request_authority(command.owner_id, command.job_id)
        current = authority.current_job
        self._require_owner_job(current, command)
        self._require_request_expectations(current, command)
        validate_publication_request_authority(authority)
        current_etag = self._require_exact_phase6_authority(authority)
        self._require_command_review_and_approval(command, authority, current_etag)
        exact_profile = self._require_exact_profile(authority)
        now = self._now()
        self._require_temporal_authority(authority, now)
        transaction = self._build_transaction(
            command=command,
            authority=authority,
            profile=exact_profile.profile,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        validate_publication_request_transaction(transaction)
        return self._commit_or_replay(
            transaction,
            command=command,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )

    @staticmethod
    def _require_owner_job(
        current: ControlJobRecord,
        command: RequestPublicationCommand,
    ) -> None:
        if current.owner_id != command.owner_id or current.job_id != command.job_id:
            # Stores must preserve the owner-first not-found boundary. Treat an adapter that
            # violates it as invalid rather than exposing the returned foreign authority.
            raise _authority_error("The publication store returned mismatched job authority")

    @staticmethod
    def _require_request_expectations(
        current: ControlJobRecord,
        command: RequestPublicationCommand,
    ) -> None:
        if current.state is not ControlJobState.APPROVED:
            raise PublicationAuthorityError(
                PublicationErrorCode.NOT_APPROVED,
                "The job is not approved for publication",
            )
        if current.publication_aggregate_id is not None:
            raise PublicationConflictError(
                PublicationErrorCode.ALREADY_REQUESTED,
                "Publication was already requested for this job",
            )
        if current.record_version != command.expected_record_version:
            _stale(
                PublicationErrorCode.STALE_RECORD,
                "The approved job changed before publication was requested",
            )

    @staticmethod
    def _require_exact_phase6_authority(authority: PublicationRequestAuthority) -> str:
        job = authority.current_job
        review = authority.review
        sync = authority.product_sync
        pricing = authority.pricing_snapshot
        evidence = authority.pricing_evidence

        try:
            parsed_review = ReviewContent.model_validate(review.model_dump(mode="python"))
            parsed_sync = ProductSyncRecord.model_validate(sync.model_dump(mode="python"))
            parsed_evidence = PricingEvidenceRecord.model_validate(
                evidence.model_dump(mode="python")
            )
            parsed_estimate = EtsyUsStandardEstimate.model_validate(
                evidence.estimate.model_dump(mode="python")
            )
        except (ValidationError, ValueError):
            raise _authority_error("The approved publication evidence is invalid") from None
        if (
            parsed_review != review
            or parsed_sync != sync
            or parsed_evidence != evidence
            or parsed_estimate != evidence.estimate
        ):
            raise _authority_error("The approved publication evidence is invalid")

        expected_review_fingerprint = PublicationRequestService._review_fingerprint(review)
        if (
            review.fingerprint != expected_review_fingerprint
            or review.product_profile_fingerprint != authority.source.product_profile_fingerprint
            or review.artwork_analysis_fingerprint != job.artwork_analysis_fingerprint
            or not job.review_validated
            or job.approved_review_version != review.review_version
            or job.approved_review_fingerprint != review.fingerprint
        ):
            raise _authority_error("The approved review fingerprint is invalid")

        try:
            expected_sync_fingerprint = product_sync_record_fingerprint(sync)
        except ValueError:
            raise _authority_error("The approved product synchronization is invalid") from None
        if sync.fingerprint != expected_sync_fingerprint:
            raise _authority_error("The approved product synchronization is invalid")

        estimate = evidence.estimate
        sync_variants = {item.variant_id: item for item in sync.variants}
        estimate_variants = {item.variant_id: item for item in estimate.variants}
        if (
            evidence.fingerprint != estimate.fingerprint
            or estimate.product_sync_fingerprint != sync.fingerprint
            or estimate.calculated_at != pricing.created_at
            or estimate.fresh_until != pricing.fresh_until
            or set(sync_variants) != set(estimate_variants)
            or any(
                estimate_variants[variant_id].retail_price_cents != variant.retail_price_cents
                or estimate_variants[variant_id].production_cost_cents
                != variant.production_cost_cents
                for variant_id, variant in sync_variants.items()
            )
        ):
            raise _authority_error("The approved pricing evidence is invalid")

        current_etag = review_etag(
            job_id=job.job_id,
            review_version=review.review_version,
            review_fingerprint=review.fingerprint,
            product_id=sync.product_id,
            product_sync_fingerprint=sync.fingerprint,
            pricing_snapshot_id=pricing.snapshot_id,
            pricing_snapshot_fingerprint=pricing.fingerprint,
        )
        if (
            job.approval_fingerprint != current_etag
            or authority.approval_decision.approval_fingerprint != current_etag
        ):
            raise _authority_error("The approval fingerprint is not current")
        return current_etag

    @staticmethod
    def _require_command_review_and_approval(
        command: RequestPublicationCommand,
        authority: PublicationRequestAuthority,
        current_etag: str,
    ) -> None:
        job = authority.current_job
        review = authority.review
        decision = authority.approval_decision
        if (
            command.expected_review_version != review.review_version
            or command.expected_review_version != job.review_version
            or command.expected_review_fingerprint != review.fingerprint
            or command.expected_review_fingerprint != job.review_fingerprint
            or command.expected_review_etag != current_etag
        ):
            _stale(
                PublicationErrorCode.STALE_REVIEW,
                "The approved review changed before publication was requested",
            )
        if (
            command.expected_approval_decision_id != decision.decision_id
            or command.expected_approval_decision_id != job.approval_decision_id
            or command.expected_approval_fingerprint != decision.approval_fingerprint
            or command.expected_approval_fingerprint != job.approval_fingerprint
        ):
            _stale(
                PublicationErrorCode.STALE_APPROVAL,
                "The approval changed before publication was requested",
            )

    def _require_exact_profile(
        self,
        authority: PublicationRequestAuthority,
    ) -> ExactReviewProductProfile:
        source = authority.source
        try:
            exact = self._profiles.get_exact(
                profile_id=source.product_profile_id,
                profile_version=source.product_profile_version,
            )
        except (ReviewProfileNotFoundError, LookupError, ValidationError):
            raise _authority_error("The approved product profile is unavailable") from None
        if not isinstance(exact, ExactReviewProductProfile):
            raise _authority_error("The approved product profile is unavailable")
        profile = exact.profile
        expected_fingerprint = control_fingerprint(profile)
        if (
            profile.profile_id != source.product_profile_id
            or profile.profile_version != source.product_profile_version
            or exact.fingerprint != expected_fingerprint
            or exact.fingerprint != source.product_profile_fingerprint
            or exact.fingerprint != authority.review.product_profile_fingerprint
        ):
            raise _authority_error("The approved product profile is not exact")
        if profile.publish_enabled is not False:
            raise _authority_error("The approved product profile is not draft safe")
        try:
            eligibility = self._profile_eligibility.get_exact(
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                profile_fingerprint=exact.fingerprint,
                expected_sales_channel="etsy",
                release_manifest_fingerprint=self._release_manifest_fingerprint,
                phase6_profile_publish_enabled=profile.publish_enabled,
            )
            require_exact_publication_profile_eligibility(
                eligibility,
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                profile_fingerprint=exact.fingerprint,
                expected_sales_channel="etsy",
                release_manifest_fingerprint=self._release_manifest_fingerprint,
                phase6_profile_publish_enabled=profile.publish_enabled,
            )
        except (
            PublicationProfileEligibilityError,
            LookupError,
            TypeError,
            ValidationError,
            ValueError,
        ):
            raise _authority_error(
                "The approved product profile lacks exact publication eligibility"
            ) from None

        estimate = authority.pricing_evidence.estimate
        sync = authority.product_sync
        expected_variant_pairs = {
            (color, size) for color in profile.colors for size in profile.sizes
        }
        observed_variant_pairs = {(variant.color, variant.size) for variant in sync.variants}
        group_by_size = {
            size: group.group_id for group in profile.placement_groups for size in group.sizes
        }
        if (
            estimate.blueprint_id != profile.blueprint_id
            or estimate.print_provider_id != profile.print_provider_id
            or any(
                variant.retail_price_cents != profile.retail_price_cents
                for variant in sync.variants
            )
            or any(
                variant.buyer_shipping_cents != profile.buyer_shipping_cents
                for variant in estimate.variants
            )
            or (
                profile.variant_ids
                and set(profile.variant_ids) != {variant.variant_id for variant in sync.variants}
            )
            or (
                not profile.variant_ids
                and (
                    observed_variant_pairs != expected_variant_pairs
                    or len(sync.variants) != len(expected_variant_pairs)
                    or any(
                        variant.placement_group_id != group_by_size.get(variant.size)
                        for variant in sync.variants
                    )
                )
            )
        ):
            raise _authority_error("The approved product profile does not match pricing authority")
        return exact

    @staticmethod
    def _require_temporal_authority(
        authority: PublicationRequestAuthority,
        now: datetime,
    ) -> None:
        if now < authority.current_job.updated_at:
            raise _authority_error("The publication clock precedes approved job authority")
        if now >= authority.pricing_snapshot.fresh_until:
            raise PublicationAuthorityError(
                PublicationErrorCode.PRICING_NOT_FRESH,
                "The approved pricing evidence is no longer fresh",
            )

    def _build_transaction(
        self,
        *,
        command: RequestPublicationCommand,
        authority: PublicationRequestAuthority,
        profile: ProductProfile,
        key_digest: str,
        request_fingerprint: str,
        now: datetime,
    ) -> PublicationRequestTransaction:
        current = authority.current_job
        sync = authority.product_sync
        pricing = authority.pricing_snapshot
        evidence = authority.pricing_evidence
        decision = authority.approval_decision

        aggregate_id = self._stable_id("publication", current.owner_id, current.job_id)
        snapshot_id = self._stable_id("publication_snapshot", aggregate_id)
        attempt_id = self._stable_id("publication_attempt", aggregate_id)
        permit_id = self._stable_id("publication_permit", aggregate_id)
        work_request_id = self._stable_id("publication_work", aggregate_id)
        receipt_id = self._stable_id(
            "publication_receipt",
            current.owner_id,
            current.job_id,
            key_digest,
        )
        execution_name = self._stable_id("publication_execution", work_request_id)
        deadline = now + timedelta(seconds=1800)

        snapshot_values: dict[str, object] = {
            "owner_id": current.owner_id,
            "job_id": current.job_id,
            "expected_record_version": current.record_version,
            "approval_decision_id": decision.decision_id,
            "approval_fingerprint": current.approval_fingerprint,
            "review_version": authority.review.review_version,
            "review_fingerprint": authority.review.fingerprint,
            "product_sync_id": sync.sync_id,
            "product_sync_fingerprint": sync.fingerprint,
            "printify_shop_id": sync.printify_shop_id,
            "printify_product_id": sync.product_id,
            "printify_image_id": sync.image_id,
            "product_payload_fingerprint": sync.payload_fingerprint,
            "pricing_snapshot_id": pricing.snapshot_id,
            "pricing_snapshot_fingerprint": pricing.fingerprint,
            "pricing_evidence_fingerprint": evidence.fingerprint,
            "pricing_fresh_until": pricing.fresh_until,
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "profile_fingerprint": authority.source.product_profile_fingerprint,
            "expected_sales_channel": "etsy",
            "publication_body_fingerprint": publication_body_fingerprint(),
            "release_manifest_fingerprint": self._release_manifest_fingerprint,
            "requested_at": now,
            "verification_deadline": deadline,
        }
        snapshot = PublicationSnapshot(
            snapshot_id=snapshot_id,
            fingerprint=publication_snapshot_fingerprint(snapshot_values),
            **snapshot_values,
        )

        attempt_values: dict[str, object] = {
            "attempt_id": attempt_id,
            "aggregate_id": aggregate_id,
            "owner_id": current.owner_id,
            "job_id": current.job_id,
            "snapshot_id": snapshot_id,
            "snapshot_fingerprint": snapshot.fingerprint,
            "root_attempt_number": 1,
            "record_version": 0,
            "shop_get_call_limit": 3,
            "shop_get_call_count": 0,
            "product_get_call_limit": 100,
            "product_get_call_count": 0,
            "publish_post_call_limit": 1,
            "publish_post_call_count": 0,
            "requested_at": now,
            "verification_deadline": deadline,
        }
        attempt = PublicationAttempt(
            **attempt_values,
            fingerprint=publication_attempt_fingerprint(attempt_values),
        )

        permit_values: dict[str, object] = {
            "permit_id": permit_id,
            "aggregate_id": aggregate_id,
            "attempt_id": attempt_id,
            "snapshot_id": snapshot_id,
            "snapshot_fingerprint": snapshot.fingerprint,
            "owner_id": current.owner_id,
            "job_id": current.job_id,
            "work_request_id": work_request_id,
            "status": PublicationPermitState.AVAILABLE,
            "maximum_publish_posts_authorized": 1,
            "record_version": 0,
            "created_at": now,
        }
        permit = PublicationPermit(
            **permit_values,
            fingerprint=publication_permit_fingerprint(permit_values),
        )

        work_values: dict[str, object] = {
            "work_request_id": work_request_id,
            "aggregate_id": aggregate_id,
            "attempt_id": attempt_id,
            "snapshot_id": snapshot_id,
            "snapshot_fingerprint": snapshot.fingerprint,
            "permit_id": permit_id,
            "owner_id": current.owner_id,
            "job_id": current.job_id,
            "receipt_id": receipt_id,
            "execution_name": execution_name,
            "record_version": 0,
            "attempt_count": 0,
            "verification_deadline": deadline,
            "next_dispatch_at": now,
            "created_at": now,
            "updated_at": now,
        }
        work = PublicationWorkRequest(
            **work_values,
            input_fingerprint=publication_work_input_fingerprint(work_values),
        )

        aggregate_values: dict[str, object] = {
            "aggregate_id": aggregate_id,
            "owner_id": current.owner_id,
            "job_id": current.job_id,
            "state": PublicationState.PUBLICATION_REQUESTED,
            "record_version": 0,
            "snapshot_id": snapshot_id,
            "snapshot_fingerprint": snapshot.fingerprint,
            "attempt_id": attempt_id,
            "permit_id": permit_id,
            "work_request_id": work_request_id,
            "receipt_id": receipt_id,
            "requested_at": now,
            "updated_at": now,
            "terminal_at": None,
            "source_release_eligible_at": None,
            "operational_expires_at": None,
        }
        aggregate = PublicationAggregate(
            **aggregate_values,
            fingerprint=publication_aggregate_fingerprint(aggregate_values),
        )

        event_values: dict[str, object] = {
            "aggregate_id": aggregate_id,
            "owner_id": current.owner_id,
            "job_id": current.job_id,
            "sequence": 1,
            "name": PublicationEventName.PUBLICATION_REQUESTED,
            "state": PublicationState.PUBLICATION_REQUESTED,
            "snapshot_id": snapshot_id,
            "attempt_id": attempt_id,
            "permit_id": permit_id,
            "work_request_id": work_request_id,
            "occurred_at": now,
        }
        event = PublicationDomainEvent(
            **event_values,
            fingerprint=publication_event_fingerprint(event_values),
        )
        link = PublicationJobLink(
            owner_id=current.owner_id,
            job_id=current.job_id,
            expected_record_version=current.record_version,
            result_record_version=current.record_version + 1,
            expected_event_sequence=current.event_sequence,
            result_event_sequence=current.event_sequence,
            publication_aggregate_id=aggregate_id,
            linked_at=now,
        )
        updated_job = ControlJobRecord.model_validate(
            {
                **current.model_dump(mode="python"),
                "record_version": current.record_version + 1,
                "publication_aggregate_id": aggregate_id,
                "updated_at": now,
            }
        )
        response = PublicationRequestResponse(
            job_id=current.job_id,
            publication_aggregate_id=aggregate_id,
            record_version=updated_job.record_version,
            review_version=current.review_version,
            work_request_id=work_request_id,
            requested_at=now,
            verification_deadline=deadline,
        )
        receipt_values: dict[str, object] = {
            "receipt_id": receipt_id,
            "owner_id": current.owner_id,
            "job_id": current.job_id,
            "aggregate_id": aggregate_id,
            "snapshot_id": snapshot_id,
            "attempt_id": attempt_id,
            "permit_id": permit_id,
            "work_request_id": work_request_id,
            "command_type": PublicationCommandType.REQUEST_PUBLICATION,
            "idempotency_key_digest": key_digest,
            "request_fingerprint": request_fingerprint,
            "response": response,
            "created_at": now,
        }
        receipt = PublicationCommandReceipt(
            **receipt_values,
            fingerprint=publication_command_receipt_fingerprint(receipt_values),
        )
        commit = PublicationRequestCommit(
            job_link=link,
            aggregate=aggregate,
            snapshot=snapshot,
            attempt=attempt,
            permit=permit,
            work_request=work,
            event=event,
            receipt=receipt,
        )
        return PublicationRequestTransaction(
            authority=authority,
            updated_job=updated_job,
            commit=commit,
        )

    def _commit_or_replay(
        self,
        transaction: PublicationRequestTransaction,
        *,
        command: RequestPublicationCommand,
        key_digest: str,
        request_fingerprint: str,
    ) -> PublicationRequestResponse:
        try:
            receipt = self.store.commit_request(transaction)
        except (PublicationIdempotencyConflictError, PublicationConflictError):
            receipt = self.store.resolve_request_receipt(
                command.owner_id,
                command.job_id,
                key_digest,
            )
            if receipt is None:
                raise
        return self._receipt_response(
            receipt,
            command=command,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )

    @staticmethod
    def _receipt_response(
        receipt: PublicationCommandReceipt,
        *,
        command: RequestPublicationCommand,
        key_digest: str,
        request_fingerprint: str,
    ) -> PublicationRequestResponse:
        try:
            revalidated = PublicationCommandReceipt.model_validate(
                receipt.model_dump(mode="python")
            )
        except (AttributeError, ValidationError, ValueError):
            raise _authority_error("The publication receipt is invalid") from None
        if revalidated != receipt:
            raise _authority_error("The publication receipt is invalid")
        aggregate_id = PublicationRequestService._stable_id(
            "publication",
            command.owner_id,
            command.job_id,
        )
        expected_identity = (
            PublicationRequestService._stable_id(
                "publication_receipt",
                command.owner_id,
                command.job_id,
                key_digest,
            ),
            aggregate_id,
            PublicationRequestService._stable_id("publication_snapshot", aggregate_id),
            PublicationRequestService._stable_id("publication_attempt", aggregate_id),
            PublicationRequestService._stable_id("publication_permit", aggregate_id),
            PublicationRequestService._stable_id("publication_work", aggregate_id),
        )
        actual_identity = (
            receipt.receipt_id,
            receipt.aggregate_id,
            receipt.snapshot_id,
            receipt.attempt_id,
            receipt.permit_id,
            receipt.work_request_id,
        )
        if (
            receipt.owner_id != command.owner_id
            or receipt.job_id != command.job_id
            or receipt.idempotency_key_digest != key_digest
            or actual_identity != expected_identity
        ):
            raise _authority_error("The publication receipt identity is invalid")
        if receipt.request_fingerprint != request_fingerprint:
            raise PublicationIdempotencyConflictError()
        if (
            receipt.response.record_version != command.expected_record_version + 1
            or receipt.response.review_version != command.expected_review_version
            or receipt.response.verification_deadline
            != receipt.response.requested_at + timedelta(seconds=1800)
        ):
            raise _authority_error("The publication receipt response is invalid")
        return receipt.response

    @staticmethod
    def _review_fingerprint(review: ReviewContent) -> str:
        return control_fingerprint(
            {
                "contract_version": review.contract_version,
                "job_id": review.job_id,
                "review_version": review.review_version,
                "actor": review.actor.value,
                "title": review.title,
                "description": review.description,
                "tags": review.tags,
                "audience": review.audience,
                "title_rationale": review.title_rationale,
                "tag_rationale": review.tag_rationale,
                "validation_passed": review.validation_passed,
                "validation_issue_codes": review.validation_issue_codes,
                "artwork_analysis_fingerprint": review.artwork_analysis_fingerprint,
                "product_profile_fingerprint": review.product_profile_fingerprint,
                "created_at": review.created_at.isoformat(),
            }
        )

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = sha256("\x00".join(parts).encode()).hexdigest()[:40]
        return f"{prefix}_{digest}"

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise _authority_error("The publication clock must be timezone-aware")
        return now.astimezone(UTC)


__all__ = ["PublicationProfileAuthority", "PublicationRequestService"]
