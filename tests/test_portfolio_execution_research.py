from datetime import datetime, timedelta, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, getcontext, localcontext
from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from mrs3.portfolio.execution_research import (
    AVAILABLE,
    ExecutionObservation,
    INCONSISTENT,
    NEEDS_RETEST,
    PARTIAL,
    REFINEMENT_KINDS,
    RESEARCH_ONLY,
    UNKNOWN,
    OrderEvent,
    _decimal_precision,
    calibrate_execution,
    build_refinement_evidence,
    create_retest_lineage,
    evaluate_correlated_stress,
    reduce_order_lifecycle,
    refinement_evidence,
    _digest,
    _decimal_difference,
    _decimal_ratio,
)


AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
LIFECYCLE_FIXTURE = Path(__file__).parent / "fixtures" / "portfolio" / "execution" / "lifecycle.json"


def event(event_type: str, seq: int, *, event_id: str | None = None, revision: int = 0, requested: str | None = "10", fill: str | None = None, cumulative: str | None = None, delta: str | None = None, remaining: str | None = None, at: datetime | None = None, regime: str | None = None, revision_link: str | None = None, reduce_only: bool | None = None, order_id: str = "order") -> OrderEvent:
    return OrderEvent(
        event_id=event_id or f"e-{seq}", execution_campaign_id="camp", trading_run_id="run", account_alias="acct",
        order_id=order_id, order_revision=revision, symbol="BTCUSDT", side="LONG", timeframe="1h", regime=regime,
        event_type=event_type, source_sequence=seq, timestamp_utc=at or (AT + timedelta(seconds=seq)),
        requested_qty=requested, fill_qty=fill, cumulative_filled_qty=cumulative, fill_delta_qty=delta, remaining_qty=remaining, revision_link=revision_link, reduce_only=reduce_only,
    )


def test_event_schema_identity_and_unknown_type_are_strict():
    placed = event("PLACED", 0, remaining="10")
    assert placed.identity == ("camp", "acct", "order", 0, "e-0")
    assert placed.digest == event("PLACED", 0, remaining="10").digest
    with pytest.raises(ValueError, match="unknown event_type"):
        event("MAYBE", 0)
    with pytest.raises(ValueError, match="unsupported event_schema_version"):
        event("PLACED", 0).from_mapping({**event("PLACED", 0).as_dict(), "event_schema_version": 2})
    with pytest.raises(ValueError, match="timezone"):
        event("PLACED", 0, at=datetime(2026, 1, 1))


def test_lifecycle_is_deterministic_and_preserves_duplicate_without_double_fill():
    placed = event("PLACED", 0, remaining="10")
    partial = event("PARTIAL_FILL", 1, fill="4", remaining="6")
    full = event("FULL_FILL", 2, fill="10", remaining="0")
    result = reduce_order_lifecycle([placed, partial, partial, full])
    assert result.status == AVAILABLE
    assert result.duplicate_event_ids == ("e-1",)
    assert result.filled_qty == Decimal("10")
    assert result.fill_ratio == Decimal("1")
    assert result.partial_count == 1
    assert result.time_to_first_fill == Decimal("1.000000")
    assert result.time_to_full_fill == Decimal("2.000000")


def test_lifecycle_rejects_conflict_order_and_bad_remaining():
    placed = event("PLACED", 0, remaining="10")
    conflict = event("PLACED", 0, event_id="e-0", remaining="9")
    assert reduce_order_lifecycle([placed, conflict]).status == INCONSISTENT
    assert reduce_order_lifecycle([event("PARTIAL_FILL", 1)]).reason == "INVALID_INITIAL_STATE"
    assert reduce_order_lifecycle([placed, event("PARTIAL_FILL", 1, fill="4", remaining="5")]).status == INCONSISTENT
    with pytest.raises(ValueError, match="fill_qty and cumulative_filled_qty must match"):
        event("PARTIAL_FILL", 1, fill="4", cumulative="5", remaining="5")
    assert reduce_order_lifecycle([placed, event("PARTIAL_FILL", 1, fill="4", remaining="6"), event("PARTIAL_FILL", 2, fill="3", remaining="7")]).reason == "NON_MONOTONE_CUMULATIVE_FILL"
    assert reduce_order_lifecycle([placed, event("FULL_FILL", 1, fill="11", remaining="0")]).reason == "FILL_EXCEEDS_REQUESTED"
    assert reduce_order_lifecycle([placed, event("PARTIAL_FILL", 1, fill="4", delta="3", remaining="6")]).reason == "FILL_DELTA_MISMATCH"
    mixed_order = event("ACKNOWLEDGED", 1, order_id="other")
    assert reduce_order_lifecycle([placed, mixed_order]).reason == "INCONSISTENT_ORDER_ID"


def test_lifecycle_missing_fill_timestamp_stays_unknown_and_known_reversal_is_rejected():
    placed = event("PLACED", 0, at=AT, remaining="10")
    partial_missing = OrderEvent.from_mapping({**event("PARTIAL_FILL", 1, fill="4", remaining="6").as_dict(), "timestamp_utc": None})
    full_known = event("FULL_FILL", 2, fill="10", remaining="0", at=AT + timedelta(seconds=2))
    missing_first = reduce_order_lifecycle([placed, partial_missing, full_known])
    assert missing_first.time_to_first_fill is None

    placed_late = event("PLACED", 0, at=AT + timedelta(seconds=10), remaining="10")
    acknowledged_missing = OrderEvent.from_mapping({**event("ACKNOWLEDGED", 1, at=AT + timedelta(seconds=1), remaining="10").as_dict(), "timestamp_utc": None})
    partial_early = event("PARTIAL_FILL", 2, at=AT + timedelta(seconds=5), fill="4", remaining="6")
    assert reduce_order_lifecycle([placed_late, acknowledged_missing, partial_early]).reason == "INVALID_EVENT_ORDER"


def test_missing_requested_is_unknown_and_cancel_keeps_reserve_until_confirmed():
    missing = reduce_order_lifecycle([event("PLACED", 0, requested=None), event("PARTIAL_FILL", 1, requested=None, fill="2")])
    assert missing.status == PARTIAL
    assert missing.fill_ratio is None
    assert missing.reason == "REQUESTED_QUANTITY_MISSING"
    pending = reduce_order_lifecycle([event("PLACED", 0, remaining="10"), event("CANCEL_REQUESTED", 1, remaining="10")])
    assert pending.reserve_active is True
    cancelled = reduce_order_lifecycle(pending.events + (event("CANCEL_CONFIRMED", 2, remaining="6"),))
    assert cancelled.reserve_active is False
    assert cancelled.remaining_at_cancel == Decimal("6")


