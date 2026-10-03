"""Typed, closed request/config contract for Performance v2 finalist selection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from colorsys import hls_to_rgb
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.worksheet.datavalidation import DataValidation

from .performance_v2_windows import (
    METRICS_VERSION, WindowMetrics, _METRIC_COLUMNS, _cached, _calculate,
    _load_equity_samples_for_quality, _load_initial_balance, _load_source, _metric_from_row, _persist_many,
    calendar_window_days, get_or_calculate_window, get_or_calculate_window_pair,
)
from .performance_v2_equity_cache import (
    ALGORITHM_VERSION, EquityQualityCacheError, EquitySourceChangedError,
    current_equity_source_metadata, decode_equity_facts, equity_source_revision,
    upsert_equity_quality_facts_checked,
)
from .performance_v2_equity_quality import EquityQualityFacts, EquitySample, calculate_equity_quality_facts
from .performance_v2_store import PerformanceV2StoreError, require_performance_v2_readable
from .audit import write_audit_workbook

StageScope = Literal["pair_side", "pair_side_timeframe"]

_STAGE_IDS = frozenset((
    "pair_side_pnl_upper_half",
    "structural_stage_1",
    "structural_stage_2",
    "pair_side_stage_3",
    "filter_lot_variant_redundancy",
    "filter_holding_outlier",
    "filter_low_trades",
    "filter_min_shift",
    "filter_equity_regime",
    "filter_hard_cutoffs",
    "ab_deterioration",
    "pareto_window_b",
    "pareto_window_b_dd_shift",
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
    "filter_best_trade_dependency",
    "filter_time_consistency",
    "pareto_robust",
    "pareto_shift_near_tie",
    "pareto_close_ma_near_tie",
    "rank_robust_top_n",
))
_SCOPES = frozenset(("pair_side", "pair_side_timeframe"))
SELECTION_REASON_ALIASES = {"PARETO_PLATEAU_POINTS_PER_ORDER": "PARETO_PL_PTS_PER_ORDER"}
_CANDIDATE_COLUMNS = (
    "strategy_id", "strategy_name", "symbol", "side", "timeframe", "close_ma_len", "order_count",
    "result_id", "total_pnl", "total_pnl_pct", "max_drawdown", "max_drawdown_pct", "total_fees",
    "total_trades", "trades_30d", "pnl_30d_pct", "profit_factor", "win_rate_pct", "risk_scale", "dd5_proxy", "holding_p95_minutes",
    "holding_median_minutes",
    "report_start_utc", "report_end_utc", "reported_start_utc", "reported_end_utc",
    "effective_start_utc", "effective_end_utc",
    "ab_pnl_change_30d_pct", "ab_return_b_pct", "first_shift_bp", "scaled_lot_sum", "capital_proxy",
    "capital_efficiency", "total_plateau_point_count",
    "ab_return_a_30d_pct", "ab_return_b_30d_pct", "ab_calendar_days_a", "ab_calendar_days_b", "ab_win_rate_b_pct",
    "ab_trade_rate_a_30d", "ab_trade_rate_b_30d", "ab_drawdown_b_pct", "ab_holding_p95_minutes",
    "initial_balance", "history_days", "completed_cycle_count", "reliable_completed_cycle_count", "completed_profitable_cycle_count", "completed_cycle_net_pnl", "top5_pnl", "top5_share_pct", "pnl_after_top5", "completed_cycles_reliable",
    "best_trade_profit_share_pct", "pnl_without_best_trade", "pnl_without_best_trade_pct", "completed_profitable_trade_count", "best_trade_reliable",
    "ab_completed_cycle_count",
    "positive_quarter_count", "positive_quarter_available_count", "positive_quarter_status", "robust_pnl_30d_pct", "worst_drawdown_pct", "worst_holding_p95_minutes",
    "ab_stability_ratio", "minimum_plateau_point_count",
    "lot_variant_group_key", "lot_variant_representative_strategy_id",
    "rank_quality_robust_pnl", "rank_quality_worst_drawdown", "rank_quality_ab_stability", "rank_quality_worst_holding",
    "rank_quality_first_shift", "rank_quality_minimum_plateau_points", "rank_quality_close_ma", "rank_weight_coverage_pct",
    "rank_weight_robust_pnl", "rank_weight_worst_drawdown", "rank_weight_ab_stability", "rank_weight_worst_holding",
    "rank_weight_first_shift", "rank_weight_minimum_plateau_points", "rank_weight_close_ma", "final_score", "final_rank",
    *(f"order_{order}_{field}" for order in range(1, 5) for field in (
        "open_ma_len", "open_multiplier", "shift_bp", "lot_x", "plateau_point_count", "plateau_key",
    )),
)


class PerformanceV2SelectionError(ValueError):
    """Stable error code for an invalid finalist-selection request/config."""


class EquitySchemaUpgradeRequiredError(EquityQualityCacheError):
    code = "EQUITY_SCHEMA_UPGRADE_REQUIRED"


class EquityCacheSchemaInvalidError(EquityQualityCacheError):
    code = "PERFORMANCE_V2_SCHEMA_INVALID"


@dataclass(frozen=True, slots=True)
class SelectionStage:
    id: str
    enabled: bool
    scope: StageScope
    min_shift_pct: Decimal | None = None
    pnl_tolerance_pct: Decimal | None = None
    top_n: int | None = None
    method: Literal["robust_v1", "equity_quality_v1"] | None = None


@dataclass(frozen=True, slots=True)
class SelectionRequest:
    symbol: str
    side: Literal["LONG", "SHORT"]
    stages: tuple[SelectionStage, ...]
    ranking_scope: Literal["ORDINARY", "RETEST_COHORT"] = "ORDINARY"
    bulk_retest_job_id: str | None = None
    cohort_members: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    ab_final_days: int = 14
    ab_return_floor_pct: Decimal = Decimal("4")
    ab_return_divisor: Decimal = Decimal("10")
    ab_win_rate_floor_pct: Decimal = Decimal("55")
    ab_decline_cap_pct: Decimal = Decimal("15")
    ab_completed_cycles: int = 25
    lot_full_dd5_multiplier: Decimal = Decimal("1.10")
    lot_full_dd_multiplier: Decimal = Decimal("1.10")
    lot_tolerance: Decimal = Decimal("1e-9")
    hard_dd_pct: Decimal = Decimal("23")
    hard_dd_profit_multiplier: Decimal = Decimal("3")
    hard_pnl30_floor_pct: Decimal = Decimal("4")
    hard_ratio: Decimal = Decimal("0.75")
    hard_min_history_days: int = 45
    hard_min_cycles: int = 25
    top5_share_pct: Decimal = Decimal("80")
    top5_min_history_days: int = 45
    top5_min_profitable_cycles: int = 25
    # Kept for decoding older local configs; the old predicates are retired.
    ab_trade_rate_divisor: Decimal = Decimal("7")
    plateau_points_pareto_pnl_multiplier: Decimal = Decimal("2")
    best_trade_max_profit_share_pct: Decimal = Decimal("35")
    best_trade_min_profitable_trades: int = 4
    shift_near_tie_min_advantage_bp: int = 10
    lot_variant_redundancy_enabled: bool = True
    researched_pnl_dd5_ratio: Decimal = Decimal("0.50")
    researched_pnl_b_ratio: Decimal = Decimal("0.60")
    researched_b_abs: Decimal = Decimal("2")
    researched_b_rel: Decimal = Decimal("0.25")
    researched_dd5_abs: Decimal = Decimal("3")
    researched_dd5_rel: Decimal = Decimal("0.25")
    researched_dd_abs: Decimal = Decimal("1")
    researched_dd_rel: Decimal = Decimal("0.25")
    researched_points_mean_ratio: Decimal = Decimal("0.40")
    researched_points_same_floor_ratio: Decimal = Decimal("0.40")
    researched_points_cross_floor_ratio: Decimal = Decimal("0.30")
    researched_points_single_best_ratio: Decimal = Decimal("0.10")
    researched_open_ma_delta: Decimal = Decimal("2.6")
    researched_close_ma_delta: Decimal = Decimal("3")
    researched_hold_p95_ratio: Decimal = Decimal("0.20")
    researched_hold_median_ratio: Decimal = Decimal("0.30")
    researched_hold_p95_veto_ratio: Decimal = Decimal("0.15")

    @property
    def ab_min_completed_cycles(self) -> int:
        return self.ab_completed_cycles

    @property
    def top5_max_profit_share_pct(self) -> Decimal:
        return self.top5_share_pct

    @property
    def top5_min_profitable_trades(self) -> int:
        return self.top5_min_profitable_cycles

    @property
    def hard_cutoff_dd_pct(self) -> Decimal:
        return self.hard_dd_pct

    @property
    def hard_cutoff_ratio(self) -> Decimal:
        return self.hard_ratio


def _error(code: str) -> PerformanceV2SelectionError:
    return PerformanceV2SelectionError(code)


def parse_selection_request(
    payload: Mapping[str, object], *, allow_retired_enabled: bool = False,
) -> SelectionRequest:
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
        if not isinstance(raw, Mapping):
            raise _error("INVALID_STAGE")
        stage_id = raw.get("id")
        expected = {"id", "enabled", "scope"}
        if stage_id == "filter_min_shift":
            expected.add("min_shift_pct")
        if stage_id in {"pareto_shift_near_tie", "pareto_close_ma_near_tie"}:
            expected.add("pnl_tolerance_pct")
        if stage_id == "rank_robust_top_n":
            expected.add("top_n")
            if "method" in raw:
                expected.add("method")
        if set(raw) != expected:
            raise _error("INVALID_STAGE")
        enabled, scope = raw["enabled"], raw["scope"]
        if not isinstance(stage_id, str) or stage_id not in _STAGE_IDS:
            raise _error("UNKNOWN_STAGE")
        if stage_id in seen:
            raise _error("DUPLICATE_STAGE")
        if not isinstance(enabled, bool):
            raise _error("INVALID_STAGE")
        if scope not in _SCOPES:
            raise _error("INVALID_SCOPE")
        if stage_id == "filter_time_consistency" and enabled and not allow_retired_enabled:
            raise _error("RETIRED_STAGE")
        min_shift_pct = _positive_decimal(raw["min_shift_pct"], "min_shift_pct") if stage_id == "filter_min_shift" and enabled else None
        pnl_tolerance_pct = _bounded_decimal(raw["pnl_tolerance_pct"], "pnl_tolerance_pct", Decimal(0), Decimal(100)) if stage_id in {"pareto_shift_near_tie", "pareto_close_ma_near_tie"} and enabled else None
        top_n = _positive_int(raw["top_n"], "top_n") if stage_id == "rank_robust_top_n" else None
        method = raw.get("method") if stage_id == "rank_robust_top_n" else None
        if "method" in raw and method not in ("robust_v1", "equity_quality_v1"):
            raise _error("INVALID_RANK_METHOD")
        if stage_id == "rank_robust_top_n" and scope != "pair_side":
            raise _error("RANK_STAGE_SCOPE")
        if stage_id == "filter_lot_variant_redundancy" and scope != "pair_side_timeframe":
            raise _error("LOT_VARIANT_STAGE_SCOPE")
        if stage_id == "filter_equity_regime" and scope != "pair_side":
            raise _error("EQUITY_REGIME_STAGE_SCOPE")
        if stage_id in {"pair_side_pnl_upper_half", "pair_side_stage_3"} and scope != "pair_side":
            raise _error("STAGE_SCOPE")
        if stage_id in {"structural_stage_1", "structural_stage_2"} and scope != "pair_side_timeframe":
            raise _error("STAGE_SCOPE")
        seen.add(stage_id)
        parsed.append(SelectionStage(stage_id, enabled, scope, min_shift_pct, pnl_tolerance_pct, top_n, method))
    if any(stage.id == "rank_robust_top_n" for stage in parsed) and parsed[-1].id != "rank_robust_top_n":
        raise _error("RANK_STAGE_MUST_BE_LAST")
    return SelectionRequest(symbol.strip(), side, tuple(parsed))


def retest_cohort_request(
    request: SelectionRequest,
    job_id: str,
    members: Mapping[int, int] | Sequence[tuple[int, int]],
) -> SelectionRequest:
    """Create a server-owned cohort request; browsers never supply the IDs."""
    if not isinstance(request, SelectionRequest) or not isinstance(job_id, str) or not job_id.strip():
        raise PerformanceV2SelectionError("RETEST_COHORT_INVALID")
    pairs = tuple(sorted((int(strategy_id), int(result_id)) for strategy_id, result_id in (
        members.items() if isinstance(members, Mapping) else members
    )))
    if not pairs or any(strategy_id <= 0 or result_id <= 0 for strategy_id, result_id in pairs):
        raise PerformanceV2SelectionError("RETEST_COHORT_NO_SUCCESSFUL_MEMBERS")
    if len({strategy_id for strategy_id, _ in pairs}) != len(pairs):
        raise PerformanceV2SelectionError("RETEST_COHORT_INVALID")
    return replace(request, ranking_scope="RETEST_COHORT", bulk_retest_job_id=job_id.strip(), cohort_members=pairs)


def _cohort_clause(request: SelectionRequest, alias: str = "s") -> tuple[str, list[object]]:
    if request.ranking_scope == "ORDINARY":
        return "", []
    if request.ranking_scope != "RETEST_COHORT" or not request.bulk_retest_job_id or not request.cohort_members:
        raise PerformanceV2SelectionError("RETEST_COHORT_NO_SUCCESSFUL_MEMBERS")
    ids = tuple(strategy_id for strategy_id, _ in request.cohort_members)
    return f" and {alias}.strategy_id in ({','.join('?' for _ in ids)})", list(ids)


def _verify_retest_cohort(connection: duckdb.DuckDBPyConnection, request: SelectionRequest) -> None:
    if request.ranking_scope != "RETEST_COHORT":
        return
    clause, parameters = _cohort_clause(request)
    rows = connection.execute(
        "select s.strategy_id, s.current_result_id from strategies s where s.lifecycle_status = 'ACTIVE'" + clause,
        parameters,
    ).fetchall()
    current = {int(strategy_id): None if result_id is None else int(result_id) for strategy_id, result_id in rows}
    expected = dict(request.cohort_members)
    stale = sorted(strategy_id for strategy_id, result_id in expected.items() if current.get(strategy_id) != result_id)
    if len(current) != len(expected) or stale:
        raise PerformanceV2SelectionError("RETEST_COHORT_STALE_RESULTS")


def _verify_retest_cohort_for_database(database: Path, request: SelectionRequest) -> None:
    with duckdb.connect(str(database), read_only=True) as connection:
        _verify_retest_cohort(connection, request)


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


def _bounded_decimal(value: object, name: str, lower: Decimal, upper: Decimal) -> Decimal:
    if isinstance(value, bool):
        raise _error(f"INVALID_CONFIG_{name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _error(f"INVALID_CONFIG_{name}") from None
    if not parsed.is_finite() or parsed < lower or parsed >= upper:
        raise _error(f"INVALID_CONFIG_{name}")
    return parsed


def _bounded_decimal_inclusive(value: object, name: str, lower: Decimal, upper: Decimal) -> Decimal:
    if isinstance(value, bool):
        raise _error(f"INVALID_CONFIG_{name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _error(f"INVALID_CONFIG_{name}") from None
    if not parsed.is_finite() or parsed < lower or parsed > upper:
        raise _error(f"INVALID_CONFIG_{name}")
    return parsed


def _at_least_decimal(value: object, name: str, lower: Decimal) -> Decimal:
    parsed = _positive_decimal(value, name)
    if parsed < lower:
        raise _error(f"INVALID_CONFIG_{name}")
    return parsed


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    return _bounded_decimal_inclusive(value, name, Decimal(0), Decimal("1e999"))


def _positive_bounded_decimal(value: object, name: str, upper: Decimal) -> Decimal:
    parsed = _positive_decimal(value, name)
    if parsed > upper:
        raise _error(f"INVALID_CONFIG_{name}")
    return parsed


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _error(f"INVALID_CONFIG_{name}")
    return value


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
    def setting(name: str, default: object, *aliases: str) -> object:
        for key in (name, *aliases):
            if key in selected:
                return selected[key]
        return default
    final_days = selected.get("ab_final_days", 14)
    if isinstance(final_days, bool) or not isinstance(final_days, int) or final_days < 1:
        raise _error("INVALID_CONFIG_ab_final_days")
    best_trade_min = selected.get("best_trade_min_profitable_trades", 4)
    shift_advantage = selected.get("shift_near_tie_min_advantage_bp", 10)
    lot_variant_enabled = selected.get("lot_variant_redundancy_enabled", True)
    if not isinstance(lot_variant_enabled, bool):
        raise _error("INVALID_CONFIG_lot_variant_redundancy_enabled")
    ratio = lambda name, default: _bounded_decimal_inclusive(setting(name, default), name, Decimal(0), Decimal(1))
    positive = lambda name, default: _positive_decimal(setting(name, default), name)
    return SelectionConfig(
        ab_final_days=final_days,
        ab_return_floor_pct=_nonnegative_decimal(setting("ab_return_floor_pct", 4), "ab_return_floor_pct"),
        ab_return_divisor=_at_least_decimal(setting("ab_return_divisor", 10), "ab_return_divisor", Decimal(1)),
        ab_win_rate_floor_pct=_bounded_decimal_inclusive(setting("ab_win_rate_floor_pct", 55), "ab_win_rate_floor_pct", Decimal(0), Decimal(100)),
        ab_decline_cap_pct=_at_least_decimal(setting("ab_decline_cap_pct", 15), "ab_decline_cap_pct", _nonnegative_decimal(setting("ab_return_floor_pct", 4), "ab_return_floor_pct")),
        ab_completed_cycles=_positive_int(setting("ab_completed_cycles", 25, "ab_min_completed_cycles"), "ab_completed_cycles"),
        lot_full_dd5_multiplier=_at_least_decimal(setting("lot_full_dd5_multiplier", 1.10), "lot_full_dd5_multiplier", Decimal(1)),
        lot_full_dd_multiplier=_at_least_decimal(setting("lot_full_dd_multiplier", 1.10), "lot_full_dd_multiplier", Decimal(1)),
        lot_tolerance=_positive_decimal(setting("lot_tolerance", "1e-9"), "lot_tolerance"),
        hard_dd_pct=_positive_decimal(setting("hard_dd_pct", 23, "hard_cutoff_dd_pct"), "hard_dd_pct"),
        hard_dd_profit_multiplier=_positive_decimal(setting("hard_dd_profit_multiplier", 3), "hard_dd_profit_multiplier"),
        hard_pnl30_floor_pct=_nonnegative_decimal(setting("hard_pnl30_floor_pct", 4), "hard_pnl30_floor_pct"),
        hard_ratio=_positive_bounded_decimal(setting("hard_ratio", "0.75", "hard_cutoff_ratio"), "hard_ratio", Decimal(1)),
        hard_min_history_days=_positive_int(setting("hard_min_history_days", 45), "hard_min_history_days"),
        hard_min_cycles=_positive_int(setting("hard_min_cycles", 25), "hard_min_cycles"),
        top5_share_pct=_bounded_decimal_inclusive(setting("top5_share_pct", 80, "top5_max_profit_share_pct"), "top5_share_pct", Decimal(0), Decimal(100)),
        top5_min_history_days=_positive_int(setting("top5_min_history_days", 45), "top5_min_history_days"),
        top5_min_profitable_cycles=_positive_int(setting("top5_min_profitable_cycles", 25, "top5_min_profitable_trades"), "top5_min_profitable_cycles"),
        ab_trade_rate_divisor=_positive_decimal(selected.get("ab_trade_rate_divisor", 7), "ab_trade_rate_divisor"),
        plateau_points_pareto_pnl_multiplier=_positive_decimal(
            selected.get("plateau_points_pareto_pnl_multiplier", 2),
            "plateau_points_pareto_pnl_multiplier",
        ),
        best_trade_max_profit_share_pct=_bounded_decimal(
            selected.get("best_trade_max_profit_share_pct", 35), "best_trade_max_profit_share_pct", Decimal(0), Decimal(100)
        ),
        best_trade_min_profitable_trades=_positive_int(best_trade_min, "best_trade_min_profitable_trades"),
        shift_near_tie_min_advantage_bp=_positive_int(shift_advantage, "shift_near_tie_min_advantage_bp"),
        lot_variant_redundancy_enabled=lot_variant_enabled,
        researched_pnl_dd5_ratio=ratio("researched_pnl_dd5_ratio", "0.50"),
        researched_pnl_b_ratio=ratio("researched_pnl_b_ratio", "0.60"),
        researched_b_abs=positive("researched_b_abs", "2"),
        researched_b_rel=ratio("researched_b_rel", "0.25"),
        researched_dd5_abs=positive("researched_dd5_abs", "3"),
        researched_dd5_rel=ratio("researched_dd5_rel", "0.25"),
        researched_dd_abs=positive("researched_dd_abs", "1"),
        researched_dd_rel=ratio("researched_dd_rel", "0.25"),
        researched_points_mean_ratio=ratio("researched_points_mean_ratio", "0.40"),
        researched_points_same_floor_ratio=ratio("researched_points_same_floor_ratio", "0.40"),
        researched_points_cross_floor_ratio=ratio("researched_points_cross_floor_ratio", "0.30"),
        researched_points_single_best_ratio=ratio("researched_points_single_best_ratio", "0.10"),
        researched_open_ma_delta=positive("researched_open_ma_delta", "2.6"),
        researched_close_ma_delta=positive("researched_close_ma_delta", "3"),
        researched_hold_p95_ratio=ratio("researched_hold_p95_ratio", "0.20"),
        researched_hold_median_ratio=ratio("researched_hold_median_ratio", "0.30"),
        researched_hold_p95_veto_ratio=ratio("researched_hold_p95_veto_ratio", "0.15"),
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
    cohort_sql, cohort_params = _cohort_clause(request)
    rows = connection.execute(
        """with actions as (
                 select a.result_id, a.timestamp_utc, a.action_index, lower(a.action) as kind,
                        a.post_size, lower(a.post_side) as post_side
                   from strategy_actions a
                   join strategy_results r on r.result_id = a.result_id
                   join strategies s on s.strategy_id = r.strategy_id and s.current_result_id = r.result_id
                  where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?
                    """ + cohort_sql + """
                    and lower(a.action) in ('opened', 'increased', 'decreased', 'closed')
             ), numbered as (
                 select *, sum(case when kind = 'opened' and post_size <> 0 and post_side in ('long', 'short') then 1 else 0 end)
                    over (partition by result_id order by timestamp_utc, action_index rows unbounded preceding) as position_number
                   from actions
             ), intervals as (
                 select result_id, position_number,
                        min(timestamp_utc) filter (where kind = 'opened' and post_size <> 0 and post_side in ('long', 'short')) as opened_at,
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
        [request.symbol, request.side, *cohort_params],
    ).fetchall()
    return {
        int(result_id): (Decimal(str(p95)), Decimal(str(median)))
        for result_id, p95, median in rows if p95 is not None and median is not None
    }


