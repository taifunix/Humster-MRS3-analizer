"""MRS2 history in the trade store: P2 segment plan per pair, simulation of a segment, the history run.

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 1 contract»).
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Executor, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
import uuid

import duckdb
import openpyxl

from .minute_data import ensure_days
from .mrs2_simulator import MinuteBars, RunSettings, load_minutes, simulate_mrs2, simulate_period
from .period_csv import GENERATOR_VERSION, PERIOD_DAYS, Point, first_period_day, has_warmup_data, journal_append, ms_of, split_periods
from .trade_store import (
    SegmentKey, SegmentTables, TradeStore, TradeStoreError, build_tables, hourly_rows, metrics_columns, point_id, price_rows,
    trade_columns, write_segment_files,
)

ENGINE = GENERATOR_VERSION
SOURCE = "simulator"
LATEST_DAYS = PERIOD_DAYS
VERDICT_RANK = {"GO": 0, "CHECK": 1, None: 2, "": 2, "STOP": 3}


def pair_order(registry: Path) -> list[tuple[str, date]]:
    """Pairs with their listing day: screening verdict GO, CHECK, none, STOP; inside a group by 7-day turnover."""
    workbook = openpyxl.load_workbook(registry, read_only=True, data_only=True)
    try:
        pairs = workbook["Пары"]
        head = list(next(pairs.iter_rows(min_row=1, max_row=1, values_only=True)))
        listing = {}
        for row in pairs.iter_rows(min_row=2, values_only=True):
            if row[0]:
                value = row[head.index("Дата листинга на Bybit (UTC)")]
                listing[str(row[0]).strip().upper()] = value.date() if isinstance(value, datetime) else None
        screening = {}
        if "Скрининг" in workbook.sheetnames:
            sheet = workbook["Скрининг"]
            shead = list(next(sheet.iter_rows(min_row=1, max_row=1, values_only=True)))
            for row in sheet.iter_rows(min_row=2, values_only=True):
                if row[0] and (row[shead.index("Сторона")] in (None, "LONG")):
                    volume = row[shead.index("Объём 7д, USDT")] if "Объём 7д, USDT" in shead else None
                    screening[str(row[0]).strip().upper()] = (row[shead.index("Вердикт")], float(volume or 0))
    finally:
        workbook.close()

    def key(symbol: str):
        verdict, volume = screening.get(symbol, (None, 0.0))
        return (VERDICT_RANK.get(verdict, 2), -volume, symbol)
    return [(symbol, listing[symbol]) for symbol in sorted(listing, key=key) if listing[symbol] is not None]


def pair_segments(listing: date, end_day: date, warmup_days: int = 4) -> list[tuple[date, date]]:
    """P2 history periods over [listing + warm-up, end − 20 days), then the latest window [end − 20, end)."""
    first = first_period_day(listing, warmup_days)
    latest = end_day - timedelta(days=LATEST_DAYS)
    if first > latest:
        return []
    return [*split_periods(first, latest), (latest, end_day)]


def simulate_segment(minutes: MinuteBars, start: date, end: date, *, tick: float, qty_step: float,
                     points: Sequence[Point], settings: RunSettings = RunSettings()) -> SegmentTables:
    """Fresh simulator run of [start, end) for every point: trades, hourly series (with the price series), metrics."""
    start_ms, end_ms = ms_of(start), ms_of(end)
    trades, hourly, metrics = [], [price_rows(minutes, start_ms, end_ms)], []
    for point in sorted(points, key=lambda p: point_id(p.timeframe, p.shift_bp, p.open_len, p.close_len)):
        pid = point_id(point.timeframe, point.shift_bp, point.open_len, point.close_len)
        cycles = simulate_mrs2(minutes, start_ms, end_ms, timeframe=point.timeframe, open_len=point.open_len,
                               open_mult=(10_000 - point.shift_bp) / 10_000, close_len=point.close_len, tick=tick,
                               settings=settings)
        result = simulate_period(cycles, minutes, start_ms, end_ms, qty_step=qty_step, settings=settings)
        trades.append(trade_columns(pid, result))
        hourly.append(hourly_rows(pid, result, start_ms, end_ms))
        metrics.append(metrics_columns(pid, result))
    return build_tables(trades, hourly, metrics)


# -- worker tasks ---------------------------------------------------------------------


def prepare_pair(symbol: str, listing: date, data_root: str, end_day: date, download: bool, warmup_days: int,
                 download_threads: int) -> tuple[list[tuple[date, date]], dict]:
    """Plan the pair's segments and download the missing candle days of the plan."""
    plan = pair_segments(listing, end_day, warmup_days)
    record = {"pair": symbol, "listing_date": listing.isoformat(), "end_day": end_day.isoformat()}
    if not plan:
        return [], {**record, "status": "NO_FULL_PERIOD"}
    if not download:
        return plan, {**record, "status": "PLANNED", "segments": len(plan)}
    days = ensure_days(Path(data_root), symbol, plan[0][0] - timedelta(days=warmup_days), end_day - timedelta(days=1),
                       workers=download_threads)
    return plan, {**record, "status": "PLANNED", "segments": len(plan), "days_created": len(days["created"]),
                  "days_failed": [day.isoformat() for day in days["failed"]]}


