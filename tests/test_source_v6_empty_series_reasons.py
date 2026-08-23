"""A series that became empty is not the same fact as a bot that did nothing.

The surfaces published on 2026-08-22 declared every one of their 4,746 and 5,472
combinations an empty result, because `measure_points` treated every
`SourceV6EmptySeriesError` as genuine zero activity. Only one of its causes is.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "performance"


def _traded():
    from mrs3.source_v6 import normalize_source_v6

    return normalize_source_v6((FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())


def _idle():
    from mrs3.source_v6 import normalize_source_v6

    return normalize_source_v6((FIXTURES / "source_v6_zero_activity.html").read_bytes())


def _with_open_cycle_at_first_sample(fragment):
    """The shape the real corpus had: a cycle left open at the first sample.

    Reproduces it from a fragment that really traded, so the resulting series is
    emptied by the open-tail filter while actions, events, closed cycles and
    samples all remain present.
    """
    from mrs3.source_v6 import NormalizedCycle, canonical_fragment_bytes
    from hashlib import sha256

    first = min(sample.timestamp_ms for sample in fragment.wallet_samples)
    hanging = NormalizedCycle(
        cycle_id="hanging-open-cycle",
        symbol=fragment.point.symbol,
        order_id="hanging",
        action_ids=(),
        open_timestamp_ms=first,
        close_timestamp_ms=None,
        realized_pnl=Decimal("0"),
        fees=Decimal("0"),
    )
    item = replace(fragment, cycles=(hanging, *fragment.cycles))
    return replace(item, fragment_id=sha256(canonical_fragment_bytes(item)).hexdigest())


def test_a_genuinely_idle_report_is_still_a_genuine_zero() -> None:
    """The one cause that may become a flat result keeps doing so."""
    from mrs3.source_v6_stitch import (
        GENUINE_ZERO_ACTIVITY,
        SourceV6EmptySeriesError,
        calculate_metrics,
    )

    with pytest.raises(SourceV6EmptySeriesError) as raised:
        calculate_metrics((_idle(),))
    assert raised.value.reason == GENUINE_ZERO_ACTIVITY


def test_an_open_tail_that_hides_everything_is_not_zero_activity() -> None:
    """The real defect: a full trading history reported as nothing at all."""
    from mrs3.source_v6_stitch import (
        OPEN_TAIL_FILTER_REMOVED_ALL_DATA,
        SourceV6EmptySeriesError,
        calculate_metrics,
    )

    fragment = _with_open_cycle_at_first_sample(_traded())
    assert fragment.wallet_samples and fragment.actions

    with pytest.raises(SourceV6EmptySeriesError) as raised:
        calculate_metrics((fragment,))
    assert raised.value.reason == OPEN_TAIL_FILTER_REMOVED_ALL_DATA
    # The diagnostic has to carry what the fragment actually held, otherwise the
    # panel can only say "0 candidates" again.
    assert raised.value.diagnostic["wallet_samples"] == len(fragment.wallet_samples)
    assert raised.value.diagnostic["events"] == len(fragment.events)
    assert raised.value.diagnostic["closed_cycles"] > 0
    assert raised.value.diagnostic["open_cycles"] > 0


def test_a_selected_window_that_hides_everything_says_so() -> None:
    """Distinct again: the data exists and the requested window cannot see it."""
    from mrs3.source_v6_stitch import (
        SourceV6EmptySeriesError,
        WINDOW_EXCLUDES_MEASURABLE_DATA,
        calculate_metrics,
    )

    fragment = _traded()
    with pytest.raises(SourceV6EmptySeriesError) as raised:
        calculate_metrics(
            (fragment,),
            start_ms=fragment.report_end_ms + 10_000_000,
            end_ms=fragment.report_end_ms + 20_000_000,
        )
    assert raised.value.reason == WINDOW_EXCLUDES_MEASURABLE_DATA


def test_only_genuine_zero_activity_becomes_a_flat_result() -> None:
    """`measure_points` must not publish a zero it cannot justify."""
    from mrs3.source_v6_stitch import (
        OPEN_TAIL_FILTER_REMOVED_ALL_DATA,
        SourceV6EmptySeriesError,
        measure_points,
    )

    idle = _idle()
    measured, empty = measure_points((idle,))
    assert [item["reason"] for item in empty] == ["NO_WALLET_OR_EQUITY_SAMPLES"]
    assert measured[idle.point.canonical_key].total_trades == 0

    fragment = _with_open_cycle_at_first_sample(_traded())
    with pytest.raises(SourceV6EmptySeriesError) as raised:
        measure_points((fragment,))
    assert raised.value.reason == OPEN_TAIL_FILTER_REMOVED_ALL_DATA
    assert fragment.point.canonical_key in str(raised.value)


def test_materialization_refuses_to_publish_a_false_zero() -> None:
    """The invariant the corpus violated: facts present, flat result published.

    A whole canonical grid where one cell was emptied by the open-tail cutoff.
    Before this, the grid published and that cell carried zeros; now the whole
    materialization stops and names the cell.
    """
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_stitch import (
        OPEN_TAIL_FILTER_REMOVED_ALL_DATA,
        SourceV6EmptySeriesError,
    )
    from tests.test_source_v6_empty_results import _grid_with_one_idle

    facts, _idle_key = _grid_with_one_idle()
    broken = _with_open_cycle_at_first_sample(facts[1])
    grid = (facts[0], broken, *facts[2:])
    point = broken.point
    scope = f"{point.symbol}|{point.side}|{point.timeframe}"

    with pytest.raises(SourceV6EmptySeriesError) as raised:
        materialize_source_v6(grid, (scope,))
    assert raised.value.reason == OPEN_TAIL_FILTER_REMOVED_ALL_DATA
    assert point.canonical_key in str(raised.value)
    assert raised.value.diagnostic["closed_cycles"] > 0
