from decimal import Decimal, Inexact, ROUND_HALF_EVEN, getcontext, localcontext

import pytest

from mrs3.portfolio.search import (
    FAIL,
    OPEN_POLICY,
    PASS,
    UNKNOWN,
    build_pair_slots,
    canonical_candidate_identity,
    classify_identity,
    enumerate_compositions,
    search_portfolios,
    _limit_parts,
    _variant_identity,
)
from mrs3.portfolio.canonical import PORTFOLIO_REASON_V1, PORTFOLIO_REASON_V2
from mrs3.portfolio.disposition import PORTFOLIO_REASON_CODES_V1, PORTFOLIO_REASON_CODES_V2


def candidate(side="LONG", symbol="BTCUSDT", *, scalar=100, tf="1h", dd=2):
    return {
        "status": "FINALIST",
        "symbol": symbol,
        "side": side,
        "strategy_id": f"strat-{side.lower()}",
        "result_id": f"result-{side.lower()}",
        "timeframe": tf,
        "geometry": {"entry": "ma", "close": "dedicated"},
        "lot_x": "1",
        "orders": [{"id": "open", "quantity": "3", "qty_step": "1", "min_qty": "1"}],
        "liquidity_scalar_pct_max": str(scalar),
        "margin_scalar_pct_max": str(scalar),
        "exchange_scalar_pct_max": str(scalar),
        "d100": str(dd),
        "d100_currency": "USDT",
        "d100_timestamp_ms": 1000,
        "d100_expires_at_ms": 2000,
        "net_pnl": "10",
        "initial_margin": "2",
        "runtime": {"mode": "linear"},
        "dedicated_close": {"enabled": True},
    }


def capability(*, dual_tf=True):
    return {
        "version": "adapter-v1",
        "dual_tf": dual_tf,
        "dedicated_close": True,
        "opposite_opening": True,
        "physical_fields": {
            "symbol": "symbol",
            "leverage": "leverage",
            "long": "long",
            "short": "short",
            "close": "close",
            "opposite_policy": "opposite_policy",
        },
        "common_runtime_fields": ["mode"],
    }


def equity():
    return {"amount": "100", "currency": "USDT", "timestamp_ms": 1000, "expires_at_ms": 2000}


def test_exact_finalist_pair_slots_have_no_status_fallback():
    slots = build_pair_slots([candidate("LONG"), candidate("SHORT"), {"symbol": "BTCUSDT", "side": "SHORT", "status": "RESERVE"}])
    assert len(slots) == 1
    assert slots[0].symbol == "BTCUSDT"
    assert slots[0].long["status"] == "FINALIST"
    assert slots[0].short["status"] == "FINALIST"
    assert build_pair_slots([{**candidate(), "status": "RESERVE"}]) == ()


def test_both_slot_has_explicit_directional_compositions_and_margin_exchange_use_minimum():
    slot = build_pair_slots([candidate("LONG"), candidate("SHORT")])[0]
    assert [item.composition for item in enumerate_compositions(slot)] == ["LONG", "SHORT", "LONG+SHORT"]
    capped = candidate(scalar=100)
    capped["margin_scalar_pct_max"] = "90"
    capped["exchange_scalar_pct_max"] = "40"
    capped["orders"][0]["quantity"] = "100"
    result = search_portfolios(
        [capped], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["25"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
    )
    assert result.status == PASS
    assert result.maximum.scalar == Decimal("25")


def test_scalar_dd_and_round_down_keep_immutable_geometry():
    long = candidate(scalar=80, dd=20)
    original = dict(long)
    result = search_portfolios(
        [long],
        capability=capability(),
        current_equity=equity(), now_ms=1500,
        dd_cap_pct="10",
        sizing_grid=["100", "50"],
        grid_version="grid-v1",
        max_pretest_variant_count=10,
        ranking_policy={"version": "rank-v1", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
    )
    assert result.status == PASS
    assert result.maximum.scalar == Decimal("50")
    assert long == original
    assert result.maximum.directions["LONG"].geometry == original["geometry"]
    assert result.maximum.directions["LONG"].quantity == Decimal("1")


def test_missing_dd_equity_or_cap_fails_closed_or_open_policy():
    unknown = search_portfolios(
        [candidate()], capability=capability(), current_equity=None, now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]}
    )
    assert unknown.status == UNKNOWN
    assert unknown.reason == "INDIVIDUAL_DD_UNAVAILABLE"
    policy = search_portfolios(
        [candidate()], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct=None, sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]}
    )
    assert policy.status == OPEN_POLICY


