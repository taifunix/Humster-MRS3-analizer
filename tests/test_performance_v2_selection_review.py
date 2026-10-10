from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from io import BytesIO
from pathlib import Path

import duckdb
import numpy as np
from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook
import pandas as pd
import pytest
import mrs3.performance_v2_selection_review as selection_review_module

from mrs3.performance_v2_selection import (
    SelectionConfig, load_selection_candidates, parse_selection_request, retest_cohort_request,
    run_selection, write_selection_workbook,
)
from mrs3.performance_v2_equity_cache import (
    current_equity_source_metadata, encode_equity_facts, equity_source_revision,
)
from mrs3.performance_v2_equity_quality import EquitySample, calculate_equity_quality_facts
from mrs3.performance_v2_equity_regime import (
    ALGORITHM_VERSION as EQUITY_REGIME_ALGORITHM_VERSION,
    EquityRegimeSample,
    assess_equity_regime,
    calculate_equity_regime_facts,
)
from mrs3.performance_v2_equity_regime_cache import (
    encode_equity_regime_assessment,
    encode_equity_regime_facts,
    equity_regime_source_revision,
    upsert_equity_regime_facts_checked,
)
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
    _rejected_strategy_ids,
    _selection_rows,
    automatic_filter_rejected_strategy_ids,
)


def test_selection_rows_does_not_copy_equity_evidence_for_each_column(monkeypatch) -> None:
    frame = pd.DataFrame({"strategy_id": range(32), "result_id": range(32), "auto_status": ["FINALIST"] * 32})
    evidence = {str(index): {"facts": "x" * 1000} for index in range(100)}
    frame.attrs["equity_regime_evidence"] = evidence
    original_deepcopy = pd.core.generic.deepcopy
    evidence_copies = 0

    def counted_deepcopy(value, *args, **kwargs):
        nonlocal evidence_copies
        if isinstance(value, dict) and value.get("equity_regime_evidence") is not None:
            evidence_copies += 1
        return original_deepcopy(value, *args, **kwargs)

    monkeypatch.setattr(pd.core.generic, "deepcopy", counted_deepcopy)
    rows = _selection_rows(frame)

    assert len(rows) == 32
    assert rows[0]["strategy_id"] == 0
    assert 1 <= evidence_copies < 10
    assert frame.attrs["equity_regime_evidence"] is evidence
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


def _equity_regime_cache_entry(
    connection: duckdb.DuckDBPyConnection, result_id: int,
) -> dict[str, object]:
    source = current_equity_source_metadata(connection, result_id)
    samples = [
        EquityRegimeSample(int(row[0]), int(row[1]), row[2].astimezone(UTC), row[3])
        for row in connection.execute(
            """select result_id, sample_index, timestamp_utc, equity from strategy_equity
               where result_id = ? order by sample_index, timestamp_utc""",
            [result_id],
        ).fetchall()
    ]
    facts = calculate_equity_regime_facts(
        result_id, source["report_start_utc"], source["report_end_utc"], samples,
    )
    upsert_equity_regime_facts_checked(connection, source, facts, calculated_at_utc=datetime.now(UTC))
    assessment = assess_equity_regime(facts)
    facts_json = encode_equity_regime_facts(facts)
    return {
        "status": "FRESH",
        "assessment": assessment,
        "facts": facts,
        "source_revision": equity_regime_source_revision(source),
        "facts_sha256": sha256(facts_json.encode()).hexdigest(),
        "classifier_algo_version": EQUITY_REGIME_ALGORITHM_VERSION,
        "equity_regime_json": encode_equity_regime_assessment(assessment),
    }


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


@pytest.mark.parametrize(("stage_id", "column", "reason", "tag_source"), [
    ("filter_lot_variant_redundancy", "eliminated_by_filter_lot_variant_redundancy", "LOT_VARIANT_FULL_DD5", "SELECTION_LOT_VARIANT"),
    ("ab_deterioration", "eliminated_by_ab_deterioration", "AB_DETERIORATION;B_PNL30_FLOOR", "SELECTION_AB_DETERIORATION"),
])
def test_additional_filter_publication_tags_actual_exclusion_rejected(
    tmp_path: Path, stage_id: str, column: str, reason: str, tag_source: str,
) -> None:
    connection = _database(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": stage_id, "enabled": True, "scope": "pair_side_timeframe"},
    ]})
    result = _result()
    result[column] = [True, False]
    result.loc[0, "auto_status"] = "FILTERED"
    result.loc[0, "finalist"] = False
    result.loc[1, "auto_status"] = "FINALIST"
    result.loc[1, "finalist"] = True
    result.loc[0, "elimination_reason"] = "producer reason wording can evolve"
    metadata = new_run_metadata(connection, request)

    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, b"workbook")

    assert connection.execute(
        "select strategy_id, source, source_ref from strategy_tags where tag = 'REJECTED'"
    ).fetchall() == [(1, tag_source, metadata["selection_run_id"])]


def test_equity_reserve_is_not_in_automatic_filter_rejection_ids(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_lot_variant_redundancy", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"},
        {"id": "ab_deterioration", "enabled": True, "scope": "pair_side"},
    ]})
    result = _result()
    result["eliminated_by_filter_equity_regime"] = [True, False]
    result.loc[0, "auto_status"] = "RESERVE"
    result.loc[0, "finalist"] = False
    result.loc[0, "elimination_reason"] = "EQUITY_REGIME_STALLED_RESERVE"

    assert automatic_filter_rejected_strategy_ids(result, request, SelectionConfig()) == set()

    metadata = new_run_metadata(connection, request)
    persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, b"reserve")
    assert connection.execute("select count(*) from strategy_tags where tag = 'REJECTED'").fetchone() == (0,)


def test_automatic_rejection_rejects_ambiguous_multiple_stage_flags() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_lot_variant_redundancy", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"},
    ]})
    result = _result()
    result["eliminated_by_filter_lot_variant_redundancy"] = [True, False]
    result["eliminated_by_filter_hard_cutoffs"] = [True, False]

    with pytest.raises(SelectionReviewError, match="SELECTION_REVIEW_INVALID_SELECTION"):
        automatic_filter_rejected_strategy_ids(result, request, SelectionConfig())


