"""Build and verify a lossless Performance v2 database candidate."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import math
import os
import shutil
import tempfile
import time
from typing import Callable, Iterable, Sequence
import uuid

import duckdb

from .performance_v2_optimizer import (
    EXPECTED_UNAVAILABILITY_REASONS,
    decode_prepared_storage,
    encode_prepared_storage,
)
from .performance_v2_store import (
    initialize_performance_v2,
    require_performance_v2_readable,
)


TABLES = (
    "strategies", "analysis_plateaus", "strategy_orders", "strategy_results",
    "strategy_actions", "strategy_equity", "window_metrics", "import_runs",
    "import_files", "selection_runs", "selection_results",
    "selection_review_imports", "selection_review_rows", "strategy_tags",
    "optimizer_prepared_inputs", "equity_quality_metrics",
)
TABLE_LEADING_KEY = {
    "strategies": "strategy_id",
    "analysis_plateaus": "analysis_run_id",
    "strategy_orders": "strategy_id",
    "strategy_results": "result_id",
    "strategy_actions": "result_id",
    "strategy_equity": "result_id",
    "window_metrics": "result_id",
    "import_runs": "import_run_id",
    "import_files": "import_file_id",
    "selection_runs": "selection_run_id",
    "selection_results": "selection_run_id",
    "selection_review_imports": "review_import_id",
    "selection_review_rows": "review_import_id",
    "strategy_tags": "strategy_id",
    "optimizer_prepared_inputs": "result_id",
    "equity_quality_metrics": "result_id",
}
TABLE_PRIMARY_KEY = {
    "strategies": ("strategy_id",),
    "analysis_plateaus": ("analysis_run_id", "plateau_id"),
    "strategy_orders": ("strategy_id", "order_id"),
    "strategy_results": ("result_id",),
    "strategy_actions": ("result_id", "action_index"),
    "strategy_equity": ("result_id", "sample_index"),
    "window_metrics": (
        "result_id", "requested_start_utc", "requested_end_utc", "metrics_version",
    ),
    "import_runs": ("import_run_id",),
    "import_files": ("import_file_id",),
    "selection_runs": ("selection_run_id",),
    "selection_results": ("selection_run_id", "strategy_id"),
    "selection_review_imports": ("review_import_id",),
    "selection_review_rows": ("review_import_id", "strategy_id"),
    "strategy_tags": ("strategy_id", "tag"),
    "optimizer_prepared_inputs": ("result_id",),
    "equity_quality_metrics": ("result_id", "algo_version"),
}
PREPARED = "optimizer_prepared_inputs"
RANGE_VALUES = 128
FETCH_BATCH = 4096
PREPARED_FETCH_BATCH = 16
CAPACITY_RESERVE = 10 * 1024**3
MEMORY_LIMIT = "16GB"


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _path_sql(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _source_path(path: Path) -> Path:
    source = path.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("source must be an existing DuckDB file")
    return source


def _output_path(path: Path, source: Path) -> Path:
    requested = path.expanduser()
    if requested.is_symlink():
        raise FileExistsError(requested)
    output = requested.resolve(strict=False)
    if output == source:
        raise ValueError("output aliases the source database")
    if output.exists():
        try:
            if os.path.samefile(output, source):
                raise ValueError("output aliases the source database")
        except FileNotFoundError:
            pass
        raise FileExistsError(output)
    if output.suffix.lower() not in {".duckdb", ".db"}:
        raise ValueError("output must have a .duckdb or .db suffix")
    if not output.parent.is_dir():
        raise ValueError("output parent directory must already exist")
    return output


def _stat(path: Path) -> dict[str, object]:
    info = path.stat()
    return {
        "file_id": [int(info.st_dev), int(info.st_ino)],
        "size_bytes": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "blocks": int(getattr(info, "st_blocks", 0)),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wal_paths(source: Path) -> tuple[Path, ...]:
    candidates = (Path(str(source) + ".wal"), Path(str(source) + "-wal"))
    return tuple(dict.fromkeys(candidates))


def _wal_state(source: Path) -> list[dict[str, object]]:
    return [
        {"path": str(path), "size_bytes": int(path.stat().st_size)}
        for path in _wal_paths(source) if path.exists()
    ]


def _refuse_nonzero_wal(source: Path) -> None:
    if any(item["size_bytes"] for item in _wal_state(source)):
        raise ValueError("source has a nonzero WAL sidecar")


def _capacity(*paths: Path) -> None:
    for path in paths:
        if shutil.disk_usage(path).free < CAPACITY_RESERVE:
            raise OSError(f"insufficient free space on {path}: 10 GiB reserve required")


def _configure_connection(
    connection: duckdb.DuckDBPyConnection,
    workers: int,
    spill: Path,
) -> None:
    connection.execute(f"set memory_limit = '{MEMORY_LIMIT}'")
    connection.execute(f"set threads = {workers}")
    connection.execute(f"set temp_directory = {_path_sql(spill)}")
    actual_threads = int(connection.execute("select current_setting('threads')").fetchone()[0])
    actual_memory = str(connection.execute("select current_setting('memory_limit')").fetchone()[0]).strip()
    if actual_threads != workers or not actual_memory or actual_memory in {"0", "0 B", "0B"}:
        raise RuntimeError("DuckDB verification settings were not applied")


def _table_columns(connection: duckdb.DuckDBPyConnection, catalog: str, table: str) -> tuple[str, ...]:
    prefix = "main" if catalog == "main" else f"{_ident(catalog)}.main"
    return tuple(
        str(row[0])
        for row in connection.execute(f"describe {prefix}.{_ident(table)}").fetchall()
    )


def _markers(connection: duckdb.DuckDBPyConnection, catalog: str = "main") -> dict[str, str]:
    prefix = "main" if catalog == "main" else f"{_ident(catalog)}.main"
    return {
        str(key): str(value)
        for key, value in connection.execute(
            f"select key, value from {prefix}.schema_info"
        ).fetchall()
    }


def _unknown_catalog_objects(connection: duckdb.DuckDBPyConnection) -> None:
    tables = {
        f"{row[0]}.{row[1]}" for row in connection.execute(
            "select schema_name, table_name from duckdb_tables() "
            "where not internal and not temporary "
            "and schema_name not in ('information_schema','pg_catalog')"
        ).fetchall()
    }
    allowed_tables = {f"main.{table}" for table in (*TABLES, "schema_info")}
    unknown_tables = tables - allowed_tables
    schemas = {
        str(row[0]) for row in connection.execute(
            "select schema_name from information_schema.schemata"
        ).fetchall()
        if str(row[0]) not in {"main", "information_schema", "pg_catalog"}
    }
    views = {
        str(row[0]) for row in connection.execute(
            "select schema_name || '.' || view_name from duckdb_views() "
            "where not internal and not temporary and schema_name not in ('information_schema','pg_catalog')"
        ).fetchall()
    }
    macros = {
        str(row[0]) for row in connection.execute(
            "select schema_name || '.' || function_name from duckdb_functions() "
            "where function_type = 'macro' and not internal and schema_name not in ('information_schema','pg_catalog')"
        ).fetchall()
    }
    types = {
        str(row[0]) for row in connection.execute(
            "select schema_name || '.' || type_name from duckdb_types() "
            "where not internal and schema_name not in ('information_schema','pg_catalog')"
        ).fetchall()
    }
    if unknown_tables or schemas or views or macros or types:
        raise ValueError("source has unknown catalog objects")


def _commission_nullable(connection: duckdb.DuckDBPyConnection) -> str:
    row = connection.execute(
        "select is_nullable from information_schema.columns "
        "where table_schema = 'main' and table_name = 'strategy_results' "
        "and column_name = 'commission_rate'"
    ).fetchone()
    if row is None:
        raise ValueError("strategy_results commission_rate is missing")
    return str(row[0]).upper()


def _catalog_signature(connection: duckdb.DuckDBPyConnection) -> dict[str, object]:
    columns = [
        list(row) for row in connection.execute(
            "select table_name, column_name, data_type, is_nullable, column_default "
            "from information_schema.columns where table_schema='main' "
            "order by table_name, ordinal_position"
        ).fetchall()
    ]
    for row in columns:
        if row[0] == "strategy_results" and row[1] == "commission_rate":
            row[3] = "YES"
    constraints = [
        [row[0], row[1], row[2], row[3], row[4], row[5], row[6]]
        for row in connection.execute(
            "select table_name, constraint_type, constraint_text, expression, "
            "constraint_column_names, referenced_table, referenced_column_names "
            "from duckdb_constraints() where schema_name='main' "
            "order by table_name, constraint_index"
        ).fetchall()
    ]
    # The only commissioned v6->v7/v8 catalog change is commission_rate NULLability.
    constraints = [
        row for row in constraints
        if not (
            row[0] == "strategy_results" and row[1] == "NOT NULL"
            and row[4] == ["commission_rate"]
        )
    ]
    constraints.sort(key=lambda row: tuple(str(value) for value in row))
    indexes = [
        list(row) for row in connection.execute(
            "select table_name, index_name, sql from duckdb_indexes() "
            "where schema_name='main' and sql is not null order by table_name, index_name"
        ).fetchall()
    ]
    sequences = [
        list(row) for row in connection.execute(
            "select schema_name, sequence_name, start_value, min_value, max_value, "
            "increment_by, cycle, last_value from duckdb_sequences() "
            "where schema_name='main' order by sequence_name"
        ).fetchall()
    ]
    return {"columns": columns, "constraints": constraints, "indexes": indexes, "sequences": sequences}


def _ranges(connection: duckdb.DuckDBPyConnection, table: str) -> Iterable[tuple[object, object]]:
    lead = _ident(TABLE_LEADING_KEY[table])
    previous: object | None = None
    while True:
        if previous is None:
            sql = (
                f"select distinct {lead} from origin.main.{_ident(table)} "
                f"order by {lead} limit {RANGE_VALUES}"
            )
            rows = connection.execute(sql).fetchall()
        else:
            sql = (
                f"select distinct {lead} from origin.main.{_ident(table)} where {lead} > ? "
                f"order by {lead} limit {RANGE_VALUES}"
            )
            rows = connection.execute(sql, [previous]).fetchall()
        if not rows:
            return
        values = [row[0] for row in rows]
        current = values[-1]
        yield values[0], current
        previous = current


def _count_range(connection: duckdb.DuckDBPyConnection, table: str, low: object, high: object) -> int:
    lead = _ident(TABLE_LEADING_KEY[table])
    return int(connection.execute(
        f"select count(*) from origin.main.{_ident(table)} where {lead} between ? and ?",
        [low, high],
    ).fetchone()[0])


def _progress(callback: Callable[[dict[str, object]], object] | None, event: dict[str, object]) -> None:
    if callback is not None:
        callback(event)


def _transform_prepared(row: tuple[object, ...], columns: tuple[str, ...]) -> tuple[tuple[object, ...], int, int]:
    positions = {name: columns.index(name) for name in columns}
    payload_index = positions["prepared_json"]
    payload = row[payload_index]
    if payload is None:
        if (
            row[positions["availability_status"]] != "UNAVAILABLE"
            or row[positions["unavailable_reason"]] not in EXPECTED_UNAVAILABILITY_REASONS
        ):
            raise ValueError("prepared unavailable row is invalid")
        return row, 0, 0
    if row[positions["availability_status"]] != "AVAILABLE":
        raise ValueError("prepared unavailable row has a payload")
    decoded = decode_prepared_storage(str(payload))
    stored = encode_prepared_storage(decoded)
    updated = list(row)
    updated[payload_index] = stored
    return tuple(updated), len(decoded.encode("utf-8")), len(stored.encode("utf-8"))


def _copy_prepared_range(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: tuple[str, ...],
    low: object,
    high: object,
    workers: int,
    spill_root: Path,
    output_parent: Path,
    stats: dict[str, int],
) -> int:
    lead = _ident(TABLE_LEADING_KEY[table])
    selected = ", ".join(_ident(column) for column in columns)
    query = (
        f"select {selected} from origin.main.{_ident(table)} "
        f"where {lead} between ? and ? order by {lead}"
    )
    insert = f"insert into {_ident(table)} ({selected}) values ({', '.join('?' for _ in columns)})"
    count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        cursor = connection.cursor()
        cursor.execute(query, [low, high])
        while True:
            rows = cursor.fetchmany(PREPARED_FETCH_BATCH)
            if not rows:
                break
            _capacity(spill_root, output_parent)
            transformed = list(executor.map(lambda item: _transform_prepared(item, columns), rows))
            connection.execute("begin transaction")
            try:
                connection.executemany(insert, [item[0] for item in transformed])
                connection.execute("commit")
            except BaseException:
                connection.execute("rollback")
                raise
            for _, raw_size, stored_size in transformed:
                if raw_size:
                    stats["prepared_rows"] += 1
                    stats["raw_utf8_bytes"] += raw_size
                    stats["encoded_storage_bytes"] += stored_size
                    bucket = "<1KiB" if raw_size < 1024 else "1KiB-1MiB" if raw_size < 1024**2 else ">=1MiB"
                    stats[f"raw_size_{bucket}"] = stats.get(f"raw_size_{bucket}", 0) + 1
            count += len(rows)
            _capacity(spill_root, output_parent)
    return count


def _copy_table(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    workers: int,
    spill_root: Path,
    output_parent: Path,
    stats: dict[str, int],
    callback: Callable[[dict[str, object]], object] | None,
    result_ranges: Sequence[tuple[object, object]] | None = None,
) -> int:
    columns = _table_columns(connection, "origin", table)
    target_columns = _table_columns(connection, "main", table)
    if columns != target_columns:
        raise ValueError(f"source/target column schema mismatch for {table}")
    selected = ", ".join(_ident(column) for column in columns)
    lead = _ident(TABLE_LEADING_KEY[table])
    copied = 0
    ranges = (
        result_ranges
        if TABLE_LEADING_KEY[table] == "result_id" and result_ranges is not None
        else _ranges(connection, table)
    )
    for low, high in ranges:
        _capacity(spill_root, output_parent)
        if table == PREPARED:
            count = _copy_prepared_range(
                connection, table, columns, low, high, workers,
                spill_root, output_parent, stats,
            )
        else:
            count = _count_range(connection, table, low, high)
            connection.execute("begin transaction")
            try:
                connection.execute(
                    f"insert into {_ident(table)} ({selected}) "
                    f"select {selected} from origin.main.{_ident(table)} "
                    f"where {lead} between ? and ?",
                    [low, high],
                )
                connection.execute("commit")
            except BaseException:
                connection.execute("rollback")
                raise
            _capacity(spill_root, output_parent)
        copied += count
        _progress(callback, {"phase": "copy", "table": table, "rows": copied})
    return copied


def _equal(left: object, right: object) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) and math.isnan(right):
            return True
        if left == right == 0.0:
            return math.copysign(1.0, left) == math.copysign(1.0, right)
    return left == right


def _compare_rows(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: Sequence[str],
    low: object,
    high: object,
) -> int:
    if not columns:
        return 0
    selected = ", ".join(_ident(column) for column in columns)
    order = ", ".join(_ident(column) for column in TABLE_PRIMARY_KEY[table])
    lead = _ident(TABLE_LEADING_KEY[table])
    sql = (
        f"select {selected} from {{catalog}}.{_ident(table)} "
        f"where {lead} between ? and ? order by {order}"
    )
    source = connection.cursor()
    target = connection.cursor()
    source.execute(sql.format(catalog="origin.main"), [low, high])
    target.execute(sql.format(catalog="main"), [low, high])
    count = 0
    while True:
        left = source.fetchmany(FETCH_BATCH)
        right = target.fetchmany(FETCH_BATCH)
        if len(left) != len(right):
            raise ValueError(f"typed row count mismatch in {table}")
        if not left:
            return count
        for left_row, right_row in zip(left, right):
            if len(left_row) != len(right_row) or any(not _equal(a, b) for a, b in zip(left_row, right_row)):
                raise ValueError(f"typed row mismatch in {table}")
        count += len(left)


def _verify_table(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    callback: Callable[[dict[str, object]], object] | None,
    result_ranges: Sequence[tuple[object, object]] | None = None,
) -> int:
    columns = _table_columns(connection, "origin", table)
    payload_index = columns.index("prepared_json") if table == PREPARED else -1
    typed_columns = tuple(column for index, column in enumerate(columns) if index != payload_index)
    source_count = int(connection.execute(f"select count(*) from origin.main.{_ident(table)}").fetchone()[0])
    target_count = int(connection.execute(f"select count(*) from main.{_ident(table)}").fetchone()[0])
    if source_count != target_count:
        raise ValueError(f"row count mismatch in {table}")
    checked = 0
    payload_checked = 0
    ranges = (
        result_ranges
        if TABLE_LEADING_KEY[table] == "result_id" and result_ranges is not None
        else _ranges(connection, table)
    )
    for low, high in ranges:
        checked += _compare_rows(connection, table, typed_columns, low, high)
        if table == PREPARED:
            lead = _ident(TABLE_LEADING_KEY[table])
            source = connection.cursor()
            target = connection.cursor()
            query = (
                f"select {lead}, prepared_json from {{catalog}}.{_ident(table)} "
                f"where {lead} between ? and ? order by {lead}"
            )
            source.execute(query.format(catalog="origin.main"), [low, high])
            target.execute(query.format(catalog="main"), [low, high])
            while True:
                left = source.fetchmany(PREPARED_FETCH_BATCH)
                right = target.fetchmany(PREPARED_FETCH_BATCH)
                if len(left) != len(right):
                    raise ValueError("prepared payload row count mismatch")
                if not left:
                    break
                for (left_id, left_payload), (right_id, right_payload) in zip(left, right):
                    if left_id != right_id:
                        raise ValueError("prepared payload identity mismatch")
                    if left_payload is None or right_payload is None:
                        if left_payload != right_payload:
                            raise ValueError("prepared payload nullability mismatch")
                    elif (
                        decode_prepared_storage(str(left_payload)).encode("utf-8")
                        != decode_prepared_storage(str(right_payload)).encode("utf-8")
                    ):
                        raise ValueError("prepared decoded payload mismatch")
                    payload_checked += 1
    if checked != source_count:
        raise ValueError(f"typed verification coverage mismatch in {table}")
    if table == PREPARED and payload_checked != source_count:
        raise ValueError("prepared payload verification coverage mismatch")
    _progress(callback, {"phase": "verify", "table": table, "rows": checked})
    return checked


def _cleanup(path: Path, output_parent: Path) -> None:
    if path.parent.resolve() != output_parent.resolve() or not path.name.startswith("."):
        raise RuntimeError("refusing to clean an unexpected staging path")
    for candidate in (path, Path(str(path) + ".wal"), Path(str(path) + "-wal")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def compact_performance_v2(
    source_path: Path | str,
    output_path: Path | str,
    *,
    workers: int,
    progress_callback: Callable[[dict[str, object]], object] | None = None,
) -> dict[str, object]:
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    source = _source_path(Path(source_path))
    output = _output_path(Path(output_path), source)
    _refuse_nonzero_wal(source)
    source_before = _stat(source)
    source_hash_before = _sha256(source)
    source_wal_before = _wal_state(source)
    source_signature: dict[str, object]
    source_markers: dict[str, str]
    source_version: int
    with duckdb.connect(str(source), read_only=True) as connection:
        source_version = require_performance_v2_readable(connection)
        expected_nullable = "NO" if source_version == 6 else "YES"
        if _commission_nullable(connection) != expected_nullable:
            raise ValueError("source commission_rate nullability does not match schema version")
        _unknown_catalog_objects(connection)
        source_signature = _catalog_signature(connection)
        source_markers = _markers(connection)

    stage = output.with_name(f".{output.name}.{uuid.uuid4().hex}.staging.duckdb")
    temp_root = Path(tempfile.gettempdir()).resolve()
    if output.parent.resolve().drive.upper() != temp_root.drive.upper():
        raise ValueError("output must be on the C TEMP volume")
    spill: Path | None = None
    target: duckdb.DuckDBPyConnection | None = None
    started = time.monotonic()
    published = False
    counts: dict[str, int] = {}
    verified_counts: dict[str, int] = {}
    payload_stats = {"prepared_rows": 0, "raw_utf8_bytes": 0, "encoded_storage_bytes": 0}
    try:
        _capacity(temp_root, output.parent)
        spill = Path(tempfile.mkdtemp(prefix="mrs3-compact-", dir=str(temp_root))).resolve()
        if not spill.is_relative_to(temp_root):
            raise RuntimeError("spill directory escaped the configured C TEMP root")
        target = duckdb.connect(str(stage))
        _configure_connection(target, workers, spill)
        source_sql = _path_sql(source)
        target.execute(f"attach {source_sql} as origin (read_only)")
        database_name = str(target.execute("select current_database()").fetchone()[0])
        target.execute(
            f"copy from database origin to {_ident(database_name)} (schema)"
        )
        target.execute("delete from main.schema_info")
        target.execute(
            "insert into main.schema_info (key, value) select key, value from origin.main.schema_info"
        )
        target.execute("detach origin")
        if require_performance_v2_readable(target) != source_version:
            raise ValueError("native schema copy changed the source schema version")
        if _commission_nullable(target) != ("NO" if source_version == 6 else "YES"):
            raise ValueError("native schema copy changed commission_rate nullability")
        initialize_performance_v2(target, create_if_missing=False)
        if require_performance_v2_readable(target) != 8:
            raise ValueError("target did not reach schema version 8")
        if _commission_nullable(target) != "YES":
            raise ValueError("target commission_rate is not nullable in schema version 8")
        if any(int(target.execute(f"select count(*) from main.{_ident(table)}").fetchone()[0]) for table in TABLES):
            raise ValueError("target was not empty before migration")
        target.execute(f"attach {source_sql} as origin (read_only)")
        result_ranges = tuple(_ranges(target, "strategy_results"))
        for table in TABLES:
            counts[table] = _copy_table(
                target, table, workers, spill, output.parent, payload_stats, progress_callback,
                result_ranges,
            )
        target.execute("detach origin")
        target.execute("checkpoint")
        target.close()
        target = None

        with duckdb.connect(str(stage), read_only=True) as check:
            _configure_connection(check, workers, spill)
            if require_performance_v2_readable(check) != 8:
                raise ValueError("candidate is not a readable v8 database")
            if _commission_nullable(check) != "YES":
                raise ValueError("candidate commission_rate is not nullable in schema version 8")
            if _markers(check) != {**source_markers, "schema_version": "8"}:
                raise ValueError("database markers were not preserved")
            if _catalog_signature(check) != source_signature:
                raise ValueError("database catalog changed during compaction")
            check.execute(f"attach {source_sql} as origin (read_only)")
            result_ranges = tuple(_ranges(check, "strategy_results"))
            for table in TABLES:
                verified_counts[table] = _verify_table(check, table, progress_callback, result_ranges)
            check.execute("detach origin")

        _refuse_nonzero_wal(source)
        source_after = _stat(source)
        source_hash_after = _sha256(source)
        source_wal_after = _wal_state(source)
        if (
            source_after != source_before
            or source_hash_after != source_hash_before
            or source_wal_after != source_wal_before
        ):
            raise ValueError("source database changed during compaction")
        _capacity(spill, output.parent)
        stage_stat = _stat(stage)
        target_hash = _sha256(stage)
        _refuse_nonzero_wal(stage)
        os.link(stage, output)
        published = True
        return {
            "source": {"path": str(source), **source_before, "sha256": source_hash_before, "wal": source_wal_before},
            "source_after": {**source_after, "sha256": source_hash_after, "wal": source_wal_after},
            "source_schema_version": source_version,
            "target": {"path": str(output), **stage_stat, "sha256": target_hash, "schema_version": 8},
            "table_counts": counts,
            "verified_table_counts": verified_counts,
            "prepared": payload_stats,
            "workers": workers,
            "memory_limit": MEMORY_LIMIT,
            "range_leading_key_limit": RANGE_VALUES,
            "prepared_fetch_batch": PREPARED_FETCH_BATCH,
            "source_stat_unchanged": True,
            "duration_seconds": round(time.monotonic() - started, 6),
        }
    finally:
        if target is not None:
            try:
                target.close()
            except Exception:
                pass
        if spill is not None:
            resolved = spill.resolve(strict=False)
            if resolved.parent == temp_root and resolved.name.startswith("mrs3-compact-"):
                shutil.rmtree(resolved, ignore_errors=True)
        if not published:
            _cleanup(stage, output.parent)
        else:
            try:
                stage.unlink()
            except OSError:
                pass


__all__ = ["compact_performance_v2"]
