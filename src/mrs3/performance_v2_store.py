from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import BinaryIO
from uuid import UUID, uuid4

import duckdb

from .config import PanelPathSettings, load_duckdb_import_settings, load_panel_path_settings


_SCHEMA_VERSION = "6"
_V5_SCHEMA_VERSION = "5"
_DATABASE_NAME = "strategy_performance.duckdb"
_MAX_WORKERS = 64
_DEFAULT_V1_PERFORMANCE_ROOT = PanelPathSettings().performance_db_root
_OPTIMIZER_SOURCE_METADATA_VERSION = 1
_PRICE_COST_SEMANTICS = "actual_fill_not_planned_position"


class PerformanceV2StoreError(ValueError):
    """Raised when a database is not the isolated Performance v2 schema."""


class PerformanceV2WriterLock:
    """Small cross-process lock for the v2 database writer."""

    def __init__(self, database_root: Path) -> None:
        self.database_root = Path(database_root).resolve()
        self.path = self.database_root / ".performance-v2.lock"
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "PerformanceV2WriterLock":
        # The import target gate owns directory creation policy. A bare lock
        # must never make a missing database root look initialized.
        if not self.database_root.is_dir():
            raise PerformanceV2StoreError("Performance v2 database root does not exist")
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise PerformanceV2StoreError("Performance v2 database writer is busy") from error
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


@dataclass(frozen=True, slots=True)
class PerformanceV2Config:
    database_root: Path
    workers: int = 16
    max_html_bytes: int = 67_108_864
    max_actions_per_report: int = 1_000_000
    v1_database_root: Path = _DEFAULT_V1_PERFORMANCE_ROOT
    strategy_root: Path | None = None

    def __post_init__(self) -> None:
        for name in ("database_root", "v1_database_root"):
            value = getattr(self, name)
            if not isinstance(value, Path):
                raise ValueError(f"unified_performance_v2.{name} must be a path")
            object.__setattr__(self, name, value.resolve())
        if self.strategy_root is not None:
            if not isinstance(self.strategy_root, Path):
                raise ValueError("unified_performance_v2.strategy_root must be a path")
            object.__setattr__(self, "strategy_root", self.strategy_root.resolve())
        for name in ("workers", "max_html_bytes", "max_actions_per_report"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"unified_performance_v2.{name} must be a positive integer")
        if self.workers > _MAX_WORKERS:
            object.__setattr__(self, "workers", _MAX_WORKERS)


