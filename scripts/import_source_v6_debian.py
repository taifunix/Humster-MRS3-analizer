#!/usr/bin/env python3
"""Headless Source v6 HTML importer (one command, no web framework)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" if (ROOT / "src").exists() else ROOT))

from mrs3.source_v6 import SourceV6Error  # noqa: E402
from mrs3.config import load_duckdb_import_settings, load_source_v6_import_settings  # noqa: E402
from mrs3.source_v6_importer import SourceV6ImportError, SourceV6WorkerFailure, import_source_v6, preflight_source_v6  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Source v6 HTML reports into a fresh DuckDB")
    parser.add_argument("html", type=Path, help="HTML report or directory of reports")
    parser.add_argument("database", type=Path, help="fresh Source v6 DuckDB path")
    parser.add_argument("--surface-dir", type=Path, default=None, help="publish a stitched DuckDB surface after import")
    parser.add_argument("--export-dir", type=Path, default=None, help="export plateau_report.xlsx from the published surface")
    parser.add_argument("--handoff-manifest", type=Path, default=None, help="write machine-readable import/surface handoff manifest")
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.json", help="config.local.json with duckdb_import.workers")
    parser.add_argument("--progress", type=Path, default=None, help="write atomic import progress for an owning panel job")
    args = parser.parse_args()
    try:
        settings = load_duckdb_import_settings(args.config)
        source_v6_settings = load_source_v6_import_settings(args.config)
        config_document = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
        source_section = config_document.get("source_v6_import") if isinstance(config_document, dict) else None
        segment_writer_limit = source_v6_settings.segment_writer_limit
        if isinstance(source_section, dict) and "segment_writer_limit" in source_section:
            source_v6_settings.validate_for_workers(settings.workers)
        else:
            segment_writer_limit = min(segment_writer_limit, settings.workers)
        preflight = preflight_source_v6(args.html, args.database)
        started_at = int(time.time())

        def write_progress(current: int, total: int) -> None:
            if args.progress is None:
                return
            args.progress.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.progress.with_name(f".{args.progress.name}.tmp")
            temporary.write_text(f"{current} {total} {settings.workers} {started_at}\n", encoding="ascii")
            temporary.replace(args.progress)

        write_progress(0, len(preflight.snapshots))
        import_options = {
            "preflight": preflight,
            "workers": settings.workers,
            "batch_size": source_v6_settings.write_batch_size,
            "worker_chunk_size": source_v6_settings.worker_chunk_size,
            "max_in_flight_chunks": source_v6_settings.max_in_flight_chunks,
            "segment_writer_limit": segment_writer_limit,
            # Fact collections are only needed to publish a surface; coverage,
            # readiness and the receipts below read metadata alone.
            "hydrate_fragments": args.surface_dir is not None,
            "progress_callback": write_progress,
        }
        imported = import_source_v6(args.html, args.database, **import_options)
    except SourceV6WorkerFailure as error:
        snapshot = next((item for item in preflight.snapshots if item.input_ordinal == error.input_ordinal), None)
        failure = {
            "ordinal": error.input_ordinal,
            "path": str(snapshot.path) if snapshot is not None else None,
            "relative_path": error.relative_path,
            "preflight_size": snapshot.source_size if snapshot is not None else None,
            "preflight_mtime_ns": snapshot.source_mtime_ns if snapshot is not None else None,
            "reason": error.reason,
        }
        print(json.dumps({"status": "FAILED", "error": str(error), "worker_failure": failure}, ensure_ascii=False))
        return 1
    except (OSError, SourceV6Error, SourceV6ImportError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "FAILED", "error": str(error)}, ensure_ascii=False))
        return 1
    accepted_by_name = {fragment.source_name: fragment for fragment in imported.accepted_fragments}
    receipts = [
        {
            "file": snapshot.relative_path,
            "status": "COMMITTED" if snapshot.relative_path in accepted_by_name else "QUARANTINED",
            "safe_to_delete": imported.safe_to_delete if snapshot.relative_path in accepted_by_name else "NO",
        }
        for snapshot in preflight.snapshots
    ]
    result = {
        "status": imported.status,
        "reports": receipts,
        "source_content_digest": imported.source_content_digest,
        "accepted_count": imported.accepted_count,
        "quarantined": imported.quarantined_count,
        "quarantined_count": imported.quarantined_count,
        "safe_to_delete": imported.safe_to_delete,
        "quarantine_reasons": list(imported.quarantine_reasons),
        "workers": settings.workers,
        "write_batch_size": source_v6_settings.write_batch_size,
        "worker_chunk_size": source_v6_settings.worker_chunk_size,
        "max_in_flight_chunks": source_v6_settings.max_in_flight_chunks,
        "segment_writer_limit": segment_writer_limit,
    }
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
