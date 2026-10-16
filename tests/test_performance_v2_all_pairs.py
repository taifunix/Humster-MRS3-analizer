from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import duckdb
import pandas as pd

from mrs3.performance_v2_selection import (
    PerformanceV2SelectionError,
    SelectionRequest,
    coordinate_all_pairs,
    effective_selection_stages,
    parse_selection_request,
)
from mrs3.performance_v2_store import initialize_performance_v2


def _db(tmp_path: Path) -> tuple[duckdb.DuckDBPyConnection, Path]:
    root = tmp_path / "performance"
    root.mkdir(parents=True)
    connection = duckdb.connect(str(root / "strategy_performance.duckdb"))
    initialize_performance_v2(connection)
    now = datetime(2026, 10, 10, tzinfo=UTC)
    db_id = connection.execute(
        "select value from schema_info where key = 'database_instance_id'"
    ).fetchone()[0]
    for strategy_id, symbol, side, run_id in (
        (1, "BTCUSDT", "LONG", "source-btc"),
        (2, "ETHUSDT", "SHORT", "source-eth"),
    ):
        connection.execute(
            """insert into strategies (
                strategy_id, strategy_name, symbol, side, timeframe, close_ma_len,
                order_count, analysis_run_id, candidate_identity, lifecycle_status,
                current_result_id, created_at_utc, updated_at_utc
            ) values (?, ?, ?, ?, '1h', 5, 1, 'analysis', ?, 'ACTIVE', ?, ?, ?)""",
            [strategy_id, f"s-{strategy_id}", symbol, side, f"candidate-{strategy_id}", 100 + strategy_id, now, now],
        )
        connection.execute(
            """insert into strategy_results (
                result_id, strategy_id, report_start_utc, report_end_utc, exchange,
                initial_balance, final_balance, imported_at_utc
            ) values (?, ?, ?, ?, 'BYBIT', 100, 101, ?)""",
            [100 + strategy_id, strategy_id, now, now, now],
        )
        connection.execute(
            """insert into selection_runs (
                selection_run_id, database_instance_id, symbol, side,
                selection_contract_version, request_json, request_sha256,
                config_json, config_sha256, candidate_count, representative_count,
                auto_finalist_count, top_n, workbook_sha256, created_at_utc
            ) values (?, ?, ?, ?, 'selection-v1', '{}', 'request', '{}', 'config', 1, 1, 1, 1, ?, ?)""",
            [run_id, db_id, symbol, side, f"ordinary-{strategy_id}", now],
        )
        connection.execute(
            """insert into selection_results (
                selection_run_id, strategy_id, result_id_at_selection, auto_status,
                prior_rejected, stage_trace_json
            ) values (?, ?, ?, 'FINALIST', false, '{}')""",
            [run_id, strategy_id, 100 + strategy_id],
        )
    connection.commit()
    return connection, root


def _request(*, finalists_only: bool = False, symbol: str = "BTCUSDT", side: str = "LONG") -> SelectionRequest:
    return parse_selection_request({
        "symbol": symbol,
        "side": side,
        "stages": [],
        **({"finalists_only": True} if finalists_only else {}),
    })


def _candidate(symbol: str, side: str, strategy_id: int, result_id: int) -> pd.DataFrame:
    return pd.DataFrame([{
        "strategy_id": strategy_id,
        "strategy_name": f"s-{strategy_id}",
        "symbol": symbol,
        "side": side,
        "result_id": result_id,
    }])


def _runner(candidates: pd.DataFrame, request: SelectionRequest, config) -> pd.DataFrame:
    result = candidates.copy()
    result["auto_status"] = "FINALIST"
    result["final_score"] = 1.0
    result["final_rank"] = 1
    result["elimination_reason"] = None
    result["prior_rejected"] = False
    for stage in effective_selection_stages(request, config):
        result[f"eliminated_by_{stage.id}"] = False
    for stage_id in ("filter_lot_variant_redundancy", "filter_hard_cutoffs", "ab_deterioration"):
        result[f"eliminated_by_{stage_id}"] = False
    return result


def _insert_manual_review(
    connection: duckdb.DuckDBPyConnection, *, review_id: str, strategy_id: int,
    run_id: str, status: str | None, comment: str | None = None,
    imported_at: datetime | None = None,
) -> None:
    now = imported_at or datetime(2026, 10, 10, 1, 0, tzinfo=UTC)
    connection.execute(
        """insert into selection_review_imports (
            review_import_id, selection_run_id, workbook_sha256,
            imported_at_utc, row_count
        ) values (?, ?, ?, ?, 1)""",
        [review_id, run_id, f"workbook-{review_id}", now],
    )
    connection.execute(
        """insert into selection_review_rows (
            review_import_id, strategy_id, user_status, user_rank,
            user_analog_of_strategy_id, comment
        ) values (?, ?, ?, ?, NULL, ?)""",
        [review_id, strategy_id, status, 1 if status in {"FINALIST", "RESERVE"} else None, comment],
    )


