from __future__ import annotations

from datetime import datetime, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import threading

import duckdb
from openpyxl import Workbook
import pytest

import mrs3.panel as panel_module
from mrs3.panel import PanelController, create_panel_server
from mrs3.performance_v2_store import initialize_performance_v2
from mrs3.performance_v2_retest import RetestBatch


def _controller(tmp_path: Path, *, seed: bool = True) -> PanelController:
    bot = tmp_path / "bot"
    dates = tmp_path / "input" / "dates.xlsx"
    dates.parent.mkdir(parents=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["BTCUSDT", datetime(2026, 2, 1)])
    workbook.save(dates)
    template = tmp_path / "templates" / "base.json"
    template.parent.mkdir()
    template.write_text("{}", encoding="utf-8")
    config = {
        "tester_runner": {
            "bot_root": str(bot), "executable": "hb_c.exe", "base_url": "http://127.0.0.1:80",
            "port": 80, "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test",
            "wizard_result": "tester/wizard_result.json", "wizard_progress": "tester/wizard_progress.json",
            "tester_config": "tester/tester_config.json", "inbox_root": str(tmp_path / "inbox"),
        },
        "panel_paths": {"performance_db_root": "legacy"},
        "panel_workflow": {
            "listing_dates_path": "input/dates.xlsx",
            "strategy_templates": {"LONG": "templates/base.json", "SHORT": "templates/base.json"},
        },
    }
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "config.performance.json").write_text(
        json.dumps({"unified_performance_v2": {"database_root": "performance-v2", "workers": 1}}),
        encoding="utf-8",
    )
    if seed:
        database = tmp_path / "performance-v2" / "strategy_performance.duckdb"
        database.parent.mkdir()
        with duckdb.connect(str(database)) as connection:
            initialize_performance_v2(connection)
            strategy_id = connection.execute(
                """
                insert into strategies (
                    strategy_name, symbol, side, timeframe, close_ma_len, order_count,
                    analysis_run_id, candidate_identity, lifecycle_status, current_result_id,
                    created_at_utc, updated_at_utc
                ) values ('alpha', 'BTCUSDT', 'LONG', '1h', 20, 1, 'run', 'candidate', 'ACTIVE', null, now(), now())
                returning strategy_id
                """
            ).fetchone()[0]
            result_id = connection.execute(
                """
                insert into strategy_results (
                    strategy_id, report_start_utc, report_end_utc, exchange,
                    commission_rate, initial_balance, final_balance, imported_at_utc
                ) values (?, '2026-01-01 00:00:00+00', '2026-01-09 00:00:00+00', 'Bybit', .0004, 100, 101, now())
                returning result_id
                """,
                [strategy_id],
            ).fetchone()[0]
            connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
            connection.execute(
                "insert into strategy_tags values (?, 'RETEST', 'TEST', 'fixture', now())", [strategy_id]
            )
    return PanelController(tmp_path, config_path)


def test_retest_status_is_db_authoritative_and_defaults_from_current_result(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    status = controller.strategies_performance_v2_retest_status()

    assert status["count"] == status["retest_count"] == status["active_count"] == 1
    assert status["default_start"] == "2026-01-01"
    assert status["default_end"] == "2026-01-09"


def test_retest_status_is_safe_when_database_is_missing_or_empty(tmp_path: Path) -> None:
    missing = _controller(tmp_path / "missing", seed=False)
    assert missing.strategies_performance_v2_retest_status() == {
        "count": 0, "retest_count": 0, "active_count": 0, "phase": "IDLE"
    }

    empty_root = tmp_path / "empty"
    empty = _controller(empty_root, seed=False)
    database = empty_root / "performance-v2" / "strategy_performance.duckdb"
    database.parent.mkdir()
    with duckdb.connect(str(database)) as connection:
        initialize_performance_v2(connection)
    status = empty.strategies_performance_v2_retest_status()
    assert status["count"] == 0 and status["phase"] == "IDLE"
    assert status["default_start"] is None and status["default_end"] is None


def test_metadata_retest_inbox_resolves_relative_artifacts_from_runner_dirs(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    bot_root = tmp_path / "bot"
    report_dir = bot_root / "tester" / "report" / "my_test"
    strategy_dir = bot_root / "settings_strategy"
    report_dir.mkdir(parents=True)
    strategy_dir.mkdir(parents=True)
    (report_dir / "alpha.html").write_text("<html></html>", encoding="utf-8")
    (strategy_dir / "alpha.json").write_text("{}", encoding="utf-8")
    inbox = tmp_path / "inbox" / "retest-relative"
    inbox.mkdir(parents=True)
    (inbox / "inbox_manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "run_mode": "SINGLE_MODE",
        "source_mode": "metadata_only",
        "inbox_ready": True,
        "expected_strategy_names": ["alpha"],
        "entries": [{"strategy_name": "alpha", "strategy_path": "alpha.json", "report_path": "alpha.html"}],
    }), encoding="utf-8")

    controller._validate_metadata_inbox(inbox)


