from __future__ import annotations

from decimal import Decimal

import pytest

from mrs3.portfolio.market_snapshot import MarketSnapshotError, load_market_snapshot


def instrument(symbol: str, *, status: str = "Trading", contract: str = "LinearPerpetual") -> dict:
    return {
        "symbol": symbol,
        "status": status,
        "contractType": contract,
        "priceFilter": {"tickSize": "0.1"},
        "lotSizeFilter": {
            "qtyStep": "0.001",
            "minOrderQty": "0.001",
            "maxOrderQty": "100",
        },
        "leverageFilter": {"leverageStep": "0.01", "maxLeverage": "100"},
    }


def risk(symbol: str, value: str = "100000") -> dict:
    return {"symbol": symbol, "id": "1", "riskLimitValue": value, "maxLeverage": "50"}


def page(items: list[dict], cursor: str = "") -> dict:
    return {
        "retCode": 0,
        "result": {"category": "linear", "list": items, "nextPageCursor": cursor},
    }


def ticker(symbol: str, mark: str = "50000") -> dict:
    return page([{"symbol": symbol, "markPrice": mark}])


def fetcher_for(symbols: tuple[str, ...]):
    calls: list[tuple[str, dict[str, str]]] = []

    def fetch(feed: str, params: dict[str, str]) -> dict:
        calls.append((feed, dict(params)))
        symbol = params["symbol"]
        assert symbol in symbols
        if feed == "instruments-info":
            return page([instrument(symbol)])
        if feed == "risk-limit":
            return page([risk(symbol)])
        return ticker(symbol, "50000" if symbol == "BTCUSDT" else "3000")

    return fetch, calls


def test_loads_complete_snapshot_deterministically_independent_of_input_order() -> None:
    fetch, calls = fetcher_for(("BTCUSDT", "ETHUSDT"))
    first = load_market_snapshot(("ETHUSDT", "BTCUSDT"), captured_at_ms=7, fetcher=fetch)
    fetch2, _ = fetcher_for(("BTCUSDT", "ETHUSDT"))
    second = load_market_snapshot(("BTCUSDT", "ETHUSDT"), captured_at_ms=7, fetcher=fetch2)

    assert tuple(item.symbol for item in first.reference.instruments) == ("BTCUSDT", "ETHUSDT")
    assert first.mark_prices == {"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")}
    assert first.captured_at_ms == 7
    assert first.content_digest == second.content_digest
    assert calls[0] == ("instruments-info", {"category": "linear", "symbol": "BTCUSDT"})
    with pytest.raises(TypeError):
        first.mark_prices["BTCUSDT"] = Decimal("1")  # type: ignore[index]


def test_paginates_reference_feeds_and_detects_repeated_cursor() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fetch(feed: str, params: dict[str, str]) -> dict:
        calls.append((feed, dict(params)))
        if feed == "instruments-info":
            return page([], "next") if "cursor" not in params else page([instrument("BTCUSDT")])
        if feed == "risk-limit":
            return page([], "risk-next") if "cursor" not in params else page([risk("BTCUSDT")])
        return ticker("BTCUSDT")

    snapshot = load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=fetch)
    assert snapshot.reference.instrument("BTCUSDT").status == "Trading"
    assert ("instruments-info", {"category": "linear", "symbol": "BTCUSDT", "cursor": "next"}) in calls
    assert ("risk-limit", {"category": "linear", "symbol": "BTCUSDT", "cursor": "risk-next"}) in calls

    def repeated(feed: str, params: dict[str, str]) -> dict:
        if feed == "instruments-info":
            return page([], "same")
        return page([risk("BTCUSDT")]) if feed == "risk-limit" else ticker("BTCUSDT")

    with pytest.raises(MarketSnapshotError, match="cursor repeats"):
        load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=repeated)


@pytest.mark.parametrize("mark", ["0", "-1", "NaN", "Infinity", "bad", 1.5, True])
def test_rejects_invalid_mark_price(mark: object) -> None:
    fetch, _ = fetcher_for(("BTCUSDT",))

    def invalid(feed: str, params: dict[str, str]) -> dict:
        return ticker("BTCUSDT", mark) if feed == "tickers" else fetch(feed, params)

    with pytest.raises(MarketSnapshotError, match="markPrice"):
        load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=invalid)


