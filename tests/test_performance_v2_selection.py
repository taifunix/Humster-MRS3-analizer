from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, replace
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from openpyxl.comments import Comment
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
import mrs3.performance_v2_equity_cache as equity_cache_module
import mrs3.performance_v2_selection as selection_module

from mrs3.performance_v2_selection import (
    PerformanceV2SelectionError,
    SelectionConfig,
    _consistency_summary,
    _consistency_windows,
    _ab_metrics_from_windows,
    _holding_p95_minutes,
    _return_30d,
    _selection_windows,
    _trade_rate_30d,
    load_selection_candidates,
    load_selection_config,
    parse_selection_request,
    prepare_selection_window_cache,
    run_selection,
    selection_cache_missing_strategy_ids,
    selection_cache_status,
    write_selection_workbook,
    retest_cohort_request,
)
from mrs3.performance_v2_store import initialize_performance_v2
from mrs3.performance_v2_windows import METRICS_VERSION, WindowMetrics, _cached
from mrs3.performance_v2_equity_cache import (
    EquityQualityCacheError,
    EquitySourceChangedError,
    current_equity_source_metadata,
    decode_equity_facts,
    equity_source_revision,
    read_equity_quality_facts,
)
from mrs3.performance_v2_equity_quality import EquitySample, calculate_equity_quality_facts


def test_retest_cohort_request_is_explicit_and_rejects_empty_members():
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    scoped = retest_cohort_request(request, "bulk-1", {9: 19, 2: 12})
    assert scoped.ranking_scope == "RETEST_COHORT"
    assert scoped.bulk_retest_job_id == "bulk-1"
    assert scoped.cohort_members == ((2, 12), (9, 19))
    with pytest.raises(PerformanceV2SelectionError, match="RETEST_COHORT_NO_SUCCESSFUL_MEMBERS"):
        retest_cohort_request(request, "bulk-1", {})


UTC = timezone.utc


@pytest.mark.parametrize(
    ("duration", "window_count"),
    [
        (timedelta(days=20, hours=23, minutes=59, seconds=59), 0),
        (timedelta(days=21), 3),
        (timedelta(days=27, hours=23, minutes=59, seconds=59), 3),
        (timedelta(days=28), 4),
        (timedelta(days=45), 4),
    ],
)
def test_consistency_windows_have_exact_fractional_calendar_boundaries(duration: timedelta, window_count: int) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + duration

    windows = _consistency_windows(start, end)

    assert len(windows) == window_count
    if windows:
        assert windows[0][0] == start
        assert windows[-1][1] == end
        assert all(left[1] == right[0] for left, right in zip(windows, windows[1:]))


def test_three_consistency_windows_are_requested_without_q4_or_positional_tail() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=21)

    windows = _selection_windows(start, end, SelectionConfig())

    assert all(window in windows for window in _consistency_windows(start, end))
    assert not any(window[0] == start + timedelta(days=15.75) for window in windows)
    assert windows[-1][1] == end


def _consistency_metric(start: datetime, end: datetime, growth: str = "1.1") -> WindowMetrics:
    return WindowMetrics(
        1, start, end, METRICS_VERSION, start, end, "AVAILABLE", None,
        Decimal(growth), None, None, None, Decimal("2"), None,
        None, None, 1, Decimal("100"),
    )


def test_time_consistency_status_counts_positive_windows_and_handles_unavailable() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    four = _consistency_windows(start, start + timedelta(days=28))
    three = _consistency_windows(start, start + timedelta(days=21))

    assert _consistency_summary([_consistency_metric(*window) for window in four[:3]] + [_consistency_metric(*four[3], "1")], four) == (3, 4, "PASS")
    assert _consistency_summary([_consistency_metric(*window) for window in four[:2]] + [_consistency_metric(*four[2], "1"), _consistency_metric(*four[3], "1")], four) == (2, 4, "FAIL")
    assert _consistency_summary([_consistency_metric(*window) for window in three[:2]] + [_consistency_metric(*three[2], "1")], three) == (2, 3, "PASS")
    assert _consistency_summary([_consistency_metric(*window) for window in three[:1]] + [_consistency_metric(*three[1], "1"), _consistency_metric(*three[2], "1")], three) == (1, 3, "FAIL")
    no_trades = WindowMetrics.unavailable(1, *four[3], "NO_TRADES")
    collapsed = WindowMetrics.unavailable(1, *four[3], "COLLAPSED")
    assert _consistency_summary([], ()) == (None, None, "UNAVAILABLE")
    assert _consistency_summary([_consistency_metric(*window) for window in four[:3]] + [no_trades], four) == (3, 3, "PASS")
    assert _consistency_summary([_consistency_metric(*window) for window in four[:2]] + [no_trades, no_trades], four) == (2, 2, "FAIL")
    assert _consistency_summary([no_trades] * 4, four) == (None, 0, "UNAVAILABLE")
    assert _consistency_summary([_consistency_metric(*window) for window in four[:3]] + [collapsed], four) == (None, None, "UNAVAILABLE")
    assert _consistency_summary([_consistency_metric(*window) for window in four[:3]] + [None], four) == (None, None, "UNAVAILABLE")


def test_consistency_summary_passes_each_subwindow_bounds_to_normalization(monkeypatch) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    windows = _consistency_windows(start, start + timedelta(days=21))
    metrics = [_consistency_metric(*window) for window in windows]
    calls = []
    monkeypatch.setattr(selection_module, "_return_30d", lambda _metric, window_start, window_end: calls.append((window_start, window_end)) or Decimal("1"))

    assert _consistency_summary(metrics, windows) == (3, 3, "PASS")
    assert calls == list(windows)


def test_low_trades_filter_uses_calendar_rate_and_excludes_missing_rates() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_low_trades", "enabled": True, "scope": "pair_side"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("a", strategy_id=1, total_trades=1, trades_30d=10),
        _selection_row("b", strategy_id=2, total_trades=1000, trades_30d=10),
        _selection_row("c", strategy_id=3, total_trades=2, trades_30d=10),
        _selection_row("missing", strategy_id=4, total_trades=0, trades_30d=None),
    ]), request).set_index("strategy_name")

    assert result["eliminated_by_filter_low_trades"].sum() == 0
    assert result.loc["missing", "finalist"]


def test_unavailable_time_consistency_survives_and_exports_na(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_time_consistency", "enabled": True, "scope": "pair_side_timeframe"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("unavailable", strategy_id=1, positive_quarter_count=3, positive_quarter_available_count=4, positive_quarter_status="UNAVAILABLE"),
        _selection_row("fail", strategy_id=2, positive_quarter_count=1, positive_quarter_available_count=4, positive_quarter_status="FAIL"),
    ]), request)
    result = result.set_index("strategy_name")

    assert result.loc["unavailable", "finalist"]
    assert result.loc["fail", "eliminated_by_filter_time_consistency"]
    book = load_workbook(write_selection_workbook(result.reset_index(), tmp_path / "consistency.xlsx", request), data_only=True)
    headers = [cell.value for cell in book["All candidates"][1]]
    assert "positive_quarter_status" not in headers
    data_rows = {
        book["All candidates"].cell(row, headers.index("Стратегия") + 1).value: row
        for row in range(2, book["All candidates"].max_row + 1)
    }
    unavailable_row = data_rows["unavailable"]
    assert book["All candidates"].cell(unavailable_row, headers.index("Positive windows") + 1).value == "N/A"
    assert book["All candidates"].cell(unavailable_row, headers.index("eliminated_by_filter_time_consistency") + 1).value == "N/A"


def test_unavailable_status_exports_na_without_count_fields(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    result = run_selection(pd.DataFrame([_selection_row("unavailable", positive_quarter_status="UNAVAILABLE")]), request)
    result = result.drop(columns=["positive_quarter_count", "positive_quarter_available_count"], errors="ignore")

    book = load_workbook(write_selection_workbook(result, tmp_path / "status-only.xlsx", request), data_only=True)
    headers = [cell.value for cell in book["All candidates"][1]]
    assert book["All candidates"].cell(2, headers.index("Positive windows") + 1).value == "N/A"


def test_v21_cache_rows_are_stale_for_v22_selection_readiness(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1)
    with duckdb.connect(str(database)) as check:
        check.execute("update window_metrics set metrics_version = 'performance-window-v2.1'")
        assert METRICS_VERSION == "performance-window-v2.2"
        assert selection_cache_status(check, request, SelectionConfig()) == {"total": 1, "missing": 1, "ready": False}


def test_selection_30d_rates_use_calendar_window_with_sparse_events() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    metrics = WindowMetrics(
        1, start, start + timedelta(days=14), "test",
        start, start + timedelta(days=2), "AVAILABLE", None,
        Decimal("1.1"), Decimal("10"), None, None, Decimal("2"), Decimal("5"),
        Decimal("0.1"), Decimal("2"), 5, Decimal("60"),
    )

    assert _return_30d(metrics).quantize(Decimal(".0001")) == Decimal("22.6588")
    assert _trade_rate_30d(metrics).quantize(Decimal(".0001")) == Decimal("10.7143")


def test_selection_ab_metrics_clamp_duration_to_report_bounds() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    metrics = WindowMetrics(
        1, start, start + timedelta(days=14), "test",
        start, start + timedelta(days=2), "AVAILABLE", None,
        Decimal("1.1"), Decimal("10"), None, None, Decimal("2"), Decimal("5"),
        Decimal("0.1"), Decimal("2"), 5, Decimal("60"),
    )

    values = _ab_metrics_from_windows(
        metrics, metrics, report_start_utc=start + timedelta(days=3), report_end_utc=start + timedelta(days=10)
    )

    assert values["ab_calendar_days_a"] == Decimal("7")
    assert values["ab_calendar_days_b"] == Decimal("7")
    assert values["ab_trade_rate_a_30d"] == Decimal(5) * 30 / 7


def _config(path: Path, **selection: object) -> Path:
    path.write_text(
        json.dumps({
            "unified_performance_v2": {
                "database_root": "data/performance-v2",
                "finalist_selection": selection,
            },
        }),
        encoding="utf-8",
    )
    return path


def test_parse_selection_request_accepts_all_known_stages_in_order() -> None:
    stages = [
        "filter_holding_outlier", "filter_low_trades", "filter_min_shift", "ab_deterioration", "pareto_window_b", "pareto_window_b_dd_shift", "pareto_dd5_balanced",
        "pareto_plateau_points_per_order", "pareto_plateau_points_total", "pareto_efficiency_shift",
        "pareto_dd5_holding", "pareto_dd5_close_ma", "pareto_dd5_first_shift",
        "pareto_conditional_close_ma", "pareto_primary", "pareto_dd5_capital", "filter_lot_variant_redundancy",
    ]

    request = parse_selection_request({
        "symbol": "BTCUSDT",
        "side": "LONG",
        "stages": [
            {"id": stage_id, "enabled": index < 5,
             "scope": "pair_side" if index < 4 else "pair_side_timeframe",
             **({"min_shift_pct": "0.3"} if stage_id == "filter_min_shift" else {})}
            for index, stage_id in enumerate(stages)
        ],
    })

    assert request.symbol == "BTCUSDT"
    assert request.side == "LONG"
    assert [stage.id for stage in request.stages] == stages


def test_parse_selection_request_accepts_new_stages_and_requires_last_fixed_ranker() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_robust", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "pareto_shift_near_tie", "enabled": True, "scope": "pair_side_timeframe", "pnl_tolerance_pct": "10"},
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 50},
    ]})

    assert request.stages[-1].top_n == 50
    assert request.stages[-2].pnl_tolerance_pct == Decimal("10")
    with pytest.raises(PerformanceV2SelectionError, match="INVALID_CONFIG_top_n"):
        parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 0},
        ]})
    with pytest.raises(PerformanceV2SelectionError, match="RANK_STAGE_MUST_BE_LAST"):
        parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 50},
            {"id": "pareto_robust", "enabled": True, "scope": "pair_side_timeframe"},
        ]})
    with pytest.raises(PerformanceV2SelectionError, match="DUPLICATE_STAGE"):
        parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 50},
            {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 10},
        ]})


def test_equity_quality_method_is_optional_only_on_the_existing_rank_stage() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 20,
         "method": "equity_quality_v1"},
    ]})

    assert request.stages[0].method == "equity_quality_v1"
    legacy = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 20},
    ]})
    assert legacy.stages[0].method is None
    with pytest.raises(PerformanceV2SelectionError, match="INVALID_RANK_METHOD"):
        parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 20,
             "method": "equity_quality_v2"},
        ]})
    with pytest.raises(PerformanceV2SelectionError, match="INVALID_STAGE"):
        parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "pareto_robust", "enabled": True, "scope": "pair_side_timeframe",
             "method": "equity_quality_v1"},
        ]})


def test_equity_quality_rank_orders_class_then_exact_score_tie_chain() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 20,
         "method": "equity_quality_v1"},
    ]})
    rows = [
        _selection_row("class1-high", strategy_id=1, result_id=101, close_ma_len=2,
                       _equity_quality=_equity_rank_facts(101, 1, "10", "0", "0", 28)),
        _selection_row("class0-low", strategy_id=2, result_id=102, close_ma_len=3,
                       _equity_quality=_equity_rank_facts(102, 0, "1", "0.2", "0.2", 7)),
        _selection_row("dd-tie", strategy_id=8, result_id=108, close_ma_len=4,
                       _equity_quality=_equity_rank_facts(108, 0, "1", "0.05", "0.1", 7)),
        _selection_row("p-tie", strategy_id=9, result_id=109, close_ma_len=5,
                       _equity_quality=_equity_rank_facts(109, 0, "1", "0.05", "0.05", 7)),
        _selection_row("h-tie", strategy_id=10, result_id=110, close_ma_len=6,
                       _equity_quality=_equity_rank_facts(110, 0, "1", "0.05", "0.05", 14)),
        _selection_row("id-tie", strategy_id=3, result_id=103, close_ma_len=7,
                       _equity_quality=_equity_rank_facts(103, 0, "1", "0.05", "0.05", 14)),
    ]

    result = run_selection(pd.DataFrame(rows), request).set_index("strategy_name")

    assert result.sort_values("final_rank").index.tolist() == [
        "id-tie", "h-tie", "p-tie", "dd-tie", "class0-low", "class1-high",
    ]
    assert result.loc["class0-low", "final_rank"] < result.loc["class1-high", "final_rank"]


def test_equity_quality_unscoreable_is_reserve_and_non_up_can_rank() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("unscoreable", strategy_id=1, result_id=101, close_ma_len=2,
                       _equity_quality=_equity_rank_facts(101, None, None, None, None, None)),
        _selection_row("declining", strategy_id=2, result_id=102, close_ma_len=3,
                       _equity_quality=_equity_rank_facts(102, 3, "-2", "0.1", "0.1", 28,
                                                          state="DECLINING_OR_MIXED")),
    ]), request).set_index("strategy_name")

    assert result.loc["declining", "auto_status"] == "FINALIST"
    assert result.loc["unscoreable", "auto_status"] == "RESERVE"
    assert result.loc["unscoreable", "elimination_reason"] == "RANK_NOT_EVALUATED_INSUFFICIENT_DATA"


def test_robust_rank_is_equivalent_with_equity_filter_when_every_fact_passes() -> None:
    robust = {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 2}
    base = [
        _selection_row("growing", strategy_id=1, close_ma_len=3, robust_pnl_30d_pct=Decimal("20"),
                       worst_drawdown_pct=Decimal("2"), worst_holding_p95_minutes=Decimal("20"),
                       ab_stability_ratio=Decimal(".8"), minimum_plateau_point_count=20, first_shift_bp=200,
                       _equity_state="GROWING", _equity_disposition="PASS", _equity_reason="H_UP_SHORTS_NONDECLINING"),
        _selection_row("weakening", strategy_id=2, close_ma_len=5, robust_pnl_30d_pct=Decimal("10"),
                       worst_drawdown_pct=Decimal("4"), worst_holding_p95_minutes=Decimal("40"),
                       ab_stability_ratio=Decimal(".7"), minimum_plateau_point_count=10, first_shift_bp=100,
                       _equity_state="WEAKENING", _equity_disposition="PASS", _equity_reason="SHORT_WINDOW_DECLINE"),
    ]
    without_filter = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [robust]})
    with_filter = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"}, robust,
    ]})

    first = run_selection(pd.DataFrame(base), without_filter).set_index("strategy_name")
    second = run_selection(pd.DataFrame(base), with_filter).set_index("strategy_name")

    assert first["final_rank"].to_dict() == second["final_rank"].to_dict()
    assert first["final_score"].to_dict() == second["final_score"].to_dict()
    assert second.loc["weakening", "auto_status"] == "FINALIST"


def test_equity_quality_rank_rejects_duplicate_strategy_ids() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    rows = [
        _selection_row("a", strategy_id=1, result_id=101, _equity_quality=_equity_rank_facts(101, 0, "1", "0", "0", 28)),
        _selection_row("b", strategy_id=1, result_id=102, _equity_quality=_equity_rank_facts(102, 0, "1", "0", "0", 28)),
    ]

    with pytest.raises(PerformanceV2SelectionError, match="EQUITY_RANK_DUPLICATE_STRATEGY_ID"):
        run_selection(pd.DataFrame(rows), request)


def test_equity_quality_rank_validates_all_candidates_before_prior_filters() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_min_shift", "enabled": True, "scope": "pair_side_timeframe", "min_shift_pct": "1"},
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    rows = [
        _selection_row("filtered-duplicate", strategy_id=1, result_id=101, order_1_shift_bp=1,
                       _equity_quality=_equity_rank_facts(101, 0, "1", "0", "0", 28)),
        _selection_row("survivor-duplicate", strategy_id=1, result_id=102, order_1_shift_bp=100,
                       _equity_quality=_equity_rank_facts(102, 0, "1", "0", "0", 28)),
    ]

    with pytest.raises(PerformanceV2SelectionError, match="EQUITY_RANK_DUPLICATE_STRATEGY_ID"):
        run_selection(pd.DataFrame(rows), request)


def test_equity_rank_review_metadata_survives_panel_candidate_preparation(tmp_path: Path) -> None:
    from mrs3.performance_v2_selection_review import apply_prior_rejected, equity_quality_snapshot_metadata

    connection = _candidate_db(tmp_path)
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    prepare_selection_window_cache(
        tmp_path / "strategy_performance.duckdb", request, SelectionConfig(), workers=1, include_equity=True,
    )
    connection = duckdb.connect(str(tmp_path / "strategy_performance.duckdb"))
    candidates = load_selection_candidates(connection, request, SelectionConfig(), cache_only=True)
    selected = run_selection(apply_prior_rejected(connection, candidates), request)

    assert "_equity_quality" not in selected.columns
    assert {"equity_state", "equity_basis", "equity_dd_pct", "equity_smoothness"}.issubset(selected.columns)
    cached_facts = candidates.iloc[0]["_equity_cache"]["facts"]
    assert selected.iloc[0]["equity_state"] == cached_facts.state
    assert selected.iloc[0]["equity_basis"] == "28d / OK"
    assert selected.iloc[0]["equity_dd_pct"] == cached_facts.drawdown * 100
    assert selected.iloc[0]["equity_smoothness"] == cached_facts.windows[-1].er
    assert set(selected.attrs["equity_quality_facts"]) == {str(int(candidates.iloc[0]["strategy_id"]))}
    snapshot = equity_quality_snapshot_metadata(request, SelectionConfig(), selected)
    assert snapshot is not None
    assert snapshot["sources"][str(int(candidates.iloc[0]["strategy_id"]))]["result_id"] == int(candidates.iloc[0]["result_id"])


