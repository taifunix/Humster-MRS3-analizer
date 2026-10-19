"""The minute data layer of both exchanges and the BingX minute verification (Panel stage 5a).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 5a contract»; rules in
docs/specs/2026-10-18-pair-registry.md, «Minute data layer» and «BingX minute verification». Every step that needs
candles calls `ensure_minute_days`: Bybit days come from the official trade archive (complete by construction);
BingX days come from the klines API and are accepted only when complete (`OK`) or when the hourly klines prove the
missing minutes had no trades (`VERIFIED_GAPS`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

import duckdb

from ..bingx_downloader import (
    DAY_MS, MINUTE_MS, BingXClient, BingXDownloadError, _day_start_ms, _first_minute_ms, _rows_from_klines, _text,
    last_available_day, planned_days, validate_day_file, write_day_file,
)
from ..minute_data import ensure_days
from ..tester_candles import HEADER, CandleDayError, read_day
from .store import RegistryError, RegistrySettings

ACCEPTED = ("OK", "VERIFIED_GAPS")
SUSPECT = "SUSPECT"
REFETCHES = 3
HOUR_MS = 3_600_000
TOLERANCE = Decimal("1e-9")
# BingX hourly volumes are not the exact sum of their minutes: +0.048 % at most over 864 hours of KOPN, complete hours
# included (measured 2026-10-19, ADR-0082); prices always match. An hour may exceed its minutes by this share.
VOLUME_NOISE = Decimal("0.001")
RECORD_COLUMNS = ("exchange", "symbol", "day", "candles", "expected_minutes", "zero_volume_minutes", "missing_minutes",
                  "gap_ranges", "differing_hours", "status", "verified_ms", "file_sha256")


@dataclass(slots=True)
class MinuteDays:
    """Accepted days, refused days with the reason, and the new verification records (BingX only)."""

    accepted: list[date] = field(default_factory=list)
    refused: dict[date, str] = field(default_factory=dict)
    records: list[dict] = field(default_factory=list)


# -- one BingX day ------------------------------------------------------------------------------------------------

def day_path(data_root: Path, symbol: str, api_symbol: str, day: date) -> Path:
    """The downloader's file: `<root>/<api symbol>/<human ticker><day>_1m.parquet`."""
    return Path(data_root) / api_symbol / f"{symbol}{day.isoformat()}_1m.parquet"


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _file_rows(path: Path) -> list[list[str]]:
    columns = read_day(path.read_bytes()).to_pydict()
    return [[_text(columns[name][index]) for name in HEADER] for index in range(len(columns["timestamp"]))]


def _fetch(client, api_symbol: str, day: date) -> list[list[str]]:
    """The day's minutes from BingX (nothing is written here)."""
    start = _day_start_ms(day)
    return _rows_from_klines(client.klines(api_symbol, start, start + DAY_MS - 1), day)


def _valid_rows(path: Path, day: date, launch_ms: int) -> list[list[str]] | None:
    """The rows of a valid day file on disk; None when there is none or it is damaged."""
    if not path.is_file() or validate_day_file(path, day, launch_ms) is not None:
        return None
    try:
        return _file_rows(path)
    except (OSError, CandleDayError):
        return None


def _clock(minute_ms: int, day_start: int) -> str:
    minutes = (minute_ms - day_start) // MINUTE_MS
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def completeness(rows: Sequence[Sequence[str]], day: date, launch_ms: int) -> tuple[int, list[str], int]:
    """(expected minutes, gap ranges «HH:MM-HH:MM», zero-volume minutes) of a day's rows."""
    start = _day_start_ms(day)
    first = _first_minute_ms(day, launch_ms)
    expected = (start + DAY_MS - first) // MINUTE_MS
    present = {int(row[0]) for row in rows}
    gaps: list[str] = []
    gap_start = None
    for stamp in range(first, start + DAY_MS + MINUTE_MS, MINUTE_MS):
        missing = stamp < start + DAY_MS and stamp not in present
        if missing and gap_start is None:
            gap_start = stamp
        elif not missing and gap_start is not None:
            gaps.append(f"{_clock(gap_start, start)}-{_clock(stamp - MINUTE_MS, start)}")
            gap_start = None
    zero = sum(1 for row in rows if Decimal(row[5]) == 0)
    return expected, gaps, zero


def _near(left: Decimal, right: Decimal) -> bool:
    """Equal at a relative tolerance (two zeros are equal)."""
    return abs(left - right) <= TOLERANCE * max(abs(left), abs(right))


