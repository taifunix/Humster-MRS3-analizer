from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
from threading import Barrier, Event, Thread

import duckdb
import pytest

from mrs3 import duckdb_events, duckdb_import
from mrs3.duckdb_import import (
    ImportRequest,
    discover_compact_reports,
    import_html_tree,
    preflight_html_import,
)
from mrs3.duckdb_source_schema import ensure_source_schema


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "duckdb_import"


def _copy_report(root: Path, relative_path: str, fixture: str = "report_a.html") -> Path:
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / fixture, target)
    return target


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in discover_compact_reports(root)
    }


def _codec_source_hash(path: Path) -> str:
    return sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _newline_variants(path: Path) -> tuple[bytes, bytes]:
    lf = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return lf, lf.replace(b"\n", b"\r\n")


def _request(
    tmp_path: Path,
    incoming: Path,
    *,
    database_name: str = "source.duckdb",
    audit_name: str = "audit",
    job_id: str = "job-1",
    batch_size: int = 50,
    cancellation_requested=None,
) -> ImportRequest:
    return ImportRequest(
        root_path=incoming,
        database_path=tmp_path / database_name,
        audit_root=tmp_path / audit_name,
        workers=2,
        transaction_batch_size=batch_size,
        job_id=job_id,
        cancellation_requested=cancellation_requested,
    )


def _active_rows(database: Path) -> list[tuple[str, str, str]]:
    connection = duckdb.connect(str(database), read_only=True)
    try:
        return connection.execute(
            "select canonical_report_key,source_sha256,source_file "
            "from active_reports order by canonical_report_key"
        ).fetchall()
    finally:
        connection.close()


