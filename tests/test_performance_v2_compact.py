from __future__ import annotations

from pathlib import Path
import os

import duckdb
import pytest
import shutil

import mrs3.performance_v2_compact as compact_module
from mrs3.performance_v2_compact import compact_performance_v2


def test_compact_refuses_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    target = tmp_path / "target.duckdb"
    source.write_bytes(b"fixture")
    target.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        compact_performance_v2(source, target, workers=1)

    assert target.read_bytes() == b"keep"


def test_compact_copies_complete_small_v9_fixture(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    (tmp_path / "source").mkdir()
    connection = _candidate_db(tmp_path / "source")
    source = tmp_path / "source" / "strategy_performance.duckdb"
    strategy_id, result_id = connection.execute(
        "select strategy_id, result_id from strategy_results"
    ).fetchone()
    selection_run_id = "compact-v9-run"
    instance_id = connection.execute(
        "select value from schema_info where key = 'database_instance_id'"
    ).fetchone()[0]
    connection.execute(
        """insert into selection_runs values
           (?, ?, 'BTCUSDT', 'LONG', 'v1', '{}', 'request', '{}', 'config', 1, 1, 0, 1, 'workbook', now())""",
        [selection_run_id, instance_id],
    )
    snapshot_json = '{"snapshot":"preserve exact value"}'
    connection.execute(
        """insert into selection_results (
               selection_run_id, strategy_id, result_id_at_selection, auto_status,
               auto_reason, prior_rejected, stage_trace_json, equity_regime_json
           ) values (?, ?, ?, 'FILTERED', 'test', false, '{}', ?)""",
        [selection_run_id, strategy_id, result_id, snapshot_json],
    )
    source_evidence = (
        999, "EQUITY_REGIME_FILTER", "W28_DOWN", 999, "orphan-run",
        "equity-regime-v1", "source-revision", "a" * 64,
    )
    connection.execute(
        """insert into strategy_rejection_sources (
               strategy_id, source_kind, reason_code, first_result_id,
               first_selection_run_id, classifier_algo_version, source_revision,
               facts_sha256, created_at_utc
           ) values (?, ?, ?, ?, ?, ?, ?, ?, now())""",
        source_evidence,
    )
    selection_result_before = connection.execute(
        "select * from selection_results"
    ).fetchall()
    rejection_sources_before = connection.execute(
        "select * from strategy_rejection_sources"
    ).fetchall()
    rejection_constraints_before = connection.execute(
        """select constraint_type, constraint_column_names, referenced_table,
                  referenced_column_names
             from duckdb_constraints()
            where schema_name = 'main' and table_name = 'strategy_rejection_sources'
            order by constraint_index"""
        ).fetchall()
    before = {
        table: connection.execute(f"select count(*) from {table}").fetchone()[0]
        for table in (
            "strategies", "analysis_plateaus", "strategy_orders", "strategy_results",
            "strategy_actions", "strategy_equity", "window_metrics", "import_runs",
            "import_files", "selection_runs", "selection_results",
            "selection_review_imports", "selection_review_rows", "strategy_tags",
            "optimizer_prepared_inputs", "equity_quality_metrics", "strategy_rejection_sources",
        )
    }
    connection.close()
    target = tmp_path / "target" / "candidate.duckdb"
    target.parent.mkdir()

    report = compact_performance_v2(source, target, workers=2)

    assert report["source_stat_unchanged"] is True
    assert report["source_schema_version"] == 9
    assert report["target"]["schema_version"] == 9
    assert report["memory_limit"] == "16GB"
    assert report["table_counts"] == {name: int(count) for name, count in before.items()}
    assert report["verified_table_counts"] == report["table_counts"]
    with duckdb.connect(str(target), read_only=True) as compacted:
        assert compacted.execute("select * from selection_results").fetchall() == selection_result_before
        assert compacted.execute("select * from strategy_rejection_sources").fetchall() == rejection_sources_before
        assert compacted.execute(
            "select count(*) from duckdb_constraints() where schema_name = 'main' "
            "and table_name = 'strategy_rejection_sources' and constraint_type = 'FOREIGN KEY'"
        ).fetchone() == (0,)
        assert compacted.execute(
            """select constraint_type, constraint_column_names, referenced_table,
                      referenced_column_names
                 from duckdb_constraints()
                where schema_name = 'main' and table_name = 'strategy_rejection_sources'
                order by constraint_index"""
        ).fetchall() == rejection_constraints_before


def test_compact_transforms_prepared_payload_before_insert(tmp_path: Path) -> None:
    from tests.test_performance_v2_optimizer import _typed_candidate_database
    from mrs3.performance_v2_optimizer import (
        decode_prepared_storage,
        prepare_current_optimizer_inputs,
    )

    source, (result_id,) = _typed_candidate_database(tmp_path / "source")
    prepare_current_optimizer_inputs(str(source), [result_id], workers=2)
    target = tmp_path / "target" / "candidate.duckdb"
    target.parent.mkdir()
    with source.open("rb") as handle:
        source_before = handle.read()
    report = compact_performance_v2(source, target, workers=2)

    import duckdb

    with duckdb.connect(str(source), read_only=True) as source_connection, duckdb.connect(
        str(target), read_only=True
    ) as target_connection:
        source_payload = source_connection.execute(
            "select prepared_json from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone()[0]
        target_payload = target_connection.execute(
            "select prepared_json from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone()[0]
    assert target_payload.startswith("mrs3-zlib-v1:")
    assert decode_prepared_storage(target_payload) == decode_prepared_storage(source_payload)
    assert report["prepared"]["prepared_rows"] == 1
    assert source.read_bytes() == source_before


def test_compact_preserves_semantically_invalid_legacy_payload(tmp_path: Path) -> None:
    from tests.test_performance_v2_optimizer import _typed_candidate_database
    from mrs3.performance_v2_optimizer import decode_prepared_storage, prepare_current_optimizer_inputs

    source, (result_id,) = _typed_candidate_database(tmp_path / "source")
    prepare_current_optimizer_inputs(str(source), [result_id], workers=1)
    legacy_payload = '{"not_a_prepared_input":true}'
    with duckdb.connect(str(source)) as connection:
        connection.execute(
            "update optimizer_prepared_inputs set prepared_json = ? where result_id = ?",
            [legacy_payload, result_id],
        )
    target = tmp_path / "target.duckdb"

    compact_performance_v2(source, target, workers=1)

    with duckdb.connect(str(target), read_only=True) as connection:
        target_payload = connection.execute(
            "select prepared_json from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone()[0]
    assert decode_prepared_storage(target_payload) == legacy_payload


def test_compact_migrates_empty_v6_schema_to_v9(tmp_path: Path) -> None:
    from tests.test_performance_v2_store import _initialize_v6_fixture

    source = tmp_path / "source.duckdb"
    with duckdb.connect(str(source)) as connection:
        _initialize_v6_fixture(connection)
    target = tmp_path / "target.duckdb"

    report = compact_performance_v2(source, target, workers=1)

    assert report["source_schema_version"] == 6
    assert report["target"]["schema_version"] == 9


def test_compact_accepts_v5_commission_rate_not_nullability(tmp_path: Path) -> None:
    from tests.test_performance_v2_store import _initialize_v4_fixture
    from mrs3.performance_v2_store import _migrate_schema_v4_to_v5, require_performance_v2_readable

    source = tmp_path / "source-v5.duckdb"
    with duckdb.connect(str(source)) as connection:
        _initialize_v4_fixture(connection)
        _migrate_schema_v4_to_v5(connection)
        assert require_performance_v2_readable(connection) == 5
        assert connection.execute(
            "select is_nullable from information_schema.columns "
            "where table_name = 'strategy_results' and column_name = 'commission_rate'"
        ).fetchone() == ("NO",)

    target = tmp_path / "target-v9.duckdb"
    report = compact_performance_v2(source, target, workers=1)

    assert report["source_schema_version"] == 5
    assert report["target"]["schema_version"] == 9
    with duckdb.connect(str(source), read_only=True) as connection:
        assert require_performance_v2_readable(connection) == 5
        assert connection.execute(
            "select is_nullable from information_schema.columns "
            "where table_name = 'strategy_results' and column_name = 'commission_rate'"
        ).fetchone() == ("NO",)


def test_compact_migrates_v8_selection_rows_without_backfill(tmp_path: Path) -> None:
    from tests.test_performance_v2_store import (
        _initialize_v6_fixture,
        _migrate_schema_v6_to_v7,
        _migrate_schema_v7_to_v8,
        _strategy,
    )
    from mrs3.performance_v2_store import require_performance_v2_readable

    source = tmp_path / "source-v8.duckdb"
    selection_run_id = "compact-v8-run"
    with duckdb.connect(str(source)) as connection:
        _initialize_v6_fixture(connection)
        _migrate_schema_v6_to_v7(connection)
        _migrate_schema_v7_to_v8(connection)
        strategy_id = _strategy(connection, name="compact-v8")
        result_id = connection.execute(
            """insert into strategy_results (
                   strategy_id, report_start_utc, report_end_utc, exchange,
                   commission_rate, initial_balance, final_balance, imported_at_utc
               ) values (?, '2026-01-01', '2026-01-02', 'BYBIT', .001, 100, 101, now())
               returning result_id""",
            [strategy_id],
        ).fetchone()[0]
        connection.execute(
            "update strategies set current_result_id = ? where strategy_id = ?",
            [result_id, strategy_id],
        )
        connection.execute(
            "insert into equity_quality_metrics values (?, 'legacy-revision', 'R7.3', 'legacy-facts', ?, now())",
            [result_id, "a" * 64],
        )
        instance_id = connection.execute(
            "select value from schema_info where key = 'database_instance_id'"
        ).fetchone()[0]
        connection.execute(
            """insert into selection_runs values
               (?, ?, 'BTCUSDT', 'LONG', 'v1', '{}', 'request', '{}', 'config', 1, 1, 0, 1, 'workbook', now())""",
            [selection_run_id, instance_id],
        )
        connection.execute(
            """insert into selection_results (
                   selection_run_id, strategy_id, result_id_at_selection, auto_status,
                   auto_reason, prior_rejected, stage_trace_json
               ) values (?, ?, ?, 'FILTERED', 'legacy', false, '{}')""",
            [selection_run_id, strategy_id, result_id],
        )
        selection_before = connection.execute("select * from selection_results").fetchall()
        metrics_before = connection.execute("select * from equity_quality_metrics").fetchall()
        assert len(selection_before) == 1

    target = tmp_path / "target-v9.duckdb"
    report = compact_performance_v2(source, target, workers=1)

    assert report["source_schema_version"] == 8
    assert report["target"]["schema_version"] == 9
    assert report["table_counts"]["selection_results"] == 1
    assert report["verified_table_counts"]["selection_results"] == 1
    assert report["table_counts"]["strategy_rejection_sources"] == 0
    assert report["verified_table_counts"]["strategy_rejection_sources"] == 0
    with duckdb.connect(str(target), read_only=True) as connection:
        assert require_performance_v2_readable(connection) == 9
        assert connection.execute("select * from selection_results").fetchall() == [
            (*selection_before[0], None)
        ]
        assert connection.execute("select * from equity_quality_metrics").fetchall() == metrics_before
        assert connection.execute("select count(*) from strategy_rejection_sources").fetchone() == (0,)
        assert connection.execute(
            "select count(*) from selection_results where equity_regime_json is not null"
        ).fetchone() == (0,)
    with duckdb.connect(str(source), read_only=True) as connection:
        assert require_performance_v2_readable(connection) == 8
        assert connection.execute(
            "select count(*) from information_schema.tables where table_name = 'strategy_rejection_sources'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("version", [6, 7])
def test_compact_preserves_populated_legacy_v6_v7_and_sequence_gap(
    tmp_path: Path, version: int
) -> None:
    from tests.test_performance_v2_optimizer import _source
    from tests.test_performance_v2_store import (
        _initialize_v6_fixture,
        _insert_result_with_children,
        _migrate_schema_v6_to_v7,
        _strategy,
    )
    from mrs3.performance_v2_optimizer import build_prepared_input, source_digest

    source = tmp_path / f"source-{version}.duckdb"
    with duckdb.connect(str(source)) as connection:
        _initialize_v6_fixture(connection)
        if version == 7:
            _migrate_schema_v6_to_v7(connection)
        result_id = _insert_result_with_children(connection, _strategy(connection, name=f"legacy-{version}"))
        typed = _source(result_id=int(result_id))
        connection.execute(
            "update optimizer_prepared_inputs set preparation_version = ?, source_digest = ?, prepared_json = ? where result_id = ?",
            ["5", source_digest(typed), build_prepared_input(typed).to_json(), result_id],
        )
        connection.execute("select nextval('performance_v2_result_id_seq')")
    with duckdb.connect(str(source), read_only=True) as connection:
        source_sequence = connection.execute(
            "select last_value from duckdb_sequences() where sequence_name = 'performance_v2_result_id_seq'"
        ).fetchone()[0]
    target = tmp_path / f"target-{version}.duckdb"

    report = compact_performance_v2(source, target, workers=2)

    assert report["source_schema_version"] == version
    assert report["target"]["sha256"] == compact_module._sha256(target)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select last_value from duckdb_sequences() where sequence_name = 'performance_v2_result_id_seq'"
        ).fetchone()[0] == source_sequence


def test_compact_rejects_source_stat_change_during_copy(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.close()
    source = source_dir / "strategy_performance.duckdb"
    original_mtime = source.stat().st_mtime_ns
    changed = False

    def progress(_event: dict[str, object]) -> None:
        nonlocal changed
        if not changed:
            os.utime(source, ns=(original_mtime, original_mtime + 123456789))
            changed = True

    target = tmp_path / "target.duckdb"
    with pytest.raises(ValueError, match="source database changed"):
        compact_performance_v2(source, target, workers=1, progress_callback=progress)
    assert changed
    assert not target.exists()


def test_compact_rejects_alias_unknown_catalog_and_low_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.close()
    source = source_dir / "strategy_performance.duckdb"
    alias = tmp_path / "alias.duckdb"
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match="aliases"):
        compact_performance_v2(source, alias, workers=1)

    def low_space(_path: Path):
        return shutil._ntuple_diskusage(total=1, used=1, free=0)

    monkeypatch.setattr(compact_module.shutil, "disk_usage", low_space)
    with pytest.raises(OSError, match="10 GiB"):
        compact_performance_v2(source, tmp_path / "low-space.duckdb", workers=1)

    monkeypatch.undo()
    with duckdb.connect(str(source)) as connection:
        connection.execute("create view unexpected_view as select 1")
    with pytest.raises(ValueError, match="catalog"):
        compact_performance_v2(source, tmp_path / "view.duckdb", workers=1)

def test_compact_rejects_nonzero_wal_sidecar(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.close()
    source = source_dir / "strategy_performance.duckdb"
    Path(str(source) + ".wal").write_bytes(b"wal")

    with pytest.raises(ValueError, match="nonzero WAL"):
        compact_performance_v2(source, tmp_path / "target.duckdb", workers=1)


def test_compact_rejects_unknown_base_table(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.execute("create table unexpected_table (value integer)")
    connection.close()
    source = source_dir / "strategy_performance.duckdb"

    with duckdb.connect(str(source), read_only=True) as connection:
        with pytest.raises(ValueError, match="unknown catalog objects"):
            compact_module._unknown_catalog_objects(connection)
    with pytest.raises(ValueError, match="catalog"):
        compact_performance_v2(source, tmp_path / "target.duckdb", workers=1)


def test_compact_rejects_planted_stage_wal_before_publish(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.close()
    source = source_dir / "strategy_performance.duckdb"
    target = tmp_path / "target.duckdb"
    planted = False

    def progress(event: dict[str, object]) -> None:
        nonlocal planted
        if planted or event.get("phase") != "verify" or event.get("table") != "equity_quality_metrics":
            return
        stages = tuple(target.parent.glob(f".{target.name}.*.staging.duckdb"))
        assert len(stages) == 1
        Path(str(stages[0]) + ".wal").write_bytes(b"wal")
        planted = True

    with pytest.raises(ValueError, match="nonzero WAL"):
        compact_performance_v2(source, target, workers=1, progress_callback=progress)

    assert planted
    assert not target.exists()


def test_compact_rejects_corrupt_prepared_stream_without_publishing(tmp_path: Path) -> None:
    from tests.test_performance_v2_optimizer import _typed_candidate_database
    from mrs3.performance_v2_optimizer import prepare_current_optimizer_inputs

    source, (result_id,) = _typed_candidate_database(tmp_path / "source")
    prepare_current_optimizer_inputs(str(source), [result_id], workers=1)
    with duckdb.connect(str(source)) as connection:
        connection.execute(
            "update optimizer_prepared_inputs set prepared_json = ? where result_id = ?",
            ["mrs3-zlib-v1:1:bad:not-base64", result_id],
        )
    target = tmp_path / "target.duckdb"

    with pytest.raises(ValueError, match="prepared storage|prepared artifact"):
        compact_performance_v2(source, target, workers=1)

    assert not target.exists()


def test_typed_comparison_preserves_float_signed_zero() -> None:
    assert not compact_module._equal(-0.0, 0.0)
    assert compact_module._equal(float("nan"), float("nan"))


def test_verify_existing_candidate_compares_large_tables_and_publishes_hardlink(
    tmp_path: Path,
) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_connection = _candidate_db(source_dir)
    source_connection.close()
    source = source_dir / "strategy_performance.duckdb"
    candidate = tmp_path / "candidate.duckdb"
    shutil.copyfile(source, candidate)
    source_before = compact_module._sha256(source)
    candidate_before = compact_module._sha256(candidate)
    output = tmp_path / "published.duckdb"
    progress: list[dict[str, object]] = []

    report = compact_module.verify_existing_candidate(
        source, candidate, output, workers=1, spill_parent=tmp_path,
        progress_callback=progress.append,
    )

    assert report["verified_table_counts"]["strategy_actions"] == 3
    assert report["verified_table_counts"]["strategy_equity"] == 4
    assert report["publication"] == "hardlink_alias_not_backup"
    assert os.path.samefile(candidate, output)
    assert compact_module._sha256(source) == source_before
    assert compact_module._sha256(candidate) == candidate_before
    assert {event["table"] for event in progress if event["phase"] == "verify_range"} == {
        "strategy_actions", "strategy_equity",
    }


def test_verify_existing_candidate_mismatch_preserves_candidate_and_skips_publication(
    tmp_path: Path,
) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_connection = _candidate_db(source_dir)
    source_connection.close()
    source = source_dir / "strategy_performance.duckdb"
    candidate = tmp_path / "candidate.duckdb"
    shutil.copyfile(source, candidate)
    with duckdb.connect(str(candidate)) as connection:
        connection.execute("update strategy_equity set equity = 999 where sample_index = 0")
    candidate_before = compact_module._sha256(candidate)
    output = tmp_path / "published.duckdb"

    with pytest.raises(ValueError, match="EXCEPT ALL mismatch in strategy_equity"):
        compact_module.verify_existing_candidate(
            source, candidate, output, workers=1, spill_parent=tmp_path,
        )

    assert compact_module._sha256(candidate) == candidate_before
    assert not output.exists()


def test_verify_existing_candidate_rejects_candidate_only_result_id_range(
    tmp_path: Path,
) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_connection = _candidate_db(source_dir)
    source_connection.close()
    source = source_dir / "strategy_performance.duckdb"
    candidate = tmp_path / "candidate.duckdb"
    shutil.copyfile(source, candidate)
    with duckdb.connect(str(candidate)) as connection:
        strategy_id = connection.execute(
            """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
               order_count, analysis_run_id, candidate_identity, lifecycle_status,
               created_at_utc, updated_at_utc) values ('candidate-only', 'ETHUSDT', 'LONG',
               '1h', 3, 1, 'run', 'candidate-only-999', 'ACTIVE', now(), now())
               returning strategy_id"""
        ).fetchone()[0]
        connection.execute(
            """insert into strategy_results (result_id, strategy_id, report_start_utc,
               report_end_utc, exchange, commission_rate, initial_balance, final_balance,
               total_pnl, total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees,
               total_trades, imported_at_utc)
               values (999, ?, now(), now(), 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 2, 2, now())""",
            [strategy_id],
        )
        connection.execute("update strategy_actions set result_id = 999 where action_index = 2")
    candidate_before = compact_module._sha256(candidate)
    output = tmp_path / "published.duckdb"

    with pytest.raises(ValueError):
        compact_module.verify_existing_candidate(
            source, candidate, output, workers=1, spill_parent=tmp_path,
        )

    assert compact_module._sha256(candidate) == candidate_before
    assert not output.exists()


def test_verify_existing_candidate_fails_closed_on_oversized_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_connection = _candidate_db(source_dir)
    source_connection.close()
    source = source_dir / "strategy_performance.duckdb"
    candidate = tmp_path / "candidate.duckdb"
    shutil.copyfile(source, candidate)
    monkeypatch.setattr(compact_module, "MAX_FAST_VERIFY_RANGE_ROWS", 2)

    with pytest.raises(ValueError, match="exceeds fast verification limit"):
        compact_module.verify_existing_candidate(
            source, candidate, tmp_path / "published.duckdb", workers=1,
            spill_parent=tmp_path,
        )


def test_verify_existing_candidate_smoke_checks_ranges_without_publication(
    tmp_path: Path,
) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_connection = _candidate_db(source_dir)
    source_connection.close()
    source = source_dir / "strategy_performance.duckdb"
    candidate = tmp_path / "candidate.duckdb"
    shutil.copyfile(source, candidate)

    report = compact_module.verify_existing_candidate(
        source, candidate, workers=1, spill_parent=tmp_path, smoke_ranges=1,
    )

    assert report["partial"] is True
    assert report["publication"] is None
    assert report["verified_ranges"] == {"strategy_actions": 1, "strategy_equity": 1}


def test_verify_existing_candidate_refuses_existing_publication_path(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_connection = _candidate_db(source_dir)
    source_connection.close()
    source = source_dir / "strategy_performance.duckdb"
    candidate = tmp_path / "candidate.duckdb"
    shutil.copyfile(source, candidate)
    output = tmp_path / "published.duckdb"
    output.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        compact_module.verify_existing_candidate(
            source, candidate, output, workers=1, spill_parent=tmp_path,
        )

    assert output.read_bytes() == b"keep"
    assert not os.path.samefile(candidate, output)


def test_verify_existing_candidate_does_not_replace_concurrent_output(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_connection = _candidate_db(source_dir)
    source_connection.close()
    source = source_dir / "strategy_performance.duckdb"
    candidate = tmp_path / "candidate.duckdb"
    shutil.copyfile(source, candidate)
    candidate_before = compact_module._sha256(candidate)
    output = tmp_path / "published.duckdb"
    planted = False

    def create_output_before_link(event: dict[str, object]) -> None:
        nonlocal planted
        if not planted and event.get("phase") == "verify" and event.get("table") == "strategy_rejection_sources":
            output.write_bytes(b"created concurrently")
            planted = True

    with pytest.raises(FileExistsError):
        compact_module.verify_existing_candidate(
            source, candidate, output, workers=1, spill_parent=tmp_path,
            progress_callback=create_output_before_link,
        )

    assert planted
    assert output.read_bytes() == b"created concurrently"
    assert compact_module._sha256(candidate) == candidate_before
    assert not os.path.samefile(candidate, output)


def test_compact_uses_sql_fast_verifier_for_action_and_equity_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_connection = _candidate_db(source_dir)
    source_connection.close()
    checked: list[str] = []
    original = compact_module._verify_fast_table

    def record_fast_table(*args, **kwargs):
        checked.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(compact_module, "_verify_fast_table", record_fast_table)
    compact_performance_v2(
        source_dir / "strategy_performance.duckdb", tmp_path / "compact.duckdb",
        workers=1,
    )

    assert checked == ["strategy_actions", "strategy_equity"]


def test_compact_falls_back_to_typed_rows_when_fast_metadata_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_connection = _candidate_db(source_dir)
    source_connection.close()
    events: list[dict[str, object]] = []

    def unsupported(*_args, **_kwargs):
        raise compact_module._FastVerifyUnsupported

    monkeypatch.setattr(compact_module, "_fast_column_signature", unsupported)
    report = compact_performance_v2(
        source_dir / "strategy_performance.duckdb", tmp_path / "compact.duckdb",
        workers=1, progress_callback=events.append,
    )

    assert report["verified_table_counts"]["strategy_actions"] == 3
    assert report["verified_table_counts"]["strategy_equity"] == 4
    assert [event["table"] for event in events if event["phase"] == "verify_fallback"] == [
        "strategy_actions", "strategy_equity",
    ]


def _candidate_verifier_cli():
    import importlib.util

    script = Path(__file__).parents[1] / "scripts" / "verify_performance_v2_candidate.py"
    spec = importlib.util.spec_from_file_location("verify_performance_v2_candidate_cli", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verify_candidate_cli_workers_override_config(tmp_path: Path, monkeypatch, capsys) -> None:
    from types import SimpleNamespace

    cli = _candidate_verifier_cli()
    monkeypatch.setattr(cli, "load_duckdb_import_settings", lambda _path: SimpleNamespace(workers=30))
    called: dict[str, object] = {}

    def verify(*_args, **kwargs):
        called.update(kwargs)
        return {"partial": False}

    monkeypatch.setattr(cli, "verify_existing_candidate", verify)

    exit_code = cli.main([
        "--source", str(tmp_path / "source.duckdb"),
        "--candidate", str(tmp_path / "candidate.duckdb"),
        "--output", str(tmp_path / "output.duckdb"),
        "--config", "config.local.json",
        "--spill-parent", str(tmp_path),
        "--workers", "16",
    ])

    assert exit_code == 0
    assert called["workers"] == 16
    assert '"partial": false' in capsys.readouterr().out


def test_verify_candidate_cli_uses_config_workers_without_override(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    from types import SimpleNamespace

    cli = _candidate_verifier_cli()
    monkeypatch.setattr(cli, "load_duckdb_import_settings", lambda _path: SimpleNamespace(workers=30))
    called: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "verify_existing_candidate",
        lambda *_args, **kwargs: (called.update(kwargs) or {"partial": False}),
    )

    exit_code = cli.main([
        "--source", str(tmp_path / "source.duckdb"),
        "--candidate", str(tmp_path / "candidate.duckdb"),
        "--output", str(tmp_path / "output.duckdb"),
        "--config", "config.local.json",
        "--spill-parent", str(tmp_path),
    ])

    assert exit_code == 0
    assert called["workers"] == 30
    assert '"partial": false' in capsys.readouterr().out


def test_verify_candidate_cli_rejects_nonpositive_worker_override(
    tmp_path: Path, capsys,
) -> None:
    cli = _candidate_verifier_cli()

    with pytest.raises(SystemExit) as error:
        cli.main([
            "--source", str(tmp_path / "source.duckdb"),
            "--candidate", str(tmp_path / "candidate.duckdb"),
            "--output", str(tmp_path / "output.duckdb"),
            "--config", "config.local.json",
            "--spill-parent", str(tmp_path),
            "--workers", "0",
        ])

    assert error.value.code == 2
    assert "workers must be a positive integer" in capsys.readouterr().err
