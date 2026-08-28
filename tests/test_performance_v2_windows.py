from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import duckdb
import pytest

from mrs3.performance_v2_store import initialize_performance_v2
from mrs3.performance_v2_windows import (
    METRICS_VERSION,
    WindowMetrics,
    compare_window_pair_geometrically,
    get_or_calculate_window,
    get_or_calculate_window_pair,
)


UTC = timezone.utc


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
        "insert into strategy_actions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", actions
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
        second = get_or_calculate_window(second_connection, second_id, "2026-01-01", "2026-01-05")
        assert first.trade_count == second.trade_count == 2
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
