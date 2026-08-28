from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

import duckdb
import pandas as pd

from .config import AlgorithmConfig
from .lots import LotMethod, allocate_lots
from .pipeline import _base_structure, _publish_strategies, _write_json_atomic
from .analysis_storage import require_canonical_operational_surface
from .published_surface import load_published_surface
from .analysis_shortlist import filter_analysis_candidates
from .selection import (
    has_frozen_operational_facts,
    require_complete_operational_facts,
    validate_frozen_operational_facts,
)
from .strategy_json import generate_strategy, validate_strategy, validate_unique_names


V6_READY_GENERATOR_SCHEMA = "mrs3-ready-json-v6-v1"
V6_EVENT_MODE = "real_independent_events"
_V6_REQUIRED_COMPATIBILITY = (
    "surface_schema_version",
    "metric_schema_version",
    "event_schema_version",
    "readiness_schema_version",
    "frozen_facts_digest_algorithm",
)


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


def normalize_analysis_scopes(
    selected_scopes: Sequence[object],
) -> tuple[tuple[str, str, str], ...]:
    """Normalize exact (symbol, side, timeframe) scopes to sorted unique tuples."""
    if isinstance(selected_scopes, (str, bytes)) or not isinstance(selected_scopes, Sequence):
        raise ValueError("selected_scopes must be a list of (symbol, side, timeframe) tuples")
    normalized: set[tuple[str, str, str]] = set()
    for scope in selected_scopes:
        if isinstance(scope, (str, bytes)) or not isinstance(scope, Sequence) or len(scope) != 3:
            raise ValueError("each selected scope must be a (symbol, side, timeframe) tuple")
        if not all(isinstance(part, str) for part in scope):
            raise ValueError("each selected scope field must be a string")
        symbol, side, timeframe = (part.strip() for part in scope)
        side = side.upper()
        if not symbol or not timeframe:
            raise ValueError("selected scope symbol and timeframe must be non-empty strings")
        if side not in {"LONG", "SHORT"}:
            raise ValueError(f"selected scope has invalid side: {side!r}")
        normalized.add((symbol, side, timeframe))
    if not normalized:
        raise ValueError("selected_scopes must not be empty")
    return tuple(sorted(normalized))


def load_validated_plateau_facts(
    connection: duckdb.DuckDBPyConnection, run_id: str
) -> tuple[tuple[str, dict[str, object]], ...]:
    """Read and validate frozen Plateau operational facts for a run.

    Any consumer relying on frozen facts must apply the same structural and
    semantic validator; malformed or contradictory facts fail the read.
    """
    surface = connection.execute(
        "select surface_id from analysis_runs where run_id=?", [run_id]
    ).fetchone()
    if surface is None:
        raise ValueError("unknown analysis run")
    surface_id = str(surface[0])
    facts_state_row = connection.execute(
        "select facts_state from analysis_run_facts where run_id=?", [run_id]
    ).fetchone()
    require_facts = facts_state_row is None or str(facts_state_row[0]) == "COMPUTED"
    surface_points = {
        str(row[0])
        for row in connection.execute(
            "select canonical_point_key from surface_points where surface_id=?",
            [surface_id],
        ).fetchall()
    }
    result: list[tuple[str, dict[str, object]]] = []
    for plateau_id, raw_metrics in connection.execute(
        "select plateau_id, metrics_json from plateaus where run_id=? order by plateau_id",
        [run_id],
    ).fetchall():
        try:
            metrics = json.loads(str(raw_metrics))
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("plateau metrics contain invalid JSON") from error
        if not isinstance(metrics, dict):
            raise ValueError("plateau metrics must be a JSON object")
        if require_facts and bool(metrics.get("ready")):
            require_complete_operational_facts(metrics)
        if not has_frozen_operational_facts(metrics):
            continue
        members = {
            str(row[0])
            for row in connection.execute(
                "select canonical_point_key from plateau_members where run_id=? and plateau_id=?",
                [run_id, str(plateau_id)],
            ).fetchall()
        }
        validate_frozen_operational_facts(
            metrics,
            surface_point_ids=surface_points,
            plateau_all_point_ids=members,
            standalone_eligible_point_ids=tuple(metrics.get("standalone_eligible_point_ids") or ()),
        )
        result.append((str(plateau_id), metrics))
    return tuple(result)


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


