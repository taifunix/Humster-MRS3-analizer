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
    "optimizer_prepared_inputs", "equity_quality_metrics", "strategy_rejection_sources",
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
    "strategy_rejection_sources": "strategy_id",
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
    "strategy_rejection_sources": ("strategy_id", "source_kind", "reason_code"),
}
PREPARED = "optimizer_prepared_inputs"
RANGE_VALUES = 128
FETCH_BATCH = 4096
PREPARED_FETCH_BATCH = 16
MAX_FAST_VERIFY_RANGE_ROWS = 20_000_000
CAPACITY_RESERVE = 10 * 1024**3
MEMORY_LIMIT = "16GB"
FAST_VERIFY_TABLES = {"strategy_actions", "strategy_equity"}


class _FastVerifyUnsupported(Exception):
    pass


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


def _table_names(connection: duckdb.DuckDBPyConnection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "select table_name from information_schema.tables where table_schema = 'main'"
        ).fetchall()
    )


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
    legacy_selection_results = (
        table == "selection_results"
        and target_columns == (*columns, "equity_regime_json")
    )
    if columns != target_columns and not legacy_selection_results:
        raise ValueError(f"source/target column schema mismatch for {table}")
    selected = ", ".join(_ident(column) for column in target_columns)
    source_selected = ", ".join(_ident(column) for column in columns)
    if legacy_selection_results:
        source_selected += ", NULL"
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
                    f"select {source_selected} from origin.main.{_ident(table)} "
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
    target_columns = _table_columns(connection, "main", table)
    legacy_selection_results = (
        table == "selection_results"
        and target_columns == (*columns, "equity_regime_json")
    )
    if columns != target_columns and not legacy_selection_results:
        raise ValueError(f"source/target column schema mismatch for {table}")
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
    if legacy_selection_results and connection.execute(
        "select count(*) from main.selection_results where equity_regime_json is not null"
    ).fetchone()[0]:
        raise ValueError("legacy selection snapshots were backfilled during v9 migration")
    _progress(callback, {"phase": "verify", "table": table, "rows": checked})
    return checked


def _fast_column_signature(
    connection: duckdb.DuckDBPyConnection,
    catalog: str,
    table: str,
) -> tuple[tuple[object, ...], ...]:
    try:
        duckdb_rows = connection.execute(
            "select column_name, column_index, data_type, numeric_precision, numeric_scale "
            "from duckdb_columns() where database_name = ? and schema_name = 'main' "
            "and table_name = ? order by column_index",
            [catalog, table],
        ).fetchall()
        information_rows = connection.execute(
            "select column_name, ordinal_position, data_type, numeric_precision, numeric_scale, "
            "collation_name, is_generated, generation_expression "
            "from information_schema.columns where table_catalog = ? and table_schema = 'main' "
            "and table_name = ? order by ordinal_position",
            [catalog, table],
        ).fetchall()
    except duckdb.Error as exc:
        raise _FastVerifyUnsupported from exc
    if not duckdb_rows or len(duckdb_rows) != len(information_rows):
        raise _FastVerifyUnsupported
    signature: list[tuple[object, ...]] = []
    for duckdb_row, information_row in zip(duckdb_rows, information_rows):
        name, index, data_type, precision, scale = duckdb_row
        info_name, ordinal, info_type, info_precision, info_scale, collation, generated, expression = information_row
        if (
            name != info_name or int(index) != int(ordinal)
            or data_type != info_type or precision != info_precision or scale != info_scale
        ):
            raise _FastVerifyUnsupported
        normalized_type = str(data_type).upper()
        if (
            "FLOAT" in normalized_type or "DOUBLE" in normalized_type
            or normalized_type == "REAL" or collation is not None
        ):
            raise _FastVerifyUnsupported
        signature.append((
            str(name), str(data_type), precision, scale, collation,
            str(generated), expression,
        ))
    return tuple(signature)