def test_replace_starts_revision_and_keeps_old_remaining():
    result = reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"), event("PARTIAL_FILL", 1, fill="4", remaining="6"),
        event("REPLACE_REQUESTED", 2, remaining="6"),
        event("REPLACE_CONFIRMED", 3, revision=1, requested="8", remaining="8", revision_link="0"),
    ])
    assert result.revision == 1
    assert result.replacement_links == ((0, 1),)
    assert result.remaining_at_replace == Decimal("6")
    assert result.requested_qty == Decimal("8")
    assert result.reserve_active is True
    assert reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"), event("REPLACE_REQUESTED", 1, remaining="10"),
        event("REPLACE_CONFIRMED", 3, revision=2, requested="8", remaining="8", revision_link="0"),
    ]).reason == "INVALID_REPLACEMENT_REVISION"
    assert reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"), event("REPLACE_REQUESTED", 1, remaining="10"),
        event("REPLACE_CONFIRMED", 2, revision=1, requested="8", remaining="8"),
    ]).reason == "INVALID_REPLACEMENT_LINK"


def test_replacement_revision_accepts_continuation_events_and_keeps_terminal_terminal():
    continued = reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"), event("REPLACE_REQUESTED", 1, remaining="10"),
        event("REPLACE_CONFIRMED", 2, revision=1, requested="8", remaining="8", revision_link="0"),
        event("ACKNOWLEDGED", 3, revision=1, requested="8", remaining="8"),
        event("PARTIAL_FILL", 4, revision=1, requested="8", fill="3", remaining="5"),
        event("CANCEL_REQUESTED", 5, revision=1, requested="8", remaining="5"),
        event("CANCEL_CONFIRMED", 6, revision=1, requested="8", remaining="5"),
    ])
    assert continued.status == AVAILABLE
    assert continued.requested_by_revision == {0: Decimal("10"), 1: Decimal("8")}

    replaced = reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"), event("REPLACE_REQUESTED", 1, remaining="10"),
        event("REPLACE_CONFIRMED", 2, revision=1, requested="8", remaining="8", revision_link="0"),
        event("REPLACE_REQUESTED", 3, revision=1, requested="8", remaining="8"),
        event("REPLACE_CONFIRMED", 4, revision=2, requested="6", remaining="6", revision_link="1"),
        event("REJECTED", 5, revision=2, requested="6"),
    ])
    assert replaced.status == AVAILABLE
    assert replaced.revision == 2
    assert reduce_order_lifecycle(replaced.events + (event("ACKNOWLEDGED", 6, revision=2, requested="6"),)).status == INCONSISTENT


def test_cancel_confirmation_unknown_fill_is_never_available_and_uses_derived_remaining():
    known = reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"), event("PARTIAL_FILL", 1, fill="4", remaining="6"),
        event("CANCEL_REQUESTED", 2, remaining="6"), event("CANCEL_CONFIRMED", 3),
    ])
    assert known.status == AVAILABLE
    assert known.remaining_at_cancel == Decimal("6")

    unknown = reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"), event("PARTIAL_FILL", 1, fill="4", remaining="6"),
        event("CANCEL_REQUESTED", 2, remaining="6"), event("CANCEL_CONFIRMED", 3, remaining="5"),
    ])
    assert unknown.status == UNKNOWN
    assert unknown.reason == "FILL_QUANTITY_UNKNOWN"
    assert unknown.fill_ratio is None


def test_reduce_only_fill_is_delta_from_cumulative_fill_baseline():
    result = reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"),
        event("PARTIAL_FILL", 1, fill="4", remaining="6"),
        event("REDUCE_ONLY_FILL", 2, fill="6", remaining="4"),
    ])
    assert result.filled_qty == Decimal("6")
    assert result.reduce_only_fill_qty == Decimal("2")
    assert result.reduce_only_fill_count == 1

    unknown = reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"),
        event("PARTIAL_FILL", 1, fill="4", remaining="6"),
        event("REDUCE_ONLY_FILL", 2, remaining="4"),
    ])
    assert unknown.reduce_only_fill_qty is None


def test_replace_mismatch_preserves_observed_remaining_through_chained_replace():
    result = reduce_order_lifecycle([
        event("PLACED", 0, remaining="10"),
        event("PARTIAL_FILL", 1, fill="4", remaining="6"),
        event("REPLACE_REQUESTED", 2, remaining="6"),
        event("REPLACE_CONFIRMED", 3, revision=1, requested="8", remaining="5", revision_link="0"),
        event("REPLACE_REQUESTED", 4, revision=1, requested="8"),
        event("REPLACE_CONFIRMED", 5, revision=2, requested="8", revision_link="1"),
    ])
    assert result.status == PARTIAL
    assert result.remaining_at_replace == Decimal("5")


def test_calibration_builds_base_before_explicit_regime_and_is_descriptive():
    observations = [
        {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "regime": "volatile", "requested_qty": "10", "fill_ratio": "0.8", "source_digest": "a"},
        {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "regime": "quiet", "requested_qty": "20", "fill_ratio": "0.4", "source_digest": "b"},
        {"symbol": "ETHUSDT", "side": "SHORT", "timeframe": "4h", "requested_qty": "5", "fill_ratio": None},
    ]
    result = calibrate_execution(observations, policy={"version": "p1", "confidence": "DESCRIPTIVE"}, proxy={"BTCUSDT|LONG|1h": "0.5"})
    assert result.disposition == RESEARCH_ONLY
    assert len(result.base_curves) == 2
    assert len(result.regime_curves) == 2
    btc = result.base_curves[0]
    assert btc.sample_count == 2
    assert btc.coverage == Decimal("1")
    assert btc.proxy_vs_empirical["difference"] == Decimal("0.1")
    assert result.base_curves[1].incomplete_count == 1
    assert result.base_curves[0].degradation_points[0]["first_fill_seconds"] is None
    assert result.base_curves[1].degradation_points[0]["remaining_at_cancel"] is None
    replay = calibrate_execution(tuple(reversed(observations)), policy={"version": "p1", "confidence": "DESCRIPTIVE"}, proxy={"BTCUSDT|LONG|1h": "0.5"})
    assert replay.source_digest == result.source_digest
    assert replay.content_digest == result.content_digest
    assert calibrate_execution(observations, policy={"version": "p2", "confidence": "DESCRIPTIVE"}, proxy={"BTCUSDT|LONG|1h": "0.5"}).content_digest != result.content_digest
    assert calibrate_execution(observations, policy={"version": "p1", "confidence": "DESCRIPTIVE"}, proxy={"BTCUSDT|LONG|1h": "0.6"}).content_digest != result.content_digest
    assert calibrate_execution(observations, policy={"version": "p1", "confidence": "DESCRIPTIVE"}, proxy={"BTCUSDT|LONG|1h": "0.5"}, calibration_version="execution_calibration_v2").content_digest != result.content_digest


