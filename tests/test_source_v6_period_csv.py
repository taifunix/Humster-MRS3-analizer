from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from mrs3.source_v6_period_csv import (
    Candles,
    Cycle,
    PeriodResult,
    RunSettings,
    csv_row,
    cycles_from_fragment,
    first_period_day,
    header,
    last_covered_end,
    month_keys,
    multiplier_text,
    qty_step_from_fragments,
    simulate_period,
    split_periods,
    write_csv,
)

MIN = 60_000
HOUR = 3_600_000


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def candles(start: str, minutes: int, price) -> Candles:
    first = ms(start)
    stamps = np.arange(first, first + minutes * MIN, MIN, dtype=np.int64)
    closes = np.array([price(i) if callable(price) else price for i in range(minutes)], dtype=np.float64)
    return Candles(stamps, closes)


def test_split_periods_keeps_the_remainder_in_the_last_period():
    assert split_periods(date(2026, 7, 1), date(2026, 8, 29)) == [
        (date(2026, 7, 1), date(2026, 7, 21)), (date(2026, 7, 21), date(2026, 8, 29)),
    ]
    assert split_periods(date(2026, 7, 1), date(2026, 7, 31)) == [(date(2026, 7, 1), date(2026, 7, 31))]
    assert split_periods(date(2026, 7, 1), date(2026, 7, 20)) == []  # 19 days: no full period
    assert len(split_periods(date(2026, 7, 1), date(2026, 8, 30))) == 3


def test_first_day_is_listing_plus_warmup_or_a_later_requested_start():
    assert first_period_day(date(2026, 6, 9)) == date(2026, 6, 13)
    assert first_period_day(date(2026, 6, 9), from_day=date(2026, 8, 1)) == date(2026, 8, 1)
    assert first_period_day(date(2026, 6, 9), from_day=date(2026, 6, 10)) == date(2026, 6, 13)


def test_last_covered_end_drops_a_partial_day():
    assert last_covered_end(ms("2026-10-04")) == date(2026, 10, 4)
    assert last_covered_end(ms("2026-10-04T13:00:00")) == date(2026, 10, 4)


def test_multiplier_text_matches_the_tester_values():
    assert [multiplier_text(bp) for bp in (70, 90, 200, 550)] == ["0.993", "0.991", "0.98", "0.945"]


def test_month_keys_and_header_order():
    assert month_keys(ms("2026-07-24"), ms("2026-09-17")) == ["2026_07", "2026_08", "2026_09"]
    assert month_keys(ms("2026-08-01"), ms("2026-09-01")) == ["2026_08"]
    columns = header(ms("2026-08-08"), ms("2026-09-17"))
    assert columns[20:25] == ["UlcerIndex", "Run id", "RunTag", "MonthlyReturn_2026_08", "MonthlyReturn_2026_09"]
    assert columns[-1] == "settings[*].mrs2.ma_long.multiplier"


def _action(action_id, ts, action, size, fee, pnl="0", post="0"):
    return SimpleNamespace(action_id=action_id, timestamp_ms=ms(ts), action=action, size=Decimal(size), fee=Decimal(fee),
                           pnl=Decimal(pnl), post_size=Decimal(post), balance=None)


def test_cycles_take_entry_from_the_maker_fee_and_exit_from_the_first_reducing_fill():
    actions = [
        _action("1", "2026-08-01T10:00:00", "opened", "0.02", "0.0002", post="0.02"),          # 0.02 * 1000 * 1e-5
        _action("2", "2026-08-01T10:01:00", "increased", "0.08", "0.0008", post="0.1"),
        _action("3", "2026-08-01T11:00:00", "decreased", "0.04", "0.000404", "0.4", "0.06"),   # exit price 1010
        _action("4", "2026-08-01T11:05:00", "closed", "0.06", "0.000606", "0.6"),
    ]
    cycle = SimpleNamespace(cycle_id="c1", open_timestamp_ms=ms("2026-08-01T10:00:00"), action_ids=("1", "2", "3", "4"), closed=True)
    fragment = SimpleNamespace(actions=actions, cycles=(cycle,))
    (result,) = cycles_from_fragment(fragment)
    assert result.open_ms == ms("2026-08-01T10:00:00") and result.exit_ms == ms("2026-08-01T11:00:00")
    assert result.entry_price == pytest.approx(1000.0) and result.exit_price == pytest.approx(1010.0)
    assert result.exit_fee_rate == pytest.approx(0.00001)
    assert qty_step_from_fragments([fragment]) == pytest.approx(0.01)


