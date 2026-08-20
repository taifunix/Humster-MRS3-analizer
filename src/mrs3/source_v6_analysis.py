"""Read-only Source v6 analysis evidence and deterministic Plateau export.

The exporter consumes the frozen surface manifest; it never reopens HTML or
recalculates source metrics.  It intentionally keeps diagnostics separate
from production admission gates.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from typing import Mapping, Sequence
from datetime import datetime, timezone

import pandas as pd

from .source_v6_surface import read_surface, read_surface_db


def _payload(path: str | Path) -> dict[str, object]:
    return read_surface_db(path) if Path(path).suffix == ".duckdb" else read_surface(path)


def _frames(payload: Mapping[str, object]) -> dict[str, pd.DataFrame]:
    persisted = payload.get("analysis_facts")
    if isinstance(persisted, Mapping):
        return {name: pd.DataFrame(persisted.get(name, [])) for name in ("Plateaus", "Plateau Members", "Before After", "CloseMA Profiles", "Lineage", "Diagnostics")}
    raise ValueError("surface has no persisted analysis facts; refusing to rerun analysis during export")


def build_persisted_analysis_facts(
    point_facts: Sequence[Mapping[str, object]],
    point_metrics: Sequence[Mapping[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """Build analysis evidence once, at publication, from frozen point facts.

    Export only reads the resulting frames; it never invokes plateau geometry.
    """
    from .config import AlgorithmConfig
    from .eligibility import annotate_eligibility
    from .plateau import build_plateaus
    from .refine import annotate_refine
    from .selection import build_close_profiles, build_structures, select_base_one_order

    metrics = {str(item["point_key"]): item for item in point_metrics}
    rows: list[dict[str, object]] = []
    for fact in point_facts:
        key = str(fact["point_key"])
        metric = metrics.get(key, {})
        pnl = float(metric.get("TotalPnLPercent", 0))
        dd = abs(float(metric.get("MaxDrawdownPercent", 0)))
        events = int(metric.get("point_event_count", 0))
        rows.append({
            "point_id": key, "symbol": fact["symbol"], "side": fact["side"],
            "timeframe": fact["timeframe"], "shift_bp": int(fact["shift_bp"]),
            "open_ma": int(fact["open_ma_length"]), "close_ma": int(fact["close_ma_length"]),
            "pnl_pct": pnl, "dd_pct": dd,
            "trades": int(metric.get("TotalTrades", 0)),
            "wins": int(metric.get("Win", 0)), "losses": int(metric.get("Los", 0)),
            "win_rate_pct": float(metric.get("WinRate", 0)),
            "report_start": pd.Timestamp(int(fact.get("report_start_ms", 0)), unit="ms", tz="UTC"),
            "report_end": pd.Timestamp(int(fact.get("report_end_ms", 0)), unit="ms", tz="UTC"),
            "listing_date": pd.Timestamp(int(fact.get("report_start_ms", 0)), unit="ms", tz="UTC"),
            "point_event_count": events, "event_mode": "real_independent_events",
        })
    points = pd.DataFrame(rows)
    event_ids_by_point = {str(item["point_key"]): tuple(str(value) for value in item.get("event_ids", [])) for item in point_metrics}
    config = AlgorithmConfig.defaults()
    if rows:
        points = annotate_eligibility(points, config)
        points, _missing = annotate_refine(points, config)
        annotated, plateaus = build_plateaus(points, config)
        plateaus, cma_profiles = build_close_profiles(annotated, plateaus, config)
        bases = select_base_one_order(annotated, plateaus, config)
        structures, structure_diagnostics = build_structures(annotated, plateaus, cma_profiles, config)
    else:
        annotated, plateaus, cma_profiles, bases, structures, structure_diagnostics = points, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    plateau_rows: list[dict[str, object]] = []
    def clean(value: object) -> object:
        if value is None:
            return None
        try:
            return None if pd.isna(value) else value
        except (TypeError, ValueError):
            return value

    for row in plateaus.to_dict("records"):
        ids = list(row.get("all_point_ids", []))
        union = tuple(sorted({event_id for point_id in ids for event_id in event_ids_by_point.get(str(point_id), ())}))
        plateau_rows.append({
            "plateau_id": row["plateau_id"], "point_key": "|".join(ids),
            "AllPointCount": len(ids), "CoreSize": row.get("core_size", 0),
            "SupportedSize": row.get("supported_size", 0),
            "EventEligiblePointCount": sum(int(points.loc[points.point_id == p, "event_eligible"].iloc[0]) for p in ids),
            "PointEventCountSum": sum(int(points.loc[points.point_id == p, "point_event_count"].iloc[0]) for p in ids),
            "PlateauEventCount": len(union),
            "EventIds": list(union), "EventIdsHash": sha256("|".join(union).encode("utf-8")).hexdigest(),
            "primary_close_ma": clean(row.get("primary_close_ma")),
            "base_1ord_point_id": clean(row.get("base_1ord_point_id")),
            "status": clean(row.get("status")), "ready": clean(row.get("ready")),
        })
    ids = {str(row["point_id"]): row.get("plateau_id") for row in annotated.to_dict("records")}
    base_by_point = {str(point_id): row.get("base_1ord_point_id") for row in plateaus.to_dict("records") for point_id in row.get("all_point_ids", [])}
    roles = {str(row["point_id"]): row.get("plateau_role") for row in annotated.to_dict("records")}
    reasons = {str(row["point_id"]): tuple(row.get("reject_reasons") or ()) for row in annotated.to_dict("records")}
    members: list[dict[str, object]] = []
    before_after: list[dict[str, object]] = []
    profiles: list[dict[str, object]] = []
    lineage: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    lineage.extend(
        {
            "point_key": str(row["point_key"]),
            "lineage": "BASE_1ORD",
            "base_1ord_point_id": clean(row.get("base_1ord_point_id")),
        }
        for row in plateau_rows
        if row.get("base_1ord_point_id")
    )
    for fact in sorted(point_facts, key=lambda item: str(item["point_key"])):
        key = str(fact["point_key"]); metric = metrics.get(key, {}); events = int(metric.get("point_event_count", 0))
        plateau_id = ids.get(key) or "P-" + sha256(key.encode("utf-8")).hexdigest()[:16]
        members.append({"plateau_id": plateau_id, "point_key": key, "role": clean(roles.get(key)) or "UNASSIGNED", "point_event_count": events, "event_ids": list(event_ids_by_point.get(key, ()))})
        before_after.append({"plateau_id": plateau_id, "point_key": key, "before_event_eligible": True, "after_event_eligible": events >= 3, "before_reason": "SOURCE_FACTS", "after_reason": "ELIGIBLE" if events >= 3 else "POINT_EVENT_COUNT", "rejected_reasons": list(reasons.get(key, ()))})
        matched_profiles = cma_profiles.loc[cma_profiles["point_id"].eq(key)] if not cma_profiles.empty else pd.DataFrame()
        if not matched_profiles.empty:
            profiles.extend(matched_profiles.to_dict("records"))
        else:
            for close_ma in range(2, 8):
                profiles.append({"point_key": key, "close_ma": close_ma, "status": "REPRESENTATIVE" if close_ma == int(fact["close_ma_length"]) else "NO_REPRESENTATIVE", "reason": "FROZEN_SOURCE_FACT" if close_ma == int(fact["close_ma_length"]) else "MISSING_POINT"})
        lineage.append({"point_key": key, "plateau_id": plateau_id, "lineage": "SOURCE_V6_FROZEN_FACT", "base_1ord_point_id": clean(base_by_point.get(key)), "ready_structures": int(len(structures.loc[structures["plateau_ids"].apply(lambda values: key in values)]) if not structures.empty else 0)})
        if events < 3:
            diagnostics.append({"point_key": key, "code": "POINT_EVENT_COUNT_BELOW_FLOOR", "severity": "DIAGNOSTIC", "gating": False})
    if not structure_diagnostics.empty:
        diagnostics.extend(structure_diagnostics.to_dict("records"))
    if not structures.empty:
        lineage.extend({"structure_id": row["structure_id"], "lineage": "READY_MRS3_STRUCTURE", "order_count": int(row["order_count"]), "orders": list(row["orders"])} for row in structures.to_dict("records"))
    return {"Plateaus": plateau_rows, "Plateau Members": members, "Before After": before_after, "CloseMA Profiles": profiles, "Lineage": lineage, "Diagnostics": diagnostics}


def export_plateau_report(surface_path: str | Path, output_dir: str | Path) -> dict[str, object]:
    """Export CSV sheets, ``plateau_report.xlsx`` and a content manifest."""
    payload = _payload(surface_path)
    target = Path(output_dir)
    if target.exists() and any(target.iterdir()):
        raise ValueError("export output directory must be empty")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    try:
        frames = _frames(payload)
        hashes: dict[str, str] = {}
        counts: dict[str, int] = {}
        for name, frame in frames.items():
            filename = name.lower().replace(" ", "_") + ".csv"
            frame.to_csv(staging / filename, index=False, lineterminator="\n")
            hashes[filename] = sha256((staging / filename).read_bytes()).hexdigest()
            counts[name] = len(frame)
        workbook = staging / "plateau_report.xlsx"
        with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
            writer.book.properties.created = datetime(2000, 1, 1, tzinfo=timezone.utc)
            writer.book.properties.modified = datetime(2000, 1, 1, tzinfo=timezone.utc)
            for name, frame in frames.items():
                frame.to_excel(writer, sheet_name=name[:31], index=False)
        hashes[workbook.name] = sha256(workbook.read_bytes()).hexdigest()
        manifest = {
            "surface_id": payload["surface_id"],
            "report": "plateau_report.xlsx",
            "row_counts": counts,
            "sha256": hashes,
            "facts_source": "persisted_source_v6_surface",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
