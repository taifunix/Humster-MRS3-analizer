"""Rich train-window features per (scope, cut, point) from the daily export.

Point features come from a dense, forward-filled daily path of the balance chain
and equity (close/min/max). Neighbourhood features are computed on the full
19 x 6 x 6 grid cube of each scope, failed points included.
"""
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

GRID = [30, 40, 50, 60, 70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550]
SI = {s: i for i, s in enumerate(GRID)}
DAY = 86_400_000
WARMUP_DAYS = 4

POINT_FEATURES = [
    "pnl30", "maxdd", "eff", "ulcer", "upi", "underwater", "pos_weeks", "act_weeks",
    "worst_week", "sharpe_w", "min_third_ratio", "min_third_trades", "recent_ratio",
    "trades30",
]
NB_SOURCES = ["eff", "upi", "pnl30", "pos_weeks", "sharpe_w", "maxdd"]


def scope_features(args):
    scope, daily, cuts = args
    days = pd.to_datetime(daily["utc_day"]).values.astype("datetime64[D]")
    d0 = min(days.min(), min(np.datetime64(int(s_ // DAY), "D") for s_ in cuts.s_ms)) - 1
    d1 = max(days.max(), max(np.datetime64(int(t // DAY), "D") for t in cuts.t_ms) + 30)
    n_days = int((d1 - d0).astype(int)) + 1
    pids = sorted(daily.point_id.unique())
    pidx = {p: i for i, p in enumerate(pids)}
    n = len(pids)
    ib = daily.groupby("point_id").initial_balance.first().reindex(pids).values
    di = (days - d0).astype(int)
    pi = daily.point_id.map(pidx).values

    def dense(col, fill_first):
        a = np.full((n, n_days), np.nan)
        a[pi, di] = daily[col].values
        a = pd.DataFrame(a).ffill(axis=1).values
        first = np.where(np.isnan(a), 1, 0)
        a = np.where(np.isnan(a), fill_first[:, None], a)
        return a

    bal = dense("balance_close", ib)
    eqc = dense("equity_close", ib)
    eqmin = np.full((n, n_days), np.nan)
    eqmin[pi, di] = daily.equity_min.values
    eqmin = np.where(np.isnan(eqmin), eqc, eqmin)
    eqmax = np.full((n, n_days), np.nan)
    eqmax[pi, di] = daily.equity_max.values
    eqmax = np.where(np.isnan(eqmax), eqc, eqmax)
    trades = np.zeros((n, n_days))
    trades[pi, di] = daily.trades_closed.values

    meta = pd.DataFrame({"point_id": pids})
    parts = meta.point_id.str.split("|")
    meta["si"] = parts.str[3].astype(int).map(SI)
    meta["oi"] = parts.str[6].astype(int) - 2
    meta["ci"] = parts.str[9].astype(int) - 2

    out = []
    for cut in cuts.itertuples(index=False):
        s = int((np.datetime64(int(cut.s_ms // DAY), "D") - d0).astype(int)) + WARMUP_DAYS
        t = int((np.datetime64(int(cut.t_ms // DAY), "D") - d0).astype(int))
        L = t - s
        base = ib
        bal_start = bal[:, s - 1] if s > 0 else ib
        eq_start = eqc[:, s - 1] if s > 0 else ib
        pnl = bal[:, t - 1] - bal_start
        f = {"pnl30": pnl / base * 100 * 30 / L}
        prev_max = np.maximum.accumulate(np.concatenate([eq_start[:, None], eqmax[:, s:t - 1]], axis=1), axis=1)
        dd = np.clip((prev_max - eqmin[:, s:t]) / prev_max * 100, 0, None)
        f["maxdd"] = dd.max(axis=1)
        f["eff"] = f["pnl30"] / np.maximum(f["maxdd"], 0.5)
        f["ulcer"] = np.sqrt((dd ** 2).mean(axis=1))
        f["upi"] = f["pnl30"] / np.maximum(f["ulcer"], 0.25)
        f["underwater"] = (dd > 1.0).mean(axis=1)
        n_w = L // 7
        edges = [t - 7 * k for k in range(n_w, -1, -1)]
        wb = np.stack([bal[:, e - 1] if e > 0 else ib for e in edges], axis=1)
        wp = np.diff(wb, axis=1) / base[:, None] * 100
        wt = np.stack([trades[:, edges[k]:edges[k + 1]].sum(axis=1) for k in range(n_w)], axis=1)
        f["pos_weeks"] = (wp > 0).mean(axis=1)
        f["act_weeks"] = (wt > 0).mean(axis=1)
        f["worst_week"] = wp.min(axis=1)
        sd = wp.std(axis=1)
        f["sharpe_w"] = np.where(sd > 0, wp.mean(axis=1) / np.where(sd > 0, sd, 1), 0.0)
        b1, b2 = s + round(L / 3), s + round(2 * L / 3)
        tb = [bal_start] + [bal[:, x - 1] for x in (b1, b2, t)]
        lens = np.array([b1 - s, b2 - b1, t - b2], dtype=float)
        rates = np.stack([(tb[k + 1] - tb[k]) / lens[k] for k in range(3)], axis=1)
        mean_rate = pnl / L
        f["min_third_ratio"] = np.where(mean_rate > 0, rates.min(axis=1) / np.where(mean_rate > 0, mean_rate, 1), np.nan)
        f["min_third_trades"] = np.stack([trades[:, a:b].sum(axis=1) for a, b in ((s, b1), (b1, b2), (b2, t))], axis=1).min(axis=1)
        r14 = (bal[:, t - 1] - (bal[:, t - 15] if t - 15 >= 0 else ib)) / 14
        f["recent_ratio"] = np.where(mean_rate > 0, r14 / np.where(mean_rate > 0, mean_rate, 1), np.nan)
        f["trades30"] = trades[:, s:t].sum(axis=1) * 30 / L
        frame = meta.copy()
        for k, v in f.items():
            frame[k] = v
        # neighbourhood features on the grid cube
        for src in NB_SOURCES:
            cube = np.full((19, 6, 6), np.nan)
            cube[frame.si, frame.oi, frame.ci] = frame[src].values
            padded = np.pad(cube, 1, constant_values=np.nan)
            views = [padded[1 + a:20 + a, 1 + b:7 + b, 1 + c:7 + c]
                     for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)]
            stack = np.stack(views)
            with np.errstate(all="ignore"):
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    q25 = np.nanpercentile(stack, 25, axis=0)
                    med = np.nanmedian(stack, axis=0)
                    mn = np.nanmin(stack, axis=0)
            frame[f"nb_q25_{src}"] = q25[frame.si, frame.oi, frame.ci]
            frame[f"nb_med_{src}"] = med[frame.si, frame.oi, frame.ci]
            frame[f"nb_min_{src}"] = mn[frame.si, frame.oi, frame.ci]
            if src == "eff":
                padded2 = np.pad(cube, ((2, 2), (1, 1), (1, 1)), constant_values=np.nan)
                views2 = [padded2[2 + a:21 + a, 1 + b:7 + b, 1 + c:7 + c]
                          for a in (-2, -1, 0, 1, 2) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)]
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    q25w = np.nanpercentile(np.stack(views2), 25, axis=0)
                frame["nb2_q25_eff"] = q25w[frame.si, frame.oi, frame.ci]
        pos = np.full((19, 6, 6), np.nan)
        pos[frame.si, frame.oi, frame.ci] = (frame.pnl30.values > 0).astype(float)
        padded = np.pad(pos, 1, constant_values=np.nan)
        stack = np.stack([padded[1 + a:20 + a, 1 + b:7 + b, 1 + c:7 + c]
                          for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)])
        frame["nb_pos_share"] = np.nanmean(stack, axis=0)[frame.si, frame.oi, frame.ci]
        frame["scope"] = scope
        frame["cut"] = cut.cut
        frame["train_days"] = L
        out.append(frame)
    return pd.concat(out, ignore_index=True)


def main():
    b, work = sys.argv[1], Path(sys.argv[2])
    c = duckdb.connect()
    c.execute("set threads to 16")
    cuts = c.execute(f"""select distinct scope, cut, s_ms, t_ms from read_parquet('{b}/outcomes.parquet')
                         where variant='exp' and forward_days=14""").df()
    scopes = sorted(cuts.scope.unique())
    c.register("sc", pd.DataFrame({"scope": scopes}))
    daily = c.execute(f"""select d.scope, d.point_id, d.utc_day, d.balance_close, d.equity_close, d.equity_min,
                          d.equity_max, d.trades_closed, d.initial_balance
                          from read_parquet('{b}/daily_equity.part0*.parquet') d join sc using (scope)""").df()
    print("daily rows", len(daily), "scopes", len(scopes), flush=True)
    groups = {k: v for k, v in daily.groupby("scope", sort=True)}
    del daily
    tasks = [(s, groups[s], cuts[cuts.scope == s]) for s in scopes]
    frames = []
    with ProcessPoolExecutor(max_workers=24) as pool:
        for i, fr in enumerate(pool.map(scope_features, tasks, chunksize=4)):
            frames.append(fr)
            if i % 50 == 0:
                print("scope", i, flush=True)
    feats = pd.concat(frames, ignore_index=True)
    c.register("feats", feats)
    out = (work / "deep_features.parquet").as_posix()
    c.execute(f"copy (select * from feats) to '{out}' (format parquet, compression zstd)")
    print("written", out, feats.shape)


if __name__ == "__main__":
    main()
