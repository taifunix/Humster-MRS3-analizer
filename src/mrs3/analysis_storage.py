from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
from typing import TYPE_CHECKING, Mapping

import duckdb

if TYPE_CHECKING:
    from .duckdb_direct import DirectPoint, DirectSurface


ANALYSIS_SCHEMA_VERSION = 3
EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT = (
    "58a445394e54f43f95cce56d2019ae4fb97040ccae83769debe205aa93987db8"
)
_V2_FINGERPRINT = "a61bf184df6377f5161e13ba4e542bb36b669c528af360ddbb9b62d666f6adba"
_V1_FINGERPRINT = "f2a206838bdbe1483c11df88d6ebeb51f137fbc54f0c2a3d07453a6ffb364aa6"
_DIRECT_REQUIRED_METRICS = {
    "TotalPnLPercent",
    "MaxDrawdownPercent",
    "TotalTrades",
    "Win",
    "Los",
    "WinRate",
    "ProfitFactor",
}


class AnalysisSchemaError(ValueError):
    """Raised when an analysis DuckDB does not match this storage contract."""


@dataclass(frozen=True, slots=True)
class PublishedSurface:
    surface_id: str
    parent_surface_id: str | None
    created: bool
    points: tuple[DirectPoint, ...]


@dataclass(frozen=True, slots=True)
class PublishedAnalysisRun:
    run_id: str
    surface_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class AnalysisRunFacts:
    facts_state: str
    unique_point_count: int | None
    economic_eligible_point_count: int | None
    event_eligible_point_count: int | None
    plateau_count: int | None
    ready_candidate_count: int | None
    final_state: str


def _facts(row: tuple[object, ...]) -> AnalysisRunFacts:
    return AnalysisRunFacts(str(row[0]), *(None if value is None else int(value) for value in row[1:6]), str(row[6]))


