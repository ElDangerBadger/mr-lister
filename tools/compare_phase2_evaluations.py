"""Compare private, score-only Phase 2 evaluation artifacts without invoking a model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.phase2_evaluation import summarize_score_documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path(".mr_lister_private/evaluation-results"),
    )
    args = parser.parse_args()
    documents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(args.directory.glob("*/*.json"))
    ]
    print(json.dumps(summarize_score_documents(documents), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
