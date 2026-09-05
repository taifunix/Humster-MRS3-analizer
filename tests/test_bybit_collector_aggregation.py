from __future__ import annotations

from math import isclose

import pytest

import mrs3.bybit_collector.aggregation as aggregation_module
from mrs3.bybit_collector.aggregation import (
    BANDS_BPS,
    LIQUIDITY_1M_COLUMNS,
    FiveSecondScheduler,
    MarketSample,
    MinuteAggregator,
    quantile,
)


def sample(
    timestamp_ms: int = 12_345,
    *,
    bids: dict[float, float] | None = None,
    asks: dict[float, float] | None = None,
    valid: bool = True,
    connected: bool = True,
    reset_count: int = 0,
) -> MarketSample:
    return MarketSample(
        local_timestamp_ms=timestamp_ms,
        bids=bids if bids is not None else {100.0: 2.0},
        asks=asks if asks is not None else {101.0: 3.0},
        book_valid=valid,
        ws_connected=connected,
        reset_count=reset_count,
    )


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([7.0], 0.05, 7.0),
        ([0.0, 10.0], 0.05, 0.5),
        ([0.0, 10.0], 0.95, 9.5),
        ([0.0, 10.0, 20.0], 0.5, 10.0),
        (list(range(20)), 0.05, 0.95),
        (list(range(20)), 0.95, 18.05),
        ([3.0, 3.0, 8.0], 0.95, 7.5),
    ],
)
def test_quantile_uses_specified_linear_interpolation(values, p, expected) -> None:
    assert quantile(values, p) == pytest.approx(expected)


def test_quantile_empty_is_null() -> None:
    assert quantile([], 0.5) is None


@pytest.mark.parametrize("probability", [-0.01, 1.01])
def test_quantile_rejects_probability_outside_unit_interval(probability: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        quantile([1.0], probability)


def test_scheduler_returns_normal_five_second_boundaries_without_sleep() -> None:
    scheduler = FiveSecondScheduler()

    assert scheduler.poll(wall_ms=1_001, monotonic_ms=10_000).due_boundaries == ()
    result = scheduler.poll(wall_ms=6_002, monotonic_ms=15_001)

    assert result.due_boundaries == (5_000,)
    assert result.missed_boundaries == 0
    assert not result.clock_discontinuity
    assert result.reanchored is False


def test_scheduler_returns_only_newest_due_boundary_and_counts_older_ones_as_missed() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)

    result = scheduler.poll(wall_ms=16_001, monotonic_ms=25_001)

    assert result.due_boundaries == (15_000,)
    assert result.missed_boundaries == 2
    assert result.missed_boundary_range_ms == (5_000, 10_000)


def test_scheduler_discontinuity_reports_count_without_synthetic_timestamps() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)

    result = scheduler.poll(wall_ms=1_100, monotonic_ms=30_000)

    assert result.due_boundaries == ()
    assert result.missed_boundaries == 4
    assert result.missed_boundary_range_ms is None


def test_scheduler_discontinuity_count_is_authoritative_without_synthetic_timestamps() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=0, monotonic_ms=0)

    result = scheduler.poll(wall_ms=120_100, monotonic_ms=125_000)

    assert result.missed_boundaries == 25
    assert result.missed_boundary_range_ms is None


def test_scheduler_short_discontinuity_does_not_report_a_missed_boundary() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)

    result = scheduler.poll(wall_ms=900, monotonic_ms=10_001)

    assert result.clock_discontinuity
    assert result.missed_boundaries == 0
    assert result.missed_boundary_range_ms is None


def test_scheduler_late_poll_does_not_inflate_aggregator_sample_count() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)
    result = scheduler.poll(wall_ms=16_001, monotonic_ms=25_001)
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=3)

    for _boundary in result.due_boundaries:
        aggregate.record_boundary(sample())
    aggregate.record_missed_boundary(result.missed_boundaries)

    row = aggregate.finalize()

    assert row["sample_count"] == 1
    assert row["valid_sample_count"] == 1
    assert aggregate.missed_boundary_count == 2


@pytest.mark.parametrize(
    ("wall_ms", "monotonic_ms", "expected_missed"),
    [(30_000, 15_000, 1), (900, 15_000, 1)],
)
def test_scheduler_reanchors_on_forward_or_backward_clock_jump(
    wall_ms: int, monotonic_ms: int, expected_missed: int
) -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)

    result = scheduler.poll(wall_ms=wall_ms, monotonic_ms=monotonic_ms)

    assert result.due_boundaries == ()
    assert result.missed_boundaries == expected_missed
    assert result.missed_boundary_range_ms is None
    assert result.clock_discontinuity
    assert result.reanchored
    # The next poll must continue from the new wall-clock anchor, never replaying
    # boundaries from the old timeline.
    after = scheduler.poll(wall_ms=wall_ms + 5_001, monotonic_ms=monotonic_ms + 5_001)
    assert after.due_boundaries == (result.next_boundary_ms,)


