import json, sys, runpy, io, contextlib
import numpy as np, pandas as pd
facts = sys.argv[1]
sys.argv = [sys.argv[0].replace("min_history.py", "er_thresholds.py"), facts]
with contextlib.redirect_stdout(io.StringIO()):
    ns = runpy.run_path(sys.argv[0], run_name="lib")
snapshot, er_full, family = ns["snapshot"], ns["er_full"], ns["family"]
for cut_key, hk, lim in (("14","14",10.0), ("7","7",7.0)):
    rows = []
    with open(facts, encoding="utf-8") as h:
        for line in h:
            r = json.loads(line); f = r.get("facts") or {}
            cut = (f.get("prefixes") or {}).get(cut_key); s = snapshot(cut); now = snapshot(f)
            if s is None or now is None: continue
            e = er_full(cut)
            if e is None: continue
            days = 28 + (float(cut["pre28_days"]) if cut.get("pre28") else 0)
            F, R = now["p"+hk], now["dd"+hk]
            rows.append({"pool": r["symbol"]+"_"+r["side"], "fam": family(s), "er": e, "days": days,
                         "bad": float(F<0 or R>lim), "F": F, "R": R})
    df = pd.DataFrame(rows)
    df = df[df["fam"].isin(["STRONG","WEAK","RESUMED","WEAKENING","STALLED","CORRECTING"])]
    print(f"\n######## T-{cut_key} -> {hk}d")
    for label, sub in (("history >=45", df[df.days >= 45]), ("45-56", df[(df.days >= 45) & (df.days < 56)]), (">=56", df[df.days >= 56])):
        sub = sub.copy()
        sub["band"] = pd.cut(sub["er"], [-9, .30, .40, .55, 9], labels=["<0.30", "0.30-0.40", "0.40-0.55", ">=0.55"])
        sub = sub[sub.groupby("pool")["pool"].transform("size") >= 15]
        sub["pb"] = sub.groupby("pool")["bad"].transform("mean")
        sub["pr"] = sub.groupby("pool")["R"].transform("mean")
        print(f"\n{label}: n={len(sub)}, pools={sub['pool'].nunique()}, median er={sub['er'].median():.3f}")
        for b in sub["band"].cat.categories:
            x = sub[sub["band"] == b]
            if len(x) >= 40:
                print(f"  {b:10s} n={len(x):5d} pools={x['pool'].nunique():2d} abs bad={100*x['bad'].mean():5.1f}%  within-pool d_bad={100*(x['bad']-x['pb']).mean():+5.1f} pp  d_fwdDD={(x['R']-x['pr']).mean():+5.2f}")
