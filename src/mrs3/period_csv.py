"""Period CSVs in the tester's `reports_history` format from the MRS2 simulator; the last period goes to the tester.

Contract: docs/specs/2026-10-16-mrs2-period-csv-generator.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Iterable, Mapping, Sequence

from .mrs2_simulator import DAY_MS, MinuteBars, PeriodResult, RunSettings, load_minutes, month_starts, simulate_mrs2, simulate_period

GENERATOR_VERSION = "mrs2-sim-2"
SHIFTS_BP = (70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550)
TIMEFRAMES = ("15m", "30m", "45m", "1h", "2h", "3h", "4h")
MA_LENGTHS = (2, 3, 4, 5, 6, 7)
PERIOD_DAYS = 20

BASE_COLUMNS = (
    "ReportUtc", "NameComment", "StartDate", "EndDate", "PairsCount", "InitialBalance", "FinalBalance", "TotalPnL",
    "TotalPnLPercent", "TotalTrades", "Win", "Los", "WinRate", "Fills", "MaxDrawdown", "MaxDrawdownPercent",
    "TotalFees", "PositionAvgPercent", "PositionMaxPercent", "ProfitFactor", "UlcerIndex", "Run id", "RunTag",
)
SETTING_COLUMNS = (
    "settings[*].basic.symbol", "settings[*].basic.time_frame", "settings[*].mrs2.ma_close_long.len",
    "settings[*].mrs2.ma_long.len", "settings[*].mrs2.ma_long.multiplier",
)


class PeriodCsvError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Point:
    timeframe: str
    shift_bp: int
    open_len: int
    close_len: int


def grid(timeframes: Sequence[str] = TIMEFRAMES) -> list[Point]:
    return [Point(tf, shift, o, c) for tf in timeframes for shift in SHIFTS_BP for o in MA_LENGTHS for c in MA_LENGTHS]


# -- periods ------------------------------------------------------------------


def split_periods(first_day: date, end_day: date, period_days: int = PERIOD_DAYS) -> list[tuple[date, date]]:
    """[first_day, end_day) cut into the most full periods; the remainder joins the last one (P2)."""
    count = (end_day - first_day).days // period_days
    periods = []
    for index in range(count):
        start = first_day + timedelta(days=index * period_days)
        periods.append((start, end_day if index == count - 1 else start + timedelta(days=period_days)))
    return periods


def first_period_day(listing: date, warmup_days: int = 4, from_day: date | None = None) -> date:
    """Listing + warm-up (the tester's warm-up must lie on real candles), or a later requested start."""
    first = listing + timedelta(days=warmup_days)
    return max(first, from_day) if from_day is not None else first


def ms_of(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


# -- CSV ----------------------------------------------------------------------------


def multiplier_text(shift_bp: int) -> str:
    return f"{(10_000 - shift_bp) / 10_000:.4f}".rstrip("0").rstrip(".")


def header(start_ms: int, end_ms: int) -> list[str]:
    return [*BASE_COLUMNS, *(f"MonthlyReturn_{key}" for key, _ in month_starts(start_ms, end_ms)), *SETTING_COLUMNS]


def _money(value: float) -> str:
    text = f"{value:.2f}"
    return "0.00" if text == "-0.00" else text


def csv_row(result: PeriodResult, *, start_ms: int, end_ms: int, name: str, report_utc: str, run_id: int, run_total: int,
            symbol: str, point: Point, settings: RunSettings = RunSettings()) -> dict[str, str]:
    lot = settings.lot
    trades = result.trades
    wins = sum(1 for gross in trades if gross > 0)
    gross_profit = sum(gross for gross in trades if gross > 0)
    gross_loss = -sum(gross for gross in trades if gross < 0)
    pnl = result.final_balance - lot
    stamp = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    entries = result.position_percents[0::2]
    row = {
        "ReportUtc": report_utc, "NameComment": name, "StartDate": stamp(start_ms), "EndDate": stamp(end_ms),
        "PairsCount": "1", "InitialBalance": _money(lot), "FinalBalance": _money(result.final_balance),
        "TotalPnL": _money(pnl), "TotalPnLPercent": _money(pnl / lot * 100), "TotalTrades": str(len(trades)),
        "Win": str(wins), "Los": str(len(trades) - wins),
        "WinRate": _money(wins / len(trades) * 100 if trades else 0.0), "Fills": str(len(trades)),
        "MaxDrawdown": _money(result.max_drawdown), "MaxDrawdownPercent": _money(result.max_drawdown_percent),
        "TotalFees": _money(result.fees),
        "PositionAvgPercent": _money(sum(entries) / len(entries) if entries else 0.0),
        "PositionMaxPercent": _money(max(result.position_percents, default=0.0)),
        "ProfitFactor": f"{gross_profit / gross_loss:.2f}" if gross_loss else "",
        "UlcerIndex": f"{result.ulcer_index:.4f}" if trades else "",
        "Run id": str(run_id), "RunTag": f"run_{run_id}_of_{run_total}",
    }
    for key, _ in month_starts(start_ms, end_ms):
        row[f"MonthlyReturn_{key}"] = _money(result.monthly_returns.get(key, 0.0))
    row.update({
        "settings[*].basic.symbol": symbol, "settings[*].basic.time_frame": point.timeframe,
        "settings[*].mrs2.ma_close_long.len": str(point.close_len), "settings[*].mrs2.ma_long.len": str(point.open_len),
        "settings[*].mrs2.ma_long.multiplier": multiplier_text(point.shift_bp),
    })
    return row


def csv_bytes(columns: Sequence[str], rows: Iterable[Mapping[str, str]]) -> bytes:
    """The tester's reports_history.csv: no BOM, LF, comma-separated, no quoting (values never contain commas)."""
    lines = [",".join(columns)]
    for row in rows:
        values = [row[column] for column in columns]
        if any("," in value or "\n" in value for value in values):
            raise PeriodCsvError("a CSV value contains a separator")
        lines.append(",".join(values))
    return ("\n".join(lines) + "\n").encode("utf-8")


# -- tester documents -------------------------------------------------------------------


def strategy_document(template: Mapping, symbol: str, settings: RunSettings = RunSettings()) -> dict:
    """The MRS2 LONG strategy as run: fixed lot, SMA ohlc4 MAs, the pair symbol."""
    document = json.loads(json.dumps(template))
    basic = document["basic"]
    basic.update(symbol=symbol, use_long=True, use_short=False, use_fix=True, my_fix_balance=settings.lot, more_data=False)
    document["exchange"]["use_upnl"] = False
    document["name"] = f"mrs2_long_{symbol}"
    return document


def build_tester_config(template: Mapping, symbol: str, start: date, end: date, name: str,
                  timeframes: Sequence[str] = TIMEFRAMES, settings: RunSettings = RunSettings()) -> dict:
    """Current-format tester config for a CSV-only run of the full grid of one pair and period."""
    document = json.loads(json.dumps(template))
    document.update(name_comment=name, InitialBalance=settings.lot, StartDate=f"{start.isoformat()}T00:00:00",
                    EndDate=f"{end.isoformat()}T00:00:00", WarmupDays=settings.warmup_days, MakerFee=settings.maker_fee,
                    TakerFee=settings.taker_fee, SlippagePercent=settings.slippage, FundingRate=settings.funding_rate,
                    FundingIntervalHours=settings.funding_interval_hours, UpdateData=True, LimitOrderVolumeCheck=False,
                    order_limit_fill_plus_tick=settings.limit_fill_plus_tick)
    document["report"]["enable_html_report"] = False
    for entry in document["parameter_mining"]:
        field = entry["name"]
        if field.endswith("ma_long.multiplier"):
            entry["values"] = [multiplier_text(bp) for bp in SHIFTS_BP]
        elif field.endswith("ma_long.len") or field.endswith("ma_close_long.len"):
            entry.update(start=float(MA_LENGTHS[0]), end=float(MA_LENGTHS[-1]), step=1.0, values=[])
        elif field.endswith("time_frame"):
            entry["values"] = list(timeframes)
        elif field.endswith("basic.symbol"):
            entry["values"] = [symbol]
    return document


def run_snapshot(strategy: Mapping, config: Mapping) -> dict:
    return {"settings": [strategy], "program_config": {}, "tester_config": config}


# -- generation -----------------------------------------------------------------------


def simulate_rows(minutes: MinuteBars, symbol: str, start: date, end: date, *, tick: float, qty_step: float, name: str,
                  report_utc: str, points: Sequence[Point], settings: RunSettings = RunSettings()) -> list[dict[str, str]]:
    start_ms, end_ms = ms_of(start), ms_of(end)
    rows = []
    for run_id, point in enumerate(points, 1):
        cycles = simulate_mrs2(minutes, start_ms, end_ms, timeframe=point.timeframe, open_len=point.open_len,
                               open_mult=(10_000 - point.shift_bp) / 10_000, close_len=point.close_len, tick=tick,
                               settings=settings)
        result = simulate_period(cycles, minutes, start_ms, end_ms, qty_step=qty_step, settings=settings)
        rows.append(csv_row(result, start_ms=start_ms, end_ms=end_ms, name=name, report_utc=report_utc, run_id=run_id,
                            run_total=len(points), symbol=symbol, point=point, settings=settings))
    return rows


def write_period_folder(folder: Path, files: Mapping[str, bytes]) -> None:
    """Write a period folder atomically: a sibling temporary folder renamed into place; an existing one is kept."""
    if folder.exists():
        raise PeriodCsvError(f"period folder already exists: {folder.name}")
    temporary = folder.with_name(folder.name + ".tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    try:
        for name, data in files.items():
            (temporary / name).write_bytes(data)
        os.replace(temporary, folder)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def journal_append(path: Path, record: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def has_warmup_data(minutes: MinuteBars, start: date, warmup_days: int) -> bool:
    """The tester's warm-up must lie on real candles: some trade on the first warm-up day."""
    first = ms_of(start) - warmup_days * DAY_MS
    stamps = minutes.timestamps
    return bool(len(stamps)) and int(stamps[0]) < first + DAY_MS


__all__ = [
    "GENERATOR_VERSION", "Point", "PeriodCsvError", "csv_bytes", "csv_row", "first_period_day", "grid", "header",
    "has_warmup_data", "journal_append", "load_minutes", "ms_of", "multiplier_text", "run_snapshot", "sha256",
    "simulate_rows", "split_periods", "strategy_document", "build_tester_config", "write_period_folder",
]
