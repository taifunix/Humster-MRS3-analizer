"""Read-only preflight and in-memory materialization for direct v5 source reports."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

import duckdb
import pandas as pd

from .duckdb_events import (
    calculate_point_metrics,
    decode_compact_actions,
    decode_compact_deltas,
    decode_wallet_changes,
    reconstruct_closed_cycles,
)
from .duckdb_source_schema import (
    NORMALIZATION_CONTRACT_VERSION,
    SourceSchemaError,
    _canonical_point_key,
    validate_source_database_structural,
)
from .source_packs import SourcePackError
from .analysis_storage import PublishedSurface, publish_surface
from .config import DEFAULT_CANONICAL_SHIFTS_BP, DirectMaterializationSettings


REAL_EVENT_MODE = "real_independent_events"
OBSERVED_GRID_CONTRACT_KIND = "OBSERVED_GRID_CONTRACT"
V2_GRID_CONTRACT_KIND = "OBSERVED_SPARSE_GRID_CONTRACT_V2"
CANONICAL_GRID_VERSION = "mrs3_shift_grid_30_550_v1"
READINESS_CONTRACT_VERSION = "close_ma_2_7_canonical_grid_v1"
CANONICAL_MATERIALIZER_VERSION = "v4-canonical-grid-parallel"
POINT_MATERIALIZATION_SEMANTICS_VERSION = "direct_point_materialization_v1"
V2_AUDIT_SCHEMA_VERSION = 1
REQUIRED_CLOSE_MAS = (2, 3, 4, 5, 6, 7)
READINESS_MAX_SHIFT_BP = 550
COVERAGE_CSV_COLUMNS = (
    "pair", "side", "timeframe",
    "evaluation_id", "displayed_interval",
    "row_type", "shift_bp", "open_ma", "close_ma",
    "interval_start_utc", "interval_end_utc",
    "report_start_utc", "report_end_utc", "grid_start_utc", "grid_end_utc",
    "effective_start_utc", "effective_end_utc",
    "required_for_readiness", "readiness_witness",
    "gap_start_bp", "gap_end_bp", "max_gap_bp",
    "report_id", "source_sha256", "selected_report",
    "status", "reason_code", "reason_detail",
    "readiness_contract_version", "readiness_max_shift_bp",
)
_PARALLEL_WAIT_TIMEOUT_SECONDS = 0.1


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
    selected_scopes: tuple[str, ...] = ()
    grid_contract_kind: str = OBSERVED_GRID_CONTRACT_KIND
    readiness_contract_version: str = READINESS_CONTRACT_VERSION
    readiness_max_shift_bp: int = READINESS_MAX_SHIFT_BP
    audit_artifact_name: str = ""
    audit_schema_version: int = 1
    audit_row_count: int = 0
    audit_sha256: str = ""
    audit_bytes: bytes | None = None
    audit_size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class DirectScope:
    symbol: str
    side: str
    timeframe: str


@dataclass(frozen=True, slots=True)
class CoverageIssue:
    symbol: str
    timeframe: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class CoverageReviewRow:
    symbol: str
    side: str
    timeframe: str
    selectable: bool
    interval_start_utc: str
    interval_end_utc: str
    gap_details: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReadinessWitness:
    open_ma: int
    close_ma: int
    shifts_bp: tuple[int, ...]
    contract_version: str = READINESS_CONTRACT_VERSION
    max_shift_bp: int = READINESS_MAX_SHIFT_BP


@dataclass(frozen=True, slots=True)
class CoverageInterval:
    scope: DirectScope
    start_utc: str
    end_utc: str
    witnesses: tuple[ReadinessWitness, ...]
    displayed: bool = False
    selectable: bool = False


@dataclass(frozen=True, slots=True)
class DirectCoverage:
    scopes: tuple[DirectScope, ...]
    rows: tuple[CoverageReviewRow, ...]
    intervals: tuple[CoverageInterval, ...]

    def __iter__(self):
        return iter(self.rows)


@dataclass(frozen=True, slots=True)
class DirectPreflight:
    usable_timeframes: Mapping[str, tuple[str, ...]]
    unavailable_symbols: Mapping[str, tuple[str, ...]]
    coverage_issues: tuple[CoverageIssue, ...]
    grid_contract: Mapping[str, object]
    source_hashes: tuple[str, ...]
    manifest: tuple[tuple[str, str], ...]
    accepted_point_keys: tuple[str, ...]
    coverage_rows: tuple[CoverageReviewRow, ...] = ()
    witnesses: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    point_evidence_sha256: str = ""
    audit_artifact_name: str = ""
    audit_schema_version: int = 1
    audit_row_count: int = 0
    audit_sha256: str = ""
    audit_bytes: bytes | None = None
    audit_size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class DirectPoint:
    canonical_point_key: str
    source_report_id: str
    source_hash: str
    point_event_count: int
    metrics: Mapping[str, int | float | None]
    event_ids: tuple[str, ...] = ()
    provenance_state: str = "REPRODUCIBLE"


@dataclass(frozen=True, slots=True)
class DirectSurface:
    request: DirectBuildRequest
    preflight: DirectPreflight
    event_mode: str
    points: tuple[DirectPoint, ...]
    parent_surface_id: str | None = None
    build_mode: str = "DUCKDB_DIRECT"


@dataclass(frozen=True, slots=True)
class DirectQueueResult:
    publication_state: str
    surfaces: tuple[PublishedSurface, ...]
    phase: str = "PUBLISHED"
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _CoverageScan:
    token: str
    coverage: DirectCoverage
    inventory_path: Path
    inventory_sha256: str
    source_evidence_sha256: str
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DirectCommonInterval:
    side: str
    start_utc: str
    end_utc: str
    scopes: tuple[DirectScope, ...]


def _verify_scan_inventory(scan: _CoverageScan) -> None:
    try:
        if not scan.inventory_path.is_file() or hashlib.sha256(scan.inventory_path.read_bytes()).hexdigest() != scan.inventory_sha256:
            raise DirectMaterializationError("STALE_PREFLIGHT")
    except OSError as error:
        raise DirectMaterializationError("STALE_PREFLIGHT") from error


def _window(request: DirectBuildRequest) -> tuple[pd.Timestamp, pd.Timestamp]:
    start, end = pd.Timestamp(request.start_utc), pd.Timestamp(request.end_utc)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    if end <= start:
        raise DirectMaterializationError("UTC end must be later than start")
    if (
        request.side not in {"LONG", "SHORT"}
        or not request.symbols
        or (not request.required_shifts_bp and request.grid_contract_kind != V2_GRID_CONTRACT_KIND)
    ):
        raise DirectMaterializationError("side, symbols and required shifts are required")
    return start, end


def _reports(source: duckdb.DuckDBPyConnection, request: DirectBuildRequest) -> list[dict[str, object]]:
    symbol_clause = " and p.symbol in (select * from unnest(?))" if request.symbols else ""
    parameters: list[object] = [request.side]
    if request.symbols:
        parameters.append(list(request.symbols))
    cursor = source.execute(
        f"""select r.report_id,r.canonical_point_key,r.source_sha256,r.raw_action_count,
                  r.equity_sample_count,r.wallet_change_count,r.grid_hash,
                  r.report_period_start_ms,r.report_period_end_ms,
                  g.sample_count,g.start_timestamp_ms,g.end_timestamp_ms,
                  p.symbol,p.side,p.timeframe,p.shift_bp,p.open_ma_len,p.close_ma_len
             from active_reports r join point_configs p using(canonical_point_key)
             join time_grids g using(grid_hash)
            where p.side=?{symbol_clause}
            order by p.symbol,p.timeframe,p.shift_bp,p.open_ma_len,p.close_ma_len,r.report_id""",
        parameters,
    )
    rows = [dict(zip((item[0] for item in cursor.description), row, strict=True)) for row in cursor.fetchall()]
    rows = [
        row for row in rows
        if not (
            int(row["report_period_start_ms"]) == int(row["report_period_end_ms"])
            and int(row["start_timestamp_ms"]) == int(row["end_timestamp_ms"])
        )
    ]
    if not request.selected_scopes:
        return rows
    selected = set(request.selected_scopes)
    return [
        row for row in rows
        if f"{row['symbol']}|{row['timeframe']}" in selected
    ]


def _coverage_scan_rows(
    source_connection: duckdb.DuckDBPyConnection, *, side: str | None, symbols: tuple[str, ...]
) -> list[dict[str, object]]:
    if side is not None and side not in {"LONG", "SHORT"}:
        raise DirectMaterializationError("side is required")
    sides = ("LONG", "SHORT") if side is None else (side,)
    all_rows: list[dict[str, object]] = []
    for current_side in sides:
        request = DirectBuildRequest("", "", current_side, symbols, (), "", "")
        all_rows.extend(_reports(source_connection, request))
    return all_rows


def _effective_window(row: Mapping[str, object]) -> tuple[int, int]:
    report_start = int(row["report_period_start_ms"])
    report_end = int(row["report_period_end_ms"])
    grid_start = int(row["start_timestamp_ms"])
    grid_end = int(row["end_timestamp_ms"])
    start = max(report_start, grid_start)
    end = min(report_end, grid_end)
    if end <= start:
        raise DirectMaterializationError(
            f"empty report/grid intersection for {row['canonical_point_key']}"
        )
    return start, end


def _merge_windows(windows: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[list[int]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return tuple((start, end) for start, end in merged)


def _utc_ms(ms: int) -> str:
    timestamp = pd.Timestamp(int(ms), unit="ms", tz="UTC")
    return timestamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{timestamp.microsecond // 1000:03d}+00:00"


def _date_only(ms: int) -> str:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").strftime("%Y-%m-%d")


def _as_utc_ms(value: str | pd.Timestamp) -> int:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return int(timestamp.value // 1_000_000)


def _canonical_json_bytes(value: object) -> bytes:
    _reject_canonical_floats(value)
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _reject_canonical_floats(value: object) -> None:
    if isinstance(value, float):
        raise ValueError("floats are forbidden in canonical JSON")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("object keys must be strings in canonical JSON")
            _reject_canonical_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_canonical_floats(item)


def canonical_point_materialization_semantic_payload(
    canonical_shifts_bp: tuple[int, ...],
) -> dict[str, object]:
    """Return the one canonical semantic contract used by direct surfaces."""
    shifts = tuple(canonical_shifts_bp)
    if (
        not shifts
        or any(isinstance(shift, bool) or not isinstance(shift, int) for shift in shifts)
        or any(left >= right for left, right in zip(shifts, shifts[1:]))
    ):
        raise ValueError("canonical shifts must be a strictly increasing integer tuple")
    return {
        "canonical_grid_version": CANONICAL_GRID_VERSION,
        "canonical_shifts_bp": list(shifts),
        "event_id_contract": "sha256_utf8_pipe(symbol,position_side,timeframe,opened_at_utc_ns)",
        "event_mode": REAL_EVENT_MODE,
        "materialization_scope_contract": "fully_covering_selected_scope_points_on_exact_canonical_shifts",
        "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION,
        "point_event_count_contract": "count_unique_sorted_canonical_event_ids",
        "readiness_contract_version": READINESS_CONTRACT_VERSION,
        "required_close_mas": list(REQUIRED_CLOSE_MAS),
        "semantic_contract_version": POINT_MATERIALIZATION_SEMANTICS_VERSION,
        "window_contract": "utc_half_open_[start,end)",
    }


def canonical_point_materialization_config_hash(canonical_shifts_bp: tuple[int, ...]) -> str:
    return hashlib.sha256(
        json.dumps(
            canonical_point_materialization_semantic_payload(canonical_shifts_bp),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def point_key_tuple(canonical_point_key: str) -> tuple[str, str, str, int, int, int]:
    parts = canonical_point_key.split("|")
    if len(parts) != 6 or any(not part for part in parts):
        raise ValueError("canonical point key must have six fields")
    symbol, side, timeframe, shift, open_ma, close_ma = parts
    try:
        shift_bp = int(shift)
        open_ma_len = int(open_ma)
        close_ma_len = int(close_ma)
    except ValueError as error:
        raise ValueError("canonical point key has non-integer grid fields") from error
    metadata = {
        "symbol": symbol,
        "side": side,
        "timeframe": timeframe,
        "shift_bp": shift_bp,
        "open_ma_len": open_ma_len,
        "close_ma_len": close_ma_len,
    }
    try:
        expected = _canonical_point_key(metadata)
    except SourceSchemaError as error:
        raise ValueError(f"canonical point key does not round-trip: {error}") from error
    if expected != canonical_point_key:
        raise ValueError("canonical point key does not round-trip")
    return symbol, side, timeframe, shift_bp, open_ma_len, close_ma_len


def _evidence_jsonl_bytes(records: Sequence[Mapping[str, object]]) -> bytes:
    normalized: list[tuple[tuple[str, str, str, int, int, int], dict[str, object]]] = []
    for record in records:
        key = point_key_tuple(str(record["point_key"]))
        normalized.append((
            key,
            {
                "point_key": str(record["point_key"]),
                "report_id": str(record["report_id"]),
                "source_sha256": str(record["source_sha256"]),
            },
        ))
    normalized.sort(key=lambda item: item[0])
    return b"".join(_canonical_json_bytes(record) + b"\n" for _, record in normalized)


def point_evidence_jsonl_bytes(points: Sequence[DirectPoint]) -> bytes:
    """Return canonical JSONL evidence sorted by decoded point-key tuple."""
    return _evidence_jsonl_bytes([
        {
            "point_key": point.canonical_point_key,
            "report_id": point.source_report_id,
            "source_sha256": point.source_hash,
        }
        for point in points
    ])


def _audit_data_row_count(audit_bytes: bytes) -> int:
    if not audit_bytes:
        return 0
    if not audit_bytes.endswith(b"\n"):
        raise DirectMaterializationError("V2 audit CSV must be LF-terminated")
    reader = csv.DictReader(io.StringIO(audit_bytes.decode("utf-8"), newline=""))
    if reader.fieldnames is None:
        raise DirectMaterializationError("V2 audit CSV must include a header row")
    return sum(1 for _ in reader)


def _verify_v2_audit_metadata(
    *,
    audit_bytes: bytes,
    artifact_name: str,
    schema_version: int,
    row_count: int,
    sha256_digest: str,
    size_bytes: int = 0,
) -> None:
    if not isinstance(audit_bytes, bytes) or not audit_bytes:
        raise DirectMaterializationError("V2 audit bytes are required")
    if not isinstance(artifact_name, str) or not artifact_name:
        raise DirectMaterializationError("V2 audit artifact name is required")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != V2_AUDIT_SCHEMA_VERSION:
        raise DirectMaterializationError("V2 audit schema version must be exactly 1")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        raise DirectMaterializationError("V2 audit row count must be a non-negative integer")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0 or size_bytes != len(audit_bytes):
        raise DirectMaterializationError("V2 audit size mismatch")
    if not isinstance(sha256_digest, str) or len(sha256_digest) != 64 or any(
        char not in "0123456789abcdef" for char in sha256_digest
    ):
        raise DirectMaterializationError("V2 audit hash must be a SHA-256 digest")
    if hashlib.sha256(audit_bytes).hexdigest() != sha256_digest:
        raise DirectMaterializationError("V2 audit hash mismatch")
    if row_count != _audit_data_row_count(audit_bytes):
        raise DirectMaterializationError("V2 audit row count mismatch")


def _evaluation_id(scope: DirectScope, start_utc: str, end_utc: str) -> str:
    payload = _canonical_json_bytes([scope.symbol, scope.side, scope.timeframe, start_utc, end_utc])
    return hashlib.sha256(payload).hexdigest()


def _window_covers(window: tuple[int, int], start_ms: int, end_ms: int) -> bool:
    return window[0] <= start_ms and window[1] >= end_ms


def _available_shifts(
    pair: tuple[int, int],
    interval: tuple[int, int],
    merged_cells: Mapping[tuple[int, int, int], tuple[tuple[int, int], ...]],
) -> set[int]:
    start_ms, end_ms = interval
    return {
        shift
        for (shift, open_ma, close_ma), windows in merged_cells.items()
        if (open_ma, close_ma) == pair and any(_window_covers(window, start_ms, end_ms) for window in windows)
    }


def _witness_vector_key(
    witnesses: Sequence[ReadinessWitness],
) -> tuple[tuple[int, int, tuple[int, ...]], ...]:
    return tuple((witness.close_ma, witness.open_ma, witness.shifts_bp) for witness in witnesses)


def _scope_witness_vector(
    rows: Sequence[dict[str, object]],
    start_ms: int,
    end_ms: int,
    required_shifts_bp: Sequence[int],
) -> tuple[ReadinessWitness, ...] | None:
    merged_cells = _scope_merged_cells(rows)
    required = set(required_shifts_bp)
    witnesses: list[ReadinessWitness] = []
    for close_ma in REQUIRED_CLOSE_MAS:
        open_mas = sorted({
            open_ma for (_, open_ma, current_close), _ in merged_cells.items()
            if current_close == close_ma
        })
        candidates = [
            open_ma for open_ma in open_mas
            if required <= _available_shifts((open_ma, close_ma), (start_ms, end_ms), merged_cells)
        ]
        if not candidates:
            return None
        witnesses.append(ReadinessWitness(candidates[0], close_ma, tuple(required_shifts_bp)))
    return tuple(witnesses)


def _v2_selected_rows(
    rows: Sequence[dict[str, object]],
    start_ms: int,
    end_ms: int,
) -> tuple[dict[str, object], ...]:
    selected_by_key: dict[str, dict[str, object]] = {}
    for row in rows:
        effective = _effective_window(row)
        if not _window_covers(effective, start_ms, end_ms):
            continue
        key = str(row["canonical_point_key"])
        current = selected_by_key.get(key)
        if current is None:
            selected_by_key[key] = row
            continue

        def sort_key(candidate: Mapping[str, object]) -> tuple[int, int, int, str]:
            window = _effective_window(candidate)
            return (window[1] - window[0], window[0], window[1], str(candidate["report_id"]))

        if sort_key(row) < sort_key(current):
            selected_by_key[key] = row
    return tuple(sorted(
        selected_by_key.values(),
        key=lambda row: point_key_tuple(str(row["canonical_point_key"])),
    ))


def _scope_merged_cells(
    rows: list[dict[str, object]]
) -> dict[tuple[int, int, int], tuple[tuple[int, int], ...]]:
    cell_windows: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
    for row in rows:
        cell = (
            int(row["shift_bp"]),
            int(row["open_ma_len"]),
            int(row["close_ma_len"]),
        )
        cell_windows.setdefault(cell, []).append(_effective_window(row))
    return {
        cell: _merge_windows(windows) for cell, windows in cell_windows.items()
    }


def _scope_factual_chains(rows: list[dict[str, object]]) -> tuple[tuple[int, int], ...]:
    return _merge_windows([
        window for windows in _scope_merged_cells(rows).values() for window in windows
    ])


def _gap_details(chains: tuple[tuple[int, int], ...]) -> tuple[str, ...]:
    return tuple(
        f"missing: {_date_only(previous[1])} .. {_date_only(current[0])}"
        for previous, current in zip(chains, chains[1:], strict=False)
    )


def _direct_coverage(all_reports: list[dict[str, object]]) -> DirectCoverage:
    scope_keys = sorted(
        {(str(row["symbol"]), str(row["side"]), str(row["timeframe"])) for row in all_reports},
        key=lambda item: (item[0], 0 if item[1] == "LONG" else 1, item[2]),
    )
    scopes = tuple(DirectScope(symbol, side, timeframe) for symbol, side, timeframe in scope_keys)
    rows: list[CoverageReviewRow] = []
    intervals: list[CoverageInterval] = []
    for scope in scopes:
        scope_rows = [
            row for row in all_reports
            if str(row["symbol"]) == scope.symbol
            and str(row["side"]) == scope.side
            and str(row["timeframe"]) == scope.timeframe
        ]
        merged_cells = _scope_merged_cells(scope_rows)
        factual_chains = _scope_factual_chains(scope_rows)
        boundaries = sorted({
            boundary
            for windows in merged_cells.values()
            for window in windows
            for boundary in window
        })
        runs: list[tuple[int, int, tuple[ReadinessWitness, ...]]] = []
        active_runs: list[tuple[int, int, tuple[ReadinessWitness, ...]]] = []
        for index in range(len(boundaries) - 1):
            segment = (boundaries[index], boundaries[index + 1])
            vector = _scope_witness_vector(
                scope_rows, segment[0], segment[1], DEFAULT_CANONICAL_SHIFTS_BP
            )
            if vector is None:
                runs.extend(active_runs)
                active_runs = []
                continue
            next_runs = []
            for start_ms, end_ms, current_vector in active_runs:
                combined = (start_ms, segment[1])
                combined_vector = _scope_witness_vector(
                    scope_rows, combined[0], combined[1], DEFAULT_CANONICAL_SHIFTS_BP
                )
                if combined_vector is not None:
                    next_runs.append((combined[0], combined[1], combined_vector))
                else:
                    runs.append((start_ms, end_ms, current_vector))
            active_runs = [*next_runs, (segment[0], segment[1], vector)]
        runs.extend(active_runs)
        ordered_candidates = sorted(
            {
                (start_ms, end_ms, vector): (start_ms, end_ms, vector)
                for start_ms, end_ms, vector in runs
            }.values(),
            key=lambda item: (
                -(item[1] - item[0]),
                item[0],
                item[1],
                _witness_vector_key(item[2]),
            ),
        )
        selectable = False
        interval_start_utc = ""
        interval_end_utc = ""
        if ordered_candidates:
            for index, (start_ms, end_ms, vector) in enumerate(ordered_candidates):
                displayed = index == 0
                intervals.append(CoverageInterval(
                    scope,
                    _utc_ms(start_ms),
                    _utc_ms(end_ms),
                    vector,
                    displayed=displayed,
                    selectable=displayed,
                ))
                if displayed:
                    selectable = True
                    interval_start_utc = _utc_ms(start_ms)
                    interval_end_utc = _utc_ms(end_ms)
        elif factual_chains:
            longest = max(factual_chains, key=lambda window: (window[1] - window[0], -window[0]))
            interval_start_utc = _utc_ms(longest[0])
            interval_end_utc = _utc_ms(longest[1])
        rows.append(CoverageReviewRow(
            scope.symbol,
            scope.side,
            scope.timeframe,
            selectable,
            interval_start_utc,
            interval_end_utc,
            _gap_details(factual_chains),
        ))
    return DirectCoverage(scopes, tuple(rows), tuple(intervals))


def canonical_coverage_from_rows(rows: Sequence[Mapping[str, object]]) -> DirectCoverage:
    """Evaluate readiness using the same canonical 6-CloseMA/19-Shift contract.

    Source-v6 adapters use this entry point with normalized report rows; keeping
    the calculation here prevents a second, subtly different readiness policy.
    """
    return _direct_coverage([dict(row) for row in rows])


def list_duckdb_direct_coverage(
    source_connection: duckdb.DuckDBPyConnection, *, side: str | None = None, symbols: tuple[str, ...] = ()
) -> DirectCoverage:
    """List readiness-gated Pair+Side+TF coverage; both sides when side is omitted."""
    coverage = _direct_coverage(_coverage_scan_rows(source_connection, side=side, symbols=symbols))
    validation = validate_source_database_structural(source_connection)
    if not validation.valid:
        raise DirectMaterializationError(f"invalid active v5 source: {validation.errors}")
    return coverage


def coverage_scan_direct(
    source_connection: duckdb.DuckDBPyConnection,
    audit_root: str | os.PathLike[str],
    *,
    symbols: tuple[str, ...] = (),
) -> _CoverageScan:
    """Scan both sides, freeze canonical inventory bytes, and return a token."""
    all_rows = _coverage_scan_rows(source_connection, side=None, symbols=symbols)
    coverage = _direct_coverage(all_rows)
    validation = validate_source_database_structural(source_connection)
    if not validation.valid:
        raise DirectMaterializationError(f"invalid active v5 source: {validation.errors}")
    inventory_bytes = coverage_inventory_csv_bytes(
        source_connection,
        symbols=symbols,
    )
    inventory_sha256 = hashlib.sha256(inventory_bytes).hexdigest()
    source_evidence = _canonical_json_bytes(tuple(sorted(
        (str(row["report_id"]), str(row["source_sha256"]))
        for row in all_rows
    )))
    source_evidence_sha256 = hashlib.sha256(source_evidence).hexdigest()
    token_document = {
        "canonical_grid_version": CANONICAL_GRID_VERSION,
        "required_shifts_bp": list(DEFAULT_CANONICAL_SHIFTS_BP),
        "readiness_contract_version": READINESS_CONTRACT_VERSION,
        "coverage_rows": tuple(
            (
                row.symbol,
                row.side,
                row.timeframe,
                row.selectable,
                row.interval_start_utc,
                row.interval_end_utc,
                row.gap_details,
            )
            for row in coverage.rows
        ),
        "witnesses": tuple(
            (
                interval.scope.symbol,
                interval.scope.side,
                interval.scope.timeframe,
                interval.start_utc,
                interval.end_utc,
                tuple(
                    (
                        witness.open_ma,
                        witness.close_ma,
                        witness.shifts_bp,
                        witness.contract_version,
                        witness.max_shift_bp,
                    )
                    for witness in interval.witnesses
                ),
            )
            for interval in coverage.intervals
        ),
        "inventory_sha256": inventory_sha256,
        "source_evidence_sha256": source_evidence_sha256,
    }
    token = hashlib.sha256(_canonical_json_bytes(token_document)).hexdigest()
    inventory_path = write_coverage_artifact(
        audit_root,
        f"surface_coverage/{token}/coverage_inventory.csv",
        inventory_bytes,
    )
    return _CoverageScan(
        token,
        coverage,
        inventory_path,
        inventory_sha256,
        source_evidence_sha256,
        tuple(sorted(symbols)),
    )


def common_intervals_for_scopes(
    coverage: DirectCoverage,
    scopes: Sequence[DirectScope],
) -> tuple[DirectCommonInterval, ...]:
    """Intersect every selected row's exact interval per side."""
    if not scopes:
        raise DirectMaterializationError("at least one selected scope is required")
    by_side: dict[str, list[tuple[DirectScope, int, int]]] = {}
    for scope in scopes:
        row = next(
            (
                item for item in coverage.rows
                if item.symbol == scope.symbol
                and item.side == scope.side
                and item.timeframe == scope.timeframe
            ),
            None,
        )
        if row is None or not row.selectable:
            raise DirectMaterializationError("selected scope is unavailable")
        by_side.setdefault(scope.side, []).append(
            (scope, _as_utc_ms(row.interval_start_utc), _as_utc_ms(row.interval_end_utc))
        )
    if not by_side:
        raise DirectMaterializationError("selected scope is unavailable")
    intervals: list[DirectCommonInterval] = []
    for side in sorted(by_side, key=lambda item: 0 if item == "LONG" else 1):
        start_ms = max(item[1] for item in by_side[side])
        end_ms = min(item[2] for item in by_side[side])
        if end_ms <= start_ms:
            raise DirectMaterializationError(f"selected common interval is empty for {side}")
        intervals.append(DirectCommonInterval(
            side,
            _utc_ms(start_ms),
            _utc_ms(end_ms),
            tuple(scope for scope, _, _ in by_side[side]),
        ))
    return tuple(intervals)


