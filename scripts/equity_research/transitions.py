import runpy, sys, io, contextlib, json
import pandas as pd
facts = sys.argv[1]
sys.argv = [sys.argv[0].replace("transitions.py", "strong_weak.py"), facts]
with contextlib.redirect_stdout(io.StringIO()):
    ns = runpy.run_path(sys.argv[0], run_name="lib")
snapshot, label = ns["snapshot"], ns["label"]

def verdict(s):
    lab = label(s)
    if lab in ("N/A",):
        return "NO_DATA"
    if lab in ("DECLINING", "DEEP_DRAWDOWN"):
        return "SPAD"
    er = s["er28"]
    sm = "S" if er > .45 else ("N" if er > .25 else "C")
    if lab == "GROWTH":
        speed = min(s[k] for k in ("v28","p28","v14","p14","v7","p7"))
        slow = s["v14"] <= .8*s["v28"] and s["v7"] <= .8*s["v14"]
        if speed >= 10 and slow:
            return "RISK_GROWTH"            # weakening
        strong = speed >= 10
        if sm == "S" or (not strong and sm == "N"):
            return "RELIABLE"
        if not strong and sm == "C":
            return "RISK_GROWTH"
        return "RISKY_GROWTH"
    if lab == "RESUMED":
        return {"S": "RELIABLE", "N": "RISK_GROWTH", "C": "RISKY_GROWTH"}[sm]
    if lab == "STALLED":
        return "PAUSE"
    if lab == "CORRECTING":
        return "PAUSE_SOFT" if sm == "S" else "CORRECTION"
    return lab

order = ["RELIABLE","RISK_GROWTH","RISKY_GROWTH","PAUSE","PAUSE_SOFT","CORRECTION","SPAD","NO_DATA"]
for cut_key, horizon in (("14", 14), ("7", 7)):
    pairs = []
    with open(facts, encoding="utf-8") as h:
        for line in h:
            r = json.loads(line); f = r.get("facts") or {}
            a = snapshot((f.get("prefixes") or {}).get(cut_key)); b = snapshot(f)
            if a is None or b is None:
                continue
            pairs.append((verdict(a), verdict(b)))
    t = pd.crosstab(pd.Series([p[0] for p in pairs], name="from"), pd.Series([p[1] for p in pairs], name="to"), normalize="index")
    t = t.reindex(index=[o for o in order if o in t.index], columns=[o for o in order if o in t.columns]).fillna(0)
    n = pd.Series([p[0] for p in pairs]).value_counts()
    print(f"\n== {horizon} days later (rows: verdict at T-{cut_key}, %)")
    out = (100*t).round(1)
    out.insert(0, "n", [n[i] for i in out.index])
    print(out.to_string())
