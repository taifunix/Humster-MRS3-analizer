"""Export the optimizer base lot from official Bybit minute liquidity data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import argparse
from copy import copy
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Callable, Iterable
import zipfile

from openpyxl import load_workbook
from openpyxl.styles import PatternFill

if __package__ in {None, ""}:  # Allow ``python scripts/calculate_bybit_base_lots.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mrs3.portfolio.minute_capacity import (  # noqa: E402
    MinuteCapacityError,
    backfill_missing_days,
    calculate_minute_capacity,
    fetch_bybit_trade_archive,
    resolve_liquidity_window,
)


DEFAULT_WORKBOOK = Path(r"D:\SHARE\!MN\hamster\MRS-Analizer\Input\bybit_tradfi_liquidity.xlsx")
DEFAULT_DATA_ROOT = Path(r"D:\SHARE\!MN\hamster\hb\tester\data\bybit")
DEFAULT_SHEET = "Actual"
DEFAULT_K = Decimal("9")
DEFAULT_ROUND_DOWN_USDT = Decimal("10")
DEFAULT_PUBLICATION_LAG_HOURS = 6
DEFAULT_WORKERS = 16
ACTUALIZATION_HEADER = "Дата актуализации"
ERROR_FILL = PatternFill(fill_type="solid", fgColor="FFC7CE")
_CLEAR_FILL = PatternFill(fill_type=None)
_SYMBOL = re.compile(r"[A-Z0-9]+\Z", re.ASCII)


class BaseLotExportError(ValueError):
    """A user-facing input, data, or publication error."""


@dataclass(frozen=True, slots=True)
class ExportRow:
    symbol: str
    v25: Decimal
    a15: Decimal
    base_lot_usdt: Decimal
    content_digest: str
    file_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ExportResult:
    workbook_path: Path
    rows_written: int
    rows: tuple[ExportRow, ...]
    errors: tuple[ExportError, ...]
    actualization_date: date


@dataclass(frozen=True, slots=True)
class ExportError:
    symbol: str
    message: str


def _decimal(value: object, field: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (bool, float)):
        raise BaseLotExportError(f"{field} must be an exact decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise BaseLotExportError(f"{field} must be an exact decimal") from error
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise BaseLotExportError(f"{field} has an invalid value")
    return result


def calculate_base_lot(
    v25: object,
    a15: object,
    *,
    k: object = DEFAULT_K,
    round_down_usdt: object = DEFAULT_ROUND_DOWN_USDT,
) -> Decimal:
    """Return ``floor_to_step(K * V25 * A15)`` using Decimal arithmetic."""
    turnover = _decimal(v25, "v25", positive=True)
    activity = _decimal(a15, "a15", nonnegative=True)
    coefficient = _decimal(k, "k", positive=True)
    step = _decimal(round_down_usdt, "round_down_usdt", positive=True)
    precision = max(28, sum(len(value.as_tuple().digits) for value in (coefficient, turnover, activity)) + 8)
    with localcontext() as context:
        context.prec = precision
        context.rounding = ROUND_HALF_EVEN
        raw = coefficient * turnover * activity
    value_digits = int("".join(str(digit) for digit in raw.as_tuple().digits) or "0")
    step_digits = int("".join(str(digit) for digit in step.as_tuple().digits) or "0")
    value_exponent = raw.as_tuple().exponent
    step_exponent = step.as_tuple().exponent
    if value_exponent >= step_exponent:
        numerator = value_digits * 10 ** (value_exponent - step_exponent)
        denominator = step_digits
    else:
        numerator = value_digits
        denominator = step_digits * 10 ** (step_exponent - value_exponent)
    quotient = numerator // denominator
    with localcontext() as context:
        context.prec = max(28, len(str(quotient)) + len(step.as_tuple().digits) + 8)
        context.rounding = ROUND_HALF_EVEN
        return Decimal(quotient) * step


def _parse_symbol(value: object, row_number: int, sheet_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BaseLotExportError(f"{sheet_name} row {row_number}: symbol must be uppercase ASCII letters/digits")
    symbol = value.strip()
    if not _SYMBOL.fullmatch(symbol):
        raise BaseLotExportError(f"{sheet_name} row {row_number}: symbol must be uppercase ASCII letters/digits")
    return symbol


def _validate_ordinary_workbook(workbook, path: Path) -> None:
    if path.suffix.lower() != ".xlsx":
        raise BaseLotExportError("only ordinary .xlsx workbooks are supported")
    for sheet in workbook.worksheets:
        if getattr(sheet, "_charts", ()) or getattr(sheet, "_images", ()) or getattr(sheet, "tables", {}):
            raise BaseLotExportError("only ordinary xlsx workbooks without charts, images, or tables are supported")


def _validate_xlsx_package(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as package:
            names = package.namelist()
    except (OSError, zipfile.BadZipFile) as error:
        raise BaseLotExportError(f"cannot inspect workbook package {path}: {error}") from error
    unsupported_prefixes = (
        "xl/charts/", "xl/media/", "xl/tables/", "xl/pivottables/", "xl/pivotcache/",
        "xl/embeddings/", "xl/slicers/", "xl/slicercaches/", "xl/ctrlprops/",
    )
    unsupported = any(name.lower().startswith(unsupported_prefixes) for name in names)
    unsupported = unsupported or any(
        name.lower().startswith("xl/drawings/")
        and not name.lower().endswith(".vml")
        and not (name.lower().startswith("xl/drawings/_rels/") and "vmldrawing" in name.lower())
        for name in names
    )
    unsupported = unsupported or any(name.lower().endswith("vbaproject.bin") for name in names)
    if unsupported:
        raise BaseLotExportError("only ordinary xlsx workbooks without charts, images, or tables are supported")


def _read_symbol_rows(path: Path, sheet_name: str) -> tuple[tuple[str, ...], dict[str, tuple[int, ...]]]:
    _validate_xlsx_package(path)
    try:
        workbook = load_workbook(path, read_only=False, data_only=False)
    except (OSError, ValueError) as error:
        raise BaseLotExportError(f"cannot open workbook {path}: {error}") from error
    try:
        _validate_ordinary_workbook(workbook, path)
        if sheet_name not in workbook.sheetnames:
            raise BaseLotExportError(f"workbook is missing sheet {sheet_name!r}")
        sheet = workbook[sheet_name]
        symbols: list[str] = []
        rows: dict[str, list[int]] = {}
        for row_number, row in enumerate(sheet.iter_rows(min_row=2, max_col=1, values_only=True), start=2):
            value = row[0] if row else None
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            symbol = _parse_symbol(value, row_number, sheet_name)
            if symbol not in rows:
                symbols.append(symbol)
                rows[symbol] = []
            rows[symbol].append(row_number)
        return tuple(symbols), {symbol: tuple(indices) for symbol, indices in rows.items()}
    finally:
        workbook.close()


def _ensure_data(
    data_root: Path,
    symbol: str,
    days: Iterable,
    *,
    workers: int,
    fetch_day: Callable[[str, object], bytes],
) -> None:
    try:
        result = backfill_missing_days(
            data_root,
            symbol,
            days,
            fetch_day=fetch_day,
            enabled=True,
            max_workers=workers,
        )
    except (MinuteCapacityError, OSError, ValueError) as error:
        raise BaseLotExportError(f"{symbol}: cannot validate Bybit minute data: {error}") from error
    if result.missing or result.failed:
        failed = ", ".join(f"{day}: {reason}" for day, reason in result.failed.items())
        missing = ", ".join(day.isoformat() for day in result.missing)
        detail = "; ".join(item for item in (failed, f"missing {missing}" if missing else "") if item)
        raise BaseLotExportError(f"{symbol}: official Bybit data unavailable ({detail})")


def _atomic_save_workbook(workbook, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
        workbook.save(temporary)
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            mode = None
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    except OSError as error:
        raise BaseLotExportError(f"cannot atomically save workbook {target}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_export(
    workbook_path: str | Path = DEFAULT_WORKBOOK,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    *,
    sheet_name: str = DEFAULT_SHEET,
    anchor_created_at: datetime | None = None,
    k: object = DEFAULT_K,
    round_down_usdt: object = DEFAULT_ROUND_DOWN_USDT,
    publication_lag_hours: int = DEFAULT_PUBLICATION_LAG_HOURS,
    workers: int = DEFAULT_WORKERS,
    fetch_day: Callable[[str, object], bytes] | None = None,
) -> ExportResult:
    """Calculate and publish all base lots, replacing the workbook once."""
    workbook_path = Path(workbook_path)
    data_root = Path(data_root)
    if not isinstance(sheet_name, str) or not sheet_name:
        raise BaseLotExportError("sheet_name must be non-empty text")
    if type(workers) is not int or not 1 <= workers <= 16:
        raise BaseLotExportError("workers must be an integer from 1 through 16")
    if type(publication_lag_hours) is not int or not 0 <= publication_lag_hours <= 48:
        raise BaseLotExportError("publication_lag_hours must be an integer from 0 through 48")
    coefficient = _decimal(k, "k", positive=True)
    step = _decimal(round_down_usdt, "round_down_usdt", positive=True)
    if anchor_created_at is None:
        anchor_created_at = datetime.now(timezone.utc)
    if not isinstance(anchor_created_at, datetime) or anchor_created_at.tzinfo is None or anchor_created_at.utcoffset() is None:
        raise BaseLotExportError("anchor_created_at must be timezone-aware")
    anchor_created_at = anchor_created_at.astimezone(timezone.utc)
    actualization_date = anchor_created_at.date()
    try:
        source_stat = workbook_path.stat()
        source_signature = source_stat.st_mtime_ns, source_stat.st_size
    except OSError as error:
        raise BaseLotExportError(f"cannot inspect workbook {workbook_path}: {error}") from error
    days = resolve_liquidity_window(anchor_created_at, publication_lag_hours)
    symbols, symbol_rows = _read_symbol_rows(workbook_path, sheet_name)
    if not symbols:
        raise BaseLotExportError(f"{sheet_name} sheet contains no symbols")
    fetch = fetch_day or fetch_bybit_trade_archive
    rows: list[ExportRow] = []
    errors: list[ExportError] = []
    for symbol in symbols:
        try:
            _ensure_data(data_root, symbol, days, workers=workers, fetch_day=fetch)
            capacity = calculate_minute_capacity(
                data_root,
                symbol,
                round_down_usdt=step,
                publication_lag_hours=publication_lag_hours,
                lot_model=True,
                anchor_created_at=anchor_created_at,
            )
            features = capacity.lot_features
            if features is None:
                raise BaseLotExportError("liquidity features are unavailable")
            rows.append(ExportRow(symbol, features.v25, features.a15, calculate_base_lot(features.v25, features.a15, k=coefficient, round_down_usdt=step), features.content_digest, features.file_hashes))
        except (BaseLotExportError, MinuteCapacityError, OSError, ValueError, ArithmeticError) as error:
            message = str(error)
            prefix = f"{symbol}: "
            errors.append(ExportError(symbol, message[len(prefix):] if message.startswith(prefix) else message))
            continue

    try:
        workbook = load_workbook(workbook_path, data_only=False)
    except (OSError, ValueError) as error:
        raise BaseLotExportError(f"cannot open workbook {workbook_path}: {error}") from error
    try:
        current_stat = workbook_path.stat()
        current_signature = current_stat.st_mtime_ns, current_stat.st_size
        if current_signature != source_signature:
            raise BaseLotExportError("workbook changed while the export was running; no values were published")
        _validate_ordinary_workbook(workbook, workbook_path)
        if sheet_name not in workbook.sheetnames:
            raise BaseLotExportError(f"workbook is missing sheet {sheet_name!r}")
        sheet = workbook[sheet_name]
        by_symbol = {row.symbol: row.base_lot_usdt for row in rows}
        header = sheet.cell(row=1, column=3)
        actualization_header = sheet.cell(row=1, column=4, value=ACTUALIZATION_HEADER)
        actualization_header._style = copy(header._style)
        actualization_header.number_format = header.number_format
        if sheet.column_dimensions["C"].width is not None:
            sheet.column_dimensions["D"].width = sheet.column_dimensions["C"].width
        for symbol, row_numbers in symbol_rows.items():
            for row_number in row_numbers:
                cell = sheet.cell(row=row_number, column=3)
                date_cell = sheet.cell(row=row_number, column=4)
                if symbol in by_symbol:
                    cell.value = by_symbol[symbol]
                    date_cell.value = actualization_date
                    date_cell.number_format = "yyyy-mm-dd"
                    date_cell.fill = _CLEAR_FILL
                else:
                    error = next(item.message for item in errors if item.symbol == symbol)
                    cell.value = None
                    date_cell.value = f"ERROR: {symbol}: {error}"
                    date_cell.number_format = "General"
                    date_cell.fill = ERROR_FILL
        _atomic_save_workbook(workbook, workbook_path)
    finally:
        workbook.close()

    successful_row_count = sum(len(symbol_rows[row.symbol]) for row in rows)
    return ExportResult(workbook_path, successful_row_count, tuple(rows), tuple(errors), actualization_date)


def _anchor(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("anchor must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("anchor must include a timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", dest="workbook_path", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--sheet", dest="sheet_name", default=DEFAULT_SHEET)
    parser.add_argument("--anchor-created-at", type=_anchor, default=None)
    parser.add_argument("--k", default=DEFAULT_K)
    parser.add_argument("--round-down-usdt", default=DEFAULT_ROUND_DOWN_USDT)
    parser.add_argument("--publication-lag-hours", type=int, default=DEFAULT_PUBLICATION_LAG_HOURS)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        result = run_export(**vars(build_parser().parse_args(argv)))
    except Exception as error:  # CLI boundary: every failure is explicit and nonzero.
        print(f"ERROR: {type(error).__name__}: {error}")
        return 1
    for error in result.errors:
        print(f"ERROR: {error.symbol}: {error.message}")
    if result.errors:
        print(f"FAILED: {len(result.errors)} symbol(s) failed; wrote {result.rows_written} base lots to {result.workbook_path}")
        return 1
    print(f"OK: wrote {result.rows_written} base lots to {result.workbook_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
