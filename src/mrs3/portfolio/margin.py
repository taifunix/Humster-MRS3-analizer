"""Pure margin, sizing, leverage, and limiter calculations for M3.

The module deliberately accepts plain mappings as well as its small records.  It
does not read market data, call the tester, or write a store; callers provide
the frozen facts and retain the returned evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, Inexact, InvalidOperation, MAX_EMAX, MIN_EMIN, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, localcontext
from functools import wraps
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from .canonical import PORTFOLIO_REASON_V1



OBSERVED = "OBSERVED"
CALCULATED = "CALCULATED"
CONSERVATIVE_BOUND = "CONSERVATIVE_BOUND"
UNKNOWN = "UNKNOWN"
PASS = "PASS"
FAIL = "FAIL"
NEEDS_RETEST = "NEEDS_RETEST"

_D = Decimal
_WORK_PRECISION = 10000
_MAX_OPERATION_PRECISION = _WORK_PRECISION * 2 + 2


def _context_safe(function: Callable[..., Any]) -> Callable[..., Any]:
    """Run Decimal-heavy public calculations outside the caller's context."""
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with localcontext() as context:
            context.prec = _WORK_PRECISION
            context.rounding = ROUND_HALF_EVEN
            context.Emax = MAX_EMAX
            context.Emin = MIN_EMIN
            for signal in context.traps:
                context.traps[signal] = False
            return function(*args, **kwargs)
    return wrapped


def _divide_up(numerator: Decimal, denominator: Decimal) -> tuple[Decimal, bool]:
    """Return an upward-rounded quotient and whether the division is inexact."""
    with localcontext() as context:
        context.prec = _WORK_PRECISION
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        for signal in context.traps:
            context.traps[signal] = False
        context.rounding = ROUND_UP
        context.clear_flags()
        result = numerator / denominator
        division_inexact = bool(context.flags[Inexact])
        # The flag is authoritative for the operation.  The comparison is a
        # second guard for exact quotients whose coefficient exceeds the
        # working precision and may have been rounded by the context.
        exact = result * denominator == numerator
        return result, division_inexact or not exact


def _operation_precision(left: Decimal, right: Decimal, *, multiply: bool) -> int:
    left_tuple = left.as_tuple()
    right_tuple = right.as_tuple()
    if multiply:
        return len(left_tuple.digits) + len(right_tuple.digits) + 1
    low_exponent = min(left_tuple.exponent, right_tuple.exponent)
    return max(
        len(left_tuple.digits) + left_tuple.exponent - low_exponent,
        len(right_tuple.digits) + right_tuple.exponent - low_exponent,
    ) + 1


def _decimal_operation(left: Decimal, right: Decimal, *, operation: str, rounding: str) -> tuple[Decimal, bool]:
    precision = _operation_precision(left, right, multiply=operation == "multiply")
    with localcontext() as context:
        context.prec = max(_WORK_PRECISION, min(precision, _MAX_OPERATION_PRECISION))
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        for signal in context.traps:
            context.traps[signal] = False
        context.rounding = rounding
        context.clear_flags()
        if operation == "multiply":
            result = left * right
        elif operation == "add":
            result = left + right
        elif operation == "subtract":
            result = left - right
        else:
            raise ValueError(f"unknown decimal operation: {operation}")
        return result, bool(context.flags[Inexact]) or precision > _WORK_PRECISION


def _multiply_up(left: Decimal, right: Decimal) -> tuple[Decimal, bool]:
    return _decimal_operation(left, right, operation="multiply", rounding=ROUND_UP)


def _multiply_down(left: Decimal, right: Decimal) -> tuple[Decimal, bool]:
    return _decimal_operation(left, right, operation="multiply", rounding=ROUND_DOWN)


def _add_up(left: Decimal, right: Decimal) -> tuple[Decimal, bool]:
    return _decimal_operation(left, right, operation="add", rounding=ROUND_UP)


def _subtract_up(left: Decimal, right: Decimal) -> tuple[Decimal, bool]:
    return _decimal_operation(left, right, operation="subtract", rounding=ROUND_UP)


def _subtract_down(left: Decimal, right: Decimal) -> tuple[Decimal, bool]:
    return _decimal_operation(left, right, operation="subtract", rounding=ROUND_DOWN)


def _decimal(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (float, bool)):
        raise TypeError(f"{name} must be an exact Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a Decimal") from exc
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{name} has an invalid value")
    return result


def _strict_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a bool")
    return value


