"""Within scope-cut: structure geometry (gaps, first shift, depth) vs forward outcomes."""
import sys
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
b, work = sys.argv[1], Path(sys.argv[2])
c = duckdb.connect()
rng = np.random.default_rng(14)
df = c.execute(f"""select s.*, sc.symbol, sc.half from read_parquet('{(work / 'deep_struct.parquet').as_posix()}') s
  join (select distinct scope, symbol, half from read_parquet('{b}/scopes.parquet')) sc using (scope)
  where kind='PROD'""").df()
sh = df.shifts.str.split("|").map(lambda x: [int(v) for v in x])
df["first"] = sh.map(lambda x: x[0])
df["last"] = sh.map(lambda x: x[-1])
df["min_gap"] = sh.map(lambda x: min(b_ - a for a, b_ in zip(x, x[1:])))
df["min_gap_rel"] = sh.map(lambda x: min((b_ - a) / a for a, b_ in zip(x, x[1:])))
df["span_rel"] = (df["last"] - df["first"]) / df["first"]
df["r_ex_c"] = df.r_ex.clip(-30, 30)
GEOM = ["min_gap", "min_gap_rel", "first", "last", "span_rel", "train_upi"]
TARGETS = {"r_ex_c": 1, "hit10": 1, "loss_neg": -1, "dd_fwd": -1, "breach": -1, "ret30": 1}
KEY = ["scope", "cut", "F", "n"]


def spearman(g, x, y):
    g = g[KEY + ["symbol", "half", x, y]].dropna().copy()
    g["rx"] = g.groupby(KEY)[x].rank()
    g["ry"] = g.groupby(KEY)[y].rank()
    agg = g.groupby(KEY + ["symbol", "half"])
    sx, sy, n = agg.rx.std(), agg.ry.std(), agg.size()
    cov = agg[["rx", "ry"]].cov().unstack()[("rx", "ry")]
    rho = (cov / (sx * sy)).where(n >= 10).dropna().reset_index(name="rho")
    return rho


for F in (28, 14):
    for n in (2, 3):
        sub = df[(df.F == F) & (df.n == n)]
        print(f"\n===== F={F}, {n}-order PROD structures: {len(sub)}; within scope-cut Spearman, signed + = better =====")
        print(f"{'feature':12} " + " ".join(f"{t:>26}" for t in TARGETS))
        for x in GEOM:
            cells = []
            for y, sign in TARGETS.items():
                r = spearman(sub, x, y)
                vals = []
                for subset in ("all", "A", "B"):
                    s = r if subset == "all" else r[r.half == subset]
                    ps = s.groupby("symbol").rho.mean().values * sign
                    if len(ps) < 3:
                        vals.append((np.nan, np.nan, np.nan))
                        continue
                    bo = ps[rng.integers(0, len(ps), size=(1000, len(ps)))].mean(axis=1)
                    vals.append((ps.mean(), *np.percentile(bo, [2.5, 97.5])))
                (m, lo, hi), (ma, la, ha), (mb, lb, hb) = vals
                flag = "*" if (la > 0 and lb > 0) or (ha < 0 and hb < 0) else " "
                cells.append(f"{m:+.2f}[{lo:+.2f};{hi:+.2f}]{ma:+.2f}/{mb:+.2f}{flag}")
            print(f"{x:12} " + " ".join(f"{cl:>26}" for cl in cells))

# gap buckets: outcome levels by min_gap_rel bucket, paired within scope-cut via demeaning
print("\n===== min_gap_rel buckets (demeaned within scope-cut, per-symbol mean) =====")
df["bucket"] = pd.cut(df.min_gap_rel, [0, 0.5, 1.0, 1.5, 2.5, 100], labels=["<0.5", "0.5-1", "1-1.5", "1.5-2.5", ">2.5"])
for F in (28, 14):
    for n in (2, 3):
        sub = df[(df.F == F) & (df.n == n)].copy()
        for m in ("r_ex_c", "dd_fwd", "loss_neg", "hit10", "ret30"):
            sub[m + "_d"] = sub[m] - sub.groupby(KEY)[m].transform("mean")
        tab = sub.groupby(["bucket", "symbol"])[[m + "_d" for m in ("r_ex_c", "dd_fwd", "loss_neg", "hit10", "ret30")]].mean()
        tab = tab.groupby(level="bucket").mean()
        tab["structures"] = sub.groupby("bucket").size()
        print(f"\nF={F} n={n}\n", tab.round(3).to_string())
