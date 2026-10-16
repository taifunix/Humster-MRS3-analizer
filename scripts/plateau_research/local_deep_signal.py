"""Within-scope rank correlation of train features with forward targets."""
import sys
import warnings
warnings.filterwarnings("ignore")
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

b, work = sys.argv[1], Path(sys.argv[2])
c = duckdb.connect()
c.execute("set threads to 16")
rng = np.random.default_rng(7)
feat = (work / "deep_features.parquet").as_posix()

FEATURES = [
    "pnl30", "maxdd", "eff", "ulcer", "upi", "underwater", "pos_weeks", "act_weeks", "worst_week",
    "sharpe_w", "min_third_ratio", "min_third_trades", "recent_ratio", "trades30",
    "nb_q25_eff", "nb_med_eff", "nb_min_eff", "nb2_q25_eff", "nb_q25_upi", "nb_med_upi",
    "nb_q25_pnl30", "nb_q25_pos_weeks", "nb_q25_sharpe_w", "nb_med_maxdd", "nb_pos_share",
    "win_rate_pct", "S_q25", "plateau_size", "shift_bp",
]
TARGETS = {"q_fwd": 1, "loss_neg": -1, "dd_fwd": -1, "no_trades": -1, "r_ex": 1}


def load(F, population):
    where = {"active": "f.min_third_trades >= 1 and f.trades30 * f.train_days / 30 >= 6",
             "econ": "o.economic_pass"}[population]
    return c.execute(f"""
      select f.*, o.symbol, o.half, o.win_rate_pct, o.S_q25, coalesce(o.plateau_size, 0) plateau_size, o.shift_bp,
        o.pnl_fwd_pct * 30.0 / {F} * 5 / greatest(o.dd_fwd_pct, 1.0) q_fwd,
        (o.pnl_fwd_pct < 0)::double loss_neg, o.dd_fwd_pct dd_fwd, (o.trades_fwd = 0)::double no_trades,
        o.pnl_fwd_pct * 30.0 / {F} * 5 / greatest(f.maxdd, 1.0) r_ex
      from read_parquet('{feat}') f join read_parquet('{b}/outcomes.parquet') o using (scope, cut, point_id)
      where o.variant='exp' and o.forward_days={F} and {where}""").df()


def group_spearman(df, x, y):
    g = df[["symbol", "scope", "cut", x, y]].dropna()
    key = ["scope", "cut"]
    g["rx"] = g.groupby(key)[x].rank()
    g["ry"] = g.groupby(key)[y].rank()
    stats = g.groupby(key + ["symbol"]).agg(n=("rx", "size"), sx=("rx", "std"), sy=("ry", "std"))
    cov = g.groupby(key + ["symbol"])[["rx", "ry"]].cov().unstack()[("rx", "ry")]
    rho = (cov / (stats.sx * stats.sy)).where(stats.n >= 8)
    per_symbol = rho.dropna().groupby(level="symbol").mean()
    return per_symbol


def boot(v):
    v = v.values
    if len(v) < 3:
        return np.nan, np.nan, np.nan
    m = v[rng.integers(0, len(v), size=(1000, len(v)))].mean(axis=1)
    return v.mean(), *np.percentile(m, [2.5, 97.5])


rows = []
for F in (28, 14):
    for population in ("active", "econ"):
        df = load(F, population)
        for x in FEATURES:
            for y, sign in TARGETS.items():
                for subset, part in (("A", df[df.half == "A"]), ("B", df[df.half == "B"])):
                    ps = group_spearman(part, x, y) * sign
                    m, lo, hi = boot(ps)
                    rows.append(dict(F=F, population=population, feature=x, target=y, subset=subset,
                                     rho=m, lo=lo, hi=hi, symbols=len(ps)))
        print("done", F, population, len(df), flush=True)
res = pd.DataFrame(rows)
c.register("res_df", res)
c.execute(f"copy (select * from res_df) to '{(work / 'deep_signal.parquet').as_posix()}' (format parquet)")

pd.set_option("display.width", 250)
pd.set_option("display.max_rows", 400)
for F in (28, 14):
    for population in ("active", "econ"):
        t = res[(res["F"] == F) & (res["population"] == population)]
        piv = t.pivot_table(index="feature", columns=["target", "subset"], values="rho").round(2)
        conf = t.assign(sig=((t.lo > 0) | (t.hi < 0)).astype(float)).pivot_table(index="feature", columns=["target", "subset"], values="sig")
        both = pd.DataFrame({tg: ((conf[(tg, "A")] > 0) & (conf[(tg, "B")] > 0) &
                                  (np.sign(piv[(tg, "A")]) == np.sign(piv[(tg, "B")]))).map({True: "*", False: ""})
                             for tg in TARGETS})
        print(f"\n===== F={F} population={population}  (rho signed so that + = better; * = CI excl 0 in both halves, same sign) =====")
        show = pd.concat({tg: piv[tg].astype(str).agg(" / ".join, axis=1) + both[tg] for tg in TARGETS}, axis=1)
        print(show.loc[FEATURES].to_string())
