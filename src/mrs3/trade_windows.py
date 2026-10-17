"""Window metrics of MRS2 points from the trade store: runs of consecutive whole segments, all points at once.

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 2 contract», window metrics).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pyarrow as pa

from .mrs2_simulator import DAY_MS, HOUR_MS, month_starts
from .trade_store import PRICE_POINT, TradeStore, TradeStoreError


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    """Per-point metrics of one window; every array is aligned with `point_ids` (ascending)."""

    point_ids: np.ndarray
    start_ms: int
    end_ms: int
    lot: float
    final_balance: np.ndarray
    trades: np.ndarray
    wins: np.ndarray
    gross_profit: np.ndarray
    gross_loss: np.ndarray
    fees: np.ndarray
    max_drawdown_percent: np.ndarray
    ulcer_index: np.ndarray
    monthly_returns: Mapping[str, np.ndarray]

    @property
    def days(self) -> float:
        return (self.end_ms - self.start_ms) / DAY_MS

    @property
    def pnl_percent(self) -> np.ndarray:
        return (self.final_balance - self.lot) / self.lot * 100


def _check_run(segments: Sequence[Mapping]) -> None:
    if not segments:
        raise TradeStoreError("a window needs at least one segment")
    first = segments[0]
    for previous, segment in zip(segments, segments[1:]):
        if previous["end_ms"] != segment["start_ms"]:
            raise TradeStoreError(f"segments are not consecutive: {previous['segment_id']} -> {segment['segment_id']}")
    for segment in segments:
        if (segment["exchange"], segment["symbol"], segment["source"]) != (first["exchange"], first["symbol"],
                                                                        first["source"]):
            raise TradeStoreError("a window takes segments of one pair and source")


def _point_matrix(hourly: pa.Table, point_ids: np.ndarray, hours: int, name: str) -> np.ndarray:
    pids = hourly.column("point_id").to_numpy()
    keep = pids != PRICE_POINT
    values = hourly.column(name).to_numpy()[keep]
    if len(values) != len(point_ids) * hours:
        raise TradeStoreError("hourly rows do not cover every point and hour of the segment")
    matrix = values.reshape(len(point_ids), hours)
    if name == "point_id" and not np.array_equal(matrix[:, 0], point_ids):
        raise TradeStoreError("hourly rows are not ordered like the metrics rows")
    return matrix


def compute_window(parts: Sequence[tuple[Mapping, pa.Table, pa.Table]], lot: float = 10_000.0) -> WindowMetrics:
    """Window metrics from (segment record, metrics table, hourly table) of consecutive whole segments."""
    segments = [segment for segment, _, _ in parts]
    _check_run(segments)
    start_ms, end_ms = int(segments[0]["start_ms"]), int(segments[-1]["end_ms"])
    point_ids = parts[0][1].column("point_id").to_numpy()
    sums = {name: np.zeros(len(point_ids)) for name in ("trades", "wins", "gross_profit", "gross_loss", "fees")}
    offset = np.zeros(len(point_ids))
    final = None                      # summed from the first segment's balance: one segment gives its exact final
    last_action = np.full(len(point_ids), -1, dtype=np.int64)
    closes, lows, highs, drawdowns = [], [], [], []
    marked_last = None
    for segment, metrics, hourly in parts:
        if not np.array_equal(metrics.column("point_id").to_numpy(), point_ids):
            raise TradeStoreError(f"segment {segment['segment_id']} has another point set")
        hours = (int(segment["end_ms"]) - int(segment["start_ms"])) // HOUR_MS
        _point_matrix(hourly, point_ids, hours, "point_id")
        low_own = _point_matrix(hourly, point_ids, hours, "low").astype(np.float64)
        high_own = _point_matrix(hourly, point_ids, hours, "high").astype(np.float64)
        closes.append(_point_matrix(hourly, point_ids, hours, "close").astype(np.float64) + offset[:, None])
        lows.append(low_own + offset[:, None])
        highs.append(high_own + offset[:, None])
        # the intra-hour drawdown (m − v) / m was measured on the segment's own scale; on the shifted curve it is
        # (m − v) / (m + offset), bounded tightly from above by the hour's high (offset > 0) or low (offset < 0)
        shift = offset[:, None]
        reference = np.where(shift >= 0, high_own, low_own)
        denominator = reference + shift
        scale = np.where(denominator > 0, reference / np.where(denominator > 0, denominator, 1.0), 1.0)
        drawdowns.append(_point_matrix(hourly, point_ids, hours, "drawdown").astype(np.float64) * scale)
        for name in sums:
            sums[name] += metrics.column(name).to_numpy()
        traded = metrics.column("trades").to_numpy() > 0
        last_action = np.where(traded, metrics.column("last_ms").to_numpy(), last_action)
        marked_last = metrics.column("marked_final").to_numpy() + offset
        segment_final = metrics.column("final_balance").to_numpy()
        final = segment_final.copy() if final is None else final + (segment_final - lot)
        offset = final - lot
    close, low, high, drawdown = (np.concatenate(item, axis=1) for item in (closes, lows, highs, drawdowns))

    # MaxDD%: the peak before each hour against the hour's low, or the drawdown inside the hour
    peak_before = np.maximum.accumulate(np.concatenate([np.full((len(point_ids), 1), lot), high[:, :-1]], axis=1),
                                        axis=1)
    cross = (peak_before - low) / peak_before * 100
    max_dd = np.clip(np.maximum(cross.max(axis=1), drawdown.max(axis=1)), 0.0, None)

    # Ulcer: hourly closes from the first hour end through the hour of the last curve point
    hour_ends = start_ms + HOUR_MS * np.arange(1, close.shape[1] + 1, dtype=np.int64)
    limit = np.minimum(end_ms, (last_action // HOUR_MS + 1) * HOUR_MS)
    included = (hour_ends[None, :] <= limit[:, None]) & (last_action[:, None] >= 0)
    peaks = np.maximum.accumulate(close, axis=1)
    squares = ((peaks - close) / peaks * 100) ** 2
    count = included.sum(axis=1)
    ulcer = np.where(count > 0, np.sqrt(np.where(included, squares, 0.0).sum(axis=1) / np.maximum(count, 1)), 0.0)

    # monthly: month-end margin balance against the previous month end; the last month ends marked
    months = month_starts(start_ms, end_ms)
    monthly: dict[str, np.ndarray] = {}
    previous = np.full(len(point_ids), lot)
    for index, (key, _) in enumerate(months):
        if index + 1 < len(months):
            column = (months[index + 1][1] - start_ms) // HOUR_MS - 1
            value = close[:, column] if column >= 0 else np.full(len(point_ids), lot)
        else:
            value = marked_last
        monthly[key] = (value / previous - 1) * 100
        previous = value
    return WindowMetrics(point_ids, start_ms, end_ms, lot, final, sums["trades"].astype(np.int64),
                         sums["wins"].astype(np.int64), sums["gross_profit"], sums["gross_loss"], sums["fees"], max_dd,
                         ulcer, monthly)


def window_metrics(store: TradeStore, segments: Sequence[Mapping], lot: float = 10_000.0) -> WindowMetrics:
    """Window metrics of consecutive active segments read (hash-checked) from the store."""
    _check_run(segments)
    return compute_window([(segment, store.read_metrics(segment), store.read_hourly(segment)) for segment in segments],
                          lot)


__all__ = ["WindowMetrics", "compute_window", "window_metrics"]
