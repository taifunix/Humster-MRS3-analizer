from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from mrs3.performance_v2_selection import (
    SelectionConfig,
    _selection_windows,
    parse_selection_request,
    prepare_selection_window_cache,
)
from mrs3.portfolio import input as portfolio_input
from mrs3.portfolio.input import (
    SOURCE_SNAPSHOT_UNAVAILABLE,
    DecisionCampaign,
    PortfolioInputError,
    fresh_decision_campaign,
    read_performance_snapshot,
)
from mrs3.portfolio.store import PortfolioStore
from tests.test_performance_v2_selection import _candidate_db


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


def test_snapshot_fails_closed_when_current_review_is_missing(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("delete from selection_review_rows")

    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST])

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


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


def test_snapshot_fails_closed_when_current_review_timestamps_tie(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "insert into selection_review_imports values ('review-tie', 'run-default', 'review-hash-tie', ?, 1)",
            [datetime(2026, 1, 1, tzinfo=timezone.utc)],
        )

    with pytest.raises(PortfolioInputError) as error:
        _snapshot(database, [REQUEST])

    assert error.value.code == SOURCE_SNAPSHOT_UNAVAILABLE


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

    assert snapshot.source_schema_version == "4"
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
        connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
        connection.executemany(
            "insert into strategy_actions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "insert into strategy_actions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
