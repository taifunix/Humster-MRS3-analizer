from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from mrs3.performance_v2_equity_regime import (
    EquityRegimeSample,
    assess_equity_regime,
    calculate_equity_regime_facts,
    classify_equity_regime,
)


UTC = timezone.utc
T = datetime(2026, 10, 1, tzinfo=UTC)


def sample(day: float, value: object, index: int, *, result_id: int = 7) -> EquityRegimeSample:
    return EquityRegimeSample(
        result_id,
        index,
        T + timedelta(hours=day * 24),
        Decimal(str(value)),
    )


def curve(values: list[tuple[float, object]]) -> tuple[EquityRegimeSample, ...]:
    return tuple(sample(day, value, index) for index, (day, value) in enumerate(values))


def rising_curve(*, start: float = -42, step: float = 1) -> tuple[EquityRegimeSample, ...]:
    return curve([(start + index * step, 100 + index * 2) for index in range(43)])


def geometric_curve(*, start: int = -42, end: int = 0) -> tuple[EquityRegimeSample, ...]:
    return tuple(
        sample(day, Decimal("100") * (Decimal("1.01") ** (day - start)), index)
        for index, day in enumerate(range(start, end + 1))
    )


def six_hour_exponential_curve() -> tuple[EquityRegimeSample, ...]:
    return tuple(
        sample(-28 + index / 4, Decimal("100") * (Decimal("1.0025") ** index), index)
        for index in range(113)
    )


def six_hour_curve_for_speed(speed: Decimal) -> tuple[EquityRegimeSample, ...]:
    factor = (speed / Decimal("12000")).exp()
    return tuple(
        sample(-28 + index / 4, Decimal("100") * (factor ** index), index)
        for index in range(113)
    )


def evaluate(samples, *, report_start: datetime | None = None):
    start = report_start or min(item.timestamp_utc for item in samples)
    return classify_equity_regime(7, start, T, samples)


def test_public_wrapper_exposes_frozen_facts_and_exact_direction_epsilon():
    result = evaluate(curve([(-28, 100), (-14, 100.0), (-7, 100.0), (0, 100)]))

    assert result.state == "DROP"
    assert result.decision == "DROP"
    assert result.facts.raw_sample_count == 4
    with pytest.raises(AttributeError):
        result.state = "DROP"


def test_window_direction_uses_strict_up_and_inclusive_flat_boundaries():
    samples = curve([(-28, 100), (-14, 100), (-7, 100), (0, 100)])
    facts = calculate_equity_regime_facts(7, T - timedelta(days=28), T, samples)

    assert facts.windows["28"].direction == "FLAT"
    assert facts.windows["14"].direction == "FLAT"
    assert facts.windows["7"].direction == "FLAT"


def test_equal_timestamps_keep_sample_index_order_for_grid_and_ath_events():
    samples = curve([
        (-28, 100), (-14, 110), (-7, 120),
        (-1, 125), (-1, 130), (-1, 126), (0, 126), (0, 127),
    ])

    facts = calculate_equity_regime_facts(7, T - timedelta(days=28), T, samples)

    assert facts.windows_7.start_equity == Decimal("120")
    assert facts.hwm_t7 == Decimal("120")
    assert facts.new_ath_w7_times_utc == (T - timedelta(days=1), T - timedelta(days=1))
    assert facts.new_ath_w7_values == (Decimal("125"), Decimal("130"))
    assert facts.final_equity == Decimal("127")


def test_boundary_hwms_carry_the_last_full_raw_record_before_each_boundary():
    samples = curve([
        (-42, 100), (-28, 105), (-14.25, 140), (-7.25, 160), (-0.25, 180), (0, 175)
    ])

    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, samples)

    assert (facts.hwm_t28, facts.hwm_t28_time_utc) == (Decimal("105"), T - timedelta(days=28))
    assert (facts.hwm_t14, facts.hwm_t14_time_utc) == (Decimal("140"), T - timedelta(days=14, hours=6))
    assert (facts.hwm_t7, facts.hwm_t7_time_utc) == (Decimal("160"), T - timedelta(days=7, hours=6))
    assert (facts.hwm_t, facts.hwm_t_time_utc) == (Decimal("180"), T - timedelta(hours=6))


def test_same_timestamp_raw_high_then_low_contributes_to_drawdown():
    samples = curve([
        (-42, 100), (-28, 110), (-14, 115), (-7, 120),
        (-1, 125), (-1, 90), (0, 130),
    ])

    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, samples)

    assert facts.dd7 >= Decimal("28")


