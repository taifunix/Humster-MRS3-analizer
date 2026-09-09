"""Pure, fixture-only enrichment of finalist rows with one full position size."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .liquidity import Instrument, LiquidityError, ReferenceSnapshot
from .minute_capacity import CapacityWindow, MinuteCapacityResult


PASS = "PASS"
FAIL = "FAIL"
_CAPACITY_STATUSES = frozenset({"READY", "PRELIMINARY"})
_RUNTIME_FIELDS = frozenset({
    "position_size_usdt", "maximum_closing_quantity", "maximum_closing_notional_usdt",
    "planned_leverage", "sizing_digest", "reference_digest", "capacity_digest",
    "capacity_status", "calendar_7d", "weekday_5d", "opening_allocations",
})


def _decimal(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise ValueError(f"{field} must be an exact decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite decimal") from error
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{field} has an invalid value")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"type": "Decimal", "value": format(value, "f")}
    if is_dataclass(value):
        return {"type": type(value).__name__, "value": _canonical(asdict(value))}
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=repr)
    if isinstance(value, float):
        return {"type": "float", "value": repr(value)}
    if isinstance(value, (bool, int, str)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        return {"type": type(value).__name__, "value": value.isoformat()}
    return {"type": type(value).__name__, "value": repr(value)}


def _digest(payload: Any) -> str:
    encoded = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(64, len(value.as_tuple().digits) + len(step.as_tuple().digits) + 16)
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True, slots=True)
class PositionSizingExclusion:
    row: Mapping[str, Any]
    reason: str
    status: str = "EXCLUDED"

    def __post_init__(self) -> None:
        object.__setattr__(self, "row", _freeze(dict(self.row)))


@dataclass(frozen=True, slots=True)
class PositionSizingResult:
    status: str
    rows: tuple[Mapping[str, Any], ...] = ()
    exclusions: tuple[PositionSizingExclusion, ...] = ()
    reason: str | None = None

    @property
    def enriched_rows(self) -> tuple[Mapping[str, Any], ...]:
        return self.rows


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("symbol", "")),
        str(row.get("side", "")),
        str(row.get("strategy_id", "")),
        str(row.get("result_id", "")),
        json.dumps(_canonical(row), sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )


def _global_reference_reason(reference: Any, *, now_ms: Any, maximum_age_hours: Any) -> str | None:
    if not isinstance(reference, ReferenceSnapshot):
        return "INVALID_REFERENCE_SNAPSHOT"
    if type(now_ms) is not int or now_ms < 0:
        return "INVALID_POLICY"
    try:
        age_hours = _decimal(maximum_age_hours, "maximum_age_hours", positive=True)
    except ValueError:
        return "INVALID_POLICY"
    if type(reference.captured_at_ms) is not int or reference.captured_at_ms < 0:
        return "INVALID_REFERENCE_SNAPSHOT"
    if not isinstance(reference.content_digest, str) or not reference.content_digest:
        return "INVALID_REFERENCE_SNAPSHOT"
    if now_ms < reference.captured_at_ms:
        return "REFERENCE_STALE"
    if now_ms - reference.captured_at_ms > age_hours * Decimal(3_600_000):
        return "REFERENCE_STALE"
    if not isinstance(reference.instruments, tuple) or not isinstance(reference.risk_tiers, tuple):
        return "INVALID_REFERENCE_SNAPSHOT"
    symbols: set[str] = set()
    try:
        for instrument in reference.instruments:
            if not isinstance(instrument, Instrument) or _text(instrument.symbol, "instrument.symbol") in symbols:
                return "INVALID_REFERENCE_SNAPSHOT"
            symbols.add(instrument.symbol)
            if instrument.status == "" or instrument.contract_type == "":
                return "INVALID_REFERENCE_SNAPSHOT"
            for field in ("tick_size", "qty_step", "min_qty", "max_qty", "leverage_step", "max_leverage"):
                _decimal(getattr(instrument, field), f"instrument.{field}", positive=True)
            if instrument.min_notional is not None:
                _decimal(instrument.min_notional, "instrument.min_notional", positive=True)
        for tier in reference.risk_tiers:
            _text(tier.symbol, "tier.symbol")
            _decimal(tier.risk_limit_value, "tier.risk_limit_value", positive=True)
            _decimal(tier.max_leverage, "tier.max_leverage", positive=True)
    except (TypeError, ValueError, AttributeError):
        return "INVALID_REFERENCE_SNAPSHOT"
    return None


def _source_row(row: Mapping[str, Any], geometry: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    source = {key: value for key, value in row.items() if key not in _RUNTIME_FIELDS}
    source["strategy_orders"] = geometry
    return source


def _exclude(row: Any, reason: str) -> PositionSizingExclusion:
    return PositionSizingExclusion(dict(row) if isinstance(row, Mapping) else {}, reason)


def enrich_finalist_rows(
    rows: Sequence[Mapping[str, Any]],
    capacities: Mapping[str, MinuteCapacityResult],
    reference: ReferenceSnapshot,
    mark_prices: Mapping[str, Any],
    *,
    now_ms: int,
    maximum_age_hours: Any,
) -> PositionSizingResult:
    """Enrich exact finalist rows with one exchange-rounded full position."""
    raw_rows = tuple(rows)
    global_reason = _global_reference_reason(reference, now_ms=now_ms, maximum_age_hours=maximum_age_hours)
    if global_reason is not None:
        exclusions = tuple(sorted((_exclude(row, global_reason) for row in raw_rows), key=lambda item: _row_key(item.row)))
        return PositionSizingResult(FAIL, (), exclusions, global_reason)

    enriched: list[Mapping[str, Any]] = []
    exclusions: list[PositionSizingExclusion] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            exclusions.append(_exclude({}, "INVALID_ROW"))
            continue
        row = dict(raw)
        try:
            if row.get("user_status") != "FINALIST":
                raise ValueError("USER_STATUS_NOT_FINALIST")
            symbol = _text(row.get("symbol"), "symbol")
            side = row.get("side")
            if side not in {"LONG", "SHORT"}:
                raise ValueError("INVALID_DIRECTION")
            orders = row.get("strategy_orders")
            if isinstance(orders, (str, bytes)) or not isinstance(orders, (Sequence, tuple)) or not orders:
                raise ValueError("INVALID_STRATEGY_ORDERS")
            parsed_orders: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for order in orders:
                if not isinstance(order, Mapping):
                    raise ValueError("INVALID_STRATEGY_ORDERS")
                order_id = order.get("order_id")
                if order_id is None or (isinstance(order_id, str) and not order_id.strip()):
                    raise ValueError("INVALID_STRATEGY_ORDERS")
                order_key = json.dumps(_canonical(order_id), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                if order_key in seen_ids:
                    raise ValueError("INVALID_STRATEGY_ORDERS")
                lot_x = _decimal(order.get("lot_x"), "lot_x", positive=True)
                seen_ids.add(order_key)
                parsed_orders.append({**dict(order), "order_id": order_id, "lot_x": lot_x})
            parsed_orders.sort(key=lambda item: json.dumps(_canonical(item["order_id"]), sort_keys=True, separators=(",", ":"), ensure_ascii=True))
            geometry = tuple(parsed_orders)
            capacity = capacities.get(symbol) if isinstance(capacities, Mapping) else None
            if not isinstance(capacity, MinuteCapacityResult) or capacity.symbol != symbol:
                raise ValueError("MISSING_CAPACITY")
            if capacity.status not in _CAPACITY_STATUSES or not isinstance(capacity.calendar_7d, CapacityWindow) or not isinstance(capacity.weekday_5d, CapacityWindow):
                raise ValueError("INVALID_CAPACITY")
            capacity_digest = _text(capacity.content_digest, "capacity.content_digest")
            round_down = _decimal(capacity.round_down_usdt, "round_down_usdt", positive=True)
            position_cap = _decimal(capacity.position_cap_usdt, "position_cap_usdt", nonnegative=True)
            mark = _decimal(mark_prices.get(symbol), "mark_price", positive=True)
            instrument = reference.instrument(symbol)
            if instrument.status != "Trading" or instrument.contract_type != "LinearPerpetual":
                raise ValueError("INACTIVE_INSTRUMENT")
            raw_cap = min(position_cap, instrument.max_qty * mark)
            rounded_cap = _floor_step(raw_cap, round_down)
            if rounded_cap == 0:
                raise ValueError("SIZE_ROUNDED_TO_ZERO")
            closing_quantity = _floor_step(rounded_cap / mark, instrument.qty_step)
            if closing_quantity < instrument.min_qty:
                raise ValueError("SIZE_BELOW_MINIMUM_QTY")
            final_notional = closing_quantity * mark
            if final_notional > rounded_cap:
                raise ValueError("SIZE_EXCEEDS_ROUNDED_CAP")
            if instrument.min_notional is not None and final_notional < instrument.min_notional:
                raise ValueError("SIZE_BELOW_MINIMUM_NOTIONAL")
            leverage = reference.maximum_symbol_leverage(symbol, final_notional, 0)
            leverage = _decimal(leverage, "planned_leverage", positive=True)
            total_lot = sum((item["lot_x"] for item in geometry), Decimal(0))
            with localcontext() as context:
                context.prec = 28
                allocations = tuple(
                    {"order_id": item["order_id"], "lot_x": item["lot_x"], "target_notional_usdt": final_notional * item["lot_x"] / total_lot}
                    for item in geometry
                )
            reference_digest = reference.content_digest
            sizing_digest = _digest({
                "schema": "portfolio_position_sizing_v1",
                "source_row": _source_row(row, geometry),
                "mark_price": mark,
                "raw_cap_usdt": raw_cap,
                "rounded_cap_usdt": rounded_cap,
                "closing_quantity": closing_quantity,
                "position_size_usdt": final_notional,
                "allocations": allocations,
                "planned_leverage": leverage,
                "instrument": instrument,
                "reference_digest": reference_digest,
                "capacity_digest": capacity_digest,
                "capacity_status": capacity.status,
                "policy": {"now_ms": now_ms, "maximum_age_hours": _decimal(maximum_age_hours, "maximum_age_hours", nonnegative=True)},
            })
            enriched_row = dict(row)
            enriched_row.update({
                "position_size_usdt": final_notional,
                "maximum_closing_quantity": closing_quantity,
                "maximum_closing_notional_usdt": final_notional,
                "planned_leverage": leverage,
                "sizing_digest": sizing_digest,
                "reference_digest": reference_digest,
                "capacity_digest": capacity_digest,
                "capacity_status": capacity.status,
                "calendar_7d": capacity.calendar_7d,
                "weekday_5d": capacity.weekday_5d,
                "opening_allocations": allocations,
            })
            enriched.append(_freeze(enriched_row))
        except LiquidityError:
            exclusions.append(_exclude(row, "REFERENCE_FACTS_UNAVAILABLE"))
        except (ArithmeticError, KeyError, TypeError, ValueError) as error:
            reason = str(error) or "INVALID_ROW"
            exclusions.append(_exclude(row, reason if reason.isupper() and " " not in reason else "INVALID_ROW"))

    enriched.sort(key=_row_key)
    exclusions.sort(key=lambda item: _row_key(item.row) + (item.reason,))
    if not enriched:
        return PositionSizingResult(FAIL, (), tuple(exclusions), "NO_ENRICHED_ROWS")
    return PositionSizingResult(PASS, tuple(enriched), tuple(exclusions))


size_finalist_rows = enrich_finalist_rows


__all__ = ["FAIL", "PASS", "PositionSizingExclusion", "PositionSizingResult", "enrich_finalist_rows", "size_finalist_rows"]
