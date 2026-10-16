"""Pair suitability on the Source v6 export: which train-time pair features tell in advance
whether a pair keeps working, and how a full concept (pair filter + v4 picks) compares with production.

Usage: local_pair_concept.py <export dir> <work dir> [candle root]
Work dir must hold deep_features.parquet (local_deep_features.py). Writes pair_concept_picks.parquet.
v4 pick: active points (>= 6 train trades, trades in each third), shift >= 70, one candidate per shift
(best open MA) per close MA, top-6 shifts, 2-3 orders with next shift >= 1.5x previous, ranked by mean
member nb_med_upi; outcomes from the summed member curves (evaluate()).
"""
import itertools
import json
import os
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from local_deep_struct import DAY, GRID, WARMUP, dense, evaluate  # noqa: E402

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)

TFS = {"15m", "30m", "45m", "1h", "2h"}
FLOOR, POOL, RATIO, KEEP = 70, 6, 1.5, 3
SHALLOW_MAX = 90


def v4_candidates(fc):
    act = fc[(fc.min_third_trades >= 1) & (fc.trades30 * fc.train_days / 30 >= 6) & fc.nb_med_upi.notna()]
    act = act[act.si.map(lambda v: GRID[int(v)]) >= FLOOR]
    out = []
    for _, g in act.groupby("ci"):
        best = g.sort_values("nb_med_upi", ascending=False).drop_duplicates("si").head(POOL)
        pts = sorted(zip((GRID[int(v)] for v in best.si), best.point_id, best.nb_med_upi))
        for n in (2, 3):
            for combo in itertools.combinations(pts, n):
                sh = [x[0] for x in combo]
                if all(r >= RATIO * l for l, r in zip(sh, sh[1:])):
                    out.append((float(np.mean([x[2] for x in combo])), sh, [x[1] for x in combo]))
    out.sort(key=lambda x: -x[0])
    return out[:KEEP]


def pair_features(fc):
    shallow = fc[fc.si.map(lambda v: GRID[int(v)]) <= SHALLOW_MAX]
    act = fc[(fc.trades30 * fc.train_days / 30 >= 6)]
    return dict(f_dd_shallow=shallow.maxdd.median(), f_ulcer_shallow=shallow.ulcer.median(),
                f_pnl_shallow=shallow.pnl30.median(), f_pos_share=(act.pnl30 > 0).mean() if len(act) else np.nan,
                f_best_nb=fc.nb_med_upi.max(), f_q90_nb=fc.nb_med_upi.quantile(0.9),
                f_trades_shallow=shallow.trades30.median())


