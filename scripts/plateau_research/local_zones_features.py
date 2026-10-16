"""Zone features per (scope, cut, point) for the pre-registered definitions."""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

GRID = [30, 40, 50, 60, 70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550]

b, work = sys.argv[1], Path(sys.argv[2])
work.mkdir(parents=True, exist_ok=True)
c = duckdb.connect()
c.execute("set threads to 16")
c.execute("create table grid as select unnest(?) shift_bp, generate_subscripts(?, 1) - 1 si", [GRID, GRID])
c.execute(f"""create table p as
  select scope, cut, symbol, half, s_ms, t_ms, train_days, point_id, shift_bp, open_ma, close_ma,
         economic_pass, S_q25, plateau_id
  from read_parquet('{b}/outcomes.parquet') where variant='exp' and forward_days=14""")
c.execute("create table p2 as select p.*, grid.si from p join grid using (shift_bp)")
print("points:", c.execute("select count(*), count(distinct scope||cut) from p2").fetchone())

c.execute(f"""create table d as select scope, point_id, cast(utc_day as date) dd, balance_close, initial_balance
  from read_parquet('{b}/daily_equity.part0*.parquet')""")
c.execute("create table ib as select scope, point_id, any_value(initial_balance) ib from d group by all")
c.execute("""create table bnd as select scope, cut, point_id,
  cast(to_timestamp(s_ms/1000) as date) b0,
  cast(to_timestamp(s_ms/1000) as date) + cast(round(train_days/3) as integer) b1,
  cast(to_timestamp(s_ms/1000) as date) + cast(round(2*train_days/3) as integer) b2,
  cast(to_timestamp(t_ms/1000) as date) b3, train_days
  from p2""")
for k in range(4):
    c.execute(f"""create table bal{k} as select x.scope, x.cut, x.point_id, coalesce(d.balance_close, ib.ib) v
      from bnd x join ib using (scope, point_id)
      asof left join d on d.scope = x.scope and d.point_id = x.point_id and d.dd < x.b{k}""")
c.execute("""create table parts as select x.scope, x.cut, x.point_id,
  (b1.v - b0.v) p1, (b2.v - b1.v) p2, (b3.v - b2.v) p3,
  datediff('day', x.b0, x.b1) d1, datediff('day', x.b1, x.b2) d2, datediff('day', x.b2, x.b3) d3, x.train_days
  from bnd x join bal0 b0 using (scope, cut, point_id) join bal1 b1 using (scope, cut, point_id)
  join bal2 b2 using (scope, cut, point_id) join bal3 b3 using (scope, cut, point_id)""")
c.execute("""create table t as select *,
  (p1 > 0 and p2 > 0 and p3 > 0) z3,
  ((p1+p2+p3) > 0 and least(p1/d1, p2/d2, p3/d3) >= (p1+p2+p3)/(d1+d2+d3)/3) z3r,
  case when (p1+p2+p3) > 0 then least(p1/d1, p2/d2, p3/d3) / ((p1+p2+p3)/(d1+d2+d3)) end min_part_ratio
  from parts""")

c.execute("create table pt as select p2.*, t.z3, t.z3r, t.min_part_ratio, t.p1, t.p2, t.p3 from p2 join t using (scope, cut, point_id)")
c.execute("""create table nb as select a.scope, a.cut, a.point_id,
  count(*) n_nb, sum(b.economic_pass::int) n_nb_pass, sum(b.z3::int) n_nb_z3
  from pt a join pt b on a.scope=b.scope and a.cut=b.cut and abs(a.si-b.si)<=1 and abs(a.open_ma-b.open_ma)<=1
     and abs(a.close_ma-b.close_ma)<=1 and a.point_id<>b.point_id
  group by all""")
c.execute("""create table f as select pt.*, nb.n_nb, nb.n_nb_pass, nb.n_nb_z3,
  pt.economic_pass and pt.plateau_id is not null z0,
  pt.economic_pass and nb.n_nb_pass = nb.n_nb z2,
  pt.economic_pass and nb.n_nb_pass >= 0.8*nb.n_nb z2s,
  pt.economic_pass and pt.z3 z3p,
  pt.economic_pass and pt.z3r z3rp,
  pt.economic_pass and pt.z3 and nb.n_nb_z3 >= 0.8*nb.n_nb z4,
  max(case when pt.economic_pass then pt.S_q25 end) over (partition by pt.scope, pt.cut) sq_max
  from pt join nb using (scope, cut, point_id)""")
f = c.execute("select * from f").df()


def components(sub: pd.DataFrame) -> pd.Series:
    """Size of the 3x3x3-connected component of each row in sub."""
    keys = list(zip(sub.si, sub.open_ma, sub.close_ma))
    index = {key: i for i, key in enumerate(keys)}
    parent = list(range(len(keys)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (s, o, cl) in enumerate(keys):
        for ds in (-1, 0, 1):
            for do in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    j = index.get((s + ds, o + do, cl + dc))
                    if j is not None and j != i:
                        a, bb = find(i), find(j)
                        if a != bb:
                            parent[a] = bb
    roots = [find(i) for i in range(len(keys))]
    sizes = pd.Series(roots).map(pd.Series(roots).value_counts())
    return pd.Series(sizes.values, index=sub.index)


for name, alpha in (("z1a", 0.5), ("z1b", 0.7)):
    cand = f.economic_pass & f.S_q25.notna() & (f.sq_max > 0) & (f.S_q25 >= alpha * f.sq_max)
    f[name] = False
    for _, sub in f.loc[cand].groupby(["scope", "cut"], sort=False):
        sizes = components(sub)
        f.loc[sizes.index[sizes >= 3], name] = True

out = work / "zone_features.parquet"
result = f.drop(columns=["sq_max"])
c.register("result_df", result)
c.execute(f"copy (select * from result_df) to '{out.as_posix()}' (format parquet, compression zstd)")
print("written", out, len(f))
print(f.loc[f.economic_pass, ["z0", "z1a", "z1b", "z2", "z2s", "z3p", "z3rp", "z4"]].mean())
