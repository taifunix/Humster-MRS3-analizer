from __future__ import annotations

import json
from types import MappingProxyType
import csv
import hashlib
import io
import pickle
import threading
import time
from concurrent.futures import Future
from hashlib import sha256
from datetime import datetime
from dataclasses import MISSING, asdict, replace
from pathlib import Path
import os
from types import MappingProxyType

import duckdb
import pytest

from mrs3 import duckdb_direct
from mrs3 import duckdb_source_schema
from mrs3.config import DEFAULT_CANONICAL_SHIFTS_BP, DirectMaterializationSettings
from mrs3.duckdb_direct import (
    CANONICAL_GRID_VERSION,
    CANONICAL_MATERIALIZER_VERSION,
    POINT_MATERIALIZATION_SEMANTICS_VERSION,
    REAL_EVENT_MODE,
    READINESS_CONTRACT_VERSION,
    READINESS_MAX_SHIFT_BP,
    REQUIRED_CLOSE_MAS,
    V2_GRID_CONTRACT_KIND,
    CoverageIssue,
    DirectBuildRequest,
    DirectMaterializationError,
    COVERAGE_CSV_COLUMNS,
    DirectPoint,
    DirectPreflight,
    DirectQueueResult,
    DirectScope,
    DirectSurface,
    MaterializationPayload,
    _CoverageScan,
    _coverage_scan_rows,
    _direct_coverage,
    _fetch_materialization_payload_batch,
    _materialize_payload_chunk,
    _materialize_payloads_parallel,
    common_intervals_for_scopes,
    coverage_scan_direct,
    _canonical_json_bytes,
    _effective_window,
    coverage_audit_csv_bytes,
    coverage_inventory_csv_bytes,
    canonical_point_materialization_config_hash,
    canonical_point_materialization_semantic_payload,
    list_duckdb_direct_coverage,
    materialize_duckdb_direct,
    point_evidence_jsonl_bytes,
    preflight_duckdb_direct,
    prepare_direct_surfaces,
    replay_direct_preflights,
    publish_direct_surfaces,
    verify_persisted_surface_audit,
    run_panel_direct_build,
    write_coverage_artifact,
)
from mrs3.analysis_storage import (
    ANALYSIS_SCHEMA_VERSION,
    PublishedSurface,
    ensure_analysis_schema,
    publish_surface,
)
from mrs3.duckdb_events import ACTION_CODEC, EQUITY_CODEC, canonical_event_id
from mrs3.duckdb_source_schema import (
    NORMALIZATION_CONTRACT_VERSION,
    _grid_content_hash,
    _grid_hash,
    _payload_hash,
    _point_hash,
    _report_hash,
    canonical_report_key,
    ensure_source_schema,
)


START_MS = 1_704_067_200_000
END_MS = 1_704_074_400_000
READY_SHIFTS = DEFAULT_CANONICAL_SHIFTS_BP


def _hash(*values: object) -> str:
    return sha256("|".join(map(str, values)).encode()).hexdigest()


def _seed_report(
    source: duckdb.DuckDBPyConnection, *, symbol: str = "BTCUSDT", timeframe: str = "1h",
    shift: int = 100, open_ma: int = 3, close_ma: int = 9, trades: int = 7,
    start_ms: int = START_MS, end_ms: int = END_MS, source_hash: str | None = None,
    actions: tuple[dict[str, str], ...] = (), side: str = "LONG",
    grid_start_ms: int | None = None, grid_end_ms: int | None = None,
) -> str:
    point_key = f"{symbol}|{side}|{timeframe}|{shift}|{open_ma}|{close_ma}"
    point = {
        "canonical_point_key": point_key, "symbol": symbol, "side": side, "timeframe": timeframe,
        "shift_bp": shift, "open_ma_type": "EMA", "open_ma_source": "close", "open_ma_len": open_ma,
        "open_multiplier_raw": "0.99", "close_ma_type": "EMA", "close_ma_source": "close", "close_ma_len": close_ma,
    }
    point["row_sha256"] = _point_hash(point)
    if source.execute("select count(*) from point_configs where canonical_point_key=?", [point_key]).fetchone()[0] == 0:
        source.execute("insert into point_configs values (?,?,?,?,?,?,?,?,?,?,?,?,?)", list(point.values()))
    grid_start_ms = start_ms if grid_start_ms is None else grid_start_ms
    grid_end_ms = end_ms if grid_end_ms is None else grid_end_ms
    timestamps = (grid_start_ms, (grid_start_ms + grid_end_ms) // 2, grid_end_ms)
    import struct, zlib
    grid = {"grid_hash": _grid_content_hash(timestamps), "sample_count": len(timestamps), "start_timestamp_ms": grid_start_ms, "end_timestamp_ms": grid_end_ms, "timestamps_zlib": zlib.compress(struct.pack("<3q", timestamps[0], timestamps[1] - timestamps[0], timestamps[2] - timestamps[1]))}
    grid["row_sha256"] = _grid_hash(grid)
    if source.execute("select count(*) from time_grids where grid_hash=?", [grid["grid_hash"]]).fetchone()[0] == 0:
        source.execute("insert into time_grids values (?,?,?,?,?,?)", list(grid.values()))
    canonical = canonical_report_key({"canonical_point_key": point_key, "report_period_start_ms": start_ms, "report_period_end_ms": end_ms})
    if source_hash is not None and len(source_hash) != 64:
        source_hash = source_hash.rjust(64, "0")
    source_hash = source_hash or _hash(canonical)
    report_id = _hash(canonical, source_hash)
    report = {"report_id": report_id, "canonical_report_key": canonical, "canonical_point_key": point_key, "grid_hash": grid["grid_hash"], "source_sha256": source_hash, "source_file": "fixture.html", "source_size": 1, "imported_at_utc": datetime(2026, 8, 11), "settings_json": "{}", "raw_action_count": len(actions), "equity_sample_count": 3, "wallet_change_count": 1, "report_period_start_ms": start_ms, "report_period_end_ms": end_ms}
    report["row_sha256"] = _report_hash(report)
    source.execute("insert into active_reports values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", list(report.values()))
    headers = sorted({key for action in actions for key in action})
    action_payload = {"headers": headers, "rows": [[action.get(header, "") for header in headers] for action in actions]}
    payload = {"report_id": report_id, "series_codec": EQUITY_CODEC, "actions_codec": ACTION_CODEC, "actions_zlib": zlib.compress(json.dumps(action_payload).encode()), "equity_zlib": zlib.compress(struct.pack("<3q", 100, 90, 110)), "wallet_zlib": zlib.compress(struct.pack("<Iq", 0, 100))}
    payload["payload_sha256"] = _payload_hash(payload)
    source.execute("insert into report_payloads values (?,?,?,?,?,?,?)", list(payload.values()))
    return point_key


@pytest.fixture
def connections():
    source, analysis = duckdb.connect(":memory:"), duckdb.connect(":memory:")
    ensure_source_schema(source)
    try:
        yield source, analysis
    finally:
        source.close(); analysis.close()


def _request(**changes: object) -> DirectBuildRequest:
    fields = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-01T02:00:00Z", "side": "LONG", "symbols": ("BTCUSDT",), "required_shifts_bp": (100,), "materializer_version": "v1", "point_materialization_config_hash": "a" * 64}
    fields.update(changes)
    return DirectBuildRequest(**fields)


def _v2_preflight(side: str) -> DirectPreflight:
    return DirectPreflight(
        {}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}),
        (), (), (),
    )


def test_direct_preflight_empty_witnesses_uses_default_factory() -> None:
    field = DirectPreflight.__dataclass_fields__["witnesses"]

    assert field.default_factory is not MISSING
    assert field.default_factory() == MappingProxyType({})


class _StatementSource:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, *args: object) -> object:
        self.statements.append(str(statement))
        return None

    def close(self) -> None:
        pass


def test_preflight_accepts_utc_half_open_coverage_and_observed_grid_contract(connections) -> None:
    source, _ = connections
    point = _seed_report(source)

    preflight = preflight_duckdb_direct(source, _request())

    assert preflight.usable_timeframes == {"BTCUSDT": ("1h",)}
    assert preflight.unavailable_symbols == {}
    assert preflight.source_hashes
    assert preflight.grid_contract["kind"] == "OBSERVED_GRID_CONTRACT"
    assert preflight.grid_contract["required_shifts_bp"] == (100,)
    assert preflight.grid_contract["pairs"] == ("100|3|9",)
    assert preflight.accepted_point_keys == (point,)

    row = next(item for item in preflight.coverage_rows)
    assert row.selectable is False
    assert "witness" not in row.__dataclass_fields__


