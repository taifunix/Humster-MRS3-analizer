"""Fresh immutable multi-scope Source v6 surface publisher."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

import duckdb

from .locking import OutputDirectoryLock
from .source_v6 import _canonical_json, decode_fragment, encode_fragment
from .source_v6_materializer import MaterializedSourceV6
from .source_v6_coverage import ReadyInterval
from .source_v6_storage import source_content_digest


FINGERPRINT = "surface-v6-fresh-compact-v1"
SOURCE_FINGERPRINT = "source-v6-fresh-compact-v1"


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


def publish_multiscope_surface(directory: str | Path, materialized: MaterializedSourceV6) -> Path:
    """Write one fresh `.surface-v6.duckdb`; the caller supplies decoded compact facts."""
    with OutputDirectoryLock(Path(directory)):
        return _publish_multiscope_surface(directory, materialized)


def _publish_multiscope_surface(directory: str | Path, materialized: MaterializedSourceV6) -> Path:
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
        connection.executemany("insert into manifest values (?, ?)", [("fingerprint", FINGERPRINT), ("source_fingerprint", SOURCE_FINGERPRINT), ("source_content_digest", materialized.source_content_digest), ("surface_facts_digest", surface_facts_digest), ("surface_id", surface_id)])
        for scope in scopes:
            witness = asdict(scope.ready_witness)
            witness["start"] = witness["start"].isoformat()
            witness["end"] = witness["end"].isoformat()
            connection.execute("insert into scope_manifests values (?, ?, ?)", [scope.scope_key, digests[scope.scope_key], json.dumps(witness, sort_keys=True, separators=(",", ":"))])
            for fragment in scope.facts:
                encoded = encode_fragment(fragment)
                connection.execute("insert into factual_fragments values (?, ?, ?, ?, ?, ?)", [scope.scope_key, fragment.fragment_id, fragment.point.canonical_key, encoded.payload, encoded.codec, sha256(encoded.payload).hexdigest()])
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
        read_multiscope_surface(temporary)
        os.replace(temporary, target)
        return target
    finally:
        try:
            connection.close()
        except Exception:
            pass
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_multiscope_surface(path: str | Path) -> dict[str, object]:
    """Validate lineage and per-scope factual/witness binding before use."""
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
            scope = _read_scope(connection, str(scope_key), str(expected), str(witness_json))
            all_fragment_ids.extend(item.fragment_id for item in scope.facts)
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
