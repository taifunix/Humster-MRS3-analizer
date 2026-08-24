from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"


def test_fresh_frames_strip_admission_fields_except_plateau_audit_count() -> None:
    import mrs3.source_v6_analysis_fresh as fresh

    point = {
        "point_id": "ONUSDT|LONG|1h|100|200",
        "plateau_id": "PLAT_1",
        "symbol": "ONUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "close_ma": 200,
        "open_ma": 100,
        "shift_bp": 250,
        "shift_pct": 2.5,
        "pnl_pct": 12.0,
        "dd_pct": 4.0,
        "efficiency": 3.0,
        "trades": 11,
        "plateau_point_count": 3,
        "base_point_trades": 11,
        "plateau_total_trades": 31,
        "standalone_eligible": True,
        "depth_eligible": True,
        "events_last_30d": 22,
        "plateau_event_count": 44,
    }
    stages = SimpleNamespace(
        points=pd.DataFrame([point]),
        refine_requests=pd.DataFrame(),
        plateaus=pd.DataFrame([{
            "plateau_id": point["plateau_id"],
            "plateau_event_count": point["plateau_event_count"],
        }]),
        close_profiles=pd.DataFrame(),
        base_one_order=pd.DataFrame([point]),
        structures=pd.DataFrame([{
            "structure_id": "STR_legacy",
            "events_last_30d": 22,
            "plateau_event_count": 44,
            "orders": ({"point_id": point["point_id"], "events_last_30d": 22, "plateau_event_count": 44},),
        }]),
        structure_diagnostics=pd.DataFrame(),
    )

    frames = fresh._frames(stages)

    assert "events_last_30d" not in frames["points"][0]
    assert "plateau_event_count" not in frames["points"][0]
    assert frames["plateaus"] == [{
        "plateau_id": point["plateau_id"],
        "plateau_event_count": point["plateau_event_count"],
    }]
    assert all(
        key not in row
        for row in frames["structures"]
        for key in ("events_last_30d", "plateau_event_count")
    )
    legacy = next(row for row in frames["structures"] if row["structure_id"] == "STR_legacy")
    assert all(
        key not in order
        for order in legacy["orders"]
        for key in ("events_last_30d", "plateau_event_count")
    )
    base = next(row for row in frames["structures"] if row["structure_id"].startswith("BASE_"))
    assert base["order_count"] == 1
    assert base["status"] == "READY_MRS3_STRUCTURE"
    for key, value in {
        "plateau_point_count": 3,
        "base_point_trades": 11,
        "plateau_total_trades": 31,
    }.items():
        assert base[key] == base["orders"][0][key] == value


def test_fresh_fallback_adds_events_last_30d_before_frame_conversion(monkeypatch) -> None:
    from datetime import date

    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import ReadyInterval
    import mrs3.source_v6_analysis_fresh as fresh

    fragment = normalize_source_v6(FIXTURE.read_bytes())
    scope = SimpleNamespace(
        ready_witness=ReadyInterval("ONUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 31)),
        facts=(fragment,),
        scope_digest="scope-digest",
    )
    _witness, rows = fresh._measured_rows(scope)

    assert rows
    assert all(type(row["events_last_30d"]) is int for row in rows)


def test_fresh_frames_preserve_legacy_order_and_append_sorted_base_records() -> None:
    import mrs3.source_v6_analysis_fresh as fresh

    def base_point(point_id: str, close_ma: int) -> dict[str, object]:
        return {
            "point_id": point_id,
            "plateau_id": f"PLAT_{point_id}",
            "symbol": "ONUSDT",
            "side": "LONG",
            "timeframe": "1h",
            "close_ma": close_ma,
            "open_ma": 3,
            "shift_bp": 250,
            "shift_pct": 2.5,
            "pnl_pct": 12.0,
            "dd_pct": 4.0,
            "efficiency": 3.0,
            "trades": 11,
            "standalone_eligible": True,
            "depth_eligible": True,
        }

    base_one_order = pd.DataFrame([base_point("BASE_INPUT_B", 200), base_point("BASE_INPUT_A", 220)])
    stages = SimpleNamespace(
        points=pd.DataFrame(),
        refine_requests=pd.DataFrame(),
        plateaus=pd.DataFrame(),
        close_profiles=pd.DataFrame(),
        base_one_order=base_one_order,
        structures=pd.DataFrame([{"structure_id": "LEGACY_Z"}, {"structure_id": "LEGACY_A"}]),
        structure_diagnostics=pd.DataFrame(),
    )

    frames = fresh._frames(stages)
    expected_base_ids = sorted(
        fresh._base_structure(row)["structure_id"]
        for _, row in base_one_order.iterrows()
    )

    assert [row["structure_id"] for row in frames["structures"]] == [
        "LEGACY_Z", "LEGACY_A", *expected_base_ids,
    ]


def test_fresh_analysis_does_not_reuse_legacy_algorithm_artifact(tmp_path: Path) -> None:
    import duckdb
    from mrs3.config import AlgorithmConfig
    from mrs3.pipeline import ALGORITHM_VERSION
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    facts = tuple(
        identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close)))
        for shift in CANONICAL_READINESS_SHIFTS_BP
        for close in CANONICAL_READINESS_CLOSE_LENGTHS
    )
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    output = tmp_path / "analysis"
    legacy = run_multiscope_analysis(
        surface, output, AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
        algorithm_version="0.7-canonical-phase1",
    )
    current = run_multiscope_analysis(
        surface, output, AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
    )

    assert current != legacy
    with duckdb.connect(str(legacy), read_only=True) as connection:
        legacy_manifest = dict(connection.execute("select key, value from manifest").fetchall())
    with duckdb.connect(str(current), read_only=True) as connection:
        current_manifest = dict(connection.execute("select key, value from manifest").fetchall())
    assert legacy_manifest["algorithm_version"] == "0.7-canonical-phase1"
    assert current_manifest["algorithm_version"] == ALGORITHM_VERSION == "0.7-canonical-phase1-base-1ord-v3"
    assert legacy_manifest["analysis_id"] != current_manifest["analysis_id"]


