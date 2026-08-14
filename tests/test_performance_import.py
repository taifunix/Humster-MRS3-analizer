from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import duckdb
import pytest
import mrs3.performance_import as performance_import

from mrs3.performance_import import (
    PerformanceImportError,
    PerformanceImportProgress,
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
    strategy = {"name": "MRS3 Demo", "exchange": {"name": "Bybit"}, "basic": {"side": "LONG", "strategy": "MRS3", "symbol": "ONUSDT", "time_frame": "1h"}}
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


def _replace_strategy(request: PerformanceImportRequest, strategy: dict[str, object]) -> None:
    strategy_path = next((request.inbox / "strategies").glob("*.json"))
    strategy_path.write_bytes(_canonical(strategy))
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    entry["source_strategy_sha256"] = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
    entry["strategy_version_id"] = hashlib.sha256(_canonical(strategy)).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))


def _replace_report(request: PerformanceImportRequest, report: bytes) -> None:
    report_path = request.inbox / "reports" / "entry-1.html"
    report_path.write_bytes(report)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["source_report_sha256"] = hashlib.sha256(report).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))


def _legacy_rounded_report() -> bytes:
    report = FIXTURE.read_bytes()
    replacements = [
        (b"<td>Final balance</td><td>1012.5</td>", b"<td>Final balance</td><td>1439.53</td>"),
        (b"<td>Total PnL</td><td>12.5</td>", b"<td>Total PnL</td><td>439.53</td>"),
        (b"<td>Total PnL, %</td><td>1.25</td>", b"<td>Total PnL, %</td><td>43.95</td>"),
        (b"<td>Total Trades</td><td>2</td>", b"<td>Total Trades</td><td>310</td>"),
        (b"<td>Win Trades</td><td>1</td>", b"<td>Win Trades</td><td>237</td>"),
        (b"<td>Los Trades</td><td>1</td>", b"<td>Los Trades</td><td>73</td>"),
        (b"<td>Win Rate, %</td><td>50</td>", b"<td>Win Rate, %</td><td>76.45</td>"),
        (b"<td>Max Drawdown</td><td>5.0625</td>", b"<td>Max Drawdown</td><td>74.44</td>"),
        (b"<td>Max Drawdown, %</td><td>0.5</td>", b"<td>Max Drawdown, %</td><td>5.80</td>"),
        (
            b'const walletSeries = [[1785542400000,"1000"],[1785546000000,"1012.5"],[1785549600000,"1007.4375"],[1785553200000,"1012.5"]];',
            b'const walletSeries = [[1785542400000,"1000"],[1785546000000,"1282.77898396"],[1785549600000,"1208.3342077"],[1785553200000,"1447.49436808"],[1785556800000,"1439.532329415"]];',
        ),
        (
            b'const equitySeries = [[1785542400000,"1000"],[1785546000000,"1012.5"],[1785549600000,"1007.4375"],[1785553200000,"1012.5"]];',
            b'const equitySeries = [[1785542400000,"1000"],[1785546000000,"1282.77898396"],[1785549600000,"1208.3342077"],[1785553200000,"1447.49436808"],[1785556800000,"1439.532329415"]];',
        ),
    ]
    for old, new in replacements:
        report = report.replace(old, new)
    return report


def _fractional_count_report(metric: str, value: str) -> bytes:
    report = FIXTURE.read_bytes()
    if metric == "Loss Trades":
        return report.replace(
            b"<td>Los Trades</td><td>1</td>",
            f"<td>{metric}</td><td>{value}</td>".encode(),
        )
    replacements = {
        "Total Trades": (b"<td>Total Trades</td><td>2</td>",),
        "Win Trades": (b"<td>Win Trades</td><td>1</td>",),
        "Los Trades": (b"<td>Los Trades</td><td>1</td>",),
    }
    old = replacements[metric][0]
    return report.replace(old, f"<td>{metric}</td><td>{value}</td>".encode())


def _bare_timestamp_report() -> bytes:
    return (
        FIXTURE.read_bytes()
        .replace(b"2026-08-01T00:00:00Z", b"2026-08-01 00:00:00")
        .replace(b"2026-08-01T01:00:00Z", b"2026-08-01 01:00:00")
    )


