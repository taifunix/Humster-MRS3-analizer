from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mrs3.portfolio import market_snapshot as market_snapshot_module
from mrs3.portfolio.market_snapshot import ApiRateLimiter, MarketSnapshotError, load_market_snapshot


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


def test_load_market_snapshot_requires_limiter_for_default_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market_snapshot_module, "_http_fetch", lambda *_: pytest.fail("default fetch must not run"))
    with pytest.raises(MarketSnapshotError, match="limiter"):
        load_market_snapshot(("BTCUSDT",), captured_at_ms=0)


def test_load_market_snapshot_routes_every_reference_fetch_through_limiter() -> None:
    fetch, calls = fetcher_for(("BTCUSDT",))
    limited: list[str] = []

    class Limiter:
        def call(self, raw_fetch, feed, params):
            limited.append(feed)
            return raw_fetch(feed, params)

    load_market_snapshot(("BTCUSDT",), captured_at_ms=0, fetcher=fetch, limiter=Limiter())  # type: ignore[arg-type]

    assert len(limited) == len(calls)
    assert limited == [feed for feed, _params in calls]


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


def test_shared_api_limiter_retries_charged_attempts_and_persists_403_cooldown(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    sleeps: list[float] = []
    limiter = ApiRateLimiter(tmp_path / "cooldown.json", clock=lambda: now[0], sleep=lambda seconds: (sleeps.append(seconds), now.__setitem__(0, now[0] + timedelta(seconds=seconds))))
    calls = 0

    def retryable(_feed, _params):
        nonlocal calls
        calls += 1
        return {"retCode": 10006, "result": {}} if calls < 3 else {"retCode": 0, "result": {}}

    assert limiter.call(retryable, "tickers", {})["retCode"] == 0
    assert calls == 3
    assert len(sleeps) >= 2

    class TooFrequent(RuntimeError):
        status_code = 403

        def __str__(self):
            return "access too frequent"

    with pytest.raises(TooFrequent):
        limiter.call(lambda *_: (_ for _ in ()).throw(TooFrequent()), "tickers", {})
    blocked_calls = 0
    restored = ApiRateLimiter(tmp_path / "cooldown.json", clock=lambda: now[0], sleep=lambda seconds: None)

    def blocked(*_):
        nonlocal blocked_calls
        blocked_calls += 1
        return {}

    with pytest.raises(MarketSnapshotError, match="cooldown"):
        restored.call(blocked, "tickers", {})
    assert blocked_calls == 0


def test_shared_api_limiter_requires_persisted_state_path():
    with pytest.raises(MarketSnapshotError, match="state_path"):
        ApiRateLimiter(clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc), sleep=lambda _seconds: None)


def test_shared_api_limiter_fails_closed_after_final_retryable_payload(tmp_path):
    calls = 0

    def exhausted(_feed, _params):
        nonlocal calls
        calls += 1
        return {"retCode": 10006, "result": {}}

    limiter = ApiRateLimiter(tmp_path / "cooldown.json", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc), sleep=lambda _seconds: None)

    with pytest.raises(MarketSnapshotError, match="retry budget exhausted"):
        limiter.call(exhausted, "tickers", {})
    assert calls == 3


def test_shared_api_limiter_clamps_retry_after_to_one_minute(tmp_path):
    sleeps: list[float] = []
    limiter = ApiRateLimiter(tmp_path / "cooldown.json", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc), sleep=sleeps.append)

    with pytest.raises(MarketSnapshotError, match="retry budget exhausted"):
        limiter.call(lambda *_: {"retCode": 10006, "headers": {"Retry-After": "120"}}, "tickers", {})

    assert sleeps
    assert max(sleeps) <= 60


def test_shared_api_limiter_retries_mapping_status_code_429_and_exhausts(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    calls = 0
    limiter = ApiRateLimiter(tmp_path / "cooldown.json", clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds)))

    def exhausted(_feed, _params):
        nonlocal calls
        calls += 1
        return {"statusCode": 429}

    with pytest.raises(MarketSnapshotError, match="retry budget exhausted"):
        limiter.call(exhausted, "tickers", {})

    assert calls == 3


def test_shared_api_limiter_rejects_malformed_persisted_cooldown(tmp_path):
    state = tmp_path / "cooldown.json"
    state.write_text("{not-json", encoding="utf-8")

    with pytest.raises(MarketSnapshotError, match="persisted"):
        ApiRateLimiter(state, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc), sleep=lambda _seconds: None)
