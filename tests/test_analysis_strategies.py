from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from mrs3.analysis_storage import ensure_analysis_schema
from mrs3.config import AlgorithmConfig, DEFAULT_CANONICAL_SHIFTS_BP
from mrs3.duckdb_direct import (
    CANONICAL_GRID_VERSION,
    CANONICAL_MATERIALIZER_VERSION,
    POINT_MATERIALIZATION_SEMANTICS_VERSION,
    READINESS_CONTRACT_VERSION,
    READINESS_MAX_SHIFT_BP,
    V2_AUDIT_SCHEMA_VERSION,
    V2_GRID_CONTRACT_KIND,
    DirectPoint,
    canonical_point_materialization_config_hash,
    point_evidence_jsonl_bytes,
)
from mrs3.duckdb_source_schema import NORMALIZATION_CONTRACT_VERSION
from mrs3.pipeline import PipelineInput


def _template() -> dict[str, object]:
    entry = {"id": 1, "side": "buy", "type": "SMA", "source": "ohlc4", "len": 3, "multiplier": 0.997, "lot_x": 1.0, "order_type": "limit", "post_only": True, "hidden": False, "value": None}
    return {"name": "TEMPLATE", "is_runing": True, "basic": {"strategy": "mrs3", "symbol": "OLD", "time_frame": "1h", "use_long": True, "use_short": True}, "mrs3": {"ma_long": [entry], "ma_short": [{**entry, "side": "sell", "multiplier": 1.003}], "ma_close_long": {"len": 4, "multiplier": 1.003, "side": "sell"}, "ma_close_short": {"len": 4, "multiplier": 0.997, "side": "buy"}}}


def test_generate_analysis_strategies_uses_only_ready_candidates_and_publishes_two_variants(tmp_path: Path, monkeypatch) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies, list_analysis_candidates

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    ready = {"structure_id": "STR_1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "common_close_ma": 9, "order_count": 2, "status": "READY_MRS3_STRUCTURE", "orders": [{"id": 1, "plateau_id": "P1", "point_id": "BTCUSDT|LONG|1h|100|3|9", "open_ma": 3, "shift_bp": 100, "shift_pct": 1.0, "source_pnl_pct": 10.0, "source_dd_pct": 2.0, "source_efficiency": 5.0, "trades": 7, "close_support": 1.0, "standalone_eligible": True, "depth_eligible": True}, {"id": 2, "plateau_id": "P2", "point_id": "BTCUSDT|LONG|1h|200|4|9", "open_ma": 4, "shift_bp": 200, "shift_pct": 2.0, "source_pnl_pct": 11.0, "source_dd_pct": 2.0, "source_efficiency": 5.5, "trades": 8, "close_support": 1.0, "standalone_eligible": True, "depth_eligible": True}]}
    connection.execute("insert into candidates values ('C1', 'R1', 'S1', ?)", [json.dumps(ready)])
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|200|4|9")
    points = pd.DataFrame([
        {"point_id": "BTCUSDT|LONG|1h|100|3|9", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "shift_bp": 100, "open_ma": 3, "close_ma": 9, "pnl_pct": 10.0, "dd_pct": 2.0, "trades": 7, "event_mode": "legacy_trades_proxy", "point_event_count": 7, "event_ids_hash": "legacy"},
        {"point_id": "BTCUSDT|LONG|1h|200|4|9", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "shift_bp": 200, "open_ma": 4, "close_ma": 9, "pnl_pct": 11.0, "dd_pct": 2.0, "trades": 8, "event_mode": "legacy_trades_proxy", "point_event_count": 8, "event_ids_hash": "legacy"},
    ])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"; template.write_text(json.dumps(_template()), encoding="utf-8")

    preview = list_analysis_candidates(connection, "R1")
    result = generate_analysis_strategies(
        connection, "R1", ["C1"], [("BTCUSDT", "LONG", "1h")], template,
        tmp_path / "generated", AlgorithmConfig.defaults(),
    )

    assert preview[0]["candidate_id"] == "C1" and preview[0]["orders"][0]["shift_bp"] == 100
    assert result.strategy_count == 2
    assert sorted(path.name for path in result.strategies_path.glob("*.json"))
    assert json.loads(result.manifest_path.read_text(encoding="utf-8"))["run_id"] == "R1"


def test_generate_analysis_strategies_rejects_candidate_deferred_by_active_filters(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3 import analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    candidate = {"structure_id": "STR_1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "common_close_ma": 9, "order_count": 1, "status": "READY_MRS3_STRUCTURE", "orders": []}
    connection.execute("insert into candidates values ('C1', 'R1', 'S1', ?)", [json.dumps(candidate)])
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    monkeypatch.setattr(
        analysis_strategies,
        "filter_analysis_candidates",
        lambda *_args: SimpleNamespace(rows=({"candidate_id": "C1", "filter_status": "DEFERRED_REDUNDANT"},)),
        raising=False,
    )
    monkeypatch.setattr(
        "mrs3.analysis_strategies.load_published_surface",
        lambda _connection, surface_id: PipelineInput(surface_id, pd.DataFrame([_point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7)])),
    )
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="deferred"):
        analysis_strategies.generate_analysis_strategies(
            connection, "R1", ["C1"], [("BTCUSDT", "LONG", "1h")], template, tmp_path / "out",
            AlgorithmConfig.defaults(), ("source_pnl",),
        )


def test_generate_analysis_strategies_rejects_mixed_ready_and_non_ready_selection(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    ready = {"structure_id": "STR_READY", "status": "READY_MRS3_STRUCTURE", "orders": []}
    rejected = {"structure_id": "STR_REJECTED", "status": "REJECTED", "orders": []}
    connection.executemany(
        "insert into candidates values (?, 'R1', 'S1', ?)",
        [("C_READY", json.dumps(ready)), ("C_REJECTED", json.dumps(rejected))],
    )
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    monkeypatch.setattr(
        "mrs3.analysis_strategies.load_published_surface",
        lambda _connection, surface_id: PipelineInput(surface_id, pd.DataFrame([_point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7)])),
    )
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="not READY"):
        generate_analysis_strategies(
            connection, "R1", ["C_READY", "C_REJECTED"], [("BTCUSDT", "LONG", "1h")], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )


