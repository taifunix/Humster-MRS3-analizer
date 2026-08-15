from __future__ import annotations

import json
from types import MappingProxyType
import csv
import hashlib
import io
from hashlib import sha256
from datetime import datetime
from dataclasses import replace
from pathlib import Path
import os
from types import MappingProxyType

import duckdb
import pytest

from mrs3 import duckdb_source_schema
from mrs3.duckdb_direct import (
    READINESS_CONTRACT_VERSION,
    READINESS_MAX_SHIFT_BP,
    V2_GRID_CONTRACT_KIND,
    CoverageIssue,
    DirectBuildRequest,
    DirectMaterializationError,
    COVERAGE_CSV_COLUMNS,
    DirectPoint,
    DirectPreflight,
    DirectQueueResult,
    DirectSurface,
    _canonical_json_bytes,
    coverage_audit_csv_bytes,
    coverage_inventory_csv_bytes,
    list_duckdb_direct_coverage,
    materialize_duckdb_direct,
    point_evidence_jsonl_bytes,
    preflight_duckdb_direct,
    prepare_direct_surfaces,
    publish_direct_surfaces,
    run_panel_direct_build,
    write_coverage_artifact,
)
from mrs3.analysis_storage import PublishedSurface, publish_surface
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
        {}, {}, (), MappingProxyType({"kind": V2_GRID_CONTRACT_KIND}),
        (), (), (),
    )


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


def test_readiness_uses_30_150_430_boundaries_and_gap_limits(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=tuple(range(30, 151, 10)) + tuple(range(190, 431, 40)),
    )
    _seed_readiness_scope(
        source,
        symbol="ETHUSDT",
        shifts=(30, 150, 430),
        open_ma=4,
        close_ma=10,
    )

    coverage = list_duckdb_direct_coverage(source, symbols=())
    btc = next(row for row in coverage.rows if row.symbol == "BTCUSDT")
    eth = next(row for row in coverage.rows if row.symbol == "ETHUSDT")
    witness = next(item for item in coverage.intervals if item.scope.symbol == "BTCUSDT").witness

    assert btc.selectable is True
    assert btc.interval_start_utc == "2024-01-01T00:00:00.000+00:00"
    assert btc.interval_end_utc == "2024-01-01T02:00:00.000+00:00"
    assert witness is not None
    assert witness.open_ma == 3
    assert witness.close_ma == 9
    assert witness.shifts_bp == tuple(range(30, 151, 10)) + tuple(range(190, 431, 40))
    assert eth.selectable is False


def test_readiness_accepts_denser_shifts_and_requires_common_ma_pair(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(source, symbol="BTCUSDT", shifts=tuple(range(30, 431, 5)))
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=(30, 150, 430),
        open_ma=4,
        close_ma=10,
    )
    _seed_readiness_scope(
        source,
        symbol="ETHUSDT",
        shifts=(30, 430),
        open_ma=3,
        close_ma=9,
    )
    _seed_readiness_scope(
        source,
        symbol="ETHUSDT",
        shifts=(30, 150),
        open_ma=4,
        close_ma=10,
    )

    coverage = list_duckdb_direct_coverage(source, symbols=())
    btc = next(row for row in coverage.rows if row.symbol == "BTCUSDT")
    eth = next(row for row in coverage.rows if row.symbol == "ETHUSDT")
    witness = next(item for item in coverage.intervals if item.scope.symbol == "BTCUSDT").witness

    assert btc.selectable is True
    assert witness is not None
    assert witness.open_ma == 3
    assert witness.shifts_bp == tuple(range(30, 151, 10)) + tuple(range(190, 431, 40))
    assert eth.selectable is False


def test_readiness_retains_exact_150_when_optional_shift_is_immediately_above(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=(30, 35, 45, 55, 65, 75, 85, 95, 105, 115, 125, 135, 145, 150, 153, 190, 230, 270, 310, 350, 390, 430),
    )

    coverage = list_duckdb_direct_coverage(source, symbols=())
    witness = next(item for item in coverage.intervals if item.scope.symbol == "BTCUSDT").witness

    assert next(iter(coverage.rows)).selectable is True
    assert witness is not None
    assert 150 in witness.shifts_bp


