from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from mrs3.analysis_storage import ANALYSIS_SCHEMA_VERSION, AnalysisSchemaError, ensure_analysis_schema


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
    "plateau_lineage",
}


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
        analysis.execute("insert into candidates(candidate_id, run_id, surface_id, candidate_json) values ('C0', 'R1', 'S1', '{}')")
    with pytest.raises(duckdb.ConstraintException):
        analysis.execute("insert into candidates values ('C1', 'R1', 'missing', 'S1', '{}')")
    analysis.execute("insert into candidates values ('C2', 'R1', 'P1', 'S1', '{}')")


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