def _request_with_entries(tmp_path: Path, count: int = 3) -> PerformanceImportRequest:
    request = _request(tmp_path)
    if count <= 1:
        return request
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_strategy = json.loads(
        next((request.inbox / "strategies").glob("*.json")).read_text(encoding="utf-8")
    )
    reports = request.inbox / "reports"
    strategies = request.inbox / "strategies"
    for index in range(2, count + 1):
        name = f"MRS3 Demo-{index}"
        report = FIXTURE.read_bytes().replace(
            b'"name":"MRS3 Demo"', f'"name":"{name}"'.encode("utf-8")
        )
        strategy = dict(base_strategy)
        strategy["name"] = name
        strategy_id = hashlib.sha256(_canonical(strategy)).hexdigest()
        report_path = f"reports/entry-{index}.html"
        strategy_path = f"strategies/{strategy_id}.json"
        (reports / f"entry-{index}.html").write_bytes(report)
        (strategies / f"{strategy_id}.json").write_bytes(_canonical(strategy))
        manifest["entries"].append({
            "manifest_entry_id": f"entry-{index}",
            "strategy_name": name,
            "strategy_version_id": strategy_id,
            "strategy_path": strategy_path,
            "report_path": report_path,
            "wizard_run_id": f"run-{index}",
            "exchange_name": "Bybit",
            "source_strategy_sha256": hashlib.sha256(_canonical(strategy)).hexdigest(),
            "source_report_sha256": hashlib.sha256(report).hexdigest(),
        })
        manifest["expected_strategy_names"].append(name)
    manifest_path.write_bytes(_canonical(manifest))
    return request


class _FakeFuture:
    def __init__(self, fn, args) -> None:
        self.fn = fn
        self.args = args

    def result(self):
        return self.fn(*self.args)

    def cancel(self) -> bool:
        return False


class _ReversingExecutor:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self.futures: list[_FakeFuture] = []

    def __enter__(self) -> "_ReversingExecutor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def submit(self, fn, *args):
        future = _FakeFuture(fn, args)
        self.futures.append(future)
        return future


def test_identical_payload_skips_without_second_run(tmp_path: Path) -> None:
    request = _request(tmp_path)

    first = import_performance_batch(request)
    second = import_performance_batch(request)

    assert first.imported_count == 1
    assert second.skipped_count == 1
    assert _count(request.database, "backtest_runs") == 1


def test_import_persists_precise_metrics_and_retains_summary_diagnostics(tmp_path: Path) -> None:
    request = _request(tmp_path)

    result = import_performance_batch(request)

    assert result.imported_count == 1
    with duckdb.connect(str(request.database), read_only=True) as connection:
        row = connection.execute(
            "select final_balance, total_pnl, total_pnl_pct, max_drawdown, max_drawdown_pct, win_rate_pct, metrics_json from backtest_metrics"
        ).fetchone()
    assert row[0] == Decimal("1012.5")
    assert row[1] == Decimal("12.5")
    assert row[2] == Decimal("1.25")
    assert row[3] == Decimal("5.0625")
    assert row[4] == Decimal("5.0625") / Decimal("1012.5") * 100
    assert row[5] == Decimal("50")
    assert json.loads(row[6])["Total PnL, %"] == "1.25"


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("Total Trades", "2.5"),
        ("Win Trades", "1.5"),
        ("Los Trades", "1.5"),
        ("Loss Trades", "1.5"),
    ],
)
def test_fractional_trade_count_metric_is_rejected_before_integer_conversion(
    tmp_path: Path, metric: str, value: str
) -> None:
    request = _request(tmp_path)
    _replace_report(request, _fractional_count_report(metric, value))
    manifest = json.loads(
        (request.inbox / "inbox_manifest.json").read_text(encoding="utf-8")
    )

    with pytest.raises(PerformanceImportError, match="must be an integer"):
        performance_import._prepare_entry(request.inbox, manifest["entries"][0])