def list_surface_library(
    connection: duckdb.DuckDBPyConnection, *, side: str | None = None,
    period_start_utc: str | None = None, period_end_utc: str | None = None,
    symbol: str | None = None, build_mode: str | None = None,
    parent_surface_id: str | None = None, source_hash: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Read-only library rows; each run retains its own facts and metrics."""
    verify_analysis_schema(connection)
    clauses, values = ["1=1"], []
    for column, value in (("s.side", side), ("s.build_mode", build_mode), ("s.parent_surface_id", parent_surface_id)):
        if value is not None: clauses.append(f"{column}=?"); values.append(value)
    if period_start_utc is not None: clauses.append("s.period_start_utc>=?"); values.append(period_start_utc)
    if period_end_utc is not None: clauses.append("s.period_end_utc<=?"); values.append(period_end_utc)
    if symbol is not None: clauses.append("exists(select 1 from surface_pairs sp where sp.surface_id=s.surface_id and sp.symbol=?)"); values.append(symbol)
    if source_hash is not None: clauses.append("exists(select 1 from surface_sources ss where ss.surface_id=s.surface_id and ss.source_hash=?)"); values.append(source_hash)
    rows = connection.execute(f"select s.surface_id,s.parent_surface_id,s.period_start_utc,s.period_end_utc,s.side,s.build_mode from surfaces s where {' and '.join(clauses)} order by s.period_start_utc,s.surface_id", values).fetchall()
    result = []
    for surface_id, parent, start, end, row_side, mode in rows:
        runs = []
        for run_id, state, unique, economic, event, plateaus, ready, final in connection.execute("""select r.run_id,f.facts_state,f.unique_point_count,f.economic_eligible_point_count,f.event_eligible_point_count,f.plateau_count,f.ready_candidate_count,f.final_state from analysis_runs r join analysis_run_facts f using(run_id) where r.surface_id=? order by r.run_id""", [surface_id]).fetchall():
            runs.append({"run_id": str(run_id), "facts": _facts((state, unique, economic, event, plateaus, ready, final))})
        result.append({"surface_id": str(surface_id), "parent_surface_id": parent, "period_start_utc": str(start), "period_end_utc": str(end), "side": str(row_side), "build_mode": str(mode), "unique_point_count": int(connection.execute("select count(*) from surface_points where surface_id=?", [surface_id]).fetchone()[0]), "source_hashes": tuple(row[0] for row in connection.execute("select source_hash from surface_sources where surface_id=? order by source_hash", [surface_id]).fetchall()), "coverage_reasons": tuple(row[0] for row in connection.execute("select issue_code from coverage_issues where surface_id=? order by issue_code,issue_id", [surface_id]).fetchall()), "runs": tuple(runs)})
    return tuple(result)


def compare_analysis_runs(connection: duckdb.DuckDBPyConnection, left_run_id: str, right_run_id: str) -> dict[str, object]:
    """Read two published runs without lineage writes or metric aggregation."""
    def one(run_id: str) -> dict[str, object]:
        row = connection.execute("""select r.run_id,r.surface_id,s.parent_surface_id,s.period_start_utc,s.period_end_utc,s.side,f.facts_state,f.unique_point_count,f.economic_eligible_point_count,f.event_eligible_point_count,f.plateau_count,f.ready_candidate_count,f.final_state from analysis_runs r join surfaces s using(surface_id) join analysis_run_facts f using(run_id) where r.run_id=?""", [run_id]).fetchone()
        if row is None: raise ValueError("unknown analysis run")
        return {"run_id": str(row[0]), "surface_id": str(row[1]), "parent_surface_id": row[2], "period_start_utc": str(row[3]), "period_end_utc": str(row[4]), "side": str(row[5]), "facts": _facts(tuple(row[6:]))}
    verify_analysis_schema(connection)
    return {"left": one(left_run_id), "right": one(right_run_id)}


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
    "candidate_plateaus",
    "plateau_lineage",
    "analysis_run_facts",
}
_V1_TABLES = _TABLES - {"candidate_plateaus", "analysis_run_facts"}


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
    try:
        metadata = dict(connection.execute("select key, value from schema_info").fetchall())
    except duckdb.Error as error:
        raise AnalysisSchemaError("analysis database has no readable schema metadata") from error
    if metadata.get("schema_version") == "1" and metadata.get("schema_fingerprint") == _V1_FINGERPRINT:
        if tables != _V1_TABLES or _schema_fingerprint(connection) != _V1_FINGERPRINT:
            raise AnalysisSchemaError("analysis database schema fingerprint does not match v1")
        connection.execute("begin transaction")
        try:
            _migrate_v1_to_v2(connection, transactional=False)
            _migrate_v2_to_v3(connection, transactional=False)
            connection.execute("commit")
        except BaseException:
            connection.execute("rollback")
            raise
        return
    if metadata.get("schema_version") == "2" and metadata.get("schema_fingerprint") == _V2_FINGERPRINT:
        if tables != (_TABLES - {"analysis_run_facts"}) or _schema_fingerprint(connection) != _V2_FINGERPRINT:
            raise AnalysisSchemaError("analysis database schema fingerprint does not match v2")
        _migrate_v2_to_v3(connection)
        return
    _verify_current_schema(connection, tables, metadata)


def _verify_current_schema(
    connection: duckdb.DuckDBPyConnection,
    tables: set[str] | None = None,
    metadata: Mapping[str, str] | None = None,
) -> None:
    tables = _table_names(connection) if tables is None else tables
    if metadata is None:
        try:
            metadata = dict(connection.execute("select key, value from schema_info").fetchall())
        except duckdb.Error as error:
            raise AnalysisSchemaError("analysis database has no readable schema metadata") from error
    if tables != _TABLES:
        raise AnalysisSchemaError("analysis database tables do not match the required schema")
    if metadata.get("schema_version") != str(ANALYSIS_SCHEMA_VERSION):
        raise AnalysisSchemaError(
            f"analysis database schema version {metadata.get('schema_version', 'missing')} "
            f"is not v{ANALYSIS_SCHEMA_VERSION}"
        )
    if metadata.get("schema_fingerprint") != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
        raise AnalysisSchemaError("analysis database stored schema fingerprint is not v3")
    if _schema_fingerprint(connection) != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
        raise AnalysisSchemaError("analysis database schema fingerprint does not match v3")


def verify_analysis_schema(connection: duckdb.DuckDBPyConnection) -> int:
    """Verify the current v3 contract without attempting any migration."""
    _verify_current_schema(connection)
    return ANALYSIS_SCHEMA_VERSION


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
        create table analysis_run_facts(
            run_id varchar primary key references analysis_runs(run_id),
            facts_state varchar not null check(facts_state in ('COMPUTED', 'UNAVAILABLE_LEGACY')),
            unique_point_count bigint check(unique_point_count is null or unique_point_count >= 0),
            economic_eligible_point_count bigint check(economic_eligible_point_count is null or economic_eligible_point_count >= 0),
            event_eligible_point_count bigint check(event_eligible_point_count is null or event_eligible_point_count >= 0),
            plateau_count bigint check(plateau_count is null or plateau_count >= 0),
            ready_candidate_count bigint check(ready_candidate_count is null or ready_candidate_count >= 0),
            final_state varchar not null check(final_state = 'COMMITTED'),
            check((facts_state = 'COMPUTED' and unique_point_count is not null and economic_eligible_point_count is not null and event_eligible_point_count is not null and plateau_count is not null and ready_candidate_count is not null) or (facts_state = 'UNAVAILABLE_LEGACY' and unique_point_count is null and economic_eligible_point_count is null and event_eligible_point_count is null and plateau_count is null and ready_candidate_count is null))
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
            surface_id varchar not null,
            candidate_json varchar,
            unique(candidate_id, run_id, surface_id),
            foreign key(run_id, surface_id) references analysis_runs(run_id, surface_id)
        );
        create table candidate_plateaus(
            candidate_id varchar not null,
            run_id varchar not null,
            plateau_id varchar not null,
            surface_id varchar not null,
            primary key(candidate_id, plateau_id),
            foreign key(candidate_id, run_id, surface_id)
                references candidates(candidate_id, run_id, surface_id),
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
        create index candidate_plateaus_by_run on candidate_plateaus(run_id, plateau_id);
        create index lineage_by_child on plateau_lineage(child_run_id, child_plateau_id);
        """
    )