def _frozen_facts_run(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    connection.execute("insert into surface_pairs(surface_id, pair_key) values ('S1', 'BTCUSDT|LONG|100|3|9')")
    connection.execute("insert into surface_timeframes(surface_id, pair_key, timeframe) values ('S1', 'BTCUSDT|LONG|100|3|9', '1h')")
    connection.execute("insert into surface_points(surface_id, canonical_point_key, pair_key, timeframe, point_event_count, source_report_id, source_hash, provenance_state, metrics_json) values ('S1', 'BTCUSDT|LONG|1h|100|3|9', 'BTCUSDT|LONG|100|3|9', '1h', 7, 'report', ?, 'REPRODUCIBLE', '{}')", ["a" * 64])
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    connection.execute("insert into plateaus(run_id, plateau_id, surface_id, metrics_json) values ('R1', 'P1', 'S1', ?)", [json.dumps(_valid_frozen_metrics())])
    connection.execute("insert into plateau_members(run_id, plateau_id, surface_id, canonical_point_key) values ('R1', 'P1', 'S1', 'BTCUSDT|LONG|1h|100|3|9')")
    _canonicalize_surface(connection)


def _valid_frozen_metrics() -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "ready": True,
        "operational_facts_version": "cma_representatives_v1",
        "primary_close_ma": 9,
        "cma_representatives": [
            {
                "close_ma": 9,
                "point_id": "BTCUSDT|LONG|1h|100|3|9",
                "support": 1.0,
                "support_status": "PRIMARY_CLOSE",
                "continuity_status": "USABLE",
                "usable": True,
            }
        ],
        "base_1ord_point_id": "BTCUSDT|LONG|1h|100|3|9",
        "standalone_eligible_point_ids": ("BTCUSDT|LONG|1h|100|3|9",),
    }


def test_load_validated_plateau_facts_accepts_canonical_facts() -> None:
    from mrs3.analysis_strategies import load_validated_plateau_facts

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _frozen_facts_run(connection)

    facts = load_validated_plateau_facts(connection, "R1")

    assert len(facts) == 1
    assert facts[0][0] == "P1"
    assert facts[0][1]["operational_facts_version"] == "cma_representatives_v1"


def test_load_validated_plateau_facts_rejects_malformed_facts() -> None:
    from mrs3.analysis_strategies import load_validated_plateau_facts

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _frozen_facts_run(connection)
    metrics = _valid_frozen_metrics()
    metrics["cma_representatives"][0]["continuity_status"] = "BLOCKED_BY_CONTINUITY"
    metrics["cma_representatives"][0]["usable"] = False
    connection.execute("update plateaus set metrics_json=? where run_id='R1'", [json.dumps(metrics)])

    with pytest.raises(ValueError, match="continuity"):
        load_validated_plateau_facts(connection, "R1")


def test_load_validated_plateau_facts_rejects_unknown_version() -> None:
    from mrs3.analysis_strategies import load_validated_plateau_facts

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _frozen_facts_run(connection)
    metrics = _valid_frozen_metrics()
    metrics["operational_facts_version"] = "unknown_v1"
    connection.execute("update plateaus set metrics_json=? where run_id='R1'", [json.dumps(metrics)])

    with pytest.raises(ValueError, match="version"):
        load_validated_plateau_facts(connection, "R1")


def _no_facts_run(connection: duckdb.DuckDBPyConnection, facts_state: str) -> None:
    connection.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    if facts_state == "COMPUTED":
        connection.execute("insert into analysis_run_facts(run_id, facts_state, unique_point_count, economic_eligible_point_count, event_eligible_point_count, plateau_count, ready_candidate_count, final_state) values ('R1', 'COMPUTED', 1, 1, 1, 1, 0, 'COMMITTED')")
    else:
        connection.execute("insert into analysis_run_facts(run_id, facts_state, final_state) values ('R1', 'UNAVAILABLE_LEGACY', 'COMMITTED')")
    connection.execute("insert into plateaus(run_id, plateau_id, surface_id, metrics_json) values ('R1', 'P1', 'S1', ?)", [json.dumps({"ready": True})])


def test_load_validated_plateau_facts_rejects_computed_ready_plateau_without_facts() -> None:
    from mrs3.analysis_strategies import load_validated_plateau_facts

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _no_facts_run(connection, "COMPUTED")

    with pytest.raises(ValueError, match="frozen operational facts"):
        load_validated_plateau_facts(connection, "R1")


