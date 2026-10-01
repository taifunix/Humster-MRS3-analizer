from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from io import BytesIO
from pathlib import Path

import duckdb
from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook
import pandas as pd
import pytest

from mrs3.performance_v2_selection import (
    SelectionConfig, parse_selection_request, retest_cohort_request,
    run_selection, write_selection_workbook,
)
from mrs3.performance_v2_equity_cache import (
    current_equity_source_metadata, encode_equity_facts, equity_source_revision,
)
from mrs3.performance_v2_equity_quality import EquitySample, calculate_equity_quality_facts
from mrs3.performance_v2_selection_review import (
    META_SHEET,
    SelectionReviewError,
    apply_prior_rejected,
    canonical_contract,
    import_retest_tags,
    import_selection_review,
    latest_effective_finalists,
    latest_user_reviews_by_strategy,
    new_run_metadata,
    persist_selection_snapshot,
    effective_selection_decisions,
)
from mrs3.panel import PanelController
from mrs3.performance_v2_store import initialize_performance_v2


def _database(tmp_path: Path, *, filename: str = "performance.duckdb") -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(str(tmp_path / filename))
    initialize_performance_v2(connection)
    now = datetime(2026, 9, 2, tzinfo=UTC)
    for strategy_id in (1, 2):
        connection.execute(
            """insert into strategies values (?, ?, 'BTCUSDT', 'LONG', '1h', 5, 1, 'run', ?, 'ACTIVE', null, ?, ?)""",
            [strategy_id, f"strategy-{strategy_id}", f"candidate-{strategy_id}", now, now],
        )
        connection.execute(
            """insert into strategy_results (
                result_id, strategy_id, report_start_utc, report_end_utc, exchange,
                commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
                max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc
            ) values (?, ?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 1, 10, ?)""",
            [100 + strategy_id, strategy_id, now, now, now],
        )
        connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [100 + strategy_id, strategy_id])
    return connection


def _request():
    return parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1},
    ]})


def _result() -> pd.DataFrame:
    return pd.DataFrame([
        {"strategy_id": 1, "result_id": 101, "strategy_name": "strategy-1", "symbol": "BTCUSDT", "side": "LONG",
         "timeframe": "1h", "order_count": 1, "close_ma_len": 5, "auto_status": "FINALIST", "finalist": True,
         "final_rank": 1, "final_score": 90.0, "elimination_reason": None, "analog_group_key": '["a"]',
         "auto_analog_of_strategy_id": None, "prior_rejected": False, "eliminated_by_rank_robust_top_n": False},
        {"strategy_id": 2, "result_id": 102, "strategy_name": "strategy-2", "symbol": "BTCUSDT", "side": "LONG",
         "timeframe": "1h", "order_count": 1, "close_ma_len": 5, "auto_status": "ANALOG", "finalist": False,
         "final_rank": None, "final_score": 80.0, "elimination_reason": "ANALOG", "analog_group_key": '["a"]',
         "auto_analog_of_strategy_id": 1, "prior_rejected": False, "eliminated_by_rank_robust_top_n": True},
    ])


def test_hard_cutoff_publication_tags_only_actual_exclusion(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"},
    ]})
    result = _result()
    result["eliminated_by_filter_hard_cutoffs"] = [True, False]
    result.loc[0, "auto_status"] = "FILTERED"
    result.loc[0, "elimination_reason"] = 'FILTER_HARD_CUTOFFS:{"triggered":["PNL30_FLOOR"]}'
    metadata = new_run_metadata(connection, request)

    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, b"workbook")

    assert connection.execute(
        "select strategy_id, source, source_ref from strategy_tags where tag = 'REJECTED'"
    ).fetchall() == [(1, "SELECTION_HARD_CUTOFF", metadata["selection_run_id"])]


def test_hard_cutoff_engine_result_publishes_tag_and_reason(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"},
    ]})
    candidates = _result().drop(columns=["auto_status", "finalist", "elimination_reason"]).assign(
        max_drawdown_pct=[24, 5], pnl_30d_pct=[5, 10],
        history_days=[30, 30], completed_cycle_count=[0, 0],
    )
    result = run_selection(candidates, request)
    metadata = new_run_metadata(connection, request)
    assert result.loc[0, "eliminated_by_filter_hard_cutoffs"]
    assert result.loc[0, "auto_status"] == "FILTERED"
    reason = result.loc[0, "elimination_reason"]
    assert reason.startswith("FILTER_HARD_CUTOFFS:")
    evidence = json.loads(reason.partition(":")[2])
    assert evidence["triggered"] == ["DD_PROFIT_GUARD"]
    assert evidence["values"]["full_dd_pct"] == "24"
    assert evidence["values"]["full_pnl30_pct"] == "5"

    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, b"workbook")

    assert connection.execute(
        "select strategy_id from strategy_tags where tag = 'REJECTED'"
    ).fetchall() == [(1,)]
    assert "DD_PROFIT_GUARD" in connection.execute(
        "select auto_reason from selection_results where strategy_id = 1"
    ).fetchone()[0]


def test_hard_cutoff_tag_overrides_older_manual_status(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"]).value = "FINALIST"
    sheet.cell(2, headers["User Rank"]).value = 1
    edited = BytesIO()
    workbook.save(edited)
    import_selection_review(connection, edited.getvalue())
    connection.execute(
        "insert into strategy_tags (strategy_id, tag, source, source_ref, updated_at_utc) "
        "values (1, 'REJECTED', 'SELECTION_HARD_CUTOFF', 'new-run', current_timestamp)"
    )

    assert effective_selection_decisions(connection)[1][:2] == ("REJECTED", 1)


def test_hard_cutoff_tag_write_failure_rolls_back_snapshot(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"},
    ]})
    result = _result()
    result["eliminated_by_filter_hard_cutoffs"] = [True, False]
    result.loc[0, "auto_status"] = "FILTERED"
    result.loc[0, "elimination_reason"] = 'FILTER_HARD_CUTOFFS:{"triggered":["PNL30_FLOOR"]}'

    class FailingTagConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, parameters=None):
            if "insert into strategy_tags" in sql.lower():
                raise RuntimeError("tag write failed")
            return connection.execute(sql, parameters) if parameters is not None else connection.execute(sql)

    with pytest.raises(RuntimeError, match="tag write failed"):
        persist_selection_snapshot(
            FailingTagConnection(), request, SelectionConfig(), result,
            new_run_metadata(connection, request), b"workbook",
        )
    for table in ("selection_runs", "selection_results", "strategy_tags"):
        assert connection.execute(f"select count(*) from {table}").fetchone() == (0,)


