from __future__ import annotations

import json
import math

import duckdb
import pandas as pd

from .loader import LEGACY_EVENT_IDS_HASH
from .pipeline import PipelineInput


_REQUIRED_METRICS = {
    "TotalPnLPercent",
    "MaxDrawdownPercent",
    "TotalTrades",
    "Win",
    "Los",
    "WinRate",
    "ProfitFactor",
}


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def _finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"published surface metric {name} must be finite") from error
    if not math.isfinite(number):
        raise ValueError(f"published surface metric {name} must be finite")
    return number


def _count(value: object, name: str) -> int:
    number = _finite(value, name)
    if number < 0 or not number.is_integer():
        raise ValueError(f"published surface metric {name} must be a non-negative integer")
    return int(number)


def load_published_surface(
    analysis_connection: duckdb.DuckDBPyConnection, surface_id: str
) -> PipelineInput:
    """Read immutable point facts from the analysis DB without opening the source DB."""
    period = analysis_connection.execute(
        "select period_start_utc, period_end_utc, side from surfaces where surface_id=?",
        [surface_id],
    ).fetchone()
    if period is None:
        raise ValueError("unknown published surface")
    rows = analysis_connection.execute(
        """select canonical_point_key, point_event_count, metrics_json
             from surface_points where surface_id=? order by canonical_point_key""",
        [surface_id],
    ).fetchall()
    if not rows:
        raise ValueError("published surface has no points")

    start, end, surface_side = _utc(period[0]), _utc(period[1]), str(period[2])
    points: list[dict[str, object]] = []
    for run_id, (key, event_count, metrics_json) in enumerate(rows, start=1):
        parts = str(key).split("|")
        if len(parts) != 6:
            raise ValueError("published surface point key must have six fields")
        symbol, side, timeframe, shift_text, open_text, close_text = parts
        if side != surface_side or not symbol or not timeframe:
            raise ValueError("published surface point is outside its surface scope")
        try:
            shift_bp, open_ma, close_ma = int(shift_text), int(open_text), int(close_text)
        except ValueError as error:
            raise ValueError("published surface point key has invalid grid fields") from error
        if shift_bp < 0 or open_ma <= 0 or close_ma <= 0:
            raise ValueError("published surface point key has invalid grid fields")
        try:
            metrics = json.loads(str(metrics_json))
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("published surface metrics are invalid JSON") from error
        if not isinstance(metrics, dict) or _REQUIRED_METRICS.difference(metrics):
            raise ValueError("published surface metrics are incomplete")
        trades = _count(metrics["TotalTrades"], "TotalTrades")
        if trades != _count(event_count, "point_event_count"):
            raise ValueError("published point_event_count must equal TotalTrades")
        multiplier = 1 + (-shift_bp if side == "LONG" else shift_bp) / 10_000
        points.append(
            {
                "point_id": str(key),
                "run_id": run_id,
                "symbol": symbol,
                "side": side,
                "timeframe": timeframe,
                "shift_bp": shift_bp,
                "shift_pct": shift_bp / 100,
                "open_ma": open_ma,
                "close_ma": close_ma,
                "multiplier": multiplier,
                "pnl_pct": _finite(metrics["TotalPnLPercent"], "TotalPnLPercent"),
                "dd_pct": _finite(metrics["MaxDrawdownPercent"], "MaxDrawdownPercent"),
                "win_rate_pct": _finite(metrics["WinRate"], "WinRate"),
                "profit_factor": (
                    None
                    if metrics["ProfitFactor"] is None
                    else _finite(metrics["ProfitFactor"], "ProfitFactor")
                ),
                "trades": trades,
                "wins": _count(metrics["Win"], "Win"),
                "losses": _count(metrics["Los"], "Los"),
                "event_mode": "legacy_trades_proxy",
                "point_event_count": trades,
                "event_ids_hash": LEGACY_EVENT_IDS_HASH,
                "report_start": start,
                "report_end": end,
            }
        )
    return PipelineInput(surface_id, pd.DataFrame(points))