def test_all_grid_points_are_evaluated_without_monotonic_shortcut_and_budget_is_hard():
    seen = []

    def margin_gate(variant):
        seen.append(variant.scalar)
        return variant.scalar != Decimal("50")

    sized = candidate(dd=20)
    sized["orders"][0]["quantity"] = "100"
    result = search_portfolios(
        [sized],
        capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["100", "50", "25"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
        margin_gate=margin_gate, max_pretest_variant_count=3,
    )
    assert seen == [Decimal("100"), Decimal("50"), Decimal("25")]
    assert [variant.scalar for variant in result.tried] == [Decimal("100"), Decimal("50"), Decimal("25")]
    assert result.maximum.scalar == Decimal("25")
    limited_seen = []
    limited_value = candidate()
    limited_value["orders"][0]["quantity"] = "100"
    limited = search_portfolios(
        [limited_value], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["100", "50", "25"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
        max_pretest_variant_count=2, structural_gate=lambda variant: limited_seen.append(variant.scalar) or True,
    )
    assert limited_seen == [Decimal("100"), Decimal("50"), Decimal("25")]
    assert len(limited.tried) == 3
    assert limited.exhausted is True
    assert len(limited.excluded) == 1
    assert limited.excluded[0].reason == "ENUMERATION_FALLBACK_USED"
    assert limited.maximum.scalar == Decimal("100")
    assert limited.seed.scalar == Decimal("100")
    assert {variant.scalar for variant in limited.passing} == {Decimal("100"), Decimal("50")}
    excluded_identity = limited.excluded[0].variant_identity
    assert excluded_identity == _variant_identity(limited.tried[-1])
    assert excluded_identity not in limited.order
    assert excluded_identity not in limited.scheduling_order


def test_nonmonotonic_fail_does_not_reach_sink_and_zero_priority_is_retained():
    called = []
    result = search_portfolios(
        [candidate()], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["100", "50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
        priority=0, margin_gate=lambda variant: variant.scalar != Decimal("100"), sink=called.append,
    )
    assert result.maximum.scalar == Decimal("50")
    assert len(called) == 1
    assert called[0].scalar == Decimal("50")
    assert result.priority == 0


def test_open_policy_requires_versioned_explicit_metrics_and_scheduling_tie_uses_identity():
    no_policy = search_portfolios([candidate()], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["50"])
    assert no_policy.status == OPEN_POLICY
    first = candidate(); second = {**candidate(), "symbol": "ETHUSDT", "strategy_id": "other", "result_id": "other"}
    package = search_portfolios(
        [first, second], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["50"], ranking_policy={"version": "rank-v1", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
    )
    assert package.maximum is not None
    assert package.scheduling_key is not None
    assert package.scheduling_key.label == "scheduling_key"
    assert package.order == tuple(sorted(package.order))
    assert canonical_candidate_identity(first) != canonical_candidate_identity(second)


def test_mixed_timeframes_are_unsupported_without_dual_tf_capability():
    result = search_portfolios(
        [candidate("LONG", tf="1h"), candidate("SHORT", tf="5m")], capability=capability(dual_tf=False),
        current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
    )
    assert result.status == FAIL


def test_identity_only_reference_timestamp_is_not_a_new_trading_run():
    from mrs3.portfolio.search import classify_identity

    old = {"leverage": "5", "quantity": "1", "reference": {"timestamp_ms": 1, "mark": "10"}}
    fresh = {"leverage": "5", "quantity": "1", "reference": {"timestamp_ms": 2, "mark": "10"}}
    changed = {"leverage": "6", "quantity": "1", "reference": {"timestamp_ms": 2, "mark": "10"}}
    assert classify_identity(old, fresh).new_trading_run is False
    assert classify_identity(old, fresh).new_evaluation is False
    assert classify_identity(old, changed).new_trading_run is True


def test_identity_compares_typed_whole_executable_payload_and_rejects_bad_reference():
    base = {
        "manifest": {"qty": {"value": "1"}},
        "scalar": {"composition": "LONG"},
        "reference": {"timestamp_ms": 1, "mark": "10"},
    }
    renamed = {**base, "manifest": {"quantity": {"value": "1"}}}
    nested = {**base, "manifest": {"qty": {"value": "2"}}}
    composed = {**base, "scalar": {"composition": "SHORT"}}
    for changed in (renamed, nested, composed):
        result = classify_identity(base, changed)
        assert result.new_evaluation and result.new_trading_run
        assert result.detail == "EXECUTABLE_PAYLOAD_CHANGED"
    bad_reference = {**base, "reference": "not-a-mapping"}
    result = classify_identity(base, bad_reference)
    assert result.new_evaluation and result.new_trading_run
    assert result.detail == "VALIDATION_FAILED"


def test_identity_recursively_separates_nested_reference_facts():
    base = {
        "manifest_v2": {
            "symbol": "BTCUSDT",
            "executable": {"qty": {"value": "1"}},
            "reference": {"timestamp_ms": 1, "mark": "10"},
        },
        "composition": "LONG",
    }
    timestamp = {
        **base,
        "manifest_v2": {**base["manifest_v2"], "reference": {"timestamp_ms": 2, "mark": "10"}},
    }
    meaningful = {
        **base,
        "manifest_v2": {**base["manifest_v2"], "reference": {"timestamp_ms": 2, "mark": "11"}},
    }
    quantity = {
        **base,
        "manifest_v2": {
            **base["manifest_v2"],
            "executable": {"qty": {"value": "2"}},
        },
    }
    timestamp_result = classify_identity(base, timestamp)
    assert timestamp_result.new_evaluation is False
    assert timestamp_result.new_trading_run is False
    meaningful_result = classify_identity(base, meaningful)
    assert meaningful_result.new_evaluation is True
    assert meaningful_result.new_trading_run is False
    quantity_result = classify_identity(base, quantity)
    assert quantity_result.new_evaluation is True
    assert quantity_result.new_trading_run is True


def test_dd_requires_explicit_now_and_expiry_and_never_crashes_missing_expiry():
    value = candidate()
    value.pop("d100_expires_at_ms")
    result = search_portfolios([value], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]})
    assert result.status == UNKNOWN
    assert result.reason == "INDIVIDUAL_DD_UNAVAILABLE"
    result = search_portfolios([candidate()], capability=capability(), current_equity=equity(), dd_cap_pct="10", sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]})
    assert result.status == UNKNOWN
    assert result.reason == "INDIVIDUAL_DD_UNAVAILABLE"


