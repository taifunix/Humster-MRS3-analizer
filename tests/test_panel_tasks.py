"""Panel task runner: progress, ETA, errors, stop and the stuck-RUNNING defect (Panel rework stage 3).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 3 contract» (task area) and «Defect to fix».
Real child processes (the .venv Python) are used: the defect is about real pipes and real process trees.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import psutil
import pytest

from mrs3.panel_tasks import TaskRunner, TaskRunnerError

PY = sys.executable


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _wait(runner: TaskRunner, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = runner.status()["task"]
        if task["state"] in ("done", "failed", "stopped"):
            return task
        time.sleep(0.05)
    raise AssertionError(f"task did not finish: {runner.status()}")


def test_progress_eta_errors_and_results(tmp_path: Path) -> None:
    child = _script(tmp_path, "child.py", """
import json, time
print(json.dumps({"progress": {"done": 0, "total": 4, "step": "history", "pair": "XOMUSDT"}}), flush=True)
for i in range(1, 5):
    time.sleep(0.05)
    print(json.dumps({"progress": {"done": i, "total": 4, "step": "history", "pair": f"P{i}"}}), flush=True)
    print(json.dumps({"pair": f"P{i}", "segments": 2}), flush=True)
print(json.dumps({"error": "MISSING_DAYS 2026-10-14", "pair": "P3", "step": "history"}), flush=True)
print("plain log line C:\\\\Users\\\\me\\\\secret", flush=True)
""")
    runner = TaskRunner(tmp_path)
    started = runner.start("history", "Пересчитать историю", [PY, child])
    assert started["task"]["state"] == "running"
    task = _wait(runner)
    assert task["state"] == "done" and task["returncode"] == 0
    assert task["progress"] == {"done": 4, "total": 4, "step": "history", "pair": "P4"}
    assert task["errors"] == [{"pair": "P3", "step": "history", "message": "MISSING_DAYS 2026-10-14"}]
    assert len(task["results"]) == 4 and task["elapsed_s"] >= 0.2
    assert "Users" not in json.dumps(task)                                    # local paths are redacted
    runner.start("ready", "x", [PY, "-c", "import time; time.sleep(5)"])
    with pytest.raises(TaskRunnerError):
        runner.start("ready", "y", [PY, "-c", "pass"])                        # one task at a time
    runner.stop()
    _wait(runner)


def test_eta_comes_from_the_rate_so_far(tmp_path: Path) -> None:
    child = _script(tmp_path, "slow.py", """
import json, time
print(json.dumps({"progress": {"done": 1, "total": 10}}), flush=True)
time.sleep(3)
""")
    runner = TaskRunner(tmp_path)
    runner.start("history", "x", [PY, child])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = runner.status()["task"]
        if task["progress"].get("done") == 1 and task["elapsed_s"] > 0.3:
            break
        time.sleep(0.05)
    assert task["eta_s"] is not None and task["eta_s"] == pytest.approx(task["elapsed_s"] * 9, rel=0.2)
    runner.stop()
    assert _wait(runner)["state"] == "stopped"


def test_stop_kills_the_whole_process_tree(tmp_path: Path) -> None:
    child = _script(tmp_path, "tree.py", """
import subprocess, sys, time, json
grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(json.dumps({"grandchild": grand.pid}), flush=True)
time.sleep(60)
""")
    runner = TaskRunner(tmp_path)
    runner.start("history", "x", [PY, child])
    deadline = time.monotonic() + 10
    while not runner.status()["task"]["results"] and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = runner.status()["task"]["results"][0]["grandchild"]
    runner.stop()
    assert _wait(runner)["state"] == "stopped"
    time.sleep(0.5)
    assert not psutil.pid_exists(grandchild) or psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE


def test_task_finishes_when_the_main_process_exits_but_orphans_hold_the_pipe(tmp_path: Path) -> None:
    # Defect 2026-10-18: the main process ended, two pool workers kept the output pipe open, the task stayed RUNNING.
    child = _script(tmp_path, "orphans.py", """
import subprocess, sys, json
worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])   # inherits stdout
print(json.dumps({"worker": worker.pid}), flush=True)
""")
    runner = TaskRunner(tmp_path, grace_s=1.0)
    runner.start("history", "x", [PY, child])
    task = _wait(runner, timeout=15)
    assert task["state"] == "done" and task["returncode"] == 0
    worker = task["results"][0]["worker"]
    time.sleep(0.5)
    assert not psutil.pid_exists(worker) or psutil.Process(worker).status() == psutil.STATUS_ZOMBIE


def test_cooperative_stop_writes_the_stop_file_and_waits(tmp_path: Path) -> None:
    stop_file = tmp_path / "jobs" / ".stop"
    child = _script(tmp_path, "coop.py", f"""
import time
from pathlib import Path
stop = Path(r"{stop_file}")
while not stop.exists():
    time.sleep(0.05)
print("restored tester files", flush=True)
""")
    runner = TaskRunner(tmp_path)
    runner.start("tester", "Проверить тестером", [PY, child], stop_file=stop_file)
    time.sleep(0.3)
    assert runner.stop()["task"]["stopping"] is True
    task = _wait(runner)
    assert task["state"] == "stopped" and "restored tester files" in task["log"][-1]


def test_a_failing_child_is_reported(tmp_path: Path) -> None:
    runner = TaskRunner(tmp_path)
    runner.start("ready", "x", [PY, "-c", "import sys; print('boom'); sys.exit(3)"])
    task = _wait(runner)
    assert (task["state"], task["returncode"]) == ("failed", 3)
    assert task["errors"][-1]["message"] == "exit code 3"


def test_a_process_that_cannot_start_fails_with_a_frozen_elapsed_time(tmp_path: Path) -> None:
    ticks = iter([10.0, 12.0, 50.0, 90.0])

    def popen(*_args, **kwargs):
        assert kwargs["env"]["PYTHONUTF8"] == "1" and kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        raise OSError("missing")

    runner = TaskRunner(tmp_path, popen=popen, clock=lambda: next(ticks))
    with pytest.raises(TaskRunnerError):
        runner.start("sync", "Sync", ["nope"])
    first = runner.status()["task"]
    second = runner.status()["task"]
    assert first["state"] == "failed" and first["elapsed_s"] == second["elapsed_s"] == 2.0