def scope_task(args):
    scope, daily, cuts, feats, prod = args
    d0, pidx, ib, bal, eqc, eqmin, tr = dense(daily, cuts)
    arrays = (ib, bal, eqc, eqmin, tr)
    rows = []
    for cut in cuts.itertuples(index=False):
        s = int((np.datetime64(int(cut.s_ms // DAY), "D") - d0).astype(int)) + WARMUP
        t = int((np.datetime64(int(cut.t_ms // DAY), "D") - d0).astype(int))
        fc = feats[feats.cut == cut.cut]
        if fc.empty:
            continue
        pf = pair_features(fc)
        cands = v4_candidates(fc)
        for F in cut.forwards:
            if t + F > bal.shape[1]:
                continue
            base = dict(scope=scope, cut=cut.cut, t_ms=cut.t_ms, s_ms=cut.s_ms, F=F, **pf)
            for rank, (score, sh, ids) in enumerate(cands, 1):
                rows.append(dict(base, kind="V4", rank=rank, score=score, shifts="|".join(map(str, sh)),
                                 **evaluate([pidx[i] for i in ids], arrays, s, t, F)))
            prs = []
            for _, st in prod[prod.cut == cut.cut].iterrows():
                orders = json.loads(st.orders)
                if any(o["point_id"] not in pidx for o in orders):
                    continue
                r = evaluate([pidx[o["point_id"]] for o in orders], arrays, s, t, F)
                prs.append((r["train_upi"], sorted(o["shift_bp"] for o in orders), r))
            prs.sort(key=lambda x: -x[0])
            for rank, (score, sh, r) in enumerate(prs[:KEEP], 1):
                rows.append(dict(base, kind="PROD", rank=rank, score=score, shifts="|".join(map(str, sh)), **r))
    return pd.DataFrame(rows)


def build(b, work):
    c = duckdb.connect()
    c.execute("set threads to 16")
    cuts = c.execute(f"""select scope, cut, any_value(s_ms) s_ms, any_value(t_ms) t_ms, list(distinct forward_days) forwards
                         from read_parquet('{b}/outcomes.parquet') where variant='exp' group by all""").df()
    cuts = cuts[cuts.scope.str.split("|").str[2].isin(TFS)]
    cuts["forwards"] = cuts.forwards.map(lambda x: sorted(int(v) for v in x))
    prod = c.execute(f"""select scope, cut, orders from read_parquet('{b}/stages_structures.parquet')
                         where wtype='train_exp' and status='READY_MRS3_STRUCTURE'""").df()
    scopes = sorted(cuts.scope.unique())
    c.register("sc", pd.DataFrame({"scope": scopes}))
    feats = c.execute(f"select f.* from read_parquet('{(work / 'deep_features.parquet').as_posix()}') f join sc using (scope)").df()
    daily = c.execute(f"""select d.scope, d.point_id, d.utc_day, d.balance_close, d.equity_close, d.equity_min,
                          d.trades_closed, d.initial_balance
                          from read_parquet('{b}/daily_equity.part0*.parquet') d join sc using (scope)""").df()
    dg, fg, pg = dict(tuple(daily.groupby("scope"))), dict(tuple(feats.groupby("scope"))), dict(tuple(prod.groupby("scope")))
    empty = pd.DataFrame(columns=["scope", "cut", "orders"])
    tasks = [(s, dg[s], cuts[cuts.scope == s], fg[s], pg.get(s, empty)) for s in scopes if s in fg]
    del daily, feats
    with ProcessPoolExecutor(max_workers=24) as pool:
        res = pd.concat(list(pool.map(scope_task, tasks, chunksize=2)), ignore_index=True)
    sc = c.execute(f"select distinct scope, symbol, half, timeframe from read_parquet('{b}/scopes.parquet')").df()
    res = res.merge(sc, on="scope")
    res.to_parquet(work / "pair_concept_picks.parquet") if _has_pyarrow() else _duck_write(c, res, work)
    return res


def _has_pyarrow():
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


def _duck_write(c, res, work):
    c.register("res_df", res)
    c.execute(f"copy (select * from res_df) to '{(work / 'pair_concept_picks.parquet').as_posix()}' (format parquet)")


def candle_features(root, units):
    """Daily volatility, max drawdown and buy&hold of the price in the train window [S+4d, T)."""
    if root is None:
        return pd.DataFrame()
    c = duckdb.connect()
    rows = []
    for sym, g in units.groupby("symbol"):
        folder = Path(root) / sym
        if not folder.is_dir():
            continue
        glob = (folder / f"{sym}*_1m.parquet").as_posix()
        d = c.execute(f"""select "timestamp" // 86400000 dd, arg_max("close", "timestamp") px, min("low") lw
                          from read_parquet('{glob}') group by 1 order by 1""").df()
        for u in g.itertuples(index=False):
            lo, hi = u.s_ms // DAY + WARMUP, u.t_ms // DAY
            w = d[(d.dd >= lo) & (d.dd < hi)]
            if len(w) < 10:
                continue
            r = w.px.pct_change().dropna()
            peak = w.px.cummax()
            rows.append(dict(symbol=sym, cut=u.cut, c_vol=r.std() * 100, c_maxdd=((peak - w.lw) / peak).max() * 100,
                             c_bh30=(w.px.iloc[-1] / w.px.iloc[0] - 1) * 100 * 30 / len(w)))
    return pd.DataFrame(rows)


OUT = ["ret30", "dd_fwd", "q_fwd", "loss_neg", "hit10", "r_ex"]


def pair_table(res, kind):
    """One row per (symbol, cut, F): mean over TFs of the top-1 pick of this kind."""
    p = res[(res.kind == kind) & (res["rank"] == 1)].copy()
    p["q_fwd"] = p.ret30 * 5 / p.dd_fwd.clip(lower=1)
    feats = [c for c in res.columns if c.startswith("f_")]
    agg = {**{k: "mean" for k in OUT + feats + ["score", "train_upi", "train_maxdd"]},
           "t_ms": "first", "s_ms": "first", "half": "first", "timeframe": "nunique"}
    return p.groupby(["symbol", "cut", "F"]).agg(agg).rename(columns={"timeframe": "n_tf"}).reset_index()


def main():
    b, work = sys.argv[1], Path(sys.argv[2])
    candles = sys.argv[3] if len(sys.argv) > 3 else None
    path = work / "pair_concept_picks.parquet"
    if path.exists():
        res = duckdb.connect().execute(f"select * from read_parquet('{path.as_posix()}')").df()
    else:
        res = build(b, work)
    print(f"picks {len(res)}; V4 scope-cuts {res[res.kind == 'V4'].groupby(['scope', 'cut', 'F']).ngroups}; "
          f"PROD scope-cuts {res[res.kind == 'PROD'].groupby(['scope', 'cut', 'F']).ngroups}")

    v4 = pair_table(res, "V4")
    cf = candle_features(candles, v4[["symbol", "cut", "s_ms", "t_ms"]].drop_duplicates(["symbol", "cut"]))
    if len(cf):
        v4 = v4.merge(cf, on=["symbol", "cut"], how="left")
    v4["t_date"] = pd.to_datetime(v4.t_ms, unit="ms").dt.date
    v4["month"] = pd.to_datetime(v4.t_ms, unit="ms").dt.to_period("M").astype(str)
    v4.to_csv(work / "pair_concept_pairs.csv", index=False, encoding="utf-8")
    print(f"pair-units {len(v4)}, symbols {v4.symbol.nunique()}, with candles {v4.c_vol.notna().sum() if 'c_vol' in v4 else 0}")

    if "c_vol" in v4:
        x = v4.dropna(subset=["c_vol"])
        print("\n== 0. is the shallow-point drawdown a price-volatility proxy? Spearman over pair-units ==")
        for a in ("f_dd_shallow", "f_ulcer_shallow"):
            print(f"   {a:16} ~ c_vol {x[[a, 'c_vol']].corr(method='spearman').iloc[0, 1]:+.2f}"
                  f"  ~ c_maxdd {x[[a, 'c_maxdd']].corr(method='spearman').iloc[0, 1]:+.2f}")

    feats = ["score", "train_upi", "train_maxdd", "f_dd_shallow", "f_ulcer_shallow", "f_pnl_shallow",
             "f_pos_share", "f_best_nb", "f_q90_nb", "f_trades_shallow"] + (["c_vol", "c_maxdd", "c_bh30"] if "c_vol" in v4 else [])
    print("\n== 1. same-month cross-section: Spearman of a train pair feature with the pair's forward result ==")
    print("   (median over (month, F) groups with >= 8 pairs; share of groups with the sign of the median)")
    rows = []
    for F in (28, 14):
        sub = v4[v4.F == F]
        for feat in feats:
            for target in ("q_fwd", "ret30", "dd_fwd", "loss_neg"):
                cs = []
                for _, g in sub.groupby("month"):
                    g = g.dropna(subset=[feat, target])
                    if len(g) >= 8:
                        cs.append(g[[feat, target]].corr(method="spearman").iloc[0, 1])
                cs = np.array([v for v in cs if np.isfinite(v)])
                if len(cs):
                    med = np.median(cs)
                    rows.append(dict(F=F, feature=feat, target=target, rho=med, same_sign=(np.sign(cs) == np.sign(med)).mean(), groups=len(cs)))
    t = pd.DataFrame(rows).pivot_table(index=["F", "feature"], columns="target", values=["rho", "same_sign"])
    print(t.round(2).to_string())

    print("\n== 2. pair filter by tertile of a feature within the month: forward of v4 picks (mean over pairs) ==")
    for F in (28, 14):
        sub = v4[v4.F == F].copy()
        rows = []
        for feat in feats:
            s2 = sub.dropna(subset=[feat]).copy()
            s2["tert"] = s2.groupby("month")[feat].transform(lambda v: pd.qcut(v.rank(method="first"), 3, labels=False) if len(v) >= 6 else np.nan)
            g = s2.groupby("tert")[["ret30", "dd_fwd", "q_fwd", "loss_neg", "hit10"]].mean()
            for tert in (0, 2):
                if tert in g.index:
                    rows.append(g.loc[tert].rename((feat, "low" if tert == 0 else "high")))
        print(f"\n-- F={F}; all pairs: {sub[['ret30', 'dd_fwd', 'q_fwd', 'loss_neg', 'hit10']].mean().round(2).to_dict()}")
        print(pd.DataFrame(rows).round(2).to_string())

    print("\n== 3. does a pair keep its quality? earlier realized forward (ended before T) vs current forward ==")
    rows = []
    for F in (28, 14):
        sub = v4[v4.F == F].sort_values("t_ms")
        for sym, g in sub.groupby("symbol"):
            for u in g.itertuples(index=False):
                prev = g[g.t_ms + F * DAY <= u.t_ms]
                if len(prev):
                    p = prev.iloc[-1]
                    rows.append(dict(F=F, symbol=sym, month=u.month, prev_q=p.q_fwd, prev_ret=p.ret30, prev_loss=p.loss_neg,
                                     q_fwd=u.q_fwd, ret30=u.ret30, loss_neg=u.loss_neg, dd_fwd=u.dd_fwd))
    pr = pd.DataFrame(rows)
    for F, g in pr.groupby("F"):
        print(f"   F={F}: units {len(g)}, Spearman prev_q~q {g[['prev_q', 'q_fwd']].corr(method='spearman').iloc[0, 1]:+.2f}, "
              f"prev_ret~ret {g[['prev_ret', 'ret30']].corr(method='spearman').iloc[0, 1]:+.2f}")
        print("   after a losing forward:", g[g.prev_loss > 0.5][["ret30", "q_fwd", "loss_neg", "dd_fwd"]].mean().round(2).to_dict())
        print("   after a winning forward:", g[g.prev_loss <= 0.5][["ret30", "q_fwd", "loss_neg", "dd_fwd"]].mean().round(2).to_dict())

    print("\n== 4. concept vs production: per-scope top-1 pick, TFs 15m-2h, where both exist; per-symbol mean ==")
    for F in (28, 14):
        a = res[(res["rank"] == 1) & (res.F == F)]
        both = a.groupby(["scope", "cut"]).kind.nunique()
        keys = both[both == 2].index
        a = a.set_index(["scope", "cut"]).loc[keys].reset_index()
        g = a.groupby(["symbol", "kind"])[["ret30", "dd_fwd", "loss_neg", "hit10", "r_ex"]].mean().unstack("kind")
        d = pd.DataFrame({m: g[(m, "V4")] - g[(m, "PROD")] for m in ("ret30", "dd_fwd", "loss_neg", "hit10", "r_ex")})
        print(f"\n-- F={F}: scope-cuts {len(keys)}, symbols {len(g)}")
        print("   levels:", g.mean().unstack("kind").round(2).to_dict())
        print("   V4-PROD per-symbol mean:", d.mean().round(2).to_dict(), " symbols where V4 ret30 better:", f"{(d.ret30 > 0).mean():.0%}")


if __name__ == "__main__":
    main()
