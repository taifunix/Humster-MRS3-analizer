"""Monthly CSV periods vs one training window: how should READY be scored when MRS2 runs come in months?

Usage: local_window_study.py <export dir> <work dir> [segment days, default 30]
For every scope-cut (TF 15m-2h) with at least 60 (90) days of history before T, the history is cut into
30-day segments ending at T, as separate monthly CSV runs would see it: each segment has its own PnL,
drawdown path (peak restarts at the segment start) and Ulcer; positions carried across a boundary are not
re-simulated. Point score per segment = neighbourhood median UPI on the (shift, open MA, close MA) cube.
Variants combine the segment scores (or score the whole window); each picks the v4 structure of the
scope-cut and the forward outcome of that structure comes from the summed member curves.
"""
import itertools
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from local_deep_struct import DAY, GRID, SI, WARMUP, dense, evaluate  # noqa: E402
from local_pair_concept import TFS  # noqa: E402

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)
SEG, POOL, RATIO, FLOOR = 30, 6, 1.5, 70
LENGTHS = (60, 90)


def nb_median(values, si, oi, ci):
    cube = np.full((len(GRID), 6, 6), np.nan)
    cube[si, oi, ci] = values
    p = np.pad(cube, 1, constant_values=np.nan)
    n = len(GRID)
    views = np.stack([p[1 + a:n + 1 + a, 1 + b:7 + b, 1 + c:7 + c]
                      for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)])
    return np.nanmedian(views, axis=0)[si, oi, ci]


def window_upi(ib, bal, eqc, eqmin, tr, a, b):
    pnl30 = (bal[:, b - 1] - bal[:, a - 1]) / ib * 100 * 30 / (b - a)
    prev = np.concatenate([eqc[:, a - 1:a], eqc[:, a:b - 1]], axis=1)
    peak = np.maximum.accumulate(prev, axis=1)
    dd = np.clip((peak - eqmin[:, a:b]) / peak * 100, 0, None)
    ulcer = np.sqrt((dd ** 2).mean(axis=1))
    return pnl30 / np.maximum(ulcer, 0.25), tr[:, a:b].sum(axis=1)


def pick(score, active, shift, ci, pids):
    """v4 structure: per close MA the best open MA per shift, top-6 shifts, 2-3 orders, ratio 1.5, mean score."""
    best = None
    df = pd.DataFrame({"score": score, "shift": shift, "ci": ci, "pid": pids})[active & (shift >= FLOOR) & np.isfinite(score)]
    for _, g in df.groupby("ci"):
        cand = g.sort_values("score", ascending=False).drop_duplicates("shift").head(POOL).sort_values("shift")
        pts = list(cand.itertuples(index=False))
        for n in (2, 3):
            for combo in itertools.combinations(pts, n):
                sh = [p.shift for p in combo]
                if all(r >= RATIO * l for l, r in zip(sh, sh[1:])):
                    s = float(np.mean([p.score for p in combo]))
                    if best is None or s > best[0]:
                        best = (s, sh, [p.pid for p in combo])
    return best


