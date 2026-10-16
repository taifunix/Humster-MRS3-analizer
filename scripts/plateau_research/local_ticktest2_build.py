"""Build the MRS3 tick-test set for the pair/TF admission rule: admitted (score > 10) vs rejected (score <= 10).

Usage: local_ticktest2_build.py <export dir> <work dir> <output dir> <candle root>
       [--dates all|d1,d2] [--max-tf N] [--ranks K] [--exclude earlier/structures.csv] [--data-end YYYY-MM-DD]
Only cuts whose tester window T-4 .. T+28 is covered by the local tester candles, or with --data-end
(the tester downloads candles itself) only cuts with T+28 <= data end. Per scope the top-1 v4 structure (local_pair_concept.v4_candidates); per symbol up to
MAX_TF admitted and MAX_TF rejected scopes (highest score first); only symbols with tester candles.
The tester window is always T .. T+28; the proxy uses the longest forward the export has.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from local_deep_struct import DAY, WARMUP, dense, evaluate  # noqa: E402
import local_pair_concept  # noqa: E402
from local_deep_struct import SI  # noqa: E402
from local_pair_concept import TFS, v4_candidates  # noqa: E402
from mrs3.config import AlgorithmConfig  # noqa: E402
from mrs3.lots import LotMethod, allocate_lots  # noqa: E402
from mrs3.strategy_json import generate_strategy  # noqa: E402

CUT_DATES = ["2026-09-01", "2026-09-15"]
TEST_DAYS, MAX_TF, ADMIT = 28, 2, 10.0
TESTER_WARMUP_DAYS = 4


def candle_span(folder, symbol):
    days = sorted(f.name[len(symbol):len(symbol) + 10] for f in folder.glob(f"{symbol}*_1m.parquet"))
    return (np.datetime64(days[0]), np.datetime64(days[-1])) if days else None


def covered(candles, symbol, t_date):
    span = candle_span(candles / symbol, symbol) if (candles / symbol).is_dir() else None
    t = np.datetime64(t_date)
    return span is not None and span[0] <= t - TESTER_WARMUP_DAYS and span[1] >= t + TEST_DAYS - 1


def parse_args(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("export"), ap.add_argument("work"), ap.add_argument("out"), ap.add_argument("candles")
    ap.add_argument("--dates", default=",".join(CUT_DATES), help="comma list or 'all'")
    ap.add_argument("--max-tf", type=int, default=MAX_TF)
    ap.add_argument("--exclude", action="append", default=[], help="structures.csv of an earlier set")
    ap.add_argument("--ranks", type=int, default=1, help="structures per admitted scope")
    ap.add_argument("--max-tf-reject", type=int, help="cap of rejected scopes per (cut, symbol); default --max-tf")
    ap.add_argument("--data-end", help="YYYY-MM-DD: the tester downloads candles itself; require only T+28 <= this date")
    ap.add_argument("--tfs", default=",".join(sorted(TFS)), help="comma list of timeframes")
    ap.add_argument("--diverse", type=int, default=0,
                    help="K: only scopes whose top score > --div-min; up to K structures above it that differ from every picked one")
    ap.add_argument("--div-min", type=float, default=20.0)
    ap.add_argument("--exclude-level", choices=("scope", "structure"), default="scope")
    return ap.parse_args(argv)


def close_of(ids):
    return int(ids[0].split("|")[9])


def orders_text(ids):
    parts = sorted((point_parts(i) for i in ids), key=lambda x: x["shift_bp"])
    return " ".join(f"{x['shift_bp']}bp/MA{x['open_ma']}" for x in parts)


def zone(shifts):
    return {SI[s] + k for s in shifts for k in (-1, 0, 1)}


def diverse_pick(cand, k, min_score):
    """Greedy by score: keep a structure only if, against every kept one, its close MA differs by >= 2
    or its shift levels lie in another zone (no level within one grid step)."""
    kept = []
    for score, shifts, ids in cand:
        if score <= min_score or len(kept) >= k:
            break
        if all(abs(close_of(ids) - close_of(o[2])) >= 2 or not (zone(shifts) & zone(o[1])) for o in kept):
            kept.append((score, shifts, ids))
    return kept


def point_parts(pid):
    p = pid.split("|")
    return dict(symbol=p[0], side=p[1], timeframe=p[2], shift_bp=int(p[3]), open_ma=int(p[6]), close_ma=int(p[9]))


def main():
    args = parse_args(sys.argv[1:])
    b, work, out, candles = Path(args.export), Path(args.work), Path(args.out), Path(args.candles)
    c = duckdb.connect()
    cuts = c.execute(f"""select scope, cut, any_value(s_ms) s_ms, any_value(t_ms) t_ms, max(forward_days) fmax
        from read_parquet('{b.as_posix()}/outcomes.parquet') where variant='exp' group by all""").df()
    cuts["t_date"] = pd.to_datetime(cuts.t_ms, unit="ms").dt.date.astype(str)
    cuts["symbol"] = cuts.scope.str.split("|").str[0]
    cuts["timeframe"] = cuts.scope.str.split("|").str[2]
    cuts = cuts[cuts.timeframe.isin(args.tfs.split(","))]
    if args.dates != "all":
        cuts = cuts[cuts.t_date.isin(args.dates.split(","))]
    if args.data_end:
        cuts = cuts[[np.datetime64(t) + TEST_DAYS <= np.datetime64(args.data_end) for t in cuts.t_date]]
    else:
        cuts = cuts[[covered(candles, s, t) for s, t in zip(cuts.symbol, cuts.t_date)]]
    done_structures = set()
    for path in args.exclude:
        done = pd.read_csv(path)
        if args.exclude_level == "scope":
            done_keys = set(zip(done.t_date, done.symbol, done.timeframe))
            cuts = cuts[[k not in done_keys for k in zip(cuts.t_date, cuts.symbol, cuts.timeframe)]]
        else:
            done_structures |= set(zip(done.t_date, done.symbol, done.timeframe, done.close_ma, done.orders))
    if args.diverse:
        local_pair_concept.KEEP = 10 ** 6
    c.register("sc", cuts[["scope", "cut"]])
    feats = c.execute(f"select f.* from read_parquet('{(work / 'deep_features.parquet').as_posix()}') f join sc using (scope, cut)").df()

    picks = []
    for cut in cuts.itertuples(index=False):
        cand = v4_candidates(feats[(feats.scope == cut.scope) & (feats.cut == cut.cut)])
        if not cand:
            continue
        if args.diverse:
            if cand[0][0] <= args.div_min:
                continue
            for rank, (score, shifts, ids) in enumerate(diverse_pick(cand, args.diverse, args.div_min), 1):
                key = (cut.t_date, cut.symbol, cut.timeframe, close_of(ids), orders_text(ids))
                if key in done_structures:
                    continue
                picks.append(dict(scope=cut.scope, cut=cut.cut, symbol=cut.symbol, t_date=cut.t_date, s_ms=cut.s_ms,
                                  t_ms=cut.t_ms, fmax=int(cut.fmax), score=score, ids=ids, group="DIVERSE", rank=rank,
                                  scope_score=cand[0][0]))
            continue
        group = "ADMIT" if cand[0][0] > ADMIT else "REJECT"
        # the scope's top structure decides the group; admitted scopes also get lower ranks (READY top-k)
        for rank, (score, shifts, ids) in enumerate(cand[:args.ranks if group == "ADMIT" else 1], 1):
            picks.append(dict(scope=cut.scope, cut=cut.cut, symbol=cut.symbol, t_date=cut.t_date, s_ms=cut.s_ms,
                              t_ms=cut.t_ms, fmax=int(cut.fmax), score=score, ids=ids, group=group, rank=rank,
                              scope_score=cand[0][0]))
    p = pd.DataFrame(picks)
    if not args.diverse:
        top = p[p["rank"] == 1].sort_values("score", ascending=False)
        cap = top.group.map({"ADMIT": args.max_tf, "REJECT": args.max_tf_reject or args.max_tf})
        top = top[top.groupby(["t_date", "symbol", "group"]).cumcount() < cap]
        p = p.merge(top[["scope", "cut"]], on=["scope", "cut"])

    c.register("ps", p[["scope"]].drop_duplicates())
    daily = c.execute(f"""select d.scope, d.point_id, d.utc_day, d.balance_close, d.equity_close, d.equity_min,
        d.trades_closed, d.initial_balance from read_parquet('{b.as_posix()}/daily_equity.part0*.parquet') d join ps using (scope)""").df()
    config = AlgorithmConfig()
    template = json.loads((ROOT / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))
    tester = json.loads((ROOT / "templates/exchanges/Bybit/mrs3-tester.json").read_text(encoding="utf-8"))
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
        structure = dict(structure_id=f"{u.group}{u.rank}_{u.t_date[5:].replace('-', '')}_{digest}", symbol=u.symbol, side="LONG", timeframe=parts[0]["timeframe"],
                         order_count=len(parts), common_close_ma=parts[0]["close_ma"],
                         orders=[dict(id=k, open_ma=x["open_ma"], shift_bp=x["shift_bp"]) for k, x in enumerate(parts, 1)])
        lots = allocate_lots(structure["orders"], LotMethod.EQUAL, config)
        strategy = generate_strategy(template, structure, lots, LotMethod.EQUAL, config)
        folder = out / f"T_{u.t_date}" / "strategies"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{strategy['name']}.json").write_text(json.dumps(strategy, ensure_ascii=False, indent=2), encoding="utf-8")
        index.append(dict(t_date=u.t_date, symbol=u.symbol, timeframe=structure["timeframe"], method=u.group, rank=u.rank, scope_score=round(u.scope_score, 2),
                          strategy=strategy["name"], score=round(u.score, 2),
                          band="hi" if u.score > ADMIT else ("mid" if u.score > 5 else "lo"), close_ma=structure["common_close_ma"],
                          orders=" ".join(f"{x['shift_bp']}bp/MA{x['open_ma']}" for x in parts), proxy_days=F,
                          proxy_train_upi=round(r["train_upi"], 3), proxy_train_maxdd=round(r["train_maxdd"], 3),
                          proxy_ret30=round(r["ret30"], 3), proxy_dd_fwd=round(r["dd_fwd"], 3),
                          proxy_r_ex=round(r["r_ex"], 3), proxy_loss=r["loss_neg"]))
    idx = pd.DataFrame(index)
    names = idx.strategy
    assert names.is_unique, "strategy names must be unique"
    for t_date in idx.t_date.unique():
        cfg = dict(tester)
        cfg["StartDate"] = t_date
        cfg["EndDate"] = str(np.datetime64(t_date) + TEST_DAYS)
        cfg["WarmupDays"] = TESTER_WARMUP_DAYS
        (out / f"T_{t_date}" / "config_tester.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    idx.to_csv(out / "structures.csv", index=False, encoding="utf-8")
    print(idx.groupby(["t_date", "method"]).agg(n=("strategy", "size"), symbols=("symbol", "nunique"),
                                                 score=("score", "median"), proxy_ret30=("proxy_ret30", "mean"),
                                                 proxy_dd=("proxy_dd_fwd", "mean")).round(2).to_string())
    print(idx[["t_date", "symbol", "timeframe", "method", "score", "orders"]].to_string(index=False))
    print("total strategies:", len(idx))


if __name__ == "__main__":
    main()