@pytest.mark.parametrize(
    "payload",
    [
        {"test_start": "2026-01-01", "test_end": "2026-01-01"},
        {"test_start": "2026-02-30", "test_end": "2026-03-01"},
        {"test_start": "2026-01-01"},
        {"test_start": "2026-01-01", "test_end": "2026-01-02", "start_date": "2026-01-01", "end_date": "2026-01-02"},
    ],
)
def test_retest_start_rejects_invalid_or_mixed_date_contract(tmp_path: Path, payload: dict[str, str]) -> None:
    controller = _controller(tmp_path)
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        with pytest.raises(ValueError):
            controller._retest_range(payload, connection)


def test_retest_start_uses_native_single_mode_and_keeps_database_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path)
    database = tmp_path / "performance-v2" / "strategy_performance.duckdb"
    before = database.read_bytes()
    batch = RetestBatch("batch-1", tmp_path / "output" / "strategies", tmp_path / "output" / "strategy_manifest.json", 1)
    seen: dict[str, object] = {}

    monkeypatch.setattr(panel_module, "build_retest_manifest", lambda *args: batch)

    class FakeTester:
        def start(self, manifest_path, *, analysis_run_id, start_date, end_date, job_id):
            seen.update(locals())
            return {"job_id": job_id, "state": "RUNNING", "phase": "RUNNING"}

    monkeypatch.setattr(controller, "_single_mode_strategy_test", lambda: FakeTester())
    result = controller.strategies_performance_v2_retest_start({})

    assert result["state"] == "RUNNING"
    assert seen["analysis_run_id"] == "batch-1"
    assert seen["start_date"] == "2026-01-01"
    assert seen["end_date"] == "2026-01-09"
    assert controller._panel_jobs.runtime(result["job_id"])["mode"] == "SINGLE_MODE"
    runtime = controller._panel_jobs.runtime(result["job_id"])
    assert runtime["retest"] is True and runtime["manifest_path"].endswith("strategy_manifest.json")
    assert database.read_bytes() == before


def _committed_retest_job(controller: PanelController, tmp_path: Path, *, state: str = "COMMITTED", job_id: str = "retest-job") -> str:
    inbox = tmp_path / "inbox" / job_id
    inbox.mkdir(parents=True)
    (inbox / "inbox_manifest.json").write_text(
        json.dumps({"expected_strategy_names": ["alpha"], "run_mode": "SINGLE_MODE"}), encoding="utf-8"
    )
    controller._panel_jobs.submit(
        "strategies.tester.native.start", {"retest": True}, f"panel:{job_id}", ("strategies.tester",), job_id=job_id
    )
    controller._panel_jobs.transition(job_id, "RUNNING")
    controller._panel_jobs.sync(
        job_id,
        {"state": state, "phase": state, "inbox_ready": state == "COMMITTED"},
        runtime={
            "retest": True, "inbox_path": str(inbox), "test_start": "2026-01-01",
            "test_end": "2026-01-09", "listing_dates_path": "input/dates.xlsx",
        },
    )
    return job_id


