from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import shutil

import duckdb
import pytest
import mrs3.performance_v2_store as store

from mrs3.performance_v2_optimizer import (
    OptimizerAction,
    OptimizerEquityPoint,
    OptimizerSourceInput,
    OptimizerIntegrityError,
    build_prepared_input,
    canonical_json,
    decode_prepared_input,
    prepare_current_optimizer_inputs,
    prepared_availability,
    read_prepared_optimizer_inputs,
    source_digest,
)


def _source(**overrides) -> OptimizerSourceInput:
    values = {
        "source_document_version": "performance-v2",
        "result_id": 17,
        "strategy_id": 11,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "revision_timestamp_utc": datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        "report_start_utc": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "report_end_utc": datetime(2026, 1, 15, tzinfo=timezone.utc),
        "effective_start_utc": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "effective_end_utc": datetime(2026, 1, 15, tzinfo=timezone.utc),
        "initial_balance": Decimal("1000"),
        "sizing_use_upnl": True,
        "sizing_use_frozen_balance": True,
        "sizing_use_fix": False,
        "sizing_balance_percentage_long": Decimal("100"),
        "sizing_risk_long": Decimal("1"),
        "sizing_max_balance": Decimal("0"),
        "actions": (
            OptimizerAction(
                action_index=0,
                timestamp_utc=datetime(2026, 1, 2, tzinfo=timezone.utc),
                symbol="BTCUSDT",
                action="open",
                size=Decimal("1"),
                price=Decimal("100.00"),
                cost=Decimal("100"),
                post_size=Decimal("1"),
                balance=Decimal("1000"),
                pnl=Decimal("0"),
                fee=Decimal("1"),
            ),
            OptimizerAction(
                action_index=1,
                timestamp_utc=datetime(2026, 1, 3, tzinfo=timezone.utc),
                symbol="BTCUSDT",
                action="close",
                size=Decimal("1"),
                price=Decimal("101"),
                cost=Decimal("101"),
                post_size=Decimal("0"),
                balance=Decimal("1010"),
                pnl=Decimal("10"),
                fee=Decimal("1"),
            ),
        ),
        "equity": (
            OptimizerEquityPoint(0, datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("1000")),
            OptimizerEquityPoint(1, datetime(2026, 1, 15, tzinfo=timezone.utc), Decimal("1010")),
        ),
    }
    values.update(overrides)
    return OptimizerSourceInput(**values)


def test_decimal_and_timestamp_canonicalization_is_exact() -> None:
    document = _source(initial_balance=Decimal("-0.000000000000"), revision_timestamp_utc="2026-09-17T14:00:00+02:00").to_document()
    assert document["initial_balance"] == "0.000000000000"
    assert document["revision_timestamp_utc"] == "2026-09-17T12:00:00.000000Z"
    assert '"e-' not in canonical_json(document).lower()
    assert '"e+' not in canonical_json(document).lower()


def test_key_order_does_not_change_digest_and_every_field_is_covered() -> None:
    first = _source()
    second = _source(actions=tuple(reversed(first.actions)), equity=tuple(reversed(first.equity)))
    # The builder's order is timestamp/index order, not caller insertion order.
    assert source_digest(first) == source_digest(second)
    baseline = source_digest(first)
    for field in first.to_document():
        changed = dict(first.to_document())
        if isinstance(changed[field], list):
            changed[field] = list(changed[field]) + [{"sentinel": "1"}]
        elif isinstance(changed[field], str):
            changed[field] = changed[field] + "x"
        else:
            changed[field] = 999
        assert source_digest(changed) != baseline


def test_typed_decimal_over_scale_is_integrity_error() -> None:
    with pytest.raises(OptimizerIntegrityError):
        _source(initial_balance=Decimal("1.0000000000001"))


def test_prepared_round_trip_and_identity_validation() -> None:
    source = _source()
    prepared = build_prepared_input(source)
    encoded = prepared.to_json()
    decoded = decode_prepared_input(encoded, result_id=source.result_id, digest=source_digest(source))
    assert decoded.to_json() == encoded
    with pytest.raises(OptimizerIntegrityError):
        decode_prepared_input(encoded, result_id=source.result_id + 1, digest=source_digest(source))


def test_availability_reasons_are_stable() -> None:
    assert prepared_availability(_source(sizing_use_upnl=None)).reason == "MISSING_TYPED_FACTS"
    assert prepared_availability(_source(sizing_use_fix=True)).reason == "UNSUPPORTED_SIZING"


def test_prepared_weighted_input_bypasses_raw_cycle_reconstruction(monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3.portfolio import input as portfolio_input

    source = _source()
    prepared = build_prepared_input(source)
    row = {
        "symbol": source.symbol, "side": source.side, "strategy_id": source.strategy_id,
        "result_id": source.result_id, "report_start_utc": source.report_start_utc,
        "report_end_utc": source.report_end_utc, "effective_start_utc": source.effective_start_utc,
        "effective_end_utc": source.effective_end_utc, "initial_balance": source.initial_balance,
        "actions": source.to_document()["actions"], "equity": source.to_document()["equity"],
        "_prepared_optimizer_input": prepared,
    }
    monkeypatch.setattr(portfolio_input, "_cycle_records", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("raw reconstruction")))
    result = portfolio_input.prepare_weighted_input((row,), minimum_common_days=1)
    assert result.cycles["BTCUSDT:LONG:11:17"]


