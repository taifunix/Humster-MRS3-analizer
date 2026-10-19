"""MRS2 trade store: immutable Parquet segments (trades, hourly series, metrics) and a DuckDB catalog.

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 1 contract»), ADR-0078.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import itertools
import json
import os
import time
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .mrs2_simulator import HOUR_MS, TIMEFRAME_MS, MinuteBars, PeriodResult, RunSettings

PRICE_POINT = 0          # hourly rows of the pair price series
CONNECT_WAIT_S = 60.0    # how long a catalog connection waits for a file lock held by another process
SIDE_LONG = "LONG"
TRADE_SCHEMA = pa.schema([
    pa.field("point_id", pa.int32(), nullable=False),
    pa.field("entry_ms", pa.int64(), nullable=False),
    pa.field("exit_ms", pa.int64(), nullable=False),
    pa.field("entry_price", pa.float64(), nullable=False),
    pa.field("exit_price", pa.float64(), nullable=False),
    pa.field("qty", pa.float64(), nullable=False),
    pa.field("fee_in", pa.float64(), nullable=False),
    pa.field("fee_out", pa.float64(), nullable=False),
    pa.field("funding", pa.float64(), nullable=False),
    pa.field("exit_kind", pa.string(), nullable=False),
])
HOURLY_SCHEMA = pa.schema([
    pa.field("point_id", pa.int32(), nullable=False),
    pa.field("hour_ms", pa.int64(), nullable=False),
    pa.field("close", pa.float32(), nullable=False),
    pa.field("low", pa.float32(), nullable=False),
    pa.field("high", pa.float32(), nullable=False),
    pa.field("drawdown", pa.float32(), nullable=False),
])
METRICS_SCHEMA = pa.schema([
    pa.field("point_id", pa.int32(), nullable=False),
    pa.field("final_balance", pa.float64(), nullable=False),
    pa.field("marked_final", pa.float64(), nullable=False),
    pa.field("trades", pa.int32(), nullable=False),
    pa.field("wins", pa.int32(), nullable=False),
    pa.field("gross_profit", pa.float64(), nullable=False),
    pa.field("gross_loss", pa.float64(), nullable=False),
    pa.field("fees", pa.float64(), nullable=False),
    pa.field("max_drawdown", pa.float64(), nullable=False),
    pa.field("max_drawdown_percent", pa.float64(), nullable=False),
    pa.field("ulcer_index", pa.float64(), nullable=False),
    pa.field("position_avg_percent", pa.float64(), nullable=False),
    pa.field("position_max_percent", pa.float64(), nullable=False),
    pa.field("last_ms", pa.int64(), nullable=False),
])
FILE_KINDS = ("trades", "hourly", "metrics")
CATALOG_DDL = """
create table if not exists points (
    point_id integer primary key, side varchar, timeframe varchar, shift_bp integer,
    open_len integer, close_len integer, ma_type varchar, ma_source varchar);
create table if not exists segments (
    segment_id varchar primary key, exchange varchar, symbol varchar, source varchar,
    start_ms bigint, end_ms bigint, engine varchar, settings_json varchar, tick_size double, qty_step double,
    trades_path varchar, trades_sha256 varchar, hourly_path varchar, hourly_sha256 varchar,
    metrics_path varchar, metrics_sha256 varchar, points integer, trades bigint,
    status varchar, created_utc varchar, superseded_by varchar);
create table if not exists runs (
    run_id varchar primary key, started_utc varchar, finished_utc varchar, engine varchar,
    settings_json varchar, end_day varchar, args_json varchar, written integer, skipped integer, failed integer);
create table if not exists jobs (
    job_id varchar primary key, exchange varchar, symbol varchar, start_ms bigint, end_ms bigint,
    block_json varchar, rule_version varchar, selection_sha256 varchar, state varchar,
    executor varchar, result_segment varchar, created_utc varchar, updated_utc varchar, error varchar);
create table if not exists plans (
    exchange varchar, symbol varchar, start_ms bigint, end_ms bigint, selection_sha256 varchar, planned_utc varchar,
    primary key (exchange, symbol, start_ms, end_ms));
