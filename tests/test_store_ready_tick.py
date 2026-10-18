"""Tick-test batches from trade-store READY folders (Panel rework stage 1b).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 1b contract».
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mrs3.evaluation_protocol import EvaluationProtocolError, EvaluationTarget, require_protocol_v2_strategy
from mrs3.evaluation_protocol import tester_base as _tester_base
from mrs3.panel_fast_strategy_test import LocalFastStrategyTestService
from mrs3.panel_strategy_batch import validate_strategy_manifest
from mrs3.panel_testing import mrs3_tester_config_template
from mrs3.ready_store import build_ready
from mrs3.store_ready_tick import StoreReadyError, list_ready_folders, prepare_tick_batch, resolve_ready_folder
from mrs3.trade_store import point_id
from tests.test_ready_store import _store_with_periods

REPO = Path(__file__).parents[1]
TEMPLATE = REPO / "templates" / "exchanges" / "Bybit" / "mrs3-strategy.json"
TARGET = EvaluationTarget(_tester_base(mrs3_tester_config_template(REPO)), "Bybit")
NAME = "2026-10-18_simulation_101500"


def _ready(tmp_path: Path, symbols: tuple[str, ...] = ("GOODUSDT", "YOUNGUSDT")) -> tuple[Path, Path]:
    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 3, 35.0)
    _store_with_periods(tmp_path / "store", "YOUNGUSDT", 1, 30.0)
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    ready = tmp_path / "store" / "ready" / NAME
    build_ready(store, list(symbols), ready, template)
    return tmp_path / "store", ready


def _manifest(ready: Path) -> dict:
    return json.loads((ready / "ready_manifest.json").read_text(encoding="utf-8"))


def test_catalog_lists_ready_folders_with_counts(tmp_path: Path) -> None:
    store, ready = _ready(tmp_path)
    (store / "ready" / "not-a-ready-folder").mkdir()
    old = store / "ready" / "2026-10-17_simulation_090000"
    old.mkdir()
    document = _manifest(ready)
    for pair in document["pairs"]:
        for item in pair["structures"]:
            for order in item["orders"]:
                order.pop("zone_points")
    (old / "ready_manifest.json").write_text(json.dumps(document), encoding="utf-8")

    entries = list_ready_folders(store)

    assert [entry["name"] for entry in entries] == [NAME, old.name]            # newest first
    newest, older = entries
    assert {key: newest[key] for key in ("rule_version", "exchange", "pairs", "ready_pairs", "strategies", "testable")} == {
        "rule_version": "ready-v4-store-1", "exchange": "bybit", "pairs": 2, "ready_pairs": 1, "strategies": 1,
        "testable": True,
    }
    assert older["testable"] is False and "пересоберите READY" in older["reason"]
    assert list_ready_folders(tmp_path / "no-store") == []


def test_prepared_batch_passes_the_tick_test_checks(tmp_path: Path) -> None:
    _store, ready = _ready(tmp_path)

    result = prepare_tick_batch(ready, tmp_path / "out", TEMPLATE, TARGET)

    validated = validate_strategy_manifest(Path(result["manifest_path"]))
    assert validated.analysis_run_id == result["analysis_run_id"]
    assert len(result["analysis_run_id"]) == 64 and int(result["analysis_run_id"], 16) >= 0
    assert (result["strategy_count"], result["pairs"], result["ready_pairs"], result["reused"]) == (1, 2, 1, False)
    names = LocalFastStrategyTestService._expected(validated)
    LocalFastStrategyTestService._require_diagnostics(validated, names)
    [item] = _manifest(ready)["pairs"][0]["structures"]
    assert names == (item["strategy"],)                                      # same names as the READY folder
    document = json.loads((validated.strategy_source / f"{item['strategy']}.json").read_text(encoding="utf-8"))
    require_protocol_v2_strategy(document, TARGET)
    assert document["basic"]["my_fix_balance"] == 10000.0 and document["exchange"]["name"] == "Bybit"
    mapping = validated.provenance["candidate_identity_to_strategy_names"]
    assert mapping == {"bybit/GOODUSDT/15m/70bp2_110bp2": [item["strategy"]]}
    diagnostic = validated.provenance["candidate_diagnostics"]["bybit/GOODUSDT/15m/70bp2_110bp2"]
    assert diagnostic == {"order_count": 2, "orders": [
        {"order_id": 1, "plateau_id": f"zone:GOODUSDT:{point_id('15m', 70, 2, 2)}", "plateau_point_count": 8,
         "base_point_trades": 5, "plateau_total_trades": 40},
        {"order_id": 2, "plateau_id": f"zone:GOODUSDT:{point_id('15m', 110, 2, 2)}", "plateau_point_count": 12,
         "base_point_trades": 5, "plateau_total_trades": 60},
    ]}
    assert validated.provenance["ready_folder"] == NAME


def test_prepare_is_idempotent_and_never_overwrites_a_batch(tmp_path: Path) -> None:
    _store, ready = _ready(tmp_path)
    first = prepare_tick_batch(ready, tmp_path / "out", TEMPLATE, TARGET)
    again = prepare_tick_batch(ready, tmp_path / "out", TEMPLATE, TARGET)
    assert again["analysis_run_id"] == first["analysis_run_id"] and again["reused"] is True
    assert [path.name for path in (tmp_path / "out").iterdir()] == [first["analysis_run_id"]]

    strategy = next((Path(first["manifest_path"]).parent / "strategies").glob("*.json"))
    strategy.write_text(strategy.read_text(encoding="utf-8").replace("GOODUSDT", "EVILUSDT"), encoding="utf-8")
    with pytest.raises(StoreReadyError, match="damaged"):
        prepare_tick_batch(ready, tmp_path / "out", TEMPLATE, TARGET)

    other = EvaluationTarget(TARGET.base * 2, "Bybit")                       # another base: another batch
    assert prepare_tick_batch(ready, tmp_path / "out", TEMPLATE, other)["analysis_run_id"] != first["analysis_run_id"]


def test_folder_without_zone_facts_or_strategies_is_refused(tmp_path: Path) -> None:
    _store, ready = _ready(tmp_path)
    document = _manifest(ready)
    for order in document["pairs"][0]["structures"][0]["orders"]:
        del order["zone_trades"]
    (ready / "ready_manifest.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(StoreReadyError, match="пересоберите READY"):
        prepare_tick_batch(ready, tmp_path / "out", TEMPLATE, TARGET)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())

    _store2, young = _ready(tmp_path / "young", ("YOUNGUSDT",))
    with pytest.raises(StoreReadyError, match="нет стратегий"):
        prepare_tick_batch(young, tmp_path / "out", TEMPLATE, TARGET)


def test_protocol_v2_refusal_publishes_nothing(tmp_path: Path) -> None:
    _store, ready = _ready(tmp_path)
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    template["basic"]["use_fix"] = False
    broken = tmp_path / "broken-template.json"
    broken.write_text(json.dumps(template), encoding="utf-8")
    with pytest.raises(EvaluationProtocolError):
        prepare_tick_batch(ready, tmp_path / "out", broken, TARGET)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "/abs", "C:\\x", "missing", "not-a-ready-folder"])
def test_unsafe_or_unknown_folder_names_are_refused(tmp_path: Path, name: str) -> None:
    store, _ready_dir = _ready(tmp_path)
    (store / "ready" / "not-a-ready-folder").mkdir()
    with pytest.raises(StoreReadyError):
        resolve_ready_folder(store, name)
    assert resolve_ready_folder(store, NAME) == (store / "ready" / NAME).resolve()


def test_a_malformed_folder_is_flagged_not_fatal(tmp_path: Path) -> None:
    # review of stage 1b part A: one damaged folder must not break the catalog of all folders
    store, ready = _ready(tmp_path)
    broken = store / "ready" / "2026-10-19_simulation_000000"
    broken.mkdir()
    document = _manifest(ready)
    document["pairs"][0]["structures"] = ["oops"]
    (broken / "ready_manifest.json").write_text(json.dumps(document), encoding="utf-8")

    entries = {entry["name"]: entry for entry in list_ready_folders(store)}

    assert entries[NAME]["testable"] is True
    assert entries[broken.name]["testable"] is False and "повреждён" in entries[broken.name]["reason"]
    with pytest.raises(StoreReadyError, match="повреждён"):
        prepare_tick_batch(broken, tmp_path / "out", TEMPLATE, TARGET)
    missing_key = _manifest(ready)
    del missing_key["pairs"][0]["structures"][0]["orders"][0]["point_id"]
    (broken / "ready_manifest.json").write_text(json.dumps(missing_key), encoding="utf-8")
    with pytest.raises(StoreReadyError, match="повреждён"):
        prepare_tick_batch(broken, tmp_path / "out", TEMPLATE, TARGET)
