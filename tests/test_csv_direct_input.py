from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import zipfile

import pytest

from mrs3.csv_direct_input import DirectCsvError, load_csv_direct
from tests.csv_direct_support import csv_text, snapshot, report_row, write_tester_zip

DATES = {"XYZUSDT": "2026-01-01"}


def _load(tmp_path: Path, rows, snap=None, **kwargs):
    path = write_tester_zip(tmp_path / "bundle.zip", rows, snap)
    return load_csv_direct([path], listing_dates=kwargs.pop("listing_dates", DATES), **kwargs)


def test_point_identity_and_source_v6_metric_definitions(tmp_path):
    row = report_row(pnl="317.98", dd_pct="11.48", trades=173, wins=128, losses=44, fills=401)
    output = _load(tmp_path, [row])
    point = output.frame.iloc[0]
    assert point["point_id"] == "XYZUSDT|LONG|30m|30|SMA|ohlc4|2|SMA|ohlc4|3"
    # PnL% from TotalPnL/InitialBalance, not the two-decimal TotalPnLPercent.
    assert point["pnl_pct"] == Decimal("31.798")
    assert point["dd_pct"] == Decimal("11.48")
    # One breakeven trade: Win + Los < TotalTrades is valid, as in Source v6.
    assert (point["trades"], point["wins"], point["losses"]) == (173, 128, 44)
    assert point["win_rate_pct"] == Decimal(100) * 128 / 172
    assert output.total_trades == 173 and output.total_fills == 401
    assert "_event_ids" not in output.frame.columns
    assert "pretest_ab" not in output.frame.columns


def test_activity_is_exact_monthly_rate_over_effective_days(tmp_path):
    row = report_row(trades=4, wins=3, losses=1, window=("2026-07-10 00:00:00", "2026-10-08 00:00:00"))
    point = _load(tmp_path, [row]).frame.iloc[0]
    assert point["effective_days"] == Decimal(90)
    assert point["activity_30d"] == Decimal(4) / Decimal(3)


def test_activity_is_capped_at_trades_for_short_history(tmp_path):
    # Listed 9 days before the end: Source v6 counts all 3 trades as "last 30 days".
    row = report_row(trades=3, wins=3, losses=0, window=("2026-09-20 00:00:00", "2026-10-08 00:00:00"))
    point = _load(tmp_path, [row], listing_dates={"XYZUSDT": "2026-09-29"}).frame.iloc[0]
    assert point["effective_days"] == Decimal(9)
    assert point["activity_30d"] == Decimal(3)


def test_tester_win_rate_may_count_breakevens_either_way(tmp_path):
    row = report_row(trades=10, wins=6, losses=3)
    row["WinRate"] = "60.00"  # wins / TotalTrades
    assert _load(tmp_path, [row]).frame.iloc[0]["win_rate_pct"] == Decimal(100) * 6 / 9
    row["WinRate"] = "50.00"
    with pytest.raises(DirectCsvError, match="WinRate disagrees"):
        _load(tmp_path, [row])


def test_pnl_must_agree_with_reported_percent(tmp_path):
    row = report_row(pnl="317.98")
    row["TotalPnLPercent"] = "31.90"
    with pytest.raises(DirectCsvError, match="disagrees with TotalPnLPercent"):
        _load(tmp_path, [row])


@pytest.mark.parametrize("use_fix,use_upnl", [(False, False), (True, True)])
def test_only_fixed_lot_runs_are_admitted(tmp_path, use_fix, use_upnl):
    with pytest.raises(DirectCsvError, match="only fixed-lot runs"):
        _load(tmp_path, [report_row()], snapshot(use_fix=use_fix, use_upnl=use_upnl))


def test_swept_fixed_lot_column_overrides_snapshot(tmp_path):
    row = {**report_row(), "settings[*].basic.use_fix": "True"}
    output = _load(tmp_path, [row], snapshot(use_fix=False))
    assert len(output.frame) == 1


