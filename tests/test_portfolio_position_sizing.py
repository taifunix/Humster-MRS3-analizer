from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext

import pytest

from mrs3.portfolio.liquidity import ReferenceReader
from mrs3.portfolio.minute_capacity import CapacityWindow, MinuteCapacityResult, MinuteLiquidityFeatures
from mrs3.portfolio.position_sizing import calculate_lot_capacity, enrich_finalist_rows, size_composition_vector


def _window(cap: str) -> CapacityWindow:
    return CapacityWindow(7, 10_080, 100, 9_980, Decimal("0.01"), Decimal("10000"), Decimal("1"), Decimal(cap), Decimal(cap))


def _capacity(symbol: str, cap: str = "600", *, status: str = "READY", step: str = "50") -> MinuteCapacityResult:
    return MinuteCapacityResult(
        status, symbol, None, None, 30, Decimal(step), _window(cap), _window(cap), Decimal(cap), "CALENDAR_7D", (), ("fixture",), f"capacity-{symbol}",
    )


def _lot_capacity(symbol: str = "BTCUSDT", *, v25: str = "1000", a15: str = "1") -> MinuteCapacityResult:
    window_dates = tuple(date(2026, 9, day) for day in range(1, 8))
    features = MinuteLiquidityFeatures(
        Decimal(v25), 672, Decimal(a15),
        tuple((f"{symbol}2026-09-{day:02d}_1m.csv", f"{day:064x}") for day in range(1, 8)),
        f"features-{symbol}",
        window_dates,
        6,
        "2026-09-08T06:00:00+00:00",
    )
    return MinuteCapacityResult(
        "READY", symbol, window_dates[0], window_dates[-1], None, Decimal("10"), None, None,
        Decimal("0"), "LOT_MODEL_V1", (), tuple(name for name, _digest in features.file_hashes),
        f"capacity-{symbol}", features,
    )


def _lot_settings(*, base: str = "1", bonus: str = "1.1"):
    return {
        "lot_model_base_coefficient": Decimal(base),
        "lot_model_max_shift_bonus": Decimal(bonus),
    }


def _reference(*, captured_at_ms: int = 1_000, max_qty: str = "100", tier_limit: str = "1000", tier_leverage: str = "50", min_qty: str = "0.1", min_notional: str = "1"):
    return ReferenceReader.from_records(
        instruments=[{
            "symbol": "BTCUSDT", "status": "Trading", "contract_type": "LinearPerpetual",
            "tick_size": "0.1", "qty_step": "0.1", "min_qty": min_qty, "max_qty": max_qty,
            "leverage_step": "0.1", "max_leverage": "100", "min_notional": min_notional,
        }],
        risk_tiers=[{"symbol": "BTCUSDT", "risk_limit_value": tier_limit, "max_leverage": tier_leverage}],
        captured_at_ms=captured_at_ms,
    )


def _row(*, status: str = "FINALIST", side: str = "LONG", symbol: str = "BTCUSDT"):
    return {
        "user_status": status,
        "symbol": symbol,
        "side": side,
        "strategy_id": 7,
        "result_id": 70,
        "strategy_orders": (
            {"order_id": 1, "lot_x": Decimal("0.5"), "shift_bp": 100},
            {"order_id": 2, "lot_x": Decimal("1"), "shift_bp": 200},
            {"order_id": 3, "lot_x": Decimal("1.5"), "shift_bp": 300},
        ),
    }


def test_full_position_cap_preserves_lot_proportions_and_audit_facts() -> None:
    result = enrich_finalist_rows(
        [_row()], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")}, now_ms=1_000, maximum_age_hours=2,
    )

    assert result.status == "PASS"
    enriched = result.rows[0]
    assert enriched["position_size_usdt"] == Decimal("600")
    assert enriched["maximum_closing_quantity"] == Decimal("6")
    assert enriched["maximum_closing_notional_usdt"] == Decimal("600")
    assert [item["target_notional_usdt"] for item in enriched["opening_allocations"]] == [Decimal("100"), Decimal("200"), Decimal("300")]
    assert sum(item["target_notional_usdt"] for item in enriched["opening_allocations"]) == enriched["position_size_usdt"]
    assert enriched["planned_leverage"] == Decimal("50.0")
    assert enriched["capacity_status"] == "READY"
    assert enriched["calendar_7d"] == enriched["weekday_5d"]
    assert enriched["sizing_digest"]
    assert enriched["reference_digest"] == _reference().content_digest
    assert enriched["capacity_digest"] == "capacity-BTCUSDT"
    with pytest.raises(TypeError):
        enriched["position_size_usdt"] = Decimal("1")


