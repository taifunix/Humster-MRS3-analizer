"""Train-window equity features of tick-tested structures (Source v6 summed member curves, daily)
against their real forward MRS3 tick-test result: direction UP/DOWN by periods, smoothness, Ulcer,
floating-loss (MAE-like) measure.

Usage: local_equity_features_proxy.py <export dir> <tick results csv> <output csv>
Samples: all rank-1 structures, then the clean ones (score > 10, score > 20). Comparisons are made
within the same cut date (same market month), so the market move does not drive the result.
"""
import re
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from local_deep_struct import DAY, WARMUP, dense, path_dd  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)


def point_ids(row):
    out = []
    for token in row.orders.split():
        shift, ma = re.match(r"(\d+)bp/MA(\d+)", token).groups()
        out.append(f"{row.symbol}|LONG|{row.timeframe}|{shift}|SMA|ohlc4|{ma}|SMA|ohlc4|{row.close_ma}")
    return out


def features(B, C, M, s, t):
    """Daily summed balance B, equity close C, equity min M; train days [s, t)."""
    eq = C[s - 1:t]
    base = C[s - 1]
    L = t - s
    f = {}
    for w in (7, 14, 28):
        if L >= w:
            f[f"ret{w}"] = (C[t - 1] - C[t - 1 - w]) / base * 100 * 30 / w
            f[f"up{w}"] = float(C[t - 1] > C[t - 1 - w])
    f["ret_full30"] = (C[t - 1] - base) / base * 100 * 30 / L
    edges = [s - 1 + round(L * k / 3) for k in range(4)]
    thirds = [C[edges[k + 1]] - C[edges[k]] for k in range(3)]
    f["up_thirds"] = float(sum(v > 0 for v in thirds))
    f["last_third_share"] = thirds[2] / max(sum(abs(v) for v in thirds), 1e-9)
    steps = np.abs(np.diff(eq)).sum()
    f["er"] = (eq[-1] - eq[0]) / steps if steps > 0 else 0.0
    dd = path_dd(C[s - 1], C[s:t], M[s:t])
    f["ulcer"] = np.sqrt((dd ** 2).mean())
    f["maxdd"] = dd.max()
    f["dd_end"] = (np.maximum.accumulate(C[s - 1:t]).max() - C[t - 1]) / np.maximum.accumulate(C[s - 1:t]).max() * 100
    f["under_water"] = (dd > 1.0).mean()
    f["float_mean"] = ((M[s:t] - B[s:t]) / B[s:t] * 100).mean()
    f["upi"] = f["ret_full30"] / max(f["ulcer"], 0.25)
    peak_before_w7 = C[s - 1:t - 7].max() if L > 7 else np.nan
    f["new_ath_w7"] = float(C[t - 7:t].max() > peak_before_w7) if L > 7 else np.nan
    return f


