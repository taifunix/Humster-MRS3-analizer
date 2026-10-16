"""Market regime per pair for the grid750 windows, from the tester's 1m candles.

Usage: local_grid750_market.py <tester candle root (…/data/bybit)> <work dir>
Joins `grid750_pairs.csv` from local_grid750_eval.py and prints, per pair and window:
buy&hold return, daily volatility, max price drawdown, and their relation to the v4 forward outcome.
"""
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)

GROUPS = {"G1": ("2026-07-24", ["SNOW", "PURR", "RDW", "IONQ", "FLY", "LLY"]),
          "G2": ("2026-08-08", ["CXMT", "REGN", "XOM", "CLSK"])}
CUT, END = "2026-09-17", "2026-10-15"


def window(con, root, sym, start, end):
    glob = (root / f"{sym}USDT" / f"{sym}USDT*_1m.parquet").as_posix()
    lo, hi = (int(pd.Timestamp(x, tz="UTC").timestamp() * 1000) for x in (start, end))
    d = con.execute(f"""
        with c as (select "timestamp" ts, "close" px, "low" lw from read_parquet('{glob}')
                   where "timestamp" >= {lo} and "timestamp" < {hi}),
        dly as (select ts // 86400000 dd, arg_max(px, ts) px, min(lw) lw from c group by 1)
        select dd, px as close, lw as low from dly order by dd""").df()
    r = d.close.pct_change().dropna()
    peak = d.close.cummax()
    return dict(bh=(d.close.iloc[-1] / d.close.iloc[0] - 1) * 100, vol=r.std() * 100,
                maxdd=((peak - d.low) / peak).max() * 100, worst_day=r.min() * 100, days=len(d))


def main():
    root, work = Path(sys.argv[1]), Path(sys.argv[2])
    con = duckdb.connect()
    rows = []
    for g, (start, syms) in GROUPS.items():
        for s in syms:
            t, f = window(con, root, s, start, CUT), window(con, root, s, CUT, END)
            rows.append(dict(symbol=s, group=g, **{f"t_{k}": v for k, v in t.items()}, **{f"f_{k}": v for k, v in f.items()}))
    m = pd.DataFrame(rows).set_index("symbol")
    pairs = pd.read_csv(work / "grid750_pairs.csv", index_col=0)
    m = m.join(pairs[["v4_ret30", "v4_dd", "v4_q", "rnd_ret30", "rnd_q", "train_best_score"]])
    m.to_csv(work / "grid750_market.csv", encoding="utf-8")
    print(m.sort_values("v4_q", ascending=False).round(2).to_string())
    print("\nSpearman across pairs:")
    for a, b in (("f_bh", "v4_ret30"), ("f_maxdd", "v4_dd"), ("f_vol", "v4_q"), ("t_vol", "v4_q"),
                 ("t_vol", "f_vol"), ("t_maxdd", "v4_q"), ("t_bh", "f_bh"), ("train_best_score", "t_vol")):
        print(f"  {a:17} ~ {b:9} {m[[a, b]].corr(method='spearman').iloc[0, 1]:+.2f}")


if __name__ == "__main__":
    main()
