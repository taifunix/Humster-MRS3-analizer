from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import duckdb
import pytest

from mrs3.performance_v2_store import initialize_performance_v2, require_performance_v2_readable


def _load_creator():
    path = Path(__file__).parents[1] / "scripts" / "create_performance_v2_equity_benchmark_slice.py"
    spec = importlib.util.spec_from_file_location("create_performance_v2_equity_benchmark_slice", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_v5(path: Path, *, v6: bool = False) -> Path:
    connection = duckdb.connect(str(path))
    initialize_performance_v2(connection)
    start = connection.execute("select timestamptz '2026-01-01 00:00:00+00'").fetchone()[0]
    end = connection.execute("select timestamptz '2026-01-05 00:00:00+00'").fetchone()[0]
    for name, status, symbol, side in (
        ("alpha", "ACTIVE", "BTCUSDT", "LONG"),
        ("bravo", "ACTIVE", "BTCUSDT", "LONG"),
        ("charlie", "ACTIVE", "BTCUSDT", "LONG"),
        ("inactive", "DISCARDED", "BTCUSDT", "LONG"),
        ("other-pair", "ACTIVE", "ETHUSDT", "LONG"),
    ):
        strategy_id = connection.execute(
            """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
                   order_count, analysis_run_id, candidate_identity, lifecycle_status,
                   created_at_utc, updated_at_utc)
               values (?, ?, ?, '1h', 3, 1, ?, ?, ?, ?, ?) returning strategy_id""",
            [name, symbol, side, f"run-{name}", f"candidate-{name}", status, start, start],
        ).fetchone()[0]
        result_id = connection.execute(
            """insert into strategy_results (strategy_id, report_start_utc, report_end_utc,
                   exchange, commission_rate, initial_balance, final_balance, total_pnl,
                   total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees, total_trades,
                   imported_at_utc)
               values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, -2, -2, 0, 2, ?)
               returning result_id""",
            [strategy_id, start, end, start],
        ).fetchone()[0]
        connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
        if v6:
            connection.execute(
                "insert into equity_quality_metrics values (?, 'test-source', 'equity_quality_v1', '{}', 'digest', ?)",
                [result_id, start],
            )
        connection.execute("insert into analysis_plateaus values (?, 'P1', 12, 34)", [f"run-{name}"])
        connection.execute("insert into analysis_plateaus values (?, 'UNUSED', 1, 0)", [f"run-{name}"])
        connection.execute(
            "insert into strategy_orders values (?, 1, 7, .995, 125, 1, ?, 'P1', 8)",
            [strategy_id, f"run-{name}"],
        )
        connection.execute(
            """insert into strategy_actions (result_id, action_index, timestamp_utc, symbol,
                   order_id, action, size, post_size, post_side, pnl, fee, balance)
               values (?, 0, ?, ?, 1, 'opened', 1, 1, 'long', 0, 0, 100)""",
            [result_id, start, symbol],
        )
        connection.execute("insert into strategy_equity values (?, 0, ?, 100, 100)", [result_id, start])
        connection.execute(
            """insert into window_metrics (result_id, requested_start_utc, requested_end_utc,
                   metrics_version, availability_status, calculated_at_utc)
               values (?, ?, ?, 'test-v1', 'AVAILABLE', ?)""",
            [result_id, start, end, start],
        )
        connection.execute(
            "insert into strategy_tags values (?, 'RETEST', 'test', ?, ?)",
            [strategy_id, name, start],
        )
    # A real v5 catalog differs from v6 only by the additive equity facts table.
    if not v6:
        connection.execute("drop table equity_quality_metrics")
        connection.execute("update schema_info set value = '5' where key = 'schema_version'")
    assert require_performance_v2_readable(connection) == (6 if v6 else 5)
    connection.execute("checkpoint")
    connection.close()
    return path


def test_creator_copies_deterministic_current_active_slice_read_only(tmp_path: Path) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "slice-v6.duckdb"
    before = (source.stat().st_size, source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())

    manifest = creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=2)

    after = (source.stat().st_size, source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())
    assert before == after
    assert manifest["selected_strategy_ids"] == sorted(_deterministic_ids([1, 2, 3], 2))
    assert manifest["source_schema_version"] == 5
    assert manifest["v6_only_columns"] == {}
    assert manifest["selected_count"] == 2
    assert manifest["analysis_plateaus_scope"] == "order_referenced"
    assert manifest["table_counts"] == {
        "strategies": 2,
        "strategy_results": 2,
        "analysis_plateaus": 2,
        "strategy_orders": 2,
        "strategy_actions": 2,
        "strategy_equity": 2,
        "window_metrics": 2,
        "equity_quality_metrics": 0,
        "strategy_tags": 2,
    }
    assert manifest["selected_ids_sha256"] == hashlib.sha256(
        ("\n".join(map(str, sorted(manifest["selected_strategy_ids"]))) + "\n").encode("ascii")
    ).hexdigest()
    assert manifest["source_scoped_counts"] == {
        "strategy_actions": 2,
        "strategy_equity": 2,
        "window_metrics": 2,
        "strategy_orders": 2,
        "strategy_tags": 2,
        "analysis_plateaus": 2,
        "equity_quality_metrics": 0,
    }
    with duckdb.connect(str(output), read_only=True) as connection:
        assert require_performance_v2_readable(connection) == 6
        assert connection.execute("select strategy_id from strategies order by strategy_id").fetchall() == [
            (strategy_id,) for strategy_id in manifest["selected_strategy_ids"]
        ]
        with duckdb.connect(str(source), read_only=True) as source_connection:
            selected_pairs = source_connection.execute(
                "select strategy_id, current_result_id from strategies where strategy_id in (?, ?) order by strategy_id",
                manifest["selected_strategy_ids"],
            ).fetchall()
        assert connection.execute(
            "select strategy_id, current_result_id from strategies order by strategy_id"
        ).fetchall() == selected_pairs
        assert connection.execute("select count(*) from analysis_plateaus where plateau_id = 'UNUSED'").fetchone() == (0,)
        assert connection.execute("select count(*) from optimizer_prepared_inputs").fetchone() == (0,)
        assert connection.execute("select count(*) from import_runs").fetchone() == (0,)
        for table in manifest["omitted_tables"]:
            assert connection.execute(f"select count(*) from {table}").fetchone() == (0,)
        actual_tables = {
            row[0] for row in connection.execute(
                "select table_name from information_schema.tables where table_schema = 'main'"
            ).fetchall()
        }
        declared_tables = (
            set(manifest["table_counts"])
            | set(manifest["omitted_tables"])
            | {"schema_info"}
        )
        assert declared_tables == actual_tables


