"""Weekly non-overlapping stages (up to 8 weeks before the T-14 cut) from raw equity; next 14 days as outcome.

Read-only on the PerformanceDB. Equity at a boundary = last sample at or before it.
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
con.execute("set memory_limit='12GB'")
con.execute(f"set temp_directory='{tmp.as_posix()}'")
started = time.time()
con.execute("""
create temp table res as
select r.result_id, s.symbol || '_' || s.side as pool, r.report_start_utc as t0, r.report_end_utc as t_end,
       r.report_end_utc - interval 14 day as cut
from strategy_results r join strategies s on s.current_result_id = r.result_id
where s.lifecycle_status = 'ACTIVE' and r.report_end_utc - interval 42 day >= r.report_start_utc
""")
con.execute("""
create temp table bounds as
select result_id, k, cut - k * interval 7 day as t from res, range(0, 9) as g(k)
where cut - k * interval 7 day >= t0
union all select result_id, -1 as k, t_end as t from res
""")
con.execute("""
create temp table bvals as
select b.result_id, b.k, e.equity::double as eq
from bounds b asof join strategy_equity e on e.result_id = b.result_id and e.timestamp_utc <= b.t
""")
print("boundary values", round(time.time() - started, 1), "s")
fwd_dd = con.execute("""
with s as (
  select e.result_id, e.timestamp_utc as ts, e.equity::double as eq, r.cut,
         max(e.equity::double) over (partition by e.result_id order by e.sample_index rows unbounded preceding) as hwm
  from strategy_equity e join res r using (result_id)
  where e.timestamp_utc <= r.t_end
)
select result_id, max(1 - eq / hwm) * 100 as fwd_dd from s where ts > cut group by result_id
""").df()
print("forward drawdown", round(time.time() - started, 1), "s")
b = con.execute("select * from bvals").df()
pools = con.execute("select result_id, pool from res").df()

wide = b.pivot(index="result_id", columns="k", values="eq")
rows = []
for result_id, row in wide.iterrows():
    if pd.isna(row.get(0)) or pd.isna(row.get(-1)):
        continue
    weeks = []
    for k in range(1, 9):
        if k in row.index and not pd.isna(row[k]) and row[k] > 0 and row[k - 1] > 0:
            weeks.append(np.log(row[k - 1] / row[k]))  # week k: from cut-7k to cut-7(k-1)
        else:
            break
    if len(weeks) < 4:
        continue
    weeks = np.array(weeks)
    streak = 0
    for w in weeks:
        if w > 0:
            streak += 1
        else:
            break
    rows.append({"result_id": result_id, "n_weeks": len(weeks), "pos_share": float((weeks > 0).mean()),
                 "worst_week": float(weeks.min()) * 100, "mean_week": float(weeks.mean()) * 100,
                 "streak": streak, "F": float(np.log(row[-1] / row[0])) * 100})
df = pd.DataFrame(rows).merge(fwd_dd, on="result_id").merge(pools, on="result_id")
df["bad"] = ((df["F"] < 0) | (df["fwd_dd"] > 10)).astype(float)
df = df[df.groupby("pool")["pool"].transform("size") >= 15]
for c in ("bad", "F", "fwd_dd"):
    df["p_" + c] = df.groupby("pool")[c].transform("mean")
print(f"rows {len(df)}, pools {df['pool'].nunique()}, weeks median {df['n_weeks'].median()}, base bad {100*df['bad'].mean():.1f}%")


def top5(col, sign=1, label=None):
    dF, dB, dR, rho = [], [], [], []
    for _, g in df.groupby("pool"):
        key = g[col] * sign + 1e-9 * g["mean_week"]
        t = g.loc[key.nlargest(5).index]
        dF.append(t["F"].mean() - g["F"].mean()); dB.append(100 * (t["bad"].mean() - g["bad"].mean()))
        dR.append(t["fwd_dd"].mean() - g["fwd_dd"].mean()); rho.append(g["F"].corr(g[col] * sign, method="spearman"))
    print(f"{(label or col):28s} dF={np.mean(dF):+5.2f} d_bad={np.mean(dB):+5.1f}pp d_fwdDD={np.mean(dR):+5.2f} rho(F)={np.nanmedian(rho):+.2f}")


print("\n== top-5 per pool (forward return in % over 14 days)")
top5("pos_share", 1, "share of positive weeks")
top5("worst_week", 1, "best worst-week")
top5("streak", 1, "positive streak at end")
top5("mean_week", 1, "mean weekly return")
print("\n== share of positive weeks vs pool average")
df["share_bin"] = pd.cut(df["pos_share"], [-0.01, 0.5, 0.75, 0.99, 1.0], labels=["<=50%", "50-75%", "75-99%", "100%"])
for b_, x in df.groupby("share_bin", observed=True):
    print(f"{b_:7s} n={len(x):5d} d_bad={100*(x['bad']-x['p_bad']).mean():+5.1f}pp d_F={(x['F']-x['p_F']).mean():+5.2f} d_fwdDD={(x['fwd_dd']-x['p_fwd_dd']).mean():+5.2f}")
print("done", round(time.time() - started, 1), "s")
