#!/usr/bin/env python3
"""Headless Source v6 HTML importer (one command, no web framework)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" if (ROOT / "src").exists() else ROOT))

from mrs3.source_v6 import SourceV6Error  # noqa: E402
from mrs3.config import load_duckdb_import_settings  # noqa: E402
from mrs3.source_v6_importer import SourceV6ImportError, import_source_v6, preflight_source_v6  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Source v6 HTML reports into a fresh DuckDB")
    parser.add_argument("html", type=Path, help="HTML report or directory of reports")
    parser.add_argument("database", type=Path, help="fresh Source v6 DuckDB path")
    parser.add_argument("--surface-dir", type=Path, default=None, help="publish a stitched DuckDB surface after import")
    parser.add_argument("--export-dir", type=Path, default=None, help="export plateau_report.xlsx from the published surface")
    parser.add_argument("--handoff-manifest", type=Path, default=None, help="write machine-readable import/surface handoff manifest")
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.json", help="config.local.json with duckdb_import.workers")
    args = parser.parse_args()
    try:
        settings = load_duckdb_import_settings(args.config)
        preflight = preflight_source_v6(args.html, args.database)
        imported = import_source_v6(
            args.html,
            args.database,
            preflight=preflight,
            workers=settings.workers,
        )
    except (OSError, SourceV6Error, SourceV6ImportError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "FAILED", "error": str(error)}, ensure_ascii=False))
        return 1
    accepted_by_source = {fragment.source_sha256: fragment for fragment in imported.accepted_fragments}
    receipts = [
        {
            "file": snapshot.relative_path,
            "status": "COMMITTED" if snapshot.source_sha256 in accepted_by_source else "QUARANTINED",
            "safe_to_delete": imported.safe_to_delete if snapshot.source_sha256 in accepted_by_source else "NO",
        }
        for snapshot in preflight.snapshots
    ]
    result = {"status": imported.status, "reports": receipts, "source_content_digest": imported.source_content_digest, "quarantined": imported.quarantined_count, "workers": settings.workers}
    from mrs3.source_v6_coverage import canonical_ready_intervals, coverage_cells
    active = list(imported.active_fragments)
    result["coverage"] = {
        "cells": len(coverage_cells(tuple(active))),
        "canonical_ready_intervals": [
            {"scope": item.scope_key, "start": item.start.isoformat(), "end": item.end.isoformat()}
            for item in canonical_ready_intervals(tuple(active))
        ],
    }
    if args.surface_dir is not None:
        from mrs3.source_v6_surface import publish_surface_db
        if not active:
            raise SystemExit("no stitchable fragments available for surface")
        surface = publish_surface_db(args.surface_dir, tuple(active))
        result["surface"] = str(surface)
        if args.export_dir is not None:
            from mrs3.source_v6_analysis import export_plateau_report
            result["plateau_report"] = export_plateau_report(surface, args.export_dir)
    if args.handoff_manifest is not None:
        args.handoff_manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(dir=args.handoff_manifest.parent, prefix=f".{args.handoff_manifest.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        temporary.replace(args.handoff_manifest)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
