from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping, Sequence


class PreciseMetricError(ValueError):
    """Raised when immutable evidence cannot produce canonical metrics."""


@dataclass(frozen=True, slots=True)
class PreciseBacktestMetrics:
    final_balance: Decimal
    total_pnl: Decimal
    total_pnl_pct: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    win_rate_pct: Decimal


_DECLARED_PRECISION = Decimal("0.01")
_DECLARED_KEYS = {
    "initial_balance": ("Initial balance", "InitialBalance"),
    "final_balance": ("Final balance", "FinalBalance"),
    "total_pnl": ("Total PnL", "TotalPnL"),
    "total_pnl_pct": ("Total PnL, %", "TotalPnLPercent"),
    "max_drawdown": ("Max Drawdown", "MaxDrawdown"),
    "max_drawdown_pct": ("Max Drawdown, %", "MaxDrawdownPercent"),
    "win_rate_pct": ("Win Rate, %", "WinRate"),
}


def _decimal(value: object, label: str) -> Decimal:
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise PreciseMetricError(f"{label} must be a finite decimal") from error
    if not converted.is_finite():
        raise PreciseMetricError(f"{label} must be a finite decimal")
    return converted


def _positive_decimal(value: object, label: str) -> Decimal:
    converted = _decimal(value, label)
    if converted <= 0:
        raise PreciseMetricError(f"{label} must be positive")
    return converted


def _series(values: Sequence[object], label: str) -> tuple[Decimal, ...]:
    if isinstance(values, (str, bytes)) or len(values) == 0:
        raise PreciseMetricError(f"{label} samples must not be empty")
    return tuple(_positive_decimal(value, label) for value in values)


def _integer(value: object, label: str) -> int:
    converted = _decimal(value, label)
    if converted != converted.to_integral_value():
        raise PreciseMetricError(f"{label} must be an integer")
    return int(converted)


def _declared(
    declared_metrics: Mapping[str, object],
    key: str,
) -> Decimal:
    for name in _DECLARED_KEYS[key]:
        if name in declared_metrics:
            return _decimal(declared_metrics[name], name)
    raise PreciseMetricError(f"declared metrics missing {_DECLARED_KEYS[key][0]}")


def _validate_declared(
    actual: Decimal,
    declared: Decimal,
    label: str,
) -> None:
    quantum = Decimal("1").scaleb(declared.as_tuple().exponent)
    rounded = actual.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded != declared:
        raise PreciseMetricError(f"derived {label} does not match declared metrics")


def derive_precise_metrics(
    initial_balance: object,
    wallet_values: Sequence[object],
    equity_values: Sequence[object],
    win_trades: object,
    total_trades: object,
    *,
    timestamps: Sequence[object] | None = None,
    declared_metrics: Mapping[str, object] | None = None,
) -> PreciseBacktestMetrics:
    """Derive canonical tester metrics from immutable wallet/equity/count evidence."""
    if isinstance(wallet_values, (str, bytes)) or isinstance(equity_values, (str, bytes)):
        raise PreciseMetricError("wallet and equity samples must be sequences")
    if len(wallet_values) != len(equity_values):
        raise PreciseMetricError("wallet and equity sample counts must match")
    wallet = _series(wallet_values, "wallet")
    equity = _series(equity_values, "equity")
    if timestamps is not None:
        if len(timestamps) != len(wallet):
            raise PreciseMetricError("wallet/equity timestamps must match sample counts")
        if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
            raise PreciseMetricError("wallet/equity timestamps must be strictly increasing")
    initial = _positive_decimal(initial_balance, "initial balance")
    wins = _integer(win_trades, "win_trades")
    total = _integer(total_trades, "total_trades")
    if total <= 0:
        raise PreciseMetricError("total_trades must be positive")
    if wins < 0 or wins > total:
        raise PreciseMetricError("win_trades must be between zero and total_trades")

    final_balance = wallet[-1]
    total_pnl = final_balance - initial
    total_pnl_pct = total_pnl / initial * Decimal("100")
    peak = equity[0]
    peak_at_max_drawdown = peak
    max_drawdown = Decimal("0")
    for value in equity:
        peak = max(peak, value)
        drawdown = peak - value
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            peak_at_max_drawdown = peak
    max_drawdown_pct = max_drawdown / peak_at_max_drawdown * Decimal("100")
    win_rate_pct = Decimal(wins) / Decimal(total) * Decimal("100")

    if declared_metrics is not None:
        _validate_declared(initial, _declared(declared_metrics, "initial_balance"), "initial balance")
        _validate_declared(final_balance, _declared(declared_metrics, "final_balance"), "final balance")
        _validate_declared(total_pnl, _declared(declared_metrics, "total_pnl"), "total PnL")
        _validate_declared(total_pnl_pct, _declared(declared_metrics, "total_pnl_pct"), "total PnL, %")
        _validate_declared(max_drawdown, _declared(declared_metrics, "max_drawdown"), "max drawdown")
        _validate_declared(max_drawdown_pct, _declared(declared_metrics, "max_drawdown_pct"), "max drawdown, %")
        _validate_declared(win_rate_pct, _declared(declared_metrics, "win_rate_pct"), "win rate, %")

    return PreciseBacktestMetrics(
        final_balance=final_balance,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        win_rate_pct=win_rate_pct,
    )
