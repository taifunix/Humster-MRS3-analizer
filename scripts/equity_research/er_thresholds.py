import json, sys, runpy, io, contextlib
import numpy as np, pandas as pd
facts = sys.argv[1]
sys.argv = [sys.argv[0].replace("er_thresholds.py", "strong_weak.py"), facts]
with contextlib.redirect_stdout(io.StringIO()):
    ns = runpy.run_path(sys.argv[0], run_name="lib")
snapshot, label = ns["snapshot"], ns["label"]

def er_full(cut):
    w = cut["windows"]["28"]; log = float(w["endpoint30"])*28/3000; e = float(w["er"])
    if e == 0: return None
    path = abs(log/e)
    pre = cut.get("pre28")
    if pre:
        lp = float(pre["endpoint30"])*float(cut["pre28_days"])/3000; ep = float(pre["er"])
        if ep == 0: return None
        path += abs(lp/ep); log += lp
    return log/path

def family(s):
    lab = label(s)
    if lab == "GROWTH":
        sp = min(s[k] for k in ("v28","p28","v14","p14","v7","p7"))
        slow = s["v14"] <= .8*s["v28"] and s["v7"] <= .8*s["v14"]
        return "WEAKENING" if (sp >= 10 and slow) else ("STRONG" if sp >= 10 else "WEAK")
    return lab

def load(cut_key, horizon_key, dd_limit):
    rows = []
    with open(facts, encoding="utf-8") as h:
        for line in h:
            r = json.loads(line); f = r.get("facts") or {}
            cut = (f.get("prefixes") or {}).get(cut_key); s = snapshot(cut); now = snapshot(f)
            if s is None or now is None: continue
            e = er_full(cut)
            if e is None: continue
            F = now["p"+horizon_key]; R = now["dd"+horizon_key]
            rows.append({"pool": r["symbol"]+"_"+r["side"], "fam": family(s), "er": e, "F": F, "R": R,
                         "bad": float(F < 0 or R > dd_limit)})
    return pd.DataFrame(rows)

d14 = load("14", "14", 10.0)
d7 = load("7", "7", 7.0)
groups = {"growth (STRONG+WEAK)": ["STRONG","WEAK"], "STRONG": ["STRONG"], "WEAK": ["WEAK"], "RESUMED": ["RESUMED"],
          "STALLED": ["STALLED"], "CORRECTING": ["CORRECTING"]}
edges = [-1, .20, .25, .30, .35, .40, .45, .50, .55, .60, .70, 1.01]
for name, data in (("T-14 -> 14d (bad: loss or DD>10%)", d14), ("T-7 -> 7d (bad: loss or DD>7%)", d7)):
    print(f"\n#### {name}; base bad {100*data['bad'].mean():.1f}%")
    data["bin"] = pd.cut(data["er"], edges)
    header = "er bin".ljust(14) + "".join(g[:12].rjust(14) for g in groups)
    print(header)
    for b in data["bin"].cat.categories:
        cells = []
        for g, fams in groups.items():
            x = data[(data["bin"] == b) & data["fam"].isin(fams)]
            cells.append(f"{100*x['bad'].mean():5.1f}% ({len(x):4d})" if len(x) >= 40 else "      -       ")
        print(str(b).ljust(14) + "".join(c.rjust(14) for c in cells))

def verdict(fam, er, rough=0.30, lo=0.40, hi=0.55):
    sm = "S" if er >= hi else ("N" if er >= lo else ("C" if er >= rough else "R"))
    if fam in ("DECLINING", "DEEP_DRAWDOWN"): return "DECLINE"
    if fam == "N/A": return "NO_DATA"
    if fam == "STALLED": return "PAUSE"
    if fam == "CORRECTING": return "STEADY" if sm in "SN" else "CORRECTION"
    if fam in ("WEAK", "RESUMED"): return {"S": "STEADY", "N": "STEADY", "C": "UNEVEN", "R": "VOLATILE"}[sm]
    if fam == "STRONG": return {"S": "STEADY", "N": "UNEVEN", "C": "VOLATILE", "R": "VOLATILE"}[sm]
    if fam == "WEAKENING": return "UNEVEN" if sm in "SN" else "VOLATILE"
    return fam

order = ["STEADY", "PAUSE", "UNEVEN", "VOLATILE", "CORRECTION", "DECLINE"]
for name, data in (("T-14 -> 14d", d14), ("T-7 -> 7d", d7)):
    data["v"] = [verdict(f, e) for f, e in zip(data["fam"], data["er"])]
    print(f"\n== verdicts, {name}")
    for v in order:
        x = data[data["v"] == v]
        print(f"{v:11s} n={len(x):5d} share={100*len(x)/len(data):4.1f}% bad={100*x['bad'].mean():5.1f}% F={x['F'].mean():5.1f} fwdDD={x['R'].mean():4.1f}")
    t = data.copy(); t["steady"] = t["v"] == "STEADY"
    pools = [g for _, g in t.groupby("pool") if len(g) >= 15]
    has = np.mean([g["steady"].sum() >= 3 for g in pools])
    print(f"pools with >=3 STEADY: {100*has:.0f}% of {len(pools)}")
