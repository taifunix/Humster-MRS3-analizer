from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path

import pandas as pd

from .audit import write_audit_workbook


_CRITERIA = {
    "source_pnl": "Source PnL",
    "efficiency": "PnL-DD",
    "pnl_dd": "PnL-DD",
    "close_support": "CloseSupport",
    "point_event_count": "PointEventCount",
}
_CRITERION_ORDER = ("source_pnl", "efficiency", "close_support", "point_event_count")
_CRITERION_ALIASES = {
    "source_pnl": {"source_pnl", "source pnl", "Source PnL"},
    "efficiency": {"efficiency", "pnl_dd", "pnl/dd", "PnL-DD", "PnL/DD"},
    "close_support": {"close_support", "close support", "CloseSupport"},
    "point_event_count": {
        "point_event_count",
        "point event count",
        "PointEventCount",
    },
}
_BASE_HEADERS = (
    "candidate_id",
    "comparison_key",
    "filter_status",
    "deferred_by",
    "deferred_by_candidate_id",
    "criteria",
    "defer_reason",
)
_METRICS = (
    ("source_pnl", ("source_pnl_pct", "source_pnl", "pnl")),
    ("pnl_dd", ("source_efficiency", "pnl_dd", "efficiency", "source_pnl_dd")),
    ("close_support", ("close_support",)),
    ("point_event_count", ("point_event_count",)),
)
_AUDIT_HEADERS = _BASE_HEADERS + tuple(
    f"order{order}_{metric}_{side}"
    for order in range(1, 5)
    for side in ("a", "b")
    for metric, _aliases in _METRICS
)
_MISSING = object()


