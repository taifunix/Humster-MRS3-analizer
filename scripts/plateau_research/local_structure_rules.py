"""How should a multi-order structure be scored? Rules compared on the Source v6 export (proxy curves).

Usage: local_structure_rules.py <export dir> <work dir>
Admitted scope-cuts (TF 15m-2h, best mean-member score > 10). Candidates: per close MA the best open MA
per shift (active points, shift >= 70), every 2-3 order combination with next shift >= 1.5x previous.
Rules pick one structure per scope-cut; the forward outcome comes from the summed member curves.
"""
import itertools
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from local_deep_struct import DAY, GRID, WARMUP, dense, evaluate  # noqa: E402
from local_pair_concept import TFS, v4_candidates  # noqa: E402

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)
RATIO, FLOOR, ADMIT = 1.5, 70, 10.0


def candidates(fc):
    act = fc[(fc.min_third_trades >= 1) & (fc.trades30 * fc.train_days / 30 >= 6) & fc.nb_med_upi.notna()].copy()
    act["shift"] = act.si.map(lambda v: GRID[int(v)])
    act = act[act["shift"] >= FLOOR]
    # depth-relative quality: percentile of the point among all active points of its shift in this scope-cut
    act["rel"] = act.groupby("shift").nb_med_upi.rank(pct=True)
    out = []
    for ci, g in act.groupby("ci"):
        best = g.sort_values("nb_med_upi", ascending=False).drop_duplicates("shift").sort_values("shift")
        pts = list(best.itertuples(index=False))
        for p in pts:
            out.append(((p.shift,), (p.point_id,), (p.nb_med_upi,), (p.rel,)))
        for n in (2, 3):
            for combo in itertools.combinations(pts, n):
                sh = [p.shift for p in combo]
                if all(r >= RATIO * l for l, r in zip(sh, sh[1:])):
                    out.append((tuple(sh), tuple(p.point_id for p in combo), tuple(p.nb_med_upi for p in combo),
                                tuple(p.rel for p in combo)))
    return out


def task(args):
    scope, daily, cuts, feats = args
    d0, pidx, ib, bal, eqc, eqmin, tr = dense(daily, cuts)
    arrays = (ib, bal, eqc, eqmin, tr)
    rows = []
    for cut in cuts.itertuples(index=False):
        fc = feats[feats.cut == cut.cut]
        top = v4_candidates(fc)
        if not top or top[0][0] <= ADMIT:
            continue
        s = int((np.datetime64(int(cut.s_ms // DAY), "D") - d0).astype(int)) + WARMUP
        t = int((np.datetime64(int(cut.t_ms // DAY), "D") - d0).astype(int))
        cand = candidates(fc)
        for F in cut.forwards:
            if t + F > bal.shape[1]:
                continue
            evals = []
            for sh, ids, nb, rel in cand:
                r = evaluate([pidx[i] for i in ids], arrays, s, t, F)
                evals.append(dict(n=len(sh), shifts="/".join(map(str, sh)), first=sh[0], last=sh[-1],
                                  mean_nb=float(np.mean(nb)), min_nb=float(np.min(nb)), rel=float(np.mean(rel)),
                                  min_rel=float(np.min(rel)), **r))
            e = pd.DataFrame(evals)
            multi = e[e.n >= 2]
            picks = {
                "1 mean nb (current)": multi.mean_nb.idxmax(),
                "2 summed curve UPI": multi.train_upi.idxmax(),
                "3 depth-relative mean": multi.rel.idxmax(),
                "4 min member nb": multi.min_nb.idxmax(),
                "5 best 3-order mean nb": e[e.n == 3].mean_nb.idxmax() if (e.n == 3).any() else None,
                "6 best 3-order curve UPI": e[e.n == 3].train_upi.idxmax() if (e.n == 3).any() else None,
                "7 single best point": e[e.n == 1].mean_nb.idxmax(),
                "8 curve UPI, members nb > 10": multi[multi.min_nb > 10].train_upi.idxmax() if (multi.min_nb > 10).any() else None,
                "9 depth-rel, members nb > 5": multi[multi.min_nb > 5].rel.idxmax() if (multi.min_nb > 5).any() else None,
            }
            for rule, i in picks.items():
                if i is None:
                    continue
                rows.append(dict(scope=scope, cut=cut.cut, F=F, rule=rule, top_score=top[0][0], **e.loc[i].to_dict()))
    return pd.DataFrame(rows)


def main():
    b, work = sys.argv[1], Path(sys.argv[2])
    c = duckdb.connect()
    cuts = c.execute(f"""select scope, cut, any_value(s_ms) s_ms, any_value(t_ms) t_ms, list(distinct forward_days) forwards
        from read_parquet('{b}/outcomes.parquet') where variant='exp' group by all""").df()
    cuts = cuts[cuts.scope.str.split("|").str[2].isin(TFS)]
    cuts["forwards"] = cuts.forwards.map(lambda x: sorted(int(v) for v in x))
    c.register("sc", cuts[["scope"]].drop_duplicates())
    feats = c.execute(f"select f.* from read_parquet('{(work / 'deep_features.parquet').as_posix()}') f join sc using (scope)").df()
    daily = c.execute(f"""select d.scope, d.point_id, d.utc_day, d.balance_close, d.equity_close, d.equity_min, d.trades_closed,
        d.initial_balance from read_parquet('{b}/daily_equity.part0*.parquet') d join sc using (scope)""").df()
    dg, fg = dict(tuple(daily.groupby("scope"))), dict(tuple(feats.groupby("scope")))
    tasks = [(s, dg[s], cuts[cuts.scope == s], fg[s]) for s in sorted(fg) if s in dg]
    del daily, feats
    with ProcessPoolExecutor(max_workers=24) as pool:
        res = pd.concat(list(pool.map(task, tasks, chunksize=2)), ignore_index=True)
    res["symbol"] = res.scope.str.split("|").str[0]
    res["q"] = res.ret30 * 5 / res.dd_fwd.clip(lower=1)
    res.to_csv(work / "structure_rules.csv", index=False, encoding="utf-8")
    cols = ["ret30", "dd_fwd", "q", "loss_neg", "hit10", "breach"]
    for F in (28, 14):
        sub = res[res.F == F]
        base = sub[sub.rule == "1 mean nb (current)"].set_index(["scope", "cut"])
        rows = []
        for rule, g in sub.groupby("rule"):
            lv = g.groupby("symbol")[cols].mean().mean()
            gi = g.set_index(["scope", "cut"])
            common = gi.index.intersection(base.index)
            dq = (gi.loc[common, "q"] - base.loc[common, "q"]).groupby(level=0).mean()
            lv["scope_cuts"] = len(g)
            lv["first_med"], lv["last_med"] = g["first"].median(), g["last"].median()
            lv["n3_share"] = (g.n == 3).mean()
            lv["top_shift_set"] = g.shifts.value_counts().index[0]
            lv["top_set_share"] = g.shifts.value_counts(normalize=True).iloc[0]
            rows.append(lv.rename(rule))
        print(f"\n== F = {F}: per-symbol mean of the pick (summed curves, equal lots) ==")
        print(pd.DataFrame(rows).round(2).to_string())


if __name__ == "__main__":
    main()
