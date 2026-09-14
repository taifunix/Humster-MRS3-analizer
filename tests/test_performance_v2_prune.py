from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys

import duckdb
import pytest

import mrs3.performance_v2_prune as prune_module
from mrs3.performance_v2_prune import prune_performance_v2
from mrs3.performance_v2_store import PerformanceV2Config, initialize_performance_v2, performance_v2_database_path


CUTOFF = datetime(2026, 9, 6, tzinfo=UTC)


def _prune_script() -> object:
    path = Path(__file__).resolve().parents[1] / "scripts" / "prune_performance_v2.py"
    spec = importlib.util.spec_from_file_location("performance_v2_prune_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _database(tmp_path: Path) -> Path:
    config = PerformanceV2Config(
        tmp_path / "data" / "performance-v2", v1_database_root=tmp_path / "legacy"
    )
    database = performance_v2_database_path(config)
    database.parent.mkdir(parents=True)
    with duckdb.connect(str(database)) as connection:
        initialize_performance_v2(connection)
        connection.execute("insert into analysis_plateaus values ('run', 'P1', 1, 1)")
        for name, report_end in (
            ("fresh", "2026-09-06 00:00:00+00"),
            ("old-finalist", "2026-01-01 00:00:00+00"),
            ("old-reserve", "2026-01-02 00:00:00+00"),
            ("old-unprotected", "2026-01-03 00:00:00+00"),
        ):
            strategy_id = connection.execute(
                """
                insert into strategies (
                    strategy_name, symbol, side, timeframe, close_ma_len, order_count,
                    analysis_run_id, candidate_identity, lifecycle_status,
                    created_at_utc, updated_at_utc
                ) values (?, 'BTCUSDT', 'LONG', '1h', 20, 1, 'run', ?, 'ACTIVE', now(), now())
                returning strategy_id
                """,
                [name, name],
            ).fetchone()[0]
            result_id = connection.execute(
                """
                insert into strategy_results (
                    strategy_id, report_start_utc, report_end_utc, exchange,
                    commission_rate, initial_balance, final_balance, imported_at_utc
                ) values (?, '2026-01-01', ?, 'BYBIT', .001, 100, 101, now())
                returning result_id
                """,
                [strategy_id, report_end],
            ).fetchone()[0]
            connection.execute(
                """
                insert into strategy_orders (
                    strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
                    analysis_run_id, plateau_id, base_point_trades
                ) values (?, 1, 10, 1, 0, 1, 'run', 'P1', 1)
                """,
                [strategy_id],
            )
            connection.execute(
                "insert into strategy_actions values (?, 0, ?, 'BTCUSDT', 1, 'opened', 1, 1, 'LONG', 0, 0, 100, '{}')",
                [result_id, report_end],
            )
            connection.execute(
                "insert into strategy_equity values (?, 0, ?, 100, 100)", [result_id, report_end]
            )
            connection.execute(
                "insert into window_metrics (result_id, requested_start_utc, requested_end_utc, metrics_version, availability_status, calculated_at_utc) values (?, ?, ?, 'v1', 'AVAILABLE', now())",
                [result_id, "2026-01-01", report_end],
            )
            connection.execute(
                "insert into strategy_tags values (?, 'RETEST', 'TEST', ?, now())", [strategy_id, name]
            )

        instance_id = connection.execute(
            "select value from schema_info where key = 'database_instance_id'"
        ).fetchone()[0]
        for number, reserve_status in ((1, "FINALIST"), (2, "RESERVE")):
            run_id = f"selection-{number}"
            review_id = f"review-{number}"
            connection.execute(
                """
                insert into selection_runs (
                    selection_run_id, database_instance_id, symbol, side, selection_contract_version,
                    request_json, request_sha256, config_json, config_sha256, candidate_count,
                    representative_count, auto_finalist_count, top_n, workbook_sha256, created_at_utc
                ) values (?, ?, 'BTCUSDT', 'LONG', 'v1', '{}', ?, '{}', ?, 2, 2, 1, 2, ?, ?)
                """,
                [run_id, instance_id, run_id, run_id, run_id, f"2026-09-0{number} 00:00:00+00"],
            )
            for strategy_id in (2, 3):
                connection.execute(
                    "insert into selection_results values (?, ?, ?, 'FILTERED', null, null, null, null, null, false, '{}')",
                    [run_id, strategy_id, strategy_id],
                )
            connection.execute(
                "insert into selection_review_imports values (?, ?, ?, ?, 2)",
                [review_id, run_id, review_id, f"2026-09-0{number} 01:00:00+00"],
            )
            connection.execute(
                "insert into selection_review_rows values (?, 2, 'FINALIST', 1, null, null)",
                [review_id],
            )
            connection.execute(
                "insert into selection_review_rows values (?, 3, ?, null, null, null)",
                [review_id, reserve_status],
            )
    return database


def test_preview_keeps_fresh_and_ranked_rows_without_mutation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    before = database.read_bytes()

    result = prune_performance_v2(database, CUTOFF)

    assert result["mode"] == "preview"
    assert result["counts"]["strategies"] == 2
    assert result["protected_strategies"] == 2
    assert database.read_bytes() == before
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (4,)


def test_apply_backups_and_deletes_only_old_unprotected_strategy(tmp_path: Path) -> None:
    database = _database(tmp_path)

    result = prune_performance_v2(database, CUTOFF, apply=True)

    backup = Path(result["backup_path"])
    assert result["mode"] == "apply"
    assert backup.is_file()
    assert backup.parent == database.parent / "backups"
    with duckdb.connect(str(backup), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (4,)
    assert result["counts"] == {
        "window_metrics": 2,
        "strategy_actions": 2,
        "strategy_equity": 2,
        "strategy_results": 2,
        "strategy_tags": 2,
        "strategy_orders": 2,
        "strategies": 2,
    }
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("select strategy_name from strategies order by strategy_name").fetchall() == [
            ("fresh",),
            ("old-finalist",),
        ]
        assert connection.execute("select count(*) from selection_runs").fetchone() == (2,)
        assert connection.execute("select count(*) from selection_results").fetchone() == (4,)
        assert connection.execute("select count(*) from selection_review_rows").fetchone() == (4,)
        for table in ("window_metrics", "strategy_actions", "strategy_equity", "strategy_results", "strategy_tags", "strategy_orders"):
            assert connection.execute(f"select count(*) from {table}").fetchone() == (2,)


def test_apply_holds_the_writer_lock_through_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path)
    events: list[str] = []
    lock_type = prune_module.PerformanceV2WriterLock
    delete = prune_module._delete

    class RecordingLock:
        def __init__(self, root: Path) -> None:
            self._lock = lock_type(root)

        def __enter__(self) -> "RecordingLock":
            self._lock.__enter__()
            events.append("enter")
            return self

        def __exit__(self, *args: object) -> None:
            events.append("exit")
            self._lock.__exit__(*args)

    def checked_delete(connection: duckdb.DuckDBPyConnection, stale_ids: list[int]) -> None:
        assert events == ["enter"]
        delete(connection, stale_ids)

    monkeypatch.setattr(prune_module, "PerformanceV2WriterLock", RecordingLock)
    monkeypatch.setattr(prune_module, "_delete", checked_delete)
    prune_performance_v2(database, CUTOFF, apply=True)

    assert events == ["enter", "exit"]


def test_apply_restores_backup_after_partial_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path)

    def fail_after_delete(connection: duckdb.DuckDBPyConnection, stale_ids: list[int]) -> None:
        connection.execute("delete from strategy_actions where result_id = 4")
        raise RuntimeError("forced failure")

    monkeypatch.setattr(prune_module, "_delete", fail_after_delete)
    with pytest.raises(prune_module.PerformanceV2PruneError, match="restored from"):
        prune_performance_v2(database, CUTOFF, apply=True)

    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (4,)
        assert connection.execute("select count(*) from strategy_actions").fetchone() == (4,)


