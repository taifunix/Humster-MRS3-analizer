"""Reproducible, copy-only M5 measurements for Performance v2 selection.

This harness never targets the Panel's configured database. Pass a frozen v6
copy outside the repository; every mutating run uses a temporary copy of it.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Iterator, Mapping, Sequence

import duckdb

from mrs3.panel import PanelController
from mrs3.performance_v2_selection import (
    load_selection_config,
    parse_selection_request,
    prepare_selection_window_cache,
    selection_cache_missing_strategy_ids,
)
from mrs3.performance_v2_store import require_performance_v2_readable
from mrs3.performance_v2_store import load_performance_v2_config, performance_v2_database_path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "performance-v2" / "strategy_performance.duckdb"
CONSUMER_MODES = ("legacy", "filter_only", "rank_only", "both")
RECALC_CACHE_STATES = ("cold", "old_warm_new_cold")
WORKER_PROFILES = (1, 4, 8, 16)
_WRITE_SQL = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|REPLACE|MERGE|COPY)\b", re.I)
_COUNT_SQL_PATCH_LOCK = threading.Lock()
_TABLES = (
    "window_metrics", "equity_quality_metrics", "strategy_equity", "strategy_actions",
)


class _SQLCounter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.values: dict[str, int] = {
            "queries": 0,
            "connections_opened": 0,
            "read_queries": 0,
            "write_queries": 0,
            "result_rows_returned": 0,
            "window_metrics_reads": 0,
            "equity_quality_metrics_reads": 0,
            "strategy_equity_reads": 0,
            "strategy_actions_reads": 0,
            "cache_writes": 0,
            "window_metrics_rows_returned": 0,
            "equity_quality_metrics_rows_returned": 0,
            "strategy_equity_rows_returned": 0,
            "strategy_actions_rows_returned": 0,
        }

    def statement(self, sql: str) -> tuple[bool, frozenset[str]]:
        normalized = sql.lower()
        write = bool(_WRITE_SQL.match(sql))
        tables = frozenset(table for table in _TABLES if re.search(rf"\b{table}\b", normalized))
        read = not write and bool(re.match(r"\s*(?:select|with|show|describe|explain|pragma)\b", sql, re.I))
        with self.lock:
            self.values["queries"] += 1
            self.values["write_queries" if write else "read_queries" if read else "other_queries"] = (
                self.values.get("write_queries" if write else "read_queries" if read else "other_queries", 0) + 1
            )
            for table in tables:
                if read:
                    self.values[f"{table}_reads"] += 1
                if write and table in {"window_metrics", "equity_quality_metrics"}:
                    self.values["cache_writes"] += 1
        return read, tables

    def connection_opened(self) -> None:
        with self.lock:
            self.values["connections_opened"] += 1

    def rows(self, count: int, read: bool, tables: frozenset[str]) -> None:
        if not count:
            return
        with self.lock:
            self.values["result_rows_returned"] += count
            if read:
                for table in tables:
                    self.values[f"{table}_rows_returned"] += count

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.values)


class _CursorProxy:
    def __init__(self, cursor: object, counter: _SQLCounter, read: bool, tables: frozenset[str]) -> None:
        self._cursor = cursor
        self._counter = counter
        self._read = read
        self._tables = tables

    def fetchone(self):
        row = self._cursor.fetchone()
        self._counter.rows(int(row is not None), self._read, self._tables)
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._counter.rows(len(rows), self._read, self._tables)
        return rows

    def fetchmany(self, size: int = 1):
        rows = self._cursor.fetchmany(size)
        self._counter.rows(len(rows), self._read, self._tables)
        return rows

    def __iter__(self):
        for row in self._cursor:
            self._counter.rows(1, self._read, self._tables)
            yield row

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _ConnectionProxy:
    def __init__(self, connection: object, counter: _SQLCounter) -> None:
        self._connection = connection
        self._counter = counter
        self._last_read = False
        self._last_tables: frozenset[str] = frozenset()

    def execute(self, sql: str, *args, **kwargs):
        self._last_read, self._last_tables = self._counter.statement(str(sql))
        return _CursorProxy(self._connection.execute(sql, *args, **kwargs), self._counter, self._last_read, self._last_tables)

    def executemany(self, sql: str, *args, **kwargs):
        self._last_read, self._last_tables = self._counter.statement(str(sql))
        return _CursorProxy(self._connection.executemany(sql, *args, **kwargs), self._counter, self._last_read, self._last_tables)

    def fetchone(self):
        row = self._connection.fetchone()
        self._counter.rows(int(row is not None), self._last_read, self._last_tables)
        return row

    def fetchall(self):
        rows = self._connection.fetchall()
        self._counter.rows(len(rows), self._last_read, self._last_tables)
        return rows

    def fetchmany(self, size: int = 1):
        rows = self._connection.fetchmany(size)
        self._counter.rows(len(rows), self._last_read, self._last_tables)
        return rows

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


@contextmanager
def _count_sql() -> Iterator[_SQLCounter]:
    if not _COUNT_SQL_PATCH_LOCK.acquire(blocking=False):
        raise RuntimeError("overlapping SQL measurement contexts are unsupported")
    counter = _SQLCounter()
    original_connect = duckdb.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        counter.connection_opened()
        return _ConnectionProxy(connection, counter)

    duckdb.connect = connect
    try:
        yield counter
    finally:
        duckdb.connect = original_connect
        _COUNT_SQL_PATCH_LOCK.release()


@contextmanager
def _sample_rss() -> Iterator[dict[str, int | None]]:
    try:
        import psutil
    except ImportError:
        yield {"rss_start_bytes": None, "peak_rss_bytes": None, "peak_rss_delta_bytes": None}
        return

    process = psutil.Process()
    start = int(process.memory_info().rss)
    rss_sample: dict[str, int | None] = {
        "rss_start_bytes": start,
        "peak_rss_bytes": start,
        "peak_rss_delta_bytes": 0,
    }
    done = threading.Event()

    def sample() -> None:
        while not done.wait(0.01):
            try:
                rss_sample["peak_rss_bytes"] = max(
                    int(rss_sample["peak_rss_bytes"] or 0), int(process.memory_info().rss)
                )
            except psutil.Error:
                return

    sampler = threading.Thread(target=sample, name="panel-benchmark-rss", daemon=True)
    sampler.start()
    try:
        yield rss_sample
    finally:
        done.set()
        sampler.join()
        peak_value = max(int(rss_sample["peak_rss_bytes"] or 0), int(process.memory_info().rss))
        rss_sample["peak_rss_bytes"] = peak_value
        rss_sample["peak_rss_delta_bytes"] = max(0, peak_value - start)


def _measure(call: Callable[[], object]) -> tuple[dict[str, object], object]:
    with _sample_rss() as rss, _count_sql() as sql:
        started = time.perf_counter()
        result = call()
        wall = time.perf_counter() - started
        sql_counts = sql.snapshot()
    return {
        "wall_seconds": wall,
        **rss,
        "sql": sql_counts,
        "row_counter_semantics": "rows fetched into Python; not physical rows scanned by DuckDB",
        "row_counter_coverage": (
            "fetchone/fetchall/fetchmany/iterator only; excludes dataframe, Arrow, and Polars fetch APIs"
        ),
        "sql_counter_scope": (
            "process-wide duckdb.connect patch during measured call; includes worker threads; "
            "excludes pre-existing handles and subprocesses; run standalone"
        ),
        "rss_scope": "parent process RSS, including its threads",
    }, result


def _resolved(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=True)


def _configured_database_path() -> Path | None:
    try:
        config = load_performance_v2_config(ROOT / "config.performance.json")
        return performance_v2_database_path(config)
    except (OSError, ValueError):
        return None


def _same_file_if_present(first: Path, second: Path) -> bool:
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _validate_database_copy(path: Path) -> dict[str, object]:
    source = _resolved(Path(path))
    configured_database = _configured_database_path()
    if configured_database is not None and _same_file_if_present(source, configured_database):
        raise ValueError("database-copy must not be the same file as the configured live Performance v2 database")
    if _same_file_if_present(source, DEFAULT_DATABASE):
        raise ValueError("database-copy must be a frozen copy, not the same file as the default Performance v2 database")
    if source.is_relative_to(ROOT):
        raise ValueError("database must be an explicit frozen copy outside the project, not its configured/default database")
    if not source.is_file() or source.suffix.lower() not in {".duckdb", ".db"}:
        raise ValueError("database-copy must name an existing DuckDB file")
    if any(source.with_name(source.name + suffix).exists() for suffix in (".wal", "-wal")):
        raise ValueError("database copy has a WAL sidecar; checkpoint and freeze the copy first")
    stat = source.stat()
    with duckdb.connect(str(source), read_only=True) as connection:
        version = require_performance_v2_readable(connection)
        if version != 6:
            raise ValueError("database copy must already have Performance v2 schema v6")
    return {
        "path": source,
        "schema_version": 6,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _request_payload(symbol: str, side: str, top_n: int, mode: str = "both") -> dict[str, object]:
    if mode not in CONSUMER_MODES:
        raise ValueError(f"unknown consumer mode: {mode}")
    equity_filter = mode in {"filter_only", "both"}
    equity_rank = mode in {"rank_only", "both"}
    rank: dict[str, object] = {
        "id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": top_n,
    }
    if equity_rank:
        rank["method"] = "equity_quality_v1"
    return {
        "symbol": symbol,
        "side": side,
        "stages": [
            {"id": "filter_equity_regime", "enabled": equity_filter, "scope": "pair_side"},
            rank,
        ],
    }


def _create_controller(root: Path, workers: int, database: Path) -> PanelController:
    root.mkdir(parents=True, exist_ok=True)
    database_root = root / "performance-v2"
    database_root.mkdir(parents=True, exist_ok=True)
    target = database_root / "strategy_performance.duckdb"
    shutil.copy2(database, target)
    (root / "config.local.json").write_text(json.dumps({
        "panel_paths": {"performance_db_root": "v1"},
        "duckdb_import": {"workers": workers},
    }), encoding="utf-8")
    (root / "config.performance.json").write_text(json.dumps({
        "unified_performance_v2": {"database_root": "performance-v2"},
    }), encoding="utf-8")
    return PanelController(root, root / "config.local.json")


def _json_value(value: object) -> object:
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, (str, int, bool)):
        return value
    if value.__class__.__name__ == "Decimal":
        return str(value)
    return str(value)


def _decision_rows(result) -> list[dict[str, object]]:
    columns = (
        "strategy_id", "result_id", "finalist", "auto_status", "final_rank",
        "elimination_reason", "equity_regime_state", "equity_regime_disposition",
        "equity_regime_reason", "final_score",
    )
    return [
        {name: _json_value(row[name]) for name in columns if name in result.columns}
        for _, row in result.iterrows()
    ]


def _decision_outcome(controller: PanelController, payload: Mapping[str, object]) -> tuple[list[dict[str, object]], str, dict[str, object]]:
    _, result = controller._performance_v2_selection_result(payload)
    rows = _decision_rows(result)
    signature = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return rows, signature, result.attrs.get("stage_counts", {})


def _write_jsonl(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False, default=str) + "\n")


def _revision() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _cohort(source: Path, symbol: str, side: str) -> tuple[list[tuple[int, int]], str]:
    with duckdb.connect(str(source), read_only=True) as connection:
        pairs = connection.execute(
            """select strategy_id, current_result_id from strategies
                 where lifecycle_status = 'ACTIVE' and symbol = ? and side = ?
                   and current_result_id is not null order by strategy_id""",
            [symbol, side],
        ).fetchall()
    if not pairs:
        raise ValueError(f"frozen copy has no active {symbol}/{side} cohort")
    signature = hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()
    return [(int(strategy_id), int(result_id)) for strategy_id, result_id in pairs], signature


def run_benchmark(
    *, database_copy: Path, symbol: str, side: str, top_n: int, output_jsonl: Path,
    consumer_modes: Sequence[str] = CONSUMER_MODES,
) -> list[dict[str, object]]:
    """Measure warm previews for each consumer mode on one isolated scratch copy."""
    source_info = _validate_database_copy(Path(database_copy))
    source = source_info["path"]
    assert isinstance(source, Path)
    before = source.stat()
    cohort, cohort_signature = _cohort(source, symbol, side)
    if set(consumer_modes).difference(CONSUMER_MODES):
        raise ValueError("consumer_modes must be selected from legacy/filter_only/rank_only/both")
    records: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="mrs3-v2-preview-") as temporary:
        root = Path(temporary) / "panel"
        controller = _create_controller(root, 1, source)
        prep_payload = _request_payload(symbol, side, top_n, "both")
        controller.strategies_performance_v2_recalculate(prep_payload)
        for mode in consumer_modes:
            # Give each mode a fresh in-process candidate cache so the four
            # consumer variants cannot reuse a frame assembled for another mode.
            controller = PanelController(root, root / "config.local.json")
            payload = _request_payload(symbol, side, top_n, mode)
            status = controller.strategies_performance_v2_selection_cache_status(payload)
            if not status.get("ready"):
                raise RuntimeError(f"warm-cache preparation incomplete for {mode}: {status}")
            measured: list[dict[str, object]] = []
            warmup, _ = _measure(lambda: controller.strategies_performance_v2_selection_preview(payload))
            warmup_rows, warmup_signature, stage_counts = _decision_outcome(controller, payload)
            warmup["decision_signature_sha256"] = warmup_signature
            for _ in range(3):
                run, preview = _measure(lambda: controller.strategies_performance_v2_selection_preview(payload))
                decision_rows, decision_signature, measured_stage_counts = _decision_outcome(controller, payload)
                run["decision_signature_sha256"] = decision_signature
                run["stage_counts"] = preview.get("stages", {}) if isinstance(preview, Mapping) else {}
                run["decision_validation_queries_included"] = False
                measured.append(run)
                if run["stage_counts"] != stage_counts:
                    raise RuntimeError(f"preview stage counts changed during measured repeats for {mode}")
                if measured_stage_counts != stage_counts:
                    raise RuntimeError(f"preview stage counts changed during measured repeats for {mode}")
                if decision_rows != warmup_rows:
                    raise RuntimeError(f"preview decisions changed during measured repeats for {mode}")
            signatures = {run["decision_signature_sha256"] for run in measured}
            if len(signatures) != 1 or warmup_signature not in signatures:
                raise RuntimeError(f"preview decision signature was not stable for {mode}")
            records.append({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "runtime_revision": _revision(),
                "schema_version": source_info["schema_version"],
                "cohort_size": len(cohort),
                "cohort_signature_sha256": cohort_signature,
                "symbol": symbol,
                "side": side,
                "top_n": top_n,
                "consumer_mode": mode,
                "request_payload": payload,
                "cache_state": "all_warm",
                "warmup_runs": 1,
                "warmup_run": warmup,
                "measured_runs": measured,
                "stage_counts": stage_counts,
                "decision_rows": warmup_rows,
                "decision_signature_sha256": warmup_signature,
                "baseline_comparison": "not_run: prior runtime is not launched by this harness",
                "one_replace": "not_run: requires an exact replacement inbox/report and import job",
            })
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("source frozen copy changed during preview benchmark")
    for record in records:
        record["source_copy_stat_unchanged"] = True
    _write_jsonl(Path(output_jsonl), records)
    return records


def _prepare_cache_state(database: Path, root: Path, symbol: str, side: str, workers: int, state: str) -> dict[str, int]:
    if state == "cold":
        with duckdb.connect(str(database)) as connection:
            connection.execute("delete from window_metrics")
            connection.execute("delete from equity_quality_metrics")
    elif state == "old_warm_new_cold":
        selection_path = root / "config.performance.json"
        config = load_selection_config(selection_path)
        request = parse_selection_request(_request_payload(symbol, side, 1, "legacy"))
        with duckdb.connect(str(database), read_only=True) as connection:
            missing = selection_cache_missing_strategy_ids(connection, request, config, include_equity=False)
        prepare_selection_window_cache(database, request, config, workers, missing, include_equity=False)
        with duckdb.connect(str(database)) as connection:
            connection.execute("delete from equity_quality_metrics")
    else:
        raise ValueError(f"unsupported recalculation cache state: {state}")
    with duckdb.connect(str(database), read_only=True) as connection:
        window_rows = connection.execute(
            """select count(*) from window_metrics wm
                 join strategy_results r on r.result_id = wm.result_id
                 join strategies s on s.strategy_id = r.strategy_id and s.current_result_id = r.result_id
                where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?""",
            [symbol, side],
        ).fetchone()[0]
        equity_rows = connection.execute(
            """select count(*) from equity_quality_metrics eq
                 join strategy_results r on r.result_id = eq.result_id
                 join strategies s on s.strategy_id = r.strategy_id and s.current_result_id = r.result_id
                where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?""",
            [symbol, side],
        ).fetchone()[0]
    return {"window_metrics_rows": int(window_rows), "equity_quality_metrics_rows": int(equity_rows)}


def run_recalculation_profiles(
    *, database_copy: Path, symbol: str, side: str, output_jsonl: Path,
    workers: Sequence[int] = WORKER_PROFILES, repeats: int = 3,
) -> list[dict[str, object]]:
    """Measure copy-isolated recalc for cold and old-warm/new-cold cache states."""
    source_info = _validate_database_copy(Path(database_copy))
    source = source_info["path"]
    assert isinstance(source, Path)
    before = source.stat()
    cohort, cohort_signature = _cohort(source, symbol, side)
    if repeats < 1 or set(workers).difference(WORKER_PROFILES):
        raise ValueError("repeats must be positive and workers must be drawn from 1/4/8/16")
    records: list[dict[str, object]] = []
    payload = _request_payload(symbol, side, 1, "both")
    for worker_count in workers:
        for state in RECALC_CACHE_STATES:
            runs: list[dict[str, object]] = []
            setup_runs: list[dict[str, int]] = []
            warmup_run: dict[str, object] | None = None
            for index in range(repeats + 1):
                with tempfile.TemporaryDirectory(prefix="mrs3-v2-recalc-") as temporary:
                    root = Path(temporary) / "panel"
                    controller = _create_controller(root, worker_count, source)
                    target = root / "performance-v2" / "strategy_performance.duckdb"
                    cache_setup = _prepare_cache_state(target, root, symbol, side, worker_count, state)
                    setup_runs.append(cache_setup)
                    run, _ = _measure(lambda: controller.strategies_performance_v2_recalculate(payload))
                    run["cache_state_before_recalculate"] = cache_setup
                    if index == 0:
                        warmup_run = run
                    else:
                        runs.append(run)
            records.append({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "runtime_revision": _revision(),
                "schema_version": source_info["schema_version"],
                "cohort_size": len(cohort),
                "cohort_signature_sha256": cohort_signature,
                "symbol": symbol,
                "side": side,
                "request_payload": payload,
                "worker_count": worker_count,
                "cache_state": state,
                "warmup_runs": 1,
                "warmup_run": warmup_run,
                "measured_runs": runs,
                "cache_setup_runs": setup_runs,
                "baseline_comparison": "not_run: prior runtime is not launched by this harness",
                "one_replace": "not_run: requires an exact replacement inbox/report and import job",
            })
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("source frozen copy changed during recalculation benchmark")
    for record in records:
        record["source_copy_stat_unchanged"] = True
    _write_jsonl(Path(output_jsonl), records)
    return records


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-copy", required=True, type=Path, help="frozen Performance v2 schema-v6 DuckDB copy outside the repository")
    parser.add_argument("--confirm-frozen-copy", action="store_true", help="confirm the supplied file is not the live/default database")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--side", required=True, choices=("LONG", "SHORT"))
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--output-jsonl", required=True, type=Path)
    parser.add_argument("--include-recalc", action="store_true", help="also run isolated recalculation profiles")
    parser.add_argument("--workers", nargs="+", type=int, choices=WORKER_PROFILES, default=list(WORKER_PROFILES))
    args = parser.parse_args(argv)
    if not args.confirm_frozen_copy:
        parser.error("pass --confirm-frozen-copy only for an offline frozen copy")
    if args.top_n < 1:
        parser.error("--top-n must be positive")
    records = run_benchmark(
        database_copy=args.database_copy,
        symbol=args.symbol,
        side=args.side,
        top_n=args.top_n,
        output_jsonl=args.output_jsonl,
    )
    if args.include_recalc:
        records.extend(run_recalculation_profiles(
            database_copy=args.database_copy,
            symbol=args.symbol,
            side=args.side,
            output_jsonl=args.output_jsonl,
            workers=args.workers,
        ))
    print(json.dumps({"records_written": len(records), "output_jsonl": str(args.output_jsonl)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
