from __future__ import annotations

from decimal import Decimal
import gzip
import json
from pathlib import Path

import pytest

import mrs3.bybit_collector.reference as reference_module
from mrs3.bybit_collector.reference import (
    ReferenceDataCollector,
    ReferenceDataError,
)


def instrument(symbol: str, *, status: str = "Trading", tick_size: str = "0.1") -> dict:
    return {
        "symbol": symbol,
        "status": status,
        "symbolType": "",
        "contractType": "LinearPerpetual",
        "launchTime": "1700000000000",
        "settleCoin": "USDT",
        "priceFilter": {"tickSize": tick_size},
        "lotSizeFilter": {
            "qtyStep": "0.001",
            "minOrderQty": "0.001",
            "maxOrderQty": "100",
            "maxMktOrderQty": "50",
            "minNotionalValue": "5",
        },
        "leverageFilter": {
            "minLeverage": "1",
            "maxLeverage": "100",
            "leverageStep": "0.01",
        },
        "fundingInterval": "480",
        "upperFundingRate": "0.003",
        "lowerFundingRate": "-0.003",
        "fullName": "",
        "marketRegion": "",
        "underlyingTicker": "",
    }


def risk(symbol: str, *, risk_id: str = "1", mm_deduction: str = "0") -> dict:
    return {
        "id": risk_id,
        "symbol": symbol,
        "riskLimitValue": "2000000",
        "maintenanceMargin": "0.005",
        "initialMargin": "0.01",
        "maxLeverage": "100",
        "mmDeduction": mm_deduction,
        "isLowestRisk": 1,
    }


def response(items: list[dict], cursor: str = "") -> dict:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": "linear", "list": items, "nextPageCursor": cursor},
    }


def test_collects_symbol_specific_paginated_reference_data(tmp_path: Path) -> None:
    pages = {
        ("instruments-info", "BTCUSDT", None): response(
            [instrument("BTCUSDT")], "instrument-next"
        ),
        ("instruments-info", "BTCUSDT", "instrument-next"): response(
            [instrument("BTCUSDT", tick_size="0.2")]
        ),
        ("risk-limit", "BTCUSDT", None): response([risk("BTCUSDT")]),
    }
    calls: list[tuple[str, dict[str, str]]] = []

    def fetcher(feed: str, params: dict[str, str]) -> dict:
        calls.append((feed, params.copy()))
        return pages[(feed, params["symbol"], params.get("cursor"))]

    result = ReferenceDataCollector(tmp_path, fetcher=fetcher).collect(
        ["BTCUSDT"], captured_at_ms=1_756_000_000_000
    )

    assert len(result.instruments) == 2
    assert result.instruments[0]["tick_size"] == Decimal("0.1")
    assert result.instruments[1]["tick_size"] == Decimal("0.2")
    assert len(result.risk_limits) == 1
    assert result.published_files[0].suffix == ".parquet"
    assert result.instruments[0]["launch_time_ms"] == 1_700_000_000_000
    assert calls == [
        ("instruments-info", {"category": "linear", "symbol": "BTCUSDT"}),
        (
            "instruments-info",
            {"category": "linear", "symbol": "BTCUSDT", "cursor": "instrument-next"},
        ),
        ("risk-limit", {"category": "linear", "symbol": "BTCUSDT"}),
    ]


def test_rejects_malformed_rest_envelope(tmp_path: Path) -> None:
    def fetcher(feed: str, params: dict[str, str]) -> dict:
        return {"retCode": 10001, "retMsg": "bad request", "result": {}}

    with pytest.raises(ReferenceDataError, match="retCode"):
        ReferenceDataCollector(tmp_path, fetcher=fetcher).collect(
            ["BTCUSDT"], captured_at_ms=1_756_000_000_000
        )


def test_normalizes_bybit_empty_risk_deduction_as_zero(tmp_path: Path) -> None:
    def fetcher(feed: str, _params: dict[str, str]) -> dict:
        return response([instrument("BTCUSDT")]) if feed == "instruments-info" else response(
            [risk("BTCUSDT", mm_deduction="")]
        )

    result = ReferenceDataCollector(tmp_path, fetcher=fetcher).collect(
        ["BTCUSDT"], captured_at_ms=1_756_000_000_000
    )
    assert result.risk_limits[0]["mm_deduction"] == Decimal("0")


