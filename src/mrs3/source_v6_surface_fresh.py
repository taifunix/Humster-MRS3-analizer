"""Fresh immutable multi-scope Source v6 surface publisher."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from typing import Callable
import zlib

import duckdb

from .locking import OutputDirectoryLock
from .source_v6 import _canonical_json, decode_fragment, encode_fragment
from .source_v6_materializer import MaterializedSourceV6
from .source_v6_coverage import ReadyInterval
from .source_v6_storage import _insert_frame, source_content_digest


FINGERPRINT = "surface-v6-fresh-compact-v1"
SOURCE_FINGERPRINT = "source-v6-fresh-compact-v1"
# Bounds the bind list of the pass-through copy, as the merge does.
_COPY_BATCH = 512


@dataclass(frozen=True, slots=True)
class ValidatedMultiscopeScope:
    scope_key: str
    facts: tuple[object, ...]
    ready_witness: ReadyInterval
    scope_digest: str


def _scope_digest(scope: object) -> str:
    witness = asdict(scope.ready_witness)
    witness["start"] = witness["start"].isoformat()
    witness["end"] = witness["end"].isoformat()
    return sha256(_canonical_json({"scope": scope.scope_key, "facts": [item.fragment_id for item in scope.facts], "witness": witness}).encode("utf-8")).hexdigest()


def _witness_bounds(witness: dict) -> tuple[datetime, datetime]:
    """The stored witness as the half-open UTC window analysis measures over."""
    start = date.fromisoformat(str(witness["start"]))
    end = date.fromisoformat(str(witness["end"])) + timedelta(days=1)
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end, time.min, tzinfo=timezone.utc),
    )


def analysis_input_digest(surface_facts_digest: str, scopes: tuple) -> str:
    """Bind the precomputed analysis rows to the facts they were measured from (W8).

    A surface that carries derived rows has to prove they belong to its own
    facts, otherwise a consumer trusting the fast path could analyse one
    surface's numbers against another's payloads. The digest covers the factual
    digest and every row, so either both match or the surface is rejected.
    """
    return sha256(_canonical_json({
        "surface_facts_digest": surface_facts_digest,
        "scopes": {
            scope.scope_key: sorted(
                tuple(scope.analysis_input), key=lambda row: str(row["point_id"])
            )
            for scope in scopes
        },
    }).encode("utf-8")).hexdigest()


def _stored_scope_digest(scope_key: str, fragment_ids: list[str], witness: object) -> str:
    return sha256(_canonical_json({"scope": scope_key, "facts": sorted(fragment_ids), "witness": witness}).encode("utf-8")).hexdigest()


def _artifact_filename(value: str, suffix: str) -> str:
    name = Path(value).name
    if name != value or not name.endswith(suffix) or len(name) > 180:
        raise ValueError(f"artifact filename must end with {suffix}")
    return name


def suggested_multiscope_surface_filename(materialized: MaterializedSourceV6) -> str:
    """Human-readable default; immutable identity stays in the manifest."""
    scopes = tuple(materialized.scopes)
    pairs = sorted({scope.scope_key.split("|", 1)[0].removesuffix("USDT") for scope in scopes})
    start = min(scope.ready_witness.start.isoformat() for scope in scopes)
    end = max(scope.ready_witness.end.isoformat() for scope in scopes)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", "_".join(pairs))
    return _artifact_filename(f"{stem}_{start}_{end}.surface-v6.duckdb", ".surface-v6.duckdb")


def publish_multiscope_surface(
    directory: str | Path,
    materialized: MaterializedSourceV6,
    *,
    source_database: str | Path | None = None,
    progress_callback: Callable[..., object] | None = None,
    filename: str | None = None,
    workers: int = 1,
) -> Path:
    """Write one fresh `.surface-v6.duckdb`.

    `source_database` is the compact database these fragments came from. Given
    it, sealed payloads are copied across in SQL instead of being rebuilt in
    Python — see S1. The stored payload is byte-identical to what
    `encode_fragment` would produce, so this is the same artifact for less work,
    not a different one. Without it the previous behaviour stands.
    """
    with OutputDirectoryLock(Path(directory)):
        return _publish_multiscope_surface(directory, materialized, source_database, progress_callback, filename, workers)


def _copy_sealed_payloads(
    connection: duckdb.DuckDBPyConnection, source_database: str | Path, scopes: tuple,
    progress_callback: Callable[..., object] | None = None,
) -> None:
    """Copy `payload_blob`, `codec` and `payload_sha256` straight across (S1).

    One `INSERT ... SELECT` per scope against the attached source, so payload
    bytes never enter Python. `_COPY_BATCH` bounds the bind list, not the work.
    """
    literal = str(Path(source_database).resolve()).replace("'", "''")
    total = sum(len(scope.facts) for scope in scopes)
    completed = 0
    if progress_callback is not None:
        progress_callback("WRITING", completed=completed, total=total)
    connection.execute(f"attach '{literal}' as surface_source (read_only)")
    try:
        for scope in scopes:
            ids = [item.fragment_id for item in scope.facts]
            for start in range(0, len(ids), _COPY_BATCH):
                chunk = ids[start : start + _COPY_BATCH]
                placeholders = ", ".join("?" for _ in chunk)
                inserted = connection.execute(
                    "insert into factual_fragments "
                    "select ?, fragment_id, point_key, payload_blob, codec, payload_sha256 "
                    f"from surface_source.compact_fragments where fragment_id in ({placeholders})",
                    [scope.scope_key, *chunk],
                ).fetchone()
                # A fragment the materializer selected must exist in the source
                # it claims to come from; a silent short copy would publish a
                # surface missing facts its own manifest promises.
                if int(inserted[0]) != len(chunk):
                    raise ValueError("surface source is missing a materialized fragment")
                completed += len(chunk)
                if progress_callback is not None:
                    progress_callback("WRITING", completed=completed, total=total, detail=scope.scope_key)
    finally:
        try:
            connection.execute("detach surface_source")
        except Exception:
            pass


def verify_surface_payload_slice(path: str, ids: list[str]) -> None:
    """Check the byte identity of one explicit id slice (W6).

    Opens its own read-only connection so it is safe in a worker process, and
    returns nothing: the verdict is the absence of an exception, which is what
    keeps the fan-out cheaper than what it verifies.
    """
    if not ids:
        return
    connection = duckdb.connect(path, read_only=True)
    try:
        placeholders = ", ".join("?" for _ in ids)
        rows = connection.execute(
            "select scope_key, fragment_id, payload_blob, payload_sha256 from factual_fragments "
            f"where fragment_id in ({placeholders})",
            list(ids),
        ).fetchall()
    finally:
        connection.close()
    for scope_key, fragment_id, payload, payload_sha256 in rows:
        blob = bytes(payload)
        if sha256(blob).hexdigest() != str(payload_sha256):
            raise ValueError(f"payload checksum mismatch: {scope_key}")
        try:
            canonical = zlib.decompress(blob)
        except zlib.error as error:
            raise ValueError(f"payload is not decodable: {scope_key}") from error
        if sha256(canonical).hexdigest() != str(fragment_id):
            raise ValueError(f"payload does not derive its fragment id: {scope_key}")


def _validate_published_payloads(
    path: str, scopes: tuple, progress_callback: Callable[..., object] | None = None,
    workers: int = 1,
) -> None:
    """Re-derive every identity from the stored bytes (S3, W6).

    The C3a predicate: `sha256(zlib.decompress(payload_blob))` is the
    `fragment_id`, and the stored `payload_sha256` describes the stored bytes.
    Publication does not need the decoded object, and reconstructing every
    fragment to check an id it can derive directly cost 48.1 s of a 100.4 s
    publication.

    W6 splits the check in two. The set, codec and scope-purity half is decided
    here from indexed columns, so no payload byte enters this process. The
    per-fragment `zlib` + `sha256` half is pure Python and independent per
    fragment, so it runs over bounded id slices across processes. Both halves
    apply exactly the predicates the single-pass version applied, and worker
    count changes only elapsed time.
    """
    connection = duckdb.connect(path, read_only=True)
    try:
        expected = {scope.scope_key: {item.fragment_id for item in scope.facts} for scope in scopes}
        rows = connection.execute(
            "select scope_key, fragment_id, point_key, codec from factual_fragments"
        ).fetchall()
    finally:
        connection.close()
    seen: dict[str, set[str]] = {key: set() for key in expected}
    total = len(rows)
    if progress_callback is not None:
        progress_callback("VALIDATING", completed=0, total=total)
    for scope_key, fragment_id, point_key, codec in rows:
        scope_key, fragment_id = str(scope_key), str(fragment_id)
        if scope_key not in expected or fragment_id not in expected[scope_key]:
            raise ValueError(f"surface holds an unexpected payload: {scope_key}")
        if not str(codec).startswith("json+zlib-v1:"):
            raise ValueError(f"payload codec is not supported: {scope_key}")
        # `point_key` is stored as a column, so scope purity is a string compare
        # rather than a reason to rebuild the fragment.
        if not str(point_key).startswith(scope_key.split("|")[0] + "|"):
            raise ValueError(f"factual fragment belongs to another scope: {scope_key}")
        seen[scope_key].add(fragment_id)
    if seen != expected:
        raise ValueError("surface payload set does not match the materialized scopes")
    ids = sorted(fragment_id for members in seen.values() for fragment_id in members)
    slices = [ids[start : start + _COPY_BATCH] for start in range(0, len(ids), _COPY_BATCH)]
    completed = 0
    if max(1, int(workers)) < 2 or len(slices) < 2:
        for chunk in slices:
            verify_surface_payload_slice(path, chunk)
            completed += len(chunk)
            if progress_callback is not None:
                progress_callback("VALIDATING", completed=completed, total=total)
        return
    with ProcessPoolExecutor(max_workers=min(int(workers), len(slices))) as executor:
        futures = {
            executor.submit(verify_surface_payload_slice, path, chunk): len(chunk)
            for chunk in slices
        }
        for future in as_completed(futures):
            future.result()
            completed += futures[future]
            if progress_callback is not None:
                progress_callback("VALIDATING", completed=completed, total=total)


def _publish_multiscope_surface(
    directory: str | Path,
    materialized: MaterializedSourceV6,
    source_database: str | Path | None = None,
    progress_callback: Callable[..., object] | None = None,
    filename: str | None = None,
    workers: int = 1,
) -> Path:
    if not materialized.scopes:
        raise ValueError("cannot publish an empty multi-scope surface")
    scopes = tuple(sorted(materialized.scopes, key=lambda item: item.scope_key))
    digests = {scope.scope_key: _scope_digest(scope) for scope in scopes}
    surface_facts_digest = source_content_digest(item.fragment_id for scope in scopes for item in scope.facts)
    surface_id = sha256(_canonical_json({"source_content_digest": materialized.source_content_digest, "scope_digests": digests}).encode("utf-8")).hexdigest()
    analysis_rows = [
        (scope.scope_key, str(row["point_id"]), _canonical_json(row))
        for scope in scopes
        for row in sorted(tuple(scope.analysis_input), key=lambda item: str(item["point_id"]))
    ]
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=True)
    target = output / (
        _artifact_filename(filename, ".surface-v6.duckdb")
        if filename is not None else suggested_multiscope_surface_filename(materialized)
    )
    if target.exists():
        raise FileExistsError(f"surface target already exists: {target}")
    handle, temporary = tempfile.mkstemp(prefix=".surface-v6-", suffix=".staging", dir=output)
    os.close(handle)
    os.unlink(temporary)
    connection = duckdb.connect(temporary)
    try:
        if progress_callback is not None:
            progress_callback("STAGING", completed=0, total=len(scopes))
        connection.execute("create table manifest(key varchar primary key, value varchar not null)")
        connection.execute("create table scope_manifests(scope_key varchar primary key, scope_digest varchar not null, witness_json varchar not null)")
        connection.execute("create table factual_fragments(scope_key varchar not null, fragment_id varchar not null, point_key varchar not null, payload_blob blob not null, codec varchar not null, payload_sha256 varchar not null, primary key(scope_key, fragment_id))")
        # W8: the analysis input, already measured. Its own table rather than a
        # manifest blob, because a consumer reads one scope at a time.
        connection.execute("create table point_analysis_input(scope_key varchar not null, point_id varchar not null, row_json varchar not null, primary key(scope_key, point_id))")
        # S2: set-based, as C4 established. A statement per row was measured at
        # 736 rows/s on real payloads.
        _insert_frame(
            connection,
            "insert into manifest select key, value from _surface_manifest",
            "_surface_manifest",
            [
                ("fingerprint", FINGERPRINT),
                ("source_fingerprint", SOURCE_FINGERPRINT),
                ("source_content_digest", materialized.source_content_digest),
                ("surface_facts_digest", surface_facts_digest),
                ("surface_id", surface_id),
                # E3: which parameter combinations were tested and produced no
                # trades. They keep their cells, so this is the only place the
                # difference between "traded nothing" and "traded to zero" is
                # recorded.
                ("empty_result_points", _canonical_json(list(materialized.empty_result_points))),
                # W8: absent when no scope carries rows, so an older surface and
                # a hydrated-path surface stay exactly what they were.
                *((("analysis_input_digest", analysis_input_digest(surface_facts_digest, scopes)),) if analysis_rows else ()),
            ],
            ("key", "value"),
        )
        scope_rows = []
        for scope in scopes:
            witness = asdict(scope.ready_witness)
            witness["start"] = witness["start"].isoformat()
            witness["end"] = witness["end"].isoformat()
            scope_rows.append((scope.scope_key, digests[scope.scope_key], json.dumps(witness, sort_keys=True, separators=(",", ":"))))
        _insert_frame(
            connection,
            "insert into scope_manifests select scope_key, scope_digest, witness_json from _surface_scopes",
            "_surface_scopes",
            scope_rows,
            ("scope_key", "scope_digest", "witness_json"),
        )
        if analysis_rows:
            _insert_frame(
                connection,
                "insert into point_analysis_input select scope_key, point_id, row_json from _surface_analysis_input",
                "_surface_analysis_input",
                analysis_rows,
                ("scope_key", "point_id", "row_json"),
            )
        if source_database is not None:
            _copy_sealed_payloads(connection, source_database, scopes, progress_callback)
        else:
            fact_rows = []
            for scope in scopes:
                for fragment in scope.facts:
                    # W3: id-only facts carry no payload to encode. Say so here
                    # rather than failing obscurely inside `encode_fragment`.
                    if not hasattr(fragment, "point"):
                        raise ValueError(
                            "publishing id-only materialized facts requires source_database"
                        )
                    encoded = encode_fragment(fragment)
                    fact_rows.append((scope.scope_key, fragment.fragment_id, fragment.point.canonical_key, encoded.payload, encoded.codec, sha256(encoded.payload).hexdigest()))
            _insert_frame(
                connection,
                "insert into factual_fragments select scope_key, fragment_id, point_key, payload_blob, codec, payload_sha256 from _surface_facts",
                "_surface_facts",
                fact_rows,
                ("scope_key", "fragment_id", "point_key", "payload_blob", "codec", "payload_sha256"),
            )
            if progress_callback is not None:
                progress_callback("WRITING", completed=len(fact_rows), total=len(fact_rows))
        if progress_callback is not None:
            progress_callback("CHECKPOINT")
        connection.execute("checkpoint")
        connection.close()
        check = duckdb.connect(temporary, read_only=True)
        try:
            if check.execute("select count(*) from scope_manifests").fetchone()[0] != len(scopes):
                raise ValueError("surface readback scope count mismatch")
            if check.execute("select count(*) from factual_fragments").fetchone()[0] != sum(len(scope.facts) for scope in scopes):
                raise ValueError("surface readback factual count mismatch")
        finally:
            check.close()
        # S3: identity from the stored bytes, not a full reconstruction.
        _validate_published_payloads(temporary, scopes, progress_callback, workers)
        read_multiscope_surface(temporary, decode=False)
        if progress_callback is not None:
            progress_callback("COMMIT", completed=1, total=1)
        os.replace(temporary, target)
        return target
    finally:
        try:
            connection.close()
        except Exception:
            pass
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_multiscope_surface(path: str | Path, *, decode: bool = True) -> dict[str, object]:
    """Validate lineage and per-scope factual/witness binding before use.

    `decode=False` skips reconstructing every fragment. Publication uses it
    because `_validate_published_payloads` has already re-derived each identity
    from the stored bytes, and rebuilding the objects to learn the same ids cost
    48.1 s of a 100.4 s publication (S3). Consumers keep the default: they need
    the fragments, not just the guarantee.
    """
    connection = duckdb.connect(str(path), read_only=True)
    try:
        manifest = dict(connection.execute("select key, value from manifest").fetchall())
        if manifest.get("fingerprint") != FINGERPRINT or manifest.get("source_fingerprint") != SOURCE_FINGERPRINT or not manifest.get("source_content_digest") or not manifest.get("surface_facts_digest"):
            raise ValueError("unsupported fresh multi-scope surface")
        rows = connection.execute("select scope_key, scope_digest, witness_json from scope_manifests order by scope_key").fetchall()
        if not rows:
            raise ValueError("surface has no scopes")
        all_fragment_ids = []
        for scope_key, expected, witness_json in rows:
            if decode:
                scope = _read_scope(connection, str(scope_key), str(expected), str(witness_json))
                all_fragment_ids.extend(item.fragment_id for item in scope.facts)
                continue
            ids = [
                str(row[0])
                for row in connection.execute(
                    "select fragment_id from factual_fragments where scope_key=? order by fragment_id",
                    [str(scope_key)],
                ).fetchall()
            ]
            if _stored_scope_digest(str(scope_key), ids, json.loads(str(witness_json))) != str(expected):
                raise ValueError(f"scope digest mismatch: {scope_key}")
            all_fragment_ids.extend(ids)
        if source_content_digest(all_fragment_ids) != manifest["surface_facts_digest"]:
            raise ValueError("surface factual digest mismatch")
        expected_surface_id = sha256(_canonical_json({"source_content_digest": manifest["source_content_digest"], "scope_digests": {str(scope_key): str(digest) for scope_key, digest, _witness in rows}}).encode("utf-8")).hexdigest()
        if manifest.get("surface_id") != expected_surface_id:
            raise ValueError("surface identity mismatch")
        return {
            "surface_id": manifest["surface_id"],
            "source_content_digest": manifest["source_content_digest"],
            # E3: a record nobody can read is not a record. Older surfaces
            # predate the key and report none, which is what they held.
            "empty_result_points": json.loads(manifest.get("empty_result_points") or "[]"),
            # W8: `None` on a surface that carries no precomputed rows, which
            # is how a consumer knows to measure the facts itself.
            "analysis_input_digest": manifest.get("analysis_input_digest"),
            "scope_count": len(rows),
            "scope_digests": {str(scope_key): str(digest) for scope_key, digest, _witness in rows},
        }
    finally:
        connection.close()


def read_multiscope_analysis_input(path: str | Path) -> dict[str, dict[str, object]] | None:
    """The precomputed analysis rows, verified against the facts (W8, W9).

    Returns `{scope_key: {"witness": (start, end), "rows": [...]}}`, or `None`
    when the surface carries none — an older artifact, or one published from the
    hydrated path — which tells the caller to measure the facts itself rather
    than to trust an empty result.
    """
    connection = duckdb.connect(str(path), read_only=True)
    try:
        manifest = dict(connection.execute("select key, value from manifest").fetchall())
        stored = manifest.get("analysis_input_digest")
        if not stored:
            return None
        rows = connection.execute(
            "select scope_key, point_id, row_json from point_analysis_input order by scope_key, point_id"
        ).fetchall()
        witnesses = {
            str(scope_key): _witness_bounds(json.loads(str(witness_json)))
            for scope_key, witness_json in connection.execute(
                "select scope_key, witness_json from scope_manifests"
            ).fetchall()
        }
    finally:
        connection.close()
    grouped: dict[str, list[dict[str, object]]] = {}
    for scope_key, _point_id, row_json in rows:
        grouped.setdefault(str(scope_key), []).append(json.loads(str(row_json)))
    scopes = tuple(
        SimpleNamespace(scope_key=scope_key, analysis_input=tuple(members))
        for scope_key, members in sorted(grouped.items())
    )
    if analysis_input_digest(str(manifest["surface_facts_digest"]), scopes) != str(stored):
        raise ValueError("surface analysis input does not match its facts")
    return {
        scope_key: {"witness": witnesses[scope_key], "rows": members}
        for scope_key, members in grouped.items()
    }


def read_multiscope_scope(path: str | Path, scope_key: str) -> ValidatedMultiscopeScope:
    """Load one fully validated scope for independent analysis workers."""
    connection = duckdb.connect(str(path), read_only=True)
    try:
        row = connection.execute(
            "select scope_digest, witness_json from scope_manifests where scope_key=?", [scope_key]
        ).fetchone()
        if row is None:
            raise ValueError(f"surface scope not found: {scope_key}")
        return _read_scope(connection, scope_key, str(row[0]), str(row[1]))
    finally:
        connection.close()


def _read_scope(connection: object, scope_key: str, expected: str, witness_json: str) -> ValidatedMultiscopeScope:
    witness = json.loads(witness_json)
    fact_rows = connection.execute(
        "select fragment_id, payload_blob, codec, payload_sha256 from factual_fragments where scope_key=? order by fragment_id",
        [scope_key],
    ).fetchall()
    facts = []
    for fragment_id, payload, codec, payload_sha256 in fact_rows:
        if sha256(bytes(payload)).hexdigest() != str(payload_sha256):
            raise ValueError(f"payload checksum mismatch: {scope_key}")
        fragment = decode_fragment(bytes(payload), codec=str(codec), expected_fragment_id=str(fragment_id))
        actual_scope = f"{fragment.point.symbol}|{fragment.point.side}|{fragment.point.timeframe}"
        if actual_scope != scope_key:
            raise ValueError(f"factual fragment belongs to another scope: {scope_key}")
        facts.append(fragment)
    actual = _stored_scope_digest(scope_key, [item.fragment_id for item in facts], witness)
    if actual != expected:
        raise ValueError(f"scope digest mismatch: {scope_key}")
    return ValidatedMultiscopeScope(
        scope_key=scope_key,
        facts=tuple(facts),
        ready_witness=ReadyInterval(scope_key, date.fromisoformat(witness["start"]), date.fromisoformat(witness["end"])),
        scope_digest=expected,
    )
