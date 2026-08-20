from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from decimal import Decimal

import duckdb
import pytest

import mrs3.source_v6_storage as storage
from mrs3.source_v6 import normalize_source_v6
from mrs3.source_v6_storage import (
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
)


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"


def _fragment():
    return normalize_source_v6(FIXTURE.read_bytes(), source_name="report.html")


def _fragment_b():
    return normalize_source_v6((FIXTURE.parent / "source_v6_fixed_lot_overlap_b.html").read_bytes(), source_name="report-b.html")


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