def test_period_starts_flat_and_replays_a_closed_trade_at_the_fixed_lot():
    data = candles("2026-08-01T00:00:00", 6 * 60, 100.0)
    before = Cycle(ms("2026-07-31T23:00:00"), 100.0, 1e-5, ms("2026-08-01T01:00:00"), 101.0, 1e-5)  # open at the start: dropped
    inside = Cycle(ms("2026-08-01T02:00:00"), 99.0, 1e-5, ms("2026-08-01T03:00:00"), 100.0, 1e-5)
    result = simulate_period([before, inside], data, ms("2026-08-01"), ms("2026-08-01T06:00:00"), qty_step=0.01)
    qty = math.floor(10_000 / 99.0 / 0.01 + 1e-9) * 0.01
    gross = qty * (100.0 - 99.0)
    fees = qty * 99.0 * 1e-5 + qty * 100.0 * 1e-5
    assert result.trades == [pytest.approx(gross)]
    assert result.fees == pytest.approx(fees)
    assert result.final_balance == pytest.approx(10_000 + gross - fees)


def test_open_position_is_closed_at_market_on_the_last_candle_and_pays_funding():
    data = candles("2026-08-01T06:00:00", 4 * 60, lambda i: 100.0 if i < 239 else 98.0)
    cycle = Cycle(ms("2026-08-01T07:00:00"), 100.0, 1e-5, None, None, None)
    result = simulate_period([cycle], data, ms("2026-08-01T06:00:00"), ms("2026-08-01T10:00:00"), qty_step=0.01)
    qty = 100.0
    funding = qty * 100.0 * 0.00025  # 08:00 UTC while in the position
    exit_price = 98.0 * (1 - 0.001)
    expected = 10_000 - qty * 100.0 * 1e-5 - funding + qty * (exit_price - 100.0) - qty * exit_price * 0.0002
    assert len(result.trades) == 1 and result.trades[0] == pytest.approx(qty * (exit_price - 100.0))
    assert result.final_balance == pytest.approx(expected)
    assert result.max_drawdown_percent > 0


def test_csv_row_leaves_profit_factor_and_ulcer_empty_like_the_tester():
    flat = PeriodResult(final_balance=10_000.0)
    row = csv_row(flat, start_ms=ms("2026-08-08"), end_ms=ms("2026-09-17"), name="n", report_utc="2026-10-16 00:00:00",
                  run_id=7, run_total=9, symbol="XOMUSDT", timeframe="1h", shift_bp=110, open_len=2, close_len=3)
    assert (row["TotalTrades"], row["ProfitFactor"], row["UlcerIndex"], row["PairsCount"]) == ("0", "", "", "1")
    assert (row["StartDate"], row["EndDate"], row["RunTag"]) == ("2026-08-08 00:00:00", "2026-09-17 00:00:00", "run_7_of_9")
    assert row["settings[*].mrs2.ma_long.multiplier"] == "0.989"
    winners = PeriodResult(final_balance=10_100.0, trades=[60.0, 40.0], ulcer_index=0.12345)
    row = csv_row(winners, start_ms=ms("2026-08-08"), end_ms=ms("2026-09-17"), name="n", report_utc="x", run_id=1,
                  run_total=1, symbol="XOMUSDT", timeframe="1h", shift_bp=110, open_len=2, close_len=3)
    assert (row["Win"], row["Los"], row["WinRate"], row["ProfitFactor"], row["UlcerIndex"]) == ("2", "0", "100.00", "", "0.1235")
    assert (row["TotalPnL"], row["TotalPnLPercent"]) == ("100.00", "1.00")


def test_write_csv_uses_the_tester_byte_format(tmp_path: Path):
    path = tmp_path / "reports_history.csv"
    write_csv(path, ["a", "b"], [{"a": "1", "b": ""}])
    assert path.read_bytes() == b"a,b\n1,\n"
    with pytest.raises(ValueError):
        write_csv(path, ["a"], [{"a": "1,2"}])
