"""Immutable v6-to-tester/DD5 final shortlist joins."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb


def build_v6_final_shortlist(
    generation_manifest: str | Path,
    inbox: str | Path,
    performance_database: str | Path,
    import_id: str,
    dd5_run_id: str,
) -> dict[str, object]:
    """Join only reconciled Performance/DD5 evidence to immutable v6 provenance.

    Candidates lacking a matching tester inbox/performance/DD5 row remain visible
    with ``tested=False``; source metrics are never substituted for tester PnL.
    """
    manifest = json.loads(Path(generation_manifest).read_text(encoding="utf-8"))
    inbox_root = Path(inbox).resolve()
    inbox_manifest = json.loads((inbox_root / "inbox_manifest.json").read_text(encoding="utf-8"))
    provenance = inbox_manifest.get("v6_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("tester inbox has no v6 provenance")
    if provenance.get("analysis_run_id") != manifest.get("analysis_run_id"):
        raise ValueError("analysis run does not match tester inbox")
    if provenance.get("generation_manifest_sha256") != manifest.get("generation_manifest_sha256"):
        raise ValueError("generation manifest does not match tester inbox")
    if provenance.get("strategy_json_sha256") != manifest.get("strategy_json_sha256"):
        raise ValueError("strategy JSON hashes do not match tester inbox")
    with duckdb.connect(str(Path(performance_database).resolve()), read_only=True) as connection:
        import_row = connection.execute(
            "select batch_id, status, quarantined_count from import_runs where import_id = ?",
            [import_id],
        ).fetchone()
        if import_row is None or import_row[1] != "COMMITTED" or int(import_row[2]) != 0:
            raise ValueError("final shortlist requires committed zero-quarantine Performance evidence")
        dd5_row = connection.execute(
            "select import_id, status from dd5_runs where dd5_run_id = ?", [dd5_run_id]
        ).fetchone()
        if dd5_row != (import_id, "CALCULATION_ONLY"):
            raise ValueError("final shortlist requires calculation-only DD5 for the same import")
        rows = connection.execute(
            """
            select f.strategy_name, f.test_run_id, d.projected_pnl_dd5,
                   d.projected_dd_pct, d.pareto
            from import_files f
            left join dd5_results d on d.test_run_id = f.test_run_id and d.dd5_run_id = ?
            where f.import_id = ? and f.status in ('IMPORTED', 'SKIPPED')
            order by f.strategy_name
            """,
            [dd5_run_id, import_id],
        ).fetchall()
    by_name = {str(row[0]): row for row in rows}
    names = sorted(str(name) for name in manifest.get("strategy_json_sha256", {}))
    candidates: list[dict[str, Any]] = []
    for filename in names:
        name = Path(filename).stem
        row = by_name.get(name)
        candidates.append({
            "strategy_name": name,
            "source_surface_id": manifest.get("source_surface_id"),
            "analysis_run_id": manifest.get("analysis_run_id"),
            "generation_manifest_sha256": manifest.get("generation_manifest_sha256"),
            "tester_batch_id": inbox_manifest.get("batch_id"),
            "performance_import_id": import_id if row else None,
            "dd5_run_id": dd5_run_id if row and row[2] is not None else None,
            "tested": bool(row and row[2] is not None),
            "test_run_id": row[1] if row else None,
            "projected_pnl_dd5": row[2] if row else None,
            "projected_dd_pct": row[3] if row else None,
            "pareto": bool(row[4]) if row and row[4] is not None else False,
        })
    return {
        "status": "RECONCILED_ZERO_QUARANTINE",
        "dd5_mode": "CALCULATION_ONLY",
        "source_surface_id": manifest.get("source_surface_id"),
        "analysis_run_id": manifest.get("analysis_run_id"),
        "tester_batch_id": inbox_manifest.get("batch_id"),
        "performance_import_id": import_id,
        "dd5_run_id": dd5_run_id,
        "candidates": candidates,
    }
