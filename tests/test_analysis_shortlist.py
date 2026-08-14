from __future__ import annotations

import json
from decimal import Decimal

import duckdb
import pytest


def _candidate(candidate_id: str, structure_id: str, orders: list[dict[str, object]], **extra: object) -> tuple[str, str, str, str]:
    payload = {
        "structure_id": structure_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "common_close_ma": 9,
        "order_count": len(orders),
        "status": "READY_MRS3_STRUCTURE",
        "orders": orders,
        **extra,
    }
    return candidate_id, "R1", "S1", json.dumps(payload)


def _order(point_id: str, pnl: float, efficiency: float, support: float) -> dict[str, object]:
    return {
        "point_id": point_id,
        "source_pnl_pct": pnl,
        "source_efficiency": efficiency,
        "close_support": support,
    }


def _connection(
    *candidates: tuple[str, str, str, str],
    events: dict[str, tuple[str, ...]],
    event_mode: str = "real_independent_events",
) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute("create table analysis_runs(run_id varchar, surface_id varchar, algorithm_version varchar)")
    connection.execute("create table surfaces(surface_id varchar, event_mode varchar)")
    connection.execute("create table surface_points(surface_id varchar, canonical_point_key varchar, point_event_count bigint)")
    connection.execute("create table candidates(candidate_id varchar, run_id varchar, surface_id varchar, candidate_json varchar)")
    connection.execute("create table surface_point_events(surface_id varchar, canonical_point_key varchar, event_id varchar)")
    connection.execute("insert into analysis_runs values ('R1', 'S1', 'v1')")
    connection.execute("insert into surfaces values ('S1', ?)", [event_mode])
    connection.executemany("insert into candidates values (?, ?, ?, ?)", candidates)
    event_rows = [(point_id, event_id) for point_id, event_ids in events.items() for event_id in event_ids]
    if event_rows:
        connection.executemany("insert into surface_point_events values ('S1', ?, ?)", event_rows)
    point_rows = [(point_id, len(set(event_ids))) for point_id, event_ids in events.items()]
    if point_rows:
        connection.executemany("insert into surface_points values ('S1', ?, ?)", point_rows)
    return connection


def _row(result, candidate_id: str) -> dict[str, object]:
    return next(row for row in result.rows if row["candidate_id"] == candidate_id)


def test_filter_uses_structural_group_and_ordered_vectors_without_summing_pnl() -> None:
    candidates = [
        _candidate("C_A", "STR_A", [_order("P1", 100, 10, 0.9), _order("P2", 1, 1, 0.8)]),
        _candidate("C_B", "STR_B", [_order("P3", 50, 9, 0.85), _order("P4", 50, 2, 0.7)]),
        _candidate("C_OTHER", "STR_OTHER", [_order("P5", 1, 1, 0.1), _order("P6", 1, 1, 0.1)]),
    ]
    connection = _connection(
        *candidates,
        events={"P1": ("e1", "e2"), "P2": ("e3",), "P3": ("e1", "e2"), "P4": ("e3",), "P5": ("x1",), "P6": ("x2",)},
    )

    from mrs3.analysis_shortlist import filter_analysis_candidates

    result = filter_analysis_candidates(connection, "R1", {"source_pnl": True})

    assert _row(result, "C_A")["filter_status"] == "READY_AFTER_FILTERS"
    assert _row(result, "C_B")["filter_status"] == "READY_AFTER_FILTERS"
    assert _row(result, "C_OTHER")["filter_status"] == "DEFERRED_REDUNDANT"
    assert {row["candidate_id"] for row in result.standalone["source_pnl"]} == {"C_OTHER"}
    assert {row["candidate_id"] for row in result.combined} == {"C_OTHER"}


def test_filter_returns_standalone_and_combined_audit_rows_with_deterministic_dominator() -> None:
    candidates = [
        _candidate("C_B", "STR_B", [_order("P3", 9, 9, 0.9), _order("P4", 19, 9, 0.9)]),
        _candidate("C_Z", "STR_Z", [_order("P5", 10, 10, 0.91), _order("P6", 20, 10, 0.91)]),
        _candidate("C_A", "STR_A", [_order("P1", 10, 10, 0.91), _order("P2", 20, 10, 0.91)]),
    ]
    connection = _connection(
        *candidates,
        events={"P1": ("a1",), "P2": ("a2",), "P3": ("b1",), "P4": ("b2",), "P5": ("z1",), "P6": ("z2",)},
    )

    from mrs3.analysis_shortlist import filter_analysis_candidates

    result = filter_analysis_candidates(connection, "R1", {"source_pnl": True, "efficiency": True, "close_support": True})

    assert _row(result, "C_B")["filter_status"] == "DEFERRED_REDUNDANT"
    assert _row(result, "C_B")["deferred_by"] == "STR_A"
    assert _row(result, "C_B")["deferred_by_candidate_id"] == "C_A"
    assert result.standalone["source_pnl"][0]["candidate_id"] == "C_B"
    assert result.standalone["efficiency"][0]["candidate_id"] == "C_B"
    assert result.standalone["close_support"][0]["candidate_id"] == "C_B"
    assert result.combined[0]["candidate_id"] == "C_B"
    assert result.combined[0]["criterion"] == "source_pnl,efficiency,close_support"
    assert result.combined[0]["defer_reason"] == "SAME_STRUCTURE_DOMINATED"
    assert "comparison_key" in result.combined[0]
    assert "behavior_key" not in result.combined[0]
    assert result.combined[0]["a_values"]["source_pnl"] == [Decimal("10"), Decimal("20")]
    assert result.combined[0]["b_values"]["source_pnl"] == [Decimal("9"), Decimal("19")]


