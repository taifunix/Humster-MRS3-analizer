import json, sys, runpy, io, contextlib
import numpy as np, pandas as pd
facts = sys.argv[1]
sys.argv = [sys.argv[0].replace("full_er.py", "strong_weak.py"), facts]
with contextlib.redirect_stdout(io.StringIO()):
    ns = runpy.run_path(sys.argv[0], run_name="lib")
snapshot, label = ns["snapshot"], ns["label"]

def er_full(cut):
    w = cut["windows"]["28"]
    log28 = float(w["endpoint30"]) * 28 / 3000
    er28 = float(w["er"])
    if er28 == 0:
        return None, None
    path = abs(log28 / er28); log = log28
    pre = cut.get("pre28")
    if pre:
        lp = float(pre["endpoint30"]) * float(cut["pre28_days"]) / 3000
        ep = float(pre["er"])
        if ep == 0:
            return None, None
        path += abs(lp / ep); log += lp
    return log / path, float(cut.get("pre28_days") or 0) + 28

rows = []
with open(facts, encoding="utf-8") as h:
    for line in h:
        r = json.loads(line); f = r.get("facts") or {}
        cut = (f.get("prefixes") or {}).get("14")
        s = snapshot(cut); now = snapshot(f)
        if s is None or now is None:
            continue
        ef, days = er_full(cut)
        if ef is None:
            continue
        lab = label(s)
        if lab == "GROWTH":
            sp = min(s[k] for k in ("v28","p28","v14","p14","v7","p7"))
            slow = s["v14"] <= .8*s["v28"] and s["v7"] <= .8*s["v14"]
            lab = "WEAKENING" if (sp >= 10 and slow) else ("STRONG" if sp >= 10 else "WEAK")
        rows.append({"pool": r["symbol"]+"_"+r["side"], "label": lab, "er28": s["er28"], "er_full": ef,
                     "days": days, "F": now["p14"], "R": now["dd14"]})
df = pd.DataFrame(rows)
df["bad"] = ((df["F"] < 0) | (df["R"] > 10)).astype(float)
print("rows", len(df), " history days at cut: median", df["days"].median(), "p90", df["days"].quantile(.9))
print("corr(er28, er_full) spearman:", round(df["er28"].corr(df["er_full"], method="spearman"), 2))
print("er_full quantiles:", df["er_full"].quantile([.1,.25,.5,.75,.9]).round(3).to_dict())

def table(col, cuts):
    df["sm"] = pd.cut(df[col], [-9, cuts[0], cuts[1], 9], labels=["CHOPPY","NORMAL","SMOOTH"])
    print(f"\n== {col} bands {cuts}")
    for lab in ["STRONG","WEAK","WEAKENING","RESUMED","STALLED","CORRECTING","DECLINING"]:
        parts = []
        for s in ["SMOOTH","NORMAL","CHOPPY"]:
            x = df[(df["label"]==lab)&(df["sm"]==s)]
            parts.append(f"{s[:1]}: n={len(x):4d} bad={100*x['bad'].mean():4.1f}% F={x['F'].mean():5.1f}" if len(x) >= 30 else f"{s[:1]}: n={len(x):4d}   -")
        print(f"{lab:10s} " + " | ".join(parts))
    t = df.copy(); t["rk"] = t.groupby("pool")[col].rank(pct=True)
    print("within-pool terciles bad%:", [round(100*t[(t.rk>a)&(t.rk<=b)]["bad"].mean(),1) for a,b in ((0,1/3),(1/3,2/3),(2/3,1.01))])

table("er28", (.25, .45))
q = df["er_full"].quantile([1/3, 2/3]).round(3).tolist()
table("er_full", (q[0], q[1]))
