"""One Panel task at a time, from real facts: progress, elapsed time, ETA, errors, stop (Panel rework stage 3).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 3 contract» and «Defect to fix». A child
process reports through JSON lines on stdout:
- `{"progress": {"done": i, "total": n, "step": "...", "pair": "..."}}` updates the progress bar;
- `{"error": "...", "pair": "...", "step": "..."}` adds an error as it happens;
- any other JSON object is a result row; other lines are the log.

The task finishes when the main process exits, even if orphaned workers still hold the output pipe (the defect of
2026-10-18); such descendants are then killed. «Стоп» writes a stop file for a cooperative task (tester runs restore
the tester's files between jobs) and kills the whole process tree otherwise.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Callable, Sequence

import psutil

from .error_sanitization import redact_local_paths

LOG_LINES = 200
RESULT_LINES = 500


class TaskRunnerError(RuntimeError):
    """A task cannot be started (another one runs, or the process failed to start)."""


class _JobObject:
    """A Windows job object that holds the task and every process it starts (pool workers included), so the whole
    tree can be ended at once; KILL_ON_JOB_CLOSE ends it with the Panel too. Elsewhere it does nothing (the psutil
    tree kill still applies)."""

    def __init__(self) -> None:
        self._handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            return
        self._kernel32, self._handle = kernel32, handle

    def assign(self, process: subprocess.Popen) -> None:
        if self._handle is not None:
            self._kernel32.AssignProcessToJobObject(self._handle, int(process._handle))

    def terminate(self) -> None:
        if self._handle is not None:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _redacted(value: object) -> object:
    if isinstance(value, str):
        return redact_local_paths(value)
    if isinstance(value, dict):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class TaskRunner:
    def __init__(self, repo_root: Path, *, grace_s: float = 5.0, popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.repo_root = Path(repo_root)
        self._grace = grace_s
        self._popen = popen
        self._clock = clock
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_file: Path | None = None
        self._started_at = 0.0
        self._finished_at: float | None = None
        self._descendants: set[int] = set()
        self._job: _JobObject | None = None
        self._state: dict = {"state": "idle", "kind": None, "label": None, "progress": {}, "errors": [], "log": [],
                             "results": [], "returncode": None}

    # -- control ------------------------------------------------------------------------------------------

    def start(self, kind: str, label: str, argv: Sequence[str], *, total: int | None = None,
              stop_file: Path | None = None) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise TaskRunnerError("задача уже выполняется")
            if stop_file is not None:
                Path(stop_file).unlink(missing_ok=True)    # a stop left from an earlier run must not stop this one
            self._stop_file = Path(stop_file) if stop_file is not None else None
            self._started_at, self._finished_at, self._descendants = self._clock(), None, set()
            self._state = {"state": "running", "kind": kind, "label": label, "started_utc": _utc(),
                           "progress": {"done": 0, "total": total} if total else {}, "errors": [], "log": [],
                           "results": [], "returncode": None, "stopping": False, "stopped": False}
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            try:
                env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}   # lines are read as UTF-8
                self._process = self._popen(list(argv), cwd=str(self.repo_root), stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                            encoding="utf-8", errors="replace", creationflags=flags, env=env)
            except OSError:
                self._state.update(state="failed", errors=[{"pair": None, "step": None,
                                                            "message": "процесс не запустился"}])
                self._process = None
                self._finished_at = self._clock()
                raise TaskRunnerError("не удалось запустить процесс задачи") from None
            self._job = _JobObject()
            self._job.assign(self._process)
            self._thread = threading.Thread(target=self._supervise, args=(self._process, self._state),
                                            name=f"panel-task-{kind}", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            process, stop_file = self._process, self._stop_file
            running = process is not None and process.poll() is None
            if running:
                self._state["stopping"] = True
                self._state["stopped"] = True
                if stop_file is not None:
                    stop_file.parent.mkdir(parents=True, exist_ok=True)
                    stop_file.write_text("stop", encoding="ascii")
        if running and stop_file is None:
            self._kill_tree(process.pid)
        return self.status()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # -- status -------------------------------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            task = json.loads(json.dumps(self._state, default=str))
            end = self._finished_at if self._finished_at is not None else self._clock()
            elapsed = max(0.0, end - self._started_at) if task["state"] != "idle" else 0.0
        task["elapsed_s"] = round(elapsed, 3)
        progress = task.get("progress") or {}
        done, total = progress.get("done"), progress.get("total")
        task["eta_s"] = (round(elapsed / done * (total - done), 1)
                         if task["state"] == "running" and isinstance(done, int) and isinstance(total, int)
                         and 0 < done < total else None)
        return {"task": task}

    # -- internals ----------------------------------------------------------------------------------------

    def _line(self, state: dict, line: str) -> None:
        text = redact_local_paths(line.rstrip("\n"))[:1000]
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = None
        with self._lock:
            state["log"] = [*state["log"], text][-LOG_LINES:]
            if not isinstance(parsed, dict):
                return
            parsed = _redacted(parsed)
            if isinstance(parsed.get("progress"), dict):
                progress = parsed["progress"]
                state["progress"] = {**state["progress"], **{key: progress[key] for key in
                                                              ("done", "total", "step", "pair") if key in progress}}
            elif "error" in parsed:
                state["errors"] = [*state["errors"], {"pair": parsed.get("pair"), "step": parsed.get("step"),
                                                      "message": str(parsed["error"])[:500]}][-LOG_LINES:]
            else:
                state["results"] = [*state["results"], parsed][-RESULT_LINES:]

    def _reader(self, process: subprocess.Popen, state: dict, eof: threading.Event) -> None:
        try:
            for line in process.stdout:
                self._line(state, line)
        except (OSError, ValueError):
            pass
        finally:
            eof.set()

    def _remember_descendants(self, pid: int) -> None:
        try:
            children = psutil.Process(pid).children(recursive=True)
        except psutil.Error:
            return
        with self._lock:
            self._descendants.update(child.pid for child in children)

    def _kill_tree(self, pid: int) -> None:
        if self._job is not None:
            self._job.terminate()
        self._remember_descendants(pid)
        with self._lock:
            pids = set(self._descendants) | {pid}
        processes = []
        for item in pids:
            try:
                processes.append(psutil.Process(item))
            except psutil.Error:
                continue
        for process in processes:
            try:
                process.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(processes, timeout=5)

    def _supervise(self, process: subprocess.Popen, state: dict) -> None:
        eof = threading.Event()
        reader = threading.Thread(target=self._reader, args=(process, state, eof), daemon=True)
        reader.start()
        while process.poll() is None:                  # remember the tree while the main process lives
            self._remember_descendants(process.pid)
            time.sleep(0.2)
        code = process.returncode
        if not eof.wait(self._grace):                  # orphans hold the pipe: end them, then end the task anyway
            if self._job is not None:
                self._job.terminate()
            with self._lock:
                orphans = set(self._descendants)
            for pid in orphans:
                try:
                    psutil.Process(pid).kill()
                except psutil.Error:
                    pass
            eof.wait(self._grace)                      # never close the pipe here: the reader thread holds its lock
        if self._job is not None:
            self._job.close()
        with self._lock:
            self._finished_at = self._clock()
            stopped = state.get("stopped")
            if stopped:
                state["state"] = "stopped"
            else:
                state["state"] = "done" if code == 0 else "failed"
                if code != 0:
                    state["errors"] = [*state["errors"], {"pair": None, "step": None, "message": f"exit code {code}"}]
            state.update(returncode=code, finished_utc=_utc(), stopping=False)
