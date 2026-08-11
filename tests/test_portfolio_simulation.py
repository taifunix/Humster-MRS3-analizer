"""Тесты слоя B: симулятор, подбор лотов, перебор, Парето, OOS."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mrs3.portfolio.lots import fit_lots, weight_shapes
from mrs3.portfolio.models import RunConfig, StrategyInput, TradeRecord
from mrs3.portfolio.search import (
    correlation_matrix,
    enumerate_sets,
    pareto_front,
    screen_layer_a,
)
from mrs3.portfolio.simulator import simulate_set

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def trades(sid, count, *, hold_min=60, gap_min=120, pnl=0.01, mae=None, start=T0):
    out, t = [], start
    for _ in range(count):
        entry = t
        exit_ = entry + timedelta(minutes=hold_min)
        out.append(
            TradeRecord(sid, entry, exit_, pnl_frac=pnl, fee_frac=0.0, mae_frac=mae)
        )
        t = exit_ + timedelta(minutes=gap_min)
    return out


def strat(sid, *, count=40, hold=60, gap=120, pnl=0.01, lot=0.5, mae=None,
          pair=None, side="LONG", turnover=5_000_000.0, start=T0, tf="1h"):
    return StrategyInput(
        strategy_id=sid,
        pair=pair or f"{sid}USDT",
        side=side,
        timeframe=tf,
        lot_x_base=lot,
        pnl_pct=20.0,
        dd_pct=5.0,
        turnover_24h=turnover,
        trades=trades(sid, count, hold_min=hold, gap_min=gap, pnl=pnl, mae=mae, start=start),
    )


CFG = RunConfig(deposit=1000.0, limiters=(2,), d_eff_common_min_days=0.0, oos_min_days=1e9)


# --- контракт модели -------------------------------------------------------


def test_mmr_must_be_below_imr():
    with pytest.raises(ValueError, match="mmr < imr"):
        StrategyInput("A", "X", "LONG", "1h", 0.5, 1.0, 5.0, 1.0, mmr=0.06, imr=0.05)


def test_pnl_frac_scales_with_notional():
    single = strat("A", count=10, pnl=0.01)
    small = simulate_set([single], 1, {"A": 0.10}, CFG)
    large = simulate_set([single], 1, {"A": 0.20}, CFG)
    assert large.pnl_abs > small.pnl_abs * 1.5


# --- ограничитель ----------------------------------------------------------


def test_limiter_blocks_third_concurrent_signal():
    members = [strat(s, count=20, hold=600, gap=1) for s in ("A", "B", "C")]
    result = simulate_set(members, 2, {m.strategy_id: 0.05 for m in members}, CFG)
    blocked = sum(o.blocked_slot for o in result.outcomes)
    assert blocked > 0
    assert all(o.blocked_margin == 0 for o in result.outcomes)


def test_higher_limiter_accepts_more_signals():
    members = [strat(s, count=20, hold=600, gap=1) for s in ("A", "B", "C")]
    lots = {m.strategy_id: 0.05 for m in members}
    low = simulate_set(members, 2, lots, CFG)
    high = simulate_set(members, 3, lots, CFG)
    assert sum(o.accepted for o in high.outcomes) >= sum(o.accepted for o in low.outcomes)


def test_blocked_signal_is_lost_not_deferred():
    members = [strat(s, count=5, hold=6000, gap=1) for s in ("A", "B")]
    result = simulate_set(members, 1, {"A": 0.05, "B": 0.05}, CFG)
    total = sum(o.accepted + o.blocked for o in result.outcomes)
    assert total == 10  # ровно число исходных сигналов, ничего не переносится


# --- маржа -----------------------------------------------------------------


def test_margin_limit_blocks_entry_and_is_reported_separately():
    members = [strat(s, count=15, hold=300, gap=60) for s in ("A", "B")]
    tight = RunConfig(deposit=1000.0, limiters=(2,), margin_limit=0.001,
                      d_eff_common_min_days=0.0, oos_min_days=1e9)
    result = simulate_set(members, 2, {"A": 0.5, "B": 0.5}, tight)
    assert sum(o.blocked_margin for o in result.outcomes) > 0
    assert sum(o.accepted for o in result.outcomes) == 0


def test_resting_orders_consume_margin():
    """Занятая маржа считается и когда позиции ещё нет: заявки висят."""
    members = [strat(s, count=10, hold=60, gap=6000) for s in ("A", "B")]
    result = simulate_set(members, 2, {"A": 0.5, "B": 0.5}, CFG)
    assert result.max_occupancy_margin > 0


def test_margin_ratio_uses_mmr_not_imr():
    single = strat("A", count=10, hold=600, gap=60)
    result = simulate_set([single], 1, {"A": 0.5}, CFG)
    # mmr 0.02 против imr 0.05: ликвидационная метрика заведомо ниже занятой
    assert result.max_margin_ratio < result.max_occupancy_margin


# --- ёмкость ---------------------------------------------------------------


def test_capacity_caps_notional():
    thin = strat("A", count=20, hold=60, gap=60, turnover=1000.0)
    rich = strat("B", count=20, hold=60, gap=60, turnover=50_000_000.0)
    assert thin.capacity < rich.capacity
    result = simulate_set([thin], 1, {"A": 10.0}, CFG)
    assert "CAPACITY_BOUND" in result.flags


# --- встречные заявки ------------------------------------------------------


def test_cancel_opposite_blocks_short_while_long_is_open():
    long_ = strat("L", count=10, hold=600, gap=1, pair="XLKUSDT", side="LONG")
    short = strat("S", count=10, hold=600, gap=1, pair="XLKUSDT", side="SHORT")
    on = simulate_set([long_, short], 2, {"L": 0.05, "S": 0.05}, CFG)
    off = simulate_set(
        [long_, short], 2, {"L": 0.05, "S": 0.05},
        RunConfig(deposit=1000.0, limiters=(2,), cancel_opposite=False,
                  d_eff_common_min_days=0.0, oos_min_days=1e9),
    )
    assert sum(o.accepted for o in on.outcomes) < sum(o.accepted for o in off.outcomes)


# --- подбор множителя ------------------------------------------------------


def test_weight_shapes_collapse_scalar_multiples():
    shapes = weight_shapes(3, (1, 2, 3))
    assert (1, 1, 1) in shapes
    assert (2, 2, 2) not in shapes  # кратно (1,1,1)
    assert (1, 1, 2) in shapes


def test_weight_shapes_single_level_gives_one_shape():
    assert weight_shapes(4, (1,)) == [(1, 1, 1, 1)]


def test_fit_lots_respects_dd_target():
    members = [strat(s, count=40, hold=60, gap=120, pnl=0.004, mae=-0.02) for s in ("A", "B")]
    cfg = RunConfig(deposit=1000.0, limiters=(2,), dd_target_pct=5.0,
                    d_eff_common_min_days=0.0, oos_min_days=1e9)
    fit = fit_lots(members, 2, (1, 1), cfg)
    assert fit.result is not None
    if fit.status == "OK":
        assert fit.result.max_dd_pct <= cfg.dd_target_pct + 1e-9


def test_fit_lots_flags_unreachable_target():
    """Совсем безубыточная стратегия не дотянет до целевой просадки."""
    members = [strat(s, count=30, hold=60, gap=600, pnl=0.01) for s in ("A", "B")]
    cfg = RunConfig(deposit=1000.0, limiters=(2,), dd_target_pct=50.0,
                    d_eff_common_min_days=0.0, oos_min_days=1e9)
    fit = fit_lots(members, 2, (1, 1), cfg)
    assert fit.status == "DD_TARGET_UNREACHABLE"


def test_fit_lots_scan_covers_whole_grid():
    members = [strat(s, count=20) for s in ("A", "B")]
    cfg = RunConfig(deposit=1000.0, limiters=(2,), g_grid_points=8,
                    d_eff_common_min_days=0.0, oos_min_days=1e9)
    fit = fit_lots(members, 2, (1, 1), cfg)
    assert len(fit.scan) >= cfg.g_grid_points
    assert fit.scan == sorted(fit.scan, key=lambda item: item[0])


# --- слой A внутри перебора ------------------------------------------------


def test_layer_a_rejects_timeframe_asymmetry():
    slow = strat("SLOW", count=60, hold=480, gap=60)
    fast = strat("FAST", count=400, hold=6, gap=6)
    ok, reasons, _, _ = screen_layer_a([slow, fast], 2, RunConfig(d_eff_common_min_days=0.0))
    assert not ok and "TF_ASYMMETRY" in reasons


def test_layer_a_rejects_short_common_window():
    a = strat("A", count=20, start=T0)
    b = strat("B", count=20, start=T0 + timedelta(days=300))
    ok, reasons, _, _ = screen_layer_a([a, b], 2, RunConfig())
    assert not ok and "D_EFF_COMMON_TOO_SHORT" in reasons


# --- Парето ----------------------------------------------------------------


def test_pareto_drops_dominated_sets():
    members = [strat(s, count=30, hold=60, gap=180, mae=-0.01) for s in ("A", "B", "C")]
    cfg = RunConfig(deposit=1000.0, limiters=(2,), d_eff_common_min_days=0.0,
                    oos_min_days=1e9, max_size_factor=2)
    results, _ = enumerate_sets(members, cfg)
    front = pareto_front(results)
    assert len(front) <= len(results)
    for candidate in results:
        if candidate in front:
            continue
        assert any(
            other.pnl30_pct >= candidate.pnl30_pct
            and other.capital_requirement <= candidate.capital_requirement
            for other in front
        )


def test_enumerate_is_deterministic():
    members = [strat(s, count=25, hold=60, gap=180, mae=-0.01) for s in ("A", "B", "C")]
    cfg = RunConfig(deposit=1000.0, limiters=(2,), d_eff_common_min_days=0.0,
                    oos_min_days=1e9, max_size_factor=2)
    first, _ = enumerate_sets(members, cfg)
    second, _ = enumerate_sets(list(reversed(members)), cfg)
    assert [r.strategy_ids for r in first] == [r.strategy_ids for r in second]
    assert [round(r.pnl30_pct, 9) for r in first] == [round(r.pnl30_pct, 9) for r in second]


def test_set_size_may_exceed_limiter():
    members = [strat(s, count=20, hold=30, gap=600, mae=-0.01) for s in "ABCD"]
    cfg = RunConfig(deposit=1000.0, limiters=(2,), d_eff_common_min_days=0.0,
                    oos_min_days=1e9, max_size_factor=2)
    _, screened = enumerate_sets(members, cfg)
    assert max(row["size"] for row in screened) == 4


# --- корреляция ------------------------------------------------------------


def test_correlation_of_identical_streams_is_one():
    a = strat("A", count=30, hold=60, gap=180)
    b = strat("B", count=30, hold=60, gap=180)
    corr = correlation_matrix([a, b])
    assert corr["pairs"][0]["corr"] == pytest.approx(1.0, abs=1e-6)


def test_worst_day_is_negative_when_losses_exist():
    losing = strat("A", count=30, hold=60, gap=180, pnl=-0.02)
    corr = correlation_matrix([losing, strat("B", count=30)])
    assert corr["worst_day_frac"] < 0


# --- метрики конкуренции ---------------------------------------------------


def test_acceptance_is_one_when_nothing_competes():
    single = strat("A", count=20, hold=30, gap=600)
    result = simulate_set([single], 2, {"A": 0.05}, CFG)
    assert result.outcomes[0].acceptance == pytest.approx(1.0)


def test_flags_report_missing_mae():
    result = simulate_set([strat("A", count=10)], 1, {"A": 0.05}, CFG)
    assert "CLOSED_TRADE_DD_ONLY" in result.flags


def test_flags_report_estimated_target_share():
    result = simulate_set([strat("A", count=10)], 1, {"A": 0.05}, CFG)
    assert "TARGET_SHARE_ESTIMATED" in result.flags


# --- регрессии по замечаниям ревью --------------------------------------


def test_entering_strategy_margin_is_not_double_counted():
    """Маржа входящей стратегии считалась дважды: как заявка и как позиция.

    При пороге ровно между одинарным и двойным резервом вход обязан пройти.
    """
    members = [strat("A", count=6, hold=60, gap=600, lot=0.5)]
    single = 0.5 * 0.05           # lot * imr
    cfg = RunConfig(
        deposit=1000.0, limiters=(1,), margin_limit=single * 1.5,
        d_eff_common_min_days=0.0, oos_min_days=1e9,
    )
    result = simulate_set(members, 1, {"A": 0.5}, cfg)
    assert sum(o.accepted for o in result.outcomes) == 6
    assert sum(o.blocked_margin for o in result.outcomes) == 0


def test_overlapping_positions_of_one_strategy_both_reserve_margin():
    """Перекрывающиеся циклы в журнале: маржу занимает каждый, а не последний."""
    overlapping = StrategyInput(
        strategy_id="A", pair="XUSDT", side="LONG", timeframe="1h",
        lot_x_base=0.2, pnl_pct=10.0, dd_pct=5.0, turnover_24h=50_000_000.0,
        trades=[
            TradeRecord("A", T0, T0 + timedelta(hours=10), 0.01),
            TradeRecord("A", T0 + timedelta(hours=1), T0 + timedelta(hours=11), 0.01),
        ],
    )
    result = simulate_set([overlapping], 2, {"A": 0.2}, CFG)
    assert sum(o.accepted for o in result.outcomes) == 2
    # две позиции по 0.2 * 0.05 = 0.02 каждая
    assert result.max_occupancy_margin == pytest.approx(0.02, abs=5e-3)


def test_priority_follows_pnl_dd_not_alphabet():
    """При одновременных сигналах первым идёт сильнейший по PnL30_DD5."""
    weak = StrategyInput(
        strategy_id="AAA", pair="P1", side="LONG", timeframe="1h",
        lot_x_base=0.3, pnl_pct=5.0, dd_pct=5.0, turnover_24h=50_000_000.0,
        trades=trades("AAA", 4, hold_min=600, gap_min=1),
    )
    strong = StrategyInput(
        strategy_id="ZZZ", pair="P2", side="LONG", timeframe="1h",
        lot_x_base=0.3, pnl_pct=80.0, dd_pct=5.0, turnover_24h=50_000_000.0,
        trades=trades("ZZZ", 4, hold_min=600, gap_min=1),
    )
    result = simulate_set([weak, strong], 1, {"AAA": 0.1, "ZZZ": 0.1}, CFG)
    outcomes = {o.strategy_id: o for o in result.outcomes}
    assert outcomes["ZZZ"].accepted >= outcomes["AAA"].accepted


def test_partial_mae_coverage_is_flagged():
    """Часть циклов без MAE считается нулём — это должно быть видно в флагах."""
    with_mae = strat("A", count=10, mae=-0.02)
    without = strat("B", count=10, mae=None)
    result = simulate_set([with_mae, without], 2, {"A": 0.05, "B": 0.05}, CFG)
    assert any(f.startswith("PARTIAL_MAE_COVERAGE") for f in result.flags)
    assert "CLOSED_TRADE_DD_ONLY" not in result.flags


def test_full_mae_coverage_has_no_partial_flag():
    members = [strat(s, count=10, mae=-0.02) for s in ("A", "B")]
    result = simulate_set(members, 2, {"A": 0.05, "B": 0.05}, CFG)
    assert not any(f.startswith("PARTIAL_MAE") for f in result.flags)


def test_trade_crossing_window_end_is_kept_and_flagged():
    """Сделка, начатая в окне и вышедшая за него, не теряется."""
    member = StrategyInput(
        strategy_id="A", pair="XUSDT", side="LONG", timeframe="1h",
        lot_x_base=0.2, pnl_pct=10.0, dd_pct=5.0, turnover_24h=50_000_000.0,
        trades=[
            TradeRecord("A", T0, T0 + timedelta(hours=1), 0.01),
            TradeRecord("A", T0 + timedelta(hours=2), T0 + timedelta(hours=50), 0.02),
        ],
    )
    window = (T0, T0 + timedelta(hours=10))
    result = simulate_set([member], 1, {"A": 0.2}, CFG, window=window)
    assert sum(o.accepted for o in result.outcomes) == 2
    assert any(f.startswith("TRUNCATED_AT_WINDOW_END") for f in result.flags)


def test_single_day_history_does_not_divide_by_zero():
    member = StrategyInput(
        strategy_id="A", pair="XUSDT", side="LONG", timeframe="5m",
        lot_x_base=0.2, pnl_pct=1.0, dd_pct=5.0, turnover_24h=1_000_000.0,
        trades=[
            TradeRecord("A", T0, T0 + timedelta(minutes=5), 0.001),
            TradeRecord("A", T0 + timedelta(minutes=10), T0 + timedelta(minutes=15), 0.001),
        ],
    )
    assert member.d_eff_days > 0
    assert member.trades_per_day > 0
    assert member.occupancy > 0
