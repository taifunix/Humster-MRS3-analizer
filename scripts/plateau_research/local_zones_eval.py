"""Inside-minus-outside forward outcomes per zone definition (pre-registered)."""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ZONES = ["z0", "z1a", "z1b", "z2", "z2s", "z3p", "z3rp", "z4"]
METRICS = ["loss_neg", "no_trades", "succ_fwd10", "dd_fwd", "success_10"]
B = 2000

b, work = sys.argv[1], Path(sys.argv[2])
c = duckdb.connect()
c.execute("set threads to 16")
feat = (work / "zone_features.parquet").as_posix()
rng = np.random.default_rng(20261015)


def load(F):
    return c.execute(f"""
      select o.scope, o.cut, o.symbol, o.half, o.point_id, o.shift_bp,
             o.success_10::double success_10, o.bad::double bad, o.breach::double breach, o.R,
             (o.pnl_fwd_pct <= 0)::double loss, (o.pnl_fwd_pct < 0)::double loss_neg, (o.trades_fwd = 0)::double no_trades, o.dd_fwd_pct dd_fwd,
             (o.pnl_fwd_pct * 30.0 / o.forward_days * 5 / greatest(o.dd_fwd_pct, 1.0) >= 10)::double succ_fwd10,
             {", ".join("z." + z for z in ZONES)}, z.min_part_ratio
      from read_parquet('{b}/outcomes.parquet') o
      join read_parquet('{feat}') z using (scope, cut, point_id)
      where o.variant='exp' and o.forward_days={F} and o.economic_pass""").df()


def per_symbol_diff(df, zone, metric):
    g = df.groupby(["symbol", "scope", "cut", zone])[metric].mean().unstack(zone)
    if True not in g.columns or False not in g.columns:
        return pd.Series(dtype=float)
    g = g.dropna(subset=[True, False])
    d = (g[True] - g[False]).groupby(level="symbol").mean()
    return d


def boot(d):
    if len(d) < 3:
        return np.nan, np.nan, np.nan, np.nan
    v = d.values
    idx = rng.integers(0, len(v), size=(B, len(v)))
    means = v[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    p = 2 * min((means <= 0).mean(), (means >= 0).mean())
    return v.mean(), lo, hi, min(p, 1.0)


rows = []
for F in (28, 14):
    df = load(F)
    for zone in ZONES:
        cover = df[zone].mean()
        for subset, part in (("all", df), ("A", df[df.half == "A"]), ("B", df[df.half == "B"])):
            for metric in METRICS:
                d = per_symbol_diff(part, zone, metric)
                m, lo, hi, p = boot(d)
                inside = part.loc[part[zone], metric].mean()
                outside = part.loc[~part[zone], metric].mean()
                rows.append(dict(F=F, zone=zone, subset=subset, metric=metric, coverage=cover,
                                 symbols=len(d), inside=inside, outside=outside,
                                 diff=m, lo=lo, hi=hi, p=p))
res = pd.DataFrame(rows)

prim = res[(res.F == 28) & (res.subset == "all") & (res.metric == "success_10")].sort_values("p")
k = len(prim)
holm = []
running = 0.0
for i, p in enumerate(prim.p.values):
    running = max(running, min(1.0, (k - i) * p))
    holm.append(running)
prim = prim.assign(p_holm=holm)
res = res.merge(prim[["F", "zone", "subset", "metric", "p_holm"]], how="left", on=["F", "zone", "subset", "metric"])
c.register("res_df", res)
c.execute(f"copy (select * from res_df) to '{(work / 'zone_eval.parquet').as_posix()}' (format parquet)")

pd.set_option("display.width", 250)
pd.set_option("display.max_rows", 500)
fmt = lambda x: f"{x:+.3f}" if pd.notna(x) else "  nan"
for F in (28, 14):
    print(f"\n===== F = {F} =====")
    for metric in METRICS:
        print(f"\n-- {metric}: inside-minus-outside (per-symbol mean) [95% CI] p / Holm --")
        t = res[(res.F == F) & (res.metric == metric)]
        for zone in ZONES:
            line = [f"{zone:5} cov={t[t.zone == zone].coverage.iloc[0]:.2f}"]
            for subset in ("all", "A", "B"):
                r = t[(t.zone == zone) & (t.subset == subset)].iloc[0]
                extra = f" holm={r.p_holm:.3f}" if subset == "all" and pd.notna(r.p_holm) else ""
                line.append(f"{subset}: {fmt(r['diff'])} [{fmt(r.lo)};{fmt(r.hi)}] p={r.p:.3f}{extra} n={r.symbols} in={r.inside:.3f} out={r.outside:.3f}")
            print(" | ".join(line))
