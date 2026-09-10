from decimal import Decimal
from types import MappingProxyType

import pytest

from mrs3.portfolio.candidate_search import search_pretest_proxy
from mrs3.portfolio.pretest_proxy import PretestProxyError, compute_proxy_metrics, scale_equity_path


def _path(values):
    return tuple({"timestamp_utc": f"2026-01-01T{index:02d}:00:00Z", "equity": Decimal(str(value))} for index, value in enumerate(values))


def _row(symbol, side="LONG", strategy_id=1):
    return {
        "user_status": "FINALIST",
        "symbol": symbol,
        "side": side,
        "strategy_id": strategy_id,
        "result_id": strategy_id,
        "total_pnl": Decimal("1"),
        "initial_balance": Decimal("100"),
        "position_size_usdt": Decimal("100"),
        "strategy_orders": ({"order_id": 1, "lot_x": Decimal("1")},),
        "equity": _path((100, 120, 90, 100)),
    }


def test_proxy_scales_increment_and_uses_peak_for_dd_percentage():
    result = compute_proxy_metrics(_path((100, 120, 90, 100)), campaign_equity=100)

    assert result.end_pnl_usdt == Decimal("0.00000000")
    assert result.max_drawdown_usdt == Decimal("30.00000000")
    assert result.max_drawdown_pct == Decimal("25.00000000")

    scaled = scale_equity_path(_path((100, 120)), source_initial_balance=100, tested_size_usdt=100, actual_size_usdt=50, campaign_equity=100)
    assert scaled[-1]["scaled_increment"] == Decimal("10.00000000")
    assert scaled[-1]["tested_size_basis"] == "SOURCE_INITIAL_BALANCE_X_OPENING_LOT"
    assert result.as_dict()["joint_metrics"] == "NOT_TESTED"


def test_proxy_accepts_float64_like_daily_values_and_preserves_decimal_money():
    result = compute_proxy_metrics(
        (("2026-01-01T00:00:00Z", 100.0), ("2026-01-02T00:00:00Z", 101.5)),
        campaign_equity=100.0,
    )

    assert isinstance(result.end_pnl_usdt, Decimal)
    assert result.end_pnl_usdt == Decimal("1.50000000")


def test_proxy_rejects_non_increasing_timestamps():
    with pytest.raises(PretestProxyError):
        compute_proxy_metrics((
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": 100},
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": 101},
        ), campaign_equity=100)


def test_proxy_drawdown_and_reserve_include_campaign_starting_equity():
    result = compute_proxy_metrics(_path((90, 95)), campaign_equity=100)

    assert result.max_drawdown_usdt == Decimal("10.00000000")
    assert result.max_drawdown_pct == Decimal("10.00000000")
    assert result.reserve_usdt == Decimal("90.00000000")


def test_pretest_budget_is_mandatory_before_evaluation():
    calls = []
    result = search_pretest_proxy(
        [_row("A"), _row("B", strategy_id=2)],
        selected_symbols=("A", "B"),
        profile_id="BALANCED",
        scenario_id="fixture",
        max_candidates=2,
        max_enumerated_combinations=2,
        evaluator=lambda members: calls.append(members) or {"status": "PASS"},
    )

    assert result.reason == "PRETEST_BUDGET_TOO_SMALL"
    assert result.required_budget == 3
    assert calls == []


def _search_metrics(members):
    ids = frozenset(member["strategy_id"] for member in members)
    # Make the otherwise late B-C-D branch the best parent and winner.  A
    # canonical product walk reaches B-C-D only after A-B-C/A-B-D/A-C-D.
    recovery = Decimal("100") if ids == frozenset({2, 3, 4}) else (Decimal("10") if ids == frozenset({2, 3}) else Decimal("1"))
    pnl = Decimal("100") if ids == frozenset({2, 3, 4}) else Decimal("1")
    return {
        "status": "PASS",
        "proxy_pnl_usdt": pnl,
        "proxy_recovery_factor": recovery,
        "proxy_reserve_usdt": Decimal("1"),
        "proxy_max_drawdown_pct": Decimal("1"),
    }


