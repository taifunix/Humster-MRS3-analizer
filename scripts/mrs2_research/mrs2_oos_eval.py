"""MRS2-only OOS: pair earlier/later analyses, link points, sweep point thresholds (pooled view).
Reads mrs2_points.pkl/mrs2_meta.pkl from mrs2_oos_build.py; writes mrs2_oos.pkl.
Usage: mrs2_oos_eval.py <work dir>
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

d = Path(sys.argv[1])
pts = pd.read_pickle(d / "mrs2_points.pkl")
meta = pd.read_pickle(d / "mrs2_meta.pkl")
meta["end"] = pd.to_datetime(meta["end"], utc=True)
# Some early analyses store PRETEST a_pnl/b_pnl in account currency (10x the %); rescale them to %.
ratio = (pts["pnl"] / pts["a_pnl"]).where(pts["a_pnl"].abs() > 1e-9).groupby(pts["aid"]).median()
factor = ratio.round(1).fillna(1.0)
assert set(factor.unique()) <= {0.1, 1.0}, factor.to_dict()
pts["a_pnl"] = pts["a_pnl"] * pts["aid"].map(factor)
pts["b_pnl"] = pts["b_pnl"] * pts["aid"].map(factor)
print("PRETEST pnl rescaled for", int((factor != 1.0).sum()), "analyses")
pairs = []
for _, e in meta.iterrows():
    for _, l in meta.iterrows():
        if l["aid"] == e["aid"] or l["b_start"] is None or pd.isna(l["b_start"]):
            continue
        common = set(e["symbols"]) & set(l["symbols"])
        if common and l["b_start"] >= e["end"]:
            for s in common:
                pairs.append((e["aid"], l["aid"], s, (l["b_start"] - e["end"]).days))
pairs = pd.DataFrame(pairs, columns=["aid_e", "aid_l", "symbol", "gap_days"])
print("analysis pairs", len(pairs[["aid_e", "aid_l"]].drop_duplicates()), "symbol links", len(pairs))
print(pairs.groupby("symbol").size().to_dict())

E = pts.rename(columns=lambda c: c if c in ("point_id", "symbol") else c + "_e")
L = pts[["aid", "point_id", "b_pnl", "b_days", "b_trades", "ab_status", "pnl", "days"]].rename(columns=lambda c: c if c == "point_id" else c + "_l")
x = pairs.merge(E, left_on=["aid_e", "symbol"], right_on=["aid_e", "symbol"])
x = x.merge(L, left_on=["aid_l", "point_id"], right_on=["aid_l", "point_id"])
x = x[x["b_days_l"].fillna(0) > 0].copy()
x["b30_l"] = x["b_pnl_l"] * 30 / x["b_days_l"]
x["bad"] = (x["b_pnl_l"] < 0).astype(float)
x["grp"] = x["aid_e"].str[:8] + "|" + x["aid_l"].str[:8] + "|" + x["symbol"] + "|" + x["side_e"] + "|" + x["tf_e"]
x["rel"] = x["b30_l"] - x.groupby("grp")["b30_l"].transform("median")
x["pnl30"] = x["pnl_e"] * 30 / x["days_e"]
x["tr30"] = x["trades_e"] * 30 / x["days_e"]
pre_days = x["days_e"] - x["b_days_e"]
x["pre30_e"] = (x["a_pnl_e"] - x["b_pnl_e"]) * 30 / pre_days.where(pre_days > 0)
x["b30_e"] = x["b_pnl_e"] * 30 / x["b_days_e"]
x["ab_ratio_e"] = x["b30_e"] / x["pre30_e"].where(x["pre30_e"] > 0)
x["in_plateau"] = x["plateau_id_e"].notna()
x["plateau_points_e"] = pd.to_numeric(x["plateau_points_e"], errors="coerce")
x["active_b"] = x["b_trades_l"].fillna(0) > 0
x["keep_rate"] = x["b30_l"] / x["pnl30"].where(x["pnl30"] > 0)
x["econ_now"] = (x["pnl_e"] > 5) & (x["dd_e"] > 0) & (x["wr_e"] >= 68) & (x["dd_e"] <= 12) & (x["eff_e"] >= 3) & (x["events_e"] >= 6)
print(f"linked point observations {len(x)}, groups {x['grp'].nunique()}, base bad (B loss) {100 * x['bad'].mean():.1f}%")
x.to_pickle(d / "mrs2_oos.pkl")


def report(sub, label):
    print(f"\n### {label}: n={len(sub)}, bad={100 * sub['bad'].mean():.1f}%, rel B30 median={sub['rel'].median():+.2f}")


report(x, "all points")
report(x[x["econ_now"]], "pass current economic rule (pnl>5, WR>=68, DD<=12, eff>=3, events>=6)")
report(x[~x["econ_now"]], "fail current economic rule")
report(x[x["econ_now"] & x["in_plateau"]], "economic pass and in a plateau")

base = x[x["econ_now"] & x["in_plateau"]].copy()


def sweep(col, thresholds, op, pop, name):
    print(f"\n{name} (population: economic pass and in plateau, n={len(pop)}, bad {100 * pop['bad'].mean():.1f}%)")
    for t in thresholds:
        m = op(pop[col], t).fillna(False)
        if m.sum() == 0:
            continue
        a = pop["active_b"]
        print(f"  cut {col} {op.__name__} {t}: removes {100 * m.mean():5.1f}%  loss-in-B (active) removed {100 * pop.loc[m & a, 'bad'].mean():5.1f}% vs kept {100 * pop.loc[~m & a, 'bad'].mean():5.1f}%  "
              f"kept-rate removed {pop.loc[m, 'keep_rate'].median():+5.2f} kept {pop.loc[~m, 'keep_rate'].median():+5.2f}  quiet-B removed {100 * (~pop.loc[m, 'active_b']).mean():4.1f}%")


lt = np.less
sweep("pf_e", (1.5, 2, 2.5, 3), lt, base, "Profit factor")
sweep("wr_e", (70, 72, 75, 80), lt, base, "Win rate")
sweep("eff_e", (4, 5, 6, 8), lt, base, "Efficiency (PnL/DD)")
sweep("pnl30", (3, 5, 8, 12), lt, base, "PnL/30")
sweep("tr30", (5, 10, 20), lt, base, "Trades/30")
sweep("plateau_points_e", (8, 10, 15, 20), lt, base, "Plateau points")
sweep("ab_ratio_e", (0, 0.25, 0.5), lt, base, "Own A/B: B30/pre30")
sweep("b30_e", (0,), lt, base, "Own B30 (earlier analysis)")
gt = np.greater
sweep("dd_e", (6, 8, 10), gt, base, "DD above")

print("\nWithin-group Spearman (median over groups with >=20 obs) of feature vs later B30, economic pass + plateau")
for col in ("pf_e", "wr_e", "eff_e", "pnl30", "dd_e", "tr30", "plateau_points_e", "ab_ratio_e", "b30_e", "pre30_e"):
    rh = []
    for _, g in base[[col, "b30_l", "grp"]].dropna().groupby("grp"):
        if len(g) >= 20 and g[col].nunique() >= 3:
            rh.append(g[col].corr(g["b30_l"], method="spearman"))
    print(f"  {col:16s} rho={np.nanmedian(rh):+.3f}  positive in {100 * np.mean(np.array(rh) > 0):.0f}% of {len(rh)} groups")

print("\nPF < 2.5 within trade-frequency terciles (economic pass + plateau)")
b = base.copy()
b["trq"] = b.groupby("grp")["tr30"].rank(pct=True).map(lambda v: "low" if v <= 1/3 else ("high" if v > 2/3 else "mid"))
for q, g in b.groupby("trq"):
    m = g["pf_e"] < 2.5; a = g["active_b"]
    print(f"  trades {q:4s}: n={len(g)} PF<2.5 share {100*m.mean():4.1f}%  loss-in-B removed {100*g.loc[m&a,'bad'].mean():5.1f}% kept {100*g.loc[~m&a,'bad'].mean():5.1f}%  kept-rate removed {g.loc[m,'keep_rate'].median():+.2f} kept {g.loc[~m,'keep_rate'].median():+.2f}")
print("\nCurrent rule components on ALL points (each criterion alone, others ignored): kept-rate and loss-in-B")
allp = x[x["events_e"] >= 6]
for name, m in (("pnl>5", allp["pnl_e"] > 5), ("WR>=68", allp["wr_e"] >= 68), ("DD<=12", allp["dd_e"] <= 12), ("eff>=3", allp["eff_e"] >= 3)):
    a = allp["active_b"]
    print(f"  {name:7s} pass {100*m.mean():5.1f}%  loss-in-B pass {100*allp.loc[m&a,'bad'].mean():5.1f}% fail {100*allp.loc[~m&a,'bad'].mean():5.1f}%  kept-rate pass {allp.loc[m,'keep_rate'].median():+.2f} fail {allp.loc[~m,'keep_rate'].median():+.2f}")
