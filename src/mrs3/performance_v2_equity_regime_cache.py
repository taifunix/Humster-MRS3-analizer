"""Canonical persistence helpers for versioned equity-regime assessments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from typing import Mapping

import duckdb

from .performance_v2_equity_cache import (
    EquityQualityCacheError,
    EquitySourceChangedError,
    current_equity_source_metadata,
    equity_source_revision,
)
from .performance_v2_equity_regime import (
    ALGORITHM_VERSION,
    EquityRegimeAssessment,
    EquityRegimeFacts,
    EquityRegimeWindowFacts,
)


_MAX_JSON_BYTES = 65_536
_MAX_INT = (1 << 63) - 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CODE_RE = re.compile(r"[A-Z0-9_]{1,100}\Z")
_FACT_KEYS = frozenset(
    {
        "algo_version", "result_id", "report_start_utc", "report_end_utc",
        "raw_sample_count", "invalid_reasons", "windows", "pre28", "dd14",
        "dd7", "hwm_t28", "hwm_t14", "hwm_t7", "hwm_t",
        "hwm_t28_time_utc", "hwm_t14_time_utc", "hwm_t7_time_utc",
        "hwm_t_time_utc", "previous_ath_w7", "previous_ath_w7_time_utc",
        "ath_stage_counts", "ath_stage_event_times_utc", "ath_stage_event_values",
        "ath_stage_strict_increase", "new_ath_w7", "held_w7_breakout", "final_equity",
    }
)
_WINDOW_KEYS = frozenset(
    {"days", "start_utc", "end_utc", "elapsed_days", "start_equity", "end_equity", "v", "p", "direction", "grid_points"}
)
_ASSESSMENT_KEYS = frozenset({"state", "decision", "rank", "reasons", "facts"})
_INVALID_REASONS = (
    "INVALID_REPORT_START_UTC", "INVALID_REPORT_END_UTC", "INVALID_REPORT_RANGE",
    "MALFORMED_EQUITY_SAMPLE", "ROW_OWNERSHIP_MISMATCH", "INVALID_SAMPLE_INDEX",
    "INVALID_EQUITY_TIMESTAMP_UTC", "MALFORMED_OR_NONFINITE_EQUITY",
    "NONPOSITIVE_EQUITY", "UNORDERED_EQUITY_SOURCE", "EQUITY_OUTSIDE_REPORT_INTERVAL",
    "W28_UNAVAILABLE",
)
_DIRECTIONS = frozenset({"UP", "DOWN", "FLAT", "MIXED"})
_STATES = frozenset({"GROWING", "WEAKENING", "RESUMED", "STALLED", "DROP", "NOT_EVALUATED"})
_DECISIONS = frozenset({"PASS", "DROP", "NOT_EVALUATED"})
_RANKS = frozenset({"GROWING", "WEAKENING", "RESUMED", "RESERVED"})


class EquityRegimeCacheError(ValueError):
    """Raised when an equity-regime payload or cache input is invalid."""


class EquityRegimeSourceChangedError(EquityRegimeCacheError):
    """Raised when source metadata differs from the assessed result."""


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _fail_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _json(payload: str) -> object:
    if not isinstance(payload, str):
        raise EquityRegimeCacheError("equity-regime JSON is invalid")
    try:
        if len(payload.encode("utf-8")) > _MAX_JSON_BYTES:
            raise EquityRegimeCacheError("equity-regime JSON is too large")
        return json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_fail_constant)
    except (UnicodeEncodeError, TypeError, ValueError) as error:
        if isinstance(error, EquityRegimeCacheError):
            raise
        raise EquityRegimeCacheError("equity-regime JSON is invalid") from error


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_INT:
        raise EquityRegimeCacheError(f"{field} must be an integer in range")
    return value


def _decimal(value: object, field: str, *, optional: bool = False) -> Decimal | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or len(value) > 1024:
        raise EquityRegimeCacheError(f"{field} must be a finite Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise EquityRegimeCacheError(f"{field} must be a finite Decimal string") from error
    if not parsed.is_finite() or format(parsed, "f") != value or len(parsed.as_tuple().digits) > 38:
        raise EquityRegimeCacheError(f"{field} must be a canonical finite Decimal")
    return parsed


def _timestamp(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EquityRegimeCacheError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise EquityRegimeCacheError(f"{field} must be a canonical UTC timestamp") from error
    if parsed.utcoffset() != timedelta(0) or _timestamp_text(parsed) != value:
        raise EquityRegimeCacheError(f"{field} must be a canonical UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE_RE.fullmatch(value) is None:
        raise EquityRegimeCacheError(f"{field} must be a stable code")
    return value


def _codes(value: object, field: str, *, allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EquityRegimeCacheError(f"{field} must be an array of stable codes")
    values = tuple(_code(code, field) for code in value)
    if len(set(values)) != len(values):
        raise EquityRegimeCacheError(f"{field} contains duplicates")
    if allowed is not None and any(code not in allowed for code in values):
        raise EquityRegimeCacheError(f"{field} contains an unknown code")
    if allowed is not None and tuple(sorted(values, key=allowed.index)) != values:
        raise EquityRegimeCacheError(f"{field} is not in canonical order")
    return values


def _window(value: object, expected_days: int | None = None) -> EquityRegimeWindowFacts | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _WINDOW_KEYS:
        raise EquityRegimeCacheError("equity-regime window keys do not match the schema")
    days = _integer(value["days"], "window.days", minimum=1)
    if expected_days is not None and days != expected_days:
        raise EquityRegimeCacheError("equity-regime window horizon is invalid")
    start = _timestamp(value["start_utc"], "window.start_utc")
    end = _timestamp(value["end_utc"], "window.end_utc")
    elapsed = _decimal(value["elapsed_days"], "window.elapsed_days")
    start_equity = _decimal(value["start_equity"], "window.start_equity")
    end_equity = _decimal(value["end_equity"], "window.end_equity")
    trend = _decimal(value["v"], "window.v")
    endpoint = _decimal(value["p"], "window.p")
    direction = value["direction"]
    grid_points = _integer(value["grid_points"], "window.grid_points", minimum=1)
    assert start is not None and end is not None and elapsed is not None
    assert start_equity is not None and end_equity is not None and trend is not None and endpoint is not None
    if not isinstance(direction, str) or direction not in _DIRECTIONS:
        raise EquityRegimeCacheError("equity-regime direction is invalid")
    delta = end - start
    with localcontext(Context(prec=38)):
        actual_elapsed = Decimal(delta.days * 86400 + delta.seconds) / Decimal(86400)
        actual_elapsed += Decimal(delta.microseconds) / Decimal(86_400_000_000)
    if elapsed != actual_elapsed or elapsed <= 0 or start_equity <= 0 or end_equity <= 0:
        raise EquityRegimeCacheError("equity-regime window metrics are inconsistent")
    if expected_days is not None and (elapsed != expected_days or grid_points != expected_days * 4 + 1):
        raise EquityRegimeCacheError("equity-regime window interval is invalid")
    if expected_days is None and days != int(elapsed):
        raise EquityRegimeCacheError("PRE28 horizon does not match its interval")
    return EquityRegimeWindowFacts(days, start, end, elapsed, start_equity, end_equity, trend, endpoint, direction, grid_points)


def _facts(document: object) -> EquityRegimeFacts:
    if not isinstance(document, dict) or set(document) != _FACT_KEYS:
        raise EquityRegimeCacheError("equity-regime facts keys do not match the schema")
    if document["algo_version"] != ALGORITHM_VERSION:
        raise EquityRegimeCacheError("equity-regime algorithm version mismatch")
    result_id = _integer(document["result_id"], "result_id", minimum=1)
    start = _timestamp(document["report_start_utc"], "report_start_utc", optional=True)
    end = _timestamp(document["report_end_utc"], "report_end_utc", optional=True)
    raw_count = _integer(document["raw_sample_count"], "raw_sample_count")
    reasons = _codes(document["invalid_reasons"], "invalid_reasons", allowed=_INVALID_REASONS)
    windows_doc = document["windows"]
    if not isinstance(windows_doc, dict) or set(windows_doc) != {"28", "14", "7"}:
        raise EquityRegimeCacheError("equity-regime windows are invalid")
    w28, w14, w7 = (_window(windows_doc[key], days) for key, days in (("28", 28), ("14", 14), ("7", 7)))
    if bool(w28) != bool(w14) or bool(w14) != bool(w7):
        raise EquityRegimeCacheError("equity-regime window availability is inconsistent")
    pre28 = _window(document["pre28"])
    if any(window is not None and (end is None or window.end_utc != end) for window in (w28, w14, w7)):
        raise EquityRegimeCacheError("equity-regime window endpoint does not match the report")
    for window, days in ((w28, 28), (w14, 14), (w7, 7)):
        if window is not None and window.start_utc != end - timedelta(days=days):
            raise EquityRegimeCacheError("equity-regime window bounds are invalid")
    decimals = tuple(
        _decimal(document[key], key, optional=True)
        for key in ("dd14", "dd7", "hwm_t28", "hwm_t14", "hwm_t7", "hwm_t", "previous_ath_w7", "final_equity")
    )
    dd14, dd7, hwm28, hwm14, hwm7, hwm, previous_ath, final_equity = decimals
    if any(value is not None and value < 0 for value in (dd14, dd7)):
        raise EquityRegimeCacheError("equity-regime drawdown cannot be negative")
    times = tuple(
        _timestamp(document[key], key, optional=True)
        for key in ("hwm_t28_time_utc", "hwm_t14_time_utc", "hwm_t7_time_utc", "hwm_t_time_utc")
    )
    previous_ath_time = _timestamp(document["previous_ath_w7_time_utc"], "previous_ath_w7_time_utc", optional=True)
    if previous_ath_time != times[2]:
        raise EquityRegimeCacheError("previous weekly ATH time is inconsistent")
    counts_doc = document["ath_stage_counts"]
    event_doc = document["ath_stage_event_times_utc"]
    value_doc = document["ath_stage_event_values"]
    if (
        not isinstance(counts_doc, list) or len(counts_doc) != 3
        or not isinstance(event_doc, list) or len(event_doc) != 3
        or not isinstance(value_doc, list) or len(value_doc) != 3
    ):
        raise EquityRegimeCacheError("equity-regime ATH stage evidence is invalid")
    counts = tuple(_integer(count, "ath_stage_counts") for count in counts_doc)
    events_list: list[tuple[datetime, ...]] = []
    values_list: list[tuple[Decimal, ...]] = []
    for count, stage, stage_values in zip(counts, event_doc, value_doc):
        if not isinstance(stage, list) or not isinstance(stage_values, list):
            raise EquityRegimeCacheError("equity-regime ATH event times are invalid")
        stamps = tuple(_timestamp(value, "ath_stage_event_times_utc") for value in stage)
        event_values = tuple(_decimal(value, "ath_stage_event_values") for value in stage_values)
        if (
            any(stamp is None for stamp in stamps) or tuple(sorted(stamps)) != stamps
            or len(stamps) != count or len(event_values) != count
            or any(value is None or value <= 0 for value in event_values)
        ):
            raise EquityRegimeCacheError("equity-regime ATH event times are inconsistent")
        events_list.append(stamps)  # type: ignore[arg-type]
        values_list.append(event_values)  # type: ignore[arg-type]
    flags = tuple(document[key] for key in ("ath_stage_strict_increase", "new_ath_w7", "held_w7_breakout"))
    if any(type(flag) is not bool for flag in flags):
        raise EquityRegimeCacheError("equity-regime ATH flags must be booleans")
    facts = EquityRegimeFacts(
        ALGORITHM_VERSION, result_id, start, end, raw_count, reasons, w28, w14, w7, pre28,
        dd14, dd7, hwm28, hwm14, hwm7, hwm,
        *times, previous_ath, counts, tuple(events_list), tuple(values_list), *flags, final_equity,
    )
    if w28 is not None and (final_equity is None or any(value is None for value in (hwm28, hwm14, hwm7, hwm))):
        raise EquityRegimeCacheError("equity-regime W28 evidence is incomplete")
    return facts


def _assessment(document: object) -> EquityRegimeAssessment:
    if not isinstance(document, dict) or set(document) != _ASSESSMENT_KEYS:
        raise EquityRegimeCacheError("equity-regime assessment keys do not match the schema")
    state, decision, rank = document["state"], document["decision"], document["rank"]
    if (
        not isinstance(state, str) or state not in _STATES
        or not isinstance(decision, str) or decision not in _DECISIONS
        or (rank is not None and (not isinstance(rank, str) or rank not in _RANKS))
    ):
        raise EquityRegimeCacheError("equity-regime assessment disposition is invalid")
    reasons = _codes(document["reasons"], "assessment.reasons")
    return EquityRegimeAssessment(state, decision, rank, reasons, _facts(document["facts"]))


def _encode(value: EquityRegimeFacts | EquityRegimeAssessment) -> str:
    if isinstance(value, EquityRegimeFacts):
        document = value.to_canonical_dict()
        checked: object = _facts(document)
    elif isinstance(value, EquityRegimeAssessment):
        document = value.to_canonical_dict()
        checked = _assessment(document)
    else:
        raise EquityRegimeCacheError("equity-regime payload type is invalid")
    if checked != value:
        raise EquityRegimeCacheError("equity-regime payload is not internally consistent")
    payload = _canonical(document)
    if len(payload.encode("utf-8")) > _MAX_JSON_BYTES:
        raise EquityRegimeCacheError("equity-regime JSON is too large")
    return payload


def _decode(payload: str, digest: str, decoder, expected_result_id: int | None = None):
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise EquityRegimeCacheError("equity-regime digest is malformed")
    try:
        encoded = payload.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as error:
        raise EquityRegimeCacheError("equity-regime JSON is invalid") from error
    if len(encoded) > _MAX_JSON_BYTES:
        raise EquityRegimeCacheError("equity-regime JSON is too large")
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise EquityRegimeCacheError("equity-regime digest mismatch")
    document = _json(payload)
    result = decoder(document)
    result_id = result.facts.result_id if isinstance(result, EquityRegimeAssessment) else result.result_id
    if expected_result_id is not None and result_id != _integer(expected_result_id, "expected_result_id", minimum=1):
        raise EquityRegimeCacheError("equity-regime result_id mismatch")
    if _canonical(result.to_canonical_dict()) != payload:
        raise EquityRegimeCacheError("equity-regime JSON is not canonical")
    return result


def encode_equity_regime_facts(facts: EquityRegimeFacts) -> str:
    return _encode(facts)


def decode_equity_regime_facts(
    payload: str, digest: str, *, expected_result_id: int | None = None
) -> EquityRegimeFacts:
    return _decode(payload, digest, _facts, expected_result_id)


def encode_equity_regime_assessment(assessment: EquityRegimeAssessment) -> str:
    return _encode(assessment)


def decode_equity_regime_assessment(
    payload: str, digest: str, *, expected_result_id: int | None = None
) -> EquityRegimeAssessment:
    return _decode(payload, digest, _assessment, expected_result_id)


def equity_regime_source_revision(metadata: Mapping[str, object]) -> str:
    try:
        return equity_source_revision(metadata)
    except EquityQualityCacheError as error:
        raise EquityRegimeCacheError(str(error)) from error


def read_equity_regime_facts(
    connection: duckdb.DuckDBPyConnection, result_id: int, expected_source_revision: str
) -> EquityRegimeFacts | None:
    result_id = _integer(result_id, "result_id", minimum=1)
    if not isinstance(expected_source_revision, str) or _SHA256_RE.fullmatch(expected_source_revision) is None:
        raise EquityRegimeCacheError("expected source revision is malformed")
    row = connection.execute(
        """select source_revision, algo_version, facts_json, facts_sha256
             from equity_quality_metrics where result_id = ? and algo_version = ?""",
        [result_id, ALGORITHM_VERSION],
    ).fetchone()
    if row is None:
        return None
    source_revision, algo_version, payload, digest = row
    if source_revision != expected_source_revision or algo_version != ALGORITHM_VERSION:
        return None
    try:
        facts = decode_equity_regime_facts(payload, digest, expected_result_id=result_id)
    except EquityRegimeCacheError:
        return None
    return facts if facts.result_id == result_id else None


def upsert_equity_regime_facts_checked(
    connection: duckdb.DuckDBPyConnection,
    metadata: Mapping[str, object],
    facts: EquityRegimeFacts,
    *,
    calculated_at_utc: datetime,
) -> str:
    if not isinstance(metadata, Mapping) or not isinstance(facts, EquityRegimeFacts):
        raise EquityRegimeCacheError("equity-regime publication inputs are invalid")
    result_id = _integer(metadata.get("result_id"), "result_id", minimum=1)
    if facts.result_id != result_id:
        raise EquityRegimeCacheError("facts result_id does not match source metadata")
    if (
        not isinstance(calculated_at_utc, datetime)
        or calculated_at_utc.tzinfo is None
        or calculated_at_utc.utcoffset() != timedelta(0)
    ):
        raise EquityRegimeCacheError("calculated_at_utc must be timezone-aware UTC")
    source_revision = equity_regime_source_revision(metadata)
    try:
        current = current_equity_source_metadata(connection, result_id)
        current_revision = equity_regime_source_revision(current)
    except (EquityQualityCacheError, EquitySourceChangedError) as error:
        raise EquityRegimeSourceChangedError("EQUITY_SOURCE_CHANGED") from error
    if current_revision != source_revision:
        raise EquityRegimeSourceChangedError("EQUITY_SOURCE_CHANGED")
    if facts.report_start_utc != metadata.get("report_start_utc") or facts.report_end_utc != metadata.get("report_end_utc"):
        raise EquityRegimeSourceChangedError("EQUITY_SOURCE_CHANGED")
    payload = encode_equity_regime_facts(facts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    connection.execute(
        """insert into equity_quality_metrics
               (result_id, source_revision, algo_version, facts_json, facts_sha256, calculated_at_utc)
           values (?, ?, ?, ?, ?, ?)
           on conflict (result_id, algo_version) do update set
               source_revision = excluded.source_revision,
               facts_json = excluded.facts_json,
               facts_sha256 = excluded.facts_sha256,
               calculated_at_utc = excluded.calculated_at_utc""",
        [result_id, source_revision, ALGORITHM_VERSION, payload, digest, calculated_at_utc.astimezone(timezone.utc)],
    )
    return source_revision


__all__ = [
    "ALGORITHM_VERSION",
    "EquityRegimeCacheError",
    "EquityRegimeSourceChangedError",
    "decode_equity_regime_assessment",
    "decode_equity_regime_facts",
    "encode_equity_regime_assessment",
    "encode_equity_regime_facts",
    "equity_regime_source_revision",
    "read_equity_regime_facts",
    "upsert_equity_regime_facts_checked",
]