def test_calibration_is_permutation_invariant_and_uses_fixed_decimal_precision():
    observations = [
        {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "10", "fill_ratio": "0.333333333333333333333333333333", "first_fill_seconds": "1", "full_fill_seconds": "2"},
        {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "10", "fill_ratio": "0.142857142857142857142857142857", "first_fill_seconds": "2", "full_fill_seconds": "3"},
        {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "10", "fill_ratio": "0.666666666666666666666666666667", "first_fill_seconds": "3", "full_fill_seconds": "4"},
    ]
    original = getcontext().prec
    try:
        getcontext().prec = 6
        low = calibrate_execution(observations, policy={"version": "p1"})
        getcontext().prec = 100
        high = calibrate_execution(tuple(reversed(observations)), policy={"version": "p1"})
    finally:
        getcontext().prec = original
    assert low.content_digest == high.content_digest
    assert low.base_curves[0].degradation_points == high.base_curves[0].degradation_points


def test_decimal_evidence_is_context_independent_across_wide_exponent_span():
    observations = [
        {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "1E-60", "fill_ratio": "0.333333333333333333333333333333", "first_fill_seconds": "1", "full_fill_seconds": "2"},
        {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "1E+60", "fill_ratio": "0.666666666666666666666666666667", "first_fill_seconds": "2", "full_fill_seconds": "3"},
    ]
    wide_values = tuple(Decimal(value) for value in ("1E-60", "2E+60", "3E-90", "4E+90"))
    assert _decimal_precision(wide_values) == _decimal_precision(tuple(reversed(wide_values)))
    lifecycle_events = [event("PLACED", 0, remaining="10"), event("PARTIAL_FILL", 1, fill="4", remaining="6"), event("FULL_FILL", 2, fill="10", remaining="0")]

    def snapshot(precision: int, rounding: str):
        with localcontext() as context:
            context.prec = precision
            context.rounding = rounding
            lifecycle = reduce_order_lifecycle(lifecycle_events)
            stress = evaluate_correlated_stress(
                {"capacity": "1E-60", "executable_digest": "prior"},
                mark="1E+60",
                equity="1E+60",
                mark_shock="-0.1",
                equity_shock="-0.2",
                liquidity_capacity={"large": "2E+60", "small": "2E-60"},
                leverage_capacity={"large": "3E+60", "small": "3E-60"},
                deepest_fill="1E-60",
                liquidity_factor="0.5",
                leverage_factor="0.8",
                margin_result="PASS",
                liquidity_result="PASS",
            )
            calibration = calibrate_execution(observations, policy={"version": "p1"})
            return (
                lifecycle.time_to_first_fill,
                lifecycle.time_to_full_fill,
                stress.capacity,
                stress.new_executable_digest,
                stress.input_digest,
                calibration.content_digest,
            )

    snapshots = [snapshot(precision, rounding) for precision, rounding in ((6, ROUND_DOWN), (28, ROUND_HALF_UP), (200, ROUND_FLOOR))]
    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_nonterminating_decimal_arithmetic_ignores_hostile_context_and_replays_identically():
    observations = [
        {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "1E-500", "fill_ratio": "0.333333333333333333333333333333", "first_fill_seconds": "1", "full_fill_seconds": "2"},
        {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "1E+500", "fill_ratio": "0.666666666666666666666666666667", "first_fill_seconds": "2", "full_fill_seconds": "3"},
    ]
    lifecycle_events = [event("PLACED", 0, requested="3", remaining="3"), event("PARTIAL_FILL", 1, requested="3", fill="1", remaining="2"), event("CANCEL_REQUESTED", 2, requested="3", remaining="2"), event("CANCEL_CONFIRMED", 3, requested="3", remaining="2")]

    def snapshot(precision: int, rounding: str, emin: int, emax: int):
        with localcontext() as context:
            context.prec = precision
            context.rounding = rounding
            context.Emin = emin
            context.Emax = emax
            for signal in context.traps:
                context.traps[signal] = True
            lifecycle = reduce_order_lifecycle(lifecycle_events)
            calibration = calibrate_execution(observations, policy={"version": "nonterminating"})
            stress = evaluate_correlated_stress(
                {"capacity": "1E-500", "executable_digest": "prior"}, mark="1E+500", equity="1E+500",
                mark_shock="-0.1", equity_shock="-0.2", liquidity_capacity={"large": "2E+500", "small": "2E-500"},
                leverage_capacity={"large": "3E+500", "small": "3E-500"}, deepest_fill="1E-500",
                liquidity_factor="0.5", leverage_factor="0.8", margin_result="PASS", liquidity_result="PASS",
            )
            return (lifecycle.fill_ratio, lifecycle.source_digest, calibration.content_digest, stress.input_digest, stress.new_executable_digest)

    first = snapshot(7, ROUND_DOWN, -2, 2)
    second = snapshot(200, ROUND_FLOOR, -999999, 999999)
    assert first == second and first[0] is not None and len(format(first[0], "f")) > 20


@pytest.mark.parametrize(
    ("operation", "args", "error"),
    (
        (_decimal_ratio, (Decimal("1"), Decimal("0")), DivisionByZero),
        (_decimal_ratio, (Decimal("0"), Decimal("0")), InvalidOperation),
        (_decimal_difference, (Decimal("Infinity"), Decimal("Infinity")), InvalidOperation),
    ),
)
def test_decimal_arithmetic_rejects_zero_division_invalid_and_nonfinite_results(operation, args, error):
    with localcontext() as context:
        context.prec = 6
        context.traps[DivisionByZero] = False
        context.traps[InvalidOperation] = False
        context.traps[Overflow] = False
        with pytest.raises(error):
            operation(*args)


