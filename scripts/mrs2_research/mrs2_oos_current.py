"""MRS2-only OOS: each current economic threshold alone, compared within pair groups.
Usage: mrs2_oos_current.py <work dir> (after mrs2_oos_eval.py)
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd
x = pd.read_pickle(Path(sys.argv[1]) / "mrs2_oos.pkl")
x["active_b"] = x["b_trades_l"].fillna(0) > 0
x["keep_rate"] = x["b30_l"] / x["pnl30"].where(x["pnl30"] > 0)
x["plateau_points_e"] = pd.to_numeric(x["plateau_points_e"], errors="coerce")
allp = x[x["events_e"] >= 6].copy()
rules = {"pnl>5": allp["pnl_e"] > 5, "WR>=68": allp["wr_e"] >= 68, "DD<=12": allp["dd_e"] <= 12, "eff>=3": allp["eff_e"] >= 3,
         "all four": (allp["pnl_e"] > 5) & (allp["wr_e"] >= 68) & (allp["dd_e"] <= 12) & (allp["eff_e"] >= 3),
         "in plateau (given all four)": None}
for name, m in rules.items():
    pop = allp
    if m is None:
        pop = allp[rules["all four"]]; m = pop["in_plateau"]
    rows = []
    for _, g in pop.assign(m=m).groupby("grp"):
        a = g[g.active_b]
        if a.m.sum() >= 10 and (~a.m).sum() >= 10:
            rows.append((a.loc[~a.m, "bad"].mean() - a.loc[a.m, "bad"].mean(), g.loc[g.m, "keep_rate"].median() - g.loc[~g.m, "keep_rate"].median(),
                         g.loc[g.m, "b30_l"].median() - g.loc[~g.m, "b30_l"].median()))
    r = np.array(rows)
    print(f"{name:28s} pass {100*m.mean():5.1f}% | groups {len(r):3d}: pass has fewer B losses in {100*np.mean(r[:,0]>0):3.0f}%, better kept-rate in {100*np.mean(r[:,1]>0):3.0f}%, higher B30 in {100*np.mean(r[:,2]>0):3.0f}% "
          f"| median diff loss {100*np.median(r[:,0]):+5.1f}pp, B30 {np.median(r[:,2]):+5.2f}")