def test_selection_publication_rechecks_loaded_source_revision(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request = _request()
    result = _result()
    from mrs3.performance_v2_selection_review import _current_equity_revisions
    result.attrs["source_revisions"] = _current_equity_revisions(connection, [1, 2])
    connection.execute("update strategy_results set report_end_utc = report_end_utc + interval 1 second where result_id = 101")

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_STALE_RESULTS"):
        persist_selection_snapshot(
            connection, request, SelectionConfig(), result,
            new_run_metadata(connection, request), b"workbook",
        )
    for table in ("selection_runs", "selection_results", "strategy_tags"):
        assert connection.execute(f"select count(*) from {table}").fetchone() == (0,)


def test_catalog_exposes_selection_thresholds_without_database(tmp_path: Path) -> None:
    local_config = tmp_path / "config.local.json"
    local_config.write_text(json.dumps({"panel_paths": {"performance_db_root": "legacy"}}), encoding="utf-8")
    (tmp_path / "config.performance.json").write_text(json.dumps({
        "unified_performance_v2": {
            "database_root": "missing",
            "finalist_selection": {"hard_dd_pct": 27, "hard_dd_profit_multiplier": 4},
        },
    }), encoding="utf-8")

    catalog = PanelController(tmp_path, local_config).strategies_performance_v2_catalog()

    assert catalog["strategies"] == []
    assert catalog["selection_config"]["hard_dd_pct"] == "27"
    assert catalog["selection_config"]["hard_dd_profit_multiplier"] == "4"


def test_panel_xlsx_shows_pending_hard_rejection_after_publication(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "panel"
    database_root = root / "data"
    database_root.mkdir(parents=True)
    connection = _database(database_root, filename="strategy_performance.duckdb")
    connection.close()
    local_config = root / "config.local.json"
    local_config.write_text(json.dumps({"panel_paths": {"performance_db_root": "legacy"}}), encoding="utf-8")
    (root / "config.performance.json").write_text(
        json.dumps({"unified_performance_v2": {"database_root": "data", "workers": 1}}), encoding="utf-8"
    )
    controller = PanelController(root, local_config)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"},
    ]})
    result = _result()
    result["eliminated_by_filter_hard_cutoffs"] = [True, False]
    result.loc[0, "auto_status"] = "FILTERED"
    result.loc[0, "finalist"] = False
    result.loc[0, "elimination_reason"] = 'FILTER_HARD_CUTOFFS:{"triggered":["PNL30_FLOOR"]}'
    monkeypatch.setattr(controller, "_performance_v2_selection_result", lambda _payload: (request, result))

    _, data = controller.strategies_performance_v2_selection({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    sheet = load_workbook(BytesIO(data))["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    values = {
        sheet.cell(row, headers["ID"]).value: (
            sheet.cell(row, headers["User Status"]).value,
            sheet.cell(row, headers["Auto Status"]).value,
        )
        for row in range(2, sheet.max_row + 1)
    }
    assert values[1] == ("REJECTED", "FILTERED")
    with duckdb.connect(str(database_root / "strategy_performance.duckdb"), read_only=True) as check:
        assert check.execute(
            "select source from strategy_tags where strategy_id = 1 and tag = 'REJECTED'"
        ).fetchone() == ("SELECTION_HARD_CUTOFF",)


def _export(connection: duckdb.DuckDBPyConnection, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    request = _request()
    result = _result()
    metadata = new_run_metadata(connection)
    completed_review = {
        int(row.strategy_id): {
            "user_status": str(row.auto_status),
            "user_rank": row.final_rank if row.auto_status in {"FINALIST", "RESERVE"} else None,
            "user_analog_of_strategy_id": row.auto_analog_of_strategy_id if row.auto_status == "ANALOG" else None,
            "comment": None,
        }
        for row in result.itertuples()
    }
    path = write_selection_workbook(result, tmp_path / "review.xlsx", request, metadata, completed_review)
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())
    return path, metadata


def _equity_ranked_result(connection: duckdb.DuckDBPyConnection) -> tuple[object, pd.DataFrame]:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    result = _result().copy()
    result["_equity_cache"] = None
    for row_index in result.index:
        result_id = int(result.at[row_index, "result_id"])
        source = current_equity_source_metadata(connection, result_id)
        facts = calculate_equity_quality_facts(
            result_id, source["report_start_utc"], source["report_end_utc"], (),
        )
        facts_json = encode_equity_facts(facts)
        result.at[row_index, "_equity_cache"] = {
            "status": "FRESH",
            "facts": facts,
            "source_revision": equity_source_revision(source),
            "facts_sha256": sha256(facts_json.encode()).hexdigest(),
        }
    return request, run_selection(apply_prior_rejected(connection, result), request)


def _review_rows(result: pd.DataFrame) -> dict[int, dict[str, object]]:
    return {
        int(row.strategy_id): {
            "user_status": str(row.auto_status),
            "user_rank": row.final_rank if row.auto_status in {"FINALIST", "RESERVE"} else None,
            "user_analog_of_strategy_id": row.auto_analog_of_strategy_id if row.auto_status == "ANALOG" else None,
            "comment": None,
        }
        for row in result.itertuples()
    }


class _CountingConnection:
    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, parameters: object = None):
        self.calls.append((str(sql), parameters))
        if parameters is None:
            return self.connection.execute(sql)
        return self.connection.execute(sql, parameters)

    def cursor(self):
        raise AssertionError("effective_selection_decisions must not create a child cursor")


class _BatchCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.consumed = False

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        assert size == 1024
        if self.consumed:
            return []
        self.consumed = True
        return self.rows


class _ReorderedResultConnection(_CountingConnection):
    def __init__(self, connection: duckdb.DuckDBPyConnection, rows: list[tuple[object, ...]]) -> None:
        super().__init__(connection)
        self.rows = rows

    def execute(self, sql: str, parameters: object = None):
        result = super().execute(sql, parameters)
        if "select results.selection_run_id" in sql.lower():
            result.fetchall()
            return _BatchCursor(self.rows)
        return result


class _FetchManyProbe:
    def __init__(self, relation: object, sizes: list[int]) -> None:
        self.relation = relation
        self.sizes = sizes

    def fetchmany(self, size: int):
        self.sizes.append(size)
        return self.relation.fetchmany(size)


class _FetchManyCountingConnection(_CountingConnection):
    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        super().__init__(connection)
        self.fetchmany_sizes: list[int] = []

    def execute(self, sql: str, parameters: object = None):
        result = super().execute(sql, parameters)
        if "select results.selection_run_id" in sql.lower():
            return _FetchManyProbe(result, self.fetchmany_sizes)
        return result


def _insert_history_fixture(connection: duckdb.DuckDBPyConnection, count: int) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        run_id = f"history-run-{index:03d}"
        connection.execute(
            """insert into selection_runs values (
                ?, 'db', 'BTCUSDT', 'LONG', 'v', '{}', 'request', '{}', 'config',
                1, 0, 0, 1, ?, ?
            )""",
            [run_id, f"workbook-{index}", start + timedelta(seconds=index)],
        )
        connection.execute(
            """insert into selection_results values (
                ?, 1, 101, 'FINALIST', 0, 1, null, null, null, true, '{}'
            )""",
            [run_id],
        )


@pytest.mark.parametrize("run_count", [2, 102])
@pytest.mark.parametrize("symbol", [None, "BTCUSDT"])
def test_effective_selection_decisions_bulk_history_uses_five_parent_reads(
    tmp_path: Path, run_count: int, symbol: str | None,
) -> None:
    connection = _database(tmp_path)
    _insert_history_fixture(connection, run_count)
    counted = _CountingConnection(connection)

    expected = {
        1: ("REJECTED", None, f"history-run-{run_count - 1:03d}"),
    }
    actual = effective_selection_decisions(counted, symbol=symbol)
    assert actual == expected
    expected_digest = {
        2: "c93c30105a37cf4d6d1f3c9bd5d3d160f9be351dafe64ac6f794f4b6a17ab2da",
        102: "c520caf0a6040741611470b8c5f3873e0119e7c5783f3f93ffcdb4cea2fc1cfd",
    }[run_count]
    assert sha256(repr(list(actual.items())).encode()).hexdigest() == expected_digest
    assert len(counted.calls) <= 5
    assert not any(" in (" in sql.lower() for sql, _ in counted.calls)
    if symbol is None:
        assert all(parameters in (None, []) for _, parameters in counted.calls)
    else:
        assert sum(parameters == ["BTCUSDT"] for _, parameters in counted.calls) == 3


def test_effective_selection_decisions_ignores_unknown_result_runs(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    _insert_decision_run(connection, "known-run", start, "{}", result_rows=((1, True),))
    reordered = _ReorderedResultConnection(
        connection,
        [("unknown-run", 999, True), ("known-run", 1, True)],
    )

    assert effective_selection_decisions(reordered) == {
        1: ("REJECTED", None, "known-run"),
    }


def test_effective_selection_decisions_rejects_backward_known_result_order(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    _insert_decision_run(connection, "early-run", start, "{}", result_rows=((1, True),))
    _insert_decision_run(connection, "late-run", start + timedelta(seconds=1), "{}", result_rows=((2, True),))
    reordered = _ReorderedResultConnection(
        connection,
        [("late-run", 2, True), ("early-run", 1, True)],
    )

    with pytest.raises(ValueError, match="selection results out of order"):
        effective_selection_decisions(reordered)


def _insert_decision_run(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    created_at: datetime,
    request_json: str,
    *,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    result_rows: tuple[tuple[int, bool], ...] = (),
) -> None:
    connection.execute(
        """insert into selection_runs values (
            ?, 'db', ?, ?, 'v', ?, 'request', '{}', 'config',
            ?, 0, 0, 1, ?, ?
        )""",
        [run_id, symbol, side, request_json, len(result_rows), f"workbook-{run_id}", created_at],
    )
    for strategy_id, prior_rejected in result_rows:
        connection.execute(
            """insert into selection_results values (
                ?, ?, ?, 'FINALIST', 0, 1, null, null, null, ?, '{}'
            )""",
            [run_id, strategy_id, 100 + strategy_id, prior_rejected],
        )


def test_effective_selection_decisions_bulk_replay_preserves_resets_overlays_ties_and_lineage(
    tmp_path: Path,
) -> None:
    connection = _database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    _insert_decision_run(connection, "run-base-a", start, "{}", result_rows=((1, True), (2, False)))
    _insert_decision_run(connection, "run-empty", start + timedelta(seconds=1), "{}")
    _insert_decision_run(connection, "run-base-b", start + timedelta(seconds=2), "{}", result_rows=((1, True), (2, False)))
    _insert_decision_run(
        connection, "run-dormant", start + timedelta(seconds=3),
        '{"ranking_scope":"RETEST_COHORT"}', result_rows=((1, False), (2, False)),
    )
    _insert_decision_run(
        connection, "run-empty-review", start + timedelta(seconds=4),
        '{"ranking_scope":"CURRENT_EFFECTIVE"}', result_rows=((1, False), (2, False)),
    )
    connection.execute(
        "insert into selection_review_imports values ('review-empty', 'run-empty-review', 'hash-empty', ?, 0)",
        [start + timedelta(seconds=5)],
    )
    _insert_decision_run(
        connection, "run-reviewed", start + timedelta(seconds=6),
        '{"ranking_scope":"RETEST_COHORT"}', result_rows=((1, False), (2, False)),
    )
    tie_time = start + timedelta(seconds=7)
    connection.execute(
        "insert into selection_review_imports values ('review-active-a', 'run-reviewed', 'hash-a', ?, 1)",
        [tie_time],
    )
    connection.execute(
        "insert into selection_review_rows values ('review-active-a', 1, 'FINALIST', 3, null, null)",
    )
    connection.execute(
        "insert into selection_review_imports values ('review-active-z', 'run-reviewed', 'hash-z', ?, 2)",
        [tie_time],
    )
    connection.execute(
        """insert into selection_review_rows values
            ('review-active-z', 1, 'RESERVE', 9, null, null),
            ('review-active-z', 2, 'FINALIST', 8, null, null)""",
    )
    _insert_decision_run(
        connection, "run-other", start + timedelta(seconds=8), "{}", symbol="ETHUSDT", side="SHORT",
        result_rows=((3, True),),
    )
    _insert_decision_run(
        connection, "run-malformed", start + timedelta(seconds=9), "not-json", symbol="ETHUSDT", side="SHORT",
    )
    _insert_decision_run(
        connection, "run-populated-final", start + timedelta(seconds=10), "{}", symbol="XRPUSDT",
        result_rows=((5, True),),
    )
    _insert_decision_run(
        connection, "run-empty-final", start + timedelta(seconds=11), "{}", symbol="XRPUSDT",
    )
    _insert_decision_run(
        connection, "run-other-side", start + timedelta(seconds=12), "{}", symbol="BTCUSDT", side="SHORT",
        result_rows=((4, True),),
    )

    expected = {
        1: ("RESERVE", 9, "run-reviewed"),
        2: ("FINALIST", 8, "run-reviewed"),
        4: ("REJECTED", None, "run-other-side"),
    }
    decisions = effective_selection_decisions(connection)
    assert decisions == expected
    ordered = list(decisions.items())
    assert ordered == [
        (1, ("RESERVE", 9, "run-reviewed")),
        (2, ("FINALIST", 8, "run-reviewed")),
        (4, ("REJECTED", None, "run-other-side")),
    ]
    assert sha256(repr(ordered).encode()).hexdigest() == "4cb2db599aa3888d6588f791e01fce05e9652e7261f04b313028af53b39f1e6a"


def test_effective_selection_decisions_102_run_mixed_ordered_digest(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(102):
        run_id = f"mixed-run-{index:03d}"
        if index == 0:
            _insert_decision_run(
                connection, run_id, start + timedelta(seconds=index), "{}", symbol="XRPUSDT",
                result_rows=((3, True),),
            )
        elif index == 1:
            _insert_decision_run(
                connection, run_id, start + timedelta(seconds=index), "{}", symbol="XRPUSDT",
            )
        elif index == 101:
            _insert_decision_run(
                connection, run_id, start + timedelta(seconds=index),
                '{"ranking_scope":"CURRENT_EFFECTIVE"}', result_rows=((1, False), (2, False)),
            )
        elif index == 100:
            _insert_decision_run(
                connection, run_id, start + timedelta(seconds=index), "{}", result_rows=((1, True),),
            )
        else:
            _insert_decision_run(connection, run_id, start + timedelta(seconds=index), "{}")
    review_time = start + timedelta(seconds=102)
    connection.execute(
        "insert into selection_review_imports values ('mixed-review', 'mixed-run-101', 'mixed-hash', ?, 2)",
        [review_time],
    )
    connection.execute(
        """insert into selection_review_rows values
            ('mixed-review', 1, 'FINALIST', 7, null, null),
            ('mixed-review', 2, 'RESERVE', 8, null, null)""",
    )

    decisions = effective_selection_decisions(connection)
    ordered = list(decisions.items())
    assert ordered == [
        (1, ("FINALIST", 7, "mixed-run-101")),
        (2, ("RESERVE", 8, "mixed-run-101")),
    ]
    assert sha256(repr(ordered).encode()).hexdigest() == "04bf46058b7f9cfb4201c924ac9c32ecd423dbb8e126679a49407395aeb1ddd6"


def test_effective_selection_decisions_scoped_symbol_keeps_global_latest_review_and_lineage(
    tmp_path: Path,
) -> None:
    connection = _database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    _insert_decision_run(
        connection, "eth-base", start, "{}", symbol="ETHUSDT", side="LONG",
        result_rows=((11, True), (12, True), (13, True)),
    )
    _insert_decision_run(
        connection, "eth-dormant", start + timedelta(seconds=1),
        '{"ranking_scope":"RETEST_COHORT"}', symbol="ETHUSDT", side="LONG",
        result_rows=((11, False), (12, False), (13, False)),
    )
    _insert_decision_run(
        connection, "eth-reviewed", start + timedelta(seconds=2),
        '{"ranking_scope":"CURRENT_EFFECTIVE"}', symbol="ETHUSDT", side="LONG",
        result_rows=((11, False), (12, False), (13, False)),
    )
    _insert_decision_run(
        connection, "btc-base", start + timedelta(seconds=3), "{}", symbol="BTCUSDT", side="LONG",
        result_rows=((11, True), (20, True), (21, True)),
    )
    _insert_decision_run(
        connection, "btc-dormant", start + timedelta(seconds=4),
        '{"ranking_scope":"RETEST_COHORT"}', symbol="BTCUSDT", side="LONG",
        result_rows=((11, False), (20, False), (21, False)),
    )
    _insert_decision_run(
        connection, "btc-reviewed", start + timedelta(seconds=5),
        '{"ranking_scope":"CURRENT_EFFECTIVE"}', symbol="BTCUSDT", side="LONG",
        result_rows=((11, False), (20, False), (21, False)),
    )
    connection.execute(
        "insert into selection_review_imports values ('btc-review', 'btc-reviewed', 'btc-hash', ?, 1)",
        [start + timedelta(seconds=50)],
    )
    connection.execute(
        "insert into selection_review_rows values ('btc-review', 11, 'FINALIST', 1, null, null)",
    )
    connection.execute(
        "insert into selection_review_imports values ('eth-review', 'eth-reviewed', 'eth-hash', ?, 2)",
        [start + timedelta(seconds=60)],
    )
    connection.execute(
        """insert into selection_review_rows values
            ('eth-review', 11, 'RESERVE', 88, null, null),
            ('eth-review', 12, 'FINALIST', 77, null, null)""",
    )

    all_decisions = effective_selection_decisions(connection)
    counted = _CountingConnection(connection)
    btc_decisions = effective_selection_decisions(counted, symbol="BTCUSDT")
    btc_lineage = {
        strategy_id: decision
        for strategy_id, decision in all_decisions.items()
        if str(decision[2]).startswith("btc-")
    }

    assert btc_decisions == btc_lineage == {
        11: ("RESERVE", 88, "btc-reviewed"),
        20: ("REJECTED", None, "btc-base"),
        21: ("REJECTED", None, "btc-base"),
    }
    assert 12 not in btc_decisions
    assert all_decisions[12] == ("FINALIST", 77, "eth-reviewed")
    assert len(counted.calls) == 5
    assert sum(parameters == ["BTCUSDT"] for _, parameters in counted.calls) == 3


def test_effective_selection_decisions_streams_results_across_fetchmany_boundary(
    tmp_path: Path,
) -> None:
    connection = _database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    _insert_decision_run(connection, "wide-base", start, "{}")
    connection.execute(
        """insert into selection_results
           select 'wide-base', strategy_id, 100000 + strategy_id, 'FINALIST', 0, 1,
                  null, null, null, true, '{}'
             from range(1, 1026) as values(strategy_id)""",
    )
    connection.execute("update selection_runs set candidate_count = 1025 where selection_run_id = 'wide-base'")
    _insert_decision_run(
        connection, "wide-overlay", start + timedelta(seconds=1),
        '{"ranking_scope":"RETEST_COHORT"}', result_rows=((1025, False),),
    )
    connection.execute(
        "insert into selection_review_imports values ('wide-review', 'wide-overlay', 'wide-hash', ?, 1)",
        [start + timedelta(seconds=2)],
    )
    connection.execute(
        "insert into selection_review_rows values ('wide-review', 1025, 'FINALIST', 7, null, null)",
    )
    counted = _FetchManyCountingConnection(connection)

    decisions = effective_selection_decisions(counted)
    ordered = list(decisions.items())
    expected = [(strategy_id, ("REJECTED", None, "wide-base")) for strategy_id in range(1, 1025)]
    expected.append((1025, ("FINALIST", 7, "wide-overlay")))
    assert ordered == expected
    assert sha256(repr(ordered).encode()).hexdigest() == "910176a0c0df2ff052a8732a573c6b78644a9440aa9c74ce10a872fd2633593e"
    assert len(counted.calls) == 5
    assert counted.fetchmany_sizes and all(size == 1024 for size in counted.fetchmany_sizes)
    assert len(counted.fetchmany_sizes) >= 2


def test_effective_selection_decisions_sees_uncommitted_rows_and_rollback_removes_them(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    connection.execute("begin transaction")
    _insert_decision_run(
        connection, "uncommitted-run", datetime(2026, 1, 1, tzinfo=UTC), "{}", result_rows=((9, True),),
    )
    assert effective_selection_decisions(connection) == {
        9: ("REJECTED", None, "uncommitted-run"),
    }
    connection.execute("rollback")
    assert effective_selection_decisions(connection) == {}


def test_effective_selection_decisions_fourth_read_failure_leaves_history_unchanged(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _insert_history_fixture(connection, 2)
    before = {
        table: connection.execute(f"select * from {table}").fetchall()
        for table in ("selection_runs", "selection_results", "selection_review_imports", "selection_review_rows")
    }

    class FailingConnection(_CountingConnection):
        def execute(self, sql: str, parameters: object = None):
            if len(self.calls) == 3:
                raise RuntimeError("injected final read failure")
            return super().execute(sql, parameters)

    with pytest.raises(RuntimeError, match="injected final read failure"):
        effective_selection_decisions(FailingConnection(connection))
    after = {
        table: connection.execute(f"select * from {table}").fetchall()
        for table in ("selection_runs", "selection_results", "selection_review_imports", "selection_review_rows")
    }
    assert after == before


def test_review_export_serializes_workbook_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    saves = 0
    original_save = Workbook.save

    def counted_save(self: Workbook, filename: object) -> None:
        nonlocal saves
        saves += 1
        original_save(self, filename)

    monkeypatch.setattr(Workbook, "save", counted_save)
    path = write_selection_workbook(
        _result(), tmp_path / "single-pass.xlsx", _request(),
        {"selection_run_id": "fixed-run"}, _review_rows(_result()),
    )

    workbook = load_workbook(path)
    assert saves == 1
    assert workbook["_MRS_SELECTION_META"].sheet_state == "veryHidden"
    assert workbook["All candidates"].data_validations.count == 2
    assert workbook["Finalists"].data_validations.count == 2


def test_contract_hashes_are_canonical() -> None:
    first = canonical_contract(_request(), SelectionConfig())
    second = canonical_contract(_request(), SelectionConfig())
    assert first == second
    assert len(first[1]) == len(first[3]) == 64


def test_disabled_equity_quality_rank_keeps_v1_review_contract(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": False, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    result = run_selection(_result(), request)
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "disabled-equity-rank.xlsx", request, metadata, _review_rows(result))
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())
    stored_json, version = connection.execute(
        "select request_json, selection_contract_version from selection_runs where selection_run_id = ?",
        [metadata["selection_run_id"]],
    ).fetchone()
    stored_request = json.loads(stored_json)

    assert new_run_metadata(connection, request)["selection_contract_version"] == "performance-v2-selection-review-v1"
    assert version == "performance-v2-selection-review-v1"
    assert "equity_quality_snapshot" not in stored_request
    assert "method" not in stored_request["stages"][0]
    assert import_selection_review(connection, path.read_bytes())["row_count"] == 2


def test_equity_quality_review_revision_is_stable_in_non_utc_connection_timezone(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    connection.execute("set TimeZone = 'America/Los_Angeles'")
    request, result = _equity_ranked_result(connection)
    source = result.attrs["equity_quality_facts"]["1"]
    current = current_equity_source_metadata(connection, 101)
    assert current["report_start_utc"].utcoffset().total_seconds() == 0
    assert source["source_revision"] == equity_source_revision(current)

    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "non-utc-equity-review.xlsx", request, metadata, _review_rows(result))
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())

    assert import_selection_review(connection, path.read_bytes())["row_count"] == 2


def test_equity_quality_review_v2_round_trips_cached_decision_evidence(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request, result = _equity_ranked_result(connection)
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "equity-review.xlsx", request, metadata, _review_rows(result))
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())
    request_json, contract_version = connection.execute(
        "select request_json, selection_contract_version from selection_runs"
    ).fetchone()
    stored = json.loads(request_json)

    assert contract_version == "performance-v2-selection-review-v2"
    assert stored["stages"][-1]["method"] == "equity_quality_v1"
    assert stored["equity_quality_snapshot"]["policy_version"] == "equity-quality-rank-v1"
    assert stored["equity_quality_snapshot"]["effective_stage_order"] == ["filter_lot_variant_redundancy", "rank_robust_top_n"]
    source = stored["equity_quality_snapshot"]["sources"]["1"]
    assert source["result_id"] == 101
    assert len(source["source_revision"]) == len(source["facts_sha256"]) == 64
    assert source["decision_facts"]["state"] == "INSUFFICIENT_HISTORY"
    assert source["decision_facts"]["equity_class"] is None
    assert source["facts"]["result_id"] == 101
    assert import_selection_review(connection, path.read_bytes())["row_count"] == 2


def test_equity_review_import_accepts_historical_retired_time_stage(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request, result = _equity_ranked_result(connection)
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "legacy-time.xlsx", request, metadata, _review_rows(result))
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())
    document = json.loads(connection.execute(
        "select request_json from selection_runs where selection_run_id = ?", [metadata["selection_run_id"]]
    ).fetchone()[0])
    document.pop("effective_stage_order")
    document["stages"].insert(-1, {
        "id": "filter_time_consistency", "enabled": True, "scope": "pair_side_timeframe",
        "min_shift_pct": None, "pnl_tolerance_pct": None, "top_n": None,
    })
    document["equity_quality_snapshot"]["effective_stage_order"] = [
        "filter_lot_variant_redundancy", "filter_time_consistency", "rank_robust_top_n",
    ]
    connection.execute(
        "update selection_runs set request_json = ? where selection_run_id = ?",
        [json.dumps(document), metadata["selection_run_id"]],
    )

    assert import_selection_review(connection, path.read_bytes())["row_count"] == 2


def test_equity_quality_review_rejects_same_id_replacement_after_snapshot(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request, result = _equity_ranked_result(connection)
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "stale-equity-review.xlsx", request, metadata, _review_rows(result))
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())
    connection.execute("update strategy_results set imported_at_utc = ? where result_id = 101", [datetime(2026, 9, 3, tzinfo=UTC)])

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_STALE_RESULTS"):
        import_selection_review(connection, path.read_bytes())
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)


