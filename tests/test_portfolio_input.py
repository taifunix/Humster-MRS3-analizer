from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import duckdb
import pytest

from mrs3.performance_v2_selection import (
    SelectionConfig,
    _selection_windows,
    parse_selection_request,
    prepare_selection_window_cache,
)
from mrs3.panel_portfolio import _plain
from mrs3.portfolio import input as portfolio_input
from mrs3.portfolio import reports as portfolio_reports
from mrs3.portfolio.input import (
    SOURCE_SNAPSHOT_UNAVAILABLE,
    DecisionCampaign,
    apply_finalist_cutoff,
    PortfolioInputError,
    fresh_decision_campaign,
    read_and_select_finalists,
    read_current_finalists,
    read_performance_snapshot,
    preparation_cache_key,
    prepare_weighted_input,
    resolve_common_pretest_period,
    _cycle_records,
    _PreparedResultRow,
)
from mrs3.portfolio.store import PortfolioStore
from tests.test_performance_v2_selection import _candidate_db
from mrs3.portfolio.reports import PositionCycle
from mrs3.performance_v2_optimizer import (
    OptimizerSourceInput,
    build_prepared_input,
    prepare_current_optimizer_inputs,
)


REQUEST = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
IDENTITY = {
    "optimizer_config_hash": "config-hash-v1",
    "algorithm_versions": {"sizing": "sizing-v1", "ranking": "ranking-v1"},
    "seed": 17,
    "upstream_selection_window": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-31T00:00:00Z", "identity": "window-1"},
    "account_currency": "USDT",
    "read_at_utc": datetime(2026, 2, 1, tzinfo=timezone.utc),
}


def _add_review(
    connection: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    review_id: str,
    symbol: str,
    side: str,
    strategy_id: int,
    result_id: int,
    user_status: str,
    selection_time: datetime,
) -> None:
    instance_id = connection.execute(
        "select value from schema_info where key = 'database_instance_id'"
    ).fetchone()[0]
    connection.execute(
        """insert into selection_runs values
           (?, ?, ?, ?, 'test-selection-v1', '{}', ?, '{}', ?, 1, 1, 1, 1, ?, ?)""",
        [run_id, instance_id, symbol, side, f"request-hash-{run_id}", f"config-hash-{run_id}", f"workbook-hash-{run_id}", selection_time],
    )
    connection.execute(
        """insert into selection_results values
           (?, ?, ?, 'FINALIST', 1, 1, 'selected', '{}', null, false, '{}')""",
        [run_id, strategy_id, result_id],
    )
    connection.execute(
        "insert into selection_review_imports values (?, ?, ?, ?, 1)",
        [review_id, run_id, f"review-hash-{review_id}", selection_time],
    )
    connection.execute(
        "insert into selection_review_rows values (?, ?, ?, 1, null, 'selected')",
        [review_id, strategy_id, user_status],
    )


def _database(tmp_path: Path) -> Path:
    connection = _candidate_db(tmp_path)
    strategy_id, result_id = connection.execute(
        "select strategy_id, current_result_id from strategies"
    ).fetchone()
    connection.execute(
        """update strategy_results set reported_start_utc = report_start_utc,
           reported_end_utc = report_end_utc, effective_start_utc = report_start_utc,
           effective_end_utc = report_end_utc, sizing_use_upnl = true,
           sizing_use_frozen_balance = true, sizing_use_fix = false,
           sizing_balance_percentage_long = 100, sizing_risk_long = 1,
           sizing_max_balance = 0 where result_id = ?""",
        [result_id],
    )
    connection.execute("update strategy_actions set price = 10, cost = 10")
    selection_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _add_review(
        connection,
        run_id="run-default",
        review_id="review-default",
        symbol="BTCUSDT",
        side="LONG",
        strategy_id=strategy_id,
        result_id=result_id,
        user_status="FINALIST",
        selection_time=selection_time,
    )
    connection.close()
    prepare_current_optimizer_inputs(str(tmp_path / "strategy_performance.duckdb"), [result_id])
    return tmp_path / "strategy_performance.duckdb"


def _snapshot(database: Path, request=REQUEST, **overrides):
    values = {**IDENTITY, **overrides}
    return read_performance_snapshot(database, request, **values)


def test_snapshot_is_read_only_and_cache_miss_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("delete from window_metrics")
        before = connection.execute("select count(*) from window_metrics").fetchone()[0]

    calls: list[bool] = []
    original = portfolio_input.load_selection_candidates

    def checked(connection, request, *args, **kwargs):
        calls.append(kwargs.get("cache_only"))
        return original(connection, request, *args, **kwargs)

    monkeypatch.setattr(portfolio_input, "load_selection_candidates", checked)
    snapshot = _snapshot(database, [REQUEST])

    assert calls == [True]
    assert len(snapshot.window_metrics) >= 1
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("select count(*) from window_metrics").fetchone()[0] == before
        with pytest.raises(duckdb.Error):
            connection.execute("insert into window_metrics select * from window_metrics limit 0")


def test_snapshot_digest_is_invariant_to_cache_population_and_stale_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    prepare_selection_window_cache(database, REQUEST, SelectionConfig(), workers=1)
    with duckdb.connect(str(database)) as connection:
        result_id, report_start, report_end = connection.execute(
            "select s.current_result_id, r.report_start_utc, r.report_end_utc from strategies s join strategy_results r on r.result_id = s.current_result_id"
        ).fetchone()
        windows = _selection_windows(report_start, report_end, SelectionConfig())
        numeric_columns = (
            "growth_factor", "return_pct", "daily_log_return", "daily_growth_pct", "max_drawdown_pct",
            "return_dd_ratio", "fees_pct", "profit_factor", "trade_count", "win_rate_pct",
            "holding_seconds", "time_in_market_pct",
        )
        assignment = ", ".join(
            f"{column} = case when {column} is null then null else {column} + 1 end"
            for column in numeric_columns
        )
        assert connection.execute(
            "select count(*) from window_metrics where result_id = ?", [result_id]
        ).fetchone()[0] == len(windows)
        for start, end in windows:
            assert connection.execute(
                "select count(*) from window_metrics where result_id = ? and requested_start_utc = ? and requested_end_utc = ?",
                [result_id, start, end],
            ).fetchone()[0] == 1
            connection.execute(
                f"update window_metrics set {assignment} where result_id = ? and requested_start_utc = ? and requested_end_utc = ?",
                [result_id, start, end],
            )
        connection.execute(
            """insert into window_metrics
               select result_id, requested_start_utc - interval '1 day', requested_end_utc - interval '1 day',
                      metrics_version, effective_start_utc, effective_end_utc, availability_status,
                      unavailable_reason, growth_factor, return_pct, daily_log_return, daily_growth_pct,
                      max_drawdown_pct, return_dd_ratio, fees_pct, profit_factor, trade_count, win_rate_pct,
                      holding_seconds, time_in_market_pct, calculated_at_utc
                 from window_metrics limit 1"""
        )
    cached = _snapshot(database, [REQUEST])
    with duckdb.connect(str(database)) as connection:
        connection.execute("delete from window_metrics")
    uncached = _snapshot(database, [REQUEST])

    assert cached.digest == uncached.digest
    assert cached.canonical_json == uncached.canonical_json


def test_overlapping_candidate_rows_are_deduplicated_by_strategy_and_result(tmp_path: Path) -> None:
    database = _database(tmp_path)
    snapshot = _snapshot(database, [REQUEST, REQUEST])

    assert len(snapshot.candidates) == 1


def test_snapshot_admits_only_exact_current_finalists(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("update selection_review_rows set user_status = 'RESERVE'")

    snapshot = _snapshot(database, [REQUEST])

    assert snapshot.candidates == ()


def test_snapshot_keeps_source_candidate_schema_separate_from_review_admission(tmp_path: Path) -> None:
    snapshot = _snapshot(_database(tmp_path), [REQUEST])

    assert snapshot.candidates
    assert "user_status" not in snapshot.candidates[0]
    assert "selection_run_id" not in snapshot.candidates[0]
    assert "review_import_id" not in snapshot.candidates[0]


def test_snapshot_preserves_admission_lineage_and_digest_tracks_lineage(tmp_path: Path) -> None:
    database = _database(tmp_path)
    snapshot = _snapshot(database, [REQUEST])

    lineage = [dict(row) for row in snapshot.provenance["admission_lineage"]]
    assert lineage == [{
        "symbol": "BTCUSDT",
        "side": "LONG",
        "selection_run_id": "run-default",
        "review_import_id": "review-default",
    }]

    with duckdb.connect(str(database)) as connection:
        strategy_id = connection.execute("select strategy_id from strategies").fetchone()[0]
        connection.execute(
            "insert into selection_review_imports values ('review-new', 'run-default', 'review-hash-new', ?, 1)",
            [datetime(2026, 1, 2, tzinfo=timezone.utc)],
        )
        connection.execute(
            "insert into selection_review_rows values ('review-new', ?, 'FINALIST', 1, null, 'selected')",
            [strategy_id],
        )

    changed = _snapshot(database, [REQUEST])

    assert changed.digest != snapshot.digest
    assert dict(changed.provenance["admission_lineage"][0])["review_import_id"] == "review-new"


def _replace_with_unconstrained_copy(connection: duckdb.DuckDBPyConnection, table: str) -> None:
    copy = f"{table}_copy"
    connection.execute(f"create table {copy} as select * from {table}")
    connection.execute(f"drop table {table}")
    connection.execute(f"alter table {copy} rename to {table}")


def test_snapshot_fails_closed_on_duplicate_selection_result_identity(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        _replace_with_unconstrained_copy(connection, "selection_results")
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        connection.execute(
            "insert into selection_results values ('run-default', ?, ?, 'FINALIST', 9, 1, 'conflict', '{}', null, false, '{}')",
            [strategy_id, result_id + 1],
        )

    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST])

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


def test_snapshot_ignores_historical_selection_for_removed_strategy(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            """insert into selection_results values
               ('run-default', 999, 999, 'FILTERED', null, null, 'missing', '{}', null, false, '{}')"""
        )

    assert len(_snapshot(database, [REQUEST]).candidates) == 1


def test_snapshot_fails_closed_on_duplicate_review_row_identity(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        _replace_with_unconstrained_copy(connection, "selection_review_rows")
        strategy_id = connection.execute("select strategy_id from strategies").fetchone()[0]
        connection.execute(
            "insert into selection_review_rows values ('review-default', ?, 'RESERVE', 1, null, 'conflict')",
            [strategy_id],
        )

    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST])

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


def test_snapshot_fails_closed_on_duplicate_durable_row_in_older_import(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        _replace_with_unconstrained_copy(connection, "selection_review_rows")
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        instance_id = connection.execute(
            "select value from schema_info where key = 'database_instance_id'"
        ).fetchone()[0]
        connection.execute(
            """insert into selection_runs values
               ('run-older', ?, 'BTCUSDT', 'LONG', 'test-selection-v1', '{}',
                'request-hash-older', '{}', 'config-hash-older', 1, 1, 1, 1,
                'workbook-hash-older', ?)""",
            [instance_id, datetime(2025, 12, 31, tzinfo=timezone.utc)],
        )
        connection.execute(
            "insert into selection_results values ('run-older', ?, ?, 'FINALIST', 1, 1, 'older', '{}', null, false, '{}')",
            [strategy_id, result_id],
        )
        connection.execute(
            "insert into selection_review_imports values ('review-older', 'run-older', 'review-hash-older', ?, 2)",
            [datetime(2026, 1, 2, tzinfo=timezone.utc)],
        )
        connection.executemany(
            "insert into selection_review_rows values ('review-older', ?, 'RESERVE', 1, null, 'duplicate')",
            [(strategy_id,), (strategy_id,)],
        )

    with pytest.raises(PortfolioInputError) as error:
        read_current_finalists(database, [("BTCUSDT", "LONG")], False)

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


def test_snapshot_scopes_review_status_by_pair_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path)
    eth_request = parse_selection_request({"symbol": "ETHUSDT", "side": "SHORT", "stages": []})
    with duckdb.connect(str(database)) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        selection_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
        _add_review(
            connection,
            run_id="run-eth",
            review_id="review-eth",
            symbol="ETHUSDT",
            side="SHORT",
            strategy_id=strategy_id,
            result_id=result_id,
            user_status="RESERVE",
            selection_time=selection_time,
        )

    original = portfolio_input.load_selection_candidates
    source_frame = None

    def pair_scoped_loader(connection, request, *args, **kwargs):
        nonlocal source_frame
        if request.symbol == "BTCUSDT":
            source_frame = original(connection, request, *args, **kwargs)
            return source_frame
        clone = source_frame.copy()
        clone["symbol"] = "ETHUSDT"
        clone["side"] = "SHORT"
        return clone

    monkeypatch.setattr(portfolio_input, "load_selection_candidates", pair_scoped_loader)
    snapshot = _snapshot(database, [REQUEST, eth_request])

    assert [candidate["symbol"] for candidate in snapshot.candidates] == ["BTCUSDT"]


