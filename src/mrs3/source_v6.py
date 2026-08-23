"""Source v6 HTML normalisation primitives.

This module intentionally stops at an immutable, normalised fragment.  DuckDB
publication, overlap ownership and surface materialisation consume these facts
in later stages and must not re-parse HTML.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
import re
import zlib
from collections import defaultdict
from typing import Mapping

from .performance import (
    ParsedPerformanceReport,
    PerformanceParseError,
    parse_performance_report,
    report_range,
)


SOURCE_V6_SCHEMA_VERSION = 2
# Strict inverse of str(int): rejects "+5", " 5 ", "3_0", "-0", "05", "007".
_INTEGER_FIELD = re.compile(r"0|-?[1-9][0-9]*")
EXECUTION_FINGERPRINT_VERSION = "execution_compatibility_fingerprint_v1"


class SourceV6Error(ValueError):
    """Raised when an HTML report cannot form a lossless v6 fragment."""


def _finite_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise SourceV6Error(f"{field} must be a finite decimal") from error
    if not result.is_finite():
        raise SourceV6Error(f"{field} must be a finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    """Emit exact fixed-point text, collapsing exponent/zero forms without context-rounding digits."""
    if not value.is_finite():
        raise SourceV6Error("non-finite decimal cannot be canonicalized")
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, float):
        return _decimal_text(_finite_decimal(value, "settings value"))
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise SourceV6Error(f"unsupported settings value type: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(_canonical_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: object) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise SourceV6Error(f"invalid action timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        # The tester's HTML trade table emits ``YYYY-MM-DD HH:MM:SS``
        # without an offset.  Its accompanying wallet/equity series are
        # epoch milliseconds in UTC, and ``performance._timestamp`` already
        # defines this exact display form as UTC.  Keep the same explicit
        # convention here so the normalizer does not reject real tester
        # reports while still rejecting other ambiguous forms below.
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
            raise SourceV6Error("action timestamp must be timezone-aware UTC")
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise SourceV6Error("action timestamp must use UTC offset")
    return parsed.astimezone(timezone.utc)


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _required(mapping: Mapping[str, object], key: str, context: str) -> object:
    if key not in mapping or mapping[key] in (None, ""):
        raise SourceV6Error(f"missing {context}.{key}")
    return mapping[key]


def _integral_length(value: object, field: str) -> int:
    parsed = _finite_decimal(value, field)
    if parsed != parsed.to_integral_value() or parsed <= 0:
        raise SourceV6Error(f"{field} must be a positive integer")
    return int(parsed)


def _side(settings: Mapping[str, object], basic: Mapping[str, object]) -> str:
    explicit = basic.get("side")
    if explicit is not None and str(explicit).strip():
        side = str(explicit).strip().upper()
        if side in {"LONG", "SHORT"}:
            return side
        raise SourceV6Error(f"unsupported side: {explicit!r}")
    long_enabled = basic.get("use_long") is True
    short_enabled = basic.get("use_short") is True
    if long_enabled == short_enabled:
        raise SourceV6Error("report side is missing or ambiguous")
    return "LONG" if long_enabled else "SHORT"


def _point(settings: Mapping[str, object]) -> "PointIdentity":
    basic = settings.get("basic")
    if not isinstance(basic, Mapping):
        raise SourceV6Error("settings.basic is missing")
    side = _side(settings, basic)
    strategy_name = str(_required(basic, "strategy", "basic"))
    strategy = settings.get(strategy_name)
    if not isinstance(strategy, Mapping):
        raise SourceV6Error(f"settings.{strategy_name} is missing")
    suffix = side.lower()
    open_ma = strategy.get(f"ma_{suffix}")
    close_ma = strategy.get(f"ma_close_{suffix}")
    if not isinstance(open_ma, Mapping) or not isinstance(close_ma, Mapping):
        raise SourceV6Error("open/close MA settings are missing")
    symbol = str(_required(basic, "symbol", "basic")).strip()
    timeframe = str(_required(basic, "time_frame", "basic")).strip()
    if not symbol or not timeframe:
        raise SourceV6Error("symbol and timeframe must be non-empty")
    multiplier = _finite_decimal(_required(open_ma, "multiplier", "open MA"), "open MA multiplier")
    shift = abs(Decimal("1") - multiplier) * Decimal("10000")
    shift_bp = shift.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if abs(shift - shift_bp) > Decimal("0.000001") or shift_bp < 0:
        raise SourceV6Error("open MA multiplier does not map to an integer shift_bp")
    return PointIdentity(
        symbol=symbol,
        side=side,
        timeframe=timeframe,
        shift_bp=int(shift_bp),
        open_ma_type=str(_required(open_ma, "type", "open MA")),
        open_ma_source=str(_required(open_ma, "source", "open MA")),
        open_ma_length=_integral_length(_required(open_ma, "len", "open MA"), "open MA length"),
        close_ma_type=str(_required(close_ma, "type", "close MA")),
        close_ma_source=str(_required(close_ma, "source", "close MA")),
        close_ma_length=_integral_length(_required(close_ma, "len", "close MA"), "close MA length"),
    )


@dataclass(frozen=True, slots=True)
class PointIdentity:
    symbol: str
    side: str
    timeframe: str
    shift_bp: int
    open_ma_type: str
    open_ma_source: str
    open_ma_length: int
    close_ma_type: str
    close_ma_source: str
    close_ma_length: int

    @property
    def canonical_key(self) -> str:
        return "|".join(
            (
                self.symbol,
                self.side,
                self.timeframe,
                str(self.shift_bp),
                self.open_ma_type,
                self.open_ma_source,
                str(self.open_ma_length),
                self.close_ma_type,
                self.close_ma_source,
                str(self.close_ma_length),
            )
        )

    @classmethod
    def from_canonical_key(cls, key: str) -> "PointIdentity":
        """Rebuild the identity that `canonical_key` encodes.

        The key is a lossless join of every field, so a point can be recovered
        from stored metadata without decoding a fragment payload.
        """
        parts = str(key).split("|")
        if len(parts) != 10:
            raise SourceV6Error(f"invalid canonical point key: {key!r}")
        # int() would accept "+5", " 5 " and "3_0", which re-key differently.
        if any(not _INTEGER_FIELD.fullmatch(parts[index]) for index in (3, 6, 9)):
            raise SourceV6Error(f"invalid canonical point key: {key!r}")
        try:
            return cls(
                symbol=parts[0],
                side=parts[1],
                timeframe=parts[2],
                shift_bp=int(parts[3]),
                open_ma_type=parts[4],
                open_ma_source=parts[5],
                open_ma_length=int(parts[6]),
                close_ma_type=parts[7],
                close_ma_source=parts[8],
                close_ma_length=int(parts[9]),
            )
        except ValueError as error:
            raise SourceV6Error(f"invalid canonical point key: {key!r}") from error


@dataclass(frozen=True, slots=True)
class NormalizedSample:
    timestamp_ms: int
    value: Decimal
    upnl: Decimal


@dataclass(frozen=True, slots=True)
class NormalizedAction:
    action_id: str
    timestamp_ms: int
    symbol: str
    order_id: str
    action: str
    fee: Decimal
    pnl: Decimal
    balance: Decimal | None
    size: Decimal | None
    post_size: Decimal | None
    post_side: str


@dataclass(frozen=True, slots=True)
class NormalizedCycle:
    cycle_id: str
    symbol: str
    order_id: str
    action_ids: tuple[str, ...]
    open_timestamp_ms: int
    close_timestamp_ms: int | None
    realized_pnl: Decimal
    fees: Decimal

    @property
    def closed(self) -> bool:
        return self.close_timestamp_ms is not None


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    event_id: str
    timestamp_ms: int
    action_id: str


@dataclass(frozen=True, slots=True)
class SourceV6Fragment:
    schema_version: int
    fragment_id: str
    source_sha256: str
    source_name: str
    point: PointIdentity
    report_start_ms: int
    report_end_ms: int
    initial_balance: Decimal
    fixed_order_balance: Decimal
    balance_percentage: Decimal
    settings_fingerprint: str
    stitchability: str
    actions: tuple[NormalizedAction, ...]
    cycles: tuple[NormalizedCycle, ...]
    events: tuple[NormalizedEvent, ...]
    wallet_samples: tuple[NormalizedSample, ...]
    equity_samples: tuple[NormalizedSample, ...]
    open_tail_cycle_ids: tuple[str, ...]
    metrics: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class EncodedSourceV6Fragment:
    """Canonical fragment bytes and an interchangeable compressed payload."""

    fragment_id: str
    canonical: bytes
    payload: bytes
    codec: str


# These mirror `dataclasses.asdict` for their exact field order, without its
# recursive copy and per-value typing checks, which dominate canonical
# serialisation of a large report.
def _point_payload(point: PointIdentity) -> dict[str, object]:
    return {
        "symbol": point.symbol,
        "side": point.side,
        "timeframe": point.timeframe,
        "shift_bp": point.shift_bp,
        "open_ma_type": point.open_ma_type,
        "open_ma_source": point.open_ma_source,
        "open_ma_length": point.open_ma_length,
        "close_ma_type": point.close_ma_type,
        "close_ma_source": point.close_ma_source,
        "close_ma_length": point.close_ma_length,
    }


def _action_payload(action: NormalizedAction) -> dict[str, object]:
    return {
        "action_id": action.action_id,
        "timestamp_ms": action.timestamp_ms,
        "symbol": action.symbol,
        "order_id": action.order_id,
        "action": action.action,
        "fee": action.fee,
        "pnl": action.pnl,
        "balance": action.balance,
        "size": action.size,
        "post_size": action.post_size,
        "post_side": action.post_side,
    }


def _sample_payload(sample: NormalizedSample) -> dict[str, object]:
    return {
        "timestamp_ms": sample.timestamp_ms,
        "value": sample.value,
        "upnl": sample.upnl,
    }


def canonical_fragment_payload(fragment: SourceV6Fragment) -> dict[str, object]:
    """Return all analytical facts, excluding path/raw-input identity fields."""
    return {
        "schema_version": fragment.schema_version,
        "point": _point_payload(fragment.point),
        "report_start_ms": fragment.report_start_ms,
        "report_end_ms": fragment.report_end_ms,
        "initial_balance": fragment.initial_balance,
        "fixed_order_balance": fragment.fixed_order_balance,
        "balance_percentage": fragment.balance_percentage,
        "settings_fingerprint": fragment.settings_fingerprint,
        "stitchability": fragment.stitchability,
        "actions": [_action_payload(item) for item in fragment.actions],
        "wallet_samples": [_sample_payload(item) for item in fragment.wallet_samples],
        "equity_samples": [_sample_payload(item) for item in fragment.equity_samples],
        "metrics": dict(fragment.metrics),
    }


def canonical_fragment_bytes(fragment: SourceV6Fragment) -> bytes:
    return _canonical_json(canonical_fragment_payload(fragment)).encode("utf-8")


def canonical_fragment_id(fragment: SourceV6Fragment) -> str:
    return sha256(canonical_fragment_bytes(fragment)).hexdigest()


def encode_fragment(fragment: SourceV6Fragment, *, compression_level: int = 9) -> EncodedSourceV6Fragment:
    if not 0 <= compression_level <= 9:
        raise SourceV6Error("compression level must be between 0 and 9")
    if fragment.schema_version != SOURCE_V6_SCHEMA_VERSION:
        raise SourceV6Error("unsupported Source v6 fragment schema")
    canonical = canonical_fragment_bytes(fragment)
    fragment_id = sha256(canonical).hexdigest()
    if fragment.fragment_id != fragment_id:
        raise SourceV6Error("fragment identity does not match canonical content")
    return EncodedSourceV6Fragment(fragment_id, canonical, zlib.compress(canonical, compression_level), f"json+zlib-v1:{compression_level}")


def _fragment_from_payload(payload: Mapping[str, object]) -> SourceV6Fragment:
    try:
        expected_keys = {
            "schema_version", "point", "report_start_ms", "report_end_ms",
            "initial_balance", "fixed_order_balance", "balance_percentage",
            "settings_fingerprint", "stitchability", "actions", "wallet_samples",
            "equity_samples", "metrics",
        }
        if set(payload) != expected_keys or type(payload["schema_version"]) is not int or payload["schema_version"] != SOURCE_V6_SCHEMA_VERSION:
            raise SourceV6Error("unsupported canonical fragment schema")
        point = PointIdentity(**payload["point"])
        actions = tuple(
            NormalizedAction(**{
                **row,
                "fee": Decimal(str(row["fee"])),
                "pnl": Decimal(str(row["pnl"])),
                "balance": None if row["balance"] is None else Decimal(str(row["balance"])),
                "size": None if row["size"] is None else Decimal(str(row["size"])),
                "post_size": None if row["post_size"] is None else Decimal(str(row["post_size"])),
            }) for row in payload["actions"]
        )
        cycles, events, open_tail_cycle_ids = reconstruct_derived_facts(actions, point)
        wallet = tuple(NormalizedSample(**{
            **row,
            "value": Decimal(str(row["value"])),
            "upnl": Decimal(str(row["upnl"])),
        }) for row in payload["wallet_samples"])
        equity = tuple(NormalizedSample(**{
            **row,
            "value": Decimal(str(row["value"])),
            "upnl": Decimal(str(row["upnl"])),
        }) for row in payload["equity_samples"])
        return SourceV6Fragment(
            schema_version=int(payload["schema_version"]), fragment_id=canonical_fragment_id_from_payload(payload),
            source_sha256="", source_name="", point=point,
            report_start_ms=int(payload["report_start_ms"]), report_end_ms=int(payload["report_end_ms"]),
            initial_balance=Decimal(str(payload["initial_balance"])),
            fixed_order_balance=Decimal(str(payload["fixed_order_balance"])),
            balance_percentage=Decimal(str(payload["balance_percentage"])),
            settings_fingerprint=str(payload["settings_fingerprint"]), stitchability=str(payload["stitchability"]),
            actions=actions, cycles=cycles, events=events, wallet_samples=wallet, equity_samples=equity,
            open_tail_cycle_ids=open_tail_cycle_ids, metrics=dict(payload["metrics"]),
        )
    except SourceV6Error:
        raise
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise SourceV6Error("invalid canonical fragment payload") from error


def canonical_fragment_id_from_payload(payload: Mapping[str, object]) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def decode_fragment(payload: bytes, *, codec: str = "json+zlib-v1:9", expected_fragment_id: str | None = None) -> SourceV6Fragment:
    try:
        if not codec.startswith("json+zlib-v1:"):
            raise SourceV6Error("unsupported compact codec")
        raw = zlib.decompress(bytes(payload))
        # `raw` is the canonical document that `encode_fragment` hashed, so the
        # identity follows from the stored bytes without rebuilding them.
        actual = sha256(raw).hexdigest()
        if expected_fragment_id is not None and actual != expected_fragment_id:
            raise SourceV6Error("canonical fragment identity mismatch")
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, Mapping):
            raise SourceV6Error("canonical fragment must be an object")
        fragment = _fragment_from_payload(document)
        if actual != fragment.fragment_id:
            raise SourceV6Error("canonical fragment identity mismatch")
        return fragment
    except SourceV6Error:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, zlib.error, ValueError) as error:
        raise SourceV6Error("cannot decode compact fragment") from error


def _report_period(metrics: Mapping[str, str]) -> tuple[int, int]:
    """Millisecond half-open period from the report header.

    Delegates the parse so this and `PerformanceInventory` cannot disagree
    about the window: the same header is the only source for both.
    """
    try:
        start, end = report_range(metrics)
    except PerformanceParseError as error:
        raise SourceV6Error(str(error)) from error
    return _timestamp_ms(start), _timestamp_ms(end)


def _effective_report_period(report: ParsedPerformanceReport) -> tuple[int, int]:
    """Return the canonical half-open period, retaining a tester endpoint sample.

    The tester's date-only ``Report range`` is displayed as an inclusive end
    date in some reports.  Such reports can contain a terminal action and
    wallet/equity sample exactly at midnight on the displayed end date.  Keep
    the v6 interval half-open by extending that date-only endpoint by one UTC
    day when a validated report actually contains the endpoint sample.  Reports
    without that sample retain the strict header-derived endpoint.
    """
    start_ms, end_ms = _report_period(report.metrics)
    action_timestamps = tuple(_timestamp_ms(_timestamp(row["Timestamp"])) for row in report.actions)
    series_timestamps = tuple(int(timestamp) for timestamp, _ in report.wallet_series) + tuple(
        int(timestamp) for timestamp, _ in report.equity_series
    )
    observed_timestamps = action_timestamps + series_timestamps
    if any(timestamp > end_ms for timestamp in observed_timestamps):
        raise SourceV6Error("report data exceeds the date-only terminal endpoint")
    if end_ms in observed_timestamps:
        end_ms = _timestamp_ms(datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc) + timedelta(days=1))
    return start_ms, end_ms


def _fingerprint(report: ParsedPerformanceReport, point: PointIdentity, initial: Decimal, fixed: Decimal, percentage: Decimal) -> str:
    basic = report.settings["basic"]
    payload = {
        "symbol": point.symbol,
        "timeframe": point.timeframe,
        "side": point.side,
        "shift_bp": point.shift_bp,
        "open_ma": {"type": point.open_ma_type, "source": point.open_ma_source, "length": point.open_ma_length},
        "close_ma": {"type": point.close_ma_type, "source": point.close_ma_source, "length": point.close_ma_length},
        "initial_balance": initial,
        "fixed_order_balance": fixed,
        "balance_percentage": percentage,
    }
    return f"{EXECUTION_FINGERPRINT_VERSION}:{_sha256_text(_canonical_json(payload))}"


def _action(report: ParsedPerformanceReport, point: PointIdentity, row: Mapping[str, str], occurrence: int = 0) -> NormalizedAction:
    timestamp = _timestamp(row["Timestamp"])
    order_id = str(row.get("Order ID", "")).strip()
    action = str(row.get("Action", "")).strip().lower()
    if not action:
        raise SourceV6Error("action row has no Action")
    fee = _finite_decimal(row.get("Fee", "0"), "action fee")
    pnl = _finite_decimal(row.get("PnL", "0"), "action PnL")
    balance = None if row.get("Balance", "") in (None, "") else _finite_decimal(row["Balance"], "action Balance")
    size = None if row.get("Size", "") in (None, "") else _finite_decimal(row["Size"], "action Size")
    post_size = None if row.get("Post Size", "") in (None, "") else _finite_decimal(row["Post Size"], "action Post Size")
    post_side = str(row.get("Post Side", "")).strip().lower()
    material = {
        "point": point.canonical_key,
        "timestamp_ms": _timestamp_ms(timestamp),
        "symbol": str(row.get("Symbol", point.symbol)).strip(),
        "order_id": order_id,
        "action": action,
        "fee": fee,
        "pnl": pnl,
        "balance": balance,
        "size": size,
        "post_size": post_size,
        "post_side": post_side,
        "occurrence": occurrence,
    }
    action_id = _sha256_text(_canonical_json(material))
    return NormalizedAction(action_id, _timestamp_ms(timestamp), material["symbol"], order_id, action, fee, pnl, balance, size, post_size, post_side)


def reconstruct_derived_facts(
    actions: tuple[NormalizedAction, ...], point: PointIdentity
) -> tuple[tuple[NormalizedCycle, ...], tuple[NormalizedEvent, ...], tuple[str, ...]]:
    """Build the non-factual cycle/event caches deterministically from actions."""
    episodes: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for item in sorted(actions, key=lambda value: (value.symbol, value.timestamp_ms, value.action_id)):
        if current is None or current["symbol"] != item.symbol:
            current = None
        if current is None:
            current = {
                "symbol": item.symbol,
                "order_id": item.order_id or item.action_id,
                "actions": [],
                "open": item.timestamp_ms,
                "close": None,
                "pnl": Decimal("0"),
                "fees": Decimal("0"),
            }
            episodes.append(current)
        current["actions"].append(item.action_id)
        current["pnl"] += item.pnl
        current["fees"] += item.fee
        if (item.post_size is not None and item.post_size == 0) or item.action == "closed":
            current["close"] = item.timestamp_ms
            current = None
    cycles: list[NormalizedCycle] = []
    for values in episodes:
        symbol, order_id = str(values["symbol"]), str(values["order_id"])
        cycle_id = _sha256_text(_canonical_json({
            "point": point.canonical_key,
            "symbol": symbol,
            "order_id": order_id,
            "action_ids": list(values["actions"]),
            "open_timestamp_ms": values["open"],
            "close_timestamp_ms": values["close"],
            "realized_pnl": values["pnl"],
            "fees": values["fees"],
        }))
        cycles.append(NormalizedCycle(
            cycle_id, symbol, order_id, tuple(values["actions"]), int(values["open"]),
            values["close"], values["pnl"], values["fees"],
        ))
    cycles.sort(key=lambda item: (item.open_timestamp_ms, item.cycle_id))
    events = tuple(NormalizedEvent(item.action_id, item.timestamp_ms, item.action_id) for item in actions)
    return tuple(cycles), events, tuple(cycle.cycle_id for cycle in cycles if not cycle.closed)


def _m7_metric(metrics: Mapping[str, str], names: tuple[str, ...]) -> tuple[str, Decimal | None] | None:
    for name in names:
        if name in metrics:
            raw = str(metrics[name]).strip()
            if raw.casefold() == "n/a":
                return name, None
            return name, _finite_decimal(raw, f"declared {name}")
    return None


def _m7_mismatch(
    source_sha256: str, source_name: str, metric: str, declared: object, derived: object,
    fragment_id: str = "",
) -> SourceV6Error:
    details = json.dumps(
        {
            "source_sha256": source_sha256,
            "source_name": source_name,
            "fragment_id": fragment_id,
            "metric": metric,
            "declared": None if declared is None else _decimal_text(Decimal(str(declared))),
            "derived": None if derived is None else _decimal_text(Decimal(str(derived))),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return SourceV6Error(f"M7 metric mismatch: {details}")


def _validate_m7(
    report: ParsedPerformanceReport,
    actions: tuple[NormalizedAction, ...],
    *,
    initial_balance: Decimal,
    source_sha256: str,
    source_name: str,
    fragment_id: str = "",
) -> None:
    """Check independent tester declarations before a fragment is encoded."""
    # The parser rejects action-bearing reports with an empty wallet series;
    # keep this guard defensive so a malformed caller cannot escape as IndexError.
    if not report.wallet_series:
        return

    def check(name: str, actual: Decimal | None, declared: tuple[str, Decimal | None] | None) -> None:
        if declared is None:
            # Sparse seam fragments may omit tester summaries; absence is not a
            # declaration and must not invent a value or quarantine a valid fact.
            return
        declared_name, declared_value = declared
        if declared_value is None:
            if actual is not None:
                raise _m7_mismatch(source_sha256, source_name, declared_name, None, actual, fragment_id)
            return
        if actual is None:
            raise _m7_mismatch(source_sha256, source_name, declared_name, declared_value, None, fragment_id)
        quantum = Decimal("1").scaleb(declared_value.as_tuple().exponent)
        # Tester PF is a ratio and can amplify a tiny rounded loss denominator;
        # keep its accepted absolute drift at one hundredth.
        tolerance = Decimal("0.01") if name == "Profit Factor" else quantum / 2
        if abs(actual - declared_value) <= tolerance:
            return
        rounded = actual.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded != declared_value:
            raise _m7_mismatch(source_sha256, source_name, declared_name, declared_value, rounded, fragment_id)

    wallet = report.wallet_series
    # M4 anchors PnL to the declared initial balance; the first wallet sample
    # may already include an opening fee and therefore is not that anchor.
    total_pnl = wallet[-1][1] - initial_balance
    total_fees = sum((item.fee for item in actions), Decimal("0"))
    realizing = tuple(item for item in actions if item.action in {"decreased", "closed"})
    gross_profit = sum((item.pnl for item in realizing if item.pnl > 0), Decimal("0"))
    gross_loss = sum((item.pnl for item in realizing if item.pnl < 0), Decimal("0"))
    profit_factor = gross_profit / abs(gross_loss) if gross_loss else None
    declared_total_pnl = _m7_metric(report.metrics, ("Total PnL", "TotalPnL"))
    check("Total PnL", total_pnl, declared_total_pnl)
    check("Total fees", total_fees, _m7_metric(report.metrics, ("Total fees", "TotalFees")))
    declared_profit_factor = _m7_metric(
        report.metrics, ("Profit Factor", "Profit Factor (gross profit/gross loss)")
    )
    # The tester emits numeric zero for a positive-only run where the ratio has
    # no finite denominator; preserve that explicit convention.
    if not (profit_factor is None and declared_profit_factor is not None and declared_profit_factor[1] == 0):
        check("Profit Factor", profit_factor, declared_profit_factor)
    recovery = _m7_metric(report.metrics, ("Recovery Factor", "Recovery Factor (Total PnL / Max DD)"))
    declared_max_drawdown = _m7_metric(report.metrics, ("Max Drawdown", "Max DD"))
    raw_recovery_pnl = total_pnl
    equity_peak = report.equity_series[0][1] if report.equity_series else None
    raw_equity_drawdown = Decimal("0")
    if equity_peak is not None:
        for _, value in report.equity_series:
            if value > equity_peak:
                equity_peak = value
            elif equity_peak - value > raw_equity_drawdown:
                raw_equity_drawdown = equity_peak - value
    if (
        recovery is not None
        and recovery[1] is not None
        and declared_max_drawdown is not None
        and declared_max_drawdown[1] is not None
        and raw_equity_drawdown > 0
    ):
        # Tester recovery uses raw M4 PnL and sampled equity drawdown; the
        # displayed Max DD and Total PnL are rounded declarations. If the
        # declared DD is not the sampled DD, M6's declared-candidate rule means
        # this fragment has no independent RF denominator to validate.
        dd_quantum = Decimal("1").scaleb(declared_max_drawdown[1].as_tuple().exponent)
        if raw_equity_drawdown.quantize(dd_quantum, rounding=ROUND_HALF_UP) == declared_max_drawdown[1]:
            quantum = Decimal("1").scaleb(recovery[1].as_tuple().exponent)
            rounded = (raw_recovery_pnl / raw_equity_drawdown).quantize(quantum, rounding=ROUND_HALF_UP)
            if abs(rounded - recovery[1]) > quantum:
                raise _m7_mismatch(
                    source_sha256, source_name, recovery[0], recovery[1], rounded, fragment_id
                )


def normalize_source_v6(source: bytes, *, source_name: str = "") -> SourceV6Fragment:
    """Normalize one report; see `normalize_and_encode_source_v6` for imports."""
    return _normalize_with_canonical(source, source_name=source_name)[1]


def normalize_and_encode_source_v6(
    source: bytes, *, source_name: str = "", compression_level: int = 9
) -> tuple[SourceV6Fragment, EncodedSourceV6Fragment]:
    """Normalize and encode in one pass, serialising the fragment only once.

    Deriving the identity already produces the canonical document, so encoding
    reuses those exact bytes instead of rebuilding them.
    """
    if not 0 <= compression_level <= 9:
        raise SourceV6Error("compression level must be between 0 and 9")
    canonical, fragment = _normalize_with_canonical(source, source_name=source_name)
    encoded = EncodedSourceV6Fragment(
        fragment.fragment_id,
        canonical,
        zlib.compress(canonical, compression_level),
        f"json+zlib-v1:{compression_level}",
    )
    return fragment, encoded


def _normalize_with_canonical(source: bytes, *, source_name: str = "") -> tuple[bytes, SourceV6Fragment]:
    if not isinstance(source, bytes):
        raise SourceV6Error("source must be bytes")
    source_hash = sha256(source).hexdigest()
    try:
        # ADR-0016: the v6 importer is the only caller that admits a run with
        # no trades. The v1 performance store keeps rejecting them.
        report = parse_performance_report(source, allow_zero_activity=True)
        point = _point(report.settings)
        report_start_ms, report_end_ms = _effective_report_period(report)
        action_timestamps = tuple(_timestamp_ms(_timestamp(row["Timestamp"])) for row in report.actions)
        if any(timestamp < report_start_ms or timestamp >= report_end_ms for timestamp in action_timestamps):
            raise SourceV6Error("action timestamp is outside report interval")
        for series_name, series in (("wallet", report.wallet_series), ("equity", report.equity_series)):
            timestamps = tuple(int(timestamp) for timestamp, _ in series)
            if len(set(timestamps)) != len(timestamps) or timestamps != tuple(sorted(timestamps)):
                raise SourceV6Error(f"{series_name} sample timestamps are not strictly increasing")
            if any(timestamp < report_start_ms or timestamp >= report_end_ms for timestamp in timestamps):
                raise SourceV6Error(f"{series_name} sample timestamp is outside report interval")
        initial = _finite_decimal(_required(report.metrics, "Initial balance", "metrics"), "Initial balance")
        basic = report.settings["basic"]
        fixed = _finite_decimal(_required(basic, "my_fix_balance", "basic"), "fixed order balance")
        percentage_key = f"balance_percentage_{point.side.lower()}"
        percentage = _finite_decimal(_required(basic, percentage_key, "basic"), percentage_key)
        use_upnl = report.settings.get("exchange", {}).get("use_upnl")
        use_fix = basic.get("use_fix")
        if not isinstance(use_upnl, bool) or not isinstance(use_fix, bool):
            raise SourceV6Error("fixed-lot admission settings are missing or malformed")
        stitchability = "STITCHABLE_FIXED_LOT" if not use_upnl and use_fix else "NON_STITCHABLE_POSITION_SIZING"
        ordered_rows = sorted(report.actions, key=lambda row: _canonical_json(dict(sorted(row.items()))))
        occurrences: defaultdict[str, int] = defaultdict(int)
        parsed_actions = []
        for row in ordered_rows:
            key = _canonical_json(dict(sorted(row.items())))
            occurrence = occurrences[key]
            occurrences[key] += 1
            parsed_actions.append(_action(report, point, row, occurrence))
        actions = tuple(sorted(parsed_actions, key=lambda item: (item.timestamp_ms, item.action_id)))
        # A cycle is one position, not one order. `Order ID` identifies the
        # fills of a single order, and a position routinely opens under one
        # order and closes under another — in the reference report the opening
        # and closing order ids never once coincide. Grouping by order id
        # therefore split every position in two: the half holding the `closed`
        # action looked complete, and the half holding `opened` looked like a
        # position that never closed. The earliest such phantom then became the
        # open-tail cutoff and hid every sample of the report.
        #
        # `Post Size` states the truth directly: the position is open while it
        # is above zero and closed the moment it returns to zero. An episode may
        # begin without an `opened` action, because the position can have been
        # opened before the report started.
        cycles, events, open_tail_cycle_ids = reconstruct_derived_facts(actions, point)
        wallet = tuple(NormalizedSample(timestamp, value, Decimal("0")) for timestamp, value in report.wallet_series)
        wallet_timestamps = tuple(timestamp for timestamp, _ in report.wallet_series)
        equity_timestamps = tuple(timestamp for timestamp, _ in report.equity_series)
        if wallet_timestamps != equity_timestamps:
            raise SourceV6Error("wallet and equity sample timestamps are not synchronized")
        equity = tuple(NormalizedSample(timestamp, value, value - report.wallet_series[index][1]) for index, (timestamp, value) in enumerate(report.equity_series))
        fragment = SourceV6Fragment(
                SOURCE_V6_SCHEMA_VERSION,
                "",
            source_hash,
            source_name,
            point,
            report_start_ms,
            report_end_ms,
            initial,
            fixed,
            percentage,
            _fingerprint(report, point, initial, fixed, percentage),
            stitchability,
            actions,
            cycles,
            events,
            wallet,
            equity,
            open_tail_cycle_ids,
            dict(report.metrics),
        )
        canonical = canonical_fragment_bytes(fragment)
        _validate_m7(
            report,
            actions,
            initial_balance=initial,
            source_sha256=source_hash,
            source_name=source_name,
            fragment_id=sha256(canonical).hexdigest(),
        )
        return canonical, SourceV6Fragment(
            fragment.schema_version,
            sha256(canonical).hexdigest(),
            fragment.source_sha256,
            fragment.source_name,
            fragment.point,
            fragment.report_start_ms,
            fragment.report_end_ms,
            fragment.initial_balance,
            fragment.fixed_order_balance,
            fragment.balance_percentage,
            fragment.settings_fingerprint,
            fragment.stitchability,
            fragment.actions,
            fragment.cycles,
            fragment.events,
            fragment.wallet_samples,
            fragment.equity_samples,
            fragment.open_tail_cycle_ids,
            fragment.metrics,
        )
    except SourceV6Error:
        raise
    except Exception as error:
        raise SourceV6Error(f"cannot normalize Source v6 report: {error}") from error
