from __future__ import annotations

import json
from pathlib import Path
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


def test_member_manifest_names_must_match_registered_descriptor_before_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    called = False

    def unexpected_build(*_args: object, **_kwargs: object) -> Path:
        nonlocal called
        called = True
        raise AssertionError("builder must not receive a mismatched member")

    monkeypatch.setattr("mrs3.panel_report_collection.build_single_mode_collection_inbox", unexpected_build)
    with pytest.raises(PanelJobError, match="COLLECTION_MEMBER_NAMES_MISMATCH"):
        service.verify(collection_id)
    assert called is False


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
    assert registry.runtime(collection_id)["performance_v2_import_verified"] is False
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
