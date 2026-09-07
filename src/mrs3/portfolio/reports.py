"""Fixture-only portfolio report boundary and position-cycle reconstruction.

The tester's physical HTML format is deliberately not parsed here.  Callers
must inject a decoder which returns this small, versioned mapping contract.
That keeps an unknown Q06 format fail-closed while making the rest of M6
replayable from sanitized fixtures.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


REPORT_CONTRACT = "portfolio_report_v1"
REPORT_SCHEMA = "portfolio_normalized_report_v1"
REPORT_VERSION = 1
PARSER_VERSION = "portfolio_report_parser_v1"
METRICS_VERSION = "portfolio_report_metrics_v1"
SERIES_NAMES = ("wallet", "equity", "margin_balance", "notional")
BLOCKING_DIAGNOSTICS = frozenset({"EQUITY_PATH_MISSING", "EQUITY_COVERAGE_INSUFFICIENT", "EQUITY_DENOMINATOR_INVALID", "FINANCIAL_RECONCILIATION_FAILED", "FINANCIAL_RECONCILIATION_UNVERIFIED", "MARGIN_BOUND_FAILED"})
# These are the exact aliases accepted by the versioned fixture boundary.  A
# typo or an unmodelled field must fail closed instead of disappearing from
# the semantic digest.
_TOP_LEVEL_KEYS = frozenset({
    "schema", "contract", "version", "schema_version", "identity",
    "run_id", "attempt_id", "member", "member_id",
    "action_count", "declared_action_count", "actionCount",
    "actions", "action_rows", "actionRows", "executions",
    "period", "report_start", "report_end", "start", "end",
    "series", "time_series", "portfolio", "portfolio_summary", "result",
    "symbols", "symbol_summaries", "actual_leverage", "applied_leverage",
    "source_report_name", "parser_version", "metrics_version",
})
_HEX64 = set("0123456789abcdef")


class ReportNormalizationError(ValueError):
    """A report cannot be trusted as a normalized M6 result."""

    def __init__(self, message: str, *, code: str = "REPORT_INVALID") -> None:
        super().__init__(message)
        self.code = code

    @property
    def capability_result(self) -> str | None:
        return "BLOCKING_UNKNOWN" if self.code == "Q06_BLOCKING_UNKNOWN" else None


def _decimal(value: Any, field_name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ReportNormalizationError(f"{field_name} must be Decimal, integer, or string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ReportNormalizationError(f"{field_name} is not decimal") from error
    if not result.is_finite():
        raise ReportNormalizationError(f"{field_name} must be finite")
    return result


def _timestamp(value: Any, field_name: str = "timestamp") -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as error:
            raise ReportNormalizationError(f"{field_name} is not an ISO timestamp") from error
    else:
        raise ReportNormalizationError(f"{field_name} is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReportNormalizationError(f"{field_name} must be timezone-aware")
    parsed = parsed.astimezone(timezone.utc)
    # Fixed-width UTC keeps lexical ordering equivalent to timestamp ordering.
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == 0:
            value = Decimal("0")
        else:
            value = value.normalize()
        return {"__decimal__": format(value, "f")}
    if hasattr(value, "__dataclass_fields__"):
        return _json_value(asdict(value))
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _raw_digest(raw: Any) -> str:
    payload = raw if isinstance(raw, bytes) else bytes(raw) if isinstance(raw, bytearray) else _canonical_bytes(raw)
    return hashlib.sha256(payload).hexdigest()


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReportNormalizationError(f"{name} must be an object")
    return value


def _value(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    present = [name for name in names if name in mapping]
    if len(present) > 1:
        raise ReportNormalizationError(
            "multiple aliases for " + "/".join(names), code="REPORT_SCHEMA_INVALID"
        )
    if present:
        return mapping[present[0]]
    return default


def _numeric_summary(value: Any, key: str) -> Any:
    if value is None:
        return None
    lowered = key.casefold().strip().replace(" ", "_")
    numeric = (
        any(token in lowered for token in ("pnl", "profit", "loss", "fee", "fund", "balance", "equity", "volume", "notional", "drawdown", "recovery", "price", "size", "quantity", "margin", "turnover"))
        or lowered in {"pf", "profit_factor", "trades", "executions", "count", "dd", "max_position"}
    )
    if numeric:
        if lowered in {"trades", "executions", "count"}:
            if isinstance(value, bool) or isinstance(value, float):
                raise ReportNormalizationError(f"{key} must be an integer")
            try:
                integer = int(value)
            except (TypeError, ValueError) as error:
                raise ReportNormalizationError(f"{key} must be an integer") from error
            if str(integer) != str(value).strip() and not isinstance(value, int):
                raise ReportNormalizationError(f"{key} must be an integer")
            if integer < 0:
                raise ReportNormalizationError(f"{key} must be non-negative")
            return integer
        return _decimal(value, key, optional=True)
    return value


def _summary(value: Any, name: str) -> Mapping[str, Any]:
    source = _mapping({} if value is None else value, name)
    result: dict[str, Any] = {}
    for key, item in source.items():
        key_string = str(key)
        if isinstance(item, Mapping):
            result[key_string] = _summary(item, key_string)
        elif isinstance(item, (list, tuple)):
            result[key_string] = tuple(_numeric_summary(part, key_string) for part in item)
        else:
            result[key_string] = _numeric_summary(item, key_string)
    return MappingProxyType(result)


def _summary_value(summary: Mapping[str, Any], *names: str) -> Any:
    """Read summary fields case-insensitively without changing their keys."""
    wanted = {name.casefold().strip().replace(" ", "_") for name in names}
    present = [
        (key, value)
        for key, value in summary.items()
        if str(key).casefold().strip().replace(" ", "_") in wanted
    ]
    if len(present) > 1:
        raise ReportNormalizationError(
            "multiple summary aliases for " + "/".join(names),
            code="REPORT_SCHEMA_INVALID",
        )
    if present:
        return present[0][1]
    return None


@dataclass(frozen=True, slots=True)
class ReportProvenance:
    source_report_name: str
    source_report_sha256: str
    parser_version: str = PARSER_VERSION
    metrics_version: str = METRICS_VERSION

    def __post_init__(self) -> None:
        if not self.source_report_name:
            raise ValueError("source_report_name is required")
        if len(self.source_report_sha256) != 64 or set(self.source_report_sha256) - _HEX64:
            raise ValueError("source_report_sha256 must be lowercase SHA-256")
        if not self.parser_version or not self.metrics_version:
            raise ValueError("parser and metrics versions are required")


@dataclass(frozen=True, slots=True)
class ReportSeriesPoint:
    timestamp_utc: str
    value: Decimal | None
    source_ordinal: int
    availability: str = "AVAILABLE"
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_utc", _timestamp(self.timestamp_utc))
        if type(self.source_ordinal) is not int or self.source_ordinal < 0:
            raise ValueError("source_ordinal must be a non-negative integer")
        if self.value is not None:
            _decimal(self.value, "series value")
        if self.availability not in {"AVAILABLE", "UNAVAILABLE"}:
            raise ValueError("series availability is invalid")
        if self.availability == "AVAILABLE" and self.value is None:
            raise ValueError("available series point needs a value")
        if self.availability == "UNAVAILABLE" and self.value is not None:
            raise ValueError("unavailable series point cannot have a value")

    @property
    def numeric_value(self) -> Decimal | None:
        return self.value


@dataclass(frozen=True, slots=True)
class ReportAction:
    timestamp_utc: str
    source_ordinal: int
    symbol: str | None
    action: str
    side: str | None = None
    order_id: str | None = None
    size: Decimal | None = None
    price: Decimal | None = None
    cost: Decimal | None = None
    fee: Decimal | None = None
    funding: Decimal | None = None
    pnl: Decimal | None = None
    balance: Decimal | None = None
    post_size: Decimal | None = None
    post_side: str | None = None
    requested_quantity: Decimal | None = None
    qty_delta: Decimal | None = None
    close_attribution: str | None = None
    pre_size: Decimal | None = None
    pre_side: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_utc", _timestamp(self.timestamp_utc))
        if type(self.source_ordinal) is not int or self.source_ordinal < 0:
            raise ValueError("source_ordinal must be a non-negative integer")
        if not self.action:
            raise ValueError("action is required")
        if self.symbol is not None and not self.symbol:
            raise ValueError("symbol cannot be empty")
        for name in ("size", "price", "cost", "fee", "funding", "pnl", "balance", "post_size", "requested_quantity", "qty_delta", "pre_size"):
            current = getattr(self, name)
            if current is not None:
                _decimal(current, name)

    @property
    def quantity(self) -> Decimal | None:
        return self.size


@dataclass(frozen=True, slots=True)
class PositionCycle:
    symbol: str
    cycle_id: int
    opened_at: str | None
    closed_at: str | None
    duration_seconds: Decimal | None
    censored: bool
    carry_in: bool
    side: str | None
    realized_pnl: Decimal | None
    fees: Decimal | None
    maximum_position: Decimal
    execution_count: int
    close_attribution: str | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizedReport:
    schema: str
    version: int
    run_id: str
    attempt_id: str
    member: str
    portfolio: Mapping[str, Any]
    symbols: tuple[Mapping[str, Any], ...]
    actions: tuple[ReportAction, ...]
    series: Mapping[str, tuple[ReportSeriesPoint, ...]]
    cycles: tuple[PositionCycle, ...]
    provenance: ReportProvenance
    declared_action_count: int
    report_start: str | None = None
    report_end: str | None = None
    actual_leverage: Mapping[str, Decimal] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    semantic_digest: str = ""
    raw_digest: str = ""

    @property
    def source_report_name(self) -> str:
        return self.provenance.source_report_name

    @property
    def identity(self) -> Mapping[str, str]:
        return MappingProxyType({"run_id": self.run_id, "attempt_id": self.attempt_id, "member": self.member})

    @property
    def portfolio_summary(self) -> Mapping[str, Any]:
        return self.portfolio

    @property
    def source_report_sha256(self) -> str:
        return self.provenance.source_report_sha256

    @property
    def canonical_semantic_digest(self) -> str:
        return self.semantic_digest

    @property
    def raw_sha256(self) -> str:
        return self.raw_digest

    @property
    def parser_version(self) -> str:
        return self.provenance.parser_version

    @property
    def metrics_version(self) -> str:
        return self.provenance.metrics_version

    @property
    def action_count(self) -> int:
        return len(self.actions)

    @property
    def actions_count(self) -> int:
        return self.action_count

    @property
    def available_series(self) -> tuple[str, ...]:
        return tuple(name for name, points in self.series.items() if points)

    @property
    def series_availability(self) -> Mapping[str, str]:
        return MappingProxyType({name: ("AVAILABLE" if any(point.value is not None for point in points) else "UNAVAILABLE") for name, points in self.series.items()})

    @property
    def blocking(self) -> bool:
        return bool(BLOCKING_DIAGNOSTICS & set(self.diagnostics))

    def semantic_payload(self) -> Mapping[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            # Run/attempt identifiers are provenance and must not make an
            # equivalent retry look nondeterministic.
            "identity": {"member": self.member},
            "portfolio": self.portfolio,
            "symbols": self.symbols,
            "actions": self.actions,
            "series": self.series,
            "cycles": self.cycles,
            "declared_action_count": self.declared_action_count,
            "report_start": self.report_start,
            "report_end": self.report_end,
            "actual_leverage": self.actual_leverage,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "member": self.member,
            "portfolio": dict(self.portfolio),
            "symbols": [dict(item) for item in self.symbols],
            "actions": [asdict(item) for item in self.actions],
            "series": {name: [asdict(item) for item in points] for name, points in self.series.items()},
            "cycles": [asdict(item) for item in self.cycles],
            "source_report_name": self.source_report_name,
            "source_report_sha256": self.source_report_sha256,
            "parser_version": self.parser_version,
            "metrics_version": self.metrics_version,
            "declared_action_count": self.declared_action_count,
            "report_start": self.report_start,
            "report_end": self.report_end,
            "actual_leverage": dict(self.actual_leverage),
            "diagnostics": list(self.diagnostics),
            "semantic_digest": self.semantic_digest,
            "raw_digest": self.raw_digest,
        }


def _decode(raw: bytes, decoder: Any) -> Mapping[str, Any]:
    if decoder is None:
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReportNormalizationError("physical report format is unknown; an injected Q06 decoder is required", code="Q06_BLOCKING_UNKNOWN") from error
        if isinstance(decoded, Mapping) and decoded.get("schema") in {REPORT_CONTRACT, REPORT_SCHEMA}:
            return decoded
        raise ReportNormalizationError("structured fixture does not carry the versioned report schema", code="Q06_BLOCKING_UNKNOWN")
    method: Callable[[bytes], Any]
    if callable(decoder):
        method = decoder
    else:
        method = getattr(decoder, "decode", None) or getattr(decoder, "parse", None)
        if not callable(method):
            raise ReportNormalizationError("report decoder has no callable decode/parse method", code="Q06_BLOCKING_UNKNOWN")
    try:
        decoded = method(raw)
    except Exception as error:
        raise ReportNormalizationError("injected report decoder failed", code="Q06_BLOCKING_UNKNOWN") from error
    return _mapping(decoded, "decoded report")


def _series_points(raw: Any, name: str) -> tuple[ReportSeriesPoint, ...]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        if "points" in raw and "values" in raw:
            raise ReportNormalizationError(
                f"series {name} has multiple point aliases", code="REPORT_SCHEMA_INVALID"
            )
        if "points" in raw or "values" in raw:
            raw = raw.get("points", raw.get("values", ()))
        else:
            raw = [{"timestamp": timestamp, "value": value, "source_ordinal": ordinal} for ordinal, (timestamp, value) in enumerate(raw.items())]
    if not isinstance(raw, (list, tuple)):
        raise ReportNormalizationError(f"series {name} must be an array")
    points: list[ReportSeriesPoint] = []
    for ordinal, item in enumerate(raw):
        if isinstance(item, Mapping):
            timestamp = _value(item, "timestamp_utc", "timestamp", "time")
            source_ordinal = _value(item, "source_ordinal", "ordinal", default=ordinal)
            value = _value(item, "value", "numeric_value", "equity", "amount")
            available = _value(item, "availability", "available", default="AVAILABLE")
            reason = _value(item, "reason", "unavailable_reason")
        elif isinstance(item, (list, tuple)) and len(item) in {2, 3}:
            timestamp, value = item[:2]
            source_ordinal = item[2] if len(item) == 3 else ordinal
            available, reason = "AVAILABLE", None
        else:
            raise ReportNormalizationError(f"series {name} has malformed point")
        if isinstance(available, bool):
            available = "AVAILABLE" if available else "UNAVAILABLE"
        elif isinstance(available, str):
            available = available.upper()
            if available == "MISSING":
                available = "UNAVAILABLE"
        if available not in {"AVAILABLE", "UNAVAILABLE"}:
            raise ReportNormalizationError(
                f"series {name} availability is invalid", code="REPORT_SCHEMA_INVALID"
            )
        if type(source_ordinal) is not int or source_ordinal < 0:
            raise ReportNormalizationError(f"series {name} source ordinal is invalid")
        if available == "UNAVAILABLE" and value is not None:
            raise ReportNormalizationError(
                f"series {name} unavailable point has a value", code="REPORT_SCHEMA_INVALID"
            )
        if available == "UNAVAILABLE" or value is None:
            points.append(ReportSeriesPoint(_timestamp(timestamp), None, source_ordinal, "UNAVAILABLE", str(reason or "MISSING_VALUE")))
        else:
            points.append(ReportSeriesPoint(_timestamp(timestamp), _decimal(value, f"series {name} value"), source_ordinal, "AVAILABLE", None))
    if len({point.source_ordinal for point in points}) != len(points):
        raise ReportNormalizationError(f"series {name} source ordinals are duplicated")
    return tuple(sorted(points, key=lambda point: (point.timestamp_utc, point.source_ordinal)))


def _action(raw: Any, ordinal: int) -> ReportAction:
    item = _mapping(raw, "action")
    timestamp = _value(item, "timestamp_utc", "timestamp", "time")
    symbol = _value(item, "symbol", "instrument")
    if symbol is not None:
        symbol = str(symbol).strip()
    action = str(_value(item, "action", "kind", "type", default="")).strip().upper()
    if not action:
        raise ReportNormalizationError("action kind is required")
    side = _value(item, "position_side", "side", "direction")
    if side is not None:
        side = str(side).strip().upper()
    if action.casefold() not in _ACTION_KINDS:
        raise ReportNormalizationError("unsupported action kind", code="REPORT_SCHEMA_INVALID")
    if side is not None and side not in _POSITION_SIDES:
        raise ReportNormalizationError("unsupported action side", code="REPORT_SCHEMA_INVALID")

    def derived_side(*aliases: str) -> str | None:
        value = _value(item, *aliases)
        if value is None:
            return None
        normalized = str(value).strip().upper()
        if normalized not in _POSITION_SIDES:
            raise ReportNormalizationError("unsupported derived action side", code="REPORT_SCHEMA_INVALID")
        return normalized

    def dec(name: str, *aliases: str) -> Decimal | None:
        return _decimal(_value(item, name, *aliases), name, optional=True)
    source_ordinal = _value(item, "source_ordinal", "ordinal", default=ordinal)
    if type(source_ordinal) is not int or source_ordinal < 0:
        raise ReportNormalizationError("action source ordinal is invalid")
    return ReportAction(
        _timestamp(timestamp), source_ordinal, symbol, action, side,
        str(_value(item, "order_id", "orderId")) if _value(item, "order_id", "orderId") is not None else None,
        dec("size", "quantity", "filled_size", "filled_quantity"),
        dec("price", "fill_price"), dec("cost", "notional", "value"), dec("fee", "fees"),
        dec("funding", "funding_fee"), dec("pnl", "realized_pnl", "realised_pnl"), dec("balance"),
        dec("post_size", "position_size", "post_quantity"),
        derived_side("post_side"),
        dec("requested_quantity", "requested_size"), dec("qty_delta", "signed_quantity", "delta"),
        str(_value(item, "close_attribution")) if _value(item, "close_attribution") is not None else None,
        dec("pre_size", "previous_size", "before_size"),
        derived_side("pre_side", "previous_side", "before_side"),
    )


def _signed_size(action: ReportAction, current: Decimal) -> Decimal:
    if action.qty_delta is not None:
        return action.qty_delta
    size = action.size or Decimal("0")
    kind = action.action.casefold()
    side = (action.side or action.post_side or "").upper()
    if kind in _CLOSING_ACTIONS:
        return -size if current >= 0 else size
    if kind in {"sell", "short", "open_short", "increase_short"}:
        return -size
    if kind in {"buy", "long", "open_long", "increase_long", "open", "increase", "add"}:
        return -size if side == "SHORT" else size
    if side == "SHORT":
        return -size
    return size


_CLOSING_ACTIONS = frozenset({
    "close", "reduce", "decrease", "exit", "sell_close", "buy_close",
    "forced_close", "force_close", "forced-close", "force-close", "liquidation", "liquidate",
})
_ACTION_KINDS = _CLOSING_ACTIONS | frozenset({
    "open", "increase", "add", "buy", "sell", "long", "short",
    "open_long", "open_short", "increase_long", "increase_short",
})
_POSITION_SIDES = frozenset({"LONG", "SHORT"})


def _cycle_state(opened_at: str | None, carry_in: bool, side: str | None, maximum: Decimal, diagnostics: list[str]) -> dict[str, Any]:
    return {
        "opened_at": opened_at,
        "carry_in": carry_in,
        "side": side,
        "pnl": Decimal("0"),
        "pnl_seen": False,
        "pnl_complete": True,
        "fees": Decimal("0"),
        "fees_seen": False,
        "fees_complete": True,
        "max": maximum,
        "count": 0,
        "diagnostics": diagnostics,
    }


def reconstruct_cycles(actions: Iterable[ReportAction], *, report_end: str | None = None) -> tuple[PositionCycle, ...]:
    """Rebuild flat/non-flat cycles while retaining ambiguous transitions."""
    grouped: dict[str, list[ReportAction]] = {}
    for action in sorted(actions, key=lambda item: (item.timestamp_utc, item.source_ordinal)):
        if action.symbol:
            grouped.setdefault(action.symbol, []).append(action)
    result: list[PositionCycle] = []
    cycle_number = 0
    for symbol in sorted(grouped):
        current = Decimal("0")
        active: dict[str, Any] | None = None
        for action in grouped[symbol]:
            before = action.pre_size
            if before is None:
                before = current
            elif action.pre_side:
                before = abs(before) if action.pre_side != "SHORT" else -abs(before)
            kind = action.action.casefold()
            unknown_leading_close = (
                before == 0
                and action.pre_size is None
                and action.post_size is None
                and action.qty_delta is None
                and kind in _CLOSING_ACTIONS
            )
            delta = Decimal("0") if unknown_leading_close else _signed_size(action, before)
            after = action.post_size
            if after is None:
                after = before + delta
            if action.post_side and after:
                after = abs(after) if action.post_side != "SHORT" else -abs(after)
            if active is None and unknown_leading_close:
                active = _cycle_state(None, True, None, Decimal("0"), ["CARRY_IN", "CARRY_IN_DIRECTION_UNKNOWN"])
            if active is None and before != 0:
                active = _cycle_state(None, True, "SHORT" if before < 0 else "LONG", abs(before), ["CARRY_IN"])
            if active is None and before == 0 and after != 0:
                active = _cycle_state(action.timestamp_utc, False, "SHORT" if after < 0 else "LONG", abs(after), [])
            previous_max = active["max"] if active is not None else Decimal("0")
            if active is not None:
                if action.pnl is not None:
                    active["pnl"] += action.pnl
                    active["pnl_seen"] = True
                elif kind in _CLOSING_ACTIONS:
                    active["pnl_complete"] = False
                if action.fee is not None:
                    active["fees"] += action.fee
                    active["fees_seen"] = True
                else:
                    active["fees_complete"] = False
                active["max"] = max(active["max"], abs(after), abs(before))
                active["count"] += 1
            if before != 0 and after != 0 and ((before > 0) != (after > 0)):
                if active is not None:
                    # The opposite-side quantity belongs to the new cycle;
                    # keep the terminating cycle's maximum on its own side.
                    active["max"] = max(previous_max, abs(before))
                    active["count"] -= 1
                    active["diagnostics"].extend(("UNEXPECTED_REVERSAL", "REVERSAL_ATTRIBUTION_AMBIGUOUS"))
                    result.append(_finish_cycle(symbol, cycle_number, active, action.timestamp_utc, False, "UNKNOWN", report_end))
                    cycle_number += 1
                active = _cycle_state(action.timestamp_utc, False, "SHORT" if after < 0 else "LONG", abs(after), ["UNEXPECTED_REVERSAL", "REVERSAL_ATTRIBUTION_AMBIGUOUS"])
                active["count"] = 1
            if after == 0 and active is not None:
                attribution = action.close_attribution or "UNKNOWN"
                result.append(_finish_cycle(symbol, cycle_number, active, action.timestamp_utc, False, attribution, report_end))
                cycle_number += 1
                active = None
            current = after
        if active is not None:
            result.append(_finish_cycle(symbol, cycle_number, active, None, True, None, report_end))
            cycle_number += 1
    return tuple(result)


def _finish_cycle(symbol: str, number: int, active: Mapping[str, Any], closed: str | None, censored: bool, attribution: str | None, report_end: str | None) -> PositionCycle:
    opened = active.get("opened_at")
    duration: Decimal | None = None
    end = closed or report_end
    if opened and end:
        delta = datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(opened.replace("Z", "+00:00"))
        duration = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal("1000000")
    diagnostics = list(active.get("diagnostics", ()))
    if censored:
        diagnostics.append("OPEN_AT_END")
    realized_pnl = active.get("pnl") if active.get("pnl_seen") and active.get("pnl_complete", True) else None
    fees = active.get("fees") if active.get("fees_seen") and active.get("fees_complete", True) else None
    if not censored and realized_pnl is None:
        diagnostics.append("REALIZED_PNL_UNAVAILABLE")
    if not censored and fees is None:
        diagnostics.append("FEES_UNAVAILABLE")
    return PositionCycle(symbol, number, opened, closed, duration, censored, bool(active.get("carry_in")), active.get("side"), realized_pnl, fees, active.get("max", Decimal("0")), int(active.get("count", 0)), attribution or ("UNKNOWN" if "UNEXPECTED_REVERSAL" in diagnostics else None), tuple(dict.fromkeys(diagnostics)))


def _validate_range(timestamp: str, start: str | None, end: str | None, name: str) -> None:
    if start and timestamp < start or end and timestamp > end:
        raise ReportNormalizationError(f"{name} is outside declared report range", code="REPORT_RANGE_INVALID")


def normalize_report(
    raw: bytes | bytearray | Mapping[str, Any],
    *,
    decoder: Any = None,
    source_report_name: str | None = None,
    source_report_sha256: str | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
    member: str | None = None,
    parser_version: str = PARSER_VERSION,
    metrics_version: str = METRICS_VERSION,
    expected_identity: Mapping[str, Any] | None = None,
    source_name: str | None = None,
    source_digest: str | None = None,
) -> NormalizedReport:
    """Decode a fixture/adapter result and normalize it into the M6 contract."""
    if expected_identity:
        run_id = run_id if run_id is not None else expected_identity.get("run_id")
        attempt_id = attempt_id if attempt_id is not None else expected_identity.get("attempt_id")
        member = member if member is not None else expected_identity.get("member", expected_identity.get("member_id"))
    if source_name is not None:
        source_report_name = source_name
    if source_digest is not None:
        source_report_sha256 = source_digest
    computed_raw_digest = _raw_digest(raw)
    raw_digest = source_report_sha256 or computed_raw_digest
    if len(raw_digest) != 64 or set(raw_digest) - _HEX64:
        raise ReportNormalizationError("source SHA-256 is malformed")
    if source_report_sha256 is not None and source_report_sha256 != computed_raw_digest:
        raise ReportNormalizationError("source SHA-256 does not match report bytes", code="REPORT_DIGEST_INVALID")
    document = _decode(bytes(raw), decoder) if isinstance(raw, (bytes, bytearray)) else _mapping(raw, "report")
    unknown_top_level = set(document) - _TOP_LEVEL_KEYS
    if unknown_top_level:
        raise ReportNormalizationError(
            "unsupported top-level report fields: " + ", ".join(sorted(str(key) for key in unknown_top_level)),
            code="REPORT_SCHEMA_INVALID",
        )
    schema = _value(document, "schema", "contract")
    version = _value(document, "version", "schema_version")
    if schema not in {REPORT_CONTRACT, REPORT_SCHEMA} or version != REPORT_VERSION:
        raise ReportNormalizationError("unsupported versioned portfolio report schema", code="REPORT_SCHEMA_INVALID")
    identity = _mapping(document.get("identity", {}), "identity")
    actual_run = str(_value(identity, "run_id", default=_value(document, "run_id", default="")))
    actual_attempt = str(_value(identity, "attempt_id", default=_value(document, "attempt_id", default="")))
    actual_member = str(_value(identity, "member", "member_id", default=_value(document, "member", "member_id", default="")))
    for name, actual, expected in (("run_id", actual_run, run_id), ("attempt_id", actual_attempt, attempt_id), ("member", actual_member, member)):
        if expected is not None and actual != str(expected):
            raise ReportNormalizationError(f"{name} identity mismatch", code="REPORT_IDENTITY_INVALID")
        if not actual:
            raise ReportNormalizationError(f"{name} identity is missing", code="REPORT_IDENTITY_INVALID")
    declared = _value(document, "action_count", "declared_action_count", "actionCount")
    if declared is None:
        declared = _value(
            _mapping(_value(document, "portfolio", "portfolio_summary", "result", default={}), "portfolio"),
            "action_count", "actionCount", "trades", "Trades",
        )
    if type(declared) is not int or declared < 0:
        raise ReportNormalizationError("declared action count is invalid", code="REPORT_COUNT_INVALID")
    raw_actions = _value(document, "actions", "action_rows", "actionRows", "executions")
    if not isinstance(raw_actions, (list, tuple)):
        raise ReportNormalizationError("actions are required", code="REPORT_SCHEMA_INVALID")
    actions = tuple(_action(item, ordinal) for ordinal, item in enumerate(raw_actions))
    if len(actions) != declared:
        raise ReportNormalizationError("declared action count does not match parsed actions", code="REPORT_COUNT_INVALID")
    if len({action.source_ordinal for action in actions}) != len(actions):
        raise ReportNormalizationError("action source ordinals are duplicated", code="REPORT_COUNT_INVALID")
    period = _mapping(document.get("period", {}), "period")
    start_value = _value(document, "report_start", "start", default=_value(period, "start", "start_utc"))
    end_value = _value(document, "report_end", "end", default=_value(period, "end", "end_utc"))
    start = _timestamp(start_value, "report_start") if start_value is not None else None
    end = _timestamp(end_value, "report_end") if end_value is not None else None
    if start and end and start > end:
        raise ReportNormalizationError("report range is reversed", code="REPORT_RANGE_INVALID")
    for action in actions:
        _validate_range(action.timestamp_utc, start, end, "action")
    series_raw = _mapping(_value(document, "series", "time_series", default={}), "series")
    known_series = {name.casefold().replace("_", "") for name in SERIES_NAMES}
    unknown_series = [str(key) for key in series_raw if str(key).casefold().replace("_", "") not in known_series]
    if unknown_series:
        raise ReportNormalizationError(
            "unsupported report series: " + ", ".join(sorted(unknown_series)),
            code="REPORT_SCHEMA_INVALID",
        )
    series_keys = [str(key).casefold().replace("_", "") for key in series_raw]
    if len(series_keys) != len(set(series_keys)):
        raise ReportNormalizationError("series aliases collide", code="REPORT_SCHEMA_INVALID")
    series_lookup = {key: value for key, value in zip(series_keys, series_raw.values())}
    series = {name: _series_points(series_lookup.get(name.casefold().replace("_", "")), name) for name in SERIES_NAMES}
    for points in series.values():
        for point in points:
            _validate_range(point.timestamp_utc, start, end, "series point")
    portfolio = _summary(_value(document, "portfolio", "portfolio_summary", "result", default={}), "portfolio")
    for aliases in (
        ("action_count", "actionCount", "trades", "Trades", "executions"),
        ("realized_pnl", "realised_pnl"),
        ("fees", "fee"),
        ("funding", "funding_fee"),
        ("net_pnl", "total_pnl"),
        ("initial_equity",),
        ("final_equity",),
    ):
        _summary_value(portfolio, *aliases)
    declared_trades = _summary_value(portfolio, "trades", "executions")
    if declared_trades is not None and declared_trades != declared:
        raise ReportNormalizationError("Trades does not match parsed execution count", code="REPORT_COUNT_INVALID")
    symbols_raw = _value(document, "symbols", "symbol_summaries", default=())
    if isinstance(symbols_raw, Mapping):
        symbols = tuple({"symbol": str(name), **dict(_summary(value, "symbol summary"))} for name, value in sorted(symbols_raw.items(), key=lambda item: str(item[0])))
    elif isinstance(symbols_raw, (list, tuple)):
        symbols = tuple(sorted(({"symbol": str(_value(_mapping(item, "symbol summary"), "symbol", "instrument", default="")), **dict(_summary(item, "symbol summary"))} for item in symbols_raw), key=lambda item: item["symbol"]))
    else:
        raise ReportNormalizationError("symbol summaries must be an array or object")
    actual_leverage_raw = _value(document, "actual_leverage", "applied_leverage", default={})
    actual_leverage: dict[str, Decimal] = {}
    if actual_leverage_raw is not None:
        for name, value in _mapping(actual_leverage_raw, "actual_leverage").items():
            if value is not None:
                actual_leverage[str(name)] = _decimal(value, "actual leverage")
    declared_parser = document.get("parser_version")
    if declared_parser is not None and str(declared_parser) != str(parser_version):
        raise ReportNormalizationError("report parser version does not match the caller", code="REPORT_SCHEMA_INVALID")
    declared_metrics = document.get("metrics_version")
    if declared_metrics is not None and str(declared_metrics) != str(metrics_version):
        raise ReportNormalizationError("report metrics version does not match the caller", code="REPORT_SCHEMA_INVALID")
    actual_source_name = source_name or source_report_name or document.get("source_report_name") or "fixture"
    provenance = ReportProvenance(str(actual_source_name), raw_digest, str(parser_version), str(metrics_version))
    cycles = reconstruct_cycles(actions, report_end=end)
    diagnostics: list[str] = []
    if not series["equity"]:
        diagnostics.append("EQUITY_PATH_MISSING")
    if any(cycle.censored for cycle in cycles):
        diagnostics.append("OPEN_AT_END")
    result = NormalizedReport(REPORT_SCHEMA, version, actual_run, actual_attempt, actual_member, portfolio, symbols, tuple(sorted(actions, key=lambda item: (item.timestamp_utc, item.source_ordinal))), series, cycles, provenance, declared, start, end, MappingProxyType(actual_leverage), tuple(dict.fromkeys(diagnostics)), "", raw_digest)
    semantic_digest = hashlib.sha256(_canonical_bytes(result.semantic_payload())).hexdigest()
    return NormalizedReport(result.schema, result.version, result.run_id, result.attempt_id, result.member, result.portfolio, result.symbols, result.actions, result.series, result.cycles, result.provenance, result.declared_action_count, result.report_start, result.report_end, result.actual_leverage, result.diagnostics, semantic_digest, raw_digest)


class ReportNormalizer:
    """Small adapter object for callers that keep one decoder per tester."""

    def __init__(self, decoder: Any = None, *, parser_version: str = PARSER_VERSION, metrics_version: str = METRICS_VERSION) -> None:
        self.decoder = decoder
        self.parser_version = parser_version
        self.metrics_version = metrics_version

    def normalize(self, raw: bytes | bytearray | Mapping[str, Any], **kwargs: Any) -> NormalizedReport:
        kwargs.setdefault("decoder", self.decoder)
        kwargs.setdefault("parser_version", self.parser_version)
        kwargs.setdefault("metrics_version", self.metrics_version)
        return normalize_report(raw, **kwargs)

    parse = normalize


PortfolioReportNormalizer = ReportNormalizer


parse_report = normalize_report
normalize_portfolio_report = normalize_report
normalize = normalize_report
PortfolioReport = NormalizedReport
NormalizedPortfolioReport = NormalizedReport
Action = ReportAction
SeriesPoint = ReportSeriesPoint
Cycle = PositionCycle
reconstruct_position_cycles = reconstruct_cycles


def compare_semantic_results(
    first: NormalizedReport,
    second: NormalizedReport,
    *,
    executable_identity: Mapping[str, Any] | None = None,
    first_executable_identity: Mapping[str, Any] | None = None,
    second_executable_identity: Mapping[str, Any] | None = None,
    same_executable: bool | None = None,
) -> str:
    """Classify repeat output; raw formatting differences are immaterial."""
    if same_executable is None:
        if first_executable_identity is not None or second_executable_identity is not None:
            first_executable_identity = first_executable_identity if first_executable_identity is not None else executable_identity
            second_executable_identity = second_executable_identity if second_executable_identity is not None else executable_identity
            known = all(
                isinstance(value, Mapping) and bool(value) and all(item is not None for item in value.values())
                for value in (first_executable_identity, second_executable_identity)
            )
            if not known or first_executable_identity != second_executable_identity:
                return "UNKNOWN"
            same_executable = True
        elif executable_identity is not None:
            known = isinstance(executable_identity, Mapping) and bool(executable_identity) and all(item is not None for item in executable_identity.values())
            if not known:
                return "UNKNOWN"
            same_executable = True
        else:
            return "UNKNOWN"
    elif not same_executable:
        return "UNKNOWN"
    if first.semantic_digest == second.semantic_digest:
        return "DETERMINISTIC"
    return "NONDETERMINISTIC_RESULT"


classify_semantic_identity = compare_semantic_results


def raw_sha256(raw: bytes | bytearray | Mapping[str, Any]) -> str:
    return _raw_digest(raw)


def canonical_semantic_digest(value: NormalizedReport | Any) -> str:
    if isinstance(value, NormalizedReport):
        return value.semantic_digest
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


__all__ = [
    "REPORT_CONTRACT", "REPORT_SCHEMA", "REPORT_VERSION", "PARSER_VERSION", "METRICS_VERSION", "SERIES_NAMES", "BLOCKING_DIAGNOSTICS",
    "ReportNormalizationError", "ReportProvenance", "ReportSeriesPoint", "ReportAction", "PositionCycle", "NormalizedReport", "ReportNormalizer", "PortfolioReportNormalizer",
    "normalize_report", "normalize_portfolio_report", "parse_report", "normalize", "PortfolioReport", "NormalizedPortfolioReport", "Action", "SeriesPoint", "Cycle", "reconstruct_cycles", "reconstruct_position_cycles",
    "compare_semantic_results", "classify_semantic_identity", "raw_sha256", "canonical_semantic_digest",
]
