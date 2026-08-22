from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from hashlib import sha256

import duckdb
import pytest

import mrs3.source_v6_storage as storage
from mrs3.source_v6 import canonical_fragment_id, encode_fragment, normalize_source_v6
from mrs3.source_v6_storage import (
    SourceV6SegmentOutcome,
    SourceV6SegmentReceipt,
    SourceV6StorageError,
    create_v6_database,
    database_info,
    import_fragment,
    iter_fragments,
    preflight_import,
    read_fragment,
    reconstruct_fragment,
    set_day_disposition,
    apply_fragment_resolution,
    compact_v6_database,
    merge_source_v6_segments,
    reduce_source_v6_segments,
    write_source_v6_segment,
)


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"


def _fragment():
    return normalize_source_v6(FIXTURE.read_bytes(), source_name="report.html")


def _fragment_b():
    return normalize_source_v6((FIXTURE.parent / "source_v6_fixed_lot_overlap_b.html").read_bytes(), source_name="report-b.html")


def _unique_fragment(index: int):
    fragment = _fragment()
    fragment = replace(
        fragment,
        source_name=f"report-{index}.html",
        source_sha256=sha256(f"source-{index}".encode()).hexdigest(),
        point=replace(fragment.point, shift_bp=fragment.point.shift_bp + index),
    )
    return replace(fragment, fragment_id=canonical_fragment_id(fragment))


def _accepted_outcome(ordinal: int, fragment, *, relative_path: str | None = None):
    return SourceV6SegmentOutcome(
        ordinal=ordinal,
        relative_path=relative_path or fragment.source_name,
        status="PREPARED",
        source_sha256=fragment.source_sha256,
        fragment_id=fragment.fragment_id,
    )


def _write_leaf(path: Path, outcomes, prepared, *, run_token: str = "run-a") -> SourceV6SegmentReceipt:
    return write_source_v6_segment(
        path,
        outcomes,
        prepared,
        run_token=run_token,
    )


def test_sealed_segment_has_exact_schema_and_fingerprint(tmp_path: Path) -> None:
    fragment = _fragment()
    segment = tmp_path / "run-a-0000.segment.duckdb"

    receipt = _write_leaf(
        segment,
        [_accepted_outcome(0, fragment)],
        [(0, fragment, encode_fragment(fragment))],
    )

    assert isinstance(receipt, SourceV6SegmentReceipt)
    connection = duckdb.connect(str(segment), read_only=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'main'"
            ).fetchall()
        }
        assert tables == {
            "segment_manifest",
            "segment_outcomes",
            "segment_compact_rows",
        }
        assert connection.execute(
            "select fingerprint, sealed, checkpointed from segment_manifest"
        ).fetchone() == (
            "source-v6-import-segment-v1",
            True,
            True,
        )
    finally:
        connection.close()

    assert not Path(f"{segment}.wal").exists()


def test_segment_outcomes_and_compact_rows_correspond_by_ordinal(tmp_path: Path) -> None:
    fragment = _fragment()
    quarantine_sha = sha256(b"stable parse failure").hexdigest()
    segment = tmp_path / "run-a-0000.segment.duckdb"

    _write_leaf(
        segment,
        [
            _accepted_outcome(0, fragment),
            SourceV6SegmentOutcome(
                ordinal=1,
                relative_path="broken.html",
                status="QUARANTINED",
                source_sha256=quarantine_sha,
                reason="invalid report",
            ),
        ],
        [(0, fragment, encode_fragment(fragment))],
    )

    connection = duckdb.connect(str(segment), read_only=True)
    try:
        assert connection.execute(
            "select ordinal, status, source_sha256 from segment_outcomes order by ordinal"
        ).fetchall() == [
            (0, "PREPARED", fragment.source_sha256),
            (1, "QUARANTINED", quarantine_sha),
        ]
        assert connection.execute(
            "select ordinal, fragment_id from segment_compact_rows"
        ).fetchall() == [(0, fragment.fragment_id)]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda connection, fragment: connection.execute(
            "update segment_compact_rows set payload_blob = ?",
            [b"tampered"],
        ),
        lambda connection, fragment: connection.execute(
            "update segment_outcomes set source_sha256 = ?",
            [sha256(b"substituted").hexdigest()],
        ),
    ],
)
def test_segment_merge_rejects_tampered_or_substituted_facts(
    tmp_path: Path, mutator
) -> None:
    fragment = _fragment()
    source = tmp_path / "run-a-0000.segment.duckdb"
    target = tmp_path / "run-a-merged.segment.duckdb"
    _write_leaf(source, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])
    connection = duckdb.connect(str(source))
    try:
        mutator(connection, fragment)
    finally:
        connection.close()

    with pytest.raises(SourceV6StorageError, match="segment|digest|checksum|mismatch|tamper"):
        merge_source_v6_segments([source], target, run_token="run-a")


def test_segment_merge_rejects_run_substitution_gap_overlap_and_duplicate_ordinal(
    tmp_path: Path,
) -> None:
    fragment = _fragment()
    first = tmp_path / "run-a-0000.segment.duckdb"
    second = tmp_path / "run-a-0002.segment.duckdb"
    duplicate = tmp_path / "run-a-dup.segment.duckdb"
    _write_leaf(first, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])
    _write_leaf(second, [_accepted_outcome(2, fragment)], [(2, fragment, encode_fragment(fragment))])

    with pytest.raises(SourceV6StorageError, match="gap|ordinal"):
        merge_source_v6_segments([first, second], tmp_path / "gap.segment.duckdb", run_token="run-a")

    overlap = tmp_path / "run-a-overlap.segment.duckdb"
    _write_leaf(overlap, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])
    with pytest.raises(SourceV6StorageError, match="overlap|ordinal"):
        merge_source_v6_segments([first, overlap], tmp_path / "overlap.segment.duckdb", run_token="run-a")

    other_run = tmp_path / "run-b-0000.segment.duckdb"
    _write_leaf(other_run, [_accepted_outcome(1, fragment)], [(1, fragment, encode_fragment(fragment))], run_token="run-b")
    with pytest.raises(SourceV6StorageError, match="run token|namespace"):
        merge_source_v6_segments([first, other_run], tmp_path / "substitution.segment.duckdb", run_token="run-a")

    with pytest.raises(SourceV6StorageError, match="duplicate|ordinal"):
        _write_leaf(
            duplicate,
            [_accepted_outcome(0, fragment), _accepted_outcome(0, fragment)],
            [(0, fragment, encode_fragment(fragment))],
        )


def test_duplicate_fragment_winner_is_lowest_ordinal_then_path(tmp_path: Path) -> None:
    fragment = _fragment()
    first = tmp_path / "run-a-0000.segment.duckdb"
    second = tmp_path / "run-a-0001.segment.duckdb"
    _write_leaf(
        first,
        [_accepted_outcome(0, fragment, relative_path="z/report.html")],
        [(0, fragment, encode_fragment(fragment))],
    )
    _write_leaf(
        second,
        [_accepted_outcome(1, fragment, relative_path="a/report.html")],
        [(1, fragment, encode_fragment(fragment))],
    )

    merged = tmp_path / "run-a-merged.segment.duckdb"
    receipt = merge_source_v6_segments([first, second], merged, run_token="run-a")

    assert receipt.row_count == 1
    connection = duckdb.connect(str(merged), read_only=True)
    try:
        assert connection.execute(
            "select ordinal, relative_path, status, reason, winner_ordinal "
            "from segment_outcomes order by ordinal"
        ).fetchall() == [
            (0, "z/report.html", "PREPARED", None, None),
            (1, "a/report.html", "QUARANTINED", "duplicate_fragment", 0),
        ]
        assert connection.execute("select ordinal from segment_compact_rows").fetchone() == (0,)
    finally:
        connection.close()


def test_duplicate_fragment_is_quarantined_in_final_audit_without_duplicate_commit(
    tmp_path: Path,
) -> None:
    fragment = _fragment()
    first = tmp_path / "run-a-0000.segment.duckdb"
    second = tmp_path / "run-a-0001.segment.duckdb"
    _write_leaf(first, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])
    _write_leaf(second, [_accepted_outcome(1, fragment)], [(1, fragment, encode_fragment(fragment))])

    target = tmp_path / "source-v6.duckdb"
    reduce_source_v6_segments([first, second], target, run_token="run-a")

    connection = duckdb.connect(str(target), read_only=True)
    try:
        assert connection.execute("select count(*) from compact_fragments").fetchone() == (1,)
        assert connection.execute(
            "select status, error from import_audit order by status"
        ).fetchall() == [("COMMITTED", None), ("QUARANTINED", "duplicate_fragment")]
        assert connection.execute(
            "select reason from quarantine"
        ).fetchall() == [("duplicate_fragment",)]
    finally:
        connection.close()