def test_automatic_rejection_uses_only_the_three_enabled_exact_trace_flags() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_lot_variant_redundancy", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
        {"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"},
        {"id": "ab_deterioration", "enabled": True, "scope": "pair_side"},
    ]})
    result = pd.DataFrame([
        {"strategy_id": 1, "eliminated_by_filter_lot_variant_redundancy": np.bool_(True),
         "eliminated_by_filter_hard_cutoffs": False, "eliminated_by_ab_deterioration": False,
         "eliminated_by_filter_equity_regime": True, "auto_status": "FINALIST", "elimination_reason": "other"},
        {"strategy_id": 2, "eliminated_by_filter_lot_variant_redundancy": False,
         "eliminated_by_filter_hard_cutoffs": True, "eliminated_by_ab_deterioration": False,
         "eliminated_by_filter_equity_regime": False, "auto_status": "RESERVE", "elimination_reason": "other"},
        {"strategy_id": 3, "eliminated_by_filter_lot_variant_redundancy": False,
         "eliminated_by_filter_hard_cutoffs": False, "eliminated_by_ab_deterioration": True,
         "eliminated_by_filter_equity_regime": False, "auto_status": "ANALOG", "elimination_reason": "other"},
        {"strategy_id": 4, "eliminated_by_filter_lot_variant_redundancy": False,
         "eliminated_by_filter_hard_cutoffs": False, "eliminated_by_ab_deterioration": False,
         "eliminated_by_filter_equity_regime": True, "auto_status": "FILTERED", "elimination_reason": "hard cutoff"},
        {"strategy_id": 5, "eliminated_by_filter_lot_variant_redundancy": False,
         "eliminated_by_filter_hard_cutoffs": False, "eliminated_by_ab_deterioration": False,
         "eliminated_by_filter_equity_regime": True, "auto_status": "RESERVE",
         "elimination_reason": "EQUITY_REGIME_STALLED_RESERVE"},
    ])

    assert selection_review_module.automatic_filter_rejected_ids(result, request, SelectionConfig()) == {
        "SELECTION_LOT_VARIANT": {1},
        "SELECTION_HARD_CUTOFF": {2},
        "SELECTION_AB_DETERIORATION": {3},
    }


def test_automatic_rejection_ignores_non_boolean_trace_values() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_lot_variant_redundancy", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"},
        {"id": "ab_deterioration", "enabled": True, "scope": "pair_side"},
    ]})
    result = pd.DataFrame([{
        "strategy_id": 1,
        "eliminated_by_filter_lot_variant_redundancy": "false",
        "eliminated_by_filter_hard_cutoffs": float("nan"),
        "eliminated_by_ab_deterioration": 1,
        "extra_untrusted_trace": pd.NA,
    }])

    assert selection_review_module.automatic_filter_rejected_ids(result, request, SelectionConfig()) == {
        "SELECTION_LOT_VARIANT": set(),
        "SELECTION_HARD_CUTOFF": set(),
        "SELECTION_AB_DETERIORATION": set(),
    }


def test_direct_rejection_transition_preserves_comment_and_marks_prior_status() -> None:
    transition = getattr(selection_review_module, "direct_rejection_transition", None)
    assert callable(transition), "pure direct-rejection transition is missing"

    result = transition(
        effective_rejected=False,
        prior_user_status="FINALIST",
        prior_user_rank=4,
        prior_analog_of_strategy_id=25,
        prior_comment="keep this note",
    )

    assert (result.user_status, result.user_rank, result.analog_of_strategy_id, result.comment) == (
        "REJECTED", None, None, "keep this note\nDegraded Finalist",
    )
    assert not result.no_op


@pytest.mark.parametrize(
    ("status", "comment", "expected_comment"),
    [
        (None, "keep", "keep"),
        ("", "keep", "keep"),
        ("RESERVE", "keep", "keep\nDegraded Reserved"),
        ("ANALOG", "", 'Degraded "ANALOG"'),
        (" legacy ", None, 'Degraded " legacy "'),
    ],
)
def test_direct_rejection_transition_comment_markers(status, comment, expected_comment) -> None:
    transition = getattr(selection_review_module, "direct_rejection_transition", None)
    assert callable(transition), "pure direct-rejection transition is missing"

    result = transition(
        effective_rejected=False,
        prior_user_status=status,
        prior_user_rank=7,
        prior_analog_of_strategy_id=9,
        prior_comment=comment,
    )

    assert result.user_status == "REJECTED"
    assert result.user_rank is None and result.analog_of_strategy_id is None
    assert result.comment == expected_comment


def test_effectively_rejected_transition_is_a_universal_noop() -> None:
    transition = getattr(selection_review_module, "direct_rejection_transition", None)
    assert callable(transition), "pure direct-rejection transition is missing"
    prior = ("RESERVE", 8, 42, "untouched")

    result = transition(
        effective_rejected=True,
        prior_user_status=prior[0],
        prior_user_rank=prior[1],
        prior_analog_of_strategy_id=prior[2],
        prior_comment=prior[3],
    )

    assert (result.user_status, result.user_rank, result.analog_of_strategy_id, result.comment) == prior
    assert result.no_op


def test_direct_rejection_preserves_and_appends_to_a_long_existing_comment() -> None:
    transition = getattr(selection_review_module, "direct_rejection_transition", None)
    assert callable(transition), "pure direct-rejection transition is missing"
    existing = "x" * 1000

    result = transition(
        effective_rejected=False,
        prior_user_status="FINALIST",
        prior_user_rank=1,
        prior_analog_of_strategy_id=None,
        prior_comment=existing,
    )

    assert result.comment == f"{existing}\nDegraded Finalist"


def test_outside_top_n_requires_same_run_enabled_stage_and_exact_rank_trace() -> None:
    eligible = getattr(selection_review_module, "outside_top_n_eligible", None)
    assert callable(eligible), "pure exact-trace Top N predicate is missing"
    stages = ("filter_hard_cutoffs", "rank_robust_top_n")
    trace = {"filter_hard_cutoffs": False, "rank_robust_top_n": True}
    row = {
        "enabled_stage_ids": stages,
        "stage_trace": trace,
        "top_n": 3,
        "auto_status": "RESERVE",
        "auto_rank": 4,
        "auto_reason": "RANK_ROBUST_TOP_N",
        "auto_analog_of_strategy_id": None,
        "prior_rejected": False,
    }

    assert eligible(**row)
    assert not eligible(**{**row, "enabled_stage_ids": ("filter_hard_cutoffs",)})
    assert not eligible(**{**row, "stage_trace": {"filter_hard_cutoffs": False}})
    assert not eligible(**{**row, "stage_trace": {"filter_hard_cutoffs": True, "rank_robust_top_n": True}})
    assert not eligible(**{**row, "stage_trace": {"filter_hard_cutoffs": False, "rank_robust_top_n": False}})
    assert not eligible(**{**row, "stage_trace": {
        "filter_hard_cutoffs": False, "rank_robust_top_n": True, "disabled_extra": False,
    }})
    assert not eligible(**{**row, "auto_reason": "ANALOG"})
    assert not eligible(**{**row, "auto_analog_of_strategy_id": 1})
    assert not eligible(**{**row, "auto_status": "FILTERED"})
    assert not eligible(**{**row, "prior_rejected": True})
    assert not eligible(**{**row, "auto_rank": 3})
    assert not eligible(**{**row, "top_n": None})
    assert eligible(**{
        **row,
        "stage_trace": {
            "filter_hard_cutoffs": np.bool_(False),
            "rank_robust_top_n": np.bool_(True),
        },
        "prior_rejected": np.bool_(False),
    })


