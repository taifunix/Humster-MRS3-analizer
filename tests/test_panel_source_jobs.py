from __future__ import annotations

import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

import pytest

from mrs3.panel_source_jobs import LocalSourceDbJobRunner


def _wait_for(runner: LocalSourceDbJobRunner, job_id: str, state: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        document = runner.status(job_id)
        if document["state"] == state:
            return document
        time.sleep(0.01)
    pytest.fail(f"job did not reach {state}: {runner.status(job_id)}")


class FakeService:
    def __init__(self) -> None:
        self.started = {"import": Event(), "merge": Event()}
        self.release = {"import": Event(), "merge": Event()}
        self.cancel_seen = Event()
        self.failure: BaseException | None = None
        self.result = SimpleNamespace(status="COMMITTED", accepted_count=3, input_count=4)

    def _execute(self, operation: str, token: str, cancellation_requested) -> object:
        self.started[operation].set()
        while not self.release[operation].wait(0.01):
            if cancellation_requested():
                self.cancel_seen.set()
                raise RuntimeError("Source v6 operation cancelled")
        if self.failure is not None:
            raise self.failure
        return self.result

    def execute_import(self, token: str, *, cancellation_requested) -> object:
        return self._execute("import", token, cancellation_requested)

    def execute_merge(self, token: str, *, cancellation_requested) -> object:
        return self._execute("merge", token, cancellation_requested)


def test_distinct_resource_keys_run_in_parallel_and_return_running_documents() -> None:
    service = FakeService()
    runner = LocalSourceDbJobRunner(service)

    imported = runner.start_import("import-token", "target-a")
    merged = runner.start_merge("merge-token", "target-b")

    assert imported["state"] == "RUNNING"
    assert merged["state"] == "RUNNING"
    assert service.started["import"].wait(1)
    assert service.started["merge"].wait(1)
    service.release["import"].set()
    service.release["merge"].set()
    assert _wait_for(runner, imported["job_id"], "COMMITTED")["error"] is None
    assert _wait_for(runner, merged["job_id"], "COMMITTED")["error"] is None


def test_same_resource_key_is_busy_across_import_and_merge() -> None:
    service = FakeService()
    runner = LocalSourceDbJobRunner(service)
    imported = runner.start_import("import-token", "same-target")
    assert service.started["import"].wait(1)

    with pytest.raises(RuntimeError, match="resource busy"):
        runner.start_merge("merge-token", "same-target")

    runner.cancel(imported["job_id"])
    assert _wait_for(runner, imported["job_id"], "CANCELLED")["error"] is None


def test_commit_reports_only_safe_basic_counts_and_no_paths() -> None:
    service = FakeService()
    service.release["import"].set()
    service.result = SimpleNamespace(
        status="COMMITTED",
        accepted_count=3,
        input_count=4,
        target_path=Path("D:/private/source.duckdb"),
    )
    runner = LocalSourceDbJobRunner(service)

    started = runner.start_import("import-token", "target-a")
    committed = _wait_for(runner, started["job_id"], "COMMITTED")

    assert committed["counts"] == {"accepted_count": 3, "input_count": 4}
    assert "target_path" not in committed
    assert "D:/private" not in json.dumps(committed)


def test_failure_is_generic_and_does_not_leak_exception_details() -> None:
    service = FakeService()
    service.release["import"].set()
    service.failure = RuntimeError("secret D:/private/source.duckdb and traceback")
    runner = LocalSourceDbJobRunner(service)

    started = runner.start_import("import-token", "target-a")
    failed = _wait_for(runner, started["job_id"], "FAILED")

    assert failed["error"] == {"code": "FAILED"}
    assert "secret" not in json.dumps(failed)
    assert "source.duckdb" not in json.dumps(failed)


def test_cancel_sets_callback_and_finishes_cancelled() -> None:
    service = FakeService()
    runner = LocalSourceDbJobRunner(service)

    started = runner.start_merge("merge-token", "target-a")
    assert service.started["merge"].wait(1)
    cancelling = runner.cancel(started["job_id"])

    assert cancelling["state"] == "CANCELLING"
    cancelled = _wait_for(runner, started["job_id"], "CANCELLED")
    assert cancelled["error"] is None
    assert service.cancel_seen.is_set()


def test_worker_completion_is_reported_to_the_persisted_job_callback() -> None:
    service = FakeService()
    service.release["import"].set()
    updates: list[dict[str, object]] = []
    runner = LocalSourceDbJobRunner(service, on_update=updates.append)

    started = runner.start_import("import-token", "target-a", job_id="persisted-job")
    _wait_for(runner, str(started["job_id"]), "COMMITTED")

    assert updates[-1]["job_id"] == "persisted-job"
    assert updates[-1]["state"] == "COMMITTED"
