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


def _fresh_selection(
    path: Path, analysis_id: str, options: tuple[bool, bool, bool] = (False, False, False),
):
    from mrs3.fresh_shortlist import FreshShortlistExecutor

    return FreshShortlistExecutor().evaluate(path, analysis_id, options, workers=1)


def _fresh_shortlist(path: Path, analysis_id: str) -> dict[str, object]:
    from mrs3.fresh_analysis_strategies import fresh_shortlist_response
    from mrs3.fresh_shortlist import FreshShortlistExecutor

    prepared, evaluation = FreshShortlistExecutor().evaluate_with_prepared(
        path, analysis_id, (False, False, False), workers=1,
    )
    return fresh_shortlist_response(prepared, evaluation)


def _change_structure_pnl_same_size(path: Path) -> bool:
    connection = duckdb.connect(str(path))
    try:
        scope, raw = connection.execute("select scope_key, payload_json from structures").fetchone()
        structure = json.loads(raw)
        structure["orders"][0]["source_pnl_pct"] = 11.0
        old_size = path.stat().st_size
        connection.execute(
            "update structures set payload_json=? where scope_key=?",
            [json.dumps(structure, sort_keys=True, separators=(",", ":")), scope],
        )
        return path.stat().st_size == old_size
    finally:
        connection.close()


def _replace_candidate_and_point_ids(path: Path, *, numeric: bool) -> None:
    connection = duckdb.connect(str(path))
    try:
        rows = connection.execute("select rowid, payload_json from points order by rowid").fetchall()
        point_ids: dict[str, object] = {}
        for index, (rowid, raw) in enumerate(rows, start=1):
            point = json.loads(raw)
            old_id = str(point["point_id"])
            new_id: object = float(index * 100) if numeric else f" {old_id} "
            point_ids[old_id] = new_id
            point["point_id"] = new_id
            connection.execute(
                "update points set payload_json=? where rowid=?",
                [json.dumps(point, sort_keys=True, separators=(",", ":")), rowid],
            )
        rows = connection.execute("select rowid, payload_json from structures").fetchall()
        for rowid, raw in rows:
            structure = json.loads(raw)
            structure_id: object = 7.0 if numeric else f" {structure['structure_id']} "
            structure["structure_id"] = structure_id
            structure["candidate_id"] = structure_id
            for order in structure["orders"]:
                order["point_id"] = point_ids[str(order["point_id"])]
            connection.execute(
                "update structures set payload_json=? where rowid=?",
                [json.dumps(structure, sort_keys=True, separators=(",", ":")), rowid],
            )
    finally:
        connection.close()


def test_retired_fresh_pareto_adapters_are_not_exposed() -> None:
    import mrs3.fresh_analysis_strategies as fresh_strategies

    assert not hasattr(fresh_strategies, "filter_fresh_analysis_candidates")
    assert not hasattr(fresh_strategies, "list_fresh_analysis_shortlist")


