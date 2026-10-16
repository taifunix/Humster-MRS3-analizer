"""Evaluate the admission-rule tick test: ADMIT (score > 10) vs REJECT, real MRS3 reports.

Usage: local_ticktest2_eval.py <set dir with T_*/reports and structures.csv> <output csv>
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


def main():
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    files = [f for f in glob.glob(str(root / "T_*" / "reports" / "*.html")) if not Path(f).name.startswith("report_optimizer")]
    with ProcessPoolExecutor(max_workers=16) as pool:
        rep = pd.DataFrame(list(pool.map(parse, files, chunksize=2))).drop_duplicates("strategy")
    idx = pd.read_csv(root / "structures.csv")
    df = idx.merge(rep, on="strategy", how="left", indicator=True)
    print(f"index {len(idx)}, reports {len(rep)}, missing {(df._merge == 'left_only').sum()}")
    df = df[df._merge == "both"].drop(columns="_merge").copy()
    df["ret30"] = df.pnl_pct * 30 / df.days
    df["q"] = df.ret30 * 5 / df.dd.clip(lower=1)
    df["loss"] = (df.pnl_pct < 0).astype(float)
    df["hit10"] = (df.ret30 >= 10).astype(float)
    df.to_csv(out, index=False, encoding="utf-8")

    print("\n== by cut and group: mean (median) ==")
    g = df.groupby(["t_date", "method"])[COLS]
    print(g.mean().round(2).join(g.size().rename("n")).to_string())
    print(df.groupby(["t_date", "method"])[["ret30", "dd"]].median().round(2).rename(columns=lambda c: c + "_med").to_string())

    print("\n== per pair: ADMIT vs REJECT where the pair has both (same cut) ==")
    p = df.groupby(["t_date", "symbol", "method"])[["ret30", "dd", "q"]].mean().unstack("method")
    both = p.dropna()
    print(both.round(2).to_string())
    if len(both):
        print(f"   ADMIT better ret30 in {(both[('ret30', 'ADMIT')] > both[('ret30', 'REJECT')]).mean():.0%}, "
              f"lower dd in {(both[('dd', 'ADMIT')] < both[('dd', 'REJECT')]).mean():.0%} of {len(both)} pair-cuts")

    print("\n== score vs real result (Spearman, all strategies per cut) ==")
    for t, g in df.groupby("t_date"):
        print(f"   {t}: score~ret30 {g[['score', 'ret30']].corr(method='spearman').iloc[0, 1]:+.2f}, "
              f"score~dd {g[['score', 'dd']].corr(method='spearman').iloc[0, 1]:+.2f}, "
              f"score~q {g[['score', 'q']].corr(method='spearman').iloc[0, 1]:+.2f}")
    sub = df[df.proxy_days >= 14]
    print(f"   proxy ret30 vs real ret30 (cuts with proxy): {sub[['proxy_ret30', 'ret30']].corr(method='spearman').iloc[0, 1]:+.2f}")

    print("\n== every strategy ==")
    show = df.sort_values(["t_date", "method", "score"], ascending=[True, True, False])
    print(show[["t_date", "method", "symbol", "timeframe", "score", "orders", "ret30", "dd", "trades", "proxy_ret30"]].round(2).to_string(index=False))


if __name__ == "__main__":
    main()