def missing_days(data_root: Path, symbol: str, first: date, end: date) -> list[date]:
    """Days of [first, end) without a candle file in the tester's data folder."""
    folder = Path(data_root) / symbol
    return [first + timedelta(days=offset) for offset in range((end - first).days)
            if not (folder / f"{symbol}{(first + timedelta(days=offset)).isoformat()}_1m.parquet").is_file()]


def simulate_and_write(store_root: str, key: SegmentKey, data_root: str, tick: float, qty_step: float,
                       points: Sequence[Point], settings: RunSettings, replace_unregistered: bool = False) -> dict:
    """Simulate one segment and write its files; returns the catalog record or a skip status.

    Every candle day of the warm-up and the span must exist: a day lost to a failed download would otherwise be
    stored as a quiet market under the segment's permanent id."""
    first = key.start - timedelta(days=settings.warmup_days)
    absent = missing_days(Path(data_root), key.symbol, first, key.end)
    if absent:
        return {"status": "MISSING_DAYS", "days": [day.isoformat() for day in absent]}
    minutes = load_minutes(Path(data_root) / key.symbol, key.symbol, ms_of(first), ms_of(key.end))
    if not has_warmup_data(minutes, key.start, settings.warmup_days):
        return {"status": "NO_WARMUP_DATA"}
    tables = simulate_segment(minutes, key.start, key.end, tick=tick, qty_step=qty_step, points=points,
                              settings=settings)
    record = write_segment_files(Path(store_root), key, tables, settings=settings, tick_size=tick, qty_step=qty_step,
                                 replace_unregistered=replace_unregistered)
    return {"status": "OK", "record": record}


# -- history run ----------------------------------------------------------------------------

PREPARE_IN_FLIGHT = 2


def _reactivate(store: TradeStore, segment: SegmentKey, journal: Path, base: Mapping) -> bool:
    """Reactivate a superseded segment planned again; False (journaled) when its files are missing or damaged."""
    try:
        store.reactivate(segment.segment_id)
    except (TradeStoreError, OSError, duckdb.Error) as error:
        journal_append(journal, {**base, "segment_id": segment.segment_id, "status": "REACTIVATE_FAILED",
                                 "error": f"{type(error).__name__}: {error}"})
        return False
    journal_append(journal, {**base, "segment_id": segment.segment_id, "status": "REACTIVATED"})
    return True