def _all_pairs_loader(_connection, request):
    return _candidate(
        request.symbol, request.side,
        1 if request.symbol == "BTCUSDT" else 2,
        101 if request.symbol == "BTCUSDT" else 102,
    )


def _hard_filter_request() -> SelectionRequest:
    return parse_selection_request({
        "symbol": "BTCUSDT",
        "side": "LONG",
        "stages": [{"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"}],
    })


def _hard_filter_runner(candidates: pd.DataFrame, request: SelectionRequest, config) -> pd.DataFrame:
    result = _runner(candidates, request, config)
    result["eliminated_by_filter_hard_cutoffs"] = result["strategy_id"] == 1
    result.loc[result["strategy_id"] == 1, "auto_status"] = "FILTERED"
    result.loc[result["strategy_id"] == 1, "elimination_reason"] = "HARD_CUTOFF"
    result.loc[result["strategy_id"] == 1, "prior_rejected"] = True
    return result


def test_coordinate_all_pairs_keeps_partition_isolation_and_persists_source_overlay_roles(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    loaded: list[tuple[str, str]] = []

    def loader(_connection, request):
        loaded.append((request.symbol, request.side))
        if request.symbol == "BTCUSDT":
            return _candidate("BTCUSDT", "LONG", 1, 101)
        return _candidate("ETHUSDT", "SHORT", 2, 102)

    result = coordinate_all_pairs(
        connection, root, _request(), operation_key="operation-all-pairs-1",
        candidate_loader=loader, selection_runner=_runner,
    )

    assert loaded == [("BTCUSDT", "LONG"), ("ETHUSDT", "SHORT")]
    assert [(item.pair, item.side, len(item.result)) for item in result.partitions] == [
        ("BTCUSDT", "LONG", 1), ("ETHUSDT", "SHORT", 1),
    ]
    assert connection.execute(
        "select pair, side, role from selection_publication_runs order by pair, role"
    ).fetchall() == [
        ("BTCUSDT", "LONG", "OVERLAY"), ("BTCUSDT", "LONG", "SOURCE"),
        ("ETHUSDT", "SHORT", "OVERLAY"), ("ETHUSDT", "SHORT", "SOURCE"),
    ]
    requests = connection.execute(
        "select request_json from selection_runs where selection_run_id like 'overlay-%' order by symbol"
    ).fetchall()
    assert all('"ranking_scope":"AUTOMATIC_REJECTION_OVERLAY"' in row[0] for row in requests)
    stored = connection.execute(
        "select request_json, request_sha256 from selection_runs where selection_run_id like 'overlay-%' order by symbol"
    ).fetchall()
    import hashlib
    assert all(hashlib.sha256(request.encode()).hexdigest() == digest for request, digest in stored)


def test_coordinate_all_pairs_can_freeze_package_before_publication(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)

    result = coordinate_all_pairs(
        connection, root, _request(), operation_key="operation-prebuild",
        candidate_loader=_all_pairs_loader, selection_runner=_runner, publish=False,
    )

    assert result.publication is None
    assert result.package is not None
    assert result.package.decision_group_id
    assert result.package.database_instance_id == connection.execute(
        "select value from schema_info where key = 'database_instance_id'"
    ).fetchone()[0]
    assert connection.execute("select count(*) from selection_publications").fetchone() == (0,)


def test_coordinate_all_pairs_keeps_empty_partition_in_frozen_package(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)

    def loader(_connection, request):
        if request.symbol == "BTCUSDT":
            return _candidate("BTCUSDT", "LONG", 1, 101)
        return pd.DataFrame(columns=["strategy_id", "strategy_name", "symbol", "side", "result_id"])

    result = coordinate_all_pairs(
        connection, root, _request(), operation_key="operation-all-pairs-empty",
        candidate_loader=loader, selection_runner=_runner,
    )

    assert [len(item.result) for item in result.partitions] == [1, 0]
    assert connection.execute(
        "select count(*) from selection_results where selection_run_id like 'overlay-%'"
    ).fetchone() == (1,)


def test_coordinate_all_pairs_rejects_source_replacement_before_publication(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    inserted = False

    def loader(_connection, request):
        nonlocal inserted
        if request.symbol == "BTCUSDT" and not inserted:
            inserted = True
            db_id = connection.execute(
                "select value from schema_info where key = 'database_instance_id'"
            ).fetchone()[0]
            connection.execute(
                """insert into selection_runs (
                    selection_run_id, database_instance_id, symbol, side,
                    selection_contract_version, request_json, request_sha256,
                    config_json, config_sha256, candidate_count,
                    representative_count, auto_finalist_count, top_n,
                    workbook_sha256, created_at_utc
                ) values ('source-btc-replaced', ?, 'BTCUSDT', 'LONG',
                    'selection-v1', '{}', 'request-2', '{}', 'config-2',
                    1, 1, 1, 1, 'ordinary-2', ?)""",
                [db_id, datetime(2026, 10, 10, 2, tzinfo=UTC)],
            )
            connection.execute(
                """insert into selection_results (
                    selection_run_id, strategy_id, result_id_at_selection,
                    auto_status, prior_rejected, stage_trace_json
                ) values ('source-btc-replaced', 1, 101, 'FINALIST', false, '{}')"""
            )
        return _all_pairs_loader(_connection, request)

    try:
        coordinate_all_pairs(
            connection, root, _request(), operation_key="operation-stale-source",
            candidate_loader=loader, selection_runner=_runner,
        )
    except PerformanceV2SelectionError as error:
        assert str(error) == "SOURCE_RUN_STALE"
    else:
        raise AssertionError("stale ordinary source was accepted")
    assert connection.execute("select count(*) from selection_publications").fetchone() == (0,)


def test_coordinate_all_pairs_cohort_on_only_keeps_manual_finalist_or_reserve(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    _insert_manual_review(
        connection, review_id="manual-btc", strategy_id=1, run_id="source-btc",
        status="FINALIST", comment="btc",
    )
    _insert_manual_review(
        connection, review_id="manual-eth", strategy_id=2, run_id="source-eth",
        status="RESERVE", comment="eth",
    )

    result = coordinate_all_pairs(
        connection, root, _request(finalists_only=True),
        operation_key="operation-cohort-on", candidate_loader=_all_pairs_loader,
        selection_runner=_runner,
    )

    assert [len(item.result) for item in result.partitions] == [1, 1]
    assert connection.execute(
        "select user_status from selection_review_rows where review_import_id = 'manual-eth'"
    ).fetchone() == ("RESERVE",)


def test_coordinate_all_pairs_cohort_off_evaluates_manual_rejected(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    _insert_manual_review(
        connection, review_id="manual-eth", strategy_id=2, run_id="source-eth",
        status="REJECTED", comment="eth",
    )

    result = coordinate_all_pairs(
        connection, root, _request(), operation_key="operation-cohort-off",
        candidate_loader=_all_pairs_loader, selection_runner=_runner,
    )

    assert [len(item.result) for item in result.partitions] == [1, 1]


def test_coordinate_all_pairs_cohort_on_excludes_unreviewed_and_null_manual_rows(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    _insert_manual_review(
        connection, review_id="manual-null", strategy_id=1, run_id="source-btc",
        status=None,
    )

    result = coordinate_all_pairs(
        connection, root, _request(finalists_only=True), operation_key="operation-cohort-null",
        candidate_loader=_all_pairs_loader, selection_runner=_runner,
    )

    assert [len(item.result) for item in result.partitions] == [0, 0]


def test_coordinate_all_pairs_overlay_review_does_not_widen_manual_cohort(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    _insert_manual_review(
        connection, review_id="manual-btc", strategy_id=1, run_id="source-btc",
        status="FINALIST", comment="btc",
    )
    coordinate_all_pairs(
        connection, root, _hard_filter_request(), operation_key="operation-overlay-cohort",
        candidate_loader=_all_pairs_loader, selection_runner=_hard_filter_runner,
    )

    result = coordinate_all_pairs(
        connection, root, _request(finalists_only=True), operation_key="operation-overlay-cohort-2",
        candidate_loader=_all_pairs_loader, selection_runner=_runner,
    )

    assert len(result.partitions[0].result) == 1


def test_coordinate_all_pairs_empty_partition_without_source_is_safe(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    connection.execute(
        "update selection_runs set request_json = ? where selection_run_id = 'source-eth'",
        ['{"ranking_scope":"CURRENT_EFFECTIVE"}'],
    )

    def loader(_connection, request):
        if request.symbol == "BTCUSDT":
            return _candidate("BTCUSDT", "LONG", 1, 101)
        return pd.DataFrame(columns=["strategy_id", "result_id"])

    result = coordinate_all_pairs(
        connection, root, _request(), operation_key="operation-empty-no-source",
        candidate_loader=loader, selection_runner=_runner,
    )

    assert [len(item.result) for item in result.partitions] == [1, 0]
    assert connection.execute(
        "select role, selection_run_id from selection_publication_runs where pair = 'ETHUSDT'"
    ).fetchall() == [("OVERLAY", "overlay-operation-empty-no-source-ethusdt-short")]


def test_coordinate_all_pairs_uses_allowlisted_direct_rejection_transition(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    _insert_manual_review(
        connection, review_id="manual-btc", strategy_id=1, run_id="source-btc",
        status="FINALIST", comment="btc",
    )

    coordinate_all_pairs(
        connection, root, _hard_filter_request(), operation_key="operation-reject",
        candidate_loader=_all_pairs_loader, selection_runner=_hard_filter_runner,
    )

    review = connection.execute(
        """select rows.user_status, rows.user_rank, rows.user_analog_of_strategy_id,
                  rows.comment
             from selection_review_rows rows
             join selection_review_imports imports using (review_import_id)
            where imports.selection_run_id like 'overlay-%'
              and rows.strategy_id = 1"""
    ).fetchone()
    assert review == ("REJECTED", None, None, "btc\nDegraded Finalist")
    assert connection.execute(
        "select tag, source from strategy_tags where strategy_id = 1 and tag = 'REJECTED'"
    ).fetchone() == ("REJECTED", "SELECTION_AUTOMATIC_REJECTION")
    eliminated = connection.execute(
        """select auto_status, auto_reason, stage_trace_json, prior_rejected
             from selection_results
            where selection_run_id like 'overlay-%' and strategy_id = 1"""
    ).fetchone()
    assert eliminated[:2] == ("FILTERED", "HARD_CUTOFF")
    assert eliminated[3] is True
    eliminated_trace = json.loads(eliminated[2])
    assert eliminated_trace["filter_hard_cutoffs"] is True
    assert all(value is False for key, value in eliminated_trace.items() if key != "filter_hard_cutoffs")
    survivor = connection.execute(
        """select auto_status, auto_reason, stage_trace_json, prior_rejected
             from selection_results
            where selection_run_id like 'overlay-%' and strategy_id = 2"""
    ).fetchone()
    assert survivor[:2] == ("FINALIST", None)
    assert survivor[3] is False
    assert all(value is False for value in json.loads(survivor[2]).values())


def test_coordinate_all_pairs_uses_newest_raw_review_for_rejection_transition(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    _insert_manual_review(
        connection, review_id="manual-old-rejected", strategy_id=1, run_id="source-btc",
        status="REJECTED", imported_at=datetime(2026, 10, 10, 1, tzinfo=UTC),
    )
    _insert_manual_review(
        connection, review_id="manual-new-finalist", strategy_id=1, run_id="source-btc",
        status="FINALIST", comment="new", imported_at=datetime(2026, 10, 10, 2, tzinfo=UTC),
    )

    coordinate_all_pairs(
        connection, root, _hard_filter_request(), operation_key="operation-newest-review",
        candidate_loader=_all_pairs_loader, selection_runner=_hard_filter_runner,
    )

    assert connection.execute(
        """select rows.user_status, rows.comment
             from selection_review_rows rows
             join selection_review_imports imports using (review_import_id)
            where imports.selection_run_id like 'overlay-%' and rows.strategy_id = 1"""
    ).fetchone() == ("REJECTED", "new\nDegraded Finalist")


def test_coordinate_all_pairs_effective_rejected_is_a_noop(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    connection.execute(
        """insert into strategy_tags (
            strategy_id, tag, source, source_ref, updated_at_utc
        ) values (1, 'REJECTED', 'PRIOR', 'prior-ref', ?)""",
        [datetime(2026, 10, 10, tzinfo=UTC)],
    )

    coordinate_all_pairs(
        connection, root, _hard_filter_request(), operation_key="operation-reject-noop",
        candidate_loader=_all_pairs_loader, selection_runner=_hard_filter_runner,
    )

    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)
    assert connection.execute(
        "select source, source_ref from strategy_tags where strategy_id = 1 and tag = 'REJECTED'"
    ).fetchone() == ("PRIOR", "prior-ref")


def test_coordinate_all_pairs_rejects_missing_required_candidate_columns(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)

    try:
        coordinate_all_pairs(
            connection, root, _request(), operation_key="operation-invalid-candidates",
            candidate_loader=lambda _connection, _request: pd.DataFrame({"symbol": ["BTCUSDT"]}),
            selection_runner=_runner,
        )
    except PerformanceV2SelectionError as error:
        assert str(error) == "INVALID_SELECTION_CANDIDATES"
    else:
        raise AssertionError("missing required candidate columns were accepted")


def test_coordinate_all_pairs_normalizes_launch_pair_from_controls(tmp_path: Path) -> None:
    first_connection, first_root = _db(tmp_path / "first")
    first = coordinate_all_pairs(
        first_connection, first_root, _request(symbol="BTCUSDT", side="LONG"),
        operation_key="operation-controls-1", candidate_loader=_all_pairs_loader,
        selection_runner=_runner,
    )
    second_connection, second_root = _db(tmp_path / "second")
    second = coordinate_all_pairs(
        second_connection, second_root, _request(symbol="ETHUSDT", side="SHORT"),
        operation_key="operation-controls-2", candidate_loader=_all_pairs_loader,
        selection_runner=_runner,
    )
    first_controls = first_connection.execute(
        "select controls_json, controls_sha256 from selection_publications where publication_id = ?",
        [first.publication.publication_id],
    ).fetchone()
    second_controls = second_connection.execute(
        "select controls_json, controls_sha256 from selection_publications where publication_id = ?",
        [second.publication.publication_id],
    ).fetchone()
    assert first_controls == second_controls


def test_coordinate_all_pairs_corrupt_or_empty_source_is_not_eligible(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)
    connection.execute(
        "update selection_runs set request_json = ? where selection_run_id = 'source-btc'",
        ["not-json"],
    )
    try:
        coordinate_all_pairs(
            connection, root, _request(), operation_key="operation-corrupt-source",
            candidate_loader=_all_pairs_loader, selection_runner=_runner,
        )
    except PerformanceV2SelectionError as error:
        assert str(error) == "SOURCE_RUN_REQUIRED"
    else:
        raise AssertionError("corrupt source run was accepted")

    connection.execute(
        "update selection_runs set request_json = '{}' where selection_run_id = 'source-btc'"
    )
    connection.execute("delete from selection_results where selection_run_id = 'source-btc'")
    try:
        coordinate_all_pairs(
            connection, root, _request(), operation_key="operation-empty-source",
            candidate_loader=_all_pairs_loader, selection_runner=_runner,
        )
    except PerformanceV2SelectionError as error:
        assert str(error) == "SOURCE_RUN_REQUIRED"
    else:
        raise AssertionError("empty source run was accepted")


def test_coordinate_all_pairs_top_n_uses_effective_enabled_rank_stage(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)

    coordinate_all_pairs(
        connection, root, _request(), operation_key="operation-top-default",
        candidate_loader=_all_pairs_loader, selection_runner=_runner,
    )
    enabled = parse_selection_request({
        "symbol": "BTCUSDT", "side": "LONG",
        "stages": [{"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 3}],
    })
    coordinate_all_pairs(
        connection, root, enabled, operation_key="operation-top-enabled",
        candidate_loader=_all_pairs_loader, selection_runner=_runner,
    )
    disabled = parse_selection_request({
        "symbol": "BTCUSDT", "side": "LONG",
        "stages": [{"id": "rank_robust_top_n", "enabled": False, "scope": "pair_side", "top_n": 9}],
    })
    coordinate_all_pairs(
        connection, root, disabled, operation_key="operation-top-disabled",
        candidate_loader=_all_pairs_loader, selection_runner=_runner,
    )

    assert connection.execute(
        """select top_n from selection_runs
            where selection_run_id in (
                'overlay-operation-top-default-btcusdt-long',
                'overlay-operation-top-enabled-btcusdt-long',
                'overlay-operation-top-disabled-btcusdt-long'
            ) order by selection_run_id"""
    ).fetchall() == [(1,), (1,), (3,)]


def test_coordinate_all_pairs_rejects_incomplete_stage_trace_before_publication(tmp_path: Path) -> None:
    connection, root = _db(tmp_path)

    def missing_trace_runner(candidates, request, config):
        result = _runner(candidates, request, config)
        result = result.drop(columns=["eliminated_by_filter_lot_variant_redundancy"])
        return result

    try:
        coordinate_all_pairs(
            connection, root, _request(), operation_key="operation-missing-trace",
            candidate_loader=_all_pairs_loader, selection_runner=missing_trace_runner,
        )
    except PerformanceV2SelectionError as error:
        assert str(error) == "INVALID_SELECTION_RESULT"
    else:
        raise AssertionError("incomplete stage trace was accepted")
    assert connection.execute("select count(*) from selection_publications").fetchone() == (0,)
