"""Render the exact Phase 6 dispatcher stream-filter correction offline.

This renderer starts from the exact deployed post-review-query-hotfix template and changes only
the DynamoDB event-source filter pattern.  Historical template hashes remain historical; the
corrected public source template has its own independently sealed authority.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
LEGACY_SOURCE_TEMPLATE_SHA256: Final = (
    "d7ad630eb4817de5aa81a6e4a9dcec7d7347dc63b10d745a4251d4841b5a1d55"
)
CORRECTED_SOURCE_TEMPLATE_SHA256: Final = (
    "96439a80d9c65e658de68cb3e8ca6c9ca99ad85b69ef9cbef2b4bb9d1517430b"
)
PREDECESSOR_TEMPLATE_SHA256: Final = (
    "81ad610ad62fa4ab58017c107c980b9572c4306681264f9565555e77379325e8"
)
# Filled only after canonical rendering has been independently calculated.
DISPATCH_FILTER_CORRECTION_TEMPLATE_SHA256: Final = (
    "e5fb4ba29915fd8ec4261476987432c20b490f339019f4eb6a6972b4c24f86c3"
)

SOURCE_PATH: Final = REPOSITORY_ROOT / "infra/phase6/template.json"
DEFAULT_PREDECESSOR_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-review-query-code-hotfix/"
    "template.review-query-code-hotfix.local.json"
)
DEFAULT_OUTPUT_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-dispatch-filter-correction/"
    "template.dispatch-filter-correction.local.json"
)

OLD_FILTER: Final = {"dynamodb": {"Keys": {"SK": {"S": [{"prefix": "WORK#"}]}}}}
SAFE_FILTER: Final = {
    "eventName": ["INSERT", "MODIFY"],
    "dynamodb": {"Keys": {"SK": {"S": [{"prefix": "WORK#"}]}}},
}
_FILTER_PATH: Final = (
    "Resources",
    "DispatcherFunction",
    "Properties",
    "Events",
    "OperationalStateChanges",
    "Properties",
    "FilterCriteria",
    "Filters",
    "0",
    "Pattern",
)
_GENERIC_ERROR = "Phase 6 dispatcher stream-filter correction is invalid"


class Phase6DispatchFilterCorrectionError(RuntimeError):
    """A value-free correction rendering failure."""


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode()


def _reject_constant(_value: str) -> None:
    raise ValueError


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _document(raw: bytes, expected_sha256: str, *, canonical: bool) -> dict[str, Any]:
    value = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    if (
        not isinstance(value, dict)
        or sha256(raw).hexdigest() != expected_sha256
        or (canonical and _canonical(value) != raw)
    ):
        raise ValueError
    return value


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError
    return value


def _pattern(document: Mapping[str, Any]) -> str:
    value: object = document
    for component in _FILTER_PATH:
        value = value[int(component)] if isinstance(value, list) else _mapping(value).get(component)
    if not isinstance(value, str):
        raise ValueError
    return value


def _set_pattern(document: dict[str, Any], pattern: str) -> None:
    value: object = document
    for component in _FILTER_PATH[:-1]:
        value = value[int(component)] if isinstance(value, list) else _mapping(value)[component]
    _mapping(value)[_FILTER_PATH[-1]] = pattern


def _strict_pattern(value: str) -> object:
    parsed = json.loads(value, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    if json.dumps(parsed, allow_nan=False, separators=(",", ":"), sort_keys=False) != value:
        raise ValueError
    return parsed


def _changed_paths(
    before: object, after: object, path: tuple[str, ...] = ()
) -> set[tuple[str, ...]]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        result: set[tuple[str, ...]] = set()
        for key in set(before) | set(after):
            if key not in before or key not in after:
                result.add((*path, str(key)))
            else:
                result.update(_changed_paths(before[key], after[key], (*path, str(key))))
        return result
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        result = set()
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            result.update(_changed_paths(left, right, (*path, str(index))))
        return result
    return set() if before == after else {path}


def render_phase6_dispatch_filter_correction(predecessor_raw: bytes, source_raw: bytes) -> bytes:
    """Return the canonical one-property correction template."""

    try:
        predecessor = _document(predecessor_raw, PREDECESSOR_TEMPLATE_SHA256, canonical=True)
        source = _document(source_raw, CORRECTED_SOURCE_TEMPLATE_SHA256, canonical=False)
        if _strict_pattern(_pattern(predecessor)) != OLD_FILTER:
            raise ValueError
        if _strict_pattern(_pattern(source)) != SAFE_FILTER:
            raise ValueError
        target = deepcopy(predecessor)
        _set_pattern(target, json.dumps(SAFE_FILTER, separators=(",", ":")))
        if _changed_paths(predecessor, target) != {_FILTER_PATH}:
            raise ValueError
        rendered = _canonical(target)
        if sha256(rendered).hexdigest() != DISPATCH_FILTER_CORRECTION_TEMPLATE_SHA256:
            raise ValueError
        return rendered
    except Exception:
        raise Phase6DispatchFilterCorrectionError(_GENERIC_ERROR) from None


def _repository_path(path: Path) -> tuple[Path, tuple[str, ...]]:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(REPOSITORY_ROOT)
    except ValueError:
        raise ValueError from None
    if not relative.parts:
        raise ValueError
    return candidate, relative.parts


def _read_file(path: Path) -> bytes:
    candidate, parts = _repository_path(path)
    current = REPOSITORY_ROOT
    for index, part in enumerate(parts):
        current /= part
        metadata = current.lstat()
        if index < len(parts) - 1:
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError
        elif not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError
    return candidate.read_bytes()


def _private_output(path: Path) -> Path:
    candidate, parts = _repository_path(path)
    if parts[0] != ".mr_lister_private" or len(parts) < 2:
        raise ValueError
    current = REPOSITORY_ROOT
    for part in parts[:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError
        current.chmod(0o700)
    return candidate


def write_phase6_dispatch_filter_correction() -> Path:
    """Create the private output once, or accept identical existing bytes."""

    try:
        rendered = render_phase6_dispatch_filter_correction(
            _read_file(DEFAULT_PREDECESSOR_PATH), _read_file(SOURCE_PATH)
        )
        output = _private_output(DEFAULT_OUTPUT_PATH)
        if output.exists() or output.is_symlink():
            if _read_file(output) != rendered:
                raise ValueError
        else:
            temporary = output.with_name(f".{output.name}.{secrets.token_hex(12)}.tmp")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(rendered)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
        output.chmod(0o600)
        return output
    except Phase6DispatchFilterCorrectionError:
        raise
    except Exception:
        raise Phase6DispatchFilterCorrectionError(_GENERIC_ERROR) from None


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        output = write_phase6_dispatch_filter_correction()
        raw = _read_file(output)
    except Phase6DispatchFilterCorrectionError as error:
        print(str(error))
        return 2
    print(
        json.dumps({"result": "passed", "target_sha256": sha256(raw).hexdigest()}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
