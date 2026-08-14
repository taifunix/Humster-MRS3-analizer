from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from mrs3.analysis_storage import ensure_analysis_schema
from mrs3.config import AlgorithmConfig
from mrs3.pipeline import PipelineInput


def _template() -> dict[str, object]:
    entry = {"id": 1, "side": "buy", "type": "SMA", "source": "ohlc4", "len": 3, "multiplier": 0.997, "lot_x": 1.0, "order_type": "limit", "post_only": True, "hidden": False, "value": None}
    return {"name": "TEMPLATE", "is_runing": True, "basic": {"strategy": "mrs3", "symbol": "OLD", "time_frame": "1h", "use_long": True, "use_short": True}, "mrs3": {"ma_long": [entry], "ma_short": [{**entry, "side": "sell", "multiplier": 1.003}], "ma_close_long": {"len": 4, "multiplier": 1.003, "side": "sell"}, "ma_close_short": {"len": 4, "multiplier": 0.997, "side": "buy"}}}


def test_generate_analysis_strategies_uses_only_ready_candidates_and_publishes_two_variants(tmp_path: Path, monkeypatch) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies, list_analysis_candidates

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    connection.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    ready = {"structure_id": "STR_1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "common_close_ma": 9, "order_count": 2, "status": "READY_MRS3_STRUCTURE", "orders": [{"id": 1, "plateau_id": "P1", "point_id": "BTCUSDT|LONG|1h|100|3|9", "open_ma": 3, "shift_bp": 100, "shift_pct": 1.0, "source_pnl_pct": 10.0, "source_dd_pct": 2.0, "source_efficiency": 5.0, "trades": 7, "close_support": 1.0, "standalone_eligible": True, "depth_eligible": True}, {"id": 2, "plateau_id": "P2", "point_id": "BTCUSDT|LONG|1h|200|4|9", "open_ma": 4, "shift_bp": 200, "shift_pct": 2.0, "source_pnl_pct": 11.0, "source_dd_pct": 2.0, "source_efficiency": 5.5, "trades": 8, "close_support": 1.0, "standalone_eligible": True, "depth_eligible": True}]}
    connection.execute("insert into candidates values ('C1', 'R1', 'S1', ?)", [json.dumps(ready)])
    points = pd.DataFrame([
        {"point_id": "BTCUSDT|LONG|1h|100|3|9", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "shift_bp": 100, "open_ma": 3, "close_ma": 9, "pnl_pct": 10.0, "dd_pct": 2.0, "trades": 7, "event_mode": "legacy_trades_proxy", "point_event_count": 7, "event_ids_hash": "legacy"},
        {"point_id": "BTCUSDT|LONG|1h|200|4|9", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "shift_bp": 200, "open_ma": 4, "close_ma": 9, "pnl_pct": 11.0, "dd_pct": 2.0, "trades": 8, "event_mode": "legacy_trades_proxy", "point_event_count": 8, "event_ids_hash": "legacy"},
    ])
    monkeypatch.setattr("mrs3.analysis_strategies.load_published_surface", lambda _connection, surface_id: PipelineInput(surface_id, points))
    template = tmp_path / "template.json"; template.write_text(json.dumps(_template()), encoding="utf-8")

    preview = list_analysis_candidates(connection, "R1")
    result = generate_analysis_strategies(connection, "R1", ["C1"], template, tmp_path / "generated", AlgorithmConfig.defaults())

    assert preview[0]["candidate_id"] == "C1" and preview[0]["orders"][0]["shift_bp"] == 100
    assert result.strategy_count == 2
    assert sorted(path.name for path in result.strategies_path.glob("*.json"))
    assert json.loads(result.manifest_path.read_text(encoding="utf-8"))["run_id"] == "R1"


def test_one_order_structures_selects_three_per_pair_side_timeframe() -> None:
    from mrs3.analysis_strategies import _one_order_structures

    connection = duckdb.connect(":memory:")
    connection.execute("create table plateaus(run_id varchar, plateau_id varchar, metrics_json varchar)")
    rows = []
    for timeframe in ("5m", "1h"):
        point_ids = []
        for index in range(4):
            point_id = f"ONUSDT|LONG|{timeframe}|{30 + index * 10}|3|5"
            point_ids.append(point_id)
            rows.append(
                {
                    "point_id": point_id,
                    "symbol": "ONUSDT",
                    "side": "LONG",
                    "timeframe": timeframe,
                    "shift_bp": 30 + index * 10,
                    "shift_pct": (30 + index * 10) / 100,
                    "open_ma": 3,
                    "close_ma": 5,
                    "pnl_pct": 10 + index,
                    "dd_pct": 5,
                    "trades": 100,
                    "point_event_count": 100,
                }
            )
        short_point_id = f"ONUSDT|SHORT|{timeframe}|90|3|5"
        point_ids.append(short_point_id)
        rows.append(
            {
                "point_id": short_point_id,
                "symbol": "ONUSDT",
                "side": "SHORT",
                "timeframe": timeframe,
                "shift_bp": 90,
                "shift_pct": 0.9,
                "open_ma": 3,
                "close_ma": 5,
                "pnl_pct": 100,
                "dd_pct": 1,
                "trades": 100,
                "point_event_count": 100,
            }
        )
        connection.execute(
            "insert into plateaus values ('R1', ?, ?)",
            [f"P_{timeframe}", json.dumps({"ready": True, "standalone_eligible_point_ids": point_ids})],
        )

    structures, selected = _one_order_structures(
        connection,
        "R1",
        pd.DataFrame(rows),
        AlgorithmConfig.defaults(),
        {("ONUSDT", "LONG", "5m"), ("ONUSDT", "LONG", "1h")},
    )

    assert len(structures) == 6
    assert selected.groupby(["symbol", "side", "timeframe"]).size().to_dict() == {
        ("ONUSDT", "LONG", "1h"): 3,
        ("ONUSDT", "LONG", "5m"): 3,
    }


def test_generate_analysis_strategies_rejects_candidate_deferred_by_active_filters(
    tmp_path: Path, monkeypatch,
) -> None:
    from mrs3 import analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    connection.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side, event_mode) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG', 'real_independent_events')")
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    candidate = {"structure_id": "STR_1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "common_close_ma": 9, "order_count": 1, "status": "READY_MRS3_STRUCTURE", "orders": []}
    connection.execute("insert into candidates values ('C1', 'R1', 'S1', ?)", [json.dumps(candidate)])
    monkeypatch.setattr(
        analysis_strategies,
        "filter_analysis_candidates",
        lambda *_args: SimpleNamespace(rows=({"candidate_id": "C1", "filter_status": "DEFERRED_REDUNDANT"},)),
        raising=False,
    )
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="deferred"):
        analysis_strategies.generate_analysis_strategies(
            connection, "R1", ["C1"], template, tmp_path / "out",
            AlgorithmConfig.defaults(), ("source_pnl",),
        )


def test_generate_analysis_strategies_rejects_mixed_ready_and_non_ready_selection(
    tmp_path: Path,
) -> None:
    from mrs3.analysis_strategies import generate_analysis_strategies

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    connection.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG')")
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    ready = {"structure_id": "STR_READY", "status": "READY_MRS3_STRUCTURE", "orders": []}
    rejected = {"structure_id": "STR_REJECTED", "status": "REJECTED", "orders": []}
    connection.executemany(
        "insert into candidates values (?, 'R1', 'S1', ?)",
        [("C_READY", json.dumps(ready)), ("C_REJECTED", json.dumps(rejected))],
    )
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="not READY"):
        generate_analysis_strategies(
            connection, "R1", ["C_READY", "C_REJECTED"], template,
            tmp_path / "out", AlgorithmConfig.defaults(),
        )
