"""Walk-forward calibration of READY v4 thresholds on the MRS2 trade store (exact, hourly-sampled).

For every pair with >=3 active simulator segments: walk forward period by period. Train the current READY v4
structure-finding rule (zone score, best 2-order structure per TF) on up to the 3 preceding periods (same formula
as production: score and DD are MEANS of the two orders' own period figures). Then measure the forward outcome of
the chosen 2 orders over the next period as a TRUE combined curve (summed hourly close/low/high, shared base
2x lot) rather than a mean-of-orders approximation, since that is what a real combined MRS3 structure experiences.

Every (pair, cut, TF) candidate becomes one row, pooled across the whole registry, independent of the current
admission thresholds (collected with a permissive rule). The printed tables bucket candidates by their forecast
score and training-DD to show what forward PnL/DD/loss rate each band actually produced, so the READY v4
thresholds can be set from this data instead of the provisional daily-scale Source v6 figures.

Usage: .venv\\Scripts\\python.exe scripts\\plateau_research\\store_threshold_study.py [--out data\\ready_threshold_study.parquet]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mrs3.mrs2_simulator import DAY_MS  # noqa: E402
from mrs3.ready_store import ReadyRule, best_structures, common_points, period_features  # noqa: E402
from mrs3.trade_store import TradeStore  # noqa: E402

LOT = 10_000.0
PERMISSIVE = ReadyRule(admit_score=-1e18, max_dd=1e18)       # collect every TF candidate, gate nothing


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


def walk_forward(store: TradeStore, pairs: list[str]) -> pd.DataFrame:
    rows = []
    for symbol in pairs:
        segments = store.segments(symbol, source="simulator")
        if len(segments) < 3:
            continue
        loaded = [(segment, store.read_metrics(segment)) for segment in segments]
        features = [period_features(metrics, (segment["end_ms"] - segment["start_ms"]) / DAY_MS)
                    for segment, metrics in loaded]
        for k in range(2, len(segments)):
            train = common_points(features[max(0, k - 3):k][::-1])
            if not len(train[0].point_ids):
                continue
            found = best_structures(train, PERMISSIVE)
            if not found:
                continue
            forward_segment, forward_metrics = loaded[k]
            forward_hourly = store.read_hourly(forward_segment)
            days = (forward_segment["end_ms"] - forward_segment["start_ms"]) / DAY_MS
            for item in found:
                orders = item["orders"]
                outcome = combined_forward(forward_hourly, forward_metrics, orders[0]["point_id"],
                                           orders[1]["point_id"], days)
                rows.append({"pair": symbol, "cut": k, "periods_before": len(train),
                            "timeframe": item["timeframe"], "close_ma": item["close_ma"], "score": item["score"],
                            "train_dd": item["dd"], "shift_a": orders[0]["shift_bp"], "shift_b": orders[1]["shift_bp"],
                            **outcome})
        print(f"{symbol}: {len(segments)} periods", flush=True)
    return pd.DataFrame(rows)


def pair_weighted(df: pd.DataFrame) -> dict:
    """Every PAIR counts once, not every (pair, cut, TF) candidate: a pair with many admitted TF candidates on
    the same cut shares one underlying forward price path, so those candidates are not independent samples."""
    if not len(df):
        return {"n_pairs": 0, "loss": float("nan"), "ret30": float("nan")}
    per_pair = df.groupby("pair").agg(loss=("fwd_loss", "mean"), ret30=("fwd_pnl30", "mean"))
    return {"n_pairs": len(per_pair), "loss": float(per_pair.loss.mean()), "ret30": float(per_pair.ret30.mean())}


def report(df: pd.DataFrame) -> None:
    print(f"\ncandidates {len(df)}, pairs {df.pair.nunique()}, cuts {df.groupby(['pair', 'cut']).ngroups}\n")

    print("-- admit_score sweep, candidate-weighted (one row per (pair,cut,TF)) vs pair-weighted "
          "(one row per pair: mean of its own candidates, every pair counts once) --")
    print("   jointly with the DD ceiling (train_dd <= 6, current max_dd):")
    sweep = []
    for x in (0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 60, 80, 100):
        g = df[(df.score > x) & (df.train_dd <= 6)]
        if not len(g):
            continue
        pw = pair_weighted(g)
        sweep.append({"cutoff": x, "n": len(g), "pairs": g.pair.nunique(), "cand_ret30": g.fwd_pnl30.mean(),
                     "cand_loss": g.fwd_loss.mean(), "pair_ret30": pw["ret30"], "pair_loss": pw["loss"]})
    print(pd.DataFrame(sweep).round(3).to_string(index=False))

    print("-- by forecast score band --")
    bins = [-np.inf, 0, 5, 10, 15, 20, 25, 30, 40, 60, np.inf]
    band = pd.cut(df.score, bins)
    table = df.groupby(band).agg(n=("score", "size"), ret30=("fwd_pnl30", "mean"), ret30_med=("fwd_pnl30", "median"),
                                 dd_p90=("fwd_maxdd", lambda s: s.quantile(0.9)), dd_med=("fwd_maxdd", "median"),
                                 loss=("fwd_loss", "mean"))
    print(table.round(2).to_string())

    print("\n-- by training DD band (mean-of-orders, current formula) --")
    dd_bins = [0, 2, 3.5, 6, 8, 12, 20, np.inf]
    dd_band = pd.cut(df.train_dd, dd_bins)
    admitted = df[df.score > 20]
    dd_table = admitted.groupby(dd_band).agg(n=("train_dd", "size"), pairs=("pair", "nunique"),
                                             ret30=("fwd_pnl30", "mean"), fwd_dd_p90=("fwd_maxdd", lambda s: s.quantile(0.9)),
                                             fwd_dd_med=("fwd_maxdd", "median"), loss=("fwd_loss", "mean"))
    print("(restricted to score > 20, the current admission cutoff)")
    print(dd_table.round(2).to_string())

    print("\n-- correlation of train_dd with forward MaxDD, among admitted (score > 20) --")
    print(admitted[["train_dd", "fwd_maxdd"]].corr().round(3).to_string())

    print("\n-- quantiles of train_dd and forward MaxDD among admitted (score > 20) --")
    q = [0.25, 0.5, 0.75, 0.9, 0.95]
    print(admitted[["train_dd", "fwd_maxdd"]].quantile(q).round(2).to_string())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "ready_threshold_study.parquet")
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.json")
    parser.add_argument("--pairs", default="")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    store = TradeStore(Path(config["trade_store"]["root"]))
    if args.pairs:
        pairs = [item.strip().upper() for item in args.pairs.split(",") if item.strip()]
    else:
        with store.connect(read_only=True) as con:
            pairs = [row[0] for row in con.execute(
                "select distinct symbol from segments where status = 'active' and source = 'simulator' "
                "order by symbol").fetchall()]
    df = walk_forward(store, pairs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out)
    print(f"\nwrote {args.out} ({len(df)} rows)")
    report(df)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
