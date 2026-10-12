import json, sys, runpy, io, contextlib
import numpy as np, pandas as pd
facts = sys.argv[1]
sys.argv = [sys.argv[0].replace("within_pool_verdict.py", "er_thresholds.py"), facts]
with contextlib.redirect_stdout(io.StringIO()):
    ns = runpy.run_path(sys.argv[0], run_name="lib")
snapshot, er_full, family, verdict = ns["snapshot"], ns["er_full"], ns["family"], ns["verdict"]
order = ["STEADY","PAUSE","UNEVEN","VOLATILE","CORRECTION","DECLINE"]
for cut_key, hk, lim in (("14","14",10.0), ("7","7",7.0)):
    rows = []
    with open(facts, encoding="utf-8") as h:
        for line in h:
            r = json.loads(line); f = r.get("facts") or {}
            cut = (f.get("prefixes") or {}).get(cut_key); s = snapshot(cut); now = snapshot(f)
            if s is None or now is None: continue
            e = er_full(cut)
            if e is None: continue
            F, R = now["p"+hk], now["dd"+hk]
            rows.append({"pool": r["symbol"]+"_"+r["side"], "v": verdict(family(s), e), "bad": float(F<0 or R>lim), "F": F, "R": R})
    df = pd.DataFrame(rows)
    df["pool_bad"] = df.groupby("pool")["bad"].transform("mean")
    df["pool_F"] = df.groupby("pool")["F"].transform("mean")
    df["pool_R"] = df.groupby("pool")["R"].transform("mean")
    print(f"\n== within pool, T-{cut_key} -> {hk}d: verdict minus its own pool average (pools with >=20 strategies)")
    big = df[df.groupby("pool")["pool"].transform("size") >= 20]
    for v in order:
        x = big[big["v"] == v]
        pools = x["pool"].nunique()
        if len(x) < 40: 
            print(f"{v:11s} n={len(x)}"); continue
        print(f"{v:11s} n={len(x):5d} pools={pools:2d} d_bad={100*(x['bad']-x['pool_bad']).mean():+6.1f} pp  d_F={(x['F']-x['pool_F']).mean():+5.1f}  d_fwdDD={(x['R']-x['pool_R']).mean():+5.2f}")
