from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import zlib
from dataclasses import replace
from hashlib import sha256

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"


def test_v2_payload_stores_facts_and_reconstructs_derivatives() -> None:
    from mrs3.source_v6 import (
        SOURCE_V6_SCHEMA_VERSION,
        _canonical_json,
        canonical_fragment_payload,
        decode_fragment,
        encode_fragment,
        normalize_and_encode_source_v6,
    )

    fragment, encoded = normalize_and_encode_source_v6(FIXTURE.read_bytes())
    document = json.loads(zlib.decompress(encoded.payload))

    assert SOURCE_V6_SCHEMA_VERSION == 2
    assert set(document) == {
        "schema_version", "point", "report_start_ms", "report_end_ms",
        "initial_balance", "fixed_order_balance", "balance_percentage",
        "settings_fingerprint", "stitchability", "actions", "wallet_samples",
        "equity_samples", "metrics",
    }
    assert json.loads(_canonical_json(canonical_fragment_payload(fragment))) == document
    decoded = decode_fragment(encoded.payload, codec=encoded.codec, expected_fragment_id=encoded.fragment_id)
    reencoded = encode_fragment(decoded)
    assert (reencoded.canonical, reencoded.fragment_id) == (encoded.canonical, encoded.fragment_id)
    assert decoded.cycles == fragment.cycles
    assert decoded.events == fragment.events
    assert decoded.open_tail_cycle_ids == fragment.open_tail_cycle_ids


def test_second_process_produces_identical_canonical_bytes_and_id() -> None:
    script = (
        "from pathlib import Path; import sys; "
        "from mrs3.source_v6 import normalize_and_encode_source_v6; "
        "_, item = normalize_and_encode_source_v6(Path(sys.argv[1]).read_bytes()); "
        "print(item.fragment_id); print(item.canonical.hex())"
    )
    command = [sys.executable, "-c", script, str(FIXTURE)]
    first = subprocess.run(command, capture_output=True, text=True, check=True)
    second = subprocess.run(command, capture_output=True, text=True, check=True)
    assert first.stdout == second.stdout


def test_v1_payload_is_rejected_before_publication() -> None:
    from mrs3.source_v6 import SourceV6Error, decode_fragment, normalize_and_encode_source_v6

    _fragment, encoded = normalize_and_encode_source_v6(FIXTURE.read_bytes())
    document = json.loads(encoded.canonical)
    document["schema_version"] = 1
    with pytest.raises(SourceV6Error, match="unsupported canonical fragment schema"):
        decode_fragment(zlib.compress(json.dumps(document, separators=(",", ":"), sort_keys=True).encode()), expected_fragment_id=None)


def test_v1_hydrated_facts_are_rejected_before_surface_publish(tmp_path: Path) -> None:
    from mrs3.source_v6 import canonical_fragment_bytes
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    from tests.test_source_v6_surface_throughput import _ready_facts

    facts = tuple(
        replace(item, schema_version=1,
                fragment_id=sha256(canonical_fragment_bytes(replace(item, schema_version=1))).hexdigest())
        for item in _ready_facts()
    )
    materialized = materialize_source_v6(facts, ("ONUSDT|LONG|1h",))
    with pytest.raises(ValueError, match="schema|v2|unsupported"):
        publish_multiscope_surface(tmp_path, materialized)
    assert not tuple(tmp_path.glob("*.surface-v6.duckdb"))


def test_v1_source_database_is_rejected_before_sql_copy_surface_publish(tmp_path: Path) -> None:
    import duckdb

    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    from tests.test_source_v6_surface_throughput import _ready_facts, _source_database

    facts = _ready_facts()
    source = _source_database(tmp_path / "source.duckdb", facts)
    connection = duckdb.connect(str(source))
    try:
        connection.execute("update schema_info set value = 'source-v6-fresh-compact-v1' where key = 'fingerprint'")
    finally:
        connection.close()
    materialized = materialize_source_v6(facts, ("ONUSDT|LONG|1h",))
    with pytest.raises((ValueError, RuntimeError), match="unsupported|fresh|fingerprint|schema"):
        publish_multiscope_surface(tmp_path / "surface", materialized, source_database=source)
    assert not tuple((tmp_path / "surface").glob("*.surface-v6.duckdb"))


