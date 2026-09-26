from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import duckdb
import pytest

from mrs3.performance_v2_store import initialize_performance_v2


def _load_benchmark():
    path = Path(__file__).parents[1] / "scripts" / "benchmark_performance_v2_equity_selection.py"
    spec = importlib.util.spec_from_file_location("benchmark_performance_v2_equity_selection", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frozen_fixture(tmp_path: Path) -> Path:
    database = tmp_path / "frozen-v6-copy.duckdb"
    connection = duckdb.connect(str(database))
    initialize_performance_v2(connection)
    start = connection.execute("select timestamptz '2026-01-01 00:00:00+00'").fetchone()[0]
    end = connection.execute("select timestamptz '2026-01-05 00:00:00+00'").fetchone()[0]
    strategy_id = connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
               order_count, analysis_run_id, candidate_identity, lifecycle_status,
               created_at_utc, updated_at_utc)
           values ('alpha', 'BTCUSDT', 'LONG', '1h', 3, 1, 'run', 'candidate', 'ACTIVE', ?, ?)
           returning strategy_id""",
        [start, start],
    ).fetchone()[0]
    result_id = connection.execute(
        """insert into strategy_results (strategy_id, report_start_utc, report_end_utc,
               exchange, commission_rate, initial_balance, final_balance, total_pnl,
               total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees, total_trades,
               imported_at_utc)
           values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 0, 0, 0, 2, ?)
           returning result_id""",
        [strategy_id, start, end, start],
    ).fetchone()[0]
    connection.execute(
        "update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id],
    )
    connection.execute(
        "insert into analysis_plateaus values ('run', 'P1', 12, 34)"
    )
    connection.execute(
        "insert into strategy_orders values (?, 1, 7, .995, 125, 1, 'run', 'P1', 8)", [strategy_id],
    )
    connection.executemany(
        """insert into strategy_actions (result_id, action_index, timestamp_utc, symbol,
               order_id, action, size, post_size, post_side, pnl, fee, balance, raw_action_json)
           values (?, ?, ?, 'BTCUSDT', 1, ?, 1, ?, ?, ?, 0, ?, null)""",
        [
            (result_id, 0, start, "opened", 1, "long", 0, 100),
            (result_id, 1, end, "closed", 0, "", 10, 110),
        ],
    )
    connection.executemany(
        "insert into strategy_equity values (?, ?, ?, ?, ?)",
        [(result_id, 0, start, 100, 100), (result_id, 1, end, 110, 110)],
    )
    connection.execute("checkpoint")
    connection.close()
    return database


def test_benchmark_requires_an_explicit_copy_argument(monkeypatch) -> None:
    benchmark = _load_benchmark()
    monkeypatch.setattr(sys, "argv", ["benchmark_performance_v2_equity_selection.py"])

    with pytest.raises(SystemExit) as raised:
        benchmark.main()

    assert raised.value.code == 2


def test_benchmark_rejects_default_database_before_opening_it(tmp_path: Path, monkeypatch) -> None:
    benchmark = _load_benchmark()
    default_database = tmp_path / "default" / "strategy_performance.duckdb"
    default_database.parent.mkdir()
    default_database.touch()
    monkeypatch.setattr(benchmark, "DEFAULT_DATABASE", default_database)
    monkeypatch.setattr(benchmark.duckdb, "connect", lambda *_args, **_kwargs: pytest.fail("opened default DB"))

    with pytest.raises(ValueError, match="frozen copy"):
        benchmark._validate_database_copy(default_database)


def test_benchmark_rejects_configured_database_before_opening_it(tmp_path: Path, monkeypatch) -> None:
    benchmark = _load_benchmark()
    configured_database = tmp_path / "configured" / "strategy_performance.duckdb"
    configured_database.parent.mkdir()
    configured_database.touch()
    monkeypatch.setattr(benchmark, "DEFAULT_DATABASE", tmp_path / "other-default.duckdb")
    monkeypatch.setattr(benchmark, "_configured_database_path", lambda: configured_database)
    monkeypatch.setattr(benchmark.duckdb, "connect", lambda *_args, **_kwargs: pytest.fail("opened configured DB"))

    with pytest.raises(ValueError, match="configured"):
        benchmark._validate_database_copy(configured_database)


def test_benchmark_rejects_hardlink_alias_of_configured_database(tmp_path: Path, monkeypatch) -> None:
    benchmark = _load_benchmark()
    configured_database = _frozen_fixture(tmp_path)
    alias = tmp_path / "frozen-copy-alias.duckdb"
    alias.hardlink_to(configured_database)
    monkeypatch.setattr(benchmark, "DEFAULT_DATABASE", tmp_path / "unrelated-default.duckdb")
    monkeypatch.setattr(benchmark, "_configured_database_path", lambda: configured_database)
    monkeypatch.setattr(benchmark.duckdb, "connect", lambda *_args, **_kwargs: pytest.fail("opened configured DB alias"))

    with pytest.raises(ValueError, match="same file"):
        benchmark._validate_database_copy(alias)


def test_configured_database_path_uses_current_panel_config(tmp_path: Path, monkeypatch) -> None:
    benchmark = _load_benchmark()
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.performance.json").write_text(
        json.dumps({"unified_performance_v2": {"database_root": "configured-performance"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(benchmark, "ROOT", project)

    assert benchmark._configured_database_path() == (
        project / "configured-performance" / "strategy_performance.duckdb"
    ).resolve()


@pytest.mark.parametrize("wal_suffix", (".wal", "-wal"))
def test_benchmark_rejects_wal_and_in_repository_paths_before_opening(
    tmp_path: Path, monkeypatch, wal_suffix: str,
) -> None:
    benchmark = _load_benchmark()
    source = _frozen_fixture(tmp_path)
    source.with_name(source.name + wal_suffix).touch()
    monkeypatch.setattr(benchmark.duckdb, "connect", lambda *_args, **_kwargs: pytest.fail("opened WAL DB"))

    with pytest.raises(ValueError, match="WAL"):
        benchmark._validate_database_copy(source)

    repository = tmp_path / "repo"
    inside = repository / "frozen-copy.duckdb"
    inside.parent.mkdir()
    inside.touch()
    monkeypatch.setattr(benchmark, "ROOT", repository)
    with pytest.raises(ValueError, match="frozen copy"):
        benchmark._validate_database_copy(inside)


def test_benchmark_rejects_non_v6_without_changing_source(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    source = _frozen_fixture(tmp_path)
    with duckdb.connect(str(source)) as connection:
        connection.execute("update schema_info set value = '5' where key = 'schema_version'")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    with pytest.raises(ValueError):
        benchmark._validate_database_copy(source)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_sql_counter_observes_opened_connections_and_cache_writes(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    database = tmp_path / "sql-counter.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create table equity_quality_metrics (result_id integer)")

    with benchmark._count_sql() as counter:
        with benchmark.duckdb.connect(str(database)) as connection:
            connection.execute("insert into equity_quality_metrics values (1)")
            assert connection.execute("select * from equity_quality_metrics").fetchall() == [(1,)]

    counts = counter.snapshot()
    assert counts["connections_opened"] == 1
    assert counts["cache_writes"] == 1
    assert counts["equity_quality_metrics_reads"] == 1
    assert counts["equity_quality_metrics_rows_returned"] == 1


def test_sql_counter_rejects_overlapping_global_patches() -> None:
    benchmark = _load_benchmark()
    with benchmark._count_sql():
        with pytest.raises(RuntimeError, match="overlap"):
            with benchmark._count_sql():
                pass


def test_benchmark_runs_all_warm_consumer_modes_without_writing_source(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    source = _frozen_fixture(tmp_path)
    before = (source.stat().st_size, source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())
    output = tmp_path / "evidence.jsonl"

    records = benchmark.run_benchmark(
        database_copy=source,
        symbol="BTCUSDT",
        side="LONG",
        top_n=1,
        output_jsonl=output,
        consumer_modes=benchmark.CONSUMER_MODES,
    )

    after = (source.stat().st_size, source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert before == after
    assert all(record["source_copy_stat_unchanged"] is True for record in lines)
    assert {record["consumer_mode"] for record in records} == {"legacy", "filter_only", "rank_only", "both"}
    assert len(lines) == 4
    assert all(record["warmup_runs"] == 1 and len(record["measured_runs"]) == 3 for record in lines)
    assert all(record["cache_state"] == "all_warm" for record in lines)
    assert all(record["decision_rows"] for record in lines)
    assert all(run["sql"]["read_queries"] > 0 for record in lines for run in record["measured_runs"])
    assert all(run["sql"]["connections_opened"] > 0 for record in lines for run in record["measured_runs"])
    assert all(run["sql"]["cache_writes"] == 0 for record in lines for run in record["measured_runs"])
    assert all(record["request_payload"]["stages"][-1]["id"] == "rank_robust_top_n" for record in lines)
    for record in lines:
        signatures = [run["decision_signature_sha256"] for run in record["measured_runs"]]
        assert len(set(signatures)) == 1
        assert record["decision_signature_sha256"] == signatures[0]
        assert all(
            run["row_counter_coverage"] == "fetchone/fetchall/fetchmany/iterator only; excludes dataframe, Arrow, and Polars fetch APIs"
            for run in record["measured_runs"]
        )
        assert all("process-wide" in run["sql_counter_scope"] for run in record["measured_runs"])
        assert record["baseline_comparison"].startswith("not_run:")
        assert record["one_replace"].startswith("not_run:")


def test_benchmark_recalc_profile_uses_fresh_copies_and_counts_writes(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    source = _frozen_fixture(tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "recalc.jsonl"

    records = benchmark.run_recalculation_profiles(
        database_copy=source,
        symbol="BTCUSDT",
        side="LONG",
        output_jsonl=output,
        workers=(1,),
        repeats=3,
    )

    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert {record["cache_state"] for record in lines} == {"cold", "old_warm_new_cold"}
    assert all(record["warmup_runs"] == 1 and len(record["measured_runs"]) == 3 for record in lines)
    assert all(run["sql"]["write_queries"] > 0 for record in lines for run in record["measured_runs"])
    assert all(run["sql"]["cache_writes"] > 0 for record in lines for run in record["measured_runs"])
    for record in lines:
        setup = record["cache_setup_runs"]
        assert len(setup) == 4
        if record["cache_state"] == "cold":
            assert all(state["window_metrics_rows"] == 0 and state["equity_quality_metrics_rows"] == 0 for state in setup)
        else:
            assert all(state["window_metrics_rows"] > 0 and state["equity_quality_metrics_rows"] == 0 for state in setup)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert all(record["source_copy_stat_unchanged"] is True for record in lines)
    assert all(record["baseline_comparison"].startswith("not_run:") for record in lines)
    assert all(record["one_replace"].startswith("not_run:") for record in lines)


def test_preview_detects_a_source_copy_mutation(tmp_path: Path, monkeypatch) -> None:
    benchmark = _load_benchmark()
    source = _frozen_fixture(tmp_path)
    create_controller = benchmark._create_controller

    def mutate_source(*args, **kwargs):
        controller = create_controller(*args, **kwargs)
        source.touch()
        return controller

    monkeypatch.setattr(benchmark, "_create_controller", mutate_source)
    with pytest.raises(RuntimeError, match="source frozen copy changed"):
        benchmark.run_benchmark(
            database_copy=source,
            symbol="BTCUSDT",
            side="LONG",
            top_n=1,
            output_jsonl=tmp_path / "mutated.jsonl",
            consumer_modes=("legacy",),
        )
