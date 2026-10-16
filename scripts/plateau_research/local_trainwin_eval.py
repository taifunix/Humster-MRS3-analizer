"""Real MRS3 tick tests on the training windows vs the real forward 28 days of the same strategies.

Usage: local_trainwin_eval.py <output csv> <forward results csv> [...] -- <trainwin set dir> [...]
Train features come from the tester HTML (equity/wallet series, actions, metrics): average drawdown
(Ulcer, daily floating loss), smoothness, direction UP/DOWN by periods, and the Performance v2 filters
3-5 (hard cutoffs, A/B deterioration, top-5 trades). Comparisons are made within the same cut date,
on all strategies and on the clean samples (v4 score > 10, > 20).
"""
import glob
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
DAY = pd.Timedelta(days=1)


def num(m, key):
    v = m.get(key)
    if v in (None, "n/a"):
        return np.nan
    try:
        return float(str(v).split()[0].replace(",", ""))
    except ValueError:
        return np.nan


def cycles(actions):
    """Completed position cycles: realized PnL summed until the position returns to zero."""
    out, pnl, open_ = [], 0.0, False
    for a in actions:
        pnl += float(a.pnl)
        if float(a.post_size) != 0:
            open_ = True
        elif open_:
            out.append((a.timestamp_utc, pnl))
            pnl, open_ = 0.0, False
    return out


def features(path):
    from mrs3.performance_v2_html import parse_current_performance_v2_html
    from mrs3.performance_v2_store import load_performance_v2_config
    cfg = load_performance_v2_config(ROOT / "config.performance.json")
    r = parse_current_performance_v2_html(Path(path).read_bytes(), cfg)
    m = r.metrics
    f = dict(strategy=r.settings.get("name"), tw_days=num(m, "Days in test"), tw_pnl=num(m, "Total PnL, %"),
             tw_dd=num(m, "Max Drawdown, %"), tw_ulcer=num(m, "Ulcer Index (hourly), %"),
             tw_trades=num(m, "Total Trades (entry → full exit)"), tw_wr=num(m, "Win Rate, %"),
             tw_upnl_dd=num(m, "Max Unrealized Drawdown, % of wallet"))
    f["tw_ret30"] = f["tw_pnl"] * 30 / f["tw_days"] if f["tw_days"] else np.nan
    eq = pd.Series({t: float(v) for t, v in r.equity_series}).sort_index()
    wa = pd.Series({t: float(v) for t, v in r.wallet_series}).sort_index()
    if len(eq) < 10:
        return f
    end = eq.index[-1]
    g = eq.resample("6h").last().ffill()
    lg = np.log(g)
    steps = np.abs(np.diff(lg.values)).sum()
    f["tw_er"] = (lg.iloc[-1] - lg.iloc[0]) / steps if steps > 0 else 0.0
    peak = g.cummax()
    dd = (peak - g) / peak * 100
    f["tw_under_water"] = float((dd > 1).mean())
    f["tw_dd_end"] = float(dd.iloc[-1])
    joined = pd.concat([eq.rename("e"), wa.rename("w")], axis=1).ffill().dropna()
    joined = joined[joined.w > 0]
    fl = ((joined.e - joined.w) / joined.w * 100).resample("1D").min().dropna()
    f["tw_float_mean"] = float(fl.mean()) if len(fl) else np.nan
    for w in (7, 14, 28):
        past = g[: end - w * DAY]
        if len(past):
            f[f"tw_ret{w}"] = (g.iloc[-1] / past.iloc[-1] - 1) * 100 * 30 / w
            f[f"tw_up{w}"] = float(g.iloc[-1] > past.iloc[-1])
    edges = [g.index[0] + (end - g.index[0]) * k / 3 for k in range(4)]
    vals = [g[:e].iloc[-1] for e in edges]
    f["tw_up_thirds"] = float(sum(vals[k + 1] > vals[k] for k in range(3)))
    before = g[: end - 7 * DAY]
    f["tw_new_ath_w7"] = float(g[end - 7 * DAY:].max() > before.max()) if len(before) else np.nan
    # Performance v2 filters 3-5 on the training tick test
    cyc = cycles(r.actions)
    pnl30, ddp = f["tw_ret30"], f["tw_dd"]
    a_part = g[: end - 14 * DAY]
    b_ret = (g.iloc[-1] / a_part.iloc[-1]) ** (30 / 14) * 100 - 100 if len(a_part) else np.nan
    a_days = (a_part.index[-1] - g.index[0]) / DAY if len(a_part) else 0
    a_ret = (a_part.iloc[-1] / g.iloc[0]) ** (30 / a_days) * 100 - 100 if a_days > 0 else np.nan
    b_cycles = [p for t, p in cyc if t > end - 14 * DAY]
    b_wins, b_loss = sum(p > 0 for p in b_cycles), sum(p < 0 for p in b_cycles)
    b_wr = 100 * b_wins / (b_wins + b_loss) if b_wins + b_loss else np.nan
    f.update(tw_b_ret30=b_ret, tw_a_ret30=a_ret, tw_cycles=len(cyc), tw_b_cycles=len(b_cycles))
    hist = f["tw_days"] or 0
    f["f3_hard"] = float((ddp > 23 and pnl30 < 3 * ddp) or pnl30 <= 4
                         or (hist >= 45 and len(cyc) >= 25 and ddp > 0 and pnl30 / ddp < 0.75
                             and not math.isnan(b_ret) and b_ret / ddp < 0.75))
    f["f4_ab"] = float((not math.isnan(b_ret) and b_ret <= 4)
                       or (not math.isnan(a_ret) and not math.isnan(b_ret) and a_ret > 0 and b_ret <= a_ret / 10 and b_ret <= 15)
                       or (len(b_cycles) >= 25 and not math.isnan(b_wr) and b_wr < 55))
    pos = sorted((p for _, p in cyc if p > 0), reverse=True)
    net = sum(p for _, p in cyc)
    f["tw_top5_share"] = sum(pos[:5]) / net * 100 if net > 0 else np.nan
    f["f5_top5"] = float(hist >= 45 and len(pos) >= 25 and net > 0 and f["tw_top5_share"] > 80)
    return f


