"""Build a verified Performance v2 candidate from a read-only source."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mrs3.config import load_duckdb_import_settings  # noqa: E402
from mrs3.performance_v2_compact import compact_performance_v2  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    workers = load_duckdb_import_settings(args.config).workers

    def progress(event: dict[str, object]) -> None:
        print(json.dumps(event, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)

    report = compact_performance_v2(
        args.source, args.output, workers=workers, progress_callback=progress,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