@pytest.mark.parametrize("consumer", ["loader", "generator"])
def test_fresh_consumer_rejects_same_size_artifact_change_before_candidate_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: str,
) -> None:
    from mrs3 import fresh_shortlist
    from mrs3.fresh_analysis_strategies import (
        generate_fresh_analysis_strategies,
        load_fresh_ready_candidates,
    )

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    selection = _fresh_selection(database, analysis_id)
    assert _change_structure_pnl_same_size(database)

    def unexpected_parse(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("candidate payload parsed before stale digest rejection")

    monkeypatch.setattr(fresh_shortlist, "_canonical_id", unexpected_parse)
    scopes = [("BTCUSDT", "LONG", "1h")]
    if consumer == "loader":
        operation = lambda: load_fresh_ready_candidates(
            database, analysis_id, selection.ready_candidate_ids, scopes,
            expected_artifact_sha256=selection.artifact_sha256,
        )
    else:
        template = tmp_path / "template.json"
        template.write_text(json.dumps(_template()), encoding="utf-8")
        operation = lambda: generate_fresh_analysis_strategies(
            database, analysis_id, selection.ready_candidate_ids, scopes,
            template, tmp_path / "out", AlgorithmConfig.defaults(), selection=selection,
        )

    with pytest.raises(ValueError, match="STALE_SHORTLIST_SELECTION"):
        operation()
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("numeric", [False, True], ids=["padded-strings", "integral-json-floats"])
def test_fresh_generation_accepts_canonicalized_candidate_and_point_ids(
    tmp_path: Path, numeric: bool,
) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    _replace_candidate_and_point_ids(database, numeric=numeric)
    selection = _fresh_selection(database, analysis_id)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    result = generate_fresh_analysis_strategies(
        database, analysis_id, selection.ready_candidate_ids, [("BTCUSDT", "LONG", "1h")],
        template, tmp_path / "out", AlgorithmConfig.defaults(), selection=selection,
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["candidate_identities"] == (["7"] if numeric else ["STR-READY"])
    assert set(manifest["candidate_identity_to_strategy_names"]) == set(manifest["candidate_identities"])


def test_fresh_json_generator_requires_verified_shortlist_selection(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    with pytest.raises(ValueError, match="verified shortlist selection"):
        generate_fresh_analysis_strategies(
            tmp_path / "missing.analysis-v6.duckdb", "a" * 64, ["client-id"],
            [("BTCUSDT", "LONG", "1h")], tmp_path / "template.json", tmp_path / "out",
            AlgorithmConfig.defaults(), selection=None,
        )
    assert not (tmp_path / "out").exists()


def test_fresh_json_generator_binds_min_shift_settings_to_selection(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies
    from mrs3.fresh_shortlist import FreshShortlistExecutor

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    selection = FreshShortlistExecutor().evaluate(
        database, analysis_id, (False, False, False), workers=1,
        min_shift_enabled=True, min_shift_pct="0.3",
    )
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    common = (database, analysis_id, selection.ready_candidate_ids, [("BTCUSDT", "LONG", "1h")], template, tmp_path / "out", AlgorithmConfig.defaults())
    with pytest.raises(ValueError, match="settings are required"):
        generate_fresh_analysis_strategies(*common, selection=selection)
    with pytest.raises(ValueError, match="supplied together"):
        generate_fresh_analysis_strategies(*common, min_shift_enabled=True, selection=selection)
    with pytest.raises(ValueError, match="options disagree"):
        generate_fresh_analysis_strategies(
            *common, min_shift_enabled=True, min_shift_pct="1.000", selection=selection,
        )
    result = generate_fresh_analysis_strategies(
        *common, min_shift_enabled=True, min_shift_pct="0.3", selection=selection,
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["shortlist_v2"]["applied_options"]["min_shift_pct"] == "0.300"


def test_legacy_analysis_works_without_pretest_and_requires_rebuild_with_it(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    path = tmp_path / "legacy.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(path, legacy=True)

    shortlist = _fresh_selection(path, analysis_id)
    assert shortlist.ready_candidate_ids == ("STR-READY",)
    with pytest.raises(ValueError, match="PRETEST_AB_EVIDENCE_UNAVAILABLE"):
        _fresh_selection(path, analysis_id, (True, False, False))

    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    generated = generate_fresh_analysis_strategies(
        path, analysis_id, ["STR-READY"], [("BTCUSDT", "LONG", "1h")], template,
        tmp_path / "legacy-out", AlgorithmConfig.defaults(), filters={},
        selection=_fresh_selection(path, analysis_id),
    )
    manifest = json.loads(generated.manifest_path.read_text(encoding="utf-8"))
    assert "analysis_input_digest" not in manifest

    blocked = tmp_path / "blocked-out"
    with pytest.raises(ValueError, match="PRETEST_AB_EVIDENCE_UNAVAILABLE"):
        _fresh_selection(path, analysis_id, (True, False, False))
    assert not blocked.exists()


def test_fresh_adapter_generates_only_selected_ready_candidate_and_binds_hashes(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import GENERATOR_SCHEMA, generate_fresh_analysis_strategies

    analysis_id, surface = _make_analysis(tmp_path / "run.analysis-v6.duckdb")
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    selection = _fresh_selection(tmp_path / "run.analysis-v6.duckdb", analysis_id, (True, False, False))

    result = generate_fresh_analysis_strategies(
        tmp_path / "run.analysis-v6.duckdb",
        analysis_id,
        ["STR-READY"],
        [("BTCUSDT", "LONG", "1h")],
        template,
        tmp_path / "out",
        AlgorithmConfig.defaults(),
        pretest_ab_enabled=True,
        selection=selection,
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.strategy_count == 2
    assert manifest["analysis_id"] == analysis_id
    assert manifest["analysis_run_id"] == analysis_id
    assert manifest["source_surface_id"] == surface["surface_id"]
    assert manifest["source_content_digest"] == surface["source_content_digest"]
    assert manifest["scope_digests"] == surface["scope_digests"]
    assert manifest["analysis_input_digest"] == "c" * 64
    assert manifest["generator_schema_version"] == GENERATOR_SCHEMA
    assert GENERATOR_SCHEMA.endswith("-shortlist-v2")
    assert manifest["candidate_identities"] == ["STR-READY"]
    assert manifest["shortlist_v2"] == {
        "filter_version": "shortlist-v2",
        "filter_engine_version": selection.filter_engine_version,
        "selection_token": selection.selection_token,
        "artifact_sha256": selection.artifact_sha256,
        "applied_options": {
            "pretest_ab_enabled": True, "ladder_enabled": False, "pareto_enabled": False,
        },
        "selected_candidate_ids": ["STR-READY"],
    }
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
        selection=_fresh_selection(tmp_path / "run.analysis-v6.duckdb", analysis_id),
    )

    assert result.strategy_count == 2


def test_fresh_generation_rejects_candidate_ids_outside_verified_ready_scope(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate IDs do not match verified READY selection"):
        generate_fresh_analysis_strategies(
            database, analysis_id, ["NOT-SELECTED"], [("BTCUSDT", "LONG", "1h")],
            template, tmp_path / "out", AlgorithmConfig.defaults(),
            selection=_fresh_selection(database, analysis_id),
        )
    assert not (tmp_path / "out").exists()


def test_fresh_generation_ignores_numeric_sql_overmatch_for_adjacent_large_point_ids(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    connection = duckdb.connect(str(database))
    try:
        point_rowid, raw = connection.execute(
            "select rowid, payload_json from points order by rowid limit 1",
        ).fetchone()
        point = json.loads(raw)
        point["point_id"] = 9007199254740992.0
        connection.execute(
            "update points set payload_json=? where rowid=?",
            [json.dumps(point, sort_keys=True, separators=(",", ":")), point_rowid],
        )
        structure_rowid, raw = connection.execute(
            "select rowid, payload_json from structures limit 1",
        ).fetchone()
        structure = json.loads(raw)
        structure["orders"][0]["point_id"] = 9007199254740992.0
        connection.execute(
            "update structures set payload_json=? where rowid=?",
            [json.dumps(structure, sort_keys=True, separators=(",", ":")), structure_rowid],
        )
        adjacent = _point("unused", 500, 5, "event-adjacent")
        adjacent["point_id"] = 9007199254740993
        connection.execute(
            "insert into points values (?, ?)",
            ["BTCUSDT|LONG|1h", json.dumps(adjacent, sort_keys=True, separators=(",", ":"))],
        )
    finally:
        connection.close()
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    selection = _fresh_selection(database, analysis_id)

    result = generate_fresh_analysis_strategies(
        database, analysis_id, ["STR-READY"], [("BTCUSDT", "LONG", "1h")],
        template, tmp_path / "out", AlgorithmConfig.defaults(), selection=selection,
    )

    assert result.strategy_count == 2


def test_panel_discards_staged_generation_if_artifact_changes_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies
    from mrs3.panel import PanelController

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({
        "panel_workflow": {"strategy_templates": {"LONG": "template.json"}},
    }), encoding="utf-8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _path: AlgorithmConfig.defaults())
    controller._fresh_analysis_paths[analysis_id] = database
    snapshot = controller.strategies_fresh_shortlist({
        "analysis_run_id": analysis_id, "filter_version": "shortlist-v2",
        "pretest_ab_enabled": False, "ladder_enabled": False, "pareto_enabled": False,
    })
    original_generate = generate_fresh_analysis_strategies

    def generate_then_change_source(*args: object, **kwargs: object):
        result = original_generate(*args, **kwargs)
        assert result.manifest_path.is_file()
        assert list(result.strategies_path.glob("*.json"))
        assert _change_structure_pnl_same_size(database)
        return result

    monkeypatch.setattr("mrs3.panel.generate_fresh_analysis_strategies", generate_then_change_source)
    output_root = tmp_path / "Output" / "fresh-shortlist-v2" / analysis_id
    with pytest.raises(ValueError, match="STALE_SHORTLIST_SELECTION"):
        controller._generate_fresh_strategies({
            "analysis_run_id": analysis_id, "filter_version": "shortlist-v2",
            "pretest_ab_enabled": False, "ladder_enabled": False, "pareto_enabled": False,
            "selection_token": snapshot["selection_token"],
            "selected_scopes": [["BTCUSDT", "LONG", "1h"]],
        })

    assert output_root.is_dir()
    assert list(output_root.iterdir()) == []
    assert not list(output_root.rglob("strategy_manifest.json"))


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
        selection=_fresh_selection(tmp_path / "run.analysis-v6.duckdb", analysis_id),
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
        selection=_fresh_selection(tmp_path / "run.analysis-v6.duckdb", analysis_id),
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
    from mrs3.panel_strategy_batch import validate_strategy_manifest

    assert validate_strategy_manifest(result.manifest_path).provenance["shortlist_v2"]["selection_token"] == manifest[
        "shortlist_v2"
    ]["selection_token"]


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


def test_fresh_generation_uses_all_server_ready_ids_in_selected_scope(tmp_path: Path) -> None:
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
    selection = _fresh_selection(database, analysis_id)

    with pytest.raises(ValueError, match="candidate IDs do not match verified READY selection"):
        generate_fresh_analysis_strategies(
            database, analysis_id, ["BASE-READY"], [("BTCUSDT", "LONG", "1h")],
            template, tmp_path / "partial", AlgorithmConfig.defaults(), selection=selection,
        )

    result = generate_fresh_analysis_strategies(
        database,
        analysis_id,
        ["BASE-READY", "STR-READY"],
        [("BTCUSDT", "LONG", "1h")],
        template,
        tmp_path / "out",
        AlgorithmConfig.defaults(),
        selection=selection,
    )

    assert result.strategy_count == 3
    assert {path.name for path in result.strategies_path.glob("*.json")} == {
        "BTCUSDT_1h_LONG_1ORD_CMA9_BASE-READY_EQUAL.json",
        "BTCUSDT_1h_LONG_2ORD_CMA9_STR-READY_EQUAL.json",
        "BTCUSDT_1h_LONG_2ORD_CMA9_STR-READY_INCOME.json",
    }


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
            selection=_fresh_selection(database, analysis_id),
        )


@pytest.mark.parametrize("event_mode", ["legacy_trades_proxy", "mixed"])
def test_fresh_adapter_rejects_non_independent_event_mode(tmp_path: Path, event_mode: str) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    analysis_id, _ = _make_analysis(tmp_path / "run.analysis-v6.duckdb", event_mode=event_mode)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    with pytest.raises(ValueError, match="real_independent_events"):
        _fresh_selection(tmp_path / "run.analysis-v6.duckdb", analysis_id)


def test_fresh_adapter_rejects_unready_or_unselected_candidate(tmp_path: Path) -> None:
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    analysis_id, _ = _make_analysis(tmp_path / "run.analysis-v6.duckdb", ready=False)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    selection = _fresh_selection(tmp_path / "run.analysis-v6.duckdb", analysis_id)
    with pytest.raises(ValueError, match="EMPTY_READY_SELECTION"):
        generate_fresh_analysis_strategies(
            tmp_path / "run.analysis-v6.duckdb", analysis_id, ["STR-READY"], [("BTCUSDT", "LONG", "1h")],
            template, tmp_path / "out", AlgorithmConfig.defaults(), selection=selection,
        )


def test_fresh_shortlist_returns_only_safe_candidate_summary(tmp_path: Path) -> None:
    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _ = _make_analysis(database)
    result = _fresh_shortlist(database, analysis_id)

    assert result["analysis_run_id"] == analysis_id
    assert result["filter_version"] == "shortlist-v2"
    assert result["items"][0]["candidate_id"] == "STR-READY"
    assert result["items"][0]["filter_status"] == "READY_AFTER_FILTERS"
    # The grouped view carries counts only; no order, point or lot detail leaks.
    group = result["groups"][0]
    assert group["counts"] == {"1ORD": 0, "2ORD": 1, "3ORD": 0, "4ORD": 0}
    assert group["ready"] == group["ready_after_filters"] == group["total"] == 1
    assert group["candidate_ids"] == ["STR-READY"]
    assert not ({"orders", "points", "lot", "pnl_pct"} & set(group))
