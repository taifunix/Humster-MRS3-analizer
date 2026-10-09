import json

import pytest

import mrs3.panel_jobs as panel_jobs_module
from mrs3.panel_jobs import PanelJobError, PanelJobRegistry


def _permission_error_with_winerror(winerror):
    error = PermissionError("journal replace failed")
    error.winerror = winerror
    return error


def test_submit_rolls_back_and_maps_permission_error_without_winerror(tmp_path, monkeypatch):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    original_replace = panel_jobs_module.os.replace
    calls = []

    def fail_replace(source, destination):
        calls.append((source, destination))
        raise PermissionError(13, "access denied")

    monkeypatch.setattr(panel_jobs_module.os, "replace", fail_replace)

    with pytest.raises(PanelJobError) as caught:
        registry.submit("strategies.tester.start", {}, "posix-permission-error", ("strategies.tester",))

    assert caught.value.code == "JOB_PERSISTENCE_FAILED"
    assert len(calls) == 1
    assert registry.jobs == {}
    assert registry._journal_dirty is True
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(panel_jobs_module.os, "replace", original_replace)
    retry = registry.submit("strategies.tester.start", {}, "posix-permission-retry", ("strategies.tester",))
    assert retry["state"] == "QUEUED"
    assert registry._journal_dirty is False


def test_submit_rolls_back_when_save_raises_non_os_error(tmp_path, monkeypatch):
    registry = PanelJobRegistry(tmp_path / "jobs.json")

    def fail_save():
        raise RuntimeError("serialization failed")

    monkeypatch.setattr(registry, "_save", fail_save)

    with pytest.raises(PanelJobError) as caught:
        registry.submit("testing.local", {}, "non-os-save-error")

    assert caught.value.code == "JOB_PERSISTENCE_FAILED"
    assert registry.jobs == {}
    assert registry._journal_dirty is True


def test_submit_reraises_non_exception_base_exception_after_rollback(tmp_path, monkeypatch):
    registry = PanelJobRegistry(tmp_path / "jobs.json")

    class CancelSignal(BaseException):
        pass

    signal = CancelSignal()

    def fail_save():
        raise signal

    monkeypatch.setattr(registry, "_save", fail_save)

    with pytest.raises(CancelSignal) as caught:
        registry.submit("testing.local", {}, "cancel-during-save")

    assert caught.value is signal
    assert registry.jobs == {}
    assert registry._journal_dirty is True


@pytest.mark.parametrize("winerror", [32, 33])
def test_submit_retries_other_windows_sharing_errors(tmp_path, monkeypatch, winerror):
    journal = tmp_path / "jobs.json"
    registry = PanelJobRegistry(journal)
    original_replace = panel_jobs_module.os.replace
    calls = []
    sleeps = []

    def replace_once(source, destination):
        calls.append((source, destination))
        if len(calls) == 1:
            raise _permission_error_with_winerror(winerror)
        return original_replace(source, destination)

    monkeypatch.setattr(panel_jobs_module.os, "replace", replace_once)
    monkeypatch.setattr(panel_jobs_module.time, "sleep", sleeps.append)

    job = registry.submit("testing.local", {}, f"transient-{winerror}")

    assert job["state"] == "QUEUED"
    assert len(calls) == 2
    assert sleeps == [panel_jobs_module._WINDOWS_REPLACE_RETRY_DELAY_SECONDS]
    assert json.loads(journal.read_text(encoding="utf-8"))[job["job_id"]]["state"] == "QUEUED"


def test_submit_does_not_retry_unlisted_windows_error(tmp_path, monkeypatch):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    calls = []
    sleeps = []

    def fail_replace(source, destination):
        calls.append((source, destination))
        raise _permission_error_with_winerror(1224)

    monkeypatch.setattr(panel_jobs_module.os, "replace", fail_replace)
    monkeypatch.setattr(panel_jobs_module.time, "sleep", sleeps.append)

    with pytest.raises(PanelJobError) as caught:
        registry.submit("testing.local", {}, "unlisted-windows-error")

    assert caught.value.code == "JOB_PERSISTENCE_FAILED"
    assert len(calls) == 1
    assert sleeps == []
    assert registry.jobs == {}
    assert list(tmp_path.iterdir()) == []


def test_submit_bounds_retries_and_removes_temporary_file(tmp_path, monkeypatch):
    registry = PanelJobRegistry(tmp_path / "jobs.json")
    calls = []
    sleeps = []

    def fail_replace(source, destination):
        calls.append((source, destination))
        raise _permission_error_with_winerror(5)

    monkeypatch.setattr(panel_jobs_module.os, "replace", fail_replace)
    monkeypatch.setattr(panel_jobs_module.time, "sleep", sleeps.append)

    with pytest.raises(PanelJobError) as caught:
        registry.submit("testing.local", {}, "persistent-sharing-error")

    assert caught.value.code == "JOB_PERSISTENCE_FAILED"
    assert len(calls) == panel_jobs_module._WINDOWS_REPLACE_RETRIES == 5
    assert len(sleeps) == 4
    assert registry.jobs == {}
    assert list(tmp_path.iterdir()) == []
