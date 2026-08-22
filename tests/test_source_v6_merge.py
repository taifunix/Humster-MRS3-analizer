from __future__ import annotations

import subprocess
import sys
import zlib
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


def _opens_read_only_from_another_process(path: Path) -> bool:
    """Whether a separate process can open `path` — the worker's actual test.

    DuckDB's in-process instance cache hands a second `connect()` in *this*
    process a working handle even while the first holds an open transaction, so
    an in-process probe proves nothing about release. Across processes the file
    lock is real, and refuses even a read-only open.
    """
    probe = subprocess.run(
        [sys.executable, "-c", f"import duckdb; duckdb.connect(r'{path}', read_only=True)"],
        capture_output=True,
        text=True,
        # DuckDB refuses immediately on Windows rather than blocking on the
        # writer's lock, but a platform that blocks would hang the suite.
        timeout=60,
    )
    return probe.returncode == 0


def test_merge_readback_runs_on_a_released_file_and_publishes_nothing_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin all three halves of the readback placement.

    The readback moved past `commit` for throughput (C8), then out of the copy
    function so it could fan out (C9), then onto `compacted` so it describes
    the file that is published (ADR-0015). Three things have to stay true: it
    must still run at all, the writer that produced the file it checks must be
    released before it runs — a worker process cannot open a file another
    process holds, so a writer left open would fail in production at
    `workers >= 2` and nowhere else — and a failure must still leave no target
    and no staging residue.
    """
    first, _ = _fragments()
    source = tmp_path / "source.source-v6.duckdb"
    _db(source, first, "source")
    target = tmp_path / "output.source-v6.duckdb"

    real = source_v6_merge.verify_published_identity_parallel
    calls: list[str] = []

    def corrupt_then_verify(path, **options) -> None:
        assert _opens_read_only_from_another_process(Path(path)), "the file still has a live writer"
        connection = duckdb.connect(str(path))
        try:
            # The committed rows are here, so the copy transaction closed too.
            assert connection.execute("select count(*) from compact_fragments").fetchone()[0] == 1
            assert Path(path).name.endswith(".packed"), "the readback must target the published file"
            connection.execute("update compact_fragments set payload_blob = ?", [b"not a payload"])
        finally:
            connection.close()
        calls.append("verified")
        real(path, **options)

    monkeypatch.setattr(source_v6_merge, "verify_published_identity_parallel", corrupt_then_verify)

    with pytest.raises(SourceV6StorageError, match="readback mismatch"):
        merge_source_v6((source,), target)

    assert calls == ["verified"]
    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.*.staging*"))


def test_merge_workers_do_not_change_the_published_artifact(tmp_path: Path) -> None:
    """C9: `workers` is a throughput knob, never an input to the artifact.

    `merge_source_v6` does not expose `chunk_size`, and the default of 512 is
    far above any fixture corpus, so a plain `workers=4` merge would
    short-circuit to the serial path and assert nothing. The wrapper forces
    `chunk_size=1` so the parallel merge really does fan out to processes.
    """
    first, second = _fragments()
    inputs = []
    for name, fragment in (("a", first), ("b", second)):
        path = tmp_path / f"in-{name}.source-v6.duckdb"
        _db(path, fragment, f"in-{name}")
        inputs.append(path)

    serial_target = tmp_path / "serial.source-v6.duckdb"
    parallel_target = tmp_path / "parallel.source-v6.duckdb"
    # The merge attaches its inputs read-only, so the same two feed both runs
    # and nothing but the verification width differs between them.
    serial = merge_source_v6(inputs, serial_target, workers=1)

    real = source_v6_merge.verify_published_identity_parallel
    fanned: list[int] = []

    def fan_out(path, *, workers: int = 1, chunk_size: int = 1) -> None:
        fanned.append(workers)
        real(path, workers=workers, chunk_size=1)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(source_v6_merge, "verify_published_identity_parallel", fan_out)
        parallel = merge_source_v6(inputs, parallel_target, workers=4)

    assert fanned == [4]
    assert serial.source_content_digest == parallel.source_content_digest
    assert _dump_merged(serial_target) == _dump_merged(parallel_target)


def test_merge_verifies_the_file_it_publishes_not_the_intermediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0015: the readback must cover `compacted`, the file that survives.

    `compact_v6_database` rewrites the payload bytes into a new file, and that
    new file is what `replace(target)` publishes. Corrupting a payload during
    the repack — checksum repaired, so every column check still passes — must
    fail the merge. While the readback ran on `staging` this corruption
    published silently behind a `safe_to_delete=YES`.
    """
    first, _ = _fragments()
    source = tmp_path / "source.source-v6.duckdb"
    _db(source, first, "source")
    target = tmp_path / "output.source-v6.duckdb"

    real = source_v6_merge.compact_v6_database

    def compact_then_corrupt(source_path, target_path) -> None:
        real(source_path, target_path)
        connection = duckdb.connect(str(target_path))
        try:
            # A valid payload for a different document: it decompresses, and
            # its stored checksum matches, so only re-deriving `fragment_id`
            # from the bytes can object.
            forged = zlib.compress(b'{"not":"this fragment"}', 9)
            connection.execute(
                "update compact_fragments set payload_blob = ?, payload_sha256 = ?",
                [forged, sha256(forged).hexdigest()],
            )
        finally:
            connection.close()

    monkeypatch.setattr(source_v6_merge, "compact_v6_database", compact_then_corrupt)

    with pytest.raises(SourceV6StorageError, match="readback mismatch"):
        merge_source_v6((source,), target)

    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.*.staging*"))