def _frozen_base_structures(
    facts: Sequence[tuple[str, dict[str, object]]],
    points: pd.DataFrame,
    config: AlgorithmConfig,
    scopes: set[tuple[str, str, str]],
) -> tuple[list[dict[str, object]], pd.DataFrame]:
    """Select at most three frozen BASE points per exact scope.

    Only persisted ``base_1ord_point_id`` facts are consumed; standalone lists
    are never enumerated and no raw point is substituted for a frozen BASE.
    """
    point_by_id = {
        str(row.point_id): row._asdict()
        for row in points.itertuples(index=False)
    }
    candidates: list[dict[str, object]] = []
    for plateau_id, metrics in facts:
        if not bool(metrics.get("ready")):
            continue
        base_id = metrics.get("base_1ord_point_id")
        if not isinstance(base_id, str) or not base_id:
            continue
        if (
            str(metrics.get("symbol")),
            str(metrics.get("side")),
            str(metrics.get("timeframe")),
        ) not in scopes:
            continue
        point = point_by_id.get(base_id)
        if point is None:
            raise ValueError(
                f"frozen base point {base_id} is missing from the published surface"
            )
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
        ["pnl30_dd5", "pnl_pct", "trades", "dd_pct", "point_id"],
        ascending=[False, False, False, True, True], kind="mergesort",
    )
    selected = selected.groupby(
        ["symbol", "side", "timeframe"], sort=False, group_keys=False
    ).head(3)
    return [_base_structure(row) for _, row in selected.iterrows()], selected


def generate_analysis_strategies(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    candidate_ids: Sequence[str],
    selected_scopes: Sequence[tuple[str, str, str]],
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
    require_canonical_operational_surface(connection, surface_id)
    surface = connection.execute(
        "select side from surfaces where surface_id=?", [surface_id]
    ).fetchone()
    if surface is None:
        raise ValueError("unknown published surface")
    run_side = str(surface[0])
    facts = load_validated_plateau_facts(connection, run_id)
    template_file, target = Path(template_path).resolve(), Path(output_dir).resolve()
    template = _template(template_file)
    scopes = normalize_analysis_scopes(selected_scopes)
    points = load_published_surface(connection, surface_id).points
    surface_scopes = {
        (str(row.symbol), str(row.side), str(row.timeframe))
        for row in points.itertuples(index=False)
    }
    for scope in scopes:
        if scope[1] != run_side:
            raise ValueError(
                f"selected scope side {scope[1]} does not match run side {run_side}"
            )
        if scope not in surface_scopes:
            raise ValueError(f"selected scope {scope} is absent from the published surface")

    selected_ids = tuple(
        sorted({str(item).strip() for item in candidate_ids if str(item).strip()})
    )
    structures: list[dict[str, object]] = []
    if selected_ids:
        if criteria:
            filtered = filter_analysis_candidates(connection, run_id, criteria)
            ready_ids = {
                str(row["candidate_id"])
                for row in filtered.rows
                if row.get("filter_status") == "READY_AFTER_FILTERS"
            }
            deferred = sorted(set(selected_ids).difference(ready_ids))
            if deferred:
                raise ValueError(
                    f"selected candidate is deferred by active Phase 2 filters: {deferred}"
                )
        rows = connection.execute(
            "select candidate_id, candidate_json from candidates where run_id=? and candidate_id in (select * from unnest(?)) order by candidate_id",
            [run_id, list(selected_ids)],
        ).fetchall()
        if len(rows) != len(selected_ids):
            raise ValueError("selected candidate is absent from this analysis run")
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
        for structure in structures:
            if (
                str(structure["symbol"]),
                str(structure["side"]),
                str(structure["timeframe"]),
            ) not in scopes:
                raise ValueError("selected candidate is outside the selected scopes")

    base, _ = _frozen_base_structures(facts, points, config, set(scopes))
    # Candidate records preserve the eligibility facts that are intentionally not
    # duplicated in the immutable surface metrics.
    order_facts = {
        str(order["point_id"]): order
        for structure in structures
        for order in structure["orders"]
    }
    for structure in base:
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
    if not base and not structures:
        raise ValueError("no BASE or READY candidates exist in the selected scopes")
    variants: list[dict[str, object]] = []
    generated: list[dict[str, object]] = []
    for structure in [*base, *sorted(structures, key=lambda item: str(item["structure_id"]))]:
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
        "selected_scopes": [list(scope) for scope in scopes],
        "ready_structure_count": len(structures),
        "base_1ord_count": len(base),
        "strategy_count": len(generated),
        "template_sha256": sha256(template_file.read_bytes()).hexdigest(),
    }
    manifest_path = target / "strategy_manifest.json"
    _write_json_atomic(manifest_path, manifest)
    return GeneratedAnalysisStrategies(run_id, surface_id, strategies, manifest_path, len(generated))