def _equity_review_roundtrip(connection, request, config, workbook_path: Path):
    from mrs3.performance_v2_selection_review import (
        apply_prior_rejected, import_selection_review, new_run_metadata, persist_selection_snapshot,
    )

    candidates = load_selection_candidates(connection, request, config, cache_only=True)
    result = run_selection(apply_prior_rejected(connection, candidates), request, config)
    review_rows = {
        int(row.strategy_id): {
            "user_status": str(row.auto_status),
            "user_rank": row.final_rank if row.auto_status in {"FINALIST", "RESERVE"} else None,
            "user_analog_of_strategy_id": row.auto_analog_of_strategy_id if row.auto_status == "ANALOG" else None,
            "comment": None,
        }
        for row in result.itertuples()
    }
    metadata = new_run_metadata(connection, request)
    workbook = write_selection_workbook(result, workbook_path, request, metadata, review_rows)
    persist_selection_snapshot(connection, request, config, result, metadata, workbook.read_bytes())
    imported = import_selection_review(connection, workbook.read_bytes())
    return candidates, result, imported


def test_equity_cache_evidence_round_trips_from_candidate_loader(tmp_path: Path) -> None:
    from mrs3.performance_v2_selection_review import canonical_json

    database = tmp_path / "strategy_performance.duckdb"
    connection = _candidate_db(tmp_path)
    strategy_id, result_id = connection.execute(
        "select strategy_id, current_result_id from strategies"
    ).fetchone()
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    config = SelectionConfig(lot_variant_redundancy_enabled=False)
    prepare_selection_window_cache(database, request, config, workers=1, include_equity=True)

    connection = duckdb.connect(str(database))
    candidates, result, imported = _equity_review_roundtrip(
        connection, request, config, tmp_path / "equity-cache-review.xlsx",
    )
    cached = connection.execute(
        "select source_revision, facts_json, facts_sha256 from equity_quality_metrics where result_id = ?",
        [result_id],
    ).fetchone()
    current_revision = equity_source_revision(current_equity_source_metadata(connection, result_id))
    source = result.attrs["equity_quality_facts"][str(strategy_id)]

    assert len(candidates) == 1
    assert imported["row_count"] == 1
    assert source["facts_sha256"] == cached[2]
    assert source["source_revision"] == cached[0] == current_revision
    assert cached[1] == canonical_json(source["facts"])
    assert hashlib.sha256(canonical_json(source["facts"]).encode()).hexdigest() == cached[2]


def test_equity_quality_retest_cohort_persists_and_imports_review(tmp_path: Path) -> None:
    database = tmp_path / "strategy_performance.duckdb"
    connection = _candidate_db(tmp_path)
    first_strategy_id, first_result_id = connection.execute(
        "select strategy_id, current_result_id from strategies order by strategy_id"
    ).fetchone()
    second_strategy_id, second_result_id = _clone_current_candidate(connection, "beta")
    base_request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 2,
         "method": "equity_quality_v1"},
    ]})
    request = retest_cohort_request(base_request, "review-cohort", {
        int(first_strategy_id): int(first_result_id),
        int(second_strategy_id): int(second_result_id),
    })
    config = SelectionConfig(lot_variant_redundancy_enabled=False)
    connection.close()
    prepare_selection_window_cache(database, request, config, workers=1, include_equity=True)

    connection = duckdb.connect(str(database))
    _, _, imported = _equity_review_roundtrip(
        connection, request, config, tmp_path / "equity-retest-review.xlsx",
    )
    request_json = connection.execute(
        "select request_json from selection_runs where selection_run_id = ?", [imported["selection_run_id"]],
    ).fetchone()[0]
    stored_request = json.loads(request_json)

    assert imported["row_count"] == 2
    assert stored_request["ranking_scope"] == "RETEST_COHORT"
    assert stored_request["bulk_retest_job_id"] == "review-cohort"
    assert stored_request["cohort_members"] == [
        [int(first_strategy_id), int(first_result_id)], [int(second_strategy_id), int(second_result_id)],
    ]


def test_equity_rank_disabled_method_does_not_require_facts_or_change_result() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": False, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    candidates = pd.DataFrame([_selection_row("candidate", strategy_id=1)])

    result = run_selection(candidates, request)

    assert result.loc[0, "finalist"]
    assert "final_rank" not in result
    assert not result.loc[0, "eliminated_by_rank_robust_top_n"]
    assert "_equity_quality" not in result


def test_equity_regime_stage_has_fixed_pair_side_scope_and_legacy_absence_is_unchanged() -> None:
    legacy = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    encoded = json.dumps(asdict(legacy), sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(encoded.encode()).hexdigest() == "40080afb14245d0729d4124794ce647e089521725624767bdfb94de87cc6edb4"

    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    assert [(stage.id, stage.scope, stage.enabled) for stage in request.stages] == [
        ("filter_equity_regime", "pair_side", True),
    ]
    with pytest.raises(PerformanceV2SelectionError, match="EQUITY_REGIME_STAGE_SCOPE"):
        parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side_timeframe"},
        ]})


def test_legacy_robust_request_json_stays_byte_identical() -> None:
    from mrs3.performance_v2_selection_review import canonical_contract

    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1},
    ]})
    expected = (
        '{"bulk_retest_job_id":null,"cohort_members":[],"ranking_scope":"ORDINARY",'
        '"side":"LONG","stages":[{"enabled":true,"id":"rank_robust_top_n",'
        '"min_shift_pct":null,"pnl_tolerance_pct":null,"scope":"pair_side","top_n":1}],'
        '"symbol":"BTCUSDT"}'
    )

    assert canonical_contract(request, SelectionConfig())[0] == expected


def test_legacy_filter_and_rank_contract_keeps_pre_m3_json_and_hash() -> None:
    from mrs3.performance_v2_selection_review import canonical_contract

    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_min_shift", "enabled": True, "scope": "pair_side", "min_shift_pct": "2.5"},
        {"id": "pareto_shift_near_tie", "enabled": True, "scope": "pair_side_timeframe", "pnl_tolerance_pct": "3"},
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 5},
    ]})
    expected = (
        '{"bulk_retest_job_id":null,"cohort_members":[],"ranking_scope":"ORDINARY",'
        '"side":"LONG","stages":[{"enabled":true,"id":"filter_min_shift",'
        '"min_shift_pct":"2.5","pnl_tolerance_pct":null,"scope":"pair_side","top_n":null},'
        '{"enabled":true,"id":"pareto_shift_near_tie","min_shift_pct":null,'
        '"pnl_tolerance_pct":"3","scope":"pair_side_timeframe","top_n":null},'
        '{"enabled":true,"id":"rank_robust_top_n","min_shift_pct":null,'
        '"pnl_tolerance_pct":null,"scope":"pair_side","top_n":5}],"symbol":"BTCUSDT"}'
    )

    request_json, request_hash, *_ = canonical_contract(request, SelectionConfig())
    assert request_json == expected
    assert request_hash == "b5fd67f1a42c9f1286df804cb813cbba6b9ceb3acda2b436e41f395fa4a3481e"

    explicit_default = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 5,
         "method": "robust_v1"},
    ]})
    legacy_default = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 5},
    ]})
    assert canonical_contract(explicit_default, SelectionConfig()) == canonical_contract(legacy_default, SelectionConfig())

    active_equity_method = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 5,
         "method": "equity_quality_v1"},
    ]})
    active_json = canonical_contract(active_equity_method, SelectionConfig())[0]
    assert json.loads(active_json)["stages"][0]["method"] == "equity_quality_v1"


def test_disabled_rank_method_variants_share_v1_golden_json_and_hash() -> None:
    from mrs3.performance_v2_selection_review import canonical_contract

    expected_json = (
        '{"bulk_retest_job_id":null,"cohort_members":[],"ranking_scope":"ORDINARY",'
        '"side":"LONG","stages":[{"enabled":true,"id":"filter_min_shift",'
        '"min_shift_pct":"2.5","pnl_tolerance_pct":null,"scope":"pair_side","top_n":null},'
        '{"enabled":false,"id":"rank_robust_top_n","min_shift_pct":null,'
        '"pnl_tolerance_pct":null,"scope":"pair_side","top_n":5}],"symbol":"BTCUSDT"}'
    )
    expected_hash = "ebd8f3b43e3b7194d9edb10e21056f749479b2d1dffbaedff9a76897212ff45e"
    method_variants = (None, "robust_v1", "equity_quality_v1")

    for method in method_variants:
        rank_stage = {"id": "rank_robust_top_n", "enabled": False, "scope": "pair_side", "top_n": 5}
        if method is not None:
            rank_stage["method"] = method
        request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "filter_min_shift", "enabled": True, "scope": "pair_side", "min_shift_pct": "2.5"},
            rank_stage,
        ]})

        request_json, request_hash, *_ = canonical_contract(request, SelectionConfig())
        assert request_json == expected_json
        assert request_hash == expected_hash


@pytest.mark.parametrize(
    ("state", "disposition", "reason", "finalist", "unassessed"),
    [
        ("GROWING", "PASS", "H_UP_SHORTS_NONDECLINING", True, 0),
        ("WEAKENING", "PASS", "SHORT_WINDOW_DECLINE", True, 0),
        ("FLAT", "BLOCK_IF_ERF_ENABLED", "H_FLAT", False, 0),
        ("DECLINING_OR_MIXED", "BLOCK", "H_DECLINING_OR_MIXED", False, 0),
        ("NONPOSITIVE_EQUITY", "BLOCK", "NONPOSITIVE_EQUITY", False, 0),
        ("INSUFFICIENT_HISTORY", "NOT_EVALUATED", "INSUFFICIENT_HISTORY", True, 1),
        ("MISSING_BASELINE", "NOT_EVALUATED", "MISSING_BASELINE", True, 1),
        ("UNKNOWN_INVALID_SOURCE", "NOT_EVALUATED", "UNKNOWN_INVALID_SOURCE", True, 1),
    ],
)
def test_equity_regime_filter_uses_cached_disposition_without_false_pass(
    state: str, disposition: str, reason: str, finalist: bool, unassessed: int,
) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    result = run_selection(pd.DataFrame([_selection_row(
        "candidate", _equity_state=state, _equity_disposition=disposition,
        _equity_reason=reason,
    )]), request)

    assert bool(result.loc[0, "finalist"]) is finalist
    assert result.loc[0, "equity_regime_state"] == state
    assert result.loc[0, "equity_regime_reason"] == reason
    assert result.attrs["stage_counts"]["filter_equity_regime"]["not_evaluated"] == unassessed
    if not finalist:
        assert result.loc[0, "elimination_reason"] == "FILTER_EQUITY_REGIME"
    if unassessed:
        assert result.loc[0, "elimination_reason"] == reason


def test_run_selection_maps_fresh_equity_facts_after_duplicate_input_index() -> None:
    from mrs3.performance_v2_selection import SelectionConfig

    def quality(result_id: int, state: str, drawdown: str, smoothness: str, horizon: int) -> dict[str, object]:
        evidence = _equity_rank_facts(
            result_id, 0, "1.23456789", drawdown, "0.1", horizon, state=state,
        )
        facts = evidence["facts"]
        windows = tuple(
            replace(window, er=Decimal(smoothness)) if window.days == horizon else window
            for window in facts.windows
        )
        facts = replace(facts, windows=windows)
        evidence["facts"] = facts
        evidence["facts_sha256"] = hashlib.sha256(json.dumps(
            facts.to_canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        return evidence

    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    rows = pd.DataFrame([
        _selection_row(
            "first", strategy_id=1, result_id=101,
            _equity_quality=quality(101, "GROWING", "0.123456789", "0.23456789", 28),
        ),
        _selection_row(
            "second", strategy_id=2, result_id=102,
            _equity_quality=quality(102, "WEAKENING", "0.23456789", "-0.3456789", 14),
        ),
    ], index=[7, 7])

    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False)).set_index(
        "strategy_name"
    )

    assert result.loc["first", ["equity_state", "equity_basis", "equity_dd_pct", "equity_smoothness"]].tolist() == [
        "GROWING", "28d / OK", Decimal("12.345678900"), Decimal("0.23456789"),
    ]
    assert result.loc["second", ["equity_state", "equity_basis", "equity_dd_pct", "equity_smoothness"]].tolist() == [
        "WEAKENING", "14d / PARTIAL", Decimal("23.456789000"), Decimal("-0.3456789"),
    ]


@pytest.mark.parametrize("missing_column", ["_equity_state", "_equity_disposition", "_equity_reason"])
def test_equity_regime_rejects_missing_cached_fact_columns(missing_column: str) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    row = _selection_row(
        "candidate", _equity_state="UNKNOWN_INVALID_SOURCE",
        _equity_disposition="NOT_EVALUATED", _equity_reason="UNKNOWN_INVALID_SOURCE",
    )
    frame = pd.DataFrame([row]).drop(columns=[missing_column, "_equity_cache"])

    with pytest.raises(PerformanceV2SelectionError, match="EQUITY_CACHE_INCOMPLETE"):
        run_selection(frame, request)


def test_enabled_equity_consumer_rejects_legacy_projection_without_cache_entry() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    row = _selection_row(
        "candidate", result_id=101, _equity_state="GROWING", _equity_disposition="PASS",
        _equity_reason="H_UP_SHORTS_NONDECLINING",
    )
    row.pop("_equity_cache", None)

    with pytest.raises(PerformanceV2SelectionError, match="EQUITY_CACHE_INCOMPLETE"):
        run_selection(pd.DataFrame([row]), request)


@pytest.mark.parametrize("disposition", [None, "UNKNOWN", "PASS "])
def test_equity_regime_rejects_missing_or_invalid_cached_disposition(disposition: object) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    frame = pd.DataFrame([_selection_row(
        "candidate", _equity_state="UNKNOWN_INVALID_SOURCE", _equity_disposition=disposition,
        _equity_reason="UNKNOWN_INVALID_SOURCE",
    )])

    with pytest.raises(PerformanceV2SelectionError, match="EQUITY_CACHE_INCOMPLETE"):
        run_selection(frame, request)


def test_equity_regime_runs_after_lot_and_before_submitted_filters_and_rank() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_primary", "enabled": True, "scope": "pair_side"},
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
        {"id": "filter_lot_variant_redundancy", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 10},
    ]})
    result = run_selection(pd.DataFrame([_lot_variant_row(
        "candidate", 1, _equity_state="GROWING", _equity_disposition="PASS",
        _equity_reason="H_UP_SHORTS_NONDECLINING",
    )]), request)

    assert list(result.attrs["stage_counts"]) == [
        "filter_lot_variant_redundancy", "filter_equity_regime", "pareto_primary", "rank_robust_top_n",
    ]


def test_equity_regime_preserves_mixed_lot_group_until_blocked_variant_is_removed() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    result = run_selection(pd.DataFrame([
        _lot_variant_row("old-lot-winner", 1, lots=("1", "2"), dd5_proxy=Decimal("11"),
                         _equity_state="FLAT", _equity_disposition="BLOCK_IF_ERF_ENABLED",
                         _equity_reason="H_FLAT"),
        _lot_variant_row("passing-sibling", 2, lots=("3", "4"), dd5_proxy=Decimal("10"),
                         _equity_state="GROWING", _equity_disposition="PASS",
                         _equity_reason="H_UP_SHORTS_NONDECLINING"),
    ]), request).set_index("strategy_name")

    assert not result.loc["old-lot-winner", "finalist"]
    assert result.loc["passing-sibling", "finalist"]
    assert result.loc["passing-sibling", "elimination_reason"] == "LOT_GROUP_EQUITY_BLOCKED"
    assert not result["eliminated_by_filter_lot_variant_redundancy"].any()
    assert result.loc["old-lot-winner", "elimination_reason"] == "FILTER_EQUITY_REGIME"


def test_equity_regime_skips_whole_three_member_lot_group_if_any_member_is_blocked() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    result = run_selection(pd.DataFrame([
        _lot_variant_row("pass-a", 1, lots=("1", "2"), dd5_proxy=Decimal("11"),
                         _equity_state="GROWING", _equity_disposition="PASS",
                         _equity_reason="H_UP_SHORTS_NONDECLINING"),
        _lot_variant_row("pass-b", 2, lots=("3", "4"), dd5_proxy=Decimal("10"),
                         _equity_state="WEAKENING", _equity_disposition="PASS",
                         _equity_reason="H_UP_SHORTS_NONDECLINING"),
        _lot_variant_row("blocked-c", 3, lots=("5", "6"), dd5_proxy=Decimal("9"),
                         _equity_state="FLAT", _equity_disposition="BLOCK_IF_ERF_ENABLED",
                         _equity_reason="H_FLAT"),
    ]), request).set_index("strategy_name")

    assert result.loc["pass-a", "finalist"]
    assert result.loc["pass-b", "finalist"]
    assert not result.loc["blocked-c", "finalist"]
    assert result.loc["pass-a", "elimination_reason"] == "LOT_GROUP_EQUITY_BLOCKED"
    assert result.loc["pass-b", "elimination_reason"] == "LOT_GROUP_EQUITY_BLOCKED"
    assert result.loc["blocked-c", "elimination_reason"] == "FILTER_EQUITY_REGIME"
    assert not result["eliminated_by_filter_lot_variant_redundancy"].any()
    assert result["eliminated_by_filter_equity_regime"].to_dict() == {
        "pass-a": False, "pass-b": False, "blocked-c": True,
    }


def test_equity_regime_all_pass_lot_group_keeps_existing_winner_and_precedence_for_unassessed() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    all_pass = run_selection(pd.DataFrame([
        _lot_variant_row("a", 1, lots=("1", "2"), dd5_proxy=Decimal("11"),
                         _equity_state="GROWING", _equity_disposition="PASS",
                         _equity_reason="H_UP_SHORTS_NONDECLINING"),
        _lot_variant_row("b", 2, lots=("3", "4"), dd5_proxy=Decimal("10"),
                         _equity_state="WEAKENING", _equity_disposition="PASS",
                         _equity_reason="SHORT_WINDOW_DECLINE"),
    ]), request).set_index("strategy_name")
    assert all_pass.loc["a", "finalist"] and not all_pass.loc["b", "finalist"]
    assert all_pass.loc["b", "eliminated_by_filter_lot_variant_redundancy"]
    assert not all_pass.loc["b", "eliminated_by_filter_equity_regime"]

    unknown = run_selection(pd.DataFrame([
        _lot_variant_row("blocked", 1, lots=("1", "2"), dd5_proxy=Decimal("11"),
                         _equity_state="FLAT", _equity_disposition="BLOCK_IF_ERF_ENABLED",
                         _equity_reason="H_FLAT"),
        _lot_variant_row("unassessed", 2, lots=("3", "4"), dd5_proxy=Decimal("10"),
                         _equity_state="MISSING_BASELINE", _equity_disposition="NOT_EVALUATED",
                         _equity_reason="MISSING_BASELINE"),
    ]), request).set_index("strategy_name")
    assert not unknown.loc["blocked", "finalist"] and unknown.loc["unassessed", "finalist"]
    assert unknown.loc["unassessed", "elimination_reason"] == "LOT_GROUP_EQUITY_UNASSESSED"
    assert not unknown["eliminated_by_filter_lot_variant_redundancy"].any()