def test_segment_fingerprint_is_required_and_exact(tmp_path: Path) -> None:
    fragment = _fragment()
    source = tmp_path / "run-a-0000.segment.duckdb"
    _write_leaf(source, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])

    connection = duckdb.connect(str(source))
    try:
        connection.execute(
            "update segment_manifest set fingerprint = 'wrong-fingerprint'"
        )
    finally:
        connection.close()
    with pytest.raises(SourceV6StorageError, match="fingerprint"):
        merge_source_v6_segments([source], tmp_path / "wrong.segment.duckdb")

    missing = tmp_path / "missing.segment.duckdb"
    _write_leaf(missing, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])
    connection = duckdb.connect(str(missing))
    try:
        connection.execute("delete from segment_manifest")
    finally:
        connection.close()
    with pytest.raises(SourceV6StorageError, match="manifest"):
        merge_source_v6_segments([missing], tmp_path / "missing-output.segment.duckdb")

    unsealed = tmp_path / "unsealed.segment.duckdb"
    _write_leaf(unsealed, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])
    connection = duckdb.connect(str(unsealed))
    try:
        connection.execute("update segment_manifest set sealed = false")
    finally:
        connection.close()
    with pytest.raises(SourceV6StorageError, match="seal"):
        merge_source_v6_segments([unsealed], tmp_path / "unsealed-output.segment.duckdb")


def test_fan_in_is_bounded_and_fan_eight_succeeds(tmp_path: Path) -> None:
    fragment = _fragment()
    source = tmp_path / "run-a-0000.segment.duckdb"
    _write_leaf(source, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])

    with pytest.raises(SourceV6StorageError, match="fan-in"):
        merge_source_v6_segments([source], tmp_path / "fan9.segment.duckdb", fan_in=9)
    assert merge_source_v6_segments(
        [source], tmp_path / "fan8.segment.duckdb", fan_in=8
    ).kind == "intermediate"


@pytest.mark.parametrize("fan_in", [0, 1, 9])
def test_merge_rejects_fan_in_outside_two_to_eight(tmp_path: Path, fan_in: int) -> None:
    fragment = _fragment()
    source = tmp_path / f"run-a-{fan_in}.segment.duckdb"
    _write_leaf(source, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])

    with pytest.raises(SourceV6StorageError, match="fan-in"):
        merge_source_v6_segments([source], tmp_path / f"merge-{fan_in}.duckdb", fan_in=fan_in)


@pytest.mark.parametrize("fan_in", [0, 1, 9])
def test_reduce_rejects_fan_in_outside_two_to_eight(tmp_path: Path, fan_in: int) -> None:
    fragment = _fragment()
    source = tmp_path / f"run-a-{fan_in}.segment.duckdb"
    _write_leaf(source, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])

    with pytest.raises(SourceV6StorageError, match="fan-in"):
        reduce_source_v6_segments([source], tmp_path / f"reduce-{fan_in}.duckdb", fan_in=fan_in)


@pytest.mark.parametrize("fan_in", [-5, None, "3", 2.5])
def test_fan_in_requires_an_integer_between_two_and_eight(
    tmp_path: Path, fan_in: object
) -> None:
    fragment = _fragment()
    source = tmp_path / "run-a-0000.segment.duckdb"
    _write_leaf(source, [_accepted_outcome(0, fragment)], [(0, fragment, encode_fragment(fragment))])

    with pytest.raises(SourceV6StorageError, match="fan-in"):
        merge_source_v6_segments([source], tmp_path / "invalid-merge.segment.duckdb", fan_in=fan_in)
    with pytest.raises(SourceV6StorageError, match="fan-in"):
        reduce_source_v6_segments([source], tmp_path / "invalid-reduce.duckdb", fan_in=fan_in)


def test_duplicate_first_wins_through_merge_and_final_reduce(tmp_path: Path) -> None:
    fragment = _fragment()
    first = tmp_path / "run-a-0000.segment.duckdb"
    later = tmp_path / "run-a-0001.segment.duckdb"
    _write_leaf(
        first,
        [_accepted_outcome(0, fragment, relative_path="00/first.html")],
        [(0, fragment, encode_fragment(fragment))],
    )
    _write_leaf(
        later,
        [_accepted_outcome(1, fragment, relative_path="01/later.html")],
        [(1, fragment, encode_fragment(fragment))],
    )

    merged = tmp_path / "merged.segment.duckdb"
    merge_source_v6_segments([first, later], merged, run_token="run-a", fan_in=2)
    connection = duckdb.connect(str(merged), read_only=True)
    try:
        assert connection.execute(
            "select ordinal, status, reason, winner_ordinal from segment_outcomes "
            "order by ordinal"
        ).fetchall() == [
            (0, "PREPARED", None, None),
            (1, "QUARANTINED", "duplicate_fragment", 0),
        ]
        assert connection.execute("select ordinal from segment_compact_rows").fetchall() == [(0,)]
    finally:
        connection.close()

    target = tmp_path / "source-v6.duckdb"
    reduce_source_v6_segments([first, later], target, run_token="run-a", fan_in=2)
    assert tuple(iter_fragments(target)) == (fragment,)


def test_mixed_fragment_and_source_sha_collisions_use_explicit_maps(tmp_path: Path) -> None:
    shared_sha = sha256(b"shared-source").hexdigest()
    first = replace(_unique_fragment(0), source_sha256=shared_sha)
    second = replace(_unique_fragment(1), source_sha256=shared_sha)
    third = replace(_unique_fragment(2), source_sha256=sha256(b"fragment-first").hexdigest())
    fourth = replace(third, source_sha256=sha256(b"fragment-later").hexdigest())
    segments = []
    for ordinal, fragment in enumerate((first, second, third, fourth)):
        path = tmp_path / f"run-a-{ordinal:04d}.segment.duckdb"
        _write_leaf(path, [_accepted_outcome(ordinal, fragment)], [(ordinal, fragment, encode_fragment(fragment))])
        segments.append(path)

    merged = tmp_path / "mixed-merged.segment.duckdb"
    merge_source_v6_segments(segments, merged, run_token="run-a", fan_in=4)
    connection = duckdb.connect(str(merged), read_only=True)
    try:
        assert connection.execute(
            "select ordinal, status, reason, winner_ordinal from segment_outcomes order by ordinal"
        ).fetchall() == [
            (0, "PREPARED", None, None),
            (1, "QUARANTINED", "duplicate_fragment", 0),
            (2, "PREPARED", None, None),
            (3, "QUARANTINED", "duplicate_fragment", 2),
        ]
    finally:
        connection.close()

    target = tmp_path / "mixed-source-v6.duckdb"
    reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=4)
    assert tuple(item.fragment_id for item in iter_fragments(target)) == tuple(
        sorted((first.fragment_id, third.fragment_id))
    )


def test_final_reduce_removes_target_after_partial_import_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segments = []
    for ordinal in range(3):
        fragment = _unique_fragment(ordinal)
        path = tmp_path / f"run-a-{ordinal:04d}.segment.duckdb"
        _write_leaf(path, [_accepted_outcome(ordinal, fragment)], [(ordinal, fragment, encode_fragment(fragment))])
        segments.append(path)

    original = storage._publish_segments_single_pass

    def partial_then_fail(target, segments, accepted, database_id):
        original(target, segments, accepted[:1], database_id)
        raise SourceV6StorageError("forced final import failure")

    monkeypatch.setattr(storage, "_publish_segments_single_pass", partial_then_fail)
    target = tmp_path / "partial-failure.duckdb"
    with pytest.raises(SourceV6StorageError, match="forced final import failure"):
        reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    assert not target.exists()
    assert not Path(f"{target}.wal").exists()
    assert not (tmp_path / ".partial-failure.duckdb.run-a.segments").exists()


def test_identical_inputs_have_deterministic_segment_digests(tmp_path: Path) -> None:
    fragment = _fragment()
    first = _write_leaf(
        tmp_path / "first.segment.duckdb",
        [_accepted_outcome(0, fragment)],
        [(0, fragment, encode_fragment(fragment))],
    )
    second = _write_leaf(
        tmp_path / "second.segment.duckdb",
        [_accepted_outcome(0, fragment)],
        [(0, fragment, encode_fragment(fragment))],
    )

    assert (first.outcome_digest, first.compact_digest) == (
        second.outcome_digest,
        second.compact_digest,
    )
    assert first.segment_id != second.segment_id


def test_unreadable_worker_failures_are_not_segment_quarantine(tmp_path: Path) -> None:
    failure = SourceV6SegmentOutcome(
        ordinal=0,
        relative_path="unreadable.html",
        status="READ_FAILED",
        reason="permission denied",
    )

    with pytest.raises(SourceV6StorageError, match="infrastructure|failure"):
        _write_leaf(tmp_path / "failed.segment.duckdb", [failure], [])


