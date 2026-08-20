"""Atomic merge of fresh compact Source v6 databases.

Merge inputs are opened read-only.  The target is always a new compact DB;
fragments are deduplicated by their canonical identity before stitching is
recomputed across the complete input set.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Callable, Iterable, Mapping, Sequence
from uuid import uuid4

import duckdb

from .source_v6 import SourceV6Fragment, canonical_fragment_bytes
from .source_v6_importer import source_v6_import_lock
from .source_v6_storage import (
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
    database_id: str
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
    if not path.is_file():
        raise SourceV6MergeError(f"merge input does not exist: {path}")
    before = _content_identity(path)
    info = database_info(path)
    fragments = tuple(iter_fragments(path))
    if info["source_content_digest"] != source_content_digest(item.fragment_id for item in fragments):
        raise SourceV6MergeError(f"merge input source content digest mismatch: {path}")
    origins = _origins(path, info, fragments)
    after = _content_identity(path)
    if before != after:
        raise SourceV6MergeError(f"merge input changed while reading: {path}")
    return _Input(path, info["database_id"], after, fragments, origins)


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
    fragments_by_id: dict[str, SourceV6Fragment] = {}
    origins_by_id: dict[str, list[tuple[str, str, str, str]]] = {}
    for item in inputs:
        for fragment in item.fragments:
            previous = fragments_by_id.setdefault(fragment.fragment_id, fragment)
            if canonical_fragment_bytes(previous) != canonical_fragment_bytes(fragment):
                raise SourceV6MergeError(f"canonical fragment identity collision: {fragment.fragment_id}")
            if (fragment.source_sha256, fragment.source_name) < (previous.source_sha256, previous.source_name):
                fragments_by_id[fragment.fragment_id] = fragment
        for origin in item.origins:
            origins_by_id.setdefault(origin[0], []).append(origin)
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
            for fragment in unique:
                _check_cancelled(cancellation_requested)
                token = preflight_import(staging, fragment)
                import_fragment(staging, fragment, preflight_token=token)
            if fault_injector:
                fault_injector("after_write")

            # Recompute ownership from all unique fragments, never from input
            # activation flags.  This makes partition order and associativity
            # independent of prior import boundaries.
            groups: dict[str, list[SourceV6Fragment]] = {}
            for fragment in unique:
                if fragment.stitchability == "STITCHABLE_FIXED_LOT":
                    groups.setdefault(fragment.point.canonical_key, []).append(fragment)
            active: list[SourceV6Fragment] = []
            for point in sorted(groups):
                resolution = resolve_batch(tuple(groups[point]))
                persist_batch_resolution(str(staging), tuple(groups[point]), resolution)
                active.extend(resolution.active_fragments)
            if fault_injector:
                fault_injector("after_stitch")

            # Preserve every raw origin, including duplicate canonical content,
            # without retaining a nested database lineage.
            connection = duckdb.connect(str(staging))
            try:
                connection.execute("begin")
                connection.execute("delete from fragment_origins")
                for fragment_id in sorted(origins_by_id):
                    for origin in sorted(set(origins_by_id[fragment_id]), key=lambda row: (row[1], row[3], row[2])):
                        connection.execute("insert or ignore into fragment_origins values (?, ?, ?, ?)", list(origin))
                connection.execute("commit")
            except Exception:
                connection.execute("rollback")
                raise
            finally:
                connection.close()

            _check_cancelled(cancellation_requested)
            validate_source_v6_database(staging)
            compact_v6_database(staging, compacted)
            validate_source_v6_database(compacted)
            readback = tuple(iter_fragments(compacted))
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