def prepare_direct_surfaces(
    source_connection: duckdb.DuckDBPyConnection,
    requests: Sequence[DirectBuildRequest],
    *,
    audit_root: str | os.PathLike[str],
    coverage_scan: _CoverageScan | None = None,
    cancellation: Callable[[], bool] = lambda: False,
    progress_callback: Callable[..., object] = lambda *args, **kwargs: None,
    materialization_settings: DirectMaterializationSettings | None = None,
) -> tuple[DirectSurface, ...]:
    """Prepare all sides in memory under one read-only source transaction."""
    if not requests:
        raise DirectMaterializationError("at least one direct request is required")
    ordered = tuple(sorted(requests, key=lambda request: 0 if request.side == "LONG" else 1))
    if len({request.side for request in ordered}) != len(ordered):
        raise DirectMaterializationError("only one request per side is allowed")
    if any(
        request.grid_contract_kind == V2_GRID_CONTRACT_KIND
        or request.materializer_version == CANONICAL_MATERIALIZER_VERSION
        for request in ordered
    ):
        raise DirectMaterializationError("STALE_PREFLIGHT")
    source_connection.execute("begin transaction")
    prepared: list[DirectSurface] = []
    try:
        if coverage_scan is not None:
            active_scan = coverage_scan_direct(
                source_connection,
                audit_root,
                symbols=coverage_scan.symbols,
            )
            if active_scan.token != coverage_scan.token:
                raise DirectMaterializationError("active coverage scan changed after preflight")
        for index, request in enumerate(ordered, start=1):
            if cancellation():
                raise DirectMaterializationError("direct build cancelled before publication")
            side = request.side
            progress_callback(
                f"PREPARING_{side}",
                side=side,
                ordinal=index,
                total=len(ordered),
            )
            audit_bytes = coverage_audit_csv_bytes(
                source_connection,
                request.start_utc,
                request.end_utc,
                side=side,
                symbols=request.symbols,
                selected_scopes=request.selected_scopes,
                required_shifts_bp=request.required_shifts_bp or DEFAULT_CANONICAL_SHIFTS_BP,
            )
            audit_sha256 = hashlib.sha256(audit_bytes).hexdigest()
            audit_artifact_name = f"surface_coverage_audit_{side}.csv"
            audit_size_bytes = len(audit_bytes)
            audit_row_count = _audit_data_row_count(audit_bytes)
            if request.audit_bytes is not None and (
                request.audit_bytes != audit_bytes
                or request.audit_sha256 != audit_sha256
                or request.audit_size_bytes != audit_size_bytes
                or request.audit_row_count != audit_row_count
                or request.audit_artifact_name != audit_artifact_name
                or request.audit_schema_version != V2_AUDIT_SCHEMA_VERSION
            ):
                raise DirectMaterializationError("STALE_PREFLIGHT")
            request = replace(
                request,
                grid_contract_kind=V2_GRID_CONTRACT_KIND,
                materializer_version=CANONICAL_MATERIALIZER_VERSION,
                point_materialization_config_hash=canonical_point_materialization_config_hash(
                    request.required_shifts_bp or DEFAULT_CANONICAL_SHIFTS_BP
                ),
                readiness_contract_version=READINESS_CONTRACT_VERSION,
                readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
                audit_artifact_name=audit_artifact_name,
                audit_schema_version=V2_AUDIT_SCHEMA_VERSION,
                audit_row_count=audit_row_count,
                audit_sha256=audit_sha256,
                audit_bytes=audit_bytes,
                audit_size_bytes=audit_size_bytes,
            )
            preflight = preflight_duckdb_direct(source_connection, request)
            if preflight.unavailable_symbols:
                raise DirectMaterializationError("selected scope is unavailable")
            write_coverage_artifact(
                audit_root,
                f"surface_coverage/{audit_sha256}/{audit_artifact_name}",
                audit_bytes,
            )
            if cancellation():
                raise DirectMaterializationError("direct build cancelled before publication")
            materialize_kwargs: dict[str, object] = {"preflight": preflight}
            if materialization_settings is not None:
                materialize_kwargs["materialization_settings"] = materialization_settings
            materialize_kwargs["progress_callback"] = progress_callback
            materialize_kwargs["progress_side"] = request.side
            surface = _materialize_direct_with_compat(
                source_connection, None, request, cancellation, materialize_kwargs
            )
            prepared.append(surface)
            progress_callback(
                f"PREPARED_{side}",
                side=side,
                materialized_points=len(surface.points),
            )
        source_connection.execute("commit")
    except BaseException:
        source_connection.execute("rollback")
        raise
    return tuple(prepared)


