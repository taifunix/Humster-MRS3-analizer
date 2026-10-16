"""B11: MRS2 grid 70-750 bp from CSV-only tester runs (train window vs forward window).

Usage: local_grid750_eval.py <tester report root> <work dir>
Reads `grid750_<G>_train_hist` and `grid750_<G>_fwd_hist` folders, validates them with the
CSV Direct loader, joins train and forward rows by point and prints point-level and
structure-level forward outcomes. Structure outcomes are proxies built from member rows:
ret = mean member return, dd = mean member DD (an upper bound of the averaged curve DD).
"""
import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)

SHIFTS = [70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550, 590, 630, 670, 710, 750]
SI = {s: i for i, s in enumerate(SHIFTS)}
BUCKETS = [(70, 90, "070-090"), (110, 200, "110-200"), (230, 390, "230-390"), (430, 550, "430-550"), (590, 750, "590-750")]
COLS = {"settings[*].basic.symbol": "symbol", "settings[*].basic.time_frame": "tf",
        "settings[*].mrs2.ma_long.len": "open_ma", "settings[*].mrs2.ma_close_long.len": "close_ma",
        "settings[*].mrs2.ma_long.multiplier": "mult"}
MIN_TRAIN_TRADES = 6
RNG = np.random.default_rng(7)


def bucket(shift):
    for lo, hi, name in BUCKETS:
        if lo <= shift <= hi:
            return name
    return "other"


def validate(folder):
    sys.path.insert(0, str(ROOT / ".worktrees" / "csv-import-integration" / "src"))
    from mrs3.csv_direct_input import load_csv_direct
    head = pd.read_csv(folder / "reports_history.csv", usecols=["StartDate", "settings[*].basic.symbol"])
    listing = {s: head.StartDate.iloc[0] for s in head["settings[*].basic.symbol"].unique()}
    d = load_csv_direct([folder], listing_dates=listing, allow_shared_snapshot_template=True)
    return len(d.frame), len(d.scope_keys)


def read(folder, prefix):
    df = pd.read_csv(folder / "reports_history.csv").rename(columns=COLS)
    days = (pd.to_datetime(df.EndDate) - pd.to_datetime(df.StartDate)).dt.days
    df["shift"] = ((1 - df.mult) * 10000).round().astype(int)
    out = df[["symbol", "tf", "shift", "open_ma", "close_ma"]].copy()
    out[prefix + "ret30"] = df.TotalPnLPercent * 30 / days
    out[prefix + "dd"] = df.MaxDrawdownPercent
    out[prefix + "ulcer"] = df.UlcerIndex.fillna(0.0)
    out[prefix + "trades"] = df.TotalTrades
    out[prefix + "days"] = days
    months = [c for c in df.columns if c.startswith("MonthlyReturn_")]
    out[prefix + "neg_months"] = (df[months] < 0).sum(axis=1)
    return out


def neighbourhood(scope):
    """Median over the 26 grid neighbours (shift index, open MA, close MA), the point itself excluded."""
    cube = np.full((len(SHIFTS), 6, 6), np.nan)
    si, oi, ci = scope["shift"].map(SI).values, scope.open_ma.values - 2, scope.close_ma.values - 2
    cube[si, oi, ci] = scope.upi.values
    p = np.pad(cube, 1, constant_values=np.nan)
    n = len(SHIFTS)
    views = np.stack([p[1 + a:n + 1 + a, 1 + b:7 + b, 1 + c:7 + c]
                      for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)])
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            med = np.nanmedian(views, axis=0)
    return med[si, oi, ci]


