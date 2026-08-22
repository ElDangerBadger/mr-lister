"""Exact application-owned product authority for seller review."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from re import fullmatch

from pydantic import ValidationError

from mr_lister.contracts import ProductProfile
from mr_lister.control.fingerprints import canonical_fingerprint


@dataclass(frozen=True)
class ExactReviewProductProfile:
    profile: ProductProfile
    fingerprint: str

    @property
    def product_name(self) -> str:
        """Return a stable label derived only from fingerprinted profile authority."""

        return f"Product profile {self.profile.profile_id}"

    @property
    def provider_name(self) -> str:
        """Return a stable provider label derived only from fingerprinted profile authority."""

        return f"Print provider {self.profile.print_provider_id}"


class ReviewProfileNotFoundError(Exception):
    """Stable internal error translated to projection unavailability."""


class FilesystemReviewProductAuthority:
    """Load one exact profile whose fingerprint includes its seller-facing labels."""

    def __init__(self, *, profile_directory: Path) -> None:
        self._profile_directory = profile_directory

    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactReviewProductProfile:
        if fullmatch(r"[a-z0-9][a-z0-9_-]+", profile_id) is None:
            raise ReviewProfileNotFoundError("The review product profile was not found")
        profile_path = self._profile_directory / f"{profile_id}.json"
        try:
            profile = ProductProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise ReviewProfileNotFoundError("The review product profile was not found") from error
        fingerprint = canonical_fingerprint(profile)
        if profile.profile_id != profile_id or profile.profile_version != profile_version:
            raise ReviewProfileNotFoundError("The review product profile was not found")
        return ExactReviewProductProfile(
            profile=profile,
            fingerprint=fingerprint,
        )


__all__ = [
    "ExactReviewProductProfile",
    "FilesystemReviewProductAuthority",
    "ReviewProfileNotFoundError",
]
