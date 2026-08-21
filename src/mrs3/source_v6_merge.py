"""Atomic merge of fresh compact Source v6 databases.

Merge inputs are opened read-only.  The target is always a new compact DB;
fragments are deduplicated by their canonical identity before stitching is
recomputed across the complete input set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Callable, Iterable, Mapping, Sequence
from uuid import uuid4
import zlib

import duckdb

from .source_v6 import SourceV6Fragment
from .source_v6_importer import source_v6_import_lock
from .source_v6_storage import (
    _PUBLISH_COLUMNS,
    _insert_frame,
    _publish_metadata_rows,
    _schema_info,
    _utc_now,
    _verify_published_identity,
    SourceV6FragmentMetadata,
    compact_v6_database,
    create_v6_database,
    database_info,
    decode_fragment_slice,
    fragment_metadata,
    source_content_digest,
    validate_source_v6_database,
)
from .source_v6_stitch import persist_batch_resolution, resolve_batch


_COPY_BATCH = 512


class SourceV6MergeError(RuntimeError):
    """Raised when a merge cannot be safely published."""


@dataclass(frozen=True, slots=True)
class SourceV6MergeResult:
    status: str
    target_path: Path
    source_content_digest: str
    input_count: int
    accepted_count: int
    duplicate_count: int
    writer_count: int
    active_fragments: tuple[SourceV6Fragment, ...]

    @property
    def merged_count(self) -> int:
        return self.accepted_count

    @property
    def duplicate_fragments(self) -> int:
        return self.duplicate_count


@dataclass(frozen=True, slots=True)
class SourceV6MergePreflight:
    token: str
    input_paths: tuple[Path, ...]
    target_path: Path
    input_identities: tuple[tuple[int, int, str], ...]
    target_identity: tuple[int, int, str] | None


@dataclass(frozen=True, slots=True)
class _Input:
    path: Path
    identity: tuple[int, int, str]
    fragments: tuple[SourceV6Fragment, ...]
    origins: tuple[tuple[str, str, str, str], ...]


def _content_identity(path: Path) -> tuple[int, int, str]:
    """Capture DB and its mutable DuckDB sidecars for read-only verification."""
    stat = path.stat()
    digest = sha256()
    for artifact in (path, Path(f"{path}.wal"), Path(f"{path}.tmp")):
        digest.update(artifact.name.encode("utf-8"))
        if not artifact.exists():
            digest.update(b"\0")
            continue
        if not artifact.is_file():
            raise SourceV6MergeError(f"merge input sidecar is not a file: {artifact}")
        content = artifact.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return stat.st_size, stat.st_mtime_ns, digest.hexdigest()


def _paths(value: Iterable[str | Path]) -> tuple[Path, ...]:
    if isinstance(value, (str, Path)):
        value = (value,)
    return tuple(dict.fromkeys(Path(item).resolve() for item in value))


def _origins(path: Path, info: Mapping[str, str], fragments: Sequence[SourceV6Fragment]) -> tuple[tuple[str, str, str, str], ...]:
    fallback = tuple((item.fragment_id, item.source_sha256, item.source_name, info["database_id"]) for item in fragments)
    connection = duckdb.connect(str(path), read_only=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "select table_name from information_schema.tables where table_schema='main'"
            ).fetchall()
        }
        if "fragment_origins" not in tables:
            return fallback
        rows = connection.execute(
            "select fragment_id, source_sha256, source_name, origin_database_id from fragment_origins order by fragment_id, source_sha256, origin_database_id"
        ).fetchall()
        allowed = {item.fragment_id for item in fragments}
        return tuple(
            (str(fragment_id), str(source_sha256), str(source_name), str(origin_database_id))
            for fragment_id, source_sha256, source_name, origin_database_id in rows
            if str(fragment_id) in allowed
        ) or fallback
    finally:
        connection.close()


def _read_input(path: Path) -> _Input:
    """Read one input's metadata; payloads stay in DuckDB until publication.

    The digest check needs only fragment ids, and deduplication, ordering and
    ownership resolution need only the metadata columns. Decoding every
    fragment here is what made a large merge exceed available memory.
    """
    if not path.is_file():
        raise SourceV6MergeError(f"merge input does not exist: {path}")
    before = _content_identity(path)
    info = database_info(path)
    fragments = fragment_metadata(path)
    if info["source_content_digest"] != source_content_digest(item.fragment_id for item in fragments):
        raise SourceV6MergeError(f"merge input source content digest mismatch: {path}")
    origins = _origins(path, info, fragments)
    after = _content_identity(path)
    if before != after:
        raise SourceV6MergeError(f"merge input changed while reading: {path}")
    return _Input(path, after, fragments, origins)


def _merge_token(
    input_paths: Sequence[Path],
    target_path: Path,
    input_identities: Sequence[tuple[int, int, str]],
    target_identity: tuple[int, int, str] | None,
) -> str:
    payload = {
        "format": "source-v6-fresh-compact-v1-merge",
        "inputs": [(str(path), list(identity)) for path, identity in zip(input_paths, input_identities)],
        "target": str(target_path),
        "target_identity": None if target_identity is None else list(target_identity),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def preflight_source_v6_merge(
    input_paths: Iterable[str | Path] | None = None,
    target_path: str | Path | None = None,
    *,
    source_paths: Iterable[str | Path] | None = None,
    output_path: str | Path | None = None,
) -> SourceV6MergePreflight:
    if input_paths is None:
        input_paths = source_paths
    if target_path is None:
        target_path = output_path
    if input_paths is None or target_path is None:
        raise SourceV6MergeError("merge inputs and target are required")
    target = Path(target_path).resolve()
    paths = _paths(input_paths)
    if not paths:
        raise SourceV6MergeError("at least one merge input is required")
    if target in paths:
        raise SourceV6MergeError("merge target must be separate from inputs")
    if target.exists():
        raise SourceV6MergeError(f"Source v6 merge target already exists: {target}")
    inputs = tuple(_read_input(path) for path in paths)
    identities = tuple(item.identity for item in inputs)
    return SourceV6MergePreflight(_merge_token(paths, target, identities, None), paths, target, identities, None)


source_v6_merge_preflight = preflight_source_v6_merge


def _remove_staging(staging: Path) -> None:
    for path in (staging, Path(f"{staging}.wal"), Path(f"{staging}.tmp")):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _recover_staging(database: Path) -> None:
    """Remove only orphan merge staging artifacts for this target after locking."""
    for artifact in database.parent.glob(f".{database.name}.*.staging*"):
        _remove_staging(artifact)


def _check_cancelled(cancellation_requested: Callable[[], bool] | None) -> None:
    if cancellation_requested and cancellation_requested():
        raise SourceV6MergeError("Source v6 merge cancelled")


def _copy_fragments_from_inputs(
    staging: Path,
    unique: Sequence[SourceV6FragmentMetadata],
    owner_by_id: Mapping[str, Path],
    cancellation_requested: Callable[[], bool] | None,
) -> None:
    """Copy the winning rows from each input inside DuckDB, in publication order.

    Payload bytes move input-to-target through SQL, and the metadata tables are
    written with one statement each, so neither the corpus nor a per-fragment
    connection is ever materialised.
    """
    by_owner: dict[Path, list[str]] = {}
    for fragment in unique:
        by_owner.setdefault(owner_by_id[fragment.fragment_id], []).append(fragment.fragment_id)

    connection = duckdb.connect(str(staging))
    attached: list[str] = []
    try:
        info = _schema_info(connection)
        generation = int(info["mutation_generation"])
        owners = sorted(by_owner, key=lambda item: str(item))
        for index, owner in enumerate(owners):
            _check_cancelled(cancellation_requested)
            alias = f"merge_in_{index}"
            literal = str(owner).replace("'", "''")
            connection.execute(f"attach '{literal}' as {alias} (read_only)")
            attached.append(alias)
        connection.execute("begin")
        for index, owner in enumerate(owners):
            alias = f"merge_in_{index}"
            ids = by_owner[owner]
            for start in range(0, len(ids), _COPY_BATCH):
                chunk = ids[start : start + _COPY_BATCH]
                placeholders = ", ".join("?" for _ in chunk)
                connection.execute(
                    f"insert into compact_fragments select {_PUBLISH_COLUMNS}, true, null, null "
                    f"from {alias}.compact_fragments where fragment_id in ({placeholders})",
                    chunk,
                )
        point_rows: list[tuple[object, ...]] = []
        day_rows: list[tuple[object, ...]] = []
        audit_rows: list[tuple[object, ...]] = []
        for fragment in unique:
            started = _utc_now()
            point_rows.append((fragment.point.canonical_key,))
            day = datetime.fromtimestamp(fragment.report_start_ms / 1000, timezone.utc).date()
            end = datetime.fromtimestamp(fragment.report_end_ms / 1000, timezone.utc).date()
            while day < end:
                day_rows.append((fragment.fragment_id, day))
                day += timedelta(days=1)
            audit_rows.append(
                (
                    str(uuid4()), fragment.fragment_id, fragment.source_sha256, started, started,
                    "COMMITTED", generation, generation + 1, "YES", 0, None,
                )
            )
            generation += 1
        # The merge of the two real corpora emitted 1,205,395 day rows;
        # row-by-row binds run at 1,730 rows/s, which is the difference between
        # minutes and hours.
        # `fragment_origins` is deliberately left empty here: `merge_source_v6`
        # deletes and rewrites the whole table from the flattened per-input
        # lineage, so anything written now is thrown away unread.
        _publish_metadata_rows(connection, point_rows, (), day_rows, audit_rows)
        connection.execute(
            "update schema_info set value = ? where key = 'mutation_generation'", [str(generation)]
        )
        ids = [
            str(row[0])
            for row in connection.execute(
                "select fragment_id from compact_fragments order by fragment_id"
            ).fetchall()
        ]
        connection.execute(
            "update schema_info set value = ? where key = 'source_content_digest'",
            [source_content_digest(ids)],
        )
        connection.execute("commit")
        # Deliberately after the commit, for a measured reason whose mechanism
        # is only partly established, and the two measurements below are on
        # different artifacts rather than an A/B on one. The readback windows
        # rows 128 ids at a time. Against the *committed* 59,675-fragment
        # input database (4.3 GB of payload) one window costs 0.239 s — far too
        # fast to be reading the payload column, so the scan touches
        # `fragment_id` and materialises payload only for the 128 matches.
        # Against rows still open in this transaction, the 63,131-fragment
        # merge of both corpora did not finish in 40 minutes, parked here under
        # `py-spy`. The plan is a sequential scan either way, so index
        # availability is *not* the difference. Transaction-local storage of
        # the 4.7 GB of payload is the likeliest cause; that is unproven and is
        # not asserted here.
        #
        # Nothing is published by committing here: the target is a `.staging`
        # file, and publication is the `compacted.replace(target)` in
        # `merge_source_v6`, which this function's caller reaches only if the
        # verification below raises nothing. `validate_source_v6_database`
        # already runs post-commit on the same staging file for the same
        # reason.
        _verify_published_identity(connection)
    except Exception:
        try:
            connection.execute("rollback")
        except Exception:
            pass
        raise
    finally:
        for alias in attached:
            try:
                connection.execute(f"detach {alias}")
            except Exception:
                pass
        connection.close()


def _assert_no_identity_collision(inputs: Sequence[_Input], contested: set[str]) -> None:
    """Confirm ids shared by several inputs really carry the same bytes.

    Only the contested ids are decompressed, so the cost is bounded by actual
    overlap rather than by corpus size.
    """
    if not contested:
        return
    seen: dict[str, bytes] = {}
    for item in inputs:
        wanted = sorted(contested.intersection({row.fragment_id for row in item.fragments}))
        if not wanted:
            continue
        connection = duckdb.connect(str(item.path), read_only=True)
        try:
            placeholders = ", ".join("?" for _ in wanted)
            rows = connection.execute(
                f"select fragment_id, payload_blob from compact_fragments where fragment_id in ({placeholders})",
                wanted,
            ).fetchall()
        finally:
            connection.close()
        for fragment_id, payload in rows:
            canonical = zlib.decompress(bytes(payload))
            known = seen.setdefault(str(fragment_id), canonical)
            if known != canonical:
                raise SourceV6MergeError(f"canonical fragment identity collision: {fragment_id}")


def merge_source_v6(
    input_paths: Iterable[str | Path] | None,
    target_path: str | Path | None,
    *,
    preflight: SourceV6MergePreflight | None = None,
    source_databases: Iterable[str | Path] | None = None,
    output_path: str | Path | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
    fault_injector: Callable[[str], object] | None = None,
) -> SourceV6MergeResult:
    """Merge fresh Source v6 DBs into a new target with one writer."""
    if input_paths is None:
        input_paths = source_databases
    if target_path is None:
        target_path = output_path
    if preflight is not None:
        if input_paths is None:
            input_paths = preflight.input_paths
        if target_path is None:
            target_path = preflight.target_path
    if input_paths is None or target_path is None:
        raise SourceV6MergeError("merge inputs and target are required")
    target = Path(target_path).resolve()
    paths = _paths(input_paths)
    if not paths:
        raise SourceV6MergeError("at least one merge input is required")
    if target in paths:
        raise SourceV6MergeError("merge target must be separate from inputs")

    # Read and validate all inputs before acquiring the target writer lock.
    inputs = tuple(_read_input(path) for path in paths)
    if preflight is not None:
        if tuple(preflight.input_paths) != paths or preflight.target_path != target:
            raise SourceV6MergeError("merge preflight does not match inputs")
        if _merge_token(paths, target, tuple(item.identity for item in inputs), None) != preflight.token:
            raise SourceV6MergeError("stale Source v6 merge preflight")
    fragments_by_id: dict[str, SourceV6FragmentMetadata] = {}
    owner_by_id: dict[str, Path] = {}
    contested: set[str] = set()
    origins_by_id: dict[str, list[tuple[str, str, str, str]]] = {}
    for item in inputs:
        for fragment in item.fragments:
            previous = fragments_by_id.setdefault(fragment.fragment_id, fragment)
            if previous is not fragment:
                # The same id in two inputs must carry the same canonical
                # bytes; anything else is a SHA-256 collision.
                contested.add(fragment.fragment_id)
            else:
                owner_by_id[fragment.fragment_id] = item.path
            if (fragment.source_sha256, fragment.source_name) < (previous.source_sha256, previous.source_name):
                fragments_by_id[fragment.fragment_id] = fragment
                owner_by_id[fragment.fragment_id] = item.path
        for origin in item.origins:
            origins_by_id.setdefault(origin[0], []).append(origin)
    _assert_no_identity_collision(inputs, contested)
    unique = tuple(sorted(fragments_by_id.values(), key=lambda item: (item.point.canonical_key, item.report_start_ms, item.fragment_id)))
    duplicate_count = sum(len(item.fragments) for item in inputs) - len(unique)

    with source_v6_import_lock(target):
        # Every Source v6 target writer uses this shared lock.  The final
        # existence check below is defensive under that lock-scoped contract.
        _recover_staging(target)
        if target.exists():
            raise SourceV6MergeError(f"Source v6 merge target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.{uuid4().hex}.staging"
        compacted = Path(f"{staging}.packed")
        try:
            _check_cancelled(cancellation_requested)
            create_v6_database(staging)
            _copy_fragments_from_inputs(staging, unique, owner_by_id, cancellation_requested)
            if fault_injector:
                fault_injector("after_write")

            # Recompute ownership from all unique fragments, never from input
            # activation flags.  This makes partition order and associativity
            # independent of prior import boundaries.
            groups: dict[str, list[SourceV6FragmentMetadata]] = {}
            for fragment in unique:
                if fragment.stitchability == "STITCHABLE_FIXED_LOT":
                    groups.setdefault(fragment.point.canonical_key, []).append(fragment)
            for point in sorted(groups):
                members: Sequence[object] = groups[point]
                if len(members) > 1:
                    # Seam facts need real cycles and actions, but only a point
                    # with more than one fragment produces a persisted decision.
                    members = decode_fragment_slice(
                        staging, [item.fragment_id for item in groups[point]]
                    )
                resolution = resolve_batch(tuple(members))
                persist_batch_resolution(str(staging), tuple(members), resolution)
            if fault_injector:
                fault_injector("after_stitch")

            # Preserve every raw origin, including duplicate canonical content,
            # without retaining a nested database lineage.
            connection = duckdb.connect(str(staging))
            try:
                connection.execute("begin")
                connection.execute("delete from fragment_origins")
                flattened = [
                    list(origin)
                    for fragment_id in sorted(origins_by_id)
                    for origin in sorted(
                        set(origins_by_id[fragment_id]), key=lambda row: (row[1], row[3], row[2])
                    )
                ]
                # This is the only surviving origin write, so it carries every
                # row: one per input fragment, 65,534 for the merge of the two
                # real corpora against 63,131 published fragments, because
                # lineage keeps the duplicates the publication drops.
                # `insert or ignore` is kept
                # rather than reusing `_ORIGIN_INSERT`, because a plain insert
                # would turn a repeated key into a failure instead of a skip.
                _insert_frame(
                    connection,
                    "insert or ignore into fragment_origins "
                    "select fragment_id, source_sha256, source_name, database_id "
                    "from _merge_origins",
                    "_merge_origins",
                    flattened,
                    ("fragment_id", "source_sha256", "source_name", "database_id"),
                )
                connection.execute("commit")
            except Exception:
                # Guarded like the sibling in `_copy_fragments_from_inputs`: if
                # the `commit` itself failed there is no active transaction, and
                # a bare rollback would replace the real cause with
                # "cannot rollback - no transaction is active".
                try:
                    connection.execute("rollback")
                except Exception:
                    pass
                raise
            finally:
                connection.close()

            _check_cancelled(cancellation_requested)
            validate_source_v6_database(staging)
            compact_v6_database(staging, compacted)
            validate_source_v6_database(compacted)
            readback = fragment_metadata(compacted)
            connection = duckdb.connect(str(compacted), read_only=True)
            try:
                active_ids = {
                    str(row[0])
                    for row in connection.execute("select fragment_id from compact_fragments where active").fetchall()
                }
            finally:
                connection.close()
            # Resolution applies only to fixed-lot fragments.  The result is
            # nevertheless a faithful view of the target: active non-fixed
            # fragments remain active and must be reported too.
            active = [item for item in readback if item.fragment_id in active_ids]
            digest = source_content_digest(item.fragment_id for item in readback)
            info = database_info(compacted)
            if info["source_content_digest"] != digest or {item.fragment_id for item in readback} != set(fragments_by_id):
                raise SourceV6MergeError("merged source content digest/readback mismatch")
            if any(_content_identity(item.path) != item.identity for item in inputs):
                raise SourceV6MergeError("merge input changed before publication")
            if fault_injector:
                fault_injector("before_publish")
            # A compliant concurrent writer cannot publish while this lock is
            # held; reject any target observed before the replace regardless.
            if target.exists():
                raise SourceV6MergeError(f"Source v6 merge target appeared during merge: {target}")
            compacted.replace(target)
            if fault_injector:
                fault_injector("after_publish")
            return SourceV6MergeResult("COMMITTED", target, digest, len(inputs), len(unique), duplicate_count, 1, tuple(sorted(active, key=lambda item: (item.point.canonical_key, item.report_start_ms, item.fragment_id))))
        except Exception:
            _remove_staging(staging)
            raise
        finally:
            _remove_staging(staging)
            _remove_staging(compacted)


run_source_v6_merge = merge_source_v6
source_v6_merge = merge_source_v6
merge_source_v6_databases = merge_source_v6


__all__ = [
    "SourceV6MergeError",
    "SourceV6MergePreflight",
    "SourceV6MergeResult",
    "merge_source_v6",
    "merge_source_v6_databases",
    "preflight_source_v6_merge",
    "run_source_v6_merge",
    "source_v6_merge",
    "source_v6_merge_preflight",
]