def test_refinement_fails_closed_for_missing_stale_conflict_and_nonpositive():
    valid = refinement_evidence("BORROW", "2", unit="USDT", currency="USDT", provenance="fixture", observed_at=AT, expires_at=AT + timedelta(hours=1), now=AT)
    typed = [refinement_evidence(kind, "1.25", unit="USDT", currency="USDT", provenance="fixture", observed_at=AT, expires_at=AT + timedelta(hours=1), now=AT) for kind in sorted(REFINEMENT_KINDS)]
    assert all(item.status == AVAILABLE and item.value == Decimal("1.25") and item.usable is False for item in typed)
    assert valid.status == AVAILABLE
    for item in (
        refinement_evidence("BORROW", "2", unit="USDT", currency="USDT", provenance="fixture", observed_at=AT, expires_at=AT + timedelta(hours=1), now=AT + timedelta(hours=2)),
        refinement_evidence("BORROW", "0", unit="USDT", currency="USDT", provenance="fixture", observed_at=AT, expires_at=AT + timedelta(hours=1)),
        refinement_evidence("BORROW", "2", unit="USDT", currency="USDT", provenance="fixture", observed_at=AT, expires_at=AT + timedelta(hours=1), conflicting=True),
        refinement_evidence("BORROW", None, unit="USDT", currency="USDT", provenance="fixture", observed_at=AT, expires_at=AT + timedelta(hours=1)),
    ):
        assert item.status == UNKNOWN and item.value is None
    facts = build_refinement_evidence({"BORROW": {"value": "2", "unit": "USDT", "currency": "USDT", "provenance": "fixture", "observed_at": AT, "expires_at": AT + timedelta(hours=1)}}, now=AT)
    assert facts["BORROW"].value == Decimal("2")


def test_refinement_requires_explicit_as_of_time():
    for expires_at in (AT + timedelta(hours=1), AT - timedelta(hours=1)):
        item = refinement_evidence("BORROW", "2", unit="USDT", currency="USDT", provenance="fixture", observed_at=AT, expires_at=expires_at)
        assert item.status == UNKNOWN
        assert item.value is None
        assert item.reason == "MISSING_ASOF"
    future = refinement_evidence("BORROW", "2", unit="USDT", currency="USDT", provenance="fixture", observed_at=AT + timedelta(hours=1), expires_at=AT + timedelta(hours=2), now=AT)
    assert future.status == UNKNOWN and future.reason == "FUTURE_FACT"
    with pytest.raises(TypeError):
        refinement_evidence("BORROW", "2", unit=1, currency="USDT", provenance="fixture", observed_at=AT, expires_at=AT + timedelta(hours=1), now=AT)
    with pytest.raises(TypeError):
        refinement_evidence("BORROW", 1.25, unit="USDT", currency="USDT", provenance="fixture", observed_at=AT, expires_at=AT + timedelta(hours=1), now=AT)


def test_correlated_stress_is_one_immutable_joint_scenario_and_marks_retest():
    prior = {"capacity": Decimal("100"), "executable_digest": "prior"}
    result = evaluate_correlated_stress(
        prior, mark="100", equity="1000", mark_shock="-0.1", equity_shock="-0.2",
        liquidity_capacity="80", leverage_capacity="120", deepest_fill="70", liquidity_factor="0.5", leverage_factor="0.8",
        margin_result="PASS", liquidity_result="PASS",
    )
    assert result.status == AVAILABLE
    assert result.capacity == Decimal("40")
    assert result.disposition == NEEDS_RETEST
    assert result.joint_retest_required is True
    assert prior == {"capacity": Decimal("100"), "executable_digest": "prior"}
    unknown = evaluate_correlated_stress({}, mark="100", equity="1000", deepest_fill=None)
    assert unknown.status == UNKNOWN
    assert evaluate_correlated_stress({}, mark="100", equity="1000", deepest_fill="1", margin_result="PASS").reason == "STRESS_GATE_MISSING"
    missing = evaluate_correlated_stress(prior, mark="100", equity="1000", deepest_fill="1")
    assert missing.status == UNKNOWN and missing.reason == "STRESS_GATE_MISSING"
    gate_unknown = evaluate_correlated_stress(prior, mark="100", equity="1000", deepest_fill="1", margin_result="UNKNOWN", liquidity_result="PASS")
    assert gate_unknown.status == UNKNOWN and gate_unknown.reason == "STRESS_GATE_UNKNOWN"
    gate_fail = evaluate_correlated_stress(prior, mark="100", equity="1000", deepest_fill="1", margin_result="FAIL", liquidity_result="PASS")
    assert gate_fail.status == "FAIL" and gate_fail.reason == "STRESS_GATE_FAILED"
    for blocked in (gate_unknown, gate_fail):
        assert blocked.capacity is None
        assert blocked.disposition == RESEARCH_ONLY
        assert blocked.joint_retest_required is False
        assert blocked.new_executable_digest is None
    with pytest.raises(ValueError, match="stress factors"):
        evaluate_correlated_stress({}, mark="100", equity="1000", deepest_fill="1", liquidity_factor="1.1", margin_result="PASS", liquidity_result="PASS")


def _fixture_events(rows: list[dict[str, object]]) -> list[OrderEvent]:
    common = {
        "execution_campaign_id": "fixture-campaign",
        "trading_run_id": "fixture-run",
        "account_alias": "fixture-account",
        "order_id": "fixture-order",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "provenance": "phase2a-fixture",
    }
    return [OrderEvent.from_mapping({**common, **row}) for row in rows]


