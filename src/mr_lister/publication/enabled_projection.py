"""Seller-safe general-availability projection for contract 7.1.0."""

from __future__ import annotations

from typing import Literal

from pydantic import StrictBool, ValidationError, model_validator

from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import ControlJobRecord, ControlJobState
from mr_lister.publication.fingerprints import canonical_fingerprint
from mr_lister.publication.projection import (
    PublicationProjectionAuthority,
    PublicationProjectionStore,
    PublicationProjectionUnavailableError,
    SellerPublicationProjectionService,
)
from mr_lister.publication.projection_models import SellerPublicationProjection

Phase718RequestDisabledReason = Literal[
    "PUBLICATION_NOT_ELIGIBLE",
    "PUBLICATION_ALREADY_REQUESTED",
]


class Phase718SellerPublicationProjection(SellerPublicationProjection):
    """The existing safe projection with only its activation fields advanced."""

    contract_version: Literal["7.1.0"] = "7.1.0"
    publication_enabled: Literal[True] = True
    request_enabled: StrictBool
    request_disabled_reason: Phase718RequestDisabledReason | None = None

    @model_validator(mode="after")
    def request_availability_matches_state(self) -> Phase718SellerPublicationProjection:
        if self.request_enabled:
            if self.state != "not_requested" or self.request_disabled_reason is not None:
                raise ValueError("Enabled publication request projection is inconsistent")
        elif self.request_disabled_reason is None:
            raise ValueError("Unavailable publication request requires a public reason")
        elif (
            self.state == "not_requested"
            and self.request_disabled_reason != "PUBLICATION_NOT_ELIGIBLE"
        ):
            raise ValueError("Unrequested publication has the wrong disabled reason")
        elif (
            self.state != "not_requested"
            and self.request_disabled_reason != "PUBLICATION_ALREADY_REQUESTED"
        ):
            raise ValueError("Requested publication has the wrong disabled reason")
        return self


class Phase718SellerPublicationProjectionService:
    """Advance only public activation flags over the validated 7.0.1 projection graph."""

    __slots__ = ("_store",)

    def __init__(self, store: PublicationProjectionStore) -> None:
        self._store = store

    def get(self, *, owner_id: str, job_id: str) -> Phase718SellerPublicationProjection:
        job = self._load_job(owner_id=owner_id, job_id=job_id)
        if job.publication_aggregate_id is None:
            predecessor = SellerPublicationProjectionService._not_requested(job)
            eligible = (
                job.state is ControlJobState.APPROVED and job.approval_decision_id is not None
            )
            return _enable_projection(
                predecessor,
                request_enabled=eligible,
                request_disabled_reason=(None if eligible else "PUBLICATION_NOT_ELIGIBLE"),
            )

        try:
            authority = self._store.get_publication_authority(
                owner_id,
                job.publication_aggregate_id,
            )
            reparsed = PublicationProjectionAuthority.model_validate(
                authority.model_dump(mode="python")
            )
            if reparsed.job != job:
                raise ValueError
            predecessor = SellerPublicationProjectionService._project(reparsed)
            return _enable_projection(
                predecessor,
                request_enabled=False,
                request_disabled_reason="PUBLICATION_ALREADY_REQUESTED",
            )
        except NotFoundError:
            raise PublicationProjectionUnavailableError(
                "Publication status is temporarily unavailable"
            ) from None
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise PublicationProjectionUnavailableError(
                "Publication status is temporarily unavailable"
            ) from None

    def _load_job(self, *, owner_id: str, job_id: str) -> ControlJobRecord:
        try:
            job = self._store.get_job_for_owner(owner_id, job_id)
        except NotFoundError:
            raise NotFoundError from None
        except Exception:
            raise PublicationProjectionUnavailableError(
                "Publication status is temporarily unavailable"
            ) from None
        try:
            exact = ControlJobRecord.model_validate(job.model_dump(mode="python"))
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise PublicationProjectionUnavailableError(
                "Publication status is temporarily unavailable"
            ) from None
        if exact.owner_id != owner_id or exact.job_id != job_id:
            raise NotFoundError from None
        return exact


def _enable_projection(
    predecessor: SellerPublicationProjection,
    *,
    request_enabled: bool,
    request_disabled_reason: Phase718RequestDisabledReason | None,
) -> Phase718SellerPublicationProjection:
    payload = predecessor.model_dump(mode="python")
    payload.update(
        {
            "contract_version": "7.1.0",
            "publication_enabled": True,
            "request_enabled": request_enabled,
            "request_disabled_reason": request_disabled_reason,
            "etag": canonical_fingerprint(
                {
                    "kind": "phase718_seller_publication_projection",
                    "predecessor_etag": predecessor.etag,
                    "request_enabled": request_enabled,
                    "request_disabled_reason": request_disabled_reason,
                }
            ),
        }
    )
    return Phase718SellerPublicationProjection.model_validate(payload)


__all__ = [
    "Phase718RequestDisabledReason",
    "Phase718SellerPublicationProjection",
    "Phase718SellerPublicationProjectionService",
]
