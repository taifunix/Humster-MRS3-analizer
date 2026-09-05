"""RAM-only five-second scheduling and ``liquidity_1m`` aggregation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil, floor, isfinite
from typing import Any


SAMPLE_INTERVAL_MS = 5_000
BANDS_BPS = (10, 25, 50, 100)

_BASE_COLUMNS = (
    "minute_ts_ms",
    "symbol",
    "sample_count",
    "valid_sample_count",
    "coverage_ratio",
    "book_reset_count",
    "ws_connected_ratio",
    "active_sample_target",
    "mid_median",
    "spread_bps_median",
    "spread_bps_p95",
    "spread_bps_max",
)
_DEPTH_COLUMNS = tuple(
    name
    for band in BANDS_BPS
    for name in (
        f"bid_depth_usdt_{band}bps_p05",
        f"bid_depth_usdt_{band}bps_median",
        f"ask_depth_usdt_{band}bps_p05",
        f"ask_depth_usdt_{band}bps_median",
    )
)
_COMPLETE_COLUMNS = tuple(f"depth_{band}bps_complete_ratio" for band in BANDS_BPS)
LIQUIDITY_1M_COLUMNS = _BASE_COLUMNS + _DEPTH_COLUMNS + _COMPLETE_COLUMNS

# (name, DuckDB type, nullable) is the archive's single source of schema truth.
LIQUIDITY_1M_SCHEMA = (
    ("minute_ts_ms", "BIGINT", False),
    ("symbol", "VARCHAR", False),
    ("sample_count", "SMALLINT", False),
    ("valid_sample_count", "SMALLINT", False),
    ("coverage_ratio", "DOUBLE", True),
    ("book_reset_count", "SMALLINT", False),
    ("ws_connected_ratio", "DOUBLE", True),
    ("active_sample_target", "SMALLINT", False),
    ("mid_median", "DOUBLE", True),
    ("spread_bps_median", "DOUBLE", True),
    ("spread_bps_p95", "DOUBLE", True),
    ("spread_bps_max", "DOUBLE", True),
    ("bid_depth_usdt_10bps_p05", "DOUBLE", True),
    ("bid_depth_usdt_10bps_median", "DOUBLE", True),
    ("ask_depth_usdt_10bps_p05", "DOUBLE", True),
    ("ask_depth_usdt_10bps_median", "DOUBLE", True),
    ("bid_depth_usdt_25bps_p05", "DOUBLE", True),
    ("bid_depth_usdt_25bps_median", "DOUBLE", True),
    ("ask_depth_usdt_25bps_p05", "DOUBLE", True),
    ("ask_depth_usdt_25bps_median", "DOUBLE", True),
    ("bid_depth_usdt_50bps_p05", "DOUBLE", True),
    ("bid_depth_usdt_50bps_median", "DOUBLE", True),
    ("ask_depth_usdt_50bps_p05", "DOUBLE", True),
    ("ask_depth_usdt_50bps_median", "DOUBLE", True),
    ("bid_depth_usdt_100bps_p05", "DOUBLE", True),
    ("bid_depth_usdt_100bps_median", "DOUBLE", True),
    ("ask_depth_usdt_100bps_p05", "DOUBLE", True),
    ("ask_depth_usdt_100bps_median", "DOUBLE", True),
    ("depth_10bps_complete_ratio", "DOUBLE", True),
    ("depth_25bps_complete_ratio", "DOUBLE", True),
    ("depth_50bps_complete_ratio", "DOUBLE", True),
    ("depth_100bps_complete_ratio", "DOUBLE", True),
)
if tuple(name for name, _type, _nullable in LIQUIDITY_1M_SCHEMA) != LIQUIDITY_1M_COLUMNS:
    raise RuntimeError("liquidity_1m schema and columns are inconsistent")


def quantile(values: Sequence[float], probability: float) -> float | None:
    """Return the spec's linearly interpolated quantile."""

    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be between 0 and 1")
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    h = (len(ordered) - 1) * probability
    lower = floor(h)
    upper = ceil(h)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (h - lower) * (ordered[upper] - ordered[lower])


