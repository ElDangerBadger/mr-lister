"""Render the exact post-web-edge Phase 6 review-query runtime correction.

The additive web-edge template intentionally preserved every active core resource.  Its
review-query Lambda therefore retained the earlier implicit 256 MB / 15 second runtime envelope.
This local-only renderer applies the separately reviewed, no-replacement correction and proves
that no other template path changes.  It never contacts AWS or grants deployment authority.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

from tools.render_phase6_web_edge_transition import (
    SOURCE_TEMPLATE_SHA256,
    WEB_EDGE_TEMPLATE_SHA256,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PREDECESSOR_TEMPLATE_SHA256: Final = WEB_EDGE_TEMPLATE_SHA256
REVIEW_QUERY_RUNTIME_ENVELOPE_TEMPLATE_SHA256: Final = (
    "618fbca8d00b1edbfa7412668a6e7d2a0e4e65e23460ee8b9216f92f19dbdfc2"
)
REVIEW_QUERY_RUNTIME_ENVELOPE_FORMAT: Final = (
    "mr-lister-phase6-review-query-runtime-envelope-correction-v1"
)
DEFAULT_PREDECESSOR_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-web-edge-transition/template.web-edge-active-draft-only.local.json"
)
DEFAULT_SOURCE_PATH: Final = REPOSITORY_ROOT / "infra/phase6/template.json"
DEFAULT_OUTPUT_PATH: Final = REPOSITORY_ROOT / (
    ".mr_lister_private/phase6-runtime-envelope-correction/"
    "template.review-query-runtime-envelope.local.json"
)

_GENERIC_ERROR = "Phase 6 review-query runtime correction is invalid"
_METADATA_KEY = "MrListerPhase6ReviewQueryRuntimeEnvelopeCorrection"


class Phase6ReviewQueryRuntimeEnvelopeError(RuntimeError):
    """A value-free runtime-correction rendering failure."""


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            indent=2,
            separators=(",", ": "),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError
        value[key] = nested
    return value


def _document(raw: bytes, expected_sha256: str, *, canonical: bool) -> dict[str, Any]:
    value = json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_object,
    )
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


def _repository_path(path: Path) -> tuple[Path, tuple[str, ...]]:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(REPOSITORY_ROOT)
    except ValueError:
        raise ValueError from None
    if not relative.parts:
        raise ValueError
    return candidate, relative.parts


def _read_repository_file(path: Path) -> bytes:
    candidate, components = _repository_path(path)
    current = REPOSITORY_ROOT
    repository_metadata = current.lstat()
    if not stat.S_ISDIR(repository_metadata.st_mode) or stat.S_ISLNK(repository_metadata.st_mode):
        raise ValueError
    for index, component in enumerate(components):
        current /= component
        metadata = current.lstat()
        if index < len(components) - 1:
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError
        elif (
            current != candidate
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise ValueError
    return candidate.read_bytes()


def _prepare_private_output(path: Path) -> Path:
    candidate, components = _repository_path(path)
    if components[0] != ".mr_lister_private" or len(components) < 2:
        raise ValueError
    current = REPOSITORY_ROOT
    repository_metadata = current.lstat()
    if not stat.S_ISDIR(repository_metadata.st_mode) or stat.S_ISLNK(repository_metadata.st_mode):
        raise ValueError
    for component in components[:-1]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError
        current.chmod(0o700)
    return candidate


def _changed_paths(
    before: object,
    after: object,
    prefix: tuple[str, ...] = (),
) -> set[tuple[str, ...]]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths: set[tuple[str, ...]] = set()
        for key in set(before) | set(after):
            if key not in before or key not in after:
                paths.add((*prefix, str(key)))
            else:
                paths.update(_changed_paths(before[key], after[key], (*prefix, str(key))))
        return paths
    return set() if before == after else {prefix}


def render_phase6_review_query_runtime_envelope(
    predecessor_raw: bytes,
    source_raw: bytes,
) -> bytes:
    """Return the canonical exact correction from its two sealed template authorities."""

    try:
        predecessor = _document(
            predecessor_raw,
            PREDECESSOR_TEMPLATE_SHA256,
            canonical=True,
        )
        source = _document(source_raw, SOURCE_TEMPLATE_SHA256, canonical=False)
        source_resources = _mapping(source.get("Resources"))
        source_function = _mapping(source_resources.get("ReviewQueryApiFunction"))
        source_properties = _mapping(source_function.get("Properties"))
        if source_properties.get("MemorySize") != 512 or source_properties.get("Timeout") != 30:
            raise ValueError

        predecessor_globals = _mapping(predecessor.get("Globals"))
        predecessor_function_defaults = _mapping(predecessor_globals.get("Function"))
        if predecessor_function_defaults.get("MemorySize") != 256:
            raise ValueError

        target = deepcopy(predecessor)
        resources = _mapping(target.get("Resources"))
        function = _mapping(resources.get("ReviewQueryApiFunction"))
        properties = _mapping(function.get("Properties"))
        metadata = _mapping(target.get("Metadata"))
        if (
            "MemorySize" in properties
            or properties.get("Timeout") != 15
            or _METADATA_KEY in metadata
        ):
            raise ValueError

        properties["MemorySize"] = 512
        properties["Timeout"] = 30
        metadata[_METADATA_KEY] = {
            "Changes": {
                "MemorySize": {"From": 256, "To": 512},
                "Timeout": {"From": 15, "To": 30},
            },
            "Format": REVIEW_QUERY_RUNTIME_ENVELOPE_FORMAT,
            "PredecessorTemplateSha256": PREDECESSOR_TEMPLATE_SHA256,
            "Resource": "ReviewQueryApiFunction",
            "SourceTemplateSha256": SOURCE_TEMPLATE_SHA256,
        }

        expected_paths = {
            ("Metadata", _METADATA_KEY),
            ("Resources", "ReviewQueryApiFunction", "Properties", "MemorySize"),
            ("Resources", "ReviewQueryApiFunction", "Properties", "Timeout"),
        }
        if _changed_paths(predecessor, target) != expected_paths:
            raise ValueError
        rendered = _canonical(target)
        if sha256(rendered).hexdigest() != REVIEW_QUERY_RUNTIME_ENVELOPE_TEMPLATE_SHA256:
            raise ValueError
        return rendered
    except Phase6ReviewQueryRuntimeEnvelopeError:
        raise
    except Exception:
        raise Phase6ReviewQueryRuntimeEnvelopeError(_GENERIC_ERROR) from None


def write_phase6_review_query_runtime_envelope() -> Path:
    """Write or verify the fixed private correction target."""

    try:
        rendered = render_phase6_review_query_runtime_envelope(
            _read_repository_file(DEFAULT_PREDECESSOR_PATH),
            _read_repository_file(DEFAULT_SOURCE_PATH),
        )
        output_path = _prepare_private_output(DEFAULT_OUTPUT_PATH)
        if output_path.exists() or output_path.is_symlink():
            if _read_repository_file(output_path) != rendered:
                raise ValueError
        else:
            temporary = output_path.with_name(f".{output_path.name}.{secrets.token_hex(12)}.tmp")
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    descriptor = None
                    output.write(rendered)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(output_path)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        output_path.chmod(0o600)
        return output_path
    except Phase6ReviewQueryRuntimeEnvelopeError:
        raise
    except Exception:
        raise Phase6ReviewQueryRuntimeEnvelopeError(_GENERIC_ERROR) from None


def main() -> int:
    try:
        output = write_phase6_review_query_runtime_envelope()
        rendered = _read_repository_file(output)
    except Phase6ReviewQueryRuntimeEnvelopeError as error:
        print(str(error))
        return 2
    print(
        json.dumps(
            {
                "result": "passed",
                "target_byte_count": len(rendered),
                "target_sha256": sha256(rendered).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
