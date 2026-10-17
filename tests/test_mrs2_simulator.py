from __future__ import annotations

from datetime import datetime, timezone
import math

import numpy as np
import pytest

from mrs3.mrs2_simulator import (
    Cycle,
    MinuteBars,
    bar_ohlc4,
    month_starts,
    order_prices,
    simulate_mrs2,
    simulate_period,
)

MIN = 60_000
HOUR = 3_600_000


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def bars(start: str, rows: list[tuple[int, float, float, float, float]]) -> MinuteBars:
    """rows: (minute offset, open, high, low, close)."""
    first = ms(start)
    arr = np.array(rows, dtype=np.float64)
    return MinuteBars((first + arr[:, 0] * MIN).astype(np.int64), arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4])


def flat(start: str, minutes: int, price) -> MinuteBars:
    return bars(start, [(i, *(price(i) if callable(price) else price,) * 4) for i in range(minutes)])


def test_bars_fill_minutes_without_trades_with_the_previous_close():
    # 15m bar 00:00 has trades only at 00:00 (100) and 00:10 (110); bar 00:15 has none at all.
    data = bars("2026-08-01T00:00:00", [(0, 100, 100, 100, 100), (10, 110, 112, 109, 110), (30, 120, 120, 120, 120)])
    starts, ohlc4 = bar_ohlc4(data, 15 * MIN)
    assert list(starts) == [ms("2026-08-01T00:00:00"), ms("2026-08-01T00:15:00"), ms("2026-08-01T00:30:00")]
    assert ohlc4[0] == pytest.approx((100 + 112 + 100 + 110) / 4)
    assert ohlc4[1] == pytest.approx(110)                     # empty bar: flat at the previous close
    assert ohlc4[2] == pytest.approx(120)


def test_order_price_is_the_sma_of_closed_bars_rounded_to_the_tick():
    prices = order_prices(np.array([100.0, 102.0, 104.0, 106.0]), 2, 0.993, 0.01)
    assert math.isnan(prices[0]) and math.isnan(prices[1])
    assert prices[2] == pytest.approx(round(101.0 * 0.993, 2))
    assert prices[3] == pytest.approx(round(103.0 * 0.993, 2))


def test_limit_buy_and_sell_fill_at_their_prices():
    # 1h bars at 100 for two hours, then a dip to 98 and a rally to 102 in the third hour.
    def price(i):
        if i < 150:
            return 100.0
        if i < 160:
            return 98.0
        return 102.0
    data = flat("2026-08-01T00:00:00", 240, price)
    cycles = simulate_mrs2(data, ms("2026-08-01T00:00:00"), ms("2026-08-01T04:00:00"), timeframe="1h", open_len=2,
                           open_mult=0.99, close_len=2, tick=0.01)
    assert cycles[0].entry_price == pytest.approx(99.0) and cycles[0].open_ms == ms("2026-08-01T02:30:00")
    assert cycles[0].exit_price == pytest.approx(100.3) and cycles[0].exit_ms == ms("2026-08-01T02:40:00")


def test_a_sell_order_below_the_market_rests_one_tick_above_the_last_price():
    # The entry candle closes far above the close-MA price: the sell goes one tick above that close.
    rows = [(i, 100.0, 100.0, 100.0, 100.0) for i in range(150)]
    rows.append((150, 100.0, 100.0, 98.0, 105.0))            # buy at 99 fills, candle closes at 105
    rows.append((151, 105.0, 106.0, 104.0, 105.5))
    data = bars("2026-08-01T00:00:00", rows)
    cycles = simulate_mrs2(data, ms("2026-08-01T00:00:00"), ms("2026-08-01T04:00:00"), timeframe="1h", open_len=2,
                           open_mult=0.99, close_len=2, tick=0.01)
    assert cycles[0].exit_price == pytest.approx(105.01) and cycles[0].exit_ms == ms("2026-08-01T02:31:00")


