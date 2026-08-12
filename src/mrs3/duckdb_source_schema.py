from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, Mapping, Sequence
import zlib

import duckdb

from .duckdb_events import (
    ACTION_CODEC,
    EQUITY_CODEC,
    decode_compact_actions,
    decode_compact_deltas,
    decode_wallet_changes,
)
from .source_packs import SourcePackError


SOURCE_SCHEMA_VERSION = 5
NORMALIZATION_CONTRACT_VERSION = "shift-bp-v1"
NORMALIZATION_CONTRACT_TOLERANCE_BP = "0.000001"
STORAGE_MODE = "one-active-compact-payload-per-canonical-report"

_REQUIRED_V4_COLUMNS = {
    "schema_info": {"key", "value"},
    "point_configs": {
        "point_id",
        "symbol",
        "side",
        "timeframe",
        "open_ma_type",
        "open_ma_source",
        "open_ma_len",
        "open_multiplier",
        "close_ma_type",
        "close_ma_source",
        "close_ma_len",
    },
    "time_grids": {
        "grid_id",
        "sample_count",
        "start_timestamp_ms",
        "end_timestamp_ms",
        "timestamps_zlib",
    },
    "report_runs": {
        "report_id",
        "source_sha256",
        "canonical_key",
        "point_id",
        "grid_id",
        "source_file",
        "source_size",
        "imported_at_utc",
        "settings_json",
        "raw_action_count",
        "equity_sample_count",
        "wallet_change_count",
    },
    "report_payloads": {
        "report_id",
        "series_codec",
        "actions_codec",
        "actions_zlib",
        "equity_zlib",
        "wallet_zlib",
    },
}

_REQUIRED_V5_COLUMNS = {
    "schema_info": {"key", "value"},
    "point_configs": {
        "canonical_point_key",
        "symbol",
        "side",
        "timeframe",
        "shift_bp",
        "open_ma_type",
        "open_ma_source",
        "open_ma_len",
        "open_multiplier_raw",
        "close_ma_type",
        "close_ma_source",
        "close_ma_len",
        "row_sha256",
    },
    "time_grids": {
        "grid_hash",
        "sample_count",
        "start_timestamp_ms",
        "end_timestamp_ms",
        "timestamps_zlib",
        "row_sha256",
    },
    "active_reports": {
        "report_id",
        "canonical_report_key",
        "canonical_point_key",
        "grid_hash",
        "source_sha256",
        "source_file",
        "source_size",
        "imported_at_utc",
        "settings_json",
        "raw_action_count",
        "equity_sample_count",
        "wallet_change_count",
        "report_period_start_ms",
        "report_period_end_ms",
        "row_sha256",
    },
    "report_payloads": {
        "report_id",
        "series_codec",
        "actions_codec",
        "actions_zlib",
        "equity_zlib",
        "wallet_zlib",
        "payload_sha256",
    },
    "replacement_history": {
        "audit_id",
        "canonical_report_key",
        "old_source_sha256",
        "new_source_sha256",
        "imported_at_utc",
        "job_id",
    },
}

_REQUIRED_V5_CONSTRAINTS = {
    ("schema_info", "PRIMARY KEY", ("key",), None, ()),
    ("point_configs", "PRIMARY KEY", ("canonical_point_key",), None, ()),
    ("time_grids", "PRIMARY KEY", ("grid_hash",), None, ()),
    ("active_reports", "PRIMARY KEY", ("canonical_report_key",), None, ()),
    ("active_reports", "UNIQUE", ("report_id",), None, ()),
    ("active_reports", "UNIQUE", ("source_sha256",), None, ()),
    (
        "active_reports",
        "FOREIGN KEY",
        ("canonical_point_key",),
        "point_configs",
        ("canonical_point_key",),
    ),
    ("active_reports", "FOREIGN KEY", ("grid_hash",), "time_grids", ("grid_hash",)),
    ("report_payloads", "PRIMARY KEY", ("report_id",), None, ()),
    (
        "report_payloads",
        "FOREIGN KEY",
        ("report_id",),
        "active_reports",
        ("report_id",),
    ),
    ("replacement_history", "PRIMARY KEY", ("audit_id",), None, ()),
}


class SourceSchemaError(ValueError):
    """Raised when a source database cannot satisfy the versioned contract."""


@dataclass(frozen=True, slots=True)
class SourceValidationResult:
    valid: bool
    schema_version: int
    report_count: int
    point_count: int
    grid_count: int
    payload_count: int
    replacement_count: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceMigrationResult:
    source_path: Path
    target_path: Path
    report_count: int
    point_count: int
    grid_count: int
    payload_count: int
    source_database_sha256: str
    target_database_sha256: str
    validation: SourceValidationResult


@dataclass(frozen=True, slots=True)
class _V4Snapshot:
    points: tuple[dict[str, object], ...]
    grids: tuple[dict[str, object], ...]
    reports: tuple[dict[str, object], ...]
    payload_count: int
    source_hashes: frozenset[str]


def normalize_source_shift(value: object, contract_version: object) -> int:
    """Normalize either LONG or SHORT multiplier distance from one to basis points."""
    if str(contract_version) != NORMALIZATION_CONTRACT_VERSION:
        raise SourceSchemaError(
            f"incompatible normalization contract: {contract_version!r}; "
            f"expected {NORMALIZATION_CONTRACT_VERSION!r}"
        )
    try:
        multiplier = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise SourceSchemaError(f"invalid source multiplier: {value!r}") from error
    if not multiplier.is_finite() or multiplier <= 0:
        raise SourceSchemaError(f"invalid source multiplier: {value!r}")
    basis_points = abs(Decimal("1") - multiplier) * Decimal("10000")
    rounded = basis_points.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    tolerance = Decimal(NORMALIZATION_CONTRACT_TOLERANCE_BP)
    if abs(basis_points - rounded) > tolerance:
        raise SourceSchemaError(
            f"source multiplier does not map to the normalization grid: {value!r}"
        )
    return int(rounded)


