import sys
from pathlib import Path
import duckdb
from mrs3.performance_v2_store import load_performance_v2_config, performance_v2_database_path

root = Path(sys.argv[1])
db = performance_v2_database_path(load_performance_v2_config(root / "config.performance.json"))
con = duckdb.connect(str(db), read_only=True)
con.execute("set memory_limit='4GB'")
# qty step per symbol ~ 10^-(max decimals used by any fill size); notional step at max price.
df = con.execute(r"""
with fills as (
  select s.symbol, x.size, x.price
  from strategy_actions x join strategies s on s.current_result_id = x.result_id
  where x.action in ('opened','increased') and x.size > 0
), dec as (
  select symbol,
         max(length(regexp_replace(rtrim(rtrim(cast(size as varchar), '0'), '.'), '^[0-9]*\.?', ''))) as decimals,
         max(price) as max_price, median(price) as med_price
  from fills group by symbol
), lots as (
  select s.symbol, min(o.lot_x)::double as min_lot_x
  from strategy_orders o join strategies s using (strategy_id)
  where s.current_result_id is not null group by s.symbol
)
select d.symbol, d.decimals, power(10, -d.decimals) as qty_step, d.max_price::double as max_price,
       power(10, -d.decimals) * d.max_price::double as step_usdt, l.min_lot_x,
       power(10, -d.decimals) * d.max_price::double / (0.01 * l.min_lot_x) as base_for_1pct
from dec d join lots l using (symbol)
order by base_for_1pct desc
""").df()
print(df.head(12).round(4).to_string(index=False))
print("\nsymbols", len(df))
for b in (1000, 3000, 5000, 10000, 20000):
    print(f"base {b}: symbols with worst-level step error > 1%: {(df['base_for_1pct'] > b).sum()}")
print("min lot_x distribution:", df["min_lot_x"].describe().round(3).to_dict())