def test_reduce_cleanup_removes_intermediates_and_exception_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragments = [_unique_fragment(index) for index in range(3)]
    segments = []
    for index, fragment in enumerate(fragments):
        path = tmp_path / f"run-a-{index:04d}.segment.duckdb"
        _write_leaf(path, [_accepted_outcome(index, fragment)], [(index, fragment, encode_fragment(fragment))])
        segments.append(path)

    target = tmp_path / "source-v6.duckdb"
    reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    namespace = tmp_path / ".source-v6.duckdb.run-a.segments"
    assert not namespace.exists()
    assert not list(tmp_path.glob("*.wal"))

    target.unlink()
    reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    assert target.exists()
    assert not namespace.exists()

    failure_target = tmp_path / "failed-source-v6.duckdb"
    monkeypatch.setattr(
        storage,
        "_publish_segments_single_pass",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SourceV6StorageError("forced merge failure")
        ),
    )
    with pytest.raises(SourceV6StorageError, match="forced merge failure"):
        reduce_source_v6_segments(segments, failure_target, run_token="run-a", fan_in=2)
    assert not failure_target.exists()
    assert not (tmp_path / ".failed-source-v6.duckdb.run-a.segments").exists()


def test_cleanup_oserror_does_not_mask_original_or_skip_remaining_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segments = []
    for ordinal in range(3):
        fragment = _unique_fragment(ordinal)
        path = tmp_path / f"run-a-{ordinal:04d}.segment.duckdb"
        _write_leaf(path, [_accepted_outcome(ordinal, fragment)], [(ordinal, fragment, encode_fragment(fragment))])
        segments.append(path)

    original_import = storage._publish_segments_single_pass

    def fail_after_partial(target, segments, accepted, database_id):
        original_import(target, segments, accepted[:1], database_id)
        raise SourceV6StorageError("original final import failure")

    monkeypatch.setattr(storage, "_publish_segments_single_pass", fail_after_partial)
    original_unlink = Path.unlink
    unlink_calls: list[Path] = []
    blocked = "cleanup-failure.duckdb"

    def flaky_unlink(path: Path, *args, **kwargs):
        unlink_calls.append(path)
        if path.name == blocked:
            raise OSError("forced cleanup unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    target = tmp_path / "cleanup-failure.duckdb"
    # A failing cleanup must surface the original error, not the OSError.
    with pytest.raises(SourceV6StorageError, match="original final import failure"):
        reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)

    # Cleanup was attempted for the target and continued past the blocked unlink.
    assert any(path.name == blocked for path in unlink_calls)
    assert any(path.name == f"{blocked}.wal" for path in unlink_calls)


def test_reduce_assigns_parent_lifecycle_ids_and_preserves_quarantine(tmp_path: Path) -> None:
    fragment = _fragment()
    source = tmp_path / "run-a-0000.segment.duckdb"
    target = tmp_path / "source-v6.duckdb"
    quarantine_sha = sha256(b"stable parse failure").hexdigest()
    _write_leaf(
        source,
        [
            _accepted_outcome(0, fragment),
            SourceV6SegmentOutcome(
                ordinal=1,
                relative_path="broken.html",
                status="QUARANTINED",
                source_sha256=quarantine_sha,
                reason="invalid report",
            ),
        ],
        [(0, fragment, encode_fragment(fragment))],
    )

    receipt = reduce_source_v6_segments([source], target, run_token="run-a")

    assert receipt.database_id == database_info(target)["database_id"]
    assert receipt.database_id != receipt.segment_id
    connection = duckdb.connect(str(target), read_only=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'main'"
            ).fetchall()
        }
        assert not {"segment_manifest", "segment_outcomes", "segment_compact_rows"}.intersection(tables)
        columns = {
            row[0]
            for row in connection.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'main'"
            ).fetchall()
        }
        assert not {"segment_id", "run_token", "sealed", "checkpointed"}.intersection(columns)
        assert connection.execute("select distinct origin_database_id from fragment_origins").fetchall() == [
            (receipt.database_id,)
        ]
        assert connection.execute(
            "select source_sha256, reason from quarantine"
        ).fetchall() == [(quarantine_sha, "invalid report")]
        assert connection.execute(
            "select generation_before, generation_after from import_audit where status = 'COMMITTED'"
        ).fetchall() == [(0, 1)]
    finally:
        connection.close()


@pytest.mark.parametrize("fan_in", [2, 4, 8])
def test_hierarchical_fan_in_is_semantically_equivalent(tmp_path: Path, fan_in: int) -> None:
    segments = []
    fragments = []
    for index in range(8):
        fragment = _unique_fragment(index)
        fragments.append(fragment)
        segment = tmp_path / f"run-a-{index:04d}.segment.duckdb"
        _write_leaf(
            segment,
            [_accepted_outcome(index, fragment)],
            [(index, fragment, encode_fragment(fragment))],
        )
        segments.append(segment)

    target = tmp_path / f"source-v6-{fan_in}.duckdb"
    receipt = reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=fan_in)

    assert receipt.row_count == len(fragments)
    assert tuple(item.fragment_id for item in iter_fragments(target)) == tuple(
        sorted(item.fragment_id for item in fragments)
    )
    assert database_info(target)["source_content_digest"] == storage.source_content_digest(
        item.fragment_id for item in fragments
    )


def _leaf_segments(tmp_path: Path, count: int) -> tuple[list[Path], list]:
    segments: list[Path] = []
    fragments = []
    for index in range(count):
        fragment = _unique_fragment(index)
        fragments.append(fragment)
        segment = tmp_path / f"run-a-{index:04d}.segment.duckdb"
        _write_leaf(
            segment,
            [_accepted_outcome(index, fragment)],
            [(index, fragment, encode_fragment(fragment))],
        )
        segments.append(segment)
    return segments, fragments


PUBLICATION_TABLES = (
    "schema_info",
    "compact_fragments",
    "points",
    "fragment_origins",
    "day_ownership",
    "import_audit",
    "quarantine",
)


def _dump_publication(path: Path) -> dict[str, list[tuple]]:
    """Dump every published table with non-deterministic fields normalised."""
    connection = duckdb.connect(str(path), read_only=True)
    try:
        dump: dict[str, list[tuple]] = {}
        for table in PUBLICATION_TABLES:
            rows = connection.execute(f"select * from {table}").fetchall()
            columns = [
                item[0]
                for item in connection.execute(
                    "select column_name from information_schema.columns "
                    "where table_schema='main' and table_name=? order by ordinal_position",
                    [table],
                ).fetchall()
            ]
            normalised = []
            for row in rows:
                item = []
                for name, value in zip(columns, row):
                    if name in {"audit_id", "database_id", "origin_database_id"}:
                        item.append("<id>")
                    elif name.endswith("_at_utc"):
                        item.append("<ts>" if value is not None else None)
                    elif name == "key" and value == "database_id":
                        item.append(value)
                    elif name == "value" and len(row) == 2 and row[0] == "database_id":
                        item.append("<id>")
                    else:
                        item.append(value)
                normalised.append(tuple(item))
            dump[table] = sorted(normalised, key=repr)
        return dump
    finally:
        connection.close()


def test_publication_layout_is_stable_across_implementations(tmp_path: Path) -> None:
    """The published database must stay byte-comparable for materialization."""
    fragments = [_unique_fragment(index) for index in range(6)]
    database = tmp_path / "published.duckdb"
    create_v6_database(database, database_id="db-fixed")
    storage.import_fragment_batch(
        database,
        [(fragment, encode_fragment(fragment)) for fragment in fragments],
    )

    dump = _dump_publication(database)

    # Generation advances once per committed fragment and audit rows record it.
    assert dict(dump["schema_info"])["mutation_generation"] == str(len(fragments))
    audit = dump["import_audit"]
    assert len(audit) == len(fragments)
    assert {row[5] for row in audit} == {"COMMITTED"}
    assert sorted((row[6], row[7]) for row in audit) == [
        (index, index + 1) for index in range(len(fragments))
    ]
    assert {row[8] for row in audit} == {"YES"}
    assert len(dump["compact_fragments"]) == len(fragments)
    assert len(dump["fragment_origins"]) == len(fragments)
    assert dump["quarantine"] == []
    # Every calendar day of every report period is owned exactly once.
    expected_days = 0
    for fragment in fragments:
        start = datetime.fromtimestamp(fragment.report_start_ms / 1000, timezone.utc).date()
        end = datetime.fromtimestamp(fragment.report_end_ms / 1000, timezone.utc).date()
        expected_days += (end - start).days
    assert len(dump["day_ownership"]) == expected_days
    assert {row[2] for row in dump["day_ownership"]} == {"ACTIVE"}
    assert database_info(database)["source_content_digest"] == storage.source_content_digest(
        fragment.fragment_id for fragment in fragments
    )


