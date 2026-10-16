"""EQUAL vs INCOME lots on real MRS3 tick tests: the same structure and window, paired.

Usage: local_lot_method_compare.py <strategy_performance.duckdb>
Pairs strategies whose names differ only in the _EQUAL/_INCOME suffix and whose current results
share the report window; 2- and 3-order structures, fixed-lot results only.
"""
import sys

import duckdb
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)


def boot(v, n=4000, seed=3):
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    rng = np.random.default_rng(seed)
    m = v[rng.integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    return v.mean(), *np.percentile(m, [2.5, 97.5])


def main():
    c = duckdb.connect(sys.argv[1], read_only=True)
    df = c.execute("""
        select s.strategy_name, s.symbol, s.timeframe, s.order_count, r.report_start_utc, r.report_end_utc,
               r.total_pnl_pct, r.max_drawdown_pct, r.total_trades,
               date_diff('second', r.effective_start_utc, r.effective_end_utc) / 86400.0 as test_days,
               (select max(o.lot_x) from strategy_orders o where o.strategy_id = s.strategy_id and o.order_id = 1) as lot_first
        from strategies s join strategy_results r on r.result_id = s.current_result_id
        where s.order_count >= 2 and r.sizing_use_fix and not r.sizing_use_upnl""").df()
    df["method"] = np.where(df.strategy_name.str.endswith("_INCOME"), "INCOME", "EQUAL")
    df["key"] = df.strategy_name.str.replace(r"_(EQUAL|INCOME)$", "", regex=True)
    df["ret30"] = df.total_pnl_pct * 30 / df.test_days
    df["dd"] = df.max_drawdown_pct
    df["q"] = df.ret30 * 5 / df.dd.clip(lower=1)
    df["loss"] = (df.total_pnl_pct < 0).astype(float)
    df["hit10"] = (df.q >= 10).astype(float)
    w = df.pivot_table(index=["key", "symbol", "timeframe", "order_count", "report_start_utc", "report_end_utc"],
                       columns="method", values=["ret30", "dd", "q", "loss", "hit10", "lot_first", "test_days"], aggfunc="first").dropna()
    print(f"paired structures {len(w)}, symbols {w.index.get_level_values('symbol').nunique()}; "
          f"median test days {w[("test_days", "EQUAL")].median():.0f}; INCOME first-order lot median {w[('lot_first', 'INCOME')].median():.2f}")

    print("\n== INCOME minus EQUAL (per-symbol mean, bootstrap over symbols) ==")
    for oc in (2, 3, None):
        sub = w if oc is None else w[w.index.get_level_values("order_count") == oc]
        label = "all" if oc is None else f"{oc} orders"
        print(f"\n-- {label}: {len(sub)} pairs")
        print("   levels EQUAL / INCOME:", {m: (round(sub[(m, 'EQUAL')].mean(), 2), round(sub[(m, 'INCOME')].mean(), 2))
                                          for m in ("ret30", "dd", "q", "loss", "hit10")})
        for m in ("ret30", "dd", "q", "loss", "hit10"):
            d = (sub[(m, "INCOME")] - sub[(m, "EQUAL")])
            ps = d.groupby(level="symbol").mean()
            mean, lo, hi = boot(ps.values)
            better = (d > 0).mean() if m in ("ret30", "q", "hit10") else (d < 0).mean()
            print(f"   {m:6} {mean:+7.3f} [{lo:+.3f}; {hi:+.3f}]  structures where INCOME better {better:.0%}, "
                  f"symbols where INCOME better {((ps > 0) if m in ('ret30', 'q', 'hit10') else (ps < 0)).mean():.0%}")

    print("\n== by how strongly INCOME tilts to the first order (INCOME first-order lot) ==")
    w2 = w.copy()
    w2["tilt"] = pd.cut(w2[("lot_first", "INCOME")], [0, 0.4, 0.55, 0.7, 1.01])
    out = {}
    for m in ("ret30", "dd", "q", "loss"):
        out[m] = (w2[(m, "INCOME")] - w2[(m, "EQUAL")]).groupby(w2.tilt, observed=True).mean()
    out["n"] = w2.groupby("tilt", observed=True).size()
    print(pd.DataFrame(out).round(3).to_string())

    print("\n== which variant is better per structure by q: share where INCOME wins, by EQUAL q level ==")
    w2["eq_q"] = pd.cut(w2[("q", "EQUAL")], [-1e9, 0, 5, 10, 20, 1e9])
    print(((w2[("q", "INCOME")] > w2[("q", "EQUAL")]).groupby(w2.eq_q, observed=True).agg(["mean", "size"])).round(2).to_string())


if __name__ == "__main__":
    main()
