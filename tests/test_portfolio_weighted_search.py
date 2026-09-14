from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext, localcontext
import importlib

import pytest

from mrs3.portfolio.candidate_search import PortfolioCandidate, SearchResult
from mrs3.portfolio.input import PreparedWeightedInput
from mrs3.portfolio.position_sizing import size_composition_vector
from mrs3.portfolio.weighted_search import (
    _precision_for,
    _sum_products,
    bank_for_path,
    evaluate_weighted_path,
    weighted_search,
)


def _prepared(rows: tuple[tuple[str, ...], ...], strategy_ids=(1, 2)) -> PreparedWeightedInput:
    t = len(rows)
    timestamps = tuple(
        datetime(2024, 1, 1, tzinfo=timezone.utc).replace(minute=5 * index).isoformat().replace("+00:00", "Z")
        for index in range(t + 1)
    )
    return PreparedWeightedInput(
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 1, 0, 5 * t, tzinfo=timezone.utc),
        5,
        timestamps,
        tuple(strategy_ids),
        tuple(tuple(Decimal(value) for value in row) for row in rows),
        tuple(tuple(True for _ in row) for row in rows),
        tuple(tuple(None for _ in row) for row in rows),
        {},
        {},
        "fixture",
    )


def _members(count: int = 2):
    return tuple(
        {"symbol": f"S{index}", "side": "LONG", "strategy_id": index + 1, "result_id": 100 + index}
        for index in range(count)
    )


def test_bank_for_path_matches_independent_peak_drawdown_calculation() -> None:
    g = (Decimal("0"), Decimal("1000"), Decimal("400"))
    bank = bank_for_path(g, Decimal("0.20"))
    equity = tuple(Decimal("2000") + value for value in g)
    peaks = []
    high = Decimal("0")
    for value in equity:
        high = max(high, value)
        peaks.append(high)
    dd = max((peak - value) / peak for peak, value in zip(peaks, equity))
    assert bank == Decimal("2000")
    assert dd <= Decimal("0.20")


def test_negative_first_observation_keeps_initial_bank_as_peak() -> None:
    evaluated = evaluate_weighted_path(((Decimal("-1"),),), (Decimal("10"),), max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert evaluated["bank_for_path"] == Decimal("50")
    assert evaluated["max_drawdown_fraction"] == Decimal("0.2")
    assert evaluated["max_drawdown_pct"] == Decimal("20.0")


def test_bank_for_path_ignores_a_low_global_decimal_precision() -> None:
    original = getcontext().prec
    try:
        with localcontext() as context:
            context.prec = 2
            expected = bank_for_path((Decimal("0"), Decimal("1000"), Decimal("400")), Decimal("0.20"))
        with localcontext() as context:
            context.prec = 2
            actual = bank_for_path((Decimal("0"), Decimal("1000"), Decimal("400")), Decimal("0.20"))
        assert actual == expected == Decimal("2000")
    finally:
        getcontext().prec = original


def test_precision_bound_does_not_scale_with_matrix_cell_count() -> None:
    assert _precision_for(tuple(Decimal("1.2345") for _ in range(10_000))) < 256
    assert _precision_for((Decimal("1e100"), Decimal("1"))) >= 101


def test_sum_products_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError, match="VECTOR_SHAPE_MISMATCH"):
        _sum_products((Decimal("1"),), (Decimal("1"), Decimal("2")))


