from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil

import duckdb
import pytest

from mrs3 import duckdb_events, duckdb_import
from mrs3.duckdb_import import (
    ImportRequest,
    discover_compact_reports,
    import_html_tree,
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
    hash_a = sha256(report.read_bytes()).hexdigest()
    assert import_html_tree(_request(tmp_path, incoming, job_id="job-a"), None).inserted == 1

    _rewrite(report, '"multiplier":"0.97"', '"multiplier":"0.9700"')
    hash_b = sha256(report.read_bytes()).hexdigest()
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
    original_hash = sha256(original.read_bytes()).hexdigest()
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
    original_hash = sha256(original.read_bytes()).hexdigest()
    import_html_tree(_request(tmp_path, initial, job_id="initial"), None)
    database = tmp_path / "source.duckdb"
    database_before = database.read_bytes()

    incoming = tmp_path / "retry"
    replacement = _copy_report(incoming, "a-replacement.html")
    _rewrite(replacement, "98.25", "98.50")
    replacement_hash = sha256(replacement.read_bytes()).hexdigest()
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
        sha256((FIXTURES / "report_b.html").read_bytes()).hexdigest(),
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
