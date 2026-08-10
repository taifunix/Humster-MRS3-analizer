from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

from .config import AlgorithmConfig
from .models import InputAudit, Side


class InputError(ValueError):
    """Raised when source files cannot produce an auditable point grid."""


def normalize_shift(
    side: Side,
    multiplier: str | int | float | Decimal,
    tolerance_bp: str | int | float | Decimal = Decimal("0.000001"),
) -> int:
    """Convert an entry multiplier to integer basis points of price deviation."""
    try:
        value = Decimal(str(multiplier))
    except InvalidOperation as exc:
        raise InputError(f"invalid multiplier: {multiplier!r}") from exc
    deviation = (Decimal("1") - value) if side is Side.LONG else (value - Decimal("1"))
    basis_points = deviation * Decimal("10000")
    rounded = basis_points.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    tolerance = Decimal(str(tolerance_bp))
    if tolerance < 0:
        raise InputError("shift grid tolerance cannot be negative")
    if abs(basis_points - rounded) > tolerance:
        raise InputError(f"multiplier does not map to the configured shift grid: {multiplier!r}")
    result = int(rounded)
    if result < 0:
        raise InputError(f"negative shift derived from multiplier: {multiplier!r}")
    return result


def _load_listing_dates(path: Path) -> dict[str, pd.Timestamp]:
    frame = pd.read_excel(path, header=None, usecols=[0, 1], names=["symbol", "listing_date"])
    frame = frame.dropna(subset=["symbol", "listing_date"]).copy()
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame["listing_date"] = pd.to_datetime(frame["listing_date"], errors="raise")
    if frame["symbol"].duplicated().any():
        symbols = sorted(frame.loc[frame["symbol"].duplicated(False), "symbol"].unique())
        raise InputError(f"duplicate listing dates: {symbols}")
    return dict(zip(frame["symbol"], frame["listing_date"], strict=True))


def load_points(
    csv_path: Path,
    dates_path: Path,
    side: Side,
    config: AlgorithmConfig,
) -> tuple[pd.DataFrame, InputAudit]:
    raw = pd.read_csv(csv_path)
    columns = {**config.base_columns, **config.side_columns[side]}
    missing_columns = [column for column in columns.values() if column not in raw.columns]
    if missing_columns:
        raise InputError(f"missing columns: {missing_columns}")

    essential_keys = [
        "symbol",
        "timeframe",
        "open_ma",
        "close_ma",
        "multiplier",
        "report_start",
        "report_end",
        "pnl_pct",
        "trades",
        "win_rate_pct",
        "dd_pct",
        "run_id",
    ]
    service_mask = raw[[columns[key] for key in essential_keys]].isna().any(axis=1)
    service_rows = int(service_mask.sum())
    data = raw.loc[~service_mask].copy()
    listing_dates = _load_listing_dates(dates_path)

    points = pd.DataFrame(
        {
            "run_id": pd.to_numeric(data[columns["run_id"]], errors="raise").astype("int64"),
            "symbol": data[columns["symbol"]].astype(str).str.strip(),
            "side": side.value,
            "timeframe": data[columns["timeframe"]].astype(str).str.strip(),
            "open_ma": pd.to_numeric(data[columns["open_ma"]], errors="raise").astype("int64"),
            "close_ma": pd.to_numeric(data[columns["close_ma"]], errors="raise").astype("int64"),
            "multiplier": pd.to_numeric(data[columns["multiplier"]], errors="raise").astype(float),
            "report_start": pd.to_datetime(data[columns["report_start"]], errors="raise"),
            "report_end": pd.to_datetime(data[columns["report_end"]], errors="raise"),
            "pnl_pct": pd.to_numeric(data[columns["pnl_pct"]], errors="raise").astype(float),
            "trades": pd.to_numeric(data[columns["trades"]], errors="raise").astype("int64"),
            "wins": pd.to_numeric(data[columns["wins"]], errors="coerce").fillna(0).astype("int64"),
            "losses": pd.to_numeric(data[columns["losses"]], errors="coerce").fillna(0).astype("int64"),
            "win_rate_pct": pd.to_numeric(data[columns["win_rate_pct"]], errors="raise").astype(float),
            "dd_pct": pd.to_numeric(data[columns["dd_pct"]], errors="raise").astype(float),
            "profit_factor": pd.to_numeric(data[columns["profit_factor"]], errors="coerce").astype(float),
        }
    )
    points["shift_bp"] = [
        normalize_shift(side, value, config.grid_tolerance_bp)
        for value in data[columns["multiplier"]]
    ]
    points["shift_pct"] = points["shift_bp"] / 100.0
    points["listing_date"] = points["symbol"].map(listing_dates)
    missing_dates = sorted(points.loc[points["listing_date"].isna(), "symbol"].unique())
    if missing_dates:
        raise InputError(f"missing listing dates: {missing_dates}")
    invalid_period = points["report_end"] <= points["report_start"]
    if invalid_period.any():
        raise InputError("report EndDate must be later than StartDate")

    key_columns = ["symbol", "side", "timeframe", "shift_bp", "open_ma", "close_ma"]
    duplicate_mask = points.duplicated(key_columns, keep=False)
    duplicate_count = int(duplicate_mask.sum())
    if duplicate_count:
        sample = points.loc[duplicate_mask, key_columns].head(3).to_dict("records")
        raise InputError(f"duplicate parameter cell: {sample}")

    points["point_id"] = points[key_columns].astype(str).agg("|".join, axis=1)
    points["event_mode"] = "legacy_trades_proxy"
    points["point_event_count"] = points["trades"]
    points["event_ids_hash"] = "LEGACY_PROXY_NO_EVENT_IDS"
    ordered_columns = [
        "point_id",
        "run_id",
        "symbol",
        "side",
        "timeframe",
        "shift_bp",
        "shift_pct",
        "open_ma",
        "close_ma",
        "multiplier",
        "pnl_pct",
        "dd_pct",
        "win_rate_pct",
        "profit_factor",
        "trades",
        "wins",
        "losses",
        "event_mode",
        "point_event_count",
        "event_ids_hash",
        "report_start",
        "report_end",
        "listing_date",
    ]
    points = points[ordered_columns].sort_values(key_columns, kind="mergesort").reset_index(drop=True)
    audit = InputAudit(
        source_rows=len(raw),
        normalized_rows=len(points),
        service_rows=service_rows,
        symbols=int(points["symbol"].nunique()),
        timeframes=int(points["timeframe"].nunique()),
        duplicate_cells=0,
    )
    return points, audit
