"""Read-only preflight and in-memory materialization for direct v5 source reports."""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Callable, Mapping

import duckdb
import pandas as pd

from .duckdb_events import (
    calculate_point_metrics,
    decode_compact_actions,
    decode_compact_deltas,
    decode_wallet_changes,
)
from .duckdb_source_schema import NORMALIZATION_CONTRACT_VERSION, validate_source_database_structural
from .source_packs import SourcePackError
from .analysis_storage import PublishedSurface, publish_surface


TRADES_PROXY_EVENT_MODE = "legacy_trades_proxy"


class DirectMaterializationError(ValueError):
    """The source cannot produce a complete direct surface."""


@dataclass(frozen=True, slots=True)
class DirectBuildRequest:
    start_utc: str
    end_utc: str
    side: str
    symbols: tuple[str, ...]
    required_shifts_bp: tuple[int, ...]
    materializer_version: str
    point_materialization_config_hash: str


@dataclass(frozen=True, slots=True)
class CoverageIssue:
    symbol: str
    timeframe: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class DirectPreflight:
    usable_timeframes: Mapping[str, tuple[str, ...]]
    unavailable_symbols: Mapping[str, tuple[str, ...]]
    coverage_issues: tuple[CoverageIssue, ...]
    grid_contract: Mapping[str, object]
    source_hashes: tuple[str, ...]
    manifest: tuple[tuple[str, str], ...]
    accepted_point_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DirectPoint:
    canonical_point_key: str
    source_report_id: str
    source_hash: str
    point_event_count: int
    metrics: Mapping[str, int | float | None]
    provenance_state: str = "REPRODUCIBLE"


@dataclass(frozen=True, slots=True)
class DirectSurface:
    request: DirectBuildRequest
    preflight: DirectPreflight
    event_mode: str
    points: tuple[DirectPoint, ...]
    parent_surface_id: str | None = None
    build_mode: str = "DUCKDB_DIRECT"


def _window(request: DirectBuildRequest) -> tuple[pd.Timestamp, pd.Timestamp]:
    start, end = pd.Timestamp(request.start_utc), pd.Timestamp(request.end_utc)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    if end <= start:
        raise DirectMaterializationError("UTC end must be later than start")
    if request.side not in {"LONG", "SHORT"} or not request.symbols or not request.required_shifts_bp:
        raise DirectMaterializationError("side, symbols and required shifts are required")
    return start, end


def _reports(source: duckdb.DuckDBPyConnection, request: DirectBuildRequest) -> list[dict[str, object]]:
    cursor = source.execute(
        """select r.report_id,r.canonical_point_key,r.source_sha256,r.raw_action_count,
                  r.equity_sample_count,r.wallet_change_count,r.grid_hash,
                  g.sample_count,g.start_timestamp_ms,g.end_timestamp_ms,
                  p.symbol,p.side,p.timeframe,p.shift_bp,p.open_ma_len,p.close_ma_len
             from active_reports r join point_configs p using(canonical_point_key)
             join time_grids g using(grid_hash)
            where p.side=? and p.symbol in (select * from unnest(?))
            order by p.symbol,p.timeframe,p.shift_bp,p.open_ma_len,p.close_ma_len,r.report_id""",
        [request.side, list(request.symbols)],
    )
    return [dict(zip((item[0] for item in cursor.description), row, strict=True)) for row in cursor.fetchall()]


def preflight_duckdb_direct(source_connection: duckdb.DuckDBPyConnection, request: DirectBuildRequest) -> DirectPreflight:
    """Validate full UTC coverage and observed-grid completeness without decoding payloads."""
    start, end = _window(request)
    validation = validate_source_database_structural(source_connection)
    if not validation.valid:
        raise DirectMaterializationError(f"invalid active v5 source: {validation.errors}")
    start_ms, end_ms = start.value // 1_000_000, end.value // 1_000_000
    all_reports = _reports(source_connection, request)
    covered = [
        row for row in all_reports
        if int(row["sample_count"]) > 0
        and int(row["start_timestamp_ms"]) <= start_ms
        and int(row["end_timestamp_ms"]) >= end_ms
    ]
    issues: list[CoverageIssue] = []
    by_timeframe: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in covered:
        by_timeframe.setdefault((str(row["symbol"]), str(row["timeframe"])), []).append(row)
    discovered_timeframes = {(str(row["symbol"]), str(row["timeframe"])) for row in all_reports}
    usable: dict[str, list[str]] = {}
    accepted: list[dict[str, object]] = []
    contract_pairs: set[str] = set()
    required = set(request.required_shifts_bp)
    for symbol, timeframe in sorted(discovered_timeframes):
        rows = by_timeframe.get((symbol, timeframe), [])
        by_shift: dict[int, set[tuple[int, int]]] = {}
        for row in rows:
            by_shift.setdefault(int(row["shift_bp"]), set()).add((int(row["open_ma_len"]), int(row["close_ma_len"])))
        observed = set().union(*(by_shift.get(shift, set()) for shift in required))
        missing = [(shift, pair) for shift in sorted(required) for pair in sorted(observed - by_shift.get(shift, set()))]
        if not observed:
            issues.append(CoverageIssue(symbol, timeframe, "GRID_NOT_COVERED", "no report covers the requested UTC half-open window"))
        elif missing:
            for shift, pair in missing:
                issues.append(CoverageIssue(symbol, timeframe, "MISSING_GRID_CELL", f"shift={shift}; pair={pair[0]}|{pair[1]}"))
        else:
            keys = {str(row["canonical_point_key"]) for row in rows if int(row["shift_bp"]) in required and (int(row["open_ma_len"]), int(row["close_ma_len"])) in observed}
            if len(keys) != len([row for row in rows if int(row["shift_bp"]) in required and (int(row["open_ma_len"]), int(row["close_ma_len"])) in observed]):
                issues.append(CoverageIssue(symbol, timeframe, "CONFLICTING_CANONICAL_POINT", "multiple active reports map to one canonical point"))
                continue
            usable.setdefault(symbol, []).append(timeframe)
            accepted.extend(row for row in rows if str(row["canonical_point_key"]) in keys)
            contract_pairs.update(f"{shift}|{open_ma}|{close_ma}" for shift in required for open_ma, close_ma in observed)
    unavailable = {symbol: tuple(sorted(tf for sym, tf in discovered_timeframes if sym == symbol)) for symbol in request.symbols if symbol not in usable}
    accepted.sort(key=lambda row: str(row["canonical_point_key"]))
    return DirectPreflight(
        {symbol: tuple(sorted(timeframes)) for symbol, timeframes in sorted(usable.items())}, unavailable,
        tuple(issues), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT", "required_shifts_bp": tuple(sorted(required)), "pairs": tuple(sorted(contract_pairs)), "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION}),
        tuple(sorted(str(row["source_sha256"]) for row in accepted)),
        tuple((str(row["report_id"]), str(row["source_sha256"])) for row in accepted),
        tuple(str(row["canonical_point_key"]) for row in accepted),
    )