def test_snapshot_excludes_stale_non_finalist_alongside_valid_finalist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path)
    eth_request = parse_selection_request({"symbol": "ETHUSDT", "side": "SHORT", "stages": []})
    with duckdb.connect(str(database)) as connection:
        strategy_id, _ = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        selection_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
        _add_review(
            connection,
            run_id="run-eth-stale",
            review_id="review-eth-stale",
            symbol="ETHUSDT",
            side="SHORT",
            strategy_id=strategy_id,
            result_id=999,
            user_status="RESERVE",
            selection_time=selection_time,
        )

    original = portfolio_input.load_selection_candidates
    source_frame = None

    def stale_pair_loader(connection, request, *args, **kwargs):
        nonlocal source_frame
        if request.symbol == "BTCUSDT":
            source_frame = original(connection, request, *args, **kwargs)
            return source_frame
        clone = source_frame.copy()
        clone["symbol"] = "ETHUSDT"
        clone["side"] = "SHORT"
        return clone

    monkeypatch.setattr(portfolio_input, "load_selection_candidates", stale_pair_loader)
    snapshot = _snapshot(database, [REQUEST, eth_request])

    assert [candidate["symbol"] for candidate in snapshot.candidates] == ["BTCUSDT"]


def test_snapshot_ignores_when_current_review_is_missing(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("delete from selection_review_rows")

    assert _snapshot(database, [REQUEST]).candidates == ()


def test_read_current_finalists_ignores_later_auto_only_review_row(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        strategy_id = connection.execute("select strategy_id from strategies").fetchone()[0]
        _replace_with_unconstrained_copy(connection, "selection_review_rows")
        connection.execute(
            "insert into selection_review_imports values ('review-auto', 'run-default', 'review-hash-auto', ?, 1)",
            [datetime(2026, 1, 2, tzinfo=timezone.utc)],
        )
        connection.execute(
            "insert into selection_review_rows values ('review-auto', ?, null, null, null, 'automatic-only')",
            [strategy_id],
        )

    row = read_current_finalists(database, [("BTCUSDT", "LONG")], False)[0]

    assert row["user_status"] == "FINALIST"
    assert row["review_import_id"] == "review-default"


def test_read_current_finalists_preserves_untouched_review_in_later_partial_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        first_strategy_id, first_result_id = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        second_strategy_id = connection.execute(
            """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
               order_count, analysis_run_id, candidate_identity, lifecycle_status,
               created_at_utc, updated_at_utc) values
               ('beta', 'BTCUSDT', 'LONG', '1h', 3, 1, 'run', 'candidate-beta', 'ACTIVE', ?, ?)
               returning strategy_id""",
            [datetime(2026, 1, 1, tzinfo=timezone.utc)] * 2,
        ).fetchone()[0]
        second_result_id = connection.execute(
            """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
               commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
               max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
               values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 2, 2, ?)
               returning result_id""",
            [second_strategy_id, datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 31, tzinfo=timezone.utc), datetime(2026, 1, 1, tzinfo=timezone.utc)],
        ).fetchone()[0]
        connection.execute(
            "update strategies set current_result_id = ? where strategy_id = ?",
            [second_result_id, second_strategy_id],
        )
        connection.execute(
            "insert into strategy_orders values (?, 1, 7, .995, 125, 1, 'run', 'P1', 8)",
            [second_strategy_id],
        )
        connection.execute(
            "insert into selection_results values ('run-default', ?, ?, 'FINALIST', 1, 1, 'selected', '{}', null, false, '{}')",
            [second_strategy_id, second_result_id],
        )
        connection.execute(
            "insert into selection_review_rows values ('review-default', ?, 'FINALIST', 2, null, 'selected')",
            [second_strategy_id],
        )
        instance_id = connection.execute(
            "select value from schema_info where key = 'database_instance_id'"
        ).fetchone()[0]
        connection.execute(
            """insert into selection_runs values
               ('run-partial', ?, 'BTCUSDT', 'LONG', 'test-selection-v1', '{}',
                'request-hash-partial', '{}', 'config-hash-partial', 2, 2, 2, 2,
                'workbook-hash-partial', ?)""",
            [instance_id, datetime(2026, 1, 2, tzinfo=timezone.utc)],
        )
        connection.executemany(
            "insert into selection_results values ('run-partial', ?, ?, 'FINALIST', 1, 1, 'selected', '{}', null, false, '{}')",
            [(first_strategy_id, first_result_id), (second_strategy_id, second_result_id)],
        )
        connection.execute(
            "insert into selection_review_imports values ('review-partial', 'run-partial', 'review-hash-partial', ?, 1)",
            [datetime(2026, 1, 3, tzinfo=timezone.utc)],
        )
        connection.execute(
            "insert into selection_review_rows values ('review-partial', ?, 'RESERVE', 1, null, 'partial')",
            [first_strategy_id],
        )

    rows = read_current_finalists(database, [("BTCUSDT", "LONG")], False)

    assert [(row["strategy_id"], row["review_import_id"]) for row in rows] == [(second_strategy_id, "review-default")]


def test_read_current_finalists_uses_review_import_id_for_equal_imported_at(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        strategy_id = connection.execute("select strategy_id from strategies").fetchone()[0]
        connection.execute(
            "insert into selection_review_imports values ('review-z', 'run-default', 'review-hash-z', ?, 1)",
            [datetime(2026, 1, 1, tzinfo=timezone.utc)],
        )
        connection.execute(
            "insert into selection_review_rows values ('review-z', ?, 'FINALIST', 3, null, 'tie-break')",
            [strategy_id],
        )

    row = read_current_finalists(database, [("BTCUSDT", "LONG")], False)[0]

    assert row["review_import_id"] == "review-z"
    assert row["user_rank"] == 3


def test_snapshot_fails_closed_when_finalist_period_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path)

    original = portfolio_input.load_selection_candidates

    def missing_period(connection, request, *args, **kwargs):
        frame = original(connection, request, *args, **kwargs)
        frame["report_start_utc"] = None
        return frame

    monkeypatch.setattr(portfolio_input, "load_selection_candidates", missing_period)

    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST])

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


def test_snapshot_fails_closed_when_finalist_candidate_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    original = portfolio_input.load_selection_candidates

    def missing_candidate(*args, **kwargs):
        return original(*args, **kwargs).iloc[0:0]

    monkeypatch.setattr(portfolio_input, "load_selection_candidates", missing_candidate)

    with pytest.raises(PortfolioInputError, match="current finalist candidate is unavailable") as error:
        _snapshot(database, [REQUEST])

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


def test_snapshot_fails_closed_when_current_run_timestamps_tie(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        instance_id = connection.execute(
            "select value from schema_info where key = 'database_instance_id'"
        ).fetchone()[0]
        connection.execute(
            """insert into selection_runs values
               ('run-tie', ?, 'BTCUSDT', 'LONG', 'test-selection-v1', '{}',
                'request-hash-2', '{}', 'config-hash-2', 1, 1, 1, 1, 'workbook-hash-2', ?)""",
            [instance_id, datetime(2026, 1, 1, tzinfo=timezone.utc)],
        )

    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST])

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


def test_snapshot_ignores_current_review_timestamp_tie(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "insert into selection_review_imports values ('review-tie', 'run-default', 'review-hash-tie', ?, 1)",
            [datetime(2026, 1, 1, tzinfo=timezone.utc)],
        )

    assert len(_snapshot(database, [REQUEST]).candidates) == 1


def test_snapshot_uses_review_import_id_to_break_durable_review_timestamp_tie(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        connection.execute(
            "insert into selection_review_imports values ('review-tie', 'run-default', 'review-hash-tie', ?, 1)",
            [datetime(2026, 1, 1, tzinfo=timezone.utc)],
        )
        connection.execute(
            "insert into selection_review_rows values ('review-tie', ?, 'RESERVE', 1, null, 'tie')",
            [strategy_id],
        )
        instance_id = connection.execute(
            "select value from schema_info where key = 'database_instance_id'"
        ).fetchone()[0]
        connection.execute(
            """insert into selection_runs values
               ('run-new', ?, 'BTCUSDT', 'LONG', 'test-selection-v1', '{}',
                'request-hash-new', '{}', 'config-hash-new', 1, 1, 1, 1,
                'workbook-hash-new', ?)""",
            [instance_id, datetime(2026, 1, 2, tzinfo=timezone.utc)],
        )
        connection.execute(
            """insert into selection_results values
               ('run-new', ?, ?, 'FINALIST', 1, 1, 'new', '{}', null, false, '{}')""",
            [strategy_id, result_id],
        )

    assert _snapshot(database, [REQUEST]).candidates == ()


def test_identical_requests_collapse_but_conflicting_stages_fail_closed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    staged = parse_selection_request({
        "symbol": "BTCUSDT",
        "side": "LONG",
        "stages": [{"id": "ab_deterioration", "enabled": True, "scope": "pair_side"}],
    })

    snapshot = _snapshot(database, [REQUEST, REQUEST])
    assert snapshot.requests == (REQUEST,)

    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST, staged])
    assert error.value.code == "INVALID_REQUEST"


def test_value_equal_requests_collapse_to_single_canonical_request(tmp_path: Path) -> None:
    database = _database(tmp_path)
    raw = {"symbol": "BTCUSDT", "side": "LONG", "stages": []}
    independently_parsed = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})

    duplicate = _snapshot(database, [raw, independently_parsed])
    single = _snapshot(database, [REQUEST])

    assert duplicate.requests == (independently_parsed,)
    assert duplicate.canonical_json == single.canonical_json
    assert duplicate.digest == single.digest


def test_non_empty_stage_request_is_canonicalized_and_changes_digest(tmp_path: Path) -> None:
    database = _database(tmp_path)
    staged = parse_selection_request({
        "symbol": "BTCUSDT",
        "side": "LONG",
        "stages": [{"id": "ab_deterioration", "enabled": True, "scope": "pair_side"}],
    })

    staged_snapshot = _snapshot(database, [staged])
    free_snapshot = _snapshot(database, [REQUEST])
    request_payload = staged_snapshot.payload["requests"].items[0]
    stages = request_payload["stages"]

    assert stages.type_tag == "stage_sequence"
    assert stages.unit_tag == "1"
    assert stages.value["0"]["id"] == "ab_deterioration"
    assert staged_snapshot.digest != free_snapshot.digest


def test_snapshot_assembly_wraps_generic_errors_and_preserves_coded_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path)

    def generic_failure(_envelope):
        raise ValueError("canonical failure")

    monkeypatch.setattr(portfolio_input, "canonical_json_v1", generic_failure)
    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST])
    assert error.value.code == "SNAPSHOT_ASSEMBLY_FAILED"

    def coded_failure(_envelope):
        raise PortfolioInputError("bad source", code="INVALID_SOURCE_VALUE")

    monkeypatch.setattr(portfolio_input, "canonical_json_v1", coded_failure)
    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST])
    assert error.value.code == "INVALID_SOURCE_VALUE"