def test_panel_xlsx_does_not_override_equity_reserve_user_status(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(
        "mrs3.panel.latest_user_reviews_by_strategy",
        lambda *_args, **_kwargs: {1: {"user_status": "FINALIST"}},
    )
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_lot_variant_redundancy", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "filter_hard_cutoffs", "enabled": True, "scope": "pair_side"},
        {"id": "ab_deterioration", "enabled": True, "scope": "pair_side"},
    ]})
    result = _result()
    result["eliminated_by_filter_equity_regime"] = [True, False]
    result.loc[0, "auto_status"] = "RESERVE"
    result.loc[0, "finalist"] = False
    result.loc[0, "elimination_reason"] = "EQUITY_REGIME_STALLED_RESERVE"
    monkeypatch.setattr(controller, "_performance_v2_selection_result", lambda _payload: (request, result))

    _, data = controller.strategies_performance_v2_selection({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    sheet = load_workbook(BytesIO(data), data_only=True)["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    exported = next(
        (sheet.cell(row, headers["User Status"]).value, sheet.cell(row, headers["Auto Status"]).value)
        for row in range(2, sheet.max_row + 1)
        if sheet.cell(row, headers["ID"]).value == 1
    )

    assert exported == ("FINALIST", "RESERVE")
    assert result.loc[0, "auto_status"] == "RESERVE"


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


@pytest.mark.parametrize(("stage_id", "scope", "column", "reason", "tag_source"), [
    ("filter_lot_variant_redundancy", "pair_side_timeframe", "eliminated_by_filter_lot_variant_redundancy", "LOT_VARIANT_FULL_DD5", "SELECTION_LOT_VARIANT"),
    ("filter_hard_cutoffs", "pair_side", "eliminated_by_filter_hard_cutoffs", 'FILTER_HARD_CUTOFFS:{"triggered":["PNL30_FLOOR"]}', "SELECTION_HARD_CUTOFF"),
    ("ab_deterioration", "pair_side", "eliminated_by_ab_deterioration", "AB_DETERIORATION;B_PNL30_FLOOR", "SELECTION_AB_DETERIORATION"),
])
def test_panel_xlsx_shows_pending_automatic_filter_rejection(
    tmp_path: Path, monkeypatch, stage_id: str, scope: str, column: str, reason: str, tag_source: str,
) -> None:
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
    monkeypatch.setattr(
        "mrs3.panel.latest_user_reviews_by_strategy",
        lambda *_args, **_kwargs: {1: {"user_status": "FINALIST"}},
    )
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": stage_id, "enabled": True, "scope": scope},
    ]})
    result = _result()
    result[column] = [True, False]
    result.loc[0, "auto_status"] = "FILTERED"
    result.loc[0, "finalist"] = False
    result.loc[0, "elimination_reason"] = reason
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
    assert result.loc[0, "auto_status"] == "FILTERED"
    with duckdb.connect(str(database_root / "strategy_performance.duckdb"), read_only=True) as check:
        assert check.execute(
            "select source from strategy_tags where strategy_id = 1 and tag = 'REJECTED'"
        ).fetchone() == (tag_source,)


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
    result["_equity_regime_cache"] = None
    for row_index in result.index:
        result_id = int(result.at[row_index, "result_id"])
        result.at[row_index, "_equity_regime_cache"] = _equity_regime_cache_entry(connection, result_id)
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
                ?, 1, 101, 'FINALIST', 0, 1, null, null, null, true, '{}', null
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
                ?, ?, ?, 'FINALIST', 0, 1, null, null, null, ?, '{}', null
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
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values ('review-empty', 'run-empty-review', 'hash-empty', ?, 0)",
        [start + timedelta(seconds=5)],
    )
    _insert_decision_run(
        connection, "run-reviewed", start + timedelta(seconds=6),
        '{"ranking_scope":"RETEST_COHORT"}', result_rows=((1, False), (2, False)),
    )
    tie_time = start + timedelta(seconds=7)
    connection.execute(
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values ('review-active-a', 'run-reviewed', 'hash-a', ?, 1)",
        [tie_time],
    )
    connection.execute(
        "insert into selection_review_rows values ('review-active-a', 1, 'FINALIST', 3, null, null)",
    )
    connection.execute(
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values ('review-active-z', 'run-reviewed', 'hash-z', ?, 2)",
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
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values ('mixed-review', 'mixed-run-101', 'mixed-hash', ?, 2)",
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
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values ('btc-review', 'btc-reviewed', 'btc-hash', ?, 1)",
        [start + timedelta(seconds=50)],
    )
    connection.execute(
        "insert into selection_review_rows values ('btc-review', 11, 'FINALIST', 1, null, null)",
    )
    _insert_decision_run(
        connection, "btc-sibling-reviewed", start + timedelta(seconds=6),
        '{"ranking_scope":"CURRENT_EFFECTIVE"}', symbol="BTCUSDT", side="LONG",
        result_rows=((2, False), (11, False), (20, False), (21, False)),
    )
    connection.execute(
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values ('btc-sibling-review', 'btc-sibling-reviewed', 'btc-sibling-hash', ?, 1)",
        [start + timedelta(seconds=70)],
    )
    connection.execute(
        "insert into selection_review_rows values ('btc-sibling-review', 2, 'RESERVE', 2, null, null)",
    )
    connection.execute(
        "insert into strategy_tags values (2, 'REJECTED', 'scoped-fixture', 'sibling', ?)",
        [start + timedelta(seconds=70)],
    )
    connection.execute(
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values ('eth-review', 'eth-reviewed', 'eth-hash', ?, 2)",
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
    filtered_counted = _CountingConnection(connection)
    btc_filtered_decisions = effective_selection_decisions(
        filtered_counted, symbol="BTCUSDT", strategy_ids=[2, 11, 20, 21],
    )
    strict_counted = _CountingConnection(connection)
    btc_strict_decisions = effective_selection_decisions(
        strict_counted, symbol="BTCUSDT", strategy_ids=(11,),
    )
    btc_lineage = {
        strategy_id: decision
        for strategy_id, decision in all_decisions.items()
        if str(decision[2]).startswith("btc-")
    }

    assert btc_decisions == btc_lineage == {
        2: ("REJECTED", 2, "btc-sibling-reviewed"),
        11: ("RESERVE", 88, "btc-reviewed"),
        20: ("REJECTED", None, "btc-base"),
        21: ("REJECTED", None, "btc-base"),
    }
    assert btc_filtered_decisions == btc_lineage
    assert btc_strict_decisions == {11: ("RESERVE", 88, "btc-reviewed")}
    assert any("rows.strategy_id in" in sql.lower() for sql, _ in filtered_counted.calls)
    assert any(
        "from selection_review_rows rows" in sql.lower()
        and "where rows.strategy_id in" in sql.lower()
        and parameters == [[11]]
        for sql, parameters in strict_counted.calls
    )
    assert 12 not in btc_decisions
    assert all_decisions[12] == ("FINALIST", 77, "eth-reviewed")
    assert len(counted.calls) == 5
    assert sum(parameters == ["BTCUSDT"] for _, parameters in counted.calls) == 3


@pytest.mark.parametrize("schema_version", ["5", "8"])
def test_rejected_strategy_ids_scopes_legacy_schema_fallback(
    tmp_path: Path, schema_version: str,
) -> None:
    connection = _database(tmp_path)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    connection.execute(
        "insert into strategy_tags values (1, 'REJECTED', 'legacy-fixture', 'legacy', ?)", [now]
    )
    connection.execute("drop table strategy_rejection_sources")
    connection.execute("update schema_info set value = ? where key = 'schema_version'", [schema_version])

    assert _rejected_strategy_ids(connection, [1]) == {1}
    assert _rejected_strategy_ids(connection, [2]) == set()


def test_rejected_strategy_ids_scopes_rejection_sources(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    connection.execute(
        """insert into strategy_rejection_sources (
               strategy_id, source_kind, reason_code, first_result_id,
               first_selection_run_id, classifier_algo_version, source_revision,
               facts_sha256, created_at_utc
           ) values
               (1, 'EQUITY_REGIME_FILTER', 'DD_14_7_GTE_23', 101, 'fixture-run', 'v1', 'rev', 'one', ?),
               (2, 'EQUITY_REGIME_FILTER', 'DD_14_7_GTE_23', 102, 'fixture-run', 'v1', 'rev', 'two', ?)""",
        [now, now],
    )

    assert _rejected_strategy_ids(connection, [1]) == {1}


def test_effective_selection_decisions_requires_symbol_for_strategy_scope(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    with pytest.raises(ValueError, match="symbol is required"):
        effective_selection_decisions(connection, strategy_ids=[1])
    assert effective_selection_decisions(connection, symbol="BTCUSDT", strategy_ids=()) == {}


def test_effective_selection_decisions_streams_results_across_fetchmany_boundary(
    tmp_path: Path,
) -> None:
    connection = _database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    _insert_decision_run(connection, "wide-base", start, "{}")
    connection.execute(
        """insert into selection_results
           select 'wide-base', strategy_id, 100000 + strategy_id, 'FINALIST', 0, 1,
                  null, null, null, true, '{}', null
             from range(1, 1026) as values(strategy_id)""",
    )
    connection.execute("update selection_runs set candidate_count = 1025 where selection_run_id = 'wide-base'")
    _insert_decision_run(
        connection, "wide-overlay", start + timedelta(seconds=1),
        '{"ranking_scope":"RETEST_COHORT"}', result_rows=((1025, False),),
    )
    connection.execute(
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values ('wide-review', 'wide-overlay', 'wide-hash', ?, 1)",
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
    regime_evidence = result.attrs["equity_regime_evidence"]
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "equity-review.xlsx", request, metadata, _review_rows(result))
    assert result.attrs["equity_regime_evidence"] is regime_evidence
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


def test_equity_quality_rank_blocks_publication_when_required_cache_is_missing(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    candidates = load_selection_candidates(connection, request, SelectionConfig(), cache_only=True)
    result = run_selection(apply_prior_rejected(connection, candidates), request)
    assert set(result.attrs["equity_quality_facts"]) == {"1", "2"}
    assert connection.execute(
        "select count(*) from equity_quality_metrics where algo_version = ?", ["equity-quality-r7.3-v1"],
    ).fetchone() == (0,)
    metadata = new_run_metadata(connection, request)
    path = write_selection_workbook(result, tmp_path / "r73-cache-miss.xlsx", request, metadata, _review_rows(result))

    with pytest.raises(SelectionReviewError) as raised:
        persist_selection_snapshot(connection, request, SelectionConfig(), result, metadata, path.read_bytes())

    assert raised.value.code == "SELECTION_CACHE_INCOMPLETE"
    assert connection.execute(
        "select count(*) from selection_runs where selection_run_id = ?",
        [metadata["selection_run_id"]],
    ).fetchone() == (0,)


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


def test_mixed_equity_retest_cohort_keeps_rejected_pass_candidate_as_reserve(tmp_path: Path) -> None:
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
    for result_id, step in ((101, Decimal("0.1")), (102, Decimal("0.05")), (103, Decimal("0.03"))):
        connection.executemany(
            """insert into strategy_equity (result_id, sample_index, timestamp_utc, wallet, equity)
               values (?, ?, ?, 100, ?)""",
            [
                [result_id, sample_index, start + timedelta(hours=6 * sample_index),
                 Decimal(100) + Decimal(sample_index) * step]
                for sample_index in range(113)
            ],
        )
        _equity_regime_cache_entry(connection, result_id)

    base_request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    request = retest_cohort_request(base_request, "mixed-review-cohort", {1: 101, 2: 102, 3: 103})
    config = SelectionConfig(lot_variant_redundancy_enabled=False)
    candidates = load_selection_candidates(connection, request, config, cache_only=True)
    assert candidates["_equity_regime_cache"].map(
        lambda value: value["assessment"].decision
    ).tolist() == ["PASS", "PASS", "PASS"]
    result = run_selection(apply_prior_rejected(connection, candidates), request, config)
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
    assert int(decisions.loc[3, "final_rank"]) in {1, 2, 3}
    assert sorted(int(value) for value in decisions["final_rank"].dropna()) == [1, 2, 3]
    assert all(isinstance(value, Decimal) for value in decisions["final_score"])
    sources = stored["equity_quality_snapshot"]["sources"]
    assert set(sources) == {"1", "2", "3"}
    assert sources["3"]["decision_facts"]["score12"] is not None
    assert decisions.loc[3, "equity_regime_decision"] == "PASS"
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
        assert visible[-10:] == [
            "Lots", "Points", "MA", "Auto Status", "Auto Rank", "User Status", "User Rank",
            "RETEST", "Comment", "Причина",
        ]
        assert not {"Equity state", "Equity basis", "Equity DD, %", "Equity smoothness"}.intersection(headers)
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


def _partial_selection_workbook(
    metadata: dict[str, str], rows: list[tuple[object, object, object]], *,
    width: int = 73, id_column: int = 1, status_column: int = 67, rank_column: int = 68,
) -> bytes:
    workbook = Workbook()
    meta = workbook.active
    meta.title = META_SHEET
    for key, value in metadata.items():
        meta.append([key, value])
    meta.sheet_state = "veryHidden"
    sheet = workbook.create_sheet("All candidates")
    headers = [None] * width
    headers[id_column - 1] = "ID"
    headers[status_column - 1] = "User Status"
    headers[rank_column - 1] = "User Rank"
    sheet.append(headers)
    for strategy_id, status, rank in rows:
        values = [None] * width
        values[id_column - 1] = strategy_id
        values[status_column - 1] = status
        values[rank_column - 1] = rank
        sheet.append(values)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _import_partial_selection(connection: duckdb.DuckDBPyConnection, data: bytes) -> dict[str, object]:
    from mrs3 import performance_v2_selection_review as selection_review_module

    importer = getattr(selection_review_module, "import_selection_user_fields", None)
    assert callable(importer), "partial selection user-fields importer has not been implemented"
    return importer(connection, data)


@pytest.mark.parametrize(
    ("width", "id_column", "status_column", "rank_column"),
    [(73, 1, 67, 68), (99, 1, 93, 94), (69, 1, 63, 64)],
)
def test_partial_selection_import_uses_exact_headers_across_workbook_layouts(
    tmp_path: Path, width: int, id_column: int, status_column: int, rank_column: int,
) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    data = _partial_selection_workbook(
        metadata, [(1, "FINALIST", None), (2, "RESERVE", None)],
        width=width, id_column=id_column, status_column=status_column, rank_column=rank_column,
    )

    imported = _import_partial_selection(connection, data)

    assert imported["row_count"] == imported["applied_count"] == 2
    assert imported["unchanged_count"] == 0
    assert connection.execute(
        "select strategy_id, user_status, user_rank from selection_review_rows order by strategy_id"
    ).fetchall() == [(1, "FINALIST", None), (2, "RESERVE", None)]


@pytest.mark.parametrize("missing_header", ["ID", "User Status", "User Rank"])
def test_partial_selection_import_requires_all_field_headers_atomically(
    tmp_path: Path, missing_header: str,
) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    workbook = load_workbook(BytesIO(_partial_selection_workbook(metadata, [(1, None, None)])))
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(1, headers[missing_header]).value = None
    data = BytesIO()
    workbook.save(data)

    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, data.getvalue())

    assert raised.value.code == "SELECTION_REVIEW_SCHEMA_MISMATCH"
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)
    assert connection.execute("select count(*) from selection_review_rows").fetchone() == (0,)
    assert connection.execute("select count(*) from strategy_tags").fetchone() == (0,)


def test_partial_selection_import_clears_blank_fields_and_preserves_other_fields(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, metadata = _export(connection, tmp_path)
    full = load_workbook(path)
    sheet = full["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "FINALIST")
    sheet.cell(2, headers["User Rank"], 5)
    sheet.cell(2, headers["Comment"], "preserve finalist comment")
    sheet.cell(3, headers["User Status"], "ANALOG")
    sheet.cell(3, headers["User Rank"]).value = None
    sheet.cell(3, headers["Analog Of ID"], 1)
    sheet.cell(3, headers["Comment"], "preserve analog comment")
    saved = BytesIO()
    full.save(saved)
    import_selection_review(connection, saved.getvalue())
    data = _partial_selection_workbook(metadata, [(1, None, None), (2, "REJECTED", None)])

    imported = _import_partial_selection(connection, data)

    assert imported["applied_count"] == 2
    assert imported["unchanged_count"] == 0
    assert latest_user_reviews_by_strategy(connection) == {
        1: {"user_status": "RESERVE", "user_rank": None, "user_analog_of_strategy_id": None, "comment": "preserve finalist comment"},
        2: {"user_status": "REJECTED", "user_rank": None, "user_analog_of_strategy_id": None, "comment": "preserve analog comment"},
    }
    assert connection.execute(
        "select strategy_id from strategy_tags where tag = 'REJECTED' order by strategy_id"
    ).fetchall() == [(2,)]


def test_partial_selection_import_applies_blank_rows_and_validates_membership(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)

    cleared = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, None, None)]),
    )

    assert cleared["applied_count"] == 1
    assert cleared["unchanged_count"] == 0
    assert connection.execute(
        "select strategy_id, user_status, user_rank from selection_review_rows order by strategy_id"
    ).fetchall() == [(1, None, None)]
    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, _partial_selection_workbook(metadata, [(999, None, None)]))
    assert raised.value.code == "SELECTION_REVIEW_ROWSET_MISMATCH"


