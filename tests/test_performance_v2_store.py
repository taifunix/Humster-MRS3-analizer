from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from mrs3.performance_import import PerformanceImportRequest, allocate_performance_database
from mrs3.performance_v2_store import (
    PerformanceV2Config,
    PerformanceV2StoreError,
    initialize_performance_v2,
    load_performance_v2_config,
    performance_v2_database_path,
    require_performance_v2,
)


def _config(path: Path, **values: object) -> Path:
    path.write_text(
        json.dumps({"unified_performance_v2": {"database_root": "data/performance-v2", **values}}),
        encoding="utf-8",
    )
    return path


def _strategy(connection: duckdb.DuckDBPyConnection, *, name: str = "BTC-long") -> int:
    return connection.execute(
        """
        insert into strategies (
            strategy_name, symbol, side, timeframe, close_ma_len, order_count,
            analysis_run_id, candidate_identity, lifecycle_status,
            created_at_utc, updated_at_utc
        ) values (?, 'BTCUSDT', 'LONG', '1h', 20, 1, 'run-a', ?, 'ACTIVE', now(), now())
        returning strategy_id
        """,
        [name, name],
    ).fetchone()[0]


def test_config_uses_the_fixed_owned_target_and_ignores_v1_namespace(tmp_path: Path) -> None:
    config = load_performance_v2_config(
        _config(tmp_path / "config.performance.json", workers=99, performance_db_root="data/performanceDB")
    )

    assert config.workers == 64
    assert performance_v2_database_path(config) == (
        tmp_path / "data" / "performance-v2" / "strategy_performance.duckdb"
    )


def test_config_rejects_nonstandard_runtime_v1_root_before_any_duckdb_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_performance_v2_config(
        _config(tmp_path / "config.performance.json"),
        v1_database_root=Path("data"),
    )

    monkeypatch.setattr(duckdb, "connect", lambda *args, **kwargs: pytest.fail("DuckDB must not connect"))

    with pytest.raises(ValueError, match="v1"):
        performance_v2_database_path(config)