def _process_search_metrics(members, _context):
    return _search_metrics(members)


def test_pretest_beam_reaches_late_frontier_winner_with_bounded_budget():
    rows = [_row(symbol, strategy_id=index) for index, symbol in enumerate(("A", "B", "C", "D"), 1)]

    result = search_pretest_proxy(
        rows,
        selected_symbols=("A", "B", "C", "D"),
        profile_id="BALANCED",
        scenario_id="fixture",
        max_candidates=1,
        max_enumerated_combinations=12,
        evaluator=_search_metrics,
    )

    assert result.status == "PASS"
    assert result.evaluated == 12
    assert {member["strategy_id"] for member in result.candidates[0].members} == {2, 3, 4}


def test_pretest_frontier_is_deterministic_when_rows_are_shuffled():
    rows = [_row(symbol, strategy_id=index) for index, symbol in enumerate(("A", "B", "C", "D"), 1)]
    first = search_pretest_proxy(rows, selected_symbols=("A", "B", "C", "D"), profile_id="BALANCED", scenario_id="fixture", max_candidates=2, max_enumerated_combinations=20, evaluator=_search_metrics)
    second = search_pretest_proxy(list(reversed(rows)), selected_symbols=("A", "B", "C", "D"), profile_id="BALANCED", scenario_id="fixture", max_candidates=2, max_enumerated_combinations=20, evaluator=_search_metrics)

    assert first.evaluated == second.evaluated
    assert [item.identity for item in first.candidates] == [item.identity for item in second.candidates]


def test_pretest_process_batch_matches_serial_results():
    rows = [_row(symbol, strategy_id=index) for index, symbol in enumerate(("A", "B", "C"), 1)]
    common = dict(
        selected_symbols=("A", "B", "C"), profile_id="BALANCED", scenario_id="fixture",
        max_candidates=2, max_enumerated_combinations=10,
    )
    serial = search_pretest_proxy(rows, **common, evaluator=_search_metrics)
    parallel = search_pretest_proxy(
        rows, **common, evaluator=_search_metrics,
        process_evaluator=_process_search_metrics,
        process_context=MappingProxyType({"captured": (MappingProxyType({"value": 1}),)}),
        workers=2,
    )
    assert parallel.evaluated == serial.evaluated
    assert [item.identity for item in parallel.candidates] == [item.identity for item in serial.candidates]
    assert [dict(item.metrics) for item in parallel.candidates] == [dict(item.metrics) for item in serial.candidates]


def test_pretest_custom_evaluator_stays_serial_with_worker_width():
    calls = []

    def custom(members):
        calls.append(tuple(member["strategy_id"] for member in members))
        return {"status": "PASS", "proxy_pnl_usdt": Decimal("1")}

    result = search_pretest_proxy(
        [_row("A"), _row("B", strategy_id=2)],
        selected_symbols=("A", "B"), profile_id="BALANCED", scenario_id="fixture",
        max_candidates=1, max_enumerated_combinations=3, evaluator=custom, workers=2,
    )

    assert len(calls) == result.evaluated == 3


def test_pretest_keeps_high_individual_drawdown_and_ignores_legacy_top_n_fields():
    row = _row("A")
    row["max_drawdown_pct"] = Decimal("999")

    result = search_pretest_proxy(
        (row,), selected_symbols=("A",), profile_id="BALANCED", scenario_id="fixture",
        max_candidates=1, max_enumerated_combinations=1, evaluator=lambda members: {"status": "PASS"},
    )

    assert result.status == "PASS"
    assert result.candidates[0].members[0]["strategy_id"] == 1


