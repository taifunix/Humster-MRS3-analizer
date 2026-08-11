from __future__ import annotations

import json
from hashlib import sha256
from datetime import datetime

import duckdb
import pytest

from mrs3.duckdb_direct import (
    DirectBuildRequest,
    DirectMaterializationError,
    materialize_duckdb_direct,
    preflight_duckdb_direct,
)
from mrs3.duckdb_events import ACTION_CODEC, EQUITY_CODEC
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
) -> str:
    point_key = f"{symbol}|LONG|{timeframe}|{shift}|{open_ma}|{close_ma}"
    point = {
        "canonical_point_key": point_key, "symbol": symbol, "side": "LONG", "timeframe": timeframe,
        "shift_bp": shift, "open_ma_type": "EMA", "open_ma_source": "close", "open_ma_len": open_ma,
        "open_multiplier_raw": "0.99", "close_ma_type": "EMA", "close_ma_source": "close", "close_ma_len": close_ma,
    }
    point["row_sha256"] = _point_hash(point)
    source.execute("insert into point_configs values (?,?,?,?,?,?,?,?,?,?,?,?,?)", list(point.values()))
    timestamps = (start_ms, (start_ms + end_ms) // 2, end_ms)
    import struct, zlib
    grid = {"grid_hash": _grid_content_hash(timestamps), "sample_count": len(timestamps), "start_timestamp_ms": start_ms, "end_timestamp_ms": end_ms, "timestamps_zlib": zlib.compress(struct.pack("<3q", timestamps[0], timestamps[1] - timestamps[0], timestamps[2] - timestamps[1]))}
    grid["row_sha256"] = _grid_hash(grid)
    if source.execute("select count(*) from time_grids where grid_hash=?", [grid["grid_hash"]]).fetchone()[0] == 0:
        source.execute("insert into time_grids values (?,?,?,?,?,?)", list(grid.values()))
    canonical = canonical_report_key({"canonical_point_key": point_key, "report_period_start_ms": start_ms, "report_period_end_ms": end_ms})
    source_hash = source_hash or _hash(canonical)
    report_id = _hash(canonical, source_hash)
    report = {"report_id": report_id, "canonical_report_key": canonical, "canonical_point_key": point_key, "grid_hash": grid["grid_hash"], "source_sha256": source_hash, "source_file": "fixture.html", "source_size": 1, "imported_at_utc": datetime(2026, 8, 11), "settings_json": "{}", "raw_action_count": 0, "equity_sample_count": 3, "wallet_change_count": 1, "report_period_start_ms": start_ms, "report_period_end_ms": end_ms}
    report["row_sha256"] = _report_hash(report)
    source.execute("insert into active_reports values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", list(report.values()))
    payload = {"report_id": report_id, "series_codec": EQUITY_CODEC, "actions_codec": ACTION_CODEC, "actions_zlib": zlib.compress(json.dumps({"headers": [], "rows": []}).encode()), "equity_zlib": zlib.compress(struct.pack("<3q", 100, 90, 110)), "wallet_zlib": zlib.compress(struct.pack("<Iq", 0, 100))}
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


def test_preflight_grid_contract_is_immutable(connections) -> None:
    source, _ = connections
    _seed_report(source)

    preflight = preflight_duckdb_direct(source, _request())

    with pytest.raises(TypeError):
        preflight.grid_contract["kind"] = "MUTATED"  # type: ignore[index]
    with pytest.raises(AttributeError):
        preflight.grid_contract["required_shifts_bp"].append(200)  # type: ignore[union-attr]


def test_preflight_excludes_missing_grid_cells_and_marks_symbol_unavailable(connections) -> None:
    source, _ = connections
    _seed_report(source, shift=100)
    _seed_report(source, shift=200, open_ma=4, close_ma=10)

    preflight = preflight_duckdb_direct(source, _request(required_shifts_bp=(100, 200)))

    assert preflight.usable_timeframes == {}
    assert preflight.unavailable_symbols == {"BTCUSDT": ("1h",)}
    assert {issue.code for issue in preflight.coverage_issues} == {"MISSING_GRID_CELL"}


def test_materialization_uses_trades_proxy_and_does_not_publish_to_analysis(connections) -> None:
    source, analysis = connections
    point = _seed_report(source, trades=7)

    surface = materialize_duckdb_direct(source, analysis, _request(), lambda: False)

    assert surface.event_mode == "legacy_trades_proxy"
    assert surface.points[0].canonical_point_key == point
    assert surface.points[0].point_event_count == surface.points[0].metrics["TotalTrades"] == 0
    assert analysis.execute("select count(*) from information_schema.tables").fetchone() == (0,)


def test_materialization_rejects_same_connection_and_cancellation_before_publication(connections) -> None:
    source, analysis = connections
    _seed_report(source)
    with pytest.raises(DirectMaterializationError, match="distinct"):
        materialize_duckdb_direct(source, source, _request(), lambda: False)
    with pytest.raises(DirectMaterializationError, match="cancelled"):
        materialize_duckdb_direct(source, analysis, _request(), lambda: True)
    assert analysis.execute("select count(*) from information_schema.tables").fetchone() == (0,)
