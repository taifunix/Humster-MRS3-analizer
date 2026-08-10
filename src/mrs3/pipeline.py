from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Mapping

import pandas as pd

from .audit import canonical_frame_json, write_audit_csvs, write_audit_workbook
from .config import AlgorithmConfig
from .eligibility import annotate_eligibility
from .loader import load_points
from .lots import LotMethod, allocate_lots
from .locking import OutputDirectoryLock
from .models import InputAudit, Side
from .plateau import build_plateaus, find_isolated_peaks
from .refine import annotate_refine
from .selection import (
    build_close_profiles,
    build_structures,
    select_base_one_order,
)
from .strategy_json import (
    generate_strategy,
    validate_strategy,
    validate_unique_names,
)


ALGORITHM_VERSION = "0.6"


@dataclass(frozen=True, slots=True)
class SelectionInputs:
    csv_path: Path
    dates_path: Path
    template_path: Path
    side: Side
    output_dir: Path


@dataclass(slots=True)
class RunArtifacts:
    points: pd.DataFrame
    refine_requests: pd.DataFrame
    plateaus: pd.DataFrame
    close_profiles: pd.DataFrame
    isolated_peaks: pd.DataFrame
    base_one_order: pd.DataFrame
    structures: pd.DataFrame
    structure_diagnostics: pd.DataFrame
    lot_variants: pd.DataFrame
    generated_strategies: list[dict[str, object]]
    manifest: dict[str, object]
    output_dir: Path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Side):
        return value.value
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        return {str(_canonical(key)): _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        values = [_canonical(item) for item in value]
        return sorted(values, key=str) if isinstance(value, set) else values
    return value


def _flatten_config(config: AlgorithmConfig) -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    def walk(prefix: str, value: object) -> None:
        canonical = _canonical(value)
        if isinstance(canonical, dict):
            for key in sorted(canonical):
                walk(f"{prefix}.{key}" if prefix else key, canonical[key])
        elif isinstance(canonical, list):
            rows.append({"parameter": prefix, "value": json.dumps(canonical, sort_keys=True)})
        else:
            rows.append({"parameter": prefix, "value": canonical})

    walk("", config)
    return pd.DataFrame(rows)


def _aggregate_refine_requests(raw: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "target_point_id",
        "symbol",
        "side",
        "timeframe",
        "shift_bp",
        "shift_pct",
        "open_ma",
        "close_ma",
        "reason",
        "requested_by_count",
        "requested_by_point_ids",
    ]
    if raw.empty:
        return pd.DataFrame(columns=columns)
    group_columns = [
        "target_point_id",
        "symbol",
        "side",
        "timeframe",
        "shift_bp",
        "shift_pct",
        "open_ma",
        "close_ma",
        "reason",
    ]
    rows: list[dict[str, object]] = []
    for keys, group in raw.groupby(group_columns, sort=True, dropna=False):
        row = dict(zip(group_columns, keys, strict=True))
        centers = tuple(sorted(set(group["center_point_id"].astype(str))))
        row["requested_by_count"] = len(centers)
        row["requested_by_point_ids"] = centers
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def _pair_history(points: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for keys, group in points.groupby(["symbol", "side"], sort=True):
        symbol, side = keys
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "listing_date": group["listing_date"].min(),
                "report_start": group["report_start"].min(),
                "report_end": group["report_end"].max(),
                "effective_start": group["effective_start"].min(),
                "effective_days": group["effective_days"].max(),
                "history_pass": bool(group["history_pass"].all()),
            }
        )
    return pd.DataFrame(rows)


def _families(close_profiles: pd.DataFrame, config: AlgorithmConfig) -> pd.DataFrame:
    if close_profiles.empty:
        return pd.DataFrame(
            columns=["symbol", "side", "timeframe", "common_close_ma", "plateau_count", "plateau_ids"]
        )
    usable = close_profiles.loc[
        close_profiles["status"].isin({"PRIMARY_CLOSE", "CORE_CLOSE", "SUPPORTED_CLOSE"})
        & close_profiles["support"].ge(float(config.close_supported_min))
    ]
    rows: list[dict[str, object]] = []
    for keys, group in usable.groupby(["symbol", "side", "timeframe", "close_ma"], sort=True):
        ids = tuple(sorted(set(group["plateau_id"].astype(str))))
        if len(ids) >= 2:
            symbol, side, timeframe, close_ma = keys
            rows.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "timeframe": timeframe,
                    "common_close_ma": int(close_ma),
                    "plateau_count": len(ids),
                    "plateau_ids": ids,
                }
            )
    return pd.DataFrame(rows)


