from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest

from mrs3.panel_jobs import PanelJobError, PanelJobRegistry
import mrs3.panel as panel_module
from mrs3.panel import PanelController
from mrs3.panel_report_collection import PanelReportCollection


def _registry(tmp_path: Path) -> PanelJobRegistry:
    return PanelJobRegistry(tmp_path / ".panel-jobs.json", recover_on_load=False)


def _tester(registry: PanelJobRegistry, job_id: str, *, state: str = "COMMITTED", inbox: Path | None = None) -> None:
    registry.submit(
        "strategies.tester.start",
        {"analysis_run_id": "run", "mode": "SINGLE_MODE"},
        f"panel:{job_id}",
        ("strategies.tester",),
        job_id=job_id,
    )
    if state == "RUNNING":
        registry.transition(job_id, "RUNNING")
    elif state == "COMMITTED":
        registry.transition(job_id, "RUNNING")
        registry.transition(job_id, "COMMITTED")
    elif state in {"FAILED", "CANCELLED"}:
        registry.transition(job_id, "FAILED" if state == "FAILED" else "CANCELLED")
    if inbox is not None:
        runtime = {"inbox_path": str(inbox)}
        registry.sync(job_id, {"state": state, "inbox_ready": state == "COMMITTED"}, runtime=runtime)