def test_no_orders_before_the_first_bar_start_of_the_period():
    data = flat("2026-08-01T00:00:00", 180, lambda i: 100.0 if i < 125 or i > 128 else 98.0)
    late = simulate_mrs2(data, ms("2026-08-01T02:00:00"), ms("2026-08-01T03:00:00"), timeframe="1h", open_len=2,
                         open_mult=0.99, close_len=2, tick=0.01)
    assert late == ()   # the dip lies in the period's first bar: the tester places orders from the next bar


def test_period_replays_a_trade_at_the_fixed_lot_with_fees():
    data = flat("2026-08-01T00:00:00", 6 * 60, 100.0)
    trade = Cycle(ms("2026-08-01T02:00:00"), 99.0, 1e-5, ms("2026-08-01T03:00:00"), 100.0, 1e-5)
    result = simulate_period([trade], data, ms("2026-08-01"), ms("2026-08-01T06:00:00"), qty_step=0.01)
    qty = math.floor(10_000 / 99.0 / 0.01 + 1e-9) * 0.01
    fees = qty * 99.0 * 1e-5 + qty * 100.0 * 1e-5
    assert result.trades == [pytest.approx(qty)]
    assert result.final_balance == pytest.approx(10_000 + qty - fees)


def test_open_position_closes_at_market_and_pays_funding():
    data = flat("2026-08-01T06:00:00", 4 * 60, lambda i: 100.0 if i < 239 else 98.0)
    trade = Cycle(ms("2026-08-01T07:00:00"), 100.0, 1e-5, None, None, None)
    result = simulate_period([trade], data, ms("2026-08-01T06:00:00"), ms("2026-08-01T10:00:00"), qty_step=0.01)
    exit_price = 98.0 * (1 - 0.001)
    funding = 100 * 100.0 * 0.00025                          # 08:00 UTC while in the position
    expected = 10_000 - 100 * 100.0 * 1e-5 - funding + 100 * (exit_price - 100.0) - 100 * exit_price * 0.0002
    assert result.final_balance == pytest.approx(expected)
    assert result.marked_final == pytest.approx(10_000 - 100 * 100.0 * 1e-5 - funding + 100 * (98.0 - 100.0))


def test_ulcer_ignores_hours_after_the_last_curve_point():
    # A trade with an unrealized peak above its exit, then nothing until the end: no drawdown tail.
    # the hour 01:00 ends at the unrealized peak (110), the exit at 104 comes in the next hour
    data = flat("2026-08-01T00:00:00", 3 * 60, lambda i: 100.0 if i < 70 else 110.0 if i < 120 else 104.0)
    trade = Cycle(ms("2026-08-01T01:00:00"), 100.0, 1e-5, ms("2026-08-01T02:10:00"), 104.0, 1e-5)
    long_end = simulate_period([trade], data, ms("2026-08-01"), ms("2026-08-20"), qty_step=0.01)
    short_end = simulate_period([trade], data, ms("2026-08-01"), ms("2026-08-01T03:00:00"), qty_step=0.01)
    assert long_end.ulcer_index == pytest.approx(short_end.ulcer_index)
    assert long_end.ulcer_index > 0


def test_monthly_returns_chain_month_end_margin_balances():
    data = flat("2026-07-31T20:00:00", 8 * 60, 100.0)
    trade = Cycle(ms("2026-07-31T21:00:00"), 100.0, 0.0, ms("2026-07-31T22:00:00"), 110.0, 0.0)
    result = simulate_period([trade], data, ms("2026-07-31T20:00:00"), ms("2026-08-01T04:00:00"), qty_step=0.01)
    assert result.monthly_returns["2026_07"] == pytest.approx(10.0)
    assert result.monthly_returns["2026_08"] == pytest.approx(0.0)
    assert [key for key, _ in month_starts(ms("2026-07-24"), ms("2026-09-17"))] == ["2026_07", "2026_08", "2026_09"]
