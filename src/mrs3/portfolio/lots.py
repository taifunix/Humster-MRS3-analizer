"""Подбор лотов — раздел 5 спецификации.

Две ступени:

* **форма** — относительные веса между стратегиями (§5.1, надстройка, по
  умолчанию все веса равны);
* **масштаб** — общий множитель ``g``, приводящий просадку сета к целевой (§5.2).

``g`` не ищется бинарным поиском. Просадка по нему **не монотонна**: больший
``g`` занимает больше маржи, из-за чего часть сигналов отсекается и просадка
может упасть. Плюс упор в потолок ёмкости делает часть диапазона плоской.
Поэтому используется сканирование сетки с последующим уточнением.
"""

from __future__ import annotations

from datetime import datetime
from itertools import product
from math import gcd
from typing import Sequence

from .models import RunConfig, SetResult, StrategyInput
from .simulator import simulate_set

__all__ = ["weight_shapes", "fit_lots", "LotFitResult"]


def weight_shapes(size: int, levels: Sequence[int]) -> list[tuple[int, ...]]:
    """Уникальные формы весов. Векторы, кратные друг другу, считаются одним."""
    if size <= 0:
        raise ValueError("size must be > 0")
    unique: dict[tuple[int, ...], tuple[int, ...]] = {}
    for combo in product(sorted(set(levels)), repeat=size):
        divisor = 0
        for value in combo:
            divisor = gcd(divisor, value)
        canonical = tuple(v // divisor for v in combo) if divisor else combo
        unique.setdefault(canonical, canonical)
    return sorted(unique.values())


class LotFitResult:
    """Результат подбора масштаба вместе с диагностикой."""

    __slots__ = ("result", "g", "weights", "status", "scan")

    def __init__(
        self,
        result: SetResult | None,
        g: float,
        weights: tuple[int, ...],
        status: str,
        scan: list[tuple[float, float]],
    ) -> None:
        self.result = result
        self.g = g
        self.weights = weights
        self.status = status
        self.scan = scan


def _lots_for(
    members: Sequence[StrategyInput], weights: Sequence[int], g: float
) -> dict[str, float]:
    return {
        m.strategy_id: w * m.lot_x_base * g
        for m, w in zip(members, weights, strict=True)
    }


def fit_lots(
    members: Sequence[StrategyInput],
    limiter: int,
    weights: Sequence[int],
    cfg: RunConfig,
    window: tuple[datetime, datetime] | None = None,
) -> LotFitResult:
    """Найти наибольший ``g``, при котором просадка не превышает целевую."""
    ordered = sorted(members, key=lambda m: m.strategy_id)
    weights = tuple(weights)
    if len(weights) != len(ordered):
        raise ValueError("weights length must match set size")

    step = (cfg.g_grid_hi / cfg.g_grid_lo) ** (1.0 / max(cfg.g_grid_points - 1, 1))
    grid = [cfg.g_grid_lo * step**i for i in range(cfg.g_grid_points)]

    scan: list[tuple[float, float]] = []
    results: dict[float, SetResult] = {}
    for g in grid:
        res = simulate_set(ordered, limiter, _lots_for(ordered, weights, g), cfg, window)
        scan.append((g, res.max_dd_pct))
        results[g] = res

    fitting = [g for g, dd in scan if dd <= cfg.dd_target_pct]
    if not fitting:
        best = min(scan, key=lambda item: item[1])
        return LotFitResult(results[best[0]], best[0], weights, "DD_TARGET_TOO_LOW", scan)

    best_g = max(fitting)
    if best_g == grid[-1]:
        return LotFitResult(
            results[best_g], best_g, weights, "DD_TARGET_UNREACHABLE", scan
        )

    upper = min(g for g in grid if g > best_g)
    for i in range(1, cfg.g_refine_points + 1):
        candidate = best_g + (upper - best_g) * i / (cfg.g_refine_points + 1)
        res = simulate_set(
            ordered, limiter, _lots_for(ordered, weights, candidate), cfg, window
        )
        scan.append((candidate, res.max_dd_pct))
        if res.max_dd_pct <= cfg.dd_target_pct and candidate > best_g:
            best_g, results[candidate] = candidate, res

    scan.sort(key=lambda item: item[0])
    return LotFitResult(results[best_g], best_g, weights, "OK", scan)
