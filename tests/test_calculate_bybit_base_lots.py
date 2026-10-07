from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.worksheet.table import Table
import pytest

from scripts.calculate_bybit_base_lots import (
    BaseLotExportError,
    _validate_xlsx_package,
    calculate_base_lot,
    main,
    run_export,
)
from mrs3.portfolio.minute_capacity import resolve_liquidity_window


def _minute(day: date, index: int, value: str = "100") -> str:
    stamp = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000) + index * 60_000
    return f"{stamp},1,1,1,1,{value},{value},0,1\n"


def _write_daily_files(root: Path, symbol: str, anchor: datetime) -> None:
    days = resolve_liquidity_window(anchor, 6)
    target = root / symbol
    target.mkdir(parents=True, exist_ok=True)
    for day in days:
        (target / f"{symbol}{day.isoformat()}_1m.csv").write_text(
            "timestamp,open,high,low,close,volume,buy_volume,sell_volume,trades\n"
            + _minute(day, 0)
            + _minute(day, 15),
            encoding="utf-8",
        )


def _workbook(path: Path) -> None:
    workbook = Workbook()
    actual = workbook.active
    actual.title = "Actual"
    actual.append(("Pair", "Listing", "Base lot"))
    actual.append(("BTCUSDT", date(2026, 1, 1), "keep until success"))
    actual.append(("ETHUSDT", date(2026, 1, 2), 1234))
    other = workbook.create_sheet("Other")
    other["A1"] = "preserve"
    workbook.save(path)


def _empty_workbook(path: Path) -> None:
    workbook = Workbook()
    actual = workbook.active
    actual.title = "Actual"
    actual.append(("Pair", "Listing", "Base lot"))
    workbook.save(path)


def test_calculate_base_lot_uses_decimal_formula_and_floors_without_b(tmp_path: Path) -> None:
    assert calculate_base_lot("123.456", "0.75", k="9", round_down_usdt="10") == Decimal("830")
    assert calculate_base_lot(Decimal("123.456"), Decimal("0.75"), k="9", round_down_usdt="1") == Decimal("833")


def test_run_export_writes_actual_columns_atomically(tmp_path: Path) -> None:
    anchor = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)
    workbook_path = tmp_path / "liquidity.xlsx"
    data_root = tmp_path / "bybit"
    _workbook(workbook_path)
    _write_daily_files(data_root, "BTCUSDT", anchor)
    _write_daily_files(data_root, "ETHUSDT", anchor)

    result = run_export(
        workbook_path,
        data_root,
        anchor_created_at=anchor,
        fetch_day=lambda _symbol, _day: pytest.fail("existing official files should be reused"),
    )

    assert result.rows_written == 2
    assert result.errors == ()
    saved = load_workbook(workbook_path, data_only=False)
    assert saved.sheetnames == ["Actual", "Other"]
    assert saved["Actual"]["D1"].value == "\u0414\u0430\u0442\u0430 \u0430\u043a\u0442\u0443\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u0438"
    assert saved["Actual"]["C2"].value == 10
    assert saved["Actual"]["C3"].value == 10
    assert result.actualization_date == anchor.date()
    assert saved["Actual"]["D2"].value.date() == anchor.date()
    assert saved["Actual"]["D2"].number_format == "yyyy-mm-dd"
    assert saved["Actual"]["D2"].fill.fill_type is None
    assert saved["Actual"]["D3"].value.date() == anchor.date()
    assert saved["Other"]["A1"].value == "preserve"


def test_symbol_error_is_written_to_column_d_and_other_symbols_continue(tmp_path: Path) -> None:
    anchor = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)
    workbook_path = tmp_path / "liquidity.xlsx"
    data_root = tmp_path / "bybit"
    _workbook(workbook_path)
    _write_daily_files(data_root, "BTCUSDT", anchor)

    result = run_export(
        workbook_path,
        data_root,
        anchor_created_at=anchor,
        fetch_day=lambda _symbol, _day: b"invalid archive",
    )

    assert result.rows_written == 1
    assert len(result.errors) == 1
    saved = load_workbook(workbook_path, data_only=False)
    assert saved["Actual"]["C2"].value == 10
    assert saved["Actual"]["D2"].value.date() == anchor.date()
    assert saved["Actual"]["C3"].value is None
    assert str(saved["Actual"]["D3"].value).startswith("ERROR: ETHUSDT:")
    assert saved["Actual"]["D3"].fill.fgColor.rgb in {"00FFC7CE", "FFC7CE"}


