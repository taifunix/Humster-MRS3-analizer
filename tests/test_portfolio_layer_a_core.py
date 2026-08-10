"""Тесты слоя A. Бьют по границам порогов, а не по серединам диапазонов."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mrs3.portfolio.layer_a import (
    ASYMMETRY_RATIO,
    LOAD_REJECT_ABOVE,
    MIN_D_EFF_COMMON_DAYS,
    Candidate,
    LayerAError,
    common_window_days,
    find_asymmetry,
    iter_combinations,
    screen_all,
    screen_combination,
)

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def make(
    sid: str,
    *,
    trades: int = 100,
    hold: float = 60.0,
    days: float = 60.0,
    start_offset: float = 0.0,
    turnover: float = 1_000_000.0,
    share: float = 0.10,
    pair: str = "XLKUSDT",
    side: str = "LONG",
    tf: str = "1h",
) -> Candidate:
    start = T0 + timedelta(days=start_offset)
    return Candidate(
        strategy_id=sid,
        pair=pair,
        side=side,
        timeframe=tf,
        trades=trades,
        median_hold_min=hold,
        lot_x_base=0.5,
        pnl_pct=20.0,
        dd_pct=5.0,
        window_start=start,
        window_end=start + timedelta(days=days),
        turnover_24h=turnover,
        target_share=share,
    )


# --- контракт входа --------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trades": 0},
        {"hold": 0.0},
        {"share": 0.0},
        {"share": 1.5},
    ],
)
def test_invalid_candidate_rejected(kwargs):
    with pytest.raises(LayerAError):
        make("A", **kwargs)


def test_invalid_side_rejected():
    with pytest.raises(LayerAError):
        make("A", side="BOTH")


def test_window_end_before_start_rejected():
    with pytest.raises(LayerAError):
        Candidate(
            strategy_id="A",
            pair="X",
            side="LONG",
            timeframe="1h",
            trades=10,
            median_hold_min=10,
            lot_x_base=1.0,
            pnl_pct=1.0,
            dd_pct=5.0,
            window_start=T0,
            window_end=T0,
            turnover_24h=1.0,
            target_share=0.1,
        )


# --- производные величины, §4.1 и §4.2 -------------------------------------


def test_occupancy_matches_specification():
    # 100 сделок по 60 минут за 60 дней = 6000 минут из 86400
    c = make("A", trades=100, hold=60.0, days=60.0)
    assert c.occupancy == pytest.approx(6000 / 86400)


def test_capacity_matches_specification():
    # 100 сделок за 60 дней -> 1.6667 в сутки -> 3.3333 оборота номинала
    c = make("A", trades=100, days=60.0, turnover=1_000_000.0, share=0.10)
    assert c.turns_per_day == pytest.approx(100 / 60 * 2)
    assert c.capacity == pytest.approx(1_000_000.0 * 0.10 / (100 / 60 * 2))


def test_capacity_falls_when_strategy_trades_more_often():
    slow = make("A", trades=100, days=60.0)
    fast = make("B", trades=1000, days=60.0)
    assert fast.capacity == pytest.approx(slow.capacity / 10)


# --- пересечение окон, §7.1 ------------------------------------------------


def test_common_window_is_intersection_not_minimum():
    a = make("A", days=60.0, start_offset=0.0)
    b = make("B", days=60.0, start_offset=30.0)
    # обе по 60 дней, но пересекаются только на 30
    assert common_window_days([a, b]) == pytest.approx(30.0)


def test_disjoint_windows_give_zero():
    a = make("A", days=10.0, start_offset=0.0)
    b = make("B", days=10.0, start_offset=20.0)
    assert common_window_days([a, b]) == 0.0


def test_short_common_window_rejects_combination():
    a = make("A", days=60.0, start_offset=0.0)
    b = make("B", days=60.0, start_offset=35.0)  # пересечение 25 дней
    screen = screen_combination([a, b], limiter=2)
    assert screen.d_eff_common_days < MIN_D_EFF_COMMON_DAYS
    assert "D_EFF_COMMON_TOO_SHORT" in screen.reject_reasons


# --- загрузка слотов, §4.2 -------------------------------------------------


def test_load_status_boundaries():
    # occupancy подбирается так, чтобы load попал ровно на границу
    def screen_with(total_occ: float, limiter: int = 1):
        # одна стратегия, занятость = total_occ
        c = make("A", trades=1, hold=total_occ * 1440.0, days=1.0)
        return screen_combination([c], limiter=limiter)

    assert screen_with(0.49).load_status == "IDLE"
    assert screen_with(0.50).load_status == "WORKING"
    assert screen_with(1.50).load_status == "WORKING"
    assert screen_with(1.51).load_status == "HEAVY"
    assert screen_with(2.50).load_status == "HEAVY"
    assert screen_with(2.51).load_status == "OVERLOADED"


def test_overloaded_combination_is_rejected():
    heavy = [make(f"S{i}", trades=1, hold=1440.0, days=1.0) for i in range(6)]
    screen = screen_combination(heavy, limiter=2)
    assert screen.load == pytest.approx(3.0)
    assert not screen.accepted
    assert f"LOAD_ABOVE_{LOAD_REJECT_ABOVE}" in screen.reject_reasons


def test_larger_limiter_lowers_load():
    members = [make(f"S{i}", trades=100, hold=60.0, days=60.0) for i in range(4)]
    load2 = screen_combination(members, limiter=2).load
    load4 = screen_combination(members, limiter=4).load
    assert load4 == pytest.approx(load2 / 2)


# --- асимметрия таймфреймов, §4.3 ------------------------------------------


def test_asymmetry_needs_both_ratio_and_occupancy():
    # отношение выше порога, но медленная почти не занимает слот
    slow_idle = make("SLOW", trades=1, hold=480.0, days=60.0)
    fast = make("FAST", trades=1000, hold=6.0, days=60.0)
    assert find_asymmetry([slow_idle, fast]) == ()

    # то же отношение, но медленная держит слот заметную долю времени
    slow_busy = make("SLOW", trades=100, hold=480.0, days=60.0)
    pairs = find_asymmetry([slow_busy, fast])
    assert len(pairs) == 1
    assert pairs[0].slow_id == "SLOW"
    assert pairs[0].fast_id == "FAST"
    assert pairs[0].ratio == pytest.approx(80.0)


def test_asymmetry_ratio_boundary_is_strict():
    fast = make("FAST", trades=1000, hold=10.0, days=60.0)
    at_threshold = make("SLOW", trades=200, hold=10.0 * ASYMMETRY_RATIO, days=60.0)
    assert find_asymmetry([at_threshold, fast]) == ()

    above = make("SLOW", trades=200, hold=10.0 * ASYMMETRY_RATIO + 1, days=60.0)
    assert len(find_asymmetry([above, fast])) == 1


def test_asymmetry_direction_is_independent_of_input_order():
    slow = make("AAA", trades=100, hold=480.0, days=60.0)
    fast = make("ZZZ", trades=1000, hold=6.0, days=60.0)
    forward = find_asymmetry([slow, fast])
    backward = find_asymmetry([fast, slow])
    assert forward == backward
    assert forward[0].slow_id == "AAA"


def test_asymmetry_rejects_combination():
    slow = make("SLOW", trades=100, hold=480.0, days=60.0)
    fast = make("FAST", trades=1000, hold=6.0, days=60.0)
    screen = screen_combination([slow, fast], limiter=2)
    assert "TF_ASYMMETRY" in screen.reject_reasons


# --- перебор составов, §6.1 ------------------------------------------------


def test_combination_sizes_start_at_limiter():
    cands = [make(f"S{i}", days=60.0) for i in range(5)]
    sizes = {len(c) for c in iter_combinations(cands, limiter=3)}
    assert min(sizes) == 3


def test_combination_size_may_exceed_limiter():
    cands = [make(f"S{i}", days=60.0) for i in range(8)]
    sizes = {len(c) for c in iter_combinations(cands, limiter=2)}
    assert max(sizes) == 6  # limiter * 3


def test_combination_size_capped_by_candidate_count():
    cands = [make(f"S{i}", days=60.0) for i in range(4)]
    sizes = {len(c) for c in iter_combinations(cands, limiter=2)}
    assert max(sizes) == 4


# --- детерминированность ---------------------------------------------------


def test_screen_all_is_deterministic_regardless_of_input_order():
    cands = [make(f"S{i}", trades=50 + i, hold=30.0 + i, days=60.0) for i in range(5)]
    first = screen_all(cands)
    second = screen_all(list(reversed(cands)))
    assert [r.strategy_ids for r in first] == [r.strategy_ids for r in second]
    assert [r.load for r in first] == [r.load for r in second]


def test_strategy_ids_are_sorted_inside_a_screen():
    a, b, c = make("CCC", days=60.0), make("AAA", days=60.0), make("BBB", days=60.0)
    screen = screen_combination([a, b, c], limiter=3)
    assert screen.strategy_ids == ("AAA", "BBB", "CCC")


def test_empty_combination_rejected():
    with pytest.raises(LayerAError):
        screen_combination([], limiter=2)
