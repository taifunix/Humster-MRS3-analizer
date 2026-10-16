import json, sys, runpy, io, contextlib
import numpy as np, pandas as pd
facts = sys.argv[1]
sys.argv = [sys.argv[0].replace("er_length.py", "er_thresholds.py"), facts]
with contextlib.redirect_stdout(io.StringIO()):
    ns = runpy.run_path(sys.argv[0], run_name="lib")
snapshot, er_full, family, verdict = ns["snapshot"], ns["er_full"], ns["family"], ns["verdict"]
rows = []
with open(facts, encoding="utf-8") as h:
    for line in h:
        r = json.loads(line); f = r.get("facts") or {}
        cut = (f.get("prefixes") or {}).get("14"); s = snapshot(cut); now = snapshot(f)
        if s is None or now is None: continue
        e = er_full(cut)
        if e is None: continue
        days = 28 + (float(cut["pre28_days"]) if cut.get("pre28") else 0)
        F, R = now["p14"], now["dd14"]
        rows.append({"days": days, "v": verdict(family(s), e), "er": e, "bad": float(F < 0 or R > 10), "F": F})
df = pd.DataFrame(rows)
df["len"] = pd.cut(df["days"], [0, 41.9, 49, 56, 1e9], labels=["28-42 (W28 only)", "42-49", "49-56", ">56"])
print(df["len"].value_counts().sort_index().to_string())
print("\nbad% by verdict x history length (n)")
order = ["STEADY","PAUSE","UNEVEN","VOLATILE","CORRECTION","DECLINE"]
print("verdict".ljust(12) + "".join(str(c).rjust(20) for c in df["len"].cat.categories))
for v in order:
    cells = []
    for c in df["len"].cat.categories:
        x = df[(df["v"] == v) & (df["len"] == c)]
        cells.append(f"{100*x['bad'].mean():5.1f}% ({len(x):4d})" if len(x) >= 40 else "-")
    print(v.ljust(12) + "".join(cl.rjust(20) for cl in cells))
print("\nmedian er by history length:", df.groupby("len", observed=True)["er"].median().round(3).to_dict())