def _base_structure(point: pd.Series) -> dict[str, object]:
    point_id = str(point["point_id"])
    digest = hashlib.sha256(point_id.encode("utf-8")).hexdigest()[:16]
    order = {
        "id": 1,
        "plateau_id": str(point["plateau_id"]),
        "point_id": point_id,
        "open_ma": int(point["open_ma"]),
        "shift_bp": int(point["shift_bp"]),
        "shift_pct": float(point["shift_pct"]),
        "source_pnl_pct": float(point["pnl_pct"]),
        "source_dd_pct": float(point["dd_pct"]),
        "source_efficiency": float(point["efficiency"]),
        "trades": int(point["trades"]),
        "close_support": 1.0,
        "standalone_eligible": bool(point["standalone_eligible"]),
        "depth_eligible": bool(point["depth_eligible"]),
    }
    return {
        "structure_id": f"BASE_{digest}",
        "symbol": str(point["symbol"]),
        "side": str(point["side"]),
        "timeframe": str(point["timeframe"]),
        "common_close_ma": int(point["close_ma"]),
        "order_count": 1,
        "orders": (order,),
        "status": "READY_MRS3_STRUCTURE",
    }


def _write_json_atomic(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _publish_strategies(
    output_dir: Path,
    lot_variants: pd.DataFrame,
    generated: list[dict[str, object]],
) -> Path:
    target = output_dir / "strategies"
    backup = output_dir / ".strategies.mrs3-backup"
    if backup.exists():
        raise RuntimeError(f"strategy backup requires recovery: {backup}")
    staging = Path(tempfile.mkdtemp(prefix=".strategies.mrs3-stage-", dir=output_dir))
    moved_existing = False
    installed = False
    try:
        for row, strategy in zip(
            lot_variants.itertuples(index=False), generated, strict=True
        ):
            _write_json_atomic(staging / row.json_filename, strategy)
        if target.exists():
            if not target.is_dir():
                raise RuntimeError(f"strategy output is not a directory: {target}")
            target.replace(backup)
            moved_existing = True
        staging.replace(target)
        installed = True
        if moved_existing:
            shutil.rmtree(backup)
        return target
    except Exception:
        if moved_existing and backup.exists() and not target.exists():
            backup.replace(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if installed and backup.exists():
            shutil.rmtree(backup)


def _build_variants(
    template: Mapping[str, object],
    points: pd.DataFrame,
    base_one_order: pd.DataFrame,
    structures: pd.DataFrame,
    config: AlgorithmConfig,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    generated: list[dict[str, object]] = []

    def add_variant(structure: Mapping[str, object], method: LotMethod, variant_type: str) -> None:
        lots = allocate_lots(structure["orders"], method, config)
        strategy = generate_strategy(template, structure, lots, method, config)
        validate_strategy(strategy, structure, points, config)
        filename = f"{strategy['name']}.json"
        rows.append(
            {
                "strategy_name": strategy["name"],
                "structure_id": structure["structure_id"],
                "variant_type": variant_type,
                "lot_method": method.value,
                "order_count": structure["order_count"],
                "lots": tuple(str(lot) for lot in lots),
                "json_filename": filename,
            }
        )
        generated.append(strategy)

    for _, point in base_one_order.sort_values(["symbol", "side"], kind="mergesort").iterrows():
        add_variant(_base_structure(point), LotMethod.EQUAL, "BASE_1ORD")
    if not structures.empty:
        for _, structure in structures.sort_values("structure_id", kind="mergesort").iterrows():
            mapping = structure.to_dict()
            add_variant(mapping, LotMethod.EQUAL, "MRS3")
            add_variant(mapping, LotMethod.INCOME, "MRS3")
    validate_unique_names(generated)
    variants = pd.DataFrame(
        rows,
        columns=[
            "strategy_name",
            "structure_id",
            "variant_type",
            "lot_method",
            "order_count",
            "lots",
            "json_filename",
        ],
    )
    return variants, generated


def _validate_variant_registry(
    variants: pd.DataFrame,
    base_one_order: pd.DataFrame,
    structures: pd.DataFrame,
) -> None:
    base = variants.loc[variants["variant_type"].eq("BASE_1ORD")]
    mrs3 = variants.loc[variants["variant_type"].eq("MRS3")]
    if len(base) != len(base_one_order):
        raise RuntimeError("BASE_1ORD variant count does not match selected baselines")
    expected_ids = set(structures.get("structure_id", pd.Series(dtype=str)).astype(str))
    if set(mrs3["structure_id"].astype(str)) != expected_ids:
        raise RuntimeError("MRS3 variant registry does not match READY structures")
    for structure_id, group in mrs3.groupby("structure_id", sort=True):
        if len(group) != len(LotMethod) or set(group["lot_method"]) != {
            method.value for method in LotMethod
        }:
            raise RuntimeError(
                f"READY structure does not have exactly EQUAL and INCOME: {structure_id}"
            )
    if variants["strategy_name"].duplicated().any() or variants[
        "json_filename"
    ].duplicated().any():
        raise RuntimeError("variant registry contains duplicate strategy names")


def _recalibration_table(config: AlgorithmConfig) -> pd.DataFrame:
    names = [
        "equivalent_tolerance",
        "core_link_min",
        "plateau_envelope_min",
        "base_rates_all_tf_including_30m",
        "shift_factors",
        "absolute_trade_floors",
        "economic_gates",
        "close_support",
        "shift_0_3_economics",
        "fine_geometry",
        "gaps_0_6_0_8",
        "deep_gap_over_4",
        "equal_vs_income",
        "short_close_multiplier",
        "dd5_scaling_accuracy",
        "capital_proxy_accuracy",
        "position_holding_and_capital_occupancy",
        "pnl30_short_history_reliability",
    ]
    return pd.DataFrame(
        {"parameter": names, "status": "RECALIBRATE_LATER", "algorithm_version": ALGORITHM_VERSION}
    )


def _run_selection_unlocked(inputs: SelectionInputs, config: AlgorithmConfig) -> RunArtifacts:
    output_dir = inputs.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    loaded, input_audit = load_points(
        inputs.csv_path, inputs.dates_path, inputs.side, config
    )
    eligible = annotate_eligibility(loaded, config)
    refined, raw_missing = annotate_refine(eligible, config)
    refine_requests = _aggregate_refine_requests(raw_missing)
    plateau_points, plateaus = build_plateaus(refined, config)
    isolated = find_isolated_peaks(plateau_points, config)
    plateaus, close_profiles = build_close_profiles(plateau_points, plateaus, config)
    base_one_order = select_base_one_order(plateau_points, plateaus, config)
    structures, structure_diagnostics = build_structures(
        plateau_points, plateaus, close_profiles, config
    )
    template = json.loads(inputs.template_path.read_text(encoding="utf-8"))
    lot_variants, generated = _build_variants(
        template, plateau_points, base_one_order, structures, config
    )
    _validate_variant_registry(lot_variants, base_one_order, structures)
    _publish_strategies(output_dir, lot_variants, generated)

    input_audit_frame = pd.DataFrame([asdict(input_audit)])
    families = _families(close_profiles, config)
    ready_json = lot_variants[
        ["strategy_name", "structure_id", "variant_type", "lot_method", "json_filename"]
    ].copy()
    if structure_diagnostics.empty:
        deep_gap = pd.DataFrame(columns=["status", "reason"])
    else:
        deep_gap = structure_diagnostics.loc[
            structure_diagnostics["status"].eq("DEEP_GAP_RESEARCH")
        ].copy()
    tables = {
        "00_Input_Audit": input_audit_frame,
        "01_Pair_History": _pair_history(plateau_points),
        "02_Filtering": plateau_points,
        "03_Refine_Required": refine_requests,
        "04_Plateau_Points": plateau_points.loc[plateau_points["plateau_id"].notna()].copy(),
        "05_Plateau_Library": plateaus,
        "06_CloseMA_Profile": close_profiles,
        "07_Isolated_Peaks": isolated,
        "08_1ORD": base_one_order,
        "09_CloseMA_Families": families,
        "10_MRS3_Structures": structures,
        "11_Lot_Variants": lot_variants,
        "12_Ready_JSON": ready_json,
        "13_Deep_Gap_Research": deep_gap,
        "14_Recalibration": _recalibration_table(config),
        "15_Config": _flatten_config(config),
        "16_Point_Events": plateau_points[[
            "point_id", "plateau_id", "symbol", "side", "timeframe", "open_ma", "close_ma", "shift_bp",
            "trades", "event_mode", "point_event_count", "event_eligible", "event_ids_hash",
        ]].copy(),
        "17_Plateau_Events": plateaus[[
            "plateau_id", "symbol", "side", "timeframe", "core_size", "supported_size", "plateau_event_count", "plateau_event_ids_hash", "status", "ready",
        ]].copy(),
    }
    write_audit_csvs(tables, output_dir / "audit_csv")
    write_audit_workbook(tables, output_dir / "audit.xlsx")

    canonical_config = json.dumps(
        _canonical(config), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256()
    digest.update(canonical_config.encode("utf-8"))
    for name, table in tables.items():
        digest.update(name.encode("utf-8"))
        digest.update(canonical_frame_json(table).encode("utf-8"))
    for strategy in generated:
        digest.update(
            json.dumps(strategy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    structure_counts = {
        f"{order_count}ORD": int(structures["order_count"].eq(order_count).sum())
        if "order_count" in structures
        else 0
        for order_count in range(2, config.max_orders + 1)
    }
    base_json_count = int(lot_variants["variant_type"].eq("BASE_1ORD").sum())
    mrs3_json_count = int(lot_variants["variant_type"].eq("MRS3").sum())
    manifest: dict[str, object] = {
        "algorithm_version": ALGORITHM_VERSION,
        "side": inputs.side.value,
        "input_file": inputs.csv_path.name,
        "input_sha256": _sha256_file(inputs.csv_path),
        "dates_sha256": _sha256_file(inputs.dates_path),
        "template_sha256": _sha256_file(inputs.template_path),
        "source_rows": input_audit.source_rows,
        "normalized_rows": len(plateau_points),
        "event_mode": str(plateau_points["event_mode"].iloc[0]),
        "event_eligible_point_count": int(plateau_points["event_eligible"].sum()),
        "event_ineligible_point_count": int((~plateau_points["event_eligible"]).sum()),
        "economic_pass_count": int(plateau_points["economic_pass"].sum()),
        "refine_point_count": int(plateau_points["refine_required"].sum()),
        "refine_request_count": len(refine_requests),
        "geometric_plateau_count": len(plateaus),
        "ready_plateau_count": int(plateaus["ready"].sum()) if not plateaus.empty else 0,
        "insufficient_event_plateau_count": int(plateaus["status"].eq("INSUFFICIENT_INDEPENDENT_EVENTS").sum()) if not plateaus.empty else 0,
        "base_1ord_count": len(base_one_order),
        "ready_structure_count": len(structures),
        "ready_structure_count_by_orders": structure_counts,
        "base_json_count": base_json_count,
        "mrs3_json_count": mrs3_json_count,
        "ready_json_count": len(generated),
        "deterministic_digest": digest.hexdigest(),
    }
    _write_json_atomic(output_dir / "run_manifest.json", manifest)
    return RunArtifacts(
        points=plateau_points,
        refine_requests=refine_requests,
        plateaus=plateaus,
        close_profiles=close_profiles,
        isolated_peaks=isolated,
        base_one_order=base_one_order,
        structures=structures,
        structure_diagnostics=structure_diagnostics,
        lot_variants=lot_variants,
        generated_strategies=generated,
        manifest=manifest,
        output_dir=output_dir,
    )


def run_selection(inputs: SelectionInputs, config: AlgorithmConfig) -> RunArtifacts:
    with OutputDirectoryLock(inputs.output_dir):
        return _run_selection_unlocked(inputs, config)