def test_lot_unassessed_advisory_is_replaced_when_later_pareto_eliminates_survivor() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
        {"id": "pareto_primary", "enabled": True, "scope": "pair_side"},
    ]})
    result = run_selection(pd.DataFrame([
        _lot_variant_row("unassessed-winner", 1, lots=("1", "2"), dd5_proxy=Decimal("10"),
                         capital_proxy=Decimal("5"), _equity_state="MISSING_BASELINE",
                         _equity_disposition="NOT_EVALUATED", _equity_reason="MISSING_BASELINE"),
        _lot_variant_row("unassessed-loser", 2, lots=("3", "4"), dd5_proxy=Decimal("5"),
                         capital_proxy=Decimal("10"), _equity_state="MISSING_BASELINE",
                         _equity_disposition="NOT_EVALUATED", _equity_reason="MISSING_BASELINE"),
    ]), request).set_index("strategy_name")

    assert result.loc["unassessed-winner", "finalist"]
    assert result.loc["unassessed-winner", "elimination_reason"] == "LOT_GROUP_EQUITY_UNASSESSED"
    assert not result.loc["unassessed-loser", "finalist"]
    assert result.loc["unassessed-loser", "elimination_reason"] == "PARETO_PRIMARY"


def test_equity_regime_off_keeps_legacy_selection_frame_without_equity_columns() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    result = run_selection(pd.DataFrame([_selection_row("candidate")]), request)

    assert "eliminated_by_filter_equity_regime" not in result
    assert "equity_regime_state" not in result
    assert "equity_regime_reason" not in result


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"symbol": "BTCUSDT", "side": "LONG", "stages": [{"id": "unknown", "enabled": True, "scope": "pair_side"}]}, "UNKNOWN_STAGE"),
        ({"symbol": "BTCUSDT", "side": "LONG", "stages": [{"id": "ab_deterioration", "enabled": True, "scope": "pair_side"}, {"id": "ab_deterioration", "enabled": False, "scope": "pair_side"}]}, "DUPLICATE_STAGE"),
        ({"symbol": "BTCUSDT", "side": "LONG", "stages": [{"id": "ab_deterioration", "enabled": True, "scope": "global"}]}, "INVALID_SCOPE"),
        ({"symbol": "BTCUSDT", "side": "LONG", "stages": [{"id": "filter_lot_variant_redundancy", "enabled": True, "scope": "pair_side"}]}, "LOT_VARIANT_STAGE_SCOPE"),
    ],
)
def test_parse_selection_request_rejects_unknown_duplicate_and_invalid_scope(payload: dict[str, object], code: str) -> None:
    with pytest.raises(PerformanceV2SelectionError, match=code):
        parse_selection_request(payload)


def test_min_shift_filter_excludes_any_existing_order_below_threshold() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_min_shift", "enabled": True, "scope": "pair_side", "min_shift_pct": "0.3"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("kept", strategy_id=1, order_count=2, order_1_shift_bp=30, order_2_shift_bp=40),
        _selection_row("excluded", strategy_id=2, order_count=2, order_1_shift_bp=40, order_2_shift_bp=20),
        _selection_row("missing", strategy_id=3, order_count=2, order_1_shift_bp=40),
    ]), request).set_index("strategy_name")

    assert not result.loc["kept", "eliminated_by_filter_min_shift"]
    assert result.loc["excluded", "eliminated_by_filter_min_shift"]
    assert not result.loc["missing", "eliminated_by_filter_min_shift"]


def test_window_b_pareto_eliminates_only_candidate_dominated_on_all_b_metrics() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_window_b", "enabled": True, "scope": "pair_side_timeframe"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("winner", ab_return_b_30d_pct=Decimal("20"), ab_trade_rate_b_30d=Decimal("30"), ab_drawdown_b_pct=Decimal("4"), ab_holding_p95_minutes=Decimal("60")),
        _selection_row("loser", ab_return_b_30d_pct=Decimal("10"), ab_trade_rate_b_30d=Decimal("20"), ab_drawdown_b_pct=Decimal("5"), ab_holding_p95_minutes=Decimal("90")),
        _selection_row("missing", ab_return_b_30d_pct=Decimal("5"), ab_trade_rate_b_30d=Decimal("10"), ab_drawdown_b_pct=Decimal("6")),
    ]), request).set_index("strategy_name")

    assert not result.loc["winner", "eliminated_by_pareto_window_b"]
    assert result.loc["loser", "eliminated_by_pareto_window_b"]
    assert not result.loc["missing", "eliminated_by_pareto_window_b"]


def test_window_b_dd_shift_pareto_eliminates_only_fully_dominated_candidate() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_window_b_dd_shift", "enabled": True, "scope": "pair_side_timeframe"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("winner", ab_return_b_30d_pct=Decimal("20"), max_drawdown_pct=Decimal("4"), first_shift_bp=200),
        _selection_row("loser", ab_return_b_30d_pct=Decimal("10"), max_drawdown_pct=Decimal("5"), first_shift_bp=100),
        _selection_row("tradeoff", ab_return_b_30d_pct=Decimal("25"), max_drawdown_pct=Decimal("5"), first_shift_bp=100),
    ]), request).set_index("strategy_name")

    assert not result.loc["winner", "eliminated_by_pareto_window_b_dd_shift"]
    assert result.loc["loser", "eliminated_by_pareto_window_b_dd_shift"]
    assert not result.loc["tradeoff", "eliminated_by_pareto_window_b_dd_shift"]


def test_selection_config_reads_agreed_defaults(tmp_path: Path) -> None:
    config = load_selection_config(_config(tmp_path / "config.performance.json"))

    assert config.ab_final_days == 14
    assert config.ab_return_floor_pct == 5
    assert config.ab_return_divisor == 10
    assert config.ab_win_rate_floor_pct == 58
    assert config.ab_trade_rate_divisor == 7
    assert config.plateau_points_pareto_pnl_multiplier == 2
    assert config.best_trade_max_profit_share_pct == 35
    assert config.best_trade_min_profitable_trades == 4
    assert config.shift_near_tie_min_advantage_bp == 10
    assert config.lot_variant_redundancy_enabled is True


def test_selection_config_reads_explicit_overrides(tmp_path: Path) -> None:
    config = load_selection_config(_config(
        tmp_path / "config.performance.json",
        ab_final_days=21,
        ab_return_floor_pct=4.5,
        ab_return_divisor=8,
        ab_win_rate_floor_pct=60,
        ab_trade_rate_divisor=6,
        plateau_points_pareto_pnl_multiplier=1.5,
        best_trade_max_profit_share_pct=40,
        best_trade_min_profitable_trades=5,
        shift_near_tie_min_advantage_bp=15,
        lot_variant_redundancy_enabled=False,
    ))

    assert config.ab_final_days == 21
    assert config.ab_return_floor_pct == Decimal("4.5")
    assert config.plateau_points_pareto_pnl_multiplier == Decimal("1.5")
    assert config.best_trade_max_profit_share_pct == 40
    assert config.best_trade_min_profitable_trades == 5
    assert config.shift_near_tie_min_advantage_bp == 15
    assert config.lot_variant_redundancy_enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ab_final_days", 0),
        ("ab_final_days", True),
        ("ab_return_floor_pct", 0),
        ("ab_return_divisor", "wrong"),
        ("ab_win_rate_floor_pct", float("nan")),
        ("ab_trade_rate_divisor", float("inf")),
        ("plateau_points_pareto_pnl_multiplier", False),
        ("best_trade_max_profit_share_pct", 100),
        ("best_trade_min_profitable_trades", 0),
        ("shift_near_tie_min_advantage_bp", 0),
        ("lot_variant_redundancy_enabled", "yes"),
    ],
)
def test_selection_config_rejects_invalid_values(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(PerformanceV2SelectionError, match=f"INVALID_CONFIG_{field}"):
        load_selection_config(_config(tmp_path / "config.performance.json", **{field: value}))


def test_selection_config_rejects_malformed_v2_namespace(tmp_path: Path) -> None:
    path = tmp_path / "config.performance.json"
    path.write_text(json.dumps({"unified_performance_v2": []}), encoding="utf-8")

    with pytest.raises(PerformanceV2SelectionError, match="INVALID_CONFIG"):
        load_selection_config(path)


def test_real_selection_config_has_agreed_defaults() -> None:
    config = load_selection_config(Path(__file__).resolve().parents[1] / "config.performance.json")

    assert config == SelectionConfig()


def _candidate_db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(str(tmp_path / "strategy_performance.duckdb"))
    initialize_performance_v2(connection)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_id = connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
           order_count, analysis_run_id, candidate_identity, lifecycle_status,
           created_at_utc, updated_at_utc) values ('alpha', 'BTCUSDT', 'LONG', '1h',
           3, 1, 'run', 'candidate', 'ACTIVE', ?, ?) returning strategy_id""",
        [start, start],
    ).fetchone()[0]
    result_id = connection.execute(
        """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
           commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
           max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
           values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 2, 2, ?) returning result_id""",
        [strategy_id, start, datetime(2026, 1, 31, tzinfo=UTC), start],
    ).fetchone()[0]
    connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
    connection.execute("insert into analysis_plateaus values ('run', 'P1', 12, 34)")
    connection.execute(
        """insert into strategy_orders values (?, 1, 7, .995, 125, 1, 'run', 'P1', 8)""",
        [strategy_id],
    )
    connection.executemany(
        "insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size, post_size, post_side, pnl, fee, balance, raw_action_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (result_id, 0, start, "BTCUSDT", 1, "closed", 1, 0, "", 0, 0, 100, None),
            (result_id, 1, datetime(2026, 1, 2, tzinfo=UTC), "BTCUSDT", 1, "opened", 1, 1, "long", 0, 0, 100, None),
            (result_id, 2, datetime(2026, 1, 3, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", 10, 2, 110, None),
        ],
    )
    connection.executemany(
        "insert into strategy_equity values (?, ?, ?, ?, ?)",
        [
            (result_id, 0, start, 100, 100),
            (result_id, 1, datetime(2026, 1, 2, tzinfo=UTC), 100, 100),
            (result_id, 2, datetime(2026, 1, 3, tzinfo=UTC), 110, 110),
            (result_id, 3, datetime(2026, 1, 31, tzinfo=UTC), 110, 110),
        ],
    )
    return connection


def _clone_current_candidate(connection: duckdb.DuckDBPyConnection, name: str) -> tuple[int, int]:
    source_strategy_id, source_result_id = connection.execute(
        "select strategy_id, result_id from strategy_results order by result_id limit 1"
    ).fetchone()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_id = int(connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
               order_count, analysis_run_id, candidate_identity, lifecycle_status,
               created_at_utc, updated_at_utc) values (?, 'BTCUSDT', 'LONG', '1h',
               3, 1, 'run', ?, 'ACTIVE', ?, ?) returning strategy_id""",
        [name, name, now, now],
    ).fetchone()[0])
    result_columns = [row[0] for row in connection.execute(
        "select column_name from information_schema.columns where table_name = 'strategy_results' order by ordinal_position"
    ).fetchall()]
    source_result = connection.execute(
        "select * from strategy_results where result_id = ?", [source_result_id]
    ).fetchone()
    result_values = [
        strategy_id if column == "strategy_id" else value
        for column, value in zip(result_columns, source_result)
        if column != "result_id"
    ]
    result_insert_columns = [column for column in result_columns if column != "result_id"]
    result_id = int(connection.execute(
        f"insert into strategy_results ({', '.join(result_insert_columns)}) values ({', '.join('?' for _ in result_values)}) returning result_id",
        result_values,
    ).fetchone()[0])
    connection.execute(
        "update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id]
    )
    for table in ("strategy_actions", "strategy_equity"):
        columns = [row[0] for row in connection.execute(
            "select column_name from information_schema.columns where table_name = ? order by ordinal_position", [table]
        ).fetchall()]
        rows = connection.execute(f"select * from {table} where result_id = ?", [source_result_id]).fetchall()
        connection.executemany(
            f"insert into {table} ({', '.join(columns)}) values ({', '.join('?' for _ in columns)})",
            [[result_id if column == "result_id" else value for column, value in zip(columns, row)] for row in rows],
        )
    return strategy_id, result_id


def test_equity_warmup_shares_the_cold_selection_source_load(tmp_path: Path, monkeypatch) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id, strategy_id = connection.execute(
        "select result_id, strategy_id from strategy_results"
    ).fetchone()
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    calls = 0
    original = selection_module._load_source

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(selection_module, "_load_source", counted)

    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)

    assert calls == 1
    with duckdb.connect(str(database), read_only=True) as check:
        assert check.execute("select count(*) from window_metrics").fetchone() == (7,)
        assert check.execute("select count(*) from equity_quality_metrics").fetchone() == (1,)
        assert selection_cache_status(check, request, SelectionConfig()) == {
            "total": 1, "missing": 0, "ready": True,
        }
        assert selection_cache_status(check, request, SelectionConfig(), include_equity=True)["ready"]
        metadata = current_equity_source_metadata(check, result_id)
        facts = read_equity_quality_facts(
            check, result_id, equity_source_revision(metadata)
        )
        assert facts is not None
        assert facts.raw_sample_count == 4


def test_enabled_equity_selection_batch_reads_only_fresh_facts_for_exact_retest_cohort(
    tmp_path: Path, monkeypatch,
) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    first_id = int(connection.execute("select strategy_id from strategies").fetchone()[0])
    first_result = int(connection.execute("select current_result_id from strategies where strategy_id = ?", [first_id]).fetchone()[0])
    second_id, second_result = _clone_current_candidate(connection, "beta")
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)
    with duckdb.connect(str(database)) as writer:
        writer.execute("update equity_quality_metrics set source_revision = 'stale' where result_id = ?", [first_result])
    with duckdb.connect(str(database), read_only=True) as check:
        before = check.execute("select result_id, facts_json, facts_sha256 from equity_quality_metrics order by result_id").fetchall()
        scoped = retest_cohort_request(request, "one-result", {second_id: second_result})
        equity_queries: list[tuple[str, object]] = []

        class CountingConnection:
            def execute(self, sql: str, parameters: object = None):
                if sql.lower().lstrip().startswith("select result_id, source_revision, algo_version, facts_json, facts_sha256"):
                    equity_queries.append((sql, parameters))
                return check.execute(sql) if parameters is None else check.execute(sql, parameters)

        monkeypatch.setattr(selection_module, "_load_source", lambda *args: (_ for _ in ()).throw(AssertionError("raw source read")))
        monkeypatch.setattr(selection_module, "_load_equity_samples_for_quality", lambda *args: (_ for _ in ()).throw(AssertionError("raw equity read")))
        monkeypatch.setattr(selection_module, "_persist", lambda *args: (_ for _ in ()).throw(AssertionError("cache write")))
        candidates = load_selection_candidates(CountingConnection(), scoped, SelectionConfig(), cache_only=True)
        assert candidates["strategy_id"].tolist() == [second_id]
        assert candidates.loc[0, "_equity_cache"]["status"] == "FRESH"
        assert candidates.loc[0, "_equity_cache"]["facts"].state == "FLAT"
        assert candidates.loc[0, "_equity_cache"]["facts"].erf_disposition == "BLOCK_IF_ERF_ENABLED"
        assert len(equity_queries) == 1
        assert "result_id in (?)" in equity_queries[0][0].lower()
        assert equity_queries[0][1][-1] == second_result

        equity_queries.clear()
        with pytest.raises(PerformanceV2SelectionError, match="EQUITY_CACHE_INCOMPLETE"):
            load_selection_candidates(CountingConnection(), request, SelectionConfig(), cache_only=True)
        assert len(equity_queries) == 1
        assert "result_id in (?,?)" in equity_queries[0][0].lower()
        after = check.execute("select result_id, facts_json, facts_sha256 from equity_quality_metrics order by result_id").fetchall()

    assert before == after


def test_equity_rank_only_reads_verified_cached_facts_in_one_batch(tmp_path: Path, monkeypatch) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)
    with duckdb.connect(str(database), read_only=True) as check:
        queries: list[str] = []

        class CountingConnection:
            def execute(self, sql: str, parameters: object = None):
                if "from equity_quality_metrics" in sql.lower():
                    queries.append(sql)
                return check.execute(sql) if parameters is None else check.execute(sql, parameters)

        monkeypatch.setattr(selection_module, "_load_source", lambda *args: (_ for _ in ()).throw(AssertionError("raw source read")))
        monkeypatch.setattr(selection_module, "_load_equity_samples_for_quality", lambda *args: (_ for _ in ()).throw(AssertionError("raw equity read")))
        candidates = load_selection_candidates(CountingConnection(), request, SelectionConfig(), cache_only=True)

    assert len(queries) == 1
    cached = candidates.loc[0, "_equity_cache"]
    assert cached["status"] == "FRESH"
    assert cached["facts"].state == "FLAT"
    assert cached["facts"].equity_class == 2
    assert cached["facts"].score12 == Decimal("0E-12")
    assert cached["facts"].horizon_days == 28
    assert len(cached["source_revision"]) == 64
    assert len(cached["facts_sha256"]) == 64


