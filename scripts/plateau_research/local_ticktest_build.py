"""Build the limited MRS3 tick-test set: v4 candidate structures vs production structures.

Usage: local_ticktest_build.py <export dir> <work dir> <output dir>
For each selected symbol: the latest F=28 cut where both pools exist, the 3 most active
timeframes (train data only), top-2 structures per pool by the summed-curve train UPI.
Writes strategy JSON (protocol v2 Bybit template, EQUAL lots), one tester config per
cut date and an index with the proxy forward metrics.
"""
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
from mrs3.config import AlgorithmConfig  # noqa: E402
from mrs3.lots import LotMethod, allocate_lots  # noqa: E402
from mrs3.strategy_json import generate_strategy  # noqa: E402

SYMBOLS = ["LLYUSDT", "SNOWUSDT", "IONQUSDT", "CLSKUSDT", "REGNUSDT",
           "RDWUSDT", "PURRUSDT", "FLYUSDT", "XOMUSDT", "CXMTUSDT"]
F = 28
TOP_N, TOP_K, TFS_PER_SYMBOL, GAP_RATIO = 6, 2, 3, 1.5
TESTER_WARMUP_DAYS = 4


def candidate_structures(fc):
    act = fc[(fc.min_third_trades >= 1) & (fc.trades30 * fc.train_days / 30 >= 6) & fc.nb_med_upi.notna()]
    cand = act.sort_values("nb_med_upi", ascending=False).groupby("ci").head(TOP_N)
    out = []
    for _, sl in cand.groupby("ci"):
        pts = sorted(zip((GRID[int(v)] for v in sl.si), sl.point_id))
        for n in (2, 3):
            for combo in itertools.combinations(pts, n):
                shifts = [x[0] for x in combo]
                if len(set(shifts)) == n and all(r >= GAP_RATIO * l for l, r in zip(shifts, shifts[1:])):
                    out.append([x[1] for x in combo])
    return out


def point_parts(pid):
    p = pid.split("|")
    return dict(symbol=p[0], side=p[1], timeframe=p[2], shift_bp=int(p[3]), open_ma=int(p[6]), close_ma=int(p[9]))