def test_snapshot_contains_full_candidate_source_and_digest(tmp_path: Path) -> None:
    snapshot = _snapshot(_database(tmp_path), [REQUEST])

    assert snapshot.source_schema_version == "5"
    assert snapshot.database_kind == "unified_performance_v2"
    assert snapshot.database_instance_id
    assert snapshot.candidates[0]["result_id"]
    assert snapshot.actions and snapshot.equity
    assert snapshot.digest == snapshot.canonical_digest
    assert snapshot.canonical_json.startswith('{')
    assert snapshot.canonical_content == snapshot.canonical_json
    assert __import__('hashlib').sha256(snapshot.canonical_content.encode('utf-8')).hexdigest() == snapshot.digest
    assert len(snapshot.digest) == 64
    assert snapshot.decision_replay == "AVAILABLE"
    assert snapshot.tick_replay == "UNAVAILABLE"


def test_snapshot_identity_and_nested_window_are_immutable() -> None:
    from mrs3.portfolio.input import SnapshotIdentity

    window = {"window": {"start": "a", "tags": ["initial"], "markers": {"first"}}}
    identity = SnapshotIdentity("hash", {"algo": "v1"}, 1, window, "USDT")
    window["window"]["start"] = "changed"
    window["window"]["tags"].append("changed")
    window["window"]["markers"].add("changed")
    with pytest.raises(TypeError):
        identity.algorithm_versions["new"] = "v2"
    with pytest.raises(TypeError):
        identity.upstream_selection_window["window"]["start"] = "b"
    assert identity.upstream_selection_window["window"]["start"] == "a"
    assert identity.upstream_selection_window["window"]["tags"] == ("initial",)
    assert identity.upstream_selection_window["window"]["markers"] == ("first",)


def test_canonical_content_persists_and_replays_after_source_deletion(tmp_path: Path) -> None:
    database = _database(tmp_path)
    snapshot = _snapshot(database, [REQUEST])
    portfolio = PortfolioStore(tmp_path / "portfolio.duckdb")
    portfolio.create_campaign("campaign-1", snapshot.digest, snapshot.canonical_content)
    portfolio.save_campaign_snapshot("snapshot-1", "campaign-1", snapshot.digest, snapshot.canonical_content)
    database.unlink()

    stored = portfolio.get_campaign_snapshot("snapshot-1")
    assert stored is not None
    assert stored["canonical_digest"] == snapshot.digest
    assert stored["content"] == snapshot.canonical_content
    assert snapshot.decision_replay == "AVAILABLE"
    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST])
    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


def test_identity_and_read_time_are_digest_inputs(tmp_path: Path) -> None:
    database = _database(tmp_path)
    base = _snapshot(database, [REQUEST])
    variants = (
        {"optimizer_config_hash": "config-hash-v2"},
        {"algorithm_versions": {"sizing": "sizing-v2", "ranking": "ranking-v1"}},
        {"seed": 18},
        {"upstream_selection_window": {"start": "2026-01-02T00:00:00Z", "end": "2026-01-31T00:00:00Z", "identity": "window-2"}},
        {"read_at_utc": datetime(2026, 2, 2, tzinfo=timezone.utc)},
    )

    assert all(_snapshot(database, [REQUEST], **variant).digest != base.digest for variant in variants)


def test_selection_provenance_rows_have_typed_identities(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        strategy_id, result_id, instance_id = connection.execute(
            "select s.strategy_id, s.current_result_id, i.value from strategies s cross join schema_info i where i.key = 'database_instance_id'"
        ).fetchone()
        created = datetime(2026, 2, 1, tzinfo=timezone.utc)
        connection.execute(
            "insert into selection_runs values (?, ?, 'BTCUSDT', 'LONG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["run-1", instance_id, "selection-v1", "{}", "request-hash", "{}", "config-hash", 1, 1, 1, 1, "workbook-hash", created],
        )
        connection.execute(
            "insert into selection_results values (?, ?, ?, 'FINALIST', 1, 1, 'selected', '{}', null, false, '{}')",
            ["run-1", strategy_id, result_id],
        )
        connection.execute(
            "insert into selection_review_imports values (?, ?, ?, ?, ?)",
            ["review-1", "run-1", "review-hash", created, 1],
        )
        connection.execute(
            "insert into selection_review_rows values (?, ?, 'FINALIST', 1, null, 'ok')",
            ["review-1", strategy_id],
        )
        connection.execute(
            "insert into strategy_tags values (?, 'RETEST', 'test', 'ref-1', ?)",
            [strategy_id, created],
        )
    snapshot = _snapshot(database, [REQUEST])

    assert all(snapshot.payload["provenance"][name].items and all("identity" in row for row in snapshot.payload["provenance"][name].items) for name in snapshot.payload["provenance"])


def test_series_keep_source_ordinals_per_result_identity(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        strategy_id = connection.execute(
            "insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len, order_count, analysis_run_id, candidate_identity, lifecycle_status, created_at_utc, updated_at_utc) values ('beta', 'BTCUSDT', 'LONG', '1h', 3, 1, 'run-b', 'candidate-b', 'ACTIVE', ?, ?) returning strategy_id",
            [start, start],
        ).fetchone()[0]
        result_id = connection.execute(
            "insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange, commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc) values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 2, 2, ?) returning result_id",
            [strategy_id, start, datetime(2026, 1, 31, tzinfo=timezone.utc), start],
        ).fetchone()[0]
        connection.execute(
            """update strategy_results set reported_start_utc = report_start_utc,
               reported_end_utc = report_end_utc, effective_start_utc = report_start_utc,
               effective_end_utc = report_end_utc where result_id = ?""",
            [result_id],
        )
        connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
        connection.executemany(
            "insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size, post_size, post_side, pnl, fee, balance, raw_action_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(result_id, 0, start, "BTCUSDT", 1, "closed", 1, 0, "", 0, 0, 100, None), (result_id, 1, datetime(2026, 1, 2, tzinfo=timezone.utc), "BTCUSDT", 1, "opened", 1, 1, "long", 0, 0, 100, None), (result_id, 2, datetime(2026, 1, 3, tzinfo=timezone.utc), "BTCUSDT", 1, "closed", 1, 0, "", 10, 2, 110, None)],
        )
        connection.executemany(
            "insert into strategy_equity values (?, ?, ?, ?, ?)",
            [(result_id, 0, start, 100, 100), (result_id, 1, datetime(2026, 1, 2, tzinfo=timezone.utc), 100, 100), (result_id, 2, datetime(2026, 1, 3, tzinfo=timezone.utc), 110, 110)],
        )
        connection.execute(
            "insert into selection_results values ('run-default', ?, ?, 'FINALIST', 2, 2, 'selected', '{}', null, false, '{}')",
            [strategy_id, result_id],
        )
        connection.execute(
            "insert into selection_review_rows values ('review-default', ?, 'FINALIST', 2, null, 'selected')",
            [strategy_id],
        )
    snapshot = _snapshot(database, [REQUEST])

    action_groups = snapshot.payload["actions"].items
    assert len(action_groups) == 2
    assert all([row["source_ordinal"] for row in group["items"].items] == [0, 1, 2] for group in action_groups)


def test_old_snapshot_replays_after_source_mutation_and_new_digest(tmp_path: Path) -> None:
    database = _database(tmp_path)
    old = _snapshot(database, [REQUEST])
    with duckdb.connect(str(database)) as connection:
        connection.execute("update strategy_results set total_pnl = total_pnl + 1")
    new = _snapshot(database, [REQUEST])

    assert old.digest != new.digest
    assert old.candidates[0]["total_pnl"] != new.candidates[0]["total_pnl"]
    assert old.decision_replay == "AVAILABLE"


def test_old_snapshot_replays_after_source_series_deletion(tmp_path: Path) -> None:
    database = _database(tmp_path)
    old = _snapshot(database, [REQUEST])
    with duckdb.connect(str(database)) as connection:
        connection.execute("delete from strategy_actions")
        connection.execute("delete from strategy_equity")

    assert old.actions and old.equity
    assert old.decision_replay == "AVAILABLE"


def test_current_result_replacement_same_strategy_changes_input_digest(tmp_path: Path) -> None:
    database = _database(tmp_path)
    old = _snapshot(database, [REQUEST])
    with duckdb.connect(str(database)) as connection:
        strategy_id, result_id, start, end = connection.execute(
            "select s.strategy_id, s.current_result_id, r.report_start_utc, r.report_end_utc from strategies s join strategy_results r on r.result_id = s.current_result_id"
        ).fetchone()
        connection.execute("delete from window_metrics where result_id = ?", [result_id])
        connection.execute("delete from strategy_actions where result_id = ?", [result_id])
        connection.execute("delete from strategy_equity where result_id = ?", [result_id])
        replacement_end = datetime(2026, 2, 5, tzinfo=timezone.utc)
        connection.execute(
            """update strategy_results set report_start_utc = ?, report_end_utc = ?, exchange = ?,
               commission_rate = ?, initial_balance = ?, final_balance = ?, total_pnl = ?, total_pnl_pct = ?,
               max_drawdown = ?, max_drawdown_pct = ?, total_fees = ?, total_trades = ?, imported_at_utc = ?,
               reported_start_utc = ?, reported_end_utc = ?, listing_date_utc = ?, listing_date_raw = ?,
               listing_date_source = ?, effective_start_utc = ?, effective_end_utc = ?, warmup_hours = ?,
               excluded_trade_count = ?, exclusion_reason = ? where result_id = ?""",
            [
                start,
                replacement_end,
                "Bybit",
                0.0004,
                100,
                125,
                25,
                25,
                7,
                7,
                3,
                3,
                datetime(2026, 2, 1, tzinfo=timezone.utc),
                start,
                replacement_end,
                datetime(2025, 12, 1, tzinfo=timezone.utc),
                "2025-12-01",
                "fixture",
                start,
                replacement_end,
                24,
                1,
                "warmup",
                result_id,
            ],
        )
        connection.executemany(
            "insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size, post_size, post_side, pnl, fee, balance, raw_action_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (result_id, 0, start, "BTCUSDT", 1, "closed", 1, 0, "", 0, 0, 100, None),
                (result_id, 1, datetime(2026, 1, 4, tzinfo=timezone.utc), "BTCUSDT", 1, "opened", 1, 1, "long", 0, 0, 100, None),
                (result_id, 2, datetime(2026, 1, 5, tzinfo=timezone.utc), "BTCUSDT", 1, "closed", 1, 0, "", 25, 3, 125, None),
            ],
        )
        connection.executemany(
            "insert into strategy_equity values (?, ?, ?, ?, ?)",
            [
                (result_id, 0, start, 100, 100),
                (result_id, 1, datetime(2026, 1, 4, tzinfo=timezone.utc), 100, 100),
                (result_id, 2, datetime(2026, 1, 5, tzinfo=timezone.utc), 125, 125),
                (result_id, 3, replacement_end, 125, 125),
            ],
        )
    new = _snapshot(database, [REQUEST])

    assert old.candidates[0]["strategy_id"] == new.candidates[0]["strategy_id"]
    assert old.candidates[0]["result_id"] == new.candidates[0]["result_id"] == result_id
    assert old.candidates[0]["total_pnl"] != new.candidates[0]["total_pnl"]
    assert old.actions != new.actions
    assert old.equity != new.equity
    assert old.digest != new.digest


def test_fresh_reference_facts_make_new_decision_campaign_keep_execution_id() -> None:
    old = fresh_decision_campaign("execution-1", {"turnover24h": "100"})
    new = fresh_decision_campaign("execution-1", {"turnover24h": "101"})

    assert isinstance(old, DecisionCampaign)
    assert old.execution_campaign_id == new.execution_campaign_id == "execution-1"
    assert old.decision_campaign_id != new.decision_campaign_id
    assert old.content_digest != new.content_digest


def test_tick_replay_requires_binary_ticks_and_artifacts() -> None:
    assert portfolio_input.tick_replay_availability("binary", "ticks", [Path("missing")]).status == "UNAVAILABLE"
    assert portfolio_input.tick_replay_availability("binary", "ticks", []).status == "UNAVAILABLE"


def test_tick_replay_is_available_when_identities_and_artifacts_exist(tmp_path: Path) -> None:
    artifact = tmp_path / "tester.bin"
    artifact.write_bytes(b"fixture")
    result = portfolio_input.tick_replay_availability("binary", "ticks", [artifact])
    assert result.status == "AVAILABLE"
    assert result.available


def test_concurrent_writer_is_consistent_or_fails_closed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    writer = duckdb.connect(str(database))
    try:
        try:
            snapshot = _snapshot(database, [REQUEST])
        except PortfolioInputError as error:
            assert error.code == SOURCE_SNAPSHOT_UNAVAILABLE
        else:
            assert snapshot.candidates[0]["strategy_id"]
    finally:
        writer.close()


def test_unavailable_source_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PortfolioInputError) as error:
        _snapshot(tmp_path / "missing.duckdb", [REQUEST])
    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