def test_partial_selection_blank_only_import_demotes_prior_finalist(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    first = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, "FINALIST", 3)]),
    )
    row_count_before = connection.execute("select count(*) from selection_review_rows").fetchone()
    import_count_before = connection.execute("select count(*) from selection_review_imports").fetchone()

    cleared = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, None, None)]),
    )

    assert first["row_count"] == first["finalist_count"] == 1
    assert cleared["row_count"] == cleared["applied_count"] == 1
    assert cleared["unchanged_count"] == cleared["finalist_count"] == 0
    assert latest_user_reviews_by_strategy(connection)[1]["user_status"] == "RESERVE"
    assert latest_user_reviews_by_strategy(connection)[1]["user_rank"] is None
    assert effective_selection_decisions(connection)[1][:2] == ("RESERVE", None)
    assert connection.execute("select count(*) from selection_review_rows").fetchone()[0] == row_count_before[0] + 1
    assert connection.execute("select count(*) from selection_review_imports").fetchone()[0] == import_count_before[0] + 1


def test_partial_selection_import_reserves_absent_prior_finalists_and_clears_rejected_rank(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    prior_review_id = "prior-partial-review"
    prior_imported_at = datetime(2026, 9, 3, tzinfo=UTC)
    connection.execute(
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values (?, ?, ?, ?, 2)",
        [prior_review_id, metadata["selection_run_id"], "d" * 64, prior_imported_at],
    )
    connection.executemany(
        "insert into selection_review_rows values (?, ?, 'FINALIST', ?, null, ?)",
        [
            (prior_review_id, 1, 1, "keep comment one"),
            (prior_review_id, 2, 2, "keep comment two"),
        ],
    )
    prior_ledger = connection.execute(
        "select review_import_id, strategy_id, user_status, user_rank, comment "
        "from selection_review_rows order by review_import_id, strategy_id"
    ).fetchall()

    imported = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, "REJECTED", 99)]),
    )

    assert imported["row_count"] == imported["applied_count"] == 2
    assert latest_user_reviews_by_strategy(connection) == {
        1: {"user_status": "REJECTED", "user_rank": None, "user_analog_of_strategy_id": None, "comment": "keep comment one"},
        2: {"user_status": "RESERVE", "user_rank": None, "user_analog_of_strategy_id": None, "comment": "keep comment two"},
    }
    assert connection.execute(
        "select strategy_id from strategy_tags where tag = 'REJECTED' order by strategy_id"
    ).fetchall() == [(1,)]
    assert connection.execute(
        "select review_import_id, strategy_id, user_status, user_rank, comment "
        "from selection_review_rows where review_import_id != ? order by review_import_id, strategy_id",
        [imported["review_import_id"]],
    ).fetchall() == prior_ledger


