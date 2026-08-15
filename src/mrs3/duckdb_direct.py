"""Read-only preflight and in-memory materialization for direct v5 source reports."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass, replace
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


REAL_EVENT_MODE = "real_independent_events"
OBSERVED_GRID_CONTRACT_KIND = "OBSERVED_GRID_CONTRACT"
V2_GRID_CONTRACT_KIND = "OBSERVED_SPARSE_GRID_CONTRACT_V2"
READINESS_CONTRACT_VERSION = "shift_readiness_v1"
READINESS_MAX_SHIFT_BP = 430
READINESS_BOUNDARIES_BP = (30, 150, 430)
READINESS_BAND_GAPS_BP = ((150, 10), (430, 40))
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
    witness: ReadinessWitness | None
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
    witnesses: Mapping[str, object] = MappingProxyType({})
    point_evidence_sha256: str = ""
    audit_artifact_name: str = ""
    audit_schema_version: int = 1
    audit_row_count: int = 0
    audit_sha256: str = ""
    audit_bytes: bytes | None = None


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
) -> None:
    if not isinstance(audit_bytes, bytes) or not audit_bytes:
        raise DirectMaterializationError("V2 audit bytes are required")
    if not isinstance(artifact_name, str) or not artifact_name:
        raise DirectMaterializationError("V2 audit artifact name is required")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
        raise DirectMaterializationError("V2 audit schema version must be a positive integer")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        raise DirectMaterializationError("V2 audit row count must be a non-negative integer")
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


def _readiness_for_pair(
    open_ma: int, close_ma: int, available_shifts: set[int]
) -> tuple[ReadinessWitness | None, tuple[tuple[object, ...], ...]]:
    missing_boundaries = [boundary for boundary in READINESS_BOUNDARIES_BP if boundary not in available_shifts]
    if missing_boundaries:
        return None, tuple(("MISSING_BOUNDARY", boundary) for boundary in missing_boundaries)
    witness = [30]
    current = 30
    for upper, max_gap in READINESS_BAND_GAPS_BP:
        while current < upper:
            candidates = sorted(
                shift for shift in available_shifts
                if current < shift <= min(current + max_gap, upper)
            )
            if not candidates:
                next_shift = min(
                    (shift for shift in available_shifts if shift > current + max_gap), default=None
                )
                if next_shift is None:
                    return None, (("MISSING_BOUNDARY", upper),)
                return None, (("SHIFT_GAP_EXCEEDS_MAX", current, next_shift, max_gap),)
            current = candidates[-1]
            witness.append(current)
        if current != upper:
            return None, (("MISSING_BOUNDARY", upper),)
    return ReadinessWitness(open_ma, close_ma, tuple(witness)), ()


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


def _scope_witness(
    rows: Sequence[dict[str, object]],
    start_ms: int,
    end_ms: int,
) -> ReadinessWitness | None:
    merged_cells = _scope_merged_cells(rows)
    passing: list[tuple[tuple[int, int], ReadinessWitness]] = []
    for pair in sorted({(open_ma, close_ma) for _, open_ma, close_ma in merged_cells}):
        witness, _ = _readiness_for_pair(
            pair[0],
            pair[1],
            _available_shifts(pair, (start_ms, end_ms), merged_cells),
        )
        if witness is not None:
            passing.append((pair, witness))
    if not passing:
        return None
    return min(passing, key=lambda item: (item[0], item[1].shifts_bp))[1]


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


def _interval_has_no_gap(
    interval: tuple[int, int], factual_chains: tuple[tuple[int, int], ...]
) -> bool:
    start_ms, end_ms = interval
    return any(window[0] <= start_ms and window[1] >= end_ms for window in factual_chains)


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
        candidates: list[tuple[int, int, tuple[int, int], ReadinessWitness]] = []
        pairs = sorted({(open_ma, close_ma) for _, open_ma, close_ma in merged_cells})
        for pair in pairs:
            runs: list[tuple[tuple[int, int], int, int, ReadinessWitness]] = []
            for index in range(len(boundaries) - 1):
                segment = (boundaries[index], boundaries[index + 1])
                witness, _ = _readiness_for_pair(
                    pair[0], pair[1], _available_shifts(pair, segment, merged_cells)
                )
                if witness is None:
                    continue
                if runs and runs[-1][0] == pair:
                    combined_start = runs[-1][1]
                    combined_end = segment[1]
                    combined_witness, _ = _readiness_for_pair(
                        pair[0],
                        pair[1],
                        _available_shifts(pair, (combined_start, combined_end), merged_cells),
                    )
                    if combined_witness is not None:
                        runs[-1] = (pair, combined_start, combined_end, combined_witness)
                        continue
                runs.append((pair, segment[0], segment[1], witness))
            candidates.extend((start, end, pair, witness) for pair, start, end, witness in runs)
        unique_candidates = {
            (start, end, pair, witness): (start, end, pair, witness)
            for start, end, pair, witness in candidates
        }
        ordered_candidates = sorted(
            unique_candidates.values(),
            key=lambda item: (
                -(item[1] - item[0]),
                item[0],
                item[1],
                item[2],
                item[3].shifts_bp,
            ),
        )
        selectable = False
        interval_start_utc = ""
        interval_end_utc = ""
        if ordered_candidates:
            for index, (start_ms, end_ms, _pair, witness) in enumerate(ordered_candidates):
                displayed = index == 0
                current_selectable = (
                    displayed
                    and len(factual_chains) == 1
                    and _interval_has_no_gap((start_ms, end_ms), factual_chains)
                )
                if displayed:
                    selectable = current_selectable
                    interval_start_utc = _utc_ms(start_ms)
                    interval_end_utc = _utc_ms(end_ms)
                intervals.append(CoverageInterval(
                    scope,
                    _utc_ms(start_ms),
                    _utc_ms(end_ms),
                    witness,
                    displayed=displayed,
                    selectable=current_selectable,
                ))
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
                interval.witness.open_ma if interval.witness is not None else None,
                interval.witness.close_ma if interval.witness is not None else None,
                interval.witness.shifts_bp if interval.witness is not None else (),
                interval.witness.contract_version if interval.witness is not None else "",
                interval.witness.max_shift_bp if interval.witness is not None else 0,
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
) -> tuple[DirectSurface, ...]:
    """Prepare all sides in memory under one read-only source transaction."""
    if not requests:
        raise DirectMaterializationError("at least one direct request is required")
    ordered = tuple(sorted(requests, key=lambda request: 0 if request.side == "LONG" else 1))
    if len({request.side for request in ordered}) != len(ordered):
        raise DirectMaterializationError("only one request per side is allowed")
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
            )
            audit_sha256 = hashlib.sha256(audit_bytes).hexdigest()
            audit_artifact_name = f"surface_coverage_audit_{side}.csv"
            write_coverage_artifact(
                audit_root,
                f"surface_coverage/{audit_sha256}/{audit_artifact_name}",
                audit_bytes,
            )
            request = replace(
                request,
                grid_contract_kind=V2_GRID_CONTRACT_KIND,
                readiness_contract_version=READINESS_CONTRACT_VERSION,
                readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
                audit_artifact_name=audit_artifact_name,
                audit_schema_version=1,
                audit_row_count=_audit_data_row_count(audit_bytes),
                audit_sha256=audit_sha256,
                audit_bytes=audit_bytes,
            )
            preflight = preflight_duckdb_direct(source_connection, request)
            if preflight.unavailable_symbols:
                raise DirectMaterializationError("selected scope is unavailable")
            if cancellation():
                raise DirectMaterializationError("direct build cancelled before publication")
            surface = materialize_duckdb_direct(
                source_connection,
                None,
                request,
                cancellation,
                preflight=preflight,
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


def publish_direct_surfaces(
    analysis_connection: duckdb.DuckDBPyConnection,
    surfaces: Sequence[DirectSurface],
    *,
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
            published_surface = publish_surface(analysis_connection, surface)
        except BaseException as error:
            state = "PARTIAL" if published else "FAILED"
            return DirectQueueResult(state, tuple(published), phase=state, error=str(error))
        published.append(published_surface)
    return DirectQueueResult("PUBLISHED", tuple(published), phase="PUBLISHED")


def _evaluation_rows_for_scope(
    rows: list[dict[str, object]],
    scope: DirectScope,
    interval_start_utc: str,
    interval_end_utc: str,
    displayed_interval: bool,
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
    pairs = sorted({(open_ma, close_ma) for _, open_ma, close_ma in merged_cells})
    pair_witness: dict[tuple[int, int], ReadinessWitness | None] = {}
    pair_diagnostics: dict[tuple[int, int], tuple[tuple[object, ...], ...]] = {}
    for pair in pairs:
        witness, diagnostics = _readiness_for_pair(
            pair[0],
            pair[1],
            _available_shifts(pair, (start_ms, end_ms), merged_cells),
        )
        pair_witness[pair] = witness
        pair_diagnostics[pair] = diagnostics
    passing = [(pair, witness) for pair, witness in pair_witness.items() if witness is not None]
    canonical_pair, canonical_witness = (
        min(passing, key=lambda item: (item[0], item[1].shifts_bp)) if passing else (None, None)
    )
    canonical_shifts = set(canonical_witness.shifts_bp) if canonical_witness is not None else set()
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
            canonical_pair == pair
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
        for diagnostic in pair_diagnostics[pair]:
            gap_start_bp = None
            gap_end_bp = None
            max_gap_bp = None
            if diagnostic[0] == "MISSING_BOUNDARY":
                reason_code = "MISSING_BOUNDARY"
                reason_detail = f"MISSING_BOUNDARY: boundary_bp={diagnostic[1]}"
            else:
                _, gap_start_bp, gap_end_bp, max_gap_bp = diagnostic
                reason_code = "SHIFT_GAP_EXCEEDS_MAX"
                reason_detail = (
                    f"SHIFT_GAP_EXCEEDS_MAX: gap_start_bp={gap_start_bp}, "
                    f"gap_end_bp={gap_end_bp}, max_gap_bp={max_gap_bp}"
                )
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
                "gap_start_bp": gap_start_bp,
                "gap_end_bp": gap_end_bp,
                "max_gap_bp": max_gap_bp,
                "report_id": None,
                "source_sha256": None,
                "selected_report": None,
                "status": "MISSING",
                "reason_code": reason_code,
                "reason_detail": reason_detail,
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
                    scope_rows, scope, interval.start_utc, interval.end_utc, interval.displayed
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
            scope_rows, scope, interval_start_utc, interval_end_utc, True
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


def _preflight_duckdb_direct_v2(
    source_connection: duckdb.DuckDBPyConnection,
    request: DirectBuildRequest,
    start_ms: int,
    end_ms: int,
    all_reports: list[dict[str, object]],
) -> DirectPreflight:
    if request.readiness_contract_version != READINESS_CONTRACT_VERSION or request.readiness_max_shift_bp != READINESS_MAX_SHIFT_BP:
        raise DirectMaterializationError("V2 readiness contract is not active")
    _verify_v2_audit_metadata(
        audit_bytes=request.audit_bytes,
        artifact_name=request.audit_artifact_name,
        schema_version=request.audit_schema_version,
        row_count=request.audit_row_count,
        sha256_digest=request.audit_sha256,
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

    witnesses: dict[str, dict[str, object]] = {}
    accepted_rows: list[dict[str, object]] = []
    issues: list[CoverageIssue] = []
    usable: dict[str, list[str]] = {}
    for scope in selected_scopes:
        symbol, timeframe = scope.split("|", maxsplit=1)
        rows = scope_rows.get((symbol, timeframe), [])
        if not rows:
            issues.append(CoverageIssue(symbol, timeframe, "GRID_NOT_COVERED", "no report covers the selected scope"))
            continue
        witness = _scope_witness(rows, start_ms, end_ms)
        if witness is None:
            issues.append(CoverageIssue(symbol, timeframe, "GRID_NOT_READY", "readiness contract is not satisfied"))
            continue
        selected = _v2_selected_rows(rows, start_ms, end_ms)
        if not selected:
            issues.append(CoverageIssue(symbol, timeframe, "GRID_NOT_COVERED", "no report covers the requested UTC half-open window"))
            continue
        witnesses[scope] = {
            "symbol": symbol,
            "side": request.side,
            "timeframe": timeframe,
            "open_ma": witness.open_ma,
            "close_ma": witness.close_ma,
            "shifts_bp": list(witness.shifts_bp),
            "contract_version": witness.contract_version,
            "max_shift_bp": witness.max_shift_bp,
        }
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
        "selected_scopes": list(selected_scopes),
        "readiness_contract_version": request.readiness_contract_version,
        "readiness_max_shift_bp": request.readiness_max_shift_bp,
        "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION,
        "witnesses": {scope: witnesses[scope] for scope in selected_scopes if scope in witnesses},
        "point_evidence": evidence_bytes.decode("utf-8"),
        "point_evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "audit_artifact_name": request.audit_artifact_name,
        "audit_schema_version": request.audit_schema_version,
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
        audit_row_count=request.audit_row_count,
        audit_sha256=request.audit_sha256,
        audit_bytes=request.audit_bytes,
    )


def preflight_duckdb_direct(source_connection: duckdb.DuckDBPyConnection, request: DirectBuildRequest) -> DirectPreflight:
    """Validate full UTC coverage and observed-grid completeness without decoding payloads."""
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


def materialize_duckdb_direct(
    source_connection: duckdb.DuckDBPyConnection,
    analysis_connection: duckdb.DuckDBPyConnection | None,
    request: DirectBuildRequest,
    cancellation: Callable[[], bool],
    *,
    preflight: DirectPreflight | None = None,
) -> DirectSurface:
    """Decode only preflight-accepted reports into a non-published direct surface."""
    if analysis_connection is not None and source_connection is analysis_connection:
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
            actions = decode_compact_actions(bytes(actions_blob), int(action_count))
            metrics = calculate_point_metrics(grid, decode_compact_deltas(bytes(equity_blob), int(equity_count), codec=str(series_codec)), decode_wallet_changes(bytes(wallet_blob), int(wallet_count), codec=str(series_codec)), actions, start.isoformat(), end.isoformat())
            symbol, _, timeframe, *_ = str(point_key).split("|")
            reconstruction = reconstruct_closed_cycles(
                str(report_id), symbol, timeframe, actions, start.isoformat(), end.isoformat()
            )
            event_ids = tuple(sorted({cycle.event_id for cycle in reconstruction.included}))
        except SourcePackError as error:
            raise DirectMaterializationError(f"cannot materialize {point_key}: {error}") from error
        points.append(DirectPoint(str(point_key), report_id, source_hash, len(event_ids), metrics, event_ids))
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
