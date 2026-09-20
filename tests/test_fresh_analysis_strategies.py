from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import duckdb
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.pipeline import _canonical
from mrs3.source_v6 import _canonical_json


def _template() -> dict[str, object]:
    entry = {"id": 0, "len": 2, "multiplier": 1.0, "lot_x": 1.0}
    return {
        "name": "OLD",
        "is_runing": True,
        "basic": {"strategy": "old", "symbol": "OLD", "time_frame": "1h", "use_long": True, "use_short": True},
        "mrs3": {
            "ma_long": [entry],
            "ma_short": [{**entry, "multiplier": 1.0}],
            "ma_close_long": {"len": 4, "multiplier": 1.003, "side": "sell"},
            "ma_close_short": {"len": 4, "multiplier": 0.997, "side": "buy"},
        },
    }


def _point(point_id: str, shift: int, open_ma: int, event: str) -> dict[str, object]:
    return {
        "point_id": point_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "shift_bp": shift,
        "shift_pct": shift / 100,
        "open_ma": open_ma,
        "close_ma": 9,
        "pnl_pct": 10.0,
        "dd_pct": 2.0,
        "efficiency": 5.0,
        "trades": 10,
        "wins": 7,
        "losses": 3,
        "win_rate_pct": 70.0,
        "plateau_id": f"P{shift}",
        "economic_pass": True,
        "standalone_eligible": True,
        "depth_eligible": True,
        "refine_required": False,
        "event_mode": "real_independent_events",
        "_event_ids": [event],
        "event_ids_hash": sha256(event.encode()).hexdigest(),
        "point_event_count": 1,
        "pretest_ab": {
            "contract_version": "source-v6-pretest-ab-v1",
            "status": "COMPARABLE",
            "reason": "FULL_READY_WITNESS",
            "a_start_ms": 0,
            "a_end_ms": 30 * 24 * 60 * 60 * 1000,
            "b_start_ms": 16 * 24 * 60 * 60 * 1000,
            "b_end_ms": 30 * 24 * 60 * 60 * 1000,
            "a_days": 30,
            "b_days": 14,
            "a_pnl": "100",
            "b_pnl": "70",
            "a_round_trips": 10,
            "b_round_trips": 2,
        },
    }


def _order(point: dict[str, object], number: int) -> dict[str, object]:
    return {
        "id": number,
        "plateau_id": point["plateau_id"],
        "point_id": point["point_id"],
        "open_ma": point["open_ma"],
        "shift_bp": point["shift_bp"],
        "shift_pct": point["shift_pct"],
        "source_pnl_pct": point["pnl_pct"],
        "source_dd_pct": point["dd_pct"],
        "source_efficiency": point["efficiency"],
        "trades": point["trades"],
        "plateau_point_count": point.get("plateau_point_count", 1),
        "base_point_trades": point.get("base_point_trades", point["trades"]),
        "plateau_total_trades": point.get("plateau_total_trades", point["trades"]),
        "close_support": 1.0,
        "standalone_eligible": True,
        "depth_eligible": True,
    }


