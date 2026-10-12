"""Walk-forward calibration of the STRONG/WEAK speed boundary on frozen M3 facts.

Labels are assigned on the prefix cut T-14 (only data before the cut); the
outcome is what the same curve did over the next 14 days (facts at T):
forward return F = W14 endpoint30 at T (log-pp per 30 days), forward risk =
max DD inside W14 at T (from the historical HWM).
"""
import json
import math
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

EPS = 2.0
DD_LIMIT = 23.0


def direction(v, p):
    if v > EPS and p > EPS:
        return "UP"
    if v < -EPS and p < -EPS:
        return "DOWN"
    if abs(v) <= EPS and abs(p) <= EPS:
        return "FLAT"
    return "MIXED"


def window(facts, days):
    w = (facts.get("windows") or {}).get(str(days))
    if not w:
        return None
    return float(w["trend30"]), float(w["endpoint30"]), float(w.get("er") or 0)


def snapshot(facts):
    if not facts or facts.get("status") != "READY":
        return None
    w28, w14, w7 = window(facts, 28), window(facts, 14), window(facts, 7)
    if not (w28 and w14 and w7):
        return None
    pre = facts.get("pre28")
    pre_dir = direction(float(pre["trend30"]), float(pre["endpoint30"])) if pre else None
    stages = facts.get("stages") or [0, 0, 0]
    close = float(facts["close"])
    hwm_end = float(facts["hwm"]["0"]["value"])
    return {
        "v28": w28[0], "p28": w28[1], "er28": w28[2],
        "v14": w14[0], "p14": w14[1], "er14": w14[2],
        "v7": w7[0], "p7": w7[1], "er7": w7[2],
        "d28": direction(w28[0], w28[1]), "d14": direction(w14[0], w14[1]), "d7": direction(w7[0], w7[1]),
        "pre": pre_dir, "dd14": float(facts["dd"]["14"]), "dd7": float(facts["dd"]["7"]),
        "held": bool(facts.get("held_weekly_breakout")), "stages": [int(x) for x in stages],
        "dd_end": 100.0 * (1.0 - close / hwm_end) if hwm_end > 0 else None,
    }


def label(s, d_min=3.0):
    if s is None:
        return "N/A"
    if s["dd14"] >= DD_LIMIT:
        return "DEEP_DRAWDOWN"
    if s["d28"] == "DOWN" or (s["d28"] in ("FLAT", "MIXED") and s["pre"] != "UP"):
        return "DECLINING"
    if s["stages"][2] == 0:
        return "STALLED" if (s["dd_end"] is not None and s["dd_end"] <= d_min) else "CORRECTING"
    if not s["held"]:
        return "CORRECTING"
    growth = (s["d28"] == s["d14"] == s["d7"] == "UP" and all(x > 0 for x in s["stages"])
              and s["pre"] in (None, "UP"))
    if growth:
        return "GROWTH"
    return "RESUMED" if s["d7"] == "UP" else "STALLED"


rows = []
with open(sys.argv[1], encoding="utf-8") as handle:
    for line in handle:
        record = json.loads(line)
        facts = record.get("facts") or {}
        now = snapshot(facts)
        cut = snapshot((facts.get("prefixes") or {}).get("14"))
        cut7 = snapshot((facts.get("prefixes") or {}).get("7"))
        if now is None:
            continue
        row = {"result_id": record["result_id"], "symbol": record["symbol"], "side": record["side"],
               "F14": now["p14"], "R14": now["dd14"], "F7": now["p7"], "R7": now["dd7"]}
        if cut is not None:
            row.update({f"c_{k}": v for k, v in cut.items() if not isinstance(v, list)})
            row["c_label"] = label(cut)
        if cut7 is not None:
            row.update({f"c7_{k}": v for k, v in cut7.items() if not isinstance(v, list)})
            row["c7_label"] = label(cut7)
        rows.append(row)

df = pd.DataFrame(rows)
df["pool"] = df["symbol"] + "_" + df["side"]
print("records:", len(df), "with cut14:", df["c_label"].notna().sum(), "with cut7:", df["c7_label"].notna().sum())


