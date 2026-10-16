"""Top-k selection per scope-cut by candidate scores vs baselines (cross-fitted by symbol half)."""
import sys
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
b, work = sys.argv[1], Path(sys.argv[2])
c = duckdb.connect()
c.execute("set threads to 16")
rng = np.random.default_rng(11)
feat = (work / "deep_features.parquet").as_posix()

MODEL_FEATURES = [
    "pnl30", "maxdd", "eff", "ulcer", "upi", "underwater", "pos_weeks", "act_weeks", "worst_week",
    "sharpe_w", "min_third_ratio", "trades30", "nb_q25_eff", "nb_med_eff", "nb_min_eff", "nb2_q25_eff",
    "nb_q25_upi", "nb_med_upi", "nb_q25_pnl30", "nb_q25_pos_weeks", "nb_q25_sharpe_w", "nb_med_maxdd",
    "nb_pos_share", "shift_bp",
]
KEY = ["scope", "cut"]


def load(F):
    df = c.execute(f"""
      select f.*, o.symbol, o.half, o.shift_bp, o.economic_pass, o.S_q25,
        o.pnl_fwd_pct * 30.0 / {F} * 5 / greatest(o.dd_fwd_pct, 1.0) q_fwd,
        (o.pnl_fwd_pct < 0)::double loss_neg, o.dd_fwd_pct dd_fwd, (o.trades_fwd = 0)::double no_trades,
        o.pnl_fwd_pct * 30.0 / {F} * 5 / greatest(f.maxdd, 1.0) r_ex,
        (o.dd_fwd_pct > greatest(f.maxdd, 1.0))::double breach_d,
        (m.point_id is not null) m1
      from read_parquet('{feat}') f
      join read_parquet('{b}/outcomes.parquet') o using (scope, cut, point_id)
      left join (select distinct scope, cut, point_id from read_parquet('{b}/picks.parquet')
                 where method='M1' and variant='exp') m using (scope, cut, point_id)
      where o.variant='exp' and o.forward_days={F}
        and f.min_third_trades >= 1 and f.trades30 * f.train_days / 30 >= 6""").df()
    df["hit10"] = ((df.r_ex >= 10) & (df.breach_d == 0)).astype(float)
    df["hit15"] = ((df.r_ex >= 15) & (df.breach_d == 0)).astype(float)
    df["bad_d"] = ((df.loss_neg == 1) | (df.breach_d == 1)).astype(float)
    return df


def ranked(df, cols):
    return df.groupby(KEY)[cols].rank(pct=True).fillna(0.5)


def cross_fit(df):
    """Ridge on within-scope ranks; fit on one half, predict the other."""
    X = ranked(df, MODEL_FEATURES).values
    y = df.groupby(KEY)["r_ex"].rank(pct=True).values
    pred = np.zeros(len(df))
    coefs = {}
    for train_half, test_half in (("A", "B"), ("B", "A")):
        tr = (df.half == train_half).values
        te = (df.half == test_half).values
        Xt = np.c_[np.ones(tr.sum()), X[tr]]
        lam = 1.0 * tr.sum() / 100
        w = np.linalg.solve(Xt.T @ Xt + lam * np.eye(Xt.shape[1]), Xt.T @ y[tr])
        pred[te] = np.c_[np.ones(te.sum()), X[te]] @ w
        coefs[train_half] = pd.Series(w[1:], index=MODEL_FEATURES)
    return pred, pd.DataFrame(coefs)


def pick_top(df, score, k):
    r = df.groupby(KEY)[score].rank(ascending=False, method="first")
    return r <= k


METRICS = ["hit10", "hit15", "bad_d", "loss_neg", "dd_fwd", "no_trades"]


def compare(df, mask, name, k):
    """Mean of picks minus mean of all active points, within scope-cut, per symbol, bootstrap."""
    rows = []
    sel = df[mask]
    for subset in ("all", "A", "B"):
        s = sel if subset == "all" else sel[sel.half == subset]
        base = df if subset == "all" else df[df.half == subset]
        ms = s.groupby(KEY + ["symbol"])[METRICS].mean()
        mb = base.groupby(KEY + ["symbol"])[METRICS].mean().loc[ms.index]
        diff = (ms - mb).groupby(level="symbol").mean()
        lvl = ms.groupby(level="symbol").mean().mean()
        for m in METRICS:
            v = diff[m].values
            boots = v[rng.integers(0, len(v), size=(1000, len(v)))].mean(axis=1)
            lo, hi = np.percentile(boots, [2.5, 97.5])
            rows.append(dict(method=name, k=k, subset=subset, metric=m, level=lvl[m], diff=v.mean(), lo=lo, hi=hi,
                             symbols=len(v), picks_per_cut=s.groupby(KEY).size().mean()))
    return rows


all_rows = []
for F in (28, 14):
    df = load(F)
    df["model"], coefs = cross_fit(df)
    if F == 28:
        print("ridge coefficients (fit on A / fit on B), within-scope rank features:")
        print(coefs.round(3).sort_values("A").to_string())
    df["neg_rand"] = rng.random(len(df))
    scores = ["model", "nb_med_eff", "nb_med_upi", "nb_q25_eff", "eff", "upi", "pnl30", "S_q25"]
    rows = []
    for k in (1, 4):
        for s in scores:
            rows += compare(df, pick_top(df, s, k), s, k)
        econ_k = df[df.economic_pass].groupby(KEY)["neg_rand"].rank(method="first") <= k
        rows += compare(df, df.economic_pass & econ_k.reindex(df.index, fill_value=False), "random_econ", k)
    rows += compare(df, df.m1, "prod_M1", "M1")
    res = pd.DataFrame(rows).assign(F=F)
    all_rows.append(res)
    print(f"\n===== F={F}: picks minus all active points (within scope-cut, per-symbol mean) =====")
    for metric in ("hit10", "bad_d", "loss_neg", "dd_fwd", "no_trades"):
        t = res[res.metric == metric]
        print(f"\n-- {metric} --  method k | level(all) | all diff [CI] | A diff | B diff")
        for (method, k), g in t.groupby(["method", "k"], sort=False):
            a = g[g.subset == "all"].iloc[0]
            pa = g[g.subset == "A"].iloc[0]
            pb = g[g.subset == "B"].iloc[0]
            flag = "*" if (pa.lo > 0 and pb.lo > 0) or (pa.hi < 0 and pb.hi < 0) else ""
            print(f"{method:12} {str(k):3} | {a.level:6.3f} | {a['diff']:+.3f} [{a.lo:+.3f};{a.hi:+.3f}] | "
                  f"{pa['diff']:+.3f} | {pb['diff']:+.3f} {flag}")
out = pd.concat(all_rows, ignore_index=True)
out["k"] = out["k"].astype(str)
c.register("o_df", out)
c.execute(f"copy (select * from o_df) to '{(work / 'deep_select.parquet').as_posix()}' (format parquet)")