def _fast_except_count(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: Sequence[str],
    left_catalog: str,
    right_catalog: str,
    low: object,
    high: object,
) -> int:
    lead = _ident("result_id")
    projection = ", ".join(_ident(column) for column in columns)
    return int(connection.execute(
        f"select count(*) from ("
        f"select {projection} from {_ident(left_catalog)}.main.{_ident(table)} "
        f"where {lead} between ? and ? except all "
        f"select {projection} from {_ident(right_catalog)}.main.{_ident(table)} "
        f"where {lead} between ? and ?"
        ") as difference",
        [low, high, low, high],
    ).fetchone()[0])


def _verify_fast_table(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    source_catalog: str,
    target_catalog: str,
    result_ranges: Sequence[tuple[object, object]],
    callback: Callable[[dict[str, object]], object] | None,
    *,
    max_ranges: int | None = None,
) -> int:
    source_schema = _fast_column_signature(connection, source_catalog, table)
    target_schema = _fast_column_signature(connection, target_catalog, table)
    if source_schema != target_schema:
        raise ValueError(f"source/target column schema mismatch for {table}")
    columns = tuple(sorted(str(column[0]) for column in source_schema))
    lead = _ident("result_id")
    table_ref = f"{{catalog}}.main.{_ident(table)}"
    if max_ranges is None:
        source_count, source_nulls = connection.execute(
            f"select count(*), count(*) filter (where {lead} is null) "
            f"from {table_ref.format(catalog=_ident(source_catalog))}"
        ).fetchone()
        target_count, target_nulls = connection.execute(
            f"select count(*), count(*) filter (where {lead} is null) "
            f"from {table_ref.format(catalog=_ident(target_catalog))}"
        ).fetchone()
        source_count, target_count = int(source_count), int(target_count)
        if int(source_nulls) or int(target_nulls):
            raise ValueError(f"NULL result_id rows cannot be ranged in {table}")
        if source_count != target_count:
            raise ValueError(f"row count mismatch in {table}")
    ranges = result_ranges[:max_ranges] if max_ranges is not None else result_ranges

    checked = 0
    range_count = len(ranges)
    for range_index, (low, high) in enumerate(ranges, 1):
        source_rows = _count_range(connection, table, low, high)
        target_rows = int(connection.execute(
            f"select count(*) from main.{_ident(table)} where {lead} between ? and ?",
            [low, high],
        ).fetchone()[0])
        if source_rows != target_rows:
            raise ValueError(f"range row count mismatch in {table}")
        if max(source_rows, target_rows) > MAX_FAST_VERIFY_RANGE_ROWS:
            raise ValueError(
                f"result_id range exceeds fast verification limit in {table}: "
                f"{MAX_FAST_VERIFY_RANGE_ROWS} rows"
            )
        if (
            _fast_except_count(connection, table, columns, source_catalog, target_catalog, low, high)
            or _fast_except_count(connection, table, columns, target_catalog, source_catalog, low, high)
        ):
            raise ValueError(f"EXCEPT ALL mismatch in {table}")
        checked += source_rows
        _progress(callback, {
            "phase": "verify_range", "table": table,
            "range_index": range_index, "range_count": range_count,
            "result_id_low": low, "result_id_high": high,
            "rows": source_rows, "checked_rows": checked,
        })
    if max_ranges is None and checked != source_count:
        raise ValueError(f"result_id range count coverage mismatch in {table}")
    _progress(callback, {"phase": "verify", "table": table, "rows": checked})
    return checked