def test_pretest_reaches_late_swap_neighbor_before_canonical_product_order():
    rows = [_row(symbol, strategy_id=index) for index, symbol in enumerate(("A", "B", "C", "D"), 1)]

    def evaluator(members):
        ids = frozenset(member["strategy_id"] for member in members)
        return {
            "status": "PASS",
            "proxy_pnl_usdt": Decimal("100") if ids == frozenset({2, 3, 4}) else Decimal("1"),
            "proxy_recovery_factor": Decimal("100") if ids == frozenset({2, 3, 4}) else (Decimal("10") if ids == frozenset({1, 2}) else Decimal("1")),
            "proxy_reserve_usdt": Decimal("1"),
            "proxy_max_drawdown_pct": Decimal("1"),
        }

    result = search_pretest_proxy(
        rows, selected_symbols=("A", "B", "C", "D"), profile_id="BALANCED", scenario_id="fixture",
        max_candidates=1, max_enumerated_combinations=12, evaluator=evaluator,
    )

    assert result.status == "PASS"
    assert {member["strategy_id"] for member in result.candidates[0].members} == {2, 3, 4}


def test_pretest_reaches_replacement_neighbor_within_remaining_budget():
    rows = [
        _row("A", strategy_id=1), _row("A", strategy_id=2),
        _row("B", strategy_id=3), _row("C", strategy_id=4),
    ]

    def evaluator(members):
        ids = frozenset(member["strategy_id"] for member in members)
        return {
            "status": "PASS",
            "proxy_pnl_usdt": Decimal("100") if ids == frozenset({2, 3, 4}) else Decimal("1"),
            "proxy_recovery_factor": Decimal("100") if ids == frozenset({2, 3, 4}) else (Decimal("10") if ids == frozenset({1, 3}) else Decimal("1")),
            "proxy_reserve_usdt": Decimal("1"),
            "proxy_max_drawdown_pct": Decimal("1"),
        }

    result = search_pretest_proxy(
        rows, selected_symbols=("A", "B", "C"), profile_id="BALANCED", scenario_id="fixture",
        max_candidates=1, max_enumerated_combinations=11, evaluator=evaluator,
    )

    assert result.status == "PASS"
    assert {member["strategy_id"] for member in result.candidates[0].members} == {2, 3, 4}


def test_pretest_large_mandatory_frontier_keeps_evaluation_and_selection_bounded():
    rows = [_row(symbol, strategy_id=offset + index) for offset, symbol in ((0, "A"), (1000, "B"), (2000, "C")) for index in range(40)]
    calls = []

    def evaluator(members):
        calls.append(tuple(member["strategy_id"] for member in members))
        return {"status": "PASS", "proxy_pnl_usdt": Decimal("1"), "proxy_recovery_factor": Decimal("1"), "proxy_reserve_usdt": Decimal("1"), "proxy_max_drawdown_pct": Decimal("1")}

    result = search_pretest_proxy(
        rows, selected_symbols=("A", "B", "C"), profile_id="BALANCED", scenario_id="fixture",
        max_candidates=5, max_enumerated_combinations=4920, evaluator=evaluator,
    )

    assert result.required_budget == 4920
    assert len(calls) == result.evaluated == 4920
    assert len(result.candidates) == 5


def test_pretest_search_compacts_candidate_payload_and_keeps_batch_order():
    rows = [_row("A"), _row("B", strategy_id=2)]
    rows[0]["actions"] = ({"timestamp_utc": "unused"},)
    rows[0]["minute_equity"] = _path((100, 101))

    def batch(tasks):
        # Return completion order backwards; generation order remains canonical.
        return tuple((index, {"status": "PASS", "proxy_pnl_usdt": Decimal(index)}) for index, _ in reversed(tasks))

    result = search_pretest_proxy(
        rows,
        selected_symbols=("A", "B"),
        profile_id="BALANCED",
        scenario_id="fixture",
        max_candidates=2,
        max_enumerated_combinations=3,
        batch_evaluator=batch,
    )

    assert result.evaluated == 3
    assert all("equity" not in member and "actions" not in member and "minute_equity" not in member for candidate in result.candidates for member in candidate.members)
    assert all("equity_path" not in candidate.metrics for candidate in result.candidates)
