"""Pure parser for the current tester Performance v2 HTML profile."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
REPORT_FORMAT_LEGACY = "legacy"
REPORT_FORMAT_2026_10 = "2026-10"
_FORMAT_2026_10_MIN_VERSION = (2026, 10, 10)
_TESTER_VERSION = re.compile(r"hb/tester/(?:\s|<[^>]*>)*ver\.(\d{4})/(\d{2})/(\d{2})")
_UNAVAILABLE = frozenset(("", "n/a", "—", "-"))
# Non-finite analytics (a short test can print an infinite ratio) are unavailable, not invalid.
_NON_FINITE = frozenset(("inf", "+inf", "-inf", "infinity", "-infinity", "∞", "-∞", "nan"))
_PERCENT_DECIMAL = re.compile(r"^([+-]?\d+(?:\.\d+)?)%?$")
_DURATION = re.compile(r"^(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$")
_COUNT_OF = re.compile(r"^(\d+) of \d+(?: \([\d.]+%\))?$")
_ANALYTICS_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)(?: UTC)?$")
_INTEGER_TEXT = re.compile(r"^\d+$")
# One explicit label map (spec 2026-10-12-performance-db-new-report-format §3.1, §5.2).
REPORT_ANALYTICS_LABELS: tuple[tuple[str, str, str], ...] = (
    ("gross_profit_usdt", "Gross profit", "decimal"),
    ("gross_loss_usdt", "Gross loss", "decimal"),
    ("trading_volume_usdt", "Trading volume (USDT)", "decimal"),
    ("funding_paid_usdt", "Funding paid", "decimal"),
    ("funding_accrued_usdt", "Funding accrued (received)", "decimal"),
    ("funding_net_usdt", "Funding net", "decimal"),
    ("net_costs_usdt", "Net costs (fees \u2212 net funding)", "decimal"),
    ("pnl_before_costs_usdt", "PnL before costs", "decimal"),
    ("top5_winning_pnl_usdt", "Top 5 winning trades PnL", "decimal"),
    ("pnl_without_top5_usdt", "PnL without top 5 winning trades", "decimal"),
    ("avg_mae_usdt", "Avg MAE (worst open PnL), all trades", "decimal"),
    ("avg_mae_winners_usdt", "Avg MAE of winning trades", "decimal"),
    ("worst_mae_usdt", "Worst MAE", "decimal"),
    ("avg_mfe_usdt", "Avg MFE (best open PnL)", "decimal"),
    ("profit_capture_winners_pct", "Profit capture of winners (PnL / MFE), %", "decimal"),
    ("losing_trades_in_profit", "Losing trades that were in profit", "count_of"),
    ("max_unrealized_dd_pct_wallet", "Max Unrealized Drawdown, % of wallet", "decimal"),
    ("max_unrealized_dd_usdt", "Max Unrealized Drawdown \u2014 amount", "decimal"),
    ("max_unrealized_dd_at_utc", "Max Unrealized Drawdown \u2014 time (UTC)", "timestamp"),
    ("buy_hold_return_pct", "Buy & Hold return (equal weight), %", "decimal"),
    ("excess_return_pct", "Excess return vs Buy & Hold, %", "decimal"),
    ("buy_hold_max_dd_daily_pct", "Buy & Hold Max Drawdown (daily), %", "decimal"),
    ("trades_full_exit", "Total Trades (entry \u2192 full exit)", "integer"),
    ("win_trades", "Win Trades", "integer"),
    ("loss_trades", "Los Trades", "integer"),
    ("expectancy_per_trade_usdt", "Expectancy per trade (net of fees)", "decimal"),
    ("avg_win_usdt", "Avg win", "decimal"),
    ("avg_loss_usdt", "Avg loss", "decimal"),
    ("payoff_ratio", "Payoff ratio (avg win / avg loss)", "decimal"),
    ("largest_win_usdt", "Largest win", "decimal"),
    ("largest_loss_usdt", "Largest loss", "decimal"),
    ("avg_fills_per_trade", "Avg fills per trade", "decimal"),
    ("avg_holding_seconds", "Avg holding time", "duration"),
    ("max_holding_seconds", "Max holding time", "duration"),
    ("max_consecutive_wins", "Max consecutive wins", "integer"),
    ("max_consecutive_losses", "Max consecutive losses", "integer"),
    ("fills_reduce_close", "Fills (reduce / close)", "integer"),
    ("win_fills", "Win Fills", "integer"),
    ("loss_fills", "Los Fills", "integer"),
    ("ulcer_index_hourly_pct", "Ulcer Index (hourly), %", "decimal"),
    ("martin_ratio", "Martin ratio (CAGR / Ulcer Index)", "decimal"),
    ("time_under_water_pct", "Time under water, %", "decimal"),
    ("drawdown_episodes", "Drawdown episodes", "integer"),
    ("longest_drawdown_seconds", "Longest drawdown (peak \u2192 recovery)", "duration"),
    ("var95_daily_pct", "VaR 95%, daily (historical)", "decimal"),
    ("cvar95_daily_pct", "CVaR 95% (avg of worst 5% days)", "decimal"),
    ("worst_day_pct", "Worst day, %", "decimal"),
    ("best_day_pct", "Best day, %", "decimal"),
    ("skewness", "Skewness", "decimal"),
    ("excess_kurtosis", "Excess kurtosis (fat tails > 0)", "decimal"),
    ("sharpe_monthly", "Sharpe ratio (monthly)", "decimal"),
    ("sortino_monthly", "Sortino ratio (monthly)", "decimal"),
    ("cagr_pct", "CAGR, %", "decimal"),
    ("calmar_ratio", "Calmar ratio (CAGR / Max DD%)", "decimal"),
    ("recovery_factor", "Recovery Factor (Total PnL / Max DD)", "decimal"),
    ("avg_monthly_return_geo_pct", "Average monthly return (geometric), %", "decimal"),
    ("avg_monthly_return_arith_pct", "Average monthly return (arithmetic), %", "decimal"),
    ("best_month_pct", "Best month, %", "decimal"),
    ("worst_month_pct", "Worst month, %", "decimal"),
)
REPORT_REGIME_HEADERS = (
    "Regime", "Pair-days", "PnL", "Avg PnL per pair-day", "Winning days, % of active", "Share of PnL",
)
REPORT_REGIME_LABELS = {
    "Trend \u00b7 High vol": "TREND_HIGH_VOL",
    "Trend \u00b7 Low vol": "TREND_LOW_VOL",
    "Flat \u00b7 High vol": "FLAT_HIGH_VOL",
    "Flat \u00b7 Low vol": "FLAT_LOW_VOL",
    "Trend (all)": "TREND",
    "Flat (all)": "FLAT",
    "High vol (all)": "HIGH_VOL",
    "Low vol (all)": "LOW_VOL",
    "\u2197 Uptrend": "UPTREND",
    "\u2198 Downtrend": "DOWNTREND",
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_INTEGER = re.compile(r"^[+-]?\d+$")
_MAX_OPTIONAL_DECIMAL_TEXT = 256


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
    price: Decimal | None = None
    cost: Decimal | None = None
    invalid_optional_fields: tuple[str, ...] = ()

    @property
    def timestamp(self) -> datetime:
        return self.timestamp_utc


@dataclass(frozen=True, slots=True)
class ParsedReportRegime:
    """One market-regime row of the 2026-10 report."""

    regime: str
    pair_days: int
    pnl_usdt: Decimal
    avg_pnl_per_pair_day_usdt: Decimal | None
    winning_days_pct: Decimal | None
    pnl_share_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class ParsedReportAnalytics:
    """Typed report analytics of the 2026-10 format; absent labels are None."""

    metrics: Mapping[str, object]
    regimes: tuple[ParsedReportRegime, ...]
    monthly: tuple[tuple[date, Decimal], ...]


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
    tester_version: str | None = None
    report_format: str = REPORT_FORMAT_LEGACY
    analytics: ParsedReportAnalytics | None = None


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


def _optional_decimal(row: Mapping[str, str], field: str) -> tuple[Decimal | None, bool]:
    """Read an optional execution decimal without rejecting the core report."""
    if field not in row or not row[field].strip():
        return None, False
    text = row[field].strip()
    if len(text) > _MAX_OPTIONAL_DECIMAL_TEXT:
        return None, True
    try:
        return _decimal(text, field), False
    except PerformanceV2HtmlError:
        return None, True


def _order_id(value: object) -> int:
    text = str(value).strip()
    if not _INTEGER.fullmatch(text):
        raise PerformanceV2HtmlError(f"Order ID must be an integer: {value!r}")
    result = int(text)
    if result < 1:
        raise PerformanceV2HtmlError("Order ID must be positive")
    return result


def _check_current_header(data: bytes) -> tuple[str, list[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]]]:
    """Check the action header and return the decoded source and its raw tables."""
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
    return source, tables


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
        post_side = row["Post Side"].strip().casefold()
        if post_size != 0 and not post_side:
            raise PerformanceV2HtmlError("Post Side is required for non-zero Post Size")
        if (post_size == 0 and post_side) or (post_size != 0 and (
            post_side not in {"long", "short"}
            or (post_size > 0 and post_side != "long")
            or (post_size < 0 and post_side != "short")
        )):
            raise PerformanceV2HtmlError("Post Size sign is inconsistent with Post Side")
        price, invalid_price = _optional_decimal(row, "Price")
        cost, invalid_cost = _optional_decimal(row, "Cost")
        invalid_optional_fields = tuple(
            field for field, invalid in (("Price", invalid_price), ("Cost", invalid_cost)) if invalid
        )
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
                price,
                cost,
                invalid_optional_fields,
            )
        )
    return tuple(result)


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
        latest_action = max(actions, key=lambda item: (item.timestamp_utc, item.action_index), default=None)
        if latest_action is not None and latest_action.timestamp_utc > _epoch_timestamp(wallet_series[-1][0]):
            final_wallet = latest_action.balance.quantize(quantum, rounding=ROUND_HALF_UP)
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


def _tester_version(source: str) -> str | None:
    versions = {match.groups() for match in _TESTER_VERSION.finditer(source)}
    if len(versions) > 1:
        raise PerformanceV2HtmlError("report declares more than one tester version")
    if not versions:
        return None
    year, month, day = next(iter(versions))
    return f"{year}/{month}/{day}"


def _report_format(version: str | None) -> str:
    if version is None:
        return REPORT_FORMAT_LEGACY
    parts = tuple(int(part) for part in version.split("/"))
    return REPORT_FORMAT_2026_10 if parts >= _FORMAT_2026_10_MIN_VERSION else REPORT_FORMAT_LEGACY


def _analytics_invalid(label: str) -> PerformanceV2HtmlError:
    return PerformanceV2HtmlError(f"REPORT_ANALYTICS_INVALID: {label}")


def _analytics_value(text: str, kind: str, label: str) -> object:
    value = text.strip()
    folded = value.casefold()
    if folded in _UNAVAILABLE or folded in _NON_FINITE or folded.startswith("n/a"):
        return None
    if kind == "decimal":
        match = _PERCENT_DECIMAL.fullmatch(value)
        if match is None:
            raise _analytics_invalid(label)
        return Decimal(match.group(1))
    if kind == "integer":
        if not _INTEGER_TEXT.fullmatch(value):
            raise _analytics_invalid(label)
        return int(value)
    if kind == "count_of":
        match = _COUNT_OF.fullmatch(value)
        if match is None:
            raise _analytics_invalid(label)
        return int(match.group(1))
    if kind == "duration":
        if value.endswith("(ongoing)"):
            value = value[: -len("(ongoing)")].strip()
        match = _DURATION.fullmatch(value)
        if not value or match is None or not any(match.groups()):
            raise _analytics_invalid(label)
        days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
        return ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    if kind == "timestamp":
        match = _ANALYTICS_TIMESTAMP.fullmatch(value)
        if match is None:
            raise _analytics_invalid(label)
        stamp = match.group(1)
        layout = "%Y-%m-%d %H:%M:%S" if stamp.count(":") == 2 else "%Y-%m-%d %H:%M"
        return datetime.strptime(stamp, layout).replace(tzinfo=timezone.utc)
    raise ValueError(f"unknown analytics kind: {kind}")


def _report_regimes(tables: list[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]]) -> tuple[ParsedReportRegime, ...]:
    candidates = [rows for headers, rows in tables if headers == REPORT_REGIME_HEADERS]
    if len(candidates) > 1:
        raise PerformanceV2HtmlError("report contains more than one regime table")
    regimes: list[ParsedReportRegime] = []
    seen: set[str] = set()
    for row in candidates[0] if candidates else ():
        if len(row) <= 1:
            continue  # merged separator row ("Totals by one dimension") or an empty row
        if len(row) != len(REPORT_REGIME_HEADERS):
            raise _analytics_invalid(f"Regime {row[0]}")
        if row[0] not in REPORT_REGIME_LABELS:
            continue
        regime = REPORT_REGIME_LABELS[row[0]]
        if regime in seen:
            raise PerformanceV2HtmlError(f"duplicate regime row: {row[0]}")
        seen.add(regime)
        label = f"Regime {row[0]}"
        pair_days = _analytics_value(row[1], "integer", label)
        pnl = _analytics_value(row[2], "decimal", label)
        if pair_days is None or pnl is None:
            raise _analytics_invalid(label)
        regimes.append(ParsedReportRegime(
            regime, pair_days, pnl,  # type: ignore[arg-type]
            _analytics_value(row[3], "decimal", label),  # type: ignore[arg-type]
            _analytics_value(row[4], "decimal", label),  # type: ignore[arg-type]
            _analytics_value(row[5], "decimal", label),  # type: ignore[arg-type]
        ))
    return tuple(regimes)


def _report_monthly(tables: list[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]]) -> tuple[tuple[date, Decimal], ...]:
    candidates = [
        (headers, rows) for headers, rows in tables
        if len(headers) > 1 and headers[0] == "Year" and headers[1] in _MONTHS
    ]
    if len(candidates) > 1:
        raise PerformanceV2HtmlError("report contains more than one monthly returns table")
    monthly: list[tuple[date, Decimal]] = []
    years: set[int] = set()
    for headers, rows in candidates:
        if headers[-1] != "Year" or not set(headers[1:-1]) <= set(_MONTHS) or len(set(headers[1:-1])) != len(headers) - 2:
            raise _analytics_invalid("Monthly Returns Heatmap")
        for row in rows:
            if len(row) != len(headers) or not _INTEGER_TEXT.fullmatch(row[0]):
                raise _analytics_invalid("Monthly Returns Heatmap")
            year = int(row[0])
            if year in years:
                raise _analytics_invalid(f"Monthly return {year}")
            years.add(year)
            for header, cell in zip(headers[1:-1], row[1:-1], strict=True):
                value = _analytics_value(cell, "decimal", f"Monthly return {year} {header}")
                if value is not None:
                    monthly.append((date(year, _MONTHS.index(header) + 1, 1), value))  # type: ignore[arg-type]
    return tuple(sorted(monthly))


def _report_analytics(
    metrics: Mapping[str, str], tables: list[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]],
) -> ParsedReportAnalytics:
    values = {
        column: (_analytics_value(metrics[label], kind, label) if label in metrics else None)
        for column, label, kind in REPORT_ANALYTICS_LABELS
    }
    return ParsedReportAnalytics(_FrozenMapping(values), _report_regimes(tables), _report_monthly(tables))


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
    source, raw_tables = _check_current_header(data)
    tester_version = _tester_version(source)
    report_format = _report_format(tester_version)
    try:
        parsed = parse_performance_report(
            data, report_format_2026_10=report_format == REPORT_FORMAT_2026_10,
        )
    except PerformanceParseError as error:
        raise PerformanceV2HtmlError(str(error)) from error
    if len(parsed.actions) > limits.max_actions_per_report:
        raise PerformanceV2HtmlError("report exceeds action limit")
    symbol = _settings_identity(parsed.settings)
    actions = _typed_actions(parsed.actions, symbol)
    _validate_report_integrity(parsed.metrics, actions, parsed.wallet_series, parsed.equity_series)
    analytics = (
        _report_analytics(parsed.metrics, raw_tables) if report_format == REPORT_FORMAT_2026_10 else None
    )
    return ParsedPerformanceV2Report(
        _freeze(parsed.settings),  # type: ignore[arg-type]
        _FrozenMapping(parsed.metrics),
        actions,
        _typed_series(parsed.wallet_series),
        _typed_series(parsed.equity_series),
        parsed.inventory,
        tester_version=tester_version,
        report_format=report_format,
        analytics=analytics,
    )


__all__ = [
    "CURRENT_ACTION_HEADERS",
    "REPORT_ANALYTICS_LABELS",
    "REPORT_FORMAT_2026_10",
    "REPORT_FORMAT_LEGACY",
    "REPORT_REGIME_LABELS",
    "ParsedPerformanceV2Action",
    "ParsedPerformanceV2Report",
    "ParsedReportAnalytics",
    "ParsedReportRegime",
    "PerformanceV2HtmlError",
    "PerformanceV2ParseError",
    "parse_current_performance_v2_html",
]