def test_partial_selection_reconciles_prior_finalist_absent_from_current_run(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    now = datetime(2026, 9, 2, tzinfo=UTC)
    connection.execute(
        "insert into strategies values (3, 'older-run-only', 'BTCUSDT', 'LONG', '1h', 5, 1, 'run', 'candidate-3', 'ACTIVE', null, ?, ?)",
        [now, now],
    )
    old_run_id = "older-run-btc-long"
    connection.execute(
        """insert into selection_runs (
               selection_run_id, database_instance_id, symbol, side, selection_contract_version,
               request_json, request_sha256, config_json, config_sha256, candidate_count,
               representative_count, auto_finalist_count, top_n, workbook_sha256, created_at_utc
           ) values (?, (select value from schema_info where key='database_instance_id'),
                     'BTCUSDT', 'LONG', 'performance-v2-selection-review-v1', '{}', ?, '{}', ?,
                     1, 1, 1, 20, ?, ?)""",
        [old_run_id, "a" * 64, "b" * 64, "c" * 64, now],
    )
    connection.execute(
        """insert into selection_results (
               selection_run_id, strategy_id, result_id_at_selection, auto_status, auto_score,
               auto_rank, auto_reason, analog_group_key, auto_analog_of_strategy_id,
               prior_rejected, stage_trace_json
           ) values (?, 3, 103, 'FINALIST', 90, 1, null, null, null, false, '{}')""",
        [old_run_id],
    )
    connection.execute(
        "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values ('older-finalist-review', ?, ?, ?, 1)",
        [old_run_id, "e" * 64, now],
    )
    connection.execute(
        "insert into selection_review_rows values ('older-finalist-review', 3, 'FINALIST', 1, null, 'older comment')"
    )
    assert connection.execute(
        "select count(*) from selection_results where selection_run_id = ? and strategy_id = 3",
        [metadata["selection_run_id"]],
    ).fetchone() == (0,)

    imported = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(2, "FINALIST", 1)]),
    )

    assert connection.execute(
        "select strategy_id, user_status, user_rank, comment from selection_review_rows "
        "where review_import_id = ? and strategy_id = 3",
        [imported["review_import_id"]],
    ).fetchone() == (3, "RESERVE", None, "older comment")
    assert latest_user_reviews_by_strategy(connection, [3])[3]["user_status"] == "RESERVE"


