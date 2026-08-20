from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from mrs3.final_shortlist import build_v6_final_shortlist


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / "strategy_manifest.json"
    manifest = {
        "source_surface_id": "surface-1", "analysis_run_id": "run-1",
        "generation_manifest_sha256": "a" * 64,
        "strategy_json_sha256": {"A.json": "b" * 64},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "inbox_manifest.json").write_text(json.dumps({
        "batch_id": "batch-1", "v6_provenance": {
            "analysis_run_id": "run-1", "generation_manifest_sha256": "a" * 64,
            "strategy_json_sha256": {"A.json": "b" * 64},
        },
    }), encoding="utf-8")
    database = tmp_path / "performance.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create table import_runs(import_id varchar, batch_id varchar, status varchar, quarantined_count integer)")
        connection.execute("create table dd5_runs(dd5_run_id varchar, import_id varchar, status varchar)")
        connection.execute("create table import_files(import_id varchar, strategy_name varchar, test_run_id varchar, status varchar)")
        connection.execute("create table dd5_results(dd5_run_id varchar, test_run_id varchar, projected_pnl_dd5 decimal, projected_dd_pct decimal, pareto boolean)")
        connection.execute("insert into import_runs values ('imp-1','batch-1','COMMITTED',0)")
        connection.execute("insert into dd5_runs values ('dd5-1','imp-1','CALCULATION_ONLY')")
        connection.execute("insert into import_files values ('imp-1','A','test-1','IMPORTED')")
        connection.execute("insert into dd5_results values ('dd5-1','test-1',12.5,3.0,true)")
    return manifest_path, inbox, database


def test_final_shortlist_requires_zero_quarantine_and_joins_lineage(tmp_path: Path) -> None:
    manifest, inbox, database = _fixture(tmp_path)
    result = build_v6_final_shortlist(manifest, inbox, database, "imp-1", "dd5-1")
    row = result["candidates"][0]
    assert result["status"] == "RECONCILED_ZERO_QUARANTINE"
    assert result["dd5_mode"] == "CALCULATION_ONLY"
    assert row["tested"] is True
    assert row["source_surface_id"] == "surface-1"
    assert row["projected_pnl_dd5"] == 12.5


def test_final_shortlist_rejects_quarantine_and_provenance_mismatch(tmp_path: Path) -> None:
    manifest, inbox, database = _fixture(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("update import_runs set quarantined_count=1")
    with pytest.raises(ValueError, match="zero-quarantine"):
        build_v6_final_shortlist(manifest, inbox, database, "imp-1", "dd5-1")


def test_final_shortlist_marks_missing_dd5_as_untested(tmp_path: Path) -> None:
    manifest, inbox, database = _fixture(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("delete from dd5_results")
    result = build_v6_final_shortlist(manifest, inbox, database, "imp-1", "dd5-1")
    row = result["candidates"][0]
    assert row["tested"] is False
    assert row["dd5_run_id"] is None
    assert row["projected_pnl_dd5"] is None


def test_final_shortlist_rejects_provenance_and_dd5_link_mismatches(tmp_path: Path) -> None:
    manifest, inbox, database = _fixture(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["generation_manifest_sha256"] = "c" * 64
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="generation manifest"):
        build_v6_final_shortlist(manifest, inbox, database, "imp-1", "dd5-1")

    manifest, inbox, database = _fixture(tmp_path / "status")
    with duckdb.connect(str(database)) as connection:
        connection.execute("update dd5_runs set status='TICK_TEST'")
    with pytest.raises(ValueError, match="calculation-only"):
        build_v6_final_shortlist(manifest, inbox, database, "imp-1", "dd5-1")


@pytest.mark.parametrize(("field", "message"), [
    ("analysis_run_id", "analysis run"),
    ("strategy_json_sha256", "strategy JSON hashes"),
])
def test_final_shortlist_rejects_each_provenance_field_mismatch(
    tmp_path: Path, field: str, message: str
) -> None:
    manifest, inbox, database = _fixture(tmp_path)
    inbox_document = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    inbox_document["v6_provenance"][field] = "wrong" if field == "analysis_run_id" else {"A.json": "c" * 64}
    (inbox / "inbox_manifest.json").write_text(json.dumps(inbox_document), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        build_v6_final_shortlist(manifest, inbox, database, "imp-1", "dd5-1")


def test_final_shortlist_rejects_noncommitted_import_without_quarantine(tmp_path: Path) -> None:
    manifest, inbox, database = _fixture(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("update import_runs set status='RUNNING'")
    with pytest.raises(ValueError, match="zero-quarantine"):
        build_v6_final_shortlist(manifest, inbox, database, "imp-1", "dd5-1")