def test_equity_rank_allows_mixed_report_ends_in_exact_retest_cohort(tmp_path: Path, monkeypatch) -> None:
    connection = _candidate_db(tmp_path)
    first_strategy, first_result = connection.execute(
        "select strategy_id, current_result_id from strategies"
    ).fetchone()
    second_strategy, second_result = _clone_current_candidate(connection, "beta")
    second_end = datetime(2026, 2, 1, tzinfo=UTC)
    connection.execute("update strategy_results set report_end_utc = ? where result_id = ?", [second_end, second_result])
    connection.close()
    base = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 2,
         "method": "equity_quality_v1"},
    ]})
    request = retest_cohort_request(base, "mixed-t", {
        int(first_strategy): int(first_result), int(second_strategy): int(second_result),
    })
    database = tmp_path / "strategy_performance.duckdb"
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)
    with duckdb.connect(str(database), read_only=True) as check:
        fact_queries: list[str] = []

        class CountingConnection:
            def execute(self, sql: str, parameters: object = None):
                if "from equity_quality_metrics" in sql.lower():
                    fact_queries.append(sql)
                return check.execute(sql) if parameters is None else check.execute(sql, parameters)

        monkeypatch.setattr(selection_module, "_load_source", lambda *args: (_ for _ in ()).throw(AssertionError("raw source read")))
        monkeypatch.setattr(selection_module, "_load_equity_samples_for_quality", lambda *args: (_ for _ in ()).throw(AssertionError("raw equity read")))
        candidates = load_selection_candidates(CountingConnection(), request, SelectionConfig(), cache_only=True)

    assert len(candidates) == 2
    assert len(fact_queries) == 1
    assert {row["report_end_utc"] for _, row in candidates.iterrows()} == {
        datetime(2026, 1, 31, tzinfo=UTC), second_end,
    }
    assert {
        row["_equity_cache"]["facts"].report_end_utc for _, row in candidates.iterrows()
    } == {datetime(2026, 1, 31, tzinfo=UTC), second_end}
    assert f"result_id in (?,?)" in fact_queries[0].lower()


def test_equity_rank_rejects_malformed_facts_on_previously_eliminated_candidate() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_min_shift", "enabled": True, "scope": "pair_side_timeframe", "min_shift_pct": "1"},
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    rows = [
        _selection_row("filtered-bad-facts", strategy_id=1, result_id=101, order_1_shift_bp=1,
                       _equity_quality={**_equity_rank_facts(101, 0, "1", "0", "0", 28), "source_revision": None}),
        _selection_row("rankable", strategy_id=2, result_id=102, order_1_shift_bp=100,
                       _equity_quality=_equity_rank_facts(102, 0, "1", "0", "0", 28)),
    ]

    with pytest.raises(PerformanceV2SelectionError, match="EQUITY_CACHE_INCOMPLETE"):
        run_selection(pd.DataFrame(rows), request)


def test_equity_rank_id_lookup_avoids_iterrows_numeric_upcast() -> None:
    numeric_rows = pd.DataFrame({"strategy_id": [7], "result_id": [17], "metric": [1.25]})

    iterated_id = next(numeric_rows.iterrows())[1]["strategy_id"]

    assert not isinstance(iterated_id, int)
    assert selection_module._equity_rank_strategy_id(numeric_rows, 0) == 7


@pytest.mark.parametrize("corruption", ["missing", "stale", "wrong-algorithm", "bad-digest"])
def test_enabled_equity_selection_rejects_unverified_cache_facts(
    tmp_path: Path, monkeypatch, corruption: str,
) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)
    with duckdb.connect(str(database)) as writer:
        if corruption == "missing":
            writer.execute("delete from equity_quality_metrics where result_id = ?", [result_id])
        elif corruption == "stale":
            writer.execute("update equity_quality_metrics set source_revision = 'stale' where result_id = ?", [result_id])
        elif corruption == "wrong-algorithm":
            writer.execute("update equity_quality_metrics set algo_version = 'old' where result_id = ?", [result_id])
        else:
            writer.execute("update equity_quality_metrics set facts_sha256 = ? where result_id = ?", ["0" * 64, result_id])

    monkeypatch.setattr(selection_module, "_load_source", lambda *args: (_ for _ in ()).throw(AssertionError("raw source read")))
    monkeypatch.setattr(selection_module, "_load_equity_samples_for_quality", lambda *args: (_ for _ in ()).throw(AssertionError("raw equity read")))
    monkeypatch.setattr(selection_module, "_persist", lambda *args: (_ for _ in ()).throw(AssertionError("cache write")))
    with duckdb.connect(str(database), read_only=True) as check:
        with pytest.raises(PerformanceV2SelectionError, match="EQUITY_CACHE_INCOMPLETE"):
            load_selection_candidates(check, request, SelectionConfig(), cache_only=True)


def test_equity_fact_batch_does_not_mask_malformed_candidate_source_fields(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)

    with duckdb.connect(str(database), read_only=True) as check:
        with pytest.raises(IndexError):
            selection_module._selection_equity_facts_by_result(check, ((1, result_id),))
        malformed_row = (1, result_id, None, None, "not-a-datetime", None, None, None)
        with pytest.raises(AttributeError):
            selection_module._selection_equity_facts_by_result(check, (malformed_row,))


def test_equity_selection_accepts_cached_no_sample_not_evaluated_fact(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    connection.execute("delete from strategy_equity")
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)

    with duckdb.connect(str(database), read_only=True) as check:
        assert selection_cache_status(check, request, SelectionConfig(), include_equity=True) == {
            "total": 1, "missing": 0, "ready": True,
        }
        candidates = load_selection_candidates(check, request, SelectionConfig(), cache_only=True)
    result = run_selection(candidates, request)

    assert result.loc[0, "finalist"]
    assert result.loc[0, "equity_regime_state"] == "MISSING_BASELINE"
    assert result.loc[0, "equity_regime_disposition"] == "NOT_EVALUATED"
    assert result.loc[0, "equity_regime_reason"] == "MISSING_BASELINE"
    assert result.attrs["stage_counts"]["filter_equity_regime"]["not_evaluated"] == 1


def test_equity_selection_reads_cached_growing_pass_fact(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id, report_end = connection.execute(
        "select result_id, report_end_utc from strategy_results"
    ).fetchone()
    connection.execute("delete from strategy_equity where result_id = ?", [result_id])
    first = report_end - timedelta(days=28)
    connection.executemany(
        "insert into strategy_equity values (?, ?, ?, ?, ?)",
        [
            (result_id, day, first + timedelta(days=day), Decimal(100 + day), Decimal(100 + day))
            for day in range(29)
        ],
    )
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)

    with duckdb.connect(str(database), read_only=True) as check:
        candidates = load_selection_candidates(check, request, SelectionConfig(), cache_only=True)
    result = run_selection(candidates, request)

    assert candidates.loc[0, "_equity_cache"]["status"] == "FRESH"
    assert candidates.loc[0, "_equity_cache"]["facts"].state == "GROWING"
    assert candidates.loc[0, "_equity_cache"]["facts"].erf_disposition == "PASS"
    assert result.loc[0, "equity_regime_state"] == "GROWING"
    assert result.loc[0, "equity_regime_disposition"] == "PASS"
    assert result.loc[0, "equity_regime_reason"] == "H_UP_SHORTS_NONDECLINING"
    assert result.loc[0, "finalist"]


def test_equity_selection_reads_verified_facts_when_optional_optimizer_metadata_is_unparseable(
    tmp_path: Path,
) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.execute(
        "update strategy_results set optimizer_source_metadata_json = ? where result_id = ?",
        ["not-json", result_id],
    )
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)

    with duckdb.connect(str(database), read_only=True) as check:
        metadata = current_equity_source_metadata(check, result_id)
        revision = equity_source_revision(metadata)
        assert revision == equity_source_revision({**metadata, "optimizer_source_metadata_json": None})
        facts = read_equity_quality_facts(check, result_id, revision)
        assert facts is not None
        candidates = load_selection_candidates(check, request, SelectionConfig(), cache_only=True)
    result = run_selection(candidates, request)

    assert facts.state == "FLAT"
    assert facts.erf_disposition == "BLOCK_IF_ERF_ENABLED"
    assert candidates.loc[0, "_equity_cache"]["status"] == "FRESH"
    assert candidates.loc[0, "_equity_cache"]["facts"].state == facts.state
    assert candidates.loc[0, "_equity_cache"]["facts"].erf_disposition == facts.erf_disposition
    assert result.loc[0, "equity_regime_reason"] == facts.reason
    assert not result.loc[0, "finalist"]


def test_enabled_equity_selection_handles_empty_candidate_frame_without_empty_in_query(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    request = parse_selection_request({"symbol": "ETHUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    status = selection_cache_status(connection, request, SelectionConfig(), include_equity=True)
    candidates = load_selection_candidates(connection, request, SelectionConfig(), cache_only=True)
    connection.close()
    result = run_selection(candidates, request)

    assert status == {"total": 0, "missing": 0, "ready": False}
    assert candidates.empty
    assert "_equity_cache" in candidates.columns
    assert result.empty
    assert result.attrs["stage_counts"]["filter_equity_regime"] == {
        "enabled": True, "eliminated": 0, "remaining": 0, "not_evaluated": 0,
    }


def test_equity_selection_off_hydrates_optional_cache_sentinel_without_equity_compute(
    tmp_path: Path, monkeypatch,
) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    absent = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    disabled = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": False, "scope": "pair_side"},
    ]})
    prepare_selection_window_cache(database, absent, SelectionConfig(), workers=1)
    monkeypatch.setattr(selection_module, "_load_source", lambda *args: (_ for _ in ()).throw(AssertionError("raw source read")))
    monkeypatch.setattr(selection_module, "_load_equity_samples_for_quality", lambda *args: (_ for _ in ()).throw(AssertionError("raw equity read")))
    monkeypatch.setattr(selection_module, "_persist", lambda *args: (_ for _ in ()).throw(AssertionError("cache write")))
    with duckdb.connect(str(database), read_only=True) as check:
        calls: list[str] = []

        class CountingConnection:
            def execute(self, sql: str, parameters: object = None):
                if "from equity_quality_metrics" in sql.lower():
                    calls.append(sql)
                return check.execute(sql) if parameters is None else check.execute(sql, parameters)

        legacy_candidates = load_selection_candidates(CountingConnection(), absent, SelectionConfig(), cache_only=True)
        disabled_candidates = load_selection_candidates(CountingConnection(), disabled, SelectionConfig(), cache_only=True)
    assert legacy_candidates.columns.tolist() == disabled_candidates.columns.tolist()
    assert "_equity_cache" in disabled_candidates.columns
    assert disabled_candidates.loc[0, "_equity_cache"] == {"status": "ABSENT"}
    assert len(calls) == 2
    assert run_selection(legacy_candidates, absent).equals(run_selection(disabled_candidates, disabled))
    disabled_result = run_selection(disabled_candidates, disabled)
    assert "eliminated_by_filter_equity_regime" not in disabled_result
    assert disabled_result.attrs["stage_counts"]["filter_equity_regime"] == {
        "enabled": False, "eliminated": 0, "remaining": 1,
    }


@pytest.mark.parametrize(("sentinel", "mutation"), [
    ("STALE", "update equity_quality_metrics set source_revision = 'stale' where result_id = ?"),
    ("INVALID", "update equity_quality_metrics set facts_sha256 = ? where result_id = ?"),
    ("SCHEMA5", None),
])
def test_disabled_equity_consumer_tolerates_stale_invalid_and_v5_sentinels(
    tmp_path: Path, monkeypatch, sentinel: str, mutation: str | None,
) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)
    with duckdb.connect(str(database)) as writer:
        if sentinel == "SCHEMA5":
            writer.execute("drop table equity_quality_metrics")
            writer.execute("update schema_info set value = '5' where key = 'schema_version'")
        elif sentinel == "INVALID":
            writer.execute(mutation, ["0" * 64, result_id])
        else:
            writer.execute(mutation, [result_id])

    monkeypatch.setattr(selection_module, "_load_source", lambda *args: (_ for _ in ()).throw(AssertionError("raw source read")))
    monkeypatch.setattr(selection_module, "_load_equity_samples_for_quality", lambda *args: (_ for _ in ()).throw(AssertionError("raw equity read")))
    monkeypatch.setattr(selection_module, "_persist", lambda *args: (_ for _ in ()).throw(AssertionError("cache write")))
    with duckdb.connect(str(database), read_only=True) as check:
        candidates = load_selection_candidates(check, request, SelectionConfig(), cache_only=True)

    assert candidates.loc[0, "_equity_cache"] == {"status": sentinel}


def test_selection_loader_prefers_current_equity_algorithm_when_older_row_exists(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)

    with duckdb.connect(str(database)) as connection:
        connection.execute(
            """insert into equity_quality_metrics
               select result_id, source_revision, 'older-equity-quality-v1', facts_json,
                      facts_sha256, calculated_at_utc
                 from equity_quality_metrics where result_id = ? and algo_version = ?""",
            [result_id, equity_cache_module.ALGORITHM_VERSION],
        )
        candidates = load_selection_candidates(connection, request, SelectionConfig(), cache_only=True)
        assert candidates.loc[0, "_equity_cache"]["status"] == "FRESH"
        connection.execute(
            "delete from equity_quality_metrics where result_id = ? and algo_version = ?",
            [result_id, equity_cache_module.ALGORITHM_VERSION],
        )
        only_older = load_selection_candidates(connection, request, SelectionConfig(), cache_only=True)

    assert only_older.loc[0, "_equity_cache"] == {"status": "STALE"}


def test_serial_and_parallel_equity_preparation_publish_identical_canonical_bytes(tmp_path: Path) -> None:
    serial_dir = tmp_path / "serial"
    serial_dir.mkdir()
    connection = _candidate_db(serial_dir)
    _clone_current_candidate(connection, "beta")
    serial_database = serial_dir / "strategy_performance.duckdb"
    connection.close()
    parallel_database = tmp_path / "parallel.duckdb"
    shutil.copyfile(serial_database, parallel_database)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})

    prepare_selection_window_cache(serial_database, request, SelectionConfig(), workers=1, include_equity=True)
    prepare_selection_window_cache(parallel_database, request, SelectionConfig(), workers=2, include_equity=True)

    with duckdb.connect(str(serial_database), read_only=True) as serial, duckdb.connect(
        str(parallel_database), read_only=True
    ) as parallel:
        serial_bytes = serial.execute(
            "select facts_json, facts_sha256 from equity_quality_metrics order by result_id"
        ).fetchall()
        parallel_bytes = parallel.execute(
            "select facts_json, facts_sha256 from equity_quality_metrics order by result_id"
        ).fetchall()
    assert serial_bytes == parallel_bytes


def test_retest_equity_warmup_is_limited_to_the_exact_cohort(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    first_id = int(connection.execute("select strategy_id from strategies").fetchone()[0])
    second_id, _ = _clone_current_candidate(connection, "beta")
    first_result = int(connection.execute(
        "select current_result_id from strategies where strategy_id = ?", [first_id]
    ).fetchone()[0])
    connection.close()
    ordinary = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    scoped = retest_cohort_request(ordinary, "bulk-equity", {first_id: first_result})

    prepare_selection_window_cache(
        database, scoped, SelectionConfig(), workers=2,
        strategy_ids=(first_id, second_id), include_equity=True,
    )

    with duckdb.connect(str(database), read_only=True) as check:
        assert check.execute("select result_id from equity_quality_metrics").fetchall() == [(first_result,)]
        assert check.execute("select distinct result_id from window_metrics").fetchall() == [(first_result,)]


def test_equity_readiness_requires_v6_only_when_opted_in(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    connection.execute("drop table equity_quality_metrics")
    connection.execute("update schema_info set value = '5' where key = 'schema_version'")
    try:
        assert selection_cache_status(connection, request, SelectionConfig()) == {
            "total": 1, "missing": 1, "ready": False,
        }
        with pytest.raises(EquityQualityCacheError, match="EQUITY_SCHEMA_UPGRADE_REQUIRED"):
            selection_cache_status(connection, request, SelectionConfig(), include_equity=True)
    finally:
        connection.close()


def test_cold_full_and_warm_bounded_sources_publish_identical_canonical_facts(tmp_path: Path) -> None:
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    connection = _candidate_db(full_dir)
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.execute("update strategy_results set report_end_utc = ? where result_id = ?", [datetime(2026, 1, 31, tzinfo=UTC), result_id])
    connection.execute("update strategy_equity set equity = -1 where result_id = ? and sample_index = 0", [result_id])
    connection.execute("insert into strategy_equity values (?, 4, ?, 100, 100)", [result_id, datetime(2026, 1, 2, tzinfo=UTC)])
    connection.execute("insert into strategy_equity values (?, 5, ?, 100, 100)", [result_id, datetime(2026, 2, 1, tzinfo=UTC)])
    full_database = full_dir / "strategy_performance.duckdb"
    connection.close()
    bounded_database = tmp_path / "bounded.duckdb"
    shutil.copyfile(full_database, bounded_database)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})

    prepare_selection_window_cache(full_database, request, SelectionConfig(), workers=1, include_equity=True)
    prepare_selection_window_cache(bounded_database, request, SelectionConfig(), workers=1)
    prepare_selection_window_cache(bounded_database, request, SelectionConfig(), workers=1, include_equity=True)

    with duckdb.connect(str(full_database), read_only=True) as full, duckdb.connect(
        str(bounded_database), read_only=True
    ) as bounded:
        full_fact = full.execute("select facts_json, facts_sha256 from equity_quality_metrics").fetchone()
        bounded_fact = bounded.execute("select facts_json, facts_sha256 from equity_quality_metrics").fetchone()
    assert full_fact == bounded_fact
    facts = decode_equity_facts(*full_fact)
    assert facts.raw_sample_count == 6
    assert facts.in_report_sample_count == 5
    assert facts.nonpositive_in_report_rows == 1
    assert facts.duplicate_timestamp_count == 1
    assert facts.invalid_reasons == ("EQUITY_OUTSIDE_REPORT_INTERVAL",)


def test_bounded_source_uses_greatest_sample_index_at_duplicate_timestamp(tmp_path: Path) -> None:
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    connection = _candidate_db(full_dir)
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.execute(
        "update strategy_results set report_end_utc = ? where result_id = ?",
        [datetime(2026, 1, 31, tzinfo=UTC), result_id],
    )
    connection.execute(
        "update strategy_equity set timestamp_utc = ? where result_id = ? and sample_index = 2",
        [datetime(2026, 1, 4, tzinfo=UTC), result_id],
    )
    connection.execute(
        "insert into strategy_equity values (?, 4, ?, 150, 150)",
        [result_id, datetime(2026, 1, 2, tzinfo=UTC)],
    )
    full_database = full_dir / "strategy_performance.duckdb"
    connection.close()
    bounded_database = tmp_path / "bounded.duckdb"
    shutil.copyfile(full_database, bounded_database)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})

    prepare_selection_window_cache(full_database, request, SelectionConfig(), workers=1, include_equity=True)
    prepare_selection_window_cache(bounded_database, request, SelectionConfig(), workers=1)
    prepare_selection_window_cache(bounded_database, request, SelectionConfig(), workers=1, include_equity=True)

    with duckdb.connect(str(full_database), read_only=True) as full, duckdb.connect(
        str(bounded_database), read_only=True
    ) as bounded:
        full_fact = full.execute("select facts_json, facts_sha256 from equity_quality_metrics").fetchone()
        bounded_fact = bounded.execute("select facts_json, facts_sha256 from equity_quality_metrics").fetchone()
    assert full_fact == bounded_fact
    facts = decode_equity_facts(*full_fact)
    horizon = next(window for window in facts.windows if window.days == 28)

    assert facts.duplicate_timestamp_count == 1
    assert facts.horizon_days == 28
    assert facts.state == "DECLINING_OR_MIXED"
    assert facts.drawdown is not None
    assert Decimal("-27") < horizon.return_pct < Decimal("-26")
    assert Decimal("0.26") < facts.drawdown < Decimal("0.27")


