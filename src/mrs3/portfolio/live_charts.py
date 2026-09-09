"""Read-only, dependency-free chart data for the Phase 2B monitor.

The chart layer deliberately knows only the PortfolioStore read protocol.  It
does not open a database, copy a baseline into the live database, or expose an
exchange client.  A caller supplies a read-only object with
``read_portfolio_run(run_id, attempt_id)`` (or a callable with that shape).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
import re
from urllib.parse import parse_qs, urlsplit
from decimal import ROUND_HALF_EVEN, localcontext
from typing import Any, Callable


CHART_MODEL_VERSION = "portfolio_live_chart_v1"
AVAILABLE = "AVAILABLE"
UNKNOWN = "UNKNOWN"
TESTED = "TESTED"
LIVE = "LIVE"
_PRESENTATION_KEYS = ("unit", "units", "currency", "time_basis", "applicability", "denominator", "denominator_basis", "metric", "series")


class _UnavailablePoint(ValueError):
    pass


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    for name in ("payload", "to_dict", "as_dict"):
        candidate = getattr(value, name, None)
        if callable(candidate):
            candidate = candidate()
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("chart value must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("chart value must be decimal") from error
    if not result.is_finite():
        raise ValueError("chart value must be finite")
    return result


def _decimal_text(value: Decimal, *, trim: bool = False) -> str:
    text = format(value, "f")
    if trim and "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return text


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        number = Decimal(value)
        # Existing normalized reports use ISO strings.  Millisecond support
        # keeps this reader useful with the original tester fixture shape.
        if abs(number) >= Decimal("10000000000"):
            number /= Decimal("1000")
        seconds = int(number)
        micros = int((number - seconds) * Decimal("1000000"))
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=micros)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise ValueError("chart timestamp is invalid") from error
    else:
        raise ValueError("chart timestamp is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("chart timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _elapsed_text(value: datetime, start: datetime) -> str:
    delta = value - start
    seconds = Decimal(delta.days * 86400 + delta.seconds) + (Decimal(delta.microseconds) / Decimal("1000000"))
    return _decimal_text(seconds, trim=True)


def _name(value: Any) -> str:
    return str(value).casefold().replace("_", "").replace("-", "")


def _metadata(raw: Any, defaults: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    if isinstance(raw, Mapping):
        for key in _PRESENTATION_KEYS:
            if key in raw:
                result[key] = raw[key]
        nested = raw.get("metadata")
        if isinstance(nested, Mapping):
            for key in _PRESENTATION_KEYS:
                if key in nested:
                    result[key] = nested[key]
    return result


def _point_items(raw: Any) -> tuple[Any, ...]:
    if isinstance(raw, Mapping):
        if "points" in raw:
            raw = raw["points"]
        elif "values" in raw:
            raw = raw["values"]
        elif "timestamp_utc" in raw or "timestamp" in raw or "time" in raw:
            return (raw,)
        else:
            return tuple({"timestamp_utc": key, "value": value, "source_ordinal": ordinal} for ordinal, (key, value) in enumerate(raw.items()))
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(raw)
    if raw is None:
        return ()
    return tuple(raw) if hasattr(raw, "__iter__") else ()


@dataclass(frozen=True, slots=True)
class _ValuePoint:
    timestamp: datetime
    value: Decimal
    ordinal: int
    metadata: Mapping[str, Any]


def _series(raw: Any, defaults: Mapping[str, Any]) -> tuple[_ValuePoint, ...]:
    descriptor = _mapping(raw)
    point_source = raw
    metadata = _metadata(descriptor, defaults)
    if descriptor is not None and ("points" in descriptor or "values" in descriptor):
        point_source = descriptor
    points: list[_ValuePoint] = []
    for ordinal, item in enumerate(_point_items(point_source)):
        item_map = _mapping(item)
        if item_map is not None:
            timestamp = item_map.get("timestamp_utc", item_map.get("timestamp", item_map.get("time")))
            value = item_map.get("value", item_map.get("numeric_value", item_map.get("equity", item_map.get("amount"))))
            point_ordinal = item_map.get("source_ordinal", item_map.get("ordinal", ordinal))
            available = item_map.get("availability", item_map.get("available", "AVAILABLE"))
            if isinstance(available, bool):
                available = AVAILABLE if available else UNKNOWN
            if str(available).upper() not in {AVAILABLE, "TRUE"}:
                raise _UnavailablePoint("series point is unavailable")
            point_metadata = _metadata(item_map, metadata)
        elif isinstance(item, (tuple, list)) and len(item) in {2, 3}:
            timestamp, value = item[:2]
            point_ordinal = item[2] if len(item) == 3 else ordinal
            point_metadata = metadata
        else:
            raise ValueError("series point is malformed")
        if isinstance(point_ordinal, bool) or not isinstance(point_ordinal, int) or point_ordinal < 0:
            raise ValueError("series point ordinal is invalid")
        points.append(_ValuePoint(_timestamp(timestamp), _decimal(value), point_ordinal, point_metadata))
    if len({(item.timestamp, item.ordinal) for item in points}) != len(points):
        raise ValueError("series points are duplicated")
    return tuple(sorted(points, key=lambda item: (item.timestamp, item.ordinal)))


def _selected_report_key(manifest: Mapping[str, Any]) -> str | None:
    for key in ("report_id", "selected_report", "selected_report_id", "report_member", "selected_member", "member"):
        value = manifest.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value)
    selection = manifest.get("report_selection")
    if isinstance(selection, Mapping):
        for key in ("report_id", "selected_report", "report_member", "member", "id"):
            value = selection.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                return str(value)
    composition = _composition(manifest.get("member_composition"))
    if composition is not None and len(composition) == 1:
        value = composition[0].get("strategy_id")
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value)
    return None


def _series_map(run: Mapping[str, Any], manifest: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    for key in ("series", "portfolio_series", "chart_series"):
        candidate = run.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    payload = _mapping(run.get("payload"))
    if payload is not None:
        for key in ("series", "portfolio_series", "chart_series"):
            candidate = payload.get(key)
            if isinstance(candidate, Mapping):
                return candidate
    facts = run.get("report_facts", run.get("facts"))
    if isinstance(facts, Mapping):
        selected = next(iter(facts.values())) if len(facts) == 1 else facts.get(_selected_report_key(manifest or {})) if _selected_report_key(manifest or {}) is not None else None
        fact = _mapping(selected)
        if fact is not None:
            fact_payload = _mapping(fact.get("payload"))
            for candidate in (fact.get("series"), fact_payload.get("series") if fact_payload is not None else None):
                if isinstance(candidate, Mapping):
                    return candidate
            if any(key in fact for key in ("equity", "wallet", "net_pnl", "drawdown", "initial_margin", "maintenance_margin", "im", "mm")):
                return fact
    return {}


def _report_facts_unresolved(run: Mapping[str, Any], manifest: Mapping[str, Any]) -> bool:
    facts = run.get("report_facts", run.get("facts"))
    return isinstance(facts, Mapping) and len(facts) > 1 and not _series_map(run, manifest)


def _series_lookup(series: Mapping[str, Any], *names: str) -> Any:
    wanted = {_name(item) for item in names}
    for key, value in series.items():
        if _name(key) in wanted:
            return value
    return None


def _composition(value: Any) -> tuple[Mapping[str, Any], ...] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    result: list[Mapping[str, Any]] = []
    for item in value:
        item_map = _mapping(item)
        if item_map is None:
            if isinstance(item, (str, int)) and not isinstance(item, bool):
                item_map = {"strategy_id": str(item)}
            else:
                return None
        result.append(item_map)
    return tuple(result)


def _composition_equal(expected: Any, actual: Any) -> bool:
    wanted = _composition(expected)
    observed = _composition(actual)
    if wanted is None or observed is None:
        return False
    if len(wanted) != len(observed):
        return False
    keys = tuple(sorted({key for item in wanted for key in item}, key=str))
    if not keys:
        return False
    return sorted((tuple(item.get(key) for key in keys) for item in wanted), key=repr) == sorted((tuple(item.get(key) for key in keys) for item in observed), key=repr)


def _actual_composition(run: Mapping[str, Any]) -> Any:
    for key in ("member_composition", "composition", "members"):
        if key in run:
            return run[key]
    reports = run.get("reports")
    if isinstance(reports, (list, tuple)):
        members = []
        for item in reports:
            if isinstance(item, (list, tuple)) and item:
                members.append({"strategy_id": item[0]})
            elif isinstance(item, Mapping):
                members.append(item if "strategy_id" in item else {"strategy_id": item.get("member", item.get("id")), **dict(item)})
        if members:
            return members
    payload = _mapping(run.get("payload"))
    if payload is not None and "members" in payload:
        return payload["members"]
    facts = run.get("report_facts", run.get("facts"))
    if isinstance(facts, Mapping) and facts:
        return [{"strategy_id": key} for key in facts]
    return None


def _has_explicit_composition(run: Mapping[str, Any]) -> bool:
    if any(key in run for key in ("member_composition", "composition", "members")):
        return True
    payload = _mapping(run.get("payload"))
    return payload is not None and any(key in payload for key in ("member_composition", "composition", "members"))


def _run_field(run: Mapping[str, Any], key: str) -> Any:
    if key in run:
        return run[key]
    payload = _mapping(run.get("payload"))
    return payload.get(key) if payload is not None else None


def _read_baseline(reader: Any, run_id: str, attempt_id: str) -> Mapping[str, Any] | None:
    method: Callable[..., Any] | None = getattr(reader, "read_portfolio_run", None)
    if not callable(method) and callable(reader):
        method = reader
    if method is None:
        return None
    value = method(run_id, attempt_id)
    return _mapping(value)


def _point_dict(item: _ValuePoint, start: datetime, source: str, provenance: Mapping[str, Any], *, value: Decimal | None = None, trim: bool = False, extra: Mapping[str, Any] | None = None) -> "ChartPoint":
    merged = dict(provenance)
    for key in _PRESENTATION_KEYS:
        if key in item.metadata:
            merged[key] = item.metadata[key]
    if extra:
        merged.update(extra)
    return ChartPoint(
        timestamp_utc=_timestamp_text(item.timestamp),
        elapsed_from_start=_elapsed_text(item.timestamp, start),
        value=None if value is None else _decimal_text(value, trim=trim),
        source=source,
        availability=AVAILABLE if value is not None else UNKNOWN,
        provenance=merged,
    )


@dataclass(frozen=True, slots=True)
class ChartPoint:
    timestamp_utc: str
    elapsed_from_start: str
    value: str | None
    source: str
    availability: str
    provenance: Mapping[str, Any]
    reason: str | None = None

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "elapsed_from_start": self.elapsed_from_start,
            "value": self.value,
            "source": self.source,
            "availability": self.availability,
            "provenance": dict(self.provenance),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ChartResult:
    version: str
    view: str
    availability: str
    source: str
    points: tuple[ChartPoint, ...] = ()
    reason: str | None = None
    provenance: Mapping[str, Any] = None  # type: ignore[assignment]
    tested_points: tuple[ChartPoint, ...] = ()
    live_points: tuple[ChartPoint, ...] = ()

    def __post_init__(self) -> None:
        if self.provenance is None:
            object.__setattr__(self, "provenance", {})

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "view": self.view,
            "availability": self.availability,
            "source": self.source,
            "points": [item.as_dict() for item in self.points],
            "reason": self.reason,
            "provenance": dict(self.provenance),
            "tested_points": [item.as_dict() for item in self.tested_points],
            "live_points": [item.as_dict() for item in self.live_points],
        }


def _unknown(view: str, reason: str, provenance: Mapping[str, Any] | None = None, *, tested: tuple[ChartPoint, ...] = (), live: tuple[ChartPoint, ...] = ()) -> ChartResult:
    return ChartResult(CHART_MODEL_VERSION, view, UNKNOWN, "TESTED" if tested else "LIVE" if live else "UNKNOWN", (), reason, dict(provenance or {}), tested, live)


def _manifest_map(manifest: Any) -> Mapping[str, Any] | None:
    value = _mapping(manifest)
    if value is None:
        return None
    return value


def _validate_pins(run: Mapping[str, Any], manifest: Mapping[str, Any], run_id: str, attempt_id: str, expected_semantic_digest: str | None, expected_metrics_version: str | None, expected_series_version: str | None, expected_members: Any) -> str | None:
    checks = {
        "semantic_digest": expected_semantic_digest,
        "metrics_version": expected_metrics_version,
        "series_version": expected_series_version,
        "portfolio_set": manifest.get("portfolio_set"),
        "evaluation": manifest.get("evaluation"),
    }
    for key, expected in checks.items():
        if expected is None:
            continue
        observed = _run_field(run, key)
        if observed is None or str(observed) != str(expected):
            return "BASELINE_PIN_MISMATCH"
    if _run_field(run, "run_id") is None or str(_run_field(run, "run_id")) != run_id:
        return "BASELINE_PIN_MISMATCH"
    if _run_field(run, "attempt_id") is None or str(_run_field(run, "attempt_id")) != attempt_id:
        return "BASELINE_PIN_MISMATCH"
    actual_members = _actual_composition(run)
    if expected_members is not None:
        if not _has_explicit_composition(run):
            return "COMPOSITION_UNVERIFIABLE"
        if not _composition_equal(expected_members, actual_members):
            expected_rows, actual_rows = _composition(expected_members), _composition(actual_members)
            expected_keys = {key for item in expected_rows or () for key in item}
            if actual_rows is None or any(key not in item for item in actual_rows for key in expected_keys):
                return "COMPOSITION_UNVERIFIABLE"
            return "BASELINE_PIN_MISMATCH"
    if expected_series_version is not None or expected_metrics_version is not None:
        for raw in _series_map(run).values():
            descriptor = _mapping(raw)
            if descriptor is None:
                continue
            for key, expected in (("series_version", expected_series_version), ("metrics_version", expected_metrics_version)):
                if expected is not None and descriptor.get(key) is not None and str(descriptor[key]) != str(expected):
                    return "BASELINE_PIN_MISMATCH"
    return None


def _provenance(manifest: Mapping[str, Any], run_id: str, attempt_id: str, run: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "manifest_id": manifest.get("manifest_id"),
        "semantic_digest": manifest.get("semantic_digest", _run_field(run, "semantic_digest")),
        "series_version": manifest.get("series_version", _run_field(run, "series_version")),
        "metrics_version": manifest.get("metrics_version", _run_field(run, "metrics_version")),
    }
    for key in ("portfolio_set", "evaluation", "unit", "units", "currency", "time_basis", "applicability"):
        value = manifest.get(key, _run_field(run, key))
        if value is not None:
            result[key] = value
    return result


def _build_value_points(values: tuple[_ValuePoint, ...], source: str, provenance: Mapping[str, Any], *, start: datetime | None = None, transform: Callable[[tuple[_ValuePoint, ...], int], Decimal] | None = None, trim: bool = False, extra: Mapping[str, Any] | None = None) -> tuple[ChartPoint, ...]:
    if not values:
        return ()
    start = start or min(item.timestamp for item in values)
    result: list[ChartPoint] = []
    for index, item in enumerate(values):
        value = transform(values, index) if transform is not None else item.value
        result.append(_point_dict(item, start, source, provenance, value=value, trim=trim, extra=extra))
    return tuple(result)


def _map_values(values: tuple[_ValuePoint, ...], transform: Callable[[tuple[_ValuePoint, ...], int], Decimal]) -> tuple[_ValuePoint, ...]:
    return tuple(
        _ValuePoint(item.timestamp, transform(values, index), item.ordinal, item.metadata)
        for index, item in enumerate(values)
    )


def _label_values(values: tuple[_ValuePoint, ...], metric: str, series: str) -> tuple[_ValuePoint, ...]:
    return tuple(_ValuePoint(item.timestamp, item.value, item.ordinal, {**item.metadata, "metric": metric, "series": series}) for item in values)


def _overlay_metadata_compatible(tested: tuple[_ValuePoint, ...], live: tuple[_ValuePoint, ...], manifest: Mapping[str, Any]) -> bool:
    if not tested or not live:
        return False
    for keys in (("unit", "units"), ("currency",), ("time_basis",), ("applicability",)):
        if keys == ("currency",):
            units = {
                str(item.metadata.get("unit", item.metadata.get("units", ""))).casefold()
                for item in (*tested, *live)
            }
            if units and units <= {"%", "percent", "pct"}:
                continue
        def first(meta: Mapping[str, Any], *, fallback: bool) -> Any:
            for key in keys:
                if meta.get(key) is not None:
                    return meta[key]
            if fallback:
                for key in keys:
                    if manifest.get(key) is not None:
                        return manifest[key]
            return None
        tested_dimensions = {first(item.metadata, fallback=True) for item in tested}
        live_dimensions = {first(item.metadata, fallback=False) for item in live}
        if len(tested_dimensions) != 1 or len(live_dimensions) != 1:
            return False
        left, right = next(iter(tested_dimensions)), next(iter(live_dimensions))
        if left is None or right is None or str(left) != str(right):
            return False
    return True


def _margin_values(series: Mapping[str, Any], defaults: Mapping[str, Any]) -> tuple[tuple[_ValuePoint, ...], tuple[_ValuePoint, ...], tuple[_ValuePoint, ...]] | None:
    im_raw = _series_lookup(series, "initial_margin", "im")
    mm_raw = _series_lookup(series, "maintenance_margin", "mm")
    denominator_name = "margin_balance" if _series_lookup(series, "margin_balance") is not None else "equity"
    denominator_raw = _series_lookup(series, denominator_name)
    if im_raw is None or mm_raw is None or denominator_raw is None:
        return None
    try:
        im_values, mm_values = _series(im_raw, defaults), _series(mm_raw, defaults)
        denominator_values = _series(denominator_raw, defaults)
    except _UnavailablePoint:
        raise
    except (ValueError, TypeError, OverflowError):
        return None
    if not im_values or not mm_values or not denominator_values:
        return None
    if len(im_values) != len(mm_values) or any(left.timestamp != right.timestamp for left, right in zip(im_values, mm_values)):
        return None
    denominator_by_timestamp = {item.timestamp: item for item in denominator_values}

    def loads(values: tuple[_ValuePoint, ...]) -> tuple[_ValuePoint, ...] | None:
        result: list[_ValuePoint] = []
        for item in values:
            denominator = denominator_by_timestamp.get(item.timestamp)
            if denominator is None or denominator.value <= 0:
                return None
            for keys in (("unit", "units"), ("currency",), ("time_basis",), ("applicability",)):
                if any(item.metadata.get(key) is not None and denominator.metadata.get(key) is not None and str(item.metadata[key]) != str(denominator.metadata[key]) for key in keys):
                    return None
            metadata = dict(item.metadata)
            metadata["unit"] = "%"
            metadata["units"] = "%"
            metadata.pop("currency", None)
            metadata["denominator"] = denominator_name
            metadata["denominator_basis"] = "same_timestamp"
            result.append(_ValuePoint(item.timestamp, item.value / denominator.value * Decimal("100"), item.ordinal, metadata))
        return tuple(result)

    im_load_values, mm_load_values = loads(im_values), loads(mm_values)
    if im_load_values is None or mm_load_values is None:
        return None
    return im_load_values, mm_load_values, im_load_values + mm_load_values


def _live_input(live: Any, view: str) -> tuple[Any, Mapping[str, Any] | None, bool]:
    value = _mapping(live)
    if value is None:
        return live, None, False
    # A descriptor with points is itself a series.  A mapping keyed by view is
    # the convenient multi-series form used by LiveStore adapters.
    if any(key in value for key in ("points", "values", "timestamp_utc", "timestamp", "time")):
        return live, _metadata(value, {}), False
    aliases = (view,)
    if view in {"drawdown", "net_pnl", "cumulative_pnl"}:
        aliases = (view, "equity", "wallet")
    elif view == "margin_load":
        aliases = ("margin_load", "initial_margin", "im")
    candidate = _series_lookup(value, *aliases)
    if candidate is not None:
        return candidate, _metadata(value, {}), _series_lookup(value, view) is not None
    series = value.get("series")
    if isinstance(series, Mapping):
        candidate = _series_lookup(series, *aliases)
        if candidate is not None:
            return candidate, _metadata(value, {}), _series_lookup(series, view) is not None
    return live, value, False


def build_chart_model(
    reader: Any,
    manifest: Any = None,
    *,
    view: str = "equity",
    run_id: str | None = None,
    attempt_id: str | None = None,
    expected_semantic_digest: str | None = None,
    expected_metrics_version: str | None = None,
    expected_series_version: str | None = None,
    expected_member_composition: Any = None,
    live: Any = None,
    overlay: bool = False,
    mode: str | None = None,
    pair: tuple[str, str] | None = None,
    contour: str = "NONE",
    bounded: bool = False,
    raw_fact_count: int | None = None,
    pair_options_count: int | None = None,
) -> ChartResult:
    """Build one chart-ready view from a pinned tested run and optional live points.

    ``live`` is intentionally a plain descriptor (or a reader adapter's
    returned descriptor).  It is never written to the PortfolioStore.
    """
    if mode is not None or bounded:
        return build_chart_overview(reader, manifest, mode=mode or PORTFOLIO, pair=pair, live=live, contour=contour, raw_fact_count=raw_fact_count, pair_options_count=pair_options_count)  # type: ignore[return-value]
    view = str(view).casefold().replace("-", "_")
    manifest_map = _manifest_map(manifest)
    if manifest_map is None:
        return _unknown(view, "BASELINE_PIN_MISMATCH")
    run_id = run_id or _string(manifest_map.get("run_id"))
    attempt_id = attempt_id or _string(manifest_map.get("attempt_id"))
    expected_semantic_digest = expected_semantic_digest or _string(manifest_map.get("semantic_digest"))
    expected_metrics_version = expected_metrics_version or _string(manifest_map.get("metrics_version"))
    expected_series_version = expected_series_version or _string(manifest_map.get("series_version"))
    expected_member_composition = expected_member_composition if expected_member_composition is not None else manifest_map.get("member_composition")
    if (
        not run_id
        or not attempt_id
        or not expected_semantic_digest
        or not expected_metrics_version
        or not expected_series_version
        or manifest_map.get("portfolio_set") is None
        or manifest_map.get("evaluation") is None
        or expected_member_composition is None
    ):
        return _unknown(view, "BASELINE_PIN_MISMATCH")
    try:
        run = _read_baseline(reader, run_id, attempt_id)
    except Exception:
        return _unknown(view, "BASELINE_UNAVAILABLE")
    if run is None:
        return _unknown(view, "BASELINE_PIN_MISMATCH")
    pin_reason = _validate_pins(run, manifest_map, run_id, attempt_id, expected_semantic_digest, expected_metrics_version, expected_series_version, expected_member_composition)
    if pin_reason is not None:
        return _unknown(view, pin_reason)
    provenance = _provenance(manifest_map, run_id, attempt_id, run)
    series_map = _series_map(run, manifest_map)
    defaults = {**manifest_map, **provenance}
    canonical_view = view
    drawdown_percent = view == "drawdown_pct"
    if view in {"pnl", "net", "net_pnl", "cumulative_pnl", "cumulative_net_pnl"}:
        canonical_view = "net_pnl"
    elif view in {"dd", "drawdown_pct", "equity_drawdown"}:
        canonical_view = "drawdown"
    elif view in {"im", "initial_margin_load", "margin_im_load"}:
        canonical_view = "im_load"
    elif view in {"mm", "maintenance_margin_load", "margin_mm_load"}:
        canonical_view = "mm_load"

    tested_extra: Mapping[str, Any] | None = None
    tested_basis_values: tuple[_ValuePoint, ...]
    if canonical_view in {"im_load", "mm_load", "margin_load"}:
        try:
            margin = _margin_values(series_map, defaults)
        except _UnavailablePoint:
            return _unknown(view, "SERIES_POINT_UNAVAILABLE", provenance)
        if margin is None:
            reason = "MULTIPLE_REPORT_FACTS_UNRESOLVED" if _report_facts_unresolved(run, manifest_map) else "MARGIN_DENOMINATOR_UNAVAILABLE"
            return _unknown(view, reason, provenance)
        im_load_values, mm_load_values, overlay_values = margin
        if canonical_view == "im_load":
            tested_values = im_load_values
        elif canonical_view == "mm_load":
            tested_values = mm_load_values
        else:
            tested_values = tuple(item for pair in zip(_label_values(im_load_values, "IM", "initial_margin"), _label_values(mm_load_values, "MM", "maintenance_margin")) for item in pair)
        tested_basis_values = overlay_values
    else:
        base_name = "equity" if canonical_view in {"equity", "net_pnl", "drawdown"} else canonical_view
        raw = _series_lookup(series_map, base_name)
        if raw is None and canonical_view in {"equity", "net_pnl", "drawdown"}:
            raw = _series_lookup(series_map, "wallet")
        if raw is None:
            reason = "MULTIPLE_REPORT_FACTS_UNRESOLVED" if _report_facts_unresolved(run, manifest_map) else "SERIES_UNAVAILABLE"
            return _unknown(view, reason, provenance)
        try:
            base_values = _series(raw, defaults)
        except _UnavailablePoint:
            return _unknown(view, "SERIES_POINT_UNAVAILABLE", provenance)
        except (ValueError, TypeError, OverflowError):
            return _unknown(view, "SERIES_UNAVAILABLE", provenance)
        if not base_values:
            return _unknown(view, "SERIES_UNAVAILABLE", provenance)
        tested_basis_values = base_values
        if canonical_view == "net_pnl":
            direct = _series_lookup(series_map, "net_pnl", "cumulative_pnl", "pnl")
            try:
                tested_values = _series(direct, defaults) if direct is not None else _map_values(base_values, lambda items, index: items[index].value - items[0].value)
            except _UnavailablePoint:
                return _unknown(view, "SERIES_POINT_UNAVAILABLE", provenance)
            except (ValueError, TypeError, OverflowError):
                return _unknown(view, "SERIES_UNAVAILABLE", provenance)
        elif canonical_view == "drawdown":
            direct = _series_lookup(series_map, "drawdown_pct") if drawdown_percent else _series_lookup(series_map, "drawdown")

            def drawdown(items: tuple[_ValuePoint, ...], index: int) -> Decimal:
                high_water = max(item.value for item in items[: index + 1])
                if drawdown_percent:
                    if high_water <= 0:
                        raise ValueError("drawdown percentage denominator is unavailable")
                    return (items[index].value - high_water) / high_water * Decimal("100")
                return items[index].value - high_water
            try:
                tested_values = _series(direct, defaults) if direct is not None else _map_values(base_values, drawdown)
                if drawdown_percent and direct is not None:
                    if any(str(item.metadata.get("unit", item.metadata.get("units", ""))).casefold() not in {"%", "percent", "pct"} or item.metadata.get("denominator") is None for item in tested_values):
                        return _unknown(view, "DRAWDOWN_PERCENT_UNAVAILABLE", provenance)
                elif drawdown_percent:
                    tested_extra = {"unit": "%", "units": "%", "denominator": "equity_high_water", "denominator_basis": "running_high_water"}
            except _UnavailablePoint:
                return _unknown(view, "SERIES_POINT_UNAVAILABLE", provenance)
            except (ValueError, TypeError, OverflowError):
                return _unknown(view, "DRAWDOWN_PERCENT_UNAVAILABLE" if drawdown_percent else "SERIES_UNAVAILABLE", provenance)
        else:
            tested_values = base_values

    if not tested_values:
        return _unknown(view, "SERIES_UNAVAILABLE", provenance)
    tested_elapsed_origin = min(item.timestamp for item in tested_values)
    chart_provenance = {**provenance, "elapsed_origin_utc": _timestamp_text(tested_elapsed_origin)}
    if canonical_view in {"im_load", "mm_load", "margin_load"}:
        chart_provenance.update({"unit": "%", "units": "%", "denominator": "margin_balance" if _series_lookup(series_map, "margin_balance") is not None else "equity", "denominator_basis": "same_timestamp"})
        chart_provenance.pop("currency", None)
    tested_points = _build_value_points(tested_values, TESTED, chart_provenance, start=tested_elapsed_origin, trim=canonical_view in {"im_load", "mm_load", "margin_load"}, extra=tested_extra)

    if not tested_points:
        return _unknown(view, "SERIES_UNAVAILABLE", provenance)
    if live is None:
        return ChartResult(CHART_MODEL_VERSION, view, AVAILABLE, TESTED, tested_points, None, chart_provenance, tested_points, ())
    live_raw, live_meta, live_direct = _live_input(live, canonical_view)
    live_defaults = dict(live_meta or {})
    if canonical_view in {"im_load", "mm_load", "margin_load"}:
        live_map = _mapping(live)
        if live_map is not None and isinstance(live_map.get("series"), Mapping):
            live_map = live_map["series"]
        try:
            margin = _margin_values(live_map or {}, live_defaults)
        except _UnavailablePoint:
            return _unknown(view, "LIVE_SERIES_POINT_UNAVAILABLE", chart_provenance, tested=tested_points)
        if margin is None:
            return _unknown(view, "LIVE_SERIES_UNAVAILABLE", chart_provenance, tested=tested_points)
        im_live, mm_live, live_overlay_values = margin
        live_values = im_live if canonical_view == "im_load" else mm_live if canonical_view == "mm_load" else tuple(item for pair in zip(_label_values(im_live, "IM", "initial_margin"), _label_values(mm_live, "MM", "maintenance_margin")) for item in pair)
    else:
        try:
            live_values = _series(live_raw, live_defaults)
        except _UnavailablePoint:
            return _unknown(view, "LIVE_SERIES_POINT_UNAVAILABLE", chart_provenance, tested=tested_points)
        except (ValueError, TypeError, OverflowError):
            return _unknown(view, "LIVE_SERIES_UNAVAILABLE", chart_provenance, tested=tested_points)
        if not live_values:
            return _unknown(view, "LIVE_SERIES_UNAVAILABLE", chart_provenance, tested=tested_points)
        live_overlay_values = live_values
    if canonical_view == "net_pnl" and not live_direct:
        live_values = _map_values(live_values, lambda items, index: items[index].value - tested_basis_values[0].value)
        live_extra = {"derived_basis": "TESTED_BASELINE_ORIGIN", "derived_from": "TESTED"}
    elif canonical_view == "drawdown" and not live_direct:
        def live_drawdown(items: tuple[_ValuePoint, ...], index: int) -> Decimal:
            high_water = max(item.value for item in (*tested_basis_values, *items[: index + 1]))
            if drawdown_percent:
                if high_water <= 0:
                    raise ValueError("drawdown percentage denominator is unavailable")
                return (items[index].value - high_water) / high_water * Decimal("100")
            return items[index].value - high_water
        try:
            live_values = _map_values(live_values, live_drawdown)
        except (ValueError, TypeError, OverflowError):
            return _unknown(view, "DERIVED_BASIS_INCOMPATIBLE", chart_provenance, tested=tested_points)
        live_extra = {"derived_basis": "TESTED_BASELINE_HIGH_WATER", "derived_from": "TESTED"} | ({"unit": "%", "units": "%", "denominator": "equity_high_water", "denominator_basis": "running_high_water"} if drawdown_percent else {})
    else:
        live_extra = None
    live_provenance = chart_provenance if canonical_view in {"im_load", "mm_load", "margin_load"} else {**chart_provenance, **live_defaults}
    live_points = _build_value_points(live_values, LIVE, live_provenance, start=tested_elapsed_origin, trim=canonical_view in {"im_load", "mm_load", "margin_load"}, extra=live_extra)
    if not overlay:
        return ChartResult(CHART_MODEL_VERSION, view, AVAILABLE, TESTED, tested_points, None, chart_provenance, tested_points, live_points)
    if not _overlay_metadata_compatible(tested_basis_values, live_overlay_values, manifest_map):
        return _unknown(view, "OVERLAY_INCOMPATIBLE", chart_provenance, tested=tested_points, live=live_points)
    combined_points = tuple(sorted((*tested_points, *live_points), key=lambda item: (_timestamp(item.timestamp_utc), 0 if item.source == TESTED else 1)))
    combined_points = tuple(
        ChartPoint(item.timestamp_utc, _elapsed_text(_timestamp(item.timestamp_utc), tested_elapsed_origin), item.value, item.source, item.availability, item.provenance, item.reason)
        for item in combined_points
    )
    return ChartResult(CHART_MODEL_VERSION, view, AVAILABLE, "OVERLAY", combined_points, None, chart_provenance, tested_points, live_points)


build_chart = build_chart_model
read_chart = build_chart_model


class LiveChartModel:
    """Small convenience wrapper retaining the same read-only protocol."""

    def __init__(self, reader: Any, manifest: Any):
        self.reader = reader
        self.manifest = manifest

    def build(self, *, view: str = "equity", live: Any = None, overlay: bool = False, **kwargs: Any) -> ChartResult:
        return build_chart_model(self.reader, self.manifest, view=view, live=live, overlay=overlay, **kwargs)


# Phase 2B exact read model.  The provisional API above is retained for the
# already accepted tested-only callers; this section is the canonical bounded
# server model used by new readers.
CHART_OVERVIEW_VERSION = "portfolio_chart_overview_v1"
ELAPSED_BUCKET_VERSION = "elapsed_bucket_v1"
PORTFOLIO = "PORTFOLIO"
PAIR = "PAIR"
UNKNOWN_REASON = "UNKNOWN"
MAX_RECORDS = 256
MAX_TRACES = 32
MAX_PAIR_OPTIONS = 128
MAX_RAW_FACTS = 1_000_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MICROSECONDS = Decimal("1000000")
_URI_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_BUCKET_LADDER_US = (1_000_000, 5_000_000, 15_000_000, 30_000_000,
                     60_000_000, 300_000_000, 900_000_000, 1_800_000_000,
                     3_600_000_000, 14_400_000_000, 21_600_000_000,
                     43_200_000_000, 86_400_000_000, 604_800_000_000,
                     2_592_000_000_000, 7_776_000_000_000,
                     31_536_000_000_000)


class ResourceBoundExceeded(ValueError):
    """The bounded chart contract cannot be satisfied without truncation."""

    def __init__(self, reason: str = "RESOURCE_BOUND_EXCEEDED") -> None:
        super().__init__(reason)
        self.reason = reason


class ChartFragmentLimitExceeded(ResourceBoundExceeded):
    def __init__(self) -> None:
        super().__init__("CHART_FRAGMENT_LIMIT_EXCEEDED")


def _canonical_decimal(value: Any) -> str:
    number = _decimal(value)
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        number = +number
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _contains_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, ExactChartPoint):  # type: ignore[name-defined]
        return _contains_float(value.value) or _contains_float(value.provenance)
    if isinstance(value, Mapping):
        return any(_contains_float(k) or _contains_float(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_float(item) for item in value)
    return False


def _timestamp_us_exact(value: Any) -> int:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("chart timestamp must be timezone-aware")
        parsed = parsed.astimezone(timezone.utc)
        return (parsed.date() - datetime(1970, 1, 1, tzinfo=timezone.utc).date()).days * 86_400_000_000 + parsed.hour * 3_600_000_000 + parsed.minute * 60_000_000 + parsed.second * 1_000_000 + parsed.microsecond
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        number = Decimal(value)
        if abs(number) >= Decimal("100000000000000"):
            return int(number)
        if abs(number) >= Decimal("10000000000"):
            number /= Decimal("1000")
        with localcontext() as context:
            context.prec = 50
            return int(number * MICROSECONDS)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return _timestamp_us_exact(parsed)
    raise ValueError("chart timestamp is invalid")


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, float):
        raise TypeError("float is forbidden in chart serialization")
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if hasattr(value, "as_dict"):
        return _canonical_json_value(value.as_dict())
    raise TypeError(f"unsupported chart value: {type(value).__name__}")


def canonical_chart_bytes(value: Any) -> bytes:
    return json.dumps(_canonical_json_value(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def canonical_chart_digest(value: Any) -> str:
    return sha256(canonical_chart_bytes(value)).hexdigest()


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _point_value(raw: Any) -> Any:
    if isinstance(raw, Mapping):
        return _first(raw, "value", "numeric_value", "amount", "equity", "pnl")
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        return raw[1]
    return None


def _point_time(raw: Any) -> Any:
    if isinstance(raw, Mapping):
        return _first(raw, "timestamp_us", "timestamp_utc", "timestamp", "effective_at_utc", "effective_at", "observed_at_utc", "observed_at", "time")
    if isinstance(raw, (tuple, list)) and raw:
        return raw[0]
    return None


def _point_identity(raw: Any, index: int) -> str:
    if isinstance(raw, Mapping):
        value = _first(raw, "immutable_identity", "identity", "source_id", "event_id", "point_id", "id")
        if value is not None:
            return str(value)
    if isinstance(raw, (tuple, list)) and len(raw) > 2:
        return str(raw[2])
    return str(index)


def _source_ordinal(raw: Any, index: int) -> int:
    value = raw.get("source_ordinal", raw.get("ordinal", index)) if isinstance(raw, Mapping) else index
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("source ordinal is invalid")
    return value


def _iter_points(raw: Any) -> tuple[Any, ...]:
    if isinstance(raw, Mapping):
        if "points" in raw:
            raw = raw["points"]
        elif "values" in raw:
            raw = raw["values"]
        elif _point_time(raw) is not None:
            return (raw,)
        else:
            return tuple({"timestamp_utc": key, "value": value, "source_ordinal": index} for index, (key, value) in enumerate(raw.items()))
    if raw is None:
        return ()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(raw)
    return ()


def _raw_fact_count(raw: Any) -> int:
    if raw is None:
        return 0
    if isinstance(raw, Mapping) and isinstance(raw.get("points"), Sequence):
        return len(raw["points"])
    if isinstance(raw, Mapping) and isinstance(raw.get("values"), Sequence):
        return len(raw["values"])
    if isinstance(raw, Mapping) and any(key in raw for key in ("equity", "wallet_equity", "balance", "value", "amount", "realized_pnl", "unrealized_pnl", "initial_margin", "maintenance_margin")):
        return 1
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return len(raw)
    return 1 if _point_time(raw) is not None else 0


def _source_identity(raw: Any, path: str | None = None) -> tuple[str, str]:
    """Return a stable identity for one selected immutable source.

    Source aliases (``equity``/``wallet`` and ``im``/``initial_margin``) are
    deliberately collapsed by the caller's canonical path.  Explicit source
    identities win, so two genuinely distinct selected sources with the same
    metric name cannot be silently merged.
    """
    mapping = raw if isinstance(raw, Mapping) else None
    explicit = _first(mapping, "immutable_identity", "source_id", "source_path", "path", "series_id") if mapping is not None else None
    if explicit is not None:
        return ("identity", str(explicit))
    if path is not None:
        aliases = {
            "wallet": "equity", "walletequity": "equity", "balance": "equity",
            "im": "initialmargin", "initialmargin": "initialmargin",
            "mm": "maintenancemargin", "maintenancemargin": "maintenancemargin",
        }
        canonical_path = _name(path.rsplit(".", 1)[-1])
        aliases.update({"nettradingpnl": "netpnl"})
        return ("path", aliases.get(canonical_path, canonical_path))
    return ("object", str(id(raw)))


def _full_provenance(manifest: Mapping[str, Any], run: Mapping[str, Any], point: Mapping[str, Any], *, source: str, availability: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    aliases = {
        "portfolio_set": ("portfolio_set", "set_id"),
        "evaluation": ("evaluation", "evaluation_id"),
        "run_id": ("run_id",),
        "attempt_id": ("attempt_id",),
        "source_digest": ("source_digest", "source_sha256", "source_hash", "payload_digest", "digest"),
        "semantic_digest": ("semantic_digest",),
        "manifest_id": ("manifest_id",),
        "manifest_digest": ("manifest_digest",),
        "manifest_version": ("manifest_version", "version"),
        "series_version": ("series_version",),
        "metrics_version": ("metrics_version",),
        "composition": ("member_composition", "composition"),
        "scope": ("scope", "scope_id"),
        "attribution_basis": ("attribution_basis",),
        "unit": ("unit", "units"),
        "currency": ("currency",),
        "time_basis": ("time_basis",),
        "applicability": ("applicability",),
        "denominator": ("denominator",),
    }
    for output, keys in aliases.items():
        value = _first(point, *keys, default=_first(run, *keys, default=_first(manifest, *keys)))
        if value is not None:
            result[output] = value
    result.update({"source": source, "availability": availability})
    if "composition" in result and isinstance(result["composition"], (list, tuple)):
        result["composition"] = [_canonical_json_value(item) for item in result["composition"]]
    nested = point.get("provenance") if isinstance(point.get("provenance"), Mapping) else {}
    for key, value in nested.items():
        if key not in {"source", "provenance"}:
            result.setdefault(str(key), value)
    return result


@dataclass(frozen=True, slots=True)
class ExactChartPoint:
    timestamp_us: int
    value: str | None
    source: str
    source_ordinal: int
    immutable_identity: str
    availability: str = AVAILABLE
    provenance: Mapping[str, Any] = None  # type: ignore[assignment]
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.provenance is None:
            object.__setattr__(self, "provenance", {})

    def as_dict(self) -> dict[str, Any]:
        return {"timestamp_us": self.timestamp_us, "value": self.value, "source": self.source,
                "source_ordinal": self.source_ordinal, "immutable_identity": self.immutable_identity,
                "availability": self.availability, "provenance": dict(self.provenance), "reason": self.reason}


def build_exact_points(raw: Any, *, manifest: Mapping[str, Any] | None = None,
                       run: Mapping[str, Any] | None = None, source: str = TESTED,
                       provenance: Mapping[str, Any] | None = None) -> tuple[ExactChartPoint, ...]:
    """Normalize an immutable series without applying any aggregation."""
    if _contains_float(raw) or _contains_float(manifest) or _contains_float(run) or _contains_float(provenance):
        raise TypeError("float is forbidden in chart facts")
    manifest = manifest or {}
    run = run or {}
    rows: list[ExactChartPoint] = []
    for index, item in enumerate(_iter_points(raw)):
        mapping = item if isinstance(item, Mapping) else {}
        timestamp = _point_time(item)
        if timestamp is None:
            raise ValueError("chart point timestamp is missing")
        timestamp_value = _timestamp_us_exact(timestamp)
        identity = _point_identity(item, index)
        ordinal = _source_ordinal(item, index)
        observed_raw = _first(mapping, "observed_at_us", "observed_at_utc", "observed_at", default=timestamp)
        observed_us = _timestamp_us_exact(observed_raw)
        source_kind = str(_first(mapping, "source_kind", "source", default=source))
        source_id = str(_first(mapping, "source_id", "event_id", "point_id", "id", default=identity))
        source_sequence = _first(mapping, "source_sequence", "sequence", default=ordinal)
        if isinstance(source_sequence, bool) or not isinstance(source_sequence, int):
            raise ValueError("source sequence is invalid")
        payload_digest = str(_first(mapping, "canonical_payload_digest", "payload_digest", "digest", default=""))
        availability = str(_first(mapping, "availability", "status", default=AVAILABLE)).upper() if mapping else AVAILABLE
        if availability not in {AVAILABLE, "KNOWN", "TRUE"}:
            rows.append(ExactChartPoint(timestamp_value, None, source, ordinal, identity, UNKNOWN,
                                        {**_full_provenance(manifest, run, mapping, source=source, availability=UNKNOWN), "observed_at_us": observed_us, "source_kind": source_kind, "source_id": source_id, "source_sequence": source_sequence, "canonical_payload_digest": payload_digest},
                                        str(_first(mapping, "reason", default="UNAVAILABLE"))))
            continue
        value = _point_value(item)
        if value is None:
            rows.append(ExactChartPoint(timestamp_value, None, source, ordinal, identity, UNKNOWN,
                                        {**_full_provenance(manifest, run, mapping, source=source, availability=UNKNOWN), "observed_at_us": observed_us, "source_kind": source_kind, "source_id": source_id, "source_sequence": source_sequence, "canonical_payload_digest": payload_digest}, "VALUE_UNAVAILABLE"))
            continue
        decimal = _canonical_decimal(value)
        point_provenance = {**_full_provenance(manifest, run, mapping, source=source, availability=AVAILABLE),
                            "observed_at_us": observed_us, "source_kind": source_kind,
                            "source_id": source_id, "source_sequence": source_sequence,
                            "canonical_payload_digest": payload_digest, **dict(provenance or {})}
        if mapping.get("baseline_complete") is True or mapping.get("complete_baseline") is True:
            point_provenance["baseline_complete"] = True
        rows.append(ExactChartPoint(timestamp_value, decimal, source, ordinal, identity, AVAILABLE, point_provenance))
    rows.sort(key=lambda item: (item.timestamp_us, int(item.provenance.get("observed_at_us", item.timestamp_us)), str(item.provenance.get("source_kind", "")), str(item.provenance.get("source_id", item.immutable_identity)), int(item.provenance.get("source_sequence", item.source_ordinal)), str(item.provenance.get("canonical_payload_digest", "")), item.source_ordinal, item.immutable_identity))
    identities = [(item.timestamp_us, item.source_ordinal, item.immutable_identity) for item in rows]
    if len(set(identities)) != len(rows):
        raise ValueError("duplicate chart point identity")
    seen_payloads: dict[tuple[int, str], tuple[str | None, str]] = {}
    for item in rows:
        key = (item.timestamp_us, item.immutable_identity)
        payload = (item.value, str(item.provenance.get("canonical_payload_digest", "")))
        previous = seen_payloads.get(key)
        if previous is not None and previous != payload:
            raise ValueError("conflicting chart point payload")
        seen_payloads[key] = payload
    return tuple(rows)


def _series_map_for(run: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("series", "portfolio_series", "chart_series", "traces"):
        value = run.get(key)
        if isinstance(value, Mapping):
            return value
    return run


def _series_for(series: Mapping[str, Any], name: str) -> Any:
    wanted = _name(name)
    for key, value in series.items():
        if _name(str(key)) == wanted:
            return value
    return None


def _run_mapping(reader: Any, manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if isinstance(reader, Mapping):
        return reader
    run_id = str(manifest.get("run_id", ""))
    attempt_id = str(manifest.get("attempt_id", ""))
    method = getattr(reader, "read_portfolio_run", None)
    if not callable(method) and callable(reader):
        method = reader
    if not callable(method):
        return None
    value = method(run_id, attempt_id)
    return _mapping(value)


def _pinned_run(reader: Any, manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    run = _run_mapping(reader, manifest)
    if run is None:
        return None
    for key in ("run_id", "attempt_id", "semantic_digest", "series_version", "metrics_version", "portfolio_set", "evaluation"):
        expected = manifest.get(key)
        if expected is not None and str(_first(run, key, default=None)) != str(expected):
            return None
    expected_comp = manifest.get("member_composition")
    if expected_comp is not None and _actual_composition(run) is None:
        return None
    if expected_comp is not None and not _composition_equal(expected_comp, _actual_composition(run)):
        return None
    return run


def _point_series(run: Mapping[str, Any], name: str, *, mode: str = PORTFOLIO, pair: tuple[str, str] | None = None) -> Any:
    series = _series_map_for(run)
    if mode == PAIR:
        candidates = ("pair_series", "pairs", "member_series", "attributed_series")
        for root in candidates:
            collection = run.get(root)
            if isinstance(collection, Mapping):
                key_options = (f"{pair[0]}|{pair[1]}", f"{pair[0]}:{pair[1]}", f"{pair[0]}_{pair[1]}", f"{pair[0]}/{pair[1]}") if pair else ()
                member = next((collection.get(key) for key in key_options if key in collection), None)
                if member is None and pair:
                    member = next((value for key, value in collection.items() if isinstance(value, Mapping) and str(value.get("symbol", "")).upper() == pair[0] and str(value.get("side", "")).upper() == pair[1]), None)
                if isinstance(member, Mapping):
                    direct = _series_for(member, name)
                    if direct is not None:
                        return direct
            elif isinstance(collection, Sequence) and not isinstance(collection, (str, bytes, bytearray)) and pair:
                for member in collection:
                    if isinstance(member, Mapping) and str(member.get("symbol", "")).upper() == pair[0] and str(member.get("side", member.get("direction", ""))).upper() == pair[1]:
                        direct = _series_for(member, name)
                        if direct is not None:
                            return direct
        member = next((item for item in run.get("members", ()) if isinstance(item, Mapping) and str(item.get("symbol", "")).upper() == pair[0] and str(item.get("side", "")).upper() == pair[1]), None) if pair else None
        if isinstance(member, Mapping) and _series_for(member, name) is not None:
            return _series_for(member, name)
        return None
    return _series_for(series, name)


def _pair_known(manifest: Mapping[str, Any], pair: tuple[str, str]) -> bool:
    for item in manifest.get("member_composition", manifest.get("composition", ())):
        if isinstance(item, Mapping) and str(item.get("symbol", "")).upper() == pair[0] and str(item.get("side", item.get("direction", ""))).upper() == pair[1]:
            return True
    return False


def _pair_attribution_basis(manifest: Mapping[str, Any], run: Mapping[str, Any], pair: tuple[str, str]) -> str | None:
    composition = manifest.get("member_composition", manifest.get("composition", ()))
    members = [item for item in composition
               if isinstance(item, Mapping) and str(item.get("symbol", "")).upper() == pair[0]
                and str(item.get("side", item.get("direction", ""))).upper() == pair[1]]
    same_symbol = [item for item in composition
                   if isinstance(item, Mapping) and str(item.get("symbol", "")).upper() == pair[0]]
    # A symbol shared by any side/strategy is ambiguous even when this exact
    # side currently has one member.  It needs an immutable local mapping or
    # explicit orderLinkId evidence.
    if len(members) == 1 and len(same_symbol) == 1:
        member = members[0]
        if any(member.get(key) is not None for key in ("order_link_id", "orderLinkId", "local_mapping_id", "mapping_digest")):
            return "immutable_local_mapping"
        return "unique_pinned_member"
    if len(members) == 1 and any(members[0].get(key) is not None for key in ("order_link_id", "orderLinkId", "local_mapping_id", "mapping_digest")):
        return "immutable_local_mapping"
    if not members:
        return None
    pair_key = f"{pair[0]}|{pair[1]}"
    for root_name in ("pair_attribution", "attribution", "local_pair_map", "pair_map"):
        root = run.get(root_name)
        value = root.get(pair_key) if isinstance(root, Mapping) else None
        if value is None and isinstance(root, Mapping):
            value = root.get(f"{pair[0]}:{pair[1]}")
        if isinstance(value, Mapping):
            strategy = value.get("strategy_id", value.get("member_id"))
            proven = any(value.get(key) is not None for key in ("order_link_id", "orderLinkId", "local_mapping_id", "mapping_digest", "immutable_identity"))
            if strategy is not None and proven and sum(1 for item in members if str(item.get("strategy_id")) == str(strategy)) == 1:
                return "order_link_or_immutable_mapping"
    return None


def _cashflow_signed(item: Mapping[str, Any]) -> tuple[Decimal | None, str | None]:
    classification = _first(item, "classification", "type", "kind")
    if classification is None:
        # Retained monitor facts may carry the explicit cashflow class in
        # ``direction``.  A direction-only row is still classified; arbitrary
        # or missing directions remain fail-closed below.
        classification = _first(item, "direction")
    if classification is None:
        return None, None
    amount = _first(item, "amount", "value", "cashflow")
    if amount is None:
        return None, str(classification)
    try:
        number = _decimal(amount)
    except ValueError:
        return None, str(classification)
    kind = str(classification).strip().upper().replace("-", "_").replace(" ", "_")
    direction = str(_first(item, "direction", default="")).strip().upper().replace("-", "_").replace(" ", "_")
    inbound = {"IN", "INBOUND", "DEPOSIT", "DEPOSITED", "CREDIT", "ACCOUNT_INFLOW"}
    outbound = {"OUT", "OUTBOUND", "WITHDRAWAL", "WITHDRAW", "DEBIT", "ACCOUNT_OUTFLOW"}
    if kind in {"DEPOSIT", "DEPOSITED", "INBOUND", "ACCOUNT_INFLOW"} and (not direction or direction in inbound):
        return abs(number), str(classification)
    if kind in {"WITHDRAWAL", "WITHDRAW", "OUTBOUND", "ACCOUNT_OUTFLOW"} and (not direction or direction in outbound):
        number = -abs(number)
        return number, str(classification)
    if kind in {"INTERNAL_TRANSFER", "TRANSFER_INTERNAL", "INTERNAL"} and (
        item.get("internal") is True or str(item.get("scope", "")).upper() == "INTERNAL"
    ) and item.get("boundary") is False:
        return Decimal("0"), str(classification)
    return None, str(classification)


def _cashflow_rows(cashflows: Any, *, currency: Any) -> tuple[list[tuple[int | None, int, str, Decimal | None, str | None, tuple[Any, ...]]], list[dict[str, Any]], str | None]:
    rows: list[tuple[int | None, int, str, Decimal | None, str | None, tuple[Any, ...]]] = []
    audit: list[dict[str, Any]] = []
    reason: str | None = None
    for index, raw in enumerate(_iter_points(cashflows)):
        if not isinstance(raw, Mapping):
            reason = reason or "CASHFLOW_UNKNOWN"
            continue
        identity_value = _first(raw, "immutable_identity", "identity", "source_id", "cashflow_id", "event_id", "id")
        identity = str(identity_value) if identity_value is not None else canonical_chart_digest(raw)
        classification = _first(raw, "classification", "type", "kind")
        signed, _ = _cashflow_signed(raw)
        item_reason: str | None = None
        try:
            when = _timestamp_us_exact(_point_time(raw))
        except (TypeError, ValueError):
            when = None
            item_reason = "CASHFLOW_ORDER_UNKNOWN"
        item_currency = _first(raw, "currency", "asset", default=currency)
        if currency is not None and item_currency is not None and str(item_currency) != str(currency):
            item_reason = item_reason or "CASHFLOW_CURRENCY_MISMATCH"
        if signed is None:
            item_reason = item_reason or "CASHFLOW_UNKNOWN"
        source_ordinal = raw.get("source_ordinal", raw.get("ordinal", index))
        if isinstance(source_ordinal, bool) or not isinstance(source_ordinal, int):
            item_reason = item_reason or "CASHFLOW_ORDER_UNKNOWN"
            source_ordinal = index
        source_sequence = raw.get("source_sequence", raw.get("sequence", source_ordinal if isinstance(source_ordinal, int) else 0))
        if isinstance(source_sequence, bool) or not isinstance(source_sequence, int) or source_sequence < 0:
            item_reason = item_reason or "CASHFLOW_ORDER_UNKNOWN"
            source_sequence = 0
        effective_key = when if when is not None else 0
        observed_raw = _first(raw, "observed_at_utc", "observed_at", "observed_timestamp_utc", default=_point_time(raw))
        try:
            observed_key = _timestamp_us_exact(observed_raw) if observed_raw is not None else None
        except (TypeError, ValueError):
            observed_key = None
            item_reason = item_reason or "CASHFLOW_ORDER_UNKNOWN"
        source_kind = str(_first(raw, "source_kind", "source", default="CASHFLOW"))
        payload_digest = str(_first(raw, "canonical_payload_digest", "payload_digest", "digest", default=canonical_chart_digest(raw)))
        if observed_key is None:
            item_reason = item_reason or "CASHFLOW_ORDER_UNKNOWN"
        reason = reason or item_reason
        event = {"timestamp_us": when, "source_id": identity, "classification": classification,
                 "direction": _first(raw, "direction"), "amount": _first(raw, "amount", "value", "cashflow"),
                 "signed_amount": _canonical_decimal(signed) if signed is not None else None,
                 "currency": item_currency, "source_kind": source_kind, "source_sequence": source_sequence,
                 "observed_at_us": observed_key, "canonical_payload_digest": payload_digest,
                 "availability": AVAILABLE if item_reason is None else UNKNOWN,
                 "reason": item_reason}
        audit.append(event)
        order_key = (effective_key, observed_key if observed_key is not None else 0, source_kind, identity, source_sequence, payload_digest)
        rows.append((when, source_ordinal, identity, signed, item_reason, order_key))
    rows.sort(key=lambda row: (row[0] is None, row[5]))
    audit.sort(key=lambda event: (
        event.get("timestamp_us") is None,
        event.get("timestamp_us") if event.get("timestamp_us") is not None else 0,
        event.get("observed_at_us") if event.get("observed_at_us") is not None else 0,
        str(event.get("source_kind", "")), str(event.get("source_id", "")),
        int(event.get("source_sequence", 0)), str(event.get("canonical_payload_digest", "")),
    ))
    return rows, audit, reason


def _cashflow_adjusted(equity: tuple[ExactChartPoint, ...], cashflows: Any, *, currency: Any) -> tuple[ExactChartPoint, ...]:
    if not equity:
        return ()
    flows, _audit, _flow_reason = _cashflow_rows(cashflows, currency=currency)
    if any(row[0] is None for row in flows):
        return tuple(ExactChartPoint(p.timestamp_us, None, p.source, p.source_ordinal, p.immutable_identity, UNKNOWN, p.provenance, "CASHFLOW_ORDER_UNKNOWN") for p in equity)
    start = next((p.timestamp_us for p in equity if p.value is not None), equity[0].timestamp_us)
    total = Decimal(0)
    flow_index = 0
    invalid = False
    equity_blocked = False
    result: list[ExactChartPoint] = []
    for point in equity:
        marker = point.provenance.get("baseline_complete") is True
        if marker:
            start = point.timestamp_us
            total = Decimal(0)
            invalid = False
            equity_blocked = False
        while flow_index < len(flows) and flows[flow_index][0] is not None and flows[flow_index][0] <= point.timestamp_us:
            when, _ordinal, _identity, amount, row_reason, _order_key = flows[flow_index]
            if when > start:
                if row_reason is not None or amount is None:
                    invalid = True
                elif not invalid:
                    with localcontext() as context:
                        context.prec = 34
                        context.rounding = ROUND_HALF_EVEN
                        total += amount
            flow_index += 1
        if point.value is None:
            equity_blocked = True
        if point.timestamp_us < start or point.value is None or invalid or equity_blocked:
            result.append(ExactChartPoint(point.timestamp_us, None, point.source, point.source_ordinal, point.immutable_identity, UNKNOWN, point.provenance, "CASHFLOW_UNKNOWN" if invalid else "EQUITY_GAP"))
        else:
            with localcontext() as context:
                context.prec = 34
                context.rounding = ROUND_HALF_EVEN
                adjusted = _canonical_decimal(_decimal(point.value) - total)
            result.append(ExactChartPoint(point.timestamp_us, adjusted, point.source, point.source_ordinal, point.immutable_identity, point.availability, point.provenance, point.reason))
    return tuple(result)


def _cashflow_audit(cashflows: Any, *, currency: Any) -> dict[str, Any]:
    _rows, events, reason = _cashflow_rows(cashflows, currency=currency)
    signed = [Decimal(event["signed_amount"]) for event in events if event.get("signed_amount") is not None]
    if reason is None:
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            total = _canonical_decimal(sum(signed, Decimal(0)))
    else:
        total = None
    return {"availability": UNKNOWN if reason is not None else AVAILABLE, "reason": reason,
            "count": len(events), "net_sum": total, "events": events}


def _drawdown_points(equity: tuple[ExactChartPoint, ...], *, percent: bool = False) -> tuple[ExactChartPoint, ...]:
    high: Decimal | None = None
    blocked = False
    result: list[ExactChartPoint] = []
    for point in equity:
        if point.provenance.get("baseline_complete") is True:
            high = None
            blocked = False
        if point.value is None:
            blocked = True
            result.append(ExactChartPoint(point.timestamp_us, None, point.source, point.source_ordinal, point.immutable_identity, UNKNOWN, point.provenance, "EQUITY_UNKNOWN"))
            continue
        if blocked:
            result.append(ExactChartPoint(point.timestamp_us, None, point.source, point.source_ordinal, point.immutable_identity, UNKNOWN, point.provenance, "EQUITY_GAP"))
            continue
        value = _decimal(point.value)
        high = value if high is None else max(high, value)
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            path = value - high
        if percent and high <= 0:
            result.append(ExactChartPoint(point.timestamp_us, None, point.source, point.source_ordinal, point.immutable_identity, UNKNOWN, {**point.provenance, "unit": "%", "denominator": "equity_high_water"}, "NONPOSITIVE_HIGH_WATER"))
        else:
            with localcontext() as context:
                context.prec = 34
                context.rounding = ROUND_HALF_EVEN
                result.append(ExactChartPoint(point.timestamp_us, _canonical_decimal(path / high * Decimal("100") if percent else path), point.source, point.source_ordinal, point.immutable_identity, AVAILABLE, {**point.provenance, **({"unit": "%", "denominator": "equity_high_water"} if percent else {})}))
    return tuple(result)


def _margin_points(series: Mapping[str, Any], *, manifest: Mapping[str, Any], run: Mapping[str, Any], source: str) -> tuple[ExactChartPoint, ...]:
    im_raw = _series_for(series, "initial_margin") or _series_for(series, "im")
    mm_raw = _series_for(series, "maintenance_margin") or _series_for(series, "mm")
    denominator_raw = _series_for(series, "margin_balance") or _series_for(series, "equity")
    if im_raw is None or mm_raw is None or denominator_raw is None:
        return ()
    im = build_exact_points(im_raw, manifest=manifest, run=run, source=source, provenance={"metric": "IM"})
    mm = build_exact_points(mm_raw, manifest=manifest, run=run, source=source, provenance={"metric": "MM"})
    denominator = build_exact_points(denominator_raw, manifest=manifest, run=run, source=source, provenance={"metric": "DENOMINATOR"})
    all_times = sorted({p.timestamp_us for p in (*im, *mm, *denominator)})
    result: list[ExactChartPoint] = []
    freshness = _first(manifest, "margin_freshness_seconds", "freshness_seconds")
    invalid = {"IM": False, "MM": False, "DENOMINATOR": False}

    def conflict(points: tuple[ExactChartPoint, ...], when: int) -> bool:
        values = {
            (_canonical_decimal(p.value), p.provenance.get("unit"), p.provenance.get("currency"), p.provenance.get("source_kind"))
            for p in points if p.timestamp_us == when and p.value is not None
        }
        return len(values) > 1

    for when in all_times:
        im_prior = [p for p in im if p.timestamp_us <= when]
        mm_prior = [p for p in mm if p.timestamp_us <= when]
        den_prior = [p for p in denominator if p.timestamp_us <= when]
        for metric, prior in (("IM", im_prior), ("MM", mm_prior)):
            current = prior[-1] if prior else None
            den = den_prior[-1] if den_prior else None
            metric_points = im if metric == "IM" else mm
            if conflict(metric_points, when):
                invalid[metric] = True
            elif any(p.timestamp_us == when for p in metric_points):
                invalid[metric] = False
            if conflict(denominator, when):
                invalid["DENOMINATOR"] = True
            elif any(p.timestamp_us == when for p in denominator):
                invalid["DENOMINATOR"] = False
            reason = "MARGIN_FRESHNESS_UNKNOWN" if freshness is None else "MARGIN_UNAVAILABLE"
            if current is None or den is None or current.value is None or den.value is None or _decimal(den.value) <= 0 or invalid[metric] or invalid["DENOMINATOR"]:
                result.append(ExactChartPoint(when, None, source, len(result), f"{metric}:{when}", UNKNOWN, {"metric": metric, "unit": "%", "denominator": "margin_balance" if _series_for(series, "margin_balance") is not None else "equity"}, reason))
                continue
            with localcontext() as context:
                context.prec = 34
                context.rounding = ROUND_HALF_EVEN
                current_age = Decimal(when - current.timestamp_us) / MICROSECONDS
                denominator_age = Decimal(when - den.timestamp_us) / MICROSECONDS
            if freshness is None or current_age > _decimal(freshness) or denominator_age > _decimal(freshness):
                result.append(ExactChartPoint(when, None, source, len(result), f"{metric}:{when}", UNKNOWN, {"metric": metric, "unit": "%"}, "MARGIN_FRESHNESS_UNKNOWN" if freshness is None else "MARGIN_STALE"))
                continue
            if any(current.provenance.get(key) is not None and den.provenance.get(key) is not None and str(current.provenance[key]) != str(den.provenance[key]) for key in ("currency", "time_basis", "applicability")):
                result.append(ExactChartPoint(when, None, source, len(result), f"{metric}:{when}", UNKNOWN, {"metric": metric, "unit": "%"}, "MARGIN_INCOMPATIBLE"))
                continue
            with localcontext() as context:
                context.prec = 34
                context.rounding = ROUND_HALF_EVEN
                ratio = _decimal(current.value) / _decimal(den.value) * Decimal("100")
            with localcontext() as context:
                context.prec = 34
                context.rounding = ROUND_HALF_EVEN
                age = Decimal(when - den.timestamp_us) / MICROSECONDS
            result.append(ExactChartPoint(when, _canonical_decimal(ratio), source, len(result), f"{metric}:{when}", AVAILABLE, {**current.provenance, "metric": metric, "unit": "%", "denominator": "margin_balance" if _series_for(series, "margin_balance") is not None else "equity", "denominator_value": _canonical_decimal(den.value), "numerator_value": _canonical_decimal(current.value), "denominator_source_id": den.immutable_identity, "numerator_source_id": current.immutable_identity, "freshness_seconds": _canonical_decimal(age)}))
    return tuple(result)


def _provenance_signature(point: ExactChartPoint) -> str:
    # A point's observation identity is membership data, not a semantic
    # segment boundary.  In particular, observed_at/source_id/sequence and
    # payload digests must not turn every normalized fact into a fragment.
    provenance = point.provenance
    semantic = {
        "source": point.source,
        "source_kind": provenance.get("source_kind", point.source),
        "run_id": provenance.get("run_id"),
        "attempt_id": provenance.get("attempt_id"),
        "manifest_id": provenance.get("manifest_id"),
        "manifest_digest": provenance.get("manifest_digest"),
        "manifest_version": provenance.get("manifest_version"),
        "semantic_digest": provenance.get("semantic_digest"),
        "series_version": provenance.get("series_version"),
        "metrics_version": provenance.get("metrics_version"),
        "composition": provenance.get("composition"),
        "scope": provenance.get("scope"),
        "attribution_basis": provenance.get("attribution_basis"),
        "metric": provenance.get("metric"),
        "unit": provenance.get("unit"),
        "currency": provenance.get("currency"),
        "time_basis": provenance.get("time_basis"),
        "applicability": provenance.get("applicability"),
        "denominator": provenance.get("denominator"),
        "availability": point.availability,
    }
    return canonical_chart_digest(semantic)


def _record_value(value: Decimal | None) -> str | None:
    return None if value is None else _canonical_decimal(value)


def _elapsed_decimal(timestamp_us: int, start_us: int) -> str:
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        return _canonical_decimal(Decimal(timestamp_us - start_us) / MICROSECONDS)


def _datetime_from_us(timestamp_us: int) -> datetime:
    seconds, micros = divmod(timestamp_us, 1_000_000)
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds, microseconds=micros)


def _observation(point: ExactChartPoint | None, *, start_us: int) -> dict[str, Any] | None:
    if point is None:
        return None
    return {"value": point.value, "timestamp_utc": _timestamp_text(_datetime_from_us(point.timestamp_us)),
            "timestamp_us": point.timestamp_us, "elapsed_from_start": _elapsed_decimal(point.timestamp_us, start_us),
            "identity": point.immutable_identity}


def _ladder_width(span_us: int) -> int:
    for width in _BUCKET_LADDER_US:
        if (span_us + width - 1) // width <= MAX_RECORDS:
            return width
    raise ResourceBoundExceeded("RESOURCE_BOUND_EXCEEDED")


def _aggregate_once(points: tuple[ExactChartPoint, ...], width_us: int, start_us: int, *, strict_unknown: bool = False,
                    inclusive_final: bool = False) -> tuple[dict[str, Any], ...]:
    if not points:
        return ()
    grouped: list[tuple[int, list[ExactChartPoint]]] = []
    for point_index, point in enumerate(points):
        offset = point.timestamp_us - start_us
        if offset == MAX_RECORDS * width_us and inclusive_final and all(item.timestamp_us == point.timestamp_us for item in points[point_index:]):
            index = MAX_RECORDS - 1
        elif offset < 0 or offset // width_us >= MAX_RECORDS:
            raise ValueError("point lies outside elapsed bucket grid")
        else:
            index = offset // width_us
        if grouped and grouped[-1][0] == index and (_provenance_signature(grouped[-1][1][-1]) == _provenance_signature(point) and grouped[-1][1][-1].availability == point.availability):
            bucket = grouped[-1][1]
        else:
            grouped.append((index, []))
            bucket = grouped[-1][1]
        bucket.append(point)
    records: list[dict[str, Any]] = []
    previous_index: int | None = None
    previous_signature: tuple[str, str] | None = None
    segment_index = -1
    for index, bucket in grouped:
        gap_before = previous_index is not None and index > previous_index + 1
        if gap_before:
            # Preserve the missing elapsed interval as one bounded explicit
            # record.  It carries no fabricated value and therefore cannot be
            # mistaken for interpolation; consecutive empty cells coalesce.
            segment_index += 1
            gap_start = start_us + (previous_index + 1) * width_us
            gap_end = start_us + index * width_us
            records.append({
                "index": previous_index + 1, "bucket_index": previous_index + 1,
                "segment": segment_index, "segment_index": segment_index,
                "grid_start_us": gap_start, "grid_end_us": gap_end,
                "grid_start_elapsed": _elapsed_decimal(gap_start, start_us),
                "grid_end_elapsed": _elapsed_decimal(gap_end, start_us),
                "display_midpoint_us": (gap_start + gap_end) // 2,
                "display_midpoint_elapsed": _elapsed_decimal((gap_start + gap_end) // 2, start_us),
                "first": None, "last": None, "min": None, "max": None, "mean": None,
                "count": 0, "known_count": 0, "availability": UNKNOWN,
                "first_timestamp_us": None, "last_timestamp_us": None,
                "first_source_id": None, "last_source_id": None,
                "first_exact": None, "last_exact": None, "exact_min": None, "exact_max": None,
                "min_exact": None, "max_exact": None,
                "provenance_membership_digest": canonical_chart_digest([]),
                "provenance": {}, "gap_before": True, "gap_start_us": gap_start,
                "gap_end_us": gap_end, "gap": True, "reason": "GAP",
            })
        known = [p for p in bucket if p.value is not None and p.availability == AVAILABLE]
        values = [_decimal(p.value) for p in known]
        minimum = None if strict_unknown and len(known) != len(bucket) else min(values) if values else None
        maximum = None if strict_unknown and len(known) != len(bucket) else max(values) if values else None
        min_point = next((p for p in known if _decimal(p.value) == minimum), None)
        max_point = next((p for p in known if _decimal(p.value) == maximum), None)
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            mean = None if strict_unknown and len(known) != len(bucket) else sum(values, Decimal(0)) / Decimal(len(values)) if values else None
        first, last = bucket[0], bucket[-1]
        end_us = start_us + (index + 1) * width_us
        signature = (_provenance_signature(first), first.availability)
        if previous_index is None or gap_before or previous_signature != signature:
            segment_index += 1
        display_midpoint_us = (first.timestamp_us + last.timestamp_us) // 2
        membership = [(p.immutable_identity, _timestamp_text(_datetime_from_us(p.timestamp_us)),
                       _canonical_decimal(p.value) if p.value is not None else None,
                       canonical_chart_digest(p.provenance)) for p in bucket]
        record = {
            "index": index, "bucket_index": index, "segment": segment_index, "segment_index": segment_index,
            "grid_start_us": start_us + index * width_us, "grid_end_us": end_us,
            "grid_start_elapsed": _elapsed_decimal(start_us + index * width_us, start_us),
            "grid_end_elapsed": _elapsed_decimal(end_us, start_us),
            "display_midpoint_us": display_midpoint_us, "display_midpoint_elapsed": _elapsed_decimal(display_midpoint_us, start_us),
            "first": _record_value(_decimal(first.value) if first.value is not None else None),
            "last": _record_value(_decimal(last.value) if last.value is not None else None),
            "min": _record_value(minimum), "max": _record_value(maximum), "mean": _record_value(mean),
            "count": len(bucket), "known_count": len(known),
            "availability": AVAILABLE if len(known) == len(bucket) else UNKNOWN,
            "first_timestamp_us": first.timestamp_us, "last_timestamp_us": last.timestamp_us,
            "first_source_id": first.immutable_identity, "last_source_id": last.immutable_identity,
            "first_exact": _observation(first, start_us=start_us), "last_exact": _observation(last, start_us=start_us),
            "exact_min": _observation(min_point, start_us=start_us), "exact_max": _observation(max_point, start_us=start_us),
            "min_exact": _observation(min_point, start_us=start_us), "max_exact": _observation(max_point, start_us=start_us),
            "provenance_membership_digest": canonical_chart_digest(membership),
            "provenance": dict(first.provenance),
            "gap_before": gap_before, "gap_start_us": start_us + (previous_index + 1) * width_us if gap_before else None,
            "gap_end_us": start_us + index * width_us if gap_before else None, "gap": gap_before,
        }
        records.append(record)
        previous_index = index
        previous_signature = signature
    return tuple(records)


def aggregate_elapsed_bucket(points: Sequence[ExactChartPoint | Mapping[str, Any]], *, start_us: int | None = None,
                             raw_fact_count: int | None = None, strict_unknown: bool = False,
                             duration_us: int | None = None, allow_final_boundary: bool = False) -> dict[str, Any]:
    """Aggregate exact points into the bounded elapsed-bucket contract."""
    if _contains_float(points):
        raise TypeError("float is forbidden in chart facts")
    if raw_fact_count is not None and (isinstance(raw_fact_count, bool) or raw_fact_count < 0 or raw_fact_count > MAX_RAW_FACTS):
        raise ResourceBoundExceeded("CHART_RAW_FACTS_LIMIT_EXCEEDED")
    normalized = tuple(
        point if isinstance(point, ExactChartPoint) else ExactChartPoint(
            int(point["timestamp_us"]), point.get("value"), str(point.get("source", TESTED)),
            int(point.get("source_ordinal", 0)), str(point.get("immutable_identity", point.get("source_id", "0"))),
            str(point.get("availability", AVAILABLE)), point.get("provenance", {}), point.get("reason")
        )
        for point in points
    )
    normalized = tuple(sorted(normalized, key=lambda p: (p.timestamp_us, p.source_ordinal, p.immutable_identity)))
    if not normalized:
        duration = 1 if duration_us is None else int(duration_us)
        return {"version": ELAPSED_BUCKET_VERSION, "duration_us": duration,
                "bucket_duration_us": str(duration), "start_us": None,
                "end_us": None, "original_point_count": 0,
                "display_bucket_count": 0, "gap_count": 0, "records": []}
    start = normalized[0].timestamp_us if start_us is None else int(start_us)
    end = normalized[-1].timestamp_us
    if start > normalized[0].timestamp_us:
        raise ValueError("chart elapsed origin precedes every point")
    duration = _ladder_width(max(1, end - start)) if duration_us is None else int(duration_us)
    if duration <= 0:
        raise ValueError("chart bucket duration is invalid")
    if duration_us is None:
        ladder_index = _BUCKET_LADDER_US.index(duration)
        records = ()
        for width in _BUCKET_LADDER_US[ladder_index:ladder_index + 17]:
            try:
                records = _aggregate_once(normalized, width, start, strict_unknown=strict_unknown, inclusive_final=True)
            except ValueError:
                records = ()
            if len(records) <= MAX_RECORDS:
                duration = width
                break
        else:
            raise ResourceBoundExceeded("RESOURCE_BOUND_EXCEEDED")
    else:
        records = _aggregate_once(normalized, duration, start, strict_unknown=strict_unknown,
                                   inclusive_final=allow_final_boundary)
        if len(records) > MAX_RECORDS:
            raise ChartFragmentLimitExceeded()
    gap_count = sum(1 for record in records if record.get("gap"))
    return {"version": ELAPSED_BUCKET_VERSION, "duration_us": duration,
            "bucket_duration_us": str(duration), "start_us": start,
            "end_us": end, "original_point_count": len(normalized),
            "display_bucket_count": len(records), "gap_count": gap_count,
            "range": {"start_us": start, "end_us": end}, "records": list(records)}


def _trace(name: str, points: tuple[ExactChartPoint, ...], *, summary: Mapping[str, Any] | None = None,
           start_us: int | None = None, duration_us: int | None = None,
           defer: bool = False) -> dict[str, Any]:
    if len(points) > MAX_RAW_FACTS:
        raise ResourceBoundExceeded("CHART_RAW_FACTS_LIMIT_EXCEEDED")
    overview = (aggregate_elapsed_bucket(points, start_us=start_us, duration_us=duration_us, strict_unknown=name == "drawdown_pct")
                if not defer else {"version": ELAPSED_BUCKET_VERSION, "records": []})
    values = [_decimal(p.value) for p in points if p.value is not None]
    summary_data = dict(summary or {})
    if name in {"drawdown", "drawdown_pct"}:
        path = [value for value in values if value <= 0]
        if len(values) == len(points) and path:
            summary_data.setdefault("max_drawdown" if name == "drawdown" else "max_drawdown_pct", _canonical_decimal(-min(path)))
        if name == "drawdown_pct" and len(values) != len(points):
            summary_data["max_drawdown_pct"] = None
        if name == "drawdown" and len(values) != len(points):
            summary_data["max_drawdown"] = None
    return {"trace": name, "version": ELAPSED_BUCKET_VERSION, "overview": overview,
            "summary": summary_data, "_exact_points": points}


def _chart_origin(manifest: Mapping[str, Any], run: Mapping[str, Any], points: Sequence[ExactChartPoint]) -> int:
    explicit = _first(run, "elapsed_origin_us", "chart_origin_us", "elapsed_origin", "chart_origin", "t_start_us", "t_start_utc", "start_us",
                      default=_first(manifest, "elapsed_origin_us", "chart_origin_us", "elapsed_origin", "chart_origin", "t_start_us", "t_start_utc", "start_us"))
    if explicit is not None:
        return _timestamp_us_exact(explicit)
    return min((point.timestamp_us for point in points), default=0)


def _finalize_trace_set(traces: list[dict[str, Any]], *, origin: int) -> tuple[int, int, int, int]:
    all_points: list[ExactChartPoint] = []
    for trace in traces:
        all_points.extend(trace.get("_exact_points", ()))
        live = trace.get("live")
        if isinstance(live, Mapping):
            all_points.extend(live.get("_exact_points", ()))
    if any(point.timestamp_us < origin for point in all_points):
        raise ValueError("negative chart elapsed time")
    end = max((point.timestamp_us for point in all_points), default=origin)
    duration = _ladder_width(max(1, end - origin))
    ladder_index = _BUCKET_LADDER_US.index(duration)
    for width in _BUCKET_LADDER_US[ladder_index:ladder_index + 17]:
        try:
            for trace in traces:
                points = tuple(trace.get("_exact_points", ()))
                trace["overview"] = aggregate_elapsed_bucket(points, start_us=origin, duration_us=width,
                                                              strict_unknown=trace.get("trace") == "drawdown_pct",
                                                              allow_final_boundary=True)
                live = trace.get("live")
                if isinstance(live, dict):
                    live_points = tuple(live.get("_exact_points", ()))
                    live["overview"] = aggregate_elapsed_bucket(live_points, start_us=origin, duration_us=width,
                                                                 strict_unknown=live.get("trace") == "drawdown_pct",
                                                                 allow_final_boundary=True)
            duration = width
            break
        except (ChartFragmentLimitExceeded, ValueError):
            continue
    else:
        raise ResourceBoundExceeded("RESOURCE_BOUND_EXCEEDED")
    for trace in traces:
        trace.pop("_exact_points", None)
        live = trace.get("live")
        if isinstance(live, dict):
            live.pop("_exact_points", None)
    return duration, end, sum(trace["overview"].get("original_point_count", 0) for trace in traces), sum(trace["overview"].get("gap_count", 0) for trace in traces)


def _net_pnl_points(equity: tuple[ExactChartPoint, ...]) -> tuple[ExactChartPoint, ...]:
    if not equity:
        return ()
    baseline = _decimal(equity[0].value) if equity[0].value is not None else None
    blocked = baseline is None
    result: list[ExactChartPoint] = []
    for point in equity:
        if point.provenance.get("baseline_complete") is True and point.value is not None:
            baseline = _decimal(point.value)
            blocked = False
        if point.value is None:
            blocked = True
        if baseline is None or point.value is None or blocked:
            result.append(ExactChartPoint(point.timestamp_us, None, point.source, point.source_ordinal, point.immutable_identity, UNKNOWN, point.provenance, "PNL_BASELINE_UNKNOWN"))
            continue
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            value = _canonical_decimal(_decimal(point.value) - baseline)
        result.append(ExactChartPoint(point.timestamp_us, value, point.source, point.source_ordinal, point.immutable_identity, AVAILABLE, {**point.provenance, "derived_basis": "EQUITY_ORIGIN"}))
    return tuple(result)


def _combine_component_points(components: Sequence[tuple[ExactChartPoint, ...]]) -> tuple[ExactChartPoint, ...]:
    if not components:
        return ()
    expected_timestamps = {p.timestamp_us for p in components[0]}
    timestamps = set(expected_timestamps)
    by_time: list[dict[int, ExactChartPoint]] = []
    for points in components:
        if any(p.value is None or p.availability != AVAILABLE for p in points):
            return ()
        mapping = {p.timestamp_us: p for p in points}
        if set(mapping) != expected_timestamps:
            return ()
        timestamps &= set(mapping)
        by_time.append(mapping)
    result: list[ExactChartPoint] = []
    for ordinal, when in enumerate(sorted(timestamps)):
        selected = [mapping[when] for mapping in by_time]
        dimensions = {(p.provenance.get("unit"), p.provenance.get("currency"), p.provenance.get("time_basis"), p.provenance.get("applicability")) for p in selected}
        if len(dimensions) != 1:
            return ()
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            total = sum((_decimal(p.value) for p in selected), Decimal(0))
        result.append(ExactChartPoint(when, _canonical_decimal(total), selected[0].source, ordinal,
                                      "pair-net:" + ":".join(p.immutable_identity for p in selected), AVAILABLE,
                                      {**selected[0].provenance, "attribution_basis": "explicit_pair_components"}))
    return tuple(result)


def _pair_member_payload(raw: Any, pair: tuple[str, str]) -> Mapping[str, Any] | None:
    payload = raw
    if isinstance(payload, Mapping) and isinstance(payload.get("series"), Mapping):
        payload = {**payload["series"], **{key: value for key, value in payload.items() if key != "series"}}
    if not isinstance(payload, Mapping):
        return None
    for root_name in ("pair_series", "pairs", "member_series", "attributed_series"):
        collection = payload.get(root_name)
        if isinstance(collection, Mapping):
            keys = (f"{pair[0]}|{pair[1]}", f"{pair[0]}:{pair[1]}", f"{pair[0]}_{pair[1]}", f"{pair[0]}/{pair[1]}")
            selected = next((collection[key] for key in keys if key in collection), None)
            if isinstance(selected, Mapping):
                return selected
            selected = next((value for value in collection.values() if isinstance(value, Mapping)
                             and str(value.get("symbol", "")).upper() == pair[0]
                             and str(value.get("side", value.get("direction", ""))).upper() == pair[1]), None)
            if isinstance(selected, Mapping):
                return selected
        elif isinstance(collection, Sequence) and not isinstance(collection, (str, bytes, bytearray)):
            selected = next((value for value in collection if isinstance(value, Mapping)
                             and str(value.get("symbol", "")).upper() == pair[0]
                             and str(value.get("side", value.get("direction", ""))).upper() == pair[1]), None)
            if isinstance(selected, Mapping):
                return selected
    return None


def _overlay_pair_points(raw: Any, *, manifest: Mapping[str, Any], run: Mapping[str, Any], pair: tuple[str, str]) -> dict[str, tuple[ExactChartPoint, ...]]:
    selected = _pair_member_payload(raw, pair)
    if selected is None:
        return {}
    same_symbol = [item for item in manifest.get("member_composition", manifest.get("composition", ()))
                   if isinstance(item, Mapping) and str(item.get("symbol", "")).upper() == pair[0]]
    live_mapping = _pair_attribution_basis(manifest, run, pair)
    if len(same_symbol) > 1 and live_mapping not in {"immutable_local_mapping", "order_link_or_immutable_mapping"} and not any(selected.get(key) is not None for key in ("order_link_id", "orderLinkId", "local_mapping_id", "mapping_digest", "immutable_identity")):
        return {}
    result: dict[str, tuple[ExactChartPoint, ...]] = {}
    for name in ("net_pnl", "net_trading_pnl", "realized_pnl", "unrealized_pnl", "fees", "funding", "initial_margin", "maintenance_margin", "im", "mm"):
        value = _series_for(selected, name)
        if value is not None:
            result[name] = build_exact_points(value, manifest=manifest, run=run, source=LIVE,
                                              provenance={"attribution_basis": "live_pair_attribution"})
    return result


def _overlay_points(raw: Any, *, manifest: Mapping[str, Any], run: Mapping[str, Any], mode: str, pair: tuple[str, str] | None) -> dict[str, tuple[ExactChartPoint, ...]]:
    if raw is None:
        return {}
    if mode == PAIR and pair is not None:
        return _overlay_pair_points(raw, manifest=manifest, run=run, pair=pair)
    if isinstance(raw, Mapping) and _point_time(raw) is not None:
        return {"equity": build_exact_points(raw, manifest=manifest, run=run, source=LIVE)} if mode == PORTFOLIO else {}
    if isinstance(raw, Mapping) and isinstance(raw.get("series"), Mapping):
        raw = raw["series"]
    if not isinstance(raw, Mapping):
        return {}
    names = ("equity", "wallet", "net_pnl", "realized_pnl", "unrealized_pnl", "fees", "funding", "initial_margin", "maintenance_margin", "im", "mm")
    result: dict[str, tuple[ExactChartPoint, ...]] = {}
    for name in names:
        value = _series_for(raw, name)
        if value is not None:
            result[name] = build_exact_points(value, manifest=manifest, run=run, source=LIVE)
    return result


def _live_portfolio_traces(raw: Any, *, manifest: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, tuple[ExactChartPoint, ...]]:
    payload = raw
    if isinstance(payload, Mapping) and isinstance(payload.get("series"), Mapping):
        payload = {**payload["series"], **{key: value for key, value in payload.items() if key != "series"}}
    if isinstance(payload, Mapping) and isinstance(payload.get("wallet"), Mapping):
        # Retained REST/WS account facts expose the selected equity and margin
        # values inside a wallet snapshot.  Flatten that read shape while
        # retaining the outer timestamp, cashflows, and source metadata.
        payload = {**payload["wallet"], **payload}
    if not isinstance(payload, Mapping):
        return {}
    equity_raw = payload if _point_time(payload) is not None else _series_for(payload, "equity") or _series_for(payload, "wallet")
    if equity_raw is None:
        return {}
    equity = build_exact_points(equity_raw, manifest=manifest, run=run, source=LIVE)
    currency = next((p.provenance.get("currency") for p in equity if p.provenance.get("currency") is not None), manifest.get("currency"))
    adjusted = _cashflow_adjusted(equity, payload.get("cashflows", _series_for(payload, "cashflows")), currency=currency)
    traces = {"net_trading_pnl": _net_pnl_points(adjusted), "cashflow_adjusted_equity": adjusted,
              "drawdown": _drawdown_points(adjusted), "drawdown_pct": _drawdown_points(adjusted, percent=True)}
    margin = _margin_points(payload, manifest=manifest, run=run, source=LIVE)
    if margin:
        traces.update({"im_load": tuple(p for p in margin if p.provenance.get("metric") == "IM"),
                       "mm_load": tuple(p for p in margin if p.provenance.get("metric") == "MM")})
    return traces


def _live_raw_sources(raw: Any, *, mode: str, pair: tuple[str, str] | None) -> tuple[tuple[str, Any], ...]:
    if raw is None:
        return ()
    payload = raw
    if isinstance(payload, Mapping) and isinstance(payload.get("series"), Mapping):
        payload = {**payload["series"], **{key: value for key, value in payload.items() if key != "series"}}
    if mode == PAIR and pair is not None:
        payload = _pair_member_payload(payload, pair) or {}
        names = ("net_pnl", "net_trading_pnl", "realized_pnl", "unrealized_pnl", "fees", "funding", "initial_margin", "maintenance_margin", "im", "mm")
    else:
        names = ("equity", "wallet", "cashflows", "initial_margin", "maintenance_margin", "margin_balance", "im", "mm")
    return tuple((name, value) for name in names if isinstance(payload, Mapping)
                 for value in (_series_for(payload, name),) if value is not None)


def _build_chart_overview(reader: Any, manifest: Any = None, *, mode: str = PORTFOLIO,
                         pair: tuple[str, str] | None = None, symbol: str | None = None,
                         side: str | None = None, live: Any = None, contour: str = "NONE",
                         raw_fact_count: int | None = None, pair_options_count: int | None = None) -> dict[str, Any]:
    """Build bounded exact chart traces from a pinned report and optional live facts."""
    manifest_map = _manifest_map(manifest) or {}
    if _contains_float(manifest_map) or _contains_float(live):
        raise TypeError("float is forbidden in chart facts")
    if raw_fact_count is not None and (isinstance(raw_fact_count, bool) or raw_fact_count < 0 or raw_fact_count > MAX_RAW_FACTS):
        raise ResourceBoundExceeded("CHART_RAW_FACTS_LIMIT_EXCEEDED")
    if pair_options_count is not None and (isinstance(pair_options_count, bool) or pair_options_count < 0 or pair_options_count > MAX_PAIR_OPTIONS):
        raise ResourceBoundExceeded("CHART_PAIR_OPTIONS_LIMIT_EXCEEDED")
    members = manifest_map.get("member_composition", manifest_map.get("composition", ()))
    distinct_options: set[tuple[str, str]] = set()
    if isinstance(members, Sequence) and not isinstance(members, (str, bytes, bytearray)):
        for item in members:
            if isinstance(item, Mapping):
                distinct_options.add((str(item.get("symbol", "")).upper(),
                                      str(item.get("side", item.get("direction", ""))).upper()))
                if len(distinct_options) > MAX_PAIR_OPTIONS:
                    raise ResourceBoundExceeded("CHART_PAIR_OPTIONS_LIMIT_EXCEEDED")
    if pair_options_count is not None and pair_options_count < len(distinct_options):
        raise ResourceBoundExceeded("CHART_PAIR_OPTIONS_COUNT_UNDERSTATED")
    mode = str(mode).upper()
    if mode not in {PORTFOLIO, PAIR}:
        return {"version": CHART_OVERVIEW_VERSION, "availability": UNKNOWN, "reason": "INVALID_MODE", "traces": []}
    pair = pair or ((str(symbol).upper(), str(side).upper()) if symbol is not None and side is not None else None)
    if mode == PAIR and (pair is None or not _pair_known(manifest_map, pair)):
        return {"version": CHART_OVERVIEW_VERSION, "mode": mode, "availability": UNKNOWN, "reason": "PAIR_UNPINNED", "traces": []}
    run = _pinned_run(reader, manifest_map)
    if run is None:
        return {"version": CHART_OVERVIEW_VERSION, "mode": mode, "availability": UNKNOWN, "reason": "BASELINE_PIN_MISMATCH", "traces": []}
    if _contains_float(run):
        raise TypeError("float is forbidden in chart facts")
    series = _series_map_for(run)
    source = TESTED
    traces: list[dict[str, Any]] = []
    cashflow_audit: dict[str, Any] | None = None
    raw_fact_total = 0
    counted_sources: set[tuple[str, str]] = set()

    def count_source(raw: Any, path: str | None = None) -> int:
        nonlocal raw_fact_total
        if raw is None:
            return 0
        identity = _source_identity(raw, path)
        if identity in counted_sources:
            return 0
        counted_sources.add(identity)
        amount = _raw_fact_count(raw)
        raw_fact_total += amount
        if raw_fact_total > MAX_RAW_FACTS:
            raise ResourceBoundExceeded("CHART_RAW_FACTS_LIMIT_EXCEEDED")
        return amount
    if mode == PORTFOLIO:
        equity_raw = _point_series(run, "equity") or _point_series(run, "wallet")
        if equity_raw is None:
            return {"version": CHART_OVERVIEW_VERSION, "mode": mode, "availability": UNKNOWN, "reason": "EQUITY_UNAVAILABLE", "traces": []}
        count_source(equity_raw, "equity")
        cashflows = run.get("cashflows", _series_for(series, "cashflows"))
        count_source(cashflows, "cashflows")
        for margin_name in ("initial_margin", "maintenance_margin", "margin_balance", "equity", "im", "mm"):
            margin_raw = _series_for(series, margin_name)
            if margin_raw is not None:
                count_source(margin_raw, margin_name)
        equity = build_exact_points(equity_raw, manifest=manifest_map, run=run, source=source)
        currency = next((p.provenance.get("currency") for p in equity if p.provenance.get("currency") is not None), manifest_map.get("currency"))
        adjusted = _cashflow_adjusted(equity, cashflows, currency=currency)
        cashflow_audit = _cashflow_audit(cashflows, currency=currency)
        dd = _drawdown_points(adjusted)
        dd_pct = _drawdown_points(adjusted, percent=True)
        margin = _margin_points(series, manifest=manifest_map, run=run, source=source)
        origin = min((point.timestamp_us for points in (equity, adjusted, dd, dd_pct, margin) for point in points), default=equity[0].timestamp_us)
        for name, points in (("net_trading_pnl", _net_pnl_points(adjusted)), ("cashflow_adjusted_equity", adjusted), ("drawdown", dd), ("drawdown_pct", dd_pct)):
            traces.append(_trace(name, points, start_us=origin, defer=True))
        if margin:
            traces.append(_trace("im_load", tuple(p for p in margin if p.provenance.get("metric") == "IM"), start_us=origin, defer=True))
            traces.append(_trace("mm_load", tuple(p for p in margin if p.provenance.get("metric") == "MM"), start_us=origin, defer=True))
        else:
            traces.append(_trace("im_load", (), start_us=origin, defer=True))
            traces.append(_trace("mm_load", (), start_us=origin, defer=True))
    else:
        # Pair views are component-only. Equity and account drawdown are never
        # synthesized from shared-symbol or un-attributed facts.
        attribution_basis = _pair_attribution_basis(manifest_map, run, pair)
        if attribution_basis is None:
            return {"version": CHART_OVERVIEW_VERSION, "mode": mode, "pair": list(pair), "availability": UNKNOWN, "reason": "PAIR_ATTRIBUTION_AMBIGUOUS", "traces": []}
        component_points: list[tuple[ExactChartPoint, ...]] = []
        pnl_components: list[tuple[ExactChartPoint, ...]] = []
        pair_trace_inputs: list[tuple[str, tuple[ExactChartPoint, ...]]] = []
        pair_payload = _pair_member_payload(run, pair) or {}
        direct_net_raw = _series_for(pair_payload, "net_trading_pnl") or _series_for(pair_payload, "net_pnl")
        net_points: tuple[ExactChartPoint, ...] = ()
        for name in ("realized_pnl", "unrealized_pnl", "fees", "funding", "initial_margin", "maintenance_margin"):
            raw = _point_series(run, name, mode=PAIR, pair=pair)
            if raw is None:
                continue
            count_source(raw, name)
            points = build_exact_points(raw, manifest=manifest_map, run=run, source=source, provenance={"attribution_basis": attribution_basis})
            component_points.append(points)
            if name in {"realized_pnl", "unrealized_pnl", "fees", "funding"}:
                pnl_components.append(points)
            pair_trace_inputs.append((name, points))
        if direct_net_raw is not None:
            count_source(direct_net_raw, "net_trading_pnl")
            net_points = build_exact_points(direct_net_raw, manifest=manifest_map, run=run, source=source,
                                            provenance={"attribution_basis": attribution_basis})
        else:
            declared = pair_payload.get("complete_components", pair_payload.get("component_set", pair_payload.get("components_complete")))
            if declared is True:
                declared = ("realized_pnl", "unrealized_pnl", "fees", "funding")
            declared_names = {str(item) for item in declared} if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes, bytearray)) else set()
            required = {"realized_pnl", "unrealized_pnl", "fees", "funding"}
            if required <= declared_names and len(pnl_components) == len(required):
                net_points = _combine_component_points(pnl_components)
        if net_points:
            pair_trace_inputs.append(("net_trading_pnl", net_points))
        pair_im = _point_series(run, "initial_margin", mode=PAIR, pair=pair)
        pair_mm = _point_series(run, "maintenance_margin", mode=PAIR, pair=pair)
        account_denominator = _point_series(run, "margin_balance") or _point_series(run, "equity")
        for name, value in (("initial_margin", pair_im), ("maintenance_margin", pair_mm), ("margin_balance", account_denominator)):
            count_source(value, name)
        if pair_im is not None and pair_mm is not None and account_denominator is not None:
            pair_margin = _margin_points({"initial_margin": pair_im, "maintenance_margin": pair_mm, "margin_balance": account_denominator}, manifest=manifest_map, run=run, source=source)
            if pair_margin:
                pair_margin = tuple(ExactChartPoint(point.timestamp_us, point.value, point.source, point.source_ordinal,
                                                    point.immutable_identity, point.availability,
                                                    {**point.provenance, "attribution_basis": attribution_basis}, point.reason)
                                    for point in pair_margin)
                pair_trace_inputs.append(("im_load", tuple(p for p in pair_margin if p.provenance.get("metric") == "IM")))
                pair_trace_inputs.append(("mm_load", tuple(p for p in pair_margin if p.provenance.get("metric") == "MM")))
        if not pair_trace_inputs:
            return {"version": CHART_OVERVIEW_VERSION, "mode": mode, "pair": list(pair), "availability": UNKNOWN, "reason": "PAIR_ATTRIBUTION_UNAVAILABLE", "traces": []}
        origin = min(point.timestamp_us for _, points in pair_trace_inputs for point in points)
        traces.extend(_trace(name, points, start_us=origin, defer=True) for name, points in pair_trace_inputs)
        if not net_points:
            unknown_net = _trace("net_trading_pnl", (), start_us=origin, defer=True)
            unknown_net["availability"] = UNKNOWN
            unknown_net["reason"] = "PAIR_COMPONENTS_INCOMPLETE"
            traces.append(unknown_net)
    for path, raw_source in _live_raw_sources(live, mode=mode, pair=pair):
        count_source(raw_source, "live." + path)
    live_series = _live_portfolio_traces(live, manifest=manifest_map, run=run) if mode == PORTFOLIO else _overlay_points(live, manifest=manifest_map, run=run, mode=mode, pair=pair)
    if live_series:
        for trace in traces:
            name = str(trace["trace"])
            source_name = "cashflow_adjusted_equity" if name == "cashflow_adjusted_equity" else "net_trading_pnl" if name == "net_trading_pnl" else name
            points = live_series.get(source_name)
            if points:
                trace.setdefault("live", _trace(name, points, start_us=origin if "origin" in locals() else min(p.timestamp_us for p in points), defer=True))
    all_requested_points = [point for trace in traces for point in trace.get("_exact_points", ())]
    all_requested_points.extend(point for trace in traces for point in trace.get("live", {}).get("_exact_points", ()) if isinstance(trace.get("live"), Mapping))
    emitted_trace_count = len(traces) + sum(1 for trace in traces if isinstance(trace.get("live"), Mapping))
    if emitted_trace_count > MAX_TRACES:
        raise ResourceBoundExceeded("CHART_TRACE_LIMIT_EXCEEDED")
    try:
        origin = _chart_origin(manifest_map, run, all_requested_points)
        bucket_duration, chart_end, displayed_source_count, gap_count = _finalize_trace_set(traces, origin=origin)
    except ResourceBoundExceeded:
        raise
    except ValueError:
        return {"version": CHART_OVERVIEW_VERSION, "mode": mode, "availability": UNKNOWN,
                "reason": "NEGATIVE_ELAPSED", "traces": []}
    if raw_fact_count is not None and raw_fact_count < raw_fact_total:
        raise ResourceBoundExceeded("CHART_RAW_FACT_COUNT_UNDERSTATED")
    if max(raw_fact_total, raw_fact_count or 0) > MAX_RAW_FACTS:
        raise ResourceBoundExceeded("CHART_RAW_FACTS_LIMIT_EXCEEDED")
    contour_body: dict[str, Any] | None = None
    if mode == PAIR and str(contour).upper() == PORTFOLIO:
        contour_body = build_chart_overview(reader, manifest_map, mode=PORTFOLIO, live=live, contour="NONE",
                                             raw_fact_count=raw_fact_count, pair_options_count=pair_options_count)
        if contour_body.get("availability") != AVAILABLE:
            contour_body = {"version": CHART_OVERVIEW_VERSION, "availability": UNKNOWN,
                            "reason": "INCOMPATIBLE_METADATA", "traces": []}
        else:
            pair_dimensions = {key: {trace.get("overview", {}).get("records", [{}])[0].get("provenance", {}).get(key) for trace in traces if trace.get("overview", {}).get("records")} for key in ("unit", "currency", "time_basis", "applicability")}
            contour_traces = contour_body.get("traces", [])
            contour_dimensions = {key: {trace.get("overview", {}).get("records", [{}])[0].get("provenance", {}).get(key) for trace in contour_traces if trace.get("overview", {}).get("records")} for key in ("unit", "currency", "time_basis", "applicability")}
            if any(left and right and left != right for left, right in ((pair_dimensions[key], contour_dimensions[key]) for key in pair_dimensions)):
                contour_body = {"version": CHART_OVERVIEW_VERSION, "availability": UNKNOWN,
                                "reason": "INCOMPATIBLE_METADATA", "traces": []}
    body = {"version": CHART_OVERVIEW_VERSION, "bucket_version": ELAPSED_BUCKET_VERSION, "mode": mode,
            "pair": list(pair) if pair else None, "availability": AVAILABLE, "traces": traces,
            "bucket_duration_us": str(bucket_duration),
            "range": {"start_us": origin, "end_us": chart_end},
            "totals": {"original_point_count": raw_fact_total, "display_bucket_count": sum(trace["overview"].get("display_bucket_count", 0) for trace in traces), "gap_count": gap_count},
            "live": {"availability": AVAILABLE if live_series else UNKNOWN, "reason": None if live_series else "NO_LIVE_BINDING", "traces": [trace["live"] for trace in traces if "live" in trace]},
            "provenance": {"manifest_id": manifest_map.get("manifest_id"), "run_id": manifest_map.get("run_id"),
                           "attempt_id": manifest_map.get("attempt_id"), "semantic_digest": manifest_map.get("semantic_digest"),
                           "series_version": manifest_map.get("series_version"), "metrics_version": manifest_map.get("metrics_version")}}
    if cashflow_audit is not None:
        body["cashflow_audit"] = cashflow_audit
    if contour_body is not None:
        body["contour"] = {"mode": PORTFOLIO, "version": contour_body["version"],
                           "availability": contour_body["availability"], "reason": contour_body.get("reason"),
                           "traces": contour_body.get("traces", [])}
    encoded = canonical_chart_bytes(body)
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ResourceBoundExceeded("CHART_RESPONSE_BYTES_LIMIT_EXCEEDED")
    return body


def build_chart_overview(reader: Any, manifest: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Build a bounded overview and turn every resource breach into a small UNKNOWN body."""
    try:
        return _build_chart_overview(reader, manifest, **kwargs)
    except ResourceBoundExceeded as error:
        return {"version": CHART_OVERVIEW_VERSION, "availability": UNKNOWN,
                "reason": error.reason, "traces": []}