def test_exchange_cap_and_quantity_step_are_applied_after_usdt_rounding() -> None:
    result = enrich_finalist_rows(
        [_row()], {"BTCUSDT": _capacity("BTCUSDT", "999")}, _reference(max_qty="5.03"), {"BTCUSDT": Decimal("101")}, now_ms=1_000, maximum_age_hours=2,
    )

    enriched = result.rows[0]
    assert enriched["position_size_usdt"] == Decimal("494.9")
    assert enriched["maximum_closing_quantity"] == Decimal("4.9")
    assert enriched["maximum_closing_notional_usdt"] == Decimal("494.9")


def test_zero_rounded_cap_has_distinct_exclusion_reason() -> None:
    result = enrich_finalist_rows(
        [_row()], {"BTCUSDT": _capacity("BTCUSDT", "49", step="50")}, _reference(), {"BTCUSDT": Decimal("100")}, now_ms=1_000, maximum_age_hours=2,
    )

    assert result.status == "FAIL"
    assert result.exclusions[0].reason == "SIZE_ROUNDED_TO_ZERO"


def test_positive_rounded_cap_below_minimum_quantity_keeps_quantity_reason() -> None:
    result = enrich_finalist_rows(
        [_row()], {"BTCUSDT": _capacity("BTCUSDT", "50", step="50")}, _reference(), {"BTCUSDT": Decimal("1000")}, now_ms=1_000, maximum_age_hours=2,
    )

    assert result.status == "FAIL"
    assert result.exclusions[0].reason == "SIZE_BELOW_MINIMUM_QTY"


def test_reference_boundary_and_tier_leverage_are_current_and_exact() -> None:
    result = enrich_finalist_rows(
        [_row()], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(captured_at_ms=1_000, tier_limit="700", tier_leverage="25"), {"BTCUSDT": Decimal("100")}, now_ms=7_200_001, maximum_age_hours=2,
    )
    assert result.status == "PASS"
    assert result.rows[0]["planned_leverage"] == Decimal("25.0")

    stale = enrich_finalist_rows(
        [_row()], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(captured_at_ms=1_000), {"BTCUSDT": Decimal("100")}, now_ms=7_201_001, maximum_age_hours=2,
    )
    assert stale.status == "FAIL"
    assert stale.reason == "REFERENCE_STALE"


def test_bad_rows_are_excluded_without_poisoning_other_rows_and_inputs_stay_unchanged() -> None:
    good = _row()
    bad = _row(side="WRONG")
    rows = [bad, good]
    original = deepcopy(rows)
    result = enrich_finalist_rows(
        rows, {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")}, now_ms=1_000, maximum_age_hours=2,
    )

    assert len(result.rows) == 1
    assert result.rows[0]["side"] == "LONG"
    assert [(item.status, item.reason) for item in result.exclusions] == [("EXCLUDED", "INVALID_DIRECTION")]
    assert rows == original


def test_invalid_facts_and_deterministic_order_and_digest() -> None:
    missing = _row(symbol="ETHUSDT")
    first = enrich_finalist_rows(
        [_row(), missing], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")}, now_ms=1_000, maximum_age_hours=2,
    )
    second = enrich_finalist_rows(
        [missing, _row()], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")}, now_ms=1_000, maximum_age_hours=2,
    )
    assert first.rows == second.rows
    assert first.exclusions == second.exclusions
    assert first.rows[0]["sizing_digest"] == second.rows[0]["sizing_digest"]
    assert first.exclusions[0].reason == "MISSING_CAPACITY"


def test_sizing_digest_binds_full_order_geometry() -> None:
    first_row = _row()
    changed_row = deepcopy(first_row)
    changed_row["strategy_orders"][0]["shift_bp"] = 101
    args = ({"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")})

    first = enrich_finalist_rows([first_row], *args, now_ms=1_000, maximum_age_hours=2)
    changed = enrich_finalist_rows([changed_row], *args, now_ms=1_000, maximum_age_hours=2)

    assert first.rows[0]["sizing_digest"] != changed.rows[0]["sizing_digest"]