def load_performance_v2_config(
    path: Path, *, v1_database_root: Path | None = None
) -> PerformanceV2Config:
    """Load only the additive v2 namespace from its dedicated configuration."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid config.performance.json") from error
    if not isinstance(raw, dict):
        raise ValueError("config.performance.json must be an object")
    section = raw.get("unified_performance_v2")
    if not isinstance(section, dict):
        raise ValueError("unified_performance_v2 must be an object")
    root = section.get("database_root")
    if not isinstance(root, str) or not root.strip():
        raise ValueError("unified_performance_v2.database_root must be a relative path")
    relative_root = Path(root.strip().replace("\\", "/"))
    if (
        relative_root.is_absolute()
        or ":" in relative_root.parts[0]
        or "." in relative_root.parts
        or ".." in relative_root.parts
    ):
        raise ValueError("unified_performance_v2.database_root must be a relative path")
    runtime_v1_root = (
        load_panel_path_settings(path.with_name("config.local.json")).performance_db_root
        if v1_database_root is None
        else v1_database_root
    )
    if not isinstance(runtime_v1_root, Path):
        raise ValueError("unified_performance_v2.v1_database_root must be a path")
    if not runtime_v1_root.is_absolute():
        runtime_v1_root = path.parent / runtime_v1_root
    strategy_root = section.get("strategy_root", "Output/strategies")
    if not isinstance(strategy_root, str) or not strategy_root.strip():
        raise ValueError("unified_performance_v2.strategy_root must be a relative path")
    relative_strategy_root = Path(strategy_root.strip().replace("\\", "/"))
    if (
        relative_strategy_root.is_absolute()
        or ":" in relative_strategy_root.parts[0]
        or "." in relative_strategy_root.parts
        or ".." in relative_strategy_root.parts
    ):
        raise ValueError("unified_performance_v2.strategy_root must be a relative path")
    return PerformanceV2Config(
        database_root=(path.parent / relative_root),
        workers=load_duckdb_import_settings(path.with_name("config.local.json")).workers,
        max_html_bytes=section.get("max_html_bytes", 67_108_864),
        max_actions_per_report=section.get("max_actions_per_report", 1_000_000),
        v1_database_root=runtime_v1_root,
        strategy_root=path.parent / relative_strategy_root,
    )


def performance_v2_database_path(config: PerformanceV2Config) -> Path:
    """Return the one v2 target without opening DuckDB or creating its root."""
    root = config.database_root.resolve()
    target = (root / _DATABASE_NAME).resolve()
    v1_root = config.v1_database_root.resolve()
    if (
        root.is_relative_to(v1_root)
        or v1_root.is_relative_to(root)
        or target.is_relative_to(v1_root)
        or v1_root.is_relative_to(target)
    ):
        raise ValueError("Performance v2 target overlaps the v1 performance root")
    if target.name.endswith(".performance-v6.duckdb") or not target.is_relative_to(root):
        raise ValueError("Performance v2 target is not inside its owned root")
    return target


_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS performance_v2_strategy_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS performance_v2_result_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS performance_v2_import_run_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS performance_v2_import_file_id_seq START 1;

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id BIGINT PRIMARY KEY DEFAULT nextval('performance_v2_strategy_id_seq'),
    strategy_name VARCHAR NOT NULL UNIQUE,
    symbol VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    timeframe VARCHAR NOT NULL,
    close_ma_len INTEGER NOT NULL CHECK (close_ma_len > 0),
    order_count INTEGER NOT NULL CHECK (order_count BETWEEN 1 AND 4),
    analysis_run_id VARCHAR NOT NULL,
    candidate_identity VARCHAR NOT NULL,
    lifecycle_status VARCHAR NOT NULL CHECK (lifecycle_status IN ('ACTIVE', 'DISCARDED')),
    current_result_id BIGINT,
    created_at_utc TIMESTAMPTZ NOT NULL,
    updated_at_utc TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_plateaus (
    analysis_run_id VARCHAR NOT NULL,
    plateau_id VARCHAR NOT NULL,
    plateau_point_count INTEGER NOT NULL CHECK (plateau_point_count > 0),
    plateau_total_trades INTEGER NOT NULL CHECK (plateau_total_trades >= 0),
    PRIMARY KEY (analysis_run_id, plateau_id)
);

CREATE TABLE IF NOT EXISTS strategy_orders (
    strategy_id BIGINT NOT NULL REFERENCES strategies(strategy_id),
    order_id INTEGER NOT NULL CHECK (order_id BETWEEN 1 AND 4),
    open_ma_len INTEGER NOT NULL CHECK (open_ma_len > 0),
    open_multiplier DECIMAL(38,12) NOT NULL,
    shift_bp INTEGER NOT NULL,
    lot_x DECIMAL(38,12) NOT NULL CHECK (lot_x > 0),
    analysis_run_id VARCHAR NOT NULL,
    plateau_id VARCHAR NOT NULL,
    base_point_trades INTEGER NOT NULL CHECK (base_point_trades >= 0),
    PRIMARY KEY (strategy_id, order_id),
    FOREIGN KEY (analysis_run_id, plateau_id) REFERENCES analysis_plateaus(analysis_run_id, plateau_id)
);

CREATE TABLE IF NOT EXISTS strategy_results (
    result_id BIGINT PRIMARY KEY DEFAULT nextval('performance_v2_result_id_seq'),
    strategy_id BIGINT NOT NULL UNIQUE REFERENCES strategies(strategy_id),
    report_start_utc TIMESTAMPTZ NOT NULL,
    report_end_utc TIMESTAMPTZ NOT NULL,
    exchange VARCHAR NOT NULL,
    commission_rate DECIMAL(38,12) NOT NULL,
    initial_balance DECIMAL(38,12) NOT NULL,
    final_balance DECIMAL(38,12) NOT NULL,
    total_pnl DECIMAL(38,12),
    total_pnl_pct DECIMAL(38,12),
    max_drawdown DECIMAL(38,12),
    max_drawdown_pct DECIMAL(38,12),
    total_fees DECIMAL(38,12),
    total_trades INTEGER,
    imported_at_utc TIMESTAMPTZ NOT NULL,
    reported_start_utc TIMESTAMPTZ,
    reported_end_utc TIMESTAMPTZ,
    listing_date_utc TIMESTAMPTZ,
    listing_date_raw VARCHAR,
    listing_date_source VARCHAR,
    effective_start_utc TIMESTAMPTZ,
    effective_end_utc TIMESTAMPTZ,
    warmup_hours INTEGER,
    excluded_trade_count INTEGER,
    exclusion_reason VARCHAR,
    optimizer_source_metadata_json VARCHAR,
    sizing_use_upnl BOOLEAN,
    sizing_use_frozen_balance BOOLEAN,
    sizing_use_fix BOOLEAN,
    sizing_balance_percentage_long DECIMAL(38,12),
    sizing_risk_long DECIMAL(38,12),
    sizing_max_balance DECIMAL(38,12),
    CHECK (report_end_utc >= report_start_utc)
);

CREATE TABLE IF NOT EXISTS strategy_actions (
    result_id BIGINT NOT NULL REFERENCES strategy_results(result_id),
    action_index INTEGER NOT NULL CHECK (action_index >= 0),
    timestamp_utc TIMESTAMPTZ NOT NULL,
    symbol VARCHAR NOT NULL,
    order_id INTEGER,
    action VARCHAR NOT NULL,
    size DECIMAL(38,12) NOT NULL,
    post_size DECIMAL(38,12) NOT NULL,
    post_side VARCHAR NOT NULL,
    pnl DECIMAL(38,12) NOT NULL,
    fee DECIMAL(38,12) NOT NULL,
    balance DECIMAL(38,12) NOT NULL,
    price DECIMAL(38,12),
    cost DECIMAL(38,12),
    raw_action_json VARCHAR,
    PRIMARY KEY (result_id, action_index)
);

CREATE TABLE IF NOT EXISTS strategy_equity (
    result_id BIGINT NOT NULL REFERENCES strategy_results(result_id),
    sample_index INTEGER NOT NULL CHECK (sample_index >= 0),
    timestamp_utc TIMESTAMPTZ NOT NULL,
    wallet DECIMAL(38,12) NOT NULL,
    equity DECIMAL(38,12) NOT NULL,
    PRIMARY KEY (result_id, sample_index)
);

CREATE TABLE IF NOT EXISTS window_metrics (
    result_id BIGINT NOT NULL REFERENCES strategy_results(result_id),
    requested_start_utc TIMESTAMPTZ NOT NULL,
    requested_end_utc TIMESTAMPTZ NOT NULL,
    metrics_version VARCHAR NOT NULL,
    effective_start_utc TIMESTAMPTZ,
    effective_end_utc TIMESTAMPTZ,
    availability_status VARCHAR NOT NULL,
    unavailable_reason VARCHAR,
    growth_factor DECIMAL(38,12),
    return_pct DECIMAL(38,12),
    daily_log_return DECIMAL(38,12),
    daily_growth_pct DECIMAL(38,12),
    max_drawdown_pct DECIMAL(38,12),
    return_dd_ratio DECIMAL(38,12),
    fees_pct DECIMAL(38,12),
    profit_factor DECIMAL(38,12),
    trade_count INTEGER,
    win_rate_pct DECIMAL(38,12),
    holding_seconds DECIMAL(38,12),
    time_in_market_pct DECIMAL(38,12),
    calculated_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (result_id, requested_start_utc, requested_end_utc, metrics_version),
    CHECK (requested_end_utc >= requested_start_utc)
);

CREATE TABLE IF NOT EXISTS import_runs (
    import_run_id BIGINT PRIMARY KEY DEFAULT nextval('performance_v2_import_run_id_seq'),
    source_inbox_sha256 VARCHAR NOT NULL UNIQUE,
    expected_report_count INTEGER NOT NULL CHECK (expected_report_count >= 0),
    imported_count INTEGER NOT NULL CHECK (imported_count >= 0),
    skipped_count INTEGER NOT NULL CHECK (skipped_count >= 0),
    rejected_count INTEGER NOT NULL CHECK (rejected_count >= 0),
    status VARCHAR NOT NULL,
    started_at_utc TIMESTAMPTZ NOT NULL,
    finished_at_utc TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS import_files (
    import_file_id BIGINT PRIMARY KEY DEFAULT nextval('performance_v2_import_file_id_seq'),
    import_run_id BIGINT NOT NULL REFERENCES import_runs(import_run_id),
    source_filename VARCHAR NOT NULL,
    source_html_sha256 VARCHAR NOT NULL,
    source_size_bytes BIGINT NOT NULL CHECK (source_size_bytes >= 0),
    action_count INTEGER,
    equity_sample_count INTEGER,
    status VARCHAR NOT NULL,
    error_message VARCHAR,
    UNIQUE (import_run_id, source_html_sha256)
);

CREATE INDEX IF NOT EXISTS strategy_results_strategy_id_idx ON strategy_results(strategy_id);
CREATE INDEX IF NOT EXISTS strategy_actions_result_timestamp_idx ON strategy_actions(result_id, timestamp_utc);
CREATE INDEX IF NOT EXISTS strategy_equity_result_timestamp_idx ON strategy_equity(result_id, timestamp_utc);

CREATE TABLE IF NOT EXISTS optimizer_prepared_inputs (
    result_id BIGINT PRIMARY KEY REFERENCES strategy_results(result_id),
    preparation_version VARCHAR NOT NULL CHECK (length(trim(preparation_version)) > 0),
    source_digest VARCHAR NOT NULL CHECK (length(trim(source_digest)) > 0),
    availability_status VARCHAR NOT NULL CHECK (availability_status IN ('AVAILABLE', 'UNAVAILABLE')),
    unavailable_reason VARCHAR,
    prepared_json VARCHAR,
    prepared_at_utc TIMESTAMPTZ NOT NULL,
    CHECK (
        (availability_status = 'AVAILABLE' AND unavailable_reason IS NULL AND prepared_json IS NOT NULL)
        OR (availability_status = 'UNAVAILABLE' AND unavailable_reason IN ('MISSING_TYPED_FACTS', 'UNSUPPORTED_SIZING', 'PREPARED_TOO_LARGE') AND prepared_json IS NULL)
    )
);
"""

_SELECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS selection_runs (
    selection_run_id VARCHAR PRIMARY KEY,
    database_instance_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    side VARCHAR NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    selection_contract_version VARCHAR NOT NULL,
    request_json VARCHAR NOT NULL,
    request_sha256 VARCHAR NOT NULL,
    config_json VARCHAR NOT NULL,
    config_sha256 VARCHAR NOT NULL,
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    representative_count INTEGER NOT NULL CHECK (representative_count >= 0),
    auto_finalist_count INTEGER NOT NULL CHECK (auto_finalist_count >= 0),
    top_n INTEGER NOT NULL CHECK (top_n > 0),
    workbook_sha256 VARCHAR NOT NULL,
    created_at_utc TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS selection_results (
    selection_run_id VARCHAR NOT NULL REFERENCES selection_runs(selection_run_id),
    strategy_id BIGINT NOT NULL,
    result_id_at_selection BIGINT NOT NULL,
    auto_status VARCHAR NOT NULL CHECK (auto_status IN ('FINALIST', 'RESERVE', 'ANALOG', 'FILTERED')),
    auto_score DOUBLE,
    auto_rank INTEGER CHECK (auto_rank IS NULL OR auto_rank > 0),
    auto_reason VARCHAR,
    analog_group_key VARCHAR,
    auto_analog_of_strategy_id BIGINT,
    prior_rejected BOOLEAN NOT NULL,
    stage_trace_json VARCHAR NOT NULL,
    PRIMARY KEY (selection_run_id, strategy_id)
);

CREATE TABLE IF NOT EXISTS selection_review_imports (
    review_import_id VARCHAR PRIMARY KEY,
    selection_run_id VARCHAR NOT NULL REFERENCES selection_runs(selection_run_id),
    workbook_sha256 VARCHAR NOT NULL UNIQUE,
    imported_at_utc TIMESTAMPTZ NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0)
);

CREATE TABLE IF NOT EXISTS selection_review_rows (
    review_import_id VARCHAR NOT NULL REFERENCES selection_review_imports(review_import_id),
    strategy_id BIGINT NOT NULL,
    user_status VARCHAR NOT NULL CHECK (user_status IN ('FINALIST', 'RESERVE', 'ANALOG', 'FILTERED', 'REJECTED')),
    user_rank INTEGER CHECK (user_rank IS NULL OR user_rank > 0),
    user_analog_of_strategy_id BIGINT,
    comment VARCHAR,
    PRIMARY KEY (review_import_id, strategy_id)
);