def load(root):
    parts = []
    for group in ("G1", "G2"):
        tr, fw = root / f"grid750_{group}_train_hist", root / f"grid750_{group}_fwd_hist"
        for folder in (tr, fw):
            rows, scopes = validate(folder)
            print(f"CSV Direct OK: {folder.name}: points {rows}, scopes {scopes}")
        key = ["symbol", "tf", "shift", "open_ma", "close_ma"]
        m = read(tr, "t_").merge(read(fw, "f_"), on=key, how="inner", validate="one_to_one")
        m["group"] = group
        parts.append(m)
    df = pd.concat(parts, ignore_index=True)
    df["symbol"] = df.symbol.str.replace("USDT", "", regex=False)
    df["upi"] = df.t_ret30 / np.maximum(df.t_ulcer, 0.25)
    df["nb_med_upi"] = np.nan
    for _, idx in df.groupby(["symbol", "tf"]).groups.items():
        df.loc[idx, "nb_med_upi"] = neighbourhood(df.loc[idx])
    df["bucket"] = df["shift"].map(bucket)
    df["active"] = df.t_trades >= MIN_TRAIN_TRADES
    df["f_loss"] = (df.f_ret30 < 0).astype(float)
    df["f_notrade"] = (df.f_trades == 0).astype(float)
    df["f_q"] = df.f_ret30 * 5 / df.f_dd.clip(lower=1)
    df["f_hit10"] = (df.f_ret30 >= 10).astype(float)
    return df


def per_symbol(frame, cols):
    """Mean over symbols of per-symbol means (each pair weighs the same)."""
    return frame.groupby("symbol")[cols].mean().mean()


OUT = ["f_ret30", "f_dd", "f_q", "f_loss", "f_notrade", "f_hit10"]


def points(df):
    print("\n== 1. activity on train by depth: share of points with >= 6 train trades; mean train trades ==")
    t = df.groupby(["bucket", "tf"]).agg(active=("active", "mean"), trades=("t_trades", "mean")).unstack("tf")
    print(t.round(2).to_string())

    a = df[df.active].copy()
    a["robust"] = a.groupby(["symbol", "tf"]).nb_med_upi.rank(pct=True) >= 0.75
    print("\n== 2. forward by depth, active points: all vs robust (top-25% nb_med_upi in scope); per-symbol mean ==")
    rows = []
    for b, g in a.groupby("bucket"):
        for label, sub in (("all", g), ("robust", g[g.robust])):
            r = per_symbol(sub, OUT + ["t_ret30", "t_dd"])
            r["n"] = len(sub)
            r["share_of_robust"] = g.robust.sum() / max(a.robust.sum(), 1)
            rows.append(r.rename((b, label)))
    print(pd.DataFrame(rows).round(3).to_string())

    print("\n== 3. within-scope Spearman of train scores with forward outcome, by depth (active points) ==")
    rows = []
    for b, g in a.groupby("bucket"):
        r = {}
        for score in ("nb_med_upi", "upi", "t_ret30"):
            for target in ("f_q", "f_dd", "f_ret30"):
                c = g.groupby(["symbol", "tf"]).apply(
                    lambda x: x[[score, target]].corr(method="spearman").iloc[0, 1] if len(x) >= 8 else np.nan,
                    include_groups=False).dropna()
                r[f"{score}~{target}"] = c.median()
        rows.append(pd.Series(r, name=b))
    print(pd.DataFrame(rows).round(2).to_string())

    print("\n== 4. Open MA by depth: forward outcome minus the (scope, shift, close MA) mean, active points ==")
    a["oma"] = pd.cut(a.open_ma, [1, 3, 5, 7], labels=["2-3", "4-5", "6-7"])
    for col in ("f_dd", "f_ret30", "f_loss", "f_notrade"):
        a[col + "_x"] = a[col] - a.groupby(["symbol", "tf", "shift", "close_ma"])[col].transform("mean")
    t = a.groupby(["bucket", "oma"], observed=True)[["f_dd_x", "f_ret30_x", "f_loss_x", "f_notrade_x"]].mean()
    print(t.round(3).unstack("oma").to_string())

    print("\n== 5. TF: robust active points, forward (per-symbol mean) ==")
    rows = [per_symbol(g[g.robust], OUT).rename(tf) for tf, g in a.groupby("tf")]
    print(pd.DataFrame(rows).round(3).to_string())
    return a


