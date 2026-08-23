"""E1..E5: a parameter combination that produced no trades must not abort a surface."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "performance"


def _variant(base, shift: int, close: int, tag: str):
    from mrs3.source_v6 import canonical_fragment_bytes, reconstruct_derived_facts

    item = replace(
        base,
        point=replace(base.point, shift_bp=shift, close_ma_length=close),
        source_sha256=sha256(tag.encode("ascii")).hexdigest(),
        source_name=f"{tag}.html",
    )
    cycles, events, open_tail_cycle_ids = reconstruct_derived_facts(item.actions, item.point)
    item = replace(item, cycles=cycles, events=events, open_tail_cycle_ids=open_tail_cycle_ids)
    return replace(item, fragment_id=sha256(canonical_fragment_bytes(item)).hexdigest())


def _healthy_and_empty():
    """Ten combinations that traded, plus one that was tested and did not."""
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import (
        CANONICAL_READINESS_CLOSE_LENGTHS,
        CANONICAL_READINESS_SHIFTS_BP,
    )

    traded = normalize_source_v6((FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    idle = normalize_source_v6((FIXTURES / "source_v6_zero_activity.html").read_bytes())
    shifts = list(CANONICAL_READINESS_SHIFTS_BP)
    close = list(CANONICAL_READINESS_CLOSE_LENGTHS)[0]
    healthy = tuple(_variant(traded, shift, close, f"ok{shift}") for shift in shifts[:10])
    return healthy, _variant(idle, shifts[10], close, "idle")


def _grid_with_one_idle():
    """A full canonical grid where one combination produced no trades."""
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import (
        CANONICAL_READINESS_CLOSE_LENGTHS,
        CANONICAL_READINESS_SHIFTS_BP,
    )

    traded = normalize_source_v6((FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    idle = normalize_source_v6((FIXTURES / "source_v6_zero_activity.html").read_bytes())
    shifts = list(CANONICAL_READINESS_SHIFTS_BP)
    closes = list(CANONICAL_READINESS_CLOSE_LENGTHS)
    facts = []
    for shift in shifts:
        for close in closes:
            base = idle if (shift, close) == (shifts[0], closes[0]) else traded
            facts.append(_variant(base, shift, close, f"v{shift}_{close}"))
    return tuple(facts), facts[0].point.canonical_key


def _payload(path: Path) -> dict:
    from mrs3.source_v6_surface import read_surface

    return read_surface(path)


def test_one_empty_combination_does_not_abort_the_whole_surface(tmp_path: Path) -> None:
    """E2: all eleven publish, and the eleventh is recorded as what it is.

    Before this, `calculate_metrics` raised inside an unguarded loop and all
    eleven combinations were lost, including the ten that were fine.
    """
    from mrs3.source_v6_surface import publish_surface

    healthy, idle = _healthy_and_empty()
    payload = _payload(publish_surface(tmp_path / "mixed", (*healthy, idle)))

    assert len(payload["point_metrics"]) == 11
    assert idle.point.canonical_key in payload["points"]
    assert [item["point_key"] for item in payload["empty_result_points"]] == [
        idle.point.canonical_key
    ]
    assert payload["empty_result_points"][0]["reason"] == "NO_WALLET_OR_EQUITY_SAMPLES"


def test_a_never_traded_combination_carries_the_declared_result(tmp_path: Path) -> None:
    """E2: the flat result is reported, and the undefined ratio stays undefined.

    Nothing is invented. The report states total PnL zero, drawdown zero and no
    trades, and Z1 verified those agree before admitting it. Profit factor is
    genuinely undefined and stays None under ADR-0006 rather than becoming a
    number.
    """
    from mrs3.source_v6_surface import publish_surface

    healthy, idle = _healthy_and_empty()
    payload = _payload(publish_surface(tmp_path / "declared", (*healthy, idle)))
    row = next(
        item
        for item in payload["point_metrics"]
        if item["point_key"] == idle.point.canonical_key
    )
    assert row["TotalTrades"] == 0
    assert row["Win"] == 0 and row["Los"] == 0
    assert Decimal(str(row["TotalPnLPercent"])) == 0
    assert Decimal(str(row["MaxDrawdownPercent"])) == 0
    assert row["ProfitFactor"] is None


def test_a_never_traded_combination_keeps_the_canonical_grid_whole(tmp_path: Path) -> None:
    """Why it is kept: the pipeline adapter requires all 114 cells.

    Dropping the cell published a 113-of-114 grid that
    `load_source_v6_pipeline_input` then rejected with `INCOMPLETE_GRID`, naming
    neither the reason nor the combination. A loud publish-time failure became a
    quiet artifact that died later.
    """
    from mrs3.source_v6_coverage import (
        CANONICAL_READINESS_CLOSE_LENGTHS,
        CANONICAL_READINESS_SHIFTS_BP,
    )
    from mrs3.source_v6_surface import publish_surface

    facts, idle_key = _grid_with_one_idle()
    payload = _payload(publish_surface(tmp_path / "grid", facts))
    grid = {
        (int(item["shift_bp"]), int(item["close_ma_length"]))
        for item in payload["point_facts"]
    }
    required = {
        (int(shift), int(close))
        for shift in CANONICAL_READINESS_SHIFTS_BP
        for close in CANONICAL_READINESS_CLOSE_LENGTHS
    }
    assert grid == required
    assert idle_key in payload["points"]


def test_a_never_traded_combination_is_rejected_by_eligibility(tmp_path: Path) -> None:
    """Why the flat result is safe: it is visible but can never be selected.

    `annotate_eligibility` runs before plateau geometry and rejects both a
    non-positive PnL and a non-positive drawdown.
    """
    from mrs3.source_v6_surface import publish_surface

    facts, idle_key = _grid_with_one_idle()
    payload = _payload(publish_surface(tmp_path / "eligible", facts))
    rows = {
        str(row["point_key"]): row
        for frame in payload["analysis_facts"].values()
        for row in frame
        if isinstance(row, dict) and "rejected_reasons" in row
    }
    assert idle_key in rows, "the combination must be present, not hidden"
    reasons = set(rows[idle_key]["rejected_reasons"])
    assert {"REJECT_PNL_NONPOSITIVE", "REJECT_DD_NONPOSITIVE"} <= reasons
    # Present in the grid, and assigned to no plateau.
    members = {
        str(row["point_key"]): row
        for row in payload["analysis_facts"]["Plateau Members"]
    }
    assert members[idle_key]["plateau_id"] is None
    assert members[idle_key]["role"] == "UNASSIGNED"


def test_an_interval_with_no_measurable_data_fails_and_names_the_combination(
    tmp_path: Path,
) -> None:
    """E1: a window that hides a combination's data is a bad request, not a zero.

    Distinct from a combination that never traded: here data exists and the
    selected interval cannot see it, so publishing a flat zero would be a lie.
    It raises, and the message names the combination.
    """
    from mrs3.source_v6_stitch import SourceV6EmptySeriesError
    from mrs3.source_v6_surface import publish_surface

    healthy, _idle = _healthy_and_empty()
    dropped = healthy[9]
    span = (dropped.report_start_ms, dropped.report_end_ms)
    intervals = {item.point.canonical_key: span for item in healthy}
    intervals[dropped.point.canonical_key] = (
        dropped.report_end_ms + 10_000_000,
        dropped.report_end_ms + 20_000_000,
    )
    with pytest.raises(SourceV6EmptySeriesError) as raised:
        publish_surface(tmp_path / "windowed", healthy, intervals=intervals)
    assert raised.value.reason == "WINDOW_EXCLUDES_MEASURABLE_DATA"
    assert dropped.point.canonical_key in str(raised.value)


def test_the_db_surface_entry_point_behaves_the_same(tmp_path: Path) -> None:
    """`publish_surface_db` is the path `scripts/import_source_v6_debian.py` uses."""
    from mrs3.source_v6_surface import publish_surface_db, read_surface_db

    healthy, idle = _healthy_and_empty()
    payload = read_surface_db(publish_surface_db(tmp_path / "db", (*healthy, idle)))
    assert len(payload["point_metrics"]) == 11
    assert [item["point_key"] for item in payload["empty_result_points"]] == [
        idle.point.canonical_key
    ]


def test_the_multiscope_surface_keeps_and_records_empty_combinations(tmp_path: Path) -> None:
    """E4a on the path the panel takes: the facts stay, the record is written."""
    import duckdb

    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    facts, idle_key = _grid_with_one_idle()
    materialized = materialize_source_v6(facts, ("ONUSDT|LONG|1h",))
    assert [item["point_key"] for item in materialized.empty_result_points] == [idle_key]

    surface = publish_multiscope_surface(tmp_path / "surfaces", materialized)
    connection = duckdb.connect(str(surface), read_only=True)
    try:
        stored = {
            str(row[0])
            for row in connection.execute("select point_key from factual_fragments").fetchall()
        }
        manifest = dict(connection.execute("select key, value from manifest").fetchall())
    finally:
        connection.close()
    assert idle_key in stored, "the grid must stay whole here too"
    assert json.loads(manifest["empty_result_points"])[0]["point_key"] == idle_key


def test_the_multiscope_analysis_runs_with_an_empty_combination_present(
    tmp_path: Path,
) -> None:
    """The end-to-end failure this whole change exists to remove."""
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    facts, _idle_key = _grid_with_one_idle()
    surface = publish_multiscope_surface(
        tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",))
    )
    artifact = run_multiscope_analysis(
        surface,
        tmp_path / "analysis",
        AlgorithmConfig.defaults(),
        listing_dates={"ONUSDT": "2020-01-01"},
        workers=1,
    )
    assert artifact.exists()


def test_a_surface_without_empty_results_keeps_its_identity(tmp_path: Path) -> None:
    """The invariant, pinned against the values the previous module produced."""
    from mrs3.source_v6_surface import publish_surface

    healthy, _idle = _healthy_and_empty()
    payload = _payload(publish_surface(tmp_path / "identity", healthy))
    assert payload["surface_id"] == (
        "34eb330199b4594317dd76c1cc814bc34953914fa7adb0497e853d768e2874d6"
    )
    assert payload["frozen_facts_sha256"] == (
        "ff5e0fbd8e01c8f0c447be9cc428cb9f4c1bbb8251dd4bdeca5a8244825a3bd7"
    )
    assert payload["empty_result_points"] == []


def test_the_materializer_measures_against_its_own_ready_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E4a: measurability on the multiscope path is judged over the witness."""
    import mrs3.source_v6_materializer as materializer

    facts, _idle_key = _grid_with_one_idle()
    baseline = materializer.materialize_source_v6(facts, ("ONUSDT|LONG|1h",))
    expected = materializer._witness_window(baseline.scopes[0].ready_witness)

    seen: list[object] = []
    real = materializer.measure_points

    def record(members, intervals=None):
        seen.append(intervals)
        return real(members, intervals)

    monkeypatch.setattr(materializer, "measure_points", record)
    materializer.materialize_source_v6(facts, ("ONUSDT|LONG|1h",))

    assert seen and all(item for item in seen), "measurement must be windowed"
    assert {value for item in seen for value in item.values()} == {expected}