def freeze_direct_preflights(
    source_connection: duckdb.DuckDBPyConnection,
    requests: Sequence[DirectBuildRequest],
    *,
    audit_root: str | os.PathLike[str],
    coverage_scan: _CoverageScan,
    preflight_func: Callable[[duckdb.DuckDBPyConnection, DirectBuildRequest], DirectPreflight] | None = None,
) -> tuple[tuple[DirectBuildRequest, ...], tuple[DirectPreflight, ...]]:
    """Freeze selected side requests and preflights before materialization."""
    if not requests:
        raise DirectMaterializationError("at least one direct request is required")
    ordered = tuple(sorted(requests, key=lambda item: 0 if item.side == "LONG" else 1))
    if len({item.side for item in ordered}) != len(ordered):
        raise DirectMaterializationError("only one request per side is allowed")
    source_connection.execute("begin transaction")
    frozen_requests: list[DirectBuildRequest] = []
    frozen_preflights: list[DirectPreflight] = []
    try:
        preflight_func = preflight_func or globals()["preflight_duckdb_direct"]
        _verify_scan_inventory(coverage_scan)
        active_scan = coverage_scan_direct(
            source_connection,
            audit_root,
            symbols=coverage_scan.symbols,
        )
        if active_scan.token != coverage_scan.token:
            raise DirectMaterializationError("stale coverage token is required")
        for request in ordered:
            audit_bytes = coverage_audit_csv_bytes(
                source_connection,
                request.start_utc,
                request.end_utc,
                side=request.side,
                symbols=request.symbols,
                selected_scopes=request.selected_scopes,
                required_shifts_bp=request.required_shifts_bp or DEFAULT_CANONICAL_SHIFTS_BP,
            )
            audit_sha256 = hashlib.sha256(audit_bytes).hexdigest()
            bound = replace(
                request,
                grid_contract_kind=V2_GRID_CONTRACT_KIND,
                readiness_contract_version=READINESS_CONTRACT_VERSION,
                readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
                audit_artifact_name=f"surface_coverage_audit_{request.side}.csv",
                audit_schema_version=V2_AUDIT_SCHEMA_VERSION,
                audit_size_bytes=len(audit_bytes),
                audit_row_count=_audit_data_row_count(audit_bytes),
                audit_sha256=audit_sha256,
                audit_bytes=audit_bytes,
            )
            preflight = preflight_func(source_connection, bound)
            if preflight.unavailable_symbols:
                raise DirectMaterializationError("selected scope is unavailable")
            write_coverage_artifact(
                audit_root,
                f"surface_coverage/{audit_sha256}/{bound.audit_artifact_name}",
                audit_bytes,
            )
            frozen_requests.append(bound)
            frozen_preflights.append(preflight)
        source_connection.execute("commit")
    except BaseException:
        source_connection.execute("rollback")
        raise
    return tuple(frozen_requests), tuple(frozen_preflights)


