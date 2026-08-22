from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
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


def _raw_series_timestamps(source: str, name: str, *, allow_empty: bool = False) -> tuple[datetime, ...]:
    assignments = list(re.finditer(rf"\b(?:const|let|var)\s+{name}\s*=\s*", source))
    if len(assignments) != 1:
        raise PerformanceParseError(f"exactly one {name} assignment is required")
    try:
        raw = json.JSONDecoder().raw_decode(source[assignments[0].end():])[0]
    except json.JSONDecodeError as error:
        raise PerformanceParseError(f"malformed {name} array") from error
    if not isinstance(raw, list):
        raise PerformanceParseError(f"{name} must be non-empty")
    if not raw:
        # Z2: an explicitly empty array is a claim, not an absence. The caller
        # decides whether to honour it; a missing assignment stays fatal above.
        if not allow_empty:
            raise PerformanceParseError(f"{name} must be non-empty")
        return ()
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


def _series(source: str, name: str, *, allow_empty: bool = False) -> tuple[tuple[int, Decimal], ...]:
    assignments = list(re.finditer(rf"\b(?:const|let|var)\s+{name}\s*=\s*", source))
    if len(assignments) != 1:
        raise PerformanceParseError(f"exactly one {name} assignment is required")
    try:
        raw = json.JSONDecoder().raw_decode(source[assignments[0].end():])[0]
    except json.JSONDecodeError as error:
        raise PerformanceParseError(f"malformed {name} array") from error
    if not isinstance(raw, list):
        raise PerformanceParseError(f"{name} must be non-empty")
    if not raw:
        # Z2: an explicitly empty array is a claim, not an absence. The caller
        # decides whether to honour it; a missing assignment stays fatal above.
        if not allow_empty:
            raise PerformanceParseError(f"{name} must be non-empty")
        return ()
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


def _raw_inventory(source: str, *, allow_empty: bool = False) -> _RawInventory:
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
        _raw_series_timestamps(source, "walletSeries", allow_empty=allow_empty),
        _raw_series_timestamps(source, "equitySeries", allow_empty=allow_empty),
    )


# Z1: counters that directly contradict emptiness. Absent is a failure — a
# report that does not say how many trades it had has declared nothing.
_REQUIRED_ZERO_COUNTS = ("Total Trades", "Total transactions (buy/sell)")
# Corroboration. Absent is tolerated so a tester rename does not break the
# importer; present and non-zero is fatal.
_CORROBORATING_ZEROS = (
    "Win Trades", "Los Trades", "Total PnL", "Gross profit", "Gross loss",
    "Trading volume (USDT)", "Total fees", "Max Drawdown",
)
_EQUAL_BALANCES = ("Initial balance", "Final balance", "Min balance", "Max balance")
# Ratios whose denominator is zero without trades. This is the strongest single
# signal: a report whose series merely failed to render would still carry
# computed values here. ADR-0006 already fixed `n/a` as preserved meaning.
_UNDEFINED_RATIOS = (
    "Expectancy per trade",
    "Profit Factor (gross profit/gross loss)",
    "Risk/Reward (avg win/avg loss)",
    "Recovery Factor (Total PnL / Max DD)",
    "Sharpe ratio (monthly)",
    "Sortino ratio (monthly)",
    "Calmar ratio (CAGR / Max DD%)",
)


def report_range(metrics: Mapping[str, str]) -> tuple[datetime, datetime]:
    """Parse the `Report range` header into a half-open UTC day interval.

    The single parser for this header. `source_v6._report_period` builds its
    millisecond interval on top of it, so the window a fragment claims and the
    window this report describes can never drift apart.
    """
    raw = metrics.get("Report range")
    if not raw or " - " not in raw:
        raise PerformanceParseError("report is missing 'Report range'")
    start_text, end_text = (part.strip() for part in raw.split(" - ", 1))
    try:
        start = datetime.combine(date.fromisoformat(start_text), time.min, tzinfo=timezone.utc)
        end = datetime.combine(date.fromisoformat(end_text), time.min, tzinfo=timezone.utc)
    except ValueError as error:
        raise PerformanceParseError(f"invalid report range: {raw!r}") from error
    if end <= start:
        raise PerformanceParseError("report range end must be after start")
    return start, end