def test_huge_finite_lp_input_fails_closed() -> None:
    prepared = _prepared((("1e10000", "0"),))
    result = weighted_search(prepared, capacities=(Decimal("1"), Decimal("1")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=1)
    assert result.status == "FAIL"
    assert result.reason == "SOLVER_ERROR"


def test_realistic_scale_lp_reports_authoritative_bank_that_satisfies_drawdown() -> None:
    cycle = ((Decimal("0.4"), Decimal("-0.2")), (Decimal("-0.4"), Decimal("0.3")), (Decimal("0.1"), Decimal("-0.2")), (Decimal("0.1"), Decimal("0")))
    rows = cycle * 75
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    prepared = PreparedWeightedInput(
        start,
        start + timedelta(minutes=5 * len(rows)),
        5,
        tuple((start + timedelta(minutes=5 * index)).isoformat().replace("+00:00", "Z") for index in range(len(rows) + 1)),
        (1, 2),
        rows,
        tuple((True, True) for _ in rows),
        tuple((None, None) for _ in rows),
        {},
        {},
        "realistic-scale",
    )
    result = weighted_search(prepared, capacities=(Decimal("10000"), Decimal("1000000")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("100000"))
    assert result.status == "PASS"
    candidate = result.candidates[0]
    x = tuple(member["x_usdt"] for member in candidate.members)
    evaluated = evaluate_weighted_path(rows, x, max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert candidate.metrics["required_bank_usdt"] == max(Decimal("1"), evaluated["bank_for_path"])
    assert candidate.metrics["bank_for_path_usdt"] == evaluated["bank_for_path"]
    assert evaluated["max_drawdown_fraction"] <= Decimal("0.2")


def test_capacity_mapping_requires_one_unambiguous_strategy_key() -> None:
    prepared = _prepared((("0.4", "0.1"), ("-0.2", "0.2")))
    with pytest.raises(ValueError, match="AMBIGUOUS_CAPACITY_KEY"):
        weighted_search(prepared, capacities={1: Decimal("100"), "1": Decimal("200"), 2: Decimal("100")}, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("100"))
    with pytest.raises(ValueError, match="MISSING_CAPACITY_1"):
        weighted_search(prepared, capacities={"S0": Decimal("100"), 2: Decimal("100")}, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("100"))
    for stale in ({1: Decimal("100"), 2: Decimal("100"), 3: Decimal("50")}, {1: Decimal("100"), 2: Decimal("100"), "S0": Decimal("50")}):
        with pytest.raises(ValueError, match="UNKNOWN_CAPACITY_KEY"):
            weighted_search(prepared, capacities=stale, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("100"))


def test_capacity_mapping_and_sequence_are_equivalent() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    sequence = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("450"))
    mapping = weighted_search(prepared, capacities={1: Decimal("100"), 2: Decimal("100")}, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("450"))
    assert tuple(member["x_usdt"] for member in sequence.candidates[0].members) == tuple(member["x_usdt"] for member in mapping.candidates[0].members)
    assert sequence.candidates[0].metrics["required_bank_usdt"] == mapping.candidates[0].metrics["required_bank_usdt"]
    assert sequence.candidates[0].identity == mapping.candidates[0].identity


def test_capacity_sequence_requires_exact_member_count() -> None:
    prepared = _prepared((("0.4", "0.1"),))
    for capacities in ((Decimal("100"),), (Decimal("100"), Decimal("100"), Decimal("100"))):
        with pytest.raises(ValueError, match="CAPACITY_SHAPE_MISMATCH"):
            weighted_search(prepared, capacities=capacities, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"))


def test_authoritative_bank_available_check_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    monkeypatch.setattr(weighted_search_module, "bank_for_path", lambda path, max_dd: Decimal("1.00000000001"))
    prepared = _prepared((("0.4", "0.1"),))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"), bank_available=Decimal("1"))
    assert result.status == "FAIL"
    assert result.reason == "LP_SOLUTION_INVALID"


def test_genuine_infeasible_target_keeps_target_infeasible_reason() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("1000"))
    assert result.status == "FAIL"
    assert result.reason == "TARGET_INFEASIBLE"


def test_solver_error_fails_closed_without_partial_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    monkeypatch.setattr(weighted_search_module, "linprog", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("solver down")))
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=2)
    assert result.status == "FAIL"
    assert result.reason == "SOLVER_ERROR"
    assert result.candidates == ()


def test_weighted_lp_returns_candidate_search_result_without_raw_payload() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.20"), common_days=Decimal("1"), target_p30=Decimal("450"))

    assert isinstance(result, SearchResult)
    assert not isinstance(result, __import__("mrs3.portfolio.search", fromlist=["SearchResult"]).SearchResult)
    assert result.mode == "WEIGHTED_V1"
    assert result.status == "PASS"
    assert isinstance(result.candidates[0], PortfolioCandidate)
    assert result.candidates[0].metrics["p30_common_usdt_30d"] >= Decimal("450")
    member = result.candidates[0].members[0]
    assert member["x_usdt"] >= 0
    assert member["capacity_usdt"] == Decimal("100")
    assert all(key not in member for key in ("normalized_delta", "equity", "actions", "cycles"))
    assert all(key not in result.candidates[0].metrics for key in ("normalized_delta", "equity", "actions", "cycles"))


def test_negative_participant_is_retained_when_it_reduces_required_bank() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.20"), common_days=Decimal("1"), target_p30=Decimal("450"))
    sizes = {member["strategy_id"]: member["x_usdt"] for member in result.candidates[0].members}
    assert sizes[2] > 0


