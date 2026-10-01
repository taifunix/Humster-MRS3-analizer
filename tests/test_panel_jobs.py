from datetime import datetime
from copy import deepcopy
import json
import threading
import mrs3.panel_jobs as panel_jobs_module

import pytest

from mrs3.panel_jobs import PanelJobError, PanelJobRegistry


def test_registry_idempotency_collision_capacity_and_restart(tmp_path):
    path = tmp_path / "jobs.json"; registry = PanelJobRegistry(path, capacity=1)
    first = registry.submit("testing.local", {"x": 1}, "same", ("write:out",))
    assert registry.submit("testing.local", {"x": 1}, "same", ("write:out",))["job_id"] == first["job_id"]
    try: registry.submit("other", {}, "other", ("write:out",))
    except PanelJobError as error: assert error.code == "JOB_CAPACITY_EXHAUSTED"
    restarted = PanelJobRegistry(path, capacity=1)
    assert restarted.get(first["job_id"])["error"]["code"] == "INTERRUPTED"


def test_registry_can_defer_restart_recovery_until_owner_migrates(tmp_path):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path, recover_on_load=False)
    job = registry.submit("testing.local", {}, "deferred")

    assert registry.get(job["job_id"])["state"] == "QUEUED"
    assert registry.recover_interrupted() is True
    assert registry.get(job["job_id"])["state"] == "FAILED"
    assert registry.recover_interrupted() is False


def test_registry_recovery_is_durable_before_identical_terminal_import_poll(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path)
    job = registry.submit("strategies.performance.v2.import", {}, "recover-import")
    registry.transition(job["job_id"], "RUNNING")

    replacements = []
    original_replace = panel_jobs_module.os.replace

    def count_replace(source, destination):
        if destination == path:
            replacements.append((source, destination))
        return original_replace(source, destination)

    monkeypatch.setattr(panel_jobs_module.os, "replace", count_replace)
    recovered = PanelJobRegistry(path)
    snapshot = recovered.get(job["job_id"])
    assert snapshot["state"] == "FAILED"
    assert snapshot["error"] == {"code": "INTERRUPTED"}
    assert len(replacements) == 1
    assert recovered._journal_dirty is False

    recovered.sync(job["job_id"], snapshot, skip_save_if_unchanged=True)

    assert len(replacements) == 1
    assert json.loads(path.read_text(encoding="utf-8"))[job["job_id"]] == recovered.jobs[job["job_id"]]


def test_registry_rejected_runtime_reservation_leaves_journal_clean(tmp_path):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path)
    job = registry.submit("strategies.performance.v2.import", {}, "bad-reservation")
    before = deepcopy(registry.jobs[job["job_id"]])

    with pytest.raises(TypeError):
        registry.reserve_runtime(job["job_id"], "bad", object())

    assert registry.jobs[job["job_id"]] == before
    assert registry._journal_dirty is False


