from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mr_lister.contracts import Placement, PlacementGroup, ProductProfile
from mr_lister.control.errors import InvalidControlStateError
from mr_lister.control.fingerprints import canonical_fingerprint, product_sync_record_fingerprint
from mr_lister.control.models import (
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    ProductMockupEvidence,
    ProductSyncRecord,
    ProductVariantEvidence,
    ProviderCallPermit,
    ProviderCallPermitStatus,
    ProviderUploadAttempt,
    ProviderWriteAttempt,
    ProviderWriteOperation,
    ReconciliationOutcome,
    ReviewActor,
    ReviewContent,
    SourceArtifactRecord,
    UploadedArtworkRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.control.worker_commands import (
    BeginProviderUploadCommand,
    BeginProviderWriteCommand,
    RecordPricingSuccessCommand,
    RecordProductSyncSuccessCommand,
    RecordProductWriteOutcomeUnknownCommand,
    RecordProviderUploadOutcomeUnknownCommand,
    RecordProviderUploadSuccessCommand,
    RecordReconciliationObservationCommand,
    RecordUploadReconciliationObservationCommand,
)
from mr_lister.production.draft_sync import (
    CreateAmbiguityReason,
    CreateReconciliationOutcome,
    CreateReconciliationResult,
    DraftSynchronizationEvidence,
    DraftSyncOperation,
    DraftVariantEconomics,
    PrintifyCreateOutcomeUnknown,
    PrintifyUploadOutcomeUnknown,
    UpdateReconciliationOutcome,
    UpdateReconciliationResult,
    build_canonical_draft,
    job_correlation_token,
)
from mr_lister.production.economics import ProductCostEvidence, ProductVariantCostEvidence
from mr_lister.production.phase6_worker import (
    ExactProductProfile,
    Phase6ProductMachineWorker,
)
from mr_lister.production.printify import (
    PrintifyCatalogMismatchError,
    PrintifyInputError,
    PrintifyResolvedProfile,
    PrintifyResolvedVariant,
    PrintifyUploadedImage,
)
from mr_lister.production.printify_shipping import (
    StandardUsShippingEvidence,
    parse_standard_us_shipping,
)

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
OWNER = "a" * 64
REVIEW_1_FP = "2" * 64
REVIEW_2_FP = "3" * 64
SYNC_FP = "5" * 64
RESPONSE_FP = "6" * 64
JOB_ID = "job_phase6_provider"
SYNC_WORK_ID = "work_sync"
RECON_WORK_ID = "work_reconcile"
ECONOMICS_WORK_ID = "work_economics"


def _profile() -> ProductProfile:
    return ProductProfile(
        profile_id="phase6_fixture",
        profile_version=1,
        blueprint_id=145,
        print_provider_id=39,
        colors=("Black",),
        sizes=("S",),
        retail_price_cents=2999,
        placement_groups=(
            PlacementGroup(
                group_id="small",
                sizes=("S",),
                canvas_width=3021,
                canvas_height=3927,
                placement=Placement(x=0.5, y=0.25, scale=0.65),
            ),
        ),
    )


PROFILE_FP = canonical_fingerprint(_profile())


def _source_material() -> dict[str, object]:
    return {
        "job_id": JOB_ID,
        "owner_id": OWNER,
        "bucket": "phase6-private",
        "object_key": f"private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png",
        "version_id": "version_1",
        "content_sha256": "8" * 64,
        "size_bytes": 2048,
        "media_type": "image/png",
        "product_profile_id": "phase6_fixture",
        "product_profile_version": 1,
        "product_profile_fingerprint": PROFILE_FP,
        "created_at": NOW,
    }


SOURCE_FP = source_artifact_fingerprint(**_source_material())


def _resolved() -> PrintifyResolvedProfile:
    return PrintifyResolvedProfile(
        profile_id="phase6_fixture",
        profile_version=1,
        shop_id=42,
        blueprint_id=145,
        print_provider_id=39,
        variants=(
            PrintifyResolvedVariant(
                variant_id=1000,
                color="Black",
                size="S",
                placement_group_id="small",
                canvas_width=3021,
                canvas_height=3927,
                retail_price_cents=2999,
            ),
        ),
    )


def _review(version: int) -> ReviewContent:
    fingerprint = REVIEW_1_FP if version == 1 else REVIEW_2_FP
    return ReviewContent(
        job_id=JOB_ID,
        review_version=version,
        fingerprint=fingerprint,
        actor=ReviewActor.MODEL,
        title=f"Midnight Robot Shirt v{version}",
        description="A durable machine-readable listing draft.",
        tags=tuple(f"tag {index}" for index in range(1, 14)),
        audience=("robot fans",),
        title_rationale="Names the visible subject.",
        tag_rationale="Covers subject, style, and audience.",
        validation_passed=True,
        artwork_analysis_fingerprint="7" * 64,
        product_profile_fingerprint=PROFILE_FP,
        created_at=NOW,
    )


def _source() -> SourceArtifactRecord:
    material = _source_material()
    return SourceArtifactRecord(
        fingerprint=source_artifact_fingerprint(**material),
        **material,
    )


def _work(*, work_type: WorkType, work_id: str, review_version: int) -> WorkRequest:
    return WorkRequest(
        work_request_id=work_id,
        owner_id=OWNER,
        job_id=JOB_ID,
        receipt_id=f"receipt_{work_id}",
        work_type=work_type,
        review_version=review_version,
        input_fingerprint="9" * 64,
        execution_name=f"execution_{work_id}",
        status=WorkRequestStatus.DISPATCHED,
        attempt_count=1,
        execution_arn=f"arn:aws:states:us-west-2:123:execution:test:{work_id}",
        next_dispatch_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _job(
    *,
    state: ControlJobState = ControlJobState.PRODUCT_DRAFT_SYNCING,
    review_version: int = 1,
    work_id: str = SYNC_WORK_ID,
    product_id: str | None = None,
    prior_payload_fingerprint: str | None = None,
    product_sync_id: str | None = None,
    product_sync_fingerprint: str | None = None,
    synchronized_review_version: int | None = None,
) -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=OWNER,
        job_id=JOB_ID,
        record_version=10,
        event_sequence=10,
        state=state,
        review_version=review_version,
        review_fingerprint=REVIEW_1_FP if review_version == 1 else REVIEW_2_FP,
        review_validated=True,
        source_artifact_fingerprint=SOURCE_FP,
        provider_upload_attempt_id=("upload_original" if product_id is not None else None),
        uploaded_artwork_id=("uploaded_original" if product_id is not None else None),
        uploaded_image_id=("image_old" if product_id is not None else None),
        uploaded_artwork_fingerprint=("0" * 64 if product_id is not None else None),
        product_id=product_id,
        provider_payload_fingerprint=prior_payload_fingerprint,
        product_sync_id=product_sync_id,
        synchronized_review_version=synchronized_review_version,
        product_sync_fingerprint=product_sync_fingerprint,
        active_work_request_id=work_id,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeStore:
    def __init__(self, *, job: ControlJobRecord, work: WorkRequest) -> None:
        self.job = job
        self.works = {work.work_request_id: work}
        self.reviews = {1: _review(1), 2: _review(2)}
        self.source = _source()
        self.attempts: dict[str, ProviderWriteAttempt] = {}
        self.upload_attempts: dict[str, ProviderUploadAttempt] = {}
        self.uploaded: dict[str, UploadedArtworkRecord] = {}
        self.permits: dict[str, ProviderCallPermit] = {}
        self.syncs: dict[str, ProductSyncRecord] = {}
        if job.uploaded_artwork_id is not None:
            file_name = f"mr-lister-{'a' * 24}-{'8' * 16}.png"
            self.upload_attempts["upload_original"] = ProviderUploadAttempt(
                attempt_id="upload_original",
                job_id=JOB_ID,
                work_request_id="work_original_sync",
                source_artifact_fingerprint=SOURCE_FP,
                file_name=file_name,
                reconciliation_deadline=NOW + timedelta(minutes=15),
                started_at=NOW,
            )
            self.uploaded[job.uploaded_artwork_id] = UploadedArtworkRecord(
                upload_id=job.uploaded_artwork_id,
                attempt_id="upload_original",
                job_id=JOB_ID,
                source_artifact_fingerprint=SOURCE_FP,
                image_id=job.uploaded_image_id or "",
                file_name=file_name,
                width=3021,
                height=3021,
                size_bytes=2048,
                fingerprint=job.uploaded_artwork_fingerprint or "",
                confirmed_at=NOW,
            )

    def get_job(self, job_id: str) -> ControlJobRecord:
        assert job_id == JOB_ID
        return self.job

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        assert job_id == JOB_ID
        return self.works[work_request_id]

    def get_review(self, job_id: str, review_version: int) -> ReviewContent:
        assert job_id == JOB_ID
        return self.reviews[review_version]

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord:
        assert job_id == JOB_ID
        return self.source

    def get_provider_write_attempt(self, job_id: str, attempt_id: str) -> ProviderWriteAttempt:
        assert job_id == JOB_ID
        return self.attempts[attempt_id]

    def get_provider_upload_attempt(self, job_id: str, attempt_id: str) -> ProviderUploadAttempt:
        assert job_id == JOB_ID
        return self.upload_attempts[attempt_id]

    def get_uploaded_artwork(self, job_id: str, upload_id: str) -> UploadedArtworkRecord:
        assert job_id == JOB_ID
        return self.uploaded[upload_id]

    def get_provider_call_permit(self, job_id: str, attempt_id: str) -> ProviderCallPermit:
        assert job_id == JOB_ID
        return self.permits[attempt_id]

    def get_product_sync(self, job_id: str, sync_id: str) -> ProductSyncRecord:
        assert job_id == JOB_ID
        return self.syncs[sync_id]


class FakeProfiles:
    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactProductProfile:
        assert (profile_id, profile_version) == ("phase6_fixture", 1)
        return ExactProductProfile(profile=_profile(), fingerprint=PROFILE_FP)


class ForgedFingerprintProfiles:
    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactProductProfile:
        assert (profile_id, profile_version) == ("phase6_fixture", 1)
        changed = _profile().model_copy(update={"retail_price_cents": 3199})
        return ExactProductProfile(profile=changed, fingerprint=PROFILE_FP)


class FakeSynchronizer:
    def __init__(self) -> None:
        self.mutations: list[str | None] = []
        self.drafts: list[object] = []
        self.raise_unknown = False
        self.unexpected_error: Exception | None = None
        self.create_result: CreateReconciliationResult | None = None
        self.update_result: UpdateReconciliationResult | None = None
        self.reconcile_calls = 0

    def synchronize(self, *, job_id: str, draft, product_id: str | None, prior_draft=None):
        assert job_id == JOB_ID
        if product_id is None:
            assert prior_draft is None
        else:
            assert prior_draft is not None
        self.mutations.append(product_id)
        self.drafts.append(draft)
        if self.unexpected_error is not None:
            raise self.unexpected_error
        if self.raise_unknown:
            raise PrintifyCreateOutcomeUnknown("lost response")
        return _evidence(draft=draft, product_id=product_id or "product_1")

    def reconcile_initial_create(self, *, job_id: str, draft):
        assert job_id == JOB_ID
        self.reconcile_calls += 1
        assert self.create_result is not None
        return self.create_result

    def reconcile_update(self, *, job_id: str, product_id: str, target_draft, prior_draft):
        assert job_id == JOB_ID
        assert product_id == "product_1"
        assert target_draft.payload_fingerprint != prior_draft.payload_fingerprint
        self.reconcile_calls += 1
        assert self.update_result is not None
        return self.update_result


class FakeResources:
    def __init__(self, synchronizer: FakeSynchronizer) -> None:
        self.sync = synchronizer
        self.upload_count = 0
        self.upload_sources: list[SourceArtifactRecord] = []
        self.upload_dimensions = (3021, 3021)
        self.expected_source_dimensions: tuple[int, int] | None = None
        self.geometry_checks: list[tuple[str, str]] = []
        self.preflight_count = 0
        self.uploads: dict[str, PrintifyUploadedImage] = {}
        self.upload_error: Exception | None = None
        self.upload_readback_override: PrintifyUploadedImage | None = None
        self.product_costs_override: ProductCostEvidence | None = None
        self.shipping_override: StandardUsShippingEvidence | None = None
        self.product_cost_reads: list[tuple[int, str, tuple[int, ...]]] = []
        self.shipping_reads: list[tuple[int, int, tuple[int, ...]]] = []

    def preflight(self, *, owner_id: str, profile: ProductProfile) -> PrintifyResolvedProfile:
        assert owner_id == OWNER
        assert profile == _profile()
        self.preflight_count += 1
        return _resolved()

    def upload_source(
        self, *, owner_id: str, source: SourceArtifactRecord, file_name: str
    ) -> PrintifyUploadedImage:
        assert owner_id == OWNER
        assert source.job_id == JOB_ID
        assert source.owner_id == OWNER
        self.upload_sources.append(source)
        self.upload_count += 1
        if self.upload_error is not None:
            raise self.upload_error
        width, height = self.upload_dimensions
        upload = PrintifyUploadedImage(
            image_id="image_new",
            file_name=file_name,
            width=width,
            height=height,
            size_bytes=2048,
            mime_type="image/png",
        )
        self.uploads[upload.image_id] = upload
        return self.verify_upload_source_geometry(
            owner_id=owner_id,
            source=source,
            upload=upload,
        )

    def list_uploads(self, *, owner_id: str) -> tuple[PrintifyUploadedImage, ...]:
        assert owner_id == OWNER
        return tuple(self.uploads.values())

    def get_upload(self, *, owner_id: str, image_id: str) -> PrintifyUploadedImage:
        assert owner_id == OWNER
        if self.upload_readback_override is not None:
            return self.upload_readback_override
        return self.uploads[image_id]

    def verify_upload_source_geometry(
        self,
        *,
        owner_id: str,
        source: SourceArtifactRecord,
        upload: PrintifyUploadedImage,
    ) -> PrintifyUploadedImage:
        assert owner_id == OWNER
        assert source.job_id == JOB_ID
        assert source.owner_id == OWNER
        self.geometry_checks.append((source.fingerprint, upload.image_id))
        if self.expected_source_dimensions is not None and (
            upload.width,
            upload.height,
        ) != self.expected_source_dimensions:
            raise PrintifyCatalogMismatchError("upload geometry mismatch")
        return upload

    def current_product_costs(
        self,
        *,
        owner_id: str,
        shop_id: int,
        product_id: str,
        product_sync_fingerprint: str,
        variant_ids: tuple[int, ...],
    ) -> ProductCostEvidence:
        assert owner_id == OWNER
        self.product_cost_reads.append((shop_id, product_id, variant_ids))
        if self.product_costs_override is not None:
            return self.product_costs_override
        return ProductCostEvidence(
            product_sync_fingerprint=product_sync_fingerprint,
            observed_at=NOW + timedelta(minutes=1),
            variants=tuple(
                ProductVariantCostEvidence(
                    variant_id=variant_id,
                    retail_price_cents=2999,
                    production_cost_cents=1175,
                )
                for variant_id in variant_ids
            ),
        )

    def standard_us_shipping(
        self,
        *,
        owner_id: str,
        blueprint_id: int,
        print_provider_id: int,
        variant_ids: tuple[int, ...],
    ) -> StandardUsShippingEvidence:
        assert owner_id == OWNER
        self.shipping_reads.append((blueprint_id, print_provider_id, variant_ids))
        if self.shipping_override is not None:
            return self.shipping_override
        return parse_standard_us_shipping(
            {
                "data": [
                    {
                        "type": "variant_shipping_standard_us",
                        "id": str(variant_id),
                        "attributes": {
                            "shippingType": "standard",
                            "country": {"code": "US"},
                            "variantId": variant_id,
                            "shippingPlanId": f"plan-{variant_id}",
                            "handlingTime": {"from": 4, "to": 8},
                            "shippingCost": {
                                "firstItem": {"amount": 399, "currency": "USD"},
                                "additionalItems": {"amount": 219, "currency": "USD"},
                            },
                        },
                    }
                    for variant_id in variant_ids
                ]
            },
            blueprint_id=blueprint_id,
            print_provider_id=print_provider_id,
            expected_variant_ids=variant_ids,
            observed_at=NOW + timedelta(minutes=2),
        )

    def synchronizer(self, *, owner_id: str, shop_id: int) -> FakeSynchronizer:
        assert (owner_id, shop_id) == (OWNER, 42)
        return self.sync


class FakeControl:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.successes: list[RecordProductSyncSuccessCommand] = []
        self.unknowns: list[RecordProductWriteOutcomeUnknownCommand] = []
        self.observations: list[RecordReconciliationObservationCommand] = []
        self.upload_unknowns: list[RecordProviderUploadOutcomeUnknownCommand] = []
        self.upload_observations: list[RecordUploadReconciliationObservationCommand] = []
        self.pricing_successes: list[RecordPricingSuccessCommand] = []
        self.fail_success_once = False

    def begin_provider_upload(self, command: BeginProviderUploadCommand) -> CommandResponse:
        job = self.store.job
        attempt = ProviderUploadAttempt(
            attempt_id=f"upload_attempt_{command.work_request_id}",
            job_id=job.job_id,
            work_request_id=command.work_request_id,
            source_artifact_fingerprint=command.source_artifact_fingerprint,
            file_name=command.file_name,
            reconciliation_deadline=NOW + timedelta(minutes=15),
            started_at=NOW,
        )
        self.store.upload_attempts[attempt.attempt_id] = attempt
        self.store.permits[attempt.attempt_id] = ProviderCallPermit(
            attempt_id=attempt.attempt_id,
            job_id=job.job_id,
            work_request_id=command.work_request_id,
            created_at=NOW,
        )
        self.store.job = job.model_copy(
            update={
                "record_version": job.record_version + 1,
                "event_sequence": job.event_sequence + 1,
                "provider_upload_attempt_id": attempt.attempt_id,
                "upload_outcome_unconfirmed": True,
            }
        )
        return _response(self.store.job)

    def authorize_provider_upload(
        self, *, job_id: str, attempt_id: str
    ) -> ProviderUploadAttempt | None:
        assert job_id == JOB_ID
        permit = self.store.permits[attempt_id]
        if permit.status is ProviderCallPermitStatus.CONSUMED:
            return None
        self.store.permits[attempt_id] = permit.model_copy(
            update={
                "status": ProviderCallPermitStatus.CONSUMED,
                "consumed_at": NOW,
                "consumed_work_request_id": self.store.job.active_work_request_id,
            }
        )
        return self.store.upload_attempts[attempt_id]

    def record_provider_upload_success(
        self, command: RecordProviderUploadSuccessCommand
    ) -> CommandResponse:
        job = self.store.job
        material = {
            "job_id": job.job_id,
            "attempt_id": command.attempt_id,
            "source_artifact_fingerprint": self.store.source.fingerprint,
            "image_id": command.observation.image_id,
            "file_name": command.observation.file_name,
            "width": command.observation.width,
            "height": command.observation.height,
            "size_bytes": command.observation.size_bytes,
            "mime_type": command.observation.mime_type,
        }
        upload = UploadedArtworkRecord(
            upload_id="upload_checkpoint",
            fingerprint=canonical_fingerprint(material),
            confirmed_at=NOW,
            **material,
        )
        self.store.uploaded[upload.upload_id] = upload
        self.store.job = job.model_copy(
            update={
                "record_version": job.record_version + 1,
                "event_sequence": job.event_sequence + 1,
                "uploaded_artwork_id": upload.upload_id,
                "uploaded_image_id": upload.image_id,
                "uploaded_artwork_fingerprint": upload.fingerprint,
                "upload_outcome_unconfirmed": False,
            }
        )
        return _response(self.store.job)

    def record_provider_upload_outcome_unknown(
        self, command: RecordProviderUploadOutcomeUnknownCommand
    ) -> CommandResponse:
        self.upload_unknowns.append(command)
        return _response(self.store.job)

    def begin_provider_write(self, command: BeginProviderWriteCommand) -> CommandResponse:
        job = self.store.job
        assert command.expected_record_version == job.record_version
        operation = (
            ProviderWriteOperation.CREATE
            if job.product_id is None
            else ProviderWriteOperation.UPDATE
        )
        attempt = ProviderWriteAttempt(
            attempt_id=f"attempt_{command.work_request_id}",
            job_id=job.job_id,
            work_request_id=command.work_request_id,
            review_version=job.review_version,
            operation=operation,
            product_id=job.product_id,
            image_id=command.image_id,
            target_payload_fingerprint=command.target_payload_fingerprint,
            prior_payload_fingerprint=(
                None
                if operation is ProviderWriteOperation.CREATE
                else job.provider_payload_fingerprint
            ),
            correlation_token=command.correlation_token,
            reconciliation_deadline=NOW + timedelta(minutes=15),
            started_at=NOW,
        )
        self.store.attempts[attempt.attempt_id] = attempt
        self.store.permits[attempt.attempt_id] = ProviderCallPermit(
            attempt_id=attempt.attempt_id,
            job_id=job.job_id,
            work_request_id=command.work_request_id,
            created_at=NOW,
        )
        self.store.job = job.model_copy(
            update={
                "record_version": job.record_version + 1,
                "event_sequence": job.event_sequence + 1,
                "provider_write_attempt_id": attempt.attempt_id,
                "product_create_attempt_id": (
                    attempt.attempt_id
                    if operation is ProviderWriteOperation.CREATE
                    else job.product_create_attempt_id
                ),
                "provider_outcome_unconfirmed": True,
            }
        )
        return _response(self.store.job)

    def authorize_provider_call(
        self, *, job_id: str, attempt_id: str
    ) -> ProviderWriteAttempt | None:
        assert job_id == JOB_ID
        permit = self.store.permits[attempt_id]
        if permit.status is ProviderCallPermitStatus.CONSUMED:
            return None
        self.store.permits[attempt_id] = permit.model_copy(
            update={
                "status": ProviderCallPermitStatus.CONSUMED,
                "consumed_at": NOW,
                "consumed_work_request_id": self.store.job.active_work_request_id,
            }
        )
        return self.store.attempts[attempt_id]

    def record_product_sync_success(
        self, command: RecordProductSyncSuccessCommand
    ) -> CommandResponse:
        self.successes.append(command)
        if self.fail_success_once:
            self.fail_success_once = False
            raise RuntimeError("settlement temporarily unavailable")
        return _response(self.store.job)

    def record_pricing_success(self, command: RecordPricingSuccessCommand) -> CommandResponse:
        self.pricing_successes.append(command)
        return _response(self.store.job)

    def record_product_write_outcome_unknown(
        self, command: RecordProductWriteOutcomeUnknownCommand
    ) -> CommandResponse:
        self.unknowns.append(command)
        return _response(self.store.job)

    def record_reconciliation_observation(
        self, command: RecordReconciliationObservationCommand
    ) -> CommandResponse:
        self.observations.append(command)
        return _response(self.store.job)

    def record_upload_reconciliation_observation(
        self, command: RecordUploadReconciliationObservationCommand
    ) -> CommandResponse:
        self.upload_observations.append(command)
        return _response(self.store.job)


def _response(job: ControlJobRecord) -> CommandResponse:
    return CommandResponse(
        job_id=job.job_id,
        state=job.state,
        record_version=job.record_version,
        review_version=job.review_version,
    )


def _image_id(draft) -> str:
    return draft.print_areas[0].placeholders[0].images[0].id


def _evidence(*, draft, product_id: str) -> DraftSynchronizationEvidence:
    return DraftSynchronizationEvidence(
        operation=(
            DraftSyncOperation.CREATED
            if product_id == "product_1" and draft.title.endswith("v1")
            else DraftSyncOperation.REPLACED
        ),
        product_id=product_id,
        image_id=_image_id(draft),
        request_fingerprint=draft.payload_fingerprint,
        response_fingerprint=RESPONSE_FP,
        provider_locked=False,
        provider_published=False,
        variants=(
            DraftVariantEconomics(
                variant_id=1000,
                retail_price_cents=2999,
                production_cost_cents=1100,
            ),
        ),
        mockups=(
            ProductMockupEvidence(
                url="https://images.printify.com/product_1/front.jpg",
                position="front",
                variant_ids=(1000,),
            ),
        ),
    )


def _prior_sync() -> tuple[ProductSyncRecord, str]:
    draft = build_canonical_draft(
        job_id=JOB_ID,
        listing=_listing(_review(1)),
        profile=_profile(),
        resolved=_resolved(),
        image_id="image_old",
    )
    sync = ProductSyncRecord(
        sync_id="sync_1",
        job_id=JOB_ID,
        review_version=1,
        product_id="product_1",
        printify_shop_id=42,
        image_id="image_old",
        payload_fingerprint=draft.payload_fingerprint,
        response_fingerprint=RESPONSE_FP,
        fingerprint=SYNC_FP,
        variants=(
            ProductVariantEvidence(
                variant_id=1000,
                color="Black",
                size="S",
                placement_group_id="small",
                retail_price_cents=2999,
                production_cost_cents=1100,
            ),
        ),
        synchronized_at=NOW,
    )
    sync = sync.model_copy(update={"fingerprint": product_sync_record_fingerprint(sync)})
    return sync, draft.payload_fingerprint


def _listing(review: ReviewContent):
    from mr_lister.contracts import ListingIntelligence

    return ListingIntelligence(
        title=review.title,
        description=review.description,
        tags=review.tags,
        audience=review.audience,
        title_rationale=review.title_rationale,
        tag_rationale=review.tag_rationale,
    )


def _worker(
    *, job: ControlJobRecord, work: WorkRequest, synchronizer: FakeSynchronizer
) -> tuple[Phase6ProductMachineWorker, FakeStore, FakeControl, FakeResources]:
    store = FakeStore(job=job, work=work)
    control = FakeControl(store)
    resources = FakeResources(synchronizer)
    worker = Phase6ProductMachineWorker(
        store=store,  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        profiles=FakeProfiles(),
        resources=resources,
        clock=lambda: NOW + timedelta(minutes=3),
    )
    return worker, store, control, resources


def test_economics_refresh_uses_exact_product_get_and_v2_shipping_without_mutation() -> None:
    sync_record, prior_payload = _prior_sync()
    synchronizer = FakeSynchronizer()
    worker, store, control, resources = _worker(
        job=_job(
            state=ControlJobState.PRICING_REFRESHING,
            work_id=ECONOMICS_WORK_ID,
            product_id=sync_record.product_id,
            prior_payload_fingerprint=prior_payload,
            product_sync_id=sync_record.sync_id,
            product_sync_fingerprint=sync_record.fingerprint,
            synchronized_review_version=1,
        ),
        work=_work(
            work_type=WorkType.REFRESH_ECONOMICS,
            work_id=ECONOMICS_WORK_ID,
            review_version=1,
        ),
        synchronizer=synchronizer,
    )
    store.syncs[sync_record.sync_id] = sync_record

    worker.run_economics_refresh(job_id=JOB_ID, work_request_id=ECONOMICS_WORK_ID)

    assert resources.product_cost_reads == [(42, "product_1", (1000,))]
    assert resources.shipping_reads == [(145, 39, (1000,))]
    assert resources.upload_count == 0
    assert synchronizer.mutations == []
    assert len(control.pricing_successes) == 1
    estimate = control.pricing_successes[0].estimate
    assert estimate.variants[0].production_cost_cents == 1175
    assert estimate.variants[0].production_shipping_cents == 399
    assert estimate.variants[0].estimated_proceeds_cents == 1095
    assert estimate.product_sync_fingerprint == sync_record.fingerprint


def test_economics_refresh_rejects_product_get_variant_drift_before_shipping() -> None:
    sync_record, prior_payload = _prior_sync()
    worker, store, control, resources = _worker(
        job=_job(
            state=ControlJobState.PRICING_REFRESHING,
            work_id=ECONOMICS_WORK_ID,
            product_id=sync_record.product_id,
            prior_payload_fingerprint=prior_payload,
            product_sync_id=sync_record.sync_id,
            product_sync_fingerprint=sync_record.fingerprint,
            synchronized_review_version=1,
        ),
        work=_work(
            work_type=WorkType.REFRESH_ECONOMICS,
            work_id=ECONOMICS_WORK_ID,
            review_version=1,
        ),
        synchronizer=FakeSynchronizer(),
    )
    store.syncs[sync_record.sync_id] = sync_record
    resources.product_costs_override = ProductCostEvidence(
        product_sync_fingerprint=sync_record.fingerprint,
        observed_at=NOW + timedelta(minutes=1),
        variants=(
            ProductVariantCostEvidence(
                variant_id=9999,
                retail_price_cents=2999,
                production_cost_cents=1175,
            ),
        ),
    )

    with pytest.raises(InvalidControlStateError, match="readback"):
        worker.run_economics_refresh(job_id=JOB_ID, work_request_id=ECONOMICS_WORK_ID)

    assert resources.product_cost_reads == [(42, "product_1", (1000,))]
    assert resources.shipping_reads == []
    assert control.pricing_successes == []


def test_initial_create_claims_then_consumes_one_shot_before_mutation() -> None:
    sync = FakeSynchronizer()
    worker, _store, control, resources = _worker(
        job=_job(),
        work=_work(work_type=WorkType.SYNCHRONIZE_PRODUCT, work_id=SYNC_WORK_ID, review_version=1),
        synchronizer=sync,
    )

    worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert resources.preflight_count == 1
    assert resources.upload_count == 1
    assert sync.mutations == [None]
    assert control.successes[0].observation.product_id == "product_1"
    assert control.successes[0].observation.printify_shop_id == 42
    variant = control.successes[0].observation.variants[0]
    assert variant.production_cost_cents == 1100
    assert (variant.color, variant.size, variant.placement_group_id) == (
        "Black",
        "S",
        "small",
    )
    assert control.successes[0].observation.mockups[0].variant_ids == (1000,)


def test_persisted_provider_geometry_drives_width_fixed_draft_placement() -> None:
    sync = FakeSynchronizer()
    worker, store, _control, resources = _worker(
        job=_job(),
        work=_work(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            work_id=SYNC_WORK_ID,
            review_version=1,
        ),
        synchronizer=sync,
    )
    resources.upload_dimensions = (2000, 800)

    worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert resources.upload_sources == [store.source]
    assert len(sync.drafts) == 1
    placement = sync.drafts[0].print_areas[0].placeholders[0].images[0]  # type: ignore[attr-defined]
    assert placement.x == 0.5
    assert placement.y == 0.100008
    assert placement.scale == 0.65


def test_tall_persisted_provider_geometry_is_rejected_before_product_mutation() -> None:
    sync = FakeSynchronizer()
    worker, store, _control, resources = _worker(
        job=_job(),
        work=_work(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            work_id=SYNC_WORK_ID,
            review_version=1,
        ),
        synchronizer=sync,
    )
    resources.upload_dimensions = (1000, 2100)

    with pytest.raises(PrintifyInputError, match="too tall"):
        worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert resources.upload_count == 1
    assert sync.mutations == []


def test_profile_cannot_claim_the_pinned_fingerprint_after_its_payload_changes() -> None:
    sync = FakeSynchronizer()
    job = _job()
    work = _work(
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        work_id=SYNC_WORK_ID,
        review_version=1,
    )
    store = FakeStore(job=job, work=work)
    control = FakeControl(store)
    resources = FakeResources(sync)
    worker = Phase6ProductMachineWorker(
        store=store,  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        profiles=ForgedFingerprintProfiles(),
        resources=resources,
    )

    with pytest.raises(InvalidControlStateError, match="snapshot changed"):
        worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert resources.upload_count == 0
    assert sync.mutations == []


def test_lost_upload_response_never_posts_again_and_routes_to_get_only_recovery() -> None:
    sync = FakeSynchronizer()
    worker, _store, control, resources = _worker(
        job=_job(),
        work=_work(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            work_id=SYNC_WORK_ID,
            review_version=1,
        ),
        synchronizer=sync,
    )
    resources.upload_error = PrintifyUploadOutcomeUnknown("lost upload response")

    worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)
    worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert resources.upload_count == 1
    assert sync.mutations == []
    assert len(control.upload_unknowns) == 2
    assert control.upload_unknowns[0].code == "PROVIDER_CONNECTION_LOST"


def test_upload_is_checkpointed_only_after_exact_provider_readback() -> None:
    sync = FakeSynchronizer()
    worker, _store, control, resources = _worker(
        job=_job(),
        work=_work(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            work_id=SYNC_WORK_ID,
            review_version=1,
        ),
        synchronizer=sync,
    )
    resources.upload_readback_override = PrintifyUploadedImage(
        image_id="image_new",
        file_name="wrong.png",
        width=3021,
        height=3927,
        size_bytes=2048,
        mime_type="image/png",
    )

    worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert resources.upload_count == 1
    assert sync.mutations == []
    assert len(control.upload_unknowns) == 1
    assert control.upload_unknowns[0].code == "PROVIDER_RESPONSE_INVALID"


def test_upload_reconciliation_lists_then_reads_exact_candidate_without_mutation() -> None:
    file_name = f"mr-lister-{'a' * 24}-{'8' * 16}.png"
    job = _job(
        state=ControlJobState.RECONCILIATION_REQUIRED,
        work_id=RECON_WORK_ID,
    ).model_copy(
        update={
            "provider_upload_attempt_id": "upload_attempt",
            "upload_outcome_unconfirmed": True,
        }
    )
    work = _work(
        work_type=WorkType.RECONCILE_PRODUCT,
        work_id=RECON_WORK_ID,
        review_version=1,
    )
    worker, store, control, resources = _worker(
        job=job,
        work=work,
        synchronizer=FakeSynchronizer(),
    )
    store.upload_attempts["upload_attempt"] = ProviderUploadAttempt(
        attempt_id="upload_attempt",
        job_id=JOB_ID,
        work_request_id=SYNC_WORK_ID,
        source_artifact_fingerprint=SOURCE_FP,
        file_name=file_name,
        reconciliation_deadline=NOW + timedelta(minutes=15),
        started_at=NOW,
    )
    resources.uploads["image_new"] = PrintifyUploadedImage(
        image_id="image_new",
        file_name=file_name,
        width=3021,
        height=3927,
        size_bytes=2048,
        mime_type="image/png",
    )
    resources.expected_source_dimensions = (3021, 3927)

    worker.run_product_reconciliation(job_id=JOB_ID, work_request_id=RECON_WORK_ID)

    assert resources.upload_count == 0
    assert control.upload_observations[0].outcome is ReconciliationOutcome.TARGET_MATCH
    assert control.upload_observations[0].upload is not None
    assert control.upload_observations[0].upload.image_id == "image_new"
    assert resources.geometry_checks == [(store.source.fingerprint, "image_new")]


def test_upload_reconciliation_rejects_candidate_with_changed_source_geometry() -> None:
    file_name = f"mr-lister-{'a' * 24}-{'8' * 16}.png"
    job = _job(
        state=ControlJobState.RECONCILIATION_REQUIRED,
        work_id=RECON_WORK_ID,
    ).model_copy(
        update={
            "provider_upload_attempt_id": "upload_attempt",
            "upload_outcome_unconfirmed": True,
        }
    )
    work = _work(
        work_type=WorkType.RECONCILE_PRODUCT,
        work_id=RECON_WORK_ID,
        review_version=1,
    )
    worker, store, control, resources = _worker(
        job=job,
        work=work,
        synchronizer=FakeSynchronizer(),
    )
    store.upload_attempts["upload_attempt"] = ProviderUploadAttempt(
        attempt_id="upload_attempt",
        job_id=JOB_ID,
        work_request_id=SYNC_WORK_ID,
        source_artifact_fingerprint=SOURCE_FP,
        file_name=file_name,
        reconciliation_deadline=NOW + timedelta(minutes=15),
        started_at=NOW,
    )
    resources.uploads["image_new"] = PrintifyUploadedImage(
        image_id="image_new",
        file_name=file_name,
        width=3021,
        height=3927,
        size_bytes=2048,
        mime_type="image/png",
    )
    resources.expected_source_dimensions = (3021, 3021)

    worker.run_product_reconciliation(job_id=JOB_ID, work_request_id=RECON_WORK_ID)

    assert control.upload_observations[0].outcome is ReconciliationOutcome.CONFLICT
    assert control.upload_observations[0].upload is None
    assert resources.geometry_checks == [(store.source.fingerprint, "image_new")]


def test_lost_create_response_routes_to_reconciliation_without_second_call() -> None:
    sync = FakeSynchronizer()
    sync.raise_unknown = True
    worker, _store, control, _resources = _worker(
        job=_job(),
        work=_work(work_type=WorkType.SYNCHRONIZE_PRODUCT, work_id=SYNC_WORK_ID, review_version=1),
        synchronizer=sync,
    )

    worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert sync.mutations == [None]
    assert control.unknowns[0].code == "PROVIDER_CONNECTION_LOST"


def test_unexpected_post_permit_failure_routes_to_reconciliation_without_second_call() -> None:
    sync = FakeSynchronizer()
    sync.unexpected_error = RuntimeError("private provider implementation detail")
    worker, _store, control, _resources = _worker(
        job=_job(),
        work=_work(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            work_id=SYNC_WORK_ID,
            review_version=1,
        ),
        synchronizer=sync,
    )

    worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert sync.mutations == [None]
    assert len(control.unknowns) == 1
    assert control.unknowns[0].code == "PROVIDER_RESPONSE_INVALID"


def test_replay_after_success_settlement_failure_never_mutates_twice() -> None:
    sync = FakeSynchronizer()
    worker, _store, control, resources = _worker(
        job=_job(),
        work=_work(work_type=WorkType.SYNCHRONIZE_PRODUCT, work_id=SYNC_WORK_ID, review_version=1),
        synchronizer=sync,
    )
    control.fail_success_once = True

    with pytest.raises(RuntimeError, match="settlement"):
        worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)
    worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert sync.mutations == [None]
    assert resources.upload_count == 1
    assert len(control.unknowns) == 1