def test_weighted_search_rejects_any_invalid_participant_cell() -> None:
    prepared = _prepared((("0.1", "0.2"), ("0", "0")))
    prepared = PreparedWeightedInput(
        prepared.period_start_utc,
        prepared.period_end_utc,
        prepared.history_step_minutes,
        prepared.timestamps_utc,
        prepared.strategy_ids,
        prepared.normalized_delta,
        ((True, False), (True, True)),
        prepared.reasons,
        prepared.cycles,
        prepared.diagnostics,
        prepared.preparation_key,
    )
    with pytest.raises(ValueError, match="INVALID_PARTICIPANT_CELL"):
        weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"))


def test_evaluate_weighted_path_recomputes_drawdown_after_diversifier_removed() -> None:
    deltas = ((Decimal("0.4"), Decimal("-0.2")), (Decimal("-0.4"), Decimal("0.3")), (Decimal("0.1"), Decimal("-0.2")), (Decimal("0.1"), Decimal("0")))
    with_diversifier = evaluate_weighted_path(deltas, (Decimal("100"), Decimal("50")), max_dd=Decimal("0.2"), common_days=Decimal("1"))
    without_diversifier = evaluate_weighted_path(deltas, (Decimal("100"), Decimal("0")), max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert without_diversifier["bank_for_path"] > with_diversifier["bank_for_path"]


def test_weighted_search_keeps_negative_overall_target_empty() -> None:
    prepared = _prepared((("-0.1", "-0.2"), ("0", "0")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert result.status == "FAIL"
    assert result.reason == "NO_POSITIVE_TARGET"


def test_weighted_lp_is_no_worse_than_a_coarse_grid() -> None:
    deltas = (("0.5", "-0.1"), ("-0.1", "0.3"), ("0.1", "-0.2"))
    prepared = _prepared(deltas)
    result = weighted_search(prepared, capacities=(Decimal("10"), Decimal("10")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("75"))
    assert result.status == "PASS"
    lp_bank = result.candidates[0].metrics["bank_for_path_usdt"]
    grid_banks = []
    for x0 in (Decimal("0"), Decimal("2.5"), Decimal("5"), Decimal("7.5"), Decimal("10")):
        for x1 in (Decimal("0"), Decimal("2.5"), Decimal("5"), Decimal("7.5"), Decimal("10")):
            evaluated = evaluate_weighted_path(
                tuple(tuple(Decimal(value) for value in row) for row in deltas), (x0, x1), max_dd=Decimal("0.2"), common_days=Decimal("1"),
            )
            if evaluated["p30_common"] >= Decimal("75"):
                grid_banks.append(evaluated["bank_for_path"])
    assert grid_banks
    assert lp_bank <= min(grid_banks) + Decimal("0.000001")


def test_homogeneous_participants_keep_the_same_ray() -> None:
    first = _prepared((("0.2", "0.4"), ("0.1", "0.2")))
    second = weighted_search(first, capacities=(Decimal("10"), Decimal("10")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("270"))
    assert second.status == "PASS"
    members = second.candidates[0].members
    assert members[0]["x_usdt"] == members[1]["x_usdt"] == Decimal("10")


def test_active_capacity_changes_share_and_identity() -> None:
    prepared = _prepared((("0.4", "0.1"), ("-0.2", "0.2"), ("0.1", "0"), ("0.1", "0")))
    broad = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("900"))
    constrained = weighted_search(prepared, capacities=(Decimal("100"), Decimal("20")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("900"))
    assert broad.status == constrained.status == "PASS"
    assert broad.candidates[0].identity != constrained.candidates[0].identity
    assert broad.candidates[0].members != constrained.candidates[0].members


def test_missing_strategy_id_is_rejected_and_shuffled_members_are_reassociated() -> None:
    prepared = _prepared((("0.4", "0.1"), ("-0.2", "0.2"), ("0.1", "0"), ("0.1", "0")))
    missing = ({"symbol": "S0", "side": "LONG", "result_id": 100}, {"symbol": "S1", "side": "LONG", "strategy_id": 2, "result_id": 101})
    with pytest.raises(ValueError, match="MEMBER_STRATEGY_SHAPE_MISMATCH"):
        weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=missing, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("900"))
    shuffled = weighted_search(prepared, capacities={1: Decimal("100"), 2: Decimal("20")}, members=(_members()[1], _members()[0]), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("900"))
    assert shuffled.status == "PASS"
    assert [member["strategy_id"] for member in shuffled.candidates[0].members] == [1, 2]


def test_weighted_target_frontier_k1_and_k2_have_deterministic_order_and_counts() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    one = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=1)
    two = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=2)
    assert [candidate.metrics["target_p30_usdt_30d"] for candidate in one.candidates] == [Decimal("600")]
    assert [candidate.metrics["target_p30_usdt_30d"] for candidate in two.candidates] == [Decimal("600"), Decimal("300")]
    assert one.total_combinations == one.evaluated == 1
    assert two.total_combinations == two.evaluated == 2


def test_weighted_target_frontier_deduplicates_initial_targets_and_refines_active_capacity_interval() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=4)
    targets = [candidate.metrics["target_p30_usdt_30d"] for candidate in result.candidates]
    assert targets[:3] == [Decimal("600"), Decimal("150"), Decimal("300")]
    assert len(targets) == len(set(targets)) == 4
    assert targets[3] == Decimal("450")
    assert result.total_combinations == result.evaluated == 4
    first = result.candidates[0].members
    refined = result.candidates[-1].members
    assert tuple(member["x_usdt"] for member in first) != tuple(member["x_usdt"] for member in refined)


def test_explicit_target_and_available_bank_do_not_generate_a_frontier() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    explicit = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("450"), max_targets=8)
    available = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), bank_available=Decimal("200"), max_targets=8)
    assert len(explicit.candidates) == len(available.candidates) == 1


