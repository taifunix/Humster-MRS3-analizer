"""MRS3 link: weakest-point and plateau features vs MRS3 outcomes within symbol|side|ORD.
INVALID on the current PerformanceDB: MRS3 there use source open, MRS2 points ohlc4. Rerun on the protocol-v2 DB.
Usage: mrs3_link_eval.py <work dir> (after mrs3_link_build.py)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

d = Path(sys.argv[1])
o = pd.read_pickle(d / "orders.pkl")
r = pd.read_pickle(d / "results.pkl")
r["ret_dd"] = r["pnl"] / r["dd"].where(r["dd"] > 0)
r["oos_days"] = (pd.to_datetime(r["last_ts"], utc=True) - pd.to_datetime(r["analysis_end"], utc=True)).dt.total_seconds() / 86400
r["oos30"] = 100 * np.log(r["e_last"] / r["e_at_end"]) * 30 / r["oos_days"].where(r["oos_days"] >= 5)
r["finalist"] = (r["auto_status"] == "FINALIST").astype(float)

o["p_ret_dd"] = o["p_pnl"] / o["p_dd"].where(o["p_dd"] > 0)
o["p_tr30"] = o["p_trades"] * 30 / o["p_days"]
pre_days = o["p_days"] - o["p_b_days"]
o["p_pre30"] = o["p_pre_pnl"] * 30 / pre_days.where(pre_days > 0)
o["p_b30"] = o["p_b_pnl"] * 30 / o["p_b_days"]
o["p_b_minus_pre"] = o["p_b30"] - o["p_pre30"]
o["p_b_neg"] = (o["p_b_pnl"] < 0).astype(float)
o["p_b_trades_zero"] = (o["p_b_trades"] == 0).astype(float)

feat = o.groupby("strategy_id").agg(
    symbol=("symbol", "first"), side=("side", "first"), ord=("order_count", "first"), tf=("timeframe", "first"),
    lifecycle=("lifecycle_status", "first"),
    pts_min=("plateau_point_count", "min"), pts_mean=("plateau_point_count", "mean"),
    plt_tr_min=("plateau_total_trades", "min"), base_tr_min=("base_point_trades", "min"),
    tr30_min=("p_tr30", "min"), pf_min=("p_pf", "min"), wr_min=("p_wr", "min"), eff_min=("p_eff", "min"),
    pnl_min=("p_pnl", "min"), dd_max=("p_dd", "max"), retdd_min=("p_ret_dd", "min"),
    b30_min=("p_b30", "min"), bpre_min=("p_b_minus_pre", "min"), bneg_any=("p_b_neg", "max"), bneg_n=("p_b_neg", "sum"),
)
df = feat.join(r.set_index("strategy_id"), how="inner")
df["discarded"] = (df["lifecycle"] == "DISCARDED").astype(float)
df["grp"] = df["symbol"] + "|" + df["side"] + "|" + df["ord"].astype(str)
print("strategies", len(df), "groups", df["grp"].nunique(), "with OOS>=5d", df["oos30"].notna().sum(),
      "median OOS days", df["oos_days"].median().round(1))
print(df.groupby("ord").size().to_dict())

FEATURES = ["pts_min", "pts_mean", "plt_tr_min", "base_tr_min", "tr30_min", "pf_min", "wr_min", "eff_min",
            "pnl_min", "dd_max", "retdd_min", "b30_min", "bpre_min", "bneg_n"]
OUTCOMES = ["ret_dd", "oos30", "discarded", "finalist"]


def within(df, feature, outcome, min_n=15):
    rhos, lo, hi = [], [], []
    for _, g in df[[feature, outcome, "grp"]].dropna().groupby("grp"):
        if len(g) < min_n or g[feature].nunique() < 3:
            continue
        rhos.append(g[feature].corr(g[outcome], method="spearman"))
        rk = g[feature].rank(pct=True)
        base = g[outcome].mean()
        lo.append(g.loc[rk <= 1 / 3, outcome].mean() - base)
        hi.append(g.loc[rk > 2 / 3, outcome].mean() - base)
    if not rhos:
        return None
    return np.nanmedian(rhos), np.nanmean(lo), np.nanmean(hi), len(rhos)


for outcome in OUTCOMES:
    print(f"\n=== outcome {outcome} (within symbol|side|ORD; rho median, low third vs group mean, high third)")
    for f in FEATURES:
        res = within(df, f, outcome)
        if res:
            print(f"  {f:12s} rho={res[0]:+.2f}  low3={res[1]:+.3f}  high3={res[2]:+.3f}  groups={res[3]}")
df.to_pickle(d / "strategy_frame.pkl")