create table if not exists ready (
    ready_id varchar primary key, exchange varchar, symbol varchar, rule_version varchar,
    inputs_sha256 varchar, created_utc varchar, payload_json varchar);
"""


class TradeStoreError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# -- grid -----------------------------------------------------------------------


def point_id(timeframe: str, shift_bp: int, open_len: int, close_len: int) -> int:
    """Stable id of a LONG SMA(ohlc4) point: TF minutes × 1e6 + shift bp × 100 + open length × 10 + close length."""
    if timeframe not in TIMEFRAME_MS or not (0 < shift_bp < 1000 and 0 < open_len < 10 and 0 < close_len < 10):
        raise TradeStoreError(f"point parameters out of the id range: {timeframe} {shift_bp} {open_len} {close_len}")
    return (TIMEFRAME_MS[timeframe] // 60_000) * 1_000_000 + shift_bp * 100 + open_len * 10 + close_len


def point_params(pid: int) -> tuple[str, int, int, int]:
    minutes, rest = divmod(pid, 1_000_000)
    for timeframe, value in TIMEFRAME_MS.items():
        if value // 60_000 == minutes:
            return timeframe, rest // 100, (rest % 100) // 10, rest % 10
    raise TradeStoreError(f"unknown point id: {pid}")


# -- segment tables -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SegmentTables:
    trades: pa.Table
    hourly: pa.Table
    metrics: pa.Table


def trade_columns(pid: int, result: PeriodResult) -> dict[str, list]:
    ledger = result.ledger
    return {"point_id": [pid] * len(ledger), "entry_ms": [row.entry_ms for row in ledger],
            "exit_ms": [row.exit_ms for row in ledger], "entry_price": [row.entry_price for row in ledger],
            "exit_price": [row.exit_price for row in ledger], "qty": [row.qty for row in ledger],
            "fee_in": [row.fee_in for row in ledger], "fee_out": [row.fee_out for row in ledger],
            "funding": [row.funding for row in ledger], "exit_kind": [row.exit_kind for row in ledger]}


def metrics_columns(pid: int, result: PeriodResult) -> dict[str, list]:
    trades = result.trades
    entries = result.position_percents[0::2]
    return {"point_id": [pid], "final_balance": [result.final_balance], "marked_final": [result.marked_final],
            "trades": [len(trades)], "wins": [sum(1 for gross in trades if gross > 0)],
            "gross_profit": [sum(gross for gross in trades if gross > 0)],
            "gross_loss": [-sum(gross for gross in trades if gross < 0)], "fees": [result.fees],
            "max_drawdown": [result.max_drawdown], "max_drawdown_percent": [result.max_drawdown_percent],
            "ulcer_index": [result.ulcer_index],
            "position_avg_percent": [sum(entries) / len(entries) if entries else 0.0],
            "position_max_percent": [max(result.position_percents, default=0.0)], "last_ms": [result.path[-1][0]]}


def _hourly_aggregate(pid: int, stamps: np.ndarray, values: np.ndarray, start_ms: int, end_ms: int,
                      initial: float) -> dict[str, np.ndarray]:
    """Per UTC hour of [start, end): the last value stamped before the hour end; the low/high of the hour's values
    including the value carried in from the previous hour (`initial` before the first sample); and the largest
    drawdown inside the hour (%) against the running maximum of those values."""
    hours = np.arange(start_ms // HOUR_MS * HOUR_MS, end_ms, HOUR_MS, dtype=np.int64)
    last = np.searchsorted(stamps, hours + HOUR_MS, "left") - 1
    close = np.where(last >= 0, values[np.clip(last, 0, None)], initial)
    carried = np.r_[initial, close[:-1]]
    low, high = carried.copy(), carried.copy()
    bucket = (stamps - hours[0]) // HOUR_MS
    keep = (bucket >= 0) & (bucket < len(hours))
    np.minimum.at(low, bucket[keep], values[keep])
    np.maximum.at(high, bucket[keep], values[keep])
    # running maximum restarted every hour: the carried value first, then the hour's samples in time order
    hour_of = np.concatenate([np.arange(len(hours)), bucket[keep]])
    sample = np.concatenate([carried, values[keep]])
    order = np.argsort(hour_of, kind="stable")
    hour_of, sample = hour_of[order], sample[order]
    floor = sample.min()
    span = sample.max() - floor + 1.0
    running = np.maximum.accumulate(hour_of * span + (sample - floor)) - hour_of * span + floor
    drawdown = np.zeros(len(hours))
    np.maximum.at(drawdown, hour_of, (running - sample) / running * 100)
    return {"point_id": np.full(len(hours), pid, dtype=np.int32), "hour_ms": hours,
            "close": close.astype(np.float32), "low": low.astype(np.float32), "high": high.astype(np.float32),
            "drawdown": drawdown.astype(np.float32)}


def hourly_rows(pid: int, result: PeriodResult, start_ms: int, end_ms: int) -> dict[str, np.ndarray]:
    """Margin balance per UTC hour of the segment from the simulator path."""
    stamps = np.array([stamp for stamp, _ in result.path], dtype=np.int64)
    values = np.array([value for _, value in result.path], dtype=np.float64)
    return _hourly_aggregate(pid, stamps, values, start_ms, end_ms, values[0] if len(values) else result.final_balance)


def price_rows(minutes: MinuteBars, start_ms: int, end_ms: int) -> dict[str, np.ndarray]:
    """Pair price per UTC hour (point 0): last minute close before the hour end, low/high of the minute closes."""
    before = int(np.searchsorted(minutes.timestamps, start_ms, "left")) - 1
    initial = float(minutes.closes[before]) if before >= 0 else float(minutes.closes[0])
    return _hourly_aggregate(PRICE_POINT, minutes.timestamps, minutes.closes, start_ms, end_ms, initial)


def build_tables(trades: Iterable[Mapping[str, list]], hourly: Iterable[Mapping[str, np.ndarray]],
                 metrics: Iterable[Mapping[str, list]]) -> SegmentTables:
    def concat(parts, schema: pa.Schema) -> pa.Table:
        parts = list(parts)
        if not parts:
            return schema.empty_table()
        columns = {}
        for name in schema.names:
            values = [part[name] for part in parts]
            columns[name] = (np.concatenate(values) if isinstance(values[0], np.ndarray)
                              else list(itertools.chain.from_iterable(values)))
        return pa.Table.from_pydict(columns, schema=schema)
    return SegmentTables(concat(trades, TRADE_SCHEMA), concat(hourly, HOURLY_SCHEMA), concat(metrics, METRICS_SCHEMA))


def parquet_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd", use_dictionary=False, write_statistics=True)
    return sink.getvalue().to_pybytes()


def _write_once(path: Path, data: bytes, replace: bool = False) -> str:
    """Immutable file: a flushed temporary sibling hard-linked into place. Identical bytes already in place are
    accepted (rerun after a crash). Different bytes are an error unless `replace` (the caller knows the file
    belongs to no registered segment)."""
    digest = hashlib.sha256(data).hexdigest()
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
        return digest
    if path.exists() and not replace:
        raise TradeStoreError(f"segment file exists with different content: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise TradeStoreError(f"segment file exists with different content: {path}") from None
    finally:
        temporary.unlink(missing_ok=True)
    return digest


# -- store --------------------------------------------------------------------------


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class SegmentKey:
    exchange: str
    symbol: str
    source: str           # "simulator" | "tester"
    start: date
    end: date
    engine: str

    @property
    def file_name(self) -> str:
        return f"{self.source}_{self.start.isoformat()}_{self.end.isoformat()}_{self.engine}.parquet"

    @property
    def segment_id(self) -> str:
        return f"{self.exchange}/{self.symbol}/{self.file_name[: -len('.parquet')]}"


def write_segment_files(root: Path, key: SegmentKey, tables: SegmentTables, *, settings: RunSettings,
                        tick_size: float, qty_step: float, replace_unregistered: bool = False) -> dict:
    """Write the three files of a segment under the store root (no catalog access, safe in worker processes)
    and return its catalog record for `TradeStore.register`."""
    record = {"segment_id": key.segment_id, "exchange": key.exchange, "symbol": key.symbol, "source": key.source,
              "start_ms": _ms(key.start), "end_ms": _ms(key.end), "engine": key.engine,
              "settings_json": json.dumps(asdict(settings), sort_keys=True), "tick_size": tick_size,
              "qty_step": qty_step}
    for kind in FILE_KINDS:
        relative = Path(kind) / key.exchange / key.symbol / key.file_name
        record[f"{kind}_path"] = relative.as_posix()
        record[f"{kind}_sha256"] = _write_once(Path(root) / relative, parquet_bytes(getattr(tables, kind)),
                                               replace_unregistered)
    record["points"] = tables.metrics.num_rows
    record["trades"] = tables.trades.num_rows
    return record


class TradeStore:
    """Store root with segment files and `catalog.duckdb`; only one process writes the catalog."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.root / "catalog.duckdb"
        with self.connect() as con:
            con.execute(CATALOG_DDL)

    @classmethod
    def reader(cls, root: Path) -> "TradeStore":
        """The store opened for reading only: no folder or catalog is created and no DDL runs, so no writable
        connection is opened (the Panel reads the catalog while its threads hold read-only connections and a CLI
        child process writes it). Only the read methods may be used."""
        store = cls.__new__(cls)
        store.root = Path(root)
        store.catalog_path = store.root / "catalog.duckdb"
        if not store.catalog_path.is_file():
            raise TradeStoreError("в хранилище ещё нет каталога")
        return store

    def connect(self, read_only: bool = False, *, wait_s: float = CONNECT_WAIT_S) -> duckdb.DuckDBPyConnection:
        """Catalog connection; a file lock held by another process (a reader such as the Panel status, or the
        writing CLI) is waited for instead of failing."""
        deadline = time.monotonic() + wait_s
        while True:
            try:
                return duckdb.connect(str(self.catalog_path), read_only=read_only)
            except duckdb.IOException:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)

    # files (any process)

    def write_files(self, key: SegmentKey, tables: SegmentTables, *, settings: RunSettings, tick_size: float,
                    qty_step: float) -> dict:
        return write_segment_files(self.root, key, tables, settings=settings, tick_size=tick_size, qty_step=qty_step)

    def _read(self, segment: Mapping, kind: str) -> pa.Table:
        data = (self.root / segment[f"{kind}_path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != segment[f"{kind}_sha256"]:
            raise TradeStoreError(f"{kind} file hash mismatch: {segment['segment_id']}")
        return pq.read_table(pa.BufferReader(data))

    def read_trades(self, segment: Mapping) -> pa.Table:
        return self._read(segment, "trades")

    def read_hourly(self, segment: Mapping) -> pa.Table:
        return self._read(segment, "hourly")

    def read_metrics(self, segment: Mapping) -> pa.Table:
        return self._read(segment, "metrics")

    # catalog (main process)

    def register_points(self, pids: Iterable[int]) -> None:
        rows = [(pid, SIDE_LONG, *point_params(pid), "SMA", "ohlc4") for pid in pids]
        with self.connect() as con:
            con.executemany("insert or ignore into points values (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def statuses(self, exchange: str, symbol: str) -> dict[str, str]:
        """segment id -> status ('active' / 'superseded') of every registered segment of the pair."""
        with self.connect(read_only=True) as con:
            rows = con.execute("select segment_id, status from segments where exchange = ? and symbol = ?",
                               [exchange, symbol]).fetchall()
        return dict(rows)

    def _supersede_overlaps(self, con: duckdb.DuckDBPyConnection, segment_id: str, exchange: str, symbol: str,
                            source: str, start_ms: int, end_ms: int) -> None:
        con.execute("update segments set status = 'superseded', superseded_by = ? where exchange = ? and "
                    "symbol = ? and source = ? and status = 'active' and segment_id <> ? and start_ms < ? and end_ms > ?",
                    [segment_id, exchange, symbol, source, segment_id, end_ms, start_ms])

    def reactivate(self, segment_id: str) -> None:
        """Make a superseded segment active again (its span is planned again); its files must be intact."""
        with self.connect(read_only=True) as con:
            cursor = con.execute("select * from segments where segment_id = ?", [segment_id])
            names = [column[0] for column in cursor.description]
            row = cursor.fetchone()
        if row is None:
            raise TradeStoreError(f"segment not registered: {segment_id}")
        segment = dict(zip(names, row))
        for kind in FILE_KINDS:
            self._read(segment, kind)
        with self.connect() as con:
            con.execute("begin")
            self._supersede_overlaps(con, segment_id, segment["exchange"], segment["symbol"], segment["source"],
                                     segment["start_ms"], segment["end_ms"])
            con.execute("update segments set status = 'active', superseded_by = null where segment_id = ?", [segment_id])
            con.execute("commit")

    def register(self, record: Mapping, *, replace_superseded: bool = False) -> None:
        """Register a written segment; active segments of the same pair and source overlapping it are superseded.

        `replace_superseded`: the id may already be registered as superseded (its files were damaged and the
        segment was simulated again); that row is replaced by the new record."""
        columns = ("segment_id", "exchange", "symbol", "source", "start_ms", "end_ms", "engine", "settings_json",
                   "tick_size", "qty_step", "trades_path", "trades_sha256", "hourly_path", "hourly_sha256",
                   "metrics_path", "metrics_sha256", "points", "trades")
        with self.connect() as con:
            existing = con.execute("select status from segments where segment_id = ?", [record["segment_id"]]).fetchone()
            if existing is not None and not (replace_superseded and existing[0] == "superseded"):
                raise TradeStoreError(f"segment already registered: {record['segment_id']}")
            con.execute("begin")
            if existing is not None:
                con.execute("delete from segments where segment_id = ?", [record["segment_id"]])
            self._supersede_overlaps(con, record["segment_id"], record["exchange"], record["symbol"], record["source"],
                                     record["start_ms"], record["end_ms"])
            con.execute(f"insert into segments ({', '.join(columns)}, status, created_utc) values "
                        f"({', '.join('?' * len(columns))}, 'active', ?)", [record[name] for name in columns] + [utc_now()])
            con.execute("commit")

    def segments(self, symbol: str, exchange: str = "bybit", source: str | None = None,
                 include_superseded: bool = False) -> list[dict]:
        query = "select * from segments where symbol = ? and exchange = ?"
        args: list = [symbol, exchange]
        if not include_superseded:
            query += " and status = 'active'"
        if source:
            query += " and source = ?"
            args.append(source)
        with self.connect(read_only=True) as con:
            cursor = con.execute(query + " order by start_ms, segment_id", args)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def start_run(self, run_id: str, *, engine: str, settings: RunSettings, end_day: date, args: Mapping) -> None:
        with self.connect() as con:
            con.execute("insert into runs values (?, ?, null, ?, ?, ?, ?, 0, 0, 0)",
                        [run_id, utc_now(), engine, json.dumps(asdict(settings), sort_keys=True), end_day.isoformat(),
                         json.dumps(args, sort_keys=True, default=str)])

    def finish_run(self, run_id: str, *, written: int, skipped: int, failed: int) -> None:
        with self.connect() as con:
            con.execute("update runs set finished_utc = ?, written = ?, skipped = ?, failed = ? where run_id = ?",
                        [utc_now(), written, skipped, failed, run_id])


__all__ = [
    "FILE_KINDS", "HOURLY_SCHEMA", "METRICS_SCHEMA", "PRICE_POINT", "SegmentKey", "SegmentTables", "TRADE_SCHEMA",
    "TradeStore", "TradeStoreError", "build_tables", "hourly_rows", "metrics_columns", "parquet_bytes", "point_id",
    "point_params", "price_rows", "trade_columns", "write_segment_files",
]