def outcome(frame, f="F14", r="R14"):
    return {"n": len(frame), "F_mean": round(frame[f].mean(), 2), "F_median": round(frame[f].median(), 2),
            "P(F>0)": round((frame[f] > 0).mean(), 3), "fwdDD_mean": round(frame[r].mean(), 2)}


print("\n== Forward 14d outcome by label at the T-14 cut")
for name, group in df.dropna(subset=["c_label"]).groupby("c_label"):
    print(f"{name:14s}", outcome(group))

g = df[df["c_label"] == "GROWTH"].copy()
speed_cols = ["c_v28", "c_p28", "c_v14", "c_p14", "c_v7", "c_p7"]
g["s_min"] = g[speed_cols].min(axis=1)
g["s_base"] = g[["c_v28", "c_p28"]].min(axis=1)
g["slow"] = (g["c_v14"] <= 0.8 * g["c_v28"]) & (g["c_v7"] <= 0.8 * g["c_v14"])
print("\nGROWTH at cut:", len(g), "pools:", g["pool"].nunique(), "two-step slowdown share:", round(g["slow"].mean(), 3))

print("\n== Deciles of s_min (minimum of v and p over W28/W14/W7) inside GROWTH")
g["dec"] = pd.qcut(g["s_min"], 10, labels=False, duplicates="drop")
for d, group in g.groupby("dec"):
    lo, hi = group["s_min"].min(), group["s_min"].max()
    print(f"d{d} s_min {lo:6.1f}..{hi:6.1f}", outcome(group))

overall = g["F14"].corr(g["s_min"], method="spearman")
within = [grp["F14"].corr(grp["s_min"], method="spearman") for _, grp in g.groupby("pool") if len(grp) >= 10]
print(f"\nSpearman(s_min, F14): overall {overall:.3f}; within pools (n>=10): median {np.nanmedian(within):.3f}, "
      f"mean {np.nanmean(within):.3f}, share>0 {np.mean(np.array(within) > 0):.2f}, pools {len(within)}")


def within_pool_gap(frame, mask, f="F14"):
    gaps, weights = [], []
    for _, grp in frame.groupby("pool"):
        a, b = grp[mask.loc[grp.index]], grp[~mask.loc[grp.index]]
        if len(a) >= 3 and len(b) >= 3:
            gaps.append(a[f].mean() - b[f].mean())
            weights.append(min(len(a), len(b)))
    if not gaps:
        return None, 0
    return float(np.average(gaps, weights=weights)), len(gaps)


print("\n== Threshold scan: STRONG = s_min >= q (inside GROWTH)")
print(" q  n_strong share  F_strong F_weak  gap  P+strong P+weak  DDs  DDw  within-pool gap (pools)")
for q in [4, 6, 8, 10, 12, 14, 16, 18, 20, 25, 30]:
    m = g["s_min"] >= q
    a, b = g[m], g[~m]
    if len(a) < 30 or len(b) < 30:
        continue
    gap, pools = within_pool_gap(g, m)
    print(f"{q:3d} {len(a):8d} {m.mean():5.2f} {a['F14'].mean():8.2f} {b['F14'].mean():6.2f} "
          f"{a['F14'].mean()-b['F14'].mean():5.2f} {(a['F14']>0).mean():7.3f} {(b['F14']>0).mean():6.3f} "
          f"{a['R14'].mean():5.2f} {b['R14'].mean():5.2f}  {gap if gap is None else round(gap,2)} ({pools})")

print("\n== Old v1 split inside GROWTH (strong_10 and no slowdown, or accelerating 7/10/10)")
strong10 = (g[speed_cols] >= 10).all(axis=1)
strong7 = ((g["c_v28"] >= 7) & (g["c_p28"] >= 7) & (g[["c_v14", "c_p14", "c_v7", "c_p7"]] >= 10).all(axis=1)
           & (g["c_v28"] <= g["c_v14"]) & (g["c_v14"] <= g["c_v7"]))
v1_strong = (strong10 & ~g["slow"]) | strong7
v1_weak_low = ~strong10 & ~strong7
v1_weakening = ~v1_strong & ~v1_weak_low
for name, mask in (("v1 STRONG", v1_strong), ("WEAK (low speed)", v1_weak_low), ("WEAKENING (fast but slowing)", v1_weakening)):
    print(f"{name:30s}", outcome(g[mask]))
