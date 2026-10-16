"""Step 4: neighbourhood scores, selection methods M0-M5, outcomes, statistics (H1-H5).

Reads the per-scope cache written by measure.py and writes:
  WORK/analysis/outcomes.parquet   point x cut x variant x F: training features, S_*, outcomes
  WORK/analysis/picks.parquet      method, cut, variant, scope, point (M0: repetition number)
  WORK/analysis/*.parquet          statistic tables used by the report
  WORK/analysis/tables.md          the same tables as markdown

Selection uses training data only. Outcomes use the full-history series (§5.5).
Independence unit: symbol (bootstrap 2000 over symbols).

Run: set PYTHONPATH=src & .venv\\Scripts\\python.exe scripts\\plateau_research\\analyze.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DAY_MS, WORK, read_parquet, shift_bucket, symbol_half, write_parquet  # noqa: E402
from measure import cut_grid  # noqa: E402

OUT = WORK / "analysis"
SHIFTS = [30, 40, 50, 60, 70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550]
SHIFT_INDEX = {s: i for i, s in enumerate(SHIFTS)}
BOOT = 2000
RNG = np.random.default_rng(20261014)
KS = (5, 10, 15)
M0_REPS = 200
PRIMARY = ("M1", "M2", "M4min", "M4q", "M4p", "M5")


# ---------------------------------------------------------------- loading

def _glob(name: str) -> str:
    return (WORK / "scopes" / "*" / "*" / f"{name}.parquet").as_posix()


def load() -> dict[str, pd.DataFrame]:
    con = duckdb.connect()
    q = lambda sql: con.execute(sql).df()  # noqa: E731
    data = {
        "points": q(f"select * from read_parquet('{_glob('stages_points')}', union_by_name=true) where wtype in ('train_exp','train_roll')"),
        "forward": q(f"select * exclude (weeks) from read_parquet('{_glob('forward_points')}', union_by_name=true)"),
        "base": q(f"select database, scope, wtype, cut, point_id, selection_role from read_parquet('{_glob('stages_base_one_order')}', union_by_name=true) where wtype in ('train_exp','train_roll')"),
        "structures": q(f"select * from read_parquet('{_glob('stages_structures')}', union_by_name=true) where wtype in ('train_exp','train_roll')"),
        "plateaus": q(f"select database, scope, wtype, cut, plateau_id, all_point_ids, base_1ord_point_id, cma_representatives, ready from read_parquet('{_glob('stages_plateaus')}', union_by_name=true) where wtype in ('train_exp','train_roll')"),
        "profiles": q(f"select database, scope, wtype, cut, plateau_id, close_ma, point_id, status from read_parquet('{_glob('stages_close_profiles')}', union_by_name=true) where wtype in ('train_exp','train_roll')"),
        "alog": q(f"select * from read_parquet('{_glob('analysis_log')}', union_by_name=true)"),
    }
    con.close()
    return data


def train_windows(plan: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for r in plan.loc[plan["mode"].eq("study")].itertuples(index=False):
        for cut in cut_grid(int(r.ready_start_ms), int(r.ready_end_ms)):
            for variant, wtype in (("exp", "train_exp"), ("roll", "train_roll")):
                s, t = cut[wtype]
                rows.append({"database": r.database, "scope": r.scope, "symbol": r.symbol, "timeframe": r.timeframe,
                             "cut": cut["cut"], "variant": variant, "s_ms": s, "t_ms": t,
                             "train_days": (t - s) / DAY_MS, "same_as_exp": cut["train_roll"] == cut["train_exp"]})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- scores

def neighbourhood_scores(group: pd.DataFrame, days: float) -> pd.DataFrame:
    """S_min, S_q25, S_pool over the clipped 3x3x3 neighbourhood (shift index,
    OpenMA, CloseMA +-1), failing neighbours included with their own values."""
    grid_v = np.full((19, 6, 6), np.nan)   # DD5/30, NaN where DD = 0 or missing
    grid_p = np.full((19, 6, 6), np.nan)   # pnl
    grid_d = np.full((19, 6, 6), np.nan)   # dd
    si = group["shift_bp"].map(SHIFT_INDEX).to_numpy()
    oi = group["open_ma"].to_numpy() - 2
    ci = group["close_ma"].to_numpy() - 2
    pnl = group["pnl_pct"].to_numpy(float)
    dd = group["dd_pct"].to_numpy(float)
    val = np.where(dd > 0, pnl * (30.0 / days) * 5.0 / np.where(dd > 0, dd, 1), np.nan)
    grid_v[si, oi, ci], grid_p[si, oi, ci], grid_d[si, oi, ci] = val, pnl, dd
    out = {"S_min": [], "S_q25": [], "S_pool": [], "nb_size": [], "nb_undefined": [], "nb_missing": []}
    for a, b, c, own in zip(si, oi, ci, val):
        sl = (slice(max(a - 1, 0), a + 2), slice(max(b - 1, 0), b + 2), slice(max(c - 1, 0), c + 2))
        v, p, d = grid_v[sl].ravel(), grid_p[sl].ravel(), grid_d[sl].ravel()
        present = ~np.isnan(p)
        defined = v[~np.isnan(v)]
        out["nb_size"].append(int(present.sum()))
        out["nb_missing"].append(int((~present).sum()))
        out["nb_undefined"].append(int(present.sum() - len(defined)))
        if np.isnan(own) or not len(defined):
            out["S_min"].append(np.nan); out["S_q25"].append(np.nan)
        else:
            out["S_min"].append(float(defined.min())); out["S_q25"].append(float(np.percentile(defined, 25)))
        max_dd = np.nanmax(d) if present.any() else np.nan
        out["S_pool"].append(float(np.nansum(p) * (30.0 / days) * 5.0 / max_dd) if max_dd and max_dd > 0 and not np.isnan(own) else np.nan)
    return pd.DataFrame(out, index=group.index)


# ---------------------------------------------------------------- outcomes

def outcome_columns(frame: pd.DataFrame) -> pd.DataFrame:
    f = frame["forward_days"].astype(float)
    dd_train = frame["dd_train_pct"]
    frame["R"] = frame["pnl_fwd_pct"] * (30.0 / f) * 5.0 / np.maximum(dd_train, 1.0)
    frame["breach"] = frame["dd_fwd_pct"] > dd_train
    frame["breach_F"] = frame["dd_fwd_pct"] > frame["dd_train_rollF_pct"]
    for k in KS:
        frame[f"success_{k}"] = (frame["R"] >= k) & ~frame["breach"]
    frame["bad"] = (frame["pnl_fwd_pct"] <= 0) | frame["breach"]
    frame["no_trades"] = frame["trades_fwd"].eq(0)
    frame["weeks_share"] = frame["weeks_profitable"] / frame["weeks_total"].replace(0, np.nan)
    # Same outcomes on the literal sample-anchored series (secondary, §5.5 note).
    dd_s = frame["dd_train_pct_samples"]
    frame["R_samples"] = frame["pnl_fwd_pct_samples"] * (30.0 / f) * 5.0 / np.maximum(dd_s, 1.0)
    frame["success_10_samples"] = (frame["R_samples"] >= 10) & ~(frame["dd_fwd_pct_samples"] > dd_s)
    return frame


# ---------------------------------------------------------------- statistics helpers

def to_markdown(frame: pd.DataFrame) -> str:
    def cell(v):
        if isinstance(v, (float, np.floating)):
            return "" if np.isnan(v) else f"{v:.3f}"
        return str(v)
    head = "| " + " | ".join(map(str, frame.columns)) + " |"
    sep = "| " + " | ".join("---" for _ in frame.columns) + " |"
    body = ["| " + " | ".join(cell(v) for v in row) + " |" for row in frame.itertuples(index=False)]
    return "\n".join([head, sep, *body])


def boot_symbols(values_by_symbol: pd.DataFrame, stat, n: int = BOOT) -> tuple[float, float, float]:
    """Bootstrap over symbols. `values_by_symbol` has one row per symbol; `stat`
    maps a resampled frame to a scalar."""
    symbols = values_by_symbol.index.to_numpy()
    if len(symbols) == 0:
        return np.nan, np.nan, np.nan
    point = stat(values_by_symbol)
    draws = []
    for _ in range(n):
        sample = values_by_symbol.loc[RNG.choice(symbols, len(symbols), replace=True)]
        draws.append(stat(sample))
    draws = np.array(draws, dtype=float)
    return float(point), float(np.nanpercentile(draws, 2.5)), float(np.nanpercentile(draws, 97.5))


def boot_mean_with_p(per_symbol: pd.Series) -> dict:
    per_symbol = per_symbol.dropna()
    if per_symbol.empty:
        return {"n_symbols": 0}
    values = per_symbol.to_numpy(float)
    draws = np.array([RNG.choice(values, len(values), replace=True).mean() for _ in range(BOOT)])
    p = 2 * min((draws <= 0).mean(), (draws >= 0).mean())
    return {"n_symbols": len(values), "mean_diff": values.mean(), "ci_lo": np.percentile(draws, 2.5),
            "ci_hi": np.percentile(draws, 97.5), "median_diff": float(np.median(values)),
            "share_symbols_better": float((values > 0).mean()), "p_boot": max(p, 1 / BOOT)}


def holm(pvalues: pd.Series) -> pd.Series:
    order = pvalues.sort_values()
    m = len(order)
    adjusted, running = {}, 0.0
    for i, (key, p) in enumerate(order.items()):
        running = max(running, min(1.0, (m - i) * p))
        adjusted[key] = running
    return pd.Series(adjusted).reindex(pvalues.index)


def share_table(frame: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    """Method-level shares with bootstrap CIs over symbols (ratio of sums)."""
    metrics = ["success_5", "success_10", "success_15", "bad", "breach", "breach_F", "no_trades"]
    rows = []
    for key, g in frame.groupby(group_cols, dropna=False):
        key = key if isinstance(key, tuple) else (key,)
        sums = g.groupby("symbol")[metrics].sum().astype(float)
        sums["n"] = g.groupby("symbol").size()
        row = dict(zip(group_cols, key))
        row.update(n_points=int(sums["n"].sum()), n_symbols=len(sums),
                   n_scope_cuts=g[["database", "scope", "cut"]].drop_duplicates().shape[0],
                   median_R=float(g["R"].median()), mean_weeks_share=float(g["weeks_share"].mean()))
        for m in metrics:
            est, lo, hi = boot_symbols(sums, lambda s, m=m: s[m].sum() / s["n"].sum())
            row[m] = est
            if m in ("success_10", "bad"):
                row[f"{m}_lo"], row[f"{m}_hi"] = lo, hi
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- methods

def select_methods(train: pd.DataFrame, data: dict, windows: pd.DataFrame) -> pd.DataFrame:
    """picks: one row per (method, scope, cut, variant, point[, rep])."""
    base = data["base"]
    structures = data["structures"]
    plateaus = data["plateaus"]
    profiles = data["profiles"]
    picks = []
    key_cols = ["database", "scope", "cut", "variant"]
    wmap = {"exp": "train_exp", "roll": "train_roll"}
    for key, g in train.groupby(key_cols, sort=False):
        database, scope, cut, variant = key
        wtype = g["src_wtype"].iloc[0]
        econ = g.loc[g["economic_pass"]]
        sel = lambda d: d.loc[d["database"].eq(database) & d["scope"].eq(scope) & d["wtype"].eq(wtype) & d["cut"].eq(cut)]  # noqa: E731
        m1 = sel(base)["point_id"].tolist()
        k = len(m1) if m1 else 4
        flag = "M1_EMPTY_K4" if not m1 else ""
        add = lambda method, ids, rep=None: picks.extend(  # noqa: E731
            {"method": method, "database": database, "scope": scope, "cut": cut, "variant": variant,
             "point_id": pid, "rep": rep, "k": k, "flag": flag} for pid in ids)
        add("M1", m1)
        s = sel(structures)
        ids = sorted({o["point_id"] for orders in s["orders"] for o in json.loads(orders)}) if not s.empty else []
        add("M1s", ids)
        members = g.loc[g["plateau_id"].notna(), "point_id"].tolist()
        add("M2", members)
        add("M2i", econ.loc[econ["plateau_id"].isna(), "point_id"].tolist())
        if econ.empty:
            continue
        kk = min(k, len(econ))
        ids = econ["point_id"].to_numpy()
        for rep in range(M0_REPS):
            add("M0", RNG.choice(ids, kk, replace=False).tolist(), rep)
        order = lambda col: econ.sort_values([col, "point_id"], ascending=[False, True])  # noqa: E731
        add("M3", order("pnl_pct")["point_id"].head(k).tolist())
        add("M3e", order("efficiency")["point_id"].head(k).tolist())
        for name, col in (("M4min", "S_min"), ("M4q", "S_q25"), ("M4p", "S_pool")):
            add(name, econ.loc[econ[col].notna()].sort_values([col, "point_id"], ascending=[False, True])["point_id"].head(k).tolist())
        # M5 / production pick per plateau (H3)
        p = sel(plateaus)
        byid = g.set_index("point_id")
        for row in p.itertuples(index=False):
            mem = [m for m in json.loads(row.all_point_ids) if m in byid.index]
            if not mem:
                continue
            sm = byid.loc[mem, "S_min"].dropna()
            prod = row.base_1ord_point_id if isinstance(row.base_1ord_point_id, str) and row.base_1ord_point_id else None
            if prod is None and isinstance(row.cma_representatives, str):
                reps = json.loads(row.cma_representatives) or []
                prim = [r for r in reps if r.get("support_status") == "PRIMARY_CLOSE"]
                prod = prim[0]["point_id"] if prim else None
            if sm.empty or prod is None:
                continue
            best = sm.sort_values(ascending=False).index[0]
            picks.append({"method": "M5", "database": database, "scope": scope, "cut": cut, "variant": variant,
                          "point_id": best, "rep": None, "k": k, "flag": flag, "plateau_id": row.plateau_id, "pair_point": prod})
            picks.append({"method": "M5prod", "database": database, "scope": scope, "cut": cut, "variant": variant,
                          "point_id": prod, "rep": None, "k": k, "flag": flag, "plateau_id": row.plateau_id, "pair_point": best})
        pr = sel(profiles)
        for row in pr.itertuples(index=False):
            plat = p.loc[p["plateau_id"].eq(row.plateau_id)]
            if plat.empty:
                continue
            mem = [m for m in json.loads(plat["all_point_ids"].iloc[0]) if m in byid.index]
            cand = byid.loc[mem]
            cand = cand.loc[cand["close_ma"].eq(row.close_ma), "S_min"].dropna()
            if cand.empty:
                continue
            best = cand.sort_values(ascending=False).index[0]
            tag = f"{row.plateau_id}|{row.close_ma}"
            picks.append({"method": "M5c", "database": database, "scope": scope, "cut": cut, "variant": variant,
                          "point_id": best, "rep": None, "k": k, "flag": flag, "plateau_id": tag, "pair_point": row.point_id})
            picks.append({"method": "M5c_prod", "database": database, "scope": scope, "cut": cut, "variant": variant,
                          "point_id": row.point_id, "rep": None, "k": k, "flag": flag, "plateau_id": tag, "pair_point": best})
    return pd.DataFrame(picks)


# ---------------------------------------------------------------- main

def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    plan = read_parquet(WORK / "inventory" / "plan.parquet")
    data = load()
    windows = train_windows(plan)
    pts = data["points"]
    # variant rows: roll falls back to the exp analysis where the windows coincide
    exp = pts.loc[pts["wtype"].eq("train_exp")].assign(variant="exp", src_wtype="train_exp")
    roll = pts.loc[pts["wtype"].eq("train_roll")].assign(variant="roll", src_wtype="train_roll")
    have_roll = roll[["database", "scope", "cut"]].drop_duplicates()
    fallback = exp.merge(windows.loc[windows["variant"].eq("roll") & windows["same_as_exp"], ["database", "scope", "cut"]],
                         on=["database", "scope", "cut"])
    fallback = fallback.merge(have_roll, on=["database", "scope", "cut"], how="left", indicator=True)
    fallback = fallback.loc[fallback["_merge"].eq("left_only")].drop(columns="_merge").assign(variant="roll")
    train = pd.concat([exp, roll, fallback], ignore_index=True)
    train = train.merge(windows[["database", "scope", "cut", "variant", "symbol", "timeframe", "s_ms", "t_ms", "train_days"]],
                        on=["database", "scope", "cut", "variant"], how="left")
    parts = []
    for _, g in train.groupby(["database", "scope", "cut", "variant"], sort=False):
        parts.append(neighbourhood_scores(g, float(g["train_days"].iloc[0])))
    train = train.join(pd.concat(parts))
    train["shift_bucket"] = train["shift_bp"].map(shift_bucket)
    train["half"] = train["symbol"].map(symbol_half)
    train["plateau_size"] = train["plateau_point_count"].fillna(0)

    fwd = data["forward"]
    fwd = fwd.loc[fwd["covered"]]
    long = []
    for variant in ("exp", "roll"):
        cols = {f"dd_series_train_{variant}_pct": "dd_train_pct", f"dd_series_train_{variant}_pct_samples": "dd_train_pct_samples"}
        f = fwd.rename(columns=cols)
        f["dd_train_rollF_pct"] = np.where(f["forward_days"].eq(28), f[f"dd_series_train_{variant}_roll28_pct"],
                                           f[f"dd_series_train_{variant}_roll14_pct"])
        keep = ["database", "scope", "point_id", "cut", "forward_days", "dd_train_pct", "dd_train_pct_samples", "dd_train_rollF_pct",
                "pnl_fwd_pct", "dd_fwd_pct", "pnl_fwd_pct_samples", "dd_fwd_pct_samples", "trades_fwd", "weeks_total",
                "weeks_profitable", "crossing_trades", "crossing_gross_pnl", "open_positions_at_end", "upnl_at_end"]
        long.append(f[keep].assign(variant=variant))
    fwd_long = pd.concat(long, ignore_index=True)
    outcomes = train.merge(fwd_long, on=["database", "scope", "point_id", "cut", "variant"], how="inner")
    outcomes = outcome_columns(outcomes)
    outcomes["t_date"] = pd.to_datetime(outcomes["t_ms"], unit="ms").dt.strftime("%Y-%m-%d")
    write_parquet(outcomes.drop(columns=["reject_reasons"], errors="ignore"), OUT / "outcomes.parquet")
    uncovered = data["forward"].loc[~data["forward"]["covered"]]
    write_parquet(uncovered, OUT / "forward_uncovered.parquet")

    picks = select_methods(train, data, windows)
    write_parquet(picks, OUT / "picks.parquet")
    print("outcomes", outcomes.shape, "picks", picks.shape, flush=True)
    stats(outcomes, picks, train)


def stats(outcomes: pd.DataFrame, picks: pd.DataFrame, train: pd.DataFrame) -> None:
    md: list[str] = []

    def emit(title: str, frame: pd.DataFrame, name: str) -> None:
        write_parquet(frame, OUT / f"{name}.parquet")
        md.append(f"\n### {title}\n\n" + to_markdown(frame) + "\n")

    keys = ["database", "scope", "point_id", "cut", "variant"]
    joined = picks.merge(outcomes, on=keys, how="inner")
    sel_counts = picks.groupby(["method", "variant"]).size().rename("picked").reset_index()
    sel_counts = sel_counts.merge(joined.groupby(["method", "variant", "forward_days"]).size().rename("with_outcome").reset_index(),
                                  on=["method", "variant"], how="left")
    emit("Число отобранных точек и с исходом", sel_counts, "selection_counts")

    # pooled shares per method (M0 pooled over its repetitions)
    pooled = share_table(joined, ["variant", "forward_days", "method"])
    per_scope = picks.loc[picks["method"].ne("M0")].groupby(["method", "variant", "database", "scope", "cut"]).size()
    pooled = pooled.merge(per_scope.groupby(["method", "variant"]).mean().rename("mean_picked_per_scope_cut").reset_index(),
                          on=["method", "variant"], how="left")
    emit("Доли исходов по методам (бутстрэп по символам)", pooled, "method_shares")

    # paired comparisons within scope-cut
    def scope_share(method: str, metric: str, variant: str, f: int, flag_filter=None) -> pd.Series:
        j = joined.loc[joined["method"].eq(method) & joined["variant"].eq(variant) & joined["forward_days"].eq(f)]
        if flag_filter is not None:
            j = j.loc[flag_filter(j)]
        if method == "M0":
            return j.groupby(["symbol", "database", "scope", "cut", "rep"])[metric].mean().groupby(level=[0, 1, 2, 3]).mean()
        return j.groupby(["symbol", "database", "scope", "cut"])[metric].mean()

    def paired(a: str, b: str, metric: str, variant: str, f: int, flag_filter=None) -> dict:
        sa, sb = scope_share(a, metric, variant, f, flag_filter), scope_share(b, metric, variant, f, flag_filter)
        both = pd.concat([sa.rename("a"), sb.rename("b")], axis=1).dropna()
        per_symbol = (both["a"] - both["b"]).groupby(level=0).mean()
        res = boot_mean_with_p(per_symbol)
        res.update(n_scope_cuts=len(both), share_a=both["a"].mean() if len(both) else np.nan, share_b=both["b"].mean() if len(both) else np.nan)
        return res

    comparisons = [("M1", "M0"), ("M2", "M2i"), ("M4min", "M0"), ("M4q", "M0"), ("M4p", "M0"), ("M5", "M5prod")]
    rows = []
    for a, b in comparisons:
        rows.append({"comparison": f"{a} vs {b}", **paired(a, b, "success_10", "exp", 28)})
    primary = pd.DataFrame(rows)
    primary["p_holm"] = holm(primary.set_index("comparison")["p_boot"]).to_numpy()
    emit("ОСНОВНАЯ КОНЕЧНАЯ ТОЧКА: разница доли Success_10, F=28, S=начало READY (Холм)", primary, "primary_endpoint")

    rows = []
    for a, b in comparisons:
        rows.append({"comparison": f"{a} vs {b}", "subset": "M1 непуст",
                     **paired(a, b, "success_10", "exp", 28, lambda j: j["flag"].ne("M1_EMPTY_K4"))})
    emit("Основная конечная точка только по скоупам с непустым M1", pd.DataFrame(rows), "primary_m1_nonempty")

    sec = []
    all_methods = ["M1", "M1s", "M2", "M2i", "M3", "M3e", "M4min", "M4q", "M4p"]
    for variant in ("exp", "roll"):
        for f in (28, 14):
            for metric in ("success_5", "success_10", "success_15", "bad", "breach", "breach_F"):
                for m in all_methods:
                    sec.append({"variant": variant, "F": f, "metric": metric, "comparison": f"{m} vs M0", **paired(m, "M0", metric, variant, f)})
                sec.append({"variant": variant, "F": f, "metric": metric, "comparison": "M2 vs M2i", **paired("M2", "M2i", metric, variant, f)})
                sec.append({"variant": variant, "F": f, "metric": metric, "comparison": "M5 vs M5prod", **paired("M5", "M5prod", metric, variant, f)})
                sec.append({"variant": variant, "F": f, "metric": metric, "comparison": "M5c vs M5c_prod", **paired("M5c", "M5c_prod", metric, variant, f)})
    emit("Вторичные парные сравнения (без претензии на значимость)", pd.DataFrame(sec), "secondary_paired")

    # per T (calendar) for the primary endpoint
    rows = []
    for t_date in sorted(joined["t_date"].unique()):
        for a, b in comparisons:
            res = paired(a, b, "success_10", "exp", 28, lambda j, t=t_date: j["t_date"].eq(t))
            rows.append({"t_date": t_date, "comparison": f"{a} vs {b}",
                         **{k: res.get(k) for k in ("n_symbols", "n_scope_cuts", "mean_diff", "share_symbols_better", "share_a", "share_b")}})
    emit("Основная конечная точка по каждому T", pd.DataFrame(rows), "primary_by_t")

    # by symbol half (discovery / confirmation split)
    rows = []
    for half in ("A", "B"):
        for a, b in comparisons:
            res = paired(a, b, "success_10", "exp", 28, lambda j, h=half: j["half"].eq(h))
            rows.append({"half": half, "comparison": f"{a} vs {b}", **res})
    emit("Основная конечная точка по половинам символов (хэш символа)", pd.DataFrame(rows), "primary_by_half")

    # ---- correlations (H2/H5): within scope-cut, among economic-pass points
    econ = outcomes.loc[outcomes["economic_pass"]]
    features = ["pnl_pct", "efficiency", "dd_pct", "win_rate_pct", "trades", "plateau_size", "S_min", "S_q25", "S_pool", "shift_bp"]
    corr_rows = []
    for (variant, f), g0 in econ.groupby(["variant", "forward_days"]):
        for bucket in ["all"] + sorted(g0["shift_bucket"].unique()):
            g1 = g0 if bucket == "all" else g0.loc[g0["shift_bucket"].eq(bucket)]
            per = []
            for (sym, db, sc, cut), g in g1.groupby(["symbol", "database", "scope", "cut"]):
                if len(g) < 8:
                    continue
                ranks = g[features + ["R", "breach"]].astype(float).rank()
                c = ranks.corr()
                per.append({"symbol": sym, **{f"{x}|R": c.loc[x, "R"] for x in features}, **{f"{x}|breach": c.loc[x, "breach"] for x in features}})
            per = pd.DataFrame(per)
            if per.empty:
                continue
            for x in features:
                for target in ("R", "breach"):
                    col = f"{x}|{target}"
                    by_sym = per.groupby("symbol")[col].apply(list)
                    vals = per[["symbol", col]].dropna()
                    if vals.empty:
                        continue
                    sym_frame = vals.groupby("symbol")[col].apply(np.array).to_frame("v")
                    est, lo, hi = boot_symbols(sym_frame, lambda s: float(np.median(np.concatenate(s["v"].to_list()))), n=500)
                    corr_rows.append({"variant": variant, "F": f, "shift_bucket": bucket, "feature": x, "target": target,
                                      "n_scope_cuts": int(vals.shape[0]), "median_rho": est, "ci_lo": lo, "ci_hi": hi})
                    del by_sym
    emit("Спирмен внутри скоупа-разреза (медиана, ДИ по символам)", pd.DataFrame(corr_rows), "correlations")

    # ---- H4: S_q25 quartile within scope-cut x shift bucket
    e = econ.loc[econ["variant"].eq("exp") & econ["S_q25"].notna()].copy()
    e["sq_quartile"] = e.groupby(["database", "scope", "cut", "forward_days"])["S_q25"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 4, labels=["Q1", "Q2", "Q3", "Q4"]) if len(s) >= 4 else pd.Series(["Q?"] * len(s), index=s.index))
    emit("H4: Success/Bad по квартилю S_q25 (внутри скоупа-разреза) и корзине шифта", share_table(e, ["forward_days", "sq_quartile", "shift_bucket"]), "h4_quartile_shift")
    # how raw PnL ranks vs success, per shift bucket
    e["pnl_rank_pct"] = e.groupby(["database", "scope", "cut", "forward_days"])["pnl_pct"].rank(pct=True)
    e["sq_rank_pct"] = e.groupby(["database", "scope", "cut", "forward_days"])["S_q25"].rank(pct=True)
    h4b = e.groupby(["forward_days", "shift_bucket"]).agg(n=("R", "size"), mean_pnl_rank=("pnl_rank_pct", "mean"),
                                                         mean_sq25_rank=("sq_rank_pct", "mean"), success_10=("success_10", "mean"),
                                                         bad=("bad", "mean"), no_trades=("no_trades", "mean")).reset_index()
    emit("H4: средний ранг по PnL и по S_q25 против исхода, по корзинам шифта", h4b, "h4_rank_vs_outcome")
    shifts = joined.loc[joined["method"].isin(["M3", "M4q", "M4min", "M1"]) & joined["variant"].eq("exp") & joined["forward_days"].eq(28)]
    emit("H4: распределение отобранных точек по корзинам шифта (F=28)",
         shifts.groupby(["method", "shift_bucket"]).agg(n=("R", "size"), success_10=("success_10", "mean"), bad=("bad", "mean")).reset_index(),
         "h4_method_shift")

    # ---- facts about outcomes themselves
    facts = outcomes.loc[outcomes["variant"].eq("exp")].groupby(["forward_days", "shift_bucket"]).agg(
        n=("R", "size"), no_trades=("no_trades", "mean"), crossing_share=("crossing_trades", lambda s: (s > 0).mean()),
        open_at_end_share=("open_positions_at_end", lambda s: (s > 0).mean()),
        negative_upnl_at_end_share=("upnl_at_end", lambda s: (s < 0).mean()),
        success_10_samples=("success_10_samples", "mean"), success_10=("success_10", "mean")).reset_index()
    emit("Факты прямых окон по корзинам шифта (все точки, S=начало READY)", facts, "forward_facts")
    zero_dd = train.loc[train["variant"].eq("exp")].assign(undefined=lambda d: d["dd_pct"].le(0)).groupby("shift_bucket").agg(
        points=("undefined", "size"), dd_zero=("undefined", "sum"), econ=("economic_pass", "sum")).reset_index()
    emit("Точки с DD = 0 на обучении (оценки окрестности не определены)", zero_dd, "dd_zero")

    (OUT / "tables.md").write_text("\n".join(md), encoding="utf-8")


if __name__ == "__main__":
    main()
