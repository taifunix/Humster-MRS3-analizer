from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import re

from lxml import etree, html


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


_TESTER_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


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
    value = value.strip()
    if _TESTER_TIMESTAMP.fullmatch(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError as error:
            raise PerformanceParseError(f"invalid UTC timestamp: {value!r}") from error
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
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
        raw = "".join(pre.itertext()).strip()
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as error:
            if raw.startswith("{"):
                raise PerformanceParseError("malformed settings JSON") from error
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
        metric_candidate = len(headers) >= 2 and headers[:2] == ("Metric", "Value")
        trade_candidate = {"Timestamp", "Symbol", "Action", "PnL"} <= set(headers)
        if (metric_candidate or trade_candidate) and len(headers) != len(set(headers)):
            raise PerformanceParseError("duplicate table header")
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
    if len(trade_matches) != 1:
        raise PerformanceParseError("exactly one trade table is required")
    if not metric_matches:
        raise PerformanceParseError("exactly one Metric/Value table is required")
    metric_headers = metric_matches[0][1]
    metrics: dict[str, str] = {}
    for candidate, headers in metric_matches:
        if headers != metric_headers:
            raise PerformanceParseError("metric table headers differ")
        for key, value in candidate.items():
            if key in metrics:
                raise PerformanceParseError("metrics contain duplicate or empty keys")
            metrics[key] = value
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


class _RawMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pre_text: list[str] = []
        self.tables: list[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]] = []
        self._pre_parts: list[str] | None = None
        self._table_headers: list[str] | None = None
        self._table_rows: list[tuple[str, ...]] | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "pre":
            self._pre_parts = []
        elif tag == "table":
            self._table_headers, self._table_rows = [], []
        elif tag == "tr" and self._table_rows is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._pre_parts is not None:
            self._pre_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre" and self._pre_parts is not None:
            self.pre_text.append(" ".join("".join(self._pre_parts).split()))
            self._pre_parts = None
        elif tag in {"th", "td"} and self._cell_parts is not None and self._row is not None:
            value = " ".join("".join(self._cell_parts).split())
            self._row.append(value)
            self._cell_parts = None
        elif tag == "tr" and self._row is not None and self._table_rows is not None:
            if self._table_headers is None or not self._table_headers:
                self._table_headers = list(self._row)
            else:
                self._table_rows.append(tuple(self._row))
            self._row = None
        elif tag == "table" and self._table_headers is not None and self._table_rows is not None:
            self.tables.append((tuple(self._table_headers), tuple(self._table_rows)))
            self._table_headers = None
            self._table_rows = None


_RawMarkup = tuple[list[str], list[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]]]


def _stdlib_raw_markup(source: str) -> _RawMarkup:
    """Scan the markup with Python's own parser, independent of libxml2.

    This must stay a *tokenizer*, not a tree builder. Its whole purpose is to
    disagree with lxml when lxml silently repairs malformed markup — an
    unterminated ``<td>``, a missing ``</tr>``. Any spec-compliant HTML5 tree
    builder (lexbor, html5lib, libxml2 itself) performs the same implicit-close
    recovery lxml does, so substituting one here would make the two parses agree
    on exactly the inputs this cross-check exists to reject. Faster is not a
    reason to change it; a genuinely independent tokenizer would be.
    """
    parser = _RawMarkupParser()
    parser.feed(source)
    parser.close()
    return parser.pre_text, parser.tables


_raw_markup = _stdlib_raw_markup


def _raw_inventory(source: str) -> _RawInventory:
    pre_text, raw_tables = _raw_markup(source)
    settings_count = 0
    for raw in pre_text:
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as error:
            if raw.startswith("{"):
                raise PerformanceParseError("malformed settings JSON") from error
            continue
        if isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"] and isinstance(value.get("basic"), dict):
            settings_count += 1

    metric_matches: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []
    trade_matches: list[tuple[int, tuple[str, ...], tuple[datetime, ...]]] = []
    for headers, rows in raw_tables:
        metric_candidate = len(headers) >= 2 and headers[:2] == ("Metric", "Value")
        trade_candidate = {"Timestamp", "Symbol", "Action", "PnL"} <= set(headers)
        if not metric_candidate and not trade_candidate:
            continue
        if len(headers) != len(set(headers)):
            raise PerformanceParseError("duplicate table header")
        if any(len(row) != len(headers) for row in rows):
            raise PerformanceParseError("table row width differs from its headers")
        if metric_candidate:
            keys = [row[0] for row in rows]
            if any(not key for key in keys) or len(keys) != len(set(keys)):
                raise PerformanceParseError("metrics contain duplicate or empty keys")
            metric_matches.append((len(rows), headers, tuple(keys)))
        if trade_candidate:
            timestamp_index = headers.index("Timestamp")
            trade_matches.append((len(rows), headers, tuple(_timestamp(row[timestamp_index]) for row in rows)))
    if settings_count != 1:
        raise PerformanceParseError("exactly one complete settings JSON object is required")
    if not metric_matches:
        raise PerformanceParseError("exactly one Metric/Value table is required")
    if len(trade_matches) != 1:
        raise PerformanceParseError("exactly one trade table is required")
    metric_headers = metric_matches[0][1]
    metric_count = 0
    metric_keys: set[str] = set()
    for count, headers, keys in metric_matches:
        if headers != metric_headers:
            raise PerformanceParseError("metric table headers differ")
        if metric_keys.intersection(keys):
            raise PerformanceParseError("metrics contain duplicate or empty keys")
        metric_keys.update(keys)
        metric_count += count
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


def parse_performance_report(source: bytes) -> ParsedPerformanceReport:
    if not isinstance(source, bytes):
        raise PerformanceParseError("source must be bytes")
    try:
        decoded = source.decode("utf-8", errors="strict")
        raw = _raw_inventory(decoded)
        document = html.fromstring(decoded)
    except (UnicodeDecodeError, etree.ParserError) as error:
        raise PerformanceParseError("malformed UTF-8 or HTML") from error
    settings = _settings(document)
    metrics, actions, metric_headers, trade_headers = _tables(document)
    wallet = _series(decoded, "walletSeries")
    equity = _series(decoded, "equitySeries")
    if len(wallet) != len(equity):
        raise PerformanceParseError("wallet/equity sample counts must match")
    action_times = tuple(_timestamp(row["Timestamp"]) for row in actions)
    wallet_times = tuple(_epoch_timestamp(point[0]) for point in wallet)
    equity_times = tuple(_epoch_timestamp(point[0]) for point in equity)
    if wallet_times != equity_times:
        raise PerformanceParseError("wallet/equity timestamps must match pairwise")
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