gap, pools = within_pool_gap(g, v1_strong)
print("within-pool gap v1 STRONG vs rest:", None if gap is None else round(gap, 2), f"({pools} pools)")

print("\n== Robustness: same scan on the T-7 cut, forward 7 days")
g7 = df[df["c7_label"] == "GROWTH"].copy()
g7["s_min"] = g7[[c.replace("c_", "c7_") for c in speed_cols]].min(axis=1)
for q in [6, 8, 10, 12, 14, 16, 20]:
    m = g7["s_min"] >= q
    a, b = g7[m], g7[~m]
    if len(a) < 30 or len(b) < 30:
        continue
    gap, pools = within_pool_gap(g7, m, f="F7")
    print(f"q={q:3d} n_strong={len(a):6d} share={m.mean():.2f} F7 strong={a['F7'].mean():6.2f} weak={b['F7'].mean():6.2f} "
          f"P+ {(a['F7']>0).mean():.3f}/{(b['F7']>0).mean():.3f} within-pool gap={None if gap is None else round(gap,2)} ({pools})")

print("\n== Quality candidates inside GROWTH (cut T-14 -> next 14 days)")
g["er_min"] = g[["c_er28", "c_er14", "c_er7"]].min(axis=1)
g["risk_adj"] = g["c_p28"] / g["c_dd14"].clip(lower=0.5)
g["FR"] = g["F14"] / g["R14"].clip(lower=0.5)
for feature in ("s_min", "c_er28", "er_min", "risk_adj"):
    within_f = [grp["F14"].corr(grp[feature], method="spearman") for _, grp in g.groupby("pool") if len(grp) >= 10]
    within_r = [grp["R14"].corr(grp[feature], method="spearman") for _, grp in g.groupby("pool") if len(grp) >= 10]
    within_q = [grp["FR"].corr(grp[feature], method="spearman") for _, grp in g.groupby("pool") if len(grp) >= 10]
    print(f"{feature:9s} within-pool Spearman vs fwd return {np.nanmedian(within_f):+.3f}, vs fwd DD {np.nanmedian(within_r):+.3f}, "
          f"vs fwd return/DD {np.nanmedian(within_q):+.3f}")
    q = pd.qcut(g[feature], 5, labels=False, duplicates="drop")
    parts = []
    for k, grp in g.groupby(q):
        parts.append(f"Q{k}: F {grp['F14'].mean():5.1f} DD {grp['R14'].mean():4.1f} F/DD {grp['FR'].median():4.1f} P+ {(grp['F14']>0).mean():.2f}")
    print("    " + " | ".join(parts))

print("\n== Top-5 per pool by feature at the cut vs realised next-14-day return/DD (pools with >=15 GROWTH strategies)")
for feature in ("s_min", "c_er28", "risk_adj"):
    hits, pools = [], 0
    for _, grp in g.groupby("pool"):
        if len(grp) < 15:
            continue
        pools += 1
        chosen = set(grp.nlargest(5, feature).index)
        best = set(grp.nlargest(5, "FR").index)
        hits.append(len(chosen & best))
    print(f"{feature:9s} mean overlap with realised top-5 by return/DD: {np.mean(hits):.2f} of 5 over {pools} pools (random ~{5*5/np.mean([len(gr) for _, gr in g.groupby('pool') if len(gr)>=15]):.2f})")
g["fast"] = g.groupby("pool")["s_min"].transform(lambda s: s >= s.median())
g["smooth"] = g.groupby("pool")["c_er28"].transform(lambda s: s >= s.median())
print("\n== Within-pool quadrants (median split per pool), next 14 days")
for (fast, smooth), grp in g.groupby(["fast", "smooth"]):
    name = ("fast" if fast else "slow") + "+" + ("smooth" if smooth else "rough")
    print(f"{name:12s} n={len(grp):5d} F {grp['F14'].mean():5.2f} median {grp['F14'].median():5.2f} DD {grp['R14'].mean():4.2f} "
          f"F/DD(med) {grp['FR'].median():4.2f} P+ {(grp['F14']>0).mean():.3f}")
