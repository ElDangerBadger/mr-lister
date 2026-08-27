"""Prepare and verify private Phase 6.6 live-acceptance evidence.

This intentionally small local-only tool performs two closed jobs:

* create the exact valid 5 MiB PNG required by the primary canary; and
* run the authoritative evidence-set verifier over one completed private bundle.

It does not import boto3, contact AWS or Printify, read browser credentials, stage arbitrary
files, sanitize raw observations, or grant provider authority. Evidence producers must write one
complete, already-sanitized bundle under the repository's Git-ignored private workspace before
verification.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import struct
import zlib
from collections.abc import Sequence
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, Final

from PIL import Image

from mr_lister.acceptance.evidence_set import (
    EvidenceSetVerificationError,
    verify_phase66_evidence_set,
)
from mr_lister.control.models import PHASE6_MAX_SOURCE_ARTWORK_BYTES
from mr_lister.control.source_artwork import verify_phase6_source_artwork

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_WORKSPACE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"
CANARY_FILENAME: Final = "phase66-primary-canary.png"
RECORDS_FILENAME: Final = "records.json"
ARTIFACT_INDEX_FILENAME: Final = "artifact-files.json"

_CANARY_WIDTH = 512
_PADDING_CHUNK_TYPE = b"mrLT"
_MAX_CONTROL_FILE_BYTES = 100 * 1024 * 1024


class Phase66LiveAcceptanceError(RuntimeError):
    """A local acceptance-bundle operation failed closed."""


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (OverflowError, TypeError, ValueError):
        raise Phase66LiveAcceptanceError("Acceptance data must be strict JSON") from None


def _private_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        candidate.relative_to(PRIVATE_WORKSPACE_ROOT)
    except ValueError:
        raise Phase66LiveAcceptanceError(
            "Acceptance files must stay inside the repository private workspace"
        ) from None
    return candidate


def _ensure_private_directory(directory: Path) -> None:
    directory = _private_path(directory)
    current = REPOSITORY_ROOT
    relative = directory.relative_to(REPOSITORY_ROOT)
    for component in relative.parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError:
                raise Phase66LiveAcceptanceError(
                    "Private acceptance directory could not be created"
                ) from None
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise Phase66LiveAcceptanceError(
                "Private acceptance path contains a non-directory component"
            )
        try:
            current.chmod(0o700)
        except OSError:
            raise Phase66LiveAcceptanceError(
                "Private acceptance directory permissions could not be secured"
            ) from None


def _validate_private_directory(directory: Path) -> None:
    directory = _private_path(directory)
    current = REPOSITORY_ROOT
    relative = directory.relative_to(REPOSITORY_ROOT)
    for component in relative.parts:
        current /= component
        try:
            metadata = current.lstat()
        except OSError:
            raise Phase66LiveAcceptanceError(
                "Private acceptance directory is unavailable"
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077 != 0
        ):
            raise Phase66LiveAcceptanceError("Private acceptance directory is not confined")


def _write_atomic(path: Path, contents: bytes) -> None:
    path = _private_path(path)
    _ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    except OSError:
        raise Phase66LiveAcceptanceError("Private acceptance file could not be written") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_regular_file(path: Path) -> bytes:
    path = _private_path(path)
    _validate_private_directory(path.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o077 != 0
            or before.st_size < 1
            or before.st_size > _MAX_CONTROL_FILE_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        raise Phase66LiveAcceptanceError(
            "Acceptance input must be one stable private regular file"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise Phase66LiveAcceptanceError("Acceptance input changed while it was being read")
    return b"".join(chunks)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = nested
    return value


def _load_json(path: Path) -> object:
    payload = _read_regular_file(path)
    try:
        return json.loads(
            payload,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise Phase66LiveAcceptanceError("Acceptance input must be strict JSON") from None


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum)
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def exact_phase66_canary_png() -> bytes:
    """Return a fully decodable, mixed-alpha, square PNG of exactly 5 MiB."""

    image = Image.new("RGBA", (_CANARY_WIDTH, _CANARY_WIDTH), (20, 40, 92, 0))
    pixels = image.load()
    for y in range(_CANARY_WIDTH):
        for x in range(_CANARY_WIDTH):
            distance = (x - _CANARY_WIDTH // 2) ** 2 + (y - _CANARY_WIDTH // 2) ** 2
            if distance < 180**2:
                alpha = 255 if (x + y) % 11 else 196
                pixels[x, y] = (24 + x % 160, 80 + y % 120, 184, alpha)

    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    base = output.getvalue()
    if not base.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"):
        raise Phase66LiveAcceptanceError("Canary PNG encoder produced an unexpected envelope")
    iend = base[-12:]
    before_iend = base[:-12]
    padding_size = PHASE6_MAX_SOURCE_ARTWORK_BYTES - len(base) - 12
    if padding_size < 0:
        raise Phase66LiveAcceptanceError("Canary PNG base image exceeds the Phase 6 limit")
    padded = before_iend + _png_chunk(_PADDING_CHUNK_TYPE, b"\0" * padding_size) + iend
    if len(padded) != PHASE6_MAX_SOURCE_ARTWORK_BYTES:
        raise Phase66LiveAcceptanceError("Canary PNG did not reach its exact byte authority")
    verified = verify_phase6_source_artwork(
        filename=CANARY_FILENAME,
        content_type="image/png",
        content=padded,
        expected_sha256=sha256(padded).hexdigest(),
        expected_size_bytes=PHASE6_MAX_SOURCE_ARTWORK_BYTES,
    )
    if (
        verified.width != _CANARY_WIDTH
        or verified.height != _CANARY_WIDTH
        or verified.alpha_minimum != 0
        or verified.alpha_maximum != 255
    ):
        raise Phase66LiveAcceptanceError("Canary PNG failed the closed artwork authority")
    return padded


def write_exact_canary_png(run_root: Path) -> dict[str, object]:
    run_root = _private_path(run_root)
    contents = exact_phase66_canary_png()
    output = run_root / CANARY_FILENAME
    if output.exists():
        if _read_regular_file(output) != contents:
            raise Phase66LiveAcceptanceError("Canary output already exists with different bytes")
    else:
        _write_atomic(output, contents)
    return {
        "result": "passed",
        "artifact_digest": sha256(contents).hexdigest(),
        "byte_count": len(contents),
        "width": _CANARY_WIDTH,
        "height": _CANARY_WIDTH,
        "alpha_minimum": 0,
        "alpha_maximum": 255,
    }


def verify_bundle(bundle_root: Path) -> dict[str, object]:
    bundle_root = _private_path(bundle_root)
    try:
        records = _load_json(bundle_root / RECORDS_FILENAME)
        artifact_files = _load_json(bundle_root / ARTIFACT_INDEX_FILENAME)
        if not isinstance(records, list) or not isinstance(artifact_files, list):
            raise Phase66LiveAcceptanceError("Evidence bundle indexes must be JSON arrays")
        verified = verify_phase66_evidence_set(
            records,
            artifact_files,
            allowed_artifact_root=bundle_root,
        )
    except EvidenceSetVerificationError:
        raise Phase66LiveAcceptanceError(
            "Phase 6.6 evidence bundle is incomplete or invalid"
        ) from None
    return {"result": "passed", **verified.model_dump(mode="json")}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    canary = subparsers.add_parser("make-canary-png")
    canary.add_argument("run_root", type=Path)

    verify = subparsers.add_parser("verify")
    verify.add_argument("bundle_root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "make-canary-png":
            result = write_exact_canary_png(arguments.run_root)
        else:
            result = verify_bundle(arguments.bundle_root)
    except Phase66LiveAcceptanceError as error:
        parser.error(str(error))
    print(_canonical_json(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