def test_retry_work_reuses_unused_initial_create_claim_without_a_second_attempt() -> None:
    retry_work_id = "work_retry_sync"
    target = build_canonical_draft(
        job_id=JOB_ID,
        listing=_listing(_review(1)),
        profile=_profile(),
        resolved=_resolved(),
        image_id="image_new",
    )
    job = _job(work_id=retry_work_id).model_copy(
        update={
            "provider_upload_attempt_id": "upload_original",
            "uploaded_artwork_id": "uploaded_original",
            "uploaded_image_id": "image_new",
            "uploaded_artwork_fingerprint": "0" * 64,
            "provider_write_attempt_id": "attempt_original",
            "product_create_attempt_id": "attempt_original",
            "provider_outcome_unconfirmed": True,
        }
    )
    sync = FakeSynchronizer()
    worker, store, _control, _resources = _worker(
        job=job,
        work=_work(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            work_id=retry_work_id,
            review_version=1,
        ),
        synchronizer=sync,
    )
    store.works[SYNC_WORK_ID] = _work(
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        work_id=SYNC_WORK_ID,
        review_version=1,
    ).model_copy(
        update={
            "status": WorkRequestStatus.COMPLETED,
            "execution_arn": None,
            "updated_at": NOW,
        }
    )
    store.attempts["attempt_original"] = ProviderWriteAttempt(
        attempt_id="attempt_original",
        job_id=JOB_ID,
        work_request_id=SYNC_WORK_ID,
        review_version=1,
        operation=ProviderWriteOperation.CREATE,
        image_id="image_new",
        target_payload_fingerprint=target.payload_fingerprint,
        correlation_token=job_correlation_token(JOB_ID),
        reconciliation_deadline=NOW + timedelta(minutes=15),
        started_at=NOW,
    )
    store.permits["attempt_original"] = ProviderCallPermit(
        attempt_id="attempt_original",
        job_id=JOB_ID,
        work_request_id=SYNC_WORK_ID,
        created_at=NOW,
    )

    worker.run_product_sync(job_id=JOB_ID, work_request_id=retry_work_id)

    assert sync.mutations == [None]
    assert tuple(store.attempts) == ("attempt_original",)
    permit = store.permits["attempt_original"]
    assert permit.status is ProviderCallPermitStatus.CONSUMED
    assert permit.work_request_id == SYNC_WORK_ID
    assert permit.consumed_work_request_id == retry_work_id