def test_merge_passes_sealed_payloads_through_without_decoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Intermediate merges must not decode and re-encode already sealed payloads."""
    segments, fragments = _leaf_segments(tmp_path, 4)
    expected = tmp_path / "expected.segment.duckdb"
    merge_source_v6_segments(segments, expected, run_token="run-a")
    _, _, expected_rows = storage._read_source_v6_segment(expected)

    decodes: list[str] = []
    original_decode = storage.decode_fragment

    def counting_decode(*args: object, **kwargs: object) -> object:
        decodes.append("decode")
        return original_decode(*args, **kwargs)

    monkeypatch.setattr(storage, "decode_fragment", counting_decode)

    merged = tmp_path / "merged.segment.duckdb"
    merge_source_v6_segments(segments, merged, run_token="run-a")
    _, _, merged_rows = storage._read_source_v6_segment(merged)

    assert merged_rows == expected_rows
    assert decodes == []


def test_parallel_decode_matches_serial_decode(tmp_path: Path) -> None:
    """Parallel decoding must return exactly what the serial reader returns."""
    segments, _fragments = _leaf_segments(tmp_path, 8)
    target = tmp_path / "source-v6.duckdb"
    reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)

    serial = tuple(iter_fragments(target))
    parallel = storage.iter_fragments_parallel(target, workers=4, chunk_size=2)

    assert parallel == serial
    assert storage.iter_fragments_parallel(target, workers=1) == serial


def test_point_identity_round_trips_through_its_canonical_key() -> None:
    from mrs3.source_v6 import PointIdentity

    for fragment in (_fragment(), _fragment_b()):
        assert PointIdentity.from_canonical_key(fragment.point.canonical_key) == fragment.point


@pytest.mark.parametrize("shift", ["+30", " 30 ", "3_0", "-0", "030", "007"])
def test_point_identity_rejects_non_invertible_integer_text(shift: str) -> None:
    """A key that would re-serialise differently is not a valid key."""
    from mrs3.source_v6 import PointIdentity, SourceV6Error

    key = f"BTCUSDT|LONG|1h|{shift}|sma|close|2|sma|close|3"
    with pytest.raises(SourceV6Error, match="invalid canonical point key"):
        PointIdentity.from_canonical_key(key)


def test_fragment_metadata_matches_decoded_fragments_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata reads must agree with a full decode and never touch a payload."""
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    first = _fragment()
    second = _fragment_b()
    import_fragment(database, first, preflight_token=preflight_import(database, first))
    import_fragment(database, second, preflight_token=preflight_import(database, second))
    decoded = tuple(iter_fragments(database))

    decodes: list[str] = []
    original = storage.decode_fragment

    def counting(*args: object, **kwargs: object) -> object:
        decodes.append("decode")
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "decode_fragment", counting)
    metadata = storage.fragment_metadata(database)
    monkeypatch.undo()

    assert decodes == []
    assert len(metadata) == len(decoded)
    for view, fragment in zip(metadata, decoded):
        assert view.fragment_id == fragment.fragment_id
        assert view.source_sha256 == fragment.source_sha256
        assert view.source_name == fragment.source_name
        assert view.point == fragment.point
        assert view.report_start_ms == fragment.report_start_ms
        assert view.report_end_ms == fragment.report_end_ms
        assert view.stitchability == fragment.stitchability
        assert view.settings_fingerprint == fragment.settings_fingerprint
        assert view.initial_balance == fragment.initial_balance
        assert view.fixed_order_balance == fragment.fixed_order_balance
        assert view.balance_percentage == fragment.balance_percentage
        assert view.open_tail_cycle_ids == fragment.open_tail_cycle_ids
        assert dict(view.metrics) == dict(fragment.metrics)


def test_metadata_supports_coverage_and_stitching_unchanged(tmp_path: Path) -> None:
    """Coverage, readiness and stitching must accept metadata views verbatim."""
    from mrs3.source_v6_coverage import canonical_ready_intervals, coverage_cells
    from mrs3.source_v6_stitch import resolve_batch

    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    first = _fragment()
    second = _fragment_b()
    import_fragment(database, first, preflight_token=preflight_import(database, first))
    import_fragment(database, second, preflight_token=preflight_import(database, second))

    decoded = tuple(iter_fragments(database))
    metadata = storage.fragment_metadata(database)

    assert coverage_cells(metadata) == coverage_cells(decoded)
    assert canonical_ready_intervals(metadata) == canonical_ready_intervals(decoded)
    assert tuple(item.fragment_id for item in resolve_batch(metadata).active_fragments) == tuple(
        item.fragment_id for item in resolve_batch(decoded).active_fragments
    )


def test_decode_fragment_slice_rejects_an_incomplete_slice(tmp_path: Path) -> None:
    segments, _fragments = _leaf_segments(tmp_path, 3)
    target = tmp_path / "source-v6.duckdb"
    reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    ids = storage.fragment_ids(target)

    with pytest.raises(SourceV6StorageError, match="incomplete"):
        storage.decode_fragment_slice(target, (*ids, "missing-fragment-id"))


