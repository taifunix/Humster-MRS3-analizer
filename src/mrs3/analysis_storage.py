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


ANALYSIS_SCHEMA_VERSION = 2
EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT = (
    "a61bf184df6377f5161e13ba4e542bb36b669c528af360ddbb9b62d666f6adba"
)
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
}
_V1_TABLES = _TABLES - {"candidate_plateaus"}


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
        _migrate_v1_to_v2(connection)
        return
    if tables != _TABLES:
        raise AnalysisSchemaError("analysis database tables do not match the required schema")
    if metadata.get("schema_version") != str(ANALYSIS_SCHEMA_VERSION):
        raise AnalysisSchemaError(
            f"analysis database schema version {metadata.get('schema_version', 'missing')} "
            f"is not v{ANALYSIS_SCHEMA_VERSION}"
        )
    if metadata.get("schema_fingerprint") != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
        raise AnalysisSchemaError("analysis database stored schema fingerprint is not v2")
    if _schema_fingerprint(connection) != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
        raise AnalysisSchemaError("analysis database schema fingerprint does not match v2")


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


def _migrate_v1_to_v2(connection: duckdb.DuckDBPyConnection) -> None:
    """Upgrade candidate ownership to a junction without touching published facts."""
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
        if _schema_fingerprint(connection) != EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT:
            raise AnalysisSchemaError("v1 migration DDL does not match the code-owned v2 fingerprint")
        connection.execute("update schema_info set value=? where key='schema_version'", [str(ANALYSIS_SCHEMA_VERSION)])
        connection.execute("update schema_info set value=? where key='schema_fingerprint'", [EXPECTED_ANALYSIS_SCHEMA_FINGERPRINT])
        connection.execute("commit")
    except BaseException:
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
            raise AnalysisSchemaError("analysis schema DDL does not match the code-owned v2 fingerprint")
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
    if existing:
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