def clamp_missed_boundaries(count: int, remaining_capacity: int) -> int:
    """Clamp unattributed misses to the current minute's remaining capacity."""

    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("missed boundary count must be a non-negative integer")
    if (
        not isinstance(remaining_capacity, int)
        or isinstance(remaining_capacity, bool)
        or remaining_capacity < 0
    ):
        raise ValueError("remaining capacity must be a non-negative integer")
    return min(count, remaining_capacity)


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    """One scheduler poll.

    ``missed_boundaries`` is the authoritative count.  A normal late poll
    additionally reports the compact inclusive range of missed wall-clock
    boundaries.  The count emitted on a discontinuity transition is
    authoritative and has no timestamp range.  After a backward jump past an
    emitted watermark, wall labels are suppressed until the wall clock catches
    the next watermark-derived label; monotonic cadence remains bounded to one
    interval and may emit only the newest due label.  Skipped slots in that
    trusted watermark-derived cadence may have a compact range, while callers
    still attribute the discontinuity count to the active interval or
    clock-fault record.  Normal ranges can support minute partitioning.
    """

    due_boundaries: tuple[int, ...]
    missed_boundaries: int
    clock_discontinuity: bool
    reanchored: bool
    next_boundary_ms: int
    next_boundary_monotonic_ms: int
    missed_boundary_range_ms: tuple[int, int] | None = None


