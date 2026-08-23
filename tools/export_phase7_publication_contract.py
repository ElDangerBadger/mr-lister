"""Export or verify the frozen Phase 7 publication contract artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from mr_lister.publication.contract import phase7_publication_contract_bytes

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "contracts" / "publication" / "phase7.0.1.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = phase7_publication_contract_bytes()
    if arguments.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != expected:
            raise SystemExit("Phase 7 publication contract artifact is stale")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
