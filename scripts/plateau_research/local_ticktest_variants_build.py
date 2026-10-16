"""Tick-test sets of structure variants on the same scopes.

Usage: local_ticktest_variants_build.py <export dir> <work dir> <output dir> <mode> [--liquid SYM,SYM,...]
mode "composition": scopes with a v4 score > 20; variants of the order set
    cur    best 2-3 order structure by mean member score (what v4 picks),
    chain3 best 3-order structure,
    deep3  best 3-order structure with the last order >= 350 bp,
    wide2  best 2-order structure with the second order >= 230 bp.
mode "shallow": liquid pairs (--liquid), scopes whose best structure score > --min-score (default 10); variants
    f70    best 2-3 order structure with the first order >= 70 bp (current floor),
    f50    best 2-3 order structure with the first order at 50 or 60 bp.
Candidates: active points (>= 6 train trades, trades in each third), best open MA per shift and close MA,
next shift >= 1.5x previous, ranked by mean member nb_med_upi. Identical structures chosen by several
variants are tested once (column variants). Tester window T .. T+28, only T + 28 <= DATA_END.
"""
import argparse
import hashlib
import itertools
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from local_deep_struct import DAY, GRID, WARMUP, dense, evaluate  # noqa: E402
from local_pair_concept import TFS, v4_candidates  # noqa: E402
from mrs3.config import AlgorithmConfig  # noqa: E402
from mrs3.lots import LotMethod, allocate_lots  # noqa: E402
from mrs3.strategy_json import generate_strategy  # noqa: E402

DATA_END, TEST_DAYS, WARMUP_DAYS, RATIO = np.datetime64("2026-10-15"), 28, 4, 1.5


def pool(fc, floor):
    act = fc[(fc.min_third_trades >= 1) & (fc.trades30 * fc.train_days / 30 >= 6) & fc.nb_med_upi.notna()].copy()
    act["shift"] = act.si.map(lambda v: GRID[int(v)])
    act = act[act["shift"] >= floor]
    out = []
    for _, g in act.groupby("ci"):
        best = g.sort_values("nb_med_upi", ascending=False).drop_duplicates("shift").sort_values("shift")
        pts = list(best.itertuples(index=False))
        for n in (2, 3):
            for combo in itertools.combinations(pts, n):
                sh = [p.shift for p in combo]
                if all(r >= RATIO * l for l, r in zip(sh, sh[1:])):
                    out.append((float(np.mean([p.nb_med_upi for p in combo])), sh, [p.point_id for p in combo]))
    return out


def pick(cands, cond):
    sel = [c for c in cands if cond(c[1])]
    return max(sel, key=lambda c: c[0]) if sel else None


VARIANTS = {
    "composition": [("cur", 70, lambda s: True), ("chain3", 70, lambda s: len(s) == 3),
                    ("deep3", 70, lambda s: len(s) == 3 and s[-1] >= 350), ("wide2", 70, lambda s: len(s) == 2 and s[-1] >= 230)],
    "shallow": [("f70", 70, lambda s: True), ("f50", 50, lambda s: s[0] in (50, 60))],
}


