"""Strict persistence helpers for Performance v2 equity-quality facts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Iterable, Mapping

import duckdb

from .performance_v2_equity_quality import (
    ALGORITHM_VERSION,
    EquityQualityFacts,
    EquityWindowFacts,
)
from .performance_v2_store import (
    PerformanceV2StoreError,
    decode_optimizer_source_metadata,
    require_performance_v2,
    require_performance_v2_readable,
)


_FACT_KEYS = frozenset(
    {
        "algo_version",
        "result_id",
        "report_start_utc",
        "report_end_utc",
        "state",
        "reason",
        "erf_disposition",
        "raw_sample_count",
        "in_report_sample_count",
        "nonpositive_in_report_rows",
        "duplicate_timestamp_count",
        "invalid_reasons",
        "available_baselines_days",
        "horizon_days",
        "equity_class",
        "windows",
        "drawdown",
        "peak_gap",
        "raw_h_path_points",
        "score12",
    }
)
_WINDOW_KEYS = frozenset(
    {"days", "start_utc", "end_utc", "trend30", "endpoint30", "return_pct", "er", "grid_points"}
)
_STATES = frozenset(
    {
        "UNKNOWN_INVALID_SOURCE",
        "NONPOSITIVE_EQUITY",
        "INSUFFICIENT_HISTORY",
        "MISSING_BASELINE",
        "GROWING",
        "WEAKENING",
        "FLAT",
        "DECLINING_OR_MIXED",
    }
)
_DISPOSITIONS = frozenset({"NOT_EVALUATED", "PASS", "BLOCK", "BLOCK_IF_ERF_ENABLED"})
_WINDOW_DAYS = (7, 14, 28)
_MAX_COUNT = (1 << 63) - 1
_MAX_JSON_BYTES = 65_536
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CODE_RE = re.compile(r"[A-Z0-9_]{1,100}\Z")


class EquityQualityCacheError(ValueError):
    """Raised when equity facts or their source/catalog metadata are invalid."""


class EquitySourceChangedError(EquityQualityCacheError):
    """A result changed between source read and cache publication."""


def _fail_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(payload: str) -> object:
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > _MAX_JSON_BYTES:
        raise EquityQualityCacheError("equity facts JSON is invalid or too large")
    try:
        return json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_fail_constant)
    except (UnicodeEncodeError, TypeError, ValueError) as error:
        raise EquityQualityCacheError("equity facts JSON is invalid") from error


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _utc_datetime(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EquityQualityCacheError(f"{field} must be timezone-aware UTC")
    try:
        if value.utcoffset() != timedelta(0):
            raise EquityQualityCacheError(f"{field} must be timezone-aware UTC")
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError) as error:
        if isinstance(error, EquityQualityCacheError):
            raise
        raise EquityQualityCacheError(f"{field} must be timezone-aware UTC") from error


def _timestamp_text(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EquityQualityCacheError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise EquityQualityCacheError(f"{field} must be a canonical UTC timestamp") from error
    normalized = _utc_datetime(parsed, field)
    if _timestamp_text(normalized) != value:
        raise EquityQualityCacheError(f"{field} must be a canonical UTC timestamp")
    return normalized


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _parse_decimal(value: object, field: str, *, optional: bool = False) -> Decimal | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or len(value) > 1024:
        raise EquityQualityCacheError(f"{field} must be a finite canonical Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise EquityQualityCacheError(f"{field} must be a finite canonical Decimal string") from error
    if not parsed.is_finite() or _decimal_text(parsed) != value:
        raise EquityQualityCacheError(f"{field} must be a finite canonical Decimal string")
    if len(parsed.as_tuple().digits) > 38:
        raise EquityQualityCacheError(f"{field} exceeds the supported Decimal precision")
    return parsed


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_COUNT:
        raise EquityQualityCacheError(f"{field} must be an integer in range")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE_RE.fullmatch(value) is None:
        raise EquityQualityCacheError(f"{field} must be a stable code")
    return value


def _source_report_hash(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise EquityQualityCacheError("source_report_sha256 is malformed")
    return value


def equity_source_revision(metadata: Mapping[str, object]) -> str:
    """Hash the stable result/import/window identity used by the cache."""
    if not isinstance(metadata, Mapping):
        raise EquityQualityCacheError("equity source metadata must be a mapping")
    result_id = _integer(metadata.get("result_id"), "result_id", minimum=1)
    timestamps = {
        name: _utc_datetime(metadata.get(name), name, optional=name.startswith("effective_"))
        for name in (
            "imported_at_utc",
            "report_start_utc",
            "report_end_utc",
            "effective_start_utc",
            "effective_end_utc",
        )
    }
    if timestamps["report_end_utc"] < timestamps["report_start_utc"]:
        raise EquityQualityCacheError("report bounds are invalid")
    if (
        timestamps["effective_start_utc"] is not None
        and timestamps["effective_end_utc"] is not None
        and timestamps["effective_end_utc"] < timestamps["effective_start_utc"]
    ):
        raise EquityQualityCacheError("effective bounds are invalid")
    stamps = {name: _timestamp_text(value) for name, value in timestamps.items()}
    raw_metadata = metadata.get("optimizer_source_metadata_json")
    source_hash: str | None = None
    if raw_metadata is not None:
        try:
            source_document = decode_optimizer_source_metadata(raw_metadata, timestamps["imported_at_utc"])
        except UnicodeEncodeError:
            source_document = None
        if source_document is not None:
            try:
                source_hash = _source_report_hash(source_document.get("source_report_sha256"))
            except EquityQualityCacheError:
                # Store decoding intentionally treats this metadata as optional.
                source_hash = None
    identity = {
        "result_id": result_id,
        **stamps,
        "source_report_sha256": source_hash,
    }
    return hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()


def _validate_document(document: object) -> EquityQualityFacts:
    if not isinstance(document, dict) or set(document) != _FACT_KEYS:
        raise EquityQualityCacheError("equity facts keys do not match the canonical schema")
    if document["algo_version"] != ALGORITHM_VERSION:
        raise EquityQualityCacheError("equity facts algorithm version mismatch")
    result_id = _integer(document["result_id"], "result_id", minimum=1)
    report_start = _parse_timestamp(document["report_start_utc"], "report_start_utc", optional=True)
    report_end = _parse_timestamp(document["report_end_utc"], "report_end_utc", optional=True)
    if (
        report_start is not None
        and report_end is not None
        and report_end < report_start
        and document["state"] != "UNKNOWN_INVALID_SOURCE"
    ):
        raise EquityQualityCacheError("report bounds are invalid")
    state = document["state"]
    disposition = document["erf_disposition"]
    if (
        not isinstance(state, str)
        or state not in _STATES
        or not isinstance(disposition, str)
        or disposition not in _DISPOSITIONS
    ):
        raise EquityQualityCacheError("equity facts state or disposition is invalid")
    reason = _code(document["reason"], "reason")
    raw_count = _integer(document["raw_sample_count"], "raw_sample_count")
    in_report_count = _integer(document["in_report_sample_count"], "in_report_sample_count")
    nonpositive_count = _integer(document["nonpositive_in_report_rows"], "nonpositive_in_report_rows")
    duplicate_count = _integer(document["duplicate_timestamp_count"], "duplicate_timestamp_count")
    if in_report_count > raw_count or nonpositive_count > in_report_count or duplicate_count > in_report_count:
        raise EquityQualityCacheError("equity facts sample counts are inconsistent")
    invalid_values = document["invalid_reasons"]
    if (
        not isinstance(invalid_values, list)
        or any(_code(item, "invalid_reason") != item for item in invalid_values)
        or invalid_values != sorted(set(invalid_values))
    ):
        raise EquityQualityCacheError("invalid_reasons must be sorted unique codes")
    baseline_values = document["available_baselines_days"]
    if (
        not isinstance(baseline_values, list)
        or any(type(item) is not int or item not in _WINDOW_DAYS for item in baseline_values)
        or baseline_values != sorted(set(baseline_values), reverse=True)
    ):
        raise EquityQualityCacheError("available_baselines_days is invalid")
    horizon = document["horizon_days"]
    if horizon is not None and (type(horizon) is not int or horizon not in _WINDOW_DAYS):
        raise EquityQualityCacheError("horizon_days is invalid")
    equity_class = document["equity_class"]
    if equity_class is not None and (type(equity_class) is not int or equity_class not in (0, 1, 2, 3)):
        raise EquityQualityCacheError("equity_class is invalid")
    windows_value = document["windows"]
    if not isinstance(windows_value, list):
        raise EquityQualityCacheError("windows must be an array")
    windows: list[EquityWindowFacts] = []
    for raw_window in windows_value:
        if not isinstance(raw_window, dict) or set(raw_window) != _WINDOW_KEYS:
            raise EquityQualityCacheError("equity window keys do not match the canonical schema")
        days = raw_window["days"]
        if type(days) is not int or days not in _WINDOW_DAYS:
            raise EquityQualityCacheError("window days are invalid")
        start = _parse_timestamp(raw_window["start_utc"], "window.start_utc")
        end = _parse_timestamp(raw_window["end_utc"], "window.end_utc")
        assert start is not None and end is not None
        if end - start != timedelta(days=days):
            raise EquityQualityCacheError("window interval does not match its horizon")
        trend = _parse_decimal(raw_window["trend30"], "trend30")
        endpoint = _parse_decimal(raw_window["endpoint30"], "endpoint30")
        returns = _parse_decimal(raw_window["return_pct"], "return_pct")
        er = _parse_decimal(raw_window["er"], "er")
        assert trend is not None and endpoint is not None and returns is not None and er is not None
        grid_points = _integer(raw_window["grid_points"], "grid_points", minimum=1)
        if grid_points != days * 4 + 1 or max(abs(trend), abs(endpoint)) > Decimal("1000000"):
            raise EquityQualityCacheError("window metric range is invalid")
        if abs(returns) > Decimal("1e41") or not Decimal(-1) <= er <= Decimal(1):
            raise EquityQualityCacheError("window metric range is invalid")
        windows.append(EquityWindowFacts(days, start, end, trend, endpoint, returns, er, grid_points))
    if [window.days for window in windows] != sorted({window.days for window in windows}):
        raise EquityQualityCacheError("windows are not in canonical order")
    if baseline_values:
        if (
            horizon != max(baseline_values)
            or baseline_values != [days for days in (28, 14, 7) if days <= horizon]
            or [window.days for window in windows] != [days for days in _WINDOW_DAYS if days <= horizon]
        ):
            raise EquityQualityCacheError("window availability does not match the horizon")
        if report_start is None or report_end is None or any(
            window.end_utc != report_end or window.start_utc < report_start for window in windows
        ):
            raise EquityQualityCacheError("window endpoint does not match the report endpoint")
    elif horizon is not None or windows:
        raise EquityQualityCacheError("metrics require an available baseline")
    drawdown = _parse_decimal(document["drawdown"], "drawdown", optional=True)
    peak_gap = _parse_decimal(document["peak_gap"], "peak_gap", optional=True)
    score = _parse_decimal(document["score12"], "score12", optional=True)
    if drawdown is not None and not Decimal(0) <= drawdown <= Decimal(1):
        raise EquityQualityCacheError("drawdown is out of range")
    if peak_gap is not None and not Decimal(0) <= peak_gap <= Decimal(1):
        raise EquityQualityCacheError("peak_gap is out of range")
    if score is not None and abs(score) > Decimal("1000000"):
        raise EquityQualityCacheError("score12 is out of range")
    raw_h_path_points = _integer(document["raw_h_path_points"], "raw_h_path_points")
    if raw_h_path_points > raw_count + 2:
        raise EquityQualityCacheError("raw_h_path_points is inconsistent")
    if state == "UNKNOWN_INVALID_SOURCE":
        if (
            not invalid_values
            or reason != "INVALID_OR_OUT_OF_INTERVAL_SOURCE"
            or (
                report_start is not None
                and report_end is not None
                and report_end < report_start
                and "INVALID_REPORT_RANGE" not in invalid_values
            )
            or disposition != "NOT_EVALUATED"
            or baseline_values
            or horizon is not None
            or equity_class is not None
            or windows
            or raw_h_path_points != 0
            or any(value is not None for value in (drawdown, peak_gap, score))
        ):
            raise EquityQualityCacheError("invalid source facts contain metrics")
    elif state == "NONPOSITIVE_EQUITY":
        if (
            invalid_values
            or nonpositive_count == 0
            or reason != "NONPOSITIVE_IN_REPORT_EQUITY"
            or disposition != "BLOCK"
            or baseline_values
            or horizon is not None
            or equity_class is not None
            or windows
            or raw_h_path_points != 0
            or any(value is not None for value in (drawdown, peak_gap, score))
        ):
            raise EquityQualityCacheError("nonpositive source facts are inconsistent")
    elif state in {"INSUFFICIENT_HISTORY", "MISSING_BASELINE"}:
        if (
            invalid_values
            or report_start is None
            or report_end is None
            or reason != state
            or (state == "INSUFFICIENT_HISTORY" and report_end - report_start >= timedelta(days=7))
            or (state == "MISSING_BASELINE" and report_end - report_start < timedelta(days=7))
            or baseline_values
            or horizon is not None
            or equity_class is not None
            or windows
            or raw_h_path_points != 0
            or disposition != "NOT_EVALUATED"
            or any(value is not None for value in (drawdown, peak_gap, score))
        ):
            raise EquityQualityCacheError("unavailable facts contain metrics")
    else:
        expected_state = {
            "GROWING": (0, "PASS", "H_UP_SHORTS_NONDECLINING"),
            "WEAKENING": (1, "PASS", "SHORT_WINDOW_DECLINE"),
            "FLAT": (2, "BLOCK_IF_ERF_ENABLED", "H_FLAT"),
        }
        class_contract = expected_state.get(state)
        if state == "DECLINING_OR_MIXED":
            class_ok = (
                (equity_class == 2 and reason == "H_NONDECLINING_NOT_UP")
                or (equity_class == 3 and reason == "H_DECLINING_OR_MIXED")
            )
            expected_disposition = "BLOCK"
            reason_ok = class_ok
        else:
            class_ok = class_contract is not None and equity_class == class_contract[0]
            expected_disposition = None if class_contract is None else class_contract[1]
            reason_ok = class_contract is not None and reason == class_contract[2]
        if (
            invalid_values
            or report_start is None
            or report_end is None
            or raw_count == 0
            or in_report_count == 0
            or not baseline_values
            or equity_class is None
            or not class_ok
            or disposition != expected_disposition
            or not reason_ok
            or raw_h_path_points == 0
            or score is None
            or score != score.quantize(Decimal("0.000000000001"))
            or drawdown is None
            or peak_gap is None
        ):
            raise EquityQualityCacheError("valid metric facts are incomplete")
    return EquityQualityFacts(
        ALGORITHM_VERSION,
        result_id,
        report_start,
        report_end,
        state,
        reason,
        disposition,
        raw_count,
        in_report_count,
        nonpositive_count,
        duplicate_count,
        tuple(invalid_values),
        tuple(baseline_values),
        horizon,
        equity_class,
        tuple(windows),
        drawdown,
        peak_gap,
        raw_h_path_points,
        score,
    )


def encode_equity_facts(facts: EquityQualityFacts) -> str:
    if not isinstance(facts, EquityQualityFacts):
        raise EquityQualityCacheError("facts must be EquityQualityFacts")
    try:
        document = facts.to_canonical_dict()
        validated = _validate_document(document)
        if validated != facts:
            raise EquityQualityCacheError("facts are not canonical")
        return _canonical(document)
    except Exception as error:
        if isinstance(error, EquityQualityCacheError):
            raise
        raise EquityQualityCacheError("facts cannot be encoded canonically") from error


def decode_equity_facts(payload: str, digest: str) -> EquityQualityFacts:
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise EquityQualityCacheError("equity facts digest is malformed")
    if not isinstance(payload, str):
        raise EquityQualityCacheError("equity facts JSON is invalid or too large")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EquityQualityCacheError("equity facts JSON is invalid") from error
    if len(encoded) > _MAX_JSON_BYTES:
        raise EquityQualityCacheError("equity facts JSON is invalid or too large")
    actual_digest = hashlib.sha256(encoded).hexdigest()
    if actual_digest != digest:
        raise EquityQualityCacheError("equity facts digest mismatch")
    document = _json(payload)
    facts = _validate_document(document)
    if _canonical(facts.to_canonical_dict()) != payload:
        raise EquityQualityCacheError("equity facts JSON is not canonical")
    return facts


def current_equity_source_metadata(
    connection: duckdb.DuckDBPyConnection, result_id: int
) -> dict[str, object]:
    result_id = _integer(result_id, "result_id", minimum=1)
    row = connection.execute(
        """select result_id, imported_at_utc, report_start_utc, report_end_utc,
                  effective_start_utc, effective_end_utc, optimizer_source_metadata_json
             from strategy_results where result_id = ?""",
        [result_id],
    ).fetchone()
    if row is None:
        raise EquitySourceChangedError("EQUITY_SOURCE_CHANGED")
    names = (
        "result_id",
        "imported_at_utc",
        "report_start_utc",
        "report_end_utc",
        "effective_start_utc",
        "effective_end_utc",
        "optimizer_source_metadata_json",
    )
    metadata = dict(zip(names, row))
    # DuckDB returns TIMESTAMPTZ in the process-local zone. The stored values
    # are instants, so normalize these database values before enforcing the
    # public source-revision contract's UTC-only representation.
    for name in (
        "imported_at_utc",
        "report_start_utc",
        "report_end_utc",
        "effective_start_utc",
        "effective_end_utc",
    ):
        value = metadata[name]
        if value is not None:
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise EquityQualityCacheError(f"{name} is not a valid database timestamp")
            metadata[name] = value.astimezone(timezone.utc)
    return metadata


def read_equity_quality_facts(
    connection: duckdb.DuckDBPyConnection, result_id: int, expected_source_revision: str
) -> EquityQualityFacts | None:
    try:
        version = require_performance_v2_readable(connection)
    except PerformanceV2StoreError as error:
        raise EquityQualityCacheError("Performance database schema is invalid") from error
    if version == 5:
        raise EquityQualityCacheError("EQUITY_SCHEMA_UPGRADE_REQUIRED")
    result_id = _integer(result_id, "result_id", minimum=1)
    if not isinstance(expected_source_revision, str) or _SHA256_RE.fullmatch(expected_source_revision) is None:
        raise EquityQualityCacheError("expected source revision is malformed")
    row = connection.execute(
        """select source_revision, algo_version, facts_json, facts_sha256
             from equity_quality_metrics where result_id = ? and algo_version = ?""",
        [result_id, ALGORITHM_VERSION],
    ).fetchone()
    if row is None:
        return None
    source_revision, algo_version, payload, digest = row
    if source_revision != expected_source_revision:
        return None
    if algo_version != ALGORITHM_VERSION:
        raise EquityQualityCacheError("equity cache algorithm version mismatch")
    facts = decode_equity_facts(payload, digest)
    if facts.result_id != result_id:
        raise EquityQualityCacheError("equity cache result_id mismatch")
    return facts


def _prepare_publications(
    items: Iterable[tuple[Mapping[str, object], EquityQualityFacts]], calculated_at_utc: datetime
) -> list[tuple[dict[str, object], EquityQualityFacts, str, str, str, datetime]]:
    calculated_at = _utc_datetime(calculated_at_utc, "calculated_at_utc")
    assert calculated_at is not None
    prepared: list[tuple[dict[str, object], EquityQualityFacts, str, str, str, datetime]] = []
    seen_result_ids: set[int] = set()
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise EquityQualityCacheError("publication entries must be (metadata, facts) pairs")
        metadata, facts = item
        if not isinstance(metadata, Mapping) or not isinstance(facts, EquityQualityFacts):
            raise EquityQualityCacheError("publication entry types are invalid")
        result_id = _integer(metadata.get("result_id"), "result_id", minimum=1)
        if facts.result_id != result_id:
            raise EquityQualityCacheError("facts result_id does not match source metadata")
        if result_id in seen_result_ids:
            raise EquityQualityCacheError("publication batch contains duplicate result_id")
        seen_result_ids.add(result_id)
        expected_start = _utc_datetime(metadata.get("report_start_utc"), "report_start_utc")
        expected_end = _utc_datetime(metadata.get("report_end_utc"), "report_end_utc")
        facts_start = _utc_datetime(facts.report_start_utc, "facts.report_start_utc", optional=True)
        facts_end = _utc_datetime(facts.report_end_utc, "facts.report_end_utc", optional=True)
        if facts_start != expected_start or facts_end != expected_end:
            raise EquityQualityCacheError("facts report bounds do not match source metadata")
        source_revision = equity_source_revision(metadata)
        payload = encode_equity_facts(facts)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        prepared.append((dict(metadata), facts, source_revision, payload, digest, calculated_at))
    return prepared


def upsert_equity_quality_facts_checked(
    connection: duckdb.DuckDBPyConnection,
    items: Iterable[tuple[Mapping[str, object], EquityQualityFacts]],
    *,
    calculated_at_utc: datetime,
) -> tuple[str, ...]:
    """Recheck a batch's sources, then upsert; caller owns the transaction."""
    prepared = _prepare_publications(items, calculated_at_utc)
    try:
        require_performance_v2(connection)
    except PerformanceV2StoreError as error:
        raise EquityQualityCacheError("Performance database schema is invalid") from error
    # Check every source before the first write so a stale member cannot
    # partially publish a caller's batch.
    for metadata, facts, source_revision, _payload, _digest, _calculated_at in prepared:
        current = current_equity_source_metadata(connection, facts.result_id)
        if equity_source_revision(current) != source_revision:
            raise EquitySourceChangedError("EQUITY_SOURCE_CHANGED")
    for _metadata, facts, source_revision, payload, digest, calculated_at in prepared:
        connection.execute(
            """insert into equity_quality_metrics
                   (result_id, source_revision, algo_version, facts_json, facts_sha256, calculated_at_utc)
               values (?, ?, ?, ?, ?, ?)
               on conflict (result_id, algo_version) do update set
                   source_revision = excluded.source_revision,
                   facts_json = excluded.facts_json,
                   facts_sha256 = excluded.facts_sha256,
                   calculated_at_utc = excluded.calculated_at_utc""",
            [facts.result_id, source_revision, ALGORITHM_VERSION, payload, digest, calculated_at],
        )
    return tuple(row[2] for row in prepared)


