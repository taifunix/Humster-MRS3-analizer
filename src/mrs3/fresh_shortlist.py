"""Validated, compact selection evidence for fresh MRS3 analyses."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from pathlib import Path
import sys
import threading
import time
from typing import Mapping, Sequence

import duckdb
import numpy as np


FILTER_VERSION = "shortlist-v2"
LEGACY_FILTER_ENGINE_VERSION = "shortlist-v2-engine-1"
FILTER_ENGINE_VERSION = "shortlist-v2-engine-2"
_MISSING = object()
_READY_STATUS = "READY_MRS3_STRUCTURE"
_TABLES = ("points", "structures", "plateaus")
_DEFAULT_CACHE_LIMIT = 256 * 1024 * 1024
# ponytail: this caps NumPy ndarray scratch, not Python ranking scratch or process RSS.
_DEFAULT_TEMP_ARRAY_LIMIT = 64 * 1024 * 1024
_DEFAULT_EVALUATION_LIMIT = 8
_MAX_IDENTICAL_FOLLOWERS = 4
_WAIT_TIMEOUT_SECONDS = 30.0
_MIN_PARALLEL_PARETO_WORK = 50_000


@dataclass(frozen=True, slots=True)
class FreshOrderMetrics:
    open_ma: int
    shift_bp: int
    point_id: str
    source_pnl_pct: Decimal
    source_dd_pct: Decimal
    plateau_point_count: int
    point_event_count: int


@dataclass(frozen=True, slots=True)
class FreshCandidateMetrics:
    candidate_id: str
    structure_id: str
    pair: str
    side: str
    timeframe: str
    common_close_ma: int
    order_count: int
    persisted_status: str
    orders: tuple[FreshOrderMetrics, ...]
    pretest_ab: tuple[tuple[str, object], ...] | None


@dataclass(frozen=True, slots=True)
class FreshScopeFacts:
    scope_key: str
    pair: str
    side: str
    timeframe: str
    plateau_count: int
    period: str | None


@dataclass(frozen=True, slots=True)
class PreparedFreshAnalysis:
    analysis_id: str
    artifact_sha256: str
    scopes: tuple[FreshScopeFacts, ...]
    candidates: tuple[FreshCandidateMetrics, ...]


@dataclass(frozen=True, slots=True)
class FreshCandidateResult:
    candidate_id: str
    structure_id: str
    scope_key: str
    order_count: int
    filter_status: str
    reason: str
    dominator_candidate_id: str | None
    pretest_ab_status: str
    pretest_ab_reason: str
    pretest_ab_decline_pct: str | None


@dataclass(frozen=True, slots=True)
class FreshScopeResult:
    scope_key: str
    pair: str
    side: str
    timeframe: str
    plateau_count: int
    period: str | None
    order_counts: tuple[int, int, int, int]
    ready: int
    deferred: int
    all_count: int
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FreshShortlistEvaluation:
    analysis_id: str
    artifact_sha256: str
    filter_version: str
    filter_engine_version: str
    options: tuple[bool, bool, bool]
    selection_token: str
    ready_candidate_ids: tuple[str, ...]
    candidates: tuple[FreshCandidateResult, ...]
    groups: tuple[FreshScopeResult, ...]
    min_shift_enabled: bool = False
    min_shift_pct: str | None = None


@dataclass(frozen=True, slots=True)
class FreshShortlistCacheInfo:
    prepared_bytes: int
    evaluation_count: int
    total_bytes: int


class ShortlistBusyError(RuntimeError):
    code = "SHORTLIST_BUSY"
    status_code = 409
    retry_after = 1

    def __init__(self) -> None:
        super().__init__(self.code)


def _raise_shared_error(error: Exception) -> None:
    try:
        follower_error = copy(error)
    except Exception:
        follower_error = error
    if follower_error is error:
        try:
            follower_error = type(error)(*error.args)
        except Exception:
            follower_error = RuntimeError(str(error))
    raise follower_error from error


def _object_graph_size(value: object, seen: set[int] | None = None) -> int:
    visited = seen if seen is not None else set()
    identity = id(value)
    if identity in visited:
        return 0
    visited.add(identity)
    size = sys.getsizeof(value)
    if is_dataclass(value) and not isinstance(value, type):
        return size + sum(_object_graph_size(getattr(value, item.name), visited) for item in fields(value))
    if isinstance(value, Mapping):
        return size + sum(
            _object_graph_size(key, visited) + _object_graph_size(item, visited)
            for key, item in value.items()
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return size + sum(_object_graph_size(item, visited) for item in value)
    if isinstance(value, np.ndarray):
        # Include owned data whether this NumPy version includes it in
        # getsizeof or reports just the ndarray header.
        return size if size >= value.nbytes else size + int(value.nbytes)
    return size


def _canonical_id(value: object, field: str) -> str:
    if isinstance(value, bool) or value is None or isinstance(value, (list, dict, tuple, set)):
        raise ValueError(f"fresh structure has invalid {field}")
    if isinstance(value, int):
        normalized = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"fresh structure has invalid {field}")
        normalized = str(int(value))
    elif isinstance(value, str):
        normalized = value.strip()
    else:
        raise ValueError(f"fresh structure has invalid {field}")
    if not normalized:
        raise ValueError(f"fresh structure has invalid {field}")
    return normalized


def _strict_int(value: object, field: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"fresh analysis has invalid {field}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    else:
        raise ValueError(f"fresh analysis has invalid {field}")
    if result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"fresh analysis has invalid {field}")
    return result


def _decimal(value: object, field: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"fresh analysis has invalid {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"fresh analysis has invalid {field}") from error
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError(f"fresh analysis has invalid {field}")
    return result


def _scope_key(row: Mapping[str, object], field: str) -> tuple[str, str, str]:
    pair, side, timeframe = row.get("symbol"), row.get("side"), row.get("timeframe")
    if not all(isinstance(value, str) and value.strip() for value in (pair, side, timeframe)):
        raise ValueError(f"fresh analysis {field} has invalid scope identity")
    return str(pair).strip(), str(side).strip().upper(), str(timeframe).strip()


def _manifest_scopes(manifest: Mapping[str, object]) -> dict[str, tuple[str, str, str]]:
    scope_digests = manifest.get("scope_digests")
    if not isinstance(scope_digests, Mapping) or not scope_digests:
        raise ValueError("fresh analysis scope identity is missing")
    scopes: dict[str, tuple[str, str, str]] = {}
    for raw_key, digest in scope_digests.items():
        if not isinstance(raw_key, str) or not isinstance(digest, str):
            raise ValueError("fresh analysis scope identity is malformed")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("fresh analysis scope identity is malformed")
        parts = tuple(raw_key.split("|"))
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise ValueError("fresh analysis scope identity is malformed")
        scope = (parts[0].strip(), parts[1].strip().upper(), parts[2].strip())
        key = "|".join(scope)
        if key != raw_key or key in scopes:
            raise ValueError("fresh analysis scope identity is malformed")
        scopes[key] = scope
    return dict(sorted(scopes.items()))


def _load_table(connection: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, dict[str, object]]]:
    try:
        cursor = connection.execute(f"select scope_key, payload_json from {table}")
    except duckdb.Error as error:
        raise ValueError(f"fresh analysis table {table} cannot be read") from error
    rows: list[tuple[str, dict[str, object]]] = []
    while batch := cursor.fetchmany(512):
        for raw_scope, raw_payload in batch:
            try:
                payload = json.loads(str(raw_payload))
            except (json.JSONDecodeError, TypeError) as error:
                raise ValueError(f"fresh analysis table {table} contains invalid JSON") from error
            if not isinstance(payload, dict):
                raise ValueError(f"fresh analysis table {table} contains a non-object row")
            rows.append((str(raw_scope), payload))
    return rows


def _period(points: Sequence[Mapping[str, object]]) -> str | None:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for point in points:
        for field, target in (("report_start", starts), ("report_end", ends)):
            value = point.get(field)
            if isinstance(value, str):
                try:
                    target.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
                except ValueError:
                    pass
    if not starts or not ends:
        return None
    return f"{min(starts):%d.%m}-{max(ends):%d.%m}"


def prepare_fresh_shortlist(
    analysis_path: Path | str,
    analysis_id: str,
    *,
    _digest_before: str | None = None,
) -> PreparedFreshAnalysis:
    """Validate the committed artifact once and retain only shortlist metrics."""
    from .fresh_analysis_strategies import (
        _read_analysis,
        _supports_pretest_ab,
        _validate_points,
    )

    path = Path(analysis_path).resolve()
    manifest, actual_id, digest_before = _read_analysis(path, artifact_sha256=_digest_before)
    if str(analysis_id) != actual_id:
        raise ValueError("fresh analysis run identity mismatch")
    scopes = _manifest_scopes(manifest)
    scope_values = set(scopes.values())
    try:
        connection = duckdb.connect(str(path), read_only=True)
    except (OSError, duckdb.Error) as error:
        raise ValueError(f"cannot open fresh analysis artifact: {error}") from error
    try:
        scope_rows = connection.execute(
            "select scope_key, scope_digest from scope_runs"
        ).fetchall()
        runs = {str(key): str(scope_digest) for key, scope_digest in scope_rows}
        if len(runs) != len(scope_rows) or set(runs) != set(scopes):
            raise ValueError("fresh analysis scope_runs disagree with manifest scopes")
        manifest_digests = dict(manifest["scope_digests"])
        for scope, digest in runs.items():
            if digest != manifest_digests[scope]:
                raise ValueError("fresh analysis scope_runs disagree with manifest digests")
        raw_by_table = {table: _load_table(connection, table) for table in _TABLES}
    finally:
        connection.close()

    points_by_scope: dict[str, list[dict[str, object]]] = {key: [] for key in scopes}
    points_flat: list[dict[str, object]] = []
    for scope_key, point in raw_by_table["points"]:
        if scope_key not in scopes or _scope_key(point, "point") != scopes.get(scope_key):
            raise ValueError("fresh analysis point scope disagrees with manifest or table scope")
        event_count = point.get("point_event_count")
        _strict_int(event_count, "point_event_count", minimum=0)
        points_by_scope[scope_key].append(point)
        points_flat.append(point)
    validated_points = _validate_points(
        points_flat,
        scope_values,
        require_pretest_ab=_supports_pretest_ab(manifest),
    )
    point_by_id: dict[str, dict[str, object]] = {}
    for row in validated_points.to_dict("records"):
        point_by_id[str(row["point_id"])] = row
    del validated_points, points_flat

    scope_plateau_ids: dict[str, set[str]] = {key: set() for key in scopes}
    for scope_key, plateau in raw_by_table["plateaus"]:
        if scope_key not in scopes:
            raise ValueError("fresh analysis plateau is outside manifest scopes")
        payload_scope = scopes[scope_key]
        for index, name in enumerate(("symbol", "side", "timeframe")):
            if name in plateau:
                actual = str(plateau[name]).strip()
                if (actual.upper() if index == 1 else actual) != payload_scope[index]:
                    raise ValueError("fresh analysis plateau scope disagrees with table scope")
        plateau_id = _canonical_id(plateau.get("plateau_id"), "plateau_id")
        scope_plateau_ids[scope_key].add(plateau_id)

    facts: list[FreshScopeFacts] = []
    for scope_key, (pair, side, timeframe) in scopes.items():
        facts.append(FreshScopeFacts(
            scope_key, pair, side, timeframe, len(scope_plateau_ids[scope_key]),
            _period(points_by_scope[scope_key]),
        ))

    candidates: list[FreshCandidateMetrics] = []
    seen_candidates: set[str] = set()
    for sql_scope, structure in raw_by_table["structures"]:
        if sql_scope not in scopes:
            raise ValueError("fresh structure is outside manifest scopes")
        scope = _scope_key(structure, "structure")
        if scope != scopes[sql_scope]:
            raise ValueError("fresh structure scope disagrees with manifest or table scope")
        order_count = _strict_int(structure.get("order_count"), "order_count", minimum=1, maximum=4)
        raw_candidate_id = structure.get("candidate_id", structure.get("structure_id"))
        candidate_id = _canonical_id(raw_candidate_id, "candidate identity")
        structure_id = _canonical_id(structure.get("structure_id"), "structure_id")
        if candidate_id in seen_candidates:
            raise ValueError(f"fresh analysis has duplicate or colliding candidate identity: {candidate_id}")
        seen_candidates.add(candidate_id)
        common_close_ma = _strict_int(structure.get("common_close_ma"), "common_close_ma", minimum=1)
        persisted_status = str(structure.get("status", ""))
        orders: list[FreshOrderMetrics] = []
        pretest: tuple[tuple[str, object], ...] | None = None
        if persisted_status == _READY_STATUS:
            raw_orders = structure.get("orders")
            if not isinstance(raw_orders, list) or len(raw_orders) != order_count:
                raise ValueError("READY fresh candidate order_count disagrees with orders")
            previous: tuple[int, str] | None = None
            for index, raw_order in enumerate(raw_orders, start=1):
                if not isinstance(raw_order, Mapping):
                    raise ValueError("READY fresh candidate has malformed order")
                if "id" in raw_order and _strict_int(raw_order["id"], "order id", minimum=1) != index:
                    raise ValueError("READY fresh candidate order id disagrees with position")
                point_id = _canonical_id(raw_order.get("point_id"), "order point_id")
                point = point_by_id.get(point_id)
                if point is None:
                    raise ValueError("READY fresh candidate references unknown point")
                point_scope = (str(point["symbol"]), str(point["side"]).upper(), str(point["timeframe"]))
                if point_scope != scope:
                    raise ValueError("READY fresh candidate order point is outside its scope")
                shift_bp = _strict_int(raw_order.get("shift_bp"), "order shift_bp", minimum=-(2**63))
                if _strict_int(point.get("shift_bp"), "point shift_bp", minimum=-(2**63)) != shift_bp:
                    raise ValueError("READY fresh candidate order shift disagrees with point")
                sequence_key = (shift_bp, point_id)
                if previous is not None and sequence_key <= previous:
                    raise ValueError("READY fresh candidate orders are not in canonical sequence")
                previous = sequence_key
                open_ma = _strict_int(raw_order.get("open_ma"), "order open_ma", minimum=1)
                point_open_ma = _strict_int(point.get("open_ma"), "point open_ma", minimum=1)
                if point_open_ma != open_ma:
                    raise ValueError("READY fresh candidate order open_ma disagrees with point")
                event_count = _strict_int(point.get("point_event_count"), "point_event_count", minimum=0)
                plateau_count = _strict_int(
                    raw_order.get("plateau_point_count"), "order plateau_point_count", minimum=1,
                )
                orders.append(FreshOrderMetrics(
                    open_ma=open_ma,
                    shift_bp=shift_bp,
                    point_id=point_id,
                    source_pnl_pct=_decimal(raw_order.get("source_pnl_pct"), "source_pnl_pct"),
                    source_dd_pct=_decimal(raw_order.get("source_dd_pct"), "source_dd_pct", nonnegative=True),
                    plateau_point_count=plateau_count,
                    point_event_count=event_count,
                ))
            first_point = point_by_id[orders[0].point_id]
            evidence = first_point.get("pretest_ab")
            if isinstance(evidence, Mapping):
                pretest = tuple(sorted((str(key), value) for key, value in evidence.items()))
            elif _supports_pretest_ab(manifest):
                # `_validate_points` should have caught this, but keep READY
                # candidate evidence complete if the validator changes later.
                raise ValueError("fresh analysis point is missing required pretest_ab evidence")
        candidates.append(FreshCandidateMetrics(
            candidate_id, structure_id, scope[0], scope[1], scope[2], common_close_ma,
            order_count, persisted_status, tuple(orders), pretest,
        ))
    candidates.sort(key=lambda item: (item.pair, item.side, item.timeframe, item.candidate_id))
    del raw_by_table, points_by_scope, point_by_id

    from .fresh_analysis_strategies import _file_digest

    digest_after = _file_digest(path)
    if digest_after != digest_before:
        raise ValueError("fresh analysis artifact changed during shortlist preparation")
    return PreparedFreshAnalysis(actual_id, digest_after, tuple(facts), tuple(candidates))


def _validate_options(options: object) -> tuple[bool, bool, bool]:
    if not isinstance(options, (tuple, list)) or len(options) != 3 or any(type(value) is not bool for value in options):
        raise ValueError("fresh shortlist options must be three booleans")
    return bool(options[0]), bool(options[1]), bool(options[2])


def normalize_min_shift(enabled: object, pct: object = _MISSING) -> tuple[bool, str | None]:
    """Validate and canonicalize the fresh shortlist Minimum Shift settings."""
    if type(enabled) is not bool:
        raise ValueError("min_shift_enabled must be a boolean")
    if not enabled:
        return False, None
    if pct is _MISSING:
        raise ValueError("min_shift_pct is required when min_shift_enabled is true")
    if isinstance(pct, bool) or pct is None:
        raise ValueError("min_shift_pct must be a finite numeric percentage")
    try:
        value = Decimal(str(pct))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("min_shift_pct must be a finite numeric percentage") from error
    if not value.is_finite() or value <= 0 or value > 100 or value.as_tuple().exponent < -3:
        raise ValueError("min_shift_pct must be greater than 0, at most 100, and use at most 3 decimals")
    canonical = format(value.quantize(Decimal("0.001")), "f")
    return enabled, canonical if enabled else None


def parse_fresh_shortlist_request(
    payload: Mapping[str, object],
) -> tuple[tuple[bool, bool, bool], bool, str | None]:
    """Parse all fresh shortlist options, including the optional Shift gate."""
    legacy_names = ("source_pnl", "efficiency", "close_support", "point_event_count")
    legacy_values: list[object] = []
    if "filters" in payload:
        filters = payload["filters"]
        if not isinstance(filters, Mapping) or set(filters).difference(legacy_names):
            raise ValueError("filters must contain only recognized legacy booleans")
        legacy_values.extend(filters.values())
    legacy_values.extend(payload[name] for name in legacy_names if name in payload)
    if any(type(value) is not bool for value in legacy_values):
        raise ValueError("Phase 2 filters must be booleans")

    flag_names = ("pretest_ab_enabled", "ladder_enabled", "pareto_enabled")
    flags: list[bool] = []
    for name in flag_names:
        value = payload.get(name, False)
        if type(value) is not bool:
            raise ValueError(f"{name} must be a boolean")
        flags.append(value)

    if "filter_version" in payload and payload["filter_version"] != FILTER_VERSION:
        raise ValueError(f"unsupported filter_version; expected {FILTER_VERSION}")
    if any(legacy_values):
        raise ValueError("stale shortlist client; send filter_version=shortlist-v2 and use the v2 flags")
    if "filter_version" not in payload and any(
        name in payload for name in (*flag_names[1:], "min_shift_enabled", "min_shift_pct")
    ):
        raise ValueError("stale shortlist client; send filter_version=shortlist-v2")

    enabled = payload.get("min_shift_enabled", False)
    if "min_shift_pct" in payload and "min_shift_enabled" not in payload:
        raise ValueError("min_shift_enabled is required when min_shift_pct is supplied")
    pct = payload["min_shift_pct"] if "min_shift_pct" in payload else _MISSING
    normalized_enabled, normalized_pct = normalize_min_shift(enabled, pct)
    return (flags[0], flags[1], flags[2]), normalized_enabled, normalized_pct


def min_shift_threshold_bp(canonical_pct: str) -> Decimal:
    """Convert the canonical percentage token to the comparison basis."""
    return Decimal(canonical_pct) * 100


def serialize_applied_options(
    options: tuple[bool, bool, bool], *, min_shift_enabled: bool = False,
    min_shift_pct: object = _MISSING,
) -> dict[str, object]:
    """Serialize applied options while retaining the disabled legacy shape."""
    flags = _validate_options(options)
    enabled, canonical = normalize_min_shift(min_shift_enabled, min_shift_pct)
    result: dict[str, object] = dict(zip(
        ("pretest_ab_enabled", "ladder_enabled", "pareto_enabled"), flags, strict=True,
    ))
    if enabled:
        result.update(min_shift_enabled=True, min_shift_pct=canonical)
    return result


def _coerce_order_shift(value: object = _MISSING) -> Decimal | None:
    """Parse a present persisted Shift without leaking Decimal exceptions."""
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("fresh shortlist order Shift is invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("fresh shortlist order Shift is invalid") from error
    if not result.is_finite():
        raise ValueError("fresh shortlist order Shift is invalid")
    return result


def _dominates(first: FreshCandidateMetrics, second: FreshCandidateMetrics) -> bool:
    if len(first.orders) != len(second.orders):
        return False
    strict_economic_improvement = False
    for left, right in zip(first.orders, second.orders):
        if (
            left.source_pnl_pct < right.source_pnl_pct
            or left.source_dd_pct > right.source_dd_pct
            or left.plateau_point_count < right.plateau_point_count
            or left.point_event_count < right.point_event_count
        ):
            return False
        if left.source_pnl_pct > right.source_pnl_pct or left.source_dd_pct < right.source_dd_pct:
            strict_economic_improvement = True
    return strict_economic_improvement


def _comparison_key(candidate: FreshCandidateMetrics) -> tuple[str, str, str, int, int]:
    return (candidate.pair, candidate.side, candidate.timeframe, candidate.common_close_ma, candidate.order_count)


def _token(
    prepared: PreparedFreshAnalysis, options: tuple[bool, bool, bool], ready_ids: tuple[str, ...],
    *, min_shift_enabled: bool = False, min_shift_pct: str | None = None,
    engine_version: str = LEGACY_FILTER_ENGINE_VERSION,
) -> str:
    payload = {
        "analysis_id": prepared.analysis_id,
        "artifact_sha256": prepared.artifact_sha256,
        "engine_version": engine_version,
        "options": serialize_applied_options(
            options,
            **({"min_shift_enabled": True, "min_shift_pct": min_shift_pct}
               if min_shift_enabled else {}),
        ),
        "ready_candidate_ids": list(ready_ids),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def _pareto_group_decimal(group: Sequence[FreshCandidateMetrics]) -> dict[str, str]:
    ordered = sorted(group, key=lambda item: (item.structure_id, item.candidate_id))
    dominated = {
        candidate.candidate_id
        for candidate in ordered
        if any(other.candidate_id != candidate.candidate_id and _dominates(other, candidate) for other in ordered)
    }
    frontier = [candidate for candidate in ordered if candidate.candidate_id not in dominated]
    return {
        candidate.candidate_id: next(other.candidate_id for other in frontier if _dominates(other, candidate))
        for candidate in ordered if candidate.candidate_id in dominated
    }


def _rank_matrix(group: Sequence[FreshCandidateMetrics]) -> tuple[np.ndarray, tuple[int, ...]]:
    ordered = sorted(group, key=lambda item: (item.structure_id, item.candidate_id))
    order_count = ordered[0].order_count
    dimensions: list[tuple[object, ...]] = []
    economic_columns: list[int] = []
    for order_index in range(order_count):
        dimensions.extend((
            tuple(candidate.orders[order_index].source_pnl_pct for candidate in ordered),
            tuple(candidate.orders[order_index].source_dd_pct for candidate in ordered),
            tuple(candidate.orders[order_index].plateau_point_count for candidate in ordered),
            tuple(candidate.orders[order_index].point_event_count for candidate in ordered),
        ))
        economic_columns.extend((order_index * 4, order_index * 4 + 1))
    ranks = np.empty((len(ordered), len(dimensions)), dtype=np.int64)
    for column_index, values in enumerate(dimensions):
        unique_values = sorted(set(values))
        rank = {value: index for index, value in enumerate(unique_values)}
        if column_index % 4 == 1:
            largest = len(unique_values) - 1
            ranks[:, column_index] = [largest - rank[value] for value in values]
        else:
            ranks[:, column_index] = [rank[value] for value in values]
    return ranks, tuple(economic_columns)


def _pareto_group_numpy(
    group: Sequence[FreshCandidateMetrics],
    *,
    temporary_array_budget_bytes: int,
) -> dict[str, str] | None:
    """Return a bounded exact frontier comparison, or None if arrays won't fit."""
    ordered = sorted(group, key=lambda item: (item.structure_id, item.candidate_id))
    count = len(ordered)
    if count < 2:
        return {}
    order_count = ordered[0].order_count
    dimension_count = order_count * 4
    rank_bytes = count * dimension_count * np.dtype(np.int64).itemsize
    fixed_mask_bytes = count * 2
    available = temporary_array_budget_bytes - rank_bytes - fixed_mask_bytes
    if available < count * 3 + 9:
        return None
    block_size = max(1, min(count, available // (count * 3 + 9)))
    ranks, economic_columns = _rank_matrix(ordered)
    dominated = np.zeros(count, dtype=np.bool_)
    for start in range(0, count, block_size):
        stop = min(count, start + block_size)
        matrix = np.ones((stop - start, count), dtype=np.bool_)
        strict_economic = np.zeros_like(matrix)
        comparison = np.empty_like(matrix)
        for column in range(dimension_count):
            np.less_equal(ranks[start:stop, column, None], ranks[None, :, column], out=comparison)
            np.logical_and(matrix, comparison, out=matrix)
            if column in economic_columns:
                np.less(ranks[start:stop, column, None], ranks[None, :, column], out=comparison)
                np.logical_or(strict_economic, comparison, out=strict_economic)
        np.logical_and(matrix, strict_economic, out=matrix)
        dominated[start:stop] = np.any(matrix, axis=1)
        del matrix, strict_economic, comparison

    frontier_mask = np.logical_not(dominated)
    mapping: dict[str, str] = {}
    for start in range(0, count, block_size):
        stop = min(count, start + block_size)
        matrix = np.ones((stop - start, count), dtype=np.bool_)
        strict_economic = np.zeros_like(matrix)
        comparison = np.empty_like(matrix)
        for column in range(dimension_count):
            np.less_equal(ranks[start:stop, column, None], ranks[None, :, column], out=comparison)
            np.logical_and(matrix, comparison, out=matrix)
            if column in economic_columns:
                np.less(ranks[start:stop, column, None], ranks[None, :, column], out=comparison)
                np.logical_or(strict_economic, comparison, out=strict_economic)
        np.logical_and(matrix, strict_economic, out=matrix)
        np.logical_and(matrix, frontier_mask[None, :], out=matrix)
        winners = np.argmax(matrix, axis=1)
        has_winner = np.any(matrix, axis=1)
        for row_index in range(stop - start):
            victim_index = start + row_index
            if not dominated[victim_index]:
                continue
            if not has_winner[row_index]:
                raise RuntimeError("Pareto frontier lost a transitive dominator")
            mapping[ordered[victim_index].candidate_id] = ordered[int(winners[row_index])].candidate_id
        del matrix, strict_economic, comparison, winners, has_winner
    del frontier_mask
    return mapping


def _pareto_groups(
    groups: Mapping[tuple[str, str, str, int, int], Sequence[FreshCandidateMetrics]],
    *,
    workers: int,
    temporary_array_budget_bytes: int,
) -> dict[str, str]:
    eligible = [(key, tuple(value)) for key, value in sorted(groups.items()) if len(value) > 1]
    work = sum(len(group) * len(group) * group[0].order_count * 4 for _key, group in eligible)
    worker_count = min(workers, len(eligible)) if work >= _MIN_PARALLEL_PARETO_WORK else 1
    per_worker_budget = max(1, temporary_array_budget_bytes // worker_count)

    def run(item: tuple[tuple[str, str, str, int, int], tuple[FreshCandidateMetrics, ...]]) -> dict[str, str]:
        _key, group = item
        result = _pareto_group_numpy(group, temporary_array_budget_bytes=per_worker_budget)
        return result if result is not None else _pareto_group_decimal(group)

    if worker_count > 1:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="fresh-shortlist-pareto") as pool:
            results = list(pool.map(run, eligible))
    else:
        results = [run(item) for item in eligible]
    return {candidate_id: dominator_id for result in results for candidate_id, dominator_id in result.items()}


def evaluate_fresh_shortlist(
    prepared: PreparedFreshAnalysis,
    options: tuple[bool, bool, bool] | Sequence[bool],
    *,
    workers: int,
    temporary_array_budget_bytes: int = _DEFAULT_TEMP_ARRAY_LIMIT,
    min_shift_enabled: object = False,
    min_shift_pct: object = _MISSING,
) -> FreshShortlistEvaluation:
    """Apply READY -> PRETEST A/B -> first-order MA ladder -> one joint Pareto."""
    from .fresh_analysis_strategies import _pretest_ab_outcome

    flags = _validate_options(options)
    shift_enabled, shift_pct = normalize_min_shift(min_shift_enabled, min_shift_pct)
    engine_version = FILTER_ENGINE_VERSION if shift_enabled else LEGACY_FILTER_ENGINE_VERSION
    threshold_bp = min_shift_threshold_bp(shift_pct) if shift_enabled and shift_pct is not None else None
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("fresh shortlist workers must be a positive integer")
    if isinstance(temporary_array_budget_bytes, bool) or not isinstance(temporary_array_budget_bytes, int) or temporary_array_budget_bytes < 1:
        raise ValueError("fresh shortlist temporary-array budget must be a positive integer")
    pretest_enabled, ladder_enabled, pareto_enabled = flags
    provisional: dict[str, tuple[FreshCandidateMetrics, str, str, str, str, str | None]] = {}
    pareto_universe: dict[tuple[str, str, str, int, int], list[FreshCandidateMetrics]] = {}
    for candidate in prepared.candidates:
        if candidate.persisted_status != _READY_STATUS:
            provisional[candidate.candidate_id] = (
                candidate, "DEFERRED_NOT_READY", "PERSISTED_NOT_READY", "DISABLED", "DISABLED", None,
            )
            continue
        if candidate.pretest_ab is None:
            if pretest_enabled:
                raise ValueError("PRETEST_AB_EVIDENCE_UNAVAILABLE: rebuild the fresh analysis")
            ab_status, ab_reason, decline = "DISABLED", "LEGACY_ANALYSIS", None
        else:
            evidence = dict(candidate.pretest_ab)
            ab_status, ab_reason, decline = _pretest_ab_outcome(evidence, pretest_enabled)
        if ab_status == "REJECT":
            provisional[candidate.candidate_id] = (
                candidate, "DEFERRED_PRETEST_AB", ab_reason, ab_status, ab_reason, decline,
            )
            continue
        if ladder_enabled and any(abs(order.open_ma - candidate.orders[0].open_ma) > 1 for order in candidate.orders[1:]):
            provisional[candidate.candidate_id] = (
                candidate, "DEFERRED_LADDER", "OPEN_MA_OUTSIDE_FIRST_ORDER_PLUS_MINUS_1",
                ab_status, ab_reason, decline,
            )
            continue
        if shift_enabled:
            # Minimum Shift limits the opening (first) order only. For 1ORD it
            # is the sole order; for 2ORD/3ORD later orders keep the existing
            # strictly increasing-Shift construction rule.
            first_order = candidate.orders[0] if candidate.orders else None
            first_shift = _coerce_order_shift(
                getattr(first_order, "shift_bp", _MISSING) if first_order is not None else _MISSING,
            )
            if first_shift is None or first_shift < threshold_bp:
                provisional[candidate.candidate_id] = (
                    candidate, "DEFERRED_MIN_SHIFT",
                    "ORDER_SHIFT_UNKNOWN" if first_shift is None else "ORDER_SHIFT_BELOW_MINIMUM",
                    ab_status, ab_reason, decline,
                )
                continue
        provisional[candidate.candidate_id] = (
            candidate, "READY_AFTER_FILTERS", "", ab_status, ab_reason, decline,
        )
        pareto_universe.setdefault(_comparison_key(candidate), []).append(candidate)

    dominators = (
        _pareto_groups(
            pareto_universe,
            workers=workers,
            temporary_array_budget_bytes=temporary_array_budget_bytes,
        )
        if pareto_enabled else {}
    )
    for candidate_id, dominator_id in dominators.items():
        candidate, _status, _reason, ab_status, ab_reason, decline = provisional[candidate_id]
        provisional[candidate_id] = (
            candidate, "DEFERRED_PARETO", "JOINT_PARETO_DOMINATED", ab_status, ab_reason, decline,
        )

    results: list[FreshCandidateResult] = []
    for candidate in prepared.candidates:
        _raw, status, reason, ab_status, ab_reason, decline = provisional[candidate.candidate_id]
        results.append(FreshCandidateResult(
            candidate_id=candidate.candidate_id,
            structure_id=candidate.structure_id,
            scope_key=f"{candidate.pair}|{candidate.side}|{candidate.timeframe}",
            order_count=candidate.order_count,
            filter_status=status,
            reason=reason,
            dominator_candidate_id=dominators.get(candidate.candidate_id),
            pretest_ab_status=ab_status,
            pretest_ab_reason=ab_reason,
            pretest_ab_decline_pct=decline,
        ))
    ready_ids = tuple(sorted(
        item.candidate_id for item in results if item.filter_status == "READY_AFTER_FILTERS"
    ))
    candidates_by_scope: dict[str, list[FreshCandidateResult]] = {fact.scope_key: [] for fact in prepared.scopes}
    result_by_id = {item.candidate_id: item for item in results}
    for candidate in prepared.candidates:
        candidates_by_scope[f"{candidate.pair}|{candidate.side}|{candidate.timeframe}"].append(
            result_by_id[candidate.candidate_id]
        )
    scope_groups: list[FreshScopeResult] = []
    for fact in prepared.scopes:
        rows = candidates_by_scope[fact.scope_key]
        ready_rows = [item for item in rows if item.filter_status == "READY_AFTER_FILTERS"]
        counts = tuple(sum(item.order_count == order for item in ready_rows) for order in (1, 2, 3, 4))
        ids = tuple(sorted(item.candidate_id for item in ready_rows))
        scope_groups.append(FreshScopeResult(
            fact.scope_key, fact.pair, fact.side, fact.timeframe, fact.plateau_count, fact.period,
            counts, len(ready_rows), len(rows) - len(ready_rows), len(rows), ids,
        ))
    return FreshShortlistEvaluation(
        prepared.analysis_id, prepared.artifact_sha256, FILTER_VERSION, engine_version,
        flags, _token(
            prepared, flags, ready_ids, min_shift_enabled=shift_enabled,
            min_shift_pct=shift_pct, engine_version=engine_version,
        ),
        ready_ids, tuple(results), tuple(scope_groups), shift_enabled, shift_pct,
    )


class FreshShortlistExecutor:
    """One bounded, content-verified preparation/evaluation cache per controller."""

    def __init__(
        self,
        *,
        cache_limit_bytes: int = _DEFAULT_CACHE_LIMIT,
        evaluation_limit: int = _DEFAULT_EVALUATION_LIMIT,
        temporary_array_budget_bytes: int = _DEFAULT_TEMP_ARRAY_LIMIT,
        wait_timeout_seconds: float = _WAIT_TIMEOUT_SECONDS,
        max_identical_followers: int = _MAX_IDENTICAL_FOLLOWERS,
    ) -> None:
        if isinstance(cache_limit_bytes, bool) or not isinstance(cache_limit_bytes, int) or cache_limit_bytes < 1:
            raise ValueError("fresh shortlist cache limit must be a positive integer")
        if isinstance(evaluation_limit, bool) or not isinstance(evaluation_limit, int) or evaluation_limit < 1:
            raise ValueError("fresh shortlist evaluation limit must be a positive integer")
        if isinstance(temporary_array_budget_bytes, bool) or not isinstance(temporary_array_budget_bytes, int) or temporary_array_budget_bytes < 1:
            raise ValueError("fresh shortlist temporary-array budget must be a positive integer")
        if wait_timeout_seconds <= 0 or max_identical_followers < 0:
            raise ValueError("fresh shortlist concurrency limits must be positive")
        self.cache_limit_bytes = cache_limit_bytes
        self.evaluation_limit = evaluation_limit
        self.temporary_array_budget_bytes = temporary_array_budget_bytes
        self.wait_timeout_seconds = wait_timeout_seconds
        self.max_identical_followers = max_identical_followers
        self._condition = threading.Condition(threading.RLock())
        self._prepared_key: tuple[str, str, str] | None = None
        self._prepared: PreparedFreshAnalysis | None = None
        self._prepared_bytes = 0
        self._evaluations: OrderedDict[
            tuple[tuple[str, str, str], tuple[bool, bool, bool]],
            tuple[FreshShortlistEvaluation, int],
        ] = OrderedDict()
        self._evaluation_bytes = 0
        self._active_key: tuple[tuple[str, str, str], tuple[bool, bool, bool]] | None = None
        self._active_done = False
        self._active_prepared: PreparedFreshAnalysis | None = None
        self._active_result: FreshShortlistEvaluation | None = None
        self._active_error: Exception | None = None
        self._followers = 0

    @property
    def cache_info(self) -> FreshShortlistCacheInfo:
        with self._condition:
            return FreshShortlistCacheInfo(
                self._prepared_bytes,
                len(self._evaluations),
                self._prepared_bytes + self._evaluation_bytes,
            )

    def _clear_evaluations(self) -> None:
        self._evaluations.clear()
        self._evaluation_bytes = 0

    def _clear_active(self) -> None:
        self._active_key = None
        self._active_done = False
        self._active_prepared = None
        self._active_result = None
        self._active_error = None

    def _get_cached(
        self,
        prepared_key: tuple[str, str, str],
        cache_key: tuple[object, ...],
    ) -> tuple[PreparedFreshAnalysis | None, FreshShortlistEvaluation | None]:
        with self._condition:
            prepared = self._prepared if self._prepared_key == prepared_key else None
            cached = self._evaluations.get(cache_key)
            if cached is not None:
                self._evaluations.move_to_end(cache_key)
                return prepared, cached[0]
            return prepared, None

    def _remember(
        self,
        prepared_key: tuple[str, str, str],
        prepared: PreparedFreshAnalysis,
        cache_key: tuple[object, ...],
        result: FreshShortlistEvaluation,
    ) -> None:
        prepared_size = _object_graph_size(prepared)
        result_size = _object_graph_size(result)
        with self._condition:
            if self._prepared_key != prepared_key:
                if prepared_size <= self.cache_limit_bytes:
                    self._prepared_key = prepared_key
                    self._prepared = prepared
                    self._prepared_bytes = prepared_size
                    self._clear_evaluations()
                elif self._prepared is None:
                    # Oversized request-local data is never retained. Existing
                    # smaller snapshots remain valid under their own content key.
                    self._prepared_key = None
                    self._prepared_bytes = 0
            if self._prepared_key != prepared_key or self._prepared is not prepared:
                return
            prior = self._evaluations.pop(cache_key, None)
            if prior is not None:
                self._evaluation_bytes -= prior[1]
            if self._prepared_bytes + result_size > self.cache_limit_bytes:
                return
            while self._evaluations and (
                len(self._evaluations) >= self.evaluation_limit
                or self._prepared_bytes + self._evaluation_bytes + result_size > self.cache_limit_bytes
            ):
                _old_key, (_old_value, old_size) = self._evaluations.popitem(last=False)
                self._evaluation_bytes -= old_size
            if (
                result_size <= self.cache_limit_bytes
                and self._prepared_bytes + self._evaluation_bytes + result_size <= self.cache_limit_bytes
            ):
                self._evaluations[cache_key] = (result, result_size)
                self._evaluation_bytes += result_size

    def evaluate(
        self,
        analysis_path: Path | str,
        analysis_id: str,
        options: tuple[bool, bool, bool] | Sequence[bool],
        *,
        workers: int,
        min_shift_enabled: object = False,
        min_shift_pct: object = _MISSING,
    ) -> FreshShortlistEvaluation:
        return self.evaluate_with_prepared(
            analysis_path, analysis_id, options, workers=workers,
            min_shift_enabled=min_shift_enabled, min_shift_pct=min_shift_pct,
        )[1]

    def evaluate_with_prepared(
        self,
        analysis_path: Path | str,
        analysis_id: str,
        options: tuple[bool, bool, bool] | Sequence[bool],
        *,
        workers: int,
        min_shift_enabled: object = False,
        min_shift_pct: object = _MISSING,
    ) -> tuple[PreparedFreshAnalysis, FreshShortlistEvaluation]:
        from .fresh_analysis_strategies import _file_digest

        flags = _validate_options(options)
        shift_enabled, shift_pct = normalize_min_shift(min_shift_enabled, min_shift_pct)
        path = Path(analysis_path).resolve()
        # Hash at the action's consistency point even for a warm cache hit.
        digest = _file_digest(path)
        # Preparation reads the analysis artifact only; the engine version is
        # an evaluation concern and must not force the same artifact to be
        # reparsed when the checkbox is toggled.
        prepared_key = (str(analysis_id), digest, "prepared-v1")
        cache_key: tuple[object, ...] = (
            (prepared_key, flags)
            if not shift_enabled else (prepared_key, flags, "min_shift-v1", shift_pct)
        )
        action_key = cache_key
        cached_prepared, cached_result = self._get_cached(prepared_key, cache_key)
        if cached_result is not None:
            if cached_prepared is None:
                raise RuntimeError("fresh shortlist evaluation cache has no prepared analysis")
            return cached_prepared, cached_result

        with self._condition:
            if self._active_key is not None:
                if self._active_key != action_key:
                    raise ShortlistBusyError()
                if self._active_done:
                    if self._active_error is not None:
                        _raise_shared_error(self._active_error)
                    if self._active_result is None or self._active_prepared is None:
                        raise RuntimeError("fresh shortlist single-flight completed without a result")
                    return self._active_prepared, self._active_result
                if self._followers >= self.max_identical_followers:
                    raise ShortlistBusyError()
                self._followers += 1
                deadline = time.monotonic() + self.wait_timeout_seconds
                try:
                    while not self._active_done:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ShortlistBusyError()
                        self._condition.wait(remaining)
                    if self._active_error is not None:
                        _raise_shared_error(self._active_error)
                    if self._active_result is None or self._active_prepared is None:
                        raise RuntimeError("fresh shortlist single-flight completed without a result")
                    return self._active_prepared, self._active_result
                finally:
                    self._followers -= 1
                    if self._active_done and self._followers == 0:
                        self._clear_active()
                        self._condition.notify_all()
            self._active_key = action_key
            self._active_done = False
            self._active_result = None
            self._active_prepared = None
            self._active_error = None
            self._followers = 0

        try:
            prepared = cached_prepared or prepare_fresh_shortlist(path, analysis_id, _digest_before=digest)
            result = evaluate_fresh_shortlist(
                prepared,
                flags,
                workers=workers,
                temporary_array_budget_bytes=self.temporary_array_budget_bytes,
                min_shift_enabled=shift_enabled,
                **({"min_shift_pct": shift_pct} if shift_enabled else {}),
            )
            self._remember(prepared_key, prepared, cache_key, result)
        except BaseException as error:
            with self._condition:
                self._active_done = True
                self._active_prepared = None
                self._active_error = (
                    error if isinstance(error, Exception)
                    else RuntimeError(f"fresh shortlist leader was interrupted: {error}")
                )
                if self._followers == 0:
                    self._clear_active()
                self._condition.notify_all()
            raise
        with self._condition:
            self._active_done = True
            self._active_prepared = prepared
            self._active_result = result
            if self._followers == 0:
                self._clear_active()
            self._condition.notify_all()
        return prepared, result