def test_pre_h_duplicate_timestamp_is_valid_and_counted(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.execute("update strategy_results set report_end_utc = ? where result_id = ?", [datetime(2026, 1, 31, tzinfo=UTC), result_id])
    connection.execute("insert into strategy_equity values (?, 4, ?, 100, 100)", [result_id, datetime(2026, 1, 2, tzinfo=UTC)])
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()

    prepare_selection_window_cache(
        database, request, SelectionConfig(), workers=1, include_equity=True
    )
    with duckdb.connect(str(database), read_only=True) as reader:
        metadata = current_equity_source_metadata(reader, result_id)
        facts = read_equity_quality_facts(reader, result_id, equity_source_revision(metadata))

    assert facts is not None
    assert facts.state == "FLAT"
    assert facts.duplicate_timestamp_count == 1
    assert facts.invalid_reasons == ()


def test_selection_window_job_fetches_cached_windows_in_one_query(tmp_path: Path, monkeypatch) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id, report_start, report_end = connection.execute(
        "select result_id, report_start_utc, report_end_utc from strategy_results"
    ).fetchone()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    windows = _selection_windows(report_start, report_end, SelectionConfig())
    connection.close()
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1)

    helper = getattr(selection_module, "_cached_many", None)
    assert callable(helper), "result-scoped cached-window loader is missing"

    class CountingConnection:
        def __init__(self, target):
            self.target = target
            self.queries = 0

        def execute(self, *args):
            self.queries += 1
            return self.target.execute(*args)

    with duckdb.connect(str(database), read_only=True) as reader:
        legacy = CountingConnection(reader)
        legacy_cached = tuple(
            _cached(legacy, int(result_id), start, end, METRICS_VERSION)
            for start, end in windows
        )
        counted = CountingConnection(reader)
        cached = helper(counted, int(result_id), windows, METRICS_VERSION)

    assert len(cached) == len(windows) == 7
    assert all(metric is not None for metric in cached)
    assert cached == legacy_cached
    assert legacy.queries == 7
    assert counted.queries == 1
    assert cached[-1].requested_end_utc == windows[-1][1]


def test_selection_preparation_caps_queued_jobs_at_twice_worker_count(tmp_path: Path, monkeypatch) -> None:
    connection = _candidate_db(tmp_path)
    for name in ("beta", "gamma", "delta", "epsilon"):
        _clone_current_candidate(connection, name)
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    submitted: list[tuple[object, ...]] = []
    pending_count = 0
    max_pending = 0

    class ObservedFuture(Future):
        def __init__(self):
            super().__init__()
            self.consumed = False

        def result(self, *args, **kwargs):
            nonlocal pending_count
            result = super().result(*args, **kwargs)
            if not self.consumed:
                self.consumed = True
                pending_count -= 1
            return result

    class DeterministicExecutor:
        def __init__(self, max_workers):
            assert max_workers == 2

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, function, args):
            nonlocal pending_count, max_pending
            submitted.append(args)
            pending_count += 1
            max_pending = max(max_pending, pending_count)
            future = ObservedFuture()
            future.set_result(function(args))
            return future

    monkeypatch.setattr(selection_module, "ThreadPoolExecutor", DeterministicExecutor)
    monkeypatch.setattr(
        selection_module,
        "_selection_window_job_from_args",
        lambda _args: selection_module._SelectionWindowJobResult((), None),
    )

    prepare_selection_window_cache(database, request, SelectionConfig(), workers=2)

    assert len(submitted) == 5
    assert max_pending == 4
    assert pending_count == 0


def test_earlier_preparation_batch_remains_committed_after_later_batch_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    connection = _candidate_db(tmp_path)
    for name in ("beta", "gamma"):
        _clone_current_candidate(connection, name)
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    original = selection_module._selection_window_job_from_args
    seen: list[int] = []

    def fail_on_third_result(args):
        seen.append(int(args[1]))
        if len(seen) == 3:
            raise RuntimeError("later preparation batch failed")
        return original(args)

    monkeypatch.setattr(selection_module, "_selection_window_job_from_args", fail_on_third_result)

    with pytest.raises(RuntimeError, match="later preparation batch failed"):
        prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)

    assert len(seen) == 3
    with duckdb.connect(str(database), read_only=True) as check:
        assert set(row[0] for row in check.execute("select distinct result_id from window_metrics").fetchall()) == set(seen[:2])
        assert set(row[0] for row in check.execute("select result_id from equity_quality_metrics").fetchall()) == set(seen[:2])


def test_equity_only_cold_warmup_uses_bounded_equity_without_legacy_source_or_actions(
    tmp_path: Path, monkeypatch,
) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id, strategy_id = connection.execute(
        "select result_id, strategy_id from strategy_results"
    ).fetchone()
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    config = SelectionConfig()
    prepare_selection_window_cache(database, request, config, workers=1)
    with duckdb.connect(str(database)) as writer:
        writer.execute(
            "update strategy_equity set equity = -1 where result_id = ? and sample_index = 0",
            [result_id],
        )
    bounded_counts: list[int] = []
    original_loader = selection_module._load_equity_samples_for_quality

    def counted_loader(*args):
        samples, summary = original_loader(*args)
        bounded_counts.append(len(samples))
        return samples, summary

    def legacy_load_forbidden(*_args):
        raise AssertionError("equity-only cold preparation read actions through legacy source loading")

    monkeypatch.setattr(selection_module, "_load_equity_samples_for_quality", counted_loader)
    monkeypatch.setattr(selection_module, "_load_source", legacy_load_forbidden)

    with duckdb.connect(str(database), read_only=True) as check:
        assert selection_cache_missing_strategy_ids(
            check, request, config, include_equity=True
        ) == (strategy_id,)
    prepare_selection_window_cache(
        database, request, config, workers=1, strategy_ids=(strategy_id,), include_equity=True
    )

    assert bounded_counts == [3]
    with duckdb.connect(str(database), read_only=True) as check:
        metadata = current_equity_source_metadata(check, result_id)
        facts = read_equity_quality_facts(check, result_id, equity_source_revision(metadata))
        assert facts is not None
        assert facts.state == "NONPOSITIVE_EQUITY"
        assert facts.raw_sample_count == 4
        assert facts.in_report_sample_count == 4
        assert facts.nonpositive_in_report_rows == 1


def test_warm_equity_preparation_has_no_source_load_or_cache_write(tmp_path: Path, monkeypatch) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    _result_id, strategy_id = connection.execute(
        "select result_id, strategy_id from strategy_results"
    ).fetchone()
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    config = SelectionConfig()
    prepare_selection_window_cache(database, request, config, workers=1, include_equity=True)
    with duckdb.connect(str(database), read_only=True) as check:
        before = check.execute(
            "select source_revision, facts_json, facts_sha256, calculated_at_utc from equity_quality_metrics"
        ).fetchall()
        window_times = check.execute(
            "select calculated_at_utc from window_metrics order by requested_start_utc, requested_end_utc"
        ).fetchall()

    def raw_load_forbidden(*_args):
        raise AssertionError("warm preparation read raw source rows")

    monkeypatch.setattr(selection_module, "_load_source", raw_load_forbidden)
    monkeypatch.setattr(selection_module, "_load_equity_samples_for_quality", raw_load_forbidden)
    monkeypatch.setattr(selection_module, "_persist", raw_load_forbidden)
    prepare_selection_window_cache(
        database, request, config, workers=1, strategy_ids=(strategy_id,), include_equity=True
    )

    with duckdb.connect(str(database), read_only=True) as check:
        assert check.execute(
            "select source_revision, facts_json, facts_sha256, calculated_at_utc from equity_quality_metrics"
        ).fetchall() == before
        assert check.execute(
            "select calculated_at_utc from window_metrics order by requested_start_utc, requested_end_utc"
        ).fetchall() == window_times


def test_equity_readiness_is_opt_in_and_rechecks_source_revision_and_digest(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id, strategy_id = connection.execute(
        "select result_id, strategy_id from strategy_results"
    ).fetchone()
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    config = SelectionConfig()
    prepare_selection_window_cache(database, request, config, workers=1, include_equity=True)

    with duckdb.connect(str(database)) as writer:
        writer.execute(
            "update strategy_results set imported_at_utc = imported_at_utc + interval '1 second' where result_id = ?",
            [result_id],
        )
    with duckdb.connect(str(database), read_only=True) as check:
        assert selection_cache_status(check, request, config) == {"total": 1, "missing": 0, "ready": True}
        assert selection_cache_status(check, request, config, include_equity=True)["ready"] is False
        assert selection_cache_missing_strategy_ids(check, request, config, include_equity=True) == (strategy_id,)
    prepare_selection_window_cache(
        database, request, config, workers=1, strategy_ids=(strategy_id,), include_equity=True
    )
    with duckdb.connect(str(database)) as writer:
        writer.execute("update equity_quality_metrics set facts_sha256 = ?", ["0" * 64])
    with duckdb.connect(str(database), read_only=True) as check:
        assert selection_cache_status(check, request, config, include_equity=True)["ready"] is False
        assert selection_cache_missing_strategy_ids(check, request, config, include_equity=True) == (strategy_id,)


def test_equity_publication_rechecks_source_revision_and_rolls_back_current_batch(
    tmp_path: Path, monkeypatch,
) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    original = selection_module._selection_window_job_from_args
    changed = False

    def mutate_after_source_read(args):
        nonlocal changed
        result = original(args)
        if not changed:
            changed = True
            with duckdb.connect(str(database)) as writer:
                writer.execute(
                    "update strategy_results set imported_at_utc = imported_at_utc + interval '1 second' where result_id = ?",
                    [result_id],
                )
        return result

    monkeypatch.setattr(selection_module, "_selection_window_job_from_args", mutate_after_source_read)

    with pytest.raises(EquitySourceChangedError, match="EQUITY_SOURCE_CHANGED"):
        prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)

    with duckdb.connect(str(database), read_only=True) as check:
        assert check.execute("select count(*) from window_metrics").fetchone() == (0,)
        assert check.execute("select count(*) from equity_quality_metrics").fetchone() == (0,)


def test_equity_publication_uses_single_writer_source_revision_check(tmp_path: Path, monkeypatch) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    selection_checks = 0
    publication_checks = 0
    read_metadata = selection_module.current_equity_source_metadata
    write_metadata = equity_cache_module.current_equity_source_metadata

    def count_selection_check(connection, result_id):
        nonlocal selection_checks
        selection_checks += 1
        return read_metadata(connection, result_id)

    def count_publication_check(connection, result_id):
        nonlocal publication_checks
        publication_checks += 1
        return write_metadata(connection, result_id)

    monkeypatch.setattr(selection_module, "current_equity_source_metadata", count_selection_check)
    monkeypatch.setattr(equity_cache_module, "current_equity_source_metadata", count_publication_check)

    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1, include_equity=True)

    assert selection_checks == 1
    assert publication_checks == 1


def test_same_id_replace_race_rechecks_revision_for_old_windows_when_equity_is_warm(
    tmp_path: Path, monkeypatch,
) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    result_id = int(connection.execute("select result_id from strategy_results").fetchone()[0])
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    config = SelectionConfig()
    prepare_selection_window_cache(database, request, config, workers=1, include_equity=True)
    with duckdb.connect(str(database)) as writer:
        missing_window = writer.execute(
            "select requested_start_utc, requested_end_utc from window_metrics where result_id = ? order by requested_start_utc, requested_end_utc limit 1",
            [result_id],
        ).fetchone()
        writer.execute(
            "delete from window_metrics where result_id = ? and requested_start_utc = ? and requested_end_utc = ?",
            [result_id, *missing_window],
        )
        before_equity = writer.execute(
            "select source_revision, facts_json, facts_sha256, calculated_at_utc from equity_quality_metrics"
        ).fetchall()
    original = selection_module._selection_window_job_from_args
    changed = False

    def replace_after_read(args):
        nonlocal changed
        result = original(args)
        if not changed:
            changed = True
            with duckdb.connect(str(database)) as writer:
                writer.execute(
                    "update strategy_results set imported_at_utc = imported_at_utc + interval '1 second' where result_id = ?",
                    [result_id],
                )
        return result

    monkeypatch.setattr(selection_module, "_selection_window_job_from_args", replace_after_read)

    with pytest.raises(EquitySourceChangedError, match="EQUITY_SOURCE_CHANGED"):
        prepare_selection_window_cache(database, request, config, workers=1, include_equity=True)

    with duckdb.connect(str(database), read_only=True) as check:
        assert check.execute("select count(*) from window_metrics where result_id = ?", [result_id]).fetchone() == (6,)
        assert check.execute(
            "select source_revision, facts_json, facts_sha256, calculated_at_utc from equity_quality_metrics"
        ).fetchall() == before_equity