def test_scheduler_reports_suspend_as_missed_without_backfill() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)

    result = scheduler.poll(wall_ms=1_100, monotonic_ms=30_000)

    assert result.clock_discontinuity
    assert result.due_boundaries == ()
    assert result.missed_boundaries == 4
    assert result.missed_boundary_range_ms is None


def test_scheduler_discontinuity_does_not_overlap_subsequent_due_or_missed_boundaries() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)

    discontinuity = scheduler.poll(wall_ms=1_100, monotonic_ms=30_000)
    after = scheduler.poll(wall_ms=5_001, monotonic_ms=34_001)

    assert discontinuity.missed_boundary_range_ms is None
    assert after.due_boundaries == (5_000,)


def test_scheduler_backward_jump_never_replays_an_emitted_boundary() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)
    before_jump = scheduler.poll(wall_ms=6_000, monotonic_ms=15_000)

    discontinuity = scheduler.poll(wall_ms=1_000, monotonic_ms=25_000)
    after_jump = scheduler.poll(wall_ms=10_001, monotonic_ms=34_001)

    assert before_jump.due_boundaries == (5_000,)
    assert discontinuity.due_boundaries == ()
    assert discontinuity.missed_boundary_range_ms is None
    assert discontinuity.next_boundary_ms >= before_jump.due_boundaries[-1] + 5_000
    assert after_jump.due_boundaries == (10_000,)
    assert after_jump.clock_discontinuity
    assert len({*before_jump.due_boundaries, *after_jump.due_boundaries}) == 2


def test_scheduler_backward_jump_after_emission_uses_bounded_fault_cadence() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=3_600_000, monotonic_ms=0)
    emitted = scheduler.poll(wall_ms=3_605_001, monotonic_ms=5_001)

    discontinuity = scheduler.poll(wall_ms=1_000, monotonic_ms=10_001)
    assert emitted.due_boundaries == (3_605_000,)
    assert discontinuity.clock_discontinuity
    assert discontinuity.missed_boundary_range_ms is None
    assert discontinuity.next_boundary_ms == 3_610_000
    assert scheduler.next_wait_ms(10_001) <= scheduler.interval_ms

    next_boundary = scheduler.poll(wall_ms=1_100, monotonic_ms=15_002)
    assert not next_boundary.clock_discontinuity
    assert next_boundary.due_boundaries == (3_610_000,)
    assert next_boundary.missed_boundary_range_ms is None

    later_boundary = scheduler.poll(wall_ms=1_200, monotonic_ms=25_003)
    assert not later_boundary.clock_discontinuity
    assert later_boundary.due_boundaries == (3_620_000,)
    assert later_boundary.missed_boundaries == 1
    assert later_boundary.missed_boundary_range_ms == (3_615_000, 3_615_000)
    assert not ({3_605_000} & set(next_boundary.due_boundaries + later_boundary.due_boundaries))


def test_scheduler_one_hour_backward_jump_signals_once_while_samples_continue() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=3_600_000, monotonic_ms=0)
    emitted = scheduler.poll(wall_ms=3_605_001, monotonic_ms=5_001)

    entering = scheduler.poll(wall_ms=5_000, monotonic_ms=10_001)
    stale_sample = scheduler.poll(wall_ms=5_100, monotonic_ms=15_002)
    stale_sample_later = scheduler.poll(wall_ms=5_200, monotonic_ms=25_003)

    assert emitted.due_boundaries == (3_605_000,)
    assert entering.clock_discontinuity
    assert not stale_sample.clock_discontinuity
    assert not stale_sample_later.clock_discontinuity
    assert stale_sample.due_boundaries == (3_610_000,)
    assert stale_sample_later.due_boundaries == (3_620_000,)


def test_scheduler_late_poll_reports_compact_range_for_missed_boundaries() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=0, monotonic_ms=0)

    wall_ms = 10**15
    result = scheduler.poll(wall_ms=wall_ms, monotonic_ms=wall_ms)

    assert result.due_boundaries == (wall_ms,)
    assert result.missed_boundaries == wall_ms // 5_000 - 1
    assert result.missed_boundary_range_ms == (5_000, wall_ms - 5_000)


