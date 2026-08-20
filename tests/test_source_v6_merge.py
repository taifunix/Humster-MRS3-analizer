from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import duckdb
import pytest

from mrs3.locking import OutputDirectoryBusyError
from mrs3.source_v6 import normalize_source_v6
from mrs3.source_v6_importer import source_v6_import_lock
from mrs3.source_v6_merge import merge_source_v6
from mrs3.source_v6_storage import (
    create_v6_database,
    database_info,
    import_fragment,
    iter_fragments,
    preflight_import,
)


FIXTURES = Path(__file__).parent / "fixtures" / "performance"


def _db(path: Path, fragment, database_id: str) -> None:
    create_v6_database(path, database_id=database_id)
    import_fragment(path, fragment, preflight_token=preflight_import(path, fragment))


def _fragments():
    first = normalize_source_v6(
        (FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes(),
        source_name="a.html",
    )
    second = normalize_source_v6(
        (FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes(),
        source_name="b.html",
    )
    return first, second


def _database_artifact_hashes(path: Path) -> tuple[str | None, ...]:
    """Evidence helper covering the DB plus DuckDB/WAL staging sidecars."""
    return tuple(
        None if not artifact.exists() else sha256(artifact.read_bytes()).hexdigest()
        for artifact in (path, Path(f"{path}.wal"), Path(f"{path}.tmp"))
    )


def test_merge_deduplicates_fragments_recomputes_global_stitch_and_flattens_origins(tmp_path: Path) -> None:
    first, second = _fragments()
    first_alt = normalize_source_v6(
        (FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes(),
        source_name="z.html",
    )
    left = tmp_path / "left.source-v6.duckdb"
    right = tmp_path / "right.source-v6.duckdb"
    _db(left, first, "left")
    _db(right, first_alt, "right")
    import_fragment(right, second, preflight_token=preflight_import(right, second))
    before = _database_artifact_hashes(left), _database_artifact_hashes(right)

    target = tmp_path / "merged.source-v6.duckdb"
    result = merge_source_v6((left, right), target)

    assert result.status == "COMMITTED"
    assert result.duplicate_count == 1
    assert tuple(item.fragment_id for item in iter_fragments(target)) == tuple(sorted((first.fragment_id, second.fragment_id)))
    assert database_info(target)["source_content_digest"] == result.source_content_digest
    assert _database_artifact_hashes(left) == before[0]
    assert _database_artifact_hashes(right) == before[1]
    connection = duckdb.connect(str(target), read_only=True)
    try:
        assert connection.execute("select count(*) from fragment_origins").fetchone()[0] == 3
        assert connection.execute("select count(*) from compact_fragments where active").fetchone()[0] == 2
    finally:
        connection.close()


def test_merge_publishes_the_compacted_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _fragments()
    left = tmp_path / "left.source-v6.duckdb"
    right = tmp_path / "right.source-v6.duckdb"
    _db(left, first, "left")
    _db(right, second, "right")
    target = tmp_path / "merged.source-v6.duckdb"
    import mrs3.source_v6_merge as merge_module
    calls: list[tuple[Path, Path]] = []
    original = merge_module.compact_v6_database

    def compact(source: Path, published: Path) -> None:
        calls.append((source, published))
        original(source, published)

    monkeypatch.setattr(merge_module, "compact_v6_database", compact)

    assert merge_source_v6((left, right), target).status == "COMMITTED"
    assert calls and calls[0][0] != target and calls[0][1] != target


def test_merge_is_associative_for_fragment_order_and_rejects_existing_or_busy_target(tmp_path: Path) -> None:
    first, second = _fragments()
    left = tmp_path / "left.source-v6.duckdb"
    right = tmp_path / "right.source-v6.duckdb"
    _db(left, first, "left")
    _db(right, second, "right")

    ab = tmp_path / "ab.source-v6.duckdb"
    ba = tmp_path / "ba.source-v6.duckdb"
    merge_source_v6((left, right), ab)
    merge_source_v6((right, left), ba)
    assert database_info(ab)["source_content_digest"] == database_info(ba)["source_content_digest"]
    assert tuple(item.fragment_id for item in iter_fragments(ab)) == tuple(item.fragment_id for item in iter_fragments(ba))

    existing = tmp_path / "existing.source-v6.duckdb"
    create_v6_database(existing)
    with pytest.raises(Exception, match="already exists"):
        merge_source_v6((left,), existing)



def test_merge_held_shared_lock_rejects_before_target_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, _ = _fragments()
    source = tmp_path / "source.source-v6.duckdb"
    _db(source, first, "source")
    target = tmp_path / "busy.source-v6.duckdb"
    writes: list[Path] = []

    import mrs3.source_v6_merge as merge_module

    original_create = merge_module.create_v6_database

    def record_write(path: Path, **kwargs: object) -> object:
        writes.append(path)
        return original_create(path, **kwargs)

    monkeypatch.setattr(merge_module, "create_v6_database", record_write)
    with source_v6_import_lock(target):
        with pytest.raises(OutputDirectoryBusyError, match="already being written"):
            merge_source_v6((source,), target)

    assert writes == []
    assert not target.exists()


def test_merge_failure_removes_staging_and_keeps_target_absent(tmp_path: Path) -> None:
    first, _ = _fragments()
    source = tmp_path / "source.source-v6.duckdb"
    _db(source, first, "source")
    target = tmp_path / "output.source-v6.duckdb"

    def fail(stage: str) -> None:
        if stage == "before_publish":
            raise RuntimeError("injected merge failure")

    with pytest.raises(RuntimeError, match="injected merge failure"):
        merge_source_v6((source,), target, fault_injector=fail)
    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.*.staging*"))


def test_merge_recovers_target_specific_orphan_staging_and_republishes_clean_digest(tmp_path: Path) -> None:
    first, second = _fragments()
    left = tmp_path / "left.source-v6.duckdb"
    right = tmp_path / "right.source-v6.duckdb"
    _db(left, first, "left")
    _db(right, second, "right")

    expected = tmp_path / "expected.source-v6.duckdb"
    expected_result = merge_source_v6((left, right), expected)

    target = tmp_path / "recovered.source-v6.duckdb"
    orphan = tmp_path / f".{target.name}.killed.staging"
    orphan.write_bytes(b"partial merge before commit")
    Path(f"{orphan}.wal").write_bytes(b"partial wal")
    unrelated = tmp_path / ".other-target.killed.staging"
    unrelated.write_bytes(b"leave this orphan alone")

    result = merge_source_v6((left, right), target)

    assert result.source_content_digest == expected_result.source_content_digest
    assert target.exists()
    assert not orphan.exists()
    assert not Path(f"{orphan}.wal").exists()
    assert unrelated.exists()


def test_merge_canonicalizes_duplicate_paths_without_collapsing_distinct_content(tmp_path: Path) -> None:
    first, second = _fragments()
    left = tmp_path / "left.source-v6.duckdb"
    right = tmp_path / "right.source-v6.duckdb"
    _db(left, first, "left")
    _db(right, second, "right")

    target = tmp_path / "merged.source-v6.duckdb"
    result = merge_source_v6((left, left.resolve(), right), target)

    assert result.input_count == 2
    assert result.accepted_count == 2
    assert result.duplicate_count == 0
    assert {item.fragment_id for item in iter_fragments(target)} == {first.fragment_id, second.fragment_id}


def test_merge_rejects_input_sidecar_mutation_before_publication(tmp_path: Path) -> None:
    first, _ = _fragments()
    source = tmp_path / "source.source-v6.duckdb"
    _db(source, first, "source")
    target = tmp_path / "output.source-v6.duckdb"
    sidecar = Path(f"{source}.wal")

    def mutate_sidecar(stage: str) -> None:
        if stage == "after_write":
            sidecar.write_bytes(b"mutated source WAL")

    try:
        with pytest.raises(Exception, match="merge input changed before publication"):
            merge_source_v6((source,), target, fault_injector=mutate_sidecar)
    finally:
        sidecar.unlink(missing_ok=True)
    assert not target.exists()


def test_merge_result_reports_all_active_fragments_including_non_fixed_lot(tmp_path: Path) -> None:
    fixed, _ = _fragments()
    non_fixed = normalize_source_v6(
        (FIXTURES / "source_v6_legacy_nonstitchable.html").read_bytes(),
        source_name="legacy.html",
    )
    first = tmp_path / "fixed.source-v6.duckdb"
    second = tmp_path / "legacy.source-v6.duckdb"
    _db(first, fixed, "fixed")
    _db(second, non_fixed, "legacy")

    target = tmp_path / "mixed.source-v6.duckdb"
    result = merge_source_v6((first, second), target)

    expected = {fixed.fragment_id, non_fixed.fragment_id}
    assert {item.fragment_id for item in result.active_fragments} == expected
    connection = duckdb.connect(str(target), read_only=True)
    try:
        rows = connection.execute("select fragment_id from compact_fragments where active order by fragment_id").fetchall()
    finally:
        connection.close()
    assert {str(row[0]) for row in rows} == expected