def test_readiness_retains_exact_430_when_optional_shift_is_immediately_above(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=tuple(range(30, 151, 10)) + (190, 230, 270, 310, 350, 385, 425, 430, 435),
    )

    coverage = list_duckdb_direct_coverage(source, symbols=())
    witness = next(item for item in coverage.intervals if item.scope.symbol == "BTCUSDT").witness

    assert next(iter(coverage.rows)).selectable is True
    assert witness is not None
    assert witness.shifts_bp[-1] == 430


def test_optional_points_above_430_do_not_disable_ready_scope(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=tuple(range(30, 151, 10)) + tuple(range(190, 431, 40)),
    )
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
    for shift in (500, 600):
        point = next(item for item in audit_rows if item["shift_bp"] == str(shift))
        assert point["status"] == "AVAILABLE"
        assert point["required_for_readiness"] == "false"
    optional_pair = next(item for item in audit_rows if item["shift_bp"] == "700")
    assert optional_pair["status"] == "AVAILABLE"
    assert optional_pair["open_ma"] == "4"


def test_ready_chain_with_other_factual_gap_is_disabled(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=tuple(range(30, 151, 10)) + tuple(range(190, 431, 40)),
        start_ms=START_MS,
        end_ms=END_MS,
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
    assert row.selectable is False
    assert row.gap_details == ("missing: 2024-01-01 .. 2024-01-01",)
    assert all(not interval.selectable for interval in coverage.intervals)


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
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=tuple(range(30, 151, 10)) + tuple(range(190, 431, 40)),
        start_ms=START_MS,
        end_ms=START_MS + 2 * 3_600_000,
    )
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=tuple(range(30, 151, 10)) + tuple(range(190, 431, 40)),
        open_ma=4,
        close_ma=10,
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


def test_readiness_rejects_exact_11_bp_gap(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(source, symbol="BTCUSDT", shifts=(30, 41, 150, 430))

    csv_text = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    ).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(csv_text)))

    gap = next(row for row in rows if row["row_type"] == "READINESS_GAP")
    assert gap["status"] == "MISSING"
    assert gap["reason_code"] == "SHIFT_GAP_EXCEEDS_MAX"
    assert gap["reason_detail"] == "SHIFT_GAP_EXCEEDS_MAX: gap_start_bp=30, gap_end_bp=41, max_gap_bp=10"
    assert (gap["gap_start_bp"], gap["gap_end_bp"], gap["max_gap_bp"]) == ("30", "41", "10")
    assert next(iter(list_duckdb_direct_coverage(source, symbols=()).rows)).selectable is False


def test_readiness_rejects_exact_41_bp_gap(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=tuple(range(30, 151, 10)) + (191, 430),
    )

    csv_text = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    ).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(csv_text)))

    gap = next(row for row in rows if row["row_type"] == "READINESS_GAP")
    assert gap["status"] == "MISSING"
    assert gap["reason_code"] == "SHIFT_GAP_EXCEEDS_MAX"
    assert gap["reason_detail"] == "SHIFT_GAP_EXCEEDS_MAX: gap_start_bp=150, gap_end_bp=191, max_gap_bp=40"
    assert (gap["gap_start_bp"], gap["gap_end_bp"], gap["max_gap_bp"]) == ("150", "191", "40")


def test_coverage_csv_exact_columns_order_nulls_timestamps_reasons_and_hash(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=tuple(range(30, 151, 10)) + tuple(range(190, 431, 40)),
    )
    _seed_report(source, shift=30, start_ms=START_MS, end_ms=END_MS + 3_600_000, source_hash="overlap")
    _seed_readiness_scope(
        source,
        symbol="ETHUSDT",
        shifts=(30, 430),
        open_ma=4,
        close_ma=10,
    )

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
    assert all(row["readiness_max_shift_bp"] == "430" for row in rows)
    assert all(row["readiness_contract_version"] == "shift_readiness_v1" for row in rows)
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
    assert missing["reason_code"] == "MISSING_BOUNDARY"
    assert missing["reason_detail"] == "MISSING_BOUNDARY: boundary_bp=150"
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