@pytest.mark.parametrize("interval_ms", [0, -1])
def test_scheduler_rejects_non_positive_interval(interval_ms: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        FiveSecondScheduler(interval_ms=interval_ms)


@pytest.mark.parametrize("interval_ms", [True, 1.5, "5000", None])
def test_scheduler_rejects_non_integer_interval(interval_ms) -> None:
    with pytest.raises(ValueError, match="interval_ms must be an integer"):
        FiveSecondScheduler(interval_ms=interval_ms)


@pytest.mark.parametrize("clock_tolerance_ms", [True, 1.5, "1000", None])
def test_scheduler_rejects_non_integer_clock_tolerance(clock_tolerance_ms) -> None:
    with pytest.raises(ValueError, match="clock_tolerance_ms must be an integer"):
        FiveSecondScheduler(clock_tolerance_ms=clock_tolerance_ms)


def test_scheduler_rejects_negative_clock_tolerance() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        FiveSecondScheduler(clock_tolerance_ms=-1)


def test_scheduler_next_wait_reanchors_after_discontinuity() -> None:
    scheduler = FiveSecondScheduler()
    scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)

    result = scheduler.poll(wall_ms=1_100, monotonic_ms=30_000)

    assert result.next_boundary_ms == 5_000
    assert result.next_boundary_monotonic_ms == 33_900
    assert scheduler.next_wait_ms(30_000) == 3_900
    assert scheduler.next_wait_ms(33_900) == 0


def test_scheduler_next_wait_is_derived_from_monotonic_deadline_before_and_after_poll() -> None:
    scheduler = FiveSecondScheduler()

    assert scheduler.next_wait_ms(10_000) is None
    with pytest.raises(ValueError, match="must be an integer"):
        scheduler.next_wait_ms(10_000.5)
    first = scheduler.poll(wall_ms=1_000, monotonic_ms=10_000)
    assert first.next_boundary_ms == 5_000
    assert scheduler.next_wait_ms(10_000) == 4_000
    assert scheduler.next_wait_ms(12_345) == 1_655

    second = scheduler.poll(wall_ms=5_000, monotonic_ms=14_000)
    assert second.due_boundaries == (5_000,)
    assert scheduler.next_wait_ms(14_000) == 5_000
    assert scheduler.next_wait_ms(19_001) == 0


@pytest.mark.parametrize("value", [1.5, "1000", None, True])
def test_scheduler_rejects_non_integer_clock_inputs(value) -> None:
    scheduler = FiveSecondScheduler()
    with pytest.raises(ValueError, match="must be an integer"):
        scheduler.poll(wall_ms=value, monotonic_ms=0)

    scheduler.poll(wall_ms=0, monotonic_ms=0)
    with pytest.raises(ValueError, match="must be an integer"):
        scheduler.next_wait_ms(value)


def test_zero_target_emits_no_row() -> None:
    assert MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=0).finalize() is None


@pytest.mark.parametrize("symbol", ["", None, 1, True])
def test_minute_aggregator_rejects_empty_or_non_string_symbol(symbol) -> None:
    with pytest.raises(ValueError, match="symbol must be a non-empty string"):
        MinuteAggregator(symbol, minute_ts_ms=0, active_sample_target=1)


def test_minute_aggregator_rejects_negative_active_sample_target() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=-1)


@pytest.mark.parametrize("minute_ts_ms", [1.5, "60000", True])
def test_minute_aggregator_rejects_non_integer_minute_timestamp(minute_ts_ms) -> None:
    with pytest.raises(ValueError, match="minute_ts_ms must be an integer"):
        MinuteAggregator("BTCUSDT", minute_ts_ms=minute_ts_ms, active_sample_target=1)


def test_zero_valid_row_has_counts_ratios_and_null_market_fields() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=60_000, active_sample_target=3)
    aggregate.add_sample(sample(valid=False, connected=True))
    aggregate.add_sample(sample(valid=False, connected=False))
    aggregate.record_missed_boundary()

    row = aggregate.finalize()

    assert row["sample_count"] == 2
    assert row["valid_sample_count"] == 0
    assert row["active_sample_target"] == 3
    assert row["coverage_ratio"] == 0.0
    assert row["ws_connected_ratio"] == pytest.approx(1 / 3)
    assert row["mid_median"] is None
    assert row["spread_bps_median"] is None
    assert row["bid_depth_usdt_10bps_p05"] is None
    assert row["depth_100bps_complete_ratio"] is None


