"""Pure, deterministic equity-quality facts for Performance v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    localcontext,
)
from typing import Iterable, Literal


ALGORITHM_VERSION = "equity-quality-r7.3-v1"
EPSILON = Decimal("1e-8")
ZERO = Decimal(0)
ONE = Decimal(1)
GRID_STEP = timedelta(hours=6)
WINDOW_DAYS = (7, 14, 28)
MAX_WINDOW_DAYS = 28
GRID_POINTS_28D = MAX_WINDOW_DAYS * 4 + 1
_METRIC_CONTEXT = Context(
    prec=38,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)

EquityQualityState = Literal[
    "UNKNOWN_INVALID_SOURCE",
    "NONPOSITIVE_EQUITY",
    "INSUFFICIENT_HISTORY",
    "MISSING_BASELINE",
    "GROWING",
    "WEAKENING",
    "FLAT",
    "DECLINING_OR_MIXED",
]
ErfDisposition = Literal["NOT_EVALUATED", "PASS", "BLOCK", "BLOCK_IF_ERF_ENABLED"]


@dataclass(frozen=True, slots=True)
class EquitySample:
    result_id: int
    sample_index: int
    timestamp_utc: datetime
    equity: object


@dataclass(frozen=True, slots=True)
class EquityWindowFacts:
    days: int
    start_utc: datetime
    end_utc: datetime
    trend30: Decimal
    endpoint30: Decimal
    return_pct: Decimal
    er: Decimal
    grid_points: int


@dataclass(frozen=True, slots=True)
class EquityQualityFacts:
    algo_version: str
    result_id: int
    report_start_utc: datetime | None
    report_end_utc: datetime | None
    state: EquityQualityState
    reason: str
    erf_disposition: ErfDisposition
    raw_sample_count: int
    in_report_sample_count: int
    nonpositive_in_report_rows: int
    duplicate_timestamp_count: int
    invalid_reasons: tuple[str, ...]
    available_baselines_days: tuple[int, ...]
    horizon_days: int | None
    equity_class: int | None
    windows: tuple[EquityWindowFacts, ...]
    drawdown: Decimal | None
    peak_gap: Decimal | None
    raw_h_path_points: int
    score12: Decimal | None

    def to_canonical_dict(self) -> dict[str, object]:
        """Return the fixed JSON-ready facts shape used by the cache layer."""
        return {
            "algo_version": self.algo_version,
            "result_id": self.result_id,
            "report_start_utc": _iso(self.report_start_utc),
            "report_end_utc": _iso(self.report_end_utc),
            "state": self.state,
            "reason": self.reason,
            "erf_disposition": self.erf_disposition,
            "raw_sample_count": self.raw_sample_count,
            "in_report_sample_count": self.in_report_sample_count,
            "nonpositive_in_report_rows": self.nonpositive_in_report_rows,
            "duplicate_timestamp_count": self.duplicate_timestamp_count,
            "invalid_reasons": list(self.invalid_reasons),
            "available_baselines_days": list(self.available_baselines_days),
            "horizon_days": self.horizon_days,
            "equity_class": self.equity_class,
            "windows": [
                {
                    "days": window.days,
                    "start_utc": _iso(window.start_utc),
                    "end_utc": _iso(window.end_utc),
                    "trend30": _decimal_text(window.trend30),
                    "endpoint30": _decimal_text(window.endpoint30),
                    "return_pct": _decimal_text(window.return_pct),
                    "er": _decimal_text(window.er),
                    "grid_points": window.grid_points,
                }
                for window in self.windows
            ],
            "drawdown": _decimal_text(self.drawdown),
            "peak_gap": _decimal_text(self.peak_gap),
            "raw_h_path_points": self.raw_h_path_points,
            "score12": _decimal_text(self.score12),
        }


@dataclass(frozen=True, slots=True)
class _Point:
    sample_index: int
    timestamp_utc: datetime
    equity: Decimal


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        offset = value.utcoffset()
        if offset != timedelta(0):
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return converted if converted.is_finite() else None


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _facts(
    *,
    result_id: int,
    start: datetime | None,
    end: datetime | None,
    state: EquityQualityState,
    reason: str,
    disposition: ErfDisposition,
    raw_count: int,
    in_report_count: int,
    nonpositive_count: int = 0,
    duplicate_timestamp_count: int = 0,
    invalid_reasons: Iterable[str] = (),
    baselines: Iterable[int] = (),
    horizon: int | None = None,
    equity_class: int | None = None,
    windows: Iterable[EquityWindowFacts] = (),
    drawdown: Decimal | None = None,
    peak_gap: Decimal | None = None,
    raw_h_path_points: int = 0,
    score12: Decimal | None = None,
) -> EquityQualityFacts:
    return EquityQualityFacts(
        ALGORITHM_VERSION,
        result_id,
        start,
        end,
        state,
        reason,
        disposition,
        raw_count,
        in_report_count,
        nonpositive_count,
        duplicate_timestamp_count,
        tuple(sorted(set(invalid_reasons))),
        tuple(baselines),
        horizon,
        equity_class,
        tuple(windows),
        drawdown,
        peak_gap,
        raw_h_path_points,
        score12,
    )


def _raw_risk(points: list[_Point], left: datetime, end: datetime, start_value: Decimal, end_value: Decimal) -> tuple[Decimal, Decimal, int]:
    raw = [point for point in points if left <= point.timestamp_utc <= end]
    has_left = any(point.timestamp_utc == left for point in raw)
    has_end = any(point.timestamp_utc == end for point in raw)
    path = ([] if has_left else [start_value]) + [point.equity for point in raw] + ([] if has_end else [end_value])
    peak = ZERO
    drawdown = ZERO
    for equity in path:
        peak = max(peak, equity)
        drawdown = max(drawdown, ONE - equity / peak)
    return drawdown, ONE - end_value / peak, len(path)


def _window_facts(
    days: int,
    end: datetime,
    grid_start_index: int,
    grid_equities: list[Decimal],
    grid_logs: list[Decimal],
) -> EquityWindowFacts:
    start_index = (MAX_WINDOW_DAYS - days) * 4 - grid_start_index
    count = days * 4 + 1
    values = grid_equities[start_index : start_index + count]
    logs = grid_logs[start_index : start_index + count]
    xs = [Decimal(index) / Decimal(4) for index in range(count)]
    n = Decimal(count)
    xbar = sum(xs, ZERO) / n
    ybar = sum(logs, ZERO) / n
    denominator = sum(((x - xbar) ** 2 for x in xs), ZERO)
    slope = sum(((x - xbar) * (y - ybar) for x, y in zip(xs, logs)), ZERO) / denominator
    path = sum((abs(right - left) for left, right in zip(logs, logs[1:])), ZERO)
    endpoint_log = logs[-1] - logs[0]
    er = ZERO if path == ZERO else endpoint_log / path
    returns = Decimal(100) * (values[-1] / values[0] - ONE)
    return EquityWindowFacts(
        days,
        end - timedelta(days=days),
        end,
        Decimal(3000) * slope,
        Decimal(3000) * endpoint_log / Decimal(days),
        returns,
        er,
        count,
    )


def calculate_equity_quality_facts(
    result_id: int,
    report_start_utc: datetime,
    report_end_utc: datetime,
    samples: Iterable[EquitySample],
) -> EquityQualityFacts:
    """Calculate facts from rows ordered by ``(timestamp_utc, sample_index)``.

    Production ``strategy_equity.equity`` is bounded upstream as
    ``DECIMAL(38,12)``. This pure engine adds no second magnitude clamp;
    Task 2's cache decoder owns decoded-value range validation.
    """
    start = _utc(report_start_utc)
    end = _utc(report_end_utc)
    invalid: set[str] = set()
    if start is None:
        invalid.add("INVALID_REPORT_START_UTC")
    if end is None:
        invalid.add("INVALID_REPORT_END_UTC")
    if start is not None and end is not None and end < start:
        invalid.add("INVALID_REPORT_RANGE")

    raw_samples = list(samples)
    parsed: list[_Point] = []
    seen_indices: set[int] = set()
    previous_key: tuple[datetime, int] | None = None
    in_report_count = 0
    nonpositive_count = 0
    for sample in raw_samples:
        if not isinstance(sample, EquitySample):
            invalid.add("MALFORMED_EQUITY_SAMPLE")
            continue
        if type(sample.result_id) is not int or sample.result_id != result_id:
            invalid.add("ROW_OWNERSHIP_MISMATCH")
        if type(sample.sample_index) is not int or sample.sample_index < 0:
            invalid.add("INVALID_SAMPLE_INDEX")
            sample_index = None
        else:
            sample_index = sample.sample_index
            if sample_index in seen_indices:
                invalid.add("DUPLICATE_SAMPLE_INDEX")
            seen_indices.add(sample_index)
        timestamp = _utc(sample.timestamp_utc)
        if timestamp is None:
            invalid.add("INVALID_EQUITY_TIMESTAMP_UTC")
        equity = _decimal(sample.equity)
        if equity is None:
            invalid.add("MALFORMED_OR_NONFINITE_EQUITY")
        if timestamp is not None and start is not None and end is not None:
            if start <= timestamp <= end:
                in_report_count += 1
                if equity is not None and equity <= ZERO:
                    nonpositive_count += 1
            else:
                invalid.add("EQUITY_OUTSIDE_REPORT_INTERVAL")
        if sample_index is not None and timestamp is not None and equity is not None:
            key = (timestamp, sample_index)
            if previous_key is not None and key < previous_key:
                invalid.add("UNORDERED_EQUITY_SOURCE")
            previous_key = key
            parsed.append(_Point(sample_index, timestamp, equity))
    ordered = parsed
    duplicate_timestamps = 0
    previous_timestamp: datetime | None = None
    for point in ordered:
        if start is not None and end is not None and start <= point.timestamp_utc <= end:
            if point.timestamp_utc == previous_timestamp:
                duplicate_timestamps += 1
            previous_timestamp = point.timestamp_utc

    if invalid:
        return _facts(
            result_id=result_id,
            start=start,
            end=end,
            state="UNKNOWN_INVALID_SOURCE",
            reason="INVALID_OR_OUT_OF_INTERVAL_SOURCE",
            disposition="NOT_EVALUATED",
            raw_count=len(raw_samples),
            in_report_count=in_report_count,
            nonpositive_count=nonpositive_count,
            duplicate_timestamp_count=duplicate_timestamps,
            invalid_reasons=invalid,
        )
    if nonpositive_count:
        return _facts(
            result_id=result_id,
            start=start,
            end=end,
            state="NONPOSITIVE_EQUITY",
            reason="NONPOSITIVE_IN_REPORT_EQUITY",
            disposition="BLOCK",
            raw_count=len(raw_samples),
            in_report_count=in_report_count,
            nonpositive_count=nonpositive_count,
            duplicate_timestamp_count=duplicate_timestamps,
        )

    assert start is not None and end is not None
    age = end - start
    grid_start = end - timedelta(days=MAX_WINDOW_DAYS)
    grid_times = [grid_start + GRID_STEP * index for index in range(GRID_POINTS_28D)]
    in_report = [point for point in ordered if start <= point.timestamp_utc <= end]
    grid_equities: list[Decimal | None] = []
    source_index = 0
    latest: Decimal | None = None
    for grid_time in grid_times:
        while source_index < len(in_report) and in_report[source_index].timestamp_utc <= grid_time:
            latest = in_report[source_index].equity
            source_index += 1
        grid_equities.append(latest)

    available = tuple(
        days
        for days in (28, 14, 7)
        if start <= end - timedelta(days=days)
        and grid_equities[(MAX_WINDOW_DAYS - days) * 4] is not None
    )
    if not available:
        state: EquityQualityState = "INSUFFICIENT_HISTORY" if age < timedelta(days=7) else "MISSING_BASELINE"
        return _facts(
            result_id=result_id,
            start=start,
            end=end,
            state=state,
            reason=state,
            disposition="NOT_EVALUATED",
            raw_count=len(raw_samples),
            in_report_count=in_report_count,
            duplicate_timestamp_count=duplicate_timestamps,
        )

    horizon = max(available)
    horizon_grid_start = (MAX_WINDOW_DAYS - horizon) * 4
    horizon_grid: list[Decimal] = [
        value for value in grid_equities[horizon_grid_start:] if value is not None
    ]
    try:
        with localcontext(_METRIC_CONTEXT) as context:
            context.clear_flags()
            reference = horizon_grid[0]
            horizon_logs = [(value / reference).ln() for value in horizon_grid]
            windows = tuple(
                _window_facts(days, end, horizon_grid_start, horizon_grid, horizon_logs)
                for days in WINDOW_DAYS
                if days <= horizon
            )
            main = next(window for window in windows if window.days == horizon)
            growth = min(main.trend30, main.endpoint30)
            horizon_up = main.trend30 > EPSILON and main.endpoint30 > EPSILON
            horizon_nondeclining = main.trend30 >= -EPSILON and main.endpoint30 >= -EPSILON
            horizon_flat = abs(main.trend30) <= EPSILON and abs(main.endpoint30) <= EPSILON

            if horizon_up:
                short_decline = any(
                    window.days < horizon
                    and (window.trend30 < -EPSILON or window.endpoint30 < -EPSILON)
                    for window in windows
                )
                state = "WEAKENING" if short_decline else "GROWING"
                equity_class = 1 if short_decline else 0
                disposition: ErfDisposition = "PASS"
                reason = "SHORT_WINDOW_DECLINE" if short_decline else "H_UP_SHORTS_NONDECLINING"
            elif horizon_flat:
                state = "FLAT"
                equity_class = 2
                disposition = "BLOCK_IF_ERF_ENABLED"
                reason = "H_FLAT"
            else:
                state = "DECLINING_OR_MIXED"
                equity_class = 2 if horizon_nondeclining else 3
                disposition = "BLOCK"
                reason = "H_NONDECLINING_NOT_UP" if horizon_nondeclining else "H_DECLINING_OR_MIXED"

            drawdown, peak_gap, raw_path_points = _raw_risk(
                in_report,
                end - timedelta(days=horizon),
                end,
                horizon_grid[0],
                horizon_grid[-1],
            )
            quality = max(ZERO, main.er)
            if growth > EPSILON:
                score = growth * quality * (ONE - drawdown) * (ONE - peak_gap)
            elif abs(growth) <= EPSILON:
                score = ZERO
            else:
                score = growth * (ONE + drawdown + peak_gap)
            score = score.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    except DecimalException:
        return _facts(
            result_id=result_id,
            start=start,
            end=end,
            state="UNKNOWN_INVALID_SOURCE",
            reason="DECIMAL_METRIC_ERROR",
            disposition="NOT_EVALUATED",
            raw_count=len(raw_samples),
            in_report_count=in_report_count,
            duplicate_timestamp_count=duplicate_timestamps,
            invalid_reasons=("DECIMAL_METRIC_ERROR",),
        )

    return _facts(
        result_id=result_id,
        start=start,
        end=end,
        state=state,
        reason=reason,
        disposition=disposition,
        raw_count=len(raw_samples),
        in_report_count=in_report_count,
        duplicate_timestamp_count=duplicate_timestamps,
        baselines=available,
        horizon=horizon,
        equity_class=equity_class,
        windows=windows,
        drawdown=drawdown,
        peak_gap=peak_gap,
        raw_h_path_points=raw_path_points,
        score12=score,
    )


__all__ = [
    "ALGORITHM_VERSION",
    "EPSILON",
    "EquityQualityFacts",
    "EquityQualityState",
    "EquitySample",
    "EquityWindowFacts",
    "ErfDisposition",
    "calculate_equity_quality_facts",
]
