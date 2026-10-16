"""Step 5 (§5.8, H6): approximate MRS3 structures as sums of single-point curves.

Approximation: equal fixed lots, common CloseMA -> the structure's balance is
the sum of its orders' balances and its equity the sum of their equities
(each order's last known equity carried forward to every timestamp of the
union). Real MRS3 may differ (order interaction, re-entries, INCOME lots).

Compared, per scope x cut (S = READY start; F = 28 and 14):
* PROD  - current `structures` from the training analysis;
* M4S   - structures from neighbourhood-score points, pre-registered rule:
          pool = economic-pass points with defined S_min; per CloseMA, greedy by
          S_min desc; add a point only if, together with the chosen ones,
          shifts are strictly increasing, every consecutive gap >= required_gap_bp
          (selection.py) and 3x3x3 neighbourhoods are pairwise disjoint
          (|shift index diff| >= 3 or |OpenMA diff| >= 3); at most max_orders;
          kept when >= 2 orders. Not `validate_order_tuple`: outside plateaus
          it would demand plateau ids and eligibility flags that are false there.

Run: set PYTHONPATH=src & .venv\\Scripts\\python.exe scripts\\plateau_research\\stage2.py [--workers 15]
Output: WORK/analysis/stage2_structures.parquet, stage2_summary.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DAY_MS, SOURCE_ROOT, WORK, load_config, read_parquet, write_parquet  # noqa: E402
from measure import Series, _open_tail_cutoff, cut_grid, scope_dir  # noqa: E402

SHIFTS = [30, 40, 50, 60, 70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550]
SHIFT_INDEX = {s: i for i, s in enumerate(SHIFTS)}


def m4_structures(econ: pd.DataFrame, config) -> list[list[str]]:
    from mrs3.selection import required_gap_bp

    result = []
    pool = econ.loc[econ["S_min"].notna()]
    for close_ma, g in pool.groupby("close_ma"):
        chosen: list[pd.Series] = []
        for _, p in g.sort_values(["S_min", "point_id"], ascending=[False, True]).iterrows():
            if len(chosen) >= config.max_orders:
                break
            trial = sorted(chosen + [p], key=lambda x: int(x["shift_bp"]))
            shifts = [int(x["shift_bp"]) for x in trial]
            if any(b <= a for a, b in zip(shifts, shifts[1:])):
                continue
            if any(b - a < required_gap_bp(a, config) for a, b in zip(shifts, shifts[1:])):
                continue
            if any(abs(SHIFT_INDEX[int(p["shift_bp"])] - SHIFT_INDEX[int(c["shift_bp"])]) < 3
                   and abs(int(p["open_ma"]) - int(c["open_ma"])) < 3 for c in chosen):
                continue
            chosen.append(p)
        if len(chosen) >= 2:
            result.append([str(x["point_id"]) for x in sorted(chosen, key=lambda x: int(x["shift_bp"]))])
    return result


def combined(series: list[Series], a: int, b: int) -> tuple[float, float, float, float]:
    """Sum curve on [a, b): (balance anchor, balance end, dd abs, dd %)."""
    bal_a = sum(s.balance_before(a) for s in series)
    bal_b = sum(s.balance_before(b) for s in series)
    stamps = np.unique(np.concatenate([s.mt[(s.mt >= a) & (s.mt < b)] for s in series] + [np.array([], dtype=np.int64)]))
    anchor = sum(s.equity_before(a) for s in series)
    path = [anchor]
    for t in stamps:
        path.append(sum(s.equity_before(int(t) + 1) for s in series))
    seg = np.array(path)
    peak = np.maximum.accumulate(seg)
    dd = peak - seg
    j = int(np.argmax(dd))
    return bal_a, bal_b, float(dd[j]), (float(dd[j] / peak[j] * 100) if peak[j] else 0.0)


def run_scope(database: str, scope: str, ready: tuple[int, int], structures: dict, covered: dict, config) -> list[dict]:
    from mrs3.source_v6_materializer import _point_groups
    from mrs3.source_v6_stitch import calculate_metrics
    from mrs3.source_v6_storage import decode_fragment_slice, fragment_metadata

    needed = sorted({pid for items in structures.values() for _, ids in items for pid in ids})
    if not needed:
        return []
    groups = _point_groups([v for v in fragment_metadata(database) if v.point.canonical_key in set(needed)], [scope])[scope]
    cache = {}
    for pid in needed:
        fragments = decode_fragment_slice(database, groups[pid])
        cutoff = _open_tail_cutoff(fragments[0])
        cache[pid] = Series(calculate_metrics(fragments), fragments[0], cutoff)
    rows = []
    cuts = {c["cut"]: c for c in cut_grid(*ready)}
    for cut_name, items in structures.items():
        cut = cuts[cut_name]
        s_ms, t_ms = cut["train_exp"]
        for kind, ids in items:
            series = [cache[p] for p in ids]
            _, _, _, dd_train = combined(series, s_ms, t_ms)
            for f in cut["forwards"]:
                if not all(covered.get((p, cut_name, f), False) for p in ids):
                    rows.append({"database": Path(database).name, "scope": scope, "cut": cut_name, "kind": kind,
                                 "orders": json.dumps(ids), "forward_days": f, "covered": False})
                    continue
                e_ms = t_ms + f * DAY_MS
                bal_a, bal_b, _, dd_fwd = combined(series, t_ms, e_ms)
                pnl = (bal_b - bal_a) / bal_a * 100
                r = pnl * (30 / f) * 5 / max(dd_train, 1.0)
                breach = dd_fwd > dd_train
                rows.append({"database": Path(database).name, "scope": scope, "cut": cut_name, "kind": kind,
                             "orders": json.dumps(ids), "order_count": len(ids), "forward_days": f, "covered": True,
                             "dd_train_pct": dd_train, "pnl_fwd_pct": pnl, "dd_fwd_pct": dd_fwd, "R": r, "breach": breach,
                             "success_5": r >= 5 and not breach, "success_10": r >= 10 and not breach,
                             "success_15": r >= 15 and not breach, "bad": pnl <= 0 or breach})
    return rows


def summarize() -> None:
    """H6 tables: shares per kind with symbol-bootstrap CIs; paired PROD vs M4S
    inside scope x cut (mean over a scope-cut's structures, then per symbol)."""
    from analyze import boot_mean_with_p, boot_symbols, to_markdown

    frame = read_parquet(WORK / "analysis" / "stage2_structures.parquet")
    cov = frame.loc[frame["covered"]].copy()
    metrics = ["success_5", "success_10", "success_15", "bad", "breach"]
    for m in metrics + ["breach"]:
        cov[m] = cov[m].astype(float)
    rows = []
    for (f, kind), g in cov.groupby(["forward_days", "kind"]):
        sums = g.groupby("symbol")[metrics].sum()
        sums["n"] = g.groupby("symbol").size()
        row = {"F": f, "kind": kind, "structures": len(g), "symbols": len(sums),
               "scope_cuts": g[["scope", "cut"]].drop_duplicates().shape[0],
               "orders_mean": g["order_count"].mean(), "median_R": g["R"].median(),
               "median_dd_train": g["dd_train_pct"].median(), "median_dd_fwd": g["dd_fwd_pct"].median()}
        for m in metrics:
            est, lo, hi = boot_symbols(sums, lambda s, m=m: s[m].sum() / s["n"].sum())
            row[m], row[f"{m}_lo"], row[f"{m}_hi"] = est, lo, hi
        rows.append(row)
    shares = pd.DataFrame(rows)
    paired = []
    for f, g in cov.groupby("forward_days"):
        per = g.groupby(["symbol", "scope", "cut", "kind"])[metrics].mean().unstack("kind")
        for m in metrics:
            both = per[m].dropna(subset=["PROD", "M4S"]) if {"PROD", "M4S"} <= set(per[m].columns) else pd.DataFrame()
            if both.empty:
                continue
            per_symbol = (both["M4S"] - both["PROD"]).groupby(level=0).mean()
            paired.append({"F": f, "metric": m, "scope_cuts": len(both), **boot_mean_with_p(per_symbol)})
    paired = pd.DataFrame(paired)
    write_parquet(shares, WORK / "analysis" / "stage2_summary.parquet")
    write_parquet(paired, WORK / "analysis" / "stage2_paired.parquet")
    uncovered = frame.loc[~frame["covered"]].groupby(["forward_days", "kind"]).size().rename("uncovered").reset_index()
    text = ("\n### Этап 2: доли по видам структур\n\n" + to_markdown(shares) +
            "\n\n### Этап 2: M4S минус PROD внутри скоупа-разреза\n\n" + to_markdown(paired) +
            "\n\n### Этап 2: непокрытые\n\n" + to_markdown(uncovered) + "\n")
    (WORK / "analysis" / "stage2_tables.md").write_text(text, encoding="utf-8")
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=15)
    parser.add_argument("--stats-only", action="store_true")
    args = parser.parse_args()
    if args.stats_only:
        summarize()
        return
    config = load_config()
    plan = read_parquet(WORK / "inventory" / "plan.parquet")
    plan = plan.loc[plan["mode"].eq("study")]
    outcomes = read_parquet(WORK / "analysis" / "outcomes.parquet")
    outcomes = outcomes.loc[outcomes["variant"].eq("exp")]
    fwd_cov = outcomes[["database", "scope", "point_id", "cut", "forward_days"]].drop_duplicates()
    econ = outcomes.loc[outcomes["economic_pass"] & outcomes["forward_days"].eq(28)]
    tasks = []
    for r in plan.itertuples(index=False):
        cuts = cut_grid(int(r.ready_start_ms), int(r.ready_end_ms))
        if not cuts:
            continue
        sdir = scope_dir(str(SOURCE_ROOT / r.database), r.scope)
        prod = read_parquet(sdir / "stages_structures.parquet") if (sdir / "stages_structures.parquet").exists() else pd.DataFrame()
        e_scope = econ.loc[econ["database"].eq(r.database) & econ["scope"].eq(r.scope)]
        structures = {}
        for cut in cuts:
            items = []
            if not prod.empty:
                for orders in prod.loc[prod["wtype"].eq("train_exp") & prod["cut"].eq(cut["cut"]), "orders"]:
                    items.append(("PROD", [o["point_id"] for o in json.loads(orders)]))
            for ids in m4_structures(e_scope.loc[e_scope["cut"].eq(cut["cut"])], config):
                items.append(("M4S", ids))
            if items:
                structures[cut["cut"]] = items
        if not structures:
            continue
        cov = fwd_cov.loc[fwd_cov["database"].eq(r.database) & fwd_cov["scope"].eq(r.scope)]
        covered = {(p, c, int(f)): True for p, c, f in zip(cov["point_id"], cov["cut"], cov["forward_days"])}
        tasks.append((str(SOURCE_ROOT / r.database), r.scope, (int(r.ready_start_ms), int(r.ready_end_ms)), structures, covered, config))
    print("scopes with structures:", len(tasks), flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for i, result in enumerate(pool.map(run_scope, *zip(*tasks)), 1):
            rows.extend(result)
            if i % 25 == 0:
                print(i, len(rows), flush=True)
    frame = pd.DataFrame(rows)
    frame["symbol"] = frame["scope"].str.split("|").str[0]
    write_parquet(frame, WORK / "analysis" / "stage2_structures.parquet")
    summarize()


if __name__ == "__main__":
    main()