def _make_analysis(
    path: Path, *, event_mode: str = "real_independent_events", ready: bool = True, legacy: bool = False,
) -> tuple[str, dict[str, object]]:
    config = AlgorithmConfig.defaults()
    config_hash = sha256(_canonical_json(_canonical(config)).encode()).hexdigest()
    surface_identity = {
        "surface_id": "SURFACE-1",
        "surface_fingerprint": "surface-v6-fresh-compact-v2" if legacy else "surface-v6-fresh-compact-v3",
        "source_content_digest": "a" * 64,
        "scope_digests": {"BTCUSDT|LONG|1h": "d" * 64},
    }
    if not legacy:
        surface_identity["analysis_input_digest"] = "c" * 64
    identity = {
        "fingerprint": "analysis-v6-fresh-compact-v1" if legacy else "analysis-v6-fresh-compact-v2",
        **surface_identity,
        "algorithm_version": "algo-v1",
        "algorithm_config_sha256": config_hash,
        "listing_dates_sha256": "b" * 64,
        "event_mode": event_mode,
    }
    analysis_id = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    point_a = _point("BTCUSDT|LONG|1h|100|3|9", 100, 3, "event-a")
    point_b = _point("BTCUSDT|LONG|1h|300|4|9", 300, 4, "event-b")
    if legacy:
        point_a.pop("pretest_ab")
        point_b.pop("pretest_ab")
    structure = {
        "structure_id": "STR-READY",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "common_close_ma": 9,
        "order_count": 2,
        "orders": [_order(point_a, 1), _order(point_b, 2)],
        "plateau_point_count": [3, 4],
        "base_point_trades": [10, 11],
        "plateau_total_trades": [30, 40],
        "status": "READY_MRS3_STRUCTURE" if ready else "DEFERRED",
    }
    manifest = {**identity, "analysis_id": analysis_id}
    connection = duckdb.connect(str(path))
    connection.execute("create table manifest(key varchar primary key, value varchar not null)")
    connection.execute("create table scope_runs(scope_key varchar primary key, scope_digest varchar not null, result_digest varchar not null)")
    for name in ("points", "refine_requests", "plateaus", "close_profiles", "base_one_order", "structures", "structure_diagnostics"):
        connection.execute(f"create table {name}(scope_key varchar not null, payload_json varchar not null)")
    connection.executemany("insert into manifest values (?, ?)", [(key, value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))) for key, value in manifest.items()])
    scope_key = "BTCUSDT|LONG|1h"
    frames = {"points": [point_a, point_b], "structures": [structure]}
    connection.execute("insert into scope_runs values (?, ?, ?)", [scope_key, "d" * 64, "r" * 64])
    for name, rows in frames.items():
        connection.executemany(f"insert into {name} values (?, ?)", [(scope_key, json.dumps(row, sort_keys=True, separators=(",", ":"))) for row in rows])
    connection.close()
    return analysis_id, surface_identity


