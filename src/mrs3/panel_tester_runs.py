"""Isolated local execution of generated tester run snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from threading import Event, RLock, Thread
from typing import Callable

from .runner.inbox import capture_run_snapshot_inbox, extract_html_strategy_name


_TERMINAL = frozenset({"COMMITTED", "CANCELLED", "FAILED"})


@dataclass(slots=True)
class _RunsJob:
    job_id: str
    root: Path
    total: int
    report_dir: Path
    snapshots: dict[str, dict[str, object]]
    provenance: dict[str, object]
    test_start: str
    test_end: str
    tester_config_bytes: bytes
    cancel: Event = field(default_factory=Event)
    process: object | None = None
    state: str = "RUNNING"
    phase: str = "RUNNING"
    error: dict[str, str] | None = None
    inbox_path: Path | None = None


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

    def __init__(self, config: object, *, launcher: Callable[[Path], object] = _launch, capture_inbox=capture_run_snapshot_inbox, on_update=None) -> None:
        self.config, self._launcher, self._capture_inbox, self._on_update = config, launcher, capture_inbox, on_update
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
    def _load_manifest(root: Path, snapshots: list[Path]) -> tuple[dict[str, dict[str, object]], dict[str, object], str, str]:
        path = LocalRunsBatchService._target(root, "tester", "runs_manifest.json")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            entries = manifest["entries"]
            analysis_id = manifest["analysis_run_id"]
            generation_hash = manifest["generation_manifest_sha256"]
            start, end = manifest["test_start"], manifest["test_end"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("RUNS manifest is unavailable") from error
        if (manifest.get("schema_version") != 1 or not isinstance(entries, list)
                or not isinstance(analysis_id, str) or len(analysis_id) != 64
                or not isinstance(generation_hash, str) or len(generation_hash) != 64
                or not isinstance(start, str) or not isinstance(end, str)):
            raise ValueError("RUNS manifest is invalid")
        unsigned = dict(manifest); unsigned.pop("generation_manifest_sha256", None)
        if sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() != generation_hash:
            raise ValueError("RUNS manifest hash is invalid")
        by_filename = {item.name: item for item in snapshots}
        if len(entries) != len(by_filename):
            raise ValueError("RUNS manifest does not match snapshots")
        strategies: dict[str, dict[str, object]] = {}
        hashes: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("RUNS manifest is invalid")
            filename, name = entry.get("filename"), entry.get("strategy_name")
            path = by_filename.get(filename) if isinstance(filename, str) else None
            if not isinstance(name, str) or not name or path is None or sha256(path.read_bytes()).hexdigest() != entry.get("snapshot_sha256"):
                raise ValueError("RUNS snapshots changed after generation")
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                settings = document["settings"]
                strategy = settings[0] if isinstance(settings, list) and len(settings) == 1 else None
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("RUNS snapshot is invalid") from error
            if not isinstance(strategy, dict) or strategy.get("name") != name:
                raise ValueError("RUNS snapshot does not match manifest")
            strategy_hash = sha256(json.dumps(strategy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            if strategy_hash != entry.get("strategy_sha256") or name in strategies:
                raise ValueError("RUNS snapshot hash does not match manifest")
            strategies[name], hashes[f"{name}.json"] = strategy, strategy_hash
        return strategies, {"analysis_run_id": analysis_id, "generation_manifest_sha256": generation_hash, "strategy_json_sha256": hashes}, start, end

    @staticmethod
    def _reports(job: _RunsJob) -> dict[str, Path]:
        reports: dict[str, Path] = {}
        for path in job.report_dir.glob("*.html"):
            if path.is_symlink():
                raise ValueError("tester RUNS report contains a symbolic link")
            name = extract_html_strategy_name(path)
            if name in reports:
                raise ValueError("tester RUNS report names are duplicated")
            reports[name] = path
        if set(reports) != set(job.snapshots):
            raise ValueError("tester RUNS reports do not match snapshots")
        return reports

    @staticmethod
    def _completed(job: _RunsJob) -> int:
        if not job.report_dir.is_dir():
            return 0
        return min(job.total, sum(1 for path in job.report_dir.glob("*.html") if path.is_file() and not path.is_symlink()))

    def _document(self, job: _RunsJob) -> dict[str, object]:
        document = {
            "job_id": job.job_id, "state": job.state, "phase": job.phase,
            "progress": {"current": self._completed(job), "total": job.total, "unit": "reports"},
            "strategy_count": job.total, "error": job.error, "mode": "RUNS",
        }
        if job.inbox_path is not None:
            document["inbox_path"] = str(job.inbox_path)
        return document

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
        strategies, provenance, test_start, test_end = self._load_manifest(root, snapshots)
        try:
            tester_config_bytes = Path(getattr(self.config, "tester_config")).read_bytes()
        except OSError as error:
            raise ValueError("tester config is unavailable") from error
        with self._lock:
            job = _RunsJob(job_id, root, len(snapshots), report_dir, strategies, provenance, test_start, test_end, tester_config_bytes)
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
                job.inbox_path = self._capture_inbox(
                    self.config, job.job_id, job.snapshots, self._reports(job),
                    tester_config_bytes=job.tester_config_bytes, provenance=job.provenance,
                    test_start=job.test_start, test_end=job.test_end,
                )
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