def test_partial_selection_does_not_reserve_a_finalist_later_explicitly_rejected(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(1, "FINALIST", 1)]))
    rejected = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, "REJECTED", 77)]),
    )

    next_import = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(2, "FINALIST", 1)]),
    )

    assert latest_user_reviews_by_strategy(connection, [1])[1]["user_status"] == "REJECTED"
    assert latest_user_reviews_by_strategy(connection, [1])[1]["user_rank"] is None
    assert connection.execute(
        "select user_status from selection_review_rows where review_import_id = ? and strategy_id = 1",
        [rejected["review_import_id"]],
    ).fetchone() == ("REJECTED",)
    assert connection.execute(
        "select strategy_id, user_status from selection_review_rows where review_import_id = ? order by strategy_id",
        [next_import["review_import_id"]],
    ).fetchall() == [(2, "FINALIST")]


def test_partial_selection_reconciliation_does_not_touch_other_pair_or_side(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(1, "FINALIST", 1)]))
    now = datetime(2026, 9, 2, tzinfo=UTC)
    for strategy_id, symbol, side in ((3, "ETHUSDT", "LONG"), (4, "BTCUSDT", "SHORT")):
        connection.execute(
            "insert into strategies values (?, ?, ?, ?, '1h', 5, 1, 'run', ?, 'ACTIVE', null, ?, ?)",
            [strategy_id, f"strategy-{strategy_id}", symbol, side, f"candidate-{strategy_id}", now, now],
        )
        run_id = f"prior-{strategy_id}"
        connection.execute(
            """insert into selection_runs (
                   selection_run_id, database_instance_id, symbol, side, selection_contract_version,
                   request_json, request_sha256, config_json, config_sha256, candidate_count,
                   representative_count, auto_finalist_count, top_n, workbook_sha256, created_at_utc
               ) values (?, (select value from schema_info where key='database_instance_id'), ?, ?,
                         'performance-v2-selection-review-v1', '{}', ?, '{}', ?, 1, 1, 1, 20, ?, ?)""",
            [run_id, symbol, side, "a" * 64, "b" * 64, "c" * 64, now],
        )
        review_id = f"prior-import-{strategy_id}"
        connection.execute(
            "insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count) values (?, ?, ?, ?, 1)",
            [review_id, run_id, f"{strategy_id:064d}", now],
        )
        connection.execute(
            "insert into selection_review_rows values (?, ?, 'FINALIST', 1, null, 'out of scope')",
            [review_id, strategy_id],
        )

    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(2, "FINALIST", 1)]))

    assert latest_user_reviews_by_strategy(connection, [3, 4]) == {
        3: {"user_status": "FINALIST", "user_rank": 1, "user_analog_of_strategy_id": None, "comment": "out of scope"},
        4: {"user_status": "FINALIST", "user_rank": 1, "user_analog_of_strategy_id": None, "comment": "out of scope"},
    }


def test_partial_selection_rejected_stale_rank_clears_for_nonprior_finalist(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)

    imported = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, "REJECTED", 99)]),
    )

    assert latest_user_reviews_by_strategy(connection)[1]["user_status"] == "REJECTED"
    assert latest_user_reviews_by_strategy(connection)[1]["user_rank"] is None
    assert connection.execute(
        "select row_count from selection_review_imports where review_import_id = ?",
        [imported["review_import_id"]],
    ).fetchone() == (1,)


@pytest.mark.parametrize("stale_rank", [99, 0, "not-a-rank"])
def test_partial_selection_import_clears_stale_rank_from_prior_finalist_reserve(
    tmp_path: Path, stale_rank: object,
) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, "FINALIST", 1), (2, "FINALIST", 2)]),
    )

    imported = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, "RESERVE", stale_rank)]),
    )

    assert imported["row_count"] == imported["applied_count"] == 2
    assert latest_user_reviews_by_strategy(connection) == {
        1: {"user_status": "RESERVE", "user_rank": None, "user_analog_of_strategy_id": None, "comment": None},
        2: {"user_status": "RESERVE", "user_rank": None, "user_analog_of_strategy_id": None, "comment": None},
    }