def test_saves_each_rest_response_as_gzip_json_without_sidecars(tmp_path: Path) -> None:
    payload = response([instrument("BTCUSDT")])
    risk_payload = response([risk("BTCUSDT")])

    def fetcher(feed: str, _params: dict[str, str]) -> dict:
        return payload if feed == "instruments-info" else risk_payload

    result = ReferenceDataCollector(
        tmp_path, fetcher=fetcher
    ).collect(["BTCUSDT"], captured_at_ms=1_756_000_000_000)

    raw_files = list((tmp_path / "raw_reference").rglob("*.json.gz"))
    assert raw_files == list(result.raw_files)
    assert raw_files[0].name.startswith("BTCUSDT_")
    with gzip.open(raw_files[0], "rt", encoding="utf-8") as stream:
        assert json.load(stream) == payload
    assert not list((tmp_path / "raw_reference").rglob("*.sha256"))


def test_truncated_raw_gzip_is_rewritten_through_sibling_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = response([instrument("BTCUSDT")])
    risk_payload = response([risk("BTCUSDT")])

    def fetcher(feed: str, _params: dict[str, str]) -> dict:
        return payload if feed == "instruments-info" else risk_payload

    collector = ReferenceDataCollector(tmp_path, fetcher=fetcher)
    first = collector.collect(["BTCUSDT"], captured_at_ms=1_756_000_000_000)
    raw_path = first.raw_files[0]
    raw_path.write_bytes(b"truncated gzip")

    replacements: list[tuple[str, str]] = []
    real_replace = reference_module.os.replace

    def replace(source: str, destination: str) -> None:
        if destination.endswith(".json.gz"):
            replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(reference_module.os, "replace", replace)
    collector.collect(["BTCUSDT"], captured_at_ms=1_756_000_000_000)

    with gzip.open(raw_path, "rt", encoding="utf-8") as stream:
        assert json.load(stream) == payload
    assert len(replacements) == 1
    source, destination = replacements[0]
    assert Path(source).parent == raw_path.parent
    assert source.endswith(".tmp")
    assert destination == str(raw_path)
    assert not list(raw_path.parent.glob("*.tmp"))


def test_daily_publication_is_atomic_and_immutable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = response([instrument("BTCUSDT")])
    risk_payload = response([risk("BTCUSDT")])
    replacements: list[tuple[str, str]] = []
    real_replace = reference_module.os.replace

    def replace(source: str, destination: str) -> None:
        if destination.endswith(".parquet"):
            replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(reference_module.os, "replace", replace)
    def fetcher(feed: str, _params: dict[str, str]) -> dict:
        return payload if feed == "instruments-info" else risk_payload

    collector = ReferenceDataCollector(tmp_path, fetcher=fetcher)
    first = collector.collect(["BTCUSDT"], captured_at_ms=1_756_000_000_000)
    published = first.published_files[0]
    original = published.read_bytes()

    changed = response([instrument("BTCUSDT", status="PreLaunch")])
    collector.fetcher = lambda feed, _params: changed if feed == "instruments-info" else risk_payload
    collector.collect(["BTCUSDT"], captured_at_ms=1_756_000_000_000)

    assert len(replacements) == 2
    assert published.read_bytes() == original
    assert not list((tmp_path / "reference").rglob("*.tmp"))


def test_symbol_events_report_added_removed_and_changed_symbols(tmp_path: Path) -> None:
    current = {"BTCUSDT": instrument("BTCUSDT"), "ETHUSDT": instrument("ETHUSDT")}

    def fetcher(feed: str, params: dict[str, str]) -> dict:
        symbol = params["symbol"]
        if feed == "instruments-info":
            return response([current[symbol]])
        return response([risk(symbol)])

    collector = ReferenceDataCollector(tmp_path, fetcher=fetcher)
    first = collector.collect(["BTCUSDT"], captured_at_ms=1_756_000_000_000)
    assert [(event.symbol, event.event_type) for event in first.symbol_events] == [
        ("BTCUSDT", "added")
    ]

    second = collector.collect(["BTCUSDT", "ETHUSDT"], captured_at_ms=1_756_000_001_000)
    assert [(event.symbol, event.event_type) for event in second.symbol_events] == [
        ("ETHUSDT", "added")
    ]

    current["BTCUSDT"] = instrument("BTCUSDT", status="PreLaunch")
    third = collector.collect(["BTCUSDT"], captured_at_ms=1_756_000_002_000)
    assert [(event.symbol, event.event_type) for event in third.symbol_events] == [
        ("BTCUSDT", "changed"),
        ("ETHUSDT", "removed"),
    ]
