"""Strict seller-presentation evidence shared across application boundaries."""

from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class PresentationEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductMockupEvidence(PresentationEvidenceModel):
    """Bounded Printify mockup evidence safe for later seller projection."""

    url: str = Field(min_length=1, max_length=2_048)
    position: (
        Annotated[
            str,
            StringConstraints(max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"),
        ]
        | None
    ) = None
    variant_ids: tuple[int, ...] = ()

    @model_validator(mode="after")
    def url_and_variant_scope_are_strict(self) -> ProductMockupEvidence:
        if (
            not self.url.startswith("https://")
            or not self.url.isascii()
            or "\\" in self.url
            or any(ord(character) < 32 or ord(character) == 127 for character in self.url)
            or re.search(r"%(?![0-9A-Fa-f]{2})", self.url)
        ):
            raise ValueError("Provider mockup URL is malformed")
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError as error:
            raise ValueError("Provider mockup URL is malformed") from error
        if (
            parsed.scheme != "https"
            or parsed.netloc != "images.printify.com"
            or parsed.hostname != "images.printify.com"
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith("/")
            or parsed.path == "/"
            or parsed.fragment
        ):
            raise ValueError("Provider mockup URL is malformed")
        if any(type(variant_id) is not int or variant_id <= 0 for variant_id in self.variant_ids):
            raise ValueError("Provider mockup variants must be positive integer IDs")
        if len(set(self.variant_ids)) != len(self.variant_ids):
            raise ValueError("Provider mockup variants must be unique")
        return self


__all__ = ["ProductMockupEvidence"]