def test_exact_epsilon_is_flat_for_a_six_hour_carry_forward_grid():
    facts = calculate_equity_regime_facts(
        7, T - timedelta(days=28), T, six_hour_curve_for_speed(Decimal("2"))
    )

    assert abs(facts.windows["28"].v - Decimal("2")) < Decimal("0.0000000001")
    assert abs(facts.windows["28"].p - Decimal("2")) < Decimal("0.0000000001")
    assert facts.windows["28"].direction == "FLAT"


def test_grid_is_anchored_to_report_end_when_source_points_are_off_grid():
    left = T - timedelta(days=28)
    samples = (
        EquityRegimeSample(7, 0, left - timedelta(hours=1), Decimal("100")),
        EquityRegimeSample(7, 1, left + timedelta(hours=5), Decimal("200")),
        EquityRegimeSample(7, 2, T, Decimal("210")),
    )

    facts = calculate_equity_regime_facts(7, left - timedelta(hours=1), T, samples)

    assert facts.windows_28.grid_points == 113
    assert facts.windows_28.start_equity == Decimal("100")
    assert facts.windows_28.end_equity == Decimal("210")


def test_nonpositive_or_missing_w28_is_nonsticky_not_evaluated():
    nonpositive = curve([(-28, 100), (-14, 0), (-7, 110), (0, 120)])
    missing_w28 = curve([(-27.999, 100), (-14, 110), (-7, 120), (0, 130)])

    for samples in (nonpositive, missing_w28):
        result = evaluate(samples)
        assert result.state == "NOT_EVALUATED"
        assert result.decision == "NOT_EVALUATED"
        assert result.rank is None


def test_pre28_is_available_at_exactly_fourteen_elapsed_days():
    samples = curve([(-42, 100), (-28, 110), (-14, 120), (-7, 130), (0, 140)])
    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, samples)

    assert facts.pre28 is not None
    assert facts.pre28.elapsed_days == Decimal("14")


def test_pre28_samples_on_the_grid_anchored_to_report_end():
    left = T - timedelta(days=28)
    first = left - timedelta(days=14, hours=2)
    samples = [EquityRegimeSample(7, 0, first, Decimal("100"))]
    samples.extend(
        EquityRegimeSample(7, index, first + timedelta(hours=6 * index), Decimal(100 + index))
        for index in range(1, 57)
    )
    samples.append(EquityRegimeSample(7, 57, T, Decimal("200")))

    facts = calculate_equity_regime_facts(7, first, T, samples)

    grid_times = [first, *[left - timedelta(hours=6 * index) for index in range(56, -1, -1)]]
    cursor = 0
    values = []
    for stamp in grid_times:
        while cursor + 1 < len(samples) and samples[cursor + 1].timestamp_utc <= stamp:
            cursor += 1
        values.append(samples[cursor].equity)
    elapsed = Decimal((left - first).total_seconds()) / Decimal(86400)
    with localcontext() as context:
        context.prec = 38
        xs = [Decimal((stamp - first).total_seconds()) / Decimal(86400) for stamp in grid_times]
        logs = [value.ln() for value in values]
        xbar = sum(xs) / Decimal(len(xs))
        ybar = sum(logs) / Decimal(len(logs))
        slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, logs)) / sum(
            (x - xbar) ** 2 for x in xs
        )

    assert abs(facts.pre28.elapsed_days - elapsed) < Decimal("0.0000000000000000000000001")
    assert abs(facts.pre28.v - Decimal(3000) * slope) < Decimal("0.00000001")


def test_all_hard_reasons_are_emitted_in_canonical_order():
    samples = curve([
        (-42, 100), (-28, 120), (-14, 90), (-7, 70), (-3, 75), (0, 80)
    ])
    result = evaluate(samples, report_start=T - timedelta(days=42))

    assert result.state == "DROP"
    assert result.decision == "DROP"
    assert result.reasons == (
        "DD_14_7_GTE_23",
        "W28_DOWN",
    )


def test_w28_flat_with_unavailable_pre28_is_hard_drop():
    samples = curve([(-28, 100), (-14, 100), (-7, 101), (0, 100)])
    result = evaluate(samples)

    assert result.state == "DROP"
    assert result.reasons == ("PRE28_AND_W28_NOT_UP",)


