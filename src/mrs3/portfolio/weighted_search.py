"""Phase 3 weighted portfolio search.

The search is deliberately small: Phase 2 supplies one aligned participant
matrix, this module solves the peak-equity drawdown LP, and the existing
candidate-search data classes carry the compact result.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from scipy.optimize import linprog

from .candidate_search import (
    CANDIDATE_SCHEMA_VERSION,
    FAIL,
    PASS,
    PortfolioCandidate,
    SearchResult,
)
from .input import PreparedWeightedInput


WEIGHTED_V1 = "WEIGHTED_V1"
_EPS = Decimal("0.0000001")
_NORMALIZATION_EPS = Decimal("0.00000001")
_RELATIVE_SOLVER_EPS = Decimal("0.000000001")
_RAW_KEYS = frozenset({
    "normalized_delta", "equity", "equity_series", "equity_path", "actions",
    "action_series", "strategy_actions", "minute_actions", "cycles",
})


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
            return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID")
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
            return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID")
        if bank_available is not None and authoritative_bank > bank_available:
            return _SolveOutcome("ERROR", reason="LP_SOLUTION_INVALID")
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


def _safe_member(member: Mapping[str, Any], x: Decimal, capacity: Decimal) -> dict[str, Any]:
    result = {
        key: value
        for key, value in member.items()
        if key not in _RAW_KEYS and key in {"symbol", "side", "strategy_id", "result_id", "user_rank", "user_status"}
    }
    result["capacity_usdt"] = capacity
    result["x_usdt"] = x
    return result


def _result(status: str, reason: str | None = None) -> SearchResult:
    return SearchResult(status=status, reason=reason, mode=WEIGHTED_V1)


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
) -> PortfolioCandidate | None:
    evaluated = evaluate_weighted_path(normalized_delta, solution.x, max_dd=max_dd, common_days=common_days)
    with localcontext() as context:
        context.prec = _precision_for(evaluated["bank_for_path"], solution.bank)
        if evaluated["bank_for_path"] > solution.bank + _solver_tolerance(evaluated["bank_for_path"], solution.bank):
            return None
    compact_members = tuple(_safe_member(member, x, cap) for member, x, cap in zip(source_members, solution.x, capacities))
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
    identity = _digest({"profile_id": profile_id, "scenario_id": scenario_id, "members": compact_members, "metrics": metrics})
    return PortfolioCandidate(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        profile_id=str(profile_id),
        scenario_id=str(scenario_id),
        identity=identity,
        members=compact_members,
        metrics=metrics,
        status=PASS,
    )


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
        candidate = _candidate_for_solution(
            solution,
            normalized_delta,
            source_members,
            caps,
            max_dd=drawdown,
            common_days=days,
            target=target_value,
            profile_id=profile_id,
            scenario_id=scenario_id,
        )
        if candidate is None:
            error_reason = "LP_SOLUTION_INVALID"
            return False
        if target_value is not None:
            solved[target_value] = (solution, candidate)
        ordered_candidates.append(candidate)
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
        while error_reason is None and len(ordered_candidates) < max_targets:
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
        return SearchResult(status=FAIL, reason=error_reason, total_combinations=calls, evaluated=0, mode=WEIGHTED_V1)
    if not ordered_candidates:
        if target is None and available is not None:
            reason = "LP_INFEASIBLE"
        elif target is None and available is None:
            reason = "FRONTIER_INFEASIBLE"
        else:
            reason = "TARGET_INFEASIBLE"
        return SearchResult(status=FAIL, reason=reason, total_combinations=calls, evaluated=0, mode=WEIGHTED_V1)
    return SearchResult(
        status=PASS,
        total_combinations=calls,
        candidates=tuple(ordered_candidates),
        evaluated=len(ordered_candidates),
        mode=WEIGHTED_V1,
    )


__all__ = ["WEIGHTED_V1", "bank_for_path", "evaluate_weighted_path", "weighted_search"]