def test_equity_quality_review_rejects_non_mapping_saved_stage_as_schema_mismatch(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request, result = _equity_ranked_result(connection)
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "malformed-stage.xlsx", request, metadata, _review_rows(result))
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())
    run_id = metadata["selection_run_id"]
    request_document = json.loads(connection.execute(
        "select request_json from selection_runs where selection_run_id = ?", [run_id],
    ).fetchone()[0])
    request_document["stages"] = ["malformed"]
    connection.execute(
        "update selection_runs set request_json = ? where selection_run_id = ?",
        [json.dumps(request_document), run_id],
    )

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_SCHEMA_MISMATCH"):
        import_selection_review(connection, path.read_bytes())


@pytest.mark.parametrize(
    "stage_order",
    [
        ["filter_lot_variant_redundancy", "unknown_stage", "rank_robust_top_n"],
        ["rank_robust_top_n", "filter_lot_variant_redundancy"],
        ["filter_lot_variant_redundancy", 7, "rank_robust_top_n"],
    ],
)
def test_equity_quality_review_rejects_inconsistent_effective_stage_order(
    tmp_path: Path, stage_order: list[object],
) -> None:
    connection = _database(tmp_path)
    request, result = _equity_ranked_result(connection)
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "bad-stage-order.xlsx", request, metadata, _review_rows(result))
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())
    run_id = metadata["selection_run_id"]
    request_document = json.loads(connection.execute(
        "select request_json from selection_runs where selection_run_id = ?", [run_id],
    ).fetchone()[0])
    request_document["equity_quality_snapshot"]["effective_stage_order"] = stage_order
    connection.execute(
        "update selection_runs set request_json = ? where selection_run_id = ?",
        [json.dumps(request_document), run_id],
    )

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_SCHEMA_MISMATCH"):
        import_selection_review(connection, path.read_bytes())