def main():
    b, work, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    c = duckdb.connect()
    sc = c.execute(f"select distinct scope, symbol, timeframe from read_parquet('{b.as_posix()}/scopes.parquet') where kept").df()
    sc = sc[sc.symbol.isin(SYMBOLS)]
    cuts = c.execute(f"""select scope, cut, any_value(s_ms) s_ms, any_value(t_ms) t_ms
        from read_parquet('{b.as_posix()}/outcomes.parquet') where variant='exp' and forward_days={F} group by all""").df()
    cuts = cuts[cuts.scope.isin(sc.scope)]
    prod = c.execute(f"""select scope, cut, orders from read_parquet('{b.as_posix()}/stages_structures.parquet')
        where wtype='train_exp' and status='READY_MRS3_STRUCTURE'""").df()
    feats = c.execute(f"select * from read_parquet('{(work / 'deep_features.parquet').as_posix()}')").df()
    config = AlgorithmConfig()
    template = json.loads((ROOT / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))
    tester = json.loads((ROOT / "templates/exchanges/Bybit/mrs3-tester.json").read_text(encoding="utf-8"))
    index = []
    for symbol in SYMBOLS:
        scopes = sc[sc.symbol == symbol].scope.tolist()
        sym_cuts = cuts[cuts.scope.isin(scopes)]
        choices = []
        for cut in sym_cuts.itertuples(index=False):
            fc = feats[(feats.scope == cut.scope) & (feats.cut == cut.cut)]
            cand = candidate_structures(fc)
            pr = prod[(prod.scope == cut.scope) & (prod.cut == cut.cut)]
            if cand and len(pr):
                activity = int(((fc.min_third_trades >= 1) & (fc.trades30 * fc.train_days / 30 >= 6)).sum())
                choices.append((cut, fc, cand, pr, activity))
        if not choices:
            print("no comparable cut for", symbol)
            continue
        latest_t = max(ch[0].t_ms for ch in choices)
        latest = sorted([ch for ch in choices if ch[0].t_ms == latest_t], key=lambda ch: -ch[4])[:TFS_PER_SYMBOL]
        daily = c.execute(f"""select scope, point_id, utc_day, balance_close, equity_close, equity_min, trades_closed, initial_balance
            from read_parquet('{b.as_posix()}/daily_equity.part0*.parquet') where scope in ({",".join("'" + ch[0].scope + "'" for ch in latest)})""").df()
        for cut, fc, cand, pr, _ in latest:
            d = daily[daily.scope == cut.scope]
            cutdf = pd.DataFrame([dict(s_ms=cut.s_ms, t_ms=cut.t_ms)])
            d0, pidx, ib, bal, eqc, eqmin, tr = dense(d, cutdf)
            s = int((np.datetime64(int(cut.s_ms // DAY), "D") - d0).astype(int)) + WARMUP
            t = int((np.datetime64(int(cut.t_ms // DAY), "D") - d0).astype(int))
            arrays = (ib, bal, eqc, eqmin, tr)
            pools = {"V4": cand, "PROD": [[o["point_id"] for o in json.loads(x)] for x in pr.orders]}
            for method, structures in pools.items():
                scored = []
                for pids in structures:
                    if any(p not in pidx for p in pids):
                        continue
                    r = evaluate([pidx[p] for p in pids], arrays, s, t, F)
                    scored.append((r["train_upi"], pids, r))
                scored.sort(key=lambda x: -x[0])
                for rank, (_, pids, r) in enumerate(scored[:TOP_K], start=1):
                    parts = [point_parts(p) for p in pids]
                    parts.sort(key=lambda p: p["shift_bp"])
                    digest = hashlib.sha256("|".join(pids).encode()).hexdigest()[:8]
                    t_date = str(np.datetime64(int(cut.t_ms // DAY), "D"))
                    structure = dict(
                        structure_id=f"{method}{rank}_{digest}", symbol=symbol, side="LONG",
                        timeframe=parts[0]["timeframe"], order_count=len(parts),
                        common_close_ma=parts[0]["close_ma"],
                        orders=[dict(id=i, open_ma=p["open_ma"], shift_bp=p["shift_bp"]) for i, p in enumerate(parts, 1)],
                    )
                    lots = allocate_lots(structure["orders"], LotMethod.EQUAL, config)
                    strategy = generate_strategy(template, structure, lots, LotMethod.EQUAL, config)
                    folder = out / f"T_{t_date}" / "strategies"
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / f"{strategy['name']}.json").write_text(json.dumps(strategy, ensure_ascii=False, indent=2), encoding="utf-8")
                    index.append(dict(t_date=t_date, symbol=symbol, timeframe=structure["timeframe"], method=method, rank=rank,
                                      strategy=strategy["name"], close_ma=structure["common_close_ma"],
                                      orders=" ".join(f"{p['shift_bp']}bp/MA{p['open_ma']}" for p in parts),
                                      proxy_train_upi=round(r["train_upi"], 3), proxy_train_maxdd=round(r["train_maxdd"], 3),
                                      proxy_ret30=round(r["ret30"], 3), proxy_dd_fwd=round(r["dd_fwd"], 3),
                                      proxy_r_ex=round(r["r_ex"], 3), proxy_loss=r["loss_neg"]))
    idx = pd.DataFrame(index)
    for t_date, g in idx.groupby("t_date"):
        cfg = dict(tester)
        cfg["StartDate"] = t_date
        cfg["EndDate"] = str(np.datetime64(t_date) + F)
        cfg["WarmupDays"] = TESTER_WARMUP_DAYS
        (out / f"T_{t_date}" / "config_tester.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    idx.to_csv(out / "structures.csv", index=False, encoding="utf-8")
    print(idx.groupby(["t_date", "method"]).size().to_string())
    print("total strategies:", len(idx))


if __name__ == "__main__":
    main()
