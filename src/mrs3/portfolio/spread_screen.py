"""Pure preliminary spread screening for already-loaded finalist facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mrs3.bybit_collector.storage import PublishedHour, SQLiteSpool, StorageWriteError

from .liquidity import LiquidityError, _read_marked_file


READY = "READY"
PRELIMINARY = "PRELIMINARY"
CLEAR = "CLEAR"
OVERLAPS_SPREAD = "OVERLAPS_SPREAD"
UNKNOWN = "UNKNOWN"
_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000
_SPREAD_SOURCE = "bybit_liquidity_1m/published_hours"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{field} must be an exact Decimal-compatible value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a Decimal-compatible value") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _nonnegative_decimal(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _reader_spread(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise LiquidityError(f"{field} must be a finite non-negative spread", "LIQUIDITY_QUALITY_INSUFFICIENT")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiquidityError(f"{field} must be a finite non-negative spread", "LIQUIDITY_QUALITY_INSUFFICIENT") from exc
    if not result.is_finite() or result < 0:
        raise LiquidityError(f"{field} must be a finite non-negative spread", "LIQUIDITY_QUALITY_INSUFFICIENT")
    return result


def _mean(values: Sequence[Decimal]) -> Decimal:
    precision = max(80, max(len(value.as_tuple().digits) for value in values) + len(str(len(values))) + 16)
    with localcontext() as context:
        context.prec = precision
        total = sum(values, Decimal(0))
        return total / Decimal(len(values))


@dataclass(frozen=True, slots=True)
class SpreadHistoryReadResult:
    observations: Mapping[str, tuple[Mapping[str, Decimal], ...]]
    statuses: Mapping[str, str]
    window_start_ms: int
    window_end_ms: int
    available_hour_count: int
    source: str = _SPREAD_SOURCE

    def __post_init__(self) -> None:
        observations = {
            str(symbol): tuple(_freeze(row) for row in rows)
            for symbol, rows in sorted(self.observations.items(), key=lambda item: str(item[0]))
        }
        statuses = {str(symbol): str(status) for symbol, status in sorted(self.statuses.items(), key=lambda item: str(item[0]))}
        object.__setattr__(self, "observations", MappingProxyType(observations))
        object.__setattr__(self, "statuses", MappingProxyType(statuses))

    @property
    def spread_observations(self) -> Mapping[str, tuple[Mapping[str, Decimal], ...]]:
        return self.observations

    @property
    def history_statuses(self) -> Mapping[str, str]:
        return self.statuses

    def __iter__(self):
        yield self.observations
        yield self.statuses


def _requested_symbols(symbols: Sequence[str] | str | None) -> tuple[str, ...] | None:
    if symbols is None:
        return None
    if isinstance(symbols, str):
        symbols = (symbols,)
    elif isinstance(symbols, (bytes,)) or not isinstance(symbols, Sequence):
        raise TypeError("symbols must be a sequence of non-empty strings")
    result = tuple(sorted(set(symbols)))
    if any(not isinstance(symbol, str) or not symbol for symbol in result):
        raise ValueError("symbols must be a sequence of non-empty strings")
    return result


def read_spread_history(
    root: str | Path,
    symbols: Sequence[str] | str | None = None,
    *,
    now_ms: int,
    minimum_coverage_pct: int = 90,
) -> SpreadHistoryReadResult:
    """Read marker-authoritative p95 spread rows from the available UTC window."""

    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")
    if type(minimum_coverage_pct) is not int or not 1 <= minimum_coverage_pct <= 100:
        raise ValueError("minimum_coverage_pct must be an integer from 1 through 100")
    requested = _requested_symbols(symbols)
    day_end = (now_ms // _DAY_MS) * _DAY_MS
    window_start = day_end - 7 * _DAY_MS
    root_path = Path(root)
    try:
        with SQLiteSpool.open_read_only(root_path) as spool:
            marker_rows = tuple(spool.published_hours())
    except StorageWriteError:
        marker_rows = ()
    if len({marker.hour_start_ms for marker in marker_rows}) != len(marker_rows):
        raise LiquidityError("published liquidity contains duplicate hour markers", "LIQUIDITY_QUALITY_INSUFFICIENT")
    selected = tuple(
        sorted(
            (marker for marker in marker_rows if window_start <= marker.hour_start_ms < day_end),
            key=lambda marker: marker.hour_start_ms,
        )
    )
    rows_by_symbol: dict[str, list[Mapping[str, Decimal]]] = {}
    symbol_hours: dict[str, set[int]] = {}
    seen_rows: set[tuple[int, str]] = set()
    for marker in selected:
        if not isinstance(marker, PublishedHour):
            raise LiquidityError("published liquidity marker is invalid", "LIQUIDITY_QUALITY_INSUFFICIENT")
        for row in _read_marked_file(root_path, marker):
            if not isinstance(row, Mapping):
                raise LiquidityError("liquidity parquet row is invalid", "LIQUIDITY_QUALITY_INSUFFICIENT")
            minute = row.get("minute_ts_ms")
            symbol = row.get("symbol")
            if type(minute) is not int or not isinstance(symbol, str) or not symbol:
                raise LiquidityError("liquidity parquet row identity is invalid", "LIQUIDITY_QUALITY_INSUFFICIENT")
            key = (minute, symbol)
            if key in seen_rows:
                raise LiquidityError("duplicate minute/symbol liquidity rows", "LIQUIDITY_QUALITY_INSUFFICIENT")
            seen_rows.add(key)
            symbol_hours.setdefault(symbol, set()).add(marker.hour_start_ms)
            coverage = _reader_spread(row.get("coverage_ratio"), f"{symbol}:{minute}.coverage_ratio")
            if coverage > 1:
                raise LiquidityError("liquidity coverage ratio exceeds one", "LIQUIDITY_QUALITY_INSUFFICIENT")
            if coverage * 100 < minimum_coverage_pct:
                continue
            value = row.get("spread_bps_p95")
            if value is None:
                continue
            spread = _reader_spread(value, f"{symbol}:{minute}.spread_bps_p95")
            rows_by_symbol.setdefault(symbol, []).append({"spread_bps_p95": spread})

    known_symbols = set(symbol_hours) | set(rows_by_symbol)
    output_symbols = set(requested) if requested is not None else known_symbols
    observations = {
        symbol: tuple(rows_by_symbol.get(symbol, ()))
        for symbol in sorted(output_symbols)
    }
    statuses: dict[str, str] = {}
    for symbol in sorted(output_symbols):
        hours = symbol_hours.get(symbol, set())
        if not hours:
            statuses[symbol] = UNKNOWN
        elif len(selected) == 7 * 24 and len(hours) == 7 * 24:
            statuses[symbol] = READY
        else:
            statuses[symbol] = PRELIMINARY
    return SpreadHistoryReadResult(observations, statuses, window_start, day_end, len(selected))


read_available_spread_history = read_spread_history


@dataclass(frozen=True, slots=True)
class SpreadHistoryDiagnostic:
    symbol: str
    history_status: str | None
    mean_spread_bps: Decimal | None
    observation_count: int
    usable_observation_count: int
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    @property
    def spread_bps_p95_mean(self) -> Decimal | None:
        return self.mean_spread_bps

    @property
    def provenance_count(self) -> int:
        return self.usable_observation_count


@dataclass(frozen=True, slots=True)
class SpreadExclusion:
    row: Mapping[str, Any]
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "row", _freeze(self.row))

    @property
    def candidate(self) -> Mapping[str, Any]:
        return self.row


@dataclass(frozen=True, slots=True)
class SpreadScreenResult:
    retained_rows: tuple[Mapping[str, Any], ...]
    exclusions: tuple[SpreadExclusion, ...]
    warnings: tuple[str, ...]
    per_symbol: tuple[SpreadHistoryDiagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "retained_rows", tuple(_freeze(row) for row in self.retained_rows))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(self.warnings)))
        object.__setattr__(self, "per_symbol", tuple(self.per_symbol))

    @property
    def retained(self) -> tuple[Mapping[str, Any], ...]:
        return self.retained_rows

    @property
    def history(self) -> tuple[SpreadHistoryDiagnostic, ...]:
        return self.per_symbol


def _history_diagnostics(
    symbol: str,
    observations: Sequence[Mapping[str, Any]],
    history_status: str | None,
) -> SpreadHistoryDiagnostic:
    values: list[Decimal] = []
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise TypeError(f"liquidity observation {symbol}[{index}] must be a mapping")
        if "spread_bps_p95" not in observation:
            raise ValueError(f"liquidity observation {symbol}[{index}] lacks spread_bps_p95")
        values.append(_nonnegative_decimal(observation["spread_bps_p95"], f"{symbol}[{index}].spread_bps_p95"))
    diagnostics: list[str] = []
    if history_status not in {READY, PRELIMINARY}:
        diagnostics.append("SPREAD_HISTORY_STATUS_UNKNOWN")
    elif history_status == PRELIMINARY:
        diagnostics.append("SPREAD_HISTORY_PRELIMINARY")
    mean = _mean(values) if values else None
    if mean is None:
        diagnostics.append("SPREAD_HISTORY_UNKNOWN")
    return SpreadHistoryDiagnostic(symbol, history_status, mean, len(observations), len(values), tuple(diagnostics))


def _valid_shifts(row: Mapping[str, Any]) -> tuple[Decimal, ...] | None:
    orders = row.get("strategy_orders")
    if isinstance(orders, (str, bytes, Mapping)) or not isinstance(orders, Sequence) or not orders:
        return None
    shifts: list[Decimal] = []
    try:
        for order in orders:
            if not isinstance(order, Mapping) or "shift_bp" not in order:
                return None
            shift = _nonnegative_decimal(order["shift_bp"], "shift_bp")
            shifts.append(shift)
    except (TypeError, ValueError):
        return None
    return tuple(shifts)


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("symbol", "")),
        str(row.get("side", "")),
        str(row.get("strategy_id", "")),
        str(row.get("result_id", "")),
        str(row.get("strategy_name", "")),
    )


def screen_spread(
    finalist_rows: Sequence[Mapping[str, Any]],
    liquidity_observations: Mapping[str, Sequence[Mapping[str, Any]]],
    history_statuses: Mapping[str, str],
) -> SpreadScreenResult:
    """Classify and preliminarily filter finalist rows by p95 spread history."""

    if isinstance(finalist_rows, (str, bytes)) or not isinstance(finalist_rows, Sequence):
        raise TypeError("finalist_rows must be a sequence")
    if not isinstance(liquidity_observations, Mapping):
        raise TypeError("liquidity_observations must be a mapping")
    if not isinstance(history_statuses, Mapping):
        raise TypeError("history_statuses must be a mapping")

    observations_by_symbol: dict[str, Sequence[Mapping[str, Any]]] = {}
    for symbol, observations in liquidity_observations.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("liquidity observation symbols must be non-empty strings")
        if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
            raise TypeError(f"liquidity observations for {symbol} must be a sequence")
        observations_by_symbol[symbol] = observations

    status_by_symbol = dict(history_statuses)
    symbols = set(observations_by_symbol) | {symbol for row in finalist_rows if isinstance(row, Mapping) and isinstance((symbol := row.get("symbol")), str)}
    symbols |= {symbol for symbol in status_by_symbol if isinstance(symbol, str) and symbol}
    histories: dict[str, SpreadHistoryDiagnostic] = {}
    for symbol in sorted(symbols):
        histories[symbol] = _history_diagnostics(
            symbol,
            observations_by_symbol.get(symbol, ()),
            status_by_symbol.get(symbol),
        )

    warnings: list[str] = []
    for history in histories.values():
        warnings.extend(history.diagnostics)

    classified: list[tuple[Mapping[str, Any], str, tuple[str, ...], tuple[str, str]]] = []
    for row in finalist_rows:
        if not isinstance(row, Mapping):
            raise TypeError("each finalist row must be a mapping")
        symbol = row.get("symbol") if isinstance(row.get("symbol"), str) else ""
        side = row.get("side")
        side_key = side.upper() if isinstance(side, str) else ""
        history = histories.get(symbol)
        diagnostics = list(history.diagnostics if history is not None else ("SPREAD_HISTORY_UNKNOWN",))
        mean = history.mean_spread_bps if history is not None else None
        status = UNKNOWN
        shifts = _valid_shifts(row)
        if mean is not None and history is not None and history.history_status in {READY, PRELIMINARY}:
            if shifts is None:
                diagnostics.append("INVALID_ORDER_GEOMETRY")
            elif all(shift > mean for shift in shifts):
                status = CLEAR
            else:
                status = OVERLAPS_SPREAD
        elif shifts is None:
            diagnostics.append("INVALID_ORDER_GEOMETRY")
        enriched = dict(row)
        enriched.update(
            {
                "spread_mean_bps": mean,
                "spread_bps_p95_mean": mean,
                "spread_history_status": history.history_status if history is not None else None,
                "spread_observation_count": history.observation_count if history is not None else 0,
                "spread_usable_observation_count": history.usable_observation_count if history is not None else 0,
                "spread_status": status,
                "spread_diagnostics": tuple(dict.fromkeys(diagnostics)),
            }
        )
        frozen = _freeze(enriched)
        row_warnings = [item for item in diagnostics if item not in {"SPREAD_HISTORY_PRELIMINARY"}]
        warnings.extend(row_warnings)
        classified.append((frozen, status, tuple(dict.fromkeys(diagnostics)), (symbol, side_key)))

    clear_groups = {
        group
        for row, status, _diagnostics, group in classified
        if status == CLEAR and row.get("spread_history_status") == READY
    }
    grouped_statuses: dict[tuple[str, str], list[str]] = {}
    for _row, status, _diagnostics, group in classified:
        grouped_statuses.setdefault(group, []).append(status)

    retained: list[Mapping[str, Any]] = []
    exclusions: list[SpreadExclusion] = []
    for enriched, status, _diagnostics, group in classified:
        if status == OVERLAPS_SPREAD and group in clear_groups:
            exclusions.append(SpreadExclusion(enriched, OVERLAPS_SPREAD))
        else:
            retained.append(enriched)
    for statuses in grouped_statuses.values():
        if statuses and CLEAR not in statuses and all(status == OVERLAPS_SPREAD for status in statuses):
            warnings.append("ALL_FINALISTS_OVERLAP_SPREAD")

    retained.sort(key=_row_key)
    exclusions.sort(key=lambda item: (_row_key(item.row), item.reason))
    return SpreadScreenResult(tuple(retained), tuple(exclusions), tuple(dict.fromkeys(warnings)), tuple(histories.values()))


screen_spread_candidates = screen_spread
apply_spread_screen = screen_spread


__all__ = [
    "READY",
    "PRELIMINARY",
    "CLEAR",
    "OVERLAPS_SPREAD",
    "UNKNOWN",
    "SpreadHistoryReadResult",
    "read_spread_history",
    "read_available_spread_history",
    "SpreadHistoryDiagnostic",
    "SpreadExclusion",
    "SpreadScreenResult",
    "screen_spread",
    "screen_spread_candidates",
    "apply_spread_screen",
]