def test_preflight_does_not_run_full_payload_validation(
    connections, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = connections
    _seed_report(source)

    def forbidden(*_: object) -> object:
        raise AssertionError("direct preflight must not decode all report payloads")

    monkeypatch.setattr(duckdb_source_schema, "decode_compact_actions", forbidden)
    monkeypatch.setattr(duckdb_source_schema, "decode_wallet_changes", forbidden)

    assert preflight_duckdb_direct(source, _request()).usable_timeframes == {
        "BTCUSDT": ("1h",)
    }


def test_preflight_rejects_incompatible_storage_mode(connections) -> None:
    source, _ = connections
    _seed_report(source)
    source.execute("update schema_info set value='incompatible' where key='storage_mode'")

    with pytest.raises(DirectMaterializationError, match="invalid active v5 source"):
        preflight_duckdb_direct(source, _request())


def test_preflight_wraps_structural_source_error(connections) -> None:
    source, _ = connections
    _seed_report(source)
    source.execute("update active_reports set row_sha256=?", ["0" * 64])

    with pytest.raises(DirectMaterializationError, match="invalid active v5 source"):
        preflight_duckdb_direct(source, _request())


def test_preflight_grid_contract_is_immutable(connections) -> None:
    source, _ = connections
    _seed_report(source)

    preflight = preflight_duckdb_direct(source, _request())

    with pytest.raises(TypeError):
        preflight.grid_contract["kind"] = "MUTATED"  # type: ignore[index]
    with pytest.raises(AttributeError):
        preflight.grid_contract["required_shifts_bp"].append(200)  # type: ignore[union-attr]


def test_preflight_excludes_timeframe_when_no_ma_pair_is_common_to_every_shift(connections) -> None:
    source, _ = connections
    _seed_report(source, shift=100)
    _seed_report(source, shift=200, open_ma=4, close_ma=10)

    preflight = preflight_duckdb_direct(source, _request(required_shifts_bp=(100, 200)))

    assert preflight.usable_timeframes == {}
    assert preflight.unavailable_symbols == {"BTCUSDT": ("1h",)}
    assert {issue.code for issue in preflight.coverage_issues} == {"GRID_NO_COMMON_PAIRS"}


def test_preflight_keeps_maximal_complete_ma_pair_grid_and_audits_excluded_pairs(connections) -> None:
    source, _ = connections
    _seed_report(source, shift=100, open_ma=3, close_ma=9)
    _seed_report(source, shift=100, open_ma=4, close_ma=10)
    _seed_report(source, shift=200, open_ma=3, close_ma=9)

    preflight = preflight_duckdb_direct(source, _request(required_shifts_bp=(100, 200)))

    assert preflight.usable_timeframes == {"BTCUSDT": ("1h",)}
    assert preflight.grid_contract["pairs"] == ("100|3|9", "200|3|9")
    assert len(preflight.accepted_point_keys) == 2
    assert {issue.code for issue in preflight.coverage_issues} == {"EXCLUDED_INCOMPLETE_PAIR"}


def test_preflight_selects_the_narrowest_covering_report_for_an_overlapping_cell(connections) -> None:
    source, _ = connections
    _seed_report(source, end_ms=END_MS + 3_600_000)
    _seed_report(source, end_ms=END_MS)

    preflight = preflight_duckdb_direct(source, _request())

    assert preflight.usable_timeframes == {"BTCUSDT": ("1h",)}
    assert len(preflight.manifest) == 1
    assert {issue.code for issue in preflight.coverage_issues} == {"OVERLAPPING_REPORTS_RESOLVED"}


def test_preflight_reports_longest_continuous_interval_and_explicit_gap_details(connections) -> None:
    source, _ = connections
    _seed_report(source, start_ms=START_MS, end_ms=START_MS + 2 * 3_600_000, source_hash="a" * 64)
    _seed_report(source, start_ms=START_MS + 2 * 3_600_000, end_ms=START_MS + 4 * 3_600_000, source_hash="b" * 64)
    _seed_report(source, start_ms=START_MS + 6 * 3_600_000, end_ms=START_MS + 8 * 3_600_000, source_hash="c" * 64)

    preflight = preflight_duckdb_direct(source, _request())

    row = next(item for item in preflight.coverage_rows if item.symbol == "BTCUSDT" and item.timeframe == "1h")
    assert row.selectable is False
    assert row.interval_start_utc == "2024-01-01T00:00:00.000+00:00"
    assert row.interval_end_utc == "2024-01-01T04:00:00.000+00:00"
    assert row.gap_details == ("missing: 2024-01-01 .. 2024-01-01",)


def test_preflight_does_not_mark_gap_free_but_unready_scope_selectable(connections) -> None:
    source, _ = connections
    _seed_report(source, symbol="ETHUSDT", timeframe="4h", start_ms=START_MS, end_ms=START_MS + 2 * 3_600_000, source_hash="d" * 64)
    _seed_report(source, symbol="ETHUSDT", timeframe="4h", start_ms=START_MS + 2 * 3_600_000, end_ms=START_MS + 4 * 3_600_000, source_hash="e" * 64)

    preflight = preflight_duckdb_direct(source, _request(symbols=("ETHUSDT",)))

    row = next(item for item in preflight.coverage_rows if item.symbol == "ETHUSDT" and item.timeframe == "4h")
    assert row.selectable is False
    assert row.interval_start_utc == "2024-01-01T00:00:00.000+00:00"
    assert row.interval_end_utc == "2024-01-01T04:00:00.000+00:00"
    assert row.gap_details == ()


def test_preflight_coverage_rows_use_effective_report_grid_and_readiness(connections) -> None:
    source, _ = connections
    _seed_report(source, start_ms=START_MS, end_ms=END_MS, source_hash="effective-grid")

    preflight = preflight_duckdb_direct(source, _request())

    row = next(item for item in preflight.coverage_rows)
    assert row.interval_start_utc == "2024-01-01T00:00:00.000+00:00"
    assert row.interval_end_utc == "2024-01-01T02:00:00.000+00:00"
    assert row.selectable is False


def test_coverage_scan_discovers_all_symbols_when_filter_is_empty(connections) -> None:
    source, _ = connections
    _seed_report(source, symbol="BTCUSDT", source_hash="a" * 64)
    _seed_report(source, symbol="ETHUSDT", timeframe="4h", source_hash="b" * 64)

    rows = list_duckdb_direct_coverage(source, side="LONG", symbols=())

    assert [(row.symbol, row.timeframe) for row in rows] == [
        ("BTCUSDT", "1h"),
        ("ETHUSDT", "4h"),
    ]


def test_coverage_uses_report_grid_intersection_and_rejects_empty(connections) -> None:
    source, _ = connections
    _seed_report(source)

    row = next(iter(list_duckdb_direct_coverage(source, symbols=()).rows))

    assert row.interval_start_utc == "2024-01-01T00:00:00.000+00:00"
    assert row.interval_end_utc == "2024-01-01T02:00:00.000+00:00"

    _seed_report(
        source,
        symbol="ETHUSDT",
        source_hash="empty-grid",
        grid_start_ms=END_MS,
        grid_end_ms=END_MS + 3_600_000,
    )
    with pytest.raises(DirectMaterializationError, match="empty report/grid intersection"):
        list_duckdb_direct_coverage(source, symbols=())


def test_coverage_merges_touching_effective_windows_per_cell(connections) -> None:
    source, _ = connections
    _seed_report(source, start_ms=START_MS, end_ms=START_MS + 2 * 3_600_000, source_hash="a" * 64)
    _seed_report(source, start_ms=START_MS + 2 * 3_600_000, end_ms=START_MS + 4 * 3_600_000, source_hash="b" * 64)

    row = next(iter(list_duckdb_direct_coverage(source, symbols=()).rows))

    assert row.interval_start_utc == "2024-01-01T00:00:00.000+00:00"
    assert row.interval_end_utc == "2024-01-01T04:00:00.000+00:00"
    assert row.gap_details == ()


def _seed_onusdt_shift_430_double_zero_rows(
    source: duckdb.DuckDBPyConnection,
) -> None:
    zero_ms = START_MS
    for open_ma in (5, 6, 7):
        for close_ma in range(2, 8):
            _seed_report(
                source,
                symbol="ONUSDT",
                timeframe="15m",
                shift=430,
                open_ma=open_ma,
                close_ma=close_ma,
                start_ms=zero_ms,
                end_ms=zero_ms,
                grid_start_ms=zero_ms,
                grid_end_ms=zero_ms,
                source_hash=_hash("onusdt", open_ma, close_ma),
            )


def test_coverage_ignores_double_degenerate_rows_only_when_report_and_grid_are_zero_duration(
    connections,
) -> None:
    source, _ = connections
    _seed_report(source)
    baseline = list_duckdb_direct_coverage(source, symbols=())

    _seed_onusdt_shift_430_double_zero_rows(source)
    coverage = list_duckdb_direct_coverage(source, symbols=())

    assert coverage.rows == baseline.rows
    assert coverage.intervals == baseline.intervals
    assert [(row.symbol, row.timeframe) for row in coverage.rows] == [("BTCUSDT", "1h")]


@pytest.mark.parametrize("grid_zero", [False, True])
def test_effective_window_remains_fail_closed_for_single_degenerate_windows(
    connections,
    grid_zero: bool,
) -> None:
    source, _ = connections
    if grid_zero:
        _seed_report(
            source,
            source_hash="grid-zero",
            start_ms=START_MS,
            end_ms=END_MS,
            grid_start_ms=START_MS,
            grid_end_ms=START_MS,
        )
    else:
        _seed_report(
            source,
            source_hash="report-zero",
            start_ms=START_MS,
            end_ms=START_MS,
            grid_start_ms=START_MS,
            grid_end_ms=END_MS,
        )

    with pytest.raises(DirectMaterializationError, match="empty report/grid intersection"):
        list_duckdb_direct_coverage(source, symbols=())


def test_effective_window_remains_fail_closed_for_reversed_and_disjoint_windows() -> None:
    rows = (
        {
            "canonical_point_key": "SYM|LONG|15m|430|5|2",
            "report_period_start_ms": 300,
            "report_period_end_ms": 100,
            "start_timestamp_ms": 0,
            "end_timestamp_ms": 200,
        },
        {
            "canonical_point_key": "SYM|LONG|15m|430|5|2",
            "report_period_start_ms": 0,
            "report_period_end_ms": 200,
            "start_timestamp_ms": 300,
            "end_timestamp_ms": 100,
        },
        {
            "canonical_point_key": "SYM|LONG|15m|430|5|2",
            "report_period_start_ms": 300,
            "report_period_end_ms": 100,
            "start_timestamp_ms": 400,
            "end_timestamp_ms": 50,
        },
        {
            "canonical_point_key": "SYM|LONG|15m|430|5|2",
            "report_period_start_ms": 0,
            "report_period_end_ms": 100,
            "start_timestamp_ms": 200,
            "end_timestamp_ms": 300,
        },
    )
    for row in rows:
        with pytest.raises(DirectMaterializationError, match="empty report/grid intersection"):
            _effective_window(row)


def _seed_readiness_scope(
    source: duckdb.DuckDBPyConnection,
    *,
    symbol: str,
    shifts: tuple[int, ...],
    open_ma: int = 3,
    close_ma: int = 9,
    start_ms: int = START_MS,
    end_ms: int = END_MS,
    side: str = "LONG",
) -> None:
    for shift in shifts:
        _seed_report(
            source,
            symbol=symbol,
            shift=shift,
            open_ma=open_ma,
            close_ma=close_ma,
            start_ms=start_ms,
            end_ms=end_ms,
            side=side,
            source_hash=_hash(symbol, side, shift, open_ma, close_ma, start_ms, end_ms),
        )


def test_readiness_requires_all_six_close_mas_on_common_interval(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma)
    _seed_readiness_scope(
        source,
        symbol="ETHUSDT",
        shifts=READY_SHIFTS,
        open_ma=4,
        close_ma=7,
    )

    coverage = list_duckdb_direct_coverage(source, symbols=())
    btc = next(row for row in coverage.rows if row.symbol == "BTCUSDT")
    eth = next(row for row in coverage.rows if row.symbol == "ETHUSDT")
    interval = next(item for item in coverage.intervals if item.scope.symbol == "BTCUSDT")

    assert btc.selectable is True
    assert btc.interval_start_utc == "2024-01-01T00:00:00.000+00:00"
    assert btc.interval_end_utc == "2024-01-01T02:00:00.000+00:00"
    assert tuple(w.close_ma for w in interval.witnesses) == REQUIRED_CLOSE_MAS
    assert all(w.open_ma == 3 for w in interval.witnesses)
    assert all(w.shifts_bp == READY_SHIFTS for w in interval.witnesses)
    assert all(w.contract_version == READINESS_CONTRACT_VERSION for w in interval.witnesses)
    assert all(w.max_shift_bp == READINESS_MAX_SHIFT_BP for w in interval.witnesses)
    assert eth.selectable is False


def test_readiness_allows_different_open_ma_per_close_ma(connections) -> None:
    source, _ = connections
    for close_ma, open_ma in zip(REQUIRED_CLOSE_MAS, (3, 4, 5, 6, 7, 8), strict=True):
        _seed_readiness_scope(
            source, symbol="BTCUSDT", shifts=READY_SHIFTS, open_ma=open_ma, close_ma=close_ma
        )

    coverage = list_duckdb_direct_coverage(source, symbols=())
    row = next(iter(coverage.rows))
    interval = next(iter(coverage.intervals))

    assert row.selectable is True
    assert tuple(w.open_ma for w in interval.witnesses) == (3, 4, 5, 6, 7, 8)
    assert tuple(w.close_ma for w in interval.witnesses) == REQUIRED_CLOSE_MAS


def test_readiness_keeps_later_start_candidate_when_it_is_longer(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(
            source,
            symbol="BTCUSDT",
            shifts=READY_SHIFTS,
            open_ma=3,
            close_ma=close_ma,
            start_ms=START_MS,
            end_ms=START_MS + 2 * 3_600_000,
        )
        _seed_readiness_scope(
            source,
            symbol="BTCUSDT",
            shifts=READY_SHIFTS,
            open_ma=5,
            close_ma=close_ma,
            start_ms=START_MS + 3_600_000,
            end_ms=START_MS + 4 * 3_600_000,
        )

    coverage = list_duckdb_direct_coverage(source, symbols=())
    row = next(iter(coverage.rows))
    interval = next(item for item in coverage.intervals if item.selectable)

    assert row.interval_start_utc == "2024-01-01T01:00:00.000+00:00"
    assert row.interval_end_utc == "2024-01-01T04:00:00.000+00:00"
    assert tuple(w.open_ma for w in interval.witnesses) == (5,) * len(REQUIRED_CLOSE_MAS)


def test_readiness_fails_closed_when_close_ma_7_is_missing(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS[:-1]:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma)

    coverage = list_duckdb_direct_coverage(source, symbols=())

    assert next(iter(coverage.rows)).selectable is False
    assert all(not interval.selectable for interval in coverage.intervals)


def test_readiness_rejects_stitching_two_open_mas_for_one_close_ma(connections) -> None:
    source, _ = connections
    split = READY_SHIFTS.index(230)
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(
            source, symbol="BTCUSDT", shifts=READY_SHIFTS[:split], close_ma=close_ma, open_ma=3
        )
        _seed_readiness_scope(
            source, symbol="BTCUSDT", shifts=READY_SHIFTS[split:], close_ma=close_ma, open_ma=5
        )

    coverage = list_duckdb_direct_coverage(source, symbols=())

    assert next(iter(coverage.rows)).selectable is False
    assert all(not interval.selectable for interval in coverage.intervals)


def test_readiness_rejects_missing_canonical_shift_550(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS[:-1], close_ma=close_ma)

    coverage = list_duckdb_direct_coverage(source, symbols=())

    assert next(iter(coverage.rows)).selectable is False


def test_legacy_extra_shift_cannot_replace_missing_canonical_shift(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS[:-1], close_ma=close_ma)
        _seed_report(
            source,
            symbol="BTCUSDT",
            shift=1490,
            open_ma=3,
            close_ma=close_ma,
            source_hash=_hash("extra", close_ma),
        )

    coverage = list_duckdb_direct_coverage(source, symbols=())

    assert next(iter(coverage.rows)).selectable is False


def test_readiness_is_deterministic_under_row_permutation(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma)
    rows = _coverage_scan_rows(source, side=None, symbols=())
    forward = _direct_coverage(rows)
    backward = _direct_coverage(list(reversed(rows)))

    assert forward == backward
    assert forward.rows == backward.rows
    assert forward.intervals == backward.intervals


def test_readiness_prefers_earliest_start_when_candidate_durations_tie(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(
            source,
            symbol="BTCUSDT",
            shifts=READY_SHIFTS,
            close_ma=close_ma,
            start_ms=START_MS,
            end_ms=START_MS + 2 * 3_600_000,
        )
        _seed_readiness_scope(
            source,
            symbol="BTCUSDT",
            shifts=READY_SHIFTS,
            close_ma=close_ma,
            start_ms=START_MS + 4 * 3_600_000,
            end_ms=START_MS + 6 * 3_600_000,
        )

    coverage = list_duckdb_direct_coverage(source, symbols=())
    row = next(iter(coverage.rows))
    scope_intervals = [item for item in coverage.intervals if item.scope.symbol == "BTCUSDT"]

    assert row.selectable is True
    assert row.interval_start_utc == "2024-01-01T00:00:00.000+00:00"
    assert row.interval_end_utc == "2024-01-01T02:00:00.000+00:00"
    assert len(scope_intervals) == 2
    assert scope_intervals[0].selectable is True
    assert scope_intervals[1].selectable is False
    assert {
        (item.start_utc, item.end_utc) for item in scope_intervals
    } == {
        ("2024-01-01T00:00:00.000+00:00", "2024-01-01T02:00:00.000+00:00"),
        ("2024-01-01T04:00:00.000+00:00", "2024-01-01T06:00:00.000+00:00"),
    }


def test_v2_request_rejects_noncanonical_materialization_tuple(connections) -> None:
    supplied = (
        30, 40, 50, 60, 70, 80, 90, 100, 110,
        120, 130, 140, 150, 190, 230, 270, 310, 430, 550,
    )
    assert supplied != DEFAULT_CANONICAL_SHIFTS_BP
    source, _ = connections
    request = _request(
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        selected_scopes=('BTCUSDT|1h',),
        required_shifts_bp=supplied,
        readiness_contract_version=READINESS_CONTRACT_VERSION,
        readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
        materializer_version=CANONICAL_MATERIALIZER_VERSION,
        point_materialization_config_hash=canonical_point_materialization_config_hash(supplied),
        audit_artifact_name='surface_coverage_audit_LONG.csv',
        audit_schema_version=1,
        audit_size_bytes=1,
        audit_row_count=0,
        audit_sha256='a' * 64,
        audit_bytes=b'x',
    )

    with pytest.raises(DirectMaterializationError, match="canonical shift tuple"):
        preflight_duckdb_direct(source, request)


def test_optional_noncanonical_points_do_not_disable_ready_scope(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma)
    _seed_report(source, shift=500, source_hash="shift-500")
    _seed_report(source, shift=600, source_hash="shift-600")
    _seed_report(source, shift=700, open_ma=4, close_ma=10, source_hash="shift-700")

    coverage = list_duckdb_direct_coverage(source, symbols=())
    row = next(iter(coverage.rows))
    csv_text = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
        symbols=("BTCUSDT",),
    ).decode("utf-8")
    audit_rows = list(csv.DictReader(io.StringIO(csv_text)))

    assert row.selectable is True
    for shift in (500, 600, 700):
        point = next(item for item in audit_rows if item["shift_bp"] == str(shift))
        assert point["status"] == "AVAILABLE"
        assert point["required_for_readiness"] == "false"


def test_factual_gap_in_non_witness_pair_does_not_disable_ready_scope(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(
            source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma,
            start_ms=START_MS, end_ms=END_MS,
        )
    _seed_report(
        source,
        shift=100,
        start_ms=START_MS + 4 * 3_600_000,
        end_ms=START_MS + 6 * 3_600_000,
        source_hash="gap-chain",
    )

    coverage = list_duckdb_direct_coverage(source, symbols=())

    row = next(iter(coverage.rows))
    assert row.selectable is True
    assert row.gap_details == ("missing: 2024-01-01 .. 2024-01-01",)


def test_inventory_emits_every_factual_chain_when_no_readiness_interval(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=(30, 150, 430),
        start_ms=START_MS,
        end_ms=START_MS + 2 * 3_600_000,
    )
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=(30, 150, 430),
        start_ms=START_MS + 4 * 3_600_000,
        end_ms=START_MS + 6 * 3_600_000,
    )

    inventory_rows = list(csv.DictReader(io.StringIO(coverage_inventory_csv_bytes(source).decode("utf-8"))))

    assert len({row["evaluation_id"] for row in inventory_rows}) == 2
    assert len({row["evaluation_id"] for row in inventory_rows if row["displayed_interval"] == "true"}) == 1
    assert all(
        row["interval_start_utc"].endswith("+00:00") and row["interval_end_utc"].endswith("+00:00")
        for row in inventory_rows
    )


def test_inventory_emits_every_candidate_chain_but_publication_emits_one_exact_block(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(
            source,
            symbol="BTCUSDT",
            shifts=READY_SHIFTS,
            close_ma=close_ma,
            start_ms=START_MS,
            end_ms=START_MS + 2 * 3_600_000,
        )
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(
            source,
            symbol="BTCUSDT",
            shifts=READY_SHIFTS,
            open_ma=5,
            close_ma=close_ma,
            start_ms=START_MS + 2 * 3_600_000,
            end_ms=START_MS + 4 * 3_600_000,
        )

    inventory_rows = list(csv.DictReader(io.StringIO(coverage_inventory_csv_bytes(source).decode("utf-8"))))
    audit_rows = list(csv.DictReader(io.StringIO(coverage_audit_csv_bytes(
        source,
        "2024-01-01T02:00:00+00:00",
        "2024-01-01T04:00:00+00:00",
    ).decode("utf-8"))))

    assert len({row["evaluation_id"] for row in inventory_rows}) == 2
    assert len({row["evaluation_id"] for row in audit_rows}) == 1
    assert all(row["interval_start_utc"] == "2024-01-01T02:00:00.000+00:00" for row in audit_rows)
    assert all(row["interval_end_utc"] == "2024-01-01T04:00:00.000+00:00" for row in audit_rows)
    assert all(row["displayed_interval"] == "true" for row in audit_rows)


def test_readiness_audit_reports_missing_canonical_shifts(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS[:-1], close_ma=close_ma)

    csv_text = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    ).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(csv_text)))

    gaps = [row for row in rows if row["row_type"] == "READINESS_GAP"]
    assert gaps
    assert all(row["status"] == "MISSING" for row in gaps)
    assert all(row["reason_code"] == "MISSING_SHIFT" for row in gaps)
    assert all("550" in row["reason_detail"] for row in gaps)
    assert next(iter(list_duckdb_direct_coverage(source, symbols=()).rows)).selectable is False