def test_retest_import_requires_committed_inbox_and_builds_mapping_on_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path)
    job_id = _committed_retest_job(controller, tmp_path)
    assert controller._panel_jobs.get(job_id)["retest"] is True
    captured: dict[str, object] = {}
    def fake_import(payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        captured.update(payload)
        controller._panel_jobs.submit(
            "strategies.performance.v2.import", {"retest": True}, "panel:import",
            ("performance-v2-db",), job_id="import",
        )
        controller._panel_jobs.transition("import", "RUNNING")
        return controller._panel_jobs.get("import")

    monkeypatch.setattr(controller, "strategies_performance_v2_import", fake_import)

    with pytest.raises(ValueError, match="server-built"):
        controller.strategies_performance_v2_retest_import({"tester_job_id": job_id, "replacement_strategy_ids": {"alpha": 1}})
    result = controller.strategies_performance_v2_retest_import({"tester_job_id": job_id})

    assert result["job_id"] == "import"
    assert captured["mode"] == "REPLACE"
    assert captured["replacement_strategy_ids"] == {"alpha": 1}
    assert captured["clear_retest_on_success"] is True
    assert captured["test_start"] == "2026-01-01"
    assert captured["listing_dates_path"] == "input/dates.xlsx"
    assert controller._panel_jobs.runtime(job_id)["retest_import_job_id"] == "import"
    with pytest.raises(ValueError, match="already started"):
        controller.strategies_performance_v2_retest_import({"tester_job_id": job_id})
    restarted = PanelController(tmp_path, tmp_path / "config.local.json")
    with pytest.raises(ValueError, match="already started"):
        restarted.strategies_performance_v2_retest_import({"tester_job_id": job_id})

    _committed_retest_job(controller, tmp_path / "not-ready", state="RUNNING", job_id="not-ready")
    with pytest.raises(ValueError, match="not committed"):
        controller.strategies_performance_v2_retest_import({"tester_job_id": "not-ready"})


def test_retest_import_allows_retry_after_failed_inner_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path)
    tester_job_id = _committed_retest_job(controller, tmp_path, job_id="tester-retry-failed-import")
    failed_import_id = "failed-retest-import"
    controller._panel_jobs.submit(
        "strategies.performance.v2.import", {"retest": True}, "panel:failed-retest-import",
        ("performance-v2-db",), job_id=failed_import_id,
    )
    controller._panel_jobs.transition(failed_import_id, "RUNNING")
    controller._panel_jobs.sync(
        failed_import_id,
        {"state": "FAILED", "phase": "FAILED", "error": {"code": "PERFORMANCE_V2_IMPORT_FAILED"}},
    )
    runtime = controller._panel_jobs.runtime(tester_job_id)
    runtime["retest_import_job_id"] = failed_import_id
    controller._panel_jobs.sync(tester_job_id, {"state": "COMMITTED"}, runtime=runtime)

    def fake_import(_payload: dict[str, object], *, _internal: bool = False) -> dict[str, object]:
        assert _internal is True
        retry_id = "retry-retest-import"
        controller._panel_jobs.submit(
            "strategies.performance.v2.import", {"retest": True}, f"panel:{retry_id}",
            ("performance-v2-db",), job_id=retry_id,
        )
        controller._panel_jobs.transition(retry_id, "RUNNING")
        return controller._panel_jobs.get(retry_id)

    monkeypatch.setattr(controller, "strategies_performance_v2_import", fake_import)

    result = controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})

    assert result["job_id"] == "retry-retest-import"
    assert result["job_id"] != failed_import_id
    assert controller._panel_jobs.runtime(tester_job_id)["retest_import_job_id"] == "retry-retest-import"
    with pytest.raises(ValueError, match="already started"):
        controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})


def test_retest_import_allows_retry_after_cancelled_inner_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path)
    tester_job_id = _committed_retest_job(controller, tmp_path, job_id="tester-retry-cancelled-import")
    cancelled_import_id = "cancelled-retest-import"
    controller._panel_jobs.submit(
        "strategies.performance.v2.import", {"retest": True}, "panel:cancelled-retest-import",
        ("performance-v2-db",), job_id=cancelled_import_id,
    )
    controller._panel_jobs.transition(cancelled_import_id, "RUNNING")
    controller._panel_jobs.transition(cancelled_import_id, "CANCELLING")
    controller._panel_jobs.transition(cancelled_import_id, "CANCELLED")
    runtime = controller._panel_jobs.runtime(tester_job_id)
    runtime["retest_import_job_id"] = cancelled_import_id
    controller._panel_jobs.sync(tester_job_id, {"state": "COMMITTED"}, runtime=runtime)

    def fake_import(_payload: dict[str, object], *, _internal: bool = False) -> dict[str, object]:
        assert _internal is True
        retry_id = "retry-cancelled-retest-import"
        controller._panel_jobs.submit(
            "strategies.performance.v2.import", {"retest": True}, f"panel:{retry_id}",
            ("performance-v2-db",), job_id=retry_id,
        )
        controller._panel_jobs.transition(retry_id, "RUNNING")
        return controller._panel_jobs.get(retry_id)

    monkeypatch.setattr(controller, "strategies_performance_v2_import", fake_import)
    result = controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})

    assert result["job_id"] == "retry-cancelled-retest-import"
    assert controller._panel_jobs.runtime(tester_job_id)["retest_import_job_id"] == "retry-cancelled-retest-import"
    with pytest.raises(ValueError, match="already started"):
        controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})


