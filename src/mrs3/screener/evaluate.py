"""Compute per-pair screener verdicts from a tester report folder's CSV history."""

from __future__ import annotations

import csv
import dataclasses
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import io
from pathlib import Path

import pandas as pd

from mrs3.config import AlgorithmConfig
from mrs3.eligibility import absolute_trade_floor

from .config import ScreenerConfig
from .errors import ScreenerEvaluationError
from .listing import resolve_listing_dates
from .registry import ScreeningRow, write_screening_results

_SYMBOL_COL = "settings[*].basic.symbol"
_TF_COL = "settings[*].basic.time_frame"
_LONG_MULT_COL = "settings[*].mrs2.ma_long.multiplier"
_SHORT_MULT_COL = "settings[*].mrs2.ma_short.multiplier"
_LONG_CLOSE_LEN_COL = "settings[*].mrs2.ma_close_long.len"
_SHORT_CLOSE_LEN_COL = "settings[*].mrs2.ma_close_short.len"


@dataclass(frozen=True, slots=True)
class PairVerdict:
    symbol: str
    side: str
    verdict: str
    big_shift: bool
    n_reports: int
    n_unique_combos: int
    n_good: int
    n_good_big_shift: int
    best_pnl30: Decimal | None
    best_timeframe: str | None
    best_shift_bp: int | None
    best_close_len: str | None
    best_dd_pct: Decimal | None
    effective_days: Decimal
    window_start: str
    window_end: str


def _read_reports(report_dir: Path) -> pd.DataFrame:
    paths = sorted(report_dir.glob("reports_history*.csv"))
    if not paths:
        raise ScreenerEvaluationError(f"no reports_history*.csv found under {report_dir}")
    frames = []
    for path in paths:
        try:
            frames.append(pd.read_csv(path, encoding="utf-8-sig", dtype=str))
        except (OSError, ValueError) as exc:
            raise ScreenerEvaluationError(f"cannot read {path}: {exc}") from exc
    return pd.concat(frames, ignore_index=True)


def _detect_side(columns: pd.Index) -> tuple[str, str, str]:
    has_long = _LONG_MULT_COL in columns
    has_short = _SHORT_MULT_COL in columns
    if has_long and has_short:
        raise ScreenerEvaluationError(
            "reports mix LONG and SHORT columns in the same folder"
        )
    if has_long:
        return "LONG", _LONG_MULT_COL, _LONG_CLOSE_LEN_COL
    if has_short:
        return "SHORT", _SHORT_MULT_COL, _SHORT_CLOSE_LEN_COL
    raise ScreenerEvaluationError("reports have neither a LONG nor a SHORT multiplier column")


_REQUIRED_METRIC_COLS = (
    "StartDate",
    "EndDate",
    "TotalPnLPercent",
    "MaxDrawdownPercent",
    "WinRate",
    "TotalTrades",
)


def _require_columns(frame: pd.DataFrame, multiplier_col: str, close_len_col: str) -> None:
    required = {_SYMBOL_COL, _TF_COL, multiplier_col, close_len_col, *_REQUIRED_METRIC_COLS}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ScreenerEvaluationError(f"reports are missing required columns: {missing}")


def _check_single_window(frame: pd.DataFrame) -> tuple[str, str]:
    # Compare parsed timestamps, not raw strings, so a purely cosmetic
    # formatting difference between partitioned CSV files for the same
    # logical window (e.g. from the tester writing them at slightly
    # different times) can't be mistaken for two different runs.
    try:
        parsed = list(
            zip(
                pd.to_datetime(frame["StartDate"], utc=True),
                pd.to_datetime(frame["EndDate"], utc=True),
                strict=True,
            )
        )
    except (ValueError, TypeError) as exc:
        raise ScreenerEvaluationError(f"invalid StartDate/EndDate in reports: {exc}") from exc
    windows = sorted(set(parsed))
    if len(windows) != 1:
        raise ScreenerEvaluationError(
            "reports folder mixes more than one (StartDate, EndDate) window: "
            + ", ".join(f"{start}..{end}" for start, end in windows)
        )
    start, end = windows[0]
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _effective_days(
    start_date: pd.Timestamp, end_date: pd.Timestamp, listing_date: pd.Timestamp
) -> Decimal:
    effective_start = max(start_date, listing_date)
    delta = end_date - effective_start
    # Timedelta.value is exact integer nanoseconds; dividing as Decimal avoids
    # baking float rounding noise into a value later compared against
    # threshold Decimals (pnl30 sits right on stop/good thresholds for some
    # borderline pairs).
    return Decimal(delta.value) / Decimal(86_400_000_000_000)


