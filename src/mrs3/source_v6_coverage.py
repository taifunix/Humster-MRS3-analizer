"""Coverage and exact UTC-day gap helpers for Source v6 fragments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import csv
import io
import json
from typing import Iterable, Sequence

from .config import DEFAULT_CANONICAL_SHIFTS_BP
from .source_v6 import SourceV6Fragment


CANONICAL_READINESS_CLOSE_LENGTHS = tuple(range(2, 8))
CANONICAL_READINESS_SHIFTS_BP = DEFAULT_CANONICAL_SHIFTS_BP


@dataclass(frozen=True, slots=True)
class CoverageCell:
    point_key: str
    utc_day: date
    status: str


@dataclass(frozen=True, slots=True)
class ReadyInterval:
    # Ready is a data-availability contract per Pair+Side+TF.  MA/shift
    # variants of the same execution scope therefore contribute to one
    # interval rather than producing misleading point-specific intervals.
    point_key: str
    start: date
    end: date

    @property
    def scope_key(self) -> str:
        return self.point_key


def _days(start_ms: int, end_ms: int) -> tuple[date, ...]:
    current = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc).date()
    result: list[date] = []
    while current < end:
        result.append(current)
        current += timedelta(days=1)
    return tuple(result)


def coverage_cells(fragments: Sequence[SourceV6Fragment]) -> tuple[CoverageCell, ...]:
    cells: dict[tuple[str, date], CoverageCell] = {}
    for fragment in fragments:
        for day in _days(fragment.report_start_ms, fragment.report_end_ms):
            cells[(fragment.point.canonical_key, day)] = CoverageCell(fragment.point.canonical_key, day, "READY")
    return tuple(sorted(cells.values(), key=lambda cell: (cell.point_key, cell.utc_day)))


def missing_cells(fragments: Sequence[SourceV6Fragment], *, start: date, end: date, point_keys: Iterable[str] | None = None, symbols: Iterable[str] | None = None, timeframes: Iterable[str] | None = None, sides: Iterable[str] | None = None) -> tuple[CoverageCell, ...]:
    keys = tuple(sorted(point_keys or {fragment.point.canonical_key for fragment in fragments}))
    symbol_filter = set(symbols or ())
    timeframe_filter = set(timeframes or ())
    side_filter = set(sides or ())
    if symbol_filter or timeframe_filter or side_filter:
        allowed = {
            fragment.point.canonical_key
            for fragment in fragments
            if (not symbol_filter or fragment.point.symbol in symbol_filter)
            and (not timeframe_filter or fragment.point.timeframe in timeframe_filter)
            and (not side_filter or fragment.point.side in side_filter)
        }
        keys = tuple(key for key in keys if key in allowed)
    covered = {(cell.point_key, cell.utc_day) for cell in coverage_cells(fragments)}
    result = []
    current = start
    while current < end:
        for key in keys:
            if (key, current) not in covered:
                result.append(CoverageCell(key, current, "MISSING"))
        current += timedelta(days=1)
    return tuple(result)


def ready_intervals(
    fragments: Sequence[SourceV6Fragment],
    *,
    required_shifts: Iterable[int] | None = None,
    required_close_lengths: Iterable[int] | None = None,
) -> tuple[ReadyInterval, ...]:
    """Return continuous bounds per Pair+Side+TF.

    When the inherited readiness grid is supplied, a day is READY only when
    every required shift/CloseMA point variant covers that day.  The default
    remains a raw coverage view for import diagnostics.
    """
    if (required_shifts is None) != (required_close_lengths is None):
        raise ValueError("required_shifts and required_close_lengths must be supplied together")
    if required_shifts is not None and required_close_lengths is not None:
        expected = {(fragment.point.shift_bp, fragment.point.close_ma_length) for fragment in fragments}
        required = {(int(shift), int(close)) for shift in required_shifts for close in required_close_lengths}
        if not required:
            raise ValueError("readiness grid cannot be empty")
        if not expected.issuperset(required):
            return ()
        by_variant: dict[tuple[str, int, int], set[date]] = {}
        for fragment in fragments:
            scope = f"{fragment.point.symbol}|{fragment.point.side}|{fragment.point.timeframe}"
            key = (scope, fragment.point.shift_bp, fragment.point.close_ma_length)
            by_variant.setdefault(key, set()).update(_days(fragment.report_start_ms, fragment.report_end_ms))
        scopes = sorted({scope for scope, _shift, _close in by_variant})
        grouped: dict[str, list[date]] = {}
        for scope in scopes:
            common: set[date] | None = None
            for shift, close in required:
                days = by_variant.get((scope, shift, close), set())
                common = set(days) if common is None else common & days
            grouped[scope] = sorted(common or set())
    else:
        grouped = {}
        for fragment in fragments:
            scope = f"{fragment.point.symbol}|{fragment.point.side}|{fragment.point.timeframe}"
            grouped.setdefault(scope, []).extend(_days(fragment.report_start_ms, fragment.report_end_ms))
    intervals: list[ReadyInterval] = []
    for key, days in grouped.items():
        ordered = sorted(set(days))
        if not ordered:
            continue
        start = previous = ordered[0]
        for current in ordered[1:]:
            if current != previous + timedelta(days=1):
                intervals.append(ReadyInterval(key, start, previous))
                start = current
            previous = current
        intervals.append(ReadyInterval(key, start, previous))
    return tuple(sorted(intervals, key=lambda item: (item.point_key, item.start)))


def coverage_csv(cells: Sequence[CoverageCell]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("point_key", "utc_day", "status"))
    for cell in sorted(cells, key=lambda item: (item.point_key, item.utc_day, item.status)):
        writer.writerow((cell.point_key, cell.utc_day.isoformat(), cell.status))
    return output.getvalue().encode("utf-8")


def coverage_json(cells: Sequence[CoverageCell]) -> bytes:
    payload = [{"point_key": cell.point_key, "utc_day": cell.utc_day.isoformat(), "status": cell.status} for cell in sorted(cells, key=lambda item: (item.point_key, item.utc_day, item.status))]
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def select_ready_interval(
    intervals: Sequence[ReadyInterval],
    *,
    scope_key: str,
    start: date,
    end: date,
) -> ReadyInterval:
    """Validate a user-selected half-open interval inside one READY range."""
    if end <= start:
        raise ValueError("selected interval end must be after start")
    for interval in intervals:
        if interval.scope_key == scope_key and interval.start <= start and end - timedelta(days=1) <= interval.end:
            return ReadyInterval(scope_key, start, end - timedelta(days=1))
    raise ValueError("selected interval is outside READY bounds")


def canonical_ready_intervals(fragments: Sequence[SourceV6Fragment]) -> tuple[ReadyInterval, ...]:
    # Contract: publish one stable selectable range per scope; ignore hidden,
    # degenerate, and shorter duplicate candidates.
    """Evaluate the inherited six-CloseMA × nineteen-Shift contract."""
    from .duckdb_direct import canonical_coverage_from_rows
    rows = [{
        "report_id": fragment.fragment_id,
        "source_hash": fragment.source_sha256,
        "canonical_point_key": fragment.point.canonical_key,
        "symbol": fragment.point.symbol,
        "side": fragment.point.side,
        "timeframe": fragment.point.timeframe,
        "shift_bp": fragment.point.shift_bp,
        "open_ma_len": fragment.point.open_ma_length,
        "close_ma_len": fragment.point.close_ma_length,
        "report_period_start_ms": fragment.report_start_ms,
        "report_period_end_ms": fragment.report_end_ms,
        "start_timestamp_ms": fragment.report_start_ms,
        "end_timestamp_ms": fragment.report_end_ms,
    } for fragment in fragments]
    coverage = canonical_coverage_from_rows(rows)
    result_by_scope: dict[str, ReadyInterval] = {}
    for interval in coverage.intervals:
        if not interval.selectable:
            continue
        start = datetime.fromisoformat(interval.start_utc).astimezone(timezone.utc).date()
        end = datetime.fromisoformat(interval.end_utc).astimezone(timezone.utc).date() - timedelta(days=1)
        if end < start:
            continue
        scope = f"{interval.scope.symbol}|{interval.scope.side}|{interval.scope.timeframe}"
        candidate = ReadyInterval(scope, start, end)
        previous = result_by_scope.get(scope)
        # Equal spans choose the earliest start for stable readiness bounds.
        if previous is None or (candidate.end - candidate.start, -candidate.start.toordinal()) > (previous.end - previous.start, -previous.start.toordinal()):
            result_by_scope[scope] = candidate
    return tuple(sorted(result_by_scope.values(), key=lambda item: (item.point_key, item.start, item.end)))
