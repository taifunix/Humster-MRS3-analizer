from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from mrs3.mrs2_simulator import HOUR_MS, PeriodResult, _drawdowns, monthly_returns, simulate_mrs2, simulate_period
from mrs3.period_csv import ms_of
from mrs3.trade_store import PRICE_POINT, TradeStoreError, hourly_rows, point_id
from mrs3.trade_history import simulate_segment
from mrs3.trade_windows import compute_window

from tests.test_trade_store import POINTS, walk


def _record(start: date, end: date) -> dict:
    return {"segment_id": f"bybit/TESTUSDT/simulator_{start}_{end}_eng", "exchange": "bybit", "symbol": "TESTUSDT",
            "source": "simulator", "start_ms": ms_of(start), "end_ms": ms_of(end)}


def _result(data, point, start: date, end: date) -> PeriodResult:
    cycles = simulate_mrs2(data, ms_of(start), ms_of(end), timeframe=point.timeframe, open_len=point.open_len,
                           open_mult=(10_000 - point.shift_bp) / 10_000, close_len=point.close_len, tick=0.01)
    return simulate_period(cycles, data, ms_of(start), ms_of(end), qty_step=0.01)


def test_hourly_drawdown_is_the_largest_drawdown_inside_the_hour():
    data = walk(date(2026, 8, 1), 6)
    start, end = ms_of(date(2026, 8, 5)), ms_of(date(2026, 8, 7))
    cycles = simulate_mrs2(data, start, end, timeframe="15m", open_len=2, open_mult=0.993, close_len=2, tick=0.01)
    result = simulate_period(cycles, data, start, end, qty_step=0.01)
    rows = hourly_rows(7, result, start, end)
    stamps = np.array([stamp for stamp, _ in result.path])
    values = np.array([value for _, value in result.path])
    carried = 10_000.0
    for index, hour in enumerate(rows["hour_ms"]):
        samples = [carried, *values[(stamps >= hour) & (stamps < hour + HOUR_MS)]]
        running = np.maximum.accumulate(samples)
        assert rows["drawdown"][index] == pytest.approx(((running - samples) / running * 100).max(), abs=1e-5)
        carried = values[np.searchsorted(stamps, hour + HOUR_MS, "left") - 1]
    assert rows["drawdown"].max() > 0


def test_one_segment_window_equals_the_segment_metrics():
    data = walk(date(2026, 7, 24), 12)
    start, end = date(2026, 7, 28), date(2026, 8, 4)
    tables = simulate_segment(data, start, end, tick=0.01, qty_step=0.01, points=POINTS)
    window = compute_window([(_record(start, end), tables.metrics, tables.hourly)])
    metrics = tables.metrics.to_pydict()
    assert list(window.point_ids) == metrics["point_id"]
    assert list(window.final_balance) == metrics["final_balance"]
    assert list(window.trades) == metrics["trades"]
    assert window.max_drawdown_percent == pytest.approx(metrics["max_drawdown_percent"], abs=1e-3)
    assert window.ulcer_index == pytest.approx(metrics["ulcer_index"], abs=1e-3)
    for index, point in enumerate(sorted(POINTS, key=lambda p: point_id(p.timeframe, p.shift_bp, p.open_len,
                                                                          p.close_len))):
        result = _result(data, point, start, end)
        for key, value in result.monthly_returns.items():
            assert window.monthly_returns[key][index] == pytest.approx(value, abs=1e-3)
    assert sum(metrics["trades"]) > 10 and max(metrics["max_drawdown_percent"]) > 0


def test_multi_segment_window_chains_the_segment_curves():
    data = walk(date(2026, 7, 24), 13)
    spans = [(date(2026, 7, 28), date(2026, 8, 2)), (date(2026, 8, 2), date(2026, 8, 6))]
    parts = []
    for start, end in spans:
        tables = simulate_segment(data, start, end, tick=0.01, qty_step=0.01, points=POINTS)
        parts.append((_record(start, end), tables.metrics, tables.hourly))
    window = compute_window(parts)
    start_ms, end_ms = ms_of(spans[0][0]), ms_of(spans[-1][1])
    ordered = sorted(POINTS, key=lambda p: point_id(p.timeframe, p.shift_bp, p.open_len, p.close_len))
    for index, point in enumerate(ordered):
        path, offset, marked, trades = [], 0.0, 0.0, 0
        for start, end in spans:
            result = _result(data, point, start, end)
            if result.trades or not path:
                # the curve ends at the last action: a trailing segment without trades does not extend it
                path += [(stamp, value + offset) for stamp, value in result.path]
            marked = result.marked_final + offset
            offset += result.final_balance - 10_000
            trades += len(result.trades)
        chained = PeriodResult(final_balance=10_000 + offset, path=path, marked_final=marked,
                               trades=[0.0] * trades)
        _drawdowns(chained, start_ms, end_ms)
        assert window.final_balance[index] == pytest.approx(chained.final_balance, abs=1e-9)
        assert window.trades[index] == trades
        assert window.ulcer_index[index] == pytest.approx(chained.ulcer_index, abs=1e-3)
        assert chained.max_drawdown_percent - 1e-3 <= window.max_drawdown_percent[index] <= \
            chained.max_drawdown_percent + 0.05
        for key, value in monthly_returns(chained, start_ms, end_ms, 10_000).items():
            assert window.monthly_returns[key][index] == pytest.approx(value, abs=1e-3)
    assert set(window.monthly_returns) == {"2026_07", "2026_08"}


def test_a_window_takes_consecutive_segments_of_one_pair():
    data = walk(date(2026, 7, 24), 13)
    first = simulate_segment(data, date(2026, 7, 28), date(2026, 8, 1), tick=0.01, qty_step=0.01, points=POINTS)
    later = simulate_segment(data, date(2026, 8, 2), date(2026, 8, 5), tick=0.01, qty_step=0.01, points=POINTS)
    with pytest.raises(TradeStoreError):
        compute_window([(_record(date(2026, 7, 28), date(2026, 8, 1)), first.metrics, first.hourly),
                        (_record(date(2026, 8, 2), date(2026, 8, 5)), later.metrics, later.hourly)])
    assert PRICE_POINT == 0


def test_intra_hour_drawdown_is_rescaled_to_the_shifted_curve():
    from mrs3.trade_store import build_tables, metrics_columns, trade_columns

    def segment(start: date, path: list[tuple[int, float]], final: float):
        result = PeriodResult(final_balance=final, trades=[final - 10_000 or 1.0], path=path, marked_final=final)
        tables = build_tables([trade_columns(7, result)], [hourly_rows(7, result, ms_of(start), ms_of(start) + 86_400_000)],
                              [metrics_columns(7, result)])
        return tables.metrics, tables.hourly

    a, b = ms_of(date(2026, 8, 1)), ms_of(date(2026, 8, 2))
    first = segment(date(2026, 8, 1), [(a, 10_000.0), (a + 2 * HOUR_MS, 13_000.0)], 13_000.0)
    second = segment(date(2026, 8, 2), [(b, 10_000.0), (b + 600_000, 10_300.0), (b + 1_200_000, 10_000.0)], 10_000.0)
    window = compute_window([(_record(date(2026, 8, 1), date(2026, 8, 2)), *first),
                             (_record(date(2026, 8, 2), date(2026, 8, 3)), *second)])
    assert window.max_drawdown_percent[0] == pytest.approx(300 / 13_300 * 100, abs=1e-3)
