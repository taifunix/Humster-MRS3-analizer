from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

import mrs3.portfolio.spread_screen as spread_module
from mrs3.bybit_collector.storage import PublishedHour
from mrs3.portfolio.liquidity import LiquidityError
from mrs3.portfolio.spread_screen import read_spread_history, screen_spread


def row(symbol="BTCUSDT", side="LONG", name="candidate", shifts=(10,)):
    return {
        "symbol": symbol,
        "side": side,
        "strategy_name": name,
        "strategy_orders": tuple({"shift_bp": shift} for shift in shifts),
    }


def test_strict_comparison_and_equality_are_distinct():
    result = screen_spread(
        [row(name="clear", shifts=(Decimal("10.01"),)), row(name="equal", shifts=(10,))],
        {"BTCUSDT": [{"spread_bps_p95": "10"}]},
        {"BTCUSDT": "READY"},
    )

    assert [item["strategy_name"] for item in result.retained_rows] == ["clear"]
    assert result.exclusions[0].reason == "OVERLAPS_SPREAD"
    assert result.retained_rows[0]["spread_status"] == "CLEAR"
    assert result.retained_rows[0]["spread_mean_bps"] == Decimal("10")


def test_mixed_group_excludes_overlaps_but_keeps_unknown_and_isolates_side():
    candidates = [
        row(name="long-clear", shifts=(11,)),
        row(name="long-overlap", shifts=(9,)),
        row(name="short-overlap", side="SHORT", shifts=(9,)),
    ]
    result = screen_spread(
        candidates,
        {"BTCUSDT": [{"spread_bps_p95": 10}]},
        {"BTCUSDT": "READY"},
    )

    assert [item["strategy_name"] for item in result.retained_rows] == ["long-clear", "short-overlap"]
    assert [item.row["strategy_name"] for item in result.exclusions] == ["long-overlap"]


def test_all_overlap_fallback_retains_group_with_warning():
    result = screen_spread(
        [row(name="a", shifts=(9,)), row(name="b", shifts=(10,))],
        {"BTCUSDT": [{"spread_bps_p95": "10"}]},
        {"BTCUSDT": "READY"},
    )

    assert [item["strategy_name"] for item in result.retained_rows] == ["a", "b"]
    assert "ALL_FINALISTS_OVERLAP_SPREAD" in result.warnings
    assert result.exclusions == ()


def test_missing_history_retains_unknown_and_preliminary_warns():
    unknown = screen_spread([row()], {}, {})
    assert unknown.retained_rows[0]["spread_status"] == "UNKNOWN"
    assert "SPREAD_HISTORY_UNKNOWN" in unknown.warnings
    assert unknown.retained_rows[0]["spread_mean_bps"] is None

    preliminary = screen_spread(
        [row(shifts=(11,))],
        {"BTCUSDT": [{"spread_bps_p95": "10"}]},
        {"BTCUSDT": "PRELIMINARY"},
    )
    assert preliminary.retained_rows[0]["spread_status"] == "CLEAR"
    assert "SPREAD_HISTORY_PRELIMINARY" in preliminary.warnings


def test_invalid_geometry_is_retained_with_warning():
    candidate = row(shifts=(11,))
    candidate["strategy_orders"] = ({"other": 1},)
    result = screen_spread(
        [candidate],
        {"BTCUSDT": [{"spread_bps_p95": "10"}]},
        {"BTCUSDT": "READY"},
    )

    assert result.retained_rows[0]["spread_status"] == "UNKNOWN"
    assert "INVALID_ORDER_GEOMETRY" in result.retained_rows[0]["spread_diagnostics"]
    assert result.exclusions == ()


def test_input_mappings_and_order_are_unchanged_and_results_are_immutable():
    candidates = [row(name="second", shifts=(11,)), row(name="first", shifts=(9,))]
    original = deepcopy(candidates)
    result = screen_spread(
        candidates,
        {"BTCUSDT": [{"spread_bps_p95": "10"}]},
        {"BTCUSDT": "READY"},
    )

    assert candidates == original
    assert [item["strategy_name"] for item in result.retained_rows] == ["second"]
    with pytest.raises(TypeError):
        result.retained_rows[0]["new"] = 1


@pytest.mark.parametrize("value", [0.1, float("nan"), float("inf"), Decimal("NaN"), Decimal("-1"), -1])
def test_rejects_float_nonfinite_and_negative_observations(value):
    with pytest.raises((TypeError, ValueError)):
        screen_spread([row()], {"BTCUSDT": [{"spread_bps_p95": value}]}, {"BTCUSDT": "READY"})


def test_history_summary_has_exact_mean_and_provenance_counts():
    result = screen_spread(
        [row(shifts=(11,))],
        {"BTCUSDT": [{"spread_bps_p95": "0.1"}, {"spread_bps_p95": "0.2"}]},
        {"BTCUSDT": "READY"},
    )

    summary = result.per_symbol[0]
    assert summary.mean_spread_bps == Decimal("0.15")
    assert summary.observation_count == 2
    assert summary.usable_observation_count == 2


def test_result_order_is_independent_of_input_order():
    rows = [
        {**row("ETHUSDT", name="eth", shifts=(11,)), "strategy_id": 2, "result_id": 20},
        {**row("BTCUSDT", name="btc", shifts=(11,)), "strategy_id": 1, "result_id": 10},
    ]
    observations = {
        "BTCUSDT": [{"spread_bps_p95": "10"}],
        "ETHUSDT": [{"spread_bps_p95": "10"}],
    }
    statuses = {"BTCUSDT": "READY", "ETHUSDT": "READY"}

    first = screen_spread(rows, observations, statuses)
    second = screen_spread(list(reversed(rows)), observations, statuses)

    assert first.retained_rows == second.retained_rows


