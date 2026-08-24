"""Capability-free Phase 7 eligibility for an exact draft-safe product profile.

Phase 6 deliberately requires ``ProductProfile.publish_enabled`` to remain false while it
constructs an unpublished Printify draft.  Phase 7 eligibility is a separate, release-bound
statement about that exact profile; it is not seller-route or provider-mutation activation.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, StrictInt, ValidationError, field_validator, model_validator

from mr_lister.publication.models import Fingerprint, PublicationModel, SafeId


class PublicationProfileEligibilityError(LookupError):
    """The exact release-bound profile eligibility was absent or inconsistent."""


class PublicationProfileEligibility(PublicationModel):
    """Static eligibility whose explicit false flags grant no runtime capability."""

    profile_id: SafeId
    profile_version: StrictInt = Field(ge=1)
    profile_fingerprint: Fingerprint
    expected_sales_channel: Literal["etsy"] = "etsy"
    release_manifest_fingerprint: Fingerprint
    phase6_profile_publish_enabled: Literal[False] = False
    publication_eligible: Literal[True] = True
    seller_request_enabled: Literal[False] = False
    provider_mutation_enabled: Literal[False] = False

    @field_validator(
        "phase6_profile_publish_enabled",
        "seller_request_enabled",
        "provider_mutation_enabled",
        mode="before",
    )
    @classmethod
    def disabled_flags_are_exact_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Publication profile eligibility flags must be exact")
        return value

    @field_validator("publication_eligible", mode="before")
    @classmethod
    def eligibility_flag_is_exact_true(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Publication profile eligibility flag must be exact")
        return value

    @model_validator(mode="after")
    def release_binding_is_real(self) -> PublicationProfileEligibility:
        if self.release_manifest_fingerprint == "0" * 64:
            raise ValueError("Publication profile eligibility requires a real release binding")
        return self


class PublicationProfileEligibilityAuthority(Protocol):
    """Resolve only one exact profile/release/channel eligibility statement."""

    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
        profile_fingerprint: str,
        expected_sales_channel: str,
        release_manifest_fingerprint: str,
        phase6_profile_publish_enabled: bool,
    ) -> PublicationProfileEligibility: ...


def build_publication_profile_eligibility(
    *,
    profile_id: str,
    profile_version: int,
    profile_fingerprint: str,
    release_manifest_fingerprint: str,
    phase6_profile_publish_enabled: bool,
    expected_sales_channel: str = "etsy",
) -> PublicationProfileEligibility:
    """Build exact static eligibility while proving the source profile stayed draft-safe."""

    try:
        return PublicationProfileEligibility(
            profile_id=profile_id,
            profile_version=profile_version,
            profile_fingerprint=profile_fingerprint,
            expected_sales_channel=expected_sales_channel,
            release_manifest_fingerprint=release_manifest_fingerprint,
            phase6_profile_publish_enabled=phase6_profile_publish_enabled,
        )
    except (TypeError, ValidationError, ValueError):
        raise PublicationProfileEligibilityError(
            "Publication profile eligibility is unavailable"
        ) from None


def require_exact_publication_profile_eligibility(
    value: object,
    *,
    profile_id: str,
    profile_version: int,
    profile_fingerprint: str,
    release_manifest_fingerprint: str,
    phase6_profile_publish_enabled: bool,
    expected_sales_channel: str = "etsy",
) -> PublicationProfileEligibility:
    """Reparse and compare every constituent; eligibility never enables a route or mutation."""

    expected = build_publication_profile_eligibility(
        profile_id=profile_id,
        profile_version=profile_version,
        profile_fingerprint=profile_fingerprint,
        expected_sales_channel=expected_sales_channel,
        release_manifest_fingerprint=release_manifest_fingerprint,
        phase6_profile_publish_enabled=phase6_profile_publish_enabled,
    )
    try:
        exact = PublicationProfileEligibility.model_validate(value)
    except (TypeError, ValidationError, ValueError):
        raise PublicationProfileEligibilityError(
            "Publication profile eligibility is unavailable"
        ) from None
    if exact != expected:
        raise PublicationProfileEligibilityError("Publication profile eligibility is unavailable")
    return exact


class PinnedPublicationProfileEligibilityAuthority:
    """Expose one immutable eligibility record and no filesystem-wide policy authority."""

    __slots__ = ("_eligibility",)

    def __init__(self, eligibility: PublicationProfileEligibility) -> None:
        self._eligibility = require_exact_publication_profile_eligibility(
            eligibility.model_dump(mode="python"),
            profile_id=eligibility.profile_id,
            profile_version=eligibility.profile_version,
            profile_fingerprint=eligibility.profile_fingerprint,
            expected_sales_channel=eligibility.expected_sales_channel,
            release_manifest_fingerprint=eligibility.release_manifest_fingerprint,
            phase6_profile_publish_enabled=eligibility.phase6_profile_publish_enabled,
        )

    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
        profile_fingerprint: str,
        expected_sales_channel: str,
        release_manifest_fingerprint: str,
        phase6_profile_publish_enabled: bool,
    ) -> PublicationProfileEligibility:
        return require_exact_publication_profile_eligibility(
            self._eligibility.model_dump(mode="python"),
            profile_id=profile_id,
            profile_version=profile_version,
            profile_fingerprint=profile_fingerprint,
            expected_sales_channel=expected_sales_channel,
            release_manifest_fingerprint=release_manifest_fingerprint,
            phase6_profile_publish_enabled=phase6_profile_publish_enabled,
        )


__all__ = [
    "PinnedPublicationProfileEligibilityAuthority",
    "PublicationProfileEligibility",
    "PublicationProfileEligibilityAuthority",
    "PublicationProfileEligibilityError",
    "build_publication_profile_eligibility",
    "require_exact_publication_profile_eligibility",
]
