from __future__ import annotations

from hashlib import sha256
import json

import duckdb


ANALYSIS_SCHEMA_VERSION = 1
EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT = (
    "f2a206838bdbe1483c11df88d6ebeb51f137fbc54f0c2a3d07453a6ffb364aa6"
)


class AnalysisSchemaError(ValueError):
    """Raised when an analysis DuckDB does not match this storage contract."""


_TABLES = {
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


def _table_names(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "select table_name from information_schema.tables where table_schema = 'main'"
        ).fetchall()
    }


def _schema_fingerprint(connection: duckdb.DuckDBPyConnection) -> str:
    columns = connection.execute(
        """select table_name, ordinal_position, column_name, data_type,
                  is_nullable, coalesce(column_default, '')
             from information_schema.columns
            where table_schema = 'main'
            order by table_name, ordinal_position"""
    ).fetchall()
    constraints = connection.execute(
        """select table_name, constraint_type, constraint_text,
                  coalesce(array_to_string(constraint_column_names, ','), ''),
                  coalesce(referenced_table, ''),
                  coalesce(array_to_string(referenced_column_names, ','), '')
             from duckdb_constraints()
            where schema_name = 'main'
            order by table_name, constraint_type, constraint_text"""
    ).fetchall()
    indexes = connection.execute(
        """select table_name, index_name, is_unique, is_primary,
                  coalesce(expressions, '')
             from duckdb_indexes()
            where schema_name = 'main'
            order by table_name, index_name"""
    ).fetchall()
    payload = json.dumps(
        {"columns": columns, "constraints": constraints, "indexes": indexes},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return sha256(payload).hexdigest()


def _verify_schema(connection: duckdb.DuckDBPyConnection) -> None:
    tables = _table_names(connection)
    if tables != _TABLES:
        raise AnalysisSchemaError("analysis database tables do not match the required schema")
    try:
        metadata = dict(connection.execute("select key, value from schema_info").fetchall())
    except duckdb.Error as error:
        raise AnalysisSchemaError("analysis database has no readable schema metadata") from error
    if metadata.get("schema_version") != str(ANALYSIS_SCHEMA_VERSION):
        raise AnalysisSchemaError(
            f"analysis database schema version {metadata.get('schema_version', 'missing')} "
            f"is not v{ANALYSIS_SCHEMA_VERSION}"
        )
    if metadata.get("schema_fingerprint") != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
        raise AnalysisSchemaError("analysis database stored schema fingerprint is not v1")
    if _schema_fingerprint(connection) != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
        raise AnalysisSchemaError("analysis database schema fingerprint does not match v1")


def _create_tables(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        create table schema_info(key varchar primary key, value varchar not null);
        create table surfaces(
            surface_id varchar primary key,
            parent_surface_id varchar references surfaces(surface_id),
            build_mode varchar not null,
            period_start_utc timestamp not null,
            period_end_utc timestamp not null,
            side varchar not null check(side in ('LONG', 'SHORT')),
            grid_contract_json varchar,
            normalization_contract_version varchar,
            materializer_version varchar,
            point_materialization_config_hash varchar,
            created_at_utc timestamp not null default current_timestamp,
            check(period_end_utc > period_start_utc)
        );
        create table surface_sources(
            surface_id varchar not null references surfaces(surface_id),
            source_database_id varchar,
            source_hash varchar not null check(length(source_hash) = 64),
            primary key(surface_id, source_hash)
        );
        create table surface_pairs(
            surface_id varchar not null references surfaces(surface_id),
            pair_key varchar not null,
            symbol varchar,
            side varchar check(side is null or side in ('LONG', 'SHORT')),
            shift_bp integer check(shift_bp is null or shift_bp >= 0),
            open_ma integer,
            close_ma integer,
            primary key(surface_id, pair_key)
        );
        create table surface_timeframes(
            surface_id varchar not null,
            pair_key varchar not null,
            timeframe varchar not null,
            coverage_status varchar,
            primary key(surface_id, pair_key, timeframe),
            foreign key(surface_id, pair_key) references surface_pairs(surface_id, pair_key)
        );
        create table surface_points(
            surface_id varchar not null,
            canonical_point_key varchar not null,
            pair_key varchar not null,
            timeframe varchar not null,
            point_event_count bigint not null check(point_event_count >= 0),
            source_report_id varchar not null,
            source_hash varchar not null check(length(source_hash) = 64),
            provenance_state varchar not null,
            metrics_json varchar not null,
            primary key(surface_id, canonical_point_key),
            foreign key(surface_id, pair_key, timeframe)
                references surface_timeframes(surface_id, pair_key, timeframe)
        );
        create table coverage_issues(
            issue_id varchar primary key,
            surface_id varchar not null references surfaces(surface_id),
            symbol varchar,
            timeframe varchar,
            issue_code varchar not null,
            detail_json varchar
        );
        create table dedup_decisions(
            decision_id varchar primary key,
            surface_id varchar not null references surfaces(surface_id),
            canonical_point_key varchar,
            decision varchar not null,
            detail_json varchar
        );
        create table analysis_runs(
            run_id varchar primary key,
            surface_id varchar not null references surfaces(surface_id),
            algorithm_version varchar not null,
            algorithm_config_json varchar not null,
            created_at_utc timestamp not null default current_timestamp,
            unique(surface_id, algorithm_version, algorithm_config_json),
            unique(run_id, surface_id)
        );
        create table plateaus(
            run_id varchar not null,
            plateau_id varchar not null,
            surface_id varchar not null,
            metrics_json varchar,
            primary key(run_id, plateau_id),
            unique(run_id, plateau_id, surface_id),
            foreign key(run_id, surface_id) references analysis_runs(run_id, surface_id)
        );
        create table plateau_members(
            run_id varchar not null,
            plateau_id varchar not null,
            surface_id varchar not null,
            canonical_point_key varchar not null,
            primary key(run_id, plateau_id, canonical_point_key),
            foreign key(run_id, plateau_id, surface_id)
                references plateaus(run_id, plateau_id, surface_id),
            foreign key(surface_id, canonical_point_key)
                references surface_points(surface_id, canonical_point_key)
        );
        create table candidates(
            candidate_id varchar primary key,
            run_id varchar not null,
            plateau_id varchar not null,
            surface_id varchar not null,
            candidate_json varchar,
            foreign key(run_id, plateau_id, surface_id)
                references plateaus(run_id, plateau_id, surface_id)
        );
        create table plateau_lineage(
            lineage_id varchar primary key,
            child_run_id varchar,
            child_plateau_id varchar,
            parent_run_id varchar,
            parent_plateau_id varchar,
            relation varchar not null check(relation in ('CONTINUED', 'SPLIT', 'MERGED', 'NEW', 'DROPPED')),
            detail_json varchar,
            foreign key(child_run_id, child_plateau_id) references plateaus(run_id, plateau_id),
            foreign key(parent_run_id, parent_plateau_id) references plateaus(run_id, plateau_id),
            check((child_run_id is null) = (child_plateau_id is null)),
            check((parent_run_id is null) = (parent_plateau_id is null)),
            check(
                (relation = 'NEW' and child_run_id is not null and parent_run_id is null)
                or (relation = 'DROPPED' and child_run_id is null and parent_run_id is not null)
                or (relation in ('CONTINUED', 'SPLIT', 'MERGED')
                    and child_run_id is not null and parent_run_id is not null)
            )
        );
        create index surface_points_by_surface on surface_points(surface_id);
        create index analysis_runs_by_surface on analysis_runs(surface_id);
        create index plateaus_by_run on plateaus(run_id);
        create index lineage_by_child on plateau_lineage(child_run_id, child_plateau_id);
        """
    )


def ensure_analysis_schema(connection: duckdb.DuckDBPyConnection) -> int:
    """Create an empty analysis schema or verify the existing versioned contract."""
    if _table_names(connection):
        _verify_schema(connection)
        return ANALYSIS_SCHEMA_VERSION

    connection.execute("begin transaction")
    try:
        _create_tables(connection)
        if _schema_fingerprint(connection) != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
            raise AnalysisSchemaError("analysis schema DDL does not match the code-owned v1 fingerprint")
        connection.executemany(
            "insert into schema_info(key, value) values (?, ?)",
            [
                ("schema_version", str(ANALYSIS_SCHEMA_VERSION)),
                ("schema_fingerprint", EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT),
            ],
        )
        connection.execute("commit")
    except BaseException:
        connection.execute("rollback")
        raise
    return ANALYSIS_SCHEMA_VERSION
