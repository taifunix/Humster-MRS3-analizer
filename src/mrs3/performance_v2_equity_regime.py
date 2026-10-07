"""Pure production equity-regime classifier for one Performance v2 result."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Iterable, Literal, Mapping


ALGORITHM_VERSION = "equity-regime-v1"
EPSILON = Decimal("2")
DD_LIMIT = Decimal("23")
GRID_STEP = timedelta(hours=6)
ZERO = Decimal("0")
ONE = Decimal("1")
_METRIC_CONTEXT = Context(
    prec=38,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    traps=[InvalidOperation],
)
_INVALID_REASON_ORDER = (
    "INVALID_REPORT_START_UTC",
    "INVALID_REPORT_END_UTC",
    "INVALID_REPORT_RANGE",
    "MALFORMED_EQUITY_SAMPLE",
    "ROW_OWNERSHIP_MISMATCH",
    "INVALID_SAMPLE_INDEX",
    "INVALID_EQUITY_TIMESTAMP_UTC",
    "MALFORMED_OR_NONFINITE_EQUITY",
    "NONPOSITIVE_EQUITY",
    "UNORDERED_EQUITY_SOURCE",
    "EQUITY_OUTSIDE_REPORT_INTERVAL",
)


def equity_regime_policy_snapshot() -> dict[str, object]:
    """Return the immutable classifier inputs used by a frozen retest action."""
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "epsilon": format(EPSILON, "f"),
        "dd_limit": format(DD_LIMIT, "f"),
        "grid_step_hours": int(GRID_STEP.total_seconds() // 3600),
        "windows_days": [28, 14, 7],
        "pre28_min_days": 14,
        "slowdown_ratio": "0.8",
        "strong_thresholds": {"v28": "10", "v14": "10", "v7": "10", "alternate_v28": "7"},
        "ath": {"require_strict_stages": True, "require_held_w7_breakout": True},
    }

RegimeState = Literal["GROWING", "WEAKENING", "RESUMED", "STALLED", "DROP", "NOT_EVALUATED"]
Decision = Literal["PASS", "DROP", "NOT_EVALUATED"]
Rank = Literal["GROWING", "WEAKENING", "RESUMED", "RESERVED"]


@dataclass(frozen=True, slots=True)
class EquityRegimeSample:
    result_id: int
    sample_index: int
    timestamp_utc: datetime
    equity: object


EquitySample = EquityRegimeSample


@dataclass(frozen=True, slots=True)
class EquityRegimeWindowFacts:
    days: int
    start_utc: datetime
    end_utc: datetime
    elapsed_days: Decimal
    start_equity: Decimal
    end_equity: Decimal
    v: Decimal
    p: Decimal
    direction: str
    grid_points: int

    @property
    def trend30(self) -> Decimal:
        return self.v

    @property
    def endpoint30(self) -> Decimal:
        return self.p


@dataclass(frozen=True, slots=True)
class EquityRegimeFacts:
    algo_version: str
    result_id: int
    report_start_utc: datetime | None
    report_end_utc: datetime | None
    raw_sample_count: int
    invalid_reasons: tuple[str, ...]
    windows_28: EquityRegimeWindowFacts | None
    windows_14: EquityRegimeWindowFacts | None
    windows_7: EquityRegimeWindowFacts | None
    pre28: EquityRegimeWindowFacts | None
    dd14: Decimal | None
    dd7: Decimal | None
    hwm_t28: Decimal | None
    hwm_t14: Decimal | None
    hwm_t7: Decimal | None
    hwm_t: Decimal | None
    hwm_t28_time_utc: datetime | None
    hwm_t14_time_utc: datetime | None
    hwm_t7_time_utc: datetime | None
    hwm_t_time_utc: datetime | None
    previous_ath_w7: Decimal | None
    ath_stage_counts: tuple[int, int, int]
    ath_stage_event_times_utc: tuple[tuple[datetime, ...], tuple[datetime, ...], tuple[datetime, ...]]
    ath_stage_event_values: tuple[tuple[Decimal, ...], tuple[Decimal, ...], tuple[Decimal, ...]]
    ath_stage_strict_increase: bool
    new_ath_w7: bool
    held_w7_breakout: bool
    final_equity: Decimal | None

    @property
    def windows(self) -> Mapping[str, EquityRegimeWindowFacts]:
        return MappingProxyType(
            {
                str(window.days): window
                for window in (self.windows_28, self.windows_14, self.windows_7)
                if window is not None
            }
        )

    @property
    def invalid(self) -> bool:
        return bool(self.invalid_reasons)

    @property
    def new_ath_w7_times_utc(self) -> tuple[datetime, ...]:
        return self.ath_stage_event_times_utc[2]

    @property
    def new_ath_w7_values(self) -> tuple[Decimal, ...]:
        return self.ath_stage_event_values[2]

    @property
    def ath_stage_events(self) -> tuple[tuple[tuple[datetime, Decimal], ...], ...]:
        return tuple(
            tuple(zip(times, values))
            for times, values in zip(self.ath_stage_event_times_utc, self.ath_stage_event_values)
        )

    @property
    def previous_ath_w7_time_utc(self) -> datetime | None:
        return self.hwm_t7_time_utc

    def to_canonical_dict(self) -> dict[str, object]:
        def decimal(value: Decimal | None) -> str | None:
            if value is None:
                return None
            return format(value, "f")

        def window(value: EquityRegimeWindowFacts | None) -> dict[str, object] | None:
            if value is None:
                return None
            return {
                "days": value.days,
                "start_utc": value.start_utc.isoformat().replace("+00:00", "Z"),
                "end_utc": value.end_utc.isoformat().replace("+00:00", "Z"),
                "elapsed_days": decimal(value.elapsed_days),
                "start_equity": decimal(value.start_equity),
                "end_equity": decimal(value.end_equity),
                "v": decimal(value.v),
                "p": decimal(value.p),
                "direction": value.direction,
                "grid_points": value.grid_points,
            }

        return {
            "algo_version": self.algo_version,
            "result_id": self.result_id,
            "report_start_utc": None if self.report_start_utc is None else self.report_start_utc.isoformat().replace("+00:00", "Z"),
            "report_end_utc": None if self.report_end_utc is None else self.report_end_utc.isoformat().replace("+00:00", "Z"),
            "raw_sample_count": self.raw_sample_count,
            "invalid_reasons": list(self.invalid_reasons),
            "windows": {"28": window(self.windows_28), "14": window(self.windows_14), "7": window(self.windows_7)},
            "pre28": window(self.pre28),
            "dd14": decimal(self.dd14),
            "dd7": decimal(self.dd7),
            "hwm_t28": decimal(self.hwm_t28),
            "hwm_t14": decimal(self.hwm_t14),
            "hwm_t7": decimal(self.hwm_t7),
            "hwm_t": decimal(self.hwm_t),
            "hwm_t28_time_utc": None if self.hwm_t28_time_utc is None else self.hwm_t28_time_utc.isoformat().replace("+00:00", "Z"),
            "hwm_t14_time_utc": None if self.hwm_t14_time_utc is None else self.hwm_t14_time_utc.isoformat().replace("+00:00", "Z"),
            "hwm_t7_time_utc": None if self.hwm_t7_time_utc is None else self.hwm_t7_time_utc.isoformat().replace("+00:00", "Z"),
            "hwm_t_time_utc": None if self.hwm_t_time_utc is None else self.hwm_t_time_utc.isoformat().replace("+00:00", "Z"),
            "previous_ath_w7": decimal(self.previous_ath_w7),
            "previous_ath_w7_time_utc": None if self.previous_ath_w7_time_utc is None else self.previous_ath_w7_time_utc.isoformat().replace("+00:00", "Z"),
            "ath_stage_counts": list(self.ath_stage_counts),
            "ath_stage_event_times_utc": [
                [stamp.isoformat().replace("+00:00", "Z") for stamp in stage]
                for stage in self.ath_stage_event_times_utc
            ],
            "ath_stage_event_values": [
                [decimal(value) for value in stage]
                for stage in self.ath_stage_event_values
            ],
            "ath_stage_strict_increase": self.ath_stage_strict_increase,
            "new_ath_w7": self.new_ath_w7,
            "held_w7_breakout": self.held_w7_breakout,
            "final_equity": decimal(self.final_equity),
        }


@dataclass(frozen=True, slots=True)
class EquityRegimeAssessment:
    state: RegimeState
    decision: Decision
    rank: Rank | None
    reasons: tuple[str, ...]
    facts: EquityRegimeFacts

    @property
    def reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None

    @property
    def disposition(self) -> Decision:
        return self.decision

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "decision": self.decision,
            "rank": self.rank,
            "reasons": list(self.reasons),
            "facts": self.facts.to_canonical_dict(),
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
        if value.utcoffset() != timedelta(0):
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _empty_facts(result_id: int, start: datetime | None, end: datetime | None, count: int, reasons: Iterable[str]) -> EquityRegimeFacts:
    return EquityRegimeFacts(
        ALGORITHM_VERSION, result_id, start, end, count, tuple(reasons),
        None, None, None, None, None, None, None, None, None, None, None,
        None, None, None, None, (0, 0, 0), ((), (), ()), ((), (), ()), False, False, False, None,
    )


def _direction(v: Decimal, p: Decimal) -> str:
    if v > EPSILON and p > EPSILON:
        return "UP"
    if v < -EPSILON and p < -EPSILON:
        return "DOWN"
    if abs(v) <= EPSILON and abs(p) <= EPSILON:
        return "FLAT"
    return "MIXED"


def _grid_values(
    points: list[_Point], left: datetime, right: datetime
) -> tuple[list[datetime], list[Decimal]] | None:
    steps = int((right - left) // GRID_STEP)
    first_grid_time = right - GRID_STEP * steps
    times = ([left] if left < first_grid_time else []) + [
        first_grid_time + GRID_STEP * index for index in range(steps + 1)
    ]
    values: list[Decimal] = []
    cursor = 0
    latest: Decimal | None = None
    for stamp in times:
        while cursor < len(points) and points[cursor].timestamp_utc <= stamp:
            latest = points[cursor].equity
            cursor += 1
        if latest is None:
            return None
        values.append(latest)
    return times, values


def _window(days: int, end: datetime, values: list[Decimal]) -> EquityRegimeWindowFacts:
    elapsed = Decimal(days)
    logs = [value.ln() - values[0].ln() for value in values]
    xs = [Decimal(index) / Decimal(4) for index in range(len(values))]
    n = Decimal(len(values))
    xbar = sum(xs, ZERO) / n
    ybar = sum(logs, ZERO) / n
    denominator = sum(((x - xbar) ** 2 for x in xs), ZERO)
    slope = sum(((x - xbar) * (y - ybar) for x, y in zip(xs, logs)), ZERO) / denominator
    p = Decimal(3000) * (logs[-1] - logs[0]) / elapsed
    v = Decimal(3000) * slope
    return EquityRegimeWindowFacts(
        days, end - timedelta(days=days), end, elapsed, values[0], values[-1], v, p,
        _direction(v, p), len(values),
    )


def _pre28(points: list[_Point], left: datetime) -> EquityRegimeWindowFacts | None:
    first = points[0].timestamp_utc
    elapsed = left - first
    elapsed_days = Decimal(elapsed.days * 86400 + elapsed.seconds) / Decimal(86400)
    elapsed_days += Decimal(elapsed.microseconds) / Decimal(86_400_000_000)
    if elapsed < timedelta(days=14):
        return None
    grid = _grid_values(points, first, left)
    if grid is None:
        return None
    times, values = grid
    logs = [value.ln() - values[0].ln() for value in values]
    xs = []
    for stamp in times:
        elapsed = stamp - first
        days = Decimal(elapsed.days * 86400 + elapsed.seconds) / Decimal(86400)
        xs.append(days + Decimal(elapsed.microseconds) / Decimal(86_400_000_000))
    n = Decimal(len(values))
    xbar = sum(xs, ZERO) / n
    ybar = sum(logs, ZERO) / n
    denominator = sum(((x - xbar) ** 2 for x in xs), ZERO)
    slope = sum(((x - xbar) * (y - ybar) for x, y in zip(xs, logs)), ZERO) / denominator
    v = Decimal(3000) * slope
    p = Decimal(3000) * (logs[-1] - logs[0]) / elapsed_days
    return EquityRegimeWindowFacts(
        int(elapsed_days), first, left, elapsed_days, values[0], values[-1], v, p,
        _direction(v, p), len(values),
    )


def calculate_equity_regime_facts(
    result_id: int,
    report_start_utc: datetime,
    report_end_utc: datetime,
    samples: Iterable[EquityRegimeSample],
) -> EquityRegimeFacts:
    """Calculate all regime facts from one ordered, independent result."""
    start = _utc(report_start_utc)
    end = _utc(report_end_utc)
    raw = list(samples)
    invalid: list[str] = []
    if start is None:
        invalid.append("INVALID_REPORT_START_UTC")
    if end is None:
        invalid.append("INVALID_REPORT_END_UTC")
    if start is not None and end is not None and end < start:
        invalid.append("INVALID_REPORT_RANGE")
    points: list[_Point] = []
    previous_index: int | None = None
    previous_timestamp: datetime | None = None
    for item in raw:
        try:
            item_result_id = item.result_id
            item_sample_index = item.sample_index
            item_timestamp = item.timestamp_utc
            item_equity = item.equity
        except AttributeError:
            invalid.append("MALFORMED_EQUITY_SAMPLE")
            continue
        if type(item_result_id) is not int or item_result_id != result_id:
            invalid.append("ROW_OWNERSHIP_MISMATCH")
        if type(item_sample_index) is not int or item_sample_index < 0 or (
            previous_index is not None and item_sample_index <= previous_index
        ):
            invalid.append("INVALID_SAMPLE_INDEX")
        timestamp = _utc(item_timestamp)
        if timestamp is None:
            invalid.append("INVALID_EQUITY_TIMESTAMP_UTC")
        equity = _decimal(item_equity)
        if equity is None:
            invalid.append("MALFORMED_OR_NONFINITE_EQUITY")
        elif equity <= ZERO:
            invalid.append("NONPOSITIVE_EQUITY")
        if timestamp is not None and previous_timestamp is not None and timestamp < previous_timestamp:
            invalid.append("UNORDERED_EQUITY_SOURCE")
        if timestamp is not None and start is not None and end is not None and not (start <= timestamp <= end):
            invalid.append("EQUITY_OUTSIDE_REPORT_INTERVAL")
        if timestamp is not None and equity is not None and type(item_sample_index) is int and item_sample_index >= 0:
            points.append(_Point(item_sample_index, timestamp, equity))
        if type(item_sample_index) is int:
            previous_index = item_sample_index
        if timestamp is not None:
            previous_timestamp = timestamp
    invalid = list(dict.fromkeys(invalid))
    invalid.sort(key=lambda reason: _INVALID_REASON_ORDER.index(reason) if reason in _INVALID_REASON_ORDER else len(_INVALID_REASON_ORDER))
    if invalid or start is None or end is None:
        return _empty_facts(result_id, start, end, len(raw), invalid or ("INVALID_SOURCE",))
    if not points:
        return _empty_facts(result_id, start, end, len(raw), ("W28_UNAVAILABLE",))
    left28, left14, left7 = (end - timedelta(days=days) for days in (28, 14, 7))
    if end - start < timedelta(days=28):
        return _empty_facts(result_id, start, end, len(raw), ("W28_UNAVAILABLE",))
    grid28 = _grid_values(points, left28, end)
    if grid28 is None:
        return _empty_facts(result_id, start, end, len(raw), ("W28_UNAVAILABLE",))
    _, values28 = grid28
    with localcontext(_METRIC_CONTEXT):
        values14 = values28[56:]
        values7 = values28[84:]
        w28 = _window(28, end, values28)
        w14 = _window(14, end, values14)
        w7 = _window(7, end, values7)
        pre = _pre28(points, left28)

        hwm = points[0].equity
        hwm_time = points[0].timestamp_utc
        hwm_values: dict[datetime, tuple[Decimal, datetime]] = {}
        stage_counts = [0, 0, 0]
        stage_event_times: list[list[datetime]] = [[], [], []]
        stage_event_values: list[list[Decimal]] = [[], [], []]
        drawdowns: list[tuple[datetime, Decimal]] = []
        for point in points:
            if point.equity > hwm:
                hwm = point.equity
                hwm_time = point.timestamp_utc
                if left28 < point.timestamp_utc <= left14:
                    stage_counts[0] += 1
                    stage_event_times[0].append(point.timestamp_utc)
                    stage_event_values[0].append(hwm)
                elif left14 < point.timestamp_utc <= left7:
                    stage_counts[1] += 1
                    stage_event_times[1].append(point.timestamp_utc)
                    stage_event_values[1].append(hwm)
                elif left7 < point.timestamp_utc <= end:
                    stage_counts[2] += 1
                    stage_event_times[2].append(point.timestamp_utc)
                    stage_event_values[2].append(hwm)
            drawdowns.append((point.timestamp_utc, Decimal(100) * (ONE - point.equity / hwm)))
            for boundary in (left28, left14, left7, end):
                if point.timestamp_utc <= boundary:
                    hwm_values[boundary] = (hwm, hwm_time)
        # W28 coverage guarantees all boundary HWM values exist.
        hwm28_snapshot = hwm_values.get(left28)
        hwm14_snapshot = hwm_values.get(left14, hwm28_snapshot)
        hwm7_snapshot = hwm_values.get(left7, hwm14_snapshot)
        hwm_end_snapshot = hwm_values.get(end, hwm7_snapshot)
        hwm28, hwm28_time = hwm28_snapshot or (None, None)
        hwm14, hwm14_time = hwm14_snapshot or (hwm28, hwm28_time)
        hwm7, hwm7_time = hwm7_snapshot or (hwm14, hwm14_time)
        hwm_end, hwm_end_time = hwm_end_snapshot or (hwm7, hwm7_time)
        def max_dd(left: datetime) -> Decimal:
            candidates = [value for stamp, value in drawdowns if left <= stamp <= end]
            prior = [value for stamp, value in drawdowns if stamp <= left]
            if prior:
                candidates.append(prior[-1])
            return max(candidates, default=ZERO)
        dd14, dd7 = max_dd(left14), max_dd(left7)
        previous_ath = hwm7
        new_ath_w7 = stage_counts[2] > 0
        final_equity = points[-1].equity
        held = new_ath_w7 and final_equity > previous_ath
        strict = bool(hwm28 is not None and hwm14 is not None and hwm7 is not None and hwm_end is not None
                      and hwm14 > hwm28 and hwm7 > hwm14 and hwm_end > hwm7)
    return EquityRegimeFacts(
        ALGORITHM_VERSION, result_id, start, end, len(raw), (),
        w28, w14, w7, pre, dd14, dd7, hwm28, hwm14, hwm7, hwm_end,
        hwm28_time, hwm14_time, hwm7_time, hwm_end_time,
        previous_ath, tuple(stage_counts), tuple(tuple(stage) for stage in stage_event_times),
        tuple(tuple(stage) for stage in stage_event_values),
        strict, new_ath_w7, held, final_equity,
    )


def _assess_equity_regime(facts: EquityRegimeFacts) -> EquityRegimeAssessment:
    if facts.invalid_reasons or facts.windows_28 is None:
        reason = facts.invalid_reasons or ("W28_UNAVAILABLE",)
        return EquityRegimeAssessment("NOT_EVALUATED", "NOT_EVALUATED", None, tuple(reason), facts)
    w28, w14, w7, pre = facts.windows_28, facts.windows_14, facts.windows_7, facts.pre28
    hard: list[str] = []
    if facts.dd14 is None or facts.dd7 is None:
        return EquityRegimeAssessment("NOT_EVALUATED", "NOT_EVALUATED", None, ("DD_UNAVAILABLE",), facts)
    if facts.dd14 >= DD_LIMIT or facts.dd7 >= DD_LIMIT:
        hard.append("DD_14_7_GTE_23")
    if w28.direction == "DOWN":
        hard.append("W28_DOWN")
    if w28.direction in {"FLAT", "MIXED"} and (pre is None or pre.direction != "UP"):
        hard.append("PRE28_AND_W28_NOT_UP")
    if hard:
        return EquityRegimeAssessment("DROP", "DROP", None, tuple(hard), facts)

    all_up = all(window.direction == "UP" for window in (w28, w14, w7))
    pre_allows_growth = pre is None or pre.direction == "UP"
    staged_geometry = all_up and pre_allows_growth and facts.ath_stage_strict_increase and facts.held_w7_breakout
    strong_10 = all(window.v >= Decimal(10) and window.p >= Decimal(10) for window in (w28, w14, w7))
    slowdown_20 = w14.v <= Decimal("0.8") * w28.v and w7.v <= Decimal("0.8") * w14.v
    strong_7 = (
        w28.v >= Decimal(7) and w28.p >= Decimal(7)
        and w14.v >= Decimal(10) and w14.p >= Decimal(10)
        and w7.v >= Decimal(10) and w7.p >= Decimal(10)
        and w28.v <= w14.v <= w7.v
    )
    if staged_geometry and (strong_10 and not slowdown_20 or strong_7):
        return EquityRegimeAssessment("GROWING", "PASS", "GROWING", ("GROWING",), facts)
    if staged_geometry:
        reasons = []
        if not strong_10 and not strong_7:
            reasons.append("LOW_SPEED")
        if slowdown_20:
            reasons.append("TWO_STEP_SLOWDOWN")
        return EquityRegimeAssessment("WEAKENING", "PASS", "WEAKENING", tuple(reasons), facts)

    resumed = facts.held_w7_breakout and (
        (w28.direction == "UP" and w7.direction == "UP")
        or (pre is not None and pre.direction == "UP" and w28.direction in {"FLAT", "MIXED"}
            and w14.direction == "UP" and w7.direction == "UP")
    )
    if resumed:
        return EquityRegimeAssessment("RESUMED", "PASS", "RESUMED", ("RESUMED",), facts)
    if w28.direction == "UP" or (pre is not None and pre.direction == "UP" and w28.direction in {"FLAT", "MIXED"}):
        return EquityRegimeAssessment("STALLED", "PASS", "RESERVED", ("STALLED",), facts)
    return EquityRegimeAssessment("NOT_EVALUATED", "NOT_EVALUATED", None, ("UNCLASSIFIED_GEOMETRY",), facts)


def assess_equity_regime(facts: EquityRegimeFacts) -> EquityRegimeAssessment:
    with localcontext(_METRIC_CONTEXT):
        return _assess_equity_regime(facts)


def classify_equity_regime(
    result_id: int,
    report_start_utc: datetime,
    report_end_utc: datetime,
    samples: Iterable[EquityRegimeSample],
) -> EquityRegimeAssessment:
    return assess_equity_regime(calculate_equity_regime_facts(result_id, report_start_utc, report_end_utc, samples))


__all__ = [
    "ALGORITHM_VERSION",
    "equity_regime_policy_snapshot",
    "DD_LIMIT",
    "EPSILON",
    "EquityRegimeAssessment",
    "EquityRegimeFacts",
    "EquityRegimeSample",
    "EquityRegimeWindowFacts",
    "EquitySample",
    "assess_equity_regime",
    "calculate_equity_regime_facts",
    "classify_equity_regime",
]