def test_filter_requires_one_common_dominator_and_point_events_can_dominate() -> None:
    candidates = [
        _candidate("C_A", "STR_A", [_order("P1", 10, 8, 0.9), _order("P2", 10, 8, 0.9)]),
        _candidate("C_B", "STR_B", [_order("P3", 9, 9, 0.8), _order("P4", 9, 9, 0.8)]),
        _candidate("C_C", "STR_C", [_order("P5", 8, 7, 0.7), _order("P6", 8, 7, 0.7)]),
    ]
    connection = _connection(
        *candidates,
        events={"P1": ("a1", "a2"), "P2": ("a3", "a4"), "P3": ("b1",), "P4": ("b2",), "P5": ("c1",), "P6": ("c2",)},
    )

    from mrs3.analysis_shortlist import filter_analysis_candidates

    tradeoff = filter_analysis_candidates(connection, "R1", {"source_pnl": True, "efficiency": True})
    event_only = filter_analysis_candidates(connection, "R1", {"point_event_count": True})

    assert _row(tradeoff, "C_A")["filter_status"] == "READY_AFTER_FILTERS"
    assert _row(tradeoff, "C_B")["filter_status"] == "READY_AFTER_FILTERS"
    assert _row(tradeoff, "C_C")["filter_status"] == "DEFERRED_REDUNDANT"
    assert {row["candidate_id"] for row in event_only.combined} == {"C_B", "C_C"}
    assert _row(event_only, "C_A")["filter_status"] == "READY_AFTER_FILTERS"
    assert _row(event_only, "C_B")["filter_status"] == "DEFERRED_REDUNDANT"
    assert len(event_only.standalone["point_event_count"]) == 2


def test_filter_without_criteria_marks_all_ready_and_does_not_mutate_analysis_run() -> None:
    candidate = _candidate("C1", "STR_1", [_order("P1", 10, 5, 0.9)])
    connection = _connection(*[candidate], events={"P1": ("e1",)})
    before = connection.execute("select candidate_json from candidates").fetchall()

    from mrs3.analysis_shortlist import filter_analysis_candidates

    result = filter_analysis_candidates(connection, "R1", {})

    assert result.criteria == ()
    assert result.standalone == {}
    assert result.combined == ()
    assert _row(result, "C1")["filter_status"] == "READY_AFTER_FILTERS"
    assert _row(result, "C1")["enabled_criteria"] == []
    assert result.input_count == 1
    assert result.ready_count == 1
    assert result.deferred_count == 0
    assert result.comparison_group_count == 1
    assert result.comparable_count == 0
    assert _row(result, "C1")["comparison_group_size"] == 1
    assert connection.execute("select candidate_json from candidates").fetchall() == before
    assert connection.execute("select * from analysis_runs").fetchall() == [("R1", "S1", "v1")]


def test_filter_rows_preserve_candidate_payload_for_panel_and_strategy_consumers() -> None:
    candidate = _candidate("C1", "STR_1", [_order("P1", 10, 5, 0.9)], custom_label="keep-me")
    connection = _connection(*[candidate], events={"P1": ("e1",)})

    from mrs3.analysis_shortlist import filter_analysis_candidates

    result = filter_analysis_candidates(connection, "R1", {})

    row = result.rows[0]
    assert row["symbol"] == "BTCUSDT"
    assert row["timeframe"] == "1h"
    assert row["order_count"] == 1
    assert row["orders"] == [_order("P1", 10, 5, 0.9)]
    assert row["custom_label"] == "keep-me"
    assert row["candidate_id"] == "C1"
    assert row["filter_status"] == "READY_AFTER_FILTERS"


def test_filter_fails_closed_for_legacy_enabled_criteria_and_missing_real_event_membership() -> None:
    from mrs3.analysis_shortlist import filter_analysis_candidates

    legacy = _connection(
        _candidate("C1", "STR_1", [_order("P1", 10, 5, 0.9)]),
        _candidate("C2", "STR_2", [_order("P1", 9, 4, 0.8)]),
        events={},
        event_mode="legacy_trades_proxy",
    )
    with pytest.raises(ValueError, match="legacy"):
        filter_analysis_candidates(legacy, "R1", {"source_pnl": True})
    legacy_result = filter_analysis_candidates(legacy, "R1", {})
    assert all(row["filter_status"] == "READY_AFTER_FILTERS" for row in legacy_result.rows)
    assert len({row["comparison_key"] for row in legacy_result.rows}) == 2

    missing = _connection(
        _candidate("C1", "STR_1", [_order("P1", 10, 5, 0.9)]),
        events={},
    )
    with pytest.raises(ValueError, match="event membership"):
        filter_analysis_candidates(missing, "R1", {"point_event_count": True})


def test_filter_rejects_incomplete_event_membership_and_unknown_event_mode() -> None:
    from mrs3.analysis_shortlist import filter_analysis_candidates

    incomplete = _connection(
        _candidate("C1", "STR_1", [_order("P1", 10, 5, 0.9)]),
        events={"P1": ("e1",)},
    )
    incomplete.execute(
        "update surface_points set point_event_count=2 where canonical_point_key='P1'"
    )
    with pytest.raises(ValueError, match="event membership count"):
        filter_analysis_candidates(incomplete, "R1", {"source_pnl": True})

    unknown = _connection(
        _candidate("C1", "STR_1", [_order("P1", 10, 5, 0.9)]),
        events={"P1": ("e1",)}, event_mode="unknown",
    )
    with pytest.raises(ValueError, match="event_mode"):
        filter_analysis_candidates(unknown, "R1", {})
