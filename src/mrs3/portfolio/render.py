"""Pure fixture renderer for one exact M4 payload per symbol."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .canonical import PORTFOLIO_REASON_V2
from .search import (
    PairSlot,
    REASON_ENUM_VERSION,
    UNKNOWN,
    _capability_ok,
    _decimal,
    _first,
    _freeze,
    _get,
    _plain,
    _stable,
    _thaw,
    _typed_json,
    _valid_identity,
    build_pair_slots,
)


@dataclass(frozen=True)
class RenderResult:
    status: str
    reason: str | None = None
    detail: str | None = None
    payloads: tuple[Mapping[str, Any], ...] = ()
    json_by_symbol: Mapping[str, str] | None = None
    comparisons: tuple[bool, ...] = ()
    reason_enum_version: str = REASON_ENUM_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "payloads", tuple(_freeze(payload) for payload in self.payloads))
        object.__setattr__(self, "json_by_symbol", MappingProxyType(dict(self.json_by_symbol or {})))
        if self.reason is not None and self.reason not in PORTFOLIO_REASON_V2:
            object.__setattr__(self, "reason", _stable(self.reason))


def _manifest(capability: Mapping[str, Any]) -> dict[str, str] | None:
    fields = capability.get("physical_fields", capability.get("physical_field_names"))
    if not isinstance(fields, Mapping):
        return None
    required = ("symbol", "leverage", "long", "short", "close", "opposite_policy")
    if any(not isinstance(fields.get(key), str) or not fields.get(key) for key in required):
        return None
    values = [fields[key] for key in required]
    if len(values) != len(set(values)):
        return None
    return {key: fields[key] for key in required}


def _valid_slot(value: Any) -> PairSlot | None:
    selected = _get(value, "slot")
    if isinstance(selected, PairSlot):
        status = _get(value, "status")
        if status is not None and status != "PASS":
            return None
        return _valid_slot(selected)
    if isinstance(value, PairSlot):
        if value.long is not None and (not _valid_identity(value.long) or str(_get(value.long, "side")).upper() != "LONG" or str(_get(value.long, "symbol")) != value.symbol):
            return None
        if value.short is not None and (not _valid_identity(value.short) or str(_get(value.short, "side")).upper() != "SHORT" or str(_get(value.short, "symbol")) != value.symbol):
            return None
        return value if value.long is not None or value.short is not None else None
    if not isinstance(value, Mapping) or ("long" not in value and "short" not in value):
        return None
    long = value.get("long")
    short = value.get("short")
    if long is not None and (not _valid_identity(long) or str(_get(long, "side")).upper() != "LONG"):
        return None
    if short is not None and (not _valid_identity(short) or str(_get(short, "side")).upper() != "SHORT"):
        return None
    symbol = value.get("symbol") or _get(long or short, "symbol")
    if not symbol or any(str(_get(candidate, "symbol")) != str(symbol) for candidate in (long, short) if candidate is not None):
        return None
    return PairSlot(str(symbol), long, short)


def _candidate_payload(candidate: Mapping[str, Any], sized: Any = None) -> dict[str, Any]:
    result = _thaw(candidate)
    if isinstance(result, dict):
        result.pop("status", None)
        result.pop("symbol", None)
        result.pop("dedicated_close", None)
        result.pop("close", None)
        result.pop("leverage", None)
        result.pop("planned_leverage", None)
        result.pop("opposite_policy", None)
    if sized is not None:
        if sized.orders:
            result["orders"] = _thaw(sized.orders)
        if sized.quantity is not None:
            result["quantity"] = sized.quantity
    if isinstance(result.get("orders"), list) and len(result["orders"]) > 1:
        result.pop("quantity", None)
    return result


def _same_typed(left: Any, right: Any) -> bool:
    return _typed_json(left) == _typed_json(right)


def _valid_planned_leverage(value: Any) -> bool:
    parsed = _decimal(value)
    return parsed is not None and parsed > 0


_DECODE_FAILED = object()


def _decode_like(value: Any, template: Any) -> Any:
    """Decode JSON scalar strings only where the source fact is Decimal."""

    if isinstance(template, Decimal):
        if isinstance(value, Decimal):
            return value if value.is_finite() else _DECODE_FAILED
        if not isinstance(value, str):
            return _DECODE_FAILED
        try:
            parsed = Decimal(value)
        except (ArithmeticError, ValueError):
            return _DECODE_FAILED
        return parsed if parsed.is_finite() and format(parsed, "f") == value else _DECODE_FAILED
    if isinstance(template, Mapping):
        if not isinstance(value, Mapping) or set(value) != set(template):
            return _DECODE_FAILED
        decoded = {}
        for key in template:
            item = _decode_like(value[key], template[key])
            if item is _DECODE_FAILED:
                return _DECODE_FAILED
            decoded[key] = item
        return decoded
    if isinstance(template, (list, tuple)):
        if not isinstance(value, (list, tuple)) or len(value) != len(template):
            return _DECODE_FAILED
        decoded = []
        for actual, expected in zip(value, template):
            item = _decode_like(actual, expected)
            if item is _DECODE_FAILED:
                return _DECODE_FAILED
            decoded.append(item)
        return decoded
    if template is None:
        return value if value is None else _DECODE_FAILED
    if isinstance(template, bool):
        return value if type(value) is bool else _DECODE_FAILED
    if isinstance(template, int):
        return value if type(value) is int else _DECODE_FAILED
    if isinstance(template, str):
        return value if type(value) is str else _DECODE_FAILED
    return value if type(value) is type(template) else _DECODE_FAILED


def _reverse_payload_facts(payload: Mapping[str, Any], slot: PairSlot, manifest: Mapping[str, str], leverage: Any, opposite_policy: Any, directions: Mapping[str, Any] | None) -> bool:
    expected_keys = {manifest["symbol"], manifest["leverage"], manifest["opposite_policy"], manifest["close"]}
    expected_candidates: dict[str, Mapping[str, Any]] = {}
    for side, candidate in (("LONG", slot.long), ("SHORT", slot.short)):
        if candidate is not None:
            key = manifest[side.lower()]
            expected_keys.add(key)
            expected_candidates[key] = _candidate_payload(candidate, (directions or {}).get(side))
    if set(payload) != expected_keys:
        return False
    if not _same_typed(payload[manifest["symbol"]], slot.symbol):
        return False
    for key, expected in (
        (manifest["leverage"], leverage),
        (manifest["opposite_policy"], _thaw(opposite_policy)),
    ):
        decoded = _decode_like(payload[key], expected)
        if decoded is _DECODE_FAILED or not _same_typed(decoded, expected):
            return False
    for key, expected in expected_candidates.items():
        decoded = _decode_like(payload[key], expected)
        if decoded is _DECODE_FAILED or not _same_typed(decoded, expected):
            return False
    close_expected = {}
    for side, candidate in (("LONG", slot.long), ("SHORT", slot.short)):
        if candidate is not None:
            value = _first(candidate, "dedicated_close", "close")
            if value is None or value is False:
                return False
            close_expected[side] = _thaw(value)
    decoded_close = _decode_like(payload[manifest["close"]], close_expected)
    return decoded_close is not _DECODE_FAILED and _same_typed(decoded_close, close_expected)


def _build_payload(slot: PairSlot, manifest: Mapping[str, str], planned_leverage: Any, opposite_policy: Any, directions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        manifest["symbol"]: slot.symbol,
        manifest["leverage"]: planned_leverage,
        manifest["opposite_policy"]: _thaw(opposite_policy),
    }
    if slot.long is not None:
        result[manifest["long"]] = _candidate_payload(slot.long, (directions or {}).get("LONG"))
    if slot.short is not None:
        result[manifest["short"]] = _candidate_payload(slot.short, (directions or {}).get("SHORT"))
    close: dict[str, Any] = {}
    for side, candidate in (("LONG", slot.long), ("SHORT", slot.short)):
        if candidate is not None:
            value = _first(candidate, "dedicated_close", "close")
            if value is None or value is False:
                raise ValueError("dedicated close is missing")
            close[side] = _thaw(value)
    if not close:
        raise ValueError("dedicated close is missing")
    result[manifest["close"]] = close
    return result


def _extract(values: Any) -> tuple[list[Any], dict[str, Any]]:
    if values is None:
        return [], {}
    if isinstance(values, PairSlot) or _get(values, "slot") is not None:
        slot = _valid_slot(values)
        directions = _get(values, "directions")
        selected = {str(slot.symbol): values} if slot is not None and isinstance(directions, Mapping) else {}
        return ([values], selected) if slot is not None else ([values], {})
    if isinstance(values, Mapping) and ("long" in values or "short" in values or "side" in values):
        return [values], {}
    try:
        entries = list(values)
    except TypeError:
        return [], {}
    selected: dict[str, Any] = {}
    for entry in entries:
        slot = _valid_slot(entry)
        directions = _get(entry, "directions")
        if slot is not None and isinstance(directions, Mapping):
            selected[str(slot.symbol)] = entry
    return entries, selected


def render_portfolio(slots_or_candidates: Sequence[Any], *, capability: Mapping[str, Any] | None, planned_leverage: Any, opposite_policy: Any) -> RenderResult:
    if not isinstance(capability, Mapping) or not any(capability.get(key) is True for key in ("dedicated_close", "dedicated_close_capability")) or not any(capability.get(key) is True for key in ("opposite_opening", "opposite_opening_capability")):
        return RenderResult(UNKNOWN, "VALIDATION_FAILED", "CAPABILITY_UNSUPPORTED")
    manifest = _manifest(capability)
    leverage_input_valid = (
        all(_valid_planned_leverage(value) for value in planned_leverage.values())
        if isinstance(planned_leverage, Mapping)
        else _valid_planned_leverage(planned_leverage)
    )
    if manifest is None or not leverage_input_valid or opposite_policy in (None, "", "UNKNOWN"):
        return RenderResult(UNKNOWN, "VALIDATION_FAILED", "RENDER_INPUT_UNAVAILABLE")
    entries, selected = _extract(slots_or_candidates)
    if not entries:
        return RenderResult(UNKNOWN, "VALIDATION_FAILED", "STRUCTURAL_UNSUPPORTED")
    # A direct export is an admission boundary: silently dropping RESERVE,
    # missing-identity, or malformed rows would produce a false executable
    # payload.
    for entry in entries:
        if _get(entry, "slot") is not None:
            continue
        if isinstance(entry, PairSlot) or (isinstance(entry, Mapping) and ("long" in entry or "short" in entry)):
            continue
        if not _valid_identity(entry):
            return RenderResult(UNKNOWN, "VALIDATION_FAILED", "FINALIST_IDENTITY_INCOMPLETE")
    try:
        direct_slots = [_valid_slot(entry) for entry in entries]
        direct_entries = [slot is not None for slot in direct_slots]
        candidate_entries = [not direct and _valid_identity(entry) for direct, entry in zip(direct_entries, entries)]
        if any(direct_entries) and any(candidate_entries):
            return RenderResult(UNKNOWN, "VALIDATION_FAILED", "HETEROGENEOUS_INPUT")
        if all(direct_entries):
            slots = tuple(slot for slot in direct_slots if slot is not None)
        elif all(candidate_entries):
            slots = build_pair_slots(entries)
        else:
            return RenderResult(UNKNOWN, "VALIDATION_FAILED", "UNCLASSIFIABLE_INPUT")
    except (TypeError, ValueError):
        return RenderResult(UNKNOWN, "VALIDATION_FAILED", "TYPED_COMPARISON_FAILED")
    slots = tuple(sorted(slots, key=lambda slot: slot.symbol))
    if not slots or len({slot.symbol for slot in slots}) != len(slots) or any(slot.structural_reason for slot in slots):
        return RenderResult(UNKNOWN, "VALIDATION_FAILED", "DUPLICATE_SYMBOL_OR_EMPTY_SLOT")
    leverage_map: Mapping[str, Any] | None = planned_leverage if isinstance(planned_leverage, Mapping) else None
    if leverage_map is None and len(slots) > 1:
        return RenderResult(UNKNOWN, "VALIDATION_FAILED", "PER_SYMBOL_LEVERAGE_REQUIRED")
    if leverage_map is not None and set(leverage_map) != {slot.symbol for slot in slots}:
        return RenderResult(UNKNOWN, "VALIDATION_FAILED", "PER_SYMBOL_LEVERAGE_REQUIRED")
    payloads: list[Mapping[str, Any]] = []
    json_by_symbol: dict[str, str] = {}
    comparisons: list[bool] = []
    for slot in slots:
        leverage = leverage_map.get(slot.symbol) if leverage_map is not None else planned_leverage
        if not _valid_planned_leverage(leverage):
            return RenderResult(UNKNOWN, "VALIDATION_FAILED", f"LEVERAGE_MISSING:{slot.symbol}")
        ok, stable_reason, detail = _capability_ok(capability, slot, opposite_policy)
        if not ok:
            return RenderResult(UNKNOWN, stable_reason, detail)
        for candidate in (slot.long, slot.short):
            if candidate is not None:
                embedded = _first(candidate, "planned_leverage", "leverage")
                if embedded is not None and not _same_typed(embedded, leverage):
                    return RenderResult(UNKNOWN, "VALIDATION_FAILED", "LEVERAGE_MISMATCH")
        directions = _get(selected.get(slot.symbol), "directions") if selected.get(slot.symbol) is not None else None
        try:
            payload = _build_payload(slot, manifest, leverage, opposite_policy, directions)
        except (TypeError, ValueError):
            return RenderResult(UNKNOWN, "VALIDATION_FAILED", "RENDER_INPUT_UNAVAILABLE")
        try:
            json_text = json.dumps(_plain(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            emitted = json.loads(json_text)
        except (TypeError, ValueError):
            return RenderResult(UNKNOWN, "VALIDATION_FAILED", "TYPED_COMPARISON_FAILED")
        if not reverse_typed_compare(emitted, [slot], capability=capability, planned_leverage=leverage, opposite_policy=opposite_policy, directions=directions):
            return RenderResult(UNKNOWN, "VALIDATION_FAILED", "TYPED_COMPARISON_FAILED")
        payloads.append(payload)
        json_by_symbol[slot.symbol] = json_text
        comparisons.append(True)
    return RenderResult("PASS", None, None, tuple(payloads), json_by_symbol, tuple(comparisons))


def reverse_typed_compare(payload: Mapping[str, Any], slots_or_candidates: Sequence[Any], *, capability: Mapping[str, Any] | None, planned_leverage: Any = None, opposite_policy: Any = None, directions: Mapping[str, Any] | None = None) -> bool:
    if not isinstance(capability, Mapping) or not _valid_planned_leverage(planned_leverage) or opposite_policy is None:
        return False
    manifest = _manifest(capability)
    if manifest is None or not isinstance(payload, Mapping):
        return False
    entries, _ = _extract(slots_or_candidates)
    slots = [_valid_slot(entry) for entry in entries]
    if len(slots) != 1 or slots[0] is None:
        return False
    slot = slots[0]
    try:
        return _reverse_payload_facts(payload, slot, manifest, planned_leverage, opposite_policy, directions)
    except (TypeError, ValueError):
        return False


__all__ = ["RenderResult", "render_portfolio", "reverse_typed_compare"]