@pytest.mark.parametrize(
    "root",
    [
        Path("data/performanceDB"),
        Path("data/performanceDB/v2"),
        Path("data"),
    ],
)
def test_legacy_root_is_rejected_before_any_duckdb_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    called = False

    def forbidden_connect(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("DuckDB must not open a v1 path")

    monkeypatch.setattr(duckdb, "connect", forbidden_connect)

    with pytest.raises(ValueError, match="v1"):
        performance_v2_database_path(
            PerformanceV2Config(
                tmp_path / root,
                v1_database_root=tmp_path / "data" / "performanceDB",
            )
        )

    assert not called


@pytest.mark.parametrize("field", ["workers", "max_html_bytes", "max_actions_per_report"])
@pytest.mark.parametrize("value", [True, 0, -1])
def test_config_rejects_boolean_and_non_positive_limits(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        load_performance_v2_config(_config(tmp_path / "config.performance.json", **{field: value}))


def test_config_defaults_workers_to_sixteen(tmp_path: Path) -> None:
    assert load_performance_v2_config(_config(tmp_path / "config.performance.json")).workers == 16


def test_initialize_is_idempotent_and_requires_schema_v2() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        initialize_performance_v2(connection)

        require_performance_v2(connection)
        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("2",)


def test_v1_schema_is_rejected_without_mutation() -> None:
    with duckdb.connect(":memory:") as connection:
        connection.execute("create table schema_info (key varchar primary key, value varchar not null)")
        connection.execute("insert into schema_info values ('schema_version', '1')")

        with pytest.raises(PerformanceV2StoreError, match="schema version"):
            initialize_performance_v2(connection)

        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("1",)
        assert connection.execute("select count(*) from information_schema.tables where table_name = 'strategies'").fetchone() == (0,)


def test_foreign_database_is_rejected_without_mutation() -> None:
    with duckdb.connect(":memory:") as connection:
        connection.execute("create table strategies (legacy_id integer primary key)")

        with pytest.raises(PerformanceV2StoreError, match="not empty"):
            initialize_performance_v2(connection)

        assert connection.execute("select column_name from information_schema.columns where table_name = 'strategies'").fetchall() == [
            ("legacy_id",)
        ]
        assert connection.execute("select count(*) from information_schema.tables where table_name = 'schema_info'").fetchone() == (0,)


def test_forged_v2_markers_with_foreign_catalog_object_are_rejected_without_mutation() -> None:
    with duckdb.connect(":memory:") as connection:
        connection.execute("create table schema_info (key varchar primary key, value varchar not null)")
        connection.executemany(
            "insert into schema_info values (?, ?)",
            [("schema_version", "2"), ("database_kind", "unified_performance_v2")],
        )
        connection.execute("create table foreign_facts (value integer)")

        with pytest.raises(PerformanceV2StoreError, match="catalog"):
            initialize_performance_v2(connection)

        assert connection.execute("select key, value from schema_info order by key").fetchall() == [
            ("database_kind", "unified_performance_v2"),
            ("schema_version", "2"),
        ]
        assert connection.execute("select count(*) from information_schema.tables where table_name = 'foreign_facts'").fetchone() == (1,)
        assert connection.execute("select count(*) from information_schema.tables where table_name = 'strategies'").fetchone() == (0,)


def test_schema_enforces_order_plateau_and_single_result_invariants() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        with pytest.raises(duckdb.ConstraintException):
            connection.execute(
                """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len, order_count,
                   analysis_run_id, candidate_identity, lifecycle_status, created_at_utc, updated_at_utc)
                   values ('zero', 'BTCUSDT', 'LONG', '1h', 20, 0, 'run-a', 'zero', 'ACTIVE', now(), now())"""
            )
        with pytest.raises(duckdb.ConstraintException):
            connection.execute(
                """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len, order_count,
                   analysis_run_id, candidate_identity, lifecycle_status, created_at_utc, updated_at_utc)
                   values ('five', 'BTCUSDT', 'LONG', '1h', 20, 5, 'run-a', 'five', 'ACTIVE', now(), now())"""
            )

        strategy_id = _strategy(connection)
        with pytest.raises(duckdb.ConstraintException):
            connection.execute(
                """insert into strategy_orders (
                    strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
                    analysis_run_id, plateau_id, base_point_trades
                ) values (?, 1, 10, 1.1, 50, 1, 'run-a', 'missing', 25)""",
                [strategy_id],
            )
        for order_id in (0, 5):
            with pytest.raises(duckdb.ConstraintException):
                connection.execute(
                    """insert into strategy_orders (
                        strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
                        analysis_run_id, plateau_id, base_point_trades
                    ) values (?, ?, 10, 1.1, 50, 1, 'run-a', 'missing', 25)""",
                    [strategy_id, order_id],
                )

        connection.execute("insert into analysis_plateaus values ('run-a', 'P1', 3, 25)")
        connection.execute("insert into analysis_plateaus values ('run-b', 'P1', 4, 30)")
        connection.execute(
            """insert into strategy_orders (
                strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
                analysis_run_id, plateau_id, base_point_trades
            ) values (?, 1, 10, 1.1, 50, 1, 'run-a', 'P1', 25)""",
            [strategy_id],
        )
        assert connection.execute("select count(*) from analysis_plateaus where plateau_id = 'P1'").fetchone() == (2,)

        connection.execute(
            """insert into strategy_results (
                strategy_id, report_start_utc, report_end_utc, exchange, commission_rate,
                initial_balance, final_balance, imported_at_utc
            ) values (?, now(), now(), 'BYBIT', 0.001, 100, 101, now())""",
            [strategy_id],
        )
        with pytest.raises(duckdb.ConstraintException):
            connection.execute(
                """insert into strategy_results (
                    strategy_id, report_start_utc, report_end_utc, exchange, commission_rate,
                    initial_balance, final_balance, imported_at_utc
                ) values (?, now(), now(), 'BYBIT', 0.001, 100, 102, now())""",
                [strategy_id],
            )


def test_v1_import_defaults_and_allocator_are_unchanged(tmp_path: Path) -> None:
    request = PerformanceImportRequest(tmp_path / "inbox", tmp_path / "database.duckdb")

    assert request.workers == 16
    assert allocate_performance_database(tmp_path / "performance", ("BTCUSDT",), "2026-02-01", "2026-09-06").name == (
        "BTC_01.02-06.09.performance-v6.duckdb"
    )
