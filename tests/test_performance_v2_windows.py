from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import duckdb
import pytest
import mrs3.performance_v2_windows as windows_module

from mrs3.performance_v2_store import initialize_performance_v2
from mrs3.performance_v2_windows import (
    METRICS_VERSION,
    _Action,
    _calculate,
    _Equity,
    _round_trips,
    _persist,
    _persist_many,
    WindowMetrics,
    compare_window_pair_geometrically,
    get_or_calculate_window,
    get_or_calculate_window_pair,
)

UTC = timezone.utc


def _typed_source() -> tuple[datetime, datetime, tuple[_Action, ...], tuple[_Equity, ...]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    actions = (
        _Action(0, start, "opened", Decimal("1"), Decimal("0"), Decimal("0")),
        _Action(1, start + timedelta(days=1), "closed", Decimal("0"), Decimal("5"), Decimal("1")),
        _Action(2, start + timedelta(days=2), "opened", Decimal("1"), Decimal("0"), Decimal("0")),
        _Action(3, start + timedelta(days=3), "closed", Decimal("0"), Decimal("5"), Decimal("1")),
    )
    equity = (
        _Equity(0, start, Decimal("100"), Decimal("100")),
        _Equity(1, start + timedelta(days=1), Decimal("105"), Decimal("105")),
        _Equity(2, start + timedelta(days=2), Decimal("105"), Decimal("105")),
        _Equity(3, start + timedelta(days=3), Decimal("110"), Decimal("110")),
        _Equity(4, start + timedelta(days=4), Decimal("110"), Decimal("110")),
    )
    return start, start + timedelta(days=4), actions, equity


def test_calculate_accepts_authoritative_flat_samples_without_changing_metrics(monkeypatch) -> None:
    report_start, report_end, actions, equity = _typed_source()
    calls = 0
    original = windows_module._flat_samples

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(windows_module, "_flat_samples", counted)
    no_start = _calculate(
        1, report_start, report_end, METRICS_VERSION, report_start, report_end, actions, equity,
        flat_samples=(),
    )
    no_end = _calculate(
        1, report_start + timedelta(days=2), report_start + timedelta(days=2, hours=1),
        METRICS_VERSION, report_start, report_end, actions, equity,
        flat_samples=(report_start + timedelta(days=3), report_start + timedelta(days=4)),
    )
    supplied = (report_start + timedelta(days=1), report_start + timedelta(days=3), report_end)
    with_flat = _calculate(
        1, report_start, report_end, METRICS_VERSION, report_start, report_end, actions, equity,
        flat_samples=supplied,
    )

    assert no_start.unavailable_reason == "NO_FLAT_START"
    assert no_end.unavailable_reason == "NO_FLAT_END"
    assert calls == 0

    original_result = _calculate(
        1, report_start, report_end, METRICS_VERSION, report_start, report_end, actions, equity,
    )
    assert type(with_flat) is WindowMetrics
    assert with_flat == original_result
    assert calls == 1


@pytest.mark.parametrize(
    ("offsets", "left", "right", "reason"),
    [
        ((), 0, 4, "NO_FLAT_START"),
        ((0, 1), 2, 4, "NO_FLAT_START"),
        ((3, 4), 1, 2, "NO_FLAT_END"),
        ((2,), 1, 3, "COLLAPSED"),
        ((0, 4), 1, 3, "COLLAPSED"),
        ((0, 4), 2, 3, "COLLAPSED"),
    ],
)
def test_ordered_boundaries_keep_linear_reason_precedence(offsets, left, right, reason) -> None:
    report_start, report_end, actions, equity = _typed_source()
    flat = tuple(report_start + timedelta(days=offset) for offset in offsets)
    args = (1, report_start + timedelta(days=left), report_start + timedelta(days=right),
            METRICS_VERSION, report_start, report_end, actions, equity)
    expected = _calculate(*args, flat_samples=flat)
    assert expected.unavailable_reason == reason
    assert _calculate(*args, flat_samples=flat, ordered_source=True) == expected


def test_ordered_boundary_search_matches_linear_with_duplicate_ends(monkeypatch) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=2)
    actions = (
        _Action(0, start, "opened", Decimal(1), Decimal(0), Decimal(0)),
        _Action(1, start, "closed", Decimal(0), Decimal(1), Decimal(1)),
        _Action(2, start, "opened", Decimal(1), Decimal(0), Decimal(0)),
        _Action(3, end, "decreased", Decimal(1), Decimal(2), Decimal(2)),
        _Action(4, end, "closed", Decimal(0), Decimal(3), Decimal(3)),
    )
    equity = (
        _Equity(0, start, Decimal(100), Decimal(100)),
        _Equity(1, start, Decimal(101), Decimal(101)),
        _Equity(2, end, Decimal(105), Decimal(105)),
        _Equity(3, end, Decimal(106), Decimal(106)),
    )
    flat = (start, start, end, end)
    args = (1, start, end, METRICS_VERSION, start, end, actions, equity)
    expected = _calculate(*args, flat_samples=flat)
    assert expected.availability_status == "AVAILABLE"
    assert expected.fees_pct == Decimal(5) / Decimal(100) * 100
    assert _calculate(*args, flat_samples=flat, ordered_source=True) == expected


def test_ordered_boundary_search_is_opt_in(monkeypatch) -> None:
    report_start, report_end, actions, equity = _typed_source()
    args = (1, report_start, report_end, METRICS_VERSION, report_start, report_end, actions, equity)
    flat = (report_start, report_start + timedelta(days=1), report_end)
    expected = _calculate(*args, flat_samples=flat)
    assert expected.availability_status == "AVAILABLE"
    original_left = windows_module.bisect_left
    original_right = windows_module.bisect_right
    calls = []

    def counted_left(*values, **kwargs):
        calls.append("left")
        return original_left(*values, **kwargs)

    def counted_right(*values, **kwargs):
        calls.append("right")
        return original_right(*values, **kwargs)

    monkeypatch.setattr(windows_module, "bisect_left", counted_left)
    monkeypatch.setattr(windows_module, "bisect_right", counted_right)
    assert _calculate(*args, flat_samples=flat) == expected
    assert calls == []
    assert _calculate(*args, flat_samples=flat, ordered_source=True) == expected
    assert calls == ["left", "right", "left", "right", "right", "right"]
    calls.clear()
    out_of_range = _calculate(
        1, report_end + timedelta(days=1), report_end + timedelta(days=2),
        METRICS_VERSION, report_start, report_end, actions, equity,
        ordered_source=True,
    )
    assert out_of_range.unavailable_reason == "OUT_OF_RANGE"
    assert calls == []