def _holding_p95_minutes(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest
) -> dict[int, Decimal]:
    return {result_id: values[0] for result_id, values in _holding_quantiles_minutes(connection, request).items()}


def _window_b_holding_p95_minutes(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest, config: SelectionConfig
) -> dict[int, Decimal]:
    cohort_sql, cohort_params = _cohort_clause(request)
    rows = connection.execute(
        """with actions as (
                 select a.result_id, a.timestamp_utc, a.action_index, lower(a.action) as kind, a.post_size, lower(a.post_side) as post_side,
                        r.report_end_utc - (? * interval '1 day') as b_start, r.report_end_utc
                   from strategy_actions a
                   join strategy_results r on r.result_id = a.result_id
                   join strategies s on s.strategy_id = r.strategy_id and s.current_result_id = r.result_id
                  where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?
                    """ + cohort_sql + """
                    and lower(a.action) in ('opened', 'increased', 'decreased', 'closed')
             ), numbered as (
                 select *, sum(case when kind = 'opened' and post_size <> 0 and post_side in ('long', 'short') then 1 else 0 end)
                    over (partition by result_id order by timestamp_utc, action_index rows unbounded preceding) as position_number
                   from actions
             ), intervals as (
                 select result_id, position_number, min(b_start) as b_start, min(report_end_utc) as report_end_utc,
                        min(timestamp_utc) filter (where kind = 'opened' and post_size <> 0 and post_side in ('long', 'short')) as opened_at,
                        min(timestamp_utc) filter (where kind = 'closed' and post_size = 0) as closed_at
                   from numbered group by result_id, position_number
             )
             select result_id, cast(quantile_cont(date_diff('second', opened_at, closed_at) / cast(60 as decimal(20, 6)), .95) as decimal(38, 6))
               from intervals
              where opened_at is not null and closed_at between b_start and report_end_utc and closed_at >= opened_at
              group by result_id""",
        [config.ab_final_days, request.symbol, request.side, *cohort_params],
    ).fetchall()
    return {int(result_id): Decimal(str(p95)) for result_id, p95 in rows if p95 is not None}


def _completed_cycle_facts(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest, config: SelectionConfig,
) -> dict[int, dict[str, object]]:
    cohort_sql, cohort_params = _cohort_clause(request)
    rows = connection.execute(
        """with actions as (
                 select a.result_id, a.timestamp_utc, a.action_index, lower(a.action) as kind,
                         a.post_size, lower(a.post_side) as post_side, a.pnl, lower(s.side) as expected_side,
                         r.report_start_utc, r.report_end_utc, r.effective_start_utc, r.effective_end_utc,
                         case when a.post_size <> 0 and lower(a.post_side) in ('long', 'short')
                                   and lower(a.post_side) <> lower(s.side) then 1 else 0 end as side_flip
                   from strategy_actions a
                   join strategy_results r on r.result_id = a.result_id
                   join strategies s on s.strategy_id = r.strategy_id and s.current_result_id = r.result_id
                  where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?
                    """ + cohort_sql + """
                    and lower(a.action) in ('opened', 'increased', 'decreased', 'closed')
             ), numbered as (
                 select *, sum(case when kind = 'opened' and post_size <> 0 and post_side = expected_side then 1 else 0 end)
                    over (partition by result_id order by timestamp_utc, action_index rows unbounded preceding) as position_number
                   from actions
             ), trips as (
                  select result_id, position_number,
                         max(side_flip) as side_flip,
                         min(report_start_utc) as report_start_utc,
                         min(report_end_utc) as report_end_utc,
                         min(effective_start_utc) as effective_start_utc,
                         min(effective_end_utc) as effective_end_utc,
                         min(timestamp_utc) filter (where kind = 'opened' and post_size <> 0 and post_side = expected_side) as opened_at,
                         max(timestamp_utc) filter (where kind = 'closed' and post_size = 0) as closed_at,
                         max(case when kind = 'closed' and post_size = 0 then 1 else 0 end) as completed,
                         sum(pnl) filter (where kind in ('decreased', 'closed')) as trip_pnl,
                         count(*) filter (where kind in ('decreased', 'closed') and pnl is null) as missing_pnl
                    from numbered
                   group by result_id, position_number
              )
              select result_id, report_start_utc, report_end_utc, effective_start_utc, effective_end_utc,
                     opened_at, closed_at, trip_pnl, side_flip, completed, missing_pnl
                    from trips
                   where position_number > 0""",
         [request.symbol, request.side, *cohort_params],
    ).fetchall()
    grouped: dict[int, list[tuple[object, ...]]] = {}
    for row in rows:
        grouped.setdefault(int(row[0]), []).append(row)
    facts: dict[int, dict[str, object]] = {}
    for result_id, cycles in grouped.items():
        report_start = _utc_datetime(cycles[0][1])
        report_end = _utc_datetime(cycles[0][2])
        effective_start = _utc_datetime(cycles[0][3])
        effective_end = _utc_datetime(cycles[0][4])
        if report_end is None:
            facts[result_id] = {"completed_cycles_reliable": False}
            continue
        effective_start = effective_start or report_start or report_end
        effective_end = effective_end or report_end
        history_days = Decimal(str((effective_end - effective_start).total_seconds())) / Decimal(86400)
        reliable = all(bool(cycle[8] == 0) for cycle in cycles)
        completed = [cycle for cycle in cycles if cycle[9] == 1 and _decimal_or_none(cycle[7]) is not None]
        if any(cycle[9] == 1 and (cycle[10] or _decimal_or_none(cycle[7]) is None) for cycle in cycles):
            reliable = False
        full_pnls = [_decimal_or_none(cycle[7]) for cycle in completed]
        full_pnls = [pnl for pnl in full_pnls if pnl is not None]
        b_end = effective_end
        b_start = b_end - timedelta(days=config.ab_final_days)
        b_cycles = [cycle for cycle in completed if _utc_datetime(cycle[6]) is not None and b_start <= _utc_datetime(cycle[6]) <= b_end]
        b_pnls = [_decimal_or_none(cycle[7]) for cycle in b_cycles]
        b_pnls = [pnl for pnl in b_pnls if pnl is not None]
        if not reliable:
            facts[result_id] = {
                "completed_cycles_reliable": False, "history_days": history_days,
                "completed_cycle_count": None, "completed_profitable_cycle_count": None,
                "completed_cycle_net_pnl": None, "top5_pnl": None, "top5_share_pct": None,
                "pnl_after_top5": None, "ab_completed_cycle_count": None, "ab_win_rate_b_pct": None,
                "best_trade_profit_share_pct": None, "pnl_without_best_trade": None,
                "pnl_without_best_trade_pct": None, "completed_profitable_trade_count": None,
                "best_trade_reliable": False,
            }
            continue
        net = sum(full_pnls, Decimal(0))
        positive = sorted((pnl for pnl in full_pnls if pnl > 0), reverse=True)
        best = positive[0] if positive else None
        top5 = sum(positive[:5], Decimal(0))
        b_positive = sum(pnl > 0 for pnl in b_pnls)
        b_negative = sum(pnl < 0 for pnl in b_pnls)
        b_win_rate = Decimal(b_positive) * 100 / Decimal(b_positive + b_negative) if b_positive + b_negative else None
        facts[result_id] = {
            "completed_cycles_reliable": True, "history_days": history_days,
            "completed_cycle_count": len(full_pnls), "reliable_completed_cycle_count": len(full_pnls), "completed_profitable_cycle_count": len(positive),
            "completed_cycle_net_pnl": net, "top5_pnl": top5,
            "top5_share_pct": top5 / net * 100 if net > 0 else None,
            "pnl_after_top5": net - top5, "ab_completed_cycle_count": len(b_pnls),
            "ab_win_rate_b_pct": b_win_rate,
            "best_trade_profit_share_pct": best / sum(positive, Decimal(0)) * 100 if best is not None else None,
            "pnl_without_best_trade": net - best if best is not None else None,
            "pnl_without_best_trade_pct": None,
            "completed_profitable_trade_count": len(positive) if positive else None,
            "best_trade_reliable": True,
        }
    return facts


def _return_30d(
    metrics: WindowMetrics,
    report_start_utc: datetime | None = None,
    report_end_utc: datetime | None = None,
) -> Decimal | None:
    if not metrics.available or metrics.growth_factor is None:
        return None
    elapsed = calendar_window_days(metrics, report_start_utc, report_end_utc)
    if elapsed is None:
        return None
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


def _trade_rate_30d(
    metrics: WindowMetrics,
    report_start_utc: datetime | None = None,
    report_end_utc: datetime | None = None,
) -> Decimal | None:
    if not metrics.available or metrics.trade_count is None:
        return None
    days = calendar_window_days(metrics, report_start_utc, report_end_utc)
    if days is None:
        return None
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
        return _empty_ab_metrics()
    metrics_a, metrics_b = get_or_calculate_window_pair(
        connection, result_id, (report_start, split), (split, report_end)
    )
    return _ab_metrics_from_windows(metrics_a, metrics_b, report_start, report_end)


def _ab_metrics_from_windows(
    metrics_a: WindowMetrics,
    metrics_b: WindowMetrics,
    report_start_utc: datetime | None = None,
    report_end_utc: datetime | None = None,
) -> dict[str, Decimal | None]:
    return_a = _return_30d(metrics_a, report_start_utc, report_end_utc)
    return_b = _return_30d(metrics_b, report_start_utc, report_end_utc)
    return {
        "ab_pnl_change_30d_pct": None if return_a is None or return_b is None or return_a <= 0 else (return_b / return_a - 1) * 100,
        "ab_return_b_pct": metrics_b.return_pct,
        "ab_return_a_30d_pct": return_a, "ab_calendar_days_a": calendar_window_days(metrics_a, report_start_utc, report_end_utc),
        "ab_return_b_30d_pct": return_b, "ab_calendar_days_b": calendar_window_days(metrics_b, report_start_utc, report_end_utc),
        "ab_win_rate_b_pct": metrics_b.win_rate_pct, "ab_trade_rate_a_30d": _trade_rate_30d(metrics_a, report_start_utc, report_end_utc),
        "ab_trade_rate_b_30d": _trade_rate_30d(metrics_b, report_start_utc, report_end_utc), "ab_drawdown_b_pct": metrics_b.max_drawdown_pct,
    }


def _consistency_windows(report_start: datetime, report_end: datetime) -> tuple[tuple[datetime, datetime], ...]:
    span = report_end - report_start
    if span >= timedelta(days=28):
        count = 4
    elif span >= timedelta(days=21):
        count = 3
    else:
        return ()
    return tuple(
        (report_start + span * index / count, report_end if index == count - 1 else report_start + span * (index + 1) / count)
        for index in range(count)
    )


def _selection_windows(report_start: datetime, report_end: datetime, config: SelectionConfig) -> tuple[tuple[datetime, datetime], ...]:
    split = report_end - timedelta(days=config.ab_final_days)
    windows = [(report_start, report_end)]
    if split > report_start:
        windows.extend(((report_start, split), (split, report_end)))
    windows.extend(_consistency_windows(report_start, report_end))
    return tuple(dict.fromkeys(windows))


def _cached_many(
    connection: duckdb.DuckDBPyConnection,
    result_id: int,
    windows: Sequence[tuple[datetime, datetime]],
    version: str,
) -> tuple[WindowMetrics | None, ...]:
    if not windows:
        return ()
    pairs = tuple(dict.fromkeys(windows))
    predicates = " or ".join(
        "(requested_start_utc = ? and requested_end_utc = ?)" for _ in pairs
    )
    parameters: list[object] = [result_id, version]
    parameters.extend(value for pair in pairs for value in pair)
    rows = connection.execute(
        "select " + ", ".join(_METRIC_COLUMNS) + " from window_metrics "
        "where result_id = ? and metrics_version = ? and (" + predicates + ")",
        parameters,
    ).fetchall()
    by_window = {
        (metric.requested_start_utc, metric.requested_end_utc): metric
        for metric in (_metric_from_row(row) for row in rows)
    }
    return tuple(by_window.get(window) for window in windows)


def _empty_ab_metrics() -> dict[str, Decimal | None]:
    return {key: None for key in (
        "ab_pnl_change_30d_pct", "ab_return_b_pct", "ab_return_a_30d_pct", "ab_calendar_days_a", "ab_calendar_days_b", "ab_return_b_30d_pct",
        "ab_win_rate_b_pct", "ab_trade_rate_a_30d", "ab_trade_rate_b_30d", "ab_drawdown_b_pct",
    )}


