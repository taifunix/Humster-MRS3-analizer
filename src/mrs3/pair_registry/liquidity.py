"""Weekday liquidity, LOT_MODEL_V2 (docs/decisions/0079-lot-model-v2-weekday-liquidity.md).

Same formulas as LOT_MODEL_V1 (`portfolio.minute_capacity._lot_model_capacity`), but the window is the last N
weekdays (Monday-Friday UTC) instead of seven calendar days. Contract: docs/specs/2026-10-18-pair-registry.md,
«Stage 2 contract».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

import duckdb

from ..portfolio.minute_capacity import (
    MinuteCapacityError, MinuteCapacityResult, MinuteLiquidityFeatures, _day_path, _day_start_ms, _read_day_bytes,
    _type7_q25,
)
from ..portfolio.position_sizing import _floor_step
from .refresh import write_journal
from .store import RegistryError, RegistrySettings

MODEL = "LOT_MODEL_V2"
UNAVAILABLE = "LIQUIDITY_MODEL_WINDOW_UNAVAILABLE"
BINS_PER_DAY = 96


@dataclass(frozen=True, slots=True)
class DayFacts:
    day: date
    weekend: bool
    turnover: Decimal
    positive_minutes: int
    active_bins: int
    minute_turnovers: tuple[Decimal, ...]
    file_sha256: str


def day_facts(path: Path, day: date) -> DayFacts:
    """Facts of one tester minute day file (read with the LOT_MODEL_V1 reader)."""
    data = Path(path).read_bytes()
    rows = _read_day_bytes(data, Path(path), day)
    start = _day_start_ms(day)
    positive = [turnover for _stamp, turnover in rows if turnover > 0]
    bins = {(stamp - start) // 60_000 // 15 for stamp, turnover in rows if turnover > 0}
    with localcontext() as context:
        context.prec = 28
        total = sum(positive, Decimal(0))
    return DayFacts(day, day.weekday() >= 5, total, len(positive), len(bins), tuple(positive),
                    hashlib.sha256(data).hexdigest())


def bingx_day_facts(path: Path, day: date) -> DayFacts:
    """Facts of one BingX minute day file (Panel stage 5d). BingX files carry no trade count and no buy/sell split,
    so the V1 reader would refuse them; the turnover is `close × volume` (volume in the base asset, checked live)."""
    from ..bingx_downloader import _text
    from ..tester_candles import CandleDayError, read_day

    data = Path(path).read_bytes()
    try:
        columns = read_day(data).to_pydict()
    except CandleDayError as error:
        raise MinuteCapacityError(f"unreadable BingX day: {error}") from None
    start, previous, positive, bins = _day_start_ms(day), -1, [], set()
    for index, stamp in enumerate(columns["timestamp"]):
        stamp = int(stamp)
        if stamp % 60_000 or not start <= stamp < start + 86_400_000 or stamp <= previous:
            raise MinuteCapacityError("BingX minute timestamps are not unique, ordered, aligned and in-day")
        previous = stamp
        try:
            opened, high, low, close, volume = (Decimal(_text(columns[name][index]))
                                                for name in ("open", "high", "low", "close", "volume"))
        except (InvalidOperation, ValueError, TypeError):
            raise MinuteCapacityError(f"unreadable BingX minute row {index + 1}") from None
        if not all(value.is_finite() for value in (opened, high, low, close, volume)) or \
                min(opened, high, low, close) <= 0 or volume < 0 or high < max(opened, low, close) \
                or low > min(opened, close):
            raise MinuteCapacityError(f"inconsistent BingX minute row {index + 1}")
        with localcontext() as context:
            context.prec = 28
            context.rounding = ROUND_HALF_EVEN
            turnover = close * volume
        if turnover > 0:
            positive.append(turnover)
            bins.add((stamp - start) // 60_000 // 15)
    with localcontext() as context:
        context.prec = 28
        total = sum(positive, Decimal(0))
    return DayFacts(day, day.weekday() >= 5, total, len(positive), len(bins), tuple(positive),
                    hashlib.sha256(data).hexdigest())


def window_span(today: date, weekdays: int) -> tuple[date, date, date]:
    """(first day, last weekday = window end, last day) of the window that ends on the last weekday before today."""
    last_day = today - timedelta(days=1)
    end = _last_weekday(last_day)
    _none, window = select_window({}, end, weekdays)
    return min(window), end, last_day


def ensure_bingx_window(con: duckdb.DuckDBPyConnection, settings: RegistrySettings, first_today: date,
                        today: date, *, client=None, now: datetime | None = None) -> dict[str, int]:
    """Download and verify the window days of every TRADING BingX pair (BingX has no market collector): from the
    window of `first_today` to the day before `today`. One pair failing never stops the others."""
    from .minute_days import ensure_minute_days

    if settings.bingx_data_root is None:
        raise RegistryError("the BingX minute data folder is unknown: set tester_runner.bot_root")
    first, _end, _last = window_span(first_today, settings.liquidity_window_weekdays)
    last_day = today - timedelta(days=1)
    symbols = [row[0] for row in con.execute("select symbol from pairs where exchange = 'bingx' and status = 'TRADING' "
                                             "and api_symbol is not null order by symbol").fetchall()]
    counts: dict = {"pairs": len(symbols), "accepted": 0, "refused": 0, "failed_pairs": 0, "failed": {}}
    for symbol in symbols:
        try:
            days = ensure_minute_days(con, settings, "bingx", symbol, first, last_day, client=client, now=now)
        except (RegistryError, OSError, duckdb.Error) as error:
            counts["failed_pairs"] += 1
            counts["failed"][symbol] = type(error).__name__
            continue
        counts["accepted"] += len(days.accepted)
        counts["refused"] += len(days.refused)
    return counts


def select_window(available: Mapping[date, object], end_day: date, weekdays: int) -> tuple[list[date], list[date]]:
    """The last `weekdays` Monday-Friday days ending on `end_day`: (days with a file, weekdays without one)."""
    chosen, missing = [], []
    day, counted = end_day, 0
    while counted < weekdays:
        if day.weekday() < 5:
            counted += 1
            (chosen if day in available else missing).append(day)
        day -= timedelta(days=1)
    return sorted(chosen), sorted(missing)


def window_row(days: Sequence[DayFacts], *, k: Decimal, step: Decimal) -> dict[str, object]:
    """V25 / A15 / L / base lot of a window of days (all days given count; the caller picks the weekdays)."""
    positive = [value for facts in days for value in facts.minute_turnovers]
    if not positive:
        raise RegistryError("no positive minutes")
    try:
        v25 = _type7_q25(positive)
    except MinuteCapacityError as error:
        raise RegistryError("no positive minutes") from error
    total_bins = len(days) * BINS_PER_DAY
    active = sum(facts.active_bins for facts in days)
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        a15 = Decimal(active) / Decimal(total_bins)
        level = v25 * a15
    lot = _floor_step(k * level, step)              # exactly the V1 floor (position_sizing._floor_step)
    files = [[facts.day.isoformat(), facts.file_sha256] for facts in days]
    return {
        "model": MODEL, "window_days": [facts.day.isoformat() for facts in days], "window_weekdays": len(days),
        "positive_minutes": len(positive), "active_bins": active, "total_bins": total_bins,
        "v25": format(v25, "f"), "a15": format(a15, "f"), "l": format(level, "f"), "k": format(k, "f"),
        "step": format(step, "f"), "base_lot": format(lot, "f"), "window_files": files,
        "inputs_sha256": hashlib.sha256(json.dumps(files, separators=(",", ":")).encode()).hexdigest(),
    }


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _last_weekday(day: date) -> date:
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def refresh_liquidity(con: duckdb.DuckDBPyConnection, exchange: str, today: date,
                      settings: RegistrySettings, *, now: datetime | None = None) -> dict[str, int]:
    """One LOT_MODEL_V2 window per TRADING pair, ending on the last weekday before `today`, plus the day rows from
    the window's first day to the day before `today`. A window weekday without a readable file makes the window
    unavailable (fail closed: stale or missing minute data never yields READY). Journaled as step `liquidity`."""
    started = _now_ms()
    try:
        counts = _refresh_liquidity(con, exchange, today, settings, started, now)
    except Exception as error:
        try:
            write_journal(con, "liquidity", exchange, "FAILED", None, started, f"{type(error).__name__}: {error}")
        except Exception:
            pass
        raise
    write_journal(con, "liquidity", exchange, "OK", counts, started, None)
    return counts


def _refresh_liquidity(con: duckdb.DuckDBPyConnection, exchange: str, today: date, settings: RegistrySettings,
                       computed: int, now: datetime | None = None) -> dict[str, int]:
    if exchange not in ("bybit", "bingx"):
        raise RegistryError(f"liquidity: unknown exchange {exchange}")
    root = settings.liquidity_data_root if exchange == "bybit" else settings.bingx_data_root
    if root is None:
        raise RegistryError("liquidity data folder is not configured (tester_runner.bot_root)")
    weekdays = settings.liquidity_window_weekdays
    first, end, last_day = window_span(today, weekdays)
    if exchange == "bingx":
        from ..bingx_downloader import last_available_day

        available = last_available_day(now or datetime.now(timezone.utc))
        if end > available:
            # BingX days appear about 9 h after midnight UTC: an early run keeps the last computed rows instead of
            # writing every window as UNAVAILABLE (Bybit's collector writes yesterday's file at once)
            return {"pairs": 0, "ready": 0, "unavailable": 0, "day_rows": 0, "unreadable_days": 0,
                    "not_yet_available": end.isoformat()}
    span = [first + timedelta(days=offset) for offset in range((last_day - first).days + 1)]
    pairs = con.execute("select symbol, api_symbol from pairs where exchange = ? and status = 'TRADING' "
                        "order by symbol", [exchange]).fetchall()
    symbols = [symbol for symbol, _api in pairs]
    counts = {"pairs": len(symbols), "ready": 0, "unavailable": 0, "day_rows": 0, "unreadable_days": 0}
    for symbol, api_symbol in pairs:
        facts: dict[date, DayFacts] = {}
        if exchange == "bingx":
            counts["unreadable_days"] += _bingx_facts(con, Path(root), symbol, api_symbol, span, facts)
        for day in span if exchange == "bybit" else ():
            path = _day_path(Path(root), symbol, day)
            if not path.is_file():
                continue
            try:
                facts[day] = day_facts(path, day)
            except (OSError, MinuteCapacityError):
                counts["unreadable_days"] += 1          # an unreadable file counts as a missing day
        chosen, missing = select_window(facts, end, weekdays)
        row: dict[str, object] | None = None
        reason = "missing weekday files" if missing else None
        if not missing:
            try:
                row = window_row([facts[day] for day in chosen], k=settings.liquidity_k,
                                 step=settings.liquidity_round_down)
            except RegistryError as error:
                reason = str(error)
        con.begin()
        try:
            con.execute("delete from liquidity_days where exchange = ? and symbol = ? and day between ? and ?",
                        [exchange, symbol, first, last_day])          # the day table follows the files
            for item in facts.values():
                con.execute("insert into liquidity_days values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [exchange, symbol, item.day, item.weekend, format(item.turnover, "f"),
                             item.positive_minutes, item.active_bins, item.file_sha256, computed])
            con.execute("delete from liquidity where exchange = ? and symbol = ? and as_of_day = ?",
                        [exchange, symbol, end])
            if row is None:
                con.execute("insert into liquidity (exchange, symbol, as_of_day, model, status, window_weekdays, "
                            "window_days, missing_days, computed_ms, reason) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [exchange, symbol, end, MODEL, UNAVAILABLE, weekdays,
                             json.dumps([day.isoformat() for day in chosen]),
                             json.dumps([day.isoformat() for day in missing]), computed, reason])
                counts["unavailable"] += 1
            else:
                con.execute("insert into liquidity (exchange, symbol, as_of_day, model, status, window_weekdays, "
                            "window_days, missing_days, positive_minutes, active_bins, total_bins, v25, a15, l, k, "
                            "step, base_lot, computed_ms, reason, window_files, inputs_sha256) "
                            "values (?, ?, ?, ?, 'READY', ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                            [exchange, symbol, end, MODEL, row["window_weekdays"], json.dumps(row["window_days"]),
                             row["positive_minutes"], row["active_bins"], row["total_bins"], row["v25"], row["a15"],
                             row["l"], row["k"], row["step"], row["base_lot"], computed,
                             json.dumps(row["window_files"]), row["inputs_sha256"]])
                counts["ready"] += 1
            con.commit()
        except BaseException:
            con.rollback()
            raise
        counts["day_rows"] += len(facts)
    return counts


def _bingx_facts(con: duckdb.DuckDBPyConnection, root: Path, symbol: str, api_symbol: str | None,
                 span: Sequence[date], facts: dict[date, DayFacts]) -> int:
    """The verified BingX days of the span (Panel stage 5d): a day counts only while its file is the verified one; a
    verified day without a file had no trades (zero turnover). Returns the number of unreadable files."""
    from .minute_days import ACCEPTED, day_path, known_records

    if not api_symbol or not span:
        return 0
    unreadable = 0
    known = known_records(con, "bingx", symbol, span[0], span[-1])
    for day in span:
        record = known.get(day)
        if record is None or record["status"] not in ACCEPTED:
            continue
        path = day_path(root, symbol, api_symbol, day)
        data = path.read_bytes() if path.is_file() else None
        if (hashlib.sha256(data).hexdigest() if data is not None else None) != record["file_sha256"]:
            continue                                   # changed since the verification: missing until verified
        if data is None:
            facts[day] = DayFacts(day, day.weekday() >= 5, Decimal(0), 0, 0, (), "none")
            continue
        try:
            facts[day] = bingx_day_facts(path, day)
        except (OSError, MinuteCapacityError):
            unreadable += 1
    return unreadable


_COLUMNS = ("as_of_day", "model", "status", "window_weekdays", "window_days", "missing_days", "reason",
            "positive_minutes", "active_bins", "total_bins", "v25", "a15", "l", "k", "step", "base_lot",
            "window_files", "inputs_sha256")


def latest_liquidity(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str) -> dict[str, object] | None:
    """The newest liquidity row of a pair, whatever its status (for views)."""
    row = con.execute(f"select {', '.join(_COLUMNS)} from liquidity where exchange = ? and symbol = ? "
                      "order by as_of_day desc limit 1", [exchange, symbol]).fetchone()
    if row is None:
        return None
    result = dict(zip(_COLUMNS, row))
    result["as_of_day"] = result["as_of_day"].isoformat()
    for key in ("window_days", "missing_days", "window_files"):
        result[key] = json.loads(result[key]) if result[key] else []
    return result


def last_weekday_before(day: date) -> date:
    """The last Monday-Friday day before `day`: where a LOT_MODEL_V2 window computed on `day` ends."""
    return _last_weekday(day - timedelta(days=1))


def registry_capacity(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, *, anchor_created_at: datetime,
                      round_down_usdt: Decimal | int | str | None = None) -> MinuteCapacityResult:
    """LOT_MODEL_V2 capacity of a pair for an optimizer Campaign frozen at `anchor_created_at` (registry stage 4).

    The window of a TRADING pair ending on the last weekday before the anchor's UTC day is used when it is READY and
    carries `inputs_sha256` (`liquidity_on`), so a Campaign retried after a later sync still finds its window; anything
    else is `LIQUIDITY_MODEL_WINDOW_UNAVAILABLE` (fail closed: the registry needs «Синхронизировать» once the previous
    weekday's minute file exists). No minute file is read and no publication lag applies. `round_down_usdt` is the
    Campaign's own floor (default: the registry step)."""
    if (not isinstance(anchor_created_at, datetime) or anchor_created_at.tzinfo is None
            or anchor_created_at.utcoffset() is None):
        raise MinuteCapacityError("LIQUIDITY_MODEL_ANCHOR_UNAVAILABLE")
    anchor = anchor_created_at.astimezone(timezone.utc)
    symbol = str(symbol).strip().upper()
    expected = last_weekday_before(anchor.date())
    status = con.execute("select status from pairs where exchange = ? and symbol = ?", [exchange, symbol]).fetchone()
    row = liquidity_on(con, exchange, symbol, expected)
    if status is None or status[0] != "TRADING" or row is None:
        raise MinuteCapacityError(UNAVAILABLE)
    try:
        days = tuple(date.fromisoformat(str(day)) for day in row["window_days"])
        files = tuple((str(day), str(digest)) for day, digest in row["window_files"])
        v25, a15 = Decimal(str(row["v25"])), Decimal(str(row["a15"]))
        step = Decimal(str(row["step"] if round_down_usdt is None else round_down_usdt))
    except (TypeError, ValueError, ArithmeticError) as error:
        raise MinuteCapacityError(UNAVAILABLE) from error
    if (not days or days[-1] != expected or len(files) != len(days)
            or [day for day, _digest in files] != [day.isoformat() for day in days]
            or type(row["active_bins"]) is not int):
        raise MinuteCapacityError(UNAVAILABLE)
    payload = {"model": MODEL, "exchange": exchange, "symbol": symbol, "window": [day.isoformat() for day in days],
               "file_hashes": files, "positive_minute_count": row["positive_minutes"], "v25": row["v25"],
               "active_15m_bins": row["active_bins"], "a15": row["a15"], "inputs_sha256": row["inputs_sha256"]}
    feature_digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    features = MinuteLiquidityFeatures(v25, row["active_bins"], a15, files, feature_digest, days, None,
                                       anchor.isoformat())
    digest = hashlib.sha256(json.dumps({**payload, "round_down_usdt": format(step, "f")}, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    return MinuteCapacityResult("READY", symbol, days[0], days[-1], None, step, None, None, Decimal(0), MODEL, (),
                                tuple(day for day, _digest in files), digest, features)


def liquidity_on(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, as_of_day: date) -> dict[str, object] | None:
    """The READY window of a pair ending on `as_of_day` with its `inputs_sha256`, else None (the base-lot export's
    dated history cells, registry stage 4)."""
    row = con.execute(f"select {', '.join(_COLUMNS)} from liquidity where exchange = ? and symbol = ? and as_of_day = ?",
                      [exchange, str(symbol).strip().upper(), as_of_day]).fetchone()
    if row is None:
        return None
    result = dict(zip(_COLUMNS, row))
    if result["status"] != "READY" or not result["inputs_sha256"] or result["model"] != MODEL:
        return None
    result["as_of_day"] = result["as_of_day"].isoformat()
    for key in ("window_days", "missing_days", "window_files"):
        result[key] = json.loads(result[key]) if result[key] else []
    return result


def read_liquidity(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str) -> dict[str, object] | None:
    """What the optimizer and the base-lot export read: the newest window of a TRADING pair if it is READY, else None
    (fail closed: a newer UNAVAILABLE window hides an older READY one)."""
    status = con.execute("select status from pairs where exchange = ? and symbol = ?", [exchange, symbol]).fetchone()
    latest = latest_liquidity(con, exchange, symbol)
    if status is None or status[0] != "TRADING" or latest is None or latest["status"] != "READY":
        return None
    return latest