def test_load_validated_plateau_facts_allows_legacy_ready_plateau_without_facts() -> None:
    from mrs3.analysis_strategies import load_validated_plateau_facts

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _no_facts_run(connection, "UNAVAILABLE_LEGACY")

    assert load_validated_plateau_facts(connection, "R1") == ()


def test_generate_analysis_strategies_rejects_invalid_frozen_base(tmp_path: Path, monkeypatch) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _frozen_facts_run(connection)
    metrics = _valid_frozen_metrics()
    metrics["base_1ord_point_id"] = "BTCUSDT|LONG|1h|200|3|9"
    connection.execute("update plateaus set metrics_json=? where run_id='R1'", [json.dumps(metrics)])
    ready = {"structure_id": "STR_1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "common_close_ma": 9, "order_count": 1, "status": "READY_MRS3_STRUCTURE", "orders": []}
    connection.execute("insert into candidates values ('C1', 'R1', 'S1', ?)", [json.dumps(ready)])
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="base"):
        generate_analysis_strategies(
            connection, "R1", ["C1"], [("BTCUSDT", "LONG", "1h")], template, tmp_path / "out",
            AlgorithmConfig.defaults(),
        )


def _seed_run(connection: duckdb.DuckDBPyConnection, surface_side: str = "LONG", event_mode: str = "real_independent_events") -> None:
    connection.execute(
        "insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side, event_mode, materializer_version, normalization_contract_version, point_materialization_config_hash) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', ?, ?, ?, ?, ?)",
        [surface_side, event_mode, CANONICAL_MATERIALIZER_VERSION, NORMALIZATION_CONTRACT_VERSION, canonical_point_materialization_config_hash(DEFAULT_CANONICAL_SHIFTS_BP)],
    )
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")


def _seed_surface_point(connection: duckdb.DuckDBPyConnection, point_id: str, event_count: int = 7) -> None:
    symbol, side, timeframe, shift_bp, open_ma, close_ma = point_id.split("|")
    pair_key = f"{symbol}|{side}|{shift_bp}|{open_ma}|{close_ma}"
    connection.execute("insert or ignore into surface_pairs(surface_id, pair_key) values ('S1', ?)", [pair_key])
    connection.execute("insert or ignore into surface_timeframes(surface_id, pair_key, timeframe) values ('S1', ?, ?)", [pair_key, timeframe])
    connection.execute("insert into surface_points(surface_id, canonical_point_key, pair_key, timeframe, point_event_count, source_report_id, source_hash, provenance_state, metrics_json) values ('S1', ?, ?, ?, ?, 'report', ?, 'REPRODUCIBLE', '{}')", [point_id, pair_key, timeframe, event_count, "a" * 64])
    _canonicalize_surface(connection)


def _seed_frozen_plateau(connection: duckdb.DuckDBPyConnection, plateau_id: str, symbol: str, side: str, timeframe: str, point_id: str, close_ma: int) -> None:
    metrics = {
        "symbol": symbol,
        "side": side,
        "timeframe": timeframe,
        "ready": True,
        "operational_facts_version": "cma_representatives_v1",
        "primary_close_ma": close_ma,
        "cma_representatives": [
            {
                "close_ma": close_ma,
                "point_id": point_id,
                "support": 1.0,
                "support_status": "PRIMARY_CLOSE",
                "continuity_status": "USABLE",
                "usable": True,
            }
        ],
        "base_1ord_point_id": point_id,
        "standalone_eligible_point_ids": [point_id],
    }
    connection.execute("insert into plateaus(run_id, plateau_id, surface_id, metrics_json) values ('R1', ?, 'S1', ?)", [plateau_id, json.dumps(metrics)])
    connection.execute("insert into plateau_members(run_id, plateau_id, surface_id, canonical_point_key) values ('R1', ?, 'S1', ?)", [plateau_id, point_id])


def _point_row(point_id: str, pnl_pct: float, dd_pct: float, trades: int, event_count: int = 7) -> dict[str, object]:
    symbol, side, timeframe, shift_bp, open_ma, close_ma = point_id.split("|")
    return {
        "point_id": point_id,
        "symbol": symbol,
        "side": side,
        "timeframe": timeframe,
        "shift_bp": int(shift_bp),
        "shift_pct": int(shift_bp) / 100,
        "open_ma": int(open_ma),
        "close_ma": int(close_ma),
        "pnl_pct": pnl_pct,
        "dd_pct": dd_pct,
        "trades": trades,
        "point_event_count": event_count,
    }


