"""Which equity-geometry facts at the T-14 cut predict the next 14 days, within a pool?"""
import json
import sys

import numpy as np
import pandas as pd

EPS = 2.0


def direction(v, p):
    if v > EPS and p > EPS:
        return "UP"
    if v < -EPS and p < -EPS:
        return "DOWN"
    if abs(v) <= EPS and abs(p) <= EPS:
        return "FLAT"
    return "MIXED"


rows = []
with open(sys.argv[1], encoding="utf-8") as handle:
    for line in handle:
        rec = json.loads(line)
        now = rec.get("facts") or {}
        cut = (now.get("prefixes") or {}).get("14")
        if not cut or cut.get("status") != "READY" or now.get("status") != "READY":
            continue
        w = cut["windows"]
        if not all(k in w for k in ("7", "14", "28")):
            continue
        row = {"pool": rec["symbol"] + "_" + rec["side"]}
        for k in ("7", "14", "28"):
            row[f"v{k}"] = float(w[k]["trend30"])
            row[f"p{k}"] = float(w[k]["endpoint30"])
            row[f"er{k}"] = float(w[k]["er"])
        pre = cut.get("pre28")
        row["pre_p"] = float(pre["endpoint30"]) if pre else np.nan
        row["pre_v"] = float(pre["trend30"]) if pre else np.nan
        row["pre_er"] = float(pre["er"]) if pre else np.nan
        row["hist_days"] = float(cut.get("pre28_days") or 0) + 28
        row["dd14"] = float(cut["dd"]["14"])
        row["dd7"] = float(cut["dd"]["7"])
        row["max_dd"] = float(cut["max_dd"])
        hwm = float(cut["hwm"]["0"]["value"])
        row["dd_end"] = 100 * (1 - float(cut["close"]) / hwm)
        row["ath_count"] = float(cut.get("strict_ath_count") or 0)
        st = cut.get("stages") or [0, 0, 0]
        row["stages_with_ath"] = sum(1 for x in st if x)
        row["held"] = float(bool(cut.get("held_weekly_breakout")))
        row["d28"] = direction(row["v28"], row["p28"])
        # outcome over the next 14 days, from facts at T
        row["F"] = float(now["windows"]["14"]["endpoint30"])
        row["R"] = float(now["dd"]["14"])
        rows.append(row)

df = pd.DataFrame(rows)
df["bad"] = ((df["F"] < 0) | (df["R"] > 10)).astype(float)
df["s_min"] = df[["v28", "p28", "v14", "p14", "v7", "p7"]].min(axis=1)
df["p28_per_dd"] = df["p28"] / df["max_dd"].clip(lower=1)
df["er_min"] = df[["er28", "er14", "er7"]].min(axis=1)
print(f"rows {len(df)}, pools {df['pool'].nunique()}, base bad-rate {df['bad'].mean():.3f}, mean F {df['F'].mean():.2f}")

# higher-is-better orientation for "lower is safer" facts
features = {
    "speed s_min": ("s_min", +1), "p28": ("p28", +1), "p7 (last week)": ("p7", +1),
    "pre28 return": ("pre_p", +1), "smoothness er28": ("er28", +1), "smoothness er_min": ("er_min", +1),
    "pre28 smoothness": ("pre_er", +1), "low max DD (history)": ("max_dd", -1), "low DD14": ("dd14", -1),
    "near high (low dd_end)": ("dd_end", -1), "return / max DD": ("p28_per_dd", +1),
    "ATH count": ("ath_count", +1), "history length": ("hist_days", +1),
}


def evaluate(frame, label):
    print(f"\n== {label}: top-5 per pool by feature vs pool average (pools with >=15)")
    print(f"{'feature':26s} {'dF top5':>8s} {'d bad%':>7s} {'rho F':>6s} {'rho bad':>8s}")
    pools = [g for _, g in frame.groupby("pool") if len(g) >= 15]
    for name, (col, sign) in features.items():
        dF, dBad, rhoF, rhoB = [], [], [], []
        for g in pools:
            g = g.dropna(subset=[col])
            if len(g) < 15:
                continue
            key = g[col] * sign
            top = g.loc[key.nlargest(5).index]
            dF.append(top["F"].mean() - g["F"].mean())
            dBad.append(100 * (top["bad"].mean() - g["bad"].mean()))
            rhoF.append(g["F"].corr(key, method="spearman"))
            rhoB.append(g["bad"].corr(key, method="spearman"))
        if dF:
            print(f"{name:26s} {np.mean(dF):+8.2f} {np.mean(dBad):+7.1f} {np.nanmedian(rhoF):+6.2f} {np.nanmedian(rhoB):+8.2f}  ({len(dF)} pools)")


evaluate(df[df["d28"] == "UP"], "candidates with W28=UP")
evaluate(df, "all READY strategies")
