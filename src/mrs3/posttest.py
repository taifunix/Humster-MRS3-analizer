from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Mapping

import duckdb
import pandas as pd

from .audit import write_audit_csvs, write_audit_workbook
from .config import AlgorithmConfig


@dataclass(frozen=True, slots=True)
class PosttestTables:
    raw: pd.DataFrame
    normalized: pd.DataFrame
    comparison: pd.DataFrame
    holding_cycles: pd.DataFrame
    holding_exclusions: pd.DataFrame


@dataclass(frozen=True, slots=True)
class PosttestArtifacts:
    workbook: Path
    csv_directory: Path
    manifest: Path


_PLATEAU_DIAGNOSTIC_COLUMNS = (
    "plateau_point_count",
    "base_point_trades",
    "plateau_total_trades",
)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _parse_lots(value: object) -> tuple[Decimal, ...]:
    if isinstance(value, str):
        decoded = json.loads(value)
    elif isinstance(value, (tuple, list)):
        decoded = value
    else:
        raise ValueError(f"invalid lots value: {value!r}")
    lots = tuple(_decimal(item) for item in decoded)
    if not lots:
        raise ValueError("lots must not be empty")
    return lots


def normalize_dd5_row(
    row: Mapping[str, object],
    config: AlgorithmConfig,
) -> dict[str, object]:
    pnl = _decimal(row["pnl_pct"])
    dd = _decimal(row["dd_pct"])
    days = _decimal(row["effective_days"])
    lots = tuple(_decimal(value) for value in row["lots"])
    if dd <= 0:
        raise ValueError("raw drawdown must be positive for DD5 normalization")
    if days <= 0:
        raise ValueError("effective days must be positive")
    scale = config.target_dd_pct / dd
    scaled_lots = tuple(lot * scale for lot in lots)
    projected_pnl = pnl * scale
    projected_dd = dd * scale
    capital_proxy = sum(scaled_lots, Decimal("0")) + projected_dd / Decimal("100")
    pnl30 = projected_pnl * Decimal("30") / days
    capital_efficiency = pnl30 / capital_proxy if capital_proxy > 0 else Decimal("NaN")
    return {
        **dict(row),
        "lots": lots,
        "dd5_scale": scale,
        "scaled_lots": scaled_lots,
        "projected_pnl_dd5": projected_pnl,
        "projected_dd_pct": projected_dd,
        "capital_requirement_proxy": capital_proxy,
        "pnl30_dd5": pnl30,
        "capital_efficiency_30": capital_efficiency,
    }


