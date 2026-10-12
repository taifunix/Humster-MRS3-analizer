"""E4/E5 with partial fills merged per exchange order_id (one entry level per order)."""
import sys
import time
from pathlib import Path

import duckdb
import numpy as np

from mrs3.performance_v2_store import load_performance_v2_config, performance_v2_database_path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
db = performance_v2_database_path(load_performance_v2_config(root / "config.performance.json"))
con = duckdb.connect(str(db), read_only=True)
con.execute("set memory_limit='6GB'")
con.execute(f"set temp_directory='{tmp.as_posix()}'")
started = time.time()
con.execute("""
create temp table acts2 as
select x.result_id, s.strategy_id, s.symbol, s.side, x.action_index, x.order_id, x.action,
       x.size::double as size, x.fee::double as fee, x.balance::double as balance,
       x.price::double as price, x.cost::double as cost,
       sum(case when x.action = 'opened' then 1 else 0 end)
         over (partition by x.result_id order by x.action_index rows unbounded preceding) as cyc
from strategy_actions x
join strategies s on s.current_result_id = x.result_id
join strategy_results r on r.result_id = x.result_id
where r.sizing_use_upnl
""")
levels = con.execute("""
with entry as (
  select * from acts2 a
  where cyc > 0 and action in ('opened', 'increased')
    and not exists (select 1 from acts2 d where d.result_id = a.result_id and d.cyc = a.cyc
                    and d.action = 'decreased' and d.action_index < a.action_index)
), lvl as (
  select result_id, strategy_id, symbol, side, cyc, order_id,
         min(action_index) as first_idx,
         sum(cost) as cost, sum(size) as size,
         arg_min(balance + fee, action_index) as bal_before,
         arg_min(price, action_index) as first_price,
         count(*) as parts
  from entry group by all
), ranked as (
  select *, row_number() over (partition by result_id, cyc order by first_idx) as k,
         sum(cost) over (partition by result_id, cyc order by first_idx
                         rows between unbounded preceding and 1 preceding) as cost_before,
         sum(size) over (partition by result_id, cyc order by first_idx
                         rows between unbounded preceding and 1 preceding) as size_before,
         first_value(cost) over (partition by result_id, cyc order by first_idx) as cost_first,
         first_value(bal_before) over (partition by result_id, cyc order by first_idx) as bal_first
  from lvl
)
select r.*, o.lot_x::double as lot_x, o1.lot_x::double as lot_x1
from ranked r
join strategy_orders o on o.strategy_id = r.strategy_id and o.order_id = r.k
join strategy_orders o1 on o1.strategy_id = r.strategy_id and o1.order_id = 1
""").df()
print("levels", len(levels), "partial-fill levels", int((levels["parts"] > 1).sum()),
      round(time.time() - started, 1), "s")

first = levels[levels["k"] == 1].copy()
first["dev"] = first["cost"] / (first["lot_x"] * first["bal_before"]) - 1
print("[E5] level-1 notional / (lot_x * balance) - 1 quantiles:",
      first["dev"].quantile([0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]).round(4).to_dict())
by_sym = first.groupby("symbol").agg(price=("first_price", "median"), lot=("lot_x", "median"),
                                     n=("dev", "size"),
                                     dev_p05=("dev", lambda s: s.quantile(0.05)),
                                     dev_med=("dev", "median"),
                                     dev_p95=("dev", lambda s: s.quantile(0.95)))
by_sym["dev_range"] = by_sym["dev_p95"] - by_sym["dev_p05"]
print("[E5] widest level-1 size noise by symbol (p95-p05):")
print(by_sym.sort_values("dev_range", ascending=False).head(10).round(4).to_string())
print("[E5] symbols:", len(by_sym), "range>5%:", int((by_sym["dev_range"] > 0.05).sum()),
      "range>10%:", int((by_sym["dev_range"] > 0.10).sum()),
      "median range:", round(float(by_sym["dev_range"].median()), 4))

small = levels.copy()
small["dev"] = small["cost"] / (small["lot_x"] * small["bal_before"]) - 1
print("[E5] all levels by lot_x bucket, median |dev| and p95 |dev|:")
for lo, hi in ((0, 0.2), (0.2, 0.34), (0.34, 0.5), (0.5, 1.01)):
    sel = small[(small["lot_x"] > lo) & (small["lot_x"] <= hi)]["dev"].abs()
    if len(sel):
        print(f"   lot_x in ({lo},{hi}]: n={len(sel)} median={sel.median():.4f} p95={sel.quantile(0.95):.4f}")

avg = levels[levels["k"] >= 2].copy()
sign = np.where(avg["side"] == "LONG", 1.0, -1.0)
avg["upnl_pct"] = sign * (avg["size_before"] * avg["first_price"] - avg["cost_before"]) / avg["bal_before"]
avg["ratio"] = (avg["cost"] / avg["lot_x"]) / (avg["cost_first"] / avg["lot_x1"]) - 1
print("\n[E4] averaging levels:", len(avg))
print("[E4] floating PnL at averaging fill, % of balance:",
      (avg["upnl_pct"] * 100).quantile([0.001, 0.01, 0.1, 0.5]).round(3).to_dict())
for lo, hi in ((-1, -0.05), (-0.05, -0.03), (-0.03, -0.02), (-0.02, -0.01), (-0.01, -0.005), (-0.005, 0.005)):
    sel = avg[(avg["upnl_pct"] > lo) & (avg["upnl_pct"] <= hi)]
    if len(sel):
        print(f"   upnl in ({lo},{hi}]: n={len(sel)} median upnl={sel['upnl_pct'].median():.4f} "
              f"median size ratio-1={sel['ratio'].median():.4f}")
print("done", round(time.time() - started, 1), "s")
