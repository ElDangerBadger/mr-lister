"""Export or verify the committed Phase 6.6 acceptance contract artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mr_lister.acceptance.phase6 import (
    evidence_record_json_schema,
    phase66_acceptance_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIRECTORY = ROOT / "contracts" / "acceptance"
MANIFEST_FILENAME = "phase6.6.manifest.json"
SCHEMA_FILENAME = "phase6.6.evidence.schema.json"


def render_json(value: Mapping[str, Any]) -> str:
    """Return the canonical checked-in representation."""

    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )


def expected_artifacts() -> dict[str, str]:
    return {
        MANIFEST_FILENAME: render_json(phase66_acceptance_manifest().model_dump(mode="json")),
        SCHEMA_FILENAME: render_json(evidence_record_json_schema()),
    }


def export_artifacts(output_directory: Path) -> tuple[Path, ...]:
    """Write both versioned artifacts atomically in stable filename order."""

    output_directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, content in sorted(expected_artifacts().items()):
        path = output_directory / filename
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        written.append(path)
    return tuple(written)


def drifted_artifacts(output_directory: Path) -> tuple[Path, ...]:
    """Return missing or byte-different artifact paths in stable order."""

    drifted: list[Path] = []
    for filename, expected in sorted(expected_artifacts().items()):
        path = output_directory / filename
        try:
            actual = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            drifted.append(path)
            continue
        if actual != expected:
            drifted.append(path)
    return tuple(drifted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help="Artifact directory (defaults to contracts/acceptance).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if committed artifacts differ; do not write files.",
    )
    arguments = parser.parse_args(argv)

    if arguments.check:
        drifted = drifted_artifacts(arguments.output_directory)
        if drifted:
            for path in drifted:
                print(f"Phase 6.6 acceptance contract drift: {path}", file=sys.stderr)
            return 1
        return 0

    for path in export_artifacts(arguments.output_directory):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
