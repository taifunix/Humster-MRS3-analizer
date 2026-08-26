"""Isolated local execution of generated tester run snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import subprocess
from threading import Event, RLock, Thread
from typing import Callable


_TERMINAL = frozenset({"COMMITTED", "CANCELLED", "FAILED"})


@dataclass(slots=True)
class _RunsJob:
    job_id: str
    root: Path
    total: int
    report_dir: Path
    cancel: Event = field(default_factory=Event)
    process: object | None = None
    state: str = "RUNNING"
    phase: str = "RUNNING"
    error: dict[str, str] | None = None


def _launch(root: Path) -> subprocess.Popen[bytes]:
    # Feeding pause lets the manual .bat stay useful while the panel-owned
    # process exits after the tester does.
    return subprocess.Popen(
        ("cmd.exe", "/d", "/c", "(echo.| call run_tester.bat)"),
        cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class LocalRunsBatchService:
    """Run the fixed ``tester/runs`` queue and expose report-count progress."""

    def __init__(self, config: object, *, launcher: Callable[[Path], object] = _launch, on_update=None) -> None:
        self.config, self._launcher, self._on_update = config, launcher, on_update
        self._lock = RLock()
        self._jobs: dict[str, _RunsJob] = {}

    @staticmethod
    def _target(root: Path, *parts: str) -> Path:
        candidate = root.joinpath(*parts)
        current = root
        for part in parts:
            current /= part
            if current.is_symlink():
                raise ValueError("tester runs target contains a symbolic link")
        return candidate

    @staticmethod
    def _snapshots(runs: Path) -> list[Path]:
        if not runs.is_dir():
            return []
        snapshots = [path for path in runs.glob("*.json") if path.is_file() and not path.is_symlink()]
        if any(path.is_symlink() for path in runs.iterdir()):
            raise ValueError("tester runs directory contains a symbolic link")
        return snapshots

    @staticmethod
    def _completed(job: _RunsJob) -> int:
        if not job.report_dir.is_dir():
            return 0
        return min(job.total, sum(1 for path in job.report_dir.glob("*.html") if path.is_file() and not path.is_symlink()))

    def _document(self, job: _RunsJob) -> dict[str, object]:
        return {
            "job_id": job.job_id, "state": job.state, "phase": job.phase,
            "progress": {"current": self._completed(job), "total": job.total, "unit": "reports"},
            "strategy_count": job.total, "error": job.error, "mode": "RUNS",
        }

    def _notify(self, job: _RunsJob) -> None:
        if self._on_update:
            self._on_update(self._document(job))

    def start(self, job_id: str) -> dict[str, object]:
        root = Path(getattr(self.config, "bot_root")).resolve()
        if not root.is_dir() or not (root / "run_tester.bat").is_file():
            raise ValueError("tester runs target is unavailable")
        runs = self._target(root, "tester", "runs")
        snapshots = self._snapshots(runs)
        if not snapshots:
            raise ValueError("RUNS_EMPTY")
        report_dir = self._target(root, "tester", "report", "my_test_runs")
        if report_dir.exists():
            shutil.rmtree(report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            job = _RunsJob(job_id, root, len(snapshots), report_dir)
            self._jobs[job_id] = job
        self._notify(job)
        Thread(target=self._run, args=(job,), daemon=True).start()
        return self._document(job)

    def _run(self, job: _RunsJob) -> None:
        try:
            job.process = self._launcher(job.root)
            while not job.cancel.wait(1):
                if job.process.poll() is not None:
                    break
            if job.cancel.is_set():
                job.state, job.phase = "CANCELLED", "CANCELLED"
            elif self._completed(job) == job.total:
                job.state, job.phase = "COMMITTED", "COMMITTED"
            else:
                job.state, job.phase = "FAILED", "FAILED"
                job.error = {"code": "RUNS_INCOMPLETE"}
        except Exception:
            job.state, job.phase, job.error = "FAILED", "FAILED", {"code": "RUNS_FAILED"}
        self._notify(job)

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            return self._document(self._jobs[job_id])

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._jobs[job_id]
            if job.state in _TERMINAL:
                return self._document(job)
            job.cancel.set()
            if job.process is not None and job.process.poll() is None:
                job.process.terminate()
            job.state, job.phase = "CANCELLING", "CANCELLING"
            return self._document(job)

    def has_active_job(self) -> bool:
        with self._lock:
            return any(job.state not in _TERMINAL for job in self._jobs.values())
