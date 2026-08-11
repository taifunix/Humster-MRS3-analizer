from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from mrs3.analysis_storage import ANALYSIS_SCHEMA_VERSION, AnalysisSchemaError, ensure_analysis_schema
from mrs3.duckdb_direct import (
    CoverageIssue,
    DirectBuildRequest,
    DirectPoint,
    DirectPreflight,
    DirectSurface,
)


REQUIRED_TABLES = {
    "schema_info",
    "surfaces",
    "surface_sources",
    "surface_pairs",
    "surface_timeframes",
    "surface_points",
    "coverage_issues",
    "dedup_decisions",
    "analysis_runs",
    "plateaus",
    "plateau_members",
    "candidates",
    "candidate_plateaus",
    "plateau_lineage",
    "analysis_run_facts",
}


def _create_v1_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Committed Task 10 v1 DDL, retained solely as a migration fixture."""
    connection.execute(
        """
        create table schema_info(key varchar primary key, value varchar not null);
        create table surfaces(surface_id varchar primary key, parent_surface_id varchar references surfaces(surface_id), build_mode varchar not null, period_start_utc timestamp not null, period_end_utc timestamp not null, side varchar not null check(side in ('LONG', 'SHORT')), grid_contract_json varchar, normalization_contract_version varchar, materializer_version varchar, point_materialization_config_hash varchar, created_at_utc timestamp not null default current_timestamp, check(period_end_utc > period_start_utc));
        create table surface_sources(surface_id varchar not null references surfaces(surface_id), source_database_id varchar, source_hash varchar not null check(length(source_hash) = 64), primary key(surface_id, source_hash));
        create table surface_pairs(surface_id varchar not null references surfaces(surface_id), pair_key varchar not null, symbol varchar, side varchar check(side is null or side in ('LONG', 'SHORT')), shift_bp integer check(shift_bp is null or shift_bp >= 0), open_ma integer, close_ma integer, primary key(surface_id, pair_key));
        create table surface_timeframes(surface_id varchar not null, pair_key varchar not null, timeframe varchar not null, coverage_status varchar, primary key(surface_id, pair_key, timeframe), foreign key(surface_id, pair_key) references surface_pairs(surface_id, pair_key));
        create table surface_points(surface_id varchar not null, canonical_point_key varchar not null, pair_key varchar not null, timeframe varchar not null, point_event_count bigint not null check(point_event_count >= 0), source_report_id varchar not null, source_hash varchar not null check(length(source_hash) = 64), provenance_state varchar not null, metrics_json varchar not null, primary key(surface_id, canonical_point_key), foreign key(surface_id, pair_key, timeframe) references surface_timeframes(surface_id, pair_key, timeframe));
        create table coverage_issues(issue_id varchar primary key, surface_id varchar not null references surfaces(surface_id), symbol varchar, timeframe varchar, issue_code varchar not null, detail_json varchar);
        create table dedup_decisions(decision_id varchar primary key, surface_id varchar not null references surfaces(surface_id), canonical_point_key varchar, decision varchar not null, detail_json varchar);
        create table analysis_runs(run_id varchar primary key, surface_id varchar not null references surfaces(surface_id), algorithm_version varchar not null, algorithm_config_json varchar not null, created_at_utc timestamp not null default current_timestamp, unique(surface_id, algorithm_version, algorithm_config_json), unique(run_id, surface_id));
        create table plateaus(run_id varchar not null, plateau_id varchar not null, surface_id varchar not null, metrics_json varchar, primary key(run_id, plateau_id), unique(run_id, plateau_id, surface_id), foreign key(run_id, surface_id) references analysis_runs(run_id, surface_id));
        create table plateau_members(run_id varchar not null, plateau_id varchar not null, surface_id varchar not null, canonical_point_key varchar not null, primary key(run_id, plateau_id, canonical_point_key), foreign key(run_id, plateau_id, surface_id) references plateaus(run_id, plateau_id, surface_id), foreign key(surface_id, canonical_point_key) references surface_points(surface_id, canonical_point_key));
        create table candidates(candidate_id varchar primary key, run_id varchar not null, plateau_id varchar not null, surface_id varchar not null, candidate_json varchar, foreign key(run_id, plateau_id, surface_id) references plateaus(run_id, plateau_id, surface_id));
        create table plateau_lineage(lineage_id varchar primary key, child_run_id varchar, child_plateau_id varchar, parent_run_id varchar, parent_plateau_id varchar, relation varchar not null check(relation in ('CONTINUED', 'SPLIT', 'MERGED', 'NEW', 'DROPPED')), detail_json varchar, foreign key(child_run_id, child_plateau_id) references plateaus(run_id, plateau_id), foreign key(parent_run_id, parent_plateau_id) references plateaus(run_id, plateau_id), check((child_run_id is null) = (child_plateau_id is null)), check((parent_run_id is null) = (parent_plateau_id is null)), check((relation = 'NEW' and child_run_id is not null and parent_run_id is null) or (relation = 'DROPPED' and child_run_id is null and parent_run_id is not null) or (relation in ('CONTINUED', 'SPLIT', 'MERGED') and child_run_id is not null and parent_run_id is not null)));
        create index surface_points_by_surface on surface_points(surface_id);
        create index analysis_runs_by_surface on analysis_runs(surface_id);
        create index plateaus_by_run on plateaus(run_id);
        create index lineage_by_child on plateau_lineage(child_run_id, child_plateau_id);
        """
    )


def _v1_with_one_candidate(connection: duckdb.DuckDBPyConnection) -> None:
    from mrs3 import analysis_storage

    _create_v1_schema(connection)
    connection.executemany("insert into schema_info values (?, ?)", [("schema_version", "1"), ("schema_fingerprint", analysis_storage._V1_FINGERPRINT)])
    connection.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    connection.execute("insert into surface_pairs(surface_id, pair_key) values ('S1', 'PAIR')")
    connection.execute("insert into surface_timeframes values ('S1', 'PAIR', '1h', 'COMPLETE')")
    connection.execute("insert into surface_points values ('S1', 'BTCUSDT|LONG|1h|100|3|9', 'PAIR', '1h', 7, 'report', ?, 'REPRODUCIBLE_AT_PUBLICATION', '{}')", ["a" * 64])
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    connection.execute("insert into plateaus(run_id, plateau_id, surface_id) values ('R1', 'P1', 'S1')")
    connection.execute("insert into plateau_members values ('R1', 'P1', 'S1', 'BTCUSDT|LONG|1h|100|3|9')")
    connection.execute("insert into candidates values ('C1', 'R1', 'P1', 'S1', '{\"legacy\":true}')")


@pytest.fixture
def connections(tmp_path: Path):
    source = duckdb.connect(str(tmp_path / "source.duckdb"))
    analysis = duckdb.connect(str(tmp_path / "analysis.duckdb"))
    try:
        yield source, analysis
    finally:
        source.close()
        analysis.close()


def _tables(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "select table_name from information_schema.tables where table_schema = 'main'"
        ).fetchall()
    }


def _surface(*, source_hash: str = "a" * 64) -> DirectSurface:
    request = DirectBuildRequest(
        "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z", "LONG", ("BTCUSDT",),
        (100,), "v1", "c" * 64,
    )
    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {}, (),
        {"kind": "OBSERVED_GRID_CONTRACT", "required_shifts_bp": (100,), "pairs": ("100|3|9",), "normalization_contract_version": "v1"},
        (source_hash,), (("report", source_hash),), ("BTCUSDT|LONG|1h|100|3|9",),
    )
    return DirectSurface(
        request, preflight, "legacy_trades_proxy",
        (DirectPoint("BTCUSDT|LONG|1h|100|3|9", "report", source_hash, 7, {"TotalTrades": 7, "TotalPnLPercent": 10.0, "MaxDrawdownPercent": 2.0, "Win": 5, "Los": 2, "WinRate": 71.4, "ProfitFactor": 2.0}),),
    )


def test_publish_surface_is_deterministic_deduplicated_and_persists_materialized_provenance(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    first = publish_surface(analysis, _surface())
    second = publish_surface(analysis, _surface())

    assert first.surface_id == second.surface_id
    assert second.created is False
    assert analysis.execute("select count(*) from surfaces").fetchone() == (1,)
    assert analysis.execute("select provenance_state from surface_points").fetchone() == ("REPRODUCIBLE_AT_PUBLICATION",)


def test_publish_analysis_run_is_deterministic_and_does_not_copy_surface_points(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    surface = publish_surface(analysis, _surface())
    result = PipelineResult(surface.surface_id, "0.6", {"minimum": 3}, pd.DataFrame(), pd.DataFrame())

    first = publish_analysis_run(analysis, result)
    second = publish_analysis_run(analysis, result)

    assert first.run_id == second.run_id
    assert not second.created
    assert analysis.execute("select count(*) from surface_points").fetchone() == (1,)
    assert analysis.execute("select count(*) from plateau_lineage").fetchone() == (0,)


def test_v3_stores_computed_run_facts_and_legacy_is_not_zero(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    surface = publish_surface(analysis, _surface())
    legacy = publish_analysis_run(analysis, PipelineResult(surface.surface_id, "v1", {}, pd.DataFrame(), pd.DataFrame()))
    assert analysis.execute("select facts_state, unique_point_count from analysis_run_facts where run_id=?", [legacy.run_id]).fetchone() == ("UNAVAILABLE_LEGACY", None)
    computed = publish_analysis_run(analysis, PipelineResult(surface.surface_id, "v2", {}, pd.DataFrame(), pd.DataFrame(), None, {"unique_point_count": 1, "economic_eligible_point_count": 1, "event_eligible_point_count": 1, "plateau_count": 0, "ready_candidate_count": 0}))
    assert analysis.execute("select facts_state, unique_point_count, economic_eligible_point_count from analysis_run_facts where run_id=?", [computed.run_id]).fetchone() == ("COMPUTED", 1, 1)
    with pytest.raises(ValueError, match="facts"):
        publish_analysis_run(analysis, replace(PipelineResult(surface.surface_id, "v2", {}, pd.DataFrame(), pd.DataFrame(), None, {"unique_point_count": 2, "economic_eligible_point_count": 1, "event_eligible_point_count": 1, "plateau_count": 0, "ready_candidate_count": 0})))


def test_library_filters_and_compare_keep_run_facts_separate(connections) -> None:
    from mrs3.analysis_storage import compare_analysis_runs, list_surface_library, publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    first = publish_surface(analysis, _surface())
    second = publish_surface(analysis, _surface(source_hash="b" * 64))
    facts = {"unique_point_count": 1, "economic_eligible_point_count": 1, "event_eligible_point_count": 1, "plateau_count": 0, "ready_candidate_count": 0}
    left = publish_analysis_run(analysis, PipelineResult(first.surface_id, "v1", {}, pd.DataFrame(), pd.DataFrame(), None, facts))
    right = publish_analysis_run(analysis, PipelineResult(second.surface_id, "v1", {}, pd.DataFrame(), pd.DataFrame(), None, {**facts, "economic_eligible_point_count": 0}))
    rows = list_surface_library(analysis, symbol="BTCUSDT", source_hash="b" * 64)
    assert [row["surface_id"] for row in rows] == [second.surface_id]
    assert rows[0]["runs"][0]["facts"].economic_eligible_point_count == 0
    before = analysis.execute("select count(*) from plateau_lineage").fetchone()
    comparison = compare_analysis_runs(analysis, left.run_id, right.run_id)
    assert comparison["left"]["facts"].economic_eligible_point_count == 1
    assert comparison["right"]["facts"].economic_eligible_point_count == 0
    assert analysis.execute("select count(*) from plateau_lineage").fetchone() == before


def test_repeated_run_publishes_each_explicit_lineage_comparison(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    parent_a = publish_surface(analysis, _surface(source_hash="a" * 64))
    parent_b = publish_surface(analysis, _surface(source_hash="b" * 64))
    child = publish_surface(analysis, _surface(source_hash="c" * 64))

    def plateau(identifier: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "plateau_id": identifier,
                    "all_point_ids": ("BTCUSDT|LONG|1h|100|3|9",),
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "timeframe": "1h",
                    "min_shift_bp": 100,
                    "max_shift_bp": 100,
                    "open_ma_min": 3,
                    "open_ma_max": 3,
                    "close_ma_min": 9,
                    "close_ma_max": 9,
                }
            ]
        )

    empty = pd.DataFrame()
    run_a = publish_analysis_run(
        analysis, PipelineResult(parent_a.surface_id, "v1", {"period": "A"}, plateau("PA"), empty)
    )
    run_b = publish_analysis_run(
        analysis, PipelineResult(parent_b.surface_id, "v1", {"period": "B"}, plateau("PB"), empty)
    )
    child_result = PipelineResult(
        child.surface_id, "v1", {"period": "C"}, plateau("PC"), empty, run_a.run_id
    )
    published = publish_analysis_run(analysis, child_result)
    repeated = publish_analysis_run(
        analysis, replace(child_result, comparison_run_id=run_b.run_id)
    )

    assert repeated.run_id == published.run_id
    assert not repeated.created
    assert {
        row[0]
        for row in analysis.execute(
            """select parent_run_id from plateau_lineage
                 where child_run_id=? and relation='CONTINUED'""",
            [published.run_id],
        ).fetchall()
    } == {run_a.run_id, run_b.run_id}


def test_publish_analysis_run_persists_2ord_and_4ord_candidates_through_junction(connections) -> None:
    from mrs3.analysis_storage import publish_analysis_run, publish_surface
    from mrs3.pipeline import PipelineResult

    _, analysis = connections
    surface = publish_surface(analysis, _surface())
    plateaus = pd.DataFrame([{
        "plateau_id": "P1", "all_point_ids": (surface.points[0].canonical_point_key,),
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
        "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9,
    }, {
        "plateau_id": "P2", "all_point_ids": (surface.points[0].canonical_point_key,),
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
        "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9,
    }, {
        "plateau_id": "P3", "all_point_ids": (surface.points[0].canonical_point_key,),
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
        "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9,
    }, {
        "plateau_id": "P4", "all_point_ids": (surface.points[0].canonical_point_key,),
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
        "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9,
    }])
    candidates = pd.DataFrame([{"structure_id": "C2", "plateau_ids": ("P1", "P2")}, {"structure_id": "C4", "plateau_ids": ("P1", "P2", "P3", "P4")}])

    publish_analysis_run(analysis, PipelineResult(surface.surface_id, "0.6", {}, plateaus, candidates))

    assert analysis.execute("select count(*) from candidates").fetchone() == (2,)
    assert analysis.execute("select count(*) from candidate_plateaus").fetchone() == (6,)
    assert analysis.execute("""select count(*) from candidate_plateaus cp
        join plateaus p using(run_id, plateau_id, surface_id)
        join plateau_members pm using(run_id, plateau_id, surface_id)
        join surface_points sp using(surface_id, canonical_point_key)
        join surface_sources ss using(surface_id, source_hash)""").fetchone() == (6,)


def test_lineage_classification_requires_common_points_and_geometry() -> None:
    from mrs3.analysis_storage import classify_plateau_lineage

    geometry = {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
                "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9}
    assert classify_plateau_lineage({"old": ({"point"}, geometry)}, {"new": ({"point"}, geometry)})[0][2] == "CONTINUED"
    shifted = {**geometry, "min_shift_bp": 200, "max_shift_bp": 200}
    assert classify_plateau_lineage({"old": ({"point"}, geometry)}, {"new": ({"point"}, shifted)})[0][2] == "NEW"


def test_lineage_classifies_split_merge_new_and_dropped_without_metrics() -> None:
    from mrs3.analysis_storage import classify_plateau_lineage

    geometry = {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "min_shift_bp": 100, "max_shift_bp": 100,
                "open_ma_min": 3, "open_ma_max": 3, "close_ma_min": 9, "close_ma_max": 9, "pnl_pct": 999}
    split = classify_plateau_lineage({"old": ({"a", "b"}, geometry)}, {"new-a": ({"a"}, geometry), "new-b": ({"b"}, geometry)})
    merged = classify_plateau_lineage({"old-a": ({"a"}, geometry), "old-b": ({"b"}, geometry)}, {"new": ({"a", "b"}, geometry)})
    isolated = classify_plateau_lineage({"dropped": ({"d"}, geometry)}, {"new": ({"n"}, geometry)})

    assert {row[2] for row in split} == {"SPLIT"}
    assert {row[2] for row in merged} == {"MERGED"}
    assert {(row[0], row[1], row[2]) for row in isolated} == {("new", None, "NEW"), (None, "dropped", "DROPPED")}
    assert all("pnl_pct" not in row[3] for row in (*split, *merged, *isolated))


def test_publish_surface_creates_immutable_child_and_rolls_back_invalid_materialized_input(connections) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    parent = publish_surface(analysis, _surface())
    same_period_child = publish_surface(analysis, _surface(source_hash="b" * 64))
    assert same_period_child.parent_surface_id == parent.surface_id
    parent_dedup = publish_surface(analysis, _surface())
    assert parent_dedup.created is False
    assert parent_dedup.parent_surface_id is None
    same_period_dedup = publish_surface(analysis, _surface(source_hash="b" * 64))
    assert same_period_dedup.created is False
    assert same_period_dedup.parent_surface_id == parent.surface_id
    child = publish_surface(analysis, replace(_surface(source_hash="c" * 64), parent_surface_id=parent.surface_id))
    assert child.parent_surface_id == parent.surface_id
    assert analysis.execute("select provenance_state from surface_points where surface_id=?", [child.surface_id]).fetchone() == ("REPRODUCIBLE_AT_PUBLICATION",)

    invalid = _surface(source_hash="not-a-hash")
    with pytest.raises(ValueError, match="source hash"):
        publish_surface(analysis, invalid)
    assert analysis.execute("select count(*) from surfaces").fetchone() == (3,)


@pytest.mark.parametrize(
    "alter",
    [
        lambda point: replace(point, metrics={"TotalTrades": 7, "TotalPnL": 1}),
        lambda point: replace(point, source_report_id="other-report"),
    ],
)
def test_dedup_rejects_facts_that_do_not_match_immutable_stored_points(connections, alter) -> None:
    from mrs3.analysis_storage import publish_surface

    _, analysis = connections
    publish_surface(analysis, _surface())
    incoming = _surface()
    changed = alter(incoming.points[0])
    tampered = replace(incoming, points=(changed,), preflight=replace(incoming.preflight, manifest=((changed.source_report_id, changed.source_hash),)))

    with pytest.raises(ValueError, match="immutable|incomplete"):
        publish_surface(analysis, tampered)


def test_bootstrap_creates_only_analysis_tables_in_a_distinct_database(connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]) -> None:
    source, analysis = connections
    source.execute("create table report_payloads(report_id varchar primary key, actions_zlib blob)")

    assert ensure_analysis_schema(analysis) == ANALYSIS_SCHEMA_VERSION
    assert _tables(analysis) == REQUIRED_TABLES
    assert _tables(source) == {"report_payloads"}
    assert not {"raw_payloads", "actions", "equity", "wallet", "report_payloads"}.intersection(
        _tables(analysis)
    )
    metadata = dict(analysis.execute("select key, value from schema_info").fetchall())
    assert metadata["schema_version"] == str(ANALYSIS_SCHEMA_VERSION)
    assert len(metadata["schema_fingerprint"]) == 64


def test_existing_schema_is_idempotent_but_other_versions_fail(connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]) -> None:
    _, analysis = connections
    assert ensure_analysis_schema(analysis) == ANALYSIS_SCHEMA_VERSION
    assert ensure_analysis_schema(analysis) == ANALYSIS_SCHEMA_VERSION

    analysis.execute("update schema_info set value = '999' where key = 'schema_version'")
    with pytest.raises(AnalysisSchemaError, match="version"):
        ensure_analysis_schema(analysis)


def test_v1_migration_preserves_candidate_link_and_published_surface_bytes() -> None:
    connection = duckdb.connect(":memory:")
    try:
        _v1_with_one_candidate(connection)
        before = connection.execute("select * from surfaces").fetchall(), connection.execute("select * from surface_points").fetchall()

        assert ensure_analysis_schema(connection) == ANALYSIS_SCHEMA_VERSION

        assert connection.execute("select candidate_id, run_id, surface_id, candidate_json from candidates").fetchall() == [("C1", "R1", "S1", '{"legacy":true}')]
        assert connection.execute("select candidate_id, run_id, plateau_id, surface_id from candidate_plateaus").fetchall() == [("C1", "R1", "P1", "S1")]
        assert connection.execute("select facts_state, unique_point_count, economic_eligible_point_count, event_eligible_point_count, plateau_count, ready_candidate_count, final_state from analysis_run_facts").fetchall() == [("UNAVAILABLE_LEGACY", None, None, None, None, None, "COMMITTED")]
        assert (connection.execute("select * from surfaces").fetchall(), connection.execute("select * from surface_points").fetchall()) == before
    finally:
        connection.close()


def test_v1_migration_rolls_back_on_injected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3 import analysis_storage

    connection = duckdb.connect(":memory:")
    try:
        _v1_with_one_candidate(connection)
        monkeypatch.setattr(analysis_storage, "EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT", "not-the-result")

        with pytest.raises(AnalysisSchemaError, match="migration DDL"):
            ensure_analysis_schema(connection)

        assert dict(connection.execute("select key, value from schema_info").fetchall())["schema_version"] == "1"
        assert "candidate_plateaus" not in _tables(connection)
        assert connection.execute("select candidate_id, plateau_id from candidates").fetchall() == [("C1", "P1")]
    finally:
        connection.close()


def test_read_only_library_rejects_legacy_schema_without_migrating(tmp_path: Path) -> None:
    from mrs3.analysis_storage import list_surface_library

    database = tmp_path / "legacy.duckdb"
    writable = duckdb.connect(str(database))
    _v1_with_one_candidate(writable)
    writable.close()

    read_only = duckdb.connect(str(database), read_only=True)
    try:
        with pytest.raises(AnalysisSchemaError):
            list_surface_library(read_only)
    finally:
        read_only.close()
    check = duckdb.connect(str(database), read_only=True)
    try:
        assert dict(check.execute("select key, value from schema_info").fetchall())["schema_version"] == "1"
    finally:
        check.close()


def test_claimed_v1_with_expected_table_names_but_wrong_schema_is_rejected(
    tmp_path: Path,
) -> None:
    from mrs3 import analysis_storage

    malformed = duckdb.connect(str(tmp_path / "malformed.duckdb"))
    try:
        malformed.execute("create table schema_info(key varchar, value varchar)")
        for table in sorted(REQUIRED_TABLES - {"schema_info"}):
            malformed.execute(f'create table "{table}"(wrong_column integer)')
        malformed.executemany(
            "insert into schema_info values (?, ?)",
            [
                ("schema_version", str(ANALYSIS_SCHEMA_VERSION)),
                ("schema_fingerprint", analysis_storage._schema_fingerprint(malformed)),
            ],
        )

        with pytest.raises(AnalysisSchemaError, match="fingerprint"):
            ensure_analysis_schema(malformed)
    finally:
        malformed.close()


def test_surface_and_run_constraints_preserve_required_lineage(connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    analysis.execute(
        "insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')"
    )
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute(
            "insert into surfaces(surface_id, parent_surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S2', 'missing', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')"
        )
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute(
            "insert into surface_sources(surface_id, source_hash) values ('missing', ?)", ["a" * 64]
        )
    analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R2', 'missing', 'v1', '{}')")


def test_unique_point_and_plateau_member_identities_are_enforced(connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    analysis.execute(
        "insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')"
    )
    analysis.execute("insert into surface_pairs(surface_id, pair_key) values ('S1', 'BTCUSDT|LONG|100|3|9')")
    analysis.execute("insert into surface_timeframes(surface_id, pair_key, timeframe) values ('S1', 'BTCUSDT|LONG|100|3|9', '15m')")
    analysis.execute("insert into surface_points(surface_id, canonical_point_key, pair_key, timeframe, point_event_count, source_report_id, source_hash, provenance_state, metrics_json) values ('S1', 'BTCUSDT|LONG|15m|100|3|9', 'BTCUSDT|LONG|100|3|9', '15m', 1, 'REPORT1', ?, 'REPRODUCIBLE', '{}')", ["a" * 64])
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into surface_points(surface_id, canonical_point_key, pair_key, timeframe, point_event_count, source_report_id, source_hash, provenance_state, metrics_json) values ('S1', 'BTCUSDT|LONG|15m|100|3|9', 'BTCUSDT|LONG|100|3|9', '15m', 1, 'REPORT1', ?, 'REPRODUCIBLE', '{}')", ["a" * 64])
    analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    analysis.execute("insert into plateaus(run_id, plateau_id, surface_id) values ('R1', 'P1', 'S1')")
    analysis.execute("insert into plateau_members(run_id, plateau_id, surface_id, canonical_point_key) values ('R1', 'P1', 'S1', 'BTCUSDT|LONG|15m|100|3|9')")
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into plateau_members(run_id, plateau_id, surface_id, canonical_point_key) values ('R1', 'P1', 'S1', 'BTCUSDT|LONG|15m|100|3|9')")


@pytest.mark.parametrize(
    "missing_field",
    ["source_report_id", "source_hash", "provenance_state", "metrics_json"],
)
def test_surface_points_require_final_provenance_and_metric_facts(
    connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection],
    missing_field: str,
) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    analysis.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    analysis.execute("insert into surface_pairs(surface_id, pair_key) values ('S1', 'PAIR')")
    analysis.execute("insert into surface_timeframes(surface_id, pair_key, timeframe) values ('S1', 'PAIR', '15m')")
    facts: dict[str, object] = {
        "source_report_id": "REPORT1",
        "source_hash": "a" * 64,
        "provenance_state": "REPRODUCIBLE",
        "metrics_json": "{}",
    }
    facts[missing_field] = None

    with pytest.raises(duckdb.ConstraintException):
        analysis.execute(
            """insert into surface_points(
                   surface_id, canonical_point_key, pair_key, timeframe,
                   point_event_count, source_report_id, source_hash,
                   provenance_state, metrics_json)
               values ('S1', 'POINT', 'PAIR', '15m', 1, ?, ?, ?, ?)""",
            list(facts.values()),
        )


def test_members_cannot_reference_a_point_from_another_run_surface(
    connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection],
) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    for surface_id in ("S1", "S2"):
        analysis.execute(
            "insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values (?, 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')",
            [surface_id],
        )
        analysis.execute(
            "insert into surface_pairs(surface_id, pair_key) values (?, 'PAIR')", [surface_id]
        )
        analysis.execute(
            "insert into surface_timeframes(surface_id, pair_key, timeframe) values (?, 'PAIR', '15m')",
            [surface_id],
        )
        analysis.execute(
            "insert into surface_points(surface_id, canonical_point_key, pair_key, timeframe, point_event_count, source_report_id, source_hash, provenance_state, metrics_json) values (?, 'POINT', 'PAIR', '15m', 1, ?, ?, 'REPRODUCIBLE', '{}')",
            [surface_id, f"REPORT-{surface_id}", (surface_id.lower() * 32)],
        )
    analysis.execute("insert into analysis_runs values ('R1', 'S1', 'v1', '{}', current_timestamp)")
    analysis.execute("insert into plateaus(run_id, plateau_id, surface_id) values ('R1', 'P1', 'S1')")

    with pytest.raises(duckdb.ConstraintException):
        analysis.execute(
            "insert into plateau_members values ('R1', 'P1', 'S2', 'POINT')"
        )


def test_candidates_require_a_valid_plateau(
    connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection],
) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    analysis.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    analysis.execute("insert into plateaus(run_id, plateau_id, surface_id) values ('R1', 'P1', 'S1')")

    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into candidates values ('C1', 'R1', 'missing', '{}')")
    analysis.execute("insert into candidates values ('C2', 'R1', 'S1', '{}')")
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into candidate_plateaus values ('C2', 'R1', 'missing', 'S1')")
    analysis.execute("insert into candidate_plateaus values ('C2', 'R1', 'P1', 'S1')")


def test_lineage_endpoint_constraints_model_new_dropped_and_cross_run_relations(
    connections: tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection],
) -> None:
    _, analysis = connections
    ensure_analysis_schema(analysis)
    for surface_id, run_id, plateau_id in (("S1", "R1", "P1"), ("S2", "R2", "P2")):
        analysis.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values (?, 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')", [surface_id])
        analysis.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values (?, ?, 'v1', '{}')", [run_id, surface_id])
        analysis.execute("insert into plateaus(run_id, plateau_id, surface_id) values (?, ?, ?)", [run_id, plateau_id, surface_id])

    analysis.execute("insert into plateau_lineage(lineage_id, child_run_id, child_plateau_id, relation) values ('L1', 'R2', 'P2', 'NEW')")
    analysis.execute("insert into plateau_lineage(lineage_id, parent_run_id, parent_plateau_id, relation) values ('L2', 'R1', 'P1', 'DROPPED')")
    analysis.execute("insert into plateau_lineage values ('L3', 'R2', 'P2', 'R1', 'P1', 'CONTINUED', null)")

    invalid = (
        "insert into plateau_lineage values ('X1', 'R2', 'P2', 'R1', 'P1', 'NEW', null)",
        "insert into plateau_lineage values ('X2', 'R2', 'P2', 'R1', 'P1', 'DROPPED', null)",
        "insert into plateau_lineage(lineage_id, child_run_id, child_plateau_id, relation) values ('X3', 'R2', 'P2', 'MERGED')",
        "insert into plateau_lineage(lineage_id, child_run_id, relation) values ('X4', 'R2', 'NEW')",
    )
    for statement in invalid:
        with pytest.raises(duckdb.ConstraintException):
            analysis.execute(statement)


def test_bootstrap_rolls_back_on_creation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3 import analysis_storage

    analysis = duckdb.connect(str(tmp_path / "analysis.duckdb"))
    try:
        def fail_after_create(connection: duckdb.DuckDBPyConnection) -> None:
            connection.execute("create table transient_table(id integer)")
            raise duckdb.TransactionException("injected failure")

        monkeypatch.setattr(analysis_storage, "_create_tables", fail_after_create)
        with pytest.raises(duckdb.TransactionException, match="injected failure"):
            ensure_analysis_schema(analysis)
        assert _tables(analysis) == set()
    finally:
        analysis.close()
