from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from decimal import Decimal
from typing import Iterable, Mapping

import duckdb


CRITERIA = ("source_pnl", "efficiency", "close_support", "point_event_count")
_ORDER_FIELDS = {
    "source_pnl": "source_pnl_pct",
    "efficiency": "source_efficiency",
    "close_support": "close_support",
}


@dataclass(frozen=True, slots=True)
class FilterResult:
    run_id: str
    surface_id: str
    criteria: tuple[str, ...]
    rows: tuple[dict[str, object], ...]
    standalone: Mapping[str, tuple[dict[str, object], ...]]
    combined: tuple[dict[str, object], ...]
    input_count: int
    ready_count: int
    deferred_count: int
    comparison_group_count: int
    comparable_count: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    candidate_id: str
    structure_id: str
    comparison_key: str
    values: Mapping[str, tuple[Decimal | int, ...]]
    payload: Mapping[str, object]


def _enabled_criteria(criteria: Mapping[str, object] | Iterable[str] | None) -> tuple[str, ...]:
    if criteria is None:
        return ()
    if isinstance(criteria, Mapping):
        unknown = set(criteria) - set(CRITERIA)
        if unknown:
            raise ValueError(f"unknown filter criteria: {sorted(unknown)}")
        return tuple(name for name in CRITERIA if bool(criteria.get(name, False)))
    if isinstance(criteria, str):
        criteria = (criteria,)
    enabled = tuple(str(name) for name in criteria)
    unknown = set(enabled) - set(CRITERIA)
    if unknown:
        raise ValueError(f"unknown filter criteria: {sorted(unknown)}")
    return tuple(name for name in CRITERIA if name in enabled)


def _comparison_key(
    candidate: Mapping[str, object],
    candidate_id: str,
    legacy: bool,
) -> str:
    key = {
        "pair": str(candidate.get("symbol", candidate.get("pair", ""))),
        "side": str(candidate.get("side", "")),
        "timeframe": str(candidate.get("timeframe", candidate.get("tf", ""))),
        "order_count": int(candidate.get("order_count", len(candidate.get("orders", ())))),
        "common_close_ma": candidate.get("common_close_ma"),
    }
    if legacy:
        key["legacy_candidate_id"] = candidate_id
    return json.dumps(key, sort_keys=True, separators=(",", ":"))