def test_equity_quality_review_rejects_extra_decision_fact_key(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request, result = _equity_ranked_result(connection)
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "extra-decision-fact.xlsx", request, metadata, _review_rows(result))
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())
    run_id = metadata["selection_run_id"]
    request_document = json.loads(connection.execute(
        "select request_json from selection_runs where selection_run_id = ?", [run_id],
    ).fetchone()[0])
    request_document["equity_quality_snapshot"]["sources"]["1"]["decision_facts"]["unreviewed"] = True
    connection.execute(
        "update selection_runs set request_json = ? where selection_run_id = ?",
        [json.dumps(request_document), run_id],
    )

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_SCHEMA_MISMATCH"):
        import_selection_review(connection, path.read_bytes())


def test_equity_revision_import_rejects_naive_database_timestamps() -> None:
    from mrs3.performance_v2_selection_review import _current_equity_revisions

    naive = datetime(2026, 9, 2)

    class FakeCursor:
        def fetchall(self):
            return [(1, 101, naive, naive, naive, None, None, None)]

    class FakeConnection:
        def execute(self, *_args):
            return FakeCursor()

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_SCHEMA_MISMATCH"):
        _current_equity_revisions(FakeConnection(), [1])


def test_mixed_equity_retest_cohort_round_trips_unscoreable_reserve_evidence(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    now = datetime(2026, 9, 2, tzinfo=UTC)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=28)
    connection.execute(
        """insert into strategies values (3, 'strategy-3', 'BTCUSDT', 'LONG', '1h', 5, 1, 'run', ?, 'ACTIVE', null, ?, ?)""",
        ["candidate-3", now, now],
    )
    connection.execute(
        """insert into strategy_results (
            result_id, strategy_id, report_start_utc, report_end_utc, exchange,
            commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
            max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc
        ) values (103, 3, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 1, 10, ?)""",
        [start, end, now],
    )
    connection.execute("update strategies set current_result_id = 103 where strategy_id = 3")
    connection.execute("update strategy_results set report_start_utc = ?, report_end_utc = ?", [start, end])
    connection.execute("insert into strategy_tags values (3, 'REJECTED', 'SELECTION_REVIEW', 'older-run', ?)", [now])

    base_request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    request = retest_cohort_request(base_request, "mixed-review-cohort", {1: 101, 2: 102, 3: 103})
    config = SelectionConfig(lot_variant_redundancy_enabled=False)
    result = pd.concat([_result().iloc[[0]], _result().iloc[[0]], _result().iloc[[0]]], ignore_index=True)
    result["strategy_id"] = [1, 2, 3]
    result["result_id"] = [101, 102, 103]
    result["strategy_name"] = ["strategy-1", "strategy-2", "strategy-3"]
    result["auto_analog_of_strategy_id"] = None
    result["_equity_cache"] = None
    for row_index in result.index:
        result_id = int(result.at[row_index, "result_id"])
        source = current_equity_source_metadata(connection, result_id)
        samples = () if result_id == 103 else tuple(
            EquitySample(result_id, sample_index, start + timedelta(hours=6 * sample_index),
                         Decimal(100) + Decimal(sample_index) * Decimal("0.1" if result_id == 101 else "0.05"))
            for sample_index in range(113)
        )
        facts = calculate_equity_quality_facts(result_id, source["report_start_utc"], source["report_end_utc"], samples)
        facts_json = encode_equity_facts(facts)
        result.at[row_index, "_equity_cache"] = {
            "status": "FRESH",
            "facts": facts,
            "source_revision": equity_source_revision(source),
            "facts_sha256": sha256(facts_json.encode()).hexdigest(),
        }
    result = run_selection(apply_prior_rejected(connection, result), request, config)
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "mixed-equity-review.xlsx", request, metadata, _review_rows(result))
    persist_selection_snapshot(connection, request, config, result, metadata, path.read_bytes())
    request_json = connection.execute(
        "select request_json from selection_runs where selection_run_id = ?", [metadata["selection_run_id"]],
    ).fetchone()[0]
    stored = json.loads(request_json)

    decisions = result.set_index("strategy_id")
    assert decisions.loc[3, "auto_status"] == "RESERVE"
    assert decisions.loc[3, "elimination_reason"] == "PRIOR_USER_REJECTED"
    assert pd.isna(decisions.loc[3, "final_rank"])
    assert decisions.loc[3, "final_score"] is None
    assert sorted(int(value) for value in decisions["final_rank"].dropna()) == [1, 2]
    assert all(isinstance(value, Decimal) for value in decisions.loc[[1, 2], "final_score"])
    sources = stored["equity_quality_snapshot"]["sources"]
    assert set(sources) == {"1", "2", "3"}
    assert sources["3"]["decision_facts"]["state"] == "MISSING_BASELINE"
    assert sources["3"]["decision_facts"]["score12"] is None
    assert import_selection_review(connection, path.read_bytes())["row_count"] == 3


