from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Iterable

from .config import AlgorithmConfig


class LotMethod(StrEnum):
    EQUAL = "EQUAL"
    INCOME = "INCOME"


def _quantum(config: AlgorithmConfig) -> Decimal:
    return Decimal("1").scaleb(-config.lot_rounding_decimals)


def _rounded_parts(
    raw_parts: Iterable[Decimal],
    total: Decimal,
    config: AlgorithmConfig,
) -> tuple[Decimal, ...]:
    parts = tuple(raw_parts)
    if not parts:
        raise ValueError("at least one lot is required")
    quantum = _quantum(config)
    rounded = [part.quantize(quantum, rounding=ROUND_HALF_UP) for part in parts[:-1]]
    rounded.append((total - sum(rounded, Decimal("0"))).quantize(quantum))
    return tuple(rounded)


def equal_lots(order_count: int, config: AlgorithmConfig) -> tuple[Decimal, ...]:
    if order_count < 1:
        raise ValueError("order count must be positive")
    raw = config.initial_lot_sum / Decimal(order_count)
    return _rounded_parts((raw for _ in range(order_count)), config.initial_lot_sum, config)


def income_lots(
    source_pnls: Iterable[Decimal],
    config: AlgorithmConfig,
) -> tuple[Decimal, ...]:
    pnls = tuple(Decimal(value) for value in source_pnls)
    if not pnls or any(value <= 0 for value in pnls):
        raise ValueError("source PnL values must all be positive")
    total_pnl = sum(pnls, Decimal("0"))
    return _rounded_parts(
        (config.initial_lot_sum * pnl / total_pnl for pnl in pnls),
        config.initial_lot_sum,
        config,
    )


def allocate_lots(
    orders: Iterable[dict[str, object]],
    method: LotMethod,
    config: AlgorithmConfig,
) -> tuple[Decimal, ...]:
    values = tuple(orders)
    if method is LotMethod.EQUAL:
        return equal_lots(len(values), config)
    return income_lots(
        (Decimal(str(order["source_pnl_pct"])) for order in values), config
    )


def scale_lots_dd5(
    lots: tuple[Decimal, ...],
    raw_dd_pct: Decimal,
    config: AlgorithmConfig,
) -> tuple[tuple[Decimal, ...], Decimal]:
    dd = Decimal(raw_dd_pct)
    if dd <= 0:
        raise ValueError("raw drawdown must be positive")
    scale = config.target_dd_pct / dd
    quantum = _quantum(config)
    scaled = tuple((lot * scale).quantize(quantum, rounding=ROUND_HALF_UP) for lot in lots)
    return scaled, scale

