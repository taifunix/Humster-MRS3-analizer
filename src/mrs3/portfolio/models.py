"""Модель данных анализатора портфеля.

Спецификация: `docs/specs/2026-08-09-portfolio-analyzer-v04.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "TradeRecord",
    "StrategyInput",
    "RunConfig",
    "SetResult",
    "StrategyOutcome",
]


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Один закрытый цикл из журнала стратегии.

    ``pnl_frac`` — результат в долях от номинала позиции. Именно он позволяет
    пересчитать сделку под другой размер лота: реальный PnL в симуляции равен
    ``pnl_frac * notional``.

    ``mae_frac`` — максимальный уход в минус внутри цикла, в долях номинала.
    Если журнал его не содержит, плавающая просадка не считается и результат
    помечается ``CLOSED_TRADE_DD_ONLY``.
    """

    strategy_id: str
    entry_ts: datetime
    exit_ts: datetime
    pnl_frac: float
    fee_frac: float = 0.0
    mae_frac: float | None = None

    @property
    def hold_minutes(self) -> float:
        return (self.exit_ts - self.entry_ts).total_seconds() / 60.0

    @property
    def net_frac(self) -> float:
        return self.pnl_frac - self.fee_frac


@dataclass(frozen=True, slots=True)
class StrategyInput:
    """Кандидат со всем, что нужно для симуляции.

    ``lot_x_base`` приходит из селектора и не пересчитывается: это лот, при
    котором стратегия в одиночку даёт ``dd_pct``.
    """

    strategy_id: str
    pair: str
    side: str
    timeframe: str
    lot_x_base: float
    pnl_pct: float
    dd_pct: float
    turnover_24h: float
    target_share: float = 0.115
    target_share_source: str = "ESTIMATED"
    mmr: float = 0.02
    imr: float = 0.05
    orders: int = 1
    trades: list[TradeRecord] = field(default_factory=list, compare=False)

    def __post_init__(self) -> None:
        if self.side not in ("LONG", "SHORT"):
            raise ValueError(f"{self.strategy_id}: side must be LONG or SHORT")
        if self.lot_x_base <= 0:
            raise ValueError(f"{self.strategy_id}: lot_x_base must be > 0")
        if not 0 < self.target_share <= 1:
            raise ValueError(f"{self.strategy_id}: target_share must be in (0, 1]")
        if not 0 < self.mmr < self.imr <= 1:
            raise ValueError(f"{self.strategy_id}: expected 0 < mmr < imr <= 1")

    @property
    def window_start(self) -> datetime:
        return min(t.entry_ts for t in self.trades)

    @property
    def window_end(self) -> datetime:
        return max(t.exit_ts for t in self.trades)

    @property
    def d_eff_days(self) -> float:
        """Длина истории. Никогда не ноль: все производные на неё делят."""
        days = (self.window_end - self.window_start).total_seconds() / 86400.0
        return max(days, 1.0 / 1440.0)  # пол в одну минуту

    @property
    def median_hold_min(self) -> float:
        holds = sorted(t.hold_minutes for t in self.trades)
        n = len(holds)
        mid = n // 2
        return holds[mid] if n % 2 else (holds[mid - 1] + holds[mid]) / 2.0

    @property
    def trades_per_day(self) -> float:
        return len(self.trades) / self.d_eff_days

    @property
    def capacity(self) -> float:
        """Предельный номинал позиции, §4.1 спецификации."""
        return self.turnover_24h * self.target_share / (self.trades_per_day * 2.0)

    @property
    def occupancy(self) -> float:
        return sum(t.hold_minutes for t in self.trades) / (self.d_eff_days * 1440.0)


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Пороги и режимы. Ничего из этого не зашито в код алгоритма."""

    deposit: float = 1000.0
    dd_target_pct: float = 5.0
    margin_limit: float = 0.40
    limiters: tuple[int, ...] = (2, 3, 4)
    max_size_factor: int = 3
    cancel_opposite: bool = True
    long_short_same_slot: bool = False
    acceptance_min: float = 0.35
    load_reject_above: float = 2.5
    asymmetry_ratio: float = 20.0
    asymmetry_slow_occupancy: float = 0.3
    d_eff_common_min_days: float = 30.0
    oos_min_days: float = 40.0
    correlation_flag: float = 0.7
    weight_levels: tuple[int, ...] = (1,)
    g_grid_lo: float = 0.2
    g_grid_hi: float = 2.0
    g_grid_points: int = 8
    g_refine_points: int = 3
    top_sets_for_weights: int = 200


@dataclass(frozen=True, slots=True)
class StrategyOutcome:
    strategy_id: str
    accepted: int
    blocked_slot: int
    blocked_margin: int
    pnl_abs: float

    @property
    def blocked(self) -> int:
        return self.blocked_slot + self.blocked_margin

    @property
    def acceptance(self) -> float:
        total = self.accepted + self.blocked
        return self.accepted / total if total else 0.0


@dataclass(frozen=True, slots=True)
class SetResult:
    strategy_ids: tuple[str, ...]
    limiter: int
    weights: tuple[int, ...]
    g: float
    lots: tuple[float, ...]
    pnl_abs: float
    pnl_pct: float
    pnl30_pct: float
    max_dd_pct: float
    max_margin_ratio: float
    max_occupancy_margin: float
    min_buffer: float
    d_eff_common_days: float
    outcomes: tuple[StrategyOutcome, ...]
    flags: tuple[str, ...] = ()

    @property
    def capital_requirement(self) -> float:
        """§11: пиковая занятая маржа плюс просадка."""
        return self.max_occupancy_margin + self.max_dd_pct / 100.0

    @property
    def efficiency(self) -> float:
        req = self.capital_requirement
        return self.pnl30_pct / req if req > 0 else 0.0
