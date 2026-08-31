"""Typed, closed request/config contract for Performance v2 finalist selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
from typing import Literal, Mapping

import duckdb
import pandas as pd

from .performance_v2_windows import WindowMetrics, get_or_calculate_window_pair

StageScope = Literal["pair_side", "pair_side_timeframe"]

_STAGE_IDS = frozenset((
    "filter_holding_outlier",
    "filter_low_trades",
    "ab_deterioration",
    "pareto_dd5_balanced",
    "pareto_plateau_points_per_order",
    "pareto_plateau_points_total",
    "pareto_efficiency_shift",
    "pareto_dd5_holding",
    "pareto_dd5_close_ma",
    "pareto_dd5_first_shift",
    "pareto_conditional_close_ma",
    "pareto_primary",
    "pareto_dd5_capital",
))
_SCOPES = frozenset(("pair_side", "pair_side_timeframe"))
_CANDIDATE_COLUMNS = (
    "strategy_id", "strategy_name", "symbol", "side", "timeframe", "close_ma_len", "order_count",
    "result_id", "total_pnl", "total_pnl_pct", "max_drawdown", "max_drawdown_pct", "total_fees",
    "total_trades", "daily_log_return", "risk_scale", "dd5_proxy", "holding_p95_minutes",
    "ab_pnl_change_30d_pct", "first_shift_bp", "scaled_lot_sum", "capital_proxy",
    "capital_efficiency", "total_plateau_point_count",
    *(f"order_{order}_{field}" for order in range(1, 5) for field in (
        "open_ma_len", "open_multiplier", "shift_bp", "lot_x", "plateau_point_count",
    )),
)


class PerformanceV2SelectionError(ValueError):
    """Stable error code for an invalid finalist-selection request/config."""


@dataclass(frozen=True, slots=True)
class SelectionStage:
    id: str
    enabled: bool
    scope: StageScope


@dataclass(frozen=True, slots=True)
class SelectionRequest:
    symbol: str
    side: Literal["LONG", "SHORT"]
    stages: tuple[SelectionStage, ...]


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    ab_final_days: int = 14
    ab_return_floor_pct: Decimal = Decimal("5")
    ab_return_divisor: Decimal = Decimal("10")
    ab_win_rate_floor_pct: Decimal = Decimal("58")
    ab_trade_rate_divisor: Decimal = Decimal("7")
    plateau_points_pareto_pnl_multiplier: Decimal = Decimal("2")


def _error(code: str) -> PerformanceV2SelectionError:
    return PerformanceV2SelectionError(code)


def parse_selection_request(payload: Mapping[str, object]) -> SelectionRequest:
    if set(payload) != {"symbol", "side", "stages"}:
        raise _error("INVALID_REQUEST")
    symbol = payload["symbol"]
    side = payload["side"]
    stages = payload["stages"]
    if not isinstance(symbol, str) or not symbol.strip():
        raise _error("INVALID_SYMBOL")
    if side not in {"LONG", "SHORT"}:
        raise _error("INVALID_SIDE")
    if not isinstance(stages, list):
        raise _error("INVALID_STAGES")

    parsed: list[SelectionStage] = []
    seen: set[str] = set()
    for raw in stages:
        if not isinstance(raw, Mapping) or set(raw) != {"id", "enabled", "scope"}:
            raise _error("INVALID_STAGE")
        stage_id, enabled, scope = raw["id"], raw["enabled"], raw["scope"]
        if not isinstance(stage_id, str) or stage_id not in _STAGE_IDS:
            raise _error("UNKNOWN_STAGE")
        if stage_id in seen:
            raise _error("DUPLICATE_STAGE")
        if not isinstance(enabled, bool):
            raise _error("INVALID_STAGE")
        if scope not in _SCOPES:
            raise _error("INVALID_SCOPE")
        seen.add(stage_id)
        parsed.append(SelectionStage(stage_id, enabled, scope))
    return SelectionRequest(symbol.strip(), side, tuple(parsed))


def _positive_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise _error(f"INVALID_CONFIG_{name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _error(f"INVALID_CONFIG_{name}") from None
    if not parsed.is_finite() or parsed <= 0:
        raise _error(f"INVALID_CONFIG_{name}")
    return parsed


def load_selection_config(path: Path) -> SelectionConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        section = raw["unified_performance_v2"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise _error("INVALID_CONFIG") from None
    if not isinstance(section, Mapping):
        raise _error("INVALID_CONFIG")
    selected = section.get("finalist_selection", {})
    if not isinstance(selected, Mapping):
        raise _error("INVALID_CONFIG")
    final_days = selected.get("ab_final_days", 14)
    if isinstance(final_days, bool) or not isinstance(final_days, int) or final_days < 1:
        raise _error("INVALID_CONFIG_ab_final_days")
    return SelectionConfig(
        ab_final_days=final_days,
        ab_return_floor_pct=_positive_decimal(selected.get("ab_return_floor_pct", 5), "ab_return_floor_pct"),
        ab_return_divisor=_positive_decimal(selected.get("ab_return_divisor", 10), "ab_return_divisor"),
        ab_win_rate_floor_pct=_positive_decimal(selected.get("ab_win_rate_floor_pct", 58), "ab_win_rate_floor_pct"),
        ab_trade_rate_divisor=_positive_decimal(selected.get("ab_trade_rate_divisor", 7), "ab_trade_rate_divisor"),
        plateau_points_pareto_pnl_multiplier=_positive_decimal(
            selected.get("plateau_points_pareto_pnl_multiplier", 2),
            "plateau_points_pareto_pnl_multiplier",
        ),
    )


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _holding_p95_minutes(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest
) -> dict[int, Decimal]:
    active: dict[tuple[int, str, str], datetime] = {}
    durations: dict[int, list[Decimal]] = {}
    rows = connection.execute(
        """select a.result_id, a.timestamp_utc, a.symbol, s.side, a.action, a.post_size, a.post_side
             from strategy_actions a
             join strategy_results r on r.result_id = a.result_id
             join strategies s on s.strategy_id = r.strategy_id and s.current_result_id = r.result_id
            where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?
            order by a.result_id, a.timestamp_utc, a.action_index""",
        [request.symbol, request.side],
    ).fetchall()
    for result_id, timestamp, symbol, side, kind, post_size, post_side in rows:
        kind = str(kind).casefold()
        if kind not in {"opened", "increased", "decreased", "closed"}:
            continue
        position_side = (
            str(side).casefold()
            if kind == "closed"
            else str(post_side).casefold()
        )
        size = _decimal_or_none(post_size)
        if position_side not in {"long", "short"} or size is None:
            continue
        key = (int(result_id), str(symbol), position_side)
        if kind == "opened" and size > 0 and key not in active:
            active[key] = timestamp
        elif kind == "closed" and size == 0:
            opened_at = active.pop(key, None)
            if opened_at is not None and timestamp >= opened_at:
                durations.setdefault(int(result_id), []).append(
                    Decimal(str((timestamp - opened_at).total_seconds())) / Decimal(60)
                )
    result: dict[int, Decimal] = {}
    for result_id, values in durations.items():
        ordered = sorted(values)
        rank = Decimal(len(ordered) - 1) * Decimal("0.95")
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        result[result_id] = ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)
    return result


def _return_30d(metrics: WindowMetrics) -> Decimal | None:
    if not metrics.available or metrics.growth_factor is None:
        return None
    start, end = metrics.effective_start_utc, metrics.effective_end_utc
    if start is None or end is None:
        return None
    elapsed = Decimal(str((end - start).total_seconds())) / Decimal(86_400)
    if elapsed < 1 or metrics.growth_factor < 0:
        return None
    if metrics.growth_factor == 0:
        return Decimal(-100)
    try:
        with localcontext() as context:
            context.prec = 34
            return ((Decimal(30) * metrics.growth_factor.ln() / elapsed).exp() - 1) * 100
    except (ArithmeticError, ValueError):
        return None


def _ab_pnl_change_30d(
    connection: duckdb.DuckDBPyConnection,
    result_id: int,
    report_start: datetime,
    report_end: datetime,
    config: SelectionConfig,
) -> Decimal | None:
    split = report_end - timedelta(days=config.ab_final_days)
    if split <= report_start:
        return None
    metrics_a, metrics_b = get_or_calculate_window_pair(
        connection, result_id, (report_start, split), (split, report_end)
    )
    return_a, return_b = _return_30d(metrics_a), _return_30d(metrics_b)
    if return_a is None or return_b is None or return_a <= 0:
        return None
    return (return_b / return_a - 1) * 100


def load_selection_candidates(
    connection: duckdb.DuckDBPyConnection,
    request: SelectionRequest,
    config: SelectionConfig = SelectionConfig(),
) -> pd.DataFrame:
    """Load all current ACTIVE candidates for one Pair + Side without filtering them."""
    holding_p95 = _holding_p95_minutes(connection, request)
    rows = connection.execute(
        """select s.strategy_id, s.strategy_name, s.symbol, s.side, s.timeframe, s.close_ma_len,
                  s.order_count, r.result_id, r.report_start_utc, r.report_end_utc,
                  r.total_pnl, r.total_pnl_pct, r.max_drawdown, r.max_drawdown_pct,
                  r.total_fees, r.total_trades, o.order_id, o.open_ma_len, o.open_multiplier,
                  o.shift_bp, o.lot_x, p.plateau_point_count
             from strategies s
             join strategy_results r on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
             left join strategy_orders o on o.strategy_id = s.strategy_id
             left join analysis_plateaus p on p.analysis_run_id = o.analysis_run_id and p.plateau_id = o.plateau_id
            where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?
            order by s.strategy_name, s.strategy_id, o.order_id""",
        [request.symbol, request.side],
    ).fetchall()
    candidates: dict[int, dict[str, object]] = {}
    for row in rows:
        (
            strategy_id, strategy_name, symbol, side, timeframe, close_ma_len, order_count,
            result_id, report_start, report_end, total_pnl, total_pnl_pct, max_drawdown,
            max_drawdown_pct, total_fees, total_trades, order_id, open_ma_len,
            open_multiplier, shift_bp, lot_x, plateau_count,
        ) = row
        candidate = candidates.get(int(strategy_id))
        if candidate is None:
            result_id = int(result_id)
            daily_log = get_or_calculate_window_pair(
                connection, result_id, (report_start, report_end), (report_start, report_end)
            )[0].daily_log_return
            drawdown = _decimal_or_none(max_drawdown_pct)
            risk_scale = Decimal(5) / drawdown if drawdown is not None and drawdown > 0 else None
            candidate = {
                "strategy_id": int(strategy_id), "strategy_name": str(strategy_name), "symbol": str(symbol),
                "side": str(side), "timeframe": str(timeframe), "close_ma_len": int(close_ma_len),
                "order_count": int(order_count), "result_id": result_id, "total_pnl": _decimal_or_none(total_pnl),
                "total_pnl_pct": _decimal_or_none(total_pnl_pct), "max_drawdown": _decimal_or_none(max_drawdown),
                "max_drawdown_pct": drawdown, "total_fees": _decimal_or_none(total_fees),
                "total_trades": int(total_trades), "daily_log_return": daily_log,
                "risk_scale": risk_scale, "dd5_proxy": daily_log * risk_scale if daily_log is not None and risk_scale is not None else None,
                "holding_p95_minutes": holding_p95.get(result_id),
                "ab_pnl_change_30d_pct": _ab_pnl_change_30d(connection, result_id, report_start, report_end, config),
                "first_shift_bp": None, "scaled_lot_sum": None, "capital_proxy": None,
                "capital_efficiency": None, "total_plateau_point_count": None,
            }
            candidates[int(strategy_id)] = candidate
        if order_id is not None:
            number = int(order_id)
            lot = _decimal_or_none(lot_x)
            points = None if plateau_count is None else int(plateau_count)
            candidate[f"order_{number}_open_ma_len"] = int(open_ma_len)
            candidate[f"order_{number}_open_multiplier"] = _decimal_or_none(open_multiplier)
            candidate[f"order_{number}_shift_bp"] = int(shift_bp)
            candidate[f"order_{number}_lot_x"] = lot
            candidate[f"order_{number}_plateau_point_count"] = points
            if number == 1:
                candidate["first_shift_bp"] = int(shift_bp)
            lots = candidate.setdefault("_lots", [])
            points_list = candidate.setdefault("_points", [])
            if lot is not None:
                lots.append(lot)
            if points is not None:
                points_list.append(points)
    for candidate in candidates.values():
        lots = candidate.pop("_lots", [])
        points = candidate.pop("_points", [])
        risk_scale = candidate["risk_scale"]
        if risk_scale is not None and len(lots) == candidate["order_count"]:
            scaled_lot_sum = sum(lots, Decimal(0)) * risk_scale
            candidate["scaled_lot_sum"] = scaled_lot_sum
            candidate["capital_proxy"] = scaled_lot_sum + Decimal("0.05")
            if candidate["dd5_proxy"] is not None:
                candidate["capital_efficiency"] = candidate["dd5_proxy"] / candidate["capital_proxy"]
        if len(points) == candidate["order_count"]:
            candidate["total_plateau_point_count"] = sum(points)
    return pd.DataFrame.from_records(list(candidates.values())).reindex(columns=_CANDIDATE_COLUMNS)