def test_import_derives_canonical_metrics_from_equity_and_counts_when_html_summary_is_rounded(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    _replace_report(request, _legacy_rounded_report())

    result = import_performance_batch(request)

    assert result.imported_count == 1
    with duckdb.connect(str(request.database), read_only=True) as connection:
        row = connection.execute(
            "select final_balance, total_pnl, total_pnl_pct, max_drawdown, max_drawdown_pct, win_rate_pct, metrics_json from backtest_metrics"
        ).fetchone()
    exact_dd_pct = Decimal("74.44477626") / Decimal("1282.77898396") * 100
    assert row[0] == Decimal("1439.532329415")
    assert row[1] == Decimal("439.532329415")
    assert row[2] == Decimal("43.9532329415")
    assert row[3] == Decimal("74.44477626")
    assert row[4] == exact_dd_pct.quantize(Decimal("0.000000000001"))
    assert row[5] == Decimal("76.45161290322580645161290323").quantize(
        Decimal("0.000000000001")
    )
    assert json.loads(row[6])["Total PnL, %"] == "43.95"
    assert json.loads(row[6])["Max Drawdown, %"] == "5.80"


def test_conflict_keeps_html_and_database_unchanged(tmp_path: Path) -> None:
    request = _request(tmp_path)
    import_performance_batch(request)
    report = request.inbox / "reports" / "entry-1.html"
    report.write_bytes(report.read_bytes().replace(b"<td>Total fees</td><td>0.1</td>", b"<td>Total fees</td><td>0.2</td>"))
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["source_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))

    with pytest.raises(PerformanceImportError, match="IDENTITY_CONFLICT"):
        import_performance_batch(request)
    assert report.is_file()
    assert _count(request.database, "backtest_runs") == 1


def test_settings_mismatch_is_quarantined(tmp_path: Path) -> None:
    request = _request(tmp_path)
    strategy_path = next((request.inbox / "strategies").glob("*.json"))
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
    strategy["basic"]["symbol"] = "OTHERUSDT"
    strategy_path.write_bytes(_canonical(strategy))
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    entry["source_strategy_sha256"] = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
    entry["strategy_version_id"] = hashlib.sha256(_canonical(strategy)).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))

    result = import_performance_batch(request)

    assert result.quarantined_count == 1
    audit = json.loads((request.inbox / "import_audit.v4.json").read_text(encoding="utf-8"))
    assert "settings" in audit["entries"][0]["error_message"]


def test_flat_strategy_settings_compare_full_object_including_exchange(tmp_path: Path) -> None:
    request = _request(tmp_path)

    result = import_performance_batch(request)

    assert result.imported_count == 1
    assert result.quarantined_count == 0


def test_html_tester_exchange_marker_preserves_exact_remaining_settings(tmp_path: Path) -> None:
    request = _request(tmp_path)
    report = (request.inbox / "reports" / "entry-1.html").read_bytes().replace(
        b'"exchange":{"name":"Bybit"}',
        b'"exchange":{"name":"tester"}',
    )
    _replace_report(request, report)

    result = import_performance_batch(request)

    assert result.imported_count == 1
    assert result.quarantined_count == 0


def test_html_exchange_other_than_source_or_tester_marker_is_quarantined(tmp_path: Path) -> None:
    request = _request(tmp_path)
    report = (request.inbox / "reports" / "entry-1.html").read_bytes().replace(
        b'"exchange":{"name":"Bybit"}',
        b'"exchange":{"name":"other"}',
    )
    _replace_report(request, report)

    result = import_performance_batch(request)

    assert result.imported_count == 0
    assert result.quarantined_count == 1


def test_profit_factor_gross_profit_gross_loss_label_is_imported(tmp_path: Path) -> None:
    request = _request(tmp_path)
    report = (request.inbox / "reports" / "entry-1.html").read_bytes().replace(
        b"<td>Profit Factor</td>",
        b"<td>Profit Factor (gross profit/gross loss)</td>",
    )
    _replace_report(request, report)

    result = import_performance_batch(request)

    assert result.imported_count == 1
    assert result.quarantined_count == 0


def test_profit_factor_na_is_imported_as_explicitly_unavailable(tmp_path: Path) -> None:
    request = _request(tmp_path)
    report = (request.inbox / "reports" / "entry-1.html").read_bytes().replace(
        b"<td>Profit Factor</td><td>2.5</td>",
        b"<td>Profit Factor</td><td>n/a</td>",
    )
    _replace_report(request, report)

    result = import_performance_batch(request)

    assert result.imported_count == 1
    with duckdb.connect(str(request.database), read_only=True) as connection:
        assert connection.execute(
            "select profit_factor, profit_factor_status from backtest_metrics"
        ).fetchone() == (None, "UNDEFINED_GROSS_LOSS_ZERO")


def test_import_progress_reports_readback_only_after_transactional_import(tmp_path: Path) -> None:
    request = _request(tmp_path)
    events: list[PerformanceImportProgress] = []

    result = import_performance_batch(request, events.append)

    assert result.imported_count == 1
    assert events[0].stage == "VALIDATE"
    assert events[0].completed == 0
    assert events[0].total == 1
    assert any(event.stage == "TRANSACTIONAL_IMPORT" and event.imported == 1 for event in events)
    assert events[-1].stage == "READBACK_VERIFIED"
    assert events[-1].imported == 1


