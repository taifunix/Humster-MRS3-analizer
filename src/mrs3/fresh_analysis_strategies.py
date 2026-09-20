"""Generate READY strategies from a fresh compact multi-scope analysis DB.

This is intentionally a read-only adapter.  It does not open the legacy
Analysis DuckDB, read CSV, or recompute source facts.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

import duckdb
import pandas as pd

from .analysis_strategies import (
    GeneratedAnalysisStrategies,
    V6_READY_GENERATOR_SCHEMA,
    _publish_strategies,
    _template,
    _v6_strategy_digest,
    normalize_analysis_scopes,
)
from .analysis_shortlist import (
    CRITERIA,
    FilterResult,
    _Candidate,
    _audit_row,
    _comparison_key,
    _decimal,
    _dominates,
    _enabled_criteria,
    _standalone_rows,
)
from .config import AlgorithmConfig
from .lots import LotMethod, allocate_lots
from .strategy_json import generate_strategy, validate_strategy, validate_unique_names
from .source_v6 import _canonical_json
from .source_v6_surface_fresh import FINGERPRINT as SURFACE_FINGERPRINT, read_multiscope_surface


FINGERPRINT = "analysis-v6-fresh-compact-v2"
LEGACY_FINGERPRINT = "analysis-v6-fresh-compact-v1"
EVENT_MODE = "real_independent_events"
GENERATOR_SCHEMA = f"{V6_READY_GENERATOR_SCHEMA}-fresh-compact"
PRETEST_AB_CONTRACT = "source-v6-pretest-ab-v1"
PRETEST_AB_WINDOW_DAYS = 14
PRETEST_AB_THRESHOLD_PCT = Decimal("95")
_TABLES = ("points", "structures")
_CANDIDATE_TABLES = ("structures",)
_ORDER_BUCKETS = (1, 2, 3, 4)
_READY_STATUS = "READY_MRS3_STRUCTURE"
_HASH_FIELDS = (
    "source_content_digest", "algorithm_config_sha256", "listing_dates_sha256", "analysis_input_digest",
)
_PLATEAU_DIAGNOSTIC_COLUMNS = (
    "plateau_point_count",
    "base_point_trades",
    "plateau_total_trades",
)
_PRETEST_AB_FIELDS = {
    "contract_version", "status", "reason", "a_start_ms", "a_end_ms", "b_start_ms", "b_end_ms",
    "a_days", "b_days", "a_pnl", "b_pnl", "a_round_trips", "b_round_trips",
}
_LEGACY_SURFACE_FINGERPRINT = "surface-v6-fresh-compact-v2"


def _supports_pretest_ab(manifest: Mapping[str, object]) -> bool:
    return manifest.get("fingerprint") == FINGERPRINT


def _plateau_diagnostics(structure: Mapping[str, object]) -> dict[str, object]:
    """Validate the persisted scalar/list shape before publishing provenance."""
    try:
        order_count = int(structure["order_count"])
        orders = structure["orders"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("fresh structure has invalid diagnostic context") from error
    if not isinstance(orders, (list, tuple)) or len(orders) != order_count:
        raise ValueError("fresh structure has invalid diagnostic context")
    diagnostics: dict[str, object] = {}
    missing: list[str] = []
    malformed: list[str] = []
    for column in _PLATEAU_DIAGNOSTIC_COLUMNS:
        if column not in structure:
            missing.append(column)
            continue
        value = structure[column]
        if order_count == 1:
            valid = type(value) is int and value >= 0
        else:
            valid = (
                isinstance(value, (list, tuple))
                and len(value) == order_count
                and all(type(item) is int and item >= 0 for item in value)
            )
        if not valid:
            malformed.append(column)
            continue
        diagnostics[column] = value
    if missing:
        raise ValueError(
            "fresh structure is missing plateau diagnostics; re-materialize analysis: "
            f"{missing}"
        )
    if malformed:
        raise ValueError(f"fresh structure has malformed plateau diagnostics: {malformed}")
    return diagnostics


def _candidate_diagnostics(structure: Mapping[str, object]) -> dict[str, object]:
    values = _plateau_diagnostics(structure)
    orders = structure["orders"]
    order_count = int(structure["order_count"])

    def at(name: str, index: int) -> int:
        value = values[name]
        return int(value if order_count == 1 else value[index])

    return {
        "order_count": order_count,
        "orders": [
            {
                "order_id": int(order["id"]),
                "plateau_id": str(order["plateau_id"]),
                "plateau_point_count": at("plateau_point_count", index),
                "base_point_trades": at("base_point_trades", index),
                "plateau_total_trades": at("plateau_total_trades", index),
            }
            for index, order in enumerate(orders)
        ],
    }


@dataclass(frozen=True, slots=True)
class FreshAnalysisStrategies(GeneratedAnalysisStrategies):
    """Result of a fresh-analysis strategy publication."""


def _canonical_digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _manifest_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _read_analysis(path: Path) -> tuple[dict[str, object], str, str]:
    if path.suffix.casefold() != ".duckdb":
        raise ValueError("fresh analysis generation requires a .analysis-v6.duckdb")
    try:
        connection = duckdb.connect(str(path), read_only=True)
    except (OSError, duckdb.Error) as error:
        raise ValueError(f"cannot open fresh analysis artifact: {error}") from error
    try:
        try:
            manifest = {
                str(key): _manifest_value(value)
                for key, value in connection.execute("select key, value from manifest").fetchall()
            }
        except duckdb.Error as error:
            raise ValueError("analysis artifact has no manifest") from error
        artifact_contract = (manifest.get("fingerprint"), manifest.get("surface_fingerprint"))
        if artifact_contract not in {
            (FINGERPRINT, SURFACE_FINGERPRINT),
            (LEGACY_FINGERPRINT, _LEGACY_SURFACE_FINGERPRINT),
        }:
            raise ValueError("unsupported analysis artifact; fresh compact analysis is required")
        if manifest.get("event_mode") != EVENT_MODE:
            raise ValueError("fresh analysis requires event_mode real_independent_events")
        if manifest.get("build_mode") == "DUCKDB_DIRECT":
            raise ValueError("DUCKDB_DIRECT is not accepted by fresh analysis generation")
        analysis_id = str(manifest.get("analysis_id", ""))
        if len(analysis_id) != 64:
            raise ValueError("fresh analysis manifest has no valid analysis_id")
        identity = dict(manifest)
        identity.pop("analysis_id", None)
        if _canonical_digest(identity) != analysis_id:
            raise ValueError("fresh analysis identity hash mismatch")
        if not isinstance(manifest.get("surface_id"), str) or not manifest["surface_id"]:
            raise ValueError("fresh analysis surface identity is missing")
        scope_digests = manifest.get("scope_digests")
        if not isinstance(scope_digests, Mapping) or not scope_digests:
            raise ValueError("fresh analysis scope identity is missing")
        hash_fields = _HASH_FIELDS if _supports_pretest_ab(manifest) else _HASH_FIELDS[:-1]
        for field in hash_fields:
            value = str(manifest.get(field, ""))
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
                raise ValueError(f"fresh analysis manifest has invalid {field}")
        for table in _TABLES:
            try:
                connection.execute(f"select 1 from {table} limit 1")
            except duckdb.Error as error:
                raise ValueError(f"fresh analysis artifact is missing {table}") from error
        return manifest, analysis_id, _file_digest(path)
    finally:
        connection.close()


def _rows(connection: duckdb.DuckDBPyConnection, table: str, scope: str) -> list[dict[str, object]]:
    try:
        raw_rows = connection.execute(
            f"select payload_json from {table} where scope_key=? order by payload_json", [scope]
        ).fetchall()
    except duckdb.Error as error:
        raise ValueError(f"fresh analysis table {table} cannot be read") from error
    result: list[dict[str, object]] = []
    for (raw,) in raw_rows:
        try:
            value = json.loads(str(raw))
        except json.JSONDecodeError as error:
            raise ValueError(f"fresh analysis table {table} contains invalid JSON") from error
        if not isinstance(value, dict):
            raise ValueError(f"fresh analysis table {table} contains a non-object row")
        result.append(value)
    return result


def _validate_points(
    points: Sequence[Mapping[str, object]], scopes: set[tuple[str, str, str]], *, require_pretest_ab: bool = True,
) -> pd.DataFrame:
    required = {
        "point_id", "symbol", "side", "timeframe", "shift_bp", "shift_pct", "open_ma", "close_ma",
        "pnl_pct", "dd_pct", "efficiency", "trades", "plateau_id", "economic_pass",
        "standalone_eligible", "depth_eligible", "refine_required", "event_mode", "_event_ids",
        "event_ids_hash", "point_event_count",
    }
    seen: set[str] = set()
    rows: list[dict[str, object]] = []
    for raw in points:
        missing = sorted(required.difference(raw))
        if missing:
            raise ValueError(f"fresh point is missing required fields: {missing}")
        point_id = str(raw["point_id"])
        if point_id in seen:
            raise ValueError("fresh analysis contains duplicate point_id")
        seen.add(point_id)
        scope = (str(raw["symbol"]), str(raw["side"]).upper(), str(raw["timeframe"]))
        if scope not in scopes:
            raise ValueError("fresh analysis point is outside selected scopes")
        if raw["event_mode"] != EVENT_MODE:
            raise ValueError("fresh point has unsupported or mixed event mode")
        event_ids = sorted({str(value) for value in raw["_event_ids"]}) if isinstance(raw["_event_ids"], (list, tuple, set)) else None
        if event_ids is None:
            raise ValueError("fresh point exact event IDs are malformed")
        if len(event_ids) != int(raw["point_event_count"]):
            raise ValueError("fresh point event count disagrees with exact event IDs")
        if str(raw["event_ids_hash"]) != sha256("|".join(event_ids).encode("utf-8")).hexdigest():
            raise ValueError("fresh point event-ID hash mismatch")
        if "pretest_ab" in raw:
            _validate_pretest_ab_evidence(raw["pretest_ab"])
        elif require_pretest_ab:
            raise ValueError("fresh point is missing required fields: ['pretest_ab']")
        rows.append({**raw, "side": scope[1], "_event_ids": event_ids})
    if not rows:
        raise ValueError("fresh analysis has no points for selected scopes")
    return pd.DataFrame(rows)


def _validate_pretest_ab_evidence(value: object) -> dict[str, object]:
    """Validate the persisted, versioned source PRETEST evidence strictly."""
    if not isinstance(value, Mapping) or set(value) != _PRETEST_AB_FIELDS:
        raise ValueError("fresh point has malformed pretest_ab evidence")
    evidence = dict(value)
    if evidence["contract_version"] != PRETEST_AB_CONTRACT:
        raise ValueError("fresh point has unsupported pretest_ab contract")
    status = evidence["status"]
    reason = evidence["reason"]
    expected_reasons = {
        "COMPARABLE": "FULL_READY_WITNESS",
        "INSUFFICIENT_HISTORY": "A_SHORTER_THAN_14_DAYS",
    }
    if status not in expected_reasons or reason != expected_reasons[status]:
        raise ValueError("fresh point has invalid pretest_ab status")
    integer_fields = ("a_start_ms", "a_end_ms", "b_start_ms", "b_end_ms", "a_days", "b_days", "a_round_trips", "b_round_trips")
    if any(type(evidence[field]) is not int or evidence[field] < 0 for field in integer_fields):
        raise ValueError("fresh point has invalid pretest_ab integer fields")
    if evidence["a_end_ms"] < evidence["a_start_ms"] or evidence["b_end_ms"] != evidence["a_end_ms"]:
        raise ValueError("fresh point has invalid pretest_ab bounds")
    if evidence["b_end_ms"] - evidence["b_start_ms"] != PRETEST_AB_WINDOW_DAYS * 24 * 60 * 60 * 1000:
        raise ValueError("fresh point has invalid pretest_ab window")
    if evidence["a_days"] != (evidence["a_end_ms"] - evidence["a_start_ms"]) // (24 * 60 * 60 * 1000):
        raise ValueError("fresh point has invalid pretest_ab day count")
    if evidence["b_days"] != PRETEST_AB_WINDOW_DAYS:
        raise ValueError("fresh point has invalid pretest_ab day count")
    decimal_fields = ("a_pnl", "b_pnl")
    for field in decimal_fields:
        item = evidence[field]
        if item is None:
            if status == "COMPARABLE" or field == "a_pnl":
                raise ValueError(f"fresh point has invalid pretest_ab {field}")
            continue
        if not isinstance(item, str):
            raise ValueError(f"fresh point has invalid pretest_ab {field}")
        try:
            parsed = Decimal(item)
        except Exception as error:
            raise ValueError(f"fresh point has invalid pretest_ab {field}") from error
        if not parsed.is_finite():
            raise ValueError(f"fresh point has invalid pretest_ab {field}")
    if status == "COMPARABLE" and evidence["a_days"] < PRETEST_AB_WINDOW_DAYS:
        raise ValueError("fresh point has invalid pretest_ab history status")
    if status == "INSUFFICIENT_HISTORY" and evidence["a_days"] >= PRETEST_AB_WINDOW_DAYS:
        raise ValueError("fresh point has invalid pretest_ab history status")
    return evidence


def _pretest_ab_outcome(evidence: Mapping[str, object], enabled: bool) -> tuple[str, str, str | None]:
    """Return status, diagnostic reason and the calculated decline percentage."""
    if not enabled:
        return "DISABLED", "DISABLED", None
    _validate_pretest_ab_evidence(evidence)
    if evidence["status"] == "INSUFFICIENT_HISTORY":
        return "PASS", "INSUFFICIENT_HISTORY", None
    if int(evidence["b_round_trips"]) == 0:
        return "PASS", "NO_B_TRADES", None
    a_pnl = Decimal(str(evidence["a_pnl"]))
    b_pnl = Decimal(str(evidence["b_pnl"]))
    a_daily = a_pnl / Decimal(int(evidence["a_days"]))
    b_daily = b_pnl / Decimal(int(evidence["b_days"]))
    if a_daily <= 0:
        return "PASS", "NOT_COMPARABLE", None
    decline = (a_daily - b_daily) / a_daily * Decimal("100")
    decline_text = format(decline, "f")
    return (
        "REJECT" if decline > PRETEST_AB_THRESHOLD_PCT else "PASS",
        "DECLINE_GT_THRESHOLD" if decline > PRETEST_AB_THRESHOLD_PCT else "DECLINE_WITHIN_THRESHOLD",
        decline_text,
    )


def _surface_binding(
    manifest: Mapping[str, object], surface_path: Path | None,
) -> dict[str, object]:
    identity = {
        "surface_fingerprint": str(manifest["surface_fingerprint"]),
        "surface_id": str(manifest["surface_id"]),
        "source_content_digest": str(manifest["source_content_digest"]),
        "scope_digests": dict(sorted((str(key), str(value)) for key, value in dict(manifest["scope_digests"]).items())),
    }
    if "analysis_input_digest" in manifest:
        identity["analysis_input_digest"] = str(manifest["analysis_input_digest"])
    binding: dict[str, object] = {
        **identity,
        "surface_identity_sha256": _canonical_digest(identity),
    }
    if surface_path is not None:
        if surface_path.suffix.casefold() != ".duckdb":
            raise ValueError("fresh surface binding requires a DuckDB surface")
        actual = read_multiscope_surface(surface_path, decode=False)
        expected = {
            "surface_id": identity["surface_id"],
            "source_content_digest": identity["source_content_digest"],
            "scope_digests": identity["scope_digests"],
        }
        if "analysis_input_digest" in identity:
            expected["analysis_input_digest"] = identity["analysis_input_digest"]
        if any(actual.get(key) != value for key, value in expected.items()):
            raise ValueError("analysis and surface identities do not match")
        binding["surface_artifact_sha256"] = _file_digest(surface_path)
    return binding


def read_fresh_analysis_identity(analysis_path: Path | str) -> dict[str, object]:
    """Validate one committed fresh analysis and return what identifies it.

    The panel needs this to reopen an analysis it did not produce in the current
    session; a file that does not validate must raise rather than be listed.
    """
    manifest, analysis_id, _artifact = _read_analysis(Path(analysis_path).resolve())
    result = {
        "analysis_run_id": analysis_id,
        "surface_id": str(manifest["surface_id"]),
        "algorithm_version": str(manifest.get("algorithm_version", "")),
        "scope_keys": sorted(dict(manifest["scope_digests"])),
    }
    if "analysis_input_digest" in manifest:
        result["analysis_input_digest"] = str(manifest["analysis_input_digest"])
    return result


def filter_fresh_analysis_candidates(
    analysis_path: Path | str,
    analysis_run_id: str,
    criteria: Mapping[str, object] | Sequence[str] | None,
    *,
    pretest_ab_enabled: bool = False,
) -> FilterResult:
    """Evaluate the immutable fresh READY candidates with Phase 2 Pareto rules."""
    if type(pretest_ab_enabled) is not bool:
        raise ValueError("pretest_ab_enabled must be a boolean")
    analysis_file = Path(analysis_path).resolve()
    manifest, analysis_id, _ = _read_analysis(analysis_file)
    if str(analysis_run_id) != analysis_id:
        raise ValueError("fresh analysis run identity mismatch")
    supports_pretest = _supports_pretest_ab(manifest)
    if pretest_ab_enabled and not supports_pretest:
        raise ValueError("PRETEST_AB_EVIDENCE_UNAVAILABLE: rebuild the Source v6 surface and analysis")
    if isinstance(criteria, Mapping) and (
        set(criteria).difference(CRITERIA) or any(type(value) is not bool for value in criteria.values())
    ):
        raise ValueError("Phase 2 filters must be named booleans")
    enabled = _enabled_criteria(criteria)
    scope_keys = sorted(dict(manifest["scope_digests"]))
    scopes = {
        (parts[0], parts[1].upper(), parts[2])
        for scope in scope_keys
        if len(parts := scope.split("|")) == 3
    }
    if len(scopes) != len(scope_keys):
        raise ValueError("fresh analysis has malformed scope identity")
    connection = duckdb.connect(str(analysis_file), read_only=True)
    try:
        points = _validate_points(
            [row for scope in scope_keys for row in _rows(connection, "points", scope)], scopes,
            require_pretest_ab=supports_pretest,
        )
        structures = [
            row for scope in scope_keys for row in _rows(connection, "structures", scope)
            if row.get("status") == _READY_STATUS
        ]
    finally:
        connection.close()
    point_rows = points.to_dict("records")
    point_events = {str(row["point_id"]): int(row["point_event_count"]) for row in point_rows}
    point_pretest = {
        str(row["point_id"]): dict(row["pretest_ab"])
        for row in point_rows if isinstance(row.get("pretest_ab"), Mapping)
    }
    candidates: list[_Candidate] = []
    pretest_outcomes: dict[str, tuple[str, str, str | None]] = {}
    for structure in structures:
        candidate_id = str(structure.get("candidate_id", structure.get("structure_id", ""))).strip()
        orders = structure.get("orders")
        if not candidate_id or not isinstance(orders, list) or not orders:
            raise ValueError("READY fresh candidate has malformed orders")
        if int(structure.get("order_count", 0)) != len(orders):
            raise ValueError("READY fresh candidate order count disagrees with orders")
        values: dict[str, list[Decimal | int]] = {name: [] for name in CRITERIA}
        order_pretest: list[dict[str, object]] = []
        for order in orders:
            if not isinstance(order, Mapping):
                raise ValueError("READY fresh candidate has malformed order")
            point_id = str(order.get("point_id", ""))
            if point_id not in point_events:
                raise ValueError("READY fresh candidate references unknown point")
            if supports_pretest:
                order_pretest.append(point_pretest[point_id])
            values["source_pnl"].append(_decimal(order.get("source_pnl_pct"), "source_pnl_pct"))
            values["efficiency"].append(_decimal(order.get("source_efficiency"), "source_efficiency"))
            values["close_support"].append(_decimal(order.get("close_support"), "close_support"))
            values["point_event_count"].append(point_events[point_id])
        first_pretest = order_pretest[0] if order_pretest else None
        outcome = (
            _pretest_ab_outcome(first_pretest, pretest_ab_enabled)
            if isinstance(first_pretest, Mapping)
            else ("DISABLED", "LEGACY_ANALYSIS", None)
        )
        pretest_outcomes[candidate_id] = outcome
        payload = dict(structure)
        payload.update(
            pretest_ab=first_pretest,
            pretest_ab_orders=order_pretest,
            pretest_ab_enabled=pretest_ab_enabled,
            pretest_ab_status=outcome[0],
            pretest_ab_reason=outcome[1],
            pretest_ab_decline_pct=outcome[2],
        )
        candidates.append(_Candidate(
            candidate_id,
            str(structure.get("structure_id", "")),
            _comparison_key(structure, candidate_id, False),
            {name: tuple(value) for name, value in values.items()},
            payload,
        ))
    survivors = [candidate for candidate in candidates if pretest_outcomes[candidate.candidate_id][0] != "REJECT"]
    pretest_deferred = tuple(
        {
            **candidate.payload,
            "candidate_id": candidate.candidate_id,
            "comparison_key": candidate.comparison_key,
            "deferred_by": None,
            "deferred_by_candidate_id": None,
            "criterion": "pretest_ab",
            "defer_reason": pretest_outcomes[candidate.candidate_id][1],
            "filter_status": "DEFERRED_PRETEST_AB",
            "enabled_criteria": list(enabled),
        }
        for candidate in candidates
        if pretest_outcomes[candidate.candidate_id][0] == "REJECT"
    )
    grouped: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in survivors:
        grouped[candidate.comparison_key].append(candidate)
    standalone = {
        name: tuple(
            row for key in sorted(grouped)
            for row in _standalone_rows(tuple(grouped[key]), name)
        )
        for name in enabled
    }
    combined = tuple(sorted(
        (
            _audit_row(min(dominators, key=lambda item: (item.structure_id, item.candidate_id)), deferred, ",".join(enabled))
            for group in grouped.values()
            for deferred in group
            if (dominators := [candidate for candidate in group if _dominates(candidate, deferred, enabled)])
        ),
        key=lambda row: (str(row["comparison_key"]), str(row["candidate_id"])),
    ))
    candidate_payloads = {candidate.candidate_id: candidate.payload for candidate in candidates}
    combined = tuple(
        {
            **candidate_payloads.get(str(row["candidate_id"]), {}),
            **row,
            "filter_status": "DEFERRED_REDUNDANT",
        }
        for row in combined
    )
    combined_all = tuple(sorted(
        (*pretest_deferred, *combined),
        key=lambda row: (str(row["comparison_key"]), str(row["candidate_id"])),
    ))
    deferred_by = {str(row["candidate_id"]): row for row in combined}
    sizes = Counter(candidate.comparison_key for candidate in candidates)
    rows = tuple(
        {
            **candidate.payload,
            "candidate_id": candidate.candidate_id,
            "comparison_key": candidate.comparison_key,
            "comparison_group_size": sizes[candidate.comparison_key],
            "filter_status": (
                "DEFERRED_PRETEST_AB"
                if pretest_outcomes[candidate.candidate_id][0] == "REJECT"
                else "DEFERRED_REDUNDANT" if candidate.candidate_id in deferred_by else "READY_AFTER_FILTERS"
            ),
            "deferred_by": deferred_by.get(candidate.candidate_id, {}).get("deferred_by"),
            "deferred_by_candidate_id": deferred_by.get(candidate.candidate_id, {}).get("deferred_by_candidate_id"),
            "enabled_criteria": list(enabled),
        }
        for candidate in sorted(candidates, key=lambda item: (item.comparison_key, item.candidate_id))
    )
    return FilterResult(
        analysis_id, str(manifest["surface_id"]), enabled, rows, standalone, combined_all,
        len(candidates), len(candidates) - len(combined_all), len(combined_all), len(sizes),
        sum(size for size in sizes.values() if size > 1), pretest_ab_enabled,
    )


def list_fresh_analysis_shortlist(
    analysis_path: Path | str,
    analysis_run_id: str,
    criteria: Mapping[str, object] | Sequence[str] | None = None,
    *,
    pretest_ab_enabled: bool = False,
) -> dict[str, object]:
    """Return safe grouped candidate summaries from one immutable fresh analysis."""
    if type(pretest_ab_enabled) is not bool:
        raise ValueError("pretest_ab_enabled must be a boolean")
    analysis_file = Path(analysis_path).resolve()
    manifest, analysis_id, _ = _read_analysis(analysis_file)
    if str(analysis_run_id) != analysis_id:
        raise ValueError("fresh analysis run identity mismatch")
    filtered = (
        filter_fresh_analysis_candidates(
            analysis_file, analysis_id, criteria, pretest_ab_enabled=pretest_ab_enabled,
        )
        if criteria is not None or pretest_ab_enabled else None
    )
    connection = duckdb.connect(str(analysis_file), read_only=True)
    try:
        scope_keys = sorted(dict(manifest["scope_digests"]))
        rows = [
            row for scope in scope_keys
            for table in _CANDIDATE_TABLES
            for row in _rows(connection, table, scope)
        ]
        scope_facts = {
            scope: _shortlist_scope_facts(
                _rows(connection, "plateaus", scope), _rows(connection, "points", scope)
            )
            for scope in scope_keys
        }
    finally:
        connection.close()
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    by_candidate = {str(row["candidate_id"]): row for row in filtered.rows} if filtered else {}
    for row in rows:
        candidate_id = str(row.get("candidate_id", row.get("structure_id", ""))).strip()
        if not candidate_id:
            raise ValueError("fresh structure has no candidate identity")
        if candidate_id in seen:
            raise ValueError(f"fresh analysis has duplicate candidate identity: {candidate_id}")
        seen.add(candidate_id)
        phase2 = by_candidate.get(candidate_id)
        item = {
            "candidate_id": candidate_id,
            "pair": str(row.get("symbol", "")),
            "side": str(row.get("side", "")).upper(),
            "timeframe": str(row.get("timeframe", "")),
            "order_count": int(row.get("order_count", 0)),
            "status": str(row.get("status", "")),
        }
        if filtered is not None:
            item["filter_status"] = phase2["filter_status"] if phase2 else "DEFERRED"
            if phase2:
                item.update({
                    "pretest_ab_enabled": phase2["pretest_ab_enabled"],
                    "pretest_ab_status": phase2["pretest_ab_status"],
                    "pretest_ab_reason": phase2["pretest_ab_reason"],
                    "pretest_ab_decline_pct": phase2["pretest_ab_decline_pct"],
                })
        items.append(item)
    items.sort(key=lambda item: str(item["candidate_id"]))
    return {
        "analysis_run_id": analysis_id,
        "items": items,
        "groups": _shortlist_groups(items, scope_facts),
        "active_criteria": list(filtered.criteria) if filtered else [],
        "pretest_ab_enabled": pretest_ab_enabled,
    }


def _shortlist_scope_facts(
    plateaus: Sequence[Mapping[str, object]], points: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    plateau_ids = {str(row["plateau_id"]) for row in plateaus if row.get("plateau_id")}
    starts = [_parse_report_date(row.get("report_start")) for row in points]
    ends = [_parse_report_date(row.get("report_end")) for row in points]
    starts = [value for value in starts if value is not None]
    ends = [value for value in ends if value is not None]
    period = None
    if starts and ends:
        start, end = min(starts), max(ends)
        period = f"{start:%d.%m}-{end:%d.%m}"
    return {"plateau_count": len(plateau_ids), "period": period}


def _parse_report_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _shortlist_groups(
    items: Sequence[Mapping[str, object]], scope_facts: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    """One row per Pair · Side · TF, counted into the bucket of its own order count.

    The panel's table is headed `1ORD..4ORD`, so the counts have to come from the
    data. Sending one flat candidate list left the panel guessing, and it guessed
    by writing every order count into the last column.
    """
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for item in items:
        key = (str(item["pair"]), str(item["side"]), str(item["timeframe"]))
        group = grouped.setdefault(key, {
            "scope_key": "|".join(key),
            "pair": key[0], "side": key[1], "timeframe": key[2],
            "counts": {f"{order}ORD": 0 for order in _ORDER_BUCKETS},
            "ready": 0, "ready_after_filters": 0, "deferred": 0, "total": 0, "candidate_ids": [],
            **scope_facts.get("|".join(key), {"plateau_count": 0, "period": None}),
        })
        group["total"] = int(group["total"]) + 1
        bucket = f"{int(item['order_count'])}ORD"
        counts = group["counts"]
        # With active Phase 2 filters, bucket columns represent candidates that
        # remain READY; ALL and DEFERRED retain the complete context.
        if bucket in counts and item.get("filter_status", "READY_AFTER_FILTERS") == "READY_AFTER_FILTERS":
            counts[bucket] = int(counts[bucket]) + 1
        if str(item["status"]) == _READY_STATUS:
            group["ready"] = int(group["ready"]) + 1
            if item.get("filter_status", "READY_AFTER_FILTERS") == "READY_AFTER_FILTERS":
                group["ready_after_filters"] = int(group["ready_after_filters"]) + 1
                group["candidate_ids"].append(str(item["candidate_id"]))
        if "filter_status" in item and item["filter_status"] != "READY_AFTER_FILTERS":
            group["deferred"] = int(group["deferred"]) + 1
    for group in grouped.values():
        group["candidate_ids"] = sorted(group["candidate_ids"])
        if not any("filter_status" in item for item in items):
            group.pop("ready_after_filters")
            group.pop("deferred")
    return [grouped[key] for key in sorted(grouped)]


def generate_fresh_analysis_strategies(
    analysis_path: Path | str,
    analysis_run_id: str,
    candidate_ids: Sequence[str],
    selected_scopes: Sequence[tuple[str, str, str]],
    template_path: Path | str,
    output_dir: Path | str,
    config: AlgorithmConfig,
    *,
    surface_path: Path | str | None = None,
    filters: Mapping[str, object] | Sequence[str] | None = None,
    pretest_ab_enabled: bool = False,
) -> FreshAnalysisStrategies:
    """Generate EQUAL/INCOME JSON for exact READY candidates in one fresh run."""
    if type(pretest_ab_enabled) is not bool:
        raise ValueError("pretest_ab_enabled must be a boolean")
    analysis_file = Path(analysis_path).resolve()
    manifest, analysis_id, analysis_artifact_sha256 = _read_analysis(analysis_file)
    if str(analysis_run_id) != analysis_id:
        raise ValueError("fresh analysis run identity mismatch")
    scopes = normalize_analysis_scopes(selected_scopes)
    scope_set = set(scopes)
    scope_digests = dict(manifest["scope_digests"])
    missing_scopes = sorted("|".join(scope) for scope in scopes if "|".join(scope) not in scope_digests)
    if missing_scopes:
        raise ValueError(f"selected scope is absent from fresh analysis: {missing_scopes}")
    surface_binding = _surface_binding(
        manifest,
        None if surface_path is None else Path(surface_path).resolve(),
    )
    template_file, target = Path(template_path).resolve(), Path(output_dir).resolve()
    template = _template(template_file)
    connection = duckdb.connect(str(analysis_file), read_only=True)
    try:
        points = _validate_points(
            [row for scope in scopes for row in _rows(connection, "points", "|".join(scope))],
            scope_set,
            require_pretest_ab=_supports_pretest_ab(manifest),
        )
        all_structures = [
            row for scope in scopes for table in _CANDIDATE_TABLES
            for row in _rows(connection, table, "|".join(scope))
        ]
    finally:
        connection.close()
    ready_by_id: dict[str, dict[str, object]] = {}
    all_by_id: dict[str, dict[str, object]] = {}
    for raw in all_structures:
        identity = str(raw.get("candidate_id", raw.get("structure_id", ""))).strip()
        if not identity:
            raise ValueError("fresh structure has no candidate identity")
        if identity in all_by_id:
            raise ValueError(f"fresh analysis has duplicate candidate identity: {identity}")
        all_by_id[identity] = raw
        if raw.get("status") == _READY_STATUS:
            orders = raw.get("orders")
            if not isinstance(orders, (list, tuple)) or not orders or not all(isinstance(item, Mapping) for item in orders):
                raise ValueError("READY fresh candidate has malformed orders")
            candidate = dict(raw)
            candidate["orders"] = tuple(dict(item) for item in orders)
            ready_by_id[identity] = candidate
    filter_result = (
        filter_fresh_analysis_candidates(
            analysis_file, analysis_id, filters, pretest_ab_enabled=pretest_ab_enabled,
        )
        if filters is not None or pretest_ab_enabled else None
    )
    selected = tuple(sorted(
        str(row["candidate_id"])
        for row in filter_result.rows
        if row["filter_status"] == "READY_AFTER_FILTERS"
        and (str(row.get("symbol", "")), str(row.get("side", "")).upper(), str(row.get("timeframe", ""))) in scope_set
    )) if filter_result else tuple(sorted({str(item).strip() for item in candidate_ids if str(item).strip()}))
    if not selected:
        raise ValueError("no READY candidate selected")
    absent = sorted(set(selected).difference(all_by_id))
    if absent:
        raise ValueError(f"selected candidate is absent from fresh analysis: {absent}")
    not_ready = sorted(item for item in selected if item not in ready_by_id)
    if not_ready:
        raise ValueError(f"selected candidate is not READY: {not_ready}")
    structures = [ready_by_id[item] for item in selected]
    for structure in structures:
        structure_scope = (
            str(structure.get("symbol", "")),
            str(structure.get("side", "")).upper(),
            str(structure.get("timeframe", "")),
        )
        if structure_scope not in scope_set:
            raise ValueError("selected candidate is outside selected scopes")

    analysis_manifest_sha256 = _canonical_digest(dict(manifest))
    common: dict[str, object] = {
        "source_surface_id": str(manifest["surface_id"]),
        "surface_id": str(manifest["surface_id"]),
        "surface_fingerprint": str(manifest["surface_fingerprint"]),
        "source_content_digest": str(manifest["source_content_digest"]),
        "scope_digests": dict(sorted((str(key), str(value)) for key, value in scope_digests.items())),
        **surface_binding,
        "analysis_id": analysis_id,
        "analysis_run_id": analysis_id,
        "analysis_identity_sha256": analysis_id,
        "analysis_manifest_sha256": analysis_manifest_sha256,
        "analysis_artifact_sha256": analysis_artifact_sha256,
        "analysis_fingerprint": str(manifest["fingerprint"]),
        "algorithm_version": str(manifest["algorithm_version"]),
        "algorithm_config_sha256": str(manifest["algorithm_config_sha256"]),
        "listing_dates_sha256": str(manifest["listing_dates_sha256"]),
        "event_mode": EVENT_MODE,
        "selected_scopes": [list(scope) for scope in scopes],
        "phase2_filters": list(filter_result.criteria) if filter_result else [],
        "pretest_ab_enabled": pretest_ab_enabled,
        "pretest_ab": {
            "enabled": pretest_ab_enabled,
            "window_days": PRETEST_AB_WINDOW_DAYS,
            "decline_threshold_pct": "95",
            "contract_version": PRETEST_AB_CONTRACT,
        },
        "generator_schema_version": GENERATOR_SCHEMA,
    }
    if "analysis_input_digest" in manifest:
        common["analysis_input_digest"] = str(manifest["analysis_input_digest"])
    generated: list[dict[str, object]] = []
    variants: list[dict[str, object]] = []
    candidate_diagnostics: dict[str, dict[str, object]] = {}
    for structure in structures:
        candidate_identity = str(structure.get("candidate_id", structure["structure_id"]))
        diagnostics = _plateau_diagnostics(structure)
        candidate_diagnostics[candidate_identity] = _candidate_diagnostics(structure)
        methods = (LotMethod.EQUAL,) if int(structure["order_count"]) == 1 else (LotMethod.EQUAL, LotMethod.INCOME)
        for method in methods:
            strategy = generate_strategy(
                template, structure, allocate_lots(structure["orders"], method, config), method, config,
            )
            validate_strategy(strategy, structure, points, config)
            generated.append(strategy)
            variants.append({
                "strategy_name": strategy["name"], "structure_id": structure["structure_id"],
                "lot_method": method.value, "json_filename": f"{strategy['name']}.json", "variant_type": "MRS3",
                "candidate_identity": candidate_identity,
                **diagnostics,
            })
    validate_unique_names(generated)
    target.mkdir(parents=True, exist_ok=True)
    strategy_hashes = {
        str(row["json_filename"]): _v6_strategy_digest(strategy)
        for row, strategy in zip(variants, generated, strict=True)
    }
    candidate_names: dict[str, list[str]] = {}
    for row in variants:
        candidate_identity = str(row["candidate_identity"])
        candidate_names.setdefault(candidate_identity, []).append(str(row["strategy_name"]))
    manifest_unsigned: dict[str, object] = {
        "format_version": 1,
        **common,
        "candidate_identities": list(selected),
        "candidate_identity_to_strategy_names": {key: sorted(value) for key, value in sorted(candidate_names.items())},
        "candidate_diagnostics": {key: candidate_diagnostics[key] for key in sorted(candidate_diagnostics)},
        "strategy_json_sha256": strategy_hashes,
        "strategy_count": len(generated),
        "template_sha256": _file_digest(template_file),
    }
    generation_hash = _canonical_digest(manifest_unsigned)
    strategies = _publish_strategies(target, pd.DataFrame(variants), generated)
    manifest_path = target / "strategy_manifest.json"
    manifest = {**manifest_unsigned, "generation_manifest_sha256": generation_hash}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return FreshAnalysisStrategies(analysis_id, str(manifest["surface_id"]), strategies, manifest_path, len(generated))


generate_source_v6_fresh_analysis_strategies = generate_fresh_analysis_strategies
