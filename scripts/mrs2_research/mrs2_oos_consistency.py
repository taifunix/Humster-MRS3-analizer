"""MRS2-only OOS: per-symbol/per-group consistency of candidate rules, A/B, DD and PF buckets.
Usage: mrs2_oos_consistency.py <work dir> (after mrs2_oos_eval.py)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

d = Path(sys.argv[1])
x = pd.read_pickle(d / "mrs2_oos.pkl")
x["plateau_points_e"] = pd.to_numeric(x["plateau_points_e"], errors="coerce")
x["active_b"] = x["b_trades_l"].fillna(0) > 0
x["keep_rate"] = x["b30_l"] / x["pnl30"].where(x["pnl30"] > 0)
base = x[x["econ_now"] & x["in_plateau"]].copy()
base["sym"] = base["symbol"]
print(f"population n={len(base)}, symbols {base['sym'].nunique()}, groups {base['grp'].nunique()}")

RULES = {
    "DD > 8": base["dd_e"] > 8,
    "DD > 6": base["dd_e"] > 6,
    "PF < 2.5": base["pf_e"] < 2.5,
    "PF < 2": base["pf_e"] < 2,
    "plateau points < 10": base["plateau_points_e"] < 10,
    "own A/B ratio in [0, 0.5)": (base["ab_ratio_e"] >= 0) & (base["ab_ratio_e"] < 0.5),
    "own A/B ratio < 0 (B loss)": base["ab_ratio_e"] < 0,
    "WR < 75": base["wr_e"] < 75,
    "eff < 5": base["eff_e"] < 5,
}


def consistency(mask, by):
    rows = []
    for key, g in base.assign(m=mask).groupby(by):
        a = g[g["active_b"]]
        if a["m"].sum() >= 10 and (~a["m"]).sum() >= 10:
            rows.append((a.loc[a.m, "bad"].mean() - a.loc[~a.m, "bad"].mean(),
                         g.loc[g.m, "keep_rate"].median() - g.loc[~g.m, "keep_rate"].median()))
    r = np.array(rows)
    if not len(r):
        return "n/a"
    return f"{len(r):3d} {by:4s}: loss-in-B worse in {100 * np.mean(r[:, 0] > 0):3.0f}%, kept-rate worse in {100 * np.mean(r[:, 1] < 0):3.0f}%"


for name, m in RULES.items():
    a = base["active_b"]
    print(f"\n{name}: removes {100 * m.mean():.1f}%; loss-in-B {100 * base.loc[m & a, 'bad'].mean():.1f}% vs {100 * base.loc[~m & a, 'bad'].mean():.1f}%; "
          f"kept-rate {base.loc[m, 'keep_rate'].median():+.2f} vs {base.loc[~m, 'keep_rate'].median():+.2f}")
    print("   ", consistency(m, "sym"))
    print("   ", consistency(m, "grp"))

print("\nOwn A/B ratio buckets (earlier analysis B30 / pre-B30), economic pass + plateau, pre30 > 0")
b = base[base["ab_ratio_e"].notna()].copy()
b["bucket"] = pd.cut(b["ab_ratio_e"], [-np.inf, -0.5, 0, 0.25, 0.5, 1, 2, np.inf])
g = b.groupby("bucket", observed=True).apply(lambda s: pd.Series({
    "n": len(s), "loss_in_B_%": 100 * s.loc[s.active_b, "bad"].mean(), "kept_rate": s["keep_rate"].median(),
    "quiet_B_%": 100 * (~s.active_b).mean()}))
print(g.round(2).to_string())
print("\nDD buckets")
b = base.copy()
b["bucket"] = pd.cut(b["dd_e"], [0, 2, 4, 6, 8, 10, 12])
print(b.groupby("bucket", observed=True).apply(lambda s: pd.Series({
    "n": len(s), "loss_in_B_%": 100 * s.loc[s.active_b, "bad"].mean(), "kept_rate": s["keep_rate"].median(),
    "B30_median": s["b30_l"].median(), "pnl30_median": s["pnl30"].median()})).round(2).to_string())
print("\nPF buckets")
b["bucket"] = pd.cut(b["pf_e"], [0, 1.5, 2, 2.5, 3, 4, 6, np.inf])
print(b.groupby("bucket", observed=True).apply(lambda s: pd.Series({
    "n": len(s), "loss_in_B_%": 100 * s.loc[s.active_b, "bad"].mean(), "kept_rate": s["keep_rate"].median(),
    "B30_median": s["b30_l"].median(), "dd_median": s["dd_e"].median(), "tr30_median": s["tr30"].median()})).round(2).to_string())
print("\nGap between earlier end and later B start (days):", base["gap_days"].describe().round(1).to_dict())