def verify_existing_candidate(
    source_path: Path | str,
    candidate_path: Path | str,
    output_path: Path | str | None = None,
    *,
    workers: int,
    spill_parent: Path | str | None = None,
    progress_callback: Callable[[dict[str, object]], object] | None = None,
    smoke_ranges: int | None = None,
) -> dict[str, object]:
    """Read-only compare an existing candidate; smoke mode never publishes."""
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if smoke_ranges is not None and (
        isinstance(smoke_ranges, bool) or not isinstance(smoke_ranges, int) or smoke_ranges < 1
    ):
        raise ValueError("smoke_ranges must be a positive integer")
    if smoke_ranges is not None and output_path is not None:
        raise ValueError("smoke mode cannot publish an output")
    if smoke_ranges is None and output_path is None:
        raise ValueError("output_path is required outside smoke mode")
    source = _source_path(Path(source_path))
    candidate_input = Path(candidate_path).expanduser()
    if candidate_input.is_symlink():
        raise ValueError("candidate must not be a symlink")
    candidate = _source_path(candidate_input)
    if os.path.samefile(source, candidate):
        raise ValueError("source and candidate alias the same database")
    output = _output_path(Path(output_path), candidate) if output_path is not None else None
    c_temp = Path(tempfile.gettempdir()).resolve()
    if candidate.drive.upper() != c_temp.drive.upper():
        raise ValueError("candidate and publication output must be on the C TEMP volume")
    if output is not None and output.drive.upper() != candidate.drive.upper():
        raise ValueError("candidate and publication output must be on the C TEMP volume")
    if output is not None and candidate.stat().st_dev != output.parent.stat().st_dev:
        raise ValueError("publication output must be on the candidate volume for hardlink publication")

    spill_root = Path(spill_parent).expanduser().resolve(strict=True) if spill_parent else c_temp
    if not spill_root.is_dir():
        raise ValueError("spill parent must be an existing directory")
    spill = Path(tempfile.mkdtemp(prefix="mrs3-fast-verify-", dir=str(spill_root))).resolve()
    started = time.monotonic()
    try:
        _refuse_nonzero_wal(source)
        _refuse_nonzero_wal(candidate)
        source_before, candidate_before = _stat(source), _stat(candidate)
        source_wal_before, candidate_wal_before = _wal_state(source), _wal_state(candidate)
        source_hash_before: str | None = None
        candidate_hash_before: str | None = None
        if smoke_ranges is None:
            _progress(progress_callback, {"phase": "snapshot", "file": "source", "state": "hash_before"})
            source_hash_before = _sha256(source)
            _progress(progress_callback, {"phase": "snapshot", "file": "candidate", "state": "hash_before"})
            candidate_hash_before = _sha256(candidate)

        with duckdb.connect(str(source), read_only=True) as source_connection:
            source_version = require_performance_v2_readable(source_connection)
            if source_version not in {8, 9}:
                raise ValueError("source schema version must be 8 or 9")
            _unknown_catalog_objects(source_connection)
            source_markers = _markers(source_connection)
            source_tables = _table_names(source_connection)
            source_signature = _catalog_signature(source_connection)

        with duckdb.connect(str(candidate), read_only=True) as connection:
            _configure_connection(connection, workers, spill)
            target_version = require_performance_v2_readable(connection)
            if target_version not in {8, 9} or target_version < source_version:
                raise ValueError("candidate schema version must be 8 or 9 and no older than source")
            _unknown_catalog_objects(connection)
            target_markers = _markers(connection)
            target_tables = _table_names(connection)
            target_signature = _catalog_signature(connection)
            if target_signature["sequences"] != source_signature["sequences"]:
                raise ValueError("source/candidate sequence state differs")
            if target_version == source_version and target_signature != source_signature:
                raise ValueError("source/candidate catalog signatures differ")
            if target_version == 9 and source_version == 8:
                if target_tables != source_tables | {"strategy_rejection_sources"}:
                    raise ValueError("source/candidate catalog tables differ")
            elif target_tables != source_tables:
                raise ValueError("source/candidate catalog tables differ")
            if target_markers != {**source_markers, "schema_version": str(target_version)}:
                raise ValueError("database markers or instance identity differ")

            source_catalog = "origin"
            target_catalog = str(connection.execute("select current_database()").fetchone()[0])
            connection.execute(f"attach {_path_sql(source)} as origin (read_only)")
            result_ranges = tuple(_ranges(connection, "strategy_results"))
            counts: dict[str, int | None] = {table: None for table in TABLES}
            verified_counts: dict[str, int | None] = {table: None for table in TABLES}
            verified_ranges: dict[str, int] = {}
            if smoke_ranges is None:
                counts = {table: 0 for table in TABLES}
                verified_counts = {table: 0 for table in TABLES}
            for table in TABLES:
                if table not in source_tables:
                    continue
                if smoke_ranges is not None:
                    if table in FAST_VERIFY_TABLES:
                        try:
                            verified_counts[table] = _verify_fast_table(
                                connection, table, source_catalog, target_catalog,
                                result_ranges, progress_callback, max_ranges=smoke_ranges,
                            )
                        except _FastVerifyUnsupported as exc:
                            raise ValueError(f"smoke mode cannot verify SQL columns in {table}") from exc
                        verified_ranges[table] = min(smoke_ranges, len(result_ranges))
                    continue
                if table in FAST_VERIFY_TABLES:
                    try:
                        verified_counts[table] = _verify_fast_table(
                            connection, table, source_catalog, target_catalog,
                            result_ranges, progress_callback,
                        )
                    except _FastVerifyUnsupported:
                        _progress(progress_callback, {
                            "phase": "verify_fallback", "table": table,
                            "method": "typed_python",
                        })
                        verified_counts[table] = _verify_table(
                            connection, table, progress_callback, result_ranges,
                        )
                else:
                    verified_counts[table] = _verify_table(
                        connection, table, progress_callback, result_ranges,
                    )
                counts[table] = verified_counts[table]
            if smoke_ranges is None:
                for table in target_tables - source_tables:
                    if table == "schema_info":
                        continue
                    target_rows = int(connection.execute(
                        f"select count(*) from main.{_ident(table)}"
                    ).fetchone()[0])
                    if target_rows:
                        raise ValueError(f"unexpected rows in candidate-only table {table}")
            connection.execute("detach origin")

        _refuse_nonzero_wal(source)
        _refuse_nonzero_wal(candidate)
        source_after, candidate_after = _stat(source), _stat(candidate)
        source_wal_after, candidate_wal_after = _wal_state(source), _wal_state(candidate)
        source_hash_after: str | None = None
        candidate_hash_after: str | None = None
        if smoke_ranges is None:
            source_hash_after, candidate_hash_after = _sha256(source), _sha256(candidate)
        if (
            source_before != source_after or source_wal_before != source_wal_after
            or (smoke_ranges is None and source_hash_before != source_hash_after)
        ):
            raise ValueError("source database changed during verification")
        if (
            candidate_before != candidate_after or candidate_wal_before != candidate_wal_after
            or (smoke_ranges is None and candidate_hash_before != candidate_hash_after)
        ):
            raise ValueError("candidate database changed during verification")

        if output is not None:
            os.link(candidate, output)
        return {
            "source": {"path": str(source), **source_before, "sha256": source_hash_before, "wal": source_wal_before},
            "source_after": {**source_after, "sha256": source_hash_after, "wal": source_wal_after},
            "candidate": {"path": str(candidate), **candidate_before, "sha256": candidate_hash_before, "wal": candidate_wal_before},
            "candidate_after": {**candidate_after, "sha256": candidate_hash_after, "wal": candidate_wal_after},
            "source_schema_version": source_version,
            "candidate_schema_version": target_version,
            "output": {"path": str(output), "hardlink_to_candidate": True} if output else None,
            "publication": "hardlink_alias_not_backup" if output else None,
            "table_counts": counts,
            "verified_table_counts": verified_counts,
            "verified_ranges": verified_ranges,
            "partial": smoke_ranges is not None,
            "verification_scope": "fast_smoke" if smoke_ranges is not None else "full_database",
            "smoke_range_limit": smoke_ranges,
            "content_hashes_checked": smoke_ranges is None,
            "workers": workers,
            "memory_limit": MEMORY_LIMIT,
            "range_leading_key_limit": RANGE_VALUES,
            "fast_verify_range_row_limit": MAX_FAST_VERIFY_RANGE_ROWS,
            "source_candidate_unchanged": True,
            "duration_seconds": round(time.monotonic() - started, 6),
        }
    finally:
        resolved_spill = spill.resolve(strict=False)
        if resolved_spill.parent == spill_root and resolved_spill.name.startswith("mrs3-fast-verify-"):
            shutil.rmtree(resolved_spill, ignore_errors=True)


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
    source_tables: frozenset[str]
    source_version: int
    with duckdb.connect(str(source), read_only=True) as connection:
        source_version = require_performance_v2_readable(connection)
        expected_nullable = "NO" if source_version in {5, 6} else "YES"
        if _commission_nullable(connection) != expected_nullable:
            raise ValueError("source commission_rate nullability does not match schema version")
        _unknown_catalog_objects(connection)
        source_signature = _catalog_signature(connection)
        source_markers = _markers(connection)
        source_tables = _table_names(connection)
    copy_tables = tuple(table for table in TABLES if table in source_tables)

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
        if _catalog_signature(target) != source_signature:
            raise ValueError("native schema copy changed the source catalog")
        if _commission_nullable(target) != ("NO" if source_version in {5, 6} else "YES"):
            raise ValueError("native schema copy changed commission_rate nullability")
        initialize_performance_v2(target, create_if_missing=False)
        if require_performance_v2_readable(target) != 9:
            raise ValueError("target did not reach schema version 9")
        if _commission_nullable(target) != "YES":
            raise ValueError("target commission_rate is not nullable in schema version 9")
        target_signature = _catalog_signature(target)
        if any(int(target.execute(f"select count(*) from main.{_ident(table)}").fetchone()[0]) for table in TABLES):
            raise ValueError("target was not empty before migration")
        target.execute(f"attach {source_sql} as origin (read_only)")
        result_ranges = tuple(_ranges(target, "strategy_results"))
        counts = {table: 0 for table in TABLES}
        for table in copy_tables:
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
            if require_performance_v2_readable(check) != 9:
                raise ValueError("candidate is not a readable v9 database")
            if _commission_nullable(check) != "YES":
                raise ValueError("candidate commission_rate is not nullable in schema version 9")
            if _markers(check) != {**source_markers, "schema_version": "9"}:
                raise ValueError("database markers were not preserved")
            if _catalog_signature(check) != target_signature:
                raise ValueError("database catalog changed during compaction")
            check.execute(f"attach {source_sql} as origin (read_only)")
            result_ranges = tuple(_ranges(check, "strategy_results"))
            target_catalog = str(check.execute("select current_database()").fetchone()[0])
            verified_counts = {table: 0 for table in TABLES}
            for table in copy_tables:
                if table in FAST_VERIFY_TABLES:
                    try:
                        verified_counts[table] = _verify_fast_table(
                            check, table, "origin", target_catalog,
                            result_ranges, progress_callback,
                        )
                    except _FastVerifyUnsupported:
                        _progress(progress_callback, {
                            "phase": "verify_fallback", "table": table,
                            "method": "typed_python",
                        })
                        verified_counts[table] = _verify_table(
                            check, table, progress_callback, result_ranges,
                        )
                else:
                    verified_counts[table] = _verify_table(
                        check, table, progress_callback, result_ranges,
                    )
            for table in set(TABLES) - set(copy_tables):
                target_count = int(check.execute(f"select count(*) from main.{_ident(table)}").fetchone()[0])
                if target_count:
                    raise ValueError(f"unexpected rows in schema-upgraded table {table}")
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
            "target": {"path": str(output), **stage_stat, "sha256": target_hash, "schema_version": 9},
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


__all__ = ["compact_performance_v2", "verify_existing_candidate"]
