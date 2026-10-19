from __future__ import annotations

from http.client import HTTPConnection
import json
from hashlib import sha256
import os
from pathlib import Path
import threading
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from tests._protocol_v2 import use_protocol_v2_target


@pytest.fixture(autouse=True)
def _protocol_v2_database(monkeypatch: pytest.MonkeyPatch):
    """MRS3 evaluation flows need a protocol v2 PerformanceDB (protocol v2 §7a)."""
    return use_protocol_v2_target(monkeypatch)

from mrs3.panel import PanelController, create_panel_server
import mrs3.panel_jobs as panel_jobs_module
from mrs3.panel_report_collection import PanelReportCollection
from mrs3.panel_jobs import PanelJobError
from mrs3.panel_tester_runs import LocalRunsBatchService, _RunsJob


def test_fresh_batch_is_recovered_from_output_after_panel_restart(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "Output" / "strategy_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("mrs3.panel.validate_strategy_manifest", lambda path: SimpleNamespace(
        manifest_path=Path(path), analysis_run_id="a" * 64,
        provenance={"strategy_json_sha256": {"S0.json": "0" * 64}},
    ))

    controller = PanelController(tmp_path, config)

    assert controller.strategies_fresh_batch() == {
        "phase": "COMMITTED", "analysis_run_id": "a" * 64, "strategy_count": 1,
    }


def test_fresh_batch_recovery_ignores_unpublished_and_malformed_directories(
    tmp_path: Path, monkeypatch,
) -> None:
    analysis_id = "a" * 64
    generation_root = tmp_path / "Output" / "fresh-shortlist-v2" / analysis_id
    published = generation_root / ("b" * 64 + "-" + "c" * 32)
    malformed = generation_root / "not-a-published-batch"
    staging = generation_root / ".stage-newest"
    for directory in (published, malformed, staging):
        directory.mkdir(parents=True)
        (directory / "strategy_manifest.json").write_text("{}", encoding="utf-8")
    staging_manifest = staging / "strategy_manifest.json"
    info = staging_manifest.stat()
    os.utime(staging_manifest, ns=(info.st_atime_ns, info.st_mtime_ns + 60_000_000_000))
    monkeypatch.setattr("mrs3.panel.validate_strategy_manifest", lambda path: SimpleNamespace(
        manifest_path=Path(path), analysis_run_id=analysis_id,
    ))
    controller = PanelController(tmp_path, tmp_path / "config.local.json")

    assert controller._fresh_strategy_manifest(analysis_id) == published / "strategy_manifest.json"


def test_tester_job_uses_the_ui_recovery_kind(tmp_path: Path, monkeypatch) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    captured: dict[str, object] = {}
    monkeypatch.setattr(controller, "_fresh_strategy_manifest", lambda _analysis_id: tmp_path / "strategy_manifest.json")
    monkeypatch.setattr(controller, "_start_tracked_panel_job", lambda kind, request, _keys, submit, **_kwargs: captured.update(kind=kind, request=request, submit=submit))
    started: dict[str, object] = {}
    monkeypatch.setattr(controller, "_single_mode_strategy_test", lambda: SimpleNamespace(start=lambda *_args, **kwargs: started.update(kwargs)))
    monkeypatch.setattr(controller, "_manifest_strategy_names", lambda _manifest: pytest.fail("unchecked launch validated collection names"))

    controller.strategies_tester_start({"analysis_run_id": "a" * 64, "start_date": "2026-01-01", "end_date": "2026-01-31"})

    assert captured["kind"] == "strategies.tester.start"
    captured["submit"]("tester-job")
    # Protocol v2 §4.3: the tester runs on the base of its template, no start balance.
    assert "initial_balance" not in started