def test_weighted_vector_seam_rounds_each_target_without_changing_legacy_sizing() -> None:
    row = _row()
    row["x_usdt"] = Decimal("550")
    result = size_composition_vector(
        [row], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")},
    )

    assert result.status == "PASS"
    sized = result.members[0]
    assert sized["target_x_usdt"] == Decimal("550")
    assert sized["capacity_usdt"] == Decimal("600")
    assert sized["quantity"] == Decimal("5.5")
    assert sized["actual_size_usdt"] == Decimal("550")
    allocations = sized["opening_allocations"]
    with localcontext() as context:
        context.prec = 64
        assert sum(item["target_notional_usdt"] for item in allocations) == Decimal("550")
    assert allocations[2]["target_notional_usdt"] == Decimal("275")


def test_weighted_vector_seam_exposes_zero_target_as_removed_member() -> None:
    row = _row()
    row["x_usdt"] = Decimal("0")
    result = size_composition_vector(
        [row], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")},
    )

    assert result.status == "FAIL"
    assert result.reason == "NO_NONZERO_TARGET"
    assert result.exclusions[0].reason == "SIZE_ZERO"


def test_weighted_vector_seam_rejects_actual_size_below_minimum_notional() -> None:
    row = _row()
    row["x_usdt"] = Decimal("10")
    result = size_composition_vector(
        [row], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(min_notional="20"), {"BTCUSDT": Decimal("100")},
    )

    assert result.status == "FAIL"
    assert result.reason == "NO_NONZERO_TARGET"
    assert result.exclusions[0].reason == "SIZE_BELOW_MINIMUM_NOTIONAL"


def test_weighted_vector_seam_preserves_positive_below_min_qty_cause() -> None:
    row = _row()
    row["x_usdt"] = Decimal("10")
    result = size_composition_vector(
        [row], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(min_qty="0.2"), {"BTCUSDT": Decimal("100")},
    )

    assert result.status == "FAIL"
    assert result.reason == "NO_NONZERO_TARGET"
    assert result.exclusions[0].reason == "SIZE_BELOW_MINIMUM_QTY"


def test_weighted_vector_seam_rejects_aggregate_capacity_for_shared_symbol() -> None:
    first = _row()
    second = _row()
    second["strategy_id"] = 8
    second["result_id"] = 80
    first["x_usdt"] = Decimal("400")
    second["x_usdt"] = Decimal("300")
    result = size_composition_vector(
        [first, second], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")},
    )

    assert result.status == "FAIL"
    assert result.reason == "CAPACITY_EXCEEDED_BTCUSDT"


def test_weighted_vector_seam_applies_max_qty_to_effective_capacity() -> None:
    row = _row()
    row["x_usdt"] = Decimal("550")
    result = size_composition_vector(
        [row], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(max_qty="5.03"), {"BTCUSDT": Decimal("101")},
    )

    assert result.status == "FAIL"
    assert result.reason == "CAPACITY_EXCEEDED_BTCUSDT"


def test_weighted_vector_seam_rejects_ambiguous_symbol_target_mapping() -> None:
    first = _row()
    second = _row()
    second["strategy_id"] = 8
    second["result_id"] = 80
    result = size_composition_vector(
        [first, second], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")}, targets={"BTCUSDT": Decimal("300")},
    )

    assert result.status == "FAIL"
    assert result.reason == "AMBIGUOUS_TARGET_KEY"


def test_weighted_vector_target_mapping_rejects_collisions_and_missing_member_keys() -> None:
    first = _row()
    second = _row()
    second["strategy_id"] = 8
    second["result_id"] = 80
    collision = size_composition_vector(
        [first], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")}, targets={7: Decimal("100"), "7": Decimal("200")},
    )
    missing = size_composition_vector(
        [first, second], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")}, targets={7: Decimal("100")},
    )
    assert collision.reason == "AMBIGUOUS_TARGET_KEY"
    assert missing.reason == "MISSING_TARGET_8"


def test_weighted_vector_target_mapping_rejects_unknown_keys() -> None:
    row = _row()
    row["x_usdt"] = Decimal("100")
    result = size_composition_vector(
        [row], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")}, targets={7: Decimal("100"), "stale": Decimal("1")},
    )
    assert result.status == "FAIL"
    assert result.reason == "UNKNOWN_TARGET_KEY"


def test_weighted_vector_allocations_assign_exact_remainder_to_last_order() -> None:
    row = _row()
    row["x_usdt"] = Decimal("551")
    row["strategy_orders"] = tuple({"order_id": index, "lot_x": Decimal("1")} for index in range(3))
    result = size_composition_vector(
        [row], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")},
    )

    assert result.status == "PASS"
    allocations = result.members[0]["opening_allocations"]
    with localcontext() as context:
        context.prec = 64
        assert sum(item["target_notional_usdt"] for item in allocations) == result.members[0]["actual_size_usdt"]
    assert abs(allocations[-1]["target_notional_usdt"] - Decimal("550") / Decimal("3")) <= Decimal("0.0000000000000000000000001")


