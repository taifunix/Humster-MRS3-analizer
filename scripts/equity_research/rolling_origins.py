"""Rolling-origin walk-forward: cuts at T-14, T-28, T-42, T-56; outcome = the 14 days after each cut.

Features use only equity before the cut (weekly stages, up to 8 weeks; drawdown from HWM at the cut).
Read-only on the PerformanceDB.
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
started = time.time()
CUTS = (14, 28, 42, 56)
con.execute(f"""
create temp table res as
select r.result_id, s.symbol || '_' || s.side as pool, r.report_start_utc as t0, r.report_end_utc as t_end, c.back,
       r.report_end_utc - c.back * interval 1 day as cut
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
print("hwm table", round(time.time() - started, 1), "s")
con.execute("""
create temp table bounds as
select result_id, back, k, cut - k * interval 7 day as t from res, range(0, 9) g(k) where cut - k * interval 7 day >= t0
union all select result_id, back, -1, cut + interval 14 day from res
""")
b = con.execute("""
select b.result_id, b.back, b.k, e.eq, e.hwm from bounds b
asof join eqh e on e.result_id = b.result_id and e.ts <= b.t
""").df()
fdd = con.execute("""
select r.result_id, r.back, max(1 - e.eq / e.hwm) * 100 as fwd_dd
from res r join eqh e on e.result_id = r.result_id and e.ts > r.cut and e.ts <= r.cut + interval 14 day
group by all
""").df()
pools = con.execute("select result_id, back, pool from res").df()
print("queries", round(time.time() - started, 1), "s")

rows = []
for (result_id, back), g in b.groupby(["result_id", "back"]):
    eq = dict(zip(g["k"], g["eq"])); hwm = dict(zip(g["k"], g["hwm"]))
    if 0 not in eq or -1 not in eq or not eq[0] or not eq[-1]:
        continue
    weeks = []
    for k in range(1, 9):
        if k in eq and eq[k] and eq[k - 1]:
            weeks.append(np.log(eq[k - 1] / eq[k]))
        else:
            break
    if len(weeks) < 4:
        continue
    weeks = np.array(weeks)
    rows.append({"result_id": result_id, "back": back, "pos_share": float((weeks > 0).mean()),
                 "mean_week": 100 * float(weeks.mean()), "worst_week": 100 * float(weeks.min()),
                 "last_week_neg": float(weeks[0] < 0), "dd_cut": 100 * (1 - eq[0] / hwm[0]),
                 "F": 100 * float(np.log(eq[-1] / eq[0]))})
df = pd.DataFrame(rows).merge(fdd, on=["result_id", "back"]).merge(pools, on=["result_id", "back"])
df["bad"] = ((df["F"] < 0) | (df["fwd_dd"] > 10)).astype(float)
df["grp"] = df["pool"] + "|" + df["back"].astype(str)
df = df[df.groupby("grp")["grp"].transform("size") >= 15]
for c in ("bad", "F", "fwd_dd"):
    df["p_" + c] = df.groupby("grp")[c].transform("mean")


def delta(x):
    return (f"n={len(x):5d} d_bad={100*(x['bad']-x['p_bad']).mean():+5.1f}pp "
            f"d_F={(x['F']-x['p_F']).mean():+5.2f} d_fwdDD={(x['fwd_dd']-x['p_fwd_dd']).mean():+5.2f}")


def top5(frame, col, sign=1):
    dF, dB, rho = [], [], []
    for _, g in frame.groupby("grp"):
        key = g[col] * sign
        t = g.loc[key.nlargest(5).index]
        dF.append(t["F"].mean() - g["F"].mean()); dB.append(100 * (t["bad"].mean() - g["bad"].mean()))
        rho.append(g["F"].corr(key, method="spearman"))
    return f"top5 dF={np.mean(dF):+5.2f} d_bad={np.mean(dB):+5.1f}pp rho={np.nanmedian(rho):+.2f}"


for back in CUTS:
    x = df[df["back"] == back]
    if x.empty:
        continue
    print(f"\n######## cut T-{back}: rows {len(x)}, pools {x['pool'].nunique()}, base bad {100*x['bad'].mean():.1f}%, "
          f"mean fwd 14d return {x['F'].mean():+.2f}%")
    print("  100% positive weeks :", delta(x[x["pos_share"] == 1]))
    print("  <75% positive weeks :", delta(x[x["pos_share"] < 0.75]))
    print("  last week negative  :", delta(x[x["last_week_neg"] == 1]))
    print("  >5% below HWM at cut:", delta(x[x["dd_cut"] > 5]))
    print("  mean weekly return  :", top5(x, "mean_week"))
    print("  share positive weeks:", top5(x, "pos_share"))
    print("  best worst-week     :", top5(x, "worst_week"))
print("done", round(time.time() - started, 1), "s")
