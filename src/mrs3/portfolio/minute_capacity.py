"""Coarse full-position capacity from the tester's sparse Bybit minute CSVs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
import csv
import gzip
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

import httpx


HEADER = ("timestamp", "open", "high", "low", "close", "volume", "buy_volume", "sell_volume", "trades")
DAY_MINUTES = 1_440
WEEK_DAYS = 7
DAY_MS = 86_400_000
_WEEKDAYS = {name: index for index, name in enumerate(("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"))}


class MinuteCapacityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CapacityWindow:
    available_days: int
    clock_minutes: int
    observed_minutes: int
    zero_trade_minutes: int
    traded_minute_ratio: Decimal
    total_turnover: Decimal
    mean_minute_turnover: Decimal
    raw_cap_usdt: Decimal
    rounded_cap_usdt: Decimal


@dataclass(frozen=True, slots=True)
class MinuteCapacityResult:
    status: str
    symbol: str
    window_start: date
    window_end: date
    participation_pct: int
    round_down_usdt: Decimal
    calendar_7d: CapacityWindow
    weekday_5d: CapacityWindow
    position_cap_usdt: Decimal
    selected_basis: str
    missing_days: tuple[date, ...]
    source_files: tuple[str, ...]
    content_digest: str


@dataclass(frozen=True, slots=True)
class BackfillResult:
    created: tuple[date, ...] = ()
    existing: tuple[date, ...] = ()
    missing: tuple[date, ...] = ()
    failed: Mapping[date, str] = MappingProxyType({})


def _decimal(value: object, field: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (bool, float)):
        raise MinuteCapacityError(f"{field} must be an exact decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise MinuteCapacityError(f"{field} must be an exact decimal") from error
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise MinuteCapacityError(f"{field} has an invalid value")
    return result


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isalnum() or value != value.upper():
        raise MinuteCapacityError("symbol must be uppercase ASCII letters/digits")
    return value


def _day_path(root: Path, symbol: str, day: date) -> Path:
    return root / symbol / f"{symbol}{day.isoformat()}_1m.csv"


def _day_start_ms(day: date) -> int:
    return int(datetime.combine(day, time(), timezone.utc).timestamp() * 1000)


def _read_day(path: Path, symbol: str, day: date) -> tuple[tuple[int, Decimal], ...]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != HEADER:
                raise MinuteCapacityError(f"invalid minute CSV header: {path}")
            values: list[tuple[int, Decimal]] = []
            previous = -1
            start = _day_start_ms(day)
            for number, row in enumerate(reader, 2):
                if set(row) != set(HEADER) or None in row or any(value is None for value in row.values()):
                    raise MinuteCapacityError(f"invalid minute CSV row {number}: {path}")
                try:
                    stamp = int(row["timestamp"])
                    trades = int(row["trades"])
                except (TypeError, ValueError) as error:
                    raise MinuteCapacityError(f"invalid minute CSV integer row {number}: {path}") from error
                if str(stamp) != row["timestamp"] or str(trades) != row["trades"] or trades <= 0:
                    raise MinuteCapacityError(f"invalid minute CSV integer row {number}: {path}")
                if stamp % 60_000 or not start <= stamp < start + DAY_MS or stamp <= previous:
                    raise MinuteCapacityError(f"minute timestamps are not unique, ordered, aligned and in-day: {path}")
                previous = stamp
                prices = [_decimal(row[key], key, positive=True) for key in ("open", "high", "low", "close")]
                volume = _decimal(row["volume"], "volume", nonnegative=True)
                buy = _decimal(row["buy_volume"], "buy_volume", nonnegative=True)
                sell = _decimal(row["sell_volume"], "sell_volume", nonnegative=True)
                if volume != buy + sell or prices[1] < max(prices[0], prices[2], prices[3]) or prices[2] > min(prices[0], prices[1], prices[3]):
                    raise MinuteCapacityError(f"inconsistent minute CSV row {number}: {path}")
                values.append((stamp, prices[3] * volume))
            return tuple(values)
    except MinuteCapacityError:
        raise
    except (OSError, UnicodeError, csv.Error) as error:
        raise MinuteCapacityError(f"cannot read minute CSV: {path}") from error


def _week_minute(value: str) -> int:
    try:
        weekday, clock = value.strip().upper().split()
        hour_text, minute_text = clock.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, ValueError) as error:
        raise MinuteCapacityError("weekend boundary must be WEEKDAY HH:MM") from error
    if weekday not in _WEEKDAYS or not 0 <= hour < 24 or not 0 <= minute < 60:
        raise MinuteCapacityError("weekend boundary must be WEEKDAY HH:MM")
    return _WEEKDAYS[weekday] * DAY_MINUTES + hour * 60 + minute


def _is_weekend(stamp_ms: int, start: int, end: int) -> bool:
    instant = datetime.fromtimestamp(stamp_ms // 1000, timezone.utc)
    minute = instant.weekday() * DAY_MINUTES + instant.hour * 60 + instant.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def _window(days: list[tuple[date, tuple[tuple[int, Decimal], ...]]], participation: Decimal, step: Decimal, *, weekend: tuple[int, int] | None) -> CapacityWindow:
    clock = 0
    observed = 0
    available_days = 0
    turnover = Decimal(0)
    for day, rows in days:
        start = _day_start_ms(day)
        minute_indexes = range(DAY_MINUTES)
        if weekend is not None:
            minute_indexes = (index for index in minute_indexes if not _is_weekend(start + index * 60_000, *weekend))
        allowed = set(minute_indexes)
        available_days += bool(allowed)
        clock += len(allowed)
        selected = [value for stamp, value in rows if (stamp - start) // 60_000 in allowed]
        observed += len(selected)
        turnover += sum(selected, Decimal(0))
    with localcontext() as context:
        context.prec = 28
        mean = turnover / Decimal(clock) if clock else Decimal(0)
        raw = mean * participation / Decimal(100)
        rounded = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
        traded_ratio = Decimal(observed) / Decimal(clock) if clock else Decimal(0)
    return CapacityWindow(available_days, clock, observed, clock - observed, traded_ratio, turnover, mean, raw, rounded)


def calculate_minute_capacity(
    root: str | Path,
    symbol: str,
    *,
    end_date: date,
    participation_pct: int,
    round_down_usdt: Decimal | int | str = "50",
    weekend_start_utc: str = "SATURDAY 00:00",
    weekend_end_utc: str = "MONDAY 00:00",
    publication_lag_hours: int = 6,
    now: datetime | None = None,
) -> MinuteCapacityResult:
    symbol = _symbol(symbol)
    if isinstance(end_date, datetime) or not isinstance(end_date, date):
        raise MinuteCapacityError("end_date must be a date")
    if type(participation_pct) is not int or not 1 <= participation_pct <= 200:
        raise ValueError("participation_pct must be an integer from 1 through 200")
    if type(publication_lag_hours) is not int or not 0 <= publication_lag_hours <= 48:
        raise ValueError("publication_lag_hours must be an integer from 0 through 48")
    step = _decimal(round_down_usdt, "round_down_usdt", positive=True)
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise MinuteCapacityError("now must be timezone-aware")
    available_at = datetime.combine(end_date + timedelta(days=1), time(), timezone.utc) + timedelta(hours=publication_lag_hours)
    if now.astimezone(timezone.utc) < available_at:
        raise MinuteCapacityError("end_date has not passed the archive publication lag")
    weekend = (_week_minute(weekend_start_utc), _week_minute(weekend_end_utc))
    if weekend[0] == weekend[1]:
        raise MinuteCapacityError("weekend interval cannot cover zero or a full week")
    root = Path(root)
    requested = tuple(end_date - timedelta(days=offset) for offset in range(WEEK_DAYS - 1, -1, -1))
    present: list[tuple[date, tuple[tuple[int, Decimal], ...]]] = []
    missing: list[date] = []
    sources: list[str] = []
    for day in requested:
        path = _day_path(root, symbol, day)
        if not path.is_file():
            missing.append(day)
            continue
        present.append((day, _read_day(path, symbol, day)))
        sources.append(str(path.resolve()))
    if not present:
        raise MinuteCapacityError("no valid daily CSV is available for the requested window")
    calendar = _window(present, Decimal(participation_pct), step, weekend=None)
    weekdays = _window(present, Decimal(participation_pct), step, weekend=weekend)
    payload = {
        "symbol": symbol,
        "requested": [item.isoformat() for item in requested],
        "missing": [item.isoformat() for item in missing],
        "sources": [(name, hashlib.sha256(Path(name).read_bytes()).hexdigest()) for name in sources],
        "participation_pct": participation_pct,
        "round_down_usdt": format(step, "f"),
        "weekend": (weekend_start_utc, weekend_end_utc),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return MinuteCapacityResult("READY" if not missing else "PRELIMINARY", symbol, requested[0], requested[-1], participation_pct, step, calendar, weekdays, calendar.rounded_cap_usdt, "CALENDAR_7D", tuple(missing), tuple(sources), digest)


def _trade_timestamp(value: str) -> datetime:
    text = value.strip()
    try:
        numeric = Decimal(text)
    except InvalidOperation:
        numeric = None
    if numeric is not None and numeric.is_finite():
        try:
            microseconds = int(numeric * Decimal(1_000_000))
            return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=microseconds)
        except (OverflowError, ValueError) as error:
            raise MinuteCapacityError("archive trade timestamp is invalid") from error
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise MinuteCapacityError("archive trade timestamp is invalid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _aggregate_archive(data: bytes, symbol: str, day: date) -> bytes:
    try:
        text = gzip.decompress(data).decode("utf-8")
        reader = csv.DictReader(StringIO(text))
    except (OSError, UnicodeError, csv.Error) as error:
        raise MinuteCapacityError("Bybit trade archive is invalid") from error
    required = {"timestamp", "symbol", "side", "size", "price"}
    if not required.issubset(reader.fieldnames or ()):
        raise MinuteCapacityError("Bybit trade archive header is invalid")
    trades: list[tuple[datetime, str, Decimal, Decimal]] = []
    for row in reader:
        if row.get("symbol") != symbol or row.get("side") not in {"Buy", "Sell"}:
            raise MinuteCapacityError("Bybit trade archive identity is invalid")
        stamp = _trade_timestamp(row["timestamp"])
        if stamp.date() != day:
            raise MinuteCapacityError("Bybit trade archive contains an out-of-day trade")
        trades.append((stamp, row["side"], _decimal(row["size"], "size", positive=True), _decimal(row["price"], "price", positive=True)))
    trades.sort(key=lambda item: item[0])
    grouped: dict[int, list[tuple[datetime, str, Decimal, Decimal]]] = {}
    for item in trades:
        minute = int(item[0].timestamp() // 60 * 60_000)
        grouped.setdefault(minute, []).append(item)
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(HEADER)
    for stamp, items in grouped.items():
        prices = [item[3] for item in items]
        buy = sum((item[2] for item in items if item[1] == "Buy"), Decimal(0))
        sell = sum((item[2] for item in items if item[1] == "Sell"), Decimal(0))
        writer.writerow((stamp, prices[0], max(prices), min(prices), prices[-1], buy + sell, buy, sell, len(items)))
    return output.getvalue().encode("utf-8")


def fetch_bybit_trade_archive(symbol: str, day: date) -> bytes:
    """Download one official public Bybit linear-trade daily archive."""
    symbol = _symbol(symbol)
    if isinstance(day, datetime) or not isinstance(day, date):
        raise MinuteCapacityError("archive day must be a date")
    url = f"https://public.bybit.com/trading/{symbol}/{symbol}{day.isoformat()}.csv.gz"
    try:
        response = httpx.get(url, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        return response.content
    except httpx.HTTPError as error:
        raise MinuteCapacityError(f"cannot download Bybit trade archive for {symbol} {day.isoformat()}") from error


def backfill_missing_days(
    root: str | Path,
    symbol: str,
    days: Iterable[date],
    *,
    fetch_day: Callable[[str, date], bytes],
    enabled: bool,
    max_workers: int = 16,
    attempts: int = 3,
) -> BackfillResult:
    symbol = _symbol(symbol)
    days = tuple(sorted(set(days)))
    if any(isinstance(day, datetime) or not isinstance(day, date) for day in days):
        raise MinuteCapacityError("backfill days must be dates")
    if type(enabled) is not bool or type(max_workers) is not int or not 1 <= max_workers <= 16 or type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError("invalid backfill controls")
    root = Path(root)
    existing: list[date] = []
    missing: list[date] = []
    for day in days:
        target = _day_path(root, symbol, day)
        if target.is_file():
            _read_day(target, symbol, day)
            existing.append(day)
        else:
            missing.append(day)
    if not enabled:
        return BackfillResult(existing=tuple(existing), missing=tuple(missing))
    if not callable(fetch_day):
        raise TypeError("fetch_day must be callable")

    def create(day: date) -> tuple[date, str | None]:
        target = _day_path(root, symbol, day)
        target.parent.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for _ in range(attempts):
            try:
                rendered = _aggregate_archive(fetch_day(symbol, day), symbol, day)
                with NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(rendered)
                try:
                    _read_day(temporary, symbol, day)
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        _read_day(target, symbol, day)
                    return day, None
                finally:
                    temporary.unlink(missing_ok=True)
            except Exception as error:  # retry injected transport, gzip and validation failures alike
                last = error
        return day, str(last or "backfill failed")

    created: list[date] = []
    failed: dict[date, str] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(missing)))) as pool:
        for day, error in pool.map(create, missing):
            if error:
                failed[day] = error
            else:
                created.append(day)
    return BackfillResult(
        tuple(sorted(created)),
        tuple(existing),
        tuple(sorted(failed)),
        MappingProxyType(dict(sorted(failed.items()))),
    )


__all__ = ["BackfillResult", "CapacityWindow", "MinuteCapacityError", "MinuteCapacityResult", "backfill_missing_days", "calculate_minute_capacity", "fetch_bybit_trade_archive"]