def test_discovery_is_recursive_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "incoming"
    shallow = root / "z-report.HTML"
    middle = root / "a" / "middle.html"
    deep = root / "a" / "nested" / "deep.Html"
    ignored = root / "a" / "nested" / "notes.txt"
    for target, source in (
        (shallow, FIXTURES / "report_a.html"),
        (middle, FIXTURES / "report_b.html"),
        (deep, FIXTURES / "report_a.html"),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    ignored.write_text("not html", encoding="utf-8")

    expected = (middle, deep, shallow)
    assert discover_compact_reports(root) == expected
    assert discover_compact_reports(root) == expected


def test_insert_identical_period_and_shift_append_emit_complete_progress(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    original = _copy_report(incoming, "nested/original.html")
    before = _snapshot(incoming)
    progress = []

    first = import_html_tree(_request(tmp_path, incoming), progress.append)

    assert first.final_state == "COMMITTED"
    assert (first.parsed, first.inserted, first.replaced, first.identical) == (1, 1, 0, 0)
    assert (first.ambiguous, first.quarantined) == (0, 0)
    assert first.safe_to_delete == "YES"
    assert _snapshot(incoming) == before
    assert progress[-1].final_state == "COMMITTED"
    assert progress[-1].counts == {
        "parsed": 1,
        "inserted": 1,
        "replaced": 0,
        "identical": 0,
        "ambiguous": 0,
        "quarantined": 0,
    }

    second = import_html_tree(
        _request(tmp_path, incoming, job_id="job-2"), progress.append
    )
    assert (second.inserted, second.replaced, second.identical) == (0, 0, 1)

    _copy_report(incoming, "period/new-period.html")
    _rewrite(incoming / "period/new-period.html", "2024-01-01", "2024-01-02")
    for old, new in (
        ("1704067200000", "1704153600000"),
        ("1704070800000", "1704157200000"),
        ("1704074400000", "1704160800000"),
    ):
        _rewrite(incoming / "period/new-period.html", old, new)
    _copy_report(incoming, "shift/new-shift.html")
    _rewrite(incoming / "shift/new-shift.html", '"multiplier":"0.97"', '"multiplier":"0.96"')
    third_before = _snapshot(incoming)

    third = import_html_tree(
        _request(tmp_path, incoming, job_id="job-3"), progress.append
    )

    assert (third.parsed, third.inserted, third.replaced, third.identical) == (3, 2, 0, 1)
    assert len(_active_rows(tmp_path / "source.duckdb")) == 3
    assert _snapshot(incoming) == third_before
    assert original.is_file()


def test_canonical_multiplier_replacement_and_a_to_b_to_a_append_audit(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    report = _copy_report(incoming, "report.html")
    hash_a = _codec_source_hash(report)
    assert import_html_tree(_request(tmp_path, incoming, job_id="job-a"), None).inserted == 1

    _rewrite(report, '"multiplier":"0.97"', '"multiplier":"0.9700"')
    hash_b = _codec_source_hash(report)
    replaced = import_html_tree(_request(tmp_path, incoming, job_id="job-b"), None)
    assert replaced.replaced == 1
    assert _active_rows(tmp_path / "source.duckdb")[0][1] == hash_b

    shutil.copyfile(FIXTURES / "report_a.html", report)
    restored = import_html_tree(_request(tmp_path, incoming, job_id="job-c"), None)

    assert restored.replaced == 1
    assert restored.identical == 0
    assert _active_rows(tmp_path / "source.duckdb")[0][1] == hash_a
    connection = duckdb.connect(str(tmp_path / "source.duckdb"), read_only=True)
    try:
        assert connection.execute(
            "select old_source_sha256,new_source_sha256,job_id "
            "from replacement_history order by imported_at_utc,audit_id"
        ).fetchall() == [(hash_a, hash_b, "job-b"), (hash_b, hash_a, "job-c")]
    finally:
        connection.close()


def _assert_failed_evidence_finalized(result) -> None:
    manifest_bytes = result.manifest_path.read_bytes()
    checklist_bytes = result.checklist_path.read_bytes()
    assert result.manifest_sha256 == sha256(manifest_bytes).hexdigest()
    assert result.checklist_sha256 == sha256(checklist_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    checklist = json.loads(checklist_bytes)
    assert manifest["final_state"] == "FAILED"
    assert manifest["safe_to_delete"] == "NO"
    assert checklist["safe_to_delete"] == "NO"


def test_crlf_input_keeps_raw_manifest_hash_but_uses_v3_semantic_source_identity(
    tmp_path: Path,
) -> None:
    crlf_root = tmp_path / "crlf"
    crlf_report = _copy_report(crlf_root, "report.html")
    lf_bytes, crlf_bytes = _newline_variants(crlf_report)
    crlf_report.write_bytes(crlf_bytes)
    expected_source_hash = sha256(
        crlf_report.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    raw_input_hash = sha256(crlf_bytes).hexdigest()
    assert expected_source_hash != raw_input_hash

    first = import_html_tree(_request(tmp_path, crlf_root, job_id="crlf"), None)

    assert first.final_state == "COMMITTED"
    assert _active_rows(tmp_path / "source.duckdb")[0][1] == expected_source_hash
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["reports"][0]["input_sha256"] == raw_input_hash

    lf_root = tmp_path / "lf"
    lf_report = _copy_report(lf_root, "report.html")
    lf_report.write_bytes(lf_bytes)
    lf_input_hash = sha256(lf_bytes).hexdigest()
    assert lf_input_hash != raw_input_hash
    second = import_html_tree(_request(tmp_path, lf_root, job_id="lf"), None)

    assert (second.inserted, second.replaced, second.identical, second.ambiguous) == (0, 0, 1, 0)
    assert _active_rows(tmp_path / "source.duckdb")[0][1] == expected_source_hash
    second_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert second_manifest["reports"][0]["input_sha256"] == lf_input_hash


def test_same_batch_crlf_and_lf_are_identical_not_ambiguous(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    crlf_report = _copy_report(incoming, "a-crlf.html")
    lf_bytes, crlf_bytes = _newline_variants(crlf_report)
    crlf_report.write_bytes(crlf_bytes)
    lf_report = _copy_report(incoming, "b-lf.html")
    lf_report.write_bytes(lf_bytes)
    assert sha256(crlf_bytes).hexdigest() != sha256(lf_bytes).hexdigest()

    result = import_html_tree(_request(tmp_path, incoming, job_id="mixed-newlines"), None)

    assert result.final_state == "COMMITTED"
    assert (result.inserted, result.identical, result.ambiguous) == (1, 1, 0)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert {
        item["relative_path"]: item["classification"] for item in manifest["reports"]
    } == {
        "a-crlf.html": "INSERTED",
        "b-lf.html": "SKIPPED_BATCH_IDENTICAL",
    }


def test_crlf_to_lf_mutation_during_parse_prevents_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = tmp_path / "initial"
    original = _copy_report(initial, "report.html")
    original_hash = _codec_source_hash(original)
    import_html_tree(_request(tmp_path, initial, job_id="initial"), None)
    database = tmp_path / "source.duckdb"
    database_before = database.read_bytes()

    incoming = tmp_path / "incoming"
    changed = _copy_report(incoming, "report.html")
    _rewrite(changed, "98.25", "98.50")
    lf_bytes, crlf_bytes = _newline_variants(changed)
    changed.write_bytes(crlf_bytes)
    raw_snapshot_hash = sha256(crlf_bytes).hexdigest()
    assert raw_snapshot_hash != sha256(lf_bytes).hexdigest()
    real_reader = duckdb_events.read_compact_html

    def mutate_after_parse(path: Path):
        outcome = real_reader(path)
        path.write_bytes(lf_bytes)
        return outcome

    monkeypatch.setattr(duckdb_events, "read_compact_html", mutate_after_parse)
    result = import_html_tree(_request(tmp_path, incoming, job_id="mutated"), None)

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert (result.inserted, result.replaced) == (0, 0)
    assert database.read_bytes() == database_before
    assert _active_rows(database)[0][1] == original_hash
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["reports"][0]["input_sha256"] == raw_snapshot_hash
    assert manifest["reports"][0]["classification"] == "NOT_IMPORTED_FAILURE"


def test_invalid_utf8_is_quarantined_with_raw_hash_and_no_active_report(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    invalid = _copy_report(incoming, "invalid.html")
    invalid_bytes = invalid.read_bytes() + b"\xff"
    invalid.write_bytes(invalid_bytes)
    raw_input_hash = sha256(invalid_bytes).hexdigest()

    result = import_html_tree(_request(tmp_path, incoming, job_id="invalid-utf8"), None)

    assert (result.parsed, result.quarantined) == (0, 1)
    assert result.safe_to_delete == "NO"
    assert _active_rows(tmp_path / "source.duckdb") == []
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["reports"][0]["input_sha256"] == raw_input_hash
    assert manifest["reports"][0]["classification"] == "INVALID_REPORT"


def test_manifest_and_checklist_are_deterministic_complete_and_hashed(tmp_path: Path) -> None:
    manifests: list[bytes] = []
    checklists: list[bytes] = []
    for suffix in ("one", "two"):
        incoming = tmp_path / suffix / "incoming"
        _copy_report(incoming, "b/report-b.html", "report_b.html")
        _copy_report(incoming, "a/report-a.html")
        result = import_html_tree(
            _request(
                tmp_path,
                incoming,
                database_name=f"{suffix}.duckdb",
                audit_name=f"audit-{suffix}",
                job_id="deterministic-job",
            ),
            None,
        )
        manifest_bytes = result.manifest_path.read_bytes()
        checklist_bytes = result.checklist_path.read_bytes()
        manifests.append(manifest_bytes)
        checklists.append(checklist_bytes)
        assert result.manifest_sha256 == sha256(manifest_bytes).hexdigest()
        assert result.checklist_sha256 == sha256(checklist_bytes).hexdigest()
        manifest = json.loads(manifest_bytes)
        checklist = json.loads(checklist_bytes)
        assert [item["relative_path"] for item in manifest["reports"]] == [
            "a/report-a.html",
            "b/report-b.html",
        ]
        assert all(item["input_sha256"] for item in manifest["reports"])
        assert manifest["compact_import_schema_version"] == 4
        assert manifest["source_database_schema_version"] == 5
        assert manifest["artifacts"]["checklist"]["sha256"] == result.checklist_sha256
        assert manifest["counts"] == {
            "discovered": 2,
            "parsed": 2,
            "inserted": 2,
            "replaced": 0,
            "identical": 0,
            "ambiguous": 0,
            "quarantined": 0,
        }
        assert all(item["parity"] == "PASS" for item in checklist["reports"])
        assert all(item["validation"] == "PASS" for item in checklist["reports"])
        assert all(item["safe_to_delete"] == "YES" for item in checklist["reports"])
    assert manifests[0] == manifests[1]
    assert checklists[0] == checklists[1]


def test_same_batch_different_payloads_are_ambiguous_and_do_not_replace(
    tmp_path: Path,
) -> None:
    initial = tmp_path / "initial"
    original = _copy_report(initial, "report.html")
    original_hash = _codec_source_hash(original)
    import_html_tree(_request(tmp_path, initial, job_id="initial"), None)

    incoming = tmp_path / "ambiguous"
    _copy_report(incoming, "a.html")
    changed = _copy_report(incoming, "nested/b.html")
    _rewrite(changed, "98.25", "98.50")
    before = _snapshot(incoming)

    result = import_html_tree(_request(tmp_path, incoming, job_id="ambiguous"), None)

    assert result.final_state == "FAILED"
    assert (result.ambiguous, result.inserted, result.replaced) == (2, 0, 0)
    assert result.safe_to_delete == "NO"
    assert _active_rows(tmp_path / "source.duckdb")[0][1] == original_hash
    assert _snapshot(incoming) == before
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert {item["classification"] for item in manifest["reports"]} == {
        "AMBIGUOUS_BATCH_DUPLICATE"
    }


def test_mixed_ambiguous_and_valid_batch_publishes_no_writes(tmp_path: Path) -> None:
    initial = tmp_path / "initial"
    _copy_report(initial, "report.html")
    import_html_tree(_request(tmp_path, initial, job_id="initial"), None)
    database = tmp_path / "source.duckdb"
    database_before = database.read_bytes()

    incoming = tmp_path / "mixed"
    _copy_report(incoming, "a/original.html")
    changed = _copy_report(incoming, "a/changed.html")
    _rewrite(changed, "98.25", "98.50")
    _copy_report(incoming, "b/valid.html", "report_b.html")
    inputs_before = _snapshot(incoming)

    result = import_html_tree(
        _request(tmp_path, incoming, job_id="mixed-ambiguity"), None
    )

    assert result.final_state == "FAILED"
    assert (result.ambiguous, result.inserted, result.replaced) == (2, 0, 0)
    assert result.safe_to_delete == "NO"
    assert database.read_bytes() == database_before
    assert len(_active_rows(database)) == 1
    assert _snapshot(incoming) == inputs_before
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["final_state"] == "FAILED"
    assert {
        item["classification"] for item in manifest["reports"]
    } == {"AMBIGUOUS_BATCH_DUPLICATE", "NOT_IMPORTED_FAILURE"}


def test_ambiguous_only_batch_does_not_create_a_new_target(tmp_path: Path) -> None:
    incoming = tmp_path / "ambiguous"
    _copy_report(incoming, "original.html")
    changed = _copy_report(incoming, "nested/changed.html")
    _rewrite(changed, "98.25", "98.50")
    inputs_before = _snapshot(incoming)
    database = tmp_path / "source.duckdb"

    result = import_html_tree(
        _request(tmp_path, incoming, job_id="new-ambiguity"), None
    )

    assert result.final_state == "FAILED"
    assert (result.ambiguous, result.inserted, result.replaced) == (2, 0, 0)
    assert result.safe_to_delete == "NO"
    assert not database.exists()
    assert _snapshot(incoming) == inputs_before


def test_identical_files_share_one_active_hash_without_ambiguity(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "one.html")
    _copy_report(incoming, "nested/two.html")
    before = _snapshot(incoming)

    result = import_html_tree(_request(tmp_path, incoming), None)

    assert (result.inserted, result.identical, result.ambiguous) == (1, 1, 0)
    assert len(_active_rows(tmp_path / "source.duckdb")) == 1
    assert _snapshot(incoming) == before


def test_parser_and_payload_integrity_failures_are_quarantine_without_file_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed_root = tmp_path / "malformed"
    malformed = _copy_report(malformed_root, "bad.html")
    _rewrite(malformed, "equitySeries", "missingSeries")
    malformed_before = _snapshot(malformed_root)

    malformed_result = import_html_tree(
        _request(tmp_path, malformed_root, database_name="malformed.duckdb", job_id="malformed"),
        None,
    )

    assert (malformed_result.parsed, malformed_result.quarantined) == (0, 1)
    assert malformed_result.safe_to_delete == "NO"
    assert _snapshot(malformed_root) == malformed_before

    corrupt_root = tmp_path / "corrupt"
    _copy_report(corrupt_root, "corrupt.html")
    corrupt_before = _snapshot(corrupt_root)
    real_reader = duckdb_events.read_compact_html

    def corrupt_reader(path: Path):
        outcome = real_reader(path)
        return replace(outcome, record=replace(outcome.record, equity_zlib=b"not-zlib"))

    monkeypatch.setattr(duckdb_events, "read_compact_html", corrupt_reader)
    corrupt_result = import_html_tree(
        _request(tmp_path, corrupt_root, database_name="corrupt.duckdb", job_id="corrupt"),
        None,
    )

    assert (corrupt_result.parsed, corrupt_result.quarantined) == (1, 1)
    assert corrupt_result.safe_to_delete == "NO"
    assert _snapshot(corrupt_root) == corrupt_before


def test_cancellation_writes_no_database_and_marks_all_evidence_unsafe(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    before = _snapshot(incoming)

    result = import_html_tree(
        _request(tmp_path, incoming, job_id="cancelled", cancellation_requested=lambda: True),
        None,
    )

    assert result.final_state == "CANCELLED"
    assert result.safe_to_delete == "NO"
    assert not (tmp_path / "source.duckdb").exists()
    assert _snapshot(incoming) == before
    checklist = json.loads(result.checklist_path.read_text(encoding="utf-8"))
    assert checklist["safe_to_delete"] == "NO"
    assert checklist["reports"][0]["safe_to_delete"] == "NO"


def test_transaction_failure_keeps_previous_active_report_and_retry_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = tmp_path / "initial"
    original = _copy_report(initial, "report.html")
    original_hash = _codec_source_hash(original)
    import_html_tree(_request(tmp_path, initial, job_id="initial"), None)
    database = tmp_path / "source.duckdb"
    database_before = database.read_bytes()

    incoming = tmp_path / "retry"
    replacement = _copy_report(incoming, "a-replacement.html")
    _rewrite(replacement, "98.25", "98.50")
    replacement_hash = _codec_source_hash(replacement)
    _copy_report(incoming, "b-insert.html", "report_b.html")
    inputs_before = _snapshot(incoming)
    real_write = duckdb_import._write_decision
    calls = 0

    def fail_second_write(connection, decision, job_id, imported_at):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise duckdb.TransactionException("injected batch failure")
        return real_write(connection, decision, job_id, imported_at)

    monkeypatch.setattr(duckdb_import, "_write_decision", fail_second_write)
    failed = import_html_tree(
        _request(tmp_path, incoming, job_id="failed", batch_size=1), None
    )

    assert failed.final_state == "FAILED"
    assert failed.safe_to_delete == "NO"
    assert (failed.inserted, failed.replaced) == (0, 0)
    failed_manifest = json.loads(failed.manifest_path.read_text(encoding="utf-8"))
    assert not {"INSERTED", "REPLACED"}.intersection(
        item["classification"] for item in failed_manifest["reports"]
    )
    assert database.read_bytes() == database_before
    assert _active_rows(database)[0][1] == original_hash
    assert _snapshot(incoming) == inputs_before

    monkeypatch.setattr(duckdb_import, "_write_decision", real_write)
    retry = import_html_tree(
        _request(tmp_path, incoming, job_id="retry", batch_size=2), None
    )

    assert retry.final_state == "COMMITTED"
    assert (retry.inserted, retry.replaced, retry.identical) == (1, 1, 0)
    assert {row[1] for row in _active_rows(database)} == {
        replacement_hash,
        _codec_source_hash(FIXTURES / "report_b.html"),
    }
    assert _snapshot(incoming) == inputs_before


def test_cancellation_after_a_stage_batch_does_not_report_unpublished_writes(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "a.html")
    _copy_report(incoming, "b.html", "report_b.html")
    before = _snapshot(incoming)
    checks = 0

    def cancel_before_second_batch() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    result = import_html_tree(
        _request(
            tmp_path,
            incoming,
            job_id="cancel-after-stage",
            batch_size=1,
            cancellation_requested=cancel_before_second_batch,
        ),
        None,
    )

    assert result.final_state == "CANCELLED"
    assert (result.inserted, result.replaced) == (0, 0)
    assert not (tmp_path / "source.duckdb").exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert not {"INSERTED", "REPLACED"}.intersection(
        item["classification"] for item in manifest["reports"]
    )
    assert _snapshot(incoming) == before


def test_incompatible_normalization_contract_fails_before_database_publication(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    database = tmp_path / "source.duckdb"
    connection = duckdb.connect(str(database))
    try:
        ensure_source_schema(connection)
        connection.execute(
            "update schema_info set value='incompatible' "
            "where key='normalization_contract_version'"
        )
    finally:
        connection.close()
    database_before = database.read_bytes()

    result = import_html_tree(_request(tmp_path, incoming, job_id="bad-contract"), None)

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert "normalization contract" in (result.error or "")
    assert database.read_bytes() == database_before


def test_concurrent_import_to_same_resolved_database_is_rejected_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    entered_parse = Event()
    allow_parse = Event()
    real_reader = duckdb_events.read_compact_html

    def blocked_reader(path: Path):
        entered_parse.set()
        assert allow_parse.wait(timeout=5)
        return real_reader(path)

    monkeypatch.setattr(duckdb_events, "read_compact_html", blocked_reader)
    first_result = []
    first = Thread(
        target=lambda: first_result.append(
            import_html_tree(_request(tmp_path, incoming, job_id="first"), None)
        )
    )
    first.start()
    assert entered_parse.wait(timeout=5)

    second = import_html_tree(_request(tmp_path, incoming, job_id="second"), None)

    assert second.final_state == "FAILED"
    assert second.safe_to_delete == "NO"
    assert "already being written" in (second.error or "")
    assert not (tmp_path / "source.duckdb").exists()
    _assert_failed_evidence_finalized(second)
    allow_parse.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert first_result[0].final_state == "COMMITTED"


def test_busy_invalid_target_is_not_opened_before_lock_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    database = tmp_path / "source.duckdb"
    invalid_target = b"invalid target owned by another writer"
    database.write_bytes(invalid_target)
    request = _request(tmp_path, incoming, job_id="busy-invalid")

    def reject_database_open(*args, **kwargs):
        raise AssertionError("busy target must not be opened")

    with duckdb_import._source_database_lock(database):
        monkeypatch.setattr(duckdb, "connect", reject_database_open)
        result = import_html_tree(request, None)

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert "already being written" in (result.error or "")
    assert database.read_bytes() == invalid_target
    _assert_failed_evidence_finalized(result)


def test_symlink_alias_to_same_database_is_rejected_as_second_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    try:
        alias_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    entered_parse = Event()
    allow_parse = Event()
    real_reader = duckdb_events.read_compact_html

    def blocked_reader(path: Path):
        entered_parse.set()
        assert allow_parse.wait(timeout=5)
        return real_reader(path)

    monkeypatch.setattr(duckdb_events, "read_compact_html", blocked_reader)
    real_request = replace(
        _request(tmp_path, incoming, job_id="real-path"),
        database_path=real_parent / "source.duckdb",
    )
    alias_request = replace(
        _request(tmp_path, incoming, job_id="alias-path"),
        database_path=alias_parent / "source.duckdb",
    )
    first_result = []
    first = Thread(
        target=lambda: first_result.append(import_html_tree(real_request, None))
    )
    first.start()
    assert entered_parse.wait(timeout=5)

    second = import_html_tree(alias_request, None)

    assert second.final_state == "FAILED"
    assert "already being written" in (second.error or "")
    assert not (real_parent / "source.duckdb").exists()
    _assert_failed_evidence_finalized(second)
    allow_parse.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert first_result[0].final_state == "COMMITTED"


def test_writer_lock_is_released_when_evidence_finalization_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    request = _request(
        tmp_path,
        incoming,
        job_id="evidence-error",
        cancellation_requested=lambda: True,
    )
    real_write_evidence = duckdb_import._write_evidence

    def fail_evidence(*args, **kwargs):
        raise RuntimeError("injected evidence failure")

    monkeypatch.setattr(duckdb_import, "_write_evidence", fail_evidence)
    with pytest.raises(RuntimeError, match="injected evidence failure"):
        import_html_tree(request, None)

    monkeypatch.setattr(duckdb_import, "_write_evidence", real_write_evidence)
    retry = import_html_tree(
        _request(tmp_path, incoming, job_id="after-evidence-error"), None
    )

    assert retry.final_state == "COMMITTED"


def test_existing_target_changed_after_preflight_is_preserved(tmp_path: Path) -> None:
    initial = tmp_path / "initial"
    _copy_report(initial, "report.html")
    import_html_tree(_request(tmp_path, initial, job_id="initial"), None)
    database = tmp_path / "source.duckdb"
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "replacement.html")
    _rewrite(incoming / "replacement.html", "98.25", "98.50")
    external_bytes = b"externally changed target"

    def replace_target_after_staging(progress) -> None:
        if progress.inserted + progress.replaced:
            database.write_bytes(external_bytes)

    result = import_html_tree(
        _request(tmp_path, incoming, job_id="stale-existing"), replace_target_after_staging
    )

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert "source database changed during import" in (result.error or "")
    assert database.read_bytes() == external_bytes
    _assert_failed_evidence_finalized(result)


def test_target_appearing_after_absent_preflight_is_preserved(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    database = tmp_path / "source.duckdb"
    external_bytes = b"target appeared after preflight"

    def create_target_after_staging(progress) -> None:
        if progress.inserted + progress.replaced:
            database.write_bytes(external_bytes)

    result = import_html_tree(
        _request(tmp_path, incoming, job_id="stale-absent"), create_target_after_staging
    )

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert "source database changed during import" in (result.error or "")
    assert database.read_bytes() == external_bytes
    _assert_failed_evidence_finalized(result)


def test_preflight_is_deterministic_and_authorizes_unchanged_import(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "nested/report.html")
    request = _request(tmp_path, incoming, job_id="preflight-success")

    first = preflight_html_import(request)
    second = preflight_html_import(request)
    result = import_html_tree(replace(request, expected_preflight_token=first.token), None)

    assert first == second
    assert first.discovered == 1
    assert first.source_schema_version is None
    assert result.final_state == "COMMITTED"


def test_preflight_snapshots_in_parallel_and_reports_byte_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "a/report.html")
    _copy_report(incoming, "b/report.html", "report_b.html")
    request = _request(tmp_path, incoming)
    gate = Barrier(2)
    original = duckdb_import._snapshot_one

    def gated(root: Path, path: Path):
        gate.wait(timeout=2)
        return original(root, path)

    monkeypatch.setattr(duckdb_import, "_snapshot_one", gated)
    progress = []
    preflight = preflight_html_import(request, progress.append)

    assert preflight.discovered == 2
    assert [item.snapshotted for item in progress] == [0, 1, 2]
    assert progress[0].discovered == 2 and progress[0].total_bytes > 0
    assert progress[-1].processed_bytes == progress[-1].total_bytes > 0


def test_changed_input_after_preflight_is_rejected_without_target_mutation(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    report = _copy_report(incoming, "report.html")
    request = _request(tmp_path, incoming, job_id="stale-input")
    preflight = preflight_html_import(request)
    _rewrite(report, "98.25", "98.50")

    result = import_html_tree(replace(request, expected_preflight_token=preflight.token), None)

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert "preflight" in (result.error or "")
    assert not (tmp_path / "source.duckdb").exists()
    _assert_failed_evidence_finalized(result)


def test_changed_existing_target_after_preflight_is_rejected_without_replacement(tmp_path: Path) -> None:
    initial = tmp_path / "initial"
    _copy_report(initial, "report.html")
    import_html_tree(_request(tmp_path, initial, job_id="initial"), None)
    database = tmp_path / "source.duckdb"
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "replacement.html")
    request = _request(tmp_path, incoming, job_id="stale-target")
    preflight = preflight_html_import(request)
    external_bytes = b"external target mutation"
    database.write_bytes(external_bytes)

    result = import_html_tree(replace(request, expected_preflight_token=preflight.token), None)

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert database.read_bytes() == external_bytes
    _assert_failed_evidence_finalized(result)


def test_target_appearing_after_absent_preflight_is_rejected(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    request = _request(tmp_path, incoming, job_id="appeared-target")
    preflight = preflight_html_import(request)
    database = tmp_path / "source.duckdb"
    external_bytes = b"external target appeared"
    database.write_bytes(external_bytes)

    result = import_html_tree(replace(request, expected_preflight_token=preflight.token), None)

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert database.read_bytes() == external_bytes
    _assert_failed_evidence_finalized(result)


def test_preflight_rejects_invalid_existing_source_database(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    database = tmp_path / "source.duckdb"
    database.write_bytes(b"not a duckdb database")

    with pytest.raises(ValueError, match="source database validation failed"):
        preflight_html_import(_request(tmp_path, incoming))


def test_preflight_public_representation_does_not_leak_local_paths(tmp_path: Path) -> None:
    incoming = tmp_path / "private-root"
    _copy_report(incoming, "nested/report.html")

    preflight = preflight_html_import(_request(tmp_path, incoming))

    rendered = repr(preflight)
    assert str(tmp_path) not in rendered
    assert "private-root" not in rendered


def test_target_changed_between_token_check_and_staging_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = tmp_path / "initial"
    _copy_report(initial, "report.html")
    import_html_tree(_request(tmp_path, initial, job_id="initial"), None)
    database = tmp_path / "source.duckdb"
    external_root = tmp_path / "external"
    _copy_report(external_root, "report-b.html", "report_b.html")
    external_database = tmp_path / "external.duckdb"
    import_html_tree(
        replace(
            _request(tmp_path, external_root, job_id="external"),
            database_path=external_database,
        ),
        None,
    )
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "replacement.html")
    request = _request(tmp_path, incoming, job_id="boundary-target")
    preflight = preflight_html_import(request)
    external_bytes = external_database.read_bytes()
    real_reader = duckdb_events.read_compact_html

    def mutate_target_after_token_check(path: Path):
        shutil.copyfile(external_database, database)
        return real_reader(path)

    monkeypatch.setattr(duckdb_events, "read_compact_html", mutate_target_after_token_check)
    result = import_html_tree(replace(request, expected_preflight_token=preflight.token), None)

    assert result.final_state == "FAILED"
    assert database.read_bytes() == external_bytes
    _assert_failed_evidence_finalized(result)


def test_preflight_token_is_bound_to_resolved_target_path(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "report.html")
    request = _request(tmp_path, incoming, job_id="other-target")
    preflight = preflight_html_import(request)
    other_target = tmp_path / "other.duckdb"

    result = import_html_tree(
        replace(request, database_path=other_target, expected_preflight_token=preflight.token),
        None,
    )

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert not other_target.exists()
    _assert_failed_evidence_finalized(result)


def test_invalid_target_after_token_validation_finalizes_unsafe_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = tmp_path / "initial"
    _copy_report(initial, "report.html")
    import_html_tree(_request(tmp_path, initial, job_id="initial"), None)
    database = tmp_path / "source.duckdb"
    incoming = tmp_path / "incoming"
    _copy_report(incoming, "replacement.html")
    request = _request(tmp_path, incoming, job_id="invalid-after-token")
    preflight = preflight_html_import(request)
    external_bytes = b"invalid mutation after token validation"
    real_preflight = duckdb_import._preflight_from_snapshots

    def mutate_target_after_preflight(*args, **kwargs):
        result = real_preflight(*args, **kwargs)
        database.write_bytes(external_bytes)
        return result

    monkeypatch.setattr(duckdb_import, "_preflight_from_snapshots", mutate_target_after_preflight)
    result = import_html_tree(replace(request, expected_preflight_token=preflight.token), None)

    assert result.final_state == "FAILED"
    assert result.safe_to_delete == "NO"
    assert database.read_bytes() == external_bytes
    _assert_failed_evidence_finalized(result)
