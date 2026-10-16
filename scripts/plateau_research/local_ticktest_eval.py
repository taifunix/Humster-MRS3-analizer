"""Evaluate the MRS3 tick test: real tester reports vs proxy, V4 vs production.

Usage: local_ticktest_eval.py <tester report root> <structures.csv> <output csv>
Reads every strategy HTML in `<root>/*/` (optimizer summaries skipped), keeps one report
per strategy name, joins the build index by name and prints paired comparisons.
"""
import glob
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")


def parse(path):
    from mrs3.performance_v2_html import parse_current_performance_v2_html
    from mrs3.performance_v2_store import load_performance_v2_config
    cfg = load_performance_v2_config(ROOT / "config.performance.json")
    r = parse_current_performance_v2_html(Path(path).read_bytes(), cfg)
    m = r.metrics
    num = lambda k: float(str(m.get(k, "nan")).split()[0]) if m.get(k) not in (None, "n/a") else np.nan
    return dict(strategy=r.settings.get("name"), file=Path(path).name, days=num("Days in test"),
                pnl_pct=num("Total PnL, %"), dd=num("Max Drawdown, %"), ulcer=num("Ulcer Index (hourly), %"),
                trades=num("Total Trades (entry → full exit)"), win_rate=num("Win Rate, %"),
                upnl_dd=num("Max Unrealized Drawdown, % of wallet"))


def boot(values, n=4000, seed=21):
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) < 3:
        return v.mean() if len(v) else np.nan, np.nan, np.nan, len(v)
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    return v.mean(), *np.percentile(means, [2.5, 97.5]), len(v)


def main():
    root, index_path, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    files = [f for f in glob.glob(str(root / "*" / "*.html")) if not Path(f).name.startswith("report_optimizer")]
    with ProcessPoolExecutor(max_workers=16) as pool:
        rows = list(pool.map(parse, files, chunksize=4))
    rep = pd.DataFrame(rows)
    dup = rep.groupby("strategy").pnl_pct.agg(["count", "nunique"])
    print(f"reports {len(rep)}, strategies {rep.strategy.nunique()}, duplicates with different PnL: {(dup['nunique'] > 1).sum()}")
    rep = rep.drop_duplicates("strategy")
    idx = pd.read_csv(index_path)
    df = idx.merge(rep, on="strategy", how="left", indicator=True)
    missing = df[df._merge == "left_only"]
    print(f"index {len(idx)}, matched {int((df._merge == 'both').sum())}, missing {len(missing)}")
    if len(missing):
        print(missing[["t_date", "strategy"]].to_string(index=False))
    df = df[df._merge == "both"].drop(columns="_merge").copy()
    df["ret30"] = df.pnl_pct * 30 / df.days
    df["q"] = df.ret30 * 5 / df.dd.clip(lower=1)
    df["loss"] = (df.pnl_pct < 0).astype(float)
    df["hit10"] = (df.q >= 10).astype(float)
    df["proxy_q"] = df.proxy_ret30 * 5 / df.proxy_dd_fwd.clip(lower=1)
    df.to_csv(out, index=False, encoding="utf-8")

    pd.set_option("display.width", 220)
    print("\n== levels by method (all picks) ==")
    print(df.groupby("method")[["ret30", "dd", "ulcer", "q", "loss", "hit10", "trades", "win_rate"]].agg(["mean", "median"]).round(2).T.to_string())

    print("\n== paired V4 minus PROD within (T, symbol, TF); per-symbol mean, bootstrap over symbols ==")
    cell = ["t_date", "symbol", "timeframe"]
    for label, sub in (("top-2 each", df), ("rank 1 only", df[df["rank"] == 1])):
        g = sub.groupby(cell + ["method"])[["ret30", "dd", "ulcer", "q", "loss", "hit10"]].mean().unstack("method")
        print(f"\n-- {label} --")
        for metric in ("ret30", "dd", "ulcer", "q", "loss", "hit10"):
            d = (g[(metric, "V4")] - g[(metric, "PROD")]).dropna()
            per_symbol = d.groupby(level="symbol").mean()
            m, lo, hi, n = boot(per_symbol.values)
            better = (d > 0).mean() if metric in ("ret30", "q", "hit10") else (d < 0).mean()
            print(f"  {metric:6} {m:+8.3f} [{lo:+.3f}; {hi:+.3f}]  symbols={n} cells={len(d)} cells where V4 better={better:.0%}")

    print("\n== proxy vs real ==")
    for proxy, real in (("proxy_ret30", "ret30"), ("proxy_dd_fwd", "dd"), ("proxy_q", "q")):
        overall = df[[proxy, real]].corr(method="spearman").iloc[0, 1]
        within = df.groupby(cell).apply(lambda g: g[[proxy, real]].corr(method="spearman").iloc[0, 1] if len(g) >= 3 else np.nan).dropna()
        print(f"  {proxy:13} vs {real:5}: Spearman overall {overall:+.2f}; within cell median {within.median():+.2f} (cells {len(within)})")
    g = df.groupby(cell + ["method"])[["ret30", "proxy_ret30", "q", "proxy_q"]].mean().unstack("method").dropna()
    for real, proxy in (("ret30", "proxy_ret30"), ("q", "proxy_q")):
        agree = (np.sign(g[(real, "V4")] - g[(real, "PROD")]) == np.sign(g[(proxy, "V4")] - g[(proxy, "PROD")])).mean()
        print(f"  sign of V4−PROD agrees between proxy and real ({real}): {agree:.0%} of {len(g)} cells")

    print("\n== per cell (rank-1 picks) ==")
    show = df[df["rank"] == 1].pivot_table(index=cell, columns="method", values=["ret30", "dd", "q"]).round(2)
    print(show.to_string())


if __name__ == "__main__":
    main()
