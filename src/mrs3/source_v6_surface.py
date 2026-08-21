"""Self-contained, atomically published Source v6 surface files."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
from pathlib import Path
import os
import tempfile
from typing import Callable, Mapping, Sequence
from uuid import uuid4

import duckdb
import pandas as pd

from .source_v6 import SourceV6Fragment, _canonical_json
from .source_v6_stitch import CanonicalMetrics, calculate_metrics
from .source_v6_coverage import coverage_cells
from .source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP


# These values are deliberately owned by the published surface adapter.  A
# surface which does not declare the complete tuple is a diagnostic/legacy
# artifact, not a v6 analysis input.
SOURCE_V6_SURFACE_SCHEMA_VERSION = 6
SOURCE_V6_METRIC_SCHEMA_VERSION = "source-v6-metrics-v1"
SOURCE_V6_EVENT_SCHEMA_VERSION = "source-v6-events-v1"
SOURCE_V6_READINESS_SCHEMA_VERSION = "source-v6-readiness-v1"
SOURCE_V6_FROZEN_DIGEST_ALGORITHM = "sha256-canonical-frozen-facts-v1"
SOURCE_V6_EVENT_MODE = "real_independent_events"


class SourceV6SurfaceError(ValueError):
    """A published surface is malformed or cannot be admitted to analysis."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if code else message)


@dataclass(frozen=True, slots=True)
class SurfaceScanResult:
    path: str
    status: str
    surface_id: str | None
    error: str | None = None


def _surface_metrics(
    fragments: Sequence[SourceV6Fragment],
    intervals: dict[str, tuple[int, int]] | None = None,
) -> CanonicalMetrics:
    by_point: dict[str, list[SourceV6Fragment]] = {}
    for fragment in fragments:
        by_point.setdefault(fragment.point.canonical_key, []).append(fragment)
    # A surface can contain many points; top-level metrics are a deterministic
    # summary of the first point, while point_metrics is authoritative per point.
    first_key = sorted(by_point)[0]
    selected = (intervals or {}).get(first_key)
    return calculate_metrics(tuple(by_point[first_key]), start_ms=selected[0], end_ms=selected[1]) if selected else calculate_metrics(tuple(by_point[first_key]))


def _scope_key(fact: Mapping[str, object]) -> str:
    return f"{fact.get('symbol')}|{str(fact.get('side', '')).upper()}|{fact.get('timeframe')}"


def _ready_intervals_payload(
    point_facts: object,
    intervals: Mapping[str, tuple[int, int]] | None,
) -> list[dict[str, object]]:
    """Encode one explicit half-open READY interval per execution scope."""
    if not intervals:
        return []
    facts = [item for item in point_facts if isinstance(item, Mapping)]
    by_scope: dict[str, list[tuple[int, int]]] = {}
    for fact in facts:
        point_key = str(fact.get("point_key", ""))
        selected = intervals.get(point_key)
        if selected is None:
            continue
        by_scope.setdefault(_scope_key(fact), []).append((int(selected[0]), int(selected[1])))
    result: list[dict[str, object]] = []
    for scope, values in sorted(by_scope.items()):
        # A surface is READY only over the intersection available to every
        # canonical point in the scope.  The adapter rejects empty intersections.
        start = max(value[0] for value in values)
        end = min(value[1] for value in values)
        if end > start:
            result.append({"scope_key": scope, "start_ms": start, "end_ms": end})
    return result


