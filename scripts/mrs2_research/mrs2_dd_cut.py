"""MRS2-only OOS: tighter DD caps (11/10/9/8 of the current 12) within pair groups.
Usage: mrs2_dd_cut.py <work dir> (after mrs2_oos_eval.py)
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd
x = pd.read_pickle(Path(sys.argv[1]) / "mrs2_oos.pkl")
x["active_b"] = x["b_trades_l"].fillna(0) > 0
x["keep_rate"] = x["b30_l"] / x["pnl30"].where(x["pnl30"] > 0)
other = (x["pnl_e"] > 5) & (x["wr_e"] >= 68) & (x["eff_e"] >= 3) & (x["events_e"] >= 6) & (x["dd_e"] > 0)
pop = x[other & (x["dd_e"] <= 12)].copy()
for cut in (11, 10, 9, 8):
    m = pop["dd_e"] > cut
    rows = []
    for (grp), g in pop.assign(m=m).groupby("grp"):
        a = g[g.active_b]
        if a.m.sum() >= 10 and (~a.m).sum() >= 10:
            rows.append((a.loc[a.m, "bad"].mean() - a.loc[~a.m, "bad"].mean(),
                         g.loc[g.m, "keep_rate"].median() - g.loc[~g.m, "keep_rate"].median(),
                         g.loc[g.m, "b30_l"].median() - g.loc[~g.m, "b30_l"].median(), g["symbol"].iloc[0]))
    r = pd.DataFrame(rows, columns=["loss", "keep", "b30", "sym"])
    sym = r.groupby("sym")[["loss", "keep", "b30"]].median()
    a = pop["active_b"]
    print(f"DD {cut}..12 removed {100*m.mean():4.1f}% of passing points | pooled loss {100*pop.loc[m&a,'bad'].mean():4.1f}% vs {100*pop.loc[~m&a,'bad'].mean():4.1f}% | "
          f"groups {len(r)}: removed lose more in {100*(r.loss>0).mean():3.0f}%, keep worse in {100*(r.keep<0).mean():3.0f}%, lower B30 in {100*(r.b30<0).mean():3.0f}% "
          f"(median {100*r.loss.median():+.1f}pp, B30 {r.b30.median():+.2f}) | symbols {len(sym)}: lose more in {100*(sym.loss>0).mean():3.0f}%")