def test_lifecycle_fixture_covers_all_phase2a_vectors_and_unknown_facts():
    payload = json.loads(LIFECYCLE_FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    cases = payload["cases"]
    event_types = {row["event_type"] for rows in cases.values() for row in rows}
    assert {"PLACED", "ACKNOWLEDGED", "PARTIAL_FILL", "FULL_FILL", "CANCEL_REQUESTED", "CANCEL_CONFIRMED", "REPLACE_REQUESTED", "REPLACE_CONFIRMED", "REDUCE_ONLY_FILL", "REJECTED"} <= event_types

    filled = reduce_order_lifecycle(_fixture_events(cases["filled"]))
    assert filled.status == AVAILABLE
    assert filled.partial_count == 1
    assert filled.time_to_first_fill == Decimal("2.000000")
    assert filled.time_to_full_fill == Decimal("3.000000")
    cycle = reduce_order_lifecycle(_fixture_events(cases["cancel_replace_reduce"]))
    assert cycle.status == AVAILABLE
    assert cycle.revision == 1
    assert cycle.remaining_at_replace == Decimal("6")
    assert cycle.remaining_at_cancel == Decimal("2")
    assert cycle.reserve_state == "RELEASED"
    assert cycle.reduce_only_fill_qty == Decimal("2")
    assert reduce_order_lifecycle(_fixture_events(cases["rejected"])).status == AVAILABLE
    missing = reduce_order_lifecycle(_fixture_events(cases["missing_facts"]))
    assert missing.status == PARTIAL and missing.fill_ratio is None and missing.time_to_first_fill is None
    missing_confirmation = reduce_order_lifecycle(_fixture_events(cases["missing_cancel_confirmation"]))
    assert missing_confirmation.status == UNKNOWN and missing_confirmation.time_to_full_fill is None
    duplicate = reduce_order_lifecycle(_fixture_events(cases["duplicate"]))
    assert duplicate.duplicate_event_ids == ("duplicate-placed",)
    assert reduce_order_lifecycle(_fixture_events(cases["conflict"])).status == INCONSISTENT
    with pytest.raises(ValueError, match="fill_qty and cumulative_filled_qty must match"):
        _fixture_events(cases["fill_mismatch"])
    assert reduce_order_lifecycle(_fixture_events(cases["fill_decrease"])).reason == "NON_MONOTONE_CUMULATIVE_FILL"
    assert reduce_order_lifecycle(_fixture_events(cases["fill_overfill"])).reason == "FILL_EXCEEDS_REQUESTED"


def test_equal_source_sequence_with_distinct_events_is_ambiguous_but_exact_duplicate_is_idempotent():
    placed = event("PLACED", 0)
    acknowledged = event("ACKNOWLEDGED", 0, event_id="ack")
    result = reduce_order_lifecycle([placed, acknowledged])
    assert result.status == INCONSISTENT and result.reason == "AMBIGUOUS_SEQUENCE"
    duplicate = reduce_order_lifecycle([placed, event("PLACED", 0, event_id=placed.event_id)])
    assert duplicate.status != INCONSISTENT and duplicate.duplicate_event_ids == (placed.event_id,)


def test_execution_evidence_rejects_nested_float_and_replays_bytes_identically():
    with pytest.raises(TypeError, match="Python float"):
        OrderEvent(**{
            "event_id": "nested-float", "execution_campaign_id": "camp", "trading_run_id": "run", "account_alias": "acct",
            "order_id": "order", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "event_type": "PLACED",
            "source_sequence": 0, "facts": {"nested": [{"value": 1.25}]},
        })
    observations = [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "10", "fill_ratio": "1", "first_fill_seconds": "1", "full_fill_seconds": "2"}]
    first = calibrate_execution(observations, policy={"version": "v1", "minimum_sample_count": 2})
    second = calibrate_execution(tuple(reversed(observations)), policy={"minimum_sample_count": 2, "version": "v1"})
    assert first.content_digest == second.content_digest and first.source_digest == second.source_digest
    assert first.base_curves[0].stratum == "BTCUSDT|LONG|1h"
    assert first.base_curves[0].regime_version is None and first.base_curves[0].stable_bound is False
    with pytest.raises(TypeError, match="Python float"):
        calibrate_execution(observations, policy={"version": "v1", "nested": {"bad": 1.25}})


def test_evidence_mapping_keys_sets_and_mutable_values_fail_closed():
    with pytest.raises(TypeError, match="mapping keys"):
        _digest({"1": "string-key", 1: "integer-key"}, "key-collision")
    with pytest.raises(TypeError, match="mapping keys"):
        OrderEvent(**{
            "event_id": "bad-key", "execution_campaign_id": "camp", "trading_run_id": "run", "account_alias": "acct",
            "order_id": "order", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "event_type": "PLACED",
            "source_sequence": 0, "facts": {"nested": {1: "integer-key", "1": "string-key"}},
        })
    with pytest.raises(TypeError, match="sets"):
        OrderEvent(**{
            "event_id": "set-value", "execution_campaign_id": "camp", "trading_run_id": "run", "account_alias": "acct",
            "order_id": "order", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "event_type": "PLACED",
            "source_sequence": 0, "facts": {"values": {Decimal("1"), 1, True}},
        })
    with pytest.raises(TypeError, match="unsupported"):
        OrderEvent(**{
            "event_id": "mutable", "execution_campaign_id": "camp", "trading_run_id": "run", "account_alias": "acct",
            "order_id": "order", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "event_type": "PLACED",
            "source_sequence": 0, "facts": {"bytes": bytearray(b"x")},
        })
    with pytest.raises(TypeError, match="unsupported"):
        OrderEvent(**{
            "event_id": "custom", "execution_campaign_id": "camp", "trading_run_id": "run", "account_alias": "acct",
            "order_id": "order", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "event_type": "PLACED",
            "source_sequence": 0, "facts": {"object": object()},
        })


def test_decimal_digest_preserves_trailing_zero_representation():
    assert _digest({"value": Decimal("1.0")}, "decimal_scale_test") != _digest({"value": Decimal("1.00")}, "decimal_scale_test")
    assert _digest({"value": Decimal("1E+3")}, "decimal_scale_test") != _digest({"value": Decimal("1000")}, "decimal_scale_test")


def test_versioned_regime_is_recorded_and_small_or_incomplete_sample_is_not_stable():
    observation = {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "regime": "volatile", "regime_version": "regime-v2", "requested_qty": "10", "fill_ratio": "1", "first_fill_seconds": "1", "full_fill_seconds": "2"}
    result = calibrate_execution([observation, {**observation, "requested_qty": "20"}], policy={"version": "v1", "minimum_sample_count": 2, "applicability": "FIXTURE", "confidence": "LOW"})
    curve = result.regime_curves[0]
    assert curve.stratum.endswith("|volatile|regime-v2")
    assert curve.regime_version == "regime-v2" and curve.stable_bound is True
    incomplete = calibrate_execution([{**observation, "fill_ratio": None}], policy={"version": "v1", "minimum_sample_count": 1}).regime_curves[0]
    assert incomplete.stable_bound is False and incomplete.applicability == "OPEN_POLICY"