def _decimal(value: object, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as error:
        raise ValueError(f"candidate order has invalid {name}") from error


def _load_candidates(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    surface_id: str,
    require_event_membership: bool,
    legacy: bool,
) -> tuple[_Candidate, ...]:
    event_rows = connection.execute(
        "select canonical_point_key, event_id from surface_point_events where surface_id=? order by canonical_point_key, event_id",
        [surface_id],
    ).fetchall()
    event_map: dict[str, list[str]] = {}
    for point_id, event_id in event_rows:
        event_map.setdefault(str(point_id), []).append(str(event_id))
    expected_counts = {
        str(point_id): int(count)
        for point_id, count in connection.execute(
            "select canonical_point_key, point_event_count from surface_points where surface_id=?",
            [surface_id],
        ).fetchall()
    }

    loaded: list[_Candidate] = []
    rows = connection.execute(
        "select candidate_id, candidate_json from candidates where run_id=? order by candidate_id",
        [run_id],
    ).fetchall()
    for candidate_id, raw in rows:
        try:
            candidate = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("analysis candidate contains invalid JSON") from error
        if not isinstance(candidate, dict):
            raise ValueError("analysis candidate must be a JSON object")
        if candidate.get("status") != "READY_MRS3_STRUCTURE":
            continue
        orders = candidate.get("orders")
        if not isinstance(orders, list) or not orders:
            raise ValueError("analysis candidate orders must be a non-empty list")
        order_values: dict[str, list[Decimal | int]] = {name: [] for name in CRITERIA}
        for order in orders:
            if not isinstance(order, dict) or "point_id" not in order:
                raise ValueError("analysis candidate order must contain point_id")
            point_id = str(order["point_id"])
            if require_event_membership and point_id not in event_map:
                raise ValueError(f"missing event membership for point {point_id}")
            point_events = event_map.get(point_id, [])
            if require_event_membership and expected_counts.get(point_id) != len(set(point_events)):
                raise ValueError(f"event membership count mismatch for point {point_id}")
            for name, field in _ORDER_FIELDS.items():
                order_values[name].append(_decimal(order.get(field), field))
            order_values["point_event_count"].append(len(set(point_events)))
        loaded.append(
            _Candidate(
                str(candidate_id),
                str(candidate.get("structure_id", "")),
                _comparison_key(candidate, str(candidate_id), legacy),
                {name: tuple(values) for name, values in order_values.items()},
                dict(candidate),
            )
        )
    return tuple(loaded)


def _dominates(left: _Candidate, right: _Candidate, criteria: tuple[str, ...]) -> bool:
    if left.comparison_key != right.comparison_key or not criteria:
        return False
    if any(
        any(left.values[name][index] < right.values[name][index] for index in range(len(left.values[name])))
        for name in criteria
    ):
        return False
    if "close_support" in criteria:
        if min(left.values["close_support"]) < min(right.values["close_support"]):
            return False
    return any(
        left.values[name][index] > right.values[name][index]
        for name in criteria
        for index in range(len(left.values[name]))
    )


def _audit_row(
    dominator: _Candidate,
    deferred: _Candidate,
    criterion: str,
) -> dict[str, object]:
    return {
        "candidate_id": deferred.candidate_id,
        "structure_id": deferred.structure_id,
        "comparison_key": deferred.comparison_key,
        "deferred_by": dominator.structure_id,
        "deferred_by_candidate_id": dominator.candidate_id,
        "criterion": criterion,
        "defer_reason": "SAME_STRUCTURE_DOMINATED",
        "a_values": {name: list(dominator.values[name]) for name in CRITERIA},
        "b_values": {name: list(deferred.values[name]) for name in CRITERIA},
    }


def _standalone_rows(candidates: tuple[_Candidate, ...], criterion: str) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for deferred in candidates:
        dominators = [
            candidate
            for candidate in candidates
            if _dominates(candidate, deferred, (criterion,))
        ]
        if dominators:
            dominator = min(dominators, key=lambda item: (item.structure_id, item.candidate_id))
            rows.append(_audit_row(dominator, deferred, criterion))
    return tuple(sorted(rows, key=lambda row: (str(row["comparison_key"]), str(row["candidate_id"]))))


def filter_analysis_candidates(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    criteria: Mapping[str, object] | Iterable[str] | None,
) -> FilterResult:
    """Return a non-mutating Phase 2 shortlist and its audit views."""
    found = connection.execute(
        "select surface_id from analysis_runs where run_id=?", [run_id]
    ).fetchone()
    if found is None:
        raise ValueError("unknown analysis run")
    surface_id = str(found[0])
    enabled = _enabled_criteria(criteria)
    surface_row = connection.execute(
        "select event_mode from surfaces where surface_id=?", [surface_id]
    ).fetchone()
    if surface_row is None:
        raise ValueError("analysis run references unknown surface")
    event_mode = str(surface_row[0]) if surface_row[0] is not None else ""
    if event_mode not in {"legacy_trades_proxy", "real_independent_events"}:
        raise ValueError("surface has unsupported event_mode")
    if enabled and event_mode != "real_independent_events":
        if event_mode == "legacy_trades_proxy":
            raise ValueError("Phase 2 criteria are unavailable for legacy surface")
        raise ValueError("Phase 2 criteria require a real event surface")
    candidates = _load_candidates(
        connection,
        run_id,
        surface_id,
        require_event_membership=event_mode == "real_independent_events",
        legacy=event_mode == "legacy_trades_proxy",
    )
    grouped: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.comparison_key].append(candidate)

    standalone: dict[str, tuple[dict[str, object], ...]] = {
        name: tuple(
            row
            for key in sorted(grouped)
            for row in _standalone_rows(tuple(grouped[key]), name)
        )
        for name in enabled
    }
    combined_rows: list[dict[str, object]] = []
    combination = ",".join(enabled)
    for key in sorted(grouped):
        group = grouped[key]
        for deferred in group:
            dominators = [
                candidate
                for candidate in group
                if _dominates(candidate, deferred, enabled)
            ]
            if dominators:
                dominator = min(dominators, key=lambda item: (item.structure_id, item.candidate_id))
                combined_rows.append(_audit_row(dominator, deferred, combination))
    combined = tuple(
        sorted(combined_rows, key=lambda row: (str(row["comparison_key"]), str(row["candidate_id"])))
    )
    deferred_by = {
        str(row["candidate_id"]): row for row in combined
    }
    group_sizes = Counter(candidate.comparison_key for candidate in candidates)
    result_rows = []
    for candidate in sorted(candidates, key=lambda item: (item.comparison_key, item.candidate_id)):
        audit = deferred_by.get(candidate.candidate_id)
        row = dict(candidate.payload)
        row.update(
            {
                "candidate_id": candidate.candidate_id,
                "comparison_key": candidate.comparison_key,
                "comparison_group_size": group_sizes[candidate.comparison_key],
                "filter_status": "DEFERRED_REDUNDANT" if audit else "READY_AFTER_FILTERS",
                "deferred_by": audit["deferred_by"] if audit else None,
                "deferred_by_candidate_id": audit["deferred_by_candidate_id"] if audit else None,
                "enabled_criteria": list(enabled),
            }
        )
        result_rows.append(row)
    deferred_count = len(combined)
    return FilterResult(
        run_id,
        surface_id,
        enabled,
        tuple(result_rows),
        standalone,
        combined,
        len(candidates),
        len(candidates) - deferred_count,
        deferred_count,
        len(group_sizes),
        sum(size for size in group_sizes.values() if size > 1),
    )
