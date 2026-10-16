"""Rolling origins (T-14/28/42): self-computable risk analytics vs smoothness and depth below the HWM.

All features use the 84 days (or the available history) before the cut on a 6-hour grid:
Ulcer index, time under water (>1% below the HWM), longest underwater spell, daily CVaR 95%,
MAE-like mean daily worst floating loss / wallet, worst floating loss, er84 and depth at the cut.
Outcomes: next 14 days. fwd_dd is measured from the all-time HWM (it includes the depth already present at
the cut); fwd_dd_new is the new drawdown from the highest equity reached since the cut (pure future risk).
Compared within pair (symbol+side) at the same cut. Read-only on the PerformanceDB.
Usage: rolling_risk.py <repo root> <duckdb temp dir>
"""
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from mrs3.performance_v2_store import load_performance_v2_config, performance_v2_database_path

root, tmp = Path(sys.argv[1]), Path(sys.argv[2])
db = performance_v2_database_path(load_performance_v2_config(root / "config.performance.json"))
con = duckdb.connect(str(db), read_only=True)
con.execute("set memory_limit='16GB'")
con.execute(f"set temp_directory='{tmp.as_posix()}'")
t0 = time.time()
CUTS = (14, 28, 42)
con.execute(f"""
create temp table res as
select r.result_id, s.symbol || '_' || s.side as pool, r.report_start_utc as t0, c.back,
       r.report_end_utc - c.back * interval 1 day as cut
from strategy_results r join strategies s on s.current_result_id = r.result_id,
     (select unnest([{','.join(map(str, CUTS))}]) as back) c
where s.lifecycle_status = 'ACTIVE' and r.report_end_utc - (c.back + 28) * interval 1 day >= r.report_start_utc
""")
con.execute("""
create temp table eqh as
select result_id, timestamp_utc as ts, equity::double as eq, wallet::double as wallet,
       max(equity::double) over (partition by result_id order by sample_index rows unbounded preceding) as hwm
from strategy_equity where result_id in (select distinct result_id from res)
""")
con.execute("""
create temp table grid as
select result_id, back, i, cut - i * interval 6 hour as t from res, range(0, 337) g(i)
where cut - i * interval 6 hour >= t0
""")
con.execute("""
create temp table gv as
select g.result_id, g.back, g.i, e.eq, e.hwm, e.wallet from grid g
asof join eqh e on e.result_id = g.result_id and e.ts <= g.t
""")
# MAE-like: worst floating loss per day from raw samples in the 84 days before the cut
mae = con.execute("""
with days as (
  select r.result_id, r.back, date_trunc('day', e.ts) as d, min((e.eq - e.wallet) / e.wallet) * 100 as day_worst
  from res r join eqh e on e.result_id = r.result_id
   and e.ts <= r.cut and e.ts > r.cut - interval 84 day and e.wallet > 0
  group by 1, 2, 3
)
select result_id, back, avg(day_worst) as mae_daily_mean, min(day_worst) as worst_float from days group by 1, 2
""").df()
fwd = con.execute("""
select r.result_id, r.back,
  (select eq from eqh e where e.result_id = r.result_id and e.ts <= r.cut order by ts desc limit 1) as e_cut,
  (select eq from eqh e where e.result_id = r.result_id and e.ts <= r.cut + interval 14 day order by ts desc limit 1) as e_fwd,
  (select max(1 - eq / hwm) * 100 from eqh e where e.result_id = r.result_id and e.ts > r.cut and e.ts <= r.cut + interval 14 day) as fwd_dd
from res r
""").df()
new_dd = con.execute("""
with s as (
  select r.result_id, r.back, e.eq,
         max(e.eq) over (partition by r.result_id, r.back order by e.ts rows unbounded preceding) as m,
         (select eq from eqh c where c.result_id = r.result_id and c.ts <= r.cut order by ts desc limit 1) as e_cut
  from res r join eqh e on e.result_id = r.result_id and e.ts > r.cut and e.ts <= r.cut + interval 14 day
)
select result_id, back, max(1 - eq / greatest(m, e_cut)) * 100 as fwd_dd_new from s group by 1, 2
""").df()
grid = con.execute("select * from gv order by result_id, back, i desc").df()
meta = con.execute("select result_id, back, pool from res").df()
print("queries", round(time.time() - t0, 1), "s")

