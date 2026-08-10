from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

import numpy as np
import pandas as pd

from .config import AlgorithmConfig


def base_rate(timeframe: str, config: AlgorithmConfig) -> Decimal:
    try:
        return config.base_rates[timeframe]
    except KeyError as exc:
        raise ValueError(f"uncalibrated timeframe: {timeframe}") from exc


def shift_factor(shift_bp: int, config: AlgorithmConfig) -> Decimal | None:
    for maximum_bp, factor in config.shift_factors:
        if shift_bp <= maximum_bp:
            return factor
    return None


def absolute_trade_floor(shift_bp: int, config: AlgorithmConfig) -> int:
    if shift_bp <= config.absolute_floor_boundary_bp:
        return config.absolute_floor_at_or_below
    return config.absolute_floor_above


def _relative_min_trades(
    effective_days: float,
    timeframe: str,
    factor: Decimal | None,
    config: AlgorithmConfig,
) -> int | None:
    if factor is None:
        return None
    value = Decimal(str(effective_days)) * base_rate(timeframe, config) * factor
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _reject_reasons(row: pd.Series, config: AlgorithmConfig) -> tuple[str, ...]:
    reasons: list[str] = []
    if Decimal(str(row["pnl_pct"])) <= config.economic_min_pnl_pct:
        reasons.append(
            "REJECT_PNL_NONPOSITIVE"
            if config.economic_min_pnl_pct == 0
            else "REJECT_PNL_MINIMUM"
        )
    if row["dd_pct"] <= 0:
        reasons.append("REJECT_DD_NONPOSITIVE")
    if Decimal(str(row["win_rate_pct"])) < config.economic_min_win_rate_pct:
        reasons.append("REJECT_WIN_RATE")
    if Decimal(str(row["dd_pct"])) > config.economic_max_dd_pct:
        reasons.append("REJECT_DRAWDOWN")
    if pd.isna(row["efficiency"]) or Decimal(str(row["efficiency"])) < config.economic_min_efficiency:
        reasons.append("REJECT_EFFICIENCY")
    if not bool(row["history_pass"]):
        reasons.append("OBSERVE_ONLY_HISTORY")
    if pd.isna(row["required_min_trades"]):
        reasons.append("REJECT_SAMPLE_UNCALIBRATED")
    elif not bool(row["standalone_sample_pass"]):
        reasons.append("REJECT_SAMPLE")
    if int(row["point_event_count"]) < config.min_point_events:
        reasons.append("INSUFFICIENT_POINT_EVENTS")
    return tuple(reasons)


def annotate_eligibility(points: pd.DataFrame, config: AlgorithmConfig) -> pd.DataFrame:
    out = points.copy()
    if "point_event_count" not in out:
        out["point_event_count"] = out["trades"]
    out["point_event_count"] = pd.to_numeric(out["point_event_count"], errors="raise").astype("int64")
    out["effective_start"] = out[["report_start", "listing_date"]].max(axis=1)
    out["effective_days"] = (
        (out["report_end"] - out["effective_start"]).dt.total_seconds() / 86400.0
    ).clip(lower=0)
    out["history_pass"] = out["effective_days"].map(
        lambda value: Decimal(str(value)) >= config.history_min_days
    )
    positive_dd = out["dd_pct"] > 0
    out["efficiency"] = np.where(positive_dd, out["pnl_pct"] / out["dd_pct"], np.nan)
    out["pnl_dd5_theoretical"] = np.where(
        positive_dd,
        out["pnl_pct"] * float(config.target_dd_pct) / out["dd_pct"],
        np.nan,
    )
    out["absolute_min_trades"] = out["shift_bp"].map(
        lambda value: absolute_trade_floor(int(value), config)
    )
    out["relative_min_trades"] = [
        _relative_min_trades(
            float(days),
            str(timeframe),
            shift_factor(int(shift_bp), config),
            config,
        )
        for days, timeframe, shift_bp in zip(
            out["effective_days"], out["timeframe"], out["shift_bp"], strict=True
        )
    ]
    out["required_min_trades"] = [
        np.nan if relative is None else max(int(relative), int(absolute))
        for relative, absolute in zip(
            out["relative_min_trades"], out["absolute_min_trades"], strict=True
        )
    ]
    out["standalone_sample_pass"] = [
        False if pd.isna(required) else int(trades) >= int(required)
        for trades, required in zip(out["trades"], out["required_min_trades"], strict=True)
    ]
    out["economic_pass"] = (
        (out["pnl_pct"] > float(config.economic_min_pnl_pct))
        & (out["dd_pct"] > 0)
        & (out["win_rate_pct"] >= float(config.economic_min_win_rate_pct))
        & (out["dd_pct"] <= float(config.economic_max_dd_pct))
        & (out["efficiency"] >= float(config.economic_min_efficiency))
    )
    out["event_eligible"] = out["economic_pass"] & out["point_event_count"].ge(config.min_point_events)
    out["reject_reasons"] = out.apply(lambda row: _reject_reasons(row, config), axis=1)
    return out