def _payload_frozen_rows(payload: Mapping[str, object]) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return the exact logical rows used by the DuckDB frozen digest."""
    point_rows = []
    for item in payload.get("point_facts", ()):
        if not isinstance(item, Mapping) or "point_key" not in item:
            continue
        point_rows.append((str(item["point_key"]), json.dumps(dict(item), sort_keys=True, separators=(",", ":"))))
    fragment_rows = []
    event_rows = []
    for item in payload.get("fragments", ()):
        if not isinstance(item, Mapping) or "fragment_id" not in item:
            continue
        fragment_id = str(item["fragment_id"])
        fragment_rows.append((fragment_id, json.dumps(dict(item), sort_keys=True, separators=(",", ":"))))
        for event in item.get("events", ()):
            if not isinstance(event, Mapping) or "event_id" not in event:
                continue
            event_rows.append((fragment_id, str(event["event_id"]), json.dumps(dict(event), sort_keys=True, separators=(",", ":"))))
    return [
        ("frozen_point_facts", sorted(point_rows)),
        ("frozen_fragments", sorted(fragment_rows)),
        ("frozen_events", sorted(event_rows)),
    ]


def _payload_frozen_digest(payload: Mapping[str, object]) -> str:
    return sha256(_canonical_json(_payload_frozen_rows(payload)).encode("utf-8")).hexdigest()


def _surface_payload(
    fragments: Sequence[SourceV6Fragment],
    metrics: CanonicalMetrics,
    intervals: dict[str, tuple[int, int]] | None = None,
    overlap_tail_decisions: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    fragment_ids = sorted(fragment.fragment_id for fragment in fragments)
    points = sorted({fragment.point.canonical_key for fragment in fragments})
    decisions = [dict(item) for item in (overlap_tail_decisions or ())]
    identity = _canonical_json({"fragments": fragment_ids, "points": points, "intervals": sorted((fragment.fragment_id, fragment.report_start_ms, fragment.report_end_ms) for fragment in fragments), "selected_intervals": sorted((intervals or {}).items()), "overlap_tail_decisions": sorted(decisions, key=_canonical_json)})
    surface_id = sha256(identity.encode("utf-8")).hexdigest()
    content: dict[str, object] = {
        "schema_version": 6,
        "surface_schema_version": SOURCE_V6_SURFACE_SCHEMA_VERSION,
        "metric_schema_version": SOURCE_V6_METRIC_SCHEMA_VERSION,
        "event_schema_version": SOURCE_V6_EVENT_SCHEMA_VERSION,
        "readiness_schema_version": SOURCE_V6_READINESS_SCHEMA_VERSION,
        "frozen_facts_digest_algorithm": SOURCE_V6_FROZEN_DIGEST_ALGORITHM,
        "event_mode": SOURCE_V6_EVENT_MODE,
        "manifest_version": "source-v6-surface-manifest-v1",
        "contract_fingerprint": "source-v6-normalized-fragment-v1",
        "surface_id": surface_id,
        "fragment_ids": fragment_ids,
        "points": points,
        "point_facts": [
            {
                "point_key": fragment.point.canonical_key,
                "symbol": fragment.point.symbol,
                "side": fragment.point.side,
                "timeframe": fragment.point.timeframe,
                "shift_bp": fragment.point.shift_bp,
                "open_ma_type": fragment.point.open_ma_type,
                "open_ma_source": fragment.point.open_ma_source,
                "open_ma_length": fragment.point.open_ma_length,
                "close_ma_type": fragment.point.close_ma_type,
                 "close_ma_source": fragment.point.close_ma_source,
                 "close_ma_length": fragment.point.close_ma_length,
                 "report_start_ms": fragment.report_start_ms,
                 "report_end_ms": fragment.report_end_ms,
                 "settings_fingerprint": fragment.settings_fingerprint,
            }
            for fragment in sorted(
                (sorted((candidate for candidate in fragments if candidate.point.canonical_key == point_key), key=lambda candidate: candidate.fragment_id)[0] for point_key in points),
                key=lambda item: item.point.canonical_key,
            )
        ],
        "fragments": [
            {
                "fragment_id": fragment.fragment_id,
                "point_key": fragment.point.canonical_key,
                "source_sha256": fragment.source_sha256,
                "report_start_ms": fragment.report_start_ms,
                "report_end_ms": fragment.report_end_ms,
                "stitchability": fragment.stitchability,
                "open_tail_cycle_ids": list(fragment.open_tail_cycle_ids),
                "actions": [
                    {"action_id": action.action_id, "timestamp_ms": action.timestamp_ms, "symbol": action.symbol, "order_id": action.order_id, "action": action.action, "fee": str(action.fee), "pnl": str(action.pnl), "balance": None if action.balance is None else str(action.balance), "size": None if action.size is None else str(action.size), "post_size": None if action.post_size is None else str(action.post_size), "post_side": action.post_side}
                    for action in fragment.actions
                ],
                "cycles": [{"cycle_id": cycle.cycle_id, "symbol": cycle.symbol, "order_id": cycle.order_id, "action_ids": list(cycle.action_ids), "open_timestamp_ms": cycle.open_timestamp_ms, "close_timestamp_ms": cycle.close_timestamp_ms, "realized_pnl": str(cycle.realized_pnl), "fees": str(cycle.fees)} for cycle in fragment.cycles],
                "events": [{"event_id": event.event_id, "timestamp_ms": event.timestamp_ms, "action_id": event.action_id} for event in fragment.events],
                "wallet_samples": [[sample.timestamp_ms, str(sample.value), str(sample.upnl)] for sample in fragment.wallet_samples],
                "equity_samples": [[sample.timestamp_ms, str(sample.value), str(sample.upnl)] for sample in fragment.equity_samples],
            }
            for fragment in sorted(fragments, key=lambda item: item.fragment_id)
        ],
        "coverage": [{"point_key": cell.point_key, "utc_day": cell.utc_day.isoformat(), "status": cell.status} for cell in coverage_cells(fragments)],
        "overlap_tail_decisions": sorted(decisions, key=_canonical_json),
        "selected_intervals": [
            {"scope_key": scope, "start_ms": values[0], "end_ms": values[1]}
            for scope, values in sorted((intervals or {}).items())
        ],
        "metrics": {
            "TotalPnL": str(metrics.total_pnl),
            "TotalPnLPercent": str(metrics.total_pnl_percent),
            "ProfitFactor": None if metrics.profit_factor is None else str(metrics.profit_factor),
            "TotalTrades": metrics.total_trades,
            "Win": metrics.win_trades,
            "Los": metrics.loss_trades,
            "WinRate": str(metrics.win_rate_percent),
            "MaxEquityDrawdown": str(metrics.max_equity_drawdown),
            "MaxEquityDrawdownPercent": str(metrics.max_equity_drawdown_percent),
            "MaxRealizedDrawdown": str(metrics.max_realized_drawdown),
            "MaxRealizedDrawdownPercent": str(metrics.max_realized_drawdown_percent),
            "event_ids": list(metrics.events),
        },
    }
    by_point: dict[str, list[SourceV6Fragment]] = {}
    for fragment in fragments:
        by_point.setdefault(fragment.point.canonical_key, []).append(fragment)
    point_rows = []
    for key in points:
        selected = (intervals or {}).get(key)
        point_metrics = calculate_metrics(tuple(by_point[key]), start_ms=selected[0], end_ms=selected[1]) if selected else calculate_metrics(tuple(by_point[key]))
        point_rows.append({
            "point_key": key,
             "TotalPnLPercent": str(point_metrics.total_pnl_percent),
             "MaxEquityDrawdownPercent": str(point_metrics.max_equity_drawdown_percent),
             "MaxDrawdownPercent": str(point_metrics.max_equity_drawdown_percent),
            "TotalTrades": point_metrics.total_trades,
            "Win": point_metrics.win_trades,
            "Los": point_metrics.loss_trades,
            "WinRate": str(point_metrics.win_rate_percent),
             "ProfitFactor": None if point_metrics.profit_factor is None else str(point_metrics.profit_factor),
             "point_event_count": len(point_metrics.events),
             "event_ids": list(point_metrics.events),
             "event_ids_hash": sha256("|".join(sorted(set(point_metrics.events))).encode("utf-8")).hexdigest(),
          })
    content["point_metrics"] = point_rows
    content["ready_intervals"] = _ready_intervals_payload(content["point_facts"], intervals)
    content["frozen_facts_sha256"] = _payload_frozen_digest(content)
    # Persist analysis evidence at publication; export is strictly read-only.
    from .source_v6_analysis import build_persisted_analysis_facts
    content["analysis_facts"] = build_persisted_analysis_facts(content["point_facts"], point_rows)
    def sanitize(value: object) -> object:
        if isinstance(value, float) and math.isnan(value):
            return None
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value
    content = sanitize(content)
    content["manifest_sha256"] = sha256(_canonical_json(content).encode("utf-8")).hexdigest()
    content["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return content


def publish_surface(directory: str | Path, fragments: Sequence[SourceV6Fragment], *, intervals: dict[str, tuple[int, int]] | None = None) -> Path:
    if not fragments:
        raise SourceV6SurfaceError("cannot publish an empty surface")
    metrics = _surface_metrics(fragments, intervals)
    payload = _surface_payload(fragments, metrics, intervals)
    output_dir = Path(directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"source-v6-p{len(payload['points'])}-{payload['surface_id']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:8]}.json"
    target = output_dir / filename
    fd, temporary = tempfile.mkstemp(prefix=".source-v6-", suffix=".staging", dir=output_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        readback = json.loads(Path(temporary).read_text(encoding="utf-8"))
        if readback.get("surface_id") != payload["surface_id"] or readback.get("schema_version") != 6:
            raise SourceV6SurfaceError("surface readback validation failed")
        os.replace(temporary, target)
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_surface(path: str | Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 6 or not payload.get("surface_id") or not payload.get("manifest_sha256"):
        raise SourceV6SurfaceError("invalid Source v6 surface")
    expected = payload.pop("manifest_sha256")
    created_at = payload.pop("created_at_utc", None)
    actual = sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    if created_at is not None:
        payload["created_at_utc"] = created_at
    payload["manifest_sha256"] = expected
    if expected != actual:
        raise SourceV6SurfaceError("surface manifest hash mismatch")
    return payload


def scan_surfaces(directory: str | Path) -> tuple[dict[str, object], ...]:
    """Return valid JSON and DuckDB manifests in deterministic order."""
    result = []
    for path in sorted(Path(directory).glob("source-v6-*.json")):
        try:
            result.append(read_surface(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    for path in sorted(Path(directory).glob("source-v6-*.duckdb")):
        try:
            result.append(read_surface_db(path))
        except (OSError, ValueError, duckdb.Error, json.JSONDecodeError):
            continue
    return tuple(sorted(result, key=lambda payload: str(payload["surface_id"])))


def scan_surface_diagnostics(directory: str | Path) -> tuple[SurfaceScanResult, ...]:
    results: list[SurfaceScanResult] = []
    for path in sorted(Path(directory).iterdir()) if Path(directory).exists() else ():
        if path.name.startswith(".") and path.name.endswith(".staging"):
            results.append(SurfaceScanResult(str(path), "STAGING", None))
        elif path.name.startswith("source-v6-") and path.suffix == ".json":
            try:
                payload = read_surface(path)
                results.append(SurfaceScanResult(str(path), "VALID", str(payload["surface_id"])))
            except SourceV6SurfaceError as error:
                status = "HASH_MISMATCH" if "hash" in str(error) else "MALFORMED"
                results.append(SurfaceScanResult(str(path), status, None, str(error)))
        elif path.name.startswith("source-v6-") and path.suffix == ".duckdb":
            try:
                payload = read_surface_db(path)
                results.append(SurfaceScanResult(str(path), "VALID", str(payload["surface_id"])))
            except (SourceV6SurfaceError, duckdb.Error) as error:
                text = str(error)
                status = "HASH_MISMATCH" if "hash" in text else "MALFORMED"
                results.append(SurfaceScanResult(str(path), status, None, text))
    return tuple(results)


def read_surface_db(path: str | Path) -> dict[str, object]:
    """Read and validate a published DuckDB surface manifest."""
    connection = duckdb.connect(str(path), read_only=True)
    try:
        rows = dict(connection.execute("select key, value from manifest").fetchall())
        raw = rows.get("surface_manifest_json")
        if not raw:
            raise SourceV6SurfaceError("DuckDB surface manifest is missing")
        payload = json.loads(raw)
        if payload.get("schema_version") != 6 or payload.get("surface_id") != rows.get("surface_id"):
            raise SourceV6SurfaceError("DuckDB surface manifest is malformed")
        expected_manifest = payload.get("manifest_sha256")
        unsigned = dict(payload)
        unsigned.pop("manifest_sha256", None)
        unsigned.pop("created_at_utc", None)
        actual_manifest = sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()
        if expected_manifest != actual_manifest or expected_manifest != rows.get("manifest_sha256"):
            raise SourceV6SurfaceError("DuckDB surface manifest hash mismatch")
        expected_frozen = rows.get("frozen_facts_sha256")
        payload_frozen = payload.get("frozen_facts_sha256")
        if not expected_frozen or not payload_frozen or payload_frozen != expected_frozen or _frozen_digest(connection) != expected_frozen:
            raise SourceV6SurfaceError("DuckDB frozen surface facts hash mismatch")
        return payload
    finally:
        connection.close()


def _frozen_digest(connection: duckdb.DuckDBPyConnection) -> str:
    rows = []
    for table in ("frozen_point_facts", "frozen_fragments", "frozen_events"):
        rows.append((table, connection.execute(f"select * from {table} order by all").fetchall()))
    return sha256(_canonical_json(rows).encode("utf-8")).hexdigest()


def verify_surface_frozen_facts(path: str | Path) -> str:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        digest = _frozen_digest(connection)
        expected = dict(connection.execute("select key, value from manifest").fetchall()).get("frozen_facts_sha256")
        if expected and expected != digest:
            raise SourceV6SurfaceError("frozen surface facts hash mismatch")
        return digest
    finally:
        connection.close()


def _adapter_error(code: str, message: str) -> SourceV6SurfaceError:
    return SourceV6SurfaceError(message, code=code)


def _canonical_scope(value: object) -> tuple[str, str, str]:
    if isinstance(value, str):
        pieces = value.split("|")
    elif isinstance(value, Mapping):
        pieces = [value.get("symbol"), value.get("side"), value.get("timeframe")]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        pieces = list(value)
    else:
        raise _adapter_error("INVALID_SCOPE", "selected scope must be symbol|side|timeframe")
    if len(pieces) != 3 or any(not isinstance(item, str) or not item.strip() for item in pieces):
        raise _adapter_error("INVALID_SCOPE", "selected scope must contain non-empty symbol, side and timeframe")
    symbol, side, timeframe = (item.strip() for item in pieces)
    side = side.upper()
    if side not in {"LONG", "SHORT"}:
        raise _adapter_error("INVALID_SCOPE", "selected side must be LONG or SHORT")
    return symbol, side, timeframe


def _utc_millis(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise _adapter_error("INVALID_INTERVAL", f"{field} must be timezone-aware UTC")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise _adapter_error("INVALID_INTERVAL", f"{field} must be an integer UTC millisecond")
        return int(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        raise _adapter_error("INVALID_INTERVAL", f"{field} must include a UTC timezone")
    candidate = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if isinstance(candidate, datetime):
        parsed = candidate
    else:
        if not isinstance(value, str):
            raise _adapter_error("INVALID_INTERVAL", f"{field} must be timezone-aware UTC")
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise _adapter_error("INVALID_INTERVAL", f"{field} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _adapter_error("INVALID_INTERVAL", f"{field} must be timezone-aware UTC")
    utc = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = utc - epoch
    total_microseconds = (
        elapsed.days * 86_400_000_000
        + elapsed.seconds * 1_000_000
        + elapsed.microseconds
    )
    millis, remainder = divmod(total_microseconds, 1_000)
    if remainder:
        raise _adapter_error("INVALID_INTERVAL", f"{field} must resolve to an exact UTC millisecond")
    return millis


def _typed_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise _adapter_error("INVALID_METRIC", f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise _adapter_error("INVALID_METRIC", f"{field} must be an integer") from error
    if isinstance(value, float) and value != result:
        raise _adapter_error("INVALID_METRIC", f"{field} must be an integer")
    if isinstance(value, str) and str(result) != value.strip():
        raise _adapter_error("INVALID_METRIC", f"{field} must be an integer")
    return result


def _finite_number(value: object, field: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or value is None:
        raise _adapter_error("INVALID_METRIC", f"{field} must be a finite number")
    try:
        parsed = float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError, OverflowError) as error:
        raise _adapter_error("INVALID_METRIC", f"{field} must be a finite number") from error
    if not math.isfinite(parsed):
        raise _adapter_error("INVALID_METRIC", f"{field} must be a finite number")
    return parsed


def _payload_interval(item: Mapping[str, object], *, field: str) -> tuple[int, int]:
    raw_start = item.get("start_ms", item.get("start"))
    raw_end = item.get("end_ms", item.get("end"))
    if raw_start is None or raw_end is None:
        raise _adapter_error("INVALID_READINESS", f"{field} must contain start and end")
    start = _utc_millis(raw_start, f"{field}.start")
    end = _utc_millis(raw_end, f"{field}.end")
    if end <= start:
        raise _adapter_error("INVALID_READINESS", f"{field} must be non-empty")
    return start, end


def _validate_adapter_manifest(
    payload: Mapping[str, object],
    *,
    expected_surface_id: str | None,
    expected_manifest_sha256: str | None,
    expected_frozen_facts_sha256: str | None,
) -> tuple[str, str, str]:
    if not isinstance(payload.get("surface_id"), str) or not payload.get("surface_id"):
        raise _adapter_error("INVALID_MANIFEST", "surface manifest has no surface_id")
    surface_id = str(payload["surface_id"])
    manifest_sha = payload.get("manifest_sha256")
    frozen_sha = payload.get("frozen_facts_sha256")
    if not isinstance(manifest_sha, str) or not manifest_sha:
        raise _adapter_error("INVALID_MANIFEST", "surface manifest has no manifest hash")
    if not isinstance(frozen_sha, str) or not frozen_sha:
        raise _adapter_error("INVALID_MANIFEST", "surface manifest has no frozen facts hash")
    if expected_surface_id is not None and surface_id != expected_surface_id:
        raise _adapter_error("SURFACE_ID_MISMATCH", "surface_id does not match the selected surface")
    if expected_manifest_sha256 is not None and manifest_sha != expected_manifest_sha256:
        raise _adapter_error("MANIFEST_HASH_MISMATCH", "surface manifest hash does not match the request")
    if expected_frozen_facts_sha256 is not None and frozen_sha != expected_frozen_facts_sha256:
        raise _adapter_error("FROZEN_DIGEST_MISMATCH", "frozen surface facts hash does not match the request")
    if _payload_frozen_digest(payload) != frozen_sha:
        raise _adapter_error("FROZEN_DIGEST_MISMATCH", "frozen surface facts hash mismatch")
    versions = (
        payload.get("surface_schema_version"),
        payload.get("metric_schema_version"),
        payload.get("event_schema_version"),
        payload.get("readiness_schema_version"),
        payload.get("frozen_facts_digest_algorithm"),
    )
    expected_versions = (
        SOURCE_V6_SURFACE_SCHEMA_VERSION,
        SOURCE_V6_METRIC_SCHEMA_VERSION,
        SOURCE_V6_EVENT_SCHEMA_VERSION,
        SOURCE_V6_READINESS_SCHEMA_VERSION,
        SOURCE_V6_FROZEN_DIGEST_ALGORITHM,
    )
    if versions != expected_versions:
        raise _adapter_error("INCOMPATIBLE_SCHEMA", "surface compatibility version tuple is absent or unsupported")
    if payload.get("event_mode") != SOURCE_V6_EVENT_MODE:
        raise _adapter_error("UNSUPPORTED_EVENT_MODE", "v6 analysis requires real_independent_events")
    return surface_id, manifest_sha, frozen_sha


def _ready_candidates(payload: Mapping[str, object], scope: tuple[str, str, str]) -> list[tuple[int, int]]:
    scope_key = "|".join(scope)
    raw = payload.get("ready_intervals")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise _adapter_error("INVALID_READINESS", "surface has no READY interval list")
    candidates: list[tuple[int, int]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise _adapter_error("INVALID_READINESS", f"READY interval {index} is malformed")
        declared = item.get("scope_key")
        if declared is None and isinstance(item.get("scope"), Mapping):
            declared = "|".join(_canonical_scope(item["scope"]))
        if declared != scope_key:
            continue
        candidates.append(_payload_interval(item, field=f"ready_intervals[{index}]"))
    if not candidates:
        raise _adapter_error("MISSING_READINESS", f"scope {scope_key} has no READY interval")
    return candidates


def _coverage_ready(payload: Mapping[str, object], point_keys: Sequence[str], start_ms: int, end_ms: int) -> None:
    raw = payload.get("coverage")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise _adapter_error("MISSING_COVERAGE", "surface has no canonical coverage evidence")
    covered = {
        (str(item.get("point_key")), str(item.get("utc_day")))
        for item in raw
        if isinstance(item, Mapping) and str(item.get("status", "")) == "READY"
    }
    first = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
    last = datetime.fromtimestamp((end_ms - 1) / 1000, timezone.utc).date()
    current = first
    while current <= last:
        day = current.isoformat()
        missing = [key for key in point_keys if (key, day) not in covered]
        if missing:
            raise _adapter_error("COVERAGE_GAP", f"canonical coverage gap at {day} for {missing[0]}")
        current = date.fromordinal(current.toordinal() + 1)


def _strict_v6_rows(
    payload: Mapping[str, object],
    *,
    scope: tuple[str, str, str],
    start_ms: int,
    end_ms: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    candidates = _ready_candidates(payload, scope)
    containing = [item for item in candidates if item[0] <= start_ms and end_ms <= item[1]]
    if len(containing) != 1:
        reason = "outside READY interval" if not containing else "ambiguous READY interval"
        raise _adapter_error("INVALID_INTERVAL", f"selected interval is {reason}")
    facts_raw = payload.get("point_facts")
    metrics_raw = payload.get("point_metrics")
    fragments_raw = payload.get("fragments")
    if not isinstance(facts_raw, Sequence) or isinstance(facts_raw, (str, bytes, bytearray)):
        raise _adapter_error("INCOMPLETE_GRID", "surface has no point facts")
    if not isinstance(metrics_raw, Sequence) or isinstance(metrics_raw, (str, bytes, bytearray)):
        raise _adapter_error("MISSING_METRIC", "surface has no point metrics")
    if not isinstance(fragments_raw, Sequence) or isinstance(fragments_raw, (str, bytes, bytearray)):
        raise _adapter_error("MISSING_EVENTS", "surface has no frozen fragments/events")
    facts = [item for item in facts_raw if isinstance(item, Mapping) and _scope_key(item) == "|".join(scope)]
    if not facts:
        raise _adapter_error("SCOPE_MISMATCH", "selected scope has no point facts")
    by_key: dict[str, Mapping[str, object]] = {}
    for fact in facts:
        key = fact.get("point_key")
        if not isinstance(key, str) or not key:
            raise _adapter_error("INVALID_POINT", "point fact has no full point_key")
        if key in by_key:
            raise _adapter_error("INVALID_POINT", f"duplicate point fact {key}")
        by_key[key] = fact
        if str(fact.get("side", "")).upper() != scope[1]:
            raise _adapter_error("SCOPE_MISMATCH", f"point {key} has a mismatched side")
        for field in ("shift_bp", "open_ma_length", "close_ma_length"):
            _typed_int(fact.get(field), field)
    grid = {
        (_typed_int(fact.get("shift_bp"), "shift_bp"), _typed_int(fact.get("close_ma_length"), "close_ma_length"))
        for fact in facts
    }
    required_grid = {(int(shift), int(close)) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS}
    if grid != required_grid:
        raise _adapter_error("INCOMPLETE_GRID", "selected scope does not contain the complete canonical point grid")
    if len(facts) != len(required_grid):
        raise _adapter_error("INCOMPLETE_GRID", "selected scope has duplicate or extra canonical points")
    metric_map: dict[str, Mapping[str, object]] = {}
    for metric in metrics_raw:
        if not isinstance(metric, Mapping) or not isinstance(metric.get("point_key"), str):
            raise _adapter_error("MISSING_METRIC", "point metric row is malformed")
        key = str(metric["point_key"])
        if key in metric_map:
            raise _adapter_error("MISSING_METRIC", f"duplicate point metric {key}")
        metric_map[key] = metric
    fragment_map: dict[str, list[Mapping[str, object]]] = {key: [] for key in by_key}
    for fragment in fragments_raw:
        if not isinstance(fragment, Mapping):
            raise _adapter_error("MISSING_EVENTS", "frozen fragment row is malformed")
        key = fragment.get("point_key")
        if key in fragment_map:
            fragment_map[str(key)].append(fragment)
    if any(not values for values in fragment_map.values()):
        raise _adapter_error("MISSING_EVENTS", "selected point has no frozen fragment")
    for key, fragments in fragment_map.items():
        for fragment in fragments:
            if fragment.get("event_mode", payload.get("event_mode")) != SOURCE_V6_EVENT_MODE:
                raise _adapter_error("UNSUPPORTED_EVENT_MODE", f"fragment for {key} has an unsupported event mode")
            events = fragment.get("events")
            if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)):
                raise _adapter_error("MISSING_EVENTS", f"fragment events are missing for {key}")
            for event in events:
                if isinstance(event, Mapping) and "event_mode" in event and event["event_mode"] != SOURCE_V6_EVENT_MODE:
                    raise _adapter_error("UNSUPPORTED_EVENT_MODE", f"event for {key} has an unsupported event mode")
    _coverage_ready(payload, tuple(sorted(by_key)), start_ms, end_ms)
    rows: list[dict[str, object]] = []
    for key in sorted(by_key):
        fact = by_key[key]
        metric = metric_map.get(key)
        if metric is None:
            raise _adapter_error("MISSING_METRIC", f"missing point metrics for {key}")
        for field in ("TotalPnLPercent", "TotalTrades", "Win", "Los", "WinRate", "ProfitFactor", "point_event_count"):
            if field not in metric:
                raise _adapter_error("MISSING_METRIC", f"missing {field} for {key}")
        pnl = _finite_number(metric["TotalPnLPercent"], f"{key}.TotalPnLPercent")
        primary_dd = metric.get("MaxEquityDrawdownPercent")
        alias_dd = metric.get("MaxDrawdownPercent")
        if primary_dd is None:
            raise _adapter_error("MISSING_METRIC", f"missing MaxEquityDrawdownPercent for {key}")
        dd = _finite_number(primary_dd, f"{key}.MaxEquityDrawdownPercent")
        if alias_dd is not None:
            alias = _finite_number(alias_dd, f"{key}.MaxDrawdownPercent")
            if alias != dd:
                raise _adapter_error("INVALID_METRIC", f"drawdown aliases disagree for {key}")
        trades = _typed_int(metric["TotalTrades"], f"{key}.TotalTrades")
        wins = _typed_int(metric["Win"], f"{key}.Win")
        losses = _typed_int(metric["Los"], f"{key}.Los")
        win_rate = _finite_number(metric["WinRate"], f"{key}.WinRate")
        pf = _finite_number(metric["ProfitFactor"], f"{key}.ProfitFactor", allow_none=True)
        if pf is None and losses != 0:
            raise _adapter_error("INVALID_METRIC", f"ProfitFactor is null despite losses for {key}")
        event_ids_raw = metric.get("event_ids")
        if not isinstance(event_ids_raw, Sequence) or isinstance(event_ids_raw, (str, bytes, bytearray)):
            raise _adapter_error("MISSING_EVENTS", f"missing exact event IDs for {key}")
        event_ids = [item for item in event_ids_raw if isinstance(item, str) and item]
        if len(event_ids) != len(event_ids_raw) or len(set(event_ids)) != len(event_ids):
            raise _adapter_error("INVALID_EVENTS", f"event IDs are not distinct strings for {key}")
        event_rows: dict[str, int] = {}
        for fragment in fragment_map[key]:
            events = fragment.get("events")
            if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)):
                raise _adapter_error("MISSING_EVENTS", f"fragment events are missing for {key}")
            for event in events:
                if not isinstance(event, Mapping) or not isinstance(event.get("event_id"), str):
                    raise _adapter_error("INVALID_EVENTS", f"event row is malformed for {key}")
                event_id = str(event["event_id"])
                timestamp = _typed_int(event.get("timestamp_ms"), f"{key}.event.timestamp_ms")
                previous = event_rows.get(event_id)
                if previous is not None and previous != timestamp:
                    raise _adapter_error("INVALID_EVENTS", f"event ID has conflicting timestamps for {key}")
                event_rows[event_id] = timestamp
        missing_ids = [event_id for event_id in event_ids if event_id not in event_rows]
        if missing_ids:
            raise _adapter_error("MISSING_EVENTS", f"exact event IDs are absent from frozen events for {key}")
        outside_ids = [event_id for event_id in event_ids if not start_ms <= event_rows[event_id] < end_ms]
        if outside_ids:
            raise _adapter_error("INVALID_EVENTS", f"admitted event lies outside the selected interval for {key}")
        selected_event_ids = sorted(event_ids)
        declared_count = _typed_int(metric["point_event_count"], f"{key}.point_event_count")
        if declared_count != len(selected_event_ids):
            raise _adapter_error("INVALID_EVENTS", f"point_event_count disagrees with exact event IDs for {key}")
        expected_event_hash = sha256("|".join(selected_event_ids).encode("utf-8")).hexdigest()
        if metric.get("event_ids_hash") != expected_event_hash:
            raise _adapter_error("INVALID_EVENTS", f"event_ids_hash disagrees for {key}")
        if metric.get("event_mode", payload.get("event_mode")) != SOURCE_V6_EVENT_MODE:
            raise _adapter_error("UNSUPPORTED_EVENT_MODE", f"point {key} has an unsupported event mode")
        rows.append({
            "point_id": key,
            "symbol": fact["symbol"],
            "side": str(fact["side"]).upper(),
            "timeframe": fact["timeframe"],
            "shift_bp": _typed_int(fact["shift_bp"], "shift_bp"),
            "shift_pct": _typed_int(fact["shift_bp"], "shift_bp") / 100,
            "open_ma": _typed_int(fact["open_ma_length"], "open_ma_length"),
            "close_ma": _typed_int(fact["close_ma_length"], "close_ma_length"),
            "pnl_pct": pnl,
            "dd_pct": dd,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
            "profit_factor": pf,
            "_event_ids": tuple(selected_event_ids),
            "event_ids_hash": expected_event_hash,
            "point_event_count": declared_count,
            "event_mode": SOURCE_V6_EVENT_MODE,
            "report_start": pd.Timestamp.fromtimestamp(start_ms / 1000, tz="UTC"),
            "report_end": pd.Timestamp.fromtimestamp(end_ms / 1000, tz="UTC"),
        })
    return rows, {
        "selected_scope": {"symbol": scope[0], "side": scope[1], "timeframe": scope[2]},
        "selected_interval_ms": (start_ms, end_ms),
        "point_keys": tuple(sorted(by_key)),
    }


def load_source_v6_pipeline_input(
    path: str | Path,
    scope: object | None = None,
    start: object | None = None,
    end: object | None = None,
    *,
    selected_scope: object | None = None,
    selected_start: object | None = None,
    selected_end: object | None = None,
    surface_id: str | None = None,
    manifest_sha256: str | None = None,
    frozen_facts_sha256: str | None = None,
    expected_surface_id: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_frozen_facts_sha256: str | None = None,
):
    """Adapt one published Source v6 surface into the shared pipeline.

    ``scope``, ``start`` and ``end`` identify exactly one READY scope and a
    non-empty UTC half-open interval.  The ``selected_*`` spellings and expected
    hash spellings are accepted as explicit aliases for panel integrations.
    """
    from .pipeline import PipelineInput

    if selected_scope is not None:
        if scope is not None and scope != selected_scope:
            raise _adapter_error("INVALID_SCOPE", "scope and selected_scope disagree")
        scope = selected_scope
    if selected_start is not None:
        if start is not None and start != selected_start:
            raise _adapter_error("INVALID_INTERVAL", "start and selected_start disagree")
        start = selected_start
    if selected_end is not None:
        if end is not None and end != selected_end:
            raise _adapter_error("INVALID_INTERVAL", "end and selected_end disagree")
        end = selected_end
    expected_surface_id = expected_surface_id if expected_surface_id is not None else surface_id
    expected_manifest_sha256 = expected_manifest_sha256 if expected_manifest_sha256 is not None else manifest_sha256
    expected_frozen_facts_sha256 = expected_frozen_facts_sha256 if expected_frozen_facts_sha256 is not None else frozen_facts_sha256
    if scope is None or start is None or end is None:
        raise _adapter_error("INVALID_REQUEST", "selected scope and non-empty UTC start/end are required")
    surface_path = Path(path)
    try:
        payload = read_surface_db(surface_path) if surface_path.suffix.lower() == ".duckdb" else read_surface(surface_path)
    except SourceV6SurfaceError:
        raise
    except (OSError, ValueError, json.JSONDecodeError, duckdb.Error) as error:
        raise _adapter_error("INVALID_SURFACE", f"published surface cannot be read: {error}") from error

    source_surface_id, source_manifest_sha256, source_frozen_sha256 = _validate_adapter_manifest(
        payload,
        expected_surface_id=expected_surface_id,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_frozen_facts_sha256=expected_frozen_facts_sha256,
    )
    selected = _canonical_scope(scope)
    start_ms = _utc_millis(start, "start")
    end_ms = _utc_millis(end, "end")
    if end_ms <= start_ms:
        raise _adapter_error("INVALID_INTERVAL", "selected interval must be non-empty")
    rows, selection = _strict_v6_rows(payload, scope=selected, start_ms=start_ms, end_ms=end_ms)
    points = pd.DataFrame(rows)
    points.attrs.update({
        "source_surface_id": source_surface_id,
        "source_manifest_sha256": source_manifest_sha256,
        "source_frozen_facts_sha256": source_frozen_sha256,
        "surface_id": source_surface_id,
        "manifest_sha256": source_manifest_sha256,
        "frozen_facts_sha256": source_frozen_sha256,
        "selected_scope": selection["selected_scope"],
        "selected_interval_ms": selection["selected_interval_ms"],
        "selected_interval": {
            "start": pd.Timestamp.fromtimestamp(selection["selected_interval_ms"][0] / 1000, tz="UTC"),
            "end": pd.Timestamp.fromtimestamp(selection["selected_interval_ms"][1] / 1000, tz="UTC"),
        },
        "event_mode": payload.get("event_mode"),
        "compatibility_versions": {
            "surface_schema_version": payload.get("surface_schema_version"),
            "metric_schema_version": payload.get("metric_schema_version"),
            "event_schema_version": payload.get("event_schema_version"),
            "readiness_schema_version": payload.get("readiness_schema_version"),
            "frozen_facts_digest_algorithm": payload.get("frozen_facts_digest_algorithm"),
        },
        "immutable_source_ids": {
            "surface_id": source_surface_id,
            "manifest_sha256": source_manifest_sha256,
            "frozen_facts_sha256": source_frozen_sha256,
            "fragment_ids": tuple(sorted(str(item) for item in payload.get("fragment_ids", ()) if isinstance(item, str))),
            "point_keys": selection["point_keys"],
        },
        "overlap_tail_decisions": tuple(dict(item) for item in payload.get("overlap_tail_decisions", ()) if isinstance(item, Mapping)),
        "tail_diagnostics": tuple({
            "fragment_id": item.get("fragment_id"),
            "point_key": item.get("point_key"),
            "open_tail_cycle_ids": tuple(item.get("open_tail_cycle_ids", ())),
        } for item in payload.get("fragments", ()) if isinstance(item, Mapping) and item.get("open_tail_cycle_ids")),
    })
    return PipelineInput(source_surface_id, points)


def publish_surface_db(directory: str | Path, fragments: Sequence[SourceV6Fragment], *, intervals: dict[str, tuple[int, int]] | None = None, overlap_tail_decisions: Sequence[Mapping[str, object]] | None = None) -> Path:
    """Publish the immutable manifest/facts and append-only analysis area in DuckDB."""
    if not fragments:
        raise SourceV6SurfaceError("cannot publish an empty surface")
    if any(not isinstance(item, SourceV6Fragment) for item in fragments):
        raise SourceV6SurfaceError(
            "surface publication requires hydrated fragments, not metadata views"
        )
    metrics = _surface_metrics(fragments, intervals)
    payload = _surface_payload(fragments, metrics, intervals, overlap_tail_decisions)
    output_dir = Path(directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"source-v6-p{len(payload['points'])}-{payload['surface_id']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:8]}.duckdb"
    fd, temporary = tempfile.mkstemp(prefix=".source-v6-", suffix=".staging.duckdb", dir=output_dir)
    os.close(fd)
    os.unlink(temporary)
    connection = duckdb.connect(temporary)
    try:
        connection.execute("create table manifest(key varchar primary key, value varchar not null)")
        connection.execute("create table frozen_point_facts(point_key varchar primary key, facts_json varchar not null)")
        connection.execute("create table frozen_fragments(fragment_id varchar primary key, facts_json varchar not null)")
        connection.execute("create table frozen_events(fragment_id varchar not null, event_id varchar not null, event_json varchar not null, primary key(fragment_id, event_id))")
        connection.execute("create table analysis_runs(run_id varchar primary key, created_at_utc varchar not null, result_json varchar not null, state varchar not null default 'COMMITTED', metadata_json varchar not null default '{}', identity_json varchar not null default '{}', attempt_id varchar)")
        connection.execute("create table analysis_run_attempts(attempt_id varchar primary key, run_id varchar not null, state varchar not null, created_at_utc varchar not null, reason varchar, metadata_json varchar not null default '{}')")
        connection.execute("create table analysis_run_facts(run_id varchar not null, fact_name varchar not null, facts_json varchar not null, primary key(run_id, fact_name))")
        connection.executemany("insert into manifest values (?, ?)", [("schema_version", "6"), ("surface_id", str(payload["surface_id"])), ("manifest_sha256", str(payload["manifest_sha256"])), ("contract_fingerprint", str(payload["contract_fingerprint"])), ("surface_manifest_json", json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))])
        connection.executemany("insert into frozen_point_facts values (?, ?)", [(str(item["point_key"]), json.dumps(item, sort_keys=True, separators=(",", ":"))) for item in payload["point_facts"]])
        for fragment in payload["fragments"]:
            connection.execute("insert into frozen_fragments values (?, ?)", [fragment["fragment_id"], json.dumps(fragment, sort_keys=True, separators=(",", ":"))])
            connection.executemany("insert into frozen_events values (?, ?, ?)", [[fragment["fragment_id"], event["event_id"], json.dumps(event, sort_keys=True, separators=(",", ":"))] for event in fragment["events"]])
        frozen_digest = _frozen_digest(connection)
        connection.execute("insert into manifest values ('frozen_facts_sha256', ?)", [frozen_digest])
        connection.execute("checkpoint")
        connection.close()
        reopened = duckdb.connect(temporary, read_only=True)
        try:
            if reopened.execute("select value from manifest where key='surface_id'").fetchone()[0] != payload["surface_id"]:
                raise SourceV6SurfaceError("DuckDB surface readback failed")
            if reopened.execute("select value from manifest where key='manifest_sha256'").fetchone()[0] != payload["manifest_sha256"]:
                raise SourceV6SurfaceError("DuckDB surface manifest hash mismatch")
            if reopened.execute("select count(*) from frozen_point_facts").fetchone()[0] != len(payload["point_facts"]):
                raise SourceV6SurfaceError("DuckDB point-fact readback count mismatch")
            if reopened.execute("select count(*) from frozen_fragments").fetchone()[0] != len(payload["fragments"]):
                raise SourceV6SurfaceError("DuckDB fragment readback count mismatch")
            expected_events = sum(len(fragment["events"]) for fragment in payload["fragments"])
            if reopened.execute("select count(*) from frozen_events").fetchone()[0] != expected_events:
                raise SourceV6SurfaceError("DuckDB event readback count mismatch")
            if reopened.execute("select value from manifest where key='frozen_facts_sha256'").fetchone()[0] != _frozen_digest(reopened):
                raise SourceV6SurfaceError("DuckDB frozen-fact hash readback mismatch")
        finally:
            reopened.close()
        os.replace(temporary, target)
        return target
    finally:
        try:
            connection.close()
        except Exception:
            pass
        if os.path.exists(temporary):
            os.unlink(temporary)


def _ensure_analysis_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Add v6 run columns/tables while retaining files made by the old API."""
    columns = {str(row[1]) for row in connection.execute("pragma table_info('analysis_runs')").fetchall()}
    additions = {
        "state": "varchar not null default 'COMMITTED'",
        "metadata_json": "varchar not null default '{}'",
        "identity_json": "varchar not null default '{}'",
        "attempt_id": "varchar",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"alter table analysis_runs add column {name} {definition}")
    connection.execute("create table if not exists analysis_run_attempts(attempt_id varchar primary key, run_id varchar not null, state varchar not null, created_at_utc varchar not null, reason varchar, metadata_json varchar not null default '{}')")
    connection.execute("create table if not exists analysis_run_facts(run_id varchar not null, fact_name varchar not null, facts_json varchar not null, primary key(run_id, fact_name))")