def test_coverage_csv_exact_columns_order_nulls_timestamps_reasons_and_hash(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma)
    _seed_report(
        source,
        shift=30,
        open_ma=3,
        close_ma=2,
        start_ms=START_MS,
        end_ms=END_MS + 3_600_000,
        source_hash="overlap",
    )
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="ETHUSDT", shifts=READY_SHIFTS[:-1], close_ma=close_ma)

    csv_bytes = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    )
    csv_text = csv_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)

    assert not csv_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in csv_bytes
    assert reader.fieldnames == list(COVERAGE_CSV_COLUMNS)
    assert all(row["readiness_max_shift_bp"] == str(READINESS_MAX_SHIFT_BP) for row in rows)
    assert all(row["readiness_contract_version"] == READINESS_CONTRACT_VERSION for row in rows)
    assert all(
        row["interval_start_utc"].endswith("+00:00") and row["interval_end_utc"].endswith("+00:00")
        for row in rows
    )
    assert all(".000+00:00" in value for row in rows for value in (row["interval_start_utc"], row["interval_end_utc"]))

    overlap = [row for row in rows if row["shift_bp"] == "30" and row["pair"] == "BTCUSDT"]
    assert {row["status"] for row in overlap} == {"AVAILABLE", "EXCLUDED"}
    excluded = next(row for row in overlap if row["status"] == "EXCLUDED")
    assert excluded["reason_code"] == "OVERLAP_NOT_SELECTED"
    assert excluded["reason_detail"] == "OVERLAP_NOT_SELECTED: selected_by_tiebreak=true"

    missing = next(row for row in rows if row["row_type"] == "READINESS_GAP")
    assert missing["shift_bp"] == ""
    assert missing["report_id"] == ""
    assert missing["source_sha256"] == ""
    assert missing["selected_report"] == ""
    assert missing["status"] == "MISSING"
    assert missing["reason_code"] == "MISSING_SHIFT"
    assert missing["reason_detail"] == "MISSING_SHIFT: missing_shifts=550"
    assert missing["pair"] == "ETHUSDT"
    reproduced = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    )
    assert reproduced == csv_bytes
    digest = sha256(csv_bytes).hexdigest()
    assert len(digest) == 64
    assert digest.islower()


def test_canonical_json_rejects_every_float_recursively() -> None:
    with pytest.raises(ValueError, match="floats"):
        _canonical_json_bytes({"nested": [1.5]})
    with pytest.raises(ValueError, match="floats"):
        _canonical_json_bytes({"nested": {"finite": 0.25}})
    with pytest.raises(ValueError, match="floats"):
        _canonical_json_bytes([True, None, 1.0])

    assert _canonical_json_bytes({"ok": [True, None, 3]}) == b'{"ok":[true,null,3]}'


def test_canonical_json_rejects_finite_nan_and_infinite_nested_floats() -> None:
    for value in (1.5, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="floats"):
            _canonical_json_bytes({"nested": {"value": value}})


def test_coverage_csv_quotes_witness_cells_and_reproduces_bytes(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma)

    csv_bytes = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    )
    csv_text = csv_bytes.decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(csv_text)))

    assert not csv_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in csv_bytes
    witness_row = next(row for row in rows if row["required_for_readiness"] == "true")
    assert witness_row["readiness_witness"] == ",".join(map(str, READY_SHIFTS))
    assert witness_row["readiness_contract_version"] == READINESS_CONTRACT_VERSION
    assert witness_row["readiness_max_shift_bp"] == str(READINESS_MAX_SHIFT_BP)
    assert csv_bytes == coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    )
    digest = sha256(csv_bytes).hexdigest()
    assert len(digest) == 64
    assert digest.islower()


def test_coverage_csv_sorts_long_before_short_and_reproduces_bytes(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(source, symbol="BTCUSDT", shifts=(30, 41, 150, 430))
    _seed_readiness_scope(source, symbol="BTCUSDT", side="SHORT", shifts=(30, 41, 150, 430))

    csv_bytes = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    )

    assert csv_bytes.find(b"BTCUSDT,LONG,1h") < csv_bytes.find(b"BTCUSDT,SHORT,1h")
    assert csv_bytes == coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    )


def test_publication_audit_ignores_unselected_timeframe_source_changes(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma)
        for shift in READY_SHIFTS:
            _seed_report(
                source,
                symbol="BTCUSDT",
                timeframe="4h",
                shift=shift,
                open_ma=3,
                close_ma=close_ma,
                source_hash=_hash("initial-4h", "BTCUSDT", "LONG", shift, 3, close_ma, START_MS, END_MS),
            )

    def audit_bytes() -> bytes:
        return coverage_audit_csv_bytes(
            source,
            "2024-01-01T00:00:00+00:00",
            "2024-01-01T02:00:00+00:00",
            side="LONG",
            symbols=("BTCUSDT",),
            selected_scopes=("BTCUSDT|1h",),
        )

    selected_audit = audit_bytes()
    selected_rows = list(csv.DictReader(io.StringIO(selected_audit.decode("utf-8"))))
    assert {row["timeframe"] for row in selected_rows} == {"1h"}

    source.execute(
        "delete from report_payloads where report_id in "
        "(select r.report_id from active_reports r join point_configs p using(canonical_point_key) "
        "where p.timeframe='4h')"
    )
    source.execute(
        "delete from active_reports where canonical_point_key in "
        "(select canonical_point_key from point_configs where timeframe='4h')"
    )
    for close_ma in REQUIRED_CLOSE_MAS:
        for shift in READY_SHIFTS:
            _seed_report(
                source,
                symbol="BTCUSDT",
                timeframe="4h",
                shift=shift,
                open_ma=3,
                close_ma=close_ma,
                source_hash=_hash("changed-4h", "BTCUSDT", "LONG", shift, 3, close_ma, START_MS, END_MS),
            )

    changed_audit = audit_bytes()
    assert changed_audit == selected_audit

    def v2_request(audit: bytes) -> DirectBuildRequest:
        return _request(
            grid_contract_kind=V2_GRID_CONTRACT_KIND,
            selected_scopes=("BTCUSDT|1h",),
            required_shifts_bp=READY_SHIFTS,
            materializer_version=CANONICAL_MATERIALIZER_VERSION,
            point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
            readiness_contract_version=READINESS_CONTRACT_VERSION,
            readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
            audit_artifact_name="surface_coverage_audit_LONG.csv",
            audit_schema_version=1,
            audit_size_bytes=len(audit),
            audit_row_count=len(list(csv.DictReader(io.StringIO(audit.decode("utf-8"))))),
            audit_sha256=sha256(audit).hexdigest(),
            audit_bytes=audit,
        )

    before = preflight_duckdb_direct(source, v2_request(selected_audit))
    after = preflight_duckdb_direct(source, v2_request(changed_audit))
    assert before.audit_sha256 == after.audit_sha256
    assert before.point_evidence_sha256 == after.point_evidence_sha256
    assert dict(before.grid_contract) == dict(after.grid_contract)


def test_coverage_scan_returns_long_and_short_groups(connections) -> None:
    source, _ = connections
    _seed_report(source, symbol="BTCUSDT", source_hash="long")
    _seed_report(source, symbol="BTCUSDT", side="SHORT", source_hash="short")

    coverage = list_duckdb_direct_coverage(source, symbols=())

    assert [(scope.symbol, scope.side, scope.timeframe) for scope in coverage.scopes] == [
        ("BTCUSDT", "LONG", "1h"),
        ("BTCUSDT", "SHORT", "1h"),
    ]
    assert len(coverage.rows) == 2


def _seed_ready_scope(
    source: duckdb.DuckDBPyConnection,
    *,
    side: str = 'LONG',
) -> None:
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(
            source,
            symbol='BTCUSDT',
            side=side,
            shifts=READY_SHIFTS,
            close_ma=close_ma,
        )


def _coverage_build_request(
    scan: _CoverageScan,
    *,
    symbol: str = 'BTCUSDT',
    side: str = 'LONG',
    timeframe: str = '1h',
) -> DirectBuildRequest:
    interval = common_intervals_for_scopes(
        scan.coverage,
        (DirectScope(symbol, side, timeframe),),
    )[0]
    return _request(
        start_utc=interval.start_utc,
        end_utc=interval.end_utc,
        side=side,
        symbols=(symbol,),
        required_shifts_bp=READY_SHIFTS,
        selected_scopes=(f'{symbol}|{timeframe}',),
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        materializer_version=CANONICAL_MATERIALIZER_VERSION,
        point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
        readiness_contract_version=READINESS_CONTRACT_VERSION,
        readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
    )


