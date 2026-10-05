"""Verify an existing Performance v2 database and publish a C-volume hardlink alias."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mrs3.config import load_duckdb_import_settings  # noqa: E402
from mrs3.performance_v2_compact import verify_existing_candidate  # noqa: E402


def _positive_int(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be a positive integer") from exc
    if workers < 1:
        raise argparse.ArgumentTypeError("workers must be a positive integer")
    return workers


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="C-volume hardlink alias; full verification only")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spill-parent", required=True, type=Path)
    parser.add_argument("--workers", type=_positive_int, help="DuckDB workers; config value is the default")
    parser.add_argument(
        "--smoke-ranges", type=int,
        help="compare only the first N actions/equity result-id ranges; never publishes",
    )
    args = parser.parse_args(argv)
    if args.smoke_ranges is None and args.output is None:
        parser.error("--output is required unless --smoke-ranges is used")
    if args.smoke_ranges is not None and args.output is not None:
        parser.error("--output cannot be used with --smoke-ranges")
    workers = args.workers if args.workers is not None else load_duckdb_import_settings(args.config).workers

    def progress(event: dict[str, object]) -> None:
        print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)

    try:
        report = verify_existing_candidate(
            args.source, args.candidate, args.output,
            workers=workers, spill_parent=args.spill_parent,
            progress_callback=progress, smoke_ranges=args.smoke_ranges,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr, flush=True)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