def task(args):
    scope, daily, cuts, seg_days = args
    global SEG, LENGTHS
    SEG, LENGTHS = seg_days, (2 * seg_days, 3 * seg_days)
    d0, pidx, ib, bal, eqc, eqmin, tr = dense(daily, cuts)
    arrays = (ib, bal, eqc, eqmin, tr)
    pids = np.array(sorted(pidx, key=pidx.get))
    parts = pd.Series(pids).str.split("|")
    shift = parts.str[3].astype(int).values
    si = np.array([SI[s] for s in shift])
    oi = parts.str[6].astype(int).values - 2
    ci = parts.str[9].astype(int).values - 2
    first = int((pd.to_datetime(daily.utc_day).values.astype("datetime64[D]").min() - d0).astype(int)) + WARMUP
    rows = []
    for cut in cuts.itertuples(index=False):
        t = int((np.datetime64(int(cut.t_ms // DAY), "D") - d0).astype(int))
        for L in LENGTHS:
            if t - L < first + 1:
                continue
            segs = [(t - SEG * (k + 1), t - SEG * k) for k in range(L // SEG)]  # newest first
            seg_nb, seg_tr = [], []
            for a, b in segs:
                upi, trades = window_upi(ib, bal, eqc, eqmin, tr, a, b)
                seg_nb.append(nb_median(upi, si, oi, ci))
                seg_tr.append(trades)
            seg_nb, seg_tr = np.array(seg_nb), np.array(seg_tr)
            full_upi, _ = window_upi(ib, bal, eqc, eqmin, tr, t - L, t)
            active = (seg_tr.sum(axis=0) >= 6) & (seg_tr.min(axis=0) >= 1)
            variants = {
                "full window": nb_median(full_upi, si, oi, ci),
                "last month only": seg_nb[0],
                "mean of months": np.nanmean(seg_nb, axis=0),
                "median of months": np.nanmedian(seg_nb, axis=0),
                "min of months": np.nanmin(seg_nb, axis=0),
            }
            if L == 3 * SEG:
                variants["recent-weighted 50/30/20"] = 0.5 * seg_nb[0] + 0.3 * seg_nb[1] + 0.2 * seg_nb[2]
            for F in cut.forwards:
                if t + F > bal.shape[1]:
                    continue
                for name, score in variants.items():
                    p = pick(score, active, shift, ci, pids)
                    if p is None:
                        continue
                    r = evaluate([pidx[i] for i in p[2]], arrays, t - L, t, F)
                    rows.append(dict(scope=scope, cut=cut.cut, t_ms=cut.t_ms, L=L, F=F, variant=name, score=p[0],
                                     shifts="/".join(map(str, p[1])), ids="|".join(p[2]), **r))
    return pd.DataFrame(rows)


def main():
    b, work = sys.argv[1], Path(sys.argv[2])
    seg_days = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    c = duckdb.connect()
    cuts = c.execute(f"""select scope, cut, any_value(t_ms) t_ms, list(distinct forward_days) forwards
        from read_parquet('{b}/outcomes.parquet') where variant='exp' group by all""").df()
    cuts = cuts[cuts.scope.str.split("|").str[2].isin(TFS)]
    cuts["s_ms"] = cuts.t_ms - (3 * seg_days + 1) * DAY
    cuts["forwards"] = cuts.forwards.map(lambda x: sorted(int(v) for v in x))
    c.register("sc", cuts[["scope"]].drop_duplicates())
    daily = c.execute(f"""select d.scope, d.point_id, d.utc_day, d.balance_close, d.equity_close, d.equity_min, d.trades_closed,
        d.initial_balance from read_parquet('{b}/daily_equity.part0*.parquet') d join sc using (scope)""").df()
    dg = dict(tuple(daily.groupby("scope")))
    tasks = [(s, dg[s], cuts[cuts.scope == s], seg_days) for s in sorted(dg)]
    del daily
    with ProcessPoolExecutor(max_workers=24) as pool:
        res = pd.concat(list(pool.map(task, tasks, chunksize=2)), ignore_index=True)
    res["symbol"] = res.scope.str.split("|").str[0]
    res["month"] = pd.to_datetime(res.t_ms, unit="ms").dt.to_period("M").astype(str)
    res["q"] = res.ret30 * 5 / res.dd_fwd.clip(lower=1)
    res.to_csv(work / f"window_study_{seg_days}.csv", index=False, encoding="utf-8")
    cols = ["ret30", "dd_fwd", "q", "loss_neg"]
    for L in (2 * seg_days, 3 * seg_days):
        for F in (28, 14):
            sub = res[(res.L == L) & (res.F == F)]
            if sub.empty:
                continue
            common = sub.groupby(["scope", "cut"]).variant.nunique()
            keys = common[common == common.max()].index
            sub = sub.set_index(["scope", "cut"]).loc[keys].reset_index()
            print(f"\n== history {L} days, forward {F} days: {len(keys)} scope-cuts, {sub.symbol.nunique()} pairs ==")
            rows = []
            for name, g in sub.groupby("variant"):
                lv = g.groupby("symbol")[cols].mean().mean()
                g = g.copy()
                g["pct"] = g.groupby("month").score.rank(pct=True)
                top, rest = g[g.pct > 0.7], g[g.pct <= 0.7]
                lv["admit30%_ret30"] = top.groupby("symbol").ret30.mean().mean()
                lv["admit30%_dd"] = top.groupby("symbol").dd_fwd.mean().mean()
                lv["admit30%_loss"] = top.groupby("symbol").loss_neg.mean().mean()
                lv["rest_ret30"] = rest.groupby("symbol").ret30.mean().mean()
                lv["rest_loss"] = rest.groupby("symbol").loss_neg.mean().mean()
                for t in ("q", "ret30"):
                    g[t + "_x"] = g[t] - g.groupby("month")[t].transform("mean")
                lv["rho_score_q"] = g[["score", "q_x"]].corr(method="spearman").iloc[0, 1]
                lv["rho_score_ret"] = g[["score", "ret30_x"]].corr(method="spearman").iloc[0, 1]
                rows.append(lv.rename(name))
            print(pd.DataFrame(rows).round(2).to_string())


if __name__ == "__main__":
    main()
