from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import duckdb
import pandas as pd

from .config import AlgorithmConfig
from .lots import LotMethod, allocate_lots
from .pipeline import _base_structure, _publish_strategies, _write_json_atomic
from .published_surface import load_published_surface
from .analysis_shortlist import filter_analysis_candidates
from .strategy_json import generate_strategy, validate_strategy, validate_unique_names


@dataclass(frozen=True, slots=True)
class GeneratedAnalysisStrategies:
    run_id: str
    surface_id: str
    strategies_path: Path
    manifest_path: Path
    strategy_count: int


def _template(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid strategy template: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("strategy template must be a JSON object")
    return value


def list_analysis_candidates(
    connection: duckdb.DuckDBPyConnection, run_id: str
) -> tuple[dict[str, object], ...]:
    """Return the human-reviewable READY shortlist without mutating the run."""
    rows = connection.execute(
        "select candidate_id, candidate_json from candidates where run_id=? order by candidate_id",
        [run_id],
    ).fetchall()
    result: list[dict[str, object]] = []
    for candidate_id, raw in rows:
        try:
            candidate = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("analysis candidate contains invalid JSON") from error
        if not isinstance(candidate, dict):
            raise ValueError("analysis candidate must be a JSON object")
        if candidate.get("status") == "READY_MRS3_STRUCTURE":
            result.append({"candidate_id": str(candidate_id), **candidate})
    return tuple(result)


def _one_order_structures(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    points: pd.DataFrame,
    config: AlgorithmConfig,
    scopes: set[tuple[str, str, str]],
) -> tuple[list[dict[str, object]], pd.DataFrame]:
    """Select the three strongest standalone points before strategy generation."""
    facts = {str(row.point_id): row._asdict() for row in points.itertuples(index=False)}
    candidates: list[dict[str, object]] = []
    for plateau_id, encoded in connection.execute(
        "select plateau_id, metrics_json from plateaus where run_id=?", [run_id]
    ).fetchall():
        plateau = json.loads(str(encoded))
        if not plateau.get("ready"):
            continue
        for point_id in plateau.get("standalone_eligible_point_ids", ()):
            point = facts.get(str(point_id))
            if point is None or (
                str(point["symbol"]),
                str(point["side"]),
                str(point["timeframe"]),
            ) not in scopes:
                continue
            candidates.append(
                {
                    **point,
                    "plateau_id": str(plateau_id),
                    "standalone_eligible": True,
                    "depth_eligible": True,
                    "refine_required": False,
                    "economic_pass": True,
                    "efficiency": float(point["pnl_pct"]) / float(point["dd_pct"]),
                }
            )
    selected = pd.DataFrame(candidates).drop_duplicates("point_id")
    if selected.empty:
        return [], selected
    selected["pnl30_dd5"] = selected["pnl_pct"] * float(config.target_dd_pct) / selected["dd_pct"]
    selected = selected.sort_values(
        ["pnl30_dd5", "dd_pct", "point_event_count", "shift_bp", "point_id"],
        ascending=[False, True, False, False, True], kind="mergesort",
    )
    selected = selected.groupby(
        ["symbol", "side", "timeframe"], sort=False, group_keys=False
    ).head(3)
    return [_base_structure(row) for _, row in selected.iterrows()], selected


def generate_analysis_strategies(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    candidate_ids: list[str] | tuple[str, ...],
    template_path: Path | str,
    output_dir: Path | str,
    config: AlgorithmConfig,
    criteria: tuple[str, ...] = (),
) -> GeneratedAnalysisStrategies:
    """Publish EQUAL and INCOME JSON variants from one immutable analysis run."""
    found = connection.execute(
        "select surface_id from analysis_runs where run_id=?", [run_id]
    ).fetchone()
    if found is None:
        raise ValueError("unknown analysis run")
    surface_id = str(found[0])
    template_file, target = Path(template_path).resolve(), Path(output_dir).resolve()
    template = _template(template_file)
    selected_ids = tuple(sorted({str(item).strip() for item in candidate_ids if str(item).strip()}))
    if not selected_ids:
        raise ValueError("select at least one READY candidate before generating JSON")
    if criteria:
        filtered = filter_analysis_candidates(connection, run_id, criteria)
        ready_ids = {
            str(row["candidate_id"])
            for row in filtered.rows
            if row.get("filter_status") == "READY_AFTER_FILTERS"
        }
        deferred = sorted(set(selected_ids).difference(ready_ids))
        if deferred:
            raise ValueError(f"selected candidate is deferred by active Phase 2 filters: {deferred}")
    rows = connection.execute(
        "select candidate_id, candidate_json from candidates where run_id=? and candidate_id in (select * from unnest(?)) order by candidate_id",
        [run_id, list(selected_ids)],
    ).fetchall()
    if len(rows) != len(selected_ids):
        raise ValueError("selected candidate is absent from this analysis run")
    structures: list[dict[str, object]] = []
    non_ready: list[str] = []
    for candidate_id, raw in rows:
        try:
            candidate = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("analysis candidate contains invalid JSON") from error
        if not isinstance(candidate, dict):
            raise ValueError("analysis candidate must be a JSON object")
        if candidate.get("status") == "READY_MRS3_STRUCTURE":
            structures.append(candidate)
        else:
            non_ready.append(str(candidate_id))
    if non_ready:
        raise ValueError(f"selected candidate is not READY: {non_ready}")
    if not structures:
        raise ValueError("analysis run has no READY candidates")

    points = load_published_surface(connection, surface_id).points
    scopes = {
        (str(structure["symbol"]), str(structure["side"]), str(structure["timeframe"]))
        for structure in structures
    }
    one_order, one_order_points = _one_order_structures(connection, run_id, points, config, scopes)
    # Candidate records preserve the eligibility facts that are intentionally not
    # duplicated in the immutable surface metrics.
    order_facts = {
        str(order["point_id"]): order
        for structure in structures
        for order in structure["orders"]
    }
    for structure in one_order:
        for order in structure["orders"]:
            order_facts[str(order["point_id"])] = order
    selected = points.loc[points["point_id"].isin(order_facts)].copy()
    if len(selected) != len(order_facts):
        raise ValueError("analysis candidate references a point outside its surface")
    for index, row in selected.iterrows():
        order = order_facts[str(row["point_id"])]
        selected.loc[index, "plateau_id"] = str(order["plateau_id"])
        selected.loc[index, "shift_pct"] = float(order["shift_pct"])
        selected.loc[index, "efficiency"] = float(order["source_efficiency"])
        selected.loc[index, "economic_pass"] = True
        selected.loc[index, "standalone_eligible"] = bool(order["standalone_eligible"])
        selected.loc[index, "depth_eligible"] = bool(order["depth_eligible"])
        selected.loc[index, "refine_required"] = False
    variants: list[dict[str, object]] = []
    generated: list[dict[str, object]] = []
    for structure in [*one_order, *sorted(structures, key=lambda item: str(item["structure_id"]))]:
        methods = (LotMethod.EQUAL,) if int(structure["order_count"]) == 1 else (LotMethod.EQUAL, LotMethod.INCOME)
        for method in methods:
            strategy = generate_strategy(template, structure, allocate_lots(structure["orders"], method, config), method, config)
            validate_strategy(strategy, structure, selected, config)
            generated.append(strategy)
            variants.append({"strategy_name": strategy["name"], "structure_id": structure["structure_id"], "lot_method": method.value, "json_filename": f"{strategy['name']}.json", "variant_type": "BASE_1ORD" if int(structure["order_count"]) == 1 else "MRS3"})
    validate_unique_names(generated)
    target.mkdir(parents=True, exist_ok=True)
    strategies = _publish_strategies(target, pd.DataFrame(variants), generated)
    manifest = {
        "format_version": 1,
        "run_id": run_id,
        "surface_id": surface_id,
        "ready_structure_count": len(structures),
        "base_1ord_count": len(one_order),
        "strategy_count": len(generated),
        "template_sha256": sha256(template_file.read_bytes()).hexdigest(),
    }
    manifest_path = target / "strategy_manifest.json"
    _write_json_atomic(manifest_path, manifest)
    return GeneratedAnalysisStrategies(run_id, surface_id, strategies, manifest_path, len(generated))
