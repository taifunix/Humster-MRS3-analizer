"""Fresh immutable multi-scope Source v6 surface publisher."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
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


def _stored_scope_digest(scope_key: str, fragment_ids: list[str], witness: object) -> str:
    return sha256(_canonical_json({"scope": scope_key, "facts": sorted(fragment_ids), "witness": witness}).encode("utf-8")).hexdigest()


def publish_multiscope_surface(
    directory: str | Path,
    materialized: MaterializedSourceV6,
    *,
    source_database: str | Path | None = None,
) -> Path:
    """Write one fresh `.surface-v6.duckdb`.

    `source_database` is the compact database these fragments came from. Given
    it, sealed payloads are copied across in SQL instead of being rebuilt in
    Python — see S1. The stored payload is byte-identical to what
    `encode_fragment` would produce, so this is the same artifact for less work,
    not a different one. Without it the previous behaviour stands.
    """
    with OutputDirectoryLock(Path(directory)):
        return _publish_multiscope_surface(directory, materialized, source_database)


def _copy_sealed_payloads(
    connection: duckdb.DuckDBPyConnection, source_database: str | Path, scopes: tuple
) -> None:
    """Copy `payload_blob`, `codec` and `payload_sha256` straight across (S1).

    One `INSERT ... SELECT` per scope against the attached source, so payload
    bytes never enter Python. `_COPY_BATCH` bounds the bind list, not the work.
    """
    literal = str(Path(source_database).resolve()).replace("'", "''")
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
    finally:
        try:
            connection.execute("detach surface_source")
        except Exception:
            pass


def _validate_published_payloads(path: str, scopes: tuple) -> None:
    """Re-derive every identity from the stored bytes (S3).

    The C3a predicate: `sha256(zlib.decompress(payload_blob))` is the
    `fragment_id`, and the stored `payload_sha256` describes the stored bytes.
    Publication does not need the decoded object, and reconstructing every
    fragment to check an id it can derive directly cost 48.1 s of a 100.4 s
    publication.
    """
    connection = duckdb.connect(path, read_only=True)
    try:
        expected = {scope.scope_key: {item.fragment_id for item in scope.facts} for scope in scopes}
        rows = connection.execute(
            "select scope_key, fragment_id, point_key, payload_blob, codec, payload_sha256 "
            "from factual_fragments"
        ).fetchall()
    finally:
        connection.close()
    seen: dict[str, set[str]] = {key: set() for key in expected}
    for scope_key, fragment_id, point_key, payload, codec, payload_sha256 in rows:
        scope_key, fragment_id = str(scope_key), str(fragment_id)
        if scope_key not in expected or fragment_id not in expected[scope_key]:
            raise ValueError(f"surface holds an unexpected payload: {scope_key}")
        blob = bytes(payload)
        if sha256(blob).hexdigest() != str(payload_sha256):
            raise ValueError(f"payload checksum mismatch: {scope_key}")
        if not str(codec).startswith("json+zlib-v1:"):
            raise ValueError(f"payload codec is not supported: {scope_key}")
        try:
            canonical = zlib.decompress(blob)
        except zlib.error as error:
            raise ValueError(f"payload is not decodable: {scope_key}") from error
        if sha256(canonical).hexdigest() != fragment_id:
            raise ValueError(f"payload does not derive its fragment id: {scope_key}")
        # `point_key` is stored as a column, so scope purity is a string compare
        # rather than a reason to rebuild the fragment.
        if not str(point_key).startswith(scope_key.split("|")[0] + "|"):
            raise ValueError(f"factual fragment belongs to another scope: {scope_key}")
        seen[scope_key].add(fragment_id)
    if seen != expected:
        raise ValueError("surface payload set does not match the materialized scopes")


def _publish_multiscope_surface(
    directory: str | Path,
    materialized: MaterializedSourceV6,
    source_database: str | Path | None = None,
) -> Path:
    if not materialized.scopes:
        raise ValueError("cannot publish an empty multi-scope surface")
    scopes = tuple(sorted(materialized.scopes, key=lambda item: item.scope_key))
    digests = {scope.scope_key: _scope_digest(scope) for scope in scopes}
    surface_facts_digest = source_content_digest(item.fragment_id for scope in scopes for item in scope.facts)
    surface_id = sha256(_canonical_json({"source_content_digest": materialized.source_content_digest, "scope_digests": digests}).encode("utf-8")).hexdigest()
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{surface_id}.surface-v6.duckdb"
    if target.exists():
        raise FileExistsError(f"surface target already exists: {target}")
    handle, temporary = tempfile.mkstemp(prefix=".surface-v6-", suffix=".staging", dir=output)
    os.close(handle)
    os.unlink(temporary)
    connection = duckdb.connect(temporary)
    try:
        connection.execute("create table manifest(key varchar primary key, value varchar not null)")
        connection.execute("create table scope_manifests(scope_key varchar primary key, scope_digest varchar not null, witness_json varchar not null)")
        connection.execute("create table factual_fragments(scope_key varchar not null, fragment_id varchar not null, point_key varchar not null, payload_blob blob not null, codec varchar not null, payload_sha256 varchar not null, primary key(scope_key, fragment_id))")
        # S2: set-based, as C4 established. A statement per row was measured at
        # 736 rows/s on real payloads.
        _insert_frame(
            connection,
            "insert into manifest select key, value from _surface_manifest",
            "_surface_manifest",
            [("fingerprint", FINGERPRINT), ("source_fingerprint", SOURCE_FINGERPRINT), ("source_content_digest", materialized.source_content_digest), ("surface_facts_digest", surface_facts_digest), ("surface_id", surface_id)],
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
        if source_database is not None:
            _copy_sealed_payloads(connection, source_database, scopes)
        else:
            fact_rows = []
            for scope in scopes:
                for fragment in scope.facts:
                    encoded = encode_fragment(fragment)
                    fact_rows.append((scope.scope_key, fragment.fragment_id, fragment.point.canonical_key, encoded.payload, encoded.codec, sha256(encoded.payload).hexdigest()))
            _insert_frame(
                connection,
                "insert into factual_fragments select scope_key, fragment_id, point_key, payload_blob, codec, payload_sha256 from _surface_facts",
                "_surface_facts",
                fact_rows,
                ("scope_key", "fragment_id", "point_key", "payload_blob", "codec", "payload_sha256"),
            )
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
        _validate_published_payloads(temporary, scopes)
        read_multiscope_surface(temporary, decode=False)
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
            "scope_count": len(rows),
            "scope_digests": {str(scope_key): str(digest) for scope_key, digest, _witness in rows},
        }
    finally:
        connection.close()


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
