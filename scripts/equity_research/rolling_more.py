"""Rolling origins (T-14/28/42): smoothness, approximate filter 7, and structural premises of filters 8-10."""
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
select r.result_id, r.strategy_id, s.symbol || '_' || s.side as pool, s.order_count, r.report_start_utc as t0,
       r.report_end_utc as t_end, c.back, r.report_end_utc - c.back * interval 1 day as cut
from strategy_results r join strategies s on s.current_result_id = r.result_id,
     (select unnest([{','.join(map(str, CUTS))}]) as back) c
where s.lifecycle_status = 'ACTIVE' and r.report_end_utc - (c.back + 28) * interval 1 day >= r.report_start_utc
""")
con.execute("""
create temp table eqh as
select result_id, timestamp_utc as ts, equity::double as eq,
       max(equity::double) over (partition by result_id order by sample_index rows unbounded preceding) as hwm
from strategy_equity where result_id in (select distinct result_id from res)
""")
# 6h grid back to 84 days before the cut (or the report start)
con.execute("""
create temp table grid as
select result_id, back, i, cut - i * interval 6 hour as t from res, range(0, 337) g(i)
where cut - i * interval 6 hour >= t0
""")
con.execute("""
create temp table gv as
select g.result_id, g.back, g.i, ln(e.eq) as le from grid g asof join eqh e on e.result_id = g.result_id and e.ts <= g.t
""")
er = con.execute("""
with d as (
  select result_id, back, i, le, le - lead(le) over (partition by result_id, back order by i) as step from gv
)
select result_id, back,
       sum(step) filter (where i < 112) / nullif(sum(abs(step)) filter (where i < 112), 0) as er28,
       sum(step) / nullif(sum(abs(step)), 0) as er84
from d where step is not null group by all
""").df()
print("smoothness", round(time.time() - t0, 1), "s")
facts = con.execute("""
with pts as (
  select r.result_id, r.back, r.cut, r.t0,
    (select eq from eqh e where e.result_id = r.result_id and e.ts <= r.t0 + interval 1 hour order by ts desc limit 1) as e_start,
    (select eq from eqh e where e.result_id = r.result_id and e.ts <= r.cut order by ts desc limit 1) as e_cut,
    (select eq from eqh e where e.result_id = r.result_id and e.ts <= r.cut - interval 14 day order by ts desc limit 1) as e_b,
    (select eq from eqh e where e.result_id = r.result_id and e.ts <= r.cut + interval 14 day order by ts desc limit 1) as e_fwd
  from res r
)
select * from pts
""").df()
dd = con.execute("""
select r.result_id, r.back,
  max(1 - e.eq / e.hwm) filter (where e.ts <= r.cut) * 100 as maxdd_cut,
  max(1 - e.eq / e.hwm) filter (where e.ts > r.cut and e.ts <= r.cut + interval 14 day) * 100 as fwd_dd
from res r join eqh e on e.result_id = r.result_id and e.ts <= r.cut + interval 14 day group by all
""").df()
struct = con.execute("""
select o.strategy_id,
  min(o.shift_bp) filter (where o.order_id = 1) as first_shift,
  avg(p.plateau_point_count) as mean_points, min(p.plateau_point_count) as min_points,
  avg(o.base_point_trades) as base_trades
from strategy_orders o left join analysis_plateaus p using (analysis_run_id, plateau_id) group by 1
""").df()
meta = con.execute("select result_id, back, strategy_id, pool, order_count, cut, t0 from res").df()
print("queries", round(time.time() - t0, 1), "s")

df = meta.merge(facts, on=["result_id", "back", "cut", "t0"]).merge(dd, on=["result_id", "back"]).merge(er, on=["result_id", "back"]).merge(struct, on="strategy_id", how="left")
df = df.dropna(subset=["e_start", "e_cut", "e_b", "e_fwd"])
days = (df["cut"] - df["t0"]).dt.total_seconds() / 86400
df["pnl30"] = 100 * np.log(df["e_cut"] / df["e_start"]) * 30 / days
df["DD5"] = df["pnl30"] * 5 / df["maxdd_cut"].where(df["maxdd_cut"] > 0)
df["B"] = 100 * np.log(df["e_cut"] / df["e_b"]) * 30 / 14
df["F"] = 100 * np.log(df["e_fwd"] / df["e_cut"])
df["bad"] = ((df["F"] < 0) | (df["fwd_dd"] > 10)).astype(float)
df["grp"] = df["pool"] + "|" + df["back"].astype(str)
df = df[df.groupby("grp")["grp"].transform("size") >= 15].copy()
for c in ("bad", "F", "fwd_dd"):
    df["p_" + c] = df.groupby("grp")[c].transform("mean")


def stage7(g):
    keep = pd.Series(True, index=g.index)
    for col, share in (("DD5", 0.5), ("B", 0.6)):
        vals = g[col].dropna().sort_values(ascending=False)
        if vals.empty:
            continue
        ref = vals.iloc[: (len(vals) + 1) // 2].median()
        if ref > 0:
            keep &= ~(g[col] < share * ref)
    return keep


df["stage7_pass"] = df.groupby("grp", group_keys=False).apply(stage7, include_groups=False)


def delta(x):
    return f"n={len(x):5d} d_bad={100*(x['bad']-x['p_bad']).mean():+5.1f}pp d_F={(x['F']-x['p_F']).mean():+5.2f} d_fwdDD={(x['fwd_dd']-x['p_fwd_dd']).mean():+5.2f}"


def corr(frame, col):
    rf, rb, rr = [], [], []
    for _, g in frame.groupby("grp"):
        g = g.dropna(subset=[col])
        if len(g) < 15 or g[col].nunique() < 3:
            continue
        rf.append(g["F"].corr(g[col], method="spearman")); rb.append(g["bad"].corr(g[col], method="spearman"))
        rr.append(g["fwd_dd"].corr(g[col], method="spearman"))
    return f"rho F={np.nanmedian(rf):+.2f}  rho bad={np.nanmedian(rb):+.2f}  rho fwdDD={np.nanmedian(rr):+.2f}  ({len(rf)} pools)"


for back in CUTS:
    x = df[df["back"] == back]
    print(f"\n######## cut T-{back}: rows {len(x)}, pools {x['pool'].nunique()}, base bad {100*x['bad'].mean():.1f}%")
    for col, label in (("er28", "smoothness 28d"), ("er84", "smoothness <=84d"), ("B", "B (last 14d)"), ("DD5", "DD5"),
                       ("first_shift", "first shift"), ("mean_points", "plateau points (mean)"),
                       ("min_points", "plateau points (weakest)"), ("base_trades", "base point trades"),
                       ("order_count", "order count")):
        print(f"  {label:26s}", corr(x, col))
    x = x.copy(); x["er_rank"] = x.groupby("grp")["er84"].rank(pct=True)
    print("  roughest third (er84)    :", delta(x[x["er_rank"] <= 1/3]))
    print("  smoothest third (er84)   :", delta(x[x["er_rank"] > 2/3]))
    print("  filter 7 excluded        :", delta(x[~x["stage7_pass"]]))
    print("  filter 7 passed          :", delta(x[x["stage7_pass"]]))
print("done", round(time.time() - t0, 1), "s")
