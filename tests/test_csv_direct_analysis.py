"""CSV Direct analysis is the Source v6 fresh analysis with a CSV input.

The decisive test feeds the same points through the Source v6 analysis row
(`_analysis_frame_row`) and through CSV Direct and requires identical frames.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.csv_direct_analysis import _digest, analysis_row, prepare_csv_direct_analysis
from mrs3.csv_direct_input import load_csv_direct
from mrs3.csv_legacy_compat import LEGACY_ANALYSIS_FINGERPRINT, LEGACY_SURFACE_FINGERPRINT
from mrs3.pipeline import _analyze_points
from mrs3.source_v6 import _canonical_json
from mrs3.source_v6_analysis_fresh import _analysis_frame_row, _frames
from tests.csv_direct_support import canonical_grid, report_row, write_tester_zip

DATES = {"XYZUSDT": "2026-01-01"}
PRETEST = {
    "contract_version": "source-v6-pretest-ab-v1", "status": "COMPARABLE", "reason": "FULL_READY_WITNESS",
    "a_start_ms": 0, "a_end_ms": 0, "b_start_ms": 0, "b_end_ms": 0, "a_days": 0, "b_days": 14,
    "a_pnl": "1", "b_pnl": "1", "a_round_trips": 0, "b_round_trips": 0,
}


def _source(tmp_path: Path, rows):
    path = write_tester_zip(tmp_path / "grid.zip", rows)
    return load_csv_direct([path], listing_dates=DATES)


def _v6_frames(source, config):
    """Run the same points as Source v6 would: its own row builder, integer 30-day counts."""
    window = (source.window_start, source.window_end)
    rows = []
    for point in source.frame.to_dict("records"):
        csv_row = analysis_row(point)
        assert csv_row["events_last_30d"] == int(csv_row["events_last_30d"])  # chosen to be integral
        v6_input = {
            **{key: csv_row[key] for key in (
                "point_id", "symbol", "side", "timeframe", "shift_bp", "open_ma", "close_ma",
                "pnl_pct", "dd_pct", "trades", "wins", "losses", "win_rate_pct", "profit_factor",
                "event_ids_hash", "event_mode",
            )},
            "event_ids": list(csv_row["_event_ids"]),
            "events_last_30d": int(csv_row["events_last_30d"]),
            "pretest_ab": PRETEST,
        }
        rows.append(_analysis_frame_row(v6_input, window, DATES))
    return _frames(_analyze_points(pd.DataFrame(rows), config))


def _canonical(frames, *, drop=("pretest_ab",)):
    def strip(value):
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items() if key not in drop}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value
    return strip(json.loads(_canonical_json(frames)))


def test_csv_direct_frames_equal_source_v6_analysis_frames(tmp_path):
    # 240 trades in 60 days -> exactly 120 per 30 days, so v6 integer counts are comparable.
    source = _source(tmp_path, canonical_grid())
    config = AlgorithmConfig()
    payload = prepare_csv_direct_analysis(source, config, listing_dates=DATES)
    csv_frames = payload.results[0]["frames"]
    assert any(row["status"] == "READY_MRS3_STRUCTURE" for row in csv_frames["structures"])
    assert _canonical(csv_frames) == _canonical(_v6_frames(source, config))


def test_plateau_activity_gate_is_applied(tmp_path):
    source = _source(tmp_path, canonical_grid())
    open_gate = prepare_csv_direct_analysis(source, AlgorithmConfig(), listing_dates=DATES)
    assert open_gate.results[0]["frames"]["structures"]
    closed = replace(AlgorithmConfig(), min_plateau_events_per_month=10**6, multi_order_min_plateau_events_per_month=10**6)
    frames = prepare_csv_direct_analysis(source, closed, listing_dates=DATES).results[0]["frames"]
    # The plateau exists but no BASE/multi-order structure passes the activity gate.
    assert frames["plateaus"]
    assert frames["structures"] == []


def test_plateau_activity_is_floor_of_exact_point_sum(tmp_path):
    # 5 trades in 70 days -> 15/7 per point; 108 members sum to 231.43 -> 231.
    window = ("2026-07-04 00:00:00", "2026-09-12 00:00:00")
    source = _source(tmp_path, canonical_grid(trades=5, window=window))
    frames = prepare_csv_direct_analysis(source, AlgorithmConfig(), listing_dates=DATES).results[0]["frames"]
    assert frames["plateaus"]
    for plateau in frames["plateaus"]:
        members = len(plateau["all_point_ids"])
        assert (members * 15) % 7, "test needs a non-integral plateau sum"
        assert plateau["plateau_event_count"] == (members * 15) // 7


def test_points_use_the_fresh_point_schema(tmp_path):
    source = _source(tmp_path, canonical_grid())
    point = prepare_csv_direct_analysis(source, AlgorithmConfig(), listing_dates=DATES).results[0]["frames"]["points"][0]
    assert point["event_mode"] == "real_independent_events"
    assert point["point_event_count"] == point["trades"] == len(point["_event_ids"]) == 240
    assert all(item.startswith("countslot:") for item in point["_event_ids"])
    assert "pretest_ab" not in point
    assert "events_last_30d" not in point


def test_manifest_is_the_v1_fresh_contract_with_csv_provenance(tmp_path):
    source = _source(tmp_path, canonical_grid())
    payload = prepare_csv_direct_analysis(source, AlgorithmConfig(), listing_dates=DATES)
    identity = payload.identity
    assert identity["fingerprint"] == LEGACY_ANALYSIS_FINGERPRINT
    assert identity["surface_fingerprint"] == LEGACY_SURFACE_FINGERPRINT
    assert identity["event_mode"] == "real_independent_events"
    assert "build_mode" not in identity and "analysis_input_digest" not in identity
    assert identity["csv_direct"]["pretest_ab_available"] is False
    assert identity["csv_direct"]["activity_contract"] == "trades_per_30d_capped-v1"
    assert payload.analysis_id == _digest(identity)
    assert set(identity["scope_digests"]) == {"XYZUSDT|LONG|30m"}


def test_scope_digest_depends_only_on_its_own_scope(tmp_path):
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    one = _source(tmp_path / "a", canonical_grid())
    both = _source(tmp_path / "b", canonical_grid() + canonical_grid(timeframe="1h"))
    first = prepare_csv_direct_analysis(one, AlgorithmConfig(), listing_dates=DATES)
    second = prepare_csv_direct_analysis(both, AlgorithmConfig(), listing_dates=DATES)
    key = "XYZUSDT|LONG|30m"
    assert first.identity["scope_digests"][key] == second.identity["scope_digests"][key]
    assert first.analysis_id != second.analysis_id


def test_listing_dates_must_cover_symbols(tmp_path):
    source = _source(tmp_path, [report_row()])
    with pytest.raises(ValueError, match="listing_dates"):
        prepare_csv_direct_analysis(source, AlgorithmConfig(), listing_dates={"OTHERUSDT": "2026-01-01"})


@pytest.mark.parametrize("version", ["1", "true", '"quoted"', "null", ""])
def test_algorithm_version_must_survive_the_manifest_reader(tmp_path, version):
    source = _source(tmp_path, [report_row()])
    with pytest.raises(ValueError, match="algorithm_version"):
        prepare_csv_direct_analysis(source, AlgorithmConfig(), listing_dates=DATES, algorithm_version=version)
