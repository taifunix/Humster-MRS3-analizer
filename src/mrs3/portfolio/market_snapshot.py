"""Current public Bybit reference and mark-price snapshot."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from .liquidity import ReferenceReader, ReferenceSnapshot, _digest


BASE_URL = "https://api.bybit.com/v5/market"
Fetcher = Callable[[str, Mapping[str, str]], Mapping[str, Any]]


class MarketSnapshotError(ValueError):
    """The complete market snapshot could not be validated."""


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    reference: ReferenceSnapshot
    mark_prices: Mapping[str, Decimal]
    captured_at_ms: int
    content_digest: str


def _http_fetch(feed: str, params: Mapping[str, str]) -> Mapping[str, Any]:
    import httpx

    response = httpx.get(f"{BASE_URL}/{feed}", params=dict(params), timeout=30.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise MarketSnapshotError(f"Bybit {feed} response must be an object")
    return payload


def _page(payload: Any, feed: str) -> tuple[list[Any], str]:
    if not isinstance(payload, Mapping) or type(payload.get("retCode")) is not int or payload["retCode"] != 0:
        raise MarketSnapshotError(f"Bybit {feed} retCode is invalid")
    result = payload.get("result")
    if not isinstance(result, Mapping) or result.get("category") != "linear":
        raise MarketSnapshotError(f"Bybit {feed} result category is invalid")
    items = result.get("list")
    if not isinstance(items, list):
        raise MarketSnapshotError(f"Bybit {feed} result.list is invalid")
    cursor = result.get("nextPageCursor", "")
    if not isinstance(cursor, str):
        raise MarketSnapshotError(f"Bybit {feed} cursor is invalid")
    return items, cursor


def _pages(fetcher: Fetcher, feed: str, symbol: str) -> list[Any]:
    items: list[Any] = []
    cursor = ""
    seen: set[str] = set()
    while True:
        params = {"category": "linear", "symbol": symbol}
        if cursor:
            params["cursor"] = cursor
        page, next_cursor = _page(fetcher(feed, params), feed)
        for item in page:
            if not isinstance(item, Mapping) or item.get("symbol") != symbol:
                raise MarketSnapshotError(f"Bybit {feed} item symbol is invalid")
        items.extend(page)
        if not next_cursor:
            return items
        if next_cursor in seen:
            raise MarketSnapshotError(f"Bybit {feed} pagination cursor repeats")
        seen.add(next_cursor)
        cursor = next_cursor


def _mark_price(fetcher: Fetcher, symbol: str) -> Decimal:
    items, cursor = _page(fetcher("tickers", {"category": "linear", "symbol": symbol}), "tickers")
    if cursor or len(items) != 1 or not isinstance(items[0], Mapping) or items[0].get("symbol") != symbol:
        raise MarketSnapshotError(f"Bybit ticker for {symbol} is not unique")
    raw_mark = items[0].get("markPrice")
    if isinstance(raw_mark, (float, bool)):
        raise MarketSnapshotError(f"Bybit ticker markPrice for {symbol} is invalid")
    try:
        mark = raw_mark if isinstance(raw_mark, Decimal) else Decimal(str(raw_mark))
    except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
        raise MarketSnapshotError(f"Bybit ticker markPrice for {symbol} is invalid") from exc
    if not mark.is_finite() or mark <= 0:
        raise MarketSnapshotError(f"Bybit ticker markPrice for {symbol} is invalid")
    return mark


def load_market_snapshot(
    symbols: Iterable[str], *, captured_at_ms: int, fetcher: Fetcher | None = None
) -> MarketSnapshot:
    if isinstance(symbols, (str, bytes)):
        raise MarketSnapshotError("symbols must be unique non-empty strings")
    try:
        values = tuple(symbols)
    except TypeError as exc:
        raise MarketSnapshotError("symbols must be unique non-empty strings") from exc
    if (
        not values
        or any(not isinstance(symbol, str) or not symbol for symbol in values)
        or len(set(values)) != len(values)
    ):
        raise MarketSnapshotError("symbols must be unique non-empty strings")
    if type(captured_at_ms) is not int or captured_at_ms < 0:
        raise MarketSnapshotError("captured_at_ms must be an exact non-negative integer")

    get = fetcher or _http_fetch
    instruments: list[Mapping[str, Any]] = []
    risk_tiers: list[Mapping[str, Any]] = []
    marks: dict[str, Decimal] = {}
    try:
        for symbol in sorted(values):
            active = [
                item
                for item in _pages(get, "instruments-info", symbol)
                if item.get("status") == "Trading" and item.get("contractType") == "LinearPerpetual"
            ]
            if len(active) != 1:
                raise MarketSnapshotError(f"{symbol} must have exactly one active LinearPerpetual instrument")
            tiers = _pages(get, "risk-limit", symbol)
            if not tiers:
                raise MarketSnapshotError(f"{symbol} must have at least one risk tier")
            instruments.extend(active)
            risk_tiers.extend(tiers)
            marks[symbol] = _mark_price(get, symbol)
        reference = ReferenceReader.from_records(
            instruments=instruments, risk_tiers=risk_tiers, captured_at_ms=captured_at_ms
        )
    except MarketSnapshotError:
        raise
    except Exception as exc:
        raise MarketSnapshotError(f"market snapshot fetch failed: {exc}") from exc

    prices = MappingProxyType(dict(sorted(marks.items())))
    digest = _digest(
        {
            "captured_at_ms": captured_at_ms,
            "reference_digest": reference.content_digest,
            "mark_prices": prices,
        },
        schema_id="portfolio_market_snapshot_v1",
    )
    return MarketSnapshot(reference, prices, captured_at_ms, digest)


__all__ = ["MarketSnapshot", "MarketSnapshotError", "load_market_snapshot"]
