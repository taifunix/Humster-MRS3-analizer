from mrs3.panel_jobs import PanelJobError, PanelJobRegistry


def test_registry_idempotency_collision_capacity_and_restart(tmp_path):
    path = tmp_path / "jobs.json"; registry = PanelJobRegistry(path, capacity=1)
    first = registry.submit("testing.local", {"x": 1}, "same", ("write:out",))
    assert registry.submit("testing.local", {"x": 1}, "same", ("write:out",))["job_id"] == first["job_id"]
    try: registry.submit("other", {}, "other", ("write:out",))
    except PanelJobError as error: assert error.code == "JOB_CAPACITY_EXHAUSTED"
    restarted = PanelJobRegistry(path, capacity=1)
    assert restarted.get(first["job_id"])["error"]["code"] == "INTERRUPTED"


def test_registry_cancel_transition_and_bounded_logs(tmp_path):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    job = registry.submit("source.local-import", {}, "a")
    assert registry.transition(job["job_id"], "RUNNING")["state"] == "RUNNING"
    for index in range(205): registry.append_log(job["job_id"], str(index))
    assert len(registry.get(job["job_id"])["logs"]) == 200
    assert registry.cancel(job["job_id"])["state"] == "CANCELLING"


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

    saved = registry.sync("worker-job", {"state": "COMMITTED", "phase": "COMMITTED", "progress": {"current": 1, "total": 1}}, runtime={"inbox_path": "private"})

    assert saved["state"] == "COMMITTED"
    assert "runtime" not in saved
    assert PanelJobRegistry(tmp_path / "jobs.json").runtime("worker-job") == {"inbox_path": "private"}
