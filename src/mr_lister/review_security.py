"""Small URL validators shared by the seller review read boundary."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

MOCKUP_IMAGE_HOST = "images.printify.com"
MAX_REVIEW_URL_LENGTH = 2_048
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SAFE_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def is_safe_mockup_url(value: str) -> bool:
    """Accept only the documented immutable mockup-image origin."""

    return _is_safe_https_url(value, exact_host=MOCKUP_IMAGE_HOST)


def is_safe_preview_url(value: str, *, exact_origin: str, job_id: str) -> bool:
    """Require the exact authenticated application route with no bearer grant or storage data."""

    if not isinstance(value, str) or not isinstance(exact_origin, str):
        return False
    if not isinstance(job_id, str):
        return False
    try:
        origin = urlsplit(exact_origin)
    except ValueError:
        return False
    if (
        not exact_origin.startswith("https://")
        or origin.scheme != "https"
        or origin.hostname is None
        or origin.netloc != origin.hostname
        or origin.path
        or origin.query
        or origin.fragment
    ):
        return False
    if _SAFE_JOB_ID.fullmatch(job_id) is None:
        return False
    if not _is_safe_https_url(value, exact_host=origin.hostname):
        return False
    try:
        preview = urlsplit(value)
    except ValueError:
        return False
    return preview.path == f"/v1/jobs/{job_id}/artwork-preview" and not preview.query


def _is_safe_https_url(value: str, *, exact_host: str) -> bool:
    if (
        not isinstance(value, str)
        or not isinstance(exact_host, str)
        or not value.startswith("https://")
        or len(value) > MAX_REVIEW_URL_LENGTH
        or not value.isascii()
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or _BAD_PERCENT_ESCAPE.search(value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return not (
        parsed.scheme != "https"
        or parsed.hostname != exact_host
        or parsed.netloc != exact_host
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path
        or parsed.path == "/"
    )


__all__ = [
    "MAX_REVIEW_URL_LENGTH",
    "MOCKUP_IMAGE_HOST",
    "is_safe_mockup_url",
    "is_safe_preview_url",
]
