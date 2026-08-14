from __future__ import annotations

from pathlib import Path

import duckdb


class PerformanceStoreError(ValueError):
    """Raised when a performance database has an unsupported schema."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS import_runs (import_id VARCHAR PRIMARY KEY, batch_id VARCHAR NOT NULL, started_at_utc TIMESTAMPTZ NOT NULL, finished_at_utc TIMESTAMPTZ, expected_report_count INTEGER NOT NULL, imported_count INTEGER NOT NULL, skipped_count INTEGER NOT NULL, quarantined_count INTEGER NOT NULL, status VARCHAR NOT NULL, manifest_json VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS import_files (import_id VARCHAR NOT NULL, manifest_entry_id VARCHAR NOT NULL, strategy_version_id VARCHAR NOT NULL, strategy_name VARCHAR NOT NULL, source_filename VARCHAR NOT NULL, source_html_sha256 VARCHAR NOT NULL, source_size BIGINT NOT NULL, test_run_id VARCHAR, action_count INTEGER, equity_sample_count INTEGER, status VARCHAR NOT NULL, error_classification VARCHAR, error_message VARCHAR, safe_to_delete BOOLEAN NOT NULL, cleanup_state VARCHAR NOT NULL, deleted_at_utc TIMESTAMPTZ, PRIMARY KEY (import_id, manifest_entry_id));
CREATE TABLE IF NOT EXISTS strategy_versions (strategy_version_id VARCHAR PRIMARY KEY, strategy_name VARCHAR NOT NULL, symbol VARCHAR NOT NULL, side VARCHAR NOT NULL, timeframe VARCHAR NOT NULL, settings_json VARCHAR NOT NULL, first_seen_at_utc TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS backtest_runs (test_run_id VARCHAR PRIMARY KEY, strategy_version_id VARCHAR NOT NULL, period_start_utc TIMESTAMPTZ NOT NULL, period_end_utc TIMESTAMPTZ NOT NULL, exchange VARCHAR NOT NULL, commission_contract_id VARCHAR NOT NULL, commission_json VARCHAR NOT NULL, initial_balance DECIMAL(38,12) NOT NULL, source_html_sha256 VARCHAR NOT NULL, result_payload_sha256 VARCHAR NOT NULL, import_id VARCHAR NOT NULL, imported_at_utc TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS backtest_metrics (test_run_id VARCHAR PRIMARY KEY, final_balance DECIMAL(38,12) NOT NULL, total_pnl DECIMAL(38,12) NOT NULL, total_pnl_pct DECIMAL(38,12) NOT NULL, max_drawdown DECIMAL(38,12) NOT NULL, max_drawdown_pct DECIMAL(38,12) NOT NULL, total_fees DECIMAL(38,12) NOT NULL, win_rate_pct DECIMAL(38,12) NOT NULL, profit_factor DECIMAL(38,12), profit_factor_status VARCHAR NOT NULL, days_in_test DECIMAL(38,12) NOT NULL, total_trades INTEGER NOT NULL, win_trades INTEGER NOT NULL, loss_trades INTEGER NOT NULL, metrics_json VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS backtest_actions (test_run_id VARCHAR NOT NULL, action_index INTEGER NOT NULL, timestamp_utc TIMESTAMPTZ NOT NULL, symbol VARCHAR, action VARCHAR, position_side VARCHAR, price DECIMAL(38,12), quantity DECIMAL(38,12), pnl DECIMAL(38,12), fee DECIMAL(38,12), balance DECIMAL(38,12), raw_action_json VARCHAR NOT NULL, PRIMARY KEY (test_run_id, action_index));
CREATE TABLE IF NOT EXISTS backtest_equity (test_run_id VARCHAR NOT NULL, sample_index INTEGER NOT NULL, timestamp_utc TIMESTAMPTZ NOT NULL, wallet DECIMAL(38,12) NOT NULL, equity DECIMAL(38,12) NOT NULL, PRIMARY KEY (test_run_id, sample_index));
CREATE TABLE IF NOT EXISTS dd5_runs (dd5_run_id VARCHAR PRIMARY KEY, import_id VARCHAR NOT NULL, created_at_utc TIMESTAMPTZ NOT NULL, target_dd_pct DECIMAL(18,8) NOT NULL, config_json VARCHAR NOT NULL, input_test_count INTEGER NOT NULL, status VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS dd5_results (dd5_run_id VARCHAR NOT NULL, test_run_id VARCHAR NOT NULL, projected_pnl_dd5 DECIMAL(38,12), projected_dd_pct DECIMAL(38,12), projected_pnl30_dd5 DECIMAL(38,12), scaled_lots_json VARCHAR, capital_requirement_proxy DECIMAL(38,12), holding_filter VARCHAR, pareto_rank INTEGER, raw_json VARCHAR, pareto BOOLEAN, PRIMARY KEY (dd5_run_id, test_run_id));
CREATE OR REPLACE VIEW latest_backtest_by_strategy_version AS SELECT * FROM backtest_runs;
CREATE OR REPLACE VIEW dd5_latest_results AS
SELECT dd5_run_id, test_run_id, projected_pnl_dd5, projected_dd_pct,
       projected_pnl30_dd5, scaled_lots_json, capital_requirement_proxy,
       holding_filter, pareto_rank, raw_json, pareto
FROM (
    SELECT d.*, row_number() OVER (
        PARTITION BY d.test_run_id
        ORDER BY r.created_at_utc DESC, d.dd5_run_id DESC
    ) AS latest_rank
    FROM dd5_results d
    JOIN dd5_runs r USING (dd5_run_id)
)
WHERE latest_rank = 1;
CREATE OR REPLACE VIEW portfolio_layer_a_input AS SELECT r.test_run_id, r.strategy_version_id, r.period_start_utc, r.period_end_utc, m.total_pnl_pct, m.max_drawdown_pct, d.dd5_run_id, d.projected_pnl_dd5, d.projected_dd_pct, d.projected_pnl30_dd5, d.scaled_lots_json, d.capital_requirement_proxy, d.holding_filter, d.pareto_rank, (SELECT count(*) > 0 FROM backtest_actions a WHERE a.test_run_id = r.test_run_id) AS action_timestamps_available, (SELECT count(*) > 0 FROM backtest_equity e WHERE e.test_run_id = r.test_run_id) AS equity_timestamps_available, FALSE AS portfolio_event_ready FROM backtest_runs r LEFT JOIN backtest_metrics m USING (test_run_id) LEFT JOIN dd5_latest_results d USING (test_run_id);
"""


def initialize_performance_database(database: Path) -> None:
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database)) as connection:
        if connection.execute("select count(*) from information_schema.tables where table_name = 'schema_info'").fetchone()[0]:
            version = connection.execute("select value from schema_info where key = 'schema_version'").fetchone()
            if version is None or version[0] != "1":
                raise PerformanceStoreError("unknown schema version")
            connection.execute("alter table dd5_results add column if not exists raw_json varchar")
            connection.execute("alter table dd5_results add column if not exists pareto boolean")
            connection.execute("alter table backtest_metrics alter column profit_factor drop not null")
            connection.execute("alter table backtest_metrics add column if not exists profit_factor_status varchar")
            connection.execute("update backtest_metrics set profit_factor_status = coalesce(profit_factor_status, 'AVAILABLE')")
            connection.execute("alter table backtest_metrics alter column profit_factor_status set not null")
            connection.execute(_SCHEMA)
            return
        connection.execute(_SCHEMA)
        connection.executemany(
            "insert into schema_info (key, value) values (?, ?)",
            [("schema_version", "1"), ("database_kind", "strategy_performance"), ("import_evidence_schema_version", "4")],
        )
