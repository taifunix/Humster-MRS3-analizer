"""Removed-minus-kept forward outcomes when one economic threshold is tightened."""
import sys

import duckdb
import numpy as np
import pandas as pd

B = 2000
b = sys.argv[1]
c = duckdb.connect()
c.execute("set threads to 16")
rng = np.random.default_rng(20261016)
RULES = [
    ("DD<=14", "dd_pct <= 14"), ("DD<=12", "dd_pct <= 12"), ("DD<=10", "dd_pct <= 10"),
    ("eff>=3", "efficiency >= 3"), ("eff>=4", "efficiency >= 4"), ("eff>=5", "efficiency >= 5"),
    ("PnL>=10", "pnl_pct >= 10"), ("PnL>=15", "pnl_pct >= 15"), ("WR>=75", "win_rate_pct >= 75"),
]


def boot(d):
    v = d.values
    if len(v) < 3:
        return np.nan, np.nan, np.nan, np.nan
    means = v[rng.integers(0, len(v), size=(B, len(v)))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return v.mean(), lo, hi, min(1.0, 2 * min((means <= 0).mean(), (means >= 0).mean()))


for F in (28, 14):
    print(f"\n===== F = {F}: removed minus kept, within scope-cut, per-symbol mean [95% CI] =====")
    for name, rule in RULES:
        df = c.execute(f"""select symbol, half, scope, cut, ({rule}) keep,
            success_10::double success_10, bad::double bad, breach::double breach,
            (pnl_fwd_pct <= 0)::double loss, (pnl_fwd_pct < 0)::double loss_neg, (trades_fwd = 0)::double no_trades, dd_fwd_pct dd_fwd,
            (pnl_fwd_pct * 30.0 / forward_days * 5 / greatest(dd_fwd_pct, 1.0) >= 10)::double succ_fwd10
            from read_parquet('{b}/outcomes.parquet')
            where variant='exp' and forward_days={F} and economic_pass""").df()
        share_removed = 1 - df.keep.mean()
        parts = [f"{name:8} removes {share_removed:5.1%}"]
        for metric in ("loss_neg", "no_trades", "succ_fwd10", "dd_fwd"):
            for subset, part in (("all", df), ("A", df[df.half == "A"]), ("B", df[df.half == "B"])):
                g = part.groupby(["symbol", "scope", "cut", "keep"])[metric].mean().unstack("keep")
                if True not in g.columns or False not in g.columns:
                    continue
                g = g.dropna(subset=[True, False])
                d = (g[False] - g[True]).groupby(level="symbol").mean()
                m, lo, hi, p = boot(d)
                if subset == "all":
                    parts.append(f"{metric}: {m:+.3f} [{lo:+.3f};{hi:+.3f}] p={p:.3f}")
                else:
                    parts.append(f"{subset} {m:+.3f}")
        print(" | ".join(parts))
