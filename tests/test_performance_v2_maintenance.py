from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import multiprocessing
from pathlib import Path
import shutil
import time
from typing import Iterator

import duckdb
import psutil
import pytest

from mrs3.performance_v2_store import initialize_performance_v2
import mrs3.performance_v2_maintenance as maintenance
from mrs3.performance_v2_maintenance import PerformanceV2MaintenanceError, apply_preview, catalog, create_preview
from mrs3.performance_v2_maintenance import set_query_workers


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_KNOWN_PRODUCTION_DB_PATHS = {
    (_REPOSITORY_ROOT / "data" / "performanceDB" / "strategy_performance.duckdb").resolve(),
    (_REPOSITORY_ROOT / "data" / "performance-v2" / "strategy_performance.duckdb").resolve(),
}


@contextmanager
def _writable_fixture(path: Path, fixture_root: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    resolved = path.resolve()
    assert _within(resolved, fixture_root), "every writable target must be beneath its fixture root"
    assert resolved not in _KNOWN_PRODUCTION_DB_PATHS, "known production PerformanceDB paths are forbidden"
    assert not _within(resolved, Path(__file__).resolve().parents[1]), "repository databases are read-only"
    with duckdb.connect(str(resolved)) as connection:
        yield connection


@pytest.fixture
def maintenance_db(tmp_path: Path) -> Path:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    target = fixture_root / "performance.duckdb"
    with _writable_fixture(target, fixture_root) as connection:
        initialize_performance_v2(connection)
        _seed_fixture(connection)
    return target


def _seed_fixture(connection: duckdb.DuckDBPyConnection) -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    schema_instance = connection.execute(
        "select value from schema_info where key = 'database_instance_id'"
    ).fetchone()[0]
    strategies = [
        (1, "btc-a", "BTCUSDT", "run-shared", "P1", 101),
        (2, "eth-a", "ETHUSDT", "run-shared", "P1", 202),
        (3, "other-a", "SOLUSDT", "run-sol", "P1", 303),
    ]
    for strategy_id, name, symbol, run_id, plateau_id, result_id in strategies:
        connection.execute(
            """insert into strategies (
                   strategy_id, strategy_name, symbol, side, timeframe, close_ma_len,
                   order_count, analysis_run_id, candidate_identity, lifecycle_status,
                   current_result_id, created_at_utc, updated_at_utc
               ) values (?, ?, ?, 'LONG', '1h', 10, 1, ?, ?, 'ACTIVE', ?, ?, ?)""",
            [strategy_id, name, symbol, run_id, name, result_id, now, now],
        )
        connection.execute(
            "insert into analysis_plateaus values (?, ?, 3, 8) on conflict do nothing",
            [run_id, plateau_id],
        )
        connection.execute(
            """insert into strategy_orders (
                   strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
                   analysis_run_id, plateau_id, base_point_trades
               ) values (?, 1, 8, 1, 0, 0.25, ?, ?, 3)""",
            [strategy_id, run_id, plateau_id],
        )
        connection.execute(
            """insert into strategy_results (
                   result_id, strategy_id, report_start_utc, report_end_utc, exchange,
                   initial_balance, final_balance, imported_at_utc
               ) values (?, ?, ?, ?, 'test', 1000, 1010, ?)""",
            [result_id, strategy_id, now, now, now],
        )
    connection.execute(
        """insert into strategy_actions (
               result_id, action_index, timestamp_utc, symbol, action, size, post_size,
               post_side, pnl, fee, balance
           ) values (101, 0, ?, 'BTCUSDT', 'opened', 1, 1, 'LONG', 1, 0, 1001),
                    (202, 0, ?, 'ETHUSDT', 'opened', 1, 1, 'LONG', 1, 0, 1001),
                    (303, 0, ?, 'SOLUSDT', 'opened', 1, 1, 'LONG', 1, 0, 1001)""",
        [now, now, now],
    )
    connection.execute(
        """insert into strategy_equity values
               (101, 0, ?, 1000, 1000), (202, 0, ?, 1000, 1000), (303, 0, ?, 1000, 1000)""",
        [now, now, now],
    )
    connection.execute(
        """insert into optimizer_prepared_inputs (
               result_id, preparation_version, source_digest, availability_status,
               prepared_json, prepared_at_utc
           ) values (101, 'v1', 'a', 'AVAILABLE', '{}', ?),
                    (202, 'v1', 'b', 'AVAILABLE', '{}', ?),
                    (303, 'v1', 'c', 'AVAILABLE', '{}', ?)""",
        [now, now, now],
    )
    connection.execute(
        """insert into window_metrics (
               result_id, requested_start_utc, requested_end_utc, metrics_version,
               availability_status, calculated_at_utc
           ) values (101, ?, ?, 'v1', 'AVAILABLE', ?),
                    (202, ?, ?, 'v1', 'AVAILABLE', ?),
                    (303, ?, ?, 'v1', 'AVAILABLE', ?)""",
        [now, now, now, now, now, now, now, now, now],
    )
    connection.execute(
        """insert into equity_quality_metrics values
               (101, 'rev', 'v1', '{}', 'hash-a', ?),
               (202, 'rev', 'v1', '{}', 'hash-b', ?),
               (303, 'rev', 'v1', '{}', 'hash-c', ?)""",
        [now, now, now],
    )
    connection.execute(
        """insert into import_runs (
               import_run_id, source_inbox_sha256, expected_report_count, imported_count,
               skipped_count, rejected_count, status, started_at_utc
           ) values (1, 'run-hash', 3, 3, 0, 0, 'IMPORTED', ?)""",
        [now],
    )
    connection.execute(
        """insert into import_files (
               import_file_id, import_run_id, source_filename, source_html_sha256,
               source_size_bytes, status
           ) values (1, 1, 'report.html', 'file-hash', 1, 'IMPORTED')"""
    )
    connection.execute(
        """insert into selection_runs (
               selection_run_id, database_instance_id, symbol, side,
               selection_contract_version, request_json, request_sha256, config_json,
               config_sha256, candidate_count, representative_count, auto_finalist_count,
               top_n, workbook_sha256, created_at_utc
           ) values ('sel-btc', ?, 'BTCUSDT', 'LONG', 'v1', '{}', 'a', '{}', 'b',
                    1, 1, 1, 1, 'c', ?),
                   ('sel-eth', ?, 'ETHUSDT', 'LONG', 'v1', '{}', 'd', '{}', 'e',
                    1, 1, 0, 1, 'f', ?),
                   ('sel-only', ?, 'XRPUSDT', 'LONG', 'v1', '{}', 'g', '{}', 'h',
                    0, 0, 0, 1, 'i', ?),
                   ('sel-sol-old', ?, 'SOLUSDT', 'LONG', 'v1', '{}', 'j', '{}', 'k',
                    1, 1, 0, 1, 'l', ?),
                   ('sel-sol-new', ?, 'SOLUSDT', 'LONG', 'v1', '{}', 'm', '{}', 'n',
                    1, 1, 0, 1, 'o', ?)""",
        [schema_instance, now, schema_instance, now, schema_instance, now,
         schema_instance, now, schema_instance, now],
    )
    connection.execute("update selection_runs set created_at_utc = '2026-09-01T00:00:00Z' where selection_run_id = 'sel-sol-old'")
    connection.execute("update selection_runs set created_at_utc = '2026-10-05T00:00:00Z' where selection_run_id = 'sel-sol-new'")
    connection.execute(
        """insert into selection_results (
               selection_run_id, strategy_id, result_id_at_selection, auto_status,
               prior_rejected, stage_trace_json
           ) values ('sel-btc', 1, 101, 'FINALIST', false, '[]'),
                    ('sel-eth', 2, 202, 'FINALIST', false, '[]'),
                    ('sel-sol-old', 3, 303, 'FILTERED', true, '[]'),
                    ('sel-sol-new', 3, 303, 'FILTERED', false, '[]')"""
    )
    connection.execute(
        """insert into selection_review_imports values ('review-btc', 'sel-btc', 'review-hash', ?, 1)""",
        [now],
    )
    connection.execute(
        """insert into selection_review_rows (
               review_import_id, strategy_id, user_status, comment
           ) values ('review-btc', 1, 'REJECTED', 'manual')"""
    )
    connection.execute(
        """insert into strategy_rejection_sources (
               strategy_id, source_kind, reason_code, first_result_id,
               first_selection_run_id, classifier_algo_version, source_revision,
               facts_sha256, created_at_utc
           ) values (2, 'EQUITY_REGIME_FILTER', 'DD_14_7_GTE_23', 202,
                    'sel-eth', 'algo-v1', 'rev', 'facts', ?)""",
        [now],
    )
    connection.execute(
        "insert into strategy_tags values (3, 'RETEST', 'fixture', 'sol-tag', ?)", [now]
    )
    connection.execute(
        "insert into strategy_tags values (1, 'RETEST', 'fixture', 'btc-tag', ?)", [now]
    )


def test_catalog_includes_strategy_and_selection_only_symbols(maintenance_db: Path) -> None:
    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        assert catalog(connection) == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def test_rejected_retirement_covers_every_strategy_or_result_table(maintenance_db: Path) -> None:
    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        actual = {
            str(row[0])
            for row in connection.execute(
                """select distinct table_name
                     from information_schema.columns
                    where table_schema = 'main'
                      and column_name in ('strategy_id', 'result_id')"""
            ).fetchall()
        }
    assert actual == set(maintenance._REJECTED_TABLES) | set(maintenance._REJECTED_RETAINED_TABLES)

    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        all_tables = {
            str(row[0])
            for row in connection.execute(
                """select table_name from information_schema.tables
                    where table_schema = 'main' and table_type = 'BASE TABLE'"""
            ).fetchall()
        }
    expected_tables = {
        *maintenance._PAIR_TABLES,
        *maintenance._GLOBAL_JOURNAL_TABLES,
        *maintenance._PRESERVED_METADATA_TABLES,
    }
    assert all_tables == expected_tables


def test_catalog_fails_closed_when_action_symbol_disagrees_with_strategy(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        connection.execute("update strategy_actions set symbol = 'ETHUSDT' where result_id = 101")
    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        with pytest.raises(ValueError, match="strategy_actions.*symbol"):
            catalog(connection)


def test_catalog_fails_closed_on_unclassified_schema_v9_table(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        connection.execute("create table unexpected_table (value varchar)")
    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        with pytest.raises(ValueError, match="unclassified.*unexpected_table"):
            catalog(connection)


def test_catalog_rejects_null_symbols(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        class NullSymbolRows:
            def fetchall(self):
                return [(None,)]

        class NullSymbolCatalogConnection:
            def execute(self, sql, parameters=None):
                if sql.strip().casefold() == (
                    "select symbol from strategies union select symbol from selection_runs order by symbol"
                ):
                    return NullSymbolRows()
                if parameters is None:
                    return connection.execute(sql)
                return connection.execute(sql, parameters)

        with pytest.raises(PerformanceV2MaintenanceError, match="NULL symbol"):
            catalog(NullSymbolCatalogConnection())


@pytest.mark.parametrize("placement", ["attached", "temporary"])
def test_schema_classification_ignores_noncurrent_tables(
    maintenance_db: Path, placement: str,
) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        connection.execute("drop table optimizer_prepared_inputs")
        if placement == "temporary":
            connection.execute("create temporary table optimizer_prepared_inputs (value varchar)")
        else:
            attached_path = maintenance_db.parent / "attached.duckdb"
            with duckdb.connect(str(attached_path)) as attached:
                attached.execute("create table optimizer_prepared_inputs (value varchar)")
            connection.execute(f"attach '{attached_path.as_posix()}' as secondary")

        with pytest.raises(maintenance.PerformanceV2MaintenanceSchemaError, match="missing classified.*optimizer_prepared_inputs"):
            maintenance._classify_schema_v9_tables(connection)


def test_maintenance_schema_ownership_columns_match_delete_model(maintenance_db: Path) -> None:
    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        schema_info_columns = {
            name: nullable
            for name, nullable in connection.execute(
                """select column_name, is_nullable from information_schema.columns
                     where table_schema = 'main' and table_name = 'schema_info'"""
            ).fetchall()
        }
        schema_info_primary_keys = {
            tuple(column_names)
            for kind, column_names in connection.execute(
                """select constraint_type, constraint_column_names from duckdb_constraints()
                     where schema_name = 'main' and table_name = 'schema_info'"""
            ).fetchall()
            if kind == "PRIMARY KEY"
        }
        columns = {
            table: {
                name: nullable
                for name, nullable in connection.execute(
                    """select column_name, is_nullable from information_schema.columns
                         where table_schema = 'main' and table_name = ?""",
                    [table],
                ).fetchall()
            }
            for table in (
                "strategies", "selection_runs", "strategy_orders", "strategy_actions",
                "strategy_results", "strategy_equity", "optimizer_prepared_inputs",
            )
        }

    assert schema_info_columns["key"] == "NO"
    assert ("key",) in schema_info_primary_keys
    assert columns["strategies"]["symbol"] == "NO"
    assert columns["selection_runs"]["symbol"] == "NO"
    assert columns["strategy_orders"]["analysis_run_id"] == "NO"
    assert columns["strategy_orders"]["plateau_id"] == "NO"
    assert "symbol" in columns["strategy_actions"]
    for table in ("strategy_results", "strategy_equity", "optimizer_prepared_inputs"):
        assert columns[table]["result_id"] == "NO"
        assert "symbol" not in columns[table]

    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        for table in ("strategy_equity", "optimizer_prepared_inputs"):
            foreign_keys = {
                tuple(column_names)
                for kind, column_names in connection.execute(
                    """select constraint_type, constraint_column_names from duckdb_constraints()
                         where schema_name = 'main' and table_name = ?""",
                    [table],
                ).fetchall()
                if kind == "FOREIGN KEY"
            }
            assert ("result_id",) in foreign_keys


def test_schema_table_classifier_preserves_duplicate_entries() -> None:
    assert maintenance._SCHEMA_TABLE_CLASSES["pair-scoped"] == maintenance._PAIR_TABLES


def test_schema_table_classifier_rejects_duplicate_declarations(
    maintenance_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        maintenance,
        "_SCHEMA_TABLE_CLASSES",
        {"pair-scoped": ("strategies", "strategies")},
    )
    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        with pytest.raises(maintenance.PerformanceV2MaintenanceSchemaError, match="classified more than once: strategies"):
            maintenance._classify_schema_v9_tables(connection)


def test_catalog_preview_and_apply_reject_cross_symbol_selection_ownership(
    maintenance_db: Path,
) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        valid_preview = create_preview(connection, ["BTCUSDT"], "full")
        connection.execute(
            """insert into selection_results (
                   selection_run_id, strategy_id, result_id_at_selection, auto_status,
                   prior_rejected, stage_trace_json
               ) values ('sel-btc', 2, 202, 'FINALIST', false, '[]')"""
        )
        before = connection.execute(
            "select * from strategies where symbol = 'BTCUSDT' order by strategy_id"
        ).fetchall()
        selection_before = connection.execute(
            "select * from selection_results where selection_run_id = 'sel-btc' order by strategy_id"
        ).fetchall()

        for operation in (
            lambda: catalog(connection),
            lambda: create_preview(connection, ["BTCUSDT"], "full"),
            lambda: apply_preview(connection, valid_preview),
        ):
            with pytest.raises(PerformanceV2MaintenanceError, match="selection_results.*owning pair"):
                operation()

        assert connection.execute(
            "select * from strategies where symbol = 'BTCUSDT' order by strategy_id"
        ).fetchall() == before
        assert connection.execute(
            "select * from selection_results where selection_run_id = 'sel-btc' order by strategy_id"
        ).fetchall() == selection_before


def test_full_preview_counts_shared_plateau_once_and_global_journal_once(maintenance_db: Path) -> None:
    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        before = connection.execute("select count(*) from strategy_actions").fetchone()[0]
        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT", "XRPUSDT"], "full")
        assert connection.execute("select count(*) from strategy_actions").fetchone()[0] == before

    assert preview["shared_plateau_rows"] == 1
    assert preview["global_counts"] == {"import_files": 1, "import_runs": 1}
    assert [pair["symbol"] for pair in preview["pairs"]] == ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    assert [pair["strategy_count"] for pair in preview["pairs"]] == [1, 1, 0]
    assert [pair["rows"] for pair in preview["pairs"]] == [13, 11, 1]
    assert preview["pair_scoped_total"] == 26


def test_preview_retains_plateau_shared_with_an_unselected_pair(maintenance_db: Path) -> None:
    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        preview = create_preview(connection, ["BTCUSDT"], "full")

    assert preview["shared_plateau_rows"] == 0
    assert preview["table_counts"]["analysis_plateaus"] == 0
    assert preview["pairs"][0]["table_counts"]["analysis_plateaus"] == 0
    assert preview["_targets"]["plateau_keys"] == []


def test_retry_preview_recovers_plateau_keys_after_order_delete_commits(
    maintenance_db: Path,
) -> None:
    selected = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        initial = create_preview(connection, selected, "full")
        with pytest.raises(duckdb.IOException, match="injected failure at analysis_plateaus"):
            apply_preview(_FailOnceOnDelete(connection, "analysis_plateaus"), initial)

        # DuckDB enforces the order -> plateau FK at statement/transaction end;
        # order deletion must commit before the parent plateau can be deleted.
        assert connection.execute(
            "select count(*) from strategy_orders where strategy_id in (1, 2)"
        ).fetchone() == (0,)
        unassisted = create_preview(connection, selected, "full")
        assert unassisted["table_counts"]["analysis_plateaus"] == 0
        retry = create_preview(connection, selected, "full", recovery_preview=initial)
        assert retry["table_counts"]["analysis_plateaus"] == 1
        assert retry["pair_scoped_total"] >= 1
        apply_preview(connection, retry)
        assert connection.execute(
            "select count(*) from analysis_plateaus where analysis_run_id = 'run-shared'"
        ).fetchone() == (0,)


def test_retry_preview_and_apply_preserve_plateau_when_unselected_pair_references_it(
    maintenance_db: Path,
) -> None:
    selected = ["BTCUSDT", "ETHUSDT"]
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        initial = create_preview(connection, selected, "full")
        with pytest.raises(duckdb.IOException, match="injected failure at analysis_plateaus"):
            apply_preview(_FailOnceOnDelete(connection, "analysis_plateaus"), initial)

        retry = create_preview(connection, selected, "full", recovery_preview=initial)
        assert retry["table_counts"]["analysis_plateaus"] == 1

        class AddUnselectedOrderAtReferenceCheck:
            def __init__(self, inner: duckdb.DuckDBPyConnection) -> None:
                self.inner = inner
                self.added = False

            def execute(self, sql: str, parameters=None):
                if "-- maintenance: verify plateau references" in sql and not self.added:
                    self.inner.execute(
                        """insert into strategy_orders (
                               strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
                               analysis_run_id, plateau_id, base_point_trades
                           ) values (3, 2, 8, 1, 0, 0.25, 'run-shared', 'P1', 3)"""
                    )
                    self.added = True
                if parameters is None:
                    return self.inner.execute(sql)
                return self.inner.execute(sql, parameters)

        with pytest.raises(PerformanceV2MaintenanceError, match="plateau.*referenced"):
            apply_preview(AddUnselectedOrderAtReferenceCheck(connection), retry)

        assert connection.execute(
            "select count(*) from analysis_plateaus where analysis_run_id = 'run-shared' and plateau_id = 'P1'"
        ).fetchone() == (1,)
        assert connection.execute(
            "select count(*) from strategy_orders where strategy_id = 3 and order_id = 2 and analysis_run_id = 'run-shared'"
        ).fetchone() == (1,)
        assert connection.execute("select count(*) from strategies where strategy_id = 3").fetchone() == (1,)


def test_recovery_preview_omits_plateau_with_a_new_unselected_reference(maintenance_db: Path) -> None:
    selected = ["BTCUSDT", "ETHUSDT"]
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        initial = create_preview(connection, selected, "full")
        with pytest.raises(duckdb.IOException, match="injected failure at analysis_plateaus"):
            apply_preview(_FailOnceOnDelete(connection, "analysis_plateaus"), initial)
        connection.execute(
            """insert into strategy_orders (
                   strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
                   analysis_run_id, plateau_id, base_point_trades
               ) values (3, 2, 8, 1, 0, 0.25, 'run-shared', 'P1', 3)"""
        )

        retry = create_preview(connection, selected, "full", recovery_preview=initial)
        assert retry["table_counts"]["analysis_plateaus"] == 0
        assert retry["_targets"]["plateau_keys"] == []
        apply_preview(connection, retry)

        assert connection.execute(
            "select count(*) from analysis_plateaus where analysis_run_id = 'run-shared' and plateau_id = 'P1'"
        ).fetchone() == (1,)
        assert connection.execute(
            "select count(*) from strategy_orders where strategy_id = 3 and order_id = 2 and analysis_run_id = 'run-shared'"
        ).fetchone() == (1,)
        assert connection.execute("select count(*) from strategies where strategy_id = 3").fetchone() == (1,)


def test_recovery_preview_accepts_a_subset_and_only_deletes_plateaus_owned_by_that_subset(maintenance_db: Path) -> None:
    original_symbols = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    retry_symbols = ["BTCUSDT", "ETHUSDT"]
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        original = create_preview(connection, original_symbols, "full")
        with pytest.raises(duckdb.IOException, match="injected failure at analysis_plateaus"):
            apply_preview(_FailOnceOnDelete(connection, "analysis_plateaus"), original)
        assert connection.execute(
            "select count(*) from strategy_orders where strategy_id in (1, 2)"
        ).fetchone() == (0,)

        retry = create_preview(connection, retry_symbols, "full", recovery_preview=original)

        assert retry["symbols"] == retry_symbols
        assert retry["table_counts"]["analysis_plateaus"] == 1
        assert retry["_targets"]["plateau_keys"] == [("run-shared", "P1")]
        apply_preview(connection, retry)

        assert connection.execute(
            "select analysis_run_id, plateau_id from analysis_plateaus order by analysis_run_id, plateau_id"
        ).fetchall() == [("run-sol", "P1")]
        assert connection.execute("select count(*) from strategy_orders where strategy_id = 3").fetchone() == (1,)


def test_recovery_preview_accepts_a_superset_while_rechecking_plateau_owners(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        connection.execute("insert into analysis_plateaus values ('run-btc', 'PBTC', 4, 9)")
        connection.execute(
            "update strategy_orders set analysis_run_id = 'run-btc', plateau_id = 'PBTC' where strategy_id = 1"
        )
        original = create_preview(connection, ["BTCUSDT"], "full")
        assert original["_targets"]["plateau_keys"] == [("run-btc", "PBTC")]
        with pytest.raises(duckdb.IOException, match="injected failure at analysis_plateaus"):
            apply_preview(_FailOnceOnDelete(connection, "analysis_plateaus"), original)
        assert connection.execute(
            "select count(*) from strategy_orders where strategy_id = 1"
        ).fetchone() == (0,)

        retry = create_preview(connection, ["BTCUSDT", "ETHUSDT"], "full", recovery_preview=original)

        assert retry["symbols"] == ["BTCUSDT", "ETHUSDT"]
        assert retry["_targets"]["plateau_keys"] == [("run-btc", "PBTC"), ("run-shared", "P1")]
        assert retry["table_counts"]["analysis_plateaus"] == 2


def test_restart_without_transient_recovery_map_preserves_unattributed_plateaus(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        connection.execute("insert into analysis_plateaus values ('run-btc', 'PBTC', 4, 9)")
        connection.execute(
            "update strategy_orders set analysis_run_id = 'run-btc', plateau_id = 'PBTC' where strategy_id = 1"
        )
        initial = create_preview(connection, ["BTCUSDT"], "full")
        with pytest.raises(duckdb.IOException, match="injected failure at analysis_plateaus"):
            apply_preview(_FailOnceOnDelete(connection, "analysis_plateaus"), initial)

        restarted_panel_preview = create_preview(connection, ["BTCUSDT"], "full")
        assert restarted_panel_preview["table_counts"]["analysis_plateaus"] == 0
        apply_preview(connection, restarted_panel_preview)

        assert connection.execute("select count(*) from strategies where symbol = 'BTCUSDT'").fetchone() == (0,)
        assert connection.execute(
            "select analysis_run_id, plateau_id from analysis_plateaus order by analysis_run_id, plateau_id"
        ).fetchall() == [("run-btc", "PBTC"), ("run-shared", "P1"), ("run-sol", "P1")]
        assert connection.execute("select count(*) from strategy_orders").fetchone() == (2,)


def test_rejected_preview_uses_effective_review_and_sticky_equity_source(maintenance_db: Path) -> None:
    with duckdb.connect(str(maintenance_db), read_only=True) as connection:
        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"], "rejected")
    by_symbol = {row["symbol"]: row for row in preview["pairs"]}
    assert {symbol: row["rows"] for symbol, row in by_symbol.items()} == {
        "BTCUSDT": 8, "ETHUSDT": 7, "SOLUSDT": 0, "XRPUSDT": 0,
    }
    assert {symbol: row["strategy_count"] for symbol, row in by_symbol.items()} == {
        "BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 0, "XRPUSDT": 0,
    }
    assert preview["pair_scoped_total"] == 15
    assert preview["global_counts"] == {}


def test_rejected_apply_retains_dedup_identity_and_removes_operational_facts(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"], "rejected")
        retained_tables = (
            "strategies", "strategy_orders", "strategy_results",
            "selection_runs", "selection_review_imports",
        )
        retained_before = {
            table: connection.execute(f"select * from {table} order by all").fetchall()
            for table in retained_tables
        }
        deleted = apply_preview(connection, preview)
        retained_after = {
            table: connection.execute(f"select * from {table} order by all").fetchall()
            for table in retained_tables
        }
        for table in retained_tables:
            if table == "strategies":
                before_without_status = [row[:9] + row[10:] for row in retained_before[table]]
                after_without_status = [row[:9] + row[10:] for row in retained_after[table]]
                assert after_without_status == before_without_status
            elif table == "strategy_results":
                before = retained_before[table]
                after = retained_after[table]
                assert [row[:4] + row[14:23] for row in after] == [row[:4] + row[14:23] for row in before]
                before_by_id = {row[1]: row for row in before}
                assert all(
                    row[4] == before_by_id[row[1]][4]
                    and row[5:14] == (None, Decimal("0"), Decimal("0"), None, None, None, None, None, None)
                    and row[23:] == (None,) * 9
                    for row in after
                    if row[1] in {1, 2}
                )
            else:
                assert retained_after[table] == retained_before[table]
        assert deleted["pair_scoped_deleted"] == 15
        assert connection.execute(
            "select strategy_id, lifecycle_status from strategies order by strategy_id"
        ).fetchall() == [(1, "DISCARDED"), (2, "DISCARDED"), (3, "ACTIVE")]
        assert connection.execute("select result_id from strategy_actions order by result_id").fetchall() == [(303,)]
        assert connection.execute("select result_id from strategy_equity order by result_id").fetchall() == [(303,)]
        assert connection.execute("select result_id from optimizer_prepared_inputs order by result_id").fetchall() == [(303,)]
        assert connection.execute("select result_id from window_metrics order by result_id").fetchall() == [(303,)]
        assert connection.execute("select result_id from equity_quality_metrics order by result_id").fetchall() == [(303,)]
        assert connection.execute("select strategy_id from strategy_tags order by strategy_id").fetchall() == [(3,)]
        assert connection.execute("select strategy_id from strategy_rejection_sources order by strategy_id").fetchall() == []
        assert connection.execute("select strategy_id from selection_results order by strategy_id").fetchall() == [(3,), (3,)]
        assert connection.execute("select strategy_id from selection_review_rows order by strategy_id").fetchall() == []
        repeat = create_preview(connection, ["BTCUSDT", "ETHUSDT"], "rejected")
        assert repeat["pair_scoped_total"] == 0
        assert [pair["strategy_count"] for pair in repeat["pairs"]] == [0, 0]


def test_apply_rejects_same_count_preview_when_target_identity_changes(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT"], "full")
        before = connection.execute("select count(*) from strategy_actions").fetchone()[0]
        connection.execute("update strategy_actions set action_index = 5 where result_id = 101")
        assert connection.execute("select count(*) from strategy_actions").fetchone()[0] == before
        with pytest.raises(ValueError, match="preview is stale"):
            apply_preview(connection, preview)
        assert connection.execute("select count(*) from strategies").fetchone()[0] == 3


def test_rejected_apply_rejects_changed_effective_strategy_set_with_empty_facts(
    maintenance_db: Path,
) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        connection.execute("delete from strategy_actions where result_id in (101, 202)")
        connection.execute("delete from strategy_equity where result_id in (101, 202)")
        connection.execute("delete from optimizer_prepared_inputs where result_id in (101, 202)")
        connection.execute("delete from strategy_rejection_sources where strategy_id = 2")
        connection.execute(
            """insert into selection_review_imports values (
                   'review-eth', 'sel-eth', 'review-eth-hash', ?, 1
               )""",
            [datetime(2026, 10, 6, tzinfo=timezone.utc)],
        )
        connection.execute(
            """insert into selection_review_rows (
                   review_import_id, strategy_id, user_status, comment
               ) values ('review-eth', 2, 'FINALIST', 'initial status')"""
        )

        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT"], "rejected")
        assert preview["_targets"]["rejected_strategy_ids"] == [1]
        assert preview["pairs"][0]["strategy_count"] == 1
        assert preview["pair_scoped_total"] == 5

        connection.execute(
            "update selection_review_rows set user_status = 'FINALIST' where review_import_id = 'review-btc'"
        )
        connection.execute(
            "update selection_review_rows set user_status = 'REJECTED' where review_import_id = 'review-eth'"
        )
        current = create_preview(connection, ["BTCUSDT", "ETHUSDT"], "rejected")
        assert current["_targets"]["rejected_strategy_ids"] == [2]

        with pytest.raises(PerformanceV2MaintenanceError, match="preview is stale"):
            apply_preview(connection, preview)


def test_rejected_retry_removes_residual_details_from_discarded_strategy(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        first = create_preview(connection, ["BTCUSDT"], "rejected")
        apply_preview(connection, first)
        connection.execute(
            """insert into strategy_actions (
                   result_id, action_index, timestamp_utc, symbol, action, size,
                   post_size, post_side, pnl, fee, balance
               ) values (101, 99, now(), 'BTCUSDT', 'opened', 1, 1, 'LONG', 0, 0, 1000)"""
        )
        retry = create_preview(connection, ["BTCUSDT"], "rejected")
        assert retry["pair_scoped_total"] == 1
        assert retry["pairs"][0]["strategy_count"] == 1
        apply_preview(connection, retry)
        assert connection.execute("select count(*) from strategy_actions where result_id = 101").fetchone() == (0,)
        assert connection.execute("select lifecycle_status from strategies where strategy_id = 1").fetchone() == ("DISCARDED",)


def test_rejected_cleanup_removes_result_facts_when_current_result_pointer_is_missing(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        connection.execute("update strategies set current_result_id = null where strategy_id = 1")
        preview = create_preview(connection, ["BTCUSDT"], "rejected")
        assert preview["pair_scoped_total"] > 0
        apply_preview(connection, preview)
        assert connection.execute("select count(*) from strategy_actions where result_id = 101").fetchone() == (0,)
        assert connection.execute("select count(*) from strategy_equity where result_id = 101").fetchone() == (0,)
        assert connection.execute("select lifecycle_status from strategies where strategy_id = 1").fetchone() == ("DISCARDED",)


def test_rejected_apply_rolls_back_all_deletes_when_one_table_fails(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT"], "rejected")
        before = {
            table: connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in maintenance._REJECTED_TABLES
        }
        before_status = connection.execute(
            "select strategy_id, lifecycle_status from strategies order by strategy_id"
        ).fetchall()

        with pytest.raises(duckdb.IOException, match="injected failure at strategy_equity"):
            apply_preview(_FailOnceOnDelete(connection, "strategy_equity"), preview)

        assert {
            table: connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in maintenance._REJECTED_TABLES
        } == before
        assert connection.execute(
            "select strategy_id, lifecycle_status from strategies order by strategy_id"
        ).fetchall() == before_status


class _FailAfterSqlFragment:
    def __init__(self, connection: duckdb.DuckDBPyConnection, fragment: str) -> None:
        self.connection = connection
        self.fragment = fragment.casefold()
        self.failed = False

    def execute(self, sql: str, parameters=None):
        if not self.failed and self.fragment in sql.casefold():
            self.failed = True
            if parameters is None:
                result = self.connection.execute(sql)
            else:
                result = self.connection.execute(sql, parameters)
            raise duckdb.IOException(f"injected failure after {self.fragment}")
        if parameters is None:
            return self.connection.execute(sql)
        return self.connection.execute(sql, parameters)


@pytest.mark.parametrize("failure_fragment", ["update strategy_results", "update strategies set lifecycle_status"])
def test_rejected_apply_rolls_back_result_compaction_and_lifecycle_update(
    maintenance_db: Path, failure_fragment: str,
) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT"], "rejected")
        before_counts = {
            table: connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in maintenance._REJECTED_TABLES
        }
        before_results = connection.execute(
            "select * from strategy_results where strategy_id in (1, 2) order by strategy_id"
        ).fetchall()
        before_status = connection.execute(
            "select strategy_id, lifecycle_status from strategies order by strategy_id"
        ).fetchall()

        with pytest.raises(duckdb.IOException, match="injected failure after"):
            apply_preview(_FailAfterSqlFragment(connection, failure_fragment), preview)

        assert {
            table: connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in maintenance._REJECTED_TABLES
        } == before_counts
        assert connection.execute(
            "select * from strategy_results where strategy_id in (1, 2) order by strategy_id"
        ).fetchall() == before_results
        assert connection.execute(
            "select strategy_id, lifecycle_status from strategies order by strategy_id"
        ).fetchall() == before_status


def test_full_apply_removes_selected_pair_reachability_and_only_global_journal(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT", "XRPUSDT"], "full")
        untouched_tables = (
            "strategies", "strategy_orders", "analysis_plateaus", "strategy_results",
            "strategy_actions", "strategy_equity", "window_metrics", "optimizer_prepared_inputs",
            "selection_runs", "selection_results", "selection_review_imports", "selection_review_rows",
            "strategy_tags", "equity_quality_metrics", "strategy_rejection_sources",
        )
        sol_before = {
            table: connection.execute(
                f"select * from {table} where "
                + ("symbol = 'SOLUSDT'" if table in {"strategies", "selection_runs"} else
                   "strategy_id = 3" if table in {"strategy_orders", "strategy_tags", "strategy_rejection_sources"} else
                   "result_id = 303" if table in {"strategy_results", "strategy_actions", "strategy_equity", "window_metrics", "optimizer_prepared_inputs", "equity_quality_metrics"} else
                   "analysis_run_id = 'run-sol'" if table == "analysis_plateaus" else
                   "selection_run_id in ('sel-sol-old', 'sel-sol-new')" if table == "selection_results" else
                   "false" if table in {"selection_review_imports", "selection_review_rows"} else "false")
                + " order by all"
            ).fetchall()
            for table in untouched_tables
        }
        events = []
        results = apply_preview(connection, preview, on_commit=events.append)
        assert results["pair_scoped_deleted"] == 26
        assert results["global_deleted"] == {"import_files": 1, "import_runs": 1}
        assert sum(int(event["rows"]) for event in events if not event["global"]) == 26
        assert [event["table"] for event in events if event["global"]] == ["import_files", "import_runs"]
        for table, rows in sol_before.items():
            query = "select * from " + table + " where " + (
                "symbol = 'SOLUSDT'" if table in {"strategies", "selection_runs"} else
                "strategy_id = 3" if table in {"strategy_orders", "strategy_tags", "strategy_rejection_sources"} else
                "result_id = 303" if table in {"strategy_results", "strategy_actions", "strategy_equity", "window_metrics", "optimizer_prepared_inputs", "equity_quality_metrics"} else
                "analysis_run_id = 'run-sol'" if table == "analysis_plateaus" else
                "selection_run_id in ('sel-sol-old', 'sel-sol-new')" if table == "selection_results" else "false"
            ) + " order by all"
            assert connection.execute(query).fetchall() == rows
        assert connection.execute("select count(*) from strategies where symbol in ('BTCUSDT', 'ETHUSDT')").fetchone() == (0,)
        assert connection.execute("select count(*) from selection_runs where symbol in ('BTCUSDT', 'ETHUSDT', 'XRPUSDT')").fetchone() == (0,)
        assert connection.execute("select count(*) from import_files").fetchone() == (0,)
        assert connection.execute("select count(*) from import_runs").fetchone() == (0,)
        assert catalog(connection) == ["SOLUSDT"]


def test_full_apply_audits_residual_selected_plateau_keys(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT"], "full")

        def restore_selected_plateau(event: dict[str, object]) -> None:
            if event["table"] == "strategies":
                connection.execute("insert into analysis_plateaus values ('run-shared', 'P1', 3, 8)")

        with pytest.raises(PerformanceV2MaintenanceError, match="selected target.*analysis_plateaus"):
            apply_preview(connection, preview, on_commit=restore_selected_plateau)

        assert connection.execute(
            "select count(*) from analysis_plateaus where analysis_run_id = 'run-shared' and plateau_id = 'P1'"
        ).fetchone() == (1,)


_FULL_DELETE_FAILURE_POINTS = (
    "strategy_rejection_sources", "selection_review_rows", "selection_review_imports", "selection_results", "selection_runs",
    "strategy_actions", "strategy_equity", "optimizer_prepared_inputs", "window_metrics",
    "equity_quality_metrics", "strategy_results", "strategy_tags",
    "strategy_orders", "analysis_plateaus", "import_files", "import_runs", "strategies",
)


class _FailOnceOnDelete:
    def __init__(self, connection: duckdb.DuckDBPyConnection, table: str) -> None:
        self.connection = connection
        self.table = table
        self.failed = False

    def execute(self, sql: str, parameters=None):
        if not self.failed and sql.lstrip().casefold().startswith(f"delete from {self.table}"):
            self.failed = True
            raise duckdb.IOException(f"injected failure at {self.table}")
        if parameters is None:
            return self.connection.execute(sql)
        return self.connection.execute(sql, parameters)


class _MissingMigratedResultsTableOnce:
    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self.connection = connection
        self.failed = False

    def execute(self, sql: str, parameters=None):
        if not self.failed and sql.lstrip().casefold().startswith("delete from strategies"):
            self.failed = True
            raise duckdb.CatalogException(
                "Catalog Error: table with name '__performance_v2_v7_strategy_results' DOES NOT EXIST!"
            )
        if parameters is None:
            return self.connection.execute(sql)
        return self.connection.execute(sql, parameters)


class _FailLegacyStrategyRetry:
    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self.connection = connection
        self.strategy_delete_attempts = 0

    def execute(self, sql: str, parameters=None):
        if sql.lstrip().casefold().startswith("delete from strategies"):
            self.strategy_delete_attempts += 1
            if self.strategy_delete_attempts == 1:
                raise duckdb.CatalogException(
                    "Catalog Error: Table with name __performance_v2_v7_strategy_results does not exist!"
                )
            raise duckdb.IOException("injected legacy strategy retry failure")
        if parameters is None:
            return self.connection.execute(sql)
        return self.connection.execute(sql, parameters)


class _FailOnStrategyDelete:
    def __init__(self, connection: duckdb.DuckDBPyConnection, error: BaseException) -> None:
        self.connection = connection
        self.error = error

    def execute(self, sql: str, parameters=None):
        if sql.lstrip().casefold().startswith("delete from strategies"):
            raise self.error
        if parameters is None:
            return self.connection.execute(sql)
        return self.connection.execute(sql, parameters)


def test_full_delete_recovers_from_legacy_migrated_strategy_results_reference(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        preview = create_preview(connection, ["BTCUSDT"], "full")
        result = apply_preview(_MissingMigratedResultsTableOnce(connection), preview)

        assert result["pair_scoped_deleted"] == preview["pair_scoped_total"]
        assert result["global_deleted"] == preview["global_counts"]
        for table, count in preview["table_counts"].items():
            assert result["table_counts"].get(table, 0) == count
        for table in maintenance._PAIR_TABLES:
            where, params = maintenance._target_predicate(table, preview["_targets"])
            assert maintenance._count_where(connection, table, where, params) == 0
        assert connection.execute(
            "select count(*) from strategies where symbol = 'BTCUSDT'"
        ).fetchone() == (0,)
        assert result["table_counts"]["strategies"] == 1
        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("9",)
        assert connection.execute(
            "select count(*) from information_schema.tables "
            "where table_schema = 'main' and table_name = '__performance_v2_v7_strategy_results'"
        ).fetchone() == (0,)


def test_legacy_recovery_rolls_back_compatibility_table_when_strategy_retry_fails(maintenance_db: Path) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        preview = create_preview(connection, ["BTCUSDT"], "full")

        with pytest.raises(duckdb.IOException, match="injected legacy strategy retry failure"):
            apply_preview(_FailLegacyStrategyRetry(connection), preview)

        assert connection.execute("select count(*) from strategies where symbol = 'BTCUSDT'").fetchone() == (1,)
        assert connection.execute(
            "select count(*) from information_schema.tables "
            "where table_schema = 'main' and table_name = '__performance_v2_v7_strategy_results'"
        ).fetchone() == (0,)
        assert connection.execute("select value from schema_info where key = 'schema_version'").fetchone() == ("9",)


@pytest.mark.parametrize(
    "error",
    [
        duckdb.CatalogException("Catalog Error: Table with name another_relation does not exist!"),
        RuntimeError("injected strategy failure"),
    ],
)
def test_unrelated_strategy_delete_errors_are_propagated_unchanged(
    maintenance_db: Path, error: BaseException,
) -> None:
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        preview = create_preview(connection, ["BTCUSDT"], "full")

        with pytest.raises(type(error)) as raised:
            apply_preview(_FailOnStrategyDelete(connection, error), preview)

        assert raised.value is error
        assert connection.execute(
            "select count(*) from information_schema.tables "
            "where table_schema = 'main' and table_name = '__performance_v2_v7_strategy_results'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("failure_table", _FULL_DELETE_FAILURE_POINTS)
def test_full_delete_failure_can_be_repreviewed_and_retried(
    maintenance_db: Path, failure_table: str,
) -> None:
    selected = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    with _writable_fixture(maintenance_db, maintenance_db.parent) as connection:
        sol_before = connection.execute("select * from strategies where strategy_id = 3").fetchall()
        sol_plateau_before = connection.execute(
            "select * from analysis_plateaus where analysis_run_id = 'run-sol'"
        ).fetchall()
        initial = create_preview(connection, selected, "full")
        with pytest.raises(duckdb.IOException, match=f"injected failure at {failure_table}"):
            apply_preview(_FailOnceOnDelete(connection, failure_table), initial)

        if failure_table == "strategies":
            assert "XRPUSDT" not in catalog(connection)
        retry = create_preview(connection, selected, "full", recovery_preview=initial)
        apply_preview(connection, retry)

        assert connection.execute("select * from strategies where strategy_id = 3").fetchall() == sol_before
        assert connection.execute(
            "select * from analysis_plateaus where analysis_run_id = 'run-sol'"
        ).fetchall() == sol_plateau_before
        assert connection.execute(
            "select count(*) from strategies where symbol in (select unnest(?::varchar[]))", [selected]
        ).fetchone() == (0,)
        assert connection.execute(
            "select count(*) from selection_runs where symbol in (select unnest(?::varchar[]))", [selected]
        ).fetchone() == (0,)
        assert connection.execute("select count(*) from import_files").fetchone() == (0,)
        assert connection.execute("select count(*) from import_runs").fetchone() == (0,)
        assert connection.execute(
            "select count(*) from strategy_orders orders left join strategies using (strategy_id) where strategies.strategy_id is null"
        ).fetchone() == (0,)
        assert connection.execute(
            "select count(*) from strategy_results results left join strategies using (strategy_id) where strategies.strategy_id is null"
        ).fetchone() == (0,)
        assert connection.execute(
            "select count(*) from strategy_actions facts left join strategy_results using (result_id) where strategy_results.result_id is null"
        ).fetchone() == (0,)
        assert connection.execute(
            "select count(*) from selection_results rows left join selection_runs using (selection_run_id) where selection_runs.selection_run_id is null"
        ).fetchone() == (0,)
        assert connection.execute(
            "select count(*) from selection_review_rows rows left join selection_review_imports using (review_import_id) where selection_review_imports.review_import_id is null"
        ).fetchone() == (0,)
        assert connection.execute(
            "select count(*) from analysis_plateaus plateaus where not exists (select 1 from strategy_orders orders where orders.analysis_run_id = plateaus.analysis_run_id and orders.plateau_id = plateaus.plateau_id)"
        ).fetchone() == (0,)


def _benchmark_worker(database: str, fixture_root: str, workers: int, output) -> None:
    target = Path(database)
    root = Path(fixture_root)
    started = time.perf_counter()
    with _writable_fixture(target, root) as connection:
        effective_workers = set_query_workers(connection, workers)
        preview = create_preview(connection, ["BTCUSDT", "ETHUSDT", "XRPUSDT"], "full")
        events: list[dict[str, object]] = []
        result = apply_preview(connection, preview, on_commit=events.append)
        digest = hashlib.sha256()
        tables = (
            "strategies", "strategy_orders", "strategy_results", "strategy_actions", "strategy_equity",
            "window_metrics", "optimizer_prepared_inputs", "equity_quality_metrics", "strategy_tags",
            "strategy_rejection_sources", "selection_runs", "selection_results", "selection_review_imports",
            "selection_review_rows", "analysis_plateaus", "import_files", "import_runs",
        )
        for table in tables:
            cursor = connection.execute(f"select * from {table} order by all")
            while rows := cursor.fetchmany(2048):
                for row in rows:
                    digest.update(json.dumps(row, default=str, separators=(",", ":")).encode())
                    digest.update(b"\n")
        pair_counts: dict[str, dict[str, int]] = {}
        for event in events:
            if event.get("global"):
                continue
            table = str(event["table"])
            for symbol, count in event["rows_by_symbol"].items():
                pair_counts.setdefault(str(symbol), {})[table] = int(count)
    process_info = psutil.Process().memory_info()
    peak_rss = int(getattr(process_info, "peak_wset", process_info.rss))
    output.put({
        "workers": effective_workers, "elapsed_seconds": time.perf_counter() - started,
        "peak_rss_bytes": peak_rss, "final_sha256": digest.hexdigest(),
        "table_counts": result["table_counts"], "pair_counts": pair_counts,
        "global_counts": result["global_deleted"],
    })


def test_full_delete_workers_have_identical_results_and_report_benchmark(maintenance_db: Path, tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    measurements = []
    for workers in (1, 8):
        fixture_root = tmp_path / f"workers-{workers}"
        fixture_root.mkdir()
        target = fixture_root / "performance.duckdb"
        shutil.copyfile(maintenance_db, target)
        output = context.Queue()
        process = context.Process(target=_benchmark_worker, args=(str(target), str(fixture_root), workers, output))
        process.start()
        process.join(90)
        if process.is_alive():
            process.terminate()
            process.join(5)
            pytest.fail(f"workers={workers} synthetic benchmark timed out")
        assert process.exitcode == 0
        measurements.append(output.get(timeout=5))
        output.close()
        output.join_thread()

    single, configured = measurements
    assert single["workers"] == 1 and 1 <= configured["workers"] <= 8
    for field in ("final_sha256", "table_counts", "pair_counts", "global_counts"):
        assert single[field] == configured[field]
    print("maintenance_worker_benchmark=" + json.dumps(measurements, sort_keys=True))