def test_canonical_phase1_smoke_preflight_materialization_and_publication(
    connections, tmp_path: Path
) -> None:
    source, analysis = connections
    _seed_ready_scope(source, side="LONG")
    actions = (
        {"Timestamp": "2024-01-01T00:10:00Z", "Symbol": "BTCUSDT", "Action": "opened", "Post Side": "long", "Side": "buy", "PnL": "0"},
        {"Timestamp": "2024-01-01T01:00:00Z", "Symbol": "BTCUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "2"},
    )
    _seed_report(source, shift=30, open_ma=4, close_ma=10, actions=actions, source_hash="f" * 64)

    scan = coverage_scan_direct(source, tmp_path, symbols=())
    request = replace(_coverage_build_request(scan), grid_contract_kind="", materializer_version="")
    (surface,) = prepare_direct_surfaces(source, (request,), audit_root=tmp_path, coverage_scan=scan)

    assert READY_SHIFTS == (30, 40, 50, 60, 70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550)
    assert surface.request.grid_contract_kind == V2_GRID_CONTRACT_KIND
    assert surface.request.materializer_version == CANONICAL_MATERIALIZER_VERSION
    contract = surface.preflight.grid_contract
    assert contract["kind"] == V2_GRID_CONTRACT_KIND
    assert contract["canonical_grid_version"] == CANONICAL_GRID_VERSION
    assert contract["canonical_shifts_bp"] == list(READY_SHIFTS)
    assert contract["point_materialization_semantics_version"] == POINT_MATERIALIZATION_SEMANTICS_VERSION
    assert contract["point_materialization_config_hash"] == canonical_point_materialization_config_hash(READY_SHIFTS)
    witnesses = contract["witnesses"]["BTCUSDT|1h"]
    assert [w["close_ma"] for w in witnesses] == list(REQUIRED_CLOSE_MAS)
    assert all(w["open_ma"] == 3 for w in witnesses)
    assert all(w["shifts_bp"] == list(READY_SHIFTS) for w in witnesses)
    assert all(w["contract_version"] == READINESS_CONTRACT_VERSION for w in witnesses)
    assert all(w["max_shift_bp"] == READINESS_MAX_SHIFT_BP for w in witnesses)
    expected_points = len(READY_SHIFTS) * len(REQUIRED_CLOSE_MAS) + 1
    assert len(surface.preflight.accepted_point_keys) == expected_points

    assert surface.event_mode == REAL_EVENT_MODE
    assert surface.build_mode == "DUCKDB_DIRECT"
    points_by_key = {point.canonical_point_key: point for point in surface.points}
    assert len(points_by_key) == expected_points
    assert all(
        point.point_event_count == len(point.event_ids) and tuple(sorted(set(point.event_ids))) == point.event_ids
        for point in surface.points
    )
    witness_point = points_by_key["BTCUSDT|LONG|1h|30|3|2"]
    assert witness_point.point_event_count == 0
    assert witness_point.event_ids == ()
    event_point = points_by_key["BTCUSDT|LONG|1h|30|4|10"]
    assert event_point.point_event_count == 1
    assert event_point.event_ids == (canonical_event_id("BTCUSDT", "long", "1h", "2024-01-01T00:10:00Z"),)

    result = publish_direct_surfaces(analysis, (surface,), audit_root=tmp_path)
    assert result.publication_state == "PUBLISHED"
    assert result.surfaces[0].created is True
    assert tuple(point.canonical_point_key for point in result.surfaces[0].points) == tuple(
        point.canonical_point_key for point in surface.points
    )
    assert ensure_analysis_schema(analysis) == ANALYSIS_SCHEMA_VERSION == 4
    stored = analysis.execute(
        "select build_mode, event_mode, materializer_version, point_materialization_config_hash from surfaces"
    ).fetchone()
    assert stored == (
        "DUCKDB_DIRECT",
        REAL_EVENT_MODE,
        CANONICAL_MATERIALIZER_VERSION,
        canonical_point_materialization_config_hash(READY_SHIFTS),
    )


def test_unchanged_source_scan_token_and_prepare_match_preview(
    connections, tmp_path: Path
) -> None:
    source, _ = connections
    _seed_ready_scope(source, side='LONG')
    _seed_ready_scope(source, side='SHORT')

    scan = coverage_scan_direct(source, tmp_path, symbols=())
    active_scan = coverage_scan_direct(source, tmp_path, symbols=())
    assert active_scan.token == scan.token
    assert active_scan.source_evidence_sha256 == scan.source_evidence_sha256
    assert active_scan.inventory_sha256 == scan.inventory_sha256

    scopes = (
        DirectScope('BTCUSDT', 'LONG', '1h'),
        DirectScope('BTCUSDT', 'SHORT', '1h'),
    )
    preview_intervals = common_intervals_for_scopes(active_scan.coverage, scopes)
    requests = tuple(
        replace(
            _coverage_build_request(active_scan, side=scope.side),
            grid_contract_kind="",
            materializer_version="",
        )
        for scope in scopes
    )
    surfaces = prepare_direct_surfaces(
        source,
        requests,
        audit_root=tmp_path,
        coverage_scan=scan,
    )
    assert [surface.request.side for surface in surfaces] == ['LONG', 'SHORT']
    for request, interval, surface in zip(requests, preview_intervals, surfaces, strict=True):
        assert request.start_utc == interval.start_utc
        assert request.end_utc == interval.end_utc
        assert request.selected_scopes == ('BTCUSDT|1h',)
        assert surface.request.grid_contract_kind == V2_GRID_CONTRACT_KIND
        assert surface.request.readiness_contract_version == READINESS_CONTRACT_VERSION
        assert surface.request.readiness_max_shift_bp == READINESS_MAX_SHIFT_BP
    for surface, request in zip(surfaces, requests, strict=True):
        assert surface.request.start_utc == request.start_utc
        assert surface.request.end_utc == request.end_utc
        assert surface.request.side == request.side
        assert surface.request.symbols == request.symbols
        assert surface.request.selected_scopes == request.selected_scopes
        assert surface.request.grid_contract_kind == V2_GRID_CONTRACT_KIND
        assert surface.request.readiness_contract_version == READINESS_CONTRACT_VERSION
        assert surface.request.readiness_max_shift_bp == READINESS_MAX_SHIFT_BP


def test_changed_source_fails_before_materializer(
    connections, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = connections
    _seed_ready_scope(source)
    scan = coverage_scan_direct(source, tmp_path, symbols=())
    request = replace(
        _coverage_build_request(scan),
        grid_contract_kind="",
        materializer_version="",
    )
    _seed_report(source, symbol='ETHUSDT', source_hash='changed-source')
    materialized: list[object] = []

    def fake_materialize(*args: object, **kwargs: object) -> DirectSurface:
        materialized.append((args, kwargs))
        raise AssertionError('materializer must not run after a changed source')

    monkeypatch.setattr('mrs3.duckdb_direct.materialize_duckdb_direct', fake_materialize)

    with pytest.raises(DirectMaterializationError, match='active coverage scan changed after preflight'):
        prepare_direct_surfaces(
            source,
            (request,),
            audit_root=tmp_path,
            coverage_scan=scan,
        )

    assert materialized == []


def test_prepare_direct_surfaces_rejects_v2_without_frozen_selected_state(
    connections, tmp_path: Path
) -> None:
    source, _ = connections
    _seed_ready_scope(source, side='LONG')

    scan = coverage_scan_direct(source, tmp_path, symbols=())
    request = _coverage_build_request(scan)

    with pytest.raises(DirectMaterializationError, match='STALE_PREFLIGHT'):
        prepare_direct_surfaces(
            source,
            (request,),
            audit_root=tmp_path,
            coverage_scan=scan,
        )


@pytest.mark.parametrize(
    'mutate_coverage',
    (
        lambda coverage: replace(
            coverage,
            rows=tuple(
                replace(row, interval_end_utc='2099-01-01T00:00:00.000+00:00')
                for row in coverage.rows
            ),
        ),
        lambda coverage: replace(
            coverage,
            intervals=tuple(
                replace(
                    interval,
                    witnesses=tuple(
                        replace(
                            witness,
                            open_ma=4,
                            close_ma=10,
                            shifts_bp=(30, 150, 430),
                            contract_version='shift_readiness_v2',
                            max_shift_bp=500,
                        )
                        for witness in interval.witnesses
                    ),
                )
                if interval.witnesses else interval
                for interval in coverage.intervals
            ),
        ),
    ),
    ids=('coverage-row', 'witness-contract'),
)
def test_coverage_token_changes_when_canonical_coverage_inputs_change(
    connections,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate_coverage,
) -> None:
    source, _ = connections
    _seed_ready_scope(source)
    baseline = coverage_scan_direct(source, tmp_path, symbols=())
    baseline_inventory = coverage_inventory_csv_bytes(source, symbols=())
    monkeypatch.setattr(
        'mrs3.duckdb_direct._direct_coverage',
        lambda _rows: mutate_coverage(baseline.coverage),
    )
    monkeypatch.setattr(
        'mrs3.duckdb_direct.coverage_inventory_csv_bytes',
        lambda *args, **kwargs: baseline_inventory,
    )
    changed = coverage_scan_direct(source, tmp_path, symbols=())

    assert changed.token != baseline.token
    assert changed.inventory_sha256 == baseline.inventory_sha256
    assert changed.source_evidence_sha256 == baseline.source_evidence_sha256


def test_coverage_token_changes_when_inventory_changes(
    connections, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = connections
    _seed_ready_scope(source)
    baseline = coverage_scan_direct(source, tmp_path, symbols=())
    baseline_inventory = coverage_inventory_csv_bytes(source, symbols=())
    monkeypatch.setattr(
        'mrs3.duckdb_direct.coverage_inventory_csv_bytes',
        lambda *args, **kwargs: baseline_inventory + b'changed',
    )
    changed = coverage_scan_direct(source, tmp_path, symbols=())

    assert changed.coverage == baseline.coverage
    assert changed.inventory_sha256 != baseline.inventory_sha256
    assert changed.source_evidence_sha256 == baseline.source_evidence_sha256
    assert changed.token != baseline.token


def test_coverage_token_changes_when_source_evidence_changes(
    connections, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = connections
    _seed_ready_scope(source)
    baseline = coverage_scan_direct(source, tmp_path, symbols=())
    baseline_inventory = coverage_inventory_csv_bytes(source, symbols=())
    rows = [dict(row) for row in _coverage_scan_rows(source, side=None, symbols=())]
    assert rows
    rows[0]['source_sha256'] = 'changed-source-evidence'.rjust(64, '0')
    monkeypatch.setattr(
        'mrs3.duckdb_direct._coverage_scan_rows',
        lambda *args, **kwargs: rows,
    )
    monkeypatch.setattr(
        'mrs3.duckdb_direct.coverage_inventory_csv_bytes',
        lambda *args, **kwargs: baseline_inventory,
    )
    changed = coverage_scan_direct(source, tmp_path, symbols=())

    assert changed.coverage == baseline.coverage
    assert changed.inventory_sha256 == baseline.inventory_sha256
    assert changed.source_evidence_sha256 != baseline.source_evidence_sha256
    assert changed.token != baseline.token


def test_v2_preflight_selects_narrowest_reports_and_includes_every_factual_point(connections) -> None:
    source, _ = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma)
    _seed_report(source, shift=500, source_hash="optional-500")
    _seed_report(source, shift=600, open_ma=4, close_ma=10, source_hash="optional-600")
    _seed_report(
        source,
        shift=30,
        open_ma=3,
        close_ma=2,
        start_ms=START_MS - 3_600_000,
        end_ms=END_MS + 3_600_000,
        source_hash="overlap-long",
    )

    csv_bytes = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
        symbols=("BTCUSDT",),
    )
    request = _request(
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        selected_scopes=("BTCUSDT|1h",),
        required_shifts_bp=READY_SHIFTS,
        materializer_version=CANONICAL_MATERIALIZER_VERSION,
        point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
        readiness_contract_version=READINESS_CONTRACT_VERSION,
        readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
        audit_artifact_name="surface_coverage_audit_LONG.csv",
        audit_schema_version=1,
        audit_size_bytes=len(csv_bytes),
        audit_row_count=len(list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))),
        audit_sha256=sha256(csv_bytes).hexdigest(),
        audit_bytes=csv_bytes,
    )

    preflight = preflight_duckdb_direct(source, request)

    assert preflight.usable_timeframes == {"BTCUSDT": ("1h",)}
    assert preflight.grid_contract["kind"] == V2_GRID_CONTRACT_KIND
    assert preflight.grid_contract["selected_scopes"] == ["BTCUSDT|1h"]
    witnesses = preflight.grid_contract["witnesses"]["BTCUSDT|1h"]
    assert [w["close_ma"] for w in witnesses] == list(REQUIRED_CLOSE_MAS)
    assert all(w["symbol"] == "BTCUSDT" for w in witnesses)
    assert all(w["side"] == "LONG" for w in witnesses)
    assert all(w["timeframe"] == "1h" for w in witnesses)
    assert all(w["open_ma"] == 3 for w in witnesses)
    assert all(w["shifts_bp"] == list(READY_SHIFTS) for w in witnesses)
    assert all(w["contract_version"] == READINESS_CONTRACT_VERSION for w in witnesses)
    assert all(w["max_shift_bp"] == READINESS_MAX_SHIFT_BP for w in witnesses)
    assert all(
        set(w) == {"symbol", "side", "timeframe", "open_ma", "close_ma", "shifts_bp", "contract_version", "max_shift_bp"}
        for w in witnesses
    )
    assert len(preflight.accepted_point_keys) == len(READY_SHIFTS) * len(REQUIRED_CLOSE_MAS)
    assert not any("|500|" in key for key in preflight.accepted_point_keys)
    assert not any("|600|4|10" in key for key in preflight.accepted_point_keys)
    assert len(preflight.manifest) == len(READY_SHIFTS) * len(REQUIRED_CLOSE_MAS)
    assert "overlap-long" not in {hash for _, hash in preflight.manifest}
    assert preflight.point_evidence_sha256 == sha256(
        preflight.grid_contract["point_evidence"].encode("utf-8")
    ).hexdigest()
    assert preflight.audit_sha256 == sha256(csv_bytes).hexdigest()


def test_materialization_retains_factual_non_witness_points_on_canonical_shifts(connections) -> None:
    source, analysis = connections
    for close_ma in REQUIRED_CLOSE_MAS:
        _seed_readiness_scope(source, symbol="BTCUSDT", shifts=READY_SHIFTS, close_ma=close_ma)
    _seed_report(source, shift=110, open_ma=5, close_ma=10, source_hash="factual-nonwitness")
    _seed_report(source, shift=600, open_ma=3, close_ma=9, source_hash="noncanonical-600")

    csv_bytes = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
        symbols=("BTCUSDT",),
    )
    request = _request(
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        selected_scopes=("BTCUSDT|1h",),
        required_shifts_bp=READY_SHIFTS,
        materializer_version=CANONICAL_MATERIALIZER_VERSION,
        point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
        readiness_contract_version=READINESS_CONTRACT_VERSION,
        readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
        audit_artifact_name="surface_coverage_audit_LONG.csv",
        audit_schema_version=1,
        audit_size_bytes=len(csv_bytes),
        audit_row_count=len(list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))),
        audit_sha256=sha256(csv_bytes).hexdigest(),
        audit_bytes=csv_bytes,
    )

    preflight = preflight_duckdb_direct(source, request)

    expected_total = len(READY_SHIFTS) * len(REQUIRED_CLOSE_MAS) + 1
    assert len(preflight.accepted_point_keys) == expected_total
    assert "BTCUSDT|LONG|1h|110|5|10" in preflight.accepted_point_keys
    assert not any("|600|" in key for key in preflight.accepted_point_keys)

    surface = materialize_duckdb_direct(source, analysis, request, lambda: False, preflight=preflight)
    keys = tuple(point.canonical_point_key for point in surface.points)
    assert len(keys) == expected_total
    assert "BTCUSDT|LONG|1h|110|5|10" in keys
    assert not any("|600|" in key for key in keys)


def test_v2_point_evidence_jsonl_has_decoded_key_order_lf_canonical_types_and_exact_sha256() -> None:
    points = (
        DirectPoint(
            "BTCUSDT|LONG|1h|100|3|9",
            "report-100",
            "a" * 64,
            0,
            {"TotalTrades": 0, "TotalPnLPercent": 0, "MaxDrawdownPercent": 0, "Win": 0, "Los": 0, "WinRate": 0, "ProfitFactor": None},
        ),
        DirectPoint(
            "BTCUSDT|LONG|1h|30|3|9",
            "report-30",
            "b" * 64,
            0,
            {"TotalTrades": 0, "TotalPnLPercent": 0, "MaxDrawdownPercent": 0, "Win": 0, "Los": 0, "WinRate": 0, "ProfitFactor": None},
        ),
    )

    payload = point_evidence_jsonl_bytes(points)
    lines = payload.splitlines()
    record_30 = {"point_key": "BTCUSDT|LONG|1h|30|3|9", "report_id": "report-30", "source_sha256": "b" * 64}
    record_100 = {"point_key": "BTCUSDT|LONG|1h|100|3|9", "report_id": "report-100", "source_sha256": "a" * 64}

    assert payload.endswith(b"\n")
    assert len(lines) == 2
    assert lines[0] == _canonical_json_bytes(record_30)
    assert lines[1] == _canonical_json_bytes(record_100)
    assert payload == _canonical_json_bytes(record_30) + b"\n" + _canonical_json_bytes(record_100) + b"\n"
    assert sha256(payload).hexdigest() == "7e4d84bcb1f462955a85b2550c066d5bc46f3742a448e2b8f82d4abd6306c8c8"



def test_coverage_artifact_write_is_atomic_and_hash_verified(tmp_path) -> None:
    data = b"pair,side\na,LONG\n"
    path = write_coverage_artifact(tmp_path, "surface_coverage/audit_long.csv", data)

    assert path == tmp_path / "surface_coverage" / "audit_long.csv"
    assert path.read_bytes() == data
    assert sha256(data).hexdigest() == sha256(path.read_bytes()).hexdigest()

    text_path = write_coverage_artifact(tmp_path, "surface_coverage/inventory.csv", "pair\n")
    assert text_path.read_bytes() == b"pair\n"

    with pytest.raises(DirectMaterializationError, match="relative"):
        write_coverage_artifact(tmp_path, "../escape.csv", b"")