def test_coverage_csv_fixed_bytes_quote_nulls_and_gap_grammar(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(source, symbol="BTCUSDT", shifts=(30, 41, 150, 430))

    csv_bytes = coverage_audit_csv_bytes(
        source,
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    )

    assert csv_bytes == (
        "pair,side,timeframe,evaluation_id,displayed_interval,row_type,shift_bp,open_ma,close_ma,"
        "interval_start_utc,interval_end_utc,report_start_utc,report_end_utc,grid_start_utc,grid_end_utc,"
        "effective_start_utc,effective_end_utc,required_for_readiness,readiness_witness,"
        "gap_start_bp,gap_end_bp,max_gap_bp,report_id,source_sha256,selected_report,status,"
        "reason_code,reason_detail,readiness_contract_version,readiness_max_shift_bp\n"
        "BTCUSDT,LONG,1h,e8a6cd104cf3c098b605ea3bcdfcc3d12dfec45c1ed72e4a86d621509dacfa99,true,"
        "POINT_CANDIDATE,30,3,9,2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,false,,,,,"
        "1e7bcbb887f5687caf2e40e83df1fd0c466dd591a911ed5aa26b73a4e8644cc3,"
        "53946c36ad3aee20e1f50201b545ae8b2d8d56d253e7d08fb6cf36305dda413f,true,AVAILABLE,AVAILABLE,"
        "AVAILABLE: selected_report=true,shift_readiness_v1,430\n"
        "BTCUSDT,LONG,1h,e8a6cd104cf3c098b605ea3bcdfcc3d12dfec45c1ed72e4a86d621509dacfa99,true,"
        "POINT_CANDIDATE,41,3,9,2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,false,,,,,"
        "2cd4ebaae576121c3457fa348dbc848944a6c8ed31c3b1d10968fb63975ac450,"
        "19c40cf2f71095f838b67f8a75f077694fd4197d8d5270f4f5a5787e92bb72a0,true,AVAILABLE,AVAILABLE,"
        "AVAILABLE: selected_report=true,shift_readiness_v1,430\n"
        "BTCUSDT,LONG,1h,e8a6cd104cf3c098b605ea3bcdfcc3d12dfec45c1ed72e4a86d621509dacfa99,true,"
        "POINT_CANDIDATE,150,3,9,2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,false,,,,,"
        "2c2a70f64a3cc1813310500723a50e5e81741fac8b564192498311552d20f166,"
        "a376cedbdd77c3efcfa68c7a4d931a887227408d4279055b0041ad83429e0e9d,true,AVAILABLE,AVAILABLE,"
        "AVAILABLE: selected_report=true,shift_readiness_v1,430\n"
        "BTCUSDT,LONG,1h,e8a6cd104cf3c098b605ea3bcdfcc3d12dfec45c1ed72e4a86d621509dacfa99,true,"
        "POINT_CANDIDATE,430,3,9,2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,"
        "2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,false,,,,,"
        "ed09e85273cbd7dac859ffc9ba6ba0bed58c518b6fdbb41e09a3cc0b39c61ca7,"
        "d9f5bfe82700b75968fa122ec9fda221e7ca3974341672226747a32bf7f01d2f,true,AVAILABLE,AVAILABLE,"
        "AVAILABLE: selected_report=true,shift_readiness_v1,430\n"
        "BTCUSDT,LONG,1h,e8a6cd104cf3c098b605ea3bcdfcc3d12dfec45c1ed72e4a86d621509dacfa99,true,"
        "READINESS_GAP,,3,9,2024-01-01T00:00:00.000+00:00,2024-01-01T02:00:00.000+00:00,,,,,,,true,,"
        "30,41,10,,,,MISSING,SHIFT_GAP_EXCEEDS_MAX,"
        "\"SHIFT_GAP_EXCEEDS_MAX: gap_start_bp=30, gap_end_bp=41, max_gap_bp=10\","
        "shift_readiness_v1,430\n"
    ).encode("utf-8")


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
    shifts = tuple(range(30, 151, 10)) + tuple(range(190, 431, 40))
    _seed_readiness_scope(source, symbol="BTCUSDT", shifts=shifts)
    for shift in shifts:
        _seed_report(
            source,
            symbol="BTCUSDT",
            timeframe="4h",
            shift=shift,
            source_hash=_hash("initial-4h", "BTCUSDT", "LONG", shift, 3, 9, START_MS, END_MS),
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
    for shift in shifts:
        _seed_report(
            source,
            symbol="BTCUSDT",
            timeframe="4h",
            shift=shift,
            source_hash=_hash("changed-4h", "BTCUSDT", "LONG", shift, 3, 9, START_MS, END_MS),
        )

    changed_audit = audit_bytes()
    assert changed_audit == selected_audit

    def v2_request(audit: bytes) -> DirectBuildRequest:
        return _request(
            grid_contract_kind=V2_GRID_CONTRACT_KIND,
            selected_scopes=("BTCUSDT|1h",),
            readiness_contract_version=READINESS_CONTRACT_VERSION,
            readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
            audit_artifact_name="surface_coverage_audit_LONG.csv",
            audit_schema_version=1,
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


def test_v2_preflight_selects_narrowest_reports_and_includes_every_factual_point(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(
        source,
        symbol="BTCUSDT",
        shifts=tuple(range(30, 151, 10)) + tuple(range(190, 431, 40)),
    )
    _seed_report(source, shift=500, source_hash="optional-500")
    _seed_report(source, shift=600, open_ma=4, close_ma=10, source_hash="optional-600")
    _seed_report(
        source,
        shift=430,
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
        readiness_contract_version=READINESS_CONTRACT_VERSION,
        readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
        audit_artifact_name="surface_coverage_audit_LONG.csv",
        audit_schema_version=1,
        audit_row_count=len(list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))),
        audit_sha256=sha256(csv_bytes).hexdigest(),
        audit_bytes=csv_bytes,
    )

    preflight = preflight_duckdb_direct(source, request)

    assert preflight.usable_timeframes == {"BTCUSDT": ("1h",)}
    assert preflight.grid_contract["kind"] == V2_GRID_CONTRACT_KIND
    assert preflight.grid_contract["selected_scopes"] == ["BTCUSDT|1h"]
    witness = preflight.grid_contract["witnesses"]["BTCUSDT|1h"]
    assert witness["symbol"] == "BTCUSDT"
    assert witness["side"] == "LONG"
    assert witness["timeframe"] == "1h"
    assert witness["open_ma"] == 3
    assert witness["close_ma"] == 9
    assert witness["shifts_bp"][-1] == 430
    assert witness["contract_version"] == READINESS_CONTRACT_VERSION
    assert witness["max_shift_bp"] == READINESS_MAX_SHIFT_BP
    assert set(witness) == {"symbol", "side", "timeframe", "open_ma", "close_ma", "shifts_bp", "contract_version", "max_shift_bp"}
    assert len(preflight.accepted_point_keys) == 22
    assert any("|500|" in key for key in preflight.accepted_point_keys)
    assert any("|600|4|10" in key for key in preflight.accepted_point_keys)
    assert len(preflight.manifest) == 22
    assert "overlap-long" not in {hash for _, hash in preflight.manifest}
    assert preflight.point_evidence_sha256 == sha256(
        preflight.grid_contract["point_evidence"].encode("utf-8")
    ).hexdigest()
    assert preflight.audit_sha256 == sha256(csv_bytes).hexdigest()


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
    assert phases == ["PREFLIGHT", "MATERIALIZING", "REVALIDATING", "PUBLISHED"]
    assert published.created is True


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
    long_request = _request(
        side="LONG",
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        selected_scopes=("BTCUSDT|1h",),
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
    long_request = _request(
        side="LONG",
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        selected_scopes=("BTCUSDT|1h",),
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
    request = _request(
        side="LONG",
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        selected_scopes=("BTCUSDT|1h",),
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


def test_v2_preflight_requires_audit_evidence(connections) -> None:
    source, _ = connections
    _seed_readiness_scope(source, symbol="BTCUSDT", shifts=(30, 150, 430))
    request = _request(
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
        selected_scopes=("BTCUSDT|1h",),
        readiness_contract_version=READINESS_CONTRACT_VERSION,
        readiness_max_shift_bp=READINESS_MAX_SHIFT_BP,
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
        "audit_artifact_name": "surface_coverage_audit_LONG.csv",
        "audit_schema_version": 1,
        "audit_sha256": sha256(audit).hexdigest(),
        "audit_bytes": audit,
    }

    with pytest.raises(DirectMaterializationError, match="row count"):
        preflight_duckdb_direct(source, _request(audit_row_count=3, **base))

    preflight = preflight_duckdb_direct(source, _request(audit_row_count=2, **base))
    assert preflight.audit_row_count == 2