def test_invalid_sample_input_leaves_all_aggregation_state_unchanged() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=60_000, active_sample_target=2)
    aggregate.add_sample(sample(reset_count=2))
    before = aggregate.finalize()

    with pytest.raises(ValueError, match="sample must be a MarketSample"):
        aggregate.add_sample(object())
    with pytest.raises(ValueError, match="reset_count must be a non-negative integer"):
        aggregate.add_sample(sample(reset_count=-1))

    assert aggregate.finalize() == before


def test_overfeeding_sample_is_rejected_atomically_and_ratios_stay_bounded() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=60_000, active_sample_target=2)
    aggregate.add_sample(sample(reset_count=2))
    aggregate.record_missed_boundary()
    before = aggregate.finalize()

    with pytest.raises(ValueError, match="active sample target"):
        aggregate.add_sample(sample(reset_count=7))

    assert aggregate.finalize() == before
    assert before["coverage_ratio"] <= 1
    assert before["ws_connected_ratio"] <= 1


def test_overfeeding_missed_boundaries_saturates_to_capacity() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=60_000, active_sample_target=2)
    aggregate.add_sample(sample())
    before = aggregate.finalize()

    aggregate.record_missed_boundary(2)

    assert aggregate.finalize() == before
    assert aggregate.missed_boundary_count == 1
    assert before["coverage_ratio"] <= 1
    assert before["ws_connected_ratio"] <= 1


def test_clamp_missed_boundaries_to_minute_capacity() -> None:
    helper = getattr(aggregation_module, "clamp_missed_boundaries", None)
    assert helper is not None
    assert helper(5, 2) == 2
    assert helper(2, 5) == 2


def test_aggregation_schema_has_exact_order_and_python_types() -> None:
    row = MinuteAggregator("BTCUSDT", minute_ts_ms=60_000, active_sample_target=1)
    row.add_sample(sample())
    row = row.finalize()

    assert tuple(row) == LIQUIDITY_1M_COLUMNS
    assert len(LIQUIDITY_1M_COLUMNS) == 32
    assert LIQUIDITY_1M_COLUMNS[:8] == (
        "minute_ts_ms",
        "symbol",
        "sample_count",
        "valid_sample_count",
        "coverage_ratio",
        "book_reset_count",
        "ws_connected_ratio",
        "active_sample_target",
    )
    assert isinstance(row["minute_ts_ms"], int)
    assert isinstance(row["symbol"], str)
    for field in ("sample_count", "valid_sample_count", "book_reset_count", "active_sample_target"):
        assert isinstance(row[field], int)
    for field in ("coverage_ratio", "ws_connected_ratio", "mid_median"):
        assert isinstance(row[field], float)


def test_valid_samples_compute_mid_spread_depth_and_reset_count() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=2)
    aggregate.add_sample(
        sample(
            bids={100.0: 2.0, 99.5: 1.0},
            asks={101.0: 3.0, 101.5: 1.0},
            reset_count=1,
        )
    )
    aggregate.add_sample(
        sample(
            bids={100.0: 4.0, 99.5: 2.0},
            asks={101.0: 1.0, 101.5: 2.0},
            reset_count=2,
        )
    )

    row = aggregate.finalize()

    assert row["sample_count"] == row["valid_sample_count"] == 2
    assert row["book_reset_count"] == 3
    assert row["mid_median"] == pytest.approx(100.5)
    assert row["spread_bps_median"] == pytest.approx((1 / 100.5) * 10_000)
    assert row["spread_bps_max"] == row["spread_bps_median"]
    assert row["bid_depth_usdt_100bps_p05"] == pytest.approx(314.475)
    assert row["bid_depth_usdt_100bps_median"] == pytest.approx(449.25)
    assert row["ask_depth_usdt_100bps_p05"] == pytest.approx(309.025)
    assert row["ask_depth_usdt_100bps_median"] == pytest.approx(354.25)


def test_partial_depth_is_quantiled_but_complete_ratio_requires_both_sides() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=2)
    aggregate.add_sample(sample(bids={100.0: 1.0}, asks={101.0: 1.0}))
    aggregate.add_sample(
        sample(
            bids={100.0: 1.0, 99.0: 1.0},
            asks={101.0: 1.0, 102.0: 1.0},
        )
    )

    row = aggregate.finalize()

    assert row["bid_depth_usdt_100bps_p05"] is not None
    assert row["ask_depth_usdt_100bps_median"] is not None
    assert row["depth_100bps_complete_ratio"] == pytest.approx(0.5)