def replay_direct_preflights(
    source_connection: duckdb.DuckDBPyConnection,
    requests: Sequence[DirectBuildRequest],
    preflights: Sequence[DirectPreflight],
    *,
    audit_root: str | os.PathLike[str],
    coverage_scan: _CoverageScan,
    cancellation: Callable[[], bool] = lambda: False,
    materialization_settings: DirectMaterializationSettings | None = None,
    progress_callback: Callable[..., object] | None = None,
) -> tuple[DirectSurface, ...]:
    """Reproduce frozen state and materialize only after exact replay succeeds."""
    ordered = tuple(sorted(zip(requests, preflights, strict=True), key=lambda item: 0 if item[0].side == "LONG" else 1))
    source_connection.execute("begin transaction")
    validated: list[tuple[DirectBuildRequest, DirectPreflight]] = []
    try:
        _verify_scan_inventory(coverage_scan)
        active_scan = coverage_scan_direct(source_connection, audit_root, symbols=coverage_scan.symbols)
        if active_scan.token != coverage_scan.token:
            raise DirectMaterializationError("STALE_PREFLIGHT")
        for request, expected in ordered:
            if cancellation():
                raise DirectMaterializationError("direct build cancelled before publication")
            try:
                audit_bytes = coverage_audit_csv_bytes(
                    source_connection,
                    request.start_utc,
                    request.end_utc,
                    side=request.side,
                    symbols=request.symbols,
                    selected_scopes=request.selected_scopes,
                    required_shifts_bp=request.required_shifts_bp,
                )
            except BaseException as error:
                raise DirectMaterializationError("STALE_PREFLIGHT") from error
            active_request = replace(
                request,
                audit_size_bytes=len(audit_bytes),
                audit_row_count=_audit_data_row_count(audit_bytes),
                audit_sha256=hashlib.sha256(audit_bytes).hexdigest(),
                audit_bytes=audit_bytes,
            )
            if active_request != request:
                raise DirectMaterializationError("STALE_PREFLIGHT")
            probe = DirectSurface(request, expected, REAL_EVENT_MODE, ())
            try:
                verify_persisted_surface_audit(audit_root, probe)
                active = preflight_duckdb_direct(source_connection, active_request)
            except BaseException as error:
                raise DirectMaterializationError("STALE_PREFLIGHT") from error
            if active != expected:
                raise DirectMaterializationError("STALE_PREFLIGHT")
            validated.append((request, expected))
        surfaces: list[DirectSurface] = []
        for request, expected in validated:
            if cancellation():
                raise DirectMaterializationError("direct build cancelled before publication")
            materialize_kwargs: dict[str, object] = {"preflight": expected}
            if materialization_settings is not None:
                materialize_kwargs["materialization_settings"] = materialization_settings
            materialize_kwargs["progress_callback"] = progress_callback
            materialize_kwargs["progress_side"] = request.side
            surfaces.append(_materialize_direct_with_compat(
                source_connection, None, request, cancellation, materialize_kwargs
            ))
        source_connection.execute("commit")
    except BaseException:
        source_connection.execute("rollback")
        raise
    return tuple(surfaces)


def publish_direct_surfaces(
    analysis_connection: duckdb.DuckDBPyConnection,
    surfaces: Sequence[DirectSurface],
    *,
    audit_root: str | os.PathLike[str] | None = None,
    cancellation: Callable[[], bool] = lambda: False,
    progress_callback: Callable[..., object] = lambda *args, **kwargs: None,
    parent_surface_id: str | None = None,
) -> DirectQueueResult:
    """Publish prepared sides separately and deterministically LONG before SHORT."""
    if not surfaces:
        raise DirectMaterializationError("at least one prepared surface is required")
    ordered = tuple(sorted(surfaces, key=lambda surface: 0 if surface.request.side == "LONG" else 1))
    if len({surface.request.side for surface in ordered}) != len(ordered):
        raise DirectMaterializationError("only one surface per side is allowed")
    if parent_surface_id is not None and len(ordered) != 1:
        raise DirectMaterializationError("parent_surface_id requires a single side")
    canonical_surfaces = tuple(
        surface for surface in ordered
        if surface.request.grid_contract_kind == V2_GRID_CONTRACT_KIND
        or (
            isinstance(getattr(surface.preflight, "grid_contract", None), Mapping)
            and surface.preflight.grid_contract.get("kind") == V2_GRID_CONTRACT_KIND
        )
    )
    if canonical_surfaces and audit_root is None:
        return DirectQueueResult("FAILED", (), phase="FAILED", error="persisted audit root is required")
    if audit_root is not None and canonical_surfaces:
        try:
            for surface in canonical_surfaces:
                verify_persisted_surface_audit(audit_root, surface)
        except BaseException as error:
            message = str(error) if isinstance(error, DirectMaterializationError) else "direct build failed"
            return DirectQueueResult("FAILED", (), phase="FAILED", error=message)
    published: list[PublishedSurface] = []
    for index, surface in enumerate(ordered, start=1):
        if cancellation():
            state = "PARTIAL" if published else "CANCELLED"
            return DirectQueueResult(state, tuple(published), phase=state)
        side = surface.request.side
        progress_callback(
            f"PUBLISHING_{side}",
            side=side,
            ordinal=index,
            total=len(ordered),
        )
        try:
            if audit_root is not None and surface in canonical_surfaces:
                verify_persisted_surface_audit(audit_root, surface)
            published_surface = publish_surface(analysis_connection, surface)
        except BaseException as error:
            state = "PARTIAL" if published else "FAILED"
            message = str(error) if isinstance(error, DirectMaterializationError) else "direct build failed"
            return DirectQueueResult(state, tuple(published), phase=state, error=message)
        published.append(published_surface)
    return DirectQueueResult("PUBLISHED", tuple(published), phase="PUBLISHED")