def test_scalar_grid_can_exceed_one_hundred_when_ceilings_allow():
    value = candidate(scalar=150)
    value["orders"][0]["quantity"] = "100"
    result = search_portfolios([value], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="1000", sizing_grid=["125"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]})
    assert result.status == PASS
    assert result.maximum.scalar == Decimal("125")


def test_default_search_enumerates_compositions_and_stable_order():
    rows = [candidate("SHORT", symbol="ETHUSDT"), candidate("LONG", symbol="ETHUSDT"), candidate("LONG", symbol="BTCUSDT"), candidate("SHORT", symbol="BTCUSDT")]
    rows.reverse()
    result = search_portfolios(rows, capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]})
    assert [slot.symbol for slot in result.slots] == ["BTCUSDT", "ETHUSDT"]
    assert {variant.composition for variant in result.tried} == {"LONG", "SHORT", "LONG+SHORT"}


def test_invalid_order_is_retained_and_geometry_has_distinct_diagnostic():
    value = candidate()
    value["orders"].append({"id": "broken", "quantity": "2", "qty_step": "0"})
    value["geometry_valid"] = False
    called = []
    result = search_portfolios([value], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]}, sink=called.append)
    assert result.tried[0].reason == "VALIDATION_FAILED"
    assert "POST_ROUNDING_GEOMETRY" in result.tried[0].detail
    assert len(result.tried[0].directions["LONG"].orders) == 2
    assert called == []