rows = []
for (result_id, back), g in grid.groupby(["result_id", "back"], sort=False):
    eq_all = g["eq"].to_numpy(dtype=float); hwm_all = g["hwm"].to_numpy(dtype=float)
    ok = np.isfinite(eq_all) & (eq_all > 0) & np.isfinite(hwm_all) & (hwm_all > 0)
    if ok.sum() < 4 * 28 or not ok.all():
        continue  # incomplete grids are skipped so the 4-step daily alignment stays exact
    eq, hwm = eq_all, hwm_all
    dd = 100 * (1 - eq / hwm)
    under = dd > 1.0
    longest = run = 0
    for flag in under:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    logs = np.log(eq)
    steps = np.diff(logs)
    path = np.abs(steps).sum()
    daily = np.diff(logs[::4])
    worst = np.sort(daily)[: max(1, int(np.ceil(0.05 * len(daily))))]
    rows.append({"result_id": result_id, "back": back,
                 "ulcer": float(np.sqrt(np.mean(dd ** 2))), "under_share": float(under.mean()),
                 "longest_under_days": longest / 4, "cvar95": float(-100 * worst.mean()),
                 "er84": float((logs[-1] - logs[0]) / path) if path > 0 else np.nan, "dd_cut": float(dd[-1])})
df = (pd.DataFrame(rows).merge(mae, on=["result_id", "back"], how="left")
      .merge(fwd, on=["result_id", "back"]).merge(new_dd, on=["result_id", "back"], how="left")
      .merge(meta, on=["result_id", "back"]))
df = df.dropna(subset=["e_cut", "e_fwd", "fwd_dd"])
df["fwd_dd_new"] = df["fwd_dd_new"].fillna(0.0)
df["F"] = 100 * np.log(df["e_fwd"] / df["e_cut"])
df["bad"] = ((df["F"] < 0) | (df["fwd_dd"] > 10)).astype(float)
df["bad_new"] = ((df["F"] < 0) | (df["fwd_dd_new"] > 5)).astype(float)
df["grp"] = df["pool"] + "|" + df["back"].astype(str)
df = df[df.groupby("grp")["grp"].transform("size") >= 15].copy()

# Orientation: +1 means "higher value = safer" so the safest third is the top third.
FEATURES = (("er84", +1, "smoothness er84"), ("dd_cut", -1, "depth below HWM at cut"),
            ("ulcer", -1, "Ulcer index"), ("under_share", -1, "time under water"),
            ("longest_under_days", -1, "longest underwater spell"), ("cvar95", -1, "daily CVaR 95%"),
            ("mae_daily_mean", +1, "MAE-like daily worst float"), ("worst_float", +1, "worst floating loss"))


def evaluate(x, dd_col, bad_col):
    out = []
    for col, sign, label in FEATURES:
        y = x.dropna(subset=[col]).copy()
        y["key"] = y[col] * sign
        keep = y.groupby("grp")["key"].transform(lambda s: len(s) >= 15 and s.nunique() >= 3)
        y = y[keep.astype(bool)].copy()
        if y.empty:
            continue
        y["p_bad"] = y.groupby("grp")[bad_col].transform("mean")
        rho_dd, rho_f = [], []
        for _, g in y.groupby("grp"):
            rho_dd.append(g[dd_col].corr(g["key"], method="spearman"))
            rho_f.append(g["F"].corr(g["key"], method="spearman"))
        y["rk"] = y.groupby("grp")["key"].rank(pct=True)
        r, s = y[y["rk"] <= 1 / 3], y[y["rk"] > 2 / 3]
        out.append(f"  {label:28s} rho(safety, {dd_col})={np.nanmedian(rho_dd):+.2f} rho(safety, F)={np.nanmedian(rho_f):+.2f} | "
                   f"riskiest third d_{bad_col}={100*(r[bad_col]-r['p_bad']).mean():+5.1f}pp "
                   f"safest third d_{bad_col}={100*(s[bad_col]-s['p_bad']).mean():+5.1f}pp")
    return "\n".join(out)


for back in CUTS:
    x = df[df["back"] == back]
    print(f"\n######## cut T-{back}: rows {len(x)}, pools {x['pool'].nunique()}, base bad {100*x['bad'].mean():.1f}%, "
          f"base bad_new {100*x['bad_new'].mean():.1f}%")
    print(" outcome A: drawdown from the all-time HWM, bad = loss or DD>10%")
    print(evaluate(x, "fwd_dd", "bad"))
    print(" outcome B: new drawdown after the cut, bad_new = loss or new DD>5%")
    print(evaluate(x, "fwd_dd_new", "bad_new"))
print("done", round(time.time() - t0, 1), "s")
