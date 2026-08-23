"""A cycle is one position, not one order.

`order_id` in this tester's reports identifies the fills of a single order. A
position opens under one order id and closes under another, so grouping cycles
by order id split every position in two: the closing half looked like a complete
cycle and the opening half like a position that never closed. The earliest of
those phantom open cycles then became the open-tail cutoff and hid every sample
of the report.

The report states the truth directly in `Post Size`: the position is open while
it is above zero and closed the moment it returns to zero.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "performance"


def _fragment(name: str):
    from mrs3.source_v6 import normalize_source_v6

    return normalize_source_v6((FIXTURES / name).read_bytes())


def test_a_position_spanning_two_order_ids_is_one_closed_cycle() -> None:
    """The exact shape of the real report: opened under 1, closed under 2."""
    fragment = _fragment("source_v6_position_across_orders.html")

    assert [action.order_id for action in fragment.actions] == ["1", "1", "2"]
    assert len(fragment.cycles) == 1
    cycle = fragment.cycles[0]
    assert cycle.closed, "a position that returned to zero size is closed"
    assert len(cycle.action_ids) == 3, "the open, the increase and the close"
    assert cycle.open_timestamp_ms < cycle.close_timestamp_ms
    assert cycle.realized_pnl == Decimal("3.1212")


def test_blank_order_id_uses_the_first_action_as_the_cycle_key() -> None:
    source = (FIXTURES / "source_v6_position_across_orders.html").read_bytes()
    source = source.replace(b"<td>1</td><td>opened", b"<td></td><td>opened").replace(
        b"<td>1</td><td>increased", b"<td></td><td>increased"
    ).replace(b"<td>2</td><td>closed", b"<td></td><td>closed")

    fragment = __import__("mrs3.source_v6", fromlist=["normalize_source_v6"]).normalize_source_v6(source)

    assert fragment.cycles[0].order_id == fragment.actions[0].action_id


def test_bridge_facts_include_every_action_in_a_cross_order_cycle() -> None:
    from dataclasses import replace
    from mrs3.source_v6_stitch import select_bridge_facts

    incoming = _fragment("source_v6_position_across_orders.html")
    outgoing = replace(incoming, cycles=(replace(incoming.cycles[0], close_timestamp_ms=None),))

    bridge = select_bridge_facts(outgoing, incoming)

    assert bridge.action_ids == incoming.cycles[0].action_ids
    assert bridge.event_ids == tuple(action.action_id for action in incoming.actions)


def test_a_position_still_open_at_the_report_end_stays_open() -> None:
    """The open tail the visibility filter exists for is a real one.

    This fixture ends with a position reduced to 0.5 rather than to zero, so the
    tail must survive the change: the filter still has something to protect.
    """
    fragment = _fragment("source_v6_fixed_lot_overlap_a.html")

    hanging = [cycle for cycle in fragment.cycles if not cycle.closed]
    assert len(hanging) == 1
    assert hanging[0].close_timestamp_ms is None
    assert len([cycle for cycle in fragment.cycles if cycle.closed]) == 2


def test_the_open_tail_does_not_swallow_the_history_before_it() -> None:
    """What the corpus needed: closed history survives a later open position."""
    from mrs3.source_v6_stitch import calculate_metrics

    metrics = calculate_metrics((_fragment("source_v6_fixed_lot_overlap_a.html"),))

    # The two leading closes have no entry in this report.  Stage 2 deliberately
    # excludes such orphan realisations instead of inventing positions for them.
    assert metrics.total_trades == 0
    assert metrics.round_trips == ()
    assert metrics.balance_series, "their samples are not hidden"


def test_existing_fixtures_report_no_phantom_open_cycles() -> None:
    """Every fixture that trades closes what it opens, by its own `Post Size`."""
    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html"):
        fragment = _fragment(name)
        for cycle in fragment.cycles:
            if not cycle.closed:
                last = max(
                    (item for item in fragment.actions if item.action_id in cycle.action_ids),
                    key=lambda item: item.timestamp_ms,
                )
                assert last.post_size is None or last.post_size != 0, (
                    f"{name}: a cycle whose position returned to zero is not open"
                )


def test_a_zero_activity_report_still_has_no_cycles() -> None:
    fragment = _fragment("source_v6_zero_activity.html")

    assert fragment.actions == () and fragment.cycles == ()