class FiveSecondScheduler:
    """Pure clock adapter; sleeping belongs to the caller.

    Missed-boundary counts are authoritative.  The count on a discontinuity
    transition has no synthetic timestamp.  A backward jump beyond an emitted
    boundary enters wall-stale mode: wall labels are suppressed until the wall
    clock catches the next label, while monotonic cadence stays bounded by
    ``interval_ms`` and never replays the watermark.  Skipped slots in the
    trusted watermark-derived cadence may have a compact range.  Callers must
    partition normal missed counts to each minute when exact attribution is
    available. ``MinuteAggregator`` clamps unattributed misses to remaining
    capacity; discontinuity counts still cannot be bucketed automatically.
    """

    def __init__(
        self,
        *,
        interval_ms: int = SAMPLE_INTERVAL_MS,
        clock_tolerance_ms: int = 1_000,
    ) -> None:
        if not isinstance(interval_ms, int) or isinstance(interval_ms, bool):
            raise ValueError("interval_ms must be an integer")
        if not isinstance(clock_tolerance_ms, int) or isinstance(clock_tolerance_ms, bool):
            raise ValueError("clock_tolerance_ms must be an integer")
        if interval_ms <= 0 or clock_tolerance_ms < 0:
            raise ValueError("interval_ms must be positive and tolerance non-negative")
        self.interval_ms = interval_ms
        self.clock_tolerance_ms = clock_tolerance_ms
        self._anchor_wall_ms: int | None = None
        self._anchor_monotonic_ms: int | None = None
        self._next_boundary_ms: int | None = None
        self._next_boundary_monotonic_ms: int | None = None
        self._last_emitted_boundary_ms: int | None = None
        self._wall_stale = False

    def poll(self, wall_ms: int, monotonic_ms: int) -> SchedulerResult:
        wall_ms = _clock_value(wall_ms, "wall_ms")
        monotonic_ms = _clock_value(monotonic_ms, "monotonic_ms")
        if self._anchor_wall_ms is None:
            self._reanchor(wall_ms, monotonic_ms)
            return self._result((), 0, None, False, True)

        if self._wall_stale:
            assert self._next_boundary_ms is not None
            assert self._next_boundary_monotonic_ms is not None
            if wall_ms < self._next_boundary_ms:
                if monotonic_ms >= self._next_boundary_monotonic_ms:
                    steps = (
                        (monotonic_ms - self._next_boundary_monotonic_ms)
                        // self.interval_ms
                    ) + 1
                    first_due = self._next_boundary_ms
                    newest_due = first_due + (steps - 1) * self.interval_ms
                    missed = steps - 1
                    due = (newest_due,)
                    self._next_boundary_ms = newest_due + self.interval_ms
                    self._last_emitted_boundary_ms = newest_due
                    self._next_boundary_monotonic_ms = monotonic_ms + self.interval_ms
                    missed_range = (
                        (first_due, newest_due - self.interval_ms)
                        if missed
                        else None
                    )
                else:
                    due = ()
                    missed = 0
                    missed_range = None
                self._anchor_wall_ms = wall_ms
                self._anchor_monotonic_ms = monotonic_ms
                return self._result(due, missed, missed_range, False, False)
            self._wall_stale = False
            fault_transition = True
        else:
            fault_transition = False

        wall_delta = wall_ms - self._anchor_wall_ms
        monotonic_delta = monotonic_ms - self._anchor_monotonic_ms
        discontinuity = not fault_transition and (
            monotonic_delta < 0
            or wall_delta < 0
            or abs(wall_delta - monotonic_delta) > self.clock_tolerance_ms
        )
        if discontinuity:
            missed_count = max(0, monotonic_delta // self.interval_ms)
            self._reanchor(wall_ms, monotonic_ms)
            return self._result((), missed_count, None, True, True)

        assert self._next_boundary_ms is not None
        if self._last_emitted_boundary_ms is not None:
            self._next_boundary_ms = max(
                self._next_boundary_ms,
                self._last_emitted_boundary_ms + self.interval_ms,
            )
        if self._next_boundary_ms <= wall_ms:
            steps = (wall_ms - self._next_boundary_ms) // self.interval_ms + 1
            newest_due = self._next_boundary_ms + (steps - 1) * self.interval_ms
            missed = steps - 1
            missed_range = (
                (self._next_boundary_ms, newest_due - self.interval_ms)
                if missed
                else None
            )
            due = (newest_due,)
            self._next_boundary_ms = newest_due + self.interval_ms
            self._last_emitted_boundary_ms = newest_due
        else:
            due = ()
            missed = 0
            missed_range = None
        self._anchor_wall_ms = wall_ms
        self._anchor_monotonic_ms = monotonic_ms
        self._next_boundary_monotonic_ms = monotonic_ms + max(
            0, self._next_boundary_ms - wall_ms
        )
        return self._result(due, missed, missed_range, fault_transition, False)

    def next_wait_ms(self, monotonic_ms: int) -> int | None:
        """Return a non-blocking wait duration based on the monotonic clock."""

        monotonic_ms = _clock_value(monotonic_ms, "monotonic_ms")
        if self._next_boundary_monotonic_ms is None:
            return None
        return max(0, self._next_boundary_monotonic_ms - monotonic_ms)

    def _reanchor(self, wall_ms: int, monotonic_ms: int) -> None:
        self._anchor_wall_ms = wall_ms
        self._anchor_monotonic_ms = monotonic_ms
        wall_next = (wall_ms // self.interval_ms + 1) * self.interval_ms
        wall_derived_next = wall_next
        watermark_next = None
        if self._last_emitted_boundary_ms is not None:
            watermark_next = self._last_emitted_boundary_ms + self.interval_ms
            wall_next = max(wall_next, watermark_next)
        self._next_boundary_ms = wall_next
        self._wall_stale = (
            watermark_next is not None and watermark_next > wall_derived_next
        )
        self._next_boundary_monotonic_ms = monotonic_ms + (
            self.interval_ms
            if self._wall_stale
            else max(0, self._next_boundary_ms - wall_ms)
        )

    def _result(
        self,
        due: tuple[int, ...],
        missed: int,
        missed_range: tuple[int, int] | None,
        discontinuity: bool,
        reanchored: bool,
    ) -> SchedulerResult:
        assert self._next_boundary_ms is not None
        assert self._next_boundary_monotonic_ms is not None
        return SchedulerResult(
            due_boundaries=due,
            missed_boundaries=missed,
            clock_discontinuity=discontinuity,
            reanchored=reanchored,
            next_boundary_ms=self._next_boundary_ms,
            next_boundary_monotonic_ms=self._next_boundary_monotonic_ms,
            missed_boundary_range_ms=missed_range,
        )


@dataclass(frozen=True, slots=True)
class MarketSample:
    """A caller-owned snapshot of one attempted boundary.

    ``reset_count`` is the reset delta observed at this boundary, not the
    cumulative ``OrderBook.book_reset_count`` value.
    """

    local_timestamp_ms: int
    bids: Mapping[Any, Any]
    asks: Mapping[Any, Any]
    book_valid: bool
    ws_connected: bool
    reset_count: int = 0

    @property
    def timestamp_ms(self) -> int:
        return self.local_timestamp_ms

    @property
    def valid(self) -> bool:
        return self.book_valid

    @property
    def book_reset_count(self) -> int:
        return self.reset_count


Sample = MarketSample
BookSample = MarketSample


class MinuteAggregator:
    """Collect boundary attempts and emit one ordered, nullable row.

    The caller owns timestamp-to-minute membership and should partition
    ``SchedulerResult.missed_boundaries`` when its range is available. A
    discontinuity count has no timestamp range and cannot be bucketed here;
    ``record_missed_boundary`` safely clamps it to the minute's remaining
    capacity while sample additions still reject overflow.
    """

    def __init__(self, symbol: str, minute_ts_ms: int, active_sample_target: int) -> None:
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        if not isinstance(minute_ts_ms, int) or isinstance(minute_ts_ms, bool):
            raise ValueError("minute_ts_ms must be an integer")
        if not isinstance(active_sample_target, int) or isinstance(active_sample_target, bool):
            raise ValueError("active_sample_target must be an integer")
        if active_sample_target < 0:
            raise ValueError("active_sample_target must be non-negative")
        self.symbol = symbol
        self.minute_ts_ms = minute_ts_ms
        self.active_sample_target = active_sample_target
        self._sample_count = 0
        self._valid_sample_count = 0
        self._connected_count = 0
        self._book_reset_count = 0
        self._missed_boundary_count = 0
        self._mids: list[float] = []
        self._spreads: list[float] = []
        self._depth: dict[int, dict[str, list[float]]] = {
            band: {"bid": [], "ask": []} for band in BANDS_BPS
        }
        self._complete: dict[int, int] = {band: 0 for band in BANDS_BPS}

    @property
    def missed_boundary_count(self) -> int:
        return self._missed_boundary_count

    def add_sample(self, sample: MarketSample) -> None:
        if not isinstance(sample, MarketSample):
            raise ValueError("sample must be a MarketSample")
        reset_count = _nonnegative_count(sample.reset_count, "reset_count")
        self._ensure_capacity(1)
        self._sample_count += 1
        if sample.ws_connected:
            self._connected_count += 1
        self._book_reset_count += reset_count
        book = _valid_book(sample)
        if book is None:
            return
        bids, asks, best_bid, best_ask = book
        self._valid_sample_count += 1
        mid = (best_bid + best_ask) / 2.0
        spread = (best_ask - best_bid) / mid * 10_000.0
        self._mids.append(mid)
        self._spreads.append(spread)
        for band in BANDS_BPS:
            bid_depth, bid_complete = _depth(bids, mid, band, "bid")
            ask_depth, ask_complete = _depth(asks, mid, band, "ask")
            self._depth[band]["bid"].append(bid_depth)
            self._depth[band]["ask"].append(ask_depth)
            if bid_complete and ask_complete:
                self._complete[band] += 1

    def record_boundary(self, sample: MarketSample | None) -> None:
        if sample is None:
            self.record_missed_boundary()
        else:
            self.add_sample(sample)

    def record_missed_boundary(self, count: int = 1) -> None:
        remaining = self.active_sample_target - self._sample_count - self._missed_boundary_count
        # ponytail: unattributed misses saturate; partition by timestamp for exact attribution.
        self._missed_boundary_count += clamp_missed_boundaries(count, remaining)

    def _ensure_capacity(self, count: int) -> None:
        if self._sample_count + self._missed_boundary_count + count > self.active_sample_target:
            raise ValueError("active sample target would be exceeded")

    def finalize(self) -> dict[str, Any] | None:
        if self.active_sample_target == 0:
            return None
        target = self.active_sample_target
        valid = self._valid_sample_count
        row: dict[str, Any] = {
            "minute_ts_ms": self.minute_ts_ms,
            "symbol": self.symbol,
            "sample_count": self._sample_count,
            "valid_sample_count": valid,
            "coverage_ratio": valid / target,
            "book_reset_count": self._book_reset_count,
            "ws_connected_ratio": self._connected_count / target,
            "active_sample_target": target,
            "mid_median": quantile(self._mids, 0.5),
            "spread_bps_median": quantile(self._spreads, 0.5),
            "spread_bps_p95": quantile(self._spreads, 0.95),
            "spread_bps_max": max(self._spreads) if self._spreads else None,
        }
        for band in BANDS_BPS:
            values = self._depth[band]
            row[f"bid_depth_usdt_{band}bps_p05"] = quantile(values["bid"], 0.05)
            row[f"bid_depth_usdt_{band}bps_median"] = quantile(values["bid"], 0.5)
            row[f"ask_depth_usdt_{band}bps_p05"] = quantile(values["ask"], 0.05)
            row[f"ask_depth_usdt_{band}bps_median"] = quantile(values["ask"], 0.5)
        for band in BANDS_BPS:
            row[f"depth_{band}bps_complete_ratio"] = (
                self._complete[band] / valid if valid else None
            )
        return row


def _clock_value(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def _nonnegative_count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _valid_book(sample: MarketSample) -> tuple[dict[float, float], dict[float, float], float, float] | None:
    if not sample.book_valid or not isinstance(sample.bids, Mapping) or not isinstance(sample.asks, Mapping):
        return None
    try:
        bids = _levels(sample.bids)
        asks = _levels(sample.asks)
    except (TypeError, ValueError):
        return None
    if not bids or not asks:
        return None
    best_bid = max(bids)
    best_ask = min(asks)
    if (
        not isfinite(best_bid)
        or not isfinite(best_ask)
        or best_bid <= 0
        or best_ask <= 0
        or best_bid >= best_ask
    ):
        return None
    return bids, asks, best_bid, best_ask


def _levels(levels: Mapping[Any, Any]) -> dict[float, float]:
    parsed: dict[float, float] = {}
    for raw_price, raw_quantity in levels.items():
        try:
            price = float(raw_price)
            quantity = float(raw_quantity)
        except (TypeError, ValueError, OverflowError):
            continue
        if not isfinite(price) or not isfinite(quantity) or price <= 0 or quantity <= 0:
            continue
        parsed[price] = quantity
    return parsed


def _depth(
    levels: Mapping[float, float], mid: float, band: int, side: str
) -> tuple[float, bool]:
    distance = band / 10_000.0
    lower = mid * (1.0 - distance)
    upper = mid * (1.0 + distance)
    if side == "bid":
        in_band = [price for price in levels if lower <= price <= mid]
        complete = min(levels) <= lower
    else:
        in_band = [price for price in levels if mid <= price <= upper]
        complete = max(levels) >= upper
    return sum(price * levels[price] for price in in_band), complete


__all__ = [
    "BANDS_BPS",
    "LIQUIDITY_1M_COLUMNS",
    "LIQUIDITY_1M_SCHEMA",
    "SAMPLE_INTERVAL_MS",
    "BookSample",
    "FiveSecondScheduler",
    "MarketSample",
    "MinuteAggregator",
    "Sample",
    "SchedulerResult",
    "clamp_missed_boundaries",
    "quantile",
]
