#!/usr/bin/env python3
"""Measure a bounded Source v6 import without making performance claims."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from time import perf_counter

from import_source_v6_debian import main as import_main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("html", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="source-v6-benchmark-") as directory:
        database = Path(directory) / "source-v6.duckdb"
        started = perf_counter()
        # Reuse the production CLI by replacing argv only inside this process.
        import sys
        previous = sys.argv
        try:
            sys.argv = ["import_source_v6_debian.py", str(args.html), str(database)]
            status = import_main()
        finally:
            sys.argv = previous
        elapsed = perf_counter() - started
        print(json.dumps({"status": status, "elapsed_seconds": elapsed, "database_bytes": database.stat().st_size if database.exists() else None}, separators=(",", ":")))
        return status


if __name__ == "__main__":
    raise SystemExit(main())
