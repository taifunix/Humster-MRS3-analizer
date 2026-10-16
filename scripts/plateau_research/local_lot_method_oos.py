"""EQUAL vs INCOME on the out-of-sample tail of real MRS3 tick tests (after the analysis window end).

Usage: local_lot_method_oos.py <strategy_performance.duckdb> <analysis dir with *.analysis-v6.duckdb>
The analysis end per symbol is read from the root analysis database file names
(<SYM>_<SYM>_<start>_<end>.analysis-v6.duckdb); symbols found in several files are skipped.
Tail metrics from strategy_equity: PnL % of the initial balance and max drawdown of equity in the tail.
"""
import re
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)
MIN_TAIL_DAYS = 7


def analysis_ends(folder):
    ends = {}
    for f in Path(folder).glob("*.analysis-v6.duckdb"):
        m = re.match(r"(.+)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.analysis-v6\.duckdb$", f.name)
        if not m:
            continue
        for sym in m.group(1).split("_"):
            ends.setdefault(sym + "USDT", set()).add(m.group(3))
    return {s: max(v) for s, v in ends.items() if len(v) == 1}


def boot(v, n=4000, seed=5):
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    rng = np.random.default_rng(seed)
    m = v[rng.integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    return v.mean(), *np.percentile(m, [2.5, 97.5])


def main():
    c = duckdb.connect(sys.argv[1], read_only=True)
    ends = analysis_ends(sys.argv[2])
    c.register("ends", pd.DataFrame({"symbol": list(ends), "a_end_s": list(ends.values())}))
    df = c.execute("""
        with s as (
            select s.strategy_id, s.strategy_name, s.symbol, s.order_count, r.result_id, r.initial_balance,
                   cast(e.a_end_s || ' 00:00:00+00' as timestamptz) as a_end, r.report_end_utc
            from strategies s join strategy_results r on r.result_id = s.current_result_id join ends e using (symbol)
            where s.order_count >= 2 and r.sizing_use_fix and not r.sizing_use_upnl
              and date_diff('day', cast(e.a_end_s || ' 00:00:00+00' as timestamptz), r.report_end_utc) >= ?),
        q as (
            select s.*, q.sample_index, q.equity, q.timestamp_utc
            from s join strategy_equity q using (result_id) where q.timestamp_utc >= s.a_end),
        dd as (
            select result_id, max((peak - equity) / peak) * 100 as dd_tail,
                   arg_max(equity, sample_index) as eq_last, arg_min(equity, sample_index) as eq_first,
                   date_diff('second', min(timestamp_utc), max(timestamp_utc)) / 86400.0 as tail_days
            from (select *, max(equity) over (partition by result_id order by sample_index) as peak from q)
            group by result_id)
        select s.strategy_name, s.symbol, s.order_count, s.initial_balance, s.a_end, dd.*
        from s join dd using (result_id)""", [MIN_TAIL_DAYS]).df()
    df["method"] = np.where(df.strategy_name.str.endswith("_INCOME"), "INCOME", "EQUAL")
    df["key"] = df.strategy_name.str.replace(r"_(EQUAL|INCOME)$", "", regex=True)
    df["ret30"] = (df.eq_last - df.eq_first) / df.initial_balance * 100 * 30 / df.tail_days
    df["q"] = df.ret30 * 5 / df.dd_tail.clip(lower=1)
    df["loss"] = (df.eq_last < df.eq_first).astype(float)
    w = df.pivot_table(index=["key", "symbol", "order_count"], columns="method",
                       values=["ret30", "dd_tail", "q", "loss", "tail_days"], aggfunc="first").dropna()
    print(f"symbols with a known analysis end: {len(ends)}; paired structures with tail >= {MIN_TAIL_DAYS} days: {len(w)}, "
          f"symbols {w.index.get_level_values('symbol').nunique()}, median tail days {w[('tail_days', 'EQUAL')].median():.0f}")
    print(w.groupby(level="symbol").size().rename("pairs").to_frame().join(
        df.groupby("symbol").a_end.first()).to_string())
    for oc in (2, 3, None):
        sub = w if oc is None else w[w.index.get_level_values("order_count") == oc]
        if not len(sub):
            continue
        print(f"\n-- {'all' if oc is None else f'{oc} orders'}: {len(sub)} pairs; levels EQUAL / INCOME:",
              {m: (round(sub[(m, 'EQUAL')].mean(), 2), round(sub[(m, 'INCOME')].mean(), 2)) for m in ("ret30", "dd_tail", "q", "loss")})
        for m in ("ret30", "dd_tail", "q", "loss"):
            d = sub[(m, "INCOME")] - sub[(m, "EQUAL")]
            ps = d.groupby(level="symbol").mean()
            mean, lo, hi = boot(ps.values)
            up = m in ("ret30", "q")
            print(f"   {m:7} INCOME-EQUAL {mean:+7.3f} [{lo:+.3f}; {hi:+.3f}]  structures where INCOME better "
                  f"{((d > 0) if up else (d < 0)).mean():.0%}, symbols {((ps > 0) if up else (ps < 0)).mean():.0%}")


if __name__ == "__main__":
    main()