def structures(df, ratio, pool, floor, cap, sizes=(2, 3)):
    rows = []
    act = df[df.active & (df["shift"] >= floor) & (df["shift"] <= cap) & df.nb_med_upi.notna()]
    for (sym, tf, close), g in act.groupby(["symbol", "tf", "close_ma"]):
        # one candidate per shift level (its best open MA), else the smooth score fills the pool with one shift pair
        best = g.sort_values("nb_med_upi", ascending=False).drop_duplicates("shift")
        cand = best.head(pool)
        pts = sorted(cand.itertuples(index=False), key=lambda r: r.shift)
        for n in sizes:
            for combo in itertools.combinations(pts, n):
                sh = [p.shift for p in combo]
                if len(set(sh)) < n or not all(r >= ratio * l for l, r in zip(sh, sh[1:])):
                    continue
                rows.append(dict(symbol=sym, tf=tf, close_ma=close, n=n, shifts="/".join(map(str, sh)),
                                 first=sh[0], last=sh[-1],
                                 score=np.mean([p.nb_med_upi for p in combo]),
                                 t_ret30=np.mean([p.t_ret30 for p in combo]),
                                 f_ret30=np.mean([p.f_ret30 for p in combo]),
                                 f_dd=np.mean([p.f_dd for p in combo]),
                                 f_trades=sum(p.f_trades for p in combo)))
    s = pd.DataFrame(rows)
    if s.empty:
        return s
    s["f_q"] = s.f_ret30 * 5 / s.f_dd.clip(lower=1)
    s["f_loss"] = (s.f_ret30 < 0).astype(float)
    s["f_notrade"] = (s.f_trades == 0).astype(float)
    s["f_hit10"] = (s.f_ret30 >= 10).astype(float)
    return s