def test_coverage_artifact_replace_failure_preserves_target_and_cleans_temp(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "audit.csv"
    target.write_bytes(b"old")

    def fail_replace(_source: str | os.PathLike[str], _destination: str | os.PathLike[str]) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("mrs3.duckdb_direct.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_coverage_artifact(tmp_path, "audit.csv", b"new")

    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_coverage_artifact_readback_mismatch_fails_hash_verification(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "audit.csv"
    original_read_bytes = Path.read_bytes

    def corrupt_read(path: Path) -> bytes:
        return b"corrupt" if path == target else original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", corrupt_read)

    with pytest.raises(DirectMaterializationError, match="hash verification failed"):
        write_coverage_artifact(tmp_path, "audit.csv", b"payload")
    assert not any(path.name.startswith(".") for path in tmp_path.iterdir())


def test_coverage_artifact_hash_mismatch_fails_hash_verification(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "audit.csv"
    real_sha256 = hashlib.sha256

    calls = 0

    class FakeDigest:
        def __init__(self, value: bytes) -> None:
            self._value = value

        def hexdigest(self) -> str:
            nonlocal calls
            calls += 1
            return real_sha256(self._value).hexdigest() if calls == 1 else "0" * 64

    def bad_sha256(value: bytes) -> FakeDigest:
        return FakeDigest(value)

    monkeypatch.setattr("mrs3.duckdb_direct.hashlib.sha256", bad_sha256)

    with pytest.raises(DirectMaterializationError, match="hash verification failed"):
        write_coverage_artifact(tmp_path, "audit.csv", b"payload")
    assert target.read_bytes() == b"payload"


def test_preflight_rejects_each_unusable_selected_timeframe(connections) -> None:
    source, _ = connections
    _seed_report(source, timeframe="1h", shift=100, source_hash="a" * 64)
    _seed_report(source, timeframe="1h", shift=200, source_hash="b" * 64)
    _seed_report(source, timeframe="4h", shift=100, source_hash="c" * 64)
    request = _request(
        required_shifts_bp=(100, 200),
        selected_scopes=("BTCUSDT|1h", "BTCUSDT|4h"),
    )

    preflight = preflight_duckdb_direct(source, request)

    assert preflight.usable_timeframes == {"BTCUSDT": ("1h",)}
    assert preflight.unavailable_symbols == {"BTCUSDT": ("4h",)}


def test_materialization_uses_real_independent_events_and_does_not_publish_to_analysis(connections) -> None:
    source, analysis = connections
    actions = (
        {"Timestamp": "2024-01-01T00:10:00Z", "Symbol": "BTCUSDT", "Action": "opened", "Post Side": "long", "Side": "buy", "PnL": "0"},
        {"Timestamp": "2024-01-01T00:30:00Z", "Symbol": "BTCUSDT", "Action": "decreased", "Post Side": "long", "Side": "sell", "PnL": "1"},
        {"Timestamp": "2024-01-01T01:00:00Z", "Symbol": "BTCUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "2"},
    )
    point = _seed_report(source, actions=actions)

    surface = materialize_duckdb_direct(source, analysis, _request(), lambda: False)

    assert surface.event_mode == "real_independent_events"
    assert surface.points[0].canonical_point_key == point
    assert surface.points[0].point_event_count == 1
    assert surface.points[0].event_ids == (
        canonical_event_id("BTCUSDT", "long", "1h", "2024-01-01T00:10:00Z"),
    )
    assert surface.points[0].metrics["TotalTrades"] == 2
    assert analysis.execute("select count(*) from information_schema.tables").fetchone() == (0,)


def test_canonical_event_id_normalizes_equivalent_utc_timestamps() -> None:
    expected = canonical_event_id("BTCUSDT", "long", "1h", "2024-01-01T00:10:00Z")

    assert canonical_event_id("BTCUSDT", "long", "1h", "2023-12-31T19:10:00-05:00") == expected
    assert canonical_event_id("BTCUSDT", "long", "1h", "2024-01-01T00:10:00.000000+00:00") == expected


def test_materializer_reuses_event_id_across_points_only_for_same_opening(connections) -> None:
    source, analysis = connections

    def cycle(opened_at: str) -> tuple[dict[str, str], ...]:
        return (
            {"Timestamp": opened_at, "Symbol": "BTCUSDT", "Action": "opened", "Post Side": "long", "Side": "buy", "PnL": "0"},
            {"Timestamp": "2024-01-01T01:00:00Z", "Symbol": "BTCUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "2"},
        )

    _seed_report(source, shift=100, actions=cycle("2024-01-01T00:10:00Z"))
    _seed_report(source, shift=200, actions=cycle("2023-12-31T19:10:00-05:00"))
    _seed_report(source, shift=300, actions=cycle("2024-01-01T00:20:00Z"))

    surface = materialize_duckdb_direct(
        source, analysis, _request(required_shifts_bp=(100, 200, 300)), lambda: False
    )
    event_by_shift = {
        int(point.canonical_point_key.split("|")[3]): point.event_ids[0]
        for point in surface.points
    }

    assert event_by_shift[100] == event_by_shift[200]
    assert event_by_shift[100] != event_by_shift[300]


def _materialization_payload(
    *,
    canonical_point_key: str,
    report_id: str,
    source_hash: str,
    grid_hash: str,
    timestamps_blob: bytes,
    actions: tuple[dict[str, str], ...] = (),
) -> MaterializationPayload:
    import struct, zlib
    headers = sorted({key for action in actions for key in action})
    action_payload = {"headers": headers, "rows": [[action.get(header, "") for header in headers] for action in actions]}
    return MaterializationPayload(
        canonical_point_key=canonical_point_key,
        report_id=report_id,
        source_hash=source_hash,
        action_count=len(actions),
        equity_count=3,
        wallet_count=1,
        series_codec=EQUITY_CODEC,
        actions_blob=zlib.compress(json.dumps(action_payload).encode()),
        equity_blob=zlib.compress(struct.pack("<3q", 100, 90, 110)),
        wallet_blob=zlib.compress(struct.pack("<Iq", 0, 100)),
        grid_hash=grid_hash,
        sample_count=3,
        timestamps_blob=timestamps_blob,
    )


def _shared_materialization_payloads() -> tuple[MaterializationPayload, ...]:
    import struct, zlib
    grid_hash = _hash("shared-materialization-grid")
    timestamps = (START_MS, (START_MS + END_MS) // 2, END_MS)
    timestamps_blob = zlib.compress(
        struct.pack("<3q", timestamps[0], timestamps[1] - timestamps[0], timestamps[2] - timestamps[1])
    )
    actions = (
        {"Timestamp": "2024-01-01T00:10:00Z", "Symbol": "BTCUSDT", "Action": "opened", "Post Side": "long", "Side": "buy", "PnL": "0"},
        {"Timestamp": "2024-01-01T00:30:00Z", "Symbol": "BTCUSDT", "Action": "decreased", "Post Side": "long", "Side": "sell", "PnL": "1"},
        {"Timestamp": "2024-01-01T01:00:00Z", "Symbol": "BTCUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "2"},
    )
    return (
        _materialization_payload(
            canonical_point_key="BTCUSDT|LONG|1h|100|3|9", report_id="report-a", source_hash="a" * 64,
            grid_hash=grid_hash, timestamps_blob=timestamps_blob, actions=actions,
        ),
        _materialization_payload(
            canonical_point_key="BTCUSDT|LONG|1h|200|3|9", report_id="report-b", source_hash="b" * 64,
            grid_hash=grid_hash, timestamps_blob=timestamps_blob, actions=actions,
        ),
    )


def test_materialization_payload_chunk_caches_shared_grid_and_matches_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = _shared_materialization_payloads()
    timestamps_blob = payloads[0].timestamps_blob
    calls: list[tuple[bytes, int]] = []
    original = duckdb_direct.decode_compact_deltas

    def counting_delta_decode(blob: bytes, expected_count: int, *, codec: str = EQUITY_CODEC) -> tuple[int, ...]:
        calls.append((bytes(blob), int(expected_count)))
        return original(blob, expected_count, codec=codec)

    monkeypatch.setattr("mrs3.duckdb_direct.decode_compact_deltas", counting_delta_decode)

    points = _materialize_payload_chunk(payloads, "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z")

    assert [point.canonical_point_key for point in points] == [payload.canonical_point_key for payload in payloads]
    assert sum(1 for blob, _ in calls if blob == timestamps_blob) == 1
    assert len(calls) == 3  # shared timestamps grid decodes once, equity decodes per payload
    expected_event_ids = (canonical_event_id("BTCUSDT", "long", "1h", "2024-01-01T00:10:00Z"),)
    for point, payload in zip(points, payloads, strict=True):
        assert point.source_report_id == payload.report_id
        assert point.source_hash == payload.source_hash
        assert point.event_ids == expected_event_ids
        assert point.point_event_count == 1
        assert point.metrics["TotalTrades"] == 2
    assert points[0].metrics == points[1].metrics
    assert points[0].event_ids == points[1].event_ids


def test_materialization_payload_chunk_is_picklable() -> None:
    payloads = _shared_materialization_payloads()
    assert pickle.loads(pickle.dumps(payloads)) == payloads
    worker = pickle.loads(pickle.dumps(_materialize_payload_chunk))
    points = worker(payloads, "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z")
    assert [point.canonical_point_key for point in points] == [payload.canonical_point_key for payload in payloads]


def test_materialization_parallel_worker_counts_produce_identical_output() -> None:
    payloads = _shared_materialization_payloads()
    window_start_utc = "2024-01-01T00:00:00Z"
    window_end_utc = "2024-01-01T02:00:00Z"

    serial_points = _materialize_payloads_parallel(
        payloads,
        DirectMaterializationSettings(workers=1, worker_chunk_size=1, max_in_flight_chunks=1),
        window_start_utc,
        window_end_utc,
        lambda: False,
    )
    parallel_points = _materialize_payloads_parallel(
        payloads,
        DirectMaterializationSettings(workers=15, worker_chunk_size=1, max_in_flight_chunks=15),
        window_start_utc,
        window_end_utc,
        lambda: False,
    )

    assert [point.canonical_point_key for point in serial_points] == [
        "BTCUSDT|LONG|1h|100|3|9",
        "BTCUSDT|LONG|1h|200|3|9",
    ]
    assert [asdict(point) for point in serial_points] == [
        asdict(point) for point in parallel_points
    ]


def test_canonical_semantic_payload_exact_fixture_and_digest() -> None:
    payload = canonical_point_materialization_semantic_payload(DEFAULT_CANONICAL_SHIFTS_BP)
    assert payload == {
        "canonical_grid_version": CANONICAL_GRID_VERSION,
        "canonical_shifts_bp": list(DEFAULT_CANONICAL_SHIFTS_BP),
        "event_id_contract": "sha256_utf8_pipe(symbol,position_side,timeframe,opened_at_utc_ns)",
        "event_mode": "real_independent_events",
        "materialization_scope_contract": "fully_covering_selected_scope_points_on_exact_canonical_shifts",
        "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION,
        "point_event_count_contract": "count_unique_sorted_canonical_event_ids",
        "readiness_contract_version": READINESS_CONTRACT_VERSION,
        "required_close_mas": list(REQUIRED_CLOSE_MAS),
        "semantic_contract_version": POINT_MATERIALIZATION_SEMANTICS_VERSION,
        "window_contract": "utc_half_open_[start,end)",
    }
    canonical_json_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    assert canonical_json_bytes == (
        b'{"canonical_grid_version":"mrs3_shift_grid_30_550_v1",'
        b'"canonical_shifts_bp":[30,40,50,60,70,90,110,140,170,200,230,270,310,350,390,430,470,510,550],'
        b'"event_id_contract":"sha256_utf8_pipe(symbol,position_side,timeframe,opened_at_utc_ns)",'
        b'"event_mode":"real_independent_events",'
        b'"materialization_scope_contract":"fully_covering_selected_scope_points_on_exact_canonical_shifts",'
        b'"normalization_contract_version":"shift-bp-v1",'
        b'"point_event_count_contract":"count_unique_sorted_canonical_event_ids",'
        b'"readiness_contract_version":"close_ma_2_7_canonical_grid_v1",'
        b'"required_close_mas":[2,3,4,5,6,7],'
        b'"semantic_contract_version":"direct_point_materialization_v1",'
        b'"window_contract":"utc_half_open_[start,end)"}'
    )
    assert canonical_point_materialization_config_hash(DEFAULT_CANONICAL_SHIFTS_BP) == (
        "5391334fb82d78439e32b6f70a055bbc67f551efbd84f63670a64e5e98464903"
    )


def test_semantic_identity_ignores_materialization_worker_settings(
    connections, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = connections
    _seed_ready_scope(source)
    scan = coverage_scan_direct(source, tmp_path, symbols=())
    request = replace(
        _coverage_build_request(scan),
        grid_contract_kind="",
        materializer_version="",
    )

    def fake_materialize(*args: object, **kwargs: object) -> DirectSurface:
        return DirectSurface(args[2], kwargs["preflight"], REAL_EVENT_MODE, ())

    monkeypatch.setattr("mrs3.duckdb_direct.materialize_duckdb_direct", fake_materialize)
    serial = prepare_direct_surfaces(
        source,
        (request,),
        audit_root=tmp_path,
        coverage_scan=scan,
        materialization_settings=DirectMaterializationSettings(
            workers=1, worker_chunk_size=1, max_in_flight_chunks=1
        ),
    )[0]
    parallel = prepare_direct_surfaces(
        source,
        (request,),
        audit_root=tmp_path,
        coverage_scan=scan,
        materialization_settings=DirectMaterializationSettings(
            workers=15, worker_chunk_size=16, max_in_flight_chunks=15
        ),
    )[0]
    expected_hash = canonical_point_materialization_config_hash(DEFAULT_CANONICAL_SHIFTS_BP)
    assert serial.request.point_materialization_config_hash == expected_hash
    assert parallel.request.point_materialization_config_hash == expected_hash
    assert serial.preflight.grid_contract["point_materialization_config_hash"] == expected_hash
    assert (
        parallel.preflight.grid_contract["point_materialization_config_hash"]
        == serial.preflight.grid_contract["point_materialization_config_hash"]
    )


class _DeferredExecutor:
    """Fake pool executor returning externally completed futures."""

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self.submitted: list[Future] = []
        self.shutdown_calls: list[tuple[bool, bool]] = []
        self.submit_hook = None

    def submit(self, fn: object, *args: object, **kwargs: object) -> Future:
        future: Future = Future()
        self.submitted.append(future)
        if self.submit_hook is not None:
            self.submit_hook(future)
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


def test_materialization_parallel_out_of_order_completion_preserves_manifest_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = _shared_materialization_payloads()
    executor = _DeferredExecutor(max_workers=2)
    all_submitted = threading.Event()

    def on_submit(_future: Future) -> None:
        if len(executor.submitted) == len(payloads):
            all_submitted.set()

    executor.submit_hook = on_submit
    monkeypatch.setattr("mrs3.duckdb_direct.ProcessPoolExecutor", lambda **_kwargs: executor)

    results: list[tuple[DirectPoint, ...]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(
                _materialize_payloads_parallel(
                    payloads,
                    DirectMaterializationSettings(workers=2, worker_chunk_size=1, max_in_flight_chunks=2),
                    "2024-01-01T00:00:00Z",
                    "2024-01-01T02:00:00Z",
                    lambda: False,
                )
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert all_submitted.wait(timeout=5)
    window = ("2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z")
    executor.submitted[1].set_result(_materialize_payload_chunk((payloads[1],), *window))
    executor.submitted[0].set_result(_materialize_payload_chunk((payloads[0],), *window))
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert [point.canonical_point_key for point in results[0]] == [
        "BTCUSDT|LONG|1h|100|3|9",
        "BTCUSDT|LONG|1h|200|3|9",
    ]
    assert executor.shutdown_calls == [(True, False)]


def test_materialization_parallel_cancellation_shuts_down_with_cancel_futures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = _shared_materialization_payloads()
    executor = _DeferredExecutor(max_workers=2)
    all_submitted = threading.Event()
    cancelled = False

    def on_submit(_future: Future) -> None:
        if len(executor.submitted) == len(payloads):
            nonlocal cancelled
            cancelled = True
            all_submitted.set()

    executor.submit_hook = on_submit
    monkeypatch.setattr("mrs3.duckdb_direct.ProcessPoolExecutor", lambda **_kwargs: executor)

    errors: list[BaseException] = []

    def run() -> None:
        try:
            _materialize_payloads_parallel(
                payloads,
                DirectMaterializationSettings(workers=2, worker_chunk_size=1, max_in_flight_chunks=2),
                "2024-01-01T00:00:00Z",
                "2024-01-01T02:00:00Z",
                lambda: cancelled,
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert all_submitted.wait(timeout=5)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], DirectMaterializationError)
    assert "cancelled" in str(errors[0])
    assert executor.shutdown_calls == [(True, True)]
    assert all(future.cancelled() for future in executor.submitted)


def test_materialization_parallel_cancellation_observed_while_futures_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = _shared_materialization_payloads()
    executor = _DeferredExecutor(max_workers=2)
    all_submitted = threading.Event()
    executor.submit_hook = lambda _future: (
        all_submitted.set() if len(executor.submitted) == len(payloads) else None
    )
    monkeypatch.setattr("mrs3.duckdb_direct.ProcessPoolExecutor", lambda **_kwargs: executor)

    cancelled = False
    wait_timeouts: list[float | None] = []

    def timed_wait(
        fs: object,
        timeout: float | None = None,
        return_when: object = duckdb_direct.FIRST_COMPLETED,
    ) -> tuple[set[Future], set[Future]]:
        nonlocal cancelled
        wait_timeouts.append(timeout)
        if timeout is not None:
            cancelled = True
        return set(), set(fs)  # type: ignore[arg-type]

    monkeypatch.setattr("mrs3.duckdb_direct.wait", timed_wait)

    errors: list[BaseException] = []

    def run() -> None:
        try:
            _materialize_payloads_parallel(
                payloads,
                DirectMaterializationSettings(workers=2, worker_chunk_size=1, max_in_flight_chunks=2),
                "2024-01-01T00:00:00Z",
                "2024-01-01T02:00:00Z",
                lambda: cancelled,
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert all_submitted.wait(timeout=5)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert wait_timeouts == [duckdb_direct._PARALLEL_WAIT_TIMEOUT_SECONDS]
    assert len(errors) == 1
    assert isinstance(errors[0], DirectMaterializationError)
    assert "cancelled" in str(errors[0])
    assert executor.shutdown_calls == [(True, True)]
    assert all(future.cancelled() for future in executor.submitted)


def test_materialization_parallel_worker_failure_shuts_down_with_cancel_futures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = _shared_materialization_payloads()
    executor = _DeferredExecutor(max_workers=2)
    all_submitted = threading.Event()
    executor.submit_hook = lambda _future: (
        all_submitted.set() if len(executor.submitted) == len(payloads) else None
    )
    monkeypatch.setattr("mrs3.duckdb_direct.ProcessPoolExecutor", lambda **_kwargs: executor)

    errors: list[BaseException] = []

    def run() -> None:
        try:
            _materialize_payloads_parallel(
                payloads,
                DirectMaterializationSettings(workers=2, worker_chunk_size=1, max_in_flight_chunks=2),
                "2024-01-01T00:00:00Z",
                "2024-01-01T02:00:00Z",
                lambda: False,
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert all_submitted.wait(timeout=5)
    window = ("2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z")
    executor.submitted[1].set_result(_materialize_payload_chunk((payloads[1],), *window))
    executor.submitted[0].set_exception(RuntimeError("worker exploded"))
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "worker exploded" in str(errors[0])
    assert executor.shutdown_calls == [(True, True)]


def test_materialization_parallel_emits_required_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = _shared_materialization_payloads()

    class _ImmediateExecutor:
        def __init__(self, max_workers: int) -> None:
            self.max_workers = max_workers

        def submit(self, fn: object, *args: object, **kwargs: object) -> Future:
            future: Future = Future()
            future.set_result(fn(*args, **kwargs))
            return future

        def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
            pass

    monkeypatch.setattr("mrs3.duckdb_direct.ProcessPoolExecutor", _ImmediateExecutor)

    events: list[dict[str, object]] = []

    def progress(phase: str, **facts: object) -> None:
        events.append({"phase": phase, **facts})

    points = _materialize_payloads_parallel(
        payloads,
        DirectMaterializationSettings(workers=2, worker_chunk_size=1, max_in_flight_chunks=2),
        "2024-01-01T00:00:00Z",
        "2024-01-01T02:00:00Z",
        lambda: False,
        progress_callback=progress,
    )

    assert [point.canonical_point_key for point in points] == [
        "BTCUSDT|LONG|1h|100|3|9",
        "BTCUSDT|LONG|1h|200|3|9",
    ]
    materializing = [event for event in events if event["phase"] == "MATERIALIZING"]
    assert len(materializing) == 2
    required = {
        "materialized_points",
        "total_points",
        "workers",
        "elapsed_seconds",
        "points_per_second",
    }
    for event in materializing:
        assert required <= set(event)
    assert [event["total_points"] for event in materializing] == [2, 2]
    assert [event["materialized_points"] for event in materializing] == [1, 2]
    assert [event["workers"] for event in materializing] == [2, 2]
    assert all(event["elapsed_seconds"] >= 0 for event in materializing)
    assert all(event["points_per_second"] >= 0 for event in materializing)


def test_materialization_multi_batch_emits_global_telemetry(
    connections, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, analysis = connections
    for shift in (100, 200, 300):
        _seed_report(source, shift=shift)

    class _ImmediateExecutor:
        def __init__(self, max_workers: int) -> None:
            self.max_workers = max_workers

        def submit(self, fn: object, *args: object, **kwargs: object) -> Future:
            future: Future = Future()
            future.set_result(fn(*args, **kwargs))
            return future

        def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
            pass

    monkeypatch.setattr("mrs3.duckdb_direct.ProcessPoolExecutor", _ImmediateExecutor)

    events: list[dict[str, object]] = []

    def progress(phase: str, **facts: object) -> None:
        events.append({"phase": phase, **facts})

    surface = materialize_duckdb_direct(
        source,
        analysis,
        _request(required_shifts_bp=(100, 200, 300)),
        lambda: False,
        materialization_settings=DirectMaterializationSettings(
            workers=2, fetch_batch_size=1, worker_chunk_size=1, max_in_flight_chunks=2
        ),
        progress_callback=progress,
    )

    assert [point.canonical_point_key for point in surface.points] == [
        "BTCUSDT|LONG|1h|100|3|9",
        "BTCUSDT|LONG|1h|200|3|9",
        "BTCUSDT|LONG|1h|300|3|9",
    ]
    materializing = [event for event in events if event["phase"] == "MATERIALIZING"]
    assert len(materializing) == 3
    assert [event["materialized_points"] for event in materializing] == [1, 2, 3]
    assert [event["total_points"] for event in materializing] == [3, 3, 3]
    assert [event["workers"] for event in materializing] == [2, 2, 2]
    elapsed = [float(event["elapsed_seconds"]) for event in materializing]
    assert elapsed == sorted(elapsed)
    assert all(float(event["points_per_second"]) >= 0 for event in materializing)


def test_materialization_payload_chunk_does_not_touch_duckdb(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ForbiddenDuckDB:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"worker must not access duckdb.{name}")

    monkeypatch.setattr("mrs3.duckdb_direct.duckdb", _ForbiddenDuckDB())
    points = _materialize_payload_chunk(_shared_materialization_payloads(), "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z")
    assert [point.canonical_point_key for point in points] == ["BTCUSDT|LONG|1h|100|3|9", "BTCUSDT|LONG|1h|200|3|9"]


def test_materialization_payload_chunk_wraps_source_pack_error_as_direct_error() -> None:
    payload = _materialization_payload(
        canonical_point_key="BTCUSDT|LONG|1h|100|3|9",
        report_id="report-a",
        source_hash="a" * 64,
        grid_hash="broken-grid",
        timestamps_blob=b"not-a-zlib-payload",
    )
    with pytest.raises(DirectMaterializationError, match=r"cannot materialize BTCUSDT\|LONG\|1h\|100\|3\|9"):
        _materialize_payload_chunk((payload,), "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z")


class _BulkRowSource:
    """Fake bulk-fetch source returning pre-captured payload rows."""

    def __init__(self, rows: tuple[tuple[object, ...], ...], *, duplicate: bool = False) -> None:
        self._rows = rows
        self._duplicate = duplicate

    def execute(self, statement: str, params: object = None) -> "_BulkRowSource":
        return self

    def fetchall(self) -> tuple[tuple[object, ...], ...]:
        return self._rows + self._rows if self._duplicate else self._rows


def test_bulk_fetch_fails_closed_on_missing_duplicate_or_mismatched_report_evidence(
    connections,
) -> None:
    source, _ = connections
    point = _seed_report(source, source_hash="a" * 64)
    report_id, source_hash = source.execute(
        "select report_id, source_sha256 from active_reports where canonical_point_key=?",
        [point],
    ).fetchone()
    manifest = ((str(report_id), str(source_hash)),)

    payloads = _fetch_materialization_payload_batch(source, manifest)
    assert len(payloads) == 1
    assert payloads[0].canonical_point_key == point
    assert payloads[0].source_hash == str(source_hash)

    with pytest.raises(DirectMaterializationError, match="active source changed"):
        _fetch_materialization_payload_batch(source, (("missing-report", str(source_hash)),))
    with pytest.raises(DirectMaterializationError, match="active source changed"):
        _fetch_materialization_payload_batch(source, ((str(report_id), "b" * 64),))

    row = source.execute(
        duckdb_direct._BULK_MATERIALIZATION_SQL, [[str(report_id)]]
    ).fetchall()[0]
    with pytest.raises(DirectMaterializationError, match="active source changed"):
        _fetch_materialization_payload_batch(_BulkRowSource((row,), duplicate=True), manifest)


def test_materialization_rejects_same_connection_and_cancellation_before_publication(connections) -> None:
    source, analysis = connections
    _seed_report(source)
    with pytest.raises(DirectMaterializationError, match="distinct"):
        materialize_duckdb_direct(source, source, _request(), lambda: False)
    with pytest.raises(DirectMaterializationError, match="cancelled"):
        materialize_duckdb_direct(source, analysis, _request(), lambda: True)
    assert analysis.execute("select count(*) from information_schema.tables").fetchone() == (0,)


def test_publication_identity_excludes_plateau_settings_and_normalizes_utc_period(connections) -> None:
    source, analysis = connections
    _seed_report(source)
    surface = materialize_duckdb_direct(source, analysis, _request(), lambda: False)

    first = publish_surface(analysis, surface)
    equivalent = materialize_duckdb_direct(source, analysis, _request(start_utc="2023-12-31T19:00:00-05:00", end_utc="2023-12-31T21:00:00-05:00"), lambda: False)
    second = publish_surface(analysis, equivalent)

    assert first.surface_id == second.surface_id
    assert analysis.execute("select count(*) from surfaces").fetchone() == (1,)


def test_publication_identity_changes_for_each_surface_contract_input(connections) -> None:
    from mrs3.analysis_storage import _surface_identity

    source, analysis = connections
    _seed_report(source)
    surface = materialize_duckdb_direct(source, analysis, _request(), lambda: False)
    baseline = _surface_identity(surface)[0]
    changed_hash = "b" * 64
    changed_preflight = replace(
        surface.preflight, source_hashes=(changed_hash,), manifest=((surface.points[0].source_report_id, changed_hash),)
    )
    changed_point = replace(surface.points[0], source_hash=changed_hash)
    variants = (
        replace(surface, build_mode="OTHER_DIRECT"),
        replace(surface, event_mode="legacy_trades_proxy"),
        replace(surface, request=replace(surface.request, start_utc="2024-01-01T00:01:00Z")),
        replace(surface, request=replace(surface.request, side="SHORT")),
        replace(surface, request=replace(surface.request, symbols=("ETHUSDT",))),
        replace(surface, request=replace(surface.request, materializer_version="v2")),
        replace(surface, request=replace(surface.request, point_materialization_config_hash="b" * 64)),
        replace(surface, preflight=changed_preflight, points=(changed_point,)),
        replace(surface, preflight=replace(surface.preflight, usable_timeframes={"BTCUSDT": ("4h",)})),
        replace(surface, preflight=replace(surface.preflight, grid_contract={**surface.preflight.grid_contract, "pairs": ("100|4|10",)})),
        replace(surface, preflight=replace(surface.preflight, grid_contract={**surface.preflight.grid_contract, "normalization_contract_version": "v2"})),
    )

    for variant in variants:
        assert _surface_identity(variant)[0] != baseline


def test_source_hash_identity_is_order_independent_and_rejects_duplicates(connections) -> None:
    from mrs3.analysis_storage import _surface_identity

    source, analysis = connections
    _seed_report(source)
    surface = materialize_duckdb_direct(source, analysis, _request(), lambda: False)
    forward = replace(surface, preflight=replace(surface.preflight, source_hashes=("a" * 64, "b" * 64)))
    reversed_hashes = replace(surface, preflight=replace(surface.preflight, source_hashes=("b" * 64, "a" * 64)))
    duplicate = replace(surface, preflight=replace(surface.preflight, source_hashes=("a" * 64, "a" * 64)))

    assert _surface_identity(forward)[0] == _surface_identity(reversed_hashes)[0]
    with pytest.raises(ValueError, match="source hashes"):
        publish_surface(analysis, duplicate)


def test_publication_rolls_back_mid_transaction_and_returns_points_in_key_order(connections) -> None:
    source, analysis = connections
    _seed_report(source, timeframe="4h")
    _seed_report(source, timeframe="1h")
    surface = materialize_duckdb_direct(source, analysis, _request(), lambda: False)
    issue = CoverageIssue("BTCUSDT", "1h", "AUDIT", "duplicate")
    broken = replace(surface, preflight=replace(surface.preflight, coverage_issues=(issue, issue)))

    with pytest.raises(duckdb.ConstraintException):
        publish_surface(analysis, broken)
    assert analysis.execute("select count(*) from surfaces").fetchone() == (0,)

    published = publish_surface(analysis, replace(surface, points=tuple(reversed(surface.points))))
    assert tuple(point.canonical_point_key for point in published.points) == tuple(sorted(surface.preflight.accepted_point_keys))


def test_raw_reproduction_status_is_derived_from_supplied_active_source_hashes(connections) -> None:
    from mrs3.analysis_storage import surface_raw_reproduction_status

    source, analysis = connections
    _seed_report(source)
    a = materialize_duckdb_direct(source, analysis, _request(), lambda: False)
    published_a = publish_surface(analysis, a)
    a_hash = a.points[0].source_hash
    b_hash = "b" * 64
    b = replace(a, preflight=replace(a.preflight, source_hashes=(b_hash,), manifest=((a.points[0].source_report_id, b_hash),)), points=(replace(a.points[0], source_hash=b_hash),))
    published_b = publish_surface(analysis, b)

    assert surface_raw_reproduction_status(analysis, published_a.surface_id, {a_hash}) == "REPRODUCIBLE"
    assert surface_raw_reproduction_status(analysis, published_a.surface_id, {b_hash}) == "RAW_REPLACED"
    assert surface_raw_reproduction_status(analysis, published_b.surface_id, {b_hash}) == "REPRODUCIBLE"
    with pytest.raises(ValueError, match="unknown surface"):
        surface_raw_reproduction_status(analysis, "missing", {b_hash})


def test_publication_rejects_unknown_mode_bad_hash_and_nonfinite_metrics(connections) -> None:
    source, analysis = connections
    _seed_report(source)
    surface = materialize_duckdb_direct(source, analysis, _request(), lambda: False)

    for invalid in (
        replace(surface, build_mode="OTHER_DIRECT"),
        replace(surface, request=replace(surface.request, point_materialization_config_hash="A" * 64)),
        replace(surface, points=(replace(surface.points[0], metrics={"TotalTrades": 0, "bad": float("nan")}),)),
        replace(surface, points=(replace(surface.points[0], metrics={**surface.points[0].metrics, "WinRate": float("inf")}),)),
    ):
        with pytest.raises(ValueError):
            publish_surface(analysis, invalid)
    assert analysis.execute("select count(*) from information_schema.tables").fetchone()[0] == 16
    assert analysis.execute("select count(*) from surfaces").fetchone() == (0,)


def test_explicit_parent_requires_same_period_and_selected_scope(connections) -> None:
    source, analysis = connections
    _seed_report(source)
    surface = materialize_duckdb_direct(source, analysis, _request(), lambda: False)
    parent = publish_surface(analysis, surface)
    eth_key = "ETHUSDT|LONG|1h|100|3|9"
    eth = replace(
        surface,
        request=replace(surface.request, symbols=("ETHUSDT",)),
        preflight=replace(surface.preflight, usable_timeframes={"ETHUSDT": ("1h",)}, accepted_point_keys=(eth_key,)),
        points=(replace(surface.points[0], canonical_point_key=eth_key),),
        parent_surface_id=parent.surface_id,
    )
    other_period = replace(
        surface, request=replace(surface.request, start_utc="2024-01-01T00:01:00Z"), parent_surface_id=parent.surface_id
    )

    for child in (eth, other_period):
        with pytest.raises(ValueError, match="explicit parent"):
            publish_surface(analysis, child)
    assert analysis.execute("select count(*) from surfaces").fetchone() == (1,)


def test_panel_build_orders_preflight_materialization_revalidation_and_publication(
    connections, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, analysis = connections
    _seed_report(source)
    calls: list[str] = []
    original_preflight = preflight_duckdb_direct

    def preflight(connection: duckdb.DuckDBPyConnection, request: DirectBuildRequest):
        calls.append("preflight")
        return original_preflight(connection, request)

    def materialize(*args: object, **kwargs: object):
        calls.append("materialize")
        return materialize_duckdb_direct(*args, **kwargs)  # type: ignore[arg-type]

    def publish(connection: duckdb.DuckDBPyConnection, surface: object):
        calls.append("publish")
        return publish_surface(connection, surface)  # type: ignore[arg-type]

    monkeypatch.setattr("mrs3.duckdb_direct.preflight_duckdb_direct", preflight)
    monkeypatch.setattr("mrs3.duckdb_direct.materialize_duckdb_direct", materialize)
    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", publish)

    phases: list[str] = []
    published = run_panel_direct_build(source, analysis, _request(), lambda: False, lambda phase, **_: phases.append(phase))

    assert calls == ["preflight", "materialize", "preflight", "publish"]
    assert phases[0] == "PREFLIGHT"
    assert phases[-2:] == ["REVALIDATING", "PUBLISHED"]
    assert "MATERIALIZING" in phases[1:-2]
    assert published.created is True


def test_replay_validates_all_sides_before_materializing_any_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _StatementSource()
    audit = b"pair,side\nBTC,LONG\n"

    def request(side: str) -> DirectBuildRequest:
        return _request(
            side=side,
            grid_contract_kind=V2_GRID_CONTRACT_KIND,
            selected_scopes=("BTCUSDT|1h",),
            required_shifts_bp=READY_SHIFTS,
            materializer_version=CANONICAL_MATERIALIZER_VERSION,
            point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
            readiness_contract_version=READINESS_CONTRACT_VERSION,
            readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
            audit_artifact_name=f"surface_coverage_audit_{side}.csv",
            audit_schema_version=1,
            audit_size_bytes=len(audit),
            audit_row_count=1,
            audit_sha256=sha256(audit).hexdigest(),
            audit_bytes=audit,
        )

    long_request, short_request = request("LONG"), request("SHORT")
    scan = type("Scan", (), {"token": "scan", "symbols": ()})()
    expected = {
        "LONG": DirectPreflight({}, {}, (), MappingProxyType({"kind": V2_GRID_CONTRACT_KIND}), (), (), ()),
        "SHORT": DirectPreflight({}, {}, (), MappingProxyType({"kind": V2_GRID_CONTRACT_KIND}), (), (), ()),
    }
    preflight_calls: list[str] = []
    materialize_calls: list[str] = []

    monkeypatch.setattr("mrs3.duckdb_direct._verify_scan_inventory", lambda _scan: None)
    monkeypatch.setattr("mrs3.duckdb_direct.coverage_scan_direct", lambda *_args, **_kwargs: scan)
    monkeypatch.setattr("mrs3.duckdb_direct.coverage_audit_csv_bytes", lambda *_args, **_kwargs: audit)
    monkeypatch.setattr("mrs3.duckdb_direct.verify_persisted_surface_audit", lambda *_args, **_kwargs: audit)

    def preflight(_source: object, current: DirectBuildRequest) -> DirectPreflight:
        preflight_calls.append(current.side)
        return expected[current.side]

    def materialize(_source: object, _analysis: object, current: DirectBuildRequest, *_args: object, **_kwargs: object) -> DirectSurface:
        materialize_calls.append(current.side)
        return DirectSurface(current, expected[current.side], "real_independent_events", ())

    monkeypatch.setattr("mrs3.duckdb_direct.preflight_duckdb_direct", preflight)
    monkeypatch.setattr("mrs3.duckdb_direct.materialize_duckdb_direct", materialize)

    with pytest.raises(DirectMaterializationError, match="STALE_PREFLIGHT"):
        replay_direct_preflights(
            source,
            (long_request, short_request),
            (expected["LONG"], replace(expected["SHORT"], source_hashes=("changed",))),
            audit_root=tmp_path,
            coverage_scan=scan,  # type: ignore[arg-type]
        )

    assert preflight_calls == ["LONG", "SHORT"]
    assert materialize_calls == []


def test_panel_refine_passes_the_explicit_chosen_parent_to_publication(
    connections, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, analysis = connections
    _seed_report(source)
    captured: list[str | None] = []

    def publish(_connection: duckdb.DuckDBPyConnection, surface: object):
        captured.append(surface.parent_surface_id)  # type: ignore[attr-defined]
        return type("Published", (), {"surface_id": "child", "points": surface.points})()

    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", publish)

    run_panel_direct_build(
        source,
        analysis,
        _request(),
        lambda: False,
        lambda *_args, **_kwargs: None,
        parent_surface_id="chosen-parent",
    )

    assert captured == ["chosen-parent"]


def test_panel_build_rejects_source_hash_change_immediately_before_publish(
    connections, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, analysis = connections
    point = _seed_report(source)
    original_preflight = preflight_duckdb_direct
    calls = 0

    def changed_after_materialization(connection: duckdb.DuckDBPyConnection, request: DirectBuildRequest):
        nonlocal calls
        calls += 1
        preflight = original_preflight(connection, request)
        if calls == 2:
            return replace(preflight, source_hashes=("b" * 64,), manifest=((point, "b" * 64),))
        return preflight

    monkeypatch.setattr("mrs3.duckdb_direct.preflight_duckdb_direct", changed_after_materialization)

    with pytest.raises(DirectMaterializationError, match="active source changed"):
        run_panel_direct_build(source, analysis, _request(), lambda: False, lambda *_args, **_kwargs: None)
    assert analysis.execute("select count(*) from information_schema.tables where table_name='surfaces'").fetchone() == (0,)


def test_panel_build_rejects_changed_preflight_contract_before_publish(
    connections, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, analysis = connections
    _seed_report(source)
    original_preflight = preflight_duckdb_direct
    calls = 0

    def changed_after_materialization(connection: duckdb.DuckDBPyConnection, request: DirectBuildRequest):
        nonlocal calls
        calls += 1
        preflight = original_preflight(connection, request)
        return replace(preflight, accepted_point_keys=("changed",)) if calls == 2 else preflight

    monkeypatch.setattr("mrs3.duckdb_direct.preflight_duckdb_direct", changed_after_materialization)

    with pytest.raises(DirectMaterializationError, match="active source changed"):
        run_panel_direct_build(source, analysis, _request(), lambda: False, lambda *_args, **_kwargs: None)
    assert analysis.execute("select count(*) from information_schema.tables where table_name='surfaces'").fetchone() == (0,)


def test_panel_build_cancellation_after_materialization_blocks_publication(
    connections, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, analysis = connections
    _seed_report(source)
    cancelled = False
    original_materialize = materialize_duckdb_direct
    published = False

    def materialize(*args: object, **kwargs: object):
        nonlocal cancelled
        surface = original_materialize(*args, **kwargs)  # type: ignore[arg-type]
        cancelled = True
        return surface

    def publish(*_: object) -> object:
        nonlocal published
        published = True
        raise AssertionError("cancelled build must not publish")

    monkeypatch.setattr("mrs3.duckdb_direct.materialize_duckdb_direct", materialize)
    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", publish)
    with pytest.raises(DirectMaterializationError, match="cancelled"):
        run_panel_direct_build(source, analysis, _request(), lambda: cancelled, lambda *_args, **_kwargs: None)
    assert published is False
    assert analysis.execute("select count(*) from information_schema.tables where table_name='surfaces'").fetchone() == (0,)


def test_direct_job_prepares_long_and_short_in_one_source_transaction_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _StatementSource()
    long_request = replace(
        _request(
            side="LONG",
            grid_contract_kind=V2_GRID_CONTRACT_KIND,
            selected_scopes=("BTCUSDT|1h",),
            required_shifts_bp=READY_SHIFTS,
            materializer_version=CANONICAL_MATERIALIZER_VERSION,
            point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
        ),
        grid_contract_kind="",
        materializer_version="",
    )
    short_request = replace(long_request, side="SHORT")
    preflights = {"LONG": _v2_preflight("LONG"), "SHORT": _v2_preflight("SHORT")}

    def fake_preflight(connection: object, request: DirectBuildRequest) -> DirectPreflight:
        assert connection is source
        return preflights[request.side]

    def fake_materialize(
        connection: object,
        analysis_connection: object,
        request: DirectBuildRequest,
        cancellation: object,
        *,
        preflight: DirectPreflight,
    ) -> DirectSurface:
        return DirectSurface(request, preflight, "real_independent_events", ())

    audit_scopes: list[tuple[str, ...]] = []

    def fake_audit(*args: object, **kwargs: object) -> bytes:
        audit_scopes.append(tuple(kwargs["selected_scopes"]))  # type: ignore[arg-type]
        return b"pair,side\na,LONG\n"

    monkeypatch.setattr("mrs3.duckdb_direct.preflight_duckdb_direct", fake_preflight)
    monkeypatch.setattr("mrs3.duckdb_direct.materialize_duckdb_direct", fake_materialize)
    monkeypatch.setattr("mrs3.duckdb_direct.coverage_audit_csv_bytes", fake_audit)
    phases: list[str] = []

    surfaces = prepare_direct_surfaces(
        source,
        (short_request, long_request),
        audit_root=tmp_path,
        progress_callback=lambda phase, **facts: phases.append(str(phase)),
    )

    assert [surface.request.side for surface in surfaces] == ["LONG", "SHORT"]
    assert audit_scopes == [("BTCUSDT|1h",), ("BTCUSDT|1h",)]
    assert phases == [
        "PREPARING_LONG",
        "PREPARED_LONG",
        "PREPARING_SHORT",
        "PREPARED_SHORT",
    ]
    assert source.statements.count("begin transaction") == 1
    assert source.statements[-1] == "commit"


def test_either_side_preparation_failure_rolls_back_and_publishes_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _StatementSource()
    long_request = replace(
        _request(
            side="LONG",
            grid_contract_kind=V2_GRID_CONTRACT_KIND,
            selected_scopes=("BTCUSDT|1h",),
            required_shifts_bp=READY_SHIFTS,
            materializer_version=CANONICAL_MATERIALIZER_VERSION,
            point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
        ),
        grid_contract_kind="",
        materializer_version="",
    )

    def fake_preflight(connection: object, request: DirectBuildRequest) -> DirectPreflight:
        if request.side == "SHORT":
            raise DirectMaterializationError("short preparation failed")
        return _v2_preflight(request.side)

    monkeypatch.setattr("mrs3.duckdb_direct.preflight_duckdb_direct", fake_preflight)
    monkeypatch.setattr(
        "mrs3.duckdb_direct.coverage_audit_csv_bytes",
        lambda *_args, **_kwargs: b"pair,side\na,LONG\n",
    )

    with pytest.raises(DirectMaterializationError, match="short preparation failed"):
        prepare_direct_surfaces(
            source,
            (long_request, replace(long_request, side="SHORT")),
            audit_root=tmp_path,
        )

    assert source.statements.count("begin transaction") == 1
    assert "rollback" in source.statements
    assert "commit" not in source.statements


def test_direct_job_publishes_long_before_short(monkeypatch: pytest.MonkeyPatch) -> None:
    sides: list[str] = []
    phases: list[str] = []

    def fake_publish(connection: object, surface: DirectSurface) -> PublishedSurface:
        sides.append(surface.request.side)
        return PublishedSurface(f"surface-{surface.request.side}", None, True, ())

    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", fake_publish)
    long_surface = DirectSurface(
        _request(side="LONG"), _v2_preflight("LONG"), "real_independent_events", ()
    )
    short_surface = DirectSurface(
        _request(side="SHORT"), _v2_preflight("SHORT"), "real_independent_events", ()
    )

    result = publish_direct_surfaces(
        object(),
        (short_surface, long_surface),
        progress_callback=lambda phase, **facts: phases.append(str(phase)),
    )

    assert result.publication_state == "PUBLISHED"
    assert sides == ["LONG", "SHORT"]
    assert phases == ["PUBLISHING_LONG", "PUBLISHING_SHORT"]


def _canonical_admission_surface(*, grid_contract: dict[str, object]) -> DirectSurface:
    audit = b"pair,side\nBTC,LONG\n"
    request = _request(
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        required_shifts_bp=READY_SHIFTS,
        materializer_version=CANONICAL_MATERIALIZER_VERSION,
        point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
        selected_scopes=("BTCUSDT|1h",),
        readiness_contract_version=READINESS_CONTRACT_VERSION,
        readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
        audit_artifact_name="surface_coverage_audit_LONG.csv",
        audit_schema_version=1,
        audit_size_bytes=len(audit),
        audit_row_count=1,
        audit_sha256=sha256(audit).hexdigest(),
        audit_bytes=audit,
    )
    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {}, (), MappingProxyType(grid_contract),
        ("a" * 64,), (("report", "a" * 64),), (),
        witnesses=grid_contract.get("witnesses", ()),
        audit_artifact_name=request.audit_artifact_name,
        audit_schema_version=1,
        audit_size_bytes=len(audit),
        audit_row_count=1,
        audit_sha256=request.audit_sha256,
        audit_bytes=audit,
    )
    return DirectSurface(request, preflight, "real_independent_events", ())


def test_v2_publish_requires_audit_root_even_when_canonical_grid_metadata_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", lambda *args: calls.append(args))
    surface = _canonical_admission_surface(grid_contract={"kind": V2_GRID_CONTRACT_KIND})

    result = publish_direct_surfaces(object(), (surface,))

    assert result.publication_state == "FAILED"
    assert calls == []
    assert "audit root" in (result.error or "")


def test_v2_request_with_legacy_preflight_cannot_bypass_audit_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", lambda *args: calls.append(args))
    surface = _canonical_admission_surface(grid_contract={"kind": "OBSERVED_GRID_CONTRACT"})

    result = publish_direct_surfaces(object(), (surface,), audit_root=tmp_path)

    assert result.publication_state == "FAILED"
    assert calls == []
    assert "V2 preflight contract" in (result.error or "")


def test_v2_publish_rejects_missing_grid_or_singular_witness_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = b"pair,side\nBTC,LONG\n"
    singular = {
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "open_ma": 3,
        "close_ma": 2, "shifts_bp": list(READY_SHIFTS),
        "contract_version": READINESS_CONTRACT_VERSION, "max_shift_bp": READINESS_MAX_SHIFT_BP,
    }
    contract = {
        "kind": V2_GRID_CONTRACT_KIND,
        "canonical_grid_version": CANONICAL_GRID_VERSION,
        "canonical_shifts_bp": list(READY_SHIFTS),
        "point_materialization_semantics_version": POINT_MATERIALIZATION_SEMANTICS_VERSION,
        "point_materialization_config_hash": canonical_point_materialization_config_hash(READY_SHIFTS),
        "selected_scopes": ["BTCUSDT|1h"],
        "witnesses": {"BTCUSDT|1h": singular},
        "audit_artifact_name": "surface_coverage_audit_LONG.csv",
        "audit_schema_version": 1,
        "audit_size_bytes": len(audit),
        "audit_row_count": 1,
        "audit_sha256": sha256(audit).hexdigest(),
    }
    surface = _canonical_admission_surface(grid_contract=contract)
    calls: list[object] = []
    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", lambda *args: calls.append(args))

    result = publish_direct_surfaces(object(), (surface,), audit_root=tmp_path)

    assert result.publication_state == "FAILED"
    assert calls == []
    assert "six entries" in (result.error or "")


def test_persisted_v2_audit_rejects_zero_request_size_without_fallback(tmp_path: Path) -> None:
    audit = b"pair,side\nBTC,LONG\n"
    witness = {
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "open_ma": 3,
        "shifts_bp": list(READY_SHIFTS),
        "contract_version": READINESS_CONTRACT_VERSION, "max_shift_bp": READINESS_MAX_SHIFT_BP,
    }
    contract = {
        "kind": V2_GRID_CONTRACT_KIND,
        "canonical_grid_version": CANONICAL_GRID_VERSION,
        "canonical_shifts_bp": list(READY_SHIFTS),
        "point_materialization_semantics_version": POINT_MATERIALIZATION_SEMANTICS_VERSION,
        "point_materialization_config_hash": canonical_point_materialization_config_hash(READY_SHIFTS),
        "selected_scopes": ["BTCUSDT|1h"],
        "witnesses": {"BTCUSDT|1h": [dict(witness, close_ma=close_ma) for close_ma in REQUIRED_CLOSE_MAS]},
        "audit_artifact_name": "surface_coverage_audit_LONG.csv",
        "audit_schema_version": 1,
        "audit_size_bytes": len(audit),
        "audit_row_count": 1,
        "audit_sha256": sha256(audit).hexdigest(),
    }
    surface = _canonical_admission_surface(grid_contract=contract)
    malformed = replace(surface, request=replace(surface.request, audit_size_bytes=0))

    with pytest.raises(DirectMaterializationError, match="size metadata is missing"):
        verify_persisted_surface_audit(tmp_path, malformed)


def test_v2_publish_verifies_saved_audit_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit = b"pair,side\nBTC,LONG\n"
    witness = {
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "open_ma": 3,
        "shifts_bp": list(READY_SHIFTS),
        "contract_version": READINESS_CONTRACT_VERSION, "max_shift_bp": READINESS_MAX_SHIFT_BP,
    }
    contract = {
        "kind": V2_GRID_CONTRACT_KIND,
        "canonical_grid_version": CANONICAL_GRID_VERSION,
        "canonical_shifts_bp": list(READY_SHIFTS),
        "point_materialization_semantics_version": POINT_MATERIALIZATION_SEMANTICS_VERSION,
        "point_materialization_config_hash": canonical_point_materialization_config_hash(READY_SHIFTS),
        "selected_scopes": ["BTCUSDT|1h"],
        "witnesses": {"BTCUSDT|1h": [dict(witness, close_ma=close_ma) for close_ma in REQUIRED_CLOSE_MAS]},
        "audit_artifact_name": "surface_coverage_audit_LONG.csv",
        "audit_schema_version": 1,
        "audit_size_bytes": len(audit),
        "audit_row_count": 1,
        "audit_sha256": sha256(audit).hexdigest(),
    }
    surface = _canonical_admission_surface(grid_contract=contract)
    path = tmp_path / "surface_coverage" / sha256(audit).hexdigest() / "surface_coverage_audit_LONG.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(audit)
    publish_calls: list[str] = []

    def fake_publish(connection: object, surface: DirectSurface) -> PublishedSurface:
        publish_calls.append(surface.request.side)
        return PublishedSurface("surface-LONG", None, True, ())

    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", fake_publish)

    result = publish_direct_surfaces(object(), (surface,), audit_root=tmp_path)
    assert result.publication_state == "PUBLISHED"
    assert publish_calls == ["LONG"]

    publish_calls.clear()
    path.write_bytes(b"tampered audit bytes")
    result = publish_direct_surfaces(object(), (surface,), audit_root=tmp_path)
    assert result.publication_state == "FAILED"
    assert publish_calls == []
    assert "audit" in (result.error or "")


def test_long_publication_failure_reports_failed_with_zero_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_publish(connection: object, surface: DirectSurface) -> PublishedSurface:
        raise RuntimeError("long publish failed")

    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", fake_publish)
    surfaces = (
        DirectSurface(
            _request(side="LONG"), _v2_preflight("LONG"), "real_independent_events", ()
        ),
        DirectSurface(
            _request(side="SHORT"), _v2_preflight("SHORT"), "real_independent_events", ()
        ),
    )

    result = publish_direct_surfaces(object(), surfaces)

    assert result.publication_state == 'FAILED'
    assert result.surfaces == ()


def test_cancellation_before_long_commit_prevents_any_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sides: list[str] = []

    def fake_publish(connection: object, surface: DirectSurface) -> PublishedSurface:
        sides.append(surface.request.side)
        return PublishedSurface('surface-LONG', None, True, ())

    monkeypatch.setattr('mrs3.duckdb_direct.publish_surface', fake_publish)
    surfaces = (
        DirectSurface(
            _request(side='LONG'), _v2_preflight('LONG'), 'real_independent_events', ()
        ),
        DirectSurface(
            _request(side='SHORT'), _v2_preflight('SHORT'), 'real_independent_events', ()
        ),
    )

    result = publish_direct_surfaces(object(), surfaces, cancellation=lambda: True)

    assert result.publication_state == 'CANCELLED'
    assert result.surfaces == ()
    assert sides == []


def test_direct_job_reports_partial_when_short_publication_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sides: list[str] = []

    def fake_publish(connection: object, surface: DirectSurface) -> PublishedSurface:
        sides.append(surface.request.side)
        if surface.request.side == "SHORT":
            raise RuntimeError("short publish failed")
        return PublishedSurface("surface-LONG", None, True, ())

    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", fake_publish)
    surfaces = (
        DirectSurface(
            _request(side="LONG"), _v2_preflight("LONG"), "real_independent_events", ()
        ),
        DirectSurface(
            _request(side="SHORT"), _v2_preflight("SHORT"), "real_independent_events", ()
        ),
    )

    result = publish_direct_surfaces(object(), surfaces)

    assert result.publication_state == "PARTIAL"
    assert sides == ["LONG", "SHORT"]
    assert tuple(surface.surface_id for surface in result.surfaces) == ("surface-LONG",)


def test_cancellation_after_long_commit_prevents_short_and_reports_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sides: list[str] = []
    checks = 0

    def cancellation() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    def fake_publish(connection: object, surface: DirectSurface) -> PublishedSurface:
        sides.append(surface.request.side)
        return PublishedSurface("surface-LONG", None, True, ())

    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", fake_publish)
    surfaces = (
        DirectSurface(
            _request(side="LONG"), _v2_preflight("LONG"), "real_independent_events", ()
        ),
        DirectSurface(
            _request(side="SHORT"), _v2_preflight("SHORT"), "real_independent_events", ()
        ),
    )

    result = publish_direct_surfaces(object(), surfaces, cancellation=cancellation)

    assert result.publication_state == "PARTIAL"
    assert sides == ["LONG"]
    assert tuple(surface.surface_id for surface in result.surfaces) == ("surface-LONG",)


def test_audit_write_failure_blocks_that_side_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _StatementSource()
    request = replace(
        _request(
            side="LONG",
            grid_contract_kind=V2_GRID_CONTRACT_KIND,
            selected_scopes=("BTCUSDT|1h",),
            required_shifts_bp=READY_SHIFTS,
            materializer_version=CANONICAL_MATERIALIZER_VERSION,
            point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
        ),
        grid_contract_kind="",
        materializer_version="",
    )
    original_write = write_coverage_artifact

    def failing_write(
        audit_root: object, relative_name: str, data: bytes
    ) -> Path:
        if "surface_coverage_audit_SHORT" in str(relative_name):
            raise OSError("short audit write failed")
        return original_write(audit_root, relative_name, data)  # type: ignore[arg-type]

    monkeypatch.setattr("mrs3.duckdb_direct.preflight_duckdb_direct", lambda *_: _v2_preflight("LONG"))
    monkeypatch.setattr(
        "mrs3.duckdb_direct.coverage_audit_csv_bytes",
        lambda *_args, **_kwargs: b"pair,side\na,LONG\n",
    )
    monkeypatch.setattr("mrs3.duckdb_direct.write_coverage_artifact", failing_write)

    with pytest.raises(OSError, match="short audit write failed"):
        prepare_direct_surfaces(
            source,
            (request, replace(request, side="SHORT")),
            audit_root=tmp_path,
        )

    assert source.statements.count("begin transaction") == 1
    assert "rollback" in source.statements
    assert "commit" not in source.statements


def test_materialization_rejects_mixed_surface_side(connections) -> None:
    source, _ = connections
    short_point = _seed_report(source, side="SHORT", source_hash="s" * 64)
    report_id, source_hash = source.execute(
        "select report_id, source_sha256 from active_reports where canonical_point_key=?",
        [short_point],
    ).fetchone()
    preflight = DirectPreflight(
        {}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}),
        (str(source_hash),), ((str(report_id), str(source_hash)),), (short_point,),
    )

    with pytest.raises(DirectMaterializationError, match="side"):
        materialize_duckdb_direct(
            source,
            None,
            _request(),
            lambda: False,
            preflight=preflight,
        )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda points: points[:-1],
        lambda points: points + (
            DirectPoint("BTCUSDT|LONG|1h|400|3|9", "extra-report", "e" * 64, 1, {"TotalTrades": 1}),
        ),
        lambda points: (points[0], replace(points[1], canonical_point_key="BTCUSDT|LONG|1h|300|3|9")),
    ),
    ids=("incomplete", "extra", "different-key-set"),
)
def test_materialization_rejects_worker_key_set_mismatch(
    connections, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    source, analysis = connections
    _seed_report(source, shift=100)
    _seed_report(source, shift=200)
    request = _request(required_shifts_bp=(100, 200))
    preflight = preflight_duckdb_direct(source, request)

    def points_from_payloads(payloads: object) -> tuple[DirectPoint, ...]:
        return tuple(
            DirectPoint(
                canonical_point_key=payload.canonical_point_key,
                source_report_id=payload.report_id,
                source_hash=payload.source_hash,
                point_event_count=1,
                metrics={"TotalTrades": 1},
            )
            for payload in payloads  # type: ignore[union-attr]
        )

    monkeypatch.setattr(
        "mrs3.duckdb_direct._materialize_payloads_parallel",
        lambda payloads, *args, **kwargs: mutate(points_from_payloads(payloads)),
    )

    with pytest.raises(DirectMaterializationError, match="manifest"):
        materialize_duckdb_direct(source, analysis, request, lambda: False, preflight=preflight)


def test_materialization_progress_carries_side(connections, monkeypatch: pytest.MonkeyPatch) -> None:
    source, analysis = connections
    _seed_report(source, shift=100)
    _seed_report(source, shift=200)

    class _ImmediateExecutor:
        def __init__(self, max_workers: int) -> None:
            self.max_workers = max_workers

        def submit(self, fn: object, *args: object, **kwargs: object) -> Future:
            future: Future = Future()
            future.set_result(fn(*args, **kwargs))
            return future

        def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
            pass

    monkeypatch.setattr("mrs3.duckdb_direct.ProcessPoolExecutor", _ImmediateExecutor)

    events: list[dict[str, object]] = []

    def progress(phase: str, **facts: object) -> None:
        events.append({"phase": phase, **facts})

    surface = materialize_duckdb_direct(
        source,
        analysis,
        _request(required_shifts_bp=(100, 200)),
        lambda: False,
        materialization_settings=DirectMaterializationSettings(
            workers=2, fetch_batch_size=1, worker_chunk_size=1, max_in_flight_chunks=2
        ),
        progress_callback=progress,
        progress_side="LONG",
    )

    materializing = [event for event in events if event["phase"] == "MATERIALIZING"]
    assert len(materializing) == 2
    assert all(event["side"] == "LONG" for event in materializing)
    assert len(surface.points) == 2


def test_prepare_direct_surfaces_forwards_side_to_materializer(
    connections, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = connections
    _seed_ready_scope(source, side="LONG")
    scan = coverage_scan_direct(source, tmp_path, symbols=())
    request = replace(
        _coverage_build_request(scan),
        grid_contract_kind="",
        materializer_version="",
    )
    captured: list[str] = []

    def fake_materialize(*args: object, **kwargs: object) -> DirectSurface:
        captured.append(str(kwargs["progress_side"]))
        return DirectSurface(args[2], kwargs["preflight"], "real_independent_events", ())

    monkeypatch.setattr("mrs3.duckdb_direct.materialize_duckdb_direct", fake_materialize)

    prepare_direct_surfaces(source, (request,), audit_root=tmp_path, coverage_scan=scan)

    assert captured == ["LONG"]


def test_replay_direct_preflights_forwards_side_to_materializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _StatementSource()
    audit = b"pair,side\nBTC,LONG\n"

    def request(side: str) -> DirectBuildRequest:
        return _request(
            side=side,
            grid_contract_kind=V2_GRID_CONTRACT_KIND,
            selected_scopes=("BTCUSDT|1h",),
            required_shifts_bp=READY_SHIFTS,
            materializer_version=CANONICAL_MATERIALIZER_VERSION,
            point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
            readiness_contract_version=READINESS_CONTRACT_VERSION,
            readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
            audit_artifact_name=f"surface_coverage_audit_{side}.csv",
            audit_schema_version=1,
            audit_size_bytes=len(audit),
            audit_row_count=1,
            audit_sha256=sha256(audit).hexdigest(),
            audit_bytes=audit,
        )

    long_request, short_request = request("LONG"), request("SHORT")
    scan = type("Scan", (), {"token": "scan", "symbols": ()})()
    expected = {
        "LONG": DirectPreflight({}, {}, (), MappingProxyType({"kind": V2_GRID_CONTRACT_KIND}), (), (), ()),
        "SHORT": DirectPreflight({}, {}, (), MappingProxyType({"kind": V2_GRID_CONTRACT_KIND}), (), (), ()),
    }
    captured: list[str] = []

    monkeypatch.setattr("mrs3.duckdb_direct._verify_scan_inventory", lambda _scan: None)
    monkeypatch.setattr("mrs3.duckdb_direct.coverage_scan_direct", lambda *_args, **_kwargs: scan)
    monkeypatch.setattr("mrs3.duckdb_direct.coverage_audit_csv_bytes", lambda *_args, **_kwargs: audit)
    monkeypatch.setattr("mrs3.duckdb_direct.verify_persisted_surface_audit", lambda *_args, **_kwargs: audit)
    monkeypatch.setattr(
        "mrs3.duckdb_direct.preflight_duckdb_direct",
        lambda _source, current: expected[current.side],
    )

    def fake_materialize(
        _source: object, _analysis: object, current: DirectBuildRequest, *_args: object, **kwargs: object
    ) -> DirectSurface:
        captured.append(str(kwargs["progress_side"]))
        return DirectSurface(current, expected[current.side], "real_independent_events", ())

    monkeypatch.setattr("mrs3.duckdb_direct.materialize_duckdb_direct", fake_materialize)

    surfaces = replay_direct_preflights(
        source,
        (long_request, short_request),
        (expected["LONG"], expected["SHORT"]),
        audit_root=tmp_path,
        coverage_scan=scan,  # type: ignore[arg-type]
    )

    assert [surface.request.side for surface in surfaces] == ["LONG", "SHORT"]
    assert captured == ["LONG", "SHORT"]


def test_v2_preflight_requires_audit_evidence(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(source, symbol="BTCUSDT", shifts=(30, 150, 430))
    request = _request(
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        selected_scopes=("BTCUSDT|1h",),
        readiness_contract_version=READINESS_CONTRACT_VERSION,
        readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
        required_shifts_bp=READY_SHIFTS,
        materializer_version=CANONICAL_MATERIALIZER_VERSION,
        point_materialization_config_hash=canonical_point_materialization_config_hash(READY_SHIFTS),
        audit_artifact_name="",
        audit_schema_version=0,
        audit_row_count=0,
        audit_sha256="",
        audit_bytes=None,
    )

    with pytest.raises(DirectMaterializationError, match="audit"):
        preflight_duckdb_direct(source, request)


def test_v2_preflight_audit_row_count_is_data_rows_excluding_header(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(source, symbol="BTCUSDT", shifts=(30, 150, 430))
    audit = b"pair,side\nrow1,LONG\nrow2,SHORT\n"
    base = {
        "grid_contract_kind": V2_GRID_CONTRACT_KIND,
        "selected_scopes": ("BTCUSDT|1h",),
        "readiness_contract_version": READINESS_CONTRACT_VERSION,
        "readiness_max_shift_bp": READINESS_MAX_SHIFT_BP,
        "required_shifts_bp": READY_SHIFTS,
        "materializer_version": CANONICAL_MATERIALIZER_VERSION,
        "point_materialization_config_hash": canonical_point_materialization_config_hash(READY_SHIFTS),
        "audit_artifact_name": "surface_coverage_audit_LONG.csv",
        "audit_schema_version": 1,
        "audit_sha256": sha256(audit).hexdigest(),
        "audit_size_bytes": len(audit),
        "audit_bytes": audit,
    }

    with pytest.raises(DirectMaterializationError, match="row count"):
        preflight_duckdb_direct(source, _request(audit_row_count=3, **base))

    preflight = preflight_duckdb_direct(source, _request(audit_row_count=2, **base))
    assert preflight.audit_row_count == 2
