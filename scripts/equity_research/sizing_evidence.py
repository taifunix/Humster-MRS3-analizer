"""Read-only evidence: does % sizing with use_upnl distort MRS3 comparisons?

E1  cycle-return magnitude and compounding (volatility) drag;
E2  within-pair ranking: fixed-like sum(r) vs compounding sum(ln(1+r));
E3  fixed-lot curve measured with relative (W1/W0-1) formulas -> dilution bias;
E4  use_upnl effect: size of averaging fills vs floating PnL at fill time;
E5  lot quantization: first-fill notional vs intended lot_x * balance.
"""
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from mrs3.performance_v2_store import load_performance_v2_config, performance_v2_database_path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
db = performance_v2_database_path(load_performance_v2_config(root / "config.performance.json"))
con = duckdb.connect(str(db), read_only=True)
con.execute("set memory_limit='6GB'")
con.execute(f"set temp_directory='{tmp.as_posix()}'")
started = time.time()

con.execute("""
create temp table acts as
select x.result_id, s.strategy_id, s.symbol, s.side, s.order_count,
       x.action_index, x.timestamp_utc, x.action,
       x.size::double as size, x.post_size::double as post_size,
       x.fee::double as fee, x.balance::double as balance,
       x.price::double as price, x.cost::double as cost,
       r.report_end_utc
from strategy_actions x
join strategies s on s.current_result_id = x.result_id
join strategy_results r on r.result_id = x.result_id
where r.sizing_use_upnl
""")
con.execute("""
create temp table acts2 as
select *, sum(case when action = 'opened' then 1 else 0 end)
          over (partition by result_id order by action_index rows unbounded preceding) as cyc
from acts
""")
print("loaded", round(time.time() - started, 1), "s")

# ---- cycles -----------------------------------------------------------------
cycles = con.execute("""
with c as (
  select result_id, strategy_id, symbol, side, cyc, any_value(report_end_utc) as report_end_utc,
         min(action_index) filter (where action = 'opened') as open_idx,
         arg_min(balance + fee, action_index) filter (where action = 'opened') as bal_start,
         arg_max(balance, action_index) as bal_end,
         arg_max(post_size, action_index) as last_post,
         max(timestamp_utc) as close_ts
  from acts2 where cyc > 0
  group by result_id, strategy_id, symbol, side, cyc
)
select * from c where last_post = 0 and bal_start > 0
""").df()
cycles["r"] = cycles["bal_end"] / cycles["bal_start"] - 1.0
print("closed cycles", len(cycles))

absr = cycles["r"].abs()
print("\n[E1] |cycle return| quantiles (fraction of balance):")
print(absr.quantile([0.5, 0.9, 0.99, 0.999]).round(5).to_dict(), "max", round(absr.max(), 4))

per = cycles.groupby(["result_id", "strategy_id", "symbol", "side"]).agg(
    n=("r", "size"),
    sum_r=("r", "sum"),
    sum_log=("r", lambda s: np.log1p(s).sum()),
    drag=("r", lambda s: (s ** 2).sum() / 2),
).reset_index()
ratio = (per["drag"] / per["sum_r"].abs().clip(lower=1e-9))
print("[E1] per-strategy drag / |sum r| quantiles:", ratio.quantile([0.5, 0.9, 0.99]).round(4).to_dict())


def topk_overlap(df: pd.DataFrame, a: str, b: str, k: int = 5, min_pool: int = 10) -> dict:
    overlaps, rhos, pools = [], [], 0
    for _, g in df.groupby(["symbol", "side"]):
        if len(g) < min_pool:
            continue
        pools += 1
        ta = set(g.nlargest(k, a)["strategy_id"])
        tb = set(g.nlargest(k, b)["strategy_id"])
        overlaps.append(len(ta & tb))
        rhos.append(g[a].rank().corr(g[b].rank()))
    ov = np.array(overlaps)
    return {
        "pools": pools,
        "mean_top5_overlap": round(float(ov.mean()), 3),
        "share_identical_top5": round(float((ov == k).mean()), 3),
        "share_overlap_le3": round(float((ov <= 3).mean()), 3),
        "median_spearman": round(float(np.median(rhos)), 5),
        "min_spearman": round(float(np.min(rhos)), 4),
    }


print("\n[E2] full period, fixed-like sum(r) vs compounding sum(ln(1+r)):", topk_overlap(per, "sum_r", "sum_log"))