def test_no_candidate_reasons_distinguish_explicit_bank_and_frontier_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3")))
    bank_limited = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), bank_available=Decimal("0.5"))
    assert bank_limited.status == "FAIL"
    assert bank_limited.reason == "LP_INFEASIBLE"
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    monkeypatch.setattr(weighted_search_module, "_solve_lp", lambda *args, **kwargs: weighted_search_module._SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE"))
    frontier = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=2)
    assert frontier.status == "FAIL"
    assert frontier.reason == "FRONTIER_INFEASIBLE"


def test_low_global_precision_preserves_frontier_targets_and_identity() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    default = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=4)
    with localcontext() as context:
        context.prec = 2
        low_precision = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=4)
        direct_300 = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("300"), max_targets=4)
    assert direct_300.status == default.status == "PASS"
    assert [candidate.metrics["target_p30_usdt_30d"] for candidate in low_precision.candidates] == [candidate.metrics["target_p30_usdt_30d"] for candidate in default.candidates]
    assert [candidate.identity for candidate in low_precision.candidates] == [candidate.identity for candidate in default.candidates]


def test_candidate_members_are_immutably_frozen() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    candidate = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("450")).candidates[0]
    original_identity = candidate.identity
    with pytest.raises(TypeError):
        candidate.members[0]["x_usdt"] = Decimal("1")
    assert candidate.identity == original_identity


def test_rounding_out_a_planned_diversifier_recomputes_realized_drawdown() -> None:
    rows = ((Decimal("1"), Decimal("-1")), (Decimal("-2"), Decimal("2")), (Decimal("2"), Decimal("0")))
    prepared = _prepared(tuple(tuple(str(value) for value in row) for row in rows))
    members = (
        {"symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 101, "strategy_orders": ({"order_id": 1, "lot_x": Decimal("1")},)},
        {"symbol": "B", "side": "LONG", "strategy_id": 2, "result_id": 102, "strategy_orders": ({"order_id": 1, "lot_x": Decimal("1")},)},
    )
    plan = weighted_search(prepared, capacities=(Decimal("100"), Decimal("5")), members=members, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("3150"))
    assert plan.status == "PASS"
    planned_x = tuple(member["x_usdt"] for member in plan.candidates[0].members)
    references = {
        "A": {"qty_step": "0.1", "min_qty": "0.1", "max_qty": "100", "min_notional": "1"},
        "B": {"qty_step": "0.01", "min_qty": "0.1", "max_qty": "100", "min_notional": "1"},
    }
    sized = size_composition_vector(
        members,
        {"A": {"position_cap_usdt": "100", "round_down_usdt": "0.01"}, "B": {"position_cap_usdt": "5", "round_down_usdt": "0.01"}},
        references,
        {"A": Decimal("100"), "B": Decimal("100")},
        targets=planned_x,
    )
    assert sized.status == "PASS"
    assert sized.exclusions[0].reason == "SIZE_BELOW_MINIMUM_QTY"
    realized_x = tuple(next((member["actual_size_usdt"] for member in sized.members if member["strategy_id"] == strategy_id), Decimal("0")) for strategy_id in (1, 2))
    planned = evaluate_weighted_path(rows, planned_x, max_dd=Decimal("0.2"), common_days=Decimal("1"))
    realized = evaluate_weighted_path(rows, realized_x, max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert realized["bank_for_path"] > planned["bank_for_path"]
