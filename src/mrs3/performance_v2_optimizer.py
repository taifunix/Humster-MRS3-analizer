"""Private, revision-bound optimizer preparation for Performance v2.

Canonical builders and worker functions are pure CPU code.  The orchestration
and strict readback helpers in this module own the small amount of DuckDB
access needed to load and publish revision-bound prepared inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping, Sequence


PREPARATION_VERSION = "5"
PREPARED_SCHEMA_VERSION = "1"
PREPARED_MAX_BYTES = 16 * 1024 * 1024
MISSING_TYPED_FACTS = "MISSING_TYPED_FACTS"
UNSUPPORTED_SIZING = "UNSUPPORTED_SIZING"
PREPARED_TOO_LARGE = "PREPARED_TOO_LARGE"
EXPECTED_UNAVAILABILITY_REASONS = frozenset({MISSING_TYPED_FACTS, UNSUPPORTED_SIZING, PREPARED_TOO_LARGE})


class OptimizerIntegrityError(ValueError):
    """A malformed or identity-inconsistent optimizer input/artifact."""


class OptimizerUnavailableError(RuntimeError):
    """An expected evidence or size condition, safe to persist as unavailable."""

    def __init__(self, reason: str) -> None:
        if reason not in EXPECTED_UNAVAILABILITY_REASONS:
            raise ValueError("invalid optimizer availability reason")
        super().__init__(reason)
        self.reason = reason


def _decimal(value: Any, field: str, *, required: bool = True) -> Decimal | None:
    if value is None:
        if required:
            raise OptimizerIntegrityError(f"{field} is required")
        return None
    if isinstance(value, bool):
        raise OptimizerIntegrityError(f"{field} must be decimal")
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise OptimizerIntegrityError(f"{field} must be decimal") from error
    if not value.is_finite():
        raise OptimizerIntegrityError(f"{field} must be finite")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -12:
        raise OptimizerIntegrityError(f"{field} exceeds DECIMAL(38,12) scale")
    if value.adjusted() >= 26:
        raise OptimizerIntegrityError(f"{field} exceeds DECIMAL(38,12) precision")
    digits = len(value.as_tuple().digits)
    adjusted = value.adjusted() if value else 0
    if max(digits, adjusted + 1 if adjusted >= 0 else digits) > 38:
        raise OptimizerIntegrityError(f"{field} exceeds DECIMAL(38,12) precision")
    return value


def canonical_decimal(value: Any, field: str = "decimal") -> str:
    parsed = _decimal(value, field)
    assert parsed is not None
    if parsed == 0:
        parsed = Decimal(0)
    # The scale check above makes this formatting exact; quantize cannot round
    # a value with more than twelve fractional digits because it was rejected.
    try:
        rendered = format(parsed, "f")
        if "." not in rendered:
            rendered += "."
        whole, fraction = rendered.split(".", 1)
        rendered = whole + "." + fraction.ljust(12, "0")
        if len(fraction) > 12:
            rendered = whole + "." + fraction[:12]
    except (ValueError, InvalidOperation) as error:
        raise OptimizerIntegrityError(f"{field} cannot be canonicalized") from error
    return rendered


def _render_decimal(value: Decimal) -> str:
    """Render derived artifact decimals without introducing an exponent."""
    if value == 0:
        value = Decimal(0)
    rendered = format(value, "f")
    if "." not in rendered:
        rendered += ".0"
    return rendered


def canonical_timestamp(value: Any, field: str = "timestamp_utc") -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise OptimizerIntegrityError(f"{field} must be ISO-8601") from error
    else:
        raise OptimizerIntegrityError(f"{field} must be a timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OptimizerIntegrityError(f"{field} must be timezone-aware")
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise OptimizerIntegrityError("decimal must be finite")
        return _render_decimal(value)
    if isinstance(value, datetime):
        return canonical_timestamp(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        raise OptimizerIntegrityError("floating-point values are not canonical input")
    raise OptimizerIntegrityError(f"unsupported canonical value {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _nonnegative_ordinal(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OptimizerIntegrityError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class OptimizerAction:
    action_index: int
    timestamp_utc: datetime | str
    symbol: str
    action: str
    side: str | None = None
    order_id: str | None = None
    size: Decimal | None = None
    price: Decimal | None = None
    cost: Decimal | None = None
    fee: Decimal | None = None
    pnl: Decimal | None = None
    balance: Decimal | None = None
    post_size: Decimal | None = None
    post_side: str | None = None
    pre_size: Decimal | None = None
    pre_side: str | None = None
    qty_delta: Decimal | None = None
    close_attribution: str | None = None

    def to_document(self) -> dict[str, Any]:
        ordinal = _nonnegative_ordinal(self.action_index, "action_index")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise OptimizerIntegrityError("action symbol is required")
        if not isinstance(self.action, str) or not self.action.strip():
            raise OptimizerIntegrityError("action kind is required")
        result: dict[str, Any] = {
            "action_index": ordinal,
            "timestamp_utc": canonical_timestamp(self.timestamp_utc),
            "symbol": self.symbol,
            "action": self.action,
        }
        for name in (
            "side", "order_id", "post_side", "pre_side", "close_attribution",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        for name in (
            "size", "price", "cost", "fee", "pnl", "balance", "post_size",
            "pre_size", "qty_delta",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = canonical_decimal(value, f"action.{name}")
        return result


@dataclass(frozen=True, slots=True)
class OptimizerEquityPoint:
    sample_index: int
    timestamp_utc: datetime | str
    equity: Decimal

    def to_document(self) -> dict[str, Any]:
        return {
            "sample_index": _nonnegative_ordinal(self.sample_index, "sample_index"),
            "timestamp_utc": canonical_timestamp(self.timestamp_utc),
            "equity": canonical_decimal(self.equity, "equity"),
        }


def _action(value: OptimizerAction | Mapping[str, Any], ordinal: int) -> OptimizerAction:
    if isinstance(value, OptimizerAction):
        return value
    if not isinstance(value, Mapping):
        raise OptimizerIntegrityError("action must be a typed mapping")
    aliases = {"source_ordinal": "action_index", "ordinal": "action_index", "timestamp": "timestamp_utc", "time": "timestamp_utc"}
    data = dict(value)
    for old, new in aliases.items():
        if new not in data and old in data:
            data[new] = data[old]
    data.setdefault("action_index", ordinal)
    if "symbol" not in data and "instrument" in data:
        data["symbol"] = data["instrument"]
    if "post_size" not in data and "position_size" in data:
        data["post_size"] = data["position_size"]
    if "price" not in data and "fill_price" in data:
        data["price"] = data["fill_price"]
    if "cost" not in data and "notional" in data:
        data["cost"] = data["notional"]
    allowed = {field for field in OptimizerAction.__dataclass_fields__}
    aliases_seen = set(aliases) | {"instrument", "position_size", "fill_price", "notional", "result_id", "raw_action_json"}
    unknown = set(data).difference(allowed | aliases_seen)
    if unknown:
        raise OptimizerIntegrityError("action contains unsupported fields")
    return OptimizerAction(**{key: item for key, item in data.items() if key in allowed})


def _equity(value: OptimizerEquityPoint | Mapping[str, Any] | Sequence[Any], ordinal: int) -> OptimizerEquityPoint:
    if isinstance(value, OptimizerEquityPoint):
        return value
    if isinstance(value, Mapping):
        data = dict(value)
        data.setdefault("sample_index", data.get("source_ordinal", data.get("ordinal", ordinal)))
        data.setdefault("timestamp_utc", data.get("timestamp", data.get("time")))
        data.setdefault("equity", data.get("value"))
        unknown = set(data).difference({"sample_index", "source_ordinal", "ordinal", "timestamp_utc", "timestamp", "time", "equity", "value", "result_id", "wallet"})
        if unknown:
            raise OptimizerIntegrityError("equity contains unsupported fields")
        return OptimizerEquityPoint(**{key: data[key] for key in ("sample_index", "timestamp_utc", "equity")})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) in {2, 3}:
        return OptimizerEquityPoint(value[2] if len(value) == 3 else ordinal, value[0], value[1])
    raise OptimizerIntegrityError("equity point must be a typed mapping")


@dataclass(frozen=True, slots=True)
class OptimizerSourceInput:
    source_document_version: str
    result_id: int
    strategy_id: int
    symbol: str
    side: str
    revision_timestamp_utc: datetime | str
    report_start_utc: datetime | str
    report_end_utc: datetime | str
    effective_start_utc: datetime | str
    effective_end_utc: datetime | str
    initial_balance: Decimal
    sizing_use_upnl: bool | None
    sizing_use_frozen_balance: bool | None
    sizing_use_fix: bool | None
    sizing_balance_percentage_long: Decimal | None
    sizing_risk_long: Decimal | None
    sizing_max_balance: Decimal | None
    actions: Sequence[OptimizerAction | Mapping[str, Any]]
    equity: Sequence[OptimizerEquityPoint | Mapping[str, Any] | Sequence[Any]]

    def __post_init__(self) -> None:
        # Validate at construction so an over-scale typed fact can never be
        # carried further and accidentally quantized by a later writer.
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "equity", tuple(self.equity))
        self.to_document()

    def to_document(self) -> dict[str, Any]:
        for field in ("result_id", "strategy_id"):
            _nonnegative_ordinal(getattr(self, field), field)
        for field in ("source_document_version", "symbol", "side"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise OptimizerIntegrityError(f"{field} is required")
        actions = tuple(_action(item, index) for index, item in enumerate(self.actions))
        equities = tuple(_equity(item, index) for index, item in enumerate(self.equity))
        action_documents = tuple(item.to_document() for item in actions)
        equity_documents = tuple(item.to_document() for item in equities)
        if len({item["action_index"] for item in action_documents}) != len(action_documents):
            raise OptimizerIntegrityError("action ordinals are duplicated")
        if len({item["sample_index"] for item in equity_documents}) != len(equity_documents):
            raise OptimizerIntegrityError("equity ordinals are duplicated")
        action_documents = tuple(sorted(action_documents, key=lambda item: (item["timestamp_utc"], item["action_index"])))
        equity_documents = tuple(sorted(equity_documents, key=lambda item: (item["timestamp_utc"], item["sample_index"])))
        sizing: dict[str, Any] = {}
        for name in ("sizing_use_upnl", "sizing_use_frozen_balance", "sizing_use_fix"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise OptimizerIntegrityError(f"{name} must be boolean or null")
            sizing[name] = value
        for name in ("sizing_balance_percentage_long", "sizing_risk_long", "sizing_max_balance"):
            sizing[name] = None if getattr(self, name) is None else canonical_decimal(getattr(self, name), name)
        return {
            "source_document_version": self.source_document_version,
            "result_id": self.result_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            "revision_timestamp_utc": canonical_timestamp(self.revision_timestamp_utc, "revision_timestamp_utc"),
            "report_start_utc": canonical_timestamp(self.report_start_utc, "report_start_utc"),
            "report_end_utc": canonical_timestamp(self.report_end_utc, "report_end_utc"),
            "effective_start_utc": canonical_timestamp(self.effective_start_utc, "effective_start_utc"),
            "effective_end_utc": canonical_timestamp(self.effective_end_utc, "effective_end_utc"),
            "initial_balance": canonical_decimal(self.initial_balance, "initial_balance"),
            "sizing": sizing,
            "actions": list(action_documents),
            "equity": list(equity_documents),
        }


def source_digest(value: OptimizerSourceInput | Mapping[str, Any]) -> str:
    document = value.to_document() if isinstance(value, OptimizerSourceInput) else value
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _source_action_rows(source: OptimizerSourceInput) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for item in source.to_document()["actions"]:
        result.append(dict(item))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PreparedAvailability:
    status: str
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "AVAILABLE"


def prepared_availability(source: OptimizerSourceInput) -> PreparedAvailability:
    document = source.to_document()
    if not document["actions"] or not document["equity"]:
        return PreparedAvailability("UNAVAILABLE", MISSING_TYPED_FACTS)
    required_action_fields = ("price", "cost", "post_size", "balance")
    if any(any(item.get(field) is None for field in required_action_fields) for item in document["actions"]):
        return PreparedAvailability("UNAVAILABLE", MISSING_TYPED_FACTS)
    sizing = document["sizing"]
    if any(sizing[name] is None for name in sizing):
        return PreparedAvailability("UNAVAILABLE", MISSING_TYPED_FACTS)
    if not (
        sizing["sizing_use_upnl"] is True
        and sizing["sizing_use_frozen_balance"] is True
        and sizing["sizing_use_fix"] is False
        and sizing["sizing_balance_percentage_long"] == "100.000000000000"
        and sizing["sizing_risk_long"] == "1.000000000000"
        and sizing["sizing_max_balance"] == "0.000000000000"
    ):
        return PreparedAvailability("UNAVAILABLE", UNSUPPORTED_SIZING)
    return PreparedAvailability("AVAILABLE")


def _cycle_records(source: OptimizerSourceInput) -> tuple[dict[str, Any], ...]:
    # The existing report adapter/reconstructor is the sole cycle algorithm.
    from .portfolio.reports import performance_rows_to_report_actions, reconstruct_cycles

    document = source.to_document()
    sizing = document["sizing"]
    dynamic_basis = (
        sizing.get("sizing_use_upnl") is True
        and sizing.get("sizing_use_frozen_balance") is True
        and sizing.get("sizing_use_fix") is False
        and sizing.get("sizing_balance_percentage_long") == "100.000000000000"
        and sizing.get("sizing_risk_long") == "1.000000000000"
        and sizing.get("sizing_max_balance") == "0.000000000000"
    )
    rows = []
    for item in document["actions"]:
        row = dict(item)
        row["action_index"] = item["action_index"]
        row["timestamp_utc"] = item["timestamp_utc"]
        rows.append(row)
    actions = performance_rows_to_report_actions(rows)
    reconstructed = reconstruct_cycles(actions, report_end=document["report_end_utc"])
    by_ordinal = {int(item["action_index"]): item for item in rows}
    opening_candidates: dict[str, list[Any]] = {}
    closing_candidates: dict[str, list[Any]] = {}
    for action in actions:
        if action.pre_size == Decimal("0") and action.post_size not in (None, Decimal("0")):
            opening_candidates.setdefault(action.timestamp_utc, []).append(action)
        if action.post_size == Decimal("0") and action.pre_size not in (None, Decimal("0")):
            closing_candidates.setdefault(action.timestamp_utc, []).append(action)
    result: list[dict[str, Any]] = []
    for cycle in reconstructed:
        basis = None
        opening_source_ordinal = None
        diagnostics = cycle.diagnostics
        if cycle.opened_at is not None:
            opening_queue = opening_candidates.get(cycle.opened_at, [])
            opening = opening_queue.pop(0) if opening_queue else None
            if opening is None:
                diagnostics = tuple(dict.fromkeys((*diagnostics, "SOURCE_OPENING_ROW_UNMATCHED")))
            if opening is not None:
                opening_source_ordinal = opening.source_ordinal
                source_row = by_ordinal.get(opening.source_ordinal, {})
                if dynamic_basis:
                    balance = Decimal(source_row["balance"])
                    pnl = Decimal(source_row["pnl"]) if source_row.get("pnl") is not None else Decimal("0")
                    fee = Decimal(source_row["fee"]) if source_row.get("fee") is not None else Decimal("0")
                    basis = balance - pnl + fee
                    if basis <= 0:
                        basis = None
        closing_balance = None
        if not cycle.censored and cycle.closed_at is not None:
            closing_queue = closing_candidates.get(cycle.closed_at, [])
            closing = closing_queue.pop(0) if closing_queue else None
            if closing is not None:
                raw_balance = by_ordinal[int(closing.source_ordinal)].get("balance")
                closing_balance = Decimal(raw_balance) if raw_balance is not None else None
        normalized_pnl = None
        if basis is not None and closing_balance is not None and not cycle.censored:
            normalized_pnl = (closing_balance - basis) / basis
        result.append({
            "cycle_id": cycle.cycle_id,
            "symbol": cycle.symbol,
            "opened_at": cycle.opened_at,
            "closed_at": cycle.closed_at,
            "duration_seconds": cycle.duration_seconds,
            "censored": cycle.censored,
            "carry_in": cycle.carry_in,
            "side": cycle.side,
            "realized_pnl": cycle.realized_pnl,
            "fees": cycle.fees,
            "close_attribution": cycle.close_attribution,
            "source_basis": basis,
            "maximum_position": cycle.maximum_position,
            "execution_count": cycle.execution_count,
            "diagnostics": diagnostics,
            "strategy_id": source.strategy_id,
            "source_ordinal": opening_source_ordinal,
            "normalized_pnl": normalized_pnl,
            "attribution_complete": False,
        })
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PreparedOptimizerInput:
    preparation_version: str
    result_id: int
    source_digest: str
    actions: tuple[Mapping[str, Any], ...]
    equity: tuple[Mapping[str, Any], ...]
    cycles: tuple[Mapping[str, Any], ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "prepared_schema_version": PREPARED_SCHEMA_VERSION,
            "preparation_version": self.preparation_version,
            "result_id": self.result_id,
            "source_digest": self.source_digest,
            "actions": list(self.actions),
            "equity": list(self.equity),
            "cycles": list(self.cycles),
        }

    def to_json(self) -> str:
        encoded = canonical_json(self.to_document())
        if len(encoded.encode("utf-8")) > PREPARED_MAX_BYTES:
            raise OptimizerUnavailableError(PREPARED_TOO_LARGE)
        return encoded


@dataclass(frozen=True, slots=True)
class PreparedCurrentResult:
    source: OptimizerSourceInput
    availability: PreparedAvailability
    prepared: PreparedOptimizerInput | None


def _source_rows(
    connection: Any,
    result_ids: Sequence[int] | None,
    *,
    current_only: bool = False,
) -> tuple[OptimizerSourceInput, ...]:
    if result_ids is not None and not result_ids:
        return ()
    if result_ids is not None:
        placeholders = ",".join("?" for _ in result_ids)
        predicate = f"where r.result_id in ({placeholders})" + (" and s.current_result_id = r.result_id" if current_only else "")
        parameters: list[Any] = list(result_ids)
    else:
        predicate = "where s.current_result_id is not null"
        parameters = []
    result_rows = connection.execute(
        f"""select r.result_id, r.strategy_id, s.symbol, s.side,
                  r.report_start_utc, r.report_end_utc, r.imported_at_utc,
                  r.effective_start_utc, r.effective_end_utc, r.initial_balance,
                  r.sizing_use_upnl, r.sizing_use_frozen_balance, r.sizing_use_fix,
                  r.sizing_balance_percentage_long, r.sizing_risk_long, r.sizing_max_balance
             from strategy_results r join strategies s on s.strategy_id = r.strategy_id
             {predicate} order by r.result_id""",
        parameters,
    ).fetchall()
    if not result_rows:
        return ()
    ids = tuple(int(row[0]) for row in result_rows)
    placeholders = ",".join("?" for _ in ids)
    action_rows = connection.execute(
        f"""select result_id, action_index, timestamp_utc, symbol, order_id, action,
                  size, post_size, post_side, pnl, fee, balance, price, cost
             from strategy_actions where result_id in ({placeholders})
             order by result_id, action_index""",
        list(ids),
    ).fetchall()
    equity_rows = connection.execute(
        f"""select result_id, sample_index, timestamp_utc, equity
             from strategy_equity where result_id in ({placeholders})
             order by result_id, sample_index""",
        list(ids),
    ).fetchall()
    actions_by_id: dict[int, list[dict[str, Any]]] = {result_id: [] for result_id in ids}
    equity_by_id: dict[int, list[dict[str, Any]]] = {result_id: [] for result_id in ids}
    for row in action_rows:
        actions_by_id[int(row[0])].append({
            "action_index": row[1], "timestamp_utc": row[2], "symbol": row[3],
            "order_id": row[4], "action": row[5], "size": row[6], "post_size": row[7],
            "post_side": row[8], "pnl": row[9], "fee": row[10], "balance": row[11],
            "price": row[12], "cost": row[13],
        })
    for row in equity_rows:
        equity_by_id[int(row[0])].append({"sample_index": row[1], "timestamp_utc": row[2], "equity": row[3]})
    result: list[OptimizerSourceInput] = []
    for row in result_rows:
        result_id = int(row[0])
        result.append(OptimizerSourceInput(
            source_document_version="performance-v2", result_id=result_id, strategy_id=int(row[1]),
            symbol=str(row[2]), side=str(row[3]), revision_timestamp_utc=row[6],
            report_start_utc=row[4], report_end_utc=row[5],
            effective_start_utc=row[7] or row[4], effective_end_utc=row[8] or row[5],
            initial_balance=row[9], sizing_use_upnl=row[10], sizing_use_frozen_balance=row[11],
            sizing_use_fix=row[12], sizing_balance_percentage_long=row[13],
            sizing_risk_long=row[14], sizing_max_balance=row[15],
            actions=tuple(actions_by_id[result_id]), equity=tuple(equity_by_id[result_id]),
        ))
    return tuple(result)


def _prepare_current_worker(source: OptimizerSourceInput) -> PreparedCurrentResult:
    availability, prepared = prepare_optimizer_input(source)
    return PreparedCurrentResult(source, availability, prepared)


def prepare_current_optimizer_inputs(database: str, result_ids: Sequence[int] | None = None, *, workers: int = 1) -> tuple[PreparedCurrentResult, ...]:
    """Prepare current rows using a closed read snapshot and one writer batch."""
    from concurrent.futures import ProcessPoolExecutor
    import duckdb
    from .performance_v2_store import PerformanceV2WriterLock, require_performance_v2

    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if result_ids is not None and not result_ids:
        return ()
    path = str(database)
    reusable: dict[int, PreparedCurrentResult] = {}
    with duckdb.connect(path, read_only=True) as connection:
        require_performance_v2(connection)
        sources = _source_rows(connection, result_ids, current_only=True)
        if sources:
            placeholders = ",".join("?" for _ in sources)
            existing_rows = connection.execute(
                f"""select result_id, preparation_version, source_digest,
                           availability_status, unavailable_reason, prepared_json
                      from optimizer_prepared_inputs where result_id in ({placeholders})""",
                [source.result_id for source in sources],
            ).fetchall()
            existing_by_id = {int(row[0]): row for row in existing_rows}
            pending: list[OptimizerSourceInput] = []
            for source in sources:
                digest = source_digest(source)
                row = existing_by_id.get(source.result_id)
                if row is None or row[1] != PREPARATION_VERSION or row[2] != digest:
                    pending.append(source)
                    continue
                try:
                    if row[3] == "UNAVAILABLE" and row[4] in EXPECTED_UNAVAILABILITY_REASONS and row[5] is None:
                        reusable[source.result_id] = PreparedCurrentResult(
                            source, PreparedAvailability("UNAVAILABLE", row[4]), None
                        )
                    elif row[3] == "AVAILABLE" and row[4] is None and row[5] is not None:
                        reusable[source.result_id] = PreparedCurrentResult(
                            source, PreparedAvailability("AVAILABLE"),
                            decode_prepared_input(row[5], result_id=source.result_id, digest=digest),
                        )
                    else:
                        raise OptimizerIntegrityError("prepared row is malformed")
                except OptimizerIntegrityError:
                    pending.append(source)
    sources_to_build = tuple(pending) if sources else ()
    if workers == 1 or len(sources_to_build) <= 1:
        built = tuple(_prepare_current_worker(source) for source in sources_to_build)
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(sources_to_build))) as executor:
            built = tuple(executor.map(_prepare_current_worker, sources_to_build))
    built_by_id = {item.source.result_id: item for item in built}
    all_results = tuple(reusable.get(source.result_id) or built_by_id[source.result_id] for source in sources)
    path_object = __import__("pathlib").Path(database).resolve()
    with PerformanceV2WriterLock(path_object.parent):
        with duckdb.connect(str(path_object)) as connection:
            require_performance_v2(connection)
            connection.execute("begin")
            try:
                for item in all_results:
                    current = connection.execute(
                        """select s.current_result_id from strategies s
                             where s.strategy_id = ? and s.current_result_id = ?""",
                        [item.source.strategy_id, item.source.result_id],
                    ).fetchone()
                    if current is None:
                        continue
                    latest = _source_rows(connection, (item.source.result_id,), current_only=True)
                    if not latest or source_digest(latest[0]) != source_digest(item.source):
                        continue
                    existing = connection.execute(
                        """select preparation_version, source_digest, availability_status,
                                  unavailable_reason from optimizer_prepared_inputs where result_id = ?""",
                        [item.source.result_id],
                    ).fetchone()
                    if existing is not None and (
                        existing[0] == PREPARATION_VERSION
                        and existing[1] == source_digest(item.source)
                        and existing[2] == item.availability.status
                        and existing[3] == item.availability.reason
                    ):
                        continue
                    connection.execute("delete from optimizer_prepared_inputs where result_id = ?", [item.source.result_id])
                    connection.execute(
                        """insert into optimizer_prepared_inputs
                           (result_id, preparation_version, source_digest, availability_status,
                            unavailable_reason, prepared_json, prepared_at_utc)
                           values (?, ?, ?, ?, ?, ?, ?)""",
                        [
                            item.source.result_id, PREPARATION_VERSION, source_digest(item.source),
                            item.availability.status, item.availability.reason,
                            item.prepared.to_json() if item.prepared is not None else None,
                            datetime.now(timezone.utc),
                        ],
                    )
                connection.execute("commit")
            except Exception:
                connection.execute("rollback")
                raise
    return all_results


def _read_prepared_optimizer_inputs(
    connection: Any, result_ids: Sequence[int]
) -> tuple[PreparedCurrentResult, ...]:
    """Strictly read prepared rows from an already-open snapshot."""
    requested = tuple(dict.fromkeys(int(item) for item in result_ids))
    if not requested:
        return ()
    sources = _source_rows(connection, requested)
    by_id = {source.result_id: source for source in sources}
    placeholders = ",".join("?" for _ in requested)
    rows = connection.execute(
        f"""select result_id, preparation_version, source_digest,
                  availability_status, unavailable_reason, prepared_json
             from optimizer_prepared_inputs where result_id in ({placeholders})""",
        list(requested),
    ).fetchall()
    by_prepared = {int(row[0]): row for row in rows}
    result: list[PreparedCurrentResult] = []
    for result_id in requested:
        source = by_id.get(result_id)
        row = by_prepared.get(result_id)
        if source is None or row is None:
            raise OptimizerIntegrityError("prepared input is missing for requested result")
        digest = source_digest(source)
        if row[1] != PREPARATION_VERSION or row[2] != digest:
            raise OptimizerIntegrityError("prepared input is stale")
        if row[3] == "UNAVAILABLE":
            reason = row[4]
            if reason not in EXPECTED_UNAVAILABILITY_REASONS or row[5] is not None:
                raise OptimizerIntegrityError("prepared unavailable row is invalid")
            result.append(PreparedCurrentResult(source, PreparedAvailability("UNAVAILABLE", reason), None))
            continue
        if row[3] != "AVAILABLE" or row[4] is not None or row[5] is None:
            raise OptimizerIntegrityError("prepared available row is invalid")
        prepared = decode_prepared_input(row[5], result_id=result_id, digest=digest)
        result.append(PreparedCurrentResult(source, PreparedAvailability("AVAILABLE"), prepared))
    return tuple(result)


def read_prepared_optimizer_inputs(database: str, result_ids: Sequence[int]) -> tuple[PreparedCurrentResult, ...]:
    """Strictly read revision-bound prepared rows; never consult compatibility JSON."""
    import duckdb
    from .performance_v2_store import require_performance_v2

    requested = tuple(dict.fromkeys(int(item) for item in result_ids))
    if not requested:
        return ()
    with duckdb.connect(str(database), read_only=True) as connection:
        require_performance_v2(connection)
        return _read_prepared_optimizer_inputs(connection, requested)


def build_prepared_input(source: OptimizerSourceInput, *, preparation_version: str = PREPARATION_VERSION) -> PreparedOptimizerInput:
    if not isinstance(preparation_version, str) or not preparation_version.strip():
        raise OptimizerIntegrityError("preparation_version is required")
    digest = source_digest(source)
    availability = prepared_availability(source)
    if not availability.available:
        raise OptimizerUnavailableError(availability.reason or MISSING_TYPED_FACTS)
    document = source.to_document()
    prepared = PreparedOptimizerInput(
        preparation_version.strip(), source.result_id, digest,
        tuple(document["actions"]), tuple(document["equity"]), _cycle_records(source),
    )
    prepared.to_json()  # apply the exact stored-byte limit before persistence
    return prepared


def prepare_optimizer_input(source: OptimizerSourceInput, *, preparation_version: str = PREPARATION_VERSION) -> tuple[PreparedAvailability, PreparedOptimizerInput | None]:
    digest = source_digest(source)
    try:
        prepared = build_prepared_input(source, preparation_version=preparation_version)
    except OptimizerUnavailableError as error:
        return PreparedAvailability("UNAVAILABLE", error.reason), None
    return PreparedAvailability("AVAILABLE"), prepared


def decode_prepared_input(payload: str | bytes | Mapping[str, Any], *, result_id: int, digest: str, preparation_version: str = PREPARATION_VERSION) -> PreparedOptimizerInput:
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        value = json.loads(payload) if isinstance(payload, str) else payload
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise OptimizerIntegrityError("prepared artifact is malformed JSON") from error
    if not isinstance(value, Mapping):
        raise OptimizerIntegrityError("prepared artifact must be an object")
    if value.get("prepared_schema_version") != PREPARED_SCHEMA_VERSION:
        raise OptimizerIntegrityError("prepared artifact schema version is invalid")
    if not isinstance(value.get("preparation_version"), str) or not value["preparation_version"].strip():
        raise OptimizerIntegrityError("prepared artifact preparation version is invalid")
    if isinstance(value.get("result_id"), bool) or not isinstance(value.get("result_id"), int):
        raise OptimizerIntegrityError("prepared artifact result identity is invalid")
    if not isinstance(value.get("source_digest"), str) or len(value["source_digest"]) != 64 or any(char not in "0123456789abcdef" for char in value["source_digest"]):
        raise OptimizerIntegrityError("prepared artifact source digest is invalid")
    if value.get("preparation_version") != preparation_version or value.get("result_id") != result_id or value.get("source_digest") != digest:
        raise OptimizerIntegrityError("prepared artifact identity is stale")
    for name in ("actions", "equity", "cycles"):
        if not isinstance(value.get(name), list):
            raise OptimizerIntegrityError(f"prepared artifact {name} is invalid")
        if any(not isinstance(item, Mapping) for item in value[name]):
            raise OptimizerIntegrityError(f"prepared artifact {name} item is invalid")
    for name, ordinal_name in (("actions", "action_index"), ("equity", "sample_index")):
        ordinals = []
        for item in value[name]:
            ordinal = item.get(ordinal_name)
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise OptimizerIntegrityError(f"prepared artifact {name} ordinal is invalid")
            ordinals.append(ordinal)
        if len(set(ordinals)) != len(ordinals):
            raise OptimizerIntegrityError(f"prepared artifact {name} ordinals are duplicated")
        ordered = sorted(value[name], key=lambda item: (item.get("timestamp_utc"), item.get(ordinal_name)))
        if list(value[name]) != ordered:
            raise OptimizerIntegrityError(f"prepared artifact {name} ordering is invalid")
    for collection_name in ("actions", "equity", "cycles"):
        for item in value[collection_name]:
            for field, item_value in item.items():
                if field.endswith("_at") or field.endswith("_utc"):
                    if item_value is not None:
                        canonical_timestamp(item_value, field)
                elif field in {"price", "cost", "size", "post_size", "pre_size", "qty_delta", "fee", "pnl", "balance", "equity", "duration_seconds", "realized_pnl", "fees", "maximum_position", "normalized_pnl", "source_basis"}:
                    if item_value is not None:
                        if not isinstance(item_value, str) or "e" in item_value.lower():
                            raise OptimizerIntegrityError(f"prepared artifact decimal {field} is invalid")
                        try:
                            parsed = Decimal(item_value)
                        except InvalidOperation as error:
                            raise OptimizerIntegrityError(f"prepared artifact decimal {field} is invalid") from error
                        if not parsed.is_finite():
                            raise OptimizerIntegrityError(f"prepared artifact decimal {field} is invalid")
                        if collection_name in {"actions", "equity"} and canonical_decimal(item_value, field) != item_value:
                            raise OptimizerIntegrityError(f"prepared artifact decimal {field} is not canonical")
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > PREPARED_MAX_BYTES:
        raise OptimizerIntegrityError("prepared artifact exceeds size limit")
    return PreparedOptimizerInput(
        str(value["preparation_version"]), int(value["result_id"]), str(value["source_digest"]),
        tuple(value["actions"]), tuple(value["equity"]), tuple(value["cycles"]),
    )


__all__ = [
    "EXPECTED_UNAVAILABILITY_REASONS", "MISSING_TYPED_FACTS", "PREPARATION_VERSION",
    "PREPARED_MAX_BYTES", "PREPARED_SCHEMA_VERSION", "PREPARED_TOO_LARGE",
    "UNSUPPORTED_SIZING", "OptimizerAction", "OptimizerEquityPoint",
    "OptimizerIntegrityError", "OptimizerSourceInput", "OptimizerUnavailableError",
    "PreparedAvailability", "PreparedOptimizerInput", "build_prepared_input",
    "canonical_decimal", "canonical_json", "canonical_timestamp", "decode_prepared_input",
    "prepare_optimizer_input", "prepared_availability", "source_digest",
    "PreparedCurrentResult", "prepare_current_optimizer_inputs", "read_prepared_optimizer_inputs",
]