CREATE TABLE IF NOT EXISTS strategy_tags (
    strategy_id BIGINT NOT NULL REFERENCES strategies(strategy_id),
    tag VARCHAR NOT NULL CHECK (tag IN ('REJECTED', 'RETEST')),
    source VARCHAR NOT NULL,
    source_ref VARCHAR NOT NULL CHECK (length(trim(source_ref)) > 0),
    updated_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (strategy_id, tag)
);

CREATE INDEX IF NOT EXISTS selection_runs_pair_side_created_idx
ON selection_runs(symbol, side, created_at_utc);
CREATE INDEX IF NOT EXISTS selection_review_imports_run_imported_idx
ON selection_review_imports(selection_run_id, imported_at_utc);
CREATE INDEX IF NOT EXISTS strategy_tags_tag_idx ON strategy_tags(tag);
"""

_EQUITY_QUALITY_SCHEMA = """
CREATE TABLE equity_quality_metrics (
    result_id BIGINT NOT NULL,
    source_revision VARCHAR NOT NULL,
    algo_version VARCHAR NOT NULL,
    facts_json VARCHAR NOT NULL,
    facts_sha256 VARCHAR NOT NULL,
    calculated_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (result_id, algo_version)
)
"""

_V4_TAG_SCHEMA = """CREATE TABLE IF NOT EXISTS strategy_tags (
    strategy_id BIGINT NOT NULL REFERENCES strategies(strategy_id),
    tag VARCHAR NOT NULL CHECK (tag IN ('REJECTED', 'RETEST')),
    source VARCHAR NOT NULL,
    source_ref VARCHAR NOT NULL CHECK (length(trim(source_ref)) > 0),
    updated_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (strategy_id, tag)
);"""
_V3_TAG_SCHEMA = """CREATE TABLE IF NOT EXISTS strategy_tags (
    strategy_id BIGINT NOT NULL REFERENCES strategies(strategy_id),
    tag VARCHAR NOT NULL CHECK (tag = 'REJECTED'),
    source_review_import_id VARCHAR NOT NULL REFERENCES selection_review_imports(review_import_id),
    updated_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (strategy_id, tag)
);"""
if _SELECTION_SCHEMA.count(_V4_TAG_SCHEMA) != 1:
    raise RuntimeError("Performance v4 selection schema tag definition changed unexpectedly")
_SELECTION_SCHEMA_V3 = _SELECTION_SCHEMA.replace(
    _V4_TAG_SCHEMA,
    _V3_TAG_SCHEMA,
)

_V2_TABLE_NAMES = {
    "schema_info",
    "strategies",
    "analysis_plateaus",
    "strategy_orders",
    "strategy_results",
    "strategy_actions",
    "strategy_equity",
    "window_metrics",
    "import_runs",
    "import_files",
}

_V4_EXPECTED_TABLES = frozenset(
    ("main", name)
    for name in _V2_TABLE_NAMES | {
        "selection_runs",
        "selection_results",
        "selection_review_imports",
        "selection_review_rows",
        "strategy_tags",
    }
)
_V5_EXPECTED_TABLES = frozenset({("main", "optimizer_prepared_inputs")}) | _V4_EXPECTED_TABLES
_EXPECTED_TABLES = _V5_EXPECTED_TABLES | {("main", "equity_quality_metrics")}
_V2_EXPECTED_TABLES = frozenset(("main", name) for name in _V2_TABLE_NAMES)
_EXPECTED_SEQUENCES = frozenset(
    ("main", name)
    for name in {
        "performance_v2_strategy_id_seq",
        "performance_v2_result_id_seq",
        "performance_v2_import_run_id_seq",
        "performance_v2_import_file_id_seq",
    }
)
_V2_EXPECTED_MARKERS = {"schema_version": "2", "database_kind": "unified_performance_v2"}
_V3_EXPECTED_MARKERS = {"schema_version": "3", "database_kind": "unified_performance_v2"}
_EXPECTED_INDEXES = frozenset(
    {
        ("main", "strategy_results_strategy_id_idx"),
        ("main", "strategy_actions_result_timestamp_idx"),
        ("main", "strategy_equity_result_timestamp_idx"),
        ("main", "selection_runs_pair_side_created_idx"),
        ("main", "selection_review_imports_run_imported_idx"),
        ("main", "strategy_tags_tag_idx"),
    }
)
_V4_EXPECTED_INDEXES = _EXPECTED_INDEXES
_V2_EXPECTED_INDEXES = frozenset(index for index in _EXPECTED_INDEXES if not index[1].startswith(("selection_", "strategy_tags_")))

_V4_ACTION_COLUMNS = frozenset(
    {
        "result_id", "action_index", "timestamp_utc", "symbol", "order_id", "action",
        "size", "post_size", "post_side", "pnl", "fee", "balance", "raw_action_json",
    }
)
_V5_ACTION_COLUMNS = _V4_ACTION_COLUMNS | {"price", "cost"}
_V4_RESULT_COLUMNS = frozenset(
    {
        "result_id", "strategy_id", "report_start_utc", "report_end_utc", "exchange",
        "commission_rate", "initial_balance", "final_balance", "total_pnl", "total_pnl_pct",
        "max_drawdown", "max_drawdown_pct", "total_fees", "total_trades", "imported_at_utc",
        "reported_start_utc", "reported_end_utc", "listing_date_utc", "listing_date_raw",
        "listing_date_source", "effective_start_utc", "effective_end_utc", "warmup_hours",
        "excluded_trade_count", "exclusion_reason", "optimizer_source_metadata_json",
    }
)
_V5_RESULT_COLUMNS = _V4_RESULT_COLUMNS | {
    "sizing_use_upnl", "sizing_use_frozen_balance", "sizing_use_fix",
    "sizing_balance_percentage_long", "sizing_risk_long", "sizing_max_balance",
}
_PREPARED_COLUMNS = frozenset(
    {
        "result_id", "preparation_version", "source_digest", "availability_status",
        "unavailable_reason", "prepared_json", "prepared_at_utc",
    }
)
_EQUITY_QUALITY_COLUMNS = (
    ("result_id", "BIGINT", "NO"),
    ("source_revision", "VARCHAR", "NO"),
    ("algo_version", "VARCHAR", "NO"),
    ("facts_json", "VARCHAR", "NO"),
    ("facts_sha256", "VARCHAR", "NO"),
    ("calculated_at_utc", "TIMESTAMP WITH TIME ZONE", "NO"),
)


def _schema_version(connection: duckdb.DuckDBPyConnection) -> str | None:
    exists = connection.execute(
        "select count(*) from information_schema.tables where table_name = 'schema_info'"
    ).fetchone()[0]
    if not exists:
        return None
    try:
        row = connection.execute("select value from schema_info where key = 'schema_version'").fetchone()
    except duckdb.Error as error:
        raise PerformanceV2StoreError("Performance database has invalid schema markers") from error
    return None if row is None else row[0]


def _catalog_objects(
    connection: duckdb.DuckDBPyConnection,
) -> tuple[frozenset[str], frozenset[str], frozenset[tuple[str, str]]]:
    tables = frozenset(
        row
        for row in connection.execute(
        """
        select table_schema, table_name
        from information_schema.tables
        where table_schema not in ('information_schema', 'pg_catalog')
        """
        ).fetchall()
    )
    sequences = frozenset(
        connection.execute("select schema_name, sequence_name from duckdb_sequences()").fetchall()
    )
    indexes = frozenset(
        connection.execute(
            "select schema_name, index_name from duckdb_indexes() where sql is not null"
        ).fetchall()
    )
    return tables, sequences, indexes


def _catalog_is_empty(connection: duckdb.DuckDBPyConnection) -> bool:
    tables, sequences, indexes = _catalog_objects(connection)
    return not tables and not sequences and not indexes


def _table_columns(connection: duckdb.DuckDBPyConnection, table_name: str) -> frozenset[str]:
    return frozenset(
        row[0]
        for row in connection.execute(
            "select column_name from information_schema.columns where table_schema = 'main' and table_name = ?",
            [table_name],
        ).fetchall()
    )


def _schema_markers(connection: duckdb.DuckDBPyConnection) -> dict[str, str]:
    try:
        return dict(connection.execute("select key, value from schema_info").fetchall())
    except duckdb.Error as error:
        raise PerformanceV2StoreError("Performance database has invalid schema markers") from error


def _require_v4_markers(connection: duckdb.DuckDBPyConnection) -> None:
    markers = _schema_markers(connection)
    if set(markers) != {"schema_version", "database_kind", "database_instance_id"} or markers.get(
        "database_kind"
    ) != "unified_performance_v2":
        raise PerformanceV2StoreError("Performance database is not unified performance v2")
    try:
        if str(UUID(markers["database_instance_id"])) != markers["database_instance_id"]:
            raise ValueError
    except (KeyError, ValueError, AttributeError):
        raise PerformanceV2StoreError("Performance database has invalid instance identity") from None


def _require_v4_catalog(connection: duckdb.DuckDBPyConnection) -> None:
    _require_v4_markers(connection)
    tables, sequences, indexes = _catalog_objects(connection)
    if (
        tables != _V4_EXPECTED_TABLES
        or sequences != _EXPECTED_SEQUENCES
        or indexes != _V4_EXPECTED_INDEXES
        or _table_columns(connection, "strategy_actions") != _V4_ACTION_COLUMNS
        or _table_columns(connection, "strategy_results") != _V4_RESULT_COLUMNS
    ):
        raise PerformanceV2StoreError("Performance database has an unexpected catalog")


def _require_v5_catalog(connection: duckdb.DuckDBPyConnection) -> None:
    _require_v4_markers(connection)
    tables, sequences, indexes = _catalog_objects(connection)
    if (
        _schema_version(connection) != _V5_SCHEMA_VERSION
        or tables != _V5_EXPECTED_TABLES
        or sequences != _EXPECTED_SEQUENCES
        or indexes != _EXPECTED_INDEXES
        or _table_columns(connection, "strategy_actions") != _V5_ACTION_COLUMNS
        or _table_columns(connection, "strategy_results") != _V5_RESULT_COLUMNS
        or _table_columns(connection, "optimizer_prepared_inputs") != _PREPARED_COLUMNS
    ):
        raise PerformanceV2StoreError("Performance database has an unexpected catalog")


def _require_v6_catalog(connection: duckdb.DuckDBPyConnection) -> None:
    _require_v4_markers(connection)
    tables, sequences, indexes = _catalog_objects(connection)
    equity_columns = tuple(
        connection.execute(
            """select column_name, data_type, is_nullable
                 from information_schema.columns
                where table_schema = 'main' and table_name = 'equity_quality_metrics'
                order by ordinal_position"""
        ).fetchall()
    )
    constraints = connection.execute(
        """select constraint_type, constraint_column_names
             from duckdb_constraints()
            where schema_name = 'main' and table_name = 'equity_quality_metrics'"""
    ).fetchall()
    primary_keys = [tuple(columns) for kind, columns in constraints if kind == "PRIMARY KEY"]
    has_foreign_key = any(kind == "FOREIGN KEY" for kind, _ in constraints)
    expected_constraints = {
        ("NOT NULL", (column,)) for column in (
            "result_id", "source_revision", "algo_version", "facts_json", "facts_sha256", "calculated_at_utc"
        )
    } | {("PRIMARY KEY", ("result_id", "algo_version"))}
    actual_constraints = {(kind, tuple(columns)) for kind, columns in constraints}
    if (
        _schema_version(connection) != _SCHEMA_VERSION
        or tables != _EXPECTED_TABLES
        or sequences != _EXPECTED_SEQUENCES
        or indexes != _EXPECTED_INDEXES
        or _table_columns(connection, "strategy_actions") != _V5_ACTION_COLUMNS
        or _table_columns(connection, "strategy_results") != _V5_RESULT_COLUMNS
        or _table_columns(connection, "optimizer_prepared_inputs") != _PREPARED_COLUMNS
        or equity_columns != _EQUITY_QUALITY_COLUMNS
        or actual_constraints != expected_constraints
        or primary_keys != [("result_id", "algo_version")]
        or has_foreign_key
    ):
        raise PerformanceV2StoreError("Performance database has an unexpected catalog")


def require_performance_v2(connection: duckdb.DuckDBPyConnection) -> None:
    """Fail closed unless the connection already contains the v2 schema."""
    if _schema_version(connection) != _SCHEMA_VERSION:
        raise PerformanceV2StoreError("Performance database does not have schema version 6")
    _require_v6_catalog(connection)


def require_performance_v2_readable(connection: duckdb.DuckDBPyConnection) -> int:
    """Accept exact read-only v5/v6 catalogs without migrating or repairing."""
    version = _schema_version(connection)
    if version == _V5_SCHEMA_VERSION:
        _require_v5_catalog(connection)
        return 5
    if version == _SCHEMA_VERSION:
        _require_v6_catalog(connection)
        return 6
    raise PerformanceV2StoreError("Performance database schema version requires upgrade")


def _require_schema_v2_for_migration(connection: duckdb.DuckDBPyConnection) -> None:
    if _schema_markers(connection) != _V2_EXPECTED_MARKERS:
        raise PerformanceV2StoreError("Performance database is not unified performance v2")
    tables, sequences, indexes = _catalog_objects(connection)
    if tables != _V2_EXPECTED_TABLES or sequences != _EXPECTED_SEQUENCES or indexes != _V2_EXPECTED_INDEXES:
        raise PerformanceV2StoreError("Performance database has an unexpected catalog")


def _require_schema_v3_for_migration(connection: duckdb.DuckDBPyConnection) -> None:
    markers = _schema_markers(connection)
    if (
        markers.get("schema_version") != _V3_EXPECTED_MARKERS["schema_version"]
        or markers.get("database_kind") != _V3_EXPECTED_MARKERS["database_kind"]
        or set(markers) != {"schema_version", "database_kind", "database_instance_id"}
    ):
        raise PerformanceV2StoreError("Performance database is not unified performance v2")
    try:
        if str(UUID(markers["database_instance_id"])) != markers["database_instance_id"]:
            raise ValueError
    except (KeyError, ValueError, AttributeError):
        raise PerformanceV2StoreError("Performance database has invalid instance identity") from None
    tables, sequences, indexes = _catalog_objects(connection)
    if tables != _V4_EXPECTED_TABLES or sequences != _EXPECTED_SEQUENCES or indexes != _EXPECTED_INDEXES:
        raise PerformanceV2StoreError("Performance database has an unexpected catalog")


def _migrate_schema_v3_to_v4(connection: duckdb.DuckDBPyConnection) -> None:
    _require_schema_v3_for_migration(connection)
    try:
        connection.execute("begin transaction")
        _add_window_columns(connection)
        connection.execute(
            """
            CREATE TABLE strategy_tags__v4_new (
                strategy_id BIGINT NOT NULL REFERENCES strategies(strategy_id),
                tag VARCHAR NOT NULL CHECK (tag IN ('REJECTED', 'RETEST')),
                source VARCHAR NOT NULL,
                source_ref VARCHAR NOT NULL CHECK (length(trim(source_ref)) > 0),
                updated_at_utc TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (strategy_id, tag)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO strategy_tags__v4_new
                (strategy_id, tag, source, source_ref, updated_at_utc)
            SELECT strategy_id, tag, 'SELECTION_REVIEW', source_review_import_id, updated_at_utc
            FROM strategy_tags
            """
        )
        connection.execute("DROP TABLE strategy_tags")
        connection.execute("ALTER TABLE strategy_tags__v4_new RENAME TO strategy_tags")
        connection.execute("CREATE INDEX strategy_tags_tag_idx ON strategy_tags(tag)")
        _add_result_provenance_columns(connection)
        connection.execute("UPDATE schema_info SET value = '4' WHERE key = 'schema_version'")
        connection.execute("commit")
    except Exception as error:
        _rollback_quietly(connection)
        raise PerformanceV2StoreError("Performance database schema migration failed") from error


def _add_window_columns(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("alter table window_metrics add column if not exists holding_seconds decimal(38,12)")
    connection.execute("alter table window_metrics add column if not exists time_in_market_pct decimal(38,12)")


def _add_result_provenance_columns(connection: duckdb.DuckDBPyConnection) -> None:
    """Add warm-up provenance to v4 databases without rewriting result facts."""
    for name, definition in (
        ("reported_start_utc", "timestamptz"),
        ("reported_end_utc", "timestamptz"),
        ("listing_date_utc", "timestamptz"),
        ("listing_date_raw", "varchar"),
        ("listing_date_source", "varchar"),
        ("effective_start_utc", "timestamptz"),
        ("effective_end_utc", "timestamptz"),
        ("warmup_hours", "integer"),
        ("excluded_trade_count", "integer"),
        ("exclusion_reason", "varchar"),
        ("optimizer_source_metadata_json", "varchar"),
    ):
        connection.execute(f"alter table strategy_results add column if not exists {name} {definition}")


def _exact_optional_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    exponent = parsed.as_tuple().exponent
    scale = -exponent if exponent < 0 else 0
    precision = len(parsed.as_tuple().digits) + (exponent if exponent > 0 else 0)
    if scale > 12 or precision > 38 or (parsed != 0 and parsed.adjusted() >= 26):
        return None
    return parsed


def _decode_action_optional_facts(payload: object) -> tuple[Decimal | None, Decimal | None]:
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > 16_384:
        return None, None
    try:
        document = json.loads(payload)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(document, dict):
        return None, None
    if document.get("schema_version") != _OPTIMIZER_SOURCE_METADATA_VERSION:
        return None, None
    if document.get("price_cost_semantics") != _PRICE_COST_SEMANTICS:
        return None, None
    invalid = document.get("invalid_fields")
    invalid_fields = set(invalid) if isinstance(invalid, list) and all(isinstance(item, str) for item in invalid) else set()
    price = None if "price" in invalid_fields else _exact_optional_decimal(document.get("price"))
    cost = None if "cost" in invalid_fields else _exact_optional_decimal(document.get("cost"))
    return price, cost


def _decode_sizing_facts(
    payload: object, imported_at_utc: object, source_report_sha256: str | None = None
) -> tuple[bool | None, bool | None, bool | None, Decimal | None, Decimal | None, Decimal | None]:
    metadata = decode_optimizer_source_metadata(payload, imported_at_utc, source_report_sha256)
    if metadata is None:
        return (None,) * 6
    settings = metadata.get("settings")
    if not isinstance(settings, dict):
        return (None,) * 6
    invalid = metadata.get("invalid_fields")
    invalid_fields = set(invalid) if isinstance(invalid, list) and all(isinstance(item, str) for item in invalid) else set()
    exchange = settings.get("exchange")
    basic = settings.get("basic")
    exchange = exchange if isinstance(exchange, dict) else {}
    basic = basic if isinstance(basic, dict) else {}

    def boolean(section: str, key: str, values: object) -> bool | None:
        return values.get(key) if type(values.get(key)) is bool and f"{section}.{key}" not in invalid_fields else None

    def decimal(section: str, key: str, values: object) -> Decimal | None:
        return None if f"{section}.{key}" in invalid_fields else _exact_optional_decimal(values.get(key))

    return (
        boolean("exchange", "use_upnl", exchange),
        boolean("exchange", "use_frozen_balance", exchange),
        boolean("basic", "use_fix", basic),
        decimal("basic", "balance_percentage_long", basic),
        decimal("basic", "risk_long", basic),
        decimal("basic", "max_balance", basic),
    )


def _backfill_phase8_typed_facts(connection: duckdb.DuckDBPyConnection) -> None:
    action_rows = connection.execute(
        "select result_id, action_index, raw_action_json from strategy_actions where raw_action_json is not null"
    ).fetchall()
    for result_id, action_index, payload in action_rows:
        price, cost = _decode_action_optional_facts(payload)
        connection.execute(
            "update strategy_actions set price = ?, cost = ? where result_id = ? and action_index = ?",
            [price, cost, result_id, action_index],
        )
    source_hashes = {
        str(row[0])
        for row in connection.execute("select distinct source_html_sha256 from import_files").fetchall()
        if isinstance(row[0], str) and len(row[0]) == 64
    }
    result_rows = connection.execute(
        "select result_id, imported_at_utc, optimizer_source_metadata_json from strategy_results"
    ).fetchall()
    for result_id, imported_at_utc, payload in result_rows:
        metadata_hash = None
        if source_hashes:
            try:
                decoded = json.loads(payload) if isinstance(payload, str) else None
                metadata_hash = decoded.get("source_report_sha256") if isinstance(decoded, dict) else None
            except (TypeError, ValueError):
                metadata_hash = None
            sizing = (
                _decode_sizing_facts(payload, imported_at_utc, metadata_hash)
                if metadata_hash in source_hashes
                else (None,) * 6
            )
        else:
            sizing = _decode_sizing_facts(payload, imported_at_utc)
        connection.execute(
            """update strategy_results set sizing_use_upnl = ?, sizing_use_frozen_balance = ?, sizing_use_fix = ?,
               sizing_balance_percentage_long = ?, sizing_risk_long = ?, sizing_max_balance = ? where result_id = ?""",
            [*sizing, result_id],
        )


_PHASE8_SCHEMA = """
CREATE TABLE optimizer_prepared_inputs (
    result_id BIGINT PRIMARY KEY REFERENCES strategy_results(result_id),
    preparation_version VARCHAR NOT NULL CHECK (length(trim(preparation_version)) > 0),
    source_digest VARCHAR NOT NULL CHECK (length(trim(source_digest)) > 0),
    availability_status VARCHAR NOT NULL CHECK (availability_status IN ('AVAILABLE', 'UNAVAILABLE')),
    unavailable_reason VARCHAR,
    prepared_json VARCHAR,
    prepared_at_utc TIMESTAMPTZ NOT NULL,
    CHECK (
        (availability_status = 'AVAILABLE' AND unavailable_reason IS NULL AND prepared_json IS NOT NULL)
        OR (availability_status = 'UNAVAILABLE' AND unavailable_reason IN ('MISSING_TYPED_FACTS', 'UNSUPPORTED_SIZING', 'PREPARED_TOO_LARGE') AND prepared_json IS NULL)
    )
)
"""


def _migrate_schema_v4_to_v5(connection: duckdb.DuckDBPyConnection) -> None:
    _require_v4_catalog(connection)
    try:
        connection.execute("begin transaction")
        connection.execute("alter table strategy_actions add column price decimal(38,12)")
        connection.execute("alter table strategy_actions add column cost decimal(38,12)")
        for name, definition in (
            ("sizing_use_upnl", "boolean"),
            ("sizing_use_frozen_balance", "boolean"),
            ("sizing_use_fix", "boolean"),
            ("sizing_balance_percentage_long", "decimal(38,12)"),
            ("sizing_risk_long", "decimal(38,12)"),
            ("sizing_max_balance", "decimal(38,12)"),
        ):
            connection.execute(f"alter table strategy_results add column {name} {definition}")
        connection.execute(_PHASE8_SCHEMA)
        _backfill_phase8_typed_facts(connection)
        connection.execute("update schema_info set value = '5' where key = 'schema_version'")
        connection.execute("commit")
    except Exception as error:
        _rollback_quietly(connection)
        raise PerformanceV2StoreError("Performance database schema migration failed") from error


def _migrate_schema_v5_to_v6(connection: duckdb.DuckDBPyConnection) -> None:
    _require_v5_catalog(connection)
    try:
        connection.execute("begin transaction")
        connection.execute(_EQUITY_QUALITY_SCHEMA)
        connection.execute("update schema_info set value = '6' where key = 'schema_version'")
        connection.execute("commit")
    except Exception as error:
        _rollback_quietly(connection)
        raise PerformanceV2StoreError("Performance database schema migration failed") from error
    _require_v6_catalog(connection)


def decode_optimizer_source_metadata(
    payload: object,
    imported_at_utc: object,
    source_report_sha256: str | None = None,
) -> dict[str, object] | None:
    """Return current optional source metadata, never stale or malformed data."""
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > 16_384:
        return None
    try:
        document = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema_version") != _OPTIMIZER_SOURCE_METADATA_VERSION:
        return None
    if document.get("price_cost_semantics") != _PRICE_COST_SEMANTICS:
        return None
    stamp = document.get("imported_at_utc")
    if not isinstance(stamp, str):
        return None
    if isinstance(imported_at_utc, datetime):
        if imported_at_utc.tzinfo is None or imported_at_utc.utcoffset() is None:
            return None
        current_stamp = imported_at_utc.astimezone(timezone.utc).isoformat()
    elif isinstance(imported_at_utc, str):
        current_stamp = imported_at_utc
    else:
        return None
    if stamp != current_stamp:
        return None
    stored_hash = document.get("source_report_sha256")
    if not isinstance(stored_hash, str) or len(stored_hash) != 64:
        return None
    if source_report_sha256 is not None and stored_hash != source_report_sha256:
        return None
    return document


def _rollback_quietly(connection: duckdb.DuckDBPyConnection) -> None:
    try:
        connection.execute("rollback")
    except Exception:
        pass


def initialize_performance_v2(
    connection: duckdb.DuckDBPyConnection,
    *,
    create_if_missing: bool = True,
) -> None:
    """Initialize or migrate the isolated Performance v2 schema to v6."""
    version = _schema_version(connection)
    if version is not None and version not in {"2", "3", "4", _V5_SCHEMA_VERSION, _SCHEMA_VERSION}:
        raise PerformanceV2StoreError("Performance database has an unsupported schema version")
    if version == "2":
        _require_schema_v2_for_migration(connection)
        try:
            connection.execute("begin transaction")
            _add_window_columns(connection)
            connection.execute(_SELECTION_SCHEMA_V3)
            connection.execute(
                "insert into schema_info values ('database_instance_id', ?)", [str(uuid4())]
            )
            connection.execute("update schema_info set value = '3' where key = 'schema_version'")
            connection.execute("commit")
        except Exception as error:
            _rollback_quietly(connection)
            raise PerformanceV2StoreError("Performance database schema migration failed") from error
        _migrate_schema_v3_to_v4(connection)
        _migrate_schema_v4_to_v5(connection)
        _migrate_schema_v5_to_v6(connection)
        require_performance_v2(connection)
        return
    if version == _SCHEMA_VERSION:
        _require_v6_catalog(connection)
        try:
            connection.execute("begin transaction")
            _add_window_columns(connection)
            _add_result_provenance_columns(connection)
            connection.execute("commit")
        except Exception as error:
            _rollback_quietly(connection)
            raise PerformanceV2StoreError("Performance database schema repair failed") from error
        require_performance_v2(connection)
        return
    if version == "3":
        _migrate_schema_v3_to_v4(connection)
        _migrate_schema_v4_to_v5(connection)
        _migrate_schema_v5_to_v6(connection)
        require_performance_v2(connection)
        return
    if version == "4":
        _migrate_schema_v4_to_v5(connection)
        _migrate_schema_v5_to_v6(connection)
        require_performance_v2(connection)
        return
    if version == _V5_SCHEMA_VERSION:
        _migrate_schema_v5_to_v6(connection)
        require_performance_v2(connection)
        return
    if not create_if_missing:
        raise PerformanceV2StoreError("Performance database does not have a supported schema")
    if not _catalog_is_empty(connection):
        raise PerformanceV2StoreError("Performance v2 target catalog is not empty")
    try:
        connection.execute("begin transaction")
        connection.execute("create table schema_info (key varchar primary key, value varchar not null)")
        connection.execute(_SCHEMA)
        connection.execute(_SELECTION_SCHEMA)
        connection.execute(_EQUITY_QUALITY_SCHEMA)
        connection.executemany(
            "insert into schema_info (key, value) values (?, ?)",
            [
                ("schema_version", _SCHEMA_VERSION),
                ("database_kind", "unified_performance_v2"),
                ("database_instance_id", str(uuid4())),
            ],
        )
        connection.execute("commit")
    except Exception as error:
        _rollback_quietly(connection)
        raise PerformanceV2StoreError("Performance database initialization failed") from error
    require_performance_v2(connection)
