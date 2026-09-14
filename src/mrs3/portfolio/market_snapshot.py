"""Current public Bybit reference and mark-price snapshot."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any

from .liquidity import ReferenceReader, ReferenceSnapshot, _digest


BASE_URL = "https://api.bybit.com/v5/market"
Fetcher = Callable[[str, Mapping[str, str]], Mapping[str, Any]]


class MarketSnapshotError(ValueError):
    """The complete market snapshot could not be validated."""


class ApiRateLimiter:
    """One injected, persisted limiter for all market-reference requests."""

    def __init__(self, state_path: str | Path | None = None, *, clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] | None = None) -> None:
        if state_path is None:
            raise MarketSnapshotError("persisted limiter state_path is required")
        self.state_path = Path(state_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or __import__("time").sleep
        self._next_allowed = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = threading.Lock()
        self._cooldown_until = self._load_cooldown()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise MarketSnapshotError("limiter clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _load_cooldown(self) -> datetime | None:
        if not self.state_path.is_file():
            return None
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8")).get("cooldown_until")
            if not isinstance(value, str):
                raise ValueError("cooldown_until is missing")
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("cooldown_until must be timezone-aware")
            return parsed.astimezone(timezone.utc)
        except (OSError, TypeError, ValueError, AttributeError) as error:
            raise MarketSnapshotError("persisted market API cooldown state is invalid") from error

    def _save_cooldown(self, until: datetime) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"cooldown_until": until.isoformat()}), encoding="utf-8")

    def _retry_delay(self, value: Any) -> float:
        headers = value.get("headers", {}) if isinstance(value, Mapping) else getattr(value, "headers", {})
        if not isinstance(headers, Mapping):
            headers = getattr(getattr(value, "response", None), "headers", {})
        if not isinstance(headers, Mapping):
            headers = {}
        for key in ("Retry-After", "retry-after"):
            if key in headers:
                try:
                    return min(60.0, max(0.0, float(headers[key])))
                except (TypeError, ValueError):
                    pass
        for key in (
            "X-Bapi-Limit-Reset-Timestamp",
            "x-bapi-limit-reset-timestamp",
            "X-Bapi-Limit-Reset",
            "x-bapi-limit-reset",
        ):
            if key in headers:
                try:
                    reset = float(headers[key])
                    if reset > 100_000_000_000:
                        reset /= 1000.0
                    elif reset < 100_000_000:
                        return min(60.0, max(0.0, reset))
                    return min(60.0, max(0.0, reset - self._now().timestamp()))
                except (TypeError, ValueError):
                    pass
        return 0.0

    @staticmethod
    def _status_code(value: Any) -> Any:
        if isinstance(value, Mapping):
            return value.get("status_code", value.get("statusCode", value.get("retCode")))
        code = getattr(value, "status_code", None)
        return code if code is not None else getattr(getattr(value, "response", None), "status_code", None)

    @classmethod
    def _retryable(cls, value: Any) -> bool:
        code = cls._status_code(value)
        return code in (429, 10006, "429", "10006")

    @staticmethod
    def _access_too_frequent(error: BaseException) -> bool:
        response = getattr(error, "response", None)
        message = str(error)
        if response is not None:
            message += " " + str(getattr(response, "text", ""))
        status = getattr(error, "status_code", None)
        if status is None:
            status = getattr(response, "status_code", None)
        return status in (403, "403") and "access" in message.casefold() and "frequent" in message.casefold()

    @staticmethod
    def _access_too_frequent_payload(payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        status = payload.get("status_code", payload.get("statusCode"))
        message = payload.get("retMsg", payload.get("message", ""))
        return status in (403, "403") and "access" in str(message).casefold() and "frequent" in str(message).casefold()

    def call(self, fetcher: Fetcher, feed: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        with self._lock:
            for attempt in range(3):
                now = self._now()
                if self._cooldown_until is not None and now < self._cooldown_until:
                    raise MarketSnapshotError("market API cooldown active")
                delay = max(0.0, (self._next_allowed - now).total_seconds())
                if delay:
                    self.sleep(delay)
                self._next_allowed = self._now() + timedelta(seconds=0.5)
                try:
                    payload = fetcher(feed, params)
                except Exception as error:
                    if self._access_too_frequent(error):
                        self._cooldown_until = self._now() + timedelta(minutes=10)
                        self._save_cooldown(self._cooldown_until)
                    if self._retryable(error) and attempt < 2:
                        delay = self._retry_delay(error)
                        if delay:
                            self.sleep(delay)
                        continue
                    raise
                if self._retryable(payload):
                    if attempt < 2:
                        delay = self._retry_delay(payload)
                        if delay:
                            self.sleep(delay)
                        continue
                    raise MarketSnapshotError("market API retry budget exhausted")
                if self._access_too_frequent_payload(payload):
                    self._cooldown_until = self._now() + timedelta(minutes=10)
                    self._save_cooldown(self._cooldown_until)
                    raise MarketSnapshotError("market API cooldown active")
                return payload
            raise MarketSnapshotError("market API retry budget exhausted")


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
    symbols: Iterable[str], *, captured_at_ms: int, fetcher: Fetcher | None = None, limiter: ApiRateLimiter | None = None
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

    if fetcher is None and limiter is None:
        raise MarketSnapshotError("default market fetcher requires a persisted API limiter")
    get = fetcher or _http_fetch
    if limiter is not None:
        raw_get = get
        get = lambda feed, params: limiter.call(raw_get, feed, params)
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


__all__ = ["ApiRateLimiter", "MarketSnapshot", "MarketSnapshotError", "load_market_snapshot"]
