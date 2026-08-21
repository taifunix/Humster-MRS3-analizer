from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import duckdb
import pytest

from mrs3.locking import OutputDirectoryBusyError
from mrs3.source_v6 import normalize_source_v6
from mrs3.source_v6_importer import source_v6_import_lock
import mrs3.source_v6_merge as source_v6_merge

from mrs3.source_v6_merge import merge_source_v6
from mrs3.source_v6_storage import (
    SourceV6StorageError,
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


MERGE_TABLES = (
    "compact_fragments",
    "points",
    "fragment_origins",
    "day_ownership",
    "import_audit",
    "quarantine",
    "fact_ownership",
    "fragment_resolutions",
)


def _dump_merged(path: Path) -> dict[str, list[tuple]]:
    """Dump every published table, normalising per-run identifiers."""
    connection = duckdb.connect(str(path), read_only=True)
    try:
        dump: dict[str, list[tuple]] = {}
        for table in MERGE_TABLES:
            columns = [
                str(item[0])
                for item in connection.execute(
                    "select column_name from information_schema.columns "
                    "where table_schema='main' and table_name=? order by ordinal_position",
                    [table],
                ).fetchall()
            ]
            rows = []
            for row in connection.execute(f"select * from {table}").fetchall():
                item = []
                for name, value in zip(columns, row):
                    if name == "audit_id":
                        item.append("<id>")
                    elif name.endswith("_at_utc"):
                        item.append(None if value is None else "<ts>")
                    else:
                        item.append(value)
                rows.append(tuple(item))
            dump[table] = sorted(rows, key=repr)
        return dump
    finally:
        connection.close()


def test_merge_publishes_a_stable_table_layout(tmp_path: Path) -> None:
    """Pin the merged artifact so a throughput rewrite cannot change it."""
    first, second = _fragments()
    left, right = tmp_path / "left.duckdb", tmp_path / "right.duckdb"
    _db(left, first, "db-left")
    _db(right, second, "db-right")
    target = tmp_path / "merged.duckdb"

    result = merge_source_v6([left, right], target)

    assert result.status == "COMMITTED"
    dump = _dump_merged(target)
    ids = {row[0] for row in dump["compact_fragments"]}
    assert ids == {first.fragment_id, second.fragment_id}
    assert len(dump["import_audit"]) == 2
    assert sorted((row[6], row[7]) for row in dump["import_audit"]) == [(0, 1), (1, 2)]
    assert {row[5] for row in dump["import_audit"]} == {"COMMITTED"}
    assert database_info(target)["mutation_generation"] == "2"
    assert dump["quarantine"] == []
    # Origins are flattened from both inputs, keeping each origin database id.
    assert {row[3] for row in dump["fragment_origins"]} == {"db-left", "db-right"}
    assert len(dump["day_ownership"]) > 0
    # A second merge of the same inputs must produce the same artifact.
    again = tmp_path / "merged-again.duckdb"
    merge_source_v6([left, right], again)
    assert _dump_merged(again) == dump


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


def test_merge_readback_runs_on_committed_staging_and_publishes_nothing_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin both halves of the post-commit readback placement.

    The readback was moved past `commit` for throughput, which is only sound
    because committing writes a `.staging` file that nothing can reach. Two
    things have to stay true and neither was covered: the readback must still
    run at all, and a failure past the commit must still leave no target and no
    staging residue. Deleting the call leaves every other merge test green.
    """
    first, _ = _fragments()
    source = tmp_path / "source.source-v6.duckdb"
    _db(source, first, "source")
    target = tmp_path / "output.source-v6.duckdb"

    real = source_v6_merge._verify_published_identity
    calls: list[str] = []

    def corrupt_then_verify(connection: duckdb.DuckDBPyConnection) -> None:
        # `rollback` succeeds only while a transaction is open, so its failure
        # is the assertion that the copy transaction has already committed.
        # Move the call back inside the transaction and this raises nothing.
        with pytest.raises(duckdb.TransactionException):
            connection.execute("rollback")
        connection.execute("update compact_fragments set payload_blob = ?", [b"not a payload"])
        calls.append("verified")
        real(connection)

    monkeypatch.setattr(source_v6_merge, "_verify_published_identity", corrupt_then_verify)

    with pytest.raises(SourceV6StorageError, match="readback mismatch"):
        merge_source_v6((source,), target)

    assert calls == ["verified"]
    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.*.staging*"))
