"""Product-profile loading behind an application-owned contract."""

from pathlib import Path
from re import fullmatch

from pydantic import ValidationError

from mr_lister.contracts import ProductProfile
from mr_lister.workflow.errors import ProfileNotFoundError


class ProductProfileRepository:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def get(self, profile_id: str) -> ProductProfile:
        if fullmatch(r"[a-z0-9][a-z0-9_-]+", profile_id) is None:
            raise ProfileNotFoundError(f"Unknown product profile: {profile_id}")
        path = self._directory / f"{profile_id}.json"
        if not path.is_file():
            raise ProfileNotFoundError(f"Unknown product profile: {profile_id}")
        try:
            return ProductProfile.model_validate_json(path.read_text())
        except ValidationError as error:
            raise ProfileNotFoundError(f"Invalid product profile: {profile_id}") from error
