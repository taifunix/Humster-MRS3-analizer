import runpy, sys
import numpy as np, pandas as pd
sys.argv = [sys.argv[0], sys.argv[1]]
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    ns = runpy.run_path(sys.argv[0].replace("smooth.py", "predictors.py"), run_name="lib")
df = ns["df"]
up = df[df["d28"] == "UP"].copy()
up["FR"] = up["F"] / up["R"].clip(lower=1)
print("er28 quantiles (W28=UP):", up["er28"].quantile([.1,.25,.5,.75,.9]).round(3).to_dict())
print("er_min quantiles:", up["er_min"].quantile([.1,.25,.5,.75,.9]).round(3).to_dict())
print("\n== by er28 bins (W28=UP)")
bins = [-1, .15, .25, .35, .45, 1.01]
up["bin"] = pd.cut(up["er28"], bins)
for b, g in up.groupby("bin", observed=True):
    print(f"{str(b):14s} n={len(g):5d} F={g['F'].mean():5.1f} fwdDD={g['R'].mean():4.1f} F/DD(med)={g['FR'].median():4.2f} bad={100*g['bad'].mean():4.1f}% P(F>0)={(g['F']>0).mean():.2f}")
print("\n== top-5 per pool vs pool average, risk-adjusted (pools>=15)")
for col in ("er28", "er_min", "s_min"):
    d_fr, d_r, d_f = [], [], []
    for _, g in up.groupby("pool"):
        if len(g) < 15: continue
        top = g.loc[g[col].nlargest(5).index]
        d_fr.append(top["FR"].median() - g["FR"].median()); d_r.append(top["R"].mean() - g["R"].mean()); d_f.append(top["F"].mean() - g["F"].mean())
    print(f"{col:7s} dF={np.mean(d_f):+5.2f} d fwdDD={np.mean(d_r):+5.2f} d F/DD(median)={np.mean(d_fr):+5.2f} share pools F/DD better={np.mean(np.array(d_fr)>0):.2f}")
print("\n== pair level vs within pair")
pools = up.groupby("pool").agg(n=("F","size"), er=("er28","median"), bad=("bad","mean"), dd=("R","mean"), F=("F","mean"))
pools = pools[pools["n"] >= 15]
print(f"pools={len(pools)} spearman(pool median er, pool bad-rate)={pools['er'].corr(pools['bad'], method='spearman'):+.2f}, vs fwdDD={pools['er'].corr(pools['dd'], method='spearman'):+.2f}")
share_between = up.groupby("pool")["er28"].transform("median").var() / up["er28"].var()
print(f"share of er28 variance explained by pair: {share_between:.2f}")
up["er_rank"] = up.groupby("pool")["er28"].rank(pct=True)
print("within-pool er28 tercile -> bad-rate, fwdDD, F:")
for lo, hi in ((0, 1/3), (1/3, 2/3), (2/3, 1.01)):
    g = up[(up["er_rank"] > lo) & (up["er_rank"] <= hi)]
    print(f"  {lo:.2f}-{min(hi,1):.2f}: bad={100*g['bad'].mean():4.1f}% fwdDD={g['R'].mean():4.2f} F={g['F'].mean():5.2f}")
print("\npools by median er28 (top/bottom 6):")
print(pools.sort_values("er").round(2).iloc[list(range(6)) + list(range(-6, 0))].to_string())