def _canonicalize_surface(connection: duckdb.DuckDBPyConnection) -> None:
    side = str(connection.execute("select side from surfaces where surface_id='S1'").fetchone()[0])
    rows = connection.execute(
        "select canonical_point_key, source_report_id, source_hash, point_event_count from surface_points where surface_id='S1' order by canonical_point_key"
    ).fetchall()
    points = [
        DirectPoint(str(key), str(report_id), str(source_hash), int(count), {})
        for key, report_id, source_hash, count in rows
    ]
    evidence = point_evidence_jsonl_bytes(points)
    scopes = sorted({
        f"{str(key).split('|')[0]}|{str(key).split('|')[2]}"
        for key, _report_id, _source_hash, _count in rows
    })
    witnesses: dict[str, object] = {}
    for scope in scopes:
        symbol, timeframe = scope.split("|")
        witnesses[scope] = [
            {
                "symbol": symbol,
                "side": side,
                "timeframe": timeframe,
                "open_ma": 3,
                "close_ma": close_ma,
                "shifts_bp": list(DEFAULT_CANONICAL_SHIFTS_BP),
                "contract_version": READINESS_CONTRACT_VERSION,
                "max_shift_bp": READINESS_MAX_SHIFT_BP,
            }
            for close_ma in range(2, 8)
        ]
    audit_bytes = b"pair,side\nBTC,LONG\n"
    config_hash = canonical_point_materialization_config_hash(DEFAULT_CANONICAL_SHIFTS_BP)
    grid_contract = {
        "kind": V2_GRID_CONTRACT_KIND,
        "canonical_grid_version": CANONICAL_GRID_VERSION,
        "canonical_shifts_bp": list(DEFAULT_CANONICAL_SHIFTS_BP),
        "point_materialization_semantics_version": POINT_MATERIALIZATION_SEMANTICS_VERSION,
        "point_materialization_config_hash": config_hash,
        "selected_scopes": scopes,
        "readiness_contract_version": READINESS_CONTRACT_VERSION,
        "readiness_max_shift_bp": READINESS_MAX_SHIFT_BP,
        "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION,
        "witnesses": witnesses,
        "point_evidence": evidence.decode("utf-8"),
        "point_evidence_sha256": sha256(evidence).hexdigest(),
        "audit_artifact_name": f"surface_coverage_audit_{side}.csv",
        "audit_schema_version": V2_AUDIT_SCHEMA_VERSION,
        "audit_size_bytes": len(audit_bytes),
        "audit_row_count": 1,
        "audit_sha256": sha256(audit_bytes).hexdigest(),
    }
    connection.execute(
        "update surfaces set grid_contract_json=?, materializer_version=?, normalization_contract_version=?, point_materialization_config_hash=?, event_mode=? where surface_id='S1'",
        [json.dumps(grid_contract), CANONICAL_MATERIALIZER_VERSION, NORMALIZATION_CONTRACT_VERSION, config_hash, "real_independent_events"],
    )


def test_frozen_base_structures_selects_at_most_three_per_scope_using_exact_ranking() -> None:
    from mrs3.analysis_strategies import _frozen_base_structures, load_validated_plateau_facts

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    specs = [
        ("P1", "BTCUSDT|LONG|1h|550|3|9", 20.0, 5.0, 10, 1000),
        ("P2", "BTCUSDT|LONG|1h|30|3|9", 15.0, 2.0, 10, 1),
        ("P3", "BTCUSDT|LONG|1h|90|3|9", 12.0, 2.0, 5, 3),
        ("P4", "BTCUSDT|LONG|1h|200|3|9", 10.0, 2.0, 50, 7),
        ("P5", "BTCUSDT|LONG|1h|310|3|9", 8.0, 2.0, 5, 9),
    ]
    points_rows = []
    for plateau_id, point_id, pnl, dd, trades, event_count in specs:
        _seed_surface_point(connection, point_id, event_count)
        _seed_frozen_plateau(connection, plateau_id, "BTCUSDT", "LONG", "1h", point_id, 9)
        points_rows.append(_point_row(point_id, pnl, dd, trades, event_count))
    points = pd.DataFrame(points_rows)

    facts = load_validated_plateau_facts(connection, "R1")
    structures, selected = _frozen_base_structures(
        facts, points, AlgorithmConfig.defaults(), {("BTCUSDT", "LONG", "1h")},
    )

    assert len(structures) == 3
    assert [str(row.point_id) for row in selected.itertuples(index=False)] == [
        "BTCUSDT|LONG|1h|30|3|9",
        "BTCUSDT|LONG|1h|90|3|9",
        "BTCUSDT|LONG|1h|200|3|9",
    ]
    # The 550 bp point has the highest EventCount but is not a Top-3 dimension.
    assert "BTCUSDT|LONG|1h|550|3|9" not in [str(row.point_id) for row in selected.itertuples(index=False)]


def test_generate_analysis_strategies_accepts_empty_candidate_ids_with_valid_base(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    _seed_frozen_plateau(connection, "P1", "BTCUSDT", "LONG", "1h", "BTCUSDT|LONG|1h|100|3|9", 9)
    points = pd.DataFrame([_point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7)])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_analysis_strategies(
        connection, "R1", (), [("BTCUSDT", "LONG", "1h")], template,
        tmp_path / "generated", AlgorithmConfig.defaults(),
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.strategy_count == 1
    assert manifest["base_1ord_count"] == 1
    assert manifest["ready_structure_count"] == 0
    assert manifest["selected_scopes"] == [["BTCUSDT", "LONG", "1h"]]
    generated_files = list(result.strategies_path.glob("*.json"))
    assert len(generated_files) == 1
    strategy = json.loads(generated_files[0].read_text(encoding="utf-8"))
    assert strategy["name"].endswith("EQUAL")


def test_generate_analysis_strategies_empty_candidates_with_active_criteria_generates_base(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    _seed_frozen_plateau(connection, "P1", "BTCUSDT", "LONG", "1h", "BTCUSDT|LONG|1h|100|3|9", 9)
    points = pd.DataFrame([_point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7)])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_analysis_strategies(
        connection, "R1", (), [("BTCUSDT", "LONG", "1h")], template,
        tmp_path / "generated", AlgorithmConfig.defaults(), ("source_pnl",),
    )

    assert result.strategy_count == 1


def test_generate_analysis_strategies_rejects_empty_selected_scopes(tmp_path: Path) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="must not be empty"):
        generate_analysis_strategies(
            connection, "R1", (), [], template, tmp_path / "out",
            AlgorithmConfig.defaults(),
        )


