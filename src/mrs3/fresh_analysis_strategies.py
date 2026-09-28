"""Generate READY strategies from a fresh compact multi-scope analysis DB.

This is intentionally a read-only adapter.  It does not open the legacy
Analysis DuckDB, read CSV, or recompute source facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from math import isfinite
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
from .config import AlgorithmConfig
from .lots import LotMethod, allocate_lots
from .strategy_json import generate_strategy, validate_strategy, validate_unique_names
from .source_v6 import _canonical_json
from .source_v6_surface_fresh import FINGERPRINT as SURFACE_FINGERPRINT, read_multiscope_surface


FINGERPRINT = "analysis-v6-fresh-compact-v2"
LEGACY_FINGERPRINT = "analysis-v6-fresh-compact-v1"
EVENT_MODE = "real_independent_events"
GENERATOR_SCHEMA = f"{V6_READY_GENERATOR_SCHEMA}-fresh-compact-shortlist-v2"
PRETEST_AB_CONTRACT = "source-v6-pretest-ab-v1"
PRETEST_AB_WINDOW_DAYS = 14
PRETEST_AB_THRESHOLD_PCT = Decimal("95")
_TABLES = ("points", "structures")
_ORDER_BUCKETS = (1, 2, 3, 4)
_READY_STATUS = "READY_MRS3_STRUCTURE"
_LEGACY_FRESH_FILTER_FIELDS = frozenset({"source_pnl", "efficiency", "close_support", "point_event_count"})
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
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _read_analysis(path: Path, *, artifact_sha256: str | None = None) -> tuple[dict[str, object], str, str]:
    if path.suffix.casefold() != ".duckdb":
        raise ValueError("fresh analysis generation requires a .analysis-v6.duckdb")
    digest_before = artifact_sha256 if artifact_sha256 is not None else _file_digest(path)
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
        return manifest, analysis_id, digest_before
    finally:
        connection.close()


def _validate_points(
    points: Sequence[Mapping[str, object]], scopes: set[tuple[str, str, str]], *, require_pretest_ab: bool = True,
) -> pd.DataFrame:
    from .fresh_shortlist import _canonical_id

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
        point_id = _canonical_id(raw["point_id"], "point_id")
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
        rows.append({**raw, "point_id": point_id, "side": scope[1], "_event_ids": event_ids})
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


def _canonical_id_sql_filter(expression: str, selected_ids: Sequence[str]) -> tuple[str, list[object]]:
    """Narrow SQL reads to canonical string IDs and equivalent integral JSON numbers."""
    clauses = [f"trim({expression}) in ({','.join('?' for _ in selected_ids)})"]
    parameters: list[object] = list(selected_ids)
    numeric_ids: list[float] = []
    for value in selected_ids:
        try:
            decimal_value = Decimal(value)
            if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
                continue
            numeric_value = float(decimal_value)
        except (InvalidOperation, OverflowError, ValueError):
            continue
        if isfinite(numeric_value) and numeric_value not in numeric_ids:
            numeric_ids.append(numeric_value)
    if numeric_ids:
        clauses.append(
            f"try_cast({expression} as double) in ({','.join('?' for _ in numeric_ids)})"
        )
        parameters.extend(numeric_ids)
    return f"({' or '.join(clauses)})", parameters


def fresh_shortlist_response(prepared: object, evaluation: object) -> dict[str, object]:
    """Render the cached v2 selection without rereading candidate payloads."""
    from .fresh_shortlist import FILTER_VERSION

    results = {item.candidate_id: item for item in evaluation.candidates}
    scope_facts = {
        item.scope_key: {"plateau_count": item.plateau_count, "period": item.period}
        for item in prepared.scopes
    }
    items = []
    for candidate in prepared.candidates:
        result = results[candidate.candidate_id]
        items.append({
            "candidate_id": candidate.candidate_id,
            "pair": candidate.pair,
            "side": candidate.side,
            "timeframe": candidate.timeframe,
            "order_count": candidate.order_count,
            "status": candidate.persisted_status,
            "filter_status": result.filter_status,
            "reason": result.reason,
            "dominator_candidate_id": result.dominator_candidate_id,
            "pretest_ab_enabled": evaluation.options[0],
            "pretest_ab_status": result.pretest_ab_status,
            "pretest_ab_reason": result.pretest_ab_reason,
            "pretest_ab_decline_pct": result.pretest_ab_decline_pct,
        })
    return {
        "analysis_run_id": evaluation.analysis_id,
        "filter_version": FILTER_VERSION,
        "filter_engine_version": evaluation.filter_engine_version,
        "artifact_sha256": evaluation.artifact_sha256,
        "selection_token": evaluation.selection_token,
        "applied_options": dict(zip(
            ("pretest_ab_enabled", "ladder_enabled", "pareto_enabled"),
            evaluation.options,
            strict=True,
        )),
        "items": items,
        "groups": _shortlist_groups(items, scope_facts, filtered=True),
        "active_criteria": [],
        "pretest_ab_enabled": evaluation.options[0],
    }


def _shortlist_groups(
    items: Sequence[Mapping[str, object]], scope_facts: Mapping[str, Mapping[str, object]],
    *, filtered: bool | None = None,
) -> list[dict[str, object]]:
    """One row per Pair · Side · TF, counted into the bucket of its own order count.

    The panel's table is headed `1ORD..4ORD`, so the counts have to come from the
    data. Sending one flat candidate list left the panel guessing, and it guessed
    by writing every order count into the last column.
    """
    has_filters = any("filter_status" in item for item in items) if filtered is None else filtered
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}

    def make_group(key: tuple[str, str, str]) -> dict[str, object]:
        group: dict[str, object] = {
            "scope_key": "|".join(key),
            "pair": key[0], "side": key[1], "timeframe": key[2],
            "counts": {f"{order}ORD": 0 for order in _ORDER_BUCKETS},
            "ready": 0, "ready_after_filters": 0, "deferred": 0, "total": 0, "candidate_ids": [],
            **scope_facts.get("|".join(key), {"plateau_count": 0, "period": None}),
        }
        return group

    for raw_key in scope_facts:
        parts = tuple(raw_key.split("|"))
        if len(parts) != 3:
            raise ValueError("fresh analysis has malformed scope identity")
        key = (parts[0], parts[1].upper(), parts[2])
        grouped[key] = make_group(key)
    for item in items:
        key = (str(item["pair"]), str(item["side"]), str(item["timeframe"]))
        group = grouped.get(key)
        if group is None:
            group = grouped[key] = make_group(key)
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
        if not has_filters:
            group.pop("ready_after_filters")
            group.pop("deferred")
    return [grouped[key] for key in sorted(grouped)]


def load_fresh_ready_candidates(
    analysis_path: Path | str,
    analysis_run_id: str,
    candidate_ids: Sequence[str],
    selected_scopes: Sequence[tuple[str, str, str]],
    *,
    expected_artifact_sha256: str,
) -> tuple[dict[str, object], pd.DataFrame, list[dict[str, object]]]:
    """Load only server-selected READY structures and their points."""
    from .fresh_shortlist import _canonical_id, _scope_key

    analysis_file = Path(analysis_path).resolve()
    manifest, actual_id, artifact_sha256 = _read_analysis(analysis_file)
    if actual_id != str(analysis_run_id):
        raise ValueError("fresh analysis run identity mismatch")
    if artifact_sha256 != expected_artifact_sha256:
        raise ValueError("STALE_SHORTLIST_SELECTION")
    scopes = normalize_analysis_scopes(selected_scopes)
    scope_set = set(scopes)
    scope_keys = tuple("|".join(scope) for scope in scopes)
    if not scope_keys or any(scope not in manifest["scope_digests"] for scope in scope_keys):
        raise ValueError("selected scope is absent from fresh analysis")
    selected = tuple(sorted(set(candidate_ids)))
    if not selected:
        raise ValueError("EMPTY_READY_SELECTION")
    scope_marks = ",".join("?" for _ in scope_keys)
    identity_expr = (
        "coalesce(json_extract_string(payload_json, '$.candidate_id'), "
        "json_extract_string(payload_json, '$.structure_id'))"
    )
    candidate_filter, candidate_parameters = _canonical_id_sql_filter(identity_expr, selected)
    point_expr = "json_extract_string(payload_json, '$.point_id')"
    connection = duckdb.connect(str(analysis_file), read_only=True)
    try:
        try:
            structure_rows = connection.execute(
                "select scope_key, payload_json from structures "
                f"where scope_key in ({scope_marks}) and "
                f"{candidate_filter}",
                [*scope_keys, *candidate_parameters],
            ).fetchall()
            selected_set = set(selected)
            decoded_structures: list[tuple[str, dict[str, object], str]] = []
            for sql_scope, raw in structure_rows:
                try:
                    structure = json.loads(str(raw))
                except (TypeError, json.JSONDecodeError) as error:
                    raise ValueError("fresh analysis structure contains invalid JSON") from error
                if not isinstance(structure, dict):
                    raise ValueError("fresh analysis structure must be an object")
                identity = _canonical_id(
                    structure.get("candidate_id", structure.get("structure_id")), "candidate identity",
                )
                if identity in selected_set:
                    decoded_structures.append((str(sql_scope), structure, identity))
            if not decoded_structures:
                raise ValueError("selected candidate is absent from fresh analysis")
            point_ids: set[str] = set()
            for _sql_scope, structure, _identity in decoded_structures:
                orders = structure.get("orders")
                if not isinstance(orders, list):
                    raise ValueError("READY fresh candidate has malformed orders")
                for order in orders:
                    if not isinstance(order, Mapping):
                        raise ValueError("READY fresh candidate has malformed order")
                    point_ids.add(_canonical_id(order.get("point_id"), "order point_id"))
            if not point_ids:
                raise ValueError("selected READY candidate has no point records")
            point_filter, point_parameters = _canonical_id_sql_filter(point_expr, tuple(sorted(point_ids)))
            point_rows = connection.execute(
                "select scope_key, payload_json from points "
                f"where scope_key in ({scope_marks}) and {point_filter}",
                [*scope_keys, *point_parameters],
            ).fetchall()
        except duckdb.Error as error:
            raise ValueError("fresh analysis selected candidates cannot be read") from error
    finally:
        connection.close()
    structures: dict[str, dict[str, object]] = {}
    scope_by_key = {"|".join(scope): scope for scope in scopes}
    for sql_scope, structure, identity in decoded_structures:
        if not isinstance(structure, dict) or _scope_key(structure, "structure") != tuple(sql_scope.split("|")):
            raise ValueError("fresh analysis structure scope disagrees with its table scope")
        if identity in structures or structure.get("status") != _READY_STATUS:
            raise ValueError("selected candidate is absent or not READY")
        structure["candidate_id"] = identity
        structure["structure_id"] = _canonical_id(structure.get("structure_id"), "structure_id")
        orders = structure.get("orders")
        if not isinstance(orders, list) or not orders:
            raise ValueError("READY fresh candidate has malformed orders")
        normalized_orders = []
        for order in orders:
            if not isinstance(order, Mapping):
                raise ValueError("READY fresh candidate has malformed orders")
            normalized_order = dict(order)
            normalized_order["point_id"] = _canonical_id(order.get("point_id"), "order point_id")
            normalized_orders.append(normalized_order)
        structure["orders"] = tuple(normalized_orders)
        structures[identity] = structure
    if set(structures) != set(selected):
        raise ValueError("selected candidate is absent from fresh analysis")
    points_payload: list[dict[str, object]] = []
    seen_points: set[str] = set()
    for sql_scope, raw in point_rows:
        try:
            point = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("fresh analysis point contains invalid JSON") from error
        if not isinstance(point, dict) or sql_scope not in scope_by_key:
            raise ValueError("fresh analysis point scope disagrees with selected scopes")
        point_id = _canonical_id(point.get("point_id"), "point_id")
        if point_id not in point_ids:
            continue
        if point_id in seen_points:
            raise ValueError("fresh analysis has duplicate point identity")
        seen_points.add(point_id)
        points_payload.append(point)
    if seen_points != point_ids:
        raise ValueError("selected candidate references an unknown point")
    points = _validate_points(
        points_payload, scope_set, require_pretest_ab=_supports_pretest_ab(manifest),
    )
    return manifest, points, [structures[item] for item in selected]


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
    selection: object,
) -> FreshAnalysisStrategies:
    """Generate EQUAL/INCOME JSON for exact READY candidates in one fresh run."""
    if type(pretest_ab_enabled) is not bool:
        raise ValueError("pretest_ab_enabled must be a boolean")
    if filters is not None:
        if isinstance(filters, Mapping):
            if set(filters).difference(_LEGACY_FRESH_FILTER_FIELDS) or any(type(value) is not bool for value in filters.values()):
                raise ValueError("filters must contain only recognized legacy booleans")
            if any(filters.values()):
                raise ValueError("stale shortlist client; use shortlist-v2 options")
        elif isinstance(filters, Sequence) and not isinstance(filters, (str, bytes)):
            if filters:
                raise ValueError("stale shortlist client; use shortlist-v2 options")
        else:
            raise ValueError("filters must contain only recognized legacy booleans")
    if selection is None:
        raise ValueError("a verified shortlist selection is required")
    analysis_file = Path(analysis_path).resolve()
    expected_digest = getattr(selection, "artifact_sha256", None)
    manifest, analysis_id, analysis_artifact_sha256 = _read_analysis(analysis_file)
    if str(analysis_run_id) != analysis_id:
        raise ValueError("fresh analysis run identity mismatch")
    if (
        getattr(selection, "analysis_id", None) != analysis_id
        or expected_digest != analysis_artifact_sha256
        or getattr(selection, "filter_version", None) != "shortlist-v2"
        or not isinstance(getattr(selection, "selection_token", None), str)
        or len(selection.selection_token) != 64
        or any(char not in "0123456789abcdef" for char in selection.selection_token)
    ):
        raise ValueError("STALE_SHORTLIST_SELECTION")
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
    selected = tuple(sorted(
        result.candidate_id
        for result in selection.candidates
        if result.filter_status == "READY_AFTER_FILTERS" and result.scope_key in {
            "|".join(scope) for scope in scopes
        }
    ))
    if selection.options[0] is not pretest_ab_enabled:
        raise ValueError("shortlist options disagree with generator request")
    if not selected:
        raise ValueError("EMPTY_READY_SELECTION")
    from .fresh_shortlist import _canonical_id

    try:
        requested = tuple(_canonical_id(value, "candidate identity") for value in candidate_ids)
    except (TypeError, ValueError) as error:
        raise ValueError("candidate IDs do not match verified READY selection") from error
    if len(requested) != len(set(requested)) or set(requested) != set(selected):
        raise ValueError("candidate IDs do not match verified READY selection")
    _selected_manifest, points, structures = load_fresh_ready_candidates(
        analysis_file, analysis_id, selected, scopes,
        expected_artifact_sha256=analysis_artifact_sha256,
    )

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
        "phase2_filters": [],
        "pretest_ab_enabled": pretest_ab_enabled,
        "pretest_ab": {
            "enabled": pretest_ab_enabled,
            "window_days": PRETEST_AB_WINDOW_DAYS,
            "decline_threshold_pct": "95",
            "contract_version": PRETEST_AB_CONTRACT,
        },
        "generator_schema_version": GENERATOR_SCHEMA,
    }
    if selection is not None:
        common["shortlist_v2"] = {
            "filter_version": selection.filter_version,
            "filter_engine_version": selection.filter_engine_version,
            "selection_token": selection.selection_token,
            "artifact_sha256": selection.artifact_sha256,
            "applied_options": dict(zip(
                ("pretest_ab_enabled", "ladder_enabled", "pareto_enabled"),
                selection.options,
                strict=True,
            )),
            "selected_candidate_ids": list(selected),
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
    if _file_digest(analysis_file) != analysis_artifact_sha256:
        raise ValueError("STALE_SHORTLIST_SELECTION")
    strategies = _publish_strategies(target, pd.DataFrame(variants), generated)
    manifest_path = target / "strategy_manifest.json"
    manifest = {**manifest_unsigned, "generation_manifest_sha256": generation_hash}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return FreshAnalysisStrategies(analysis_id, str(manifest["surface_id"]), strategies, manifest_path, len(generated))


generate_source_v6_fresh_analysis_strategies = generate_fresh_analysis_strategies
