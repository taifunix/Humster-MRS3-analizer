"""MRS3 link: finalist rate by within-group tercile; order-2 marginal gain vs the 1ORD base on the same first point.
INVALID on the current PerformanceDB (open vs ohlc4). Rerun on the protocol-v2 DB.
Usage: mrs3_link_finalists.py <work dir> (after mrs3_link_eval.py)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

d = Path(sys.argv[1])
df = pd.read_pickle(d / "strategy_frame.pkl")
o = pd.read_pickle(d / "orders.pkl")
active = df[df["lifecycle"] == "ACTIVE"].copy()
print("active", len(active), "finalists", int(active["finalist"].sum()), "rate", round(100 * active["finalist"].mean(), 2))

FEATURES = ["pts_min", "pts_mean", "plt_tr_min", "tr30_min", "pf_min", "wr_min", "eff_min", "pnl_min", "dd_max", "b30_min", "bpre_min"]


def terciles(x, feature, outcome):
    x = x[[feature, outcome, "grp"]].dropna()
    keep = x.groupby("grp")[feature].transform(lambda s: len(s) >= 15 and s.nunique() >= 3).astype(bool)
    x = x[keep].copy()
    x["t"] = x.groupby("grp")[feature].rank(pct=True).map(lambda v: 0 if v <= 1 / 3 else (2 if v > 2 / 3 else 1))
    return x.groupby("t")[outcome].agg(["mean", "sum", "count"])


print("\nFinalist rate (%) by within-group tercile of the feature (ACTIVE only); low / mid / high, finalists in low third")
for f in FEATURES:
    t = terciles(active, f, "finalist")
    print(f"  {f:12s} " + " / ".join(f"{100 * v:5.2f}" for v in t["mean"]) + f"   finalists low third={int(t['sum'].iloc[0])} of {int(t['sum'].sum())}")

print("\nMRS3 ret/DD median by tercile (all)")
for f in FEATURES:
    x = df[[f, "ret_dd", "grp"]].dropna()
    keep = x.groupby("grp")[f].transform(lambda s: len(s) >= 15 and s.nunique() >= 3).astype(bool)
    x = x[keep].copy()
    x["t"] = x.groupby("grp")[f].rank(pct=True).map(lambda v: 0 if v <= 1 / 3 else (2 if v > 2 / 3 else 1))
    # normalize ret/dd by group median so pairs are comparable
    x["rel"] = x["ret_dd"] / x.groupby("grp")["ret_dd"].transform("median")
    print(f"  {f:12s} " + " / ".join(f"{v:5.2f}" for v in x.groupby('t')['rel'].median()))

# Marginal contribution: 2ORD vs tested 1ORD whose single point equals order 1 of the 2ORD
o["key1"] = o["analysis_run_id"] + "|" + o["point_id"]
one = o[o["order_count"] == 1][["strategy_id", "key1"]].merge(df[["ret_dd"]], left_on="strategy_id", right_index=True)
one = one.groupby("key1")["ret_dd"].median().rename("base_ret_dd")
two = o[o["order_count"] == 2].pivot_table(index="strategy_id", columns="order_id", values=["key1", "p_eff", "p_pf", "p_dd", "p_pnl", "plateau_point_count", "p_trades", "p_b_pnl", "p_days"], aggfunc="first")
two.columns = [f"{a}_{b}" for a, b in two.columns]
two = two.join(df[["ret_dd", "grp"]], how="inner")
two = two.merge(one, left_on="key1_1", right_index=True)
two["gain"] = np.log(two["ret_dd"].clip(lower=1e-6) / two["base_ret_dd"].clip(lower=1e-6))
two["eff_ratio"] = two["p_eff_2"].astype(float) / two["p_eff_1"].astype(float)
print(f"\n2ORD with a tested 1ORD base on the same first point: {len(two)}; median gain log(retDD2/retDD1)={two['gain'].median():+.3f}; share improved={100 * (two['gain'] > 0).mean():.1f}%")
for col, label in (("eff_ratio", "order2 eff / order1 eff"), ("p_eff_2", "order2 eff"), ("p_pf_2", "order2 PF"),
                   ("plateau_point_count_2", "order2 plateau points"), ("p_trades_2", "order2 trades")):
    x = two[[col, "gain"]].dropna().astype(float)
    q = pd.qcut(x[col], 5, duplicates="drop")
    g = x.groupby(q, observed=True)["gain"].agg(["median", lambda s: 100 * (s > 0).mean(), "count"])
    g.columns = ["median_gain", "improved_%", "n"]
    print(f"\n  by quintile of {label}:\n" + g.round(3).to_string())