def point_parts(pid):
    p = pid.split("|")
    return dict(timeframe=p[2], shift_bp=int(p[3]), open_ma=int(p[6]), close_ma=int(p[9]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export"), ap.add_argument("work"), ap.add_argument("out"), ap.add_argument("mode", choices=list(VARIANTS))
    ap.add_argument("--liquid", default="")
    ap.add_argument("--min-score", type=float, default=10.0, help="shallow mode: best structure score of the scope must exceed it")
    args = ap.parse_args()
    b, work, out = Path(args.export), Path(args.work), Path(args.out)
    liquid = {s.strip() for s in args.liquid.split(",") if s.strip()}
    c = duckdb.connect()
    cuts = c.execute(f"""select scope, cut, any_value(s_ms) s_ms, any_value(t_ms) t_ms, max(forward_days) fmax
        from read_parquet('{b.as_posix()}/outcomes.parquet') where variant='exp' group by all""").df()
    cuts["t_date"] = pd.to_datetime(cuts.t_ms, unit="ms").dt.date.astype(str)
    cuts["symbol"] = cuts.scope.str.split("|").str[0]
    cuts = cuts[cuts.scope.str.split("|").str[2].isin(TFS)]
    cuts = cuts[[np.datetime64(t) + np.timedelta64(TEST_DAYS, "D") <= DATA_END for t in cuts.t_date]]
    if args.mode == "shallow":
        cuts = cuts[cuts.symbol.isin(liquid)]
    c.register("sc", cuts[["scope", "cut"]])
    feats = c.execute(f"select f.* from read_parquet('{(work / 'deep_features.parquet').as_posix()}') f join sc using (scope, cut)").df()

    picks = []
    for cut in cuts.itertuples(index=False):
        fc = feats[(feats.scope == cut.scope) & (feats.cut == cut.cut)]
        top = v4_candidates(fc)
        score70 = top[0][0] if top else -np.inf
        if args.mode == "composition" and score70 <= 20:
            continue
        pools = {floor: pool(fc, floor) for floor in {v[1] for v in VARIANTS[args.mode]}}
        if args.mode == "shallow" and max((x[0] for x in pools.get(50, []) + pools.get(70, [])), default=-np.inf) <= args.min_score:
            continue
        chosen = {}
        for name, floor, cond in VARIANTS[args.mode]:
            p = pick(pools[floor], cond)
            if p:
                chosen.setdefault(tuple(p[2]), [p, []])[1].append(name)
        for ids, (p, names) in chosen.items():
            picks.append(dict(scope=cut.scope, cut=cut.cut, symbol=cut.symbol, t_date=cut.t_date, s_ms=cut.s_ms,
                              t_ms=cut.t_ms, fmax=int(cut.fmax), score=p[0], scope_score=score70, ids=list(ids),
                              variants="|".join(names)))
    p = pd.DataFrame(picks)
    c.register("ps", p[["scope"]].drop_duplicates())
    daily = c.execute(f"""select d.scope, d.point_id, d.utc_day, d.balance_close, d.equity_close, d.equity_min,
        d.trades_closed, d.initial_balance from read_parquet('{b.as_posix()}/daily_equity.part0*.parquet') d join ps using (scope)""").df()
    config = AlgorithmConfig()
    template = json.loads((ROOT / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))
    tester = json.loads((ROOT / "templates/exchanges/Bybit/mrs3-tester.json").read_text(encoding="utf-8"))
    tag = "CMP" if args.mode == "composition" else "SHL"
    index = []
    for u in p.itertuples(index=False):
        d = daily[daily.scope == u.scope]
        d0, pidx, ib, bal, eqc, eqmin, tr = dense(d, pd.DataFrame([dict(s_ms=u.s_ms, t_ms=u.t_ms)]))
        s = int((np.datetime64(int(u.s_ms // DAY), "D") - d0).astype(int)) + WARMUP
        t = int((np.datetime64(int(u.t_ms // DAY), "D") - d0).astype(int))
        F = min(u.fmax, bal.shape[1] - t)
        r = evaluate([pidx[i] for i in u.ids], (ib, bal, eqc, eqmin, tr), s, t, F)
        parts = sorted((point_parts(i) for i in u.ids), key=lambda x: x["shift_bp"])
        digest = hashlib.sha256("|".join(u.ids).encode()).hexdigest()[:8]
        structure = dict(structure_id=f"{tag}_{u.t_date[5:].replace('-', '')}_{digest}", symbol=u.symbol, side="LONG",
                         timeframe=parts[0]["timeframe"], order_count=len(parts), common_close_ma=parts[0]["close_ma"],
                         orders=[dict(id=k, open_ma=x["open_ma"], shift_bp=x["shift_bp"]) for k, x in enumerate(parts, 1)])
        lots = allocate_lots(structure["orders"], LotMethod.EQUAL, config)
        strategy = generate_strategy(template, structure, lots, LotMethod.EQUAL, config)
        folder = out / f"T_{u.t_date}" / "strategies"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{strategy['name']}.json").write_text(json.dumps(strategy, ensure_ascii=False, indent=2), encoding="utf-8")
        index.append(dict(t_date=u.t_date, symbol=u.symbol, timeframe=structure["timeframe"], method=args.mode,
                          variants=u.variants, rank=1, strategy=strategy["name"], score=round(u.score, 2),
                          scope_score=round(u.scope_score, 2), close_ma=structure["common_close_ma"],
                          orders=" ".join(f"{x['shift_bp']}bp/MA{x['open_ma']}" for x in parts), proxy_days=F,
                          proxy_train_upi=round(r["train_upi"], 3), proxy_train_maxdd=round(r["train_maxdd"], 3),
                          proxy_ret30=round(r["ret30"], 3), proxy_dd_fwd=round(r["dd_fwd"], 3),
                          proxy_r_ex=round(r["r_ex"], 3), proxy_loss=r["loss_neg"]))
    idx = pd.DataFrame(index)
    assert idx.strategy.is_unique
    for t_date in idx.t_date.unique():
        cfg = dict(tester)
        cfg["StartDate"], cfg["EndDate"], cfg["WarmupDays"] = t_date, str(np.datetime64(t_date) + np.timedelta64(TEST_DAYS, "D")), WARMUP_DAYS
        (out / f"T_{t_date}" / "config_tester.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    idx.to_csv(out / "structures.csv", index=False, encoding="utf-8")
    v = idx.variants.str.split("|").explode()
    print(f"strategies {len(idx)}, scope-cuts {idx.groupby(['t_date', 'symbol', 'timeframe']).ngroups}, pairs {idx.symbol.nunique()}, folders {idx.t_date.nunique()}")
    print("variant coverage:", v.value_counts().to_dict())
    pr = idx.assign(v=idx.variants.str.split("|")).explode("v").groupby("v")[["proxy_ret30", "proxy_dd_fwd", "proxy_train_maxdd"]].mean().round(2)
    print(pr.to_string())
    print(idx.assign(v=idx.variants.str.split("|")).explode("v").groupby("v").orders.agg(lambda s: s.str.replace(r"/MA\d", "", regex=True).value_counts().head(3).to_dict()).to_string())


if __name__ == "__main__":
    main()