def _finalist_row(strategy_id: int, symbol: str = "BTCUSDT", side: str = "LONG", rank: int | None = None, *, auto_rank: int | None = 1) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "result_id": strategy_id + 100,
        "symbol": symbol,
        "side": side,
        "user_status": "FINALIST",
        "user_rank": rank,
        "auto_rank": auto_rank,
    }


def test_finalist_cutoff_applies_pair_direction_rules_independently() -> None:
    rows = [
        _finalist_row(1, rank=1), _finalist_row(2, rank=2),
        _finalist_row(4, side="SHORT"),
    ]

    result = apply_finalist_cutoff(
        rows,
        selected_pairs={("BTCUSDT", "LONG"), ("BTCUSDT", "SHORT")},
        maximums={("BTCUSDT", "LONG"): 2, ("BTCUSDT", "SHORT"): 0},
    )

    assert [row["selection_status"] for row in result[:2]] == ["SELECTED", "SELECTED"]
    assert all(row["selection_reason"] == "WITHIN_MAXIMUM" for row in result[:2])
    assert result[2]["selection_status"] == "EXCLUDED"
    assert result[2]["selection_reason"] == "DIRECTION_DISABLED"


def test_finalist_cutoff_uses_user_rank_only_and_marks_cutoff_rows() -> None:
    rows = [_finalist_row(1, rank=3, auto_rank=1), _finalist_row(2, rank=1, auto_rank=999), _finalist_row(3, rank=2, auto_rank=2)]

    result = apply_finalist_cutoff(
        rows,
        selected_pairs={("BTCUSDT", "LONG")},
        maximums={("BTCUSDT", "LONG"): 2},
    )

    assert [row["selection_status"] for row in result] == ["EXCLUDED", "SELECTED", "SELECTED"]
    assert result[0]["selection_reason"] == "USER_RANK_CUTOFF"
    assert result[1]["user_rank"] == 1


def test_finalist_cutoff_rejects_missing_rank_for_multirow_pool() -> None:
    result = apply_finalist_cutoff(
        [_finalist_row(1), _finalist_row(2, rank=2)],
        selected_pairs={("BTCUSDT", "LONG")},
        maximums={("BTCUSDT", "LONG"): 2},
    )

    assert all(row["selection_status"] == "EXCLUDED" for row in result)
    assert all(row["selection_reason"] == "USER_RANK_MISSING" for row in result)


@pytest.mark.parametrize("invalid_rank", (0, -1, "bad", []))
def test_finalist_cutoff_rejects_invalid_multirow_ranks_without_sorting_crash(invalid_rank) -> None:
    result = apply_finalist_cutoff(
        [_finalist_row(1, rank=invalid_rank), _finalist_row(2, rank=2)],
        selected_pairs={("BTCUSDT", "LONG")},
        maximums={("BTCUSDT", "LONG"): 1},
    )
    assert all(row["selection_reason"] == "USER_RANK_MISSING" for row in result)


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([_finalist_row(1), _finalist_row(2, rank=1)], "USER_RANK_MISSING"),
        ([_finalist_row(1, rank=1), _finalist_row(2, rank=1)], "USER_RANK_DUPLICATE"),
    ],
)
def test_finalist_cutoff_blocks_only_invalid_pair_direction(rows: list[dict[str, object]], reason: str) -> None:
    rows.append(_finalist_row(3, side="SHORT", rank=1))

    result = apply_finalist_cutoff(
        rows,
        selected_pairs={("BTCUSDT", "LONG"), ("BTCUSDT", "SHORT")},
        maximums={("BTCUSDT", "LONG"): 1, ("BTCUSDT", "SHORT"): 1},
    )

    assert [row["selection_reason"] for row in result[:2]] == [reason, reason]
    assert result[2]["selection_status"] == "SELECTED"


def test_finalist_cutoff_marks_unselected_pair_and_preserves_input_order() -> None:
    rows = [_finalist_row(2), _finalist_row(1)]

    result = apply_finalist_cutoff(
        rows,
        selected_pairs=set(),
        maximums={("BTCUSDT", "LONG"): 1},
    )

    assert [row["strategy_id"] for row in result] == [2, 1]
    assert all(row["selection_reason"] == "PAIR_UNSELECTED" for row in result)


def test_read_current_finalists_returns_exact_review_facts_without_writes(tmp_path: Path) -> None:
    database = _database(tmp_path)
    before = database.read_bytes()

    rows = read_current_finalists(database, [("BTCUSDT", "LONG")])

    assert rows[0] | {
        "strategy_name": "alpha",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "user_status": "FINALIST",
        "user_rank": 1,
    } == rows[0]
    assert database.read_bytes() == before


def test_read_current_finalists_does_not_promote_auto_only_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("delete from selection_review_rows")
        connection.execute("delete from selection_review_imports")

    assert read_current_finalists(database, [("BTCUSDT", "LONG")], False) == ()
    assert _snapshot(database, [REQUEST]).candidates == ()


def test_read_current_finalists_allows_extra_historical_review_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "insert into selection_review_rows values ('review-default', 999, 'FILTERED', null, null, 'historical')"
        )

    rows = read_current_finalists(database, [("BTCUSDT", "LONG")], False)

    assert len(rows) == 1