def test_fresh_analysis_is_separate_and_binds_the_supplied_gap_rules(tmp_path: Path, monkeypatch) -> None:
    import duckdb
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    import mrs3.source_v6_analysis_fresh as fresh
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    facts = tuple(identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS)
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    default = AlgorithmConfig.defaults()
    changed = replace(default, gap_rules=((30, 551, 10),))
    read_surface = fresh.read_multiscope_surface
    decode_calls = []
    monkeypatch.setattr(fresh, "read_multiscope_surface", lambda path, *, decode=True: decode_calls.append(decode) or read_surface(path, decode=decode))

    first = run_multiscope_analysis(surface, tmp_path / "analysis", default, listing_dates={"ONUSDT": "2020-01-01"}, workers=1)
    second = run_multiscope_analysis(surface, tmp_path / "analysis", changed, listing_dates={"ONUSDT": "2020-01-01"}, workers=1)

    assert first != second
    assert decode_calls == [False, False]
    assert first.name.endswith(".analysis-v6.duckdb")
    connection = duckdb.connect(str(second), read_only=True)
    try:
        manifest = dict(connection.execute("select key, value from manifest").fetchall())
        assert manifest["fingerprint"] == "analysis-v6-fresh-compact-v1"
        assert manifest["surface_fingerprint"] == "surface-v6-fresh-compact-v2"
        assert manifest["algorithm_config_sha256"] != dict(duckdb.connect(str(first), read_only=True).execute("select key, value from manifest").fetchall())["algorithm_config_sha256"]
        assert connection.execute("select count(*) from points").fetchone()[0] == len(facts)
    finally:
        connection.close()


def test_fresh_analysis_uses_one_read_only_worker_per_scope(tmp_path: Path) -> None:
    import duckdb
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    first = [identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS]
    second = [identified(replace(item, point=replace(item.point, symbol="BTCUSDT"))) for item in first]
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6((*first, *second), ("ONUSDT|LONG|1h", "BTCUSDT|LONG|1h")))

    artifact = run_multiscope_analysis(surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01", "BTCUSDT": "2020-01-01"}, workers=2)

    connection = duckdb.connect(str(artifact), read_only=True)
    try:
        assert connection.execute("select count(*) from scope_runs").fetchone()[0] == 2
    finally:
        connection.close()


def test_fresh_analysis_cancellation_prevents_publication(tmp_path: Path) -> None:
    import pytest
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    facts = tuple(identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS)
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    with pytest.raises(RuntimeError, match="cancelled"):
        run_multiscope_analysis(surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"}, cancel_check=lambda: True)
    assert not tuple((tmp_path / "analysis").glob("*.analysis-v6.duckdb"))


def test_fresh_analysis_accepts_an_editable_human_filename(tmp_path: Path) -> None:
    import pytest
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    base = normalize_source_v6(FIXTURE.read_bytes())
    facts = tuple(
        replace(item, fragment_id=sha256(canonical_fragment_bytes(item)).hexdigest())
        for item in (
            replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))
            for shift in CANONICAL_READINESS_SHIFTS_BP
            for close in CANONICAL_READINESS_CLOSE_LENGTHS
        )
    )
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))

    artifact = run_multiscope_analysis(
        surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
        filename="ON_2026-01-01_2026-01-31.analysis-v6.duckdb",
    )

    assert artifact.name == "ON_2026-01-01_2026-01-31.analysis-v6.duckdb"
    with pytest.raises(FileExistsError, match="already exists"):
        run_multiscope_analysis(
            surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
            filename="ON_2026-01-01_2026-01-31.analysis-v6.duckdb",
        )