def _canonical_v6_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _v6_strategy_digest(strategy: Mapping[str, object]) -> str:
    """Digest strategy content while excluding the two self-referential hashes."""
    value = json.loads(_canonical_v6_json(strategy))
    provenance = value.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("strategy_json_sha256", None)
        provenance.pop("generation_manifest_sha256", None)
    return sha256(_canonical_v6_json(value).encode("utf-8")).hexdigest()


def _v6_require_provenance(result: Mapping[str, object], run_id: str) -> tuple[dict[str, object], dict[str, object]]:
    if result.get("state") != "COMMITTED":
        raise ValueError("v6 analysis run is not committed")
    if result.get("event_mode") != V6_EVENT_MODE:
        raise ValueError("v6 analysis run has unsupported or mixed event mode")
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("v6 analysis run metadata is missing")
    if metadata.get("state") != "COMMITTED" or metadata.get("attempt_state") != "COMMITTED":
        raise ValueError("v6 analysis run is not committed")
    if str(result.get("analysis_run_id")) != str(run_id) or metadata.get("analysis_run_id") != run_id:
        raise ValueError("v6 analysis run identity mismatch")
    required = (
        "source_surface_id", "source_manifest_sha256", "source_frozen_facts_sha256",
        "compatibility_versions", "selected_scope", "selected_interval",
        "event_mode", "algorithm_version", "algorithm_config_sha256",
        "listing_dates_sha256", "canonical_identity_bytes",
    )
    if any(not metadata.get(name) for name in required):
        raise ValueError("v6 analysis run provenance is incomplete")
    if metadata.get("event_mode") != V6_EVENT_MODE:
        raise ValueError("v6 analysis run has unsupported or mixed event mode")
    versions = metadata["compatibility_versions"]
    if not isinstance(versions, Mapping) or any(
        key not in versions or versions[key] in (None, "") for key in _V6_REQUIRED_COMPATIBILITY
    ) or versions.get("surface_schema_version") != 6:
        raise ValueError("v6 compatibility tuple is missing or changed")
    scope = metadata["selected_scope"]
    interval = metadata["selected_interval"]
    if not isinstance(scope, Mapping) or not all(scope.get(key) for key in ("symbol", "side", "timeframe")):
        raise ValueError("v6 selected scope is missing")
    if not isinstance(interval, Mapping) or int(interval.get("end_ms", 0)) <= int(interval.get("start_ms", 0)):
        raise ValueError("v6 selected interval is invalid")
    identity_bytes = str(metadata["canonical_identity_bytes"])
    if sha256(identity_bytes.encode("utf-8")).hexdigest() != run_id:
        raise ValueError("v6 canonical analysis identity hash mismatch")
    try:
        identity = json.loads(identity_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("v6 canonical analysis identity is invalid") from error
    if not isinstance(identity, Mapping):
        raise ValueError("v6 canonical analysis identity is invalid")
    metadata_keys = {
        "surface_id": "source_surface_id",
        "manifest_sha256": "source_manifest_sha256",
        "frozen_facts_sha256": "source_frozen_facts_sha256",
        "compatibility_versions": "compatibility_versions",
        "selected_scope": "selected_scope",
        "selected_interval": "selected_interval",
        "event_mode": "event_mode",
        "algorithm_version": "algorithm_version",
        "algorithm_config_sha256": "algorithm_config_sha256",
        "listing_dates_sha256": "listing_dates_sha256",
    }
    for identity_key, metadata_key in metadata_keys.items():
        if identity.get(identity_key) != metadata.get(metadata_key):
            raise ValueError(f"v6 canonical identity disagrees with metadata: {identity_key}")
    return dict(metadata), {"symbol": str(scope["symbol"]), "side": str(scope["side"]), "timeframe": str(scope["timeframe"])}


def _v6_points_and_structures(
    result: Mapping[str, object], metadata: Mapping[str, object], scope: Mapping[str, str]
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    facts = result.get("facts")
    if not isinstance(facts, Mapping):
        raise ValueError("v6 analysis run facts are missing")
    raw_points = facts.get("points")
    raw_structures = facts.get("structures")
    if not isinstance(raw_points, list) or not isinstance(raw_structures, list):
        raise ValueError("v6 analysis run facts are incomplete")
    point_rows: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for raw in raw_points:
        if not isinstance(raw, Mapping):
            raise ValueError("v6 point fact is not an object")
        required = ("point_id", "symbol", "side", "timeframe", "shift_bp", "shift_pct", "open_ma", "close_ma", "pnl_pct", "dd_pct", "trades", "efficiency", "plateau_id", "economic_pass", "standalone_eligible", "depth_eligible", "refine_required", "event_mode", "_event_ids", "event_ids_hash", "point_event_count")
        if any(key not in raw or (raw[key] is None and key != "plateau_id") for key in required):
            raise ValueError("v6 point fact is missing a required metric or event field")
        point_id = str(raw["point_id"])
        if point_id in seen_ids:
            raise ValueError("v6 point facts contain duplicate point_id")
        seen_ids.add(point_id)
        if (str(raw["symbol"]), str(raw["side"]), str(raw["timeframe"])) != (scope["symbol"], scope["side"], scope["timeframe"]):
            raise ValueError("v6 point is outside the selected scope")
        if raw["event_mode"] != V6_EVENT_MODE:
            raise ValueError("v6 point has unsupported or mixed event mode")
        if not isinstance(raw["_event_ids"], (list, tuple, set)):
            raise ValueError("v6 point exact event IDs are malformed")
        event_ids = sorted({str(item) for item in raw["_event_ids"]})
        if len(event_ids) != int(raw["point_event_count"]):
            raise ValueError("v6 point event count disagrees with exact event IDs")
        expected_event_hash = sha256("|".join(event_ids).encode("utf-8")).hexdigest()
        if str(raw["event_ids_hash"]) != expected_event_hash:
            raise ValueError("v6 point event-ID hash mismatch")
        point_rows.append(dict(raw))
    points = pd.DataFrame(point_rows)
    structures: list[dict[str, object]] = []
    for raw in raw_structures:
        if not isinstance(raw, Mapping):
            raise ValueError("v6 candidate is not an object")
        if raw.get("status") != "READY_MRS3_STRUCTURE":
            continue
        candidate = dict(raw)
        orders = candidate.get("orders")
        if not isinstance(orders, (list, tuple)) or not orders:
            raise ValueError("READY v6 candidate has no orders")
        candidate["orders"] = tuple(dict(order) for order in orders if isinstance(order, Mapping))
        if len(candidate["orders"]) != len(orders):
            raise ValueError("READY v6 candidate contains malformed order")
        if not candidate.get("structure_id"):
            raise ValueError("READY v6 candidate has no identity")
        if (str(candidate.get("symbol")), str(candidate.get("side")), str(candidate.get("timeframe"))) != (scope["symbol"], scope["side"], scope["timeframe"]):
            raise ValueError("READY v6 candidate is outside the selected scope")
        structures.append(candidate)
    return points, structures


def generate_v6_analysis_strategies(
    surface_path: Path | str,
    analysis_run_id: str,
    candidate_ids: Sequence[str],
    selected_scopes: Sequence[tuple[str, str, str]],
    template_path: Path | str,
    output_dir: Path | str,
    config: AlgorithmConfig,
) -> GeneratedAnalysisStrategies:
    """Generate READY JSON directly from one committed Source v6 surface run.

    This entry point deliberately reads the run from its owning surface file;
    it never opens the legacy Analysis DuckDB or materializes a v5 mirror.
    """
    from .source_v6_surface import read_source_v6_analysis_run

    result = read_source_v6_analysis_run(surface_path, analysis_run_id)
    metadata, scope = _v6_require_provenance(result, analysis_run_id)
    if normalize_analysis_scopes(selected_scopes) != ((scope["symbol"], scope["side"], scope["timeframe"]),):
        raise ValueError("selected scopes do not match the v6 analysis run")
    points, ready = _v6_points_and_structures(result, metadata, scope)
    selected = {str(item).strip() for item in candidate_ids if str(item).strip()}
    if not selected:
        raise ValueError("no READY candidate selected")
    by_identity = {str(row.get("candidate_id", row["structure_id"])): row for row in ready}
    missing = sorted(selected.difference(by_identity))
    if missing:
        raise ValueError(f"selected candidate is absent or not READY: {missing}")
    structures = [by_identity[item] for item in sorted(selected)]
    template_file, target = Path(template_path).resolve(), Path(output_dir).resolve()
    template = _template(template_file)
    compatibility = dict(sorted(dict(metadata["compatibility_versions"]).items()))
    common = {
        "source_surface_id": str(metadata["source_surface_id"]),
        "source_manifest_sha256": str(metadata["source_manifest_sha256"]),
        "source_frozen_facts_sha256": str(metadata["source_frozen_facts_sha256"]),
        "compatibility_versions": compatibility,
        "compatibility_tuple": compatibility,
        "selected_scope": dict(scope),
        "selected_interval": dict(metadata["selected_interval"]),
        "event_mode": V6_EVENT_MODE,
        "analysis_run_id": analysis_run_id,
        "analysis_identity_sha256": analysis_run_id,
        "canonical_identity_sha256": analysis_run_id,
        "analysis_config_sha256": str(metadata["algorithm_config_sha256"]),
        "canonical_config_sha256": str(metadata["algorithm_config_sha256"]),
        "algorithm_config_sha256": str(metadata["algorithm_config_sha256"]),
        "listing_dates_sha256": str(metadata["listing_dates_sha256"]),
        "generator_schema_version": V6_READY_GENERATOR_SCHEMA,
    }
    generated: list[dict[str, object]] = []
    variants: list[dict[str, object]] = []
    for structure in structures:
        candidate_identity = str(structure.get("candidate_id", structure["structure_id"]))
        methods = (LotMethod.EQUAL,) if int(structure["order_count"]) == 1 else (LotMethod.EQUAL, LotMethod.INCOME)
        for method in methods:
            strategy = generate_strategy(template, structure, allocate_lots(structure["orders"], method, config), method, config)
            validate_strategy(strategy, structure, points, config)
            generated.append(strategy)
            variants.append({"strategy_name": strategy["name"], "structure_id": structure["structure_id"], "lot_method": method.value, "json_filename": f"{strategy['name']}.json", "variant_type": "MRS3", "candidate_identity": candidate_identity})
    if not generated:
        raise ValueError("no READY candidate selected")
    validate_unique_names(generated)
    target.mkdir(parents=True, exist_ok=True)
    strategy_hashes = {str(row["json_filename"]): _v6_strategy_digest(strategy) for row, strategy in zip(variants, generated, strict=True)}
    candidate_identity_to_strategy_names: dict[str, list[str]] = {}
    for row in variants:
        candidate_identity_to_strategy_names.setdefault(
            str(row["candidate_identity"]), []
        ).append(str(row["strategy_name"]))
    manifest_unsigned = {
        "format_version": 2,
        "generator_schema_version": V6_READY_GENERATOR_SCHEMA,
        "source_surface_id": common["source_surface_id"],
        "source_manifest_sha256": common["source_manifest_sha256"],
        "source_frozen_facts_sha256": common["source_frozen_facts_sha256"],
        "compatibility_versions": compatibility,
        "compatibility_tuple": compatibility,
        "selected_scope": dict(scope),
        "selected_interval": dict(metadata["selected_interval"]),
        "event_mode": V6_EVENT_MODE,
        "analysis_run_id": analysis_run_id,
        "analysis_identity_sha256": analysis_run_id,
        "canonical_identity_sha256": analysis_run_id,
        "analysis_config_sha256": common["analysis_config_sha256"],
        "canonical_config_sha256": common["canonical_config_sha256"],
        "algorithm_config_sha256": common["algorithm_config_sha256"],
        "listing_dates_sha256": common["listing_dates_sha256"],
        "candidate_identities": sorted(candidate_identity_to_strategy_names),
        "candidate_identity_to_strategy_names": {
            key: sorted(value) for key, value in sorted(candidate_identity_to_strategy_names.items())
        },
        "strategy_json_sha256": strategy_hashes,
        "strategy_count": len(generated),
        "template_sha256": sha256(template_file.read_bytes()).hexdigest(),
    }
    generation_hash = sha256(_canonical_v6_json(manifest_unsigned).encode("utf-8")).hexdigest()
    strategies = _publish_strategies(target, pd.DataFrame(variants), generated)
    manifest = {**manifest_unsigned, "generation_manifest_sha256": generation_hash}
    manifest_path = target / "strategy_manifest.json"
    _write_json_atomic(manifest_path, manifest)
    return GeneratedAnalysisStrategies(analysis_run_id, str(common["source_surface_id"]), strategies, manifest_path, len(generated))


# Descriptive alias for callers that name the source rather than the version.
generate_source_v6_analysis_strategies = generate_v6_analysis_strategies
