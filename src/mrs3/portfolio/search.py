"""Перебор, Парето и проверка на устойчивость — разделы 6, 8 и 12 спецификации."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import combinations
from typing import Sequence

from .lots import LotFitResult, fit_lots, weight_shapes
from .models import RunConfig, SetResult, StrategyInput
from .simulator import common_window

__all__ = [
    "screen_layer_a",
    "enumerate_sets",
    "pareto_front",
    "split_validation",
    "correlation_matrix",
]


# ---------------------------------------------------------------- слой A


def screen_layer_a(
    members: Sequence[StrategyInput], limiter: int, cfg: RunConfig
) -> tuple[bool, list[str], float, float]:
    """Дешёвый отсев до симуляции. Возвращает (пропущен, причины, load, дни)."""
    ordered = sorted(members, key=lambda m: m.strategy_id)
    total_occupancy = sum(m.occupancy for m in ordered)
    load = total_occupancy / limiter

    start, end = common_window(ordered)
    d_eff = max((end - start).total_seconds() / 86400.0, 0.0)

    reasons: list[str] = []
    if load > cfg.load_reject_above:
        reasons.append("LOAD_TOO_HIGH")
    if d_eff < cfg.d_eff_common_min_days:
        reasons.append("D_EFF_COMMON_TOO_SHORT")

    for a, b in combinations(ordered, 2):
        slow, fast = (a, b) if a.median_hold_min >= b.median_hold_min else (b, a)
        if fast.median_hold_min <= 0:
            continue
        ratio = slow.median_hold_min / fast.median_hold_min
        if ratio > cfg.asymmetry_ratio and slow.occupancy > cfg.asymmetry_slow_occupancy:
            reasons.append("TF_ASYMMETRY")
            break

    return (not reasons), reasons, load, d_eff


# ---------------------------------------------------------------- перебор


def enumerate_sets(
    candidates: Sequence[StrategyInput],
    cfg: RunConfig,
    window: tuple[datetime, datetime] | None = None,
) -> tuple[list[SetResult], list[dict]]:
    """Двухпроходный перебор, §6.2.

    Проход 1 — равные веса по всем составам и всем ограничителям.
    Проход 2 — формы весов только для лучших составов, и только если включена
    надстройка ``weight_levels``.
    """
    ordered = sorted(candidates, key=lambda m: m.strategy_id)
    screened: list[dict] = []
    pass1: list[tuple[SetResult, LotFitResult, int, tuple[str, ...]]] = []

    for limiter in sorted(set(cfg.limiters)):
        upper = min(len(ordered), limiter * cfg.max_size_factor)
        for size in range(min(limiter, len(ordered)), upper + 1):
            for combo in combinations(ordered, size):
                ok, reasons, load, d_eff = screen_layer_a(combo, limiter, cfg)
                ids = tuple(m.strategy_id for m in combo)
                screened.append(
                    {
                        "limiter": limiter,
                        "size": size,
                        "strategy_ids": "|".join(ids),
                        "load": round(load, 6),
                        "d_eff_common_days": round(d_eff, 3),
                        "passed": "yes" if ok else "no",
                        "reject_reasons": "|".join(reasons),
                    }
                )
                if not ok:
                    continue
                fit = fit_lots(combo, limiter, (1,) * size, cfg, window)
                if fit.result is None or fit.status == "DD_TARGET_TOO_LOW":
                    continue
                result = _finalize(fit, limiter, cfg)
                if result is not None:
                    pass1.append((result, fit, limiter, ids))

    results = [item[0] for item in pass1]

    if tuple(cfg.weight_levels) != (1,):
        top = sorted(pass1, key=lambda item: -item[0].efficiency)[: cfg.top_sets_for_weights]
        by_id = {m.strategy_id: m for m in ordered}
        for _, _, limiter, ids in top:
            combo = [by_id[i] for i in ids]
            for shape in weight_shapes(len(combo), cfg.weight_levels):
                if len(set(shape)) == 1:
                    continue  # равные веса уже посчитаны в проходе 1
                fit = fit_lots(combo, limiter, shape, cfg, window)
                if fit.result is None or fit.status == "DD_TARGET_TOO_LOW":
                    continue
                result = _finalize(fit, limiter, cfg)
                if result is not None:
                    results.append(result)

    results.sort(key=lambda r: (-r.efficiency, r.strategy_ids, r.limiter))
    return results, screened


def _finalize(fit: LotFitResult, limiter: int, cfg: RunConfig) -> SetResult | None:
    """Применить гейты §11 и дописать веса, множитель и флаги."""
    base = fit.result
    if base is None:
        return None
    if any(o.acceptance < cfg.acceptance_min for o in base.outcomes):
        return None
    if base.max_occupancy_margin > cfg.margin_limit:
        return None

    flags = list(base.flags)
    if fit.status != "OK":
        flags.append(fit.status)

    return SetResult(
        strategy_ids=base.strategy_ids,
        limiter=limiter,
        weights=fit.weights,
        g=fit.g,
        lots=base.lots,
        pnl_abs=base.pnl_abs,
        pnl_pct=base.pnl_pct,
        pnl30_pct=base.pnl30_pct,
        max_dd_pct=base.max_dd_pct,
        max_margin_ratio=base.max_margin_ratio,
        max_occupancy_margin=base.max_occupancy_margin,
        min_buffer=base.min_buffer,
        d_eff_common_days=base.d_eff_common_days,
        outcomes=base.outcomes,
        flags=tuple(flags),
    )


# ---------------------------------------------------------------- Парето


def pareto_front(results: Sequence[SetResult]) -> list[SetResult]:
    """Максимизируем ``pnl30_pct``, минимизируем требуемый капитал, §11.3."""
    front: list[SetResult] = []
    for candidate in results:
        dominated = False
        for other in results:
            if other is candidate:
                continue
            better_or_equal = (
                other.pnl30_pct >= candidate.pnl30_pct
                and other.capital_requirement <= candidate.capital_requirement
            )
            strictly_better = (
                other.pnl30_pct > candidate.pnl30_pct
                or other.capital_requirement < candidate.capital_requirement
            )
            if better_or_equal and strictly_better:
                dominated = True
                break
        if not dominated:
            front.append(candidate)
    front.sort(key=lambda r: (-r.pnl30_pct, r.capital_requirement))
    return front


# ---------------------------------------------------------------- OOS


def split_validation(
    candidates: Sequence[StrategyInput],
    cfg: RunConfig,
    top_n: int = 10,
) -> dict:
    """Отбор на первой половине, проверка на второй, §12."""
    start, end = common_window(sorted(candidates, key=lambda m: m.strategy_id))
    total_days = (end - start).total_seconds() / 86400.0
    if total_days < cfg.oos_min_days:
        return {"status": "NO_OOS_VALIDATION", "d_eff_common_days": round(total_days, 3)}

    mid = start + timedelta(days=total_days / 2.0)
    in_sample, _ = enumerate_sets(candidates, cfg, window=(start, mid))
    if not in_sample:
        return {"status": "NO_SETS_IN_FIRST_HALF"}

    by_id = {m.strategy_id: m for m in candidates}
    rows: list[dict] = []
    oos_all: list[SetResult] = []
    for rank, item in enumerate(in_sample[:top_n], 1):
        combo = [by_id[i] for i in item.strategy_ids]
        lots = dict(zip(item.strategy_ids, item.lots, strict=True))
        from .simulator import simulate_set  # локальный импорт: избегаем цикла

        oos = simulate_set(combo, item.limiter, lots, cfg, window=(mid, end))
        oos_all.append(oos)
        rows.append(
            {
                "rank_is": rank,
                "strategy_ids": "|".join(item.strategy_ids),
                "limiter": item.limiter,
                "is_pnl30_pct": round(item.pnl30_pct, 3),
                "is_dd_pct": round(item.max_dd_pct, 3),
                "oos_pnl30_pct": round(oos.pnl30_pct, 3),
                "oos_dd_pct": round(oos.max_dd_pct, 3),
                "held_up": "yes" if oos.pnl30_pct > 0 else "no",
            }
        )

    is_values = [item.pnl30_pct for item in in_sample[:top_n]]
    oos_values = [r.pnl30_pct for r in oos_all]
    return {
        "status": "OK",
        "split_at": mid.isoformat(),
        "d_eff_common_days": round(total_days, 3),
        "spearman": round(_spearman(is_values, oos_values), 4),
        "held_up": sum(1 for r in rows if r["held_up"] == "yes"),
        "checked": len(rows),
        "rows": rows,
    }


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for position, index in enumerate(order):
        ranks[index] = float(position)
    return ranks


def _spearman(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 3:
        return 0.0
    ra, rb = _rank(a), _rank(b)
    n = len(ra)
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    den_a = sum((x - mean_a) ** 2 for x in ra) ** 0.5
    den_b = sum((y - mean_b) ** 2 for y in rb) ** 0.5
    return num / (den_a * den_b) if den_a and den_b else 0.0


# ---------------------------------------------------------------- корреляция


def correlation_matrix(members: Sequence[StrategyInput]) -> dict:
    """Корреляция дневных результатов, §9.4."""
    ordered = sorted(members, key=lambda m: m.strategy_id)
    daily: dict[str, dict[str, float]] = {}
    for m in ordered:
        bucket: dict[str, float] = {}
        for trade in m.trades:
            key = trade.exit_ts.date().isoformat()
            bucket[key] = bucket.get(key, 0.0) + trade.net_frac
        daily[m.strategy_id] = bucket

    days = sorted({d for bucket in daily.values() for d in bucket})
    pairs: list[dict] = []
    values: list[float] = []
    for a, b in combinations(ordered, 2):
        xs = [daily[a.strategy_id].get(d, 0.0) for d in days]
        ys = [daily[b.strategy_id].get(d, 0.0) for d in days]
        corr = _pearson(xs, ys)
        pairs.append({"a": a.strategy_id, "b": b.strategy_id, "corr": round(corr, 4)})
        values.append(corr)

    worst_day = 0.0
    for d in days:
        total = sum(bucket.get(d, 0.0) for bucket in daily.values())
        worst_day = min(worst_day, total)

    return {
        "pairs": pairs,
        "mean_abs_corr": round(sum(abs(v) for v in values) / len(values), 4) if values else 0.0,
        "worst_day_frac": round(worst_day, 6),
    }


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    mean_a, mean_b = sum(a) / n, sum(b) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    den_a = sum((x - mean_a) ** 2 for x in a) ** 0.5
    den_b = sum((y - mean_b) ** 2 for y in b) ** 0.5
    return num / (den_a * den_b) if den_a and den_b else 0.0