def test_restore_removes_a_stale_wal_before_replacing_database(tmp_path: Path) -> None:
    database = _database(tmp_path)
    backup = database.with_name("backup.duckdb")
    backup.write_bytes(database.read_bytes())
    wal = Path(f"{database}.wal")
    wal.write_bytes(b"stale WAL must not survive restore")
    database.write_bytes(b"corrupted")

    prune_module._restore(backup, database)

    assert not wal.exists()
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (4,)


def test_preview_preserves_a_strategy_without_a_result(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            """
            insert into strategies (
                strategy_name, symbol, side, timeframe, close_ma_len, order_count,
                analysis_run_id, candidate_identity, lifecycle_status,
                created_at_utc, updated_at_utc
            ) values ('untested', 'BTCUSDT', 'LONG', '1h', 20, 1, 'run', 'untested', 'ACTIVE', now(), now())
            """
        )

    result = prune_performance_v2(database, CUTOFF)

    assert result["counts"]["strategies"] == 2
    assert result["protected_strategies"] == 3


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("state", ["missing", "empty", "garbage", "foreign_schema"])
def test_rejects_missing_empty_or_invalid_database_before_backup(
    tmp_path: Path, state: str, apply: bool
) -> None:
    database = tmp_path / "data" / "performance-v2" / "performance-v2.duckdb"
    database.parent.mkdir(parents=True)
    if state == "empty":
        database.touch()
    elif state == "garbage":
        database.write_bytes(b"not a DuckDB database")
    elif state == "foreign_schema":
        with duckdb.connect(str(database)) as connection:
            connection.execute("create table unrelated (id integer)")

    with pytest.raises(prune_module.PerformanceV2PruneError):
        prune_performance_v2(database, CUTOFF, apply=apply)

    assert not (database.parent / "backups").exists()


def test_cli_reports_a_missing_config_as_an_argument_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _prune_script()
    monkeypatch.setattr(sys, "argv", ["prune_performance_v2.py", "--config", str(tmp_path / "missing.json")])

    with pytest.raises(SystemExit) as error:
        script.main()

    assert error.value.code == 2
    assert "invalid config.performance.json" in capsys.readouterr().err