def _selection_cached_metrics(connection: duckdb.DuckDBPyConnection, request: SelectionRequest) -> dict[tuple[int, datetime, datetime], WindowMetrics]:
    cohort_sql, cohort_params = _cohort_clause(request)
    rows = connection.execute(
        "select " + ", ".join(f"wm.{column}" for column in _METRIC_COLUMNS) +
        " from window_metrics wm"
        " join strategy_results r on r.result_id = wm.result_id"
        " join strategies s on s.strategy_id = r.strategy_id and s.current_result_id = r.result_id"
        " where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ? and wm.metrics_version = ?" + cohort_sql,
        [request.symbol, request.side, METRICS_VERSION, *cohort_params],
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
        return (full, _empty_ab_metrics())
    a = cached(report_start, split)
    b = cached(split, report_end)
    if a is None or b is None:
        return (full, _empty_ab_metrics())
    return (full, _ab_metrics_from_windows(a, b, report_start, report_end))


def _consistency_summary(
    metrics: Sequence[WindowMetrics | None], windows: Sequence[tuple[datetime, datetime]],
) -> tuple[int | None, int | None, str]:
    windows = tuple(windows)
    window_count = len(windows)
    if not window_count or len(metrics) != window_count:
        return None, None, "UNAVAILABLE"
    positive = 0
    assessed = 0
    for metric, (window_start, window_end) in zip(metrics, windows):
        if metric is None:
            return None, None, "UNAVAILABLE"
        if metric.availability_status == "NO_TRADES" or metric.unavailable_reason == "NO_TRADES":
            continue
        if not metric.available:
            return None, None, "UNAVAILABLE"
        value = _return_30d(metric, window_start, window_end)
        if value is None:
            return None, None, "UNAVAILABLE"
        assessed += 1
        positive += value > 0
    if assessed == 0:
        return None, 0, "UNAVAILABLE"
    threshold = 3 if window_count == 4 else 2
    return positive, assessed, "PASS" if positive >= threshold else "FAIL"


def _cached_positive_quarters(
    result_id: int, report_start: datetime, report_end: datetime, config: SelectionConfig,
    cached_metrics: Mapping[tuple[int, datetime, datetime], WindowMetrics],
) -> tuple[int | None, int | None, str]:
    windows = _consistency_windows(report_start, report_end)
    metrics = [cached_metrics.get((result_id, start, end)) for start, end in windows]
    return _consistency_summary(metrics, windows)


@dataclass(frozen=True, slots=True)
class _SelectionWindowJobResult:
    metrics: tuple[WindowMetrics, ...]
    equity_publication: tuple[Mapping[str, object], EquityQualityFacts] | None
    source_recheck: Mapping[str, object] | None = None


def _facts_from_full_source(
    result_id: int, source: tuple[datetime, datetime, tuple[object, ...], tuple[object, ...]]
) -> EquityQualityFacts:
    report_start, report_end, _actions, equity = source
    samples = tuple(
        EquitySample(result_id, item.index, item.timestamp, item.equity)
        for item in equity
    )
    return calculate_equity_quality_facts(result_id, report_start, report_end, samples)


def _selection_window_job(
    database: str, result_id: int, report_start: datetime, report_end: datetime, final_days: int,
    include_equity: bool = False,
) -> _SelectionWindowJobResult:
    """Compute missing windows/facts in one bounded read-only worker."""
    windows = _selection_windows(report_start, report_end, SelectionConfig(ab_final_days=final_days))
    with duckdb.connect(database, read_only=True) as connection:
        connection.execute("set threads to 1")
        cached = _cached_many(connection, result_id, windows, METRICS_VERSION)
        missing_windows = any(metric is None for metric in cached)
        metadata: Mapping[str, object] | None = None
        equity_missing = False
        if include_equity:
            metadata = current_equity_source_metadata(connection, result_id)
            revision = equity_source_revision(metadata)
            equity_facts = _read_selection_equity_facts(connection, result_id, revision)
            equity_missing = equity_facts is None
        else:
            equity_facts = None
        if not missing_windows and not equity_missing:
            return _SelectionWindowJobResult((), None)

        publication: tuple[Mapping[str, object], EquityQualityFacts] | None = None
        source_recheck: Mapping[str, object] | None = None
        if missing_windows:
            source = _load_source(connection, result_id)
            initial_balance = _load_initial_balance(connection, result_id)
            if include_equity:
                source_recheck = metadata
            calculated = tuple(
                metric if metric is not None else _calculate(
                    result_id, start, end, METRICS_VERSION, *source,
                    ordered_source=True,
                    initial_balance=initial_balance,
                )
                for metric, (start, end) in zip(cached, windows)
            )
            if equity_missing:
                if metadata is None:
                    raise PerformanceV2SelectionError("EQUITY_SOURCE_METADATA_MISSING")
                publication = (metadata, _facts_from_full_source(result_id, source))
            # Preserve the old no-flag write path, while a new opt-in warm branch
            # never rewrites already-cached window rows.
            metrics_to_write = calculated if not include_equity else tuple(
                metric for metric, cached_metric in zip(calculated, cached) if cached_metric is None
            )
        else:
            if not include_equity or metadata is None:
                raise PerformanceV2SelectionError("EQUITY_SOURCE_METADATA_MISSING")
            samples, summary = _load_equity_samples_for_quality(
                connection, result_id,
                metadata["report_start_utc"], metadata["report_end_utc"],
            )
            facts = calculate_equity_quality_facts(
                result_id, metadata["report_start_utc"], metadata["report_end_utc"], samples
            )
            source_recheck = metadata
            invalid_reasons = tuple(sorted(set(facts.invalid_reasons).union(summary.invalid_reasons)))
            publication = (
                metadata,
                replace(
                    facts,
                    raw_sample_count=summary.raw_sample_count,
                    in_report_sample_count=summary.in_report_sample_count,
                    nonpositive_in_report_rows=summary.nonpositive_in_report_rows,
                    duplicate_timestamp_count=summary.duplicate_timestamp_count,
                    invalid_reasons=invalid_reasons,
                ),
            )
            metrics_to_write = ()
    return _SelectionWindowJobResult(tuple(metrics_to_write), publication, source_recheck)


def _selection_window_job_from_args(
    args: tuple[str, int, datetime, datetime, int] | tuple[str, int, datetime, datetime, int, bool],
) -> _SelectionWindowJobResult:
    return _selection_window_job(*args)


def _equity_metadata_from_selection_row(row: Sequence[object]) -> dict[str, object]:
    names = (
        "result_id", "report_start_utc", "report_end_utc", "imported_at_utc",
        "effective_start_utc", "effective_end_utc", "optimizer_source_metadata_json",
    )
    metadata = dict(zip(names, (row[1], row[2], row[3], row[4], row[5], row[6], row[7])))
    for name in ("report_start_utc", "report_end_utc", "imported_at_utc", "effective_start_utc", "effective_end_utc"):
        value = metadata[name]
        if value is not None:
            metadata[name] = value.astimezone(timezone.utc)
    return metadata


def _require_equity_read_schema(connection: duckdb.DuckDBPyConnection) -> int:
    try:
        version = require_performance_v2_readable(connection)
    except PerformanceV2StoreError as error:
        raise EquityCacheSchemaInvalidError("Performance database schema is invalid") from error
    if version == 5:
        raise EquitySchemaUpgradeRequiredError("EQUITY_SCHEMA_UPGRADE_REQUIRED")
    return version


def _read_selection_equity_facts(
    connection: duckdb.DuckDBPyConnection, result_id: int, source_revision: str,
) -> EquityQualityFacts | None:
    _require_equity_read_schema(connection)
    row = connection.execute(
        """select source_revision, algo_version, facts_json, facts_sha256
             from equity_quality_metrics where result_id = ? and algo_version = ?""",
        [result_id, ALGORITHM_VERSION],
    ).fetchone()
    return _decode_selection_equity_cache_row(row, result_id, source_revision)


def _decode_selection_equity_cache_row(
    row: Sequence[object] | None, result_id: int, source_revision: str,
) -> EquityQualityFacts | None:
    if row is None or row[0] != source_revision or row[1] != ALGORITHM_VERSION:
        return None
    try:
        facts = decode_equity_facts(row[2], row[3])
    except EquityQualityCacheError:
        return None
    if facts.result_id != result_id:
        return None
    return facts


def _selection_equity_facts_by_result(
    connection: duckdb.DuckDBPyConnection, rows: Sequence[Sequence[object]],
) -> dict[int, dict[str, object]]:
    """Hydrate fresh facts or an explicit cache sentinel with one scoped read."""
    try:
        schema_version = require_performance_v2_readable(connection)
    except PerformanceV2StoreError as error:
        raise EquityCacheSchemaInvalidError("Performance database schema is invalid") from error
    if not rows:
        return {}
    result_ids = tuple(dict.fromkeys(int(row[1]) for row in rows))
    if schema_version == 5:
        return {result_id: {"status": "SCHEMA5"} for result_id in result_ids}
    cached_rows = connection.execute(
        """select result_id, source_revision, algo_version, facts_json, facts_sha256
             from equity_quality_metrics where result_id in ("""
        + ",".join("?" for _ in result_ids) + ")",
        list(result_ids),
    ).fetchall()
    cached_by_result: dict[int, Sequence[object]] = {}
    for cached_row in cached_rows:
        result_id = int(cached_row[0])
        previous = cached_by_result.get(result_id)
        if previous is None or (
            previous[2] != ALGORITHM_VERSION and cached_row[2] == ALGORITHM_VERSION
        ):
            cached_by_result[result_id] = cached_row
    facts_by_result: dict[int, dict[str, object]] = {}
    for row in rows:
        result_id = int(row[1])
        cached = cached_by_result.get(result_id)
        if cached is None:
            facts_by_result[result_id] = {"status": "ABSENT"}
            continue
        try:
            metadata = _equity_metadata_from_selection_row(row)
            source_revision = equity_source_revision(metadata)
        except EquityQualityCacheError:
            facts_by_result[result_id] = {"status": "INVALID"}
            continue
        if cached[1] != source_revision or cached[2] != ALGORITHM_VERSION:
            facts_by_result[result_id] = {"status": "STALE"}
            continue
        try:
            facts = decode_equity_facts(cached[3], cached[4])
            if facts.result_id != result_id:
                raise EquityQualityCacheError("cached result id does not match candidate")
        except EquityQualityCacheError:
            facts_by_result[result_id] = {"status": "INVALID"}
            continue
        facts_by_result[result_id] = {
            "status": "FRESH", "facts": facts,
            "source_revision": source_revision, "facts_sha256": str(cached[4]),
        }
    return facts_by_result


def _equity_cache_ready_by_result(
    connection: duckdb.DuckDBPyConnection,
    rows: Sequence[Sequence[object]],
) -> dict[int, bool]:
    """Validate a scoped facts batch after one catalog check for this connection."""
    _require_equity_read_schema(connection)
    result_ids = tuple(dict.fromkeys(int(row[1]) for row in rows))
    if not result_ids:
        return {}
    raw = connection.execute(
        """select result_id, source_revision, algo_version, facts_json, facts_sha256
             from equity_quality_metrics where algo_version = ? and result_id in ("""
        + ",".join("?" for _ in result_ids) + ")",
        [ALGORITHM_VERSION, *result_ids],
    ).fetchall()
    cache_rows = {int(row[0]): row for row in raw}
    ready: dict[int, bool] = {}
    for row in rows:
        result_id = int(row[1])
        cached = cache_rows.get(result_id)
        if cached is None:
            ready[result_id] = False
            continue
        try:
            metadata = _equity_metadata_from_selection_row(row)
            if cached[1] != equity_source_revision(metadata) or cached[2] != ALGORITHM_VERSION:
                ready[result_id] = False
                continue
            facts = decode_equity_facts(cached[3], cached[4])
            ready[result_id] = facts.result_id == result_id
        except (EquityQualityCacheError, AttributeError, TypeError, ValueError):
            ready[result_id] = False
    return ready


def selection_equity_facts_token(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest,
    *, schema_version: int | None = None,
) -> tuple[object, ...]:
    """Current scoped source revisions and fact digests for Panel memoization."""
    cohort_sql, cohort_params = _cohort_clause(request)
    rows = connection.execute(
        """select s.strategy_id, r.result_id, r.report_start_utc, r.report_end_utc,
                  r.imported_at_utc, r.effective_start_utc, r.effective_end_utc,
                  r.optimizer_source_metadata_json
             from strategies s join strategy_results r
               on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
            where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?""" + cohort_sql + " order by s.strategy_id",
        [request.symbol, request.side, *cohort_params],
    ).fetchall()
    if schema_version is not None:
        version = schema_version
    else:
        try:
            version = require_performance_v2_readable(connection)
        except PerformanceV2StoreError as error:
            raise EquityQualityCacheError("Performance database schema is invalid") from error
    cache_rows: dict[int, tuple[object, object]] = {}
    if version in {6, 7, 8} and rows:
        ids = tuple(int(row[1]) for row in rows)
        raw = connection.execute(
            """select result_id, source_revision, facts_sha256 from equity_quality_metrics
                 where algo_version = ? and result_id in (""" + ",".join("?" for _ in ids) + ")",
            [ALGORITHM_VERSION, *ids],
        ).fetchall()
        cache_rows = {int(row[0]): (row[1], row[2]) for row in raw}
    token: list[object] = [ALGORITHM_VERSION]
    for row in rows:
        result_id = int(row[1])
        metadata = _equity_metadata_from_selection_row(row)
        revision = equity_source_revision(metadata)
        cached = cache_rows.get(result_id)
        digest = cached[1] if cached is not None and cached[0] == revision else None
        token.append((result_id, revision, digest))
    return tuple(token)


def _selection_cache_missing_strategy_ids(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest, config: SelectionConfig,
    *, include_equity: bool = False,
) -> tuple[int, ...]:
    _verify_retest_cohort(connection, request)
    cohort_sql, cohort_params = _cohort_clause(request)
    rows = connection.execute(
        """select s.strategy_id, r.result_id, r.report_start_utc, r.report_end_utc,
                  r.imported_at_utc, r.effective_start_utc, r.effective_end_utc,
                  r.optimizer_source_metadata_json from strategies s
             join strategy_results r on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
            where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?""" + cohort_sql + """
            order by s.strategy_id""",
        [request.symbol, request.side, *cohort_params],
    ).fetchall()
    cached_metrics = _selection_cached_metrics(connection, request)
    equity_ready = _equity_cache_ready_by_result(connection, rows) if include_equity else {}
    missing: list[int] = []
    for row in rows:
        strategy_id, result_id, report_start, report_end = row[:4]
        windows = _selection_windows(report_start, report_end, config)
        old_missing = any(
            cached_metrics.get((int(result_id), window_start, window_end)) is None
            for window_start, window_end in windows
        )
        if old_missing or (include_equity and not equity_ready.get(int(result_id), False)):
            missing.append(int(strategy_id))
    return tuple(missing)


def selection_cache_missing_strategy_ids(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest, config: SelectionConfig,
    *, include_equity: bool = False,
) -> tuple[int, ...]:
    """Return active strategies whose current result lacks a required cache window."""
    return _selection_cache_missing_strategy_ids(connection, request, config, include_equity=include_equity)


def prepare_selection_window_cache(
    database: Path, request: SelectionRequest, config: SelectionConfig, workers: int,
    strategy_ids: Sequence[int] | None = None, *, include_equity: bool = False,
    on_batch_complete: Callable[[int], None] | None = None,
) -> None:
    """Warm bounded result batches in independent readers and one checked writer."""
    selected_ids = None if strategy_ids is None else tuple(dict.fromkeys(int(strategy_id) for strategy_id in strategy_ids))
    if request.ranking_scope == "RETEST_COHORT":
        _verify_retest_cohort_for_database(database, request)
        allowed_ids = tuple(strategy_id for strategy_id, _ in request.cohort_members)
        if selected_ids is None:
            selected_ids = allowed_ids
        else:
            allowed = set(allowed_ids)
            selected_ids = tuple(strategy_id for strategy_id in selected_ids if strategy_id in allowed)
    if selected_ids == ():
        return
    with duckdb.connect(str(database), read_only=True) as connection:
        where = "where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?"
        parameters: list[object] = [request.symbol, request.side]
        if selected_ids is not None:
            where += " and s.strategy_id in (" + ",".join("?" for _ in selected_ids) + ")"
            parameters.extend(selected_ids)
        rows = connection.execute(
            """select r.result_id, r.report_start_utc, r.report_end_utc from strategies s
                 join strategy_results r on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
                """ + where,
            parameters,
        ).fetchall()
    if not rows:
        return
    worker_count = max(1, min(int(workers), len(rows)))
    batch_size = 2 * worker_count
    jobs = [
        (str(database), int(result_id), report_start, report_end, config.ab_final_days, include_equity)
        for result_id, report_start, report_end in rows
    ]
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for offset in range(0, len(jobs), batch_size):
            pending = {
                executor.submit(_selection_window_job_from_args, job): job
                for job in jobs[offset : offset + batch_size]
            }
            completed: list[_SelectionWindowJobResult] = []
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.pop(future)
                    completed.append(future.result())
            metrics = [metric for result in completed for metric in result.metrics]
            publications = [
                result.equity_publication for result in completed
                if result.equity_publication is not None
            ]
            source_rechecks = [
                result.source_recheck for result in completed
                if result.source_recheck is not None
            ]
            publication_ids = {int(metadata["result_id"]) for metadata, _ in publications}
            source_rechecks = [
                metadata for metadata in source_rechecks
                if int(metadata["result_id"]) not in publication_ids
            ]
            if not metrics and not publications:
                if on_batch_complete is not None:
                    on_batch_complete(len(completed))
                continue
            with duckdb.connect(str(database)) as writer:
                writer.execute("begin transaction")
                try:
                    for metadata in source_rechecks:
                        result_id = int(metadata["result_id"])
                        current = current_equity_source_metadata(writer, result_id)
                        if equity_source_revision(current) != equity_source_revision(metadata):
                            raise EquitySourceChangedError("EQUITY_SOURCE_CHANGED")
                    _persist_many(writer, metrics)
                    if publications:
                        upsert_equity_quality_facts_checked(
                            writer, publications, calculated_at_utc=datetime.now(timezone.utc)
                        )
                    writer.execute("commit")
                except BaseException:
                    writer.execute("rollback")
                    raise
            if on_batch_complete is not None:
                on_batch_complete(len(completed))


def selection_cache_status(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest, config: SelectionConfig,
    *, include_equity: bool = False, include_readiness_breakdown: bool = False,
) -> dict[str, int | bool]:
    _verify_retest_cohort(connection, request)
    cohort_sql, cohort_params = _cohort_clause(request)
    rows = connection.execute(
        """select s.strategy_id, r.result_id, r.report_start_utc, r.report_end_utc,
                  r.imported_at_utc, r.effective_start_utc, r.effective_end_utc,
                  r.optimizer_source_metadata_json from strategies s
             join strategy_results r on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
            where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?""" + cohort_sql,
        [request.symbol, request.side, *cohort_params],
    ).fetchall()
    cached_metrics = _selection_cached_metrics(connection, request)
    equity_ready = _equity_cache_ready_by_result(connection, rows) if include_equity else {}
    missing = 0
    window_missing = 0
    equity_missing = 0
    for row in rows:
        result_id, start, end = row[1], row[2], row[3]
        windows = _selection_windows(start, end, config)
        old_missing = any(
            cached_metrics.get((int(result_id), window_start, window_end)) is None
            for window_start, window_end in windows
        )
        current_equity_missing = include_equity and not equity_ready.get(int(result_id), False)
        window_missing += int(old_missing)
        equity_missing += int(current_equity_missing)
        if old_missing or current_equity_missing:
            missing += 1
    status: dict[str, int | bool] = {"total": len(rows), "missing": missing, "ready": bool(rows) and missing == 0}
    if include_readiness_breakdown:
        status["window_missing"] = window_missing
        status["equity_missing"] = equity_missing
    return status


def load_selection_candidates(
    connection: duckdb.DuckDBPyConnection,
    request: SelectionRequest,
    config: SelectionConfig = SelectionConfig(),
    *, cache_only: bool = False,
) -> pd.DataFrame:
    """Load all current ACTIVE candidates for one Pair + Side without filtering them."""
    _verify_retest_cohort(connection, request)
    cohort_sql, cohort_params = _cohort_clause(request)
    equity_source_columns = " r.imported_at_utc, r.optimizer_source_metadata_json,"
    holding_minutes = _holding_quantiles_minutes(connection, request)
    b_holding_minutes = _window_b_holding_p95_minutes(connection, request, config)
    cycle_facts = _completed_cycle_facts(connection, request, config)
    cached_metrics = _selection_cached_metrics(connection, request) if cache_only else None
    rows = connection.execute(
        """select s.strategy_id, s.strategy_name, s.symbol, s.side, s.timeframe, s.close_ma_len,
                  s.order_count, r.result_id, r.report_start_utc, r.report_end_utc,
                  r.reported_start_utc, r.reported_end_utc, r.effective_start_utc, r.effective_end_utc,
                  """ + equity_source_columns + """ r.initial_balance,
                  r.total_pnl, r.total_pnl_pct, r.max_drawdown, r.max_drawdown_pct,
                  r.total_fees, r.total_trades, o.order_id, o.analysis_run_id, o.plateau_id,
                  o.open_ma_len, o.open_multiplier, o.shift_bp, o.lot_x, p.plateau_point_count
             from strategies s
             join strategy_results r on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
             left join strategy_orders o on o.strategy_id = s.strategy_id
             left join analysis_plateaus p on p.analysis_run_id = o.analysis_run_id and p.plateau_id = o.plateau_id
            where s.lifecycle_status = 'ACTIVE' and s.symbol = ? and s.side = ?""" + cohort_sql + """
            order by s.strategy_name, s.strategy_id, o.order_id""",
        [request.symbol, request.side, *cohort_params],
    ).fetchall()
    candidates: dict[int, dict[str, object]] = {}
    equity_source_rows: dict[int, tuple[object, ...]] = {}
    for row in rows:
        (
            strategy_id, strategy_name, symbol, side, timeframe, close_ma_len, order_count,
            result_id, report_start, report_end, reported_start, reported_end, effective_start, effective_end,
            *equity_source,
            initial_balance, total_pnl, total_pnl_pct, max_drawdown,
            max_drawdown_pct, total_fees, total_trades, order_id, analysis_run_id, plateau_id,
            open_ma_len, open_multiplier, shift_bp, lot_x, plateau_count,
        ) = row
        candidate = candidates.get(int(strategy_id))
        if candidate is None:
            result_id = int(result_id)
            equity_source_rows[result_id] = (
                int(strategy_id), result_id, report_start, report_end,
                equity_source[0], effective_start, effective_end, equity_source[1],
            )
            if cache_only:
                full_metrics, ab_metrics = _cached_selection_metrics(
                    connection, result_id, report_start, report_end, config, cached_metrics
                )
                positive_quarters = _cached_positive_quarters(result_id, report_start, report_end, config, cached_metrics or {})
            else:
                full_metrics = get_or_calculate_window_pair(connection, result_id, (report_start, report_end), (report_start, report_end))[0]
                ab_metrics = _ab_metrics(connection, result_id, report_start, report_end, config)
                quarter_metrics = [
                    get_or_calculate_window(connection, result_id, start, end)
                    for start, end in _consistency_windows(report_start, report_end)
                ]
                positive_quarters = _consistency_summary(
                    quarter_metrics, _consistency_windows(report_start, report_end)
                )
            daily_log = None if full_metrics is None else full_metrics.daily_log_return
            pnl_30d = None if full_metrics is None else _return_30d(full_metrics, report_start, report_end)
            drawdown = _decimal_or_none(max_drawdown_pct)
            risk_scale = Decimal(5) / drawdown if drawdown is not None and drawdown > 0 else None
            completed_facts = cycle_facts.get(result_id, {"completed_cycles_reliable": False})
            initial_balance_value = _decimal_or_none(initial_balance)
            candidate = {
                "strategy_id": int(strategy_id), "strategy_name": str(strategy_name), "symbol": str(symbol),
                "side": str(side), "timeframe": str(timeframe), "close_ma_len": int(close_ma_len),
                "order_count": int(order_count), "result_id": result_id, "total_pnl": _decimal_or_none(total_pnl),
                "total_pnl_pct": _decimal_or_none(total_pnl_pct), "max_drawdown": _decimal_or_none(max_drawdown),
                "max_drawdown_pct": drawdown, "total_fees": _decimal_or_none(total_fees),
                "report_start_utc": report_start, "report_end_utc": report_end,
                "reported_start_utc": reported_start, "reported_end_utc": reported_end,
                "effective_start_utc": effective_start, "effective_end_utc": effective_end,
                "initial_balance": initial_balance_value,
                "total_trades": None if full_metrics is None else full_metrics.trade_count,
                "trades_30d": None if full_metrics is None else _trade_rate_30d(full_metrics, report_start, report_end),
                "pnl_30d_pct": pnl_30d,
                "profit_factor": None if full_metrics is None else full_metrics.profit_factor,
                "win_rate_pct": None if full_metrics is None else full_metrics.win_rate_pct,
                "risk_scale": risk_scale, "dd5_proxy": pnl_30d * risk_scale if pnl_30d is not None and risk_scale is not None else None,
                "holding_p95_minutes": holding_minutes.get(result_id, (None, None))[0],
                "holding_median_minutes": holding_minutes.get(result_id, (None, None))[1],
                **ab_metrics, "ab_holding_p95_minutes": b_holding_minutes.get(result_id),
                "first_shift_bp": None, "scaled_lot_sum": None, "capital_proxy": None,
                "capital_efficiency": None, "total_plateau_point_count": None,
                **completed_facts,
                "positive_quarter_count": positive_quarters[0],
                "positive_quarter_available_count": positive_quarters[1],
                "positive_quarter_status": positive_quarters[2],
                "robust_pnl_30d_pct": None,
                "worst_drawdown_pct": None, "worst_holding_p95_minutes": None,
                "ab_stability_ratio": None, "minimum_plateau_point_count": None,
                "lot_variant_group_key": None, "lot_variant_representative_strategy_id": pd.NA,
            }
            if candidate.get("pnl_without_best_trade") is not None and initial_balance_value is not None and initial_balance_value > 0:
                candidate["pnl_without_best_trade_pct"] = candidate["pnl_without_best_trade"] / initial_balance_value * 100
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
            candidate[f"order_{number}_plateau_key"] = (
                (str(analysis_run_id), str(plateau_id))
                if analysis_run_id is not None and plateau_id is not None else None
            )
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
            candidate["minimum_plateau_point_count"] = min(points)
        a, b = candidate["ab_return_a_30d_pct"], candidate["ab_return_b_30d_pct"]
        if a is not None and b is not None:
            candidate["robust_pnl_30d_pct"] = min(a, b)
            if a > 0 and b > 0:
                candidate["ab_stability_ratio"] = min(a, b) / max(a, b)
        full_dd, b_dd = candidate["max_drawdown_pct"], candidate["ab_drawdown_b_pct"]
        if full_dd is not None and b_dd is not None and full_dd >= 0 and b_dd >= 0:
            candidate["worst_drawdown_pct"] = max(full_dd, b_dd)
        full_hold, b_hold = candidate["holding_p95_minutes"], candidate["ab_holding_p95_minutes"]
        if full_hold is not None and b_hold is not None:
            candidate["worst_holding_p95_minutes"] = max(full_hold, b_hold)
    equity_consumer_enabled = any(
        stage.enabled and (
            stage.id == "filter_equity_regime"
            or (stage.id == "rank_robust_top_n" and stage.method == "equity_quality_v1")
        )
        for stage in request.stages
    )
    if equity_consumer_enabled and not equity_source_rows:
        _require_equity_read_schema(connection)
        facts_by_result = {}
    else:
        facts_by_result = _selection_equity_facts_by_result(connection, tuple(equity_source_rows.values()))
    for candidate in candidates.values():
        cached = facts_by_result.get(
            int(candidate["result_id"]), {"status": "ABSENT"}
        )
        if equity_consumer_enabled and cached.get("status") != "FRESH":
            if cached.get("status") == "SCHEMA5":
                raise EquitySchemaUpgradeRequiredError("EQUITY_SCHEMA_UPGRADE_REQUIRED")
            raise _error("EQUITY_CACHE_INCOMPLETE")
        candidate["_equity_cache"] = cached
    columns = (*_CANDIDATE_COLUMNS, "_equity_cache")
    return pd.DataFrame.from_records(list(candidates.values())).reindex(columns=columns)


_PARETO_OBJECTIVES = {
    "pareto_window_b": (("ab_return_b_30d_pct", "ab_trade_rate_b_30d"), ("ab_drawdown_b_pct", "ab_holding_p95_minutes")),
    "pareto_window_b_dd_shift": (("ab_return_b_30d_pct", "first_shift_bp"), ("max_drawdown_pct",)),
    "pareto_dd5_balanced": (("dd5_proxy", "first_shift_bp"), ("capital_proxy", "holding_p95_minutes", "close_ma_len")),
    "pareto_efficiency_shift": (("capital_efficiency", "first_shift_bp"), ()),
    "pareto_dd5_holding": (("dd5_proxy",), ("holding_p95_minutes",)),
    "pareto_dd5_close_ma": (("dd5_proxy",), ("close_ma_len",)),
    "pareto_dd5_first_shift": (("dd5_proxy", "first_shift_bp"), ()),
    "pareto_conditional_close_ma": (("capital_efficiency",), ("close_ma_len",)),
    "pareto_primary": (("dd5_proxy",), ("capital_proxy",)),
    "pareto_dd5_capital": (("dd5_proxy",), ("capital_proxy",)),
    "pareto_robust": (("robust_pnl_30d_pct", "first_shift_bp"), ("worst_drawdown_pct", "worst_holding_p95_minutes")),
}


def _present(value: object) -> bool:
    return value is not None and not pd.isna(value)


def _analog_group_keys(survivors: pd.DataFrame) -> pd.Series:
    """Partition exact plateau structures into non-transitive adjacent Close-MA groups."""
    groups: dict[tuple[object, ...], list[tuple[int, int, object]]] = {}
    keys: dict[object, tuple[object, ...]] = {}
    required = ("symbol", "side", "timeframe", "order_count", "close_ma_len")
    for index, row in survivors.iterrows():
        if not all(_present(row.get(column)) for column in required):
            keys[index] = ("__strategy__", int(row["strategy_id"]))
            continue
        order_count = int(row["order_count"])
        plateaus = tuple(row.get(f"order_{order}_plateau_key") for order in range(1, order_count + 1))
        if not plateaus or not all(_present(plateau) for plateau in plateaus):
            keys[index] = ("__strategy__", int(row["strategy_id"]))
            continue
        base = (row["symbol"], row["side"], row["timeframe"], order_count, *plateaus)
        groups.setdefault(base, []).append((int(row["close_ma_len"]), int(row["strategy_id"]), index))
    for base, members in groups.items():
        start: int | None = None
        bucket = 0
        for close_ma, _, index in sorted(members):
            if start is None or close_ma > start + 1:
                start = close_ma
                bucket += 1
            keys[index] = (*base, "close_ma", start, bucket)
    return pd.Series(keys)


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


def _near_tie_eliminated(
    group: pd.DataFrame, stage: SelectionStage, preference_column: str, *, higher_is_better: bool, advantage: Decimal = Decimal(0),
) -> list[object]:
    columns = ("robust_pnl_30d_pct", preference_column, "worst_drawdown_pct", "worst_holding_p95_minutes")
    values = group.loc[:, columns].to_numpy(dtype=object)
    valid = ~pd.isna(values).any(axis=1)
    comparable = values[valid]
    indexes = group.index[valid]
    tolerance = Decimal(1) - (stage.pnl_tolerance_pct or Decimal(10)) / Decimal(100)
    eliminated: list[object] = []
    for candidate_index, candidate in enumerate(comparable):
        pnl, preference, drawdown, holding = candidate
        if pnl <= 0:
            continue
        preferred = comparable[:, 1] >= preference + float(advantage) if higher_is_better else comparable[:, 1] < preference
        dominates = (
            (comparable[:, 0] > 0)
            & preferred
            & (comparable[:, 0] >= pnl * tolerance)
            & (comparable[:, 2] <= drawdown)
            & (comparable[:, 3] <= holding)
        )
        if np.any(dominates):
            eliminated.append(indexes[candidate_index])
    return eliminated


def _shift_near_tie_eliminated(group: pd.DataFrame, stage: SelectionStage, config: SelectionConfig) -> list[object]:
    return _near_tie_eliminated(
        group, stage, "first_shift_bp", higher_is_better=True, advantage=Decimal(config.shift_near_tie_min_advantage_bp),
    )


def _close_ma_near_tie_eliminated(group: pd.DataFrame, stage: SelectionStage) -> list[object]:
    return _near_tie_eliminated(group, stage, "close_ma_len", higher_is_better=False)


_RANK_COMPONENTS = (
    ("robust_pnl", "robust_pnl_30d_pct", Decimal(".30"), True, False),
    ("worst_drawdown", "worst_drawdown_pct", Decimal(".15"), False, False),
    ("ab_stability", "ab_stability_ratio", Decimal(".15"), True, False),
    ("worst_holding", "worst_holding_p95_minutes", Decimal(".12"), False, False),
    ("first_shift", "first_shift_bp", Decimal(".10"), True, False),
    ("minimum_plateau_points", "minimum_plateau_point_count", Decimal(".09"), True, True),
    ("close_ma", "close_ma_len", Decimal(".09"), False, False),
)


def _quality_percentiles(values: pd.Series, *, higher_is_better: bool, by_timeframe: pd.Series | None = None) -> pd.Series:
    quality = pd.Series(np.nan, index=values.index, dtype=float)
    groups = [(None, values)] if by_timeframe is None else values.groupby(by_timeframe, sort=False)
    for _, group in groups:
        numeric = pd.to_numeric(group, errors="coerce")
        numeric = numeric[numeric.notna()]
        if numeric.empty:
            continue
        if len(numeric) == 1:
            quality.loc[numeric.index] = 1.0
            continue
        ranks = numeric.rank(method="average", ascending=higher_is_better)
        quality.loc[numeric.index] = (ranks - 1) / (len(numeric) - 1)
    return quality


def _rank_robust(group: pd.DataFrame) -> tuple[pd.DataFrame, list[object]]:
    ranked = group.copy()
    quality_columns: list[str] = []
    for name, source, _, higher, within_timeframe in _RANK_COMPONENTS:
        if source not in ranked:
            ranked[source] = np.nan
        column = f"rank_quality_{name}"
        quality_columns.append(column)
        ranked[column] = _quality_percentiles(
            ranked[source], higher_is_better=higher,
            by_timeframe=ranked["timeframe"] if within_timeframe else None,
        )
    if "dd5_proxy" not in ranked:
        ranked["dd5_proxy"] = np.nan
    weights = np.array([float(weight) for _, _, weight, _, _ in _RANK_COMPONENTS])
    qualities = ranked.loc[:, quality_columns].to_numpy(dtype=float)
    present = ~np.isnan(qualities)
    coverage = present @ weights
    weighted = np.nan_to_num(qualities, nan=0.0) @ weights
    ranked["rank_weight_coverage_pct"] = coverage * 100
    for column_index, (name, _, _, _, _) in enumerate(_RANK_COMPONENTS):
        rank_weight = np.full(len(coverage), np.nan)
        np.divide(weights[column_index], coverage, out=rank_weight, where=coverage > 0)
        ranked[f"rank_weight_{name}"] = np.where(present[:, column_index], rank_weight, 0.0)
    final_score = np.full(len(coverage), np.nan)
    np.divide(weighted * 100, coverage, out=final_score, where=coverage > 0)
    ranked["final_score"] = final_score
    rankable = ranked["final_score"].notna()
    ordered = ranked.loc[rankable].sort_values(
        ["final_score", "dd5_proxy", "robust_pnl_30d_pct", "worst_drawdown_pct", "ab_stability_ratio",
         "worst_holding_p95_minutes", "first_shift_bp", "minimum_plateau_point_count", "close_ma_len", "strategy_id"],
        ascending=[False, False, False, True, False, True, False, False, True, True],
        na_position="last", kind="stable",
    )
    ranked.loc[ordered.index, "final_rank"] = range(1, len(ordered) + 1)
    return ranked, ordered.index.tolist()


def _equity_rank_strategy_id(group: pd.DataFrame, index: object) -> int:
    strategy_id = group.at[index, "strategy_id"]
    if isinstance(strategy_id, bool) or not isinstance(strategy_id, (int, np.integer)) or int(strategy_id) < 1:
        raise _error("EQUITY_RANK_INVALID_STRATEGY_ID")
    return int(strategy_id)


def _rank_equity_quality(group: pd.DataFrame) -> tuple[pd.DataFrame, list[object]]:
    if "_equity_quality" not in group:
        raise _error("EQUITY_CACHE_INCOMPLETE")
    seen_strategy_ids: set[int] = set()
    keys: dict[object, tuple[int, Decimal, Decimal, Decimal, int, int]] = {}
    for index, row in group.iterrows():
        strategy_id = _equity_rank_strategy_id(group, index)
        if strategy_id in seen_strategy_ids:
            raise _error("EQUITY_RANK_DUPLICATE_STRATEGY_ID")
        seen_strategy_ids.add(strategy_id)
        cached = row.get("_equity_quality")
        if not isinstance(cached, Mapping) or not isinstance(cached.get("facts"), EquityQualityFacts):
            raise _error("EQUITY_CACHE_INCOMPLETE")
        facts = cached["facts"]
        if facts.result_id != int(row.get("result_id", -1)):
            raise _error("EQUITY_CACHE_INCOMPLETE")
        if facts.score12 is None:
            if any(value is not None for value in (
                facts.equity_class, facts.drawdown, facts.peak_gap, facts.horizon_days,
            )):
                raise _error("EQUITY_CACHE_INCOMPLETE")
            continue
        if (
            facts.equity_class not in {0, 1, 2, 3}
            or facts.drawdown is None or facts.peak_gap is None
            or facts.horizon_days not in {7, 14, 28}
        ):
            raise _error("EQUITY_CACHE_INCOMPLETE")
        keys[index] = (
            facts.equity_class, -facts.score12, facts.drawdown, facts.peak_gap,
            -facts.horizon_days, strategy_id,
        )
    ranked = group.copy()
    ranked["final_score"] = pd.Series(
        {index: value["facts"].score12 for index, value in group["_equity_quality"].items()},
        dtype=object,
    ).reindex(group.index)
    ordered = sorted(keys, key=keys.__getitem__)
    ranked["final_rank"] = np.nan
    ranked.loc[ordered, "final_rank"] = range(1, len(ordered) + 1)
    return ranked, ordered


def _validate_equity_quality_evidence(cached: object, result_id: object) -> None:
    if (
        isinstance(result_id, bool) or not isinstance(result_id, (int, np.integer)) or int(result_id) < 1
        or not isinstance(cached, Mapping)
    ):
        raise _error("EQUITY_CACHE_INCOMPLETE")
    facts = cached.get("facts")
    source_revision = cached.get("source_revision")
    facts_sha256 = cached.get("facts_sha256")
    if (
        not isinstance(facts, EquityQualityFacts) or facts.result_id != int(result_id)
        or not isinstance(source_revision, str) or len(source_revision) != 64
        or not isinstance(facts_sha256, str) or len(facts_sha256) != 64
        or any(char not in "0123456789abcdef" for char in source_revision + facts_sha256)
    ):
        raise _error("EQUITY_CACHE_INCOMPLETE")
    try:
        encoded_facts = json.dumps(
            facts.to_canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
    except (AttributeError, TypeError, ValueError, UnicodeEncodeError):
        raise _error("EQUITY_CACHE_INCOMPLETE") from None
    if sha256(encoded_facts).hexdigest() != facts_sha256:
        raise _error("EQUITY_CACHE_INCOMPLETE")
    if facts.score12 is None:
        if any(value is not None for value in (
            facts.equity_class, facts.drawdown, facts.peak_gap, facts.horizon_days,
        )):
            raise _error("EQUITY_CACHE_INCOMPLETE")
    elif (
        type(facts.equity_class) is not int or facts.equity_class not in {0, 1, 2, 3}
        or not isinstance(facts.score12, Decimal) or not facts.score12.is_finite()
        or not isinstance(facts.drawdown, Decimal) or not facts.drawdown.is_finite()
        or not isinstance(facts.peak_gap, Decimal) or not facts.peak_gap.is_finite()
        or facts.horizon_days not in {7, 14, 28}
    ):
        raise _error("EQUITY_CACHE_INCOMPLETE")


def _equity_workbook_values(cached: object) -> dict[str, object]:
    """Map one optional facts-cache entry to the four compact workbook fields."""
    if not isinstance(cached, Mapping):
        return {"equity_state": None, "equity_basis": "INVALID", "equity_dd_pct": None, "equity_smoothness": None}
    if cached.get("status") != "FRESH":
        return {
            "equity_state": None, "equity_basis": str(cached.get("status", "INVALID")),
            "equity_dd_pct": None, "equity_smoothness": None,
        }
    facts = cached.get("facts")
    if not isinstance(facts, EquityQualityFacts):
        return {"equity_state": None, "equity_basis": "INVALID", "equity_dd_pct": None, "equity_smoothness": None}
    horizon = facts.horizon_days
    horizon_label = {28: "OK", 14: "PARTIAL", 7: "PROVISIONAL"}.get(horizon, "UNKNOWN")
    basis = facts.reason if horizon is None else f"{horizon}d / {horizon_label}"
    drawdown = facts.drawdown
    window = next((item for item in facts.windows if item.days == horizon), None) if horizon is not None else None
    if horizon is not None and window is None:
        return {"equity_state": None, "equity_basis": "INVALID", "equity_dd_pct": None, "equity_smoothness": None}
    return {
        "equity_state": facts.state,
        "equity_basis": basis,
        "equity_dd_pct": drawdown * 100 if isinstance(drawdown, Decimal) else None,
        "equity_smoothness": None if window is None else window.er,
    }


_FIXED_PREFIX = (
    "filter_equity_regime", "filter_lot_variant_redundancy", "filter_hard_cutoffs",
    "ab_deterioration", "filter_best_trade_dependency",
    "pair_side_pnl_upper_half", "structural_stage_1", "structural_stage_2", "pair_side_stage_3",
)


def effective_selection_stages(
    request: SelectionRequest, config: SelectionConfig = SelectionConfig(),
) -> tuple[SelectionStage, ...]:
    explicit_ids = {stage.id for stage in request.stages}
    stages = list(request.stages)
    if _LOT_VARIANT_STAGE_ID not in explicit_ids and config.lot_variant_redundancy_enabled:
        stages.append(SelectionStage(_LOT_VARIANT_STAGE_ID, True, "pair_side_timeframe"))
    by_id = {stage.id: stage for stage in stages}
    prefix = [by_id[stage_id] for stage_id in _FIXED_PREFIX if stage_id in by_id]
    remainder = [stage for stage in stages if stage.id not in _FIXED_PREFIX]
    return tuple([*prefix, *remainder])


def _scope_groups(frame: pd.DataFrame, scope: StageScope):
    keys = [name for name in ("symbol", "side") if name in frame]
    if scope == "pair_side_timeframe" and "timeframe" in frame:
        keys.append("timeframe")
    return frame.groupby(keys, dropna=False, sort=False) if keys else [(None, frame)]


def _upper_half_reasons(group: pd.DataFrame, config: SelectionConfig) -> dict[object, str]:
    """Return independent full/DD5 and B gate evidence by input index."""

    # The Panel already passes only survivors; spreadsheet prefilter markers
    # are not part of its input contract.
    failures: dict[object, list[str]] = {}
    for column, ratio in (
        ("dd5_proxy", config.researched_pnl_dd5_ratio),
        ("ab_return_b_30d_pct", config.researched_pnl_b_ratio),
    ):
        if column not in group:
            continue
        values = [value for value in (_research_decimal(item) for item in group[column]) if value is not None]
        if not values:
            continue
        values.sort(reverse=True)
        top = values[: (len(values) + 1) // 2]
        midpoint = len(top) // 2
        reference = top[midpoint] if len(top) % 2 else (top[midpoint - 1] + top[midpoint]) / Decimal(2)
        if reference <= 0:
            continue
        threshold = ratio * reference
        for index, value in group[column].items():
            parsed = _research_decimal(value)
            if parsed is not None and parsed < threshold:
                failures.setdefault(index, []).append("DD5" if column == "dd5_proxy" else "B")
    return {index: "+".join(names) for index, names in failures.items()}


def _upper_half_eliminated(group: pd.DataFrame, config: SelectionConfig) -> list[object]:
    return list(_upper_half_reasons(group, config))


_RESEARCH_B = "ab_return_b_30d_pct"
_RESEARCH_DD5 = "dd5_proxy"
_RESEARCH_DD = "max_drawdown_pct"
_RESEARCH_HOLD_P95 = "holding_p95_minutes"
_RESEARCH_HOLD_M = "holding_median_minutes"


@dataclass(frozen=True, slots=True)
class _ResearchDecision:
    final: str
    reason: str
    replacement_id: int | None = None


@dataclass(frozen=True, slots=True)
class _ResearchRow:
    id: int
    group: tuple[str, str, str, int]
    shift: Decimal | None
    complete: bool
    b: Decimal | None
    dd5: Decimal | None
    dd: Decimal | None
    points: tuple[Decimal, ...]
    ma: tuple[Decimal, ...]
    close: Decimal | None
    p95: Decimal | None
    median: Decimal | None
    source_yes: bool = True


def _research_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, (bool, str, date, datetime, time)):
        return None
    if not isinstance(value, (int, float, Decimal, np.integer, np.floating)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _research_shift(value: object) -> Decimal | None:
    return _research_decimal(value)


def _research_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.replace("\u00a0", " ").strip()
    return value.casefold() if value else None


def _research_sid(value: object) -> int:
    return int(value) if not isinstance(value, (bool, np.bool_)) and isinstance(value, (int, np.integer)) and int(value) > 0 else 0


def _research_vector(row: pd.Series, prefix: str, count: int) -> tuple[Decimal, ...] | None:
    values = []
    for order in range(1, count + 1):
        value = _research_decimal(row.get(f"order_{order}_{prefix}"))
        if value is None or value < 0:
            return None
        values.append(value)
    return tuple(values)


def _research_row(row: pd.Series, *, allow_ord4: bool = False, stage3_mode: bool = False) -> _ResearchRow:
    strategy_id = row.get("strategy_id")
    order_count = row.get("order_count")
    # Existing selection input is normally validated upstream.  Structural
    # stages fail open for malformed rows so one bad research field cannot
    # remove a finalist.  Valid IDs remain the audit/replacement key.
    valid_sid = _research_sid(strategy_id) > 0
    valid_ord = not isinstance(order_count, (bool, np.bool_)) and isinstance(order_count, (int, np.integer))
    sid = int(strategy_id) if valid_sid else 0
    count = int(order_count) if valid_ord else 0
    text = tuple(_research_text(row.get(column)) or "" for column in ("symbol", "side", "timeframe"))
    complete = 1 <= count <= (4 if allow_ord4 else 3)
    points = _research_vector(row, "plateau_point_count", count) if complete else ()
    ma = _research_vector(row, "open_ma_len", count) if complete else ()
    b, dd5, dd, close = map(_research_decimal, (
        row.get(_RESEARCH_B), row.get(_RESEARCH_DD5), row.get(_RESEARCH_DD), row.get("close_ma_len"),
    ))
    # DD and hold durations are non-negative measures.  Negative values are
    # invalid core evidence and therefore retain the row without replacing.
    if dd is not None and dd < 0:
        dd = None
    p95 = _research_decimal(row.get(_RESEARCH_HOLD_P95))
    median = _research_decimal(row.get(_RESEARCH_HOLD_M))
    if p95 is not None and p95 < 0:
        p95 = None
    if median is not None and median < 0:
        median = None
    if stage3_mode:
        complete = complete and points is not None and all(value is not None for value in (b, dd5, dd, p95, median))
        if not complete:
            points, ma = (), ()
        elif ma is None:
            ma = ()
    elif complete and (points is None or ma is None or any(value is None for value in (b, dd5, dd, close))):
        complete = False
    return _ResearchRow(
        sid,
        (*text, count),
        _research_shift(row.get("first_shift_bp")), complete, b, dd5, dd, points or (), ma or (), close,
        p95, median, True,
    )


def _research_threshold(old: Decimal, new: Decimal, absolute: Decimal, relative: Decimal) -> Decimal:
    return max(absolute, relative * max(abs(old), abs(new)))


def _research_mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise _error("RESEARCHED_EMPTY_VECTOR")
    return sum(values, Decimal(0)) / Decimal(len(values))


def _research_points_advantage(old: tuple[Decimal, ...], new: tuple[Decimal, ...], ratio: Decimal) -> bool:
    # Compare means by cross-multiplication: division rounding can turn an
    # exact 40% boundary into a false strict advantage (workbook ID 18880).
    old_scaled = sum(old, Decimal(0)) * len(new)
    new_scaled = sum(new, Decimal(0)) * len(old)
    return old_scaled - new_scaled > ratio * max(old_scaled, new_scaled)


def _research_mean_delta_exceeds(higher: tuple[Decimal, ...], lower: tuple[Decimal, ...], delta: Decimal) -> bool:
    return (sum(higher, Decimal(0)) * len(lower) - sum(lower, Decimal(0)) * len(higher)
            > delta * len(higher) * len(lower))


def _research_material_advantages(lower: _ResearchRow, candidate: _ResearchRow, config: SelectionConfig, *, cross: bool = False) -> tuple[str, ...]:
    values = (("B", lower.b, candidate.b, config.researched_b_abs, config.researched_b_rel, True),
              ("DD5", lower.dd5, candidate.dd5, config.researched_dd5_abs, config.researched_dd5_rel, True),
              ("DD", lower.dd, candidate.dd, config.researched_dd_abs, config.researched_dd_rel, False))
    found: list[str] = []
    for name, old, new, absolute, relative, higher in values:
        if old is None or new is None:
            continue
        advantage = old - new if higher else new - old
        if advantage > _research_threshold(old, new, absolute, relative):
            found.append(name)
    if lower.points and candidate.points:
        floor = config.researched_points_cross_floor_ratio if cross else config.researched_points_same_floor_ratio
        points = _research_points_advantage(lower.points, candidate.points, config.researched_points_mean_ratio)
        points &= min(lower.points) >= floor * min(candidate.points)
        if not cross:
            points &= len(lower.points) == len(candidate.points) and all(
                old_value >= config.researched_points_same_floor_ratio * new_value
                for old_value, new_value in zip(lower.points, candidate.points)
            )
        elif lower.group[3] == 1 and candidate.group[3] > 1:
            points &= lower.points[0] >= (Decimal(1) + config.researched_points_single_best_ratio) * max(candidate.points)
        if points:
            found.append("Points")
    if lower.ma and candidate.ma and _research_mean_delta_exceeds(candidate.ma, lower.ma, config.researched_open_ma_delta):
        found.append("MA")
    if lower.close is not None and candidate.close is not None and candidate.close - lower.close >= config.researched_close_ma_delta:
        found.append("Close")
    return tuple(found)


def _research_cross_metric_wins(
    loser: _ResearchRow, candidate: _ResearchRow, config: SelectionConfig,
) -> tuple[str, ...]:
    """Core metrics on which the candidate beats the current row in stage 8."""
    if not loser.complete or not candidate.complete:
        return ()
    found: list[str] = []
    for name, old, new, absolute, relative, higher in (
        ("B", loser.b, candidate.b, config.researched_b_abs, config.researched_b_rel, True),
        ("DD5", loser.dd5, candidate.dd5, config.researched_dd5_abs, config.researched_dd5_rel, True),
        ("DD", loser.dd, candidate.dd, config.researched_dd_abs, config.researched_dd_rel, False),
    ):
        if old is None or new is None:
            continue
        advantage = new - old if higher else old - new
        if advantage > _research_threshold(old, new, absolute, relative):
            found.append(name)
    if loser.points and candidate.points:
        if (
            _research_points_advantage(candidate.points, loser.points, config.researched_points_mean_ratio)
            and min(candidate.points) >= config.researched_points_cross_floor_ratio * min(loser.points)
        ):
            found.append("Points")
    return tuple(found)


def _research_cross_protection(
    loser: _ResearchRow, candidate: _ResearchRow, config: SelectionConfig,
) -> tuple[str, ...]:
    """Current-row protection against one eligible cross-ORD replacement."""
    reasons = list(_research_cross_metric_wins(candidate, loser, config))
    if (
        loser.group[3] == 1 and candidate.group[3] > 1 and "Points" in reasons
        and loser.points[0] < (Decimal(1) + config.researched_points_single_best_ratio) * max(candidate.points)
    ):
        reasons.remove("Points")
    if candidate.ma and loser.ma and _research_mean_delta_exceeds(candidate.ma, loser.ma, config.researched_open_ma_delta):
        reasons.append("MA")
    if candidate.close is not None and loser.close is not None and candidate.close - loser.close >= config.researched_close_ma_delta:
        reasons.append("Close")
    if reasons:
        return tuple(reasons)
    p95 = _research_hold_status(candidate.p95, loser.p95, config.researched_hold_p95_ratio)
    median = _research_hold_status(candidate.median, loser.median, config.researched_hold_median_ratio)
    p95_worse = (
        candidate.p95 is not None and loser.p95 is not None
        and loser.p95 - candidate.p95 > config.researched_hold_p95_veto_ratio * candidate.p95
    )
    hold_reasons: list[str] = []
    if p95 == "ADVANTAGE":
        hold_reasons.append("Hold-95")
    elif p95 == "INVALID":
        hold_reasons.append("HOLD_P95_INVALID")
    if median == "ADVANTAGE" and not p95_worse:
        hold_reasons.append("Hold-M")
    elif median == "INVALID" and not p95_worse:
        hold_reasons.append("HOLD_M_INVALID")
    return tuple(hold_reasons)


def _research_hold_status(candidate: Decimal | None, lower: Decimal | None, ratio: Decimal) -> str:
    if candidate is None or lower is None:
        return "INVALID"
    return "ADVANTAGE" if candidate - lower > ratio * candidate else "PASS"


def _research_compare(lower: _ResearchRow, candidate: _ResearchRow, config: SelectionConfig) -> _ResearchDecision:
    protected = _research_material_advantages(lower, candidate, config)
    if protected:
        return _ResearchDecision("KEEP", ", ".join(protected))
    p95 = _research_hold_status(candidate.p95, lower.p95, config.researched_hold_p95_ratio)
    median = _research_hold_status(candidate.median, lower.median, config.researched_hold_median_ratio)
    p95_worse = (
        candidate.p95 is not None and lower.p95 is not None
        and lower.p95 - candidate.p95 > config.researched_hold_p95_veto_ratio * candidate.p95
    )
    if p95 == "PASS" and (median == "PASS" or p95_worse):
        return _ResearchDecision("DROP", "HOLD_P95_WORSE_VETO" if p95_worse and median != "PASS" else "STRUCTURAL_REDUNDANCY", candidate.id)
    reason = "HOLD_P95_INVALID" if p95 == "INVALID" else "HOLD_M_INVALID" if median == "INVALID" else "HOLD_P95_ADVANTAGE" if p95 == "ADVANTAGE" else "HOLD_M_ADVANTAGE"
    return _ResearchDecision("RESCUE", reason, candidate.id)


def _research_stage1(rows: list[_ResearchRow], config: SelectionConfig) -> dict[int, _ResearchDecision]:
    decisions: dict[int, _ResearchDecision] = {}
    for row in rows:
        if not row.source_yes:
            decisions[row.id] = _ResearchDecision("KEEP", "SOURCE_PREFILTER")
        elif row.shift is None:
            decisions[row.id] = _ResearchDecision("KEEP", "INVALID_SHIFT")
        elif not 1 <= row.group[3] <= 3:
            decisions[row.id] = _ResearchDecision("KEEP", "OUT_OF_SCOPE_ORD")
        elif not row.complete:
            decisions[row.id] = _ResearchDecision("KEEP", "CORE_INVALID")
    groups: dict[tuple[str, str, str, int], list[_ResearchRow]] = {}
    for row in rows:
        if row.source_yes and row.complete and row.shift is not None and 1 <= row.group[3] <= 3:
            groups.setdefault(row.group, []).append(row)
    for group in groups.values():
        by_id = {row.id: row for row in group}
        edges: dict[int, set[int]] = {row.id: set() for row in group}
        raw: dict[tuple[int, int], _ResearchDecision] = {}
        for lower in group:
            for candidate in group:
                if candidate.id == lower.id or candidate.shift < lower.shift:
                    continue
                trial = _research_compare(lower, candidate, config)
                if trial.final == "DROP":
                    raw[(lower.id, candidate.id)] = trial
        coverage = {row.id: 0 for row in group}
        for _, target in raw:
            coverage[target] += 1
        def quality(row: _ResearchRow) -> tuple[object, ...]:
            dd = row.dd if row.dd is not None else Decimal("Infinity")
            close = row.close if row.close is not None else Decimal("Infinity")
            p95 = row.p95 if row.p95 is not None else Decimal("Infinity")
            median = row.median if row.median is not None else Decimal("Infinity")
            dd5 = row.dd5 if row.dd5 is not None else Decimal("-Infinity")
            b = row.b if row.b is not None else Decimal("-Infinity")
            return (row.shift, -dd, _research_mean(row.points), -_research_mean(row.ma), -close, -p95, -median, dd5, b, -row.id)
        evidence: dict[tuple[int, int], _ResearchDecision] = {}
        for (loser, target), trial in raw.items():
            if (target, loser) in raw and (quality(by_id[target])[0], coverage[target], *quality(by_id[target])[1:]) <= (quality(by_id[loser])[0], coverage[loser], *quality(by_id[loser])[1:]):
                continue
            edges[loser].add(target); evidence[(loser, target)] = trial
        selected: set[int] = set(); remaining = set(edges)
        while remaining:
            dropped = {sid for sid in remaining if edges[sid] & selected}
            for sid in dropped:
                target = max(edges[sid] & selected, key=lambda item: quality(by_id[item]))
                decisions[sid] = evidence[(sid, target)]
            remaining -= dropped
            if not remaining:
                break
            sinks = {sid for sid in remaining if not edges[sid] & remaining}
            if not sinks:
                sinks = {max(remaining, key=lambda sid: quality(by_id[sid]))}
            selected.update(sinks); remaining -= sinks
        for row in group:
            if row.id not in selected:
                continue
            candidates = sorted((candidate for candidate in group if candidate.id in selected and candidate.id != row.id and candidate.shift >= row.shift), key=quality, reverse=True)
            rescue = next((trial for candidate in candidates if (trial := _research_compare(row, candidate, config)).final == "RESCUE"), None)
            if rescue is not None:
                decisions[row.id] = rescue
                continue
            advantages = {
                name for candidate in candidates
                for name in _research_material_advantages(row, candidate, config)
            }
            ordered = ", ".join(name for name in ("B", "DD5", "DD", "Points", "MA", "Close") if name in advantages)
            lost_target = bool(edges[row.id]) and not bool(edges[row.id] & selected)
            decisions[row.id] = _ResearchDecision(
                "KEEP", "REPLACEMENT_DROPPED" if lost_target else (ordered or "NO_SUITABLE_HIGHER_SHIFT")
            )
    return decisions


def _research_stage2(rows: list[_ResearchRow], config: SelectionConfig) -> dict[int, _ResearchDecision]:
    decisions = {
        row.id: _ResearchDecision("KEEP", "SOURCE_PREFILTER" if not row.source_yes else ("4ORD" if row.group[3] == 4 else "NO_REPLACEMENT"))
        for row in rows
    }
    members = [row for row in rows if row.id > 0 and row.source_yes and row.complete and row.shift is not None and row.group[3] in {1, 2, 3}]
    groups: dict[tuple[str, str, str], list[_ResearchRow]] = {}
    for row in members:
        groups.setdefault(row.group[:3], []).append(row)
    for group in groups.values():
        edges: dict[int, set[int]] = {row.id: set() for row in group}; evidence: dict[tuple[int, int], _ResearchDecision] = {}; by_id = {row.id: row for row in group}
        for lower in group:
            for candidate in group:
                if candidate.id == lower.id or candidate.group[3] < lower.group[3] or candidate.shift < lower.shift or (candidate.group[3] == lower.group[3] and candidate.shift == lower.shift):
                    continue
                if _research_cross_protection(lower, candidate, config):
                    continue
                p95 = _research_hold_status(candidate.p95, lower.p95, config.researched_hold_p95_ratio); median = _research_hold_status(candidate.median, lower.median, config.researched_hold_median_ratio)
                p95_worse = candidate.p95 is not None and lower.p95 is not None and lower.p95 - candidate.p95 > config.researched_hold_p95_veto_ratio * candidate.p95
                if p95 == "PASS" and (median == "PASS" or p95_worse):
                    wins = list(_research_cross_metric_wins(lower, candidate, config))
                    if _research_mean_delta_exceeds(lower.ma, candidate.ma, config.researched_open_ma_delta):
                        wins.append("MA")
                    if lower.close is not None and candidate.close is not None and lower.close - candidate.close >= config.researched_close_ma_delta:
                        wins.append("Close")
                    edges[lower.id].add(candidate.id)
                    evidence[(lower.id, candidate.id)] = _ResearchDecision("DROP", ", ".join(wins) or "ORD/Shift", candidate.id)
        remaining = set(edges); retained: set[int] = set()
        while remaining:
            sinks = {sid for sid in remaining if not edges[sid] & remaining}
            if not sinks:
                retained.update(remaining); break
            retained.update(sinks); remaining -= sinks
            dropped = {sid for sid in remaining if edges[sid] & retained}
            for sid in dropped:
                target = max(edges[sid] & retained, key=lambda item: (by_id[item].group[3], by_id[item].shift, -item))
                decisions[sid] = evidence[(sid, target)]
            remaining -= dropped
        reason_order = ("B", "DD5", "DD", "Points", "MA", "Close", "Hold-95", "Hold-M", "HOLD_P95_INVALID", "HOLD_M_INVALID")
        for row in group:
            if row.id in retained:
                reasons = {
                    reason for candidate in group if candidate.id in retained and candidate.id != row.id
                    and candidate.group[3] >= row.group[3] and candidate.shift is not None and row.shift is not None
                    and candidate.shift >= row.shift
                    and (candidate.group[3] > row.group[3] or candidate.shift > row.shift)
                    for reason in _research_cross_protection(row, candidate, config)
                }
                decisions[row.id] = _ResearchDecision(
                    "KEEP", ", ".join(reason for reason in reason_order if reason in reasons) or "NO_REPLACEMENT"
                )
    return decisions


def _research_stage3(rows: list[_ResearchRow], config: SelectionConfig) -> dict[int, _ResearchDecision]:
    decisions = {
        row.id: _ResearchDecision(
            "KEEP", "SOURCE_PREFILTER" if not row.source_yes else ("NO_REPLACEMENT" if row.complete else "CORE_INVALID")
        ) for row in rows
    }
    groups: dict[tuple[str, str], list[_ResearchRow]] = {}
    for row in rows:
        if row.source_yes:
            groups.setdefault(row.group[:2], []).append(row)
    for group in groups.values():
        retained: list[_ResearchRow] = []
        def stage3_key(row: _ResearchRow) -> tuple[object, ...]:
            b = row.b if row.b is not None else Decimal("-Infinity")
            dd5 = row.dd5 if row.dd5 is not None else Decimal("-Infinity")
            dd = row.dd if row.dd is not None else Decimal("Infinity")
            return (-b, -dd5, dd, row.id)
        for lower in sorted(group, key=stage3_key):
            if not lower.complete or lower.b is None or lower.dd5 is None or lower.dd is None:
                continue
            trials = []
            protections_seen: list[str] = []
            for candidate in retained:
                if candidate.b is None or candidate.dd5 is None or candidate.dd is None or candidate.b < lower.b or candidate.dd5 < lower.dd5 or candidate.dd > lower.dd:
                    continue
                wins = [name for name, old, new, absolute, relative, higher in (("B", lower.b, candidate.b, config.researched_b_abs, config.researched_b_rel, True), ("DD5", lower.dd5, candidate.dd5, config.researched_dd5_abs, config.researched_dd5_rel, True), ("DD", lower.dd, candidate.dd, config.researched_dd_abs, config.researched_dd_rel, False)) if (new - old if higher else old - new) > _research_threshold(old, new, absolute, relative)]
                if len(wins) < 2:
                    continue
                points = _research_points_advantage(lower.points, candidate.points, config.researched_points_mean_ratio) and min(lower.points) >= config.researched_points_cross_floor_ratio * min(candidate.points)
                if lower.group[3] == 1 and candidate.group[3] > 1:
                    points &= lower.points[0] >= (Decimal(1) + config.researched_points_single_best_ratio) * max(candidate.points)
                protections = ["Points"] if points else []
                if candidate.p95 is not None and lower.p95 is not None and candidate.p95 - lower.p95 > config.researched_hold_p95_ratio * candidate.p95:
                    protections.append("Hold-95")
                p95_worse = candidate.p95 is not None and lower.p95 is not None and lower.p95 - candidate.p95 > config.researched_hold_p95_veto_ratio * candidate.p95
                if not p95_worse and candidate.median is not None and lower.median is not None and candidate.median - lower.median > config.researched_hold_median_ratio * candidate.median:
                    protections.append("Hold-M")
                if protections:
                    protections_seen.extend(protections)
                    continue
                trials.append((candidate, wins))
            if trials:
                candidate, wins = max(trials, key=lambda pair: (len(pair[1]), -pair[0].dd, pair[0].dd5, pair[0].b, pair[0].group[3], -pair[0].id))
                decisions[lower.id] = _ResearchDecision("DROP", ", ".join(wins), candidate.id)
            else:
                reason_order = ("Points", "Hold-95", "Hold-M")
                decisions[lower.id] = _ResearchDecision(
                    "KEEP", ", ".join(reason for reason in reason_order if reason in set(protections_seen)) or "NO_REPLACEMENT"
                )
                retained.append(lower)
    return decisions


def _research_decisions(frame: pd.DataFrame, stage_id: str, config: SelectionConfig) -> dict[int, _ResearchDecision]:
    ids = [_research_sid(value) for value in frame["strategy_id"]]
    if any(sid == 0 for sid in ids) or len(set(ids)) != len(ids):
        raise _error("RESEARCHED_INVALID_STRATEGY_ID")
    allow_ord4 = stage_id == "pair_side_stage_3"
    rows = [
        _research_row(row, allow_ord4=allow_ord4, stage3_mode=allow_ord4)
        for _, row in frame.iterrows()
    ]
    if stage_id == "structural_stage_1":
        return _research_stage1(rows, config)
    if stage_id == "structural_stage_2":
        return _research_stage2(rows, config)
    return _research_stage3(rows, config)


def _hard_cutoff_evidence(row: pd.Series, config: SelectionConfig) -> tuple[bool, str | None]:
    dd = _decimal_or_none(row.get("max_drawdown_pct"))
    pnl = _decimal_or_none(row.get("pnl_30d_pct"))
    history = _decimal_or_none(row.get("history_days"))
    cycles = _integer(row.get("completed_cycle_count"))
    if cycles is None:
        cycles = _integer(row.get("reliable_completed_cycle_count"))
    if _is_bool(row.get("completed_cycles_reliable"), False):
        cycles = None
    b_pnl = _decimal_or_none(row.get("ab_return_b_30d_pct"))
    triggered: list[str] = []
    if dd is not None and pnl is not None and dd > config.hard_dd_pct and pnl < config.hard_dd_profit_multiplier * dd:
        triggered.append("DD_PROFIT_GUARD")
    if pnl is not None and pnl <= config.hard_pnl30_floor_pct:
        triggered.append("PNL30_FLOOR")
    if (
        history is not None and history >= config.hard_min_history_days
        and cycles is not None and cycles >= config.hard_min_cycles
        and dd is not None and dd > 0 and pnl is not None and b_pnl is not None
        and pnl / dd < config.hard_ratio and b_pnl / dd < config.hard_ratio
    ):
        triggered.append("DUAL_RATIO")
    if not triggered:
        return False, None
    known_values = {
        "full_dd_pct": dd,
        "full_pnl30_pct": pnl,
        "b_pnl30_pct": b_pnl,
        "history_days": history,
        "reliable_completed_cycles": cycles,
        "full_ratio": pnl / dd if dd is not None and dd > 0 and pnl is not None else None,
        "b_ratio": b_pnl / dd if dd is not None and dd > 0 and b_pnl is not None else None,
        "dd_threshold_pct": config.hard_dd_pct,
        "dd_profit_multiplier": config.hard_dd_profit_multiplier,
        "pnl30_floor_pct": config.hard_pnl30_floor_pct,
        "ratio_threshold": config.hard_ratio,
        "min_history_days": config.hard_min_history_days,
        "min_reliable_cycles": config.hard_min_cycles,
    }
    payload = {
        "applicability": {
            "dd_profit_guard": dd is not None and pnl is not None,
            "pnl30_floor": pnl is not None,
            "dual_ratio": {
                "history_gate": history is not None and history >= config.hard_min_history_days,
                "cycle_gate": cycles is not None and cycles >= config.hard_min_cycles,
                "positive_full_dd": dd is not None and dd > 0,
                "full_pnl30_known": pnl is not None,
                "b_pnl30_known": b_pnl is not None,
            },
        },
        "missing": sorted(
            name for name in ("full_dd_pct", "full_pnl30_pct", "b_pnl30_pct", "history_days", "reliable_completed_cycles")
            if known_values[name] is None
        ),
        "triggered": triggered,
        "values": {
            name: str(value) if isinstance(value, Decimal) else value
            for name, value in known_values.items()
        },
    }
    return True, "FILTER_HARD_CUTOFFS:" + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _ab_evidence(row: pd.Series, config: SelectionConfig) -> tuple[bool | None, str | None]:
    a = _decimal_or_none(row.get("ab_return_a_30d_pct"))
    b = _decimal_or_none(row.get("ab_return_b_30d_pct"))
    win_b = _decimal_or_none(row.get("ab_win_rate_b_pct"))
    b_cycles = _integer(row.get("ab_completed_cycle_count"))
    triggered: list[str] = []
    if b is not None and b <= config.ab_return_floor_pct:
        triggered.append(f"B_PNL30_FLOOR(b={b})")
    if (
        a is not None and b is not None and a > 0
        and b <= a / config.ab_return_divisor and b <= config.ab_decline_cap_pct
    ):
        triggered.append(f"A_B_DECLINE(a={a},b={b})")
    if b_cycles is not None and b_cycles >= config.ab_completed_cycles and win_b is not None and win_b < config.ab_win_rate_floor_pct:
        triggered.append(f"B_WIN_RATE(cycles={b_cycles},win={win_b})")
    if triggered:
        return True, "AB_DETERIORATION;" + ";".join(triggered)
    incomplete = b is None
    if b is not None and b <= config.ab_decline_cap_pct and a is None:
        incomplete = True
    if b_cycles is None or (b_cycles >= config.ab_completed_cycles and win_b is None):
        incomplete = True
    return (False, "AB_NOT_EVALUATED_INSUFFICIENT_DATA") if incomplete else (False, None)


_LOT_VARIANT_STAGE_ID = "filter_lot_variant_redundancy"
_LOT_VARIANT_METRICS = ("dd5_proxy", "capital_proxy", "robust_pnl_30d_pct", "worst_drawdown_pct", "profit_factor")


def _utc_datetime(value: object) -> datetime | None:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _comparison_interval(row: pd.Series) -> tuple[datetime, datetime] | None:
    for start_name, end_name in (
        ("comparison_interval_start_utc", "comparison_interval_end_utc"),
        ("effective_start_utc", "effective_end_utc"),
        ("reported_start_utc", "reported_end_utc"),
        ("report_start_utc", "report_end_utc"),
    ):
        if start_name not in row or end_name not in row:
            continue
        raw_start, raw_end = row[start_name], row[end_name]
        if not _present(raw_start) and not _present(raw_end):
            continue
        start, end = _utc_datetime(raw_start), _utc_datetime(raw_end)
        if start is not None and end is not None and end >= start:
            return start, end
        return None
    return None


def _integer(value: object) -> int | None:
    parsed = _decimal_or_none(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _is_bool(value: object, expected: bool) -> bool:
    return isinstance(value, (bool, np.bool_)) and bool(value) is expected


def _lot_variant_structure(row: pd.Series) -> tuple[tuple[object, ...], tuple[Decimal, ...]] | None:
    close_ma = _integer(row.get("close_ma_len"))
    order_count = _integer(row.get("order_count"))
    if close_ma is None or order_count is None or order_count < 1:
        return None
    fields: list[tuple[int, int, Decimal, Decimal]] = []
    for order in range(1, order_count + 1):
        open_ma = _integer(row.get(f"order_{order}_open_ma_len"))
        shift = _integer(row.get(f"order_{order}_shift_bp"))
        open_multiplier = _decimal_or_none(row.get(f"order_{order}_open_multiplier"))
        lot = _decimal_or_none(row.get(f"order_{order}_lot_x"))
        if (
            open_ma is None or shift is None or open_multiplier is None or lot is None
            or not open_multiplier.is_finite() or not lot.is_finite()
        ):
            return None
        fields.append((open_ma, shift, open_multiplier, lot))
    fields.sort(key=lambda value: (value[0], value[1], value[2], value[3]))
    symbol, side, timeframe = (row.get(name) for name in ("symbol", "side", "timeframe"))
    if any(value is None or pd.isna(value) for value in (symbol, side, timeframe)):
        return None
    pairs = tuple((open_ma, shift, open_multiplier) for open_ma, shift, open_multiplier, _ in fields)
    lots = tuple(lot for _, _, _, lot in fields)
    return (str(symbol), str(side), str(timeframe), close_ma, order_count, pairs), lots


def _lot_variant_group_key(structure: tuple[object, ...]) -> str:
    symbol, side, timeframe, close_ma, order_count, pairs = structure
    return json.dumps(
        [symbol, side, timeframe, close_ma, order_count, [[open_ma, shift, str(multiplier)] for open_ma, shift, multiplier in pairs]],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _lot_variant_eliminated(
    result: pd.DataFrame, group: pd.DataFrame, config: SelectionConfig, *, protect_equity: bool = False,
) -> list[object]:
    candidates: dict[tuple[object, ...], list[tuple[object, tuple[Decimal, ...]]]] = {}
    for index, row in group.iterrows():
        structure = _lot_variant_structure(row)
        interval = _comparison_interval(row)
        if structure is None or interval is None:
            continue
        group_id = (*structure[0], interval[0], interval[1])
        candidates.setdefault(group_id, []).append((index, structure[1]))

    eliminated: list[object] = []
    tolerance = config.lot_tolerance
    for group_id, members in candidates.items():
        if len(members) != 2:
            continue
        rows = result.loc[[index for index, _ in members]]
        equal: list[object] = []
        income: list[object] = []
        for index, lots in members:
            spread = max(lots) - min(lots)
            if spread <= tolerance:
                equal.append(index)
            elif spread > tolerance:
                income.append(index)
        if len(equal) != 1 or len(income) != 1:
            continue
        strategy_ids: dict[object, int] = {}
        for index in rows.index:
            raw_id = rows.at[index, "strategy_id"]
            if isinstance(raw_id, bool) or not isinstance(raw_id, (int, np.integer)) or int(raw_id) < 1:
                break
            strategy_ids[index] = int(raw_id)
        else:
            equal_index, income_index = equal[0], income[0]
            equal_row, income_row = result.loc[equal_index], result.loc[income_index]
            equal_lots = members[next(i for i, (index, _) in enumerate(members) if index == equal_index)][1]
            income_lots = members[next(i for i, (index, _) in enumerate(members) if index == income_index)][1]
            initial_equal = _decimal_or_none(equal_row.get("initial_balance"))
            initial_income = _decimal_or_none(income_row.get("initial_balance"))
            equal_dd = _decimal_or_none(equal_row.get("max_drawdown_pct"))
            income_dd = _decimal_or_none(income_row.get("max_drawdown_pct"))
            equal_full = _decimal_or_none(equal_row.get("pnl_30d_pct"))
            income_full = _decimal_or_none(income_row.get("pnl_30d_pct"))
            equal_b = _decimal_or_none(equal_row.get("ab_return_b_30d_pct"))
            income_b = _decimal_or_none(income_row.get("ab_return_b_30d_pct"))
            equal_total, income_total = sum(equal_lots, Decimal(0)), sum(income_lots, Decimal(0))
            complete = all(value is not None and value.is_finite() for value in (
                initial_equal, initial_income, equal_dd, income_dd, equal_full, income_full, equal_b, income_b,
            )) and initial_equal > 0 and initial_income > 0 and abs(initial_equal - initial_income) <= tolerance
            complete = complete and equal_dd > 0 and income_dd > 0 and equal_full > 0 and equal_b > 0
            complete = complete and equal_total > 0 and income_total > 0 and abs(equal_total - income_total) <= tolerance
            if not complete:
                continue
            equal_full_dd5 = equal_full * 5 / equal_dd
            income_full_dd5 = income_full * 5 / income_dd
            equal_b_dd5 = equal_b * 5 / equal_dd
            income_b_dd5 = income_b * 5 / income_dd
            income_wins = (
                income_full_dd5 >= config.lot_full_dd5_multiplier * equal_full_dd5
                and income_dd <= config.lot_full_dd_multiplier * equal_dd
                and income_b_dd5 >= equal_b_dd5
                and income_b >= equal_b
            )
            winner, loser = (income_index, equal_index) if income_wins else (equal_index, income_index)
            failed = []
            if not income_wins:
                if income_full_dd5 < config.lot_full_dd5_multiplier * equal_full_dd5:
                    failed.append("FULL_DD5")
                if income_dd > config.lot_full_dd_multiplier * equal_dd:
                    failed.append("FULL_DD")
                if income_b_dd5 < equal_b_dd5:
                    failed.append("B_DD5")
                if income_b < equal_b:
                    failed.append("B_PNL30")
            structure = group_id[:-2]
            key = _lot_variant_group_key(structure)
            result.loc[rows.index, "lot_variant_group_key"] = key
            result.loc[rows.index, "lot_variant_representative_strategy_id"] = strategy_ids[winner]
            eliminated.append(loser)
            result.loc[loser, "auto_status"] = "FILTERED"
            result.loc[loser, "elimination_reason"] = "LOT_VARIANT_" + (";".join(failed) if failed else "INCOME_WINS")
    return eliminated


def run_selection(
    candidates: pd.DataFrame, request: SelectionRequest, config: SelectionConfig = SelectionConfig()
) -> pd.DataFrame:
    """Apply the submitted stages in order; input candidates remain fully represented."""
    equity_rank_enabled = any(
        stage.id == "rank_robust_top_n" and stage.enabled and stage.method == "equity_quality_v1"
        for stage in request.stages
    )
    if equity_rank_enabled:
        if "strategy_id" not in candidates:
            raise _error("EQUITY_RANK_INVALID_STRATEGY_ID")
        ids = candidates["strategy_id"].tolist()
        if any(isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) < 1 for value in ids):
            raise _error("EQUITY_RANK_INVALID_STRATEGY_ID")
        if len({int(value) for value in ids}) != len(ids):
            raise _error("EQUITY_RANK_DUPLICATE_STRATEGY_ID")
    result = candidates.copy()
    result["_source_order"] = np.arange(len(result))
    result = result.sort_values(["strategy_name", "strategy_id"], kind="stable").reset_index(drop=True)
    if "symbol" not in result:
        result["symbol"] = request.symbol
    if "side" not in result:
        result["side"] = request.side
    if "prior_rejected" not in result:
        result["prior_rejected"] = False
    result["prior_rejected"] = result["prior_rejected"].fillna(False).astype(bool)
    equity_filter_enabled = any(
        stage.id == "filter_equity_regime" and stage.enabled for stage in request.stages
    )
    equity_consumer_enabled = equity_rank_enabled or equity_filter_enabled
    if equity_consumer_enabled and "_equity_cache" not in result:
        raise _error("EQUITY_CACHE_INCOMPLETE")
    if equity_consumer_enabled:
        projected_quality: list[object] = []
        projected_state: list[object] = []
        projected_disposition: list[object] = []
        projected_reason: list[object] = []
        for index in result.index:
            entry = result.at[index, "_equity_cache"]
            if not isinstance(entry, Mapping) or entry.get("status") != "FRESH":
                if isinstance(entry, Mapping) and entry.get("status") == "SCHEMA5":
                    raise EquitySchemaUpgradeRequiredError("EQUITY_SCHEMA_UPGRADE_REQUIRED")
                raise _error("EQUITY_CACHE_INCOMPLETE")
            facts = entry.get("facts")
            cached = {
                "facts": facts,
                "source_revision": entry.get("source_revision"),
                "facts_sha256": entry.get("facts_sha256"),
            }
            _validate_equity_quality_evidence(cached, result.at[index, "result_id"])
            projected_quality.append(cached)
            projected_state.append(facts.state)
            projected_disposition.append(facts.erf_disposition)
            projected_reason.append(facts.reason)
        result["_equity_quality"] = projected_quality
        if equity_filter_enabled:
            result["_equity_state"] = projected_state
            result["_equity_disposition"] = projected_disposition
            result["_equity_reason"] = projected_reason
    if equity_rank_enabled:
        if "result_id" not in result or "_equity_quality" not in result:
            raise _error("EQUITY_CACHE_INCOMPLETE")
        for index in result.index:
            _validate_equity_quality_evidence(result.at[index, "_equity_quality"], result.at[index, "result_id"])
    result["finalist"] = True
    result["elimination_reason"] = None
    result["auto_status"] = None
    result["analog_group_key"] = None
    result["auto_analog_of_strategy_id"] = pd.NA
    result["lot_variant_group_key"] = None
    result["lot_variant_representative_strategy_id"] = pd.NA
    stage_counts: dict[str, dict[str, int | bool]] = {}
    if equity_filter_enabled:
        equity_columns = ("_equity_state", "_equity_disposition", "_equity_reason")
        if any(column not in result for column in equity_columns):
            raise _error("EQUITY_CACHE_INCOMPLETE")
        if result[list(equity_columns)].isna().any().any() or not result["_equity_disposition"].isin(
            {"PASS", "BLOCK", "BLOCK_IF_ERF_ENABLED", "NOT_EVALUATED"}
        ).all():
            raise _error("EQUITY_CACHE_INCOMPLETE")
        result["equity_regime_state"] = result["_equity_state"]
        result["equity_regime_disposition"] = result["_equity_disposition"]
        result["equity_regime_reason"] = result["_equity_reason"]
    explicit_stage_ids = {stage.id for stage in request.stages}
    stages = list(effective_selection_stages(request, config))
    implicit_lot_variant_stage = _LOT_VARIANT_STAGE_ID not in explicit_stage_ids
    for stage in stages:
        if stage.id == "filter_equity_regime" and not stage.enabled:
            stage_counts[stage.id] = {
                "enabled": False, "eliminated": 0, "remaining": int(result["finalist"].sum()),
            }
            continue
        column = f"eliminated_by_{stage.id}"
        result[column] = False
        if not stage.enabled or (stage.id == _LOT_VARIANT_STAGE_ID and not config.lot_variant_redundancy_enabled):
            if stage.id != _LOT_VARIANT_STAGE_ID or not implicit_lot_variant_stage:
                stage_counts[stage.id] = {"enabled": False, "eliminated": 0, "remaining": int(result["finalist"].sum())}
            continue
        if stage.id == _LOT_VARIANT_STAGE_ID:
            survivors = result.loc[result["finalist"]]
            for _, group in _scope_groups(survivors, stage.scope):
                eliminated = _lot_variant_eliminated(result, group, config, protect_equity=False)
                if eliminated:
                    result.loc[eliminated, column] = True
                    result.loc[eliminated, "finalist"] = False
            if stage.id != _LOT_VARIANT_STAGE_ID or not implicit_lot_variant_stage:
                stage_counts[stage.id] = {"enabled": True, "eliminated": int(result[column].sum()), "remaining": int(result["finalist"].sum())}
            continue
        if stage.id == "pair_side_pnl_upper_half":
            survivors = result.loc[result["finalist"]]
            eliminated: list[object] = []
            upper_reasons: dict[object, str] = {}
            for _, group in _scope_groups(survivors, stage.scope):
                group_reasons = _upper_half_reasons(group, config)
                eliminated.extend(group_reasons)
                upper_reasons.update(group_reasons)
            if eliminated:
                result.loc[eliminated, column] = True
                result.loc[eliminated, "finalist"] = False
                result.loc[eliminated, "elimination_reason"] = [
                    f"{stage.id.upper()};{upper_reasons[index]}" for index in eliminated
                ]
            stage_counts[stage.id] = {"enabled": True, "eliminated": int(result[column].sum()), "remaining": int(result["finalist"].sum())}
            continue
        if stage.id in {"structural_stage_1", "structural_stage_2", "pair_side_stage_3"}:
            survivors = result.loc[result["finalist"]]
            survivor_ids = [_research_sid(value) for value in survivors["strategy_id"]]
            if 0 in survivor_ids or len(set(survivor_ids)) != len(survivor_ids):
                raise _error("RESEARCHED_INVALID_STRATEGY_ID")
            decisions: dict[int, _ResearchDecision] = {}
            for _, group in _scope_groups(survivors, stage.scope):
                decisions.update(_research_decisions(group, stage.id, config))
            eliminated = [
                index for index, row in survivors.iterrows()
                if decisions.get(
                    _research_sid(row.get("strategy_id")),
                    _ResearchDecision("KEEP", "NO_REPLACEMENT"),
                ).final == "DROP"
            ]
            if eliminated:
                result.loc[eliminated, column] = True
                result.loc[eliminated, "finalist"] = False
                result.loc[eliminated, "elimination_reason"] = [
                    f"{stage.id.upper()};{decisions[_research_sid(result.at[index, 'strategy_id'])].reason}"
                    for index in eliminated
                ]
                for index in eliminated:
                    replacement_id = decisions[_research_sid(result.at[index, "strategy_id"])].replacement_id
                    if replacement_id is not None and decisions.get(_research_sid(replacement_id), _ResearchDecision(final="DROP", reason="INVALID_REPLACEMENT")).final in {"KEEP", "RESCUE"}:
                        result.at[index, "auto_analog_of_strategy_id"] = replacement_id
            stage_counts[stage.id] = {"enabled": True, "eliminated": int(result[column].sum()), "remaining": int(result["finalist"].sum())}
            continue
        if stage.id == "rank_robust_top_n":
            survivors = result.loc[result["finalist"]]
            if stage.enabled and stage.method == "equity_quality_v1" and "final_score" in result:
                result["final_score"] = result["final_score"].astype(object)
            ranked, ordered = (
                _rank_equity_quality(survivors)
                if stage.enabled and stage.method == "equity_quality_v1"
                else _rank_robust(survivors)
            )
            rank_columns = [column for column in ranked.columns if column.startswith("rank_") or column in {"final_score", "final_rank"}]
            result.loc[ranked.index, rank_columns] = ranked.loc[:, rank_columns]
            order_position = {index: position for position, index in enumerate(ordered)}
            representatives: list[object] = []
            analog_keys = _analog_group_keys(result.loc[survivors.index])
            for key, group in result.loc[survivors.index].groupby(analog_keys, sort=False):
                group_key = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
                result.loc[group.index, "analog_group_key"] = group_key
                not_rejected = group.index[~result.loc[group.index, "prior_rejected"]]
                pool = not_rejected if len(not_rejected) else group.index
                rankable_pool = [index for index in pool if index in order_position]
                representative = (
                    min(rankable_pool, key=order_position.get)
                    if rankable_pool
                    else result.loc[pool, "strategy_id"].astype(int).idxmin()
                )
                representatives.append(representative)
                analogs = group.index.difference([representative], sort=False)
                if len(analogs):
                    result.loc[analogs, "auto_status"] = "ANALOG"
                    result.loc[analogs, "auto_analog_of_strategy_id"] = int(result.at[representative, "strategy_id"])
                    result.loc[analogs, "finalist"] = False
                    result.loc[analogs, column] = True
                    result.loc[analogs, "elimination_reason"] = "ANALOG"

            ranked_representatives = sorted(
                (index for index in representatives if index in order_position), key=order_position.get
            )
            unranked_representatives = sorted(
                (index for index in representatives if index not in order_position),
                key=lambda index: int(result.at[index, "strategy_id"]),
            )
            result.loc[survivors.index, "final_rank"] = np.nan
            result.loc[ranked_representatives, "final_rank"] = range(1, len(ranked_representatives) + 1)
            selected = 0
            for index in [*ranked_representatives, *unranked_representatives]:
                result.at[index, "finalist"] = False
                if result.at[index, "prior_rejected"]:
                    result.at[index, "auto_status"] = "RESERVE"
                    result.at[index, "elimination_reason"] = "PRIOR_USER_REJECTED"
                elif index not in order_position:
                    result.at[index, "auto_status"] = "RESERVE"
                    result.at[index, "elimination_reason"] = "RANK_NOT_EVALUATED_INSUFFICIENT_DATA"
                elif selected < stage.top_n:
                    result.at[index, "auto_status"] = "FINALIST"
                    result.at[index, "finalist"] = True
                    result.at[index, "elimination_reason"] = None
                    selected += 1
                else:
                    result.at[index, "auto_status"] = "RESERVE"
                    result.at[index, "elimination_reason"] = stage.id.upper()
                result.at[index, column] = not result.at[index, "finalist"]
            eliminated_count = int(result.loc[survivors.index, column].sum())
            stage_counts[stage.id] = {"enabled": stage.enabled, "eliminated": eliminated_count, "remaining": int(result["finalist"].sum())}
            continue
        if stage.id == "filter_equity_regime":
            survivors = result.loc[result["finalist"]]
            disposition = survivors["equity_regime_disposition"]
            blocked = disposition.isin({"BLOCK", "BLOCK_IF_ERF_ENABLED"})
            not_evaluated = ~(disposition.eq("PASS") | blocked)
            eliminated = survivors.index[blocked]
            if len(eliminated):
                result.loc[eliminated, column] = True
                result.loc[eliminated, "finalist"] = False
                result.loc[eliminated, "elimination_reason"] = stage.id.upper()
            for index in survivors.index[not_evaluated]:
                if result.at[index, "elimination_reason"] is None:
                    result.at[index, "elimination_reason"] = result.at[index, "equity_regime_reason"]
            stage_counts[stage.id] = {
                "enabled": True, "eliminated": len(eliminated),
                "remaining": int(result["finalist"].sum()), "not_evaluated": int(not_evaluated.sum()),
            }
            continue
        survivors = result.loc[result["finalist"]]
        stage_evidence: dict[object, str] = {}
        for _, group in _scope_groups(survivors, stage.scope):
            if stage.id in {"filter_holding_outlier", "filter_low_trades"}:
                metric = "holding_p95_minutes" if stage.id == "filter_holding_outlier" else "trades_30d"
                if metric not in group:
                    continue
                values = pd.to_numeric(group[metric], errors="coerce").dropna()
                if values.empty:
                    continue
                q1, q3 = values.quantile(.25), values.quantile(.75)
                threshold = q3 + 1.5 * (q3 - q1) if stage.id == "filter_holding_outlier" else q1 - 1.5 * (q3 - q1)
                failed = group[metric] > threshold if stage.id == "filter_holding_outlier" else group[metric] < threshold
                eliminated = group.index[failed.fillna(False)]
            elif stage.id == "filter_min_shift":
                threshold_bp = stage.min_shift_pct * 100
                shift_columns = [f"order_{order}_shift_bp" for order in range(1, 5) if f"order_{order}_shift_bp" in group]
                failed = group[shift_columns].apply(
                    lambda shifts: any(_present(value) and Decimal(str(value)) < threshold_bp for value in shifts), axis=1
                ) if shift_columns else pd.Series(False, index=group.index)
                eliminated = group.index[failed]
            elif stage.id == "filter_hard_cutoffs":
                eliminated = []
                for index, row in group.iterrows():
                    decision, evidence = _hard_cutoff_evidence(row, config)
                    if decision:
                        eliminated.append(index)
                        stage_evidence[index] = evidence or stage.id.upper()
            elif stage.id == "filter_best_trade_dependency":
                eliminated = []
                for index, row in group.iterrows():
                    reliable_value = row.get("completed_cycles_reliable", row.get("top5_reliable", False))
                    reliable = _is_bool(reliable_value, True)
                    history = _decimal_or_none(row.get("history_days"))
                    count = _integer(row.get("completed_profitable_cycle_count"))
                    share = _decimal_or_none(row.get("top5_share_pct"))
                    net = _decimal_or_none(row.get("completed_cycle_net_pnl"))
                    if (
                        reliable and history is not None and history >= config.top5_min_history_days
                        and count is not None and count >= config.top5_min_profitable_cycles
                        and net is not None and net > 0 and share is not None and share > config.top5_share_pct
                    ):
                        eliminated.append(index)
                        stage_evidence[index] = f"FILTER_BEST_TRADE_DEPENDENCY;TOP5_SHARE(share={share},net={net},remainder={_decimal_or_none(row.get('pnl_after_top5'))})"
                    elif not reliable or any(value is None for value in (history, count, net, share)):
                        if result.at[index, "elimination_reason"] is None:
                            result.at[index, "elimination_reason"] = "TOP5_NOT_EVALUATED_INSUFFICIENT_DATA"
            elif stage.id == "filter_time_consistency":
                eliminated = []
            elif stage.id == "ab_deterioration":
                eliminated = []
                for index, row in group.iterrows():
                    decision, evidence = _ab_evidence(row, config)
                    if decision:
                        eliminated.append(index)
                        stage_evidence[index] = evidence or stage.id.upper()
                    elif evidence is not None and result.at[index, "elimination_reason"] is None:
                        result.at[index, "elimination_reason"] = evidence
            else:
                if stage.id == "pareto_conditional_close_ma" and len(group) <= 3:
                    stage_counts[stage.id] = {"enabled": True, "eliminated": 0, "remaining": int(result["finalist"].sum())}
                    continue
                eliminated = (
                    _plateau_pareto_eliminated(group, stage.id, config)
                    if stage.id.startswith("pareto_plateau_points")
                    else _shift_near_tie_eliminated(group, stage, config)
                    if stage.id == "pareto_shift_near_tie"
                    else _close_ma_near_tie_eliminated(group, stage)
                    if stage.id == "pareto_close_ma_near_tie"
                    else _pareto_eliminated(group, *_PARETO_OBJECTIVES[stage.id])
                )
            if len(eliminated):
                result.loc[eliminated, column] = True
                result.loc[eliminated, "finalist"] = False
                result.loc[eliminated, "elimination_reason"] = [
                    stage_evidence.get(index, stage.id.upper()) for index in eliminated
                ]
        if stage.id != _LOT_VARIANT_STAGE_ID or not implicit_lot_variant_stage:
            stage_counts[stage.id] = {"enabled": True, "eliminated": int(result[column].sum()), "remaining": int(result["finalist"].sum())}
    result.loc[result["auto_status"].isna() & result["finalist"], "auto_status"] = "FINALIST"
    result.loc[result["auto_status"].isna() & ~result["finalist"], "auto_status"] = "FILTERED"
    equity_consumer_enabled = equity_rank_enabled or equity_filter_enabled
    if equity_consumer_enabled and "_equity_cache" in result:
        equity_columns = [
            _equity_workbook_values(result.at[index, "_equity_cache"])
            for index in result.index
        ]
        for column in ("equity_state", "equity_basis", "equity_dd_pct", "equity_smoothness"):
            result[column] = [values[column] for values in equity_columns]
    if equity_rank_enabled:
        result.attrs["equity_quality_facts"] = {
            str(int(row["strategy_id"])): {
                "result_id": int(row["result_id"]),
                "source_revision": row["_equity_quality"]["source_revision"],
                "facts_sha256": row["_equity_quality"]["facts_sha256"],
                "facts": row["_equity_quality"]["facts"].to_canonical_dict(),
            }
            for _, row in result.iterrows()
        }
    result = result.drop(columns=[
        "_source_order", "_equity_state", "_equity_disposition", "_equity_reason", "_equity_quality", "_equity_cache",
    ], errors="ignore")
    result.attrs["stage_counts"] = stage_counts
    return result


def write_selection_workbook(
    result: pd.DataFrame,
    path: Path,
    request: SelectionRequest,
    review_metadata: Mapping[str, str] | None = None,
    user_review_rows: Mapping[int, Mapping[str, object]] | None = None,
) -> Path:
    """Write the one disposable selection workbook; internal A/B facts stay internal."""
    equity_rank = next((
        stage for stage in request.stages
        if stage.id == "rank_robust_top_n" and stage.enabled and stage.method == "equity_quality_v1"
    ), None)
    selection_method = None if equity_rank is None else "equity_quality_v1"
    equity_request_enabled = selection_method is not None or any(
        stage.id == "filter_equity_regime" and stage.enabled for stage in request.stages
    )
    equity_columns = ("equity_state", "equity_basis", "equity_dd_pct", "equity_smoothness")
    cached_equity_values = result.get("_equity_cache")
    fresh_equity_present = cached_equity_values is not None and any(
        isinstance(value, Mapping) and value.get("status") == "FRESH"
        for value in cached_equity_values
    )
    if "_equity_cache" in result and (equity_request_enabled or fresh_equity_present):
        values = [_equity_workbook_values(value) for value in result["_equity_cache"]]
        missing_equity_columns = [column for column in equity_columns if column not in result]
        if missing_equity_columns:
            result = result.copy()
            for column in missing_equity_columns:
                result[column] = [item[column] for item in values]
    display = result.drop(columns=[
        column for column in result.columns
        if column.startswith("ab_") and column not in {
            "ab_pnl_change_30d_pct", "ab_return_a_30d_pct", "ab_calendar_days_a", "ab_return_b_30d_pct", "ab_calendar_days_b", "ab_stability_ratio", "ab_completed_cycle_count", "ab_win_rate_b_pct",
        }
    ] + ["total_pnl", "total_pnl_pct", "max_drawdown", "total_fees", "risk_scale", "scaled_lot_sum", "daily_log_return", "_equity_cache"], errors="ignore").copy()
    equity_block_enabled = equity_request_enabled or any(column in display for column in equity_columns)
    if equity_block_enabled:
        for column in equity_columns:
            if column not in display:
                display[column] = None
    if "ab_pnl_change_30d_pct" not in display:
        display["ab_pnl_change_30d_pct"] = None
    enabled_stages = [stage for stage in effective_selection_stages(request) if stage.enabled]
    reason_positions = {stage.id.upper(): index for index, stage in enumerate(enabled_stages, start=1)}
    reason_colors = {
        stage.id.upper(): "".join(
            f"{round(value * 255):02X}"
            for value in hls_to_rgb(((220 + 140 * index / max(1, len(enabled_stages) - 1)) % 360) / 360, .96 if index == 1 else .92, .55)
        )
        for index, stage in enumerate(enabled_stages)
    }
    row_fills = [
        "D9EAD3" if finalist else reason_colors.get(str(reason))
        for finalist, reason in zip(display.get("finalist", []), display.get("elimination_reason", []))
    ]
    if "elimination_reason" in display:
        display["elimination_reason"] = display["elimination_reason"].map(
            lambda reason: (
                f"{reason_positions[reason.split(';', 1)[0]]}. {reason}"
                if reason.split(';', 1)[0] in reason_positions else reason
            ) if isinstance(reason, str) else reason
        )
    for column in ("pnl_30d_pct", "profit_factor", "win_rate_pct"):
        if column not in display:
            display[column] = None
    if "positive_quarter_status" in display:
        if "positive_quarter_count" not in display:
            display["positive_quarter_count"] = None
        if "positive_quarter_available_count" not in display:
            display["positive_quarter_available_count"] = None
    if {"positive_quarter_count", "positive_quarter_available_count"}.issubset(display.columns):
        display["positive_quarter_count"] = display.apply(
            lambda row: (
                "N/A" if row.get("positive_quarter_status") == "UNAVAILABLE" else
                f"{int(row['positive_quarter_count'])}/{int(row['positive_quarter_available_count'])}"
                if _present(row["positive_quarter_count"]) and _present(row["positive_quarter_available_count"])
                else None
            ),
            axis=1,
        )
    for column in display.columns:
        if column.endswith("_id") or "count" in column or column.endswith("_bp") or column in {"equity_dd_pct", "equity_smoothness"}:
            continue
        display[column] = display[column].map(
            lambda value: value.quantize(Decimal(".01")) if isinstance(value, Decimal) else value
        )
    for column in ("first_shift_bp", *(f"order_{order}_shift_bp" for order in range(1, 4))):
        if column in display:
            display[column] = display[column].map(
                lambda value: (Decimal(str(value)) / Decimal("100")).quantize(
                    Decimal(".1"), rounding=ROUND_HALF_UP
                ) if value is not None and not pd.isna(value) else value
            )
    for column in (
        "pnl_30d_pct", "dd5_proxy", "profit_factor", "ab_pnl_change_30d_pct",
        "ab_return_a_30d_pct", "ab_return_b_30d_pct", "pnl_without_best_trade_pct",
        "capital_efficiency", "win_rate_pct", "holding_p95_minutes",
        "holding_median_minutes", "final_rank",
        *(f"order_{order}_plateau_point_count" for order in range(1, 5)),
        *(f"order_{order}_open_ma_len" for order in range(1, 5)),
    ):
        if column in display:
            display[column] = display[column].map(
                lambda value: int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                if value is not None and not pd.isna(value) else value
            )
    def order_values(columns: list[str], render: Callable[[object], str]) -> pd.Series:
        values = display.reindex(columns=columns)
        return values.apply(
            lambda row: " / ".join(
                "-" if value is None or pd.isna(value) else render(value)
                for value in row.iloc[:max(index for index, value in enumerate(row) if value is not None and not pd.isna(value)) + 1]
            ) if any(value is not None and not pd.isna(value) for value in row) else None,
            axis=1,
        )
    ma_columns = [f"order_{order}_open_ma_len" for order in range(1, 5)]
    display["open_ma"] = order_values(
        ma_columns, lambda value: str(int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))),
    )
    lot_columns = [f"order_{order}_lot_x" for order in range(1, 5)]
    display["lots"] = order_values(
        lot_columns, lambda value: str(int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))),
    )
    point_columns = [f"order_{order}_plateau_point_count" for order in range(1, 5)]
    display["points"] = order_values(
        point_columns, lambda value: str(int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))),
    )
    display["auto_rank"] = display.get("final_rank")
    if review_metadata is not None:
        def review_value(row: pd.Series, key: str) -> object:
            review = (user_review_rows or {}).get(int(row["strategy_id"]))
            return review.get(key) if review else None

        if display.empty:
            display["user_status"] = pd.Series(dtype=object)
            display["user_rank"] = pd.Series(dtype=object)
            display["user_analog_of_strategy_id"] = pd.Series(dtype=object)
            display["retest"] = pd.Series(dtype=object)
            display["comment"] = pd.Series(dtype=object)
        else:
            display["user_status"] = display.apply(lambda row: review_value(row, "user_status"), axis=1)
            display["user_rank"] = display.apply(lambda row: review_value(row, "user_rank"), axis=1)
            display["user_analog_of_strategy_id"] = display.apply(
                lambda row: review_value(row, "user_analog_of_strategy_id"), axis=1
            )
            display["retest"] = display.apply(
                lambda row: "RETEST" if bool(row.get("prior_retest", False)) else None, axis=1
            )
            display["comment"] = display.apply(lambda row: review_value(row, "comment"), axis=1)
    def effective_date(value: object) -> str | None:
        if not _present(value):
            return None
        try:
            timestamp = pd.Timestamp(value)
        except (OverflowError, TypeError, ValueError):
            return None
        if pd.isna(timestamp):
            return None
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert(timezone.utc)
        return timestamp.strftime("%d.%m")

    for column in ("effective_start_utc", "effective_end_utc"):
        if column not in display:
            display[column] = None
        else:
            display[column] = display[column].map(effective_date)
    review_identity_columns = ["result_id"] if review_metadata is not None else []
    review_columns = [
        "user_status", "user_rank", "retest", "comment", "auto_analog_of_strategy_id",
        "user_analog_of_strategy_id",
    ] if review_metadata is not None else []
    finalist_flags = display["finalist"] if not display.empty else pd.Series(False, index=display.index, dtype=bool)
    column_order = [
        "strategy_id", *review_identity_columns, "strategy_name", "symbol", "side", "timeframe", "effective_start_utc", "effective_end_utc", "order_count", "close_ma_len",
        "pnl_30d_pct", "dd5_proxy", "ab_pnl_change_30d_pct", "ab_return_a_30d_pct", "ab_calendar_days_a", "ab_return_b_30d_pct", "ab_calendar_days_b", "positive_quarter_count",
        "capital_efficiency", "profit_factor", "max_drawdown_pct", "win_rate_pct", "total_trades", "trades_30d", "capital_proxy",
        "holding_p95_minutes", "holding_median_minutes",
        "history_days", "completed_cycle_count", "completed_profitable_cycle_count", "completed_cycle_net_pnl", "top5_pnl", "top5_share_pct", "pnl_after_top5", "ab_completed_cycle_count", "ab_win_rate_b_pct",
        "robust_pnl_30d_pct", "worst_drawdown_pct", "worst_holding_p95_minutes", "ab_stability_ratio",
        "rank_quality_robust_pnl", "rank_quality_worst_drawdown", "rank_quality_ab_stability", "rank_quality_first_shift", "rank_quality_minimum_plateau_points", "rank_quality_close_ma",
        "rank_weight_coverage_pct", "rank_weight_robust_pnl", "rank_weight_worst_drawdown", "rank_weight_ab_stability", "rank_weight_first_shift", "rank_weight_minimum_plateau_points", "rank_weight_close_ma",
        "final_score",
        *(f"order_{order}_shift_bp" for order in range(1, 4)),
        "lots",
        "points",
        "open_ma",
        *(equity_columns if equity_block_enabled else ()),
        "auto_status", "auto_rank", *review_columns,
        "elimination_reason",
    ]
    display = display.reindex(columns=column_order)
    display = display.rename(columns={
        "strategy_id": "ID", "result_id": "Result ID", "strategy_name": "Стратегия", "symbol": "Пара", "side": "Side", "timeframe": "ТФ",
        "close_ma_len": "Close", "order_count": "ORD", "effective_start_utc": "Start", "effective_end_utc": "End",
        "pnl_30d_pct": "PnL/30", "dd5_proxy": "PnL DD5/30", "profit_factor": "PF",
        "ab_pnl_change_30d_pct": "∆ PnL A/B", "ab_return_a_30d_pct": "PnL A/30д, %", "ab_calendar_days_a": "Дней A", "ab_return_b_30d_pct": "PnL B/30д, %", "ab_calendar_days_b": "Дней B", "capital_efficiency": "CE",
        "max_drawdown_pct": "DD", "win_rate_pct": "W/R", "total_trades": "Trades", "capital_proxy": "Lot DD5",
        "holding_p95_minutes": "Hold p95", "holding_median_minutes": "Hold M",
        "elimination_reason": "Причина",
        "history_days": "History days", "completed_cycle_count": "Completed cycles", "completed_profitable_cycle_count": "Profitable cycles", "completed_cycle_net_pnl": "Completed net PnL", "top5_pnl": "Top 5 PnL", "top5_share_pct": "Top 5 share, %", "pnl_after_top5": "PnL after top 5", "ab_completed_cycle_count": "B cycles", "ab_win_rate_b_pct": "B W/R",
        "positive_quarter_count": "Positive windows", "trades_30d": "Trades/30",
        "robust_pnl_30d_pct": "Robust PnL/30", "worst_drawdown_pct": "Worst DD", "worst_holding_p95_minutes": "Worst Hold p95",
        "ab_stability_ratio": "A/B stability",
        "rank_quality_robust_pnl": "Rank q PnL", "rank_quality_worst_drawdown": "Rank q DD",
        "rank_quality_ab_stability": "Rank q A/B", "rank_quality_first_shift": "Rank q Shift", "rank_quality_minimum_plateau_points": "Rank q Points",
        "rank_quality_close_ma": "Rank q Close MA",
        "rank_weight_coverage_pct": "Rank coverage, %", "rank_weight_robust_pnl": "Rank w PnL",
        "rank_weight_worst_drawdown": "Rank w DD", "rank_weight_ab_stability": "Rank w A/B",
        "rank_weight_first_shift": "Rank w Shift", "rank_weight_minimum_plateau_points": "Rank w Points",
        "rank_weight_close_ma": "Rank w Close MA",
        "final_score": "Final score (Pair+Side)",
        "open_ma": "MA",
        **{f"order_{order}_shift_bp": f"{order} Shift" for order in range(1, 4)},
        "lots": "Lots",
        "points": "Points",
        "auto_status": "Auto Status", "user_status": "User Status", "retest": "RETEST", "auto_rank": "Auto Rank",
        "user_rank": "User Rank", "auto_analog_of_strategy_id": "Auto Analog Of ID",
        "user_analog_of_strategy_id": "Analog Of ID", "comment": "Comment",
        "equity_state": "Equity state", "equity_basis": "Equity basis",
        "equity_dd_pct": "Equity DD, %", "equity_smoothness": "Equity smoothness",
    })
    finalists = display.loc[finalist_flags].copy()
    finalist_fills = [color for color, finalist in zip(row_fills, finalist_flags) if finalist]
    compact_widths = {
        header: min(255, max(len(header), max((len(str(value)) for value in display[header] if value is not None), default=0)) + 2)
        for header in ("Lots", "Points", "MA")
    }
    def finalize_workbook(workbook: Workbook) -> None:
        metadata_values = dict(review_metadata or {})
        if selection_method is not None:
            metadata_values["selection_method"] = selection_method
        if metadata_values:
            metadata_sheet = workbook.create_sheet("_MRS_SELECTION_META")
            for row in metadata_values.items():
                metadata_sheet.append(row)
            metadata_sheet.sheet_state = "veryHidden"
        for sheet_name in ("All candidates", "Finalists"):
            worksheet = workbook[sheet_name]
            headers = {cell.value: cell.column_letter for cell in worksheet[1]}
            for header, width in compact_widths.items():
                worksheet.column_dimensions[headers[header]].width = width
            if selection_method is not None and "Final score (Pair+Side)" in headers:
                score_header = worksheet[f'{headers["Final score (Pair+Side)"]}1']
                method_note = f"Selection method: {selection_method}"
                existing_comment = score_header.comment
                if existing_comment is None:
                    score_header.comment = Comment(method_note, "MRS3")
                elif method_note not in existing_comment.text:
                    comment = Comment(
                        f"{existing_comment.text}\n{method_note}", existing_comment.author or "MRS3",
                    )
                    comment.width = existing_comment.width
                    comment.height = existing_comment.height
                    score_header.comment = comment
            if "User Status" in headers:
                validation = DataValidation(
                    type="list", formula1='"FINALIST,RESERVE,ANALOG,FILTERED,REJECTED"', allow_blank=False
                )
                worksheet.add_data_validation(validation)
                validation.add(f'{headers["User Status"]}2:{headers["User Status"]}{max(2, worksheet.max_row)}')
            if "RETEST" in headers:
                validation = DataValidation(type="list", formula1='"RETEST"', allow_blank=True)
                worksheet.add_data_validation(validation)
                validation.add(f'{headers["RETEST"]}2:{headers["RETEST"]}{max(2, worksheet.max_row)}')

    workbook_path = write_audit_workbook(
        {"All candidates": display, "Finalists": finalists}, Path(path), data_widths_only=True,
        minimum_width=3, hidden_columns=frozenset({
            "Result ID", "Стратегия", "Auto Analog Of ID", "Analog Of ID", "Positive trades", "Rank coverage, %", "Rank w PnL", "Rank w DD",
            "Rank w A/B", "Rank w Shift", "Rank w Points", "Rank w Close MA",
            "Robust PnL/30", "Worst Hold p95", "Rank q PnL", "Rank q DD", "Rank q A/B",
            "Rank q Shift", "Rank q Points", "Rank q Close MA", "Final score (Pair+Side)",
            "Worst DD", "A/B stability", "Best trade, %", "PnL without best, %",
        }), numeric_decimals=True, left_aligned_columns=frozenset({"Причина"}), row_fill_colors={
            "All candidates": row_fills,
            "Finalists": finalist_fills,
        },
        number_formats={
            **{f"{order} Shift": "0.0" for order in range(1, 4)},
            **{header: "0" for header in (
                "PnL/30", "PnL DD5/30", "PF", "PnL A/30д, %", "Дней A", "PnL B/30д, %", "Дней B", "PnL without best, %",
            )},
            "Auto Rank": "0",
            "Equity DD, %": "0.0000", "Equity smoothness": "0.000000",
        },
        center_from_column=5,
        font_colors={"Дней A": "FF0000FF", "Дней B": "FF0000FF"},
        bold_columns=frozenset({"Close", "DD", "Hold p95", "1 Shift", "Auto Rank"}),
        column_edge_borders={
                "Positive windows": ("right",),
            "Hold p95": ("left",),
            "Hold M": ("right",),
            "1 Shift": ("left",),
            "3 Shift": ("right",),
                "Points": ("left", "right"),
            "MA": ("left", "right"),
            "Auto Rank": ("left", "right"),
            "Close": ("left", "right"),
        },
        finalize_workbook=finalize_workbook,
    )
    return workbook_path