def run_history(store: TradeStore, *, data_root: Path, pairs: Sequence[tuple[str, date]], end_day: date,
                instruments: Mapping[str, tuple[float, float]], points: Sequence[Point], workers: int,
                download: bool = True, settings: RunSettings = RunSettings(), exchange: str = "bybit",
                engine: str = ENGINE, run_args: Mapping | None = None,
                progress: Callable[[dict], None] | None = None) -> dict:
    """Bring every pair's planned segments into the store, pairs in the given priority order.

    Workers simulate and write segment files; only this process writes the catalog and the journal. Pairs are
    prepared lazily (at most PREPARE_IN_FLIGHT at a time) so the first pairs' segments start before later pairs
    are even planned; download threads share the `workers` limit.
    """
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    workers = max(1, workers)
    queue = list(dict.fromkeys(pairs))[::-1]          # duplicates dropped, popped from the end in priority order
    store.register_points(point_id(p.timeframe, p.shift_bp, p.open_len, p.close_len) for p in points)
    store.start_run(run_id, engine=engine, settings=settings, end_day=end_day, args=dict(run_args or {}))
    journal = store.root / "journal.jsonl"
    counts = {"written": 0, "reactivated": 0, "skipped": 0, "failed": 0}
    units = {"done": 0, "total": len(queue)}           # one unit per pair plan, plus one per segment to simulate

    def report(symbol: str, step: str, error: str | None = None) -> None:
        if progress is None:
            return
        if error is not None:
            progress({"error": error, "pair": symbol, "step": step})
        progress({"progress": {"done": units["done"], "total": units["total"], "step": step, "pair": symbol}})

    download_threads = max(1, workers // PREPARE_IN_FLIGHT)
    pool: Executor = ThreadPoolExecutor(max_workers=1) if workers == 1 else ProcessPoolExecutor(max_workers=workers)
    pending: dict = {}

    def in_flight_prepares() -> int:
        return sum(1 for kind, _, _ in pending.values() if kind == "prepare")

    def fill() -> None:
        while queue and len(pending) < workers and in_flight_prepares() < PREPARE_IN_FLIGHT:
            symbol, listing = queue.pop()
            pending[pool.submit(prepare_pair, symbol, listing, str(data_root), end_day, download, settings.warmup_days,
                                download_threads)] = ("prepare", symbol, None)

    try:
        with pool:
            fill()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    kind, symbol, key = pending.pop(future)
                    base = {"run_id": run_id, "pair": symbol}
                    try:
                        result = future.result()
                    except Exception as error:  # one pair or segment never stops the others
                        counts["failed"] += 1
                        journal_append(journal, {**base, "stage": kind, "status": "ERROR",
                                                 "segment_id": key.segment_id if key else None,
                                                 "error": f"{type(error).__name__}: {error}"})
                        units["done"] += 1
                        report(symbol, kind, f"{type(error).__name__}: {error}")
                        continue
                    if kind == "prepare":
                        plan, record = result
                        journal_append(journal, {**base, **record})
                        known = store.statuses(exchange, symbol)
                        tick, qty_step = instruments[symbol]
                        for start, end in plan:
                            segment = SegmentKey(exchange, symbol, SOURCE, start, end, engine)
                            status = known.get(segment.segment_id)
                            if status == "active":
                                counts["skipped"] += 1
                            elif status == "superseded" and _reactivate(store, segment, journal, base):
                                counts["reactivated"] += 1
                            else:
                                # absent, or superseded with damaged files: simulate again and own the files
                                pending[pool.submit(simulate_and_write, str(store.root), segment, str(data_root),
                                                    tick, qty_step, points, settings, True)] = ("segment", symbol,
                                                                                                segment)
                                units["total"] += 1
                        units["done"] += 1
                        report(symbol, "plan")
                        fill()
                        continue
                    if result["status"] == "OK":
                        try:
                            store.register(result["record"], replace_superseded=True)
                        except Exception as error:   # a segment already registered (duplicate plan, race): count,
                            counts["failed"] += 1      # keep the files, move on
                            journal_append(journal, {**base, "segment_id": key.segment_id, "status": "ERROR",
                                                     "error": f"{type(error).__name__}: {error}"})
                            continue
                        counts["written"] += 1
                        journal_append(journal, {**base, "segment_id": key.segment_id, "status": "OK",
                                                 "trades": result["record"]["trades"]})
                    else:
                        counts["skipped"] += 1
                        journal_append(journal, {**base, "segment_id": key.segment_id,
                                                 **{name: value for name, value in result.items()}})
                    units["done"] += 1
                    report(symbol, "segment", None if result.get("status") == "OK" else
                           f"{key.segment_id}: {result.get('status')} {result.get('missing_days', '')}".strip())
                fill()
    finally:
        store.finish_run(run_id, written=counts["written"] + counts["reactivated"], skipped=counts["skipped"],
                         failed=counts["failed"])
    return {"run_id": run_id, **counts}


__all__ = ["ENGINE", "SOURCE", "missing_days", "pair_order", "pair_segments", "run_history", "simulate_segment"]