def test_loader_derives_proxy_holding_and_order_plateau_counts(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    try:
        result_id = connection.execute("select result_id from strategy_results").fetchone()[0]
        connection.execute(
            "insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size, post_size, post_side, pnl, fee, balance, raw_action_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [result_id, 3, datetime(2026, 1, 3, 12, tzinfo=UTC), "BTCUSDT", 1, "fee", 1, 0, "", -2, 0, 108, None],
        )
        row = load_selection_candidates(connection, request).iloc[0]
    finally:
        connection.close()

    assert row["order_1_plateau_point_count"] == 12
    assert row["order_1_plateau_key"] == ("run", "P1")
    assert row["total_plateau_point_count"] == 12
    assert row["total_trades"] == 1
    assert row["dd5_proxy"] is not None
    assert row["dd5_proxy"] > 0
    assert row["holding_p95_minutes"] == 1440
    assert row["holding_median_minutes"] == 1440
    assert row["ab_pnl_change_30d_pct"] is None
    assert row["trades_30d"] == Decimal("1")
    assert row["total_pnl_pct"] == Decimal("10")
    assert row["positive_quarter_status"] == "UNAVAILABLE"
    assert pd.isna(row["positive_quarter_count"])
    assert pd.isna(row["positive_quarter_available_count"])
    assert row["best_trade_profit_share_pct"] == 100
    assert row["pnl_without_best_trade"] == 0
    assert row["pnl_without_best_trade_pct"] == 0
    assert row["completed_profitable_trade_count"] == 1
    assert row["best_trade_reliable"]


def test_signed_short_actions_feed_holding_and_best_trade_metrics(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    result_id = connection.execute("select result_id from strategy_results").fetchone()[0]
    connection.execute("update strategies set side = 'SHORT'")
    connection.execute(
        "update strategy_actions set size = -1, post_size = -1, post_side = 'short' "
        "where result_id = ? and action = 'opened'",
        [result_id],
    )
    connection.execute(
        "update strategy_actions set post_size = 0, post_side = '' where result_id = ? and action = 'closed'",
        [result_id],
    )
    connection.executemany(
        "insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size, post_size, post_side, pnl, fee, balance, raw_action_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (result_id, 3, datetime(2026, 1, 25, tzinfo=UTC), "BTCUSDT", 1, "opened", -1, -1, "short", 0, 0, 110, None),
            (result_id, 4, datetime(2026, 1, 26, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", 5, 0, 115, None),
        ],
    )
    try:
        row = load_selection_candidates(
            connection, parse_selection_request({"symbol": "BTCUSDT", "side": "SHORT", "stages": []})
        ).iloc[0]
    finally:
        connection.close()

    assert row["holding_p95_minutes"] == Decimal("1440")
    assert row["ab_holding_p95_minutes"] == Decimal("1440")
    assert row["best_trade_reliable"]
    assert row["completed_profitable_trade_count"] == 2
    assert row["pnl_without_best_trade"] == 5


def test_best_trade_facts_with_no_profitable_trip_are_not_evaluable(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    try:
        connection.execute("update strategy_actions set pnl = -10 where action = 'closed'")
        row = load_selection_candidates(connection, request).iloc[0]
    finally:
        connection.close()

    assert row["best_trade_profit_share_pct"] is None
    assert row["pnl_without_best_trade"] is None
    assert row["completed_profitable_trade_count"] is None


def test_loader_exports_pnl_without_best_as_pct_of_initial_balance(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    try:
        result_id = connection.execute("select result_id from strategy_results").fetchone()[0]
        connection.executemany(
            "insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size, post_size, post_side, pnl, fee, balance, raw_action_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (result_id, 3, datetime(2026, 1, 4, tzinfo=UTC), "BTCUSDT", 1, "opened", 1, 1, "long", 0, 0, 110, None),
                (result_id, 4, datetime(2026, 1, 5, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", 5, 1, 115, None),
            ],
        )
        row = load_selection_candidates(connection, request).iloc[0]
    finally:
        connection.close()

    assert row["pnl_without_best_trade"] == 5
    assert row["pnl_without_best_trade_pct"] == 5


def test_holding_p95_uses_all_closed_positions(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    result_id = connection.execute("select result_id from strategy_results").fetchone()[0]
    connection.executemany(
        "insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size, post_size, post_side, pnl, fee, balance, raw_action_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (result_id, 3, datetime(2026, 1, 5, tzinfo=UTC), "BTCUSDT", 1, "opened", 1, 1, "long", 0, 0, 110, None),
            (result_id, 4, datetime(2026, 1, 7, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", 0, 0, 110, None),
            (result_id, 5, datetime(2026, 1, 8, tzinfo=UTC), "BTCUSDT", 1, "opened", 1, 1, "long", 0, 0, 110, None),
            (result_id, 6, datetime(2026, 1, 12, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", 0, 0, 110, None),
        ],
    )
    try:
        p95 = _holding_p95_minutes(connection, request)
    finally:
        connection.close()

    assert p95[result_id] == Decimal("5472.0")


def test_parallel_window_warmup_persists_default_selection_windows(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})

    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1)
    prepare_selection_window_cache(database, request, SelectionConfig(), workers=1)

    with duckdb.connect(str(database), read_only=True) as check:
        assert check.execute("select count(*) from window_metrics").fetchone() == (7,)


def test_missing_cache_strategy_ids_only_returns_current_results_without_facts(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    config = SelectionConfig()
    connection.close()
    prepare_selection_window_cache(database, request, config, workers=1)

    with duckdb.connect(str(database)) as check:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        strategy_id = check.execute(
            """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
               order_count, analysis_run_id, candidate_identity, lifecycle_status,
               created_at_utc, updated_at_utc) values ('beta', 'BTCUSDT', 'LONG', '1h',
               3, 1, 'run', 'candidate-beta', 'ACTIVE', ?, ?) returning strategy_id""",
            [start, start],
        ).fetchone()[0]
        result_id = check.execute(
            """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
               commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
               max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
               values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 2, 2, ?) returning result_id""",
            [strategy_id, start, datetime(2026, 1, 31, tzinfo=UTC), start],
        ).fetchone()[0]
        check.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])

        assert selection_cache_missing_strategy_ids(check, request, config) == (strategy_id,)


def test_legacy_full_ab_only_cache_is_not_ready(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    database = tmp_path / "strategy_performance.duckdb"
    connection.close()
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    config = SelectionConfig()
    prepare_selection_window_cache(database, request, config, workers=1)
    with duckdb.connect(str(database)) as check:
        result_id, start, end = check.execute("select result_id, report_start_utc, report_end_utc from strategy_results").fetchone()
        split = end - timedelta(days=config.ab_final_days)
        check.execute(
            "delete from window_metrics where result_id = ? and (requested_start_utc, requested_end_utc) not in ((?, ?), (?, ?), (?, ?))",
            [result_id, start, end, start, split, split, end],
        )
        assert selection_cache_status(check, request, config) == {"total": 1, "missing": 1, "ready": False}


def test_loader_leaves_incomplete_order_and_empty_candidate_facts_blank(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    try:
        connection.execute("update strategies set order_count = 2")
        connection.execute("update strategy_results set max_drawdown_pct = 10")
        row = load_selection_candidates(connection, request).iloc[0]
        now = datetime(2026, 2, 1, tzinfo=UTC)
        no_orders_id = connection.execute(
            """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
               order_count, analysis_run_id, candidate_identity, lifecycle_status,
               created_at_utc, updated_at_utc) values ('without-orders', 'BTCUSDT', 'LONG', '1h',
               3, 1, 'run', 'candidate-2', 'ACTIVE', ?, ?) returning strategy_id""",
            [now, now],
        ).fetchone()[0]
        no_orders_result = connection.execute(
            """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
               commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
               max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
               values (?, ?, ?, 'Bybit', .0004, 100, 100, 0, 0, 0, 0, 0, 0, ?) returning result_id""",
            [no_orders_id, now, now, now],
        ).fetchone()[0]
        connection.execute(
            "update strategies set current_result_id = ? where strategy_id = ?", [no_orders_result, no_orders_id]
        )
        all_rows = load_selection_candidates(connection, request).set_index("strategy_name")
        connection.execute("update strategy_results set max_drawdown_pct = 0 where strategy_id = 1")
        zero_dd = load_selection_candidates(connection, request).set_index("strategy_name").loc["alpha"]
        empty = load_selection_candidates(
            connection, parse_selection_request({"symbol": "ETHUSDT", "side": "LONG", "stages": []})
        )
    finally:
        connection.close()

    assert row["risk_scale"] == Decimal("0.5")
    assert row["dd5_proxy"] is not None
    assert row["scaled_lot_sum"] is None
    assert row["capital_proxy"] is None
    assert row["capital_efficiency"] is None
    assert set(all_rows.index) == {"alpha", "without-orders"}
    assert pd.isna(all_rows.loc["without-orders", "order_1_plateau_point_count"])
    assert pd.isna(all_rows.loc["without-orders", "capital_proxy"])
    assert zero_dd["dd5_proxy"] is None
    assert zero_dd["scaled_lot_sum"] is None
    assert "strategy_id" in empty.columns
    assert empty.empty


def _selection_row(name: str, **values: object) -> dict[str, object]:
    row = {
        "strategy_id": 1 if name == "winner" else 2,
        "strategy_name": name,
        "timeframe": "1h",
        "order_count": 1,
        "dd5_proxy": Decimal("10") if name == "winner" else Decimal("5"),
        "first_shift_bp": 200 if name == "winner" else 100,
        "order_1_shift_bp": 30 if name == "winner" else 270,
        "order_2_plateau_point_count": Decimal("7.6"),
        "order_3_plateau_point_count": Decimal("8.4"),
        "order_4_plateau_point_count": Decimal("9.5"),
        "order_1_open_ma_len": Decimal("3.6"),
        "order_2_open_ma_len": Decimal("4.4"),
        "order_3_open_ma_len": Decimal("5.5"),
        "order_4_open_ma_len": Decimal("6.1"),
        "order_1_lot_x": Decimal("0.25"),
        "order_2_lot_x": Decimal("0.50"),
        "order_3_lot_x": Decimal("0.75"),
        "order_4_lot_x": Decimal("1.00"),
        "capital_proxy": Decimal("1") if name == "winner" else Decimal("2"),
        "capital_efficiency": Decimal("10") if name == "winner" else Decimal("2.5"),
        "holding_p95_minutes": Decimal("10") if name == "winner" else Decimal("20"),
        "close_ma_len": 3 if name == "winner" else 5,
        "total_trades": 100,
        "order_1_plateau_point_count": 20 if name == "winner" else 10,
        "total_plateau_point_count": 20 if name == "winner" else 10,
        **values,
    }
    return _with_test_equity_cache(row)


def _equity_rank_facts(
    result_id: int, growth_class: int | None, score: str | None, drawdown: str | None,
    peak_gap: str | None, horizon: int | None, *, state: str | None = None,
) -> dict[str, object]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    facts = calculate_equity_quality_facts(
        result_id, start, start + timedelta(days=30),
        (EquitySample(result_id, 0, start, Decimal("100")),
         EquitySample(result_id, 1, start + timedelta(days=30), Decimal("101"))),
    )
    canonical_facts = replace(
            facts,
            state=state or ("GROWING" if growth_class == 0 else "WEAKENING" if growth_class == 1 else "DECLINING_OR_MIXED"),
            equity_class=growth_class,
            score12=None if score is None else Decimal(score),
            drawdown=None if drawdown is None else Decimal(drawdown),
            peak_gap=None if peak_gap is None else Decimal(peak_gap),
            horizon_days=horizon,
        )
    encoded_facts = json.dumps(canonical_facts.to_canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "facts": canonical_facts,
        "source_revision": "0" * 64,
        "facts_sha256": hashlib.sha256(encoded_facts.encode()).hexdigest(),
    }


def _with_test_equity_cache(row: dict[str, object]) -> dict[str, object]:
    if "_equity_quality" in row and "_equity_cache" not in row:
        quality = row["_equity_quality"]
        facts = quality.get("facts") if isinstance(quality, dict) else None
        if facts is not None:
            row.setdefault("result_id", facts.result_id)
            row["_equity_cache"] = {"status": "FRESH", **quality}
    elif all(column in row for column in ("_equity_state", "_equity_disposition", "_equity_reason")):
        result_id = int(row.get("result_id", 100_000 + int(row["strategy_id"])))
        row["result_id"] = result_id
        quality = _equity_rank_facts(result_id, None, None, None, None, None, state=str(row["_equity_state"]))
        facts = replace(
            quality["facts"], reason=str(row["_equity_reason"]),
            erf_disposition=str(row["_equity_disposition"]),
        )
        digest = hashlib.sha256(json.dumps(
            facts.to_canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        row["_equity_cache"] = {
            "status": "FRESH", "facts": facts,
            "source_revision": "0" * 64, "facts_sha256": digest,
        }
    return row


def _lot_variant_row(
    name: str,
    strategy_id: int,
    lots: tuple[str, str] = ("1", "2"),
    interval: tuple[datetime, datetime] = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)),
    **metrics: object,
) -> dict[str, object]:
    start, end = interval
    row = {
        "strategy_id": strategy_id,
        "strategy_name": name,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "close_ma_len": 20,
        "order_count": 2,
        "order_1_open_ma_len": 5,
        "order_1_shift_bp": 100,
        "order_1_lot_x": Decimal(lots[0]),
        "order_2_open_ma_len": 10,
        "order_2_shift_bp": 200,
        "order_2_lot_x": Decimal(lots[1]),
        "report_start_utc": start,
        "report_end_utc": end,
        "effective_start_utc": start,
        "effective_end_utc": end,
        "dd5_proxy": Decimal("10"),
        "capital_proxy": Decimal("5"),
        "robust_pnl_30d_pct": Decimal("8"),
        "worst_drawdown_pct": Decimal("6"),
        "profit_factor": Decimal("1.5"),
        **metrics,
    }
    return _with_test_equity_cache(row)


def test_selection_workbook_shows_effective_dates_as_day_and_month(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    path = write_selection_workbook(
        pd.DataFrame([_lot_variant_row(
            "dated", 1,
            interval=(datetime(2026, 2, 11, 23, 30, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC)),
            finalist=True,
        )]),
        tmp_path / "dated.xlsx", request,
    )

    sheet = load_workbook(path, data_only=True)["All candidates"]
    headers = [cell.value for cell in sheet[1]]
    assert headers[headers.index("Start")] == "Start"
    assert headers[headers.index("End")] == "End"
    assert sheet.cell(2, headers.index("Start") + 1).value == "11.02"
    assert sheet.cell(2, headers.index("End") + 1).value == "03.09"


def test_selection_workbook_keeps_missing_effective_dates_blank_for_sparse_rows(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    result = pd.DataFrame([{"strategy_id": 1, "finalist": True, "effective_start_utc": None, "effective_end_utc": pd.NaT}])
    sheet = load_workbook(
        write_selection_workbook(result, tmp_path / "blank-dates.xlsx", request), data_only=True
    )["All candidates"]
    headers = [cell.value for cell in sheet[1]]

    assert sheet.cell(2, headers.index("Start") + 1).value is None
    assert sheet.cell(2, headers.index("End") + 1).value is None

    empty_headers = [cell.value for cell in load_workbook(
        write_selection_workbook(pd.DataFrame(), tmp_path / "empty.xlsx", request), data_only=True
    )["All candidates"][1]]
    assert {"Start", "End"}.issubset(empty_headers)


def test_selection_workbook_keeps_malformed_effective_dates_blank(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    sheet = load_workbook(write_selection_workbook(
        pd.DataFrame([{"strategy_id": 1, "finalist": False, "effective_start_utc": "not-a-date", "effective_end_utc": ""}]),
        tmp_path / "malformed-dates.xlsx", request,
    ), data_only=True)["All candidates"]
    headers = [cell.value for cell in sheet[1]]

    assert sheet.cell(2, headers.index("Start") + 1).value is None
    assert sheet.cell(2, headers.index("End") + 1).value is None


def test_lot_variant_filter_is_default_on_first_and_keeps_loser_auditable() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_best_trade_dependency", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "filter_lot_variant_redundancy", "enabled": True, "scope": "pair_side_timeframe"},
    ]})
    result = run_selection(pd.DataFrame([
        _lot_variant_row("winner", 1, lots=("1", "2"), dd5_proxy=Decimal("11")),
        _lot_variant_row(
            "loser", 2, lots=("3", "4"), best_trade_reliable=True,
            completed_profitable_trade_count=4, pnl_without_best_trade=Decimal("0"),
            best_trade_profit_share_pct=Decimal("40"),
        ),
    ]), request).set_index("strategy_name")

    assert result.loc["winner", "finalist"]
    assert not result.loc["loser", "finalist"]
    assert result.loc["loser", "eliminated_by_filter_lot_variant_redundancy"]
    assert not result.loc["loser", "eliminated_by_filter_best_trade_dependency"]
    assert result.loc["loser", "auto_status"] == "FILTERED"
    assert result.loc["loser", "elimination_reason"] == "LOT_VARIANT_REDUNDANT"
    assert result.loc["winner", "lot_variant_representative_strategy_id"] == 1
    assert result.loc["loser", "lot_variant_representative_strategy_id"] == 1
    assert result.loc["winner", "lot_variant_group_key"] == result.loc["loser", "lot_variant_group_key"]


def test_lot_variant_filter_can_be_disabled_and_fails_closed() -> None:
    frame = pd.DataFrame([
        _lot_variant_row("missing-a", 1, lots=("1", "2"), profit_factor=None),
        _lot_variant_row("missing-b", 2, lots=("3", "4"), profit_factor=None),
    ])
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})

    disabled = run_selection(frame, request, SelectionConfig(lot_variant_redundancy_enabled=False))
    assert disabled["finalist"].all()
    assert disabled["lot_variant_group_key"].isna().all()

    failed_closed = run_selection(frame, request)
    assert failed_closed["finalist"].all()
    assert failed_closed["lot_variant_group_key"].isna().all()

    malformed = pd.DataFrame([
        _lot_variant_row("bad-a", 1, lots=("1", "2"), effective_start_utc="not-a-date"),
        _lot_variant_row("bad-b", 2, lots=("3", "4")),
    ])
    malformed_result = run_selection(malformed, request)
    assert malformed_result["finalist"].all()
    assert malformed_result["lot_variant_group_key"].isna().all()


def test_lot_variant_filter_isolated_by_interval_and_canonicalizes_order_permutation() -> None:
    first = _lot_variant_row("first", 1, lots=("1", "2"))
    second = _lot_variant_row("second", 2, lots=("3", "4"))
    second["order_1_open_ma_len"], second["order_2_open_ma_len"] = second["order_2_open_ma_len"], second["order_1_open_ma_len"]
    second["order_1_shift_bp"], second["order_2_shift_bp"] = second["order_2_shift_bp"], second["order_1_shift_bp"]
    second["order_1_lot_x"], second["order_2_lot_x"] = second["order_2_lot_x"], second["order_1_lot_x"]
    same_interval = run_selection(pd.DataFrame([first, second]), parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})).set_index("strategy_name")
    assert same_interval.loc["first", "finalist"]
    assert not same_interval.loc["second", "finalist"]

    later = _lot_variant_row(
        "later", 3, lots=("3", "4"),
        interval=(datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC)),
    )
    separate = run_selection(pd.DataFrame([first, later]), parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []}))
    assert separate["finalist"].all()


@pytest.mark.parametrize(
    ("metric", "a_value", "b_value", "expected"),
    [
        ("dd5_proxy", Decimal("11"), Decimal("10"), "a"),
        ("capital_proxy", Decimal("4"), Decimal("5"), "a"),
        ("robust_pnl_30d_pct", Decimal("9"), Decimal("8"), "a"),
        ("worst_drawdown_pct", Decimal("5"), Decimal("6"), "a"),
        ("profit_factor", Decimal("1.6"), Decimal("1.5"), "a"),
        ("strategy_id", None, None, "b"),
    ],
)
def test_lot_variant_filter_uses_declared_winner_order(metric: str, a_value: object, b_value: object, expected: str) -> None:
    a_id, b_id = (2, 1) if metric == "strategy_id" else (1, 2)
    a = _lot_variant_row("a", a_id, lots=("1", "2"), **({metric: a_value} if a_value is not None else {}))
    b = _lot_variant_row("b", b_id, lots=("3", "4"), **({metric: b_value} if b_value is not None else {}))
    result = run_selection(pd.DataFrame([a, b]), parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []}))
    finalists = result.loc[result["finalist"], "strategy_name"].tolist()
    assert finalists == [expected]


@pytest.mark.parametrize("stage_id", [
    "pareto_dd5_balanced", "pareto_plateau_points_per_order", "pareto_plateau_points_total",
    "pareto_efficiency_shift", "pareto_dd5_holding", "pareto_dd5_close_ma",
    "pareto_dd5_first_shift", "pareto_primary", "pareto_dd5_capital",
])
def test_pareto_stages_eliminate_dominated_candidate(stage_id: str) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": stage_id, "enabled": True, "scope": "pair_side"},
    ]})

    result = run_selection(pd.DataFrame([_selection_row("winner"), _selection_row("loser")]), request)
    result = result.set_index("strategy_name")

    assert not result.loc["winner", f"eliminated_by_{stage_id}"]
    assert result.loc["loser", f"eliminated_by_{stage_id}"]
    assert not result.loc["loser", "finalist"]


def test_ab_insufficient_data_does_not_eliminate() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "ab_deterioration", "enabled": True, "scope": "pair_side"},
    ]})
    frame = pd.DataFrame([_selection_row("winner")])

    result = run_selection(frame, request)

    assert result.loc[0, "finalist"]
    assert result.loc[0, "elimination_reason"] == "AB_NOT_EVALUATED_INSUFFICIENT_DATA"


def test_new_robust_filters_only_eliminate_evaluable_or_dominated_candidates() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_best_trade_dependency", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "filter_time_consistency", "enabled": True, "scope": "pair_side_timeframe"},
        {"id": "pareto_robust", "enabled": True, "scope": "pair_side_timeframe"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("winner", strategy_id=1, best_trade_reliable=True, completed_profitable_trade_count=4,
                       best_trade_profit_share_pct=Decimal("20"), pnl_without_best_trade=Decimal("5"),
                       positive_quarter_count=4, positive_quarter_available_count=4,
                       robust_pnl_30d_pct=Decimal("20"), worst_drawdown_pct=Decimal("4"),
                       worst_holding_p95_minutes=Decimal("40"), first_shift_bp=200),
        _selection_row("dependent", strategy_id=2, best_trade_reliable=True, completed_profitable_trade_count=4,
                       best_trade_profit_share_pct=Decimal("36"), pnl_without_best_trade=Decimal("5"),
                       positive_quarter_count=4, positive_quarter_available_count=4),
        _selection_row("boundary", strategy_id=5, best_trade_reliable=True, completed_profitable_trade_count=4,
                       best_trade_profit_share_pct=Decimal("35"), pnl_without_best_trade=Decimal("5"),
                       positive_quarter_count=1, positive_quarter_available_count=3),
        _selection_row("inconsistent", strategy_id=3, best_trade_reliable=False, completed_profitable_trade_count=1,
                       positive_quarter_count=2, positive_quarter_available_count=4),
        _selection_row("dominated", strategy_id=4, best_trade_reliable=False, completed_profitable_trade_count=1,
                       positive_quarter_count=4, positive_quarter_available_count=4,
                       robust_pnl_30d_pct=Decimal("10"), worst_drawdown_pct=Decimal("5"),
                       worst_holding_p95_minutes=Decimal("50"), first_shift_bp=100),
    ]), request).set_index("strategy_name")

    assert result.loc["dependent", "eliminated_by_filter_best_trade_dependency"]
    assert not result.loc["boundary", "eliminated_by_filter_best_trade_dependency"]
    assert not result.loc["boundary", "eliminated_by_filter_time_consistency"]
    assert result.loc["inconsistent", "eliminated_by_filter_time_consistency"]
    assert result.loc["dominated", "eliminated_by_pareto_robust"]
    assert result.loc["winner", "finalist"]


def test_shift_near_tie_and_final_rank_keep_top_rankable_and_unranked_rows() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_shift_near_tie", "enabled": True, "scope": "pair_side_timeframe", "pnl_tolerance_pct": "10"},
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("shift-winner", strategy_id=1, robust_pnl_30d_pct=Decimal("100"), worst_drawdown_pct=Decimal("4"),
                       worst_holding_p95_minutes=Decimal("40"), ab_stability_ratio=Decimal(".9"), minimum_plateau_point_count=20, first_shift_bp=200),
        _selection_row("near-tie", strategy_id=2, robust_pnl_30d_pct=Decimal("95"), worst_drawdown_pct=Decimal("5"),
                       worst_holding_p95_minutes=Decimal("50"), ab_stability_ratio=Decimal(".8"), minimum_plateau_point_count=10, first_shift_bp=100),
        _selection_row("unranked", strategy_id=3, robust_pnl_30d_pct=None, worst_drawdown_pct=None,
                       worst_holding_p95_minutes=None, ab_stability_ratio=None, minimum_plateau_point_count=None, first_shift_bp=None, close_ma_len=None),
    ]), request).set_index("strategy_name")

    assert result.loc["near-tie", "eliminated_by_pareto_shift_near_tie"]
    assert result.loc["shift-winner", "final_rank"] == 1
    assert not result.loc["unranked", "finalist"]
    assert result.loc["unranked", "auto_status"] == "RESERVE"
    assert result.loc["unranked", "elimination_reason"] == "RANK_NOT_EVALUATED_INSUFFICIENT_DATA"