def append_analysis_run(path: str | Path, run_id: str, result: object) -> None:
    connection = duckdb.connect(str(path))
    transaction_started = False
    try:
        _ensure_analysis_schema(connection)
        before = _frozen_digest(connection)
        expected = dict(connection.execute("select key, value from manifest").fetchall()).get("frozen_facts_sha256")
        if not expected or before != expected:
            raise SourceV6SurfaceError("frozen surface facts hash mismatch before analysis append")
        connection.execute("begin")
        transaction_started = True
        connection.execute("insert into analysis_runs(run_id, created_at_utc, result_json) values (?, ?, ?)", [run_id, datetime.now(timezone.utc).isoformat(), json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))])
        if _frozen_digest(connection) != before:
            connection.execute("rollback")
            raise SourceV6SurfaceError("analysis append changed frozen surface facts")
        connection.execute("commit")
        transaction_started = False
    except Exception:
        if transaction_started:
            connection.execute("rollback")
        raise
    finally:
        connection.close()


def _v6_jsonable(value: object) -> object:
    if is_dataclass(value):
        return _v6_jsonable(asdict(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _v6_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_v6_jsonable(item) for item in value]
    if hasattr(value, "item") and callable(value.item):
        try:
            return _v6_jsonable(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _v6_listing_snapshot(listing_dates: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(listing_dates, Mapping) or not listing_dates:
        raise SourceV6SurfaceError("listing-date snapshot must be a non-empty mapping", code="INVALID_LISTING_DATES")
    snapshot: dict[str, str] = {}
    for symbol, value in listing_dates.items():
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise SourceV6SurfaceError("listing dates must be timezone-aware", code="INVALID_LISTING_DATES")
            value = value.astimezone(timezone.utc).date()
        if isinstance(value, date):
            text = value.isoformat()
        elif isinstance(value, str):
            try:
                text = date.fromisoformat(value[:10]).isoformat()
            except ValueError as error:
                raise SourceV6SurfaceError("listing date is not ISO-8601", code="INVALID_LISTING_DATES") from error
        else:
            raise SourceV6SurfaceError("listing date must be a date", code="INVALID_LISTING_DATES")
        symbol_text = str(symbol).strip()
        if not symbol_text:
            raise SourceV6SurfaceError("listing-date symbol must be non-empty", code="INVALID_LISTING_DATES")
        snapshot[symbol_text] = text
    return dict(sorted(snapshot.items()))


def _v6_dataframe_rows(frame: object) -> list[dict[str, object]]:
    if not isinstance(frame, pd.DataFrame):
        return []
    return [_v6_jsonable(row) for row in frame.to_dict("records")]


def _v6_run_tables(stages: object, points: pd.DataFrame, attrs: Mapping[str, object]) -> dict[str, list[dict[str, object]]]:
    tables: dict[str, list[dict[str, object]]] = {
        "points": _v6_dataframe_rows(getattr(stages, "points")),
        "refine_requests": _v6_dataframe_rows(getattr(stages, "refine_requests")),
        "plateaus": _v6_dataframe_rows(getattr(stages, "plateaus")),
        "close_profiles": _v6_dataframe_rows(getattr(stages, "close_profiles")),
        "isolated_peaks": _v6_dataframe_rows(getattr(stages, "isolated_peaks")),
        "base_one_order": _v6_dataframe_rows(getattr(stages, "base_one_order")),
        "structures": _v6_dataframe_rows(getattr(stages, "structures")),
        "structure_diagnostics": _v6_dataframe_rows(getattr(stages, "structure_diagnostics")),
    }
    tables["before_after"] = [
        {
            "point_id": row.get("point_id"),
            "before_event_eligible": bool(row.get("economic_pass", False)),
            "after_event_eligible": bool(row.get("event_eligible", False)),
            "reject_reasons": row.get("reject_reasons", ()),
            "before_reason": "ECONOMIC_FILTER_PASS" if row.get("economic_pass", False) else "ECONOMIC_FILTER_REJECT",
            "after_reason": "EVENT_FILTER_PASS" if row.get("event_eligible", False) else "INSUFFICIENT_POINT_EVENTS",
            "event_mode": row.get("event_mode"),
            "point_event_count": row.get("point_event_count"),
        }
        for row in tables["points"]
    ]
    tables["event_unions"] = [
        {
            "point_id": row.get("point_id"),
            "event_ids": sorted(set(str(item) for item in row.get("_event_ids", ()) or ())),
            "point_event_count": row.get("point_event_count"),
        }
        for row in tables["points"]
    ]
    tables["plateau_event_unions"] = [
        {
            "plateau_id": row.get("plateau_id"),
            "point_ids": row.get("all_point_ids", ()),
            "event_ids": sorted(set(str(item) for item in row.get("plateau_event_ids", ()) or ())),
            "plateau_event_count": row.get("plateau_event_count"),
            "plateau_event_ids_hash": row.get("plateau_event_ids_hash"),
        }
        for row in tables["plateaus"]
    ]
    tables["lineage"] = [
        *[{"lineage": "BASE_1ORD", **row} for row in tables["base_one_order"]],
        *[
            {"lineage": f"{int(row.get('order_count', 0))}ORD" if row.get("order_count") else "READY", **row}
            for row in tables["structures"]
        ],
    ]
    tables["tail_diagnostics"] = [_v6_jsonable(item) for item in attrs.get("tail_diagnostics", ())]
    tables["overlap_tail_decisions"] = [_v6_jsonable(item) for item in attrs.get("overlap_tail_decisions", ())]
    return tables


def _v6_run_validation(connection: duckdb.DuckDBPyConnection, pipeline_input: object, *, expected: Mapping[str, str]) -> None:
    rows = dict(connection.execute("select key, value from manifest").fetchall())
    actual = {key: rows.get(key) for key in ("surface_id", "manifest_sha256", "frozen_facts_sha256")}
    raw_manifest = rows.get("surface_manifest_json")
    if not raw_manifest:
        raise SourceV6SurfaceError("surface manifest is missing", code="SURFACE_CHANGED")
    manifest = json.loads(raw_manifest)
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    unsigned.pop("created_at_utc", None)
    manifest_hash_ok = sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest() == rows.get("manifest_sha256") == manifest.get("manifest_sha256")
    if actual != dict(expected) or manifest.get("surface_id") != expected["surface_id"] or not manifest_hash_ok or _frozen_digest(connection) != expected["frozen_facts_sha256"]:
        raise SourceV6SurfaceError("surface identity or frozen facts changed", code="SURFACE_CHANGED")
    attrs = getattr(pipeline_input, "points").attrs
    if attrs.get("event_mode") != SOURCE_V6_EVENT_MODE:
        raise SourceV6SurfaceError("v6 analysis requires real_independent_events", code="UNSUPPORTED_EVENT_MODE")


def run_source_v6_analysis(
    path: str | Path,
    pipeline_input: object,
    config: object,
    *,
    algorithm_version: str,
    listing_dates: Mapping[str, object],
    listing_dates_sha256: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Run existing selection stages and append one immutable v6-backed run."""
    from .pipeline import _analyze_points, _canonical as pipeline_canonical

    surface_path = Path(path)
    if surface_path.suffix.lower() != ".duckdb":
        raise SourceV6SurfaceError("v6 analysis runs require a DuckDB surface", code="INVALID_SURFACE")
    read_surface_db(surface_path)
    points = getattr(pipeline_input, "points", None)
    if not isinstance(points, pd.DataFrame):
        raise SourceV6SurfaceError("analysis input must be PipelineInput", code="INVALID_INPUT")
    attrs = points.attrs
    required_attrs = ("source_surface_id", "source_manifest_sha256", "source_frozen_facts_sha256", "compatibility_versions", "selected_scope", "selected_interval_ms", "event_mode")
    if any(name not in attrs for name in required_attrs):
        raise SourceV6SurfaceError("PipelineInput is missing v6 provenance", code="INVALID_INPUT")
    versions = attrs["compatibility_versions"]
    if not isinstance(versions, Mapping) or any(value in (None, "") for value in versions.values()):
        raise SourceV6SurfaceError("incomplete v6 compatibility tuple", code="INCOMPATIBLE_SURFACE")
    if attrs["event_mode"] != SOURCE_V6_EVENT_MODE:
        raise SourceV6SurfaceError("v6 analysis requires real_independent_events", code="UNSUPPORTED_EVENT_MODE")
    snapshot = _v6_listing_snapshot(listing_dates)
    snapshot_json = _canonical_json(snapshot)
    calculated_dates_hash = sha256(snapshot_json.encode("utf-8")).hexdigest()
    if listing_dates_sha256 is not None and listing_dates_sha256 != calculated_dates_hash:
        raise SourceV6SurfaceError("listing-date snapshot hash mismatch", code="LISTING_DATES_CHANGED")
    if not isinstance(algorithm_version, str) or not algorithm_version.strip():
        raise SourceV6SurfaceError("algorithm version must be non-empty", code="INVALID_ALGORITHM")
    config_value = _v6_jsonable(pipeline_canonical(config))
    config_hash = sha256(_canonical_json(config_value).encode("utf-8")).hexdigest()
    scope = _canonical_scope(attrs["selected_scope"])
    raw_interval = attrs["selected_interval_ms"]
    if not isinstance(raw_interval, Sequence) or len(raw_interval) != 2 or int(raw_interval[1]) <= int(raw_interval[0]):
        raise SourceV6SurfaceError("selected interval must be non-empty", code="INVALID_INTERVAL")
    interval = {"start_ms": int(raw_interval[0]), "end_ms": int(raw_interval[1])}
    expected = {
        "surface_id": str(attrs["source_surface_id"]),
        "manifest_sha256": str(attrs["source_manifest_sha256"]),
        "frozen_facts_sha256": str(attrs["source_frozen_facts_sha256"]),
    }
    identity = {
        "identity_version": "source-v6-analysis-run-id-v1",
        "surface_id": expected["surface_id"],
        "manifest_sha256": expected["manifest_sha256"],
        "frozen_facts_sha256": expected["frozen_facts_sha256"],
        "compatibility_versions": _v6_jsonable(dict(sorted(versions.items()))),
        "selected_scope": {"symbol": scope[0], "side": scope[1], "timeframe": scope[2]},
        "selected_interval": interval,
        "event_mode": SOURCE_V6_EVENT_MODE,
        "algorithm_version": algorithm_version,
        "algorithm_config_sha256": config_hash,
        "listing_dates_sha256": calculated_dates_hash,
    }
    identity_json = _canonical_json(identity)
    run_id = sha256(identity_json.encode("utf-8")).hexdigest()
    metadata = {
        **identity,
        "source_surface_id": expected["surface_id"],
        "source_manifest_sha256": expected["manifest_sha256"],
        "source_frozen_facts_sha256": expected["frozen_facts_sha256"],
        "analysis_run_id": run_id,
        "algorithm_config": config_value,
        "listing_dates": snapshot,
        "canonical_identity_bytes": identity_json,
        "canonical_identity_json": identity_json,
        "attempt_state": "COMMITTED",
        "attempt_state_model": ["REQUESTED", "VALIDATED", "RUNNING", "COMMITTED", "FAILED", "CANCELLED"],
    }
    connection = duckdb.connect(str(surface_path))
    attempt_id = uuid4().hex
    metadata["attempt_id"] = attempt_id
    metadata["state"] = "COMMITTED"
    transaction_started = False
    try:
        _ensure_analysis_schema(connection)
        _v6_run_validation(connection, pipeline_input, expected=expected)
        existing = connection.execute("select identity_json, result_json, state from analysis_runs where run_id = ?", [run_id]).fetchone()
        if existing is not None:
            if existing[0] != identity_json:
                raise SourceV6SurfaceError("analysis run identity collision", code="RUN_ID_COLLISION")
            reuse_metadata = dict(metadata)
            reuse_metadata["attempt_state"] = "IDEMPOTENT_REUSE"
            reuse_metadata["reuse_of_attempt_id"] = json.loads(existing[1]).get("metadata", {}).get("attempt_id")
            reuse_json = json.dumps(reuse_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            now = datetime.now(timezone.utc).isoformat()
            connection.execute("insert into analysis_run_attempts values (?, ?, ?, ?, ?, ?)", [f"{attempt_id}:REQUESTED", run_id, "REQUESTED", now, "IDEMPOTENT_REUSE", reuse_json])
            connection.execute("insert into analysis_run_attempts values (?, ?, ?, ?, ?, ?)", [f"{attempt_id}:COMMITTED", run_id, "COMMITTED", now, "IDEMPOTENT_REUSE", reuse_json])
            return json.loads(existing[1])
        requested_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "insert into analysis_run_attempts values (?, ?, ?, ?, ?, ?)",
            [f"{attempt_id}:REQUESTED", run_id, "REQUESTED", now, None, requested_json],
        )
        if cancel_check is not None and cancel_check():
            raise SourceV6SurfaceError("analysis run cancelled", code="RUN_CANCELLED")
        connection.execute("begin")
        transaction_started = True
        _v6_run_validation(connection, pipeline_input, expected=expected)
        stage_points = points.copy()
        stage_points["listing_date"] = pd.to_datetime(stage_points["symbol"].map(snapshot), utc=True)
        if stage_points["listing_date"].isna().any():
            missing = sorted(set(stage_points.loc[stage_points["listing_date"].isna(), "symbol"]))
            raise SourceV6SurfaceError(f"missing listing dates: {missing}", code="INVALID_LISTING_DATES")
        if cancel_check is not None and cancel_check():
            raise SourceV6SurfaceError("analysis run cancelled", code="RUN_CANCELLED")
        stages = _analyze_points(stage_points, config)
        tables = _v6_run_tables(stages, stage_points, attrs)
        output = {"analysis_run_id": run_id, "state": "COMMITTED", "event_mode": SOURCE_V6_EVENT_MODE, "selected_interval": interval, "metadata": metadata, "facts": tables}
        result_json = json.dumps(_v6_jsonable(output), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection.execute("insert into analysis_runs(run_id, created_at_utc, result_json, state, metadata_json, identity_json, attempt_id) values (?, ?, ?, ?, ?, ?, ?)", [run_id, datetime.now(timezone.utc).isoformat(), result_json, "COMMITTED", json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")), identity_json, attempt_id])
        for name, rows in tables.items():
            connection.execute("insert into analysis_run_facts values (?, ?, ?)", [run_id, name, json.dumps(_v6_jsonable(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":"))])
        now = datetime.now(timezone.utc).isoformat()
        for state in ("VALIDATED", "RUNNING", "COMMITTED"):
            connection.execute("insert into analysis_run_attempts values (?, ?, ?, ?, ?, ?)", [f"{attempt_id}:{state}", run_id, state, now, None, json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))])
        if cancel_check is not None and cancel_check():
            raise SourceV6SurfaceError("analysis run cancelled", code="RUN_CANCELLED")
        # DuckDB rejects a read-only connection while this writable handle is
        # open; a separate default connection still gives the validation a
        # fresh MVCC snapshot before the commit.
        fresh_validation = duckdb.connect(str(surface_path))
        try:
            _v6_run_validation(fresh_validation, pipeline_input, expected=expected)
        finally:
            fresh_validation.close()
        connection.execute("commit")
        transaction_started = False
        return output
    except Exception as error:
        if transaction_started:
            connection.execute("rollback")
            transaction_started = False
        try:
            _ensure_analysis_schema(connection)
            attempt_state = "CANCELLED" if getattr(error, "code", None) == "RUN_CANCELLED" else "FAILED"
            failed_metadata = dict(metadata)
            failed_metadata["attempt_state"] = attempt_state
            failed_metadata["state"] = attempt_state
            connection.execute("insert into analysis_run_attempts values (?, ?, ?, ?, ?, ?)", [f"{attempt_id}:{attempt_state}", run_id, attempt_state, datetime.now(timezone.utc).isoformat(), str(error), json.dumps(failed_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))])
        except Exception:
            pass
        raise
    finally:
        connection.close()


append_source_v6_analysis_run = run_source_v6_analysis
append_v6_analysis_run = run_source_v6_analysis


def list_source_v6_analysis_runs(path: str | Path) -> tuple[dict[str, object], ...]:
    read_surface_db(path)
    connection = duckdb.connect(str(path), read_only=True)
    try:
        rows = connection.execute("select result_json from analysis_runs where state='COMMITTED' order by run_id").fetchall()
        return tuple(json.loads(row[0]) for row in rows)
    finally:
        connection.close()


def read_source_v6_analysis_run(path: str | Path, analysis_run_id: str) -> dict[str, object]:
    read_surface_db(path)
    connection = duckdb.connect(str(path), read_only=True)
    try:
        row = connection.execute("select result_json from analysis_runs where run_id=? and state='COMMITTED'", [analysis_run_id]).fetchone()
        if row is None:
            raise SourceV6SurfaceError("analysis run not found", code="RUN_NOT_FOUND")
        return json.loads(row[0])
    finally:
        connection.close()


get_source_v6_analysis_run = read_source_v6_analysis_run