def test_research_outputs_are_never_usable_and_each_execution_change_creates_retest_lineage():
    valid = refinement_evidence("BORROW", "2", unit="USDT", currency="USDT", provenance="fixture", observed_at=AT, expires_at=AT + timedelta(hours=1), now=AT)
    calibration = calibrate_execution([{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "10", "fill_ratio": "1", "first_fill_seconds": "1", "full_fill_seconds": "2"}], policy={"version": "v1"})
    stress = evaluate_correlated_stress({"capacity": "100", "executable_digest": "prior"}, mark="100", equity="1000", deepest_fill="80", liquidity_capacity="90", leverage_capacity="100", margin_result="PASS", liquidity_result="PASS")
    assert valid.usable is False and valid.approved is False
    assert calibration.usable is False and calibration.approved is False
    assert stress.usable is False and stress.approved is False and stress.ready is False and stress.admission_eligible is False
    prior = {"status": "PASS", "evaluation_digest": "prior-digest", "nested": {"row": "unchanged"}}
    for name in ("sizing", "capacity", "reserve", "margin_envelope"):
        lineage = create_retest_lineage(prior, **{name: "changed"})
        assert lineage.status == NEEDS_RETEST and lineage.disposition == NEEDS_RETEST
        assert lineage.parent_digest == "prior-digest" and lineage.child_record[name] == "changed"
        assert lineage.child_record["approved"] is False
        with pytest.raises(TypeError):
            lineage.child_record["nested"] = {}
    assert prior == {"status": "PASS", "evaluation_digest": "prior-digest", "nested": {"row": "unchanged"}}


def test_unknown_or_failed_stress_still_allows_immutable_envelope_retest_lineage():
    prior = {"capacity": Decimal("100"), "evaluation_digest": "prior"}
    for gate in ("UNKNOWN", "FAIL"):
        stress = evaluate_correlated_stress(prior, mark="100", equity="1000", deepest_fill="80", liquidity_capacity="90", leverage_capacity="100", margin_result=gate, liquidity_result="PASS")
        assert stress.capacity is None and stress.usable is False and stress.approved is False
        lineage = create_retest_lineage(prior, margin_envelope={"stress_status": stress.status})
        assert lineage.disposition == NEEDS_RETEST and lineage.parent_record["capacity"] == Decimal("100")


def test_stress_digest_covers_normalized_factors_gate_evidence_and_evaluator_outputs():
    prior = {"capacity": "100", "executable_digest": "prior"}
    base = evaluate_correlated_stress(
        prior, mark="100", equity="1000", deepest_fill="80", liquidity_capacity="90", leverage_capacity="100",
        liquidity_factor="0.5", leverage_factor="0.8", margin_result={"status": "PASS", "evidence": {"source": "m1"}},
        liquidity_result={"status": "PASS", "evidence": {"source": "l1"}},
    )
    changed_factor = evaluate_correlated_stress(
        prior, mark="100", equity="1000", deepest_fill="80", liquidity_capacity="90", leverage_capacity="100",
        liquidity_factor="0.4", leverage_factor="0.8", margin_result={"status": "PASS", "evidence": {"source": "m1"}},
        liquidity_result={"status": "PASS", "evidence": {"source": "l1"}},
    )
    changed_gate = evaluate_correlated_stress(
        prior, mark="100", equity="1000", deepest_fill="80", liquidity_capacity="90", leverage_capacity="100",
        liquidity_factor="0.5", leverage_factor="0.8", margin_result={"status": "PASS", "evidence": {"source": "m2"}},
        liquidity_result={"status": "PASS", "evidence": {"source": "l1"}},
    )
    evaluated = evaluate_correlated_stress(
        prior, mark="100", equity="1000", deepest_fill="80", liquidity_capacity="90", leverage_capacity="100",
        margin_evaluator=lambda _: {"status": "PASS", "evidence": {"source": "evaluated-m"}},
        liquidity_evaluator=lambda _: {"status": "PASS", "evidence": {"source": "evaluated-l"}},
    )
    assert base.input_digest != changed_factor.input_digest
    assert base.input_digest != changed_gate.input_digest
    assert evaluated.input_digest != base.input_digest
    assert base.margin_result["evidence"]["source"] == "m1"


def test_full_fill_requires_known_requested_and_exact_cumulative_and_partial_cannot_be_complete():
    missing = reduce_order_lifecycle([event("PLACED", 0, requested="10", remaining="10"), event("FULL_FILL", 1, requested="10")])
    assert missing.status == UNKNOWN
    mismatch = reduce_order_lifecycle([event("PLACED", 0, requested="10", remaining="10"), event("FULL_FILL", 1, requested="10", fill="8", remaining="2")])
    assert mismatch.status == INCONSISTENT
    partial_complete = reduce_order_lifecycle([event("PLACED", 0, requested="10", remaining="10"), event("PARTIAL_FILL", 1, requested="10", fill="10", remaining="0")])
    assert partial_complete.status == INCONSISTENT
    full_delta = reduce_order_lifecycle([event("PLACED", 0, requested="10", remaining="10"), event("FULL_FILL", 1, requested="10", fill="10", delta="9", remaining="0")])
    assert full_delta.status == INCONSISTENT


@pytest.mark.parametrize("unknown_event_type", ("PARTIAL_FILL", "REDUCE_ONLY_FILL"))
def test_cumulative_high_water_survives_unknown_fill_events(unknown_event_type: str):
    placed = event("PLACED", 0, requested="10", remaining="10")
    first = event("PARTIAL_FILL", 1, requested="10", cumulative="5", remaining="5")
    unknown = event(unknown_event_type, 2, requested="10", reduce_only=unknown_event_type == "REDUCE_ONLY_FILL")
    lower = event(unknown_event_type, 3, requested="10", cumulative="3", remaining="7", reduce_only=unknown_event_type == "REDUCE_ONLY_FILL")

    result = reduce_order_lifecycle([lower, unknown, first, placed])

    assert result.status == INCONSISTENT
    assert result.reason == "NON_MONOTONE_CUMULATIVE_FILL"
    assert result.source_digest == reduce_order_lifecycle([placed, first, unknown, lower]).source_digest


def test_cumulative_fill_can_increase_after_unknown_fill_event():
    events = [
        event("PLACED", 0, requested="10", remaining="10"),
        event("PARTIAL_FILL", 1, requested="10", cumulative="5", remaining="5"),
        event("PARTIAL_FILL", 2, requested="10"),
        event("PARTIAL_FILL", 3, requested="10", cumulative="7", remaining="3"),
    ]

    result = reduce_order_lifecycle(list(reversed(events)))

    assert result.status == PARTIAL
    assert result.filled_qty == Decimal("7")
    assert result.fill_ratio == Decimal("0.7")
    assert result.source_digest == reduce_order_lifecycle(events).source_digest


