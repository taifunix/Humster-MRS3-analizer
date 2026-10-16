"""Evaluate a variant tick-test set (local_ticktest_variants_build.py): paired comparison of variants
on the same (cut, pair, TF) with real MRS3 reports.

Usage: local_ticktest_variants_eval.py <set dir> <output csv> <base variant> <variant> [<variant> ...]
"""
import glob
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from local_ticktest_eval import parse  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)
COLS = ["ret30", "dd", "q", "loss", "hit10", "trades"]


def boot(v, n=4000, seed=13):
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 3:
        return (v.mean() if len(v) else np.nan), np.nan, np.nan
    m = v[np.random.default_rng(seed).integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    return v.mean(), *np.percentile(m, [2.5, 97.5])


def main():
    root, out, base, variants = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4:]
    files = [f for f in glob.glob(str(root / "T_*" / "reports" / "*.html")) if not Path(f).name.startswith("report_optimizer")]
    with ProcessPoolExecutor(max_workers=16) as pool:
        rep = pd.DataFrame(list(pool.map(parse, files, chunksize=4))).drop_duplicates("strategy")
    idx = pd.read_csv(root / "structures.csv")
    df = idx.merge(rep, on="strategy", how="inner")
    print(f"index {len(idx)}, reports {len(rep)}, joined {len(df)}")
    df["ret30"] = df.pnl_pct * 30 / df.days
    df["q"] = df.ret30 * 5 / df.dd.clip(lower=1)
    df["loss"] = (df.pnl_pct < 0).astype(float)
    df["hit10"] = (df.ret30 >= 10).astype(float)
    df.to_csv(out, index=False, encoding="utf-8")
    long = df.assign(v=df.variants.str.split("|")).explode("v")
    key = ["t_date", "symbol", "timeframe"]
    print("\n== levels by variant (each structure counted under every variant that chose it) ==")
    g = long.groupby("v")
    print(g[COLS + ["proxy_train_maxdd"]].mean().round(2).join(g.size().rename("n")).join(g.symbol.nunique().rename("pairs")).to_string())
    print("   shared structures:", (df.variants.str.contains(r"\|")).sum(), "of", len(df))
    wide = long.pivot_table(index=key, columns="v", values=["ret30", "dd", "q", "loss"], aggfunc="first")
    for v in variants:
        sub = wide.dropna(subset=[("ret30", base), ("ret30", v)])
        print(f"\n== {v} minus {base}: {len(sub)} pair-TF-cuts, {sub.index.get_level_values('symbol').nunique()} pairs (per-pair mean, bootstrap over pairs) ==")
        for m in ("ret30", "dd", "q", "loss"):
            d = sub[(m, v)] - sub[(m, base)]
            ps = d.groupby(level="symbol").mean()
            mean, lo, hi = boot(ps.values)
            up = m in ("ret30", "q")
            print(f"   {m:5} {mean:+7.2f} [{lo:+.2f}; {hi:+.2f}]  cells where {v} better {((d > 0) if up else (d < 0)).mean():.0%}, ties {(d == 0).mean():.0%}")
        per_pair = pd.DataFrame({f"{m}_{k}": sub[(m, k)].groupby(level="symbol").mean() for m in ("ret30", "dd", "loss") for k in (base, v)})
        print(per_pair.round(2).to_string())
        by_date = pd.DataFrame({f"{m}_{k}": sub[(m, k)].groupby(level="t_date").mean() for m in ("ret30", "dd") for k in (base, v)})
        print(by_date.round(2).to_string())


if __name__ == "__main__":
    main()