def serialize_chart_overview(value: Any) -> bytes:
    encoded = canonical_chart_bytes(value)
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ResourceBoundExceeded("CHART_RESPONSE_BYTES_LIMIT_EXCEEDED")
    return encoded


build_exact_chart_overview = build_chart_overview
build_overview = build_chart_overview
build_exact_trace = build_exact_points
aggregate_trace = aggregate_elapsed_bucket
serialize_overview = serialize_chart_overview


@dataclass(frozen=True, slots=True)
class ChartHTTPResponse:
    status: int
    body: Mapping[str, Any]
    headers: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.headers is None:
            object.__setattr__(self, "headers", {})

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "headers": dict(self.headers), "body": _canonical_json_value(self.body)}

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self):
        yield self.status
        yield self.body

    @property
    def status_code(self) -> int:
        return self.status


def _query_values(query: Mapping[str, Any] | None, path: str) -> dict[str, list[str]] | None:
    parsed = parse_qs(urlsplit(path).query, keep_blank_values=True)
    if query is not None:
        for key, value in query.items():
            if isinstance(value, (list, tuple)):
                parsed.setdefault(str(key), []).extend(str(item) for item in value)
            else:
                parsed.setdefault(str(key), []).append(str(value))
    return parsed


def _service_call(service: Any, names: tuple[str, ...], *args: Any) -> Any:
    for name in names:
        method = getattr(service, name, None)
        if callable(method):
            return method(*args)
    if isinstance(service, Mapping):
        for name in names:
            value = service.get(name)
            if callable(value):
                return value(*args)
    return None


