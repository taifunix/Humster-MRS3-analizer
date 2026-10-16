"""Non-overlapping stages S1=(T-28,T-14], S2=(T-14,T-7], S3=(T-7,T] at the T-14 cut -> next 14 days, within pool."""
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
        L = {k: float(w[k]["endpoint30"])*int(k)/3000 for k in ("7","14","28")}
        er = {k: float(w[k]["er"]) for k in ("7","14","28")}
        if any(er[k] == 0 for k in er):
            continue
        P = {k: abs(L[k]/er[k]) for k in L}
        s3, s2, s1 = L["7"], L["14"]-L["7"], L["28"]-L["14"]
        p3, p2, p1 = P["7"], P["14"]-P["7"], P["28"]-P["14"]
        if min(p1, p2, p3) <= 1e-12:
            continue
        r30 = lambda x, d: 3000*x/d
        S1, S2, S3 = r30(s1, 14), r30(s2, 7), r30(s3, 7)
        now = f["windows"]["14"]; F = float(now["endpoint30"]); R = float(f["dd"]["14"])
        rows.append({"pool": r["symbol"]+"_"+r["side"], "S1": S1, "S2": S2, "S3": S3,
                     "er1": s1/p1, "er2": s2/p2, "er3": s3/p3,
                     "min_stage": min(S1, S2, S3), "pos_stages": (S1 > 0)+(S2 > 0)+(S3 > 0),
                     "accel": S3-S1, "er_stage_min": min(s1/p1, s2/p2, s3/p3),
                     "stage_spread": np.std([S1, S2, S3]),
                     "F": F, "R": R, "bad": float(F < 0 or R > 10)})
df = pd.DataFrame(rows)
df = df[df.groupby("pool")["pool"].transform("size") >= 15]
for c in ("bad","F","R"):
    df["p_"+c] = df.groupby("pool")[c].transform("mean")
print(f"rows {len(df)}, pools {df['pool'].nunique()}, base bad {100*df['bad'].mean():.1f}%")

def top5(col, sign=1, label=None):
    dF, dB, dR, rho = [], [], [], []
    for _, g in df.groupby("pool"):
        key = g[col]*sign
        t = g.loc[key.nlargest(5).index]
        dF.append(t["F"].mean()-g["F"].mean()); dB.append(100*(t["bad"].mean()-g["bad"].mean())); dR.append(t["R"].mean()-g["R"].mean())
        rho.append(g["F"].corr(key, method="spearman"))
    print(f"{(label or col):32s} dF={np.mean(dF):+5.2f} d_bad={np.mean(dB):+5.1f}pp d_fwdDD={np.mean(dR):+5.2f} rho(F)={np.nanmedian(rho):+.2f}")

print("\n== top-5 per pool by stage feature vs pool average")
for col, sign, label in (("S1",1,"S1 return (28-14d)"),("S2",1,"S2 return (14-7d)"),("S3",1,"S3 return (last 7d)"),
                         ("min_stage",1,"worst stage return"),("accel",1,"acceleration S3-S1"),("accel",-1,"deceleration S1-S3"),
                         ("er_stage_min",1,"worst stage smoothness"),("er3",1,"last stage smoothness"),
                         ("stage_spread",-1,"most even stages")):
    top5(col, sign, label)

print("\n== number of positive stages vs pool average")
for k in (0,1,2,3):
    x = df[df["pos_stages"] == k]
    if len(x) >= 40:
        print(f"{k} positive: n={len(x):5d} share={100*len(x)/len(df):4.1f}% d_bad={100*(x['bad']-x['p_bad']).mean():+5.1f}pp d_F={(x['F']-x['p_F']).mean():+5.2f} d_fwdDD={(x['R']-x['p_R']).mean():+5.2f}")
print("\n== which stage is negative (flags) vs pool average")
for col in ("S1","S2","S3"):
    x = df[df[col] < 0]
    print(f"{col}<0: n={len(x):5d} d_bad={100*(x['bad']-x['p_bad']).mean():+5.1f}pp d_F={(x['F']-x['p_F']).mean():+5.2f} d_fwdDD={(x['R']-x['p_R']).mean():+5.2f}")
