from __future__ import annotations

from http.client import HTTPConnection
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import time
from hashlib import sha256
from dataclasses import replace

import pytest

from mrs3.panel import PanelController, _Job, create_panel_server
from mrs3.duckdb_import import ImportJobResult, ImportPreflight, ImportProgress


class _FakeProcess:
    def __init__(self, command: list[str], **_: object) -> None:
        self.command = command
        self.pid = 12345
        self.stdout = io.StringIO("started\nfinished\n")
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode


def _wait_finished(controller: PanelController) -> dict[str, object]:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        snapshot = controller.snapshot()
        if snapshot["job"] and not snapshot["job"]["running"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("panel job did not finish")


def _import_result(tmp_path: Path, *, final_state: str = "COMMITTED", tampered: bool = False) -> ImportJobResult:
    audit = tmp_path / "audit" / "job-1"
    audit.mkdir(parents=True)
    checklist = audit / "html_delete_checklist.json"
    checklist.write_text(json.dumps({"job_id": "job-1", "safe_to_delete": "YES"}), encoding="utf-8")
    manifest = audit / "import_manifest.json"
    manifest.write_text(json.dumps({"job_id": "job-1", "final_state": final_state, "safe_to_delete": "YES", "artifacts": {"checklist": {"sha256": sha256(checklist.read_bytes()).hexdigest()}}}), encoding="utf-8")
    if tampered:
        checklist.write_text("not json", encoding="utf-8")
    return ImportJobResult("job-1", final_state, 3, 2, 1, 0, 1, 0, 0, "YES", manifest, sha256(manifest.read_bytes()).hexdigest(), checklist, sha256(checklist.read_bytes()).hexdigest())


def _wait_import_finished(controller: PanelController) -> dict[str, object]:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        document = controller.snapshot()["duckdb_import"]
        if document and not document["running"]:
            return document
        time.sleep(.01)
    raise AssertionError("panel import did not finish")


class _ImportUiParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.input_ids: set[str] = set()
        self.actions: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("id"):
            self.input_ids.add(str(attributes["id"]))
        if tag == "button" and attributes.get("onclick"):
            self.actions.add(str(attributes["onclick"]))


def test_duckdb_import_settings_preflight_start_cancel_and_evidence_gate(tmp_path: Path) -> None:
    calls: list[object] = []

    def preflight(request: object) -> ImportPreflight:
        calls.append(request)
        return ImportPreflight("token-1", 3, 5, "digest")

    def importer(request: object, progress: object) -> ImportJobResult:
        progress(ImportProgress("RUNNING", 3, 2, 1, 0, 1, 0, 0))
        return _import_result(tmp_path)

    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=preflight, import_func=importer)
    settings = controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "default_html_root": "html", "audit_root": "audit", "workers": 2, "transaction_batch_size": 10})
    assert settings["workers"] == 2
    assert controller.duckdb_import_preflight({"root_path": "html"})["token"] == "token-1"
    with pytest.raises(ValueError, match="preflight"):
        controller.start_duckdb_import({"root_path": "html"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "token-1"})
    deadline = time.monotonic() + 1
    while controller.snapshot()["duckdb_import"]["running"] and time.monotonic() < deadline:
        time.sleep(.01)
    job = controller.snapshot()["duckdb_import"]
    assert job["counts"] == {"parsed": 2, "inserted": 1, "replaced": 0, "identical": 1, "ambiguous": 0, "quarantined": 0}
    assert job["final_state"] == "COMMITTED"
    assert job["safe_to_delete"] == "YES"
    assert all(str(tmp_path) not in value for value in json.dumps(job).splitlines())
    assert controller.cancel_duckdb_import()["running"] is False