def test_invalid_sample_does_not_enter_market_metrics_or_valid_count() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=2)
    aggregate.add_sample(sample(valid=True))
    aggregate.add_sample(sample(bids={100.0: 1.0}, asks={100.0: 1.0}, valid=True))

    row = aggregate.finalize()

    assert row["sample_count"] == 2
    assert row["valid_sample_count"] == 1
    assert row["coverage_ratio"] == 0.5
    assert row["mid_median"] == pytest.approx(100.5)


def test_invalid_deep_levels_are_ignored_when_each_side_retains_valid_levels() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=1)
    aggregate.add_sample(
        sample(
            bids={100.0: 2.0, 99.0: 0.0, 98.0: -1.0, float("nan"): 4.0, "bad": 1.0},
            asks={101.0: 3.0, 102.0: float("nan"), 103.0: 0.0, "bad": 1.0},
        )
    )

    row = aggregate.finalize()

    assert row["valid_sample_count"] == 1
    assert row["mid_median"] == pytest.approx(100.5)


def test_malformed_deep_levels_do_not_mark_depth_complete() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=1)
    aggregate.add_sample(
        sample(
            bids={100.0: 2.0, 99.0: "bad"},
            asks={101.0: 3.0, 102.0: "bad"},
        )
    )

    row = aggregate.finalize()

    assert row["valid_sample_count"] == 1
    assert row["depth_100bps_complete_ratio"] == 0.0


@pytest.mark.parametrize(
    ("bids", "asks"),
    [({}, {101.0: 1.0}), ({100.0: 1.0}, {})],
)
def test_empty_usable_side_makes_sample_invalid(bids, asks) -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=1)
    aggregate.add_sample(sample(bids=bids, asks=asks))

    assert aggregate.finalize()["valid_sample_count"] == 0


@pytest.mark.parametrize(
    ("bids", "asks"),
    [({101.0: 1.0}, {101.0: 1.0}), ({102.0: 1.0}, {101.0: 1.0})],
)
def test_equal_or_crossed_best_levels_make_sample_invalid(bids, asks) -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=1)
    aggregate.add_sample(sample(bids=bids, asks=asks))

    assert aggregate.finalize()["valid_sample_count"] == 0


def test_record_boundary_none_counts_missed_boundaries_without_sample_inflation() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=2)
    aggregate.record_boundary(None)
    aggregate.record_boundary(None)

    row = aggregate.finalize()

    assert aggregate.missed_boundary_count == 2
    assert row["sample_count"] == 0
    assert row["valid_sample_count"] == 0


@pytest.mark.parametrize("count", [-1, True])
def test_record_missed_boundary_rejects_negative_or_boolean_count(count) -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=2)

    with pytest.raises(ValueError, match="non-negative integer"):
        aggregate.record_missed_boundary(count)

    assert aggregate.missed_boundary_count == 0
    assert aggregate.finalize()["sample_count"] == 0


def test_reset_count_is_summed_per_boundary_delta() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=2)
    aggregate.add_sample(sample(reset_count=7))
    aggregate.add_sample(sample(reset_count=8))

    assert aggregate.finalize()["book_reset_count"] == 15


def test_spread_p95_uses_linear_interpolation() -> None:
    aggregate = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=3)
    aggregate.add_sample(sample(bids={99.0: 1.0}, asks={101.0: 1.0}))
    aggregate.add_sample(sample(bids={98.0: 1.0}, asks={102.0: 1.0}))
    aggregate.add_sample(sample(bids={97.0: 1.0}, asks={103.0: 1.0}))

    assert aggregate.finalize()["spread_bps_p95"] == pytest.approx(580.0)


def test_all_bands_have_the_four_depth_fields_then_complete_ratio() -> None:
    row = MinuteAggregator("BTCUSDT", minute_ts_ms=0, active_sample_target=1)
    row.add_sample(sample())
    keys = tuple(row.finalize())
    depth_start = keys.index("bid_depth_usdt_10bps_p05")
    for offset, band in enumerate(BANDS_BPS):
        start = depth_start + offset * 4
        assert keys[start : start + 4] == (
            f"bid_depth_usdt_{band}bps_p05",
            f"bid_depth_usdt_{band}bps_median",
            f"ask_depth_usdt_{band}bps_p05",
            f"ask_depth_usdt_{band}bps_median",
        )
    assert keys[depth_start + len(BANDS_BPS) * 4 :] == tuple(
        f"depth_{band}bps_complete_ratio" for band in BANDS_BPS
    )