@pytest.mark.parametrize("status", ["FINALIST", "RESERVE", "REJECTED"])
def test_partial_selection_import_preserves_prior_comment_for_each_supported_status(
    tmp_path: Path, status: str,
) -> None:
    connection = _database(tmp_path)
    path, metadata = _export(connection, tmp_path)
    full = load_workbook(path)
    sheet = full["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "FINALIST")
    sheet.cell(2, headers["User Rank"], 5)
    sheet.cell(2, headers["Comment"], "keep this comment")
    sheet.cell(2, headers["Analog Of ID"]).value = None
    sheet.cell(3, headers["User Status"], "RESERVE")
    sheet.cell(3, headers["User Rank"], 6)
    sheet.cell(3, headers["Comment"], "other comment")
    sheet.cell(3, headers["Analog Of ID"]).value = None
    saved = BytesIO()
    full.save(saved)
    import_selection_review(connection, saved.getvalue())
    rank = 1 if status == "FINALIST" else None

    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(1, status, rank)]))

    assert latest_user_reviews_by_strategy(connection)[1]["comment"] == "keep this comment"


def test_partial_selection_blank_status_clears_existing_analog_target(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, metadata = _export(connection, tmp_path)
    full = load_workbook(path)
    sheet = full["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "FINALIST")
    sheet.cell(2, headers["User Rank"], 1)
    sheet.cell(3, headers["User Status"], "ANALOG")
    sheet.cell(3, headers["Analog Of ID"], 1)
    saved = BytesIO()
    full.save(saved)
    import_selection_review(connection, saved.getvalue())
    ledger_rows_before = connection.execute("select count(*) from selection_review_rows").fetchone()

    imported = _import_partial_selection(connection, _partial_selection_workbook(metadata, [(2, None, None)]))

    assert imported["applied_count"] == 2
    assert latest_user_reviews_by_strategy(connection)[1]["user_status"] == "RESERVE"
    assert latest_user_reviews_by_strategy(connection)[1]["user_rank"] is None
    assert latest_user_reviews_by_strategy(connection)[2]["user_status"] is None
    assert latest_user_reviews_by_strategy(connection)[2]["user_analog_of_strategy_id"] is None
    assert connection.execute("select count(*) from selection_review_rows").fetchone()[0] == ledger_rows_before[0] + 2


def test_partial_selection_blank_fields_demote_prior_finalist_before_same_run_uniqueness_check(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(1, "FINALIST", 1)]))

    imported = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, None, None), (2, "FINALIST", 1)]),
    )

    assert imported["applied_count"] == 2
    assert latest_user_reviews_by_strategy(connection)[1]["user_status"] == "RESERVE"
    assert latest_user_reviews_by_strategy(connection)[1]["user_rank"] is None
    assert latest_user_reviews_by_strategy(connection)[2]["user_status"] == "FINALIST"
    assert latest_user_reviews_by_strategy(connection)[2]["user_rank"] == 1


def test_partial_selection_new_finalist_demotes_absent_prior_finalist_before_rank_check(
    tmp_path: Path,
) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)

    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(1, None, 7)]))
    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(2, "FINALIST", 7)]))

    assert latest_user_reviews_by_strategy(connection)[1]["user_status"] is None
    assert latest_user_reviews_by_strategy(connection)[1]["user_rank"] == 7
    assert latest_user_reviews_by_strategy(connection)[2]["user_status"] == "FINALIST"
    assert latest_user_reviews_by_strategy(connection)[2]["user_rank"] == 7

    imported = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(1, "FINALIST", 7)]),
    )

    assert imported["applied_count"] == 2
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (3,)
    assert latest_user_reviews_by_strategy(connection)[1]["user_status"] == "FINALIST"
    assert latest_user_reviews_by_strategy(connection)[1]["user_rank"] == 7
    assert latest_user_reviews_by_strategy(connection)[2]["user_status"] == "RESERVE"
    assert latest_user_reviews_by_strategy(connection)[2]["user_rank"] is None


def test_partial_selection_clear_rejected_tag_preserves_other_tags(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, metadata = _export(connection, tmp_path)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    for row in (2, 3):
        sheet.cell(row, headers["User Status"]).value = "REJECTED"
        sheet.cell(row, headers["User Rank"]).value = None
        sheet.cell(row, headers["Analog Of ID"]).value = None
    sheet.cell(3, headers["RETEST"]).value = "RETEST"
    reviewed = BytesIO()
    workbook.save(reviewed)
    import_selection_review(connection, reviewed.getvalue())

    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(1, None, None)]))

    assert connection.execute(
        "select strategy_id, tag from strategy_tags order by strategy_id, tag"
    ).fetchall() == [(2, "REJECTED"), (2, "RETEST")]


def test_partial_selection_clear_exports_prior_finalist_as_rankless_reserve(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(1, "FINALIST", 3)]))
    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(1, None, None)]))

    output = write_selection_workbook(
        _result(), tmp_path / "after-clear.xlsx", _request(), metadata,
        latest_user_reviews_by_strategy(connection),
    )
    sheet = load_workbook(output, data_only=True)["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    row = next(row for row in range(2, sheet.max_row + 1) if sheet.cell(row, headers["ID"]).value == 1)

    assert sheet.cell(row, headers["User Status"]).value == "RESERVE"
    assert sheet.cell(row, headers["User Rank"]).value is None


def test_partial_selection_import_replaces_absent_prior_finalist_rank_in_same_run(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    first = _partial_selection_workbook(metadata, [(1, "FINALIST", 1)])
    second = _partial_selection_workbook(metadata, [(2, "FINALIST", 1)])
    _import_partial_selection(connection, first)

    imported = _import_partial_selection(connection, second)

    assert imported["applied_count"] == 2
    assert latest_user_reviews_by_strategy(connection) == {
        1: {"user_status": "RESERVE", "user_rank": None, "user_analog_of_strategy_id": None, "comment": None},
        2: {"user_status": "FINALIST", "user_rank": 1, "user_analog_of_strategy_id": None, "comment": None},
    }
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (2,)


def test_partial_selection_import_write_failure_rolls_back_and_connection_is_reusable(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3 import performance_v2_selection_review as selection_review_module

    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    now = datetime(2026, 9, 2, tzinfo=UTC)
    connection.execute(
        "insert into strategy_tags values (1, 'REJECTED', 'TEST', 'existing', ?)", [now],
    )
    data = _partial_selection_workbook(metadata, [(1, "REJECTED", None)])
    original_insert_rows = selection_review_module._insert_rows

    def fail_rejected_tag_insert(conn, table, columns, rows):
        if table == "strategy_tags":
            raise RuntimeError("forced tag insert failure")
        return original_insert_rows(conn, table, columns, rows)

    monkeypatch.setattr(selection_review_module, "_insert_rows", fail_rejected_tag_insert)
    with pytest.raises(RuntimeError, match="forced tag insert failure"):
        _import_partial_selection(connection, data)

    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)
    assert connection.execute("select count(*) from selection_review_rows").fetchone() == (0,)
    assert connection.execute(
        "select strategy_id, tag, source, source_ref from strategy_tags where strategy_id = 1",
    ).fetchall() == [(1, "REJECTED", "TEST", "existing")]
    monkeypatch.setattr(selection_review_module, "_insert_rows", original_insert_rows)
    retried = _import_partial_selection(connection, data)
    assert retried["applied_count"] == 1


@pytest.mark.parametrize(("status", "rank"), [("RESERVE", 1)])
def test_partial_selection_import_rejects_rank_for_nonfinalist_atomically(
    tmp_path: Path, status: str, rank: int,
) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    data = _partial_selection_workbook(metadata, [(1, status, rank)])

    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, data)
    assert raised.value.code == "SELECTION_REVIEW_INVALID_RANK"

    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)
    assert connection.execute("select count(*) from selection_review_rows").fetchone() == (0,)