def test_v1_payload_in_v2_source_database_is_rejected_before_sql_copy_publish(tmp_path: Path) -> None:
    import duckdb

    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    from tests.test_source_v6_surface_throughput import _ready_facts, _source_database

    facts = _ready_facts()
    source = _source_database(tmp_path / "source.duckdb", facts)
    connection = duckdb.connect(str(source))
    try:
        fragment_id, payload = connection.execute(
            "select fragment_id, payload_blob from compact_fragments order by fragment_id limit 1"
        ).fetchone()
        document = json.loads(zlib.decompress(bytes(payload)))
        document["schema_version"] = 1
        forged = zlib.compress(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"), 9)
        connection.execute(
            "update compact_fragments set payload_blob = ?, payload_sha256 = ? where fragment_id = ?",
            [forged, sha256(forged).hexdigest(), fragment_id],
        )
    finally:
        connection.close()
    materialized = materialize_source_v6(facts, ("ONUSDT|LONG|1h",))
    with pytest.raises(ValueError, match="payload schema|Source v6 v2"):
        publish_multiscope_surface(tmp_path / "surface", materialized, source_database=source)
    assert not tuple((tmp_path / "surface").glob("*.surface-v6.duckdb"))


@pytest.mark.parametrize("column", ["cycle_count", "event_count"])
def test_header_derived_count_mismatch_fails_closed(tmp_path: Path, column: str) -> None:
    import duckdb

    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_storage import (
        SourceV6StorageError,
        create_v6_database,
        import_fragment,
        preflight_import,
        read_fragment,
    )

    fragment = normalize_source_v6(FIXTURE.read_bytes())
    database = tmp_path / "source.duckdb"
    create_v6_database(database)
    import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))
    connection = duckdb.connect(str(database))
    try:
        connection.execute(f"update compact_fragments set {column} = {column} + 1")
    finally:
        connection.close()
    with pytest.raises(SourceV6StorageError, match="count"):
        read_fragment(database, fragment.fragment_id)


def test_header_open_tail_cache_mismatch_fails_closed(tmp_path: Path) -> None:
    import duckdb
    from hashlib import sha256

    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_storage import (
        SourceV6StorageError,
        create_v6_database,
        import_fragment,
        preflight_import,
        read_fragment,
    )

    fragment = normalize_source_v6(FIXTURE.read_bytes())
    database = tmp_path / "source.duckdb"
    create_v6_database(database)
    import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))
    connection = duckdb.connect(str(database))
    try:
        row = connection.execute("select header_json from compact_fragments where fragment_id = ?", [fragment.fragment_id]).fetchone()
        header = json.loads(row[0])
        header["open_tail_cycle_ids"] = []
        header_json = json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "update compact_fragments set header_json = ?, header_sha256 = ? where fragment_id = ?",
            [header_json, sha256(header_json.encode()).hexdigest(), fragment.fragment_id],
        )
    finally:
        connection.close()
    with pytest.raises(SourceV6StorageError, match="header"):
        read_fragment(database, fragment.fragment_id)


def test_w6_identity_readback_does_not_reconstruct_derived_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mrs3 import source_v6_storage as storage
    from mrs3 import source_v6

    fragment = source_v6.normalize_source_v6(FIXTURE.read_bytes())
    database = tmp_path / "source.duckdb"
    storage.create_v6_database(database)
    storage.import_fragment(database, fragment, preflight_token=storage.preflight_import(database, fragment))

    def fail_reconstruction(_document: object) -> object:
        raise AssertionError("W6 identity validation must not reconstruct derived facts")

    monkeypatch.setattr(source_v6, "_fragment_from_payload", fail_reconstruction)
    storage.verify_published_identity_parallel(database)


def test_database_materialization_rejects_in_range_cycle_count_tampering(
    tmp_path: Path,
) -> None:
    import duckdb

    from mrs3.source_v6_materializer import materialize_source_v6_from_database
    from mrs3.source_v6_storage import SourceV6StorageError
    from tests.test_source_v6_surface_throughput import _ready_facts, _source_database

    facts = _ready_facts()
    source = _source_database(tmp_path / "source.duckdb", facts)
    connection = duckdb.connect(str(source))
    try:
        fragment_id, cycle_count, action_count = connection.execute(
            "select fragment_id, cycle_count, action_count from compact_fragments "
            "where cycle_count < action_count order by fragment_id limit 1"
        ).fetchone()
        assert int(cycle_count) + 1 <= int(action_count)
        connection.execute(
            "update compact_fragments set cycle_count = cycle_count + 1 where fragment_id = ?",
            [fragment_id],
        )
    finally:
        connection.close()
    with pytest.raises(SourceV6StorageError, match="count|readback"):
        materialize_source_v6_from_database(source, ("ONUSDT|LONG|1h",))


def test_w6_rejects_malformed_open_tail_cache_as_storage_error(tmp_path: Path) -> None:
    import duckdb

    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_storage import (
        SourceV6StorageError,
        create_v6_database,
        import_fragment,
        preflight_import,
        verify_fragment_slice,
    )

    fragment = normalize_source_v6(FIXTURE.read_bytes())
    database = tmp_path / "source.duckdb"
    create_v6_database(database)
    import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))
    connection = duckdb.connect(str(database))
    try:
        row = connection.execute(
            "select header_json from compact_fragments where fragment_id = ?",
            [fragment.fragment_id],
        ).fetchone()
        header = json.loads(row[0])
        header["open_tail_cycle_ids"] = ["not-a-sha256"]
        header_json = json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "update compact_fragments set header_json = ?, header_sha256 = ? where fragment_id = ?",
            [header_json, sha256(header_json.encode()).hexdigest(), fragment.fragment_id],
        )
    finally:
        connection.close()
    with pytest.raises(SourceV6StorageError, match="readback mismatch"):
        verify_fragment_slice(database, [fragment.fragment_id])