def test_register_persists_explicit_membership_before_worker_and_restart_recovers(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _tester(registry, "job-1", state="RUNNING")
    service = PanelReportCollection(
        registry,
        inbox_root=tmp_path / "inbox",
        report_root=tmp_path / "reports",
        trusted_strategy_root=tmp_path / "strategies",
    )

    collection_id = service.register("job-1", ["S1", "S2"])

    assert registry.runtime("job-1")["report_collection_id"] == collection_id
    status = service.status()
    assert status["collection_id"] == collection_id
    assert status["state"] == "OPEN"
    assert status["total_registered_packs"] == 1
    assert status["active_packs"] == 1

    restored = PanelReportCollection(
        PanelJobRegistry(registry.journal, recover_on_load=False),
        inbox_root=tmp_path / "inbox",
        report_root=tmp_path / "reports",
        trusted_strategy_root=tmp_path / "strategies",
    )
    assert restored.status()["collection_id"] == collection_id
    assert restored.status()["active_packs"] == 1


def test_unchecked_jobs_and_failed_members_do_not_contribute_and_retry_replaces_source(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    source_inbox = tmp_path / "source"
    retry_inbox = tmp_path / "retry"
    _tester(registry, "failed", state="FAILED")
    _tester(registry, "unregistered", state="COMMITTED", inbox=tmp_path / "unregistered")
    service = PanelReportCollection(registry, inbox_root=tmp_path / "inbox", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    collection_id = service.register("failed", ["S1"])
    _tester(registry, "retry", state="COMMITTED", inbox=retry_inbox)
    service.register("retry", ["S1"], replaces_job_id="failed")

    status = service.status()
    assert status["collection_id"] == collection_id
    assert status["failed_cancelled_packs"] == 0
    assert status["committed_packs"] == 1
    assert status["exact_committed_report_count"] == 0
    assert "unregistered" not in {member["tester_job_id"] for member in status["members"]}


def test_active_member_blocks_verify_and_clear_does_not_delete_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(tmp_path)
    inbox = tmp_path / "member"
    inbox.mkdir()
    marker = inbox / "inbox_manifest.json"
    marker.write_text("member", encoding="utf-8")
    _tester(registry, "job-1", state="RUNNING", inbox=inbox)
    service = PanelReportCollection(registry, inbox_root=tmp_path / "inbox", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    collection_id = service.register("job-1", ["S1"])
    with pytest.raises(PanelJobError, match="COLLECTION_ACTIVE_MEMBERS"):
        service.verify(collection_id)
    registry.transition("job-1", "FAILED")
    before = marker.read_bytes()
    result = service.clear(collection_id)
    assert result["state"] == "CLEARED"
    assert marker.read_bytes() == before


def test_verified_generation_is_immutable_and_checked_append_opens_successor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(tmp_path)
    first = tmp_path / "first"
    first.mkdir()
    (first / "inbox_manifest.json").write_text(json.dumps({"expected_strategy_names": ["S1"], "entries": [{"strategy_name": "S1"}]}), encoding="utf-8")
    _tester(registry, "job-1", state="COMMITTED", inbox=first)
    service = PanelReportCollection(registry, inbox_root=tmp_path / "inbox", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    first_id = service.register("job-1", ["S1"])
    verified = tmp_path / "verified"
    verified.mkdir()
    (verified / "inbox_manifest.json").write_text(json.dumps({"collection_id": first_id}), encoding="utf-8")

    monkeypatch.setattr("mrs3.panel_report_collection.build_single_mode_collection_inbox", lambda *args, **kwargs: verified)
    assert service.verify(first_id) == verified
    assert service.status()["state"] == "VERIFIED"
    _tester(registry, "job-2", state="RUNNING")
    second_id = service.register("job-2", ["S2"])
    assert second_id != first_id
    assert service.status()["collection_id"] == second_id
    assert service.status()["state"] == "OPEN"
    assert registry.runtime(first_id)["collection_state"] == "VERIFIED"


def test_failed_import_keeps_verified_snapshot_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(tmp_path)
    member = tmp_path / "member"
    member.mkdir()
    (member / "inbox_manifest.json").write_text(json.dumps({"expected_strategy_names": ["S1"], "entries": [{"strategy_name": "S1"}]}), encoding="utf-8")
    _tester(registry, "job-1", state="COMMITTED", inbox=member)
    service = PanelReportCollection(registry, inbox_root=tmp_path / "inbox", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    collection_id = service.register("job-1", ["S1"])
    verified = tmp_path / "verified"
    verified.mkdir()
    (verified / "inbox_manifest.json").write_text(json.dumps({"collection_id": collection_id}), encoding="utf-8")
    monkeypatch.setattr("mrs3.panel_report_collection.build_single_mode_collection_inbox", lambda *args, **kwargs: verified)
    service.verify(collection_id)
    runtime = registry.runtime(collection_id)
    runtime["performance_v2_import_verified"] = False
    registry.sync(collection_id, {"state": "COMMITTED"}, runtime=runtime)
    assert service.status()["state"] == "VERIFIED"
    assert service.verify(collection_id) == verified
    assert registry.runtime(collection_id)["performance_v2_import_verified"] is True


def test_verified_manifest_tamper_and_mismatched_id_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(tmp_path)
    member = tmp_path / "member"
    member.mkdir()
    (member / "inbox_manifest.json").write_text(json.dumps({"expected_strategy_names": ["S1"], "entries": [{"strategy_name": "S1"}]}), encoding="utf-8")
    _tester(registry, "job-1", state="COMMITTED", inbox=member)
    service = PanelReportCollection(registry, inbox_root=tmp_path / "inbox", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    collection_id = service.register("job-1", ["S1"])
    verified = tmp_path / "verified"
    verified.mkdir()

    def publish_wrong_id(*_args: object, **_kwargs: object) -> Path:
        (verified / "inbox_manifest.json").write_text(json.dumps({"collection_id": "other"}), encoding="utf-8")
        return verified

    monkeypatch.setattr("mrs3.panel_report_collection.build_single_mode_collection_inbox", publish_wrong_id)
    with pytest.raises(PanelJobError, match="COLLECTION_MANIFEST_ID_MISMATCH"):
        service.verify(collection_id)

    (verified / "inbox_manifest.json").write_text(json.dumps({"collection_id": collection_id}), encoding="utf-8")
    monkeypatch.setattr("mrs3.panel_report_collection.build_single_mode_collection_inbox", lambda *args, **kwargs: verified)
    service.verify(collection_id)
    (verified / "inbox_manifest.json").write_text(json.dumps({"collection_id": collection_id, "changed": True}), encoding="utf-8")
    with pytest.raises(PanelJobError, match="COLLECTION_VERIFIED_INBOX_TAMPERED"):
        service.verify(collection_id)


def test_panel_passes_registered_names_to_exact_collection_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(tmp_path)
    member = tmp_path / "member"
    member.mkdir()
    (member / "inbox_manifest.json").write_text(
        json.dumps({"expected_strategy_names": ["S2"], "entries": [{"strategy_name": "S2"}]}),
        encoding="utf-8",
    )
    _tester(registry, "job-1", state="COMMITTED", inbox=member)
    service = PanelReportCollection(registry, inbox_root=tmp_path / "inbox", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    collection_id = service.register("job-1", ["S1"])
    verified = tmp_path / "verified"
    verified.mkdir()
    (verified / "inbox_manifest.json").write_text(
        json.dumps({"collection_id": collection_id}), encoding="utf-8"
    )
    captured: dict[str, object] = {}

    def capture_build(*_args: object, **kwargs: object) -> Path:
        captured.update(kwargs)
        return verified

    monkeypatch.setattr("mrs3.panel_report_collection.build_single_mode_collection_inbox", capture_build)
    service.verify(collection_id)
    assert captured["expected_member_names"] == [["S1"]]


def test_retry_persists_source_supersession_and_retest_source_never_inherits_collection(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _tester(registry, "failed", state="FAILED")
    _tester(registry, "retry", state="RUNNING")
    service = PanelReportCollection(registry, inbox_root=tmp_path / "inbox", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    collection_id = service.register("failed", ["S1"])
    service.register("retry", ["S1"], replaces_job_id="failed")
    members = registry.runtime(collection_id)["members"]
    source = next(member for member in members if member["tester_job_id"] == "failed")
    assert source["superseded"] is True
    assert source["superseded_by"] == "retry"

    controller = PanelController(tmp_path / "retest", tmp_path / "retest" / "config.local.json")
    controller._panel_jobs.submit(
        "strategies.tester.native.start", {"retest": True}, "panel:retest", ("strategies.tester",), job_id="retest"
    )
    controller._panel_jobs.transition("retest", "FAILED")
    runtime = controller._panel_jobs.runtime("retest")
    runtime["retest"] = True
    runtime["report_collection_id"] = "malicious-collection"
    controller._panel_jobs.sync("retest", {"state": "FAILED"}, runtime=runtime)
    with pytest.raises(PanelJobError, match="RETEST"):
        controller.strategies_tester_retry({"job_id": "retest"})


def test_controller_collection_verify_then_import_binds_exact_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(tmp_path)
    member = tmp_path / "member"
    member.mkdir()
    (member / "inbox_manifest.json").write_text(json.dumps({"expected_strategy_names": ["S1"], "entries": [{"strategy_name": "S1"}]}), encoding="utf-8")
    _tester(registry, "job-1", state="COMMITTED", inbox=member)
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    service = PanelReportCollection(registry, inbox_root=tmp_path / "inbox", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    controller._panel_jobs = registry
    controller._report_collection_service = service
    collection_id = service.register("job-1", ["S1"])
    verified = tmp_path / "verified"
    verified.mkdir()
    (verified / "inbox_manifest.json").write_text(json.dumps({"collection_id": collection_id}), encoding="utf-8")
    monkeypatch.setattr("mrs3.panel_report_collection.build_single_mode_collection_inbox", lambda *args, **kwargs: verified)
    monkeypatch.setattr(panel_module.RunnerConfig, "from_json", lambda _path: SimpleNamespace(
        inbox_root=tmp_path / "inbox", report_dir=tmp_path / "reports", strategy_dir=tmp_path / "strategies", bot_root=tmp_path / "bot"
    ))
    assert controller.strategies_tester_verify_inbox(collection_id)["state"] == "VERIFIED"

    monkeypatch.setattr(controller, "_validate_metadata_inbox", lambda _path: None)
    monkeypatch.setattr(controller, "_performance_v2_config", lambda: SimpleNamespace(workers=1))
    monkeypatch.setattr(controller, "_initialize_missing_performance_v2_target", lambda _config: None)
    monkeypatch.setattr(controller, "_output_strategy_root", lambda: tmp_path / "Output")
    dates = tmp_path / "dates.xlsx"
    dates.write_bytes(b"dates")
    monkeypatch.setattr(controller, "_workflow_default", lambda _name, **_kwargs: dates)
    monkeypatch.setattr(panel_module, "PerformanceV2PanelRequest", lambda **kwargs: kwargs)
    captured: dict[str, object] = {}

    class StubJobs:
        def start(self, request: object, *, job_id: str | None = None) -> dict[str, object]:
            captured["request"] = request
            return {"job_id": job_id, "state": "RUNNING", "phase": "RUNNING"}

    controller._performance_v2_jobs = StubJobs()  # type: ignore[assignment]
    result = controller.strategies_performance_v2_import({"tester_job_id": collection_id})
    assert result["job_id"]
    assert captured["request"]["inbox"] == verified  # type: ignore[index]
    assert captured["request"]["expected_inbox_manifest_sha256"] == sha256(  # type: ignore[index]
        (verified / "inbox_manifest.json").read_bytes()
    ).hexdigest()
    assert captured["request"]["expected_collection_id"] == collection_id  # type: ignore[index]
    assert registry.runtime(collection_id)["import_in_progress"] == result["job_id"]
    controller._record_special_job({"job_id": result["job_id"], "state": "FAILED", "phase": "FAILED", "error": {"code": "IMPORT_FAILED"}})
    assert registry.runtime(collection_id)["collection_state"] == "VERIFIED"
    assert registry.runtime(collection_id)["performance_v2_import_verified"] is True

    class FailingJobs:
        def start(self, _request: object, *, job_id: str | None = None) -> dict[str, object]:
            raise RuntimeError("worker start failed")

    controller._performance_v2_jobs = FailingJobs()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="worker start failed"):
        controller.strategies_performance_v2_import({"tester_job_id": collection_id})
    assert registry.runtime(collection_id)["collection_state"] == "VERIFIED"
    assert registry.runtime(collection_id)["performance_v2_import_verified"] is True
    (verified / "inbox_manifest.json").write_text(json.dumps({"collection_id": collection_id, "changed": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="explicit inbox verification"):
        controller.strategies_performance_v2_import({"tester_job_id": collection_id})


def test_controller_registers_checked_job_before_worker_callback_and_rejects_non_boolean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    registry = controller._panel_jobs
    collection = PanelReportCollection(
        registry,
        inbox_root=tmp_path / "inbox",
        report_root=tmp_path / "reports",
        trusted_strategy_root=tmp_path / "strategies",
    )
    controller._report_collection_service = collection
    manifest = tmp_path / "strategy_manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(controller, "_fresh_strategy_manifest", lambda _analysis_id: manifest)
    monkeypatch.setattr(
        panel_module,
        "validate_strategy_manifest",
        lambda _path: SimpleNamespace(provenance={"strategy_json_sha256": {"S1.json": "a" * 64}}),
    )
    events: list[object] = []

    class FakeService:
        def start(self, _manifest, *, analysis_run_id, start_date, end_date, job_id, **_kwargs):
            runtime = registry.runtime(job_id)
            events.append(runtime.get("report_collection_id"))
            return {"job_id": job_id, "state": "RUNNING", "phase": "RUNNING"}

    controller._single_mode_strategy_test_service = FakeService()
    result = controller.strategies_tester_start({
        "analysis_run_id": "run",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "collect_reports": True,
    })
    assert events and isinstance(events[0], str)
    assert registry.runtime(result["job_id"])["report_collection_id"] == events[0]
    with pytest.raises(ValueError, match="collect_reports"):
        controller.strategies_tester_start({
            "analysis_run_id": "run",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "collect_reports": 1,
        })


def test_verify_revision_conflict_preserves_new_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(tmp_path)
    first = tmp_path / "first"
    first.mkdir()
    (first / "inbox_manifest.json").write_text(json.dumps({"entries": [{"strategy_name": "S1"}]}), encoding="utf-8")
    _tester(registry, "job-1", state="COMMITTED", inbox=first)
    service = PanelReportCollection(registry, inbox_root=tmp_path / "collections", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    collection_id = service.register("job-1", ["S1"])
    entered = Barrier(2)
    release = Barrier(2)
    verified = tmp_path / "verified"
    verified.mkdir()
    (verified / "inbox_manifest.json").write_text(json.dumps({"collection_id": collection_id}), encoding="utf-8")

    def blocked_build(*_args: object, **_kwargs: object) -> Path:
        entered.wait(timeout=2)
        release.wait(timeout=2)
        return verified

    monkeypatch.setattr("mrs3.panel_report_collection.build_single_mode_collection_inbox", blocked_build)
    errors: list[BaseException] = []
    worker = Thread(target=lambda: _capture_error(errors, service.verify, collection_id))
    worker.start()
    entered.wait(timeout=2)
    _tester(registry, "job-2", state="COMMITTED", inbox=first)
    service.register("job-2", ["S2"])
    release.wait(timeout=2)
    worker.join(timeout=2)

    assert errors and isinstance(errors[0], PanelJobError)
    assert errors[0].code == "COLLECTION_CHANGED_DURING_VERIFY"
    assert [member["tester_job_id"] for member in registry.runtime(collection_id)["members"]] == ["job-1", "job-2"]


def test_clear_revision_conflict_cannot_be_overwritten_by_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(tmp_path)
    member = tmp_path / "member"
    member.mkdir()
    (member / "inbox_manifest.json").write_text(json.dumps({"entries": [{"strategy_name": "S1"}]}), encoding="utf-8")
    _tester(registry, "job-1", state="COMMITTED", inbox=member)
    service = PanelReportCollection(registry, inbox_root=tmp_path / "collections", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    collection_id = service.register("job-1", ["S1"])
    entered = Barrier(2)
    release = Barrier(2)
    verified = tmp_path / "verified"
    verified.mkdir()
    (verified / "inbox_manifest.json").write_text(json.dumps({"collection_id": collection_id}), encoding="utf-8")

    def blocked_build(*_args: object, **_kwargs: object) -> Path:
        entered.wait(timeout=2)
        release.wait(timeout=2)
        return verified

    monkeypatch.setattr("mrs3.panel_report_collection.build_single_mode_collection_inbox", blocked_build)
    errors: list[BaseException] = []
    worker = Thread(target=lambda: _capture_error(errors, service.verify, collection_id))
    worker.start()
    entered.wait(timeout=2)
    service.clear(collection_id)
    release.wait(timeout=2)
    worker.join(timeout=2)

    assert errors and isinstance(errors[0], PanelJobError)
    assert errors[0].code == "COLLECTION_CHANGED_DURING_VERIFY"
    assert registry.runtime(collection_id)["collection_state"] == "CLEARED"


def _capture_error(errors: list[BaseException], function: object, *args: object) -> None:
    try:
        function(*args)  # type: ignore[operator]
    except BaseException as error:
        errors.append(error)


def test_import_claim_blocks_clear_and_failed_completion_releases_for_retry(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    service = PanelReportCollection(registry, inbox_root=tmp_path / "collections", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    _tester(registry, "job-1", state="COMMITTED", inbox=tmp_path / "member")
    collection_id = service.register("job-1", ["S1"])
    inbox = tmp_path / "verified"
    inbox.mkdir()
    (inbox / "inbox_manifest.json").write_text(json.dumps({"collection_id": collection_id}), encoding="utf-8")
    runtime = registry.runtime(collection_id)
    runtime.update({
        "collection_state": "VERIFIED",
        "verified_inbox_path": str(inbox),
        "verified_inbox_sha256": sha256((inbox / "inbox_manifest.json").read_bytes()).hexdigest(),
        "performance_v2_import_verified": True,
    })
    registry.sync(collection_id, {"state": "COMMITTED", "inbox_ready": True}, runtime=runtime)

    service.claim_import(collection_id, "import-1")
    with pytest.raises(PanelJobError, match="IMPORT_IN_PROGRESS"):
        service.clear(collection_id)
    service.finish_import(collection_id, "import-1", committed=False)
    assert registry.runtime(collection_id)["collection_state"] == "VERIFIED"
    assert registry.runtime(collection_id)["performance_v2_import_verified"] is True
    service.claim_import(collection_id, "import-2")
    service.finish_import(collection_id, "import-2", committed=True)
    assert registry.runtime(collection_id)["collection_state"] == "IMPORTED"


def _claimed_collection(tmp_path: Path, *, import_state: str = "RUNNING") -> tuple[PanelJobRegistry, PanelReportCollection, str]:
    registry = _registry(tmp_path)
    member = tmp_path / "member"
    member.mkdir()
    _tester(registry, "job-1", state="COMMITTED", inbox=member)
    service = PanelReportCollection(registry, inbox_root=tmp_path / "collections", report_root=tmp_path / "reports", trusted_strategy_root=tmp_path / "strategies")
    collection_id = service.register("job-1", ["S1"])
    inbox = tmp_path / "verified"
    inbox.mkdir()
    (inbox / "inbox_manifest.json").write_text(json.dumps({"collection_id": collection_id}), encoding="utf-8")
    runtime = registry.runtime(collection_id)
    runtime.update({
        "collection_state": "VERIFIED",
        "verified_inbox_path": str(inbox),
        "verified_inbox_sha256": sha256((inbox / "inbox_manifest.json").read_bytes()).hexdigest(),
        "performance_v2_import_verified": True,
    })
    registry.sync(collection_id, {"state": "COMMITTED", "inbox_ready": True}, runtime=runtime)
    registry.submit("strategies.performance.v2.import", {"tester_job_id": collection_id}, "panel:import-1", ("performance-v2-db",), job_id="import-1")
    registry.transition("import-1", "RUNNING")
    if import_state == "COMMITTED":
        registry.transition("import-1", "COMMITTED")
    service.claim_import(collection_id, "import-1")
    return registry, service, collection_id


def test_restart_releases_orphaned_claim_and_preserves_verified_retryability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, _service, collection_id = _claimed_collection(tmp_path)
    monkeypatch.setattr(panel_module.RunnerConfig, "from_json", lambda _path: SimpleNamespace(
        inbox_root=tmp_path / "inbox", report_dir=tmp_path / "reports", strategy_dir=tmp_path / "strategies", bot_root=tmp_path / "bot"
    ))

    restarted = PanelController(tmp_path, tmp_path / "config.local.json")
    restarted._report_collection().status()

    runtime = restarted._panel_jobs.runtime(collection_id)
    assert runtime["collection_state"] == "VERIFIED"
    assert runtime["performance_v2_import_verified"] is True
    assert "import_in_progress" not in runtime


def test_restart_resolves_persisted_committed_import_to_imported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry_before, _service, collection_id = _claimed_collection(tmp_path, import_state="COMMITTED")
    monkeypatch.setattr(panel_module.RunnerConfig, "from_json", lambda _path: SimpleNamespace(
        inbox_root=tmp_path / "inbox", report_dir=tmp_path / "reports", strategy_dir=tmp_path / "strategies", bot_root=tmp_path / "bot"
    ))

    restarted = PanelController(tmp_path, tmp_path / "config.local.json")
    restarted._report_collection().status()

    runtime = restarted._panel_jobs.runtime(collection_id)
    assert runtime["collection_state"] == "IMPORTED"
    assert "import_in_progress" not in runtime


def test_live_claim_is_preserved_during_lazy_reconciliation(tmp_path: Path) -> None:
    _registry_before, service, collection_id = _claimed_collection(tmp_path)

    service.reconcile_import_claims(live_import_job_ids={"import-1"})

    runtime = service.registry.runtime(collection_id)
    assert runtime["import_in_progress"] == "import-1"
    assert runtime["performance_v2_import_verified"] is False


def test_pending_claim_survives_reconciliation_before_worker_start(tmp_path: Path) -> None:
    registry, service, collection_id = _claimed_collection(tmp_path)
    controller = PanelController(tmp_path / "controller", tmp_path / "controller" / "config.local.json")
    controller._panel_jobs = registry
    controller._report_collection_service = service
    controller._collection_import_claim_ids.add("import-1")

    controller._report_collection()
    assert registry.runtime(collection_id)["import_in_progress"] == "import-1"

    controller._record_special_job({"job_id": "import-1", "state": "COMMITTED", "phase": "COMMITTED", "result": {"status": "COMMITTED"}})
    assert controller._panel_jobs.get("import-1")["state"] == "COMMITTED"
    assert "import-1" not in controller._collection_import_claim_ids
    assert registry.runtime(collection_id)["collection_state"] == "IMPORTED"
    assert "import_in_progress" not in registry.runtime(collection_id)


def test_pending_claim_is_not_released_by_concurrent_reconcile(tmp_path: Path, monkeypatch) -> None:
    registry, service, collection_id = _claimed_collection(tmp_path)
    service.release_import_claim(collection_id, "import-1")
    controller = PanelController(tmp_path / "controller", tmp_path / "controller" / "config.local.json")
    controller._panel_jobs = registry
    controller._report_collection_service = service
    entered, release, claim_done = Event(), Event(), Event()
    errors: list[BaseException] = []
    real_reconcile = service.reconcile_import_claims

    def paused_reconcile(*, live_import_job_ids=()):
        entered.set()
        if not release.wait(5):
            raise AssertionError("reconcile barrier timed out")
        return real_reconcile(live_import_job_ids=live_import_job_ids)

    def claim():
        with controller._lock:
            controller._collection_import_claim_ids.add("import-1")
        service.claim_import(collection_id, "import-1")
        claim_done.set()

    monkeypatch.setattr(service, "reconcile_import_claims", paused_reconcile)
    access_thread = Thread(target=lambda: _capture_error(errors, controller._report_collection))
    claim_thread = Thread(target=lambda: _capture_error(errors, claim))
    access_thread.start()
    try:
        assert entered.wait(5)
        claim_thread.start()
        completed_early = claim_done.wait(0.5)
    finally:
        release.set()
        access_thread.join(5)
        if claim_thread.ident is not None:
            claim_thread.join(5)
    assert not access_thread.is_alive() and not claim_thread.is_alive()
    assert not errors
    assert not completed_early
    assert registry.runtime(collection_id)["import_in_progress"] == "import-1"


def test_terminal_pending_id_is_pruned_before_reconciliation(tmp_path: Path) -> None:
    registry, service, collection_id = _claimed_collection(tmp_path)
    controller = PanelController(tmp_path / "controller", tmp_path / "controller" / "config.local.json")
    controller._panel_jobs = registry
    controller._report_collection_service = service
    controller._collection_import_claim_ids.add("import-1")
    registry.transition("import-1", "FAILED")

    controller._report_collection()
    assert "import-1" not in controller._collection_import_claim_ids
    runtime = registry.runtime(collection_id)
    assert "import_in_progress" not in runtime
    assert runtime["performance_v2_import_verified"] is True


def test_committed_pending_id_is_completed_before_terminal_callback(tmp_path: Path) -> None:
    registry, service, collection_id = _claimed_collection(tmp_path, import_state="COMMITTED")
    controller = PanelController(tmp_path / "controller", tmp_path / "controller" / "config.local.json")
    controller._panel_jobs = registry
    controller._report_collection_service = service
    controller._collection_import_claim_ids.add("import-1")

    controller._report_collection()
    assert "import-1" not in controller._collection_import_claim_ids
    runtime = registry.runtime(collection_id)
    assert runtime["collection_state"] == "IMPORTED"
    assert "import_in_progress" not in runtime
    controller._record_special_job({"job_id": "import-1", "state": "COMMITTED", "phase": "COMMITTED", "result": {"status": "COMMITTED"}})
    assert registry.runtime(collection_id)["collection_state"] == "IMPORTED"


def test_concurrent_first_collection_access_constructs_one_service(tmp_path: Path, monkeypatch) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    entered, release, twice = Event(), Event(), Event()
    calls: list[int] = []
    results: list[PanelReportCollection] = []
    errors: list[BaseException] = []

    def slow_config(_path):
        calls.append(1)
        entered.set()
        if len(calls) == 2:
            twice.set()
        if not release.wait(5):
            raise AssertionError("collection construction barrier timed out")
        return SimpleNamespace(inbox_root=tmp_path / "inbox", report_dir=tmp_path / "reports")

    def access():
        results.append(controller._report_collection())

    monkeypatch.setattr(panel_module.RunnerConfig, "from_json", slow_config)
    first = Thread(target=lambda: _capture_error(errors, access))
    second = Thread(target=lambda: _capture_error(errors, access))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        called_twice = twice.wait(0.5)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert not called_twice
    assert len(calls) == 1
    assert results == [controller._report_collection_service] * 2


def test_controller_reconciliation_preserves_local_live_import_worker(tmp_path: Path) -> None:
    _registry_before, service, collection_id = _claimed_collection(tmp_path)
    controller = PanelController(tmp_path / "controller", tmp_path / "controller" / "config.local.json")
    controller._report_collection_service = service

    class LiveJobs:
        def active_job_ids(self) -> set[str]:
            return {"import-1"}

    controller._performance_v2_jobs = LiveJobs()  # type: ignore[assignment]
    controller._report_collection().status()

    runtime = service.registry.runtime(collection_id)
    assert runtime["import_in_progress"] == "import-1"
    assert runtime["performance_v2_import_verified"] is False


def test_reconciliation_accepts_iterable_live_ids_and_fails_closed_on_probe_error(tmp_path: Path) -> None:
    registry, service, collection_id = _claimed_collection(tmp_path)
    controller = PanelController(tmp_path / "controller", tmp_path / "controller" / "config.local.json")
    controller._panel_jobs = registry
    controller._report_collection_service = service

    class ListJobs:
        def active_job_ids(self):
            return ["import-1"]

    controller._performance_v2_jobs = ListJobs()  # type: ignore[assignment]
    controller._report_collection()
    assert registry.runtime(collection_id)["import_in_progress"] == "import-1"

    class BrokenJobs:
        def active_job_ids(self):
            raise RuntimeError("worker probe failed")

    controller._performance_v2_jobs = BrokenJobs()  # type: ignore[assignment]
    assert controller._report_collection() is service
    assert registry.runtime(collection_id)["import_in_progress"] == "import-1"

    controller._performance_v2_jobs = SimpleNamespace()  # type: ignore[assignment]
    assert controller._report_collection() is service
    assert registry.runtime(collection_id)["import_in_progress"] == "import-1"


def test_terminal_import_reconciles_failed_collection_finish_before_next_claim(tmp_path: Path, monkeypatch) -> None:
    registry, service, collection_id = _claimed_collection(tmp_path)
    controller = PanelController(tmp_path / "controller", tmp_path / "controller" / "config.local.json")
    controller._panel_jobs = registry
    controller._report_collection_service = service
    real_finish = service.finish_import

    def fail_finish(*_args, **_kwargs):
        raise PanelJobError("injected collection finish failure")

    monkeypatch.setattr(service, "finish_import", fail_finish)
    controller._record_special_job({"job_id": "import-1", "state": "FAILED", "phase": "FAILED", "error": None})
    assert registry.get("import-1")["state"] == "FAILED"
    assert registry.runtime(collection_id)["import_in_progress"] == "import-1"

    monkeypatch.setattr(service, "finish_import", real_finish)
    controller._report_collection()
    runtime = registry.runtime(collection_id)
    assert "import_in_progress" not in runtime
    assert runtime["collection_state"] == "VERIFIED"
    assert runtime["performance_v2_import_verified"] is True
    service.claim_import(collection_id, "import-2")
    assert registry.runtime(collection_id)["import_in_progress"] == "import-2"


def test_cancelled_import_completion_releases_collection_claim(tmp_path: Path) -> None:
    registry, service, collection_id = _claimed_collection(tmp_path)
    controller = PanelController(tmp_path / "controller", tmp_path / "controller" / "config.local.json")
    controller._panel_jobs = registry
    controller._report_collection_service = service

    controller._record_special_job({
        "job_id": "import-1", "state": "CANCELLED", "phase": "CANCELLED",
        "error": None,
    })

    runtime = registry.runtime(collection_id)
    assert runtime["collection_state"] == "VERIFIED"
    assert runtime["performance_v2_import_verified"] is True
    assert "import_in_progress" not in runtime
