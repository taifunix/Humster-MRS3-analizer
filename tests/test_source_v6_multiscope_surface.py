from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"


def test_multiscope_surface_publishes_all_facts_and_per_scope_digests(tmp_path: Path) -> None:
    import duckdb
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    first = [identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS]
    second = [identified(replace(item, point=replace(item.point, symbol="BTCUSDT"))) for item in first]
    materialized = materialize_source_v6((*first, *second), ("ONUSDT|LONG|1h", "BTCUSDT|LONG|1h"))

    surface = publish_multiscope_surface(tmp_path, materialized)

    assert surface.name.endswith(".surface-v6.duckdb")
    connection = duckdb.connect(str(surface), read_only=True)
    try:
        assert connection.execute("select count(*) from scope_manifests").fetchone()[0] == 2
        assert connection.execute("select count(*) from factual_fragments").fetchone()[0] == len(first) + len(second)
        assert connection.execute("select count(*) from manifest where key='source_content_digest'").fetchone()[0] == 1
        assert connection.execute("select value from manifest where key='source_fingerprint'").fetchone()[0] == "source-v6-fresh-compact-v2"
    finally:
        connection.close()


def test_multiscope_surface_reader_rejects_tampered_scope_digest(tmp_path: Path) -> None:
    import duckdb
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface, read_multiscope_surface

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    facts = tuple(identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS)
    surface = publish_multiscope_surface(tmp_path, materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    assert read_multiscope_surface(surface)["source_content_digest"]
    connection = duckdb.connect(str(surface))
    try:
        connection.execute("update scope_manifests set scope_digest='tampered'")
    finally:
        connection.close()
    import pytest
    with pytest.raises(ValueError, match="scope digest"):
        read_multiscope_surface(surface)


def test_multiscope_surface_reader_rejects_tampered_surface_identity(tmp_path: Path) -> None:
    import duckdb
    import pytest
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface, read_multiscope_surface

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    facts = tuple(identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS)
    surface = publish_multiscope_surface(tmp_path, materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    connection = duckdb.connect(str(surface))
    try:
        connection.execute("update manifest set value='tampered' where key='surface_id'")
    finally:
        connection.close()
    with pytest.raises(ValueError, match="surface identity"):
        read_multiscope_surface(surface)


def test_multiscope_surface_keeps_parent_digest_when_only_some_scopes_are_selected(tmp_path: Path) -> None:
    import duckdb
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface, read_multiscope_surface

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    first = [identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS]
    second = [identified(replace(item, point=replace(item.point, symbol="BTCUSDT"))) for item in first]
    surface = publish_multiscope_surface(tmp_path, materialize_source_v6((*first, *second), ("ONUSDT|LONG|1h",)))

    assert read_multiscope_surface(surface)["scope_count"] == 1
    connection = duckdb.connect(str(surface), read_only=True)
    try:
        manifest = dict(connection.execute("select key, value from manifest").fetchall())
        assert manifest["source_content_digest"] != manifest["surface_facts_digest"]
    finally:
        connection.close()


def test_multiscope_scope_reader_round_trips_all_facts_and_witness(tmp_path: Path) -> None:
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface, read_multiscope_scope

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    grid = [identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS]
    extra = identified(replace(base, point=replace(base.point, shift_bp=35, close_ma_length=9)))
    surface = publish_multiscope_surface(tmp_path, materialize_source_v6((*grid, extra), ("ONUSDT|LONG|1h",)))

    scope = read_multiscope_scope(surface, "ONUSDT|LONG|1h")

    assert len(scope.facts) == len(grid) + 1
    assert any(item.point.shift_bp == 35 and item.point.close_ma_length == 9 for item in scope.facts)
    assert scope.ready_witness.scope_key == "ONUSDT|LONG|1h"