def test_default_calculation_keeps_unordered_portfolio_inputs(monkeypatch) -> None:
    report_start, report_end, actions, equity = _typed_source()
    args = (1, report_start, report_end, METRICS_VERSION,
            report_start, report_end, tuple(reversed(actions)), tuple(reversed(equity)))
    flat = (report_start, report_start + timedelta(days=1), report_end)
    expected = _calculate(*args, flat_samples=flat)
    monkeypatch.setattr(windows_module, "bisect_left", lambda *_args, **_kwargs: pytest.fail("default used bisect"))
    monkeypatch.setattr(windows_module, "bisect_right", lambda *_args, **_kwargs: pytest.fail("default used bisect"))
    assert _calculate(*args, flat_samples=flat) == expected


def test_ordered_boundary_search_keeps_no_trades_reason() -> None:
    report_start, report_end, _actions, equity = _typed_source()
    args = (1, report_start, report_end, METRICS_VERSION,
            report_start, report_end, (), equity)
    flat = (report_start, report_end)
    expected = _calculate(*args, flat_samples=flat)
    assert expected.unavailable_reason == "NO_TRADES"
    assert _calculate(*args, flat_samples=flat, ordered_source=True) == expected


def _db(tmp_path, *, scale: Decimal = Decimal("1")) -> tuple[duckdb.DuckDBPyConnection, int]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(tmp_path / "strategy_performance.duckdb"))
    initialize_performance_v2(connection)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_id = connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
           order_count, analysis_run_id, candidate_identity, lifecycle_status,
           created_at_utc, updated_at_utc) values ('alpha', 'BTCUSDT', 'LONG', '1h',
           3, 1, 'run', 'candidate', 'ACTIVE', ?, ?) returning strategy_id""",
        [now, now],
    ).fetchone()[0]
    result_id = connection.execute(
        """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
           commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
           max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
           values (?, ?, ?, 'Bybit', .0004, ?, ?, ?, ?, 0, 0, ?, 2, ?) returning result_id""",
        [
            strategy_id,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 5, tzinfo=UTC),
            Decimal("100") * scale,
            Decimal("110") * scale,
            Decimal("10") * scale,
            Decimal("10") * scale,
            Decimal("2") * scale,
            now,
        ],
    ).fetchone()[0]
    connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
    actions = [
        (result_id, 0, datetime(2026, 1, 1, 0, tzinfo=UTC), "BTCUSDT", 1, "opened", 1, 1, "long", 0, 1 * scale, 100 * scale, None),
        (result_id, 1, datetime(2026, 1, 1, 12, tzinfo=UTC), "BTCUSDT", 1, "increased", 1, 2, "long", 0, 1 * scale, 100 * scale, None),
        (result_id, 2, datetime(2026, 1, 2, 0, tzinfo=UTC), "BTCUSDT", 1, "decreased", 1, 1, "long", Decimal("3") * scale, Decimal("0.5") * scale, Decimal("103") * scale, None),
        (result_id, 3, datetime(2026, 1, 3, 0, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", Decimal("7") * scale, Decimal("0.5") * scale, Decimal("110") * scale, None),
        (result_id, 4, datetime(2026, 1, 4, 0, tzinfo=UTC), "BTCUSDT", 1, "opened", 1, 1, "long", Decimal("0") * scale, Decimal("1") * scale, Decimal("110") * scale, None),
        (result_id, 5, datetime(2026, 1, 4, 12, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", Decimal("2") * scale, Decimal("0.2") * scale, Decimal("112") * scale, None),
    ]
    connection.executemany(
        "insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size, post_size, post_side, pnl, fee, balance, raw_action_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", actions
    )
    equity = [
        (result_id, 0, datetime(2026, 1, 1, tzinfo=UTC), 100 * scale, 100 * scale),
        (result_id, 1, datetime(2026, 1, 2, tzinfo=UTC), 103 * scale, 105 * scale),
        (result_id, 2, datetime(2026, 1, 3, tzinfo=UTC), 110 * scale, 108 * scale),
        (result_id, 3, datetime(2026, 1, 4, tzinfo=UTC), 110 * scale, 109 * scale),
        (result_id, 4, datetime(2026, 1, 5, tzinfo=UTC), 112 * scale, 112 * scale),
    ]
    connection.executemany("insert into strategy_equity values (?, ?, ?, ?, ?)", equity)
    return connection, int(result_id)


def test_load_source_orders_shuffled_timestamps_for_selection(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    try:
        connection.execute(
            "update strategy_actions set timestamp_utc = ? where result_id = ? and action_index in (4, 5)",
            [datetime(2026, 1, 1, tzinfo=UTC), result_id],
        )
        connection.execute(
            "update strategy_equity set timestamp_utc = ? where result_id = ? and sample_index in (3, 4)",
            [datetime(2026, 1, 1, tzinfo=UTC), result_id],
        )
        _, _, actions, equity = windows_module._load_source(connection, result_id)
        action_keys = [(item.timestamp, item.index) for item in actions]
        equity_keys = [(item.timestamp, item.index) for item in equity]
        assert action_keys == sorted(action_keys)
        assert equity_keys == sorted(equity_keys)
        assert tuple(item.index for item in actions) != tuple(range(len(actions)))
        assert tuple(item.index for item in equity) != tuple(range(len(equity)))
        flat = windows_module._flat_samples(equity, actions)
        assert flat == tuple(sorted(flat))
        assert all(value.tzinfo is not None and value.utcoffset() == timedelta(0) for value in flat)
    finally:
        connection.close()


def test_ordered_boundaries_match_stored_rows_at_duplicate_w0_w1(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    try:
        w0 = datetime(2026, 1, 3, tzinfo=UTC)
        w1 = datetime(2026, 1, 5, tzinfo=UTC)
        connection.execute(
            "update strategy_actions set timestamp_utc = ? where result_id = ? and action_index = 2",
            [w0, result_id],
        )
        connection.execute(
            "update strategy_actions set timestamp_utc = ? where result_id = ? and action_index in (4, 5)",
            [w1, result_id],
        )
        connection.execute(
            "update strategy_equity set timestamp_utc = ? where result_id = ? and sample_index = 3",
            [w0, result_id],
        )
        connection.execute(
            "insert into strategy_equity values (?, 5, ?, 112, 112)", [result_id, w1],
        )
        source = windows_module._load_source(connection, result_id)
        flat = windows_module._flat_samples(source[3], source[2])
        assert flat.count(w0) == flat.count(w1) == 2
        assert sum(item.timestamp == w0 for item in source[2]) == 2
        assert sum(item.timestamp == w1 for item in source[2]) == 2
        assert sum(item.timestamp == w0 for item in source[3]) == 2
        assert sum(item.timestamp == w1 for item in source[3]) == 2
        args = (result_id, w0, w1, METRICS_VERSION, *source)
        linear = _calculate(*args, flat_samples=flat)
        ordered = _calculate(*args, flat_samples=flat, ordered_source=True)
        assert linear.availability_status == "AVAILABLE"
        assert ordered == linear
        _persist(connection, linear)
        row_linear = connection.execute(
            "select * exclude(calculated_at_utc) from window_metrics where result_id = ?", [result_id],
        ).fetchone()
        _persist(connection, ordered)
        row_ordered = connection.execute(
            "select * exclude(calculated_at_utc) from window_metrics where result_id = ?", [result_id],
        ).fetchone()
        assert row_ordered == row_linear
    finally:
        connection.close()


def _persist_metric(
    result_id: int = 1,
    *,
    key: str = "A",
    key_index: int | None = None,
    value: Decimal = Decimal("1.25"),
    unavailable_reason: str | None = None,
    trade_count: int | None = 3,
) -> WindowMetrics:
    start = datetime(2026, 2, 1, tzinfo=UTC) + timedelta(
        days=ord(key[0]) - ord("A") if key_index is None else key_index
    )
    return WindowMetrics(
        result_id, start, start + timedelta(days=1), METRICS_VERSION,
        start, start + timedelta(days=1), "UNAVAILABLE" if unavailable_reason else "AVAILABLE",
        unavailable_reason, value, value, value, value, value, value, value, value,
        trade_count, value, value, value,
    )


class _RecordingConnection:
    def __init__(self, connection=None):
        self.connection = connection
        self.calls: list[tuple[str, object]] = []
        self.returned: list[object] = []

    def execute(self, sql: str, parameters=None):
        self.calls.append((sql, parameters))
        result = self.connection.execute(sql, parameters) if self.connection is not None else object()
        self.returned.append(result)
        return result


def test_persist_many_empty_is_a_noop_and_matches_scalar_sql_for_one_row(monkeypatch, tmp_path) -> None:
    fixed = datetime(2026, 9, 29, 12, tzinfo=UTC)
    calls: list[datetime] = []

    class FrozenDatetime:
        @classmethod
        def now(cls, tz=None):
            calls.append(fixed)
            return fixed

    monkeypatch.setattr(windows_module, "datetime", FrozenDatetime)
    metric = _persist_metric()
    scalar_db, _ = _db(tmp_path / "scalar")
    bulk_db, _ = _db(tmp_path / "bulk")
    scalar = _RecordingConnection(scalar_db)
    bulk = _RecordingConnection(bulk_db)
    try:
        _persist(scalar, metric)
        before_bulk = len(calls)
        _persist_many(bulk, [metric])
        assert len(calls) == before_bulk + 1
        assert scalar.calls[0][0] == bulk.calls[0][0]
        assert scalar.calls[0][1] == bulk.calls[0][1]
        empty = _RecordingConnection()
        _persist_many(empty, [])
        assert empty.calls == []
        assert len(calls) == before_bulk + 1
    finally:
        scalar_db.close()
        bulk_db.close()


@pytest.mark.parametrize(("count", "expected_chunks"), [(896, [18_816]), (897, [18_816, 21])])
def test_persist_many_uses_fixed_896_row_chunks(count: int, expected_chunks: list[int]) -> None:
    recorder = _RecordingConnection()
    metrics = [_persist_metric(result_id=index + 1) for index in range(count)]

    _persist_many(recorder, metrics)

    assert [len(parameters) for _, parameters in recorder.calls] == expected_chunks
    assert all("begin" not in sql.lower() and "commit" not in sql.lower() for sql, _ in recorder.calls)


def test_persist_many_uses_source_ordered_duplicate_groups(monkeypatch) -> None:
    fixed = datetime(2026, 9, 29, 12, tzinfo=UTC)
    clock_calls: list[datetime] = []

    class FrozenDatetime:
        @classmethod
        def now(cls, tz=None):
            value = fixed + timedelta(seconds=len(clock_calls))
            clock_calls.append(value)
            return value

    monkeypatch.setattr(windows_module, "datetime", FrozenDatetime)
    metrics = [_persist_metric(result_id=index + 1) for index in range(896)]
    metrics.append(_persist_metric(result_id=898, key_index=896, value=Decimal("88.88")))
    metrics.append(_persist_metric(value=Decimal("99.99")))
    metrics.append(_persist_metric(result_id=898, key_index=896, value=Decimal("77.77")))
    recorder = _RecordingConnection()

    _persist_many(recorder, metrics)

    assert len(clock_calls) == len(metrics)
    assert len(recorder.calls) == 3
    first_parameters = recorder.calls[0][1]
    second_parameters = recorder.calls[1][1]
    third_parameters = recorder.calls[2][1]
    assert len(first_parameters) == 18_816
    assert len(second_parameters) == 42
    assert len(third_parameters) == 21
    assert first_parameters[0] == 1
    assert first_parameters[8] == Decimal("1.25")
    assert first_parameters[20] == clock_calls[0]
    assert second_parameters[0] == 898
    assert second_parameters[8] == Decimal("88.88")
    assert second_parameters[20] == clock_calls[896]
    assert second_parameters[21] == 1
    assert second_parameters[29] == Decimal("99.99")
    assert second_parameters[41] == clock_calls[897]
    assert third_parameters[0] == 898
    assert third_parameters[8] == Decimal("77.77")
    assert third_parameters[20] == clock_calls[898]


def test_persist_many_does_not_hide_nonfinite_earlier_duplicate(monkeypatch) -> None:
    fixed = datetime(2026, 9, 29, 12, tzinfo=UTC)
    clock_calls: list[datetime] = []

    class FrozenDatetime:
        @classmethod
        def now(cls, tz=None):
            value = fixed + timedelta(seconds=len(clock_calls))
            clock_calls.append(value)
            return value

    monkeypatch.setattr(windows_module, "datetime", FrozenDatetime)
    recorder = _RecordingConnection()

    with pytest.raises(duckdb.ConversionException):
        _persist_many(
            recorder,
            [_persist_metric(value=Decimal("NaN")), _persist_metric(value=Decimal("2.5"))],
        )

    assert len(clock_calls) == 2
    assert recorder.calls == []


def test_persist_many_preserves_native_validation_for_earlier_duplicate_overflows(tmp_path) -> None:
    cases = (
        (
            "decimal-overflow",
            replace(_persist_metric(), growth_factor=Decimal("123456789012345678901234567890123456789")),
            _persist_metric(value=Decimal("2.25")),
        ),
        (
            "integer-overflow",
            replace(_persist_metric(), trade_count=2_147_483_648),
            _persist_metric(value=Decimal("2.25")),
        ),
    )

    for label, invalid, valid in cases:
        for bulk in (False, True):
            database_dir = tmp_path / f"{label}-{'bulk' if bulk else 'scalar'}"
            database = database_dir / "strategy_performance.duckdb"
            connection, _ = _db(database_dir)
            try:
                connection.execute("begin transaction")
                with pytest.raises(duckdb.ConversionException):
                    if bulk:
                        _persist_many(connection, [invalid, valid])
                    else:
                        _persist(connection, invalid)
                connection.execute("rollback")
            finally:
                connection.close()
            with duckdb.connect(str(database), read_only=True) as check:
                assert check.execute("select count(*) from window_metrics").fetchone() == (0,)


@pytest.mark.parametrize("mode", ["scalar", "bulk", "scalar_bulk", "bulk_scalar"])
def test_scalar_and_bulk_persistence_have_identical_actual_schema_rows(tmp_path, monkeypatch, mode: str) -> None:
    fixed = datetime(2026, 9, 29, 12, tzinfo=UTC)

    class FrozenDatetime:
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(windows_module, "datetime", FrozenDatetime)
    first = replace(
        _persist_metric(
            value=Decimal("12345678901234567890123456.123456789012"),
            unavailable_reason="нет-данных", trade_count=None,
        ),
        growth_factor=None, return_pct=Decimal("0"), daily_log_return=Decimal("-1.5"),
    )
    second = _persist_metric(key="B", value=Decimal("1.0000000000005"), trade_count=2_147_483_647)
    latest = _persist_metric(value=Decimal("2.25"), unavailable_reason=None, trade_count=7)
    mixed = [
        replace(_persist_metric(key_index=10), growth_factor=None),
        replace(_persist_metric(key_index=11), growth_factor=Decimal("0")),
        replace(_persist_metric(key_index=12), growth_factor=Decimal("-1.5")),
        replace(
            _persist_metric(key_index=13, unavailable_reason="\u043d\u0435\u0442-\u0434\u0430\u043d\u043d\u044b\u0445"),
            growth_factor=Decimal("12345678901234567890123456.123456789012"),
        ),
        replace(
            _persist_metric(key_index=14, trade_count=2_147_483_647),
            growth_factor=Decimal("1.0000000000005"),
        ),
    ]
    all_metrics = [first, second, *mixed, latest]
    database = tmp_path / mode
    connection, _ = _db(database)
    try:
        if mode == "scalar":
            for metric in all_metrics:
                _persist(connection, metric)
        elif mode == "bulk":
            _persist_many(connection, all_metrics)
        elif mode == "scalar_bulk":
            _persist(connection, first)
            _persist_many(connection, [second, *mixed, latest])
        else:
            _persist_many(connection, [first, second, *mixed])
            _persist(connection, latest)
        observed = connection.execute(
            "select result_id, requested_start_utc, requested_end_utc, metrics_version, effective_start_utc, effective_end_utc, availability_status, unavailable_reason, growth_factor, return_pct, daily_log_return, daily_growth_pct, max_drawdown_pct, return_dd_ratio, fees_pct, profit_factor, trade_count, win_rate_pct, holding_seconds, time_in_market_pct, calculated_at_utc from window_metrics order by requested_start_utc"
        ).fetchall()
        assert len(observed) == 7
        assert {row[8] for row in observed} >= {
            None, Decimal("0"), Decimal("-1.5"),
            Decimal("12345678901234567890123456.123456789012"),
            Decimal("1.000000000001"),
        }
        assert any(row[7] == "\u043d\u0435\u0442-\u0434\u0430\u043d\u043d\u044b\u0445" for row in observed)
        assert any(row[16] == 2_147_483_647 for row in observed)
    finally:
        connection.close()

    comparison_path = tmp_path / "comparison"
    comparison, _ = _db(comparison_path)
    try:
        _persist_many(comparison, all_metrics)
        assert comparison.execute(
            "select result_id, requested_start_utc, requested_end_utc, metrics_version, effective_start_utc, effective_end_utc, availability_status, unavailable_reason, growth_factor, return_pct, daily_log_return, daily_growth_pct, max_drawdown_pct, return_dd_ratio, fees_pct, profit_factor, trade_count, win_rate_pct, holding_seconds, time_in_market_pct, calculated_at_utc from window_metrics order by requested_start_utc"
        ).fetchall() == observed
    finally:
        comparison.close()


@pytest.mark.parametrize("nonfinite", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
@pytest.mark.parametrize("bulk", [False, True])
def test_nonfinite_scalar_and_bulk_raise_and_rollback(tmp_path, nonfinite: Decimal, bulk: bool) -> None:
    database_dir = tmp_path / ("bulk" if bulk else "scalar")
    database = database_dir / "strategy_performance.duckdb"
    connection, _ = _db(database_dir)
    metric = _persist_metric(value=nonfinite)
    try:
        connection.execute("begin transaction")
        with pytest.raises(duckdb.ConversionException):
            _persist_many(connection, [metric]) if bulk else _persist(connection, metric)
        connection.execute("rollback")
    finally:
        connection.close()
    with duckdb.connect(str(database), read_only=True) as check:
        assert check.execute("select count(*) from window_metrics").fetchone() == (0,)


def test_persist_many_rolls_back_first_chunk_when_second_chunk_conversion_fails(tmp_path) -> None:
    database_dir = tmp_path / "late-failure"
    database = database_dir / "strategy_performance.duckdb"
    connection, _ = _db(database_dir)
    valid = [_persist_metric(key_index=index) for index in range(897)]
    invalid = _persist_metric(key_index=897, value=Decimal("123456789012345678901234567890123456789"))
    recorder = _RecordingConnection(connection)
    try:
        connection.execute("begin transaction")
        with pytest.raises(duckdb.ConversionException):
            _persist_many(recorder, [*valid, invalid])
        assert len(recorder.calls) == 2
        assert len(recorder.returned) == 1
        connection.execute("rollback")
    finally:
        connection.close()
    with duckdb.connect(str(database), read_only=True) as check:
        assert check.execute("select count(*) from window_metrics").fetchone() == (0,)


def test_boundaries_move_inward_independently_and_never_expand(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    try:
        window = get_or_calculate_window(
            connection,
            result_id,
            datetime(2025, 12, 31, tzinfo=UTC),
            datetime(2026, 1, 5, tzinfo=UTC),
        )
        assert window.availability_status == "AVAILABLE"
        assert window.effective_start_utc == datetime(2026, 1, 3, tzinfo=UTC)
        assert window.effective_end_utc == datetime(2026, 1, 5, tzinfo=UTC)

        clipped = get_or_calculate_window(
            connection,
            result_id,
            datetime(2026, 1, 1, 12, tzinfo=UTC),
            datetime(2026, 1, 3, 12, tzinfo=UTC),
        )
        assert clipped.effective_start_utc == datetime(2026, 1, 3, tzinfo=UTC)
        assert clipped.effective_end_utc == datetime(2026, 1, 3, tzinfo=UTC)
    finally:
        connection.close()


def test_overlapping_nested_and_disjoint_pair_is_independently_cached(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    try:
        pair = get_or_calculate_window_pair(
            connection,
            result_id,
            ("2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z"),
            ("2026-01-02T00:00:00Z", "2026-01-05T00:00:00Z"),
        )
        assert pair[0].effective_start_utc <= pair[1].effective_start_utc
        assert connection.execute("select count(*) from window_metrics").fetchone() == (2,)
        nested = get_or_calculate_window_pair(
            connection,
            result_id,
            ("2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z"),
            ("2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"),
        )
        assert nested[0].effective_start_utc <= nested[1].effective_start_utc
        disjoint = get_or_calculate_window_pair(
            connection,
            result_id,
            ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
            ("2026-01-04T00:00:00Z", "2026-01-05T00:00:00Z"),
        )
        assert disjoint[0].availability_status == "UNAVAILABLE"
        assert disjoint[1].availability_status == "UNAVAILABLE"
    finally:
        connection.close()


def test_equity_quality_source_read_is_bounded_and_keeps_exact_source_counts(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 31, tzinfo=UTC)
    connection.execute("update strategy_results set report_end_utc = ? where result_id = ?", [end, result_id])
    connection.execute(
        "update strategy_equity set equity = -1 where result_id = ? and sample_index = 0", [result_id]
    )
    connection.execute(
        "insert into strategy_equity values (?, 5, ?, 100, 100)",
        [result_id, datetime(2025, 12, 31, tzinfo=UTC)],
    )
    connection.execute(
        "insert into strategy_equity values (?, 6, ?, 100, 100)",
        [result_id, datetime(2026, 1, 3, tzinfo=UTC)],
    )
    try:
        loader = getattr(windows_module, "_load_equity_samples_for_quality", None)
        assert callable(loader), "bounded equity-only source loader is missing"

        samples, summary = loader(connection, result_id, start, end)

        assert [sample.sample_index for sample in samples] == [5, 0, 2, 6, 3, 4]
        assert summary.raw_sample_count == 7
        assert summary.in_report_sample_count == 6
        assert summary.nonpositive_in_report_rows == 1
        assert summary.duplicate_timestamp_count == 1
        assert summary.invalid_reasons == ("EQUITY_OUTSIDE_REPORT_INTERVAL",)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("start", "end", "reason"),
    [
        ("2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z", "OUT_OF_RANGE"),
        ("2026-01-01T00:00:00Z", "2026-01-02T06:00:00Z", "NO_FLAT_END"),
        ("2026-01-03T00:00:00Z", "2026-01-03T00:00:00Z", "COLLAPSED"),
    ],
)
def test_unavailable_outcomes_are_cacheable(tmp_path, start, end, reason) -> None:
    connection, result_id = _db(tmp_path)
    try:
        first = get_or_calculate_window(connection, result_id, start, end)
        second = get_or_calculate_window(connection, result_id, start, end)
        assert first.availability_status == second.availability_status == "UNAVAILABLE"
        assert first.unavailable_reason == second.unavailable_reason == reason
        assert connection.execute("select count(*) from window_metrics").fetchone() == (1,)
    finally:
        connection.close()


def test_no_trades_is_unavailable_and_calculator_version_is_a_cache_miss(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    try:
        connection.execute("delete from strategy_actions where result_id = ?", [result_id])
        first = get_or_calculate_window(connection, result_id, "2026-01-01", "2026-01-05")
        assert first.unavailable_reason == "NO_TRADES"
        second = get_or_calculate_window(
            connection, result_id, "2026-01-01", "2026-01-05", calculator_version="test-v2"
        )
        assert second.metrics_version == "test-v2"
        assert connection.execute("select count(*) from window_metrics").fetchone() == (2,)
    finally:
        connection.close()


def test_no_flat_start_is_typed_and_four_timestamp_pair_form_is_supported(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    try:
        connection.execute("update strategy_actions set post_size = 1 where result_id = ?", [result_id])
        unavailable = get_or_calculate_window(connection, result_id, "2026-01-01", "2026-01-05")
        assert unavailable.unavailable_reason == "NO_FLAT_START"
        pair = get_or_calculate_window_pair(
            connection,
            result_id,
            "2026-01-01",
            "2026-01-05",
            "2026-01-01",
            "2026-01-05",
        )
        assert pair[0].unavailable_reason == pair[1].unavailable_reason == "NO_FLAT_START"
    finally:
        connection.close()


def test_upnl_metrics_are_scale_invariant_and_partial_fills_form_round_trips(tmp_path) -> None:
    first_connection, first_id = _db(tmp_path / "one")
    second_connection, second_id = _db(tmp_path / "two", scale=Decimal("10"))
    try:
        first = get_or_calculate_window(first_connection, first_id, "2026-01-01", "2026-01-05")
        cached = get_or_calculate_window(first_connection, first_id, "2026-01-01", "2026-01-05")
        second = get_or_calculate_window(second_connection, second_id, "2026-01-01", "2026-01-05")
        assert first == cached
        assert first.trade_count == second.trade_count == 1
        assert first.growth_factor == second.growth_factor
        assert first.return_pct == second.return_pct
        assert first.max_drawdown_pct == second.max_drawdown_pct
        assert first.profit_factor == second.profit_factor
        assert first.fees_pct == second.fees_pct
        assert first.holding_seconds > 0
        assert 0 < first.time_in_market_pct <= 100
    finally:
        first_connection.close()
        second_connection.close()


@pytest.mark.parametrize(
    ("window_a", "window_b", "expected_flat"),
    [
        (("2026-01-01", "2026-01-05"), ("2026-01-02", "2026-01-05"), 2),
        (("2026-01-06", "2026-01-07"), ("2026-01-08", "2026-01-09"), 0),
        (("2026-01-01", "2026-01-05"), ("2026-01-06", "2026-01-07"), 1),
        (("2026-01-06", "2026-01-07"), ("2026-01-01", "2026-01-05"), 1),
    ],
)
def test_pair_distinct_cold_windows_reuse_source_without_changing_calculation_work(
    tmp_path, monkeypatch, window_a, window_b, expected_flat
) -> None:
    connection, result_id = _db(tmp_path / "candidate")
    oracle_connection, oracle_result_id = _db(tmp_path / "oracle")
    oracle_source = windows_module._load_source(oracle_connection, oracle_result_id)
    oracle_metrics = tuple(
        get_or_calculate_window(oracle_connection, oracle_result_id, *window)
        for window in (window_a, window_b)
    )
    deterministic_sql = (
        "select " + ", ".join(windows_module._METRIC_COLUMNS) +
        " from window_metrics where result_id = ? order by requested_start_utc"
    )
    oracle_rows = oracle_connection.execute(deterministic_sql, [oracle_result_id]).fetchall()
    counts = {name: 0 for name in ("source", "flat", "calculate", "persist", "cached")}
    consumed_sources: list[tuple[datetime, datetime, tuple[_Action, ...], tuple[_Equity, ...]]] = []
    original_source = windows_module._load_source
    original_flat = windows_module._flat_samples
    original_calculate = windows_module._calculate
    original_persist = windows_module._persist
    original_cached = windows_module._cached

    def counted_source(*args):
        counts["source"] += 1
        return original_source(*args)

    def counted_flat(*args):
        counts["flat"] += 1
        return original_flat(*args)

    def counted_calculate(*args, **kwargs):
        counts["calculate"] += 1
        consumed_sources.append(args[4:8])
        return original_calculate(*args, **kwargs)

    def counted_persist(*args):
        counts["persist"] += 1
        return original_persist(*args)

    def counted_cached(*args):
        counts["cached"] += 1
        return original_cached(*args)

    monkeypatch.setattr(windows_module, "_load_source", counted_source)
    monkeypatch.setattr(windows_module, "_flat_samples", counted_flat)
    monkeypatch.setattr(windows_module, "_calculate", counted_calculate)
    monkeypatch.setattr(windows_module, "_persist", counted_persist)
    monkeypatch.setattr(windows_module, "_cached", counted_cached)

    try:
        observed = get_or_calculate_window_pair(connection, result_id, window_a, window_b)
        assert counts == {"source": 1, "flat": expected_flat, "calculate": 2, "persist": 2, "cached": 4}
        assert observed == oracle_metrics
        assert [
            (metric.availability_status, metric.unavailable_reason) for metric in observed
        ] == [
            (metric.availability_status, metric.unavailable_reason) for metric in oracle_metrics
        ]
        assert connection.execute(deterministic_sql, [result_id]).fetchall() == oracle_rows
        assert all(
            row[0] is not None
            for row in connection.execute(
                "select calculated_at_utc from window_metrics where result_id = ?", [result_id]
            ).fetchall()
        )
        assert consumed_sources[0] == oracle_source
        assert consumed_sources[1] == oracle_source
        assert consumed_sources[0][2] is consumed_sources[1][2]
        assert consumed_sources[0][3] is consumed_sources[1][3]
    finally:
        connection.close()
        oracle_connection.close()


def test_pair_source_reuse_is_limited_to_one_public_call(tmp_path, monkeypatch) -> None:
    oracle_connection, oracle_result_id = _db(tmp_path / "oracle")
    candidate_connection, candidate_result_id = _db(tmp_path / "candidate")
    windows = (("2026-01-01", "2026-01-05"), ("2026-01-02", "2026-01-05"))
    try:
        oracle_first = tuple(
            get_or_calculate_window(oracle_connection, oracle_result_id, *window) for window in windows
        )
        deterministic_sql = (
            "select " + ", ".join(windows_module._METRIC_COLUMNS) +
            " from window_metrics where result_id = ? order by requested_start_utc"
        )
        oracle_first_rows = oracle_connection.execute(deterministic_sql, [oracle_result_id]).fetchall()
        oracle_connection.execute(
            "update strategy_actions set fee = 2.2 where result_id = ? and action_index = 5", [oracle_result_id]
        )
        oracle_connection.execute(
            "update strategy_equity set wallet = 120, equity = 120 where result_id = ? and sample_index = 4",
            [oracle_result_id],
        )
        oracle_connection.execute("delete from window_metrics where result_id = ?", [oracle_result_id])
        oracle_mutated = tuple(
            get_or_calculate_window(oracle_connection, oracle_result_id, *window) for window in windows
        )
        oracle_mutated_rows = oracle_connection.execute(deterministic_sql, [oracle_result_id]).fetchall()

        counts = {name: 0 for name in ("source", "flat", "calculate", "persist", "cached")}
        originals = {
            "source": windows_module._load_source,
            "flat": windows_module._flat_samples,
            "calculate": windows_module._calculate,
            "persist": windows_module._persist,
            "cached": windows_module._cached,
        }

        def counted_source(*args):
            counts["source"] += 1
            return originals["source"](*args)

        def counted_flat(*args):
            counts["flat"] += 1
            return originals["flat"](*args)

        def counted_calculate(*args, **kwargs):
            counts["calculate"] += 1
            return originals["calculate"](*args, **kwargs)

        def counted_persist(*args):
            counts["persist"] += 1
            return originals["persist"](*args)

        def counted_cached(*args):
            counts["cached"] += 1
            return originals["cached"](*args)

        monkeypatch.setattr(windows_module, "_load_source", counted_source)
        monkeypatch.setattr(windows_module, "_flat_samples", counted_flat)
        monkeypatch.setattr(windows_module, "_calculate", counted_calculate)
        monkeypatch.setattr(windows_module, "_persist", counted_persist)
        monkeypatch.setattr(windows_module, "_cached", counted_cached)

        def deterministic_rows(connection, source_result_id):
            return connection.execute(deterministic_sql, [source_result_id]).fetchall()

        def calculated_at_rows(connection, source_result_id):
            return connection.execute(
                "select calculated_at_utc from window_metrics where result_id = ? order by requested_start_utc",
                [source_result_id],
            ).fetchall()

        candidate_first = get_or_calculate_window_pair(candidate_connection, candidate_result_id, *windows)
        assert candidate_first == oracle_first
        assert deterministic_rows(candidate_connection, candidate_result_id) == oracle_first_rows
        assert all(row[0] is not None for row in calculated_at_rows(candidate_connection, candidate_result_id))
        candidate_connection.execute(
            "update strategy_actions set fee = 2.2 where result_id = ? and action_index = 5", [candidate_result_id]
        )
        candidate_connection.execute(
            "update strategy_equity set wallet = 120, equity = 120 where result_id = ? and sample_index = 4",
            [candidate_result_id],
        )
        candidate_connection.execute("delete from window_metrics where result_id = ?", [candidate_result_id])
        assert candidate_connection.execute("select count(*) from window_metrics").fetchone() == (0,)
        assert candidate_connection.execute(
            "select fee from strategy_actions where result_id = ? and action_index = 5", [candidate_result_id]
        ).fetchone() == (Decimal("2.2"),)
        assert candidate_connection.execute(
            "select wallet, equity from strategy_equity where result_id = ? and sample_index = 4", [candidate_result_id]
        ).fetchone() == (Decimal("120.0"), Decimal("120.0"))
        before_second = counts.copy()

        candidate_second = get_or_calculate_window_pair(candidate_connection, candidate_result_id, *windows)
        assert candidate_second == oracle_mutated
        assert deterministic_rows(candidate_connection, candidate_result_id) == oracle_mutated_rows
        assert all(row[0] is not None for row in calculated_at_rows(candidate_connection, candidate_result_id))
        assert {key: counts[key] - before_second[key] for key in counts} == {
            "source": 1,
            "flat": 2,
            "calculate": 2,
            "persist": 2,
            "cached": 4,
        }
        assert counts["source"] == 2
        assert all(
            second.growth_factor != first.growth_factor and second.fees_pct != first.fees_pct
            for first, second in zip(candidate_first, candidate_second)
        )
    finally:
        oracle_connection.close()
        candidate_connection.close()


def test_scalar_out_of_range_skips_flat_and_persists_readback(tmp_path, monkeypatch) -> None:
    connection, result_id = _db(tmp_path)
    counts = {name: 0 for name in ("flat", "persist", "cached")}
    original_flat = windows_module._flat_samples
    original_persist = windows_module._persist
    original_cached = windows_module._cached

    def counted_flat(*args):
        counts["flat"] += 1
        return original_flat(*args)

    def counted_persist(*args):
        counts["persist"] += 1
        return original_persist(*args)

    def counted_cached(*args):
        counts["cached"] += 1
        return original_cached(*args)

    monkeypatch.setattr(windows_module, "_flat_samples", counted_flat)
    monkeypatch.setattr(windows_module, "_persist", counted_persist)
    monkeypatch.setattr(windows_module, "_cached", counted_cached)
    try:
        observed = get_or_calculate_window(connection, result_id, "2026-01-06", "2026-01-07")
        assert observed.availability_status == "UNAVAILABLE"
        assert observed.unavailable_reason == "OUT_OF_RANGE"
        assert counts == {"flat": 0, "persist": 1, "cached": 2}
        assert connection.execute("select count(*) from window_metrics").fetchone() == (1,)
        assert get_or_calculate_window(connection, result_id, "2026-01-06", "2026-01-07") == observed
    finally:
        connection.close()


@pytest.mark.parametrize("cache_case", ["a_cached", "b_cached", "both_cached", "duplicate_cold"])
def test_pair_cache_matrix_keeps_scalar_work_independent(tmp_path, monkeypatch, cache_case: str) -> None:
    connection, result_id = _db(tmp_path)
    window_a = ("2026-01-01", "2026-01-05")
    window_b = ("2026-01-02", "2026-01-05")
    if cache_case == "a_cached":
        get_or_calculate_window(connection, result_id, *window_a)
    elif cache_case == "b_cached":
        get_or_calculate_window(connection, result_id, *window_b)
    elif cache_case == "both_cached":
        get_or_calculate_window_pair(connection, result_id, window_a, window_b)

    counts = {name: 0 for name in ("source", "flat", "calculate", "persist", "cached")}
    originals = {
        "source": windows_module._load_source,
        "flat": windows_module._flat_samples,
        "calculate": windows_module._calculate,
        "persist": windows_module._persist,
        "cached": windows_module._cached,
    }

    def counted_source(*args):
        counts["source"] += 1
        return originals["source"](*args)

    def counted_flat(*args):
        counts["flat"] += 1
        return originals["flat"](*args)

    def counted_calculate(*args, **kwargs):
        counts["calculate"] += 1
        return originals["calculate"](*args, **kwargs)

    def counted_persist(*args):
        counts["persist"] += 1
        return originals["persist"](*args)

    def counted_cached(*args):
        counts["cached"] += 1
        return originals["cached"](*args)

    monkeypatch.setattr(windows_module, "_load_source", counted_source)
    monkeypatch.setattr(windows_module, "_flat_samples", counted_flat)
    monkeypatch.setattr(windows_module, "_calculate", counted_calculate)
    monkeypatch.setattr(windows_module, "_persist", counted_persist)
    monkeypatch.setattr(windows_module, "_cached", counted_cached)
    try:
        if cache_case == "duplicate_cold":
            observed = get_or_calculate_window_pair(connection, result_id, window_a, window_a)
            expected = {"source": 1, "flat": 1, "calculate": 1, "persist": 1, "cached": 3}
            assert observed[0] == observed[1]
            assert connection.execute("select count(*) from window_metrics").fetchone() == (1,)
        else:
            observed = get_or_calculate_window_pair(connection, result_id, window_a, window_b)
            expected = {
                "a_cached": {"source": 1, "flat": 1, "calculate": 1, "persist": 1, "cached": 3},
                "b_cached": {"source": 1, "flat": 1, "calculate": 1, "persist": 1, "cached": 3},
                "both_cached": {"source": 0, "flat": 0, "calculate": 0, "persist": 0, "cached": 2},
            }[cache_case]
            assert observed[0].availability_status == observed[1].availability_status == "AVAILABLE"
        assert counts == expected
    finally:
        connection.close()


def test_pair_error_precedence_and_autocommit_are_preserved(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    valid_window = ("2026-01-01", "2026-01-05")
    try:
        with pytest.raises(windows_module.PerformanceV2WindowsError, match="ISO-8601"):
            get_or_calculate_window_pair(connection, result_id, ("bad", "2026-01-05"), valid_window)
        assert connection.execute("select count(*) from window_metrics").fetchone() == (0,)

        with pytest.raises(windows_module.PerformanceV2WindowsError, match="ISO-8601"):
            get_or_calculate_window_pair(connection, result_id, valid_window, ("bad", "2026-01-05"))
        assert connection.execute("select count(*) from window_metrics").fetchone() == (1,)
        connection.execute("delete from window_metrics where result_id = ?", [result_id])

        with pytest.raises(ValueError, match="non-empty"):
            get_or_calculate_window_pair(connection, result_id, ("bad", "2026-01-05"), valid_window, calculator_version=" ")
        assert connection.execute("select count(*) from window_metrics").fetchone() == (0,)

        with pytest.raises(windows_module.PerformanceV2WindowsError, match="unknown result_id"):
            get_or_calculate_window_pair(connection, result_id + 1000, valid_window, ("bad", "2026-01-05"))
        with pytest.raises(ValueError, match="each window"):
            get_or_calculate_window_pair(None, result_id, ("2026-01-01",), valid_window)
    finally:
        connection.close()


def test_pair_b_error_rolls_back_a_only_when_caller_owns_transaction(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    try:
        connection.execute("begin transaction")
        with pytest.raises(windows_module.PerformanceV2WindowsError, match="ISO-8601"):
            get_or_calculate_window_pair(
                connection, result_id, ("2026-01-01", "2026-01-05"), ("bad", "2026-01-05")
            )
        connection.execute("rollback")
        assert connection.execute("select count(*) from window_metrics").fetchone() == (0,)
    finally:
        connection.close()


def test_flat_start_boundary_close_is_excluded_from_window_facts(tmp_path) -> None:
    connection, result_id = _db(tmp_path)
    try:
        window = get_or_calculate_window(connection, result_id, "2026-01-01", "2026-01-05")
        assert window.effective_start_utc == datetime(2026, 1, 3, tzinfo=UTC)
        assert window.trade_count == 1
        assert window.holding_seconds == Decimal("43200.000000000000")
    finally:
        connection.close()


def test_partial_close_then_increase_stays_one_position_episode() -> None:
    actions = tuple(
        _Action(index, datetime(2026, 1, 1, hour=index, tzinfo=UTC), kind, Decimal(post_size), Decimal(pnl), Decimal("0"))
        for index, (kind, post_size, pnl) in enumerate(
            (("opened", "1", "0"), ("decreased", "0.5", "2"), ("increased", "1", "0"), ("closed", "0", "3"))
        )
    )
    trips = _round_trips(actions)
    assert len(trips) == 1
    assert len(trips[0].entries) == 2
    assert len(trips[0].realisations) == 2


def test_signed_short_position_uses_the_same_round_trip_boundaries() -> None:
    actions = tuple(
        _Action(index, datetime(2026, 1, 1, hour=index, tzinfo=UTC), kind, Decimal(post_size), Decimal(pnl), Decimal("0"))
        for index, (kind, post_size, pnl) in enumerate(
            (("opened", "-1", "0"), ("decreased", "-0.5", "2"), ("increased", "-1", "0"), ("closed", "0", "3"))
        )
    )
    trips = _round_trips(actions)
    assert len(trips) == 1
    assert len(trips[0].entries) == 2
    assert len(trips[0].realisations) == 2


def test_geometric_comparison_rejects_zero_negative_and_unavailable_inputs() -> None:
    def window(growth: str, log_return: str) -> WindowMetrics:
        return WindowMetrics(
            result_id=1,
            requested_start_utc=datetime(2026, 1, 1, tzinfo=UTC),
            requested_end_utc=datetime(2026, 1, 2, tzinfo=UTC),
            metrics_version=METRICS_VERSION,
            effective_start_utc=datetime(2026, 1, 1, tzinfo=UTC),
            effective_end_utc=datetime(2026, 1, 2, tzinfo=UTC),
            availability_status="AVAILABLE",
            unavailable_reason=None,
            growth_factor=Decimal(growth),
            return_pct=Decimal("0"),
            daily_log_return=Decimal(log_return),
            daily_growth_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            return_dd_ratio=None,
            fees_pct=Decimal("0"),
            profit_factor=None,
            trade_count=1,
            win_rate_pct=Decimal("100"),
        )

    positive = window("2", "0.6931471805599453")
    better = window("4", "1.3862943611198906")
    comparison = compare_window_pair_geometrically(positive, better)
    assert comparison.status == "AVAILABLE"
    assert comparison.growth_factor_ratio == Decimal("2")
    assert comparison.log_return_ratio > Decimal("1.99")
    assert compare_window_pair_geometrically(window("0", "0"), positive).status == "UNDEFINED_ZERO_BASELINE"
    assert compare_window_pair_geometrically(positive, window("-1", "0")).status == "UNDEFINED_NON_POSITIVE_INPUT"
    unavailable = WindowMetrics.unavailable(1, positive.requested_start_utc, positive.requested_end_utc, "NO_TRADES", METRICS_VERSION)
    assert compare_window_pair_geometrically(unavailable, positive).status == "WINDOW_NOT_AVAILABLE"
