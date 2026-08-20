#!/usr/bin/env python3
"""Headless runner for the recursive HTML -> Source DuckDB import.

Thin, platform-independent wrapper over the authoritative import API
(``mrs3.duckdb_import``). It loads the configured import settings, rejects
missing source database/audit configuration, preflights the HTML tree, then
imports through ``import_html_tree`` with the exact preflight token bound.
It never parses or writes DuckDB itself and never prints configuration values.

Progress is emitted as newline-delimited JSON on stdout; the final line is a
summary. The process exits nonzero when the final state is not COMMITTED or
quarantined reports are nonzero.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any, Callable, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from mrs3.config import load_duckdb_import_settings  # noqa: E402
from mrs3.duckdb_import import (  # noqa: E402
    _JOB_ID,
    ImportJobResult,
    ImportProgress,
    ImportRequest,
    SnapshotProgress,
    import_html_tree,
    preflight_html_import,
)

EXIT_OK = 0
EXIT_IMPORT_FAILURE = 1
EXIT_USAGE = 2


def _emit(event: dict[str, object]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")))


def _guard(callback: Callable[[Any], object]) -> Callable[[Any], None]:
    def wrapped(item: Any) -> None:
        try:
            callback(item)
        except Exception:
            # Progress delivery is observational and must not abort the import.
            pass

    return wrapped


def _snapshot_progress(item: SnapshotProgress) -> None:
    _emit(
        {
            "event": "preflight_progress",
            "discovered": item.discovered,
            "snapshotted": item.snapshotted,
            "total_bytes": item.total_bytes,
            "processed_bytes": item.processed_bytes,
        }
    )


def _import_progress(item: ImportProgress) -> None:
    _emit(
        {
            "event": "import_progress",
            "phase": item.final_state,
            "discovered": item.discovered,
            "counts": item.counts,
        }
    )


def _fatal(message: str, exit_code: int) -> int:
    _emit({"event": "error", "error": message})
    print(f"import-html-duckdb: {message}", file=sys.stderr)
    return exit_code


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {raw!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import-html-duckdb",
        description="Import a recursive HTML report tree into the configured Source DuckDB.",
    )
    parser.add_argument(
        "--html-root",
        type=Path,
        required=True,
        help="root folder containing the compact HTML reports to import recursively",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to the local config (default: config.local.json next to the repository root)",
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=None,
        help="override duckdb_import.workers (default: configured value)",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=None,
        help="override duckdb_import.transaction_batch_size (default: configured value)",
    )
    parser.add_argument(
        "--job-id",
        default=None,
        help="explicit safe 1-128 character import job id (default: derived from inputs)",
    )
    return parser


def _config_path(args: argparse.Namespace) -> Path:
    if args.config is not None:
        return Path(args.config)
    return _REPO_ROOT / "config.local.json"


def _import_counts(result: ImportJobResult) -> dict[str, int]:
    return {
        "parsed": result.parsed,
        "inserted": result.inserted,
        "replaced": result.replaced,
        "identical": result.identical,
        "ambiguous": result.ambiguous,
        "quarantined": result.quarantined,
    }


def run(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    if not config_path.is_file():
        return _fatal(f"config file not found: {config_path}", EXIT_USAGE)
    try:
        settings = load_duckdb_import_settings(config_path)
    except (OSError, ValueError) as exc:
        return _fatal(f"cannot load import settings from {config_path}: {exc}", EXIT_USAGE)
    if settings.source_duckdb_path is None or settings.audit_root is None:
        return _fatal(
            "duckdb_import.source_duckdb_path and duckdb_import.audit_root must be configured",
            EXIT_USAGE,
        )
    if args.job_id is not None and not _JOB_ID.fullmatch(args.job_id):
        return _fatal(
            f"job_id must be a safe 1-128 character identifier, got {args.job_id!r}",
            EXIT_USAGE,
        )
    html_root = Path(args.html_root).resolve()
    if not html_root.is_dir():
        return _fatal(f"html root is not a directory: {html_root}", EXIT_USAGE)

    request = ImportRequest(
        root_path=html_root,
        database_path=settings.source_duckdb_path,
        audit_root=settings.audit_root,
        workers=args.workers if args.workers is not None else settings.workers,
        transaction_batch_size=(
            args.batch_size if args.batch_size is not None else settings.transaction_batch_size
        ),
        job_id=args.job_id,
    )
    try:
        preflight = preflight_html_import(request, _guard(_snapshot_progress))
    except Exception as exc:
        return _fatal(f"preflight failed: {exc}", EXIT_IMPORT_FAILURE)
    _emit(
        {
            "event": "preflight_ready",
            "discovered": preflight.discovered,
            "source_schema_version": preflight.source_schema_version,
        }
    )
    authorized = replace(
        request,
        expected_preflight_token=preflight.token,
        preflight=preflight,
    )
    try:
        result = import_html_tree(authorized, _guard(_import_progress))
    except Exception as exc:
        return _fatal(f"import failed: {exc}", EXIT_IMPORT_FAILURE)
    _emit(
        {
            "event": "summary",
            "job_id": result.job_id,
            "final_state": result.final_state,
            "discovered": result.discovered,
            "counts": _import_counts(result),
            "safe_to_delete": result.safe_to_delete,
            "manifest_path": str(result.manifest_path),
            "checklist_path": str(result.checklist_path),
            "error": result.error,
        }
    )
    if result.final_state != "COMMITTED" or result.quarantined != 0:
        return EXIT_IMPORT_FAILURE
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
