"""Tester minute data on demand: instrument parameters and automatic download of missing days.

Contract: docs/specs/2026-10-16-mrs2-period-csv-generator.md («Inputs»). Missing days are built from the
official Bybit trade archive in the tester's parquet format (an archive-built day equals the tester's own
day file) and are added next to the tester's files without overwriting any of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Callable, Mapping

import httpx

from .portfolio.minute_capacity import backfill_missing_days, fetch_bybit_trade_archive

INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
# The daily archive of day D is published a few hours after D ends; only days before this lag are requested.
ARCHIVE_LAG = timedelta(hours=9)


class MinuteDataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    tick_size: float
    qty_step: float
    launch_day: date


def _fetch_instrument_json(symbol: str) -> Mapping[str, object]:
    response = httpx.get(INSTRUMENTS_URL, params={"category": "linear", "symbol": symbol}, timeout=30.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        raise MinuteDataError(f"instruments-info failed for {symbol}")
    items = (payload.get("result") or {}).get("list") or []
    if len(items) != 1:
        raise MinuteDataError(f"instrument not found on Bybit: {symbol}")
    return items[0]


def instrument(symbol: str, cache_path: Path | None = None,
               fetch: Callable[[str], Mapping[str, object]] = _fetch_instrument_json) -> Instrument:
    """Tick size, quantity step and listing day of a Bybit linear contract (cached in `cache_path`)."""
    cache: dict[str, dict] = {}
    if cache_path is not None and cache_path.is_file():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    item = cache.get(symbol)
    if item is None:
        raw = fetch(symbol)
        item = {
            "tick_size": str(raw["priceFilter"]["tickSize"]),
            "qty_step": str(raw["lotSizeFilter"]["qtyStep"]),
            "launch_ms": int(raw["launchTime"]),
        }
        if cache_path is not None:
            cache[symbol] = item
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    return Instrument(
        symbol,
        float(Decimal(item["tick_size"])),
        float(Decimal(item["qty_step"])),
        datetime.fromtimestamp(item["launch_ms"] / 1000, timezone.utc).date(),
    )


def last_archived_day(now: datetime) -> date:
    return (now.astimezone(timezone.utc) - ARCHIVE_LAG).date() - timedelta(days=1)


def ensure_days(data_root: Path, symbol: str, first: date, last: date, *,
                fetch_day: Callable[[str, date], bytes] = fetch_bybit_trade_archive, workers: int = 8) -> dict[str, list[date]]:
    """Make sure every day of [first, last] exists in the tester's data folder; download the missing ones.

    Returns {"existing": [...], "created": [...], "failed": [...]}; a failed day stays absent (no trades or
    no archive) and the caller decides whether that is acceptable.
    """
    if first > last:
        return {"existing": [], "created": [], "failed": []}
    days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    result = backfill_missing_days(Path(data_root), symbol, days, fetch_day=fetch_day, enabled=True,
                                   max_workers=max(1, min(16, workers)))
    return {"existing": list(result.existing), "created": list(result.created), "failed": list(result.missing)}