def _required(metadata: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in metadata and metadata[name] is not None:
            return metadata[name]
    raise SourceSchemaError(f"canonical metadata is missing {names[0]}")


def _exact_integer(value: object, field: str) -> int:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise SourceSchemaError(f"{field} must be an exact integer") from error
    if not number.is_finite() or number != number.to_integral_value():
        raise SourceSchemaError(f"{field} must be an exact integer")
    return int(number)


def _canonical_point_values(metadata: Mapping[str, object]) -> tuple[str, str, str, int, int, int]:
    symbol = str(_required(metadata, "symbol")).strip()
    side = str(_required(metadata, "side")).strip().upper()
    timeframe = str(_required(metadata, "timeframe")).strip()
    if not symbol or side not in {"LONG", "SHORT"} or not timeframe:
        raise SourceSchemaError("canonical point metadata has invalid symbol, side or timeframe")
    contract = metadata.get("normalization_contract_version", NORMALIZATION_CONTRACT_VERSION)
    if "shift_bp" in metadata:
        shift = _exact_integer(metadata["shift_bp"], "shift_bp")
    else:
        raw_multiplier = _required(
            metadata, "open_multiplier", "open_multiplier_raw", "multiplier"
        )
        try:
            multiplier = Decimal(str(raw_multiplier))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise SourceSchemaError(f"invalid source multiplier: {raw_multiplier!r}") from error
        if side == "LONG" and multiplier > 1:
            raise SourceSchemaError("LONG source multiplier must not exceed one")
        if side == "SHORT" and multiplier < 1:
            raise SourceSchemaError("SHORT source multiplier must not be below one")
        shift = normalize_source_shift(raw_multiplier, contract)
    if shift < 0:
        raise SourceSchemaError("shift_bp must be non-negative")
    open_ma = _exact_integer(_required(metadata, "open_ma_len", "open_ma"), "open_ma_len")
    close_ma = _exact_integer(_required(metadata, "close_ma_len", "close_ma"), "close_ma_len")
    return symbol, side, timeframe, shift, open_ma, close_ma


def _canonical_point_key(metadata: Mapping[str, object]) -> str:
    return "|".join(map(str, _canonical_point_values(metadata)))


def canonical_report_key(metadata: Mapping[str, object]) -> str:
    """Return point-plus-period identity; grid content deliberately is not included."""
    point_key = (
        str(metadata["canonical_point_key"])
        if metadata.get("canonical_point_key") is not None
        else _canonical_point_key(metadata)
    )
    start = _exact_integer(
        _required(metadata, "report_period_start_ms", "report_period_start"),
        "report_period_start",
    )
    end = _exact_integer(
        _required(metadata, "report_period_end_ms", "report_period_end"),
        "report_period_end",
    )
    if end < start:
        raise SourceSchemaError("report period end precedes its start")
    return f"{point_key}|{start}|{end}"


def _table_columns(connection: duckdb.DuckDBPyConnection) -> dict[str, set[str]]:
    rows = connection.execute(
        """select table_name,column_name from information_schema.columns
             where table_schema='main' order by table_name,ordinal_position"""
    ).fetchall()
    result: dict[str, set[str]] = {}
    for table, column in rows:
        result.setdefault(str(table), set()).add(str(column))
    return result


def _verify_columns(
    connection: duckdb.DuckDBPyConnection, required: Mapping[str, set[str]], label: str
) -> None:
    available = _table_columns(connection)
    missing_tables = sorted(set(required).difference(available))
    if missing_tables:
        raise SourceSchemaError(f"{label} database is missing tables: {missing_tables}")
    for table, required_columns in required.items():
        if missing := sorted(required_columns.difference(available[table])):
            raise SourceSchemaError(f"{label} table {table} is missing columns: {missing}")


def _schema_metadata(connection: duckdb.DuckDBPyConnection) -> dict[str, str]:
    try:
        rows = connection.execute("select key,value from schema_info").fetchall()
    except duckdb.Error as error:
        raise SourceSchemaError("source database has no readable schema metadata") from error
    metadata = {str(key): str(value) for key, value in rows}
    if len(metadata) != len(rows):
        raise SourceSchemaError("source database has duplicate schema metadata keys")
    return metadata


def _verify_v5_constraints(connection: duckdb.DuckDBPyConnection) -> None:
    actual = {
        (
            str(table),
            str(kind),
            tuple(str(column) for column in (columns or ())),
            None if referenced_table is None else str(referenced_table),
            tuple(str(column) for column in (referenced_columns or ())),
        )
        for table, kind, columns, referenced_table, referenced_columns in connection.execute(
            """select table_name,constraint_type,constraint_column_names,
                      referenced_table,referenced_column_names
                 from duckdb_constraints() where schema_name='main'"""
        ).fetchall()
    }
    if missing := sorted(_REQUIRED_V5_CONSTRAINTS.difference(actual), key=str):
        raise SourceSchemaError(f"v5 source schema is missing required constraints: {missing}")


def _verify_contract_metadata(metadata: Mapping[str, str], *, required: bool) -> None:
    version = metadata.get("normalization_contract_version")
    tolerance = metadata.get("normalization_contract_tolerance_bp")
    if required and (version is None or tolerance is None):
        raise SourceSchemaError("source database is missing normalization contract metadata")
    if version is not None and version != NORMALIZATION_CONTRACT_VERSION:
        raise SourceSchemaError(
            f"incompatible normalization contract: {version!r}; "
            f"expected {NORMALIZATION_CONTRACT_VERSION!r}"
        )
    if tolerance is not None and tolerance != NORMALIZATION_CONTRACT_TOLERANCE_BP:
        raise SourceSchemaError(
            f"incompatible normalization contract tolerance: {tolerance!r}; "
            f"expected {NORMALIZATION_CONTRACT_TOLERANCE_BP!r}"
        )


def ensure_source_schema(connection: duckdb.DuckDBPyConnection) -> int:
    """Create an empty v5 source schema or verify an existing v5 contract."""
    tables = _table_columns(connection)
    if tables:
        if "schema_info" not in tables:
            raise SourceSchemaError("database contains tables but has no source schema marker")
        metadata = _schema_metadata(connection)
        if metadata.get("schema_version") != str(SOURCE_SCHEMA_VERSION):
            raise SourceSchemaError(
                f"database schema version {metadata.get('schema_version', 'missing')} "
                f"is not v{SOURCE_SCHEMA_VERSION}"
            )
        _verify_contract_metadata(metadata, required=True)
        _verify_columns(connection, _REQUIRED_V5_COLUMNS, "v5 source")
        _verify_v5_constraints(connection)
        return SOURCE_SCHEMA_VERSION

    connection.execute("begin transaction")
    try:
        connection.execute(
            """
            create table schema_info(
                key varchar primary key,
                value varchar not null
            );
            create table point_configs(
                canonical_point_key varchar primary key,
                symbol varchar not null,
                side varchar not null check(side in ('LONG','SHORT')),
                timeframe varchar not null,
                shift_bp integer not null check(shift_bp >= 0),
                open_ma_type varchar not null,
                open_ma_source varchar not null,
                open_ma_len integer not null,
                open_multiplier_raw varchar not null,
                close_ma_type varchar not null,
                close_ma_source varchar not null,
                close_ma_len integer not null,
                row_sha256 varchar not null check(length(row_sha256) = 64)
            );
            create table time_grids(
                grid_hash varchar primary key check(length(grid_hash) = 64),
                sample_count integer not null check(sample_count > 0),
                start_timestamp_ms bigint not null,
                end_timestamp_ms bigint not null,
                timestamps_zlib blob not null,
                row_sha256 varchar not null check(length(row_sha256) = 64),
                check(end_timestamp_ms >= start_timestamp_ms)
            );
            create table active_reports(
                report_id varchar unique not null,
                canonical_report_key varchar primary key,
                canonical_point_key varchar not null references point_configs(canonical_point_key),
                grid_hash varchar not null references time_grids(grid_hash),
                source_sha256 varchar unique not null check(length(source_sha256) = 64),
                source_file varchar not null,
                source_size bigint not null check(source_size >= 0),
                imported_at_utc timestamp not null,
                settings_json varchar not null,
                raw_action_count integer not null check(raw_action_count >= 0),
                equity_sample_count integer not null check(equity_sample_count > 0),
                wallet_change_count integer not null check(wallet_change_count >= 0),
                report_period_start_ms bigint not null,
                report_period_end_ms bigint not null,
                row_sha256 varchar not null check(length(row_sha256) = 64),
                check(report_period_end_ms >= report_period_start_ms)
            );
            create table report_payloads(
                report_id varchar primary key references active_reports(report_id),
                series_codec varchar not null,
                actions_codec varchar not null,
                actions_zlib blob not null,
                equity_zlib blob not null,
                wallet_zlib blob not null,
                payload_sha256 varchar not null check(length(payload_sha256) = 64)
            );
            create table replacement_history(
                audit_id varchar primary key,
                canonical_report_key varchar not null,
                old_source_sha256 varchar not null check(length(old_source_sha256) = 64),
                new_source_sha256 varchar not null check(length(new_source_sha256) = 64),
                imported_at_utc timestamp not null,
                job_id varchar not null
            );
            """
        )
        connection.executemany(
            "insert into schema_info values (?,?)",
            [
                ("schema_version", str(SOURCE_SCHEMA_VERSION)),
                ("storage_mode", STORAGE_MODE),
                ("normalization_contract_version", NORMALIZATION_CONTRACT_VERSION),
                ("normalization_contract_tolerance_bp", NORMALIZATION_CONTRACT_TOLERANCE_BP),
            ],
        )
        connection.execute("commit")
    except BaseException:
        connection.execute("rollback")
        raise
    return SOURCE_SCHEMA_VERSION


def _rows(
    connection: duckdb.DuckDBPyConnection, query: str, parameters: Sequence[object] = ()
) -> tuple[dict[str, object], ...]:
    cursor = connection.execute(query, parameters)
    columns = [str(column[0]) for column in cursor.description]
    return tuple(dict(zip(columns, row, strict=True)) for row in cursor.fetchall())


def _iter_rows(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    parameters: Sequence[object] = (),
    *,
    batch_size: int = 500,
) -> Iterator[dict[str, object]]:
    cursor = connection.execute(query, parameters)
    columns = [str(column[0]) for column in cursor.description]
    while batch := cursor.fetchmany(batch_size):
        for row in batch:
            yield dict(zip(columns, row, strict=True))


def _file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_fields(*values: object) -> str:
    digest = sha256()
    for value in values:
        if isinstance(value, (bytes, bytearray, memoryview)):
            payload = bytes(value)
        else:
            payload = str(value).encode("utf-8")
        digest.update(struct.pack("<Q", len(payload)))
        digest.update(payload)
    return digest.hexdigest()


def _point_hash(row: Mapping[str, object]) -> str:
    return _hash_fields(
        row["canonical_point_key"],
        row["symbol"],
        row["side"],
        row["timeframe"],
        row["shift_bp"],
        row["open_ma_type"],
        row["open_ma_source"],
        row["open_ma_len"],
        row["open_multiplier_raw"],
        row["close_ma_type"],
        row["close_ma_source"],
        row["close_ma_len"],
    )


def _grid_content_hash(timestamps: Sequence[int]) -> str:
    return sha256(struct.pack(f"<{len(timestamps)}q", *timestamps)).hexdigest()


def _grid_hash(row: Mapping[str, object]) -> str:
    return _hash_fields(
        row["grid_hash"],
        row["sample_count"],
        row["start_timestamp_ms"],
        row["end_timestamp_ms"],
        row["timestamps_zlib"],
    )


def _report_hash(row: Mapping[str, object]) -> str:
    return _hash_fields(
        row["report_id"],
        row["canonical_report_key"],
        row["canonical_point_key"],
        row["grid_hash"],
        row["source_sha256"],
        row["source_file"],
        row["source_size"],
        row["imported_at_utc"],
        row["settings_json"],
        row["raw_action_count"],
        row["equity_sample_count"],
        row["wallet_change_count"],
        row["report_period_start_ms"],
        row["report_period_end_ms"],
    )


def _payload_hash(row: Mapping[str, object]) -> str:
    return _hash_fields(
        row["report_id"],
        row["series_codec"],
        row["actions_codec"],
        row["actions_zlib"],
        row["equity_zlib"],
        row["wallet_zlib"],
    )


def _validate_v5(connection: duckdb.DuckDBPyConnection) -> SourceValidationResult:
    _verify_columns(connection, _REQUIRED_V5_COLUMNS, "v5 source")
    _verify_v5_constraints(connection)
    metadata = _schema_metadata(connection)
    if metadata.get("schema_version") != str(SOURCE_SCHEMA_VERSION):
        raise SourceSchemaError(
            f"source database schema version {metadata.get('schema_version', 'missing')} "
            f"is not v{SOURCE_SCHEMA_VERSION}"
        )
    _verify_contract_metadata(metadata, required=True)
    if metadata.get("storage_mode") != STORAGE_MODE:
        raise SourceSchemaError("source database has incompatible storage mode")

    point_count = int(connection.execute("select count(*) from point_configs").fetchone()[0])
    grid_count = int(connection.execute("select count(*) from time_grids").fetchone()[0])
    report_count = int(connection.execute("select count(*) from active_reports").fetchone()[0])
    payload_count = int(connection.execute("select count(*) from report_payloads").fetchone()[0])
    replacements = int(connection.execute("select count(*) from replacement_history").fetchone()[0])
    missing_payloads = int(
        connection.execute(
            """select count(*) from active_reports r
                 left join report_payloads p using(report_id) where p.report_id is null"""
        ).fetchone()[0]
    )
    orphan_payloads = int(
        connection.execute(
            """select count(*) from report_payloads p
                 left join active_reports r using(report_id) where r.report_id is null"""
        ).fetchone()[0]
    )
    if report_count != payload_count or missing_payloads or orphan_payloads:
        raise SourceSchemaError("active report/payload references do not match")

    for point in _iter_rows(
        connection, "select * from point_configs order by canonical_point_key"
    ):
        expected_key = _canonical_point_key(point)
        if point["canonical_point_key"] != expected_key or point["row_sha256"] != _point_hash(point):
            raise SourceSchemaError("point row hash or canonical key mismatch")
    for grid in _iter_rows(connection, "select * from time_grids order by grid_hash"):
        timestamps = decode_compact_deltas(
            bytes(grid["timestamps_zlib"]), int(grid["sample_count"]), codec=EQUITY_CODEC
        )
        if not timestamps or timestamps[0] != int(grid["start_timestamp_ms"]) or timestamps[-1] != int(
            grid["end_timestamp_ms"]
        ):
            raise SourceSchemaError("time-grid bounds do not match decoded payload")
        if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
            raise SourceSchemaError("decoded time grid is not strictly increasing")
        if grid["grid_hash"] != _grid_content_hash(timestamps) or grid["row_sha256"] != _grid_hash(grid):
            raise SourceSchemaError("time-grid content or row hash mismatch")
    for joined in _iter_rows(
        connection,
        """select r.*,p.series_codec,p.actions_codec,p.actions_zlib,p.equity_zlib,
                  p.wallet_zlib,p.payload_sha256,
                  g.start_timestamp_ms as grid_start_timestamp_ms,
                  g.end_timestamp_ms as grid_end_timestamp_ms
             from active_reports r join report_payloads p using(report_id)
             join time_grids g using(grid_hash)
            order by r.canonical_report_key""",
    ):
        report = joined
        if (
            int(report["report_period_start_ms"])
            != int(report["grid_start_timestamp_ms"])
            or int(report["report_period_end_ms"])
            != int(report["grid_end_timestamp_ms"])
        ):
            raise SourceSchemaError(
                "active report period does not match referenced time-grid bounds"
            )
        expected_key = canonical_report_key(report)
        if report["canonical_report_key"] != expected_key or report["row_sha256"] != _report_hash(report):
            raise SourceSchemaError("active report row hash or canonical key mismatch")
        try:
            settings = json.loads(str(report["settings_json"]))
        except (TypeError, ValueError) as error:
            raise SourceSchemaError("active report settings JSON is invalid") from error
        if not isinstance(settings, dict):
            raise SourceSchemaError("active report settings JSON is invalid")
        payload = joined
        if payload["payload_sha256"] != _payload_hash(payload):
            raise SourceSchemaError("report payload hash mismatch")
        if payload["series_codec"] != EQUITY_CODEC or payload["actions_codec"] != ACTION_CODEC:
            raise SourceSchemaError("report payload codec is incompatible")
        decode_compact_actions(bytes(payload["actions_zlib"]), int(report["raw_action_count"]))
        decode_compact_deltas(
            bytes(payload["equity_zlib"]),
            int(report["equity_sample_count"]),
            codec=str(payload["series_codec"]),
        )
        decode_wallet_changes(
            bytes(payload["wallet_zlib"]),
            int(report["wallet_change_count"]),
            codec=str(payload["series_codec"]),
        )
    return SourceValidationResult(
        True,
        SOURCE_SCHEMA_VERSION,
        report_count,
        point_count,
        grid_count,
        payload_count,
        replacements,
        (),
    )


def validate_source_database(connection: duckdb.DuckDBPyConnection) -> SourceValidationResult:
    """Validate every v5 row, reference, compact payload and integrity hash."""
    try:
        return _validate_v5(connection)
    except (duckdb.Error, SourcePackError, SourceSchemaError, ValueError, zlib.error) as error:
        return SourceValidationResult(False, 0, 0, 0, 0, 0, 0, (str(error),))


def _settings_point(settings_json: object, side: str) -> dict[str, object]:
    try:
        settings = json.loads(str(settings_json))
        basic = settings["basic"]
        mrs2 = settings["mrs2"]
        active = "long" if side == "LONG" else "short"
        opened = mrs2[f"ma_{active}"]
        closed = mrs2[f"ma_close_{active}"]
        return {
            "symbol": str(basic["symbol"]).strip(),
            "side": side,
            "timeframe": str(basic["time_frame"]).strip(),
            "open_multiplier": opened["multiplier"],
            "open_ma_len": opened["len"],
            "close_ma_len": closed["len"],
        }
    except (KeyError, TypeError, ValueError) as error:
        raise SourceSchemaError("v4 report settings JSON is incompatible") from error


def _read_v4_snapshot(connection: duckdb.DuckDBPyConnection) -> _V4Snapshot:
    _verify_columns(connection, _REQUIRED_V4_COLUMNS, "v4 source")
    metadata = _schema_metadata(connection)
    if metadata.get("schema_version") != "4":
        raise SourceSchemaError(
            f"source database schema version {metadata.get('schema_version', 'missing')} is not v4"
        )
    _verify_contract_metadata(metadata, required=False)
    points = _rows(connection, "select * from point_configs order by point_id")
    reports = _rows(connection, "select * from report_runs order by report_id")
    grid_count = int(connection.execute("select count(*) from time_grids").fetchone()[0])
    payload_count = int(connection.execute("select count(*) from report_payloads").fetchone()[0])
    point_ids = {str(row["point_id"]) for row in points}
    report_ids = [str(row["report_id"]) for row in reports]
    source_hashes = [str(row["source_sha256"]) for row in reports]
    old_canonical = [str(row["canonical_key"]) for row in reports]
    if len(report_ids) != len(set(report_ids)):
        raise SourceSchemaError("v4 report identifiers are not unique")
    missing_payloads = int(
        connection.execute(
            """select count(*) from report_runs r left join report_payloads p using(report_id)
                where p.report_id is null"""
        ).fetchone()[0]
    )
    orphan_payloads = int(
        connection.execute(
            """select count(*) from report_payloads p left join report_runs r using(report_id)
                where r.report_id is null"""
        ).fetchone()[0]
    )
    if len(reports) != payload_count or missing_payloads or orphan_payloads:
        raise SourceSchemaError("v4 report and payload row counts/references do not match")
    if len(source_hashes) != len(set(source_hashes)):
        raise SourceSchemaError("v4 source hashes are not unique")
    if len(old_canonical) != len(set(old_canonical)):
        raise SourceSchemaError("v4 canonical keys are not unique")
    missing_references = int(
        connection.execute(
            """select count(*) from report_runs r
                 left join point_configs c using(point_id)
                 left join time_grids g using(grid_id)
                where c.point_id is null or g.grid_id is null"""
        ).fetchone()[0]
    )
    if missing_references:
        raise SourceSchemaError("v4 report has a missing point or grid reference")

    grids: list[dict[str, object]] = []
    for grid in _iter_rows(connection, "select * from time_grids order by grid_id"):
        timestamps = decode_compact_deltas(
            bytes(grid["timestamps_zlib"]), int(grid["sample_count"]), codec=EQUITY_CODEC
        )
        if not timestamps or timestamps[0] != int(grid["start_timestamp_ms"]) or timestamps[-1] != int(
            grid["end_timestamp_ms"]
        ):
            raise SourceSchemaError("v4 time-grid bounds do not match decoded payload")
        if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
            raise SourceSchemaError("v4 decoded time grid is not strictly increasing")
        grids.append(
            {
                "grid_id": grid["grid_id"],
                "sample_count": grid["sample_count"],
                "start_timestamp_ms": grid["start_timestamp_ms"],
                "end_timestamp_ms": grid["end_timestamp_ms"],
                "grid_hash": _grid_content_hash(timestamps),
            }
        )
    if len(grids) != grid_count:
        raise SourceSchemaError("v4 time-grid row count changed during validation")
    grids_by_id = {str(row["grid_id"]): row for row in grids}
    points_by_id = {str(row["point_id"]): row for row in points}
    new_keys: list[str] = []
    for report in reports:
        point = points_by_id[str(report["point_id"])]
        grid = grids_by_id[str(report["grid_id"])]
        settings_point = _settings_point(report["settings_json"], str(point["side"]).upper())
        point_metadata = {
            "symbol": point["symbol"],
            "side": point["side"],
            "timeframe": point["timeframe"],
            "open_multiplier": point["open_multiplier"],
            "open_ma_len": point["open_ma_len"],
            "close_ma_len": point["close_ma_len"],
        }
        if _canonical_point_values(settings_point) != _canonical_point_values(point_metadata):
            raise SourceSchemaError("v4 report settings do not match its point row")
        new_keys.append(
            canonical_report_key(
                {
                    **point_metadata,
                    "report_period_start_ms": grid["start_timestamp_ms"],
                    "report_period_end_ms": grid["end_timestamp_ms"],
                }
            )
        )
    for payload in _iter_rows(
        connection,
        """select p.*,r.raw_action_count,r.equity_sample_count,r.wallet_change_count
             from report_payloads p join report_runs r using(report_id)
            order by p.report_id""",
    ):
        if payload["series_codec"] != EQUITY_CODEC or payload["actions_codec"] != ACTION_CODEC:
            raise SourceSchemaError("v4 report payload codec is incompatible")
        try:
            decode_compact_actions(bytes(payload["actions_zlib"]), int(payload["raw_action_count"]))
            decode_compact_deltas(
                bytes(payload["equity_zlib"]),
                int(payload["equity_sample_count"]),
                codec=str(payload["series_codec"]),
            )
            decode_wallet_changes(
                bytes(payload["wallet_zlib"]),
                int(payload["wallet_change_count"]),
                codec=str(payload["series_codec"]),
            )
        except (SourcePackError, ValueError, zlib.error) as error:
            raise SourceSchemaError(f"v4 report payload validation failed: {payload['report_id']}") from error
    if len(new_keys) != len(set(new_keys)):
        raise SourceSchemaError("v4 reports collapse to duplicate canonical report keys")
    return _V4Snapshot(points, tuple(grids), reports, payload_count, frozenset(source_hashes))


def _copy_snapshot(
    connection: duckdb.DuckDBPyConnection,
    source_connection: duckdb.DuckDBPyConnection,
    snapshot: _V4Snapshot,
) -> None:
    grid_metadata = {str(row["grid_id"]): row for row in snapshot.grids}
    grids_by_id: dict[str, dict[str, object]] = {}
    for old in _iter_rows(source_connection, "select * from time_grids order by grid_id"):
        metadata = grid_metadata[str(old["grid_id"])]
        row = {
            "grid_hash": metadata["grid_hash"],
            "sample_count": old["sample_count"],
            "start_timestamp_ms": old["start_timestamp_ms"],
            "end_timestamp_ms": old["end_timestamp_ms"],
            "timestamps_zlib": old["timestamps_zlib"],
        }
        row["row_sha256"] = _grid_hash(row)
        grids_by_id[str(old["grid_id"])] = row
        connection.execute(
            "insert into time_grids values (?,?,?,?,?,?) on conflict do nothing",
            list(row.values()),
        )

    points_by_id: dict[str, dict[str, object]] = {}
    for old in snapshot.points:
        point_metadata = {
            "symbol": old["symbol"],
            "side": old["side"],
            "timeframe": old["timeframe"],
            "open_multiplier": old["open_multiplier"],
            "open_ma_len": old["open_ma_len"],
            "close_ma_len": old["close_ma_len"],
        }
        canonical_point_key = _canonical_point_key(point_metadata)
        row = {
            "canonical_point_key": canonical_point_key,
            "symbol": str(old["symbol"]).strip(),
            "side": str(old["side"]).strip().upper(),
            "timeframe": str(old["timeframe"]).strip(),
            "shift_bp": normalize_source_shift(
                old["open_multiplier"], NORMALIZATION_CONTRACT_VERSION
            ),
            "open_ma_type": old["open_ma_type"],
            "open_ma_source": old["open_ma_source"],
            "open_ma_len": old["open_ma_len"],
            "open_multiplier_raw": old["open_multiplier"],
            "close_ma_type": old["close_ma_type"],
            "close_ma_source": old["close_ma_source"],
            "close_ma_len": old["close_ma_len"],
        }
        row["row_sha256"] = _point_hash(row)
        points_by_id[str(old["point_id"])] = row
        connection.execute(
            "insert into point_configs values (?,?,?,?,?,?,?,?,?,?,?,?,?) on conflict do nothing",
            list(row.values()),
        )

    reports_by_id = {str(row["report_id"]): row for row in snapshot.reports}
    for old_payload in _iter_rows(
        source_connection, "select * from report_payloads order by report_id"
    ):
        old = reports_by_id[str(old_payload["report_id"])]
        point = points_by_id[str(old["point_id"])]
        grid = grids_by_id[str(old["grid_id"])]
        report = {
            "report_id": old["report_id"],
            "canonical_report_key": canonical_report_key(
                {
                    **point,
                    "report_period_start_ms": grid["start_timestamp_ms"],
                    "report_period_end_ms": grid["end_timestamp_ms"],
                }
            ),
            "canonical_point_key": point["canonical_point_key"],
            "grid_hash": grid["grid_hash"],
            "source_sha256": old["source_sha256"],
            "source_file": old["source_file"],
            "source_size": old["source_size"],
            "imported_at_utc": old["imported_at_utc"],
            "settings_json": old["settings_json"],
            "raw_action_count": old["raw_action_count"],
            "equity_sample_count": old["equity_sample_count"],
            "wallet_change_count": old["wallet_change_count"],
            "report_period_start_ms": grid["start_timestamp_ms"],
            "report_period_end_ms": grid["end_timestamp_ms"],
        }
        report["row_sha256"] = _report_hash(report)
        connection.execute(
            "insert into active_reports values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            list(report.values()),
        )
        payload = {
            "report_id": old_payload["report_id"],
            "series_codec": old_payload["series_codec"],
            "actions_codec": old_payload["actions_codec"],
            "actions_zlib": old_payload["actions_zlib"],
            "equity_zlib": old_payload["equity_zlib"],
            "wallet_zlib": old_payload["wallet_zlib"],
        }
        payload["payload_sha256"] = _payload_hash(payload)
        connection.execute(
            "insert into report_payloads values (?,?,?,?,?,?,?)",
            list(payload.values()),
        )


def _trusted_v4_metadata(connection: duckdb.DuckDBPyConnection) -> tuple[tuple[dict[str, object], ...], dict[str, dict[str, object]], int, frozenset[str]]:
    """Read trusted-v4 metadata, decoding only time grids needed for v5 identity."""
    _verify_columns(connection, _REQUIRED_V4_COLUMNS, "v4 source")
    metadata = _schema_metadata(connection)
    if metadata.get("schema_version") != "4":
        raise SourceSchemaError("source database schema version is not v4")
    _verify_contract_metadata(metadata, required=False)
    points = _rows(connection, "select * from point_configs order by point_id")
    report_count = int(connection.execute("select count(*) from report_runs").fetchone()[0])
    payload_count = int(connection.execute("select count(*) from report_payloads").fetchone()[0])
    if report_count != payload_count:
        raise SourceSchemaError("v4 report and payload row counts/references do not match")
    if int(connection.execute("select count(*) from report_runs r left join report_payloads p using(report_id) where p.report_id is null").fetchone()[0]) or int(connection.execute("select count(*) from report_payloads p left join report_runs r using(report_id) where r.report_id is null").fetchone()[0]):
        raise SourceSchemaError("v4 report and payload row counts/references do not match")
    if int(connection.execute("select count(*) from report_runs") .fetchone()[0]) != int(connection.execute("select count(distinct source_sha256) from report_runs").fetchone()[0]):
        raise SourceSchemaError("v4 source hashes are not unique")
    if int(connection.execute("select count(*) from report_runs") .fetchone()[0]) != int(connection.execute("select count(distinct canonical_key) from report_runs").fetchone()[0]):
        raise SourceSchemaError("v4 canonical keys are not unique")
    if int(connection.execute("select count(*) from report_runs r left join point_configs c using(point_id) left join time_grids g using(grid_id) where c.point_id is null or g.grid_id is null").fetchone()[0]):
        raise SourceSchemaError("v4 report has a missing point or grid reference")
    grids: dict[str, dict[str, object]] = {}
    for old in _iter_rows(connection, "select * from time_grids order by grid_id"):
        timestamps = decode_compact_deltas(bytes(old["timestamps_zlib"]), int(old["sample_count"]), codec=EQUITY_CODEC)
        if not timestamps or timestamps[0] != int(old["start_timestamp_ms"]) or timestamps[-1] != int(old["end_timestamp_ms"]) or any(right <= left for left, right in zip(timestamps, timestamps[1:])):
            raise SourceSchemaError("v4 time-grid bounds do not match decoded payload")
        row = {"grid_hash": _grid_content_hash(timestamps), "sample_count": old["sample_count"], "start_timestamp_ms": old["start_timestamp_ms"], "end_timestamp_ms": old["end_timestamp_ms"], "timestamps_zlib": old["timestamps_zlib"]}
        row["row_sha256"] = _grid_hash(row)
        grids[str(old["grid_id"])] = row
    source_hashes = frozenset(str(row[0]) for row in connection.execute("select source_sha256 from report_runs").fetchall())
    return points, grids, report_count, source_hashes


def _trusted_prepare_batch(reports: Sequence[dict[str, object]], payloads: Mapping[str, dict[str, object]], points: Mapping[str, dict[str, object]], grids: Mapping[str, dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    prepared_reports: list[dict[str, object]] = []
    prepared_payloads: list[dict[str, object]] = []
    for old in reports:
        point, grid = points[str(old["point_id"])], grids[str(old["grid_id"])]
        point_metadata = {
            "symbol": point["symbol"], "side": point["side"], "timeframe": point["timeframe"],
            "open_multiplier": point["open_multiplier_raw"], "open_ma_len": point["open_ma_len"],
            "close_ma_len": point["close_ma_len"],
        }
        if _canonical_point_values(_settings_point(old["settings_json"], str(point["side"]))) != _canonical_point_values(point_metadata):
            raise SourceSchemaError("v4 report settings do not match its point row")
        row = {"report_id": old["report_id"], "canonical_report_key": canonical_report_key({**point, "report_period_start_ms": grid["start_timestamp_ms"], "report_period_end_ms": grid["end_timestamp_ms"]}), "canonical_point_key": point["canonical_point_key"], "grid_hash": grid["grid_hash"], "source_sha256": old["source_sha256"], "source_file": old["source_file"], "source_size": old["source_size"], "imported_at_utc": old["imported_at_utc"], "settings_json": old["settings_json"], "raw_action_count": old["raw_action_count"], "equity_sample_count": old["equity_sample_count"], "wallet_change_count": old["wallet_change_count"], "report_period_start_ms": grid["start_timestamp_ms"], "report_period_end_ms": grid["end_timestamp_ms"]}
        row["row_sha256"] = _report_hash(row)
        payload = dict(payloads[str(old["report_id"])])
        payload["payload_sha256"] = _payload_hash(payload)
        prepared_reports.append(row); prepared_payloads.append(payload)
    return prepared_reports, prepared_payloads


def _trusted_prepare_record(old: dict[str, object], payload: dict[str, object], points: Mapping[str, dict[str, object]], grids: Mapping[str, dict[str, object]]) -> tuple[dict[str, object], dict[str, object]]:
    """Prepare one detached record; deliberately does not decode opaque payloads."""
    reports, payloads = _trusted_prepare_batch((old,), {str(old["report_id"]): payload}, points, grids)
    return reports[0], payloads[0]


def _validate_v5_structural(connection: duckdb.DuckDBPyConnection) -> SourceValidationResult:
    """Validate v5 persisted structure and hashes without decoding opaque report payloads."""
    _verify_columns(connection, _REQUIRED_V5_COLUMNS, "v5 source"); _verify_v5_constraints(connection)
    metadata = _schema_metadata(connection)
    if metadata.get("schema_version") != str(SOURCE_SCHEMA_VERSION): raise SourceSchemaError("migrated database is not v5")
    _verify_contract_metadata(metadata, required=True)
    if metadata.get("storage_mode") != STORAGE_MODE: raise SourceSchemaError("source database has incompatible storage mode")
    counts = tuple(int(connection.execute(f"select count(*) from {table}").fetchone()[0]) for table in ("active_reports", "point_configs", "time_grids", "report_payloads", "replacement_history"))
    reports, points, grids, payloads, replacements = counts
    if reports != payloads or int(connection.execute("select count(*) from active_reports r full outer join report_payloads p using(report_id) where r.report_id is null or p.report_id is null").fetchone()[0]): raise SourceSchemaError("active report/payload references do not match")
    for row in _iter_rows(connection, "select * from point_configs order by canonical_point_key"):
        if row["canonical_point_key"] != _canonical_point_key(row) or row["row_sha256"] != _point_hash(row): raise SourceSchemaError("point row hash or canonical key mismatch")
    for row in _iter_rows(connection, "select * from time_grids order by grid_hash"):
        if row["row_sha256"] != _grid_hash(row): raise SourceSchemaError("time-grid row hash mismatch")
    for row in _iter_rows(connection, "select r.*,g.start_timestamp_ms as grid_start_timestamp_ms,g.end_timestamp_ms as grid_end_timestamp_ms from active_reports r join time_grids g using(grid_hash) order by r.canonical_report_key"):
        if row["canonical_report_key"] != canonical_report_key(row) or row["row_sha256"] != _report_hash(row) or int(row["report_period_start_ms"]) != int(row["grid_start_timestamp_ms"]) or int(row["report_period_end_ms"]) != int(row["grid_end_timestamp_ms"]): raise SourceSchemaError("active report row hash or canonical key mismatch")
    for row in _iter_rows(connection, "select * from report_payloads order by report_id"):
        if row["series_codec"] != EQUITY_CODEC or row["actions_codec"] != ACTION_CODEC:
            raise SourceSchemaError("report payload codec is incompatible")
        if row["payload_sha256"] != _payload_hash(row): raise SourceSchemaError("report payload hash mismatch")
    return SourceValidationResult(True, SOURCE_SCHEMA_VERSION, reports, points, grids, payloads, replacements, ())


def validate_source_database_structural(connection: duckdb.DuckDBPyConnection) -> SourceValidationResult:
    """Validate v5 structure and hashes without decoding opaque report payloads."""
    try:
        return _validate_v5_structural(connection)
    except (duckdb.Error, SourcePackError, SourceSchemaError, ValueError, zlib.error) as error:
        return SourceValidationResult(False, 0, 0, 0, 0, 0, 0, (str(error),))


def migrate_source_database(source_path: Path, target_path: Path, *, workers: int = 4, transaction_batch_size: int = 500) -> SourceMigrationResult:
    """Stream a trusted v4 archive into a validated v5 staging database."""
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1 or isinstance(transaction_batch_size, bool) or not isinstance(transaction_batch_size, int) or transaction_batch_size < 1:
        raise SourceSchemaError("workers and transaction_batch_size must be positive integers")
    source = Path(source_path).resolve()
    target = Path(target_path).resolve()
    if source == target:
        raise SourceSchemaError("source and target database paths must be different")
    if not source.is_file():
        raise SourceSchemaError(f"source database does not exist: {source}")
    if target.exists():
        raise SourceSchemaError(f"target database already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, stage_name = tempfile.mkstemp(
        prefix=f".{target.name}.migration-", suffix=".duckdb", dir=target.parent
    )
    os.close(descriptor)
    stage = Path(stage_name)
    stage.unlink()
    stage_connection: duckdb.DuckDBPyConnection | None = None
    source_connection: duckdb.DuckDBPyConnection | None = None
    try:
        source_connection = duckdb.connect(str(source), read_only=True)
        source_connection.execute("begin transaction")
        points, grids, report_count, source_hashes = _trusted_v4_metadata(source_connection)
        stage_connection = duckdb.connect(str(stage)); ensure_source_schema(stage_connection)
        converted_points: dict[str, dict[str, object]] = {}
        stage_connection.execute("begin transaction")
        try:
            for old in points:
                point = {"canonical_point_key": _canonical_point_key(old), "symbol": str(old["symbol"]).strip(), "side": str(old["side"]).strip().upper(), "timeframe": str(old["timeframe"]).strip(), "shift_bp": normalize_source_shift(old["open_multiplier"], NORMALIZATION_CONTRACT_VERSION), "open_ma_type": old["open_ma_type"], "open_ma_source": old["open_ma_source"], "open_ma_len": old["open_ma_len"], "open_multiplier_raw": old["open_multiplier"], "close_ma_type": old["close_ma_type"], "close_ma_source": old["close_ma_source"], "close_ma_len": old["close_ma_len"]}; point["row_sha256"] = _point_hash(point); converted_points[str(old["point_id"])] = point
                stage_connection.execute("insert into point_configs values (?,?,?,?,?,?,?,?,?,?,?,?,?)", list(point.values()))
            for grid in grids.values(): stage_connection.execute("insert into time_grids values (?,?,?,?,?,?)", list(grid.values()))
            stage_connection.execute("commit")
        except BaseException:
            stage_connection.execute("rollback"); raise
        with ThreadPoolExecutor(max_workers=workers) as executor:
            last_id: str | None = None
            while True:
                query = "select report_id,source_sha256,canonical_key,point_id,grid_id,source_file,source_size,imported_at_utc,settings_json,raw_action_count,equity_sample_count,wallet_change_count from report_runs"
                parameters: tuple[object, ...] = ()
                if last_id is not None:
                    query += " where report_id > ?"
                    parameters = (last_id,)
                reports = list(_iter_rows(source_connection, query + " order by report_id limit ?", (*parameters, transaction_batch_size), batch_size=transaction_batch_size))
                if not reports: break
                expected = {str(row["report_id"]) for row in reports}
                last_id = str(reports[-1]["report_id"])
                placeholders = ",".join("?" for _ in expected)
                payload_rows = list(_iter_rows(source_connection, f"select report_id,series_codec,actions_codec,actions_zlib,equity_zlib,wallet_zlib from report_payloads where report_id in ({placeholders})", tuple(expected), batch_size=transaction_batch_size))
                payloads = {str(row["report_id"]): row for row in payload_rows}
                if len(payload_rows) != len(payloads) or set(payloads) != expected: raise SourceSchemaError("v4 payload batch identifiers do not match report batch")
                if any(row["series_codec"] != EQUITY_CODEC or row["actions_codec"] != ACTION_CODEC for row in payload_rows):
                    raise SourceSchemaError("v4 report payload codec is incompatible")
                prepared = list(executor.map(_trusted_prepare_record, reports, (payloads[str(row["report_id"])] for row in reports), (converted_points for _ in reports), (grids for _ in reports)))
                prepared_reports = [row for row, _ in prepared]
                prepared_payloads = [payload for _, payload in prepared]
                stage_connection.execute("begin transaction")
                try:
                    stage_connection.executemany("insert into active_reports values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [list(row.values()) for row in prepared_reports])
                    stage_connection.executemany("insert into report_payloads values (?,?,?,?,?,?,?)", [list(row.values()) for row in prepared_payloads])
                    stage_connection.execute("commit")
                except BaseException:
                    stage_connection.execute("rollback"); raise
        if int(source_connection.execute("select count(*) from report_runs").fetchone()[0]) != report_count: raise SourceSchemaError("source database changed during migration")
        source_connection.execute("commit"); source_connection.close(); source_connection = None
        stage_connection.close()
        stage_connection = None

        validation_connection = duckdb.connect(str(stage), read_only=True)
        try:
            validation = validate_source_database_structural(validation_connection)
            migrated_hashes = frozenset(
                str(row[0])
                for row in validation_connection.execute(
                    "select source_sha256 from active_reports"
                ).fetchall()
            )
        finally:
            validation_connection.close()
        if not validation.valid:
            raise SourceSchemaError(f"migrated source validation failed: {validation.errors}")
        if migrated_hashes != source_hashes:
            raise SourceSchemaError("migrated active source-hash parity failed")
        if (
            validation.report_count != report_count
            or validation.payload_count != report_count
            or validation.point_count != len({str(row["canonical_point_key"]) for row in converted_points.values()})
            or validation.grid_count != len({str(row["grid_hash"]) for row in grids.values()})
        ):
            raise SourceSchemaError("migrated row-count parity failed")
        try:
            os.rename(stage, target)
        except FileExistsError as error:
            raise SourceSchemaError(f"target database already exists: {target}") from error
    except BaseException:
        if stage_connection is not None:
            stage_connection.close()
        if source_connection is not None:
            source_connection.close()
        if stage.exists():
            stage.unlink()
        raise

    return SourceMigrationResult(
        source,
        target,
        validation.report_count,
        validation.point_count,
        validation.grid_count,
        validation.payload_count,
        "",
        _file_sha256(target),
        validation,
    )
