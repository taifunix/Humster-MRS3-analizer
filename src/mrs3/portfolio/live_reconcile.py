"""Fixture-only REST/WS reconciliation with an explicit atomic apply seam.

The reducer consumes already-collected mappings. It has no exchange client;
the optional apply function persists an authoritative result through the
accepted live-store bundle API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
import json
import sqlite3

from .live_store import DeploymentManifest, LiveStore, LiveStoreConflict, LiveStoreError, WriteOutcome, _reject_secrets, canonical_digest, canonical_json


HEALTHY = "HEALTHY"
PARTIAL = "PARTIAL"
UNKNOWN = "UNKNOWN"
INCONSISTENT = "INCONSISTENT"
REQUIRED_CHANNELS = ("wallet", "positions", "executions", "orders")
OPTIONAL_CHANNELS = ("cashflows",)
ALL_CHANNELS = REQUIRED_CHANNELS + OPTIONAL_CHANNELS
_CHANNEL_ALIASES = {
    "wallet": "wallet", "position": "positions", "positions": "positions",
    "execution": "executions", "executions": "executions",
    "order": "orders", "orders": "orders", "cashflow": "cashflows", "cashflows": "cashflows",
}
_OPEN_ORDER_LIMIT = Decimal("5.000")


class LiveReconcileError(ValueError):
    """A malformed fixture at the reducer boundary."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


def _canonical_value(value: Any) -> Any:
    # canonical_json is the accepted live-store boundary for Decimal strings,
    # recursive float rejection, and supported values.
    return json.loads(canonical_json(value))


def _uniq(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool) and bool(str(value).strip())


def _channel(value: Any) -> str | None:
    return _CHANNEL_ALIASES.get(value.strip().casefold()) if isinstance(value, str) else None


def _sequence(event: Mapping[str, Any]) -> int | None:
    value = event.get("source_sequence", event.get("sequence", event.get("seq")))
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _event_identity(event: Mapping[str, Any]) -> Any:
    return event.get("event_id", event.get("id", event.get("execution_id", event.get("order_id", event.get("record_id", event.get("sequence", event.get("seq")))))))


def _record_identity(channel: str, record: Mapping[str, Any]) -> tuple[Any, ...]:
    if channel == "wallet":
        explicit = record.get("record_id", record.get("id"))
        if explicit is not None:
            return (channel, explicit)
        return (channel, record.get("effective_at_utc", record.get("effective_at", record.get("observed_at_utc", record.get("observed_at")))), record.get("source_id"), record.get("source_sequence", record.get("sequence")))
    if channel == "positions":
        return (channel, record.get("symbol"), str(record.get("side", "")).upper())
    if channel == "orders":
        return (channel, record.get("order_id", record.get("id")), record.get("revision", 0))
    if channel == "executions":
        return (channel, record.get("execution_id", record.get("id")))
    return (channel, record.get("cashflow_id", record.get("id")))


def _source_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    effective = record.get("effective_at_utc", record.get("effective_at", ""))
    observed = record.get("observed_at_utc", record.get("observed_at", ""))
    kind = record.get("source_kind", "")
    source_id = record.get("source_id", "")
    sequence = record.get("source_sequence", record.get("sequence", 0))
    if not all(isinstance(item, str) for item in (effective, observed, kind, source_id)) or not isinstance(sequence, int) or isinstance(sequence, bool):
        return (1, canonical_digest(record))
    return (0, effective, observed, kind, source_id, sequence, canonical_digest(record))


def _page_items(channel: str, page: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...] | None:
    value = page.get("items", page.get("records", page.get(channel, ())))
    if channel == "wallet" and isinstance(value, Mapping):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        return None
    return tuple(value) if all(isinstance(item, Mapping) for item in value) else None


def _field(mapping: Mapping[str, Any], *names: str) -> tuple[bool, Any]:
    for name in names:
        if name in mapping:
            return True, mapping[name]
    return False, None


def _page_reason(channel: str, reason: str) -> str:
    return f"{channel.upper()}_{reason}"