def _zero(value: str) -> bool:
    try:
        return Decimal(value.strip()) == 0
    except (InvalidOperation, AttributeError):
        return False


def _assert_declared_empty(metrics: Mapping[str, str]) -> None:
    """Require the report to state that nothing happened (ADR-0016, Z1).

    Absence of data is never accepted as evidence of emptiness: a truncated or
    corrupt report has no data either. Only an affirmative, mutually consistent
    declaration is.
    """
    for name in _REQUIRED_ZERO_COUNTS:
        if name not in metrics:
            raise PerformanceParseError(f"zero-activity report is missing '{name}'")
        if not _zero(metrics[name]):
            raise PerformanceParseError(f"zero-activity report reports a non-zero '{name}'")
    for name in _CORROBORATING_ZEROS:
        if name in metrics and not _zero(metrics[name]):
            raise PerformanceParseError(f"zero-activity report reports a non-zero '{name}'")
    balances = []
    for name in _EQUAL_BALANCES:
        if name not in metrics:
            continue
        try:
            value = Decimal(metrics[name].strip())
        except InvalidOperation as error:
            raise PerformanceParseError(f"zero-activity report has a malformed '{name}'") from error
        # `Decimal("nan") != Decimal("nan")`, so a NaN balance would otherwise
        # survive as a set of distinct members and be reported as a *changed*
        # balance — rejected, but diagnosed as the wrong defect.
        if not value.is_finite():
            raise PerformanceParseError(f"zero-activity report has a malformed '{name}'")
        balances.append(value)
    if len(set(balances)) > 1:
        raise PerformanceParseError("zero-activity report changes balance")
    for name in _UNDEFINED_RATIOS:
        if name in metrics and metrics[name].strip() != "n/a":
            raise PerformanceParseError(f"zero-activity report computes '{name}'")


def parse_performance_report(
    source: bytes, *, allow_zero_activity: bool = False
) -> ParsedPerformanceReport:
    """Parse one tester report into settings, metrics, actions and series.

    `allow_zero_activity` admits a run in which nothing happened — see ADR-0016
    and the Z1 criteria. It is opt-in because the v1 performance store derives
    DD5 candidates from this parser under ADR-0006, and admitting empty runs
    there would change that contract as a side effect. Only
    `normalize_source_v6` opts in.
    """
    if not isinstance(source, bytes):
        raise PerformanceParseError("source must be bytes")
    try:
        decoded = source.decode("utf-8", errors="strict")
        raw = _raw_inventory(decoded, allow_empty=allow_zero_activity)
        document = html.fromstring(decoded)
    except (UnicodeDecodeError, etree.ParserError) as error:
        raise PerformanceParseError("malformed UTF-8 or HTML") from error
    settings = _settings(document)
    metrics, actions, metric_headers, trade_headers = _tables(document)
    wallet = _series(decoded, "walletSeries", allow_empty=allow_zero_activity)
    equity = _series(decoded, "equitySeries", allow_empty=allow_zero_activity)
    if len(wallet) != len(equity):
        raise PerformanceParseError("wallet/equity sample counts must match")
    if allow_zero_activity and not wallet and not equity and actions:
        # Z2: zero has to be zero everywhere at once. Actions with no samples
        # is not a run in which nothing happened — it is a run whose samples
        # did not render, and the relaxation must not launder that into a
        # fragment with trades it cannot place in time.
        raise PerformanceParseError("walletSeries must be non-empty for a report with actions")
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
    if not all_times:
        # Z1/Z2: nothing observed at all. Emptiness has to be declared by the
        # report before it is believed, because a truncated file looks the same.
        if not (allow_zero_activity and not actions and not wallet and not equity):
            raise PerformanceParseError("report contains no actions or samples")
        _assert_declared_empty(metrics)
        # Z3: the window comes from the header. A sentinel such as the epoch
        # would propagate into interval comparisons, sorting and `test_run_id`.
        all_times = report_range(metrics)
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