# ---- last-28-day window ------------------------------------------------------
cycles["in_w28"] = cycles["close_ts"] > (cycles["report_end_utc"] - pd.Timedelta(days=28))
w = cycles.groupby(["result_id", "strategy_id", "symbol", "side"]).apply(
    lambda g: pd.Series({
        "w_sum_r": g.loc[g["in_w28"], "r"].sum(),
        "w_sum_log": np.log1p(g.loc[g["in_w28"], "r"]).sum(),
        "pre_sum_r": g.loc[~g["in_w28"], "r"].sum(),
    }), include_groups=False,
).reset_index()
# Fixed-lot curve F = B(1 + cumulative r). A relative formula on it divides by F at window start.
w["w_relative_on_fixed"] = w["w_sum_r"] / (1.0 + w["pre_sum_r"])
print("[E3] W28 fixed-like sum(r) vs compounding log:", topk_overlap(w, "w_sum_r", "w_sum_log"))
print("[E3] W28 fixed correct (per base) vs fixed measured as W1/W0-1:", topk_overlap(w, "w_sum_r", "w_relative_on_fixed"))
print("[E3] pre-window cumulative r quantiles:", w["pre_sum_r"].quantile([0.1, 0.5, 0.9]).round(3).to_dict())

# ---- averaging fills: use_upnl ----------------------------------------------
fills = con.execute("""
with f as (
  select a.*,
         row_number() over (partition by result_id, cyc order by action_index) as k,
         sum(cost) over (partition by result_id, cyc order by action_index
                         rows between unbounded preceding and 1 preceding) as cost_before,
         sum(size) over (partition by result_id, cyc order by action_index
                         rows between unbounded preceding and 1 preceding) as size_before,
         first_value(cost) over (partition by result_id, cyc order by action_index) as cost_first,
         first_value(balance + fee) over (partition by result_id, cyc order by action_index) as bal_first
  from acts2 a
  where cyc > 0 and action in ('opened', 'increased')
    and not exists (select 1 from acts2 d where d.result_id = a.result_id and d.cyc = a.cyc
                    and d.action = 'decreased' and d.action_index < a.action_index)
)
select f.result_id, f.symbol, f.side, f.k, f.price, f.cost, f.balance + f.fee as bal_before,
       f.cost_before, f.size_before, f.cost_first, f.bal_first,
       o.lot_x::double as lot_x, o1.lot_x::double as lot_x1
from f
join strategy_orders o on o.strategy_id = f.strategy_id and o.order_id = f.k
join strategy_orders o1 on o1.strategy_id = f.strategy_id and o1.order_id = 1
""").df()
print("\nfills", len(fills), round(time.time() - started, 1), "s")

first = fills[fills["k"] == 1].copy()
first["dev"] = first["cost"] / (first["lot_x"] * first["bal_before"]) - 1
print("[E5] opened fill: notional / (lot_x * balance) - 1 quantiles:",
      first["dev"].quantile([0.01, 0.05, 0.5, 0.95, 0.99]).round(4).to_dict())
first["spread_rel"] = first["price"] / (first["lot_x"] * first["bal_before"])
by_sym = first.groupby("symbol").agg(price=("price", "median"), lot=("lot_x", "median"),
                                     dev_p05=("dev", lambda s: s.quantile(0.05)),
                                     dev_p95=("dev", lambda s: s.quantile(0.95)))
by_sym["dev_range"] = by_sym["dev_p95"] - by_sym["dev_p05"]
print("[E5] symbols with widest first-fill size noise (p95-p05):")
print(by_sym.sort_values("dev_range", ascending=False).head(8).round(4).to_string())
print("[E5] share of symbols with p95-p05 range > 5%:", round(float((by_sym["dev_range"] > 0.05).mean()), 3),
      "of", len(by_sym))

avg = fills[fills["k"] >= 2].copy()
sign = np.where(avg["side"] == "LONG", 1.0, -1.0)
avg["upnl_pct"] = sign * (avg["size_before"] * avg["price"] - avg["cost_before"]) / avg["bal_before"]
avg["ratio"] = (avg["cost"] / avg["lot_x"]) / (avg["cost_first"] / avg["lot_x1"]) - 1
avg["bal_ratio"] = avg["bal_before"] / avg["bal_first"] - 1
print("\n[E4] averaging fills:", len(avg))
print("[E4] floating PnL at averaging fill, % of balance quantiles:",
      (avg["upnl_pct"] * 100).quantile([0.01, 0.1, 0.5, 0.9]).round(3).to_dict())
mask = avg["ratio"].between(-0.5, 0.5)
x = avg.loc[mask, "upnl_pct"].to_numpy()
y = avg.loc[mask, "ratio"].to_numpy()
slope, intercept = np.polyfit(x, y, 1)
print("[E4] regression (size ratio - 1) on upnl_pct: slope", round(float(slope), 3),
      "intercept", round(float(intercept), 4), "n", int(mask.sum()))
for lo, hi in ((-1, -0.05), (-0.05, -0.02), (-0.02, -0.005), (-0.005, 0.005)):
    sel = avg[(avg["upnl_pct"] > lo) & (avg["upnl_pct"] <= hi) & mask]
    if len(sel):
        print(f"   upnl in ({lo},{hi}]: n={len(sel)} median size ratio-1={sel['ratio'].median():.4f}")
print("done", round(time.time() - started, 1), "s")
