"""Per-unit drawdown of one finalist over its own full history (ADR-0069)."""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import heapq
from typing import Any


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def own_history_unit_drawdown(samples: Sequence[Mapping[str, Any]], cycles: Sequence[Mapping[str, Any]]) -> Decimal:
    """Largest peak-to-trough fall of the summed per-basis equity path.

    Attribution mirrors ``prepare_weighted_input``: between two samples the
    interval is split at cycle openings and closings, the equity change sits
    on the last split segment, and it belongs to the lowest-index cycle open at
    that segment's midpoint, or else to a cycle opening exactly at its right
    end. It is divided by that cycle's ``source_basis``. Unlike the common
    grid, one-way admission is ignored, and a change with no owner or an
    unknown basis adds 0. The path starts at 0 and is summed without
    compounding.
    """
    points = sorted((
        (instant, Decimal(str(sample.get("equity", sample.get("value")))))
        for sample in samples
        if (instant := _instant(sample.get("timestamp_utc", sample.get("timestamp")))) is not None
        and sample.get("equity", sample.get("value")) is not None
    ), key=lambda point: point[0])
    opening_events = sorted(
        (opened, index) for index, cycle in enumerate(cycles) if (opened := _instant(cycle.get("opened_at"))) is not None
    )
    closing_events = sorted(
        (closed, index) for index, cycle in enumerate(cycles) if (closed := _instant(cycle.get("closed_at"))) is not None
    )
    event_times = sorted({event[0] for event in (*opening_events, *closing_events)})
    opening_at = {timestamp: index for timestamp, index in reversed(opening_events)}
    opening_cursor = closing_cursor = 0
    active_heap: list[int] = []
    closed_indices: set[int] = set()
    with localcontext() as context:
        context.prec = 50
        total = peak = drawdown = Decimal(0)
        for (left, left_value), (right, right_value) in zip(points, points[1:]):
            delta = right_value - left_value
            # Inner boundaries before ``right``; the change sits on the last segment.
            inner = event_times[bisect_right(event_times, left):bisect_right(event_times, right)]
            segment_start = max((value for value in inner if value < right), default=left)
            middle = segment_start + (right - segment_start) / 2
            while opening_cursor < len(opening_events) and opening_events[opening_cursor][0] <= middle:
                heapq.heappush(active_heap, opening_events[opening_cursor][1])
                opening_cursor += 1
            while closing_cursor < len(closing_events) and closing_events[closing_cursor][0] <= middle:
                closed_indices.add(closing_events[closing_cursor][1])
                closing_cursor += 1
            while active_heap and active_heap[0] in closed_indices:
                heapq.heappop(active_heap)
            if not delta:
                continue
            owner = active_heap[0] if active_heap else opening_at.get(right)
            if owner is None or cycles[owner].get("source_basis") is None:
                continue
            basis = Decimal(str(cycles[owner]["source_basis"]))
            if not basis.is_finite() or basis <= 0:
                continue
            total += delta / basis
            peak = max(peak, total)
            drawdown = max(drawdown, peak - total)
    return drawdown
