"""Application-gated Phase 6.2 product machine workers.

Step Functions supplies only opaque job/work identities.  This adapter re-reads
all durable authority, derives the exact provider payload, and crosses the
external-write boundary only after :class:`WorkerControlService` atomically
consumes its one-shot permit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from mr_lister.contracts import ListingIntelligence, ProductProfile
from mr_lister.control.errors import InvalidControlStateError, WorkNotActiveError
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import (
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    ProductVariantEvidence,
    ProviderCallPermitStatus,
    ProviderUploadAttempt,
    ProviderWriteAttempt,
    ProviderWriteOperation,
    ReconciliationOutcome,
    ReviewContent,
    SourceArtifactRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.store import SellerControlStore
from mr_lister.control.worker_commands import (
    BeginProviderUploadCommand,
    BeginProviderWriteCommand,
    ProductSyncObservation,
    RecordPricingSuccessCommand,
    RecordProductSyncSuccessCommand,
    RecordProductWriteOutcomeUnknownCommand,
    RecordProviderUploadOutcomeUnknownCommand,
    RecordProviderUploadSuccessCommand,
    RecordReconciliationObservationCommand,
    RecordUploadReconciliationObservationCommand,
    UploadedArtworkObservation,
)
from mr_lister.control.worker_service import WorkerControlService
from mr_lister.production.draft_sync import (
    CanonicalPrintifyDraft,
    CreateAmbiguityReason,
    CreateReconciliationOutcome,
    DraftSynchronizationEvidence,
    PrintifyCreateOutcomeUnknown,
    PrintifyDraftSynchronizer,
    PrintifyUpdateOutcomeUnknown,
    PrintifyUploadOutcomeUnknown,
    UpdateReconciliationOutcome,
    build_canonical_draft,
    job_correlation_token,
    validate_width_first_source_fit,
)
from mr_lister.production.economics import (
    ProductCostEvidence,
    estimate_etsy_us_standard_proceeds,
)
from mr_lister.production.printify import (
    PrintifyCatalogMismatchError,
    PrintifyError,
    PrintifyInputError,
    PrintifyResolvedProfile,
    PrintifyUnavailableError,
    PrintifyUploadedImage,
)
from mr_lister.production.printify_shipping import StandardUsShippingEvidence


@dataclass(frozen=True)
class ExactProductProfile:
    """Versioned profile plus the immutable fingerprint recorded at intake."""

    profile: ProductProfile
    fingerprint: str


class ProductProfileAuthority(Protocol):
    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactProductProfile: ...


class ProviderDraftResources(Protocol):
    """Owner-scoped provider dependencies supplied by the AWS composition root."""

    def preflight(self, *, owner_id: str, profile: ProductProfile) -> PrintifyResolvedProfile: ...

    def upload_source(
        self, *, owner_id: str, source: SourceArtifactRecord, file_name: str
    ) -> PrintifyUploadedImage: ...

    def list_uploads(self, *, owner_id: str) -> tuple[PrintifyUploadedImage, ...]: ...

    def get_upload(self, *, owner_id: str, image_id: str) -> PrintifyUploadedImage: ...

    def current_product_costs(
        self,
        *,
        owner_id: str,
        shop_id: int,
        product_id: str,
        product_sync_fingerprint: str,
        variant_ids: tuple[int, ...],
    ) -> ProductCostEvidence: ...

    def standard_us_shipping(
        self,
        *,
        owner_id: str,
        blueprint_id: int,
        print_provider_id: int,
        variant_ids: tuple[int, ...],
    ) -> StandardUsShippingEvidence: ...

    def synchronizer(self, *, owner_id: str, shop_id: int) -> PrintifyDraftSynchronizer: ...


@dataclass(frozen=True)
class _DraftAuthority:
    job: ControlJobRecord
    work: WorkRequest
    source: SourceArtifactRecord
    profile: ProductProfile
    resolved: PrintifyResolvedProfile
    review: ReviewContent


class Phase6ProductMachineWorker:
    """Run the synchronize and GET-only reconciliation state-machine tasks."""

    def __init__(
        self,
        *,
        store: SellerControlStore,
        control: WorkerControlService,
        profiles: ProductProfileAuthority,
        resources: ProviderDraftResources,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._control = control
        self._profiles = profiles
        self._resources = resources
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_product_sync(self, *, job_id: str, work_request_id: str) -> CommandResponse:
        """Execute at most one externally mutating call for the active work."""

        job, work = self._active_work(
            job_id=job_id,
            work_request_id=work_request_id,
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            states={ControlJobState.PRODUCT_DRAFT_SYNCING, ControlJobState.CANCEL_REQUESTED},
        )
        authority = self._draft_authority(job=job, work=work)
        validate_width_first_source_fit(
            profile=authority.profile,
            artwork_width=authority.source.width,
            artwork_height=authority.source.height,
        )
        if job.uploaded_artwork_id is not None:
            uploaded = self._store.get_uploaded_artwork(job.job_id, job.uploaded_artwork_id)
            if (
                uploaded.image_id != job.uploaded_image_id
                or uploaded.fingerprint != job.uploaded_artwork_fingerprint
                or uploaded.source_artifact_fingerprint != authority.source.fingerprint
                or (
                    authority.source.width is not None
                    and authority.source.height is not None
                    and (uploaded.width, uploaded.height)
                    != (authority.source.width, authority.source.height)
                )
            ):
                raise InvalidControlStateError("Persisted artwork upload authority changed")
            image_id = uploaded.image_id
        else:
            upload_attempt = self._upload_attempt_for_work(job=job, work=work)
            if upload_attempt is None:
                if job.state is not ControlJobState.PRODUCT_DRAFT_SYNCING:
                    raise InvalidControlStateError("Cancellation forbids beginning an upload")
                file_name = WorkerControlService.upload_file_name(
                    job.job_id, authority.source.content_sha256
                )
                self._control.begin_provider_upload(
                    BeginProviderUploadCommand(
                        job_id=job.job_id,
                        work_request_id=work.work_request_id,
                        expected_record_version=job.record_version,
                        source_artifact_fingerprint=authority.source.fingerprint,
                        file_name=file_name,
                    )
                )
                job = self._store.get_job(job.job_id)
                if job.provider_upload_attempt_id is None:
                    raise InvalidControlStateError("Provider upload claim was not persisted")
                upload_attempt = self._store.get_provider_upload_attempt(
                    job.job_id, job.provider_upload_attempt_id
                )
            if (
                upload_attempt.source_artifact_fingerprint != authority.source.fingerprint
                or upload_attempt.file_name
                != WorkerControlService.upload_file_name(
                    job.job_id, authority.source.content_sha256
                )
            ):
                raise InvalidControlStateError("Provider upload claim changed durable authority")
            upload_permit = self._store.get_provider_call_permit(
                job.job_id, upload_attempt.attempt_id
            )
            if upload_permit.status is ProviderCallPermitStatus.CONSUMED:
                return self._record_upload_unknown(
                    job_id=job.job_id,
                    work=work,
                    attempt=upload_attempt,
                    code="PROVIDER_RESPONSE_INVALID",
                )
            if job.state is ControlJobState.CANCEL_REQUESTED:
                raise InvalidControlStateError(
                    "Cancellation forbids consuming an unused upload permit"
                )
            permitted_upload = self._control.authorize_provider_upload(
                job_id=job.job_id,
                attempt_id=upload_attempt.attempt_id,
            )
            if permitted_upload is None:
                permit = self._store.get_provider_call_permit(job.job_id, upload_attempt.attempt_id)
                if permit.status is ProviderCallPermitStatus.CONSUMED:
                    return self._record_upload_unknown(
                        job_id=job.job_id,
                        work=work,
                        attempt=upload_attempt,
                        code="PROVIDER_RESPONSE_INVALID",
                    )
                raise WorkNotActiveError("The one-shot upload permit is no longer available")
            try:
                provider_upload = self._resources.upload_source(
                    owner_id=job.owner_id,
                    source=authority.source,
                    file_name=upload_attempt.file_name,
                )
                exact_upload = self._resources.get_upload(
                    owner_id=job.owner_id,
                    image_id=provider_upload.image_id,
                )
                self._require_upload_readback(
                    returned=provider_upload,
                    exact=exact_upload,
                    source=authority.source,
                    expected_file_name=upload_attempt.file_name,
                )
                observation = self._upload_observation(exact_upload)
            except PrintifyUploadOutcomeUnknown:
                return self._record_upload_unknown(
                    job_id=job.job_id,
                    work=work,
                    attempt=upload_attempt,
                    code="PROVIDER_CONNECTION_LOST",
                )
            except PrintifyUnavailableError:
                return self._record_upload_unknown(
                    job_id=job.job_id,
                    work=work,
                    attempt=upload_attempt,
                    code="PROVIDER_TIMEOUT",
                )
            except Exception:
                return self._record_upload_unknown(
                    job_id=job.job_id,
                    work=work,
                    attempt=upload_attempt,
                    code="PROVIDER_RESPONSE_INVALID",
                )
            latest = self._store.get_job(job.job_id)
            self._control.record_provider_upload_success(
                RecordProviderUploadSuccessCommand(
                    job_id=latest.job_id,
                    work_request_id=work.work_request_id,
                    expected_record_version=latest.record_version,
                    attempt_id=upload_attempt.attempt_id,
                    observation=observation,
                )
            )
            job = self._store.get_job(job.job_id)
            authority = _DraftAuthority(
                job=job,
                work=work,
                source=authority.source,
                profile=authority.profile,
                resolved=authority.resolved,
                review=authority.review,
            )
            if job.uploaded_image_id is None:
                raise InvalidControlStateError(
                    "Provider upload success did not checkpoint an image"
                )
            image_id = job.uploaded_image_id

        active_attempt = self._attempt_for_work(job=job, work=work)
        if active_attempt is not None:
            permit = self._store.get_provider_call_permit(job.job_id, active_attempt.attempt_id)
            if permit.status is ProviderCallPermitStatus.CONSUMED:
                return self._record_unknown(
                    job_id=job.job_id,
                    work=work,
                    attempt=active_attempt,
                    code="PROVIDER_RESPONSE_INVALID",
                )
            if job.state is ControlJobState.CANCEL_REQUESTED:
                raise InvalidControlStateError(
                    "Cancellation forbids consuming an unused provider call permit"
                )
            draft = self._build_draft(
                authority=authority,
                review=authority.review,
                image_id=active_attempt.image_id,
            )
            self._require_target(attempt=active_attempt, draft=draft)
            attempt = active_attempt
        else:
            if job.state is not ControlJobState.PRODUCT_DRAFT_SYNCING:
                raise InvalidControlStateError("Cancellation forbids beginning a provider write")
            draft = self._build_draft(
                authority=authority,
                review=authority.review,
                image_id=image_id,
            )
            self._control.begin_provider_write(
                BeginProviderWriteCommand(
                    job_id=job.job_id,
                    work_request_id=work.work_request_id,
                    expected_record_version=job.record_version,
                    image_id=image_id,
                    target_payload_fingerprint=draft.payload_fingerprint,
                    correlation_token=job_correlation_token(job.job_id),
                )
            )
            claimed = self._store.get_job(job.job_id)
            if claimed.provider_write_attempt_id is None:
                raise InvalidControlStateError("Provider write claim did not persist its attempt")
            attempt = self._store.get_provider_write_attempt(
                claimed.job_id, claimed.provider_write_attempt_id
            )
            if attempt.work_request_id != work.work_request_id:
                raise InvalidControlStateError("Provider write claim changed active work identity")
            self._require_target(attempt=attempt, draft=draft)

        prior_draft = (
            self._prior_draft(authority=authority, attempt=attempt)
            if attempt.operation is ProviderWriteOperation.UPDATE
            else None
        )
        synchronizer = self._resources.synchronizer(
            owner_id=authority.job.owner_id,
            shop_id=authority.resolved.shop_id,
        )
        permitted = self._control.authorize_provider_call(
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
        )
        if permitted is None:
            latest = self._store.get_job(job.job_id)
            still_active = (
                latest.state
                in {ControlJobState.PRODUCT_DRAFT_SYNCING, ControlJobState.CANCEL_REQUESTED}
                and latest.active_work_request_id == work.work_request_id
                and latest.provider_write_attempt_id == attempt.attempt_id
            )
            permit = self._store.get_provider_call_permit(job.job_id, attempt.attempt_id)
            if still_active and permit.status is ProviderCallPermitStatus.CONSUMED:
                return self._record_unknown(
                    job_id=job.job_id,
                    work=work,
                    attempt=attempt,
                    code="PROVIDER_RESPONSE_INVALID",
                )
            raise WorkNotActiveError("The one-shot provider call permit is no longer available")

        try:
            evidence = synchronizer.synchronize(
                job_id=job.job_id,
                draft=draft,
                product_id=permitted.product_id,
                prior_draft=prior_draft,
            )
            observation = self._observation(
                evidence=evidence,
                attempt=attempt,
                resolved=authority.resolved,
            )
        except (PrintifyCreateOutcomeUnknown, PrintifyUpdateOutcomeUnknown):
            code = "PROVIDER_CONNECTION_LOST"
            return self._record_unknown(
                job_id=job.job_id,
                work=work,
                attempt=attempt,
                code=code,
            )
        except PrintifyUnavailableError:
            return self._record_unknown(
                job_id=job.job_id,
                work=work,
                attempt=attempt,
                code="PROVIDER_TIMEOUT",
            )
        except (PrintifyCatalogMismatchError, PrintifyInputError):
            return self._record_unknown(
                job_id=job.job_id,
                work=work,
                attempt=attempt,
                code="PROVIDER_RESPONSE_INVALID",
            )
        except Exception:
            # Once the one-shot permit is consumed, even an unexpected adapter failure may
            # have occurred after the provider accepted the write. Reconcile; never guess that
            # another POST or PUT is safe.
            return self._record_unknown(
                job_id=job.job_id,
                work=work,
                attempt=attempt,
                code="PROVIDER_RESPONSE_INVALID",
            )

        latest = self._store.get_job(job.job_id)
        return self._control.record_product_sync_success(
            RecordProductSyncSuccessCommand(
                job_id=latest.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=latest.record_version,
                attempt_id=attempt.attempt_id,
                observation=observation,
            )
        )

    def run_economics_refresh(self, *, job_id: str, work_request_id: str) -> CommandResponse:
        """Join two exact GET-only provider observations and persist estimated proceeds."""

        job, work = self._active_work(
            job_id=job_id,
            work_request_id=work_request_id,
            work_type=WorkType.REFRESH_ECONOMICS,
            states={ControlJobState.PRICING_REFRESHING, ControlJobState.CANCEL_REQUESTED},
        )
        authority = self._draft_authority(job=job, work=work)
        if (
            job.product_id is None
            or job.product_sync_id is None
            or job.product_sync_fingerprint is None
            or job.synchronized_review_version != job.review_version
        ):
            raise InvalidControlStateError("Economics refresh requires the current product sync")
        sync = self._store.get_product_sync(job.job_id, job.product_sync_id)
        if (
            sync.job_id != job.job_id
            or sync.product_id != job.product_id
            or sync.review_version != job.review_version
            or sync.fingerprint != job.product_sync_fingerprint
        ):
            raise InvalidControlStateError("Economics refresh changed product sync authority")
        variant_ids = tuple(item.variant_id for item in sync.variants)
        resolved_by_id = {item.variant_id: item for item in authority.resolved.variants}
        if set(resolved_by_id) != set(variant_ids) or any(
            resolved_by_id[item.variant_id].retail_price_cents != item.retail_price_cents
            for item in sync.variants
        ):
            raise InvalidControlStateError(
                "Economics refresh variants changed the pinned product profile"
            )

        product_costs = self._resources.current_product_costs(
            owner_id=job.owner_id,
            shop_id=authority.resolved.shop_id,
            product_id=sync.product_id,
            product_sync_fingerprint=sync.fingerprint,
            variant_ids=variant_ids,
        )
        live_costs_by_id = {item.variant_id: item for item in product_costs.variants}
        if (
            product_costs.product_sync_fingerprint != sync.fingerprint
            or set(live_costs_by_id) != set(variant_ids)
            or any(
                live_costs_by_id[item.variant_id].retail_price_cents != item.retail_price_cents
                for item in sync.variants
            )
        ):
            raise InvalidControlStateError(
                "Current product readback does not match synchronized variant authority"
            )
        shipping = self._resources.standard_us_shipping(
            owner_id=job.owner_id,
            blueprint_id=authority.profile.blueprint_id,
            print_provider_id=authority.profile.print_provider_id,
            variant_ids=variant_ids,
        )
        if (
            shipping.blueprint_id != authority.profile.blueprint_id
            or shipping.print_provider_id != authority.profile.print_provider_id
            or {item.variant_id for item in shipping.variants} != set(variant_ids)
        ):
            raise InvalidControlStateError(
                "Standard shipping readback does not match pinned variant authority"
            )
        estimate = estimate_etsy_us_standard_proceeds(
            product_costs=product_costs,
            shipping=shipping,
            calculated_at=self._now(),
            buyer_shipping_cents=authority.profile.buyer_shipping_cents,
        )
        latest = self._store.get_job(job.job_id)
        return self._control.record_pricing_success(
            RecordPricingSuccessCommand(
                job_id=job.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=latest.record_version,
                estimate=estimate,
            )
        )

    def run_product_reconciliation(self, *, job_id: str, work_request_id: str) -> CommandResponse:
        """Read provider state and submit a closed observation; never mutate it."""

        job, work = self._active_work(
            job_id=job_id,
            work_request_id=work_request_id,
            work_type=WorkType.RECONCILE_PRODUCT,
            states={ControlJobState.RECONCILIATION_REQUIRED, ControlJobState.CANCEL_REQUESTED},
        )
        if job.upload_outcome_unconfirmed:
            return self._run_upload_reconciliation(job=job, work=work)
        if job.provider_outcome_unconfirmed is not True:
            raise InvalidControlStateError("Reconciliation has no uncertain provider operation")
        if job.provider_write_attempt_id is None:
            raise InvalidControlStateError("Reconciliation has no immutable provider attempt")
        attempt = self._store.get_provider_write_attempt(job.job_id, job.provider_write_attempt_id)
        if attempt.review_version != job.review_version:
            raise InvalidControlStateError("Reconciliation attempt changed review authority")
        authority = self._draft_authority(job=job, work=work)
        target = self._build_draft(
            authority=authority,
            review=authority.review,
            image_id=attempt.image_id,
        )
        self._require_target(attempt=attempt, draft=target)
        synchronizer = self._resources.synchronizer(
            owner_id=job.owner_id,
            shop_id=authority.resolved.shop_id,
        )

        try:
            if attempt.operation is ProviderWriteOperation.CREATE:
                result = synchronizer.reconcile_initial_create(
                    job_id=job.job_id,
                    draft=target,
                )
                if result.outcome is CreateReconciliationOutcome.ONE:
                    assert result.evidence is not None
                    outcome = ReconciliationOutcome.TARGET_MATCH
                    product = self._observation(
                        evidence=result.evidence,
                        attempt=attempt,
                        resolved=authority.resolved,
                    )
                elif result.outcome is CreateReconciliationOutcome.ZERO:
                    outcome = ReconciliationOutcome.NO_MATCH
                    product = None
                else:
                    outcome = (
                        ReconciliationOutcome.MULTIPLE_MATCHES
                        if result.ambiguity_reason
                        is CreateAmbiguityReason.MULTIPLE_CORRELATED_PRODUCTS
                        else ReconciliationOutcome.CONFLICT
                    )
                    product = None
            else:
                prior = self._prior_draft(authority=authority, attempt=attempt)
                result = synchronizer.reconcile_update(
                    job_id=job.job_id,
                    product_id=self._required_product_id(attempt),
                    target_draft=target,
                    prior_draft=prior,
                )
                if result.outcome is UpdateReconciliationOutcome.APPLIED:
                    assert result.evidence is not None
                    outcome = ReconciliationOutcome.TARGET_MATCH
                    product = self._observation(
                        evidence=result.evidence,
                        attempt=attempt,
                        resolved=authority.resolved,
                    )
                elif result.outcome is UpdateReconciliationOutcome.PRIOR_PAYLOAD:
                    outcome = ReconciliationOutcome.PRIOR_MATCH
                    product = None
                else:
                    outcome = ReconciliationOutcome.CONFLICT
                    product = None
        except PrintifyUnavailableError:
            outcome = ReconciliationOutcome.UNAVAILABLE
            product = None
        except PrintifyError:
            outcome = ReconciliationOutcome.CONFLICT
            product = None

        latest = self._store.get_job(job.job_id)
        return self._control.record_reconciliation_observation(
            RecordReconciliationObservationCommand(
                job_id=job.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=latest.record_version,
                attempt_id=attempt.attempt_id,
                outcome=outcome,
                product=product,
                observed_payload_fingerprint=(
                    attempt.prior_payload_fingerprint
                    if outcome is ReconciliationOutcome.PRIOR_MATCH
                    else None
                ),
            )
        )

    def _run_upload_reconciliation(
        self, *, job: ControlJobRecord, work: WorkRequest
    ) -> CommandResponse:
        if job.provider_upload_attempt_id is None or job.provider_write_attempt_id is not None:
            raise InvalidControlStateError("Upload reconciliation authority is inconsistent")
        attempt = self._store.get_provider_upload_attempt(
            job.job_id, job.provider_upload_attempt_id
        )
        source = self._store.get_source_artifact(job.job_id)
        if (
            attempt.source_artifact_fingerprint != job.source_artifact_fingerprint
            or source.fingerprint != job.source_artifact_fingerprint
        ):
            raise InvalidControlStateError("Upload reconciliation changed pinned source")
        upload = None
        try:
            matches = tuple(
                item
                for item in self._resources.list_uploads(owner_id=job.owner_id)
                if item.file_name == attempt.file_name
            )
            if not matches:
                outcome = ReconciliationOutcome.NO_MATCH
            elif len(matches) > 1:
                outcome = ReconciliationOutcome.MULTIPLE_MATCHES
            else:
                exact = self._resources.get_upload(
                    owner_id=job.owner_id,
                    image_id=matches[0].image_id,
                )
                coherent = (
                    exact.image_id == matches[0].image_id
                    and exact.file_name == attempt.file_name
                    and exact.size_bytes == source.size_bytes
                    and exact.mime_type == source.media_type
                    and (
                        source.width is None
                        or source.height is None
                        or (exact.width, exact.height) == (source.width, source.height)
                    )
                )
                if coherent:
                    outcome = ReconciliationOutcome.TARGET_MATCH
                    upload = self._upload_observation(exact)
                else:
                    outcome = ReconciliationOutcome.CONFLICT
        except PrintifyUnavailableError:
            outcome = ReconciliationOutcome.UNAVAILABLE
        except PrintifyError:
            outcome = ReconciliationOutcome.CONFLICT

        latest = self._store.get_job(job.job_id)
        return self._control.record_upload_reconciliation_observation(
            RecordUploadReconciliationObservationCommand(
                job_id=job.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=latest.record_version,
                attempt_id=attempt.attempt_id,
                outcome=outcome,
                upload=upload,
            )
        )

    def _draft_authority(self, *, job: ControlJobRecord, work: WorkRequest) -> _DraftAuthority:
        source = self._store.get_source_artifact(job.job_id)
        if (
            source.job_id != job.job_id
            or source.owner_id != job.owner_id
            or source.fingerprint != job.source_artifact_fingerprint
        ):
            raise InvalidControlStateError("Pinned source artifact does not match the job")
        review = self._store.get_review(job.job_id, job.review_version)
        if (
            review.fingerprint != job.review_fingerprint
            or review.product_profile_fingerprint != source.product_profile_fingerprint
        ):
            raise InvalidControlStateError("Current review does not match pinned profile authority")
        exact = self._profiles.get_exact(
            profile_id=source.product_profile_id,
            profile_version=source.product_profile_version,
        )
        computed_profile_fingerprint = canonical_fingerprint(exact.profile)
        if (
            exact.fingerprint != computed_profile_fingerprint
            or computed_profile_fingerprint != source.product_profile_fingerprint
            or exact.profile.profile_id != source.product_profile_id
            or exact.profile.profile_version != source.product_profile_version
        ):
            raise InvalidControlStateError("Product profile snapshot changed after intake")
        resolved = self._resources.preflight(owner_id=job.owner_id, profile=exact.profile)
        return _DraftAuthority(
            job=job,
            work=work,
            source=source,
            profile=exact.profile,
            resolved=resolved,
            review=review,
        )

    def _prior_draft(
        self, *, authority: _DraftAuthority, attempt: ProviderWriteAttempt
    ) -> CanonicalPrintifyDraft:
        if authority.job.product_sync_id is None or attempt.prior_payload_fingerprint is None:
            raise InvalidControlStateError("Update reconciliation has no prior payload authority")
        sync = self._store.get_product_sync(authority.job.job_id, authority.job.product_sync_id)
        if (
            sync.product_id != attempt.product_id
            or sync.payload_fingerprint != attempt.prior_payload_fingerprint
        ):
            raise InvalidControlStateError("Prior synchronization changed product authority")
        prior_review = self._store.get_review(authority.job.job_id, sync.review_version)
        prior = self._build_draft(
            authority=authority,
            review=prior_review,
            image_id=sync.image_id,
        )
        if prior.payload_fingerprint != sync.payload_fingerprint:
            raise InvalidControlStateError("Prior provider payload cannot be reproduced exactly")
        return prior

    @staticmethod
    def _build_draft(
        *, authority: _DraftAuthority, review: ReviewContent, image_id: str
    ) -> CanonicalPrintifyDraft:
        listing = ListingIntelligence(
            title=review.title,
            description=review.description,
            tags=review.tags,
            audience=review.audience,
            title_rationale=review.title_rationale,
            tag_rationale=review.tag_rationale,
        )
        return build_canonical_draft(
            job_id=authority.job.job_id,
            listing=listing,
            profile=authority.profile,
            resolved=authority.resolved,
            image_id=image_id,
            artwork_width=authority.source.width,
            artwork_height=authority.source.height,
        )

    @staticmethod
    def _require_target(*, attempt: ProviderWriteAttempt, draft: CanonicalPrintifyDraft) -> None:
        if draft.payload_fingerprint != attempt.target_payload_fingerprint:
            raise InvalidControlStateError("Provider target payload cannot be reproduced exactly")

    @staticmethod
    def _observation(
        *,
        evidence: DraftSynchronizationEvidence,
        attempt: ProviderWriteAttempt,
        resolved: PrintifyResolvedProfile,
    ) -> ProductSyncObservation:
        if evidence.request_fingerprint != attempt.target_payload_fingerprint:
            raise InvalidControlStateError("Provider evidence does not match the claimed target")
        if evidence.image_id != attempt.image_id:
            raise InvalidControlStateError("Provider evidence changed the claimed image")
        if attempt.product_id is not None and evidence.product_id != attempt.product_id:
            raise InvalidControlStateError("Provider evidence changed immutable product identity")
        evidence_by_id = {item.variant_id: item for item in evidence.variants}
        resolved_by_id = {item.variant_id: item for item in resolved.variants}
        if set(evidence_by_id) != set(resolved_by_id) or any(
            evidence_by_id[variant_id].retail_price_cents
            != resolved_by_id[variant_id].retail_price_cents
            for variant_id in resolved_by_id
        ):
            raise InvalidControlStateError(
                "Provider evidence changed the exact configured variant identity"
            )
        return ProductSyncObservation(
            product_id=evidence.product_id,
            image_id=evidence.image_id,
            printify_shop_id=resolved.shop_id,
            request_fingerprint=evidence.request_fingerprint,
            response_fingerprint=evidence.response_fingerprint,
            mockups=evidence.mockups,
            variants=tuple(
                ProductVariantEvidence(
                    variant_id=resolved_variant.variant_id,
                    color=resolved_variant.color,
                    size=resolved_variant.size,
                    placement_group_id=resolved_variant.placement_group_id,
                    retail_price_cents=evidence_by_id[
                        resolved_variant.variant_id
                    ].retail_price_cents,
                    production_cost_cents=evidence_by_id[
                        resolved_variant.variant_id
                    ].production_cost_cents,
                )
                for resolved_variant in resolved.variants
            ),
            provider_locked=evidence.provider_locked,
            provider_published=evidence.provider_published,
        )

    def _record_unknown(
        self,
        *,
        job_id: str,
        work: WorkRequest,
        attempt: ProviderWriteAttempt,
        code: str,
    ) -> CommandResponse:
        latest = self._store.get_job(job_id)
        return self._control.record_product_write_outcome_unknown(
            RecordProductWriteOutcomeUnknownCommand(
                job_id=job_id,
                work_request_id=work.work_request_id,
                expected_record_version=latest.record_version,
                attempt_id=attempt.attempt_id,
                code=code,
            )
        )

    def _record_upload_unknown(
        self,
        *,
        job_id: str,
        work: WorkRequest,
        attempt: ProviderUploadAttempt,
        code: str,
    ) -> CommandResponse:
        latest = self._store.get_job(job_id)
        return self._control.record_provider_upload_outcome_unknown(
            RecordProviderUploadOutcomeUnknownCommand(
                job_id=job_id,
                work_request_id=work.work_request_id,
                expected_record_version=latest.record_version,
                attempt_id=attempt.attempt_id,
                code=code,
            )
        )

    @staticmethod
    def _upload_observation(upload: PrintifyUploadedImage) -> UploadedArtworkObservation:
        return UploadedArtworkObservation(
            image_id=upload.image_id,
            file_name=upload.file_name,
            width=upload.width,
            height=upload.height,
            size_bytes=upload.size_bytes,
            mime_type=upload.mime_type,
        )

    @staticmethod
    def _require_upload_readback(
        *,
        returned: PrintifyUploadedImage,
        exact: PrintifyUploadedImage,
        source: SourceArtifactRecord,
        expected_file_name: str,
    ) -> None:
        if (
            exact != returned
            or exact.file_name != expected_file_name
            or exact.size_bytes != source.size_bytes
            or exact.mime_type != source.media_type
            or (
                source.width is not None
                and source.height is not None
                and (exact.width, exact.height) != (source.width, source.height)
            )
        ):
            raise PrintifyCatalogMismatchError(
                "Printify upload readback did not match the pinned source"
            )

    def _active_work(
        self,
        *,
        job_id: str,
        work_request_id: str,
        work_type: WorkType,
        states: set[ControlJobState],
    ) -> tuple[ControlJobRecord, WorkRequest]:
        job = self._store.get_job(job_id)
        if job.state not in states or job.active_work_request_id != work_request_id:
            raise WorkNotActiveError("Machine input does not identify the active job work")
        work = self._store.get_work_request(job_id, work_request_id)
        if (
            work.job_id != job_id
            or work.owner_id != job.owner_id
            or work.work_type is not work_type
            or work.review_version != job.review_version
            or work.status not in {WorkRequestStatus.CLAIMED, WorkRequestStatus.DISPATCHED}
        ):
            raise WorkNotActiveError("Machine input does not match durable work authority")
        return job, work

    def _attempt_for_work(
        self, *, job: ControlJobRecord, work: WorkRequest
    ) -> ProviderWriteAttempt | None:
        if job.provider_write_attempt_id is None:
            return None
        attempt = self._store.get_provider_write_attempt(job.job_id, job.provider_write_attempt_id)
        if attempt.review_version != job.review_version:
            raise InvalidControlStateError("Provider attempt changed review authority")
        if attempt.work_request_id != work.work_request_id:
            permit = self._store.get_provider_call_permit(job.job_id, attempt.attempt_id)
            origin = self._store.get_work_request(job.job_id, attempt.work_request_id)
            if (
                permit.status is ProviderCallPermitStatus.CONSUMED
                and job.provider_outcome_unconfirmed
            ):
                raise InvalidControlStateError(
                    "A consumed provider write requires GET-only reconciliation"
                )
            recoverable = (
                work.work_type is WorkType.SYNCHRONIZE_PRODUCT
                and origin.work_type is WorkType.SYNCHRONIZE_PRODUCT
                and origin.status is WorkRequestStatus.COMPLETED
                and permit.status is ProviderCallPermitStatus.AVAILABLE
                and permit.consumed_at is None
                and permit.consumed_work_request_id is None
            )
            if not recoverable:
                return None
        return attempt

    def _upload_attempt_for_work(
        self, *, job: ControlJobRecord, work: WorkRequest
    ) -> ProviderUploadAttempt | None:
        if job.provider_upload_attempt_id is None:
            return None
        attempt = self._store.get_provider_upload_attempt(
            job.job_id, job.provider_upload_attempt_id
        )
        if attempt.source_artifact_fingerprint != job.source_artifact_fingerprint:
            raise InvalidControlStateError("Provider upload attempt changed source authority")
        if attempt.work_request_id != work.work_request_id:
            permit = self._store.get_provider_call_permit(job.job_id, attempt.attempt_id)
            origin = self._store.get_work_request(job.job_id, attempt.work_request_id)
            if (
                permit.status is ProviderCallPermitStatus.CONSUMED
                and job.upload_outcome_unconfirmed
            ):
                raise InvalidControlStateError(
                    "A consumed artwork upload requires GET-only reconciliation"
                )
            recoverable = (
                work.work_type is WorkType.SYNCHRONIZE_PRODUCT
                and origin.work_type is WorkType.SYNCHRONIZE_PRODUCT
                and origin.status is WorkRequestStatus.COMPLETED
                and permit.status is ProviderCallPermitStatus.AVAILABLE
            )
            if not recoverable:
                return None
        return attempt

    @staticmethod
    def _required_product_id(attempt: ProviderWriteAttempt) -> str:
        if attempt.product_id is None:
            raise InvalidControlStateError("Update attempt has no immutable product identity")
        return attempt.product_id

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise InvalidControlStateError("The economics clock must be timezone-aware")
        return now
