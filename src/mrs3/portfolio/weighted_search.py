"""Phase 3 weighted portfolio search.

The search is deliberately small: Phase 2 supplies one aligned participant
matrix, this module solves the peak-equity drawdown LP, and the existing
candidate-search data classes carry the compact result.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import json
import math
import os
import sys
import time
from typing import Any, Callable, Mapping, Sequence
import warnings

import numpy as np
import psutil
from scipy.optimize import OptimizeWarning
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from mrs3._portfolio_process_worker import (
    _ProcessBatchFailure,
    _ProcessBatchEvaluator,
    _bootstrap_indices_with_stats,
    _bootstrap_process_evaluator,
    _bootstrap_seed,
    _bootstrap_scenario_batch,
    _path,
    _precision_for,
    _sum_products,
    bank_for_path,
)

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
    x: Sequence[Any] | Mapping[Any, Any] | None = None,
    strategy_ids: Sequence[Any] | None = None,
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
        normalized_return = _member_value(cycle, "common_window_normalized_return", default=None)
        if normalized_return is None:
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index})
        try:
            # The adapter must provide one common-window contribution that is
            # already inclusive of boundary UPnL and non-overlapping costs.
            pnl_value = _decimal(normalized_return, "common_window_normalized_return")
            if x is not None:
                if isinstance(x, Mapping):
                    weight = x.get(strategy_id, _UNSET)
                    if weight is _UNSET and str(strategy_id) != strategy_id:
                        weight = x.get(str(strategy_id), _UNSET)
                elif strategy_ids is not None:
                    ids = tuple(strategy_ids)
                    if len(ids) != len(x):
                        raise ValueError("X_STRATEGY_SHAPE_MISMATCH")
                    try:
                        weight = x[ids.index(strategy_id)]
                    except (IndexError, ValueError, TypeError):
                        weight = _UNSET
                else:
                    weight = _UNSET
                if weight is _UNSET:
                    raise ValueError("X_STRATEGY_SHAPE_MISMATCH")
                weight = _decimal(weight, "x", nonnegative=True)
                with localcontext() as context:
                    context.prec = _precision_for(pnl_value, weight)
                    pnl_value *= weight
        except (TypeError, ValueError):
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_UNAVAILABLE", witness={"invalid_cycle": index})
        ordinal = _member_value(cycle, "source_ordinal", default=index)
        records.append({"index": index, "id": cycle_id, "strategy_id": strategy_id, "start": start_key, "end": end_key, "pnl": pnl_value, "ordinal": ordinal, "carry_in_count": carry_in_count, "weight": carry_in_count if carry_in_count else 1})
    if L == 0:
        if supplied is None or days is None:
            return LimiterReplayResult("UNKNOWN", None, reason="LIMITER_REPLAY_COMMON_P30_REQUIRED")
        with localcontext() as context:
            context.prec = _precision_for(tuple(item["pnl"] for item in records), days, supplied)
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
    with localcontext() as context:
        context.prec = _precision_for(tuple(item["pnl"] for item in records if item["index"] in accepted), days)
        p30 = sum((item["pnl"] for item in records if item["index"] in accepted), Decimal(0)) * Decimal(30) / days
    return LimiterReplayResult("MODEL", p30, tuple(item["id"] for item in ordered_records if item["index"] in accepted), tuple(item["id"] for item in ordered_records if item["index"] not in accepted), tuple(item["index"] in accepted for item in records), witness=replay_witness)


def _limiter_model_coefficients(
    cycles: Sequence[Mapping[str, Any]],
    strategy_ids: Sequence[Any],
    replay: LimiterReplayResult,
    *,
    common_days: Any,
) -> tuple[str, tuple[Decimal, ...] | None]:
    """Return per-USDT limiter P30 coefficients for one validated replay mask."""
    source = tuple(cycles)
    ids = tuple(strategy_ids)
    try:
        invalid_shape = (
            not isinstance(replay, LimiterReplayResult)
            or replay.status != "MODEL"
            or replay.p30_limiter is None
            or len(replay.accepted_mask) != len(source)
            or len(set(ids)) != len(ids)
            or any(type(value) is not bool for value in replay.accepted_mask)
        )
    except (TypeError, ValueError):
        invalid_shape = True
    if invalid_shape:
        return UNKNOWN, None
    try:
        days = _decimal(common_days, "common_days", positive=True)
        _decimal(replay.p30_limiter, "p30_limiter")
    except (TypeError, ValueError):
        return UNKNOWN, None
    try:
        positions = {strategy_id: index for index, strategy_id in enumerate(ids)}
        contributions: list[tuple[bool, int, Decimal]] = []
        for accepted, cycle in zip(replay.accepted_mask, source):
            strategy_id = _member_value(cycle, "strategy_id", default=None)
            if strategy_id not in positions or _member_value(cycle, "attribution_complete", default=None) is not True:
                return UNKNOWN, None
            value = _member_value(cycle, "common_window_normalized_return", default=None)
            contributions.append((accepted, positions[strategy_id], _decimal(value, "common_window_normalized_return")))
        coefficients = [Decimal(0)] * len(ids)
        with localcontext() as context:
            context.prec = _precision_for(days, tuple(value for _accepted, _index, value in contributions))
            scale = Decimal(30) / days
            for accepted, index, contribution in contributions:
                if accepted:
                    coefficients[index] += contribution * scale
    except (TypeError, ValueError, ArithmeticError):
        return UNKNOWN, None
    return "MODEL", tuple(coefficients)


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
        if value.is_zero():
            text = "0"
        else:
            text = format(value, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
        return {"decimal": text}
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


def stationary_bootstrap_indices(
    T: int,
    D: Any,
    *,
    history_step_minutes: Any = 5,
    seed: Any = 0,
    block_ordinal: int = 0,
    scenario_index: int = 0,
) -> tuple[int, ...]:
    """Generate one deterministic stationary-bootstrap index sequence."""
    if type(T) is not int or T <= 0:
        raise ValueError("BOOTSTRAP_HORIZON_INVALID")
    days = _decimal(D, "block_days", positive=True)
    step = _decimal(history_step_minutes, "history_step_minutes", positive=True)
    _bootstrap_seed(seed, block_ordinal, scenario_index)
    return _bootstrap_indices_with_stats(
        T,
        days,
        history_step_minutes=step,
        seed=seed,
        block_ordinal=block_ordinal,
        scenario_index=scenario_index,
    )[0]


def nearest_rank(values: Sequence[Any], quantile: Any = Decimal("0.95")) -> Decimal:
    """Return the nearest-rank quantile (one-based ceil rank, no interpolation)."""
    source = tuple(_decimal(value, "quantile_value") for value in values)
    if not source:
        raise ValueError("QUANTILE_EMPTY")
    q = _decimal(quantile, "quantile", positive=True)
    if q > Decimal(1):
        raise ValueError("QUANTILE_INVALID")
    with localcontext() as context:
        context.prec = _precision_for(source, q)
        rank = max(1, math.ceil(q * len(source)))
    return sorted(source)[rank - 1]


@dataclass(frozen=True, slots=True)
class _BootstrapBankResult:
    """Compact bootstrap banks; raw scenario paths are intentionally discarded."""

    historical_banks: tuple[Decimal, ...]
    scenario_banks: tuple[tuple[tuple[Decimal, ...], ...], ...]
    p95_banks: tuple[tuple[Decimal | None, ...], ...]
    risk_banks: tuple[Decimal | None, ...]
    manifest: Mapping[str, Any]
    identity: str = ""
    vector_digests: tuple[str, ...] = ()
    scenario_counts: tuple[int, ...] = ()
    complete: bool = True
    p95_witnesses: tuple[tuple[Mapping[str, Any] | None, ...], ...] = ()

    @property
    def risk_ready(self) -> bool:
        return self.complete

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def _worker_exception_type(value: Any, fallback: str = "WorkerFailure") -> str:
    return value if isinstance(value, str) and value.isidentifier() and len(value) <= 128 else fallback


def bootstrap_banks(
    normalized_delta: Sequence[Sequence[Any]],
    x_vectors: Sequence[Sequence[Any]],
    *,
    max_dd: Any,
    common_days: Any,
    seed: Any,
    block_days: Sequence[Any] = (1, 3, 7),
    scenarios_per_family: int = 1000,
    history_step_minutes: Any = 5,
    workers: int = 1,
    batch_size: int = 100,
    cancel: Callable[[], bool] | None = None,
    wall_time_limit_seconds: Any | None = None,
    prefix_result: _BootstrapBankResult | None = None,
    model_identity: Any = WEIGHTED_V1,
) -> _BootstrapBankResult:
    """Compute historical and stationary-bootstrap required banks in bounded batches.

    A single index sequence is reused for every vector in each scenario. Only the
    current scenario's sampled increments are materialized; scenario paths are
    reduced to their direct ``bank_for_path`` result immediately.
    """
    rows = tuple(tuple(_decimal(value, "normalized_delta") for value in row) for row in normalized_delta)
    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("NORMALIZED_DELTA_SHAPE_MISMATCH")
    vectors = tuple(tuple(_decimal(value, "x", nonnegative=True) for value in vector) for vector in x_vectors)
    if not vectors or any(len(vector) != len(rows[0]) for vector in vectors):
        raise ValueError("X_SHAPE_MISMATCH")
    days = _decimal(common_days, "common_days", positive=True)
    drawdown = _decimal(max_dd, "max_dd")
    if not Decimal(0) < drawdown < Decimal(1):
        raise ValueError("max_dd must be between zero and one")
    if type(scenarios_per_family) is not int or scenarios_per_family <= 0:
        raise ValueError("BOOTSTRAP_SCENARIO_COUNT_INVALID")
    if type(workers) is not int or workers <= 0:
        raise ValueError("BOOTSTRAP_WORKERS_INVALID")
    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("BOOTSTRAP_BATCH_SIZE_INVALID")
    if cancel is not None and not callable(cancel):
        raise TypeError("cancel must be callable")
    if wall_time_limit_seconds is None:
        wall_limit = None
    else:
        wall_limit = float(_decimal(wall_time_limit_seconds, "wall_time_limit_seconds", nonnegative=True))
    _bootstrap_seed(seed, 0, 0)
    step = _decimal(history_step_minutes, "history_step_minutes", positive=True)
    families = tuple(_decimal(value, "block_days", positive=True) for value in block_days)
    if not families or len(set(families)) != len(families):
        raise ValueError("BOOTSTRAP_BLOCK_DAYS_INVALID")
    T = len(rows)
    # Aggregate each r = normalized_delta @ x once, then resample r for every
    # family/scenario. This keeps all strategy vectors on exactly common indices.
    increments = tuple(tuple(_sum_products(row, vector) for row in rows) for vector in vectors)
    increment_array = np.asarray(increments, dtype=object)
    bootstrap_precision = _precision_for(increments, drawdown)
    historical = tuple(bank_for_path(_path(rows, vector), drawdown) for vector in vectors)
    vector_digests = tuple(_digest({"x": vector}) for vector in vectors)
    if len(set(vector_digests)) != len(vector_digests):
        raise ValueError("BOOTSTRAP_VECTOR_IDENTITY_DUPLICATE")
    identity = _digest({
        "model_identity": model_identity,
        "normalized_delta": rows,
        "max_dd": drawdown,
        "common_days": days,
        "seed": seed,
        "history_step_minutes": step,
        "block_days": families,
    })
    prefix_counts = [0] * len(families)
    restart_totals = [0] * len(families)
    coverage_masks = [0] * len(families)
    scenario_banks: list[list[list[Decimal]]] = [[[] for _ in families] for _ in vectors]
    completed_task_ids: set[int] = set()
    if prefix_result is not None:
        old_vector_digests = tuple(prefix_result.vector_digests) if isinstance(prefix_result, _BootstrapBankResult) else ()
        old_vector_index = {digest: index for index, digest in enumerate(old_vector_digests)}
        if (
            not isinstance(prefix_result, _BootstrapBankResult)
            or prefix_result.identity != identity
            or not prefix_result.complete
            or len(old_vector_digests) != len(prefix_result.scenario_banks)
            or len(set(old_vector_digests)) != len(old_vector_digests)
            or any(digest not in old_vector_index for digest in vector_digests)
        ):
            raise ValueError("BOOTSTRAP_PREFIX_INVALID")
        prefix_counts = list(prefix_result.scenario_counts)
        if len(prefix_counts) != len(families) or any(count < 0 or count > scenarios_per_family for count in prefix_counts):
            raise ValueError("BOOTSTRAP_PREFIX_INVALID")
        if any(len(item) != len(families) for item in prefix_result.scenario_banks):
            raise ValueError("BOOTSTRAP_PREFIX_INVALID")
        for vector_index, digest in enumerate(vector_digests):
            vector_families = prefix_result.scenario_banks[old_vector_index[digest]]
            for ordinal, values in enumerate(vector_families):
                if len(values) != prefix_counts[ordinal]:
                    raise ValueError("BOOTSTRAP_PREFIX_INVALID")
                scenario_banks[vector_index][ordinal].extend(values)
        old_manifest = prefix_result.manifest
        old_restarts = tuple(old_manifest.get("restart_counts", ()))
        old_masks = tuple(old_manifest.get("coverage_masks", ()))
        if len(old_restarts) != len(families) or len(old_masks) != len(families):
            raise ValueError("BOOTSTRAP_PREFIX_INVALID")
        restart_totals[:] = [int(value) for value in old_restarts]
        coverage_masks[:] = [int(value) for value in old_masks]
        completed_task_ids.update(int(value) for value in old_manifest.get("actual_completed_task_ids", ()))
    tasks: list[dict[str, Any]] = []
    for ordinal, start in enumerate(prefix_counts):
        for scenario_start in range(start, scenarios_per_family, batch_size):
            tasks.append({
                "task_id": ordinal * 1_000_000_000 + scenario_start,
                "family_ordinal": ordinal,
                "scenario_start": scenario_start,
                "scenario_count": min(batch_size, scenarios_per_family - scenario_start),
            })
    task_by_id = {int(task["task_id"]): task for task in tasks}
    task_count = len(tasks)
    new_scenario_count = sum(scenarios_per_family - count for count in prefix_counts)
    available_memory = max(1, int(psutil.virtual_memory().available))
    sampled_decimal_bytes = sys.getsizeof(increments[0][0]) if increments and increments[0] else 0
    object_itemsize = int(np.dtype(object).itemsize)
    sampled_int_bytes = sys.getsizeof(0)
    increment_storage_bytes = int(increment_array.nbytes) + T * len(vectors) * sampled_decimal_bytes
    resident_worker_bytes = 2 * T * len(vectors) * (sampled_decimal_bytes + object_itemsize)
    scenario_temporary_bytes = T * (sampled_int_bytes + object_itemsize)
    retained_batch_bytes = batch_size * len(vectors) * (sampled_decimal_bytes + object_itemsize)
    estimated_task_bytes = max(1, increment_storage_bytes + resident_worker_bytes + scenario_temporary_bytes + retained_batch_bytes)
    memory_width = max(1, available_memory // estimated_task_bytes)
    actual_width = min(workers, max(1, task_count), os.cpu_count() or 1, memory_width)
    worker_context = (increment_array, T, families, step, seed, drawdown, bootstrap_precision)
    process_batch = None
    worker_failure_type: str | None = None
    process_init_failed = False
    if actual_width > 1:
        try:
            process_batch = _ProcessBatchEvaluator(_bootstrap_process_evaluator, worker_context, actual_width, task_count)
            actual_width = max(1, int(getattr(process_batch, "width", actual_width)))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            worker_failure_type = type(error).__name__
            process_init_failed = True
            process_batch = None
            actual_width = 1
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    worker_wall = 0.0
    worker_cpu = 0.0
    worker_peak_rss = 0
    parent_process = psutil.Process(os.getpid())
    stopping_reason: str | None = "WORKER_FAILURE" if process_init_failed else None
    try:
        for task_offset in range(0, len(tasks), max(1, actual_width)):
            if stopping_reason is not None:
                break
            task_group = tasks[task_offset:task_offset + max(1, actual_width)]
            if cancel is not None and cancel():
                stopping_reason = "CANCELLED"
                break
            if wall_limit is not None and time.perf_counter() - started_wall >= wall_limit:
                stopping_reason = "WALL_TIME_LIMIT"
                break
            if process_batch is None:
                raw_results = tuple((task["task_id"], _bootstrap_scenario_batch(task, worker_context)) for task in task_group)
                batch_failed = False
            else:
                batch_failed = False
                try:
                    raw_results = process_batch(tuple((task["task_id"], (task,)) for task in task_group))
                except _ProcessBatchFailure as error:
                    raw_results = error.results
                    worker_failure_type = error.exception_type
                    batch_failed = True
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as error:
                    raw_results = ()
                    worker_failure_type = type(error).__name__
                    batch_failed = True
            group_worker_rss = 0
            ordered_results: list[tuple[int, Any]] = []
            group_task_ids = tuple(sorted(int(task["task_id"]) for task in task_group))
            try:
                for item in raw_results:
                    if not isinstance(item, tuple) or len(item) != 2 or type(item[0]) is not int:
                        raise ValueError("WORKER_RESULT_SHAPE_MISMATCH")
                    ordered_results.append((item[0], item[1]))
                ordered_results.sort(key=lambda item: item[0])
                returned_task_ids = tuple(item[0] for item in ordered_results)
                if len(set(returned_task_ids)) != len(returned_task_ids) or returned_task_ids != group_task_ids:
                    raise ValueError("WORKER_TASK_ID_MISMATCH")
            except (TypeError, ValueError) as error:
                worker_failure_type = worker_failure_type or type(error).__name__
                stopping_reason = "WORKER_FAILURE"
            if stopping_reason == "WORKER_FAILURE":
                ordered_results = []
            validated_results: list[tuple[int, int, int, Sequence[Any], int, int, float, float, int]] = []
            if ordered_results:
                try:
                    for task_id, payload in ordered_results:
                        if not isinstance(payload, Mapping) or payload.get("status") == "UNKNOWN":
                            worker_failure_type = worker_failure_type or _worker_exception_type(
                                payload.get("exception_type") if isinstance(payload, Mapping) else None,
                                "WorkerPayload",
                            )
                            raise ValueError("WORKER_PAYLOAD_INVALID")
                        expected_task = task_by_id[task_id]
                        ordinal = int(payload["family_ordinal"])
                        scenario_start = int(payload["scenario_start"])
                        scenario_count = int(payload["scenario_count"])
                        payload_task_id = payload["task_id"]
                        banks = payload["banks"]
                        valid_shape = (
                            type(payload_task_id) is int
                            and payload_task_id == task_id
                            and scenario_start == expected_task["scenario_start"]
                            and ordinal == expected_task["family_ordinal"]
                            and scenario_count == expected_task["scenario_count"]
                            and 0 <= ordinal < len(families)
                            and scenario_count > 0
                            and isinstance(banks, Sequence)
                            and not isinstance(banks, (str, bytes, bytearray))
                            and len(banks) == len(vectors)
                            and all(
                                isinstance(values, Sequence)
                                and not isinstance(values, (str, bytes, bytearray))
                                and len(values) == scenario_count
                                for values in banks
                            )
                        )
                        if not valid_shape:
                            raise ValueError("WORKER_PAYLOAD_SHAPE_MISMATCH")
                        validated_results.append((
                            task_id,
                            ordinal,
                            scenario_count,
                            banks,
                            int(payload["restart_count"]),
                            int(payload["coverage_mask"]),
                            float(payload.get("wall_seconds", 0.0)),
                            float(payload.get("process_cpu_seconds", 0.0)),
                            int(payload.get("peak_rss_bytes", 0)),
                        ))
                except (KeyError, TypeError, ValueError, OverflowError) as error:
                    worker_failure_type = worker_failure_type or type(error).__name__
                    stopping_reason = "WORKER_FAILURE"
                    validated_results = []
            for task_id, ordinal, completed_count, banks, restart_count, coverage_mask, wall_seconds, process_cpu_seconds, peak_rss_bytes in validated_results:
                completed_task_ids.add(task_id)
                prefix_counts[ordinal] += completed_count
                restart_totals[ordinal] += restart_count
                coverage_masks[ordinal] |= coverage_mask
                for vector_index, values in enumerate(banks):
                    scenario_banks[vector_index][ordinal].extend(values)
                worker_wall += wall_seconds
                worker_cpu += process_cpu_seconds
                group_worker_rss += peak_rss_bytes
            if process_batch is not None:
                worker_peak_rss = max(worker_peak_rss, parent_process.memory_info().rss + group_worker_rss)
            else:
                worker_peak_rss = max(worker_peak_rss, group_worker_rss)
            if batch_failed:
                stopping_reason = "WORKER_FAILURE"
            if stopping_reason is not None:
                break
            if cancel is not None and cancel():
                stopping_reason = "CANCELLED"
                break
            if wall_limit is not None and time.perf_counter() - started_wall >= wall_limit:
                stopping_reason = "WALL_TIME_LIMIT"
                break
    finally:
        if process_batch is not None:
            process_batch.close()
    if any(
        len(scenario_banks[vector_index][ordinal]) != prefix_counts[ordinal]
        for vector_index in range(len(vectors))
        for ordinal in range(len(families))
    ):
        stopping_reason = "WORKER_FAILURE"
        worker_failure_type = worker_failure_type or "WorkerPayloadShapeMismatch"
    all_task_ids = tuple(sorted(completed_task_ids | {int(task["task_id"]) for task in tasks}))
    unexplored_task_ids = tuple(task_id for task_id in all_task_ids if task_id not in completed_task_ids)
    complete = stopping_reason is None and all(count == scenarios_per_family for count in prefix_counts) and not unexplored_task_ids
    if not complete and stopping_reason is None:
        stopping_reason = "INCOMPLETE"
    p95_banks: list[list[Decimal | None]] = [[] for _ in vectors]
    p95_witnesses: list[list[Mapping[str, Any] | None]] = [[] for _ in vectors]
    for vector_index in range(len(vectors)):
        for ordinal, values in enumerate(scenario_banks[vector_index]):
            if complete:
                p95 = nearest_rank(values)
                p95_banks[vector_index].append(p95)
                p95_witnesses[vector_index].append({
                    "family_ordinal": ordinal,
                    "scenario_index": values.index(p95),
                })
            else:
                p95_banks[vector_index].append(None)
                p95_witnesses[vector_index].append(None)
    risk_banks: tuple[Decimal | None, ...]
    if complete:
        risk_banks = tuple(max((historical[index], *(value for value in p95_banks[index] if value is not None)), default=Decimal(0)) for index in range(len(vectors)))
    else:
        risk_banks = tuple(None for _ in vectors)
    diagnostics: list[str] = []
    family_manifest: list[Mapping[str, Any]] = []
    for ordinal, family_days in enumerate(families):
        with localcontext() as decimal_context:
            decimal_context.prec = _precision_for(step, family_days)
            probability = step / (Decimal(1440) * family_days)
        if not Decimal(0) < probability <= Decimal(1):
            raise ValueError("BOOTSTRAP_PROBABILITY_INVALID")
        with localcontext() as decimal_context:
            decimal_context.prec = _precision_for(days, family_days)
            common_to_block_ratio = days / family_days
            short = common_to_block_ratio < Decimal(10)
        family_diagnostics = ("COMMON_DAYS_LT_10_BLOCK_DAYS",) if short else ()
        if short:
            diagnostics.append(f"COMMON_DAYS_LT_10_BLOCK_DAYS:{format(family_days, 'f')}")
        coverage_count = int(coverage_masks[ordinal]).bit_count()
        with localcontext() as decimal_context:
            decimal_context.prec = _precision_for(coverage_count, T)
            coverage_fraction = Decimal(coverage_count) / Decimal(T)
        family_manifest.append({
            "block_ordinal": ordinal,
            "mean_block_days": family_days,
            "common_days_over_mean_block_days": common_to_block_ratio,
            "restart_probability": probability,
            "scenario_count": scenarios_per_family,
            "completed_scenario_count": prefix_counts[ordinal],
            "unexplored_scenario_count": scenarios_per_family - prefix_counts[ordinal],
            "restart_count": restart_totals[ordinal],
            "history_coverage": coverage_count,
            "history_coverage_fraction": coverage_fraction,
            "diagnostics": family_diagnostics,
        })
    manifest = {
        "numpy_version": np.__version__,
        "input_identity": identity,
        "vector_digests": tuple(sorted(vector_digests)),
        "seed": seed,
        "seed_sequence": ("campaign_seed", "block_ordinal", "scenario_index"),
        "history_step_minutes": step,
        "common_days": days,
        "block_days": families,
        "scenarios_per_family": scenarios_per_family,
        "families": tuple(family_manifest),
        "restart_counts": tuple(restart_totals),
        "coverage_masks": tuple(coverage_masks),
        "history_coverages": tuple(int(mask).bit_count() for mask in coverage_masks),
        "diagnostics": tuple(diagnostics),
        "complete": complete,
        "actual_completed_task_ids": tuple(sorted(completed_task_ids)),
        "unexplored_task_ids": unexplored_task_ids,
        "actual_completed_task_count": len(completed_task_ids),
        "unexplored_task_count": len(unexplored_task_ids),
        "actual_completed_scenario_count": sum(prefix_counts),
        "new_scenario_count": new_scenario_count,
        "unexplored_scenario_count": sum(scenarios_per_family - count for count in prefix_counts),
        "stopping_reason": stopping_reason,
        "worker_failure": (
            None
            if stopping_reason != "WORKER_FAILURE"
            else {"reason": "WORKER_FAILURE", "exception_type": _worker_exception_type(worker_failure_type)}
        ),
        "operational": {
            "worker_width": actual_width if process_batch is not None else 1,
            "wall_seconds": time.perf_counter() - started_wall,
            "worker_wall_seconds": worker_wall,
            "process_cpu_seconds": worker_cpu or time.process_time() - started_cpu,
            "peak_rss_bytes": worker_peak_rss or psutil.Process(os.getpid()).memory_info().rss,
            "ready_task_count": task_count,
            "available_memory_bytes": available_memory,
        },
    }
    return _BootstrapBankResult(
        historical,
        tuple(tuple(tuple(values) for values in vector_families) for vector_families in scenario_banks),
        tuple(tuple(values) for values in p95_banks),
        risk_banks,
        manifest,
        identity,
        vector_digests,
        tuple(prefix_counts),
        complete,
        tuple(tuple(value for value in vector_witnesses) for vector_witnesses in p95_witnesses),
    )


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
    solver_status: int | None = None
    solver_message: str | None = None
    residuals: Mapping[str, Decimal] | None = None
    budget_limited: bool = False
    optimal: bool = False


def _cumulative(normalized_delta: Sequence[Sequence[Decimal]], count: int) -> tuple[tuple[Decimal, ...], ...]:
    with localcontext() as context:
        context.prec = _precision_for(normalized_delta)
        totals = [Decimal(0)] * count
        result: list[tuple[Decimal, ...]] = []
        for row in normalized_delta:
            totals = [total + value for total, value in zip(totals, row)]
            result.append(tuple(totals))
        return tuple(result)


def _append_sparse_constraint(
    rows: list[int],
    columns: list[int],
    values: list[float],
    right_hand_sides: list[float],
    entries: Sequence[tuple[int, float]],
    right_hand_side: float,
) -> None:
    row_index = len(right_hand_sides)
    for column, value in entries:
        if value != 0.0:
            rows.append(row_index)
            columns.append(column)
            values.append(value)
    right_hand_sides.append(right_hand_side)


def _validated_lp_margin_coefficients(
    margin_a: Sequence[Any] | None,
    margin_b: Sequence[Any] | None,
    max_mm_load: Any | None,
    count: int,
) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...], Decimal] | None:
    if margin_a is None and margin_b is None and max_mm_load is None:
        return None
    if margin_a is None or margin_b is None or max_mm_load is None:
        raise ValueError("MARGIN_COEFFICIENTS_REQUIRED")
    if isinstance(margin_a, (str, bytes)) or isinstance(margin_b, (str, bytes)):
        raise TypeError("margin coefficients must be sequences")
    if not isinstance(margin_a, Sequence) or not isinstance(margin_b, Sequence):
        raise TypeError("margin coefficients must be sequences")
    a_values = tuple(_decimal(value, "margin_a", nonnegative=True) for value in margin_a)
    b_values = tuple(_decimal(value, "margin_b", nonnegative=True) for value in margin_b)
    if len(a_values) != count or len(b_values) != count:
        raise ValueError("MARGIN_COEFFICIENT_SHAPE_MISMATCH")
    mm_load = _decimal(max_mm_load, "max_mm_load", positive=True)
    if not mm_load < Decimal(1):
        raise ValueError("max_mm_load must be below one")
    return a_values, b_values, mm_load


def _solve_lp_unchecked(
    normalized_delta: tuple[tuple[Decimal, ...], ...],
    capacities: tuple[Decimal, ...],
    coefficients: tuple[Decimal, ...],
    *,
    max_dd: Decimal,
    target: Decimal | None,
    bank_available: Decimal | None,
    maximize: bool,
    margin_a: Sequence[Any] | None = None,
    margin_b: Sequence[Any] | None = None,
    max_mm_load: Any | None = None,
    time_limit: Any = Decimal("30"),
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
    margin_coefficients = _validated_lp_margin_coefficients(margin_a, margin_b, max_mm_load, n)
    solver_time_limit = _decimal(time_limit, "time_limit", positive=True)
    a_rows: list[int] = []
    a_columns: list[int] = []
    a_values: list[float] = []
    b_ub: list[float] = []
    with localcontext() as context:
        context.prec = _precision_for(max_dd)
        one_minus = Decimal(1) - max_dd
    cumulative = _cumulative(normalized_delta, n)
    for row, h_index in zip(cumulative, range(h_start, h_start + t)):
        # h_t >= g_t
        entries = [(x_start + index, float(value)) for index, value in enumerate(row)]
        entries.append((h_index, -1.0))
        _append_sparse_constraint(a_rows, a_columns, a_values, b_ub, entries, 0.0)
        # h_t >= h_(t-1), with h_0 fixed at zero by its bound.
        if h_index > h_start:
            _append_sparse_constraint(
                a_rows,
                a_columns,
                a_values,
                b_ub,
                ((h_index - 1, 1.0), (h_index, -1.0)),
                0.0,
            )
        # g_t >= (1-m)h_t - mB
        entries = [(bank_index, -float(max_dd))]
        entries.extend((x_start + index, -float(value)) for index, value in enumerate(row))
        entries.append((h_index, float(one_minus)))
        _append_sparse_constraint(a_rows, a_columns, a_values, b_ub, entries, 0.0)
    if margin_coefficients is not None:
        margin_a_values, margin_b_values, mm_load = margin_coefficients
        # Discovery intentionally uses only the necessary initial IM/MM bounds;
        # the off-limiter reserve is an acceptance constraint, not an LP bound.
        _append_sparse_constraint(
            a_rows,
            a_columns,
            a_values,
            b_ub,
            tuple([(bank_index, -float(one_minus))] + [
                (x_start + index, float(value)) for index, value in enumerate(margin_a_values)
            ]),
            0.0,
        )
        _append_sparse_constraint(
            a_rows,
            a_columns,
            a_values,
            b_ub,
            tuple([(bank_index, -float(mm_load * one_minus))] + [
                (x_start + index, float(value)) for index, value in enumerate(margin_b_values)
            ]),
            0.0,
        )
    if target is not None:
        _append_sparse_constraint(
            a_rows,
            a_columns,
            a_values,
            b_ub,
            tuple((x_start + index, -float(value)) for index, value in enumerate(coefficients)),
            -float(target),
        )
    a_ub = coo_matrix((a_values, (a_rows, a_columns)), shape=(len(b_ub), size), dtype=float).tocsr()
    upper_bank = None if bank_available is None else float(bank_available)
    bounds = [(1.0, upper_bank)] + [(0.0, float(value)) for value in capacities] + [(0.0, None)] * t
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=OptimizeWarning,
                message=r"Unrecognized options detected:.*threads",
            )
            result = linprog(
                c,
                A_ub=a_ub,
                b_ub=b_ub,
                bounds=bounds,
                method="highs",
                options={"threads": 1, "time_limit": float(solver_time_limit)},
            )
    except (ArithmeticError, ValueError, TypeError, RuntimeError):
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR")
    solver_status = getattr(result, "status", None)
    try:
        solver_status = None if solver_status is None else int(solver_status)
    except (TypeError, ValueError, OverflowError):
        solver_status = None
    budget_limited = solver_status == 1
    optimal = bool(getattr(result, "success", False)) and not budget_limited
    solver_message = getattr(result, "message", None)
    residuals: dict[str, Decimal] = {}
    for name in ("ineqlin", "lower", "upper"):
        section = getattr(result, name, None)
        values = getattr(section, "residual", None)
        if values is None:
            continue
        try:
            finite_values = [float(value) for value in values if math.isfinite(float(value))]
        except (TypeError, ValueError):
            continue
        if finite_values:
            residuals[name] = Decimal(str(min(finite_values)))
    metadata = {
        "solver_status": solver_status,
        "solver_message": solver_message,
        "residuals": residuals,
        "budget_limited": budget_limited,
        "optimal": optimal,
    }
    if not getattr(result, "success", False) and not budget_limited:
        if solver_status == 2:
            return _SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE", **metadata)
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR", **metadata)
    if result.x is None or len(result.x) != size:
        return _SolveOutcome(
            "ERROR",
            reason="SOLVER_ERROR",
            **metadata,
        )
    if any(not math.isfinite(float(value)) for value in result.x):
        return _SolveOutcome(
            "ERROR",
            reason="SOLVER_ERROR",
            **metadata,
        )
    raw_bank = Decimal(str(result.x[bank_index]))
    raw_x = tuple(Decimal(str(result.x[x_start + index])) for index in range(n))
    raw_h = tuple(Decimal(str(result.x[h_start + index])) for index in range(t))
    with localcontext() as context:
        context.prec = _precision_for(raw_bank, raw_x, capacities, bank_available)
        if raw_bank < Decimal(1) - _solver_tolerance(raw_bank):
            return _SolveOutcome(
                "ERROR",
                reason="LP_SOLUTION_INVALID",
                **metadata,
            )
        if any(value < -_solver_tolerance(value, cap) or value > cap + _solver_tolerance(value, cap) for value, cap in zip(raw_x, capacities)):
            return _SolveOutcome(
                "ERROR",
                reason="LP_SOLUTION_INVALID",
                **metadata,
            )
        if bank_available is not None and raw_bank > bank_available + _solver_tolerance(raw_bank, bank_available):
            return _SolveOutcome(
                "ERROR",
                reason="BANK_UNAVAILABLE",
                **metadata,
            )
    raw_bank = max(Decimal(1), raw_bank)
    if bank_available is not None:
        raw_bank = min(raw_bank, bank_available)
    raw_x = tuple(min(cap, max(Decimal(0), value)) for value, cap in zip(raw_x, capacities))
    margin_required = Decimal(1)
    if margin_coefficients is not None:
        margin_a_values, margin_b_values, mm_load = margin_coefficients
        with localcontext() as context:
            context.prec = _precision_for(margin_a_values, margin_b_values, raw_x, raw_bank, one_minus, mm_load)
            initial_im = _sum_products(margin_a_values, raw_x)
            initial_mm = _sum_products(margin_b_values, raw_x)
            initial_im_bound = one_minus * raw_bank
            initial_mm_bound = mm_load * initial_im_bound
            if initial_im > initial_im_bound + _solver_tolerance(initial_im, initial_im_bound):
                return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID", **metadata)
            if initial_mm > initial_mm_bound + _solver_tolerance(initial_mm, initial_mm_bound):
                return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID", **metadata)
            margin_required = max(
                Decimal(1),
                initial_im / one_minus,
                initial_mm / (mm_load * one_minus),
            )
    with localcontext() as context:
        context.prec = _precision_for(cumulative, raw_x, raw_h, raw_bank)
        for index, (gain_row, h_value) in enumerate(zip(cumulative, raw_h)):
            gain = _sum_products(gain_row, raw_x)
            previous = Decimal(0) if index == 0 else raw_h[index - 1]
            target_gain = max(gain, previous)
            dd_rhs = one_minus * h_value - max_dd * raw_bank
            if h_value < target_gain - _solver_tolerance(h_value, target_gain) or gain < dd_rhs - _solver_tolerance(gain, dd_rhs):
                return _SolveOutcome(
                    "ERROR",
                    reason="LP_SOLUTION_INVALID",
                    **metadata,
                )
    path = _path(normalized_delta, raw_x)
    required = bank_for_path(path, max_dd)
    authoritative_bank = max(Decimal(1), required, margin_required)
    with localcontext() as context:
        context.prec = _precision_for(required, authoritative_bank, raw_bank, coefficients, raw_x, target)
        if authoritative_bank > raw_bank + _solver_tolerance(authoritative_bank, raw_bank):
            return _SolveOutcome(
                "ERROR",
                reason="BANK_UNAVAILABLE" if bank_available is not None else "LP_SOLUTION_INVALID",
                **metadata,
            )
        if bank_available is not None and authoritative_bank > bank_available:
            return _SolveOutcome(
                "ERROR",
                reason="BANK_UNAVAILABLE",
                **metadata,
            )
        p30 = _sum_products(coefficients, raw_x)
        if target is not None and p30 < target - _solver_tolerance(p30, target):
            return _SolveOutcome(
                "ERROR",
                reason="LP_SOLUTION_INVALID",
                **metadata,
            )
    return _SolveOutcome(
        "PASS",
        _Solution(authoritative_bank, raw_x),
        **metadata,
    )


def _solve_lp(*args: Any, **kwargs: Any) -> _SolveOutcome:
    """Run model assembly and HiGHS behind one fail-closed boundary."""
    try:
        return _solve_lp_unchecked(*args, **kwargs)
    except (ArithmeticError, ValueError, TypeError, RuntimeError):
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR")


def _cdar_money(drawdowns: Sequence[Any], tail_fraction: Any) -> Decimal:
    values = tuple(_decimal(value, "drawdown", nonnegative=True) for value in drawdowns)
    if not values:
        raise ValueError("CDAR_EMPTY")
    tail_share = _decimal(tail_fraction, "tail_fraction", positive=True)
    if tail_share > Decimal(1):
        raise ValueError("CDAR_TAIL_INVALID")
    with localcontext() as context:
        context.prec = _precision_for(values, tail_share)
        tail = tail_share * Decimal(len(values))
        remaining = tail
        weighted = Decimal(0)
        for value in sorted(values, reverse=True):
            if remaining <= 0:
                break
            weight = min(Decimal(1), remaining)
            weighted += value * weight
            remaining -= weight
        return weighted / tail


def _cdar80_money(drawdowns: Sequence[Any]) -> Decimal:
    return _cdar_money(drawdowns, Decimal("0.20"))


def _cdar90_money(drawdowns: Sequence[Any]) -> Decimal:
    return _cdar_money(drawdowns, Decimal("0.10"))


def _additional_witness_rows(
    normalized_delta: Sequence[Sequence[Any]],
    witness: Mapping[str, Any] | None,
    manifest: Mapping[str, Any] | None,
) -> tuple[tuple[Decimal, ...], ...] | None:
    if witness is None:
        if manifest is not None:
            raise ValueError("BOOTSTRAP_WITNESS_REQUIRED")
        return None
    if manifest is None or not isinstance(witness, Mapping):
        raise ValueError("BOOTSTRAP_WITNESS_INVALID")
    try:
        ordinal = witness["family_ordinal"]
        scenario = witness["scenario_index"]
        families = manifest["families"]
        family_days = families[ordinal]["mean_block_days"]
        seed = manifest["seed"]
        step = manifest["history_step_minutes"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("BOOTSTRAP_WITNESS_INVALID") from None
    if type(ordinal) is not int or ordinal < 0 or type(scenario) is not int or scenario < 0:
        raise ValueError("BOOTSTRAP_WITNESS_INVALID")
    rows = tuple(tuple(_decimal(value, "normalized_delta") for value in row) for row in normalized_delta)
    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("NORMALIZED_DELTA_SHAPE_MISMATCH")
    indices = stationary_bootstrap_indices(
        len(rows),
        family_days,
        history_step_minutes=step,
        seed=seed,
        block_ordinal=ordinal,
        scenario_index=scenario,
    )
    return tuple(rows[index] for index in indices)


def _solve_additional_lp(
    normalized_delta: Sequence[Sequence[Any]],
    capacities: Sequence[Any],
    *,
    bank_fixed: Any,
    max_dd: Any,
    objective_coefficients: Sequence[Any],
    L: int,
    priorities: Sequence[int],
    margin_a: Sequence[Any],
    margin_b: Sequence[Any],
    max_mm_load: Any,
    reserve: Any,
    limiter_release_status: str = UNKNOWN,
    p95_witness: Mapping[str, Any] | None = None,
    bootstrap_manifest: Mapping[str, Any] | None = None,
    time_limit: Any = Decimal("30"),
    p30_floor: Any | None = None,
    p30_floor_coefficients: Sequence[Any] | None = None,
    _cdar: bool = False,
) -> _SolveOutcome:
    """Solve one fixed-bank redistribution LP without wiring search orchestration."""
    rows = tuple(tuple(_decimal(value, "normalized_delta") for value in row) for row in normalized_delta)
    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("NORMALIZED_DELTA_SHAPE_MISMATCH")
    n = len(rows[0])
    caps = tuple(_decimal(value, "capacity", nonnegative=True) for value in capacities)
    if len(caps) != n:
        raise ValueError("CAPACITY_SHAPE_MISMATCH")
    objective = tuple(_decimal(value, "objective_coefficients") for value in objective_coefficients)
    if len(objective) != n:
        raise ValueError("OBJECTIVE_SHAPE_MISMATCH")
    priorities_values = tuple(priorities)
    if len(priorities_values) != n or any(type(value) is not int or not 1 <= value <= 5 for value in priorities_values):
        raise ValueError("PRIORITY_SHAPE_MISMATCH")
    if type(L) is not int or L < 0 or (L != 0 and L >= n):
        raise ValueError("LIMITER_RANGE_INVALID")
    status = str(limiter_release_status).upper()
    if status not in {"UNKNOWN", "CONFIRMED"}:
        raise ValueError("LIMITER_RELEASE_STATUS_UNKNOWN")
    bank = _decimal(bank_fixed, "bank_fixed", positive=True)
    drawdown = _decimal(max_dd, "max_dd")
    if not Decimal(0) < drawdown < Decimal(1):
        raise ValueError("max_dd must be between zero and one")
    reserve_value = _decimal(reserve, "reserve", nonnegative=True)
    if reserve_value >= Decimal(1):
        raise ValueError("reserve must be below one")
    margin_coefficients = _validated_lp_margin_coefficients(margin_a, margin_b, max_mm_load, n)
    assert margin_coefficients is not None
    margin_a_values, margin_b_values, mm_load = margin_coefficients
    floor = None if p30_floor is None else _decimal(p30_floor, "p30_floor", nonnegative=True)
    floor_coefficients = objective if p30_floor_coefficients is None else tuple(
        _decimal(value, "p30_floor_coefficients") for value in p30_floor_coefficients
    )
    if len(floor_coefficients) != n:
        raise ValueError("P30_FLOOR_SHAPE_MISMATCH")
    witness_rows = _additional_witness_rows(rows, p95_witness, bootstrap_manifest)
    paths = (rows,) if witness_rows is None else (rows, witness_rows)
    cumulative_paths = tuple(_cumulative(path, n) for path in paths)
    one_minus_m = Decimal(1) - drawdown
    one_minus_r = Decimal(1) - reserve_value
    ell = n if L == 0 else L
    remaining = n - ell
    full_indices: list[int] = []
    boundary_indices: tuple[int, ...] = ()
    boundary_count = 0
    for priority in range(5, 0, -1):
        group = tuple(index for index, value in enumerate(priorities_values) if value == priority)
        if remaining <= 0:
            break
        if len(group) <= remaining:
            full_indices.extend(group)
            remaining -= len(group)
        else:
            boundary_indices = group
            boundary_count = remaining
            remaining = 0
            break

    bank_index = 0
    x_start = 1
    next_column = 1 + n
    h_blocks: list[tuple[int, ...]] = []
    for _path_rows in cumulative_paths:
        block = tuple(range(next_column, next_column + len(_path_rows)))
        next_column += len(_path_rows)
        h_blocks.append(block)
    le_index = next_column
    next_column += 1
    loss_z_index: int | None = None
    loss_w_indices: dict[int, int] = {}
    if boundary_indices:
        loss_z_index = next_column
        next_column += 1
        for index in boundary_indices:
            loss_w_indices[index] = next_column
            next_column += 1
    hold_z_index: int | None = None
    hold_w_indices: dict[int, int] = {}
    if status == "CONFIRMED":
        hold_z_index = next_column
        next_column += 1
        for index in range(n):
            hold_w_indices[index] = next_column
            next_column += 1
    cdar_z_index: int | None = None
    cdar_u_indices: tuple[int, ...] = ()
    if _cdar:
        cdar_z_index = next_column
        next_column += 1
        cdar_u_indices = tuple(range(next_column, next_column + len(cumulative_paths[0])))
        next_column += len(cdar_u_indices)
    size = next_column
    a_rows: list[int] = []
    a_columns: list[int] = []
    a_values: list[float] = []
    b_ub: list[float] = []

    for cumulative, h_block in zip(cumulative_paths, h_blocks):
        for row, h_index in zip(cumulative, h_block):
            _append_sparse_constraint(
                a_rows,
                a_columns,
                a_values,
                b_ub,
                tuple([(x_start + index, float(value)) for index, value in enumerate(row)] + [(h_index, -1.0)]),
                0.0,
            )
            if h_index > h_block[0]:
                _append_sparse_constraint(a_rows, a_columns, a_values, b_ub, ((h_index - 1, 1.0), (h_index, -1.0)), 0.0)
            _append_sparse_constraint(
                a_rows,
                a_columns,
                a_values,
                b_ub,
                tuple([(bank_index, -float(drawdown))] + [(x_start + index, -float(value)) for index, value in enumerate(row)] + [(h_index, float(one_minus_m))]),
                0.0,
            )

    _append_sparse_constraint(
        a_rows,
        a_columns,
        a_values,
        b_ub,
        tuple([(bank_index, -float(one_minus_m))] + [(x_start + index, float(value)) for index, value in enumerate(margin_a_values)] + [(le_index, 1.0)]),
        0.0,
    )
    _append_sparse_constraint(
        a_rows,
        a_columns,
        a_values,
        b_ub,
        tuple([(bank_index, -float(mm_load * one_minus_m))] + [(x_start + index, float(value)) for index, value in enumerate(margin_b_values)] + [(le_index, float(mm_load))]),
        0.0,
    )
    held_entries: list[tuple[int, float]] = [(bank_index, -float(one_minus_r * one_minus_m)), (le_index, float(one_minus_r))]
    if status == "UNKNOWN":
        held_entries.extend((x_start + index, float(value)) for index, value in enumerate(margin_a_values))
    else:
        held_entries.append((hold_z_index, float(ell)))
        held_entries.extend((hold_w_indices[index], 1.0) for index in range(n))
    _append_sparse_constraint(a_rows, a_columns, a_values, b_ub, tuple(held_entries), 0.0)

    if boundary_indices:
        for index in boundary_indices:
            _append_sparse_constraint(
                a_rows,
                a_columns,
                a_values,
                b_ub,
                ((x_start + index, 1.0), (loss_z_index, -1.0), (loss_w_indices[index], -1.0)),
                0.0,
            )
        loss_entries = [(le_index, -1.0)]
        loss_entries.extend((x_start + index, 0.015) for index in full_indices)
        loss_entries.append((loss_z_index, 0.015 * boundary_count))
        loss_entries.extend((loss_w_indices[index], 0.015) for index in boundary_indices)
        _append_sparse_constraint(a_rows, a_columns, a_values, b_ub, tuple(loss_entries), 0.0)
    elif full_indices:
        _append_sparse_constraint(
            a_rows,
            a_columns,
            a_values,
            b_ub,
            tuple([(le_index, -1.0)] + [(x_start + index, 0.015) for index in full_indices]),
            0.0,
        )

    if status == "CONFIRMED":
        for index in range(n):
            _append_sparse_constraint(
                a_rows,
                a_columns,
                a_values,
                b_ub,
                ((x_start + index, float(margin_a_values[index])), (hold_z_index, -1.0), (hold_w_indices[index], -1.0)),
                0.0,
            )

    if floor is not None:
        _append_sparse_constraint(
            a_rows,
            a_columns,
            a_values,
            b_ub,
            tuple((x_start + index, -float(value)) for index, value in enumerate(floor_coefficients)),
            -float(floor),
        )

    if _cdar:
        for row, h_index, u_index in zip(cumulative_paths[0], h_blocks[0], cdar_u_indices):
            _append_sparse_constraint(
                a_rows,
                a_columns,
                a_values,
                b_ub,
                tuple([(x_start + index, -float(value)) for index, value in enumerate(row)] + [(h_index, 1.0), (cdar_z_index, -1.0), (u_index, -1.0)]),
                0.0,
            )

    a_ub = coo_matrix((a_values, (a_rows, a_columns)), shape=(len(b_ub), size), dtype=float).tocsr()
    if _cdar:
        c = [0.0] * size
        c[cdar_z_index] = 1.0
        denominator = float(Decimal("0.20") * Decimal(len(cdar_u_indices)))
        for index in cdar_u_indices:
            c[index] = 1.0 / denominator
    else:
        c = [0.0] * size
        for index, value in enumerate(objective):
            c[x_start + index] = -float(value)
    bounds = [(float(bank), float(bank))]
    bounds.extend((0.0, float(value)) for value in caps)
    for block in h_blocks:
        bounds.extend((0.0, None) for _ in block)
    bounds.append((0.0, None))  # LE
    if loss_z_index is not None:
        bounds.append((None, None))
        bounds.extend((0.0, None) for _ in boundary_indices)
    if hold_z_index is not None:
        bounds.append((None, None))
        bounds.extend((0.0, None) for _ in range(n))
    if cdar_z_index is not None:
        bounds.append((0.0, None))
        bounds.extend((0.0, None) for _ in cdar_u_indices)
    try:
        solver_time_limit = _decimal(time_limit, "time_limit", positive=True)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=OptimizeWarning, message=r"Unrecognized options detected:.*threads")
            result = linprog(
                c,
                A_ub=a_ub,
                b_ub=b_ub,
                bounds=bounds,
                method="highs",
                options={"threads": 1, "time_limit": float(solver_time_limit)},
            )
    except (ArithmeticError, ValueError, TypeError, RuntimeError):
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR")
    solver_status = getattr(result, "status", None)
    try:
        solver_status = None if solver_status is None else int(solver_status)
    except (TypeError, ValueError, OverflowError):
        solver_status = None
    budget_limited = solver_status == 1
    optimal = bool(getattr(result, "success", False)) and not budget_limited
    residuals: dict[str, Decimal] = {}
    for name in ("ineqlin", "lower", "upper"):
        section = getattr(result, name, None)
        values = getattr(section, "residual", None)
        if values is None:
            continue
        try:
            finite_values = [float(value) for value in values if math.isfinite(float(value))]
        except (TypeError, ValueError):
            continue
        if finite_values:
            residuals[name] = Decimal(str(min(finite_values)))
    metadata = {
        "solver_status": solver_status,
        "solver_message": getattr(result, "message", None),
        "residuals": residuals,
        "budget_limited": budget_limited,
        "optimal": optimal,
    }
    if not getattr(result, "success", False) and not budget_limited:
        if solver_status == 2:
            return _SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE", **metadata)
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR", **metadata)
    raw_result = getattr(result, "x", None)
    if raw_result is None or len(raw_result) != size:
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR", **metadata)
    try:
        if any(not math.isfinite(float(value)) for value in raw_result):
            raise ValueError
        raw_bank = Decimal(str(raw_result[bank_index]))
        raw_x = tuple(Decimal(str(raw_result[x_start + index])) for index in range(n))
        raw_h_blocks = tuple(tuple(Decimal(str(raw_result[index])) for index in block) for block in h_blocks)
        raw_le = Decimal(str(raw_result[le_index]))
    except (OverflowError, TypeError, ValueError, ArithmeticError):
        return _SolveOutcome("ERROR", reason="SOLVER_ERROR", **metadata)
    tolerance = _solver_tolerance(bank, raw_bank, caps, raw_x)
    if abs(raw_bank - bank) > tolerance:
        return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID", **metadata)
    if any(value < -tolerance or value > cap + tolerance for value, cap in zip(raw_x, caps)) or raw_le < -tolerance:
        return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID", **metadata)
    path_required_banks = tuple(bank_for_path(_path(path, raw_x), drawdown) for path in paths)
    if any(required > bank + tolerance for required in path_required_banks):
        return _SolveOutcome("ERROR", reason="BANK_UNAVAILABLE", **metadata)
    for cumulative, h_values in zip(cumulative_paths, raw_h_blocks):
        previous = Decimal(0)
        for row, h_value in zip(cumulative, h_values):
            gain = _sum_products(row, raw_x)
            if h_value < max(gain, previous) - tolerance or gain < one_minus_m * h_value - drawdown * bank - tolerance:
                return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID", **metadata)
            previous = h_value
    full_loss = Decimal("0.015") * sum((raw_x[index] for index in full_indices), Decimal(0))
    boundary_loss = Decimal(0)
    if boundary_count:
        boundary_loss = Decimal("0.015") * sum(sorted((raw_x[index] for index in boundary_indices), reverse=True)[:boundary_count], Decimal(0))
    loss = full_loss + boundary_loss
    initial_im = _sum_products(margin_a_values, raw_x)
    initial_mm = _sum_products(margin_b_values, raw_x)
    held = initial_im
    if status == "CONFIRMED":
        held = sum(sorted((margin_a_values[index] * raw_x[index] for index in range(n)), reverse=True)[:ell], Decimal(0))
    available = one_minus_m * bank - loss
    if available <= 0 or initial_im > available + tolerance or initial_mm > mm_load * available + tolerance or held > one_minus_r * available + tolerance:
        return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID", **metadata)
    p30 = _sum_products(objective, raw_x)
    floor_p30 = _sum_products(floor_coefficients, raw_x)
    if floor is not None and floor_p30 < floor - tolerance:
        return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID", **metadata)
    return _SolveOutcome("PASS", _Solution(bank, raw_x), **metadata)


_solve_additional_lp_core = _solve_additional_lp


def _solve_cdar80_lp(*args: Any, **kwargs: Any) -> _SolveOutcome:
    kwargs = dict(kwargs)
    kwargs["_cdar"] = True
    return _solve_additional_lp_core(*args, **kwargs)


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
    replay_x = dict(zip(source_strategy_ids, x))
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
                x=replay_x,
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


def _ordered_margin_bounds(
    value: Sequence[Any] | Mapping[Any, Any] | MarginCoefficientResult,
    strategy_ids: Sequence[Any],
) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    if isinstance(value, MarginCoefficientResult):
        records = {item.strategy_id: item for item in value.coefficients}
        try:
            ordered = tuple(records[strategy_id] for strategy_id in strategy_ids)
        except KeyError as error:
            raise ValueError("MARGIN_BOUND_UNAVAILABLE") from error
    elif isinstance(value, Mapping):
        try:
            ordered = tuple(value[strategy_id] for strategy_id in strategy_ids)
        except KeyError as error:
            raise ValueError("MARGIN_BOUND_UNAVAILABLE") from error
    else:
        records = tuple(value)
        if len(records) != len(strategy_ids):
            raise ValueError("MARGIN_COEFFICIENT_SHAPE_MISMATCH")
        keyed = {
            _member_value(item, "strategy_id", default=_UNSET): item
            for item in records
            if _member_value(item, "strategy_id", default=_UNSET) is not _UNSET
        }
        if keyed:
            if len(keyed) != len(records) or any(strategy_id not in keyed for strategy_id in strategy_ids):
                raise ValueError("MARGIN_BOUND_UNAVAILABLE")
            ordered = tuple(keyed[strategy_id] for strategy_id in strategy_ids)
        else:
            ordered = records

    def field(item: Any, name: str) -> Any:
        if isinstance(item, MarginCoefficient):
            return getattr(item, name)
        if isinstance(item, Mapping) and name in item:
            return item[name]
        raise ValueError("MARGIN_BOUND_UNAVAILABLE")

    return (
        tuple(_decimal(field(item, "a"), "margin_a", nonnegative=True) for item in ordered),
        tuple(_decimal(field(item, "b"), "margin_b", nonnegative=True) for item in ordered),
    )


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
    B_risk: Decimal | None = None,
    bootstrap_summary: Mapping[str, Any] | None = None,
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
        B_risk=B_risk,
        bootstrap_summary=bootstrap_summary,
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
    B_risk: Decimal | None = None,
    bootstrap_summary: Mapping[str, Any] | None = None,
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
    risk_bank = max(Decimal(1), evaluated["bank_for_path"], B_risk or Decimal(0))
    required_bank = max(Decimal(1), solution.bank, risk_bank)
    high_water_mark = Decimal(0)
    money_drawdowns: list[Decimal] = []
    with localcontext() as context:
        context.prec = _precision_for(evaluated["path"])
        for gain in evaluated["path"]:
            high_water_mark = max(high_water_mark, gain)
            money_drawdowns.append(max(Decimal(0), high_water_mark - gain))
    metrics = {
        "mode": WEIGHTED_V1,
        "bank_for_path_usdt": evaluated["bank_for_path"],
        "historical_bank_usdt": evaluated["bank_for_path"],
        "required_bank_usdt": required_bank,
        "B_risk_usdt": B_risk,
        "p30_common_usdt_30d": evaluated["p30_common"],
        "max_drawdown_fraction": evaluated["max_drawdown_fraction"],
        "max_drawdown_pct": evaluated["max_drawdown_pct"],
        "cdar_peak80_usdt": _cdar80_money(money_drawdowns),
        "cdar_peak90_usdt": _cdar90_money(money_drawdowns),
        "max_dd": max_dd,
        "common_days": common_days,
        "target_p30_usdt_30d": target,
        "limiter_L": 0,
    }
    if bootstrap_summary is not None:
        metrics.update({
            "bootstrap_p95_banks_usdt": bootstrap_summary.get("p95_banks"),
            "bootstrap_block_days": bootstrap_summary.get("block_days"),
            "bootstrap_diagnostics": bootstrap_summary.get("diagnostics", ()),
            "bootstrap_witnesses": bootstrap_summary.get("witnesses", ()),
        })
    if margin_coefficients is not None:
        options = dict(margin_kwargs or {})
        options.setdefault("max_dd", max_dd)
        options.setdefault("common_days", common_days)
        options.setdefault("common_p30", evaluated["p30_common"])
        path_risk = risk_bank
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
                        B_risk=B_risk,
                        bootstrap_summary=bootstrap_summary,
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
                                B_risk=B_risk,
                                bootstrap_summary=bootstrap_summary,
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
    if bank_available is not None and required_bank > bank_available:
        if failure_reason is not None:
            failure_reason.append("BANK_UNAVAILABLE")
        return ()
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


def _candidate_vector_by_strategy_id(
    candidate_members: Sequence[Mapping[str, Any]],
    strategy_ids: Sequence[Any],
    capacities: Sequence[Decimal],
) -> tuple[tuple[Decimal, ...], tuple[int | None, ...]]:
    """Normalize compact/reordered candidate members to the prepared ID order."""
    ids = tuple(strategy_ids)
    caps = tuple(capacities)
    if len(ids) != len(caps) or len(set(ids)) != len(ids):
        raise ValueError("CANDIDATE_STRATEGY_SHAPE_INVALID")
    if not candidate_members:
        raise ValueError("CANDIDATE_MEMBERS_EMPTY")
    positions = {strategy_id: index for index, strategy_id in enumerate(ids)}
    values = [Decimal(0)] * len(ids)
    priorities: list[int | None] = [None] * len(ids)
    seen: set[Any] = set()
    for member in candidate_members:
        strategy_id = member.get("strategy_id", _UNSET)
        if strategy_id is _UNSET or strategy_id is None:
            raise ValueError("CANDIDATE_STRATEGY_ID_MISSING")
        if strategy_id not in positions:
            raise ValueError("CANDIDATE_STRATEGY_ID_UNKNOWN")
        if strategy_id in seen:
            raise ValueError("CANDIDATE_STRATEGY_ID_DUPLICATE")
        seen.add(strategy_id)
        index = positions[strategy_id]
        value = _decimal(member.get("x_usdt"), "candidate_x", nonnegative=True)
        if value > caps[index] + _solver_tolerance(value, caps[index]):
            raise ValueError("CANDIDATE_X_EXCEEDS_CAPACITY")
        if "capacity_usdt" in member and member["capacity_usdt"] is not None:
            member_cap = _decimal(member["capacity_usdt"], "candidate_capacity", positive=True)
            if abs(member_cap - caps[index]) > _solver_tolerance(member_cap, caps[index]):
                raise ValueError("CANDIDATE_CAPACITY_MISMATCH")
        values[index] = value
        if "priority" in member and member["priority"] is not None:
            priority = member["priority"]
            if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 5:
                raise ValueError("CANDIDATE_PRIORITY_INVALID")
            priorities[index] = priority
    return tuple(values), tuple(priorities)


def _compact_additional_inputs(
    x: tuple[Decimal, ...],
    normalized_delta: tuple[tuple[Decimal, ...], ...],
    source_members: tuple[Mapping[str, Any], ...],
    capacities: tuple[Decimal, ...],
    margin_coefficients: Sequence[Any] | Mapping[Any, Any] | MarginCoefficientResult,
    margin_kwargs: Mapping[str, Any],
    *,
    strategy_ids: tuple[Any, ...],
    limiter: int,
    common_days: Decimal,
    max_dd: Decimal,
) -> tuple[tuple[tuple[Decimal, ...], ...], tuple[Decimal, ...], tuple[Mapping[str, Any], ...], tuple[Decimal, ...], Any, dict[str, Any]]:
    active_indices = tuple(index for index, value in enumerate(x) if value > 0)
    if not active_indices:
        raise ValueError("NO_ACTIVE_MEMBER")
    active_ids = tuple(strategy_ids[index] for index in active_indices)
    active_x = tuple(x[index] for index in active_indices)
    active_rows = tuple(tuple(row[index] for index in active_indices) for row in normalized_delta)
    active_members = tuple(source_members[index] for index in active_indices)
    active_caps = tuple(capacities[index] for index in active_indices)
    if isinstance(margin_coefficients, MarginCoefficientResult):
        active_coefficients = tuple(item for item in margin_coefficients.coefficients if item.strategy_id in active_ids)
    elif isinstance(margin_coefficients, Mapping):
        active_coefficients = {strategy_id: margin_coefficients[strategy_id] for strategy_id in active_ids}
    else:
        records = tuple(margin_coefficients)
        keyed = {
            _member_value(item, "strategy_id", default=_UNSET): item
            for item in records
            if _member_value(item, "strategy_id", default=_UNSET) is not _UNSET
        }
        active_coefficients = (
            tuple(keyed[strategy_id] for strategy_id in active_ids)
            if keyed
            else tuple(records[index] for index in active_indices) if len(records) == len(strategy_ids) else records
        )
    priorities = derive_priorities(active_members, active_x)
    options = dict(margin_kwargs)
    cycles = options.pop("cycles", options.pop("attributed_cycles", None))
    if cycles is None:
        cycles = tuple(
            cycle
            for member in active_members
            for cycle in _sequence(_member_value(member, "cycles", "position_cycles", default=None))
        )
    else:
        cycles = tuple(cycles)
    options.update({
        "L": limiter,
        "priorities": tuple(priorities.get(strategy_id) for strategy_id in active_ids),
        "strategy_ids": active_ids,
        "cycles": tuple(cycle for cycle in cycles if _member_value(cycle, "strategy_id", default=None) in active_ids),
        "max_dd": max_dd,
        "common_days": common_days,
    })
    return active_rows, active_x, active_members, active_caps, active_coefficients, options


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


def _select_cdar_families(
    sources: Sequence[tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]],
) -> tuple[tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]], ...]:
    """Choose the highest-target family and one most composition-distant family."""

    def target_value(source: tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]) -> Decimal | None:
        value = source[1]
        if value is None:
            # None marks the maximize-upper solution; realized P30 is its available target.
            value = source[0].metrics.get("p30_common_usdt_30d")
        try:
            return None if value is None else _decimal(value, "base_target")
        except (TypeError, ValueError, ArithmeticError):
            return None

    exact_groups: dict[str, list[tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]]] = {}
    for source in sources:
        digest = _digest({"x": source[2].x})
        exact_groups.setdefault(digest, []).append(source)

    exact_families: dict[str, tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]] = {}
    for digest, group in exact_groups.items():
        valid = tuple((target_value(source), source) for source in group if target_value(source) is not None)
        if valid:
            highest = max(value for value, _source in valid)
            exact_families[digest] = min(
                (source for value, source in valid if value == highest),
                key=lambda source: source[0].identity,
            )
        else:
            exact_families[digest] = min(group, key=lambda source: source[0].identity)

    composition_groups: list[list[tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]]] = []
    for source in exact_families.values():
        source_x = tuple(source[2].x)
        for group in composition_groups:
            reference_x = tuple(group[0][2].x)
            if len(reference_x) != len(source_x):
                continue
            with localcontext() as context:
                context.prec = _precision_for(reference_x, source_x)
                source_total = sum(source_x, Decimal(0))
                reference_total = sum(reference_x, Decimal(0))
                proportional = (
                    source_total == reference_total == 0
                    or (
                        source_total > 0
                        and reference_total > 0
                        and all(
                            source_value * reference_total == reference_value * source_total
                            for source_value, reference_value in zip(source_x, reference_x)
                        )
                    )
                )
            if proportional:
                group.append(source)
                break
        else:
            composition_groups.append([source])

    def representative(group: Sequence[tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]]):
        valid = tuple((target_value(source), source) for source in group if target_value(source) is not None)
        if not valid:
            return min(group, key=lambda source: source[0].identity)
        highest = max(value for value, _source in valid)
        return min((source for value, source in valid if value == highest), key=lambda source: source[0].identity)

    unique = tuple(
        representative(group)
        for group in composition_groups
        if any(target_value(source) is not None for source in group)
    )
    if not unique:
        return ()

    highest = max(target_value(source) for source in unique)
    first = min(
        (source for source in unique if target_value(source) == highest),
        key=lambda source: source[0].identity,
    )
    first_composition = _normalized_composition(first[2].x)
    distant: list[tuple[Decimal, tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]]] = []
    for source in unique:
        composition = _normalized_composition(source[2].x)
        if len(composition) != len(first_composition):
            continue
        distance = sum((abs(left - right) for left, right in zip(first_composition, composition)), Decimal(0))
        if distance > _NORMALIZATION_EPS:
            distant.append((distance, source))
    if not distant:
        return (first,)
    max_distance = max(distance for distance, _source in distant)
    second = min((source for distance, source in distant if distance == max_distance), key=lambda source: source[0].identity)
    return first, second


def _cdar_p30_floor(target: Any | None, baseline: Any) -> Decimal:
    baseline_value = _decimal(baseline, "baseline_p30")
    enabled = Decimal(0) if target is None else max(Decimal(0), _decimal(target, "target_p30"))
    with localcontext() as context:
        context.prec = _precision_for(enabled, baseline_value)
        return max(enabled, Decimal("0.95") * baseline_value)


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


def _screened_selection(
    entries: Sequence[tuple[Decimal | None, _Solution]],
    screening: _BootstrapBankResult,
    limit: int,
) -> tuple[tuple[Decimal | None, _Solution], ...]:
    """Select a bounded base universe while retaining target/family coverage."""
    if len(entries) <= limit:
        return tuple(entries)
    family_count = len(screening.p95_banks[0]) if screening.p95_banks else 0
    target_groups: dict[str, list[int]] = {}
    for index, (target_value, _solution) in enumerate(entries):
        target_groups.setdefault(_digest(target_value), []).append(index)
    target_keys = tuple(sorted(target_groups))
    selected: list[int] = []
    selected_set: set[int] = set()

    def score(index: int, family: int) -> tuple[Any, ...]:
        values = screening.p95_banks[index] if index < len(screening.p95_banks) else ()
        risk = values[family] if family < len(values) and values[family] is not None else Decimal("Infinity")
        return risk, _digest({"x": entries[index][1].x}), index

    # One pass over every family and target gives each dimension a chance before
    # the remaining slots are filled by the lowest observed screening risk.
    for family in range(family_count):
        for target_key in target_keys:
            available = [index for index in target_groups[target_key] if index not in selected_set]
            if not available:
                continue
            index = min(available, key=lambda value: score(value, family))
            selected.append(index)
            selected_set.add(index)
            if len(selected) == limit:
                return tuple(entries[index] for index in selected)
    remaining = [index for index in range(len(entries)) if index not in selected_set]
    remaining.sort(key=lambda index: (
        min((score(index, family)[0] for family in range(family_count)), default=Decimal("Infinity")),
        _digest(entries[index][0]),
        _digest({"x": entries[index][1].x}),
        index,
    ))
    selected.extend(remaining[: max(0, limit - len(selected))])
    return tuple(entries[index] for index in selected)


def _select_shortlist(
    candidates: Sequence[PortfolioCandidate],
    *,
    max_candidates: int,
    bank_available: Decimal | None = None,
    origins: Mapping[str, str] | None = None,
) -> tuple[tuple[PortfolioCandidate, ...], dict[str, Any]]:
    """Apply the deterministic five-step final candidate selection contract."""
    limit = _validate_shortlist_limit(max_candidates)
    available = None if bank_available is None else _decimal(bank_available, "bank_available", positive=True)

    def candidate_content_key(candidate: PortfolioCandidate) -> str:
        return _digest({item.name: getattr(candidate, item.name) for item in fields(candidate)})

    pass_candidates = tuple(candidate for candidate in candidates if candidate.status == PASS)
    identity_groups: dict[str, list[PortfolioCandidate]] = {}
    for candidate in pass_candidates:
        identity_groups.setdefault(candidate.identity, []).append(candidate)
    unique_candidates: list[PortfolioCandidate] = []
    identity_duplicates = 0
    identity_collision_count = 0
    for group in identity_groups.values():
        contents = {candidate_content_key(candidate) for candidate in group}
        if len(contents) != 1:
            identity_collision_count += len(group)
            continue
        identity_duplicates += len(group) - 1
        unique_candidates.append(group[0])
    unique = tuple(sorted(unique_candidates, key=lambda item: (str(item.identity), candidate_content_key(item))))
    pass_filtered_count = len(candidates) - len(pass_candidates)
    all_strategy_ids = tuple(sorted({
        member.get("strategy_id")
        for candidate in unique
        for member in candidate.members
        if member.get("strategy_id", _UNSET) is not _UNSET and member.get("strategy_id") is not None
    }, key=_stable_id_key))

    def metric(candidate: PortfolioCandidate, *names: str) -> Decimal | None:
        for name in names:
            if name not in candidate.metrics or candidate.metrics[name] is None:
                continue
            try:
                return _decimal(candidate.metrics[name], name)
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                return None
        return None

    def limiter_status(candidate: PortfolioCandidate) -> str | None:
        present = False
        for name in ("limiter_p30_status", "p30_limiter_status"):
            if name not in candidate.metrics:
                continue
            present = True
            value = candidate.metrics[name]
            if value is None:
                continue
            if value == "MODEL" or value == "UNKNOWN":
                return value
            return None
        return None if present else UNKNOWN

    def limiter_level(candidate: PortfolioCandidate) -> int | None:
        for name in ("limiter_L", "L"):
            if name not in candidate.metrics:
                continue
            value = candidate.metrics[name]
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value
        return None

    def vector_items(candidate: PortfolioCandidate) -> tuple[tuple[Any, Decimal], ...] | None:
        labels = tuple(member.get("strategy_id", _UNSET) for member in candidate.members)
        labeled = tuple(label is not _UNSET for label in labels)
        if all_strategy_ids and not any(labeled):
            return None
        if any(labeled) and not all(labeled):
            return None
        if all(labeled) and (any(label is None for label in labels) or len(set(labels)) != len(labels)):
            return None
        by_id = {member.get("strategy_id"): member for member in candidate.members if member.get("strategy_id", _UNSET) is not _UNSET}
        member_values = (
            ((strategy_id, Decimal(0) if strategy_id not in by_id else by_id[strategy_id].get("x_usdt", _UNSET)) for strategy_id in all_strategy_ids)
            if by_id and all_strategy_ids
            else ((index, member.get("x_usdt", _UNSET)) for index, member in enumerate(candidate.members))
        )
        values = []
        for strategy_id, value in member_values:
            if value is _UNSET:
                return None
            try:
                value = _decimal(value, "candidate_x", nonnegative=True)
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                return None
            values.append((strategy_id, value))
        return tuple(sorted(values, key=lambda item: _stable_id_key(item[0])))

    def x_key(candidate: PortfolioCandidate) -> str:
        items = vector_items(candidate)
        if items is None:
            return _digest({"members": tuple(candidate.members)})
        exact_items = tuple(
            (strategy_id, fraction.numerator, fraction.denominator)
            for strategy_id, value in items
            for fraction in (Fraction(value),)
        )
        return _digest({"x": exact_items})

    def same_family(left: PortfolioCandidate, right: PortfolioCandidate) -> bool:
        left_items, right_items = vector_items(left), vector_items(right)
        if left_items is None or right_items is None or len(left_items) != len(right_items):
            return False
        if tuple(item[0] for item in left_items) != tuple(item[0] for item in right_items):
            return False
        left_values = tuple(value for _strategy_id, value in left_items)
        right_values = tuple(value for _strategy_id, value in right_items)
        left_exact = tuple(Fraction(value) for value in left_values)
        right_exact = tuple(Fraction(value) for value in right_values)
        left_total = sum(left_exact, Fraction(0))
        right_total = sum(right_exact, Fraction(0))
        return (
            left_total == right_total == 0
            or (
                left_total > 0
                and right_total > 0
                and all(
                    left_value * right_total == right_value * left_total
                    for left_value, right_value in zip(left_exact, right_exact)
                )
            )
        )

    def rank(candidate: PortfolioCandidate) -> tuple[Any, ...]:
        p30 = metric(candidate, "p30_common_usdt_30d", "p30_common")
        cdar = metric(candidate, "cdar_peak80_usdt", "cdar_peak80")
        bank = metric(candidate, "required_bank_usdt", "B_required_usdt", "B_required_margin_usdt")
        return (
            0 if p30 is not None else 1,
            -p30 if p30 is not None else "UNKNOWN",
            0 if cdar is not None else 1,
            cdar if cdar is not None else "UNKNOWN",
            0 if bank is not None else 1,
            bank if bank is not None else "UNKNOWN",
            str(candidate.identity),
        )

    def origin(candidate: PortfolioCandidate) -> str:
        value = None if origins is None else origins.get(candidate.identity)
        if value is None:
            value = candidate.metrics.get("origin", candidate.metrics.get("stage"))
        if value is None:
            return "scale"
        value = str(value).lower()
        if value in {"base", "scale"}:
            return "scale"
        if value in {"cdar", "cdar_alternative"}:
            return "cdar"
        if value in {"limiter", "neighboring_l", "neighbor_l", "control"}:
            return "limiter"
        raise ValueError("SHORTLIST_ORIGIN_UNKNOWN")

    selected: list[PortfolioCandidate] = []
    selected_ids: set[str] = set()
    selected_origin: dict[str, str] = {}
    selected_step: dict[str, str] = {}
    selected_l: dict[str, set[Any]] = {}
    steps = {"identity_dedupe": identity_duplicates, "primary": 0, "controls": 0, "families": 0, "targets": 0, "round_robin": 0}
    valid_vector_level = tuple(candidate for candidate in unique if vector_items(candidate) is not None and limiter_level(candidate) is not None)
    invalid_vector_or_level_count = len(unique) - len(valid_vector_level)
    valid_metric = tuple(
        candidate
        for candidate in valid_vector_level
        if metric(candidate, "required_bank_usdt", "B_required_usdt", "B_required_margin_usdt") is not None
        and limiter_status(candidate) in {"MODEL", "UNKNOWN"}
        and (
            limiter_status(candidate) != "MODEL"
            or metric(candidate, "p30_limiter_model_usdt_30d", "p30_limiter_model", "limiter_p30_model") is not None
        )
    )
    invalid_metric_count = len(valid_vector_level) - len(valid_metric)
    bank_infeasible_count = 0 if available is None else sum(
        1
        for candidate in valid_metric
        if metric(candidate, "required_bank_usdt", "B_required_usdt", "B_required_margin_usdt") > available
    )
    selection_candidates = tuple(
        candidate
        for candidate in valid_metric
        if available is None
        or metric(candidate, "required_bank_usdt", "B_required_usdt", "B_required_margin_usdt") <= available
    )
    resolved_origins = {candidate.identity: origin(candidate) for candidate in selection_candidates}
    p30_valid = tuple(candidate for candidate in unique if metric(candidate, "p30_common_usdt_30d", "p30_common") is not None)
    valid_p30 = tuple(candidate for candidate in selection_candidates if metric(candidate, "p30_common_usdt_30d", "p30_common") is not None)

    def add(candidate: PortfolioCandidate, step: str) -> bool:
        if candidate.identity in selected_ids:
            return False
        if metric(candidate, "p30_common_usdt_30d", "p30_common") is None:
            return False
        candidate_origin = resolved_origins[candidate.identity]
        vector = x_key(candidate)
        level = limiter_level(candidate)
        levels = selected_l.setdefault(vector, set())
        if level in levels or len(levels) >= 3:
            return False
        selected.append(candidate)
        selected_ids.add(candidate.identity)
        selected_origin[candidate.identity] = candidate_origin
        selected_step[candidate.identity] = step
        levels.add(level)
        steps[step] += 1
        return True

    primary_source = min(valid_p30, key=lambda item: (-metric(item, "p30_common_usdt_30d", "p30_common"), str(item.identity)), default=None)
    primary = None
    control_pool: tuple[PortfolioCandidate, ...] = ()
    if primary_source is not None:
        source_x = x_key(primary_source)
        same_x = tuple(candidate for candidate in selection_candidates if x_key(candidate) == source_x)
        if available is None:
            control_pool = same_x
        else:
            control_pool = tuple(
                candidate
                for candidate in same_x
                if (required_bank := metric(candidate, "required_bank_usdt", "B_required_usdt", "B_required_margin_usdt")) is not None
                and required_bank <= available
            )
        models = tuple(
            candidate for candidate in control_pool
            if limiter_status(candidate) == "MODEL"
            and metric(candidate, "p30_common_usdt_30d", "p30_common") is not None
            and metric(candidate, "p30_limiter_model_usdt_30d", "p30_limiter_model", "limiter_p30_model") is not None
        )
        if models:
            primary = min(
                models,
                key=lambda item: (
                    -metric(item, "p30_limiter_model_usdt_30d", "p30_limiter_model", "limiter_p30_model"),
                    str(item.identity),
                ),
            )
        else:
            unknown = tuple(
                candidate
                for candidate in control_pool
                if limiter_level(candidate) is not None
                and metric(candidate, "p30_common_usdt_30d", "p30_common") is not None
            )
            primary = min(
                unknown,
                key=lambda item: (limiter_level(item) != 0, -(limiter_level(item) or 0), str(item.identity)),
                default=primary_source,
            )
        if add(primary, "primary"):
            control_candidates: list[PortfolioCandidate] = []
            primary_level = limiter_level(primary)
            off = [candidate for candidate in control_pool if limiter_level(candidate) == 0 and candidate.identity != primary.identity]
            control_candidates.extend(sorted(off, key=rank))
            minimum_bank = None
            if available is None:
                minimum_bank = min(
                    (candidate for candidate in same_x if metric(candidate, "required_bank_usdt", "B_required_usdt", "B_required_margin_usdt") is not None),
                    key=lambda item: (metric(item, "required_bank_usdt", "B_required_usdt", "B_required_margin_usdt"), str(item.identity)),
                    default=None,
                )
                if minimum_bank is not None:
                    control_candidates.append(minimum_bank)
            model_controls = [
                candidate for candidate in models
                if candidate.identity != primary.identity and limiter_level(candidate) != primary_level
            ]
            control_candidates.extend(sorted(model_controls, key=lambda item: (
                -metric(item, "p30_limiter_model_usdt_30d", "p30_limiter_model", "limiter_p30_model"), str(item.identity)
            )))
            if not models and primary_level is not None:
                lower_unknown = [
                    candidate for candidate in control_pool
                    if candidate.identity != primary.identity
                    and limiter_level(candidate) is not None
                    and (primary_level == 0 or limiter_level(candidate) < primary_level)
                ]
                control_candidates.extend(sorted(lower_unknown, key=lambda item: (-(limiter_level(item) or 0), str(item.identity))))
            for candidate in control_candidates:
                if len(selected) >= min(limit, 3):
                    break
                add(candidate, "controls")

    # ponytail: O(n^2) exact ray grouping; hash rational vectors if shortlist universes grow materially.
    family_groups: list[list[PortfolioCandidate]] = []
    for candidate in selection_candidates:
        if candidate.identity in selected_ids:
            continue
        for group in family_groups:
            if same_family(candidate, group[0]):
                group.append(candidate)
                break
        else:
            family_groups.append([candidate])
    representatives = sorted(
        (
            min(group, key=rank)
            for group in family_groups
            if not any(same_family(group[0], selected_candidate) for selected_candidate in selected)
        ),
        key=lambda candidate: (
            0 if metric(candidate, "target_p30_usdt_30d", "target_p30") is not None else 1,
            metric(candidate, "target_p30_usdt_30d", "target_p30") if metric(candidate, "target_p30_usdt_30d", "target_p30") is not None else "UNKNOWN",
            rank(candidate),
        ),
    )
    for representative in representatives:
        if len(selected) >= limit:
            break
        if add(representative, "families"):
            continue
    # Lower and middle targets get the remaining non-family slots before upper points.
    remaining = [candidate for candidate in selection_candidates if candidate.identity not in selected_ids]
    target_values = sorted({value for candidate in selection_candidates if (value := metric(candidate, "target_p30_usdt_30d", "target_p30")) is not None})
    lower_targets = set(target_values[:-1]) if target_values else set()
    if len(target_values) > 1:
        for candidate in sorted(remaining, key=lambda item: (0 if metric(item, "target_p30_usdt_30d", "target_p30") in lower_targets else 1, rank(item))):
            if len(selected) >= limit:
                break
            if metric(candidate, "target_p30_usdt_30d", "target_p30") in lower_targets:
                add(candidate, "targets")

    # Empty origin categories intentionally consume no quota; a turn is still attempted.
    remaining = [candidate for candidate in selection_candidates if candidate.identity not in selected_ids]
    intervals = sorted({metric(candidate, "target_p30_usdt_30d", "target_p30") for candidate in remaining}, key=lambda item: (item is None, item))
    while len(selected) < limit and remaining:
        made = False
        for interval in intervals:
            for category in ("scale", "cdar", "limiter"):
                if len(selected) >= limit:
                    break
                pool = [
                    candidate for candidate in remaining
                    if resolved_origins[candidate.identity] == category and metric(candidate, "target_p30_usdt_30d", "target_p30") == interval
                ]
                if not pool:
                    continue
                candidate = min(pool, key=rank)
                remaining.remove(candidate)
                if add(candidate, "round_robin"):
                    made = True
                else:
                    made = True
        if not made:
            break

    manifest = {
        "input_count": len(candidates),
        "pass_filtered_count": pass_filtered_count,
        "deduplicated_count": identity_duplicates,
        "identity_collision_count": identity_collision_count,
        "candidate_count": len(unique),
        "invalid_p30_count": len(unique) - len(p30_valid),
        "bank_infeasible_count": bank_infeasible_count,
        "invalid_vector_or_level_count": invalid_vector_or_level_count,
        "invalid_metric_count": invalid_metric_count,
        "bank_available": available,
        "max_candidates": limit,
        "selection_eligible_count": len(selection_candidates),
        "eligible_unselected_count": len(selection_candidates) - len(selected),
        "selected_count": len(selected),
        "selected_identities": tuple(candidate.identity for candidate in selected),
        "selected_origins": tuple(selected_origin[candidate.identity] for candidate in selected),
        "selected_steps": tuple(selected_step[candidate.identity] for candidate in selected),
        "step_counts": dict(steps),
    }
    return tuple(selected), manifest


def _validate_shortlist_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_candidates must be a positive integer")
    return value


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
    max_candidates: int = 20,
    seed: int = 731,
    bootstrap_scenarios: int = 1000,
    screening_scenarios: int = 100,
    workers: int = 1,
    wall_time: Any = Decimal("900"),
    solver_time: Any = Decimal("30"),
    cancel: Callable[[], bool] | None = None,
) -> SearchResult:
    """Find a bounded weighted frontier and verify its base vectors with bootstrap risk."""
    max_targets = _validate_max_targets(max_targets)
    if type(max_candidates) is not int or not 1 <= max_candidates <= 50:
        raise ValueError("max_candidates must be an integer from 1 to 50")
    if type(bootstrap_scenarios) is not int or bootstrap_scenarios <= 0:
        raise ValueError("bootstrap_scenarios must be a positive integer")
    if type(screening_scenarios) is not int or screening_scenarios <= 0 or screening_scenarios > bootstrap_scenarios:
        raise ValueError("screening_scenarios must be positive and no greater than bootstrap_scenarios")
    if type(workers) is not int or workers <= 0:
        raise ValueError("workers must be a positive integer")
    wall_seconds = _decimal(wall_time, "wall_time", positive=True)
    solver_seconds = _decimal(solver_time, "solver_time", positive=True)
    _bootstrap_seed(seed, 0, 0)
    if cancel is not None and not callable(cancel):
        raise TypeError("cancel must be callable")
    normalized_delta = _validate_input(prepared)
    history_step_minutes = _decimal(prepared.history_step_minutes, "history_step_minutes", positive=True)
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
    margin_a: tuple[Decimal, ...] | None = None
    margin_b: tuple[Decimal, ...] | None = None
    max_mm_load: Decimal | None = None
    if margin_coefficients is not None:
        try:
            margin_a, margin_b = _ordered_margin_bounds(margin_coefficients, prepared.strategy_ids)
            max_mm_load = _decimal((margin_kwargs or {}).get("max_mm_load", Decimal("0.35")), "max_mm_load", positive=True)
            if not max_mm_load < Decimal(1):
                raise ValueError("max_mm_load must be below one")
        except (TypeError, ValueError):
            return _result(FAIL, "MARGIN_BOUND_UNAVAILABLE")
    target = None if target_p30 is None else _decimal(target_p30, "target_p30", positive=True)
    available = None if bank_available is None else _decimal(bank_available, "bank_available", positive=True)
    upper_target = _sum_products(caps, tuple(max(coefficient, Decimal(0)) for coefficient in coefficients))
    if target is None and upper_target <= 0:
        return _result(FAIL, "NO_POSITIVE_TARGET")
    calls = 0
    error_reason: str | None = None
    budget_reason: str | None = None
    solved: dict[Decimal, _Solution] = {}
    discovered: list[tuple[Decimal | None, _Solution]] = []
    solver_calls: list[Mapping[str, Any]] = []
    started_wall = time.perf_counter()

    def budget_check() -> str | None:
        if cancel is not None and cancel():
            return "CANCELLED"
        if time.perf_counter() - started_wall >= float(wall_seconds):
            return "WALL_TIME_LIMIT"
        return None

    def run_target(target_value: Decimal | None, *, maximize: bool = False) -> bool:
        nonlocal calls, error_reason, budget_reason
        reason = budget_check()
        if reason is not None:
            budget_reason = reason
            return False
        if calls >= 20:
            budget_reason = "SOLVER_CALL_LIMIT"
            return False
        calls += 1
        outcome = _solve_lp(
            normalized_delta,
            caps,
            coefficients,
            max_dd=drawdown,
            target=target_value,
            bank_available=available,
            maximize=maximize,
            margin_a=margin_a,
            margin_b=margin_b,
            max_mm_load=max_mm_load,
            time_limit=solver_seconds,
        )
        solver_calls.append({
            "call": calls,
            "target": target_value,
            "maximize": maximize,
            "status": outcome.status,
            "residuals": dict(outcome.residuals or {}),
            "budget_limited": outcome.budget_limited,
        })
        if outcome.budget_limited and budget_reason is None:
            budget_reason = "SOLVER_TIME_LIMIT"
        if outcome.status == "ERROR":
            error_reason = outcome.reason or "SOLVER_ERROR"
            return False
        if outcome.status != "PASS" or outcome.solution is None:
            return False
        solution = outcome.solution
        discovered.append((target_value, solution))
        if target_value is not None:
            solved[target_value] = solution
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
            if error_reason is not None or budget_reason is not None:
                break
        blocked: set[tuple[Decimal, Decimal]] = set()
        while error_reason is None and budget_reason is None and calls < max_targets:
            ordered = sorted(solved.items(), key=lambda item: item[0])
            intervals: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
            for (left_target, left_solution), (right_target, right_solution) in zip(ordered, ordered[1:]):
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
                if error_reason is None and budget_reason is None:
                    blocked.add((left_target, right_target))
    unique_entries: list[tuple[Decimal | None, _Solution]] = []
    seen_x: set[str] = set()
    for target_value, solution in discovered:
        digest = _digest({"x": solution.x})
        if digest not in seen_x:
            seen_x.add(digest)
            unique_entries.append((target_value, solution))
    screening_result: _BootstrapBankResult | None = None
    bootstrap_result: _BootstrapBankResult | None = None
    selected_entries = unique_entries

    def remaining_wall() -> Decimal | None:
        remaining = float(wall_seconds) - (time.perf_counter() - started_wall)
        return None if remaining <= 0 else Decimal(str(remaining))

    # A technical solver error is terminal. Completed solutions remain in the
    # manifest, but must not be presented as a verified frontier.
    if discovered and error_reason is None and budget_reason in (None, "SOLVER_TIME_LIMIT"):
        reason = budget_check()
        if reason is not None:
            budget_reason = reason
        elif len(unique_entries) > 2 * max_candidates:
            remaining = remaining_wall()
            if remaining is None:
                budget_reason = "WALL_TIME_LIMIT"
            else:
                screening_result = bootstrap_banks(
                    normalized_delta,
                    tuple(solution.x for _target_value, solution in unique_entries),
                    max_dd=drawdown,
                    common_days=days,
                    seed=seed,
                    history_step_minutes=history_step_minutes,
                    scenarios_per_family=screening_scenarios,
                    workers=workers,
                    cancel=cancel,
                    wall_time_limit_seconds=remaining,
                )
                if not screening_result.complete:
                    budget_reason = screening_result.manifest.get("stopping_reason") or "BOOTSTRAP_INCOMPLETE"
                else:
                    selected_entries = list(_screened_selection(unique_entries, screening_result, 2 * max_candidates))
        if budget_reason in (None, "SOLVER_TIME_LIMIT"):
            reason = budget_check()
            if reason is not None:
                budget_reason = reason
            else:
                remaining = remaining_wall()
                if remaining is None:
                    budget_reason = "WALL_TIME_LIMIT"
                else:
                    bootstrap_result = bootstrap_banks(
                        normalized_delta,
                        tuple(solution.x for _target_value, solution in selected_entries),
                        max_dd=drawdown,
                        common_days=days,
                        seed=seed,
                        history_step_minutes=history_step_minutes,
                        scenarios_per_family=bootstrap_scenarios,
                        workers=workers,
                        cancel=cancel,
                        wall_time_limit_seconds=remaining,
                        prefix_result=screening_result,
                    )
                    if not bootstrap_result.complete:
                        budget_reason = bootstrap_result.manifest.get("stopping_reason") or "BOOTSTRAP_INCOMPLETE"
                    else:
                        reason = budget_check()
                        if reason is not None:
                            budget_reason = reason

    risk_by_digest: dict[str, tuple[Decimal, Mapping[str, Any]]] = {}
    if bootstrap_result is not None and bootstrap_result.complete:
        for index, digest in enumerate(bootstrap_result.vector_digests):
            family_manifest = bootstrap_result.manifest["families"]
            risk_by_digest[digest] = (
                bootstrap_result.risk_banks[index],
                {
                    "p95_banks": bootstrap_result.p95_banks[index],
                    "block_days": bootstrap_result.manifest["block_days"],
                    "diagnostics": bootstrap_result.manifest["diagnostics"],
                    "witnesses": bootstrap_result.p95_witnesses[index],
                    "parameters": {
                        "seed": bootstrap_result.manifest["seed"],
                        "history_step_minutes": bootstrap_result.manifest["history_step_minutes"],
                        "numpy_version": bootstrap_result.manifest["numpy_version"],
                    },
                },
            )
    ordered_candidates: list[PortfolioCandidate] = []
    candidate_identities: set[str] = set()
    candidate_origins: dict[str, str] = {}
    candidate_sources: list[tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]] = []
    conversion_failure: str | None = None

    def candidate_x_digest(candidate: PortfolioCandidate) -> str:
        values = tuple(
            sorted(
                ((member.get("strategy_id", index), member.get("x_usdt")) for index, member in enumerate(candidate.members)),
                key=lambda item: _stable_id_key(item[0]),
            )
        )
        return _digest({"x": values})

    if bootstrap_result is not None and bootstrap_result.complete:
        selected_digests = set(risk_by_digest)
        for target_value, solution in unique_entries:
            digest = _digest({"x": solution.x})
            if digest not in selected_digests:
                continue
            risk, summary = risk_by_digest[digest]
            margin_failure: list[str] = []
            converted = _candidates_for_solution(
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
                B_risk=risk,
                bootstrap_summary=summary,
                allow_rescue=False,
                failure_reason=margin_failure,
            )
            if not converted:
                conversion_failure = margin_failure[-1] if margin_failure else (
                    "MARGIN_BOUND_FAILED" if margin_coefficients is not None else "LP_SOLUTION_INVALID"
                )
                if margin_coefficients is not None:
                    domain_reason = _margin_domain_reason(margin_coefficients, prepared.strategy_ids, solution.x)
                    if domain_reason is not None:
                        conversion_failure = domain_reason
                continue
            primary_x_digest = candidate_x_digest(converted[0])
            for ordinal, candidate in enumerate(converted[:3]):
                if candidate.identity not in candidate_identities:
                    candidate_identities.add(candidate.identity)
                    ordered_candidates.append(candidate)
                    candidate_origins[candidate.identity] = "base" if ordinal == 0 or candidate_x_digest(candidate) != primary_x_digest else "limiter"
                    candidate_sources.append((candidate, target_value, solution, summary))

    additional_manifest: dict[str, Any] = {
        "attempted": False,
        "additional_pass_count": 0,
        "outcome": "skipped",
        "reason": "NOT_RUN",
        "solver_status": None,
        "solver_message": None,
        "residuals": {},
        "new_x_count": 0,
        "p30_common_skipped_count": 0,
        "p30_common_skip_reason": None,
        "eligibility_skip_reasons": {},
    }
    additional_bootstrap_result: _BootstrapBankResult | None = None
    base_x_digests = {_digest({"x": solution.x}) for _target_value, solution in unique_entries}
    new_x_digests: set[str] = set()
    if margin_coefficients is None:
        additional_manifest["reason"] = "MARGIN_COEFFICIENTS_UNAVAILABLE"
    elif budget_reason is not None or error_reason is not None:
        additional_manifest["reason"] = budget_reason or error_reason or "BASE_INCOMPLETE"
    elif not candidate_sources:
        additional_manifest["reason"] = "NO_ELIGIBLE_MARGIN_CANDIDATE"
    else:
        primary_by_x: dict[str, tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]] = {}
        for source in candidate_sources:
            primary_by_x.setdefault(_digest({"x": source[2].x}), source)
        eligible_sources: list[tuple[PortfolioCandidate, Decimal | None, _Solution, Mapping[str, Any]]] = []
        p30_invalid_count = 0
        eligibility_skip_reasons: dict[str, int] = {}

        def record_skip(reason: str) -> None:
            eligibility_skip_reasons[reason] = eligibility_skip_reasons.get(reason, 0) + 1

        for candidate, target_value, solution, summary in primary_by_x.values():
            metrics = candidate.metrics
            limiter = metrics.get("limiter_L")
            if type(limiter) is not int or limiter < 0:
                record_skip("LIMITER_INVALID")
                continue
            if metrics.get("B_margin_usdt") is None or metrics.get("B_required_margin_usdt") is None:
                record_skip("MARGIN_METRIC_UNAVAILABLE")
                continue
            if metrics.get("B_risk_usdt") is None:
                record_skip("B_RISK_UNAVAILABLE")
                continue
            try:
                margin_bank = _decimal(metrics["B_margin_usdt"], "B_margin", nonnegative=True)
                risk_bank = _decimal(metrics["B_risk_usdt"], "B_risk", nonnegative=True)
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                record_skip("MARGIN_RISK_INVALID")
                continue
            try:
                margin_limited = margin_bank >= risk_bank - _solver_tolerance(margin_bank, risk_bank)
                headroom_dd = _decimal(metrics.get("max_drawdown_fraction"), "max_drawdown_fraction") < drawdown
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                record_skip("MAX_DRAWDOWN_INVALID")
                continue
            try:
                candidate_x, _candidate_priorities = _candidate_vector_by_strategy_id(candidate.members, prepared.strategy_ids, caps)
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                record_skip("CANDIDATE_IDENTITY_INVALID")
                continue
            try:
                solution_x = tuple(_decimal(value, "solution_x", nonnegative=True) for value in solution.x)
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                record_skip("SOLUTION_X_INVALID")
                continue
            if len(solution_x) != n or any(
                abs(candidate_value - solution_value) > _solver_tolerance(candidate_value, solution_value)
                for candidate_value, solution_value in zip(candidate_x, solution_x)
            ):
                record_skip("CANDIDATE_SOLUTION_MISMATCH")
                continue
            try:
                _decimal(metrics.get("p30_common_usdt_30d"), "p30_common")
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                p30_invalid_count += 1
                record_skip("P30_COMMON_INVALID")
                continue
            if not margin_limited:
                record_skip("NOT_MARGIN_LIMITED")
                continue
            if not headroom_dd:
                record_skip("DRAWNDOWN_HEADROOM_UNAVAILABLE")
                continue
            if not any(value < cap for value, cap in zip(candidate_x, caps)):
                record_skip("NO_CAPACITY_HEADROOM")
                continue
            eligible_sources.append((candidate, target_value, solution, summary))
        additional_manifest["eligibility_skip_reasons"] = dict(eligibility_skip_reasons)
        if not eligible_sources:
            additional_manifest["p30_common_skipped_count"] = p30_invalid_count
            additional_manifest["p30_common_skip_reason"] = "P30_COMMON_INVALID" if p30_invalid_count else None
            additional_manifest["reason"] = "P30_COMMON_UNAVAILABLE" if p30_invalid_count else "NO_ELIGIBLE_MARGIN_CANDIDATE"
        else:
            additional_manifest["p30_common_skipped_count"] = p30_invalid_count
            additional_manifest["p30_common_skip_reason"] = "P30_COMMON_INVALID" if p30_invalid_count else None
            eligible_sources.sort(key=lambda item: (
                -_decimal(item[0].metrics.get("p30_common_usdt_30d"), "p30_common"),
                item[0].identity,
            ))
            seed_candidate, target_value, seed_solution, seed_summary = eligible_sources[0]
            metrics = seed_candidate.metrics
            limiter = int(metrics["limiter_L"])
            try:
                seed_x, supplied_priorities = _candidate_vector_by_strategy_id(
                    seed_candidate.members, prepared.strategy_ids, caps
                )
                derived = derive_priorities(source_members, seed_x)
                frozen_priorities = tuple(
                    supplied if supplied is not None else derived.get(strategy_id)
                    for strategy_id, supplied in zip(prepared.strategy_ids, supplied_priorities)
                )
                if any(value is None for value in frozen_priorities):
                    raise ValueError("PRIORITY_UNKNOWN")
            except (ArithmeticError, KeyError, TypeError, ValueError, InvalidOperation):
                frozen_priorities = ()
            frozen_options = dict(margin_kwargs or {})
            frozen_options.update({
                "L": limiter,
                "priorities": frozen_priorities,
                "strategy_ids": tuple(prepared.strategy_ids),
                "max_dd": drawdown,
                "common_days": days,
            })
            cycles_value = frozen_options.get("cycles", frozen_options.get("attributed_cycles"))
            if cycles_value is None:
                cycles = tuple(
                    cycle
                    for member in source_members
                    for cycle in _sequence(_member_value(member, "cycles", "position_cycles", default=None))
                )
            else:
                cycles = tuple(cycles_value)
            try:
                _primary, frozen_variants = _margin_variant(
                    seed_solution.x,
                    source_members,
                    margin_coefficients,
                    options=frozen_options,
                    bank_available=available,
                    B_risk=metrics.get("B_risk_usdt"),
                )
                frozen_variant = next((item for item in frozen_variants if item.L == limiter), None)
                if frozen_variant is None:
                    raise ValueError("LIMITER_REPLAY_UNAVAILABLE")
                replay = frozen_variant.replay
                objective_status, limiter_coefficients = _limiter_model_coefficients(
                    cycles,
                    prepared.strategy_ids,
                    replay,
                    common_days=days,
                )
            except (ArithmeticError, TypeError, ValueError):
                objective_status, limiter_coefficients = UNKNOWN, None
                replay = None
            if objective_status == "MODEL" and limiter_coefficients:
                objective = limiter_coefficients
            else:
                objective_status = UNKNOWN
                objective = coefficients
            limiter_status = str(metrics.get("limiter_release_status", (margin_kwargs or {}).get("limiter_release_status", UNKNOWN))).upper()
            if limiter_status not in {UNKNOWN, "CONFIRMED"}:
                limiter_status = UNKNOWN
            try:
                fixed_bank = available if available is not None else max(
                    _decimal(metrics.get("required_bank_usdt", seed_solution.bank), "bank_fixed", positive=True),
                    _decimal(seed_solution.bank, "bank_fixed", positive=True),
                )
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                fixed_bank = None
            p95_values = tuple(seed_summary.get("p95_banks") or ())
            witnesses = tuple(seed_summary.get("witnesses") or ())
            witness_index: int | None = None
            if p95_values:
                numeric: list[tuple[Any, int, int]] = []
                for index, value in enumerate(p95_values):
                    if value is None:
                        continue
                    family_ordinal = index
                    if index < len(witnesses) and isinstance(witnesses[index], Mapping):
                        declared = witnesses[index].get("family_ordinal")
                        if type(declared) is int and declared >= 0:
                            family_ordinal = declared
                    numeric.append((value, family_ordinal, index))
                if numeric:
                    best_value = max(item[0] for item in numeric)
                    witness_index = min(
                        (item for item in numeric if item[0] == best_value),
                        key=lambda item: (item[1], item[2]),
                    )[2]
            p95_witness = None
            if witness_index is not None and witness_index < len(witnesses):
                witness = witnesses[witness_index]
                if isinstance(witness, Mapping):
                    p95_witness = {
                        "family_ordinal": witness.get("family_ordinal"),
                        "scenario_index": witness.get("scenario_index"),
                    }
            reason = budget_check()
            if reason is not None:
                budget_reason = reason
                additional_manifest["reason"] = reason
            elif fixed_bank is None:
                additional_manifest["reason"] = "BANK_FIXED_UNAVAILABLE"
            elif not frozen_priorities:
                additional_manifest["reason"] = "PRIORITY_UNKNOWN"
            elif calls >= 20:
                budget_reason = "SOLVER_CALL_LIMIT"
                additional_manifest["reason"] = "SOLVER_CALL_LIMIT"
            else:
                calls += 1
                additional_manifest.update({
                    "attempted": True,
                    "additional_pass_count": 1,
                    "reason": None,
                    "objective_status": objective_status,
                    "limiter_release_status": limiter_status,
                    "L": limiter,
                    "bank_fixed": fixed_bank,
                    "p95_witness": p95_witness,
                    "replay_accepted_count": 0 if replay is None else sum(replay.accepted_mask),
                    "replay_total_count": 0 if replay is None else len(replay.accepted_mask),
                })
                try:
                    outcome = _solve_additional_lp(
                        normalized_delta,
                        caps,
                        bank_fixed=fixed_bank,
                        max_dd=drawdown,
                        objective_coefficients=objective,
                        L=limiter,
                        priorities=frozen_priorities,
                        margin_a=margin_a,
                        margin_b=margin_b,
                        max_mm_load=max_mm_load,
                        reserve=(margin_kwargs or {}).get("reserve", Decimal("0.40")),
                        limiter_release_status=limiter_status,
                        p95_witness=p95_witness,
                        bootstrap_manifest=None if p95_witness is None else bootstrap_result.manifest,
                        time_limit=solver_seconds,
                        p30_floor=target_value,
                        p30_floor_coefficients=coefficients,
                    )
                except (ArithmeticError, TypeError, ValueError, RuntimeError):
                    outcome = _SolveOutcome("ERROR", reason="SOLVER_ERROR")
                additional_manifest.update({
                    "status": outcome.status,
                    "solver_status": outcome.solver_status,
                    "solver_message": outcome.solver_message,
                    "residuals": dict(outcome.residuals or {}),
                    "budget_limited": outcome.budget_limited,
                })
                solver_calls.append({
                    "call": calls,
                    "stage": "additional",
                    "target": target_value,
                    "status": outcome.status,
                    "solver_status": outcome.solver_status,
                    "residuals": dict(outcome.residuals or {}),
                    "budget_limited": outcome.budget_limited,
                })
                if outcome.budget_limited:
                    budget_reason = "SOLVER_TIME_LIMIT"
                    additional_manifest.update({"outcome": "rejected", "reason": budget_reason})
                elif outcome.status != "PASS" or outcome.solution is None:
                    additional_manifest.update({
                        "outcome": "rejected",
                        "reason": outcome.reason or "LP_INFEASIBLE",
                    })
                else:
                    proposed_solution = outcome.solution
                    proposed_digest = _digest({"x": proposed_solution.x})
                    if proposed_digest in base_x_digests or proposed_digest in new_x_digests:
                        additional_manifest.update({"outcome": "rejected", "reason": "DUPLICATE_X"})
                    elif len(new_x_digests) >= max_candidates:
                        budget_reason = "NEW_X_LIMIT"
                        additional_manifest.update({"outcome": "rejected", "reason": budget_reason})
                    else:
                        remaining = remaining_wall()
                        if remaining is None:
                            budget_reason = "WALL_TIME_LIMIT"
                            additional_manifest.update({"outcome": "rejected", "reason": budget_reason})
                        elif cancel is not None and cancel():
                            budget_reason = "CANCELLED"
                            additional_manifest.update({"outcome": "rejected", "reason": budget_reason})
                        else:
                            try:
                                additional_bootstrap_result = bootstrap_banks(
                                    normalized_delta,
                                    (proposed_solution.x,),
                                    max_dd=drawdown,
                                    common_days=days,
                                    seed=seed,
                                    history_step_minutes=history_step_minutes,
                                    scenarios_per_family=bootstrap_scenarios,
                                    workers=workers,
                                    cancel=cancel,
                                    wall_time_limit_seconds=remaining,
                                    prefix_result=None,
                                )
                            except (ArithmeticError, TypeError, ValueError, RuntimeError):
                                additional_bootstrap_result = None
                                additional_manifest.update({"outcome": "rejected", "reason": "BOOTSTRAP_ERROR"})
                            if additional_bootstrap_result is None:
                                pass
                            elif not additional_bootstrap_result.complete:
                                budget_reason = additional_bootstrap_result.manifest.get("stopping_reason") or "BOOTSTRAP_INCOMPLETE"
                                additional_manifest.update({"outcome": "rejected", "reason": budget_reason})
                            else:
                                new_x_digests.add(proposed_digest)
                                additional_manifest["new_x_count"] = len(new_x_digests)
                                additional_risk = additional_bootstrap_result.risk_banks[0]
                                additional_summary = {
                                    "p95_banks": additional_bootstrap_result.p95_banks[0],
                                    "block_days": additional_bootstrap_result.manifest["block_days"],
                                    "diagnostics": additional_bootstrap_result.manifest["diagnostics"],
                                    "witnesses": additional_bootstrap_result.p95_witnesses[0],
                                    "parameters": {
                                        "seed": additional_bootstrap_result.manifest["seed"],
                                        "history_step_minutes": additional_bootstrap_result.manifest["history_step_minutes"],
                                        "numpy_version": additional_bootstrap_result.manifest["numpy_version"],
                                    },
                                }
                                try:
                                    (
                                        active_rows,
                                        active_x,
                                        active_members,
                                        active_caps,
                                        active_coefficients,
                                        active_margin_kwargs,
                                    ) = _compact_additional_inputs(
                                        proposed_solution.x,
                                        normalized_delta,
                                        source_members,
                                        caps,
                                        margin_coefficients,
                                        margin_kwargs or {},
                                        strategy_ids=tuple(prepared.strategy_ids),
                                        limiter=limiter,
                                        common_days=days,
                                        max_dd=drawdown,
                                    )
                                    active_margin_kwargs["limiter_release_status"] = limiter_status
                                    active_solution = _Solution(proposed_solution.bank, active_x)
                                    revalidated = _candidates_for_solution(
                                        active_solution,
                                        active_rows,
                                        active_members,
                                        active_caps,
                                        max_dd=drawdown,
                                        common_days=days,
                                        target=target_value,
                                        profile_id=profile_id,
                                        scenario_id=scenario_id,
                                        margin_coefficients=active_coefficients,
                                        margin_kwargs=active_margin_kwargs,
                                        bank_available=fixed_bank,
                                        B_risk=additional_risk,
                                        bootstrap_summary=additional_summary,
                                        allow_rescue=False,
                                    )
                                except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                                    revalidated = ()
                                if target_value is not None:
                                    revalidated = tuple(
                                        candidate
                                        for candidate in revalidated
                                        if candidate.metrics.get("p30_common_usdt_30d") is not None
                                        and candidate.metrics["p30_common_usdt_30d"] >= target_value - _solver_tolerance(target_value)
                                    )
                                if not revalidated:
                                    additional_manifest.update({"outcome": "rejected", "reason": "REVALIDATION_FAILED"})
                                else:
                                    accepted_count = 0
                                    duplicate_count = 0
                                    primary_x_digest = candidate_x_digest(revalidated[0])
                                    for ordinal, candidate in enumerate(revalidated[:3]):
                                        if candidate.identity in candidate_identities:
                                            duplicate_count += 1
                                            continue
                                        candidate_identities.add(candidate.identity)
                                        ordered_candidates.append(candidate)
                                        candidate_origins[candidate.identity] = "scale" if ordinal == 0 or candidate_x_digest(candidate) != primary_x_digest else "limiter"
                                        accepted_count += 1
                                    if duplicate_count:
                                        additional_manifest["duplicate_identity_count"] = duplicate_count
                                    additional_manifest.update({
                                        "outcome": "accepted" if accepted_count else "rejected",
                                        "reason": None if accepted_count else "DUPLICATE_IDENTITY",
                                    })

    if additional_bootstrap_result is not None:
        additional_manifest["bootstrap"] = additional_bootstrap_result.manifest
    additional_manifest["final_new_x_count"] = len(new_x_digests)
    base_full_checked_count = len(bootstrap_result.vector_digests) if bootstrap_result is not None and bootstrap_result.complete else 0

    cdar_manifest: dict[str, Any] = {
        "attempted": False,
        "solve_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "reasons": [],
        "selected_source_families": [],
        "reason": None,
    }
    cdar_bootstrap_manifests: list[Mapping[str, Any]] = []
    if budget_reason is not None or error_reason is not None:
        cdar_manifest["reason"] = budget_reason or error_reason or "BASE_INCOMPLETE"
    elif margin_a is None or margin_b is None:
        cdar_manifest["reason"] = "MARGIN_BOUND_UNAVAILABLE"
    else:
        selected_cdar_sources = _select_cdar_families(candidate_sources)
        for source_candidate, source_target, source_solution, _source_summary in selected_cdar_sources:
            if budget_reason is not None or error_reason is not None:
                break
            source_metrics = source_candidate.metrics
            try:
                source_p30 = _decimal(source_metrics.get("p30_common_usdt_30d"), "source_p30")
                cdar_floor = _cdar_p30_floor(target, source_p30)
                source_limiter = source_metrics.get("limiter_L")
                if type(source_limiter) is not int or source_limiter < 0 or (source_limiter and source_limiter >= n):
                    raise ValueError("LIMITER_INVALID")
                source_x, supplied_priorities = _candidate_vector_by_strategy_id(
                    source_candidate.members, prepared.strategy_ids, caps
                )
                derived_priorities = derive_priorities(source_members, source_x)
                frozen_priorities = tuple(
                    supplied if supplied is not None else derived_priorities.get(strategy_id)
                    for strategy_id, supplied in zip(prepared.strategy_ids, supplied_priorities)
                )
                if any(value is None for value in frozen_priorities):
                    raise ValueError("PRIORITY_UNKNOWN")
                fixed_bank = max(
                    _decimal(source_metrics.get("required_bank_usdt", source_solution.bank), "bank_fixed", positive=True),
                    _decimal(source_solution.bank, "bank_fixed", positive=True),
                )
                if available is not None and fixed_bank > available + _solver_tolerance(fixed_bank, available):
                    raise ValueError("BANK_UNAVAILABLE")
                limiter_status = str(source_metrics.get("limiter_release_status", (margin_kwargs or {}).get("limiter_release_status", UNKNOWN))).upper()
                if limiter_status not in {UNKNOWN, "CONFIRMED"}:
                    limiter_status = UNKNOWN
            except (ArithmeticError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
                reason = str(exc) or "CDAR_SOURCE_INVALID"
                cdar_manifest["reasons"].append(reason)
                cdar_manifest["rejected_count"] += 1
                continue
            cdar_manifest["selected_source_families"].append({
                "identity": source_candidate.identity,
                "target": source_target,
                "floor": cdar_floor,
                "bank_fixed": fixed_bank,
                "L": source_limiter,
                "limiter_release_status": limiter_status,
            })
            reason = budget_check()
            if reason is not None:
                budget_reason = reason
                cdar_manifest["reasons"].append(reason)
                break
            if calls >= 20:
                budget_reason = "SOLVER_CALL_LIMIT"
                cdar_manifest["reasons"].append(budget_reason)
                break
            calls += 1
            cdar_manifest["attempted"] = True
            cdar_manifest["solve_count"] += 1
            try:
                outcome = _solve_cdar80_lp(
                    normalized_delta,
                    caps,
                    bank_fixed=fixed_bank,
                    max_dd=drawdown,
                    objective_coefficients=coefficients,
                    L=source_limiter,
                    priorities=frozen_priorities,
                    margin_a=margin_a,
                    margin_b=margin_b,
                    max_mm_load=max_mm_load,
                    reserve=(margin_kwargs or {}).get("reserve", Decimal("0.40")),
                    limiter_release_status=limiter_status,
                    time_limit=solver_seconds,
                    p30_floor=cdar_floor,
                    p30_floor_coefficients=coefficients,
                )
            except (ArithmeticError, TypeError, ValueError, RuntimeError):
                outcome = _SolveOutcome("ERROR", reason="SOLVER_ERROR")
            solver_calls.append({
                "call": calls,
                "stage": "cdar",
                "source_identity": source_candidate.identity,
                "target": source_target,
                "status": outcome.status,
                "solver_status": outcome.solver_status,
                "solver_message": outcome.solver_message,
                "residuals": dict(outcome.residuals or {}),
                "budget_limited": outcome.budget_limited,
            })
            if outcome.budget_limited:
                budget_reason = "SOLVER_TIME_LIMIT"
                cdar_manifest["reasons"].append(budget_reason)
                break
            if outcome.status != "PASS" or outcome.solution is None:
                cdar_manifest["rejected_count"] += 1
                cdar_manifest["reasons"].append(outcome.reason or "LP_INFEASIBLE")
                continue
            proposed_solution = outcome.solution
            try:
                proposed_bank = _decimal(proposed_solution.bank, "cdar_bank", positive=True)
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                proposed_bank = None
            if proposed_bank is None or abs(proposed_bank - fixed_bank) > _solver_tolerance(proposed_bank, fixed_bank):
                cdar_manifest["rejected_count"] += 1
                cdar_manifest["reasons"].append("FIXED_BANK_MISMATCH")
                continue
            proposed_digest = _digest({"x": proposed_solution.x})
            if proposed_digest in base_x_digests or proposed_digest in new_x_digests:
                cdar_manifest["rejected_count"] += 1
                cdar_manifest["reasons"].append("DUPLICATE_X")
                continue
            if len(new_x_digests) >= max_candidates:
                cdar_manifest["rejected_count"] += 1
                cdar_manifest["reasons"].append("NEW_X_LIMIT")
                continue
            remaining = remaining_wall()
            if remaining is None:
                budget_reason = "WALL_TIME_LIMIT"
                cdar_manifest["reasons"].append(budget_reason)
                break
            if cancel is not None and cancel():
                budget_reason = "CANCELLED"
                cdar_manifest["reasons"].append(budget_reason)
                break
            try:
                cdar_bootstrap = bootstrap_banks(
                    normalized_delta,
                    (proposed_solution.x,),
                    max_dd=drawdown,
                    common_days=days,
                    seed=seed,
                    history_step_minutes=history_step_minutes,
                    scenarios_per_family=bootstrap_scenarios,
                    workers=workers,
                    cancel=cancel,
                    wall_time_limit_seconds=remaining,
                    prefix_result=None,
                )
            except (ArithmeticError, TypeError, ValueError, RuntimeError):
                cdar_bootstrap = None
            if cdar_bootstrap is None:
                cdar_manifest["rejected_count"] += 1
                cdar_manifest["reasons"].append("BOOTSTRAP_ERROR")
                continue
            if isinstance(cdar_bootstrap.manifest, Mapping):
                cdar_bootstrap_manifests.append(cdar_bootstrap.manifest)
            if not cdar_bootstrap.complete or not cdar_bootstrap.risk_banks or cdar_bootstrap.risk_banks[0] is None:
                budget_reason = cdar_bootstrap.manifest.get("stopping_reason") or "BOOTSTRAP_INCOMPLETE"
                cdar_manifest["reasons"].append(budget_reason)
                break
            new_x_digests.add(proposed_digest)
            try:
                summary = {
                    "p95_banks": cdar_bootstrap.p95_banks[0],
                    "block_days": cdar_bootstrap.manifest["block_days"],
                    "diagnostics": cdar_bootstrap.manifest["diagnostics"],
                    "witnesses": cdar_bootstrap.p95_witnesses[0],
                    "parameters": {
                        "seed": cdar_bootstrap.manifest["seed"],
                        "history_step_minutes": cdar_bootstrap.manifest["history_step_minutes"],
                        "numpy_version": cdar_bootstrap.manifest["numpy_version"],
                    },
                }
            except (KeyError, IndexError, TypeError, AttributeError):
                cdar_manifest["rejected_count"] += 1
                cdar_manifest["reasons"].append("BOOTSTRAP_SUMMARY_INVALID")
                continue
            revalidation_reason = "REVALIDATION_FAILED"
            try:
                (
                    active_rows,
                    active_x,
                    active_members,
                    active_caps,
                    active_coefficients,
                    active_margin_kwargs,
                ) = _compact_additional_inputs(
                    proposed_solution.x,
                    normalized_delta,
                    source_members,
                    caps,
                    margin_coefficients,
                    margin_kwargs or {},
                    strategy_ids=tuple(prepared.strategy_ids),
                    limiter=source_limiter,
                    common_days=days,
                    max_dd=drawdown,
                )
                active_margin_kwargs["limiter_release_status"] = limiter_status
                frozen_by_id = dict(zip(prepared.strategy_ids, frozen_priorities))
                active_margin_kwargs["priorities"] = tuple(
                    frozen_by_id[member["strategy_id"]] for member in active_members
                )
                revalidated = _candidates_for_solution(
                    _Solution(fixed_bank, active_x),
                    active_rows,
                    active_members,
                    active_caps,
                    max_dd=drawdown,
                    common_days=days,
                    target=cdar_floor,
                    profile_id=profile_id,
                    scenario_id=scenario_id,
                    margin_coefficients=active_coefficients,
                    margin_kwargs=active_margin_kwargs,
                    bank_available=fixed_bank,
                    B_risk=cdar_bootstrap.risk_banks[0],
                    bootstrap_summary=summary,
                    allow_rescue=False,
                )
            except KeyError:
                revalidated = ()
                revalidation_reason = "PRIORITY_MEMBER_UNKNOWN"
            except IndexError:
                revalidated = ()
                revalidation_reason = "REVALIDATION_INDEX_INVALID"
            except (ArithmeticError, TypeError, ValueError, InvalidOperation):
                revalidated = ()
            revalidated = tuple(
                candidate
                for candidate in revalidated
                if candidate.status == PASS
                and candidate.metrics.get("p30_common_usdt_30d") is not None
                and candidate.metrics["p30_common_usdt_30d"] >= cdar_floor - _solver_tolerance(cdar_floor)
            )
            accepted_count = 0
            primary_x_digest = candidate_x_digest(revalidated[0]) if revalidated else None
            for ordinal, candidate in enumerate(revalidated[:3]):
                if candidate.identity in candidate_identities:
                    continue
                candidate_identities.add(candidate.identity)
                ordered_candidates.append(candidate)
                candidate_origins[candidate.identity] = "cdar" if ordinal == 0 or candidate_x_digest(candidate) != primary_x_digest else "limiter"
                accepted_count += 1
            if accepted_count:
                cdar_manifest["accepted_count"] += accepted_count
            else:
                cdar_manifest["rejected_count"] += 1
                cdar_manifest["reasons"].append(revalidation_reason if not revalidated else "DUPLICATE_IDENTITY")
        if not cdar_manifest["attempted"] and not cdar_manifest["reasons"]:
            cdar_manifest["reason"] = "NOT_RUN"
    cdar_manifest["reasons"] = tuple(cdar_manifest["reasons"])
    cdar_manifest["selected_source_families"] = tuple(cdar_manifest["selected_source_families"])
    cdar_manifest["new_x_count"] = len(new_x_digests)
    cdar_manifest["scenario_checked_x_count"] = base_full_checked_count + len(new_x_digests)

    if proposed_x is not None and ordered_candidates and budget_reason is None:
        ordered_candidates = list(_revalidate_proposed_x(
            tuple(ordered_candidates),
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
        ))
    for candidate in ordered_candidates:
        candidate_origins.setdefault(candidate.identity, "scale")
    ordered_candidates, shortlist_manifest = _select_shortlist(
        ordered_candidates,
        max_candidates=max_candidates,
        bank_available=available,
        origins=candidate_origins,
    )
    final_reason = budget_reason or error_reason
    complete = budget_reason is None and error_reason is None
    bootstrap_manifest = None if bootstrap_result is None else bootstrap_result.manifest
    screening_manifest = None if screening_result is None else screening_result.manifest
    nested_bootstrap_manifests = tuple(
        manifest
        for manifest in (
            screening_manifest,
            bootstrap_manifest,
            additional_manifest.get("bootstrap"),
        )
        if isinstance(manifest, Mapping)
    )
    remaining_work = {
        "solver_call_slots": max(0, 20 - calls),
        "base_x_slots": max(0, 2 * max_candidates - base_full_checked_count),
        "new_x_slots": max(0, max_candidates - len(new_x_digests)),
        "scenario_x_slots": max(0, 3 * max_candidates - (base_full_checked_count + len(new_x_digests))),
        "unexplored_scenario_count": sum(
            int(item.get("unexplored_scenario_count", 0)) for item in nested_bootstrap_manifests
        ) + sum(int(item.get("unexplored_scenario_count", 0)) for item in cdar_bootstrap_manifests),
    }
    manifest = {
        "parameters": {
            "max_candidates": max_candidates,
            "seed": seed,
            "bootstrap_scenarios": bootstrap_scenarios,
            "screening_scenarios": screening_scenarios,
            "workers": workers,
            "wall_time": wall_seconds,
            "solver_time": solver_seconds,
            "history_step_minutes": history_step_minutes,
        },
        "solver_calls": tuple(solver_calls),
        "solver_call_count": calls,
        "lp_call_count": calls,
        "base_x_count": len(unique_entries),
        "selected_base_x_count": len(selected_entries),
        "base_full_checked_x_count": base_full_checked_count,
        "new_x_count": len(new_x_digests),
        "scenario_checked_x_count": base_full_checked_count + len(new_x_digests),
        "additional_pass_count": additional_manifest["additional_pass_count"],
        "additional": additional_manifest,
        "cdar": cdar_manifest,
        "shortlist": shortlist_manifest,
        "final_count": len(ordered_candidates),
        "duplicate_base_x_count": len(discovered) - len(unique_entries),
        "screening": screening_manifest,
        "bootstrap": bootstrap_manifest,
        "remaining_work": remaining_work,
        "complete": complete,
        "stopping_reason": final_reason,
    }
    if budget_reason is not None:
        return SearchResult(
            status="budget_limited",
            reason=budget_reason,
            total_combinations=calls,
            candidates=tuple(ordered_candidates),
            evaluated=len(ordered_candidates),
            warnings=tuple(filter(None, (error_reason, conversion_failure))),
            mode=WEIGHTED_V1,
            manifest=manifest,
        )
    if error_reason is not None:
        return SearchResult(status=FAIL, reason=error_reason, total_combinations=calls, evaluated=0, mode=WEIGHTED_V1, manifest=manifest)
    if not ordered_candidates:
        if target is None and available is not None:
            reason = "LP_INFEASIBLE"
        elif target is None and available is None:
            reason = "FRONTIER_INFEASIBLE"
        else:
            reason = conversion_failure or "TARGET_INFEASIBLE"
        return SearchResult(status=FAIL, reason=reason, total_combinations=calls, evaluated=0, mode=WEIGHTED_V1, manifest=manifest)
    return SearchResult(
        status=PASS,
        total_combinations=calls,
        candidates=tuple(ordered_candidates),
        evaluated=len(ordered_candidates),
        warnings=tuple(filter(None, (error_reason, conversion_failure))),
        mode=WEIGHTED_V1,
        manifest=manifest,
    )


__all__ = [
    "WEIGHTED_V1", "LimiterReplayResult", "derive_priorities", "priority_details",
    "replay_limiter", "bank_for_path", "evaluate_weighted_path", "weighted_search",
    "nearest_rank", "stationary_bootstrap_indices", "bootstrap_banks",
]