def _deterministic_ids(strategy_ids: list[int], limit: int) -> list[int]:
    return sorted(strategy_ids, key=lambda strategy_id: (hashlib.sha256(str(strategy_id).encode("ascii")).digest(), strategy_id))[:limit]


def test_creator_refuses_existing_output_without_changing_it(tmp_path: Path) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "existing.duckdb"
    output.write_bytes(b"keep me")

    with pytest.raises(FileExistsError):
        creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=2)

    assert output.read_bytes() == b"keep me"


def test_creator_reads_explicit_in_repository_source_without_writing_it(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(creator, "ROOT", repository)
    source = _source_v5(repository / "explicit-live-copy.duckdb")
    output = tmp_path / "outside-slice.duckdb"
    before = (source.stat().st_size, source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())

    manifest = creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=1)

    assert manifest["source_stat_unchanged"] is True
    assert (source.stat().st_size, source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest()) == before
    assert output.exists()


@pytest.mark.parametrize("wal_suffix", (".wal", "-wal"))
def test_creator_rejects_source_with_wal_sidecar_before_opening(tmp_path: Path, monkeypatch, wal_suffix: str) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    source.with_name(source.name + wal_suffix).touch()
    monkeypatch.setattr(creator.duckdb, "connect", lambda *_args, **_kwargs: pytest.fail("opened WAL source"))

    with pytest.raises(ValueError, match="WAL"):
        creator.create_benchmark_slice(source, tmp_path / "slice.duckdb", symbol="BTCUSDT", side="LONG", limit=1)


def test_creator_rejects_output_inside_repository(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(creator, "ROOT", repository)
    source = _source_v5(tmp_path / "source-v5.duckdb")

    with pytest.raises(ValueError, match="outside the repository"):
        creator.create_benchmark_slice(source, repository / "slice.duckdb", symbol="BTCUSDT", side="LONG", limit=1)


def test_creator_rejects_hardlink_output_alias_of_source(tmp_path: Path) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    alias = tmp_path / "alias.duckdb"
    alias.hardlink_to(source)

    with pytest.raises(ValueError, match="aliases the source"):
        creator.create_benchmark_slice(source, source, symbol="BTCUSDT", side="LONG", limit=1)
    with pytest.raises(ValueError, match="aliases the source"):
        creator.create_benchmark_slice(source, alias, symbol="BTCUSDT", side="LONG", limit=1)


def test_creator_publish_does_not_replace_output_created_during_extraction(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "slice.duckdb"
    real_link = creator.os.link

    def create_racing_output(source_file, output_file):
        Path(output_file).write_bytes(b"created by another process")
        return real_link(source_file, output_file)

    monkeypatch.setattr(creator.os, "link", create_racing_output)
    with pytest.raises(FileExistsError):
        creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=1)

    assert output.read_bytes() == b"created by another process"
    assert list(tmp_path.glob(".slice.duckdb.*.tmp*")) == []


def test_creator_cleans_unpublished_temp_file_after_mid_copy_failure(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "slice.duckdb"
    monkeypatch.setattr(creator, "_copy_query", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("copy failed")))

    with pytest.raises(RuntimeError, match="copy failed"):
        creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=1)

    assert not output.exists()
    assert list(tmp_path.glob(".slice.duckdb.*.tmp*")) == []