def _field(value: object, *names: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _row_mapping(row: object) -> dict[str, object]:
    if isinstance(row, Mapping):
        return dict(row)
    if is_dataclass(row):
        return asdict(row)
    if hasattr(row, "__dict__"):
        return dict(vars(row))
    raise TypeError("filter result rows must be mappings or record objects")


def _canonical_criterion(value: object) -> str:
    text = str(value).strip()
    lowered = text.lower()
    for canonical, aliases in _CRITERION_ALIASES.items():
        if lowered in {alias.lower() for alias in aliases}:
            return canonical
    raise ValueError(f"unsupported filter criterion: {value}")


def _criteria(criteria: Iterable[object]) -> tuple[str, ...]:
    enabled = {_canonical_criterion(item) for item in criteria}
    return tuple(name for name in _CRITERION_ORDER if name in enabled)


def _display_criteria(criteria: Sequence[str]) -> str:
    return ", ".join(_CRITERIA[name] for name in criteria)


def _sequence(value: object) -> tuple[object, ...]:
    if value is None or isinstance(value, (str, bytes)):
        return ()
    if isinstance(value, Mapping):
        return tuple(value[key] for key in sorted(value, key=str))
    if isinstance(value, Sequence):
        return tuple(value)
    return tuple(value) if isinstance(value, Iterable) else ()


def _flatten_orders(result: dict[str, object]) -> None:
    vector_names = {
        "source_pnl": "source_pnl",
        "efficiency": "pnl_dd",
        "close_support": "close_support",
        "point_event_count": "point_event_count",
    }
    for side, field in (("a", "a_values"), ("b", "b_values")):
        vectors = result.get(field)
        if not isinstance(vectors, Mapping):
            continue
        for source_name, output_name in vector_names.items():
            values = _sequence(vectors.get(source_name))
            for index, value in enumerate(values[:4], start=1):
                result[f"order{index}_{output_name}_{side}"] = value
    orders_by_side = {
        "a": _sequence(
            result.get("orders_a", result.get("order_metrics_a", result.get("a_orders")))
        ),
        "b": _sequence(
            result.get("orders_b", result.get("order_metrics_b", result.get("b_orders")))
        ),
    }
    for side, orders in orders_by_side.items():
        for index, order in enumerate(orders[:4], start=1):
            order_mapping = _row_mapping(order)
            for metric, aliases in _METRICS:
                value = _field(order_mapping, *aliases)
                if value is not None:
                    result.setdefault(f"order{index}_{metric}_{side}", value)


def _normal_row(row: object, criteria: Sequence[str]) -> dict[str, object]:
    result = _row_mapping(row)
    _flatten_orders(result)
    result["candidate_id"] = _field(result, "candidate_id", "candidateId", default="")
    result["comparison_key"] = _field(result, "comparison_key", "ComparisonKey", default="")
    if not result["comparison_key"]:
        raise ValueError("filter audit row is missing comparison_key")
    result["filter_status"] = _field(result, "filter_status", "status", default="")
    result["deferred_by"] = _field(result, "deferred_by", default=None)
    result["deferred_by_candidate_id"] = _field(
        result, "deferred_by_candidate_id", "deferred_by_candidate", default=None
    )
    raw_criteria = _field(result, "criteria", default=None)
    if isinstance(raw_criteria, set):
        result["criteria"] = ", ".join(str(item) for item in sorted(raw_criteria, key=str))
    elif isinstance(raw_criteria, (tuple, list)):
        result["criteria"] = ", ".join(str(item) for item in raw_criteria)
    else:
        result["criteria"] = (
            raw_criteria if raw_criteria is not None else _display_criteria(criteria)
        )
    result["defer_reason"] = _field(
        result,
        "defer_reason",
        default=("SAME_STRUCTURE_DOMINATED" if result["deferred_by"] else None),
    )
    return result


def _is_ready(row: Mapping[str, object]) -> bool:
    status = str(row.get("filter_status", ""))
    return status == "READY_AFTER_FILTERS" or (
        not row.get("deferred_by") and not status.startswith("DEFERRED")
    )


def _sorted_rows(rows: Iterable[object], criteria: Sequence[str]) -> list[dict[str, object]]:
    normalized = [_normal_row(row, criteria) for row in rows]
    return sorted(
        normalized,
        key=lambda row: (str(row.get("comparison_key", "")), str(row.get("candidate_id", ""))),
    )


def _result_field(result: object, name: str, default: object = None) -> object:
    value = _field(result, name, default=default)
    return default if value is None else value


def _criterion_rows(result: object, criterion: str) -> Iterable[object]:
    mapping = _result_field(
        result,
        "standalone",
        _result_field(result, "per_criterion", {}),
    )
    if not isinstance(mapping, Mapping):
        raise TypeError("filter result standalone/per_criterion must be a mapping")
    for key in (criterion, _CRITERIA[criterion]):
        if key in mapping:
            return mapping[key]
    if criterion == "efficiency" and "pnl_dd" in mapping:
        return mapping["pnl_dd"]
    return ()


def _target_path(output_path: Path | str) -> Path:
    raw = Path(output_path)
    if raw.exists() and raw.is_dir():
        return raw / "phase2_filter_audit.xlsx"
    if not raw.exists() and raw.suffix == "":
        return raw / "phase2_filter_audit.xlsx"
    return raw.with_suffix(".xlsx")


def _table(rows: Iterable[Mapping[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{header: row.get(header) for header in _AUDIT_HEADERS} for row in rows],
        columns=_AUDIT_HEADERS,
    )


def export_filter_audit(
    connection: object,
    run_id: str,
    criteria: Iterable[object],
    output_path: Path | str,
) -> Path:
    """Export one immutable Phase 2 filter view without mutating its analysis run."""
    enabled = _criteria(criteria)
    run = connection.execute(
        "select algorithm_version from analysis_runs where run_id=?", [run_id]
    ).fetchone()
    if run is None:
        raise ValueError("unknown analysis run")
    algorithm_version = str(run[0])
    # Localized import keeps the exporter usable while the shortlist engine evolves.
    from .analysis_shortlist import filter_analysis_candidates

    result = filter_analysis_candidates(connection, run_id, tuple(enabled))
    rows_source = _result_field(result, "rows", None)
    explicit_combined = _field(result, "combined", "combined_rows", default=_MISSING)
    has_explicit_combined = explicit_combined is not _MISSING
    combined_source = (
        explicit_combined
        if has_explicit_combined
        else rows_source if rows_source is not None else ()
    )
    all_rows = _sorted_rows(
        rows_source if rows_source is not None else combined_source,
        enabled,
    )
    ready_rows = [row for row in all_rows if _is_ready(row)]
    combined_rows = _sorted_rows(combined_source, enabled)
    deferred_rows = [row for row in combined_rows if not _is_ready(row)] if has_explicit_combined else [
        row for row in all_rows if not _is_ready(row)
    ]
    input_rows = all_rows + (combined_rows if has_explicit_combined else [])
    input_keys = {
        (str(row.get("candidate_id", "")), str(row.get("comparison_key", "")))
        for row in input_rows
    }

    tables: dict[str, pd.DataFrame] = {
        "Summary": pd.DataFrame(
            {
                "metric": (
                     "run_id",
                    "algorithm_version",
                    "input_count",
                    "active_criteria",
                    "ready_count",
                    "deferred_count",
                ),
                "value": (
                    run_id,
                    algorithm_version,
                    _result_field(result, "input_count", len(input_keys)),
                    _display_criteria(enabled),
                    _result_field(result, "ready_count", len(ready_rows)),
                    _result_field(result, "deferred_count", len(deferred_rows)),
                ),
            }
        ),
        "READY_AFTER_FILTERS": _table(ready_rows),
    }
    for criterion in enabled:
        tables[_CRITERIA[criterion]] = _table(
            _sorted_rows(_criterion_rows(result, criterion), enabled)
        )
    tables["DEFERRED_COMBINED"] = _table(deferred_rows)
    return write_audit_workbook(tables, _target_path(output_path))