def test_nested_strategy_settings_remain_the_exact_comparison_contract(tmp_path: Path) -> None:
    request = _request(tmp_path)
    report = (request.inbox / "reports" / "entry-1.html").read_bytes().replace(
        b',"exchange":{"name":"Bybit"}',
        b"",
    )
    _replace_report(request, report)
    settings = {
        "name": "MRS3 Demo",
        "basic": {"side": "LONG", "strategy": "MRS3", "symbol": "ONUSDT", "time_frame": "1h"},
    }
    _replace_strategy(request, {"exchange": {"name": "Bybit"}, "settings": settings})

    result = import_performance_batch(request)

    assert result.imported_count == 1
    assert result.quarantined_count == 0


def test_import_preflight_rejects_duplicate_identity_before_file_reads(tmp_path: Path) -> None:
    request = _request(tmp_path)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"].append(dict(manifest["entries"][0]))
    manifest["entries"][1]["report_path"] = "../outside.html"
    manifest_path.write_bytes(_canonical(manifest))

    with pytest.raises(PerformanceImportError, match="duplicate"):
        import_performance_batch(request)
    assert _count(request.database, "backtest_runs") == 0


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


def test_cleanup_requires_valid_audit_evidence(tmp_path: Path) -> None:
    request = _request(tmp_path)
    import_performance_batch(request)
    audit_path = request.inbox / "import_audit.v4.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["schema_version"] = 3
    audit_path.write_bytes(_canonical(audit))

    with pytest.raises(PerformanceImportError, match="audit"):
        resume_performance_cleanup(request)
    assert (request.inbox / "reports" / "entry-1.html").is_file()


def test_cleanup_rejects_report_path_outside_inbox(tmp_path: Path) -> None:
    request = _request(tmp_path)
    import_performance_batch(request)
    checklist = request.inbox / "html_delete_checklist.v4.csv"
    rows = list(csv.DictReader(checklist.open(newline="", encoding="utf-8")))
    outside = tmp_path / "outside.html"
    outside.write_text("retain", encoding="utf-8")
    rows[0]["report_path"] = str(outside)
    with checklist.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(PerformanceImportError, match="inbox"):
        resume_performance_cleanup(request)
    assert outside.is_file()


def test_cleanup_hash_mismatch_preserves_html(tmp_path: Path) -> None:
    request = _request(tmp_path)
    import_performance_batch(request)
    report = request.inbox / "reports" / "entry-1.html"
    report.write_bytes(report.read_bytes() + b"changed")

    with pytest.raises(PerformanceImportError, match="hash"):
        resume_performance_cleanup(request)
    assert report.is_file()


def test_cleanup_repairs_deleted_checklist_when_database_is_deleting(tmp_path: Path) -> None:
    request = _request(tmp_path)
    import_performance_batch(request)
    checklist = request.inbox / "html_delete_checklist.v4.csv"
    rows = list(csv.DictReader(checklist.open(newline="", encoding="utf-8")))
    rows[0]["cleanup_state"] = "DELETED"
    with checklist.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    with duckdb.connect(str(request.database)) as connection:
        connection.execute("update import_files set cleanup_state='DELETING'")

    resume_performance_cleanup(request)

    assert not (request.inbox / "reports" / "entry-1.html").exists()
    assert next(csv.DictReader(checklist.open(newline="", encoding="utf-8")))["cleanup_state"] == "DELETED"


