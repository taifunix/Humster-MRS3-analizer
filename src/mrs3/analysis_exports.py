from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from typing import Mapping

import duckdb
import pandas as pd

from .analysis_storage import ANALYSIS_SCHEMA_VERSION, verify_analysis_schema
from .audit import serializable_frame, write_audit_workbook


EXPORT_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class ExportResult:
    output_path: Path
    manifest_path: Path
    run_id: str
    surface_id: str
    row_counts: Mapping[str, int]


_QUERIES = {
    "surface": ("select * from surfaces where surface_id=? order by surface_id", "surface"),
    "surface_sources": ("select * from surface_sources where surface_id=? order by source_hash", "surface"),
    "surface_pairs": ("select * from surface_pairs where surface_id=? order by pair_key", "surface"),
    "surface_timeframes": ("select * from surface_timeframes where surface_id=? order by pair_key, timeframe", "surface"),
    "surface_points": ("select * from surface_points where surface_id=? order by canonical_point_key", "surface"),
    "coverage_issues": ("select * from coverage_issues where surface_id=? order by issue_id", "surface"),
    "analysis_run": ("select * from analysis_runs where run_id=? order by run_id", "run"),
    "analysis_run_facts": ("select * from analysis_run_facts where run_id=? order by run_id", "run"),
    "plateaus": ("select * from plateaus where run_id=? order by plateau_id", "run"),
    "plateau_members": ("select * from plateau_members where run_id=? order by plateau_id, canonical_point_key", "run"),
    "candidates": ("select * from candidates where run_id=? order by candidate_id", "run"),
    "candidate_plateaus": ("select * from candidate_plateaus where run_id=? order by candidate_id, plateau_id", "run"),
    "plateau_lineage": ("select * from plateau_lineage where child_run_id=? or parent_run_id=? order by lineage_id", "both"),
}


def _canonical_json(raw: object) -> str:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("analysis database contains invalid stored JSON") from error
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _frame(connection: duckdb.DuckDBPyConnection, query: str, kind: str, run_id: str, surface_id: str) -> pd.DataFrame:
    params = [run_id, run_id] if kind == "both" else [run_id if kind == "run" else surface_id]
    result = connection.execute(query, params)
    frame = pd.DataFrame(result.fetchall(), columns=[item[0] for item in result.description])
    return _canonicalize_frame(frame)


def _canonicalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    for column in (name for name in frame.columns if name.endswith("_json")):
        frame[column] = frame[column].map(lambda value: None if value is None else _canonical_json(value))
    return frame


def _safe_target(output_path: Path | str) -> Path:
    raw = Path(output_path)
    if ".." in raw.parts:
        raise ValueError("export output path traversal is not allowed")
    target = raw.resolve()
    if target.exists() and not target.is_dir():
        raise ValueError("export target is not a directory")
    if target.exists() and any(target.iterdir()):
        raise ValueError("export target is nonempty and would conflict")
    return target


def export_analysis_run(
    connection: duckdb.DuckDBPyConnection, run_id: str, output_path: Path | str
) -> ExportResult:
    """Export one published run from its supplied analysis connection, without DB writes."""
    target = _safe_target(output_path)
    verify_analysis_schema(connection)
    found = connection.execute(
        "select surface_id from analysis_runs where run_id=?", [run_id]
    ).fetchone()
    if found is None:
        raise ValueError("unknown analysis run")
    surface_id = str(found[0])
    frames = {
        name: _frame(connection, query, kind, run_id, surface_id)
        for name, (query, kind) in _QUERIES.items()
    }
    schema = dict(connection.execute("select key, value from schema_info").fetchall())
    if schema.get("schema_version") != str(ANALYSIS_SCHEMA_VERSION):
        raise ValueError("analysis database schema version is unsupported for export")

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.mrs3-stage-", dir=target.parent))
    try:
        hashes: dict[str, str] = {}
        for name, frame in frames.items():
            filename = f"{name}.csv"
            serializable_frame(frame).to_csv(
                staging / filename, index=False, float_format="%.17g", lineterminator="\n"
            )
            hashes[filename] = sha256((staging / filename).read_bytes()).hexdigest()
        workbook = staging / "analysis_run.xlsx"
        write_audit_workbook(frames, workbook)
        hashes[workbook.name] = sha256(workbook.read_bytes()).hexdigest()
        rows = frames["analysis_run_facts"].to_dict("records")
        if len(rows) != 1:
            raise ValueError("analysis run facts are missing or ambiguous")
        fact = rows[0]
        state = fact.get("facts_state")
        final_state = fact.get("final_state")
        count_names = (
            "unique_point_count", "economic_eligible_point_count",
            "event_eligible_point_count", "plateau_count", "ready_candidate_count",
        )
        if state == "COMPUTED":
            if any(fact.get(name) is None for name in count_names):
                raise ValueError("computed analysis run facts are incomplete")
            counts: dict[str, int] | None = {name: int(fact[name]) for name in count_names}
        elif state == "UNAVAILABLE_LEGACY":
            counts = None
        else:
            raise ValueError("analysis run facts state is invalid")
        facts = {"facts_state": state, "final_state": final_state, "counts": counts}
        manifest = {
            "export_format_version": EXPORT_FORMAT_VERSION,
            "analysis_schema_version": schema["schema_version"],
            "analysis_schema_fingerprint": schema.get("schema_fingerprint"),
            "run_id": run_id,
            "surface_id": surface_id,
            "row_counts": {name: len(frame) for name, frame in frames.items()},
            **facts,
            "sha256": hashes,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        if target.exists():
            target.rmdir()
        staging.replace(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return ExportResult(target, target / "manifest.json", run_id, surface_id, manifest["row_counts"])
