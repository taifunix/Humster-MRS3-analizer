"""Layer A of the Portfolio Analyzer.

Реализует раздел 4 спецификации `docs/specs/2026-08-09-portfolio-analyzer-v04.md`:
занятость слотов, ёмкость и отсев комбинаций до симуляции.

Слой A намеренно НЕ содержит симуляции сета, подбора множителя лотов и
рекомендаций — они требуют trade timestamps, limiter contract, L2 и margin data
(см. hook «Анализатор Портфеля» в `CLAUDE.md`).

Все вычисления детерминированы: порядок кандидатов и комбинаций задаётся
лексикографически по ``strategy_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Iterable, Iterator, Sequence

MINUTES_PER_DAY = 1440.0

# --- пороги раздела 4 спецификации; вынесены сюда, а не зашиты в код --------
LOAD_IDLE_BELOW = 0.5
LOAD_WORKING_MAX = 1.5
LOAD_REJECT_ABOVE = 2.5
ASYMMETRY_RATIO = 20.0
ASYMMETRY_SLOW_OCCUPANCY = 0.3
MIN_D_EFF_COMMON_DAYS = 30.0


class LayerAError(ValueError):
    """Нарушен контракт входных данных."""


# ---------------------------------------------------------------- вход


@dataclass(frozen=True)
class Candidate:
    """Одна готовая MRS3-стратегия, приведённая к целевой просадке.

    ``lot_x_base`` — лот, при котором стратегия В ОДИНОЧКУ даёт ``dd_pct``.
    Слой A его не пересчитывает (§5 спецификации).
    """

    strategy_id: str
    pair: str
    side: str
    timeframe: str
    trades: int
    median_hold_min: float
    lot_x_base: float
    pnl_pct: float
    dd_pct: float
    window_start: datetime
    window_end: datetime
    turnover_24h: float
    target_share: float
    target_share_source: str = "ESTIMATED"

    def __post_init__(self) -> None:
        if self.side not in ("LONG", "SHORT"):
            raise LayerAError(f"{self.strategy_id}: side must be LONG or SHORT")
        if self.trades <= 0:
            raise LayerAError(f"{self.strategy_id}: trades must be > 0")
        if self.median_hold_min <= 0:
            raise LayerAError(f"{self.strategy_id}: median_hold_min must be > 0")
        if self.window_end <= self.window_start:
            raise LayerAError(f"{self.strategy_id}: window_end must be after window_start")
        if not 0 < self.target_share <= 1:
            raise LayerAError(f"{self.strategy_id}: target_share must be in (0, 1]")
        if self.target_share_source not in ("L2", "ESTIMATED"):
            raise LayerAError(f"{self.strategy_id}: target_share_source must be L2 or ESTIMATED")

    # --- производные величины, §4.1 и §4.2 ---

    @property
    def d_eff_days(self) -> float:
        return (self.window_end - self.window_start).total_seconds() / 86400.0

    @property
    def trades_per_day(self) -> float:
        return self.trades / self.d_eff_days

    @property
    def turns_per_day(self) -> float:
        """Оборот номинала в сутки: каждый цикл это вход плюс выход."""
        return self.trades_per_day * 2.0

    @property
    def capacity(self) -> float:
        """Предельный номинал позиции, §4.1."""
        return self.turnover_24h * self.target_share / self.turns_per_day

    @property
    def occupancy(self) -> float:
        """Доля времени, в течение которой стратегия держит слот занятым, §4.2."""
        return self.trades * self.median_hold_min / (self.d_eff_days * MINUTES_PER_DAY)


# ---------------------------------------------------------------- выход


@dataclass(frozen=True)
class AsymmetryPair:
    slow_id: str
    fast_id: str
    ratio: float
    slow_occupancy: float


@dataclass(frozen=True)
class CombinationScreen:
    strategy_ids: tuple[str, ...]
    limiter: int
    load: float
    total_occupancy: float
    d_eff_common_days: float
    load_status: str
    asymmetry_pairs: tuple[AsymmetryPair, ...]
    reject_reasons: tuple[str, ...] = field(default=())

    @property
    def accepted(self) -> bool:
        return not self.reject_reasons


# ---------------------------------------------------------------- расчёт


def _load_status(load: float) -> str:
    if load < LOAD_IDLE_BELOW:
        return "IDLE"
    if load <= LOAD_WORKING_MAX:
        return "WORKING"
    if load <= LOAD_REJECT_ABOVE:
        return "HEAVY"
    return "OVERLOADED"


def common_window_days(members: Sequence[Candidate]) -> float:
    """Пересечение окон истории, §7.1. Ноль, если окна не пересекаются."""
    start = max(c.window_start for c in members)
    end = min(c.window_end for c in members)
    if end <= start:
        return 0.0
    return (end - start).total_seconds() / 86400.0


def find_asymmetry(members: Sequence[Candidate]) -> tuple[AsymmetryPair, ...]:
    """Пары «медленная душит быструю», §4.3.

    Условие срабатывает, только если медленная стратегия и заметно медленнее,
    и сама держит слот значимую долю времени.
    """
    found: list[AsymmetryPair] = []
    for slow, fast in combinations(sorted(members, key=lambda c: c.strategy_id), 2):
        a, b = slow, fast
        if a.median_hold_min < b.median_hold_min:
            a, b = b, a
        ratio = a.median_hold_min / b.median_hold_min
        if ratio > ASYMMETRY_RATIO and a.occupancy > ASYMMETRY_SLOW_OCCUPANCY:
            found.append(
                AsymmetryPair(
                    slow_id=a.strategy_id,
                    fast_id=b.strategy_id,
                    ratio=ratio,
                    slow_occupancy=a.occupancy,
                )
            )
    return tuple(sorted(found, key=lambda p: (p.slow_id, p.fast_id)))


def screen_combination(members: Sequence[Candidate], limiter: int) -> CombinationScreen:
    if limiter <= 0:
        raise LayerAError("limiter must be > 0")
    if not members:
        raise LayerAError("combination must not be empty")

    ordered = tuple(sorted(members, key=lambda c: c.strategy_id))
    total_occupancy = sum(c.occupancy for c in ordered)
    load = total_occupancy / limiter
    status = _load_status(load)
    asymmetry = find_asymmetry(ordered)
    d_eff_common = common_window_days(ordered)

    reasons: list[str] = []
    if status == "OVERLOADED":
        reasons.append(f"LOAD_ABOVE_{LOAD_REJECT_ABOVE}")
    if asymmetry:
        reasons.append("TF_ASYMMETRY")
    if d_eff_common < MIN_D_EFF_COMMON_DAYS:
        reasons.append("D_EFF_COMMON_TOO_SHORT")

    return CombinationScreen(
        strategy_ids=tuple(c.strategy_id for c in ordered),
        limiter=limiter,
        load=load,
        total_occupancy=total_occupancy,
        d_eff_common_days=d_eff_common,
        load_status=status,
        asymmetry_pairs=asymmetry,
        reject_reasons=tuple(reasons),
    )


def iter_combinations(
    candidates: Sequence[Candidate],
    limiter: int,
    max_size_factor: int = 3,
) -> Iterator[tuple[Candidate, ...]]:
    """Составы размера от ``limiter`` до ``limiter * max_size_factor``, §6.1.

    Размер сета может превышать лимит: лимит ограничивает одновременные
    позиции, а не число стратегий.
    """
    ordered = sorted(candidates, key=lambda c: c.strategy_id)
    upper = min(len(ordered), limiter * max_size_factor)
    for size in range(limiter, upper + 1):
        yield from combinations(ordered, size)


def screen_all(
    candidates: Sequence[Candidate],
    limiters: Iterable[int] = (2, 3, 4),
    max_size_factor: int = 3,
) -> list[CombinationScreen]:
    """Полный отсев слоя A по всем составам и всем значениям ограничителя."""
    results: list[CombinationScreen] = []
    for limiter in sorted(set(limiters)):
        for combo in iter_combinations(candidates, limiter, max_size_factor):
            results.append(screen_combination(combo, limiter))
    results.sort(key=lambda r: (r.limiter, len(r.strategy_ids), r.strategy_ids))
    return results


# ---------------------------------------------------------------- отчёты


def candidates_report(candidates: Sequence[Candidate]) -> list[dict]:
    """Лист ``02_Candidates`` из §14 спецификации."""
    rows = []
    for c in sorted(candidates, key=lambda x: x.strategy_id):
        rows.append(
            {
                "strategy_id": c.strategy_id,
                "pair": c.pair,
                "side": c.side,
                "timeframe": c.timeframe,
                "trades": c.trades,
                "d_eff_days": round(c.d_eff_days, 3),
                "trades_per_day": round(c.trades_per_day, 4),
                "median_hold_min": c.median_hold_min,
                "occupancy": round(c.occupancy, 6),
                "lot_x_base": c.lot_x_base,
                "pnl_pct": c.pnl_pct,
                "dd_pct": c.dd_pct,
                "turnover_24h": c.turnover_24h,
                "target_share": c.target_share,
                "target_share_source": c.target_share_source,
                "capacity": round(c.capacity, 2),
            }
        )
    return rows


def screen_report(screens: Sequence[CombinationScreen]) -> list[dict]:
    """Лист ``03_Layer_A_Screen`` из §14 спецификации."""
    rows = []
    for s in screens:
        rows.append(
            {
                "limiter": s.limiter,
                "size": len(s.strategy_ids),
                "strategy_ids": "|".join(s.strategy_ids),
                "total_occupancy": round(s.total_occupancy, 6),
                "load": round(s.load, 6),
                "load_status": s.load_status,
                "d_eff_common_days": round(s.d_eff_common_days, 3),
                "asymmetry": ";".join(
                    f"{p.slow_id}>{p.fast_id}x{p.ratio:.1f}" for p in s.asymmetry_pairs
                ),
                "accepted": "yes" if s.accepted else "no",
                "reject_reasons": "|".join(s.reject_reasons),
            }
        )
    return rows