def test_same_strategy_version_allows_distinct_period(tmp_path: Path) -> None:
    request = _request(tmp_path)
    first = import_performance_batch(request)
    report = request.inbox / "reports" / "entry-1.html"
    report.write_bytes(report.read_bytes().replace(b"1785542400000", b"1785538800000"))
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["source_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))

    second = import_performance_batch(request)

    assert first.imported_count == second.imported_count == 1
    assert _count(request.database, "strategy_versions") == 1
    assert _count(request.database, "backtest_runs") == 2


def test_import_files_records_readback_counts_and_delete_state(tmp_path: Path) -> None:
    request = _request(tmp_path)

    result = import_performance_batch(request)

    with duckdb.connect(str(request.database), read_only=True) as connection:
        row = connection.execute(
            "select import_id, status, source_html_sha256, action_count, equity_sample_count, safe_to_delete, cleanup_state from import_files"
        ).fetchone()
    assert row is not None
    assert row[0] == result.import_id
    assert row[1] == "IMPORTED"
    assert row[2] == hashlib.sha256((request.inbox / "reports" / "entry-1.html").read_bytes()).hexdigest()
    assert row[3:5] == (2, 4)
    assert row[5:7] == (True, "DELETE_READY")


def test_database_init_failure_writes_audit_and_retains_html(tmp_path: Path) -> None:
    request = _request(tmp_path)
    bad_database = tmp_path / "database-dir"
    bad_database.mkdir()
    request = PerformanceImportRequest(request.inbox, bad_database)

    with pytest.raises(PerformanceImportError, match="database"):
        import_performance_batch(request)
    assert (request.inbox / "reports" / "entry-1.html").is_file()
    audit = json.loads((request.inbox / "import_audit.v4.json").read_text(encoding="utf-8"))
    assert audit["schema_version"] == 4
    assert audit["status"] == "FAILED"


def test_readback_failure_never_marks_delete_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(tmp_path)

    def fail_readback(*args: object, **kwargs: object) -> None:
        raise PerformanceImportError("readback verification failed")

    monkeypatch.setattr(performance_import, "_verify_readback", fail_readback)
    with pytest.raises(PerformanceImportError, match="readback"):
        import_performance_batch(request)
    assert (request.inbox / "reports" / "entry-1.html").is_file()
    assert not (request.inbox / "html_delete_checklist.v4.csv").exists()
    assert _count(request.database, "backtest_runs") == 0
    assert _count(request.database, "import_runs") == 0
    assert _count(request.database, "import_files") == 0


def test_parallel_preparation_preserves_manifest_order_even_when_workers_finish_reversed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request_with_entries(tmp_path, 3)
    request = PerformanceImportRequest(request.inbox, request.database, workers=3)
    monkeypatch.setattr(performance_import, "ProcessPoolExecutor", _ReversingExecutor)

    def reversed_completion(futures):
        return iter(reversed(list(futures)))

    monkeypatch.setattr(performance_import, "as_completed", reversed_completion)

    result = import_performance_batch(request)

    assert result.imported_count == 3
    audit = json.loads((request.inbox / "import_audit.v4.json").read_text(encoding="utf-8"))
    assert [entry["manifest_entry_id"] for entry in audit["entries"]] == [
        "entry-1", "entry-2", "entry-3",
    ]


def test_action_and_equity_writes_use_bulk_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(tmp_path)
    calls: list[tuple[str, int]] = []
    original_append = duckdb.DuckDBPyConnection.append

    def append_spy(connection, table, frame):
        calls.append((str(table), len(frame)))
        return original_append(connection, table, frame)

    monkeypatch.setattr(duckdb.DuckDBPyConnection, "append", append_spy)

    result = import_performance_batch(request)

    assert result.imported_count == 1
    assert any(table == "backtest_actions" for table, _ in calls)
    assert any(table == "backtest_equity" for table, _ in calls)


def test_bare_utc_action_timestamps_are_stored_as_timezone_aware_utc(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _replace_report(request, _bare_timestamp_report())

    result = import_performance_batch(request)

    assert result.imported_count == 1
    with duckdb.connect(str(request.database), read_only=True) as connection:
        rows = connection.execute(
            "select action_index, timestamp_utc from backtest_actions order by action_index"
        ).fetchall()
    assert [
        (index, timestamp.astimezone(timezone.utc))
        for index, timestamp in rows
    ] == [
        (0, datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)),
        (1, datetime(2026, 8, 1, 1, 0, 0, tzinfo=timezone.utc)),
    ]


def test_identical_retry_skips_full_parse_from_committed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    import_performance_batch(request)

    def unexpected_parse(*args, **kwargs):
        raise AssertionError("identical evidence must not trigger full HTML parse")

    monkeypatch.setattr(performance_import, "_prepare_entry", unexpected_parse)

    result = import_performance_batch(request)

    assert result.skipped_count == 1


def test_import_progress_counts_and_phase_timings_are_monotonic(tmp_path: Path) -> None:
    request = _request(tmp_path)
    events: list[PerformanceImportProgress] = []

    result = import_performance_batch(request, events.append)

    prepared = [event.prepared for event in events if event.stage == "PARSE_PREPARE"]
    scheduled = [event.scheduled for event in events if event.stage == "SCHEDULED"]
    assert prepared == sorted(prepared)
    assert scheduled == [1]
    assert result.phases
    assert result.phases["PARSE_PREPARE"] >= 0
    assert events[-1].prepared == 1
    assert events[-1].phase_seconds


def test_cancellation_preserves_html_audit_and_database(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request = PerformanceImportRequest(
        request.inbox,
        request.database,
        workers=1,
        cancellation_requested=lambda: True,
    )

    with pytest.raises(PerformanceImportError, match="cancelled"):
        import_performance_batch(request)

    assert (request.inbox / "reports" / "entry-1.html").is_file()
    audit = json.loads((request.inbox / "import_audit.v4.json").read_text(encoding="utf-8"))
    assert audit["status"] == "CANCELLED"
    assert _count(request.database, "backtest_runs") == 0