def test_rejects_mismatched_or_duplicate_ticker() -> None:
    fetch, _ = fetcher_for(("BTCUSDT",))

    for items in ([{"symbol": "ETHUSDT", "markPrice": "1"}], [
        {"symbol": "BTCUSDT", "markPrice": "1"},
        {"symbol": "BTCUSDT", "markPrice": "2"},
    ]):
        def invalid(feed: str, params: dict[str, str], items=items) -> dict:
            return page(items) if feed == "tickers" else fetch(feed, params)

        with pytest.raises(MarketSnapshotError, match="ticker"):
            load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=invalid)


@pytest.mark.parametrize("payload", [None, {}, {"retCode": 1, "result": {"category": "linear", "list": []}}, {"retCode": 0, "result": {"category": "spot", "list": []}}, {"retCode": 0, "result": {"category": "linear", "list": {}}}])
def test_rejects_response_contract_errors(payload: object) -> None:
    with pytest.raises(MarketSnapshotError):
        load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=lambda _feed, _params: payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("symbols", [(), ("",), ("BTCUSDT", "BTCUSDT"), "ETH"])
def test_rejects_empty_invalid_or_duplicate_symbols_without_fetching(symbols: object) -> None:
    called = False

    def fetch(_feed: str, _params: dict[str, str]) -> dict:
        nonlocal called
        called = True
        raise AssertionError("must not fetch")

    with pytest.raises(MarketSnapshotError, match="symbols"):
        load_market_snapshot(symbols, captured_at_ms=0, fetcher=fetch)  # type: ignore[arg-type]
    assert not called


@pytest.mark.parametrize("captured", [-1, True, 1.0])
def test_rejects_inexact_capture_time_without_fetching(captured: object) -> None:
    with pytest.raises(MarketSnapshotError, match="captured_at_ms"):
        load_market_snapshot(("BTCUSDT",), captured_at_ms=captured, fetcher=lambda *_: pytest.fail())  # type: ignore[arg-type]


def test_requires_one_active_instrument_and_at_least_one_risk_tier() -> None:
    def fetch(feed: str, _params: dict[str, str]) -> dict:
        if feed == "instruments-info":
            return page([instrument("BTCUSDT", status="Settled")])
        if feed == "risk-limit":
            return page([])
        return ticker("BTCUSDT")

    with pytest.raises(MarketSnapshotError, match="active LinearPerpetual"):
        load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=fetch)

    def no_tiers(feed: str, _params: dict[str, str]) -> dict:
        if feed == "instruments-info":
            return page([instrument("BTCUSDT")])
        return page([]) if feed == "risk-limit" else ticker("BTCUSDT")

    with pytest.raises(MarketSnapshotError, match="at least one risk tier"):
        load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=no_tiers)


def test_current_max_limit_order_quantity_alias_is_supported() -> None:
    current = instrument("BTCUSDT")
    current["lotSizeFilter"]["maxLimitOrderQty"] = current["lotSizeFilter"].pop("maxOrderQty")

    def fetch(feed: str, _params: dict[str, str]) -> dict:
        if feed == "instruments-info":
            return page([current])
        if feed == "risk-limit":
            return page([risk("BTCUSDT")])
        return ticker("BTCUSDT")

    snapshot = load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=fetch)
    assert snapshot.reference.instrument("BTCUSDT").max_qty == Decimal("100")


def test_current_bybit_empty_first_tier_mm_deduction_is_optional() -> None:
    current_risk = {**risk("BTCUSDT"), "maintenanceMargin": "0.02", "initialMargin": "0.04", "mmDeduction": ""}

    def fetch(feed: str, _params: dict[str, str]) -> dict:
        if feed == "instruments-info":
            return page([instrument("BTCUSDT")])
        if feed == "risk-limit":
            return page([current_risk])
        return ticker("BTCUSDT")

    tier = load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=fetch).reference.tiers("BTCUSDT")[0]
    assert tier.mm_deduction is None


def test_wraps_fetcher_errors_as_global_typed_failure() -> None:
    def fetch(_feed: str, _params: dict[str, str]) -> dict:
        raise RuntimeError("offline")

    with pytest.raises(MarketSnapshotError, match="fetch failed"):
        load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=fetch)