def test_disabled_final_ranker_is_inert() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": False, "scope": "pair_side", "top_n": 1},
    ]})
    result = run_selection(pd.DataFrame([_selection_row("only", strategy_id=1)]), request).set_index("strategy_name")

    assert result.loc["only", "finalist"]
    assert result.loc["only", "elimination_reason"] is None
    assert "final_rank" not in result


def test_shift_near_tie_is_bp_based_and_permutation_independent() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_shift_near_tie", "enabled": True, "scope": "pair_side_timeframe", "pnl_tolerance_pct": "10"},
    ]})
    rows = [
        _selection_row("shift-110bp", strategy_id=1, robust_pnl_30d_pct=Decimal("100"), first_shift_bp=110,
                       worst_drawdown_pct=Decimal("4"), worst_holding_p95_minutes=Decimal("40")),
        _selection_row("shift-100bp", strategy_id=2, robust_pnl_30d_pct=Decimal("95"), first_shift_bp=100,
                       worst_drawdown_pct=Decimal("4"), worst_holding_p95_minutes=Decimal("40")),
    ]
    first = run_selection(pd.DataFrame(rows), request).set_index("strategy_name")
    second = run_selection(pd.DataFrame(list(reversed(rows))), request).set_index("strategy_name")

    assert first.loc["shift-100bp", "eliminated_by_pareto_shift_near_tie"]
    assert first["eliminated_by_pareto_shift_near_tie"].to_dict() == second["eliminated_by_pareto_shift_near_tie"].to_dict()


def test_close_ma_near_tie_prefers_only_strictly_smaller_close_ma() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_close_ma_near_tie", "enabled": True, "scope": "pair_side_timeframe", "pnl_tolerance_pct": "10"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("smaller-close", strategy_id=1, robust_pnl_30d_pct=Decimal("95"), close_ma_len=3,
                       worst_drawdown_pct=Decimal("4"), worst_holding_p95_minutes=Decimal("40")),
        _selection_row("larger-close", strategy_id=2, robust_pnl_30d_pct=Decimal("100"), close_ma_len=5,
                       worst_drawdown_pct=Decimal("4"), worst_holding_p95_minutes=Decimal("40")),
        _selection_row("same-close", strategy_id=3, robust_pnl_30d_pct=Decimal("100"), close_ma_len=3,
                       worst_drawdown_pct=Decimal("4"), worst_holding_p95_minutes=Decimal("40")),
    ]), request).set_index("strategy_name")

    assert result.loc["larger-close", "eliminated_by_pareto_close_ma_near_tie"]
    assert not result.loc["smaller-close", "eliminated_by_pareto_close_ma_near_tie"]
    assert not result.loc["same-close", "eliminated_by_pareto_close_ma_near_tie"]


def test_final_rank_uses_only_prior_stage_survivors_and_renormalizes_weights() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_min_shift", "enabled": True, "scope": "pair_side_timeframe", "min_shift_pct": "0.3"},
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("filtered-top", strategy_id=1, order_1_shift_bp=20, first_shift_bp=300,
                       robust_pnl_30d_pct=Decimal("100"), worst_drawdown_pct=Decimal("1"), worst_holding_p95_minutes=Decimal("1"),
                       ab_stability_ratio=Decimal("1"), minimum_plateau_point_count=100),
        _selection_row("survivor", strategy_id=2, order_1_shift_bp=30, first_shift_bp=100,
                       robust_pnl_30d_pct=Decimal("10"), worst_drawdown_pct=Decimal("5"), worst_holding_p95_minutes=Decimal("50"),
                       ab_stability_ratio=Decimal(".5"), minimum_plateau_point_count=10),
    ]), request).set_index("strategy_name")

    assert result.loc["filtered-top", "eliminated_by_filter_min_shift"]
    assert result.loc["filtered-top", "elimination_reason"] == "FILTER_MIN_SHIFT"
    assert result.loc["survivor", "finalist"]
    assert result.loc["survivor", "final_rank"] == 1
    assert sum(result.loc["survivor", f"rank_weight_{name}"] for name in (
        "robust_pnl", "worst_drawdown", "ab_stability", "worst_holding",
        "first_shift", "minimum_plateau_points", "close_ma",
    )) == pytest.approx(1.0)


def test_final_rank_prefers_smaller_close_ma_with_approved_weight() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 2},
    ]})
    common = {
        "robust_pnl_30d_pct": Decimal("100"), "worst_drawdown_pct": Decimal("4"),
        "worst_holding_p95_minutes": Decimal("40"), "ab_stability_ratio": Decimal(".8"),
        "minimum_plateau_point_count": 20, "first_shift_bp": 100,
    }
    result = run_selection(pd.DataFrame([
        _selection_row("small-close", strategy_id=1, close_ma_len=3, **common),
        _selection_row("large-close", strategy_id=2, close_ma_len=7, **common),
    ]), request).set_index("strategy_name")

    assert result.loc["small-close", "final_rank"] == 1
    assert result.loc["large-close", "final_rank"] == 2
    assert result.loc["small-close", "rank_quality_close_ma"] == pytest.approx(1.0)
    assert result.loc["large-close", "rank_quality_close_ma"] == pytest.approx(0.0)
    assert result.loc["small-close", "rank_weight_close_ma"] == pytest.approx(0.09)
    assert [result.loc["small-close", f"rank_weight_{name}"] for name in (
        "robust_pnl", "worst_drawdown", "ab_stability", "first_shift", "minimum_plateau_points",
    )] == pytest.approx([0.30, 0.15, 0.15, 0.10, 0.09])


def test_final_rank_uses_approved_weights_including_worst_holding() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 20},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row(
            "only", strategy_id=1, robust_pnl_30d_pct=Decimal("10"), worst_drawdown_pct=Decimal("5"),
            ab_stability_ratio=Decimal(".8"), worst_holding_p95_minutes=Decimal("60"),
            first_shift_bp=100, minimum_plateau_point_count=20, close_ma_len=3,
        ),
    ]), request).iloc[0]

    assert [result[f"rank_weight_{name}"] for name in (
        "robust_pnl", "worst_drawdown", "ab_stability", "worst_holding",
        "first_shift", "minimum_plateau_points", "close_ma",
    )] == pytest.approx([.30, .15, .15, .12, .10, .09, .09])
    assert result["final_score"] == pytest.approx(100)


def test_final_rank_collapses_exact_analogs_before_top_n() -> None:
    request = parse_selection_request({"symbol": "BABAUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1},
    ]})
    common = {
        "symbol": "BABAUSDT", "side": "LONG", "timeframe": "1h", "order_count": 2,
            "close_ma_len": 3, "worst_drawdown_pct": Decimal("5"),
            "ab_stability_ratio": Decimal(".8"), "worst_holding_p95_minutes": Decimal("60"),
            "first_shift_bp": 100, "minimum_plateau_point_count": 20,
            "order_1_plateau_key": (77, 900), "order_2_plateau_key": (77, 901),
    }
    result = run_selection(pd.DataFrame([
        _selection_row("best-analog", strategy_id=5, robust_pnl_30d_pct=Decimal("20"), **common),
        _selection_row("other-analog", strategy_id=6, robust_pnl_30d_pct=Decimal("10"), **common),
        _selection_row("other-close", strategy_id=7, close_ma_len=5, robust_pnl_30d_pct=Decimal("15"),
                       **{key: value for key, value in common.items() if key != "close_ma_len"}),
    ]), request).set_index("strategy_name")

    assert result.loc["best-analog", "auto_status"] == "FINALIST"
    assert result.loc["other-analog", "auto_status"] == "ANALOG"
    assert result.loc["other-analog", "auto_analog_of_strategy_id"] == 5
    assert result.loc["other-close", "auto_status"] == "RESERVE"
    assert result["finalist"].sum() == 1


def test_final_rank_collapses_adjacent_close_ma_only_with_identical_order_plateaus() -> None:
    request = parse_selection_request({"symbol": "BABAUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 2},
    ]})
    common = {
        "symbol": "BABAUSDT", "side": "LONG", "timeframe": "1h", "order_count": 1,
        "worst_drawdown_pct": Decimal("5"), "ab_stability_ratio": Decimal(".8"),
        "worst_holding_p95_minutes": Decimal("60"), "first_shift_bp": 100,
        "minimum_plateau_point_count": 20, "order_1_plateau_key": (77, 900),
    }
    result = run_selection(pd.DataFrame([
        _selection_row("close-5", strategy_id=5, close_ma_len=5, robust_pnl_30d_pct=Decimal("20"), **common),
        _selection_row("close-6", strategy_id=6, close_ma_len=6, robust_pnl_30d_pct=Decimal("10"), **common),
        _selection_row("close-7", strategy_id=7, close_ma_len=7, robust_pnl_30d_pct=Decimal("15"), **common),
    ]), request).set_index("strategy_name")

    assert result.loc["close-5", "auto_status"] == "FINALIST"
    assert result.loc["close-6", "auto_status"] == "ANALOG"
    assert result.loc["close-6", "auto_analog_of_strategy_id"] == 5
    assert result.loc["close-7", "auto_status"] == "FINALIST"
    assert result.loc["close-7", "auto_analog_of_strategy_id"] is pd.NA


def test_prior_rejected_representative_does_not_consume_top_n_slot() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1},
    ]})
    common = {
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "order_count": 1,
        "worst_drawdown_pct": Decimal("5"), "ab_stability_ratio": Decimal(".8"),
        "worst_holding_p95_minutes": Decimal("60"), "first_shift_bp": 100,
        "minimum_plateau_point_count": 20,
    }
    result = run_selection(pd.DataFrame([
        _selection_row("rejected", strategy_id=1, close_ma_len=2, robust_pnl_30d_pct=Decimal("30"),
                       prior_rejected=True, **common),
        _selection_row("selected", strategy_id=2, close_ma_len=3, robust_pnl_30d_pct=Decimal("20"),
                       prior_rejected=False, **common),
        _selection_row("reserve", strategy_id=3, close_ma_len=4, robust_pnl_30d_pct=Decimal("10"),
                       prior_rejected=False, **common),
    ]), request).set_index("strategy_name")

    assert result.loc["rejected", "auto_status"] == "RESERVE"
    assert result.loc["rejected", "elimination_reason"] == "PRIOR_USER_REJECTED"
    assert result.loc["selected", "auto_status"] == "FINALIST"
    assert result.loc["reserve", "auto_status"] == "RESERVE"


def test_ranker_does_not_collapse_rows_with_missing_structural_key_or_dd5() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 2},
    ]})
    rows = pd.DataFrame([
        {"strategy_id": 1, "strategy_name": "one", "timeframe": "1h", "close_ma_len": 3,
         "robust_pnl_30d_pct": 20, "worst_drawdown_pct": 5},
        {"strategy_id": 2, "strategy_name": "two", "timeframe": "1h", "close_ma_len": 3,
         "robust_pnl_30d_pct": 10, "worst_drawdown_pct": 6},
    ])

    result = run_selection(rows, request).set_index("strategy_id")

    assert result["auto_status"].to_dict() == {1: "FINALIST", 2: "FINALIST"}
    assert result["auto_analog_of_strategy_id"].isna().all()


def test_final_rank_breaks_boundary_ties_by_strategy_id_independent_of_input_order() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1},
    ]})
    facts = {
            "robust_pnl_30d_pct": Decimal("10"), "worst_drawdown_pct": Decimal("5"),
            "ab_stability_ratio": Decimal(".5"), "first_shift_bp": 100,
            "minimum_plateau_point_count": 10, "close_ma_len": 3,
            "order_1_plateau_key": (77, 900),
    }
    rows = [_selection_row("two", strategy_id=2, **facts), _selection_row("one", strategy_id=1, **facts)]

    first = run_selection(pd.DataFrame(rows), request).set_index("strategy_id")
    second = run_selection(pd.DataFrame(list(reversed(rows))), request).set_index("strategy_id")

    for result in (first, second):
        assert result.loc[1, "finalist"]
        assert result.loc[1, "final_rank"] == 1
        assert result.loc[2, "elimination_reason"] == "ANALOG"
        assert result.loc[2, "auto_analog_of_strategy_id"] == 1


def test_workbook_keeps_rank_eliminated_rows_with_rank_diagnostics(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("top", strategy_id=1, close_ma_len=2, robust_pnl_30d_pct=Decimal("20"), worst_drawdown_pct=Decimal("4"),
                       worst_holding_p95_minutes=Decimal("40"), ab_stability_ratio=Decimal(".8"), minimum_plateau_point_count=20),
        _selection_row("cut", strategy_id=2, close_ma_len=3, robust_pnl_30d_pct=Decimal("10"), worst_drawdown_pct=Decimal("5"),
                       worst_holding_p95_minutes=Decimal("50"), ab_stability_ratio=Decimal(".7"), minimum_plateau_point_count=10),
    ]), request)
    book = load_workbook(write_selection_workbook(result, tmp_path / "ranked.xlsx", request), data_only=True)
    sheet = book["All candidates"]
    headers = [cell.value for cell in sheet[1]]

    assert sheet.max_row == 3
    assert "Final rank" in headers and "Rank coverage, %" in headers
    assert headers[-1] == "eliminated_by_rank_robust_top_n"
    assert {sheet.cell(row, headers.index("Final rank") + 1).value for row in (2, 3)} == {1, 2}


def test_equity_workbook_appends_four_precise_columns_and_labels_method(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    result = pd.DataFrame([
        {"strategy_id": 1, "strategy_name": "ranked", "finalist": True, "final_score": Decimal("0.123456789"),
         "final_rank": 1, "equity_state": "GROWING", "equity_basis": "28d / OK",
         "equity_dd_pct": Decimal("12.3456789"), "equity_smoothness": Decimal("0.9876543")},
        {"strategy_id": 2, "strategy_name": "excluded", "finalist": False, "final_score": None,
         "final_rank": None, "elimination_reason": "RANK_ROBUST_TOP_N", "equity_state": "FLAT",
         "equity_basis": "28d / OK", "equity_dd_pct": Decimal("3.1415926"),
         "equity_smoothness": Decimal("0.1234567")},
    ])
    path = write_selection_workbook(result, tmp_path / "equity.xlsx", request, {"snapshot_id": "one"})
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = [cell.value for cell in sheet[1]]

    assert headers[-4:] == ["Equity state", "Equity basis", "Equity DD, %", "Equity smoothness"]
    assert sheet.max_row == 3
    assert sheet.cell(2, headers.index("Equity DD, %") + 1).value == 12.3456789
    assert sheet.cell(3, headers.index("Equity smoothness") + 1).value == 0.1234567
    score_header = sheet.cell(1, headers.index("Final score (Pair+Side)") + 1)
    assert score_header.comment is not None and "equity_quality_v1" in score_header.comment.text
    metadata_sheet = workbook["_MRS_SELECTION_META"]
    assert ("selection_method", "equity_quality_v1") in {tuple(row) for row in metadata_sheet.iter_rows(min_row=1, max_col=2, values_only=True)}


def test_equity_method_metadata_without_review_preserves_existing_score_comment(
    tmp_path: Path, monkeypatch,
) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "rank_robust_top_n", "enabled": True, "scope": "pair_side", "top_n": 1,
         "method": "equity_quality_v1"},
    ]})
    original_writer = selection_module.write_audit_workbook

    def write_with_existing_comment(*args, **kwargs):
        path = original_writer(*args, **kwargs)
        workbook = load_workbook(path)
        sheet = workbook["All candidates"]
        headers = {cell.value: cell.column for cell in sheet[1]}
        sheet.cell(1, headers["Final score (Pair+Side)"]).comment = Comment("Existing score note", "Analyst")
        workbook.save(path)
        return path

    monkeypatch.setattr(selection_module, "write_audit_workbook", write_with_existing_comment)
    path = write_selection_workbook(pd.DataFrame([{
        "strategy_id": 1, "strategy_name": "ranked", "finalist": True,
        "final_score": Decimal("0.123456789"),
    }]), tmp_path / "equity-no-review.xlsx", request, review_metadata=None)
    workbook = load_workbook(path)
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    comment = sheet.cell(1, headers["Final score (Pair+Side)"]).comment

    assert comment is not None
    assert "Existing score note" in comment.text
    assert "equity_quality_v1" in comment.text
    metadata_sheet = workbook["_MRS_SELECTION_META"]
    assert ("selection_method", "equity_quality_v1") in {
        tuple(row) for row in metadata_sheet.iter_rows(min_row=1, max_col=2, values_only=True)
    }


def test_equity_workbook_rejects_fresh_facts_missing_their_horizon_window() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    facts = calculate_equity_quality_facts(101, start, start + timedelta(days=8), (
        EquitySample(101, 0, start, Decimal("100")),
        EquitySample(101, 1, start + timedelta(days=8), Decimal("110")),
    ))
    malformed = replace(facts, windows=())

    values = selection_module._equity_workbook_values({"status": "FRESH", "facts": malformed})

    assert values["equity_basis"] == "INVALID"
    assert values["equity_state"] is None
    assert values["equity_smoothness"] is None


def test_equity_workbook_maps_cached_facts_positionally_with_duplicate_dataframe_index(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    first = _equity_rank_facts(101, 0, "1", "0.1", "0.2", 28)
    second = _equity_rank_facts(102, 1, "2", "0.2", "0.3", 14, state="WEAKENING")
    result = pd.DataFrame([
        {"strategy_id": 1, "strategy_name": "first", "finalist": True,
         "_equity_cache": {"status": "FRESH", **first}},
        {"strategy_id": 2, "strategy_name": "second", "finalist": True,
         "_equity_cache": {"status": "FRESH", **second}},
    ], index=[7, 7])

    sheet = load_workbook(
        write_selection_workbook(result, tmp_path / "duplicate-index.xlsx", request), data_only=True,
    )["All candidates"]
    headers = [cell.value for cell in sheet[1]]

    assert sheet.cell(2, headers.index("Equity state") + 1).value == first["facts"].state
    assert sheet.cell(3, headers.index("Equity state") + 1).value == second["facts"].state


@pytest.mark.parametrize(("stage_id", "field", "values"), [
    ("filter_holding_outlier", "holding_p95_minutes", [10, 10, 10, 100]),
    ("filter_low_trades", "trades_30d", [100, 100, 100, 1]),
])
def test_iqr_filters_eliminate_only_outlier(stage_id: str, field: str, values: list[int]) -> None:
    rows = [_selection_row(f"row-{index}", strategy_id=index, **{field: value}) for index, value in enumerate(values)]
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": stage_id, "enabled": True, "scope": "pair_side"},
    ]})

    result = run_selection(pd.DataFrame(rows), request).set_index("strategy_name")

    assert result.loc["row-3", f"eliminated_by_{stage_id}"]
    assert result["finalist"].sum() == 3


