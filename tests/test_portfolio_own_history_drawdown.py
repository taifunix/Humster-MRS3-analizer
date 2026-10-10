"""Own-history drawdown cap (ADR-0069)."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import importlib

import pytest

from mrs3.portfolio.input import PreparedWeightedInput, prepare_weighted_input
from mrs3.portfolio.margin import MarginCoefficient
from mrs3.portfolio.own_history import own_history_unit_drawdown
from mrs3.portfolio.weighted_search import (
    _Solution,
    _solve_additional_lp,
    _solve_lp,
    evaluate_weighted_path,
    slot_composition_frontier,
    weighted_search,
)


def _at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=timezone.utc)


def _sample(day: int, equity: str) -> dict[str, object]:
    return {"timestamp_utc": _at(day).isoformat().replace("+00:00", "Z"), "equity": Decimal(equity)}


def test_unit_drawdown_divides_each_change_by_the_active_cycle_basis() -> None:
    samples = (_sample(1, "100"), _sample(2, "80"), _sample(3, "110"), _sample(4, "105"), _sample(6, "90"))
    cycles = (
        {"opened_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-05T00:00:00Z", "source_basis": Decimal("100")},
    )

    # -0.20, +0.30, -0.05 inside the cycle; the 05..06 fall has no active cycle.
    assert own_history_unit_drawdown(samples, cycles) == Decimal("0.2")


def test_unit_drawdown_skips_unknown_basis_and_is_zero_without_losses() -> None:
    samples = (_sample(1, "100"), _sample(2, "50"), _sample(3, "60"))
    unknown = ({"opened_at": "2026-01-01T00:00:00Z", "closed_at": None, "source_basis": None},)
    # Opens after the fall: the 01..02 fall has no owner, the 02..03 rise is its own.
    rising = ({"opened_at": "2026-01-02T12:00:00Z", "closed_at": None, "source_basis": Decimal("50")},)

    assert own_history_unit_drawdown(samples, unknown) == Decimal(0)
    assert own_history_unit_drawdown(samples, rising) == Decimal(0)


def _cycle(opened: str, closed: str | None) -> dict[str, object]:
    return {"opened_at": opened, "closed_at": closed, "source_basis": Decimal("100")}


@pytest.mark.parametrize("samples,cycles", [
    # (a) the cycle opens inside a sparse sample gap: the fall is still its own.
    ((_sample(1, "100"), _sample(3, "70")), (_cycle("2026-01-02T12:00:00Z", None),)),
    # (b) the cycle opens exactly at the right sample: the right-node opening owns it.
    ((_sample(1, "100"), _sample(3, "70")), (_cycle("2026-01-03T00:00:00Z", None),)),
    # (c) one cycle closes and the next opens inside one gap: the later cycle owns it.
    ((_sample(3, "100"), _sample(10, "70")), (_cycle("2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z"), _cycle("2026-01-09T00:00:00Z", None))),
])
def test_unit_drawdown_attributes_sparse_gaps_like_the_optimizer_series(samples, cycles) -> None:
    assert own_history_unit_drawdown(samples, cycles) == Decimal("0.3")


def _action(index: int, day: int, order: int, action: str, post_size: str, pnl: str, fee: str, balance: str) -> dict[str, object]:
    return {
        "action_index": index, "timestamp_utc": _at(day).isoformat().replace("+00:00", "Z"), "symbol": "X",
        "order_id": order, "action": action, "size": "1", "post_size": post_size,
        "post_side": "long" if post_size != "0" else "", "pnl": pnl, "fee": fee, "balance": balance,
        "price": "1", "cost": "1",
    }


def _row(symbol: str, strategy_id: int, start_day: int, actions, equity) -> dict[str, object]:
    start = _at(start_day).isoformat().replace("+00:00", "Z")
    end = _at(15).isoformat().replace("+00:00", "Z")
    return {
        "symbol": symbol, "side": "LONG", "strategy_id": strategy_id, "result_id": 100 + strategy_id,
        "report_start_utc": start, "report_end_utc": end, "effective_start_utc": start, "effective_end_utc": end,
        "imported_at_utc": end, "initial_balance": Decimal("100"),
        "sizing_use_upnl": True, "sizing_use_frozen_balance": True, "sizing_use_fix": False,
        "sizing_balance_percentage_long": Decimal("100"), "sizing_risk_long": Decimal("1"),
        "sizing_max_balance": Decimal("0"),
        "actions": tuple({**item, "symbol": symbol} for item in actions),
        "equity": tuple({"sample_index": index, **sample} for index, sample in enumerate(equity)),
        "_source_origin": "LEGACY_FIXTURE",
    }


def test_prepare_measures_drawdown_on_the_full_own_history_outside_the_common_window() -> None:
    early = _row(
        "AAA", 1, 1,
        (
            _action(0, 1, 1, "opened", "1", "0", "1", "100"),
            _action(1, 4, 1, "closed", "0", "20", "0", "120"),
            _action(2, 9, 2, "opened", "1", "0", "1", "120"),
            _action(3, 10, 2, "closed", "0", "5", "0", "125"),
        ),
        (_sample(1, "100"), _sample(2, "70"), _sample(4, "120"), _sample(9, "120"), _sample(10, "125"), _sample(15, "125")),
    )
    late = _row(
        "BBB", 2, 8,
        (
            _action(0, 9, 1, "opened", "1", "0", "1", "100"),
            _action(1, 10, 1, "closed", "0", "10", "0", "110"),
        ),
        (_sample(8, "100"), _sample(9, "100"), _sample(10, "110"), _sample(15, "110")),
    )

    prepared = prepare_weighted_input((early, late), minimum_common_days=1)

    assert prepared.timestamps_utc[0] == "2026-01-08T00:00:00Z"
    by_id = dict(zip(prepared.strategy_ids, prepared.own_history_unit_drawdowns))
    # basis = balance - pnl + fee at the opening row = 101; the 01..02 fall is -30.
    assert abs(by_id[1] - Decimal(30) / Decimal(101)) < Decimal("1e-20")
    assert by_id[2] == Decimal(0)
    # The common window itself has no drawdown for either member.
    assert all(value >= 0 for row in prepared.normalized_delta for value in row)


def test_unit_drawdown_matches_the_prepared_grid_when_the_window_is_the_whole_history() -> None:
    row = _row(
        "AAA", 1, 1,
        (
            _action(0, 1, 1, "opened", "1", "0", "1", "100"),
            _action(1, 4, 1, "closed", "0", "20", "0", "120"),
            _action(2, 6, 2, "opened", "1", "0", "1", "120"),
            _action(3, 9, 2, "closed", "0", "-10", "0", "110"),
        ),
        (_sample(1, "100"), _sample(2, "70"), _sample(4, "120"), _sample(6, "120"), _sample(7, "90"), _sample(9, "110"), _sample(15, "110")),
    )

    prepared = prepare_weighted_input((row,), minimum_common_days=1)

    total = peak = grid_drawdown = Decimal(0)
    for cells in prepared.normalized_delta:
        total += cells[0]
        peak = max(peak, total)
        grid_drawdown = max(grid_drawdown, peak - total)
    assert grid_drawdown > 0
    assert abs(prepared.own_history_unit_drawdowns[0] - grid_drawdown) < Decimal("1e-20")


def test_evaluate_reports_the_own_history_bank_next_to_the_path_bank() -> None:
    delta = ((Decimal("0.1"), Decimal("0.1")),)
    plain = evaluate_weighted_path(delta, (Decimal("40"), Decimal("10")), max_dd=Decimal("0.2"), common_days=Decimal("30"))
    capped = evaluate_weighted_path(
        delta, (Decimal("40"), Decimal("10")), max_dd=Decimal("0.2"), common_days=Decimal("30"),
        unit_drawdowns=(Decimal("0.5"), Decimal("0")),
    )

    assert capped["path_bank"] == plain["bank_for_path"]
    assert capped["own_history_dd_bank"] == Decimal("100")
    assert capped["bank_for_path"] == max(plain["bank_for_path"], Decimal("100"))


def test_discovery_lp_caps_only_the_member_with_a_large_own_drawdown() -> None:
    kwargs = dict(
        max_dd=Decimal("0.2"), target=None, bank_available=Decimal("100"), maximize=True,
        symbol_cap_groups={}, time_limit=Decimal("10"),
    )
    delta = ((Decimal("0.1"), Decimal("0.1")),)
    caps = (Decimal("100"), Decimal("100"))
    coefficients = (Decimal("1"), Decimal("1"))

    free = _solve_lp(delta, caps, coefficients, **kwargs)
    capped = _solve_lp(delta, caps, coefficients, unit_drawdowns=(Decimal("0.5"), Decimal("0")), **kwargs)

    assert free.status == capped.status == "PASS"
    assert free.solution.x[0] > Decimal("99")
    # 0.5 * x0 <= 0.2 * 100
    assert capped.solution.x[0] <= Decimal("40.000001")
    assert capped.solution.x[1] > Decimal("99")
    assert capped.solution.bank >= capped.solution.x[0] * Decimal("0.5") / Decimal("0.2") - Decimal("0.000001")


def test_composition_frontier_respects_the_own_drawdown_rows() -> None:
    delta = ((Decimal("0.1"), Decimal("0.1")),)
    kwargs = dict(max_dd=Decimal("0.2"), common_days=Decimal("30"), bank_available=Decimal("100"), levels=1)

    free = slot_composition_frontier(delta, (Decimal("100"), Decimal("100")), ((0,), (1,)), **kwargs)
    capped = slot_composition_frontier(
        delta, (Decimal("100"), Decimal("100")), ((0,), (1,)), unit_drawdowns=(Decimal("0.5"), Decimal("0")), **kwargs,
    )

    assert capped[0].p30 < free[0].p30


def _additional_kwargs() -> dict[str, object]:
    return {
        "normalized_delta": ((Decimal("0"), Decimal("0")),),
        "capacities": (Decimal("10"), Decimal("10")),
        "bank_fixed": Decimal("10"),
        "max_dd": Decimal("0.20"),
        "objective_coefficients": (Decimal("1"), Decimal("1")),
        "L": 1,
        "priorities": (5, 5),
        "margin_a": (Decimal("0.1"), Decimal("0.1")),
        "margin_b": (Decimal("0"), Decimal("0")),
        "max_mm_load": Decimal("0.35"),
        "reserve": Decimal("0.40"),
        "symbol_cap_groups": {},
        "limiter_release_status": "UNKNOWN",
    }


def test_fixed_bank_lp_tightens_the_bound_of_the_capped_member() -> None:
    free = _solve_additional_lp(**_additional_kwargs())
    capped = _solve_additional_lp(**_additional_kwargs(), unit_drawdowns=(Decimal("1"), Decimal("0")))

    assert free.status == capped.status == "PASS"
    assert free.solution.x[0] > Decimal("2.5")
    # x0 <= 0.2 * 10 / 1
    assert capped.solution.x[0] <= Decimal("2.000001")


def _prepared(rows, unit_drawdowns) -> PreparedWeightedInput:
    t = len(rows)
    timestamps = tuple(
        datetime(2024, 1, 1, tzinfo=timezone.utc).replace(minute=5 * index).isoformat().replace("+00:00", "Z")
        for index in range(t + 1)
    )
    base = PreparedWeightedInput(
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 1, 0, 5 * t, tzinfo=timezone.utc),
        5,
        timestamps,
        (1, 2),
        tuple(tuple(Decimal(value) for value in row) for row in rows),
        tuple(tuple(True for _ in row) for row in rows),
        tuple(tuple(None for _ in row) for row in rows),
        {},
        {},
        "fixture",
    )
    return replace(base, own_history_unit_drawdowns=tuple(Decimal(value) for value in unit_drawdowns))


def test_weighted_search_candidates_carry_and_satisfy_the_own_history_bank() -> None:
    members = tuple({"symbol": f"S{index}", "side": "LONG", "strategy_id": index + 1, "result_id": 100 + index} for index in range(2))
    result = weighted_search(
        _prepared((("0.4", "0.1"), ("0.1", "0.1")), ("0.5", "0.5")),
        (Decimal("100"), Decimal("100")),
        members=members,
        target_p30=Decimal("10"),
        bootstrap_scenarios=2,
        screening_scenarios=1,
        max_candidates=2,
        workers=1,
    )

    assert result.status == "PASS"
    assert result.candidates
    for candidate in result.candidates:
        largest = max(member["x_usdt"] for member in candidate.members)
        bank = candidate.metrics["required_bank_usdt"]
        assert largest > 0
        assert candidate.metrics["own_history_dd_bank_usdt"] == largest * Decimal("0.5") / Decimal("0.2")
        assert largest * Decimal("0.5") <= Decimal("0.2") * bank * (1 + Decimal("1e-9"))
        assert candidate.metrics["historical_bank_usdt"] <= candidate.metrics["bank_for_path_usdt"]


def test_additional_and_cdar_families_survive_compaction_of_zero_members(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    members = tuple(
        {"symbol": f"S{index}", "side": "LONG", "strategy_id": index + 1, "result_id": 100 + index, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"}
        for index in range(2)
    )
    coefficients = tuple(
        MarginCoefficient(index + 1, Decimal("0.10"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100"))
        for index in range(2)
    )
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))))
    # Each fixed-bank family sets one member to zero, so revalidation compacts it away.
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("0"), Decimal("2")))))

    result = weighted_search(
        _prepared((("1", "1"),), ("0.5", "0.5")),
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=4,
        workers=1,
    )

    assert result.status == "PASS"
    assert result.manifest["additional"]["outcome"] == "accepted"
    assert result.manifest["cdar"]["accepted_count"] > 0
    single_member = [candidate for candidate in result.candidates if len(candidate.members) == 1]
    assert single_member
    for candidate in single_member:
        assert candidate.metrics["own_history_dd_bank_usdt"] == Decimal("2") * Decimal("0.5") / Decimal("0.2")
