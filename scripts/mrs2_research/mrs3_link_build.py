"""Point/plateau strength vs MRS3 tick-test outcome. Read-only on analysis DBs and PerformanceDB.

Builds one row per MRS3 strategy with per-order point facts (from the local analysis DB) and
plateau facts, plus outcomes: in-sample ret/DD of the MRS3 test and the out-of-sample return
after the MRS2 analysis window ended. Saves pickles for follow-up analysis.
INVALID on the current PerformanceDB: its MRS3 use source open while MRS2 points use ohlc4.
Rerun on the protocol-v2 (ohlc4) DB. Usage: mrs3_link_build.py <repo root> <work dir>
"""
import glob
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from mrs3.performance_v2_store import load_performance_v2_config, performance_v2_database_path

root, out = Path(sys.argv[1]), Path(sys.argv[2])

points, windows = [], {}
for f in glob.glob(str(root / "data/Analysis/**/*.analysis-v6.duckdb"), recursive=True):
    c = duckdb.connect(f, read_only=True)
    aid = c.execute("select value from manifest where key='analysis_id'").fetchone()[0]
    for (payload,) in c.execute("select payload_json from points").fetchall():
        d = json.loads(payload)
        ab = d.get("pretest_ab") or {}
        rt = ab.get("a_round_trips"); brt = ab.get("b_round_trips")
        points.append({
            "analysis_run_id": aid, "point_id": d["point_id"],
            "p_pnl": float(d["pnl_pct"]), "p_dd": float(d["dd_pct"]), "p_trades": d["trades"],
            "p_wr": float(d["win_rate_pct"]) if d.get("win_rate_pct") else np.nan,
            "p_pf": float(d["profit_factor"]) if d.get("profit_factor") else np.nan,
            "p_eff": float(d["efficiency"]) if d.get("efficiency") else np.nan,
            "p_days": float(d["effective_days"]),
            "p_b_pnl": float(ab["b_pnl"]) if ab.get("b_pnl") is not None else np.nan,
            "p_b_days": ab.get("b_days"), "p_b_trades": brt,
            "p_pre_pnl": float(ab["a_pnl"]) - float(ab["b_pnl"]) if ab.get("b_pnl") is not None else np.nan,
            "p_role": d.get("plateau_role"),
        })
        windows[aid] = d["report_end"]
    c.close()
pts = pd.DataFrame(points).drop_duplicates(["analysis_run_id", "point_id"])
print("points", len(pts), "analyses", len(windows))

db = performance_v2_database_path(load_performance_v2_config(root / "config.performance.json"))
con = duckdb.connect(str(db), read_only=True)
con.register("pts", pts)
con.register("win", pd.DataFrame({"analysis_run_id": list(windows), "analysis_end": pd.to_datetime(list(windows.values()), utc=True)}))
orders = con.execute("""
select s.strategy_id, s.symbol, s.side, s.timeframe, s.order_count, s.lifecycle_status, s.analysis_run_id,
       s.current_result_id, o.order_id, o.shift_bp, o.open_ma_len, o.lot_x::double lot_x,
       p.plateau_point_count, p.plateau_total_trades, o.base_point_trades,
       s.symbol || '|' || s.side || '|' || s.timeframe || '|' || o.shift_bp || '|SMA|ohlc4|' || o.open_ma_len
         || '|SMA|ohlc4|' || s.close_ma_len as point_id
from strategies s join strategy_orders o using (strategy_id)
left join analysis_plateaus p on p.analysis_run_id = o.analysis_run_id and p.plateau_id = o.plateau_id
where s.analysis_run_id in (select analysis_run_id from win)
""").df()
orders = orders.merge(pts, on=["analysis_run_id", "point_id"], how="left")
print("orders", len(orders), "matched points", orders["p_pnl"].notna().mean().round(3))

res = con.execute("""
select s.strategy_id, r.total_pnl_pct::double pnl, r.max_drawdown_pct::double dd,
       datediff('hour', r.effective_start_utc, r.report_end_utc) / 24.0 as test_days, w.analysis_end, r.report_end_utc
from strategies s join strategy_results r on r.result_id = s.current_result_id join win w using (analysis_run_id)
""").df()
oos = con.execute("""
with t as (select s.strategy_id, r.result_id, w.analysis_end from strategies s
  join strategy_results r on r.result_id = s.current_result_id join win w using (analysis_run_id))
select t.strategy_id,
  (select equity from strategy_equity e where e.result_id = t.result_id and e.timestamp_utc <= t.analysis_end order by timestamp_utc desc limit 1)::double e_at_end,
  (select equity from strategy_equity e where e.result_id = t.result_id order by timestamp_utc desc limit 1)::double e_last,
  (select max(timestamp_utc) from strategy_equity e where e.result_id = t.result_id) last_ts
from t
""").df()
sel = con.execute("""
with last as (select sr.strategy_id, sr.auto_status, sr.auto_reason,
  row_number() over (partition by sr.strategy_id order by r.created_at_utc desc) rn
  from selection_results sr join selection_runs r using (selection_run_id))
select strategy_id, auto_status, auto_reason from last where rn = 1
""").df()
orders.to_pickle(out / "orders.pkl")
res.merge(oos, on="strategy_id").merge(sel, on="strategy_id", how="left").to_pickle(out / "results.pkl")
print("saved")