def test_update_uses_only_the_application_owned_product_identity() -> None:
    prior_sync, prior_fingerprint = _prior_sync()
    sync = FakeSynchronizer()
    job = _job(
        review_version=2,
        product_id="product_1",
        prior_payload_fingerprint=prior_fingerprint,
        product_sync_id=prior_sync.sync_id,
        product_sync_fingerprint=prior_sync.fingerprint,
        synchronized_review_version=1,
    )
    worker, store, control, _resources = _worker(
        job=job,
        work=_work(work_type=WorkType.SYNCHRONIZE_PRODUCT, work_id=SYNC_WORK_ID, review_version=2),
        synchronizer=sync,
    )
    store.syncs[prior_sync.sync_id] = prior_sync

    worker.run_product_sync(job_id=JOB_ID, work_request_id=SYNC_WORK_ID)

    assert sync.mutations == ["product_1"]
    assert control.successes[0].observation.product_id == "product_1"
    assert control.successes[0].observation.printify_shop_id == 42


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("target", "target_match"),
        ("zero", "no_match"),
        ("prior", "prior_match"),
        ("conflict", "conflict"),
    ],
)
def test_reconciliation_maps_closed_get_only_outcomes(mode: str, expected: str) -> None:
    update = mode in {"prior", "conflict"}
    prior_sync, prior_fingerprint = _prior_sync()
    review_version = 2 if update else 1
    job = _job(
        state=ControlJobState.RECONCILIATION_REQUIRED,
        review_version=review_version,
        work_id=RECON_WORK_ID,
        product_id="product_1" if update else None,
        prior_payload_fingerprint=prior_fingerprint if update else None,
        product_sync_id=prior_sync.sync_id if update else None,
        product_sync_fingerprint=prior_sync.fingerprint if update else None,
        synchronized_review_version=1 if update else None,
    )
    if not update:
        job = job.model_copy(
            update={
                "provider_upload_attempt_id": "upload_original",
                "uploaded_artwork_id": "uploaded_original",
                "uploaded_image_id": "image_new",
                "uploaded_artwork_fingerprint": "0" * 64,
            }
        )
    work = _work(
        work_type=WorkType.RECONCILE_PRODUCT,
        work_id=RECON_WORK_ID,
        review_version=review_version,
    )
    sync = FakeSynchronizer()
    worker, store, control, resources = _worker(job=job, work=work, synchronizer=sync)
    if update:
        store.syncs[prior_sync.sync_id] = prior_sync
    target = build_canonical_draft(
        job_id=JOB_ID,
        listing=_listing(store.reviews[review_version]),
        profile=_profile(),
        resolved=_resolved(),
        image_id="image_old" if update else "image_new",
    )
    attempt = ProviderWriteAttempt(
        attempt_id="attempt_sync",
        job_id=JOB_ID,
        work_request_id=SYNC_WORK_ID,
        review_version=review_version,
        operation=ProviderWriteOperation.UPDATE if update else ProviderWriteOperation.CREATE,
        product_id="product_1" if update else None,
        image_id="image_old" if update else "image_new",
        target_payload_fingerprint=target.payload_fingerprint,
        prior_payload_fingerprint=prior_fingerprint if update else None,
        correlation_token=job_correlation_token(JOB_ID),
        reconciliation_deadline=NOW + timedelta(minutes=15),
        started_at=NOW,
    )
    store.attempts[attempt.attempt_id] = attempt
    store.job = store.job.model_copy(
        update={
            "provider_write_attempt_id": attempt.attempt_id,
            "provider_outcome_unconfirmed": True,
        }
    )

    if mode == "target":
        sync.create_result = CreateReconciliationResult(
            outcome=CreateReconciliationOutcome.ONE,
            evidence=_evidence(draft=target, product_id="product_1"),
        )
    elif mode == "zero":
        sync.create_result = CreateReconciliationResult(
            outcome=CreateReconciliationOutcome.ZERO,
        )
    elif mode == "prior":
        sync.update_result = UpdateReconciliationResult(
            outcome=UpdateReconciliationOutcome.PRIOR_PAYLOAD,
        )
    else:
        sync.update_result = UpdateReconciliationResult(
            outcome=UpdateReconciliationOutcome.CONFLICT,
        )

    worker.run_product_reconciliation(job_id=JOB_ID, work_request_id=RECON_WORK_ID)

    assert resources.upload_count == 0
    assert sync.mutations == []
    assert sync.reconcile_calls == 1
    assert control.observations[0].outcome.value == expected


