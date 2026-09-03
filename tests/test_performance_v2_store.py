from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from mrs3.performance_import import PerformanceImportRequest, allocate_performance_database
from mrs3.performance_v2_store import (
    PerformanceV2Config,
    PerformanceV2StoreError,
    _SELECTION_SCHEMA_V3,
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


def test_sibling_local_config_v1_root_is_authoritative_before_duckdb_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _config(tmp_path / "config.performance.json")
    (tmp_path / "config.local.json").write_text(
        json.dumps({"panel_paths": {"performance_db_root": "data/performance-v2"}}),
        encoding="utf-8",
    )
    called = False

    def forbidden_connect(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("DuckDB must not open an overlapping v1 path")

    monkeypatch.setattr(duckdb, "connect", forbidden_connect)

    with pytest.raises(ValueError, match="v1"):
        performance_v2_database_path(load_performance_v2_config(config_path))

    assert not called


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


def test_versioned_performance_config_keeps_the_configured_thirty_worker_default() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config.performance.json"

    assert load_performance_v2_config(config_path).workers == 30


def test_initialize_is_idempotent_and_requires_internal_schema_v4() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        initialize_performance_v2(connection)

        require_performance_v2(connection)
        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("4",)
        instance_id = connection.execute(
            "select value from schema_info where key = 'database_instance_id'"
        ).fetchone()[0]
        assert str(UUID(instance_id)) == instance_id
        assert {
            "selection_runs", "selection_results", "selection_review_imports",
            "selection_review_rows", "strategy_tags",
        }.issubset({
            row[0] for row in connection.execute(
                "select table_name from information_schema.tables where table_schema = 'main'"
            ).fetchall()
        })


def test_strategy_tags_require_nonempty_source_ref() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        strategy_id = _strategy(connection)

        with pytest.raises(duckdb.ConstraintException):
            connection.execute(
                "insert into strategy_tags values (?, 'RETEST', 'SELECTION_REVIEW', ' ', now())",
                [strategy_id],
            )


def test_v4_reentry_restores_legacy_window_columns_before_validation() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        connection.execute("alter table window_metrics drop column holding_seconds")
        connection.execute("alter table window_metrics drop column time_in_market_pct")

        initialize_performance_v2(connection)

        columns = {
            row[0]
            for row in connection.execute(
                "select column_name from information_schema.columns where table_name = 'window_metrics'"
            ).fetchall()
        }
        assert {"holding_seconds", "time_in_market_pct"} <= columns


def test_v4_reentry_adds_result_provenance_without_changing_existing_facts() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        strategy_id = _strategy(connection)
        connection.execute(
            """insert into strategy_results (
                strategy_id, report_start_utc, report_end_utc, exchange,
                commission_rate, initial_balance, final_balance, imported_at_utc
            ) values (?, '2026-01-01', '2026-01-09', 'BYBIT', .001, 100, 101, now())""",
            [strategy_id],
        )
        initialize_performance_v2(connection)
        columns = {
            row[0] for row in connection.execute(
                "select column_name from information_schema.columns where table_name = 'strategy_results'"
            ).fetchall()
        }
        assert {
            "reported_start_utc", "reported_end_utc", "listing_date_utc", "listing_date_raw",
            "listing_date_source", "effective_start_utc", "effective_end_utc", "warmup_hours",
            "excluded_trade_count", "exclusion_reason",
        } <= columns
        assert connection.execute("select count(*) from strategy_results").fetchone() == (1,)
        initialize_performance_v2(connection)


def test_v4_marker_with_foreign_catalog_is_rejected_before_window_repair() -> None:
    with duckdb.connect(":memory:") as connection:
        connection.execute("create table schema_info (key varchar primary key, value varchar not null)")
        connection.executemany(
            "insert into schema_info values (?, ?)",
            [("schema_version", "4"), ("database_kind", "unified_performance_v2"),
             ("database_instance_id", "00000000-0000-0000-0000-000000000001")],
        )
        connection.execute("create table foreign_facts (value integer)")

        with pytest.raises(PerformanceV2StoreError, match="catalog"):
            initialize_performance_v2(connection)

        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("4",)
        assert connection.execute("select count(*) from information_schema.tables where table_name = 'foreign_facts'").fetchone() == (1,)
        assert connection.execute("select count(*) from information_schema.tables where table_name = 'window_metrics'").fetchone() == (0,)


def test_v4_invalid_instance_id_is_rejected_before_window_repair() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        connection.execute("update schema_info set value = 'not-a-uuid' where key = 'database_instance_id'")
        connection.execute("alter table window_metrics drop column holding_seconds")
        connection.execute("alter table window_metrics drop column time_in_market_pct")

        with pytest.raises(PerformanceV2StoreError, match="invalid instance identity"):
            initialize_performance_v2(connection)

        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("4",)
        assert connection.execute("select column_name from information_schema.columns where table_name = 'window_metrics' and column_name in ('holding_seconds', 'time_in_market_pct')").fetchall() == []


def test_initialize_migrates_schema_v2_through_v4_without_changing_existing_facts() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        for table in (
            "strategy_tags", "selection_review_rows", "selection_review_imports",
            "selection_results", "selection_runs",
        ):
            connection.execute(f"drop table {table}")
        connection.execute("delete from schema_info where key = 'database_instance_id'")
        connection.execute("alter table window_metrics drop column holding_seconds")
        connection.execute("alter table window_metrics drop column time_in_market_pct")
        connection.execute("update schema_info set value = '2' where key = 'schema_version'")
        strategy_id = _strategy(connection, name="before-migration")

        initialize_performance_v2(connection)

        assert connection.execute("select strategy_name from strategies where strategy_id = ?", [strategy_id]).fetchone() == (
            "before-migration",
        )
        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("4",)
        assert connection.execute("select count(*) from information_schema.tables where table_name = 'strategy_tags'").fetchone() == (1,)
        assert connection.execute("select count(*) from information_schema.columns where table_name = 'window_metrics' and column_name in ('holding_seconds', 'time_in_market_pct')").fetchone() == (2,)


def _as_v3_fixture(connection: duckdb.DuckDBPyConnection) -> None:
    initialize_performance_v2(connection)
    if connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("3",):
        return
    connection.execute("drop table strategy_tags")
    connection.execute(
        """
        create table strategy_tags (
            strategy_id bigint not null references strategies(strategy_id),
            tag varchar not null check (tag = 'REJECTED'),
            source_review_import_id varchar not null references selection_review_imports(review_import_id),
            updated_at_utc timestamptz not null,
            primary key (strategy_id, tag)
        )
        """
    )
    connection.execute("create index strategy_tags_tag_idx on strategy_tags(tag)")
    connection.execute("update schema_info set value = '3' where key = 'schema_version'")


def test_v3_selection_schema_builder_keeps_the_legacy_tag_columns() -> None:
    assert "source_review_import_id" in _SELECTION_SCHEMA_V3
    assert "source_ref" not in _SELECTION_SCHEMA_V3


def _insert_v3_rejected(
    connection: duckdb.DuckDBPyConnection,
    strategy_id: int,
    *,
    run_id: str = "run-v3",
    review_id: str = "review-1",
    updated_at: datetime | None = None,
) -> datetime:
    updated_at = updated_at or datetime.now(UTC)
    instance_id = connection.execute(
        "select value from schema_info where key = 'database_instance_id'"
    ).fetchone()[0]
    connection.execute(
        """
        insert into selection_runs (
            selection_run_id, database_instance_id, symbol, side, selection_contract_version,
            request_json, request_sha256, config_json, config_sha256, candidate_count,
            representative_count, auto_finalist_count, top_n, workbook_sha256, created_at_utc
        ) values (?, ?, 'BTCUSDT', 'LONG', 'contract', '{}', 'request', '{}', 'config', 1, 1, 1, 1, ?, ?)
        """,
        [run_id, instance_id, f"{review_id}-workbook", updated_at],
    )
    connection.execute(
        "insert into selection_review_imports values (?, ?, ?, ?, 1)",
        [review_id, run_id, f"{review_id}-hash", updated_at],
    )
    connection.execute(
        "insert into strategy_tags values (?, 'REJECTED', ?, ?)", [strategy_id, review_id, updated_at]
    )
    return updated_at


def test_v3_to_v4_migration_persists_rows_and_exact_tag_index(tmp_path: Path) -> None:
    database = tmp_path / "performance.duckdb"
    with duckdb.connect(str(database)) as connection:
        _as_v3_fixture(connection)
        connection.execute("alter table window_metrics drop column holding_seconds")
        connection.execute("alter table window_metrics drop column time_in_market_pct")
        first_id = _strategy(connection, name="v3-row-one")
        second_id = _strategy(connection, name="v3-row-two")
        first_updated = _insert_v3_rejected(
            connection, first_id, updated_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        second_updated = _insert_v3_rejected(
            connection,
            second_id,
            run_id="run-v3-two",
            review_id="review-2",
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

        initialize_performance_v2(connection)
        require_performance_v2(connection)

        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("4",)
        assert connection.execute(
            "select strategy_id, tag, source, source_ref, updated_at_utc from strategy_tags order by strategy_id"
        ).fetchall() == [
            (first_id, "REJECTED", "SELECTION_REVIEW", "review-1", first_updated),
            (second_id, "REJECTED", "SELECTION_REVIEW", "review-2", second_updated),
        ]
        assert connection.execute(
            "select table_type from information_schema.tables where table_schema = 'main' and table_name = 'strategy_tags__v4_new'"
        ).fetchall() == []
        assert connection.execute(
            "select index_name, table_name from duckdb_indexes() where schema_name = 'main' and table_name = 'strategy_tags'"
        ).fetchall() == [("strategy_tags_tag_idx", "strategy_tags")]

    with duckdb.connect(str(database)) as connection:
        require_performance_v2(connection)
        assert connection.execute("select count(*) from strategies where strategy_name like 'v3-row-%'").fetchone() == (2,)
        assert connection.execute(
            "select strategy_id, tag, source_ref, updated_at_utc from strategy_tags order by strategy_id"
        ).fetchall() == [
            (first_id, "REJECTED", "review-1", first_updated),
            (second_id, "REJECTED", "review-2", second_updated),
        ]


class _FailAtRename:
    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: object = None):
        if "alter table strategy_tags__v4_new rename to strategy_tags" in sql.lower():
            raise RuntimeError("forced migration failure")
        if parameters is None:
            return self._connection.execute(sql)
        return self._connection.execute(sql, parameters)


def test_failed_v3_to_v4_migration_rolls_back_staging_and_preserves_v3(tmp_path: Path) -> None:
    database = tmp_path / "performance.duckdb"
    with duckdb.connect(str(database)) as connection:
        _as_v3_fixture(connection)
        strategy_id = _strategy(connection, name="rollback-row")
        _insert_v3_rejected(connection, strategy_id)

        with pytest.raises(PerformanceV2StoreError, match="migration failed"):
            initialize_performance_v2(_FailAtRename(connection))
        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("3",)
        assert connection.execute("select tag, source_review_import_id from strategy_tags").fetchone() == (
            "REJECTED", "review-1"
        )
        assert connection.execute(
            "select count(*) from information_schema.tables where table_name = 'strategy_tags__v4_new'"
        ).fetchone() == (0,)

    with duckdb.connect(str(database)) as connection:
        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("3",)
        assert connection.execute("select count(*) from strategy_tags").fetchone() == (1,)


def test_v3_migration_rejects_unexpected_catalog_without_mutation() -> None:
    with duckdb.connect(":memory:") as connection:
        _as_v3_fixture(connection)
        strategy_id = _strategy(connection, name="extra-table")
        _insert_v3_rejected(connection, strategy_id)
        connection.execute("create table foreign_facts (value integer)")

        with pytest.raises(PerformanceV2StoreError, match="catalog"):
            initialize_performance_v2(connection)

        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("3",)
        assert connection.execute("select tag, source_review_import_id from strategy_tags").fetchone() == (
            "REJECTED", "review-1"
        )
        assert connection.execute("select count(*) from information_schema.tables where table_name = 'foreign_facts'").fetchone() == (1,)
        assert connection.execute("select count(*) from information_schema.tables where table_name = 'strategy_tags__v4_new'").fetchone() == (0,)


def test_v3_migration_rejects_invalid_marker_identity_without_mutation() -> None:
    with duckdb.connect(":memory:") as connection:
        _as_v3_fixture(connection)
        strategy_id = _strategy(connection, name="invalid-marker")
        _insert_v3_rejected(connection, strategy_id)
        connection.execute("update schema_info set value = 'not-v2' where key = 'database_kind'")

        with pytest.raises(PerformanceV2StoreError, match="unified performance v2"):
            initialize_performance_v2(connection)

        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("3",)
        assert connection.execute("select value from schema_info where key = 'database_kind'").fetchone() == ("not-v2",)
        assert connection.execute("select tag, source_review_import_id from strategy_tags").fetchone() == (
            "REJECTED", "review-1"
        )


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


def test_v2_schema_rejects_a_forged_extra_index() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        connection.execute("create index foreign_strategy_symbol_idx on strategies(symbol)")

        with pytest.raises(PerformanceV2StoreError, match="catalog"):
            require_performance_v2(connection)


def test_v2_schema_rejects_forged_schema_qualified_catalog_objects() -> None:
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        connection.execute("create schema forged")
        connection.execute("create table forged.strategies (value integer)")
        connection.execute("create sequence forged.performance_v2_strategy_id_seq")

        with pytest.raises(PerformanceV2StoreError, match="catalog"):
            require_performance_v2(connection)


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
