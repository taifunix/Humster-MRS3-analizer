"""Small, deterministic PRETEST_PROXY equity-path calculations.

The proxy is deliberately linear: source equity changes are scaled by the
actual full-position notional divided by the source tested notional.  It is
an admission aid for ranking and never a joint tester result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import math
from numbers import Real
from types import MappingProxyType
from typing import Any, Mapping, Sequence


PRETEST_PROXY = "PRETEST_PROXY"
PRETEST_SOURCE_CURRENT_RESULT = "PRETEST_SOURCE_CURRENT_RESULT"
LINEAR_SCALING_ASSUMPTION_UNVERIFIED = "LINEAR_SCALING_ASSUMPTION_UNVERIFIED"
UNKNOWN = "UNKNOWN"
NOT_TESTED = "NOT_TESTED"
MONEY_QUANTUM = Decimal("0.00000001")
TESTED_SIZE_BASIS = "SOURCE_INITIAL_BALANCE_X_OPENING_LOT"


class PretestProxyError(ValueError):
    pass


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    # Equity arrays may originate in pandas/numpy and therefore contain
    # float64 scalars.  Convert their text form once, then keep all computed
    # money as Decimal so persisted values remain deterministic.
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str, Real)):
        raise PretestProxyError(f"{field} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PretestProxyError(f"{field} must be a finite decimal") from error
    if not result.is_finite() or (positive and result <= 0):
        raise PretestProxyError(f"{field} must be a finite decimal")
    return result


def _timestamp(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise PretestProxyError(f"{field} must be an ISO timestamp") from error
    else:
        raise PretestProxyError(f"{field} must be an ISO timestamp")
    if result.tzinfo is None:
        raise PretestProxyError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


def _path_item(value: Any, index: int) -> tuple[Any, datetime, Decimal]:
    if isinstance(value, Mapping):
        timestamp = value.get("timestamp_utc", value.get("timestamp", value.get("time")))
        equity = value.get("equity", value.get("value"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
        timestamp, equity = value
    else:
        raise PretestProxyError(f"equity_path[{index}] must contain timestamp and equity")
    return value, _timestamp(timestamp, f"equity_path[{index}].timestamp"), _decimal(equity, f"equity_path[{index}].equity")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _public_source(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _public_source(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_public_source(item) for item in value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def scale_equity_path(
    equity_path: Sequence[Any],
    *,
    source_initial_balance: Any,
    tested_size_usdt: Any,
    actual_size_usdt: Any,
    campaign_equity: Any = 0,
) -> tuple[Mapping[str, Any], ...]:
    """Scale each source equity increment into campaign currency."""
    source_initial = _decimal(source_initial_balance, "source_initial_balance", positive=True)
    tested = _decimal(tested_size_usdt, "tested_size_usdt", positive=True)
    actual = _decimal(actual_size_usdt, "actual_size_usdt", positive=True)
    campaign = _decimal(campaign_equity, "campaign_equity")
    parsed = [_path_item(value, index) for index, value in enumerate(equity_path)]
    previous: datetime | None = None
    for _raw, timestamp, _value in parsed:
        if previous is not None and timestamp <= previous:
            raise PretestProxyError("equity_path timestamps must be strictly increasing")
        previous = timestamp
    scale = actual / tested
    result: list[Mapping[str, Any]] = []
    with localcontext() as context:
        context.prec = max(64, len(str(source_initial)) + len(str(tested)) + len(str(actual)) + 16)
        for raw, timestamp, source_equity in parsed:
            increment = (source_equity - source_initial) * scale
            result.append(MappingProxyType({
                "timestamp_utc": timestamp,
                "equity": _quantize(campaign + increment),
                "source_equity": source_equity,
                "scaled_increment": _quantize(increment),
                "source_initial_balance": source_initial,
                "tested_size_usdt": tested,
                "actual_size_usdt": actual,
                "tested_size_basis": TESTED_SIZE_BASIS,
                "scale": scale,
                "assumption": LINEAR_SCALING_ASSUMPTION_UNVERIFIED,
                "source": _public_source(raw),
            }))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ProxyMetrics:
    status: str
    end_pnl_usdt: Decimal | None = None
    max_drawdown_usdt: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    recovery_factor: Decimal | str | None = None
    reserve_usdt: Decimal | None = None
    reserve_pct: Decimal | None = None
    equity_path: tuple[Mapping[str, Any], ...] = ()
    reason: str | None = None
    metric_basis: str = PRETEST_PROXY
    source_basis: str = PRETEST_SOURCE_CURRENT_RESULT
    assumptions: tuple[str, ...] = (LINEAR_SCALING_ASSUMPTION_UNVERIFIED,)
    tested_size_usdt: Decimal | None = None
    actual_size_usdt: Decimal | None = None
    tested_size_basis: str = TESTED_SIZE_BASIS

    @property
    def pnl(self) -> Decimal | None:
        return self.end_pnl_usdt

    @property
    def max_dd_usdt(self) -> Decimal | None:
        return self.max_drawdown_usdt

    @property
    def max_dd_pct(self) -> Decimal | None:
        return self.max_drawdown_pct

    @property
    def recovery(self) -> Decimal | str | None:
        return self.recovery_factor

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "proxy_pnl_usdt": self.end_pnl_usdt,
            "proxy_end_pnl_usdt": self.end_pnl_usdt,
            "proxy_max_drawdown_usdt": self.max_drawdown_usdt,
            "proxy_max_drawdown_pct": self.max_drawdown_pct,
            "proxy_max_dd_usdt": self.max_drawdown_usdt,
            "proxy_max_dd_pct": self.max_drawdown_pct,
            "proxy_recovery_factor": self.recovery_factor,
            "proxy_reserve_usdt": self.reserve_usdt,
            "proxy_reserve_pct": self.reserve_pct,
            "equity_path": self.equity_path,
            "reason": self.reason,
            "metric_basis": self.metric_basis,
            "source_basis": self.source_basis,
            "assumptions": self.assumptions,
            "tested_size_usdt": self.tested_size_usdt,
            "actual_size_usdt": self.actual_size_usdt,
            "tested_size_basis": self.tested_size_basis,
            "balance_percentage": UNKNOWN,
            "risk": UNKNOWN,
            "joint_metrics": NOT_TESTED,
            "joint_status": NOT_TESTED,
            "recommendation": NOT_TESTED,
        }


def compute_proxy_metrics(
    equity_path: Sequence[Any],
    *,
    campaign_equity: Any,
    source_initial_balance: Any | None = None,
    tested_size_usdt: Any | None = None,
    actual_size_usdt: Any | None = None,
) -> ProxyMetrics:
    """Compute end PnL, peak-to-trough DD, recovery and reserve."""
    campaign = _decimal(campaign_equity, "campaign_equity", positive=True)
    if source_initial_balance is not None or tested_size_usdt is not None or actual_size_usdt is not None:
        if source_initial_balance is None or tested_size_usdt is None or actual_size_usdt is None:
            raise PretestProxyError("all scaling fields are required together")
        path = scale_equity_path(
            equity_path,
            source_initial_balance=source_initial_balance,
            tested_size_usdt=tested_size_usdt,
            actual_size_usdt=actual_size_usdt,
            campaign_equity=campaign,
        )
    else:
        parsed = [_path_item(value, index) for index, value in enumerate(equity_path)]
        previous: datetime | None = None
        path_list: list[Mapping[str, Any]] = []
        for raw, timestamp, equity in parsed:
            if previous is not None and timestamp <= previous:
                raise PretestProxyError("equity_path timestamps must be strictly increasing")
            previous = timestamp
            path_list.append(MappingProxyType({"timestamp_utc": timestamp, "equity": _quantize(equity), "source": _public_source(raw)}))
        path = tuple(path_list)
    if not path:
        return ProxyMetrics("UNKNOWN", reason="EQUITY_PATH_EMPTY")
    # Treat the campaign account as an observed starting point.  A first
    # sampled equity below it must count as drawdown even when the path has no
    # explicit zero-time sample.
    peak = campaign
    max_dd = Decimal(0)
    max_dd_pct_value = Decimal(0)
    for point in path:
        equity = _decimal(point["equity"], "equity", positive=False)
        if equity > peak:
            peak = equity
        if peak > 0:
            max_dd = max(max_dd, peak - equity)
            max_dd_pct_value = max(max_dd_pct_value, (peak - equity) / peak * 100)
    end_equity = _decimal(path[-1]["equity"], "equity", positive=False)
    end_pnl = _quantize(end_equity - campaign)
    max_dd = _quantize(max_dd)
    dd_pct = _quantize(max_dd_pct_value) if peak > 0 else None
    recovery = _quantize(end_pnl / max_dd) if max_dd > 0 else UNKNOWN
    reserve = _quantize(min(campaign, *(_decimal(item["equity"], "equity") for item in path)))
    reserve_pct = _quantize(reserve / campaign * 100)
    tested_size = None
    actual_size = None
    if source_initial_balance is not None:
        tested_size = _decimal(tested_size_usdt, "tested_size_usdt", positive=True)
        actual_size = _decimal(actual_size_usdt, "actual_size_usdt", positive=True)
    return ProxyMetrics(
        "PASS", end_pnl, max_dd, dd_pct, recovery, reserve, reserve_pct, tuple(path),
        tested_size_usdt=tested_size, actual_size_usdt=actual_size,
    )


def evaluate_pretest_proxy(
    equity_path: Sequence[Any],
    *,
    campaign_equity: Any,
    source_initial_balance: Any,
    tested_size_usdt: Any,
    actual_size_usdt: Any,
) -> ProxyMetrics:
    return compute_proxy_metrics(
        equity_path,
        campaign_equity=campaign_equity,
        source_initial_balance=source_initial_balance,
        tested_size_usdt=tested_size_usdt,
        actual_size_usdt=actual_size_usdt,
    )


proxy_metrics = compute_proxy_metrics
compute_pretest_metrics = compute_proxy_metrics
calculate_proxy_metrics = compute_proxy_metrics
scale_source_equity = scale_equity_path

__all__ = [
    "LINEAR_SCALING_ASSUMPTION_UNVERIFIED", "MONEY_QUANTUM", "NOT_TESTED", "PRETEST_PROXY",
    "PRETEST_SOURCE_CURRENT_RESULT", "UNKNOWN", "PretestProxyError", "ProxyMetrics",
    "TESTED_SIZE_BASIS",
    "compute_proxy_metrics", "compute_pretest_metrics", "calculate_proxy_metrics", "evaluate_pretest_proxy",
    "proxy_metrics", "scale_equity_path", "scale_source_equity",
]
