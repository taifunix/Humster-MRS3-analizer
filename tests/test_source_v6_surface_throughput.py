"""S1..S3: the surface publishes sealed payloads instead of rebuilding them."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import duckdb
import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"
SCOPE = "ONUSDT|LONG|1h"


def _ready_facts():
    """One READY scope: the canonical six-CloseMA by nineteen-Shift grid.

    Each variant gets its own `source_sha256`/`source_name`, because that column
    is `unique` in `compact_fragments` — sharing one silently stores a single
    row. `fragment_id` is recomputed afterwards: both fields are part of the
    canonical document.
    """
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import (
        CANONICAL_READINESS_CLOSE_LENGTHS,
        CANONICAL_READINESS_SHIFTS_BP,
    )

    base = normalize_source_v6(FIXTURE.read_bytes())
    facts = []
    for shift in CANONICAL_READINESS_SHIFTS_BP:
        for close in CANONICAL_READINESS_CLOSE_LENGTHS:
            variant = replace(
                base,
                point=replace(base.point, shift_bp=shift, close_ma_length=close),
                source_sha256=sha256(f"{shift}:{close}".encode("ascii")).hexdigest(),
                source_name=f"variant-{shift}-{close}.html",
            )
            facts.append(
                replace(
                    variant,
                    fragment_id=sha256(canonical_fragment_bytes(variant)).hexdigest(),
                )
            )
    return tuple(facts)


def _source_database(path: Path, facts) -> Path:
    from mrs3.source_v6_storage import create_v6_database, import_fragment, preflight_import

    create_v6_database(path)
    for fragment in facts:
        import_fragment(path, fragment, preflight_token=preflight_import(path, fragment))
    return path


def _dump(path: Path) -> dict[str, list[tuple]]:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        return {
            table: sorted(connection.execute(f"select * from {table}").fetchall(), key=repr)
            for table in ("manifest", "scope_manifests", "factual_fragments")
        }
    finally:
        connection.close()


def test_publishing_from_the_source_database_matches_the_python_path(tmp_path: Path) -> None:
    """S1: copying sealed payloads must produce the same surface, not a similar one.

    The stored payload is what `encode_fragment` would rebuild — measured
    byte-identical on the real corpus — so this is the same artifact produced
    with less work, and the whole surface must agree, `surface_id` included.
    """
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    facts = _ready_facts()
    source = _source_database(tmp_path / "source.source-v6.duckdb", facts)
    materialized = materialize_source_v6(facts, (SCOPE,))

    rebuilt = publish_multiscope_surface(tmp_path / "rebuilt", materialized)
    copied = publish_multiscope_surface(tmp_path / "copied", materialized, source_database=source)

    assert rebuilt.name == copied.name, "surface_id must not depend on how the payload got there"
    assert _dump(rebuilt) == _dump(copied)


def test_publishing_from_the_source_database_still_fails_on_a_bad_payload(
    tmp_path: Path,
) -> None:
    """S3: the cheap validation must still reject a payload that is not its id.

    The corruption keeps `payload_sha256` consistent, so only re-deriving the
    identity from the bytes can object — the C3a predicate, not a checksum.
    """
    import zlib

    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    facts = _ready_facts()
    source = _source_database(tmp_path / "source.source-v6.duckdb", facts)
    connection = duckdb.connect(str(source))
    try:
        forged = zlib.compress(b'{"not":"this fragment"}', 9)
        connection.execute(
            "update compact_fragments set payload_blob = ?, payload_sha256 = ?",
            [forged, sha256(forged).hexdigest()],
        )
    finally:
        connection.close()

    with pytest.raises(ValueError, match="payload"):
        publish_multiscope_surface(
            tmp_path / "corrupt", materialize_source_v6(facts, (SCOPE,)), source_database=source
        )
    assert not list((tmp_path / "corrupt").glob("*.surface-v6.duckdb"))


def test_publishing_from_the_source_database_does_not_encode_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 is the point: no fragment may be rebuilt when the payload can be copied.

    The equivalence test above cannot see this — with the pass-through disabled
    both sides rebuild and still agree. Only refusing to encode proves the
    59 ms per fragment is actually gone.
    """
    import mrs3.source_v6_surface_fresh as surface_fresh
    from mrs3.source_v6_materializer import materialize_source_v6

    facts = _ready_facts()
    source = _source_database(tmp_path / "source.source-v6.duckdb", facts)

    def refuse(fragment):
        raise AssertionError("the sealed payload must be copied, not rebuilt")

    monkeypatch.setattr(surface_fresh, "encode_fragment", refuse)
    surface = surface_fresh.publish_multiscope_surface(
        tmp_path / "copied", materialize_source_v6(facts, (SCOPE,)), source_database=source
    )
    assert surface.exists()


def test_publishing_fails_when_the_source_is_missing_a_materialized_fragment(
    tmp_path: Path,
) -> None:
    """A short copy must fail rather than publish a surface missing its own facts.

    The manifest and the scope digest are built from the materialized fragment
    ids, so a source that no longer holds one of them would otherwise produce a
    surface whose digests promise facts the table does not contain.
    """
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    facts = _ready_facts()
    source = _source_database(tmp_path / "source.source-v6.duckdb", facts)
    connection = duckdb.connect(str(source))
    try:
        connection.execute(
            "delete from compact_fragments where fragment_id = ?", [facts[0].fragment_id]
        )
    finally:
        connection.close()

    with pytest.raises(ValueError, match="missing a materialized fragment"):
        publish_multiscope_surface(
            tmp_path / "short", materialize_source_v6(facts, (SCOPE,)), source_database=source
        )
    assert not list((tmp_path / "short").glob("*.surface-v6.duckdb"))
