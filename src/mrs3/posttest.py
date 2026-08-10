from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Mapping

import pandas as pd

from .audit import write_audit_csvs, write_audit_workbook
from .config import AlgorithmConfig


@dataclass(frozen=True, slots=True)
class PosttestTables:
    raw: pd.DataFrame
    normalized: pd.DataFrame
    comparison: pd.DataFrame


@dataclass(frozen=True, slots=True)
class PosttestArtifacts:
    workbook: Path
    csv_directory: Path
    scaled_strategies_dir: Path
    manifest: Path
    scaled_count: int


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


def compare_posttest(
    raw_results: pd.DataFrame,
    variants: pd.DataFrame,
    config: AlgorithmConfig,
) -> PosttestTables:
    raw = _standardize_raw(raw_results)
    if variants["strategy_name"].duplicated().any():
        raise ValueError("variant strategy names must be unique")
    merged = raw.merge(
        variants[["strategy_name", "lots"]], on="strategy_name", how="left", validate="one_to_one"
    )
    if merged["lots"].isna().any():
        missing = sorted(merged.loc[merged["lots"].isna(), "strategy_name"])
        raise ValueError(f"missing audit lots for strategies: {missing}")
    normalized_rows = []
    for row in merged.to_dict(orient="records"):
        row["lots"] = _parse_lots(row["lots"])
        normalized_rows.append(normalize_dd5_row(row, config))
    normalized = pd.DataFrame(normalized_rows)
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
    return PosttestTables(raw=raw, normalized=normalized, comparison=comparison)


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
    sheets = {
        "16_Raw_MRS3_Results": tables.raw,
        "17_DD5_Normalized": tables.normalized,
        "18_Final_Comparison": tables.comparison,
    }
    write_audit_csvs(sheets, output_dir / "posttest_csv")
    write_audit_workbook(sheets, output_dir / "posttest.xlsx")


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
    try:
        variants = pd.read_excel(audit_xlsx, sheet_name="11_Lot_Variants")
    except ValueError as error:
        raise ValueError("audit workbook has no 11_Lot_Variants sheet") from error
    tables = compare_posttest(raw, variants, config)
    resolved_output = output_dir.resolve()
    resolved_output.mkdir(parents=True, exist_ok=True)
    write_posttest_outputs(tables, resolved_output)
    scaled_dir = resolved_output / "scaled_strategies"
    scaled_count = _write_scaled_strategies(
        tables, variants, strategies_dir, scaled_dir, config
    )
    manifest_path = resolved_output / "posttest_manifest.json"
    manifest = {
        "results_csv": results_csv.name,
        "results_sha256": _sha256_file(results_csv),
        "audit_xlsx": audit_xlsx.name,
        "audit_sha256": _sha256_file(audit_xlsx),
        "raw_result_count": len(tables.raw),
        "pareto_count": int(tables.comparison["pareto"].sum()),
        "scaled_strategy_count": scaled_count,
        "target_dd_pct": str(config.target_dd_pct),
        "scaled_strategies_require_retest": True,
    }
    _write_json(manifest_path, manifest)
    return PosttestArtifacts(
        workbook=resolved_output / "posttest.xlsx",
        csv_directory=resolved_output / "posttest_csv",
        scaled_strategies_dir=scaled_dir,
        manifest=manifest_path,
        scaled_count=scaled_count,
    )
