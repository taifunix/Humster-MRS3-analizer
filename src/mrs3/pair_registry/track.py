"""Walk-forward track record of a pair under its production READY v4 rule (registry stage 3).

`combined_forward` comes from scripts/plateau_research/store_threshold_study.py (the READY v4 calibration study),
which now imports it from here. Contract: docs/specs/2026-10-18-pair-registry.md, «Stage 3 contract».
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from ..mrs2_simulator import DAY_MS
from ..ready_store import ReadyRule, best_structures, common_points, period_features

LOT = 10_000.0


def combined_forward(hourly, metrics, a: int, b: int, days: float) -> dict:
    """True combined curve of two points over one period: shared base 2x lot, hourly low/high summed."""
    sub = hourly.filter(pc.is_in(hourly.column("point_id"), pa.array([a, b]))).to_pandas()
    close = sub.pivot(index="hour_ms", columns="point_id", values="close").sort_index()
    low = sub.pivot(index="hour_ms", columns="point_id", values="low").sort_index()
    high = sub.pivot(index="hour_ms", columns="point_id", values="high").sort_index()
    base = 2 * LOT
    close_sum, low_sum, high_sum = (frame.sum(axis=1).to_numpy() for frame in (close, low, high))
    peak_before = np.maximum.accumulate(np.r_[base, high_sum[:-1]]) if len(high_sum) else np.array([base])
    maxdd = float(max(0.0, ((peak_before - low_sum) / peak_before * 100).max())) if len(low_sum) else 0.0
    peaks_close = np.maximum.accumulate(np.r_[base, close_sum])[1:] if len(close_sum) else np.array([])
    dd_close = np.clip((peaks_close - close_sum) / peaks_close * 100, 0, None) if len(peaks_close) else np.array([])
    ulcer = float(np.sqrt((dd_close ** 2).mean())) if len(dd_close) else 0.0
    final = float(close_sum[-1]) if len(close_sum) else base
    pnl30 = (final - base) / base * 100 * 30 / days
    rows = metrics.to_pandas().set_index("point_id")
    trades = int(rows.loc[[a, b], "trades"].sum())
    return {"fwd_pnl30": pnl30, "fwd_maxdd": maxdd, "fwd_ulcer": ulcer, "fwd_trades": trades,
            "fwd_upi": pnl30 / max(ulcer, 0.25), "fwd_loss": final < base}


def track_inputs_sha256(segments: list[dict], rule: ReadyRule) -> str:
    """What a track record depends on: the active simulator segments (and their metrics) and the rule."""
    text = json.dumps({"segments": [[s["segment_id"], s["metrics_sha256"]] for s in segments], "rule": asdict(rule)},
                      sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def track_record(store, symbol: str, exchange: str = "bybit", rule: ReadyRule = ReadyRule()) -> dict[str, object]:
    """Walk forward period by period: train on up to the three preceding periods with the production rule, measure
    the admitted structures on the next period. Returns cuts, admitted cuts, admitted candidates, forward loss rate."""
    segments = store.segments(symbol, exchange, source="simulator")
    result: dict[str, object] = {"cuts": 0, "admitted_cuts": 0, "admitted_candidates": 0, "forward_losses": 0,
                                 "forward_loss_rate": None, "inputs_sha256": track_inputs_sha256(segments, rule)}
    if len(segments) < 3:
        return result
    loaded = [(segment, store.read_metrics(segment)) for segment in segments]
    features = [period_features(metrics, (segment["end_ms"] - segment["start_ms"]) / DAY_MS, rule)
                for segment, metrics in loaded]
    for cut in range(2, len(segments)):
        train = common_points(features[max(0, cut - 3):cut][::-1])
        if not len(train[0].point_ids):
            continue
        result["cuts"] += 1
        admitted = [item for item in best_structures(train, rule) if item["admitted"]]
        if not admitted:
            continue
        result["admitted_cuts"] += 1
        forward_segment, forward_metrics = loaded[cut]
        hourly = store.read_hourly(forward_segment)
        days = (forward_segment["end_ms"] - forward_segment["start_ms"]) / DAY_MS
        for item in admitted:
            orders = item["orders"]
            outcome = combined_forward(hourly, forward_metrics, orders[0]["point_id"], orders[1]["point_id"], days)
            result["admitted_candidates"] += 1
            result["forward_losses"] += int(bool(outcome["fwd_loss"]))
    if result["admitted_candidates"]:
        result["forward_loss_rate"] = result["forward_losses"] / result["admitted_candidates"]
    return result
