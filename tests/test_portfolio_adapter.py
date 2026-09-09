from decimal import Decimal

from datetime import date, datetime, timezone

from mrs3.portfolio.adapter import build_portfolio_candidates, run_portfolio_adapter
from mrs3.portfolio.liquidity import ReferenceReader
from mrs3.portfolio.minute_capacity import CapacityWindow, MinuteCapacityResult


def capacity(symbol: str, cap: str, status: str = "READY") -> MinuteCapacityResult:
    window = CapacityWindow(7, 10080, 100, 9980, Decimal("0.01"), Decimal("100000"), Decimal("10"), Decimal(cap), Decimal(cap))
    return MinuteCapacityResult(status, symbol, None, None, 30, Decimal("50"), window, window, Decimal(cap), "CALENDAR_7D", (), ("fixture",), f"cap-{symbol}")


def reference():
    instruments = [
        {"symbol": symbol, "status": "Trading", "contract_type": "LinearPerpetual", "tick_size": "0.1", "qty_step": "0.1", "min_qty": "0.1", "max_qty": "100", "leverage_step": "0.1", "max_leverage": "100", "min_notional": "1"}
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    ]
    tiers = [{"symbol": symbol, "risk_limit_value": "100000", "max_leverage": "50"} for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
    return ReferenceReader.from_records(instruments=instruments, risk_tiers=tiers, captured_at_ms=1_000)


def finalist(strategy_id: int, symbol: str, *, pnl: str, dd: str, recovery: str, shift: int = 20):
    return {
        "user_status": "FINALIST", "symbol": symbol, "side": "LONG",
        "strategy_id": strategy_id, "result_id": strategy_id * 10, "user_rank": strategy_id,
        "total_pnl": Decimal(pnl), "max_drawdown_pct": Decimal(dd),
        "recovery_factor": Decimal(recovery),
        "strategy_orders": ({"order_id": 1, "lot_x": Decimal("1"), "shift_bp": shift},),
    }


def document():
    ranking = {"id": "portfolio_preliminary_ranking_v1", "parameters": {}}
    return {
        "search": {"max_enumerated_combinations": 100},
        "scenarios": {"BALANCED": {"account": "BALANCED"}},
        "profiles": {
            "BALANCED": {
                "individual_max_dd_pct": "20",
                "individual_net_pnl_min_exclusive": "0",
                "ranking": {**ranking, "top_n": 1},
            }
        },
        "liquidity": {"maximum_age_hours": 2},
    }


def campaign():
    return {
        "campaign_id": "campaign-1",
        "config_document": document(),
        "launch": {"profiles": [{"profile_id": "BALANCED", "max_candidates": 3}]},
    }


def test_adapter_sizes_screens_ranks_and_builds_true_multi_pair_candidate():
    rows = (
        finalist(1, "BTCUSDT", pnl="10", dd="10", recovery="1", shift=5),
        finalist(2, "BTCUSDT", pnl="20", dd="10", recovery="2", shift=20),
        finalist(3, "ETHUSDT", pnl="15", dd="10", recovery="3", shift=20),
        finalist(4, "SOLUSDT", pnl="12", dd="10", recovery="2", shift=20),
    )
    result = build_portfolio_candidates(
        rows,
        campaign(),
        capacities={"BTCUSDT": capacity("BTCUSDT", "600"), "ETHUSDT": capacity("ETHUSDT", "400", "PRELIMINARY"), "SOLUSDT": capacity("SOLUSDT", "300")},
        reference=reference(),
        mark_prices={"BTCUSDT": Decimal("100"), "ETHUSDT": Decimal("50"), "SOLUSDT": Decimal("25")},
        spread_observations={"BTCUSDT": ({"spread_bps_p95": "10"},), "ETHUSDT": ({"spread_bps_p95": "10"},), "SOLUSDT": ({"spread_bps_p95": "10"},)},
        spread_history_statuses={"BTCUSDT": "READY", "ETHUSDT": "PRELIMINARY", "SOLUSDT": "READY"},
        now_ms=1_000,
    )

    assert result.status == "PASS"
    assert len(result.variants) == 7
    assert {variant["pair_count"] for variant in result.variants} == {1, 2, 3}
    variant = next(
        item for item in result.variants
        if {row["symbol"] for row in item["members"]} == {"BTCUSDT", "ETHUSDT"}
    )
    assert variant["profile"] == "BALANCED"
    assert variant["scenario_id"] == "BALANCED"
    assert variant["member_count"] == 2
    assert variant["pair_count"] == 2
    assert [(row["symbol"], row["position_size_usdt"]) for row in variant["members"]] == [("BTCUSDT", Decimal("600")), ("ETHUSDT", Decimal("400"))]
    assert any(item["selection_reason"] == "OVERLAPS_SPREAD" for item in result.excluded)
    assert "SPREAD_HISTORY_PRELIMINARY" in result.warnings
    assert "LIQUIDITY_CAPACITY_PRELIMINARY" in result.warnings


def test_adapter_reports_profile_failure_without_discarding_other_profile_results():
    config = document()
    config["profiles"]["AGGRESSIVE"] = {
        "individual_max_dd_pct": "30", "individual_net_pnl_min_exclusive": "100",
        "ranking": {"id": "portfolio_preliminary_ranking_v1", "parameters": {}, "top_n": 1},
    }
    config["scenarios"]["AGGRESSIVE"] = {"account": "AGGRESSIVE"}
    request = campaign()
    request["config_document"] = config
    request["launch"]["profiles"].append({"profile_id": "AGGRESSIVE", "max_candidates": 5})

    result = build_portfolio_candidates(
        (finalist(2, "BTCUSDT", pnl="20", dd="10", recovery="2"),), request,
        capacities={"BTCUSDT": capacity("BTCUSDT", "600")}, reference=reference(),
        mark_prices={"BTCUSDT": Decimal("100")}, spread_observations={},
        spread_history_statuses={}, now_ms=1_000,
    )

    assert [item["profile"] for item in result.variants] == ["BALANCED"]
    assert result.blockers == ("AGGRESSIVE:INSUFFICIENT_DIRECTIONAL_UNIVERSE",)


def test_adapter_fails_closed_on_stale_global_reference():
    result = build_portfolio_candidates(
        (finalist(2, "BTCUSDT", pnl="20", dd="10", recovery="2"),), campaign(),
        capacities={"BTCUSDT": capacity("BTCUSDT", "600")}, reference=reference(),
        mark_prices={"BTCUSDT": Decimal("100")}, spread_observations={},
        spread_history_statuses={}, now_ms=7_201_001,
    )
    assert result.status == "FAIL"
    assert result.variants == ()
    assert result.blockers == ("REFERENCE_STALE",)


def test_runtime_adapter_uses_partial_local_minutes_and_injected_current_market(tmp_path):
    minute_root = tmp_path / "minutes" / "BTCUSDT"
    minute_root.mkdir(parents=True)
    day = date(2026, 9, 8)
    stamp = int(datetime(2026, 9, 8, tzinfo=timezone.utc).timestamp() * 1000)
    (minute_root / "BTCUSDT2026-09-08_1m.csv").write_text(
        "timestamp,open,high,low,close,volume,buy_volume,sell_volume,trades\n"
        f"{stamp},100,100,100,100,28800,14000,14800,2\n",
        encoding="utf-8",
    )
    request = campaign()
    request["created_at_utc"] = "2026-09-09T12:00:00Z"
    request["config_document"]["inputs"] = {"bybit_minute_data_root": "minutes", "collector_root": "collector"}
    request["config_document"]["liquidity"] = {
        "parameters": {"close_volume_participation_pct": 30}, "round_down_usdt": 50,
            "minimum_coverage_pct": 90, "maximum_age_hours": 2, "weekend_start_utc": "SATURDAY 00:00",
        "weekend_end_utc": "MONDAY 00:00", "archive_publication_lag_hours": 6,
        "backfill_write_enabled": False,
    }

    def market(feed, params):
        symbol = params["symbol"]
        if feed == "instruments-info":
            items = [{"symbol": symbol, "status": "Trading", "contractType": "LinearPerpetual", "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "maxLimitOrderQty": "100", "minNotionalValue": "1"}, "leverageFilter": {"leverageStep": "0.1", "maxLeverage": "100"}}]
        elif feed == "risk-limit":
            items = [{"symbol": symbol, "id": "1", "riskLimitValue": "100000", "maxLeverage": "50"}]
        else:
            items = [{"symbol": symbol, "markPrice": "100"}]
        return {"retCode": 0, "result": {"category": "linear", "list": items, "nextPageCursor": ""}}

    result = run_portfolio_adapter(
        (finalist(2, "BTCUSDT", pnl="20", dd="10", recovery="2"),), request,
        workspace_root=tmp_path, market_fetcher=market,
        archive_fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("disabled backfill fetched")),
    )

    assert result.status == "PASS"
    assert result.variants[0]["members"][0]["position_size_usdt"] == Decimal("600")
    assert "LIQUIDITY_CAPACITY_PRELIMINARY" in result.warnings