def test_ranking_descriptors_missing_metric_excludes_variant_and_campaign_is_digest():
    result = search_portfolios([candidate()], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], grid_version="grid-x", ranking_policy={"version": "r", "metrics": [{"field": "missing", "direction": "DESC"}]})
    assert result.status == FAIL
    assert result.tried[0].reason == "VALIDATION_FAILED"
    assert result.campaign_identity and len(result.campaign_identity) == 64


def test_exhaustion_records_every_unvisited_variant_and_rejects_nonpositive_budget():
    value = candidate(); value["orders"][0]["quantity"] = "100"
    result = search_portfolios([value], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["25", "50", "75"], max_pretest_variant_count=1, ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]})
    assert result.exhausted is True
    assert len(result.tried) == 3
    assert len(result.excluded) == 2
    assert all(item.reason == "ENUMERATION_FALLBACK_USED" for item in result.excluded)
    assert all(item.evidence_class == "CONSERVATIVE_BOUND" for item in result.excluded)
    bad = search_portfolios([candidate()], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], max_pretest_variant_count=0, ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]})
    assert bad.status == FAIL


def test_variant_identity_includes_scalar_and_executable_facts_and_result_is_immutable():
    value = candidate(); value["orders"][0]["quantity"] = "100"
    result = search_portfolios([value], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]})
    assert len(result.order[0]) == 64
    assert result.maximum.directions["LONG"].orders[0]["quantity"] == Decimal("50")
    try:
        result.maximum.directions["LONG"].orders[0]["quantity"] = Decimal("1")
    except TypeError:
        pass
    else:
        raise AssertionError("result mappings must be immutable")


def test_reason_versions_are_canonical_and_package_exports_are_unique():
    import mrs3.portfolio as portfolio
    import mrs3.portfolio.render as render_module
    import mrs3.portfolio.search as search_module

    assert PORTFOLIO_REASON_CODES_V1 is PORTFOLIO_REASON_V1
    assert PORTFOLIO_REASON_CODES_V2 is PORTFOLIO_REASON_V2
    assert PORTFOLIO_REASON_V2 > PORTFOLIO_REASON_V1
    assert len(portfolio.__all__) == len(set(portfolio.__all__))
    assert portfolio.Variant is search_module.Variant
    assert portfolio.RenderResult is render_module.RenderResult


def test_search_is_independent_of_ambient_decimal_context():
    kwargs = dict(capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["33.3333"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]})
    value = candidate()
    value["orders"][0]["quantity"] = "100"
    baseline = search_portfolios([value], **kwargs)
    context = getcontext()
    saved = (context.prec, context.rounding, context.traps[Inexact])
    try:
        context.prec = 6
        context.rounding = ROUND_HALF_EVEN
        context.traps[Inexact] = True
        changed = search_portfolios([value], **kwargs)
    finally:
        context.prec, context.rounding, context.traps[Inexact] = saved
    assert changed.campaign_identity == baseline.campaign_identity
    assert changed.maximum.directions["LONG"].quantity == baseline.maximum.directions["LONG"].quantity


