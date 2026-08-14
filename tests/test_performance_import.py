from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from mrs3.performance_import import (
    PerformanceImportError,
    PerformanceImportRequest,
    import_performance_batch,
    resume_performance_cleanup,
)
from mrs3.performance_store import initialize_performance_database


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "report_import.html"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _request(tmp_path: Path) -> PerformanceImportRequest:
    inbox = tmp_path / "inbox"
    reports = inbox / "reports"
    strategies = inbox / "strategies"
    reports.mkdir(parents=True)
    strategies.mkdir()
    report = FIXTURE.read_bytes()
    strategy = {"name": "MRS3 Demo", "exchange": {"name": "Bybit"}, "settings": []}
    strategy_id = hashlib.sha256(_canonical(strategy)).hexdigest()
    (strategies / f"{strategy_id}.json").write_bytes(_canonical(strategy))
    entry_id = "entry-1"
    (reports / f"{entry_id}.html").write_bytes(report)
    contract = {
        "MakerFee": "0.0002", "TakerFee": "0.0004", "SlippagePercent": "0.01",
        "FundingRate": "0.0001", "FundingIntervalHours": "8",
    }
    manifest = {
        "schema_version": 1, "batch_id": "batch-1", "expected_strategy_names": ["MRS3 Demo"],
        "tester_config_sha256": "config-hash",
        "commission_contract": contract,
        "commission_contract_id": hashlib.sha256(_canonical(contract)).hexdigest(),
        "entries": [{
            "manifest_entry_id": entry_id, "strategy_name": "MRS3 Demo",
            "strategy_version_id": strategy_id,
            "strategy_path": f"strategies/{strategy_id}.json",
            "report_path": f"reports/{entry_id}.html", "wizard_run_id": "run-1",
            "exchange_name": "Bybit",
            "source_strategy_sha256": hashlib.sha256((strategies / f"{strategy_id}.json").read_bytes()).hexdigest(),
            "source_report_sha256": hashlib.sha256(report).hexdigest(),
        }],
    }
    (inbox / "inbox_manifest.json").write_bytes(_canonical(manifest))
    database = tmp_path / "strategy_performance.duckdb"
    initialize_performance_database(database)
    return PerformanceImportRequest(inbox, database)


def _count(database: Path, table: str) -> int:
    with duckdb.connect(str(database), read_only=True) as connection:
        return connection.execute(f"select count(*) from {table}").fetchone()[0]


def test_identical_payload_skips_without_second_run(tmp_path: Path) -> None:
    request = _request(tmp_path)

    first = import_performance_batch(request)
    second = import_performance_batch(request)

    assert first.imported_count == 1
    assert second.skipped_count == 1
    assert _count(request.database, "backtest_runs") == 1


def test_conflict_keeps_html_and_database_unchanged(tmp_path: Path) -> None:
    request = _request(tmp_path)
    import_performance_batch(request)
    report = request.inbox / "reports" / "entry-1.html"
    report.write_bytes(report.read_bytes().replace(b"<td>Total PnL</td><td>12.5</td>", b"<td>Total PnL</td><td>99</td>"))
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["source_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))

    with pytest.raises(PerformanceImportError, match="IDENTITY_CONFLICT"):
        import_performance_batch(request)
    assert report.is_file()
    assert _count(request.database, "backtest_runs") == 1


def test_malformed_report_is_quarantined_and_audited_without_deletion(tmp_path: Path) -> None:
    request = _request(tmp_path)
    report = request.inbox / "reports" / "entry-1.html"
    report.write_bytes(report.read_bytes().replace(b"walletSeries", b"missingSeries"))
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["source_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))

    result = import_performance_batch(request)

    assert result.quarantined_count == 1
    assert report.is_file()
    audit = json.loads((request.inbox / "import_audit.v4.json").read_text(encoding="utf-8"))
    assert audit["schema_version"] == 4
    assert audit["entries"][0]["status"] == "QUARANTINED"
    assert _count(request.database, "backtest_runs") == 0


def test_cleanup_resumes_after_crash_in_deleting_state(tmp_path: Path) -> None:
    request = _request(tmp_path)
    import_performance_batch(request)
    checklist = request.inbox / "html_delete_checklist.v4.csv"
    rows = list(csv.DictReader(checklist.open(newline="", encoding="utf-8")))
    rows[0]["cleanup_state"] = "DELETING"
    with checklist.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    resume_performance_cleanup(request)

    assert not (request.inbox / "reports" / "entry-1.html").exists()
    final_rows = list(csv.DictReader(checklist.open(newline="", encoding="utf-8")))
    assert final_rows[0]["cleanup_state"] == "DELETED"
