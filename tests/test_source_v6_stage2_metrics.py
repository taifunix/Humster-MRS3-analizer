from __future__ import annotations

from dataclasses import replace
from decimal import Decimal


def _fragment(*, declared_dd: str = "50"):
    from mrs3.source_v6 import (
        PointIdentity,
        SourceV6Fragment,
        NormalizedAction,
        NormalizedSample,
        reconstruct_derived_facts,
    )

    point = PointIdentity("BTCUSDT", "LONG", "1h", 100, "SMA", "close", 7, "SMA", "close", 3)
    rows = (
        ("lead", 1, "decreased", "10", "0", "0", "0"),
        ("open", 2, "opened", "0", "50", "50", "50"),
        ("increase", 3, "increased", "0", "50", "100", "100"),
        ("partial", 4, "decreased", "2", "50", "50", "50"),
        ("reenter", 5, "increased", "0", "50", "100", "100"),
        ("close", 6, "closed", "-1", "100", "0", "0"),
        ("tail", 7, "increased", "0", "50", "50", "50"),
    )
    actions = tuple(
        NormalizedAction(
            action_id, timestamp, "BTCUSDT", "order", action,
            Decimal("0"), Decimal(pnl), Decimal(balance), Decimal(size), Decimal(post_size), "long",
        )
        for action_id, timestamp, action, pnl, size, post_size, balance in rows
    )
    cycles, events, open_tail = reconstruct_derived_facts(actions, point)
    samples = tuple(
        NormalizedSample(timestamp, Decimal(value), Decimal("0"))
        for timestamp, value in ((1, "999"), (2, "1000"), (3, "1005"), (4, "995"), (5, "1004"), (6, "1003"))
    )
    equity = tuple(replace(sample, value=sample.value + Decimal("5"), upnl=Decimal("5")) for sample in samples)
    return SourceV6Fragment(
        2, "f" * 64, "s" * 64, "fixture.html", point, 0, 100,
        Decimal("1000"), Decimal("100"), Decimal("0"), "contract", "STITCHABLE_FIXED_LOT",
        actions, cycles, events, samples, equity, open_tail,
        {"Max Drawdown": declared_dd, "Max Drawdown, %": "5"},
    )


def _seam_pair():
    """Two coherent facts sets with a 96-hour report seam."""
    from mrs3.source_v6 import reconstruct_derived_facts

    base = _fragment()
    boundary = 96 * 3_600_000
    # Remove the fixture's deliberate open tail for this independent seam
    # reference: otherwise a tail entry could be paired with the next report's
    # leading realization by the round-trip state machine.
    first_actions = base.actions[:-1]
    first_cycles, first_events, first_open_tail = reconstruct_derived_facts(
        first_actions, base.point
    )
    first = replace(
        base,
        fragment_id="a" * 64,
        report_start_ms=0,
        report_end_ms=boundary,
        actions=first_actions,
        cycles=first_cycles,
        events=first_events,
        open_tail_cycle_ids=first_open_tail,
    )
    second_actions = tuple(
        replace(action, action_id=f"second-{action.action_id}", timestamp_ms=action.timestamp_ms + boundary)
        for action in first.actions
    )
    second_cycles, second_events, second_open_tail = reconstruct_derived_facts(
        second_actions, first.point
    )
    second = replace(
        first,
        fragment_id="e" * 64,
        source_name="second.html",
        report_start_ms=0,
        report_end_ms=boundary * 2,
        actions=second_actions,
        cycles=second_cycles,
        events=second_events,
        wallet_samples=tuple(
            replace(sample, timestamp_ms=sample.timestamp_ms + boundary)
            for sample in first.wallet_samples
        ),
        equity_samples=tuple(
            replace(sample, timestamp_ms=sample.timestamp_ms + boundary)
            for sample in first.equity_samples
        ),
        open_tail_cycle_ids=second_open_tail,
    )
    return first, second


def _three_trip_fragment():
    """One breakeven, one winning, and one losing completed position."""
    base = _fragment()
    template = base.actions[0]
    rows = (
        ("lead", 1, "decreased", "10", "0", "0", "0"),
        ("open", 2, "opened", "0", "50", "50", "50"),
        ("increase", 3, "increased", "0", "50", "100", "100"),
        ("partial", 4, "decreased", "1", "50", "50", "50"),
        ("close", 5, "closed", "-1", "50", "0", "0"),
        ("open2", 6, "opened", "0", "25", "25", "25"),
        ("close2", 7, "closed", "2", "25", "0", "0"),
        ("open3", 8, "opened", "0", "25", "25", "25"),
        ("close3", 9, "closed", "-3", "25", "0", "0"),
    )
    from mrs3.source_v6 import reconstruct_derived_facts

    actions = tuple(
        replace(
            template,
            action_id=action_id,
            timestamp_ms=timestamp,
            action=action,
            pnl=Decimal(pnl),
            balance=Decimal(balance),
            size=Decimal(size),
            post_size=Decimal(post_size),
        )
        for action_id, timestamp, action, pnl, size, post_size, balance in rows
    )
    cycles, events, open_tail = reconstruct_derived_facts(actions, base.point)
    return replace(
        base,
        actions=actions,
        cycles=cycles,
        events=events,
        open_tail_cycle_ids=open_tail,
    )


