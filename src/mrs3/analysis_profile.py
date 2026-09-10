"""Typed, local-only settings that alter fresh Source v6 analysis results."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any

from .config import AlgorithmConfig, load_duckdb_import_settings
from .panel_settings import _atomic_write


_SECTIONS = {
    "eligibility": {"min_history_days", "base_rates", "shift_factors", "floor_boundary_bp", "floor_at_or_below", "floor_above", "min_point_events"},
    "economics": {"min_pnl_pct", "min_win_rate_pct", "max_dd_pct", "min_efficiency"},
    "geometry": {"canonical_shifts_bp", "ma_neighbor_radius"},
    "plateau": {"core_link_min", "envelope_min", "supported_link_min", "isolated_peak_relative", "equivalent_tolerance", "close_core_min", "close_supported_min"},
    "ready": {"base_min_points", "base_min_events_per_month", "base_slots", "multi_min_points", "multi_min_events_per_month"},
    "structures": {"gap_rules", "max_orders", "target_dd_pct"},
}
_TOP_LEVEL = frozenset({*_SECTIONS, "workers"})


def _decimal(value: object) -> str:
    return str(Decimal(str(value)))


def _number(value: object) -> int | float:
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("invalid analysis profile number") from error
    return int(decimal) if decimal == decimal.to_integral_value() else float(decimal)


def _read(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("analysis profile config must be an object")
    return document, text


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _project(config_path: Path) -> dict[str, object]:
    config = AlgorithmConfig.from_json(config_path)
    importer = load_duckdb_import_settings(config_path)
    return {
        "eligibility": {
            "min_history_days": _decimal(config.history_min_days),
            "base_rates": {key: _decimal(value) for key, value in sorted(config.base_rates.items())},
            "shift_factors": [{"max_bp": limit, "value": _decimal(value)} for limit, value in config.shift_factors],
            "floor_boundary_bp": config.absolute_floor_boundary_bp,
            "floor_at_or_below": config.absolute_floor_at_or_below,
            "floor_above": config.absolute_floor_above,
            "min_point_events": config.min_point_events,
        },
        "economics": {"min_pnl_pct": _decimal(config.economic_min_pnl_pct), "min_win_rate_pct": _decimal(config.economic_min_win_rate_pct), "max_dd_pct": _decimal(config.economic_max_dd_pct), "min_efficiency": _decimal(config.economic_min_efficiency)},
        "geometry": {"canonical_shifts_bp": list(config.canonical_shifts_bp), "ma_neighbor_radius": config.ma_neighbor_radius},
        "plateau": {"core_link_min": _decimal(config.core_link_min), "envelope_min": _decimal(config.plateau_envelope_min), "supported_link_min": _decimal(config.supported_link_min), "isolated_peak_relative": _decimal(config.isolated_peak_relative), "equivalent_tolerance": _decimal(config.equivalent_tolerance), "close_core_min": _decimal(config.close_core_min), "close_supported_min": _decimal(config.close_supported_min)},
        "ready": {"base_min_points": config.min_plateau_points, "base_min_events_per_month": config.min_plateau_events_per_month, "base_slots": config.base_one_order_slots, "multi_min_points": config.multi_order_min_plateau_points, "multi_min_events_per_month": config.multi_order_min_plateau_events_per_month},
        "structures": {"gap_rules": [{"lower_min_bp": lower, "lower_max_exclusive_bp": upper, "min_gap_bp": gap} for lower, upper, gap in config.gap_rules], "max_orders": config.max_orders, "target_dd_pct": _decimal(config.target_dd_pct)},
        "workers": importer.workers,
    }


def _write_validation_copy(document: dict[str, Any], path: Path) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def load_analysis_profile(config_path: Path) -> dict[str, object]:
    _read(config_path)
    return _project(config_path)


def _merge(document: dict[str, Any], profile: Mapping[str, object]) -> dict[str, Any]:
    if set(profile) != _TOP_LEVEL:
        raise ValueError("unknown analysis profile field")
    merged = json.loads(json.dumps(document))
    for section, keys in _SECTIONS.items():
        values = profile.get(section)
        if not isinstance(values, Mapping) or set(values) != keys:
            raise ValueError("unknown analysis profile field")
    if type(profile.get("workers")) is not int:
        raise ValueError("workers must be a positive integer")
    eligibility = profile["eligibility"]
    assert isinstance(eligibility, Mapping)
    merged.update({"history_min_days": eligibility["min_history_days"], "base_rate_tf": eligibility["base_rates"], "shift_factors": eligibility["shift_factors"], "absolute_floor_boundary_bp": eligibility["floor_boundary_bp"], "absolute_floor_at_or_below": eligibility["floor_at_or_below"], "absolute_floor_above": eligibility["floor_above"]})
    merged["event_filter"] = {**_section(merged, "event_filter"), "min_point_events": eligibility["min_point_events"]}
    economics = profile["economics"]; assert isinstance(economics, Mapping)
    merged.update({"economic_min_pnl_pct": _number(economics["min_pnl_pct"]), "economic_min_win_rate_pct": _number(economics["min_win_rate_pct"]), "economic_max_dd_pct": _number(economics["max_dd_pct"]), "economic_min_efficiency": _number(economics["min_efficiency"])})
    geometry = profile["geometry"]; assert isinstance(geometry, Mapping)
    merged["canonical_shifts_bp"] = geometry["canonical_shifts_bp"]
    merged["refine"] = {**_section(merged, "refine"), "ma_neighbor_radius": geometry["ma_neighbor_radius"]}
    plateau = profile["plateau"]; assert isinstance(plateau, Mapping)
    merged["plateau"] = {**_section(merged, "plateau"), **{key: plateau[key] for key in ("core_link_min", "envelope_min", "supported_link_min", "isolated_peak_relative", "equivalent_tolerance")}}
    merged["close_support"] = {**_section(merged, "close_support"), "core_min": plateau["close_core_min"], "supported_min": plateau["close_supported_min"]}
    ready = profile["ready"]; assert isinstance(ready, Mapping)
    merged["base_one_order"] = {**_section(merged, "base_one_order"), "min_plateau_points": ready["base_min_points"], "min_plateau_events_per_month": ready["base_min_events_per_month"], "slots": ready["base_slots"]}
    merged["multi_order_admission"] = {**_section(merged, "multi_order_admission"), "min_plateau_points": ready["multi_min_points"], "min_plateau_events_per_month": ready["multi_min_events_per_month"]}
    structures = profile["structures"]; assert isinstance(structures, Mapping)
    merged.update({"gap_rules": structures["gap_rules"], "max_orders": structures["max_orders"], "target_dd": structures["target_dd_pct"]})
    merged["duckdb_import"] = {**_section(merged, "duckdb_import"), "workers": profile["workers"]}
    return merged


def validate_analysis_profile(config_path: Path, profile: Mapping[str, object]) -> dict[str, object]:
    document, _ = _read(config_path)
    merged = _merge(document, profile)
    temporary = config_path.with_name(f".{config_path.name}.analysis-profile-check")
    try:
        try:
            return _project(_write_validation_copy(merged, temporary))
        except InvalidOperation as error:
            raise ValueError("invalid analysis profile value") from error
    finally:
        temporary.unlink(missing_ok=True)


def save_analysis_profile(config_path: Path, profile: Mapping[str, object]) -> dict[str, object]:
    document, previous = _read(config_path)
    merged = _merge(document, profile)
    temporary = config_path.with_name(f".{config_path.name}.analysis-profile-check")
    try:
        try:
            normalized = _project(_write_validation_copy(merged, temporary))
        except InvalidOperation as error:
            raise ValueError("invalid analysis profile value") from error
    finally:
        temporary.unlink(missing_ok=True)
    _atomic_write(config_path, merged, previous)
    return normalized