def _shift_bp(multiplier_raw: str, side: str) -> int:
    text = multiplier_raw.replace(",", ".") if side == "SHORT" else multiplier_raw
    multiplier = Decimal(text)
    if side == "LONG":
        basis_points = (Decimal(1) - multiplier) * Decimal(10000)
    else:
        basis_points = (multiplier - Decimal(1)) * Decimal(10000)
    return int(basis_points.to_integral_value(rounding=ROUND_HALF_UP))


def _economic_pass(
    *,
    pnl_pct: Decimal,
    dd_pct: Decimal,
    win_rate_pct: Decimal,
    trades: int,
    shift_bp: int,
    algorithm_config: AlgorithmConfig,
) -> bool:
    if dd_pct <= 0:
        return False
    if pnl_pct < algorithm_config.economic_min_pnl_pct:
        return False
    if win_rate_pct < algorithm_config.economic_min_win_rate_pct:
        return False
    if dd_pct > algorithm_config.economic_max_dd_pct:
        return False
    if (pnl_pct / dd_pct) < algorithm_config.economic_min_efficiency:
        return False
    return trades >= absolute_trade_floor(shift_bp, algorithm_config)


@dataclass(frozen=True, slots=True)
class _Point:
    timeframe: str
    shift_bp: int
    close_len: str
    pnl30: Decimal
    dd_pct: Decimal
    economic_pass: bool
    good_point: bool


def _process_values(
    *,
    timeframe: str,
    multiplier_raw: str,
    close_len: str,
    pnl_raw: str,
    dd_raw: str,
    win_rate_raw: str,
    trades_raw: str,
    side: str,
    effective_days: Decimal,
    algorithm_config: AlgorithmConfig,
    screener_config: ScreenerConfig,
) -> _Point:
    if effective_days <= 0:
        # Listing date at/after the report window's end (misconfigured
        # window, or a symbol newer than the screened period) — refuse to
        # compute a sign-flipped pnl30 instead of silently trusting it.
        raise ValueError("non-positive effective days")

    shift_bp = _shift_bp(str(multiplier_raw), side)

    pnl_pct = Decimal(str(pnl_raw))
    dd_pct = Decimal(str(dd_raw))
    win_rate_pct = Decimal(str(win_rate_raw))
    trades = int(Decimal(str(trades_raw)))

    pnl30 = pnl_pct * Decimal(30) / effective_days

    passed = _economic_pass(
        pnl_pct=pnl_pct,
        dd_pct=dd_pct,
        win_rate_pct=win_rate_pct,
        trades=trades,
        shift_bp=shift_bp,
        algorithm_config=algorithm_config,
    )
    good = passed and pnl30 >= screener_config.good_pnl30
    return _Point(
        timeframe=timeframe,
        shift_bp=shift_bp,
        close_len=close_len,
        pnl30=pnl30,
        dd_pct=dd_pct,
        economic_pass=passed,
        good_point=good,
    )


