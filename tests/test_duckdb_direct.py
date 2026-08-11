from __future__ import annotations

import json
from hashlib import sha256
from datetime import datetime
from dataclasses import replace

import duckdb
import pytest

from mrs3.duckdb_direct import (
    CoverageIssue,
    DirectBuildRequest,
    DirectMaterializationError,
    materialize_duckdb_direct,
    preflight_duckdb_direct,
)
from mrs3.analysis_storage import publish_surface
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
    ):
        with pytest.raises(ValueError):
            publish_surface(analysis, invalid)
    assert analysis.execute("select count(*) from information_schema.tables").fetchone()[0] == 13
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