def test_current_strategy_without_result_is_outside_finalist_universe(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("update strategies set current_result_id = null")

    assert read_current_finalists(database, [("BTCUSDT", "LONG")], False) == ()
    assert _snapshot(database, [REQUEST]).candidates == ()


def test_latest_import_wins_even_when_its_selection_run_is_older(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        instance_id = connection.execute(
            "select value from schema_info where key = 'database_instance_id'"
        ).fetchone()[0]
        connection.execute(
            """insert into selection_runs values
               ('run-older', ?, 'BTCUSDT', 'LONG', 'test-selection-v1', '{}',
                'request-hash-older', '{}', 'config-hash-older', 1, 1, 1, 1,
                'workbook-hash-older', ?)""",
            [instance_id, datetime(2025, 12, 31, tzinfo=timezone.utc)],
        )
        connection.execute(
            """insert into selection_results values
               ('run-older', ?, ?, 'RESERVE', 1, 1, 'older', '{}', null, false, '{}')""",
            [strategy_id, result_id],
        )
        connection.execute(
            "insert into selection_review_imports values ('review-late', 'run-older', 'review-hash-late', ?, 1)",
            [datetime(2026, 1, 2, tzinfo=timezone.utc)],
        )
        connection.execute(
            "insert into selection_review_rows values ('review-late', ?, 'RESERVE', 1, null, 'late')",
            [strategy_id],
        )

    assert read_current_finalists(database, [("BTCUSDT", "LONG")], False) == ()


def test_read_current_finalists_preserves_review_across_unreviewed_run_and_result_replacement(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    replacement_result_id = 9001
    with duckdb.connect(str(database)) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies"
        ).fetchone()
        for table in ("strategy_results", "strategy_actions", "strategy_equity"):
            connection.execute(
                f"create temp table replacement_{table} as "
                f"select * replace (? as result_id) from {table} where result_id = ?",
                [replacement_result_id, result_id],
            )
        connection.execute("delete from optimizer_prepared_inputs where result_id = ?", [result_id])
        connection.execute("delete from window_metrics where result_id = ?", [result_id])
        for table in ("strategy_actions", "strategy_equity", "strategy_results"):
            connection.execute(f"delete from {table} where result_id = ?", [result_id])
        for table in ("strategy_results", "strategy_actions", "strategy_equity"):
            connection.execute(f"insert into {table} select * from replacement_{table}")
        connection.execute(
            "update strategies set current_result_id = ? where strategy_id = ?",
            [replacement_result_id, strategy_id],
        )
    prepare_current_optimizer_inputs(str(database), [replacement_result_id])
    replaced = read_current_finalists(database, [("BTCUSDT", "LONG")])[0]
    assert replaced["result_id"] == replacement_result_id
    assert replaced["review_import_id"] == "review-default"

    with duckdb.connect(str(database)) as connection:
        instance_id = connection.execute(
            "select value from schema_info where key = 'database_instance_id'"
        ).fetchone()[0]
        connection.execute(
            """insert into selection_runs values
               ('run-new', ?, 'BTCUSDT', 'LONG', 'test-selection-v1', '{}',
                'request-hash-run-new', '{}', 'config-hash-run-new', 1, 1, 1, 1,
                'workbook-hash-run-new', ?)""",
            [instance_id, datetime(2026, 1, 2, tzinfo=timezone.utc)],
        )
        connection.execute(
            """insert into selection_results values
               ('run-new', ?, ?, 'FILTERED', 1, null, 'changed', '{}', null, false, '{}')""",
            [strategy_id, replacement_result_id],
        )

    row = read_current_finalists(database, [("BTCUSDT", "LONG")])[0]

    assert row["result_id"] == replacement_result_id
    assert row["selection_run_id"] == "run-default"
    assert row["review_import_id"] == "review-default"
    assert row["source_provenance"]["selection_run_id"] == "run-default"
    assert row["source_provenance"]["review_import_id"] == "review-default"
    assert row["actions"] and all(item["result_id"] == replacement_result_id for item in row["actions"])
    assert row["equity"] and all(item["result_id"] == replacement_result_id for item in row["equity"])


def test_read_current_finalists_include_series_uses_one_read_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    calls: list[bool] = []
    original_connect = portfolio_input.duckdb.connect

    def tracked_connect(*args, **kwargs):
        calls.append(kwargs.get("read_only", False))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(portfolio_input.duckdb, "connect", tracked_connect)

    rows = read_current_finalists(database, [("BTCUSDT", "LONG")], include_series=True)

    assert rows[0]["actions"]
    assert rows[0]["equity"]
    assert calls == [True]


@pytest.mark.parametrize("mutation", ["missing", "stale", "unavailable"])
def test_read_current_finalists_include_series_uses_one_snapshot_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        result_id = connection.execute("select current_result_id from strategies").fetchone()[0]
        if mutation == "missing":
            connection.execute("delete from optimizer_prepared_inputs where result_id = ?", [result_id])
        elif mutation == "stale":
            connection.execute(
                "update optimizer_prepared_inputs set source_digest = ? where result_id = ?",
                ["0" * 64, result_id],
            )
        else:
            connection.execute(
                """update optimizer_prepared_inputs
                   set availability_status = 'UNAVAILABLE', unavailable_reason = 'MISSING_TYPED_FACTS',
                       prepared_json = null where result_id = ?""",
                [result_id],
            )
    calls: list[bool] = []
    original_connect = portfolio_input.duckdb.connect

    def tracked_connect(*args, **kwargs):
        calls.append(kwargs.get("read_only", False))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(portfolio_input.duckdb, "connect", tracked_connect)

    with pytest.raises(PortfolioInputError) as error:
        read_current_finalists(database, [("BTCUSDT", "LONG")], include_series=True)

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE
    assert calls == [True]


def test_read_current_finalists_preserves_source_geometry_and_identity(tmp_path: Path) -> None:
    row = read_current_finalists(_database(tmp_path), [("BTCUSDT", "LONG")])[0]

    assert {field: row[field] for field in ("close_ma_len", "order_count", "analysis_run_id", "candidate_identity")} == {
        "close_ma_len": 3,
        "order_count": 1,
        "analysis_run_id": "run",
        "candidate_identity": "candidate",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [("close_ma_len", None), ("analysis_run_id", ""), ("candidate_identity", "")],
)
def test_read_current_finalists_rejects_invalid_source_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    database = _database(tmp_path)
    original = portfolio_input._records_for_ids

    def corrupted(connection, table, column, ids):
        rows = original(connection, table, column, ids)
        if table == "strategies":
            return tuple({**row, field: value} for row in rows)
        return rows

    monkeypatch.setattr(portfolio_input, "_records_for_ids", corrupted)

    with pytest.raises(PortfolioInputError) as error:
        read_current_finalists(database, [("BTCUSDT", "LONG")])

    assert error.value.code == "INVALID_SOURCE_VALUE"
    assert field in str(error.value)


def test_read_current_finalists_can_skip_large_series_for_metadata_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    calls: list[str] = []
    original = portfolio_input._records_for_ids

    def checked(connection, table, column, ids):
        calls.append(table)
        return original(connection, table, column, ids)

    monkeypatch.setattr(portfolio_input, "_records_for_ids", checked)
    row = read_current_finalists(database, [("BTCUSDT", "LONG")], False)[0]

    assert "strategy_actions" not in calls
    assert "strategy_equity" not in calls
    assert "actions" not in row
    assert "equity" not in row
    assert "action_series" not in row
    assert "equity_series" not in row


def test_read_current_finalists_metadata_contract_returns_plain_int_result_id(tmp_path: Path) -> None:
    database = _database(tmp_path)

    rows = read_current_finalists(database, [("BTCUSDT", "LONG")], include_series=False)

    assert rows and type(rows[0]["result_id"]) is int
    assert "actions" not in rows[0]
    assert "equity" not in rows[0]


def test_read_current_finalists_returns_physical_facts_and_panel_safe_values(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        result_id = connection.execute("select current_result_id from strategies").fetchone()[0]
        connection.execute(
            """update strategy_results set reported_start_utc = report_start_utc,
               reported_end_utc = report_end_utc, effective_start_utc = report_start_utc,
               effective_end_utc = report_end_utc where result_id = ?""",
            [result_id],
        )
    before = database.read_bytes()

    row = read_current_finalists(database, [("BTCUSDT", "LONG")])[0]

    assert row["timeframe"] == "1h"
    assert row["report_start_utc"] == "2026-01-01T00:00:00Z"
    assert row["reported_end_utc"] == "2026-01-31T00:00:00Z"
    assert row["effective_start_utc"] == "2026-01-01T00:00:00Z"
    assert row["imported_at_utc"] == "2026-01-01T00:00:00Z"
    assert row["total_pnl"] == Decimal("10")
    assert row["total_fees"] == Decimal("2")
    assert row["max_drawdown"] == Decimal("5")
    assert row["max_drawdown_pct"] == Decimal("5")
    assert row["recovery_factor"] == Decimal("2")
    assert row["strategy_orders"] == ({
        "order_id": 1,
        "open_ma_len": 7,
        "open_multiplier": Decimal("0.995000000000"),
        "shift_bp": 125,
        "lot_x": Decimal("1.000000000000"),
    },)
    assert row["source_provenance"]["selection_run_id"] == "run-default"
    json.dumps(_plain(dict(row)), ensure_ascii=False, allow_nan=False)
    assert database.read_bytes() == before


def test_read_current_finalists_exposes_decoded_optimizer_source_metadata(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        result_id, imported_at = connection.execute(
            "select result_id, imported_at_utc from strategy_results"
        ).fetchone()
        metadata = {
            "schema_version": 1,
            "price_cost_semantics": "actual_fill_not_planned_position",
            "imported_at_utc": imported_at.astimezone(timezone.utc).isoformat(),
            "source_report_sha256": "a" * 64,
            "settings": {"basic": {"risk_long": "1"}},
        }
        connection.execute(
            "update strategy_results set optimizer_source_metadata_json = ? where result_id = ?",
            [json.dumps(metadata), result_id],
        )

    row = read_current_finalists(database, [("BTCUSDT", "LONG")])[0]

    assert row["optimizer_source_metadata"] == {
        "schema_version": 1,
        "price_cost_semantics": "actual_fill_not_planned_position",
        "source_report_sha256": "a" * 64,
        "settings": {"basic": {"risk_long": "1"}},
        "imported_at_utc": imported_at.astimezone(timezone.utc).isoformat(),
    }
    assert row["optimizer_source_metadata"]["imported_at_utc"] == imported_at.astimezone(timezone.utc).isoformat()
    assert "optimizer_source_metadata_json" not in row
    with pytest.raises(TypeError):
        row["optimizer_source_metadata"]["settings"]["basic"]["risk_long"] = "2"  # type: ignore[index]
    assert row["actions"][0]["action"] == "closed"
    assert row["actions"][0]["post_size"] == Decimal("0")
    assert row["actions"][0]["balance"] == Decimal("100")


def test_read_current_finalists_feeds_prepare_weighted_input_from_duckdb(tmp_path: Path) -> None:
    database = _database(tmp_path)

    rows = read_current_finalists(database, [("BTCUSDT", "LONG")])
    prepared = prepare_weighted_input(rows)

    assert prepared.strategy_ids == (rows[0]["strategy_id"],)
    assert prepared.cycles
    assert prepared.timestamps_utc[0] == "2026-01-01T00:00:00Z"


def test_read_current_finalists_accepts_legacy_null_optional_ranges(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "update strategy_results set reported_start_utc = null, reported_end_utc = null, effective_start_utc = null, effective_end_utc = null"
        )

    row = read_current_finalists(database, [("BTCUSDT", "LONG")])[0]

    assert row["report_start_utc"] == "2026-01-01T00:00:00Z"
    assert row["reported_start_utc"] is None
    assert row["effective_start_utc"] is None


def test_read_current_finalists_marks_nonpositive_drawdown_recovery_unknown(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        result_id = connection.execute("select current_result_id from strategies").fetchone()[0]
        connection.execute("update strategy_results set max_drawdown = 0 where result_id = ?", [result_id])

    row = read_current_finalists(database, [("BTCUSDT", "LONG")])[0]

    assert row["recovery_factor"] == {
        "status": "UNKNOWN",
        "reason": "MAX_DRAWDOWN_NOT_POSITIVE",
    }


@pytest.mark.parametrize("field", ["total_pnl", "total_fees", "max_drawdown", "max_drawdown_pct"])
def test_read_current_finalists_fails_closed_on_missing_physical_metric(tmp_path: Path, field: str) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        result_id = connection.execute("select current_result_id from strategies").fetchone()[0]
        connection.execute(f"update strategy_results set {field} = null where result_id = ?", [result_id])

    with pytest.raises(PortfolioInputError) as error:
        read_current_finalists(database, [("BTCUSDT", "LONG")])

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE
    assert field in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("total_fees", "-1"), ("max_drawdown", "-1"), ("max_drawdown_pct", "-1")],
)
def test_read_current_finalists_fails_closed_on_invalid_physical_metric(tmp_path: Path, field: str, value: str) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        result_id = connection.execute("select current_result_id from strategies").fetchone()[0]
        connection.execute(f"update strategy_results set {field} = ? where result_id = ?", [value, result_id])

    with pytest.raises(PortfolioInputError) as error:
        read_current_finalists(database, [("BTCUSDT", "LONG")])

    assert error.value.code == "INVALID_SOURCE_VALUE"
    assert field in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("open_multiplier", "0"), ("shift_bp", "-1")],
)
def test_read_current_finalists_fails_closed_on_invalid_strategy_geometry(tmp_path: Path, field: str, value: str) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        strategy_id = connection.execute("select strategy_id from strategies").fetchone()[0]
        connection.execute(f"update strategy_orders set {field} = ? where strategy_id = ?", [value, strategy_id])

    with pytest.raises(PortfolioInputError) as error:
        read_current_finalists(database, [("BTCUSDT", "LONG")])

    assert error.value.code == "INVALID_SOURCE_VALUE"


def test_read_and_select_finalists_uses_current_user_rank(tmp_path: Path) -> None:
    database = _database(tmp_path)
    result = read_and_select_finalists(
        database,
        selected_pairs={("BTCUSDT", "LONG")},
        maximums={("BTCUSDT", "LONG"): 1},
    )

    assert result[0]["user_status"] == "FINALIST"
    assert result[0]["user_rank"] == 1
    assert result[0]["effective_maximum"] == 1
    assert result[0]["selection_status"] == "SELECTED"


def test_read_and_select_finalists_reads_selected_direction_without_explicit_maximum(tmp_path: Path) -> None:
    database = _database(tmp_path)

    result = read_and_select_finalists(
        database,
        selected_pairs={("BTCUSDT", "LONG")},
        maximums={},
    )

    assert len(result) == 1
    assert result[0]["selection_status"] == "EXCLUDED"
    assert result[0]["selection_reason"] == "DIRECTION_DISABLED"


def test_read_current_finalists_uses_exact_current_status_and_fails_closed_on_stale_result(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("update selection_review_rows set user_status = 'RESERVE'")
    assert read_current_finalists(database, [("BTCUSDT", "LONG")]) == ()

    stale_path = tmp_path / "stale"
    stale_path.mkdir()
    database = _database(stale_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("update strategies set current_result_id = current_result_id + 1")
    with pytest.raises(PortfolioInputError) as error:
        read_current_finalists(database, [("BTCUSDT", "LONG")])
    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


def test_common_period_seeds_from_latest_prior_real_sample_and_forward_fills_initial_balance() -> None:
    start = datetime(2026, 1, 10, tzinfo=timezone.utc)
    end = datetime(2026, 1, 14, tzinfo=timezone.utc)
    prior = {"timestamp_utc": "2026-01-09T12:00:00Z", "equity": Decimal("120")}
    row = {"symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1, "report_start_utc": "2026-01-10T00:00:00Z", "report_end_utc": "2026-01-14T00:00:00Z", "initial_balance": Decimal("100"), "equity": (prior, {"timestamp_utc": "2026-01-11T12:00:00Z", "equity": Decimal("130")})}

    result = resolve_common_pretest_period((row,), minimum_common_days=1, minimum_daily_coverage_pct=1, maximum_forward_fill_gap_days=3)

    assert result.available
    path = result.daily_paths["A:LONG:1:1"]
    assert path[0]["equity"] == Decimal("120")
    assert path[0]["seed"] is True
    assert path[1]["equity"] == Decimal("120")
    assert path[2]["equity"] == Decimal("130")

    initial = dict(row, equity=({"timestamp_utc": "2026-01-11T12:00:00Z", "equity": Decimal("130")},))
    seeded = resolve_common_pretest_period((initial,), minimum_common_days=1, minimum_daily_coverage_pct=1, maximum_forward_fill_gap_days=3)
    assert seeded.available
    assert seeded.daily_paths["A:LONG:1:1"][0]["equity"] == Decimal("100")
    assert seeded.daily_paths["A:LONG:1:1"][0]["seed"] is True


def test_legacy_fixture_and_prepared_db_paths_are_exactly_equivalent_for_cycles() -> None:
    start = "2026-01-01T00:00:00Z"
    end = "2026-01-15T00:00:00Z"
    actions = (
        {"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "order_id": 1, "action": "opened", "size": "2", "post_size": "2", "post_side": "long", "pnl": "0", "fee": "1", "balance": "100", "price": "7", "cost": "8"},
        {"action_index": 1, "timestamp_utc": "2026-01-02T00:00:00Z", "symbol": "BTCUSDT", "order_id": 1, "action": "closed", "size": "2", "post_size": "0", "post_side": "", "pnl": "20", "fee": "2", "balance": "120", "price": "9", "cost": "18"},
        {"action_index": 2, "timestamp_utc": "2026-01-03T00:00:00Z", "symbol": "BTCUSDT", "order_id": 2, "action": "opened", "size": "3", "post_size": "3", "post_side": "long", "pnl": "0", "fee": "1", "balance": "120", "price": "11", "cost": "12"},
        {"action_index": 3, "timestamp_utc": "2026-01-04T00:00:00Z", "symbol": "BTCUSDT", "order_id": 2, "action": "closed", "size": "3", "post_size": "0", "post_side": "", "pnl": "30", "fee": "3", "balance": "150", "price": "13", "cost": "39"},
    )
    equity = tuple(
        {"sample_index": index, "timestamp_utc": timestamp, "equity": value}
        for index, (timestamp, value) in enumerate(
            (("2026-01-01T00:00:00Z", "100"), ("2026-01-02T00:00:00Z", "120"),
             ("2026-01-03T00:00:00Z", "120"), ("2026-01-04T00:00:00Z", "150"),
             (end, "150"))
        )
    )
    common = {
        "symbol": "BTCUSDT", "side": "LONG", "strategy_id": 11, "result_id": 17,
        "report_start_utc": start, "report_end_utc": end,
        "effective_start_utc": start, "effective_end_utc": end,
        "imported_at_utc": "2026-01-15T00:00:00Z", "initial_balance": Decimal("100"),
        "sizing_use_upnl": True, "sizing_use_frozen_balance": True, "sizing_use_fix": False,
        "sizing_balance_percentage_long": Decimal("100"), "sizing_risk_long": Decimal("1"),
        "sizing_max_balance": Decimal("0"), "actions": actions, "equity": equity,
    }
    legacy = {**common, "_source_origin": "LEGACY_FIXTURE"}
    source = OptimizerSourceInput(
        source_document_version="performance-v2", result_id=17, strategy_id=11,
        symbol="BTCUSDT", side="LONG", revision_timestamp_utc=common["imported_at_utc"],
        report_start_utc=start, report_end_utc=end, effective_start_utc=start,
        effective_end_utc=end, initial_balance=Decimal("100"), sizing_use_upnl=True,
        sizing_use_frozen_balance=True, sizing_use_fix=False,
        sizing_balance_percentage_long=Decimal("100"), sizing_risk_long=Decimal("1"),
        sizing_max_balance=Decimal("0"), actions=actions, equity=equity,
    )
    prepared = build_prepared_input(source)
    prepared_row = _PreparedResultRow({**common, "actions": prepared.actions, "equity": prepared.equity})
    prepared_row._optimizer_prepared = prepared

    legacy_result = prepare_weighted_input((legacy,), minimum_common_days=1)
    prepared_result = prepare_weighted_input((prepared_row,), minimum_common_days=1)
    key = "BTCUSDT:LONG:11:17"

    assert legacy_result.cycles[key] == prepared_result.cycles[key]
    assert legacy_result.normalized_delta == prepared_result.normalized_delta
    assert legacy_result.valid == prepared_result.valid
    assert legacy_result.cycles[key][0]["source_basis"] == Decimal("101")
    assert legacy_result.cycles[key][1]["source_basis"] == Decimal("121")
    assert legacy_result.cycles[key][0]["source_basis"] not in {Decimal("7"), Decimal("8")}


def test_prepare_weighted_input_uses_dynamic_cycle_basis_and_last_known_equity() -> None:
    start = "2026-01-01T00:00:00Z"
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": "2026-01-15T00:00:00Z",
        "effective_start_utc": start, "effective_end_utc": "2026-01-15T00:00:00Z",
        "imported_at_utc": start,
        "optimizer_source_metadata": {
            "source_report_sha256": "a" * 64,
            "settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
                "use_fix": False, "balance_percentage_long": "100", "risk_long": "1", "max_balance": "0",
            }},
        },
        "actions": ({
            "action_index": 0, "timestamp_utc": start, "symbol": "A", "action": "opened",
            "size": "1", "post_size": "1", "post_side": "LONG", "pnl": "0", "fee": "0", "balance": "100",
        }, {
            "action_index": 1, "timestamp_utc": "2026-01-15T00:00:00Z", "symbol": "A", "action": "closed",
            "size": "1", "post_size": "0", "post_side": "LONG", "pnl": "20", "fee": "0", "balance": "120",
        }),
        "equity": (
            {"timestamp_utc": start, "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:07:00Z", "equity": "110"},
            {"timestamp_utc": "2026-01-01T00:12:00Z", "equity": "120"},
        ),
    }

    prepared = prepare_weighted_input((row,))

    assert prepared.history_step_minutes == 5
    assert prepared.timestamps_utc[1] == "2026-01-01T00:05:00Z"
    assert prepared.normalized_delta[0][0] == Decimal("0")
    assert prepared.normalized_delta[1][0] == Decimal("0.1")
    assert all(prepared.valid[index][0] for index in range(2))
    assert prepared.cycles["A:LONG:1:1"][0]["source_basis"] == Decimal("100")
    diagnostics = prepared.diagnostics["rows"]["A:LONG:1:1"]
    assert diagnostics["cycle_count"] == 1
    assert diagnostics["known_hold_durations_seconds"] == (Decimal("1209600"),)
    assert diagnostics["occupied_duration_seconds"] == Decimal("1209600")
    assert diagnostics["occupied_ratio"] == Decimal("1")
    assert diagnostics["source_bases"] == (Decimal("100"),)
    assert diagnostics["median_source_basis"] == Decimal("100")


def test_prepare_weighted_input_uses_indexed_dense_equity_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 3, tzinfo=timezone.utc)
    samples = tuple(
        {"timestamp_utc": (start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"), "equity": str(100 + index)}
        for index in range(2 * 24 * 60 + 1)
    )
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start.isoformat().replace("+00:00", "Z"),
        "report_end_utc": end.isoformat().replace("+00:00", "Z"),
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": (
            {"action_index": 0, "timestamp_utc": start.isoformat().replace("+00:00", "Z"), "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},
            {"action_index": 1, "timestamp_utc": end.isoformat().replace("+00:00", "Z"), "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": str(100 + 2 * 24 * 60), "pnl": str(2 * 24 * 60), "fee": "0"},
        ),
        "equity": samples,
    }
    calls: list[int] = []
    original = getattr(portfolio_input, "bisect_right", None)

    def count_calls(values, needle):
        calls.append(len(values))
        assert original is not None
        return original(values, needle)

    monkeypatch.setattr(portfolio_input, "bisect_right", count_calls, raising=False)

    prepared = prepare_weighted_input((row,), history_step_minutes=60, minimum_common_days=1)

    assert calls
    assert prepared.normalized_delta[0][0] == Decimal("0.6")
    assert all(prepared.valid[index][0] for index in range(len(prepared.valid)))


def test_prepare_weighted_input_attributes_right_node_open_to_new_cycle() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": "100", "risk_long": "1", "max_balance": "0",
        }}},
        "actions": ({"action_index": 0, "timestamp_utc": "2026-01-01T00:05:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},),
        "equity": (
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:05:00Z", "equity": "110"},
        ),
    }

    prepared = prepare_weighted_input((row,))

    assert prepared.normalized_delta[0][0] == Decimal("0.1")
    assert prepared.valid[0][0] is True


def test_prepare_weighted_input_rejects_period_that_peels_a_finalist() -> None:
    rows = (
        {
            "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
            "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-31T00:00:00Z",
            "initial_balance": "100", "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
        },
        {
            "symbol": "B", "side": "LONG", "strategy_id": 2, "result_id": 2,
            "report_start_utc": "2026-01-20T00:00:00Z", "report_end_utc": "2026-01-31T00:00:00Z",
            "initial_balance": "100", "equity": ({"timestamp_utc": "2026-01-20T00:00:00Z", "equity": "100"},),
        },
    )

    with pytest.raises(PortfolioInputError) as error:
        prepare_weighted_input(rows)

    assert error.value.code == "COMMON_PERIOD_UNIVERSE_CHANGED"


def test_prepare_weighted_input_keeps_validity_and_reason_per_participant() -> None:
    start = "2026-01-01T00:00:00Z"
    valid_row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": "2026-01-15T00:00:00Z",
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": "100", "risk_long": "1", "max_balance": "0",
        }}},
        "actions": ({"action_index": 0, "timestamp_utc": start, "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},),
        "equity": ({"timestamp_utc": start, "equity": "100"},),
    }
    invalid_row = {
        "symbol": "B", "side": "LONG", "strategy_id": 2, "result_id": 2,
        "report_start_utc": start, "report_end_utc": "2026-01-15T00:00:00Z", "initial_balance": "100",
        "equity": ({"timestamp_utc": "2026-01-01T01:00:00Z", "equity": "110"},),
    }

    prepared = prepare_weighted_input((valid_row, invalid_row))

    assert prepared.valid[0] == (True, True)
    assert prepared.reasons[0][0] is None
    assert prepared.valid[11] == (True, False)
    assert "symbol=B" in prepared.reasons[11][1]
    assert "strategy_id=2" in prepared.reasons[11][1]
    assert "result_id=2" in prepared.reasons[11][1]


def test_prepare_weighted_input_zeroes_partial_delta_when_later_segment_is_unknown() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": (
            {"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},
            {"action_index": 1, "timestamp_utc": "2026-01-01T00:02:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "110", "pnl": "10", "fee": "0"},
        ),
        "equity": (
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:02:00Z", "equity": "110"},
            {"timestamp_utc": "2026-01-01T00:04:00Z", "equity": "120"},
        ),
    }

    prepared = prepare_weighted_input((row,))

    assert prepared.normalized_delta[0][0] == Decimal("0")
    assert prepared.valid[0][0] is False
    assert "UNATTRIBUTABLE_EQUITY" in prepared.reasons[0][0]


def test_prepare_weighted_input_rejects_unrecognized_equity_point_shape() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "initial_balance": "100",
        "equity": (("2026-01-01T00:00:00Z", "100"),),
    }

    with pytest.raises(PortfolioInputError) as error:
        prepare_weighted_input((row,))

    assert error.value.code == "INVALID_SOURCE_VALUE"