def test_reduce_bounds_simultaneous_segment_attachments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Attachments must stay bounded; the OS file-descriptor limit is finite."""
    segments, fragments = _leaf_segments(tmp_path, 12)

    reference = tmp_path / "reference.duckdb"
    reduce_source_v6_segments(segments, reference, run_token="run-a", fan_in=2, database_id="db-x")
    expected = _dump_publication(reference)

    monkeypatch.setattr(storage, "SEGMENT_ATTACH_BATCH", 3)
    peak = 0
    live = 0
    original_connect = duckdb.connect

    class _Tracking:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            nonlocal peak, live
            lowered = str(sql).lstrip().lower()
            if lowered.startswith("attach"):
                live += 1
                peak = max(peak, live)
            elif lowered.startswith("detach"):
                live -= 1
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(storage.duckdb, "connect", lambda *a, **k: _Tracking(original_connect(*a, **k)))

    target = tmp_path / "source-v6.duckdb"
    reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2, database_id="db-x")
    monkeypatch.undo()

    assert 0 < peak <= 3
    assert _dump_publication(target) == expected


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("report_start_ms", "1767139200000"),
        ("report_end_ms", "1767139200000"),
        ("stitchability", "'NON_STITCHABLE_POSITION_SIZING'"),
        ("source_name", "'swapped.html'"),
        ("point_key", "'X|LONG|1h|30|sma|close|2|sma|close|3'"),
    ],
)
def test_reduce_rejects_a_tampered_segment_column(tmp_path: Path, column: str, value: str) -> None:
    """Publication must bind every stored column to the fragment identity."""
    segments, _fragments = _leaf_segments(tmp_path, 3)
    connection = duckdb.connect(str(segments[1]))
    try:
        connection.execute(f"update segment_compact_rows set {column} = {value}")
    finally:
        connection.close()

    target = tmp_path / "tampered.duckdb"
    with pytest.raises(SourceV6StorageError):
        reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    assert not target.exists()


def _reseal_segment_manifest(connection: duckdb.DuckDBPyConnection) -> None:
    """Recompute the sealed digest so the segment is internally consistent.

    Without this a forgery is caught by the manifest rather than by
    publication, which hides whether publication itself binds the field.
    """
    resealed = connection.execute(
        "select ordinal, fragment_id, source_sha256, source_name, point_key, "
        "report_start_ms, report_end_ms, stitchability, header_json, header_sha256, "
        "payload_blob, codec, payload_sha256, action_count, cycle_count, event_count, "
        "wallet_sample_count, equity_sample_count from segment_compact_rows order by ordinal"
    ).fetchall()
    digest = storage._segment_compact_digest([(int(item[0]), tuple(item[1:])) for item in resealed])
    connection.execute("update segment_manifest set compact_digest = ?", [digest])


def _forge_header(segment: Path, column: str | None, value: object, header_key: str, header_value: object) -> None:
    """Edit a column and its header together, recomputing header_sha256."""
    connection = duckdb.connect(str(segment))
    try:
        rows = connection.execute(
            "select ordinal, header_json from segment_compact_rows order by ordinal"
        ).fetchall()
        for ordinal, header_json in rows:
            header = json.loads(str(header_json))
            header[header_key] = header_value
            forged = json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "update segment_compact_rows set header_json = ?, header_sha256 = ? where ordinal = ?",
                [forged, sha256(forged.encode("utf-8")).hexdigest(), ordinal],
            )
            if column is not None:
                connection.execute(
                    f"update segment_compact_rows set {column} = ? where ordinal = ?", [value, ordinal]
                )
        _reseal_segment_manifest(connection)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("column", "value", "header_key", "header_value"),
    [
        ("point_key", "EVIL|LONG|1h|30|sma|close|2|sma|close|3", "point", "EVIL|LONG|1h|30|sma|close|2|sma|close|3"),
        ("report_start_ms", 1, "report_start_ms", 1),
        ("report_end_ms", 2, "report_end_ms", 2),
        (None, None, "settings_fingerprint", "forged"),
        (None, None, "initial_balance", "1"),
        (None, None, "fixed_order_balance", "1"),
        (None, None, "balance_percentage", "1"),
        (None, None, "metrics", {"forged": "1"}),
        (None, None, "open_tail_cycle_ids", ["forged"]),
        (None, None, "schema_version", 99),
        ("stitchability", "NON_STITCHABLE_POSITION_SIZING", "stitchability", "NON_STITCHABLE_POSITION_SIZING"),
    ],
)
def test_reduce_rejects_a_consistently_forged_header(
    tmp_path: Path, column: str | None, value: object, header_key: str, header_value: object
) -> None:
    """Editing a column and its header together must not slip past publication."""
    segments, _fragments = _leaf_segments(tmp_path, 3)
    _forge_header(segments[1], column, value, header_key, header_value)

    target = tmp_path / "forged.duckdb"
    with pytest.raises(SourceV6StorageError):
        reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    assert not target.exists()


@pytest.mark.parametrize(
    "column",
    ["action_count", "cycle_count", "event_count", "wallet_sample_count", "equity_sample_count"],
)
def test_reduce_rejects_a_tampered_count_column(tmp_path: Path, column: str) -> None:
    """A zeroed count publishes an undecodable corpus unless it fails closed."""
    segments, _fragments = _leaf_segments(tmp_path, 3)
    connection = duckdb.connect(str(segments[1]))
    try:
        connection.execute(f"update segment_compact_rows set {column} = 0")
    finally:
        connection.close()

    target = tmp_path / "counts.duckdb"
    with pytest.raises(SourceV6StorageError):
        reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    assert not target.exists()


def test_reduce_rejects_a_forgery_that_reseals_the_segment_manifest(tmp_path: Path) -> None:
    """Publication must not trust the segment manifest alone."""
    segments, _fragments = _leaf_segments(tmp_path, 3)
    connection = duckdb.connect(str(segments[1]))
    try:
        rows = connection.execute(
            "select ordinal, header_json from segment_compact_rows order by ordinal"
        ).fetchall()
        for ordinal, header_json in rows:
            header = json.loads(str(header_json))
            header["point"] = "FORGED|LONG|1h|30|sma|close|2|sma|close|3"
            header["report_start_ms"] = 1
            forged = json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "update segment_compact_rows set header_json = ?, header_sha256 = ?, "
                "point_key = ?, report_start_ms = ?, action_count = 0 where ordinal = ?",
                [
                    forged,
                    sha256(forged.encode("utf-8")).hexdigest(),
                    "FORGED|LONG|1h|30|sma|close|2|sma|close|3",
                    1,
                    ordinal,
                ],
            )
        # Reseal the manifest so the segment is internally consistent again.
        resealed = connection.execute(
            "select ordinal, fragment_id, source_sha256, source_name, point_key, "
            "report_start_ms, report_end_ms, stitchability, header_json, header_sha256, "
            "payload_blob, codec, payload_sha256, action_count, cycle_count, event_count, "
            "wallet_sample_count, equity_sample_count from segment_compact_rows order by ordinal"
        ).fetchall()
        digest = storage._segment_compact_digest([(int(item[0]), tuple(item[1:])) for item in resealed])
        connection.execute("update segment_manifest set compact_digest = ?", [digest])
    finally:
        connection.close()

    target = tmp_path / "resealed.duckdb"
    with pytest.raises(SourceV6StorageError, match="readback mismatch"):
        reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    assert not target.exists()


def test_reduce_rejects_prepared_rows_mis_zipped_against_their_outcomes(tmp_path: Path) -> None:
    """Rows and outcomes are sealed separately; their pairing must be checked.

    Swapping two whole compact rows leaves every row self-consistent and every
    digest satisfiable, so only an explicit ordinal correspondence check
    notices that each payload now publishes under the other's outcome.
    """
    first_fragment, second_fragment = _unique_fragment(0), _unique_fragment(1)
    segment = tmp_path / "run-a-0000.segment.duckdb"
    _write_leaf(
        segment,
        [_accepted_outcome(0, first_fragment), _accepted_outcome(1, second_fragment)],
        [
            (0, first_fragment, encode_fragment(first_fragment)),
            (1, second_fragment, encode_fragment(second_fragment)),
        ],
    )
    segments = [segment]
    connection = duckdb.connect(str(segment))
    try:
        rows = connection.execute(
            "select ordinal, fragment_id, source_sha256, source_name, point_key, "
            "report_start_ms, report_end_ms, stitchability, header_json, header_sha256, "
            "payload_blob, codec, payload_sha256, action_count, cycle_count, event_count, "
            "wallet_sample_count, equity_sample_count from segment_compact_rows order by ordinal"
        ).fetchall()
        assert len(rows) == 2
        first, second = rows[0], rows[1]
        assignment = (
            "fragment_id = ?, source_sha256 = ?, source_name = ?, point_key = ?, "
            "report_start_ms = ?, report_end_ms = ?, stitchability = ?, header_json = ?, "
            "header_sha256 = ?, payload_blob = ?, codec = ?, payload_sha256 = ?, "
            "action_count = ?, cycle_count = ?, event_count = ?, wallet_sample_count = ?, "
            "equity_sample_count = ?"
        )
        for target_ordinal, donor in ((first[0], second), (second[0], first)):
            connection.execute(
                f"update segment_compact_rows set {assignment} where ordinal = ?",
                [*donor[1:], target_ordinal],
            )
        _reseal_segment_manifest(connection)
    finally:
        connection.close()

    target = tmp_path / "miszipped.duckdb"
    with pytest.raises(SourceV6StorageError):
        reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    assert not target.exists()


def test_reduce_rejects_a_substituted_payload(tmp_path: Path) -> None:
    """A payload swapped together with its own checksum must still fail closed."""
    segments, fragments = _leaf_segments(tmp_path, 3)
    other = encode_fragment(fragments[2])
    connection = duckdb.connect(str(segments[1]))
    try:
        connection.execute(
            "update segment_compact_rows set payload_blob = ?, payload_sha256 = ?",
            [other.payload, sha256(other.payload).hexdigest()],
        )
    finally:
        connection.close()

    target = tmp_path / "swapped.duckdb"
    with pytest.raises(SourceV6StorageError):
        reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)
    assert not target.exists()


def test_reduce_writes_no_intermediate_artifacts(tmp_path: Path) -> None:
    """One-pass reduce must not materialise the corpus outside the target."""
    segments, fragments = _leaf_segments(tmp_path, 8)
    before = {path.name for path in tmp_path.iterdir()}

    target = tmp_path / "source-v6.duckdb"
    reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)

    assert {path.name for path in tmp_path.iterdir()} - before == {"source-v6.duckdb"}
    assert tuple(item.fragment_id for item in iter_fragments(target)) == tuple(
        sorted(item.fragment_id for item in fragments)
    )


def test_reduce_is_independent_of_declared_fan_in(tmp_path: Path) -> None:
    segments, _fragments = _leaf_segments(tmp_path, 8)

    narrow = tmp_path / "narrow.duckdb"
    reduce_source_v6_segments(segments, narrow, run_token="run-a", fan_in=2, database_id="db-x")
    wide = tmp_path / "wide.duckdb"
    reduce_source_v6_segments(segments, wide, run_token="run-a", fan_in=8, database_id="db-x")

    assert _dump_publication(narrow) == _dump_publication(wide)


def test_reduce_publishes_without_decoding_any_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publication must reuse sealed payloads instead of decoding every fragment."""
    segments, fragments = _leaf_segments(tmp_path, 4)
    reference = tmp_path / "reference.duckdb"
    reduce_source_v6_segments(segments, reference, run_token="run-a", fan_in=2)
    expected = _dump_publication(reference)

    decodes: list[str] = []
    original_decode = storage.decode_fragment

    def counting_decode(*args: object, **kwargs: object) -> object:
        decodes.append("decode")
        return original_decode(*args, **kwargs)

    monkeypatch.setattr(storage, "decode_fragment", counting_decode)

    target = tmp_path / "source-v6.duckdb"
    reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2)

    assert decodes == []
    assert _dump_publication(target) == expected
    assert database_info(target)["source_content_digest"] == storage.source_content_digest(
        item.fragment_id for item in fragments
    )