def pareto_front(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    keep: list[bool] = []
    records = frame.to_dict(orient="records")
    for index, candidate in enumerate(records):
        candidate_pnl = _decimal(candidate["pnl30_dd5"])
        candidate_capital = _decimal(candidate["capital_requirement_proxy"])
        dominated = False
        for other_index, other in enumerate(records):
            if index == other_index:
                continue
            other_pnl = _decimal(other["pnl30_dd5"])
            other_capital = _decimal(other["capital_requirement_proxy"])
            if (
                other_pnl >= candidate_pnl
                and other_capital <= candidate_capital
                and (other_pnl > candidate_pnl or other_capital < candidate_capital)
            ):
                dominated = True
                break
        keep.append(not dominated)
    return frame.loc[keep].reset_index(drop=True)


_PARETO_SCOPE_COLUMNS = ("symbol", "side", "timeframe")
_PARETO_VARIANTS = {
    "pareto_dd5_capital": (("pnl30_dd5",), ("capital_requirement_proxy",)),
    "pareto_dd5_holding": (("pnl30_dd5",), ("holding_p95_minutes",)),
    "pareto_dd5_close_ma": (("pnl30_dd5",), ("common_close_ma",)),
    "pareto_dd5_first_shift": (("pnl30_dd5", "first_shift_bp"), ()),
    "pareto_dd5_balanced": (
        ("pnl30_dd5", "first_shift_bp"),
        ("capital_requirement_proxy", "holding_p95_minutes", "common_close_ma"),
    ),
}


def _pareto_flag(
    frame: pd.DataFrame,
    maximize: tuple[str, ...],
    minimize: tuple[str, ...],
) -> pd.Series:
    result = pd.Series(False, index=frame.index, dtype=bool)
    for _, group in frame.groupby(list(_PARETO_SCOPE_COLUMNS), dropna=False, sort=False):
        complete = group.dropna(subset=[*maximize, *minimize])
        for index, candidate in complete.iterrows():
            dominated = False
            for other_index, other in complete.iterrows():
                if other_index == index:
                    continue
                no_worse = all(_decimal(other[column]) >= _decimal(candidate[column]) for column in maximize)
                no_worse = no_worse and all(
                    _decimal(other[column]) <= _decimal(candidate[column]) for column in minimize
                )
                strictly_better = any(
                    _decimal(other[column]) > _decimal(candidate[column]) for column in maximize
                ) or any(_decimal(other[column]) < _decimal(candidate[column]) for column in minimize)
                if no_worse and strictly_better:
                    dominated = True
                    break
            result.at[index] = not dominated
    return result


def pareto_variants(frame: pd.DataFrame) -> pd.DataFrame:
    """Append scoped, independently filterable DD5 Pareto flags."""
    required = set(_PARETO_SCOPE_COLUMNS)
    for maximize, minimize in _PARETO_VARIANTS.values():
        required.update(maximize)
        required.update(minimize)
    if not required.issubset(frame.columns):
        return frame.copy()
    result = frame.copy()
    for column, (maximize, minimize) in _PARETO_VARIANTS.items():
        result[column] = _pareto_flag(result, maximize, minimize)
    return result


_SELECTION_REQUIRED_COLUMNS = (
    *_PARETO_SCOPE_COLUMNS,
    "holding_p95_minutes",
    "trades",
    "pnl30_dd5",
    "capital_requirement_proxy",
    "capital_efficiency_30",
    "first_shift_bp",
    "common_close_ma",
)


def sequential_selection(frame: pd.DataFrame) -> pd.DataFrame:
    """Apply the approved adverse-outlier filter and sequential Pareto stages."""
    result = frame.copy()
    result["selection_holding_limit"] = float("nan")
    result["selection_trades_floor"] = float("nan")
    for column in ("selection_filter_pass", "selection_stage1", "selection_stage2", "selection_stage3_applied", "selection_stage3", "selection_final"):
        result[column] = False
    result["selection_reason"] = "FILTER_MISSING_METRICS"
    missing = [column for column in _SELECTION_REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        result["selection_reason"] = "SELECTION_INPUT_MISSING"
        return result

    for _, group in result.groupby(list(_PARETO_SCOPE_COLUMNS), dropna=False, sort=False):
        holding = pd.to_numeric(group["holding_p95_minutes"], errors="coerce")
        trades = pd.to_numeric(group["trades"], errors="coerce")
        holding_q1, holding_q3 = holding.quantile(0.25), holding.quantile(0.75)
        trades_q1, trades_q3 = trades.quantile(0.25), trades.quantile(0.75)
        holding_limit = holding_q3 + 1.5 * (holding_q3 - holding_q1)
        trades_floor = trades_q1 - 1.5 * (trades_q3 - trades_q1)
        result.loc[group.index, "selection_holding_limit"] = holding_limit
        result.loc[group.index, "selection_trades_floor"] = trades_floor

        complete = group.dropna(subset=list(_SELECTION_REQUIRED_COLUMNS[3:]))
        holding_outlier = holding > holding_limit
        low_trades = trades < trades_floor
        result.loc[group.index[holding_outlier], "selection_reason"] = "FILTER_HOLDING_OUTLIER"
        result.loc[group.index[low_trades & ~holding_outlier], "selection_reason"] = "FILTER_LOW_TRADES"
        eligible_index = complete.index.difference(group.index[holding_outlier | low_trades], sort=False)
        result.loc[eligible_index, "selection_filter_pass"] = True
        result.loc[eligible_index, "selection_reason"] = "OUT_STAGE1"

        eligible = result.loc[eligible_index]
        stage1 = _pareto_flag(eligible, ("pnl30_dd5",), ("capital_requirement_proxy",))
        stage1_index = stage1.index[stage1]
        result.loc[stage1_index, "selection_stage1"] = True
        result.loc[stage1_index, "selection_reason"] = "OUT_STAGE2"

        stage1_rows = result.loc[stage1_index]
        stage2 = _pareto_flag(stage1_rows, ("capital_efficiency_30", "first_shift_bp"), ())
        stage2_index = stage2.index[stage2]
        result.loc[stage2_index, "selection_stage2"] = True

        if len(stage2_index) > 3:
            result.loc[group.index, "selection_stage3_applied"] = True
            stage2_rows = result.loc[stage2_index]
            stage3 = _pareto_flag(stage2_rows, ("capital_efficiency_30",), ("common_close_ma",))
            final_index = stage3.index[stage3]
            result.loc[stage2_index, "selection_reason"] = "OUT_STAGE3"
        else:
            final_index = stage2_index
        result.loc[final_index, "selection_stage3"] = True
        result.loc[final_index, "selection_final"] = True
        result.loc[final_index, "selection_reason"] = "SELECTED"
    return result


def selection_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Summarize sequential-selection counts and thresholds per scope."""
    columns = [
        *_PARETO_SCOPE_COLUMNS,
        "all_candidates",
        "filter_pass",
        "filter_rejected",
        "stage1",
        "stage2",
        "stage3_applied",
        "final",
        "holding_p95_limit",
        "trades_floor",
    ]
    required = {
        *columns[:3],
        "selection_filter_pass",
        "selection_stage1",
        "selection_stage2",
        "selection_stage3_applied",
        "selection_final",
        "selection_holding_limit",
        "selection_trades_floor",
    }
    if not required.issubset(frame.columns):
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, object]] = []
    for scope, group in frame.groupby(list(_PARETO_SCOPE_COLUMNS), dropna=False, sort=True):
        symbol, side, timeframe = scope
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "timeframe": timeframe,
                "all_candidates": int(len(group)),
                "filter_pass": int(group["selection_filter_pass"].sum()),
                "filter_rejected": int((~group["selection_filter_pass"]).sum()),
                "stage1": int(group["selection_stage1"].sum()),
                "stage2": int(group["selection_stage2"].sum()),
                "stage3_applied": bool(group["selection_stage3_applied"].any()),
                "final": int(group["selection_final"].sum()),
                "holding_p95_limit": group["selection_holding_limit"].iloc[0],
                "trades_floor": group["selection_trades_floor"].iloc[0],
            }
        )
    return pd.DataFrame(rows, columns=columns)


def selection_finalists(frame: pd.DataFrame) -> pd.DataFrame:
    """Return only final candidates in deterministic decision order."""
    if "selection_final" not in frame.columns:
        return frame.iloc[0:0].copy()
    finalists = frame.loc[frame["selection_final"]]
    if finalists.empty:
        return finalists.reset_index(drop=True)
    return finalists.sort_values(
        [*_PARETO_SCOPE_COLUMNS, "pnl30_dd5", "strategy_name"],
        ascending=[True, True, True, False, True],
        kind="mergesort",
    ).reset_index(drop=True)


def _near(value: Decimal, reference: Decimal, tolerance: Decimal) -> bool:
    denominator = max(value, reference)
    if denominator <= 0:
        return value == reference
    return abs(value - reference) / denominator <= tolerance


def rank_near_ties(frame: pd.DataFrame, config: AlgorithmConfig) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    remaining = frame.sort_values(
        ["pnl30_dd5", "strategy_name"], ascending=[False, True], kind="mergesort"
    ).copy()
    groups: list[pd.DataFrame] = []
    while not remaining.empty:
        reference = _decimal(remaining.iloc[0]["pnl30_dd5"])
        mask = remaining["pnl30_dd5"].map(
            lambda value: _near(_decimal(value), reference, config.equivalent_tolerance)
        )
        group = remaining.loc[mask].sort_values(
            [
                "capital_efficiency_30",
                "capital_requirement_proxy",
                "trades",
                "strategy_name",
            ],
            ascending=[False, True, False, True],
            kind="mergesort",
        )
        groups.append(group)
        remaining = remaining.loc[~mask]
    return pd.concat(groups, ignore_index=True)


def _final_comparison_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Put calculated DD5 decision metrics before raw diagnostic fields."""
    primary = [
        "strategy_name",
        "test_run_id",
        "metric_source",
        "analysis_run_id",
        "source_point_id",
        "symbol",
        "side",
        "timeframe",
        "order_count",
        *_PLATEAU_DIAGNOSTIC_COLUMNS,
        "common_close_ma",
        "first_shift_bp",
        "shift_bp_vector",
        "projected_pnl_dd5",
        "pnl30_dd5",
        "projected_dd_pct",
        "dd5_scale",
        "lots",
        "scaled_lots",
        "capital_requirement_proxy",
        "capital_efficiency_30",
        "selection_holding_limit",
        "selection_trades_floor",
        "selection_filter_pass",
        "selection_stage1",
        "selection_stage2",
        "selection_stage3_applied",
        "selection_stage3",
        "selection_final",
        "selection_reason",
        "pareto",
        "pareto_dd5_capital",
        "pareto_dd5_holding",
        "pareto_dd5_close_ma",
        "pareto_dd5_first_shift",
        "pareto_dd5_balanced",
        "near_tie_rank",
        "full_position_cycle_count",
        "holding_mean_minutes",
        "holding_median_minutes",
        "holding_p95_minutes",
        "time_in_market_pct",
        "holding_exclusion_count",
        "pnl_pct",
        "dd_pct",
        "effective_days",
        "trades",
        "win_rate_pct",
        "profit_factor",
        "profit_factor_status",
    ]
    present = [column for column in primary if column in frame.columns]
    return frame.loc[:, present]


def _point_metrics(metrics: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in metrics:
            return metrics[name]
    raise ValueError(f"source point has no metric: {names[0]}")


def one_order_baselines(
    results: pd.DataFrame,
    analysis_database: Path,
    config: AlgorithmConfig,
    *,
    limit: int = 3,
) -> pd.DataFrame:
    """Build reproducible MRS2 one-order DD5 baselines for the tested analysis run."""
    structure_ids = {
        match.group(1)
        for name in results.get("strategy_name", pd.Series(dtype=str)).astype(str)
        for match in re.finditer(r"_(STR_[A-Za-z0-9]+)_", name)
    }
    if not structure_ids:
        return pd.DataFrame()
    connection = duckdb.connect(str(analysis_database), read_only=True)
    try:
        candidate_runs: set[str] = set()
        for run_id, encoded in connection.execute("select run_id, candidate_json from candidates").fetchall():
            if not isinstance(encoded, str):
                continue
            try:
                structure_id = json.loads(encoded).get("structure_id")
            except json.JSONDecodeError:
                continue
            if structure_id in structure_ids:
                candidate_runs.add(str(run_id))
        complete_runs = []
        for candidate_run in candidate_runs:
            candidate_ids = {
                json.loads(encoded).get("structure_id")
                for encoded, in connection.execute(
                    "select candidate_json from candidates where run_id=?", [candidate_run]
                ).fetchall()
                if isinstance(encoded, str)
            }
            if structure_ids.issubset(candidate_ids):
                complete_runs.append(candidate_run)
        if not complete_runs:
            raise ValueError("tester results do not resolve to an immutable analysis run")
        placeholders = ", ".join("?" for _ in complete_runs)
        run_id = connection.execute(
            f"select run_id from analysis_runs where run_id in ({placeholders}) order by created_at_utc desc limit 1",
            complete_runs,
        ).fetchone()[0]
        surface_id = connection.execute(
            "select surface_id from analysis_runs where run_id=?", [run_id]
        ).fetchone()[0]
        start, end = connection.execute(
            "select period_start_utc, period_end_utc from surfaces where surface_id=?", [surface_id]
        ).fetchone()
        effective_days = Decimal(str((pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 86400))
        if effective_days <= 0:
            raise ValueError("analysis surface has an invalid UTC window")
        point_ids: set[str] = set()
        for encoded, in connection.execute(
            "select metrics_json from plateaus where run_id=?", [run_id]
        ).fetchall():
            if not isinstance(encoded, str):
                continue
            plateau = json.loads(encoded)
            if plateau.get("ready"):
                point_ids.update(str(value) for value in plateau.get("standalone_eligible_point_ids", ()))
        point_rows = connection.execute(
            "select canonical_point_key, point_event_count, metrics_json from surface_points where surface_id=?",
            [surface_id],
        ).fetchall()
    finally:
        connection.close()

    baselines: list[dict[str, object]] = []
    for point_id, event_count, encoded in point_rows:
        if point_id not in point_ids or not isinstance(encoded, str):
            continue
        symbol, side, timeframe, shift_bp, _open_ma, close_ma = str(point_id).split("|")
        metrics = json.loads(encoded)
        row = normalize_dd5_row(
            {
                "strategy_name": f"MRS2_1ORD_BASELINE_{point_id.replace('|', '_')}",
                "metric_source": "MRS2_1ORD_BASELINE",
                "analysis_run_id": run_id,
                "source_point_id": point_id,
                "symbol": symbol,
                "side": side,
                "timeframe": timeframe,
                "order_count": 1,
                "common_close_ma": int(close_ma),
                "first_shift_bp": int(shift_bp),
                "shift_bp_vector": shift_bp,
                "pnl_pct": _point_metrics(metrics, "TotalPnLPercent", "total_pnl_pct"),
                "dd_pct": _point_metrics(metrics, "MaxDrawdownPercent", "max_drawdown_pct"),
                "effective_days": effective_days,
                "trades": _point_metrics(metrics, "TotalTrades", "total_trades"),
                "win_rate_pct": metrics.get("WinRate", metrics.get("win_rate_pct")),
                "profit_factor": metrics.get("ProfitFactor", metrics.get("profit_factor")),
                "lots": (Decimal("1"),),
                "point_event_count": int(event_count),
            },
            config,
        )
        baselines.append(row)
    selected = sorted(
        baselines,
        key=lambda row: (
            -_decimal(row["pnl30_dd5"]),
            _decimal(row["dd_pct"]),
            -int(row["point_event_count"]),
            -int(row["first_shift_bp"]),
            str(row["source_point_id"]),
        ),
    )[:limit]
    return pd.DataFrame(selected)


_FINAL_COMPARISON_DISPLAY_DECIMALS = (
    "projected_pnl_dd5",
    "pnl30_dd5",
    "projected_dd_pct",
    "dd5_scale",
    "capital_requirement_proxy",
    "capital_efficiency_30",
    "holding_mean_minutes",
    "holding_median_minutes",
    "holding_p95_minutes",
    "selection_holding_limit",
    "selection_trades_floor",
    "time_in_market_pct",
    "pnl_pct",
    "dd_pct",
    "effective_days",
    "win_rate_pct",
    "profit_factor",
)


def _round_final_comparison_display(frame: pd.DataFrame) -> pd.DataFrame:
    """Round Excel-facing values without changing the ranking calculations."""
    display = frame.copy()
    quantum = Decimal("0.01")
    for column in _FINAL_COMPARISON_DISPLAY_DECIMALS:
        if column not in display.columns:
            continue
        display[column] = display[column].map(
            lambda value: float(_decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))
            if pd.notna(value) and _decimal(value).is_finite()
            else value
        )
    for column in ("lots", "scaled_lots"):
        if column in display.columns:
            display[column] = display[column].map(_format_lot_vector)
    return display


def _format_lot_vector(value: object) -> str:
    values = _parse_lots(value)
    quantum = Decimal("0.01")
    return json.dumps(
        [format(lot.quantize(quantum, rounding=ROUND_HALF_UP), ".2f") for lot in values]
    )


def _standardize_raw(raw: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "total_pnl_percent": "pnl_pct",
        "total_pnl_pct": "pnl_pct",
        "TotalPnLPercent": "pnl_pct",
        "max_drawdown_percent": "dd_pct",
        "max_drawdown_pct": "dd_pct",
        "MaxDrawdownPercent": "dd_pct",
        "win_rate": "win_rate_pct",
        "WinRate": "win_rate_pct",
        "total_trades": "trades",
        "TotalTrades": "trades",
        "ProfitFactor": "profit_factor",
        "days_in_test": "effective_days",
    }
    standardized = raw.rename(columns={key: value for key, value in aliases.items() if key in raw.columns}).copy()
    if "effective_days" not in standardized.columns and "period" in standardized.columns:
        def period_days(value: object) -> Decimal | None:
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", str(value))
            if len(dates) != 2:
                return None
            return Decimal(str((pd.Timestamp(dates[1]) - pd.Timestamp(dates[0])).days))

        standardized["effective_days"] = standardized["period"].map(period_days)
    if "profit_factor" not in standardized.columns:
        standardized["profit_factor"] = pd.NA
    required = {
        "strategy_name",
        "pnl_pct",
        "dd_pct",
        "win_rate_pct",
        "profit_factor",
        "trades",
        "effective_days",
    }
    missing = sorted(required.difference(standardized.columns))
    if missing:
        raise ValueError(f"post-test results missing columns: {missing}")
    if standardized["strategy_name"].duplicated().any():
        raise ValueError("post-test strategy names must be unique")
    return standardized


def _strategy_order_metadata(strategy: Mapping[str, object]) -> dict[str, object]:
    basic = strategy["basic"]
    mrs3 = strategy["mrs3"]
    if not isinstance(basic, Mapping) or not isinstance(mrs3, Mapping):
        raise TypeError("strategy has invalid basic or mrs3 section")
    is_long = bool(basic.get("use_long"))
    orders = mrs3["ma_long" if is_long else "ma_short"]
    if not isinstance(orders, list) or not orders:
        raise TypeError("strategy has no active MRS3 orders")
    lots: list[str] = []
    shifts: list[str] = []
    has_shift_multipliers = True
    for order in orders:
        if not isinstance(order, Mapping):
            raise TypeError("strategy MRS3 order is invalid")
        lots.append(str(order["lot_x"]))
        if "multiplier" not in order:
            has_shift_multipliers = False
            continue
        multiplier = _decimal(order["multiplier"])
        change = Decimal("1") - multiplier if is_long else multiplier - Decimal("1")
        shifts.append(str(int((change * Decimal("10000")).to_integral_value(rounding=ROUND_HALF_UP))))
    close = mrs3.get("ma_close_long" if is_long else "ma_close_short")
    close_ma = int(close["len"]) if isinstance(close, Mapping) and "len" in close else None
    return {
        "lots": lots,
        "shift_bp_vector": " / ".join(shifts) if has_shift_multipliers else None,
        "side": "LONG" if is_long else "SHORT",
        "order_count": len(orders),
        "common_close_ma": close_ma,
        "first_shift_bp": int(shifts[0]) if has_shift_multipliers else None,
        **{
            column: strategy["provenance"][column]
            for column in _PLATEAU_DIAGNOSTIC_COLUMNS
            if isinstance(strategy.get("provenance"), Mapping)
            and column in strategy["provenance"]
        },
    }


def _variants_from_strategy_json(strategies_dir: Path) -> pd.DataFrame:
    """Derive DD5 lots from immutable v0.7 JSON when no legacy audit exists."""
    rows: list[dict[str, str]] = []
    for path in sorted(strategies_dir.glob("*.json")):
        if path.name == "strategy_manifest.json":
            continue
        try:
            strategy = json.loads(path.read_text(encoding="utf-8"))
            name = strategy["name"]
            metadata = _strategy_order_metadata(strategy)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError(f"cannot derive DD5 lots from strategy JSON: {path.name}") from error
        lots = metadata.pop("lots")
        if not isinstance(name, str) or not name or not isinstance(lots, list) or not lots:
            raise ValueError(f"strategy JSON has no usable DD5 lots: {path.name}")
        rows.append(
            {
                "strategy_name": name,
                "lots": json.dumps(lots),
                "json_filename": path.name,
                **metadata,
            }
        )
    if not rows:
        raise ValueError(f"strategy directory contains no JSON files: {strategies_dir}")
    return pd.DataFrame(rows)


def _variants_from_tester_strategy_settings(raw: pd.DataFrame) -> pd.DataFrame | None:
    """Derive lots from the immutable settings embedded in tester HTML results."""
    if "strategy_settings_json" not in raw.columns:
        return None
    encoded_values = raw["strategy_settings_json"].dropna().tolist()
    if not any(isinstance(value, str) and value.strip() for value in encoded_values):
        return None
    rows: list[dict[str, str]] = []
    for result in raw.to_dict(orient="records"):
        expected_name = str(result["strategy_name"])
        encoded = result.get("strategy_settings_json")
        if not isinstance(encoded, str):
            raise ValueError(f"tester result has no strategy settings JSON: {expected_name}")
        try:
            strategy = json.loads(encoded)
            name = strategy["name"]
            metadata = _strategy_order_metadata(strategy)
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError(f"cannot derive DD5 lots from tester settings: {expected_name}") from error
        lots = metadata.pop("lots")
        if name != expected_name or not isinstance(lots, list) or not lots:
            raise ValueError(f"tester settings have no usable DD5 lots: {expected_name}")
        rows.append(
            {
                "strategy_name": expected_name,
                "lots": json.dumps(lots),
                **metadata,
            }
        )
    return pd.DataFrame(rows)


_HOLDING_CYCLE_COLUMNS = (
    "strategy_name",
    "symbol",
    "position_side",
    "opened_at",
    "closed_at",
    "holding_minutes",
)
_HOLDING_EXCLUSION_COLUMNS = (
    "strategy_name",
    "action_index",
    "action",
    "symbol",
    "reason",
)


def _empty_holding_frame(columns: tuple[str, ...]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


def _trade_timestamp(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _position_side_for_trade(action: Mapping[str, object], kind: str) -> str | None:
    if kind == "closed":
        side = str(action.get("Side", "")).strip().casefold()
        return {"sell": "long", "buy": "short"}.get(side)
    side = str(action.get("Post Side", "")).strip().casefold()
    return side if side in {"long", "short"} else None


def _post_size(action: Mapping[str, object]) -> Decimal:
    size = _decimal(action["Post Size"])
    if not size.is_finite():
        raise ValueError("Post Size is not finite")
    return size


def _holding_tables(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cycle_rows: list[dict[str, object]] = []
    exclusion_rows: list[dict[str, object]] = []

    def exclude(name: str, index: int | None, action: object, symbol: object, reason: str) -> None:
        exclusion_rows.append(
            {
                "strategy_name": name,
                "action_index": index,
                "action": action,
                "symbol": symbol,
                "reason": reason,
            }
        )

    if "trades_json" in raw.columns:
        for result in raw.to_dict(orient="records"):
            name = str(result["strategy_name"])
            encoded = result.get("trades_json")
            if not isinstance(encoded, str):
                if pd.notna(encoded):
                    exclude(name, None, None, None, "INVALID_TRADES_JSON")
                continue
            try:
                actions = json.loads(encoded)
            except json.JSONDecodeError:
                exclude(name, None, None, None, "INVALID_TRADES_JSON")
                continue
            if not isinstance(actions, list):
                exclude(name, None, None, None, "INVALID_TRADES_JSON")
                continue
            indexed: list[tuple[pd.Timestamp, int, Mapping[str, object]]] = []
            for index, action in enumerate(actions):
                if not isinstance(action, Mapping):
                    exclude(name, index, None, None, "INVALID_TRADE_ROW")
                    continue
                try:
                    indexed.append((_trade_timestamp(action["Timestamp"]), index, action))
                except (KeyError, TypeError, ValueError):
                    exclude(name, index, action.get("Action"), action.get("Symbol"), "INVALID_TIMESTAMP")
            indexed.sort(key=lambda item: (item[0], item[1]))
            active: dict[tuple[str, str], pd.Timestamp] = {}
            for timestamp, index, action in indexed:
                kind = str(action.get("Action", "")).strip().casefold()
                if kind not in {"opened", "increased", "decreased", "closed"}:
                    continue
                symbol = str(action.get("Symbol", "")).strip()
                position_side = _position_side_for_trade(action, kind)
                if not symbol or position_side is None:
                    exclude(name, index, kind, symbol or None, "INVALID_POSITION_SIDE")
                    continue
                try:
                    post_size = _post_size(action)
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    exclude(name, index, kind, symbol, "INVALID_POST_SIZE")
                    continue
                key = (symbol, position_side)
                if kind == "opened":
                    if post_size <= 0:
                        exclude(name, index, kind, symbol, "OPEN_NOT_POSITIVE")
                    elif key in active:
                        exclude(name, index, kind, symbol, "OPEN_WHILE_ACTIVE")
                    else:
                        active[key] = timestamp
                    continue
                if kind in {"increased", "decreased"}:
                    if post_size <= 0:
                        exclude(name, index, kind, symbol, "NON_CLOSE_ZERO_POST_SIZE")
                    elif key not in active:
                        exclude(name, index, kind, symbol, "ACTION_WITHOUT_OPEN")
                    continue
                if post_size != 0:
                    exclude(name, index, kind, symbol, "CLOSE_NOT_ZERO")
                    continue
                opened_at = active.pop(key, None)
                if opened_at is None:
                    exclude(name, index, kind, symbol, "CLOSE_WITHOUT_OPEN")
                elif timestamp < opened_at:
                    exclude(name, index, kind, symbol, "CLOSE_BEFORE_OPEN")
                else:
                    cycle_rows.append(
                        {
                            "strategy_name": name,
                            "symbol": symbol,
                            "position_side": position_side,
                            "opened_at": opened_at,
                            "closed_at": timestamp,
                            "holding_minutes": (timestamp - opened_at).total_seconds() / 60,
                        }
                    )
            for symbol, position_side in sorted(active):
                exclude(name, None, "opened", symbol, f"NO_FULL_CLOSE:{position_side}")

    cycles = pd.DataFrame(cycle_rows, columns=_HOLDING_CYCLE_COLUMNS)
    exclusions = pd.DataFrame(exclusion_rows, columns=_HOLDING_EXCLUSION_COLUMNS)
    summary_rows: list[dict[str, object]] = []
    for result in raw.to_dict(orient="records"):
        name = str(result["strategy_name"])
        strategy_cycles = cycles.loc[cycles["strategy_name"].eq(name)]
        intervals = sorted(
            (row.opened_at, row.closed_at)
            for row in strategy_cycles.itertuples(index=False)
        )
        occupied_minutes = 0.0
        if intervals:
            start, end = intervals[0]
            for next_start, next_end in intervals[1:]:
                if next_start <= end:
                    end = max(end, next_end)
                else:
                    occupied_minutes += (end - start).total_seconds() / 60
                    start, end = next_start, next_end
            occupied_minutes += (end - start).total_seconds() / 60
        days = result.get("effective_days")
        total_minutes = float(days) * 24 * 60 if pd.notna(days) and float(days) > 0 else None
        holding = strategy_cycles["holding_minutes"]
        summary_rows.append(
            {
                "strategy_name": name,
                "full_position_cycle_count": len(strategy_cycles),
                "holding_mean_minutes": holding.mean() if not holding.empty else None,
                "holding_median_minutes": holding.median() if not holding.empty else None,
                "holding_p95_minutes": holding.quantile(0.95) if not holding.empty else None,
                "time_in_market_pct": occupied_minutes * 100 / total_minutes if total_minutes else None,
                "holding_exclusion_count": int(exclusions["strategy_name"].eq(name).sum()),
            }
        )
    return pd.DataFrame(summary_rows), cycles, exclusions


def compare_posttest(
    raw_results: pd.DataFrame,
    variants: pd.DataFrame,
    config: AlgorithmConfig,
) -> PosttestTables:
    raw = _standardize_raw(raw_results)
    if variants["strategy_name"].duplicated().any():
        raise ValueError("variant strategy names must be unique")
    variant_columns = ["strategy_name", "lots"]
    variant_metadata_columns = (
        "shift_bp_vector", "side", "order_count", "common_close_ma", "first_shift_bp",
        *_PLATEAU_DIAGNOSTIC_COLUMNS,
    )
    for column in variant_metadata_columns:
        if column in variants.columns and column not in raw.columns:
            variant_columns.append(column)
    merged = raw.merge(variants[variant_columns], on="strategy_name", how="left", validate="one_to_one")
    if "shift_bp_vector" not in merged.columns:
        embedded_variants = _variants_from_tester_strategy_settings(raw)
        if embedded_variants is not None:
            embedded_by_name = embedded_variants.set_index("strategy_name")
            for column in variant_metadata_columns:
                if column in embedded_by_name.columns and column not in merged.columns:
                    merged[column] = merged["strategy_name"].map(embedded_by_name[column])
    if merged["lots"].isna().any():
        missing = sorted(merged.loc[merged["lots"].isna(), "strategy_name"])
        raise ValueError(f"missing audit lots for strategies: {missing}")
    normalized_rows = []
    for row in merged.to_dict(orient="records"):
        row["lots"] = _parse_lots(row["lots"])
        normalized_rows.append(normalize_dd5_row(row, config))
    normalized = pd.DataFrame(normalized_rows)
    holding_summary, holding_cycles, holding_exclusions = _holding_tables(normalized)
    normalized = normalized.merge(holding_summary, on="strategy_name", how="left", validate="one_to_one")
    normalized = pareto_variants(normalized)
    normalized = sequential_selection(normalized)
    front = pareto_front(normalized)
    pareto_names = set(front["strategy_name"])
    ranked = rank_near_ties(normalized, config).copy()
    ranked["near_tie_rank"] = range(1, len(ranked) + 1)
    rank_by_name = dict(zip(ranked["strategy_name"], ranked["near_tie_rank"], strict=True))
    comparison = normalized.copy()
    comparison["pareto"] = comparison["strategy_name"].isin(pareto_names)
    comparison["near_tie_rank"] = comparison["strategy_name"].map(rank_by_name)
    comparison = comparison.sort_values(
        ["pareto", "near_tie_rank", "strategy_name"],
        ascending=[False, True, True],
        kind="mergesort",
    ).reset_index(drop=True)
    comparison = _final_comparison_columns(comparison)
    return PosttestTables(
        raw=raw,
        normalized=normalized,
        comparison=comparison,
        holding_cycles=holding_cycles,
        holding_exclusions=holding_exclusions,
    )


def scale_strategy_json(
    strategy: Mapping[str, object],
    raw_dd_pct: Decimal,
    config: AlgorithmConfig,
) -> dict[str, object]:
    dd = Decimal(raw_dd_pct)
    if dd <= 0:
        raise ValueError("raw drawdown must be positive")
    scale = config.target_dd_pct / dd
    output = deepcopy(dict(strategy))
    output["name"] = f"{output.get('name', 'strategy')}_DD5"
    basic = output.get("basic", {})
    if not isinstance(basic, Mapping):
        raise ValueError("strategy basic section must be an object")
    active_key = "ma_long" if basic.get("use_long") else "ma_short"
    mrs3 = output.get("mrs3")
    if not isinstance(mrs3, Mapping) or not isinstance(mrs3.get(active_key), list):
        raise ValueError(f"strategy has no active mrs3.{active_key} order list")
    for entry in mrs3[active_key]:
        entry["lot_x"] = float(_decimal(entry["lot_x"]) * scale)
    return output


def write_posttest_outputs(tables: PosttestTables, output_dir: Path) -> None:
    comparison_display = _round_final_comparison_display(tables.comparison)
    audit_sheets = {
        "16_Raw_MRS3_Results": tables.raw,
        "17_DD5_Normalized": tables.normalized,
        "18_Final_Comparison": tables.comparison,
        "19_Position_Holding_Cycles": tables.holding_cycles,
        "20_Position_Holding_Exclusions": tables.holding_exclusions,
    }
    workbook_sheets = {
        "00_Selection_Summary": selection_summary(comparison_display),
        "01_Finalists": selection_finalists(comparison_display),
        **audit_sheets,
    }
    workbook_sheets["18_Final_Comparison"] = comparison_display
    write_audit_csvs(audit_sheets, output_dir / "posttest_csv")
    write_audit_workbook(workbook_sheets, output_dir / "posttest.xlsx")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.stem}.", suffix=".json", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_scaled_strategies(
    tables: PosttestTables,
    variants: pd.DataFrame,
    strategies_dir: Path,
    target: Path,
    config: AlgorithmConfig,
) -> int:
    if "json_filename" not in variants.columns:
        raise ValueError("audit lot variants have no json_filename column")
    lookup = variants.set_index("strategy_name", verify_integrity=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent)
    )
    backup = target.with_name(f".{target.name}.backup")
    if backup.exists():
        shutil.rmtree(staging)
        raise ValueError(f"scaled-strategy backup requires recovery: {backup}")
    moved_existing = False
    installed = False
    try:
        for row in tables.raw.sort_values("strategy_name", kind="mergesort").to_dict(
            orient="records"
        ):
            name = str(row["strategy_name"])
            if name not in lookup.index:
                raise ValueError(f"strategy is absent from audit variants: {name}")
            filename = str(lookup.loc[name, "json_filename"])
            source = (strategies_dir.resolve() / filename).resolve()
            if source.parent != strategies_dir.resolve() or not source.is_file():
                raise ValueError(f"source strategy JSON is missing: {source}")
            document = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("name") != name:
                raise ValueError(f"source strategy name mismatch: {source}")
            scaled = scale_strategy_json(document, _decimal(row["dd_pct"]), config)
            _write_json(staging / f"{scaled['name']}.json", scaled)
        if target.exists():
            if not target.is_dir():
                raise ValueError(f"scaled strategy target is not a directory: {target}")
            target.replace(backup)
            moved_existing = True
        staging.replace(target)
        installed = True
        if moved_existing:
            shutil.rmtree(backup)
        return len(tables.raw)
    except Exception:
        if moved_existing and backup.exists() and not target.exists():
            backup.replace(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if installed and backup.exists():
            shutil.rmtree(backup)


def run_posttest(
    results_csv: Path,
    audit_xlsx: Path,
    strategies_dir: Path,
    output_dir: Path,
    config: AlgorithmConfig,
) -> PosttestArtifacts:
    raw = pd.read_csv(results_csv)
    if audit_xlsx.is_file():
        try:
            variants = pd.read_excel(audit_xlsx, sheet_name="11_Lot_Variants")
        except ValueError as error:
            raise ValueError("audit workbook has no 11_Lot_Variants sheet") from error
        audit_source = audit_xlsx.name
        audit_sha256 = _sha256_file(audit_xlsx)
    else:
        variants = _variants_from_tester_strategy_settings(raw)
        if variants is None:
            variants = _variants_from_strategy_json(strategies_dir)
            audit_source = "derived_from_strategy_json"
        else:
            audit_source = "derived_from_tester_strategy_settings"
        audit_sha256 = None
    tables = compare_posttest(raw, variants, config)
    resolved_output = output_dir.resolve()
    resolved_output.mkdir(parents=True, exist_ok=True)
    write_posttest_outputs(tables, resolved_output)
    manifest_path = resolved_output / "posttest_manifest.json"
    manifest = {
        "results_csv": results_csv.name,
        "results_sha256": _sha256_file(results_csv),
        "audit_xlsx": audit_source,
        "audit_sha256": audit_sha256,
        "raw_result_count": len(tables.raw),
        "pareto_count": int(tables.comparison["pareto"].sum()),
        "full_position_cycle_count": len(tables.holding_cycles),
        "holding_exclusion_count": len(tables.holding_exclusions),
        "target_dd_pct": str(config.target_dd_pct),
        "dd5_mode": "CALCULATION_ONLY",
        "ranking_basis": ["projected_pnl_dd5", "projected_dd_pct"],
    }
    _write_json(manifest_path, manifest)
    return PosttestArtifacts(
        workbook=resolved_output / "posttest.xlsx",
        csv_directory=resolved_output / "posttest_csv",
        manifest=manifest_path,
    )
