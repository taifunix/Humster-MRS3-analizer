from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import duckdb

from .config import PanelPathSettings, load_panel_path_settings


_SCHEMA_VERSION = "2"
_DATABASE_NAME = "strategy_performance.duckdb"
_MAX_WORKERS = 64
_DEFAULT_V1_PERFORMANCE_ROOT = PanelPathSettings().performance_db_root


class PerformanceV2StoreError(ValueError):
    """Raised when a database is not the isolated Performance v2 schema."""


@dataclass(frozen=True, slots=True)
class PerformanceV2Config:
    database_root: Path
    workers: int = 16
    max_html_bytes: int = 67_108_864
    max_actions_per_report: int = 1_000_000
    v1_database_root: Path = _DEFAULT_V1_PERFORMANCE_ROOT

    def __post_init__(self) -> None:
        for name in ("database_root", "v1_database_root"):
            value = getattr(self, name)
            if not isinstance(value, Path):
                raise ValueError(f"unified_performance_v2.{name} must be a path")
            object.__setattr__(self, name, value.resolve())
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
    return PerformanceV2Config(
        database_root=(path.parent / relative_root),
        workers=section.get("workers", 16),
        max_html_bytes=section.get("max_html_bytes", 67_108_864),
        max_actions_per_report=section.get("max_actions_per_report", 1_000_000),
        v1_database_root=runtime_v1_root,
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
"""

_EXPECTED_TABLES = frozenset(
    ("main", name)
    for name in {
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
)
_EXPECTED_SEQUENCES = frozenset(
    ("main", name)
    for name in {
        "performance_v2_strategy_id_seq",
        "performance_v2_result_id_seq",
        "performance_v2_import_run_id_seq",
        "performance_v2_import_file_id_seq",
    }
)
_EXPECTED_MARKERS = {
    "schema_version": _SCHEMA_VERSION,
    "database_kind": "unified_performance_v2",
}
_EXPECTED_INDEXES = frozenset(
    {
        ("main", "strategy_results_strategy_id_idx"),
        ("main", "strategy_actions_result_timestamp_idx"),
        ("main", "strategy_equity_result_timestamp_idx"),
    }
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


def _schema_markers(connection: duckdb.DuckDBPyConnection) -> dict[str, str]:
    try:
        return dict(connection.execute("select key, value from schema_info").fetchall())
    except duckdb.Error as error:
        raise PerformanceV2StoreError("Performance database has invalid schema markers") from error


def require_performance_v2(connection: duckdb.DuckDBPyConnection) -> None:
    """Fail closed unless the connection already contains the v2 schema."""
    if _schema_version(connection) != _SCHEMA_VERSION:
        raise PerformanceV2StoreError("Performance database does not have schema version 2")
    if _schema_markers(connection) != _EXPECTED_MARKERS:
        raise PerformanceV2StoreError("Performance database is not unified performance v2")
    tables, sequences, indexes = _catalog_objects(connection)
    if (
        tables != _EXPECTED_TABLES
        or sequences != _EXPECTED_SEQUENCES
        or indexes != _EXPECTED_INDEXES
    ):
        raise PerformanceV2StoreError("Performance database has an unexpected catalog")


def initialize_performance_v2(connection: duckdb.DuckDBPyConnection) -> None:
    """Initialize the additive v2 schema, rejecting all non-v2 databases."""
    version = _schema_version(connection)
    if version is not None and version != _SCHEMA_VERSION:
        raise PerformanceV2StoreError("Performance database has an unsupported schema version")
    if version == _SCHEMA_VERSION:
        require_performance_v2(connection)
        connection.execute("alter table window_metrics add column if not exists holding_seconds decimal(38,12)")
        connection.execute("alter table window_metrics add column if not exists time_in_market_pct decimal(38,12)")
        return
    if not _catalog_is_empty(connection):
        raise PerformanceV2StoreError("Performance v2 target catalog is not empty")
    connection.execute("create table schema_info (key varchar primary key, value varchar not null)")
    connection.execute(_SCHEMA)
    connection.executemany(
        "insert into schema_info (key, value) values (?, ?)",
        list(_EXPECTED_MARKERS.items()),
    )