def test_reduce_publishes_in_one_pass_without_a_merge_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reduce must copy sealed rows straight into the target, not rebuild them per level."""
    segments, fragments = _leaf_segments(tmp_path, 8)

    expected_db = tmp_path / "expected.duckdb"
    create_v6_database(expected_db, database_id="db-expected")
    storage.import_fragment_batch(
        expected_db,
        [(fragment, encode_fragment(fragment)) for fragment in sorted(fragments, key=lambda item: item.source_name)],
    )
    expected = _dump_publication(expected_db)

    merges: list[str] = []
    original_merge = storage.merge_source_v6_segments

    def counting_merge(*args: object, **kwargs: object) -> object:
        merges.append("merge")
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(storage, "merge_source_v6_segments", counting_merge)

    target = tmp_path / "source-v6.duckdb"
    reduce_source_v6_segments(segments, target, run_token="run-a", fan_in=2, database_id="db-expected")

    assert merges == []
    assert _dump_publication(target) == expected


def test_fresh_v6_schema_has_identity_and_generation(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    dbid = create_v6_database(database, database_id="db-test")

    assert dbid == "db-test"
    assert database_info(database) == {
        "schema_version": "6",
        "fingerprint": "source-v6-fresh-compact-v1",
        "database_id": "db-test",
        "mutation_generation": "0",
        "source_content_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    }


def test_v6_rejects_existing_v5_target(tmp_path: Path) -> None:
    database = tmp_path / "old.duckdb"
    connection = duckdb.connect(str(database))
    connection.execute("create table schema_info(key varchar, value varchar)")
    connection.execute("insert into schema_info values ('schema_version', '5')")
    connection.close()

    with pytest.raises(SourceV6StorageError, match="fresh Source v6"):
        create_v6_database(database)


def test_import_is_transactional_idempotent_and_reopenable(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    fragment = _fragment()
    token = preflight_import(database, fragment)

    first = import_fragment(database, fragment, preflight_token=token)
    second = import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))

    assert first.status == "COMMITTED"
    assert first.safe_to_delete == "YES"
    assert second.status == "IDEMPOTENT"
    assert second.generation == first.generation
    assert database_info(database)["mutation_generation"] == "1"
    facts = read_fragment(database, fragment.fragment_id)
    assert len(facts["actions"]) == len(fragment.actions)
    assert len(facts["cycles"]) == len(fragment.cycles)
    assert len(facts["events"]) == len(fragment.events)
    assert facts["fragments"][0][1] == fragment.source_sha256
    assert [(row[2], Decimal(str(row[3]))) for row in facts["samples"] if row[1] == "wallet"] == [(sample.timestamp_ms, sample.value) for sample in fragment.wallet_samples]
    connection = duckdb.connect(str(database), read_only=True)
    try:
        tables = {row[0] for row in connection.execute("select table_name from information_schema.tables where table_schema = 'main'").fetchall()}
        assert "samples_zlib" not in tables
    finally:
        connection.close()


def test_compaction_rewrites_lossless_source_without_free_fragment_payloads(tmp_path: Path) -> None:
    source = tmp_path / "source-v6.staging"
    target = tmp_path / "source-v6.duckdb"
    fragment = _fragment()
    create_v6_database(source, database_id="compact-db")
    import_fragment(source, fragment, preflight_token=preflight_import(source, fragment))

    compact_v6_database(source, target)

    assert database_info(target)["database_id"] == "compact-db"
    assert tuple(iter_fragments(target)) == (fragment,)
    connection = duckdb.connect(str(target), read_only=True)
    try:
        tables = {row[0] for row in connection.execute("select table_name from information_schema.tables where table_schema = 'main'").fetchall()}
        assert "samples_zlib" not in tables
    finally:
        connection.close()


def test_compaction_refuses_a_table_signature_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source-v6.staging"
    target = tmp_path / "source-v6.duckdb"
    create_v6_database(source)
    import_fragment(source, _fragment(), preflight_token=preflight_import(source, _fragment()))
    original = storage._table_counts
    calls = 0

    def mismatched(path: Path) -> tuple[tuple[str, int], ...]:
        nonlocal calls
        calls += 1
        counts = original(path)
        return counts if calls == 1 else counts[:-1]

    monkeypatch.setattr(storage, "_table_counts", mismatched)
    with pytest.raises(SourceV6StorageError, match="metadata mismatch"):
        compact_v6_database(source, target)
    assert not target.exists()


def test_successful_import_reconstructs_complete_fragment_without_html(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    fragment = _fragment()
    import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))
    reconstructed = reconstruct_fragment(database, fragment.fragment_id)
    assert reconstructed == fragment


def test_overlap_pair_can_share_one_point_config(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    first = _fragment()
    second = _fragment_b()
    import_fragment(database, first, preflight_token=preflight_import(database, first))
    receipt = import_fragment(database, second, preflight_token=preflight_import(database, second))
    assert receipt.status == "COMMITTED"
    connection = duckdb.connect(str(database), read_only=True)
    try:
        assert connection.execute("select count(*) from points").fetchone()[0] == 1
        assert connection.execute("select count(*) from fragments").fetchone()[0] == 2
    finally:
        connection.close()


def test_resolved_fragment_marks_outgoing_facts_inactive(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    first = _fragment()
    second = _fragment_b()
    import_fragment(database, first, preflight_token=preflight_import(database, first))
    import_fragment(database, second, preflight_token=preflight_import(database, second))
    apply_fragment_resolution(database, outgoing_fragment_id=first.fragment_id, incoming_fragment_id=second.fragment_id, status="RESOLVED")
    connection = duckdb.connect(str(database), read_only=True)
    try:
        assert connection.execute("select active from fragments where fragment_id = ?", [first.fragment_id]).fetchone()[0] is False
        assert connection.execute("select winner_fragment_id from fragments where fragment_id = ?", [first.fragment_id]).fetchone()[0] == second.fragment_id
    finally:
        connection.close()


def _resolution_inputs(first, second) -> tuple[dict, dict]:
    return (
        {
            "outgoing_fragment_id": first.fragment_id,
            "incoming_fragment_id": second.fragment_id,
            "status": "USE_OLD_WITH_SEAM_EXCLUSION",
            "reason": "INCOMPLETE_SEAM_CYCLE_EXCLUDED",
            "fact_rows": [("action", "a-1", first.fragment_id, first.fragment_id, True, None, None)],
            "boundary_ms": first.report_end_ms,
            "evidence_json": '{"boundary_ms":1}',
        },
        {
            "outgoing_fragment_id": second.fragment_id,
            "incoming_fragment_id": first.fragment_id,
            "status": "UNRESOLVED",
            "reason": "BRIDGE_NOT_COVERED",
            "fact_rows": [("cycle", "c-1", second.fragment_id, second.fragment_id, False, "X", None)],
            "boundary_ms": None,
            "evidence_json": None,
        },
    )


def _prepared_pair(tmp_path: Path, name: str):
    database = tmp_path / name
    create_v6_database(database, database_id="db-fixed")
    first = _fragment()
    second = _fragment_b()
    import_fragment(database, first, preflight_token=preflight_import(database, first))
    import_fragment(database, second, preflight_token=preflight_import(database, second))
    return database, first, second


def test_bulk_resolution_matches_sequential_writes_through_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many stitch decisions must share one connection and one transaction."""
    sequential_db, first, second = _prepared_pair(tmp_path, "sequential.duckdb")
    one, two = _resolution_inputs(first, second)
    storage.persist_fragment_resolution(sequential_db, **one)
    storage.persist_fragment_resolution(sequential_db, **two)
    expected = _dump_publication(sequential_db)

    bulk_db, first, second = _prepared_pair(tmp_path, "bulk.duckdb")
    one, two = _resolution_inputs(first, second)
    opens: list[str] = []
    original_connect = duckdb.connect

    def counting_connect(target, *args, **kwargs):
        opens.append(str(target))
        return original_connect(target, *args, **kwargs)

    monkeypatch.setattr(storage.duckdb, "connect", counting_connect)
    storage.persist_fragment_resolutions(bulk_db, (one, two))
    monkeypatch.undo()

    assert [item for item in opens if item == str(bulk_db)] == [str(bulk_db)]
    assert _dump_publication(bulk_db) == expected
    for table in ("fragment_resolutions", "fact_ownership"):
        connection = duckdb.connect(str(bulk_db), read_only=True)
        try:
            got = sorted(connection.execute(f"select * from {table}").fetchall(), key=repr)
        finally:
            connection.close()
        connection = duckdb.connect(str(sequential_db), read_only=True)
        try:
            want = sorted(connection.execute(f"select * from {table}").fetchall(), key=repr)
        finally:
            connection.close()
        assert got == want


def test_persist_resolution_records_exact_fact_owners_and_bridge_membership(tmp_path: Path) -> None:
    from mrs3.source_v6_stitch import persist_resolution

    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    first = _fragment()
    second = _fragment_b()
    import_fragment(database, first, preflight_token=preflight_import(database, first))
    import_fragment(database, second, preflight_token=preflight_import(database, second))
    bridge = persist_resolution(str(database), first, second, status="RESOLVED")
    assert bridge is not None and bridge.cycle_ids and bridge.sample_timestamps
    connection = duckdb.connect(str(database), read_only=True)
    try:
        assert connection.execute("select count(*) from fact_ownership where active", []).fetchone()[0] > 0
        assert connection.execute("select active from fragments where fragment_id = ?", [first.fragment_id]).fetchone()[0] is False
    finally:
        connection.close()