def test_retest_import_blocks_interrupted_marker_with_extra_error_fields(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    tester_job_id = _committed_retest_job(controller, tmp_path, job_id="tester-interrupted-import")
    interrupted_import_id = "interrupted-retest-import"
    controller._panel_jobs.submit(
        "strategies.performance.v2.import", {"retest": True}, "panel:interrupted-retest-import",
        ("performance-v2-db",), job_id=interrupted_import_id,
    )
    controller._panel_jobs.transition(interrupted_import_id, "RUNNING")
    controller._panel_jobs.sync(
        interrupted_import_id,
        {"state": "FAILED", "phase": "FAILED", "error": {"code": "INTERRUPTED", "message": "restart"}},
    )
    runtime = controller._panel_jobs.runtime(tester_job_id)
    runtime["retest_import_job_id"] = interrupted_import_id
    controller._panel_jobs.sync(tester_job_id, {"state": "COMMITTED"}, runtime=runtime)

    with pytest.raises(ValueError, match="already started"):
        controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})


def test_retest_import_blocks_failed_marker_with_unknown_error_code(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    tester_job_id = _committed_retest_job(controller, tmp_path, job_id="tester-unknown-import-error")
    unknown_import_id = "unknown-retest-import"
    controller._panel_jobs.submit(
        "strategies.performance.v2.import", {"retest": True}, "panel:unknown-retest-import",
        ("performance-v2-db",), job_id=unknown_import_id,
    )
    controller._panel_jobs.transition(unknown_import_id, "RUNNING")
    controller._panel_jobs.sync(
        unknown_import_id,
        {"state": "FAILED", "phase": "FAILED", "error": {"code": "UNKNOWN_FAILURE"}},
    )
    runtime = controller._panel_jobs.runtime(tester_job_id)
    runtime["retest_import_job_id"] = unknown_import_id
    controller._panel_jobs.sync(tester_job_id, {"state": "COMMITTED"}, runtime=runtime)

    with pytest.raises(ValueError, match="already started"):
        controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})


