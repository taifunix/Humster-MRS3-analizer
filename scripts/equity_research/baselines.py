"""Within-pool walk-forward for Panel metrics (B, DD5) and the old R7.3 equity quality, cut T-14 -> next 14 days."""
import json, sys
import numpy as np, pandas as pd
rows = []
with open(sys.argv[1], encoding="utf-8") as h:
    for line in h:
        r = json.loads(line); f = r.get("facts") or {}
        cut = (f.get("prefixes") or {}).get("14")
        if not cut or cut.get("status") != "READY" or f.get("status") != "READY":
            continue
        w = cut["windows"]
        if not all(k in w for k in ("7","14","28")):
            continue
        v = {k: float(w[k]["trend30"]) for k in ("7","14","28")}
        p = {k: float(w[k]["endpoint30"]) for k in ("7","14","28")}
        log = p["28"]*28/3000; days = 28.0
        pre = cut.get("pre28")
        if pre:
            pd_ = float(cut["pre28_days"]); log += float(pre["endpoint30"])*pd_/3000; days += pd_
        pnl30 = 3000*log/days
        max_dd = float(cut["max_dd"])
        dd5 = pnl30*5/max_dd if max_dd > 0 else np.nan
        # R7.3 on the 28-day horizon
        up = v["28"] > 0 and p["28"] > 0
        flat = v["28"] == 0 and p["28"] == 0
        short_decline = any(v[k] < 0 or p[k] < 0 for k in ("7","14"))
        r73 = ("WEAKENING" if short_decline else "GROWING") if up else ("FLAT" if flat else "DECLINING_OR_MIXED")
        growth = min(v["28"], p["28"])
        dd = float(cut["dd"]["14"])/100; gap = 1 - float(cut["close"])/float(cut["hwm"]["0"]["value"])
        er = max(0.0, float(w["28"]["er"]))
        score = growth*er*(1-dd)*(1-gap) if growth > 0 else growth*(1+dd+gap)
        now = f["windows"]["14"]
        F = float(now["endpoint30"]); R = float(f["dd"]["14"])
        rows.append({"pool": r["symbol"]+"_"+r["side"], "B": p["14"], "pnl30": pnl30, "DD5": dd5,
                     "low_maxdd": -max_dd, "r73": r73, "r73_score": score, "F": F, "R": R, "bad": float(F < 0 or R > 10)})
df = pd.DataFrame(rows)
df = df[df.groupby("pool")["pool"].transform("size") >= 15]
print(f"rows {len(df)}, pools {df['pool'].nunique()}, base bad {100*df['bad'].mean():.1f}%")
for c in ("bad", "F", "R"):
    df["p_"+c] = df.groupby("pool")[c].transform("mean")

def top5(col, frame, label):
    dF, dB, dR, rho = [], [], [], []
    for _, g in frame.groupby("pool"):
        g = g.dropna(subset=[col])
        if len(g) < 15: continue
        t = g.loc[g[col].nlargest(5).index]
        dF.append(t["F"].mean()-g["F"].mean()); dB.append(100*(t["bad"].mean()-g["bad"].mean())); dR.append(t["R"].mean()-g["R"].mean())
        rho.append(g["F"].corr(g[col], method="spearman"))
    print(f"{label:34s} top5 vs pool: dF={np.mean(dF):+5.2f} d_bad={np.mean(dB):+5.1f}pp d_fwdDD={np.mean(dR):+5.2f}  rho(F)={np.nanmedian(rho):+.2f}  pools={len(dF)}")

print("\n== top-5 per pool by metric (all READY strategies)")
top5("B", df, "B (last 14d return)")
top5("pnl30", df, "PnL/30d (whole history)")
top5("DD5", df, "DD5 = PnL30 x 5 / maxDD")
top5("low_maxdd", df, "lowest max DD")
top5("r73_score", df, "R7.3 score")
pass_ = df[df["r73"].isin(["GROWING","WEAKENING"])]
print("\n== same, inside R7.3 PASS (GROWING/WEAKENING)")
top5("DD5", pass_, "DD5")
top5("r73_score", pass_, "R7.3 score")
print("\n== R7.3 class vs its pool average")
for c in ("GROWING","WEAKENING","FLAT","DECLINING_OR_MIXED"):
    x = df[df["r73"] == c]
    if len(x) >= 40:
        print(f"{c:20s} n={len(x):5d} share={100*len(x)/len(df):4.1f}% d_bad={100*(x['bad']-x['p_bad']).mean():+5.1f}pp d_F={(x['F']-x['p_F']).mean():+5.2f} d_fwdDD={(x['R']-x['p_R']).mean():+5.2f}")