def test_prepare_weighted_input_rejects_non_sequence_equity_container() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "initial_balance": "100",
        "equity": {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
    }

    with pytest.raises(PortfolioInputError) as error:
        prepare_weighted_input((row,))

    assert error.value.code == "INVALID_SOURCE_VALUE"


def test_common_weighted_period_uses_each_effective_range_full_utc_days() -> None:
    rows = (
        {
            "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
            "report_start_utc": "2026-01-01T12:00:00Z", "report_end_utc": "2026-01-31T12:00:00Z",
            "effective_start_utc": "2026-01-01T06:00:00Z", "effective_end_utc": "2026-01-31T06:00:00Z",
            "initial_balance": "100", "equity": (),
        },
        {
            "symbol": "B", "side": "LONG", "strategy_id": 2, "result_id": 2,
            "report_start_utc": "2026-01-01T12:00:00Z", "report_end_utc": "2026-01-31T12:00:00Z",
            "effective_start_utc": "2026-01-03T06:00:00Z", "effective_end_utc": "2026-01-20T06:00:00Z",
            "initial_balance": "100", "equity": (),
        },
    )

    period = resolve_common_pretest_period(rows, minimum_common_days=14)

    assert period.available
    assert period.start_utc == datetime(2026, 1, 4, tzinfo=timezone.utc)
    assert period.end_utc == datetime(2026, 1, 20, tzinfo=timezone.utc)


