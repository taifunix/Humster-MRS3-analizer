from decimal import Decimal, Inexact, ROUND_FLOOR, localcontext
from dataclasses import replace
from enum import IntEnum
import mrs3.portfolio as portfolio_package
import mrs3.portfolio.margin as margin_module
import pytest

from mrs3.portfolio.margin import (
    CALCULATED,
    CONSERVATIVE_BOUND,
    DepositResult,
    Evidence,
    FAIL,
    NEEDS_RETEST,
    PASS,
    UNKNOWN,
    FeeSchedule,
    MarginComponent,
    MarginResult,
    Order,
    Position,
    evaluate_limiter,
    evaluate_margin,
    evaluate_margin_envelope,
    deposit_sufficiency,
    planned_leverages,
    planned_leverage_for_symbol,
    round_quantity,
    sizing_notional,
    validate_applied_leverage,
    validate_quantity,
)


TIERS = (
    {"symbol": "BTCUSDT", "risk_limit_value": Decimal("1000"), "max_leverage": Decimal("50"), "maintenance_margin": Decimal("0.005")},
    {"symbol": "BTCUSDT", "risk_limit_value": Decimal("5000"), "max_leverage": Decimal("25"), "maintenance_margin": Decimal("0.01")},
)
FEES = FeeSchedule(Decimal("0.0001"), Decimal("0.0006"), "backtest_manifest", "manifest-1")
MODEL_GUARD = Evidence.calculated(True, provenance="fixture-model", timestamp_ms=1, model_version="fixture-v1")


class PriorityEnum(IntEnum):
    ZERO = 0


def margin(*args, **kwargs):
    kwargs.setdefault("model_guard", MODEL_GUARD)
    return evaluate_margin(*args, **kwargs)


def test_margin_keeps_position_and_order_components_and_combined_exposure_tier():
    result = margin(
        [Position("BTCUSDT", "LONG", "1", "600")],
        [Order("BTCUSDT", "LONG", "1", "600")],
        tiers=TIERS,
        leverage_steps={"BTCUSDT": Decimal("0.5")},
        fees=FEES,
        margin_balance=Decimal("1000"),
        collateral_haircut="0",
        order_loss="0",
    )
    assert result.status == PASS
    assert result.evidence_class == CALCULATED
    assert result.position_im is not None and result.order_im is not None
    assert result.total_im == result.position_im + result.order_im
    assert result.planned_leverage["BTCUSDT"] == Decimal("25")
    assert result.components[0].evidence.denominator == Decimal("600")


def test_decimal_margin_facts_replay_to_identical_canonical_outputs():
    kwargs = dict(
        positions=[Position("BTCUSDT", "LONG", "1", "100")],
        tiers=TIERS,
        leverage={"BTCUSDT": Decimal("10")},
        leverage_steps={"BTCUSDT": Decimal("1")},
        fees=FEES,
        margin_balance=Decimal("1000"),
        collateral_haircut=Decimal("0.10"),
        order_loss=Decimal("0.20"),
    )
    decimal_result = margin(**kwargs)
    string_result = margin(**{**kwargs, "leverage_steps": {"BTCUSDT": "1"}, "margin_balance": "1000", "collateral_haircut": "0.10", "order_loss": "0.20"})
    assert decimal_result.status == string_result.status == PASS
    assert decimal_result.total_im == string_result.total_im
    assert decimal_result.total_mm == string_result.total_mm
    assert decimal_result.order_loss_evidence.value == Decimal("0.20")
    assert decimal_result.collateral_haircut_evidence.value == Decimal("0.10")
    assert decimal_result.fee_evidence.value == string_result.fee_evidence.value


def test_missing_fee_or_nonpositive_denominator_is_unknown_and_never_zero():
    no_fee = margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": Decimal("50")}, leverage_steps={"BTCUSDT": "1"}, margin_balance=Decimal("1000"), collateral_haircut="0", order_loss="0")
    assert no_fee.status == UNKNOWN and no_fee.reason == "FEE_RATE_UNKNOWN"
    no_balance = margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": Decimal("50")}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, collateral_haircut="0", order_loss="0")
    assert no_balance.status == UNKNOWN and no_balance.reason == "EQUITY_DENOMINATOR_INVALID"
    bad_balance = margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": Decimal("50")}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1", collateral_haircut="0", order_loss="2")
    assert bad_balance.status == UNKNOWN and bad_balance.reason == "EQUITY_DENOMINATOR_INVALID"
    missing_loss = margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": Decimal("50")}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1000", collateral_haircut="0")
    assert missing_loss.status == UNKNOWN and missing_loss.order_loss_evidence.evidence_class == UNKNOWN
    missing_haircut = margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": Decimal("50")}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1000", order_loss="0")
    assert missing_haircut.status == UNKNOWN and missing_haircut.collateral_haircut_evidence.evidence_class == UNKNOWN


