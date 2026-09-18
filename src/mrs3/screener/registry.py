"""Read the liquidity registry's pair universe and upsert screener findings into it.

The registry workbook (`input/bybit_tradfi_liquidity.xlsx` by default) is
owned by a separate liquidity-calculation process: its `Пары`/`По дням`/
`Оборот 30д`/`Методика` sheets are read-only to the screener. The screener
owns exactly one sheet, `Скрининг`, and only ever updates its own columns
there — the manual `Финальное решение`/`Дата финального решения`/
`Комментарий` columns are never written by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile

import pandas as pd
from openpyxl import load_workbook

from .errors import ScreenerEvaluationError

PAIRS_SHEET = "Пары"
SCREENING_SHEET = "Скрининг"
_SYMBOL_COLUMN = "Пара"
_LISTING_DATE_COLUMN = "Дата листинга на Bybit (UTC)"

SCREENING_HEADER = (
    "Пара",
    "Сторона",
    "Вердикт",
    "BIG_SHIFT",
    "Хороших точек всего",
    "Хороших точек со сдвигом >=1%",
    "Окно: начало",
    "Окно: конец",
    "Дата скрининга",
    "Финальное решение",
    "Дата финального решения",
    "Комментарий",
)


@dataclass(frozen=True, slots=True)
class ScreeningRow:
    symbol: str
    side: str
    verdict: str
    big_shift: bool
    n_good: int
    n_good_big_shift: int
    window_start: str
    window_end: str


def read_registry_listing_dates(
    path: Path, symbols: tuple[str, ...]
) -> dict[str, pd.Timestamp]:
    """Read symbol -> UTC listing date from the registry's read-only `Пары` sheet.

    Only rows matching `symbols` are validated/parsed: an unrelated malformed
    or duplicate row elsewhere in the (manually maintained) registry must not
    block resolving the symbols actually being screened.
    """
    wanted = {symbol.strip().upper() for symbol in symbols}
    try:
        frame = pd.read_excel(
            path, sheet_name=PAIRS_SHEET, usecols=[_SYMBOL_COLUMN, _LISTING_DATE_COLUMN]
        )
    except (OSError, ValueError) as exc:
        raise ScreenerEvaluationError(f"cannot read liquidity registry: {exc}") from exc
    frame = frame.dropna(subset=[_SYMBOL_COLUMN, _LISTING_DATE_COLUMN]).copy()
    frame[_SYMBOL_COLUMN] = frame[_SYMBOL_COLUMN].astype(str).str.strip().str.upper()
    frame = frame.loc[frame[_SYMBOL_COLUMN].isin(wanted)]
    if frame[_SYMBOL_COLUMN].duplicated().any():
        duplicates = sorted(frame.loc[frame[_SYMBOL_COLUMN].duplicated(False), _SYMBOL_COLUMN].unique())
        raise ScreenerEvaluationError(
            f"liquidity registry has duplicate symbols in {PAIRS_SHEET!r}: {duplicates}"
        )
    try:
        listing_dates = pd.to_datetime(frame[_LISTING_DATE_COLUMN], utc=True, errors="raise")
    except (ValueError, TypeError) as exc:
        raise ScreenerEvaluationError(f"invalid listing date in liquidity registry: {exc}") from exc
    return dict(zip(frame[_SYMBOL_COLUMN], listing_dates, strict=True))


def _atomic_save_workbook(workbook, path: Path) -> None:
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".xlsx.tmp")
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        workbook.save(temp_path)
        os.replace(temp_path, path)
    except OSError as exc:
        raise ScreenerEvaluationError(
            f"cannot write liquidity registry, is it open in Excel? ({exc})"
        ) from exc
    finally:
        temp_path.unlink(missing_ok=True)


def write_screening_results(path: Path, rows: tuple[ScreeningRow, ...]) -> None:
    """Upsert `rows` into the registry's `Скрининг` sheet by (Пара, Сторона).

    Only the automatic columns (up to "Дата скрининга") are ever written;
    an existing row's manual columns are preserved untouched.
    """
    try:
        workbook = load_workbook(path)
    except (OSError, ValueError) as exc:
        raise ScreenerEvaluationError(
            f"cannot open liquidity registry, is it open in Excel? ({exc})"
        ) from exc

    if SCREENING_SHEET in workbook.sheetnames:
        sheet = workbook[SCREENING_SHEET]
        header = tuple(cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1)))
        if header != SCREENING_HEADER:
            raise ScreenerEvaluationError(
                f"{SCREENING_SHEET!r} sheet has an unexpected header: {header!r}"
            )
    else:
        sheet = workbook.create_sheet(SCREENING_SHEET)
        sheet.append(SCREENING_HEADER)

    existing_row_by_key: dict[tuple[str, str], int] = {}
    for row_index in range(2, sheet.max_row + 1):
        symbol = sheet.cell(row=row_index, column=1).value
        side = sheet.cell(row=row_index, column=2).value
        if symbol is not None and side is not None:
            existing_row_by_key[(str(symbol), str(side))] = row_index

    screened_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in rows:
        values = (
            row.symbol,
            row.side,
            row.verdict,
            "да" if row.big_shift else "нет",
            row.n_good,
            row.n_good_big_shift,
            row.window_start,
            row.window_end,
            screened_at,
        )
        key = (row.symbol, row.side)
        if key in existing_row_by_key:
            row_index = existing_row_by_key[key]
            for column, value in enumerate(values, start=1):
                sheet.cell(row=row_index, column=column, value=value)
        else:
            row_index = sheet.max_row + 1
            for column, value in enumerate(values, start=1):
                sheet.cell(row=row_index, column=column, value=value)
            existing_row_by_key[key] = row_index

    _atomic_save_workbook(workbook, path)
