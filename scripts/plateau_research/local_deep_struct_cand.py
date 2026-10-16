"""Candidate structures from top-score points per CloseMA (no plateaus), relative gap rules,
ranked by the summed-curve train UPI; production structures ranked the same way."""
import itertools
import json
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from local_deep_struct import DAY, GRID, WARMUP, dense, evaluate, gap_ok  # noqa: E402

warnings.filterwarnings("ignore")
TOP_N = 6
RULES = {"bp": None, "rel1.5": 1.5, "rel2.0": 2.0}


def rule_ok(shifts, rule):
    if rule is None:
        return gap_ok(shifts)
    return all(right >= rule * left for left, right in zip(shifts, shifts[1:]))


def scope_task(args):
    scope, daily, cuts, feats, prod = args
    d0, pidx, ib, bal, eqc, eqmin, tr = dense(daily, cuts)
    arrays = (ib, bal, eqc, eqmin, tr)
    rows = []
    for cut in cuts.itertuples(index=False):
        s = int((np.datetime64(int(cut.s_ms // DAY), "D") - d0).astype(int)) + WARMUP
        t = int((np.datetime64(int(cut.t_ms // DAY), "D") - d0).astype(int))
        fc = feats[feats.cut == cut.cut]
        act = fc[(fc.min_third_trades >= 1) & (fc.trades30 * fc.train_days / 30 >= 6) & fc.nb_med_upi.notna()]
        cand = act.sort_values("nb_med_upi", ascending=False).groupby("ci").head(TOP_N)
        for F in cut.forwards:
            if t + F > bal.shape[1]:
                continue
            for _, st in prod[prod.cut == cut.cut].iterrows():
                orders = json.loads(st.orders)
                if any(o["point_id"] not in pidx for o in orders):
                    continue
                idx = [pidx[o["point_id"]] for o in orders]
                rows.append(dict(scope=scope, cut=cut.cut, F=F, kind="PROD", n=len(idx),
                                 shifts="|".join(str(v) for v in sorted(o["shift_bp"] for o in orders)),
                                 **evaluate(idx, arrays, s, t, F)))
            for cm, sl in cand.groupby("ci"):
                pts = sorted(zip((GRID[int(v)] for v in sl.si), sl.point_id))
                seen = set()
                for n in (2, 3):
                    for combo in itertools.combinations(pts, n):
                        shifts = [x[0] for x in combo]
                        if len(set(shifts)) < n:
                            continue
                        key = tuple(x[1] for x in combo)
                        if key in seen:
                            continue
                        seen.add(key)
                        ok = {name: rule_ok(shifts, rule) for name, rule in RULES.items()}
                        if not any(ok.values()):
                            continue
                        r = evaluate([pidx[x[1]] for x in combo], arrays, s, t, F)
                        for name, passed in ok.items():
                            if passed:
                                rows.append(dict(scope=scope, cut=cut.cut, F=F, kind=f"CAND_{name}", n=n,
                                                 shifts="|".join(map(str, shifts)), **r))
    return pd.DataFrame(rows)


def main():
    b, work = sys.argv[1], Path(sys.argv[2])
    c = duckdb.connect()
    c.execute("set threads to 16")
    cuts = c.execute(f"""select scope, cut, any_value(s_ms) s_ms, any_value(t_ms) t_ms, list(distinct forward_days) forwards
                         from read_parquet('{b}/outcomes.parquet') where variant='exp' group by all""").df()
    cuts["forwards"] = cuts.forwards.map(lambda x: sorted(int(v) for v in x))
    prod = c.execute(f"""select scope, cut, orders from read_parquet('{b}/stages_structures.parquet')
                         where wtype='train_exp' and status='READY_MRS3_STRUCTURE'""").df()
    feats = c.execute(f"select * from read_parquet('{(work / 'deep_features.parquet').as_posix()}')").df()
    scopes = sorted(cuts.scope.unique())
    c.register("sc", pd.DataFrame({"scope": scopes}))
    daily = c.execute(f"""select d.scope, d.point_id, d.utc_day, d.balance_close, d.equity_close, d.equity_min,
                          d.trades_closed, d.initial_balance
                          from read_parquet('{b}/daily_equity.part0*.parquet') d join sc using (scope)""").df()
    dg, fg, pg = dict(tuple(daily.groupby("scope"))), dict(tuple(feats.groupby("scope"))), dict(tuple(prod.groupby("scope")))
    empty = pd.DataFrame(columns=["scope", "cut", "orders"])
    tasks = [(s, dg[s], cuts[cuts.scope == s], fg[s], pg.get(s, empty)) for s in scopes]
    del daily, feats
    out = []
    with ProcessPoolExecutor(max_workers=24) as pool:
        for i, fr in enumerate(pool.map(scope_task, tasks, chunksize=2)):
            out.append(fr)
    res = pd.concat(out, ignore_index=True)
    c.register("res_df", res)
    path = (work / "deep_struct_cand.parquet").as_posix()
    c.execute(f"copy (select * from res_df) to '{path}' (format parquet, compression zstd)")
    print("written", path, res.shape)
    print(res.groupby(["F", "kind", "n"]).size())


if __name__ == "__main__":
    main()