def _value(item: Any, *names: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        for name in names:
            if name in item:
                return item[name]
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _freeze(value: Any) -> Any:
    """Copy nested witness data into immutable containers."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class Evidence:
    """A number plus the facts needed to interpret it."""

    value: Any = None
    evidence_class: str = UNKNOWN
    denominator: Any = None
    availability: str = "UNKNOWN"
    provenance: str | None = None
    reason: str | None = None
    timestamp_ms: int | None = None
    model_version: str | None = None
    numerator: Any = None

    def __post_init__(self) -> None:
        if self.evidence_class not in {OBSERVED, CALCULATED, CONSERVATIVE_BOUND, UNKNOWN}:
            raise ValueError("unknown evidence class")
        if self.evidence_class == UNKNOWN and self.value is not None:
            raise ValueError("UNKNOWN evidence cannot carry a value")
        if self.evidence_class != UNKNOWN and self.value is None:
            raise ValueError("known evidence requires a value")
        if self.reason is not None and self.reason not in PORTFOLIO_REASON_V1:
            raise ValueError("reason must be a stable portfolio_reason_v1 value")

    @classmethod
    def observed(cls, value: Any, **kwargs: Any) -> "Evidence":
        return cls(value, OBSERVED, availability="AVAILABLE", **kwargs)

    @classmethod
    def calculated(cls, value: Any, **kwargs: Any) -> "Evidence":
        return cls(value, CALCULATED, availability="AVAILABLE", **kwargs)

    @classmethod
    def conservative(cls, value: Any, **kwargs: Any) -> "Evidence":
        return cls(value, CONSERVATIVE_BOUND, availability="AVAILABLE", **kwargs)

    @classmethod
    def unknown(cls, reason: str, **kwargs: Any) -> "Evidence":
        return cls(None, UNKNOWN, availability="MISSING", reason=reason, **kwargs)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    maker_fee: Decimal
    taker_fee: Decimal
    source: str
    provenance: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "maker_fee", _decimal(self.maker_fee, "maker_fee", nonnegative=True))
        object.__setattr__(self, "taker_fee", _decimal(self.taker_fee, "taker_fee", nonnegative=True))
        if self.source not in {"backtest_manifest", "tester_manifest", "deployment"} or (self.source == "deployment" and not self.provenance):
            raise ValueError("fee source and deployment provenance are required")

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any], *, source: str = "backtest_manifest", provenance: str | None = None) -> "FeeSchedule":
        def get(*names: str) -> Any:
            return _value(manifest, *names)

        maker = get("MakerFee", "maker_fee", "makerFee")
        taker = get("TakerFee", "taker_fee", "takerFee")
        if maker is None or taker is None:
            raise ValueError("manifest must contain MakerFee and TakerFee")
        return cls(maker, taker, source, provenance)

    @property
    def maker(self) -> Decimal:
        return self.maker_fee

    @property
    def taker(self) -> Decimal:
        return self.taker_fee


@dataclass(frozen=True, slots=True, init=False)
class Position:
    symbol: str
    side: str
    qty: Decimal
    mark_price: Decimal | None
    priority: int | None
    pair_slot: str | None
    maker: bool
    confirmed: bool

    def __init__(self, symbol: str, side: str, qty: Decimal | int | str = 0, mark_price: Decimal | int | str | None = None, *, quantity: Decimal | int | str | None = None, mark: Decimal | int | str | None = None, priority: int | None = 1, pair_slot: str | None = None, maker: bool = False, confirmed: bool = True) -> None:
        object.__setattr__(self, "symbol", str(symbol))
        object.__setattr__(self, "side", str(side).upper())
        object.__setattr__(self, "qty", _decimal(qty if quantity is None else quantity, "qty", nonnegative=True))
        object.__setattr__(self, "mark_price", None if mark_price is None and mark is None else _decimal(mark_price if mark is None else mark, "mark_price", positive=True))
        if priority is not None and type(priority) is not int:
            raise TypeError("priority must be an integer or None")
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "pair_slot", pair_slot)
        object.__setattr__(self, "maker", maker)
        object.__setattr__(self, "confirmed", confirmed)

    @property
    def quantity(self) -> Decimal:
        return self.qty

    @property
    def mark(self) -> Decimal | None:
        return self.mark_price

    @property
    def notional(self) -> Decimal | None:
        return None if self.mark_price is None else self.qty * self.mark_price


@dataclass(frozen=True, slots=True, init=False)
class Order:
    symbol: str
    side: str
    qty: Decimal
    price: Decimal | None
    reduce_only: bool
    maker: bool
    priority: int | None
    pair_slot: str | None
    filled_qty: Decimal
    cancel_requested: bool
    cancel_confirmed: bool
    status: str
    close_maker: bool

    def __init__(self, symbol: str, side: str, qty: Decimal | int | str = 0, price: Decimal | int | str | None = None, *, quantity: Decimal | int | str | None = None, mark_price: Decimal | int | str | None = None, reduce_only: bool = False, maker: bool = False, close_maker: bool = False, priority: int | None = 1, pair_slot: str | None = None, filled_qty: Decimal | int | str = 0, filled_quantity: Decimal | int | str | None = None, cancel_requested: bool = False, cancel_confirmed: bool = False, status: str = "OPEN") -> None:
        object.__setattr__(self, "symbol", str(symbol))
        object.__setattr__(self, "side", str(side).upper())
        object.__setattr__(self, "qty", _decimal(qty if quantity is None else quantity, "qty", nonnegative=True))
        object.__setattr__(self, "price", None if price is None and mark_price is None else _decimal(price if mark_price is None else mark_price, "price", positive=True))
        object.__setattr__(self, "reduce_only", _strict_bool(reduce_only, "reduce_only"))
        object.__setattr__(self, "maker", _strict_bool(maker, "maker"))
        object.__setattr__(self, "close_maker", _strict_bool(close_maker, "close_maker"))
        if priority is not None and type(priority) is not int:
            raise TypeError("priority must be an integer or None")
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "pair_slot", pair_slot)
        object.__setattr__(self, "filled_qty", _decimal(filled_qty if filled_quantity is None else filled_quantity, "filled_qty", nonnegative=True))
        object.__setattr__(self, "cancel_requested", _strict_bool(cancel_requested, "cancel_requested"))
        object.__setattr__(self, "cancel_confirmed", _strict_bool(cancel_confirmed, "cancel_confirmed"))
        object.__setattr__(self, "status", str(status).upper())

    @property
    def quantity(self) -> Decimal:
        return self.qty

    @property
    def remaining_qty(self) -> Decimal:
        return max(Decimal("0"), self.qty - self.filled_qty)

    @property
    def notional(self) -> Decimal | None:
        return None if self.price is None else self.remaining_qty * self.price


@dataclass(frozen=True, slots=True)
class QuantityResult:
    status: str
    quantity: Decimal
    reason: str | None = None
    notional: Decimal | None = None
    checks: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", _freeze(self.checks))


@dataclass(frozen=True, slots=True)
class LeverageResult:
    status: str
    symbol: str
    exposure: Decimal
    tier: Any | None
    leverage: Decimal | None
    evidence: Evidence
    reason: str | None = None
    conflict_symbols: tuple[str, ...] = ()

    @property
    def planned_leverage(self) -> Decimal | None:
        return self.leverage


@dataclass(frozen=True, slots=True)
class MarginComponent:
    kind: str
    symbol: str
    notional: Decimal
    initial_margin: Decimal | None
    maintenance_margin: Decimal | None
    fee: Decimal | None
    evidence: Evidence
    witness: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "witness", _freeze(self.witness))

    @property
    def im(self) -> Decimal | None:
        return self.initial_margin

    @property
    def mm(self) -> Decimal | None:
        return self.maintenance_margin


@dataclass(frozen=True, slots=True)
class MarginResult:
    status: str
    reason: str | None
    evidence_class: str
    position_im: Decimal | None
    order_im: Decimal | None
    total_im: Decimal | None
    position_mm: Decimal | None
    total_mm: Decimal | None
    denominator: Evidence
    components: tuple[MarginComponent, ...] = ()
    witness: Mapping[str, Any] = field(default_factory=dict)
    planned_leverage: Mapping[str, Decimal] = field(default_factory=dict)
    fallback_used: bool = False
    reasons: tuple[str, ...] = ()
    fee_evidence: Evidence = field(default_factory=lambda: Evidence.unknown("FEE_RATE_UNKNOWN"))
    order_loss_evidence: Evidence = field(default_factory=lambda: Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"))
    collateral_haircut_evidence: Evidence = field(default_factory=lambda: Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"))
    model_guard_evidence: Evidence = field(default_factory=lambda: Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"))
    all_executable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "witness", _freeze(self.witness))
        object.__setattr__(self, "planned_leverage", _freeze(self.planned_leverage))

    @property
    def gate_result(self) -> str:
        return self.status

    @property
    def initial_margin(self) -> Decimal | None:
        return self.total_im

    @property
    def maintenance_margin(self) -> Decimal | None:
        return self.total_mm


@dataclass(frozen=True, slots=True)
class LimiterResult:
    status: str
    limit: int | None
    counted_slots: int
    exempt_slots: int
    nonflat_slots: tuple[str, ...]
    exempt_pair_slots: tuple[str, ...]
    allowed_openings: tuple[str, ...]
    pending_cancels: tuple[str, ...]
    race_slot_max: int
    evidence_class: str
    reason: str | None = None
    witness: Mapping[str, Any] = field(default_factory=dict)
    reserved_openings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "witness", _freeze(self.witness))

    @property
    def slots(self) -> int:
        return self.counted_slots


@dataclass(frozen=True, slots=True)
class EnvelopeResult:
    status: str
    result: MarginResult | None
    evidence_class: str
    reason: str | None
    evaluated_states: int
    witness: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "witness", _freeze(self.witness))


@dataclass(frozen=True, slots=True)
class LeverageReadbackResult:
    status: str
    reason: str | None
    missing_symbols: tuple[str, ...] = ()
    mismatched_symbols: tuple[str, ...] = ()
    witness: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "witness", _freeze(self.witness))

    @property
    def gate_result(self) -> str:
        return self.status


@dataclass(frozen=True, slots=True)
class DepositResult:
    status: str
    reason: str | None
    deposit: Decimal
    total_im: Decimal
    total_mm: Decimal
    minimum_deposit: Decimal | None
    denominator: Evidence

    @property
    def gate_result(self) -> str:
        return self.status


def _pair_slot(item: Any) -> str:
    if isinstance(item, str):
        return item
    value = _value(item, "pair_slot", "pair", default=None)
    return str(value if value is not None else _value(item, "symbol", default=""))


def _pair_slots(items: Iterable[Any]) -> tuple[str, ...]:
    return tuple(_pair_slot(item) for item in items)


def _remaining_quantity(item: Any) -> Decimal:
    remaining = _value(item, "remaining_qty", default=None)
    if remaining is not None:
        return _decimal(remaining, "remaining_qty", nonnegative=True)
    qty = _decimal(_value(item, "qty", "quantity", default=0), "quantity", nonnegative=True)
    filled = _value(item, "filled_qty", "filled_quantity", default=0)
    return max(Decimal("0"), qty - _decimal(filled, "filled_qty", nonnegative=True))


_ACTIVE_ORDER_STATUSES = {"OPEN", "NEW", "PARTIALLY_FILLED", "PENDING", "ACTIVE"}
_TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
_ORDER_BOOLEAN_FIELDS = ("reduce_only", "maker", "close_maker", "cancel_requested", "cancel_confirmed")


def _order_booleans_valid(item: Any) -> bool:
    for name in _ORDER_BOOLEAN_FIELDS:
        try:
            value = _value(item, name, default=False)
        except Exception:
            return False
        if type(value) is not bool:
            return False
    return True


def _order_state_invalid(item: Any) -> bool:
    return not _order_booleans_valid(item) or _state_invalid(item, order=True)


def _state_invalid(item: Any, *, order: bool) -> bool:
    if not order:
        return _value(item, "confirmed", default=True) is not True
    status = str(_value(item, "status", default="OPEN")).upper()
    if status in _ACTIVE_ORDER_STATUSES:
        return False
    if status in _TERMINAL_ORDER_STATUSES:
        try:
            return _remaining_quantity(item) > 0
        except (TypeError, ValueError):
            return True
    return True


_INVALID_PRIORITY = object()
_PRIORITY_MISSING = "missing"
_PRIORITY_VALID = "valid"
_PRIORITY_INVALID = "invalid"
_PRIORITY_CONFLICTING = "conflicting"


def _priority_state(item: Any) -> tuple[bool, int | None | object, str]:
    try:
        if isinstance(item, Mapping):
            statements = [item[name] for name in ("priority", "position_priority") if name in item]
        else:
            statements = [getattr(item, name) for name in ("priority", "position_priority") if hasattr(item, name)]
    except Exception:
        return True, _INVALID_PRIORITY, _PRIORITY_INVALID
    if not statements:
        return False, None, _PRIORITY_MISSING
    if any(value is not None and type(value) is not int for value in statements):
        return True, _INVALID_PRIORITY, _PRIORITY_INVALID
    values = {value for value in statements if value is not None}
    if len(values) > 1:
        return True, _INVALID_PRIORITY, _PRIORITY_CONFLICTING
    if values:
        return True, next(iter(values)), _PRIORITY_VALID
    return True, None, _PRIORITY_MISSING


def _round_down_multiple(value: Decimal, step: Decimal) -> Decimal:
    """Round using integer coefficient arithmetic, independent of Decimal context."""
    value_tuple = value.as_tuple()
    step_tuple = step.as_tuple()
    common_exponent = min(value_tuple.exponent, step_tuple.exponent)
    value_coefficient = 0
    for digit in value_tuple.digits:
        value_coefficient = value_coefficient * 10 + digit
    step_coefficient = 0
    for digit in step_tuple.digits:
        step_coefficient = step_coefficient * 10 + digit
    value_integer = value_coefficient * 10 ** (value_tuple.exponent - common_exponent)
    step_integer = step_coefficient * 10 ** (step_tuple.exponent - common_exponent)
    rounded_integer = (value_integer // step_integer) * step_integer
    if rounded_integer == 0:
        digits = (0,)
    else:
        digits_reversed: list[int] = []
        remaining = rounded_integer
        while remaining:
            remaining, digit = divmod(remaining, 10)
            digits_reversed.append(digit)
        digits = tuple(reversed(digits_reversed))
    return Decimal((0, digits, common_exponent))


def round_quantity(quantity: Decimal | int | str, qty_step: Decimal | int | str) -> Decimal:
    quantity = _decimal(quantity, "quantity", nonnegative=True)
    step = _decimal(qty_step, "qty_step", positive=True)
    return _round_down_multiple(quantity, step)


@_context_safe
def validate_quantity(
    quantity: Decimal | int | str,
    *,
    qty_step: Decimal | int | str,
    price: Decimal | int | str,
    min_qty: Decimal | int | str,
    max_qty: Decimal | int | str | None = None,
    min_notional: Decimal | int | str | None = None,
    geometry_ok: bool = True,
    liquidity_ok: bool = True,
    margin_ok: bool = True,
) -> QuantityResult:
    """Round down, then report each post-rounding guard in ``checks``.

    ``positive`` and ``min_qty`` cover quantity minimums, ``min_notional`` and
    ``max_qty`` cover exchange quantity/notional bounds, and ``geometry``,
    ``liquidity``, and ``margin`` retain their respective downstream guards.
    The v1 stable reason remains ``POST_ROUNDING_MINIMUM`` for every failed
    non-geometry check; callers use this mapping for the exact diagnostic.
    """
    rounded = round_quantity(quantity, qty_step)
    price_d = _decimal(price, "price", positive=True)
    min_qty_d = _decimal(min_qty, "min_qty", nonnegative=True)
    max_qty_d = None if max_qty is None else _decimal(max_qty, "max_qty", positive=True)
    min_notional_d = None if min_notional is None else _decimal(min_notional, "min_notional", nonnegative=True)
    notional = rounded * price_d
    checks = {
        "positive": rounded > 0,
        "min_qty": rounded >= min_qty_d,
        "min_notional": min_notional_d is None or notional >= min_notional_d,
        "max_qty": max_qty_d is None or rounded <= max_qty_d,
        "geometry": geometry_ok,
        "liquidity": liquidity_ok,
        "margin": margin_ok,
    }
    if not checks["geometry"]:
        reason = "POST_ROUNDING_GEOMETRY"
    elif not all(checks.values()):
        reason = "POST_ROUNDING_MINIMUM"
    else:
        return QuantityResult(PASS, rounded, notional=notional, checks=checks)
    return QuantityResult(FAIL, rounded, reason=reason, notional=notional, checks=checks)


@_context_safe
def sizing_notional(
    balance: Decimal | int | str,
    scalar_pct: Decimal | int | str,
    lot_x: Decimal | int | str,
    *,
    max_balance: Decimal | int | str | None = None,
) -> Decimal:
    base = _decimal(balance, "balance", nonnegative=True)
    scalar, _ = _divide_up(_decimal(scalar_pct, "scalar_pct", nonnegative=True), Decimal("100"))
    lot = _decimal(lot_x, "lot_x", nonnegative=True)
    cap = base if max_balance is None else min(base, _decimal(max_balance, "max_balance", nonnegative=True))
    return cap * scalar * lot


@_context_safe
def dynamic_sizing(
    balances: Iterable[Decimal | int | str],
    scalar_pct: Decimal | int | str,
    lot_x: Decimal | int | str,
    *,
    max_balance: Decimal | int | str | None = None,
) -> tuple[Decimal, ...]:
    return tuple(sizing_notional(item, scalar_pct, lot_x, max_balance=max_balance) for item in balances)


def _tiers_for(symbol: str, tiers: Iterable[Any]) -> tuple[Any, ...]:
    rows = tuple(item for item in tiers if _value(item, "symbol", default=None) == symbol)
    return tuple(sorted(rows, key=lambda item: _decimal(_value(item, "risk_limit_value", "risk_limit", "upper_bound", "max_notional", default=0), "risk limit", positive=True)))


def _tier_for(symbol: str, exposure: Decimal, tiers: Iterable[Any]) -> Any:
    rows = _tiers_for(symbol, tiers)
    if not rows:
        raise ValueError("MARGIN_BOUND_UNAVAILABLE")
    for row in rows:
        bound = _decimal(_value(row, "risk_limit_value", "risk_limit", "upper_bound", "max_notional", default=None), "risk limit", positive=True)
        if exposure < bound:
            return row
    raise ValueError("MARGIN_BOUND_FAILED")


@_context_safe
def planned_leverage_for_symbol(
    symbol: str,
    *,
    position_exposure: Decimal | int | str = 0,
    active_order_exposure: Decimal | int | str = 0,
    tiers: Iterable[Any],
    leverage_step: Decimal | int | str,
    instrument_max_leverage: Decimal | int | str | None = None,
) -> LeverageResult:
    exposure = Decimal("0")
    try:
        position = _decimal(position_exposure, "position_exposure", nonnegative=True)
        orders = _decimal(active_order_exposure, "active_order_exposure", nonnegative=True)
        exposure, _ = _add_up(position, orders)
        if exposure <= 0:
            raise ValueError("MARGIN_BOUND_UNAVAILABLE")
        tier = _tier_for(symbol, exposure, tiers)
        maximum = _decimal(_value(tier, "max_leverage", "maximum_leverage", default=None), "max_leverage", positive=True)
        if instrument_max_leverage is not None:
            maximum = min(maximum, _decimal(instrument_max_leverage, "instrument_max_leverage", positive=True))
        step = _decimal(leverage_step, "leverage_step", positive=True)
        leverage = _round_down_multiple(maximum, step)
        if leverage <= 0:
            raise ValueError("MARGIN_BOUND_FAILED")
    except (ValueError, TypeError) as exc:
        reason = str(exc) if str(exc) in {"MARGIN_BOUND_FAILED", "MARGIN_BOUND_UNAVAILABLE"} else "MARGIN_BOUND_UNAVAILABLE"
        return LeverageResult(UNKNOWN, symbol, exposure, None, None, Evidence.unknown(reason), reason)
    return LeverageResult(PASS, symbol, exposure, tier, leverage, Evidence.calculated(leverage, denominator=exposure, provenance="risk_tier_max_leverage"))


@_context_safe
def planned_leverages(
    exposures: Mapping[str, tuple[Decimal | int | str, Decimal | int | str]],
    *,
    tiers: Iterable[Any],
    leverage_steps: Mapping[str, Decimal | int | str],
    instrument_max_leverage: Mapping[str, Decimal | int | str] | None = None,
    requested: Mapping[str, Sequence[Decimal | int | str]] | None = None,
    requested_leverages: Mapping[str, Sequence[Decimal | int | str]] | None = None,
) -> Mapping[str, LeverageResult]:
    requested = requested if requested is not None else requested_leverages
    result: dict[str, LeverageResult] = {}
    for symbol, raw_exposure in exposures.items():
        exposure = Decimal("0")
        try:
            position, orders = raw_exposure
            position = _decimal(position, "position", nonnegative=True)
            orders = _decimal(orders, "orders", nonnegative=True)
            exposure, _ = _add_up(position, orders)
        except (TypeError, ValueError):
            result[symbol] = LeverageResult(UNKNOWN, symbol, exposure, None, None, Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"), "MARGIN_BOUND_UNAVAILABLE")
            continue
        if exposure <= 0:
            result[symbol] = LeverageResult(UNKNOWN, symbol, exposure, None, None, Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"), "MARGIN_BOUND_UNAVAILABLE")
            continue
        raw_requests = requested.get(symbol, ()) if requested else ()
        try:
            requests = (raw_requests,) if isinstance(raw_requests, (Decimal, int, str)) else tuple(raw_requests)
            parsed_requests = tuple(_decimal(item, "requested leverage", positive=True) for item in requests)
        except (TypeError, ValueError):
            result[symbol] = LeverageResult(UNKNOWN, symbol, exposure, None, None, Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"), "MARGIN_BOUND_UNAVAILABLE")
            continue
        if len(set(parsed_requests)) > 1:
            result[symbol] = LeverageResult(UNKNOWN, symbol, exposure, None, None, Evidence.unknown("MARGIN_BOUND_FAILED"), "MARGIN_BOUND_FAILED", (symbol,))
            continue
        try:
            step = leverage_steps[symbol]
        except KeyError:
            result[symbol] = LeverageResult(UNKNOWN, symbol, exposure, None, None, Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"), "MARGIN_BOUND_UNAVAILABLE")
            continue
        result[symbol] = planned_leverage_for_symbol(symbol, position_exposure=position, active_order_exposure=orders, tiers=tiers, leverage_step=step, instrument_max_leverage=(instrument_max_leverage or {}).get(symbol))
    return result


def validate_applied_leverage(planned: Mapping[str, Decimal], applied: Mapping[str, Decimal | int | str | None]) -> LeverageReadbackResult:
    missing = tuple(sorted(symbol for symbol in planned if applied.get(symbol) is None))
    mismatch_values: list[str] = []
    for symbol, expected in planned.items():
        if applied.get(symbol) is None:
            continue
        try:
            same = _decimal(applied[symbol], "applied leverage", positive=True) == expected
        except (TypeError, ValueError):
            same = False
        if not same:
            mismatch_values.append(symbol)
    mismatch = tuple(sorted(mismatch_values))
    if missing or mismatch:
        return LeverageReadbackResult(NEEDS_RETEST, "LEVERAGE_MISMATCH", missing, mismatch, {"readback_available": not missing, "unreadable_symbols": missing})
    return LeverageReadbackResult(PASS, None, witness={"readback_available": True, "applied": dict(applied)})


def historical_leverage_evidence(value: Decimal | int | str, *, provenance: str) -> Evidence:
    """Retain a source test's leverage as provenance; it never gates a variant."""
    return Evidence.observed(_decimal(value, "historical_leverage", positive=True), provenance=provenance)


def symbol_conflicts(items: Iterable[Any]) -> tuple[str, ...]:
    sides: dict[str, set[str]] = {}
    for item in items:
        symbol = str(_value(item, "symbol", default=""))
        side = str(_value(item, "side", "direction", default="")).upper()
        if side:
            sides.setdefault(symbol, set()).add(side)
    return tuple(sorted(symbol for symbol, values in sides.items() if len(values) > 1))


def _validate_leverage_map(
    exposure: Mapping[str, tuple[Decimal, Decimal]],
    leverage: Mapping[str, Decimal],
    *,
    tiers: Sequence[Any],
    leverage_steps: Mapping[str, Decimal | int | str] | None,
    instrument_max_leverage: Mapping[str, Decimal | int | str] | None,
) -> str | None:
    if not leverage_steps:
        return "MARGIN_BOUND_UNAVAILABLE"
    for symbol, (position, orders) in exposure.items():
        try:
            combined_exposure, _ = _add_up(position, orders)
            tier = _tier_for(symbol, combined_exposure, tiers)
            maximum = _decimal(_value(tier, "max_leverage", "maximum_leverage", default=None), "max_leverage", positive=True)
            if instrument_max_leverage and symbol in instrument_max_leverage:
                maximum = min(maximum, _decimal(instrument_max_leverage[symbol], "instrument_max_leverage", positive=True))
            step = _decimal(leverage_steps[symbol], "leverage_step", positive=True)
            value = _decimal(leverage[symbol], "leverage", positive=True)
        except KeyError:
            return "MARGIN_BOUND_UNAVAILABLE"
        except (TypeError, ValueError):
            return "MARGIN_BOUND_UNAVAILABLE"
        if value > maximum or _round_down_multiple(value, step) != value:
            return "MARGIN_BOUND_FAILED"
    return None


def _fee(schedule: FeeSchedule | Mapping[str, Any] | None, maker: bool) -> tuple[Decimal | None, Evidence]:
    if schedule is None:
        return None, Evidence.unknown("FEE_RATE_UNKNOWN")
    if not isinstance(schedule, FeeSchedule):
        try:
            source = _value(schedule, "source", "fee_source", default=None)
            if source is None:
                return None, Evidence.unknown("FEE_RATE_UNKNOWN")
            schedule = FeeSchedule.from_manifest(schedule, source=str(source), provenance=_value(schedule, "provenance", "fee_provenance", default=None))
        except (TypeError, ValueError):
            return None, Evidence.unknown("FEE_RATE_UNKNOWN")
    rate = schedule.maker_fee if maker else schedule.taker_fee
    return rate, Evidence.observed(rate, provenance=schedule.provenance or schedule.source)


def _fee_evidence(schedule: FeeSchedule | Mapping[str, Any] | None, *, timestamp_ms: int | None = None, model_version: str = "m3_margin_v1") -> Evidence:
    if schedule is None:
        return Evidence.unknown("FEE_RATE_UNKNOWN", timestamp_ms=timestamp_ms, model_version=model_version)
    if not isinstance(schedule, FeeSchedule):
        try:
            source = _value(schedule, "source", "fee_source", default=None)
            if source is None:
                return Evidence.unknown("FEE_RATE_UNKNOWN", timestamp_ms=timestamp_ms, model_version=model_version)
            schedule = FeeSchedule.from_manifest(schedule, source=str(source), provenance=_value(schedule, "provenance", "fee_provenance", default=None))
        except (TypeError, ValueError):
            return Evidence.unknown("FEE_RATE_UNKNOWN", timestamp_ms=timestamp_ms, model_version=model_version)
    return Evidence.observed({"maker": schedule.maker_fee, "taker": schedule.taker_fee}, provenance=schedule.provenance or schedule.source, timestamp_ms=timestamp_ms, model_version=model_version)


def _tier_rate(tier: Any, names: tuple[str, ...]) -> Decimal | None:
    value = _value(tier, *names, default=None)
    return None if value is None else _decimal(value, names[0], nonnegative=True)


@_context_safe
def _item_notional_result(item: Any, *, order: bool) -> tuple[Decimal | None, bool]:
    explicit = item.get("notional") if isinstance(item, Mapping) and "notional" in item else None
    if explicit is not None:
        return _decimal(explicit, "notional", nonnegative=True), False
    qty = _value(item, "remaining_qty", default=None) if order else None
    if qty is None:
        qty = _value(item, "qty", "quantity", default=None)
        if order:
            filled = _value(item, "filled_qty", "filled_quantity", default=None)
            if filled is not None:
                qty = max(Decimal("0"), _decimal(qty, "quantity", nonnegative=True) - _decimal(filled, "filled_qty", nonnegative=True))
    price = _value(item, "price", "mark_price", "mark", default=None)
    if qty is None or price is None:
        return None, False
    return _multiply_up(_decimal(qty, "quantity", nonnegative=True), _decimal(price, "price", positive=True))


def _item_notional(item: Any, *, order: bool) -> Decimal | None:
    return _item_notional_result(item, order=order)[0]


@_context_safe
def evaluate_margin(
    positions: Iterable[Any] = (),
    orders: Iterable[Any] = (),
    *,
    tiers: Iterable[Any] = (),
    leverage: Mapping[str, Decimal | int | str] | None = None,
    leverage_steps: Mapping[str, Decimal | int | str] | None = None,
    instrument_max_leverage: Mapping[str, Decimal | int | str] | None = None,
    fees: FeeSchedule | Mapping[str, Any] | None = None,
    margin_balance: Decimal | int | str | None = None,
    collateral_haircut: Decimal | int | str | Evidence | None = None,
    order_loss: Decimal | int | str | Evidence | None = None,
    model_guard: Evidence | None = None,
    timestamp_ms: int | None = None,
    model_version: str = "m3_margin_v1",
) -> MarginResult:
    pos = tuple(positions)
    ords = tuple(orders)
    tiers = tuple(tiers)
    if any(_state_invalid(item, order=False) for item in pos) or any(_order_state_invalid(item) for item in ords):
        return MarginResult(
            UNKNOWN,
            "MARGIN_BOUND_UNAVAILABLE",
            UNKNOWN,
            None,
            None,
            None,
            None,
            None,
            Evidence.unknown("MARGIN_BOUND_UNAVAILABLE", timestamp_ms=timestamp_ms, model_version=model_version),
            reasons=("MARGIN_BOUND_UNAVAILABLE",),
        )
    exposure: dict[str, tuple[Decimal, Decimal]] = {}
    exposure_unknown = False
    approximate = False
    for item in pos:
        symbol = str(_value(item, "symbol", default=""))
        try:
            quantity = _decimal(_value(item, "qty", "quantity", default=0), "quantity", nonnegative=True)
            if quantity <= 0:
                continue
            notional, inexact = _item_notional_result(item, order=False)
            approximate = approximate or inexact
        except (TypeError, ValueError):
            notional = None
        if notional is None:
            exposure_unknown = True
        else:
            exposure.setdefault(symbol, (Decimal("0"), Decimal("0")))
            position_exposure, inexact = _add_up(exposure[symbol][0], notional)
            approximate = approximate or inexact
            exposure[symbol] = (position_exposure, exposure[symbol][1])
    for item in ords:
        symbol = str(_value(item, "symbol", default=""))
        try:
            remaining = _remaining_quantity(item)
            if remaining <= 0:
                continue
            notional, inexact = _item_notional_result(item, order=True)
            approximate = approximate or inexact
        except (TypeError, ValueError):
            notional = None
        if notional is None:
            exposure_unknown = True
        if not bool(_value(item, "reduce_only", default=False)):
            if notional is not None:
                exposure.setdefault(symbol, (Decimal("0"), Decimal("0")))
                order_exposure, inexact = _add_up(exposure[symbol][1], notional)
                approximate = approximate or inexact
                exposure[symbol] = (exposure[symbol][0], order_exposure)

    if exposure_unknown:
        return MarginResult(UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", UNKNOWN, None, None, None, None, None, Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"), reasons=("MARGIN_BOUND_UNAVAILABLE",))

    guard_reason = "MARGIN_BOUND_UNAVAILABLE"
    if not isinstance(model_guard, Evidence) or model_guard.evidence_class not in {OBSERVED, CALCULATED, CONSERVATIVE_BOUND}:
        model_evidence = model_guard if isinstance(model_guard, Evidence) else Evidence.unknown(guard_reason, timestamp_ms=timestamp_ms, model_version=model_version)
        return MarginResult(UNKNOWN, guard_reason, UNKNOWN, None, None, None, None, None, Evidence.unknown(guard_reason, timestamp_ms=timestamp_ms, model_version=model_version), model_guard_evidence=model_evidence, reasons=(guard_reason,))
    model_evidence = model_guard

    leverage_results: dict[str, LeverageResult] = {}
    if leverage is None:
        if not leverage_steps:
            return MarginResult(UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", UNKNOWN, None, None, None, None, None, Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"), reasons=("MARGIN_BOUND_UNAVAILABLE",))
        leverage_results = dict(planned_leverages(exposure, tiers=tiers, leverage_steps=leverage_steps, instrument_max_leverage=instrument_max_leverage))
        if any(item.status != PASS for item in leverage_results.values()):
            reasons = tuple(dict.fromkeys(item.reason or "MARGIN_BOUND_UNAVAILABLE" for item in leverage_results.values() if item.status != PASS))
            return MarginResult(UNKNOWN, reasons[0], UNKNOWN, None, None, None, None, None, Evidence.unknown(reasons[0]), planned_leverage={symbol: item.leverage for symbol, item in leverage_results.items() if item.leverage is not None}, reasons=reasons)
        leverage = {symbol: item.leverage for symbol, item in leverage_results.items() if item.leverage is not None}
    else:
        leverage = {symbol: _decimal(value, "leverage", positive=True) for symbol, value in leverage.items()}
        leverage_reason = _validate_leverage_map(exposure, leverage, tiers=tiers, leverage_steps=leverage_steps, instrument_max_leverage=instrument_max_leverage)
        if leverage_reason is not None:
            return MarginResult(UNKNOWN, leverage_reason, UNKNOWN, None, None, None, None, None, Evidence.unknown(leverage_reason, timestamp_ms=timestamp_ms, model_version=model_version), reasons=(leverage_reason,))

    hair_evidence = _coerce_evidence(collateral_haircut, "MARGIN_BOUND_UNAVAILABLE", timestamp_ms=timestamp_ms, model_version=model_version)
    loss_evidence = _coerce_evidence(order_loss, "MARGIN_BOUND_UNAVAILABLE", timestamp_ms=timestamp_ms, model_version=model_version)
    if hair_evidence.evidence_class == UNKNOWN or loss_evidence.evidence_class == UNKNOWN:
        reason = "MARGIN_BOUND_UNAVAILABLE"
        return MarginResult(UNKNOWN, reason, UNKNOWN, None, None, None, None, None, Evidence.unknown(reason), order_loss_evidence=loss_evidence, collateral_haircut_evidence=hair_evidence, model_guard_evidence=model_evidence, reasons=(reason,))
    haircut = _decimal(hair_evidence.value, "collateral_haircut", nonnegative=True)
    loss = _decimal(loss_evidence.value, "order_loss", nonnegative=True)
    if haircut > 1:
        return MarginResult(UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", UNKNOWN, None, None, None, None, None, Evidence.unknown("MARGIN_BOUND_UNAVAILABLE"), order_loss_evidence=loss_evidence, collateral_haircut_evidence=hair_evidence, model_guard_evidence=model_evidence, reasons=("MARGIN_BOUND_UNAVAILABLE",))

    components: list[MarginComponent] = []
    reasons: list[str] = []
    for item in pos:
        symbol = str(_value(item, "symbol", default=""))
        qty = _decimal(_value(item, "qty", "quantity", default=0), "quantity", nonnegative=True)
        if qty <= 0:
            continue
        price_value = _value(item, "mark_price", "mark", default=None)
        if price_value is None:
            reasons.append("MARGIN_BOUND_UNAVAILABLE")
            continue
        price = _decimal(price_value, "mark_price", positive=True)
        notional, component_inexact = _multiply_up(qty, price)
        approximate = approximate or component_inexact
        try:
            combined_exposure, exposure_inexact = _add_up(exposure[symbol][0], exposure[symbol][1])
            approximate = approximate or exposure_inexact
            tier = _tier_for(symbol, combined_exposure, tiers)
        except (TypeError, ValueError):
            reasons.append("MARGIN_BOUND_UNAVAILABLE")
            continue
        lev = leverage.get(symbol)
        if lev is None:
            reasons.append("MARGIN_BOUND_UNAVAILABLE")
            continue
        close_rate, _ = _fee(fees, False)
        if close_rate is None:
            reasons.append("FEE_RATE_UNKNOWN")
            continue
        close_fee, fee_inexact = _multiply_up(notional, close_rate)
        approximate = approximate or fee_inexact
        leverage_margin, leverage_inexact = _divide_up(notional, lev)
        approximate = approximate or leverage_inexact
        im, im_inexact = _add_up(leverage_margin, close_fee)
        approximate = approximate or im_inexact
        mm_rate = _tier_rate(tier, ("maintenance_margin", "maintenance_margin_rate", "mmr"))
        if mm_rate is None:
            reasons.append("MARGIN_BOUND_UNAVAILABLE")
            continue
        deduction = _tier_rate(tier, ("mm_deduction", "maintenance_deduction", "deduction")) or Decimal("0")
        mm_base, mm_inexact = _multiply_up(notional, mm_rate)
        mm_base, mm_subtract_inexact = _subtract_up(mm_base, deduction)
        mm, mm_add_inexact = _add_up(max(Decimal("0"), mm_base), close_fee)
        component_inexact = component_inexact or fee_inexact or leverage_inexact or im_inexact or mm_inexact or mm_subtract_inexact or mm_add_inexact
        approximate = approximate or component_inexact
        evidence_factory = Evidence.conservative if component_inexact else Evidence.calculated
        components.append(MarginComponent("position", symbol, notional, im, mm, close_fee, evidence_factory(im, denominator=notional, numerator=notional, provenance="m3_margin_v1", timestamp_ms=timestamp_ms, model_version=model_version), {"leverage": lev, "tier": _value(tier, "risk_id", "risk_limit_value", default=None), "close_fee_rate": close_rate}))

    for item in ords:
        if bool(_value(item, "reduce_only", default=False)):
            continue
        symbol = str(_value(item, "symbol", default=""))
        try:
            qty = _remaining_quantity(item)
        except (TypeError, ValueError):
            qty = Decimal("0")
            reasons.append("MARGIN_BOUND_UNAVAILABLE")
        price_value = _value(item, "price", "mark_price", "mark", default=None)
        if qty <= 0:
            continue
        if price_value is None:
            reasons.append("MARGIN_BOUND_UNAVAILABLE")
            continue
        try:
            price = _decimal(price_value, "order price", positive=True)
            combined_exposure, exposure_inexact = _add_up(exposure[symbol][0], exposure[symbol][1])
            approximate = approximate or exposure_inexact
            tier = _tier_for(symbol, combined_exposure, tiers)
        except (TypeError, ValueError):
            reasons.append("MARGIN_BOUND_UNAVAILABLE")
            continue
        lev = leverage.get(symbol)
        if lev is None:
            reasons.append("MARGIN_BOUND_UNAVAILABLE")
            continue
        notional, order_inexact = _multiply_up(qty, price)
        approximate = approximate or order_inexact
        open_rate, _ = _fee(fees, bool(_value(item, "maker", default=False)))
        close_rate, _ = _fee(fees, False)
        if open_rate is None or close_rate is None:
            reasons.append("FEE_RATE_UNKNOWN")
            continue
        open_fee, fee_inexact = _multiply_up(notional, open_rate)
        close_fee, close_fee_inexact = _multiply_up(notional, close_rate)
        leverage_margin, leverage_inexact = _divide_up(notional, lev)
        component_inexact = order_inexact or fee_inexact or close_fee_inexact or leverage_inexact
        approximate = approximate or component_inexact
        im, im_inexact = _add_up(leverage_margin, open_fee)
        im, close_inexact = _add_up(im, close_fee)
        component_inexact = component_inexact or im_inexact or close_inexact
        approximate = approximate or component_inexact
        total_fee, fee_sum_inexact = _add_up(open_fee, close_fee)
        component_inexact = component_inexact or fee_sum_inexact
        approximate = approximate or fee_sum_inexact
        evidence_factory = Evidence.conservative if component_inexact else Evidence.calculated
        components.append(MarginComponent("order", symbol, notional, im, None, total_fee, evidence_factory(im, denominator=notional, numerator=notional, provenance="m3_margin_v1", timestamp_ms=timestamp_ms, model_version=model_version), {"leverage": lev, "tier": _value(tier, "risk_id", "risk_limit_value", default=None), "open_fee_rate": open_rate, "close_fee_rate": close_rate}))

    if reasons:
        reason = reasons[0]
        return MarginResult(UNKNOWN, reason, UNKNOWN, None, None, None, None, None, Evidence.unknown(reason, timestamp_ms=timestamp_ms, model_version=model_version), tuple(components), planned_leverage=dict(leverage), fee_evidence=_fee_evidence(fees, timestamp_ms=timestamp_ms, model_version=model_version), order_loss_evidence=loss_evidence, collateral_haircut_evidence=hair_evidence, model_guard_evidence=model_evidence, reasons=tuple(dict.fromkeys(reasons)))

    position_im = Decimal("0")
    order_im = Decimal("0")
    position_mm = Decimal("0")
    for item in components:
        if item.kind == "position":
            position_im, inexact = _add_up(position_im, item.initial_margin or Decimal("0"))
            approximate = approximate or inexact
            position_mm, inexact = _add_up(position_mm, item.maintenance_margin or Decimal("0"))
            approximate = approximate or inexact
        else:
            order_im, inexact = _add_up(order_im, item.initial_margin or Decimal("0"))
            approximate = approximate or inexact
    total_im, inexact = _add_up(position_im, order_im)
    approximate = approximate or inexact
    collateral = None if margin_balance is None else _decimal(margin_balance, "margin_balance")
    if collateral is None:
        denominator = Evidence.unknown("EQUITY_DENOMINATOR_INVALID", timestamp_ms=timestamp_ms, model_version=model_version)
        return MarginResult(UNKNOWN, "EQUITY_DENOMINATOR_INVALID", UNKNOWN, None, None, None, None, None, denominator, tuple(components), planned_leverage=dict(leverage), fee_evidence=_fee_evidence(fees, timestamp_ms=timestamp_ms, model_version=model_version), order_loss_evidence=loss_evidence, collateral_haircut_evidence=hair_evidence, model_guard_evidence=model_evidence, reasons=("EQUITY_DENOMINATOR_INVALID",))
    denominator_factor, denominator_inexact = _subtract_down(Decimal("1"), haircut)
    collateral_value, collateral_inexact = _multiply_down(collateral, denominator_factor)
    denominator_value, loss_inexact = _subtract_down(collateral_value, loss)
    approximate = approximate or denominator_inexact or collateral_inexact or loss_inexact
    denominator_factory = Evidence.conservative if approximate else Evidence.calculated
    denominator = denominator_factory(denominator_value, denominator=collateral, numerator=total_im, provenance="margin_balance_haircut_order_loss", timestamp_ms=timestamp_ms, model_version=model_version)
    if denominator_value <= 0:
        denominator = Evidence.unknown("EQUITY_DENOMINATOR_INVALID", denominator=collateral, provenance="margin_balance_haircut_order_loss", timestamp_ms=timestamp_ms, model_version=model_version)
        return MarginResult(UNKNOWN, "EQUITY_DENOMINATOR_INVALID", UNKNOWN, None, None, None, None, None, denominator, tuple(components), planned_leverage=dict(leverage), fee_evidence=_fee_evidence(fees, timestamp_ms=timestamp_ms, model_version=model_version), order_loss_evidence=loss_evidence, collateral_haircut_evidence=hair_evidence, model_guard_evidence=model_evidence, reasons=("EQUITY_DENOMINATOR_INVALID",))
    if denominator_value < total_im or denominator_value < position_mm:
        return MarginResult(
            status=FAIL,
            reason="MARGIN_BOUND_FAILED",
            evidence_class=CONSERVATIVE_BOUND if approximate else CALCULATED,
            position_im=position_im,
            order_im=order_im,
            total_im=total_im,
            position_mm=position_mm,
            total_mm=position_mm,
            denominator=denominator,
            components=tuple(components),
            witness={"total_im": total_im, "total_mm": position_mm, "denominator": denominator_value},
            planned_leverage=dict(leverage),
            fee_evidence=_fee_evidence(fees, timestamp_ms=timestamp_ms, model_version=model_version),
            order_loss_evidence=loss_evidence,
            collateral_haircut_evidence=hair_evidence,
            model_guard_evidence=model_evidence,
            reasons=("MARGIN_BOUND_FAILED",),
        )
    evidence_class = CONSERVATIVE_BOUND if approximate else CALCULATED
    return MarginResult(PASS, None, evidence_class, position_im, order_im, total_im, position_mm, position_mm, denominator, tuple(components), {"total_im": total_im, "total_mm": position_mm, "denominator": denominator_value}, dict(leverage), fallback_used=False, fee_evidence=_fee_evidence(fees, timestamp_ms=timestamp_ms, model_version=model_version), order_loss_evidence=loss_evidence, collateral_haircut_evidence=hair_evidence, model_guard_evidence=model_evidence)


def _coerce_evidence(value: Any, missing_reason: str, *, timestamp_ms: int | None = None, model_version: str = "m3_margin_v1") -> Evidence:
    if value is None:
        return Evidence.unknown(missing_reason, timestamp_ms=timestamp_ms, model_version=model_version)
    if isinstance(value, Evidence):
        return value
    return Evidence.calculated(_decimal(value, missing_reason, nonnegative=True), provenance="caller_supplied", timestamp_ms=timestamp_ms, model_version=model_version)


@_context_safe
def evaluate_limiter(
    positions: Iterable[Any] = (),
    orders: Iterable[Any] = (),
    *,
    live_orders: Iterable[Any] | None = None,
    proposed_openings: Iterable[Any] | None = None,
    limit: int | None = None,
    events: Iterable[Any] = (),
    pending_cancels: Iterable[Any] = (),
) -> LimiterResult:
    """Evaluate limiter occupancy and opening reservations.

    ``limit=None`` is the explicit unlimited mode.  A numeric ``limit`` is a
    hard cap, so ``limit=0`` permits no counted openings; priority-zero slots
    remain exempt from the counted cap.
    """
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("limit must be a non-negative integer or None")
    positions = tuple(positions)
    legacy_orders = tuple(orders)
    live = tuple(live_orders) if live_orders is not None else ()
    proposed = tuple(proposed_openings) if proposed_openings is not None else legacy_orders
    pending_items = tuple(pending_cancels)
    if any(_state_invalid(item, order=False) for item in positions) or any(_order_state_invalid(item) for item in (*live, *proposed, *pending_items)):
        return LimiterResult(UNKNOWN, limit, 0, 0, (), (), (), (), 0, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", {"state_unconfirmed_or_inactive": True})
    quantities: dict[str, Decimal] = {}
    source_slots: set[str] = set()
    priority_values: dict[str, set[int]] = {}
    unresolved_priorities: set[str] = set()
    missing_priorities: set[str] = set()
    invalid_priorities: set[str] = set()
    conflicting_priorities: set[str] = set()

    def record_priority(item: Any, slot: str, *, position: bool) -> None:
        source_slots.add(slot)
        present, priority, state = _priority_state(item)
        if state == _PRIORITY_INVALID:
            invalid_priorities.add(slot)
            unresolved_priorities.add(slot)
            return
        if state == _PRIORITY_CONFLICTING:
            conflicting_priorities.add(slot)
            unresolved_priorities.add(slot)
            return
        if position and (not present or priority is None):
            missing_priorities.add(slot)
            unresolved_priorities.add(slot)
            return
        if priority is not None:
            priority_values.setdefault(slot, set()).add(priority)

    for item in positions:
        slot = _pair_slot(item)
        qty = _decimal(_value(item, "qty", "quantity", default=0), "position quantity", nonnegative=True)
        quantities[slot] = quantities.get(slot, Decimal("0")) + qty
        record_priority(item, slot, position=True)
    for event in events:
        slot = _pair_slot(event)
        delta = _value(event, "qty_delta", "signed_quantity", "delta", default=None)
        if delta is None:
            qty = _decimal(_value(event, "qty", "quantity", default=0), "event quantity", nonnegative=True)
            delta = -qty if str(_value(event, "action", "kind", default="")).lower() in {"close", "reduce", "sell_close"} else qty
        quantities[slot] = max(Decimal("0"), quantities.get(slot, Decimal("0")) + _decimal(delta, "event delta"))
        record_priority(event, slot, position=False)
    for item in (*live, *proposed):
        slot = _pair_slot(item)
        record_priority(item, slot, position=False)
    for item in pending_items:
        slot = _pair_slot(item)
        record_priority(item, slot, position=False)

    priorities: dict[str, int | None] = {}
    for slot in sorted(source_slots):
        values = priority_values.get(slot, set())
        if len(values) > 1:
            conflicting_priorities.add(slot)
            unresolved_priorities.add(slot)
        if slot in unresolved_priorities:
            priorities[slot] = None
        elif len(values) == 1:
            priorities[slot] = next(iter(values))
        else:
            priorities[slot] = None
            missing_priorities.add(slot)
            unresolved_priorities.add(slot)

    unknown = tuple(sorted(slot for slot, priority in priorities.items() if priority is None))
    if unresolved_priorities or unknown:
        witness = {
            "unknown_priority": unknown,
            "missing_priority": tuple(sorted(missing_priorities)),
            "invalid_priority": tuple(sorted(invalid_priorities)),
            "conflicting_priority": tuple(sorted(conflicting_priorities)),
        }
        return LimiterResult(UNKNOWN, limit, 0, 0, (), (), (), (), 0, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", witness)
    nonflat = tuple(sorted(slot for slot, qty in quantities.items() if qty > 0 and priorities.get(slot, 1) != 0))
    exempt = tuple(sorted(slot for slot, qty in quantities.items() if qty > 0 and priorities.get(slot, 1) == 0))
    pending_set = {slot for item in pending_items if (slot := _pair_slot(item)) in priorities and priorities[slot] != 0}
    for item in (*live, *proposed):
        if priorities.get(_pair_slot(item), 1) != 0 and bool(_value(item, "cancel_requested", default=False)) and not bool(_value(item, "cancel_confirmed", default=False)):
            pending_set.add(_pair_slot(item))
    pending = tuple(sorted(pending_set))

    def opening_slots(items: tuple[Any, ...]) -> tuple[str, ...] | None:
        openings_list: list[str] = []
        for item in items:
            if bool(_value(item, "reduce_only", default=False)) or bool(_value(item, "cancel_confirmed", default=False)):
                continue
            try:
                remaining = _remaining_quantity(item)
            except (TypeError, ValueError):
                return None
            if remaining > 0:
                openings_list.append(_pair_slot(item))
        return tuple(sorted(set(openings_list)))

    live_openings = opening_slots(live)
    proposed_openings_set = opening_slots(proposed)
    if live_openings is None or proposed_openings_set is None:
        return LimiterResult(UNKNOWN, limit, 0, 0, (), (), (), pending, 0, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", {"invalid_order": True})
    live_set = set(live_openings)
    proposed_set = set(proposed_openings_set)
    live_counted_openings = {slot for slot in live_set if slot not in nonflat and priorities.get(slot, 1) != 0}
    if limit is not None and len(nonflat) + len(live_counted_openings) > limit:
        return LimiterResult(UNKNOWN, limit, len(nonflat), len(exempt), nonflat, exempt, (), pending, len(nonflat) + len(live_counted_openings), UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", {"live_openings": tuple(sorted(live_set)), "live_over_limit": True})
    proposed_counted_openings = {slot for slot in proposed_set if slot not in nonflat and slot not in live_counted_openings and priorities.get(slot, 1) != 0}
    all_counted_openings = live_counted_openings | proposed_counted_openings
    existing = {slot for slot in live_set | proposed_set if slot in nonflat}
    exempt_openings = {slot for slot in live_set | proposed_set if priorities.get(slot, 1) == 0}
    candidates = tuple(sorted(proposed_counted_openings, key=lambda slot: (priorities.get(slot, 1), slot)))
    if limit is None:
        allowed_counted = candidates
        reserved = ()
    else:
        capacity = max(0, limit - len(nonflat) - len(live_counted_openings))
        allowed_counted = candidates[:capacity]
        reserved = candidates[capacity:]
    allowed = tuple(sorted(existing | live_counted_openings | exempt_openings | set(allowed_counted)))
    race_max = len(nonflat) + len(all_counted_openings)
    return LimiterResult(PASS, limit, len(nonflat), len(exempt), nonflat, exempt, allowed, pending, race_max, CALCULATED, witness={"count_before": len(nonflat), "pending_cancels_confirmed": not pending, "live_openings": tuple(sorted(live_set)), "proposed_openings": tuple(sorted(proposed_set)), "race_counted_openings": tuple(sorted(all_counted_openings))}, reserved_openings=reserved)


def _conservative_evidence(evidence: Evidence, *, numerator: Any = None) -> Evidence:
    if evidence.evidence_class == UNKNOWN or evidence.value is None:
        return evidence
    return Evidence.conservative(
        evidence.value,
        denominator=evidence.denominator,
        provenance=evidence.provenance,
        reason=evidence.reason,
        timestamp_ms=evidence.timestamp_ms,
        model_version=evidence.model_version,
        numerator=numerator,
    )


@_context_safe
def evaluate_margin_envelope(
    states: Sequence[Any],
    evaluator: Callable[[Any], MarginResult],
    *,
    enumeration_limit: int,
    bound_builder: Callable[[Sequence[Any]], MarginResult] | None = None,
) -> EnvelopeResult:
    if type(enumeration_limit) is not int or enumeration_limit <= 0:
        raise ValueError("enumeration_limit must be positive")
    if len(states) <= enumeration_limit:
        try:
            results = tuple(evaluator(state) for state in states)
        except Exception:
            return EnvelopeResult(UNKNOWN, None, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", 0)
        if not results:
            return EnvelopeResult(UNKNOWN, None, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", 0)
        if not all(isinstance(item, MarginResult) for item in results):
            return EnvelopeResult(UNKNOWN, None, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", len(results))
        invalid = tuple(item for item in results if item.status != PASS or item.total_im is None or item.total_mm is None or not isinstance(item.denominator, Evidence) or item.denominator.evidence_class == UNKNOWN or item.denominator.value is None)
        if invalid:
            reason = next((item.reason for item in invalid if item.reason), "MARGIN_BOUND_UNAVAILABLE")
            return EnvelopeResult(UNKNOWN, None, UNKNOWN, reason, len(results), {"invalid_states": len(invalid)})
        try:
            metrics = {
                "position_im": tuple(_decimal(item.position_im, "position_im", nonnegative=True) for item in results),
                "order_im": tuple(_decimal(item.order_im, "order_im", nonnegative=True) for item in results),
                "total_im": tuple(_decimal(item.total_im, "total_im", nonnegative=True) for item in results),
                "position_mm": tuple(_decimal(item.position_mm, "position_mm", nonnegative=True) for item in results),
                "total_mm": tuple(_decimal(item.total_mm, "total_mm", nonnegative=True) for item in results),
                "denominator": tuple(_decimal(item.denominator.value, "denominator", nonnegative=True) for item in results),
            }
        except (TypeError, ValueError):
            return EnvelopeResult(UNKNOWN, None, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", len(results))
        im_witness = max(range(len(results)), key=lambda index: metrics["total_im"][index])
        mm_witness = max(range(len(results)), key=lambda index: metrics["total_mm"][index])
        denominator_witness = min(range(len(results)), key=lambda index: metrics["denominator"][index])
        base = results[im_witness]
        max_total_im = max(metrics["total_im"])
        max_total_mm = max(metrics["total_mm"])
        min_denominator = metrics["denominator"][denominator_witness]
        witness = {
            **base.witness,
            "im_source": im_witness,
            "mm_source": mm_witness,
            "denominator_source": denominator_witness,
            "im_witness": im_witness,
            "mm_witness": mm_witness,
            "denominator_witness": denominator_witness,
            "im_source_evidence": results[im_witness].evidence_class,
            "mm_source_evidence": results[mm_witness].evidence_class,
            "denominator_source_evidence": results[denominator_witness].denominator,
        }
        composed = len({im_witness, mm_witness, denominator_witness}) > 1
        evidence_class = CONSERVATIVE_BOUND if composed or any(item.evidence_class == CONSERVATIVE_BOUND for item in results) else base.evidence_class
        im_position = results[im_witness].position_im
        im_order = results[im_witness].order_im
        composed_im, _ = _add_up(im_position, im_order)
        if composed_im != max_total_im:
            im_position, im_order = max_total_im, Decimal("0")
        denominator = _conservative_evidence(results[denominator_witness].denominator, numerator=max_total_im)
        witness.update({"total_im": max_total_im, "total_mm": max_total_mm, "denominator": min_denominator})
        envelope = replace(
            base,
            evidence_class=evidence_class,
            position_im=im_position,
            order_im=im_order,
            total_im=max_total_im,
            position_mm=max(metrics["position_mm"]),
            total_mm=max_total_mm,
            denominator=denominator,
            components=(),
            planned_leverage={},
            fallback_used=False,
            reasons=(),
            fee_evidence=_conservative_evidence(base.fee_evidence, numerator=max_total_im),
            order_loss_evidence=_conservative_evidence(base.order_loss_evidence, numerator=max_total_im),
            collateral_haircut_evidence=_conservative_evidence(base.collateral_haircut_evidence, numerator=max_total_im),
            model_guard_evidence=_conservative_evidence(base.model_guard_evidence, numerator=max_total_im),
            all_executable=False,
            witness=witness,
        )
        witness["denominator_source_evidence"] = denominator
        envelope = replace(envelope, witness=witness)
        if min_denominator < max_total_im or min_denominator < max_total_mm:
            envelope = replace(envelope, status=FAIL, reason="MARGIN_BOUND_FAILED", reasons=("MARGIN_BOUND_FAILED",))
            return EnvelopeResult(FAIL, envelope, evidence_class, "MARGIN_BOUND_FAILED", len(results), witness)
        return EnvelopeResult(PASS, envelope, evidence_class, None, len(results), witness)
    if bound_builder is None:
        return EnvelopeResult(UNKNOWN, None, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", 0, {"enumeration_limit": enumeration_limit, "state_count": len(states)})
    try:
        bound = bound_builder(states)
    except Exception:
        return EnvelopeResult(UNKNOWN, None, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", 0, {"enumeration_limit": enumeration_limit, "state_count": len(states)})
    if (
        not isinstance(bound, MarginResult)
        or bound.all_executable is not True
        or bound.status != PASS
        or bound.total_im is None
        or bound.total_mm is None
        or not isinstance(bound.denominator, Evidence)
        or bound.denominator.evidence_class == UNKNOWN
        or bound.denominator.value is None
    ):
        reason = bound.reason if isinstance(bound, MarginResult) and bound.reason else "MARGIN_BOUND_UNAVAILABLE"
        return EnvelopeResult(UNKNOWN, None, UNKNOWN, reason, 0, {"enumeration_limit": enumeration_limit, "state_count": len(states)})
    try:
        bound_im = _decimal(bound.total_im, "total_im", nonnegative=True)
        bound_mm = _decimal(bound.total_mm, "total_mm", nonnegative=True)
        bound_denominator = _decimal(bound.denominator.value, "denominator", nonnegative=True)
    except (TypeError, ValueError):
        return EnvelopeResult(UNKNOWN, None, UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", 0, {"enumeration_limit": enumeration_limit, "state_count": len(states)})
    bound_witness = {**bound.witness, "sufficiency_checked": True, "total_im": bound_im, "total_mm": bound_mm, "denominator": bound_denominator}
    if bound_denominator < bound_im or bound_denominator < bound_mm:
        failed = replace(
            bound,
            status=FAIL,
            reason="MARGIN_BOUND_FAILED",
            evidence_class=CONSERVATIVE_BOUND,
            witness=bound_witness,
            reasons=tuple(dict.fromkeys((*bound.reasons, "MARGIN_BOUND_FAILED"))),
        )
        return EnvelopeResult(FAIL, failed, CONSERVATIVE_BOUND, "MARGIN_BOUND_FAILED", 0, failed.witness)
    denominator = bound.denominator
    if denominator.evidence_class != UNKNOWN:
        denominator = replace(denominator, evidence_class=CONSERVATIVE_BOUND)
    components = tuple(
        replace(component, evidence=replace(component.evidence, evidence_class=CONSERVATIVE_BOUND))
        if component.evidence.evidence_class != UNKNOWN else component
        for component in bound.components
    )
    bounded = replace(
        bound,
        evidence_class=CONSERVATIVE_BOUND,
        fallback_used=True,
        denominator=denominator,
        components=components,
        witness={**bound_witness, "enumeration_limit": enumeration_limit, "covered_states": len(states), "all_executable": True},
        reasons=tuple(dict.fromkeys((*bound.reasons, "ENUMERATION_FALLBACK_USED"))),
    )
    return EnvelopeResult(PASS, bounded, CONSERVATIVE_BOUND, "ENUMERATION_FALLBACK_USED", 0, bounded.witness)


@_context_safe
def deposit_sufficiency(
    deposit: Decimal | int | str,
    *,
    total_im: Decimal | int | str,
    total_mm: Decimal | int | str,
    min_free_margin_reserve_pct: Decimal | int | str,
    max_mm_load_pct: Decimal | int | str,
    timestamp_ms: int | None = None,
    model_version: str = "m3_deposit_v1",
) -> DepositResult:
    d = _decimal(deposit, "deposit", nonnegative=True)
    im = _decimal(total_im, "total_im", nonnegative=True)
    mm = _decimal(total_mm, "total_mm", nonnegative=True)
    reserve, _ = _divide_up(_decimal(min_free_margin_reserve_pct, "reserve", nonnegative=True), Decimal("100"))
    mm_load, _ = _divide_up(_decimal(max_mm_load_pct, "mm_load", nonnegative=True), Decimal("100"))
    if reserve >= 1 or mm_load <= 0:
        return DepositResult(UNKNOWN, "EQUITY_DENOMINATOR_INVALID", d, im, mm, None, Evidence.unknown("EQUITY_DENOMINATOR_INVALID", denominator=d, timestamp_ms=timestamp_ms, model_version=model_version))
    minimum_im, _ = _divide_up(im, Decimal("1") - reserve)
    minimum_mm, _ = _divide_up(mm, mm_load)
    minimum = max(minimum_im, minimum_mm)
    status = PASS if d >= minimum else FAIL
    reason = None if status == PASS else "MARGIN_BOUND_FAILED"
    denominator = Evidence.calculated(d, denominator=d, numerator=max(im, mm), provenance="fixed_absolute_size_deposit", timestamp_ms=timestamp_ms, model_version=model_version)
    return DepositResult(status, reason, d, im, mm, minimum, denominator)