def test_partial_selection_import_missing_finalist_rank_and_atomic_unknown_id_and_duplicate(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    _import_partial_selection(
        connection, _partial_selection_workbook(metadata, [(2, "FINALIST", 7)]),
    )
    data = _partial_selection_workbook(metadata, [(1, "FINALIST", None)])

    imported = _import_partial_selection(connection, data)

    assert imported["applied_count"] == 2
    assert connection.execute(
        "select user_status, user_rank from selection_review_rows where strategy_id = 1"
    ).fetchone() == ("FINALIST", None)
    assert latest_user_reviews_by_strategy(connection)[2]["user_status"] == "RESERVE"
    assert latest_user_reviews_by_strategy(connection)[2]["user_rank"] is None
    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, data)
    assert raised.value.code == "SELECTION_REVIEW_ALREADY_IMPORTED"
    bad = _partial_selection_workbook(metadata, [(1, "REJECTED", None), (999, "FINALIST", 1)])
    before = connection.execute("select count(*) from selection_review_imports").fetchone()
    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, bad)
    assert raised.value.code == "SELECTION_REVIEW_ROWSET_MISMATCH"
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == before
    duplicate_id = _partial_selection_workbook(metadata, [(1, "FINALIST", 1), (1, "REJECTED", None)])
    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, duplicate_id)
    assert raised.value.code == "SELECTION_REVIEW_ROWSET_MISMATCH"
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == before
    assert latest_user_reviews_by_strategy(connection) == {
        1: {"user_status": "FINALIST", "user_rank": None, "user_analog_of_strategy_id": None, "comment": None},
        2: {"user_status": "RESERVE", "user_rank": None, "user_analog_of_strategy_id": None, "comment": None},
    }


def test_partial_selection_import_allows_multiple_finalists_without_ranks(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)

    imported = _import_partial_selection(
        connection,
        _partial_selection_workbook(metadata, [(1, "FINALIST", None), (2, "FINALIST", None)]),
    )

    assert imported["applied_count"] == 2
    assert latest_user_reviews_by_strategy(connection) == {
        1: {"user_status": "FINALIST", "user_rank": None, "user_analog_of_strategy_id": None, "comment": None},
        2: {"user_status": "FINALIST", "user_rank": None, "user_analog_of_strategy_id": None, "comment": None},
    }


def test_partial_selection_import_blank_finalist_rank_clears_previous_rank(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    path, metadata = _export(connection, tmp_path)
    full = load_workbook(path)
    sheet = full["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["User Status"], "FINALIST")
    sheet.cell(2, headers["User Rank"], 5)
    sheet.cell(2, headers["Analog Of ID"]).value = None
    sheet.cell(3, headers["User Status"], "RESERVE")
    sheet.cell(3, headers["User Rank"], 6)
    sheet.cell(3, headers["Analog Of ID"]).value = None
    saved = BytesIO()
    full.save(saved)
    import_selection_review(connection, saved.getvalue())

    _import_partial_selection(connection, _partial_selection_workbook(metadata, [(1, "FINALIST", None)]))

    assert latest_user_reviews_by_strategy(connection)[1]["user_rank"] is None


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    [
        ([(1, "ANALOG", None)], "SELECTION_REVIEW_INVALID_STATUS"),
        ([(1, "FINALIST", 0)], "SELECTION_REVIEW_INVALID_RANK"),
        ([(1, "FINALIST", " ")], "SELECTION_REVIEW_INVALID_RANK"),
        ([(1, "FINALIST", 4), (2, "FINALIST", 4)], "SELECTION_REVIEW_INVALID_RANK"),
    ],
)
def test_partial_selection_import_rejects_invalid_status_and_finalist_ranks_atomically(
    tmp_path: Path, rows: list[tuple[object, object, object]], expected_code: str,
) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    data = _partial_selection_workbook(metadata, rows)

    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, data)

    assert raised.value.code == expected_code
    assert connection.execute("select count(*) from selection_review_imports").fetchone() == (0,)
    assert connection.execute("select count(*) from selection_review_rows").fetchone() == (0,)


def test_partial_selection_import_checks_instance_run_and_snapshot_without_latest_or_current_result_gate(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    _, metadata = _export(connection, tmp_path)
    valid_row = [(1, "REJECTED", None)]
    wrong_instance = _partial_selection_workbook(
        {**metadata, "database_instance_id": "different-db"}, valid_row,
    )
    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, wrong_instance)
    assert raised.value.code == "SELECTION_REVIEW_DATABASE_MISMATCH"

    unknown_run = _partial_selection_workbook(
        {**metadata, "selection_run_id": "missing-run"}, valid_row,
    )
    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, unknown_run)
    assert raised.value.code == "SELECTION_REVIEW_SCHEMA_MISMATCH"

    now = datetime(2026, 9, 2, tzinfo=UTC)
    connection.execute(
        """insert into strategies values (3, 'outside-run', 'BTCUSDT', 'LONG', '1h', 5, 1,
           'run', 'candidate-3', 'ACTIVE', null, ?, ?)""",
        [now, now],
    )
    outside_snapshot = _partial_selection_workbook(metadata, [(3, "REJECTED", None)])
    with pytest.raises(SelectionReviewError) as raised:
        _import_partial_selection(connection, outside_snapshot)
    assert raised.value.code == "SELECTION_REVIEW_ROWSET_MISMATCH"

    _, newer_metadata = _export(connection, tmp_path)
    assert newer_metadata["selection_run_id"] != metadata["selection_run_id"]
    connection.execute("update strategies set current_result_id = 102 where strategy_id = 1")
    imported = _import_partial_selection(
        connection, _partial_selection_workbook(metadata, valid_row),
    )
    assert imported["selection_run_id"] == metadata["selection_run_id"]
    assert connection.execute(
        "select strategy_id, user_status from selection_review_rows where review_import_id = ?",
        [imported["review_import_id"]],
    ).fetchall() == [(1, "REJECTED")]
