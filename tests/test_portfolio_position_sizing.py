from copy import deepcopy
from decimal import Decimal

import pytest

from mrs3.portfolio.liquidity import ReferenceReader
from mrs3.portfolio.minute_capacity import CapacityWindow, MinuteCapacityResult
from mrs3.portfolio.position_sizing import enrich_finalist_rows


def _window(cap: str) -> CapacityWindow:
    return CapacityWindow(7, 10_080, 100, 9_980, Decimal("0.01"), Decimal("10000"), Decimal("1"), Decimal(cap), Decimal(cap))


def _capacity(symbol: str, cap: str = "600", *, status: str = "READY", step: str = "50") -> MinuteCapacityResult:
    return MinuteCapacityResult(
        status, symbol, None, None, 30, Decimal(step), _window(cap), _window(cap), Decimal(cap), "CALENDAR_7D", (), ("fixture",), f"capacity-{symbol}",
    )


def _reference(*, captured_at_ms: int = 1_000, max_qty: str = "100", tier_limit: str = "1000", tier_leverage: str = "50"):
    return ReferenceReader.from_records(
        instruments=[{
            "symbol": "BTCUSDT", "status": "Trading", "contract_type": "LinearPerpetual",
            "tick_size": "0.1", "qty_step": "0.1", "min_qty": "0.1", "max_qty": max_qty,
            "leverage_step": "0.1", "max_leverage": "100", "min_notional": "1",
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
