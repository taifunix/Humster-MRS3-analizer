"""Every strategy name carries the tag of its exchange: BYBIT_..., BINGX_..., and any exchange added later.

Contract: docs/decisions/0084-exchange-tag-on-every-strategy-name.md (amends ADR-0083, decision 5). Temporary folders
and fakes only.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from mrs3.evaluation_protocol import EvaluationTarget, require_protocol_v2_strategy
from mrs3.evaluation_protocol import tester_base as _tester_base
from mrs3.mrs2_simulator import RunSettings
from mrs3.panel_strategy_batch import validate_strategy_manifest
from mrs3.performance_v2_input import adapt_strategy_identity
from mrs3.performance_v2_store import KNOWN_EXCHANGES, exchange_name_tag, exchange_needs_api_symbol
from mrs3.ready_store import build_ready
from mrs3.store_ready_tick import StoreReadyError, list_ready_folders, prepare_tick_batch
from mrs3.trade_store import HOURLY_SCHEMA, TRADE_SCHEMA, SegmentKey, SegmentTables, TradeStore
from tests.test_ready_store import SHIFTS_BP, _metrics

REPO = Path(__file__).parents[1]
BYBIT_TEMPLATE = REPO / "templates" / "exchanges" / "Bybit" / "mrs3-strategy.json"
BYBIT_TARGET = EvaluationTarget(_tester_base(REPO / "templates" / "exchanges" / "Bybit" / "mrs3-tester.json"), "Bybit")


def _ready(tmp_path: Path, exchange: str = "bybit") -> Path:
    store = TradeStore(tmp_path / "store")
    first = date(2026, 7, 1)
    for index in range(3):
        start, end = first + timedelta(days=20 * index), first + timedelta(days=20 * (index + 1))
        tables = SegmentTables(TRADE_SCHEMA.empty_table(), HOURLY_SCHEMA.empty_table(),
                               _metrics({shift: 35.0 for shift in SHIFTS_BP}, 20))
        store.register(store.write_files(SegmentKey(exchange, "GOODUSDT", "simulator", start, end, "eng"), tables,
                                         settings=RunSettings(), tick_size=0.01, qty_step=0.01))
    ready = tmp_path / "store" / "ready" / "2026-10-20_ready_120000"
    build_ready(store, ["GOODUSDT"], ready, json.loads(BYBIT_TEMPLATE.read_text(encoding="utf-8")), exchange=exchange)
    return ready


def test_every_known_exchange_has_its_own_tag() -> None:
    assert {exchange: exchange_name_tag(exchange) for exchange in KNOWN_EXCHANGES} == {
        exchange: f"{exchange.upper()}_" for exchange in KNOWN_EXCHANGES}
    assert (exchange_name_tag("bybit"), exchange_name_tag(" BingX ")) == ("BYBIT_", "BINGX_")
    with pytest.raises(ValueError):
        exchange_name_tag("kraken")


def test_only_an_exchange_whose_tester_symbol_differs_needs_the_api_symbol() -> None:
    assert [exchange_needs_api_symbol(exchange) for exchange in ("bybit", "Bybit", "bingx", "BingX")] == [
        False, False, True, True]
    with pytest.raises(ValueError):
        exchange_needs_api_symbol("kraken")


def test_bybit_ready_names_carry_the_tag(tmp_path: Path) -> None:
    ready = _ready(tmp_path)
    [pair] = json.loads((ready / "ready_manifest.json").read_text(encoding="utf-8"))["pairs"]
    [item] = pair["structures"]
    assert item["strategy"].startswith("BYBIT_GOODUSDT_")
    strategy = json.loads((ready / "strategies" / f"{item['strategy']}.json").read_text(encoding="utf-8"))
    assert strategy["name"] == item["strategy"] and strategy["basic"]["symbol"] == "GOODUSDT"


def test_the_bybit_batch_keeps_the_tag_and_the_human_ticker_and_needs_no_api_symbol(tmp_path: Path) -> None:
    ready = _ready(tmp_path)
    result = prepare_tick_batch(ready, tmp_path / "out", BYBIT_TEMPLATE, BYBIT_TARGET)
    validated = validate_strategy_manifest(Path(result["manifest_path"]))
    [path] = list(Path(validated.strategy_source).glob("*.json"))
    strategy = json.loads(path.read_text(encoding="utf-8"))
    assert strategy["name"].startswith("BYBIT_GOODUSDT_") and path.stem == strategy["name"]
    assert strategy["basic"]["symbol"] == "GOODUSDT"
    require_protocol_v2_strategy(strategy, BYBIT_TARGET)
    assert adapt_strategy_identity(strategy).symbol == "GOODUSDT"


def test_an_untagged_bybit_ready_folder_asks_for_a_rebuild(tmp_path: Path) -> None:
    ready = _ready(tmp_path)
    manifest = ready / "ready_manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    for pair in document["pairs"]:
        for item in pair["structures"]:
            item["strategy"] = item["strategy"].removeprefix("BYBIT_")        # a folder built before ADR-0084
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(StoreReadyError, match="пересоберите READY"):
        prepare_tick_batch(ready, tmp_path / "out", BYBIT_TEMPLATE, BYBIT_TARGET)


def test_an_untagged_ready_folder_is_listed_as_not_testable(tmp_path: Path) -> None:
    ready = _ready(tmp_path)
    [tagged] = list_ready_folders(tmp_path / "store")
    assert tagged["testable"] is True
    manifest = ready / "ready_manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    for pair in document["pairs"]:
        for item in pair["structures"]:
            item["strategy"] = item["strategy"].removeprefix("BYBIT_")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    [untagged] = list_ready_folders(tmp_path / "store")
    assert untagged["testable"] is False and "пересоберите READY" in untagged["reason"]