def test_successful_rerun_clears_previous_error_format(tmp_path: Path) -> None:
    anchor = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)
    workbook_path = tmp_path / "liquidity.xlsx"
    data_root = tmp_path / "bybit"
    _workbook(workbook_path)
    _write_daily_files(data_root, "BTCUSDT", anchor)

    failed = run_export(
        workbook_path,
        data_root,
        anchor_created_at=anchor,
        fetch_day=lambda _symbol, _day: b"invalid archive",
    )
    assert len(failed.errors) == 1

    _write_daily_files(data_root, "ETHUSDT", anchor)
    recovered = run_export(
        workbook_path,
        data_root,
        anchor_created_at=anchor,
        fetch_day=lambda _symbol, _day: pytest.fail("all files should be present on rerun"),
    )
    assert recovered.errors == ()
    saved = load_workbook(workbook_path, data_only=False)
    assert saved["Actual"]["C3"].value == 10
    assert saved["Actual"]["D3"].value.date() == anchor.date()
    assert saved["Actual"]["D3"].number_format == "yyyy-mm-dd"
    assert saved["Actual"]["D3"].fill.fill_type is None


def test_no_symbols_fails_without_replacing_workbook(tmp_path: Path) -> None:
    workbook_path = tmp_path / "empty.xlsx"
    _empty_workbook(workbook_path)
    before = workbook_path.read_bytes()

    with pytest.raises(BaseLotExportError, match="contains no symbols"):
        run_export(workbook_path, tmp_path / "bybit")

    assert workbook_path.read_bytes() == before


@pytest.mark.parametrize("feature", ["chart", "table"])
def test_unsupported_workbook_features_fail_without_replacement(tmp_path: Path, feature: str) -> None:
    workbook_path = tmp_path / f"unsupported-{feature}.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Actual"
    sheet.append(("Pair", "Listing", "Base lot"))
    sheet.append(("BTCUSDT", date(2026, 1, 1), None))
    if feature == "chart":
        sheet.append(("x", 1, 2))
        chart = BarChart()
        chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=2), titles_from_data=True)
        sheet.add_chart(chart, "E2")
    else:
        table = Table(displayName="Pairs", ref="A1:C2")
        sheet.add_table(table)
    workbook.save(workbook_path)
    before = workbook_path.read_bytes()

    with pytest.raises(BaseLotExportError, match="ordinary xlsx"):
        run_export(workbook_path, tmp_path / "bybit")

    assert workbook_path.read_bytes() == before


def test_legacy_vml_comment_package_is_supported(tmp_path: Path) -> None:
    from openpyxl.comments import Comment

    workbook_path = tmp_path / "comment.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Actual"
    sheet.append(("Pair", "Listing", "Base lot"))
    sheet.append(("BTCUSDT", date(2026, 1, 1), None))
    sheet["A1"].comment = Comment("legacy note", "test")
    workbook.save(workbook_path)

    _validate_xlsx_package(workbook_path)


@pytest.mark.parametrize("part", ["xl/media/image1.png", "xl/pivotCache/pivotCacheDefinition1.xml", "xl/vbaProject.bin"])
def test_unsupported_package_part_fails_without_replacement(tmp_path: Path, part: str) -> None:
    workbook_path = tmp_path / "unsupported-part.xlsx"
    _empty_workbook(workbook_path)
    with ZipFile(workbook_path, "a") as package:
        package.writestr(part, b"unsupported")
    before = workbook_path.read_bytes()

    with pytest.raises(BaseLotExportError, match="ordinary xlsx"):
        run_export(workbook_path, tmp_path / "bybit")

    assert workbook_path.read_bytes() == before


def test_cli_prints_errors_to_stdout_and_returns_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--input", str(tmp_path / "missing.xlsx")]) == 1
    assert "ERROR:" in capsys.readouterr().out


def test_cli_reports_partial_failure_after_saving_workbook(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    anchor = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)
    workbook_path = tmp_path / "liquidity.xlsx"
    data_root = tmp_path / "bybit"
    _workbook(workbook_path)
    _write_daily_files(data_root, "BTCUSDT", anchor)
    invalid_dir = data_root / "ETHUSDT"
    invalid_dir.mkdir(parents=True, exist_ok=True)
    for day in resolve_liquidity_window(anchor, 6):
        (invalid_dir / f"ETHUSDT{day.isoformat()}_1m.csv").write_text("invalid", encoding="utf-8")

    assert main([
        "--input", str(workbook_path),
        "--data-root", str(data_root),
        "--anchor-created-at", anchor.isoformat(),
    ]) == 1
    output = capsys.readouterr().out
    assert "ERROR: ETHUSDT:" in output
    assert "FAILED: 1 symbol(s) failed" in output
    saved = load_workbook(workbook_path, data_only=False)
    assert str(saved["Actual"]["D3"].value).startswith("ERROR:")


def test_cli_success_path_has_no_report_option_or_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    anchor = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)
    workbook_path = tmp_path / "liquidity.xlsx"
    data_root = tmp_path / "bybit"
    _workbook(workbook_path)
    _write_daily_files(data_root, "BTCUSDT", anchor)
    _write_daily_files(data_root, "ETHUSDT", anchor)

    assert main([
        "--input", str(workbook_path),
        "--data-root", str(data_root),
        "--anchor-created-at", anchor.isoformat(),
    ]) == 0
    output = capsys.readouterr().out
    assert "OK: wrote 2 base lots" in output
    assert "REPORT:" not in output