def test_prepare_weighted_input_marks_carry_in_change_unknown() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": "100", "risk_long": "1", "max_balance": "0",
        }}},
        "actions": ({"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},),
        "equity": (
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:07:00Z", "equity": "110"},
        ),
    }

    prepared = prepare_weighted_input((row,))

    assert prepared.cycles["A:LONG:1:1"][0]["carry_in"] is True
    assert prepared.valid[1][0] is False
    assert "UNATTRIBUTABLE_EQUITY" in prepared.reasons[1][0]


def test_prepare_weighted_input_carry_in_occupancy_starts_at_common_window() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "actions": ({"action_index": 0, "timestamp_utc": "2026-01-08T00:00:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},),
        "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }

    prepared = prepare_weighted_input((row,))

    diagnostics = prepared.diagnostics["rows"]["A:LONG:1:1"]
    assert diagnostics["occupied_duration_seconds"] == Decimal("604800")
    assert diagnostics["occupied_ratio"] == Decimal("0.5")


@pytest.mark.parametrize("action_name", ["increased", "decreased"])
def test_cycle_records_leading_non_opening_has_no_dynamic_source_basis(action_name: str) -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": (
            {"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": action_name, "post_size": "2", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},
            {"action_index": 1, "timestamp_utc": "2026-01-01T00:00:01Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "101", "pnl": "1", "fee": "0"},
            {"action_index": 2, "timestamp_utc": "2026-01-01T00:00:02Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "101", "pnl": "0", "fee": "0"},
        ),
    }

    cycles = _cycle_records(row, datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc))

    assert cycles[0]["carry_in"] is True
    assert cycles[0]["source_basis"] is None
    assert cycles[1]["carry_in"] is False


def test_cycle_records_consumes_same_timestamp_opening_sources_in_order() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": (
            {"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},
            {"action_index": 1, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "110", "pnl": "10", "fee": "0"},
            {"action_index": 2, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "200", "pnl": "0", "fee": "0"},
            {"action_index": 3, "timestamp_utc": "2026-01-01T00:00:01Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "220", "pnl": "20", "fee": "0"},
        ),
    }

    cycles = _cycle_records(row, datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc))

    assert tuple(cycle["source_basis"] for cycle in cycles) == (Decimal("100"), Decimal("200"))
    assert tuple(cycle["source_ordinal"] for cycle in cycles) == (0, 2)
    assert tuple(cycle["normalized_pnl"] for cycle in cycles) == (Decimal("0.10"), Decimal("0.10"))


def test_cycle_records_uses_positional_ordinal_for_action_rows_without_index() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": (
            {"timestamp_utc": "2026-01-01T00:00:02Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "200", "pnl": "0", "fee": "0"},
            {"timestamp_utc": "2026-01-01T00:00:03Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "220", "pnl": "20", "fee": "0"},
            {"timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},
            {"timestamp_utc": "2026-01-01T00:00:01Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "110", "pnl": "10", "fee": "0"},
        ),
    }

    actions = portfolio_reports.performance_rows_to_report_actions(row["actions"])
    assert tuple(action.source_ordinal for action in actions) == (2, 3, 0, 1)

    cycles = _cycle_records(row, datetime(2026, 1, 1, 0, 0, 4, tzinfo=timezone.utc))

    assert tuple(cycle["source_basis"] for cycle in cycles) == (Decimal("100"), Decimal("200"))


def test_cycle_records_defaults_null_opening_pnl_and_fee_to_zero() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": ({"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": None, "fee": None},),
        "equity": (
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:07:00Z", "equity": "110"},
        ),
    }

    prepared = prepare_weighted_input((row,))

    assert prepared.cycles["A:LONG:1:1"][0]["source_basis"] == Decimal("100")
    assert prepared.valid[1][0] is True


def test_cycle_records_rejects_malformed_opening_numeric_source() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": ({"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "not-a-number", "pnl": "0", "fee": "0"},),
    }

    with pytest.raises(PortfolioInputError) as error:
        _cycle_records(row, datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc))

    assert error.value.code == "INVALID_SOURCE_VALUE"


def test_cycle_records_unmatched_opening_is_diagnostic_and_does_not_consume_later_source(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": ({"action_index": 0, "timestamp_utc": "2026-01-01T00:00:01Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},),
    }
    monkeypatch.setattr(portfolio_reports, "reconstruct_cycles", lambda *_args, **_kwargs: (
        PositionCycle("A", 0, "2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.500000Z", Decimal("0.5"), False, False, "LONG", None, None, Decimal("1"), 1),
        PositionCycle("A", 1, "2026-01-01T00:00:01.000000Z", None, None, True, False, "LONG", None, None, Decimal("1"), 1),
    ))

    cycles = _cycle_records(row, datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc))

    assert cycles[0]["source_basis"] is None
    assert "SOURCE_OPENING_ROW_UNMATCHED" in cycles[0]["diagnostics"]
    assert cycles[1]["source_basis"] == Decimal("100")


@pytest.mark.parametrize("strategy_id", [None, "1", 1.0, True])
def test_prepare_weighted_input_rejects_non_integer_strategy_id(strategy_id: object) -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": strategy_id, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "initial_balance": "100", "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }

    with pytest.raises(PortfolioInputError) as error:
        prepare_weighted_input((row,))

    assert error.value.code == "INVALID_SOURCE_VALUE"


def test_prepare_weighted_input_rejects_duplicate_participant_key() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "initial_balance": "100", "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }

    with pytest.raises(PortfolioInputError) as error:
        prepare_weighted_input((row, dict(row)))

    assert error.value.code == "INVALID_SOURCE_VALUE"


def test_prepare_weighted_input_attributes_equal_endpoint_intra_cell_changes() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": (
            {"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},
            {"action_index": 1, "timestamp_utc": "2026-01-01T00:02:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "110", "pnl": "10", "fee": "0"},
            {"action_index": 2, "timestamp_utc": "2026-01-01T00:02:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "200", "pnl": "0", "fee": "0"},
            {"action_index": 3, "timestamp_utc": "2026-01-01T00:04:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "190", "pnl": "-10", "fee": "0"},
        ),
        "equity": (
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:02:00Z", "equity": "110"},
            {"timestamp_utc": "2026-01-01T00:04:00Z", "equity": "100"},
        ),
    }

    prepared = prepare_weighted_input((row,))

    assert prepared.normalized_delta[0][0] == Decimal("0.05")
    assert prepared.valid[0][0] is True
    cycles = prepared.cycles["A:LONG:1:1"]
    assert cycles[0]["strategy_id"] == 1
    assert cycles[0]["source_ordinal"] == 0
    assert cycles[0]["common_window_normalized_return"] == Decimal("0.10")
    assert cycles[0]["attribution_complete"] is True
    assert cycles[0]["normalized_pnl"] == Decimal("0.10")
    assert cycles[1]["source_ordinal"] == 2
    assert cycles[1]["common_window_normalized_return"] == Decimal("-0.05")
    assert cycles[1]["attribution_complete"] is True
    assert cycles[1]["normalized_pnl"] == Decimal("-0.05")


def test_prepare_weighted_input_rolls_back_partial_cell_attribution_on_unknown_owner() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": (
            {"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},
            {"action_index": 1, "timestamp_utc": "2026-01-01T00:01:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "110", "pnl": "10", "fee": "0"},
            {"action_index": 2, "timestamp_utc": "2026-01-01T00:02:00Z", "symbol": "A", "action": "increased", "post_size": "1", "post_side": "LONG", "balance": "0", "pnl": "0", "fee": "0"},
            {"action_index": 3, "timestamp_utc": "2026-01-01T00:03:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "pnl": "10", "fee": "0"},
        ),
        "equity": (
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:01:00Z", "equity": "110"},
            {"timestamp_utc": "2026-01-01T00:02:00Z", "equity": "110"},
            {"timestamp_utc": "2026-01-01T00:03:00Z", "equity": "120"},
        ),
    }

    prepared = prepare_weighted_input((row,))

    assert prepared.normalized_delta[0][0] == Decimal("0")
    assert prepared.valid[0][0] is False
    cycles = prepared.cycles["A:LONG:1:1"]
    assert all(cycle["common_window_normalized_return"] is None for cycle in cycles)
    assert all(cycle["attribution_complete"] is False for cycle in cycles)


def test_cycle_records_keeps_clipped_return_separate_from_unknown_full_cycle_net() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100, "risk_long": 1, "max_balance": 0,
        }}},
        "actions": (
            {"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},
            {"action_index": 1, "timestamp_utc": "2026-01-01T00:01:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "pnl": "10", "fee": "0"},
            {"action_index": 2, "timestamp_utc": "2026-01-01T00:01:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "200", "pnl": "0", "fee": "0"},
        ),
        "equity": (
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:01:00Z", "equity": "110"},
            {"timestamp_utc": "2026-01-01T00:02:00Z", "equity": "120"},
        ),
    }

    prepared = prepare_weighted_input((row,))
    cycles = prepared.cycles["A:LONG:1:1"]

    assert cycles[0]["common_window_normalized_return"] == Decimal("0.10")
    assert cycles[0]["attribution_complete"] is True
    assert cycles[0]["normalized_pnl"] is None
    assert cycles[1]["common_window_normalized_return"] == Decimal("0.05")
    assert cycles[1]["attribution_complete"] is True
    assert cycles[1]["normalized_pnl"] is None


def test_prepare_weighted_input_invalidates_equal_endpoint_carry_in_movement() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "actions": ({"action_index": 0, "timestamp_utc": "2026-01-01T00:02:00Z", "symbol": "A", "action": "closed", "post_size": "0", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},),
        "equity": (
            {"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},
            {"timestamp_utc": "2026-01-01T00:02:00Z", "equity": "110"},
            {"timestamp_utc": "2026-01-01T00:04:00Z", "equity": "100"},
        ),
    }

    prepared = prepare_weighted_input((row,))

    assert prepared.normalized_delta[0][0] == Decimal("0")
    assert prepared.valid[0][0] is False
    assert "UNATTRIBUTABLE_EQUITY" in prepared.reasons[0][0]


@pytest.mark.parametrize("side", ["SHORT"])
def test_prepare_weighted_input_accepts_short_side(side: str) -> None:
    row = {
        "symbol": "A", "side": side, "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "initial_balance": "100", "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }

    prepared = prepare_weighted_input((row,))
    assert prepared.strategy_ids == (1,)


def test_prepare_weighted_input_rejects_duplicate_symbol() -> None:
    base = {
        "symbol": "A", "side": "LONG", "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "initial_balance": "100", "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }

    with pytest.raises(PortfolioInputError) as error:
        prepare_weighted_input((dict(base, strategy_id=1, result_id=1), dict(base, strategy_id=2, result_id=2)))

    assert error.value.code == "INVALID_SOURCE_VALUE"


def test_prepare_weighted_input_rejects_duplicate_strategy_id_across_symbols() -> None:
    base = {
        "side": "LONG", "strategy_id": 1, "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "initial_balance": "100", "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }

    with pytest.raises(PortfolioInputError) as error:
        prepare_weighted_input((dict(base, symbol="A", result_id=1), dict(base, symbol="B", result_id=2)))

    assert error.value.code == "INVALID_SOURCE_VALUE"


def test_prepare_weighted_input_orders_participants_deterministically() -> None:
    def row(symbol: str, strategy_id: int, result_id: int) -> dict:
        return {
            "symbol": symbol, "side": "LONG", "strategy_id": strategy_id, "result_id": result_id,
            "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
            "initial_balance": "100", "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
        }

    first = (row("B", 1, 1), row("A", 2, 2))
    second = tuple(reversed(first))
    cache: dict[str, object] = {}
    prepared_first = prepare_weighted_input(first, cache=cache)  # type: ignore[arg-type]
    prepared_second = prepare_weighted_input(second, cache=cache)  # type: ignore[arg-type]

    assert prepared_first.strategy_ids == (2, 1)
    assert prepared_second is prepared_first


def test_prepare_weighted_input_includes_period_end_for_non_dividing_step() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }

    prepared = prepare_weighted_input((row,), history_step_minutes=11)

    assert prepared.timestamps_utc[0] == "2026-01-01T00:00:00Z"
    assert prepared.timestamps_utc[-1] == "2026-01-15T00:00:00Z"


def test_prepare_weighted_input_accepts_opposite_sides_for_one_symbol_and_canonicalizes_order() -> None:
    def row(symbol: str, side: str, strategy_id: int) -> dict:
        return {
            "symbol": symbol, "side": side, "strategy_id": strategy_id, "result_id": strategy_id,
            "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
            "initial_balance": "100", "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
        }

    prepared = prepare_weighted_input((row(" btcusdt ", "short", 2), row("BTCUSDT", "LONG", 1)))

    assert prepared.strategy_ids == (1, 2)
    assert tuple(prepared.cycles) == ("BTCUSDT:LONG:1:1", "BTCUSDT:SHORT:2:2")


def test_prepare_weighted_input_numeric_dynamic_settings_match_exact_values() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "optimizer_source_metadata": {"settings": {"exchange": {"use_upnl": True, "use_frozen_balance": True}, "basic": {
            "use_fix": False, "balance_percentage_long": 100.0, "risk_long": 1.0, "max_balance": 0.0,
        }}},
        "actions": ({"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},),
        "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }

    prepared = prepare_weighted_input((row,))

    assert prepared.cycles["A:LONG:1:1"][0]["source_basis"] == Decimal("100")


def test_prepare_weighted_input_cache_result_is_deep_frozen() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "actions": ({"action_index": 0, "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "A", "action": "opened", "post_size": "1", "post_side": "LONG", "balance": "100", "pnl": "0", "fee": "0"},),
        "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }
    cache: dict[str, object] = {}
    prepared = prepare_weighted_input((row,), cache=cache)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        prepared.cycles["A:LONG:1:1"][0]["source_basis"] = Decimal("1")  # type: ignore[index]
    with pytest.raises(TypeError):
        prepared.diagnostics["rows"]["A:LONG:1:1"]["cycle_count"] = 0  # type: ignore[index]

    cached = prepare_weighted_input((row,), cache=cache)  # type: ignore[arg-type]
    assert cached.cycles["A:LONG:1:1"][0]["source_basis"] is None


def test_prepare_weighted_input_cache_misses_same_result_id_after_source_revision_change() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-01T00:00:00Z", "report_end_utc": "2026-01-15T00:00:00Z",
        "imported_at_utc": "2026-01-01T00:00:00Z",
        "equity": ({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "100"},),
    }
    cache: dict[str, object] = {}
    first = prepare_weighted_input((row,), cache=cache)  # type: ignore[arg-type]
    changed = dict(row, optimizer_source_metadata={"source_report_sha256": "b" * 64})
    second = prepare_weighted_input((changed,), cache=cache)  # type: ignore[arg-type]
    changed_imported = dict(changed, imported_at_utc="2026-01-02T00:00:00Z")
    third = prepare_weighted_input((changed_imported,), cache=cache)  # type: ignore[arg-type]
    minimum_changed = prepare_weighted_input((changed_imported,), minimum_common_days=13, cache=cache)  # type: ignore[arg-type]
    series_changed = dict(changed_imported, equity=({"timestamp_utc": "2026-01-01T00:00:00Z", "equity": "101"},))
    fourth = prepare_weighted_input((series_changed,), minimum_common_days=13, cache=cache)  # type: ignore[arg-type]

    assert first is not second
    assert second is not third
    assert third is not minimum_changed
    assert minimum_changed is not fourth
    assert len(cache) == 5


def test_preparation_cache_key_includes_campaign_weighted_algo_version(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "imported_at_utc": "2026-01-01T00:00:00Z",
    }

    first = preparation_cache_key((row,))
    monkeypatch.setattr(portfolio_input, "CAMPAIGN_WEIGHTED_ALGO_VERSION", "WS9.9")

    second = preparation_cache_key((row,))

    assert first != second


def test_current_result_prior_sample_older_than_diagnostic_gap_still_passes() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-10T00:00:00Z",
        "report_end_utc": "2026-01-24T00:00:00Z",
        "equity": (
            {"timestamp_utc": "2026-01-05T00:00:00Z", "equity": Decimal("120")},
            {"timestamp_utc": "2026-01-20T00:00:00Z", "equity": Decimal("130")},
        ),
    }

    result = resolve_common_pretest_period(
        (row,), minimum_common_days=14, minimum_daily_coverage_pct=1,
        maximum_forward_fill_gap_days=3,
    )

    assert result.available
    assert result.evidence["coverage_gate"] == "NON_BINDING_DIAGNOSTIC"
    assert result.evidence["rows"]["A:LONG:1:1"]["max_observation_gap_days"] == Decimal("10.00000000")


def test_common_period_rejects_leading_days_without_prior_or_initial_seed() -> None:
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": "2026-01-10T00:00:00Z",
        "report_end_utc": "2026-01-24T00:00:00Z",
        "equity": ({"timestamp_utc": "2026-01-11T00:00:00Z", "equity": Decimal("130")},),
    }

    result = resolve_common_pretest_period(
        (row,), minimum_common_days=14, minimum_daily_coverage_pct=90,
        maximum_forward_fill_gap_days=3,
    )

    assert not result.available
    assert any(item["reason"] == "DAILY_PATH_REQUIRES_SEED" for item in result.exclusions)


def test_current_result_accepts_sparse_observations_and_persists_diagnostics() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": start + __import__("datetime").timedelta(days=28),
        "initial_balance": Decimal("100"),
        "equity": tuple(
            {"timestamp_utc": start + __import__("datetime").timedelta(days=index), "equity": Decimal("100") + index}
            for index in range(0, 28, 2)
        ),
        "actions": (),
    }

    result = resolve_common_pretest_period(
        (row,), minimum_common_days=14, minimum_daily_coverage_pct=100,
        maximum_forward_fill_gap_days=0,
    )

    assert result.available
    evidence = result.evidence["rows"]["A:LONG:1:1"]
    assert evidence["sparse_observation_tag"] == "SPARSE_OBSERVATION_FORWARD_FILL"
    assert evidence["path_policy"] == "SPARSE_OBSERVATION_FORWARD_FILL"
    assert evidence["calendar_days"] == 28
    assert evidence["in_window_observation_count"] == 14
    assert evidence["in_window_action_count"] == 0
    assert evidence["observed_day_count"] == 14
    assert evidence["observed_day_ratio"] == Decimal("50.00000000")
    assert evidence["observed_sample_day_count"] == 14
    assert evidence["observed_sample_day_ratio"] == Decimal("50.00000000")
    assert evidence["max_observation_gap_days"] == Decimal("2")
    assert evidence["maximum_observation_gap_days"] == Decimal("2")
    assert evidence["seed_source"] == "EQUITY_OBSERVATION"
    assert evidence["start_seed_source"] == "PRIOR_OBSERVATION"
    assert evidence["initial_balance_seed_used"] is False
    assert evidence["seeded"] is False
    assert evidence["dd_bias"] == "DOWNWARD_BIASED_BETWEEN_OBSERVATIONS"
    assert evidence["pretest_source_mode"] == "CURRENT_RESULT"
    assert result.coverage_pct["A:LONG:1:1"] == Decimal("50.00000000")


def test_current_result_does_not_use_a_future_same_day_observation_as_start_seed() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": start + __import__("datetime").timedelta(days=14),
        "initial_balance": Decimal("100"),
        "equity": (
            {"timestamp_utc": start + __import__("datetime").timedelta(hours=12), "equity": Decimal("120")},
        ),
    }

    result = resolve_common_pretest_period((row,), minimum_common_days=14)

    assert result.available
    path = result.daily_paths["A:LONG:1:1"]
    assert path[0]["equity"] == Decimal("100")
    assert path[0]["seed"] is True
    assert path[1]["equity"] == Decimal("120")


