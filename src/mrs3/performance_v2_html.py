"""Pure parser for the current tester Performance v2 HTML profile."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .performance_v2_store import PerformanceV2Config

from .performance import (
    PerformanceInventory,
    PerformanceParseError,
    _epoch_timestamp,
    _raw_markup,
    _timestamp,
    parse_performance_report,
    report_range,
)


class PerformanceV2HtmlError(ValueError):
    """Raised when a current Performance v2 report is not trustworthy."""


# Keep the alias useful to callers that describe this boundary as a parser.
PerformanceV2ParseError = PerformanceV2HtmlError

CURRENT_ACTION_HEADERS = (
    "Timestamp",
    "Symbol",
    "Order ID",
    "Action",
    "Fee",
    "PnL",
    "Balance",
    "Size",
    "Post Size",
    "Post Side",
)
_ACTION_HEADER_MARKERS = frozenset(("Timestamp", "Symbol", "Action", "PnL"))
_INTEGER = re.compile(r"^[+-]?\d+$")


class _PerformanceV2Limits(Protocol):
    max_html_bytes: int
    max_actions_per_report: int


@dataclass(frozen=True, slots=True)
class ParsedPerformanceV2Action:
    """One current-layout action with all fields converted to safe types."""

    action_index: int
    timestamp_utc: datetime
    symbol: str
    order_id: int
    action: str
    size: Decimal
    post_size: Decimal
    post_side: str
    pnl: Decimal
    fee: Decimal
    balance: Decimal

    @property
    def timestamp(self) -> datetime:
        return self.timestamp_utc


@dataclass(frozen=True, slots=True)
class ParsedPerformanceV2Report:
    """Immutable parser output safe to pass between worker processes."""

    settings: Mapping[str, object]
    metrics: Mapping[str, str]
    actions: tuple[ParsedPerformanceV2Action, ...]
    wallet_series: tuple[tuple[datetime, Decimal], ...]
    equity_series: tuple[tuple[datetime, Decimal], ...]
    inventory: PerformanceInventory
    reported_start_utc: datetime | None = None
    reported_end_utc: datetime | None = None
    listing_date_utc: datetime | None = None
    listing_date_raw: str | None = None
    listing_date_source: str | None = None
    effective_start_utc: datetime | None = None
    effective_end_utc: datetime | None = None
    warmup_hours: int | None = None
    excluded_trade_count: int = 0
    exclusion_reason: str | None = None


class _FrozenMapping(Mapping[str, object]):
    """Small immutable, pickleable mapping for process-pool results."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, object]) -> None:
        self._items = tuple(values.items())

    def __getitem__(self, key: str) -> object:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __reduce__(self):
        return type(self), (dict(self._items),)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenMapping({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerformanceV2HtmlError(f"{field} must be a finite Decimal") from error
    if not result.is_finite():
        raise PerformanceV2HtmlError(f"{field} must be a finite Decimal")
    return result


def _order_id(value: object) -> int:
    text = str(value).strip()
    if not _INTEGER.fullmatch(text):
        raise PerformanceV2HtmlError(f"Order ID must be an integer: {value!r}")
    result = int(text)
    if result < 1:
        raise PerformanceV2HtmlError("Order ID must be positive")
    return result


def _check_current_header(data: bytes) -> None:
    try:
        source = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise PerformanceV2HtmlError("malformed UTF-8 or HTML") from error
    _, tables = _raw_markup(source)
    candidates = [headers for headers, _rows in tables if _ACTION_HEADER_MARKERS <= set(headers)]
    if len(candidates) != 1:
        raise PerformanceV2HtmlError("exactly one current action table is required")
    headers = candidates[0]
    missing = [header for header in CURRENT_ACTION_HEADERS if header not in headers]
    if missing:
        raise PerformanceV2HtmlError(
            f"required current action header is missing: {missing[0]}"
        )


def _settings_identity(settings: Mapping[str, object]) -> str:
    exchange = settings.get("exchange")
    if not isinstance(exchange, Mapping) or exchange.get("use_upnl") is not True:
        raise PerformanceV2HtmlError("current Performance v2 reports require use_upnl=true")
    basic = settings.get("basic")
    if not isinstance(basic, Mapping):
        raise PerformanceV2HtmlError("settings basic metadata is missing")
    symbol = basic.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise PerformanceV2HtmlError("settings symbol is missing")

    return symbol.strip()


def _typed_actions(
    rows: tuple[dict[str, str], ...],
    symbol: str,
) -> tuple[ParsedPerformanceV2Action, ...]:
    result: list[ParsedPerformanceV2Action] = []
    for action_index, row in enumerate(rows):
        try:
            timestamp = _timestamp(row["Timestamp"])
        except PerformanceParseError as error:
            raise PerformanceV2HtmlError(str(error)) from error
        row_symbol = row["Symbol"].strip()
        if row_symbol != symbol:
            raise PerformanceV2HtmlError("action symbol does not match settings symbol")
        order_id = _order_id(row["Order ID"])
        action = row["Action"].strip().casefold()
        if not action:
            raise PerformanceV2HtmlError("action name is empty")
        fee = _decimal(row["Fee"], "Fee")
        pnl = _decimal(row["PnL"], "PnL")
        balance = _decimal(row["Balance"], "Balance")
        size = _decimal(row["Size"], "Size")
        post_size = _decimal(row["Post Size"], "Post Size")
        if post_size < 0:
            raise PerformanceV2HtmlError("Post Size must not be negative")
        post_side = row["Post Side"].strip().casefold()
        if post_size != 0 and not post_side:
            raise PerformanceV2HtmlError("Post Side is required for non-zero Post Size")
        result.append(
            ParsedPerformanceV2Action(
                action_index,
                timestamp,
                row_symbol,
                order_id,
                action,
                size,
                post_size,
                post_side,
                pnl,
                fee,
                balance,
            )
        )
    return tuple(sorted(result, key=lambda item: (item.timestamp_utc, item.action_index)))


def _typed_series(
    series: tuple[tuple[int, Decimal], ...],
) -> tuple[tuple[datetime, Decimal], ...]:
    return tuple((_epoch_timestamp(timestamp), value) for timestamp, value in series)


def _validate_report_integrity(
    metrics: Mapping[str, str],
    actions: tuple[ParsedPerformanceV2Action, ...],
    wallet_series: tuple[tuple[int, Decimal], ...],
    equity_series: tuple[tuple[int, Decimal], ...] = (),
) -> None:
    declared_transactions = metrics.get("Total transactions (buy/sell)")
    if declared_transactions is not None:
        if not declared_transactions.strip():
            raise PerformanceV2HtmlError("Total transactions (buy/sell) must not be blank")
        transactions = _decimal(declared_transactions, "Total transactions (buy/sell)")
        if transactions != transactions.to_integral_value() or int(transactions) != len(actions):
            raise PerformanceV2HtmlError(
                f"declared transaction count {transactions} does not match {len(actions)} action rows"
            )
    declared_final_text = metrics.get("Final balance")
    if declared_final_text is not None:
        if not declared_final_text.strip():
            raise PerformanceV2HtmlError("Final balance must not be blank")
        if not wallet_series:
            raise PerformanceV2HtmlError("Final balance requires a wallet sample")
        declared_final = _decimal(declared_final_text, "Final balance")
        quantum = Decimal(1).scaleb(declared_final.as_tuple().exponent)
        final_wallet = wallet_series[-1][1].quantize(quantum, rounding=ROUND_HALF_UP)
        if final_wallet != declared_final:
            raise PerformanceV2HtmlError(
                f"final wallet {final_wallet} does not match declared Final balance {declared_final}"
            )
    start, end = report_range(metrics)
    timestamps = [action.timestamp_utc for action in actions]
    timestamps.extend(_epoch_timestamp(timestamp) for timestamp, _value in wallet_series)
    timestamps.extend(_epoch_timestamp(timestamp) for timestamp, _value in equity_series)
    if any(timestamp < start or timestamp > end for timestamp in timestamps):
        raise PerformanceV2HtmlError("action/equity timestamp falls outside Report range")


def _validate_limits(limits: object) -> _PerformanceV2Limits:
    max_html_bytes = getattr(limits, "max_html_bytes", None)
    max_actions = getattr(limits, "max_actions_per_report", None)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (max_html_bytes, max_actions)
    ):
        raise PerformanceV2HtmlError("limits must provide positive integer limits")
    return limits  # type: ignore[return-value]


def parse_current_performance_v2_html(
    data: bytes, limits: PerformanceV2Config
) -> ParsedPerformanceV2Report:
    """Parse current tester bytes without opening paths or mutating state."""
    if not isinstance(data, bytes):
        raise PerformanceV2HtmlError("report data must be bytes")
    limits = _validate_limits(limits)
    if len(data) > limits.max_html_bytes:
        raise PerformanceV2HtmlError("HTML report exceeds max_html_bytes")
    _check_current_header(data)
    try:
        parsed = parse_performance_report(data)
    except PerformanceParseError as error:
        raise PerformanceV2HtmlError(str(error)) from error
    if len(parsed.actions) > limits.max_actions_per_report:
        raise PerformanceV2HtmlError("report exceeds action limit")
    symbol = _settings_identity(parsed.settings)
    actions = _typed_actions(parsed.actions, symbol)
    _validate_report_integrity(parsed.metrics, actions, parsed.wallet_series, parsed.equity_series)
    return ParsedPerformanceV2Report(
        _freeze(parsed.settings),  # type: ignore[arg-type]
        _FrozenMapping(parsed.metrics),
        actions,
        _typed_series(parsed.wallet_series),
        _typed_series(parsed.equity_series),
        parsed.inventory,
    )


__all__ = [
    "CURRENT_ACTION_HEADERS",
    "ParsedPerformanceV2Action",
    "ParsedPerformanceV2Report",
    "PerformanceV2HtmlError",
    "PerformanceV2ParseError",
    "parse_current_performance_v2_html",
]