def test_duckdb_import_rejects_stale_and_parallel_jobs_and_tampered_evidence(tmp_path: Path) -> None:
    released = __import__("threading").Event()
    started = __import__("threading").Event()

    def preflight(_: object) -> ImportPreflight:
        return ImportPreflight("fresh", 1, 5, "digest")

    def importer(_: object, __: object) -> ImportJobResult:
        started.set(); released.wait(1)
        return _import_result(tmp_path, tampered=True)

    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=preflight, import_func=importer)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "default_html_root": "html", "audit_root": "audit"})
    controller.duckdb_import_preflight({"root_path": "html"})
    with pytest.raises(ValueError, match="preflight"):
        controller.start_duckdb_import({"root_path": "html", "preflight_token": "stale"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "fresh"})
    assert started.wait(1)
    with pytest.raises(RuntimeError, match="already running"):
        controller.start_duckdb_import({"root_path": "html", "preflight_token": "fresh"})
    assert controller.cancel_duckdb_import()["cancel_requested"] is True
    released.set()
    deadline = time.monotonic() + 1
    while controller.snapshot()["duckdb_import"]["running"] and time.monotonic() < deadline:
        time.sleep(.01)
    job = controller.snapshot()["duckdb_import"]
    assert job["safe_to_delete"] == "NO"
    assert job["artifacts"] == {}


def test_duckdb_import_migration_activates_only_valid_unchanged_target(tmp_path: Path) -> None:
    target = tmp_path / "migrated.duckdb"
    target.write_bytes(b"target")
    source = tmp_path / "source.duckdb"
    source.write_bytes(b"source")

    class Result:
        target_path = target
        target_database_sha256 = sha256(target.read_bytes()).hexdigest()
        validation = type("Validation", (), {"valid": True})()

    controller = PanelController(tmp_path, tmp_path / "config.local.json", migration_func=lambda *_: Result())
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb"})
    assert controller.migrate_duckdb_import({"target_path": "migrated.duckdb"})["source_duckdb_path"] == str(target.resolve())
    with pytest.raises(ValueError, match="different"):
        controller.migrate_duckdb_import({"target_path": "migrated.duckdb"})


def test_http_duckdb_import_settings_and_preflight_are_dedicated_routes(tmp_path: Path) -> None:
    def migrate(_: Path, target: Path) -> object:
        target.write_bytes(b"migrated")
        return type("Migration", (), {"validation": type("Validation", (), {"valid": True})(), "target_database_sha256": sha256(target.read_bytes()).hexdigest()})()
    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=lambda _: ImportPreflight("token", 0, 5, "digest"), migration_func=migrate)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        settings = {"source_duckdb_path": "source.duckdb", "audit_root": "audit", "default_html_root": "html"}
        connection.request("POST", "/api/duckdb-import/settings", json.dumps(settings).encode(), {"Content-Type": "application/json"})
        saved = connection.getresponse(); assert saved.status == 200; saved.read()
        connection.request("GET", "/api/duckdb-import/settings")
        loaded = connection.getresponse(); assert loaded.status == 200
        assert json.loads(loaded.read())["workers"] == 4
        connection.request("POST", "/api/duckdb-import/preflight", json.dumps({"root_path": "html"}).encode(), {"Content-Type": "application/json"})
        response = connection.getresponse(); assert response.status == 200
        assert json.loads(response.read())["token"] == "token"
        connection.request("POST", "/api/duckdb-import/migrate", json.dumps({"target_path": "migrated.duckdb"}).encode(), {"Content-Type": "application/json"})
        migrated = connection.getresponse(); assert migrated.status == 200
        assert json.loads(migrated.read())["source_duckdb_path"] == str((tmp_path / "migrated.duckdb").resolve())
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_http_ui_exposes_persistent_import_settings_and_migration_controls(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse(); parser = _ImportUiParser()
        parser.feed(response.read().decode("utf-8"))
        assert response.status == 200
        assert {"import_source_duckdb", "import_analysis_duckdb", "import_default_html_root", "import_audit_root", "import_workers", "import_batch_size", "migration_target"} <= parser.input_ids
        assert {"saveDuckdbSettings()", "migrateDuckdb()"} <= parser.actions
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_completed_import_revalidates_evidence_before_status_and_download(tmp_path: Path) -> None:
    result = _import_result(tmp_path)
    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=lambda _: ImportPreflight("token", 1, 5, "digest"), import_func=lambda *_: result)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "audit_root": "audit"})
    controller.duckdb_import_preflight({"root_path": "html"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "token"})
    assert _wait_import_finished(controller)["safe_to_delete"] == "YES"
    result.checklist_path.write_text("{}", encoding="utf-8")
    status = controller.snapshot()["duckdb_import"]
    assert status["final_state"] != "COMMITTED"
    assert status["safe_to_delete"] == "NO"
    assert status["artifacts"] == {}
    assert controller.artifact("import_checklist") is None


