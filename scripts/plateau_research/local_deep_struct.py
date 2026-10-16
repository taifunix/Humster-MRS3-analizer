"""Multi-order structures: zone-based vs production, summed daily curves, equal lots."""
import itertools
import json
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
GRID = [30, 40, 50, 60, 70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550]
SI = {s: i for i, s in enumerate(GRID)}
DAY = 86_400_000
WARMUP = 4
GAPS = [(30, 80, 80), (80, 200, 100), (200, 300, 130), (300, 551, 150)]
ALPHAS = (0.5, 0.7)


def gap_ok(shifts):
    for left, right in zip(shifts, shifts[1:]):
        need = next(g for lo, hi, g in GAPS if lo <= left < hi)
        if right - left < need:
            return False
    return True


def dense(daily, cuts):
    days = pd.to_datetime(daily["utc_day"]).values.astype("datetime64[D]")
    d0 = min(days.min(), min(np.datetime64(int(s // DAY), "D") for s in cuts.s_ms)) - 1
    d1 = max(days.max(), max(np.datetime64(int(t // DAY), "D") for t in cuts.t_ms) + 30)
    n_days = int((d1 - d0).astype(int)) + 1
    pids = sorted(daily.point_id.unique())
    pidx = {p: i for i, p in enumerate(pids)}
    ib = daily.groupby("point_id").initial_balance.first().reindex(pids).values
    di = (days - d0).astype(int)
    pi = daily.point_id.map(pidx).values

    def mat(col, fill):
        a = np.full((len(pids), n_days), np.nan)
        a[pi, di] = daily[col].values
        a = pd.DataFrame(a).ffill(axis=1).values
        return np.where(np.isnan(a), fill[:, None], a)

    bal = mat("balance_close", ib)
    eqc = mat("equity_close", ib)
    eqmin = np.full((len(pids), n_days), np.nan)
    eqmin[pi, di] = daily.equity_min.values
    eqmin = np.where(np.isnan(eqmin), eqc, eqmin)
    tr = np.zeros((len(pids), n_days))
    tr[pi, di] = daily.trades_closed.values
    return d0, pidx, ib, bal, eqc, eqmin, tr


def path_dd(eq_start, eqc, eqmin):
    """Drawdown % path: peak from previous closes (and start), trough = same-day summed min."""
    prev = np.concatenate([[eq_start], eqc[:-1]])
    peak = np.maximum.accumulate(prev)
    return np.clip((peak - eqmin) / peak * 100, 0, None)


def evaluate(idx, arrays, s, t, F):
    ib, bal, eqc, eqmin, tr = arrays
    base = ib[idx].sum()
    B = bal[idx].sum(axis=0)
    C = eqc[idx].sum(axis=0)
    M = eqmin[idx].sum(axis=0)
    T = tr[idx].sum(axis=0)
    L = t - s
    dd_tr = path_dd(C[s - 1], C[s:t], M[s:t])
    pnl_tr = B[t - 1] - B[s - 1]
    ulcer = np.sqrt((dd_tr ** 2).mean())
    maxdd = dd_tr.max()
    pnl30_tr = pnl_tr / base * 100 * 30 / L
    dd_fw = path_dd(C[t - 1], C[t:t + F], M[t:t + F])
    pnl_fw = B[t + F - 1] - B[t - 1]
    ret30 = pnl_fw / base * 100 * 30 / F
    r_ex = ret30 * 5 / max(maxdd, 1.0)
    breach = dd_fw.max() > max(maxdd, 1.0)
    return dict(train_upi=pnl30_tr / max(ulcer, 0.25), train_maxdd=maxdd, ret30=ret30, r_ex=r_ex,
                dd_fwd=dd_fw.max(), loss_neg=float(pnl_fw < 0), no_trades=float(T[t:t + F].sum() == 0),
                hit10=float(r_ex >= 10 and not breach), hit15=float(r_ex >= 15 and not breach),
                breach=float(breach))


def components_2d(cells):
    """cells: list of (si, oi); 8-connected components; returns list of index lists."""
    index = {c: i for i, c in enumerate(cells)}
    parent = list(range(len(cells)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (a, b) in enumerate(cells):
        for da in (-1, 0, 1):
            for db in (-1, 0, 1):
                j = index.get((a + da, b + db))
                if j is not None and j != i:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[ri] = rj
    groups = {}
    for i in range(len(cells)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def scope_task(args):
    scope, daily, cuts, feats, prod = args
    d0, pidx, ib, bal, eqc, eqmin, tr = dense(daily, cuts)
    arrays = (ib, bal, eqc, eqmin, tr)
    rows = []
    for cut in cuts.itertuples(index=False):
        s = int((np.datetime64(int(cut.s_ms // DAY), "D") - d0).astype(int)) + WARMUP
        t = int((np.datetime64(int(cut.t_ms // DAY), "D") - d0).astype(int))
        fc = feats[feats.cut == cut.cut]
        for F in cut.forwards:
            if t + F > bal.shape[1]:
                continue
            for _, st in prod[prod.cut == cut.cut].iterrows():
                orders = json.loads(st.orders)
                idx = [pidx[o["point_id"]] for o in orders if o["point_id"] in pidx]
                if len(idx) != len(orders):
                    continue
                r = evaluate(idx, arrays, s, t, F)
                rows.append(dict(scope=scope, cut=cut.cut, F=F, kind="PROD", n=len(idx),
                                 shifts=tuple(sorted(o["shift_bp"] for o in orders)), **r))
            act = fc[(fc.min_third_trades >= 1) & (fc.trades30 * fc.train_days / 30 >= 6) & fc.nb_med_upi.notna()]
            if act.empty:
                continue
            peak = act.nb_med_upi.max()
            if not peak > 0:
                continue
            for alpha in ALPHAS:
                zone = act[act.nb_med_upi >= alpha * peak]
                for cm, sl in zone.groupby("ci"):
                    cells = list(zip(sl.si, sl.oi))
                    comps = components_2d(cells)
                    reps = []
                    for comp in comps:
                        sub = sl.iloc[comp]
                        best = sub.loc[sub.nb_med_upi.idxmax()]
                        reps.append((int(GRID[int(best.si)]), best.point_id, len(comp)))
                    reps.sort()
                    for n in (2, 3):
                        for combo in itertools.combinations(reps, n):
                            shifts = [x[0] for x in combo]
                            if len(set(shifts)) < n or not gap_ok(shifts):
                                continue
                            idx = [pidx[x[1]] for x in combo]
                            r = evaluate(idx, arrays, s, t, F)
                            rows.append(dict(scope=scope, cut=cut.cut, F=F, kind=f"ZONE{alpha}", n=n,
                                             shifts=tuple(shifts), zone_sizes=tuple(x[2] for x in combo), **r))
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
    dg = dict(tuple(daily.groupby("scope")))
    fg = dict(tuple(feats.groupby("scope")))
    pg = dict(tuple(prod.groupby("scope")))
    empty = pd.DataFrame(columns=["scope", "cut", "orders"])
    tasks = [(s, dg[s], cuts[cuts.scope == s], fg[s], pg.get(s, empty)) for s in scopes]
    del daily, feats
    out = []
    with ProcessPoolExecutor(max_workers=24) as pool:
        for i, fr in enumerate(pool.map(scope_task, tasks, chunksize=2)):
            out.append(fr)
            if i % 60 == 0:
                print("scope", i, flush=True)
    res = pd.concat(out, ignore_index=True)
    res["shifts"] = res.shifts.map(lambda x: "|".join(map(str, x)))
    if "zone_sizes" in res:
        res["zone_sizes"] = res.zone_sizes.map(lambda x: "|".join(map(str, x)) if isinstance(x, tuple) else None)
    c.register("res_df", res)
    path = (work / "deep_struct.parquet").as_posix()
    c.execute(f"copy (select * from res_df) to '{path}' (format parquet, compression zstd)")
    print("written", path, res.shape)
    print(res.groupby(["F", "kind", "n"]).size())


if __name__ == "__main__":
    main()
