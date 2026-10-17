from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from mrs3.mrs2_simulator import PeriodResult
from mrs3.period_csv import (
    PeriodCsvError,
    Point,
    csv_bytes,
    csv_row,
    first_period_day,
    grid,
    header,
    ms_of,
    multiplier_text,
    run_snapshot,
    split_periods,
    strategy_document,
    build_tester_config,
    write_period_folder,
)

REPO = Path(__file__).parents[1]


def test_split_periods_keeps_the_remainder_in_the_last_period():
    assert split_periods(date(2026, 7, 1), date(2026, 8, 29)) == [
        (date(2026, 7, 1), date(2026, 7, 21)), (date(2026, 7, 21), date(2026, 8, 29)),
    ]
    assert split_periods(date(2026, 7, 1), date(2026, 7, 20)) == []
    assert len(split_periods(date(2026, 7, 1), date(2026, 8, 30))) == 3


def test_first_day_is_listing_plus_warmup_or_a_later_requested_start():
    assert first_period_day(date(2026, 6, 9)) == date(2026, 6, 13)
    assert first_period_day(date(2026, 6, 9), from_day=date(2026, 8, 1)) == date(2026, 8, 1)


def test_grid_and_multipliers_match_the_tester_values():
    assert len(grid()) == 3780
    assert [multiplier_text(bp) for bp in (70, 90, 200, 550)] == ["0.993", "0.991", "0.98", "0.945"]


def test_header_matches_the_tester_column_order():
    columns = header(ms_of(date(2026, 7, 24)), ms_of(date(2026, 9, 17)))
    reference = ("ReportUtc,NameComment,StartDate,EndDate,PairsCount,InitialBalance,FinalBalance,TotalPnL,"
                 "TotalPnLPercent,TotalTrades,Win,Los,WinRate,Fills,MaxDrawdown,MaxDrawdownPercent,TotalFees,"
                 "PositionAvgPercent,PositionMaxPercent,ProfitFactor,UlcerIndex,Run id,RunTag,MonthlyReturn_2026_07,"
                 "MonthlyReturn_2026_08,MonthlyReturn_2026_09,settings[*].basic.symbol,settings[*].basic.time_frame,"
                 "settings[*].mrs2.ma_close_long.len,settings[*].mrs2.ma_long.len,settings[*].mrs2.ma_long.multiplier")
    assert ",".join(columns) == reference


def test_csv_row_formats_like_the_tester():
    start, end = ms_of(date(2026, 8, 8)), ms_of(date(2026, 9, 17))
    flat = csv_row(PeriodResult(final_balance=10_000.0), start_ms=start, end_ms=end, name="n", report_utc="u", run_id=7,
                   run_total=9, symbol="XOMUSDT", point=Point("1h", 110, 2, 3))
    assert (flat["TotalTrades"], flat["ProfitFactor"], flat["UlcerIndex"], flat["PairsCount"]) == ("0", "", "", "1")
    assert (flat["StartDate"], flat["RunTag"], flat["settings[*].mrs2.ma_long.multiplier"]) == (
        "2026-08-08 00:00:00", "run_7_of_9", "0.989")
    winners = PeriodResult(final_balance=10_100.0, trades=[60.0, 40.0], ulcer_index=0.12345,
                           position_percents=[99.0, 100.5, 98.0, 99.5], monthly_returns={"2026_08": 0.4, "2026_09": 0.6})
    row = csv_row(winners, start_ms=start, end_ms=end, name="n", report_utc="u", run_id=1, run_total=1,
                  symbol="XOMUSDT", point=Point("1h", 110, 2, 3))
    assert (row["Win"], row["Los"], row["WinRate"], row["ProfitFactor"], row["UlcerIndex"]) == ("2", "0", "100.00", "", "0.1235")
    assert (row["PositionAvgPercent"], row["PositionMaxPercent"]) == ("98.50", "100.50")
    assert (row["MonthlyReturn_2026_08"], row["MonthlyReturn_2026_09"]) == ("0.40", "0.60")


def test_csv_bytes_use_the_tester_byte_format():
    assert csv_bytes(["a", "b"], [{"a": "1", "b": ""}]) == b"a,b\n1,\n"
    with pytest.raises(PeriodCsvError):
        csv_bytes(["a"], [{"a": "1,2"}])


def test_snapshot_and_tester_config_describe_the_fixed_lot_run():
    template = json.loads((REPO / "templates/strategies/source-v6-mrs2/long.json").read_text(encoding="utf-8"))
    strategy = strategy_document(template, "XOMUSDT")
    assert strategy["basic"]["symbol"] == "XOMUSDT" and strategy["basic"]["use_fix"] is True
    assert strategy["basic"]["my_fix_balance"] == 10_000.0 and strategy["exchange"]["use_upnl"] is False
    assert (strategy["mrs2"]["ma_long"]["type"], strategy["mrs2"]["ma_long"]["source"]) == ("SMA", "ohlc4")
    config_template = json.loads((REPO / "templates/tester/mrs2/config_tester_long.json").read_text(encoding="utf-8"))
    config = build_tester_config(config_template, "XOMUSDT", date(2026, 9, 20), date(2026, 10, 16), "tst")
    assert (config["StartDate"], config["EndDate"], config["WarmupDays"], config["InitialBalance"]) == (
        "2026-09-20T00:00:00", "2026-10-16T00:00:00", 4, 10_000.0)
    assert config["LimitOrderVolumeCheck"] is False and config["report"]["enable_html_report"] is False
    assert config["order_limit_fill_plus_tick"] is True        # ADR-0077 standard
    assert config["reports_history"]["ulcer_index"] is True
    mining = {entry["name"].split("]")[-1]: entry for entry in config["parameter_mining"]}
    assert len(mining[".mrs2.ma_long.multiplier"]["values"]) == 15
    assert mining[".basic.symbol"]["values"] == ["XOMUSDT"]
    assert mining[".basic.time_frame"]["values"] == ["15m", "30m", "45m", "1h", "2h", "3h", "4h"]
    assert (mining[".mrs2.ma_long.len"]["start"], mining[".mrs2.ma_long.len"]["end"]) == (2.0, 7.0)
    snapshot = run_snapshot(strategy, config)
    assert list(snapshot) == ["settings", "program_config", "tester_config"]  # reference run_snapshot keys
    assert snapshot["settings"] == [strategy] and snapshot["tester_config"] == config


def test_period_folder_is_written_once(tmp_path: Path):
    folder = tmp_path / "XOMUSDT" / "2026-08-08_2026-08-28"
    write_period_folder(folder, {"reports_history.csv": b"x\n"})
    assert (folder / "reports_history.csv").read_bytes() == b"x\n"
    with pytest.raises(PeriodCsvError):
        write_period_folder(folder, {"reports_history.csv": b"y\n"})
    assert sorted(p.name for p in folder.parent.iterdir()) == [folder.name]
