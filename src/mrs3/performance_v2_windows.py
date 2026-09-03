"""Safe, versioned UPNL-relative windows for the Performance v2 store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import duckdb


METRICS_VERSION = "performance-window-v2.1"


class PerformanceV2WindowsError(ValueError):
    """Raised when a window request cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    result_id: int
    requested_start_utc: datetime
    requested_end_utc: datetime
    metrics_version: str
    effective_start_utc: datetime | None
    effective_end_utc: datetime | None
    availability_status: str
    unavailable_reason: str | None
    growth_factor: Decimal | None
    return_pct: Decimal | None
    daily_log_return: Decimal | None
    daily_growth_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    return_dd_ratio: Decimal | None
    fees_pct: Decimal | None
    profit_factor: Decimal | None
    trade_count: int | None
    win_rate_pct: Decimal | None
    holding_seconds: Decimal | None = None
    time_in_market_pct: Decimal | None = None

    @classmethod
    def unavailable(
        cls,
        result_id: int,
        requested_start_utc: datetime,
        requested_end_utc: datetime,
        reason: str,
        metrics_version: str = METRICS_VERSION,
    ) -> "WindowMetrics":
        return cls(
            result_id,
            requested_start_utc,
            requested_end_utc,
            metrics_version,
            None,
            None,
            "UNAVAILABLE",
            reason,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

    @property
    def available(self) -> bool:
        return self.availability_status == "AVAILABLE"

    @property
    def status(self) -> str:
        return self.availability_status

    # Short aliases keep callers independent from the storage column names.
    @property
    def requested_start(self) -> datetime:
        return self.requested_start_utc

    @property
    def requested_end(self) -> datetime:
        return self.requested_end_utc

    @property
    def effective_start(self) -> datetime | None:
        return self.effective_start_utc

    @property
    def effective_end(self) -> datetime | None:
        return self.effective_end_utc

    @property
    def return_dd(self) -> Decimal | None:
        return self.return_dd_ratio


@dataclass(frozen=True, slots=True)
class WindowPairComparison:
    status: str
    growth_factor_ratio: Decimal | None
    log_return_ratio: Decimal | None

    @property
    def available(self) -> bool:
        return self.status == "AVAILABLE"

    @property
    def reason(self) -> str | None:
        return None if self.available else self.status

    @property
    def growth_ratio(self) -> Decimal | None:
        return self.growth_factor_ratio


@dataclass(frozen=True, slots=True)
class _Action:
    index: int
    timestamp: datetime
    kind: str
    post_size: Decimal
    pnl: Decimal
    fee: Decimal


@dataclass(frozen=True, slots=True)
class _Equity:
    index: int
    timestamp: datetime
    wallet: Decimal
    equity: Decimal


@dataclass(slots=True)
class _RoundTrip:
    entries: list[_Action]
    realisations: list[_Action]


_METRIC_COLUMNS = (
    "result_id",
    "requested_start_utc",
    "requested_end_utc",
    "metrics_version",
    "effective_start_utc",
    "effective_end_utc",
    "availability_status",
    "unavailable_reason",
    "growth_factor",
    "return_pct",
    "daily_log_return",
    "daily_growth_pct",
    "max_drawdown_pct",
    "return_dd_ratio",
    "fees_pct",
    "profit_factor",
    "trade_count",
    "win_rate_pct",
    "holding_seconds",
    "time_in_market_pct",
)


def _utc(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError as error:
            raise PerformanceV2WindowsError("window timestamps must be ISO-8601") from error
    else:
        raise PerformanceV2WindowsError("window timestamps must be datetime or ISO-8601")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerformanceV2WindowsError(f"{field} must be finite") from error
    if not result.is_finite():
        raise PerformanceV2WindowsError(f"{field} must be finite")
    return result


def calendar_window_days(
    metrics: WindowMetrics,
    report_start_utc: datetime | None = None,
    report_end_utc: datetime | None = None,
) -> Decimal | None:
    """Calendar denominator for rate metrics; event series may end while idle."""
    start = max(metrics.requested_start_utc, _utc(report_start_utc)) if report_start_utc else metrics.requested_start_utc
    end = min(metrics.requested_end_utc, _utc(report_end_utc)) if report_end_utc else metrics.requested_end_utc
    seconds = Decimal(str((end - start).total_seconds()))
    return seconds / Decimal(86_400) if seconds > 0 else None


def _metric_from_row(row: tuple[Any, ...]) -> WindowMetrics:
    values = list(row)
    for index in (8, 9, 10, 11, 12, 13, 14, 15, 17, 18, 19):
        if values[index] is not None:
            values[index] = _decimal(values[index], _METRIC_COLUMNS[index])
    if values[16] is not None:
        values[16] = int(values[16])
    values[1] = _utc(values[1])
    values[2] = _utc(values[2])
    values[4] = None if values[4] is None else _utc(values[4])
    values[5] = None if values[5] is None else _utc(values[5])
    return WindowMetrics(*values)


def _cached(
    connection: duckdb.DuckDBPyConnection,
    result_id: int,
    start: datetime,
    end: datetime,
    version: str,
) -> WindowMetrics | None:
    row = connection.execute(
        "select " + ", ".join(_METRIC_COLUMNS) + " from window_metrics "
        "where result_id = ? and requested_start_utc = ? and requested_end_utc = ? and metrics_version = ?",
        [result_id, start, end, version],
    ).fetchone()
    return None if row is None else _metric_from_row(row)


def _load_source(
    connection: duckdb.DuckDBPyConnection, result_id: int
) -> tuple[datetime, datetime, tuple[_Action, ...], tuple[_Equity, ...]]:
    result = connection.execute(
        "select report_start_utc, report_end_utc from strategy_results where result_id = ?",
        [result_id],
    ).fetchone()
    if result is None:
        raise PerformanceV2WindowsError(f"unknown result_id {result_id}")
    report_start, report_end = _utc(result[0]), _utc(result[1])
    actions = tuple(
        _Action(int(row[0]), _utc(row[1]), str(row[2]).casefold(), _decimal(row[3], "post_size"), _decimal(row[4], "pnl"), _decimal(row[5], "fee"))
        for row in connection.execute(
            "select action_index, timestamp_utc, action, post_size, pnl, fee "
            "from strategy_actions where result_id = ? order by timestamp_utc, action_index",
            [result_id],
        ).fetchall()
    )
    equity = tuple(
        _Equity(int(row[0]), _utc(row[1]), _decimal(row[2], "wallet"), _decimal(row[3], "equity"))
        for row in connection.execute(
            "select sample_index, timestamp_utc, wallet, equity from strategy_equity "
            "where result_id = ? order by timestamp_utc, sample_index",
            [result_id],
        ).fetchall()
    )
    return report_start, report_end, actions, equity


def _flat_samples(equity: tuple[_Equity, ...], actions: tuple[_Action, ...]) -> tuple[datetime, ...]:
    if not actions:
        return tuple(sample.timestamp for sample in equity)
    flat: list[datetime] = []
    action_index = 0
    last: _Action | None = None
    for sample in equity:
        while action_index < len(actions) and actions[action_index].timestamp <= sample.timestamp:
            last = actions[action_index]
            action_index += 1
        if last is not None and last.post_size == 0:
            flat.append(sample.timestamp)
    return tuple(flat)


def _round_trips(actions: tuple[_Action, ...]) -> tuple[_RoundTrip, ...]:
    trips: list[_RoundTrip] = []
    current: _RoundTrip | None = None
    for action in actions:
        if action.kind in {"opened", "increased"}:
            if current is None:
                current = _RoundTrip([], [])
            current.entries.append(action)
        elif action.kind in {"decreased", "closed"}:
            if current is None:
                current = _RoundTrip([], [])
            current.realisations.append(action)
        if current is not None and action.post_size == 0:
            if current.realisations:
                trips.append(current)
            current = None
    if current is not None and current.realisations:
        trips.append(current)
    return tuple(trips)


def _calculate(
    result_id: int,
    start: datetime,
    end: datetime,
    version: str,
    report_start: datetime,
    report_end: datetime,
    actions: tuple[_Action, ...],
    equity: tuple[_Equity, ...],
) -> WindowMetrics:
    if end < report_start or start > report_end or end < start or not equity:
        return WindowMetrics.unavailable(result_id, start, end, "OUT_OF_RANGE", version)
    flat = _flat_samples(equity, actions)
    effective_start = next((value for value in flat if value >= start), None)
    effective_end = next((value for value in reversed(flat) if value <= end), None)
    if effective_start is None:
        return WindowMetrics.unavailable(result_id, start, end, "NO_FLAT_START", version)
    if effective_end is None:
        return WindowMetrics.unavailable(result_id, start, end, "NO_FLAT_END", version)
    if effective_start >= effective_end:
        return WindowMetrics(
            result_id, start, end, version, effective_start, effective_end, "UNAVAILABLE", "COLLAPSED",
            None, None, None, None, None, None, None, None, None, None,
        )

    samples = tuple(item for item in equity if effective_start <= item.timestamp <= effective_end)
    if len(samples) < 2:
        return WindowMetrics(
            result_id, start, end, version, effective_start, effective_end, "UNAVAILABLE", "COLLAPSED",
            None, None, None, None, None, None, None, None, None, None,
        )
    # The flat action at W0 established the wallet/equity baseline; its fee and
    # realised PnL belong to the preceding interval, not this window.
    scoped_actions = tuple(item for item in actions if effective_start < item.timestamp <= effective_end)
    realising = tuple(item for item in scoped_actions if item.kind in {"decreased", "closed"})
    if not realising:
        return WindowMetrics(
            result_id, start, end, version, effective_start, effective_end, "UNAVAILABLE", "NO_TRADES",
            None, None, None, None, None, None, None, None, None, None,
        )

    # UPNL-aware returns are measured against wallet capital.  Equity remains
    # the mark-to-market series used only for drawdown.
    baseline = samples[0].wallet
    final = samples[-1].wallet
    if baseline == 0:
        growth = return_pct = daily_log = daily_growth = return_dd = fees_pct = None
    else:
        growth = final / baseline
        return_pct = (growth - 1) * 100
        days = Decimal(str((effective_end - effective_start).total_seconds())) / Decimal("86400")
        daily_log = growth.ln() / days if growth > 0 and days > 0 else None
        daily_growth = (daily_log.exp() - 1) * 100 if daily_log is not None else None
        fees_pct = sum((item.fee for item in scoped_actions), Decimal(0)) / baseline * 100
        return_dd = None

    peak = samples[0].equity
    max_drawdown_pct = Decimal(0)
    for sample in samples:
        peak = max(peak, sample.equity)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, (peak - sample.equity) / peak * 100)
    if baseline != 0 and max_drawdown_pct != 0:
        return_dd = return_pct / max_drawdown_pct

    gross_profit = sum((item.pnl for item in realising if item.pnl > 0), Decimal(0))
    gross_loss = -sum((item.pnl for item in realising if item.pnl < 0), Decimal(0))
    profit_factor = gross_profit / gross_loss if gross_loss else None
    trips = _round_trips(scoped_actions)
    wins = sum(sum((item.pnl for item in trip.realisations), Decimal(0)) > 0 for trip in trips)
    losses = sum(sum((item.pnl for item in trip.realisations), Decimal(0)) < 0 for trip in trips)
    win_rate = Decimal(wins) / Decimal(wins + losses) * 100 if wins + losses else None
    holding = sum(
        (trip.realisations[-1].timestamp - trip.entries[0].timestamp).total_seconds()
        for trip in trips
        if trip.entries and trip.realisations
    )
    duration = Decimal(str((effective_end - effective_start).total_seconds()))
    holding_seconds = Decimal(str(max(holding, 0)))
    time_in_market = holding_seconds / duration * 100 if duration > 0 else None
    return WindowMetrics(
        result_id, start, end, version, effective_start, effective_end, "AVAILABLE", None,
        growth, return_pct, daily_log, daily_growth, max_drawdown_pct, return_dd,
        fees_pct, profit_factor, len(trips), win_rate, holding_seconds, time_in_market,
    )


def _persist(connection: duckdb.DuckDBPyConnection, metrics: WindowMetrics) -> None:
    columns = _METRIC_COLUMNS + ("calculated_at_utc",)
    connection.execute(
        "insert into window_metrics (" + ", ".join(columns) + ") values (" + ", ".join("?" for _ in columns) + ") "
        "on conflict (result_id, requested_start_utc, requested_end_utc, metrics_version) do update set "
        + ", ".join(f"{column} = excluded.{column}" for column in _METRIC_COLUMNS[4:])
        + ", calculated_at_utc = excluded.calculated_at_utc",
        [getattr(metrics, column) for column in _METRIC_COLUMNS] + [datetime.now(timezone.utc)],
    )


def get_or_calculate_window(
    connection: duckdb.DuckDBPyConnection,
    result_id: int,
    requested_start_utc: datetime | str,
    requested_end_utc: datetime | str,
    *,
    calculator_version: str = METRICS_VERSION,
    metrics_version: str | None = None,
) -> WindowMetrics:
    """Read a cached safe window or calculate and cache it once."""
    if not isinstance(connection, duckdb.DuckDBPyConnection):
        raise TypeError("connection must be a DuckDB connection")
    if isinstance(result_id, bool) or not isinstance(result_id, int):
        raise TypeError("result_id must be an integer")
    version = metrics_version if metrics_version is not None else calculator_version
    if not isinstance(version, str) or not version.strip():
        raise ValueError("calculator_version must be non-empty")
    version = version.strip()
    start, end = _utc(requested_start_utc), _utc(requested_end_utc)
    cached = _cached(connection, result_id, start, end, version)
    if cached is not None:
        return cached
    source = _load_source(connection, result_id)
    metrics = _calculate(result_id, start, end, version, *source)
    _persist(connection, metrics)
    persisted = _cached(connection, result_id, start, end, version)
    return metrics if persisted is None else persisted


def get_or_calculate_window_pair(
    connection: duckdb.DuckDBPyConnection,
    result_id: int,
    window_a: tuple[datetime | str, datetime | str] | datetime | str,
    window_b: tuple[datetime | str, datetime | str] | datetime | str,
    *window_parts: datetime | str,
    calculator_version: str = METRICS_VERSION,
    metrics_version: str | None = None,
) -> tuple[WindowMetrics, WindowMetrics]:
    """Calculate/cache two independent windows, including overlapping windows."""
    if window_parts:
        if len(window_parts) != 2 or isinstance(window_a, (tuple, list)) or isinstance(window_b, (tuple, list)):
            raise ValueError("pair arguments must be two windows or four timestamps")
        window_a, window_b = (window_a, window_b), (window_parts[0], window_parts[1])
    if not isinstance(window_a, (tuple, list)) or not isinstance(window_b, (tuple, list)):
        raise ValueError("each window must contain start and end")
    if len(window_a) != 2 or len(window_b) != 2:
        raise ValueError("each window must contain start and end")
    return (
        get_or_calculate_window(connection, result_id, window_a[0], window_a[1], calculator_version=calculator_version, metrics_version=metrics_version),
        get_or_calculate_window(connection, result_id, window_b[0], window_b[1], calculator_version=calculator_version, metrics_version=metrics_version),
    )


def compare_window_pair_geometrically(
    window_a: WindowMetrics | tuple[WindowMetrics, WindowMetrics],
    window_b: WindowMetrics | None = None,
) -> WindowPairComparison:
    """Compare B against A only through positive geometric ratios."""
    if window_b is None:
        if not isinstance(window_a, (tuple, list)) or len(window_a) != 2:
            raise TypeError("window pair must contain A and B")
        window_a, window_b = window_a
    if not window_a.available or not window_b.available:
        return WindowPairComparison("WINDOW_NOT_AVAILABLE", None, None)
    growth_a, growth_b = window_a.growth_factor, window_b.growth_factor
    log_a, log_b = window_a.daily_log_return, window_b.daily_log_return
    if growth_a is None or growth_a == 0:
        return WindowPairComparison("UNDEFINED_ZERO_BASELINE", None, None)
    if growth_b is None or growth_a < 0 or growth_b is None or growth_b <= 0:
        return WindowPairComparison("UNDEFINED_NON_POSITIVE_INPUT", None, None)
    if log_a is None or log_b is None:
        return WindowPairComparison("UNDEFINED_NON_POSITIVE_INPUT", None, None)
    return WindowPairComparison("AVAILABLE", growth_b / growth_a, (log_b - log_a).exp())


__all__ = [
    "METRICS_VERSION",
    "PerformanceV2WindowsError",
    "WindowMetrics",
    "WindowPairComparison",
    "calendar_window_days",
    "compare_window_pair_geometrically",
    "get_or_calculate_window",
    "get_or_calculate_window_pair",
]
