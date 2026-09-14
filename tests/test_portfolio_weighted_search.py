from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext, localcontext
import importlib

import pytest

from mrs3.portfolio.candidate_search import PortfolioCandidate, SearchResult
from mrs3.portfolio.input import PreparedWeightedInput
from mrs3.portfolio.margin import MarginCoefficient, MarginCoefficientResult
from mrs3.portfolio.position_sizing import size_composition_vector
from mrs3.portfolio.weighted_search import (
    _MarginVariant,
    _Solution,
    _candidate_for_solution,
    _candidates_for_solution,
    _margin_variant,
    _ordered_margin_variants,
    _precision_for,
    _sum_products,
    bank_for_path,
    derive_priorities,
    evaluate_weighted_path,
    replay_limiter,
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


def _evidence_coefficients(rates, strategy_ids=None, *, max_notional=Decimal("1000000")):
    ids = tuple(range(1, len(rates) + 1)) if strategy_ids is None else tuple(strategy_ids)
    return tuple(
        MarginCoefficient(strategy_id, initial, maintenance, "CONSERVATIVE_BOUND", max_notional=max_notional)
        for strategy_id, (initial, maintenance) in zip(ids, rates)
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
    assert result.reason == "BANK_UNAVAILABLE"


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


def test_priority_is_close_stress_order_not_an_entry_queue() -> None:
    members = (
        {"strategy_id": "slow", "mean_hold": "10", "hold90": "10", "mean_net_pnl": "10"},
        {"strategy_id": "fast", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "3"},
    )
    priorities = derive_priorities(members)
    assert priorities["fast"] == 1 and priorities["slow"] == 2


def test_priority_uses_numeric_strategy_ties_and_does_not_average_partial_cycles() -> None:
    members = (
        {"strategy_id": 10, "cycles": ({"duration_seconds": Decimal("3600"), "net_pnl": Decimal("2")},)},
        {"strategy_id": 2, "cycles": ({"duration_seconds": Decimal("3600"), "net_pnl": Decimal("2")},)},
        {"strategy_id": 1, "cycles": ({"duration_seconds": Decimal("3600"),}, {"duration_seconds": Decimal("3600"), "net_pnl": Decimal("4")})},
    )
    details = __import__("mrs3.portfolio.weighted_search", fromlist=["priority_details"]).priority_details(members)
    assert details[2]["priority"] == details[10]["priority"] == 1
    assert details[1]["slot_score"] is None


def test_limiter_replay_releases_before_equal_timestamp_starts_and_skips_without_queue() -> None:
    result = replay_limiter(
        (
            {"cycle_id": "A", "strategy_id": "A", "first_fill": 0, "final_flat": 2, "common_window_equity": "10", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
            {"cycle_id": "B", "strategy_id": "B", "first_fill": 1, "final_flat": 3, "common_window_equity": "20", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
            {"cycle_id": "C", "strategy_id": "C", "first_fill": 2, "final_flat": 4, "common_window_equity": "30", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
        ),
        1,
        common_days=Decimal("1"),
    )
    assert result.status == "MODEL"
    assert result.accepted_cycle_ids == ("A", "C") and result.rejected_cycle_ids == ("B",)
    assert result.p30_limiter == Decimal("1200")


def test_limiter_replay_requires_attribution_and_supports_known_carry_in() -> None:
    missing = replay_limiter(({"cycle_id": "A", "strategy_id": "A", "first_fill": 0, "final_flat": 2, "net_pnl": "10"},), 1, common_days=Decimal("1"))
    assert missing.status == "UNKNOWN"
    known = replay_limiter((
        {"cycle_id": "A", "strategy_id": "A", "first_fill": 0, "final_flat": 2, "common_window_equity": "10", "carry_in_count": 1, "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
    ), 1, common_days=Decimal("1"), period_start=0, period_end=2)
    assert known.status == "MODEL" and known.accepted_cycle_ids == ("A",)


def test_limiter_replay_does_not_reuse_raw_realized_pnl_without_normalized_contribution() -> None:
    result = replay_limiter((
        {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "net_pnl": "10", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
    ), 1, common_days=Decimal("1"))
    assert result.status == "UNKNOWN"


def test_limiter_replay_l0_requires_and_verifies_common_p30() -> None:
    cycle = {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "common_window_equity": "10", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"}
    assert replay_limiter((cycle,), 0, common_days=Decimal("1"), common_p30=Decimal("300")).status == "MODEL"
    assert replay_limiter((cycle,), 0, common_days=Decimal("1"), common_p30=Decimal("301")).status == "UNKNOWN"


def test_limiter_replay_equal_start_uses_numeric_strategy_id_without_queue() -> None:
    def cycle(cycle_id, strategy_id, pnl):
        return {"cycle_id": cycle_id, "strategy_id": strategy_id, "first_fill": 0, "final_flat": 2, "common_window_equity": pnl, "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"}

    result = replay_limiter((cycle("ten", 10, "10"), cycle("two", 2, "20")), 1, common_days=Decimal("1"))
    assert result.accepted_cycle_ids == ("two",) and result.rejected_cycle_ids == ("ten",)


def test_limiter_replay_normalizes_timestamp_offsets_and_keeps_right_edge_open_occupied() -> None:
    result = replay_limiter((
        {"cycle_id": "B", "strategy_id": 2, "first_fill": "2026-01-01T01:00:00+01:00", "final_flat": None, "common_window_equity": "20", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
        {"cycle_id": "A", "strategy_id": 1, "first_fill": "2026-01-01T00:00:00Z", "final_flat": "2026-01-01T00:30:00Z", "common_window_equity": "10", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
        {"cycle_id": "C", "strategy_id": 3, "first_fill": "2026-01-01T01:30:00Z", "final_flat": "2026-01-01T01:45:00Z", "common_window_equity": "30", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
    ), 1, common_days=Decimal("1"), period_start="2026-01-01T00:00:00Z", period_end="2026-01-01T02:00:00Z")
    assert result.status == "MODEL" and result.accepted_cycle_ids == ("A", "C") and result.rejected_cycle_ids == ("B",)


def test_priority_uses_effective_hold_and_selected_x_with_duration_seconds_and_normalized_pnl() -> None:
    details = __import__("mrs3.portfolio.weighted_search", fromlist=["priority_details"]).priority_details(
        ({"strategy_id": 7, "cycles": (
            {"duration_seconds": "3600", "normalized_pnl": "10"},
            {"duration_seconds": "7200", "normalized_pnl": "20"},
        )},),
        {7: "2"},
    )
    assert details[7]["T_eff"] == Decimal("1.75")
    assert details[7]["mean_net_pnl"] == Decimal("30")
    assert details[7]["slot_score"] == Decimal(30) / Decimal("1.75")
    assert __import__("mrs3.portfolio.weighted_search", fromlist=["priority_details"]).priority_details(({"strategy_id": 7, "duration_seconds": "3600", "normalized_pnl": "10"},))[7]["slot_score"] is None


def test_priority_marks_incomplete_cycle_evidence_unknown_and_keeps_nonpositive_last() -> None:
    priorities = derive_priorities((
        {"strategy_id": 1, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "10"},
        {"strategy_id": 2, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "0"},
        {"strategy_id": 3, "cycles": ({"duration_seconds": "3600", "normalized_pnl": "1"}, {"duration_seconds": "3600"})},
    ))
    assert priorities[1] == 1 and priorities[2] == 2 and priorities[3] == 2
    assert derive_priorities(({"strategy_id": 4, "mean_hold": "1", "hold90": "1"},))[4] == 1


def test_priority_creates_no_more_than_five_groups_and_uses_half_group_max() -> None:
    members = tuple({"strategy_id": index, "mean_hold": "1", "hold90": "1", "mean_net_pnl": str(score)} for index, score in enumerate((100, 49, 24, 11, 5, 2), 1))
    priorities = derive_priorities(members)
    assert [priorities[index] for index in range(1, 7)] == [1, 2, 3, 4, 5, 5]


def test_priority_mixed_absolute_and_normalized_units_fails_closed() -> None:
    members = (
        {"strategy_id": 1, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "10"},
        {"strategy_id": 2, "mean_hold": "1", "hold90": "1", "mean_normalized_pnl": "10"},
    )
    assert derive_priorities(members, (Decimal("1"), Decimal("1"))) == {1: None, 2: None}


def test_limiter_replay_rejects_mixed_timestamp_types_and_missing_complete_attribution() -> None:
    base = {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "common_window_equity": "10"}
    mixed = replay_limiter((base, {**base, "cycle_id": "B", "strategy_id": 2, "first_fill": "1970-01-01T00:00:01Z", "final_flat": "1970-01-01T00:00:02Z", "attribution_complete": True}), 1, common_days=Decimal("1"))
    missing = replay_limiter((base,), 1, common_days=Decimal("1"))
    assert mixed.status == missing.status == "UNKNOWN"


def test_limiter_replay_carry_in_overflow_and_open_right_edge_fail_closed() -> None:
    carry = replay_limiter(({
        "cycle_id": "A", "strategy_id": 1, "first_fill": -1, "final_flat": 2,
        "carry_in_count": 2, "common_window_equity": "10", "attribution_complete": True,
    },), 1, common_days=Decimal("1"), period_start=0, period_end=2)
    open_without_edge = replay_limiter(({
        "cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": None,
        "common_window_equity": "10", "attribution_complete": True,
    },), 1, common_days=Decimal("1"))
    assert carry.status == open_without_edge.status == "UNKNOWN"


def test_limiter_replay_uses_ordinal_then_cycle_id_and_records_simultaneous_conflict() -> None:
    def cycle(cycle_id, ordinal, strategy_id):
        return {"cycle_id": cycle_id, "source_ordinal": ordinal, "strategy_id": strategy_id, "first_fill": 0, "final_flat": 2, "common_window_equity": "10", "attribution_complete": True}

    result = replay_limiter((cycle("later", 2, 10), cycle("first", 1, 2)), 1, common_days=Decimal("1"))
    assert result.accepted_cycle_ids == ("first",) and result.rejected_cycle_ids == ("later",)
    assert result.witness["simultaneous_conflict_count"] == 1
    assert result.witness["simultaneous_conflicts"][0]["rejected_cycle_ids"] == ("later",)


def test_limiter_replay_numeric_ids_precede_lexical_ids_and_priority_never_changes_admission() -> None:
    def cycle(cycle_id, strategy_id, priority):
        return {"cycle_id": cycle_id, "strategy_id": strategy_id, "priority": priority, "first_fill": 0, "final_flat": 2, "common_window_equity": "10", "attribution_complete": True}

    numeric = replay_limiter((cycle("lexical", "2", 1), cycle("numeric", 10, 5)), 1, common_days=Decimal("1"))
    assert numeric.accepted_cycle_ids == ("numeric",)
    first = replay_limiter((cycle("A", 10, 5), cycle("B", 2, 1)), 1, common_days=Decimal("1"))
    reversed_priorities = replay_limiter((cycle("A", 10, 1), cycle("B", 2, 5)), 1, common_days=Decimal("1"))
    assert first.accepted_cycle_ids == reversed_priorities.accepted_cycle_ids == ("B",)


def test_limiter_replay_off_requires_exact_common_p30_and_rejects_alias_or_raw_pnl() -> None:
    cycle = {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "common_window_equity": "10", "attribution_complete": True}
    mismatch = replay_limiter((cycle,), 0, common_days=Decimal("1"), common_p30=Decimal("300.00001"))
    alias = replay_limiter(({**cycle, "common_window_equity": None, "equity_contribution": "10"},), 1, common_days=Decimal("1"))
    raw = replay_limiter(({**cycle, "common_window_equity": None, "realized_pnl": "10", "fees": "1"},), 1, common_days=Decimal("1"))
    assert mismatch.status == alias.status == raw.status == "UNKNOWN"


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


def _margin15_fixture():
    x = (Decimal("1000"),) * 15
    members = tuple({"strategy_id": index, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for index in range(15))
    coefficients = _evidence_coefficients(((Decimal("0.10"), Decimal("0.005")),) * 15, range(15), max_notional=Decimal("1000"))
    cycles = tuple({
        "cycle_id": index, "strategy_id": index, "first_fill": index * 2,
        "final_flat": index * 2 + 1, "common_window_equity": "1",
        "attribution_complete": True,
    } for index in range(15))
    options = {
        "max_dd": Decimal("0.10"), "reserve": Decimal("0.40"),
        "max_mm_load": Decimal("0.35"),
        "priorities": {index: 1 for index in range(15)},
        "strategy_ids": tuple(range(15)), "limiter_release_status": "CONFIRMED",
        "release_evidence": {"digest": "fixture", "identity": "fixture"},
        "cycles": cycles, "common_days": Decimal("1"), "common_p30": Decimal("450"),
    }
    return x, members, coefficients, options


def test_margin_variants_check_every_strict_l_before_bank_rejection() -> None:
    x, members, coefficients, options = _margin15_fixture()
    primary, variants = _margin_variant(x, members, coefficients, options=options, bank_available=Decimal("2000"))
    assert primary is not None and primary.L == 10
    assert tuple(item.L for item in variants) == tuple(range(15))
    assert variants[0].status == "FAIL" and variants[10].status == "PASS"
    assert all(item.L != 15 for item in variants)


def test_margin_replay_is_compact_and_model_order_uses_replayed_p30() -> None:
    x, members, coefficients, options = _margin15_fixture()
    _primary, variants = _margin_variant(x, members, coefficients, options=options, bank_available=Decimal("2000"))
    assert all(item.p30_status == "MODEL" for item in variants)
    assert variants[10].p30 == Decimal("450")
    compact = {"L": variants[10].L, "p30": variants[10].p30, "mask": variants[10].replay.accepted_mask}
    assert "cycle_id" not in compact and "cycles" not in compact


def test_unknown_fixed_bank_order_is_off_then_strict_l_descending() -> None:
    x, members, coefficients, options = _margin15_fixture()
    options = {key: value for key, value in options.items() if key not in {"cycles", "common_p30"}}
    _primary, variants = _margin_variant(x, members, coefficients, options=options, bank_available=Decimal("2000"))
    ordered = _ordered_margin_variants(variants, bank_available=Decimal("2000"))
    assert all(item.p30_status == "UNKNOWN" for item in ordered)
    assert [item.L for item in ordered] == list(range(10, 0, -1))


def test_margin_candidate_recomputes_priority_and_identity_for_supplied_x() -> None:
    members = (
        {"strategy_id": 1, "symbol": "A", "mean_hold": "1", "hold90": "1", "mean_normalized_pnl": "1"},
        {"strategy_id": 2, "symbol": "B", "mean_hold": "1", "hold90": "1", "mean_normalized_pnl": "1"},
    )
    common = dict(max_dd=Decimal("0.2"), common_days=Decimal("1"), target=None, profile_id="P", scenario_id="S", margin_coefficients=_evidence_coefficients(((Decimal("0.1"), Decimal("0.01")),) * 2), margin_kwargs={"limiter_release_status": "UNKNOWN"})
    first = _candidates_for_solution(_Solution(Decimal("1"), (Decimal("10"), Decimal("1"))), ((Decimal("0"), Decimal("0")),), members, (Decimal("100"), Decimal("100")), **common)[0]
    second = _candidates_for_solution(_Solution(Decimal("1"), (Decimal("1"), Decimal("10"))), ((Decimal("0"), Decimal("0")),), members, (Decimal("100"), Decimal("100")), **common)[0]
    assert [member["priority"] for member in first.members] == [1, 2]
    assert [member["priority"] for member in second.members] == [2, 1]
    assert first.identity != second.identity
    assert all(key not in first.metrics for key in ("cycles", "equity", "actions"))


def test_path_bank_dominates_margin_risk_and_fixed_bank_eligibility() -> None:
    members = (
        {"strategy_id": 1, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
        {"strategy_id": 2, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
    )
    candidate = _candidates_for_solution(
        _Solution(Decimal("1800"), (Decimal("100"), Decimal("100"))),
        ((Decimal("1"), Decimal("1")), (Decimal("-1"), Decimal("-1"))),
        members,
        (Decimal("100"), Decimal("100")),
        max_dd=Decimal("0.10"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S", margin_coefficients=_evidence_coefficients(((Decimal("0.10"), Decimal("0.01")),) * 2),
        margin_kwargs={"limiter_release_status": "UNKNOWN"},
        bank_available=Decimal("1800"),
    )[0]
    assert candidate.metrics["bank_for_path_usdt"] == Decimal("1800")
    assert candidate.metrics["B_required_margin_usdt"] == Decimal("1800")
    assert candidate.metrics["bank_feasible"] is True


def test_margin_option_alias_is_rejected_before_l_evaluation() -> None:
    x, members, coefficients, options = _margin15_fixture()
    options = {**options, "m": Decimal("0.1")}
    with pytest.raises(ValueError, match="MARGIN_OPTION_UNKNOWN"):
        _margin_variant(x, members, coefficients, options=options, bank_available=Decimal("2000"))


def test_weighted_search_blocks_bare_or_unknown_margin_coefficients_but_accepts_frozen_evidence() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    members = tuple({**member, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for member in _members())
    kwargs = {"limiter_release_status": "UNKNOWN"}
    bare = weighted_search(prepared, (Decimal("100"), Decimal("100")), members=members, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"), margin_coefficients=((Decimal("0.1"), Decimal("0.01")),) * 2, margin_kwargs=kwargs)
    unknown = weighted_search(prepared, (Decimal("100"), Decimal("100")), members=members, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"), margin_coefficients=MarginCoefficientResult("UNKNOWN", (), "UNKNOWN"), margin_kwargs=kwargs)
    valid = MarginCoefficientResult("PASS", tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", {"declared_state_count": 1, "evaluated_state_count": 1}, max_notional=Decimal("100")) for index in range(2)), "CONSERVATIVE_BOUND", witness={"declared_state_count": 2, "evaluated_state_count": 2})
    accepted = weighted_search(prepared, (Decimal("100"), Decimal("100")), members=members, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"), margin_coefficients=valid, margin_kwargs=kwargs)
    assert bare.status == unknown.status == "FAIL"
    assert accepted.status == "PASS"


def test_domainless_margin_evidence_fails_at_public_boundaries() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    domainless = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND") for index in range(2))
    blocked = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=_members(),
        target_p30=Decimal("10"),
        margin_coefficients=domainless,
    )
    from mrs3.portfolio.margin import evaluate_weighted_margin
    mapped_none = evaluate_weighted_margin(
        (Decimal("10"),),
        {1: {"a": Decimal("0.1"), "b": Decimal("0.01"), "evidence_class": "CONSERVATIVE_BOUND", "max_notional": None}},
        strategy_ids=(1,),
        priorities=(1,),
    )
    assert blocked.status == "FAIL" and blocked.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert mapped_none.status == "UNKNOWN" and mapped_none.reason == "MARGIN_BOUND_UNAVAILABLE"


def test_margin_risk_and_bank_options_are_rejected_as_user_overrides() -> None:
    x, members, coefficients, options = _margin15_fixture()
    for key in ("B_risk", "bank_available"):
        with pytest.raises(ValueError, match="MARGIN_OPTION_UNKNOWN"):
            _margin_variant(x, members, coefficients, options={**options, key: Decimal("1")}, bank_available=Decimal("2000"))


def test_margin_strategy_and_priority_shapes_are_exact() -> None:
    x, members, coefficients, options = _margin15_fixture()
    with pytest.raises(ValueError, match="MARGIN_STRATEGY_SHAPE_MISMATCH"):
        _margin_variant(x, members, coefficients, options={**options, "strategy_ids": tuple(reversed(range(15)))}, bank_available=Decimal("2000"))
    with pytest.raises(ValueError, match="PRIORITY_SHAPE_MISMATCH"):
        _margin_variant(x, members, coefficients, options={**options, "priorities": (1,)}, bank_available=Decimal("2000"))


def test_frontier_preserves_real_candidate_before_domain_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared = _prepared((("1", "1"), ("1", "1")))
    members = tuple({**member, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for member in _members())
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("200")) for index in range(2))
    solutions = iter((
        weighted_search_module._SolveOutcome("PASS", _Solution(Decimal("1"), (Decimal("10"), Decimal("10")))),
        weighted_search_module._SolveOutcome("PASS", _Solution(Decimal("1"), (Decimal("201"), Decimal("10")))),
    ))
    monkeypatch.setattr(weighted_search_module, "_solve_lp", lambda *args, **kwargs: next(solutions))
    result = weighted_search(
        prepared,
        (Decimal("200"), Decimal("200")),
        members=members,
        common_days=Decimal("1"),
        max_targets=2,
        margin_coefficients=coefficients,
        margin_kwargs={"limiter_release_status": "UNKNOWN"},
    )
    assert result.status == "PASS" and len(result.candidates) == 1
    assert result.warnings == ("MARGIN_COEFFICIENT_DOMAIN_EXCEEDED",)


def test_priority_unknown_is_reported_at_margin_boundary() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    members = (
        {"strategy_id": 1, "symbol": "S0", "side": "LONG", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
        {"strategy_id": 2, "symbol": "S1", "side": "LONG", "mean_hold": "1", "hold90": "1", "mean_normalized_pnl": "1"},
    )
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        target_p30=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"limiter_release_status": "UNKNOWN"},
    )
    assert result.status == "FAIL" and result.reason == "PRIORITY_UNKNOWN"


def test_margin_arguments_without_coefficients_fail_closed() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=_members(),
        target_p30=Decimal("10"),
        margin_kwargs={"limiter_release_status": "UNKNOWN"},
    )
    assert result.status == "FAIL" and result.reason == "MARGIN_BOUND_UNAVAILABLE"


def test_bundled_margin_and_separate_margin_arguments_conflict() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    with pytest.raises(ValueError, match="MARGIN_OPTION_CONFLICT"):
        weighted_search(
            prepared,
            (Decimal("100"), Decimal("100")),
            members=_members(),
            target_p30=Decimal("10"),
            margin={"coefficients": coefficients, "limiter_release_status": "UNKNOWN"},
            margin_coefficients=coefficients,
        )


def test_bundled_margin_applies_coefficients_and_options_to_candidate_metrics() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=tuple({**member, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for member in _members()),
        target_p30=Decimal("10"),
        margin={"coefficients": coefficients, "limiter_release_status": "UNKNOWN"},
    )
    assert result.status == "PASS"
    metrics = result.candidates[0].metrics
    assert all(metrics[key] is not None for key in ("I_all_usdt", "M_all_usdt", "I_held_usdt", "B_margin_usdt", "B_required_margin_usdt"))


def test_sequence_priorities_are_published_in_strategy_id_order() -> None:
    members = (
        {"strategy_id": 1, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
        {"strategy_id": 2, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
    )
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    candidate = _candidates_for_solution(
        _Solution(Decimal("1"), (Decimal("10"), Decimal("10"))),
        ((Decimal("0"), Decimal("0")),),
        members,
        (Decimal("100"), Decimal("100")),
        max_dd=Decimal("0.2"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S", margin_coefficients=coefficients,
        margin_kwargs={"strategy_ids": (1, 2), "priorities": (5, 1), "limiter_release_status": "UNKNOWN"},
    )[0]
    assert [member["priority"] for member in candidate.members] == [5, 1]


def test_public_weighted_margin_rejects_bare_coefficients_and_domain_overflow() -> None:
    from mrs3.portfolio.margin import evaluate_weighted_margin

    bare = evaluate_weighted_margin((Decimal("10"),), ((Decimal("0.1"), Decimal("0.01")),), strategy_ids=(1,), priorities=(1,))
    overflow = evaluate_weighted_margin(
        (Decimal("101"),),
        (MarginCoefficient(1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")),),
        strategy_ids=(1,), priorities=(1,),
    )
    assert bare.status == "UNKNOWN" and bare.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert overflow.status == "UNKNOWN" and overflow.reason == "MARGIN_COEFFICIENT_DOMAIN_EXCEEDED"


def test_explicit_l_equal_n_is_rejected() -> None:
    x, members, coefficients, options = _margin15_fixture()
    with pytest.raises(ValueError, match="LIMITER_RANGE_INVALID"):
        _margin_variant(x, members, coefficients, options={**options, "L": 15}, bank_available=Decimal("2000"))


def test_margin_infeasible_later_frontier_keeps_prior_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    member_rows = _members()
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    first = _candidate_for_solution(
        _Solution(Decimal("1"), (Decimal("1"), Decimal("1"))),
        ((Decimal("0"), Decimal("0")),), member_rows, (Decimal("100"), Decimal("100")),
        max_dd=Decimal("0.2"), common_days=Decimal("1"), target=None, profile_id="P", scenario_id="S",
    )
    calls = 0

    def candidates(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            _kwargs["failure_reason"].append("MARGIN_BOUND_FAILED")
        return (first,) if calls == 1 else ()

    monkeypatch.setattr(weighted_search_module, "_candidates_for_solution", candidates)
    result = weighted_search(prepared, (Decimal("100"), Decimal("100")), members=member_rows, common_days=Decimal("1"), max_targets=2, margin_coefficients=coefficients, margin_kwargs={"limiter_release_status": "UNKNOWN"})
    assert result.status == "PASS" and result.candidates == (first,)
    assert result.warnings and result.warnings[-1] == "MARGIN_BOUND_FAILED"


def _rescue_fixture():
    members = (
        {"strategy_id": 1, "symbol": "A", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
        {"strategy_id": 2, "symbol": "B", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
    )
    return {
        "max_dd": Decimal("0.10"), "reserve": Decimal("0.40"),
        "max_mm_load": Decimal("0.35"),
        "limiter_release_status": "UNKNOWN",
    }, members, _evidence_coefficients(((Decimal("0.10"), Decimal("0.005")),) * 2, max_notional=Decimal("20000"))


def test_proportional_rescue_appends_a_distinct_recomputed_candidate() -> None:
    options, members, coefficients = _rescue_fixture()
    candidates = _candidates_for_solution(
        _Solution(Decimal("50000"), (Decimal("10000"), Decimal("10000"))),
        ((Decimal("0"), Decimal("0")),), members, (Decimal("20000"), Decimal("20000")),
        max_dd=Decimal("0.10"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S", margin_coefficients=coefficients,
        margin_kwargs=options, bank_available=Decimal("2000"),
    )
    assert len(candidates) == 2
    original, reduced = candidates
    assert tuple(member["x_usdt"] for member in original.members) == (Decimal("10000"), Decimal("10000"))
    assert all(Decimal("0") <= member["x_usdt"] <= Decimal("20000") for member in reduced.members)
    assert reduced.metrics["required_bank_usdt"] <= Decimal("2000")
    assert original.metrics["bank_feasible"] is False and reduced.metrics["bank_feasible"] is True
    assert reduced.identity != original.identity
    assert "replay_accepted_mask" not in reduced.metrics
    assert all("replay_accepted_mask" not in item for item in reduced.metrics["limiter_variants"] if item is not None)


def test_failed_proportional_rescue_keeps_only_the_original_candidate() -> None:
    options, members, coefficients = _rescue_fixture()
    candidates = _candidates_for_solution(
        _Solution(Decimal("50000"), (Decimal("10000"), Decimal("10000"))),
        ((Decimal("0"), Decimal("0")),), members, (Decimal("20000"), Decimal("20000")),
        max_dd=Decimal("0.10"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S", margin_coefficients=coefficients,
        margin_kwargs=options, bank_available=Decimal("1.1"),
    )
    assert len(candidates) == 1
    assert tuple(member["x_usdt"] for member in candidates[0].members) == (Decimal("10000"), Decimal("10000"))
    assert candidates[0].metrics["bank_feasible"] is False


def test_proposal_revalidation_calls_validator_once_and_preserves_on_failure() -> None:
    members = ({"strategy_id": 1, "symbol": "A"}, {"strategy_id": 2, "symbol": "B"})
    original = _candidate_for_solution(
        _Solution(Decimal("1"), (Decimal("1"), Decimal("1"))),
        ((Decimal("0"), Decimal("0")),), members, (Decimal("10"), Decimal("10")),
        max_dd=Decimal("0.2"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S",
    )
    calls: list[tuple[Decimal, ...]] = []
    accepted = __import__("mrs3.portfolio.weighted_search", fromlist=["_revalidate_proposed_x"])._revalidate_proposed_x(
        original, (Decimal("2"), Decimal("1")), ((Decimal("0"), Decimal("0")),), members,
        (Decimal("10"), Decimal("10")), max_dd=Decimal("0.2"), common_days=Decimal("1"),
        target=None, profile_id="P", scenario_id="S", validator=lambda value: calls.append(value) or True,
    )
    assert len(accepted) == 2 and len(calls) == 1
    assert all(key not in accepted[-1].metrics for key in ("cycles", "equity", "actions"))
    rejected_calls: list[tuple[Decimal, ...]] = []
    rejected = __import__("mrs3.portfolio.weighted_search", fromlist=["_revalidate_proposed_x"])._revalidate_proposed_x(
        original, (Decimal("2"), Decimal("1")), ((Decimal("0"), Decimal("0")),), members,
        (Decimal("10"), Decimal("10")), max_dd=Decimal("0.2"), common_days=Decimal("1"),
        target=None, profile_id="P", scenario_id="S", validator=lambda value: rejected_calls.append(value) or False,
    )
    assert rejected == (original,) and len(rejected_calls) == 1
