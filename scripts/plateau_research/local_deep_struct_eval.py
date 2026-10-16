"""Paired comparison of structure pools within scope-cut."""
import sys
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
b, work = sys.argv[1], Path(sys.argv[2])
c = duckdb.connect()
rng = np.random.default_rng(13)
df = c.execute(f"""select s.*, sc.symbol, sc.half from read_parquet('{(work / 'deep_struct.parquet').as_posix()}') s
  join (select distinct scope, symbol, half from read_parquet('{b}/scopes.parquet')) sc using (scope)""").df()
df["r_ex_c"] = df.r_ex.clip(-30, 30)
METRICS = ["hit10", "hit15", "loss_neg", "breach", "dd_fwd", "ret30", "r_ex_c", "no_trades"]
KEY = ["scope", "cut", "F"]


def pool(kind, top=None):
    p = df[df.kind == kind]
    if top:
        p = p.sort_values("train_upi", ascending=False).groupby(KEY).head(top)
    return p.groupby(KEY + ["symbol", "half"])[METRICS].mean()


def paired(a, bb):
    idx = a.index.intersection(bb.index)
    d = (a.loc[idx] - bb.loc[idx]).reset_index()
    res = {}
    for subset in ("all", "A", "B"):
        s = d if subset == "all" else d[d.half == subset]
        ps = s.groupby("symbol")[METRICS].mean()
        for m in METRICS:
            v = ps[m].values
            if len(v) < 3:
                res[(subset, m)] = (np.nan, np.nan, np.nan, len(v))
                continue
            bo = v[rng.integers(0, len(v), size=(2000, len(v)))].mean(axis=1)
            res[(subset, m)] = (v.mean(), *np.percentile(bo, [2.5, 97.5]), len(v))
    return res, len(idx)


for F in (28, 14):
    print(f"\n===================== F = {F} =====================")
    sub = df[df.F == F]
    lvl = sub.groupby("kind")[METRICS].mean().round(3)
    print("levels over all structures:\n", lvl.to_string())
    prof = sub.groupby("kind").agg(per_cut=("hit10", "size"), first_shift=("shifts", lambda x: np.mean([int(v.split("|")[0]) for v in x])),
                                   last_shift=("shifts", lambda x: np.mean([int(v.split("|")[-1]) for v in x])))
    prof["per_cut"] = sub.groupby("kind").size() / sub.groupby("kind").apply(lambda g: g[KEY].drop_duplicates().shape[0])
    print(prof.round(1).to_string())
    comparisons = [
        ("PROD top1(train UPI) vs PROD all", pool("PROD", 1), pool("PROD")),
        ("ZONE0.7 all vs PROD all", pool("ZONE0.7"), pool("PROD")),
        ("ZONE0.7 all vs PROD top1", pool("ZONE0.7"), pool("PROD", 1)),
        ("ZONE0.7 top1 vs PROD top1", pool("ZONE0.7", 1), pool("PROD", 1)),
        ("ZONE0.5 all vs PROD all", pool("ZONE0.5"), pool("PROD")),
        ("ZONE0.5 top1 vs PROD top1", pool("ZONE0.5", 1), pool("PROD", 1)),
    ]
    for name, a, bb in comparisons:
        res, n_cuts = paired(a.xs(F, level="F", drop_level=False) if F in a.index.get_level_values("F") else a,
                             bb)
        print(f"\n  {name}  (paired scope-cuts: {n_cuts})")
        for m in METRICS:
            al, pa, pb = res[("all", m)], res[("A", m)], res[("B", m)]
            flag = "*" if (pa[1] > 0 and pb[1] > 0) or (pa[2] < 0 and pb[2] < 0) else ""
            print(f"    {m:9} {al[0]:+.3f} [{al[1]:+.3f};{al[2]:+.3f}] A {pa[0]:+.3f} B {pb[0]:+.3f} {flag} (sym {al[3]})")