def test_unresolved_resolution_persists_inactive_reason_and_winner(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    first = _fragment()
    second = _fragment_b()
    import_fragment(database, first, preflight_token=preflight_import(database, first))
    import_fragment(database, second, preflight_token=preflight_import(database, second))
    apply_fragment_resolution(database, outgoing_fragment_id=first.fragment_id, incoming_fragment_id=second.fragment_id, status="UNRESOLVED", reason="BRIDGE_NOT_COVERED")
    connection = duckdb.connect(str(database), read_only=True)
    try:
        assert connection.execute("select active, inactive_reason, winner_fragment_id from fragments where fragment_id = ?", [second.fragment_id]).fetchone() == (False, "BRIDGE_NOT_COVERED", first.fragment_id)
    finally:
        connection.close()


def test_seam_exclusion_keeps_both_fragments_active_and_persists_diagnostic(tmp_path: Path) -> None:
    import json
    from mrs3.source_v6_stitch import persist_resolution

    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    first = _fragment()
    second = _fragment_b()
    import_fragment(database, first, preflight_token=preflight_import(database, first))
    import_fragment(database, second, preflight_token=preflight_import(database, second))

    persist_resolution(database, first, second, status="USE_OLD_WITH_SEAM_EXCLUSION", reason="INCOMPLETE_SEAM_CYCLE_EXCLUDED")

    connection = duckdb.connect(str(database), read_only=True)
    try:
        assert connection.execute("select active from compact_fragments where fragment_id in (?, ?) order by fragment_id", [first.fragment_id, second.fragment_id]).fetchall() == [(True,), (True,)]
        status, reason, boundary_ms, evidence = connection.execute("select status, reason, boundary_ms, evidence_json from fragment_resolutions").fetchone()
        assert (status, reason, boundary_ms) == ("USE_OLD_WITH_SEAM_EXCLUSION", "INCOMPLETE_SEAM_CYCLE_EXCLUDED", first.report_end_ms)
        assert json.loads(evidence)["excluded_cycle_ids"]
        assert connection.execute("select count(*) from fact_ownership where active = false and reason = 'INCOMPLETE_SEAM_CYCLE_EXCLUDED'").fetchone()[0] > 0
    finally:
        connection.close()


def test_failed_import_rolls_back_and_does_not_advance_generation(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    fragment = _fragment()

    with pytest.raises(SourceV6StorageError, match="forced import failure"):
        import_fragment(database, fragment, preflight_token=preflight_import(database, fragment), fail_after="facts")

    assert database_info(database)["mutation_generation"] == "0"
    connection = duckdb.connect(str(database), read_only=True)
    try:
        assert connection.execute("select count(*) from fragments").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_audit").fetchone()[0] == 0
    finally:
        connection.close()


def test_cancelled_import_rolls_back_and_keeps_html_unsafe(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    fragment = _fragment()
    with pytest.raises(SourceV6StorageError, match="cancelled"):
        import_fragment(database, fragment, preflight_token=preflight_import(database, fragment), cancel_check=lambda: True)
    assert database_info(database)["mutation_generation"] == "0"
    connection = duckdb.connect(str(database), read_only=True)
    try:
        assert connection.execute("select count(*) from fragments").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_audit").fetchone()[0] == 0
    finally:
        connection.close()


def test_stale_preflight_token_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    fragment = _fragment()

    with pytest.raises(SourceV6StorageError, match="stale preflight"):
        import_fragment(database, fragment, preflight_token="wrong")


def test_import_requires_preflight_token(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    with pytest.raises(SourceV6StorageError, match="preflight token required"):
        import_fragment(database, _fragment())


def test_bridge_not_covered_is_not_a_manual_disposition(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    fragment = _fragment()
    with pytest.raises(SourceV6StorageError, match="automatic"):
        set_day_disposition(database, fragment.fragment_id, "2026-01-08", "BRIDGE_NOT_COVERED")
    set_day_disposition(database, fragment.fragment_id, "2026-01-08", "EXCLUDE_DAY_AS_GAP")


def test_compact_codec_identity_is_before_compression_and_readback_is_lossless() -> None:
    from mrs3.source_v6 import decode_fragment, encode_fragment

    fragment = _fragment()
    low = encode_fragment(fragment, compression_level=1)
    high = encode_fragment(fragment, compression_level=9)

    assert low.fragment_id == high.fragment_id == fragment.fragment_id
    assert decode_fragment(low.payload, codec=low.codec).fragment_id == fragment.fragment_id
    assert decode_fragment(high.payload, codec=high.codec).fragment_id == fragment.fragment_id


def test_normalize_and_encode_serialise_the_fragment_only_once() -> None:
    """The canonical document is built to derive the id; encoding must reuse it."""
    import mrs3.source_v6 as source_v6
    from mrs3.source_v6 import encode_fragment, normalize_source_v6

    source = FIXTURE.read_bytes()

    calls: list[str] = []
    original = source_v6.canonical_fragment_payload

    def counting(item):
        calls.append("payload")
        return original(item)

    patch = pytest.MonkeyPatch()
    patch.setattr(source_v6, "canonical_fragment_payload", counting)
    try:
        fragment, encoded = source_v6.normalize_and_encode_source_v6(source, source_name="report.html")
    finally:
        patch.undo()

    # Byte-identical to normalising and encoding separately.
    reference = encode_fragment(normalize_source_v6(source, source_name="report.html"))
    assert encoded.fragment_id == fragment.fragment_id == reference.fragment_id
    assert encoded.canonical == reference.canonical
    assert encoded.payload == reference.payload
    assert encoded.codec == reference.codec
    assert sha256(encoded.canonical).hexdigest() == fragment.fragment_id
    assert calls == ["payload"]


def test_decode_derives_identity_from_stored_bytes_without_reserialising() -> None:
    """Identity is sha256 of the canonical bytes, so decode must not rebuild them."""
    import mrs3.source_v6 as source_v6
    from mrs3.source_v6 import decode_fragment, encode_fragment

    fragment = _fragment()
    encoded = encode_fragment(fragment)

    calls: list[str] = []
    original = source_v6.canonical_fragment_bytes

    def counting(item):
        calls.append("serialise")
        return original(item)

    patch = pytest.MonkeyPatch()
    patch.setattr(source_v6, "canonical_fragment_bytes", counting)
    try:
        decoded = decode_fragment(encoded.payload, codec=encoded.codec, expected_fragment_id=fragment.fragment_id)
    finally:
        patch.undo()

    # source_sha256/source_name live in columns, not in the canonical payload.
    assert decoded == replace(fragment, source_sha256="", source_name="")
    assert decoded.fragment_id == fragment.fragment_id
    assert calls == []


def test_decode_still_rejects_tampered_payload_and_wrong_expected_id() -> None:
    import zlib

    from mrs3.source_v6 import SourceV6Error, decode_fragment, encode_fragment

    fragment = _fragment()
    encoded = encode_fragment(fragment)

    with pytest.raises(SourceV6Error, match="identity mismatch"):
        decode_fragment(encoded.payload, codec=encoded.codec, expected_fragment_id="0" * 64)

    raw = zlib.decompress(encoded.payload)
    assert b'"upnl":"0"' in raw
    tampered = zlib.compress(raw.replace(b'"upnl":"0"', b'"upnl":"9"', 1), 9)
    with pytest.raises(SourceV6Error, match="identity mismatch"):
        decode_fragment(tampered, codec=encoded.codec, expected_fragment_id=fragment.fragment_id)


def test_compact_codec_preserves_high_precision_decimal_on_readback(tmp_path: Path) -> None:
    from mrs3.source_v6 import NormalizedSample, canonical_fragment_id, decode_fragment, encode_fragment

    precise = Decimal("4.6530000000000000000000000117")
    fragment = _fragment()
    fragment = replace(
        fragment,
        wallet_samples=(NormalizedSample(fragment.wallet_samples[0].timestamp_ms, precise, fragment.wallet_samples[0].upnl), *fragment.wallet_samples[1:]),
    )
    fragment = replace(fragment, fragment_id=canonical_fragment_id(fragment))

    encoded = encode_fragment(fragment)
    decoded = decode_fragment(encoded.payload, codec=encoded.codec)
    assert decoded.wallet_samples[0].value == precise

    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))
    assert reconstruct_fragment(database, fragment.fragment_id).wallet_samples[0].value == precise


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (Decimal("-4.6530000000000000000000000117"), "-4.6530000000000000000000000117"),
        (Decimal("1.50E+2"), "150"),
        (Decimal("0E+3"), "0"),
        (Decimal("-0E+5"), "0"),
        (Decimal("0E+10"), "0"),
    ),
)
def test_decimal_text_preserves_edge_values(value: Decimal, expected: str) -> None:
    from mrs3.source_v6 import _decimal_text

    assert _decimal_text(value) == expected
    assert Decimal(_decimal_text(value)) == value


def test_canonical_decimal_text_and_fragment_identity_are_golden() -> None:
    from mrs3.source_v6 import PointIdentity, SourceV6Fragment, canonical_fragment_bytes, canonical_fragment_id

    expected_fragment_id = "5f1a80bdf299fac24e15bcf3f9b2e59f08ba23af2695c50358e5ffa91dd6ca76"
    fragment = SourceV6Fragment(
        schema_version=1,
        fragment_id=expected_fragment_id,
        source_sha256="source-sha",
        source_name="golden.html",
        point=PointIdentity("BTCUSDT", "LONG", "1h", 50, "SMA", "close", 10, "SMA", "close", 20),
        report_start_ms=1,
        report_end_ms=2,
        initial_balance=Decimal("-4.6530000000000000000000000117"),
        fixed_order_balance=Decimal("1.50E+2"),
        balance_percentage=Decimal("0E+3"),
        settings_fingerprint="settings",
        stitchability="STITCHABLE",
        actions=(),
        cycles=(),
        events=(),
        wallet_samples=(),
        equity_samples=(),
        open_tail_cycle_ids=(),
        metrics={"label": "golden"},
    )

    assert canonical_fragment_bytes(fragment) == (
        b'{"actions":[],"balance_percentage":"0","cycles":[],"equity_samples":[],'
        b'"events":[],"fixed_order_balance":"150","initial_balance":"-4.6530000000000000000000000117",'
        b'"metrics":{"label":"golden"},"open_tail_cycle_ids":[],"point":{'
        b'"close_ma_length":20,"close_ma_source":"close","close_ma_type":"SMA",'
        b'"open_ma_length":10,"open_ma_source":"close","open_ma_type":"SMA",'
        b'"shift_bp":50,"side":"LONG","symbol":"BTCUSDT","timeframe":"1h"},'
        b'"report_end_ms":2,"report_start_ms":1,"schema_version":1,"settings_fingerprint":'
        b'"settings","stitchability":"STITCHABLE","wallet_samples":[]}'
    )
    assert canonical_fragment_id(fragment) == expected_fragment_id


def test_compact_database_has_indexed_fragment_rows_and_sorted_content_digest(tmp_path: Path) -> None:
    from mrs3.source_v6_storage import iter_fragments, source_content_digest

    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database, database_id="compact-db")
    first, second = _fragment(), _fragment_b()
    for fragment in (second, first):
        import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))

    assert tuple(item.fragment_id for item in iter_fragments(database)) == tuple(sorted((first.fragment_id, second.fragment_id)))
    info = database_info(database)
    assert info["fingerprint"] == "source-v6-fresh-compact-v1"
    assert info["source_content_digest"] == source_content_digest((first.fragment_id, second.fragment_id))
    connection = duckdb.connect(str(database), read_only=True)
    try:
        tables = {row[0] for row in connection.execute("select table_name from information_schema.tables where table_schema='main'").fetchall()}
        assert "compact_fragments" in tables
        assert not {"actions", "cycles", "events", "samples"}.intersection(tables)
    finally:
        connection.close()


