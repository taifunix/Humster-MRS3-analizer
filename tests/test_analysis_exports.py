from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from mrs3.analysis_storage import ensure_analysis_schema


def _analysis_with_run() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    connection.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    connection.execute("insert into surface_sources(surface_id, source_database_id, source_hash) values ('S1', 'source', ?)", ["a" * 64])
    connection.execute("insert into surface_pairs values ('S1', 'PAIR', 'BTCUSDT', 'LONG', 100, 3, 9)")
    connection.execute("insert into surface_timeframes values ('S1', 'PAIR', '1h', 'USABLE')")
    connection.execute("insert into surface_points values ('S1', 'BTCUSDT|LONG|1h|100|3|9', 'PAIR', '1h', 7, 'report', ?, 'REPRODUCIBLE_AT_PUBLICATION', '{\"TotalTrades\":7}')", ["a" * 64])
    connection.execute("insert into coverage_issues values ('I1', 'S1', 'ETHUSDT', '1h', 'MISSING', '{\"detail\":\"absent\"}')")
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{\"minimum\":3}')")
    if connection.execute("select 1 from information_schema.tables where table_schema='main' and table_name='analysis_run_facts'").fetchone():
        connection.execute(
            """insert into analysis_run_facts(
                   run_id, facts_state, unique_point_count,
                   economic_eligible_point_count, event_eligible_point_count,
                   plateau_count, ready_candidate_count, final_state)
               values ('R1', 'COMPUTED', 1, 1, 1, 1, 1, 'COMMITTED')"""
        )
    connection.execute("insert into plateaus values ('R1', 'P1', 'S1', '{\"symbol\":\"BTCUSDT\"}')")
    connection.execute("insert into plateau_members values ('R1', 'P1', 'S1', 'BTCUSDT|LONG|1h|100|3|9')")
    connection.execute("insert into candidates values ('C1', 'R1', 'S1', '{\"plateau_ids\":[\"P1\",\"P2\"]}')")
    connection.execute("insert into candidate_plateaus values ('C1', 'R1', 'P1', 'S1')")
    connection.execute("insert into plateau_lineage values ('L1', 'R1', 'P1', null, null, 'NEW', '{\"comparison_run_id\":\"OLD\"}')")
    return connection


def _tree_bytes(directory: Path) -> dict[str, bytes]:
    return {path.relative_to(directory).as_posix(): path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()}


def test_export_is_byte_stable_and_manifest_hashes_match_files(tmp_path: Path) -> None:
    from mrs3.analysis_exports import export_analysis_run

    connection = _analysis_with_run()
    try:
        first = tmp_path / "first"
        second = tmp_path / "second"
        result = export_analysis_run(connection, "R1", first)
        export_analysis_run(connection, "R1", second)

        assert _tree_bytes(first) == _tree_bytes(second)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_id"] == "R1"
        assert manifest["surface_id"] == "S1"
        assert manifest["row_counts"]["surface_points"] == 1
        if "analysis_run_facts" in manifest["row_counts"]:
            assert manifest["facts_state"] == "COMPUTED"
            assert manifest["counts"]["unique_point_count"] == 1
        for filename, digest in manifest["sha256"].items():
            assert sha256((first / filename).read_bytes()).hexdigest() == digest
    finally:
        connection.close()


def test_export_reads_only_the_supplied_analysis_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3.analysis_exports import export_analysis_run

    connection = _analysis_with_run()
    before = connection.execute("select * from surface_points").fetchall()
    monkeypatch.setattr(duckdb, "connect", lambda *_args, **_kwargs: pytest.fail("must not open a database"))
    try:
        export_analysis_run(connection, "R1", tmp_path / "export")
        assert connection.execute("select * from surface_points").fetchall() == before
    finally:
        connection.close()


def test_export_rejects_unknown_run_and_nonempty_target_without_publication(tmp_path: Path) -> None:
    from mrs3.analysis_exports import export_analysis_run

    connection = _analysis_with_run()
    try:
        target = tmp_path / "export"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with pytest.raises(ValueError, match="unknown analysis run"):
            export_analysis_run(connection, "missing", tmp_path / "missing")
        with pytest.raises(ValueError, match="nonempty"):
            export_analysis_run(connection, "R1", target)
        assert marker.read_text(encoding="utf-8") == "keep"
    finally:
        connection.close()


def test_export_failure_is_atomic_and_leaves_no_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3.analysis_exports import export_analysis_run

    connection = _analysis_with_run()
    try:
        def fail(*_args: object, **_kwargs: object) -> None:
            raise OSError("injected csv failure")

        monkeypatch.setattr(pd.DataFrame, "to_csv", fail)
        target = tmp_path / "export"
        with pytest.raises(OSError, match="injected"):
            export_analysis_run(connection, "R1", target)
        assert not target.exists()
    finally:
        connection.close()


def test_export_fails_closed_for_invalid_stored_json_and_path_traversal(tmp_path: Path) -> None:
    from mrs3.analysis_exports import export_analysis_run

    connection = _analysis_with_run()
    try:
        with pytest.raises(ValueError, match="path traversal"):
            export_analysis_run(connection, "R1", tmp_path / ".." / "escape")
        connection.execute("update plateaus set metrics_json='not-json' where run_id='R1'")
        target = tmp_path / "export"
        with pytest.raises(ValueError, match="invalid stored JSON"):
            export_analysis_run(connection, "R1", target)
        assert not target.exists()
    finally:
        connection.close()