def materialize_duckdb_direct(source_connection: duckdb.DuckDBPyConnection, analysis_connection: duckdb.DuckDBPyConnection, request: DirectBuildRequest, cancellation: Callable[[], bool], *, preflight: DirectPreflight | None = None) -> DirectSurface:
    """Decode only preflight-accepted reports into a non-published direct surface."""
    if source_connection is analysis_connection:
        raise DirectMaterializationError("source and analysis connections must be distinct")
    if cancellation():
        raise DirectMaterializationError("direct materialization cancelled before publication")
    preflight = preflight or preflight_duckdb_direct(source_connection, request)
    if preflight.unavailable_symbols:
        raise DirectMaterializationError("selected symbol has no usable timeframe")
    start, end = _window(request)
    points: list[DirectPoint] = []
    for report_id, source_hash in preflight.manifest:
        if cancellation():
            raise DirectMaterializationError("direct materialization cancelled before publication")
        row = source_connection.execute(
            """select r.canonical_point_key,r.raw_action_count,r.equity_sample_count,r.wallet_change_count,
                      p.series_codec,p.actions_codec,p.actions_zlib,p.equity_zlib,p.wallet_zlib,g.sample_count,g.timestamps_zlib
                 from active_reports r join report_payloads p using(report_id) join time_grids g using(grid_hash)
                where r.report_id=? and r.source_sha256=?""", [report_id, source_hash]
        ).fetchone()
        if row is None:
            raise DirectMaterializationError("active source changed after preflight")
        point_key, action_count, equity_count, wallet_count, series_codec, _, actions_blob, equity_blob, wallet_blob, sample_count, timestamps_blob = row
        try:
            grid = pd.to_datetime(decode_compact_deltas(bytes(timestamps_blob), int(sample_count), codec=str(series_codec)), unit="ms", utc=True)
            metrics = calculate_point_metrics(grid, decode_compact_deltas(bytes(equity_blob), int(equity_count), codec=str(series_codec)), decode_wallet_changes(bytes(wallet_blob), int(wallet_count), codec=str(series_codec)), decode_compact_actions(bytes(actions_blob), int(action_count)), start.isoformat(), end.isoformat())
        except SourcePackError as error:
            raise DirectMaterializationError(f"cannot materialize {point_key}: {error}") from error
        points.append(DirectPoint(str(point_key), report_id, source_hash, int(metrics["TotalTrades"]), metrics))
    if len({point.canonical_point_key for point in points}) != len(points):
        raise DirectMaterializationError("canonical point uniqueness failed")
    return DirectSurface(request, preflight, TRADES_PROXY_EVENT_MODE, tuple(points))


def run_panel_direct_build(
    source_connection: duckdb.DuckDBPyConnection,
    analysis_connection: duckdb.DuckDBPyConnection,
    request: DirectBuildRequest,
    cancellation: Callable[[], bool],
    progress_callback: Callable[..., object],
    *,
    parent_surface_id: str | None = None,
) -> PublishedSurface:
    """Build and atomically publish a preflight-bound direct surface."""
    if source_connection is analysis_connection:
        raise DirectMaterializationError("source and analysis connections must be distinct")
    progress_callback("PREFLIGHT", selected_symbols=len(request.symbols))
    preflight = preflight_duckdb_direct(source_connection, request)
    if preflight.unavailable_symbols:
        raise DirectMaterializationError("selected symbol has no usable timeframe")
    if cancellation():
        raise DirectMaterializationError("direct materialization cancelled before publication")
    progress_callback("MATERIALIZING", usable_timeframes=sum(map(len, preflight.usable_timeframes.values())))
    surface = materialize_duckdb_direct(source_connection, analysis_connection, request, cancellation, preflight=preflight)
    if cancellation():
        raise DirectMaterializationError("direct materialization cancelled before publication")
    progress_callback("REVALIDATING", materialized_points=len(surface.points))
    active = preflight_duckdb_direct(source_connection, request)
    if active != preflight:
        raise DirectMaterializationError("active source changed after preflight")
    if cancellation():
        raise DirectMaterializationError("direct materialization cancelled before publication")
    if parent_surface_id is not None:
        surface = replace(surface, parent_surface_id=parent_surface_id)
    published = publish_surface(analysis_connection, surface)
    progress_callback("PUBLISHED", materialized_points=len(published.points), publication_state="PUBLISHED")
    return published