def test_fixed_lot_unknown_without_snapshot(tmp_path):
    path = tmp_path / "reports_history.csv"
    path.write_text(csv_text([report_row()]), encoding="utf-8")
    identity = {("XYZUSDT", "LONG"): ("SMA", "ohlc4", "SMA", "ohlc4")}
    with pytest.raises(DirectCsvError, match="fixed-lot sizing is unknown"):
        load_csv_direct([path], listing_dates=DATES, ma_identity=identity)
    output = load_csv_direct([path], listing_dates=DATES, ma_identity=identity, require_fixed_lot=False)
    assert len(output.frame) == 1


def test_missing_snapshot_requires_known_ma_identity(tmp_path):
    path = tmp_path / "reports_history.csv"
    path.write_text(csv_text([report_row()]), encoding="utf-8")
    with pytest.raises(DirectCsvError, match="MA type/source unavailable"):
        load_csv_direct([path], listing_dates=DATES, require_fixed_lot=False)


def test_shared_snapshot_template_is_opt_in(tmp_path):
    rows = [report_row(symbol="ABCUSDT")]
    with pytest.raises(DirectCsvError, match="MA type/source unavailable"):
        _load(tmp_path, rows, listing_dates={"ABCUSDT": "2026-01-01"})
    output = _load(tmp_path, rows, listing_dates={"ABCUSDT": "2026-01-01"}, allow_shared_snapshot_template=True)
    assert output.frame.iloc[0]["symbol"] == "ABCUSDT"


def test_inconsistent_counts_are_rejected(tmp_path):
    with pytest.raises(DirectCsvError, match="inconsistent TotalTrades"):
        _load(tmp_path, [report_row(trades=3, wins=3, losses=1)])
    with pytest.raises(DirectCsvError, match="inconsistent TotalTrades"):
        _load(tmp_path, [report_row(trades=3, wins=3, losses=0, fills=2)])


def test_zero_trades_is_valid_observation(tmp_path):
    row = report_row(pnl="0.00", trades=0, wins=0, losses=0, profit_factor="")
    output = _load(tmp_path, [row])
    assert output.total_trades == 0
    assert output.frame.iloc[0]["activity_30d"] == 0
    assert output.frame.iloc[0]["profit_factor"] is None


def test_duplicate_point_and_mixed_windows_are_rejected(tmp_path):
    with pytest.raises(DirectCsvError, match="duplicate point"):
        _load(tmp_path, [report_row(), report_row(run=2)])
    other = report_row(open_ma=4, window=("2026-07-14 00:00:00", "2026-09-11 00:00:00"))
    with pytest.raises(DirectCsvError, match="distinct report windows"):
        _load(tmp_path, [report_row(), other])


def test_invalid_identity_fields_are_rejected(tmp_path):
    with pytest.raises(DirectCsvError, match="invalid symbol"):
        _load(tmp_path, [{**report_row(), "settings[*].basic.symbol": ""}])
    with pytest.raises(DirectCsvError, match="exact integer shift_bp"):
        _load(tmp_path, [{**report_row(), "settings[*].mrs2.ma_long.multiplier": "0.99347"}])
    with pytest.raises(DirectCsvError, match="exactly one active"):
        _load(tmp_path, [{**report_row(), "settings[*].mrs2.ma_short.multiplier": "1.02"}])
    with pytest.raises(DirectCsvError, match="aggregated multi-symbol"):
        _load(tmp_path, [{**report_row(), "PairsCount": "9"}])
    with pytest.raises(DirectCsvError, match="non-positive effective test duration"):
        _load(tmp_path, [report_row()], listing_dates={"XYZUSDT": "2026-09-12"})


def test_incomplete_grid_is_rejected(tmp_path):
    rows = [report_row(open_ma=2, close_ma=2), report_row(open_ma=3, close_ma=3, run=2)]
    with pytest.raises(DirectCsvError, match="incomplete rectangular grid"):
        _load(tmp_path, rows)


