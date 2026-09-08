"""Pure, immutable execution research contracts for Phase 2A.

This module consumes fixture/fake execution facts only.  It does not model a
queue, call an exchange, or publish a trading decision.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
import json
from types import MappingProxyType
from typing import Any

from .canonical import CanonicalEnvelope, canonical_digest_v1


EXECUTION_EVIDENCE_SCHEMA_VERSION = 1
ORDER_EVENT_SCHEMA_VERSION = 1
CALIBRATION_SCHEMA_VERSION = 1
STRESS_SCHEMA_VERSION = 1
REGIME_SCHEMA_VERSION = "execution_regime_v1"
RESEARCH_ONLY = "RESEARCH_ONLY"
NEEDS_RETEST = "NEEDS_RETEST"
AVAILABLE = "AVAILABLE"
PARTIAL = "PARTIAL"
UNKNOWN = "UNKNOWN"
INCONSISTENT = "INCONSISTENT"

EVENT_TYPES = frozenset(
    {
        "PLACED",
        "ACKNOWLEDGED",
        "PARTIAL_FILL",
        "FULL_FILL",
        "CANCEL_REQUESTED",
        "CANCEL_CONFIRMED",
        "REPLACE_REQUESTED",
        "REPLACE_CONFIRMED",
        "REDUCE_ONLY_FILL",
        "REJECTED",
    }
)
REFINEMENT_KINDS = frozenset(
    {"ORDER_LOSS", "COLLATERAL_HAIRCUT", "BORROW", "CLOSE_FEES", "ACTIVE_ORDER_RESERVE"}
)
_UNSET = object()
_MISSING = object()
DECIMAL128_PRECISION = 34
DECIMAL128_EMAX = 6144
DECIMAL128_EMIN = -6143
DECIMAL128_MIN_ADJUSTED = DECIMAL128_EMIN - DECIMAL128_PRECISION + 1
GATE_STATUSES = frozenset({"PASS", "FAIL", "UNKNOWN", AVAILABLE})
_ALLOWED_DATACLASS_NAMES = frozenset({
    "OrderEvent", "LifecycleResult", "ExecutionObservation", "CalibrationCurve",
    "CalibrationResult", "RefinementEvidence", "CorrelatedStressResult", "RetestLineage",
})


def _is_allowed_dataclass(value: Any) -> bool:
    if not is_dataclass(value) or type(value).__module__ != __name__:
        return False
    return type(value).__name__ in _ALLOWED_DATACLASS_NAMES and globals().get(type(value).__name__) is type(value)


def _decimal(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (float, bool)):
        raise TypeError(f"{name} must be an exact Decimal")
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{name} must be an exact Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must be a Decimal") from exc
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{name} has an invalid value")
    return result


def _decimal_precision(values: Iterable[Decimal]) -> int:
    return DECIMAL128_PRECISION


@contextmanager
def _decimal_context(values: Iterable[Decimal]):
    values = tuple(values)
    if any(not value.is_finite() for value in values):
        raise InvalidOperation("execution arithmetic requires finite Decimal inputs")
    if any(value.adjusted() > DECIMAL128_EMAX or value.adjusted() < DECIMAL128_MIN_ADJUSTED for value in values):
        raise Overflow("execution arithmetic exceeds decimal128 bounds")
    with localcontext() as context:
        context.prec = DECIMAL128_PRECISION
        context.rounding = ROUND_HALF_EVEN
        context.Emax = DECIMAL128_EMAX
        context.Emin = DECIMAL128_EMIN
        context.clamp = 1
        context.clear_flags()
        required_traps = {InvalidOperation, DivisionByZero, Overflow}
        for signal in tuple(context.traps):
            context.traps[signal] = signal in required_traps
        yield context


def _decimal_mean(values: Iterable[Decimal]) -> Decimal | None:
    values = tuple(values)
    if not values:
        return None
    with _decimal_context((*values, Decimal(len(values)))):
        return sum(values, Decimal("0")) / Decimal(len(values))


def _arithmetic_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, (float, bool)) or not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{name} must be an exact Decimal")
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must be a Decimal") from exc


def _decimal_ratio(numerator: Decimal | int, denominator: Decimal | int) -> Decimal:
    values = tuple(_arithmetic_decimal(value, name) for value, name in ((numerator, "numerator"), (denominator, "denominator")))
    with _decimal_context(values):
        return values[0] / values[1]


def _decimal_difference(left: Decimal | int | str, right: Decimal | int | str) -> Decimal:
    left_value = _arithmetic_decimal(left, "left")
    right_value = _arithmetic_decimal(right, "right")
    with _decimal_context((left_value, right_value)):
        return left_value - right_value


def _timestamp(value: Any, name: str = "timestamp") -> datetime | None:
    if value is None:
        return None
    if type(value) is int:
        if value < 0:
            raise ValueError(f"{name} must be non-negative")
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _freeze(value: Any) -> Any:
    if isinstance(value, float):
        raise TypeError("execution evidence cannot contain Python float")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("execution evidence mapping keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        raise TypeError("execution evidence cannot contain sets")
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("execution evidence Decimal must be finite")
        return value
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution evidence datetime must be timezone-aware")
        return value.astimezone(timezone.utc)
    if value is None or type(value) in {str, int, bool}:
        return value
    if _is_allowed_dataclass(value):
        return _freeze({item.name: getattr(value, item.name) for item in fields(value)})
    if is_dataclass(value):
        raise TypeError("unsupported execution evidence dataclass")
    raise TypeError(f"unsupported execution evidence value: {type(value).__name__}")


def _stable(value: Any) -> Any:
    if isinstance(value, float):
        raise TypeError("execution evidence cannot contain Python float")
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("execution evidence Decimal must be finite")
        # Decimal text intentionally preserves trailing zero scale: this digest
        # is representation-sensitive by contract, while remaining deterministic.
        return {"__type__": "Decimal", "value": str(value)}
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution evidence datetime must be timezone-aware")
        return {"__type__": "Timestamp", "value": value.astimezone(timezone.utc).isoformat()}
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("execution evidence mapping keys must be strings")
        return {"__type__": "Mapping", "items": [[key, _stable(item)] for key, item in sorted(value.items())]}
    if isinstance(value, (set, frozenset)):
        raise TypeError("execution evidence cannot contain sets")
    if isinstance(value, list):
        return {"__type__": "List", "items": [_stable(v) for v in value]}
    if isinstance(value, tuple):
        return {"__type__": "Tuple", "items": [_stable(v) for v in value]}
    if _is_allowed_dataclass(value):
        result = {}
        for item in fields(value):
            field_value = getattr(value, item.name)
            if item.name == "requested_by_revision" and isinstance(field_value, Mapping):
                if any(type(key) is not int for key in field_value):
                    raise TypeError("requested_by_revision keys must be integers")
                field_value = {str(key): field_value[key] for key in field_value}
            result[item.name] = _stable(field_value)
        return {"__type__": "Dataclass", "name": type(value).__name__, "fields": result}
    if is_dataclass(value):
        raise TypeError("unsupported execution evidence dataclass")
    if type(value) is bool:
        return {"__type__": "Bool", "value": value}
    if type(value) is int:
        return {"__type__": "Int", "value": str(value)}
    if type(value) is str:
        return {"__type__": "String", "value": value}
    if value is None:
        return {"__type__": "Null"}
    raise TypeError(f"unsupported execution evidence value: {type(value).__name__}")


def _digest(value: Any, schema_id: str) -> str:
    encoded = json.dumps(_stable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical_digest_v1(CanonicalEnvelope(schema_id, 1, "execution_research", "json", encoded))


def _field(item: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in item:
            return item[name]
    return default


def _resolve_alias(name: str, aliases: Iterable[tuple[str, Any]], *, default: Any = _MISSING, normalize: Callable[[Any], Any] | None = None) -> Any:
    present = [(alias, value) for alias, value in aliases if value is not _MISSING and value is not None]
    if not present:
        return default
    normalizer = normalize or (lambda value: value)
    first_alias, first_value = present[0]
    first_normalized = normalizer(first_value)
    for alias, value in present[1:]:
        if normalizer(value) != first_normalized:
            raise ValueError(f"{name} aliases conflict: {first_alias} and {alias}")
    return first_normalized


def _mapping_alias(item: Mapping[str, Any], name: str, *aliases: str, default: Any = _MISSING, normalize: Callable[[Any], Any] | None = None) -> Any:
    return _resolve_alias(name, ((alias, item[alias]) for alias in aliases if alias in item), default=default, normalize=normalize)


def _observation_alias(item: Mapping[str, Any], name: str, *aliases: str, default: Any = _MISSING, normalize: Callable[[Any], Any] | None = None) -> Any:
    present = [(alias, item[alias]) for alias in aliases if alias in item]
    if not present:
        return default
    normalizer = normalize or (lambda value: value)
    first_alias, first_value = present[0]
    first_normalized = normalizer(first_value)
    for alias, value in present[1:]:
        if normalizer(value) != first_normalized:
            raise ValueError(f"{name} aliases conflict: {first_alias} and {alias}")
    return first_normalized


def _observation_text(value: Any, name: str, *, allow_none: bool = False, nonempty: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str or (nonempty and not value):
        raise TypeError(f"observation {name} must be a {'non-empty ' if nonempty else ''}string")
    return value


def _revision_link_alias(value: Any) -> str:
    if type(value) is int:
        return str(value)
    if type(value) is str:
        if value.isascii() and value.isdecimal():
            return str(int(value))
        return value
    raise TypeError("revision_link must be an integer or string")


@dataclass(frozen=True, slots=True, init=False)
class OrderEvent:
    event_schema_version: int
    event_id: str
    execution_campaign_id: str
    trading_run_id: str
    account_alias: str
    order_id: str
    order_revision: int
    symbol: str
    side: str
    timeframe: str
    regime: str | None
    regime_version: str | None
    event_type: str
    source_sequence: int
    timestamp_utc: datetime | None
    requested_qty: Decimal | None
    fill_qty: Decimal | None
    cumulative_filled_qty: Decimal | None
    fill_delta_qty: Decimal | None
    remaining_qty: Decimal | None
    quantity_unit: str
    reduce_only: bool | None
    provenance: str
    revision_link: str | None
    facts: Mapping[str, Any]

    def __init__(
        self,
        event_schema_version: int = ORDER_EVENT_SCHEMA_VERSION,
        event_id: str | None = None,
        execution_campaign_id: str | None = None,
        trading_run_id: str | None = None,
        account_alias: str | None = None,
        order_id: str | None = None,
        order_revision: int = 0,
        symbol: str | None = None,
        side: str | None = None,
        timeframe: str | None = None,
        regime: str | None = None,
        regime_version: str | None = REGIME_SCHEMA_VERSION,
        event_type: str | None = None,
        source_sequence: int | None = None,
        timestamp_utc: Any = None,
        requested_qty: Any = None,
        fill_qty: Any = None,
        remaining_qty: Any = None,
        quantity_unit: str | None = None,
        reduce_only: bool | None = None,
        provenance: str = "fixture",
        revision_link: str | None = None,
        facts: Mapping[str, Any] | None = None,
        *,
        cumulative_filled_qty: Any = None,
        fill_delta_qty: Any = None,
        source_index: int | None = None,
        source_timestamp: Any = None,
        event_timestamp: Any = None,
        unit: str | None = None,
        replaces_revision: int | str | None = None,
        previous_revision: int | str | None = None,
        replaces_order_revision: int | str | None = None,
    ) -> None:
        sequence = _resolve_alias("source_sequence", (("source_sequence", source_sequence), ("source_index", source_index)))
        if sequence is _MISSING:
            raise ValueError("source_sequence is required")
        if type(event_schema_version) is not int or event_schema_version != ORDER_EVENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported event_schema_version: {event_schema_version}")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("source_sequence must be non-negative")
        if type(order_revision) is not int or order_revision < 0:
            raise ValueError("order_revision must be non-negative")
        quantity = _resolve_alias("quantity_unit", (("quantity_unit", quantity_unit), ("unit", unit)), default="contracts")
        values = {
            "event_id": event_id,
            "execution_campaign_id": execution_campaign_id,
            "trading_run_id": trading_run_id,
            "account_alias": account_alias,
            "order_id": order_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "quantity_unit": quantity,
            "provenance": provenance,
        }
        for name, value in values.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if regime is not None and (not isinstance(regime, str) or not regime):
            raise ValueError("regime must be a non-empty string or None")
        if regime is not None and (not isinstance(regime_version, str) or not regime_version):
            raise ValueError("regime_version must be a non-empty string when regime is present")
        if facts is not None and not isinstance(facts, Mapping):
            raise TypeError("facts must be a mapping")
        side_value = str(side or "").upper()
        if side_value not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        event_value = str(event_type or "").upper()
        if event_value not in EVENT_TYPES:
            raise ValueError(f"unknown event_type: {event_value}")
        event_time = _resolve_alias(
            "timestamp", (("timestamp_utc", timestamp_utc), ("source_timestamp", source_timestamp), ("event_timestamp", event_timestamp)),
            default=None, normalize=lambda value: _timestamp(value),
        )
        requested = None if requested_qty is None else _decimal(requested_qty, "requested_qty", positive=True)
        fill = None if fill_qty is None else _decimal(fill_qty, "fill_qty", nonnegative=True)
        cumulative = None if cumulative_filled_qty is None else _decimal(cumulative_filled_qty, "cumulative_filled_qty", nonnegative=True)
        if fill is not None and cumulative is not None and fill != cumulative:
            raise ValueError("fill_qty and cumulative_filled_qty must match")
        if fill is None:
            fill = cumulative
        elif cumulative is None:
            cumulative = fill
        delta = None if fill_delta_qty is None else _decimal(fill_delta_qty, "fill_delta_qty", nonnegative=True)
        remaining = None if remaining_qty is None else _decimal(remaining_qty, "remaining_qty", nonnegative=True)
        if reduce_only is not None and type(reduce_only) is not bool:
            raise TypeError("reduce_only must be a bool or None")
        if event_value == "REDUCE_ONLY_FILL" and reduce_only is False:
            raise ValueError("REDUCE_ONLY_FILL must be reduce-only")
        object.__setattr__(self, "event_schema_version", event_schema_version)
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "execution_campaign_id", execution_campaign_id)
        object.__setattr__(self, "trading_run_id", trading_run_id)
        object.__setattr__(self, "account_alias", account_alias)
        object.__setattr__(self, "order_id", order_id)
        object.__setattr__(self, "order_revision", order_revision)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side_value)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "regime", regime)
        object.__setattr__(self, "regime_version", regime_version if regime is not None else None)
        object.__setattr__(self, "event_type", event_value)
        object.__setattr__(self, "source_sequence", sequence)
        object.__setattr__(self, "timestamp_utc", event_time)
        object.__setattr__(self, "requested_qty", requested)
        object.__setattr__(self, "fill_qty", fill)
        object.__setattr__(self, "cumulative_filled_qty", cumulative)
        object.__setattr__(self, "fill_delta_qty", delta)
        object.__setattr__(self, "remaining_qty", remaining)
        object.__setattr__(self, "quantity_unit", values["quantity_unit"])
        object.__setattr__(self, "reduce_only", True if event_value == "REDUCE_ONLY_FILL" else reduce_only)
        object.__setattr__(self, "provenance", provenance)
        link = _resolve_alias(
            "revision_link", (("revision_link", revision_link), ("replaces_revision", replaces_revision), ("previous_revision", previous_revision), ("replaces_order_revision", replaces_order_revision)),
            default=None, normalize=_revision_link_alias,
        )
        if link is not None and (not link or not link.isascii() or not link.isdecimal()):
            raise ValueError("revision_link must identify a non-negative revision")
        object.__setattr__(self, "revision_link", link)
        object.__setattr__(self, "facts", _freeze(facts or {}))

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> "OrderEvent":
        if not isinstance(item, Mapping):
            raise TypeError("order event must be a mapping")
        _stable(item)
        return cls(
            event_schema_version=_mapping_alias(item, "event_schema_version", "event_schema_version", "schema_version", default=1),
            event_id=_mapping_alias(item, "event_id", "event_id"),
            execution_campaign_id=_mapping_alias(item, "execution_campaign_id", "execution_campaign_id", "campaign_id"),
            trading_run_id=_mapping_alias(item, "trading_run_id", "trading_run_id", "run_id"),
            account_alias=_mapping_alias(item, "account_alias", "account_alias", "account"),
            order_id=_mapping_alias(item, "order_id", "order_id"),
            order_revision=_mapping_alias(item, "order_revision", "order_revision", "revision", default=0),
            symbol=_mapping_alias(item, "symbol", "symbol"),
            side=_mapping_alias(item, "side", "side"),
            timeframe=_mapping_alias(item, "timeframe", "timeframe"),
            regime=_mapping_alias(item, "regime", "regime", default=None),
            regime_version=_mapping_alias(item, "regime_version", "regime_version", default=REGIME_SCHEMA_VERSION),
            event_type=_mapping_alias(item, "event_type", "event_type", "type"),
            source_sequence=_mapping_alias(item, "source_sequence", "source_sequence", "source_index", "sequence"),
            timestamp_utc=_mapping_alias(item, "timestamp", "timestamp_utc", "event_timestamp", "source_timestamp", default=None, normalize=lambda value: _timestamp(value)),
            requested_qty=_mapping_alias(item, "requested_qty", "requested_qty", "requested", default=None),
            fill_qty=_mapping_alias(item, "fill_qty", "fill_qty", "filled_qty", "filled", default=None),
            cumulative_filled_qty=_mapping_alias(item, "cumulative_filled_qty", "cumulative_filled_qty", "cumulative_fill_qty", default=None),
            fill_delta_qty=_mapping_alias(item, "fill_delta_qty", "fill_delta_qty", "delta_fill_qty", default=None),
            remaining_qty=_mapping_alias(item, "remaining_qty", "remaining_qty", "remaining", default=None),
            quantity_unit=_mapping_alias(item, "quantity_unit", "quantity_unit", "unit", default="contracts"),
            reduce_only=_mapping_alias(item, "reduce_only", "reduce_only", default=None),
            provenance=_mapping_alias(item, "provenance", "provenance", "source", default="fixture"),
            revision_link=_mapping_alias(item, "revision_link", "revision_link", "replaces_revision", "previous_revision", "replaces_order_revision", default=None, normalize=_revision_link_alias),
            facts=_mapping_alias(item, "facts", "facts", "metadata", default={}),
        )

    @property
    def identity(self) -> tuple[str, str, str, int, str]:
        return (self.execution_campaign_id, self.account_alias, self.order_id, self.order_revision, self.event_id)

    @property
    def source_timestamp(self) -> datetime | None:
        return self.timestamp_utc

    @property
    def digest(self) -> str:
        return _digest(self.as_dict(), "portfolio_order_event_v1")

    @property
    def content_digest(self) -> str:
        return self.digest

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_schema_version": self.event_schema_version,
            "event_id": self.event_id,
            "execution_campaign_id": self.execution_campaign_id,
            "trading_run_id": self.trading_run_id,
            "account_alias": self.account_alias,
            "order_id": self.order_id,
            "order_revision": self.order_revision,
            "symbol": self.symbol,
            "side": self.side,
            "timeframe": self.timeframe,
            "regime": self.regime,
            "regime_version": self.regime_version,
            "event_type": self.event_type,
            "source_sequence": self.source_sequence,
            "timestamp_utc": self.timestamp_utc,
            "requested_qty": self.requested_qty,
            "fill_qty": self.fill_qty,
            "cumulative_filled_qty": self.cumulative_filled_qty,
            "fill_delta_qty": self.fill_delta_qty,
            "remaining_qty": self.remaining_qty,
            "quantity_unit": self.quantity_unit,
            "reduce_only": self.reduce_only,
            "provenance": self.provenance,
            "revision_link": self.revision_link,
            "facts": self.facts,
        }


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    status: str
    order_id: str
    events: tuple[OrderEvent, ...]
    requested_qty: Decimal | None
    filled_qty: Decimal | None
    fill_ratio: Decimal | None
    reason: str | None
    first_fill_at: datetime | None
    full_fill_at: datetime | None
    time_to_first_fill: Decimal | None
    time_to_full_fill: Decimal | None
    partial_count: int
    remaining_at_cancel: Decimal | None
    remaining_at_replace: Decimal | None
    reserve_active: bool | None
    reserve_state: str
    revision: int
    replacement_links: tuple[tuple[int, int], ...]
    reduce_only_fill_qty: Decimal | None
    reduce_only_fill_count: int
    source_digest: str
    duplicate_event_ids: tuple[str, ...] = ()
    conflicting_event_ids: tuple[str, ...] = ()
    requested_by_revision: Mapping[int, Decimal | None] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _stable(self)
        object.__setattr__(self, "requested_by_revision", MappingProxyType(dict(self.requested_by_revision)))

    @property
    def disposition(self) -> str:
        return RESEARCH_ONLY

    @property
    def usable(self) -> bool:
        return False

    @property
    def approved(self) -> bool:
        return False

    @property
    def first_fill_latency(self) -> Decimal | None:
        return self.time_to_first_fill

    @property
    def full_fill_latency(self) -> Decimal | None:
        return self.time_to_full_fill

    @property
    def remaining_on_cancel(self) -> Decimal | None:
        return self.remaining_at_cancel

    @property
    def remaining_on_replace(self) -> Decimal | None:
        return self.remaining_at_replace


def _inconsistent(order_id: str, events: tuple[OrderEvent, ...], reason: str, *, conflicts: tuple[str, ...] = (), conflict_digests: tuple[str, ...] = ()) -> LifecycleResult:
    return LifecycleResult(INCONSISTENT, order_id, events, None, None, None, reason, None, None, None, None, 0, None, None, None, "UNKNOWN", 0, (), None, 0, _digest([*([e.digest for e in events]), *conflict_digests], "portfolio_execution_lifecycle_v1"), conflicting_event_ids=conflicts, errors=(reason,))


def _coerce_event(item: OrderEvent | Mapping[str, Any]) -> OrderEvent:
    return item if isinstance(item, OrderEvent) else OrderEvent.from_mapping(item)


def _link_matches(link: str | int | None, *, order_id: str, revision: int) -> bool:
    if type(link) is int:
        return link == revision
    if not isinstance(link, str) or not link:
        return False
    return link in {str(revision), f"{order_id}:{revision}", f"{order_id}@{revision}"}


def _seconds(start: datetime | None, end: datetime | None) -> Decimal | None:
    if start is None or end is None or end < start:
        return None
    delta = end - start
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    micros_value = Decimal(micros)
    denominator = Decimal(1_000_000)
    quantum = Decimal("0.000001")
    with _decimal_context((micros_value, denominator, quantum)):
        return (micros_value / denominator).quantize(quantum, rounding=ROUND_HALF_EVEN)


def reduce_order_lifecycle(events: Iterable[OrderEvent | Mapping[str, Any]]) -> LifecycleResult:
    incoming = tuple(_coerce_event(item) for item in events)
    if not incoming:
        raise ValueError("lifecycle requires events")
    grouped: dict[tuple[str, str, str, int, str], list[OrderEvent]] = {}
    for event in incoming:
        grouped.setdefault(event.identity, []).append(event)
    unique: list[OrderEvent] = []
    duplicates: list[str] = []
    conflicts: list[str] = []
    conflict_digests: list[str] = []
    for identity in sorted(grouped):
        candidates = sorted(grouped[identity], key=lambda event: event.digest)
        digest_groups: dict[str, list[OrderEvent]] = {}
        for event in candidates:
            digest_groups.setdefault(event.digest, []).append(event)
        distinct_digests = sorted(digest_groups)
        representative = digest_groups[distinct_digests[0]][0]
        unique.append(representative)
        duplicates.extend(event.event_id for digest in distinct_digests for event in digest_groups[digest][1:])
        conflicting_digests = distinct_digests[1:]
        conflicts.extend(digest_groups[digest][0].event_id for digest in conflicting_digests)
        conflict_digests.extend(conflicting_digests)
    unique.sort(key=lambda event: (event.source_sequence, event.identity, event.digest))
    events_tuple = tuple(unique)
    order_id = events_tuple[0].order_id
    common = events_tuple[0]
    duplicates.sort()
    conflicts.sort()
    conflict_digests.sort()
    if conflicts:
        return _inconsistent(order_id, events_tuple, "CONFLICTING_DUPLICATE", conflicts=tuple(conflicts), conflict_digests=tuple(conflict_digests))
    for event in unique:
        if event.fill_delta_qty is not None and event.event_type not in {"PARTIAL_FILL", "FULL_FILL", "REDUCE_ONLY_FILL"}:
            return _inconsistent(order_id, events_tuple, "FILL_DELTA_ON_NON_FILL")
    if any(event.order_id != order_id for event in unique):
        return _inconsistent(order_id, events_tuple, "INCONSISTENT_ORDER_ID")
    for event in unique:
        if (event.execution_campaign_id, event.trading_run_id, event.account_alias, event.symbol, event.side, event.timeframe, event.quantity_unit) != (common.execution_campaign_id, common.trading_run_id, common.account_alias, common.symbol, common.side, common.timeframe, common.quantity_unit):
            return _inconsistent(order_id, events_tuple, "INCONSISTENT_CAMPAIGN_FACTS")
    if len({event.source_sequence for event in unique}) != len(unique):
        return _inconsistent(order_id, events_tuple, "AMBIGUOUS_SEQUENCE")
    if any(event.source_sequence <= previous.source_sequence for previous, event in zip(unique, unique[1:])):
        return _inconsistent(order_id, events_tuple, "INVALID_EVENT_ORDER")
    previous_timestamp: datetime | None = None
    for event in unique:
        if event.timestamp_utc is not None:
            if previous_timestamp is not None and event.timestamp_utc < previous_timestamp:
                return _inconsistent(order_id, events_tuple, "INVALID_EVENT_ORDER")
            previous_timestamp = event.timestamp_utc
    if unique[0].event_type != "PLACED":
        return _inconsistent(order_id, events_tuple, "INVALID_INITIAL_STATE")
    states: dict[int, dict[str, Any]] = {}
    current_revision = unique[0].order_revision
    links: list[tuple[int, int]] = []
    global_requested: Decimal | None = None
    first_fill: datetime | None = None
    fill_timestamp_unknown = False
    full_fill: datetime | None = None
    partial_count = 0
    reduce_qty = Decimal("0")
    reduce_unknown = False
    reduce_count = 0
    remaining_cancel: Decimal | None = None
    remaining_replace: Decimal | None = None
    placement: datetime | None = None
    allowed = {
        None: {"PLACED"},
        "PLACED": {"ACKNOWLEDGED", "PARTIAL_FILL", "FULL_FILL", "CANCEL_REQUESTED", "REPLACE_REQUESTED", "REJECTED"},
        "ACKNOWLEDGED": {"PARTIAL_FILL", "FULL_FILL", "CANCEL_REQUESTED", "REPLACE_REQUESTED", "REJECTED"},
        "PARTIAL_FILL": {"PARTIAL_FILL", "FULL_FILL", "CANCEL_REQUESTED", "REPLACE_REQUESTED", "REDUCE_ONLY_FILL"},
        "REDUCE_ONLY_FILL": {"PARTIAL_FILL", "FULL_FILL", "CANCEL_REQUESTED", "REPLACE_REQUESTED", "REDUCE_ONLY_FILL"},
        "CANCEL_REQUESTED": {"CANCEL_CONFIRMED"},
        "REPLACE_REQUESTED": {"REPLACE_CONFIRMED"},
        "REPLACE_CONFIRMED": {"ACKNOWLEDGED", "PARTIAL_FILL", "FULL_FILL", "CANCEL_REQUESTED", "REPLACE_REQUESTED", "REDUCE_ONLY_FILL", "REJECTED"},
    }
    for event in unique:
        state = states.get(event.order_revision)
        if event.event_type == "REPLACE_CONFIRMED" and state is None:
            prior_revision = max((revision for revision in states if revision < event.order_revision), default=None)
            prior_state = states.get(prior_revision) if prior_revision is not None else None
            if prior_state is None or prior_state["last"] != "REPLACE_REQUESTED":
                return _inconsistent(order_id, events_tuple, "INVALID_REPLACE_TRANSITION")
            if event.order_revision != prior_revision + 1:
                return _inconsistent(order_id, events_tuple, "INVALID_REPLACEMENT_REVISION")
            if not _link_matches(event.revision_link, order_id=order_id, revision=prior_revision):
                return _inconsistent(order_id, events_tuple, "INVALID_REPLACEMENT_LINK")
            links.append((prior_revision, event.order_revision))
            remaining_replace = prior_state.get("remaining")
            state = {"last": "REPLACE_CONFIRMED", "requested": None, "filled": Decimal("0"), "fill_known": True, "cumulative_high_water": None, "reduce_baseline": None, "remaining": None, "remaining_observed": event.remaining_qty, "reserve": True, "placement": placement}
            states[event.order_revision] = state
            current_revision = event.order_revision
        elif event.event_type == "REPLACE_CONFIRMED":
            return _inconsistent(order_id, events_tuple, "INVALID_REPLACEMENT_REVISION")
        elif state is None:
            state = {"last": None, "requested": None, "filled": Decimal("0"), "fill_known": True, "cumulative_high_water": None, "reduce_baseline": None, "remaining": None, "remaining_observed": None, "reserve": True, "placement": placement}
            states[event.order_revision] = state
        if event.event_type != "REPLACE_CONFIRMED" and event.event_type not in allowed.get(state["last"], set()):
            return _inconsistent(order_id, events_tuple, "INVALID_LIFECYCLE_TRANSITION")
        if event.event_type == "PLACED":
            if placement is not None:
                return _inconsistent(order_id, events_tuple, "DUPLICATE_PLACEMENT")
            placement = event.timestamp_utc
            state["placement"] = placement
        if event.requested_qty is not None:
            if state["requested"] is not None and state["requested"] != event.requested_qty:
                return _inconsistent(order_id, events_tuple, "REQUESTED_QUANTITY_CONFLICT")
            state["requested"] = event.requested_qty
            if global_requested is None:
                global_requested = event.requested_qty
        candidate = state["filled"] if state["fill_known"] else None
        previous_filled = candidate
        cumulative_fill = event.cumulative_filled_qty
        if cumulative_fill is not None:
            high_water = state["cumulative_high_water"]
            if high_water is not None and cumulative_fill < high_water:
                return _inconsistent(order_id, events_tuple, "NON_MONOTONE_CUMULATIVE_FILL")
            candidate = cumulative_fill
            state["cumulative_high_water"] = cumulative_fill
            state["filled"] = cumulative_fill
            state["fill_known"] = True
        elif event.event_type in {"PARTIAL_FILL", "FULL_FILL", "REDUCE_ONLY_FILL"}:
            # A FULL_FILL event is only quantitative when it carries a known
            # cumulative quantity; the event type alone cannot prove it.
            state["fill_known"] = False
        if state["requested"] is not None and candidate is not None:
            if event.event_type == "PARTIAL_FILL" and candidate == state["requested"]:
                return _inconsistent(order_id, events_tuple, "PARTIAL_FILL_COMPLETE")
            expected_remaining = _decimal_difference(state["requested"], candidate)
            if expected_remaining < 0:
                return _inconsistent(order_id, events_tuple, "FILL_EXCEEDS_REQUESTED")
            if event.event_type == "FULL_FILL" and cumulative_fill is not None and cumulative_fill != state["requested"]:
                return _inconsistent(order_id, events_tuple, "FULL_FILL_QUANTITY_MISMATCH")
            if event.remaining_qty is not None and event.remaining_qty != expected_remaining:
                # A cancel/replace confirmation can report a fill that had no
                # separate lifecycle event.  Keep the observation and close
                # the volume ratio until the missing fill fact is supplied.
                if event.event_type in {"CANCEL_CONFIRMED", "REPLACE_CONFIRMED"} and cumulative_fill is None:
                    state["fill_known"] = False
                    state["remaining"] = event.remaining_qty
                else:
                    return _inconsistent(order_id, events_tuple, "REMAINING_QUANTITY_MISMATCH")
            else:
                state["remaining"] = expected_remaining
        elif event.remaining_qty is not None:
            if state["remaining"] is not None and event.remaining_qty > state["remaining"] and event.event_type != "REPLACE_CONFIRMED":
                return _inconsistent(order_id, events_tuple, "NON_MONOTONE_REMAINING")
            state["remaining"] = event.remaining_qty
        if event.event_type == "PARTIAL_FILL":
            partial_count += 1
        if event.event_type in {"PARTIAL_FILL", "FULL_FILL", "REDUCE_ONLY_FILL"}:
            if event.timestamp_utc is None:
                fill_timestamp_unknown = True
            if event.event_type == "REDUCE_ONLY_FILL":
                reduce_count += 1
                if state["reduce_baseline"] is None:
                    state["reduce_baseline"] = previous_filled
                if cumulative_fill is not None:
                    baseline = state["reduce_baseline"]
                    if baseline is None:
                        reduce_unknown = True
                    else:
                        delta = _decimal_difference(cumulative_fill, baseline)
                        if delta < 0:
                            reduce_unknown = True
                        else:
                            reduce_qty = delta
                else:
                    reduce_unknown = True
            if event.fill_delta_qty is not None:
                if previous_filled is None or cumulative_fill is None or event.fill_delta_qty != _decimal_difference(cumulative_fill, previous_filled):
                    return _inconsistent(order_id, events_tuple, "FILL_DELTA_MISMATCH")
            if event.event_type != "FULL_FILL" and first_fill is None and event.timestamp_utc is not None and not fill_timestamp_unknown:
                first_fill = event.timestamp_utc
        if event.event_type == "FULL_FILL":
            full_fill = event.timestamp_utc
            if first_fill is None and event.timestamp_utc is not None and not fill_timestamp_unknown:
                first_fill = event.timestamp_utc
        if event.event_type == "CANCEL_REQUESTED":
            state["reserve"] = True
        elif event.event_type == "CANCEL_CONFIRMED":
            state["reserve"] = False
            remaining_cancel = event.remaining_qty if event.remaining_qty is not None else (state["remaining"] if state["fill_known"] else None)
        elif event.event_type == "REPLACE_REQUESTED":
            state["reserve"] = True
        elif event.event_type == "REPLACE_CONFIRMED":
            state["reserve"] = True
        elif event.event_type in {"FULL_FILL", "REJECTED"}:
            state["reserve"] = False
        state["last"] = event.event_type
    state = states[current_revision]
    initial_revision = unique[0].order_revision
    requested = state["requested"] if state["requested"] is not None else (global_requested if current_revision == initial_revision else None)
    filled = state["filled"] if state["fill_known"] else None
    reason = "FILL_QUANTITY_UNKNOWN" if not state["fill_known"] else (None if requested is not None else "REQUESTED_QUANTITY_MISSING")
    ratio = None if requested is None or filled is None else _decimal_ratio(filled, requested)
    terminal = state["last"] in {"FULL_FILL", "CANCEL_CONFIRMED", "REJECTED"}
    status = UNKNOWN if reason == "FILL_QUANTITY_UNKNOWN" else (AVAILABLE if terminal else PARTIAL)
    if reason and status == AVAILABLE:
        status = PARTIAL
    reserve = state["reserve"]
    requested_by_revision = {revision: value["requested"] for revision, value in states.items()}
    return LifecycleResult(status, order_id, events_tuple, requested, filled, ratio, reason, first_fill, full_fill, _seconds(placement, first_fill), _seconds(placement, full_fill), partial_count, remaining_cancel, remaining_replace, reserve, "ACTIVE" if reserve else "RELEASED", current_revision, tuple(links), None if reduce_unknown else (reduce_qty if reduce_count else None), reduce_count, _digest([event.digest for event in events_tuple], "portfolio_execution_lifecycle_v1"), tuple(duplicates), (), requested_by_revision, ())


reduce_lifecycle = reduce_order_lifecycle


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    symbol: str
    side: str
    timeframe: str
    regime: str | None = None
    requested_qty: Decimal | None = None
    filled_qty: Decimal | None = None
    fill_ratio: Decimal | None = None
    first_fill_seconds: Decimal | None = None
    full_fill_seconds: Decimal | None = None
    partial_count: int = 0
    remaining_at_cancel: Decimal | None = None
    status: str = AVAILABLE
    source_digest: str = ""
    proxy_value: Decimal | None = None
    quantity_unit: str = "contracts"
    regime_version: str | None = REGIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in {AVAILABLE, PARTIAL, UNKNOWN, INCONSISTENT}:
            raise ValueError("observation status is invalid")
        if any(type(value) is not str or not value for value in (self.symbol, self.timeframe, self.quantity_unit)):
            raise ValueError("observation stratum is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("observation side is invalid")
        if type(self.source_digest) is not str:
            raise TypeError("observation source_digest must be a string")
        if type(self.partial_count) is not int or self.partial_count < 0:
            raise ValueError("observation partial_count must be a non-negative integer")
        if self.regime is not None and (type(self.regime) is not str or not self.regime or type(self.regime_version) is not str or not self.regime_version):
            raise ValueError("observation regime_version is required when regime is present")
        if self.regime is None:
            object.__setattr__(self, "regime_version", None)
        for name in ("requested_qty", "filled_qty", "fill_ratio", "first_fill_seconds", "full_fill_seconds", "remaining_at_cancel", "proxy_value"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name, positive=name == "requested_qty", nonnegative=name != "requested_qty"))
        if self.fill_ratio is not None and self.fill_ratio > 1:
            raise ValueError("observation fill_ratio must be within [0, 1]")
        if self.requested_qty is not None and self.filled_qty is not None and self.filled_qty > self.requested_qty:
            raise ValueError("observation filled_qty exceeds requested_qty")
        if self.requested_qty is not None and self.remaining_at_cancel is not None and self.remaining_at_cancel > self.requested_qty:
            raise ValueError("observation remaining exceeds requested_qty")
        if self.requested_qty is not None and self.filled_qty is not None and self.remaining_at_cancel is not None and _decimal_difference(self.requested_qty, self.filled_qty) != self.remaining_at_cancel:
            raise ValueError("observation filled and remaining quantities are inconsistent")
        if self.requested_qty is not None and self.filled_qty is not None and self.fill_ratio is not None and _decimal_ratio(self.filled_qty, self.requested_qty) != self.fill_ratio:
            raise ValueError("observation fill_ratio is inconsistent")
        if self.first_fill_seconds is not None and self.full_fill_seconds is not None and self.first_fill_seconds > self.full_fill_seconds:
            raise ValueError("observation first latency exceeds full latency")
        if self.status == AVAILABLE and any(value is None for value in (self.requested_qty, self.fill_ratio, self.first_fill_seconds, self.full_fill_seconds)):
            raise ValueError("available observation is incomplete")

    @classmethod
    def from_item(cls, item: LifecycleResult | Mapping[str, Any]) -> "ExecutionObservation":
        if isinstance(item, LifecycleResult):
            event = item.events[0]
            return cls(event.symbol, event.side, event.timeframe, event.regime, item.requested_qty, item.filled_qty, item.fill_ratio, item.time_to_first_fill, item.time_to_full_fill, item.partial_count, item.remaining_at_cancel, item.status, item.source_digest, quantity_unit=event.quantity_unit, regime_version=event.regime_version)
        if not isinstance(item, Mapping):
            raise TypeError("execution observation must be a mapping")
        _stable(item)
        requested = _observation_alias(item, "requested_qty", "requested_qty", "requested", default=None, normalize=lambda value: None if value is None else _decimal(value, "requested_qty", positive=True))
        filled = _observation_alias(item, "filled_qty", "filled_qty", "filled", default=None, normalize=lambda value: None if value is None else _decimal(value, "filled_qty", nonnegative=True))
        fill_ratio = _observation_alias(item, "fill_ratio", "fill_ratio", "ratio", default=None, normalize=lambda value: None if value is None else _decimal(value, "fill_ratio", nonnegative=True))
        first_fill = _observation_alias(item, "first_fill_seconds", "first_fill_seconds", "time_to_first_fill", default=None, normalize=lambda value: None if value is None else _decimal(value, "first_fill_seconds", nonnegative=True))
        full_fill = _observation_alias(item, "full_fill_seconds", "full_fill_seconds", "time_to_full_fill", default=None, normalize=lambda value: None if value is None else _decimal(value, "full_fill_seconds", nonnegative=True))
        remaining = _observation_alias(item, "remaining_at_cancel", "remaining_at_cancel", "remaining_on_cancel", default=None, normalize=lambda value: None if value is None else _decimal(value, "remaining_at_cancel", nonnegative=True))
        source_digest = _observation_alias(item, "source_digest", "source_digest", "digest", default="", normalize=lambda value: _observation_text(value, "source_digest"))
        proxy_value = _observation_alias(item, "proxy_value", "proxy_value", "proxy", default=None, normalize=lambda value: None if value is None else _decimal(value, "proxy_value", nonnegative=True))
        quantity_unit = _observation_alias(item, "quantity_unit", "quantity_unit", "unit", default="contracts", normalize=lambda value: _observation_text(value, "quantity_unit", nonempty=True))
        status = _observation_alias(item, "status", "status", default=_MISSING, normalize=lambda value: _observation_text(value, "status", nonempty=True))
        symbol = _observation_alias(item, "symbol", "symbol", default=_MISSING, normalize=lambda value: _observation_text(value, "symbol", nonempty=True))
        side = _observation_alias(item, "side", "side", default=_MISSING, normalize=lambda value: _observation_text(value, "side", nonempty=True).upper())
        timeframe = _observation_alias(item, "timeframe", "timeframe", default=_MISSING, normalize=lambda value: _observation_text(value, "timeframe", nonempty=True))
        regime = _observation_alias(item, "regime", "regime", default=None, normalize=lambda value: _observation_text(value, "regime", allow_none=True, nonempty=True))
        regime_version = _observation_alias(item, "regime_version", "regime_version", default=REGIME_SCHEMA_VERSION, normalize=lambda value: _observation_text(value, "regime_version", allow_none=True, nonempty=True))
        partial_count = _observation_alias(item, "partial_count", "partial_count", default=0)
        if status is _MISSING:
            status = AVAILABLE if all(value is not None for value in (requested, fill_ratio, first_fill, full_fill)) else UNKNOWN
        return cls(
            symbol,
            side,
            timeframe,
            regime,
            requested,
            filled,
            fill_ratio,
            first_fill,
            full_fill,
            partial_count,
            remaining,
            status,
            source_digest,
            proxy_value,
            quantity_unit,
            regime_version,
        )


@dataclass(frozen=True, slots=True)
class CalibrationCurve:
    stratum: str
    symbol: str
    side: str
    timeframe: str
    regime: str | None
    sample_count: int
    coverage: Decimal
    units: str
    source_digest: str
    calibration_version: str
    policy_version: str
    applicability: str
    confidence: str
    incomplete_count: int
    request_range: tuple[Decimal, Decimal] | None
    degradation_points: tuple[Mapping[str, Any], ...]
    proxy_vs_empirical: Mapping[str, Any] = field(default_factory=dict)
    regime_version: str | None = REGIME_SCHEMA_VERSION
    stable_bound: bool = False

    def __post_init__(self) -> None:
        _stable(self)
        if self.regime is not None and (not isinstance(self.regime_version, str) or not self.regime_version):
            raise ValueError("curve regime_version is required when regime is present")
        if self.regime is None:
            object.__setattr__(self, "regime_version", None)
        if type(self.stable_bound) is not bool:
            raise TypeError("stable_bound must be a bool")
        _stable(self.degradation_points)
        _stable(self.proxy_vs_empirical)
        object.__setattr__(self, "degradation_points", tuple(_freeze(point) for point in self.degradation_points))
        object.__setattr__(self, "proxy_vs_empirical", _freeze(dict(self.proxy_vs_empirical)))


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    status: str
    disposition: str
    calibration_version: str
    source_digest: str
    base_curves: tuple[CalibrationCurve, ...]
    regime_curves: tuple[CalibrationCurve, ...]
    applicability: str
    confidence: str
    content_digest: str = ""
    policy_version: str = ""

    def __post_init__(self) -> None:
        _stable(self)

    @property
    def curves(self) -> tuple[CalibrationCurve, ...]:
        return self.base_curves + self.regime_curves

    @property
    def calibration_digest(self) -> str:
        return self.content_digest

    @property
    def digest(self) -> str:
        return self.content_digest

    @property
    def usable(self) -> bool:
        return False

    @property
    def approved(self) -> bool:
        return False


def _curve(key: tuple[str, str, str, str | None, str], values: list[ExecutionObservation], *, version: str, policy_version: str, source_digest: str, policy: Mapping[str, Any], proxy: Mapping[str, Any] | Decimal | int | str | None) -> CalibrationCurve:
    symbol, side, timeframe, regime, regime_version = key
    complete = [item for item in values if item.fill_ratio is not None]
    incomplete = lambda item: item.status != AVAILABLE or any(metric is None for metric in (item.fill_ratio, item.first_fill_seconds, item.full_fill_seconds))
    quantities = [item.requested_qty for item in values if item.requested_qty is not None]
    points: list[Mapping[str, Any]] = []
    for quantity in sorted(set(quantities)):
        subset = [item for item in values if item.requested_qty == quantity]
        ratios = [item.fill_ratio for item in subset if item.fill_ratio is not None]
        first_latencies = [item.first_fill_seconds for item in subset if item.first_fill_seconds is not None]
        full_latencies = [item.full_fill_seconds for item in subset if item.full_fill_seconds is not None]
        remaining = [item.remaining_at_cancel for item in subset if item.remaining_at_cancel is not None]
        incomplete_count = sum(1 for item in subset if incomplete(item))
        points.append(MappingProxyType({
            "requested_qty": quantity,
            "sample_count": len(subset),
            "fill_ratio": _decimal_mean(ratios),
            "fill_ratio_mean": _decimal_mean(ratios),
            "first_fill_seconds": _decimal_mean(first_latencies),
            "time_to_first_fill": _decimal_mean(first_latencies),
            "full_fill_seconds": _decimal_mean(full_latencies),
            "time_to_full_fill": _decimal_mean(full_latencies),
            "incomplete_count": incomplete_count,
            "incomplete_rate": _decimal_ratio(incomplete_count, len(subset)) if subset else Decimal("0"),
            "remaining_at_cancel": _decimal_mean(remaining),
        }))
    empirical = _decimal_mean(item.fill_ratio for item in complete if item.fill_ratio is not None)
    proxy_item: Any = proxy
    if isinstance(proxy, Mapping):
        lookup_keys = ["|".join(filter(None, key[:3]))]
        if key[3] is not None:
            lookup_keys.append("|".join(filter(None, key[:4])))
        proxy_item = next((proxy.get(candidate) for candidate in lookup_keys if candidate in proxy), None)
    if proxy_item is None or empirical is None:
        comparison = {}
    else:
        proxy_value = _decimal(proxy_item, "proxy", nonnegative=True)
        comparison = {"proxy": proxy_value, "empirical": empirical, "difference": _decimal_difference(empirical, proxy_value)}
    applicability = policy.get("applicability", "OPEN_POLICY")
    confidence = policy.get("confidence", "UNKNOWN")
    units = ",".join(sorted({item.quantity_unit for item in values}))
    minimum_samples = policy.get("minimum_sample_count")
    stable_bound = (
        type(minimum_samples) is int and minimum_samples > 0 and len(values) >= minimum_samples
        and len({item.quantity_unit for item in values}) == 1
        and applicability != "OPEN_POLICY" and confidence != "UNKNOWN"
        and not any(incomplete(item) for item in values)
    )
    stratum_parts = [symbol, side, timeframe]
    if regime is not None:
        stratum_parts.extend((regime, regime_version))
    return CalibrationCurve("|".join(part for part in stratum_parts if part is not None), symbol, side, timeframe, regime, len(values), _decimal_ratio(len(complete), len(values)) if values else Decimal("0"), units, source_digest, version, policy_version, applicability, confidence, sum(1 for item in values if incomplete(item)), (min(quantities), max(quantities)) if quantities else None, tuple(points), comparison, regime_version, stable_bound)


def calibrate_execution(observations: Iterable[ExecutionObservation | LifecycleResult | Mapping[str, Any]], *, policy: Mapping[str, Any] | None = None, calibration_version: str = "execution_calibration_v1", proxy: Mapping[str, Any] | Decimal | int | str | None = None, source_digest: str | None = None) -> CalibrationResult:
    if not isinstance(calibration_version, str) or not calibration_version:
        raise ValueError("calibration_version must be a non-empty string")
    if source_digest is not None and (type(source_digest) is not str or not source_digest):
        raise TypeError("source_digest must be None or a non-empty string")
    if not isinstance(policy, Mapping):
        raise TypeError("calibration policy must be a mapping with a version")
    _stable(policy)
    _stable(proxy)
    policy_version = policy.get("version", policy.get("policy_version"))
    if not isinstance(policy_version, str) or not policy_version:
        raise ValueError("calibration policy version must be a non-empty string")
    applicability = policy.get("applicability", "OPEN_POLICY")
    confidence = policy.get("confidence", "UNKNOWN")
    if type(applicability) is not str or not applicability:
        raise TypeError("calibration applicability must be a non-empty string")
    if type(confidence) is not str or not confidence:
        raise TypeError("calibration confidence must be a non-empty string")
    if "minimum_sample_count" in policy and (type(policy["minimum_sample_count"]) is not int or policy["minimum_sample_count"] <= 0):
        raise ValueError("minimum_sample_count must be a positive integer")
    values = tuple(sorted((ExecutionObservation.from_item(item) for item in observations), key=lambda item: (item.symbol, item.side, item.timeframe, item.regime or "", item.requested_qty if item.requested_qty is not None else Decimal("-1"), item.source_digest, _digest(item, "portfolio_execution_observation_v1"))))
    if not values:
        digest = _digest([], "portfolio_execution_calibration_v1")
        content = _digest({"calibration_version": calibration_version, "policy": policy, "proxy": proxy, "source_digest": digest, "base": (), "regime": ()}, "portfolio_execution_calibration_v1")
        return CalibrationResult(UNKNOWN, RESEARCH_ONLY, calibration_version, digest, (), (), applicability, confidence, content, policy_version)
    source = source_digest if source_digest is not None else _digest(values, "portfolio_execution_observations_v1")
    base_groups: dict[tuple[str, str, str, None, str], list[ExecutionObservation]] = {}
    regime_groups: dict[tuple[str, str, str, str, str], list[ExecutionObservation]] = {}
    for item in values:
        base_groups.setdefault((item.symbol, item.side, item.timeframe, None, None), []).append(item)
        if item.regime:
            regime_groups.setdefault((item.symbol, item.side, item.timeframe, item.regime, item.regime_version or REGIME_SCHEMA_VERSION), []).append(item)
    base = tuple(_curve(key, items, version=calibration_version, policy_version=policy_version, source_digest=source, policy=policy, proxy=proxy) for key, items in sorted(base_groups.items()))
    regime = tuple(_curve(key, items, version=calibration_version, policy_version=policy_version, source_digest=source, policy=policy, proxy=proxy) for key, items in sorted(regime_groups.items()) if any(base_key[:3] == key[:3] for base_key in base_groups))
    status = AVAILABLE if all(item.status == AVAILABLE and item.fill_ratio is not None and item.first_fill_seconds is not None and item.full_fill_seconds is not None for item in values) else PARTIAL
    content = _digest({"calibration_version": calibration_version, "policy": policy, "proxy": proxy, "source_digest": source, "base": base, "regime": regime}, "portfolio_execution_calibration_v1")
    return CalibrationResult(status, RESEARCH_ONLY, calibration_version, source, base, regime, applicability, confidence, content, policy_version)


build_execution_calibration = calibrate_execution


@dataclass(frozen=True, slots=True)
class RefinementEvidence:
    kind: str
    value: Decimal | None
    unit: str | None
    currency: str | None
    provenance: str | None
    observed_at: datetime | None
    expires_at: datetime | None
    status: str
    reason: str | None
    content_digest: str

    def __post_init__(self) -> None:
        _stable(self)

    @property
    def evidence_class(self) -> str:
        return "OBSERVED" if self.status == AVAILABLE else UNKNOWN

    @property
    def usable(self) -> bool:
        return False

    @property
    def approved(self) -> bool:
        return False


def refinement_evidence(kind: str, value: Any = None, *, unit: str | None = None, currency: str | None = None, provenance: str | None = None, observed_at: Any = None, expires_at: Any = None, now: Any = None, conflicting: bool = False) -> RefinementEvidence:
    if type(kind) is not str or not kind:
        raise TypeError("refinement kind must be a non-empty string")
    if any(type(item) is not str or not item for item in (unit, currency, provenance) if item is not None):
        raise TypeError("refinement metadata must be non-empty strings")
    if type(conflicting) is not bool:
        raise TypeError("conflicting must be a bool")
    kind_value = kind
    observed = _timestamp(observed_at, "observed_at")
    expires = _timestamp(expires_at, "expires_at")
    reason: str | None = None
    parsed: Decimal | None = None
    if kind_value not in REFINEMENT_KINDS:
        raise ValueError("unknown refinement kind")
    if conflicting:
        reason = "CONFLICTING_FACT"
    elif value is None or unit is None or currency is None or provenance is None or observed is None or expires is None:
        reason = "MISSING_FACT"
    else:
        try:
            parsed = _decimal(value, "refinement value", positive=True)
        except ValueError:
            reason = "NON_POSITIVE_FACT"
        if reason is None and now is None:
            reason = "MISSING_ASOF"
        elif reason is None:
            current = _timestamp(now, "now")
            if current < observed:
                reason = "FUTURE_FACT"
            elif expires <= observed or current >= expires:
                reason = "STALE_FACT"
    status = UNKNOWN if reason else AVAILABLE
    digest = _digest({"kind": kind_value, "value": parsed, "unit": unit, "currency": currency, "provenance": provenance, "observed_at": observed, "expires_at": expires, "status": status, "reason": reason}, "portfolio_refinement_evidence_v1")
    return RefinementEvidence(kind_value, parsed if status == AVAILABLE else None, unit, currency, provenance, observed, expires, status, reason, digest)


typed_refinement_evidence = refinement_evidence


def build_refinement_evidence(facts: Mapping[str, Mapping[str, Any]], *, now: Any = None) -> Mapping[str, RefinementEvidence]:
    result = {kind: refinement_evidence(kind, **dict(value), now=now) for kind, value in facts.items()}
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class CorrelatedStressResult:
    status: str
    disposition: str
    scenario_version: int
    mark: Decimal | None
    equity: Decimal | None
    liquidity_capacity: Decimal | None
    leverage_capacity: Decimal | None
    deepest_fill: Decimal | None
    capacity: Decimal | None
    prior_executable_digest: str | None
    new_executable_digest: str | None
    joint_retest_required: bool
    reason: str | None
    input_digest: str
    margin_result: Any = None
    liquidity_result: Any = None

    def __post_init__(self) -> None:
        _stable(self)

    @property
    def needs_retest(self) -> bool:
        return self.disposition == NEEDS_RETEST

    @property
    def usable(self) -> bool:
        return False

    @property
    def approved(self) -> bool:
        return False

    @property
    def ready(self) -> bool:
        return False

    @property
    def admission_eligible(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class RetestLineage:
    change_kind: str
    disposition: str
    parent_digest: str
    child_digest: str
    parent_record: Mapping[str, Any]
    child_record: Mapping[str, Any]

    def __post_init__(self) -> None:
        _stable(self.parent_record)
        _stable(self.child_record)
        if self.disposition != NEEDS_RETEST:
            raise ValueError("retest lineage must be NEEDS_RETEST")
        object.__setattr__(self, "parent_record", _freeze(dict(self.parent_record)))
        object.__setattr__(self, "child_record", _freeze(dict(self.child_record)))

    @property
    def status(self) -> str:
        return NEEDS_RETEST

    @property
    def usable(self) -> bool:
        return False

    @property
    def approved(self) -> bool:
        return False


def create_retest_lineage(
    previous_evaluation: Mapping[str, Any],
    *,
    sizing: Any = _UNSET,
    capacity: Any = _UNSET,
    reserve: Any = _UNSET,
    margin_envelope: Any = _UNSET,
    change_kind: str | None = None,
    change_value: Any = _UNSET,
) -> RetestLineage:
    """Copy one evaluation into an immutable research-only retest child."""
    if not isinstance(previous_evaluation, Mapping):
        raise TypeError("previous_evaluation must be a mapping")
    _stable(previous_evaluation)
    changes = {
        name: value
        for name, value in (("sizing", sizing), ("capacity", capacity), ("reserve", reserve), ("margin_envelope", margin_envelope))
        if value is not _UNSET
    }
    if change_kind is not None:
        if change_kind not in {"sizing", "capacity", "reserve", "margin_envelope"} or change_value is _UNSET:
            raise ValueError("change_kind and change_value must identify one retest dimension")
        changes[change_kind] = change_value
    if not changes:
        raise ValueError("a retest change is required")
    _stable(changes)
    parent = dict(previous_evaluation)
    parent_digest = str(previous_evaluation.get("evaluation_digest") or _digest(previous_evaluation, "portfolio_execution_evaluation_v1"))
    changed = {**parent, **changes}
    changed["lineage_parent_digest"] = parent_digest
    changed["status"] = NEEDS_RETEST
    changed["disposition"] = NEEDS_RETEST
    changed["research_only"] = True
    changed["approved"] = False
    child_digest = _digest(changed, "portfolio_execution_evaluation_v1")
    changed["evaluation_digest"] = child_digest
    return RetestLineage(str(next(iter(changes))), NEEDS_RETEST, parent_digest, child_digest, parent, changed)


def _amount(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        _stable(value)
        values = [_decimal(value[key], name, nonnegative=True) for key in sorted(value)]
        with _decimal_context(values):
            return sum(values, Decimal("0"))
    return _decimal(value, name, nonnegative=True)


def _gate_dataclass_types() -> frozenset[type[Any]]:
    from .liquidity import LiquidityCeiling, SizingEnvelopeResult
    from .margin import EnvelopeResult, Evidence, MarginComponent, MarginResult

    return frozenset({LiquidityCeiling, SizingEnvelopeResult, EnvelopeResult, Evidence, MarginComponent, MarginResult})


def _snapshot_gate(value: Any) -> Any:
    """Take a canonical, immutable snapshot of repository gate evidence."""
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("gate evidence mapping keys must be strings")
        return {key: _snapshot_gate(item) for key, item in value.items()}
    if is_dataclass(value):
        if type(value) not in _gate_dataclass_types():
            raise TypeError("unsupported gate evidence dataclass")
        return {item.name: _snapshot_gate(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, (list, tuple)):
        return tuple(_snapshot_gate(item) for item in value)
    if isinstance(value, (set, frozenset)):
        raise TypeError("gate evidence cannot contain sets")
    if type(value) in {Decimal, datetime, str, int, bool} or value is None:
        return value
    raise TypeError(f"unsupported gate evidence value: {type(value).__name__}")


def _normalize_gate(value: Any, name: str) -> tuple[str | None, Any]:
    if value is None:
        return None, None
    if type(value) is str:
        if value not in GATE_STATUSES:
            raise ValueError(f"{name} has an invalid gate status")
        return value, value
    if isinstance(value, Mapping):
        snapshot = _snapshot_gate(value)
        status = _resolve_alias(
            f"{name} status",
            (("status", value["status"]) if "status" in value else ("status", _MISSING), ("gate_result", value["gate_result"]) if "gate_result" in value else ("gate_result", _MISSING)),
            default=None,
        )
        if type(status) is not str or status not in GATE_STATUSES:
            raise ValueError(f"{name} must contain an explicit gate status")
        snapshot = {**snapshot, "status": status}
        _stable(snapshot)
        return status, _freeze(snapshot)
    if type(value) in _gate_dataclass_types():
        status = getattr(value, "status", getattr(value, "gate_result", None))
        if type(status) is not str or status not in GATE_STATUSES:
            raise ValueError(f"{name} has an invalid gate status")
        retained = _snapshot_gate(value)
        if not isinstance(retained, Mapping):
            raise TypeError(f"{name} evidence could not be normalized")
        retained = {**retained, "status": status}
        _stable(retained)
        return status, _freeze(dict(retained))
    raise TypeError(f"{name} must be an explicit gate status or evidence record")


def _gate_status(value: Any) -> str | None:
    return _normalize_gate(value, "gate")[0]


def evaluate_correlated_stress(previous_evaluation: Mapping[str, Any], *, mark: Any = None, equity: Any = None, mark_shock: Any = Decimal("0"), equity_shock: Any = Decimal("0"), liquidity_capacity: Any = None, leverage_capacity: Any = None, deepest_fill: Any = None, liquidity_factor: Any = Decimal("1"), leverage_factor: Any = Decimal("1"), margin_result: Any = None, liquidity_result: Any = None, margin_evaluator: Callable[[Mapping[str, Any]], Any] | None = None, liquidity_evaluator: Callable[[Mapping[str, Any]], Any] | None = None) -> CorrelatedStressResult:
    if not isinstance(previous_evaluation, Mapping):
        raise TypeError("previous_evaluation must be a mapping")
    _stable(previous_evaluation)
    mark_value = None if mark is None else _decimal(mark, "mark", positive=True)
    equity_value = None if equity is None else _decimal(equity, "equity", positive=True)
    mark_delta = _decimal(mark_shock, "mark_shock")
    equity_delta = _decimal(equity_shock, "equity_shock")
    liq_factor = _decimal(liquidity_factor, "liquidity_factor", positive=True)
    lev_factor = _decimal(leverage_factor, "leverage_factor", positive=True)
    if liq_factor > 1 or lev_factor > 1:
        raise ValueError("stress factors must be in (0, 1]")
    deepest = _amount(deepest_fill, "deepest_fill")
    liquidity = _amount(liquidity_capacity, "liquidity_capacity")
    leverage = _amount(leverage_capacity, "leverage_capacity")
    if mark_value is not None and mark_delta <= -1:
        raise ValueError("mark shock would make mark non-positive")
    if equity_value is not None and equity_delta <= -1:
        raise ValueError("equity shock would make equity non-positive")
    arithmetic_values = tuple(
        value
        for value in (mark_value, equity_value, mark_delta, equity_delta, liq_factor, lev_factor, deepest, liquidity, leverage, Decimal("1"))
        if value is not None
    )
    with _decimal_context(arithmetic_values):
        stressed_mark = mark_value * (Decimal("1") + mark_delta) if mark_value is not None else None
        stressed_equity = equity_value * (Decimal("1") + equity_delta) if equity_value is not None else None
        stressed_liquidity = liquidity * liq_factor if liquidity is not None else None
        stressed_leverage = leverage * lev_factor if leverage is not None else None
        candidates = [item for item in (deepest, stressed_liquidity, stressed_leverage) if item is not None]
        capacity = min(candidates) if candidates else None
    if margin_result is None and margin_evaluator is not None:
        margin_result = margin_evaluator({"mark": stressed_mark, "equity": stressed_equity, "deepest_fill": deepest})
    if liquidity_result is None and liquidity_evaluator is not None:
        liquidity_result = liquidity_evaluator({"capacity": stressed_liquidity, "deepest_fill": deepest})
    margin_status, margin_evidence = _normalize_gate(margin_result, "margin_result")
    liquidity_status, liquidity_evidence = _normalize_gate(liquidity_result, "liquidity_result")
    gate_statuses = {margin_status, liquidity_status}
    reason = None
    if capacity is None or deepest is None:
        status = UNKNOWN
        reason = "STRESS_INPUT_MISSING"
    elif margin_status is None or liquidity_status is None or any(status not in GATE_STATUSES for status in gate_statuses):
        status = UNKNOWN
        reason = "STRESS_GATE_MISSING"
    elif "UNKNOWN" in gate_statuses:
        status = UNKNOWN
        reason = "STRESS_GATE_UNKNOWN"
    elif "FAIL" in gate_statuses:
        status = "FAIL"
        reason = "STRESS_GATE_FAILED"
    else:
        status = AVAILABLE
    if status != AVAILABLE:
        capacity = None
    prior_capacity = _amount(previous_evaluation.get("capacity", previous_evaluation.get("size")), "prior_capacity")
    changed = status == AVAILABLE and capacity is not None and (prior_capacity is None or capacity != prior_capacity)
    prior_digest = str(previous_evaluation.get("executable_digest") or previous_evaluation.get("prior_executable_digest") or _digest(previous_evaluation, "portfolio_executable_v1"))
    new_digest = _digest({"capacity": capacity, "mark": stressed_mark, "equity": stressed_equity, "liquidity": stressed_liquidity, "leverage": stressed_leverage, "deepest_fill": deepest}, "portfolio_executable_v1") if status == AVAILABLE and capacity is not None else None
    disposition = NEEDS_RETEST if changed else RESEARCH_ONLY
    input_digest = _digest({
        "previous": previous_evaluation, "mark": mark_value, "equity": equity_value,
        "mark_shock": mark_delta, "equity_shock": equity_delta,
        "liquidity_capacity": liquidity, "leverage_capacity": leverage,
        "deepest_fill": deepest, "liquidity_factor": liq_factor, "leverage_factor": lev_factor,
        "margin_gate": margin_evidence, "liquidity_gate": liquidity_evidence,
    }, "portfolio_correlated_stress_v1")
    return CorrelatedStressResult(status, disposition, STRESS_SCHEMA_VERSION, stressed_mark, stressed_equity, stressed_liquidity, stressed_leverage, deepest, capacity, prior_digest, new_digest, changed, reason, input_digest, margin_evidence, liquidity_evidence)


correlated_stress = evaluate_correlated_stress
stress_evaluate = evaluate_correlated_stress


__all__ = [
    "AVAILABLE", "CALIBRATION_SCHEMA_VERSION", "CalibrationCurve", "CalibrationResult", "CorrelatedStressResult", "EVENT_TYPES", "EXECUTION_EVIDENCE_SCHEMA_VERSION", "ExecutionObservation", "INCONSISTENT", "LifecycleResult", "NEEDS_RETEST", "ORDER_EVENT_SCHEMA_VERSION", "OrderEvent", "PARTIAL", "REFINEMENT_KINDS", "REGIME_SCHEMA_VERSION", "RESEARCH_ONLY", "RefinementEvidence", "RetestLineage", "STRESS_SCHEMA_VERSION", "UNKNOWN", "build_execution_calibration", "build_refinement_evidence", "calibrate_execution", "correlated_stress", "create_retest_lineage", "evaluate_correlated_stress", "reduce_lifecycle", "reduce_order_lifecycle", "refinement_evidence", "stress_evaluate", "typed_refinement_evidence",
]