def test_creator_rejects_limit_above_approved_sample_size(tmp_path: Path) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")

    with pytest.raises(ValueError, match="1 and 512"):
        creator.create_benchmark_slice(source, tmp_path / "slice.duckdb", symbol="BTCUSDT", side="LONG", limit=513)


def test_creator_rejects_mismatched_current_result_pair_before_publish(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "slice.duckdb"
    copy_query = creator._copy_query

    def corrupt_current_result(target, table, query, parameters):
        copied = copy_query(target, table, query, parameters)
        if table == "strategies":
            target.execute("update strategies set current_result_id = -1")
        return copied

    monkeypatch.setattr(creator, "_copy_query", corrupt_current_result)
    with pytest.raises(RuntimeError, match="current-result pairs"):
        creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=1)

    assert not output.exists()


def test_creator_rejects_mismatched_source_scoped_equity_count(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "slice.duckdb"
    copy_query = creator._copy_query

    def omit_equity_rows(target, table, query, parameters):
        if table == "strategy_equity":
            return copy_query(target, table, "select * from frozen_source.main.strategy_equity where false", [])
        return copy_query(target, table, query, parameters)

    monkeypatch.setattr(creator, "_copy_query", omit_equity_rows)
    with pytest.raises(RuntimeError, match="source-scoped count"):
        creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=1)

    assert not output.exists()


def test_creator_counts_and_copies_v6_equity_facts_from_selected_results(tmp_path: Path) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v6.duckdb", v6=True)
    output = tmp_path / "slice-v6.duckdb"

    manifest = creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=2)

    assert manifest["source_scoped_counts"]["equity_quality_metrics"] == 2
    assert manifest["table_counts"]["equity_quality_metrics"] == 2
    with duckdb.connect(str(output), read_only=True) as connection:
        assert connection.execute("select count(*) from equity_quality_metrics").fetchone() == (2,)


def test_creator_detects_missing_strategy_orders_against_source_scope(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "slice.duckdb"
    copy_query = creator._copy_query

    def omit_order_rows(target, table, query, parameters):
        if table == "strategy_orders":
            return copy_query(target, table, "select * from frozen_source.main.strategy_orders where false", [])
        return copy_query(target, table, query, parameters)

    monkeypatch.setattr(creator, "_copy_query", omit_order_rows)
    with pytest.raises(RuntimeError, match="source-scoped count mismatch for strategy_orders"):
        creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=1)

    assert not output.exists()


def test_creator_rejects_temp_wal_sidecar_before_publish(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "slice.duckdb"
    real_require = creator.require_performance_v2_readable
    calls = 0

    def create_sidecar_after_target_validation(connection):
        nonlocal calls
        calls += 1
        version = real_require(connection)
        if calls == 2:
            sidecar = tmp_path / f".{output.name}.fixture.tmp.wal"
            sidecar.touch()
        return version

    monkeypatch.setattr(creator.uuid, "uuid4", lambda: type("Token", (), {"hex": "fixture"})())
    monkeypatch.setattr(creator, "require_performance_v2_readable", create_sidecar_after_target_validation)

    with pytest.raises(RuntimeError, match="temporary benchmark database has a WAL sidecar"):
        creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=1)

    assert not output.exists()
    assert list(tmp_path.glob(".slice.duckdb.*.tmp*")) == []


def test_creator_rejects_missing_source_column_in_copied_table(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "slice.duckdb"
    with duckdb.connect(str(source)) as connection:
        connection.execute("drop index strategy_actions_result_timestamp_idx")
        connection.execute("alter table strategy_actions drop column price")
    real_require = creator.require_performance_v2_readable
    calls = 0

    def allow_malformed_fixture_source(connection):
        nonlocal calls
        calls += 1
        return 5 if calls == 1 else real_require(connection)

    monkeypatch.setattr(creator, "require_performance_v2_readable", allow_malformed_fixture_source)
    with pytest.raises(RuntimeError, match="column schema mismatch for strategy_actions"):
        creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=1)

    assert not output.exists()


def test_creator_cleans_exact_spill_directory_before_publish(tmp_path: Path, monkeypatch) -> None:
    creator = _load_creator()
    source = _source_v5(tmp_path / "source-v5.duckdb")
    output = tmp_path / "slice.duckdb"
    spill_directory = tmp_path / ".slice.duckdb.fixture.spill"
    copy_query = creator._copy_query
    monkeypatch.setattr(creator.uuid, "uuid4", lambda: type("Token", (), {"hex": "fixture"})())

    def make_spill_file(target, table, query, parameters):
        spill_directory.mkdir(exist_ok=True)
        (spill_directory / "duckdb-spill.bin").write_bytes(b"scratch")
        return copy_query(target, table, query, parameters)

    monkeypatch.setattr(creator, "_copy_query", make_spill_file)
    creator.create_benchmark_slice(source, output, symbol="BTCUSDT", side="LONG", limit=1)

    assert output.exists()
    assert not spill_directory.exists()
