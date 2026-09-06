"""Pure, fixture-only M4 portfolio composition and sizing search."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, is_dataclass
from decimal import Decimal, InvalidOperation, MAX_EMAX, MIN_EMIN, ROUND_DOWN, localcontext
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .canonical import PORTFOLIO_REASON_V2
from .disposition import primary_reason, status_for_reasons


PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
OPEN_POLICY = "OPEN_POLICY"
REASON_ENUM_VERSION = "portfolio_reason_v2"
_IDENTITY_MISSING = object()


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _first(value: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        found = _get(value, key, None)
        if found is not None:
            return found
    return default


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


@contextmanager
def _safe_context(precision: int):
    with localcontext() as ctx:
        ctx.prec = max(80, precision)
        ctx.rounding = ROUND_DOWN
        ctx.Emax = MAX_EMAX
        ctx.Emin = MIN_EMIN
        for signal in ctx.traps:
            ctx.traps[signal] = False
        yield ctx


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return deepcopy(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return value


def _typed(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("typed identity requires finite Decimals")
        return {"__decimal__": format(value, "f")}
    if isinstance(value, str):
        return {"__string__": value}
    if isinstance(value, bool):
        return {"__bool__": value}
    if value is None:
        return {"__null__": True}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("typed identity keys must be strings")
        return {key: _typed(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_typed(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise TypeError("typed identity does not support unordered sets")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("typed identity requires finite floats")
        return {"__float__": repr(value)}
    if isinstance(value, int):
        return {"__int__": repr(value)}
    if is_dataclass(value):
        return _typed(vars(value))
    raise TypeError(f"unsupported typed identity value: {type(value).__name__}")


def _typed_json(value: Any) -> str:
    return json.dumps(_typed(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_typed_json(value).encode("utf-8")).hexdigest()


def _stable(reason: str | None, fallback: str = "VALIDATION_FAILED") -> str:
    if reason is None:
        if fallback not in PORTFOLIO_REASON_V2:
            raise ValueError(f"unknown portfolio reason: {fallback}")
        return fallback
    if reason not in PORTFOLIO_REASON_V2:
        raise ValueError(f"unknown portfolio reason: {reason}")
    return reason


def canonical_candidate_identity(candidate: Any) -> str:
    return _digest(candidate)


@dataclass(frozen=True)
class PairSlot:
    symbol: str
    long: Mapping[str, Any] | None = None
    short: Mapping[str, Any] | None = None
    structural_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "long", _freeze(self.long) if self.long is not None else None)
        object.__setattr__(self, "short", _freeze(self.short) if self.short is not None else None)

    @property
    def directions(self) -> tuple[str, ...]:
        return tuple(side for side, candidate in (("LONG", self.long), ("SHORT", self.short)) if candidate is not None)

    @property
    def composition(self) -> str:
        return "+".join(self.directions)

    @property
    def long_candidate(self) -> Mapping[str, Any] | None:
        return self.long

    @property
    def short_candidate(self) -> Mapping[str, Any] | None:
        return self.short


@dataclass(frozen=True)
class DirectionalSize:
    side: str
    scalar: Decimal
    quantity: Decimal | None
    orders: tuple[Mapping[str, Any], ...]
    geometry: Any
    lot_x: Any
    rejected: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "orders", tuple(_freeze(order) for order in self.orders))
        object.__setattr__(self, "geometry", _freeze(self.geometry))
        object.__setattr__(self, "lot_x", _freeze(self.lot_x))


@dataclass(frozen=True)
class SchedulingKey:
    label: str
    ratio: Decimal | None
    numerator: Decimal | None
    denominator: Decimal | None
    identity: str


@dataclass(frozen=True)
class Variant:
    scalar: Decimal
    slot: PairSlot
    directions: Mapping[str, DirectionalSize]
    status: str
    reason: str | None = None
    detail: str | None = None
    gate: str | None = None
    limiter: int = 0
    priority: int = 1
    scheduling_key: SchedulingKey | None = None
    reason_enum_version: str = REASON_ENUM_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "directions", MappingProxyType(dict(self.directions)))
        if self.reason is not None and self.reason not in PORTFOLIO_REASON_V2:
            object.__setattr__(self, "reason", _stable(self.reason))

    @property
    def composition(self) -> str:
        return self.slot.composition


@dataclass(frozen=True)
class ExcludedVariant:
    symbol: str
    composition: str
    scalar: Decimal
    priority: int
    reason: str
    detail: str
    reason_enum_version: str = REASON_ENUM_VERSION
    evidence_class: str | None = None
    variant_identity: str = ""

    def __post_init__(self) -> None:
        if self.reason not in PORTFOLIO_REASON_V2:
            object.__setattr__(self, "reason", _stable(self.reason))


@dataclass(frozen=True)
class SearchResult:
    status: str
    reason: str | None = None
    reasons: tuple[str, ...] = ()
    detail: str | None = None
    slots: tuple[PairSlot, ...] = ()
    tried: tuple[Variant, ...] = ()
    passing: tuple[Variant, ...] = ()
    excluded: tuple[ExcludedVariant, ...] = ()
    maximum: Variant | None = None
    maximum_by_symbol: Mapping[str, Variant] = ()
    seed: Variant | None = None
    order: tuple[str, ...] = ()
    scheduling_key: SchedulingKey | None = None
    scheduling_order: tuple[str, ...] = ()
    ranking_policy: Mapping[str, Any] | None = None
    campaign_identity: str | None = None
    priority: int = 1
    limiter: int = 0
    exhausted: bool = False
    reason_enum_version: str = REASON_ENUM_VERSION
    grid_version: str = "m4-grid-v1"

    def __post_init__(self) -> None:
        if self.ranking_policy is not None:
            object.__setattr__(self, "ranking_policy", _freeze(self.ranking_policy))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "maximum_by_symbol", MappingProxyType(dict(self.maximum_by_symbol)))
        if any(reason not in PORTFOLIO_REASON_V2 for reason in self.reasons):
            raise ValueError("unknown portfolio reason in reasons")
        if self.reason is not None and self.reason not in PORTFOLIO_REASON_V2:
            raise ValueError(f"unknown portfolio reason: {self.reason}")

    def maximum_for_symbol(self, symbol: str) -> Variant | None:
        return self.maximum_by_symbol.get(symbol)


@dataclass(frozen=True)
class IdentityResult:
    new_evaluation: bool
    new_trading_run: bool
    detail: str


PortfolioCandidate = Variant


def enumerate_compositions(slot: PairSlot) -> tuple[PairSlot, ...]:
    choices: list[PairSlot] = []
    if slot.long is not None:
        choices.append(PairSlot(slot.symbol, slot.long, None, slot.structural_reason))
    if slot.short is not None:
        choices.append(PairSlot(slot.symbol, None, slot.short, slot.structural_reason))
    if slot.long is not None and slot.short is not None:
        choices.append(slot)
    return tuple(choices)


def _valid_identity(candidate: Any) -> bool:
    if _first(candidate, "status", "user_status") != "FINALIST":
        return False
    side = str(_get(candidate, "side", "")).upper()
    strategy = _first(candidate, "strategy_id", "strategyId", "strategy")
    if isinstance(strategy, Mapping):
        strategy = _first(strategy, "id", "strategy_id", "strategyId")
    result = _first(candidate, "result_id", "resultId")
    if result is None:
        nested = _get(candidate, "result")
        result = _first(nested, "id", "result_id", default=nested if isinstance(nested, str) else None)
    return side in {"LONG", "SHORT"} and all(value not in (None, "") and not isinstance(value, Mapping) for value in (_get(candidate, "symbol"), strategy, result))


def build_pair_slots(candidates: Sequence[Any]) -> tuple[PairSlot, ...]:
    groups: dict[str, list[Any]] = {}
    for candidate in candidates:
        if _valid_identity(candidate):
            groups.setdefault(str(_get(candidate, "symbol")), []).append(deepcopy(candidate))
    result: list[PairSlot] = []
    for symbol in sorted(groups):
        by_side: dict[str, Any] = {}
        reason = None
        members = sorted(groups[symbol], key=lambda value: (str(_get(value, "side")).upper(), canonical_candidate_identity(value)))
        for member in members:
            side = str(_get(member, "side")).upper()
            if side in by_side:
                reason = "DUPLICATE_DIRECTIONAL_CANDIDATE"
            else:
                by_side[side] = member
        result.append(PairSlot(symbol, by_side.get("LONG"), by_side.get("SHORT"), reason))
    return tuple(result)


def _capability_ok(capability: Any, slot: PairSlot, opposite_policy: Any) -> tuple[bool, str, str]:
    if not isinstance(capability, Mapping):
        return False, "VALIDATION_FAILED", "CAPABILITY_UNSUPPORTED"
    for keys in (("dedicated_close", "dedicated_close_capability"), ("opposite_opening", "opposite_opening_capability")):
        if not any(capability.get(key) is True for key in keys):
            return False, "VALIDATION_FAILED", "CAPABILITY_UNSUPPORTED"
    if opposite_policy in (None, "", "UNKNOWN"):
        return False, "VALIDATION_FAILED", "OPPOSITE_POLICY_UNSUPPORTED"
    if slot.long is not None and slot.short is not None:
        long_tf = _first(slot.long, "timeframe", "tf", "time_frame")
        short_tf = _first(slot.short, "timeframe", "tf", "time_frame")
        if long_tf != short_tf and capability.get("dual_tf") is not True:
            return False, "VALIDATION_FAILED", "MIXED_TIMEFRAME_UNSUPPORTED"
        common = capability.get("common_runtime_fields", capability.get("runtime_fields"))
        if common is True:
            common = ()
        if isinstance(common, Mapping):
            common = tuple(common)
        if not isinstance(common, (list, tuple)):
            return False, "VALIDATION_FAILED", "COMMON_RUNTIME_FIELDS_UNAVAILABLE"
        for key in common:
            left = _get(slot.long, "runtime", {}) or {}
            right = _get(slot.short, "runtime", {}) or {}
            if not isinstance(left, Mapping) or not isinstance(right, Mapping) or key not in left or key not in right:
                return False, "VALIDATION_FAILED", "RUNTIME_FIELD_UNAVAILABLE"
            if left[key] != right[key]:
                return False, "VALIDATION_FAILED", "RUNTIME_FIELD_CONFLICT"
    return True, "", ""


def _equity_facts(value: Any) -> tuple[Decimal | None, str | None, int | None, int | None]:
    if isinstance(value, Mapping) and isinstance(value.get("current_portfolio_equity"), Mapping):
        value = value["current_portfolio_equity"]
    amount = _first(value, "amount", "equity", "value")
    currency = _first(value, "currency", "settlement_currency")
    timestamp = _first(value, "timestamp_ms", "timestamp")
    expiry = _first(value, "expires_at_ms", "expiry_ms")
    try:
        timestamp = int(timestamp) if timestamp is not None else None
        expiry = int(expiry) if expiry is not None else None
    except (TypeError, ValueError):
        timestamp = expiry = None
    return _decimal(amount), currency, timestamp, expiry


def _dd_facts(candidate: Any) -> tuple[Decimal | None, str | None, int | None, int | None]:
    nested = _first(candidate, "individual_dd", "dd", default=None)
    source = nested if isinstance(nested, Mapping) else candidate
    value = _first(source, "d100", "D100", "amount", "value", "max_drawdown_amount")
    currency = _first(source, "d100_currency", "currency", default=_first(candidate, "d100_currency"))
    timestamp = _first(source, "d100_timestamp_ms", "timestamp_ms", "timestamp")
    expiry = _first(source, "d100_expires_at_ms", "expires_at_ms", "expiry_ms")
    try:
        timestamp = int(timestamp) if timestamp is not None else None
        expiry = int(expiry) if expiry is not None else None
    except (TypeError, ValueError):
        timestamp = expiry = None
    return _decimal(value), currency, timestamp, expiry


def _fresh(timestamp: int | None, expiry: int | None, now_ms: int | None) -> bool:
    return timestamp is not None and expiry is not None and now_ms is not None and timestamp <= now_ms <= expiry


def _limit_parts(candidate: Any, current_equity: Any, dd_cap_pct: Any, now_ms: int | None) -> tuple[Any, ...]:
    liquidity = _decimal(_first(candidate, "liquidity_scalar_pct_max", "liquidity_ceiling", "liquidity_max"))
    margin = _decimal(_first(candidate, "margin_scalar_pct_max", "margin_ceiling", "margin_max"))
    exchange = _decimal(_first(candidate, "exchange_scalar_pct_max", "exchange_ceiling", "exchange_max"))
    if liquidity is None or liquidity < 0:
        return None, "LIQUIDITY_MISSING", None, None, None, "LIQUIDITY_CEILING_UNAVAILABLE"
    if margin is None or exchange is None or margin < 0 or exchange < 0:
        return True, "MARGIN_BOUND_UNAVAILABLE", liquidity, margin, exchange, "MARGIN_OR_EXCHANGE_CEILING_UNAVAILABLE"
    if dd_cap_pct is None:
        return True, OPEN_POLICY, liquidity, margin, exchange, "INDIVIDUAL_DD_CAP_OPEN_POLICY"
    cap = _decimal(dd_cap_pct)
    if cap is None:
        return True, "INDIVIDUAL_DD_UNAVAILABLE", liquidity, margin, exchange, "DD_CAP_INVALID"
    d100, dd_currency, dd_timestamp, dd_expiry = _dd_facts(candidate)
    equity, equity_currency, equity_timestamp, equity_expiry = _equity_facts(current_equity)
    if cap < 0 or d100 is None or d100 <= 0 or equity is None or equity <= 0 or not dd_currency or not equity_currency:
        return True, "INDIVIDUAL_DD_UNAVAILABLE", liquidity, margin, exchange, "DD_FACT_INVALID"
    if dd_currency != equity_currency or not _fresh(dd_timestamp, dd_expiry, now_ms) or not _fresh(equity_timestamp, equity_expiry, now_ms):
        return True, "INDIVIDUAL_DD_UNAVAILABLE", liquidity, margin, exchange, "DD_FACT_STALE_OR_UNFRESH"
    with _safe_context(len(str(cap)) + len(str(equity)) + len(str(d100)) + 20):
        dd_limit = cap * equity / d100
    if not dd_limit.is_finite():
        return True, "INDIVIDUAL_DD_UNAVAILABLE", liquidity, margin, exchange, "DD_LIMIT_NONFINITE"
    return True, None, liquidity, margin, exchange, dd_limit


def _round_down(value: Decimal, step: Decimal) -> Decimal | None:
    if not value.is_finite() or not step.is_finite() or step <= 0:
        return None
    with _safe_context(len(value.as_tuple().digits) + len(step.as_tuple().digits) + 30):
        result = (value // step) * step
    return result if result.is_finite() else None


def _size(candidate: Any, side: str, scalar: Decimal) -> DirectionalSize:
    rejected: list[str] = []
    if _first(candidate, "geometry_valid", "immutable_geometry_valid", default=True) is not True:
        rejected.append("POST_ROUNDING_GEOMETRY")
    output: list[Mapping[str, Any]] = []
    base_quantity: Decimal | None = None
    original_orders = _first(candidate, "orders", "opening_orders", default=()) or ()
    for index, original in enumerate(original_orders):
        original_copy = _thaw(original)
        quantity = _decimal(_first(original, "quantity", "qty", "size"))
        step = _decimal(_first(original, "qty_step", "quantity_step", default=_first(candidate, "qty_step", "quantity_step")))
        if quantity is None or step is None or step <= 0:
            output.append(original_copy)
            rejected.append(f"ORDER_{index}_INVALID" if step is None or quantity is None else f"ORDER_{index}_STEP_INVALID")
            continue
        with _safe_context(len(quantity.as_tuple().digits) + len(scalar.as_tuple().digits) + len(step.as_tuple().digits) + 30):
            scaled = quantity * scalar / Decimal("100")
        rounded = _round_down(scaled, step)
        item = _thaw(original_copy)
        item["quantity"] = rounded
        output.append(item)
        minimum = _decimal(_first(original, "min_qty", "minimum_qty", default=_first(candidate, "min_qty", "minimum_qty")))
        maximum = _decimal(_first(original, "max_qty", "maximum_qty", default=_first(candidate, "max_qty", "maximum_qty")))
        price = _decimal(_first(original, "price", "mark_price", default=_first(candidate, "price", "mark_price")))
        minimum_notional = _decimal(_first(original, "min_notional", "minimum_notional", default=_first(candidate, "min_notional", "minimum_notional")))
        arithmetic_values = [value for value in (rounded, minimum, maximum, price, minimum_notional, base_quantity) if value is not None]
        with _safe_context(max(80, sum(len(str(value)) for value in arithmetic_values) + 30)):
            if rounded is not None:
                base_quantity = rounded if base_quantity is None else base_quantity + rounded
            violates = (
                rounded is None
                or rounded <= 0
                or (minimum is not None and rounded < minimum)
                or (maximum is not None and rounded > maximum)
                or (minimum_notional is not None and (price is None or rounded * price < minimum_notional))
            )
        if violates:
            rejected.append(f"ORDER_{index}_MINIMUM")
    if not original_orders:
        quantity = _decimal(_first(candidate, "quantity", "qty", "size"))
        step = _decimal(_first(candidate, "qty_step", "quantity_step", default="1"))
        if quantity is None or step is None:
            rejected.append("ORDER_0_INVALID")
        elif step <= 0:
            rejected.append("ORDER_0_STEP_INVALID")
        else:
            with _safe_context(len(quantity.as_tuple().digits) + len(scalar.as_tuple().digits) + 30):
                base_quantity = _round_down(quantity * scalar / Decimal("100"), step)
            if base_quantity is None or base_quantity <= 0:
                rejected.append("POST_ROUNDING_MINIMUM")
    return DirectionalSize(side, scalar, base_quantity, tuple(output), _thaw(_get(candidate, "geometry")), _thaw(_get(candidate, "lot_x")), tuple(rejected))


def _candidate_pnl(candidate: Any) -> Decimal | None:
    return _decimal(_first(candidate, "net_pnl", "individual_net_pnl", "historical_net_pnl"))


def _candidate_margin(candidate: Any) -> Decimal | None:
    return _decimal(_first(candidate, "initial_margin", "worst_initial_margin", "worst_calculated_initial_margin_requirement"))


def _variant_identity(variant: Variant) -> str:
    return _digest({"scalar": variant.scalar, "composition": variant.composition, "limiter": variant.limiter, "priority": variant.priority, "slot": {"symbol": variant.slot.symbol, "long": variant.slot.long, "short": variant.slot.short}, "directions": variant.directions})


def _scheduling_key(slot: PairSlot, scalar: Decimal, directions: Mapping[str, DirectionalSize]) -> SchedulingKey | None:
    operands: list[tuple[Decimal, Decimal]] = []
    for side in slot.directions:
        candidate = slot.long if side == "LONG" else slot.short
        pnl = _candidate_pnl(candidate)
        margin = _candidate_margin(candidate)
        if pnl is None or margin is None or margin <= 0:
            return None
        operands.append((pnl, margin))
    precision = max(80, len(str(scalar)) + sum(len(str(value)) for pair in operands for value in pair) + 40)
    numerator = Decimal("0")
    denominator = Decimal("0")
    with _safe_context(precision):
        for pnl, margin in operands:
            numerator += pnl * scalar / Decimal("100")
            denominator += margin * scalar / Decimal("100")
        if denominator <= 0:
            return None
        ratio = numerator / denominator
    if not all(value.is_finite() for value in (numerator, denominator, ratio)):
        return None
    identity = _digest({"slot": {"symbol": slot.symbol, "long": slot.long, "short": slot.short}, "scalar": scalar, "directions": directions})
    return SchedulingKey("scheduling_key", ratio, numerator, denominator, identity)


def _scheduling_sort_key(variant: Variant) -> tuple[Decimal, Decimal, str]:
    ratio = variant.scheduling_key.ratio
    # Equal scheduling ratios admit the largest scalar first, then canonical identity.
    with _safe_context(max(len(str(ratio)), len(str(variant.scalar))) + 40):
        negative_ratio = -ratio
        negative_scalar = -variant.scalar
    return negative_ratio, negative_scalar, _variant_identity(variant)


def _metric_values(variant: Variant, metrics: Sequence[Mapping[str, Any]]) -> tuple[bool, tuple[Decimal, ...], str]:
    values: list[Decimal] = []
    for descriptor in metrics:
        gathered: list[Decimal] = []
        for candidate in (variant.slot.long, variant.slot.short):
            if candidate is None:
                continue
            value = _decimal(_first(candidate, descriptor["field"]))
            if value is None:
                return False, (), f"RANKING_METRIC_MISSING:{descriptor['field']}"
            gathered.append(value)
        with _safe_context(max(80, *(len(str(value)) for value in gathered), 20)):
            total = sum(gathered, Decimal("0"))
        if not total.is_finite():
            return False, (), f"RANKING_METRIC_NONFINITE:{descriptor['field']}"
        values.append(total)
    return True, tuple(values), ""


def _campaign_digest(candidates: Sequence[Any], capability: Any, current_equity: Any, dd_cap_pct: Any, points: Sequence[Decimal], grid_version: str, budget: int | None, limiter: int, priorities: Sequence[int], composition: Any, opposite_policy: Any, ranking_policy: Any, tried: Sequence[Variant], ordered: Sequence[str], scheduling_order: Sequence[str], excluded: Sequence[ExcludedVariant], seed: Variant | None) -> str:
    finalists = sorted(
        (
            {"identity": canonical_candidate_identity(candidate), "candidate": candidate}
            for candidate in candidates
            if _valid_identity(candidate)
        ),
        key=lambda item: item["identity"],
    )
    return _digest({"finalists": tuple(finalists), "capability": capability, "current_equity": current_equity, "dd_cap_pct": dd_cap_pct, "grid": {"values": tuple(points), "units": "percentage", "version": grid_version, "order": tuple(points)}, "budget": budget, "limiter": limiter, "priorities": tuple(priorities), "composition": composition, "opposite_policy": opposite_policy, "ranking": ranking_policy, "tried": tuple(tried), "order": tuple(ordered), "scheduling_order": tuple(scheduling_order), "excluded": tuple(excluded), "seed": _variant_identity(seed) if seed else None})


def _failure(reason: str, detail: str, **kwargs: Any) -> SearchResult:
    stable = _stable(reason)
    return SearchResult(status=status_for_reasons((stable,)), reason=stable, reasons=(stable,), detail=detail, **kwargs)


def search_portfolios(candidates: Sequence[Any], *, capability: Mapping[str, Any] | None = None, current_equity: Any = None, dd_cap_pct: Any = None, sizing_grid: Sequence[Any] = (), grid_version: str = "m4-grid-v1", max_pretest_variant_count: int | None = None, ranking_policy: Mapping[str, Any] | None = None, limiter: int = 0, priority: int = 1, priorities: Sequence[Any] | None = None, composition: str | Sequence[str] | None = None, opposite_policy: Any = "KEEP_OPPOSITE", now_ms: int | None = None, structural_gate: Callable[[Variant], bool | str] | None = None, liquidity_gate: Callable[[Variant], bool | str] | None = None, margin_gate: Callable[[Variant], bool | str] | None = None, sink: Callable[[Variant], Any] | None = None, profile: Mapping[str, Any] | None = None, current_portfolio_equity: Any = None) -> SearchResult:
    if profile is not None and dd_cap_pct is None:
        dd_cap_pct = _first(profile, "each_strategy_max_dd_pct", "dd_cap_pct")
    if current_equity is None:
        current_equity = current_portfolio_equity
    if not isinstance(ranking_policy, Mapping) or not ranking_policy.get("version") or not isinstance(ranking_policy.get("metrics"), (list, tuple)) or not ranking_policy.get("metrics"):
        return _failure(OPEN_POLICY, "RANKING_POLICY_REQUIRED")
    metrics: list[Mapping[str, Any]] = []
    for descriptor in ranking_policy["metrics"]:
        if not isinstance(descriptor, Mapping) or not descriptor.get("field") or str(descriptor.get("direction", "")).upper() not in {"ASC", "DESC"}:
            return _failure(OPEN_POLICY, "RANKING_POLICY_DESCRIPTOR_INVALID")
        metrics.append({"field": str(descriptor["field"]), "direction": str(descriptor["direction"]).upper()})
    try:
        limiter = int(limiter)
        priority = int(priority)
        priority_values = [priority] if priorities is None else [int(value) for value in priorities]
    except (TypeError, ValueError):
        return _failure("VALIDATION_FAILED", "LIMITER_OR_PRIORITY_INVALID")
    if limiter < 0 or priority < 0 or not priority_values or any(value < 0 for value in priority_values):
        return _failure("VALIDATION_FAILED", "LIMITER_OR_PRIORITY_INVALID")
    if max_pretest_variant_count is not None:
        try:
            max_pretest_variant_count = int(max_pretest_variant_count)
        except (TypeError, ValueError):
            return _failure("VALIDATION_FAILED", "MAX_PRETEST_VARIANT_COUNT_INVALID")
        if max_pretest_variant_count <= 0:
            return _failure("VALIDATION_FAILED", "MAX_PRETEST_VARIANT_COUNT_INVALID")
    slots = build_pair_slots(candidates)
    if not slots:
        return _failure("VALIDATION_FAILED", "NO_FINALIST", slots=slots, ranking_policy=ranking_policy)
    for slot in slots:
        if slot.structural_reason:
            return _failure("VALIDATION_FAILED", slot.structural_reason, slots=slots, ranking_policy=ranking_policy)
        ok, reason, detail = _capability_ok(capability, slot, opposite_policy)
        if not ok:
            return _failure(reason, detail, slots=slots, ranking_policy=ranking_policy)
    points: list[Decimal] = []
    for raw in sizing_grid:
        point = _decimal(raw)
        if point is None or point <= 0:
            return _failure("VALIDATION_FAILED", "SIZING_GRID_INVALID", slots=slots, ranking_policy=ranking_policy)
        points.append(point)
    if not points:
        return _failure("VALIDATION_FAILED", "SIZING_GRID_INVALID", slots=slots, ranking_policy=ranking_policy)
    expanded: list[PairSlot] = []
    wanted = None if composition is None else ({str(composition)} if isinstance(composition, str) else {str(item) for item in composition})
    for slot in slots:
        choices = enumerate_compositions(slot)
        if wanted is not None:
            choices = tuple(choice for choice in choices if choice.composition in wanted or (choice.composition == "LONG+SHORT" and "BOTH" in wanted) or (choice.composition == "LONG" and "LONG_ONLY" in wanted) or (choice.composition == "SHORT" and "SHORT_ONLY" in wanted))
        expanded.extend(choices)
    if not expanded:
        return _failure("VALIDATION_FAILED", "COMPOSITION_EMPTY", slots=slots, ranking_policy=ranking_policy)
    limit_cache: dict[str, tuple[Any, ...]] = {}
    for slot in expanded:
        for side in slot.directions:
            candidate = slot.long if side == "LONG" else slot.short
            limit_cache[canonical_candidate_identity(candidate)] = _limit_parts(candidate, current_equity, dd_cap_pct, now_ms)
    planned = [(slot, selected_priority, point) for slot in expanded for selected_priority in priority_values for point in points]
    budget = len(planned) if max_pretest_variant_count is None else max_pretest_variant_count
    tried: list[Variant] = []
    passing: list[Variant] = []
    excluded: list[ExcludedVariant] = []
    for slot, selected_priority, point in planned:
        directions = {side: _size(slot.long if side == "LONG" else slot.short, side, point) for side in slot.directions}
        variant = Variant(point, slot, directions, FAIL, limiter=limiter, priority=selected_priority)
        reason = detail = gate = None
        parts = [limit_cache[canonical_candidate_identity(slot.long if side == "LONG" else slot.short)] for side in slot.directions]
        rejected = [item for size in directions.values() for item in size.rejected]
        if rejected:
            if any("INVALID" in item or "STEP_INVALID" in item for item in rejected):
                reason = "VALIDATION_FAILED"
            else:
                reason = "POST_ROUNDING_GEOMETRY" if any("GEOMETRY" in item for item in rejected) else "POST_ROUNDING_MINIMUM"
            detail, gate = ";".join(rejected), "structural"
        elif structural_gate is not None:
            verdict = structural_gate(variant)
            if verdict is not True and verdict != PASS:
                reason, detail, gate = "VALIDATION_FAILED", str(verdict), "structural"
        if reason is None:
            if any(part[0] is None for part in parts):
                reason, detail, gate = next((part[1], part[5], "liquidity") for part in parts if part[0] is None)
            elif point > min(part[2] for part in parts):
                reason, detail, gate = "LIQUIDITY_QUALITY_INSUFFICIENT", "LIQUIDITY_CEILING_EXCEEDED", "liquidity"
            elif liquidity_gate is not None:
                verdict = liquidity_gate(variant)
                if verdict is not True and verdict != PASS:
                    reason, detail, gate = "LIQUIDITY_QUALITY_INSUFFICIENT", str(verdict), "liquidity"
        if reason is None:
            if any(part[3] is None or part[4] is None for part in parts):
                reason, detail, gate = "MARGIN_BOUND_UNAVAILABLE", next((part[5] for part in parts if part[3] is None or part[4] is None), "MARGIN_OR_EXCHANGE_CEILING_UNAVAILABLE"), "margin"
            elif point > min(min(part[3], part[4]) for part in parts):
                reason, detail, gate = "MARGIN_BOUND_FAILED", "MARGIN_OR_EXCHANGE_CEILING_EXCEEDED", "margin"
            elif margin_gate is not None:
                verdict = margin_gate(variant)
                if verdict is not True and verdict != PASS:
                    reason, detail, gate = "MARGIN_BOUND_FAILED", str(verdict), "margin"
        if reason is None:
            if any(part[1] == "INDIVIDUAL_DD_UNAVAILABLE" for part in parts):
                reason, detail, gate = "INDIVIDUAL_DD_UNAVAILABLE", next((part[5] for part in parts if part[1] == "INDIVIDUAL_DD_UNAVAILABLE"), "DD_FACT_INVALID"), "individual_dd"
            elif any(part[1] == OPEN_POLICY for part in parts):
                reason, detail, gate = OPEN_POLICY, next((part[5] for part in parts if part[1] == OPEN_POLICY), "INDIVIDUAL_DD_CAP_OPEN_POLICY"), "individual_dd"
            elif point > min(part[5] if part[5] is not None else Decimal("-1") for part in parts):
                reason, detail, gate = "INDIVIDUAL_DD_LIMIT", "DD_SCALAR_LIMIT_EXCEEDED", "individual_dd"
        schedule = None if reason else _scheduling_key(slot, point, directions)
        if reason is None and schedule is None:
            reason, detail, gate = "MARGIN_BOUND_UNAVAILABLE", "SCHEDULING_MARGIN_DENOMINATOR_UNKNOWN", "margin"
        if reason is None:
            provisional = Variant(point, slot, directions, PASS, limiter=limiter, priority=selected_priority, scheduling_key=schedule)
            metric_ok, _, metric_detail = _metric_values(provisional, metrics)
            if not metric_ok:
                reason, detail, gate = "VALIDATION_FAILED", metric_detail, "ranking"
        status = PASS if reason is None else FAIL
        variant = Variant(point, slot, directions, status, _stable(reason) if reason else None, detail, gate, limiter, selected_priority, schedule)
        tried.append(variant)
        if status == PASS:
            passing.append(variant)
    if not passing:
        campaign = _campaign_digest(candidates, capability, current_equity, dd_cap_pct, points, grid_version, max_pretest_variant_count, limiter, priority_values, composition, opposite_policy, ranking_policy, tried, (), (), excluded, None)
        observed_reasons = tuple(dict.fromkeys(variant.reason for variant in tried if variant.reason is not None))
        all_reasons = tuple(dict.fromkeys((*observed_reasons, "NO_VALIDATION_PASS")))
        return SearchResult(
            status=status_for_reasons(all_reasons),
            reason=primary_reason(all_reasons),
            reasons=all_reasons,
            detail="NO_PASSING_VARIANT",
            slots=slots,
            tried=tuple(tried),
            excluded=tuple(excluded),
            ranking_policy=ranking_policy,
            campaign_identity=campaign,
            priority=priority,
            limiter=limiter,
            exhausted=bool(excluded),
            grid_version=grid_version,
        )

    def rank_key(variant: Variant) -> tuple[Any, ...]:
        _, values, _ = _metric_values(variant, metrics)
        with _safe_context(max(80, *(len(str(value)) for value in values), 20)):
            ordered_values = tuple(value if descriptor["direction"] == "ASC" else -value for descriptor, value in zip(metrics, values))
        return ordered_values + (_variant_identity(variant),)

    scheduled_all = tuple(sorted(passing, key=_scheduling_sort_key))
    scheduled = scheduled_all[:budget]
    admitted = tuple(scheduled)
    for variant in scheduled_all[budget:]:
        excluded.append(
            ExcludedVariant(
                variant.slot.symbol,
                variant.composition,
                variant.scalar,
                variant.priority,
                "ENUMERATION_FALLBACK_USED",
                "MAX_PRETEST_VARIANT_COUNT",
                evidence_class="CONSERVATIVE_BOUND",
                variant_identity=_variant_identity(variant),
            )
        )
    ordered = tuple(sorted(admitted, key=rank_key))
    if sink is not None:
        for variant in scheduled:
            sink(variant)
    maximum = max(admitted, key=lambda variant: (variant.scalar, _variant_identity(variant)))
    maximum_by_symbol = {
        symbol: max((variant for variant in admitted if variant.slot.symbol == symbol), key=lambda variant: (variant.scalar, _variant_identity(variant)))
        for symbol in {variant.slot.symbol for variant in admitted}
    }
    seed = max(admitted, key=lambda variant: (variant.scalar, _variant_identity(variant)))
    order = tuple(_variant_identity(variant) for variant in ordered)
    scheduling_order = tuple(_variant_identity(variant) for variant in scheduled)
    campaign = _campaign_digest(candidates, capability, current_equity, dd_cap_pct, points, grid_version, max_pretest_variant_count, limiter, priority_values, composition, opposite_policy, ranking_policy, tried, order, scheduling_order, excluded, seed)
    return SearchResult(status=PASS, slots=slots, tried=tuple(tried), passing=ordered, excluded=tuple(excluded), maximum=maximum, maximum_by_symbol=maximum_by_symbol, seed=seed, order=order, scheduling_key=maximum.scheduling_key, scheduling_order=scheduling_order, ranking_policy=ranking_policy, campaign_identity=campaign, priority=priority, limiter=limiter, exhausted=bool(excluded), grid_version=grid_version)


def _identity_parts(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    references: list[tuple[str, Any]] = []

    def walk_reference(node: Mapping[str, Any], path: str) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, child in node.items():
            if key == "timestamp_ms":
                continue
            child_path = f"{path}.{key}" if path else str(key)
            if key == "reference":
                if not isinstance(child, Mapping):
                    raise ValueError("reference must be a mapping")
                nested = walk_reference(child, child_path)
                if nested:
                    references.append((child_path, nested))
                continue
            cleaned[key] = walk(child, child_path, reference=True)
        return cleaned

    def walk(node: Any, path: str, *, reference: bool = False) -> Any:
        if isinstance(node, Mapping):
            cleaned: dict[str, Any] = {}
            for key, child in node.items():
                if reference and key == "timestamp_ms":
                    continue
                child_path = f"{path}.{key}" if path else str(key)
                if key == "reference":
                    if not isinstance(child, Mapping):
                        raise ValueError("reference must be a mapping")
                    meaningful = walk_reference(child, child_path)
                    if meaningful:
                        references.append((child_path, meaningful))
                    continue
                cleaned[key] = walk(child, child_path, reference=reference)
            return cleaned
        if isinstance(node, (list, tuple)):
            return [walk(child, f"{path}[{index}]", reference=reference) for index, child in enumerate(node)]
        return node

    try:
        executable = walk(value, "")
        reference_facts = sorted(references, key=lambda item: item[0])
        return _typed_json(executable), _typed_json(reference_facts)
    except (TypeError, ValueError):
        return None


def classify_identity(previous: Mapping[str, Any], current: Mapping[str, Any]) -> IdentityResult:
    old_parts = _identity_parts(previous)
    new_parts = _identity_parts(current)
    if old_parts is None or new_parts is None:
        return IdentityResult(True, True, "VALIDATION_FAILED")
    if old_parts[0] != new_parts[0]:
        return IdentityResult(True, True, "EXECUTABLE_PAYLOAD_CHANGED")
    if old_parts[1] != new_parts[1]:
        return IdentityResult(True, False, "MEANINGFUL_REFERENCE_CHANGED")
    return IdentityResult(False, False, "REFERENCE_TIMESTAMP_ONLY")


__all__ = [
    "FAIL", "OPEN_POLICY", "PASS", "UNKNOWN", "REASON_ENUM_VERSION", "PairSlot", "DirectionalSize", "SchedulingKey", "Variant", "ExcludedVariant", "PortfolioCandidate", "SearchResult", "IdentityResult",
    "build_pair_slots", "enumerate_compositions", "canonical_candidate_identity", "search_portfolios", "classify_identity",
]
