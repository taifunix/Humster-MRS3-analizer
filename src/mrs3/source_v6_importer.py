"""Shared fresh Source v6 importer.

Workers only read, normalise and encode HTML.  The parent owns the staging
DuckDB and publishes it with one atomic rename after read-only validation.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Callable, Iterator
from uuid import uuid4

from .locking import OutputDirectoryLock
from .source_v6 import EncodedSourceV6Fragment, SourceV6Error, SourceV6Fragment, encode_fragment, normalize_source_v6
from .source_v6_storage import (
    SourceV6StorageError,
    compact_v6_database,
    create_v6_database,
    database_info,
    import_fragment,
    iter_fragments,
    preflight_import,
    source_content_digest,
    validate_source_v6_database,
)
from .source_v6_stitch import persist_batch_resolution, resolve_batch


MAX_BATCH_SIZE = 32


class SourceV6ImportError(RuntimeError):
    """Raised when an import cannot be safely published."""


class SourceV6ImportCancelled(SourceV6ImportError):
    pass


@dataclass(frozen=True, slots=True)
class SourceV6Snapshot:
    path: Path
    relative_path: str
    source_sha256: str
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
class SourceV6ImportResult:
    status: str
    target_path: Path
    source_content_digest: str
    accepted_count: int
    quarantined_count: int
    safe_to_delete: str
    writer_count: int
    batch_sizes: tuple[int, ...]
    active_fragments: tuple[SourceV6Fragment, ...]
    accepted_fragments: tuple[SourceV6Fragment, ...] = ()
    quarantine_reasons: tuple[str, ...] = ()


def _lock_directory(database_path: Path) -> Path:
    identity = sha256(str(database_path.resolve()).encode("utf-8")).hexdigest()
    return database_path.resolve().parent / f".mrs3-source-v6-import-{identity}"


def source_v6_import_lock(database_path: str | Path) -> OutputDirectoryLock:
    """Return the lock shared by CLI, panel and direct importer callers."""
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
        return (root,)
    if not root.is_dir():
        raise SourceV6ImportError(f"HTML root does not exist: {root}")
    return tuple(sorted((item for item in root.rglob("*") if item.is_file() and item.suffix.casefold() == ".html"), key=lambda item: item.relative_to(root).as_posix()))


def _snapshot(root: Path, path: Path) -> SourceV6Snapshot:
    before = path.stat()
    source = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SourceV6ImportError(f"input HTML changed during preflight: {path}")
    relative = path.name if root.is_file() else path.relative_to(root).as_posix()
    return SourceV6Snapshot(path.resolve(), relative, sha256(source).hexdigest(), len(source), after.st_mtime_ns)


def _token(root: Path, database: Path, snapshots: tuple[SourceV6Snapshot, ...], target_identity: str | None) -> str:
    document = {
        "format": "source-v6-fresh-compact-v1",
        "root": str(root.resolve()),
        "target": str(database.resolve()),
        "target_identity": target_identity,
        "inputs": [(item.relative_path, item.source_sha256, item.source_size, item.source_mtime_ns) for item in snapshots],
    }
    return sha256(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def preflight_source_v6(root_path: str | Path, database_path: str | Path) -> SourceV6ImportPreflight:
    root = Path(root_path).resolve()
    database = Path(database_path).resolve()
    reports = _discover(root)
    if not reports:
        raise SourceV6ImportError("no HTML reports found")
    snapshots = tuple(_snapshot(root, report) for report in reports)
    target_identity = _target_identity(database)
    return SourceV6ImportPreflight(_token(root, database, snapshots, target_identity), root, database, snapshots, target_identity)


def _assert_preflight_current(preflight: SourceV6ImportPreflight) -> None:
    if _target_identity(preflight.database_path) != preflight.target_identity:
        raise SourceV6ImportError("target changed after Source v6 preflight")
    for snapshot in preflight.snapshots:
        try:
            stat = snapshot.path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (snapshot.source_size, snapshot.source_mtime_ns):
                raise SourceV6ImportError(f"input HTML changed after preflight: {snapshot.path}")
        except OSError as error:
            raise SourceV6ImportError(f"input HTML is unavailable after preflight: {snapshot.path}") from error


def _preflight_token_matches(preflight: SourceV6ImportPreflight) -> bool:
    return _token(preflight.root_path, preflight.database_path, preflight.snapshots, preflight.target_identity) == preflight.token


def prepare_source_v6_snapshot(snapshot: SourceV6Snapshot) -> PreparedSourceV6:
    """Read and encode one input; this is the only worker operation."""
    source = snapshot.path.read_bytes()
    stat = snapshot.path.stat()
    if (len(source), stat.st_mtime_ns) != (snapshot.source_size, snapshot.source_mtime_ns):
        raise SourceV6ImportError(f"input HTML changed while importing: {snapshot.path}")
    if sha256(source).hexdigest() != snapshot.source_sha256:
        raise SourceV6ImportError(f"input HTML content changed while importing: {snapshot.path}")
    fragment = normalize_source_v6(source, source_name=snapshot.relative_path)
    return PreparedSourceV6(snapshot, fragment, encode_fragment(fragment))


def _worker(snapshot: SourceV6Snapshot) -> PreparedSourceV6:
    return prepare_source_v6_snapshot(snapshot)


def _stream_workers(
    snapshots: tuple[SourceV6Snapshot, ...],
    workers: int,
    cancellation_requested: Callable[[], bool] | None,
) -> Iterator[tuple[SourceV6Snapshot, PreparedSourceV6 | None, str | None]]:
    iterator = iter(snapshots)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        pending: dict[Future[PreparedSourceV6], SourceV6Snapshot] = {}

        def fill() -> None:
            while len(pending) < max(1, workers) * 2:
                try:
                    snapshot = next(iterator)
                except StopIteration:
                    return
                pending[executor.submit(_worker, snapshot)] = snapshot

        fill()
        while pending:
            if cancellation_requested and cancellation_requested():
                for future in pending:
                    future.cancel()
                raise SourceV6ImportCancelled("Source v6 import cancelled")
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                snapshot = pending.pop(future)
                try:
                    yield snapshot, future.result(), None
                except BaseException as error:
                    yield snapshot, None, str(error)
            fill()


def _stream_injected(
    snapshots: tuple[SourceV6Snapshot, ...],
    worker_fn: Callable[[SourceV6Snapshot], PreparedSourceV6],
    cancellation_requested: Callable[[], bool] | None,
) -> Iterator[tuple[SourceV6Snapshot, PreparedSourceV6 | None, str | None]]:
    for snapshot in snapshots:
        if cancellation_requested and cancellation_requested():
            raise SourceV6ImportCancelled("Source v6 import cancelled")
        try:
            yield snapshot, worker_fn(snapshot), None
        except BaseException as error:
            yield snapshot, None, str(error)


def _record_quarantine(database: Path, snapshot: SourceV6Snapshot, reason: str) -> None:
    import duckdb
    from datetime import datetime, timezone

    connection = duckdb.connect(str(database))
    try:
        info = dict(connection.execute("select key, value from schema_info").fetchall())
        now = datetime.now(timezone.utc).isoformat()
        audit_id = str(uuid4())
        connection.execute("begin")
        connection.execute("insert into quarantine values (?, ?, ?, ?)", [snapshot.source_sha256, snapshot.source_sha256, reason, now])
        connection.execute("insert into import_audit values (?, ?, ?, ?, ?, 'QUARANTINED', ?, ?, 'NO', 1, ?)", [audit_id, snapshot.source_sha256, snapshot.source_sha256, now, now, info["mutation_generation"], info["mutation_generation"], reason])
        connection.execute("commit")
    except Exception:
        try:
            connection.execute("rollback")
        except Exception:
            pass
        finally:
            connection.close()
        raise
    finally:
        try:
            connection.close()
        except Exception:
            pass


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
    """Remove only orphan staging artifacts for this target after the lock."""
    for artifact in database.parent.glob(f".{database.name}.*.staging*"):
        _remove_staging(artifact)


def import_source_v6(
    root_path: str | Path,
    database_path: str | Path,
    *,
    preflight: SourceV6ImportPreflight | None = None,
    workers: int = 4,
    batch_size: int = MAX_BATCH_SIZE,
    cancellation_requested: Callable[[], bool] | None = None,
    fault_injector: Callable[[str], object] | None = None,
    worker_fn: Callable[[SourceV6Snapshot], PreparedSourceV6] | None = None,
) -> SourceV6ImportResult:
    if not 1 <= workers:
        raise ValueError("workers must be positive")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
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
        accepted: list[SourceV6Fragment] = []
        quarantine: list[tuple[SourceV6Snapshot, str]] = []
        batch: list[PreparedSourceV6] = []
        batch_sizes: list[int] = []
        try:
            create_v6_database(staging)

            def write_batch(items: list[PreparedSourceV6]) -> None:
                if not items:
                    return
                if len(items) > MAX_BATCH_SIZE:
                    raise SourceV6ImportError("parent write batch exceeds 32 fragments")
                for item in items:
                    if cancellation_requested and cancellation_requested():
                        raise SourceV6ImportCancelled("Source v6 import cancelled")
                    token = preflight_import(staging, item.fragment)
                    import_fragment(staging, item.fragment, preflight_token=token, encoded=item.encoded)
                    accepted.append(item.fragment)
                batch_sizes.append(len(items))
                if fault_injector:
                    fault_injector("after_batch")

            stream = _stream_injected(authorized.snapshots, worker_fn, cancellation_requested) if worker_fn else _stream_workers(authorized.snapshots, workers, cancellation_requested)
            for snapshot, prepared, error in stream:
                if prepared is None:
                    quarantine.append((snapshot, error or "worker failed"))
                    continue
                batch.append(prepared)
                if len(batch) >= batch_size:
                    write_batch(batch)
                    batch = []
            write_batch(batch)
            if not accepted:
                raise SourceV6ImportError("all Source v6 reports failed normalization")
            if cancellation_requested and cancellation_requested():
                raise SourceV6ImportCancelled("Source v6 import cancelled")
            for snapshot, reason in quarantine:
                _record_quarantine(staging, snapshot, reason)

            by_point: dict[str, list[SourceV6Fragment]] = {}
            for fragment in accepted:
                if fragment.stitchability == "STITCHABLE_FIXED_LOT":
                    by_point.setdefault(fragment.point.canonical_key, []).append(fragment)
            active: list[SourceV6Fragment] = []
            for fragments in by_point.values():
                resolution = resolve_batch(tuple(fragments))
                persist_batch_resolution(str(staging), tuple(fragments), resolution)
                active.extend(resolution.active_fragments)

            validate_source_v6_database(staging)
            readback = tuple(iter_fragments(staging))
            if {item.fragment_id for item in readback} != {item.fragment_id for item in accepted}:
                raise SourceV6ImportError("staging readback fragment set mismatch")
            info = database_info(staging)
            digest = source_content_digest(item.fragment_id for item in readback)
            if info["source_content_digest"] != digest:
                raise SourceV6ImportError("staging source content digest mismatch")
            compact_v6_database(staging, compacted)
            validate_source_v6_database(compacted)
            compacted_readback = tuple(iter_fragments(compacted))
            if {item.fragment_id for item in compacted_readback} != {item.fragment_id for item in accepted}:
                raise SourceV6ImportError("compacted source readback fragment set mismatch")
            if database_info(compacted)["source_content_digest"] != digest:
                raise SourceV6ImportError("compacted source content digest mismatch")
            if cancellation_requested and cancellation_requested():
                raise SourceV6ImportCancelled("Source v6 import cancelled")
            if fault_injector:
                fault_injector("before_publish")
            if database.exists():
                raise SourceV6ImportError(f"Source v6 target appeared during import: {database}")
            compacted.replace(database)
            if fault_injector:
                fault_injector("after_publish")
            return SourceV6ImportResult("COMMITTED", database, digest, len(accepted), len(quarantine), "YES" if not quarantine else "NO", 1, tuple(batch_sizes), tuple(active), tuple(accepted), tuple(reason for _, reason in quarantine))
        except SourceV6ImportError:
            _remove_staging(staging)
            raise
        except (OSError, SourceV6Error, SourceV6StorageError, RuntimeError) as error:
            _remove_staging(staging)
            raise SourceV6ImportError(str(error)) from error
        finally:
            _remove_staging(staging)
            _remove_staging(compacted)


# Short aliases keep the API discoverable for callers that use ``run`` naming.
run_source_v6_import = import_source_v6
preflight_import_source_v6 = preflight_source_v6
source_v6_lock = source_v6_import_lock