def main():
    args = sys.argv[1:]
    out = Path(args[0])
    sep = args.index("--")
    fwd = pd.concat([pd.read_csv(p) for p in args[1:sep]], ignore_index=True).drop_duplicates("strategy")
    sets = [Path(p) for p in args[sep + 1:]]
    files = [f for s in sets for f in glob.glob(str(s / "W_*" / "reports" / "*.html")) if not Path(f).name.startswith("report_optimizer")]
    with ProcessPoolExecutor(max_workers=16) as pool:
        tw = pd.DataFrame(list(pool.map(features, files, chunksize=4))).drop_duplicates("strategy")
    df = fwd.merge(tw, on="strategy", how="inner")
    df["band"] = pd.cut(df.score, [-1e9, 10, 20, 1e9], labels=["<=10", "10-20", ">20"])
    df.to_csv(out, index=False, encoding="utf-8")
    print(f"train reports {len(tw)}, joined with forward {len(df)}, pairs {df.symbol.nunique()}, cut dates {df.t_date.nunique()}")
    targets = ["ret30", "dd", "q", "loss"]
    for t in targets:
        df[t + "_x"] = df[t] - df.groupby("t_date")[t].transform("mean")
    samples = {"all": df, "score > 10": df[df.score > 10], "score > 20": df[df.score > 20]}

    print("\n== 0. proxy (Source v6 curves) vs real tick on the training window, Spearman ==")
    for a, b in (("proxy_train_maxdd", "tw_dd"), ("proxy_train_upi", "tw_ret30"), ("score", "tw_ret30")):
        print(f"   {a:18} ~ {b:9} {df[[a, b]].corr(method='spearman').iloc[0, 1]:+.2f}")

    feats = ["tw_ret30", "tw_dd", "tw_ulcer", "tw_float_mean", "tw_upnl_dd", "tw_er", "tw_under_water", "tw_dd_end",
             "tw_ret7", "tw_ret14", "tw_ret28", "tw_b_ret30", "tw_wr", "tw_top5_share", "score"]
    print("\n== 1. Spearman of a training-tick feature with the forward result (pooled, outcomes demeaned by cut date) ==")
    rows = {}
    for name, sub in samples.items():
        for feat in feats:
            for t in ("ret30_x", "dd_x", "q_x", "loss_x"):
                rows.setdefault((feat,), {})[(name, t[:-2])] = sub[[feat, t]].corr(method="spearman").iloc[0, 1]
    print(pd.DataFrame(rows).T.round(2).to_string())
    print("   n:", {k: len(v) for k, v in samples.items()})

    print("\n== 2. safest vs riskiest third within the cut date (outcome minus cut-date mean) ==")
    for name, sub in samples.items():
        rows = []
        for feat, safe_high in (("tw_ulcer", False), ("tw_float_mean", True), ("tw_dd", False), ("tw_upnl_dd", False),
                                ("tw_er", True), ("tw_under_water", False)):
            rk = sub.groupby("t_date")[feat].rank(pct=True)
            safe, risky = (sub[rk >= 2 / 3], sub[rk <= 1 / 3]) if safe_high else (sub[rk <= 1 / 3], sub[rk >= 2 / 3])
            rows.append(pd.Series({**{f"safe_{t}": safe[t + "_x"].mean() for t in targets},
                                   **{f"risky_{t}": risky[t + "_x"].mean() for t in targets}}, name=feat))
        print(f"\n-- {name}")
        print(pd.DataFrame(rows).round(2).to_string())

    print("\n== 3. direction of the training equity: UP vs DOWN (outcome minus cut-date mean) ==")
    for name, sub in samples.items():
        rows = []
        for flag in ("tw_up7", "tw_up14", "tw_up28", "tw_new_ath_w7"):
            for v, label in ((1.0, "UP"), (0.0, "DOWN")):
                g = sub[sub[flag] == v]
                rows.append(pd.Series({**{t: g[t + "_x"].mean() for t in targets}, "n": len(g),
                                       "raw_ret30": g.ret30.mean(), "raw_loss": g.loss.mean()}, name=(flag, label)))
        for v in (0.0, 1.0, 2.0, 3.0):
            g = sub[sub.tw_up_thirds == v]
            rows.append(pd.Series({**{t: g[t + "_x"].mean() for t in targets}, "n": len(g),
                                   "raw_ret30": g.ret30.mean(), "raw_loss": g.loss.mean()}, name=("up_thirds", int(v))))
        print(f"\n-- {name}")
        print(pd.DataFrame(rows).round(2).to_string())

    print("\n== 4. Performance v2 filters 3-5 on the training tick test: excluded vs kept, forward ==")
    for name, sub in samples.items():
        rows = []
        for flt in ("f3_hard", "f4_ab", "f5_top5"):
            for v, label in ((1.0, "excluded"), (0.0, "kept")):
                g = sub[sub[flt] == v]
                rows.append(pd.Series({"n": len(g), **{t: g[t].mean() for t in targets},
                                       **{t + "_x": g[t + "_x"].mean() for t in ("ret30", "dd", "loss")}}, name=(flt, label)))
        print(f"\n-- {name}")
        print(pd.DataFrame(rows).round(2).to_string())

    print("\n== 5. does the training tick add to the score? within score > 20: tertiles of training-tick q ==")
    sub = samples["score > 20"].copy()
    sub["tw_q"] = sub.tw_ret30 * 5 / sub.tw_dd.clip(lower=1)
    for feat in ("tw_q", "tw_ret30", "tw_dd"):
        sub["tert"] = sub.groupby("t_date")[feat].rank(pct=True).pipe(lambda r: pd.cut(r, [0, 1 / 3, 2 / 3, 1], labels=["low", "mid", "high"]))
        print(f"   {feat}:", sub.groupby("tert", observed=True)[["ret30", "dd", "loss"]].mean().round(2).to_dict("index"))


if __name__ == "__main__":
    main()