class _MarkerSpool:
    def __init__(self, markers):
        self.markers = tuple(markers)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def published_hours(self):
        return self.markers


def _patch_spread_reader(monkeypatch, markers, rows):
    monkeypatch.setattr(
        spread_module.SQLiteSpool,
        "open_read_only",
        lambda _root: _MarkerSpool(markers),
    )
    monkeypatch.setattr(
        spread_module,
        "_read_marked_file",
        lambda _root, marker: tuple(rows[marker.file_name]),
    )


def _markers(count):
    return tuple(
        PublishedHour(86_400_000 + index * 3_600_000, f"part-{index}", 1, index)
        for index in range(count)
    )


def test_reader_returns_available_rows_and_preliminary_status_without_filling_gaps(monkeypatch, tmp_path: Path):
    markers = _markers(2)
    rows = {
        marker.file_name: ({"minute_ts_ms": marker.hour_start_ms, "symbol": "BTCUSDT", "spread_bps_p95": 1.5, "coverage_ratio": 1},)
        for marker in markers
    }
    _patch_spread_reader(monkeypatch, markers, rows)

    result = read_spread_history(tmp_path, ["BTCUSDT", "ETHUSDT"], now_ms=8 * 86_400_000)

    assert result.statuses == {"BTCUSDT": "PRELIMINARY", "ETHUSDT": "UNKNOWN"}
    assert len(result.observations["BTCUSDT"]) == 2
    assert result.observations["BTCUSDT"][0]["spread_bps_p95"] == Decimal("1.5")
    assert result.observations["ETHUSDT"] == ()
    with pytest.raises(TypeError):
        result.observations["BTCUSDT"] = ()


def test_reader_marks_complete_168_unique_hours_ready(monkeypatch, tmp_path: Path):
    markers = _markers(168)
    rows = {
        marker.file_name: ({"minute_ts_ms": marker.hour_start_ms, "symbol": "BTCUSDT", "spread_bps_p95": "2", "coverage_ratio": 1},)
        for marker in markers
    }
    _patch_spread_reader(monkeypatch, markers, rows)

    result = read_spread_history(tmp_path, ["BTCUSDT"], now_ms=8 * 86_400_000)

    assert result.statuses == {"BTCUSDT": "READY"}
    assert len(result.observations["BTCUSDT"]) == 168


def test_reader_rejects_duplicate_markers_and_rows(monkeypatch, tmp_path: Path):
    markers = _markers(2)
    rows = {
        marker.file_name: ({"minute_ts_ms": marker.hour_start_ms, "symbol": "BTCUSDT", "spread_bps_p95": "2", "coverage_ratio": 1},)
        for marker in markers
    }
    _patch_spread_reader(monkeypatch, (markers[0], markers[0]), rows)
    with pytest.raises(LiquidityError) as marker_error:
        read_spread_history(tmp_path, ["BTCUSDT"], now_ms=8 * 86_400_000)
    assert marker_error.value.reason == "LIQUIDITY_QUALITY_INSUFFICIENT"

    _patch_spread_reader(monkeypatch, markers, {
        marker.file_name: ({"minute_ts_ms": markers[0].hour_start_ms, "symbol": "BTCUSDT", "spread_bps_p95": "2", "coverage_ratio": 1},)
        for marker in markers
    })
    with pytest.raises(LiquidityError) as row_error:
        read_spread_history(tmp_path, ["BTCUSDT"], now_ms=8 * 86_400_000)
    assert row_error.value.reason == "LIQUIDITY_QUALITY_INSUFFICIENT"


def test_reader_excludes_minutes_below_configured_coverage(monkeypatch, tmp_path: Path):
    marker = _markers(1)[0]
    _patch_spread_reader(monkeypatch, (marker,), {
        marker.file_name: (
            {"minute_ts_ms": marker.hour_start_ms, "symbol": "BTCUSDT", "spread_bps_p95": "2", "coverage_ratio": "0.89"},
        ),
    })

    result = read_spread_history(tmp_path, ["BTCUSDT"], now_ms=8 * 86_400_000, minimum_coverage_pct=90)

    assert result.statuses["BTCUSDT"] == "PRELIMINARY"
    assert result.observations["BTCUSDT"] == ()


def test_reader_rejects_coverage_ratio_above_one(monkeypatch, tmp_path: Path):
    marker = _markers(1)[0]
    _patch_spread_reader(monkeypatch, (marker,), {
        marker.file_name: (
            {"minute_ts_ms": marker.hour_start_ms, "symbol": "BTCUSDT", "spread_bps_p95": "2", "coverage_ratio": "1.01"},
        ),
    })

    with pytest.raises(LiquidityError) as error:
        read_spread_history(tmp_path, ["BTCUSDT"], now_ms=8 * 86_400_000)

    assert error.value.reason == "LIQUIDITY_QUALITY_INSUFFICIENT"


def test_preliminary_history_warns_but_does_not_remove_overlap_candidate():
    rows = (
        row(name="overlap", shifts=("5",)),
        row(name="clear", shifts=("20",)),
    )

    result = screen_spread(
        rows,
        {"BTCUSDT": ({"spread_bps_p95": "10"},)},
        {"BTCUSDT": "PRELIMINARY"},
    )

    assert [item["strategy_name"] for item in result.retained] == ["clear", "overlap"]
    assert result.exclusions == ()
    assert "SPREAD_HISTORY_PRELIMINARY" in result.warnings