def test_duplicate_column_headers_are_rejected(tmp_path):
    lines = csv_text([report_row()]).splitlines()
    lines[0] += ",TotalTrades"
    lines[1] += ",240"
    path = tmp_path / "dup.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("reports_history.csv", "\n".join(lines) + "\n")
        archive.writestr("run_snapshot_x.json", json.dumps(snapshot()))
    with pytest.raises(DirectCsvError, match="duplicate CSV headers"):
        load_csv_direct([path], listing_dates=DATES)


def test_digest_is_stable_under_row_order_and_run_tags(tmp_path):
    first, second = report_row(open_ma=2), report_row(open_ma=3, run=2)
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    a = write_tester_zip(tmp_path / "a" / "x.zip", [first, second])
    b = write_tester_zip(tmp_path / "b" / "x.zip", [{**second, "RunTag": "other"}, first])
    assert load_csv_direct([a], listing_dates=DATES).input_digest == load_csv_direct([b], listing_dates=DATES).input_digest


def test_loader_never_reads_html_members(tmp_path, monkeypatch):
    path = write_tester_zip(tmp_path / "bundle.zip", [report_row()])
    original = zipfile.ZipFile.read

    def blocked_html(self, member, *args, **kwargs):
        if str(member).endswith(".html"):
            raise AssertionError("HTML member must not be read")
        return original(self, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", blocked_html)
    assert len(load_csv_direct([path], listing_dates=DATES).frame) == 1


def test_snapshot_describes_only_its_own_run(tmp_path):
    run_a, run_b = tmp_path / "runs" / "a", tmp_path / "runs" / "b"
    run_a.mkdir(parents=True); run_b.mkdir(parents=True)
    (run_a / "reports_history.csv").write_text(csv_text([report_row()]), encoding="utf-8")
    (run_a / "run_snapshot_a.json").write_text(json.dumps(snapshot()), encoding="utf-8")
    (run_b / "reports_history.csv").write_text(csv_text([report_row(symbol="ABCUSDT")]), encoding="utf-8")
    dates = {"XYZUSDT": "2026-01-01", "ABCUSDT": "2026-01-01"}
    # Run b has no snapshot: run a's MA identity must not be guessed for it.
    with pytest.raises(DirectCsvError, match="MA type/source unavailable for ABCUSDT"):
        load_csv_direct([tmp_path / "runs"], listing_dates=dates, allow_shared_snapshot_template=True, require_fixed_lot=False)


def test_selected_csv_file_reads_its_run_snapshot(tmp_path):
    (tmp_path / "reports_history.csv").write_text(csv_text([report_row()]), encoding="utf-8")
    (tmp_path / "run_snapshot_x.json").write_text(json.dumps(snapshot()), encoding="utf-8")
    output = load_csv_direct([tmp_path / "reports_history.csv"], listing_dates=DATES)
    assert output.frame.iloc[0]["point_id"].endswith("|SMA|ohlc4|2|SMA|ohlc4|3")


def test_errors_name_files_without_local_paths(tmp_path):
    folder = tmp_path / "private-location"
    folder.mkdir()
    (folder / "reports_history.csv").write_text(csv_text([report_row(trades=1, wins=2, losses=0)]), encoding="utf-8")
    (folder / "run_snapshot_x.json").write_text(json.dumps(snapshot()), encoding="utf-8")
    with pytest.raises(DirectCsvError) as error:
        load_csv_direct([folder], listing_dates=DATES)
    assert "private-location/reports_history.csv:2" in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_csv_below_its_run_folder_uses_the_nearest_ancestor_snapshot(tmp_path):
    path = tmp_path / "nested.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("run/reports/reports_history.csv", csv_text([report_row()]))
        archive.writestr("run/run_snapshot_run.json", json.dumps(snapshot()))
    assert len(load_csv_direct([path], listing_dates=DATES).frame) == 1