def test_generate_analysis_strategies_rejects_legacy_surface_backed_run(tmp_path: Path) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection, event_mode="legacy_trades_proxy")
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="fresh canonical"):
        generate_analysis_strategies(
            connection, "R1", (), [("BTCUSDT", "LONG", "1h")], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )


def test_generate_analysis_strategies_rejects_scope_absent_from_published_surface(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    points = pd.DataFrame([_point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7)])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="absent from the published surface"):
        generate_analysis_strategies(
            connection, "R1", (), [("ETHUSDT", "LONG", "1h")], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )


def test_generate_analysis_strategies_rejects_scope_side_mismatch(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    points = pd.DataFrame([_point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7)])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="run side"):
        generate_analysis_strategies(
            connection, "R1", (), [("BTCUSDT", "SHORT", "1h")], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )


def test_generate_analysis_strategies_rejects_malformed_selected_scopes(tmp_path: Path) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="symbol, side, timeframe"):
        generate_analysis_strategies(
            connection, "R1", (), [("BTCUSDT", "LONG")], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )
    with pytest.raises(ValueError, match="invalid side"):
        generate_analysis_strategies(
            connection, "R1", (), [("BTCUSDT", "SIDEWAYS", "1h")], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )


def test_generate_analysis_strategies_rejects_candidate_outside_selected_scopes(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    _seed_surface_point(connection, "BTCUSDT|LONG|5m|100|3|9")
    ready = {"structure_id": "STR_1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "common_close_ma": 9, "order_count": 1, "status": "READY_MRS3_STRUCTURE", "orders": []}
    connection.execute("insert into candidates values ('C1', 'R1', 'S1', ?)", [json.dumps(ready)])
    points = pd.DataFrame([
        _point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7),
        _point_row("BTCUSDT|LONG|5m|100|3|9", 10.0, 2.0, 7),
    ])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="outside the selected scopes"):
        generate_analysis_strategies(
            connection, "R1", ["C1"], [("BTCUSDT", "LONG", "5m")], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )


def test_generate_analysis_strategies_errors_when_neither_base_nor_candidates_exist(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    points = pd.DataFrame([_point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7)])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="no BASE or READY"):
        generate_analysis_strategies(
            connection, "R1", (), [("BTCUSDT", "LONG", "1h")], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )


def test_generate_analysis_strategies_generates_base_and_multiorder_together(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    base_point = "BTCUSDT|LONG|1h|100|3|9"
    order_points = ["BTCUSDT|LONG|1h|200|3|9", "BTCUSDT|LONG|1h|350|3|9"]
    for point_id in [base_point, *order_points]:
        _seed_surface_point(connection, point_id)
    _seed_frozen_plateau(connection, "P1", "BTCUSDT", "LONG", "1h", base_point, 9)
    ready = {
        "structure_id": "STR_M", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h",
        "common_close_ma": 9, "order_count": 2, "status": "READY_MRS3_STRUCTURE",
        "orders": [
            {"id": 1, "plateau_id": "P2", "point_id": order_points[0], "open_ma": 3, "shift_bp": 200, "shift_pct": 2.0, "source_pnl_pct": 10.0, "source_dd_pct": 2.0, "source_efficiency": 5.0, "trades": 7, "close_support": 1.0, "standalone_eligible": True, "depth_eligible": True},
            {"id": 2, "plateau_id": "P3", "point_id": order_points[1], "open_ma": 3, "shift_bp": 350, "shift_pct": 3.5, "source_pnl_pct": 11.0, "source_dd_pct": 2.0, "source_efficiency": 5.5, "trades": 8, "close_support": 1.0, "standalone_eligible": True, "depth_eligible": True},
        ],
    }
    connection.execute("insert into candidates values ('C1', 'R1', 'S1', ?)", [json.dumps(ready)])
    points = pd.DataFrame([
        _point_row(base_point, 10.0, 2.0, 7),
        _point_row(order_points[0], 10.0, 2.0, 7),
        _point_row(order_points[1], 11.0, 2.0, 8),
    ])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_analysis_strategies(
        connection, "R1", ["C1"], [("BTCUSDT", "LONG", "1h")], template,
        tmp_path / "generated", AlgorithmConfig.defaults(),
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.strategy_count == 3
    assert manifest["base_1ord_count"] == 1
    assert manifest["ready_structure_count"] == 1
    assert manifest["selected_scopes"] == [["BTCUSDT", "LONG", "1h"]]


def test_generate_analysis_strategies_manifest_contains_sorted_unique_selected_scopes(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    points = []
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    _seed_surface_point(connection, "ETHUSDT|LONG|15m|100|3|9")
    _seed_frozen_plateau(connection, "P1", "BTCUSDT", "LONG", "1h", "BTCUSDT|LONG|1h|100|3|9", 9)
    _seed_frozen_plateau(connection, "P2", "ETHUSDT", "LONG", "15m", "ETHUSDT|LONG|15m|100|3|9", 9)
    points.append(_point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7))
    points.append(_point_row("ETHUSDT|LONG|15m|100|3|9", 10.0, 2.0, 7))
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, pd.DataFrame(points)))
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_analysis_strategies(
        connection, "R1", (), [("ETHUSDT", "LONG", "15m"), ("BTCUSDT", "LONG", "1h"), ("BTCUSDT", "LONG", "1h")],
        template, tmp_path / "generated", AlgorithmConfig.defaults(),
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["selected_scopes"] == [["BTCUSDT", "LONG", "1h"], ["ETHUSDT", "LONG", "15m"]]
    assert result.strategy_count == 2


def test_frozen_base_structures_uses_persisted_base_point_not_standalone_list() -> None:
    from mrs3.analysis_strategies import _frozen_base_structures, load_validated_plateau_facts

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    base_point = "BTCUSDT|LONG|1h|100|3|9"
    stronger_point = "BTCUSDT|LONG|1h|200|3|9"
    _seed_surface_point(connection, base_point)
    _seed_surface_point(connection, stronger_point)
    metrics = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "ready": True,
        "operational_facts_version": "cma_representatives_v1",
        "primary_close_ma": 9,
        "cma_representatives": [
            {"close_ma": 9, "point_id": base_point, "support": 1.0, "support_status": "PRIMARY_CLOSE", "continuity_status": "USABLE", "usable": True},
        ],
        "base_1ord_point_id": base_point,
        "standalone_eligible_point_ids": [base_point, stronger_point],
    }
    connection.execute("insert into plateaus(run_id, plateau_id, surface_id, metrics_json) values ('R1', 'P1', 'S1', ?)", [json.dumps(metrics)])
    connection.execute("insert into plateau_members(run_id, plateau_id, surface_id, canonical_point_key) values ('R1', 'P1', 'S1', ?)", [base_point])
    points = pd.DataFrame([
        _point_row(base_point, 5.0, 2.0, 3),
        _point_row(stronger_point, 50.0, 2.0, 30),
    ])

    facts = load_validated_plateau_facts(connection, "R1")
    structures, selected = _frozen_base_structures(
        facts, points, AlgorithmConfig.defaults(), {("BTCUSDT", "LONG", "1h")},
    )

    assert len(structures) == 1
    assert structures[0]["orders"][0]["point_id"] == base_point
    assert str(selected.iloc[0]["point_id"]) == base_point


