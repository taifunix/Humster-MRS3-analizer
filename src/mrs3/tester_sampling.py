"""Sampled tester runs: point selection from the simulator, exact rectangular block cover, block tester config.

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 3 contract»).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from typing import Iterable, Mapping, Sequence

import numpy as np
import pyarrow as pa

from .mrs2_simulator import RunSettings, TIMEFRAME_MS
from .period_csv import MA_LENGTHS, SHIFTS_BP, build_tester_config, multiplier_text
from .trade_store import point_id, point_params

SELECTION_VERSION = "select-v1"
COVER_VERSION = "cover-v1"


@dataclass(frozen=True, slots=True)
class Selection:
    point_ids: tuple[int, ...]           # ascending
    sha256: str
    version: str = SELECTION_VERSION


def neighbours(pid: int) -> list[int]:
    """Grid neighbours of a point in its TF cube (shift index, Open MA, Close MA)."""
    timeframe, shift, open_len, close_len = point_params(pid)
    index = SHIFTS_BP.index(shift)
    out = []
    for a in (-1, 0, 1):
        for b in (-1, 0, 1):
            for c in (-1, 0, 1):
                if (a, b, c) == (0, 0, 0):
                    continue
                si, o, cl = index + a, open_len + b, close_len + c
                if 0 <= si < len(SHIFTS_BP) and o in MA_LENGTHS and cl in MA_LENGTHS:
                    out.append(point_id(timeframe, SHIFTS_BP[si], o, cl))
    return out


def select_points(metrics: pa.Table, days: float, metrics_sha256: str, *, fraction: float = 0.10,
                  min_trades: int = 6, ulcer_floor: float = 0.25, lot: float = 10_000.0) -> Selection:
    """`select-v1`: the top fraction of eligible points by the simulator's UPI, plus their cube neighbours."""
    pids = metrics.column("point_id").to_numpy()
    pnl = (metrics.column("final_balance").to_numpy() - lot) / lot * 100
    upi = pnl * 30 / days / np.maximum(metrics.column("ulcer_index").to_numpy(), ulcer_floor)
    eligible = (metrics.column("trades").to_numpy() >= min_trades) & (pnl > 0)
    count = math.ceil(fraction * len(pids))
    order = sorted(np.flatnonzero(eligible), key=lambda i: (-upi[i], int(pids[i])))[:count]
    known = {int(pid) for pid in pids}
    chosen = {int(pids[i]) for i in order}
    for pid in list(chosen):
        chosen.update(n for n in neighbours(pid) if n in known)
    ids = tuple(sorted(chosen))
    text = json.dumps({"version": SELECTION_VERSION, "metrics": metrics_sha256, "fraction": fraction, "points": ids})
    return Selection(ids, hashlib.sha256(text.encode("utf-8")).hexdigest())


@dataclass(frozen=True, slots=True)
class Block:
    timeframes: tuple[str, ...]
    shifts: tuple[int, ...]
    open_range: tuple[int, int]
    close_range: tuple[int, int]

    @property
    def runs(self) -> int:
        return (len(self.timeframes) * len(self.shifts) * (self.open_range[1] - self.open_range[0] + 1)
                * (self.close_range[1] - self.close_range[0] + 1))

    def point_ids(self) -> list[int]:
        return sorted(point_id(tf, shift, o, c) for tf in self.timeframes for shift in self.shifts
                      for o in range(self.open_range[0], self.open_range[1] + 1)
                      for c in range(self.close_range[0], self.close_range[1] + 1))

    def as_dict(self) -> dict:
        return {"timeframes": list(self.timeframes), "shifts": list(self.shifts), "open_range": list(self.open_range),
                "close_range": list(self.close_range)}


def _runs(values: Sequence[int]) -> list[tuple[int, int]]:
    """Consecutive integer runs of sorted values."""
    out: list[tuple[int, int]] = []
    for value in values:
        if out and value == out[-1][1] + 1:
            out[-1] = (out[-1][0], value)
        else:
            out.append((value, value))
    return out


def cover_blocks(point_ids: Iterable[int]) -> list[Block]:
    """`cover-v1`: exact cover of the points by rectangular blocks (no point twice, no extra point)."""
    cells: dict[tuple[str, int, int], set[int]] = {}
    for pid in point_ids:
        timeframe, shift, open_len, close_len = point_params(int(pid))
        cells.setdefault((timeframe, close_len, open_len), set()).add(shift)
    # 2. equal shift sets of consecutive Open MAs
    by_close: dict[tuple[str, int, tuple[int, ...]], list[int]] = {}
    for (timeframe, close_len, open_len), shifts in cells.items():
        by_close.setdefault((timeframe, close_len, tuple(sorted(shifts))), []).append(open_len)
    open_blocks: dict[tuple[str, tuple[int, ...], tuple[int, int]], list[int]] = {}
    for (timeframe, close_len, shifts), opens in by_close.items():
        for run in _runs(sorted(opens)):
            open_blocks.setdefault((timeframe, shifts, run), []).append(close_len)
    # 3. consecutive Close MAs with the same TF, shifts and Open range
    tf_blocks: dict[tuple[tuple[int, ...], tuple[int, int], tuple[int, int]], list[str]] = {}
    for (timeframe, shifts, open_run), closes in open_blocks.items():
        for close_run in _runs(sorted(closes)):
            tf_blocks.setdefault((shifts, open_run, close_run), []).append(timeframe)
    # 4. blocks equal in everything but the TF
    blocks = [Block(tuple(sorted(tfs, key=lambda tf: TIMEFRAME_MS[tf])), shifts, open_run, close_run)
              for (shifts, open_run, close_run), tfs in tf_blocks.items()]
    return sorted(blocks, key=lambda b: (TIMEFRAME_MS[b.timeframes[0]], b.shifts, b.open_range, b.close_range,
                                         b.timeframes))


def block_config(template: Mapping, symbol: str, start: date, end: date, block: Block, name: str,
                 settings: RunSettings = RunSettings(), parallel_runs: int = 4) -> dict:
    """Tester config of one block: fixed-lot MRS2 grid restricted to the block, HTML trade report on."""
    document = build_tester_config(template, symbol, start, end, name, block.timeframes, settings)
    document.update(UpdateData=False, LimitOrderVolumeCheck=False,
                    order_limit_fill_plus_tick=settings.limit_fill_plus_tick, max_parallel_runs=max(1, parallel_runs))
    document["report"].update(enable_html_report=True, include_settings=True, include_summary_table=True,
                              include_trades_table=True, include_chart_balance=True)
    for entry in document["parameter_mining"]:
        field = entry["name"]
        if field.endswith("ma_long.multiplier"):
            entry["values"] = [multiplier_text(shift) for shift in block.shifts]
        elif field.endswith("ma_long.len"):
            entry.update(start=float(block.open_range[0]), end=float(block.open_range[1]), step=1.0, values=[])
        elif field.endswith("ma_close_long.len"):
            entry.update(start=float(block.close_range[0]), end=float(block.close_range[1]), step=1.0, values=[])
    return document


__all__ = ["Block", "COVER_VERSION", "SELECTION_VERSION", "Selection", "block_config", "cover_blocks", "neighbours",
           "select_points"]
