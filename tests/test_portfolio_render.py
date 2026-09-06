import json
from decimal import Decimal

import pytest

from mrs3.portfolio.render import render_portfolio, reverse_typed_compare
from mrs3.portfolio.search import UNKNOWN


def candidate(side="LONG", symbol="BTCUSDT", *, tf="1h"):
    return {
        "status": "FINALIST", "symbol": symbol, "side": side,
        "strategy_id": f"strat-{side.lower()}", "result_id": f"result-{side.lower()}",
        "timeframe": tf, "geometry": {"entry": "ma", "close": "dedicated"}, "lot_x": "1",
        "orders": [{"id": "open", "quantity": "3", "qty_step": "1", "min_qty": "1"}],
        "runtime": {"mode": "linear"}, "dedicated_close": {"enabled": True},
    }


def capability(*, dual_tf=True):
    return {
        "version": "adapter-v1", "dual_tf": dual_tf, "dedicated_close": True, "opposite_opening": True,
        "physical_fields": {"symbol": "symbol", "leverage": "leverage", "long": "long", "short": "short", "close": "close", "opposite_policy": "opposite_policy"},
        "common_runtime_fields": ["mode"],
    }


def test_one_json_per_symbol_uses_directional_immutable_fields_and_reverse_compare():
    long = candidate("LONG")
    short = candidate("SHORT")
    short["geometry"] = {"entry": "other", "close": "dedicated"}
    result = render_portfolio(
        [{"symbol": "BTCUSDT", "long": long, "short": short}],
        capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    )
    assert result.status == "PASS"
    assert len(result.json_by_symbol) == 1
    payload = json.loads(result.json_by_symbol["BTCUSDT"])
    assert payload["long"]["geometry"] == long["geometry"]
    assert payload["short"]["geometry"] == short["geometry"]
    assert payload["leverage"] == "5"
    assert reverse_typed_compare(result.payloads[0], [{"symbol": "BTCUSDT", "long": long, "short": short}], capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE") is True


def test_unknown_capability_and_mixed_tf_fail_without_copying_side():
    long, short = candidate("LONG", tf="1h"), candidate("SHORT", tf="5m")
    result = render_portfolio(
        [{"symbol": "BTCUSDT", "long": long, "short": short}], capability={**capability(dual_tf=False), "opposite_opening": None}, planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    )
    assert result.status == UNKNOWN
    assert result.payloads == ()


def test_common_runtime_conflict_and_missing_explicit_policy_fail_closed():
    long, short = candidate("LONG"), candidate("SHORT")
    short["runtime"] = {"mode": "inverse"}
    conflict = render_portfolio(
        [{"symbol": "BTCUSDT", "long": long, "short": short}], capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    )
    assert conflict.status == UNKNOWN
    missing = render_portfolio(
        [{"symbol": "BTCUSDT", "long": long}], capability=capability(), planned_leverage="5", opposite_policy=None,
    )
    assert missing.status == UNKNOWN


def test_reverse_typed_compare_rejects_immutable_geometry_or_side_mutation():
    long, short = candidate("LONG"), candidate("SHORT")
    result = render_portfolio(
        [{"symbol": "BTCUSDT", "long": long, "short": short}], capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    )
    tampered = dict(result.payloads[0])
    tampered["long"] = dict(tampered["long"])
    tampered["long"]["lot_x"] = "999"
    rows = [{"symbol": "BTCUSDT", "long": long, "short": short}]
    assert reverse_typed_compare(tampered, rows, capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE") is False
    assert reverse_typed_compare(result.payloads[0], rows, capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE") is True


@pytest.mark.parametrize("mutation", ["lot_x", "geometry", "close", "leverage"])
def test_reverse_typed_compare_independently_rejects_tampered_executable_facts(mutation):
    long, short = candidate("LONG"), candidate("SHORT")
    result = render_portfolio(
        [{"symbol": "BTCUSDT", "long": long, "short": short}],
        capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    )
    tampered = json.loads(result.json_by_symbol["BTCUSDT"])
    if mutation == "lot_x":
        tampered["long"]["lot_x"] = "999"
    elif mutation == "geometry":
        tampered["long"]["geometry"] = {"entry": "tampered"}
    elif mutation == "close":
        tampered["close"]["LONG"]["enabled"] = False
    else:
        tampered["leverage"] = "6"
    assert reverse_typed_compare(
        tampered,
        [{"symbol": "BTCUSDT", "long": long, "short": short}],
        capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    ) is False


def test_reverse_typed_compare_accepts_json_decode_without_decimal_loss():
    long = candidate("LONG")
    result = render_portfolio(
        [{"symbol": "BTCUSDT", "long": long}],
        capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    )
    decoded = json.loads(result.json_by_symbol["BTCUSDT"])
    assert reverse_typed_compare(
        decoded, [{"symbol": "BTCUSDT", "long": long}],
        capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    ) is True


def test_render_exports_only_manifest_physical_field_names():
    manifest = capability()
    manifest["physical_fields"] = {"symbol": "sym", "leverage": "lev", "long": "buy", "short": "sell", "close": "ded", "opposite_policy": "opp"}
    result = render_portfolio(
        [{"symbol": "BTCUSDT", "long": candidate("LONG"), "short": candidate("SHORT")}], capability=manifest, planned_leverage="5", opposite_policy="CANCEL_OPPOSITE",
    )
    assert result.status == "PASS"
    payload = result.payloads[0]
    assert set(payload) == {"sym", "lev", "buy", "sell", "ded", "opp"}


def test_render_selected_variant_uses_rounded_orders_and_keeps_geometry():
    from mrs3.portfolio.search import search_portfolios

    long = candidate("LONG")
    long.update({"liquidity_scalar_pct_max": "100", "margin_scalar_pct_max": "100", "exchange_scalar_pct_max": "100", "d100": "2", "d100_currency": "USDT", "d100_timestamp_ms": 1000, "d100_expires_at_ms": 2000, "net_pnl": "10", "initial_margin": "2"})
    variant = search_portfolios(
        [long], capability=capability(), current_equity={"amount": "100", "currency": "USDT", "timestamp_ms": 1000, "expires_at_ms": 2000}, now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]}
    ).maximum
    result = render_portfolio(variant, capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE")
    assert result.status == "PASS"
    assert result.payloads[0]["long"]["orders"][0]["quantity"] == Decimal("1")
    assert result.payloads[0]["long"]["geometry"] == long["geometry"]


def test_direct_pair_slot_and_mapping_are_admission_checked():
    from mrs3.portfolio.search import PairSlot

    long, short = candidate("LONG"), candidate("SHORT")
    slot = PairSlot("BTCUSDT", long, short)
    assert render_portfolio(slot, capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE").status == "PASS"
    assert render_portfolio({"symbol": "BTCUSDT", "long": long, "short": short}, capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE").status == "PASS"
    invalid = dict(long, status="RESERVE")
    rejected = render_portfolio({"symbol": "BTCUSDT", "long": invalid}, capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE")
    assert rejected.status == UNKNOWN


def test_renderer_rejects_mixed_direct_slot_and_candidate_inputs():
    from mrs3.portfolio.search import PairSlot

    long = candidate("LONG")
    slot = PairSlot("BTCUSDT", long)
    result = render_portfolio(
        [slot, long], capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE"
    )
    assert result.status == UNKNOWN
    assert result.payloads == ()


def test_per_symbol_leverage_mapping_is_exact_and_runtime_is_preserved():
    rows = [
        {"symbol": "BTCUSDT", "long": candidate("LONG", "BTCUSDT")},
        {"symbol": "ETHUSDT", "long": candidate("LONG", "ETHUSDT")},
    ]
    result = render_portfolio(rows, capability=capability(), planned_leverage={"BTCUSDT": "5", "ETHUSDT": "6"}, opposite_policy="KEEP_OPPOSITE")
    assert result.status == "PASS"
    assert result.payloads[0]["long"]["runtime"] == {"mode": "linear"}
    missing = render_portfolio(rows, capability=capability(), planned_leverage={"BTCUSDT": "5"}, opposite_policy="KEEP_OPPOSITE")
    assert missing.status == UNKNOWN


def test_renderer_rejects_symbol_mismatch_and_numeric_type_changes():
    from mrs3.portfolio.search import PairSlot

    mismatched = PairSlot("ETHUSDT", candidate("LONG", "BTCUSDT"))
    assert render_portfolio(mismatched, capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE").status == UNKNOWN
    embedded = candidate()
    embedded["planned_leverage"] = Decimal("5")
    assert render_portfolio({"symbol": "BTCUSDT", "long": embedded}, capability=capability(), planned_leverage=5, opposite_policy="KEEP_OPPOSITE").status == UNKNOWN


def test_reverse_compare_returns_false_for_unrenderable_candidate():
    value = candidate()
    value.pop("dedicated_close")
    assert reverse_typed_compare({}, [{"symbol": "BTCUSDT", "long": value}], capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE") is False


def test_multi_order_render_omits_non_executable_aggregate_quantity():
    value = candidate()
    value["orders"] = [
        {"id": "first", "quantity": "1", "qty_step": "1", "min_qty": "1"},
        {"id": "second", "quantity": "2", "qty_step": "1", "min_qty": "1"},
    ]
    result = render_portfolio(
        [{"symbol": "BTCUSDT", "long": value}],
        capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    )
    assert result.status == "PASS"
    assert "quantity" not in result.payloads[0]["long"]
    assert [item["quantity"] for item in result.payloads[0]["long"]["orders"]] == ["1", "2"]


@pytest.mark.parametrize("bad_float", [float("nan"), float("inf")])
def test_renderer_rejects_nonfinite_candidate_facts(bad_float):
    value = candidate()
    value["geometry"] = {"entry": bad_float, "close": "dedicated"}
    result = render_portfolio(
        [{"symbol": "BTCUSDT", "long": value}],
        capability=capability(), planned_leverage="5", opposite_policy="KEEP_OPPOSITE",
    )
    assert result.status == UNKNOWN
    assert result.reason == "VALIDATION_FAILED"
    assert result.detail == "TYPED_COMPARISON_FAILED"
    assert result.payloads == ()