def test_weighted_vector_requires_order_geometry() -> None:
    missing = _row()
    missing.pop("strategy_orders")
    empty = _row()
    empty["strategy_orders"] = ()
    invalid = _row()
    invalid["strategy_orders"] = ({"order_id": 1, "lot_x": Decimal("1")}, "bad")
    for row, reason in ((missing, "MISSING_ORDER_GEOMETRY"), (empty, "MISSING_ORDER_GEOMETRY"), (invalid, "INVALID_ORDER_GEOMETRY")):
        row["x_usdt"] = Decimal("100")
        result = size_composition_vector(
            [row], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")},
        )
        assert result.status == "FAIL"
        assert result.reason == reason


def test_weighted_vector_rejects_multiple_order_fields_and_duplicate_order_ids() -> None:
    multiple = _row()
    multiple["x_usdt"] = Decimal("100")
    multiple["orders"] = ({"order_id": 2, "lot_x": Decimal("1")},)
    duplicate = _row()
    duplicate["x_usdt"] = Decimal("100")
    duplicate["strategy_orders"] = ({"order_id": 1, "lot_x": Decimal("1")}, {"order_id": 1, "lot_x": Decimal("2")})
    for row in (multiple, duplicate):
        result = size_composition_vector(
            [row], {"BTCUSDT": _capacity("BTCUSDT")}, _reference(), {"BTCUSDT": Decimal("100")},
        )
        assert result.status == "FAIL"
        assert result.reason == "INVALID_ORDER_GEOMETRY"


def test_lot_formula_matches_golden_and_keeps_configured_tie_order() -> None:
    equal_two = calculate_lot_capacity(
        Decimal("1000"), Decimal("1"),
        ({"order_id": 1, "lot_x": Decimal("1"), "shift_bp": 50},
         {"order_id": 2, "lot_x": Decimal("1"), "shift_bp": 300}),
        base_coefficient=Decimal("1"), max_shift_bonus=Decimal("1.1"),
    )
    assert equal_two.raw_cap_usdt == Decimal("1263.890045814244064972927947")
    assert equal_two.binding_order_id == 2
    permuted = calculate_lot_capacity(
        Decimal("1000"), Decimal("1"), tuple(reversed((
            {"order_id": 1, "lot_x": Decimal("1"), "shift_bp": 50},
            {"order_id": 2, "lot_x": Decimal("1"), "shift_bp": 300},
        ))),
        base_coefficient=Decimal("1"), max_shift_bonus=Decimal("1.1"),
    )
    assert permuted == equal_two

    tie = calculate_lot_capacity(
        Decimal("1000"), Decimal("1"),
        ({"order_id": 1, "lot_x": Decimal("1"), "shift_bp": 0},
         {"order_id": 2, "lot_x": Decimal("1"), "shift_bp": 550}),
        base_coefficient=Decimal("1"), max_shift_bonus=Decimal("1"),
    )
    assert tie.raw_cap_usdt == Decimal("2000")
    assert tie.binding_order_id == 1


def test_lot_enrichment_uses_formula_bound_not_legacy_capacity_and_binds_evidence() -> None:
    row = _row()
    row["strategy_orders"] = (
        {"order_id": 1, "lot_x": Decimal("1"), "shift_bp": 50},
        {"order_id": 2, "lot_x": Decimal("1"), "shift_bp": 300},
    )
    result = enrich_finalist_rows(
        [row], {"BTCUSDT": _lot_capacity()}, _reference(tier_limit="5000"), {"BTCUSDT": Decimal("100")},
        now_ms=1_000, maximum_age_hours=2, lot_model_settings=_lot_settings(),
    )

    assert result.status == "PASS"
    enriched = result.rows[0]
    assert enriched["position_size_usdt"] == Decimal("1260")
    assert enriched["capacity_usdt"] == Decimal("1260")
    assert enriched["raw_capacity_usdt"] == Decimal("1263.890045814244064972927947")
    assert enriched["binding_order_id"] == 2
    assert enriched["liquidity_v25_usdt"] == Decimal("1000")
    assert enriched["liquidity_a15"] == Decimal("1")
    assert enriched["lot_model_digest"]
    assert enriched["liquidity_window_dates"] == tuple(date(2026, 9, day) for day in range(1, 8))
    assert enriched["liquidity_publication_lag_hours"] == 6
    assert enriched["liquidity_anchor_created_at_utc"] == "2026-09-08T06:00:00+00:00"
    assert len(enriched["liquidity_file_hashes"]) == 7


