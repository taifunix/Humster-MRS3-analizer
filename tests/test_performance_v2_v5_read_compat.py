from __future__ import annotations

from datetime import date
from pathlib import Path
import shutil

import duckdb
import pytest

from mrs3.performance_v2_finalist_retest import (
    FinalistRetestCohort,
    apply_finalist_retest_outcomes,
    cohort_digest,
    current_effective_finalist_members,
    freeze_finalist_cohort,
)
from mrs3.performance_v2_optimizer import prepare_current_optimizer_inputs, read_prepared_optimizer_inputs
from mrs3.performance_v2_retest import RetestStatus, build_retest_manifest, mark_retest_from_audit, retest_status
from mrs3.performance_v2_store import PerformanceV2StoreError, require_performance_v2_readable
from mrs3.portfolio.input import (
    SOURCE_SNAPSHOT_UNAVAILABLE,
    PortfolioInputError,
    read_current_finalists,
    read_performance_snapshot,
)
from tests.test_portfolio_input import IDENTITY, REQUEST, _database
from tests.test_performance_v2_retest import _write_workbook


def _database_state(database: Path) -> tuple[bytes, tuple[str, ...]]:
    return database.read_bytes(), tuple(sorted(path.name for path in database.parent.iterdir()))


def _v5_database(tmp_path: Path) -> tuple[Path, int, int]:
    source_dir = tmp_path / "v6"
    source_dir.mkdir()
    source = _database(source_dir)
    with duckdb.connect(str(source)) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        connection.execute(
            "insert into strategy_tags values (?, 'RETEST', 'TEST', 'fixture', now())",
            [strategy_id],
        )
        connection.execute(
            "update strategy_orders set open_multiplier = .9875 where strategy_id = ?",
            [strategy_id],
        )

    database = tmp_path / "v5.duckdb"
    shutil.copy2(source, database)
    with duckdb.connect(str(database)) as connection:
        assert require_performance_v2_readable(connection) == 6
        connection.execute("drop table equity_quality_metrics")
        connection.execute("update schema_info set value = '5' where key = 'schema_version'")
        assert require_performance_v2_readable(connection) == 5
    return database, int(strategy_id), int(result_id)


def test_v5_retest_status_and_manifest_are_read_only(tmp_path: Path) -> None:
    database, _strategy_id, _result_id = _v5_database(tmp_path)
    output_dir = tmp_path / "output-root" / "manifest"
    output_dir.mkdir(parents=True)
    template = Path(__file__).resolve().parents[1] / "templates/strategies/retest-mrs3/base.json"

    before = _database_state(database)
    with duckdb.connect(str(database), read_only=True) as connection:
        assert retest_status(connection) == RetestStatus(active_count=1)
    assert _database_state(database) == before

    before = _database_state(database)
    with duckdb.connect(str(database), read_only=True) as connection:
        batch = build_retest_manifest(connection, {"LONG": template}, output_dir)
    assert batch.strategy_count == 1
    assert _database_state(database) == before


def test_v5_finalist_retest_reads_are_read_only(tmp_path: Path) -> None:
    database, strategy_id, _result_id = _v5_database(tmp_path)
    before = _database_state(database)

    with duckdb.connect(str(database), read_only=True) as connection:
        cohort = freeze_finalist_cohort(
            connection,
            test_start="2026-01-01",
            test_end="2026-01-31",
            listing_dates={"BTCUSDT": date(2025, 12, 1)},
        )

    assert [member["strategy_id"] for member in cohort.members] == [strategy_id]
    assert _database_state(database) == before


def test_v5_prepared_optimizer_read_is_read_only(tmp_path: Path) -> None:
    database, _strategy_id, result_id = _v5_database(tmp_path)
    before = _database_state(database)

    rows = read_prepared_optimizer_inputs(str(database), [result_id])

    assert len(rows) == 1
    assert rows[0].prepared is not None
    assert _database_state(database) == before


def test_v5_portfolio_reads_are_read_only(tmp_path: Path) -> None:
    database, _strategy_id, _result_id = _v5_database(tmp_path)
    before = _database_state(database)

    snapshot = read_performance_snapshot(database, [REQUEST], **IDENTITY)
    assert _database_state(database) == before
    before = _database_state(database)
    finalists = read_current_finalists(database, [("BTCUSDT", "LONG")], include_series=False)

    assert snapshot.source_schema_version == "5"
    assert len(finalists) == 1
    assert _database_state(database) == before


def test_v5_writer_paths_still_require_v6(tmp_path: Path) -> None:
    database, strategy_id, result_id = _v5_database(tmp_path)
    audit = _write_workbook(tmp_path / "audit.xlsx", [strategy_id], [])
    cohort = FinalistRetestCohort(
        "FINALIST", "2026-01-01", "2026-01-31", 24, (), (), cohort_digest(())
    )
    before = _database_state(database)

    with duckdb.connect(str(database), read_only=True) as connection:
        with pytest.raises(PerformanceV2StoreError, match="^Performance database does not have schema version 6$"):
            mark_retest_from_audit(connection, audit)
        assert _database_state(database) == before
        with pytest.raises(PerformanceV2StoreError, match="^Performance database does not have schema version 6$"):
            apply_finalist_retest_outcomes(connection, cohort, {}, job_id="v5")
        assert _database_state(database) == before
        with pytest.raises(PerformanceV2StoreError, match="^Performance database does not have schema version 6$"):
            current_effective_finalist_members(connection)
        assert _database_state(database) == before

    with pytest.raises(PerformanceV2StoreError, match="^Performance database does not have schema version 6$"):
        prepare_current_optimizer_inputs(str(database), [result_id])
    assert _database_state(database) == before


def test_v6_missing_equity_table_fails_closed_in_all_wrappers(tmp_path: Path) -> None:
    database, _strategy_id, _result_id = _v5_database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("update schema_info set value = '6' where key = 'schema_version'")
    before = _database_state(database)

    with duckdb.connect(str(database), read_only=True) as connection:
        with pytest.raises(PerformanceV2StoreError, match="unexpected catalog"):
            retest_status(connection)
    assert _database_state(database) == before

    with pytest.raises(PortfolioInputError) as snapshot_error:
        read_performance_snapshot(database, [REQUEST], **IDENTITY)
    assert snapshot_error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE
    assert _database_state(database) == before

    with pytest.raises(PortfolioInputError) as finalist_error:
        read_current_finalists(database, [("BTCUSDT", "LONG")], include_series=False)
    assert finalist_error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE
    assert _database_state(database) == before