def test_compact_database_rejects_corrupt_payload_on_readback(tmp_path: Path) -> None:
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    fragment = _fragment()
    import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))
    connection = duckdb.connect(str(database))
    try:
        payload = connection.execute("select payload_blob from compact_fragments where fragment_id = ?", [fragment.fragment_id]).fetchone()[0]
        connection.execute("update compact_fragments set payload_blob = ? where fragment_id = ?", [bytes(payload)[:-1] + b"x", fragment.fragment_id])
    finally:
        connection.close()

    with pytest.raises(SourceV6StorageError, match="corrupt|mismatch|decode"):
        reconstruct_fragment(database, fragment.fragment_id)


def test_compact_database_fails_closed_for_old_fingerprint(tmp_path: Path) -> None:
    database = tmp_path / "old.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute("create table schema_info(key varchar, value varchar)")
        connection.execute("insert into schema_info values ('schema_version', '6'), ('fingerprint', 'source-v6-normalized-fragment-v1')")
    finally:
        connection.close()

    with pytest.raises(SourceV6StorageError, match="fresh|fingerprint"):
        create_v6_database(database)


def test_verify_published_identity_parallel_matches_serial_and_catches_corruption(
    tmp_path: Path,
) -> None:
    """C9: the parallel readback is the serial one, split across processes.

    Two fragments and `chunk_size=1`, so `workers=4` really produces more than
    one slice — with a single fragment the fan-out is short-circuited and this
    would test the serial path twice.

    Each fragment is corrupted alone and in turn, rather than both at once:
    corrupting both would still be caught by a fan-out that dropped a slice or
    ignored a future, since the surviving slice would report the other one.
    Testing them one at a time is what makes every slice's result load-bearing.

    The corruption is a payload swap with `payload_sha256` repaired to match —
    the case C3a exists for. Every checksum stays self-consistent, so the
    parent's column check passes and only re-deriving the id from the stored
    bytes in a worker can catch it.
    """
    target = tmp_path / "verify.source-v6.duckdb"
    storage.create_v6_database(target)
    for fragment in (_fragment(), _fragment_b()):
        storage.import_fragment(
            target, fragment, preflight_token=storage.preflight_import(target, fragment)
        )

    for workers in (1, 4):
        storage.verify_published_identity_parallel(target, workers=workers, chunk_size=1)

    connection = duckdb.connect(str(target))
    try:
        payloads = connection.execute(
            "select fragment_id, payload_blob from compact_fragments order by fragment_id"
        ).fetchall()
    finally:
        connection.close()
    assert len(payloads) == 2
    original = {str(row[0]): bytes(row[1]) for row in payloads}

    for corrupted, donor in ((payloads[0], payloads[1]), (payloads[1], payloads[0])):
        connection = duckdb.connect(str(target))
        try:
            connection.execute(
                "update compact_fragments set payload_blob = ?, payload_sha256 = ? "
                "where fragment_id = ?",
                [bytes(donor[1]), sha256(bytes(donor[1])).hexdigest(), str(corrupted[0])],
            )
        finally:
            connection.close()

        for workers in (1, 4):
            with pytest.raises(storage.SourceV6StorageError, match="readback mismatch"):
                storage.verify_published_identity_parallel(
                    target, workers=workers, chunk_size=1
                )

        connection = duckdb.connect(str(target))
        try:
            restored = original[str(corrupted[0])]
            connection.execute(
                "update compact_fragments set payload_blob = ?, payload_sha256 = ? "
                "where fragment_id = ?",
                [restored, sha256(restored).hexdigest(), str(corrupted[0])],
            )
        finally:
            connection.close()
        storage.verify_published_identity_parallel(target, workers=4, chunk_size=1)


def test_verify_fragment_slice_rejects_an_id_the_window_does_not_return(
    tmp_path: Path,
) -> None:
    """A short window must fail, not silently verify fewer rows than it asked for.

    Unreachable from the internal callers, which read ids from the same
    committed snapshot as the payloads. It is pinned anyway because on the
    parallel path those two reads happen on different connections, and this
    readback is what authorises `safe_to_delete=YES`: a window that quietly
    returned nothing would report success having verified nothing.
    """
    fragment = _fragment()
    target = tmp_path / "short.source-v6.duckdb"
    storage.create_v6_database(target)
    storage.import_fragment(
        target, fragment, preflight_token=storage.preflight_import(target, fragment)
    )
    present = fragment.fragment_id

    storage.verify_fragment_slice(target, [present])
    with pytest.raises(storage.SourceV6StorageError, match="readback mismatch"):
        storage.verify_fragment_slice(target, [present, "0" * 64])


def test_verify_published_identity_parallel_still_checks_the_columns(
    tmp_path: Path,
) -> None:
    """The column half is not covered by the payload half, so pin it separately.

    `_assert_canonical_matches_columns` never reads `payload_sha256`,
    `header_sha256`, `source_sha256` or `source_name`. Corrupting only
    `payload_sha256` leaves the payload hashing to its id, so every worker
    passes and the parent's single query is the only thing that can object.
    """
    fragment = _fragment()
    target = tmp_path / "columns.source-v6.duckdb"
    storage.create_v6_database(target)
    storage.import_fragment(
        target, fragment, preflight_token=storage.preflight_import(target, fragment)
    )

    connection = duckdb.connect(str(target))
    try:
        connection.execute("update compact_fragments set payload_sha256 = ?", ["0" * 64])
    finally:
        connection.close()

    with pytest.raises(storage.SourceV6StorageError, match="readback mismatch"):
        storage.verify_published_identity_parallel(target, workers=4, chunk_size=1)


def test_verify_published_identity_parallel_rejects_a_non_positive_chunk_size(
    tmp_path: Path,
) -> None:
    """A zero chunk would make `range` raise from inside the slicing instead."""
    target = tmp_path / "chunk.source-v6.duckdb"
    storage.create_v6_database(target)
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        storage.verify_published_identity_parallel(target, workers=2, chunk_size=0)


def _zero_activity_fragment():
    return normalize_source_v6(
        (FIXTURE.parent / "source_v6_zero_activity.html").read_bytes(),
        source_name="zero.html",
    )


def test_day_ownership_labels_an_empty_fragment_and_still_owns_its_days(
    tmp_path: Path,
) -> None:
    """Z4: a tested window with no trades is coverage, and is marked as such.

    Both halves matter. The days must be owned — withholding them would leave a
    permanent artificial gap for a window that was actually tested — and they
    must be distinguishable, or a consumer reads an empty window as traded data.
    """
    empty = _zero_activity_fragment()
    target = tmp_path / "empty.source-v6.duckdb"
    storage.create_v6_database(target)
    storage.import_fragment(target, empty, preflight_token=storage.preflight_import(target, empty))

    connection = duckdb.connect(str(target), read_only=True)
    try:
        rows = connection.execute(
            "select ownership, count(*) from day_ownership group by ownership"
        ).fetchall()
    finally:
        connection.close()
    # 2026-01-01 to 2026-01-09 half-open is eight owned days, all marked empty.
    assert rows == [("ACTIVE_EMPTY", 8)]


def test_day_ownership_labels_a_normal_fragment_active(tmp_path: Path) -> None:
    """Z4's other side: a fragment with facts keeps `ACTIVE`.

    Without this the labelling could invert and the test above would not know.
    """
    fragment = _fragment()
    target = tmp_path / "normal.source-v6.duckdb"
    storage.create_v6_database(target)
    storage.import_fragment(
        target, fragment, preflight_token=storage.preflight_import(target, fragment)
    )

    connection = duckdb.connect(str(target), read_only=True)
    try:
        rows = connection.execute(
            "select distinct ownership from day_ownership"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [("ACTIVE",)]