def differing_hours(rows: Sequence[Sequence[str]], hours: Sequence[Mapping], day: date, launch_ms: int) -> list[int]:
    """Hours whose candle is not the aggregate of the hour's minutes (ADR-0082): open, close, high and low equal; the
    hour's volume exceeds the sum of its minutes by at most VOLUME_NOISE of the hour (more volume in the minutes
    than in the hour is no lost trade); an hour without minutes has no candle or zero volume."""
    start = _day_start_ms(day)
    minutes: dict[int, list[Sequence[str]]] = {}
    for row in rows:
        minutes.setdefault((int(row[0]) - start) // HOUR_MS, []).append(row)
    candles = {(int(candle["time"]) - start) // HOUR_MS: candle for candle in hours
               if start <= int(candle.get("time", -1)) < start + DAY_MS}
    differ = []
    for hour in range((_first_minute_ms(day, launch_ms) - start) // HOUR_MS, 24):
        inside, candle = minutes.get(hour, []), candles.get(hour)
        try:
            if not inside:
                if candle is not None and Decimal(str(candle.get("volume", "0"))) != 0:
                    differ.append(hour)
                continue
            if candle is None:
                differ.append(hour)
                continue
            expected = (Decimal(inside[0][1]), Decimal(inside[-1][4]), max(Decimal(row[2]) for row in inside),
                        min(Decimal(row[3]) for row in inside))
            actual = tuple(Decimal(str(candle[name])) for name in ("open", "close", "high", "low"))
            minutes_volume = sum((Decimal(row[5]) for row in inside), Decimal(0))
            hour_volume = Decimal(str(candle["volume"]))
        except (KeyError, InvalidOperation, ValueError):
            differ.append(hour)
            continue
        prices_differ = not all(_near(left, right) for left, right in zip(expected, actual))
        if prices_differ or hour_volume - minutes_volume > VOLUME_NOISE * abs(hour_volume):
            differ.append(hour)
    return differ


def _verify_day(client, data_root: Path, symbol: str, api_symbol: str, launch_ms: int, day: date,
                known: Mapping | None, mode: str, now_ms: int) -> tuple[str | None, dict | None]:
    """(refusal reason or None, new record or None when the known record is reused or nothing was verified).

    The fetched minutes are a candidate: the file on disk changes only when the candidate is accepted, so a valid
    file is never replaced by a worse answer, and an empty answer never removes a valid file (only a damaged one).
    """
    path = day_path(data_root, symbol, api_symbol, day)
    if mode == "incremental" and known is not None and known["status"] in ACCEPTED \
            and known["file_sha256"] == _sha(path):
        return None, None
    on_disk = _valid_rows(path, day, launch_ms)
    # a day already on disk is verified, not downloaded again (incremental); overwrite downloads every day
    rows = on_disk if mode == "incremental" and on_disk is not None else _fetch(client, api_symbol, day)
    hours = None
    for attempt in range(REFETCHES + 1):
        expected, gaps, zero = completeness(rows, day, launch_ms)
        differ: list[int] = []
        if not gaps:
            status = "OK"
            break
        if hours is None:
            start = _day_start_ms(day)
            hours = client.klines(api_symbol, start, start + DAY_MS - 1, interval="1h")
        differ = differing_hours(rows, hours, day, launch_ms)
        if not differ:
            status = "VERIFIED_GAPS"
            break
        if attempt == REFETCHES:
            status = SUSPECT
            break
        rows = _fetch(client, api_symbol, day)
    if status in ACCEPTED:
        if rows and rows != on_disk:
            write_day_file(path, rows, day, launch_ms, replace=path.exists())
        elif not rows and on_disk:
            return "BingX returned no candles for a day with a valid file; the file is kept", None
        elif not rows:
            path.unlink(missing_ok=True)               # only a damaged file can be here: it never means «no trades»
    record = {"exchange": "bingx", "symbol": symbol, "day": day, "candles": len(rows), "expected_minutes": expected,
              "zero_volume_minutes": zero, "missing_minutes": expected - len(rows), "gap_ranges": json.dumps(gaps),
              "differing_hours": json.dumps(differ), "status": status, "verified_ms": now_ms,
              "file_sha256": _sha(path)}
    if status == SUSPECT:
        return f"SUSPECT: hours {', '.join(f'{hour:02d}' for hour in differ)} differ from the minutes", record
    return None, record


def verify_bingx_days(data_root: Path, symbol: str, api_symbol: str, launch_ms: int, first: date, last: date, *,
                      known: Mapping[date, Mapping], client=None, now: datetime | None = None,
                      mode: str = "incremental") -> MinuteDays:
    """Ensure and verify the BingX days of [first, last] without the registry (a process-pool worker can call it);
    days before the listing day or not yet available are left out."""
    now = now or datetime.now(timezone.utc)
    client = client or BingXClient()
    result = MinuteDays()
    for day in planned_days(first, last, launch_ms, last_available_day(now)):
        try:
            reason, record = _verify_day(client, Path(data_root), symbol, api_symbol, launch_ms, day, known.get(day),
                                         mode, int(now.timestamp() * 1000))
        except BingXDownloadError as error:
            result.refused[day] = f"download failed: {error}"
            continue
        except (OSError, CandleDayError) as error:
            result.refused[day] = f"download failed: {type(error).__name__}"
            continue
        except (ValueError, TypeError, KeyError, InvalidOperation) as error:   # a malformed klines answer
            result.refused[day] = f"unexpected klines answer: {type(error).__name__}"
            continue
        if record is not None:
            result.records.append(record)
        if reason is None:
            result.accepted.append(day)
        else:
            result.refused[day] = reason
    return result


# -- the registry ---------------------------------------------------------------------------------------------------

def known_records(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, first: date, last: date) -> dict[date, dict]:
    rows = con.execute(f"select {', '.join(RECORD_COLUMNS)} from minute_days where exchange = ? and symbol = ? "
                       "and day between ? and ?", [exchange, symbol, first, last]).fetchall()
    return {row[2]: dict(zip(RECORD_COLUMNS, row)) for row in rows}


def write_records(con: duckdb.DuckDBPyConnection, records: Sequence[Mapping]) -> None:
    """One row per (exchange, symbol, day), replaced by each verification."""
    if not records:
        return
    con.executemany(f"insert or replace into minute_days ({', '.join(RECORD_COLUMNS)}) values "
                    f"({', '.join('?' * len(RECORD_COLUMNS))})",
                    [[record[name] for name in RECORD_COLUMNS] for record in records])


def ensure_bingx_days_shared(settings: RegistrySettings, symbol: str, first: date, last: date, *, client=None,
                             now: datetime | None = None, api_symbol: str | None = None) -> MinuteDays:
    """`ensure_minute_days` for a BingX pair from a process that does not own the registry (the tester executor).

    The registry is opened only for this call. While another process holds it: with the records read read-only
    when that is possible, else (a writer holds it: DuckDB refuses even readers) from `api_symbol` alone with no
    known records, so every day is verified again from disk and the API. New records are written only in the first
    case (the next call verifies again)."""
    from .store import DATABASE, connect

    try:
        con = connect(settings.root)
    except duckdb.Error:
        con = None
    if con is not None:
        try:
            return ensure_minute_days(con, settings, "bingx", symbol, first, last, client=client, now=now)
        finally:
            con.close()
    if settings.bingx_data_root is None:
        raise RegistryError("the BingX minute data folder is unknown: set tester_runner.bot_root")
    try:
        reader = duckdb.connect(str(settings.root / DATABASE), read_only=True)
    except duckdb.Error:
        if not api_symbol:
            raise RegistryError("the pair registry is held by another process and cannot be read") from None
        # the listing minute is unknown here: with 0 a listing day inside the warm-up expects 1,440 minutes and its
        # pre-listing hours are accepted as VERIFIED_GAPS by the hourly check (no candle there); days after it are
        # unaffected
        return verify_bingx_days(settings.bingx_data_root, symbol, api_symbol, 0, first, last, known={},
                                 client=client, now=now, mode=settings.bingx_download_mode)
    try:
        pair = reader.execute("select api_symbol, listing_ms from pairs where exchange = 'bingx' and symbol = ?",
                              [symbol]).fetchone()
        if pair is None or not pair[0]:
            raise RegistryError(f"bingx: {symbol} is not in the registry")
        known = known_records(reader, "bingx", symbol, first, last)
    finally:
        reader.close()
    return verify_bingx_days(settings.bingx_data_root, symbol, pair[0], int(pair[1] or 0), first, last, known=known,
                             client=client, now=now, mode=settings.bingx_download_mode)


def ensure_minute_days(con: duckdb.DuckDBPyConnection, settings: RegistrySettings, exchange: str, symbol: str,
                       first: date, last: date, *, client=None, now: datetime | None = None,
                       fetch_day=None) -> MinuteDays:
    """Make sure the minute days of [first, last] exist and are accepted, on either exchange (Bybit downloads use
    the shared `duckdb_import.workers` limit)."""
    if exchange == "bybit":
        if settings.liquidity_data_root is None:
            raise RegistryError("the Bybit minute data folder is unknown: set tester_runner.bot_root")
        options = {"workers": settings.workers} if fetch_day is None else {"workers": settings.workers,
                                                                             "fetch_day": fetch_day}
        days = ensure_days(settings.liquidity_data_root, symbol, first, last, **options)
        return MinuteDays(sorted([*days["existing"], *days["created"]]),
                          {day: "no Bybit trade archive for the day" for day in days["failed"]})
    if exchange != "bingx":
        raise RegistryError(f"unknown exchange: {exchange}")
    if settings.bingx_data_root is None:
        raise RegistryError("the BingX minute data folder is unknown: set tester_runner.bot_root")
    pair = con.execute("select api_symbol, listing_ms from pairs where exchange = 'bingx' and symbol = ?",
                       [symbol]).fetchone()
    if pair is None or not pair[0]:
        raise RegistryError(f"bingx: {symbol} is not in the registry")
    result = verify_bingx_days(settings.bingx_data_root, symbol, pair[0], int(pair[1] or 0), first, last,
                               known=known_records(con, "bingx", symbol, first, last), client=client, now=now,
                               mode=settings.bingx_download_mode)
    write_records(con, result.records)
    return result
