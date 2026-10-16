"""Top-k by train UPI of candidate structure pools vs production top-k, paired by scope-cut."""
import sys
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
b, work = sys.argv[1], Path(sys.argv[2])
c = duckdb.connect()
rng = np.random.default_rng(15)
df = c.execute(f"""select s.*, sc.symbol, sc.half from read_parquet('{(work / 'deep_struct_cand.parquet').as_posix()}') s
  join (select distinct scope, symbol, half from read_parquet('{b}/scopes.parquet')) sc using (scope)""").df()
df["first"] = df.shifts.str.split("|").str[0].astype(int)
df["last"] = df.shifts.str.split("|").str[-1].astype(int)
df["r_ex_c"] = df.r_ex.clip(-30, 30)
METRICS = ["hit10", "hit15", "loss_neg", "breach", "dd_fwd", "ret30", "r_ex_c"]
KEY = ["scope", "cut", "F"]


def top(kind, k, floor=0):
    p = df[(df.kind == kind) & (df["first"] >= floor)]
    p = p.sort_values("train_upi", ascending=False).groupby(KEY).head(k)
    return p


def summarize(sel):
    return sel.groupby(KEY + ["symbol", "half"])[METRICS].mean()


def paired(a, bb):
    idx = a.index.intersection(bb.index)
    d = (a.loc[idx] - bb.loc[idx]).reset_index()
    out = {}
    for subset in ("all", "A", "B"):
        s = d if subset == "all" else d[d.half == subset]
        ps = s.groupby("symbol")[METRICS].mean()
        for m in METRICS:
            v = ps[m].values
            if len(v) < 3:
                out[(subset, m)] = (np.nan,) * 3 + (len(v),)
                continue
            bo = v[rng.integers(0, len(v), size=(2000, len(v)))].mean(axis=1)
            out[(subset, m)] = (v.mean(), *np.percentile(bo, [2.5, 97.5]), len(v))
    return out, len(idx)


for F in (28, 14):
    print(f"\n==================== F = {F} ====================")
    for k in (1, 3):
        base = top("PROD", k)
        base = base[base.F == F]
        bsum = summarize(base)
        print(f"\n--- top{k} by train UPI; reference PROD top{k}: levels "
              + " ".join(f"{m}={base[m].mean():.3f}" for m in METRICS)
              + f" first={base['first'].mean():.0f} last={base['last'].mean():.0f}")
        variants = [("PROD floor70", "PROD", 70), ("PROD floor100", "PROD", 100),
                    ("CAND_bp", "CAND_bp", 0), ("CAND_rel1.5", "CAND_rel1.5", 0), ("CAND_rel2.0", "CAND_rel2.0", 0),
                    ("CAND_rel1.5 floor70", "CAND_rel1.5", 70), ("CAND_rel1.5 floor100", "CAND_rel1.5", 100),
                    ("CAND_rel2.0 floor100", "CAND_rel2.0", 100)]
        for name, kind, floor in variants:
            sel = top(kind, k, floor)
            sel = sel[sel.F == F]
            res, n_cuts = paired(summarize(sel), bsum)
            lv = " ".join(f"{m}={sel[m].mean():.3f}" for m in ("hit10", "loss_neg", "dd_fwd", "ret30", "r_ex_c"))
            print(f"\n  {name:22} cuts={n_cuts} first={sel['first'].mean():.0f} last={sel['last'].mean():.0f} | {lv}")
            line = []
            for m in METRICS:
                al, pa, pb = res[("all", m)], res[("A", m)], res[("B", m)]
                flag = "*" if (pa[1] > 0 and pb[1] > 0) or (pa[2] < 0 and pb[2] < 0) else ""
                line.append(f"{m} {al[0]:+.3f}[{al[1]:+.3f};{al[2]:+.3f}]{flag}")
            print("    " + " | ".join(line))