def test_prepared_size_limit_uses_exact_stored_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    import mrs3.performance_v2_optimizer as optimizer

    source = _source()
    monkeypatch.setattr(optimizer, "PREPARED_MAX_BYTES", len(build_prepared_input(source).to_json().encode("utf-8")))
    availability, prepared = optimizer.prepare_optimizer_input(source)
    assert availability.available and prepared is not None
    monkeypatch.setattr(optimizer, "PREPARED_MAX_BYTES", len(prepared.to_json().encode("utf-8")) - 1)
    availability, prepared = optimizer.prepare_optimizer_input(source)
    assert availability.reason == "PREPARED_TOO_LARGE" and prepared is None


def test_lazy_current_preparation_reloads_typed_rows_and_reads_strict_artifact(tmp_path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    connection = _candidate_db(tmp_path)
    result_id = int(connection.execute("select current_result_id from strategies").fetchone()[0])
    connection.execute(
        """update strategy_results set sizing_use_upnl = true,
           sizing_use_frozen_balance = true, sizing_use_fix = false,
           sizing_balance_percentage_long = 100,
           sizing_risk_long = 1, sizing_max_balance = 0 where result_id = ?""",
        [result_id],
    )
    connection.execute("update strategy_actions set price = 10, cost = 10 where result_id = ?", [result_id])
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()

    built = prepare_current_optimizer_inputs(str(database), [result_id])
    assert built[0].availability.available
    readback = read_prepared_optimizer_inputs(str(database), [result_id])
    assert readback[0].prepared is not None
    assert readback[0].prepared.source_digest == source_digest(built[0].source)


def test_empty_result_id_scope_does_not_prepare_or_write(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    before = database.read_bytes()

    assert prepare_current_optimizer_inputs(str(database), (), workers=4) == ()
    assert database.read_bytes() == before
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("select count(*) from optimizer_prepared_inputs").fetchone() == (0,)


def _typed_candidate_database(tmp_path: Path, *, two_results: bool = False) -> tuple[Path, tuple[int, ...]]:
    from tests.test_performance_v2_selection import _candidate_db

    tmp_path.mkdir(parents=True, exist_ok=True)
    connection = _candidate_db(tmp_path)
    first_result = int(connection.execute("select current_result_id from strategies").fetchone()[0])
    connection.execute(
        """update strategy_results set sizing_use_upnl = true,
           sizing_use_frozen_balance = true, sizing_use_fix = false,
           sizing_balance_percentage_long = 100, sizing_risk_long = 1,
           sizing_max_balance = 0 where result_id = ?""",
        [first_result],
    )
    connection.execute("update strategy_actions set price = 10, cost = 10 where result_id = ?", [first_result])
    if not two_results:
        database = tmp_path / "strategy_performance.duckdb"
        connection.close()
        return database, (first_result,)

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    strategy_id = int(connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
           order_count, analysis_run_id, candidate_identity, lifecycle_status,
           created_at_utc, updated_at_utc) values ('beta', 'ETHUSDT', 'LONG', '1h',
           1, 1, 'run-beta', 'candidate-beta', 'ACTIVE', ?, ?) returning strategy_id""",
        [start, start],
    ).fetchone()[0])
    connection.execute(
        """insert into strategy_orders
           (strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
            analysis_run_id, plateau_id, base_point_trades)
           values (?, 1, 7, .995, 125, 1, 'run', 'P1', 8)""",
        [strategy_id],
    )
    second_result = int(connection.execute(
        """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
           commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
           max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc,
           sizing_use_upnl, sizing_use_frozen_balance, sizing_use_fix,
           sizing_balance_percentage_long, sizing_risk_long, sizing_max_balance)
           values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 2, 2, ?,
                   true, true, false, 100, 1, 0) returning result_id""",
        [strategy_id, start, datetime(2026, 1, 31, tzinfo=timezone.utc), start],
    ).fetchone()[0])
    connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [second_result, strategy_id])
    actions = connection.execute(
        """select action_index, timestamp_utc, symbol, order_id, action, size, post_size,
                  post_side, pnl, fee, balance, price, cost, raw_action_json
             from strategy_actions where result_id = ? order by action_index""",
        [first_result],
    ).fetchall()
    connection.executemany(
        """insert into strategy_actions
           (result_id, action_index, timestamp_utc, symbol, order_id, action, size, post_size,
            post_side, pnl, fee, balance, price, cost, raw_action_json)
           values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(second_result, *row) for row in actions],
    )
    equity = connection.execute(
        "select sample_index, timestamp_utc, wallet, equity from strategy_equity where result_id = ? order by sample_index",
        [first_result],
    ).fetchall()
    connection.executemany(
        "insert into strategy_equity (result_id, sample_index, timestamp_utc, wallet, equity) values (?, ?, ?, ?, ?)",
        [(second_result, *row) for row in equity],
    )
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    return database, (first_result, second_result)


