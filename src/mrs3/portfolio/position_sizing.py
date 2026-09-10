"""Pure, fixture-only enrichment of finalist rows with one full position size."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .liquidity import Instrument, LiquidityError, ReferenceSnapshot
from .minute_capacity import CapacityWindow, MinuteCapacityResult
from .pretest_proxy import ProxyMetrics, compute_proxy_metrics


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


# Composition sizing is intentionally separate from the historical per-row
# enrichment above.  The latter remains useful to existing Panel callers;
# PRETEST_PROXY needs one shared symbol capacity for all selected members.
FLOAT_ABS_EPS = 1e-9
FLOAT_REL_EPS = 1e-12
MONEY_EPS = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class CompositionSizingExclusion:
    members: tuple[Mapping[str, Any], ...]
    reason: str


@dataclass(frozen=True, slots=True)
class CompositionSizingResult:
    status: str
    members: tuple[Mapping[str, Any], ...] = ()
    reason: str | None = None
    k: Decimal | None = None
    proxy: ProxyMetrics | None = None
    initial_margin_usdt: Decimal | None = None
    exclusions: tuple[CompositionSizingExclusion, ...] = ()
    k1: Decimal | None = None
    corrective_reduction_applied: bool = False

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return self.members


def _map_decimal(value: Any, *keys: str, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                return _decimal(value[key], key, positive=positive, nonnegative=nonnegative)
    else:
        for key in keys:
            if hasattr(value, key):
                return _decimal(getattr(value, key), key, positive=positive, nonnegative=nonnegative)
    raise ValueError(f"missing {keys[0]}")


def _composition_capacity(value: Any) -> tuple[Decimal, Decimal]:
    if isinstance(value, MinuteCapacityResult):
        return _decimal(value.position_cap_usdt, "position_cap_usdt", nonnegative=True), _decimal(value.round_down_usdt, "round_down_usdt", positive=True)
    if isinstance(value, Mapping):
        cap = _map_decimal(value, "position_cap_usdt", "capacity_usdt", "capacity", "cap_usdt", nonnegative=True)
        step = _map_decimal(value, "round_down_usdt", "notional_step", "step", positive=True) if any(key in value for key in ("round_down_usdt", "notional_step", "step")) else Decimal("0.00000001")
        return cap, step
    return _decimal(value, "capacity", nonnegative=True), Decimal("0.00000001")


def _instrument_for(reference: Any, symbol: str) -> Any:
    if reference is None:
        return None
    if hasattr(reference, "instrument"):
        return reference.instrument(symbol)
    if isinstance(reference, Mapping):
        value = reference.get(symbol)
        if value is None:
            return None
        return value
    return None


def _field(value: Any, *keys: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                return value[key]
    else:
        for key in keys:
            if hasattr(value, key):
                return getattr(value, key)
    return default


def _member_sort_key(row: Mapping[str, Any]) -> tuple[str, int, str, str]:
    return (str(row.get("symbol", "")), 0 if str(row.get("side", "")) == "LONG" else 1, str(row.get("strategy_id", "")), str(row.get("result_id", "")))


def _quantized_k(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def _qty_round(notional: Decimal, mark: Decimal, instrument: Any, *, cap: Decimal) -> tuple[Decimal, Decimal]:
    qty_step = _map_decimal(instrument, "qty_step", "quantity_step", positive=True)
    min_qty = _map_decimal(instrument, "min_qty", "min_order_qty", nonnegative=True)
    max_qty = _map_decimal(instrument, "max_qty", nonnegative=True)
    qty = _floor_step(notional / mark, qty_step)
    qty = min(qty, max_qty)
    actual = qty * mark
    while actual > cap + MONEY_EPS:
        qty = _floor_step(qty - qty_step, qty_step)
        actual = qty * mark
    if qty < min_qty:
        return Decimal(0), Decimal(0)
    return qty, actual


def _symbol_allocations(rows: Sequence[Mapping[str, Any]], cap: Decimal, mark: Decimal, instrument: Any, k: Decimal) -> tuple[dict[str, Any], ...]:
    selected = tuple(sorted(rows, key=_member_sort_key))
    if not selected or cap < 0 or mark <= 0:
        raise ValueError("INVALID_COMPOSITION")
    qty_step = _map_decimal(instrument, "qty_step", "quantity_step", positive=True)
    n = Decimal(len(selected))
    target = cap * k / n
    values: list[dict[str, Any]] = []
    used = Decimal(0)
    for row in selected:
        qty, actual = _qty_round(target, mark, instrument, cap=cap * k)
        values.append({"row": row, "quantity": qty, "actual_size_usdt": actual})
        used += actual
    residual_qty = _floor_step((cap * k - used) / mark, qty_step)
    if residual_qty > 0:
        # The residue is distributed one exchange step at a time in canonical
        # order, making shuffles of the input irrelevant.
        while residual_qty >= qty_step:
            moved = False
            for value in values:
                if residual_qty < qty_step:
                    break
                candidate = value["quantity"] + qty_step
                max_qty = _map_decimal(instrument, "max_qty", nonnegative=True)
                if candidate > max_qty:
                    continue
                extra = qty_step * mark
                if used + extra > cap * k + MONEY_EPS:
                    continue
                value["quantity"] = candidate
                value["actual_size_usdt"] += extra
                used += extra
                residual_qty -= qty_step
                moved = True
            if not moved:
                break
    if any(value["quantity"] <= 0 for value in values):
        raise ValueError("SIZE_ROUNDED_TO_ZERO")
    min_qty = _map_decimal(instrument, "min_qty", "min_order_qty", nonnegative=True)
    if any(value["quantity"] < min_qty for value in values):
        raise ValueError("SIZE_BELOW_MINIMUM_QTY")
    return tuple(values)


def _member_path(row: Mapping[str, Any]) -> tuple[Any, Decimal, Decimal]:
    path = row.get("equity", row.get("equity_series", row.get("equity_path", ())))
    initial = row.get("initial_balance", row.get("source_initial_balance"))
    # Rebuild this from the immutable opening geometry whenever it is present;
    # a carried runtime value must never silently change the source basis.
    orders = row.get("strategy_orders", row.get("orders", row.get("opening_orders", ())))
    tested = None
    if orders:
        lot_sum = sum((_decimal(item.get("lot_x"), "lot_x", positive=True) for item in orders if isinstance(item, Mapping)), Decimal(0))
        tested = _decimal(initial, "initial_balance", positive=True) * lot_sum
    if tested is None:
        tested = row.get("tested_size_usdt")
    return path, _decimal(initial, "initial_balance", positive=True), _decimal(tested, "tested_size_usdt", positive=True)


def _combine_proxy(rows: Sequence[Mapping[str, Any]], sizes: Sequence[Decimal], *, campaign_equity: Decimal) -> ProxyMetrics:
    if not rows:
        raise ValueError("EMPTY_COMPOSITION")
    paths: list[tuple[Mapping[str, Any], ...]] = []
    tested_sizes: list[Decimal] = []
    for row, size in zip(rows, sizes):
        path, initial, tested = _member_path(row)
        tested_sizes.append(tested)
        paths.append(tuple(compute_proxy_metrics(path, campaign_equity=campaign_equity, source_initial_balance=initial, tested_size_usdt=tested, actual_size_usdt=size).equity_path))
    if any(not path for path in paths):
        raise ValueError("EQUITY_PATH_UNAVAILABLE")
    timestamps = tuple(point["timestamp_utc"] for point in paths[0])
    if any(tuple(point["timestamp_utc"] for point in path) != timestamps for path in paths[1:]):
        raise ValueError("EQUITY_PERIOD_MISMATCH")
    combined = []
    for index, timestamp in enumerate(timestamps):
        increments = sum((path[index]["equity"] - campaign_equity for path in paths), Decimal(0))
        combined.append({"timestamp_utc": timestamp, "equity": campaign_equity + increments})
    return replace(
        compute_proxy_metrics(combined, campaign_equity=campaign_equity),
        tested_size_usdt=sum(tested_sizes, Decimal(0)),
        actual_size_usdt=sum(sizes, Decimal(0)),
    )


def size_composition(
    members: Sequence[Mapping[str, Any]],
    capacities: Mapping[str, Any],
    reference: Any,
    mark_prices: Mapping[str, Any],
    *,
    equity: Any,
    max_actual_equity_dd_pct: Any = None,
    min_free_margin_reserve_pct: Any = None,
    campaign_equity: Any | None = None,
    profile: Any = None,
    dd_pct: Any = None,
    reserve_pct: Any = None,
    portfolio_equity: Any | None = None,
) -> CompositionSizingResult:
    """Size one complete composition under shared caps and uniform risk/margin k."""
    selected = tuple(sorted((dict(row) for row in members), key=_member_sort_key))
    if not selected:
        return CompositionSizingResult(FAIL, reason="EMPTY_COMPOSITION")
    try:
        if portfolio_equity is not None:
            equity = portfolio_equity
        max_actual_equity_dd_pct = max_actual_equity_dd_pct if max_actual_equity_dd_pct is not None else dd_pct
        min_free_margin_reserve_pct = min_free_margin_reserve_pct if min_free_margin_reserve_pct is not None else reserve_pct
        if profile is not None:
            max_actual_equity_dd_pct = max_actual_equity_dd_pct if max_actual_equity_dd_pct is not None else _field(profile, "max_actual_equity_dd_pct", "dd_pct")
            min_free_margin_reserve_pct = min_free_margin_reserve_pct if min_free_margin_reserve_pct is not None else _field(profile, "min_calculated_free_margin_reserve_pct", "min_free_margin_reserve_pct", "reserve_pct")
        account_equity = _decimal(equity, "equity", positive=True)
        campaign = _decimal(campaign_equity if campaign_equity is not None else equity, "campaign_equity", positive=True)
        dd_pct = _decimal(max_actual_equity_dd_pct, "max_actual_equity_dd_pct", nonnegative=True)
        reserve_pct = _decimal(min_free_margin_reserve_pct, "min_free_margin_reserve_pct", nonnegative=True)
        if reserve_pct > 100:
            raise ValueError("INVALID_PROFILE")
        if any(str(row.get("side", "")) not in {"LONG", "SHORT"} for row in selected):
            raise ValueError("INVALID_DIRECTION")
        by_symbol: dict[str, list[Mapping[str, Any]]] = {}
        for row in selected:
            by_symbol.setdefault(str(row["symbol"]), []).append(row)
        full_values: list[dict[str, Any]] = []
        for symbol, symbol_rows in sorted(by_symbol.items()):
            cap, round_down = _composition_capacity(capacities[symbol])
            mark = _decimal(mark_prices[symbol], f"mark_prices.{symbol}", positive=True)
            instrument = _instrument_for(reference, symbol)
            if instrument is None:
                # Fixtures may provide all exchange values beside capacity.
                instrument = capacities[symbol] if isinstance(capacities[symbol], Mapping) else None
            if instrument is None:
                raise ValueError("MISSING_REFERENCE")
            max_qty = _map_decimal(instrument, "max_qty", nonnegative=True)
            cap = _floor_step(min(cap, max_qty * mark), round_down)
            allocations = _symbol_allocations(symbol_rows, cap, mark, instrument, Decimal(1))
            full_values.extend(allocations)
        full_sizes = tuple(value["actual_size_usdt"] for value in full_values)
        proxy = _combine_proxy(selected, full_sizes, campaign_equity=campaign)
        dd_budget = account_equity * dd_pct / 100
        k_dd = Decimal(1) if proxy.max_drawdown_usdt in (None, Decimal(0)) else min(Decimal(1), dd_budget / proxy.max_drawdown_usdt)
        if not k_dd.is_finite() or k_dd < 0:
            raise ValueError("INVALID_PROXY_DD")
        def margin_for(values: Sequence[Mapping[str, Any]]) -> Decimal:
            totals: dict[str, Decimal] = {}
            for value in values:
                row = value.get("row", value)
                totals[str(row["symbol"])] = totals.get(str(row["symbol"]), Decimal(0)) + _decimal(value.get("actual_size_usdt", row.get("actual_size_usdt")), "actual_size_usdt", nonnegative=True)
            result = Decimal(0)
            for symbol, total in totals.items():
                instrument = _instrument_for(reference, symbol) or capacities[symbol]
                leverage = None
                if hasattr(reference, "maximum_symbol_leverage"):
                    leverage = reference.maximum_symbol_leverage(symbol, total, 0)
                if leverage is None:
                    leverage = _field(instrument, "max_leverage", default=None)
                result += total / _decimal(leverage, "leverage", positive=True)
            return result

        margin = margin_for(full_values)
        margin_budget = account_equity * (Decimal(100) - reserve_pct) / 100
        k_margin = Decimal(1) if margin == 0 else min(Decimal(1), margin_budget / margin)
        if not k_margin.is_finite() or k_margin < 0:
            raise ValueError("INVALID_MARGIN")
        k1 = min(Decimal(1), k_dd, k_margin)

        def recompute(k: Decimal) -> tuple[tuple[Mapping[str, Any], ...], ProxyMetrics, Decimal]:
            values: list[dict[str, Any]] = []
            for symbol, symbol_rows in sorted(by_symbol.items()):
                cap, round_down = _composition_capacity(capacities[symbol])
                mark = _decimal(mark_prices[symbol], f"mark_prices.{symbol}", positive=True)
                instrument = _instrument_for(reference, symbol) or capacities[symbol]
                max_qty = _map_decimal(instrument, "max_qty", nonnegative=True)
                values.extend(_symbol_allocations(symbol_rows, _floor_step(min(cap, max_qty * mark), round_down), mark, instrument, k))
            sized: list[Mapping[str, Any]] = []
            for value in values:
                row = dict(value["row"])
                actual = value["actual_size_usdt"]
                leverage = _field(row, "planned_leverage", "max_leverage", default=None)
                if leverage is None:
                    instrument = _instrument_for(reference, str(row["symbol"])) or capacities[str(row["symbol"])]
                    leverage = _field(instrument, "max_leverage", default=None)
                leverage = _decimal(leverage, "leverage", positive=True)
                orders = row.get("strategy_orders", row.get("orders", row.get("opening_orders", ())))
                total_lot = sum((_decimal(item.get("lot_x"), "lot_x", positive=True) for item in orders if isinstance(item, Mapping)), Decimal(0))
                composition_sizing_digest = _digest({"basis": "PRETEST_COMPOSITION_SHARED_CAP", "symbol": row.get("symbol"), "strategy_id": row.get("strategy_id"), "quantity": value["quantity"], "actual_size_usdt": actual, "k": k, "capacity": capacities[str(row["symbol"])]})
                row.update({"actual_size_usdt": actual, "position_size_usdt": actual, "quantity": value["quantity"], "planned_leverage": leverage, "opening_allocations": tuple({"order_id": item.get("order_id"), "lot_x": item.get("lot_x"), "target_notional_usdt": actual * _decimal(item.get("lot_x"), "lot_x", positive=True) / total_lot} for item in orders), "tested_size_usdt": _member_path(row)[2], "tested_size_basis": "SOURCE_INITIAL_BALANCE_X_OPENING_LOT", "composition_sizing_digest": composition_sizing_digest, "sizing_basis": "PRETEST_COMPOSITION_SHARED_CAP"})
                sized.append(_freeze(row))
            sizes = tuple(value["actual_size_usdt"] for value in values)
            return tuple(sized), _combine_proxy(selected, sizes, campaign_equity=campaign), sum((value["actual_size_usdt"] for value in values), Decimal(0))

        initial_k = k1
        sized, proxy, actual_total = recompute(k1)
        initial_margin = margin_for(sized)
        ratios: list[Decimal] = []
        if proxy.max_drawdown_usdt and proxy.max_drawdown_usdt > dd_budget:
            ratios.append(dd_budget / proxy.max_drawdown_usdt)
        if initial_margin > margin_budget:
            ratios.append(margin_budget / initial_margin)
        if ratios:
            k2 = _quantized_k(k1 * min(ratios))
            sized, proxy, actual_total = recompute(k2)
            k1 = k2
            initial_margin = margin_for(sized)
            if (proxy.max_drawdown_usdt is not None and proxy.max_drawdown_usdt > dd_budget + MONEY_EPS) or initial_margin > margin_budget + MONEY_EPS:
                return CompositionSizingResult(FAIL, reason="POST_ROUND_CONSTRAINT_UNSATISFIED", k=k1, proxy=proxy, initial_margin_usdt=initial_margin, k1=initial_k, corrective_reduction_applied=True)
        return CompositionSizingResult(PASS, sized, None, k1, proxy, initial_margin, k1=initial_k, corrective_reduction_applied=bool(ratios))
    except KeyError as error:
        return CompositionSizingResult(FAIL, reason=f"MISSING_{str(error).strip(chr(39)).upper()}")
    except (ArithmeticError, TypeError, ValueError, InvalidOperation) as error:
        return CompositionSizingResult(FAIL, reason=str(error) if str(error).isupper() else "INVALID_COMPOSITION")


size_portfolio_composition = size_composition
size_candidate_composition = size_composition
calculate_composition_sizing = size_composition


__all__ = [
    "FAIL", "PASS", "FLOAT_ABS_EPS", "FLOAT_REL_EPS", "MONEY_EPS", "PositionSizingExclusion", "PositionSizingResult",
    "CompositionSizingExclusion", "CompositionSizingResult", "enrich_finalist_rows", "size_finalist_rows",
    "size_composition", "size_portfolio_composition", "size_candidate_composition", "calculate_composition_sizing",
]