def _utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _elapsed_seconds(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    return Decimal(delta.days * 86400 + delta.seconds) + (Decimal(delta.microseconds) / Decimal(1_000_000))


def _timestamp_text(value: datetime | None, original: Any) -> Any:
    return value.isoformat().replace("+00:00", "Z") if value is not None else original


def _channel_pages(source: Any) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], dict[str, Any], tuple[str, ...]]:
    groups: dict[str, list[Mapping[str, Any]]] = {name: [] for name in ALL_CHANNELS}
    metadata: dict[str, Any] = {}
    reasons: list[str] = []
    if isinstance(source, Mapping):
        metadata = {key: value for key, value in source.items() if _channel(key) is None and key != "pages"}
        if isinstance(source.get("pages"), (list, tuple)):
            if any(_channel(key) is not None for key in source):
                reasons.append("REST_PAGES_AMBIGUOUS")
            raw_pages: Any = source["pages"]
        else:
            raw_pages = None
            for raw_name, value in source.items():
                name = _channel(raw_name)
                if name is None:
                    continue
                if isinstance(value, Mapping) and isinstance(value.get("pages"), (list, tuple)):
                    metadata[name] = {key: child for key, child in value.items() if key != "pages"}
                    value = value["pages"]
                elif isinstance(value, Mapping):
                    value = (value,)
                if isinstance(value, (list, tuple)):
                    for page in value:
                        if isinstance(page, Mapping):
                            groups[name].append(page)
                        else:
                            reasons.append("REST_PAGE_INVALID")
                else:
                    reasons.append(f"{name.upper()}_PAGES_INVALID")
    elif isinstance(source, (list, tuple)):
        raw_pages = source
    else:
        raw_pages = None
        reasons.append("REST_PAGES_INVALID")
    if raw_pages is not None:
        for page in raw_pages:
            name = _channel(page.get("channel")) if isinstance(page, Mapping) else None
            if name is None:
                reasons.append("REST_PAGE_INVALID")
            else:
                groups[name].append(page)
    return {key: tuple(value) for key, value in groups.items()}, metadata, _uniq(reasons)


def _declared_for(metadata: Mapping[str, Any], channel: str, *names: str) -> Any:
    for name in names:
        value = metadata.get(name)
        if isinstance(value, Mapping):
            if channel in value:
                return value[channel]
            for alias, canonical in _CHANNEL_ALIASES.items():
                if canonical == channel and alias in value:
                    return value[alias]
        elif value is not None and channel == "all":
            return value
    channel_meta = metadata.get(channel)
    if isinstance(channel_meta, Mapping):
        for name in names:
            if name in channel_meta:
                return channel_meta[name]
    return None


def _records_digest(records: Sequence[Mapping[str, Any]]) -> str:
    return canonical_digest([dict(record) for record in sorted(records, key=_source_key)])


def _validate_pages(channel: str, pages: Sequence[Mapping[str, Any]]) -> tuple[tuple[Mapping[str, Any], ...], dict[str, Any], tuple[str, ...]]:
    reasons: list[str] = []
    records: list[Mapping[str, Any]] = []
    identities: dict[str, str] = {}
    previous_next: Any = None
    terminal_cursor: Any = None
    terminal_seen = False
    page_declarations_seen = False
    page_declarations_complete = bool(pages)
    for index, page in enumerate(pages):
        items = _page_items(channel, page)
        if items is None:
            reasons.append(_page_reason(channel, "REST_PAGE_ITEMS_INVALID"))
            items = ()
        has_request, request = _field(page, "request_cursor", "page_cursor", "cursor", "previous_cursor", "prev_cursor")
        has_previous, previous = _field(page, "previous_cursor", "prev_cursor")
        has_next, next_cursor = _field(page, "next_cursor", "next", "nextPageCursor")
        has_terminal, terminal = _field(page, "terminal", "is_terminal", "is_last", "done")
        if not has_terminal or not isinstance(terminal, bool):
            reasons.append(_page_reason(channel, "REST_TERMINAL_MARKER_MISSING" if not has_terminal else "REST_TERMINAL_MARKER_INVALID"))
        if index == 0:
            if (has_request and request not in (None, "")) or (has_previous and previous not in (None, "")):
                reasons.append(_page_reason(channel, "REST_CURSOR_START_INVALID"))
        else:
            if not (has_request or has_previous):
                reasons.append(_page_reason(channel, "REST_CURSOR_DISCONTINUITY"))
            elif (has_request and (not isinstance(request, str) or not request.strip() or request != previous_next)) or (has_previous and (not isinstance(previous, str) or not previous.strip() or previous != previous_next)):
                reasons.append(_page_reason(channel, "REST_CURSOR_DISCONTINUITY"))
        if terminal_seen:
            reasons.append(_page_reason(channel, "REST_PAGE_AFTER_TERMINAL"))
        if page.get("truncated") is True or page.get("partial") is True:
            reasons.append(_page_reason(channel, "REST_TRUNCATED"))
        if page.get("stale") is True:
            reasons.append(_page_reason(channel, "REST_SNAPSHOT_STALE"))
        if has_terminal and terminal:
            terminal_seen = True
            explicit, candidate = _field(page, "terminal_cursor", "terminalCursor")
            if explicit:
                terminal_cursor = candidate
            else:
                reasons.append(_page_reason(channel, "REST_TERMINAL_CURSOR_MISSING"))
            if not isinstance(terminal_cursor, str) or not terminal_cursor.strip():
                reasons.append(_page_reason(channel, "REST_TERMINAL_CURSOR_MISSING"))
        elif not has_next or not isinstance(next_cursor, str) or not next_cursor.strip():
            reasons.append(_page_reason(channel, "REST_CURSOR_NEXT_MISSING"))
        previous_next = next_cursor if has_next else None
        page_count_present, page_count = _field(page, "declared_count", "declared_record_count", "record_count", "count")
        page_digest_present, page_digest = _field(page, "canonical_digest", "canonical_records_digest", "records_digest", "digest")
        if page_count_present or page_digest_present:
            page_declarations_seen = True
            if not page_count_present:
                page_declarations_complete = False
                reasons.append(_page_reason(channel, "REST_PAGE_DECLARED_COUNT_MISSING"))
            elif not isinstance(page_count, int) or isinstance(page_count, bool) or page_count != len(items):
                page_declarations_complete = False
                reasons.append(_page_reason(channel, "REST_PAGE_DECLARED_COUNT_MISMATCH"))
            if not page_digest_present:
                page_declarations_complete = False
                reasons.append(_page_reason(channel, "REST_PAGE_DIGEST_MISSING"))
            elif not isinstance(page_digest, str) or page_digest != _records_digest(items):
                page_declarations_complete = False
                reasons.append(_page_reason(channel, "REST_PAGE_DIGEST_MISMATCH"))
        else:
            page_declarations_complete = False
        for item in items:
            identity = _record_identity(channel, item)
            if any(part is None or (isinstance(part, str) and not part.strip()) for part in identity[1:]):
                reasons.append(f"{channel.upper()}_IDENTITY_INVALID")
            identity_key = canonical_json(identity)
            digest = canonical_digest(item)
            if identity_key in identities:
                reasons.append(_page_reason(channel, "REST_PAGE_OVERLAP" if identities[identity_key] == digest else "REST_PAGE_CONFLICT"))
            else:
                identities[identity_key] = digest
                records.append(item)
    if not pages:
        reasons.append(_page_reason(channel, "REST_CHANNEL_MISSING"))
    elif not terminal_seen:
        reasons.append(_page_reason(channel, "REST_TERMINAL_MARKER_MISSING"))
    return tuple(sorted(records, key=_source_key)), {"terminal_cursor": terminal_cursor, "page_count": len(pages), "page_declarations_seen": page_declarations_seen, "page_declarations_complete": page_declarations_complete}, _uniq(reasons)


