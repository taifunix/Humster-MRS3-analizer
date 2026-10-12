"""MRS3 link: absolute point thresholds -> share of tests removed and finalists lost.
INVALID on the current PerformanceDB (open vs ohlc4). Rerun on the protocol-v2 DB.
Usage: mrs3_link_thresholds.py <work dir> (after mrs3_link_eval.py)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

d = Path(sys.argv[1])
df = pd.read_pickle(d / "strategy_frame.pkl")
a = df[df["lifecycle"] == "ACTIVE"].copy()
F = a["finalist"].sum()
print(f"ACTIVE {len(a)}, finalists {int(F)}; rule -> share of tests removed, share of finalists lost, finalist rate kept/removed")


def rule(name, mask):
    m = mask.fillna(False)
    removed = m.mean()
    lost = a.loc[m, "finalist"].sum() / F
    kept_rate = a.loc[~m, "finalist"].mean() * 100
    rem_rate = a.loc[m, "finalist"].mean() * 100 if m.any() else float("nan")
    print(f"  {name:38s} removed {100 * removed:5.1f}%  finalists lost {100 * lost:5.1f}%  rate kept {kept_rate:4.2f}% / removed {rem_rate:4.2f}%")


for x in (1.5, 2, 2.5, 3):
    rule(f"weakest point PF < {x}", a["pf_min"] < x)
for x in (-10, -5, -2, 0):
    rule(f"weakest point B30-pre30 < {x} pp", a["bpre_min"] < x)
for x in (-5, 0, 2):
    rule(f"weakest point B30 < {x} pp", a["b30_min"] < x)
for x in (5, 8, 10):
    rule(f"weakest plateau points < {x}", a["pts_min"] < x)
for x in (3, 4):
    rule(f"weakest point eff < {x}", a["eff_min"] < x)
rule("PF<2 or B30-pre30<-5", (a["pf_min"] < 2) | (a["bpre_min"] < -5))
rule("PF<2 or B30<0", (a["pf_min"] < 2) | (a["b30_min"] < 0))
print("\npts_min quantiles", a["pts_min"].quantile([.05, .1, .25, .5]).to_dict())
print("pf_min quantiles", a["pf_min"].quantile([.1, .25, .5, .75]).round(2).to_dict())
print("bpre_min quantiles", a["bpre_min"].quantile([.1, .25, .5, .75]).round(2).to_dict())
# per-pair spread: is the PF effect consistent across pairs?
x = a.dropna(subset=["pf_min"]).copy()
x["low"] = x.groupby("grp")["pf_min"].rank(pct=True) <= 1 / 3
per = x.groupby("grp").apply(lambda g: pd.Series({"f": g["finalist"].sum(), "low_rate": g.loc[g.low, "finalist"].mean(), "rest_rate": g.loc[~g.low, "finalist"].mean()}))
per = per[per["f"] >= 5]
print(f"\ngroups with >=5 finalists: {len(per)}; PF low third has lower finalist rate in {100 * (per.low_rate < per.rest_rate).mean():.0f}% of them")
x["lowb"] = x.groupby("grp")["bpre_min"].rank(pct=True) <= 1 / 3
per = x.dropna(subset=["bpre_min"]).groupby("grp").apply(lambda g: pd.Series({"f": g["finalist"].sum(), "low_rate": g.loc[g.lowb, "finalist"].mean(), "rest_rate": g.loc[~g.lowb, "finalist"].mean()}))
per = per[per["f"] >= 5]
print(f"groups with >=5 finalists: {len(per)}; B-deterioration low third lower finalist rate in {100 * (per.low_rate < per.rest_rate).mean():.0f}% of them")
print("\nfinalist reasons context: auto_reason of ACTIVE non-finalists in PF<2:",
      a.loc[a["pf_min"] < 2, "auto_reason"].str.split(";").str[0].value_counts().head(6).to_dict())
print("same for PF>=2:", a.loc[a["pf_min"] >= 2, "auto_reason"].str.split(";").str[0].value_counts().head(6).to_dict())