def test_scheduling_order_controls_sink_and_failed_attempts_change_campaign_identity():
    high_pnl = candidate(symbol="BTCUSDT")
    high_pnl["initial_margin"] = "20"
    efficient = candidate(symbol="ETHUSDT")
    efficient.update({"strategy_id": "eth", "result_id": "eth", "net_pnl": "4", "initial_margin": "1"})
    seen = []
    kwargs = dict(capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10", sizing_grid=["50"], ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]})
    result = search_portfolios([high_pnl, efficient], sink=lambda item: seen.append(item.slot.symbol), **kwargs)
    assert result.maximum.slot.symbol == "BTCUSDT"
    assert seen == ["ETHUSDT", "BTCUSDT"]
    assert result.scheduling_order != result.order
    margin_failed = search_portfolios([high_pnl], margin_gate=lambda _: False, **kwargs)
    liquidity_failed = search_portfolios([high_pnl], liquidity_gate=lambda _: False, **kwargs)
    assert margin_failed.campaign_identity != liquidity_failed.campaign_identity


def test_negative_scheduling_ratios_sort_by_numeric_value_and_priority_sequence_is_preserved():
    worse = candidate(symbol="BTCUSDT")
    worse.update({"strategy_id": "btc", "result_id": "btc", "net_pnl": "-10", "orders": [{"id": "open", "quantity": "100", "qty_step": "1", "min_qty": "1"}]})
    less_bad = candidate(symbol="ETHUSDT")
    less_bad.update({"strategy_id": "eth", "result_id": "eth", "net_pnl": "-1", "orders": [{"id": "open", "quantity": "100", "qty_step": "1", "min_qty": "1"}]})
    seen = []
    result = search_portfolios(
        [worse, less_bad], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["50"], priorities=[0, 1, 1], max_pretest_variant_count=2,
        ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
        sink=lambda variant: seen.append((variant.slot.symbol, variant.priority)),
    )
    assert seen[0][0] == "ETHUSDT"
    assert [variant.priority for variant in result.tried] == [0, 1, 1, 0, 1, 1]
    assert all(item.reason == "ENUMERATION_FALLBACK_USED" for item in result.excluded)


def test_no_pass_preserves_mixed_reasons_and_open_policy_takes_precedence():
    mixed = search_portfolios(
        [candidate()],
        capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["100", "50"],
        ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
        structural_gate=lambda variant: variant.scalar != Decimal("100"),
        liquidity_gate=lambda variant: False,
    )
    assert mixed.status == FAIL
    assert mixed.reason == "VALIDATION_FAILED"
    assert mixed.reasons == ("VALIDATION_FAILED", "LIQUIDITY_QUALITY_INSUFFICIENT", "NO_VALIDATION_PASS")

    open_policy = search_portfolios(
        [candidate()],
        capability=capability(), current_equity=equity(), now_ms=1500,
        sizing_grid=["100", "50"],
        ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
        dd_cap_pct=None,
        structural_gate=lambda variant: variant.scalar != Decimal("100"),
    )
    assert open_policy.status == OPEN_POLICY
    assert open_policy.reason == OPEN_POLICY
    assert "VALIDATION_FAILED" in open_policy.reasons
    assert "OPEN_POLICY" in open_policy.reasons
    assert "NO_VALIDATION_PASS" in open_policy.reasons


def test_maximum_is_highest_admissible_scalar_and_available_per_symbol():
    rows = [candidate(symbol="BTCUSDT"), candidate(symbol="ETHUSDT")]
    for row in rows:
        row["strategy_id"] = row["symbol"]
        row["result_id"] = row["symbol"]
        row["orders"][0]["quantity"] = "100"
    result = search_portfolios(
        rows, capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["25", "50"],
        ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
    )
    assert result.maximum.scalar == Decimal("50")
    assert set(result.maximum_by_symbol) == {"BTCUSDT", "ETHUSDT"}
    assert result.maximum_for_symbol("BTCUSDT").scalar == Decimal("50")
    assert result.maximum_for_symbol("ETHUSDT").scalar == Decimal("50")


def test_reason_v2_is_the_exact_additive_enum():
    assert PORTFOLIO_REASON_V2 == PORTFOLIO_REASON_V1 | {
        "INDIVIDUAL_DD_UNAVAILABLE", "INDIVIDUAL_DD_LIMIT", "LEVERAGE_UNVERIFIED"
    }


def test_dd_limit_is_independent_of_ambient_exponent_bounds():
    value = candidate()
    huge_equity = {**equity(), "amount": "1E+20"}
    baseline = _limit_parts(value, huge_equity, "1E+20", 1500)
    with localcontext() as context:
        context.Emax = 6
        context.Emin = -6
        constrained = _limit_parts(value, huge_equity, "1E+20", 1500)
    assert constrained == baseline


def test_multi_order_quantity_arithmetic_is_context_independent():
    value = candidate()
    value["orders"] = [
        {"id": "a", "quantity": "1.333333333333", "qty_step": "0.000000000001", "min_qty": "0"},
        {"id": "b", "quantity": "2.666666666667", "qty_step": "0.000000000001", "min_qty": "0"},
    ]
    kwargs = dict(
        capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["33.333333333333"],
        ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
    )
    baseline = search_portfolios([value], **kwargs)
    context = getcontext()
    saved = (context.prec, context.traps[Inexact])
    try:
        context.prec = 6
        context.traps[Inexact] = True
        changed = search_portfolios([value], **kwargs)
    finally:
        context.prec, context.traps[Inexact] = saved
    assert changed.campaign_identity == baseline.campaign_identity
    assert tuple(changed.maximum.directions["LONG"].orders) == tuple(baseline.maximum.directions["LONG"].orders)


def test_scheduling_ratio_sort_is_context_independent_for_long_multi_order_ratios():
    rows = []
    for symbol, pnl, margin in (
        ("BTCUSDT", "12345678901234567890.123456789", "0.1234567890123456789"),
        ("ETHUSDT", "12345678901234567889.987654321", "0.1234567890123456791"),
    ):
        value = candidate(symbol=symbol)
        value.update({"strategy_id": symbol, "result_id": symbol, "net_pnl": pnl, "initial_margin": margin})
        value["orders"] = [
            {"id": "a", "quantity": "100", "qty_step": "1", "min_qty": "1"},
            {"id": "b", "quantity": "80", "qty_step": "1", "min_qty": "1"},
        ]
        rows.append(value)
    kwargs = dict(
        capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["33.333333333333"],
        ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
    )
    baseline = search_portfolios(rows, **kwargs)
    context = getcontext()
    saved = (context.prec, context.traps[Inexact])
    try:
        context.prec = 6
        context.traps[Inexact] = True
        changed = search_portfolios(rows, **kwargs)
    finally:
        context.prec, context.traps[Inexact] = saved
    assert changed.scheduling_order == baseline.scheduling_order
    assert changed.campaign_identity == baseline.campaign_identity


def test_unparseable_order_quantity_is_validation_failed_and_unknown_reason_raises():
    value = candidate()
    value["orders"][0]["quantity"] = "not-a-decimal"
    result = search_portfolios(
        [value], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["50"],
        ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
    )
    assert result.tried[0].reason == "VALIDATION_FAILED"
    value = candidate()
    value.pop("orders")
    value["quantity"] = "not-a-decimal"
    no_order_result = search_portfolios(
        [value], capability=capability(), current_equity=equity(), now_ms=1500, dd_cap_pct="10",
        sizing_grid=["50"],
        ranking_policy={"version": "r", "metrics": [{"field": "net_pnl", "direction": "DESC"}]},
    )
    assert no_order_result.tried[0].reason == "VALIDATION_FAILED"
    from mrs3.portfolio.search import _stable, _typed_json
    with pytest.raises(ValueError):
        _stable("NOT_A_REASON")
    with pytest.raises(TypeError):
        _typed_json({"unordered": {"a", "b"}})