def test_artifact_response_serves_the_bytes_that_passed_evidence_validation(tmp_path: Path) -> None:
    result = _import_result(tmp_path)
    expected = result.checklist_path.read_bytes()
    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=lambda _: ImportPreflight("token", 1, 5, "digest"), import_func=lambda *_: result)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "audit_root": "audit"})
    controller.duckdb_import_preflight({"root_path": "html"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "token"})
    assert _wait_import_finished(controller)["safe_to_delete"] == "YES"
    original_artifact = controller.artifact
    def mutate_after_validation(name: str) -> object:
        approved = original_artifact(name)
        result.checklist_path.write_bytes(b"tampered-after-validation")
        return approved
    controller.artifact = mutate_after_validation  # type: ignore[method-assign]
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/api/artifact?name=import_checklist")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == expected
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_generic_artifact_http_response_remains_path_streamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "results.csv"; expected = b"header\nrow\n"; artifact.write_bytes(expected)
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    controller._job = _Job("generic", "tester-run", (), {"output_csv": artifact}, {"output_csv": None}, status="SUCCEEDED")
    original_read_bytes = Path.read_bytes
    def reject_bulk_read(path: Path) -> bytes:
        if path == artifact:
            raise AssertionError("generic artifact must remain streamed")
        return original_read_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", reject_bulk_read)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/api/artifact?name=output_csv")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == expected
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_settings_change_during_migration_survives_source_activation(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"; source.write_bytes(b"source")
    target = tmp_path / "target.duckdb"
    started = __import__("threading").Event(); release = __import__("threading").Event()
    def migrate(_: Path, destination: Path) -> object:
        started.set(); assert release.wait(1)
        destination.write_bytes(b"target")
        return type("Migration", (), {"validation": type("Validation", (), {"valid": True})(), "target_database_sha256": sha256(destination.read_bytes()).hexdigest()})()
    controller = PanelController(tmp_path, tmp_path / "config.local.json", migration_func=migrate)
    controller.duckdb_import_settings({"source_duckdb_path": str(source), "workers": 4})
    migration = __import__("threading").Thread(target=lambda: controller.migrate_duckdb_import({"target_path": str(target)}))
    settings = __import__("threading").Thread(target=lambda: controller.duckdb_import_settings({"workers": 9}))
    migration.start(); assert started.wait(1); settings.start(); release.set()
    migration.join(2); settings.join(2)
    final = controller.duckdb_import_settings()
    assert final["source_duckdb_path"] == str(target.resolve())
    assert final["workers"] == 9


@pytest.mark.parametrize("result_error", [False, True])
def test_import_status_never_exposes_absolute_paths_from_errors(tmp_path: Path, result_error: bool) -> None:
    secret = tmp_path / "html" / "report.html"
    def importer(*_: object) -> ImportJobResult:
        if not result_error:
            raise RuntimeError(f"cannot open {secret}")
        result = _import_result(tmp_path, final_state="FAILED")
        return replace(result, error=f"cannot open {secret}")
    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=lambda _: ImportPreflight("token", 1, 5, "digest"), import_func=importer)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "audit_root": "audit"})
    controller.duckdb_import_preflight({"root_path": "html"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "token"})
    document = _wait_import_finished(controller)
    assert document["error"] is not None
    assert str(tmp_path) not in document["error"]


def test_controller_builds_shell_free_tester_command_and_captures_log(
    tmp_path: Path,
) -> None:
    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=_FakeProcess,
    )

    controller.start(
        "tester-run",
        {
            "config": "config.json",
            "strategies": "generated/strategies",
            "output_csv": "results/test.csv",
        },
    )
    snapshot = _wait_finished(controller)

    job = snapshot["job"]
    assert job["status"] == "SUCCEEDED"
    assert job["logs"] == ["started", "finished"]
    assert job["command"][1:4] == ["-m", "mrs3.cli", "tester-run"]
    assert job["command"][-2:] == [
        "--output-csv",
        str((tmp_path / "results/test.csv").resolve()),
    ]