def random_structures(df, ratio, floor, cap, per_scope=300):
    rows = []
    act = df[df.active & (df["shift"] >= floor) & (df["shift"] <= cap)]
    for (sym, tf), g in act.groupby(["symbol", "tf"]):
        for close, gc in g.groupby("close_ma"):
            recs = list(gc.itertuples(index=False))
            if len(recs) < 2:
                continue
            for _ in range(per_scope // 6):
                n = RNG.choice([2, 3])
                if len(recs) < n:
                    continue
                combo = sorted((recs[i] for i in RNG.choice(len(recs), n, replace=False)), key=lambda r: r.shift)
                sh = [p.shift for p in combo]
                if len(set(sh)) < n or not all(r >= ratio * l for l, r in zip(sh, sh[1:])):
                    continue
                rows.append(dict(symbol=sym, tf=tf, f_ret30=np.mean([p.f_ret30 for p in combo]),
                                 f_dd=np.mean([p.f_dd for p in combo]), f_trades=sum(p.f_trades for p in combo)))
    s = pd.DataFrame(rows)
    s["f_q"] = s.f_ret30 * 5 / s.f_dd.clip(lower=1)
    s["f_loss"] = (s.f_ret30 < 0).astype(float)
    s["f_notrade"] = (s.f_trades == 0).astype(float)
    s["f_hit10"] = (s.f_ret30 >= 10).astype(float)
    return s


def top(s, k, per="tf"):
    """Top-k structures by score: per (symbol, TF) or per symbol across TFs."""
    key = ["symbol", "tf"] if per == "tf" else ["symbol"]
    return s.sort_values("score", ascending=False).groupby(key).head(k)


SOUT = ["f_ret30", "f_dd", "f_q", "f_loss", "f_notrade", "f_hit10"]


def structure_study(df, work):
    base = dict(ratio=1.5, pool=6, floor=70, cap=750)
    rnd = random_structures(df, 1.5, 70, 750)
    print("\n== 6. structures: top-1 per (symbol, TF) by mean member nb_med_upi; per-symbol mean ==")
    print("   random = random valid 2-3 order structures (ratio 1.5) from active points")
    rows = [per_symbol(rnd, SOUT).rename("random")]
    variants = [("v4 base", base)]
    variants += [(f"ratio {r}", {**base, "ratio": r}) for r in (1.3, 2.0, 2.5)]
    variants += [(f"floor {f}", {**base, "floor": f}) for f in (90, 110, 140, 200, 310)]
    variants += [("cap 550", {**base, "cap": 550}), ("cap 390", {**base, "cap": 390})]
    variants += [(f"pool {p}", {**base, "pool": p}) for p in (4, 10)]
    cache = {}
    for name, v in variants:
        s = structures(df, v["ratio"], v["pool"], v["floor"], v["cap"])
        cache[name] = s
        t = top(s, 1)
        r = per_symbol(t, SOUT)
        r["first_med"] = t["first"].median()
        r["last_med"] = t["last"].median()
        r["n3_share"] = (t.n == 3).mean()
        rows.append(r.rename(name))
    s = cache["v4 base"]
    for n in (2, 3):
        t = top(s[s.n == n], 1)
        rows.append(per_symbol(t, SOUT).rename(f"only n={n}"))
    print(pd.DataFrame(rows).round(3).to_string())

    print("\n== 7. READY top-k per symbol across all TFs (v4 base): mean of picked ==")
    rows = []
    for k in (1, 3, 5, 10):
        t = top(s, k, per="symbol")
        r = per_symbol(t, SOUT)
        r["tfs_used"] = t.groupby("symbol").tf.nunique().mean()
        rows.append(r.rename(f"top-{k}"))
    print(pd.DataFrame(rows).round(3).to_string())

    print("\n== 8. where the deep orders sit in v4 picks: share of top-1 structures with last order in each bucket ==")
    t = top(s, 1)
    print(t["last"].map(bucket).value_counts(normalize=True).sort_index().round(3).to_string())
    print("   forward of top-1 by the bucket of its last order (per-structure mean):")
    print(t.assign(lb=t["last"].map(bucket)).groupby("lb")[SOUT].mean().round(3).to_string())

    print("\n== 9. per pair (A13): v4 top-1 per TF vs random; train pair features ==")
    t = top(s, 1)
    a = df[df.active]
    pair = pd.DataFrame({
        "v4_ret30": t.groupby("symbol").f_ret30.mean(), "v4_dd": t.groupby("symbol").f_dd.mean(),
        "v4_loss": t.groupby("symbol").f_loss.mean(), "v4_q": t.groupby("symbol").f_q.mean(),
        "rnd_ret30": rnd.groupby("symbol").f_ret30.mean(), "rnd_q": rnd.groupby("symbol").f_q.mean(),
        "rnd_loss": rnd.groupby("symbol").f_loss.mean(),
        "train_best_score": t.groupby("symbol").score.max(), "train_med_score": t.groupby("symbol").score.median(),
        "train_pos_share": a.groupby("symbol").apply(lambda g: (g.t_ret30 > 0).mean(), include_groups=False),
        "train_active_share": df.groupby("symbol").active.mean(),
        "train_ret30_med": a.groupby("symbol").t_ret30.median(),
        "group": df.groupby("symbol").group.first(),
    })
    pair["v4_minus_rnd_q"] = pair.v4_q - pair.rnd_q
    print(pair.sort_values("v4_q", ascending=False).round(2).to_string())
    print("\n   Spearman across pairs (n=10) of train pair feature vs forward v4 q / ret30:")
    for feat in ("train_best_score", "train_med_score", "train_pos_share", "train_active_share", "train_ret30_med"):
        print(f"   {feat:20} ~ v4_q {pair[[feat, 'v4_q']].corr(method='spearman').iloc[0, 1]:+.2f}"
              f"   ~ v4_ret30 {pair[[feat, 'v4_ret30']].corr(method='spearman').iloc[0, 1]:+.2f}")
    pair.to_csv(work / "grid750_pairs.csv", encoding="utf-8")
    s.to_csv(work / "grid750_structures_v4.csv", index=False, encoding="utf-8")

    print("\n== 10. per pair: v4 base minus variant (top-1 per TF), q and loss; positive q = base better ==")
    rows = {}
    bt = top(s, 1).groupby("symbol")[["f_q", "f_loss"]].mean()
    for name in ("ratio 1.3", "ratio 2.0", "floor 110", "floor 200", "cap 550", "cap 390"):
        vt = top(cache[name], 1).groupby("symbol")[["f_q", "f_loss"]].mean()
        rows[(name, "dq")] = bt.f_q - vt.f_q
        rows[(name, "dloss")] = bt.f_loss - vt.f_loss
    print(pd.DataFrame(rows).round(2).to_string())


def main():
    root, work = Path(sys.argv[1]), Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    df = load(root)
    print(f"points {len(df)}, symbols {df.symbol.nunique()}, scopes {df.groupby(['symbol', 'tf']).ngroups}, "
          f"train days {sorted(df.t_days.unique())}, fwd days {sorted(df.f_days.unique())}")
    df.to_csv(work / "grid750_points.csv", index=False, encoding="utf-8")
    points(df)
    structure_study(df, work)


if __name__ == "__main__":
    main()