def test_stage2_round_trips_use_entry_realisation_runs_and_weighted_exposure() -> None:
    from mrs3.source_v6_stitch import calculate_metrics

    metrics = calculate_metrics((_fragment(),))

    assert metrics.total_trades == 2
    assert metrics.weighted_trades == Decimal("1.5")
    assert (metrics.win_trades, metrics.loss_trades) == (1, 1)
    assert metrics.win_rate_percent == Decimal("50")
    assert metrics.profit_factor == Decimal("12")
    assert len(metrics.round_trip_ids) == 2
    assert metrics.round_trip_ids == tuple(sorted(metrics.round_trip_ids))
    # Weighted exposure uses the peak of the reconstructed position/cycle,
    # while the two round trips are decisions inside that one position.
    assert metrics.round_trips[0].peak_position_size == Decimal("100")
    assert metrics.round_trips[0].weighted_trades == Decimal("0.5")
    assert metrics.round_trips[1].peak_position_size == Decimal("100")
    assert metrics.round_trips[1].weighted_trades == Decimal("1")


def test_stage2_breakeven_round_trip_does_not_dilute_win_rate() -> None:
    from mrs3.source_v6_stitch import calculate_metrics

    metrics = calculate_metrics((_three_trip_fragment(),))

    assert metrics.total_trades == 3
    assert (metrics.win_trades, metrics.loss_trades) == (1, 1)
    assert metrics.win_rate_percent == Decimal("50")


def test_stage2_seam_round_trips_match_independent_derived_reference() -> None:
    from mrs3.source_v6_stitch import calculate_metrics, derive_round_trips, resolve_ownership

    first, second = _seam_pair()
    decision = resolve_ownership(first, second)
    metrics = calculate_metrics((first, second))
    expected = derive_round_trips(
        first.actions + second.actions,
        first.cycles + second.cycles,
        first.point.canonical_key,
    )

    assert expected
    assert decision.overlap_hours == Decimal("96")
    assert metrics.total_trades == len(expected) == 4
    assert metrics.round_trip_ids == tuple(trip.round_trip_id for trip in expected)
    assert metrics.weighted_trades == sum(
        (trip.weighted_trades for trip in expected), Decimal("0")
    )
    assert all(
        action.timestamp_ms < second.report_end_ms
        for action in second.actions
    )


def test_stage2_full_and_later_window_pnl_use_raw_anchor_but_keep_publication_rebase() -> None:
    from mrs3.source_v6_stitch import calculate_metrics

    fragment = _fragment()
    full = calculate_metrics((fragment,))
    later = calculate_metrics((fragment,), start_ms=3, end_ms=100)

    assert full.total_pnl == Decimal("3")
    assert full.balance_series[0].value == fragment.initial_balance
    assert later.total_pnl == Decimal("-2")
    assert later.balance_series[0].value == Decimal("1006")
    # The interval filter applies before realization/PF derivation, so the
    # leading +10 action at t=1 cannot leak into the later window's PF.
    assert later.profit_factor == Decimal("2")


def test_stage2_drawdown_prefers_exact_declared_candidate_and_records_source() -> None:
    from mrs3.source_v6_stitch import calculate_metrics

    metrics = calculate_metrics((_fragment(declared_dd="50"),))

    assert metrics.max_equity_drawdown == Decimal("50")
    assert metrics.max_equity_drawdown_source == "DECLARED"


def test_stage2_exact_series_declared_drawdown_tie_still_prefers_declared() -> None:
    from mrs3.source_v6_stitch import calculate_metrics

    metrics = calculate_metrics((_fragment(declared_dd="10"),))

    assert metrics.max_equity_drawdown == Decimal("10")
    assert metrics.max_equity_drawdown_source == "DECLARED"
    assert metrics.max_equity_drawdown_declared_fragment_ids == ("f" * 64,)


def test_stage2_drawdown_uses_series_when_declared_candidate_is_smaller() -> None:
    from mrs3.source_v6_stitch import calculate_metrics

    metrics = calculate_metrics((_fragment(declared_dd="5"),))

    # M6 is max(merged series DD, admissible declared DD); DECLARED only wins
    # when it is the maximum, including an exact tie.
    assert metrics.max_equity_drawdown == Decimal("10")
    assert metrics.max_equity_drawdown_source == "SERIES"


def test_stage2_analysis_row_uses_canonical_decimal_audit_fields() -> None:
    from mrs3.source_v6_materializer import analysis_input_row
    from mrs3.source_v6_stitch import calculate_metrics

    fragment = _fragment()
    metrics = calculate_metrics((fragment,))
    row = analysis_input_row(fragment.point.canonical_key, fragment.point, metrics, (fragment,), (0, 100))

    assert row["weighted_trades"] == "1.5"
    assert row["max_equity_drawdown"] == "50"
    assert row["max_equity_drawdown_source"] == "DECLARED"
    assert isinstance(row["pnl_pct"], str)
    assert not any(isinstance(value, float) for value in row.values())


def test_stage2_row_validator_rejects_missing_extra_and_float_fields() -> None:
    from mrs3.source_v6_materializer import analysis_input_row
    from mrs3.source_v6_stitch import calculate_metrics
    from mrs3.source_v6_surface_fresh import _validate_analysis_row

    fragment = _fragment()
    row = analysis_input_row(fragment.point.canonical_key, fragment.point, calculate_metrics((fragment,)), (fragment,), (0, 100))
    for invalid in (
        {key: value for key, value in row.items() if key != "weighted_trades"},
        {**row, "extra": "x"},
        {**row, "pnl_pct": 1.0},
        {**row, "profit_factor": ""},
        {**row, "profit_factor": "+0"},
        {**row, "profit_factor": "-0"},
    ):
        try:
            _validate_analysis_row(invalid)
        except ValueError:
            continue
        raise AssertionError("invalid v2 analysis row was accepted")