def _evaluation_rows_for_scope(
    rows: list[dict[str, object]],
    scope: DirectScope,
    interval_start_utc: str,
    interval_end_utc: str,
    displayed_interval: bool,
    required_shifts_bp: Sequence[int],
) -> list[dict[str, object]]:
    start_ms = _as_utc_ms(interval_start_utc)
    end_ms = _as_utc_ms(interval_end_utc)
    if end_ms <= start_ms:
        raise DirectMaterializationError("UTC end must be later than start")
    normalized_start_utc = _utc_ms(start_ms)
    normalized_end_utc = _utc_ms(end_ms)
    cell_windows: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
    for row in rows:
        cell = (
            int(row["shift_bp"]),
            int(row["open_ma_len"]),
            int(row["close_ma_len"]),
        )
        cell_windows.setdefault(cell, []).append(_effective_window(row))
    merged_cells = {
        cell: _merge_windows(windows) for cell, windows in cell_windows.items()
    }
    required = set(required_shifts_bp)
    pairs = sorted({(open_ma, close_ma) for _, open_ma, close_ma in merged_cells})
    pair_witness: dict[tuple[int, int], ReadinessWitness | None] = {}
    pair_missing: dict[tuple[int, int], tuple[int, ...]] = {}
    for pair in pairs:
        available = _available_shifts(pair, (start_ms, end_ms), merged_cells)
        missing = tuple(sorted(required - available))
        pair_missing[pair] = missing
        pair_witness[pair] = (
            None if missing else ReadinessWitness(pair[0], pair[1], tuple(required_shifts_bp))
        )
    close_witness: dict[int, ReadinessWitness] = {}
    for close_ma in REQUIRED_CLOSE_MAS:
        passing = [
            witness for pair, witness in pair_witness.items()
            if witness is not None and witness.close_ma == close_ma
        ]
        if passing:
            close_witness[close_ma] = min(passing, key=lambda item: (item.open_ma, item.shifts_bp))
    canonical_pairs = {(witness.open_ma, witness.close_ma) for witness in close_witness.values()}
    canonical_shifts = required
    covering_by_cell: dict[tuple[int, int, int], list[dict[str, object]]] = {}
    for row in rows:
        effective = _effective_window(row)
        if _window_covers(effective, start_ms, end_ms):
            cell = (
                int(row["shift_bp"]),
                int(row["open_ma_len"]),
                int(row["close_ma_len"]),
            )
            covering_by_cell.setdefault(cell, []).append(row)
    selected_by_cell: dict[tuple[int, int, int], dict[str, object]] = {}
    for cell, candidates in covering_by_cell.items():
        selected_by_cell[cell] = min(
            candidates,
            key=lambda row: (
                int(_effective_window(row)[1]) - int(_effective_window(row)[0]),
                int(_effective_window(row)[0]),
                int(_effective_window(row)[1]),
                str(row["report_id"]),
            ),
        )
    evaluation_id = _evaluation_id(scope, normalized_start_utc, normalized_end_utc)
    evaluations: list[dict[str, object]] = []
    for row in rows:
        effective = _effective_window(row)
        cell = (
            int(row["shift_bp"]),
            int(row["open_ma_len"]),
            int(row["close_ma_len"]),
        )
        pair = (int(row["open_ma_len"]), int(row["close_ma_len"]))
        covers = _window_covers(effective, start_ms, end_ms)
        if covers:
            selected = selected_by_cell[cell]
            is_selected = str(row["report_id"]) == str(selected["report_id"])
            status = "AVAILABLE" if is_selected else "EXCLUDED"
            reason_code = "AVAILABLE" if is_selected else "OVERLAP_NOT_SELECTED"
            reason_detail = (
                "AVAILABLE: selected_report=true"
                if is_selected else "OVERLAP_NOT_SELECTED: selected_by_tiebreak=true"
            )
        else:
            is_selected = False
            status = "EXCLUDED"
            reason_code = "INTERVAL_NOT_COVERED"
            reason_detail = (
                f"INTERVAL_NOT_COVERED: effective_start_utc={_utc_ms(effective[0])}, "
                f"effective_end_utc={_utc_ms(effective[1])}"
            )
        witness = pair_witness[pair]
        required_for_readiness = (
            pair in canonical_pairs
            and witness is not None
            and int(row["shift_bp"]) in canonical_shifts
        )
        evaluations.append({
            "pair": scope.symbol,
            "side": scope.side,
            "timeframe": scope.timeframe,
            "evaluation_id": evaluation_id,
            "displayed_interval": displayed_interval,
            "row_type": "POINT_CANDIDATE",
            "shift_bp": int(row["shift_bp"]),
            "open_ma": int(row["open_ma_len"]),
            "close_ma": int(row["close_ma_len"]),
            "interval_start_utc": normalized_start_utc,
            "interval_end_utc": normalized_end_utc,
            "report_start_utc": _utc_ms(int(row["report_period_start_ms"])),
            "report_end_utc": _utc_ms(int(row["report_period_end_ms"])),
            "grid_start_utc": _utc_ms(int(row["start_timestamp_ms"])),
            "grid_end_utc": _utc_ms(int(row["end_timestamp_ms"])),
            "effective_start_utc": _utc_ms(effective[0]),
            "effective_end_utc": _utc_ms(effective[1]),
            "required_for_readiness": required_for_readiness,
            "readiness_witness": ",".join(map(str, witness.shifts_bp)) if witness is not None else "",
            "gap_start_bp": None,
            "gap_end_bp": None,
            "max_gap_bp": None,
            "report_id": str(row["report_id"]),
            "source_sha256": str(row["source_sha256"]),
            "selected_report": is_selected,
            "status": status,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
            "readiness_contract_version": READINESS_CONTRACT_VERSION,
            "readiness_max_shift_bp": READINESS_MAX_SHIFT_BP,
        })
    for pair in pairs:
        missing = pair_missing[pair]
        if not missing or pair[1] in close_witness:
            continue
        evaluations.append({
            "pair": scope.symbol,
            "side": scope.side,
            "timeframe": scope.timeframe,
            "evaluation_id": evaluation_id,
            "displayed_interval": displayed_interval,
            "row_type": "READINESS_GAP",
            "shift_bp": None,
            "open_ma": pair[0],
            "close_ma": pair[1],
            "interval_start_utc": normalized_start_utc,
            "interval_end_utc": normalized_end_utc,
            "report_start_utc": None,
            "report_end_utc": None,
            "grid_start_utc": None,
            "grid_end_utc": None,
            "effective_start_utc": None,
            "effective_end_utc": None,
            "required_for_readiness": True,
            "readiness_witness": "",
            "gap_start_bp": None,
            "gap_end_bp": None,
            "max_gap_bp": None,
            "report_id": None,
            "source_sha256": None,
            "selected_report": None,
            "status": "MISSING",
            "reason_code": "MISSING_SHIFT",
            "reason_detail": "MISSING_SHIFT: missing_shifts=" + ",".join(map(str, missing)),
            "readiness_contract_version": READINESS_CONTRACT_VERSION,
            "readiness_max_shift_bp": READINESS_MAX_SHIFT_BP,
        })
    return evaluations


def _coverage_csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _nullable_sort_key(value: object) -> tuple[int, int]:
    return (0, 0) if value is None else (1, int(value))


def _csv_sort_key(row: Mapping[str, object]) -> tuple[object, ...]:
    return (
        str(row.get("pair") or ""),
        0 if str(row.get("side") or "") == "LONG" else 1,
        str(row.get("timeframe") or ""),
        str(row.get("interval_start_utc") or ""),
        str(row.get("interval_end_utc") or ""),
        str(row.get("evaluation_id") or ""),
        str(row.get("row_type") or ""),
        int(row.get("open_ma") or 0),
        int(row.get("close_ma") or 0),
        _nullable_sort_key(row.get("shift_bp")),
        _nullable_sort_key(row.get("gap_start_bp")),
        _nullable_sort_key(row.get("gap_end_bp")),
        _nullable_sort_key(row.get("max_gap_bp")),
        str(row.get("report_id") or ""),
    )


