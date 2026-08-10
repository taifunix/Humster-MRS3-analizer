#!/usr/bin/env python3
"""MRS3 Compact HTML Importer v4.

Uses a bounded multi-process pipeline: worker processes parse and compress HTML
reports while this main process is the sole DuckDB writer.  Every report remains
one compressed payload row; source HTML files are never modified or deleted.

Dependencies: py -m pip install duckdb lxml
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Iterable

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - handled by the BAT launcher
    raise SystemExit("Missing dependency: run 'py -m pip install duckdb lxml'") from exc


SCHEMA_VERSION = "4"
IMPORTER_NAME = "MRS3 Parallel Compact HTML Importer v4"
BASE_IMPORTER_PATH = Path(__file__).with_name("mrs3_html_compact_importer_v3.py")


def _load_base_importer() -> Any:
    spec = importlib.util.spec_from_file_location("_mrs3_v3_base", BASE_IMPORTER_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load required v3 codec: {BASE_IMPORTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # v4 uses the same lossless payload codec, but its own isolated schema marker.
    module.SCHEMA_VERSION = SCHEMA_VERSION
    return module


_base = _load_base_importer()
ParsedReport = _base.ParsedReport


@dataclass(frozen=True, slots=True)
class ImportResult:
    scanned_reports: int
    parsed_reports: int
    imported_reports: int
    skipped_reports: int
    quarantined_reports: int
    raw_trade_action_count: int
    equity_sample_count: int


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    source_file: str
    source_hash: str
    report: Any | None
    error: str | None


def _parse_worker(source_file: str) -> ParseOutcome:
    """Process-pool target. It has no database access by design."""
    path = Path(source_file)
    try:
        report = _base._parse_report(path)
        return ParseOutcome(source_file, report.source_hash, report, None)
    except Exception as exc:
        try:
            source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            source_hash = "UNREADABLE"
        return ParseOutcome(source_file, source_hash, None, str(exc))


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, str]]) -> None:
    _base._write_csv(path, fields, rows)


def _write_audit(audit_dir: Path, result: ImportResult, checklist: list[dict[str, str]], quarantine: list[dict[str, str]], workers: int) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(audit_dir / "html_delete_checklist.csv", ["source_file", "sha256", "import_status", "report_id", "raw_actions", "equity_samples", "wallet_change_samples", "safe_to_delete", "reason"], checklist)
    _write_csv(audit_dir / "quarantine.csv", ["source_file", "sha256", "reason"], quarantine)
    manifest = {
        "importer": IMPORTER_NAME,
        "schema_version": SCHEMA_VERSION,
        "storage_mode": "one-lossless-compressed-payload-per-report",
        "workers": workers,
        "scanned_reports": result.scanned_reports,
        "parsed_reports": result.parsed_reports,
        "imported_reports": result.imported_reports,
        "skipped_reports": result.skipped_reports,
        "quarantined_reports": result.quarantined_reports,
        "raw_trade_action_count": result.raw_trade_action_count,
        "equity_sample_count": result.equity_sample_count,
        "safe_delete_rule": "Delete only HTML files marked safe_to_delete=YES after reviewing this manifest.",
    }
    temporary = audit_dir / "import_manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(audit_dir / "import_manifest.json")


def _quarantine(connection: duckdb.DuckDBPyConnection, outcome: ParseOutcome, checklist: list[dict[str, str]], quarantine: list[dict[str, str]]) -> None:
    reason = outcome.error or "unknown worker error"
    connection.execute(
        "INSERT INTO rejected_imports VALUES (?,?,?,?) ON CONFLICT (source_sha256) DO UPDATE SET source_file=excluded.source_file, detected_at_utc=excluded.detected_at_utc, reason=excluded.reason",
        [outcome.source_hash, outcome.source_file, datetime.now(timezone.utc), reason],
    )
    checklist.append({"source_file": outcome.source_file, "sha256": outcome.source_hash, "import_status": "QUARANTINE", "report_id": "", "raw_actions": "", "equity_samples": "", "wallet_change_samples": "", "safe_to_delete": "NO", "reason": reason})
    quarantine.append({"source_file": outcome.source_file, "sha256": outcome.source_hash, "reason": reason})


def _store_outcome(connection: duckdb.DuckDBPyConnection, outcome: ParseOutcome, known_hashes: dict[str, tuple[str, int, int, int]], known_canonical: dict[str, str], known_grids: set[str], known_points: set[str], checklist: list[dict[str, str]], quarantine: list[dict[str, str]]) -> tuple[str, int, int]:
    """Return status, raw actions, and equity samples for one completed parse."""
    if outcome.error or outcome.report is None:
        _quarantine(connection, outcome, checklist, quarantine)
        return "quarantine", 0, 0
    report = outcome.report
    existing = known_hashes.get(report.source_hash)
    if existing:
        checklist.append({"source_file": outcome.source_file, "sha256": report.source_hash, "import_status": "SKIPPED_IDENTICAL", "report_id": existing[0], "raw_actions": str(existing[1]), "equity_samples": str(existing[2]), "wallet_change_samples": str(existing[3]), "safe_to_delete": "YES", "reason": "already imported by identical SHA-256"})
        return "skipped", 0, 0
    try:
        report_id, wallet_count = _base._insert_report(connection, report, known_canonical, known_grids, known_points)
    except Exception as exc:
        _quarantine(connection, ParseOutcome(outcome.source_file, report.source_hash, None, str(exc)), checklist, quarantine)
        return "quarantine", 0, 0
    known_hashes[report.source_hash] = (report_id, len(report.actions), len(report.equity_scaled), wallet_count)
    checklist.append({"source_file": outcome.source_file, "sha256": report.source_hash, "import_status": "OK", "report_id": report_id, "raw_actions": str(len(report.actions)), "equity_samples": str(len(report.equity_scaled)), "wallet_change_samples": str(wallet_count), "safe_to_delete": "YES", "reason": ""})
    return "imported", len(report.actions), len(report.equity_scaled)


def import_html_reports(html_dir: Path, database_path: Path, audit_dir: Path, *, workers: int = 20, progress_every: int = 10, batch_size: int = 250) -> ImportResult:
    if not html_dir.is_dir():
        raise ValueError(f"HTML directory does not exist: {html_dir}")
    if workers < 1:
        raise ValueError("workers must be at least one")
    if progress_every < 1 or batch_size < 1:
        raise ValueError("progress_every and batch_size must be at least one")
    files = sorted(str(path.resolve()) for path in html_dir.rglob("*") if path.is_file() and path.suffix.casefold() == ".html")
    print(IMPORTER_NAME, flush=True)
    print(f"database: {database_path.resolve()}", flush=True)
    print(f"workers: {workers}; one DuckDB writer", flush=True)
    print(f"found {len(files)} HTML report(s); progress will be shown every {progress_every} completed parse(s)", flush=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(database_path))
    imported = skipped = quarantined = raw_actions = equity_samples = parsed = committed_since_last = 0
    checklist: list[dict[str, str]] = []
    quarantine: list[dict[str, str]] = []
    try:
        _base._schema(connection)
        known_hashes, known_canonical, known_grids, known_points = _base._load_known(connection)
        connection.execute("BEGIN TRANSACTION")
        file_iterator = iter(files)
        in_flight: dict[Future[ParseOutcome], str] = {}
        with ProcessPoolExecutor(max_workers=workers) as executor:
            def fill_queue() -> None:
                while len(in_flight) < workers * 3:
                    try:
                        source_file = next(file_iterator)
                    except StopIteration:
                        return
                    in_flight[executor.submit(_parse_worker, source_file)] = source_file

            fill_queue()
            while in_flight:
                completed, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in completed:
                    source_file = in_flight.pop(future)
                    try:
                        outcome = future.result()
                    except BaseException as exc:
                        outcome = ParseOutcome(source_file, "UNREADABLE", None, f"worker failed: {exc}")
                    parsed += 1
                    status, action_count, equity_count = _store_outcome(connection, outcome, known_hashes, known_canonical, known_grids, known_points, checklist, quarantine)
                    if status == "imported":
                        imported += 1
                        raw_actions += action_count
                        equity_samples += equity_count
                        committed_since_last += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        quarantined += 1
                    if committed_since_last >= batch_size:
                        connection.execute("COMMIT")
                        connection.execute("BEGIN TRANSACTION")
                        committed_since_last = 0
                    if parsed % progress_every == 0 or parsed == len(files):
                        print(f"parsed {parsed}/{len(files)}; written={imported}; skipped={skipped}; quarantine={quarantined}; queued={len(in_flight)}", flush=True)
                fill_queue()
        connection.execute("COMMIT")
        connection.execute("CHECKPOINT")
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise
    finally:
        connection.close()
    result = ImportResult(len(files), parsed, imported, skipped, quarantined, raw_actions, equity_samples)
    _write_audit(audit_dir, result, checklist, quarantine, workers)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=250)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = import_html_reports(args.html_dir, args.database, args.audit_dir, workers=args.workers, progress_every=args.progress_every, batch_size=args.batch_size)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"scanned_reports": result.scanned_reports, "parsed_reports": result.parsed_reports, "imported_reports": result.imported_reports, "skipped_reports": result.skipped_reports, "quarantined_reports": result.quarantined_reports, "raw_trade_action_count": result.raw_trade_action_count, "equity_sample_count": result.equity_sample_count}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