def test_tester_start_http_rejects_a_start_balance_before_creating_job(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(
            "POST", "/api/v2/jobs",
            json.dumps({"kind": "strategies.tester.start", "request": {
                "analysis_run_id": "a" * 64, "start_date": "2026-01-01", "end_date": "2026-01-31", "initial_balance": 10000,
            }}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert 400 <= response.status < 500
    assert body == {"error": "invalid settings"}
    assert controller._panel_jobs.list() == []


def test_tester_start_http_reports_journal_write_failure_without_ghost_job(tmp_path: Path, monkeypatch) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    monkeypatch.setattr(controller, "_fresh_strategy_manifest", lambda _analysis_id: tmp_path / "strategy_manifest.json")
    tester_started: list[str] = []
    monkeypatch.setattr(
        controller,
        "_single_mode_strategy_test",
        lambda: SimpleNamespace(start=lambda _manifest, **kwargs: tester_started.append(kwargs["job_id"])),
    )

    def fail_replace(_source, destination):
        raise PermissionError(13, "access denied", str(destination))

    monkeypatch.setattr(panel_jobs_module.os, "replace", fail_replace)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(
            "POST", "/api/v2/jobs",
            json.dumps({"kind": "strategies.tester.start", "request": {
                "analysis_run_id": "a" * 64, "start_date": "2026-01-01", "end_date": "2026-01-31",
            }}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.status == 503
    assert body == {"error": {"code": "JOB_PERSISTENCE_FAILED", "message": "Panel could not save the job journal."}}
    assert controller._panel_jobs.list() == []
    assert tester_started == []


def test_tester_start_http_reports_collection_journal_failure(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.local.json"
    config_path.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config_path)
    monkeypatch.setattr(controller, "_fresh_strategy_manifest", lambda _analysis_id: tmp_path / "strategy_manifest.json")
    monkeypatch.setattr(controller, "_manifest_strategy_names", lambda _manifest: ("strategy-a",))
    collection = PanelReportCollection(
        controller._panel_jobs,
        inbox_root=tmp_path / "inbox",
        report_root=tmp_path / "reports",
        trusted_strategy_root=tmp_path / "strategies",
    )
    monkeypatch.setattr(controller, "_report_collection", lambda **_kwargs: collection)
    tester_started: list[str] = []
    monkeypatch.setattr(
        controller,
        "_single_mode_strategy_test",
        lambda: SimpleNamespace(start=lambda _manifest, **kwargs: tester_started.append(kwargs["job_id"])),
    )
    original_replace = panel_jobs_module.os.replace
    calls = []

    def fail_collection_submit(source, destination):
        calls.append(destination)
        if len(calls) == 3:
            raise PermissionError(13, "access denied", str(destination))
        return original_replace(source, destination)

    monkeypatch.setattr(panel_jobs_module.os, "replace", fail_collection_submit)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(
            "POST", "/api/v2/jobs",
            json.dumps({"kind": "strategies.tester.start", "request": {
                "analysis_run_id": "a" * 64, "start_date": "2026-01-01", "end_date": "2026-01-31",
                "collect_reports": True,
            }}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.status == 503
    assert body == {"error": {"code": "JOB_PERSISTENCE_FAILED", "message": "Panel could not save the job journal."}}
    assert not any(job.get("kind") == "strategies.tester.collection" for job in controller._panel_jobs.list())
    assert tester_started == []


@pytest.mark.parametrize("kind", ("strategies.tester.fast.start", "strategies.tester.fast.retry"))
def test_fast_tester_routes_are_removed(tmp_path: Path, kind: str) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    with pytest.raises(PanelJobError, match="UNSUPPORTED_ROUTE"):
        controller.panel_job_submit({
            "kind": kind,
            "request": {},
            "idempotency_key": "removed-fast-route",
            "resource_keys": [],
        })


def test_committed_tester_inbox_readiness_survives_panel_reload(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    job = controller._panel_jobs.submit("strategies.tester.runs", {}, "tester", ("strategies.tester",))
    controller._panel_jobs.transition(job["job_id"], "RUNNING")

    controller._record_special_job({"job_id": job["job_id"], "state": "COMMITTED", "inbox_path": str(tmp_path / "inbox")})

    assert controller._panel_jobs.get(job["job_id"])["inbox_ready"] is True


def test_tester_progress_does_not_rewrite_registry_until_phase_or_evidence_changes(tmp_path: Path, monkeypatch) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    job = controller._panel_jobs.submit("strategies.tester.start", {}, "tester-progress", ("strategies.tester",))
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    saves = 0
    original_save = controller._panel_jobs._save

    def count_save() -> None:
        nonlocal saves
        saves += 1
        original_save()

    monkeypatch.setattr(controller._panel_jobs, "_save", count_save)
    copies = 0
    original_copy = controller._panel_jobs._copy

    def count_copy(value: dict) -> dict:
        nonlocal copies
        copies += 1
        return original_copy(value)

    monkeypatch.setattr(controller._panel_jobs, "_copy", count_copy)
    evidence = {"verified_reports": {"S0": "S0.html"}}
    controller._record_special_job({
        "job_id": job["job_id"], "state": "RUNNING", "phase": "BOT_RUN",
        "progress": {"current": 0, "total": 1}, "evidence": evidence,
    })
    first = saves
    copies_after_first = copies
    controller._record_special_job({
        "job_id": job["job_id"], "state": "RUNNING", "phase": "BOT_RUN",
        "progress": {"current": 0, "total": 1, "active": 1}, "evidence": evidence,
    })
    assert saves == first
    assert copies == copies_after_first
    public = next(item for item in controller.panel_jobs() if item["job_id"] == job["job_id"])
    assert public["progress"]["active"] == 1
    assert public["evidence"]["verified_reports"] == 1

    controller._record_special_job({
        "job_id": job["job_id"], "state": "RUNNING", "phase": "REPORT_COLLECTION",
        "progress": {"current": 1, "total": 1}, "evidence": evidence,
    })
    assert saves == first + 1
    controller._record_special_job({
        "job_id": job["job_id"], "state": "RUNNING", "phase": "REPORT_COLLECTION",
        "progress": {"current": 1, "total": 1},
        "evidence": {"verified_reports": {"S0": "S0.html", "S1": "S1.html"}},
    })
    assert saves == first + 2
    persisted = json.loads(controller._panel_jobs.journal.read_text(encoding="utf-8"))[job["job_id"]]
    assert persisted["phase"] == "REPORT_COLLECTION"
    assert persisted["evidence"]["verified_reports"]["S1"] == "S1.html"


def test_panel_jobs_projects_only_tester_verified_report_maps(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    tester = controller._panel_jobs.submit("strategies.tester.start", {}, "tester-public", ("strategies.tester",))
    source = controller._panel_jobs.submit("source.local-import", {}, "source-public", ("source",))
    controller._panel_jobs.sync(
        tester["job_id"],
        {"state": "QUEUED", "evidence": {"failed_names": ["S0"], "verified_reports": {"S0": "S0.html"}}},
    )
    controller._panel_jobs.sync(
        source["job_id"],
        {"state": "QUEUED", "evidence": {"verified_reports": {"source": "source.html"}}},
    )

    jobs = {item["job_id"]: item for item in controller.panel_jobs()}
    assert jobs[tester["job_id"]]["evidence"] == {"failed_names": ["S0"], "verified_reports": 1}
    assert jobs[source["job_id"]]["evidence"]["verified_reports"] == {"source": "source.html"}
    assert controller._panel_jobs.get(tester["job_id"])["evidence"]["verified_reports"] == {"S0": "S0.html"}


@pytest.mark.parametrize(("source_kind", "source_phase"), (("strategies.tester.native.start", "FAILED"), ("strategies.tester.retry", "BOT_RUN")))
def test_failed_native_tester_can_start_a_tracked_retry(
    tmp_path: Path, source_kind: str, source_phase: str, _protocol_v2_database,
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    source = controller._panel_jobs.submit(
        source_kind, {}, "native-source", ("strategies.tester",),
        runtime=PanelController._protocol_v2_runtime(_protocol_v2_database),
    )
    controller._panel_jobs.transition(source["job_id"], "RUNNING")
    controller._panel_jobs.transition(source["job_id"], "FAILED", phase=source_phase)
    calls: list[tuple[str, str]] = []

    class Service:
        def retry(self, source_job_id: str, *, job_id: str, tester_config_template=None) -> dict[str, object]:
            calls.append((source_job_id, job_id))
            return {"job_id": job_id, "state": "RUNNING", "phase": "RUNNING", "progress": {}}

    controller._single_mode_strategy_test_service = Service()
    result = controller.panel_job_submit({
        "kind": "strategies.tester.retry",
        "request": {"job_id": source["job_id"]},
    })

    assert result["job_id"] != source["job_id"]
    assert calls == [(source["job_id"], result["job_id"])]
    assert controller._panel_jobs.get(result["job_id"])["state"] == "RUNNING"


@pytest.mark.parametrize("kind", ("strategies.tester.start", "strategies.tester.native.start"))
def test_existing_committed_tester_inbox_is_marked_ready_on_panel_reload(tmp_path: Path, monkeypatch, kind: str) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox_root = tmp_path / "inbox"
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=inbox_root))
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit(kind, {}, "tester", job_id="batch-1")
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    inbox = inbox_root / job["job_id"]
    controller._panel_jobs.transition(job["job_id"], "FAILED")
    controller._panel_jobs.recover_committed(job["job_id"], runtime={"inbox_path": str(inbox)})
    monkeypatch.setattr(controller, "_validate_metadata_inbox", lambda _inbox: None)

    controller._reconcile_interrupted_tester_jobs()

    assert controller._panel_jobs.get(job["job_id"])["inbox_ready"] is True


def test_interrupted_single_mode_commit_is_recovered_from_durable_inbox(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox_root = tmp_path / "inbox"
    inbox = inbox_root / "single-job"
    inbox.mkdir(parents=True)
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=inbox_root))
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit(
        "strategies.tester.retry", {}, "single", ("strategies.tester",), job_id="single-job"
    )
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    controller._panel_jobs.transition(job["job_id"], "FAILED", phase="COMMITTED")
    controller._panel_jobs.jobs[job["job_id"]]["error"] = {"code": "INTERRUPTED"}

    monkeypatch.setattr(controller, "_validate_metadata_inbox", lambda path: None if path == inbox else pytest.fail(str(path)))

    controller._reconcile_interrupted_tester_jobs()

    recovered = controller._panel_jobs.get(job["job_id"])
    assert recovered["state"] == "COMMITTED"
    assert recovered["inbox_ready"] is True


def test_interrupted_finalist_retest_releases_resource_and_keeps_frozen_cohort(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "mrs3.panel.RunnerConfig.from_json",
        lambda _path: SimpleNamespace(inbox_root=tmp_path / "inbox"),
    )
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit(
        "strategies.performance.v2.finalist-retest", {"scope": "ALL"}, "bulk-finalist",
        ("strategies.tester", "performance-v2-finalist-retest"), job_id="bulk-finalist",
    )
    controller._panel_jobs.transition(job["job_id"], "RUNNING", phase="BOT_RUN")
    frozen_runtime = {
        "bulk_retest": True,
        "cohort_members": [{"strategy_id": 7, "result_id": 71, "strategy_name": "S7"}],
    }
    controller._panel_jobs.sync(
        job["job_id"], {"state": "RUNNING", "phase": "BOT_RUN"}, runtime=frozen_runtime,
    )
    controller._record_special_job({
        "job_id": job["job_id"], "state": "RUNNING", "phase": "BOT_RUN",
        "progress": {"current": 72, "total": 250, "unit": "reports"},
        "progress_publication_error": "temporary panel journal write failed",
    })
    assert controller._panel_jobs.get(job["job_id"])["progress"]["current"] == 72
    assert controller._panel_jobs.progress_directory.is_dir()

    restarted = PanelController(tmp_path, config)

    recovered = restarted._panel_jobs.get(job["job_id"])
    assert recovered["state"] == "FAILED"
    assert recovered["phase"] == "FAILED"
    assert recovered["error"] == {"code": "INTERRUPTED"}
    assert recovered["progress"]["current"] == 72
    assert "publication_error" not in recovered["progress"]
    assert list(restarted._panel_jobs.progress_directory.glob("*.json")) == []
    assert restarted._panel_jobs.runtime(job["job_id"]) == frozen_runtime
    replacement = restarted._panel_jobs.submit(
        "strategies.tester.start", {}, "after-interrupted-finalist",
        ("strategies.tester",), job_id="after-interrupted-finalist",
    )
    assert replacement["state"] == "QUEUED"
    assert restarted._panel_jobs.transition(replacement["job_id"], "RUNNING")["state"] == "RUNNING"


def test_single_mode_verify_routes_through_existing_performance_inbox_button(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox = tmp_path / "inbox" / "single-job"
    inbox.mkdir(parents=True)
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit(
        "strategies.tester.start", {"mode": "SINGLE_MODE"}, "single", ("strategies.tester",), job_id="single-job"
    )
    controller._panel_jobs.transition(job["job_id"], "RUNNING", phase="COMMITTED")
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=tmp_path / "inbox"))
    def forbidden_full_validation(_inbox: Path) -> None:
        raise AssertionError("verify must defer full validation to the v2 importer")

    monkeypatch.setattr(controller, "_validate_performance_inbox", forbidden_full_validation)

    class SingleModeService:
        def capture_inbox(self, job_id: str) -> Path:
            assert job_id == "single-job"
            return inbox

        def mark_inbox_ready(self, job_id: str, path: Path) -> None:
            assert job_id == "single-job" and path == inbox

        def status(self, job_id: str) -> dict[str, object]:
            return {"job_id": job_id, "state": "COMMITTED", "phase": "COMMITTED", "evidence": {}, "progress": {}, "inbox_path": str(inbox)}

    monkeypatch.setattr(controller, "_single_mode_strategy_test", lambda: SingleModeService())

    result = controller.strategies_tester_verify_inbox("single-job")

    assert result["state"] == "COMMITTED"
    assert result["inbox_ready"] is True
    assert controller._panel_jobs.runtime("single-job")["inbox_path"] == str(inbox)


def test_single_mode_repeated_verify_reuses_the_verified_inbox(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox_root = tmp_path / "inbox"
    inbox = inbox_root / "single-job"
    inbox.mkdir(parents=True)
    (inbox / "inbox_manifest.json").write_text(json.dumps({"batch_id": "single-job"}), encoding="utf-8")
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit(
        "strategies.tester.start", {"mode": "SINGLE_MODE"}, "single", ("strategies.tester",), job_id="single-job"
    )
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    controller._panel_jobs.sync(
        job["job_id"],
        {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True},
        runtime={"inbox_path": str(inbox), "mode": "SINGLE_MODE", "performance_v2_import_verified": False},
    )
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=inbox_root))
    validated: list[Path] = []
    monkeypatch.setattr(controller, "_validate_metadata_inbox", validated.append)
    monkeypatch.setattr(
        controller, "_single_mode_strategy_test", lambda: pytest.fail("verified inbox must not be rebuilt")
    )

    result = controller.strategies_tester_verify_inbox("single-job")

    assert result["state"] == "COMMITTED"
    assert result["inbox_ready"] is True
    assert validated == [inbox.resolve()]
    assert controller._panel_jobs.runtime("single-job")["performance_v2_import_verified"] is True


def test_single_mode_verify_rejects_an_inbox_owned_by_another_job(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox_root = tmp_path / "inbox"
    inbox = inbox_root / "single-job"
    inbox.mkdir(parents=True)
    (inbox / "inbox_manifest.json").write_text(json.dumps({"batch_id": "other-job"}), encoding="utf-8")
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit(
        "strategies.tester.start", {"mode": "SINGLE_MODE"}, "single", ("strategies.tester",), job_id="single-job"
    )
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    controller._panel_jobs.sync(
        job["job_id"],
        {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True},
        runtime={"inbox_path": str(inbox), "mode": "SINGLE_MODE", "performance_v2_import_verified": True},
    )
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=inbox_root))
    captured: list[str] = []

    class SingleModeService:
        def capture_inbox(self, job_id: str) -> Path:
            captured.append(job_id)
            return inbox

        def mark_inbox_ready(self, job_id: str, path: Path) -> None:
            assert job_id == "single-job" and path == inbox

        def status(self, job_id: str) -> dict[str, object]:
            return {"job_id": job_id, "state": "COMMITTED", "phase": "COMMITTED", "evidence": {}, "progress": {}, "inbox_path": str(inbox)}

    monkeypatch.setattr(controller, "_single_mode_strategy_test", lambda: SingleModeService())

    with pytest.raises(ValueError, match="belongs to another job"):
        controller.strategies_tester_verify_inbox("single-job")

    assert captured == []


def test_pre_v2_native_tester_job_rebuilds_its_metadata_inbox(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox = tmp_path / "inbox" / "native-job"
    inbox.mkdir(parents=True)
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit(
        "strategies.tester.native.start", {}, "native", ("strategies.tester",), job_id="native-job"
    )
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    controller._panel_jobs.transition(job["job_id"], "COMMITTED")
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=tmp_path / "inbox"))

    class SingleModeService:
        def capture_inbox(self, job_id: str, *, force_single_mode: bool = False) -> Path:
            assert job_id == "native-job"
            assert force_single_mode is True
            return inbox

        def mark_inbox_ready(self, job_id: str, path: Path) -> None:
            assert job_id == "native-job" and path == inbox

        def status(self, job_id: str) -> dict[str, object]:
            return {"job_id": job_id, "state": "COMMITTED", "phase": "COMMITTED", "evidence": {}, "progress": {}, "inbox_path": str(inbox)}

    monkeypatch.setattr(controller, "_single_mode_strategy_test", lambda: SingleModeService())

    result = controller.strategies_tester_verify_inbox("native-job")

    assert result["state"] == "COMMITTED"
    assert result["inbox_ready"] is True


def test_committed_retest_without_inbox_uses_explicit_capture(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox = tmp_path / "inbox" / "retest-job"
    inbox.mkdir(parents=True)
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit(
        "strategies.tester.native.start", {"retest": True}, "native", ("strategies.tester",), job_id="retest-job"
    )
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    controller._panel_jobs.sync(job["job_id"], {"state": "COMMITTED", "phase": "COMMITTED"}, runtime={"retest": True})
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=tmp_path / "inbox"))
    captured: list[tuple[str, bool]] = []

    class SingleModeService:
        def capture_inbox(self, job_id: str, *, force_single_mode: bool = False) -> Path:
            captured.append((job_id, force_single_mode))
            return inbox

        def mark_inbox_ready(self, job_id: str, path: Path) -> None:
            assert job_id == "retest-job" and path == inbox

        def status(self, job_id: str) -> dict[str, object]:
            return {"job_id": job_id, "state": "COMMITTED", "phase": "COMMITTED", "evidence": {}, "progress": {}, "inbox_path": str(inbox)}

    monkeypatch.setattr(controller, "_single_mode_strategy_test", lambda: SingleModeService())

    result = controller.strategies_tester_verify_inbox("retest-job")

    assert captured == [("retest-job", True)]
    assert result["inbox_ready"] is True


def test_runs_tester_rejects_an_empty_runs_directory(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    bot_root = tmp_path / "bot"
    (bot_root / "tester" / "runs").mkdir(parents=True)
    (bot_root / "run_tester.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(bot_root=bot_root))
    controller = PanelController(tmp_path, config)

    try:
        controller.strategies_tester_runs_start({})
    except ValueError as error:
        assert str(error) == "RUNS_EMPTY"
    else:
        raise AssertionError("empty runs directory must be rejected")


def test_runs_tester_retains_old_reports_and_counts_only_fresh_html(tmp_path: Path) -> None:
    root = tmp_path / "bot"
    runs = root / "tester" / "runs"; runs.mkdir(parents=True)
    entries = []
    for index, name in enumerate(("one", "two"), start=1):
        filename = f"{index:03d}.json"; snapshot = {"settings": [{"name": name, "exchange": {"name": "Bybit"}}]}
        path = runs / filename; path.write_text(json.dumps(snapshot), encoding="utf-8")
        entries.append({"filename": filename, "strategy_name": name, "snapshot_sha256": sha256(path.read_bytes()).hexdigest(), "strategy_sha256": sha256(json.dumps(snapshot["settings"][0], sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
    manifest = {"schema_version": 1, "analysis_run_id": "a" * 64, "test_start": "2026-08-01", "test_end": "2026-08-18", "entries": entries}
    manifest["generation_manifest_sha256"] = sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (root / "tester" / "runs_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    tester_config = root / "tester" / "config_tester.json"; tester_config.write_text("{}", encoding="utf-8")
    (root / "run_tester.bat").write_text("@echo off\n", encoding="utf-8")
    report = root / "tester" / "report" / "my_test_runs"; report.mkdir(parents=True)
    (report / "old.html").write_text("old", encoding="utf-8")

    class Process:
        def poll(self): return 0
        def terminate(self): raise AssertionError("completed process must not be terminated")

    def launch(_root):
        for name in ("one", "two"):
            (report / f"{name}.html").write_text(f'<pre>{{"name":"{name}","basic":{{"symbol":"ONUSDT"}},"exchange":{{"name":"Bybit"}}}}</pre>', encoding="utf-8")
        return Process()

    inbox = tmp_path / "inbox"; inbox.mkdir()
    service = LocalRunsBatchService(SimpleNamespace(bot_root=root, tester_config=tester_config), launcher=launch, capture_inbox=lambda *_args, **_kwargs: inbox)
    service.start("runs-job")
    deadline = monotonic() + 3
    while service.status("runs-job")["state"] == "RUNNING" and monotonic() < deadline:
        sleep(0.01)
    result = service.status("runs-job")
    assert result["state"] == "COMMITTED"
    assert result["inbox_path"] == str(inbox)
    assert result["progress"] == {"current": 2, "total": 2, "unit": "reports"}
    assert (report / "old.html").read_text(encoding="utf-8") == "old"


def test_runs_terminal_state_is_hidden_until_target_is_finalized(tmp_path: Path) -> None:
    service = LocalRunsBatchService(SimpleNamespace())
    job = _RunsJob(
        "job", tmp_path, 0, tmp_path / "reports", {}, {}, "2026-08-01",
        "2026-08-31", b"{}", state="COMMITTED", phase="COMMITTED",
    )

    assert service._document(job)["state"] == "RUNNING"
    job.target_finalized = True
    assert service._document(job)["state"] == "COMMITTED"


def test_runs_and_regular_tester_share_the_panel_job_resource(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"; config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit("strategies.tester.start", {}, "regular", ("strategies.tester",))
    controller._panel_jobs.transition(job["job_id"], "RUNNING")

    try:
        controller.strategies_tester_runs_start({})
    except PanelJobError as error:
        assert error.code == "RESOURCE_BUSY"
    else:
        raise AssertionError("RUNS must not overlap the ordinary tester batch")


def test_completed_tester_batch_is_recovered_after_panel_restart(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox_root = tmp_path / "inbox"
    monkeypatch.setattr(
        "mrs3.panel.RunnerConfig.from_json",
        lambda _path: SimpleNamespace(
            inbox_root=inbox_root,
            strategy_dir=tmp_path / "strategies",
            report_dir=tmp_path / "reports",
        ),
    )
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit("strategies.tester.start", {"analysis_run_id": "a" * 64}, "tester", job_id="batch-1")
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    inbox = inbox_root / job["job_id"]
    inbox.mkdir(parents=True)
    (inbox / "inbox_manifest.json").write_text(json.dumps({
        "schema_version": 1, "entries": [{"strategy_name": "one"}], "v6_provenance": {"analysis_run_id": "a" * 64},
    }), encoding="utf-8")
    (inbox_root / f"{job['job_id']}.state.json").write_text(json.dumps({
        "state": "COMPLETED", "expected_names": ["one"], "inbox_path": str(inbox),
    }), encoding="utf-8")

    restarted = PanelController(tmp_path, config)

    assert restarted.strategies_tester_status(job["job_id"])["state"] == "FAILED"


def test_verified_tester_batch_finishes_inbox_capture_after_restart(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox_root = tmp_path / "inbox"
    monkeypatch.setattr(
        "mrs3.panel.RunnerConfig.from_json",
        lambda _path: SimpleNamespace(
            inbox_root=inbox_root,
            strategy_dir=tmp_path / "strategies",
            report_dir=tmp_path / "reports",
        ),
    )
    monkeypatch.setattr("mrs3.panel.plan_batch", lambda *_args, **_kwargs: SimpleNamespace(resume_remaining_names=()))
    inbox = inbox_root / "batch-1"
    monkeypatch.setattr("mrs3.panel.run_batch", lambda *_args, **_kwargs: SimpleNamespace(inbox_path=inbox))
    controller = PanelController(tmp_path, config)
    job = controller._panel_jobs.submit("strategies.tester.start", {"analysis_run_id": "a" * 64}, "tester", job_id="batch-1")
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    inbox_root.mkdir()
    incomplete_inbox = inbox_root / "batch-1"
    incomplete_inbox.mkdir()
    (incomplete_inbox / "partial.html").write_text("partial", encoding="utf-8")
    (inbox_root / "batch-1.state.json").write_text(json.dumps({
        "state": "STOPPED_FOR_CLEANUP", "strategy_source": str(tmp_path / "strategies"),
        "output_csv": str(inbox_root / "batch-1.csv"), "v6_provenance": {"analysis_run_id": "a" * 64},
    }), encoding="utf-8")

    restarted = PanelController(tmp_path, config)
    deadline = monotonic() + 1
    while restarted._panel_jobs.get(job["job_id"])["state"] != "COMMITTED" and monotonic() < deadline:
        sleep(0.01)

    assert restarted._panel_jobs.get(job["job_id"])["state"] == "COMMITTED"
    assert not (incomplete_inbox / "partial.html").exists()
