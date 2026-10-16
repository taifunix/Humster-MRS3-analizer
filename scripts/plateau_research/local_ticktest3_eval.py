"""Evaluate the admission-rule tick tests over several sets (ticktest2-4): score bands, top-1 vs top-2,
train-drawdown cap, per-pair and per-cut breakdown. Real MRS3 reports, EQUAL lots.

Usage: local_ticktest3_eval.py <output csv> <set dir> [<set dir> ...]
Each set dir holds T_*/reports/*.html and structures.csv (local_ticktest2_build.py).
"""
import glob
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from local_ticktest_eval import parse  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 30)
COLS = ["ret30", "dd", "q", "loss", "hit10", "trades"]


def boot(v, n=4000, seed=11):
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 3:
        return (v.mean() if len(v) else np.nan), np.nan, np.nan
    rng = np.random.default_rng(seed)
    m = v[rng.integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    return v.mean(), *np.percentile(m, [2.5, 97.5])


def per_symbol(frame, cols=COLS):
    return frame.groupby("symbol")[cols].mean().mean()


def load(sets):
    files, idx = [], []
    for s in sets:
        s = Path(s)
        files += [f for f in glob.glob(str(s / "T_*" / "reports" / "*.html")) if not Path(f).name.startswith("report_optimizer")]
        i = pd.read_csv(s / "structures.csv")
        i["set"] = s.name
        idx.append(i)
    with ProcessPoolExecutor(max_workers=16) as pool:
        rep = pd.DataFrame(list(pool.map(parse, files, chunksize=4)))
    dup = rep.groupby("strategy").pnl_pct.agg(["count", "nunique"])
    print(f"reports {len(rep)}, strategies {rep.strategy.nunique()}, repeated {int((dup['count'] > 1).sum())}, "
          f"repeats with different PnL {int((dup['nunique'] > 1).sum())}")
    rep = rep.drop_duplicates("strategy")
    idx = pd.concat(idx, ignore_index=True)
    for col, default in (("band", None), ("scope_score", None)):
        if col not in idx:
            idx[col] = default
    idx["band"] = idx.band.fillna(pd.Series(np.where(idx.score > 10, "hi", np.where(idx.score > 5, "mid", "lo")), index=idx.index))
    idx["scope_score"] = idx.scope_score.fillna(idx.score)
    df = idx.merge(rep, on="strategy", how="left", indicator=True)
    print(f"index {len(idx)}, matched {int((df._merge == 'both').sum())}, missing {int((df._merge == 'left_only').sum())}")
    df = df[df._merge == "both"].drop(columns="_merge").copy()
    df["ret30"] = df.pnl_pct * 30 / df.days
    df["q"] = df.ret30 * 5 / df.dd.clip(lower=1)
    df["loss"] = (df.pnl_pct < 0).astype(float)
    df["hit10"] = (df.ret30 >= 10).astype(float)
    df["group"] = np.where(df.method == "ADMIT", "ADMIT r" + df["rank"].astype(str), "REJECT " + df.band)
    return df


def main():
    out, sets = Path(sys.argv[1]), sys.argv[2:]
    df = load(sets)
    df.to_csv(out, index=False, encoding="utf-8")
    r1 = df[df["rank"] == 1]

    print("\n== 1. groups: mean over strategies | per-symbol mean ==")
    g = df.groupby("group")
    t = g[COLS].mean().round(2).join(g.size().rename("n")).join(g.symbol.nunique().rename("symbols"))
    t2 = pd.DataFrame({k: per_symbol(v) for k, v in df.groupby("group")}).T.round(2).add_prefix("ps_")
    print(t.join(t2[["ps_ret30", "ps_dd", "ps_loss"]]).to_string())
    print("   medians:", df.groupby("group")[["ret30", "dd"]].median().round(2).to_dict())

    print("\n== 2. top structures (rank 1) by score band ==")
    r1 = r1.assign(sbin=pd.cut(r1.score, [-1e9, 2, 5, 10, 20, 40, 1e9]))
    g = r1.groupby("sbin", observed=True)
    print(g[COLS].mean().round(2).join(g.size().rename("n")).join(g.symbol.nunique().rename("symbols")).to_string())

    print("\n== 3. ADMIT vs REJECT, per cut date (rank 1) ==")
    g = r1.groupby(["t_date", "method"])
    print(g[["ret30", "dd", "q", "loss"]].mean().round(2).join(g.size().rename("n")).unstack("method").to_string())
    d = r1.groupby(["t_date", "method"]).ret30.mean().unstack()
    print(f"   cuts where ADMIT ret30 > REJECT: {(d.ADMIT > d.REJECT).sum()} of {d.dropna().shape[0]}")

    print("\n== 4. admitted (rank 1): train drawdown cap ==")
    a = r1[r1.method == "ADMIT"]
    rows = []
    for cap in (2, 3, 4, 5, 6, 8, 1e9):
        s = a[a.proxy_train_maxdd <= cap]
        r = s[COLS].mean()
        r["n"], r["symbols"], r["dd_max"], r["dd_p90"] = len(s), s.symbol.nunique(), s.dd.max(), s.dd.quantile(0.9)
        rows.append(r.rename(f"train DD <= {cap:g}" if cap < 1e9 else "no cap"))
    print(pd.DataFrame(rows).round(2).to_string())
    print(f"   Spearman train DD ~ real DD (all rank-1): {r1[['proxy_train_maxdd', 'dd']].corr(method='spearman').iloc[0, 1]:+.2f}; "
          f"proxy ret ~ real ret (proxy available): {r1[r1.proxy_days >= 14][['proxy_ret30', 'ret30']].corr(method='spearman').iloc[0, 1]:+.2f}")
    print("   real DD / train DD (admitted):", (a.dd / a.proxy_train_maxdd.clip(lower=0.5)).quantile([0.5, 0.75, 0.9, 0.95]).round(2).to_dict())

    print("\n== 5. top-2 vs top-1 in the same admitted scope (paired) ==")
    key = ["set", "t_date", "symbol", "timeframe"]
    p = df[df.method == "ADMIT"].pivot_table(index=key, columns="rank", values=["ret30", "dd", "q", "loss"], aggfunc="first").dropna()
    for m in ("ret30", "dd", "q", "loss"):
        diff = p[(m, 2)] - p[(m, 1)]
        mean, lo, hi = boot(diff.groupby(level="symbol").mean().values)
        print(f"   {m:5} rank2-rank1 {mean:+7.2f} [{lo:+.2f}; {hi:+.2f}]  pairs {len(p)}")
    print("   mean of both (portfolio of 2) vs rank 1:",
          {m: (round(p[(m, 1)].mean(), 2), round(((p[(m, 1)] + p[(m, 2)]) / 2).mean(), 2)) for m in ("ret30", "dd", "loss")})

    print("\n== 6. per pair: admitted rank-1 results ==")
    s = a.groupby("symbol").agg(n=("ret30", "size"), cuts=("t_date", "nunique"), ret30=("ret30", "mean"),
                                ret30_min=("ret30", "min"), dd=("dd", "mean"), dd_max=("dd", "max"),
                                losses=("loss", "sum"), score=("score", "median"), train_dd=("proxy_train_maxdd", "median"))
    rj = r1[r1.method == "REJECT"].groupby("symbol").agg(rej_n=("ret30", "size"), rej_ret30=("ret30", "mean"), rej_dd=("dd", "mean"))
    print(s.join(rj, how="outer").sort_values("ret30", ascending=False).round(2).to_string())
    print(f"   admitted pairs: {s.shape[0]}; pairs with any losing admitted strategy: {(s.losses > 0).sum()}")

    print("\n== 7. pairs never admitted vs admitted at least once (rank 1) ==")
    adm = set(a.symbol)
    r1 = r1.assign(pair_kind=np.where(r1.symbol.isin(adm), "pair admitted sometimes", "pair never admitted"))
    g = r1.groupby(["pair_kind", "method"])
    print(g[COLS].mean().round(2).join(g.size().rename("n")).join(g.symbol.nunique().rename("symbols")).to_string())

    print("\n== 8. by TF (rank 1) ==")
    g = r1.groupby(["timeframe", "method"])
    print(g[["ret30", "dd", "q", "loss"]].mean().round(2).join(g.size().rename("n")).unstack("method").to_string())


if __name__ == "__main__":
    main()