def main():
    b, res_path, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    res = pd.read_csv(res_path)
    res = res.copy().rename(columns={"ulcer": "fwd_ulcer"})
    res["scope"] = res.symbol + "|LONG|" + res.timeframe
    c = duckdb.connect()
    cuts = c.execute(f"""select scope, any_value(s_ms) s_ms, t_ms from read_parquet('{b.as_posix()}/outcomes.parquet')
                         where variant='exp' group by scope, t_ms""").df()
    cuts["t_date"] = pd.to_datetime(cuts.t_ms, unit="ms").dt.date.astype(str)
    res = res.merge(cuts, on=["scope", "t_date"], how="left")
    print(f"strategies {len(res)}, with a train window {res.s_ms.notna().sum()}")
    c.register("sc", res[["scope"]].drop_duplicates())
    daily = c.execute(f"""select d.scope, d.point_id, d.utc_day, d.balance_close, d.equity_close, d.equity_min,
        d.trades_closed, d.initial_balance from read_parquet('{b.as_posix()}/daily_equity.part0*.parquet') d join sc using (scope)""").df()
    rows = []
    for scope, g in res.dropna(subset=["s_ms"]).groupby("scope"):
        d = daily[daily.scope == scope]
        d0, pidx, ib, bal, eqc, eqmin, tr = dense(d, g[["s_ms", "t_ms"]])
        for r in g.itertuples(index=False):
            ids = point_ids(r)
            if any(i not in pidx for i in ids):
                continue
            idx = [pidx[i] for i in ids]
            s = int((np.datetime64(int(r.s_ms // DAY), "D") - d0).astype(int)) + WARMUP
            t = int((np.datetime64(int(r.t_ms // DAY), "D") - d0).astype(int))
            f = features(bal[idx].sum(0), eqc[idx].sum(0), eqmin[idx].sum(0), s, t)
            rows.append(dict(strategy=r.strategy, train_days=t - s, **f))
    feat = pd.DataFrame(rows)
    df = res.merge(feat, on="strategy")
    df.to_csv(out, index=False, encoding="utf-8")
    df = df[df["rank"] == 1]
    print(f"with features {len(df)}; median train days {df.train_days.median():.0f}")

    targets = ["ret30", "dd", "q", "loss"]
    feats = ["up7", "up14", "up28", "up_thirds", "ret7", "ret14", "ret28", "ret_full30", "last_third_share",
             "er", "ulcer", "maxdd", "dd_end", "under_water", "float_mean", "upi", "new_ath_w7", "score"]
    samples = {"all": df, "score > 10": df[df.score > 10], "score > 20": df[df.score > 20]}

    print("\n== 1. Spearman within the same cut date (median over dates with >= 8 strategies) ==")
    for name, sub in samples.items():
        rows = {}
        for feat_name in feats:
            r = {}
            for target in targets:
                cs = [g[[feat_name, target]].corr(method="spearman").iloc[0, 1]
                      for _, g in sub.groupby("t_date") if len(g) >= 8 and g[feat_name].nunique() > 1]
                cs = [v for v in cs if np.isfinite(v)]
                r[target] = np.median(cs) if cs else np.nan
            r["dates"] = len(cs)
            rows[feat_name] = r
        print(f"\n-- {name}: {len(sub)} strategies, {sub.symbol.nunique()} pairs")
        print(pd.DataFrame(rows).T.round(2).to_string())

    print("\n== 2. UP vs DOWN groups (outcome minus the cut-date mean, so the market month is removed) ==")
    for name, sub in samples.items():
        sub = sub.copy()
        for t in targets:
            sub[t + "_x"] = sub[t] - sub.groupby("t_date")[t].transform("mean")
        rows = []
        for flag in ("up7", "up14", "up28", "new_ath_w7"):
            for v in (1.0, 0.0):
                g = sub[sub[flag] == v]
                rows.append(pd.Series({**{t: g[t + "_x"].mean() for t in targets}, "n": len(g)}, name=(flag, "UP" if v else "DOWN")))
        for v in (0.0, 1.0, 2.0, 3.0):
            g = sub[sub.up_thirds == v]
            rows.append(pd.Series({**{t: g[t + "_x"].mean() for t in targets}, "n": len(g)}, name=("up_thirds", int(v))))
        print(f"\n-- {name}")
        print(pd.DataFrame(rows).round(2).to_string())

    print("\n== 3. tertiles within the cut date: safest third vs riskiest third (outcome minus date mean) ==")
    for name, sub in samples.items():
        sub = sub.copy()
        for t in targets:
            sub[t + "_x"] = sub[t] - sub.groupby("t_date")[t].transform("mean")
        rows = []
        for feat_name, safe_high in (("er", True), ("ulcer", False), ("maxdd", False), ("float_mean", True),
                                     ("under_water", False), ("ret14", True), ("ret28", True), ("upi", True)):
            rk = sub.groupby("t_date")[feat_name].rank(pct=True)
            safe = sub[rk >= 2 / 3] if safe_high else sub[rk <= 1 / 3]
            risky = sub[rk <= 1 / 3] if safe_high else sub[rk >= 2 / 3]
            rows.append(pd.Series({**{f"safe_{t}": safe[t + "_x"].mean() for t in ("ret30", "dd", "loss")},
                                   **{f"risky_{t}": risky[t + "_x"].mean() for t in ("ret30", "dd", "loss")}}, name=feat_name))
        print(f"\n-- {name}")
        print(pd.DataFrame(rows).round(2).to_string())


if __name__ == "__main__":
    main()