def test_hard_reason_order_keeps_dd_before_pre28_flat_reason():
    samples = curve([(-42, 120), (-28, 100), (-14, 100), (-7, 60), (0, 100)])
    result = evaluate(samples, report_start=T - timedelta(days=42))

    assert result.reasons == ("DD_14_7_GTE_23", "PRE28_AND_W28_NOT_UP")


def test_invalid_chronology_is_technical_not_evaluated():
    samples = (
        sample(-28, 100, 0),
        sample(-14, 110, 1),
        sample(-21, 105, 2),
        sample(0, 120, 3),
    )
    result = evaluate(samples, report_start=T - timedelta(days=28))

    assert result.state == "NOT_EVALUATED"
    assert result.reasons == ("UNORDERED_EQUITY_SOURCE",)


def test_full_raw_point_drawdown_catches_intragrid_low():
    samples = curve([
        (-42, 100), (-28, 110), (-14, 115), (-7, 120),
        (-6.75, 90), (-6.5, 120), (0, 125),
    ])
    result = evaluate(samples, report_start=T - timedelta(days=42))

    assert result.facts.dd7 >= Decimal("23")
    assert "DD_14_7_GTE_23" in result.reasons


def test_growing_requires_three_strict_ath_stages_and_held_w7_breakout():
    samples = geometric_curve()
    result = evaluate(samples, report_start=T - timedelta(days=42))

    assert result.state == "GROWING"
    assert result.decision == "PASS"
    assert result.rank == "GROWING"
    assert result.facts.ath_stage_strict_increase is True
    assert result.facts.held_w7_breakout is True


def test_t_minus_7_boundary_record_belongs_to_previous_ath_stage():
    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, geometric_curve())

    assert facts.hwm_t7_time_utc == T - timedelta(days=7)
    assert facts.ath_stage_event_times_utc[1][-1] == T - timedelta(days=7)
    assert all(stamp > T - timedelta(days=7) for stamp in facts.new_ath_w7_times_utc)
    assert facts.previous_ath_w7_time_utc == facts.hwm_t7_time_utc
    assert facts.new_ath_w7_values == tuple(
        value for _, value in facts.ath_stage_events[2]
    )


def test_ath_event_values_match_their_ordered_timestamps_and_boundary_hwms():
    samples = geometric_curve()
    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, samples)

    events = facts.ath_stage_events
    assert events[0][0] == (samples[15].timestamp_utc, samples[15].equity)
    assert events[1][-1] == (samples[35].timestamp_utc, samples[35].equity)
    assert events[2][-1] == (samples[42].timestamp_utc, samples[42].equity)
    assert facts.hwm_t28_time_utc == T - timedelta(days=28)
    assert facts.hwm_t14_time_utc == T - timedelta(days=14)
    assert facts.hwm_t7_time_utc == T - timedelta(days=7)
    assert facts.hwm_t_time_utc == T
    payload = facts.to_canonical_dict()
    assert payload["previous_ath_w7_time_utc"] == (T - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    assert payload["ath_stage_event_values"][1][-1] == format(samples[35].equity, "f")


def test_weekly_breakout_must_be_held_strictly_above_previous_ath():
    samples = list(geometric_curve())
    samples[-1] = sample(0, samples[35].equity, samples[-1].sample_index)
    result = evaluate(tuple(samples), report_start=T - timedelta(days=42))

    assert result.facts.new_ath_w7 is True
    assert result.facts.held_w7_breakout is False
    assert result.state == "STALLED"


def test_growing_equality_at_twenty_percent_is_two_step_slowdown():
    samples = geometric_curve()
    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, samples)
    windows = {
        key: replace(facts.windows[key], v=Decimal(value), p=Decimal(value), direction="UP")
        for key, value in (("28", "10"), ("14", "8"), ("7", "6.4"))
    }
    facts = replace(facts, windows_28=windows["28"], windows_14=windows["14"], windows_7=windows["7"])

    assessment = assess_equity_regime(facts)
    assert assessment.state == "WEAKENING"
    assert "TWO_STEP_SLOWDOWN" in assessment.reasons


