"""Head-to-head vs production BASE picks (M1) on the same scope-cuts, k = |M1|."""
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
rng = np.random.default_rng(12)
feat = (work / "deep_features.parquet").as_posix()
KEY = ["scope", "cut"]
METRICS = ["hit10", "hit15", "loss_neg", "bad_d", "dd_fwd", "no_trades", "ret30", "r_ex_c"]


def load(F):
    df = c.execute(f"""
      select f.*, o.symbol, o.half, o.shift_bp, o.economic_pass,
        (o.pnl_fwd_pct < 0)::double loss_neg, o.dd_fwd_pct dd_fwd, (o.trades_fwd = 0)::double no_trades,
        o.pnl_fwd_pct * 30.0 / {F} ret30,
        o.pnl_fwd_pct * 30.0 / {F} * 5 / greatest(f.maxdd, 1.0) r_ex,
        (o.dd_fwd_pct > greatest(f.maxdd, 1.0))::double breach_d,
        (m.point_id is not null) m1
      from read_parquet('{feat}') f
      join read_parquet('{b}/outcomes.parquet') o using (scope, cut, point_id)
      left join (select distinct scope, cut, point_id from read_parquet('{b}/picks.parquet')
                 where method='M1' and variant='exp') m using (scope, cut, point_id)
      where o.variant='exp' and o.forward_days={F}""").df()
    df["hit10"] = ((df.r_ex >= 10) & (df.breach_d == 0)).astype(float)
    df["hit15"] = ((df.r_ex >= 15) & (df.breach_d == 0)).astype(float)
    df["bad_d"] = ((df.loss_neg == 1) | (df.breach_d == 1)).astype(float)
    df["r_ex_c"] = df.r_ex.clip(-30, 30)
    df["active"] = (df.min_third_trades >= 1) & (df.trades30 * df.train_days / 30 >= 6)
    return df


def top_k(df, mask, score, kmap):
    sub = df[mask].copy()
    sub["r"] = sub.groupby(KEY)[score].rank(ascending=False, method="first")
    sub = sub.join(kmap, on=KEY)
    return sub[sub.r <= sub.k]


def paired(a, bsel, label):
    ma = a.groupby(KEY + ["symbol", "half"])[METRICS].mean()
    mb = bsel.groupby(KEY + ["symbol", "half"])[METRICS].mean()
    idx = ma.index.intersection(mb.index)
    d = (ma.loc[idx] - mb.loc[idx]).reset_index()
    out = {}
    for subset in ("all", "A", "B"):
        s = d if subset == "all" else d[d.half == subset]
        ps = s.groupby("symbol")[METRICS].mean()
        for m in METRICS:
            v = ps[m].values
            bo = v[rng.integers(0, len(v), size=(2000, len(v)))].mean(axis=1)
            lo, hi = np.percentile(bo, [2.5, 97.5])
            out[(subset, m)] = (v.mean(), lo, hi, len(v))
    return out


for F in (28, 14):
    df = load(F)
    m1 = df[df.m1]
    kmap = m1.groupby(KEY).size().rename("k")
    scoped = df.set_index(KEY).index.isin(kmap.index)
    base_mask = scoped
    cands = {
        "nb_med_upi | active": top_k(df, base_mask & df.active, "nb_med_upi", kmap),
        "nb_med_upi | active & act_w>=0.5": top_k(df, base_mask & df.active & (df.act_weeks >= 0.5), "nb_med_upi", kmap),
        "nb_med_upi | econ": top_k(df, base_mask & df.economic_pass, "nb_med_upi", kmap),
        "upi | active": top_k(df, base_mask & df.active, "upi", kmap),
        "pnl30 | active": top_k(df, base_mask & df.active, "pnl30", kmap),
        "pnl30 | econ": top_k(df, base_mask & df.economic_pass, "pnl30", kmap),
    }
    econ = df[base_mask & df.economic_pass]
    print(f"\n===== F={F}: candidate minus production M1, same scope-cuts (k=|M1|) =====")
    print("profile of picks (mean): shift, trades30, maxdd, act_weeks")
    for name, sel in [("prod M1", m1)] + list(cands.items()) + [("all econ", econ)]:
        print(f"  {name:34} shift={sel.shift_bp.mean():6.0f} tr30={sel.trades30.mean():6.1f} "
              f"maxdd={sel.maxdd.mean():5.2f} act_w={sel.act_weeks.mean():.2f} "
              f"| hit10={sel.hit10.mean():.3f} hit15={sel.hit15.mean():.3f} loss={sel.loss_neg.mean():.3f} "
              f"dd_fwd={sel.dd_fwd.mean():.2f} no_tr={sel.no_trades.mean():.3f} ret30_med={sel.ret30.median():.2f}")
    for name, sel in list(cands.items()) + [("random econ (all econ points)", econ)]:
        res = paired(sel, m1, name)
        print(f"\n  {name}")
        for m in METRICS:
            a = res[("all", m)]
            pa, pb = res[("A", m)], res[("B", m)]
            flag = "*" if (pa[1] > 0 and pb[1] > 0) or (pa[2] < 0 and pb[2] < 0) else ""
            print(f"    {m:9} {a[0]:+.3f} [{a[1]:+.3f};{a[2]:+.3f}] A {pa[0]:+.3f} B {pb[0]:+.3f} {flag} (symbols {a[3]})")
