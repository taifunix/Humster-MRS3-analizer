from __future__ import annotations

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
        "close_support": 1.0,
        "standalone_eligible": True,
        "depth_eligible": True,
    }


def _make_analysis(path: Path, *, event_mode: str = "real_independent_events", ready: bool = True) -> tuple[str, dict[str, object]]:
    config = AlgorithmConfig.defaults()
    config_hash = sha256(_canonical_json(_canonical(config)).encode()).hexdigest()
    surface_identity = {
        "surface_id": "SURFACE-1",
        "surface_fingerprint": "surface-v6-fresh-compact-v1",
        "source_content_digest": "a" * 64,
        "scope_digests": {"BTCUSDT|LONG|1h": "d" * 64},
    }
    identity = {
        "fingerprint": "analysis-v6-fresh-compact-v1",
        **surface_identity,
        "algorithm_version": "algo-v1",
        "algorithm_config_sha256": config_hash,
        "listing_dates_sha256": "b" * 64,
        "event_mode": event_mode,
    }
    analysis_id = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    point_a = _point("BTCUSDT|LONG|1h|100|3|9", 100, 3, "event-a")
    point_b = _point("BTCUSDT|LONG|1h|300|4|9", 300, 4, "event-b")
    structure = {
        "structure_id": "STR-READY",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "common_close_ma": 9,
        "order_count": 2,
        "orders": [_order(point_a, 1), _order(point_b, 2)],
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
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.strategy_count == 2
    assert manifest["analysis_id"] == analysis_id
    assert manifest["analysis_run_id"] == analysis_id
    assert manifest["source_surface_id"] == surface["surface_id"]
    assert manifest["source_content_digest"] == surface["source_content_digest"]
    assert manifest["scope_digests"] == surface["scope_digests"]
    assert manifest["candidate_identities"] == ["STR-READY"]
    assert len(manifest["analysis_artifact_sha256"]) == 64
    assert len(manifest["analysis_manifest_sha256"]) == 64
    strategy = json.loads(next(result.strategies_path.glob("*.json")).read_text(encoding="utf-8"))
    assert strategy["provenance"]["candidate_identity"] == "STR-READY"
    assert strategy["provenance"]["analysis_id"] == analysis_id


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

    assert result == {"analysis_run_id": analysis_id, "items": [{
        "candidate_id": "STR-READY", "pair": "BTCUSDT", "side": "LONG",
        "timeframe": "1h", "order_count": 2, "status": "READY_MRS3_STRUCTURE",
    }]}