def evaluate_pairs(
    report_dir: Path,
    *,
    algorithm_config: AlgorithmConfig,
    screener_config: ScreenerConfig,
    dates_path: Path | None,
    registry_path: Path | None = None,
) -> tuple[PairVerdict, ...]:
    frame = _read_reports(report_dir)
    side, multiplier_col, close_len_col = _detect_side(frame.columns)
    _require_columns(frame, multiplier_col, close_len_col)
    if frame[_SYMBOL_COL].isna().any():
        raise ScreenerEvaluationError("reports have a blank basic.symbol value")
    start_raw, end_raw = _check_single_window(frame)
    start_date = pd.Timestamp(start_raw, tz="UTC")
    end_date = pd.Timestamp(end_raw, tz="UTC")

    frame[_SYMBOL_COL] = frame[_SYMBOL_COL].astype(str).str.strip().str.upper()
    symbols = tuple(sorted(frame[_SYMBOL_COL].unique()))
    listing = resolve_listing_dates(symbols, registry_path=registry_path, dates_path=dates_path)
    effective_days_by_symbol = {
        symbol: _effective_days(start_date, end_date, listing[symbol]) for symbol in symbols
    }

    points_by_symbol: dict[str, list[_Point]] = {symbol: [] for symbol in symbols}
    unreadable_by_symbol: dict[str, int] = {symbol: 0 for symbol in symbols}
    columns = zip(
        frame[_SYMBOL_COL],
        frame[_TF_COL],
        frame[multiplier_col],
        frame[close_len_col],
        frame["TotalPnLPercent"],
        frame["MaxDrawdownPercent"],
        frame["WinRate"],
        frame["TotalTrades"],
        strict=True,
    )
    for symbol, timeframe, multiplier_raw, close_len, pnl_raw, dd_raw, win_rate_raw, trades_raw in columns:
        try:
            point = _process_values(
                timeframe=timeframe,
                multiplier_raw=multiplier_raw,
                close_len=close_len,
                pnl_raw=pnl_raw,
                dd_raw=dd_raw,
                win_rate_raw=win_rate_raw,
                trades_raw=trades_raw,
                side=side,
                effective_days=effective_days_by_symbol[symbol],
                algorithm_config=algorithm_config,
                screener_config=screener_config,
            )
        except (ArithmeticError, ValueError, TypeError, KeyError):
            unreadable_by_symbol[symbol] += 1
            continue
        points_by_symbol[symbol].append(point)

    n_reports_by_symbol = frame[_SYMBOL_COL].value_counts()
    verdicts = []
    for symbol in symbols:
        points = points_by_symbol[symbol]
        n_reports = int(n_reports_by_symbol[symbol])
        combos = {(point.timeframe, point.shift_bp, point.close_len) for point in points}
        incomplete = (
            len(combos) != screener_config.expected_combos_per_pair
            or unreadable_by_symbol[symbol] > 0
        )

        passing = [point for point in points if point.economic_pass]
        good = [point for point in points if point.good_point]
        big_shift_good = [point for point in good if point.shift_bp >= screener_config.big_shift_bp]
        big_shift = len(big_shift_good) >= screener_config.big_min_good

        best_point = max(passing, key=lambda point: point.pnl30, default=None)

        if incomplete:
            verdict = "INCOMPLETE"
        elif best_point is None or best_point.pnl30 < screener_config.stop_best_pnl30:
            verdict = "STOP"
        elif len(good) >= screener_config.go_min_good:
            verdict = "GO"
        else:
            verdict = "CHECK"

        verdicts.append(
            PairVerdict(
                symbol=symbol,
                side=side,
                verdict=verdict,
                big_shift=big_shift,
                n_reports=n_reports,
                n_unique_combos=len(combos),
                n_good=len(good),
                n_good_big_shift=len(big_shift_good),
                best_pnl30=best_point.pnl30 if best_point is not None else None,
                best_timeframe=best_point.timeframe if best_point is not None else None,
                best_shift_bp=best_point.shift_bp if best_point is not None else None,
                best_close_len=best_point.close_len if best_point is not None else None,
                best_dd_pct=best_point.dd_pct if best_point is not None else None,
                effective_days=effective_days_by_symbol[symbol],
                window_start=start_raw,
                window_end=end_raw,
            )
        )

    verdicts.sort(
        key=lambda v: (
            {"GO": 0, "CHECK": 1, "STOP": 2, "INCOMPLETE": 3}[v.verdict],
            -v.n_good,
            v.symbol,
        )
    )
    return tuple(verdicts)


def evaluate_and_record(
    report_dir: Path,
    *,
    algorithm_config: AlgorithmConfig,
    screener_config: ScreenerConfig,
    dates_path: Path | None,
) -> tuple[PairVerdict, ...]:
    """Evaluate the report folder and, if a registry is configured, record findings."""
    verdicts = evaluate_pairs(
        report_dir,
        algorithm_config=algorithm_config,
        screener_config=screener_config,
        dates_path=dates_path,
        registry_path=screener_config.liquidity_registry_path,
    )
    if screener_config.liquidity_registry_path is not None:
        rows = tuple(
            ScreeningRow(
                symbol=verdict.symbol,
                side=verdict.side,
                verdict=verdict.verdict,
                big_shift=verdict.big_shift,
                n_good=verdict.n_good,
                n_good_big_shift=verdict.n_good_big_shift,
                window_start=verdict.window_start,
                window_end=verdict.window_end,
            )
            for verdict in verdicts
        )
        write_screening_results(screener_config.liquidity_registry_path, rows)
    return verdicts


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        # Fixed-point, never scientific notation (str(Decimal) can render
        # e.g. "3E-8"), so a plain-text/spreadsheet reader parses it as the
        # same number every time.
        return format(value, "f")
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        # Neutralize spreadsheet formula injection for genuinely
        # string-valued fields only — this branch never sees a Decimal that
        # was reformatted to a leading "-" above, since that return already
        # happened.
        return "'" + value
    return value


def export_verdicts_csv(verdicts: tuple[PairVerdict, ...]) -> bytes:
    """Render `verdicts` as UTF-8 (with BOM) CSV bytes for panel download.

    Header and row values both come from `dataclasses.fields(PairVerdict)`/
    `dataclasses.asdict`, so they can't drift out of position with each
    other the way two independently hand-written lists could.
    """
    field_names = [field.name for field in dataclasses.fields(PairVerdict)]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=field_names)
    writer.writeheader()
    for verdict in verdicts:
        row = dataclasses.asdict(verdict)
        writer.writerow({name: _csv_cell(value) for name, value in row.items()})
    return buffer.getvalue().encode("utf-8-sig")
