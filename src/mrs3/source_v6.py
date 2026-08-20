"""Source v6 HTML normalisation primitives.

This module intentionally stops at an immutable, normalised fragment.  DuckDB
publication, overlap ownership and surface materialisation consume these facts
in later stages and must not re-parse HTML.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
import re
import zlib
from collections import defaultdict
from typing import Mapping

from .performance import ParsedPerformanceReport, parse_performance_report


SOURCE_V6_SCHEMA_VERSION = 1
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


def _point_payload(point: PointIdentity) -> dict[str, object]:
    return asdict(point)


def _action_payload(action: NormalizedAction) -> dict[str, object]:
    return asdict(action)


def _cycle_payload(cycle: NormalizedCycle) -> dict[str, object]:
    result = asdict(cycle)
    result["action_ids"] = list(cycle.action_ids)
    return result


def _event_payload(event: NormalizedEvent) -> dict[str, object]:
    return asdict(event)


def _sample_payload(sample: NormalizedSample) -> dict[str, object]:
    return asdict(sample)


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
        "cycles": [_cycle_payload(item) for item in fragment.cycles],
        "events": [_event_payload(item) for item in fragment.events],
        "wallet_samples": [_sample_payload(item) for item in fragment.wallet_samples],
        "equity_samples": [_sample_payload(item) for item in fragment.equity_samples],
        "open_tail_cycle_ids": list(fragment.open_tail_cycle_ids),
        "metrics": dict(fragment.metrics),
    }


def canonical_fragment_bytes(fragment: SourceV6Fragment) -> bytes:
    return _canonical_json(canonical_fragment_payload(fragment)).encode("utf-8")


def canonical_fragment_id(fragment: SourceV6Fragment) -> str:
    return sha256(canonical_fragment_bytes(fragment)).hexdigest()


def encode_fragment(fragment: SourceV6Fragment, *, compression_level: int = 9) -> EncodedSourceV6Fragment:
    if not 0 <= compression_level <= 9:
        raise SourceV6Error("compression level must be between 0 and 9")
    canonical = canonical_fragment_bytes(fragment)
    fragment_id = sha256(canonical).hexdigest()
    if fragment.fragment_id != fragment_id:
        raise SourceV6Error("fragment identity does not match canonical content")
    return EncodedSourceV6Fragment(fragment_id, canonical, zlib.compress(canonical, compression_level), f"json+zlib-v1:{compression_level}")


def _fragment_from_payload(payload: Mapping[str, object]) -> SourceV6Fragment:
    try:
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
        cycles = tuple(
            NormalizedCycle(**{
                **row,
                "action_ids": tuple(row["action_ids"]),
                "realized_pnl": Decimal(str(row["realized_pnl"])),
                "fees": Decimal(str(row["fees"])),
            }) for row in payload["cycles"]
        )
        events = tuple(NormalizedEvent(**row) for row in payload["events"])
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
            open_tail_cycle_ids=tuple(payload["open_tail_cycle_ids"]), metrics=dict(payload["metrics"]),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise SourceV6Error("invalid canonical fragment payload") from error


def canonical_fragment_id_from_payload(payload: Mapping[str, object]) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def decode_fragment(payload: bytes, *, codec: str = "json+zlib-v1:9", expected_fragment_id: str | None = None) -> SourceV6Fragment:
    try:
        if not codec.startswith("json+zlib-v1:"):
            raise SourceV6Error("unsupported compact codec")
        raw = zlib.decompress(bytes(payload))
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, Mapping):
            raise SourceV6Error("canonical fragment must be an object")
        fragment = _fragment_from_payload(document)
        actual = canonical_fragment_id(fragment)
        if actual != fragment.fragment_id or (expected_fragment_id is not None and actual != expected_fragment_id):
            raise SourceV6Error("canonical fragment identity mismatch")
        return fragment
    except SourceV6Error:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, zlib.error, ValueError) as error:
        raise SourceV6Error("cannot decode compact fragment") from error


def _report_period(metrics: Mapping[str, str]) -> tuple[int, int]:
    raw = metrics.get("Report range")
    if not raw or " - " not in raw:
        raise SourceV6Error("report header is missing 'Report range'")
    start_text, end_text = (part.strip() for part in raw.split(" - ", 1))
    try:
        start = datetime.combine(date.fromisoformat(start_text), time.min, tzinfo=timezone.utc)
        end = datetime.combine(date.fromisoformat(end_text), time.min, tzinfo=timezone.utc)
    except ValueError as error:
        raise SourceV6Error(f"invalid report range: {raw!r}") from error
    if end <= start:
        raise SourceV6Error("report range end must be after start")
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


def normalize_source_v6(source: bytes, *, source_name: str = "") -> SourceV6Fragment:
    if not isinstance(source, bytes):
        raise SourceV6Error("source must be bytes")
    source_hash = sha256(source).hexdigest()
    try:
        report = parse_performance_report(source)
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
        cycles_by_key: dict[tuple[str, str], dict[str, object]] = {}
        for item in actions:
            key = (item.symbol, item.order_id or item.action_id)
            cycle = cycles_by_key.setdefault(key, {"actions": [], "open": item.timestamp_ms, "close": None, "pnl": Decimal("0"), "fees": Decimal("0")})
            cycle["actions"].append(item.action_id)
            cycle["open"] = min(int(cycle["open"]), item.timestamp_ms)
            cycle["pnl"] += item.pnl
            cycle["fees"] += item.fee
            if item.action == "closed":
                cycle["close"] = item.timestamp_ms
        cycles: list[NormalizedCycle] = []
        for (symbol, order_id), values in cycles_by_key.items():
            cycle_id = _sha256_text(_canonical_json({"point": point.canonical_key, "symbol": symbol, "order_id": order_id, "action_ids": list(values["actions"]), "open_timestamp_ms": values["open"], "close_timestamp_ms": values["close"], "realized_pnl": values["pnl"], "fees": values["fees"]}))
            cycles.append(NormalizedCycle(cycle_id, symbol, order_id, tuple(values["actions"]), int(values["open"]), values["close"], values["pnl"], values["fees"]))
        cycles.sort(key=lambda item: (item.open_timestamp_ms, item.cycle_id))
        events = tuple(NormalizedEvent(item.action_id, item.timestamp_ms, item.action_id) for item in actions)
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
            tuple(cycles),
            events,
            wallet,
            equity,
            tuple(cycle.cycle_id for cycle in cycles if not cycle.closed),
            dict(report.metrics),
        )
        return SourceV6Fragment(
            fragment.schema_version,
            canonical_fragment_id(fragment),
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
