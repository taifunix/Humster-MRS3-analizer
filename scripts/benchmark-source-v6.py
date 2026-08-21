#!/usr/bin/env python3
"""Measure bounded Source v6 import throughput and semantic evidence."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter, process_time
from typing import Iterable

try:
    import resource
except ImportError:  # Windows
    resource = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" if (ROOT / "src").exists() else ROOT))

import duckdb

from mrs3.source_v6_importer import import_source_v6, preflight_source_v6
from mrs3.source_v6_storage import database_info


def _json_value(value: object) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _rows(connection: duckdb.DuckDBPyConnection, query: str) -> list[list[object]]:
    return [[_json_value(value) for value in row] for row in connection.execute(query).fetchall()]


def _semantic_document(path: str | Path) -> dict[str, object]:
    connection = duckdb.connect(str(Path(path).resolve()), read_only=True)
    try:
        tables = {
            "schema_info": _rows(connection, "select key, value from schema_info where key in ('schema_version', 'fingerprint', 'source_content_digest') order by key"),
            "points": _rows(connection, "select point_key from points order by point_key"),
            "compact_fragments": _rows(connection, "select fragment_id, source_sha256, source_name, point_key, report_start_ms, report_end_ms, stitchability, header_json, header_sha256, payload_blob, codec, payload_sha256, action_count, cycle_count, event_count, wallet_sample_count, equity_sample_count, active, inactive_reason, winner_fragment_id from compact_fragments order by fragment_id"),
            "fragment_origins": _rows(connection, "select fragment_id, source_sha256, source_name from fragment_origins order by fragment_id, source_sha256, source_name"),
            "day_ownership": _rows(connection, "select fragment_id, utc_day, ownership, active, reason, winner_fragment_id from day_ownership order by fragment_id, utc_day"),
            "day_dispositions": _rows(connection, "select fragment_id, utc_day, disposition, note from day_dispositions order by fragment_id, utc_day"),
            "fact_ownership": _rows(connection, "select fact_kind, fact_id, fragment_id, owner_fragment_id, active, reason, winner_fragment_id from fact_ownership order by fact_kind, fact_id, fragment_id"),
            "fragment_resolutions": _rows(connection, "select outgoing_fragment_id, incoming_fragment_id, status, reason, boundary_ms, evidence_json from fragment_resolutions order by outgoing_fragment_id, incoming_fragment_id"),
            "quarantine": _rows(connection, "select fragment_id, source_sha256, reason from quarantine order by fragment_id, source_sha256, reason"),
            "import_audit": _rows(connection, "select fragment_id, source_sha256, status, generation_before, generation_after, safe_to_delete, quarantine_count, error from import_audit order by fragment_id, source_sha256, status, error"),
        }
        return {"schema": "source-v6-semantic-evidence-v1", "tables": tables}
    finally:
        connection.close()


def source_v6_semantic_signature(path: str | Path) -> str:
    """Return a stable digest of logical Source v6 facts, excluding volatile IDs/times."""
    document = _semantic_document(path)
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(payload).hexdigest()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _cpu_clock() -> float:
    """Measure this run's CPU; Windows reports orchestrator-only CPU."""
    if resource is None:
        return process_time()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime + children.ru_utime + children.ru_stime


def _run_once(args: argparse.Namespace, html: Path, expected_signature: str | None) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="source-v6-benchmark-") as directory:
        database = Path(directory) / "source-v6.duckdb"
        started_wall = perf_counter()
        started_cpu = _cpu_clock()
        preflight = preflight_source_v6(html, database)
        result = import_source_v6(
            html,
            database,
            preflight=preflight,
            workers=args.workers,
            batch_size=args.write_batch_size,
            worker_chunk_size=args.worker_chunk_size,
            max_in_flight_chunks=args.max_in_flight_chunks,
            segment_writer_limit=args.segment_writer_limit,
        )
        elapsed = perf_counter() - started_wall
        cpu = _cpu_clock() - started_cpu
        signature = source_v6_semantic_signature(database)
        info = database_info(database)
        return {
            "elapsed_seconds": elapsed,
            "cpu_seconds": cpu,
            "database_bytes": database.stat().st_size,
            "report_count": len(preflight.snapshots),
            "raw_input_bytes": sum(snapshot.source_size for snapshot in preflight.snapshots),
            "accepted_count": result.accepted_count,
            "quarantined_count": result.quarantined_count,
            "source_content_digest": info["source_content_digest"],
            "semantic_signature": signature,
            "semantic_match": expected_signature is None or signature == expected_signature,
        }


def _median(runs: Iterable[dict[str, object]], key: str) -> float:
    return float(statistics.median(float(run[key]) for run in runs))


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark a bounded Source v6 import without publishing artifacts")
    parser.add_argument("html", type=Path)
    parser.add_argument("--compare-database", type=Path, default=None, help="compare semantic evidence with an existing Source v6 database")
    parser.add_argument("--workers", type=_positive_int, default=30)
    parser.add_argument("--write-batch-size", type=_positive_int, default=32)
    parser.add_argument("--worker-chunk-size", type=_positive_int, default=64)
    parser.add_argument("--max-in-flight-chunks", type=_positive_int, default=60)
    parser.add_argument("--segment-writer-limit", type=_positive_int, default=None)
    parser.add_argument("--repeat", type=_positive_int, default=1)
    args = parser.parse_args()
    try:
        expected_signature = source_v6_semantic_signature(args.compare_database) if args.compare_database is not None else None
        runs = [_run_once(args, args.html, expected_signature) for _ in range(args.repeat)]
    except (OSError, RuntimeError, ValueError, duckdb.Error) as error:
        print(json.dumps({"status": "FAILED", "error": str(error)}, ensure_ascii=False, separators=(",", ":")))
        return 1
    latest = runs[-1]
    payload = {
        "status": "COMMITTED" if all(bool(run["semantic_match"]) for run in runs) else "SEMANTIC_MISMATCH",
        "runs": len(runs),
        "elapsed_seconds": _median(runs, "elapsed_seconds"),
        "cpu_seconds": _median(runs, "cpu_seconds"),
        "cpu_scope": "process+children" if resource is not None else "orchestrator-only-windows",
        "database_bytes": latest["database_bytes"],
        "report_count": latest["report_count"],
        "raw_input_bytes": latest["raw_input_bytes"],
        "accepted_count": latest["accepted_count"],
        "quarantined_count": latest["quarantined_count"],
        "source_content_digest": latest["source_content_digest"],
        "semantic_signature": latest["semantic_signature"],
        "semantic_match": all(bool(run["semantic_match"]) for run in runs),
        "settings": {
            "workers": args.workers,
            "write_batch_size": args.write_batch_size,
            "worker_chunk_size": args.worker_chunk_size,
            "max_in_flight_chunks": args.max_in_flight_chunks,
            "segment_writer_limit": args.segment_writer_limit,
        },
        "runs_detail": runs,
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0 if payload["status"] == "COMMITTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
