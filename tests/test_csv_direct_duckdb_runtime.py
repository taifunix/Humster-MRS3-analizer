"""Published CSV Direct artifacts through the unchanged shortlist and READY JSON generator.

Optional: MRS3_REFERENCE_ANALYSIS_DB=<Source v6 *.analysis-v6.duckdb or .zip> checks
that the physical SQL schema matches a real Source v6 artifact (read-only).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

import duckdb
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.csv_direct_analysis import prepare_csv_direct_analysis, publish_csv_direct_analysis
from mrs3.csv_direct_input import load_csv_direct
from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies, read_fresh_analysis_identity
from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist
from tests.csv_direct_support import canonical_grid, write_tester_zip
from tests.test_fresh_analysis_strategies import _template

DATES = {"XYZUSDT": "2026-01-01"}
EXPECTED = {
    "base_one_order": [("scope_key", "VARCHAR"), ("payload_json", "VARCHAR")],
    "close_profiles": [("scope_key", "VARCHAR"), ("payload_json", "VARCHAR")],
    "manifest": [("key", "VARCHAR"), ("value", "VARCHAR")],
    "plateaus": [("scope_key", "VARCHAR"), ("payload_json", "VARCHAR")],
    "points": [("scope_key", "VARCHAR"), ("payload_json", "VARCHAR")],
    "refine_requests": [("scope_key", "VARCHAR"), ("payload_json", "VARCHAR")],
    "scope_runs": [("scope_key", "VARCHAR"), ("scope_digest", "VARCHAR"), ("result_digest", "VARCHAR")],
    "structure_diagnostics": [("scope_key", "VARCHAR"), ("payload_json", "VARCHAR")],
    "structures": [("scope_key", "VARCHAR"), ("payload_json", "VARCHAR")],
}


def _schema(path: Path) -> dict[str, list[tuple[str, str]]]:
    with duckdb.connect(str(path), read_only=True) as connection:
        return {
            table: [(str(item[1]), str(item[2])) for item in connection.execute(f"PRAGMA table_info('{table}')").fetchall()]
            for (table,) in connection.execute("SHOW TABLES").fetchall()
        }


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    root = tmp_path_factory.mktemp("csv-direct")
    source = load_csv_direct([write_tester_zip(root / "grid.zip", canonical_grid())], listing_dates=DATES)
    payload = prepare_csv_direct_analysis(source, AlgorithmConfig(), listing_dates=DATES)
    db = publish_csv_direct_analysis(payload, root / "analysis", filename="grid.analysis-v6.duckdb")
    return payload, db


def test_artifact_has_the_source_v6_physical_schema(published):
    payload, db = published
    assert _schema(db) == EXPECTED
    assert read_fresh_analysis_identity(db)["analysis_run_id"] == payload.analysis_id
    assert not list(db.parent.glob("*.surface-v6.duckdb")) and not list(db.parent.glob("*.source-v6.duckdb"))


def test_unchanged_shortlist_and_ready_json_generator(published, tmp_path):
    payload, db = published
    selection = evaluate_fresh_shortlist(prepare_fresh_shortlist(db, payload.analysis_id), (False, False, False), workers=1)
    assert selection.ready_candidate_ids
    assert {item.pretest_ab_status for item in selection.candidates} == {"DISABLED"}
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    result = generate_fresh_analysis_strategies(
        db, payload.analysis_id, selection.ready_candidate_ids, [("XYZUSDT", "LONG", "30m")],
        template, tmp_path / "ready", AlgorithmConfig(), selection=selection,
    )
    assert result.strategy_count == len(selection.ready_candidate_ids)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["analysis_id"] == payload.analysis_id
    assert manifest["candidate_identities"] == list(selection.ready_candidate_ids)


def test_enabled_ab_gate_is_refused_not_silently_skipped(published):
    payload, db = published
    prepared = prepare_fresh_shortlist(db, payload.analysis_id)
    with pytest.raises(ValueError, match="PRETEST_AB_EVIDENCE_UNAVAILABLE"):
        evaluate_fresh_shortlist(prepared, (True, False, False), workers=1)


def test_published_file_is_immutable(published):
    payload, db = published
    with pytest.raises(FileExistsError):
        publish_csv_direct_analysis(payload, db.parent, filename=db.name)
    with pytest.raises(ValueError, match="analysis-v6.duckdb"):
        publish_csv_direct_analysis(payload, db.parent, filename="other.duckdb")


def test_reference_schema_is_identical_if_provided():
    source = os.getenv("MRS3_REFERENCE_ANALYSIS_DB")
    if not source:
        pytest.skip("set MRS3_REFERENCE_ANALYSIS_DB")
    if Path(source).suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory(prefix="mrs3-reference-") as temp:
            members = [entry for entry in archive.namelist() if entry.lower().endswith(".duckdb")]
            assert len(members) == 1
            target = Path(temp) / "reference.duckdb"
            with archive.open(members[0]) as input_file, target.open("wb") as output:
                shutil.copyfileobj(input_file, output)
            assert _schema(target) == EXPECTED
    else:
        assert _schema(Path(source)) == EXPECTED
