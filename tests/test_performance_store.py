from pathlib import Path
from decimal import Decimal

import duckdb
import pytest

from mrs3.performance_store import PerformanceStoreError, initialize_performance_database


REQUIRED_TABLES = {
    "schema_info", "import_runs", "import_files", "strategy_versions",
    "backtest_runs", "backtest_metrics", "backtest_actions", "backtest_equity",
    "dd5_runs", "dd5_results",
}
REQUIRED_VIEWS = {
    "latest_backtest_by_strategy_version", "dd5_latest_results", "portfolio_layer_a_input",
}


def test_initialize_creates_v1_performance_schema(tmp_path: Path) -> None:
    database = tmp_path / "strategy_performance.duckdb"
    initialize_performance_database(database)
    initialize_performance_database(database)

    with duckdb.connect(str(database), read_only=True) as connection:
        objects = {
            name: kind for name, kind in connection.execute(
                "select table_name, table_type from information_schema.tables "
                "where table_schema = 'main'"
            ).fetchall()
        }
        assert REQUIRED_TABLES <= {name for name, kind in objects.items() if kind == "BASE TABLE"}
        assert REQUIRED_VIEWS <= {name for name, kind in objects.items() if kind == "VIEW"}
        assert dict(connection.execute("select key, value from schema_info").fetchall()) == {
            "schema_version": "1",
            "database_kind": "strategy_performance",
            "import_evidence_schema_version": "4",
        }
        assert connection.execute(
            "select portfolio_event_ready from portfolio_layer_a_input limit 1"
        ).fetchall() == []


def test_initialize_rejects_unknown_schema_version(tmp_path: Path) -> None:
    database = tmp_path / "strategy_performance.duckdb"
    initialize_performance_database(database)
    with duckdb.connect(str(database)) as connection:
        connection.execute("update schema_info set value = '999' where key = 'schema_version'")
    with pytest.raises(PerformanceStoreError, match="unknown schema version"):
        initialize_performance_database(database)


def test_initialize_migrates_existing_profit_factor_to_nullable_status_contract(tmp_path: Path) -> None:
    database = tmp_path / "strategy_performance.duckdb"
    initialize_performance_database(database)
    with duckdb.connect(str(database)) as connection:
        connection.execute("alter table backtest_metrics drop column profit_factor_status")
        connection.execute("alter table backtest_metrics alter column profit_factor set not null")
        connection.execute(
            "insert into backtest_metrics (test_run_id, final_balance, total_pnl, total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees, win_rate_pct, profit_factor, days_in_test, total_trades, win_trades, loss_trades, metrics_json) values ('old', 1, 1, 1, 1, 1, 1, 1, 2, 1, 1, 1, 0, '{}')"
        )

    initialize_performance_database(database)

    with duckdb.connect(str(database), read_only=True) as connection:
        columns = {
            row[0]: row for row in connection.execute("describe backtest_metrics").fetchall()
        }
        assert columns["profit_factor"][2] == "YES"
        assert columns["profit_factor_status"][1] == "VARCHAR"
        assert connection.execute(
            "select profit_factor, profit_factor_status from backtest_metrics where test_run_id = 'old'"
        ).fetchone() == (Decimal("2.000000000000"), "AVAILABLE")


def test_portfolio_layer_input_exposes_dd5_and_timestamp_availability(tmp_path: Path) -> None:
    database = tmp_path / "strategy_performance.duckdb"
    initialize_performance_database(database)
    with duckdb.connect(str(database), read_only=True) as connection:
        columns = {row[0] for row in connection.execute("describe portfolio_layer_a_input").fetchall()}
    assert {
        "scaled_lots_json", "projected_pnl_dd5", "projected_dd_pct", "holding_filter",
        "pareto_rank", "action_timestamps_available", "equity_timestamps_available",
    } <= columns


def test_dd5_latest_results_selects_only_newest_run(tmp_path: Path) -> None:
    database = tmp_path / "strategy_performance.duckdb"
    initialize_performance_database(database)
    with duckdb.connect(str(database)) as connection:
        connection.execute("insert into dd5_runs values ('old', 'I', '2026-08-01T00:00:00Z', 5, '{}', 1, 'CALCULATION_ONLY')")
        connection.execute("insert into dd5_runs values ('new', 'I', '2026-08-02T00:00:00Z', 5, '{}', 1, 'CALCULATION_ONLY')")
        connection.execute("insert into dd5_results (dd5_run_id, test_run_id) values ('old', 'T')")
        connection.execute("insert into dd5_results (dd5_run_id, test_run_id) values ('new', 'T')")
        assert connection.execute("select dd5_run_id from dd5_latest_results where test_run_id = 'T'").fetchone() == ('new',)