def test_current_result_daily_path_includes_terminal_interval_endpoint() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + __import__("datetime").timedelta(days=14)
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": end,
        "initial_balance": Decimal("100"),
        "equity": (
            {"timestamp_utc": end - __import__("datetime").timedelta(hours=1), "equity": Decimal("125")},
        ),
    }

    result = resolve_common_pretest_period((row,), minimum_common_days=14)

    assert result.available
    path = result.daily_paths["A:LONG:1:1"]
    assert len(path) == 15
    assert path[-1]["timestamp_utc"] == end
    assert path[-1]["equity"] == Decimal("125")


def test_current_result_without_any_start_seed_is_rejected_even_when_window_is_empty() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": start + __import__("datetime").timedelta(days=14),
        "equity": (), "actions": (),
    }

    result = resolve_common_pretest_period((row,), minimum_common_days=14)

    assert not result.available
    assert any(item["reason"] == "DAILY_PATH_REQUIRES_SEED" for item in result.exclusions)


def test_current_result_zero_initial_balance_is_not_a_valid_start_seed() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": start + __import__("datetime").timedelta(days=14),
        "initial_balance": Decimal("0"), "equity": (), "actions": (),
    }

    result = resolve_common_pretest_period((row,), minimum_common_days=14)

    assert not result.available
    assert any(item["reason"] == "INVALID_INITIAL_BALANCE" for item in result.exclusions)


def test_current_result_coverage_and_gap_settings_are_diagnostics_only() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": start + __import__("datetime").timedelta(days=28),
        "initial_balance": Decimal("100"),
        "equity": tuple(
            {"timestamp_utc": start + __import__("datetime").timedelta(days=index), "equity": Decimal("100") + index}
            for index in range(0, 28, 4)
        ),
    }

    permissive = resolve_common_pretest_period((row,), minimum_common_days=14, minimum_daily_coverage_pct=1, maximum_forward_fill_gap_days=0)
    strict = resolve_common_pretest_period((row,), minimum_common_days=14, minimum_daily_coverage_pct=100, maximum_forward_fill_gap_days=99)

    assert permissive.status == strict.status == "PASS"
    assert (permissive.start_utc, permissive.end_utc) == (strict.start_utc, strict.end_utc)
    assert permissive.daily_paths == strict.daily_paths
    assert permissive.evidence["coverage_gate"] == strict.evidence["coverage_gate"] == "NON_BINDING_DIAGNOSTIC"
    assert permissive.evidence["forward_fill_gap_gate"] == strict.evidence["forward_fill_gap_gate"] == "NON_BINDING_DIAGNOSTIC"


def test_current_result_zero_activity_and_observation_loss_are_distinct() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    common = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": start + __import__("datetime").timedelta(days=14),
        "initial_balance": Decimal("100"),
    }
    zero = resolve_common_pretest_period((common,), minimum_common_days=14)
    assert zero.available
    assert zero.evidence["rows"]["A:LONG:1:1"]["reason"] == "ZERO_ACTIVITY_IN_WINDOW"
    assert all(point["equity"] == Decimal("100") for point in zero.daily_paths["A:LONG:1:1"])

    loss = resolve_common_pretest_period((dict(common, actions=({"timestamp_utc": start},)),), minimum_common_days=14)
    assert not loss.available
    assert any(item["reason"] == "INVALID_START_SEED" for item in loss.exclusions)

    observed_before = dict(common, equity=({"timestamp_utc": start - __import__("datetime").timedelta(days=1), "equity": Decimal("100")},), actions=({"timestamp_utc": start + __import__("datetime").timedelta(days=1)},))
    loss_with_seed = resolve_common_pretest_period((observed_before,), minimum_common_days=14)
    assert not loss_with_seed.available
    assert any(item["reason"] == "OBSERVATION_LOSS_SUSPECTED" for item in loss_with_seed.exclusions)


def test_current_result_prior_action_without_prior_equity_is_invalid_seed() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": start + __import__("datetime").timedelta(days=14),
        "initial_balance": Decimal("100"),
        "actions": ({"timestamp_utc": start},),
        "equity": ({"timestamp_utc": start + __import__("datetime").timedelta(days=1), "equity": Decimal("101")},),
    }
    result = resolve_common_pretest_period((row,), minimum_common_days=14)
    assert not result.available
    assert any(item["reason"] == "INVALID_START_SEED" for item in result.exclusions)


def test_current_result_tied_latest_start_offenders_are_peeled_deterministically() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    def row(strategy_id: int, report_start: datetime, report_end: datetime) -> dict:
        return {
            "symbol": chr(64 + strategy_id), "side": "LONG", "strategy_id": strategy_id, "result_id": strategy_id,
            "report_start_utc": report_start, "report_end_utc": report_end,
            "initial_balance": Decimal("100"),
            "equity": ({"timestamp_utc": report_start, "equity": Decimal("100")},),
        }
    result = resolve_common_pretest_period(
        (row(2, start + __import__("datetime").timedelta(days=16), start + __import__("datetime").timedelta(days=28)),
             row(1, start + __import__("datetime").timedelta(days=16), start + __import__("datetime").timedelta(days=28)),
         row(3, start, start + __import__("datetime").timedelta(days=28))),
        minimum_common_days=14,
    )
    assert result.available
    assert [item["key"] for item in result.exclusions] == ["A:LONG:1:1", "B:LONG:2:2"]
    assert result.evidence["excluded_identities"] == ("A:LONG:1:1", "B:LONG:2:2")


def test_current_result_common_period_shorter_than_minimum_fails_closed() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
        "report_start_utc": start, "report_end_utc": start + __import__("datetime").timedelta(days=13),
        "initial_balance": Decimal("100"),
        "equity": ({"timestamp_utc": start, "equity": Decimal("100")},),
    }
    result = resolve_common_pretest_period((row,), minimum_common_days=14)
    assert not result.available
    assert result.reason == "COMMON_PRETEST_PERIOD_UNAVAILABLE"