def _snapshot_digest(sections: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    return canonical_digest({channel: [dict(item) for item in sections[channel]] for channel in ALL_CHANNELS})


@dataclass(frozen=True)
class RestSnapshotResult:
    status: str
    reasons: tuple[str, ...]
    snapshot: Mapping[str, Any] | None = None
    checkpoint: Mapping[str, Any] | None = None
    sections: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None
    authoritative: bool = False
    provenance: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    reasons: tuple[str, ...]
    snapshot: Mapping[str, Any] | None = None
    checkpoint: Mapping[str, Any] | None = None
    applied_ws: tuple[Mapping[str, Any], ...] = ()
    checkpoint_candidates: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
    provenance: Mapping[str, Any] = MappingProxyType({})
    authoritative: bool = False

    @property
    def ws_events(self) -> tuple[Mapping[str, Any], ...]:
        return self.applied_ws


@dataclass(frozen=True)
class ReconcileApplication:
    """Immutable disposition returned by the optional store application seam."""

    status: str
    reasons: tuple[str, ...] = ()
    outcomes: tuple[WriteOutcome, ...] = ()
    checkpoint_advanced: bool = False
    reconcile_id: str | None = None
    reconcile_digest: str | None = None


def complete_rest_snapshot(
    manifest: DeploymentManifest | Mapping[str, Any],
    pages: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    mode: str = "startup",
    snapshot_id: str | None = None,
    deployment_id: str | None = None,
    account_id: str | None = None,
    observed_at: str | None = None,
    duration_seconds: Any = None,
    requested_start_at: str | None = None,
    requested_end_at: str | None = None,
) -> RestSnapshotResult:
    """Validate all required REST pages and return canonical immutable facts."""

    item = manifest if isinstance(manifest, DeploymentManifest) else DeploymentManifest(manifest)
    _reject_secrets(pages, "rest_fixture")
    pages = _canonical_value(pages)
    groups, metadata, grouping_reasons = _channel_pages(pages)
    reasons = list(grouping_reasons)
    mode = mode.casefold() if isinstance(mode, str) else ""
    if mode not in {"startup", "reconnect", "periodic"}:
        reasons.append("RECONCILE_MODE_INVALID")
    deadline = _as_decimal(item.payload.get("snapshot_read_deadline_seconds"))
    if deadline is None:
        reasons.append("SNAPSHOT_DEADLINE_UNPINNED")
    page_durations = [
        value for group in groups.values() for page in group
        if (value := _as_decimal(page.get("duration_seconds", page.get("elapsed_seconds")))) is not None
    ]
    supplied_duration = duration_seconds if duration_seconds is not None else metadata.get("duration_seconds", metadata.get("elapsed_seconds"))
    if supplied_duration is None and page_durations:
        supplied_duration = sum(page_durations, Decimal("0"))
    duration = _as_decimal(supplied_duration)
    if supplied_duration is None:
        reasons.append("SNAPSHOT_DURATION_MISSING")
    elif duration is None:
        reasons.append("SNAPSHOT_DURATION_INVALID")
    elif deadline is not None and duration > deadline:
        reasons.append("SNAPSHOT_DEADLINE_EXCEEDED")
    order_duration_raw = metadata.get("open_orders_duration_seconds", metadata.get("open_order_duration_seconds", metadata.get("orders_duration_seconds")))
    if order_duration_raw is None and isinstance(metadata.get("orders"), Mapping):
        order_duration_raw = metadata["orders"].get("open_orders_duration_seconds", metadata["orders"].get("duration_seconds", metadata["orders"].get("elapsed_seconds")))
    if order_duration_raw is None:
        order_page_durations = [
            value for page in groups["orders"]
            if (value := _as_decimal(page.get("duration_seconds", page.get("elapsed_seconds")))) is not None
        ]
        if order_page_durations:
            order_duration_raw = sum(order_page_durations, Decimal("0"))
    order_duration = _as_decimal(order_duration_raw)
    if metadata.get("stale") is True or metadata.get("snapshot_stale") is True:
        reasons.append("REST_SNAPSHOT_STALE")
    if order_duration_raw is None:
        reasons.append("OPEN_ORDER_SNAPSHOT_DURATION_MISSING")
    elif order_duration is None:
        reasons.append("OPEN_ORDER_SNAPSHOT_DURATION_INVALID")
    elif order_duration is not None and order_duration > _OPEN_ORDER_LIMIT:
        reasons.append("OPEN_ORDER_SNAPSHOT_DEADLINE_EXCEEDED")

    sections: dict[str, tuple[Mapping[str, Any], ...]] = {}
    terminal: dict[str, Any] = {}
    channel_provenance: dict[str, Any] = {}
    global_declared_count = metadata.get("snapshot_declared_count", metadata.get("declared_snapshot_count", metadata.get("declared_count", metadata.get("declared_record_count", metadata.get("record_count")))))
    global_declared_digest = metadata.get("snapshot_canonical_digest", metadata.get("snapshot_digest"))
    if global_declared_digest is None and isinstance(metadata.get("canonical_digest"), str):
        global_declared_digest = metadata["canonical_digest"]
    if not isinstance(global_declared_count, int) or isinstance(global_declared_count, bool):
        global_declared_count = None
    if not isinstance(global_declared_digest, str):
        global_declared_digest = None
    if global_declared_count is None:
        reasons.append("SNAPSHOT_DECLARED_COUNT_MISSING")
    if global_declared_digest is None:
        reasons.append("SNAPSHOT_DIGEST_MISSING")
    for channel in ALL_CHANNELS:
        if channel == "cashflows" and not groups[channel]:
            sections[channel] = ()
            unsupported_channels = item.payload.get("unsupported_channels", ())
            if isinstance(unsupported_channels, str):
                unsupported_channels = (unsupported_channels,)
            elif not isinstance(unsupported_channels, (list, tuple, set, frozenset)):
                unsupported_channels = ()
            unsupported = (
                item.payload.get("cashflows_supported") is False or
                item.payload.get("cashflow_channel_supported") is False or
                "cashflows" in tuple(unsupported_channels)
            )
            if not unsupported:
                reasons.append("CASHFLOWS_REST_CHANNEL_MISSING")
            channel_provenance[channel] = {
                "record_count": 0, "canonical_digest": _records_digest(()),
                "declared_count": 0 if unsupported else None, "declared_digest": _records_digest(()) if unsupported else None,
                "availability": "UNSUPPORTED" if unsupported else UNKNOWN,
            }
            continue
        records, evidence, page_reasons = _validate_pages(channel, groups[channel])
        sections[channel] = records
        terminal[channel] = evidence["terminal_cursor"]
        reasons.extend(page_reasons)
        declared_count = _declared_for(metadata, channel, "declared_count", "declared_record_count", "record_count", "counts")
        declared_digest = _declared_for(metadata, channel, "canonical_digest", "canonical_records_digest", "records_digest", "digest", "digests")
        channel_count_declared = isinstance(declared_count, int) and not isinstance(declared_count, bool)
        if not channel_count_declared and not evidence["page_declarations_complete"]:
            reasons.append(f"{channel.upper()}_DECLARED_COUNT_MISSING")
        elif channel_count_declared and declared_count != len(records):
            reasons.append(f"{channel.upper()}_DECLARED_COUNT_MISMATCH")
        expected_digest = _records_digest(records)
        channel_digest_declared = isinstance(declared_digest, str) and bool(declared_digest)
        if not channel_digest_declared and not evidence["page_declarations_complete"]:
            reasons.append(f"{channel.upper()}_DIGEST_MISSING")
        if not channel_count_declared and evidence["page_declarations_complete"]:
            declared_count = len(records)
        if not channel_digest_declared and evidence["page_declarations_complete"]:
            declared_digest = expected_digest
        elif channel_digest_declared and declared_digest != expected_digest:
            reasons.append(f"{channel.upper()}_DIGEST_MISMATCH")
        channel_provenance[channel] = {"record_count": len(records), "canonical_digest": expected_digest, "declared_count": declared_count, "declared_digest": declared_digest}

    start = metadata.get("snapshot_start_observed_at", metadata.get("start_observed_at", metadata.get("snapshot_start", metadata.get("started_at"))))
    end = metadata.get("snapshot_end_observed_at", metadata.get("end_observed_at", metadata.get("snapshot_end", metadata.get("completed_at", observed_at))))
    channel_meta = [metadata[channel] for channel in REQUIRED_CHANNELS if isinstance(metadata.get(channel), Mapping)]
    if start is None and channel_meta:
        starts = {meta.get("snapshot_start_observed_at", meta.get("start_observed_at")) for meta in channel_meta}
        if len(starts) == 1:
            start = starts.pop()
    if end is None and channel_meta:
        ends = {meta.get("snapshot_end_observed_at", meta.get("end_observed_at")) for meta in channel_meta}
        if len(ends) == 1:
            end = ends.pop()
    start_utc = _utc_timestamp(start)
    end_utc = _utc_timestamp(end)
    if start is None or start == "":
        reasons.append("SNAPSHOT_START_OBSERVED_AT_MISSING")
    elif not isinstance(start, str) or start_utc is None:
        reasons.append("SNAPSHOT_START_OBSERVED_AT_INVALID")
    if end is None or end == "":
        reasons.append("SNAPSHOT_END_OBSERVED_AT_MISSING")
    elif not isinstance(end, str) or end_utc is None:
        reasons.append("SNAPSHOT_END_OBSERVED_AT_INVALID")
    if start_utc is not None and end_utc is not None:
        if end_utc < start_utc:
            reasons.append("SNAPSHOT_OBSERVATION_WINDOW_INVALID")
        else:
            elapsed = _elapsed_seconds(start_utc, end_utc)
            if duration is not None and elapsed > duration:
                reasons.append("SNAPSHOT_WINDOW_EXCEEDS_DURATION")
            if deadline is not None and elapsed > deadline:
                reasons.append("SNAPSHOT_WINDOW_EXCEEDS_DEADLINE")
    requested_start = _utc_timestamp(requested_start_at) if requested_start_at is not None else None
    requested_end = _utc_timestamp(requested_end_at) if requested_end_at is not None else None
    if requested_start_at is not None and (start_utc is None or requested_start is None or start_utc != requested_start):
        reasons.append("SNAPSHOT_START_BOUNDARY_MISMATCH")
    if requested_end_at is not None and (end_utc is None or requested_end is None or end_utc != requested_end):
        reasons.append("SNAPSHOT_END_BOUNDARY_MISMATCH")
    start = _timestamp_text(start_utc, start)
    end = _timestamp_text(end_utc, end)
    actual_deployment = deployment_id if deployment_id is not None else metadata.get("deployment_id", item.deployment_id)
    actual_account = account_id if account_id is not None else metadata.get("account_id", item.account_id)
    actual_snapshot = snapshot_id if snapshot_id is not None else metadata.get("snapshot_id")
    if actual_deployment != item.deployment_id:
        reasons.append("DEPLOYMENT_ID_MISMATCH")
    if actual_account != item.account_id:
        reasons.append("ACCOUNT_ID_MISMATCH")
    if not _id(actual_snapshot):
        reasons.append("SNAPSHOT_ID_INVALID")
    wallet = sections["wallet"]
    if len(wallet) != 1:
        reasons.append("WALLET_PAGE_CARDINALITY_INVALID")
    watermarks = metadata.get("watermarks", metadata.get("stream_watermarks"))
    if not isinstance(watermarks, Mapping):
        reasons.append("WATERMARK_MISSING")
        watermarks = {}
    normalized_watermarks: dict[str, int] = {}
    for key, value in watermarks.items():
        channel = _channel(key)
        if channel is None or not isinstance(value, int) or isinstance(value, bool) or value < 0:
            reasons.append(f"{channel.upper()}_WATERMARK_INVALID" if channel is not None else "WATERMARK_INVALID")
        else:
            normalized_watermarks[channel] = value
    for channel in REQUIRED_CHANNELS:
        if channel not in normalized_watermarks:
            reasons.append(f"{channel.upper()}_WATERMARK_MISSING")

    expected_snapshot_digest = _snapshot_digest(sections)
    declared_snapshot_digest = global_declared_digest
    if isinstance(global_declared_count, int) and global_declared_count != sum(len(sections[channel]) for channel in ALL_CHANNELS):
        reasons.append("SNAPSHOT_DECLARED_COUNT_MISMATCH")
    if isinstance(declared_snapshot_digest, str) and declared_snapshot_digest != expected_snapshot_digest:
        reasons.append("SNAPSHOT_DIGEST_MISMATCH")
    snapshot = {
        "deployment_id": actual_deployment, "account_id": actual_account, "snapshot_id": actual_snapshot,
        "observed_at": end, "wallet": wallet[0] if wallet else {}, "positions": sections["positions"],
        "orders": sections["orders"], "executions": sections["executions"], "cashflows": sections["cashflows"],
        "watermarks": normalized_watermarks, "snapshot_start_observed_at": start, "snapshot_end_observed_at": end,
        "terminal_cursor": terminal, "record_count": sum(len(sections[channel]) for channel in ALL_CHANNELS),
        "canonical_digest": expected_snapshot_digest,
    }
    checkpoint = {
        "mode": mode, "snapshot_id": actual_snapshot, "snapshot_start_observed_at": start,
        "snapshot_end_observed_at": end, "terminal_cursor": terminal, "record_count": snapshot["record_count"],
        "canonical_digest": expected_snapshot_digest, "watermarks": normalized_watermarks,
    }
    unique = _uniq(reasons)
    status = INCONSISTENT if any(reason.endswith("REST_PAGE_CONFLICT") for reason in unique) else (HEALTHY if not unique else PARTIAL)
    provenance = _freeze({"rest": {
        "snapshot_start_observed_at": start, "snapshot_end_observed_at": end,
        "record_count": snapshot["record_count"], "canonical_digest": expected_snapshot_digest,
        "channels": channel_provenance,
    }})
    return RestSnapshotResult(status, unique, _freeze(snapshot), _freeze(checkpoint), _freeze(sections), status == HEALTHY, provenance)


def _buffer_ws(events: Sequence[Mapping[str, Any]], watermarks: Mapping[str, int]) -> tuple[tuple[Mapping[str, Any], ...], dict[str, int], tuple[str, ...], Mapping[str, Any]]:
    reasons: list[str] = []
    by_channel: dict[str, dict[int, Mapping[str, Any]]] = {}
    identities: dict[tuple[str, Any], tuple[int, str]] = {}
    invalid: set[str] = set()
    for raw in events:
        if not isinstance(raw, Mapping):
            reasons.append("WS_EVENT_INVALID")
            continue
        event = _canonical_value(raw)
        channel = _channel(event.get("channel", event.get("kind")))
        sequence = _sequence(event)
        identity = _event_identity(event)
        if channel is None or sequence is None or not _id(identity):
            reasons.append("WS_EVENT_IDENTITY_INVALID")
            continue
        payload_digest = event.get("canonical_payload_digest", event.get("payload_digest"))
        if isinstance(payload_digest, str) and payload_digest:
            digest = payload_digest
        else:
            digest = canonical_digest({key: value for key, value in event.items()
                                       if key not in {"sequence", "seq", "source_sequence"}})
        prior_identity = identities.get((channel, identity))
        if prior_identity is not None:
            if prior_identity[1] != digest:
                reasons.append("WS_CONFLICTING_DUPLICATE")
                invalid.add(channel)
            # The same immutable event may be replayed with a different
            # transport sequence.  It is already represented by its first
            # canonical identity and must not be applied twice.
            continue
        identities[(channel, identity)] = (sequence, digest)
        prior = by_channel.setdefault(channel, {}).get(sequence)
        if prior is not None:
            if canonical_digest(prior) != digest or _event_identity(prior) != identity:
                reasons.append("WS_CONFLICTING_DUPLICATE")
                invalid.add(channel)
            continue
        by_channel[channel][sequence] = event
    applied: list[Mapping[str, Any]] = []
    candidates = dict(watermarks)
    provenance: dict[str, Any] = {}
    for channel in sorted(by_channel):
        if channel not in watermarks:
            reasons.append(f"{channel.upper()}_WATERMARK_MISSING")
            invalid.add(channel)
            provenance[channel] = {
                "buffered_count": len(by_channel[channel]), "applied_count": 0,
                "applied_digest": canonical_digest([]), "status": "UNKNOWN",
                "first_observed_at": None, "last_observed_at": None,
            }
            continue
        base = watermarks[channel]
        expected = base + 1
        selected: list[Mapping[str, Any]] = []
        for sequence in sorted(by_channel[channel]):
            if sequence <= base:
                continue
            if sequence != expected:
                reasons.append("WS_SEQUENCE_GAP")
                invalid.add(channel)
                break
            selected.append(by_channel[channel][sequence])
            expected += 1
        if channel not in invalid:
            candidates[channel] = expected - 1
            applied.extend(selected)
        else:
            candidates[channel] = base
        provenance[channel] = {
            "buffered_count": len(by_channel[channel]), "applied_count": len(selected) if channel not in invalid else 0,
            "applied_digest": canonical_digest(selected), "status": "INCONSISTENT" if channel in invalid else "CONTIGUOUS",
            "first_observed_at": next((event.get("observed_at_utc", event.get("observed_at")) for event in selected), None),
            "last_observed_at": next((event.get("observed_at_utc", event.get("observed_at")) for event in reversed(selected)), None),
        }
    for channel in REQUIRED_CHANNELS:
        provenance.setdefault(channel, {
            "buffered_count": 0,
            "applied_count": 0,
            "applied_digest": canonical_digest([]),
            "status": "CONTIGUOUS",
            "first_observed_at": None,
            "last_observed_at": None,
        })
    applied.sort(key=lambda event: (_channel(event.get("channel", event.get("kind"))) or "", _sequence(event) or 0, canonical_digest(event)))
    return tuple(_freeze(event) for event in applied), candidates, _uniq(reasons), _freeze(provenance)


class LiveReconciler:
    """Reusable pure reducer.  ``store`` is accepted but never touched."""

    def __init__(self, manifest: DeploymentManifest | Mapping[str, Any], store: Any = None) -> None:
        self.manifest = manifest if isinstance(manifest, DeploymentManifest) else DeploymentManifest(manifest)
        self.store = store

    def reconcile(self, pages: Mapping[str, Any] | Sequence[Mapping[str, Any]], ws_events: Sequence[Mapping[str, Any]] = (), *, mode: str = "startup", snapshot_id: str | None = None, deployment_id: str | None = None, account_id: str | None = None, observed_at: str | None = None, duration_seconds: Any = None, requested_start_at: str | None = None, requested_end_at: str | None = None) -> ReconcileResult:
        rest = complete_rest_snapshot(self.manifest, pages, mode=mode, snapshot_id=snapshot_id, deployment_id=deployment_id, account_id=account_id, observed_at=observed_at, duration_seconds=duration_seconds, requested_start_at=requested_start_at, requested_end_at=requested_end_at)
        if not isinstance(ws_events, (list, tuple)):
            raise LiveReconcileError("WS events must be a sequence")
        _reject_secrets(ws_events, "ws_fixture")
        applied, candidates, ws_reasons, ws_provenance = _buffer_ws(ws_events, rest.checkpoint.get("watermarks", {}) if rest.checkpoint else {})
        reasons = _uniq((*rest.reasons, *ws_reasons))
        rest_conflict = any(reason.endswith("REST_PAGE_CONFLICT") for reason in reasons)
        ws_conflict = any(reason in {"WS_SEQUENCE_GAP", "WS_CONFLICTING_DUPLICATE"} for reason in reasons)
        status = INCONSISTENT if rest_conflict or ws_conflict else (rest.status if rest.status != HEALTHY else (HEALTHY if not reasons else UNKNOWN))
        snapshot, checkpoint = rest.snapshot, rest.checkpoint
        rest_channels = rest.provenance.get("rest", {}).get("channels", {}) if rest.provenance else {}
        candidate_rows = {channel: {
            "channel": channel, "sequence": sequence, "snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
            "terminal_cursor": checkpoint.get("terminal_cursor", {}).get(channel) if checkpoint else None,
            "snapshot_start_observed_at": checkpoint.get("snapshot_start_observed_at") if checkpoint else None,
            "snapshot_end_observed_at": checkpoint.get("snapshot_end_observed_at") if checkpoint else None,
            "record_count": snapshot.get("record_count") if snapshot else None,
            "canonical_digest": checkpoint.get("canonical_digest") if checkpoint else None,
            "channel_canonical_digest": rest_channels.get(channel, {}).get("canonical_digest") if isinstance(rest_channels, Mapping) else None,
        } for channel, sequence in sorted(candidates.items())}
        provenance = _freeze({"rest": rest.provenance.get("rest", {}) if rest.provenance else {}, "ws": ws_provenance})
        return ReconcileResult(status, reasons, snapshot, checkpoint, applied, _freeze(candidate_rows), provenance, status == HEALTHY)

    def apply(self, store: LiveStore, result: ReconcileResult) -> ReconcileApplication:
        return apply_reconcile(store, result)

    reconcile_fixture = reconcile


def _reconcile_identity(result: ReconcileResult) -> tuple[str, str]:
    snapshot = result.snapshot or {}
    checkpoint_rows = [dict(row) for _, row in sorted(result.checkpoint_candidates.items())]
    evidence = {
        "deployment_id": snapshot.get("deployment_id"),
        "account_id": snapshot.get("account_id"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_digest": snapshot.get("canonical_digest"),
        "events": [dict(event) for event in result.applied_ws],
        "checkpoints": checkpoint_rows,
    }
    digest = canonical_digest(evidence)
    return f"reconcile-{digest}", digest


def _checkpoint_payloads(result: ReconcileResult) -> tuple[Mapping[str, Any], ...]:
    snapshot = result.snapshot or {}
    observed_at = snapshot.get("snapshot_end_observed_at") or snapshot.get("observed_at")
    snapshot_id = snapshot.get("snapshot_id")
    rows: list[Mapping[str, Any]] = []
    for channel, candidate in sorted(result.checkpoint_candidates.items()):
        sequence = candidate.get("sequence")
        if not isinstance(channel, str) or not channel or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("checkpoint candidate identity is incomplete")
        rows.append({
            **dict(candidate),
            "channel": channel,
            "sequence": sequence,
            "snapshot_id": snapshot_id,
            "observed_at": observed_at,
            "effective_at": observed_at,
            "source_kind": "SYSTEM",
            "source_id": f"{snapshot_id}:{channel}:{sequence}",
            "source_sequence": sequence,
        })
    if not rows:
        raise ValueError("checkpoint candidates are missing")
    return tuple(rows)


def apply_reconcile(store: LiveStore, result: ReconcileResult) -> ReconcileApplication:
    """Atomically apply one healthy reducer result to the accepted live store."""

    if not isinstance(store, LiveStore):
        raise TypeError("store must be a LiveStore")
    if not isinstance(result, ReconcileResult):
        raise TypeError("result must be a ReconcileResult")
    if result.status != HEALTHY or not result.authoritative:
        return ReconcileApplication(
            result.status,
            _uniq((*result.reasons, "NOT_APPLIED_NON_AUTHORITATIVE")),
        )
    if not isinstance(result.snapshot, Mapping):
        return ReconcileApplication(INCONSISTENT, ("STORE_INPUT_INVALID",))
    snapshot = result.snapshot
    deployment_id: str | None = None
    snapshot_id: str | None = None
    observed_at: str | None = None
    reconcile_id: str | None = None
    reconcile_digest: str | None = None
    checkpoints: tuple[Mapping[str, Any], ...] = ()
    outcomes: tuple[WriteOutcome, ...] = ()
    try:
        deployment_id = snapshot["deployment_id"]
        snapshot_id = snapshot["snapshot_id"]
        observed_at = snapshot["observed_at"]
        if not all(isinstance(value, str) and value.strip() for value in (deployment_id, snapshot_id, observed_at)):
            raise ValueError("REST snapshot identity is incomplete")
        reconcile_id, reconcile_digest = _reconcile_identity(result)
        checkpoints = _checkpoint_payloads(result)
        reconciliation = {
            "reconcile_id": reconcile_id,
            "status": HEALTHY,
            "reasons": (),
            "mode": (result.checkpoint or {}).get("mode", ""),
            "snapshot_id": snapshot_id,
            "observed_at": observed_at,
            "effective_at": observed_at,
            "source_kind": "SYSTEM",
            "source_id": reconcile_id,
            "source_sequence": 0,
            "canonical_digest": snapshot.get("canonical_digest"),
            "reconcile_digest": reconcile_digest,
        }
        outcomes = tuple(store.append_reconcile_bundle(
            snapshot,
            events=tuple(result.applied_ws),
            reconciliation=reconciliation,
            checkpoints=checkpoints,
            rollback_on_conflict=True,
        ))
        if any(outcome in {WriteOutcome.CONFLICT, WriteOutcome.INCONSISTENT} for outcome in outcomes):
            return ReconcileApplication(INCONSISTENT, ("STORE_CONFLICT",), outcomes, False, reconcile_id, reconcile_digest)
    except LiveStoreConflict as error:
        return ReconcileApplication(INCONSISTENT, ("STORE_CONFLICT",), error.outcomes, False, reconcile_id, reconcile_digest)
    except (LiveStoreError, sqlite3.Error):
        return ReconcileApplication(INCONSISTENT, ("STORE_APPLY_FAILED",), (), False, reconcile_id, reconcile_digest)
    checkpoint_advanced = all(
        (latest := store.latest_checkpoint(deployment_id, row["channel"])) is not None
        and latest.get("sequence", -1) >= row["sequence"]
        for row in checkpoints
    )
    return ReconcileApplication(HEALTHY, (), outcomes, checkpoint_advanced, reconcile_id, reconcile_digest)


def reconcile_fixture(manifest: DeploymentManifest | Mapping[str, Any], pages: Mapping[str, Any] | Sequence[Mapping[str, Any]], ws_events: Sequence[Mapping[str, Any]] = (), *, store: Any = None, mode: str = "startup", snapshot_id: str | None = None, deployment_id: str | None = None, account_id: str | None = None, observed_at: str | None = None, duration_seconds: Any = None, requested_start_at: str | None = None, requested_end_at: str | None = None) -> ReconcileResult:
    return LiveReconciler(manifest, store).reconcile(pages, ws_events, mode=mode, snapshot_id=snapshot_id, deployment_id=deployment_id, account_id=account_id, observed_at=observed_at, duration_seconds=duration_seconds, requested_start_at=requested_start_at, requested_end_at=requested_end_at)


build_complete_rest_snapshot = complete_rest_snapshot
reconcile_snapshot = reconcile_fixture

__all__ = ["HEALTHY", "PARTIAL", "UNKNOWN", "INCONSISTENT", "REQUIRED_CHANNELS", "OPTIONAL_CHANNELS", "LiveReconcileError", "RestSnapshotResult", "ReconcileResult", "ReconcileApplication", "LiveReconciler", "complete_rest_snapshot", "build_complete_rest_snapshot", "reconcile_fixture", "reconcile_snapshot", "apply_reconcile"]