def test_legacy_analysis_works_without_pretest_and_requires_rebuild_with_it(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import (
        filter_fresh_analysis_candidates,
        generate_fresh_analysis_strategies,
        list_fresh_analysis_shortlist,
    )

    path = tmp_path / "legacy.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(path, legacy=True)

    shortlist = list_fresh_analysis_shortlist(path, analysis_id, {})
    assert [item["candidate_id"] for item in shortlist["items"]] == ["STR-READY"]
    assert shortlist["items"][0]["filter_status"] == "READY_AFTER_FILTERS"
    with pytest.raises(ValueError, match="PRETEST_AB_EVIDENCE_UNAVAILABLE"):
        filter_fresh_analysis_candidates(path, analysis_id, {}, pretest_ab_enabled=True)

    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    generated = generate_fresh_analysis_strategies(
        path, analysis_id, ["STR-READY"], [("BTCUSDT", "LONG", "1h")], template,
        tmp_path / "legacy-out", AlgorithmConfig.defaults(), filters={},
    )
    manifest = json.loads(generated.manifest_path.read_text(encoding="utf-8"))
    assert "analysis_input_digest" not in manifest

    blocked = tmp_path / "blocked-out"
    with pytest.raises(ValueError, match="PRETEST_AB_EVIDENCE_UNAVAILABLE"):
        generate_fresh_analysis_strategies(
            path, analysis_id, ["STR-READY"], [("BTCUSDT", "LONG", "1h")], template,
            blocked, AlgorithmConfig.defaults(), pretest_ab_enabled=True,
        )
    assert not blocked.exists()


def test_fresh_adapter_generates_only_selected_ready_candidate_and_binds_hashes(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    analysis_id, surface = _make_analysis(tmp_path / "run.analysis-v6.duckdb")
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_fresh_analysis_strategies(
        tmp_path / "run.analysis-v6.duckdb",
        analysis_id,
        ["STR-READY"],
        [("BTCUSDT", "LONG", "1h")],
        template,
        tmp_path / "out",
        AlgorithmConfig.defaults(),
        pretest_ab_enabled=True,
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.strategy_count == 2
    assert manifest["analysis_id"] == analysis_id
    assert manifest["analysis_run_id"] == analysis_id
    assert manifest["source_surface_id"] == surface["surface_id"]
    assert manifest["source_content_digest"] == surface["source_content_digest"]
    assert manifest["scope_digests"] == surface["scope_digests"]
    assert manifest["analysis_input_digest"] == "c" * 64
    assert manifest["candidate_identities"] == ["STR-READY"]
    assert manifest["pretest_ab_enabled"] is True
    assert manifest["pretest_ab"] == {
        "enabled": True,
        "window_days": 14,
        "decline_threshold_pct": "95",
        "contract_version": "source-v6-pretest-ab-v1",
    }
    assert len(manifest["analysis_artifact_sha256"]) == 64
    assert len(manifest["analysis_manifest_sha256"]) == 64
    strategy = json.loads(next(result.strategies_path.glob("*.json")).read_text(encoding="utf-8"))
    assert "provenance" not in strategy


def test_fresh_generation_does_not_block_on_runtime_config_hash(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    analysis_id, _ = _make_analysis(tmp_path / "run.analysis-v6.duckdb")
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    defaults = AlgorithmConfig.defaults()
    runtime_config = replace(
        defaults,
        close_multiplier_long=defaults.close_multiplier_long + 1,
    )

    result = generate_fresh_analysis_strategies(
        tmp_path / "run.analysis-v6.duckdb",
        analysis_id,
        ["STR-READY"],
        [("BTCUSDT", "LONG", "1h")],
        template,
        tmp_path / "out",
        runtime_config,
    )

    assert result.strategy_count == 2


def test_generation_manifest_persists_order_plateau_diagnostics(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    analysis_id, _ = _make_analysis(tmp_path / "run.analysis-v6.duckdb")
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_fresh_analysis_strategies(
        tmp_path / "run.analysis-v6.duckdb",
        analysis_id,
        ["STR-READY"],
        [("BTCUSDT", "LONG", "1h")],
        template,
        tmp_path / "out",
        AlgorithmConfig.defaults(),
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["candidate_diagnostics"]["STR-READY"] == {
        "order_count": 2,
        "orders": [
            {
                "order_id": 1,
                "plateau_id": "P100",
                "plateau_point_count": 3,
                "base_point_trades": 10,
                "plateau_total_trades": 30,
            },
            {
                "order_id": 2,
                "plateau_id": "P300",
                "plateau_point_count": 4,
                "base_point_trades": 11,
                "plateau_total_trades": 40,
            },
        ],
    }


def test_fresh_strategy_payload_excludes_provenance_and_manifest_keeps_lineage(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    analysis_id, _ = _make_analysis(tmp_path / "run.analysis-v6.duckdb")
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_fresh_analysis_strategies(
        tmp_path / "run.analysis-v6.duckdb",
        analysis_id,
        ["STR-READY"],
        [("BTCUSDT", "LONG", "1h")],
        template,
        tmp_path / "out",
        AlgorithmConfig.defaults(),
    )
    strategy = json.loads((result.strategies_path / "BTCUSDT_1h_LONG_2ORD_CMA9_STR-READY_EQUAL.json").read_text())
    manifest = json.loads(result.manifest_path.read_text())

    assert "provenance" not in strategy
    assert manifest["candidate_identity_to_strategy_names"] == {
        "STR-READY": [
            "BTCUSDT_1h_LONG_2ORD_CMA9_STR-READY_EQUAL",
            "BTCUSDT_1h_LONG_2ORD_CMA9_STR-READY_INCOME",
        ]
    }


def test_plateau_diagnostics_accept_order_aligned_tuples() -> None:
    from mrs3.fresh_analysis_strategies import _plateau_diagnostics

    assert _plateau_diagnostics({
        "order_count": 2,
        "orders": ({}, {}),
        "plateau_point_count": (3, 4),
        "base_point_trades": (10, 11),
        "plateau_total_trades": (30, 40),
    }) == {
        "plateau_point_count": (3, 4),
        "base_point_trades": (10, 11),
        "plateau_total_trades": (30, 40),
    }


def test_fresh_base_structure_publishes_one_equal_variant(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    connection = duckdb.connect(str(database))
    try:
        point = _point("BTCUSDT|LONG|1h|100|3|9", 100, 3, "event-a")
        connection.execute(
            "insert into structures values (?, ?)",
            [
                "BTCUSDT|LONG|1h",
                json.dumps({
                    "structure_id": "BASE-READY",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "timeframe": "1h",
                    "common_close_ma": 9,
                    "order_count": 1,
                    "orders": [_order(point, 1)],
                    "plateau_point_count": 3,
                    "base_point_trades": 10,
                    "plateau_total_trades": 30,
                    "status": "READY_MRS3_STRUCTURE",
                }, sort_keys=True, separators=(",", ":")),
            ],
        )
    finally:
        connection.close()
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_fresh_analysis_strategies(
        database,
        analysis_id,
        ["BASE-READY"],
        [("BTCUSDT", "LONG", "1h")],
        template,
        tmp_path / "out",
        AlgorithmConfig.defaults(),
    )

    assert result.strategy_count == 1
    assert [path.name for path in result.strategies_path.glob("*.json")] == [
        "BTCUSDT_1h_LONG_1ORD_CMA9_BASE-READY_EQUAL.json"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plateau_point_count", 3),
        ("base_point_trades", [10]),
        ("plateau_total_trades", [30, "40"]),
    ],
)
def test_fresh_generation_rejects_malformed_multiorder_diagnostics(
    tmp_path: Path, field: str, value: object,
) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    connection = duckdb.connect(str(database))
    try:
        raw = connection.execute(
            "select payload_json from structures where scope_key=?",
            ["BTCUSDT|LONG|1h"],
        ).fetchone()[0]
        structure = json.loads(raw)
        structure[field] = value
        connection.execute(
            "update structures set payload_json=? where scope_key=?",
            [json.dumps(structure, sort_keys=True, separators=(",", ":")), "BTCUSDT|LONG|1h"],
        )
    finally:
        connection.close()
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="diagnostic"):
        generate_fresh_analysis_strategies(
            database,
            analysis_id,
            ["STR-READY"],
            [("BTCUSDT", "LONG", "1h")],
            template,
            tmp_path / "out",
            AlgorithmConfig.defaults(),
        )


@pytest.mark.parametrize("event_mode", ["legacy_trades_proxy", "mixed"])
def test_fresh_adapter_rejects_non_independent_event_mode(tmp_path: Path, event_mode: str) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    analysis_id, _ = _make_analysis(tmp_path / "run.analysis-v6.duckdb", event_mode=event_mode)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    with pytest.raises(ValueError, match="real_independent_events"):
        generate_fresh_analysis_strategies(tmp_path / "run.analysis-v6.duckdb", analysis_id, ["STR-READY"], [("BTCUSDT", "LONG", "1h")], template, tmp_path / "out", AlgorithmConfig.defaults())


def test_fresh_adapter_rejects_unready_or_unselected_candidate(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    analysis_id, _ = _make_analysis(tmp_path / "run.analysis-v6.duckdb", ready=False)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    with pytest.raises(ValueError, match="not READY"):
        generate_fresh_analysis_strategies(tmp_path / "run.analysis-v6.duckdb", analysis_id, ["STR-READY"], [("BTCUSDT", "LONG", "1h")], template, tmp_path / "out", AlgorithmConfig.defaults())


def test_fresh_shortlist_returns_only_safe_candidate_summary(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import list_fresh_analysis_shortlist

    analysis_id, _ = _make_analysis(tmp_path / "run.analysis-v6.duckdb")
    result = list_fresh_analysis_shortlist(tmp_path / "run.analysis-v6.duckdb", analysis_id)

    assert result["analysis_run_id"] == analysis_id
    assert result["items"] == [{
        "candidate_id": "STR-READY", "pair": "BTCUSDT", "side": "LONG",
        "timeframe": "1h", "order_count": 2, "status": "READY_MRS3_STRUCTURE",
    }]
    # The grouped view carries counts only; no order, point or lot detail leaks.
    assert result["groups"] == [{
        "scope_key": "BTCUSDT|LONG|1h", "pair": "BTCUSDT", "side": "LONG", "timeframe": "1h",
        "counts": {"1ORD": 0, "2ORD": 1, "3ORD": 0, "4ORD": 0},
        "ready": 1, "total": 1, "candidate_ids": ["STR-READY"],
        "plateau_count": 0, "period": None,
    }]


def test_fresh_phase2_source_pnl_defers_only_a_dominated_candidate(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import filter_fresh_analysis_candidates, list_fresh_analysis_shortlist

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    connection = duckdb.connect(str(database))
    try:
        original = json.loads(connection.execute("select payload_json from structures").fetchone()[0])
        better = {
            **original,
            "structure_id": "STR-BETTER",
            "candidate_id": "STR-BETTER",
            "orders": [{**order, "source_pnl_pct": float(order["source_pnl_pct"]) + 1} for order in original["orders"]],
        }
        connection.execute(
            "insert into structures values (?, ?)",
            ["BTCUSDT|LONG|1h", json.dumps(better, sort_keys=True, separators=(",", ":"))],
        )
    finally:
        connection.close()

    result = filter_fresh_analysis_candidates(database, analysis_id, {"source_pnl": True})
    rows = {row["candidate_id"]: row for row in result.rows}

    assert rows["STR-BETTER"]["filter_status"] == "READY_AFTER_FILTERS"
    assert rows["STR-READY"]["deferred_by_candidate_id"] == "STR-BETTER"

    shortlist = list_fresh_analysis_shortlist(database, analysis_id, {"source_pnl": True})
    assert shortlist["groups"][0]["counts"] == {"1ORD": 0, "2ORD": 1, "3ORD": 0, "4ORD": 0}
    assert shortlist["groups"][0]["ready_after_filters"] == 1
    assert shortlist["groups"][0]["deferred"] == 1


def _set_point_pretest(database: Path, point_id: str, **changes: object) -> None:
    connection = duckdb.connect(str(database))
    try:
        rows = connection.execute("select rowid, payload_json from points").fetchall()
        for rowid, raw in rows:
            point = json.loads(raw)
            if point.get("point_id") != point_id:
                continue
            evidence = dict(point["pretest_ab"])
            evidence.update(changes)
            point["pretest_ab"] = evidence
            connection.execute(
                "update points set payload_json=? where rowid=?",
                [json.dumps(point, sort_keys=True, separators=(",", ":")), rowid],
            )
            return
    finally:
        connection.close()
    raise AssertionError(f"unknown point: {point_id}")


@pytest.mark.parametrize(
    ("changes", "expected_status", "expected_reason"),
    [
        ({"a_pnl": "3000", "b_pnl": "70"}, "PASS", "DECLINE_WITHIN_THRESHOLD"),
        ({"a_pnl": "3000", "b_pnl": "69.86"}, "REJECT", "DECLINE_GT_THRESHOLD"),
        ({"a_pnl": "3000", "b_pnl": "-14"}, "REJECT", "DECLINE_GT_THRESHOLD"),
        ({"a_pnl": "3000", "b_pnl": "-140", "b_round_trips": 0}, "PASS", "NO_B_TRADES"),
        ({"a_pnl": "0", "b_pnl": "-140"}, "PASS", "NOT_COMPARABLE"),
        ({"status": "INSUFFICIENT_HISTORY", "reason": "A_SHORTER_THAN_14_DAYS", "a_start_ms": 14 * 24 * 60 * 60 * 1000, "a_end_ms": 27 * 24 * 60 * 60 * 1000, "b_start_ms": 13 * 24 * 60 * 60 * 1000, "b_end_ms": 27 * 24 * 60 * 60 * 1000, "a_days": 13, "a_pnl": "0", "b_pnl": None, "b_round_trips": 0}, "PASS", "INSUFFICIENT_HISTORY"),
    ],
)
def test_fresh_pretest_ab_gate_has_strict_and_diagnostic_outcomes(
    tmp_path: Path, changes: dict[str, object], expected_status: str, expected_reason: str,
) -> None:
    from mrs3.fresh_analysis_strategies import filter_fresh_analysis_candidates

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    first_point = "BTCUSDT|LONG|1h|100|3|9"
    _set_point_pretest(database, first_point, **changes)

    result = filter_fresh_analysis_candidates(database, analysis_id, {}, pretest_ab_enabled=True)
    row = result.rows[0]
    assert row["pretest_ab_enabled"] is True
    assert row["pretest_ab_status"] == expected_status
    assert row["pretest_ab_reason"] == expected_reason
    assert row["pretest_ab"]["contract_version"] == "source-v6-pretest-ab-v1"
    assert (row["filter_status"] == "DEFERRED_PRETEST_AB") is (expected_status == "REJECT")


def test_fresh_pretest_ab_off_is_identity_preserving_and_does_not_extend_criteria(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import filter_fresh_analysis_candidates

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    result = filter_fresh_analysis_candidates(database, analysis_id, {"source_pnl": True})

    assert result.criteria == ("source_pnl",)
    assert result.rows[0]["pretest_ab_enabled"] is False
    assert result.rows[0]["pretest_ab_status"] == "DISABLED"
    assert result.rows[0]["filter_status"] == "READY_AFTER_FILTERS"


def test_fresh_pretest_ab_evaluates_only_first_order_and_removes_rejected_before_pareto(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import filter_fresh_analysis_candidates

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    _set_point_pretest(database, "BTCUSDT|LONG|1h|300|4|9", a_pnl="3000", b_pnl="-1400")
    result = filter_fresh_analysis_candidates(database, analysis_id, {"source_pnl": True}, pretest_ab_enabled=True)
    assert result.rows[0]["filter_status"] == "READY_AFTER_FILTERS"

    connection = duckdb.connect(str(database))
    try:
        original = json.loads(connection.execute("select payload_json from structures").fetchone()[0])
        stronger = {
            **original,
            "structure_id": "STR-BETTER",
            "candidate_id": "STR-BETTER",
            "orders": [{**order, "source_pnl_pct": 99} for order in reversed(original["orders"])],
        }
        connection.execute("insert into structures values (?, ?)", ["BTCUSDT|LONG|1h", json.dumps(stronger, sort_keys=True, separators=(",", ":"))])
    finally:
        connection.close()
    result = filter_fresh_analysis_candidates(database, analysis_id, {"source_pnl": True}, pretest_ab_enabled=True)
    rows = {row["candidate_id"]: row for row in result.rows}
    assert rows["STR-READY"]["filter_status"] == "READY_AFTER_FILTERS"
    assert rows["STR-BETTER"]["filter_status"] == "DEFERRED_PRETEST_AB"
    assert rows["STR-READY"]["deferred_by_candidate_id"] is None


def test_filtered_shortlist_counts_preexisting_non_ready_candidate_as_deferred(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import list_fresh_analysis_shortlist

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database, ready=False)

    group = list_fresh_analysis_shortlist(database, analysis_id, {"source_pnl": True})["groups"][0]
    assert group["counts"] == {"1ORD": 0, "2ORD": 0, "3ORD": 0, "4ORD": 0}
    assert group["ready_after_filters"] == 0
    assert group["deferred"] == 1
    assert group["total"] == 1
