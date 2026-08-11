from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Mapping

import duckdb

if TYPE_CHECKING:
    from .duckdb_direct import DirectPoint, DirectSurface


ANALYSIS_SCHEMA_VERSION = 1
EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT = (
    "f2a206838bdbe1483c11df88d6ebeb51f137fbc54f0c2a3d07453a6ffb364aa6"
)


class AnalysisSchemaError(ValueError):
    """Raised when an analysis DuckDB does not match this storage contract."""


@dataclass(frozen=True, slots=True)
class PublishedSurface:
    surface_id: str
    parent_surface_id: str | None
    created: bool
    points: tuple[DirectPoint, ...]


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


def _canonical_json(value: object) -> str:
    def normalize(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): normalize(value) for key, value in item.items()}
        if isinstance(item, tuple | list):
            return [normalize(value) for value in item]
        return item

    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


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
        if point.point_event_count < 0 or point.metrics.get("TotalTrades") != point.point_event_count:
            raise ValueError("point event count must equal materialized TotalTrades")
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