def test_controller_builds_source_csv_command_without_tester_config(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    controller.start("source-csv", {"config": "config.json", "input_csv": "a.csv;b.csv", "start": "2026-07-15T00:00:00Z", "end": "2026-08-06T00:00:00Z", "output_dir": "package"})
    job = _wait_finished(controller)["job"]

    assert job["command"][1:4] == ["-m", "mrs3.cli", "source-csv"]
    assert job["command"].count("--input-csv") == 2
    assert "--config" in job["command"]


def test_controller_builds_duckdb_command_with_optional_html_verification(
    tmp_path: Path,
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    command, _ = controller._build_command(
        "source-duckdb",
        {
            "config": "config.json",
            "database": "source.duckdb",
            "start": "2026-07-15T00:00:00Z",
            "end": "2026-08-06T00:00:00Z",
            "output_dir": "package",
            "verify_html_root": "html",
            "verification_sample_count": "4",
        },
    )

    assert command[command.index("--verify-html-root") + 1] == str((tmp_path / "html").resolve())
    assert command[command.index("--verification-sample-count") + 1] == "4"


@pytest.mark.parametrize("sample_count", ["", "two", "2", "6", 3.5, None])
def test_controller_rejects_invalid_duckdb_verification_sample_count_before_launch(
    tmp_path: Path, sample_count: object
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    with pytest.raises(ValueError, match="verification_sample_count"):
        controller._build_command(
            "source-duckdb",
            {
                "config": "config.json",
                "database": "source.duckdb",
                "start": "2026-07-15T00:00:00Z",
                "end": "2026-08-06T00:00:00Z",
                "output_dir": "package",
                "verify_html_root": "html",
                "verification_sample_count": sample_count,
            },
        )


def test_controller_selects_verified_source_package_without_raw_csv(
    tmp_path: Path,
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    command, _ = controller._build_command(
        "select",
        {
            "config": "config.json",
            "source_package": "verified-package",
            "dates": "dates.xlsx",
            "template": "template.json",
            "side": "LONG",
            "output_dir": "output",
        },
    )

    assert "--source-package" in command
    assert str((tmp_path / "verified-package").resolve()) in command
    assert "--input-csv" not in command


def test_controller_keeps_compatibility_raw_csv_selection(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    command, _ = controller._build_command(
        "select",
        {
            "config": "config.json",
            "input_csv": "input.csv",
            "dates": "dates.xlsx",
            "template": "template.json",
            "side": "LONG",
            "output_dir": "output",
        },
    )

    assert "--input-csv" in command
    assert "--source-package" not in command


@pytest.mark.parametrize(
    "source_payload",
    [{}, {"input_csv": "input.csv", "source_package": "package"}],
)
def test_controller_rejects_select_without_exactly_one_source(
    tmp_path: Path, source_payload: dict[str, str]
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    with pytest.raises(ValueError, match="exactly one"):
        controller._build_command(
            "select",
            {
                "config": "config.json",
                "dates": "dates.xlsx",
                "template": "template.json",
                "side": "LONG",
                "output_dir": "output",
                **source_payload,
            },
        )


@pytest.mark.parametrize("invalid_source", [None, "   "])
def test_controller_rejects_null_or_blank_select_source(
    tmp_path: Path, invalid_source: str | None
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    with pytest.raises(ValueError, match="exactly one"):
        controller._build_command(
            "select",
            {
                "config": "config.json",
                "input_csv": invalid_source,
                "dates": "dates.xlsx",
                "template": "template.json",
                "side": "LONG",
                "output_dir": "output",
            },
        )


def test_controller_rejects_parallel_jobs(tmp_path: Path) -> None:
    class WaitingProcess(_FakeProcess):
        def wait(self) -> int:
            time.sleep(0.2)
            return 0

    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=WaitingProcess,
    )
    payload = {"config": "config.json", "strategies": "strategies"}

    controller.start("tester-plan", payload)

    with pytest.raises(RuntimeError, match="already running"):
        controller.start("tester-plan", payload)


def test_controller_hides_artifacts_left_by_an_older_job(tmp_path: Path) -> None:
    output = tmp_path / "results/test.csv"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    state = output.with_name("test.state.json")
    state.write_text('{"state":"COMPLETED"}', encoding="utf-8")
    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=_FakeProcess,
    )

    controller.start(
        "tester-run",
        {
            "config": "config.json",
            "strategies": "strategies",
            "output_csv": "results/test.csv",
        },
    )
    snapshot = _wait_finished(controller)

    assert snapshot["job"]["workflow"] is None
    assert snapshot["job"]["artifacts"] == {}


def test_dashboard_reports_manifest_counts_but_never_claims_v2_selectable(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    manifest = package / "package_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "package_version": 2,
                "event_mode": "real_independent_events",
                "window_start": "2026-07-15T00:00:00+00:00",
                "window_end": "2026-08-06T00:00:00+00:00",
                "report_count": 12,
                "coverage_accepted_reports": 10,
                "coverage_rejected_reports": 2,
                "point_count": 10,
                "included_cycles": 45,
                "exclusions": {"OPEN_BEFORE_WINDOW": 3, "CLOSE_ON_OR_AFTER_WINDOW": 4},
                "source_summary_status": "VERIFIED",
                "window_metrics_status": "DERIVED_FROM_VERIFIED_SOURCE",
            }
        ),
        encoding="utf-8",
    )
    controller = PanelController(tmp_path, tmp_path / "config.json")
    controller._section_jobs["duckdb"] = _Job(
        job_id="complete-duckdb",
        action="source-duckdb",
        command=(),
        artifacts={"manifest": manifest},
        artifact_baseline={"manifest": None},
        status="SUCCEEDED",
    )

    dashboard = controller.snapshot()["dashboard"]["duckdb"]

    assert dashboard["available"] is True
    assert dashboard["state"] == "VERIFICATION_STATUSES_PRESENT"
    # This deliberately lacks source_summary_samples and the verification CSV.
    # The dashboard is not the package verifier and must never overrule it.
    assert dashboard["state"] != "SELECTABLE"
    assert dashboard["metrics"] == [
        {"label": "Отчёты", "value": 12},
        {"label": "Точки", "value": 10},
        {"label": "Покрытие: принято", "value": 10},
        {"label": "Покрытие: отклонено", "value": 2},
        {"label": "Включено (циклы)", "value": 45},
        {"label": "Исключено (циклы)", "value": 7},
    ]
    assert dashboard["details"] == [
        "real_independent_events · пакет v2",
        "Окно UTC: 2026-07-15T00:00:00+00:00 — 2026-08-06T00:00:00+00:00",
        "Source summary: VERIFIED",
        "Window metrics: DERIVED_FROM_VERIFIED_SOURCE",
    ]
    assert str(package) not in json.dumps(dashboard)


def test_dashboard_reports_candidate_tester_and_posttest_final_artifacts(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "run_manifest.json").write_text(
        json.dumps(
            {
                "event_mode": "real_independent_events",
                "event_eligible_point_count": 11,
                "geometric_plateau_count": 4,
                "ready_plateau_count": 3,
                "ready_structure_count": 2,
                "ready_json_count": 5,
            }
        ),
        encoding="utf-8",
    )
    results = tmp_path / "results.csv"
    results.write_text(
        "strategy_name,total_pnl_pct,max_drawdown_pct\na,2.5,1.2\nb,3.0,2.0\n",
        encoding="utf-8",
    )
    posttest = tmp_path / "posttest"
    posttest.mkdir()
    (posttest / "posttest_manifest.json").write_text(
        json.dumps(
            {"raw_result_count": 2, "pareto_count": 1, "scaled_strategy_count": 1, "target_dd_pct": "5"}
        ),
        encoding="utf-8",
    )
    controller = PanelController(tmp_path, tmp_path / "config.json")
    controller._section_jobs = {
        "candidates": _Job("selected", "select", (), {"manifest": selected / "run_manifest.json"}, {"manifest": None}, status="SUCCEEDED"),
        "tester": _Job("tested", "tester-run", (), {"output_csv": results}, {"output_csv": None}, status="SUCCEEDED"),
        "posttest": _Job("dd5", "posttest", (), {"manifest": posttest / "posttest_manifest.json"}, {"manifest": None}, status="SUCCEEDED"),
    }

    dashboard = controller.snapshot()["dashboard"]

    assert dashboard["candidates"]["state"] == "READY_FOR_TEST"
    assert dashboard["candidates"]["metrics"][-1] == {"label": "JSON для теста", "value": 5}
    assert dashboard["tester"]["state"] == "COMPLETED"
    assert dashboard["tester"]["metrics"] == [
        {"label": "Результаты", "value": 2},
        {"label": "Лучший PnL, %", "value": "3"},
        {"label": "DD лучшего, %", "value": "2"},
        {"label": "Ошибки", "value": 0},
    ]
    assert dashboard["posttest"]["state"] == "RETEST_REQUIRED"
    assert dashboard["posttest"]["metrics"][-1] == {"label": "DD5 JSON", "value": 1}


def test_dashboard_keeps_last_artifact_when_a_later_job_has_none(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    manifest = package / "package_manifest.json"
    manifest.write_text(
        json.dumps(
            {"event_mode": "legacy_trades_proxy", "source_rows": 3, "accepted_rows": 2, "rejected_rows": 1}
        ),
        encoding="utf-8",
    )
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)
    controller._section_jobs["csv"] = _Job(
        "old", "source-csv", (), {"manifest": manifest}, {"manifest": None}, status="SUCCEEDED"
    )

    controller.start("tester-plan", {"config": "config.json", "strategies": "strategies"})
    dashboard = _wait_finished(controller)["dashboard"]

    assert dashboard["csv"]["available"] is True
    assert dashboard["csv"]["metrics"][1] == {"label": "Точки", "value": 2}


def test_panel_rejects_non_loopback_bind(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json")

    with pytest.raises(ValueError, match="loopback"):
        create_panel_server("0.0.0.0", 0, controller)


def test_http_panel_serves_ui_status_and_start_endpoint(tmp_path: Path) -> None:
    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=_FakeProcess,
    )
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        assert response.status == 200
        assert "MRS3 Control Panel" in html
        assert "Каталог JSON-стратегий" in html
        assert "MRS2 · CSV" in html
        assert html.count('<button role="tab"') == 5
        assert html.count('<section role="tabpanel"') == 5
        assert "MRS2 · CSV" in html
        assert "MRS2 · DuckDB" in html
        assert "Кандидаты стратегий" in html
        assert "Анализатор портфелей" in html
        assert "Настройки" in html
        assert "legacy_trades_proxy" in html
        assert "real_independent_events" in html
        assert 'id="verify_html_root"' in html
        assert 'id="verification_sample_count"' in html
        assert "Совместимый CSV-вход (текущий путь)" in html
        assert "Симулятор сетов недоступен" in html
        assert "Рекомендации недоступны" in html
        assert 'data-runnable="true"' in html
        assert "document.querySelectorAll('[data-runnable]')" in html
        assert 'aria-live="polite"' in html
        assert "prefers-reduced-motion: reduce" in html
        assert "prefers-reduced-transparency: reduce" in html
        assert "prefers-contrast: more" in html
        assert "function activateTab" in html
        assert "function browse" in html
        assert "/api/browse" in html
        assert "CSV-файлы" in html

        body = json.dumps(
            {
                "action": "tester-plan",
                "config": "config.json",
                "strategies": "strategies",
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/start",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        started = connection.getresponse()
        document = json.loads(started.read().decode("utf-8"))
        assert started.status == 202
        assert document["job"]["action"] == "tester-plan"

        connection.request("GET", "/api/status")
        status = connection.getresponse()
        status_document = json.loads(status.read().decode("utf-8"))
        assert status.status == 200
        assert status_document["defaults"]["config"] == str(
            (tmp_path / "config.json").resolve()
        )

        connection.request(
            "POST",
            "/api/start",
            body=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        unsupported = connection.getresponse()
        unsupported.read()
        assert unsupported.status == 415
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_panel_browse_returns_only_explicit_native_selection(tmp_path: Path) -> None:
    selected = (tmp_path / "one.csv", tmp_path / "two.csv")
    calls: list[tuple[str, bool]] = []

    def chooser(kind: str, multiple: bool) -> tuple[Path, ...]:
        calls.append((kind, multiple))
        return selected

    controller = PanelController(
        tmp_path,
        tmp_path / "config.json",
        browse_factory=chooser,
    )
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        body = json.dumps({"kind": "csv", "multiple": True}).encode("utf-8")
        connection.request("POST", "/api/browse", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        document = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert calls == [("csv", True)]
        assert document == {"paths": [str(path.resolve()) for path in selected]}

        body = json.dumps({"kind": "unknown", "multiple": False}).encode("utf-8")
        connection.request("POST", "/api/browse", body=body, headers={"Content-Type": "application/json"})
        rejected = connection.getresponse()
        rejected.read()
        assert rejected.status == 400
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