def test_frozen_base_structures_uses_exact_top3_tiebreaker_chain() -> None:
    from mrs3.analysis_strategies import _frozen_base_structures, load_validated_plateau_facts

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    specs = [
        ("P1", "BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 5, 7),
        ("P2", "BTCUSDT|LONG|1h|200|3|9", 10.0, 2.0, 5, 7),
        ("P3", "BTCUSDT|LONG|1h|300|3|9", 10.0, 3.0, 5, 7),
        ("P4", "BTCUSDT|LONG|1h|400|3|9", 10.0, 2.0, 1, 7),
        ("P5", "BTCUSDT|LONG|1h|500|3|9", 20.0, 4.0, 5, 7),
        ("P6", "BTCUSDT|LONG|1h|600|3|9", 10.0, 5.0, 100, 9000),
    ]
    points_rows = []
    for plateau_id, point_id, pnl, dd, trades, event_count in specs:
        _seed_surface_point(connection, point_id, event_count)
        _seed_frozen_plateau(connection, plateau_id, "BTCUSDT", "LONG", "1h", point_id, 9)
        points_rows.append(_point_row(point_id, pnl, dd, trades, event_count))
    points = pd.DataFrame(points_rows)

    facts = load_validated_plateau_facts(connection, "R1")
    structures, selected = _frozen_base_structures(
        facts, points, AlgorithmConfig.defaults(), {("BTCUSDT", "LONG", "1h")},
    )

    assert len(structures) == 3
    assert [str(row.point_id) for row in selected.itertuples(index=False)] == [
        "BTCUSDT|LONG|1h|500|3|9",  # pnl DESC within equal pnl@DD5
        "BTCUSDT|LONG|1h|100|3|9",  # point_id ASC within pnl/trades/dd tie
        "BTCUSDT|LONG|1h|200|3|9",
    ]


def test_generate_analysis_strategies_rejects_candidate_absent_from_run(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    _seed_run(connection)
    _seed_surface_point(connection, "BTCUSDT|LONG|1h|100|3|9")
    points = pd.DataFrame([_point_row("BTCUSDT|LONG|1h|100|3|9", 10.0, 2.0, 7)])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="absent from this analysis run"):
        generate_analysis_strategies(
            connection, "R1", ["C_MISSING"], [("BTCUSDT", "LONG", "1h")], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )


def test_generate_v6_analysis_strategies_exports_only_committed_ready_with_provenance(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3.analysis_strategies import generate_v6_analysis_strategies
    from mrs3.pipeline import _canonical

    config = AlgorithmConfig.defaults()
    canonical = json.dumps(_canonical(config), sort_keys=True, separators=(",", ":"))
    config_hash = sha256(canonical.encode()).hexdigest()
    identity = {
        "identity_version": "source-v6-analysis-run-id-v1",
        "surface_id": "SURFACE_V6",
        "manifest_sha256": "m" * 64,
        "frozen_facts_sha256": "f" * 64,
        "compatibility_versions": {
            "surface_schema_version": 6,
            "metric_schema_version": "metric-v1",
            "event_schema_version": "events-v1",
            "readiness_schema_version": "ready-v1",
            "frozen_facts_digest_algorithm": "facts-v1",
        },
        "selected_scope": {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"},
        "selected_interval": {"start_ms": 1000, "end_ms": 2000},
        "event_mode": "real_independent_events",
        "algorithm_version": "algo-v6",
        "algorithm_config_sha256": config_hash,
        "listing_dates_sha256": "l" * 64,
    }
    run_id = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    metadata = {
        **identity,
        "source_surface_id": identity["surface_id"],
        "source_manifest_sha256": identity["manifest_sha256"],
        "source_frozen_facts_sha256": identity["frozen_facts_sha256"],
        "analysis_run_id": run_id,
        "canonical_identity_bytes": json.dumps(identity, sort_keys=True, separators=(",", ":")),
        "attempt_state": "COMMITTED",
        "state": "COMMITTED",
    }
    point_ids = ("BTCUSDT|LONG|1h|100|3|9", "BTCUSDT|LONG|1h|300|4|9")
    points = []
    for point_id, shift, open_ma, pnl in zip(point_ids, (100, 300), (3, 4), (10.0, 11.0), strict=True):
        events = [f"event-{shift}", f"event-{shift}-2", f"event-{shift}-3"]
        points.append({
            "point_id": point_id, "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h",
            "shift_bp": shift, "shift_pct": shift / 100, "open_ma": open_ma, "close_ma": 9,
            "pnl_pct": pnl, "dd_pct": 2.0, "efficiency": pnl / 2, "trades": 10,
            "plateau_id": f"P{shift}", "economic_pass": True, "standalone_eligible": True,
            "depth_eligible": True, "refine_required": False, "event_mode": "real_independent_events",
            "_event_ids": events, "event_ids_hash": sha256("|".join(events).encode()).hexdigest(),
            "point_event_count": 3,
        })
    orders = tuple({
        "id": index, "plateau_id": f"P{shift}", "point_id": point_id, "open_ma": open_ma,
        "shift_bp": shift, "shift_pct": shift / 100, "source_pnl_pct": pnl,
        "source_dd_pct": 2.0, "source_efficiency": pnl / 2, "trades": 10,
        "close_support": 1.0, "standalone_eligible": True, "depth_eligible": True,
    } for index, (point_id, shift, open_ma, pnl) in enumerate(zip(point_ids, (100, 300), (3, 4), (10.0, 11.0), strict=True), start=1))
    monkeypatch.setattr(
        "mrs3.source_v6_surface.read_source_v6_analysis_run",
        lambda _path, _run_id: {"analysis_run_id": run_id, "state": "COMMITTED", "event_mode": "real_independent_events", "metadata": metadata, "facts": {"points": points, "structures": [{"candidate_id": "C_READY", "structure_id": "STR_READY", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "common_close_ma": 9, "order_count": 2, "orders": orders, "status": "READY_MRS3_STRUCTURE"}]}},
    )
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_v6_analysis_strategies("surface.duckdb", run_id, ["C_READY"], [("BTCUSDT", "LONG", "1h")], template, tmp_path / "out", config)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.strategy_count == 2
    assert manifest["event_mode"] == "real_independent_events"
    assert manifest["candidate_identities"] == ["C_READY"]
    strategy = json.loads(next(result.strategies_path.glob("*.json")).read_text(encoding="utf-8"))
    assert strategy["provenance"]["source_surface_id"] == "SURFACE_V6"
    assert strategy["provenance"]["generation_manifest_sha256"] == manifest["generation_manifest_sha256"]


def _v6_provenance_stub(config: AlgorithmConfig) -> tuple[dict[str, object], str]:
    from mrs3.pipeline import _canonical

    config_json = json.dumps(_canonical(config), sort_keys=True, separators=(",", ":"))
    config_hash = sha256(config_json.encode()).hexdigest()
    identity = {
        "identity_version": "source-v6-analysis-run-id-v1",
        "surface_id": "SURFACE_V6",
        "manifest_sha256": "m" * 64,
        "frozen_facts_sha256": "f" * 64,
        "compatibility_versions": {
            "surface_schema_version": 6,
            "metric_schema_version": "metric-v1",
            "event_schema_version": "events-v1",
            "readiness_schema_version": "ready-v1",
            "frozen_facts_digest_algorithm": "facts-v1",
        },
        "selected_scope": {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"},
        "selected_interval": {"start_ms": 1000, "end_ms": 2000},
        "event_mode": "real_independent_events",
        "algorithm_version": "algo-v6",
        "algorithm_config_sha256": config_hash,
        "listing_dates_sha256": "l" * 64,
    }
    identity_bytes = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    run_id = sha256(identity_bytes.encode()).hexdigest()
    point = {
        "point_id": "BTCUSDT|LONG|1h|100|3|9", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h",
        "shift_bp": 100, "shift_pct": 1.0, "open_ma": 3, "close_ma": 9, "pnl_pct": 10.0,
        "dd_pct": 2.0, "efficiency": 5.0, "trades": 10, "plateau_id": None,
        "economic_pass": True, "standalone_eligible": True, "depth_eligible": True,
        "refine_required": False, "event_mode": "real_independent_events", "_event_ids": ["event-1"],
        "event_ids_hash": sha256(b"event-1").hexdigest(), "point_event_count": 1,
    }
    metadata = {
        "source_surface_id": "SURFACE_V6", "source_manifest_sha256": "m" * 64,
        "source_frozen_facts_sha256": "f" * 64, "compatibility_versions": identity["compatibility_versions"],
        "selected_scope": identity["selected_scope"], "selected_interval": identity["selected_interval"],
        "event_mode": "real_independent_events", "algorithm_version": "algo-v6",
        "algorithm_config_sha256": config_hash, "listing_dates_sha256": "l" * 64,
        "canonical_identity_bytes": identity_bytes, "analysis_run_id": run_id,
        "state": "COMMITTED", "attempt_state": "COMMITTED",
    }
    return {
        "analysis_run_id": run_id, "state": "COMMITTED", "event_mode": "real_independent_events",
        "metadata": metadata, "facts": {"points": [point], "structures": []},
    }, run_id


def test_generate_v6_manifest_maps_candidate_identity_to_every_variant(tmp_path: Path, monkeypatch) -> None:
    # The production-shaped metadata intentionally has source_* keys only.
    from mrs3.analysis_strategies import generate_v6_analysis_strategies

    result, run_id = _v6_provenance_stub(AlgorithmConfig.defaults())
    structure = {
        "candidate_id": "C_READY", "structure_id": "STR_READY", "symbol": "BTCUSDT", "side": "LONG",
        "timeframe": "1h", "common_close_ma": 9, "order_count": 2, "status": "READY_MRS3_STRUCTURE",
        "orders": [
            {"id": 1, "plateau_id": "P1", "point_id": "BTCUSDT|LONG|1h|100|3|9", "open_ma": 3, "shift_bp": 100, "shift_pct": 1.0, "source_pnl_pct": 10.0, "source_dd_pct": 2.0, "source_efficiency": 5.0, "trades": 10, "close_support": 1.0, "standalone_eligible": True, "depth_eligible": True},
            {"id": 2, "plateau_id": "P2", "point_id": "BTCUSDT|LONG|1h|300|4|9", "open_ma": 4, "shift_bp": 300, "shift_pct": 3.0, "source_pnl_pct": 10.0, "source_dd_pct": 2.0, "source_efficiency": 5.0, "trades": 10, "close_support": 1.0, "standalone_eligible": True, "depth_eligible": True},
        ],
    }
    result["facts"]["points"][0]["plateau_id"] = "P1"
    second_point = dict(result["facts"]["points"][0])
    second_point.update({"point_id": "BTCUSDT|LONG|1h|300|4|9", "shift_bp": 300, "shift_pct": 3.0, "open_ma": 4, "plateau_id": "P2", "_event_ids": ["event-2"]})
    second_point["event_ids_hash"] = sha256(b"event-2").hexdigest()
    result["facts"]["points"].append(second_point)
    result["facts"]["structures"] = [structure]
    monkeypatch.setattr("mrs3.source_v6_surface.read_source_v6_analysis_run", lambda *_: result)
    template = tmp_path / "template.json"; template.write_text(json.dumps(_template()), encoding="utf-8")
    generated = generate_v6_analysis_strategies("surface.duckdb", run_id, ["C_READY"], [("BTCUSDT", "LONG", "1h")], template, tmp_path / "out", AlgorithmConfig.defaults())
    manifest = json.loads(generated.manifest_path.read_text(encoding="utf-8"))
    names = sorted(path.stem for path in generated.strategies_path.glob("*.json"))
    assert manifest["candidate_identity_to_strategy_names"] == {"C_READY": names}


@pytest.mark.parametrize("failure", ["state", "mode", "missing_provenance", "manifest_hash", "frozen_hash", "config_hash", "listing_hash", "wrong_scope", "interval", "event_hash", "event_count", "no_ready"])
def test_generate_v6_rejects_invalid_run_provenance_and_ready_input(tmp_path: Path, monkeypatch, failure: str) -> None:
    from copy import deepcopy
    from mrs3.analysis_strategies import generate_v6_analysis_strategies

    config = AlgorithmConfig.defaults()
    result, run_id = _v6_provenance_stub(config)
    result = deepcopy(result)
    if failure == "state":
        result["state"] = "RUNNING"
    elif failure == "mode":
        result["event_mode"] = result["metadata"]["event_mode"] = "legacy_trades_proxy"
    elif failure == "missing_provenance":
        result["metadata"].pop("source_frozen_facts_sha256")
    elif failure == "manifest_hash":
        result["metadata"]["source_manifest_sha256"] = "x" * 64
    elif failure == "frozen_hash":
        result["metadata"]["source_frozen_facts_sha256"] = "x" * 64
    elif failure == "config_hash":
        result["metadata"]["algorithm_config_sha256"] = "c" * 64
    elif failure == "listing_hash":
        result["metadata"]["listing_dates_sha256"] = "x" * 64
    elif failure == "interval":
        result["metadata"]["selected_interval"] = {"start_ms": 2000, "end_ms": 1000}
    elif failure == "event_hash":
        result["facts"]["points"][0]["event_ids_hash"] = "e" * 64
    elif failure == "event_count":
        result["facts"]["points"][0]["point_event_count"] = 2
    elif failure == "no_ready":
        result["facts"]["structures"] = []
    monkeypatch.setattr("mrs3.source_v6_surface.read_source_v6_analysis_run", lambda *_: result)
    template = tmp_path / "template.json"; template.write_text(json.dumps(_template()), encoding="utf-8")
    candidate_ids = [] if failure == "no_ready" else ["C_MISSING"]
    selected_scope = [("ETHUSDT", "LONG", "1h")] if failure == "wrong_scope" else [("BTCUSDT", "LONG", "1h")]
    with pytest.raises(ValueError):
        generate_v6_analysis_strategies("surface.duckdb", run_id, candidate_ids, selected_scope, template, tmp_path / "out", config)