def test_order_event_rejects_conflicting_fill_forms_at_construction_and_mapping():
    with pytest.raises(ValueError, match="fill_qty and cumulative_filled_qty must match"):
        event("PARTIAL_FILL", 1, fill="4", cumulative="5")

    mapping = event("PARTIAL_FILL", 1, fill="4").as_dict()
    mapping["cumulative_filled_qty"] = "5"
    with pytest.raises(ValueError, match="fill_qty and cumulative_filled_qty must match"):
        OrderEvent.from_mapping(mapping)


def test_fill_aliases_normalize_to_one_digest_and_are_idempotent_in_lifecycle():
    placed = event("PLACED", 0, remaining="10")
    fill_only = event("PARTIAL_FILL", 1, fill="4", remaining="6")
    cumulative_mapping = event("PARTIAL_FILL", 1, remaining="6").as_dict()
    cumulative_mapping.pop("cumulative_filled_qty")
    cumulative_mapping["cumulative_fill_qty"] = "4"
    cumulative_only = OrderEvent.from_mapping(cumulative_mapping)
    both = event("PARTIAL_FILL", 1, fill="4", cumulative="4", remaining="6")
    full = event("FULL_FILL", 2, fill="10", remaining="0")

    assert fill_only.fill_qty == cumulative_only.fill_qty == both.fill_qty == Decimal("4")
    assert fill_only.cumulative_filled_qty == cumulative_only.cumulative_filled_qty == both.cumulative_filled_qty == Decimal("4")
    assert fill_only.digest == cumulative_only.digest == both.digest

    baseline = reduce_order_lifecycle([placed, fill_only, full])
    for alias_event in (fill_only, cumulative_only, both):
        lifecycle = reduce_order_lifecycle([placed, alias_event, full])
        assert lifecycle.status == AVAILABLE
        assert lifecycle.source_digest == baseline.source_digest
        assert lifecycle.source_digest == reduce_order_lifecycle([full, alias_event, placed]).source_digest

    duplicate_aliases = reduce_order_lifecycle([placed, fill_only, cumulative_only, both, full])
    assert duplicate_aliases.status == AVAILABLE
    assert duplicate_aliases.conflicting_event_ids == ()
    assert duplicate_aliases.source_digest == baseline.source_digest


def test_revision_aliases_are_revision_only_and_must_agree():
    base = event("REPLACE_CONFIRMED", 1, revision=1, requested="8", remaining="8")
    base_payload = {key: value for key, value in base.as_dict().items() if key != "revision_link"}
    aliased = OrderEvent(
        **base_payload,
        revision_link=1,
        replaces_revision="1",
        previous_revision=1,
        replaces_order_revision="1",
    )
    assert aliased.revision_link == "1"

    with pytest.raises(ValueError, match="revision_link aliases conflict"):
        OrderEvent(**base_payload, revision_link="1", replaces_revision="2")

    with pytest.raises(TypeError):
        OrderEvent(**base_payload, replacement_order_id="order-0")


@pytest.mark.parametrize("alias", ("revision_link", "replaces_revision", "previous_revision", "replaces_order_revision"))
@pytest.mark.parametrize("bad_value", ("", "-1", -1, [], {}))
def test_revision_aliases_reject_invalid_values_after_normalization(alias: str, bad_value: object):
    base = event("PLACED", 0)
    base_payload = {key: value for key, value in base.as_dict().items() if key != "revision_link"}
    with pytest.raises((TypeError, ValueError)):
        OrderEvent(**base_payload, **{alias: bad_value})

    mapping = {**base_payload, alias: bad_value}
    with pytest.raises((TypeError, ValueError)):
        OrderEvent.from_mapping(mapping)


def test_revision_aliases_accept_zero_and_equal_int_string_forms_in_mapping():
    base = event("PLACED", 0)
    base_payload = {key: value for key, value in base.as_dict().items() if key != "revision_link"}
    direct = OrderEvent(**base_payload, revision_link=0, replaces_revision="0")
    assert direct.revision_link == "0"
    mapping = {**base_payload, "revision_link": 0, "previous_revision": "0"}
    assert OrderEvent.from_mapping(mapping).revision_link == "0"


def test_execution_observation_is_a_strict_validation_boundary():
    base = {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"}
    invalid = (
        {"status": "NOT_A_STATUS"},
        {"partial_count": True},
        {"partial_count": -1},
        {"fill_ratio": "1.1"},
        {"requested_qty": "1", "filled_qty": "2"},
        {"requested_qty": "1", "remaining_at_cancel": "2"},
        {"requested_qty": "10", "filled_qty": "5", "fill_ratio": "0.4"},
        {"requested_qty": "10", "filled_qty": "5", "fill_ratio": "0.5", "remaining_at_cancel": "6"},
        {"first_fill_seconds": "2", "full_fill_seconds": "1"},
        {"status": AVAILABLE},
    )
    for update in invalid:
        with pytest.raises((TypeError, ValueError)):
            ExecutionObservation.from_item({**base, **update})
    result = calibrate_execution([
        {**base, "requested_qty": "10", "filled_qty": "10", "fill_ratio": "1", "first_fill_seconds": "1", "full_fill_seconds": "2", "partial_count": 0, "status": AVAILABLE}
    ], policy={"version": "v1", "minimum_sample_count": 1, "applicability": "OPEN_POLICY", "confidence": "UNKNOWN"})
    assert result.base_curves[0].stable_bound is False


def test_calibration_policy_metadata_and_sample_count_are_exact_types():
    observation = [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "requested_qty": "1", "fill_ratio": "1", "first_fill_seconds": "1", "full_fill_seconds": "2"}]
    with pytest.raises((TypeError, ValueError)):
        calibrate_execution(observation, policy={"version": "v1", "applicability": 1})
    with pytest.raises((TypeError, ValueError)):
        calibrate_execution(observation, policy={"version": "v1", "confidence": 1})
    with pytest.raises((TypeError, ValueError)):
        calibrate_execution(observation, policy={"version": "v1", "minimum_sample_count": 1.0})
    with pytest.raises((TypeError, ValueError)):
        calibrate_execution(observation, policy={"version": "v1", "minimum_sample_count": 0})
    with pytest.raises((TypeError, ValueError)):
        calibrate_execution(observation, policy={"version": "v1"}, source_digest=7)
    with pytest.raises((TypeError, ValueError)):
        calibrate_execution(observation, policy={"version": "v1"}, source_digest="")


