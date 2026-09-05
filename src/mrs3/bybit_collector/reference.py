"""Bybit reference-data pages, raw responses, and daily immutable snapshots."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import gzip
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

import duckdb
import httpx


BASE_URL = "https://api.bybit.com/v5/market"
_MISSING = object()
_Fetcher = Callable[[str, Mapping[str, str]], Mapping[str, Any]]


class ReferenceDataError(ValueError):
    """A REST response or reference record violates the public contract."""


@dataclass(frozen=True, slots=True)
class SymbolEvent:
    symbol: str
    event_type: str
    reason: str = "reference_snapshot_diff"


@dataclass(frozen=True, slots=True)
class ReferenceSnapshot:
    captured_at_ms: int
    instruments: tuple[dict[str, Any], ...]
    risk_limits: tuple[dict[str, Any], ...]
    raw_files: tuple[Path, ...]
    published_files: tuple[Path, ...]
    symbol_events: tuple[SymbolEvent, ...]


def _http_fetch(feed: str, params: Mapping[str, str]) -> Mapping[str, Any]:
    try:
        response = httpx.get(f"{BASE_URL}/{feed}", params=dict(params), timeout=30.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ReferenceDataError(f"Bybit {feed} request failed: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ReferenceDataError(f"Bybit {feed} response must be an object")
    return payload


def _pick(item: Mapping[str, Any], *paths: str, default: Any = _MISSING) -> Any:
    for path in paths:
        value: Any = item
        for part in path.split("."):
            if not isinstance(value, Mapping) or part not in value:
                break
            value = value[part]
        else:
            return value
    if default is not _MISSING:
        return default
    raise ReferenceDataError(f"reference record is missing {paths[0]}")


def _text(item: Mapping[str, Any], *paths: str, default: str | None = None) -> str:
    value = _pick(item, *paths, default=default)
    if value is None:
        if default is not None:
            return default
        raise ReferenceDataError(f"reference field {paths[0]} must be text")
    if not isinstance(value, str):
        raise ReferenceDataError(f"reference field {paths[0]} must be text")
    return value


def _decimal(item: Mapping[str, Any], *paths: str) -> Decimal:
    value = _pick(item, *paths)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ReferenceDataError(f"reference field {paths[0]} must be decimal") from exc
    if not result.is_finite():
        raise ReferenceDataError(f"reference field {paths[0]} must be finite")
    return result


def _integer(item: Mapping[str, Any], *paths: str) -> int:
    value = _pick(item, *paths)
    if isinstance(value, bool):
        raise ReferenceDataError(f"reference field {paths[0]} must be integer")
    try:
        result = int(str(value))
    except (ValueError, TypeError) as exc:
        raise ReferenceDataError(f"reference field {paths[0]} must be integer") from exc
    if str(value).strip() != str(result):
        raise ReferenceDataError(f"reference field {paths[0]} must be integer")
    return result


def _boolean(item: Mapping[str, Any], *paths: str) -> bool:
    value = _pick(item, *paths)
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in {"false", "true", "0", "1"}:
        return value.lower() in {"true", "1"}
    raise ReferenceDataError(f"reference field {paths[0]} must be boolean")


def _instrument(item: Any, symbol: str, captured_at_ms: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ReferenceDataError("instrument record must be an object")
    if _text(item, "symbol") != symbol:
        raise ReferenceDataError("instrument record symbol does not match request")
    return {
        "captured_at_ms": captured_at_ms,
        "symbol": symbol,
        "status": _text(item, "status"),
        "symbol_type": _text(item, "symbolType", "symbol_type", default=""),
        "contract_type": _text(item, "contractType", "contract_type"),
        "launch_time_ms": _integer(item, "launchTime", "launch_time_ms"),
        "settle_coin": _text(item, "settleCoin", "settle_coin"),
        "tick_size": _decimal(item, "priceFilter.tickSize", "tickSize", "tick_size"),
        "qty_step": _decimal(item, "lotSizeFilter.qtyStep", "qtyStep", "qty_step"),
        "min_order_qty": _decimal(
            item, "lotSizeFilter.minOrderQty", "minOrderQty", "min_order_qty"
        ),
        "max_order_qty": _decimal(
            item, "lotSizeFilter.maxOrderQty", "maxOrderQty", "max_order_qty"
        ),
        "max_market_order_qty": _decimal(
            item,
            "lotSizeFilter.maxMktOrderQty",
            "maxMktOrderQty",
            "maxMarketOrderQty",
            "max_market_order_qty",
        ),
        "min_notional_value": _decimal(
            item,
            "lotSizeFilter.minNotionalValue",
            "minNotionalValue",
            "min_notional_value",
        ),
        "min_leverage": _decimal(
            item, "leverageFilter.minLeverage", "minLeverage", "min_leverage"
        ),
        "max_leverage": _decimal(
            item, "leverageFilter.maxLeverage", "maxLeverage", "max_leverage"
        ),
        "leverage_step": _decimal(
            item, "leverageFilter.leverageStep", "leverageStep", "leverage_step"
        ),
        "funding_interval": _integer(item, "fundingInterval", "funding_interval"),
        "upper_funding_rate": _decimal(
            item, "upperFundingRate", "upper_funding_rate"
        ),
        "lower_funding_rate": _decimal(
            item, "lowerFundingRate", "lower_funding_rate"
        ),
        "full_name": _text(item, "fullName", "full_name", default=""),
        "market_region": _text(item, "marketRegion", "market_region", default=""),
        "underlying_ticker": _text(
            item, "underlyingTicker", "underlying_ticker", default=""
        ),
    }


def _risk_limit(item: Any, symbol: str, captured_at_ms: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ReferenceDataError("risk-limit record must be an object")
    if _text(item, "symbol") != symbol:
        raise ReferenceDataError("risk-limit record symbol does not match request")
    return {
        "captured_at_ms": captured_at_ms,
        "symbol": symbol,
        "risk_id": _integer(item, "id", "riskId", "risk_id"),
        "risk_limit_value": _decimal(
            item, "riskLimitValue", "risk_limit_value"
        ),
        "maintenance_margin": _decimal(
            item, "maintenanceMargin", "maintenance_margin"
        ),
        "initial_margin": _decimal(item, "initialMargin", "initial_margin"),
        "max_leverage": _decimal(item, "maxLeverage", "max_leverage"),
        "mm_deduction": _decimal(item, "mmDeduction", "mm_deduction"),
        "is_lowest_risk": _boolean(item, "isLowestRisk", "is_lowest_risk"),
    }


def _validate_page(payload: Mapping[str, Any], feed: str) -> tuple[list[Any], str]:
    if type(payload.get("retCode")) is not int or payload["retCode"] != 0:
        raise ReferenceDataError(f"Bybit {feed} response retCode is not zero")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ReferenceDataError(f"Bybit {feed} result must be an object")
    items = result.get("list")
    if not isinstance(items, list):
        raise ReferenceDataError(f"Bybit {feed} result.list must be an array")
    cursor = result.get("nextPageCursor")
    if not isinstance(cursor, str):
        raise ReferenceDataError(f"Bybit {feed} nextPageCursor must be text")
    return items, cursor


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _without_capture(record: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple((key, value) for key, value in record.items() if key != "captured_at_ms")


class ReferenceDataCollector:
    """Collect symbol-specific public reference pages and publish daily rows."""

    def __init__(
        self,
        root: Path,
        *,
        fetcher: _Fetcher | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.root = Path(root)
        self.fetcher: _Fetcher = fetcher or _http_fetch
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._previous: dict[str, tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]] = {}

    def collect(
        self, symbols: Iterable[str], *, captured_at_ms: int | None = None
    ) -> ReferenceSnapshot:
        normalized_symbols = self._symbols(symbols)
        captured = self._capture_time(captured_at_ms)
        date = datetime.fromtimestamp(captured / 1000, tz=timezone.utc).date().isoformat()
        instruments: list[dict[str, Any]] = []
        risks: list[dict[str, Any]] = []
        raw_files: list[Path] = []
        for symbol in normalized_symbols:
            items, files = self._pages("instruments-info", symbol, captured, date)
            instruments.extend(_instrument(item, symbol, captured) for item in items)
            raw_files.extend(files)
            items, files = self._pages("risk-limit", symbol, captured, date)
            risks.extend(_risk_limit(item, symbol, captured) for item in items)
            raw_files.extend(files)

        instruments.sort(key=lambda item: (item["symbol"], item["tick_size"]))
        risks.sort(key=lambda item: (item["symbol"], item["risk_id"]))
        current = {
            symbol: (
                tuple(item for item in instruments if item["symbol"] == symbol),
                tuple(item for item in risks if item["symbol"] == symbol),
            )
            for symbol in {item["symbol"] for item in instruments}
        }
        events = self._events(current)
        published = (
            self._publish_parquet(date, "instruments.parquet", instruments, "instruments"),
            self._publish_parquet(date, "risk_limits.parquet", risks, "risk_limits"),
        )
        self._previous = current
        return ReferenceSnapshot(
            captured,
            tuple(instruments),
            tuple(risks),
            tuple(raw_files),
            published,
            events,
        )

    def _symbols(self, symbols: Iterable[str]) -> tuple[str, ...]:
        try:
            values = tuple(sorted(set(symbols)))
        except (TypeError, ValueError) as exc:
            raise ReferenceDataError("symbols must be an iterable of strings") from exc
        if not values or any(not isinstance(symbol, str) or not symbol for symbol in values):
            raise ReferenceDataError("symbols must be non-empty strings")
        return values

    def _capture_time(self, value: int | None) -> int:
        value = self._now_ms() if value is None else value
        if type(value) is not int or value < 0:
            raise ReferenceDataError("captured_at_ms must be a non-negative integer")
        return value

    def _pages(
        self, feed: str, symbol: str, captured: int, date: str
    ) -> tuple[list[Any], list[Path]]:
        items: list[Any] = []
        raw_files: list[Path] = []
        cursor: str | None = None
        seen: set[str] = set()
        page = 1
        while True:
            params = {"category": "linear", "symbol": symbol}
            if cursor is not None:
                params["cursor"] = cursor
            try:
                payload = self.fetcher(feed, params)
            except ReferenceDataError:
                raise
            except Exception as exc:
                raise ReferenceDataError(f"Bybit {feed} fetch failed: {exc}") from exc
            if not isinstance(payload, Mapping):
                raise ReferenceDataError(f"Bybit {feed} response must be an object")
            page_items, next_cursor = _validate_page(payload, feed)
            raw_path = self.root / "raw_reference" / f"date={date}" / (
                f"{symbol}_{feed}_{captured}_{page:04d}.json.gz"
            )
            self._write_raw(raw_path, payload)
            raw_files.append(raw_path)
            items.extend(page_items)
            if not next_cursor:
                return items, raw_files
            if next_cursor in seen:
                raise ReferenceDataError(f"Bybit {feed} pagination cursor repeats")
            seen.add(next_cursor)
            cursor = next_cursor
            page += 1

    def _write_raw(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default
        ).encode("utf-8")
        if path.exists():
            try:
                with gzip.open(path, "rb") as raw:
                    json.loads(raw.read().decode("utf-8"))
            except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                return
        temporary = tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        )
        temporary_path = Path(temporary.name)
        try:
            with temporary:
                with gzip.GzipFile(fileobj=temporary, mode="wb", mtime=0) as compressed:
                    compressed.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(str(temporary_path), str(path))
        finally:
            temporary_path.unlink(missing_ok=True)

    def _publish_parquet(
        self, date: str, name: str, rows: Iterable[Mapping[str, Any]], table_name: str
    ) -> Path:
        final = self.root / "reference" / f"date={date}" / name
        if final.exists():
            return final
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = tempfile.NamedTemporaryFile(
            mode="wb", dir=final.parent, prefix=f".{name}.", suffix=".tmp", delete=False
        )
        temporary_path = Path(temporary_file.name)
        temporary_file.close()
        values = list(rows)
        if table_name == "instruments":
            columns = (
                ("captured_at_ms", "BIGINT"), ("symbol", "VARCHAR"), ("status", "VARCHAR"),
                ("symbol_type", "VARCHAR"), ("contract_type", "VARCHAR"), ("launch_time_ms", "BIGINT"),
                ("settle_coin", "VARCHAR"), ("tick_size", "DECIMAL(38,18)"), ("qty_step", "DECIMAL(38,18)"),
                ("min_order_qty", "DECIMAL(38,18)"), ("max_order_qty", "DECIMAL(38,18)"),
                ("max_market_order_qty", "DECIMAL(38,18)"), ("min_notional_value", "DECIMAL(38,18)"),
                ("min_leverage", "DECIMAL(38,18)"), ("max_leverage", "DECIMAL(38,18)"),
                ("leverage_step", "DECIMAL(38,18)"), ("funding_interval", "BIGINT"),
                ("upper_funding_rate", "DECIMAL(38,18)"), ("lower_funding_rate", "DECIMAL(38,18)"),
                ("full_name", "VARCHAR"), ("market_region", "VARCHAR"), ("underlying_ticker", "VARCHAR"),
            )
        else:
            columns = (
                ("captured_at_ms", "BIGINT"), ("symbol", "VARCHAR"), ("risk_id", "BIGINT"),
                ("risk_limit_value", "DECIMAL(38,18)"), ("maintenance_margin", "DECIMAL(38,18)"),
                ("initial_margin", "DECIMAL(38,18)"), ("max_leverage", "DECIMAL(38,18)"),
                ("mm_deduction", "DECIMAL(38,18)"), ("is_lowest_risk", "BOOLEAN"),
            )
        connection = duckdb.connect()
        try:
            definitions = ", ".join(f'"{name}" {kind}' for name, kind in columns)
            connection.execute(f"CREATE TABLE {table_name} ({definitions})")
            if values:
                names = tuple(name for name, _kind in columns)
                placeholders = ", ".join("?" for _ in names)
                connection.executemany(
                    f"INSERT INTO {table_name} VALUES ({placeholders})",
                    [tuple(row[name] for name in names) for row in values],
                )
            connection.execute(
                f"COPY {table_name} TO '{temporary_path.as_posix().replace(chr(39), chr(39) * 2)}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            connection.close()
        try:
            descriptor = os.open(temporary_path, os.O_RDWR)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(str(temporary_path), str(final))
        finally:
            temporary_path.unlink(missing_ok=True)
        return final

    def _events(
        self,
        current: Mapping[str, tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]],
    ) -> tuple[SymbolEvent, ...]:
        events: list[SymbolEvent] = []
        for symbol in sorted(set(self._previous) | set(current)):
            if symbol not in self._previous:
                events.append(SymbolEvent(symbol, "added"))
            elif symbol not in current:
                events.append(SymbolEvent(symbol, "removed"))
            else:
                before = self._previous[symbol]
                after = current[symbol]
                if tuple(map(_without_capture, before[0])) != tuple(map(_without_capture, after[0])) or tuple(
                    map(_without_capture, before[1])
                ) != tuple(map(_without_capture, after[1])):
                    events.append(SymbolEvent(symbol, "changed"))
        return tuple(events)


__all__ = [
    "ReferenceDataCollector",
    "ReferenceDataError",
    "ReferenceSnapshot",
    "SymbolEvent",
]