def test_slowdown_comparison_keeps_decimal_precision_at_exact_twenty_percent():
    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, geometric_curve())
    v28 = Decimal("100000000000000000000000000000.1")
    v14 = Decimal("80000000000000000000000000000.08")
    v7 = Decimal("64000000000000000000000000000.064")
    windows = {
        "28": replace(facts.windows["28"], v=v28, p=v28, direction="UP"),
        "14": replace(facts.windows["14"], v=v14, p=v14, direction="UP"),
        "7": replace(facts.windows["7"], v=v7, p=v7, direction="UP"),
    }
    facts = replace(
        facts,
        windows_28=windows["28"],
        windows_14=windows["14"],
        windows_7=windows["7"],
    )

    assessment = assess_equity_regime(facts)

    assert assessment.state == "WEAKENING"
    assert "TWO_STEP_SLOWDOWN" in assessment.reasons


@pytest.mark.parametrize("drawdown, expected", [("22.999", "PASS"), ("23", "DROP"), ("23.001", "DROP")])
def test_dd_23_percent_boundary_is_inclusive(drawdown, expected):
    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, geometric_curve())
    assessment = assess_equity_regime(replace(facts, dd14=Decimal(drawdown)))

    assert assessment.decision == expected


def test_all_applicable_hard_reasons_are_returned_in_canonical_order():
    samples = curve([
        (-42, 200), (-28, 100), (-14, 100), (-7, 60), (0, 100)
    ])
    result = evaluate(samples, report_start=T - timedelta(days=42))

    assert result.reasons == (
        "DD_14_7_GTE_23",
        "PRE28_AND_W28_NOT_UP",
    )


def test_drawdown_carries_the_left_boundary_state_from_the_previous_raw_point():
    samples = curve([
        (-42, 100), (-28, 110), (-15, 120), (-14.5, 80), (-10, 82), (-7, 84), (0, 130)
    ])

    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, samples)

    assert abs(facts.dd14 - Decimal(100) / Decimal(3)) < Decimal("0.000001")


def test_growing_accelerated_variant_includes_exact_7_and_10_thresholds():
    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, geometric_curve())
    windows = {
        key: replace(facts.windows[key], v=Decimal(value), p=Decimal(value), direction="UP")
        for key, value in (("28", "7"), ("14", "10"), ("7", "10"))
    }
    threshold = replace(facts, windows_28=windows["28"], windows_14=windows["14"], windows_7=windows["7"])
    assert assess_equity_regime(threshold).state == "GROWING"

    below = replace(threshold, windows_28=replace(windows["28"], v=Decimal("6.999999"), p=Decimal("6.999999")))
    assert assess_equity_regime(below).state == "WEAKENING"


def test_pre28_up_and_w28_flat_requires_both_short_windows_for_resumed():
    facts = calculate_equity_regime_facts(7, T - timedelta(days=42), T, geometric_curve())
    flat = replace(facts.windows["28"], v=Decimal("0"), p=Decimal("0"), direction="FLAT")
    resumed = replace(facts, windows_28=flat)
    assert assess_equity_regime(resumed).state == "RESUMED"

    w14_mixed = replace(facts.windows["14"], direction="MIXED")
    stalled = replace(resumed, windows_14=w14_mixed)
    assert assess_equity_regime(stalled).state == "STALLED"


def test_resumed_w28_up_accepts_pre28_down_when_w7_breakout_is_held():
    samples = curve([
        (-42, 120), (-28, 100), (-14, 105), (-7, 110), (-6, 122), (0, 121)
    ])
    result = evaluate(samples, report_start=T - timedelta(days=42))

    assert result.state == "RESUMED"
    assert result.decision == "PASS"


def test_stalled_is_reserved_when_growth_or_resume_geometry_is_not_confirmed():
    samples = curve([
        (-42, 100), (-28, 110), (-14, 112), (-7, 111), (0, 111)
    ])
    result = evaluate(samples, report_start=T - timedelta(days=42))

    assert result.state == "STALLED"
    assert result.decision == "PASS"
    assert result.rank == "RESERVED"


def test_exponential_curve_has_matching_v_and_p():
    samples = six_hour_exponential_curve()
    facts = calculate_equity_regime_facts(7, T - timedelta(days=28), T, samples)

    for window in facts.windows.values():
        assert abs(window.v - window.p) < Decimal("0.0000000001")


def test_factory_and_separate_assessment_are_equivalent():
    samples = rising_curve()
    direct = classify_equity_regime(7, T - timedelta(days=42), T, samples)
    separate = assess_equity_regime(
        calculate_equity_regime_facts(7, T - timedelta(days=42), T, samples)
    )

    assert direct == separate