def test_registry_cancel_transition_and_bounded_logs(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("source.local-import", {}, "a")
    assert registry.transition(job["job_id"], "RUNNING")["state"] == "RUNNING"
    for index in range(205): registry.append_log(job["job_id"], str(index))
    assert len(registry.get(job["job_id"])["logs"]) == 200
    assert registry.cancel(job["job_id"])["state"] == "CANCELLING"


def test_registry_discards_only_queued_job(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    queued = registry.submit("kind", {}, "queued")
    registry.discard_queued(queued["job_id"])
    with pytest.raises(PanelJobError, match="NOT_FOUND"):
        registry.get(queued["job_id"])

    running = registry.submit("kind", {}, "running")
    registry.transition(running["job_id"], "RUNNING")
    with pytest.raises(PanelJobError, match="DISCARD_NOT_ALLOWED"):
        registry.discard_queued(running["job_id"])


def test_registry_rejects_invalid_request_and_illegal_transition(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    for payload in (("", {}, "key", ()), ("testing.local", [], "key", ()), ("testing.local", {}, "", ())):
        try:
            registry.submit(*payload)
        except PanelJobError as error:
            assert error.code == "INVALID_REQUEST"
        else:
            raise AssertionError("invalid job request was accepted")
    job = registry.submit("testing.local", {}, "valid")
    try:
        registry.transition(job["job_id"], "COMMITTED")
    except PanelJobError as error:
        assert error.code == "INVALID_REQUEST"
    else:
        raise AssertionError("queued job skipped RUNNING")


def test_registry_accepts_controller_assigned_job_id_for_a_specialized_worker(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")

    job = registry.submit("source.local-import", {}, "special", job_id="worker-job")

    assert job["job_id"] == "worker-job"
    assert PanelJobRegistry(tmp_path / "jobs.json").get("worker-job")["error"] == {"code": "INTERRUPTED"}


def test_registry_syncs_worker_completion_and_keeps_runtime_private(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("strategies.tester", {}, "worker", job_id="worker-job")
    registry.transition(job["job_id"], "RUNNING")

    saved = registry.sync(
        "worker-job",
        {
            "state": "COMMITTED",
            "phase": "COMMITTED",
            "progress": {"current": 1, "total": 1},
            "evidence": {"safe_to_delete": "YES", "source_content_digest": "a" * 64},
        },
        runtime={"inbox_path": "private"},
    )

    assert saved["state"] == "COMMITTED"
    assert saved["evidence"]["safe_to_delete"] == "YES"
    assert "runtime" not in saved
    assert PanelJobRegistry(tmp_path / "jobs.json").runtime("worker-job") == {"inbox_path": "private"}


def test_registry_copy_removes_runtime_before_deep_copy(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("testing.local", {}, "copy-order")
    registry.jobs[job["job_id"]]["runtime"] = {"private": object()}

    copied = registry.get(job["job_id"])

    assert "runtime" not in copied


def test_registry_volatile_sync_guards_progress_and_rejects_invalid_states(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("strategies.tester", {}, "volatile")
    registry.transition(job["job_id"], "RUNNING", phase="ACTIVE")
    registry.sync(
        job["job_id"],
        {
            "state": "RUNNING",
            "phase": "ACTIVE",
            "progress": {"current": 1, "total": 3},
            "error": None,
            "evidence": {"verified_reports": {"strategy": "private.html"}},
        },
    )
    before = registry.get(job["job_id"])

    registry.volatile_sync(
        job["job_id"],
        {"state": "RUNNING", "phase": "ACTIVE", "progress": {"current": 2, "total": 3}},
        expected=registry._peek(job["job_id"]),
    )
    after = registry.get(job["job_id"])
    assert after["progress"] == {"current": 2, "total": 3}
    assert {key: value for key, value in after.items() if key != "progress"} == {
        key: value for key, value in before.items() if key != "progress"
    }

    registry.volatile_sync(
        job["job_id"],
        {"state": "RUNNING", "phase": "ACTIVE", "progress": {"current": 3, "total": 3}},
        expected={"state": "QUEUED", "phase": "ACTIVE", "error": None, "evidence": before["evidence"]},
    )
    assert registry.get(job["job_id"])["progress"] == {"current": 2, "total": 3}
    with pytest.raises(PanelJobError, match="INVALID_REQUEST"):
        registry.volatile_sync(job["job_id"], {"state": "QUEUED"})
    with pytest.raises(PanelJobError, match="NOT_FOUND"):
        registry.volatile_sync("missing", {"state": "RUNNING"})


def test_registry_volatile_sync_ignores_same_phase_progress_regression(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("strategies.performance.v2.import", {}, "monotonic")
    registry.transition(job["job_id"], "RUNNING", phase="PARSING")

    registry.volatile_sync(
        job["job_id"],
        {"state": "RUNNING", "phase": "PARSING", "progress": {"current": 136, "total": 2070, "unit": "reports"}},
    )
    registry.volatile_sync(
        job["job_id"],
        {"state": "RUNNING", "phase": "PARSING", "progress": {"current": 119, "total": 2070, "unit": "reports"}},
    )

    assert registry.get(job["job_id"])["progress"]["current"] == 136


def test_registry_coalesces_one_hundred_live_updates_until_terminal_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path)
    job = registry.submit("strategies.performance.v2.import", {}, "coalesced", job_id="coalesced")
    registry.transition(job["job_id"], "RUNNING", phase="PARSING")
    replacements = []
    real_replace = panel_jobs_module.os.replace
    monkeypatch.setattr(
        panel_jobs_module.os,
        "replace",
        lambda source, destination: (replacements.append(destination), real_replace(source, destination))[1],
    )

    for current in range(1, 101):
        registry.volatile_sync(
            job["job_id"],
            {"state": "RUNNING", "phase": "PARSING", "progress": {"current": current, "total": 100, "unit": "reports"}},
        )

    assert replacements == []
    assert registry.get(job["job_id"])["progress"]["current"] == 100
    registry.sync(
        job["job_id"],
        {"state": "COMMITTED", "phase": "COMMITTED", "progress": {"current": 100, "total": 100, "unit": "reports"}},
    )

    assert replacements == [path]
    restored = PanelJobRegistry(path)
    assert restored.get(job["job_id"])["state"] == "COMMITTED"
    assert restored.get(job["job_id"])["progress"]["current"] == 100


def test_registry_public_list_hides_verified_report_filenames(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("strategies.tester.native.start", {}, "public")
    registry.sync(
        job["job_id"],
        {
            "state": "RUNNING",
            "phase": "RUNNING",
            "progress": {"current": 0, "total": 1},
            "evidence": {"verified_reports": {"strategy": "private-report.html"}},
        },
    )

    assert registry.list()[0]["evidence"]["verified_reports"] == {"strategy": "private-report.html"}
    assert registry.public_list()[0]["evidence"]["verified_reports"] == 1


def test_registry_persists_creation_timestamp_for_restored_jobs(tmp_path):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path)
    first = registry.submit("strategies.tester.native.start", {"retest": True}, "old", job_id="z-old")
    second = registry.submit("strategies.tester.native.start", {"retest": True}, "new", job_id="a-new")

    restored = PanelJobRegistry(path)

    assert datetime.fromisoformat(first["created_at_utc"]) <= datetime.fromisoformat(second["created_at_utc"])
    assert restored.get("z-old")["created_at_utc"] == first["created_at_utc"]
    assert restored.get("a-new")["created_at_utc"] == second["created_at_utc"]


def test_registry_terminal_sync_skips_three_identical_normalized_payloads(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path)
    job = registry.submit("strategies.performance.v2.import", {}, "terminal", job_id="terminal")
    registry.transition(job["job_id"], "RUNNING")
    status = {
        "state": "COMMITTED", "phase": "COMMITTED",
        "progress": {"current": 1, "total": 1, "unit": "items"},
        "error": None, "evidence": {"safe_to_delete": "YES"},
        "result": {"imported_count": 1}, "inbox_ready": True,
    }
    runtime = {"inbox_path": "private/inbox", "nested": {"value": 1}}
    registry.sync(job["job_id"], status, runtime=runtime)
    replacements = []
    real_replace = panel_jobs_module.os.replace
    monkeypatch.setattr(panel_jobs_module.os, "replace", lambda source, destination: (replacements.append((source, destination)), real_replace(source, destination))[1])

    for _ in range(3):
        registry.sync(job["job_id"], status, runtime=runtime, skip_save_if_unchanged=True)

    assert replacements == []
    assert PanelJobRegistry(path).get(job["job_id"])["result"] == {"imported_count": 1}


def test_registry_filtered_load_forces_next_identical_sync_to_persist(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path, recover_on_load=False)
    job = registry.submit("strategies.performance.v2.import", {}, "load-filter", job_id="load-filter")
    registry.transition(job["job_id"], "RUNNING")
    status = {"state": "COMMITTED", "phase": "COMMITTED"}
    registry.sync(job["job_id"], status)
    disk = json.loads(path.read_text(encoding="utf-8"))
    disk["invalid"] = {"job_id": "invalid"}
    path.write_text(json.dumps(disk), encoding="utf-8")
    loaded = PanelJobRegistry(path, recover_on_load=False)
    replacements = []
    real_replace = panel_jobs_module.os.replace
    monkeypatch.setattr(panel_jobs_module.os, "replace", lambda source, destination: (replacements.append(destination), real_replace(source, destination))[1])

    loaded.sync(job["job_id"], status, skip_save_if_unchanged=True)

    assert replacements == [path]
    assert "invalid" not in json.loads(path.read_text(encoding="utf-8"))
    assert loaded._journal_dirty is False


def test_registry_terminal_sync_changed_payload_saves_once_and_preserves_identity(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path)
    job = registry.submit("strategies.performance.v2.import", {}, "changed", job_id="changed")
    registry.transition(job["job_id"], "RUNNING")
    status = {"state": "COMMITTED", "phase": "COMMITTED", "result": {"imported_count": 1}}
    registry.sync(job["job_id"], status, runtime={"inbox_path": "private"})
    identity = registry.jobs[job["job_id"]]
    replacements = []
    real_replace = panel_jobs_module.os.replace
    monkeypatch.setattr(panel_jobs_module.os, "replace", lambda source, destination: (replacements.append(destination), real_replace(source, destination))[1])

    registry.sync(job["job_id"], {**status, "result": {"imported_count": 2}}, runtime={"inbox_path": "private"}, skip_save_if_unchanged=True)

    assert len(replacements) == 1
    assert registry.jobs[job["job_id"]] is identity
    assert PanelJobRegistry(path).get(job["job_id"])["result"] == {"imported_count": 2}


def test_registry_default_sync_saves_and_failed_save_keeps_memory_ahead_and_dirty(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path)
    job = registry.submit("strategies.performance.v2.import", {}, "retry", job_id="retry")
    registry.transition(job["job_id"], "RUNNING")
    status = {"state": "COMMITTED", "phase": "COMMITTED", "result": {"imported_count": 3}}
    registry.sync(job["job_id"], status)
    real_replace = panel_jobs_module.os.replace
    failed = True
    captured_sources = []

    def fail_once(source, destination):
        nonlocal failed
        captured_sources.append(source)
        if failed:
            failed = False
            raise OSError("replace failed")
        return real_replace(source, destination)

    monkeypatch.setattr(panel_jobs_module.os, "replace", fail_once)
    with pytest.raises(OSError, match="replace failed"):
        registry.sync(job["job_id"], {**status, "result": {"imported_count": 4}})
    assert registry.get(job["job_id"])["result"] == {"imported_count": 4}
    assert registry._journal_dirty is True

    registry.sync(job["job_id"], {**status, "result": {"imported_count": 4}}, skip_save_if_unchanged=True)
    assert registry._journal_dirty is False
    assert PanelJobRegistry(path).get(job["job_id"])["result"] == {"imported_count": 4}
    assert all(not source.exists() for source in captured_sources)


def test_registry_default_sync_same_payload_still_replaces(tmp_path, monkeypatch):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("strategies.performance.v2.import", {}, "legacy-save", job_id="legacy-save")
    registry.transition(job["job_id"], "RUNNING")
    status = {"state": "COMMITTED", "phase": "COMMITTED", "result": {"imported_count": 1}}
    registry.sync(job["job_id"], status)
    replacements = []
    real_replace = panel_jobs_module.os.replace
    monkeypatch.setattr(panel_jobs_module.os, "replace", lambda source, destination: (replacements.append(destination), real_replace(source, destination))[1])

    registry.sync(job["job_id"], status)

    assert len(replacements) == 1


def test_registry_sync_wrapper_failure_leaves_memory_ahead_for_retry(tmp_path, monkeypatch):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("strategies.performance.v2.import", {}, "wrapper-failure", job_id="wrapper-failure")
    registry.transition(job["job_id"], "RUNNING")
    status = {"state": "COMMITTED", "phase": "COMMITTED", "result": {"imported_count": 1}}
    registry.sync(job["job_id"], status)
    real_save = registry._save
    monkeypatch.setattr(registry, "_save", lambda: (_ for _ in ()).throw(OSError("save wrapper failed")))

    with pytest.raises(OSError, match="save wrapper failed"):
        registry.sync(job["job_id"], {**status, "result": {"imported_count": 2}})
    assert registry.get(job["job_id"])["result"] == {"imported_count": 2}
    assert registry._journal_dirty is True

    monkeypatch.setattr(registry, "_save", real_save)
    registry.sync(job["job_id"], {**status, "result": {"imported_count": 2}}, skip_save_if_unchanged=True)
    assert PanelJobRegistry(registry.journal).get(job["job_id"])["result"] == {"imported_count": 2}


def test_registry_volatile_actual_change_marks_dirty_but_identical_invalid_and_stale_do_not(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("strategies.tester", {}, "volatile-dirty", job_id="volatile-dirty")
    registry.transition(job["job_id"], "RUNNING", phase="ACTIVE")
    registry.sync(job["job_id"], {"state": "RUNNING", "phase": "ACTIVE", "progress": {"current": 1, "total": 3}})
    registry.volatile_sync(job["job_id"], {"state": "RUNNING", "phase": "ACTIVE", "progress": {"current": 2, "total": 3}})
    assert registry._journal_dirty is True
    registry._save()
    assert registry._journal_dirty is False
    registry.volatile_sync(job["job_id"], {"state": "RUNNING", "phase": "ACTIVE", "progress": {"current": 2, "total": 3}})
    assert registry._journal_dirty is False
    before = deepcopy(registry.jobs[job["job_id"]])
    with pytest.raises(PanelJobError, match="INVALID_REQUEST"):
        registry.volatile_sync(job["job_id"], {
            "state": "QUEUED", "phase": "INVALID", "progress": {"current": 3},
            "evidence": {"nested": {"value": 1}},
        })
    assert registry.jobs[job["job_id"]] == before
    assert registry._journal_dirty is False
    registry.volatile_sync(job["job_id"], {
        "state": "RUNNING", "phase": "INVALID", "progress": {"current": 3, "total": 3},
        "evidence": {"nested": {"value": 2}},
    }, expected={"state": "QUEUED"})
    assert registry.jobs[job["job_id"]] == before
    assert registry._journal_dirty is False


def test_registry_dirty_volatile_change_on_other_job_is_persisted_by_identical_sync(tmp_path):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path)
    first = registry.submit("strategies.tester", {}, "volatile-first", job_id="volatile-first")
    second = registry.submit("strategies.tester", {}, "volatile-second", job_id="volatile-second")
    registry.transition(first["job_id"], "RUNNING")
    registry.transition(second["job_id"], "RUNNING")
    status = {"state": "RUNNING", "phase": "RUNNING", "progress": {"current": 0, "total": 2}}
    registry.sync(first["job_id"], status)
    registry.sync(second["job_id"], status)
    registry.volatile_sync(first["job_id"], {"state": "RUNNING", "phase": "RUNNING", "progress": {"current": 1, "total": 2}})
    registry.sync(second["job_id"], status, skip_save_if_unchanged=True)

    restored = PanelJobRegistry(path)
    assert restored.get(first["job_id"])["progress"] == {"current": 1, "total": 2}
    assert registry._journal_dirty is False


def test_registry_sync_normalization_persists_omitted_evidence_and_retains_omitted_runtime_fields(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    registry = PanelJobRegistry(path)
    job = registry.submit("strategies.performance.v2.import", {}, "normalization", job_id="normalization")
    registry.transition(job["job_id"], "RUNNING")
    initial = {
        "state": "COMMITTED", "phase": "COMMITTED", "evidence": {"verified": 1},
        "inbox_ready": True, "result": {"imported_count": 1},
    }
    registry.sync(job["job_id"], initial, runtime={"private": "value"})
    replacements = []
    dirty_at_replace = []
    real_replace = panel_jobs_module.os.replace

    def counted_replace(source, destination):
        dirty_at_replace.append(registry._journal_dirty)
        replacements.append(destination)
        return real_replace(source, destination)

    monkeypatch.setattr(panel_jobs_module.os, "replace", counted_replace)

    registry.sync(job["job_id"], {"state": "COMMITTED", "phase": "COMMITTED"}, skip_save_if_unchanged=True)
    assert len(replacements) == 1
    assert dirty_at_replace == [True]
    assert "evidence" not in registry.get(job["job_id"])
    assert registry._journal_dirty is False
    assert "evidence" not in PanelJobRegistry(path).get(job["job_id"])

    registry.sync(job["job_id"], {"state": "COMMITTED", "phase": "COMMITTED"}, skip_save_if_unchanged=True)
    assert len(replacements) == 1
    assert registry.runtime(job["job_id"]) == {"private": "value"}
    assert PanelJobRegistry(path).runtime(job["job_id"]) == {"private": "value"}


def test_j4_characterization_empty_status_rejects_every_valid_state_without_mutation_or_write(tmp_path, monkeypatch):
    registry = PanelJobRegistry(tmp_path / "jobs.json", capacity=16)
    jobs = {}
    for state in ("QUEUED", "RUNNING", "CANCELLING", "CANCELLED", "COMMITTED", "FAILED"):
        job = registry.submit("characterization", {}, f"empty-{state}", job_id=f"empty-{state}")
        if state in {"RUNNING", "CANCELLING", "CANCELLED", "COMMITTED", "FAILED"}:
            registry.transition(job["job_id"], "RUNNING")
        if state in {"CANCELLING", "CANCELLED"}:
            registry.transition(job["job_id"], "CANCELLING")
        if state == "CANCELLED":
            registry.transition(job["job_id"], "CANCELLED")
        if state in {"COMMITTED", "FAILED"}:
            registry.sync(job["job_id"], {"state": state, "phase": state}, runtime={"private": state})
        jobs[job["job_id"]] = deepcopy(registry.jobs[job["job_id"]])
    for job_id in jobs:
        registry.jobs[job_id]["runtime"] = {"changed": True}
    registry._save()
    jobs = {job_id: deepcopy(job) for job_id, job in registry.jobs.items()}
    save_calls = []
    monkeypatch.setattr(registry, "_save", lambda: save_calls.append(True))

    for job_id, before in jobs.items():
        with pytest.raises(PanelJobError, match="INVALID_REQUEST"):
            registry.sync(job_id, {}, runtime={"attempted": True})
        assert registry.jobs[job_id] == before
        assert registry.runtime(job_id) == {"changed": True}
    assert save_calls == []


def test_registry_save_lock_excludes_volatile_mutation_until_save_releases(tmp_path, monkeypatch):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("strategies.performance.v2.import", {}, "lock", job_id="lock")
    registry.transition(job["job_id"], "RUNNING")
    registry.sync(job["job_id"], {"state": "RUNNING", "phase": "RUNNING", "progress": {"current": 0, "total": 1}})
    entered = threading.Event()
    release = threading.Event()
    volatile_done = threading.Event()
    real_replace = panel_jobs_module.os.replace
    replacements = []

    def paused_replace(source, destination):
        replacements.append(destination)
        entered.set()
        assert release.wait(2)
        return real_replace(source, destination)

    monkeypatch.setattr(panel_jobs_module.os, "replace", paused_replace)
    save_thread = threading.Thread(target=lambda: registry.sync(job["job_id"], {"state": "COMMITTED", "phase": "COMMITTED"}, skip_save_if_unchanged=True))
    volatile_thread = threading.Thread(target=lambda: (registry.volatile_sync(job["job_id"], {"state": "COMMITTED", "phase": "COMMITTED", "progress": {"current": 1, "total": 1}}), volatile_done.set()))
    try:
        save_thread.start()
        assert entered.wait(2)
        volatile_thread.start()
        assert not volatile_done.wait(0.1)
    finally:
        release.set()
        save_thread.join(2)
        volatile_thread.join(2)
    assert not save_thread.is_alive()
    assert not volatile_thread.is_alive()
    assert volatile_done.is_set()
    assert registry._journal_dirty is True
    replacements_before_retry = len(replacements)
    registry.sync(job["job_id"], {"state": "COMMITTED", "phase": "COMMITTED"}, skip_save_if_unchanged=True)
    assert len(replacements) == replacements_before_retry + 1
    assert registry._journal_dirty is False
    assert PanelJobRegistry(registry.journal).get(job["job_id"])["progress"] == {"current": 1, "total": 1}
