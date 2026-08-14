from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re

from lxml import html


class PerformanceParseError(ValueError):
    """Raised when an immutable performance report is not complete and valid."""


@dataclass(frozen=True, slots=True)
class PerformanceInventory:
    metric_count: int
    metric_headers: tuple[str, ...]
    trade_headers: tuple[str, ...]
    trade_row_count: int
    wallet_sample_count: int
    equity_sample_count: int
    minimum_timestamp: datetime
    maximum_timestamp: datetime


@dataclass(frozen=True, slots=True)
class ParsedPerformanceReport:
    settings: dict[str, object]
    metrics: dict[str, str]
    actions: tuple[dict[str, str], ...]
    wallet_series: tuple[tuple[int, Decimal], ...]
    equity_series: tuple[tuple[int, Decimal], ...]
    inventory: PerformanceInventory


@dataclass(frozen=True, slots=True)
class _RawInventory:
    settings_count: int
    metric_count: int
    metric_headers: tuple[str, ...]
    trade_headers: tuple[str, ...]
    trade_row_count: int
    action_timestamps: tuple[datetime, ...]
    wallet_timestamps: tuple[datetime, ...]
    equity_timestamps: tuple[datetime, ...]


def _text(node: object) -> str:
    return " ".join(" ".join(node.itertext()).split())


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise PerformanceParseError(f"invalid UTC timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise PerformanceParseError(f"timestamp is not UTC: {value!r}")
    return parsed.astimezone(timezone.utc)


def _epoch_timestamp(value: int) -> datetime:
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise PerformanceParseError(f"invalid UTC timestamp: {value!r}") from error


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise PerformanceParseError(f"invalid finite series value: {value!r}") from error
    if not result.is_finite():
        raise PerformanceParseError(f"invalid finite series value: {value!r}")
    return result


def _settings(document: object) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    for pre in document.xpath("//pre"):
        try:
            value = json.loads("".join(pre.itertext()).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"] and isinstance(value.get("basic"), dict):
            candidates.append(value)
    if len(candidates) != 1:
        raise PerformanceParseError("exactly one complete settings JSON object is required")
    return candidates[0]


def _tables(document: object) -> tuple[dict[str, str], tuple[dict[str, str], ...], tuple[str, ...], tuple[str, ...]]:
    metric_matches: list[tuple[dict[str, str], tuple[str, ...]]] = []
    trade_matches: list[tuple[tuple[dict[str, str], ...], tuple[str, ...]]] = []
    for table in document.xpath("//table"):
        headers = tuple(_text(cell) for cell in table.xpath(".//thead/tr[1]/th|.//thead/tr[1]/td"))
        rows: list[dict[str, str]] = []
        for row in table.xpath(".//tbody/tr"):
            cells = [_text(cell) for cell in row.xpath("./th|./td")]
            if len(cells) != len(headers):
                raise PerformanceParseError("table row width differs from its headers")
            rows.append(dict(zip(headers, cells, strict=True)))
        if len(headers) >= 2 and headers[:2] == ("Metric", "Value"):
            metrics: dict[str, str] = {}
            for row in rows:
                key, value = row["Metric"], row["Value"]
                if not key or key in metrics:
                    raise PerformanceParseError("metrics contain duplicate or empty keys")
                metrics[key] = value
            metric_matches.append((metrics, headers))
        if {"Timestamp", "Symbol", "Action", "PnL"} <= set(headers):
            trade_matches.append((tuple(rows), headers))
    if len(metric_matches) != 1:
        raise PerformanceParseError("exactly one Metric/Value table is required")
    if len(trade_matches) != 1:
        raise PerformanceParseError("exactly one trade table is required")
    metrics, metric_headers = metric_matches[0]
    actions, trade_headers = trade_matches[0]
    return metrics, actions, metric_headers, trade_headers


def _raw_series_timestamps(source: str, name: str) -> tuple[datetime, ...]:
    assignments = list(re.finditer(rf"\b(?:const|let|var)\s+{name}\s*=\s*", source))
    if len(assignments) != 1:
        raise PerformanceParseError(f"exactly one {name} assignment is required")
    try:
        raw = json.JSONDecoder().raw_decode(source[assignments[0].end():])[0]
    except json.JSONDecodeError as error:
        raise PerformanceParseError(f"malformed {name} array") from error
    if not isinstance(raw, list) or not raw:
        raise PerformanceParseError(f"{name} must be non-empty")
    timestamps: list[datetime] = []
    previous: int | None = None
    for point in raw:
        if not isinstance(point, list) or len(point) != 2 or not isinstance(point[0], int) or isinstance(point[0], bool):
            raise PerformanceParseError(f"malformed {name} point")
        if previous is not None and point[0] <= previous:
            raise PerformanceParseError(f"{name} timestamps must be strictly increasing")
        timestamps.append(_epoch_timestamp(point[0]))
        previous = point[0]
    return tuple(timestamps)


def _raw_inventory(source: str, document: object) -> _RawInventory:
    settings_count = 0
    for pre in document.xpath("//pre"):
        try:
            value = json.loads("".join(pre.itertext()).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"] and isinstance(value.get("basic"), dict):
            settings_count += 1

    metric_matches: list[tuple[int, tuple[str, ...]]] = []
    trade_matches: list[tuple[int, tuple[str, ...], tuple[datetime, ...]]] = []
    for table in document.xpath("//table"):
        headers = tuple(_text(cell) for cell in table.xpath(".//thead/tr[1]/th|.//thead/tr[1]/td"))
        if len(headers) != len(set(headers)):
            raise PerformanceParseError("duplicate table header")
        rows = [tuple(_text(cell) for cell in row.xpath("./th|./td")) for row in table.xpath(".//tbody/tr")]
        if any(len(row) != len(headers) for row in rows):
            raise PerformanceParseError("table row width differs from its headers")
        if len(headers) >= 2 and headers[:2] == ("Metric", "Value"):
            keys = [row[0] for row in rows]
            if any(not key for key in keys) or len(keys) != len(set(keys)):
                raise PerformanceParseError("metrics contain duplicate or empty keys")
            metric_matches.append((len(rows), headers))
        if {"Timestamp", "Symbol", "Action", "PnL"} <= set(headers):
            timestamp_index = headers.index("Timestamp")
            trade_matches.append((len(rows), headers, tuple(_timestamp(row[timestamp_index]) for row in rows)))
    if settings_count != 1:
        raise PerformanceParseError("exactly one complete settings JSON object is required")
    if len(metric_matches) != 1:
        raise PerformanceParseError("exactly one Metric/Value table is required")
    if len(trade_matches) != 1:
        raise PerformanceParseError("exactly one trade table is required")
    metric_count, metric_headers = metric_matches[0]
    trade_count, trade_headers, action_timestamps = trade_matches[0]
    return _RawInventory(
        settings_count,
        metric_count,
        metric_headers,
        trade_headers,
        trade_count,
        action_timestamps,
        _raw_series_timestamps(source, "walletSeries"),
        _raw_series_timestamps(source, "equitySeries"),
    )


def _series(source: str, name: str) -> tuple[tuple[int, Decimal], ...]:
    assignments = list(re.finditer(rf"\b(?:const|let|var)\s+{name}\s*=\s*", source))
    if len(assignments) != 1:
        raise PerformanceParseError(f"exactly one {name} assignment is required")
    try:
        raw = json.JSONDecoder().raw_decode(source[assignments[0].end():])[0]
    except json.JSONDecodeError as error:
        raise PerformanceParseError(f"malformed {name} array") from error
    if not isinstance(raw, list) or not raw:
        raise PerformanceParseError(f"{name} must be non-empty")
    result: list[tuple[int, Decimal]] = []
    previous: int | None = None
    for point in raw:
        if not isinstance(point, list) or len(point) != 2 or not isinstance(point[0], int) or isinstance(point[0], bool):
            raise PerformanceParseError(f"malformed {name} point")
        timestamp = point[0]
        if previous is not None and timestamp <= previous:
            raise PerformanceParseError(f"{name} timestamps must be strictly increasing")
        value = _decimal(point[1])
        result.append((timestamp, value))
        previous = timestamp
    return tuple(result)


def parse_performance_report(source: bytes) -> ParsedPerformanceReport:
    if not isinstance(source, bytes):
        raise PerformanceParseError("source must be bytes")
    try:
        decoded = source.decode("utf-8", errors="strict")
        document = html.fromstring(decoded)
    except (UnicodeDecodeError, ValueError) as error:
        raise PerformanceParseError("malformed UTF-8 or HTML") from error
    raw = _raw_inventory(decoded, document)
    settings = _settings(document)
    metrics, actions, metric_headers, trade_headers = _tables(document)
    wallet = _series(decoded, "walletSeries")
    equity = _series(decoded, "equitySeries")
    if len(wallet) != len(equity):
        raise PerformanceParseError("wallet/equity sample counts must match")
    action_times = tuple(_timestamp(row["Timestamp"]) for row in actions)
    wallet_times = tuple(_epoch_timestamp(point[0]) for point in wallet)
    equity_times = tuple(_epoch_timestamp(point[0]) for point in equity)
    if (
        raw.settings_count != 1
        or len(metrics) != raw.metric_count
        or metric_headers != raw.metric_headers
        or trade_headers != raw.trade_headers
        or len(actions) != raw.trade_row_count
        or action_times != raw.action_timestamps
        or len(wallet) != len(raw.wallet_timestamps)
        or len(equity) != len(raw.equity_timestamps)
        or wallet_times != raw.wallet_timestamps
        or equity_times != raw.equity_timestamps
    ):
        raise PerformanceParseError("semantic output does not match raw HTML inventory")
    all_times = action_times + wallet_times + equity_times
    inventory = PerformanceInventory(
        metric_count=len(metrics),
        metric_headers=metric_headers,
        trade_headers=trade_headers,
        trade_row_count=len(actions),
        wallet_sample_count=len(wallet),
        equity_sample_count=len(equity),
        minimum_timestamp=min(all_times),
        maximum_timestamp=max(all_times),
    )
    if (
        inventory.trade_row_count != raw.trade_row_count
        or inventory.wallet_sample_count != len(raw.wallet_timestamps)
        or inventory.equity_sample_count != len(raw.equity_timestamps)
    ):
        raise PerformanceParseError("semantic counts do not match structural inventory")
    return ParsedPerformanceReport(settings, metrics, actions, wallet, equity, inventory)