def test_conditional_close_ma_needs_more_than_three_survivors() -> None:
    rows = [_selection_row(f"row-{index}", strategy_id=index, close_ma_len=3 + index,
                           capital_efficiency=Decimal(10 - index)) for index in range(4)]
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_conditional_close_ma", "enabled": True, "scope": "pair_side"},
    ]})

    result = run_selection(pd.DataFrame(rows), request)

    assert result["eliminated_by_pareto_conditional_close_ma"].sum() == 3


def test_workbook_keeps_all_candidates_and_ab_30d_columns(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_dd5_capital", "enabled": True, "scope": "pair_side"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row(
            "winner", ab_return_a_30d_pct=Decimal("10.75"), ab_return_b_30d_pct=Decimal("4.25"), ab_calendar_days_a=Decimal("31"), ab_calendar_days_b=Decimal("14"), ab_pnl_change_30d_pct=Decimal("1.6"),
            total_pnl_pct=Decimal("12.6"), pnl_30d_pct=Decimal("8.5"), dd5_proxy=Decimal("5.5"), profit_factor=Decimal("1.6"),
            capital_efficiency=Decimal("10.6"), win_rate_pct=Decimal("67.8"), pnl_without_best_trade_pct=Decimal("5.6"),
            holding_p95_minutes=Decimal("10.7"), holding_median_minutes=Decimal("5.6"),
                positive_quarter_count=3, positive_quarter_available_count=3, trades_30d=Decimal("3.75"),
        ),
        _selection_row("loser", ab_return_b_30d_pct=Decimal("2.50")),
    ]), request)
    result["final_rank"] = [1, 2]
    result.loc[1, "elimination_reason"] = "PARETO_PLATEAU_POINTS_PER_ORDER"
    result.loc[0, "order_1_plateau_point_count"] = 21.0
    result.loc[0, "order_2_plateau_point_count"] = 33.0
    result.loc[0, "order_3_plateau_point_count"] = None

    path = write_selection_workbook(result, tmp_path / "finalists.xlsx", request)
    book = load_workbook(path, data_only=True)
    headers = [cell.value for cell in book["All candidates"][1]]

    assert "PnL" not in headers
    assert "total_pnl_pct" not in headers
    assert "PnL/30" in headers and "Trades/30" in headers
    assert "eliminated_by_filter_lot_variant_redundancy" in headers
    assert "positive_quarter_status" not in headers
    strategy_column = headers.index("Стратегия") + 1
    winner_row = next(row for row in range(2, book["All candidates"].max_row + 1) if book["All candidates"].cell(row, strategy_column).value == "winner")
    assert book["All candidates"].cell(winner_row, headers.index("PnL/30") + 1).value == 9
    assert headers[:27] == [
        "ID", "Стратегия", "Пара", "Side", "ТФ", "Start", "End", "ORD", "Close", "PnL/30", "PnL DD5/30",
        "∆ PnL A/B", "PnL A/30д, %", "Дней A", "PnL B/30д, %", "Дней B", "Positive windows", "CE", "PF", "DD", "W/R", "Trades", "Trades/30", "Lot DD5", "Hold p95", "Hold M", "PointsALL",
    ]
    assert "Shift 1" not in headers
    strategy_column = headers.index("Стратегия") + 1
    assert book["All candidates"].column_dimensions[get_column_letter(strategy_column)].hidden
    data_rows = {
        book["All candidates"].cell(row, strategy_column).value: row
        for row in range(2, book["All candidates"].max_row + 1)
    }
    winner_row = data_rows["winner"]
    loser_row = data_rows["loser"]
    a_column = headers.index("PnL A/30д, %") + 1
    b_column = headers.index("PnL B/30д, %") + 1
    assert book["All candidates"].cell(winner_row, a_column).value == 11
    assert {book["All candidates"].cell(row, b_column).value for row in (winner_row, loser_row)} == {3, 4}
    assert book["All candidates"].cell(winner_row, b_column).data_type == "n"
    for header in ("Дней A", "Дней B"):
        column = headers.index(header) + 1
        cell = book["All candidates"].cell(winner_row, column)
        assert cell.data_type == "n"
        assert cell.alignment.horizontal == "center"
        assert cell.font.color.type == "rgb"
        assert cell.font.color.rgb == "FF0000FF"
        assert book["All candidates"].column_dimensions[get_column_letter(column)].width == 6
    assert book["All candidates"].cell(winner_row, headers.index("Positive windows") + 1).value == "3/3"
    trades_30_column = headers.index("Trades/30") + 1
    assert book["All candidates"].cell(winner_row, trades_30_column).value == 3.75
    assert book["All candidates"].cell(winner_row, trades_30_column).data_type == "n"
    pnl_without_best_column = headers.index("PnL without best, %") + 1
    assert {book["All candidates"].cell(row, pnl_without_best_column).value for row in (2, 3)} == {6, None}
    assert book["All candidates"].cell(3, pnl_without_best_column).data_type == "n"
    assert book["All candidates"].cell(3, headers.index("Причина") + 1).value == "PARETO_PL_PTS_PER_ORDER"
    assert book["All candidates"].cell(3, headers.index("Причина") + 1).alignment.horizontal == "left"
    for header in ("PnL/30", "PnL DD5/30", "PF", "PnL A/30д, %", "PnL B/30д, %", "PnL without best, %"):
        assert book["All candidates"].cell(2, headers.index(header) + 1).number_format == "0"
    for header in (
        "Positive trades", "Robust PnL/30", "Worst DD", "Worst Hold p95", "A/B stability", "Rank q PnL", "Rank q DD",
        "Rank q A/B", "Rank q Shift", "Rank q Points", "Rank coverage, %", "Rank w PnL",
        "Rank w DD", "Rank w A/B", "Rank w Shift", "Rank w Points", "Rank w Close MA",
        "Rank q Close MA", "Final score (Pair+Side)", "Best trade, %", "PnL without best, %",
    ):
        assert book["All candidates"].column_dimensions[get_column_letter(headers.index(header) + 1)].hidden
    assert not book["All candidates"].column_dimensions[get_column_letter(headers.index("Final rank") + 1)].hidden
    assert headers.index("Final rank") + 1 == headers.index("Final")
    for header in ("Close", "DD", "Hold p95", "1 Shift", "Final rank"):
        assert book["All candidates"].cell(2, headers.index(header) + 1).font.bold
    assert headers.index("ORD") + 1 == headers.index("Close")
    for header, edge in (
        ("Positive windows", "right"),
        ("Hold p95", "left"),
        ("Hold M", "right"),
        ("1 Shift", "left"),
        ("4 Shift", "right"),
        ("Points", "left"),
        ("Points", "right"),
        ("MA", "left"),
        ("MA", "right"),
        ("Final rank", "left"),
        ("Final rank", "right"),
        ("Close", "left"),
        ("Close", "right"),
    ):
        assert getattr(book["All candidates"].cell(1, headers.index(header) + 1).border, edge).style == "double"
    assert headers.index("PointsALL") + 1 == headers.index("PointsMin")
    assert headers.index("CE") + 1 == headers.index("PF")
    shifts_start = headers.index("1 Shift")
    assert headers[shifts_start:shifts_start + 7] == [
        "1 Shift", "2 Shift", "3 Shift", "4 Shift", "Lots", "Points", "MA",
    ]
    first_order_shift = headers.index("1 Shift") + 1
    assert {book["All candidates"].cell(row, first_order_shift).value for row in (2, 3)} == {0.3, 2.7}
    assert book["All candidates"].cell(2, first_order_shift).number_format == "0.0"
    assert book["All candidates"].cell(2, headers.index("Final rank") + 1).number_format == "0"
    assert book["All candidates"].cell(winner_row, headers.index("∆ PnL A/B") + 1).value == 2
    assert "1 Points" not in headers
    assert {book["All candidates"].cell(row, headers.index("Points") + 1).value for row in (2, 3)} == {
        "20 / 8 / 8 / 10", "21 / 33 / - / 10",
    }
    assert {book["All candidates"].cell(row, headers.index("MA") + 1).value for row in (2, 3)} == {"4 / 4 / 6 / 6"}
    assert {book["All candidates"].cell(row, headers.index("Lots") + 1).value for row in (2, 3)} == {"25 / 50 / 75 / 100"}
    assert book["All candidates"].column_dimensions[get_column_letter(headers.index("1 Shift") + 1)].width == 5
    assert book["All candidates"].column_dimensions[get_column_letter(headers.index("PF") + 1)].width == 5
    assert book["All candidates"].column_dimensions[get_column_letter(headers.index("Lots") + 1)].width == 20
    assert book["All candidates"].column_dimensions[get_column_letter(headers.index("MA") + 1)].width == 15
    assert book["All candidates"].cell(2, 4).alignment.horizontal is None
    assert book["All candidates"].cell(2, 5).alignment.horizontal == "center"
    assert book["Finalists"].cell(2, 5).alignment.horizontal == "center"

    assert book.sheetnames == ["All candidates", "Finalists"]
    assert headers[-1] == "eliminated_by_pareto_dd5_capital"
    assert {book["All candidates"].cell(row, len(headers)).value for row in (2, 3)} == {"BLOCK", "PASS"}
    assert "result_id" not in headers
    assert "total_pnl" not in headers
    assert book["All candidates"].max_row == 3
    assert book["Finalists"].max_row == 2


def test_workbook_prefixes_applied_filter_reason_and_fills_rows(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_plateau_points_per_order", "enabled": True, "scope": "pair_side"},
        {"id": "pareto_dd5_capital", "enabled": True, "scope": "pair_side"},
    ]})
    result = pd.DataFrame([_selection_row("winner"), _selection_row("loser")])
    result["finalist"] = [False, True]
    result["elimination_reason"] = ["PARETO_DD5_CAPITAL", None]

    book = load_workbook(write_selection_workbook(result, tmp_path / "finalists.xlsx", request), data_only=True)
    headers = [cell.value for cell in book["All candidates"][1]]

    assert book["All candidates"].cell(2, headers.index("Причина") + 1).value == "2. PARETO_DD5_CAPITAL"
    assert book["All candidates"].cell(2, 1).fill.fgColor.rgb == "00FAEFEF"
    assert book["All candidates"].cell(3, 1).fill.fgColor.rgb == "00D9EAD3"
    assert book["Finalists"].cell(2, 1).fill.fgColor.rgb == "00D9EAD3"


def test_workbook_keeps_advisory_reasons_visible_on_legacy_and_equity_finalists(tmp_path: Path) -> None:
    legacy_request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "ab_deterioration", "enabled": True, "scope": "pair_side"},
    ]})
    legacy = run_selection(pd.DataFrame([_selection_row("legacy-finalist")]), legacy_request)
    equity_request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"},
    ]})
    equity = run_selection(pd.DataFrame([
        _lot_variant_row("blocked-old-winner", 1, lots=("1", "2"),
                         _equity_state="FLAT", _equity_disposition="BLOCK_IF_ERF_ENABLED",
                         _equity_reason="H_FLAT"),
        _lot_variant_row("equity-finalist", 2, lots=("3", "4"),
                         _equity_state="GROWING", _equity_disposition="PASS",
                         _equity_reason="H_UP_SHORTS_NONDECLINING"),
    ]), equity_request)

    legacy_book = load_workbook(
        write_selection_workbook(legacy, tmp_path / "legacy.xlsx", legacy_request), data_only=True,
    )
    equity_book = load_workbook(
        write_selection_workbook(equity, tmp_path / "equity.xlsx", equity_request), data_only=True,
    )
    for book, sheet_name, strategy_name, expected_reason in (
        (legacy_book, "All candidates", "legacy-finalist", "AB_NOT_EVALUATED_INSUFFICIENT_DATA"),
        (equity_book, "All candidates", "equity-finalist", "LOT_GROUP_EQUITY_BLOCKED"),
    ):
        sheet = book[sheet_name]
        headers = [cell.value for cell in sheet[1]]
        strategy_column, finalist_column = headers.index("Стратегия") + 1, headers.index("Final") + 1
        reason_column = headers.index("Причина") + 1
        row = next(
            row for row in range(2, sheet.max_row + 1)
            if sheet.cell(row, strategy_column).value == strategy_name
        )
        assert sheet.cell(row, finalist_column).value is True
        assert sheet.cell(row, reason_column).value == expected_reason
        assert sheet.cell(row, 1).fill.fgColor.rgb == "00D9EAD3"


def test_workbook_keeps_only_enabled_filter_columns_in_request_order(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "filter_low_trades", "enabled": False, "scope": "pair_side"},
        {"id": "pareto_dd5_capital", "enabled": True, "scope": "pair_side"},
        {"id": "ab_deterioration", "enabled": False, "scope": "pair_side"},
        {"id": "pareto_dd5_balanced", "enabled": True, "scope": "pair_side"},
    ]})
    result = run_selection(pd.DataFrame([_selection_row("winner"), _selection_row("loser")]), request)

    path = write_selection_workbook(result, tmp_path / "finalists.xlsx", request)
    headers = [cell.value for cell in load_workbook(path, data_only=True)["All candidates"][1]]

    assert headers[-2:] == ["eliminated_by_pareto_dd5_capital", "eliminated_by_pareto_dd5_balanced"]
    assert "eliminated_by_filter_low_trades" not in headers
    assert "eliminated_by_ab_deterioration" not in headers


def test_workbook_consolidated_ma_never_shows_decimal_places(tmp_path: Path) -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    result = run_selection(pd.DataFrame([_selection_row(
        "sparse", order_1_open_ma_len=7.0, order_2_open_ma_len=3.0,
        order_3_open_ma_len=None, order_4_open_ma_len=None,
    )]), request)

    book = load_workbook(write_selection_workbook(result, tmp_path / "sparse.xlsx", request), data_only=True)
    headers = [cell.value for cell in book["All candidates"][1]]

    assert book["All candidates"].cell(2, headers.index("MA") + 1).value == "7 / 3"


def test_scope_timeframe_prevents_cross_timeframe_pareto_comparison() -> None:
    frame = pd.DataFrame([
        _selection_row("winner", timeframe="1h"),
        _selection_row("loser", timeframe="3h"),
    ])
    pair_side = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_dd5_capital", "enabled": True, "scope": "pair_side"},
    ]})
    timeframe = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_dd5_capital", "enabled": True, "scope": "pair_side_timeframe"},
    ]})

    all_scope = run_selection(frame, pair_side).set_index("strategy_name")
    split_scope = run_selection(frame, timeframe).set_index("strategy_name")

    assert not all_scope.loc["loser", "finalist"]
    assert split_scope["finalist"].all()


def test_stage_order_changes_survivors_and_keeps_first_elimination_trace() -> None:
    failing_dominator = _selection_row(
        "a", dd5_proxy=Decimal("10"), capital_proxy=Decimal("1"), ab_return_a_30d_pct=Decimal("10"), ab_return_b_30d_pct=Decimal("4"),
        ab_win_rate_b_pct=Decimal("60"), ab_trade_rate_a_30d=Decimal("10"), ab_trade_rate_b_30d=Decimal("10"),
    )
    passing_dominated = _selection_row(
        "b", strategy_id=3, dd5_proxy=Decimal("5"), capital_proxy=Decimal("2"),
        ab_return_a_30d_pct=Decimal("10"), ab_return_b_30d_pct=Decimal("10"),
        ab_win_rate_b_pct=Decimal("60"), ab_trade_rate_a_30d=Decimal("10"), ab_trade_rate_b_30d=Decimal("10"),
    )
    ab_first = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "ab_deterioration", "enabled": True, "scope": "pair_side"},
        {"id": "pareto_dd5_capital", "enabled": True, "scope": "pair_side"},
    ]})
    pareto_first = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": stage.id, "enabled": stage.enabled, "scope": stage.scope}
        for stage in reversed(ab_first.stages)
    ]})

    first = run_selection(pd.DataFrame([failing_dominator, passing_dominated]), ab_first).set_index("strategy_name")
    second = run_selection(pd.DataFrame([failing_dominator, passing_dominated]), pareto_first).set_index("strategy_name")

    assert first.index[first["finalist"]].tolist() == ["b"]
    assert second.index[second["finalist"]].tolist() == []
    assert first.loc["a", "eliminated_by_ab_deterioration"]
    assert not first.loc["a", "eliminated_by_pareto_dd5_capital"]
    assert second.loc["b", "eliminated_by_pareto_dd5_capital"]
    assert not second.loc["b", "eliminated_by_ab_deterioration"]
    assert second.loc["a", "eliminated_by_ab_deterioration"]
    assert not second.loc["a", "eliminated_by_pareto_dd5_capital"]


def test_stage_counts_follow_the_applied_stage_order() -> None:
    failing_dominator = _selection_row(
        "a", dd5_proxy=Decimal("10"), capital_proxy=Decimal("1"), ab_return_a_30d_pct=Decimal("10"), ab_return_b_30d_pct=Decimal("4"),
        ab_win_rate_b_pct=Decimal("60"), ab_trade_rate_a_30d=Decimal("10"), ab_trade_rate_b_30d=Decimal("10"),
    )
    passing_dominated = _selection_row(
        "b", strategy_id=3, dd5_proxy=Decimal("5"), capital_proxy=Decimal("2"),
        ab_return_a_30d_pct=Decimal("10"), ab_return_b_30d_pct=Decimal("10"),
        ab_win_rate_b_pct=Decimal("60"), ab_trade_rate_a_30d=Decimal("10"), ab_trade_rate_b_30d=Decimal("10"),
    )
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "ab_deterioration", "enabled": True, "scope": "pair_side"},
        {"id": "pareto_dd5_capital", "enabled": True, "scope": "pair_side"},
        {"id": "pareto_dd5_balanced", "enabled": False, "scope": "pair_side"},
    ]})

    result = run_selection(pd.DataFrame([failing_dominator, passing_dominated]), request)

    assert result.attrs["stage_counts"] == {
        "ab_deterioration": {"enabled": True, "eliminated": 1, "remaining": 1},
        "pareto_dd5_capital": {"enabled": True, "eliminated": 0, "remaining": 1},
        "pareto_dd5_balanced": {"enabled": False, "eliminated": 0, "remaining": 1},
    }


def test_missing_pareto_objective_neither_dominates_nor_is_eliminated() -> None:
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": [
        {"id": "pareto_dd5_capital", "enabled": True, "scope": "pair_side"},
    ]})
    result = run_selection(pd.DataFrame([
        _selection_row("missing", capital_proxy=None), _selection_row("complete"),
    ]), request)

    assert result["finalist"].all()
    assert not result["eliminated_by_pareto_dd5_capital"].any()
