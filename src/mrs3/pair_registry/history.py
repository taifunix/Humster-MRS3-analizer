"""History sufficiency of a pair from its listing day (trade store rules: 4 warm-up days, 20-day periods,
READY needs two periods)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

WARMUP_DAYS = 4
PERIOD_DAYS = 20


def _days_word(count: int) -> str:
    tail, tens = count % 10, count % 100
    if tail == 1 and tens != 11:
        return "день"
    if tail in (2, 3, 4) and tens not in (12, 13, 14):
        return "дня"
    return "дней"


def sufficiency(listing_ms: int, today: date) -> dict[str, object]:
    listing_day = datetime.fromtimestamp(listing_ms / 1000, tz=timezone.utc).date()
    days = (today - listing_day).days
    research_from = listing_day + timedelta(days=WARMUP_DAYS + PERIOD_DAYS)
    ready_from = listing_day + timedelta(days=WARMUP_DAYS + 2 * PERIOD_DAYS)
    if today >= ready_from:
        status = "READY возможен"
    elif today >= research_from:
        status = "можно исследовать"
    else:
        status = f"мало истории ({days} {_days_word(days)})"
    return {"listing_day": listing_day.isoformat(), "days": days, "research_from": research_from.isoformat(),
            "ready_from": ready_from.isoformat(), "status": status}


def _registry_connection(config_path: Path, on_busy: Callable[[str], None] | None):
    """A read-only registry connection, or None without a registry or while a writer holds it (`on_busy` told)."""
    import duckdb

    from .store import DATABASE, RegistryError, load_registry_settings

    try:
        database = load_registry_settings(Path(config_path)).root / DATABASE
    except RegistryError:
        return None
    if not database.is_file():
        return None
    try:
        return duckdb.connect(str(database), read_only=True)
    except duckdb.Error as error:
        if on_busy is not None:
            on_busy(f"pair registry unavailable: {type(error).__name__}")
        return None


def listing_days(config_path: Path, exchange: str = "bybit",
                 on_busy: Callable[[str], None] | None = None) -> dict[str, date]:
    """Listing days of every pair of an exchange in the registry, whatever its status (explicit `--pairs` of the
    history scripts); empty without a registry or while a writer holds it."""
    con = _registry_connection(config_path, on_busy)
    if con is None:
        return {}
    with con:
        rows = con.execute("select symbol, listing_ms from pairs where exchange = ? and listing_ms is not null",
                           [exchange]).fetchall()
    return {symbol: datetime.fromtimestamp(ms / 1000, timezone.utc).date() for symbol, ms in rows}


def registry_instruments(config_path: Path, exchange: str = "bybit",
                         on_busy: Callable[[str], None] | None = None) -> dict[str, tuple[float, float]] | None:
    """(tick size, qty step) of every pair of the exchange that has both in the registry, refreshed by every
    «Синхронизировать» (docs/specs/2026-10-20-panel-history-incremental.md, requirement 5). None without a registry
    or while a writer holds it."""
    con = _registry_connection(config_path, on_busy)
    if con is None:
        return None
    with con:
        rows = con.execute("select symbol, tick_size, qty_step from pairs where exchange = ? and tick_size is not null "
                           "and qty_step is not null", [exchange]).fetchall()
    return {symbol: (float(tick), float(qty)) for symbol, tick, qty in rows}


def history_pairs(config_path: Path, on_busy: Callable[[str], None] | None = None,
                  exchange: str = "bybit") -> list[tuple[str, date]] | None:
    """TRADING pairs of the exchange in the registry with their listing day, the most liquid first (latest liquidity `L`
    descending, pairs without a READY window after them), then by symbol: the pair order of the history scripts
    (registry stage 4). None when there is no registry or a writer holds it (`on_busy` is told); the caller then
    falls back to the workbook."""
    from decimal import Decimal, InvalidOperation

    con = _registry_connection(config_path, on_busy)
    if con is None:
        return None
    with con:
        rows = con.execute(
            "select p.symbol, p.listing_ms, l.l from pairs p left join (select symbol, l, row_number() over "
            "(partition by symbol order by as_of_day desc) as latest from liquidity where exchange = ? and "
            "status = 'READY') l on l.symbol = p.symbol and l.latest = 1 "
            "where p.exchange = ? and p.status = 'TRADING' and p.listing_ms is not null", [exchange, exchange]).fetchall()

    def level(value: object) -> Decimal:
        try:
            return Decimal(str(value)) if value is not None else Decimal(-1)
        except InvalidOperation:
            return Decimal(-1)

    ordered = sorted(rows, key=lambda row: (-level(row[2]), row[0]))
    return [(symbol, datetime.fromtimestamp(ms / 1000, timezone.utc).date()) for symbol, ms, _level in ordered]
