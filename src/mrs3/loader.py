from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

from .config import AlgorithmConfig
from .models import InputAudit, Side


class InputError(ValueError):
    """Raised when source files cannot produce an auditable point grid."""


EVENT_MODES = frozenset({"legacy_trades_proxy", "real_independent_events"})
LEGACY_EVENT_IDS_HASH = "LEGACY_PROXY_NO_EVENT_IDS"


def _exact_integer(
    values: pd.Series,
    field: str,
    *,
    non_negative: bool = False,
    missing_as_zero: bool = False,
) -> pd.Series:
    parsed: list[int] = []
    for value in values:
        if value is None or value is pd.NA:
            if missing_as_zero:
                parsed.append(0)
                continue
            raise InputError(f"{field} must be finite exact integers")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise InputError(f"{field} must be finite exact integers") from exc
        if not number.is_finite() or number != number.to_integral_value():
            raise InputError(f"{field} must be finite exact integers")
        integer = int(number)
        if non_negative and integer < 0:
            raise InputError(f"{field} must be finite exact integers")
        if not -(2**63) <= integer < 2**63:
            raise InputError(f"{field} must be finite exact integers")
        parsed.append(integer)
    return pd.Series(parsed, index=values.index, dtype="int64")


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
    try:
        if path.suffix.casefold() == ".csv":
            raw = pd.read_csv(path, dtype=str)
            required = {"ticker", "launch"}
            missing = sorted(required.difference(raw.columns))
            if missing:
                raise InputError(f"CSV listing dates are missing columns: {missing}")
            frame = raw.loc[:, ["ticker", "launch"]].rename(
                columns={"ticker": "symbol", "launch": "listing_date"}
            )
            if frame.empty or frame.isna().any().any() or frame["symbol"].str.strip().eq("").any():
                raise InputError("CSV listing dates require non-empty ticker and launch values")
        else:
            frame = pd.read_excel(
                path, header=None, usecols=[0, 1], names=["symbol", "listing_date"]
            ).dropna(subset=["symbol", "listing_date"])
        frame = frame.copy()
        frame["symbol"] = frame["symbol"].astype(str).str.strip()
        frame["listing_date"] = pd.to_datetime(frame["listing_date"], errors="raise", utc=True)
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError(f"invalid listing dates: {exc}") from exc
    if frame.empty or frame["symbol"].eq("").any():
        raise InputError("listing dates require non-empty symbols")
    if frame["symbol"].duplicated().any():
        symbols = sorted(frame.loc[frame["symbol"].duplicated(False), "symbol"].unique())
        raise InputError(f"duplicate listing dates: {symbols}")
    return dict(zip(frame["symbol"], frame["listing_date"], strict=True))


def load_listing_dates(path: Path) -> dict[str, pd.Timestamp]:
    """Load the explicit UTC listing-date snapshot used by analysis adapters."""
    return _load_listing_dates(path)


def load_points(
    csv_path: Path | pd.DataFrame,
    dates_path: Path,
    side: Side,
    config: AlgorithmConfig,
) -> tuple[pd.DataFrame, InputAudit]:
    raw = pd.read_csv(csv_path) if isinstance(csv_path, Path) else csv_path.copy()
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
    if data.empty:
        raise InputError("no usable data rows")
    listing_dates = _load_listing_dates(dates_path)

    points = pd.DataFrame(
        {
            "run_id": _exact_integer(data[columns["run_id"]], "run_id"),
            "symbol": data[columns["symbol"]].astype(str).str.strip(),
            "side": side.value,
            "timeframe": data[columns["timeframe"]].astype(str).str.strip(),
            "open_ma": _exact_integer(data[columns["open_ma"]], "open_ma"),
            "close_ma": _exact_integer(data[columns["close_ma"]], "close_ma"),
            "multiplier": pd.to_numeric(data[columns["multiplier"]], errors="raise").astype(float),
            "report_start": pd.to_datetime(data[columns["report_start"]], errors="raise", utc=True),
            "report_end": pd.to_datetime(data[columns["report_end"]], errors="raise", utc=True),
            "pnl_pct": pd.to_numeric(data[columns["pnl_pct"]], errors="raise").astype(float),
            "trades": _exact_integer(data[columns["trades"]], "trades"),
            "wins": _exact_integer(
                data[columns["wins"]], "wins", non_negative=True, missing_as_zero=True
            ),
            "losses": _exact_integer(
                data[columns["losses"]], "losses", non_negative=True, missing_as_zero=True
            ),
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
    if len(points[["report_start", "report_end"]].drop_duplicates()) != 1:
        raise InputError("input must contain exactly one report period")

    key_columns = ["symbol", "side", "timeframe", "shift_bp", "open_ma", "close_ma"]
    duplicate_mask = points.duplicated(key_columns, keep=False)
    duplicate_count = int(duplicate_mask.sum())
    if duplicate_count:
        sample = points.loc[duplicate_mask, key_columns].head(3).to_dict("records")
        raise InputError(f"duplicate parameter cell: {sample}")

    points["point_id"] = points[key_columns].astype(str).agg("|".join, axis=1)
    metadata_columns = {"event_mode", "point_event_count", "event_ids_hash"}
    declared_metadata = "event_mode" in data
    if declared_metadata:
        event_modes = data["event_mode"]
        normalized_modes = event_modes.astype(str).str.strip()
        modes = sorted(set(normalized_modes))
        if (
            event_modes.isna().any()
            or normalized_modes.eq("").any()
            or len(modes) != 1
            or modes[0] not in EVENT_MODES
        ):
            raise InputError(f"input must declare exactly one known event_mode: {modes}")
        points["event_mode"] = modes[0]
    else:
        partial_metadata = sorted(metadata_columns.intersection(data.columns))
        if partial_metadata:
            raise InputError("event_mode is required when event metadata is present")
        points["event_mode"] = "legacy_trades_proxy"

    if not declared_metadata:
        points["point_event_count"] = points["trades"]
        points["event_ids_hash"] = LEGACY_EVENT_IDS_HASH
    else:
        if "point_event_count" not in data:
            raise InputError("point_event_count is required with event_mode")
        points["point_event_count"] = _exact_integer(
            data["point_event_count"], "point_event_count", non_negative=True
        )
        if points["event_mode"].iloc[0] == "legacy_trades_proxy" and not points[
            "point_event_count"
        ].eq(points["trades"]).all():
            raise InputError("legacy point_event_count must equal TotalTrades")

        if "event_ids_hash" not in data:
            raise InputError("event_ids_hash is required with event_mode")
        event_ids_hash = data["event_ids_hash"]
        normalized_hashes = event_ids_hash.astype(str).str.strip()
        if event_ids_hash.isna().any() or normalized_hashes.eq("").any():
            raise InputError("event_ids_hash must be non-empty")
        if points["event_mode"].iloc[0] == "legacy_trades_proxy" and not normalized_hashes.eq(
            LEGACY_EVENT_IDS_HASH
        ).all():
            raise InputError(f"legacy_trades_proxy requires {LEGACY_EVENT_IDS_HASH}")
        points["event_ids_hash"] = normalized_hashes
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
