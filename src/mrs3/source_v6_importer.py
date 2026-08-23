"""Fresh Source v6 importer with metadata preflight and sealed segments."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import multiprocessing
from pathlib import Path
import shutil
from typing import Callable, Iterator
from uuid import uuid4

from .locking import OutputDirectoryLock
from .source_v6 import (
    EncodedSourceV6Fragment,
    SourceV6Error,
    SourceV6Fragment,
    encode_fragment,
    normalize_and_encode_source_v6,
    normalize_source_v6,
)
from .source_v6_storage import (
    SourceV6FragmentMetadata,
    SourceV6SegmentOutcome,
    SourceV6StorageError,
    compact_v6_database,
    database_info,
    decode_fragment_slice,
    fragment_ids,
    fragment_metadata,
    iter_fragments_parallel,
    reduce_source_v6_segments,
    source_content_digest,
    validate_source_v6_database,
    write_source_v6_segment,
)
from .source_v6_stitch import persist_batch_resolution, resolve_batch


MAX_BATCH_SIZE = 32
DEFAULT_WORKER_CHUNK_SIZE = 64
DEFAULT_MAX_IN_FLIGHT_CHUNKS = 60
DEFAULT_SEGMENT_WRITER_LIMIT = 4


class SourceV6ImportError(RuntimeError):
    """Raised when an import cannot be safely published."""


class SourceV6ImportCancelled(SourceV6ImportError):
    pass


class SourceV6WorkerFailure(SourceV6ImportError):
    """A worker failed before a stable source SHA could be trusted."""

    def __init__(self, input_ordinal: int, relative_path: str, reason: str, kind: str = "worker") -> None:
        self.input_ordinal = int(input_ordinal)
        self.relative_path = str(relative_path)
        self.reason = str(reason)
        self.kind = str(kind)
        self.source_sha256: None = None
        super().__init__(f"worker failure ({self.kind}) for {self.relative_path}: {self.reason}")

    def __reduce__(self) -> tuple[object, tuple[int, str, str, str]]:
        return (type(self), (self.input_ordinal, self.relative_path, self.reason, self.kind))


@dataclass(frozen=True, slots=True)
class SourceV6Snapshot:
    input_ordinal: int
    path: Path
    relative_path: str
    source_size: int
    source_mtime_ns: int


@dataclass(frozen=True, slots=True)
class SourceV6ImportPreflight:
    token: str
    root_path: Path
    database_path: Path
    snapshots: tuple[SourceV6Snapshot, ...]
    target_identity: str | None


@dataclass(frozen=True, slots=True)
class PreparedSourceV6:
    snapshot: SourceV6Snapshot
    fragment: SourceV6Fragment
    encoded: EncodedSourceV6Fragment


@dataclass(frozen=True, slots=True)
class _WorkerItem:
    snapshot: SourceV6Snapshot
    prepared: PreparedSourceV6 | None
    source_sha256: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SourceV6ImportResult:
    status: str
    target_path: Path
    source_content_digest: str
    accepted_count: int
    quarantined_count: int
    safe_to_delete: str
    writer_count: int
    batch_sizes: tuple[int, ...]
    active_fragments: tuple[SourceV6Fragment | SourceV6FragmentMetadata, ...]
    accepted_fragments: tuple[SourceV6Fragment | SourceV6FragmentMetadata, ...] = ()
    quarantine_reasons: tuple[str, ...] = ()
    fragments_hydrated: bool = True
    """False when fragments carry metadata only and no fact collections."""


def _lock_directory(database_path: Path) -> Path:
    identity = sha256(str(database_path.resolve()).encode("utf-8")).hexdigest()
    return database_path.resolve().parent / f".mrs3-source-v6-import-{identity}"


def source_v6_import_lock(database_path: str | Path) -> OutputDirectoryLock:
    return OutputDirectoryLock(_lock_directory(Path(database_path)))


def _target_identity(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise SourceV6ImportError(f"Source v6 target is not a file: {path}")
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _discover(root: Path) -> tuple[Path, ...]:
    if root.is_file():
        return () if root.name.casefold().startswith("report_optimizer_") else (root,)
    if not root.is_dir():
        raise SourceV6ImportError(f"HTML root does not exist: {root}")
    reports = (item for item in root.rglob("*") if item.is_file() and item.suffix.casefold() == ".html" and not item.name.casefold().startswith("report_optimizer_"))
    return tuple(sorted(reports, key=lambda item: item.relative_to(root).as_posix().encode("utf-8")))


def _snapshot(root: Path, path: Path, input_ordinal: int) -> SourceV6Snapshot:
    try:
        stat = path.stat()
    except OSError as error:
        raise SourceV6ImportError(f"input HTML is unavailable during preflight: {path}") from error
    relative = path.name if root.is_file() else path.relative_to(root).as_posix()
    return SourceV6Snapshot(input_ordinal, path.resolve(), relative, stat.st_size, stat.st_mtime_ns)


def _token(root: Path, database: Path, snapshots: tuple[SourceV6Snapshot, ...], target_identity: str | None) -> str:
    document = {
        "format": "source-v6-fresh-compact-v2",
        "root": str(root.resolve()),
        "target": str(database.resolve()),
        "target_identity": target_identity,
        "inputs": [
            (item.input_ordinal, item.relative_path, item.source_size, item.source_mtime_ns)
            for item in snapshots
        ],
    }
    return sha256(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def preflight_source_v6(root_path: str | Path, database_path: str | Path) -> SourceV6ImportPreflight:
    root = Path(root_path).resolve()
    database = Path(database_path).resolve()
    reports = _discover(root)
    if not reports:
        raise SourceV6ImportError("no HTML reports found")
    snapshots = tuple(_snapshot(root, report, ordinal) for ordinal, report in enumerate(reports))
    target_identity = _target_identity(database)
    return SourceV6ImportPreflight(_token(root, database, snapshots, target_identity), root, database, snapshots, target_identity)


def _assert_preflight_current(preflight: SourceV6ImportPreflight) -> None:
    if _target_identity(preflight.database_path) != preflight.target_identity:
        raise SourceV6ImportError("target changed after Source v6 preflight")
    for snapshot in preflight.snapshots:
        try:
            stat = snapshot.path.stat()
        except OSError as error:
            raise SourceV6ImportError(f"input HTML is unavailable after preflight: {snapshot.path}") from error
        if (stat.st_size, stat.st_mtime_ns) != (snapshot.source_size, snapshot.source_mtime_ns):
            raise SourceV6ImportError(f"input HTML changed after preflight: {snapshot.path}")


def _preflight_token_matches(preflight: SourceV6ImportPreflight) -> bool:
    return _token(preflight.root_path, preflight.database_path, preflight.snapshots, preflight.target_identity) == preflight.token


def _read_snapshot(snapshot: SourceV6Snapshot) -> tuple[bytes, str]:
    try:
        before = snapshot.path.stat()
    except OSError as error:
        raise SourceV6WorkerFailure(snapshot.input_ordinal, snapshot.relative_path, str(error), "stat") from error
    if (before.st_size, before.st_mtime_ns) != (snapshot.source_size, snapshot.source_mtime_ns):
        raise SourceV6WorkerFailure(snapshot.input_ordinal, snapshot.relative_path, "metadata changed before read", "toctou")
    try:
        source = snapshot.path.read_bytes()
    except OSError as error:
        raise SourceV6WorkerFailure(snapshot.input_ordinal, snapshot.relative_path, str(error), "read") from error
    try:
        after = snapshot.path.stat()
    except OSError as error:
        raise SourceV6WorkerFailure(snapshot.input_ordinal, snapshot.relative_path, str(error), "stat") from error
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(source) != snapshot.source_size:
        raise SourceV6WorkerFailure(snapshot.input_ordinal, snapshot.relative_path, "metadata changed during read", "toctou")
    return source, sha256(source).hexdigest()


def prepare_source_v6_snapshot(snapshot: SourceV6Snapshot) -> PreparedSourceV6:
    """Read and encode one input exactly once after metadata preflight."""
    source, source_sha256 = _read_snapshot(snapshot)
    fragment = normalize_source_v6(source, source_name=snapshot.relative_path)
    if fragment.source_sha256 != source_sha256:
        raise SourceV6WorkerFailure(snapshot.input_ordinal, snapshot.relative_path, "normalizer source SHA mismatch", "worker")
    return PreparedSourceV6(snapshot, fragment, encode_fragment(fragment))


def _worker_item(snapshot: SourceV6Snapshot) -> _WorkerItem:
    source, source_sha256 = _read_snapshot(snapshot)
    try:
        fragment, encoded = normalize_and_encode_source_v6(source, source_name=snapshot.relative_path)
    except SourceV6Error as error:
        return _WorkerItem(snapshot, None, source_sha256, str(error))
    if fragment.source_sha256 != source_sha256:
        raise SourceV6WorkerFailure(snapshot.input_ordinal, snapshot.relative_path, "normalizer source SHA mismatch", "worker")
    return _WorkerItem(snapshot, PreparedSourceV6(snapshot, fragment, encoded), source_sha256)


def _segment_outcome(item: _WorkerItem) -> SourceV6SegmentOutcome:
    prepared = item.prepared
    return SourceV6SegmentOutcome(
        item.snapshot.input_ordinal,
        item.snapshot.relative_path,
        "PREPARED" if prepared is not None else "QUARANTINED",
        item.source_sha256,
        prepared.fragment.fragment_id if prepared is not None else None,
        item.reason,
        item.snapshot.source_size,
        item.snapshot.source_mtime_ns,
    )


_SEGMENT_WRITER_GATE: object | None = None


def _init_segment_writer_gate(gate: object) -> None:
    """Publish the shared writer gate to a pool worker process."""
    global _SEGMENT_WRITER_GATE
    _SEGMENT_WRITER_GATE = gate


@contextmanager
def _segment_writer_slot() -> Iterator[None]:
    """Bound concurrent segment writes without serialising parse/encode work."""
    gate = _SEGMENT_WRITER_GATE
    if gate is None:
        yield
        return
    gate.acquire()
    try:
        yield
    finally:
        gate.release()


def _write_worker_chunk(snapshots: tuple[SourceV6Snapshot, ...], segment_path: Path, run_token: str) -> object:
    items = tuple(_worker_item(snapshot) for snapshot in snapshots)
    outcomes = tuple(_segment_outcome(item) for item in items)
    prepared = tuple(
        (item.snapshot.input_ordinal, item.prepared.fragment, item.prepared.encoded)
        for item in items
        if item.prepared is not None
    )
    with _segment_writer_slot():
        return write_source_v6_segment(segment_path, outcomes, prepared, run_token=run_token, kind="leaf")


def _injected_item(snapshot: SourceV6Snapshot, worker_fn: Callable[[SourceV6Snapshot], PreparedSourceV6]) -> _WorkerItem:
    try:
        prepared = worker_fn(snapshot)
    except SourceV6Error as error:
        source, source_sha256 = _read_snapshot(snapshot)
        del source
        return _WorkerItem(snapshot, None, source_sha256, str(error))
    except SourceV6WorkerFailure:
        raise
    except BaseException as error:
        raise SourceV6WorkerFailure(snapshot.input_ordinal, snapshot.relative_path, str(error), "worker") from error
    return _WorkerItem(snapshot, prepared, prepared.fragment.source_sha256)


def _write_injected_chunk(
    snapshots: tuple[SourceV6Snapshot, ...],
    segment_path: Path,
    run_token: str,
    worker_fn: Callable[[SourceV6Snapshot], PreparedSourceV6],
) -> object:
    items = tuple(_injected_item(snapshot, worker_fn) for snapshot in snapshots)
    outcomes = tuple(_segment_outcome(item) for item in items)
    prepared = tuple(
        (item.snapshot.input_ordinal, item.prepared.fragment, item.prepared.encoded)
        for item in items
        if item.prepared is not None
    )
    return write_source_v6_segment(segment_path, outcomes, prepared, run_token=run_token, kind="leaf")


def _chunks(snapshots: tuple[SourceV6Snapshot, ...], size: int) -> tuple[tuple[SourceV6Snapshot, ...], ...]:
    return tuple(snapshots[start : start + size] for start in range(0, len(snapshots), size))


def _run_chunks(
    snapshots: tuple[SourceV6Snapshot, ...],
    segment_root: Path,
    run_token: str,
    *,
    workers: int,
    worker_chunk_size: int,
    max_in_flight_chunks: int,
    segment_writer_limit: int,
    cancellation_requested: Callable[[], bool] | None,
    worker_fn: Callable[[SourceV6Snapshot], PreparedSourceV6] | None,
    progress_callback: Callable[[int, int], None] | None,
) -> tuple[object, ...]:
    groups = _chunks(snapshots, worker_chunk_size)
    segment_paths = tuple(
        segment_root / f"segment-{group[0].input_ordinal:08d}-{group[-1].input_ordinal:08d}.duckdb"
        for group in groups
    )
    if worker_fn is not None:
        receipts: list[object] = []
        for group, path in zip(groups, segment_paths):
            if cancellation_requested and cancellation_requested():
                raise SourceV6ImportCancelled("Source v6 import cancelled")
            receipts.append(_write_injected_chunk(group, path, run_token, worker_fn))
            if progress_callback:
                progress_callback(sum(item.outcome_count for item in receipts), len(snapshots))
        return tuple(receipts)

    iterator = iter(zip(groups, segment_paths))
    receipts = []
    pending: dict[Future[object], tuple[SourceV6Snapshot, ...]] = {}
    gate = multiprocessing.Semaphore(segment_writer_limit)
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_segment_writer_gate,
        initargs=(gate,),
    ) as executor:
        def fill() -> None:
            while len(pending) < max_in_flight_chunks:
                try:
                    group, path = next(iterator)
                except StopIteration:
                    return
                pending[executor.submit(_write_worker_chunk, group, path, run_token)] = group

        try:
            fill()
            while pending:
                if cancellation_requested and cancellation_requested():
                    raise SourceV6ImportCancelled("Source v6 import cancelled")
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    group = pending.pop(future)
                    try:
                        receipts.append(future.result())
                    except SourceV6WorkerFailure:
                        raise
                    except BaseException as error:
                        snapshot = group[0]
                        raise SourceV6WorkerFailure(snapshot.input_ordinal, snapshot.relative_path, str(error), "worker") from error
                if progress_callback:
                    progress_callback(sum(item.outcome_count for item in receipts), len(snapshots))
                fill()
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    return tuple(sorted(receipts, key=lambda receipt: receipt.ordinal_start))


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
    patterns = (f".{database.name}.*.staging*", f".{database.name}.*.segments*")
    for pattern in patterns:
        for artifact in database.parent.glob(pattern):
            _remove_staging(artifact)


def _quarantine_rows(database: Path) -> tuple[str, ...]:
    import duckdb

    connection = duckdb.connect(str(database), read_only=True)
    try:
        return tuple(str(row[0]) for row in connection.execute("select reason from quarantine order by created_at_utc").fetchall())
    finally:
        connection.close()


def _validate_import_settings(
    workers: int,
    batch_size: int,
    worker_chunk_size: int,
    max_in_flight_chunks: int,
    segment_writer_limit: int | None,
) -> int:
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be positive")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if isinstance(worker_chunk_size, bool) or not isinstance(worker_chunk_size, int) or not 1 <= worker_chunk_size <= 256:
        raise ValueError("source_v6_import.worker_chunk_size must be an integer from 1 to 256")
    if isinstance(max_in_flight_chunks, bool) or not isinstance(max_in_flight_chunks, int) or not 1 <= max_in_flight_chunks <= 240:
        raise ValueError("source_v6_import.max_in_flight_chunks must be an integer from 1 to 240")
    if max_in_flight_chunks < workers:
        raise ValueError("source_v6_import.max_in_flight_chunks must be at least workers")
    if worker_chunk_size * max_in_flight_chunks > 16384:
        raise ValueError("source_v6_import.worker_chunk_size * max_in_flight_chunks must be at most 16384")
    if segment_writer_limit is None:
        return min(DEFAULT_SEGMENT_WRITER_LIMIT, workers)
    if isinstance(segment_writer_limit, bool) or not isinstance(segment_writer_limit, int) or not 1 <= segment_writer_limit <= 8:
        raise ValueError("source_v6_import.segment_writer_limit must be an integer from 1 to 8")
    if segment_writer_limit > workers:
        raise ValueError("source_v6_import.segment_writer_limit must not exceed workers")
    return segment_writer_limit


def import_source_v6(
    root_path: str | Path,
    database_path: str | Path,
    *,
    preflight: SourceV6ImportPreflight | None = None,
    workers: int = 4,
    batch_size: int = MAX_BATCH_SIZE,
    worker_chunk_size: int = DEFAULT_WORKER_CHUNK_SIZE,
    max_in_flight_chunks: int = DEFAULT_MAX_IN_FLIGHT_CHUNKS,
    segment_writer_limit: int | None = None,
    write_batch_size: int | None = None,
    hydrate_fragments: bool = True,
    cancellation_requested: Callable[[], bool] | None = None,
    fault_injector: Callable[[str], object] | None = None,
    worker_fn: Callable[[SourceV6Snapshot], PreparedSourceV6] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> SourceV6ImportResult:
    if write_batch_size is not None:
        batch_size = write_batch_size
    writer_limit = _validate_import_settings(workers, batch_size, worker_chunk_size, max_in_flight_chunks, segment_writer_limit)
    root = Path(root_path).resolve()
    database = Path(database_path).resolve()
    authorized = preflight or preflight_source_v6(root, database)
    if authorized.root_path != root or authorized.database_path != database:
        raise SourceV6ImportError("preflight token does not match Source v6 inputs")
    if not _preflight_token_matches(authorized):
        raise SourceV6ImportError("preflight token does not match Source v6 inputs")
    with source_v6_import_lock(database):
        _assert_preflight_current(authorized)
        if database.exists():
            raise SourceV6ImportError(f"Source v6 target already exists: {database}")
        database.parent.mkdir(parents=True, exist_ok=True)
        _recover_staging(database)
        staging = database.parent / f".{database.name}.{uuid4().hex}.staging"
        compacted = Path(f"{staging}.packed")
        segment_root = database.parent / f".{database.name}.{authorized.token}.segments"
        try:
            segment_root.mkdir(parents=True, exist_ok=False)
            receipts = _run_chunks(
                authorized.snapshots,
                segment_root,
                authorized.token,
                workers=workers,
                worker_chunk_size=worker_chunk_size,
                max_in_flight_chunks=max_in_flight_chunks,
                segment_writer_limit=writer_limit,
                cancellation_requested=cancellation_requested,
                worker_fn=worker_fn,
                progress_callback=progress_callback,
            )
            if fault_injector:
                for _ in receipts:
                    fault_injector("after_batch")
            if cancellation_requested and cancellation_requested():
                raise SourceV6ImportCancelled("Source v6 import cancelled")
            covered = sum(int(receipt.outcome_count) for receipt in receipts)
            if covered != len(authorized.snapshots):
                raise SourceV6ImportError(
                    f"sealed segments cover {covered} of {len(authorized.snapshots)} discovered reports"
                )
            segment_paths = tuple(receipt.path for receipt in receipts)
            reduce_source_v6_segments(segment_paths, staging, run_token=authorized.token, fan_in=8)
            accepted = (
                iter_fragments_parallel(staging, workers=workers)
                if hydrate_fragments
                else fragment_metadata(staging)
            )
            if not accepted:
                raise SourceV6ImportError("all Source v6 reports failed normalization")
            by_point: dict[str, list[SourceV6Fragment]] = {}
            for fragment in accepted:
                if fragment.stitchability == "STITCHABLE_FIXED_LOT":
                    by_point.setdefault(fragment.point.canonical_key, []).append(fragment)
            active: list[SourceV6Fragment] = []
            for fragments in by_point.values():
                if not hydrate_fragments and len(fragments) > 1:
                    # Seam facts need real cycles/actions, but only a point with
                    # more than one fragment ever produces a persisted decision.
                    fragments = list(
                        decode_fragment_slice(staging, [item.fragment_id for item in fragments])
                    )
                resolution = resolve_batch(tuple(fragments))
                persist_batch_resolution(str(staging), tuple(fragments), resolution)
                active.extend(resolution.active_fragments)
            validate_source_v6_database(staging)
            digest = source_content_digest(item.fragment_id for item in accepted)
            if database_info(staging)["source_content_digest"] != digest:
                raise SourceV6ImportError("staging source content digest mismatch")
            compact_v6_database(staging, compacted)
            validate_source_v6_database(compacted)
            if fragment_ids(compacted) != tuple(item.fragment_id for item in accepted):
                raise SourceV6ImportError("compacted source readback fragment set mismatch")
            if database_info(compacted)["source_content_digest"] != digest:
                raise SourceV6ImportError("compacted source content digest mismatch")
            if cancellation_requested and cancellation_requested():
                raise SourceV6ImportCancelled("Source v6 import cancelled")
            _assert_preflight_current(authorized)
            if fault_injector:
                fault_injector("before_publish")
            if database.exists():
                raise SourceV6ImportError(f"Source v6 target appeared during import: {database}")
            compacted.replace(database)
            if fault_injector:
                fault_injector("after_publish")
            reasons = _quarantine_rows(database)
            return SourceV6ImportResult(
                "COMMITTED",
                database,
                digest,
                len(accepted),
                len(reasons),
                "YES" if not reasons else "NO",
                len(receipts),
                tuple(receipt.outcome_count for receipt in receipts),
                tuple(active),
                accepted,
                reasons,
                hydrate_fragments,
            )
        except SourceV6ImportError:
            raise
        except (OSError, SourceV6Error, SourceV6StorageError, RuntimeError) as error:
            raise SourceV6ImportError(str(error)) from error
        finally:
            _remove_staging(staging)
            _remove_staging(compacted)
            _remove_staging(segment_root)


run_source_v6_import = import_source_v6
preflight_import_source_v6 = preflight_source_v6
source_v6_lock = source_v6_import_lock
