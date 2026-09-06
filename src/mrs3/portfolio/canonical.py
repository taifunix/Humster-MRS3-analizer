"""The single, explicit canonical identity contract for portfolio artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import (
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    InvalidOperation,
    localcontext,
)
from typing import Any, Literal, Mapping


PORTFOLIO_DISPOSITION_V1 = frozenset(
    {
        "RESEARCH_ONLY",
        "RECOMMENDATION_READY",
        "NEEDS_RETEST",
        "NEEDS_RESCREEN",
        "INSUFFICIENT_EVIDENCE",
        "NONDETERMINISTIC_RESULT",
    }
)
PORTFOLIO_GATE_RESULT_V1 = frozenset({"PASS", "FAIL", "UNKNOWN"})
PORTFOLIO_EVIDENCE_CLASS_V1 = frozenset(
    {"OBSERVED", "CALCULATED", "CONSERVATIVE_BOUND", "COARSE_ESTIMATE", "UNKNOWN"}
)
PORTFOLIO_CAPABILITY_RESULT_V1 = frozenset(
    {"CONFIRMED_CAPABILITY", "APPROVED_CONSERVATIVE_BOUND", "BLOCKING_UNKNOWN"}
)
PORTFOLIO_REASON_V1 = frozenset(
    {
        "TURNOVER_MISSING",
        "TURNOVER_STALE",
        "TURNOVER_REQUEST_FAILED",
        "LIQUIDITY_MISSING",
        "LIQUIDITY_STALE",
        "LIQUIDITY_QUALITY_INSUFFICIENT",
        "FEE_RATE_UNKNOWN",
        "SIZING_ENVELOPE_UNBOUNDED",
        "EQUITY_DENOMINATOR_INVALID",
        "EQUITY_PATH_MISSING",
        "EQUITY_COVERAGE_INSUFFICIENT",
        "LEVERAGE_MISMATCH",
        "POST_ROUNDING_MINIMUM",
        "POST_ROUNDING_GEOMETRY",
        "ENUMERATION_FALLBACK_USED",
        "MARGIN_BOUND_FAILED",
        "MARGIN_BOUND_UNAVAILABLE",
        "VALIDATION_FAILED",
        "NO_VALIDATION_PASS",
        "SEMANTIC_RESULT_DIVERGENCE",
        "EXECUTABLE_PAYLOAD_CHANGED",
        "PORTFOLIO_SET_CHANGED",
        "OPEN_POLICY",
        "LOCK_OWNER_UNVERIFIABLE",
        "LOCK_MANUAL_CLEAR",
    }
)

_ENUMS = {
    "portfolio_disposition_v1": PORTFOLIO_DISPOSITION_V1,
    "portfolio_gate_result_v1": PORTFOLIO_GATE_RESULT_V1,
    "portfolio_evidence_class_v1": PORTFOLIO_EVIDENCE_CLASS_V1,
    "portfolio_capability_result_v1": PORTFOLIO_CAPABILITY_RESULT_V1,
    "portfolio_reason_v1": PORTFOLIO_REASON_V1,
}

_DECIMAL_ROUNDING_MODES = frozenset(
    {
        ROUND_05UP,
        ROUND_CEILING,
        ROUND_DOWN,
        ROUND_FLOOR,
        ROUND_HALF_DOWN,
        ROUND_HALF_EVEN,
        ROUND_HALF_UP,
        ROUND_UP,
    }
)


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


@dataclass(frozen=True)
class Unknown:
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("UNKNOWN requires a stable non-empty reason")


@dataclass(frozen=True)
class TypedValue:
    type_tag: str
    value: Any
    unit_tag: str

    def __post_init__(self) -> None:
        if not self.type_tag or not self.unit_tag:
            raise ValueError("type_tag and unit_tag are required")


@dataclass(frozen=True)
class DecimalValue:
    value: Decimal
    scale: int
    rounding: str | None
    unit_tag: str

    def __post_init__(self) -> None:
        if isinstance(self.value, float):
            raise TypeError("DecimalValue does not accept binary floats")
        if not isinstance(self.value, Decimal):
            raise TypeError("DecimalValue requires Decimal or a decimal string")
        if self.value.is_finite() is False:
            raise ValueError("DecimalValue requires a finite Decimal")
        if not isinstance(self.scale, int) or self.scale < 0:
            raise ValueError("decimal scale must be a non-negative integer")
        if not self.unit_tag:
            raise ValueError("decimal unit_tag is required")
        if self.rounding is not None and (
            not isinstance(self.rounding, str) or self.rounding not in _DECIMAL_ROUNDING_MODES
        ):
            raise ValueError(f"unknown decimal rounding mode: {self.rounding}")

    def canonical(self) -> str:
        quantum = Decimal(1).scaleb(-self.scale)
        with localcontext() as context:
            context.prec = max(32, len(self.value.as_tuple().digits) + self.scale + 8)
            try:
                rounded = self.value.quantize(quantum, rounding=self.rounding)
            except InvalidOperation as exc:
                raise ValueError("decimal cannot be represented at declared scale") from exc
        if self.rounding is None and rounded != self.value:
            raise ValueError("decimal has excess precision without declared rounding")
        return format(rounded, f".{self.scale}f")


@dataclass(frozen=True)
class TimestampValue:
    value: datetime
    precision: Literal["seconds", "milliseconds", "microseconds"]

    def __post_init__(self) -> None:
        if self.value.tzinfo is None or self.value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        if self.precision not in {"seconds", "milliseconds", "microseconds"}:
            raise ValueError("unsupported timestamp precision")
        microsecond = self.value.microsecond
        if self.precision == "seconds" and microsecond:
            raise ValueError("timestamp has excess precision")
        if self.precision == "milliseconds" and microsecond % 1000:
            raise ValueError("timestamp has excess precision")

    def canonical(self) -> str:
        utc = self.value.astimezone(timezone.utc)
        if self.precision == "seconds":
            return utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.precision == "milliseconds":
            return utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class CanonicalList:
    items: tuple[Any, ...]
    ordering: Literal["identity", "timestamp_ordinal", "load"]

    def __post_init__(self) -> None:
        if self.ordering not in {"identity", "timestamp_ordinal", "load"}:
            raise ValueError("list ordering must be explicit and versioned")


@dataclass(frozen=True)
class CanonicalEnvelope:
    schema_id: str
    schema_version: int | str
    type_tag: str
    unit_tag: str
    payload: Any
    presentation_exclusions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.schema_id or not self.type_tag or not self.unit_tag:
            raise ValueError("schema_id, type_tag and unit_tag are required")
        if any(not isinstance(path, str) or not path for path in self.presentation_exclusions):
            raise ValueError("presentation exclusions must be explicit non-empty paths")


def typed_value(type_tag: str, value: Any, *, unit: str) -> TypedValue:
    return TypedValue(type_tag, value, unit)


def unknown_value(reason: str) -> Unknown:
    return Unknown(reason)


def decimal_value(value: Decimal | str | int, *, scale: int, unit: str, rounding: str | None = None) -> DecimalValue:
    if isinstance(value, float):
        raise TypeError("decimal_value does not accept binary floats")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TypeError("decimal_value requires Decimal, integer, or decimal string") from exc
    return DecimalValue(parsed, scale, rounding, unit)


def timestamp_value(value: datetime, *, precision: Literal["seconds", "milliseconds", "microseconds"]) -> TimestampValue:
    return TimestampValue(value, precision)


def list_value(items: Any, *, ordering: Literal["identity", "timestamp_ordinal", "load"]) -> CanonicalList:
    if isinstance(items, (str, bytes)):
        raise TypeError("canonical list requires a sequence of items")
    return CanonicalList(tuple(items), ordering)


def enum_value(enum_name: str, value: str) -> TypedValue:
    allowed = _ENUMS.get(enum_name)
    if allowed is None:
        raise ValueError(f"unknown enum contract: {enum_name}")
    if value not in allowed:
        raise ValueError(f"invalid {enum_name} value: {value}")
    return TypedValue(enum_name, value, "1")


def _typed_identity(value: Any) -> bytes:
    if not isinstance(value, TypedValue):
        raise ValueError("identity ordering requires a typed identity")
    return _json_bytes(_encode(value, "", frozenset()))


def _list_sort_key(item: Any, ordering: str) -> tuple[Any, ...] | bytes:
    if not isinstance(item, Mapping):
        raise ValueError("ordered list items must be mappings")
    if ordering == "identity":
        if "identity" not in item:
            raise ValueError("identity ordering requires identity")
        return _typed_identity(item["identity"])
    if ordering == "timestamp_ordinal":
        timestamp = item.get("timestamp_utc")
        ordinal = item.get("source_ordinal")
        if not isinstance(timestamp, TimestampValue):
            raise ValueError("timestamp_ordinal ordering requires timestamp_utc")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise ValueError("timestamp_ordinal ordering requires non-negative source_ordinal")
        return timestamp.canonical(), ordinal
    required = ("symbol", "direction", "member_identity")
    if any(key not in item for key in required):
        raise ValueError("load ordering requires symbol, direction and member_identity")
    return tuple(_json_bytes(_encode(item[key], "", frozenset())) for key in required)


def _ordered_items(value: CanonicalList) -> list[Any]:
    keyed = [(_list_sort_key(item, value.ordering), item) for item in value.items]
    keyed.sort(key=lambda pair: pair[0])
    if any(keyed[index][0] == keyed[index - 1][0] for index in range(1, len(keyed))):
        raise ValueError("canonical list ordering key must be unique")
    return [item for _, item in keyed]


def _encode(value: Any, path: str, exclusions: frozenset[str]) -> Any:
    if value is MISSING:
        return {"state": "MISSING"}
    if isinstance(value, Unknown):
        return {"reason": value.reason, "state": "UNKNOWN"}
    if isinstance(value, TypedValue):
        return {"type": value.type_tag, "unit": value.unit_tag, "value": _encode(value.value, path, exclusions)}
    if isinstance(value, DecimalValue):
        return {"rounding": value.rounding, "scale": value.scale, "type": "decimal", "unit": value.unit_tag, "value": value.canonical()}
    if isinstance(value, TimestampValue):
        return {"precision": value.precision, "type": "timestamp", "unit": "UTC", "value": value.canonical()}
    if isinstance(value, CanonicalList):
        items = _ordered_items(value)
        return [_encode(item, f"{path}[{index}]", exclusions) for index, item in enumerate(items)]
    if isinstance(value, Mapping):
        result = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("canonical object keys must be strings")
            child_path = f"{path}.{key}" if path else key
            if child_path in exclusions:
                continue
            result[key] = _encode(value[key], child_path, exclusions)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError("lists require an explicit versioned CanonicalList ordering")
    if isinstance(value, float):
        raise TypeError("bare floats require DecimalValue")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_bytes_v1(envelope: CanonicalEnvelope) -> bytes:
    if not isinstance(envelope, CanonicalEnvelope):
        raise TypeError("canonical_digest_v1 requires a CanonicalEnvelope")
    exclusions = frozenset(envelope.presentation_exclusions)
    value = {
        "digest_contract": "canonical_digest_v1",
        "payload": {"type": envelope.type_tag, "unit": envelope.unit_tag, "value": _encode(envelope.payload, "", exclusions)},
        "presentation_exclusions": list(envelope.presentation_exclusions),
        "schema": {"id": envelope.schema_id, "version": envelope.schema_version},
    }
    return _json_bytes(value)


def canonical_json_v1(envelope: CanonicalEnvelope) -> str:
    return canonical_bytes_v1(envelope).decode("utf-8")


def canonical_digest_v1(envelope: CanonicalEnvelope) -> str:
    return hashlib.sha256(canonical_bytes_v1(envelope)).hexdigest()