def test_retest_recovery_selector_prioritizes_ready_and_newest_jobs() -> None:
    utility = Path(__file__).parents[1] / "src" / "mrs3" / "panel_web" / "retest_recovery.js"
    tester = lambda job_id, state, inbox_ready=False: {
        "job_id": job_id, "kind": "strategies.tester.native.start", "retest": True,
        "state": state, "inbox_ready": inbox_ready,
    }
    fixtures = [
        [tester("ready-old", "COMMITTED", True), tester("failed-new", "FAILED")],
        [tester("ready-old", "COMMITTED", True), tester("running-new", "RUNNING")],
        [tester("ready-old", "COMMITTED", True), tester("ready-new", "COMMITTED", True)],
        [tester("failed-old", "FAILED"), tester("failed-new", "FAILED")],
        [tester("failed-old", "FAILED"), tester("committed-no-ready", "COMMITTED")],
    ]
    script = (
        f"const {{selectRetestTester}} = require({json.dumps(str(utility))});"
        f"const fixtures = {json.dumps(fixtures)};"
        "process.stdout.write(JSON.stringify(fixtures.map((jobs) => selectRetestTester(jobs)?.job_id ?? null)));"
    )
    result = subprocess.run(("node", "-e", script), capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == [
        "ready-old", "ready-old", "ready-new", "failed-new", "committed-no-ready",
    ]


def test_retest_check_selector_uses_newest_committed_native_job() -> None:
    utility = Path(__file__).parents[1] / "src" / "mrs3" / "panel_web" / "retest_recovery.js"
    tester = lambda job_id, state, inbox_ready=False: {
        "job_id": job_id, "kind": "strategies.tester.native.start", "retest": True,
        "state": state, "inbox_ready": inbox_ready,
    }
    jobs = [
        tester("ready-old", "COMMITTED", True),
        tester("committed-new", "COMMITTED"),
        tester("running-newest", "RUNNING"),
    ]
    script = (
        f"const {{selectCommittedRetestTester}} = require({json.dumps(str(utility))});"
        f"const jobs = {json.dumps(jobs)};"
        "process.stdout.write(selectCommittedRetestTester(jobs)?.job_id ?? '');"
    )
    result = subprocess.run(("node", "-e", script), capture_output=True, text=True, check=True)

    assert result.stdout == "committed-new"


def test_retest_import_reserves_before_inner_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path)
    tester_job_id = _committed_retest_job(controller, tmp_path, job_id="tester-reservation")
    entered = threading.Event()
    release = threading.Event()

    def fake_import(_payload: dict[str, object], *, _internal: bool = False) -> dict[str, object]:
        assert _internal is True
        entered.set()
        assert release.wait(2)
        controller._panel_jobs.submit(
            "strategies.performance.v2.import", {"retest": True}, "panel:reserved-import",
            ("performance-v2-db",), job_id="reserved-import",
        )
        controller._panel_jobs.transition("reserved-import", "RUNNING")
        return controller._panel_jobs.get("reserved-import")

    monkeypatch.setattr(controller, "strategies_performance_v2_import", fake_import)
    errors: list[BaseException] = []

    def launch() -> None:
        try:
            controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=launch)
    worker.start()
    assert entered.wait(2)
    with pytest.raises(ValueError, match="already started"):
        controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert not errors
    assert controller._panel_jobs.runtime(tester_job_id)["retest_import_job_id"] == "reserved-import"


@pytest.mark.parametrize("malformed", [None, {}, {"job_id": ""}, {"job_id": "unregistered"}])
def test_retest_import_clears_reservation_when_inner_job_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: object
) -> None:
    controller = _controller(tmp_path)
    tester_job_id = _committed_retest_job(controller, tmp_path, job_id="tester-retry")
    calls = 0

    def fake_import(_payload: dict[str, object], *, _internal: bool = False) -> object:
        nonlocal calls
        assert _internal is True
        calls += 1
        if calls == 1:
            return malformed
        controller._panel_jobs.submit(
            "strategies.performance.v2.import", {"retest": True}, "panel:retry-import",
            ("performance-v2-db",), job_id="retry-import",
        )
        controller._panel_jobs.transition("retry-import", "RUNNING")
        return controller._panel_jobs.get("retry-import")

    monkeypatch.setattr(controller, "strategies_performance_v2_import", fake_import)
    with pytest.raises(ValueError, match="invalid|not registered"):
        controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})
    assert "retest_import_job_id" not in controller._panel_jobs.runtime(tester_job_id)

    result = controller.strategies_performance_v2_retest_import({"tester_job_id": tester_job_id})
    assert result["job_id"] == "retry-import"
    assert controller._panel_jobs.runtime(tester_job_id)["retest_import_job_id"] == "retry-import"


