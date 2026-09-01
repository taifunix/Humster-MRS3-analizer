"""Typed, closed request/config contract for Performance v2 finalist selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from typing import Literal, Mapping

import duckdb
import numpy as np
import pandas as pd

from .performance_v2_windows import (
    METRICS_VERSION, WindowMetrics, _METRIC_COLUMNS, _cached, _calculate,
    _load_source, _metric_from_row, _persist, get_or_calculate_window_pair,
)
from .audit import write_audit_workbook

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
    "total_trades", "pnl_30d_pct", "profit_factor", "win_rate_pct", "risk_scale", "dd5_proxy", "holding_p95_minutes",
    "holding_median_minutes",
    "ab_pnl_change_30d_pct", "ab_return_b_pct", "first_shift_bp", "scaled_lot_sum", "capital_proxy",
    "capital_efficiency", "total_plateau_point_count",
    "ab_return_a_30d_pct", "ab_return_b_30d_pct", "ab_win_rate_b_pct",
    "ab_trade_rate_a_30d", "ab_trade_rate_b_30d",
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


def _holding_quantiles_minutes(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest
) -> dict[int, tuple[Decimal, Decimal]]:
    rows = connection.execute(
        """with actions as (
                 select a.result_id, a.timestamp_utc, a.action_index, lower(a.action) as kind,
                        a.post_size, lower(a.post_side) as post_side
                   from strategy_actions a
                   join strategy_results r on r.result_id = a.result_id
                   join strategies s on s.strategy_id = r.strategy_id and s.current_result_id = r.result_id
                  where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?
                    and lower(a.action) in ('opened', 'increased', 'decreased', 'closed')
             ), numbered as (
                 select *, sum(case when kind = 'opened' and post_size > 0 and post_side in ('long', 'short') then 1 else 0 end)
                    over (partition by result_id order by timestamp_utc, action_index rows unbounded preceding) as position_number
                   from actions
             ), intervals as (
                 select result_id, position_number,
                        min(timestamp_utc) filter (where kind = 'opened' and post_size > 0 and post_side in ('long', 'short')) as opened_at,
                        min(timestamp_utc) filter (where kind = 'closed' and post_size = 0) as closed_at
                   from numbered
                  group by result_id, position_number
             )
             select result_id,
                    cast(quantile_cont(date_diff('second', opened_at, closed_at) / cast(60 as decimal(20, 6)), .95) as decimal(38, 6)),
                    cast(quantile_cont(date_diff('second', opened_at, closed_at) / cast(60 as decimal(20, 6)), .5) as decimal(38, 6))
               from intervals
              where opened_at is not null and closed_at is not null and closed_at >= opened_at
              group by result_id""",
        [request.symbol, request.side],
    ).fetchall()
    return {
        int(result_id): (Decimal(str(p95)), Decimal(str(median)))
        for result_id, p95, median in rows if p95 is not None and median is not None
    }


def _holding_p95_minutes(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest
) -> dict[int, Decimal]:
    return {result_id: values[0] for result_id, values in _holding_quantiles_minutes(connection, request).items()}


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


def _trade_rate_30d(metrics: WindowMetrics) -> Decimal | None:
    if not metrics.available or metrics.trade_count is None:
        return None
    start, end = metrics.effective_start_utc, metrics.effective_end_utc
    if start is None or end is None:
        return None
    days = Decimal(str((end - start).total_seconds())) / Decimal(86_400)
    return Decimal(metrics.trade_count) * 30 / days if days >= 1 else None


def _ab_metrics(
    connection: duckdb.DuckDBPyConnection,
    result_id: int,
    report_start: datetime,
    report_end: datetime,
    config: SelectionConfig,
) -> dict[str, Decimal | None]:
    split = report_end - timedelta(days=config.ab_final_days)
    if split <= report_start:
        return {key: None for key in ("ab_pnl_change_30d_pct", "ab_return_b_pct", "ab_return_a_30d_pct", "ab_return_b_30d_pct", "ab_win_rate_b_pct", "ab_trade_rate_a_30d", "ab_trade_rate_b_30d")}
    metrics_a, metrics_b = get_or_calculate_window_pair(
        connection, result_id, (report_start, split), (split, report_end)
    )
    return _ab_metrics_from_windows(metrics_a, metrics_b)


def _ab_metrics_from_windows(metrics_a: WindowMetrics, metrics_b: WindowMetrics) -> dict[str, Decimal | None]:
    return_a, return_b = _return_30d(metrics_a), _return_30d(metrics_b)
    return {
        "ab_pnl_change_30d_pct": None if return_a is None or return_b is None or return_a <= 0 else (return_b / return_a - 1) * 100,
        "ab_return_b_pct": metrics_b.return_pct,
        "ab_return_a_30d_pct": return_a, "ab_return_b_30d_pct": return_b,
        "ab_win_rate_b_pct": metrics_b.win_rate_pct, "ab_trade_rate_a_30d": _trade_rate_30d(metrics_a),
        "ab_trade_rate_b_30d": _trade_rate_30d(metrics_b),
    }


def _selection_cached_metrics(connection: duckdb.DuckDBPyConnection, request: SelectionRequest) -> dict[tuple[int, datetime, datetime], WindowMetrics]:
    rows = connection.execute(
        "select " + ", ".join(f"wm.{column}" for column in _METRIC_COLUMNS) +
        " from window_metrics wm"
        " join strategy_results r on r.result_id = wm.result_id"
        " join strategies s on s.strategy_id = r.strategy_id and s.current_result_id = r.result_id"
        " where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ? and wm.metrics_version = ?",
        [request.symbol, request.side, METRICS_VERSION],
    ).fetchall()
    metrics = (_metric_from_row(row) for row in rows)
    return {(metric.result_id, metric.requested_start_utc, metric.requested_end_utc): metric for metric in metrics}


def _cached_selection_metrics(
    connection: duckdb.DuckDBPyConnection,
    result_id: int,
    report_start: datetime,
    report_end: datetime,
    config: SelectionConfig,
    cached_metrics: Mapping[tuple[int, datetime, datetime], WindowMetrics] | None = None,
) -> tuple[WindowMetrics | None, dict[str, Decimal | None]]:
    cached = (
        (lambda start, end: cached_metrics.get((result_id, start, end)))
        if cached_metrics is not None
        else (lambda start, end: _cached(connection, result_id, start, end, METRICS_VERSION))
    )
    full = cached(report_start, report_end)
    split = report_end - timedelta(days=config.ab_final_days)
    if split <= report_start:
        return (full, {key: None for key in ("ab_pnl_change_30d_pct", "ab_return_b_pct", "ab_return_a_30d_pct", "ab_return_b_30d_pct", "ab_win_rate_b_pct", "ab_trade_rate_a_30d", "ab_trade_rate_b_30d")})
    a = cached(report_start, split)
    b = cached(split, report_end)
    if a is None or b is None:
        return (full, {key: None for key in ("ab_pnl_change_30d_pct", "ab_return_b_pct", "ab_return_a_30d_pct", "ab_return_b_30d_pct", "ab_win_rate_b_pct", "ab_trade_rate_a_30d", "ab_trade_rate_b_30d")})
    return (full, _ab_metrics_from_windows(a, b))


def _selection_window_job(database: str, result_id: int, report_start: datetime, report_end: datetime, final_days: int) -> tuple[WindowMetrics, ...]:
    """Compute default selection windows through a read-only worker connection."""
    split = report_end - timedelta(days=final_days)
    windows = ((report_start, report_end), (report_start, split), (split, report_end)) if split > report_start else ((report_start, report_end),)
    with duckdb.connect(database, read_only=True) as connection:
        connection.execute("set threads to 1")
        cached = [_cached(connection, result_id, start, end, METRICS_VERSION) for start, end in windows]
        if all(cached):
            return tuple(cached)
        source = _load_source(connection, result_id)
        return tuple(metric or _calculate(result_id, start, end, METRICS_VERSION, *source) for metric, (start, end) in zip(cached, windows))


def _selection_window_job_from_args(args: tuple[str, int, datetime, datetime, int]) -> tuple[WindowMetrics, ...]:
    return _selection_window_job(*args)


def prepare_selection_window_cache(database: Path, request: SelectionRequest, config: SelectionConfig, workers: int) -> None:
    """Warm default windows in independent readers, then persist them through one writer."""
    with duckdb.connect(str(database), read_only=True) as connection:
        rows = connection.execute(
            """select r.result_id, r.report_start_utc, r.report_end_utc from strategies s
                 join strategy_results r on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
                where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?""",
            [request.symbol, request.side],
        ).fetchall()
    if not rows:
        return
    jobs = [(str(database), int(result_id), report_start, report_end, config.ab_final_days) for result_id, report_start, report_end in rows]
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
        metrics = [metric for result in executor.map(_selection_window_job_from_args, jobs) for metric in result]
    with duckdb.connect(str(database)) as connection:
        for metric in metrics:
            _persist(connection, metric)


def selection_cache_status(connection: duckdb.DuckDBPyConnection, request: SelectionRequest, config: SelectionConfig) -> dict[str, int | bool]:
    rows = connection.execute(
        """select r.result_id, r.report_start_utc, r.report_end_utc from strategies s
             join strategy_results r on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
            where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?""",
        [request.symbol, request.side],
    ).fetchall()
    cached_metrics = _selection_cached_metrics(connection, request)
    missing = 0
    for result_id, start, end in rows:
        split = end - timedelta(days=config.ab_final_days)
        windows = ((start, end), (start, split), (split, end)) if split > start else ((start, end),)
        if any(cached_metrics.get((int(result_id), window_start, window_end)) is None for window_start, window_end in windows):
            missing += 1
    return {"total": len(rows), "missing": missing, "ready": bool(rows) and missing == 0}


def load_selection_candidates(
    connection: duckdb.DuckDBPyConnection,
    request: SelectionRequest,
    config: SelectionConfig = SelectionConfig(),
    *, cache_only: bool = False,
) -> pd.DataFrame:
    """Load all current ACTIVE candidates for one Pair + Side without filtering them."""
    holding_minutes = _holding_quantiles_minutes(connection, request)
    cached_metrics = _selection_cached_metrics(connection, request) if cache_only else None
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
            if cache_only:
                full_metrics, ab_metrics = _cached_selection_metrics(
                    connection, result_id, report_start, report_end, config, cached_metrics
                )
            else:
                full_metrics = get_or_calculate_window_pair(connection, result_id, (report_start, report_end), (report_start, report_end))[0]
                ab_metrics = _ab_metrics(connection, result_id, report_start, report_end, config)
            daily_log = None if full_metrics is None else full_metrics.daily_log_return
            pnl_30d = None if full_metrics is None else _return_30d(full_metrics)
            drawdown = _decimal_or_none(max_drawdown_pct)
            risk_scale = Decimal(5) / drawdown if drawdown is not None and drawdown > 0 else None
            candidate = {
                "strategy_id": int(strategy_id), "strategy_name": str(strategy_name), "symbol": str(symbol),
                "side": str(side), "timeframe": str(timeframe), "close_ma_len": int(close_ma_len),
                "order_count": int(order_count), "result_id": result_id, "total_pnl": _decimal_or_none(total_pnl),
                "total_pnl_pct": _decimal_or_none(total_pnl_pct), "max_drawdown": _decimal_or_none(max_drawdown),
                "max_drawdown_pct": drawdown, "total_fees": _decimal_or_none(total_fees),
                "total_trades": int(total_trades), "pnl_30d_pct": pnl_30d,
                "profit_factor": None if full_metrics is None else full_metrics.profit_factor,
                "win_rate_pct": None if full_metrics is None else full_metrics.win_rate_pct,
                "risk_scale": risk_scale, "dd5_proxy": pnl_30d * risk_scale if pnl_30d is not None and risk_scale is not None else None,
                "holding_p95_minutes": holding_minutes.get(result_id, (None, None))[0],
                "holding_median_minutes": holding_minutes.get(result_id, (None, None))[1],
                **ab_metrics,
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


_PARETO_OBJECTIVES = {
    "pareto_dd5_balanced": (("dd5_proxy", "first_shift_bp"), ("capital_proxy", "holding_p95_minutes", "close_ma_len")),
    "pareto_efficiency_shift": (("capital_efficiency", "first_shift_bp"), ()),
    "pareto_dd5_holding": (("dd5_proxy",), ("holding_p95_minutes",)),
    "pareto_dd5_close_ma": (("dd5_proxy",), ("close_ma_len",)),
    "pareto_dd5_first_shift": (("dd5_proxy", "first_shift_bp"), ()),
    "pareto_conditional_close_ma": (("capital_efficiency",), ("close_ma_len",)),
    "pareto_primary": (("dd5_proxy",), ("capital_proxy",)),
    "pareto_dd5_capital": (("dd5_proxy",), ("capital_proxy",)),
}


def _present(value: object) -> bool:
    return value is not None and not pd.isna(value)


def _dominates(other: pd.Series, candidate: pd.Series, maximize: tuple[str, ...], minimize: tuple[str, ...]) -> bool:
    columns = (*maximize, *minimize)
    if any(column not in other or not _present(other[column]) or not _present(candidate[column]) for column in columns):
        return False
    no_worse = all(other[column] >= candidate[column] for column in maximize) and all(
        other[column] <= candidate[column] for column in minimize
    )
    return no_worse and (any(other[column] > candidate[column] for column in maximize) or any(
        other[column] < candidate[column] for column in minimize
    ))


def _pareto_eliminated(group: pd.DataFrame, maximize: tuple[str, ...], minimize: tuple[str, ...]) -> list[object]:
    """Return dominated indexes without Python row-pair iteration."""
    columns = (*maximize, *minimize)
    values = group.loc[:, columns].to_numpy(dtype=object)
    valid = ~pd.isna(values).any(axis=1)
    comparable = values[valid]
    comparable_indexes = group.index[valid]
    eliminated: list[object] = []
    for candidate_index, candidate in enumerate(comparable):
        no_worse = np.ones(len(comparable), dtype=bool)
        strictly_better = np.zeros(len(comparable), dtype=bool)
        for column_index in range(len(maximize)):
            no_worse &= comparable[:, column_index] >= candidate[column_index]
            strictly_better |= comparable[:, column_index] > candidate[column_index]
        for column_index in range(len(maximize), len(columns)):
            no_worse &= comparable[:, column_index] <= candidate[column_index]
            strictly_better |= comparable[:, column_index] < candidate[column_index]
        no_worse[candidate_index] = False
        if np.any(no_worse & strictly_better):
            eliminated.append(comparable_indexes[candidate_index])
    return eliminated


def _plateau_pareto_eliminated(group: pd.DataFrame, stage_id: str, config: SelectionConfig) -> list[object]:
    eliminated: list[object] = []
    for _, same_order_count in group.groupby("order_count", sort=False):
        points = (
            tuple(f"order_{order}_plateau_point_count" for order in range(1, int(same_order_count["order_count"].iloc[0]) + 1))
            if stage_id.endswith("per_order") else ("total_plateau_point_count",)
        )
        columns = ("dd5_proxy", *points)
        values = same_order_count.loc[:, columns].to_numpy(dtype=object)
        valid = ~pd.isna(values).any(axis=1)
        comparable = values[valid]
        comparable_indexes = same_order_count.index[valid]
        for candidate_index, candidate in enumerate(comparable):
            dominates = comparable[:, 0] >= candidate[0] * config.plateau_points_pareto_pnl_multiplier
            for column_index in range(1, len(columns)):
                dominates &= comparable[:, column_index] >= candidate[column_index]
            dominates[candidate_index] = False
            if np.any(dominates):
                eliminated.append(comparable_indexes[candidate_index])
    return eliminated


def _scope_groups(frame: pd.DataFrame, scope: StageScope):
    return frame.groupby([] if scope == "pair_side" else ["timeframe"], dropna=False, sort=False) if scope != "pair_side" else [(None, frame)]


def _ab_eliminates(row: pd.Series, config: SelectionConfig) -> bool | None:
    fields = ("ab_return_a_30d_pct", "ab_return_b_30d_pct", "ab_win_rate_b_pct", "ab_trade_rate_a_30d", "ab_trade_rate_b_30d")
    if any(field not in row or not _present(row[field]) for field in fields):
        return None
    a, b, win_b, trades_a, trades_b = (row[field] for field in fields)
    return (
        b <= config.ab_return_floor_pct
        or (a > 0 and b <= a / config.ab_return_divisor)
        or win_b < config.ab_win_rate_floor_pct
        or (trades_a > 0 and trades_b <= trades_a / config.ab_trade_rate_divisor)
    )


def run_selection(
    candidates: pd.DataFrame, request: SelectionRequest, config: SelectionConfig = SelectionConfig()
) -> pd.DataFrame:
    """Apply the submitted stages in order; input candidates remain fully represented."""
    result = candidates.copy().sort_values(["strategy_name", "strategy_id"], kind="stable").reset_index(drop=True)
    result["finalist"] = True
    result["elimination_reason"] = None
    stage_counts: dict[str, dict[str, int | bool]] = {}
    for stage in request.stages:
        column = f"eliminated_by_{stage.id}"
        result[column] = False
        if not stage.enabled:
            stage_counts[stage.id] = {"enabled": False, "eliminated": 0, "remaining": int(result["finalist"].sum())}
            continue
        survivors = result.loc[result["finalist"]]
        for _, group in _scope_groups(survivors, stage.scope):
            if stage.id in {"filter_holding_outlier", "filter_low_trades"}:
                metric = "holding_p95_minutes" if stage.id == "filter_holding_outlier" else "total_trades"
                values = pd.to_numeric(group[metric], errors="coerce").dropna()
                if values.empty:
                    continue
                q1, q3 = values.quantile(.25), values.quantile(.75)
                threshold = q3 + 1.5 * (q3 - q1) if stage.id == "filter_holding_outlier" else q1 - 1.5 * (q3 - q1)
                failed = group[metric] > threshold if stage.id == "filter_holding_outlier" else group[metric] < threshold
                eliminated = group.index[failed.fillna(False)]
            elif stage.id == "ab_deterioration":
                eliminated = []
                for index, row in group.iterrows():
                    decision = _ab_eliminates(row, config)
                    if decision is None:
                        result.at[index, "elimination_reason"] = "AB_NOT_EVALUATED_INSUFFICIENT_DATA"
                    elif decision:
                        eliminated.append(index)
            else:
                if stage.id == "pareto_conditional_close_ma" and len(group) <= 3:
                    stage_counts[stage.id] = {"enabled": True, "eliminated": 0, "remaining": int(result["finalist"].sum())}
                    continue
                eliminated = (
                    _plateau_pareto_eliminated(group, stage.id, config)
                    if stage.id.startswith("pareto_plateau_points")
                    else _pareto_eliminated(group, *_PARETO_OBJECTIVES[stage.id])
                )
            if len(eliminated):
                result.loc[eliminated, column] = True
                result.loc[eliminated, "finalist"] = False
                result.loc[eliminated, "elimination_reason"] = stage.id.upper()
        stage_counts[stage.id] = {"enabled": True, "eliminated": int(result[column].sum()), "remaining": int(result["finalist"].sum())}
    result.attrs["stage_counts"] = stage_counts
    return result


def write_selection_workbook(result: pd.DataFrame, path: Path, request: SelectionRequest) -> Path:
    """Write the one disposable selection workbook; internal A/B facts stay internal."""
    display = result.drop(columns=[
        column for column in result.columns
        if column.startswith("ab_") and column not in {"ab_pnl_change_30d_pct", "ab_return_b_30d_pct"}
    ] + ["result_id", "total_pnl", "max_drawdown", "total_fees", "risk_scale", "scaled_lot_sum", "daily_log_return"], errors="ignore").copy()
    if "ab_pnl_change_30d_pct" not in display:
        display["ab_pnl_change_30d_pct"] = None
    for column in ("pnl_30d_pct", "profit_factor", "win_rate_pct"):
        if column not in display:
            display[column] = None
    for column in display.columns:
        if column.endswith("_id") or "count" in column or column.endswith("_bp"):
            continue
        display[column] = display[column].map(
            lambda value: value.quantize(Decimal(".01")) if isinstance(value, Decimal) else value
        )
    for column in ("first_shift_bp", *(f"order_{order}_shift_bp" for order in range(1, 5))):
        if column in display:
            display[column] = display[column].map(
                lambda value: (Decimal(str(value)) / Decimal("100")).quantize(
                    Decimal(".1"), rounding=ROUND_HALF_UP
                ) if value is not None and not pd.isna(value) else value
            )
    for column in (
        "ab_pnl_change_30d_pct", "capital_efficiency", "win_rate_pct", "holding_p95_minutes",
        "holding_median_minutes", "total_plateau_point_count",
        *(f"order_{order}_plateau_point_count" for order in range(1, 5)),
        *(f"order_{order}_open_ma_len" for order in range(1, 5)),
    ):
        if column in display:
            display[column] = display[column].map(
                lambda value: int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                if value is not None and not pd.isna(value) else value
            )
    enabled_filter_columns = [f"eliminated_by_{stage.id}" for stage in request.stages if stage.enabled]
    display = display.drop(columns=[
        column for column in display if column.startswith("eliminated_by_") and column not in enabled_filter_columns
    ], errors="ignore")
    column_order = [
        "strategy_id", "strategy_name", "symbol", "side", "timeframe", "close_ma_len", "order_count",
        "total_pnl_pct", "pnl_30d_pct", "dd5_proxy", "profit_factor", "ab_pnl_change_30d_pct", "ab_return_b_30d_pct",
        "capital_efficiency", "max_drawdown_pct", "win_rate_pct", "total_trades", "capital_proxy",
        "holding_p95_minutes", "holding_median_minutes", "first_shift_bp", "total_plateau_point_count",
        *(f"order_{order}_shift_bp" for order in range(1, 5)),
        *(f"order_{order}_lot_x" for order in range(1, 5)),
        *(f"order_{order}_plateau_point_count" for order in range(1, 5)),
        *(f"order_{order}_open_ma_len" for order in range(1, 5)),
        "finalist", "elimination_reason", *enabled_filter_columns,
    ]
    display = display.reindex(columns=column_order)
    display = display.rename(columns={
        "strategy_id": "ID", "strategy_name": "Стратегия", "symbol": "Пара", "side": "Side", "timeframe": "ТФ",
        "close_ma_len": "Close", "order_count": "ORD",
        "total_pnl_pct": "PnL", "pnl_30d_pct": "PnL/30", "dd5_proxy": "PnL DD5/30", "profit_factor": "PF",
        "ab_pnl_change_30d_pct": "∆ PnL A/B", "ab_return_b_30d_pct": "PnL B", "capital_efficiency": "CE",
        "max_drawdown_pct": "DD", "win_rate_pct": "W/R", "total_trades": "Trades", "capital_proxy": "Lot DD5",
        "holding_p95_minutes": "Hold p95", "holding_median_minutes": "Hold M", "first_shift_bp": "Shift 1",
        "total_plateau_point_count": "PointsALL", "finalist": "Final", "elimination_reason": "Причина",
        **{f"order_{order}_open_ma_len": f"{order} MA" for order in range(1, 5)},
        **{f"order_{order}_shift_bp": f"{order} Shift" for order in range(1, 5)},
        **{f"order_{order}_lot_x": f"{order} lot" for order in range(1, 5)},
        **{f"order_{order}_plateau_point_count": f"{order} Points" for order in range(1, 5)},
    })
    finalists = display.loc[display["Final"]].copy()
    return write_audit_workbook(
        {"All candidates": display, "Finalists": finalists}, Path(path), data_widths_only=True,
        minimum_width=3, hidden_columns=frozenset({"Стратегия"}), numeric_decimals=True,
        number_formats={
            "Shift 1": "0.0", **{f"{order} Shift": "0.0" for order in range(1, 5)},
        },
    )