@pytest.mark.parametrize("shift_bp", [1, 29, 551, 600, True])
def test_lot_enrichment_excludes_out_of_contract_shifts(shift_bp) -> None:
    row = _row()
    row["strategy_orders"] = ({"order_id": 1, "lot_x": Decimal("1"), "shift_bp": shift_bp},)
    result = enrich_finalist_rows(
        [row], {"BTCUSDT": _lot_capacity()}, _reference(), {"BTCUSDT": Decimal("100")},
        now_ms=1_000, maximum_age_hours=2, lot_model_settings=_lot_settings(),
    )
    assert result.status == "FAIL"
    assert result.reason == "LIQUIDITY_MODEL_NO_ELIGIBLE_CANDIDATE"
    assert result.exclusions[0].reason == "INVALID_ORDER_GEOMETRY"


def test_lot_enrichment_applies_quantity_step_after_usdt_floor_then_exchange_minimum() -> None:
    row = _row()
    row["strategy_orders"] = ({"order_id": 1, "lot_x": Decimal("1"), "shift_bp": 0},)
    result = enrich_finalist_rows(
        [row], {"BTCUSDT": _lot_capacity(v25="100")},
        _reference(max_qty="5.03", min_notional="600"), {"BTCUSDT": Decimal("101")},
        now_ms=1_000, maximum_age_hours=2,
        lot_model_settings={
            "lot_model_base_coefficient": Decimal("9"),
            "lot_model_max_shift_bonus": Decimal("1.1"),
        },
    )
    assert result.status == "FAIL"
    assert result.reason == "LIQUIDITY_MODEL_NO_ELIGIBLE_CANDIDATE"
    assert result.exclusions[0].reason == "SIZE_BELOW_MINIMUM_NOTIONAL"


@pytest.mark.parametrize("v25,a15", [("0", "1"), ("100", "0")])
def test_lot_enrichment_stably_excludes_degenerate_liquidity_features(v25: str, a15: str) -> None:
    row = _row()
    row["strategy_orders"] = ({"order_id": 1, "lot_x": Decimal("1"), "shift_bp": 0},)
    result = enrich_finalist_rows(
        [row], {"BTCUSDT": _lot_capacity(v25=v25, a15=a15)},
        _reference(), {"BTCUSDT": Decimal("100")}, now_ms=1_000,
        maximum_age_hours=2, lot_model_settings=_lot_settings(),
    )

    assert result.status == "FAIL"
    assert result.reason == "LIQUIDITY_MODEL_NO_ELIGIBLE_CANDIDATE"
    assert result.exclusions[0].reason == "LIQUIDITY_MODEL_WINDOW_UNAVAILABLE"


def test_lot_enrichment_caps_max_qty_then_floors_qty_without_reflooring_usdt() -> None:
    row = _row()
    row["strategy_orders"] = ({"order_id": 1, "lot_x": Decimal("1"), "shift_bp": 0},)
    result = enrich_finalist_rows(
        [row], {"BTCUSDT": _lot_capacity(v25="100")},
        _reference(max_qty="5.03"), {"BTCUSDT": Decimal("101")},
        now_ms=1_000, maximum_age_hours=2,
        lot_model_settings={
            "lot_model_base_coefficient": Decimal("9"),
            "lot_model_max_shift_bonus": Decimal("1.1"),
        },
    )

    assert result.status == "PASS"
    assert result.rows[0]["raw_capacity_usdt"] == Decimal("900")
    assert result.rows[0]["maximum_closing_quantity"] == Decimal("5.0")
    assert result.rows[0]["position_size_usdt"] == Decimal("505.0")


def test_weighted_vector_allows_independent_same_symbol_lot_caps() -> None:
    first = _row()
    second = _row()
    second["strategy_id"] = 8
    second["result_id"] = 80
    second["side"] = "SHORT"
    first["capacity_usdt"] = Decimal("400")
    second["capacity_usdt"] = Decimal("300")
    first["x_usdt"] = Decimal("400")
    second["x_usdt"] = Decimal("300")
    result = size_composition_vector(
        [first, second], {}, _reference(), {"BTCUSDT": Decimal("100")}, independent_capacities=True,
    )

    assert result.status == "PASS"
    assert [row["capacity_usdt"] for row in result.members] == [Decimal("400"), Decimal("300")]
    assert [row["actual_size_usdt"] for row in result.members] == [Decimal("400"), Decimal("300")]