def test_missing_mark_and_same_symbol_requested_leverage_conflict_block_variant():
    missing_mark = margin([{"symbol": "BTCUSDT", "side": "LONG", "qty": "1"}], tiers=TIERS, leverage={"BTCUSDT": Decimal("50")}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1000", collateral_haircut="0", order_loss="0")
    assert missing_mark.status == UNKNOWN
    conflict = planned_leverages({"BTCUSDT": ("100", "0")}, tiers=TIERS, leverage_steps={"BTCUSDT": "1"}, requested_leverages={"BTCUSDT": ("10", "20")})
    assert conflict["BTCUSDT"].status == UNKNOWN


def test_leverage_rounds_down_and_conflict_or_missing_readback_blocks_run():
    planned = planned_leverage_for_symbol("BTCUSDT", position_exposure="1100", tiers=TIERS, leverage_step="3")
    assert planned.status == PASS and planned.leverage == Decimal("24")
    conflict = validate_applied_leverage({"BTCUSDT": Decimal("24")}, {"BTCUSDT": "25"})
    assert conflict.status == NEEDS_RETEST and conflict.reason == "LEVERAGE_MISMATCH"
    unreadable = validate_applied_leverage({"BTCUSDT": Decimal("24")}, {})
    assert unreadable.status == NEEDS_RETEST and unreadable.reason == "LEVERAGE_MISMATCH" and unreadable.witness["unreadable_symbols"] == ("BTCUSDT",)


def test_round_down_and_post_rounding_minimum_or_geometry_are_candidate_failures():
    assert round_quantity("1.09", "0.1") == Decimal("1.0")
    assert validate_quantity("1.09", qty_step="0.1", price="10", min_qty="1.1").status == FAIL
    assert validate_quantity("2", qty_step="0.1", price="10", min_qty="1", geometry_ok=False).reason == "POST_ROUNDING_GEOMETRY"


def test_dynamic_sizing_recalculates_base_and_cap_only_affects_sizing():
    assert sizing_notional("100", "10", "1", max_balance="200") == Decimal("10")
    assert sizing_notional("300", "10", "1", max_balance="200") == Decimal("20")


def test_limiter_l0_exempt_priority_zero_partial_close_and_pending_cancel():
    positions = [Position("A", "LONG", "0.1", "10", priority=1), Position("B", "LONG", "1", "10", priority=0)]
    orders = [Order("C", "LONG", "1", "10", priority=1, cancel_requested=True), Order("D", "LONG", "1", "10", priority=0)]
    result = evaluate_limiter(positions, orders, limit=None)
    assert result.status == PASS and result.counted_slots == 1 and result.exempt_slots == 1
    assert result.pending_cancels == ("C",) and set(result.allowed_openings) == {"C", "D"}
    zero = evaluate_limiter([Position("B", "LONG", "1", "10", priority=0)], [Order("D", "LONG", "1", "10", priority=0)], limit=0)
    assert zero.status == PASS and zero.counted_slots == 0 and zero.allowed_openings == ("D",)
    blocked = evaluate_limiter([], [Order("C", "LONG", "1", "10", priority=1)], limit=0)
    assert blocked.status == PASS and blocked.allowed_openings == () and blocked.reserved_openings == ("C",)
    partial = evaluate_limiter([Position("A", "LONG", "0.1", "10", priority=1)], limit=1, events=[{"pair_slot": "A", "qty_delta": "-0.1", "priority": 1}])
    assert partial.counted_slots == 0


def test_limiter_l1_blocks_counted_opening_but_keeps_exempt_and_unknown_priority_unknown():
    result = evaluate_limiter([Position("A", "LONG", "1", "10", priority=1)], [Order("B", "LONG", "1", "10", priority=1), Order("Z", "LONG", "1", "10", priority=0)], limit=1)
    assert result.status == PASS and result.allowed_openings == ("Z",) and result.race_slot_max == 2
    unknown = evaluate_limiter([Position("A", "LONG", "1", "10", priority=None)], limit=1)
    assert unknown.status == UNKNOWN
    same_symbol = evaluate_limiter([Position("A", "LONG", "1", "10", priority=1)], [Order("A", "LONG", "1", "10", priority=1)], limit=1)
    assert same_symbol.race_slot_max == 1


def test_enumeration_overflow_uses_conservative_bound_and_never_top_l():
    states = [((Position("A", "LONG", str(i + 1), "10"),), ()) for i in range(3)]
    result = evaluate_margin_envelope(states, lambda state: margin(state[0], state[1], tiers=({"symbol": "A", "risk_limit_value": "10000", "max_leverage": "10", "maintenance_margin": "0.01"},), leverage={"A": Decimal("10")}, leverage_steps={"A": "1"}, fees=FEES, margin_balance=Decimal("1000"), collateral_haircut="0", order_loss="0"), enumeration_limit=1, bound_builder=lambda _states: replace(margin([Position("A", "LONG", "3", "10")], tiers=({"symbol": "A", "risk_limit_value": "10000", "max_leverage": "10", "maintenance_margin": "0.01"},), leverage={"A": Decimal("10")}, leverage_steps={"A": "1"}, fees=FEES, margin_balance=Decimal("1000"), collateral_haircut="0", order_loss="0"), all_executable=True))
    assert result.evidence_class == CONSERVATIVE_BOUND and result.reason == "ENUMERATION_FALLBACK_USED"
    assert result.result.total_im >= Decimal("0")


def test_fixed_absolute_deposit_check_is_available():
    result = deposit_sufficiency("100", total_im="40", total_mm="10", min_free_margin_reserve_pct="20", max_mm_load_pct="50")
    assert result.status == PASS and result.minimum_deposit == Decimal("50")


def test_missing_mmr_is_unknown_and_caller_leverage_obeys_cap_and_step():
    missing_mmr = margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=({"symbol": "BTCUSDT", "risk_limit_value": "1000", "max_leverage": "50"},), leverage={"BTCUSDT": "10"}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1000", collateral_haircut="0", order_loss="0")
    assert missing_mmr.status == UNKNOWN and missing_mmr.reason == "MARGIN_BOUND_UNAVAILABLE"
    cap = margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": "26"}, leverage_steps={"BTCUSDT": "3"}, fees=FEES, margin_balance="1000", collateral_haircut="0", order_loss="0")
    assert cap.status == UNKNOWN and cap.reason == "MARGIN_BOUND_FAILED"


def test_exact_tier_boundary_uses_next_tier_and_quantity_zero_fails():
    boundary = planned_leverage_for_symbol("BTCUSDT", position_exposure="1000", tiers=TIERS, leverage_step="1")
    assert boundary.status == PASS and boundary.tier["risk_limit_value"] == Decimal("5000")
    assert validate_quantity("0.09", qty_step="0.1", price="10", min_qty="0").reason == "POST_ROUNDING_MINIMUM"


def test_fee_model_uses_maker_open_and_taker_close_by_default():
    result = margin([Position("BTCUSDT", "LONG", "1", "100")], [Order("BTCUSDT", "LONG", "1", "100", maker=True)], tiers=TIERS, leverage={"BTCUSDT": "10"}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1000", collateral_haircut="0", order_loss="0")
    assert result.status == PASS
    assert result.position_im == Decimal("10.0600")
    assert result.order_im == Decimal("10.0700")
    assert result.position_mm == Decimal("0.5600")


def test_mapping_fee_source_is_required_and_deployment_needs_provenance():
    base = {"MakerFee": "0.0001", "TakerFee": "0.0006"}
    args = dict(positions=[Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": "10"}, leverage_steps={"BTCUSDT": "1"}, margin_balance="1000", collateral_haircut="0", order_loss="0")
    assert margin(fees=base, **args).reason == "FEE_RATE_UNKNOWN"
    assert margin(fees={**base, "source": "deployment"}, **args).reason == "FEE_RATE_UNKNOWN"
    assert margin(fees={**base, "source": "deployment", "provenance": "account-snapshot-1"}, **args).status == PASS


def test_mapping_order_uses_only_unfilled_quantity_and_opposite_same_symbol_is_reserved():
    result = margin([Position("BTCUSDT", "LONG", "1", "100")], [{"symbol": "BTCUSDT", "side": "SHORT", "qty": "2", "filled_qty": "1", "price": "100"}], tiers=TIERS, leverage={"BTCUSDT": "10"}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1000", collateral_haircut="0", order_loss="0")
    assert result.status == PASS and result.components[1].notional == Decimal("100")


def test_limiter_caps_counted_openings_and_keeps_reserved_race_set():
    result = evaluate_limiter([Position("A", "LONG", "1", "10", priority=1)], [Order("B", "LONG", "1", "10", priority=1), Order("C", "LONG", "1", "10", priority=1), Order("D", "LONG", "1", "10", priority=1), Order("Z", "LONG", "1", "10", priority=0)], limit=2)
    assert result.allowed_openings == ("B", "Z")
    assert len(result.reserved_openings) == 2 and result.race_slot_max == 4
    late = evaluate_limiter([Position("A", "LONG", "1", "10", priority=1)], [Order("B", "LONG", "1", "10", priority=1, cancel_requested=True)], limit=1)
    assert late.counted_slots == 1 and late.pending_cancels == ("B",)


def test_envelope_fails_closed_under_limit_and_does_not_enumerate_overflow():
    invalid = evaluate_margin_envelope(["bad"], lambda _state: margin([], [], tiers=(), leverage=None, leverage_steps=None, fees=FEES, margin_balance="1000", collateral_haircut="0", order_loss="0"), enumeration_limit=2)
    assert invalid.status == UNKNOWN
    calls = []
    states = tuple(range(3))
    result = evaluate_margin_envelope(states, lambda state: calls.append(state) or None, enumeration_limit=1, bound_builder=lambda _states: replace(margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": "10"}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1000", collateral_haircut="0", order_loss="0"), all_executable=True))
    assert result.evidence_class == CONSERVATIVE_BOUND and result.reason == "ENUMERATION_FALLBACK_USED" and calls == []


def test_evidence_and_dedicated_result_types_do_not_fabricate_margin_totals():
    result = margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": "10"}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1000", collateral_haircut="0", order_loss="0", timestamp_ms=123, model_version="fixture-v1")
    assert result.status == PASS and result.denominator.timestamp_ms == 123 and result.denominator.model_version == "fixture-v1" and result.denominator.numerator == result.total_im and result.fee_evidence.timestamp_ms == 123
    unknown = margin([Position("BTCUSDT", "LONG", "1", "100")], tiers=TIERS, leverage={"BTCUSDT": "10"}, leverage_steps={"BTCUSDT": "1"}, fees=FEES, margin_balance="1000", collateral_haircut="0")
    assert unknown.status == UNKNOWN and unknown.total_im is None and unknown.total_mm is None
    deposit = deposit_sufficiency("100", total_im="40", total_mm="10", min_free_margin_reserve_pct="20", max_mm_load_pct="50")
    assert isinstance(deposit, DepositResult) and not hasattr(deposit, "position_im") and deposit.total_im == Decimal("40")


def test_model_guard_is_required_and_unknown_guard_fails_closed():
    args = dict(
        positions=[Position("BTCUSDT", "LONG", "1", "100")],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    omitted = evaluate_margin(**args)
    unknown = evaluate_margin(model_guard=Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"), **args)
    assert omitted.status == UNKNOWN and omitted.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert unknown.status == UNKNOWN and unknown.reason == "MARGIN_BOUND_UNAVAILABLE"


def test_adjusted_denominator_cannot_pass_when_margin_is_overcommitted():
    result = margin(
        [Position("BTCUSDT", "LONG", "1", "100")],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1",
        collateral_haircut="0",
        order_loss="0",
    )
    assert result.status == FAIL and result.reason == "MARGIN_BOUND_FAILED"
    assert result.total_im is not None and result.total_mm is not None


def test_under_limit_envelope_is_componentwise_and_keeps_metric_witnesses():
    def known(im, mm, denominator):
        return MarginResult(
            PASS, None, CALCULATED, im, Decimal("0"), im, mm, mm,
            Evidence.calculated(denominator, provenance="fixture"),
        )

    result = evaluate_margin_envelope(
        (1, 2),
        lambda state: known(Decimal("100"), Decimal("10"), Decimal("200"))
        if state == 1 else known(Decimal("10"), Decimal("100"), Decimal("300")),
        enumeration_limit=2,
    )
    assert result.status == PASS
    assert result.result.total_im == Decimal("100")
    assert result.result.total_mm == Decimal("100")
    assert result.result.denominator.value == Decimal("200")
    assert result.witness["im_witness"] == 0
    assert result.witness["mm_witness"] == 1
    assert result.witness["denominator_witness"] == 0
    assert result.result.witness["total_im"] == result.result.total_im
    assert result.result.witness["total_mm"] == result.result.total_mm
    assert result.result.witness["denominator"] == result.result.denominator.value


def test_envelope_rejects_malformed_evaluator_result_without_raising():
    result = evaluate_margin_envelope((1,), lambda _state: {"status": PASS}, enumeration_limit=2)
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"


def test_overflow_requires_explicit_all_executable_attestation_and_evaluates_zero_states():
    bound = margin(
        [Position("BTCUSDT", "LONG", "1", "100")],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    missing = evaluate_margin_envelope((1, 2), lambda _: bound, enumeration_limit=1, bound_builder=lambda _: bound)
    present = evaluate_margin_envelope((1, 2), lambda _: bound, enumeration_limit=1, bound_builder=lambda _: replace(bound, all_executable=True))
    malformed = evaluate_margin_envelope((1, 2), lambda _: bound, enumeration_limit=1, bound_builder=lambda _: replace(bound, denominator=None, all_executable=True))
    assert missing.status == UNKNOWN and missing.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert present.status == PASS and present.evidence_class == CONSERVATIVE_BOUND and present.evaluated_states == 0
    assert malformed.status == UNKNOWN and malformed.reason == "MARGIN_BOUND_UNAVAILABLE"


def test_planned_leverage_malformed_and_zero_requests_are_unknown_not_exceptions():
    malformed = planned_leverages(
        {"BTCUSDT": ("100", "0")},
        tiers=TIERS,
        leverage_steps={"BTCUSDT": "1"},
        requested_leverages={"BTCUSDT": ("bad",)},
    )
    zero = planned_leverages(
        {"BTCUSDT": ("100", "0")},
        tiers=TIERS,
        leverage_steps={"BTCUSDT": "1"},
        requested_leverages={"BTCUSDT": ("0",)},
    )
    zero_exposure = planned_leverages(
        {"BTCUSDT": ("0", "0")}, tiers=TIERS, leverage_steps={"BTCUSDT": "1"}
    )
    assert malformed["BTCUSDT"].status == UNKNOWN and malformed["BTCUSDT"].reason == "MARGIN_BOUND_UNAVAILABLE"
    assert zero["BTCUSDT"].status == UNKNOWN and zero["BTCUSDT"].reason == "MARGIN_BOUND_UNAVAILABLE"
    assert zero_exposure["BTCUSDT"].status == UNKNOWN and zero_exposure["BTCUSDT"].reason == "MARGIN_BOUND_UNAVAILABLE"


def test_quantity_diagnostic_retains_exact_post_rounding_checks():
    result = validate_quantity("0.09", qty_step="0.1", price="10", min_qty="0")
    assert result.reason == "POST_ROUNDING_MINIMUM"
    assert result.checks["positive"] is False
    assert result.checks["min_qty"] is True


def test_composed_envelope_rechecks_sufficiency_and_clears_source_state():
    def known(im, mm, denominator):
        return MarginResult(
            PASS,
            None,
            CALCULATED,
            im,
            Decimal("0"),
            im,
            mm,
            mm,
            Evidence.calculated(denominator, provenance="fixture"),
            components=(MarginComponent("position", "X", im, im, mm, Decimal("0"), Evidence.calculated(im, provenance="fixture")),),
            planned_leverage={"X": Decimal("10")},
            fee_evidence=Evidence.observed(Decimal("0.001"), provenance="fixture"),
            order_loss_evidence=Evidence.calculated(Decimal("0"), provenance="fixture"),
            collateral_haircut_evidence=Evidence.calculated(Decimal("0"), provenance="fixture"),
            model_guard_evidence=Evidence.calculated(True, provenance="fixture"),
        )

    result = evaluate_margin_envelope(
        (1, 2, 3),
        lambda state: known(Decimal("70"), Decimal("10"), Decimal("100"))
        if state == 1 else known(Decimal("10"), Decimal("70"), Decimal("100"))
        if state == 2 else known(Decimal("10"), Decimal("10"), Decimal("50")),
        enumeration_limit=3,
    )
    assert result.status == FAIL and result.reason == "MARGIN_BOUND_FAILED"
    assert result.evidence_class == CONSERVATIVE_BOUND
    assert result.result.status == FAIL and result.result.reason == "MARGIN_BOUND_FAILED"
    assert result.result.components == ()
    assert result.result.planned_leverage == {}
    assert not hasattr(result.result, "quantities")
    assert result.result.denominator.evidence_class == CONSERVATIVE_BOUND
    assert result.result.denominator.numerator == result.result.total_im
    assert result.result.fee_evidence.evidence_class == CONSERVATIVE_BOUND
    assert result.result.order_loss_evidence.evidence_class == CONSERVATIVE_BOUND
    assert result.result.collateral_haircut_evidence.evidence_class == CONSERVATIVE_BOUND
    assert result.result.model_guard_evidence.evidence_class == CONSERVATIVE_BOUND
    assert result.result.all_executable is False
    assert result.result.position_im + result.result.order_im == result.result.total_im
    assert result.witness["im_source"] == 0
    assert result.witness["mm_source"] == 1
    assert result.witness["denominator_source"] == 2


def test_limiter_separates_live_orders_from_proposed_openings_and_fails_closed_over_limit():
    position = Position("A", "LONG", "1", "10", priority=1)
    live = Order("B", "LONG", "1", "10", priority=1)
    proposed = Order("C", "LONG", "1", "10", priority=1)
    over_limit = evaluate_limiter([position], live_orders=[live], proposed_openings=[proposed], limit=1)
    reserved = evaluate_limiter([position], proposed_openings=[proposed], limit=1)
    confirmed = evaluate_limiter([position], live_orders=[replace(live, cancel_requested=True, cancel_confirmed=True)], limit=1)
    assert over_limit.status == UNKNOWN and over_limit.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert reserved.status == PASS and reserved.reserved_openings == ("C",)
    assert confirmed.status == PASS and confirmed.pending_cancels == ()


def test_close_maker_flag_cannot_reduce_estimated_taker_close_fee():
    result = margin(
        [Position("BTCUSDT", "LONG", "1", "100", maker=True)],
        [Order("BTCUSDT", "LONG", "1", "100", maker=True, close_maker=True)],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    assert result.status == PASS
    assert result.position_im == Decimal("10.0600")
    assert result.order_im == Decimal("10.0700")


def test_missing_leverage_step_is_unavailable_and_quantity_checks_are_explicit():
    missing = margin(
        [Position("BTCUSDT", "LONG", "1", "100")],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    quantity = validate_quantity("1", qty_step="1", price="10", min_qty="0", max_qty="0.5", liquidity_ok=False, margin_ok=False)
    assert missing.status == UNKNOWN and missing.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert quantity.reason == "POST_ROUNDING_MINIMUM"
    assert quantity.checks == {"positive": True, "min_qty": True, "min_notional": True, "max_qty": False, "geometry": True, "liquidity": False, "margin": False}


def test_deposit_evidence_retains_fixed_size_numerator_denominator_and_metadata():
    result = deposit_sufficiency("100", total_im="40", total_mm="10", min_free_margin_reserve_pct="20", max_mm_load_pct="50", timestamp_ms=123, model_version="deposit-v2")
    assert result.denominator.numerator == Decimal("40")
    assert result.denominator.denominator == Decimal("100")
    assert result.denominator.timestamp_ms == 123
    assert result.denominator.model_version == "deposit-v2"


def test_rounding_and_planned_leverage_are_exact_for_huge_decimals():
    huge = Decimal("9" * 500)
    quantity = Decimal("9" * 500 + ".09")
    rounded = round_quantity(quantity, "0.1")
    assert rounded == huge and rounded <= quantity
    planned = planned_leverage_for_symbol(
        "X",
        position_exposure="1",
        tiers=({"symbol": "X", "risk_limit_value": "100", "max_leverage": "" + "9" * 500 + "1"},),
        leverage_step="3",
    )
    assert planned.status == PASS
    assert planned.leverage == Decimal("9" * 500 + "0")


def test_margin_sizing_and_deposit_ignore_ambient_decimal_context_and_traps():
    margin_args = dict(
        positions=[Position("BTCUSDT", "LONG", "1", "100")],
        tiers=TIERS,
        leverage={"BTCUSDT": "3"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    base_margin = margin(**margin_args)
    base_sizing = sizing_notional("123456789", "17", "3")
    base_deposit = deposit_sufficiency("3334", total_im="1", total_mm="100", min_free_margin_reserve_pct="0", max_mm_load_pct="3")
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_FLOOR
        context.Emax = 6
        context.Emin = -6
        context.traps[Inexact] = True
        constrained_margin = margin(**margin_args)
        constrained_sizing = sizing_notional("123456789", "17", "3")
        constrained_deposit = deposit_sufficiency("3334", total_im="1", total_mm="100", min_free_margin_reserve_pct="0", max_mm_load_pct="3")
    assert constrained_margin.total_im == base_margin.total_im
    assert constrained_margin.evidence_class == CONSERVATIVE_BOUND
    assert constrained_sizing == base_sizing
    assert constrained_deposit.minimum_deposit == base_deposit.minimum_deposit
    assert constrained_deposit.minimum_deposit * Decimal("3") >= Decimal("10000")


def test_unknown_evidence_requires_a_stable_v1_reason():
    with pytest.raises(ValueError, match="stable portfolio_reason_v1"):
        Evidence.unknown("missing_order_loss")


def test_package_exports_are_unique_and_reference_margin_types():
    assert len(portfolio_package.__all__) == len(set(portfolio_package.__all__))
    for name in ("QuantityResult", "LeverageResult", "LeverageReadbackResult", "EnvelopeResult", "DepositResult"):
        assert getattr(portfolio_package, name) is getattr(margin_module, name)


def test_round_down_handles_coefficients_beyond_python_int_string_limit():
    digits = "9" * 4301
    value = Decimal(digits + ".09")
    rounded = round_quantity(value, "0.1")
    assert rounded == Decimal(digits)
    leverage = planned_leverage_for_symbol(
        "X",
        position_exposure="1",
        tiers=({"symbol": "X", "risk_limit_value": "100", "max_leverage": digits + "1"},),
        leverage_step="3",
    )
    assert leverage.status == PASS and leverage.leverage == Decimal(digits + "0")


def test_divide_up_marks_high_precision_terminating_quotient_as_conservative():
    quotient = Decimal("9" * 10001)
    value, conservative = margin_module._divide_up(quotient, Decimal("1"))
    assert value >= quotient
    assert conservative is True


def test_overflow_bound_rechecks_attested_denominator_sufficiency():
    bound = margin(
        [Position("BTCUSDT", "LONG", "1", "100")],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    inconsistent = replace(bound, all_executable=True, total_im=Decimal("2000"), total_mm=Decimal("2000"))
    result = evaluate_margin_envelope((1, 2), lambda _: bound, enumeration_limit=1, bound_builder=lambda _: inconsistent)
    assert result.status == FAIL
    assert result.reason == "MARGIN_BOUND_FAILED"
    assert result.result is not None and result.result.reason == "MARGIN_BOUND_FAILED"


def test_zero_exposure_flat_positions_and_fully_filled_orders_do_not_block_leverage():
    result = margin(
        [Position("BTCUSDT", "LONG", "1", "100"), Position("FLAT", "LONG", "0")],
        [Order("FILLED", "LONG", "1", "100", filled_qty="1", status="FILLED")],
        tiers=TIERS,
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    assert result.status == PASS
    assert tuple(result.planned_leverage) == ("BTCUSDT",)


def test_pending_cancel_priority_zero_is_exempt_and_witness_is_immutable():
    result = evaluate_limiter(
        [Position("A", "LONG", "1", "10", priority=1)],
        limit=1,
        pending_cancels=[Order("Z", "LONG", "1", "10", priority=0, cancel_requested=True)],
    )
    assert result.pending_cancels == ()
    with pytest.raises(TypeError):
        result.witness["new"] = True
    exempt_cancel = evaluate_limiter(
        [Position("A", "LONG", "1", "10", priority=0)],
        limit=1,
        pending_cancels=[{"pair_slot": "A", "cancel_requested": True}],
    )
    assert exempt_cancel.status == PASS and exempt_cancel.pending_cancels == ()
    unknown_cancel = evaluate_limiter(
        [],
        limit=1,
        pending_cancels=[{"pair_slot": "missing", "cancel_requested": True}],
    )
    assert unknown_cancel.status == UNKNOWN and unknown_cancel.reason == "MARGIN_BOUND_UNAVAILABLE"
    source = {"nested": {"items": [{"value": 1}]}}
    known = MarginResult(
        PASS,
        None,
        CALCULATED,
        Decimal("1"),
        Decimal("0"),
        Decimal("1"),
        Decimal("0"),
        Decimal("0"),
        Evidence.calculated(Decimal("2"), provenance="fixture"),
        witness=source,
    )
    frozen = evaluate_margin_envelope((1,), lambda _state: known, enumeration_limit=1)
    with pytest.raises(TypeError):
        frozen.witness["nested"]["items"][0]["value"] = 2
    source["nested"]["items"][0]["value"] = 2
    assert frozen.witness["nested"]["items"][0]["value"] == 1


def test_unconfirmed_position_and_nonactive_order_fail_closed():
    unconfirmed = margin(
        [Position("BTCUSDT", "LONG", "1", "100", confirmed=False)],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    cancelled = margin(
        [],
        [Order("BTCUSDT", "LONG", "1", "100", status="CANCELED")],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    assert unconfirmed.status == UNKNOWN and unconfirmed.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert cancelled.status == UNKNOWN and cancelled.reason == "MARGIN_BOUND_UNAVAILABLE"


@pytest.mark.parametrize("confirmed", [0, None, "", "false", 1, "true"])
def test_only_exact_true_position_confirmation_is_accepted_by_margin_and_limiter(confirmed):
    position = Position("BTCUSDT", "LONG", "1", "100", confirmed=confirmed)
    result = margin(
        [position],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    limiter = evaluate_limiter([position], limit=1)
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert limiter.status == UNKNOWN and limiter.reason == "MARGIN_BOUND_UNAVAILABLE"


def test_mapping_confirmation_also_requires_exact_true():
    position = {"symbol": "BTCUSDT", "side": "LONG", "qty": "1", "mark_price": "100", "confirmed": "false"}
    result = margin(
        [position],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    limiter = evaluate_limiter([position], limit=1)
    assert result.status == UNKNOWN and limiter.status == UNKNOWN


@pytest.mark.parametrize(
    ("field", "value"),
    [("reduce_only", "no"), ("maker", 1), ("cancel_confirmed", "false"), ("cancel_requested", "0")],
)
def test_mapping_order_booleans_fail_closed(field, value):
    order = {"symbol": "BTCUSDT", "side": "LONG", "qty": "1", "price": "100", field: value}
    result = margin(
        [],
        [order],
        tiers=TIERS,
        leverage={"BTCUSDT": "10"},
        leverage_steps={"BTCUSDT": "1"},
        fees=FEES,
        margin_balance="1000",
        collateral_haircut="0",
        order_loss="0",
    )
    limiter = evaluate_limiter([], proposed_openings=[order], limit=1)
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert limiter.status == UNKNOWN and limiter.reason == "MARGIN_BOUND_UNAVAILABLE"


@pytest.mark.parametrize("field", ["reduce_only", "maker", "close_maker", "cancel_requested", "cancel_confirmed"])
def test_order_constructor_rejects_non_boolean_decision_fields(field):
    with pytest.raises(TypeError, match="must be a bool"):
        Order("BTCUSDT", "LONG", "1", "100", **{field: 1})


def test_envelope_cross_composed_margin_failure_is_written_to_inner_result():
    def known(im, mm, denominator):
        return MarginResult(
            PASS,
            None,
            CALCULATED,
            im,
            Decimal("0"),
            im,
            Decimal("0"),
            mm,
            Evidence.calculated(denominator, provenance="fixture"),
        )

    result = evaluate_margin_envelope(
        (1, 2),
        lambda state: known(Decimal("110"), Decimal("10"), Decimal("100"))
        if state == 1 else known(Decimal("10"), Decimal("110"), Decimal("100")),
        enumeration_limit=2,
    )
    assert result.status == FAIL and result.reason == "MARGIN_BOUND_FAILED"
    assert result.result.status == FAIL and result.result.reason == "MARGIN_BOUND_FAILED"
    assert result.result.total_im == Decimal("110")
    assert result.result.total_mm == Decimal("110")
    assert result.result.denominator.value == Decimal("100")
    assert result.result.witness["total_im"] == result.result.total_im
    assert result.result.witness["total_mm"] == result.result.total_mm
    assert result.result.witness["denominator"] == result.result.denominator.value


def test_event_only_slot_without_priority_fails_closed():
    result = evaluate_limiter(
        [],
        limit=1,
        events=[{"pair_slot": "event-only", "qty_delta": "1"}],
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.witness["missing_priority"] == ("event-only",)


@pytest.mark.parametrize("priority", [False, True, "0", 0.0, Decimal("0"), PriorityEnum.ZERO])
def test_non_integer_position_priority_fails_closed_without_exemption(priority):
    result = evaluate_limiter(
        [{"pair_slot": "position", "qty": "1", "priority": priority}],
        limit=1,
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()
    assert result.witness["invalid_priority"] == ("position",)


@pytest.mark.parametrize("priority", [False, True, "0", 0.0, Decimal("0"), PriorityEnum.ZERO])
def test_non_integer_event_priority_fails_closed_without_exemption(priority):
    result = evaluate_limiter(
        [],
        limit=1,
        events=[{"pair_slot": "event", "qty_delta": "1", "priority": priority}],
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()


@pytest.mark.parametrize("priority", [False, True, "0", 0.0, Decimal("0"), PriorityEnum.ZERO])
def test_non_integer_proposed_priority_fails_closed_without_exemption(priority):
    result = evaluate_limiter(
        [],
        limit=1,
        proposed_openings=[{"pair_slot": "opening", "qty": "1", "price": "10", "priority": priority}],
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()


def test_mixed_integer_and_non_integer_priorities_fail_closed_without_sorting():
    result = evaluate_limiter(
        [
            {"pair_slot": "bad", "qty": "1", "priority": "1"},
            {"pair_slot": "good", "qty": "1", "priority": 1},
        ],
        limit=2,
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()


@pytest.mark.parametrize("priority", [False, True, "0", 0.0, Decimal("0"), PriorityEnum.ZERO])
def test_invalid_event_priority_cannot_override_position_zero_exemption(priority):
    result = evaluate_limiter(
        [{"pair_slot": "slot", "qty": "1", "priority": 0}],
        limit=1,
        events=[{"pair_slot": "slot", "qty_delta": "0", "priority": priority}],
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()


@pytest.mark.parametrize("priority", [False, True, "0", 0.0, Decimal("0"), PriorityEnum.ZERO])
def test_invalid_position_priority_cannot_be_repaired_by_valid_event(priority):
    result = evaluate_limiter(
        [{"pair_slot": "slot", "qty": "1", "priority": priority}],
        limit=1,
        events=[{"pair_slot": "slot", "qty_delta": "0", "priority": 1}],
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()


@pytest.mark.parametrize("priority", [False, True, "0", 0.0, Decimal("0"), PriorityEnum.ZERO])
def test_invalid_live_and_proposed_priorities_cannot_override_position_zero(priority):
    result = evaluate_limiter(
        [{"pair_slot": "slot", "qty": "1", "priority": 0}],
        limit=1,
        live_orders=[{"pair_slot": "slot", "qty": "1", "priority": priority}],
        proposed_openings=[{"pair_slot": "slot", "qty": "1", "priority": priority}],
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()
    assert result.witness["unknown_priority"] == ("slot",)
    assert result.witness["invalid_priority"] == ("slot",)
    assert result.witness["missing_priority"] == ()
    assert result.witness["conflicting_priority"] == ()


def test_position_priority_missing_is_order_independent_and_blocks_zero_exemption():
    missing = {"pair_slot": "slot", "qty": "1"}
    zero = {"pair_slot": "slot", "qty": "1", "priority": 0}
    results = tuple(
        evaluate_limiter(positions, limit=1)
        for positions in ((missing, zero), (zero, missing))
    )
    assert all(result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE" for result in results)
    assert all(result.exempt_pair_slots == () and result.allowed_openings == () for result in results)
    assert all(result.witness["unknown_priority"] == ("slot",) for result in results)
    assert all(result.witness["missing_priority"] == ("slot",) for result in results)
    assert all(result.witness["invalid_priority"] == () for result in results)
    assert all(result.witness["conflicting_priority"] == () for result in results)


def test_new_proposed_slot_without_priority_fails_closed():
    result = evaluate_limiter(
        [],
        limit=1,
        proposed_openings=[{"pair_slot": "new", "qty": "1"}],
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()
    assert result.witness["unknown_priority"] == ("new",)
    assert result.witness["missing_priority"] == ("new",)
    assert result.witness["invalid_priority"] == ()
    assert result.witness["conflicting_priority"] == ()


@pytest.mark.parametrize(
    "source",
    [
        {"events": [{"pair_slot": "slot", "qty_delta": "0"}]},
        {"live_orders": [{"pair_slot": "slot", "qty": "1"}]},
        {"proposed_openings": [{"pair_slot": "slot", "qty": "1"}]},
        {"pending_cancels": [{"pair_slot": "slot", "cancel_requested": True}]},
    ],
)
def test_missing_non_position_priority_inherits_known_position_priority(source):
    result = evaluate_limiter(
        [{"pair_slot": "slot", "qty": "1", "priority": 0}],
        limit=1,
        **source,
    )
    assert result.status == PASS
    assert result.exempt_pair_slots == ("slot",)


@pytest.mark.parametrize("priority", [False, True, "0", 0.0, Decimal("0"), PriorityEnum.ZERO])
def test_invalid_pending_priority_blocks_known_slot(priority):
    result = evaluate_limiter(
        [{"pair_slot": "slot", "qty": "1", "priority": 0}],
        limit=1,
        pending_cancels=[{"pair_slot": "slot", "priority": priority, "cancel_requested": True}],
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()
    assert result.witness["unknown_priority"] == ("slot",)
    assert result.witness["invalid_priority"] == ("slot",)
    assert result.witness["missing_priority"] == ()
    assert result.witness["conflicting_priority"] == ()


def test_lone_integer_zero_position_remains_exempt_and_known_slot_proposed_missing_priority_is_safe():
    lone = evaluate_limiter(
        [{"pair_slot": "slot", "qty": "1", "priority": 0}],
        limit=1,
    )
    proposed = evaluate_limiter(
        [{"pair_slot": "slot", "qty": "1", "priority": 1}],
        limit=1,
        proposed_openings=[{"pair_slot": "slot", "qty": "1"}],
    )
    assert lone.status == PASS and lone.exempt_pair_slots == ("slot",)
    assert proposed.status == PASS and proposed.allowed_openings == ("slot",)


def test_conflicting_explicit_priorities_for_one_slot_fail_closed():
    result = evaluate_limiter(
        [
            {"pair_slot": "slot", "qty": "1", "priority": 0},
            {"pair_slot": "slot", "qty": "1", "priority": 1},
        ],
        limit=1,
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()
    assert result.witness["unknown_priority"] == ("slot",)
    assert result.witness["conflicting_priority"] == ("slot",)
    assert result.witness["missing_priority"] == ()
    assert result.witness["invalid_priority"] == ()


@pytest.mark.parametrize(
    "source",
    [
        {"events": [{"pair_slot": "slot", "qty_delta": "0", "priority": None, "position_priority": 1}]},
        {"live_orders": [{"pair_slot": "slot", "qty": "1", "priority": None, "position_priority": 1}]},
        {"proposed_openings": [{"pair_slot": "slot", "qty": "1", "priority": None, "position_priority": 1}]},
        {"pending_cancels": [{"pair_slot": "slot", "cancel_requested": True, "priority": None, "position_priority": 1}]},
    ],
)
def test_priority_alias_none_and_conflicting_int_fails_closed_against_position_zero(source):
    result = evaluate_limiter(
        [{"pair_slot": "slot", "qty": "1", "priority": 0}],
        limit=1,
        **source,
    )
    assert result.status == UNKNOWN and result.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert result.exempt_pair_slots == () and result.allowed_openings == ()
    assert result.witness["unknown_priority"] == ("slot",)
    assert result.witness["conflicting_priority"] == ("slot",)
    assert result.witness["missing_priority"] == ()
    assert result.witness["invalid_priority"] == ()


@pytest.mark.parametrize(
    "source",
    [
        {"events": [{"pair_slot": "slot", "qty_delta": "0", "priority": 0, "position_priority": 0}]},
        {"live_orders": [{"pair_slot": "slot", "qty": "1", "priority": 0, "position_priority": 0}]},
        {"proposed_openings": [{"pair_slot": "slot", "qty": "1", "priority": 0, "position_priority": 0}]},
        {"pending_cancels": [{"pair_slot": "slot", "cancel_requested": True, "priority": 0, "position_priority": 0}]},
    ],
)
def test_equal_priority_aliases_are_accepted_for_known_position_slot(source):
    result = evaluate_limiter(
        [{"pair_slot": "slot", "qty": "1", "priority": 0}],
        limit=1,
        **source,
    )
    assert result.status == PASS and result.exempt_pair_slots == ("slot",)


def test_oversize_product_is_an_upward_conservative_margin_bound():
    qty = Decimal("9" * 10001)
    price = Decimal("1")
    with localcontext() as context:
        context.prec = 10010
        exact_notional = qty * price
    result = margin(
        [Position("X", "LONG", qty, price)],
        tiers=({"symbol": "X", "risk_limit_value": Decimal("1e10002"), "max_leverage": Decimal("1"), "maintenance_margin": Decimal("0")},),
        leverage={"X": "1"},
        leverage_steps={"X": "1"},
        fees=FeeSchedule(Decimal("0"), Decimal("0"), "backtest_manifest", "fixture"),
        margin_balance=Decimal("1e10002"),
        collateral_haircut="0",
        order_loss="0",
    )
    assert result.status == PASS
    assert result.evidence_class == CONSERVATIVE_BOUND
    assert result.total_im >= exact_notional