def test_export_persists_exact_snapshot_and_review_contract(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, metadata = _export(connection, tmp_path)
    workbook = load_workbook(path)
    headers = [cell.value for cell in workbook["All candidates"][1]]

    assert workbook[META_SHEET].sheet_state == "veryHidden"
    assert {"Result ID", "Auto Status", "User Status", "Auto Rank", "User Rank", "Auto Analog Of ID", "Analog Of ID", "Comment"}.issubset(headers)
    assert connection.execute("select selection_run_id, candidate_count, auto_finalist_count from selection_runs").fetchone() == (metadata["selection_run_id"], 2, 1)
    assert connection.execute("select count(*) from selection_results").fetchone() == (2,)


def test_selection_workbook_has_compact_visible_order_and_hidden_analog_fields(tmp_path: Path) -> None:
    result = _result().copy()
    result.index = [17, 3]
    result["order_1_lot_x"] = Decimal("0.01")
    result["order_1_plateau_point_count"] = 2
    result["order_1_open_ma_len"] = 5
    result["equity_state"] = "GROWING"
    result["equity_basis"] = "28d / OK"
    result["equity_dd_pct"] = Decimal("2.5")
    result["equity_smoothness"] = Decimal("0.5")
    path = write_selection_workbook(
        result, tmp_path / "compact.xlsx", _request(),
        {"selection_run_id": "fixed-run"}, _review_rows(result),
    )
    workbook = load_workbook(path)
    repeat = write_selection_workbook(
        result, tmp_path / "compact-repeat.xlsx", _request(),
        {"selection_run_id": "fixed-run"}, _review_rows(result),
    )
    assert sha256(path.read_bytes()).digest() == sha256(repeat.read_bytes()).digest()
    assert workbook["Finalists"].max_row == 2
    assert workbook["Finalists"]["A2"].value == 1

    for sheet_name in ("All candidates", "Finalists"):
        sheet = workbook[sheet_name]
        headers = [cell.value for cell in sheet[1]]
        assert headers[-1] == "Причина"
        assert not any(str(header).startswith("eliminated_by_") for header in headers)
        assert not {"Final rank", "Final", "Lot variant group key", "Lot variant representative ID",
                    "4 Shift", "PointsALL", "PointsMin"}.intersection(headers)
        assert all(sheet.column_dimensions[sheet.cell(1, headers.index(header) + 1).column_letter].hidden
                   for header in ("Auto Analog Of ID", "Analog Of ID"))
        if sheet_name == "All candidates":
            assert sheet.cell(3, headers.index("Auto Analog Of ID") + 1).value == 1
            assert sheet.cell(3, headers.index("Analog Of ID") + 1).value == 1
        assert sheet.protection.sheet is False
        assert sheet.data_validations.count == 2
        visible = [header for cell, header in zip(sheet[1], headers)
                   if not sheet.column_dimensions[cell.column_letter].hidden]
        assert visible[-14:] == [
            "Lots", "Points", "MA", "Equity state", "Equity basis", "Equity DD, %",
            "Equity smoothness", "Auto Status", "Auto Rank", "User Status", "User Rank",
            "RETEST", "Comment", "Причина",
        ]
        for header in ("Lots", "Points", "MA"):
            column = headers.index(header) + 1
            value = sheet.cell(2, column).value
            assert sheet.column_dimensions[sheet.cell(1, column).column_letter].width >= max(
                len(header), len(str(value)) if value is not None else 0
            ) + 2


def test_review_import_still_accepts_legacy_retest_position(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    for row in sheet.iter_rows():
        rank_cell = row[headers["User Rank"] - 1]
        retest_cell = row[headers["RETEST"] - 1]
        rank_cell.value, retest_cell.value = retest_cell.value, rank_cell.value
    legacy = tmp_path / "legacy-retest-position.xlsx"
    workbook.save(legacy)

    assert import_selection_review(connection, legacy.read_bytes())["row_count"] == 2


def test_lots_width_fits_long_composite_value(tmp_path: Path) -> None:
    result = _result().iloc[[0]].copy()
    for order in range(1, 5):
        result[f"order_{order}_lot_x"] = Decimal("12345678901234567890")
    path = write_selection_workbook(result, tmp_path / "wide-lots.xlsx", _request())
    sheet = load_workbook(path)["All candidates"]
    headers = [cell.value for cell in sheet[1]]
    column = sheet.cell(1, headers.index("Lots") + 1).column_letter

    assert len(sheet[f"{column}2"].value) > 70
    assert sheet.column_dimensions[column].width >= len(sheet[f"{column}2"].value) + 2


def test_workbook_rejects_result_without_finalist_decision(tmp_path: Path) -> None:
    result = _result().drop(columns=["finalist"])

    with pytest.raises(KeyError, match="finalist"):
        write_selection_workbook(result, tmp_path / "missing-finalist.xlsx", _request())


def test_auto_only_snapshot_has_no_effective_user_decision_or_review_rows(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request = _request()
    result = _result()
    metadata = new_run_metadata(connection)
    path = write_selection_workbook(result, tmp_path / "automatic.xlsx", request, metadata)

    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())

    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    assert all(
        sheet.cell(row, headers[name]).value is None
        for row in range(2, sheet.max_row + 1)
        for name in ("User Status", "User Rank", "Analog Of ID", "Comment")
    )
    assert effective_selection_decisions(connection) == {}
    assert latest_effective_finalists(connection, "BTCUSDT") == (True, set())
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)
    assert connection.execute("select count(*) from selection_review_rows").fetchone() == (0,)


def test_prior_rejected_without_review_does_not_propagate_auto_rank(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request = _request()
    result = _result().iloc[[0]].copy()
    result.loc[:, "prior_rejected"] = True
    result.loc[:, "final_rank"] = 7
    metadata = new_run_metadata(connection)
    path = write_selection_workbook(result, tmp_path / "prior-rejected.xlsx", request, metadata)
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())

    assert effective_selection_decisions(connection) == {
        1: ("REJECTED", None, metadata["selection_run_id"]),
    }

    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"]).value = "FINALIST"
    sheet.cell(2, headers["User Rank"]).value = 1
    edited = BytesIO()
    workbook.save(edited)
    import_selection_review(connection, edited.getvalue())

    assert effective_selection_decisions(connection) == {
        1: ("FINALIST", 1, metadata["selection_run_id"]),
    }


def test_export_includes_current_retest_tag_and_editable_validation(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    connection.execute(
        "insert into strategy_tags values (1, 'RETEST', 'PERIOD_INTEGRITY_AUDIT', 'audit.xlsx', now())"
    )
    request = _request()
    metadata = new_run_metadata(connection)
    result = apply_prior_rejected(connection, _result())
    path = write_selection_workbook(result, tmp_path / "retest.xlsx", request, metadata)
    sheet = load_workbook(path)["All candidates"]
    headers = [cell.value for cell in sheet[1]]
    values = {sheet.cell(row, headers.index("ID") + 1).value: sheet.cell(row, headers.index("RETEST") + 1).value for row in range(2, sheet.max_row + 1)}

    assert headers[headers.index("User Rank") + 1] == "RETEST"
    assert values == {1: "RETEST", 2: None}
    retest_validation = next(validation for validation in sheet.data_validations.dataValidation if validation.formula1 == '"RETEST"')
    assert retest_validation.allow_blank
    assert str(retest_validation.sqref).endswith(f"{sheet.cell(sheet.max_row, headers.index('RETEST') + 1).column_letter}{sheet.max_row}")


def test_production_selection_export_preserves_existing_tags_on_round_trip(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "panel"
    database_root = root / "data"
    database_root.mkdir(parents=True)
    connection = _database(database_root, filename="strategy_performance.duckdb")
    connection.execute(
        "insert into strategy_tags values (1, 'REJECTED', 'SELECTION_REVIEW', 'old-review', now()), (1, 'RETEST', 'PERIOD_INTEGRITY_AUDIT', 'audit.xlsx', now())"
    )
    connection.close()
    local_config = root / "config.local.json"
    local_config.write_text(json.dumps({"panel_paths": {"performance_db_root": "legacy"}}), encoding="utf-8")
    (root / "config.performance.json").write_text(
        json.dumps({"unified_performance_v2": {"database_root": "data", "workers": 1}}), encoding="utf-8"
    )
    controller = PanelController(root, local_config)
    import mrs3.panel as panel_module
    monkeypatch.setattr(panel_module, "selection_cache_status", lambda *_args, **_kwargs: {"ready": True})
    monkeypatch.setattr(panel_module, "load_selection_candidates", lambda *_args, **_kwargs: _result())

    _, data = controller.strategies_performance_v2_selection({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    workbook = load_workbook(BytesIO(data))
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    exported = {sheet.cell(row, headers["ID"]).value: sheet.cell(row, headers["RETEST"]).value for row in range(2, sheet.max_row + 1)}
    assert exported == {1: "RETEST", 2: None}
    statuses = {sheet.cell(row, headers["ID"]).value: sheet.cell(row, headers["User Status"]).value
                for row in range(2, sheet.max_row + 1)}
    assert statuses == {1: "REJECTED", 2: None}
    assert all(
        sheet.cell(row, headers[name]).value is None
        for row in range(2, sheet.max_row + 1)
        for name in ("User Rank", "Analog Of ID", "Comment")
    )
    for row in range(2, sheet.max_row + 1):
        sheet.cell(row, headers["User Status"]).value = "REJECTED" if sheet.cell(row, headers["ID"]).value == 1 else "FILTERED"
    edited = BytesIO()
    workbook.save(edited)

    with duckdb.connect(str(database_root / "strategy_performance.duckdb")) as connection:
        response = import_selection_review(connection, edited.getvalue())
        assert connection.execute(
            "select strategy_id, tag, source, source_ref from strategy_tags order by strategy_id, tag"
        ).fetchall() == [
            (1, "REJECTED", "SELECTION_REVIEW", response["review_import_id"]),
            (1, "RETEST", "SELECTION_REVIEW", response["review_import_id"]),
        ]


def test_reviewed_decisions_survive_new_ordinary_run_and_result_replacement(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    first_path, _ = _export(connection, tmp_path)
    workbook = load_workbook(first_path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    ids = {sheet.cell(row, headers["ID"]).value: row for row in range(2, sheet.max_row + 1)}
    sheet.cell(ids[1], headers["User Status"], "FINALIST")
    sheet.cell(ids[1], headers["User Rank"], 1)
    sheet.cell(ids[1], headers["Comment"], "reviewed finalist")
    sheet.cell(ids[2], headers["User Status"], "RESERVE")
    sheet.cell(ids[2], headers["User Rank"]).value = None
    sheet.cell(ids[2], headers["Analog Of ID"]).value = None
    sheet.cell(ids[2], headers["Comment"], "reviewed reserve")
    reviewed = tmp_path / "reviewed.xlsx"
    workbook.save(reviewed)
    import_selection_review(connection, reviewed.read_bytes())

    now = datetime(2026, 9, 3, tzinfo=UTC)
    connection.execute(
        """insert into strategies values (3, 'strategy-3', 'BTCUSDT', 'LONG', '1h', 5, 1, 'run',
           'candidate-3', 'ACTIVE', null, ?, ?)""", [now, now]
    )
    connection.execute("update strategies set current_result_id = null where strategy_id in (1, 2)")
    connection.execute("delete from strategy_results where result_id in (101, 102)")
    for strategy_id, result_id in ((1, 201), (2, 202), (3, 103)):
        connection.execute(
            """insert into strategy_results (
                result_id, strategy_id, report_start_utc, report_end_utc, exchange,
                commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
                max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc
            ) values (?, ?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 1, 10, ?)""",
            [result_id, strategy_id, now, now, now],
        )
        connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])

    changed = _result().copy()
    changed.loc[changed["strategy_id"] == 1, ["result_id", "auto_status", "finalist", "final_rank"]] = [201, "FILTERED", False, None]
    changed.loc[changed["strategy_id"] == 2, ["result_id", "auto_status", "finalist", "final_rank"]] = [202, "FILTERED", False, None]
    changed = pd.concat([changed, changed.iloc[[1]].assign(
        strategy_id=3, strategy_name="strategy-3", result_id=103, auto_status="FINALIST", finalist=True,
        final_rank=1, final_score=70.0, auto_analog_of_strategy_id=None,
    )], ignore_index=True)
    metadata = new_run_metadata(connection)
    path = write_selection_workbook(
        changed, tmp_path / "replacement.xlsx", _request(), metadata,
        latest_user_reviews_by_strategy(connection, [1, 2, 3]),
    )
    exported = load_workbook(path)["All candidates"]
    headers = {cell.value: cell.column for cell in exported[1]}
    rows = {exported.cell(row, headers["ID"]).value: row for row in range(2, exported.max_row + 1)}
    assert exported.cell(rows[1], headers["Auto Status"]).value == "FILTERED"
    assert exported.cell(rows[1], headers["User Status"]).value == "FINALIST"
    assert exported.cell(rows[1], headers["User Rank"]).value == 1
    assert exported.cell(rows[1], headers["Comment"]).value == "reviewed finalist"
    assert exported.cell(rows[2], headers["Auto Status"]).value == "FILTERED"
    assert exported.cell(rows[2], headers["User Status"]).value == "RESERVE"
    assert exported.cell(rows[2], headers["User Rank"]).value is None
    assert exported.cell(rows[2], headers["Comment"]).value == "reviewed reserve"
    assert all(exported.cell(rows[3], headers[name]).value is None for name in ("User Status", "User Rank", "Analog Of ID", "Comment"))

    persist_selection_snapshot(connection, _request(), SelectionConfig(), changed, metadata, path.read_bytes())
    decisions = effective_selection_decisions(connection)
    assert decisions[1][:2] == ("FINALIST", 1)
    assert decisions[2][:2] == ("RESERVE", None)
    assert 3 not in decisions


def test_review_accepts_blank_trailing_headers(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    first_blank = sheet.max_column + 1
    sheet.cell(1, first_blank, "")
    sheet.cell(1, first_blank + 1, "")
    padded = tmp_path / "padded.xlsx"
    workbook.save(padded)

    assert import_selection_review(connection, padded.read_bytes())["row_count"] == 2


def test_review_import_ignores_informational_start_and_end_columns(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["Start"]).value = "11.02"
    sheet.cell(2, headers["End"]).value = "03.09"
    edited = tmp_path / "dated-review.xlsx"
    workbook.save(edited)

    assert import_selection_review(connection, edited.read_bytes())["row_count"] == 2


def test_retest_tag_import_uses_only_marked_rows_from_an_old_review_workbook(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    facts_before = {
        table: connection.execute(f"select * from {table} order by 1").fetchall()
        for table in ("strategies", "strategy_results", "strategy_actions", "strategy_equity")
    }
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "not a status")
    sheet.cell(2, headers["User Rank"], 999)
    sheet.cell(2, headers["Start"], "99.99")
    sheet.cell(2, headers["End"], "")
    sheet.cell(2, headers["RETEST"], "RETEST")
    edited = BytesIO()
    workbook.save(edited)
    _export(connection, tmp_path)  # Make the edited export stale for full review import.

    response = import_retest_tags(connection, edited.getvalue())

    assert response == {"row_count": 1, "retest_count": 1}
    assert connection.execute(
        "select strategy_id, tag, source from strategy_tags order by strategy_id, tag"
    ).fetchall() == [(1, "RETEST", "RETEST_WORKFLOW")]
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)
    assert {
        table: connection.execute(f"select * from {table} order by 1").fetchall()
        for table in facts_before
    } == facts_before


def test_retest_tag_import_rejects_an_unknown_marked_strategy_without_writes(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["ID"], 999)
    sheet.cell(2, headers["RETEST"], "RETEST")
    edited = BytesIO()
    workbook.save(edited)

    with pytest.raises(SelectionReviewError, match="RETEST_TAG_IMPORT_STRATEGY_MISMATCH"):
        import_retest_tags(connection, edited.getvalue())

    assert connection.execute("select count(*) from strategy_tags").fetchone() == (0,)


def test_retest_tag_import_rejects_an_invalid_retest_value_before_writes(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["RETEST"], "RETEST")
    sheet.cell(3, headers["RETEST"], "YES")
    edited = BytesIO()
    workbook.save(edited)

    with pytest.raises(SelectionReviewError, match="RETEST_TAG_IMPORT_INVALID_RETEST"):
        import_retest_tags(connection, edited.getvalue())

    assert connection.execute("select count(*) from strategy_tags").fetchone() == (0,)


@pytest.mark.parametrize("database_id", [None, "another-performance-db"])
def test_retest_tag_import_rejects_missing_or_foreign_database_id(tmp_path: Path, database_id: str | None) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    for row in workbook[META_SHEET].iter_rows(min_row=1, max_col=2):
        if row[0].value == "database_instance_id":
            row[1].value = database_id
    edited = BytesIO()
    workbook.save(edited)

    with pytest.raises(SelectionReviewError, match="RETEST_TAG_IMPORT_DATABASE_MISMATCH"):
        import_retest_tags(connection, edited.getvalue())

    assert connection.execute("select count(*) from strategy_tags").fetchone() == (0,)


def test_retest_tag_import_keeps_blank_tags_and_is_idempotent(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    connection.execute(
        "insert into strategy_tags values (1, 'RETEST', 'PERIOD_INTEGRITY_AUDIT', 'audit.xlsx', now())"
    )
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["RETEST"], None)
    sheet.cell(3, headers["RETEST"], "RETEST")
    edited = BytesIO()
    workbook.save(edited)

    assert import_retest_tags(connection, edited.getvalue()) == {"row_count": 1, "retest_count": 1}
    assert import_retest_tags(connection, edited.getvalue()) == {"row_count": 1, "retest_count": 1}
    assert connection.execute(
        "select strategy_id, source, source_ref from strategy_tags where tag = 'RETEST' order by strategy_id"
    ).fetchall() == [
        (1, "PERIOD_INTEGRITY_AUDIT", "audit.xlsx"),
        (2, "RETEST_WORKFLOW", sha256(edited.getvalue()).hexdigest()),
    ]


def test_review_import_is_atomic_and_syncs_rejected_and_retest_tags(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    old_retest_timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    connection.execute(
        "insert into strategy_tags (strategy_id, tag, source, source_ref, updated_at_utc) values (2, 'RETEST', 'PERIOD_INTEGRITY_AUDIT', 'audit-old.xlsx', ?)",
        [old_retest_timestamp],
    )
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "RESERVE")
    sheet.cell(2, headers["User Rank"], 2)
    sheet.cell(3, headers["User Status"], "REJECTED")
    sheet.cell(3, headers["Analog Of ID"]).value = None
    edited = tmp_path / "edited.xlsx"
    workbook.save(edited)

    response = import_selection_review(connection, edited.read_bytes())

    assert response["finalist_count"] == 0
    assert connection.execute("select strategy_id, tag from strategy_tags order by tag").fetchall() == [(2, "REJECTED"), (2, "RETEST")]
    rejected = connection.execute(
        "select source, source_ref from strategy_tags where strategy_id = 2 and tag = 'REJECTED'"
    ).fetchone()
    assert rejected == ("SELECTION_REVIEW", response["review_import_id"])
    assert latest_effective_finalists(connection, "BTCUSDT") == (True, set())
    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_ALREADY_IMPORTED"):
        import_selection_review(connection, edited.read_bytes())
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (1,)


def test_retest_sync_overwrites_asserted_and_leaves_absent_ids_untouched(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    connection.execute(
        "insert into strategies values (3, 'strategy-3', 'BTCUSDT', 'LONG', '1h', 5, 1, 'run', 'candidate-3', 'ACTIVE', null, ?, ?)",
        [now, now],
    )
    connection.execute(
        "insert into strategy_tags values (1, 'RETEST', 'PERIOD_INTEGRITY_AUDIT', 'audit-1', ?), (2, 'RETEST', 'PERIOD_INTEGRITY_AUDIT', 'audit-2', ?), (3, 'RETEST', 'RETEST_WORKFLOW', 'job-3', ?)",
        [now, now, now],
    )
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["RETEST"], " RETEST ")
    sheet.cell(3, headers["RETEST"], None)
    edited = tmp_path / "sync.xlsx"
    workbook.save(edited)

    response = import_selection_review(connection, edited.read_bytes())

    assert response["row_count"] == 2
    assert connection.execute(
        "select strategy_id, source, source_ref from strategy_tags where tag = 'RETEST' order by strategy_id"
    ).fetchall() == [
        (1, "SELECTION_REVIEW", response["review_import_id"]),
        (2, "PERIOD_INTEGRITY_AUDIT", "audit-2"),
        (3, "RETEST_WORKFLOW", "job-3"),
    ]


@pytest.mark.parametrize("value", ["retest", 1, 1.5, True, datetime(2026, 1, 1)])
def test_invalid_retest_values_are_rejected_before_writes(tmp_path: Path, value: object) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["RETEST"], value)
    invalid = tmp_path / "invalid-retest.xlsx"
    workbook.save(invalid)
    before = connection.execute("select count(*) from selection_review_imports").fetchone()

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_INVALID_RETEST"):
        import_selection_review(connection, invalid.read_bytes())

    assert connection.execute("select count(*) from selection_review_imports").fetchone() == before
    assert connection.execute("select count(*) from selection_review_rows").fetchone() == (0,)
    connection.execute("select 1").fetchone()


def test_old_or_manually_built_workbook_is_not_accepted(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path = write_selection_workbook(_result(), tmp_path / "old.xlsx", _request())

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_SCHEMA_MISMATCH"):
        import_selection_review(connection, path.read_bytes())


@pytest.mark.parametrize("layout", ["non_adjacent", "blank_between", "missing_header"])
def test_retest_header_layout_is_strict_without_writes(tmp_path: Path, layout: str) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    retest_column = headers["RETEST"]
    if layout == "non_adjacent":
        auto_rank_column = headers["Auto Rank"]
        sheet.cell(1, retest_column).value = "Auto Rank"
        sheet.cell(1, auto_rank_column).value = "RETEST"
    elif layout == "blank_between":
        sheet.insert_cols(retest_column)
        sheet.cell(1, retest_column).value = None
    else:
        for row in range(2, sheet.max_row + 1):
            sheet.cell(row, retest_column).value = "RETEST"
        sheet.cell(1, retest_column).value = None
    invalid = tmp_path / f"invalid-header-{layout}.xlsx"
    workbook.save(invalid)

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_SCHEMA_MISMATCH"):
        import_selection_review(connection, invalid.read_bytes())
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)
    assert connection.execute("select count(*) from selection_review_rows").fetchone() == (0,)
    assert connection.execute("select count(*) from strategy_tags").fetchone() == (0,)
    assert connection.execute("select 1").fetchone() == (1,)


def test_changed_automatic_status_is_rejected_without_writes(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["Auto Status"], "FILTERED")
    changed = tmp_path / "changed.xlsx"
    workbook.save(changed)

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_AUTOMATIC_FIELDS_CHANGED"):
        import_selection_review(connection, changed.read_bytes())
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)


def test_changed_automatic_rank_is_rejected_without_writes(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    assert sheet.cell(2, headers["Auto Rank"]).value == 1
    assert sheet.cell(3, headers["Auto Rank"]).value is None
    sheet.cell(2, headers["Auto Rank"], 2)
    changed = tmp_path / "changed-auto-rank.xlsx"
    workbook.save(changed)

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_AUTOMATIC_FIELDS_CHANGED"):
        import_selection_review(connection, changed.read_bytes())
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)


def test_formula_anywhere_in_review_workbook_is_rejected(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    workbook["Finalists"]["A2"] = "=1"
    changed = tmp_path / "formula.xlsx"
    workbook.save(changed)

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_INVALID_FILE"):
        import_selection_review(connection, changed.read_bytes())


def test_formula_in_retest_is_rejected_without_writes(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["RETEST"], "=\"RETEST\"")
    changed = tmp_path / "formula-retest.xlsx"
    workbook.save(changed)

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_INVALID_FILE"):
        import_selection_review(connection, changed.read_bytes())
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)
    assert connection.execute("select count(*) from selection_review_rows").fetchone() == (0,)
    assert connection.execute("select count(*) from strategy_tags").fetchone() == (0,)
    assert connection.execute("select 1").fetchone() == (1,)


def test_first_asserted_retest_has_full_provenance_and_coexists_with_rejected(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "REJECTED")
    sheet.cell(2, headers["User Rank"]).value = None
    sheet.cell(2, headers["Analog Of ID"]).value = None
    sheet.cell(2, headers["RETEST"], "RETEST")
    sheet.cell(3, headers["User Status"], "RESERVE")
    sheet.cell(3, headers["User Rank"], 2)
    sheet.cell(3, headers["Analog Of ID"]).value = None
    edited = tmp_path / "asserted-retest.xlsx"
    workbook.save(edited)

    response = import_selection_review(connection, edited.read_bytes())

    assert connection.execute(
        "select tag, source, source_ref from strategy_tags where strategy_id = 1 order by tag"
    ).fetchall() == [
        ("REJECTED", "SELECTION_REVIEW", response["review_import_id"]),
        ("RETEST", "SELECTION_REVIEW", response["review_import_id"]),
    ]


def test_equivalent_newer_export_keeps_older_workbook_importable(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    old_path, _ = _export(connection, tmp_path)
    old_bytes = old_path.read_bytes()
    _export(connection, tmp_path)

    imported = import_selection_review(connection, old_bytes)
    assert imported["row_count"] == 2
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (1,)


def test_non_equivalent_newer_export_keeps_older_workbook_rejected(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    old_path, _ = _export(connection, tmp_path)
    old_bytes = old_path.read_bytes()
    _export(connection, tmp_path)
    latest_id = connection.execute(
        "select selection_run_id from selection_runs order by created_at_utc desc, selection_run_id desc limit 1"
    ).fetchone()[0]
    connection.execute("update selection_results set auto_score = auto_score + 1 where selection_run_id = ? and strategy_id = 1", [latest_id])

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_NOT_LATEST_RUN"):
        import_selection_review(connection, old_bytes)
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)


def test_stale_result_ids_reject_the_complete_review(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    connection.execute("update strategies set current_result_id = 999 where strategy_id = 2")

    with pytest.raises(SelectionReviewError) as raised:
        import_selection_review(connection, path.read_bytes())
    assert raised.value.code == "SELECTION_REVIEW_STALE_RESULTS"
    assert raised.value.details == [2]
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)


def test_review_can_exceed_auto_top_n_and_later_remove_rejected_tag(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(3, headers["User Status"], "REJECTED")
    sheet.cell(3, headers["Analog Of ID"]).value = None
    first = tmp_path / "first-review.xlsx"
    workbook.save(first)
    import_selection_review(connection, first.read_bytes())
    assert connection.execute("select strategy_id from strategy_tags").fetchall() == [(2,)]

    workbook = load_workbook(first)
    sheet = workbook["All candidates"]
    sheet.cell(3, headers["User Status"], "FINALIST")
    sheet.cell(3, headers["User Rank"], 2)
    second = tmp_path / "second-review.xlsx"
    workbook.save(second)
    imported = import_selection_review(connection, second.read_bytes())

    assert imported["finalist_count"] == 2
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (2,)
    assert connection.execute("select count(*) from strategy_tags").fetchone() == (0,)
    assert latest_effective_finalists(connection, "BTCUSDT") == (True, {1, 2})


def test_analog_must_target_a_finalist_or_reserve_in_same_run(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "REJECTED")
    sheet.cell(2, headers["User Rank"]).value = None
    sheet.cell(3, headers["Analog Of ID"], 999)
    invalid = tmp_path / "invalid-analog.xlsx"
    workbook.save(invalid)

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_INVALID_ANALOG"):
        import_selection_review(connection, invalid.read_bytes())


def test_analog_target_that_is_not_selectable_is_normalized_to_filtered(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "FILTERED")
    sheet.cell(2, headers["User Rank"]).value = None
    sheet.cell(3, headers["User Status"], "ANALOG")
    sheet.cell(3, headers["User Rank"]).value = 2
    sheet.cell(3, headers["Analog Of ID"], 1)
    edited = tmp_path / "filtered-analog.xlsx"
    workbook.save(edited)

    imported = import_selection_review(connection, edited.read_bytes())

    assert imported["finalist_count"] == 0
    assert connection.execute(
        "select strategy_id, user_status, user_rank, user_analog_of_strategy_id from selection_review_rows where review_import_id = ? order by strategy_id",
        [imported["review_import_id"]],
    ).fetchall() == [(1, "FILTERED", None, None), (2, "FILTERED", None, None)]


def test_rank_on_non_selectable_status_is_normalized_to_blank(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, _ = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "FILTERED")
    sheet.cell(2, headers["User Rank"], 1)
    edited = tmp_path / "filtered-rank.xlsx"
    workbook.save(edited)

    imported = import_selection_review(connection, edited.read_bytes())

    assert connection.execute(
        "select user_status, user_rank from selection_review_rows where review_import_id = ? and strategy_id = 1",
        [imported["review_import_id"]],
    ).fetchone() == ("FILTERED", None)