def publish_equity_quality_facts_batch(
    connection: duckdb.DuckDBPyConnection,
    items: Iterable[tuple[Mapping[str, object], EquityQualityFacts]],
    *,
    calculated_at_utc: datetime,
) -> tuple[str, ...]:
    """Publish a whole batch atomically on a standalone writer connection."""
    began = False
    try:
        connection.execute("begin transaction")
        began = True
        revisions = upsert_equity_quality_facts_checked(
            connection, items, calculated_at_utc=calculated_at_utc
        )
        connection.execute("commit")
        began = False
    except Exception as error:
        if began:
            try:
                connection.execute("rollback")
            except duckdb.Error:
                pass
        if isinstance(error, EquityQualityCacheError):
            raise
        raise EquityQualityCacheError("equity facts cache publication failed") from error
    return revisions


def publish_equity_quality_facts(
    connection: duckdb.DuckDBPyConnection,
    expected_metadata: Mapping[str, object],
    facts: EquityQualityFacts,
    *,
    calculated_at_utc: datetime,
) -> str:
    """Atomically publish one row through the same checked batch path."""
    return publish_equity_quality_facts_batch(
        connection,
        [(expected_metadata, facts)],
        calculated_at_utc=calculated_at_utc,
    )[0]


__all__ = [
    "EquityQualityCacheError",
    "EquitySourceChangedError",
    "current_equity_source_metadata",
    "decode_equity_facts",
    "encode_equity_facts",
    "equity_source_revision",
    "publish_equity_quality_facts",
    "publish_equity_quality_facts_batch",
    "read_equity_quality_facts",
    "upsert_equity_quality_facts_checked",
]