def _migrate_v1_to_v2(connection: duckdb.DuckDBPyConnection, *, transactional: bool = True) -> None:
    """Upgrade candidate ownership to a junction without touching published facts."""
    if transactional:
        connection.execute("begin transaction")
    try:
        legacy = connection.execute(
            "select candidate_id, run_id, plateau_id, surface_id, candidate_json from candidates"
        ).fetchall()
        connection.execute("alter table candidates rename to candidates_v1")
        connection.execute(
            """create table candidates(
                candidate_id varchar primary key, run_id varchar not null, surface_id varchar not null,
                candidate_json varchar, unique(candidate_id, run_id, surface_id),
                foreign key(run_id, surface_id) references analysis_runs(run_id, surface_id))"""
        )
        connection.execute(
            """create table candidate_plateaus(
                candidate_id varchar not null, run_id varchar not null, plateau_id varchar not null,
                surface_id varchar not null, primary key(candidate_id, plateau_id),
                foreign key(candidate_id, run_id, surface_id) references candidates(candidate_id, run_id, surface_id),
                foreign key(run_id, plateau_id, surface_id) references plateaus(run_id, plateau_id, surface_id))"""
        )
        if legacy:
            connection.executemany("insert into candidates values (?,?,?,?)", [(a,b,d,e) for a,b,c,d,e in legacy])
            connection.executemany("insert into candidate_plateaus values (?,?,?,?)", [(a,b,c,d) for a,b,c,d,e in legacy])
        connection.execute("drop table candidates_v1")
        connection.execute("create index candidate_plateaus_by_run on candidate_plateaus(run_id, plateau_id)")
        if _schema_fingerprint(connection) != _V2_FINGERPRINT:
            raise AnalysisSchemaError("v1 migration DDL does not match the code-owned v2 fingerprint")
        connection.execute("update schema_info set value=? where key='schema_version'", ["2"])
        connection.execute("update schema_info set value=? where key='schema_fingerprint'", [_V2_FINGERPRINT])
        if transactional:
            connection.execute("commit")
    except BaseException:
        if transactional:
            connection.execute("rollback")
        raise


def _migrate_v2_to_v3(connection: duckdb.DuckDBPyConnection, *, transactional: bool = True) -> None:
    """Add immutable run facts; old runs remain coherently unavailable."""
    if transactional:
        connection.execute("begin transaction")
    try:
        connection.execute("""create table analysis_run_facts(
            run_id varchar primary key references analysis_runs(run_id),
            facts_state varchar not null check(facts_state in ('COMPUTED', 'UNAVAILABLE_LEGACY')),
            unique_point_count bigint check(unique_point_count is null or unique_point_count >= 0),
            economic_eligible_point_count bigint check(economic_eligible_point_count is null or economic_eligible_point_count >= 0),
            event_eligible_point_count bigint check(event_eligible_point_count is null or event_eligible_point_count >= 0),
            plateau_count bigint check(plateau_count is null or plateau_count >= 0),
            ready_candidate_count bigint check(ready_candidate_count is null or ready_candidate_count >= 0),
            final_state varchar not null check(final_state = 'COMMITTED'),
            check((facts_state = 'COMPUTED' and unique_point_count is not null and economic_eligible_point_count is not null and event_eligible_point_count is not null and plateau_count is not null and ready_candidate_count is not null) or (facts_state = 'UNAVAILABLE_LEGACY' and unique_point_count is null and economic_eligible_point_count is null and event_eligible_point_count is null and plateau_count is null and ready_candidate_count is null))
        )""")
        connection.execute("insert into analysis_run_facts(run_id,facts_state,final_state) select run_id,'UNAVAILABLE_LEGACY','COMMITTED' from analysis_runs")
        if _schema_fingerprint(connection) != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
            raise AnalysisSchemaError("v2 migration DDL does not match the code-owned v3 fingerprint")
        connection.execute("update schema_info set value=? where key='schema_version'", [str(ANALYSIS_SCHEMA_VERSION)])
        connection.execute("update schema_info set value=? where key='schema_fingerprint'", [EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT])
        if transactional:
            connection.execute("commit")
    except BaseException:
        if transactional:
            connection.execute("rollback")
        raise


