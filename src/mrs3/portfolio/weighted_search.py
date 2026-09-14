"""Phase 3 weighted portfolio search.

The search is deliberately small: Phase 2 supplies one aligned participant
matrix, this module solves the peak-equity drawdown LP, and the existing
candidate-search data classes carry the compact result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from typing import Any, Callable, Mapping, Sequence

from scipy.optimize import linprog

from .candidate_search import (
    CANDIDATE_SCHEMA_VERSION,
    FAIL,
    PASS,
    PortfolioCandidate,
    SearchResult,
)
from .input import PreparedWeightedInput
from .margin import (
    CALCULATED,
    CONSERVATIVE_BOUND,
    OBSERVED,
    UNKNOWN,
    Evidence,
    MarginCoefficient,
    MarginCoefficientResult,
    evaluate_weighted_margin,
)


WEIGHTED_V1 = "WEIGHTED_V1"
_EPS = Decimal("0.0000001")
_NORMALIZATION_EPS = Decimal("0.00000001")
_RELATIVE_SOLVER_EPS = Decimal("0.000000001")
_UNSET = object()
_RAW_KEYS = frozenset({
    "normalized_delta", "equity", "equity_series", "equity_path", "actions",
    "action_series", "strategy_actions", "minute_actions", "cycles",
})


@dataclass(frozen=True, slots=True)
class LimiterReplayResult:
    status: str
    p30_limiter: Decimal | None
    accepted_cycle_ids: tuple[Any, ...] = ()
    rejected_cycle_ids: tuple[Any, ...] = ()
    accepted_mask: tuple[bool, ...] = ()
    reason: str | None = None
    witness: Mapping[str, Any] | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class _MarginVariant:
    result: Any | None
    replay: LimiterReplayResult

    @property
    def status(self) -> str | None:
        return None if self.result is None else self.result.status

    @property
    def L(self) -> int | None:
        return None if self.result is None else self.result.L

    @property
    def ell(self) -> int | None:
        return None if self.result is None else self.result.ell

    @property
    def B_required(self) -> Decimal | None:
        return None if self.result is None else self.result.B_required

    @property
    def I_all(self) -> Decimal | None:
        return None if self.result is None else self.result.I_all

    @property
    def M_all(self) -> Decimal | None:
        return None if self.result is None else self.result.M_all

    @property
    def I_held(self) -> Decimal | None:
        return None if self.result is None else self.result.I_held

    @property
    def loss_extra(self) -> Decimal | None:
        return None if self.result is None else self.result.loss_extra

    @property
    def B_margin(self) -> Decimal | None:
        return None if self.result is None else self.result.B_margin

    @property
    def release_status(self) -> str | None:
        return None if self.result is None else self.result.release_status

    @property
    def p30_status(self) -> str:
        return self.replay.status

    @property
    def p30(self) -> Decimal | None:
        return self.replay.p30_limiter


def _member_value(member: Any, *names: str, default: Any = None) -> Any:
    if isinstance(member, Mapping):
        for name in names:
            if name in member:
                return member[name]
    for name in names:
        if hasattr(member, name):
            return getattr(member, name)
    return default


def _sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(value)


def _cycle_metric(cycles: Sequence[Any], names: tuple[str, ...]) -> Decimal | None:
    values: list[Decimal] = []
    for cycle in cycles:
        value = _member_value(cycle, *names, default=None)
        if value is None:
            return None
        try:
            values.append(_decimal(value, "cycle metric"))
        except (TypeError, ValueError):
            return None
    return None if not values else sum(values, Decimal(0)) / Decimal(len(values))


def _chosen_x(x: Sequence[Any] | Mapping[Any, Any] | None, strategy_id: Any, index: int) -> Decimal | None:
    if x is None:
        return None
    try:
        value = x.get(strategy_id, None) if isinstance(x, Mapping) else x[index]
        if value is None and isinstance(x, Mapping):
            value = x.get(str(strategy_id), None)
        return _decimal(value, "x", nonnegative=True)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _cycle_duration_hours(cycle: Any) -> Decimal | None:
    value = _member_value(cycle, "duration_hours", "hold_hours", "mean_hold", default=None)
    if value is None:
        seconds = _member_value(cycle, "duration_seconds", default=None)
        if seconds is not None:
            try:
                return _decimal(seconds, "duration_seconds", nonnegative=True) / Decimal("3600")
            except (TypeError, ValueError):
                return None
    if value is not None:
        try:
            return _decimal(value, "hold_hours", nonnegative=True)
        except (TypeError, ValueError):
            return None
    start = _member_value(cycle, "first_fill", "first_fill_utc", "start", "opened_at", default=None)
    end = _member_value(cycle, "final_flat", "final_flat_utc", "end", "closed_at", default=None)
    if start is None or end is None:
        return None
    try:
        left, right = _time_key(start), _time_key(end)
        if left[0] != right[0] or right[1] <= left[1]:
            return None
        if left[0] == "number":
            return (right[1] - left[1]) / Decimal("3600")
        return Decimal(str((right[1] - left[1]).total_seconds())) / Decimal("3600")
    except (TypeError, ValueError):
        return None


def _stable_id_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return (0, Decimal(value))
    return (1, str(value))


def _ordinal_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return (0, Decimal(value))
    if isinstance(value, str):
        try:
            return (0, Decimal(value.strip()))
        except (InvalidOperation, ValueError):
            pass
    return (1, str(value))


def _hold_metrics(member: Any) -> tuple[Decimal | None, Decimal | None]:
    mean = _member_value(member, "mean_hold", "mean_hold_hours", "hold_mean", default=None)
    hold90 = _member_value(member, "hold90", "hold90_hours", "hold_90", "p90_hold", default=None)
    cycles = _sequence(_member_value(member, "cycles", "position_cycles", default=None))
    if not cycles:
        duration_seconds = _member_value(member, "duration_seconds", default=None)
        if duration_seconds is not None:
            cycles = ({"duration_seconds": duration_seconds},)
    durations = tuple(_cycle_duration_hours(cycle) for cycle in cycles)
    if mean is None and cycles:
        mean = None if any(value is None for value in durations) else sum(durations, Decimal(0)) / Decimal(len(durations))
    if hold90 is None:
        if cycles and any(value is None for value in durations):
            return None, None
        holds = [value for value in durations if value is not None]
        if holds:
            holds.sort()
            hold90 = holds[min(len(holds) - 1, max(0, math.ceil(Decimal("0.9") * len(holds)) - 1))]
    if mean is None or hold90 is None:
        return None, None
    try:
        return _decimal(mean, "mean_hold", nonnegative=True), _decimal(hold90, "hold90", nonnegative=True)
    except (TypeError, ValueError):
        return None, None


def _priority_unit(member: Any) -> str | None:
    """Classify PnL evidence before scores are compared across members."""
    per_usdt = _member_value(member, "mean_net_pnl_per_usdt", "pnl_per_usdt", default=None)
    if per_usdt is not None:
        return "normalized"
    absolute = _member_value(member, "mean_net_pnl", "mean_net_pnl_usdt", "mean_pnl", "net_pnl", default=None) is not None
    normalized = _member_value(member, "mean_normalized_pnl", "normalized_pnl", default=None) is not None
    cycles = _sequence(_member_value(member, "cycles", "position_cycles", default=None))
    if cycles:
        cycle_normalized = tuple(_member_value(cycle, "normalized_pnl", "normalized_net_pnl", default=None) for cycle in cycles)
        cycle_absolute = tuple(_member_value(cycle, "net_pnl", "net_pnl_usdt", "pnl", default=None) for cycle in cycles)
        normalized = normalized or bool(cycle_normalized) and all(value is not None for value in cycle_normalized)
        absolute = absolute or bool(cycle_absolute) and all(value is not None for value in cycle_absolute)
    if absolute and normalized:
        return "mixed"
    if normalized:
        return "normalized"
    if absolute:
        return "absolute"
    return None


def priority_details(
    members: Sequence[Mapping[str, Any]],
    x: Sequence[Any] | Mapping[Any, Any] | None = None,
    *,
    max_groups: int = 5,
) -> Mapping[Any, Mapping[str, Any]]:
    """Derive deterministic close priorities from hold time and cycle PnL."""
    if type(max_groups) is not int or not 1 <= max_groups <= 5:
        raise ValueError("max_groups must be between 1 and 5")
    units = {_priority_unit(member) for member in members}
    units.discard(None)
    if "mixed" in units or len(units) > 1:
        return {
            _member_value(member, "strategy_id", default=index): {
                "slot_score": None,
                "T_eff": None,
                "mean_net_pnl": None,
                "priority": None,
            }
            for index, member in enumerate(members)
        }
    details: dict[Any, dict[str, Any]] = {}
    for index, member in enumerate(members):
        strategy_id = _member_value(member, "strategy_id", default=index)
        if strategy_id in details:
            raise ValueError("STRATEGY_ID_NOT_UNIQUE")
        mean_hold, hold90 = _hold_metrics(member)
        with localcontext() as context:
            context.prec = _precision_for(mean_hold, hold90)
            t_eff = None if mean_hold is None or hold90 is None else mean_hold + Decimal("0.5") * max(Decimal(0), hold90 - mean_hold)
        pnl = _member_value(member, "mean_net_pnl", "mean_net_pnl_usdt", "mean_pnl", "net_pnl", default=None)
        cycles = _sequence(_member_value(member, "cycles", "position_cycles", default=None))
        normalized = False
        if pnl is None:
            pnl = _member_value(member, "mean_normalized_pnl", "normalized_pnl", default=None)
            normalized = pnl is not None
        if pnl is None:
            normalized_values = [
                _member_value(cycle, "normalized_pnl", "normalized_net_pnl", default=None)
                for cycle in cycles
            ]
            if normalized_values and all(value is not None for value in normalized_values):
                pnl = _cycle_metric(cycles, ("normalized_pnl", "normalized_net_pnl"))
                normalized = True
            else:
                pnl = _cycle_metric(cycles, ("net_pnl", "net_pnl_usdt", "pnl"))
        per_usdt = _member_value(member, "mean_net_pnl_per_usdt", "pnl_per_usdt", default=None)
        if per_usdt is not None:
            pnl = per_usdt
            normalized = True
        try:
            pnl = None if pnl is None else _decimal(pnl, "mean_net_pnl")
        except (TypeError, ValueError):
            pnl = None
        if pnl is not None and normalized:
            size = _chosen_x(x, strategy_id, index)
            if size is None:
                pnl = None
            else:
                pnl *= size
        score = None if pnl is None or t_eff is None or t_eff <= 0 else pnl / t_eff
        details[strategy_id] = {"slot_score": score, "T_eff": t_eff, "mean_net_pnl": pnl, "priority": None}
    known = [(strategy_id, item["slot_score"]) for strategy_id, item in details.items() if item["slot_score"] is not None and item["slot_score"] > 0]
    known.sort(key=lambda item: (-item[1], _stable_id_key(item[0])))
    groups: list[list[Any]] = []
    group_max: list[Decimal] = []
    for strategy_id, score in known:
        if not groups or (len(groups) < max_groups and score < group_max[-1] / Decimal(2)):
            groups.append([])
            group_max.append(score)
        groups[-1].append(strategy_id)
    tail = [strategy_id for strategy_id, item in details.items() if item["slot_score"] is None or item["slot_score"] <= 0]
    if tail:
        if not groups:
            groups.append([])
        elif len(groups) < max_groups:
            groups.append([])
        groups[-1].extend(sorted(tail, key=_stable_id_key))
    for priority, group in enumerate(groups, 1):
        for strategy_id in group:
            details[strategy_id]["priority"] = priority
    return details


def derive_priorities(
    members: Sequence[Mapping[str, Any]],
    x: Sequence[Any] | Mapping[Any, Any] | None = None,
    *,
    max_groups: int = 5,
) -> Mapping[Any, int | None]:
    # Unknown evidence remains UNKNOWN; coercing it to an integer would turn a
    # failed close-priority derivation into a numeric ranking.
    return {strategy_id: item["priority"] for strategy_id, item in priority_details(members, x, max_groups=max_groups).items()}


def _time_key(value: Any) -> tuple[str, Any]:
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        numeric = Decimal(value)
        if not numeric.is_finite():
            raise ValueError("TIMESTAMP_INVALID")
        return ("number", numeric)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    else:
        raise ValueError("TIMESTAMP_INVALID")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("TIMESTAMP_TIMEZONE_REQUIRED")
    return ("timestamp", parsed.astimezone(timezone.utc))


def _time_label(key: tuple[str, Any]) -> Any:
    return key[1] if key[0] == "number" else key[1].isoformat().replace("+00:00", "Z")


def replay_limiter(
    cycles: Sequence[Mapping[str, Any]],
    L: int,
    *,
    common_days: Any | None = None,
    common_p30: Any | None = None,
    period_start: Any | None = None,
    period_end: Any | None = None,
) -> LimiterReplayResult:
    """Replay [first fill, final flat) cycles with deterministic slot admission."""
    if type(L) is not int or L < 0:
        raise ValueError("L must be a non-negative integer")
    source = tuple(cycles)
    try:
        supplied = None if common_p30 is None else _decimal(common_p30, "common_p30")
    except (TypeError, ValueError):
        return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE")
    try:
        days = None if common_days is None else _decimal(common_days, "common_days", positive=True)
    except (TypeError, ValueError):
        return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE")
    try:
        period_start_key = None if period_start is None else _time_key(period_start)
        period_end_key = None if period_end is None else _time_key(period_end)
    except (TypeError, ValueError, OverflowError):
        return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE")
    if period_start_key is not None and period_end_key is not None:
        if period_start_key[0] != period_end_key[0] or period_start_key[1] >= period_end_key[1]:
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE")
    time_kind = period_start_key[0] if period_start_key is not None else (period_end_key[0] if period_end_key is not None else None)
    records: list[dict[str, Any]] = []
    for index, cycle in enumerate(source):
        cycle_id = _member_value(cycle, "cycle_id", "id", default=index)
        strategy_id = _member_value(cycle, "strategy_id", default=None)
        start = _member_value(cycle, "first_fill", "first_fill_utc", "start", "opened_at", default=None)
        end = _member_value(cycle, "final_flat", "final_flat_utc", "end", "closed_at", default=None)
        # A right-edge open cycle remains occupied through period_end.
        if end is None and period_end_key is not None:
            end = period_end
        if strategy_id is None or start is None or end is None:
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index})
        try:
            start_key, end_key = _time_key(start), _time_key(end)
        except (TypeError, ValueError, OverflowError):
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index})
        if start_key[0] != end_key[0] or (time_kind is not None and start_key[0] != time_kind):
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_TIME_TYPE_MISMATCH", witness={"invalid_cycle": index})
        time_kind = start_key[0]
        if start_key[1] >= end_key[1]:
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index})
        carry_in_count = _member_value(cycle, "carry_in_count", default=None)
        carry_marker = _member_value(cycle, "carry_in", "is_carry_in", default=None)
        if carry_in_count is None:
            carry_in_count = 1 if carry_marker is True else 0
        if type(carry_in_count) is not int or carry_in_count < 0 or carry_in_count > L:
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index, "carry_in_or_unattributed": True})
        if period_start_key is not None:
            # Known carry-in is allowed to begin before the window; its slot is
            # represented by this cycle.  Other out-of-window cycles are not.
            if start_key[1] < period_start_key[1] and carry_in_count == 0:
                return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index})
            if start_key[1] < period_start_key[1]:
                start_key = period_start_key
        if period_end_key is not None:
            if end_key[1] > period_end_key[1]:
                end_key = period_end_key
            if start_key[1] >= end_key[1]:
                return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index})
        complete = _member_value(cycle, "attribution_complete", default=None)
        attribution = _member_value(cycle, "equity_attribution", default=None)
        if complete is not True or (attribution is not None and attribution is not True):
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index, "attribution_complete": False})
        normalized_pnl = _member_value(cycle, "common_window_equity", default=None)
        if normalized_pnl is None:
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index})
        try:
            # The adapter must provide one common-window contribution that is
            # already inclusive of boundary UPnL and non-overlapping costs.
            pnl_value = _decimal(normalized_pnl, "common_window_equity")
        except (TypeError, ValueError):
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index})
        ordinal = _member_value(cycle, "source_ordinal", default=index)
        records.append({"index": index, "id": cycle_id, "strategy_id": strategy_id, "start": start_key, "end": end_key, "pnl": pnl_value, "ordinal": ordinal, "carry_in_count": carry_in_count, "weight": carry_in_count if carry_in_count else 1})
    if L == 0:
        if supplied is None or days is None:
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_COMMON_P30_REQUIRED")
        reconstructed = sum((item["pnl"] for item in records), Decimal(0)) * Decimal(30) / days
        tolerance = max(_NORMALIZATION_EPS, abs(supplied) * _NORMALIZATION_EPS)
        if abs(reconstructed - supplied) > tolerance:
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_COMMON_P30_MISMATCH", witness={"supplied": supplied, "reconstructed": reconstructed, "tolerance": tolerance})
        return LimiterReplayResult("MODEL", supplied, tuple(item["id"] for item in records), (), tuple(True for _ in records), witness={"off_equals_common": True})
    events: list[tuple[tuple[str, Any], int, tuple[int, Any], tuple[int, Any], tuple[int, Any], int, dict[str, Any]]] = []
    for item in records:
        events.append((item["start"], 1, _stable_id_key(item["strategy_id"]), _ordinal_key(item["ordinal"]), _stable_id_key(item["id"]), item["index"], item))
        events.append((item["end"], 0, _stable_id_key(item["strategy_id"]), _ordinal_key(item["ordinal"]), _stable_id_key(item["id"]), item["index"], item))
    # Release events sort before starts at the same normalized timestamp.
    events.sort(key=lambda event: (event[0], event[1], event[2], event[3], event[4], event[5]))
    active: dict[Any, dict[str, Any]] = {}
    accepted: set[int] = set()
    conflict_witnesses: list[Mapping[str, Any]] = []
    cursor = 0
    while cursor < len(events):
        timestamp = events[cursor][0]
        end_cursor = cursor + 1
        while end_cursor < len(events) and events[end_cursor][0] == timestamp:
            end_cursor += 1
        batch = events[cursor:end_cursor]
        for event in batch:
            if event[1] == 0 and event[6]["index"] in active:
                active.pop(event[6]["index"], None)
        starts = [event for event in batch if event[1] == 1]
        available_before = L - sum(item["weight"] for item in active.values())
        rejected_at_timestamp: list[Any] = []
        accepted_at_timestamp: list[Any] = []
        for event in starts:
            item = event[6]
            if sum(active_item["weight"] for active_item in active.values()) + item["weight"] <= L:
                active[item["index"]] = item
                accepted.add(item["index"])
                accepted_at_timestamp.append(item["id"])
            else:
                rejected_at_timestamp.append(item["id"])
        if len(starts) > 1 and rejected_at_timestamp:
            conflict_witnesses.append({
                "timestamp": _time_label(timestamp),
                "candidate_cycle_ids": tuple(event[6]["id"] for event in starts),
                "accepted_cycle_ids": tuple(accepted_at_timestamp),
                "rejected_cycle_ids": tuple(rejected_at_timestamp),
                "available_slots": available_before,
            })
        cursor = end_cursor
    ordered_records = sorted(records, key=lambda item: (item["start"], _stable_id_key(item["strategy_id"]), _ordinal_key(item["ordinal"]), _stable_id_key(item["id"]), item["index"]))
    conflict_count = sum(len(item["rejected_cycle_ids"]) for item in conflict_witnesses)
    replay_witness = {
        "limit": L,
        "events": len(events),
        "simultaneous_conflict_count": conflict_count,
        "simultaneous_conflicts": tuple(conflict_witnesses),
        "conflict_count": conflict_count,
        "conflict_witness": tuple(conflict_witnesses),
    }
    if days is None:
        return LimiterReplayResult("UNKNOWN", None, tuple(item["id"] for item in ordered_records if item["index"] in accepted), tuple(item["id"] for item in ordered_records if item["index"] not in accepted), tuple(item["index"] in accepted for item in records), reason="LIMITER_REPLAY_COMMON_DAYS_REQUIRED", witness=replay_witness)
    p30 = sum((item["pnl"] for item in records if item["index"] in accepted), Decimal(0)) * Decimal(30) / days
    return LimiterReplayResult("MODEL", p30, tuple(item["id"] for item in ordered_records if item["index"] in accepted), tuple(item["id"] for item in ordered_records if item["index"] not in accepted), tuple(item["index"] in accepted for item in records), witness=replay_witness)


def _decimal(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{field} requires an exact Decimal-compatible value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be finite") from error
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{field} must be finite and valid")
    return result


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"decimal": format(value, "f")}
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        return {"type": type(value).__name__, "value": value.isoformat()}
    return {"type": type(value).__name__, "value": repr(value)}


def _digest(value: Any) -> str:
    payload = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _precision_for(*values: Any) -> int:
    """Choose stable working precision without trusting the caller context."""
    maximum_requirement = 0
    value_count = 0

    def collect(value: Any) -> None:
        nonlocal maximum_requirement, value_count
        if isinstance(value, Decimal):
            digits = len(value.as_tuple().digits)
            value_count += 1
            exponent = value.as_tuple().exponent
            integer_span = max(0, value.adjusted() + 1) if value else 0
            fractional_span = digits + max(0, -exponent)
            maximum_requirement = max(maximum_requirement, integer_span, fractional_span)
        elif isinstance(value, (tuple, list)):
            for item in value:
                collect(item)

    for value in values:
        collect(value)
    count_digits = len(str(max(1, value_count)))
    return max(64, maximum_requirement + count_digits + 32)


def _sum_products(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) != len(right):
        raise ValueError("VECTOR_SHAPE_MISMATCH")
    with localcontext() as context:
        context.prec = _precision_for(left, right)
        return sum((first * second for first, second in zip(left, right)), Decimal(0))


def _solver_tolerance(*values: Any) -> Decimal:
    decimals: list[Decimal] = []

    def collect(value: Any) -> None:
        if isinstance(value, Decimal):
            decimals.append(value)
        elif isinstance(value, (tuple, list)):
            for item in value:
                collect(item)

    for value in values:
        collect(value)
    with localcontext() as context:
        context.prec = _precision_for(values)
        scale = max((abs(value) for value in decimals), default=Decimal(1))
        return max(_EPS, scale * _RELATIVE_SOLVER_EPS)


def bank_for_path(path: Sequence[Any], max_dd: Any) -> Decimal:
    """Return the smallest B making every peak-equity DD no larger than m.

    The high-water mark starts at zero.  ``path`` contains cumulative gains,
    not raw equity values, so the returned bank is independently auditable
    from ``equity = B + path[t]``.
    """
    drawdown = _decimal(max_dd, "max_dd")
    if not Decimal(0) < drawdown < Decimal(1):
        raise ValueError("max_dd must be between zero and one")
    values = tuple(_decimal(value, "path") for value in path)
    if not values:
        raise ValueError("path must not be empty")
    with localcontext() as context:
        context.prec = _precision_for(values, drawdown)
        high = Decimal(0)
        required = Decimal(0)
        for gain in values:
            high = max(high, gain)
            required = max(required, ((Decimal(1) - drawdown) * high - gain) / drawdown)
        return max(Decimal(0), required)


def _path(normalized_delta: Sequence[Sequence[Any]], x: Sequence[Decimal]) -> tuple[Decimal, ...]:
    weights = tuple(_decimal(value, "x", nonnegative=True) for value in x)
    rows = tuple(tuple(_decimal(delta, "normalized_delta") for delta in row) for row in normalized_delta)
    with localcontext() as context:
        context.prec = _precision_for(rows, weights)
        totals = [Decimal(0)] * len(weights)
        result: list[Decimal] = []
        for row in rows:
            if len(row) != len(weights):
                raise ValueError("NORMALIZED_DELTA_SHAPE_MISMATCH")
            totals = [total + delta * weight for total, delta, weight in zip(totals, row, weights)]
            result.append(sum(totals, Decimal(0)))
        if not result:
            raise ValueError("NORMALIZED_DELTA_EMPTY")
        return tuple(result)


def _p30(path: Sequence[Decimal], common_days: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _precision_for(path, common_days)
        return path[-1] * Decimal(30) / common_days


def evaluate_weighted_path(
    normalized_delta: Sequence[Sequence[Any]],
    x: Sequence[Any],
    *,
    max_dd: Any,
    common_days: Any,
) -> Mapping[str, Any]:
    """Evaluate a chosen vector using the same exact DD geometry as the LP."""
    weights = tuple(_decimal(value, "x", nonnegative=True) for value in x)
    days = _decimal(common_days, "common_days", positive=True)
    path = _path(normalized_delta, weights)
    bank = bank_for_path(path, max_dd)
    with localcontext() as context:
        context.prec = _precision_for(path, bank)
        equity = tuple(bank + gain for gain in path)
        # Include the t=0 equity point (B + h_0, with h_0=0) in the peak.
        high_equity = bank
        drawdown = Decimal(0)
        for value in equity:
            high_equity = max(high_equity, value)
            if high_equity > 0:
                drawdown = max(drawdown, (high_equity - value) / high_equity)
        p30 = _p30(path, days)
        drawdown_pct = drawdown * Decimal(100)
    return {
        "path": path,
        "bank_for_path": bank,
        "p30_common": p30,
        "max_drawdown_fraction": drawdown,
        "max_drawdown_pct": drawdown_pct,
        "high_water_mark": high_equity,
    }


def _coefficients(normalized_delta: Sequence[Sequence[Decimal]], count: int, common_days: Decimal) -> tuple[Decimal, ...]:
    with localcontext() as context:
        context.prec = _precision_for(normalized_delta, common_days)
        sums = [Decimal(0)] * count
        for row in normalized_delta:
            if len(row) != count:
                raise ValueError("NORMALIZED_DELTA_SHAPE_MISMATCH")
            for index, value in enumerate(row):
                sums[index] += value
        return tuple(value * Decimal(30) / common_days for value in sums)


def _capacity_value(value: Any, field: str) -> Decimal:
    if isinstance(value, Mapping):
        for key in ("capacity_usdt", "position_cap_usdt", "capacity", "cap_usdt", "C"):
            if key in value:
                return _decimal(value[key], field, nonnegative=True)
        raise ValueError(f"{field} missing capacity")
    return _decimal(value, field, nonnegative=True)


def _member_capacity(capacities: Any, index: int, strategy_id: Any, member: Mapping[str, Any]) -> Decimal:
    if isinstance(capacities, Mapping):
        keys = [strategy_id]
        if str(strategy_id) != strategy_id:
            keys.append(str(strategy_id))
        matches = [key for key in keys if key in capacities]
        if len(matches) > 1:
            raise ValueError("AMBIGUOUS_CAPACITY_KEY")
        if matches:
            return _capacity_value(capacities[matches[0]], f"capacity[{index}]")
        raise ValueError(f"MISSING_CAPACITY_{strategy_id}")
    if isinstance(capacities, (str, bytes)) or not isinstance(capacities, Sequence):
        raise TypeError("capacities must be a sequence or mapping")
    if index >= len(capacities):
        raise ValueError("CAPACITY_SHAPE_MISMATCH")
    return _capacity_value(capacities[index], f"capacity[{index}]")


def _validate_input(prepared: PreparedWeightedInput) -> tuple[tuple[Decimal, ...], ...]:
    if not isinstance(prepared, PreparedWeightedInput):
        raise TypeError("prepared must be PreparedWeightedInput")
    rows = tuple(tuple(_decimal(value, "normalized_delta") for value in row) for row in prepared.normalized_delta)
    t = len(rows)
    n = len(prepared.strategy_ids)
    if t == 0 or any(len(row) != n for row in rows):
        raise ValueError("NORMALIZED_DELTA_SHAPE_MISMATCH")
    if len(prepared.timestamps_utc) != t + 1:
        raise ValueError("TIMESTAMP_SHAPE_MISMATCH")
    if len(prepared.valid) != t or any(len(row) != n for row in prepared.valid):
        raise ValueError("VALID_SHAPE_MISMATCH")
    if any(not valid for row in prepared.valid for valid in row):
        raise ValueError("INVALID_PARTICIPANT_CELL")
    return rows


def _common_days(prepared: PreparedWeightedInput, value: Any | None) -> Decimal:
    if value is not None:
        return _decimal(value, "common_days", positive=True)
    span = prepared.period_end_utc - prepared.period_start_utc
    with localcontext() as context:
        context.prec = 64
        seconds = Decimal(span.days * 86400 + span.seconds) + Decimal(span.microseconds) / Decimal(1_000_000)
        days = seconds / Decimal(86400)
    return _decimal(days, "common_days", positive=True)


@dataclass(frozen=True, slots=True)
class _Solution:
    bank: Decimal
    x: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class _SolveOutcome:
    status: str
    solution: _Solution | None = None
    reason: str | None = None


def _cumulative(normalized_delta: Sequence[Sequence[Decimal]], count: int) -> tuple[tuple[Decimal, ...], ...]:
    with localcontext() as context:
        context.prec = _precision_for(normalized_delta)
        totals = [Decimal(0)] * count
        result: list[tuple[Decimal, ...]] = []
        for row in normalized_delta:
            totals = [total + value for total, value in zip(totals, row)]
            result.append(tuple(totals))
        return tuple(result)


def _solve_lp_unchecked(
    normalized_delta: tuple[tuple[Decimal, ...], ...],
    capacities: tuple[Decimal, ...],
    coefficients: tuple[Decimal, ...],
    *,
    max_dd: Decimal,
    target: Decimal | None,
    bank_available: Decimal | None,
    maximize: bool,
) -> _SolveOutcome:
    t = len(normalized_delta)
    n = len(capacities)
    bank_index = 0
    x_start = 1
    h_start = 1 + n
    size = 1 + n + t
    c = [0.0] * size
    if maximize:
        c[x_start:x_start + n] = [-float(value) for value in coefficients]
    else:
        c[bank_index] = 1.0
    a_ub: list[list[float]] = []
    b_ub: list[float] = []
    with localcontext() as context:
        context.prec = _precision_for(max_dd)
        one_minus = Decimal(1) - max_dd
    cumulative = _cumulative(normalized_delta, n)
    for row, h_index in zip(cumulative, range(h_start, h_start + t)):
        # h_t >= g_t
        constraint = [0.0] * size
        for index, value in enumerate(row):
            constraint[x_start + index] = float(value)
        constraint[h_index] = -1.0
        a_ub.append(constraint)
        b_ub.append(0.0)
        # h_t >= h_(t-1), with h_0 fixed at zero by its bound.
        if h_index > h_start:
            constraint = [0.0] * size
            constraint[h_index - 1] = 1.0
            constraint[h_index] = -1.0
            a_ub.append(constraint)
            b_ub.append(0.0)
        # g_t >= (1-m)h_t - mB
        constraint = [0.0] * size
        constraint[bank_index] = -float(max_dd)
        for index, value in enumerate(row):
            constraint[x_start + index] = -float(value)
        constraint[h_index] = float(one_minus)
        a_ub.append(constraint)
        b_ub.append(0.0)
    if target is not None:
        constraint = [0.0] * size
        for index, value in enumerate(coefficients):
            constraint[x_start + index] = -float(value)
        a_ub.append(constraint)
        b_ub.append(-float(target))
    upper_bank = None if bank_available is None else float(bank_available)
    bounds = [(1.0, upper_bank)] + [(0.0, float(value)) for value in capacities] + [(0.0, None)] * t
    try:
        result = linprog(
            c,
            A_ub=a_ub,
            b_ub=b_ub,
            bounds=bounds,
            method="highs",
        )
    except (ArithmeticError, ValueError, TypeError, RuntimeError):
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR")
    if not result.success:
        if getattr(result, "status", None) == 2:
            return _SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE")
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR")
    if result.x is None or len(result.x) != size:
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR")
    if any(not math.isfinite(float(value)) for value in result.x):
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR")
    raw_bank = Decimal(str(result.x[bank_index]))
    raw_x = tuple(Decimal(str(result.x[x_start + index])) for index in range(n))
    raw_h = tuple(Decimal(str(result.x[h_start + index])) for index in range(t))
    with localcontext() as context:
        context.prec = _precision_for(raw_bank, raw_x, capacities, bank_available)
        if raw_bank < Decimal(1) - _solver_tolerance(raw_bank):
            return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID")
        if any(value < -_solver_tolerance(value, cap) or value > cap + _solver_tolerance(value, cap) for value, cap in zip(raw_x, capacities)):
            return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID")
        if bank_available is not None and raw_bank > bank_available + _solver_tolerance(raw_bank, bank_available):
            return _SolveOutcome("ERROR", reason="BANK_UNAVAILABLE")
    raw_bank = max(Decimal(1), raw_bank)
    if bank_available is not None:
        raw_bank = min(raw_bank, bank_available)
    raw_x = tuple(min(cap, max(Decimal(0), value)) for value, cap in zip(raw_x, capacities))
    with localcontext() as context:
        context.prec = _precision_for(cumulative, raw_x, raw_h, raw_bank)
        for index, (gain_row, h_value) in enumerate(zip(cumulative, raw_h)):
            gain = _sum_products(gain_row, raw_x)
            previous = Decimal(0) if index == 0 else raw_h[index - 1]
            target_gain = max(gain, previous)
            dd_rhs = one_minus * h_value - max_dd * raw_bank
            if h_value < target_gain - _solver_tolerance(h_value, target_gain) or gain < dd_rhs - _solver_tolerance(gain, dd_rhs):
                return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID")
    path = _path(normalized_delta, raw_x)
    required = bank_for_path(path, max_dd)
    authoritative_bank = max(Decimal(1), required)
    with localcontext() as context:
        context.prec = _precision_for(required, authoritative_bank, raw_bank, coefficients, raw_x, target)
        if authoritative_bank > raw_bank + _solver_tolerance(authoritative_bank, raw_bank):
            return _SolveOutcome("ERROR", reason="BANK_UNAVAILABLE" if bank_available is not None else "LP_SOLUTION_INVALID")
        if bank_available is not None and authoritative_bank > bank_available:
            return _SolveOutcome("ERROR", reason="BANK_UNAVAILABLE")
        p30 = _sum_products(coefficients, raw_x)
        if target is not None and p30 < target - _solver_tolerance(p30, target):
            return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID")
    return _SolveOutcome("PASS", _Solution(authoritative_bank, raw_x))


def _solve_lp(*args: Any, **kwargs: Any) -> _SolveOutcome:
    """Run model assembly and HiGHS behind one fail-closed boundary."""
    try:
        return _solve_lp_unchecked(*args, **kwargs)
    except (ArithmeticError, ValueError, TypeError, RuntimeError):
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR")


def _safe_member(member: Mapping[str, Any], x: Decimal, capacity: Decimal, *, priority: int | None | object = _UNSET) -> dict[str, Any]:
    result = {
        key: value
        for key, value in member.items()
        if key not in _RAW_KEYS and key in {"symbol", "side", "strategy_id", "result_id", "user_rank", "user_status", "priority", "position_priority", "open_positions_limiter"}
    }
    result["capacity_usdt"] = capacity
    result["x_usdt"] = x
    if priority is not _UNSET:
        result["priority"] = priority
    return result


def _margin_variant(
    x: Sequence[Decimal],
    source_members: Sequence[Mapping[str, Any]],
    coefficients: Sequence[Any] | Mapping[Any, Any],
    *,
    options: Mapping[str, Any],
    bank_available: Decimal | None,
    B_risk: Decimal | None = None,
) -> tuple[_MarginVariant | None, tuple[_MarginVariant, ...]]:
    """Evaluate off and every strict limiter before rejecting one x vector."""
    config = dict(options)
    known_options = {
        "L", "limiter", "max_dd", "reserve", "max_mm_load",
        "priorities", "strategy_ids", "full_nominals", "limiter_release_status",
        "release_evidence", "cycles", "attributed_cycles",
        "common_days", "common_p30", "period_start", "period_end",
    }
    if set(config) - known_options:
        raise ValueError("MARGIN_OPTION_UNKNOWN")
    explicit_l = config.pop("L", config.pop("limiter", None))
    if explicit_l is None:
        limits = (0, *range(1, len(x)))
    else:
        if type(explicit_l) is not int or explicit_l < 0 or explicit_l >= len(x):
            raise ValueError("LIMITER_RANGE_INVALID")
        limits = (explicit_l,)
    # L=N is equivalent to off and is intentionally never emitted.
    limits = tuple(dict.fromkeys(limit for limit in limits if limit == 0 or (type(limit) is int and 0 < limit < len(x))))
    source_strategy_ids = tuple(member.get("strategy_id") for member in source_members)
    supplied_strategy_ids = tuple(config.get("strategy_ids", source_strategy_ids))
    if supplied_strategy_ids != source_strategy_ids:
        raise ValueError("MARGIN_STRATEGY_SHAPE_MISMATCH")
    config["strategy_ids"] = supplied_strategy_ids
    priorities = config.get("priorities")
    if priorities is None:
        priorities = derive_priorities(source_members, x)
        if any(priority is None for priority in priorities.values()):
            return None, ()
        config["priorities"] = priorities
    elif not isinstance(priorities, Mapping):
        if len(priorities) != len(x):
            raise ValueError("PRIORITY_SHAPE_MISMATCH")
        strategy_ids = tuple(config["strategy_ids"])
        config["priorities"] = dict(zip(strategy_ids, priorities))
    cycles = config.pop("cycles", config.pop("attributed_cycles", None))
    common_days = config.pop("common_days", None)
    common_p30 = config.pop("common_p30", None)
    period_start = config.pop("period_start", None)
    period_end = config.pop("period_end", None)
    if cycles is None:
        cycles = tuple(
            cycle
            for member in source_members
            for cycle in _sequence(_member_value(member, "cycles", "position_cycles", default=None))
        )
    # Replay is the only source of limiter P30 evidence. Arbitrary status/value
    # kwargs remain diagnostics and cannot manufacture MODEL evidence.
    replay_by_l = {
        limit: (
            replay_limiter(
                cycles,
                limit,
                common_days=common_days,
                common_p30=common_p30,
                period_start=period_start,
                period_end=period_end,
            )
            if cycles else LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE")
        )
        for limit in limits
    }
    allowed = {
        "max_dd", "reserve", "max_mm_load", "priorities", "strategy_ids",
        "full_nominals", "limiter_release_status", "release_evidence",
    }
    config = {key: value for key, value in config.items() if key in allowed}
    if B_risk is not None:
        config["B_risk"] = B_risk
    if bank_available is not None:
        config["bank_available"] = bank_available
    variants: list[_MarginVariant] = []
    for limit in limits:
        try:
            result = evaluate_weighted_margin(x, coefficients, L=limit, **config)
        except (ArithmeticError, TypeError, ValueError):
            result = None
        variants.append(_MarginVariant(result, replay_by_l[limit]))
    passing = [item for item in variants if item.result is not None and item.status == PASS]
    eligible = passing if bank_available is None else [
        item for item in passing
        if item.B_required is not None and item.B_required <= bank_available
    ]
    return _select_margin_variants(eligible, bank_available=bank_available), tuple(variants)


def _select_margin_variants(
    variants: Sequence[_MarginVariant],
    *,
    bank_available: Decimal | None,
) -> _MarginVariant | None:
    """Select the primary fixed-x variant; alternatives stay in the tuple."""
    if not variants:
        return None
    models = [item for item in variants if item.p30_status == "MODEL" and item.p30 is not None]
    if bank_available is not None:
        if models:
            return max(models, key=lambda item: (item.p30, int(item.L or 0)))
        return next((item for item in variants if item.L == 0), max(variants, key=lambda item: int(item.L or 0)))
    return min(variants, key=lambda item: (item.B_required or Decimal("Infinity"), int(item.L or 0)))


def _ordered_margin_variants(
    variants: Sequence[_MarginVariant],
    *,
    bank_available: Decimal | None,
) -> tuple[_MarginVariant, ...]:
    """Return compact candidates without comparing UNKNOWN to numeric P30."""
    passing = [item for item in variants if item.result is not None and item.status == PASS]
    if bank_available is not None:
        passing = [item for item in passing if item.B_required is not None and item.B_required <= bank_available]
        models = sorted(
            (item for item in passing if item.p30_status == "MODEL" and item.p30 is not None),
            key=lambda item: (item.p30, int(item.L or 0)), reverse=True,
        )
        unknown = sorted(
            (item for item in passing if item.p30_status != "MODEL" or item.p30 is None),
            key=lambda item: (item.L != 0, -int(item.L or 0)),
        )
        return tuple(models + unknown)
    if not passing:
        return ()
    minimum = min(passing, key=lambda item: (item.B_required or Decimal("Infinity"), int(item.L or 0)))
    models = [item for item in passing if item.p30_status == "MODEL" and item.p30 is not None]
    best_model = max(models, key=lambda item: (item.p30, int(item.L or 0)), default=None)
    selected: list[_MarginVariant] = [minimum]
    if best_model is not None and best_model not in selected:
        selected.append(best_model)
    return tuple(selected)


def _margin_failure_reason(variants: Sequence[_MarginVariant], *, bank_available: Decimal | None) -> str:
    reasons = tuple(
        item.result.reason
        for item in variants
        if item.result is not None and isinstance(item.result.reason, str)
    )
    for reason in (
        "MARGIN_COEFFICIENT_DOMAIN_EXCEEDED",
        "PRIORITY_UNKNOWN",
        "BANK_UNAVAILABLE",
        "MARGIN_BOUND_FAILED",
        "MARGIN_BOUND_UNAVAILABLE",
    ):
        if reason in reasons:
            return reason
    if bank_available is not None and any(item.result is not None and item.B_required is not None for item in variants):
        return "BANK_UNAVAILABLE"
    return reasons[0] if reasons else "MARGIN_BOUND_UNAVAILABLE"


def _result(status: str, reason: str | None = None) -> SearchResult:
    return SearchResult(status=status, reason=reason, mode=WEIGHTED_V1)


def _coefficient_has_known_evidence(value: Any) -> bool:
    if isinstance(value, MarginCoefficient):
        try:
            _decimal(value.max_notional, "max_notional", nonnegative=True)
        except (TypeError, ValueError):
            return False
        return value.evidence_class in {OBSERVED, CALCULATED, CONSERVATIVE_BOUND}
    if not isinstance(value, Mapping) or "a" not in value or "b" not in value:
        return False
    evidence = value.get("evidence_class", value.get("evidence", _UNSET))
    if isinstance(evidence, Evidence):
        evidence = evidence.evidence_class
    elif isinstance(evidence, Mapping):
        evidence = evidence.get("evidence_class", UNKNOWN)
    if evidence not in {OBSERVED, CALCULATED, CONSERVATIVE_BOUND} or "max_notional" not in value:
        return False
    try:
        _decimal(value["max_notional"], "max_notional", nonnegative=True)
    except (TypeError, ValueError):
        return False
    return True


def _public_margin_coefficients_are_known(value: Any) -> bool:
    """Reject compact bare rates at the public sizing boundary."""
    if isinstance(value, MarginCoefficientResult):
        return (
            value.status == PASS
            and value.evidence_class in {OBSERVED, CALCULATED, CONSERVATIVE_BOUND}
            and bool(value.coefficients)
            and all(_coefficient_has_known_evidence(item) for item in value.coefficients)
        )
    if isinstance(value, Mapping):
        records = tuple(value.values())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        records = tuple(value)
    else:
        return False
    return bool(records) and all(_coefficient_has_known_evidence(item) for item in records)


def _margin_domain_reason(value: Any, strategy_ids: Sequence[Any], capacities: Sequence[Decimal]) -> str | None:
    if isinstance(value, MarginCoefficientResult):
        records = {item.strategy_id: item for item in value.coefficients}
        try:
            ordered = tuple(records[strategy_id] for strategy_id in strategy_ids)
        except KeyError:
            return "MARGIN_BOUND_UNAVAILABLE"
    elif isinstance(value, Mapping):
        try:
            ordered = tuple(value[strategy_id] for strategy_id in strategy_ids)
        except KeyError:
            return "MARGIN_BOUND_UNAVAILABLE"
    else:
        ordered = tuple(value)
    if len(ordered) != len(capacities):
        return "MARGIN_BOUND_UNAVAILABLE"
    for coefficient, capacity in zip(ordered, capacities):
        domain = coefficient.max_notional if isinstance(coefficient, MarginCoefficient) else coefficient.get("max_notional") if isinstance(coefficient, Mapping) else None
        try:
            if domain is None:
                return "MARGIN_BOUND_UNAVAILABLE"
            if capacity > _decimal(domain, "max_notional", nonnegative=True):
                return "MARGIN_COEFFICIENT_DOMAIN_EXCEEDED"
        except (TypeError, ValueError):
            return "MARGIN_BOUND_UNAVAILABLE"
    return None


def _validate_max_targets(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
        raise ValueError("max_targets must be an integer from 1 to 8")
    return value


def _candidate_for_solution(
    solution: _Solution,
    normalized_delta: tuple[tuple[Decimal, ...], ...],
    source_members: tuple[Mapping[str, Any], ...],
    capacities: tuple[Decimal, ...],
    *,
    max_dd: Decimal,
    common_days: Decimal,
    target: Decimal | None,
    profile_id: str,
    scenario_id: str,
    margin_coefficients: Sequence[Any] | Mapping[Any, Any] | None = None,
    margin_kwargs: Mapping[str, Any] | None = None,
    bank_available: Decimal | None = None,
) -> PortfolioCandidate | None:
    candidates = _candidates_for_solution(
        solution,
        normalized_delta,
        source_members,
        capacities,
        max_dd=max_dd,
        common_days=common_days,
        target=target,
        profile_id=profile_id,
        scenario_id=scenario_id,
        margin_coefficients=margin_coefficients,
        margin_kwargs=margin_kwargs,
        bank_available=bank_available,
    )
    return candidates[0] if candidates else None


def _candidates_for_solution(
    solution: _Solution,
    normalized_delta: tuple[tuple[Decimal, ...], ...],
    source_members: tuple[Mapping[str, Any], ...],
    capacities: tuple[Decimal, ...],
    *,
    max_dd: Decimal,
    common_days: Decimal,
    target: Decimal | None,
    profile_id: str,
    scenario_id: str,
    margin_coefficients: Sequence[Any] | Mapping[Any, Any] | None = None,
    margin_kwargs: Mapping[str, Any] | None = None,
    bank_available: Decimal | None = None,
    allow_rescue: bool = True,
    bank_feasible_override: bool | None = None,
    failure_reason: list[str] | None = None,
) -> tuple[PortfolioCandidate, ...]:
    evaluated = evaluate_weighted_path(normalized_delta, solution.x, max_dd=max_dd, common_days=common_days)
    with localcontext() as context:
        context.prec = _precision_for(evaluated["bank_for_path"], solution.bank)
        if evaluated["bank_for_path"] > solution.bank + _solver_tolerance(evaluated["bank_for_path"], solution.bank):
            if failure_reason is not None:
                failure_reason.append("BANK_UNAVAILABLE" if bank_available is not None else "LP_SOLUTION_INVALID")
            return ()
    metrics = {
        "mode": WEIGHTED_V1,
        "bank_for_path_usdt": evaluated["bank_for_path"],
        "required_bank_usdt": solution.bank,
        "p30_common_usdt_30d": evaluated["p30_common"],
        "max_drawdown_fraction": evaluated["max_drawdown_fraction"],
        "max_drawdown_pct": evaluated["max_drawdown_pct"],
        "max_dd": max_dd,
        "common_days": common_days,
        "target_p30_usdt_30d": target,
    }
    if margin_coefficients is not None:
        options = dict(margin_kwargs or {})
        options.setdefault("max_dd", max_dd)
        options.setdefault("common_days", common_days)
        options.setdefault("common_p30", evaluated["p30_common"])
        path_risk = max(Decimal(1), evaluated["bank_for_path"])
        primary, margin_variants = _margin_variant(
            solution.x,
            source_members,
            margin_coefficients,
            options=options,
            bank_available=bank_available,
            B_risk=path_risk,
        )
        selected = _ordered_margin_variants(margin_variants, bank_available=bank_available)
        if not selected:
            if not margin_variants and options.get("priorities") is None:
                if any(value is None for value in derive_priorities(source_members, solution.x).values()):
                    if failure_reason is not None:
                        failure_reason.append("PRIORITY_UNKNOWN")
                    return ()
            if allow_rescue and bank_available is not None:
                failed = [
                    item for item in margin_variants
                    if item.result is not None and item.B_required is not None
                ]
                rescue = min(
                    failed,
                    key=lambda item: (item.B_required, int(item.L or 0)),
                    default=None,
                )
                if rescue is not None:
                    original = _candidates_for_solution(
                        solution,
                        normalized_delta,
                        source_members,
                        capacities,
                        max_dd=max_dd,
                        common_days=common_days,
                        target=target,
                        profile_id=profile_id,
                        scenario_id=scenario_id,
                        margin_coefficients=margin_coefficients,
                        margin_kwargs=margin_kwargs,
                        bank_available=None,
                        allow_rescue=False,
                        bank_feasible_override=False,
                    )
                    try:
                        risk_bank = path_risk
                        margin_bank = _decimal(rescue.B_margin, "B_margin", positive=True)
                        denominator = max(risk_bank, margin_bank)
                        with localcontext() as context:
                            context.prec = _precision_for(bank_available, denominator, capacities, solution.x)
                            cap_scale = min(
                                (
                                    capacity / value
                                    for capacity, value in zip(capacities, solution.x)
                                    if value > 0
                                ),
                                default=Decimal("Infinity"),
                            )
                            scale = min(bank_available / denominator, cap_scale)
                        if scale.is_finite() and Decimal(0) < scale < Decimal(1):
                            scaled_x = tuple(value * scale for value in solution.x)
                            scaled_path = evaluate_weighted_path(
                                normalized_delta,
                                scaled_x,
                                max_dd=max_dd,
                                common_days=common_days,
                            )
                            reduced = _candidates_for_solution(
                                _Solution(max(Decimal(1), scaled_path["bank_for_path"]), scaled_x),
                                normalized_delta,
                                source_members,
                                capacities,
                                max_dd=max_dd,
                                common_days=common_days,
                                target=target,
                                profile_id=profile_id,
                                scenario_id=scenario_id,
                                margin_coefficients=margin_coefficients,
                                margin_kwargs=margin_kwargs,
                                bank_available=bank_available,
                                allow_rescue=False,
                            )
                            if reduced:
                                return original + reduced
                        return original
                    except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                        return original
            if failure_reason is not None:
                failure_reason.append(_margin_failure_reason(margin_variants, bank_available=bank_available))
            return ()
        all_variant_metrics = tuple(
            None if item.result is None else {
                "L": item.L,
                "status": item.status,
                "B_required": item.B_required,
                "I_held": item.I_held,
                "p30_status": item.p30_status,
                "p30": item.p30,
                "replay_accepted_count": len(item.replay.accepted_cycle_ids),
                "replay_rejected_count": len(item.replay.rejected_cycle_ids),
            }
            for item in margin_variants
        )
        candidates: list[PortfolioCandidate] = []
        for selected_variant in selected:
            margin_result = selected_variant.result
            if margin_result is None:
                continue
            variant_metrics = dict(metrics)
            variant_metrics.update({
                "I_all_usdt": selected_variant.I_all,
                "M_all_usdt": selected_variant.M_all,
                "I_held_usdt": selected_variant.I_held,
                "loss_extra_usdt": selected_variant.loss_extra,
                "B_margin_usdt": selected_variant.B_margin,
                "B_required_margin_usdt": selected_variant.B_required,
                "limiter_L": selected_variant.L,
                "limiter_ell": selected_variant.ell,
                "limiter_release_status": selected_variant.release_status,
                "limiter_p30_status": selected_variant.p30_status,
                "replay_accepted_count": len(selected_variant.replay.accepted_cycle_ids),
                "replay_rejected_count": len(selected_variant.replay.rejected_cycle_ids),
                "limiter_variants": all_variant_metrics,
            })
            if selected_variant.p30 is not None:
                variant_metrics["p30_limiter_model_usdt_30d"] = selected_variant.p30
            variant_metrics["required_bank_usdt"] = max(
                variant_metrics["required_bank_usdt"], selected_variant.B_required or Decimal(0)
            )
            variant_metrics["bank_feasible"] = (
                bank_feasible_override
                if bank_feasible_override is not None
                else bank_available is None or variant_metrics["required_bank_usdt"] <= bank_available
            )
            priority_values = options.get("priorities")
            if priority_values is None:
                priority_values = derive_priorities(source_members, solution.x)
            elif not isinstance(priority_values, Mapping):
                strategy_ids = tuple(options.get("strategy_ids", (member.get("strategy_id") for member in source_members)))
                priority_values = dict(zip(strategy_ids, priority_values))
            compact_members = tuple(
                _safe_member(
                    member,
                    x,
                    cap,
                    priority=priority_values.get(member.get("strategy_id")) if isinstance(priority_values, Mapping) else None,
                )
                for member, x, cap in zip(source_members, solution.x, capacities)
            )
            identity = _digest({"profile_id": profile_id, "scenario_id": scenario_id, "members": compact_members, "metrics": variant_metrics})
            candidates.append(PortfolioCandidate(
                schema_version=CANDIDATE_SCHEMA_VERSION,
                profile_id=str(profile_id),
                scenario_id=str(scenario_id),
                identity=identity,
                members=compact_members,
                metrics=variant_metrics,
                status=PASS,
            ))
        return tuple(candidates)
    compact_members = tuple(_safe_member(member, x, cap) for member, x, cap in zip(source_members, solution.x, capacities))
    identity = _digest({"profile_id": profile_id, "scenario_id": scenario_id, "members": compact_members, "metrics": metrics})
    return (PortfolioCandidate(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        profile_id=str(profile_id),
        scenario_id=str(scenario_id),
        identity=identity,
        members=compact_members,
        metrics=metrics,
        status=PASS,
    ),)


def _revalidate_proposed_x(
    original: Sequence[PortfolioCandidate],
    proposed_x: Sequence[Any],
    normalized_delta: tuple[tuple[Decimal, ...], ...],
    source_members: tuple[Mapping[str, Any], ...],
    capacities: tuple[Decimal, ...],
    *,
    max_dd: Decimal,
    common_days: Decimal,
    target: Decimal | None,
    profile_id: str,
    scenario_id: str,
    margin_coefficients: Sequence[Any] | Mapping[Any, Any] | None = None,
    margin_kwargs: Mapping[str, Any] | None = None,
    bank_available: Decimal | None = None,
    validator: Callable[[tuple[Decimal, ...]], bool] | None = None,
) -> tuple[PortfolioCandidate, ...]:
    """Validate one externally proposed x exactly once; failed proposals vanish."""
    original_candidates = (original,) if isinstance(original, PortfolioCandidate) else tuple(original)
    try:
        x = tuple(_decimal(value, "proposed_x", nonnegative=True) for value in proposed_x)
        if len(x) != len(capacities) or any(value > capacity for value, capacity in zip(x, capacities)):
            return original_candidates
        if validator is not None and not validator(x):
            return original_candidates
        evaluated = evaluate_weighted_path(normalized_delta, x, max_dd=max_dd, common_days=common_days)
        if target is not None and evaluated["p30_common"] < target - _solver_tolerance(evaluated["p30_common"], target):
            return original_candidates
        if bank_available is not None and evaluated["bank_for_path"] > bank_available + _solver_tolerance(evaluated["bank_for_path"], bank_available):
            return original_candidates
        proposed = _candidates_for_solution(
            _Solution(max(Decimal(1), evaluated["bank_for_path"]), x),
            normalized_delta,
            source_members,
            capacities,
            max_dd=max_dd,
            common_days=common_days,
            target=target,
            profile_id=profile_id,
            scenario_id=scenario_id,
            margin_coefficients=margin_coefficients,
            margin_kwargs=margin_kwargs,
            bank_available=bank_available,
            allow_rescue=False,
        )
    except (ArithmeticError, TypeError, ValueError, InvalidOperation):
        return original_candidates
    return original_candidates + proposed if proposed else original_candidates


def _initial_targets(upper: Decimal, max_targets: int) -> tuple[Decimal, ...]:
    with localcontext() as context:
        context.prec = _precision_for(upper)
        raw = (upper,) if max_targets == 1 else ((upper, upper / Decimal(max_targets)) if max_targets == 2 else (upper, upper / Decimal(max_targets), upper / Decimal(2)))
        result: list[Decimal] = []
        for target in raw:
            if target not in result:
                result.append(target)
        return tuple(result)


def _normalized_composition(x: Sequence[Decimal]) -> tuple[Decimal, ...]:
    with localcontext() as context:
        context.prec = _precision_for(x)
        total = sum(x, Decimal(0))
        if total <= 0:
            return ()
        return tuple(value / total for value in x)


def _active_capacity_set(x: Sequence[Decimal], capacities: Sequence[Decimal]) -> tuple[bool, ...]:
    with localcontext() as context:
        context.prec = _precision_for(x, capacities)
        return tuple(
            abs(value - capacity) <= max(_NORMALIZATION_EPS, _NORMALIZATION_EPS * capacity)
            for value, capacity in zip(x, capacities)
        )


def _interval_is_refinable(left: _Solution, right: _Solution, capacities: Sequence[Decimal]) -> bool:
    left_composition = _normalized_composition(left.x)
    right_composition = _normalized_composition(right.x)
    if not left_composition or not right_composition:
        return False
    with localcontext() as context:
        context.prec = _precision_for(left_composition, right_composition)
        l1 = sum((abs(first - second) for first, second in zip(left_composition, right_composition)), Decimal(0))
    return l1 > _NORMALIZATION_EPS or _active_capacity_set(left.x, capacities) != _active_capacity_set(right.x, capacities)


def weighted_search(
    prepared: PreparedWeightedInput,
    capacities: Sequence[Any] | Mapping[Any, Any],
    *,
    members: Sequence[Mapping[str, Any]] | None = None,
    max_dd: Any = Decimal("0.20"),
    common_days: Any | None = None,
    target_p30: Any | None = None,
    bank_available: Any | None = None,
    max_targets: int = 8,
    profile_id: str = "WEIGHTED",
    scenario_id: str = "WEIGHTED_V1",
    margin_coefficients: Sequence[Any] | Mapping[Any, Any] | None = None,
    margin_kwargs: Mapping[str, Any] | None = None,
    margin: Mapping[str, Any] | None = None,
    proposed_x: Sequence[Any] | None = None,
    proposal_validator: Callable[[tuple[Decimal, ...]], bool] | None = None,
) -> SearchResult:
    """Find a bounded deterministic weighted LP frontier for Phase 3."""
    max_targets = _validate_max_targets(max_targets)
    normalized_delta = _validate_input(prepared)
    n = len(prepared.strategy_ids)
    if members is None:
        source_members = tuple({"strategy_id": strategy_id, "symbol": str(strategy_id), "side": "LONG"} for strategy_id in prepared.strategy_ids)
    else:
        source_members = tuple(dict(member) for member in members)
        if len(source_members) != n:
            raise ValueError("MEMBER_SHAPE_MISMATCH")
        member_ids = tuple(member.get("strategy_id") for member in source_members)
        if any(strategy_id is None for strategy_id in member_ids):
            raise ValueError("MEMBER_STRATEGY_SHAPE_MISMATCH")
        if len(set(member_ids)) != n or set(member_ids) != set(prepared.strategy_ids):
            raise ValueError("MEMBER_STRATEGY_SHAPE_MISMATCH")
        by_id = {member["strategy_id"]: member for member in source_members}
        source_members = tuple(by_id[strategy_id] for strategy_id in prepared.strategy_ids)
    days = _common_days(prepared, common_days)
    drawdown = _decimal(max_dd, "max_dd")
    if not Decimal(0) < drawdown < Decimal(1):
        raise ValueError("max_dd must be between zero and one")
    if isinstance(capacities, Mapping):
        consumed_capacity_keys: list[Any] = []
        for strategy_id in prepared.strategy_ids:
            keys = [strategy_id]
            if str(strategy_id) != strategy_id:
                keys.append(str(strategy_id))
            matches = [key for key in keys if key in capacities]
            if len(matches) > 1:
                raise ValueError("AMBIGUOUS_CAPACITY_KEY")
            if not matches:
                raise ValueError(f"MISSING_CAPACITY_{strategy_id}")
            consumed_capacity_keys.append(matches[0])
        if any(key not in consumed_capacity_keys for key in capacities):
            raise ValueError("UNKNOWN_CAPACITY_KEY")
    elif isinstance(capacities, (str, bytes)) or not isinstance(capacities, Sequence) or len(capacities) != n:
        raise ValueError("CAPACITY_SHAPE_MISMATCH")
    caps = tuple(_member_capacity(capacities, index, strategy_id, member) for index, (strategy_id, member) in enumerate(zip(prepared.strategy_ids, source_members)))
    coefficients = _coefficients(normalized_delta, n, days)
    if margin is not None and (margin_coefficients is not None or margin_kwargs is not None):
        raise ValueError("MARGIN_OPTION_CONFLICT")
    if margin is not None:
        margin_options = dict(margin)
        margin_coefficients = margin_options.pop("coefficients", margin_options.pop("margin_coefficients", None))
        margin_kwargs = margin_options
    if margin_coefficients is None and margin_kwargs is not None:
        return _result(FAIL, "MARGIN_BOUND_UNAVAILABLE")
    if margin_coefficients is not None and not _public_margin_coefficients_are_known(margin_coefficients):
        return _result(FAIL, "MARGIN_BOUND_UNAVAILABLE")
    if margin_coefficients is not None:
        domain_reason = _margin_domain_reason(margin_coefficients, prepared.strategy_ids, caps)
        if domain_reason is not None:
            return _result(FAIL, domain_reason)
    target = None if target_p30 is None else _decimal(target_p30, "target_p30", positive=True)
    available = None if bank_available is None else _decimal(bank_available, "bank_available", positive=True)
    upper_target = _sum_products(caps, tuple(max(coefficient, Decimal(0)) for coefficient in coefficients))
    if target is None and upper_target <= 0:
        return _result(FAIL, "NO_POSITIVE_TARGET")
    calls = 0
    error_reason: str | None = None
    solved: dict[Decimal, tuple[_Solution, PortfolioCandidate]] = {}
    ordered_candidates: list[PortfolioCandidate] = []

    def run_target(target_value: Decimal | None, *, maximize: bool = False) -> bool:
        nonlocal calls, error_reason
        calls += 1
        outcome = _solve_lp(
            normalized_delta,
            caps,
            coefficients,
            max_dd=drawdown,
            target=target_value,
            bank_available=available,
            maximize=maximize,
        )
        if outcome.status == "ERROR":
            error_reason = outcome.reason or "SOLVER_ERROR"
            return False
        if outcome.status != "PASS" or outcome.solution is None:
            return False
        solution = outcome.solution
        margin_failure: list[str] = []
        candidates = _candidates_for_solution(
            solution,
            normalized_delta,
            source_members,
            caps,
            max_dd=drawdown,
            common_days=days,
            target=target_value,
            profile_id=profile_id,
            scenario_id=scenario_id,
            margin_coefficients=margin_coefficients,
            margin_kwargs=margin_kwargs,
            bank_available=available,
            failure_reason=margin_failure,
        )
        if not candidates:
            error_reason = margin_failure[-1] if margin_failure else (
                "MARGIN_BOUND_FAILED" if margin_coefficients is not None else "LP_SOLUTION_INVALID"
            )
            return False
        if target_value is not None:
            solved[target_value] = (solution, candidates[0])
        ordered_candidates.extend(candidates)
        return True

    if target is not None:
        run_target(target)
    elif available is not None:
        run_target(None, maximize=True)
    else:
        attempted: set[Decimal] = set()
        for initial_target in _initial_targets(upper_target, max_targets):
            attempted.add(initial_target)
            run_target(initial_target)
            if error_reason is not None:
                break
        blocked: set[tuple[Decimal, Decimal]] = set()
        while error_reason is None and calls < max_targets:
            ordered = sorted(solved.items(), key=lambda item: item[0])
            intervals: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
            for (left_target, (left_solution, _)), (right_target, (right_solution, _)) in zip(ordered, ordered[1:]):
                interval = (left_target, right_target)
                if interval in blocked or not _interval_is_refinable(left_solution, right_solution, caps):
                    continue
                with localcontext() as context:
                    context.prec = _precision_for(left_target, right_target, upper_target)
                    gap = (right_target - left_target) / upper_target
                    midpoint = (left_target + right_target) / Decimal(2)
                if midpoint in attempted:
                    blocked.add(interval)
                    continue
                intervals.append((gap, left_target, right_target, midpoint))
            if not intervals:
                break
            _, left_target, right_target, midpoint = max(intervals, key=lambda item: (item[0], -item[1]))
            attempted.add(midpoint)
            if not run_target(midpoint):
                if error_reason is None:
                    blocked.add((left_target, right_target))
    if error_reason is not None:
        if ordered_candidates and error_reason != "SOLVER_ERROR":
            return SearchResult(
                status=PASS,
                total_combinations=calls,
                candidates=tuple(ordered_candidates),
                evaluated=len(ordered_candidates),
                warnings=(error_reason,),
                mode=WEIGHTED_V1,
            )
        return SearchResult(status=FAIL, reason=error_reason, total_combinations=calls, evaluated=0, mode=WEIGHTED_V1)
    if not ordered_candidates:
        if target is None and available is not None:
            reason = "LP_INFEASIBLE"
        elif target is None and available is None:
            reason = "FRONTIER_INFEASIBLE"
        else:
            reason = "TARGET_INFEASIBLE"
        return SearchResult(status=FAIL, reason=reason, total_combinations=calls, evaluated=0, mode=WEIGHTED_V1)
    candidates = tuple(ordered_candidates)
    if proposed_x is not None:
        candidates = _revalidate_proposed_x(
            candidates,
            proposed_x,
            normalized_delta,
            source_members,
            caps,
            max_dd=drawdown,
            common_days=days,
            target=target,
            profile_id=profile_id,
            scenario_id=scenario_id,
            margin_coefficients=margin_coefficients,
            margin_kwargs=margin_kwargs,
            bank_available=available,
            validator=proposal_validator,
        )
    return SearchResult(
        status=PASS,
        total_combinations=calls,
        candidates=candidates,
        evaluated=len(candidates),
        mode=WEIGHTED_V1,
    )


__all__ = [
    "WEIGHTED_V1", "LimiterReplayResult", "derive_priorities", "priority_details",
    "replay_limiter", "bank_for_path", "evaluate_weighted_path", "weighted_search",
]
