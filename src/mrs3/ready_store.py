"""READY v4 from the MRS2 trade store: zone scores over the last periods, 2-order structures, EQUAL MRS3 JSON.

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 2 contract», READY v4;
«READY v4 threshold calibration» for the evidence behind these numbers). Rule `ready-v4-store-1`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import itertools
import json
from pathlib import Path
from typing import Mapping, Sequence
import warnings

import numpy as np
import pyarrow as pa

from .config import AlgorithmConfig
from .lots import LotMethod, allocate_lots
from .mrs2_simulator import DAY_MS
from .period_csv import MA_LENGTHS, SHIFTS_BP, TIMEFRAMES
from .strategy_json import generate_strategy
from .trade_store import TradeStore, TradeStoreError, point_params, utc_now

RULE_VERSION = "ready-v4-store-1"
CATEGORIES = ("low", "moderate", "high")


@dataclass(frozen=True, slots=True)
class ReadyRule:
    periods: int = 3
    min_periods: int = 2
    weights_3: tuple[float, float, float] = (0.5, 0.3, 0.2)     # newest first
    weights_2: tuple[float, float] = (0.6, 0.4)
    min_trades: int = 6
    min_shift_bp: int = 70
    pool: int = 6
    ratio: float = 1.5
    admit_score: float = 30.0        # calibrated on the store, see «READY v4 threshold calibration»
    max_dd: float = 6.0
    low_dd: float = 2.0
    moderate_dd: float = 3.5
    ulcer_floor: float = 0.25

    def weights(self, count: int) -> tuple[float, ...]:
        return {3: self.weights_3, 2: self.weights_2}[count]

    def category(self, dd: float) -> str:
        return "low" if dd <= self.low_dd else "moderate" if dd <= self.moderate_dd else "high"


@dataclass(frozen=True, slots=True)
class PeriodFeatures:
    """Per-point features of one period, aligned with `point_ids`."""

    point_ids: np.ndarray
    upi: np.ndarray
    max_dd: np.ndarray
    trades: np.ndarray


def period_features(metrics: pa.Table, days: float, rule: ReadyRule = ReadyRule(), lot: float = 10_000.0) -> PeriodFeatures:
    final = metrics.column("final_balance").to_numpy()
    pnl30 = (final - lot) / lot * 100 * 30 / days
    upi = pnl30 / np.maximum(metrics.column("ulcer_index").to_numpy(), rule.ulcer_floor)
    return PeriodFeatures(metrics.column("point_id").to_numpy(), upi, metrics.column("max_drawdown_percent").to_numpy(),
                          metrics.column("trades").to_numpy())


def on_grid(pid: int) -> bool:
    timeframe, shift, open_len, close_len = point_params(int(pid))
    return timeframe in TIMEFRAMES and shift in SHIFTS_BP and open_len in MA_LENGTHS and close_len in MA_LENGTHS


def common_points(periods: Sequence[PeriodFeatures]) -> list[PeriodFeatures]:
    """Periods restricted to the grid points present in every one of them (older segments may hold another
    TF set or extra shifts)."""
    shared = set.intersection(*({int(pid) for pid in period.point_ids if on_grid(pid)} for period in periods))
    out = []
    for period in periods:
        keep = np.array([int(pid) in shared for pid in period.point_ids], dtype=bool)
        order = np.argsort(period.point_ids[keep], kind="stable")
        out.append(PeriodFeatures(period.point_ids[keep][order], period.upi[keep][order], period.max_dd[keep][order],
                                  period.trades[keep][order]))
    return out


def zone_scores(point_ids: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Median of the 26 cube neighbours (shift index, Open MA, Close MA) inside each TF; NaN without neighbours."""
    params = [point_params(int(pid)) for pid in point_ids]
    shift_index = {shift: index for index, shift in enumerate(SHIFTS_BP)}
    out = np.full(len(point_ids), np.nan)
    size = (len(SHIFTS_BP), len(MA_LENGTHS), len(MA_LENGTHS))
    for timeframe in sorted({param[0] for param in params}):
        rows = [index for index, param in enumerate(params) if param[0] == timeframe]
        cells = [(shift_index[params[i][1]], params[i][2] - MA_LENGTHS[0], params[i][3] - MA_LENGTHS[0]) for i in rows]
        cube = np.full(size, np.nan)
        for i, cell in zip(rows, cells):
            cube[cell] = values[i]
        padded = np.pad(cube, 1, constant_values=np.nan)
        views = np.stack([padded[1 + a:1 + a + size[0], 1 + b:1 + b + size[1], 1 + c:1 + c + size[2]]
                          for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            median = np.nanmedian(views, axis=0)
        for i, cell in zip(rows, cells):
            out[i] = median[cell]
    return out


def best_structures(periods: Sequence[PeriodFeatures], rule: ReadyRule = ReadyRule()) -> list[dict]:
    """The best 2-order structure per TF from periods given newest first (same point set in every period)."""
    point_ids = periods[0].point_ids
    if any(not np.array_equal(period.point_ids, point_ids) for period in periods):
        raise TradeStoreError("periods have different point sets")
    weights = rule.weights(len(periods))
    score = sum(weight * zone_scores(point_ids, period.upi) for weight, period in zip(weights, periods))
    trades = np.stack([period.trades for period in periods])
    max_dd = np.stack([period.max_dd for period in periods])
    params = [point_params(int(pid)) for pid in point_ids]
    active = (trades.sum(axis=0) >= rule.min_trades) & (trades.min(axis=0) >= 1) & np.isfinite(score)
    best_per_tf: dict[str, tuple] = {}
    for timeframe in TIMEFRAMES:
        for close in MA_LENGTHS:
            # per shift the active Open MA with the best combined score (ties: lower Open MA)
            per_shift: dict[int, int] = {}
            for index, (tf, shift, open_len, close_len) in enumerate(params):
                if tf != timeframe or close_len != close or not active[index] or shift < rule.min_shift_bp:
                    continue
                kept = per_shift.get(shift)
                if kept is None or (score[index], -params[index][2]) > (score[kept], -params[kept][2]):
                    per_shift[shift] = index
            pool = sorted(per_shift.values(), key=lambda i: (-score[i], params[i][1]))[: rule.pool]
            for first, second in itertools.combinations(sorted(pool, key=lambda i: params[i][1]), 2):
                if params[second][1] < rule.ratio * params[first][1]:
                    continue
                value = float((score[first] + score[second]) / 2)
                dd = float(((max_dd[:, first] + max_dd[:, second]) / 2).max())
                key = (-value, dd, (params[first][1], params[second][1]), close)
                if timeframe not in best_per_tf or key < best_per_tf[timeframe][0]:
                    best_per_tf[timeframe] = (key, value, dd, close, (first, second))
    out = []
    for timeframe in TIMEFRAMES:
        if timeframe not in best_per_tf:
            continue
        _, value, dd, close, members = best_per_tf[timeframe]
        out.append({"timeframe": timeframe, "close_ma": close, "score": value, "dd": dd,
                    "admitted": value > rule.admit_score and dd <= rule.max_dd,
                    "orders": [{"shift_bp": params[i][1], "open_ma": params[i][2], "point_id": int(point_ids[i]),
                                "score": float(score[i])} for i in members]})
    return out


LATEST_SOURCES = ("simulator", "tester")


def pair_entry(store: TradeStore, symbol: str, rule: ReadyRule = ReadyRule(), exchange: str = "bybit",
               latest_source: str = "simulator") -> dict:
    """READY entry of one pair from its last active simulator segments; with `latest_source="tester"` the newest
    period is the tester segment of the same span (only its sampled points take part)."""
    if latest_source not in LATEST_SOURCES:
        raise TradeStoreError(f"unknown latest source: {latest_source}")
    segments = store.segments(symbol, exchange, source="simulator")[-rule.periods:]
    pending = False
    if latest_source == "tester" and segments:
        newest = segments[-1]
        tester = [s for s in store.segments(symbol, exchange, source="tester")
                  if (s["start_ms"], s["end_ms"]) == (newest["start_ms"], newest["end_ms"])]
        if tester:
            segments = [*segments[:-1], tester[-1]]
        else:
            pending = True
    entry = {"pair": symbol, "latest_source": latest_source,
             "periods": [{"segment_id": s["segment_id"], "metrics_sha256": s["metrics_sha256"],
                          "start_ms": s["start_ms"], "end_ms": s["end_ms"]} for s in segments]}
    if len(segments) < rule.min_periods:
        return {**entry, "status": "NOT_ENOUGH_PERIODS", "structures": [], "timeframes": []}
    if pending:
        return {**entry, "status": "TESTER_PENDING", "structures": [], "timeframes": []}
    newest_first = common_points([period_features(store.read_metrics(s), (s["end_ms"] - s["start_ms"]) / DAY_MS, rule)
                                  for s in reversed(segments)])
    found = best_structures(newest_first, rule) if len(newest_first[0].point_ids) else []
    admitted = [item for item in found if item["admitted"]]
    for item in admitted:
        item["category"] = rule.category(item["dd"])
    admitted.sort(key=lambda item: (CATEGORIES.index(item["category"]), -item["score"], item["timeframe"]))
    for rank, item in enumerate(admitted, 1):
        item["pair_rank"] = rank
    timeframes = [{"timeframe": item["timeframe"], "score": item["score"], "dd": item["dd"],
                   "admitted": item["admitted"]} for item in found]
    return {**entry, "status": "READY" if admitted else "NO_ADMITTED_STRUCTURE", "structures": admitted,
            "timeframes": timeframes}


def inputs_sha256(entry: Mapping, rule: ReadyRule) -> str:
    text = json.dumps({"rule": RULE_VERSION, "params": asdict(rule),
                       "inputs": [period["metrics_sha256"] for period in entry["periods"]]}, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def structure_document(symbol: str, item: Mapping) -> dict:
    orders = item["orders"]
    return {"structure_id": "_".join(f"{order['shift_bp']}bp{order['open_ma']}" for order in orders),
            "symbol": symbol, "side": "LONG", "timeframe": item["timeframe"], "order_count": len(orders),
            "common_close_ma": item["close_ma"],
            "orders": [{"id": index, "open_ma": order["open_ma"], "shift_bp": order["shift_bp"]}
                       for index, order in enumerate(orders, 1)]}


def build_ready(store: TradeStore, symbols: Sequence[str], out_dir: Path, template: Mapping,
                rule: ReadyRule = ReadyRule(), exchange: str = "bybit", latest_source: str = "simulator") -> dict:
    """READY folder (strategies + manifest) for the pairs and one catalog `ready` row per pair."""
    entries = []
    for symbol in dict.fromkeys(symbols):
        try:
            entries.append(pair_entry(store, symbol, rule, exchange, latest_source))
        except Exception as error:  # one pair never stops the READY run
            entries.append({"pair": symbol, "status": "ERROR", "error": f"{type(error).__name__}: {error}",
                            "periods": [], "structures": [], "timeframes": []})
    best: dict[str, tuple[float, str, str]] = {}
    for entry in entries:
        for item in entry["structures"]:
            candidate = (item["score"], entry["pair"], item["timeframe"])
            if item["category"] not in best or candidate[0] > best[item["category"]][0]:
                best[item["category"]] = candidate
    config = AlgorithmConfig()
    strategies = Path(out_dir) / "strategies"
    strategies.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        for item in entry["structures"]:
            item["best_in_category"] = best[item["category"]][1:] == (entry["pair"], item["timeframe"])
            structure = structure_document(entry["pair"], item)
            lots = allocate_lots(structure["orders"], LotMethod.EQUAL, config)
            strategy = generate_strategy(template, structure, lots, LotMethod.EQUAL, config)
            item["strategy"] = strategy["name"]
            (strategies / f"{strategy['name']}.json").write_text(
                json.dumps(strategy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        entry["inputs_sha256"] = inputs_sha256(entry, rule)
    manifest = {"rule_version": RULE_VERSION, "rule": asdict(rule), "exchange": exchange, "latest_source": latest_source,
                "pairs": entries}
    (Path(out_dir) / "ready_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
    with store.connect() as con:
        for entry in entries:
            if entry["status"] in ("ERROR", "TESTER_PENDING"):
                continue              # an error is not a READY result: the next run decides
            ready_id = f"{exchange}/{entry['pair']}/{RULE_VERSION}/{entry['inputs_sha256'][:16]}"
            # the catalog keeps only what the pair's own inputs determine (no cross-pair `best_in_category`)
            payload = {**entry, "structures": [{key: value for key, value in item.items() if key != "best_in_category"}
                                               for item in entry["structures"]]}
            con.execute("insert or ignore into ready values (?, ?, ?, ?, ?, ?, ?)",
                        [ready_id, exchange, entry["pair"], RULE_VERSION, entry["inputs_sha256"], utc_now(),
                         json.dumps(payload, ensure_ascii=False, sort_keys=True)])
    statuses = [entry["status"] for entry in entries]
    return {"pairs": len(entries), "ready_pairs": statuses.count("READY"), "errors": statuses.count("ERROR"),
            "strategies": sum(len(entry["structures"]) for entry in entries),
            "not_enough_periods": statuses.count("NOT_ENOUGH_PERIODS"), "tester_pending": statuses.count("TESTER_PENDING")}


__all__ = ["RULE_VERSION", "PeriodFeatures", "ReadyRule", "best_structures", "build_ready", "pair_entry",
           "period_features", "zone_scores"]
