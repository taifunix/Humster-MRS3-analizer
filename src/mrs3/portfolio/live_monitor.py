"""Pure fixture reconcile, attribution, read models, and watchdog logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import live_reconcile
from .live_reconcile import ALL_CHANNELS
from .live_store import DeploymentManifest, LiveStore, LiveStoreError, WriteOutcome, _reject_secrets, canonical_digest, canonical_json


HEALTHY = "HEALTHY"
UNKNOWN = "UNKNOWN"
INCONSISTENT = "INCONSISTENT"
AVAILABLE = "AVAILABLE"
UNATTRIBUTED = "UNATTRIBUTED"
ATTRIBUTED = "ATTRIBUTED"
LIMITER_OVERFLOW = "LIMITER_OVERFLOW"
LIMITER_RECOVERED = "LIMITER_RECOVERED"
LIMITER_BREACH = "LIMITER_BREACH"
CURRENT = "CURRENT"
STALE = "STALE"
IN_POSITION = "IN_POSITION"
PENDING_ENTRY = "PENDING_ENTRY"
NO_POSITION = "NO_POSITION"


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(timezone.utc)


def _clock_value(clock: Callable[[], Any] | datetime | None, fallback: Any = None) -> datetime:
    injected = clock is not None
    value = clock() if callable(clock) else clock
    if value is None and injected:
        raise ValueError("clock timestamp must be valid timezone-aware UTC")
    if value is None:
        value = fallback
    if value is None and fallback is not None:
        raise ValueError("clock timestamp must be valid timezone-aware UTC")
    if value is None:
        return datetime.now(timezone.utc)
    result = _utc(value)
    if result is None:
        raise ValueError("clock timestamp must be valid timezone-aware UTC")
    return result


def _number(value: Any) -> Decimal | None:
    if isinstance(value, (bool, float)) or value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _decimal_string(value: Any) -> str | None:
    number = _number(value)
    return format(number, "f") if number is not None else None


def _side(value: Any) -> str:
    return str(value or "").strip().upper()


@dataclass(frozen=True)
class Attribution:
    status: str
    strategy_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    reasons: tuple[str, ...] = ()
    deployment_id: str | None = None
    snapshot_id: str | None = None
    checkpoints: Mapping[str, int] | None = None
    executions_applied: int = 0
    executions_duplicate: int = 0
    realised_pnl: str | None = None
    unrealised_pnl: str | None = None
    forced_close_evidence: str = "UNKNOWN"
    availability: str = UNKNOWN
    provenance: Mapping[str, Any] | None = None
    realised_pnl_provenance: str = "UNKNOWN"
    executions_dropped: int = 0
    executions_gapped: int = 0

    @property
    def realized_pnl(self) -> str | None:
        return self.realised_pnl

    @property
    def unrealized_pnl(self) -> str | None:
        return self.unrealised_pnl

    @property
    def pnl_provenance(self) -> str:
        return self.realised_pnl_provenance

    @property
    def dropped_events(self) -> int:
        return self.executions_dropped

    @property
    def gapped_events(self) -> int:
        return self.executions_gapped


@dataclass(frozen=True)
class WatchdogSettings:
    limit: int | None = None
    grace_seconds: int | None = None
    settings_version: str = "unknown"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "WatchdogSettings":
        payload = dict(value or {})
        limit = payload.get("limit", payload.get("L"))
        grace = payload.get("grace_seconds", payload.get("grace"))
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("watchdog limit must be a non-negative integer")
        if grace is not None and (isinstance(grace, bool) or not isinstance(grace, int) or grace < 0):
            raise ValueError("watchdog grace must be a non-negative integer")
        version = payload.get("settings_version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("watchdog settings_version is required")
        return cls(limit, grace, str(version))


@dataclass(frozen=True)
class WatchdogTransition:
    state: str
    finding: str | None
    counted_positions: int
    exempt_positions: tuple[str, ...]
    started_at: datetime | None
    ended_at: datetime | None
    settings_version: str
    availability: str = AVAILABLE


def _manifest_entries(manifest: DeploymentManifest | Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    payload = manifest.payload if isinstance(manifest, DeploymentManifest) else dict(manifest)
    entries: list[Mapping[str, Any]] = []
    for key in ("strategies", "members", "member_composition", "strategy_map"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            entries.extend({"strategy_id": key_name, **(item if isinstance(item, Mapping) else {"symbol": item})} for key_name, item in value.items())
        elif isinstance(value, (list, tuple)):
            entries.extend(item for item in value if isinstance(item, Mapping))
    return tuple(entries)


def _manifest_maps(manifest: DeploymentManifest | Mapping[str, Any]) -> tuple[dict[tuple[str, str], str], set[tuple[str, str]]]:
    entries = _manifest_entries(manifest)
    by_pair: dict[tuple[str, str], list[str]] = {}
    for entry in entries:
        strategy = entry.get("strategy_id", entry.get("id", entry.get("identity")))
        symbol = entry.get("symbol")
        side = entry.get("side", entry.get("direction"))
        if not isinstance(strategy, str) or not strategy or not isinstance(symbol, str) or not symbol:
            continue
        strategy = strategy.strip()
        pair = (symbol.strip().upper(), _side(side))
        by_pair.setdefault(pair, []).append(strategy)
    return {pair: ids[0] for pair, ids in by_pair.items() if len(set(ids)) == 1}, set(by_pair)


def _manifest_order_links(manifest: DeploymentManifest | Mapping[str, Any]) -> tuple[dict[str, str], set[str], dict[str, tuple[tuple[str, str], ...]]]:
    payload = manifest.payload if isinstance(manifest, DeploymentManifest) else dict(manifest)
    entries = _manifest_entries(manifest)
    candidates: dict[str, set[str]] = {}
    strategy_pairs: dict[str, list[tuple[str, str]]] = {}
    for entry in entries:
        strategy = entry.get("strategy_id", entry.get("id", entry.get("identity")))
        if not isinstance(strategy, str) or not strategy.strip():
            continue
        strategy = strategy.strip()
        symbol = entry.get("symbol")
        side = entry.get("side", entry.get("direction"))
        if isinstance(symbol, str) and symbol.strip():
            strategy_pairs.setdefault(strategy, []).append((symbol.strip().upper(), _side(side)))
        links = entry.get("orderLinkId", entry.get("order_link_id", entry.get("order_link_ids", entry.get("order_links", entry.get("local_order_id", entry.get("client_order_id", ()))))))
        if isinstance(links, str):
            links = (links,)
        if isinstance(links, Mapping):
            links = links.keys()
        if isinstance(links, (list, tuple, set)):
            for link in links:
                if isinstance(link, str) and link.strip():
                    candidates.setdefault(link.strip(), set()).add(strategy)
    for key in ("orderLinkId", "order_link_id", "order_link_mapping", "order_links", "local_order_mapping", "order_mapping"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            for link, strategy in value.items():
                if isinstance(link, str) and link.strip() and isinstance(strategy, str) and strategy.strip():
                    candidates.setdefault(link.strip(), set()).add(strategy.strip())
    unique = {link: next(iter(strategies)) for link, strategies in candidates.items() if len(strategies) == 1}
    return unique, set(candidates), {strategy: tuple(pairs) for strategy, pairs in strategy_pairs.items()}


def attribute_execution(execution: Mapping[str, Any], manifest: DeploymentManifest | Mapping[str, Any]) -> Attribution:
    """Attribute only on an explicit strategy ID or an unambiguous manifest pair."""

    by_pair, pairs = _manifest_maps(manifest)
    by_link, links, strategy_pairs = _manifest_order_links(manifest)
    link = execution.get("orderLinkId", execution.get("order_link_id", execution.get("order_link", execution.get("local_order_id", execution.get("client_order_id")))))
    link = link.strip() if isinstance(link, str) and link.strip() else None
    pair = (str(execution.get("symbol", "")).strip().upper(), _side(execution.get("side", execution.get("direction"))))
    explicit = execution.get("strategy_id", execution.get("strategyId"))
    if explicit is not None:
        if not _is_id(explicit):
            return Attribution(UNATTRIBUTED, None, "STRATEGY_ID_INVALID")
        explicit = explicit.strip()
        known_ids = {str(item.get("strategy_id", item.get("id", item.get("identity")))) for item in _manifest_entries(manifest) if item.get("strategy_id", item.get("id", item.get("identity"))) is not None}
        if explicit in known_ids:
            if link is not None and (link not in by_link or by_link[link] != explicit):
                return Attribution(UNKNOWN, None, "MAPPING_DRIFT")
            if pair[0] and pair[1] and strategy_pairs.get(explicit) and pair not in strategy_pairs[explicit]:
                return Attribution(UNKNOWN, None, "MAPPING_DRIFT")
            return Attribution(ATTRIBUTED, explicit)
        if not known_ids:
            return Attribution(UNATTRIBUTED, None, "STRATEGY_ID_UNVERIFIED")
        return Attribution(UNKNOWN, None, "STRATEGY_ID_CHANGED")
    if link is not None:
        if link in by_link:
            strategy = by_link[link]
            if pair[0] and pair[1] and strategy_pairs.get(strategy) and pair not in strategy_pairs[strategy]:
                return Attribution(UNKNOWN, None, "MAPPING_DRIFT")
            return Attribution(ATTRIBUTED, strategy)
        return Attribution(UNKNOWN, None, "ORDER_LINK_UNVERIFIED")
    if pair in by_pair:
        return Attribution(ATTRIBUTED, by_pair[pair])
    if pair in pairs:
        return Attribution(UNATTRIBUTED, None, "AMBIGUOUS_SYMBOL_MAPPING")
    return Attribution(UNATTRIBUTED, None, "STRATEGY_ID_MISSING")


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_record_id(value: Any) -> bool:
    return _is_id(value) or (isinstance(value, int) and not isinstance(value, bool))


def _is_execution(item: Mapping[str, Any]) -> bool:
    kind = item.get("kind", item.get("event_type", ""))
    return isinstance(kind, str) and kind.casefold() in {"execution", "fill", "trade"} or "execution_id" in item


def _event_signature(item: Mapping[str, Any]) -> str:
    try:
        return canonical_json(item)
    except (TypeError, ValueError):
        return repr(tuple(sorted((str(key), type(value).__name__, repr(value)) for key, value in item.items())))


def _execution_signature(item: Mapping[str, Any]) -> str:
    aliases = {"pnl": "realized_pnl", "realised_pnl": "realized_pnl", "exec_qty": "qty", "quantity": "qty", "execution_price": "price", "trade_price": "price", "commission": "fee"}
    envelope = {"deployment_id", "account_id", "execution_id", "id", "event_id", "channel", "sequence", "seq", "kind", "event_type", "source_kind", "observed_at", "timestamp_received"}
    payload = {aliases.get(key, key): value for key, value in item.items() if key not in envelope}
    return _event_signature(payload)


def _count_duplicate_execution_events(items: Iterable[Any], *, default_account: str = "") -> int:
    seen: dict[tuple[str, str], str] = {}
    duplicates = 0
    for item in items:
        if not isinstance(item, Mapping) or not _is_execution(item):
            continue
        execution_id = item.get("execution_id", item.get("id"))
        if not _is_record_id(execution_id):
            continue
        account_id = item.get("account_id") if _is_id(item.get("account_id")) else default_account
        key = (account_id, execution_id.strip() if isinstance(execution_id, str) else str(execution_id))
        signature = _execution_signature(item)
        prior = seen.get(key)
        if prior is None:
            seen[key] = signature
        elif prior == signature:
            duplicates += 1
    return duplicates


def _execution_dropped_count(items: Iterable[Any], watermarks: Mapping[str, Any]) -> int:
    dropped = 0
    for item in items:
        if not isinstance(item, Mapping) or not _is_execution(item):
            continue
        sequence = item.get("sequence", item.get("seq"))
        channel = item.get("channel", item.get("kind", ""))
        if not isinstance(channel, str) or not isinstance(sequence, int) or isinstance(sequence, bool):
            continue
        watermark = watermarks.get(channel.strip())
        if isinstance(watermark, int) and not isinstance(watermark, bool) and watermark >= 0 and sequence <= watermark:
            dropped += 1
    return dropped


def _forced_close(events: Iterable[Mapping[str, Any]], executions: Iterable[Mapping[str, Any]]) -> str:
    for item in (*tuple(events), *tuple(executions)):
        if not isinstance(item, Mapping):
            continue
        explicit = item.get("forced_close")
        if explicit is True or (isinstance(explicit, str) and explicit.casefold() in {"true", "yes", "1"}) or str(item.get("close_reason", item.get("reason", ""))).casefold() in {"forced_close", "liquidation", "adl"}:
            return "OBSERVED"
    return "UNKNOWN"


def _legacy_pages(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...], bool]:
    """Adapt the small legacy fixture shape to the canonical reducer input."""

    source = dict(snapshot)
    reasons: list[str] = []
    records: dict[str, tuple[Mapping[str, Any], ...]] = {}
    wallet = snapshot.get("wallet")
    if isinstance(wallet, Mapping):
        wallet_record = dict(wallet)
        wallet_record.setdefault("record_id", snapshot.get("snapshot_id", "wallet"))
        records["wallet"] = (wallet_record,)
    else:
        records["wallet"] = ()
        reasons.append("WALLET_INCOMPLETE")
    for channel in ("positions", "executions", "orders", "cashflows"):
        value = snapshot.get(channel, ())
        if channel == "cashflows" and channel not in snapshot:
            records[channel] = ()
            continue
        if isinstance(value, (list, tuple)) and all(isinstance(item, Mapping) for item in value):
            records[channel] = tuple(value)
        else:
            records[channel] = ()
            reasons.append(f"{channel.upper()}_INCOMPLETE")
    start = snapshot.get("snapshot_start_observed_at", snapshot.get("observed_at"))
    end = snapshot.get("snapshot_end_observed_at", snapshot.get("observed_at"))
    source.update({
        "snapshot_start_observed_at": start,
        "snapshot_end_observed_at": end,
        "duration_seconds": snapshot.get("duration_seconds", "0"),
        "open_orders_duration_seconds": snapshot.get("open_orders_duration_seconds", "0"),
    })
    source["wallet"] = records["wallet"][0] if records["wallet"] else {}
    watermarks = snapshot.get("watermarks", snapshot.get("stream_watermarks"))
    if isinstance(watermarks, Mapping):
        watermarks = dict(watermarks)
        if "wallet" in watermarks:
            for channel in live_reconcile.REQUIRED_CHANNELS:
                watermarks.setdefault(channel, 0)
        source["watermarks"] = watermarks
    else:
        source["watermarks"] = watermarks
    source["declared_count"] = {channel: len(items) for channel, items in records.items()}
    source["canonical_digest"] = {channel: live_reconcile._records_digest(items) for channel, items in records.items()}
    source["snapshot_declared_count"] = sum(len(items) for items in records.values())
    canonical_records = {channel: tuple(sorted(items, key=live_reconcile._source_key)) for channel, items in records.items()}
    source["snapshot_canonical_digest"] = live_reconcile._snapshot_digest(canonical_records)
    for channel, items in records.items():
        if channel == "cashflows" and not items:
            continue
        source[channel] = [{"channel": channel, "items": list(items), "terminal": True, "terminal_cursor": f"{channel}-end", "declared_count": len(items), "canonical_digest": live_reconcile._records_digest(items)}]
    return source, tuple(dict.fromkeys(reasons)), True


def _canonical_decimal(value: Any) -> str | None:
    number = _number(value)
    if number is None:
        return None
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        number = +number
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fact_time(item: Mapping[str, Any]) -> datetime | None:
    return _utc(item.get("effective_at_utc", item.get("timestamp_utc", item.get("observed_at_utc", item.get("observed_at", item.get("timestamp", item.get("time")))))))


def _stored_timestamp_value(item: Mapping[str, Any]) -> Any:
    for key in ("observed_at_utc", "observed_at", "effective_at_utc", "effective_at"):
        value = item.get(key)
        if value is not None:
            return value
    return None


def _stored_timestamp(item: Mapping[str, Any]) -> datetime | None:
    return _utc(_stored_timestamp_value(item))


def _equity_observation_value(item: Mapping[str, Any]) -> Any:
    for key in ("snapshot_end_observed_at", "observed_at", "observed_at_utc", "effective_at", "effective_at_utc"):
        value = item.get(key)
        if value is not None:
            return value
    return None


def _equity_observation(item: Mapping[str, Any]) -> datetime | None:
    return _utc(_equity_observation_value(item))


def _currency_known(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fact_order(item: Mapping[str, Any], fallback_id: str) -> tuple[datetime, datetime, str, str, int, str] | None:
    timestamp = _fact_time(item)
    if timestamp is None:
        return None
    effective = item.get("effective_at_utc", item.get("timestamp_utc", timestamp))
    observed = item.get("observed_at_utc", item.get("observed_at", effective))
    source_kind = item.get("source_kind", "REST")
    source_id = item.get("source_id", item.get("cashflow_id", item.get("event_id", fallback_id)))
    sequence = item.get("source_sequence", item.get("sequence", 0))
    effective_utc = _utc(effective)
    observed_utc = _utc(observed)
    def _order_identity(value: Any) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return str(value)
    source_kind_text = _order_identity(source_kind)
    source_id_text = _order_identity(source_id)
    if effective_utc is None or observed_utc is None or source_kind_text is None or source_id_text is None:
        return None
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None
    return (effective_utc, observed_utc, source_kind_text, source_id_text, sequence, canonical_digest(item))


def _fact_order_key(order: tuple[datetime, datetime, str, str, int, str] | None) -> tuple[datetime, datetime, str, str, int, str]:
    if order is not None:
        return order
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    return (minimum, minimum, "", "", 0, "")


def _cashflow_signed(item: Mapping[str, Any]) -> tuple[Decimal | None, str]:
    amount = _number(item.get("amount", item.get("value", item.get("quantity"))))
    if amount is None:
        return None, "AMOUNT_INVALID"
    label = str(item.get("classification", item.get("direction", item.get("type", "")))).strip().casefold().replace("-", "_").replace(" ", "_")
    if label in {"deposit", "inbound", "in", "credit", "account_inflow", "funding_in"}:
        if amount < 0:
            return None, "AMOUNT_DIRECTION_AMBIGUOUS"
        return abs(amount), "KNOWN"
    if label in {"withdrawal", "outbound", "out", "debit", "account_outflow", "funding_out"}:
        if amount > 0:
            return None, "AMOUNT_DIRECTION_AMBIGUOUS"
        return -abs(amount), "KNOWN"
    if label in {"internal_transfer", "transfer", "internal", "internal_in", "internal_out"}:
        if (item.get("internal") is True or str(item.get("scope", "")).strip().upper() == "INTERNAL") and item.get("boundary") is False:
            return Decimal("0"), "KNOWN"
        return None, "CLASSIFICATION_UNKNOWN"
    return None, "CLASSIFICATION_UNKNOWN"


_ORDER_ID_KEYS = ("order_id", "orderId", "id", "client_order_id", "order_link_id")
_ORDER_QTY_KEYS = ("qty", "quantity", "size", "leaves_qty", "remaining_qty", "order_qty")
_ORDER_PRICE_KEYS = ("price", "order_price", "limit_price")
_ORDER_ROLE_KEYS = ("role", "order_role", "strategy_role")
_ORDER_ACTIVE_STATUSES = {"", "NEW", "OPEN", "ACTIVE", "WORKING", "CREATED", "UNTRIGGERED", "PARTIALLY_FILLED"}
_ORDER_CLOSED_STATUSES = {"CANCELED", "CANCELLED", "FILLED", "REJECTED", "EXPIRED", "DEACTIVATED", "DONE"}


def _order_channel(item: Mapping[str, Any]) -> str:
    value = item.get("channel", item.get("kind", item.get("event_type", "")))
    return value.strip().casefold() if isinstance(value, str) else ""


def _order_pair(item: Mapping[str, Any]) -> tuple[str, str] | None:
    symbol = item.get("symbol", item.get("ticker"))
    side = item.get("side", item.get("direction"))
    if not isinstance(symbol, str) or not symbol.strip() or not isinstance(side, str) or not side.strip():
        return None
    return symbol.strip().upper(), _side(side)


def _order_id(item: Mapping[str, Any]) -> str | None:
    for key in _ORDER_ID_KEYS:
        value = item.get(key)
        if _is_record_id(value):
            return str(value).strip()
    return None


def _order_revision(item: Mapping[str, Any]) -> int | None:
    value = next((item[key] for key in ("revision", "order_revision", "version") if key in item), None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _order_timestamp(item: Mapping[str, Any]) -> datetime | None:
    for key in ("confirmed_at", "confirmation_at", "updated_at", "timestamp_utc", "observed_at_utc", "observed_at", "timestamp", "time"):
        value = _utc(item.get(key))
        if value is not None:
            return value
    return None


def _order_status(item: Mapping[str, Any]) -> str:
    value = item.get("status", item.get("order_status", ""))
    return str(value).strip().upper() if value is not None else ""


class LiveMonitor:
    """Fixture-only monitor.  It has no methods capable of trading or mutation."""

    def __init__(
        self,
        manifest: DeploymentManifest | Mapping[str, Any],
        store: LiveStore | None = None,
        *,
        clock: Callable[[], Any] | datetime | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        self.manifest = manifest if isinstance(manifest, DeploymentManifest) else DeploymentManifest(manifest)
        self.store = store
        self.clock = clock
        merged_settings = dict(self.manifest.payload.get("watchdog_settings", self.manifest.payload.get("watchdog", {})))
        for key in ("snapshot_max_age_seconds", "freshness_seconds", "freshness_limit_seconds"):
            if key in self.manifest.payload:
                merged_settings.setdefault(key, self.manifest.payload[key])
        merged_settings.update(settings or {})
        self._settings = merged_settings
        self.watchdog_settings = WatchdogSettings.from_mapping(merged_settings)
        self._latest: Mapping[str, Any] | None = None
        self._latest_events: tuple[Mapping[str, Any], ...] = ()
        self._execution_pnl: dict[tuple[str, str, str], Decimal] = {}
        self._equity_points: list[dict[str, Any]] = []
        self._cashflow_points: list[dict[str, Any]] = []
        self._margin_facts: list[Mapping[str, Any]] = []
        self._metric_boundary_unknown = False
        self._integrity_invalid = False
        self._store_reconstruction_blocked = False
        self._overflow_started: datetime | None = None
        self._last_result: ReconcileResult | None = None
        self._last_watchdog_state: str | None = None
        self._order_facts: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._position_facts: dict[tuple[str, str], Mapping[str, Any]] = {}
        self._order_observations: dict[tuple[str, str], dict[str, Any]] = {}
        self._order_faults: dict[tuple[str, str], str] = {}
        if self.store is not None:
            outcome = self.store.append_manifest(self.manifest)
            if outcome in {WriteOutcome.CONFLICT, WriteOutcome.INCONSISTENT}:
                raise LiveStoreError("conflicting stored deployment manifest")

    def attribute_execution(self, execution: Mapping[str, Any]) -> Attribution:
        return attribute_execution(execution, self.manifest)

    def _reducer_input(self, snapshot: Mapping[str, Any]) -> tuple[Mapping[str, Any], tuple[str, ...], bool, live_reconcile.LiveReconciler]:
        strict = any(key in snapshot for key in ("snapshot_start_observed_at", "snapshot_end_observed_at", "snapshot_canonical_digest", "pages"))
        if not strict:
            for channel in live_reconcile.ALL_CHANNELS:
                value = snapshot.get(channel)
                if isinstance(value, (list, tuple)) and any(isinstance(page, Mapping) and ("items" in page or "terminal" in page or "next_cursor" in page) for page in value):
                    strict = True
                    break
        if strict:
            return snapshot, (), False, live_reconcile.LiveReconciler(self.manifest)
        pages, reasons, legacy = _legacy_pages(snapshot)
        reducer_manifest: DeploymentManifest | Mapping[str, Any] = self.manifest
        if "snapshot_read_deadline_seconds" not in self.manifest.payload:
            payload = self.manifest.to_dict()
            payload["snapshot_read_deadline_seconds"] = "0"
            reducer_manifest = DeploymentManifest(payload)
        return pages, reasons, legacy, live_reconcile.LiveReconciler(reducer_manifest)

    def reconcile(
        self,
        rest_snapshot: Mapping[str, Any],
        ws_events: Sequence[Mapping[str, Any]] = (),
        *,
        now: Any = None,
        disconnected: bool = False,
        mode: str = "startup",
    ) -> ReconcileResult:
        snapshot = dict(rest_snapshot) if isinstance(rest_snapshot, Mapping) else {}
        event_items = tuple(ws_events) if isinstance(ws_events, (list, tuple)) else ()
        reasons: list[str] = []
        if not isinstance(rest_snapshot, Mapping):
            reasons.append("REST_SNAPSHOT_PARTIAL")
            reducer_input, adapter_reasons, legacy, reducer = {}, (), False, live_reconcile.LiveReconciler(self.manifest)
        else:
            reducer_input, adapter_reasons, legacy, reducer = self._reducer_input(snapshot)
        reasons.extend(adapter_reasons)
        deployment_id = snapshot.get("deployment_id")
        snapshot_id = snapshot.get("snapshot_id")
        account_value = snapshot.get("account_id")
        account_id = account_value if _is_id(account_value) else ""
        if not _is_id(deployment_id):
            reasons.append("DEPLOYMENT_ID_INVALID")
        elif deployment_id != self.manifest.deployment_id:
            reasons.append("DEPLOYMENT_ID_MISMATCH")
        if not _is_id(snapshot_id):
            reasons.append("SNAPSHOT_ID_INVALID")
        if not _is_id(account_value):
            reasons.append("ACCOUNT_ID_INVALID")
        elif account_value != self.manifest.payload.get("account_id"):
            reasons.append("ACCOUNT_ID_MISMATCH")
        observed_raw = _equity_observation_value(snapshot)
        observed = _utc(observed_raw)
        sections: dict[str, tuple[Any, ...]] = {}
        if legacy:
            current = _clock_value(None, now) if now is not None else _clock_value(self.clock)
            max_age = self.settings_value("snapshot_max_age_seconds", "freshness_seconds", "freshness_limit_seconds")
            if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0:
                reasons.append("FRESHNESS_POLICY_MISSING")
            if not isinstance(observed_raw, str) or observed is None:
                reasons.append("OBSERVED_AT_INVALID")
            elif isinstance(max_age, int) and not isinstance(max_age, bool) and (current - observed).total_seconds() > max_age:
                reasons.append("REST_SNAPSHOT_STALE")
            if not isinstance(snapshot.get("wallet"), Mapping) or not snapshot.get("wallet"):
                reasons.append("WALLET_INCOMPLETE")
            for key in ("positions", "orders", "executions"):
                value = snapshot.get(key)
                if not isinstance(value, (list, tuple)):
                    reasons.append(f"{key.upper()}_INCOMPLETE")
                    sections[key] = ()
                else:
                    sections[key] = tuple(value)
        else:
            sections = {key: () for key in ("positions", "orders", "executions")}
        positions, orders, executions = sections["positions"], sections["orders"], sections["executions"]
        for position in positions:
            if not isinstance(position, Mapping) or not _is_id(position.get("symbol")) or not _is_id(position.get("side")):
                reasons.append("POSITION_IDENTITY_MISSING")
        if not isinstance(ws_events, (list, tuple)):
            reasons.append("WS_EVENTS_INCOMPLETE")
        for event in event_items:
            if not isinstance(event, Mapping):
                reasons.append("WS_EVENT_INVALID")
                continue
            event_id = event.get("event_id", event.get("id", event.get("sequence", event.get("seq"))))
            if not _is_record_id(event_id):
                reasons.append("WS_EVENT_IDENTITY_MISSING")
        pure: live_reconcile.ReconcileResult | None = None
        try:
            pure = reducer.reconcile(reducer_input, event_items, mode=mode)
            reasons.extend(pure.reasons)
        except (TypeError, ValueError):
            reasons.append("REST_VALUE_INVALID")
        if disconnected:
            reasons.append("STREAM_DISCONNECTED")
        try:
            canonical_json(snapshot)
            _reject_secrets(snapshot, "rest_snapshot")
            for event in event_items:
                if isinstance(event, Mapping):
                    canonical_json(event)
                    _reject_secrets(event, "ws_event")
            facts_safe = True
        except (TypeError, ValueError):
            facts_safe = False
            reasons.append("REST_VALUE_INVALID")
        pure_reasons = pure.reasons if pure is not None else ()
        conflict = any(reason.endswith("REST_PAGE_CONFLICT") or reason in {"WS_SEQUENCE_GAP", "WS_CONFLICTING_DUPLICATE", "DEPLOYMENT_ID_MISMATCH", "ACCOUNT_ID_MISMATCH"} for reason in reasons)
        status = INCONSISTENT if conflict else (HEALTHY if pure is not None and pure.status == HEALTHY and not reasons and facts_safe else UNKNOWN)
        fact_snapshot = pure.snapshot if pure is not None and isinstance(pure.snapshot, Mapping) else snapshot
        selected = pure.applied_ws if pure is not None else ()
        checkpoints = {channel: row["sequence"] for channel, row in (pure.checkpoint_candidates.items() if pure is not None else ()) if isinstance(row.get("sequence"), int)}
        if pure is not None:
            reconciliation_id = live_reconcile._reconcile_identity(pure)[0]
        else:
            try:
                reconciliation_id = "reconcile-" + canonical_digest({"snapshot": snapshot, "events": list(event_items)})[:20]
            except (TypeError, ValueError):
                reconciliation_id = "reconcile-invalid"
        reconciliation = {"deployment_id": self.manifest.deployment_id, "reconcile_id": reconciliation_id, "status": status, "reasons": tuple(dict.fromkeys(reasons)), "observed_at": observed_raw if isinstance(observed_raw, str) else None, "watermarks": dict(snapshot.get("watermarks", {})) if isinstance(snapshot.get("watermarks", {}), Mapping) else {}, "checkpoints": checkpoints}
        if self.store is not None:
            if status == HEALTHY and pure is not None:
                application = live_reconcile.apply_reconcile(self.store, pure)
                if application.status != HEALTHY:
                    status = INCONSISTENT
                    reasons.append("STORE_CONFLICT" if "STORE_CONFLICT" in application.reasons else "STORE_APPLY_FAILED")
            else:
                outcome = self.store.append_reconciliation(self.manifest.deployment_id, reconciliation_id, status, reconciliation)
                if outcome in {WriteOutcome.CONFLICT, WriteOutcome.INCONSISTENT}:
                    status = INCONSISTENT
                    reasons.append("CONFLICTING_LIVE_FACT")
        if status == HEALTHY and isinstance(fact_snapshot, Mapping):
            self._update_order_projection_facts(fact_snapshot, tuple(selected))
        else:
            fault = next((reason for reason in reasons if reason in {"WS_SEQUENCE_GAP", "WS_CONFLICTING_DUPLICATE"} or "REST_PAGE_CONFLICT" in reason or "REST_CHANNEL_MISSING" in reason), "RECONCILIATION_NON_AUTHORITATIVE")
            for item in _manifest_entries(self.manifest):
                pair = _order_pair(item)
                if pair is not None:
                    self._order_faults[pair] = fault
        authoritative_events = tuple(selected) if status == HEALTHY and pure is not None and pure.authoritative else ()
        self._latest = fact_snapshot
        self._latest_events = (*self._latest_events, *authoritative_events)
        if status != HEALTHY:
            self._execution_pnl.clear()
            self._equity_points.clear()
            self._cashflow_points.clear()
            self._margin_facts.clear()
            self._metric_boundary_unknown = True
        if status == HEALTHY and _is_id(deployment_id) and _is_id(account_id):
            for execution in (*tuple(fact_snapshot.get("executions", ())), *selected):
                if not isinstance(execution, Mapping):
                    continue
                execution_id = execution.get("execution_id", execution.get("id"))
                execution_account = execution.get("account_id", account_id)
                execution_deployment = execution.get("deployment_id", deployment_id)
                if not _is_record_id(execution_id) or execution_account != account_id or execution_deployment != deployment_id:
                    continue
                value = _number(execution.get("realized_pnl", execution.get("realised_pnl", execution.get("pnl"))))
                if value is not None:
                    execution_key = execution_id.strip() if isinstance(execution_id, str) else str(execution_id)
                    self._execution_pnl[(deployment_id, account_id, execution_key)] = value
        if status == HEALTHY:
            self._record_metric_facts(fact_snapshot, authoritative_events)
        applied = sum(1 for event in selected if isinstance(event, Mapping) and _is_execution(event))
        duplicates = _count_duplicate_execution_events((*tuple(fact_snapshot.get("executions", ())), *event_items), default_account=account_id)
        watermarks = fact_snapshot.get("watermarks", fact_snapshot.get("stream_watermarks", {}))
        dropped = _execution_dropped_count(event_items, watermarks if isinstance(watermarks, Mapping) else {})
        realised_raw = fact_snapshot.get("realized_pnl", fact_snapshot.get("realised_pnl"))
        has_realised_total = "realized_pnl" in fact_snapshot or "realised_pnl" in fact_snapshot
        if realised_raw is None and isinstance(fact_snapshot.get("wallet"), Mapping):
            realised_raw = fact_snapshot["wallet"].get("realized_pnl", fact_snapshot["wallet"].get("realised_pnl"))
            has_realised_total = "realized_pnl" in fact_snapshot["wallet"] or "realised_pnl" in fact_snapshot["wallet"]
        realised_value = _number(realised_raw)
        pnl_provenance = "REST_TOTAL" if has_realised_total else "UNKNOWN"
        if status == HEALTHY and realised_value is None and not has_realised_total and _is_id(deployment_id) and _is_id(account_id):
            scoped = [value for (deployment, account, _), value in self._execution_pnl.items() if deployment == deployment_id and account == account_id]
            if scoped:
                realised_value = sum(scoped, Decimal("0"))
                pnl_provenance = "LOCAL_EXECUTIONS_COVERAGE"
        realised = _decimal_string(realised_value)
        unrealised = _decimal_string(fact_snapshot.get("unrealized_pnl", fact_snapshot.get("unrealised_pnl")))
        provenance = {"source_kind": "REST_WS_FIXTURE", "deployment_id": deployment_id if _is_id(deployment_id) else None, "manifest_id": self.manifest.manifest_id, "observed_at": fact_snapshot.get("observed_at") if isinstance(fact_snapshot.get("observed_at"), str) else None, "manifest_digest": self.manifest.digest, "reducer_status": pure.status if pure is not None else UNKNOWN}
        result = ReconcileResult(
            status=status,
            reasons=tuple(dict.fromkeys(reasons)),
            deployment_id=deployment_id if _is_id(deployment_id) else None,
            snapshot_id=snapshot_id if _is_id(snapshot_id) else None,
            checkpoints=dict(checkpoints),
            executions_applied=applied,
            executions_duplicate=duplicates,
            realised_pnl=realised,
            unrealised_pnl=unrealised,
            forced_close_evidence=_forced_close(authoritative_events, fact_snapshot.get("executions", ())),
            availability=AVAILABLE if status == HEALTHY else status,
            provenance=provenance,
            realised_pnl_provenance=pnl_provenance,
            executions_dropped=dropped,
            executions_gapped=int("WS_SEQUENCE_GAP" in reasons),
        )
        self._last_result = result
        return result

    reconcile_fixture = reconcile
    reconcile_snapshot = reconcile

    def _order_manifest_role(self, order: Mapping[str, Any], pair: tuple[str, str]) -> str | None:
        """Return an explicitly declared role; symbol/side never invents one."""
        explicit = next((order[key] for key in _ORDER_ROLE_KEYS if key in order), None)
        if explicit is not None:
            return str(explicit).strip().upper() if isinstance(explicit, str) and explicit.strip() else None
        if order.get("is_entry") is True:
            return "ENTRY"
        strategy = order.get("strategy_id", order.get("strategyId"))
        link = order.get("orderLinkId", order.get("order_link_id", order.get("order_link")))
        candidates: list[Mapping[str, Any]] = []
        for item in _manifest_entries(self.manifest):
            item_id = item.get("strategy_id", item.get("id", item.get("identity")))
            links = item.get("orderLinkId", item.get("order_link_id", item.get("order_links", ())))
            link_values = (links,) if isinstance(links, str) else tuple(links) if isinstance(links, (list, tuple, set)) else ()
            if (strategy is not None and item_id == strategy) or (link is not None and link in link_values):
                candidates.append(item)
        if not candidates:
            same_pair = [
                item for item in _manifest_entries(self.manifest)
                if _order_pair(item) == pair and any(key in item for key in _ORDER_ROLE_KEYS)
            ]
            candidates = same_pair if len(same_pair) == 1 else []
        roles = {str(item[key]).strip().upper() for item in candidates for key in _ORDER_ROLE_KEYS if isinstance(item.get(key), str) and item[key].strip()}
        return next(iter(roles)) if len(roles) == 1 else None

    @staticmethod
    def _order_payload_digest(item: Mapping[str, Any]) -> str:
        envelope = {"channel", "kind", "event_type", "event_id", "sequence", "seq", "source_sequence", "observed_at", "observed_at_utc", "timestamp_received"}
        return canonical_digest({key: value for key, value in item.items() if key not in envelope})

    def _replace_order_baseline(self, snapshot: Mapping[str, Any]) -> set[tuple[str, str]]:
        facts: dict[tuple[str, str, str], dict[str, Any]] = {}
        by_identity: dict[tuple[str, str, str], tuple[int, str]] = {}
        faults: dict[tuple[str, str], str] = {}
        pairs: set[tuple[str, str]] = set()
        self._position_facts = {}
        position_digests: dict[tuple[str, str], str] = {}
        for raw in snapshot.get("positions", ()):
            if not isinstance(raw, Mapping):
                continue
            pair = _order_pair(raw)
            if pair is None:
                continue
            pairs.add(pair)
            digest = self._order_payload_digest(raw)
            prior_digest = position_digests.get(pair)
            if prior_digest is not None and prior_digest != digest:
                faults[pair] = "POSITION_CONFLICT"
            position_digests[pair] = digest
            self._position_facts[pair] = dict(raw)
        for raw in snapshot.get("orders", ()):
            if not isinstance(raw, Mapping):
                continue
            pair = _order_pair(raw)
            if pair is None:
                continue
            pairs.add(pair)
            identity = _order_id(raw)
            revision = _order_revision(raw)
            if identity is None or revision is None:
                faults[pair] = "ORDER_IDENTITY_INVALID"
                continue
            key = (*pair, identity)
            digest = self._order_payload_digest(raw)
            prior = by_identity.get(key)
            if prior is not None:
                if prior != (revision, digest):
                    faults[pair] = "ORDER_IDENTITY_CONFLICT"
                continue
            if prior is None:
                by_identity[key] = (revision, digest)
                facts[key] = dict(raw)
        self._order_facts = facts
        self._order_faults.update(faults)
        return pairs

    def _apply_order_ws(self, events: Sequence[Mapping[str, Any]], fallback_observed: datetime | None) -> tuple[set[tuple[str, str]], dict[tuple[str, str], datetime]]:
        touched: set[tuple[str, str]] = set()
        confirmation_candidates: dict[tuple[str, str], list[tuple[datetime, str]]] = {}
        for raw in events:
            if not isinstance(raw, Mapping) or _order_channel(raw) not in {"order", "orders"}:
                continue
            nested = raw.get("order")
            record = {**dict(nested), **dict(raw)} if isinstance(nested, Mapping) else dict(raw)
            pair = _order_pair(record)
            if pair is None:
                continue
            touched.add(pair)
            identity = _order_id(record)
            revision = _order_revision(record)
            if identity is None or revision is None:
                self._order_faults[pair] = "ORDER_IDENTITY_INVALID"
                continue
            key = (*pair, identity)
            prior = self._order_facts.get(key)
            if any(record.get(name) is True for name in ("cancel_replace", "is_cancel_replace", "replaced", "replace")) or any(record.get(name) is not None for name in ("replaces_order_id", "cancelled_order_id")):
                self._order_faults[pair] = "CANCEL_REPLACE_UNPROVEN"
                continue
            timestamp = _order_timestamp(record) or fallback_observed
            if timestamp is None:
                self._order_faults[pair] = "ORDER_TIMESTAMP_INVALID"
                continue
            digest = self._order_payload_digest(record)
            if prior is not None:
                prior_revision = _order_revision(prior)
                if prior_revision is None or revision < prior_revision or (revision == prior_revision and digest != self._order_payload_digest(prior)):
                    self._order_faults[pair] = "ORDER_REVISION_CONFLICT"
                    continue
                if revision > prior_revision + 1:
                    self._order_faults[pair] = "ORDER_REVISION_GAP"
                    continue
            self._order_facts[key] = record
            if self._order_manifest_role(record, pair) == "ENTRY":
                confirmation_candidates.setdefault(pair, []).append((timestamp, digest))
        confirmations = {pair: max(values, key=lambda item: (item[0], item[1]))[0] for pair, values in confirmation_candidates.items()}
        return touched, confirmations

    def _update_order_projection_facts(self, snapshot: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> None:
        observed = _utc(snapshot.get("snapshot_end_observed_at", snapshot.get("observed_at")))
        declared_pairs = {
            pair for item in (*_manifest_entries(self.manifest), *tuple(snapshot.get("positions", ())), *tuple(snapshot.get("orders", ())))
            if isinstance(item, Mapping) and (pair := _order_pair(item)) is not None
        }
        for pair in declared_pairs:
            self._order_faults.pop(pair, None)
        pairs = self._replace_order_baseline(snapshot)
        pairs.update(pair for pair in (_order_pair(item) for item in snapshot.get("orders", ())) if pair is not None)
        pairs.update(pair for pair in (_order_pair(item) for item in snapshot.get("positions", ())) if pair is not None)
        known_pairs = {
            pair for item in _manifest_entries(self.manifest)
            if (pair := _order_pair(item)) is not None
        }
        pairs.update(known_pairs)
        if observed is not None:
            for pair in pairs:
                prior = self._order_observations.get(pair, {})
                self._order_observations[pair] = {
                    "observation_at": observed,
                    "source": "REST_RECONCILED",
                    "confirmation_at": prior.get("confirmation_at"),
                }
        touched, confirmations = self._apply_order_ws(events, observed)
        for pair in touched:
            previous = self._order_observations.get(pair, {})
            candidates = [
                (_order_timestamp(event) or observed, canonical_digest(event))
                for event in events
                if isinstance(event, Mapping) and _order_channel(event) in {"order", "orders"} and _order_pair(event) == pair and (_order_timestamp(event) or observed) is not None
            ]
            timestamp = max(candidates, key=lambda item: (item[0], item[1]))[0] if candidates else None
            if timestamp is not None:
                self._order_observations[pair] = {**previous, "observation_at": timestamp, "source": "WS"}
        for pair, timestamp in confirmations.items():
            self._order_observations.setdefault(pair, {"observation_at": observed, "source": "REST_RECONCILED"})["confirmation_at"] = timestamp
        for pair in pairs:
            state = self._order_observations.setdefault(pair, {"observation_at": observed, "source": "REST_RECONCILED", "confirmation_at": None})
            if state.get("confirmation_at") is None:
                active_entry = any(
                    key[:2] == pair and self._order_manifest_role(order, pair) == "ENTRY" and _order_status(order) not in _ORDER_CLOSED_STATUSES
                    for key, order in self._order_facts.items()
                )
                if active_entry and observed is not None:
                    state["confirmation_at"] = observed

    def order_reconcile_policy(
        self,
        *,
        in_flight: bool = False,
        pending_triggers: int = 0,
        rest_interval_seconds: Any = "10",
        completion_seconds: Any = "5",
        validation_seconds: Any = "0",
        append_seconds: Any = "0",
        chart_get_seconds: Any = "5",
        completion_validation_append_seconds: Any = None,
        chart_get_budget_seconds: Any = None,
        now: Any = None,
        last_rest_observed_at: Any = None,
    ) -> dict[str, Any]:
        """Evaluate fixed P2AB-20 timing/coalescing evidence without a scheduler."""
        values = {
            "rest_interval_seconds": rest_interval_seconds,
            "completion_seconds": completion_seconds,
            "validation_seconds": validation_seconds,
            "append_seconds": append_seconds,
            "chart_get_seconds": chart_get_seconds if chart_get_budget_seconds is None else chart_get_budget_seconds,
        }
        if completion_validation_append_seconds is not None:
            values["completion_seconds"] = completion_validation_append_seconds
            values["validation_seconds"] = "0"
            values["append_seconds"] = "0"
        numbers = {key: _number(value) for key, value in values.items()}
        reasons: list[str] = []
        for key, number in numbers.items():
            if number is None or number < 0:
                reasons.append(f"{key.upper()}_INVALID")
        if numbers["rest_interval_seconds"] is not None and numbers["rest_interval_seconds"] > 10:
            reasons.append("REST_INTERVAL_EXCEEDED")
        total = None if any(numbers[key] is None for key in ("completion_seconds", "validation_seconds", "append_seconds")) else sum((numbers[key] for key in ("completion_seconds", "validation_seconds", "append_seconds") if numbers[key] is not None), Decimal("0"))
        if total is not None and total > 5:
            reasons.append("COMPLETION_BUDGET_EXCEEDED")
        if numbers["chart_get_seconds"] is not None and numbers["chart_get_seconds"] > 5:
            reasons.append("CHART_GET_BUDGET_EXCEEDED")
        if not isinstance(in_flight, bool):
            reasons.append("IN_FLIGHT_INVALID")
        if isinstance(pending_triggers, bool) or not isinstance(pending_triggers, int) or pending_triggers < 0:
            reasons.append("PENDING_TRIGGERS_INVALID")
            pending = 0
        else:
            pending = pending_triggers
        rest_due = False
        if now is not None or last_rest_observed_at is not None:
            try:
                current = _clock_value(self.clock) if now is None else _clock_value(None, now)
                observed = _utc(last_rest_observed_at)
                if observed is None:
                    raise ValueError
                age = current - observed
                if age.total_seconds() < 0:
                    raise ValueError
                rest_due = numbers["rest_interval_seconds"] is not None and Decimal(str(age.total_seconds())) >= numbers["rest_interval_seconds"]
            except (TypeError, ValueError):
                reasons.append("REST_CLOCK_INVALID")
        status = AVAILABLE if not reasons else UNKNOWN
        return {
            "status": status,
            "availability": status,
            "valid": status == AVAILABLE,
            "reasons": tuple(dict.fromkeys(reasons)),
            "rest_open_orders_interval_seconds": _canonical_decimal(numbers["rest_interval_seconds"]),
            "completion_budget_seconds": _canonical_decimal(total),
            "completion_validation_append_seconds": _canonical_decimal(total),
            "chart_get_budget_seconds": _canonical_decimal(numbers["chart_get_seconds"]),
            "max_in_flight": 1,
            "in_flight": in_flight if isinstance(in_flight, bool) else None,
            "coalesced_triggers": max(0, pending - 1),
            "rest_due": rest_due,
        }

    evaluate_order_reconcile_policy = order_reconcile_policy
    order_reconcile_evidence = order_reconcile_policy

    def entry_order_projection(self, pair: Any = None, side: Any = None, *, symbol: Any = None, now: Any = None) -> dict[str, Any]:
        """Return a deterministic, read-only projection of explicit ENTRY orders."""
        if isinstance(pair, Mapping):
            symbol, side = pair.get("symbol", symbol), pair.get("side", pair.get("direction", side))
        elif isinstance(pair, (tuple, list)) and len(pair) == 2:
            symbol, side = pair
        elif symbol is None:
            symbol = pair
        if not isinstance(symbol, str) or not symbol.strip() or not isinstance(side, str) or not side.strip():
            return {"status": UNKNOWN, "availability": UNKNOWN, "reason": "PAIR_INVALID", "policy": self.order_reconcile_policy()}
        key_pair = (symbol.strip().upper(), _side(side))
        observation = self._order_observations.get(key_pair)
        base: dict[str, Any] = {
            "symbol": key_pair[0], "side": key_pair[1], "pair": {"symbol": key_pair[0], "side": key_pair[1]},
            "status": UNKNOWN, "availability": UNKNOWN, "state": None, "source": None,
            "observation_at": None, "observation_utc": None, "observation_age": None,
            "confirmation_at": None, "confirmation_utc": None, "confirmation_age": None,
            "position": None, "position_quantity": None, "active_entry_quantity": None, "active_entry_notional": None,
            "orders": (), "order_list": (), "order_ids": [], "order_revisions": [], "constituent_order_ids": [],
            "constituent_revisions": [], "order_digest": None, "canonical_digest": None,
            "reason": None, "policy": self.order_reconcile_policy(),
        }
        if observation is None or observation.get("observation_at") is None:
            base["reason"] = "OBSERVATION_UNKNOWN"
            return base
        current = _clock_value(None, now) if now is not None else _clock_value(self.clock)
        observed = observation["observation_at"]
        age = current - observed
        if age.total_seconds() < 0:
            base["reason"] = "OBSERVATION_TIME_INVALID"
            return base
        confirmation = observation.get("confirmation_at")
        confirmation_age = current - confirmation if isinstance(confirmation, datetime) else None
        base.update({
            "observation_at": observed.isoformat().replace("+00:00", "Z"),
            "observation_utc": observed.isoformat().replace("+00:00", "Z"),
            "observation_age": _canonical_decimal(Decimal(str(age.total_seconds()))),
            "confirmation_at": confirmation.isoformat().replace("+00:00", "Z") if isinstance(confirmation, datetime) else None,
            "confirmation_utc": confirmation.isoformat().replace("+00:00", "Z") if isinstance(confirmation, datetime) else None,
            "confirmation_age": _canonical_decimal(Decimal(str(confirmation_age.total_seconds()))) if confirmation_age is not None and confirmation_age.total_seconds() >= 0 else None,
            "source": observation.get("source"),
        })
        if key_pair in self._order_faults:
            base["reason"] = self._order_faults[key_pair]
            return base
        if age.total_seconds() > 20:
            base.update({"status": STALE, "availability": STALE, "reason": "OBSERVATION_STALE"})
            return base
        position = self._position_facts.get(key_pair)
        position_qty = None
        if isinstance(position, Mapping):
            position_qty = next((_number(position.get(name)) for name in ("size", "qty", "quantity", "position_qty") if name in position), None)
            base["position"] = dict(position)
            base["position_quantity"] = _canonical_decimal(position_qty)
            if position_qty is None:
                base["reason"] = "POSITION_QUANTITY_UNKNOWN"
                return base
        active: list[tuple[str, int, str, Mapping[str, Any]]] = []
        for order_key, order in self._order_facts.items():
            if order_key[:2] != key_pair or self._order_manifest_role(order, key_pair) != "ENTRY":
                if order_key[:2] == key_pair and _order_status(order) not in _ORDER_CLOSED_STATUSES and self._order_manifest_role(order, key_pair) is None:
                    base["reason"] = "ORDER_ROLE_UNKNOWN"
                    return base
                continue
            status = _order_status(order)
            if status in _ORDER_CLOSED_STATUSES or status not in _ORDER_ACTIVE_STATUSES:
                continue
            identity = _order_id(order)
            revision = _order_revision(order)
            if identity is None or revision is None:
                base["reason"] = "ORDER_IDENTITY_INVALID"
                return base
            active.append((identity, revision, self._order_payload_digest(order), order))
        active.sort(key=lambda item: (item[0], item[1], item[2]))
        entries = [item[3] for item in active]
        quantity_values = [_number(next((item.get(name) for name in _ORDER_QTY_KEYS if name in item), None)) for item in entries]
        price_values = [_number(next((item.get(name) for name in _ORDER_PRICE_KEYS if name in item), None)) for item in entries]
        quantity_known = all(value is not None and value >= 0 for value in quantity_values)
        notional_known = quantity_known and all(value is not None and value >= 0 for value in price_values)
        quantity = sum(quantity_values, Decimal("0")) if quantity_known else None
        notional = sum((qty * price for qty, price in zip(quantity_values, price_values)), Decimal("0")) if notional_known else None
        in_position = position_qty is not None and position_qty > 0
        state = IN_POSITION if in_position else PENDING_ENTRY if entries else NO_POSITION
        order_rows = tuple({"order_id": identity, "revision": revision, "digest": digest} for identity, revision, digest, _ in active)
        digest = canonical_digest([dict(order) for order in entries]) if entries else canonical_digest([])
        base.update({
            "status": CURRENT, "availability": CURRENT, "state": state,
            "active_entry_quantity": _canonical_decimal(quantity), "active_entry_notional": _canonical_decimal(notional),
            "orders": order_rows, "order_list": order_rows,
            "order_ids": [item[0] for item in active], "order_revisions": [item[1] for item in active],
            "constituent_order_ids": [item[0] for item in active], "constituent_revisions": [item[1] for item in active],
            "order_digest": digest, "canonical_digest": digest,
        })
        if not quantity_known or notional_known is False:
            base.update({
                "status": UNKNOWN,
                "availability": UNKNOWN,
                "state": None,
                "active_entry_quantity": None,
                "active_entry_notional": None,
                "reason": "ORDER_QUANTITY_OR_PRICE_UNKNOWN",
            })
        return base

    order_projection = entry_order_projection
    project_entry_orders = entry_order_projection
    read_order_model = entry_order_projection
    order_read_model = entry_order_projection

    def settings_value(self, *names: str) -> int | None:
        return next((value for name in names if isinstance((value := self._settings.get(name)), int) and not isinstance(value, bool) and value >= 0), None)

    def _snapshot_provenance(self) -> dict[str, Any]:
        snapshot = self._latest or {}
        return {"source_kind": "REST_WS_FIXTURE", "deployment_id": snapshot.get("deployment_id"), "manifest_id": self.manifest.manifest_id, "observed_at": snapshot.get("observed_at"), "manifest_digest": self.manifest.digest}

    def _invalidate_metric_integrity(self) -> None:
        self._integrity_invalid = True
        self._metric_boundary_unknown = True
        self._equity_points.clear()
        self._cashflow_points.clear()

    def _record_metric_facts(self, snapshot: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> None:
        wallet = snapshot.get("wallet") if isinstance(snapshot.get("wallet"), Mapping) else {}
        equity = next((wallet.get(key, snapshot.get(key)) for key in ("equity", "wallet_equity", "balance") if key in wallet or key in snapshot), None)
        timestamp = _equity_observation_value(snapshot)
        cashflows = snapshot.get("cashflows", ())
        currency = wallet.get("currency", snapshot.get("currency"))
        flow_issue = False
        if isinstance(cashflows, (list, tuple)):
            for ordinal, item in enumerate(cashflows):
                if not isinstance(item, Mapping):
                    flow_issue = True
                    continue
                signed, classification = _cashflow_signed(item)
                try:
                    order = _fact_order(item, str(item.get("cashflow_id", item.get("id", ordinal))))
                except (TypeError, ValueError):
                    order = None
                if signed is None or order is None or not _currency_known(item.get("currency")) or not _currency_known(currency) or item.get("currency") != currency:
                    flow_issue = True
        elif cashflows is not None:
            flow_issue = True
        if self._metric_boundary_unknown and not flow_issue and not self._integrity_invalid and not self._store_reconstruction_blocked:
            self._equity_points.clear()
            self._cashflow_points.clear()
            self._metric_boundary_unknown = False
        elif flow_issue:
            self._invalidate_metric_integrity()
            return
        source_id = snapshot.get("snapshot_id")
        snapshot_value = _number(equity)
        snapshot_time = _utc(timestamp)
        if not _is_id(source_id):
            self._invalidate_metric_integrity()
            return
        if self._integrity_invalid:
            return
        else:
            existing = next((item for item in self._equity_points if item.get("source_id") == source_id), None)
            if existing is not None:
                if _number(existing.get("equity")) != snapshot_value or _fact_time(existing) != snapshot_time or existing.get("currency") != currency:
                    self._invalidate_metric_integrity()
                    return
            elif snapshot_value is None or snapshot_time is None:
                self._invalidate_metric_integrity()
                return
            else:
                self._equity_points.append({"timestamp_utc": timestamp, "equity": equity, "currency": currency, "source_id": source_id, "source_kind": "REST", "source_sequence": 0})
        if isinstance(cashflows, (list, tuple)):
            known = {str(item.get("cashflow_id", item.get("id", item.get("source_id", "")))) for item in self._cashflow_points if isinstance(item, Mapping)}
            for ordinal, item in enumerate(cashflows):
                if not isinstance(item, Mapping):
                    continue
                identity_value = item.get("cashflow_id", item.get("id", item.get("source_id")))
                if identity_value is None:
                    try:
                        identity_value = canonical_digest(item)
                    except (TypeError, ValueError):
                        identity_value = f"invalid-{ordinal}"
                identity = str(identity_value)
                if identity not in known:
                    self._cashflow_points.append(dict(item))
                    known.add(identity)
        known_events = {str(item.get("event_id", item.get("id", item.get("source_id", "")))) for item in self._margin_facts if isinstance(item, Mapping)}
        for item in events:
            if not isinstance(item, Mapping):
                continue
            identity = str(item.get("event_id", item.get("id", item.get("source_id", canonical_digest(item)))))
            if identity not in known_events:
                self._margin_facts.append(item)
                known_events.add(identity)
        if isinstance(wallet, Mapping) and any(key in wallet for key in ("initial_margin", "im", "maintenance_margin", "mm", "margin_balance")):
            self._margin_facts.append({**dict(wallet), "timestamp_utc": timestamp, "event_id": snapshot.get("snapshot_id", "snapshot"), "source_kind": "REST"})

    def _load_store_facts(self) -> None:
        if self.store is None or self._store_reconstruction_blocked:
            return
        deployment = self.manifest.deployment_id
        account_rows = self.store.rows("account_snapshots", deployment_id=deployment)
        manifest_account = self.manifest.payload.get("account_id")
        stored_cashflows = self.store.rows("cashflow_events", deployment_id=deployment)
        if not _is_id(manifest_account) or any(row.get("account_id") != manifest_account for row in account_rows) or any(row.get("account_id") != manifest_account for row in stored_cashflows):
            self._store_reconstruction_blocked = True
            self._metric_boundary_unknown = True
            return
        if account_rows and self._latest is None:
            reconciliations = self.store.rows("reconciliations", deployment_id=deployment)
            if not reconciliations:
                self._store_reconstruction_blocked = True
                self._metric_boundary_unknown = True
                return
            statuses = [str(row.get("status", "UNKNOWN")) for row in reconciliations]
            eligible_snapshot_ids: set[str] | None = None
            boundary_time: datetime | None = None
            boundary_rows = [row for index, row in enumerate(reconciliations) if statuses[index] != HEALTHY]
            boundary_times = [_stored_timestamp(row) for row in boundary_rows]
            if boundary_rows:
                if any(value is None for value in boundary_times):
                    self._store_reconstruction_blocked = True
                    self._metric_boundary_unknown = True
                    return
                boundary_time = max(value for value in boundary_times if value is not None)
                post_boundary = [row for index, row in enumerate(reconciliations) if statuses[index] == HEALTHY and (_stored_timestamp(row) or datetime.min.replace(tzinfo=timezone.utc)) > boundary_time]
                eligible_snapshot_ids = {str(row.get("snapshot_id")) for row in post_boundary if _is_id(row.get("snapshot_id"))}
                if not eligible_snapshot_ids:
                    self._store_reconstruction_blocked = True
                    self._metric_boundary_unknown = True
                    return
                account_rows = tuple(row for row in account_rows if str(row.get("snapshot_id", "")) in eligible_snapshot_ids)
                if not account_rows:
                    self._store_reconstruction_blocked = True
                    self._metric_boundary_unknown = True
                    return
                account_id = manifest_account
                account_times = [_equity_observation(row) for row in account_rows]
                if not _is_id(account_id) or any(row.get("account_id") != account_id for row in account_rows) or any(value is None for value in account_times):
                    self._store_reconstruction_blocked = True
                    self._metric_boundary_unknown = True
                    return
                baseline_time = min(value for value in account_times if value is not None)
                latest_time = max(value for value in account_times if value is not None)
            else:
                account_times = [_equity_observation(row) for row in account_rows]
                if any(value is None for value in account_times):
                    self._store_reconstruction_blocked = True
                    self._metric_boundary_unknown = True
                    self._latest = None
                    self._latest_events = ()
                    self._equity_points.clear()
                    self._cashflow_points.clear()
                    return
                baseline_time = latest_time = None
            def reject_account_rows() -> None:
                self._store_reconstruction_blocked = True
                self._metric_boundary_unknown = True
                self._integrity_invalid = True
                self._latest = None
                self._latest_events = ()
                self._equity_points.clear()
                self._cashflow_points.clear()
            unique_account_rows: list[Mapping[str, Any]] = []
            seen_account_points: dict[str, tuple[Any, datetime | None, Any]] = {}
            for row in account_rows:
                snapshot_key = row.get("snapshot_id")
                if not _is_id(snapshot_key):
                    reject_account_rows()
                    return
                wallet = row.get("wallet") if isinstance(row.get("wallet"), Mapping) else {}
                equity_key_present = any(key in wallet or key in row for key in ("equity", "wallet_equity", "balance"))
                equity = next((wallet.get(key, row.get(key)) for key in ("equity", "wallet_equity", "balance") if key in wallet or key in row), None)
                equity_value = _number(equity)
                observation = _equity_observation(row)
                if observation is None or not equity_key_present or equity_value is None:
                    reject_account_rows()
                    return
                point = (equity_value, observation, wallet.get("currency", row.get("currency")))
                prior = seen_account_points.get(str(snapshot_key))
                if prior is not None:
                    if prior != point:
                        reject_account_rows()
                        return
                    continue
                seen_account_points[str(snapshot_key)] = point
                unique_account_rows.append(row)
            account_rows = tuple(unique_account_rows)
            account_times = [_equity_observation(row) for row in account_rows]
            account_currency = next((row.get("wallet", {}).get("currency") if isinstance(row.get("wallet"), Mapping) else row.get("currency") for row in account_rows if _currency_known((row.get("wallet", {}).get("currency") if isinstance(row.get("wallet"), Mapping) else row.get("currency")))), None)
            if any(value is None for value in account_times):
                reject_account_rows()
                return
            if eligible_snapshot_ids is not None:
                baseline_time = min(value for value in account_times if value is not None)
                latest_time = max(value for value in account_times if value is not None)
            latest = account_rows[-1]
            self._latest = latest
            self._latest_events = () if eligible_snapshot_ids is not None else tuple(self.store.rows("stream_events", deployment_id=deployment))
            for row in account_rows:
                wallet = row.get("wallet") if isinstance(row.get("wallet"), Mapping) else {}
                equity = next((wallet.get(key, row.get(key)) for key in ("equity", "wallet_equity", "balance") if key in wallet or key in row), None)
                timestamp_value = _equity_observation_value(row)
                if _number(equity) is not None and _equity_observation(row) is not None:
                    self._equity_points.append({"timestamp_utc": timestamp_value, "equity": equity, "currency": wallet.get("currency", row.get("currency")), "source_id": row.get("snapshot_id", "snapshot"), "source_kind": "REST", "source_sequence": 0})
            validated_cashflows: list[Mapping[str, Any]] = []
            for ordinal, row in enumerate(stored_cashflows):
                if not isinstance(row, Mapping):
                    self._store_reconstruction_blocked = True
                    self._metric_boundary_unknown = True
                    self._latest = None
                    self._latest_events = ()
                    self._equity_points.clear()
                    self._cashflow_points.clear()
                    return
                observed_time = _stored_timestamp(row)
                fact_time = _fact_time(row)
                try:
                    order = _fact_order(row, str(row.get("cashflow_id", row.get("id", ordinal))))
                except (TypeError, ValueError):
                    order = None
                signed, _classification = _cashflow_signed(row)
                flow_currency = row.get("currency")
                if observed_time is None or fact_time is None or order is None or signed is None or not _currency_known(flow_currency) or not _currency_known(account_currency) or flow_currency != account_currency:
                    self._store_reconstruction_blocked = True
                    self._metric_boundary_unknown = True
                    self._latest = None
                    self._latest_events = ()
                    self._equity_points.clear()
                    self._cashflow_points.clear()
                    return
                validated_cashflows.append(row)
            cashflow_rows = tuple(validated_cashflows)
            if eligible_snapshot_ids is not None:
                filtered_cashflows: list[Mapping[str, Any]] = []
                for row in cashflow_rows:
                    if row.get("account_id") != account_id:
                        self._store_reconstruction_blocked = True
                        self._metric_boundary_unknown = True
                        self._latest = None
                        self._equity_points.clear()
                        return
                    observed_time = _stored_timestamp(row)
                    fact_time = _fact_time(row)
                    if observed_time is not None and fact_time is not None and observed_time <= boundary_time and baseline_time < fact_time <= latest_time:
                        self._metric_boundary_unknown = True
                        self._cashflow_points.clear()
                        return
                    if observed_time is None or fact_time is None:
                        self._store_reconstruction_blocked = True
                        self._metric_boundary_unknown = True
                        self._latest = None
                        self._equity_points.clear()
                        return
                    if observed_time <= boundary_time or fact_time <= baseline_time or fact_time > latest_time:
                        continue
                    filtered_cashflows.append(row)
                cashflow_rows = tuple(filtered_cashflows)
            self._cashflow_points.extend(cashflow_rows)

    def _account_metrics(self) -> dict[str, Any]:
        self._load_store_facts()
        points: list[dict[str, Any]] = []
        for item in self._equity_points:
            value = _number(item.get("equity"))
            timestamp = _fact_time(item)
            if value is not None and timestamp is not None:
                points.append({**item, "value": value, "timestamp": timestamp})
        points.sort(key=lambda item: _fact_order_key(_fact_order(item, str(item.get("source_id", "equity")))))
        if not points:
            return {"availability": UNKNOWN, "currency": None, "adjusted_equity": None, "net_trading_pnl": None, "max_drawdown": None, "max_drawdown_pct": None, "adjusted_equity_path": (), "drawdown_path": (), "drawdown_pct_path": (), "high_water": None, "reason": "EQUITY_UNKNOWN"}
        currencies = {item.get("currency") if _currency_known(item.get("currency")) else None for item in points}
        currency = next(iter(currencies), None) if len(currencies) == 1 else None
        flows: list[dict[str, Any]] = []
        bad_times: list[datetime] = []
        unlocatable_flow = False
        for ordinal, raw in enumerate(self._cashflow_points):
            if not isinstance(raw, Mapping):
                unlocatable_flow = True
                continue
            signed, classification = _cashflow_signed(raw)
            try:
                order = _fact_order(raw, str(raw.get("cashflow_id", raw.get("id", ordinal))))
            except (TypeError, ValueError):
                order = None
            if order is None:
                unlocatable_flow = True
                continue
            timestamp = order[0]
            flow_currency = raw.get("currency")
            if not _currency_known(flow_currency) or not _currency_known(currency) or flow_currency != currency:
                bad_times.append(timestamp)
            if signed is None:
                bad_times.append(timestamp)
            flows.append({**dict(raw), "timestamp": timestamp, "signed": signed, "classification": classification, "order": order})
        flows.sort(key=lambda item: item["order"])
        start = points[0]["timestamp"]
        adjusted: list[dict[str, Any]] = []
        high: Decimal | None = None
        cumulative = Decimal("0")
        unknown = self._metric_boundary_unknown or self._integrity_invalid or self._store_reconstruction_blocked or len(currencies) != 1 or not _currency_known(currency) or bool(bad_times) or unlocatable_flow
        flow_index = 0
        for point in points:
            while flow_index < len(flows) and flows[flow_index]["timestamp"] <= point["timestamp"]:
                flow = flows[flow_index]
                if flow["timestamp"] > start:
                    if flow["signed"] is None or any(bad <= flow["timestamp"] for bad in bad_times):
                        unknown = True
                    elif not unknown:
                        cumulative += flow["signed"]
                flow_index += 1
            value = None if unknown else point["value"] - cumulative
            if value is not None:
                high = value if high is None else max(high, value)
                drawdown = value - high
                pct = drawdown / high * Decimal("100") if high > 0 else None
            else:
                drawdown = pct = None
            adjusted.append({"timestamp_utc": point["timestamp"].isoformat().replace("+00:00", "Z"), "source_id": point.get("source_id"), "value": _canonical_decimal(value), "high_water": _canonical_decimal(high), "drawdown": _canonical_decimal(drawdown), "drawdown_pct": _canonical_decimal(pct)})
        absolute_values = [Decimal(item["drawdown"]) for item in adjusted if item["drawdown"] is not None]
        pct_values = [Decimal(item["drawdown_pct"]) if item["drawdown_pct"] is not None else None for item in adjusted]
        last = adjusted[-1]
        baseline = adjusted[0]["value"]
        net = None if baseline is None or last["value"] is None or unknown else _canonical_decimal(Decimal(last["value"]) - Decimal(baseline))
        result = {
            "availability": UNKNOWN if unknown else AVAILABLE,
            "currency": currency,
            "t_start": adjusted[0]["timestamp_utc"],
            "adjusted_equity": last["value"],
            "adjusted_equity_path": tuple(adjusted),
            "drawdown_path": tuple({"timestamp_utc": item["timestamp_utc"], "source_id": item["source_id"], "value": item["drawdown"]} for item in adjusted),
            "drawdown_pct_path": tuple({"timestamp_utc": item["timestamp_utc"], "source_id": item["source_id"], "value": item["drawdown_pct"]} for item in adjusted),
            "high_water": last["high_water"],
            "net_trading_pnl": net,
            "max_drawdown": _canonical_decimal(-min(absolute_values)) if absolute_values and not unknown else None,
            "max_drawdown_pct": _canonical_decimal(-min(Decimal(value) for value in pct_values if value is not None)) if pct_values and all(value is not None for value in pct_values) and not unknown else None,
            "reason": "CASHFLOW_UNKNOWN" if unknown else None,
        }
        return result

    def account_dashboard(self) -> dict[str, Any]:
        snapshot = self._latest
        if snapshot is None:
            self._load_store_facts()
            snapshot = self._latest
        if snapshot is None:
            return {"availability": UNKNOWN, "status": UNKNOWN, "provenance": self._snapshot_provenance()}
        wallet = snapshot.get("wallet", {}) if isinstance(snapshot.get("wallet"), Mapping) else {}
        result = self._latest_reconcile_status()
        model: dict[str, Any] = {"availability": result, "status": result, "provenance": self._snapshot_provenance(), "account_id": snapshot.get("account_id"), "forced_close_evidence": _forced_close(self._latest_events, snapshot.get("executions", ())), "positions": len(snapshot.get("positions", ())), "orders": len(snapshot.get("orders", ())), "facts": {}, "realized_pnl_provenance": self._last_result.realised_pnl_provenance if self._last_result else "UNKNOWN"}
        for output, keys in (("equity", ("equity", "wallet_equity")), ("available_margin", ("available_margin", "free_margin")), ("realized_pnl", ("realized_pnl", "realised_pnl")), ("unrealized_pnl", ("unrealized_pnl", "unrealised_pnl")), ("im", ("im", "initial_margin")), ("mm", ("mm", "maintenance_margin"))):
            value = next((wallet.get(key, snapshot.get(key)) for key in keys if key in wallet or key in snapshot), None)
            if output == "realized_pnl" and value is None and self._last_result is not None:
                value = self._last_result.realised_pnl
            encoded = _decimal_string(value)
            fact_availability = AVAILABLE if result == HEALTHY and encoded is not None else UNKNOWN
            model[output] = encoded
            fact_provenance = self._last_result.realised_pnl_provenance if output == "realized_pnl" and self._last_result else "REST_SNAPSHOT"
            model["facts"][output] = {"value": encoded, "availability": fact_availability, "provenance": fact_provenance, "source": self._snapshot_provenance()}
        metrics = self._account_metrics()
        model.update({key: value for key, value in metrics.items() if key != "availability"})
        model["metrics_availability"] = metrics.get("availability", UNKNOWN)
        return model

    read_account_model = account_dashboard
    account_read_model = account_dashboard
    account_metrics = _account_metrics

    def margin_timeline(self, events: Sequence[Mapping[str, Any]] | None = None, *, now: Any = None) -> tuple[dict[str, Any], ...]:
        """Build a historical margin series using event-time freshness.

        ``now`` is retained for call compatibility.  Current-time staleness is
        a dashboard concern; applying it here would rewrite historical points.
        """
        facts = tuple(events) if events is not None else tuple(self._margin_facts)
        normalized: list[dict[str, Any]] = []
        for ordinal, item in enumerate(facts):
            if not isinstance(item, Mapping):
                continue
            timestamp = _fact_time(item)
            if timestamp is None:
                continue
            fields = {key: item.get(key) for key in ("initial_margin", "im", "maintenance_margin", "mm", "margin_balance", "denominator", "equity") if item.get(key) is not None}
            if not fields:
                continue
            source_id = str(item.get("source_id", item.get("event_id", ordinal)))
            normalized.append({"timestamp": timestamp, "fields": fields, "currency": item.get("currency"), "unit": item.get("unit"), "source_id": source_id, "order": _fact_order(item, source_id)})
        normalized.sort(key=lambda item: (item["timestamp"], _fact_order_key(item["order"])))
        freshness = self.settings_value("margin_freshness_seconds", "freshness_seconds", "freshness_limit_seconds")
        state: dict[str, tuple[Decimal, datetime, str, str | None, str | None]] = {}
        invalid: set[str] = set()
        output: list[dict[str, Any]] = []
        index = 0
        while index < len(normalized):
            timestamp = normalized[index]["timestamp"]
            group: list[dict[str, Any]] = []
            while index < len(normalized) and normalized[index]["timestamp"] == timestamp:
                group.append(normalized[index])
                index += 1
            changed = False
            group_values: dict[str, list[tuple[Decimal, str, str | None, str | None]]] = {}
            conflict_keys: set[str] = set()
            for item in group:
                for raw_key, raw_value in item["fields"].items():
                    key = "im" if raw_key in {"initial_margin", "im"} else "mm" if raw_key in {"maintenance_margin", "mm"} else "denominator"
                    value = _number(raw_value)
                    if value is None:
                        conflict_keys.add(key)
                        continue
                    candidate = (value, item.get("unit"), item.get("currency"), item["source_id"])
                    prior_candidates = group_values.setdefault(key, [])
                    if any(candidate[:3] != prior_candidate[:3] for prior_candidate in prior_candidates):
                        conflict_keys.add(key)
                    prior_candidates.append(candidate)
            for key, candidates in group_values.items():
                if key in conflict_keys:
                    continue
                value, unit, currency, source_id = candidates[-1]
                prior = state.get(key)
                if prior is None or prior[:2] != (value, timestamp) or prior[3:] != (unit, currency):
                    changed = True
                state[key] = (value, timestamp, source_id, unit, currency)
                invalid.discard(key)
            invalid.update(conflict_keys)
            changed = changed or bool(conflict_keys)
            if not changed:
                continue
            im = None if "im" in invalid else state.get("im")
            mm = None if "mm" in invalid else state.get("mm")
            denominator = None if "denominator" in invalid else state.get("denominator")
            ages = [timestamp - fact[1] for fact in (im, mm, denominator) if fact is not None]
            stale = freshness is None or any(age.total_seconds() < 0 or age.total_seconds() > freshness for age in ages)
            currencies = {item[4] for item in (im, mm, denominator) if item is not None and item[4] is not None}
            units = {item[3] for item in (im, mm, denominator) if item is not None and item[3] is not None}
            compatible = len(currencies) <= 1 and len(units) <= 1
            inconsistent = bool(invalid)
            available = not inconsistent and not stale and compatible and im is not None and mm is not None and denominator is not None and denominator[0] > 0
            im_load = im[0] / denominator[0] * Decimal("100") if available else None
            mm_load = mm[0] / denominator[0] * Decimal("100") if available else None
            age = max(ages, default=None)
            output.append({
                "timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
                "status": AVAILABLE if available else INCONSISTENT if inconsistent else UNKNOWN,
                "availability": AVAILABLE if available else INCONSISTENT if inconsistent else UNKNOWN,
                "initial_margin": _canonical_decimal(im[0]) if im else None,
                "maintenance_margin": _canonical_decimal(mm[0]) if mm else None,
                "denominator": _canonical_decimal(denominator[0]) if denominator else None,
                "im_load": _canonical_decimal(im_load),
                "mm_load": _canonical_decimal(mm_load),
                "unit": next((item[3] for item in (im, mm, denominator) if item and item[3] is not None), None),
                "currency": next(iter(currencies)) if len(currencies) == 1 else None,
                "age_seconds": _canonical_decimal(Decimal(str(age.total_seconds()))) if age is not None else None,
                "source_ids": {key: value[2] for key, value in (("im", im), ("mm", mm), ("denominator", denominator)) if value is not None},
                "fact_timestamps": {key: value[1].isoformat().replace("+00:00", "Z") for key, value in (("im", im), ("mm", mm), ("denominator", denominator)) if value is not None},
                "reason": "MARGIN_CONFLICT" if inconsistent else "MARGIN_STALE" if stale else "MARGIN_UNKNOWN" if not available else None,
            })
        return tuple(output)

    def margin_dashboard(self, events: Sequence[Mapping[str, Any]] | None = None, *, now: Any = None) -> dict[str, Any]:
        timeline = self.margin_timeline(events, now=now)
        if not timeline:
            return {"status": UNKNOWN, "availability": UNKNOWN, "reason": "MARGIN_UNKNOWN"}
        result = dict(timeline[-1])
        if now is not None:
            current = _clock_value(None, now)
            freshness = self.settings_value("margin_freshness_seconds", "freshness_seconds", "freshness_limit_seconds")
            timestamps = result.get("fact_timestamps", {})
            ages = [current - timestamp for value in timestamps.values() if (timestamp := _utc(value)) is not None]
            stale = freshness is None or any(age.total_seconds() < 0 or age.total_seconds() > freshness for age in ages)
            if stale and result.get("status") != INCONSISTENT:
                result.update({"status": UNKNOWN, "availability": UNKNOWN, "im_load": None, "mm_load": None, "reason": "MARGIN_STALE", "age_seconds": _canonical_decimal(Decimal(str(max((age.total_seconds() for age in ages), default=0))))})
        return result

    read_margin_model = margin_dashboard
    margin_read_model = margin_dashboard

    def symbol_dashboard(self) -> tuple[dict[str, Any], ...]:
        snapshot = self._latest
        if snapshot is None:
            return ()
        result = self._latest_reconcile_status()
        provenance = self._snapshot_provenance()
        rows: list[dict[str, Any]] = []
        for position in snapshot.get("positions", ()):
            if not isinstance(position, Mapping):
                continue
            item = {"symbol": position.get("symbol"), "side": position.get("side"), "availability": result, "status": result, "provenance": provenance, "attribution": self.attribute_execution(position).__dict__, "facts": {}}
            for output, key in (("size", "size"), ("entry_price", "entry_price"), ("mark_price", "mark_price"), ("leverage", "leverage"), ("unrealized_pnl", "unrealized_pnl")):
                encoded = _decimal_string(position.get(key))
                item[output] = encoded
                item["facts"][output] = {"value": encoded, "availability": AVAILABLE if result == HEALTHY and encoded is not None else UNKNOWN, "provenance": provenance}
            rows.append(item)
        return tuple(rows)

    read_symbol_models = symbol_dashboard
    symbol_read_model = symbol_dashboard

    def _latest_reconcile_status(self) -> str:
        if self._last_result is not None and self.store is None:
            return self._last_result.status if self._last_result.status != HEALTHY else UNKNOWN
        if self._last_result is not None and self._last_result.status in {UNKNOWN, INCONSISTENT}:
            return self._last_result.status
        if self.store is None or self._latest is None:
            return UNKNOWN
        rows = self.store.rows("reconciliations", deployment_id=str(self._latest.get("deployment_id")))
        return str(json.loads(rows[-1]["payload"]).get("status", UNKNOWN)) if rows else UNKNOWN

    def watchdog(self, positions: Sequence[Mapping[str, Any]], *, now: Any = None, previous: WatchdogTransition | None = None) -> WatchdogTransition:
        settings = self.watchdog_settings
        current = _clock_value(None, now) if now is not None else _clock_value(self.clock)
        pair_states: dict[str, tuple[int, bool, bool]] = {}
        exempt_set: set[str] = set()
        for item in positions:
            if not isinstance(item, Mapping) or not isinstance(item.get("symbol"), str) or not item.get("symbol") or not isinstance(item.get("side"), str) or _side(item.get("side")) not in {"LONG", "SHORT"}:
                return WatchdogTransition(UNKNOWN, None, 0, (), None, None, settings.settings_version, UNKNOWN)
            priority = item.get("priority")
            if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
                return WatchdogTransition(UNKNOWN, None, 0, (), None, None, settings.settings_version, UNKNOWN)
            quantity = next((item[key] for key in ("quantity", "qty", "size") if key in item), None)
            confirmed = item.get("confirmed")
            if quantity is None and not isinstance(confirmed, bool):
                return WatchdogTransition(UNKNOWN, None, 0, (), None, None, settings.settings_version, UNKNOWN)
            if confirmed is not None and not isinstance(confirmed, bool):
                return WatchdogTransition(UNKNOWN, None, 0, (), None, None, settings.settings_version, UNKNOWN)
            if quantity is None:
                active = bool(confirmed)
            else:
                number = _number(quantity)
                if number is None or number < 0:
                    return WatchdogTransition(UNKNOWN, None, 0, (), None, None, settings.settings_version, UNKNOWN)
                active = number > 0 and confirmed is not False
            pair_slot = item.get("pair_slot")
            if not isinstance(pair_slot, str) or not pair_slot.strip():
                pair_slot = f'{item["symbol"].strip().upper()}:{_side(item["side"])}'
            else:
                pair_slot = pair_slot.strip()
            counted_flag = item.get("counted", True)
            if not isinstance(counted_flag, bool):
                return WatchdogTransition(UNKNOWN, None, 0, (), None, None, settings.settings_version, UNKNOWN)
            old = pair_states.get(pair_slot)
            if old is not None and (old[0] != priority or old[2] != counted_flag):
                return WatchdogTransition(UNKNOWN, None, 0, (), None, None, settings.settings_version, UNKNOWN)
            pair_states[pair_slot] = (priority, bool(old and old[1]) or active, counted_flag)
            if priority == 0 or not counted_flag:
                exempt_set.add(pair_slot)
        exempt = tuple(sorted(exempt_set))
        counted = sum(1 for priority, active, counted_flag in pair_states.values() if active and priority != 0 and counted_flag)
        if settings.limit is None or settings.grace_seconds is None:
            return WatchdogTransition(UNKNOWN, None, counted, exempt, None, None, settings.settings_version, UNKNOWN)
        if settings.limit == 0:
            state = "DISABLED"
            self._last_watchdog_state = state
            return WatchdogTransition(state, None, counted, exempt, None, current, settings.settings_version)
        prior_state = previous.state if previous is not None else self._last_watchdog_state
        started = previous.started_at if previous is not None and previous.started_at is not None else self._overflow_started
        if counted <= settings.limit:
            self._overflow_started = None
            state = LIMITER_RECOVERED if prior_state in {LIMITER_OVERFLOW, LIMITER_BREACH} else "OK"
            transition = WatchdogTransition(state, state if state == LIMITER_RECOVERED else None, counted, exempt, started, current, settings.settings_version)
            if state == LIMITER_RECOVERED and self.store is not None and self._last_watchdog_state != state:
                self.store.append_watchdog_finding({"deployment_id": self.manifest.deployment_id, "finding_id": f"recovered-{current.isoformat()}", "settings_version": settings.settings_version, "state": state, "interval_start": started.isoformat() if started else None, "interval_end": current.isoformat(), "counted_positions": counted, "exempt_positions": exempt})
            self._last_watchdog_state = state
            return transition
        if started is None:
            started = current
            self._overflow_started = started
        breach = (current - started).total_seconds() >= settings.grace_seconds
        state = LIMITER_BREACH if breach else LIMITER_OVERFLOW
        finding = state
        transition = WatchdogTransition(state, finding, counted, exempt, started, None, settings.settings_version)
        if self.store is not None and self._last_watchdog_state != state:
            finding_id = f"{state.lower()}-{started.isoformat()}"
            self.store.append_watchdog_finding({"deployment_id": self.manifest.deployment_id, "finding_id": finding_id, "settings_version": settings.settings_version, "state": state, "interval_start": started.isoformat(), "counted_positions": counted, "exempt_positions": exempt})
        self._last_watchdog_state = state
        return transition

    evaluate_watchdog = watchdog

    def recommend_resize(self, *, margin_capacity: Any = None, liquidity_capacity: Any = None, now: Any = None, settings: Mapping[str, Any] | None = None, **legacy: Any) -> dict[str, Any]:
        """Compare two explicitly typed capacities in one unit.

        Legacy margin/required arguments are intentionally ignored; a ratio has
        no sizing unit and cannot safely be compared with a liquidity amount.
        """

        config = dict(settings or self._settings)
        version = config.get("settings_version")
        current = _clock_value(None, now) if now is not None else _clock_value(self.clock)
        freshness = config.get("freshness_seconds", config.get("freshness_limit_seconds"))
        def capacity(value: Any) -> tuple[Decimal, str, Mapping[str, Any]] | None:
            if not isinstance(value, Mapping):
                return None
            number = _number(value.get("value"))
            unit = value.get("unit")
            provenance = value.get("provenance")
            observed = _utc(value.get("observed_at", value.get("timestamp_utc")))
            value_version = value.get("settings_version", value.get("version"))
            if number is None or number < 0 or not isinstance(unit, str) or not unit or not isinstance(provenance, (str, Mapping)) or not provenance or not isinstance(value_version, str) or not value_version or observed is None or observed > current:
                return None
            if not isinstance(version, str) or not version or value_version != version:
                return None
            if not isinstance(freshness, int) or isinstance(freshness, bool) or freshness < 0 or (current - observed).total_seconds() > freshness:
                return None
            return number, unit, value
        margin = capacity(margin_capacity)
        liquidity = capacity(liquidity_capacity)
        compatible = margin is not None and liquidity is not None and margin[1] == liquidity[1]
        status = AVAILABLE if compatible else UNKNOWN
        value = format(min(margin[0], liquidity[0]), "f") if compatible and margin and liquidity else None
        return {"status": status, "availability": status, "recommendation": value, "unit": margin[1] if compatible and margin else None, "settings_version": version or "unknown", "provenance": {"source_kind": "fixture_diagnostic", "margin": margin[2] if margin else None, "liquidity": liquidity[2] if liquidity else None, "manifest_id": self.manifest.manifest_id, "manifest_digest": self.manifest.digest}}

    def evaluate_drift(self, expected: Mapping[str, Any], actual: Mapping[str, Any], *, settings: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], ...]:
        config = dict(settings or self._settings)
        version = config.get("settings_version")
        if not isinstance(version, str) or not version:
            return ({"finding": UNKNOWN, "status": UNKNOWN, "reason": "SETTINGS_VERSION_MISSING"},)
        findings: list[dict[str, Any]] = []
        if set(expected.get("symbols", ())) != set(actual.get("symbols", ())):
            findings.append({"finding": "UNEXPECTED_SYMBOL", "expected": expected.get("symbols"), "actual": actual.get("symbols")})
        if expected.get("leverage") != actual.get("leverage"):
            findings.append({"finding": "LEVERAGE_DRIFT", "expected": expected.get("leverage"), "actual": actual.get("leverage")})
        if expected.get("order_size") != actual.get("order_size"):
            findings.append({"finding": "ORDER_SIZE_DRIFT", "expected": expected.get("order_size"), "actual": actual.get("order_size")})
        if expected.get("config_version", expected.get("settings_version")) != actual.get("config_version", actual.get("settings_version")):
            findings.append({"finding": "CONFIG_DRIFT", "expected": expected.get("config_version", expected.get("settings_version")), "actual": actual.get("config_version", actual.get("settings_version"))})
        result = tuple({**item, "status": "DRIFT", "settings_version": version, "provenance": {"source_kind": "fixture_diagnostic", "manifest_id": self.manifest.manifest_id, "manifest_digest": self.manifest.digest}} for item in findings)
        if self.store is not None:
            for item in result:
                self.store.append_watchdog_finding({"deployment_id": self.manifest.deployment_id, "finding_id": str(item["finding"]).lower() + "-" + canonical_digest(item)[:12], "settings_version": version, "state": "DRIFT", **item})
        return result


def reconcile_fixture(manifest: DeploymentManifest | Mapping[str, Any], rest_snapshot: Mapping[str, Any], ws_events: Sequence[Mapping[str, Any]] = (), *, store: LiveStore | None = None, clock: Callable[[], Any] | datetime | None = None, settings: Mapping[str, Any] | None = None, now: Any = None, disconnected: bool = False) -> ReconcileResult:
    return LiveMonitor(manifest, store, clock=clock, settings=settings).reconcile(rest_snapshot, ws_events, now=now, disconnected=disconnected)


def evaluate_drift(manifest: DeploymentManifest | Mapping[str, Any], expected: Mapping[str, Any], actual: Mapping[str, Any], *, store: LiveStore | None = None, settings: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], ...]:
    return LiveMonitor(manifest, store, settings=settings).evaluate_drift(expected, actual, settings=settings)


__all__ = [
    "AVAILABLE", "ATTRIBUTED", "HEALTHY", "INCONSISTENT", "UNKNOWN", "UNATTRIBUTED",
    "CURRENT", "STALE", "IN_POSITION", "PENDING_ENTRY", "NO_POSITION",
    "LIMITER_BREACH", "LIMITER_OVERFLOW", "LIMITER_RECOVERED", "Attribution", "LiveMonitor",
    "ReconcileResult", "WatchdogSettings", "WatchdogTransition", "attribute_execution", "evaluate_drift", "reconcile_fixture",
]