def test_execution_observation_mapping_aliases_and_types_fail_closed():
    complete = {
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h",
        "requested_qty": "1", "fill_ratio": "1", "first_fill_seconds": "1", "full_fill_seconds": "2",
    }
    with pytest.raises(ValueError, match="aliases conflict"):
        ExecutionObservation.from_item({**complete, "requested": "2"})
    with pytest.raises(ValueError, match="aliases conflict"):
        ExecutionObservation.from_item({**complete, "fill_ratio": "1", "ratio": "0.5"})
    with pytest.raises((TypeError, ValueError)):
        ExecutionObservation.from_item({**complete, "quantity_unit": 7})
    with pytest.raises((TypeError, ValueError)):
        ExecutionObservation.from_item({**complete, "source_digest": 7})
    with pytest.raises((TypeError, ValueError)):
        ExecutionObservation.from_item({**complete, "status": 7})


def test_gate_evidence_is_explicit_and_frozen():
    with pytest.raises(ValueError):
        evaluate_correlated_stress({}, deepest_fill="1", margin_result="NOT_A_GATE", liquidity_result="PASS")
    gate = {"status": "PASS", "evidence": {"source": "fixture"}}
    result = evaluate_correlated_stress({}, deepest_fill="1", margin_result=gate, liquidity_result="PASS")
    gate["evidence"]["source"] = "changed"
    assert result.status == AVAILABLE and result.margin_result["evidence"]["source"] == "fixture"
    with pytest.raises(TypeError):
        result.margin_result["evidence"] = {}


def test_gate_evidence_rejects_foreign_dataclasses_and_objects_recursively():
    @dataclass
    class ForeignEvidence:
        value: str

    class ForeignObject:
        def __init__(self) -> None:
            self.value = "mutable"

    for evidence in (ForeignEvidence("x"), ForeignObject()):
        with pytest.raises(TypeError):
            evaluate_correlated_stress(
                {}, deepest_fill="1", margin_result={"status": "PASS", "evidence": evidence}, liquidity_result="PASS"
            )


def test_aliases_reject_disagreement_and_lifecycle_arrival_order_is_irrelevant():
    with pytest.raises(ValueError):
        OrderEvent(
            event_id="aliases", execution_campaign_id="camp", trading_run_id="run", account_alias="acct",
            order_id="order", symbol="BTCUSDT", side="LONG", timeframe="1h", event_type="PLACED",
            source_sequence=1, source_index=2,
        )
    mapping = {
        "event_id": "aliases", "execution_campaign_id": "camp", "trading_run_id": "run", "account_alias": "acct",
        "order_id": "order", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "event_type": "PLACED",
        "source_sequence": 1, "source_index": 2,
    }
    with pytest.raises(ValueError):
        OrderEvent.from_mapping(mapping)
    with pytest.raises(ValueError):
        event("PLACED", 0, at=AT).from_mapping({**event("PLACED", 0, at=AT).as_dict(), "source_timestamp": AT + timedelta(seconds=1)})
    with pytest.raises(ValueError):
        OrderEvent(
            event_id="unit-alias", execution_campaign_id="camp", trading_run_id="run", account_alias="acct",
            order_id="order", symbol="BTCUSDT", side="LONG", timeframe="1h", event_type="PLACED",
            source_sequence=0, quantity_unit="contracts", unit="USDT",
        )
    placed = event("PLACED", 0, requested="10", remaining="10")
    partial = event("PARTIAL_FILL", 1, requested="10", fill="4", remaining="6")
    full = event("FULL_FILL", 2, requested="10", fill="10", remaining="0")
    result = reduce_order_lifecycle([full, partial, placed])
    assert result.status == AVAILABLE
    assert result.source_digest == reduce_order_lifecycle([placed, partial, full]).source_digest


def test_conflicting_duplicate_representative_and_metadata_are_permutation_invariant():
    placed = event("PLACED", 0, event_id="e-0", remaining="10")
    conflict_a = event("PLACED", 0, event_id="e-0", remaining="9")
    conflict_b = event("PLACED", 0, event_id="e-0", remaining="8")
    first = reduce_order_lifecycle([placed, conflict_a, conflict_b])
    second = reduce_order_lifecycle([conflict_b, placed, conflict_a])
    assert first.status == second.status == INCONSISTENT
    assert first.source_digest == second.source_digest
    assert first.events == second.events
    assert first.conflicting_event_ids == second.conflicting_event_ids


def test_duplicate_conflicting_payload_is_idempotent():
    placed = event("PLACED", 0, event_id="e-0", remaining="10")
    conflict = event("PLACED", 0, event_id="e-0", remaining="9")
    low, high = sorted((placed, conflict), key=lambda item: item.digest)
    first = reduce_order_lifecycle([low, high])
    repeated = reduce_order_lifecycle([low, high, high])
    assert repeated.status == first.status == INCONSISTENT
    assert repeated.events == first.events
    assert repeated.source_digest == first.source_digest


def test_inconsistent_order_id_result_is_permutation_invariant():
    placed = event("PLACED", 0, order_id="a", remaining="10")
    other = event("ACKNOWLEDGED", 1, order_id="b", remaining="10")
    first = reduce_order_lifecycle([placed, other])
    second = reduce_order_lifecycle([other, placed])
    assert first.status == second.status == INCONSISTENT
    assert first.order_id == second.order_id
    assert first.source_digest == second.source_digest


def test_decimal128_bounds_and_arbitrary_dataclass_rejection():
    with pytest.raises((InvalidOperation, Overflow)):
        _decimal_ratio(Decimal("1E+7000"), Decimal("1"))
    with pytest.raises((InvalidOperation, Overflow)):
        _decimal_ratio(Decimal("0E+7000"), Decimal("1"))
    with pytest.raises((InvalidOperation, Overflow)):
        _decimal_ratio(Decimal("0E-7000"), Decimal("1"))

    @dataclass
    class ForeignEvidence:
        value: str

    with pytest.raises(TypeError, match="dataclass"):
        _digest(ForeignEvidence("x"), "foreign")
