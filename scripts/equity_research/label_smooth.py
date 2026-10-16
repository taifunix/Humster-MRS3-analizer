import runpy, sys, io, contextlib
import numpy as np, pandas as pd
facts = sys.argv[1]
sys.argv = [sys.argv[0].replace("label_smooth.py", "strong_weak.py"), facts]
with contextlib.redirect_stdout(io.StringIO()):
    ns = runpy.run_path(sys.argv[0], run_name="lib")
df = ns["df"].dropna(subset=["c_label"]).copy()
g = df["c_label"] == "GROWTH"
sp = df[["c_v28","c_p28","c_v14","c_p14","c_v7","c_p7"]].min(axis=1)
slow = (df["c_v14"] <= 0.8*df["c_v28"]) & (df["c_v7"] <= 0.8*df["c_v14"])
df.loc[g & (sp >= 10) & slow, "c_label"] = "WEAKENING"
df.loc[g & (sp < 10), "c_label"] = "WEAK"
df.loc[df["c_label"] == "GROWTH", "c_label"] = "STRONG"
df["bad"] = ((df["F14"] < 0) | (df["R14"] > 10)).astype(float)
df["smooth"] = pd.cut(df["c_er28"], [-9, .25, .45, 9], labels=["CHOPPY", "NORMAL", "SMOOTH"])
order = ["STRONG","WEAK","WEAKENING","RESUMED","STALLED","CORRECTING","DECLINING"]
print(f"{'label':11s} {'smooth':7s} {'n':>5s} {'F':>5s} {'fwdDD':>6s} {'bad%':>5s}")
for lab in order:
    for s in ["SMOOTH","NORMAL","CHOPPY"]:
        x = df[(df["c_label"]==lab)&(df["smooth"]==s)]
        if len(x) >= 30:
            print(f"{lab:11s} {s:7s} {len(x):5d} {x['F14'].mean():5.1f} {x['R14'].mean():6.1f} {100*x['bad'].mean():5.1f}")
    x = df[df["c_label"]==lab]
    print(f"{lab:11s} {'all':7s} {len(x):5d} {x['F14'].mean():5.1f} {x['R14'].mean():6.1f} {100*x['bad'].mean():5.1f}\n")