def ensure_analysis_schema(connection: duckdb.DuckDBPyConnection) -> int:
    """Create an empty analysis schema or verify the existing versioned contract."""
    if _table_names(connection):
        _verify_schema(connection)
        return ANALYSIS_SCHEMA_VERSION

    connection.execute("begin transaction")
    try:
        _create_tables(connection)
        if _schema_fingerprint(connection) != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
            raise AnalysisSchemaError("analysis schema DDL does not match the code-owned v3 fingerprint")
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


def _canonical_json(value: object) -> str:
    def normalize(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): normalize(value) for key, value in item.items()}
        if isinstance(item, (tuple, list)):
            return [normalize(value) for value in item]
        if isinstance(item, set):
            return [normalize(value) for value in sorted(item, key=str)]
        if isinstance(item, datetime):
            moment = item.replace(tzinfo=timezone.utc) if item.tzinfo is None else item.astimezone(timezone.utc)
            return moment.isoformat().replace("+00:00", "Z")
        if isinstance(item, Decimal):
            return str(item)
        if hasattr(item, "item") and callable(item.item):
            return normalize(item.item())
        return item

    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _analysis_json(value: object) -> str:
    def clean(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): clean(value) for key, value in item.items()}
        if isinstance(item, (tuple, list, set)):
            return [clean(value) for value in item]
        if hasattr(item, "item") and callable(item.item):
            return clean(item.item())
        if isinstance(item, float) and not math.isfinite(item):
            return None
        return item

    return _canonical_json(clean(value))


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("surface period must be UTC-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _surface_identity(surface: DirectSurface) -> tuple[str, dict[str, object]]:
    request, preflight = surface.request, surface.preflight
    start, end = _utc(request.start_utc), _utc(request.end_utc)
    identity = {
        "build_mode": surface.build_mode,
        "period": [start, end],
        "side": request.side,
        "selected_symbols": sorted(request.symbols),
        "selected_timeframes": {
            symbol: sorted(timeframes) for symbol, timeframes in sorted(preflight.usable_timeframes.items())
        },
        "source_hashes": sorted(preflight.source_hashes),
        "grid_contract": json.loads(_canonical_json(preflight.grid_contract)),
        "normalization_contract": preflight.grid_contract.get("normalization_contract_version"),
        "materializer_version": request.materializer_version,
        "point_materialization_config_hash": request.point_materialization_config_hash,
    }
    return sha256(_canonical_json(identity).encode("ascii")).hexdigest(), identity


def _point_parts(point: DirectPoint) -> tuple[str, str, str, int, int, int]:
    parts = point.canonical_point_key.split("|")
    if len(parts) != 6:
        raise ValueError("canonical point key must have six fields")
    symbol, side, timeframe, shift, open_ma, close_ma = parts
    try:
        return symbol, side, timeframe, int(shift), int(open_ma), int(close_ma)
    except ValueError as error:
        raise ValueError("canonical point key has non-integer grid fields") from error


def _validate_direct_metrics(metrics: Mapping[str, object], event_count: int) -> None:
    if _DIRECT_REQUIRED_METRICS.difference(metrics):
        raise ValueError("point metrics are incomplete")

    def number(name: str) -> Decimal:
        try:
            value = Decimal(str(metrics[name]))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError(f"point metric {name} must be finite") from error
        if not value.is_finite():
            raise ValueError(f"point metric {name} must be finite")
        return value

    for name in ("TotalPnLPercent", "MaxDrawdownPercent", "WinRate"):
        number(name)
    for name in ("TotalTrades", "Win", "Los"):
        value = number(name)
        if value < 0 or value != value.to_integral_value():
            raise ValueError(f"point metric {name} must be a non-negative integer")
    if int(number("TotalTrades")) != event_count:
        raise ValueError("point event count must equal materialized TotalTrades")
    if metrics["ProfitFactor"] is not None:
        number("ProfitFactor")


def _validate_surface(surface: DirectSurface, identity: dict[str, object]) -> tuple[DirectPoint, ...]:
    if surface.build_mode != "DUCKDB_DIRECT" or surface.event_mode != "legacy_trades_proxy":
        raise ValueError("direct surface must use legacy_trades_proxy")
    if identity["side"] not in {"LONG", "SHORT"} or identity["period"][0] >= identity["period"][1]:
        raise ValueError("surface identity is invalid")
    config_hash = str(identity["point_materialization_config_hash"])
    if len(config_hash) != 64 or any(char not in "0123456789abcdef" for char in config_hash):
        raise ValueError("point materialization config hash must be a SHA-256 digest")
    if not surface.points:
        raise ValueError("surface has no materialized points")
    manifest = tuple(surface.preflight.manifest)
    source_hashes = tuple(identity["source_hashes"])
    if len(set(manifest)) != len(manifest):
        raise ValueError("source manifest must be unique")
    if len(set(source_hashes)) != len(source_hashes) or set(source_hash for _, source_hash in manifest) != set(source_hashes):
        raise ValueError("source manifest hashes must match source hashes")
    points = tuple(sorted(surface.points, key=lambda point: point.canonical_point_key))
    if len({point.canonical_point_key for point in points}) != len(points):
        raise ValueError("canonical point uniqueness failed")
    if tuple(point.canonical_point_key for point in points) != tuple(sorted(surface.preflight.accepted_point_keys)):
        raise ValueError("accepted point keys do not match materialized points")
    if set(manifest) != {(point.source_report_id, point.source_hash) for point in points} or set(source_hashes) != {point.source_hash for point in points}:
        raise ValueError("materialized manifest does not match point provenance")
    usable = surface.preflight.usable_timeframes
    grid_pairs = set(surface.preflight.grid_contract.get("pairs", ()))
    for point in points:
        symbol, side, timeframe, shift, open_ma, close_ma = _point_parts(point)
        if symbol not in surface.request.symbols or side != surface.request.side or timeframe not in usable.get(symbol, ()):
            raise ValueError("point is outside the selected surface scope")
        if shift not in surface.request.required_shifts_bp or f"{shift}|{open_ma}|{close_ma}" not in grid_pairs:
            raise ValueError("point is outside the immutable grid contract")
        if len(point.source_hash) != 64 or any(char not in "0123456789abcdef" for char in point.source_hash):
            raise ValueError("source hash must be a lowercase SHA-256 digest")
        if (point.source_report_id, point.source_hash) not in manifest or point.source_hash not in source_hashes:
            raise ValueError("point provenance is absent from materialized preflight")
        if point.point_event_count < 0:
            raise ValueError("point event count must be non-negative")
        _validate_direct_metrics(point.metrics, point.point_event_count)
        try:
            _canonical_json(point.metrics)
        except (TypeError, ValueError) as error:
            raise ValueError("point metrics must be finite canonical JSON") from error
    return points


def _surface_scope(connection: duckdb.DuckDBPyConnection, surface_id: str) -> tuple[tuple[str, str], ...]:
    return tuple(connection.execute(
        """select distinct p.symbol, t.timeframe from surface_timeframes t
             join surface_pairs p using(surface_id, pair_key)
            where t.surface_id=? order by p.symbol, t.timeframe""", [surface_id]
    ).fetchall())


def _parent_surface_id(
    connection: duckdb.DuckDBPyConnection, identity: dict[str, object], explicit_parent_id: str | None,
    exclude_surface_id: str | None = None,
) -> str | None:
    scope = tuple(
        (symbol, timeframe)
        for symbol, timeframes in identity["selected_timeframes"].items()  # type: ignore[union-attr]
        for timeframe in timeframes
    )
    if explicit_parent_id is not None:
        parent = connection.execute(
            """select surface_id from surfaces where surface_id=? and build_mode=?
                 and period_start_utc=? and period_end_utc=? and side=?""",
            [explicit_parent_id, identity["build_mode"], identity["period"][0], identity["period"][1], identity["side"]],
        ).fetchone()
        if parent is None or _surface_scope(connection, explicit_parent_id) != scope:
            raise ValueError("explicit parent surface is invalid")
        return explicit_parent_id
    for (surface_id,) in connection.execute(
        """select surface_id from surfaces where build_mode=? and period_start_utc=?
             and period_end_utc=? and side=? order by created_at_utc desc, surface_id desc""",
        [identity["build_mode"], identity["period"][0], identity["period"][1], identity["side"]],
    ).fetchall():
        if surface_id != exclude_surface_id and _surface_scope(connection, str(surface_id)) == scope:
            return str(surface_id)
    return None


def publish_surface(analysis_connection: duckdb.DuckDBPyConnection, surface: DirectSurface) -> PublishedSurface:
    """Atomically persist a fully materialized direct surface without reopening its source."""
    ensure_analysis_schema(analysis_connection)
    surface_id, identity = _surface_identity(surface)
    points = _validate_surface(surface, identity)
    existing = analysis_connection.execute("select surface_id, parent_surface_id from surfaces where surface_id=?", [surface_id]).fetchone()
    if existing:
        stored = tuple(analysis_connection.execute(
            """select canonical_point_key,source_report_id,source_hash,point_event_count,provenance_state,metrics_json
                 from surface_points where surface_id=? order by canonical_point_key""", [surface_id]
        ).fetchall())
        incoming = tuple(
            (point.canonical_point_key, point.source_report_id, point.source_hash, point.point_event_count,
             "REPRODUCIBLE_AT_PUBLICATION", _canonical_json(point.metrics))
            for point in points
        )
        if stored != incoming or (surface.parent_surface_id is not None and existing[1] != surface.parent_surface_id):
            raise ValueError("incoming surface conflicts with immutable publication")
        from .duckdb_direct import DirectPoint
        return PublishedSurface(
            str(existing[0]), existing[1], False,
            tuple(DirectPoint(key, report_id, source_hash, event_count, json.loads(metrics_json))
                  for key, report_id, source_hash, event_count, provenance, metrics_json in stored),
        )
    expected_parent_id = _parent_surface_id(analysis_connection, identity, surface.parent_surface_id)
    analysis_connection.execute("begin transaction")
    try:
        parent_id = expected_parent_id
        analysis_connection.execute(
            """insert into surfaces(surface_id,parent_surface_id,build_mode,period_start_utc,period_end_utc,side,
                grid_contract_json,normalization_contract_version,materializer_version,point_materialization_config_hash)
                values (?,?,?,?,?,?,?,?,?,?)""",
            [surface_id, parent_id, identity["build_mode"], identity["period"][0], identity["period"][1], identity["side"],
             _canonical_json(identity["grid_contract"]), identity["normalization_contract"], identity["materializer_version"], identity["point_materialization_config_hash"]],
        )
        analysis_connection.executemany("insert into surface_sources(surface_id,source_hash) values (?,?)", [(surface_id, source_hash) for source_hash in identity["source_hashes"]])
        pairs: set[tuple[str, str, str, int, int, int]] = set()
        timeframes: set[tuple[str, str]] = set()
        for point in points:
            symbol, side, timeframe, shift, open_ma, close_ma = _point_parts(point)
            pair_key = f"{symbol}|{side}|{shift}|{open_ma}|{close_ma}"
            pairs.add((pair_key, symbol, side, shift, open_ma, close_ma))
            timeframes.add((pair_key, timeframe))
        analysis_connection.executemany("insert into surface_pairs values (?,?,?,?,?,?,?)", [(surface_id, *pair) for pair in sorted(pairs)])
        analysis_connection.executemany("insert into surface_timeframes values (?,?,?,?)", [(surface_id, pair_key, timeframe, "USABLE") for pair_key, timeframe in sorted(timeframes)])
        analysis_connection.executemany(
            "insert into surface_points values (?,?,?,?,?,?,?,?,?)",
            [(surface_id, point.canonical_point_key, f"{'|'.join(point.canonical_point_key.split('|')[:2])}|{'|'.join(point.canonical_point_key.split('|')[3:])}", point.canonical_point_key.split("|")[2], point.point_event_count, point.source_report_id, point.source_hash, "REPRODUCIBLE_AT_PUBLICATION", _canonical_json(point.metrics)) for point in points],
        )
        issues = [(sha256(f"{surface_id}|{issue.symbol}|{issue.timeframe}|{issue.code}|{issue.detail}".encode()).hexdigest(), surface_id, issue.symbol, issue.timeframe, issue.code, _canonical_json({"detail": issue.detail})) for issue in surface.preflight.coverage_issues]
        if issues:
            analysis_connection.executemany("insert into coverage_issues values (?,?,?,?,?,?)", issues)
        analysis_connection.executemany(
            "insert into dedup_decisions values (?,?,?,?,?)",
            [(sha256(f"{surface_id}|{point.canonical_point_key}".encode()).hexdigest(), surface_id, point.canonical_point_key, "ACCEPTED", _canonical_json({"source_hash": point.source_hash})) for point in points],
        )
        analysis_connection.execute("commit")
    except BaseException:
        analysis_connection.execute("rollback")
        raise
    return PublishedSurface(surface_id, parent_id, True, points)


def surface_raw_reproduction_status(
    analysis_connection: duckdb.DuckDBPyConnection, surface_id: str, active_source_hashes: set[str],
) -> str:
    """Derive raw reproducibility from supplied active provenance, without opening source storage."""
    hashes = {
        str(row[0]) for row in analysis_connection.execute(
            "select source_hash from surface_sources where surface_id=?", [surface_id]
        ).fetchall()
    }
    if not hashes:
        raise ValueError("unknown surface")
    return "REPRODUCIBLE" if hashes <= set(active_source_hashes) else "RAW_REPLACED"


def _run_identity(surface_id: str, algorithm_version: str, algorithm_config_json: str) -> str:
    return sha256(f"{surface_id}|{algorithm_version}|{algorithm_config_json}".encode()).hexdigest()


def classify_plateau_lineage(
    parent: Mapping[str, tuple[set[str], Mapping[str, object]]], child: Mapping[str, tuple[set[str], Mapping[str, object]]],
) -> tuple[tuple[str | None, str | None, str, str], ...]:
    """Classify overlap links only; metrics deliberately never cross this boundary."""
    def linked(left: tuple[set[str], Mapping[str, object]], right: tuple[set[str], Mapping[str, object]]) -> bool:
        left_points, left_geometry = left
        right_points, right_geometry = right
        if not left_points.intersection(right_points):
            return False
        if any(left_geometry.get(key) != right_geometry.get(key) for key in ("symbol", "side", "timeframe")):
            return False
        return (int(left_geometry["min_shift_bp"]) <= int(right_geometry["max_shift_bp"])
                and int(right_geometry["min_shift_bp"]) <= int(left_geometry["max_shift_bp"])
                and int(left_geometry["open_ma_min"]) <= int(right_geometry["open_ma_max"])
                and int(right_geometry["open_ma_min"]) <= int(left_geometry["open_ma_max"])
                and int(left_geometry["close_ma_min"]) <= int(right_geometry["close_ma_max"])
                and int(right_geometry["close_ma_min"]) <= int(left_geometry["close_ma_max"]))
    child_links = {key: sorted(old for old, old_data in parent.items() if linked(old_data, data)) for key, data in child.items()}
    parent_links = {key: sorted(new for new, new_data in child.items() if linked(data, new_data)) for key, data in parent.items()}
    rows: list[tuple[str | None, str | None, str, str]] = []
    for new, old_ids in sorted(child_links.items()):
        if not old_ids:
            rows.append((new, None, "NEW", "{}"))
        for old in old_ids:
            relation = "MERGED" if len(old_ids) > 1 else "SPLIT" if len(parent_links[old]) > 1 else "CONTINUED"
            overlap = sorted(child[new][0] & parent[old][0])
            rows.append((new, old, relation, _canonical_json({"overlap_point_keys": overlap})))
    for old, new_ids in sorted(parent_links.items()):
        if not new_ids:
            rows.append((None, old, "DROPPED", "{}"))
    return tuple(rows)


def _stored_plateau_facts(
    connection: duckdb.DuckDBPyConnection, run_id: str,
) -> dict[str, tuple[set[str], Mapping[str, object]]]:
    facts: dict[str, tuple[set[str], Mapping[str, object]]] = {}
    for plateau_id, metrics_json in connection.execute(
        "select plateau_id, metrics_json from plateaus where run_id=? order by plateau_id",
        [run_id],
    ).fetchall():
        members = {
            str(key)
            for (key,) in connection.execute(
                "select canonical_point_key from plateau_members where run_id=? and plateau_id=?",
                [run_id, plateau_id],
            ).fetchall()
        }
        facts[str(plateau_id)] = (members, json.loads(metrics_json or "{}"))
    return facts


def _comparison_facts(
    connection: duckdb.DuckDBPyConnection,
    child_run_id: str,
    child_surface_id: str,
    comparison_run_id: str | None,
) -> dict[str, tuple[set[str], Mapping[str, object]]]:
    if comparison_run_id is None:
        return {}
    if comparison_run_id == child_run_id:
        raise ValueError("analysis run cannot compare lineage with itself")
    comparison = connection.execute(
        """select s.side from analysis_runs r join surfaces s using(surface_id)
             where r.run_id=?""",
        [comparison_run_id],
    ).fetchone()
    current = connection.execute(
        "select side from surfaces where surface_id=?", [child_surface_id]
    ).fetchone()
    if comparison is None or current is None or comparison[0] != current[0]:
        raise ValueError("comparison run is unknown or outside the surface scope")
    return _stored_plateau_facts(connection, comparison_run_id)


def _insert_lineage(
    connection: duckdb.DuckDBPyConnection,
    child_run_id: str,
    comparison_run_id: str | None,
    parent: Mapping[str, object],
    child: Mapping[str, object],
) -> None:
    if comparison_run_id is None:
        return
    for child_id, parent_id, relation, raw_detail in classify_plateau_lineage(parent, child):
        lineage_id = sha256(
            f"{child_run_id}|{child_id}|{comparison_run_id}|{parent_id}|{relation}".encode()
        ).hexdigest()
        detail = json.loads(raw_detail)
        detail["comparison_run_id"] = comparison_run_id
        connection.execute(
            """insert into plateau_lineage values (?,?,?,?,?,?,?)
                 on conflict(lineage_id) do nothing""",
            [
                lineage_id,
                child_run_id if child_id else None,
                child_id,
                comparison_run_id if parent_id else None,
                parent_id,
                relation,
                _canonical_json(detail),
            ],
        )


def publish_analysis_run(analysis_connection: duckdb.DuckDBPyConnection, result: object) -> PublishedAnalysisRun:
    """Atomically store a run and links while retaining surface points by reference."""
    ensure_analysis_schema(analysis_connection)
    surface_id, algorithm_version, config = str(result.surface_id), str(result.algorithm_version), result.algorithm_config
    config_json = _canonical_json(config)
    run_id = _run_identity(surface_id, algorithm_version, config_json)
    existing = analysis_connection.execute("select run_id from analysis_runs where run_id=?", [run_id]).fetchone()
    supplied_facts = getattr(result, "statistics", None)
    names = ("unique_point_count", "economic_eligible_point_count", "event_eligible_point_count", "plateau_count", "ready_candidate_count")
    if supplied_facts is not None:
        if set(supplied_facts) != set(names) or any(isinstance(supplied_facts[name], bool) or not isinstance(supplied_facts[name], int) or supplied_facts[name] < 0 for name in names):
            raise ValueError("analysis run facts are invalid")
    if existing:
        stored_facts = analysis_connection.execute(
            "select facts_state,unique_point_count,economic_eligible_point_count,event_eligible_point_count,plateau_count,ready_candidate_count from analysis_run_facts where run_id=?", [run_id]
        ).fetchone()
        if stored_facts is None:
            raise ValueError("analysis run facts are missing")
        expected = ("COMPUTED", *(supplied_facts[name] for name in names)) if supplied_facts is not None else ("UNAVAILABLE_LEGACY", None, None, None, None, None)
        if tuple(stored_facts) != expected:
            raise ValueError("analysis run facts conflict with immutable publication")
        child_facts = _stored_plateau_facts(analysis_connection, run_id)
        parent_facts = _comparison_facts(
            analysis_connection, run_id, surface_id, getattr(result, "comparison_run_id", None)
        )
        analysis_connection.execute("begin transaction")
        try:
            _insert_lineage(
                analysis_connection,
                run_id,
                getattr(result, "comparison_run_id", None),
                parent_facts,
                child_facts,
            )
            analysis_connection.execute("commit")
        except BaseException:
            analysis_connection.execute("rollback")
            raise
        return PublishedAnalysisRun(run_id, surface_id, False)
    if analysis_connection.execute("select 1 from surfaces where surface_id=?", [surface_id]).fetchone() is None:
        raise ValueError("analysis run references unknown surface")
    plateaus = result.plateaus
    candidates = result.candidates
    comparison_run_id = getattr(result, "comparison_run_id", None)
    parent_members = _comparison_facts(
        analysis_connection, run_id, surface_id, comparison_run_id
    )
    child_members = {str(row["plateau_id"]): ({str(value) for value in row.get("all_point_ids", ())}, row) for row in plateaus.to_dict("records")}
    analysis_connection.execute("begin transaction")
    try:
        analysis_connection.execute("insert into analysis_runs(run_id,surface_id,algorithm_version,algorithm_config_json) values (?,?,?,?)", [run_id, surface_id, algorithm_version, config_json])
        if supplied_facts is None:
            analysis_connection.execute("insert into analysis_run_facts(run_id,facts_state,final_state) values (?, 'UNAVAILABLE_LEGACY', 'COMMITTED')", [run_id])
        else:
            analysis_connection.execute(
                "insert into analysis_run_facts values (?,?,?,?,?,?,?,?)",
                [run_id, "COMPUTED", *(supplied_facts[name] for name in names), "COMMITTED"],
            )
        for row in plateaus.to_dict("records"):
            plateau_id = str(row["plateau_id"])
            metrics = {key: value for key, value in row.items() if key not in {"plateau_id", "all_point_ids", "core_point_ids", "supported_point_ids"}}
            analysis_connection.execute("insert into plateaus(run_id,plateau_id,surface_id,metrics_json) values (?,?,?,?)", [run_id, plateau_id, surface_id, _analysis_json(metrics)])
            members = tuple(row.get("all_point_ids", ()))
            analysis_connection.executemany("insert into plateau_members(run_id,plateau_id,surface_id,canonical_point_key) values (?,?,?,?)", [(run_id, plateau_id, surface_id, str(point)) for point in members])
        for row in candidates.to_dict("records"):
            plateau_ids = tuple(str(value) for value in row.get("plateau_ids", ()))
            if not 2 <= len(plateau_ids) <= 4 or len(set(plateau_ids)) != len(plateau_ids):
                raise ValueError("candidate requires two to four distinct plateau IDs")
            candidate_json = _analysis_json(row)
            candidate_id = sha256(f"{run_id}|{candidate_json}".encode()).hexdigest()
            analysis_connection.execute("insert into candidates values (?,?,?,?)", [candidate_id, run_id, surface_id, candidate_json])
            analysis_connection.executemany("insert into candidate_plateaus values (?,?,?,?)", [(candidate_id, run_id, plateau_id, surface_id) for plateau_id in plateau_ids])
        _insert_lineage(
            analysis_connection,
            run_id,
            comparison_run_id,
            parent_members,
            child_members,
        )
        analysis_connection.execute("commit")
    except BaseException:
        analysis_connection.execute("rollback")
        raise
    return PublishedAnalysisRun(run_id, surface_id, True)