def coverage_csv_bytes(evaluations: list[dict[str, object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=COVERAGE_CSV_COLUMNS,
        lineterminator="\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()
    for evaluation in sorted(evaluations, key=_csv_sort_key):
        writer.writerow({
            column: _coverage_csv_value(evaluation.get(column)) for column in COVERAGE_CSV_COLUMNS
        })
    return buffer.getvalue().encode("utf-8")


def coverage_inventory_csv_bytes(
    source_connection: duckdb.DuckDBPyConnection,
    *,
    side: str | None = None,
    symbols: tuple[str, ...] = (),
) -> bytes:
    all_rows = _coverage_scan_rows(source_connection, side=side, symbols=symbols)
    coverage = _direct_coverage(all_rows)
    validation = validate_source_database_structural(source_connection)
    if not validation.valid:
        raise DirectMaterializationError(f"invalid active v5 source: {validation.errors}")
    evaluations: list[dict[str, object]] = []
    for scope in coverage.scopes:
        scope_rows = [
            row for row in all_rows
            if str(row["symbol"]) == scope.symbol
            and str(row["side"]) == scope.side
            and str(row["timeframe"]) == scope.timeframe
        ]
        scope_intervals = [interval for interval in coverage.intervals if interval.scope == scope]
        if scope_intervals:
            for interval in scope_intervals:
                evaluations.extend(_evaluation_rows_for_scope(
                    scope_rows,
                    scope,
                    interval.start_utc,
                    interval.end_utc,
                    interval.displayed,
                    DEFAULT_CANONICAL_SHIFTS_BP,
                ))
            continue
        factual_chains = _scope_factual_chains(scope_rows)
        if factual_chains:
            longest = max(factual_chains, key=lambda window: (window[1] - window[0], -window[0]))
            for start_ms, end_ms in factual_chains:
                evaluations.extend(_evaluation_rows_for_scope(
                    scope_rows,
                    scope,
                    _utc_ms(start_ms),
                    _utc_ms(end_ms),
                    (start_ms, end_ms) == longest,
                    DEFAULT_CANONICAL_SHIFTS_BP,
                ))
    return coverage_csv_bytes(evaluations)


def coverage_audit_csv_bytes(
    source_connection: duckdb.DuckDBPyConnection,
    interval_start_utc: str,
    interval_end_utc: str,
    *,
    side: str | None = None,
    symbols: tuple[str, ...] = (),
    selected_scopes: tuple[str, ...] = (),
    required_shifts_bp: Sequence[int] | None = None,
) -> bytes:
    all_rows = _coverage_scan_rows(source_connection, side=side, symbols=symbols)
    coverage = _direct_coverage(all_rows)
    validation = validate_source_database_structural(source_connection)
    if not validation.valid:
        raise DirectMaterializationError(f"invalid active v5 source: {validation.errors}")
    evaluations: list[dict[str, object]] = []
    scopes = coverage.scopes
    if selected_scopes:
        selected = set(selected_scopes)
        scopes = tuple(
            scope for scope in coverage.scopes
            if (side is None or scope.side == side)
            and f"{scope.symbol}|{scope.timeframe}" in selected
        )
    for scope in scopes:
        scope_rows = [
            row for row in all_rows
            if str(row["symbol"]) == scope.symbol
            and str(row["side"]) == scope.side
            and str(row["timeframe"]) == scope.timeframe
        ]
        evaluations.extend(_evaluation_rows_for_scope(
            scope_rows,
            scope,
            interval_start_utc,
            interval_end_utc,
            True,
            required_shifts_bp or DEFAULT_CANONICAL_SHIFTS_BP,
        ))
    return coverage_csv_bytes(evaluations)


def write_coverage_artifact(audit_root: str | os.PathLike[str], relative_name: str, data: bytes | str) -> Path:
    root = Path(audit_root)
    relative = Path(relative_name)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise DirectMaterializationError("coverage artifact name must be relative")
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    readback = target.read_bytes()
    if readback != payload or hashlib.sha256(readback).hexdigest() != hashlib.sha256(payload).hexdigest():
        raise DirectMaterializationError("coverage artifact hash verification failed")
    return target


def verify_persisted_surface_audit(
    audit_root: str | os.PathLike[str],
    surface: DirectSurface,
) -> bytes:
    """Read-only verification of the frozen side audit immediately before publish."""
    request, preflight = surface.request, surface.preflight
    contract = getattr(preflight, "grid_contract", None)
    if not isinstance(contract, Mapping) or contract.get("kind") != V2_GRID_CONTRACT_KIND:
        if request.grid_contract_kind == V2_GRID_CONTRACT_KIND:
            raise DirectMaterializationError("V2 preflight contract is required")
        return b""
    if contract.get("canonical_grid_version") != CANONICAL_GRID_VERSION:
        raise DirectMaterializationError("V2 canonical grid version is invalid")
    canonical_shifts = contract.get("canonical_shifts_bp")
    if not isinstance(canonical_shifts, (list, tuple)) or tuple(canonical_shifts) != tuple(request.required_shifts_bp):
        raise DirectMaterializationError("V2 canonical shifts are invalid")
    if contract.get("point_materialization_semantics_version") != POINT_MATERIALIZATION_SEMANTICS_VERSION:
        raise DirectMaterializationError("V2 point materialization semantics version is invalid")
    if contract.get("point_materialization_config_hash") != request.point_materialization_config_hash:
        raise DirectMaterializationError("V2 point materialization config hash is invalid")
    scopes = contract.get("selected_scopes")
    witnesses = contract.get("witnesses")
    if not isinstance(scopes, (list, tuple)) or not scopes or not isinstance(witnesses, Mapping) or set(witnesses) != set(scopes):
        raise DirectMaterializationError("V2 readiness witnesses are invalid")
    witness_keys = {"symbol", "side", "timeframe", "open_ma", "close_ma", "shifts_bp", "contract_version", "max_shift_bp"}
    for scope in scopes:
        vector = witnesses[scope]
        if not isinstance(vector, list) or len(vector) != len(REQUIRED_CLOSE_MAS):
            raise DirectMaterializationError("V2 readiness witness vector must contain six entries")
        if any(not isinstance(item, Mapping) or set(item) != witness_keys for item in vector):
            raise DirectMaterializationError("V2 readiness witness is malformed")
        if [item["close_ma"] for item in vector] != list(REQUIRED_CLOSE_MAS):
            raise DirectMaterializationError("V2 readiness witnesses must be ordered CloseMA 2..7")
        expected_symbol, expected_timeframe = str(scope).split("|", maxsplit=1)
        for item in vector:
            if (item["symbol"], item["timeframe"], item["side"]) != (expected_symbol, expected_timeframe, request.side):
                raise DirectMaterializationError("V2 readiness witness scope is invalid")
            if item["shifts_bp"] != list(request.required_shifts_bp):
                raise DirectMaterializationError("V2 readiness witness shifts are invalid")
            if item["contract_version"] != READINESS_CONTRACT_VERSION or item["max_shift_bp"] != READINESS_MAX_SHIFT_BP:
                raise DirectMaterializationError("V2 readiness witness contract is invalid")
    artifact_name = request.audit_artifact_name
    expected_name = f"surface_coverage_audit_{request.side}.csv"
    if artifact_name != expected_name or preflight.audit_artifact_name != artifact_name:
        raise DirectMaterializationError("persisted audit artifact name mismatch")
    schema_version = request.audit_schema_version
    if schema_version != V2_AUDIT_SCHEMA_VERSION or preflight.audit_schema_version != schema_version:
        raise DirectMaterializationError("persisted audit schema version mismatch")
    audit_sha256 = request.audit_sha256
    if not audit_sha256 or preflight.audit_sha256 != audit_sha256:
        raise DirectMaterializationError("persisted audit hash metadata mismatch")
    expected_bytes = request.audit_bytes
    if not isinstance(expected_bytes, bytes) or preflight.audit_bytes != expected_bytes:
        raise DirectMaterializationError("persisted audit bytes are not frozen")
    if not isinstance(request.audit_size_bytes, int) or isinstance(request.audit_size_bytes, bool) or request.audit_size_bytes <= 0:
        raise DirectMaterializationError("persisted audit size metadata is missing")
    expected_size = request.audit_size_bytes
    if (
        preflight.audit_size_bytes != expected_size
    ):
        raise DirectMaterializationError("persisted audit size metadata mismatch")
    expected_rows = request.audit_row_count
    if preflight.audit_row_count != expected_rows:
        raise DirectMaterializationError("persisted audit row metadata mismatch")
    path = Path(audit_root) / "surface_coverage" / audit_sha256 / artifact_name
    try:
        if not path.is_file():
            raise DirectMaterializationError("persisted audit artifact is unavailable")
        data = path.read_bytes()
    except OSError as error:
        raise DirectMaterializationError("persisted audit artifact is unavailable") from error
    if len(data) != expected_size:
        raise DirectMaterializationError("persisted audit byte-size mismatch")
    if hashlib.sha256(data).hexdigest() != audit_sha256:
        raise DirectMaterializationError("persisted audit hash mismatch")
    if data != expected_bytes:
        raise DirectMaterializationError("persisted audit bytes mismatch")
    if not data.endswith(b"\n") or _audit_data_row_count(data) != expected_rows:
        raise DirectMaterializationError("persisted audit row count mismatch")
    for key, value in (
        ("audit_artifact_name", artifact_name),
        ("audit_schema_version", schema_version),
        ("audit_size_bytes", expected_size),
        ("audit_row_count", expected_rows),
        ("audit_sha256", audit_sha256),
    ):
        contract_value = preflight.grid_contract.get(key)
        if contract_value != value:
            raise DirectMaterializationError("persisted audit metadata mismatch")
    return data


def _validate_canonical_v2_request(request: DirectBuildRequest) -> None:
    if request.grid_contract_kind != V2_GRID_CONTRACT_KIND and request.materializer_version != CANONICAL_MATERIALIZER_VERSION:
        return
    if request.materializer_version != CANONICAL_MATERIALIZER_VERSION:
        raise DirectMaterializationError("V2 request must use the canonical materializer version")
    if tuple(request.required_shifts_bp) != DEFAULT_CANONICAL_SHIFTS_BP:
        raise DirectMaterializationError("V2 request must use the exact canonical shift tuple")
    if request.point_materialization_config_hash != canonical_point_materialization_config_hash(DEFAULT_CANONICAL_SHIFTS_BP):
        raise DirectMaterializationError("V2 request has an invalid point materialization config hash")


def _preflight_duckdb_direct_v2(
    source_connection: duckdb.DuckDBPyConnection,
    request: DirectBuildRequest,
    start_ms: int,
    end_ms: int,
    all_reports: list[dict[str, object]],
) -> DirectPreflight:
    _validate_canonical_v2_request(request)
    if request.readiness_contract_version != READINESS_CONTRACT_VERSION or request.readiness_max_shift_bp != READINESS_MAX_SHIFT_BP:
        raise DirectMaterializationError("V2 readiness contract is not active")
    _verify_v2_audit_metadata(
        audit_bytes=request.audit_bytes,
        artifact_name=request.audit_artifact_name,
        schema_version=request.audit_schema_version,
        row_count=request.audit_row_count,
        sha256_digest=request.audit_sha256,
        size_bytes=request.audit_size_bytes,
    )
    scope_rows: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in all_reports:
        scope_rows.setdefault((str(row["symbol"]), str(row["timeframe"])), []).append(row)
    if request.selected_scopes:
        selected_scopes = tuple(sorted(set(request.selected_scopes)))
    else:
        selected_scopes = tuple(sorted({
            f"{symbol}|{timeframe}"
            for symbol, timeframe in scope_rows
            if symbol in request.symbols
        }))
    if not selected_scopes:
        raise DirectMaterializationError("V2 selected scopes are required")
    for scope in selected_scopes:
        if scope.count("|") != 1:
            raise DirectMaterializationError("V2 selected scope must be symbol|timeframe")

    required_shifts_bp = request.required_shifts_bp or DEFAULT_CANONICAL_SHIFTS_BP
    required = set(required_shifts_bp)
    witnesses: dict[str, list[dict[str, object]]] = {}
    accepted_rows: list[dict[str, object]] = []
    issues: list[CoverageIssue] = []
    usable: dict[str, list[str]] = {}
    for scope in selected_scopes:
        symbol, timeframe = scope.split("|", maxsplit=1)
        rows = scope_rows.get((symbol, timeframe), [])
        if not rows:
            issues.append(CoverageIssue(symbol, timeframe, "GRID_NOT_COVERED", "no report covers the selected scope"))
            continue
        witness_vector = _scope_witness_vector(rows, start_ms, end_ms, required_shifts_bp)
        if witness_vector is None:
            issues.append(CoverageIssue(symbol, timeframe, "GRID_NOT_READY", "readiness contract is not satisfied"))
            continue
        selected = [
            row for row in _v2_selected_rows(rows, start_ms, end_ms)
            if int(row["shift_bp"]) in required
        ]
        if not selected:
            issues.append(CoverageIssue(symbol, timeframe, "GRID_NOT_COVERED", "no report covers the requested UTC half-open window"))
            continue
        witnesses[scope] = [
            {
                "symbol": symbol,
                "side": request.side,
                "timeframe": timeframe,
                "open_ma": witness.open_ma,
                "close_ma": witness.close_ma,
                "shifts_bp": list(witness.shifts_bp),
                "contract_version": witness.contract_version,
                "max_shift_bp": witness.max_shift_bp,
            }
            for witness in witness_vector
        ]
        accepted_rows.extend(selected)
        usable.setdefault(symbol, []).append(timeframe)

    accepted_rows.sort(key=lambda row: point_key_tuple(str(row["canonical_point_key"])))
    evidence_bytes = _evidence_jsonl_bytes([
        {
            "point_key": str(row["canonical_point_key"]),
            "report_id": str(row["report_id"]),
            "source_sha256": str(row["source_sha256"]),
        }
        for row in accepted_rows
    ])
    grid_contract = {
        "kind": V2_GRID_CONTRACT_KIND,
        "canonical_grid_version": CANONICAL_GRID_VERSION,
        "canonical_shifts_bp": list(required_shifts_bp),
        "selected_scopes": list(selected_scopes),
        "readiness_contract_version": request.readiness_contract_version,
        "readiness_max_shift_bp": request.readiness_max_shift_bp,
        "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION,
        "materializer_version": request.materializer_version,
        "point_materialization_semantics_version": POINT_MATERIALIZATION_SEMANTICS_VERSION,
        "point_materialization_config_hash": request.point_materialization_config_hash,
        "witnesses": {scope: witnesses[scope] for scope in selected_scopes if scope in witnesses},
        "point_evidence": evidence_bytes.decode("utf-8"),
        "point_evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "audit_artifact_name": request.audit_artifact_name,
        "audit_schema_version": request.audit_schema_version,
        "audit_size_bytes": request.audit_size_bytes,
        "audit_row_count": request.audit_row_count,
        "audit_sha256": request.audit_sha256,
    }
    unavailable: dict[str, tuple[str, ...]] = {}
    for scope in selected_scopes:
        symbol, timeframe = scope.split("|", maxsplit=1)
        if timeframe not in usable.get(symbol, ()):
            unavailable.setdefault(symbol, []).append(timeframe)
    unavailable = {
        symbol: tuple(sorted(timeframes))
        for symbol, timeframes in sorted(unavailable.items())
    }
    return DirectPreflight(
        {symbol: tuple(sorted(timeframes)) for symbol, timeframes in sorted(usable.items())},
        unavailable,
        tuple(issues),
        MappingProxyType(grid_contract),
        tuple(sorted({str(row["source_sha256"]) for row in accepted_rows})),
        tuple((str(row["report_id"]), str(row["source_sha256"])) for row in accepted_rows),
        tuple(str(row["canonical_point_key"]) for row in accepted_rows),
        _direct_coverage(all_reports).rows,
        witnesses=MappingProxyType(witnesses),
        point_evidence_sha256=grid_contract["point_evidence_sha256"],
        audit_artifact_name=request.audit_artifact_name,
        audit_schema_version=request.audit_schema_version,
        audit_size_bytes=request.audit_size_bytes,
        audit_row_count=request.audit_row_count,
        audit_sha256=request.audit_sha256,
        audit_bytes=request.audit_bytes,
    )


def preflight_duckdb_direct(source_connection: duckdb.DuckDBPyConnection, request: DirectBuildRequest) -> DirectPreflight:
    """Validate full UTC coverage and observed-grid completeness without decoding payloads."""
    _validate_canonical_v2_request(request)
    start, end = _window(request)
    validation = validate_source_database_structural(source_connection)
    if not validation.valid:
        raise DirectMaterializationError(f"invalid active v5 source: {validation.errors}")
    start_ms, end_ms = start.value // 1_000_000, end.value // 1_000_000
    all_reports = _reports(source_connection, request)
    if request.grid_contract_kind == V2_GRID_CONTRACT_KIND:
        return _preflight_duckdb_direct_v2(
            source_connection,
            request,
            start_ms,
            end_ms,
            all_reports,
        )
    covered_candidates = [
        row for row in all_reports
        if int(row["sample_count"]) > 0
        and int(row["start_timestamp_ms"]) <= start_ms
        and int(row["end_timestamp_ms"]) >= end_ms
    ]
    covered: list[dict[str, object]] = []
    overlaps: dict[tuple[str, str], int] = {}
    by_point: dict[str, list[dict[str, object]]] = {}
    for row in covered_candidates:
        by_point.setdefault(str(row["canonical_point_key"]), []).append(row)
    for point_key, candidates in by_point.items():
        # Prefer the smallest report window that still covers the requested window.
        selected = min(
            candidates,
            key=lambda row: (
                int(row["end_timestamp_ms"]) - int(row["start_timestamp_ms"]),
                int(row["start_timestamp_ms"]),
                int(row["end_timestamp_ms"]),
                str(row["report_id"]),
            ),
        )
        covered.append(selected)
        if len(candidates) > 1:
            scope = (str(selected["symbol"]), str(selected["timeframe"]))
            overlaps[scope] = overlaps.get(scope, 0) + 1
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
        overlap_count = overlaps.get((symbol, timeframe), 0)
        if overlap_count:
            issues.append(CoverageIssue(symbol, timeframe, "OVERLAPPING_REPORTS_RESOLVED", f"selected narrowest covering report for {overlap_count} cells"))
        by_shift: dict[int, set[tuple[int, int]]] = {}
        for row in rows:
            by_shift.setdefault(int(row["shift_bp"]), set()).add((int(row["open_ma_len"]), int(row["close_ma_len"])))
        shift_pairs = [by_shift.get(shift, set()) for shift in required]
        observed = set().union(*shift_pairs)
        complete = set.intersection(*shift_pairs) if shift_pairs else set()
        if not observed:
            issues.append(CoverageIssue(symbol, timeframe, "GRID_NOT_COVERED", "no report covers the requested UTC half-open window"))
        elif not complete:
            issues.append(CoverageIssue(symbol, timeframe, "GRID_NO_COMMON_PAIRS", "no MA pair is present at every required shift"))
        else:
            for open_ma, close_ma in sorted(observed - complete):
                missing_shifts = [str(shift) for shift in sorted(required) if (open_ma, close_ma) not in by_shift.get(shift, set())]
                issues.append(CoverageIssue(symbol, timeframe, "EXCLUDED_INCOMPLETE_PAIR", f"pair={open_ma}|{close_ma}; missing_shifts={','.join(missing_shifts)}"))
            keys = {str(row["canonical_point_key"]) for row in rows if int(row["shift_bp"]) in required and (int(row["open_ma_len"]), int(row["close_ma_len"])) in complete}
            if len(keys) != len([row for row in rows if int(row["shift_bp"]) in required and (int(row["open_ma_len"]), int(row["close_ma_len"])) in complete]):
                issues.append(CoverageIssue(symbol, timeframe, "CONFLICTING_CANONICAL_POINT", "multiple active reports map to one canonical point"))
                continue
            usable.setdefault(symbol, []).append(timeframe)
            accepted.extend(row for row in rows if str(row["canonical_point_key"]) in keys)
            contract_pairs.update(f"{shift}|{open_ma}|{close_ma}" for shift in required for open_ma, close_ma in complete)
    if request.selected_scopes:
        selected_scopes = {
            tuple(scope.split("|", maxsplit=1)) for scope in request.selected_scopes
        }
        unavailable = {
            symbol: tuple(sorted(timeframe for item_symbol, timeframe in selected_scopes if item_symbol == symbol and timeframe not in usable.get(symbol, ())))
            for symbol in sorted({item[0] for item in selected_scopes})
        }
        unavailable = {symbol: timeframes for symbol, timeframes in unavailable.items() if timeframes}
    else:
        unavailable = {symbol: tuple(sorted(tf for sym, tf in discovered_timeframes if sym == symbol)) for symbol in request.symbols if symbol not in usable}
    accepted.sort(key=lambda row: str(row["canonical_point_key"]))
    return DirectPreflight(
        {symbol: tuple(sorted(timeframes)) for symbol, timeframes in sorted(usable.items())}, unavailable,
        tuple(issues), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT", "required_shifts_bp": tuple(sorted(required)), "pairs": tuple(sorted(contract_pairs)), "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION}),
        tuple(sorted(str(row["source_sha256"]) for row in accepted)),
        tuple((str(row["report_id"]), str(row["source_sha256"])) for row in accepted),
        tuple(str(row["canonical_point_key"]) for row in accepted),
        _direct_coverage(all_reports).rows,
    )


@dataclass(frozen=True, slots=True)
class MaterializationPayload:
    """Pickle-safe primitive payload for one preflight-accepted report."""

    canonical_point_key: str
    report_id: str
    source_hash: str
    action_count: int
    equity_count: int
    wallet_count: int
    series_codec: str
    actions_blob: bytes
    equity_blob: bytes
    wallet_blob: bytes
    grid_hash: str
    sample_count: int
    timestamps_blob: bytes


def _materialize_payload_chunk(
    chunk: tuple[MaterializationPayload, ...],
    window_start_utc: str,
    window_end_utc: str,
) -> tuple[DirectPoint, ...]:
    """Decode one chunk of pickle-safe payloads without touching DuckDB."""
    grids_by_hash: dict[str, pd.DatetimeIndex] = {}
    points: list[DirectPoint] = []
    for payload in chunk:
        try:
            grid = grids_by_hash.get(payload.grid_hash)
            if grid is None:
                grid = pd.to_datetime(
                    decode_compact_deltas(bytes(payload.timestamps_blob), int(payload.sample_count), codec=str(payload.series_codec)),
                    unit="ms",
                    utc=True,
                )
                grids_by_hash[payload.grid_hash] = grid
            actions = decode_compact_actions(bytes(payload.actions_blob), int(payload.action_count))
            metrics = calculate_point_metrics(
                grid,
                decode_compact_deltas(bytes(payload.equity_blob), int(payload.equity_count), codec=str(payload.series_codec)),
                decode_wallet_changes(bytes(payload.wallet_blob), int(payload.wallet_count), codec=str(payload.series_codec)),
                actions,
                window_start_utc,
                window_end_utc,
            )
            symbol, _, timeframe, *_ = str(payload.canonical_point_key).split("|")
            reconstruction = reconstruct_closed_cycles(
                str(payload.report_id), symbol, timeframe, actions, window_start_utc, window_end_utc
            )
            event_ids = tuple(sorted({cycle.event_id for cycle in reconstruction.included}))
        except SourcePackError as error:
            raise DirectMaterializationError(f"cannot materialize {payload.canonical_point_key}: {error}") from error
        points.append(
            DirectPoint(
                str(payload.canonical_point_key),
                payload.report_id,
                payload.source_hash,
                len(event_ids),
                metrics,
                event_ids,
            )
        )
    return tuple(points)


_BULK_MATERIALIZATION_SQL = 'select r.canonical_point_key,r.report_id,r.source_sha256,r.raw_action_count,r.equity_sample_count,r.wallet_change_count,p.series_codec,p.actions_zlib,p.equity_zlib,p.wallet_zlib,g.grid_hash,g.sample_count,g.timestamps_zlib from active_reports r join report_payloads p using(report_id) join time_grids g using(grid_hash) where r.report_id in (select * from unnest(?))'


def _fetch_materialization_payload_batch(
    source_connection: duckdb.DuckDBPyConnection,
    manifest_batch: tuple[tuple[str, str], ...],
) -> tuple[MaterializationPayload, ...]:
    """Fetch one bulk payload batch for preflight-accepted reports."""
    report_ids = [report_id for report_id, _ in manifest_batch]
    rows = source_connection.execute(_BULK_MATERIALIZATION_SQL, [report_ids]).fetchall()
    rows_by_id: dict[str, tuple[object, ...]] = {}
    for row in rows:
        row_report_id = str(row[1])
        if row_report_id in rows_by_id:
            raise DirectMaterializationError("active source changed after preflight")
        rows_by_id[row_report_id] = row
    payloads: list[MaterializationPayload] = []
    for report_id, source_hash in manifest_batch:
        row = rows_by_id.get(report_id)
        if row is None or str(row[2]) != source_hash:
            raise DirectMaterializationError("active source changed after preflight")
        payloads.append(
            MaterializationPayload(
                canonical_point_key=str(row[0]),
                report_id=str(row[1]),
                source_hash=str(row[2]),
                action_count=int(row[3]),
                equity_count=int(row[4]),
                wallet_count=int(row[5]),
                series_codec=str(row[6]),
                actions_blob=bytes(row[7]),
                equity_blob=bytes(row[8]),
                wallet_blob=bytes(row[9]),
                grid_hash=str(row[10]),
                sample_count=int(row[11]),
                timestamps_blob=bytes(row[12]),
            )
        )
    return tuple(payloads)


def _materialize_payloads_parallel(
    payloads: Sequence[MaterializationPayload],
    settings: DirectMaterializationSettings,
    window_start_utc: str,
    window_end_utc: str,
    cancellation: Callable[[], bool],
    *,
    progress_callback: Callable[..., object] | None = None,
    progress_offset: int = 0,
    progress_total: int | None = None,
    progress_started_at: float | None = None,
    progress_side: str | None = None,
) -> tuple[DirectPoint, ...]:
    """Materialize payload chunks with completion-driven bounded scheduling."""
    chunk_size = settings.worker_chunk_size
    chunks = tuple(
        tuple(payloads[offset : offset + chunk_size])
        for offset in range(0, len(payloads), chunk_size)
    )
    if cancellation():
        raise DirectMaterializationError("direct materialization cancelled before publication")
    total_chunks = len(chunks)
    total_points = len(payloads) if progress_total is None else progress_total
    started_at = time.monotonic() if progress_started_at is None else progress_started_at
    results: dict[int, tuple[DirectPoint, ...]] = {}
    pending: dict[Future, int] = {}
    next_index = 0
    completed_chunks = 0
    materialized_points = 0
    executor = ProcessPoolExecutor(max_workers=settings.workers)
    try:
        while completed_chunks < total_chunks:
            if cancellation():
                raise DirectMaterializationError("direct materialization cancelled before publication")
            while next_index < total_chunks and len(pending) < settings.max_in_flight_chunks:
                if cancellation():
                    raise DirectMaterializationError("direct materialization cancelled before publication")
                future = executor.submit(
                    _materialize_payload_chunk, chunks[next_index], window_start_utc, window_end_utc
                )
                pending[future] = next_index
                next_index += 1
            if cancellation():
                raise DirectMaterializationError("direct materialization cancelled before publication")
            done, _ = wait(
                tuple(pending),
                timeout=_PARALLEL_WAIT_TIMEOUT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue
            for future in done:
                if cancellation():
                    raise DirectMaterializationError("direct materialization cancelled before publication")
                index = pending.pop(future)
                chunk_points = future.result()
                results[index] = chunk_points
                completed_chunks += 1
                materialized_points += len(chunk_points)
                if progress_callback is not None:
                    elapsed_seconds = time.monotonic() - started_at
                    global_materialized_points = progress_offset + materialized_points
                    progress_callback(
                        "MATERIALIZING",
                        materialized_points=global_materialized_points,
                        total_points=total_points,
                        workers=settings.workers,
                        elapsed_seconds=elapsed_seconds,
                        points_per_second=(
                            global_materialized_points / elapsed_seconds if elapsed_seconds > 0 else 0.0
                        ),
                        side=progress_side,
                    )
        executor.shutdown(wait=True)
    except BaseException:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    return tuple(point for index in range(total_chunks) for point in results[index])


def _materialize_direct_with_compat(
    source_connection: duckdb.DuckDBPyConnection,
    analysis_connection: duckdb.DuckDBPyConnection | None,
    request: DirectBuildRequest,
    cancellation: Callable[[], bool],
    materialize_kwargs: dict[str, object],
) -> DirectSurface:
    """Call the materializer, dropping progress_callback for legacy callables."""
    try:
        return materialize_duckdb_direct(
            source_connection, analysis_connection, request, cancellation, **materialize_kwargs
        )
    except TypeError as error:
        message = str(error)
        if "unexpected keyword argument" not in message:
            raise
        if "progress_callback" not in message and "progress_side" not in message:
            raise
        materialize_kwargs.pop("progress_callback", None)
        materialize_kwargs.pop("progress_side", None)
        return materialize_duckdb_direct(
            source_connection, analysis_connection, request, cancellation, **materialize_kwargs
        )


def materialize_duckdb_direct(
    source_connection: duckdb.DuckDBPyConnection,
    analysis_connection: duckdb.DuckDBPyConnection | None,
    request: DirectBuildRequest,
    cancellation: Callable[[], bool],
    *,
    preflight: DirectPreflight | None = None,
    materialization_settings: DirectMaterializationSettings | None = None,
    progress_callback: Callable[..., object] | None = None,
    progress_side: str | None = None,
) -> DirectSurface:
    """Decode only preflight-accepted reports into a non-published direct surface."""
    settings = materialization_settings or DirectMaterializationSettings()
    if analysis_connection is not None and source_connection is analysis_connection:
        raise DirectMaterializationError("source and analysis connections must be distinct")
    if cancellation():
        raise DirectMaterializationError("direct materialization cancelled before publication")
    _validate_canonical_v2_request(request)
    preflight = preflight or preflight_duckdb_direct(source_connection, request)
    if preflight.unavailable_symbols:
        raise DirectMaterializationError("selected symbol has no usable timeframe")
    start, end = _window(request)
    points: list[DirectPoint] = []
    manifest = preflight.manifest
    progress_started_at = time.monotonic()
    for offset in range(0, len(manifest), settings.fetch_batch_size):
        if cancellation():
            raise DirectMaterializationError("direct materialization cancelled before publication")
        batch = manifest[offset : offset + settings.fetch_batch_size]
        payloads = _fetch_materialization_payload_batch(source_connection, batch)
        points.extend(
            _materialize_payloads_parallel(
                payloads,
                settings,
                start.isoformat(),
                end.isoformat(),
                cancellation,
                progress_callback=progress_callback,
                progress_offset=len(points),
                progress_total=len(manifest),
                progress_started_at=progress_started_at,
                progress_side=progress_side,
            )
        )
    if request.grid_contract_kind == V2_GRID_CONTRACT_KIND:
        points.sort(key=lambda point: point_key_tuple(point.canonical_point_key))
    if len({point.canonical_point_key for point in points}) != len(points):
        raise DirectMaterializationError("canonical point uniqueness failed")
    point_sides = {
        point.canonical_point_key.split("|")[1]
        for point in points
    }
    if point_sides and point_sides != {request.side}:
        raise DirectMaterializationError("direct surface side must match request side")
    materialized_keys = tuple(point.canonical_point_key for point in points)
    if len(materialized_keys) != len(manifest) or materialized_keys != preflight.accepted_point_keys:
        raise DirectMaterializationError(
            "direct surface point set does not match frozen preflight manifest"
        )
    return DirectSurface(request, preflight, REAL_EVENT_MODE, tuple(points))


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
    surface = _materialize_direct_with_compat(
        source_connection,
        analysis_connection,
        request,
        cancellation,
        {"preflight": preflight, "progress_callback": progress_callback, "progress_side": request.side},
    )
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
