"""Pair registry stage 1: core schema, reference refresh for Bybit and BingX, history sufficiency.

Contract: docs/specs/2026-10-18-pair-registry.md, «Stage 1 contract».
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from mrs3.pair_registry import cli
from mrs3.pair_registry.bingx import human_ticker, normalize_contracts
from mrs3.pair_registry.bybit import normalize_instruments, normalize_risk_limits
from mrs3.pair_registry.history import sufficiency
from mrs3.pair_registry.refresh import RegistryError, refresh_exchange
from mrs3.pair_registry.store import RegistrySettings, connect, load_registry_settings

DAY = 86_400_000
NOW = int(datetime(2026, 10, 18, 12, tzinfo=timezone.utc).timestamp() * 1000)


def _instrument(symbol: str, symbol_type: str, *, status: str = "Trading", contract: str = "LinearPerpetual",
                tick: str = "0.01", launch: int = 1_700_000_000_000) -> dict:
    return {
        "symbol": symbol, "contractType": contract, "status": status, "baseCoin": symbol[:-4], "quoteCoin": "USDT",
        "launchTime": str(launch), "deliveryTime": "0", "priceScale": "2",
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "25.00", "leverageStep": "0.01"},
        "priceFilter": {"minPrice": "0.01", "maxPrice": "199999.98", "tickSize": tick},
        "lotSizeFilter": {"maxOrderQty": "13576.00", "minOrderQty": "0.01", "qtyStep": "0.01",
                          "maxMktOrderQty": "3394.00", "minNotionalValue": "5"},
        "fundingInterval": 480, "settleCoin": "USDT", "upperFundingRate": "0.02", "lowerFundingRate": "-0.02",
        "displayName": symbol, "symbolType": symbol_type, "fullName": f"{symbol} Inc",
    }


BYBIT = [
    _instrument("XOMUSDT", "stock"), _instrument("SPYUSDT", "ETF"), _instrument("COPPERUSDT", "commodity"),
    _instrument("BTCUSDT", ""), _instrument("EURUSDUSDT", "forex"), _instrument("NEWCOINUSDT", "innovation"),
    _instrument("SPXUSDT", "stock"), _instrument("XOMUSDT-26DEC26", "stock", contract="LinearFutures"),
    _instrument("OLDUSDT", "stock", status="Closed"),
]
RISK = [
    {"id": 1, "symbol": "XOMUSDT", "riskLimitValue": "10000", "maintenanceMargin": "0.015", "initialMargin": "0.02",
     "isLowestRisk": 1, "maxLeverage": "50.00", "mmDeduction": ""},
    {"id": 2, "symbol": "XOMUSDT", "riskLimitValue": "20000", "maintenanceMargin": "0.02", "initialMargin": "0.04",
     "isLowestRisk": 0, "maxLeverage": "25.00", "mmDeduction": "50"},
    {"id": 1, "symbol": "BTCUSDT", "riskLimitValue": "2000000", "maintenanceMargin": "0.005", "initialMargin": "0.01",
     "isLowestRisk": 1, "maxLeverage": "100.00", "mmDeduction": ""},
]


def _contract(symbol: str, *, status: int = 1, display: str | None = None, api_open: str = "true") -> dict:
    return {"symbol": symbol, "quantityPrecision": 4, "pricePrecision": 2, "makerFeeRate": 0.0002,
            "takerFeeRate": 0.0005, "tradeMinQuantity": 0.0005, "tradeMinUSDT": 2, "currency": "USDT",
            "status": status, "apiStateOpen": api_open, "apiStateClose": "true", "launchTime": 1_760_424_900_000,
            "displayName": display or symbol}


BINGX = [
    _contract("NCSKAMD2USD-USDT", display="AMDUS-USDT"), _contract("NCSKTMFUSDT-USDT"),
    _contract("NCSKSOFTBANK2JPY-USDT"), _contract("NCCOGOLD2USD-USDT", display="GOLD(XAU)-USDT"),
    _contract("NCSIUS5002USD-USDT"), _contract("NCSKTSLL2USD-USDT", api_open="false"),
    _contract("NCSKXLP2USD-USDT", status=25), _contract("NCFXEUR2USD-USDT"), _contract("BTC-USDT"),
]


def test_bybit_universe_keeps_trading_tradfi_perpetuals_only() -> None:
    records = normalize_instruments(BYBIT, RegistrySettings())
    assert sorted(record["symbol"] for record in records) == ["COPPERUSDT", "SPYUSDT", "XOMUSDT"]
    xom = next(record for record in records if record["symbol"] == "XOMUSDT")
    assert (xom["api_symbol"], xom["status"], xom["symbol_type"], xom["tick_size"], xom["max_leverage"]) == (
        "XOMUSDT", "TRADING", "stock", "0.01", "25")
    assert (xom["listing_ms"], xom["min_notional"], xom["funding_interval_min"]) == (1_700_000_000_000, "5", 480)
    tiers = normalize_risk_limits(RISK, {"XOMUSDT"})
    assert [(tier["symbol"], tier["tier_id"], tier["mm_deduction"]) for tier in tiers] == [
        ("XOMUSDT", 1, "0"), ("XOMUSDT", 2, "50")]


@pytest.mark.parametrize(("symbol", "ticker"), [
    ("NCSKAMD2USD-USDT", "AMDUSDT"), ("NCSKTMFUSDT-USDT", "TMFUSDT"), ("NCSKSOFTBANK2JPY-USDT", "SOFTBANKUSDT"),
    ("NCCOGOLD2USD-USDT", "GOLDUSDT"), ("NCSIUS5002USD-USDT", "US500USDT"), ("NCSKF2USD-USDT", "FUSDT"),
])
def test_bingx_human_ticker_comes_from_the_symbol(symbol: str, ticker: str) -> None:
    assert human_ticker(symbol) == ticker


def test_bingx_universe_statuses_and_reference_fields() -> None:
    records = {record["symbol"]: record for record in normalize_contracts(BINGX, RegistrySettings())}
    assert sorted(records) == ["AMDUSDT", "GOLDUSDT", "SOFTBANKUSDT", "TMFUSDT", "TSLLUSDT", "US500USDT", "XLPUSDT"]
    amd = records["AMDUSDT"]
    assert (amd["api_symbol"], amd["display_name"], amd["status"], amd["tick_size"], amd["qty_step"]) == (
        "NCSKAMD2USD-USDT", "AMDUS-USDT", "TRADING", "0.01", "0.0001")
    # tradeMinQuantity is min notional / live price, so it is not reference data (live run 2026-10-18: it moved
    # on 80 contracts between two refreshes); the stable fact is the minimum notional.
    assert (amd["min_order_qty"], amd["min_notional"], amd["maker_fee"], amd["taker_fee"]) == (None, "2", "0.0002", "0.0005")
    assert records["XLPUSDT"]["status"] == "SUSPENDED"
    assert records["TSLLUSDT"]["api_open"] is False and amd["api_open"] is True
    assert records["GOLDUSDT"]["symbol_type"] == "commodity" and records["US500USDT"]["symbol_type"] == "index"


def test_a_bingx_refresh_ignores_the_price_derived_minimum_quantity(tmp_path: Path) -> None:
    con = connect(tmp_path / "registry")
    refresh_exchange(con, "bingx", normalize_contracts(BINGX, RegistrySettings()), [], now_ms=NOW)
    moved = [dict(item, tradeMinQuantity=0.0007) for item in BINGX]
    assert refresh_exchange(con, "bingx", normalize_contracts(moved, RegistrySettings()), [], now_ms=NOW + DAY)["changed"] == 0


def test_bingx_ticker_collision_is_refused_with_both_symbols() -> None:
    with pytest.raises(RegistryError, match="NCSKFOO2USD-USDT.*NCSKFOOUSDT-USDT"):
        normalize_contracts([_contract("NCSKFOO2USD-USDT"), _contract("NCSKFOOUSDT-USDT")], RegistrySettings())


def _changes(con) -> list[tuple]:
    return con.execute("select symbol, kind, field, old_value, new_value from pair_changes order by seq").fetchall()


def test_refresh_is_idempotent_and_logs_each_change(tmp_path: Path) -> None:
    con = connect(tmp_path / "registry")
    records = normalize_instruments(BYBIT, RegistrySettings())
    tiers = normalize_risk_limits(RISK, {record["symbol"] for record in records})
    first = refresh_exchange(con, "bybit", records, tiers, now_ms=NOW)
    assert first == {"pairs": 3, "listed": 3, "changed": 0, "delisted": 0, "risk_tiers": 2}
    assert [row[1] for row in _changes(con)] == ["listed"] * 3
    again = refresh_exchange(con, "bybit", records, tiers, now_ms=NOW + 1000)
    assert again == {"pairs": 3, "listed": 0, "changed": 0, "delisted": 0, "risk_tiers": 2}
    assert len(_changes(con)) == 3
    assert con.execute("select count(*) from risk_tiers").fetchone()[0] == 2           # same day: replaced

    ticked = [dict(record, tick_size="0.001") if record["symbol"] == "XOMUSDT" else record for record in records]
    assert refresh_exchange(con, "bybit", ticked, tiers, now_ms=NOW + DAY)["changed"] == 1
    assert _changes(con)[-1] == ("XOMUSDT", "field", "tick_size", "0.01", "0.001")
    assert con.execute("select count(*) from risk_tiers").fetchone()[0] == 4           # next day: a new snapshot

    without_spy = [record for record in ticked if record["symbol"] != "SPYUSDT"]
    assert refresh_exchange(con, "bybit", without_spy, tiers, now_ms=NOW + 2 * DAY)["delisted"] == 1
    assert _changes(con)[-1][:2] == ("SPYUSDT", "delisted")
    assert con.execute("select status from pairs where symbol = 'SPYUSDT'").fetchone()[0] == "DELISTED"
    assert refresh_exchange(con, "bybit", ticked, tiers, now_ms=NOW + 3 * DAY)["changed"] == 1
    assert _changes(con)[-1] == ("SPYUSDT", "field", "status", "DELISTED", "TRADING")
    journal = con.execute("select step, exchange, status from journal order by started_at").fetchall()
    assert journal == [("refresh", "bybit", "OK")] * 5


def test_an_empty_instrument_list_never_delists_the_universe(tmp_path: Path) -> None:
    con = connect(tmp_path / "registry")
    refresh_exchange(con, "bybit", normalize_instruments(BYBIT, RegistrySettings()), [], now_ms=NOW)
    with pytest.raises(RegistryError, match="empty"):
        refresh_exchange(con, "bybit", [], [], now_ms=NOW + DAY)
    assert con.execute("select count(*) from pairs where status = 'TRADING'").fetchone()[0] == 3
    assert con.execute("select status from journal order by started_at desc limit 1").fetchone()[0] == "FAILED"


def test_exchanges_are_kept_apart(tmp_path: Path) -> None:
    con = connect(tmp_path / "registry")
    refresh_exchange(con, "bybit", normalize_instruments(BYBIT, RegistrySettings()), [], now_ms=NOW)
    refresh_exchange(con, "bingx", normalize_contracts(BINGX, RegistrySettings()), [], now_ms=NOW)
    counts = dict(con.execute("select exchange, count(*) from pairs group by 1").fetchall())
    assert counts == {"bybit": 3, "bingx": 7}
    assert con.execute("select count(*) from pairs where status = 'DELISTED'").fetchone()[0] == 0


@pytest.mark.parametrize(("today", "days", "status"), [
    (date(2026, 10, 18), 17, "мало истории (17 дней)"),
    (date(2026, 10, 25), 24, "можно исследовать"),
    (date(2026, 11, 13), 43, "можно исследовать"),
    (date(2026, 11, 14), 44, "READY возможен"),
])
def test_history_sufficiency_boundaries(today: date, days: int, status: str) -> None:
    listing = int(datetime(2026, 10, 1, 15, tzinfo=timezone.utc).timestamp() * 1000)
    result = sufficiency(listing, today)
    assert (result["days"], result["status"]) == (days, status)
    assert (result["research_from"], result["ready_from"]) == ("2026-10-25", "2026-11-14")


def test_settings_defaults_and_overrides(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": "reg", "universe": {"bybit": {"exclude": ["XOMUSDT"]}}}}),
                      encoding="utf-8")
    settings = load_registry_settings(config)
    assert settings.root == (tmp_path / "reg").resolve()
    assert settings.bybit_exclude == ("XOMUSDT",) and settings.bybit_symbol_types == ("stock", "ETF", "commodity")
    assert settings.bingx_prefixes == ("NCSK", "NCSI", "NCCO")
    # a configured list replaces the default one (SPXUSDT is no longer excluded here)
    assert sorted(record["symbol"] for record in normalize_instruments(BYBIT, settings)) == [
        "COPPERUSDT", "SPXUSDT", "SPYUSDT"]


def test_cli_refresh_writes_the_registry_and_prints_a_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "reg")}}), encoding="utf-8")
    monkeypatch.setattr(cli, "fetch_bybit", lambda: (BYBIT, RISK))
    monkeypatch.setattr(cli, "fetch_bingx", lambda: BINGX)
    assert cli.main(["refresh", "--exchange", "all", "--config", str(config)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["bybit"]["pairs"] == 3 and summary["bingx"]["pairs"] == 7
    assert (tmp_path / "reg" / "registry.duckdb").is_file()


def test_cli_keeps_going_when_one_exchange_fails_unexpectedly(tmp_path: Path, monkeypatch, capsys) -> None:
    # review of registry stage 1: any error of one exchange (here a DB constraint) must not lose the other
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "reg")}}), encoding="utf-8")
    duplicate = RISK + [RISK[0]]                                        # duplicate tier id -> primary key error
    monkeypatch.setattr(cli, "fetch_bybit", lambda: (BYBIT, duplicate))
    monkeypatch.setattr(cli, "fetch_bingx", lambda: BINGX)
    assert cli.main(["refresh", "--config", str(config)]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert "error" in summary["bybit"] and summary["bingx"]["pairs"] == 7
    con = connect(tmp_path / "reg")
    assert con.execute("select count(*) from pairs where exchange = 'bybit'").fetchone()[0] == 0     # rolled back
    assert con.execute("select exchange, status from journal order by exchange").fetchall() == [
        ("bingx", "OK"), ("bybit", "FAILED")]


def test_cli_journals_fetch_and_normalisation_failures(tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "reg")}}), encoding="utf-8")

    def down():
        raise OSError("network down")

    monkeypatch.setattr(cli, "fetch_bybit", down)
    monkeypatch.setattr(cli, "fetch_bingx", lambda: [_contract("NCSKFOO2USD-USDT"), _contract("NCSKFOOUSDT-USDT")])
    assert cli.main(["refresh", "--config", str(config)]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert "network down" in summary["bybit"]["error"] and "collision" in summary["bingx"]["error"]
    rows = connect(tmp_path / "reg").execute("select exchange, status, error from journal order by exchange").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("bingx", "FAILED"), ("bybit", "FAILED")]
    assert "collision" in rows[0][2]


@pytest.mark.parametrize("section", [
    {"universe": "bybit"}, {"universe": {"bybit": ["stock"]}}, {"universe": {"bingx": "NCSK"}}, {"root": 5},
])
def test_malformed_settings_are_refused_clearly(tmp_path: Path, section: dict) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": section}), encoding="utf-8")
    with pytest.raises(RegistryError):
        load_registry_settings(config)


def test_a_missing_config_file_is_refused(tmp_path: Path, capsys) -> None:
    with pytest.raises(RegistryError, match="not found"):
        load_registry_settings(tmp_path / "typo.json")
    assert cli.main(["refresh", "--config", str(tmp_path / "typo.json")]) == 1
    assert "not found" in json.loads(capsys.readouterr().out)["error"]


def test_bingx_contracts_the_ticker_rule_cannot_read_are_reported() -> None:
    from mrs3.pair_registry.bingx import unreadable_symbols

    items = BINGX + [_contract("NCSKbad.sym-USDT")]
    assert unreadable_symbols(items, RegistrySettings()) == ["NCSKbad.sym-USDT"]