def _service_methods(service: Any, names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in names
                 if callable(getattr(service, name, None)) or
                 (isinstance(service, Mapping) and callable(service.get(name))))


def handle_chart_request(method: Any, path: str, query: Mapping[str, Any] | None = None,
                         headers: Mapping[str, Any] | None = None, *, service: Any = None,
                         reader: Any = None, manifest: Any = None, live: Any = None) -> ChartHTTPResponse:
    """Pure GET-only route seam; validation precedes every service read."""
    # Accept the natural ``handler(service, method, path, query)`` fixture
    # shape as well as the keyword-oriented shape used by the direct tests.
    if not isinstance(method, str) and service is None:
        service, method, path, query, headers = method, path, query, headers, None
    if str(method).upper() != "GET":
        return ChartHTTPResponse(405, {"error": {"reason": "METHOD_NOT_ALLOWED"}}, {"Allow": "GET"})
    match = re.fullmatch(r"/api/v2/portfolio/campaigns/([^/]+)/candidates/([^/]+)/charts(?:\?.*)?", str(path))
    if not match or not _URI_ID.fullmatch(match.group(1)) or not _URI_ID.fullmatch(match.group(2)):
        return ChartHTTPResponse(400, {"error": {"reason": "INVALID_PATH"}})
    campaign_id, candidate_id = match.group(1), match.group(2)
    values = _query_values(query, path)
    if values is None:
        return ChartHTTPResponse(400, {"error": {"reason": "INVALID_QUERY"}})
    allowed = {"mode", "symbol", "side", "contour"}
    if any(key not in allowed for key in values) or any(len(items) != 1 or not items[0] or "/" in items[0] for items in values.values()):
        return ChartHTTPResponse(400, {"error": {"reason": "INVALID_QUERY"}})
    mode = values.get("mode", [None])[0]
    if mode not in {PORTFOLIO, PAIR}:
        return ChartHTTPResponse(400, {"error": {"reason": "INVALID_QUERY"}})
    if mode == PORTFOLIO and any(key in values for key in ("symbol", "side", "contour")):
        return ChartHTTPResponse(400, {"error": {"reason": "INVALID_QUERY"}})
    if mode == PAIR and ("symbol" not in values or "side" not in values or values["side"][0] not in {"LONG", "SHORT"}):
        return ChartHTTPResponse(400, {"error": {"reason": "INVALID_QUERY"}})
    if "contour" in values and values["contour"][0] not in {"NONE", "PORTFOLIO"}:
        return ChartHTTPResponse(400, {"error": {"reason": "INVALID_QUERY"}})
    pair = (values["symbol"][0].upper(), values["side"][0]) if mode == PAIR else None
    if mode == PAIR and service is None and (not isinstance(manifest, Mapping) or not _pair_known(manifest, pair)):
        return ChartHTTPResponse(404, {"error": {"reason": "PAIR_NOT_FOUND"}})
    if service is None:
        manifest_map = _manifest_map(manifest) or {}
        if (str(manifest_map.get("campaign_id", "")) != campaign_id or
                str(manifest_map.get("candidate_id", "")) != candidate_id):
            return ChartHTTPResponse(404, {"error": {"reason": "CANDIDATE_NOT_FOUND"}})
    if service is not None:
        campaign_methods = _service_methods(service, ("campaign_exists", "has_campaign"))
        if not campaign_methods:
            return ChartHTTPResponse(404, {"error": {"reason": "CAMPAIGN_NOT_FOUND"}})
        known = _service_call(service, campaign_methods, campaign_id) if campaign_methods else None
        if campaign_methods and not known:
            return ChartHTTPResponse(404, {"error": {"reason": "CAMPAIGN_NOT_FOUND"}})
        candidate_methods = _service_methods(service, ("candidate", "get_candidate", "has_candidate"))
        if not candidate_methods:
            return ChartHTTPResponse(404, {"error": {"reason": "CANDIDATE_NOT_FOUND"}})
        candidate = _service_call(service, candidate_methods, campaign_id, candidate_id) if candidate_methods else None
        if candidate is False or candidate is None and candidate_methods:
            # A service that exposes neither lookup is treated as an adapter
            # seam; a false/empty lookup remains a real 404.
            if candidate_methods:
                return ChartHTTPResponse(404, {"error": {"reason": "CANDIDATE_NOT_FOUND"}})
        if isinstance(candidate, Mapping) and (str(candidate.get("campaign_id", campaign_id)) != campaign_id or
                                                str(candidate.get("candidate_id", candidate_id)) != candidate_id):
            return ChartHTTPResponse(404, {"error": {"reason": "CANDIDATE_NOT_FOUND"}})
        if mode == PAIR:
            known_pair = _service_call(service, ("pair_known", "has_pair", "is_pair_pinned"), campaign_id, candidate_id, pair)
            pair_methods = _service_methods(service, ("pair_known", "has_pair", "is_pair_pinned"))
            manifest_map = _manifest_map(manifest) or {}
            candidate_pair_proof = _pair_known(candidate, pair) if isinstance(candidate, Mapping) else False
            manifest_pair_proof = _pair_known(manifest_map, pair)
            if pair_methods and not known_pair:
                return ChartHTTPResponse(404, {"error": {"reason": "PAIR_NOT_FOUND"}})
            if not pair_methods and not (candidate_pair_proof or manifest_pair_proof):
                return ChartHTTPResponse(404, {"error": {"reason": "PAIR_NOT_FOUND"}})
        bindings = _service_call(service, ("active_bindings", "get_active_bindings"), campaign_id, candidate_id)
        if isinstance(bindings, Sequence) and not isinstance(bindings, (str, bytes)) and len(bindings) > 1:
            return ChartHTTPResponse(409, {"error": {"reason": "INCONSISTENT"}})
        pair_options = _service_call(service, ("pair_options", "get_pair_options"), campaign_id, candidate_id)
        if isinstance(pair_options, Sequence) and not isinstance(pair_options, (str, bytes)) and len(pair_options) > MAX_PAIR_OPTIONS:
            return ChartHTTPResponse(413, {"error": {"reason": "RESOURCE_BOUND_EXCEEDED", "detail": "CHART_PAIR_OPTIONS_LIMIT_EXCEEDED"}})
    try:
        result = _service_call(service, ("read_chart", "read_charts", "get_chart"), campaign_id, candidate_id, mode, pair) if service is not None else None
        if result is None:
            if manifest is None and service is not None:
                manifest = _service_call(service, ("get_manifest", "manifest"), campaign_id, candidate_id)
            if reader is None and service is not None:
                reader = _service_call(service, ("reader", "get_reader", "portfolio_reader"), campaign_id, candidate_id)
            result = build_chart_overview(reader, manifest, mode=mode, pair=pair, contour=values.get("contour", ["NONE"])[0], live=live)
        body = result.as_dict() if hasattr(result, "as_dict") else result
        if isinstance(body, Mapping) and body.get("availability") == UNKNOWN and (
                str(body.get("reason", "")).startswith("CHART_") or
                body.get("reason") == "RESOURCE_BOUND_EXCEEDED"):
            return ChartHTTPResponse(413, {"error": {"reason": "RESOURCE_BOUND_EXCEEDED", "detail": body.get("reason")}})
        if isinstance(body, Mapping) and body.get("reason") == "INCOMPATIBLE_METADATA":
            return ChartHTTPResponse(409, {"error": {"reason": "INCOMPATIBLE_METADATA"}})
        encoded = canonical_chart_bytes(body)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ResourceBoundExceeded("CHART_RESPONSE_BYTES_LIMIT_EXCEEDED")
        return ChartHTTPResponse(200, body)
    except ResourceBoundExceeded as error:
        return ChartHTTPResponse(413, {"error": {"reason": "RESOURCE_BOUND_EXCEEDED", "detail": error.reason}})
    except (TypeError, ValueError):
        return ChartHTTPResponse(409, {"error": {"reason": "INCOMPATIBLE_METADATA"}})


chart_request = handle_chart_request
read_chart_request = handle_chart_request
get_chart_response = handle_chart_request
chart_handler = handle_chart_request