def test_multiple_correlated_create_candidates_map_to_closed_conflict_class() -> None:
    sync = FakeSynchronizer()
    sync.create_result = CreateReconciliationResult(
        outcome=CreateReconciliationOutcome.AMBIGUOUS,
        ambiguity_reason=CreateAmbiguityReason.MULTIPLE_CORRELATED_PRODUCTS,
    )
    job = _job(
        state=ControlJobState.RECONCILIATION_REQUIRED,
        work_id=RECON_WORK_ID,
    ).model_copy(
        update={
            "provider_upload_attempt_id": "upload_original",
            "uploaded_artwork_id": "uploaded_original",
            "uploaded_image_id": "image_new",
            "uploaded_artwork_fingerprint": "0" * 64,
        }
    )
    work = _work(
        work_type=WorkType.RECONCILE_PRODUCT,
        work_id=RECON_WORK_ID,
        review_version=1,
    )
    worker, store, control, _resources = _worker(job=job, work=work, synchronizer=sync)
    target = build_canonical_draft(
        job_id=JOB_ID,
        listing=_listing(store.reviews[1]),
        profile=_profile(),
        resolved=_resolved(),
        image_id="image_new",
    )
    attempt = ProviderWriteAttempt(
        attempt_id="attempt_sync",
        job_id=JOB_ID,
        work_request_id=SYNC_WORK_ID,
        review_version=1,
        operation=ProviderWriteOperation.CREATE,
        image_id="image_new",
        target_payload_fingerprint=target.payload_fingerprint,
        correlation_token=job_correlation_token(JOB_ID),
        reconciliation_deadline=NOW + timedelta(minutes=15),
        started_at=NOW,
    )
    store.attempts[attempt.attempt_id] = attempt
    store.job = store.job.model_copy(
        update={
            "provider_write_attempt_id": attempt.attempt_id,
            "provider_outcome_unconfirmed": True,
        }
    )

    worker.run_product_reconciliation(job_id=JOB_ID, work_request_id=RECON_WORK_ID)

    assert control.observations[0].outcome.value == "multiple_matches"