def test_old_preparation_version_fails_strict_read_then_rebuilds(tmp_path: Path) -> None:
    database, (result_id,) = _typed_candidate_database(tmp_path)
    first = prepare_current_optimizer_inputs(str(database), [result_id], workers=1)
    assert first[0].availability.available
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "update optimizer_prepared_inputs set preparation_version = '4' where result_id = ?",
            [result_id],
        )

    with pytest.raises(OptimizerIntegrityError, match="stale"):
        read_prepared_optimizer_inputs(str(database), [result_id])

    rebuilt = prepare_current_optimizer_inputs(str(database), [result_id], workers=1)
    assert rebuilt[0].availability.available
    assert read_prepared_optimizer_inputs(str(database), [result_id])[0].prepared is not None
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute(
            "select preparation_version from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone() == ("5",)


def test_reusable_prepared_row_deleted_before_writer_is_reinserted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, (result_id,) = _typed_candidate_database(tmp_path)
    assert prepare_current_optimizer_inputs(str(database), [result_id], workers=1)[0].availability.available
    real_lock = store.PerformanceV2WriterLock

    class DeleteReusableRow:
        def __init__(self, parent: Path) -> None:
            self.lock = real_lock(parent)

        def __enter__(self):
            entered = self.lock.__enter__()
            with duckdb.connect(str(database)) as connection:
                connection.execute("delete from optimizer_prepared_inputs where result_id = ?", [result_id])
            return entered

        def __exit__(self, *args):
            return self.lock.__exit__(*args)

    monkeypatch.setattr(store, "PerformanceV2WriterLock", DeleteReusableRow)
    rebuilt = prepare_current_optimizer_inputs(str(database), [result_id], workers=1)

    assert rebuilt[0].availability.available
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute(
            "select count(*) from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone() == (1,)


@pytest.mark.parametrize("workers", [0, -1, None, True])
def test_current_preparation_requires_positive_integer_workers(tmp_path: Path, workers: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        prepare_current_optimizer_inputs(str(tmp_path / "missing.duckdb"), (), workers=workers)  # type: ignore[arg-type]


def test_parallel_current_preparation_matches_serial_bytes_on_two_results(tmp_path: Path) -> None:
    source, result_ids = _typed_candidate_database(tmp_path / "source", two_results=True)
    serial = tmp_path / "serial.duckdb"
    parallel = tmp_path / "parallel.duckdb"
    shutil.copy2(source, serial)
    shutil.copy2(source, parallel)

    serial_result = prepare_current_optimizer_inputs(str(serial), None, workers=1)
    parallel_result = prepare_current_optimizer_inputs(str(parallel), None, workers=2)

    assert [item.availability for item in serial_result] == [item.availability for item in parallel_result]
    assert tuple(item.source.result_id for item in serial_result) == result_ids
    with duckdb.connect(str(serial), read_only=True) as serial_connection, duckdb.connect(str(parallel), read_only=True) as parallel_connection:
        serial_rows = serial_connection.execute(
            "select result_id, source_digest, prepared_json from optimizer_prepared_inputs order by result_id"
        ).fetchall()
        parallel_rows = parallel_connection.execute(
            "select result_id, source_digest, prepared_json from optimizer_prepared_inputs order by result_id"
        ).fetchall()
    assert serial_rows == parallel_rows


def test_parallel_worker_failure_happens_before_writer_and_leaves_no_rows(tmp_path: Path) -> None:
    database, result_ids = _typed_candidate_database(tmp_path, two_results=True)
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "update strategy_actions set action = 'not-supported' where result_id = ? and action_index = 0",
            [result_ids[0]],
        )

    with pytest.raises(Exception, match="unsupported performance action"):
        prepare_current_optimizer_inputs(str(database), None, workers=2)

    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("select count(*) from optimizer_prepared_inputs").fetchone() == (0,)


def test_second_lazy_writer_insert_failure_rolls_back_all_prepared_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, _result_ids = _typed_candidate_database(tmp_path, two_results=True)
    real_connect = duckdb.connect
    insert_count = 0

    class FailingWriter:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, sql, parameters=None):
            nonlocal insert_count
            if "insert into optimizer_prepared_inputs" in str(sql).lower():
                insert_count += 1
                if insert_count == 2:
                    raise RuntimeError("injected second prepared insert failure")
            return self.connection.execute(sql, parameters) if parameters is not None else self.connection.execute(sql)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def connect(path, *args, **kwargs):
        connection = real_connect(path, *args, **kwargs)
        if not kwargs.get("read_only", False):
            return FailingWriter(connection)
        return connection

    monkeypatch.setattr(duckdb, "connect", connect)
    with pytest.raises(RuntimeError, match="second prepared insert failure"):
        prepare_current_optimizer_inputs(str(database), None, workers=1)

    with real_connect(str(database), read_only=True) as connection:
        assert connection.execute("select count(*) from optimizer_prepared_inputs").fetchone() == (0,)