def test_retest_import_route_returns_inner_job_and_serves_its_failure_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path)
    tester_job_id = _committed_retest_job(controller, tmp_path, job_id="tester-route")
    report = tmp_path / "performance-v2" / "retest-failures.csv"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("reason\nTEST\n", encoding="utf-8")

    def fake_import(payload: dict[str, object], *, _internal: bool = False) -> dict[str, object]:
        assert _internal is True and payload["mode"] == "REPLACE"
        inner_id = "retest-import-route"
        controller._panel_jobs.submit(
            "strategies.performance.v2.import", {"retest": True}, f"panel:{inner_id}",
            ("performance-v2-db",), job_id=inner_id,
        )
        controller._panel_jobs.transition(inner_id, "RUNNING")
        document = {"job_id": inner_id, "state": "COMMITTED", "result": {
            "failure_report_path": str(report),
            "database_path": str(tmp_path / "private.duckdb"),
            "audit_path": str(tmp_path / "private.audit.json"),
        }}
        controller._panel_jobs.sync(inner_id, document)
        controller._record_special_job(document)
        return controller._panel_jobs.get(inner_id)

    monkeypatch.setattr(controller, "strategies_performance_v2_import", fake_import)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request(
                "POST", "/api/v2/strategies/performance-v2/retest/import",
                body=json.dumps({"tester_job_id": tester_job_id}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = json.loads(response.read())
        finally:
            connection.close()
        assert response.status == 202
        inner_id = body["job"]["job_id"]
        assert inner_id == "retest-import-route" and body["job"]["retest"] is True
        assert body["job"]["result"]["failure_report_available"] is True
        assert "failure_report_path" not in body["job"]["result"]
        assert "database_path" not in body["job"]["result"]
        assert "audit_path" not in body["job"]["result"]

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", f"/api/artifact?name=performance-v2-failure-report:{inner_id}")
            artifact_response = connection.getresponse()
            artifact = artifact_response.read()
        finally:
            connection.close()
        assert artifact_response.status == 200
        assert artifact == report.read_bytes()
        restarted = PanelController(tmp_path, tmp_path / "config.local.json")
        assert restarted.artifact(f"performance-v2-failure-report:{inner_id}") == report.resolve()
        restarted_server = create_panel_server("127.0.0.1", 0, restarted)
        restarted_thread = threading.Thread(target=restarted_server.serve_forever, daemon=True)
        restarted_thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", restarted_server.server_port, timeout=2)
            try:
                connection.request("GET", f"/api/artifact?name=performance-v2-failure-report:{inner_id}")
                restarted_response = connection.getresponse()
                restarted_artifact = restarted_response.read()
            finally:
                connection.close()
            assert restarted_response.status == 200
            assert restarted_artifact == report.read_bytes()
        finally:
            restarted_server.shutdown()
            restarted_server.server_close()
    finally:
        server.shutdown()
        server.server_close()


def test_public_performance_v2_dispatcher_rejects_replacement_controls(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    job_id = _committed_retest_job(controller, tmp_path, job_id="tester-public")

    with pytest.raises(ValueError, match="internal only"):
        controller.panel_job_submit({
            "kind": "strategies.performance.v2.import",
            "request": {"tester_job_id": job_id, "replacement_strategy_ids": {"alpha": 1}},
        })
    with pytest.raises(ValueError, match="internal only"):
        controller.panel_job_submit({
            "kind": "strategies.performance.v2.import",
            "request": {"tester_job_id": job_id, "mode": "REPLACE"},
        })
    with pytest.raises(ValueError, match="internal only"):
        controller.panel_job_submit({
            "kind": "strategies.performance.v2.import",
            "request": {"tester_job_id": job_id, "clear_retest_on_success": True},
        })
    with pytest.raises(ValueError, match="internal only"):
        controller.panel_job_submit({
            "kind": "strategies.performance.v2.import",
            "request": {"tester_job_id": job_id, "_retest": True},
        })


def test_retest_http_start_and_import_return_job_envelopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path, seed=False)
    calls: list[dict[str, object]] = []

    def fake_submit(document: dict[str, object]) -> dict[str, str]:
        calls.append(document)
        return {"job_id": f"job-{len(calls)}"}

    monkeypatch.setattr(controller, "panel_job_submit", fake_submit)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    requests = (
        ("/api/v2/strategies/performance-v2/retest/start", {"test_start": "2026-01-01", "test_end": "2026-01-09"}),
        ("/api/v2/strategies/performance-v2/retest/import", {"tester_job_id": "tester-1"}),
    )
    try:
        for endpoint, payload in requests:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request(
                    "POST", endpoint, body=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                body = json.loads(response.read())
            finally:
                connection.close()
            assert response.status == 202
            assert body == {"job": {"job_id": f"job-{len(calls)}"}}
    finally:
        server.shutdown()
        server.server_close()

    assert calls == [
        {"kind": "strategies.performance.v2.retest.start", "request": requests[0][1]},
        {"kind": "strategies.performance.v2.retest.import", "request": requests[1][1]},
    ]


def test_retest_status_http_is_safe_without_database(tmp_path: Path) -> None:
    controller = _controller(tmp_path, seed=False)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/api/v2/strategies/performance-v2/retest/status")
            response = connection.getresponse()
            body = json.loads(response.read())
        finally:
            connection.close()
        assert response.status == 200
        assert body["count"] == 0 and body["phase"] == "IDLE"
    finally:
        server.shutdown()
        server.server_close()


def test_failure_report_is_served_only_for_committed_import_job(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    job_id = "import-job"
    controller._panel_jobs.submit(
        "strategies.performance.v2.import", {"tester_job_id": "tester"}, f"panel:{job_id}", ("performance-v2-db",), job_id=job_id
    )
    controller._panel_jobs.transition(job_id, "RUNNING")
    report = tmp_path / "performance-v2" / "performance_v2_failures_import.csv"
    report.write_text("reason\nTEST\n", encoding="utf-8")
    controller._panel_jobs.sync(job_id, {"state": "COMMITTED", "result": {"failure_report_path": str(report)}})
    controller._record_special_job({"job_id": job_id, "state": "COMMITTED", "result": {"failure_report_path": str(report)}})
    assert controller.artifact(f"performance-v2-failure-report:{job_id}") == report.resolve()
    with pytest.raises(ValueError):
        controller.artifact("performance-v2-failure-report:missing")
    restarted = PanelController(tmp_path, tmp_path / "config.local.json")
    assert restarted.artifact(f"performance-v2-failure-report:{job_id}") == report.resolve()

    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", f"/api/artifact?name=performance-v2-failure-report:{job_id}")
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        assert response.status == 200
        assert body == report.read_bytes()
    finally:
        server.shutdown()
        server.server_close()


def test_failure_report_requires_committed_import_and_contained_regular_file(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    root = tmp_path / "performance-v2"
    report = root / "report.csv"
    report.write_text("ok\n", encoding="utf-8")

    controller._panel_jobs.submit(
        "strategies.performance.v2.import", {"tester_job_id": "tester"}, "import-running", ("db",), job_id="import-running"
    )
    controller._panel_jobs.transition("import-running", "RUNNING")
    controller._panel_jobs.sync("import-running", {"state": "RUNNING", "result": {"failure_report_path": str(report)}})
    with pytest.raises(ValueError):
        controller.performance_v2_failure_report("import-running")

    controller._panel_jobs.submit(
        "strategies.tester.native.start", {}, "foreign", (), job_id="foreign"
    )
    controller._panel_jobs.transition("foreign", "RUNNING")
    controller._panel_jobs.sync("foreign", {"state": "COMMITTED", "result": {"failure_report_path": str(report)}})
    with pytest.raises(ValueError):
        controller.performance_v2_failure_report("foreign")

    nested = root / "nested" / "report.csv"
    nested.parent.mkdir()
    nested.write_text("nested\n", encoding="utf-8")
    controller._panel_jobs.submit(
        "strategies.performance.v2.import", {}, "nested", (), job_id="import-nested"
    )
    controller._panel_jobs.transition("import-nested", "RUNNING")
    controller._panel_jobs.sync("import-nested", {"state": "COMMITTED", "result": {"failure_report_path": str(nested)}})
    with pytest.raises(ValueError):
        controller.performance_v2_failure_report("import-nested")

    outside = tmp_path / "outside.csv"
    outside.write_text("outside\n", encoding="utf-8")
    controller._panel_jobs.submit(
        "strategies.performance.v2.import", {}, "outside", (), job_id="import-outside"
    )
    controller._panel_jobs.transition("import-outside", "RUNNING")
    controller._panel_jobs.sync("import-outside", {"state": "COMMITTED", "result": {"failure_report_path": str(outside)}})
    with pytest.raises(ValueError):
        controller.performance_v2_failure_report("import-outside")

    symlink = root / "report-link.csv"
    try:
        symlink.symlink_to(outside)
    except OSError:
        pytest.skip("Windows environment does not provide usable symlink creation")
    controller._panel_jobs.submit(
        "strategies.performance.v2.import", {}, "symlink", (), job_id="import-symlink"
    )
    controller._panel_jobs.transition("import-symlink", "RUNNING")
    controller._panel_jobs.sync("import-symlink", {"state": "COMMITTED", "result": {"failure_report_path": str(symlink)}})
    with pytest.raises(ValueError):
        controller.performance_v2_failure_report("import-symlink")


def test_later_listing_date_is_valid_warmup_input(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    database = tmp_path / "performance-v2" / "strategy_performance.duckdb"
    with duckdb.connect(str(database), read_only=True) as connection:
        controller._validate_retest_listing(connection, tmp_path / "input" / "dates.xlsx", "2026-01-01")
