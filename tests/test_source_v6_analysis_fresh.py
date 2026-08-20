from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"


def test_fresh_analysis_is_separate_and_binds_the_supplied_gap_rules(tmp_path: Path) -> None:
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
    facts = tuple(identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS)
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    default = AlgorithmConfig.defaults()
    changed = replace(default, gap_rules=((30, 551, 10),))

    first = run_multiscope_analysis(surface, tmp_path / "analysis", default, listing_dates={"ONUSDT": "2020-01-01"}, workers=1)
    second = run_multiscope_analysis(surface, tmp_path / "analysis", changed, listing_dates={"ONUSDT": "2020-01-01"}, workers=1)

    assert first != second
    assert first.name.endswith(".analysis-v6.duckdb")
    connection = duckdb.connect(str(second), read_only=True)
    try:
        manifest = dict(connection.execute("select key, value from manifest").fetchall())
        assert manifest["fingerprint"] == "analysis-v6-fresh-compact-v1"
        assert manifest["surface_fingerprint"] == "surface-v6-fresh-compact-v1"
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
