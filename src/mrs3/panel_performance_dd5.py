"""Small safety wrapper for the local panel Performance/DD5 workflow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from threading import RLock, Thread
from typing import Callable
from uuid import uuid4

from .config import AlgorithmConfig
from .performance_dd5 import run_performance_dd5
from .performance_import import (
    PerformanceImportRequest,
    import_performance_batch,
    resume_performance_cleanup,
)


class PanelPerformanceDd5Error(ValueError):
    """Raised when panel DD5 preconditions or output verification fail."""


@dataclass(frozen=True, slots=True)
class PerformanceDd5Request:
    inbox: Path
    database: Path
    output_dir: Path
    config: AlgorithmConfig
    delete_html: bool = False


@dataclass(frozen=True, slots=True)
class PerformanceDd5Result:
    import_id: str
    dd5_run_id: str
    dd5_mode: str
    artifacts: object

    @property
    def manifest(self) -> Path:
        return getattr(self.artifacts, "manifest")

    @property
    def manifest_json(self) -> dict[str, object]:
        return getattr(self.artifacts, "manifest_json")

    @property
    def workbook(self) -> Path:
        return getattr(self.artifacts, "workbook")

    @property
    def csv_directory(self) -> Path:
        return getattr(self.artifacts, "csv_directory")


def _value(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PanelPerformanceDd5Error(f"{label} is missing or invalid") from error
    if not isinstance(document, dict):
        raise PanelPerformanceDd5Error(f"{label} is invalid")
    return document


def _reject_legacy(value: object) -> None:
    keys = {
        "legacy_csv",
        "runner_csv",
        "csv_only",
        "csv",
        "csv_path",
        "output_csv",
        "duckdb_direct",
    }

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                normalized = str(key).casefold().replace(" ", "_").replace("-", "_")
                if normalized in keys:
                    raise PanelPerformanceDd5Error(
                        "legacy CSV or DUCKDB_DIRECT evidence is not allowed"
                    )
                visit(child)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            normalized = item.strip().casefold().replace("-", "_")
            if normalized in keys:
                raise PanelPerformanceDd5Error(
                    "legacy CSV or DUCKDB_DIRECT evidence is not allowed"
                )

    visit(value)


class LocalPerformanceDd5Service:
    """Preflight, import, calculate DD5, and optionally clean up HTML."""

    def __init__(
        self,
        *,
        import_batch: Callable[..., object] = import_performance_batch,
        run_dd5: Callable[..., object] = run_performance_dd5,
        cleanup: Callable[..., object] = resume_performance_cleanup,
    ) -> None:
        self.import_batch = import_batch
        self.run_dd5 = run_dd5
        self.cleanup = cleanup

    def preflight(self, request: PerformanceDd5Request) -> dict[str, object]:
        manifest = _read_json(Path(request.inbox) / "inbox_manifest.json", "inbox manifest")
        if manifest.get("schema_version") != 1:
            raise PanelPerformanceDd5Error("inbox manifest schema_version must be 1")
        _reject_legacy(manifest)
        provenance = manifest.get("v6_provenance")
        if not isinstance(provenance, Mapping):
            raise PanelPerformanceDd5Error("v6 provenance is required")
        required = ("analysis_run_id", "generation_manifest_sha256", "strategy_json_sha256")
        if any(not provenance.get(key) for key in required):
            raise PanelPerformanceDd5Error("v6 provenance is incomplete")
        if not isinstance(provenance["analysis_run_id"], str):
            raise PanelPerformanceDd5Error("v6 provenance analysis_run_id is invalid")
        generation_hash = provenance["generation_manifest_sha256"]
        if not isinstance(generation_hash, str) or len(generation_hash) != 64:
            raise PanelPerformanceDd5Error("v6 provenance generation hash is invalid")
        hashes = provenance["strategy_json_sha256"]
        if not isinstance(hashes, Mapping):
            raise PanelPerformanceDd5Error("v6 provenance strategy hashes are invalid")
        expected_names = manifest.get("expected_strategy_names")
        if isinstance(expected_names, Sequence) and not isinstance(expected_names, (str, bytes, bytearray)):
            expected_hash_names = {f"{name}.json" for name in expected_names}
            if set(map(str, hashes)) != expected_hash_names:
                raise PanelPerformanceDd5Error("v6 provenance strategy hashes do not cover the batch")
        if any(not isinstance(value, str) or len(value) != 64 for value in hashes.values()):
            raise PanelPerformanceDd5Error("v6 provenance strategy hash is invalid")
        return dict(provenance)

    def run(
        self,
        request: PerformanceDd5Request,
        *,
        progress: Callable[[object], object] | None = None,
    ) -> PerformanceDd5Result:
        self.preflight(request)
        import_request = PerformanceImportRequest(Path(request.inbox), Path(request.database))
        imported = self.import_batch(import_request, progress=progress)
        import_id = _value(imported, "import_id")
        quarantined = _value(imported, "quarantined_count")
        if not isinstance(import_id, str) or not import_id.strip():
            raise PanelPerformanceDd5Error("performance import did not return an import_id")
        if isinstance(quarantined, bool) or not isinstance(quarantined, int) or quarantined != 0:
            raise PanelPerformanceDd5Error("DD5 requires zero quarantine")
        batch_id = _read_json(Path(request.inbox) / "inbox_manifest.json", "inbox manifest").get("batch_id")
        self._verify_audit(Path(request.inbox), import_id, batch_id)

        artifacts = self.run_dd5(
            Path(request.database), import_id, Path(request.output_dir), request.config
        )
        manifest_path = _value(artifacts, "manifest")
        dd5_run_id = _value(artifacts, "dd5_run_id")
        if not isinstance(manifest_path, (str, Path)) or not isinstance(dd5_run_id, str) or not dd5_run_id.strip():
            raise PanelPerformanceDd5Error("DD5 export is incomplete")
        manifest_path = Path(manifest_path)
        manifest = _read_json(manifest_path, "DD5 manifest")
        if manifest.get("import_id") != import_id or manifest.get("dd5_run_id") != dd5_run_id:
            raise PanelPerformanceDd5Error("DD5 manifest identity does not match import")
        mode = manifest.get("dd5_mode")
        if mode != "CALCULATION_ONLY":
            raise PanelPerformanceDd5Error("DD5 export must be CALCULATION_ONLY")
        if request.delete_html:
            self.cleanup(import_request)
        return PerformanceDd5Result(import_id, dd5_run_id, mode, artifacts)

    @staticmethod
    def _verify_audit(inbox: Path, import_id: str, batch_id: object) -> None:
        audit = _read_json(inbox / "import_audit.v4.json", "v4 import audit")
        if (
            audit.get("schema_version") != 4
            or audit.get("status") != "COMMITTED"
            or audit.get("import_id") != import_id
            or audit.get("batch_id") != batch_id
            or audit.get("quarantine_count") != 0
        ):
            raise PanelPerformanceDd5Error(
                "DD5 requires committed v4 audit evidence with zero quarantine"
            )


class LocalPerformanceDd5Jobs:
    """One local background Performance DB/DD5 job at a time."""

    def __init__(self, *, run: Callable[..., PerformanceDd5Result] | None = None, on_update: Callable[[dict[str, object]], None] | None = None) -> None:
        self._run = run or LocalPerformanceDd5Service().run
        self._on_update = on_update
        self._lock = RLock()
        self._jobs: dict[str, dict[str, object]] = {}

    def start(self, request: PerformanceDd5Request, *, job_id: str | None = None) -> dict[str, object]:
        job_id = job_id or str(uuid4())
        with self._lock:
            if any(job["state"] == "RUNNING" for job in self._jobs.values()):
                raise PanelPerformanceDd5Error("Performance DB/DD5 is already running")
            if not isinstance(job_id, str) or not job_id.strip() or job_id in self._jobs:
                raise PanelPerformanceDd5Error("Performance DB/DD5 job id is invalid")
            self._jobs[job_id] = {
                "job_id": job_id, "state": "RUNNING", "phase": "IMPORTING",
                "progress": {"current": 0, "total": 0, "unit": "reports"}, "error": None,
            }
        Thread(target=self._worker, args=(job_id, request), daemon=True, name="mrs3-panel-performance-dd5").start()
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            try:
                return dict(self._jobs[job_id])
            except KeyError:
                raise KeyError("job not found") from None

    def _worker(self, job_id: str, request: PerformanceDd5Request) -> None:
        def progress(value: object) -> None:
            current = _value(value, "completed")
            total = _value(value, "total")
            with self._lock:
                job = self._jobs[job_id]
                job["phase"] = str(_value(value, "stage") or "IMPORTING")
                job["progress"] = {
                    "current": current if isinstance(current, int) else 0,
                    "total": total if isinstance(total, int) else 0,
                    "unit": "reports",
                }
        try:
            result = self._run(request, progress=progress)
        except BaseException:
            with self._lock:
                self._jobs[job_id].update(state="FAILED", phase="FAILED", error={"code": "FAILED"})
                snapshot = dict(self._jobs[job_id])
            self._updated(snapshot)
            return
        with self._lock:
            self._jobs[job_id].update(
                state="COMMITTED", phase="COMMITTED", error=None,
                result={"import_id": result.import_id, "dd5_run_id": result.dd5_run_id, "dd5_mode": result.dd5_mode},
            )
            snapshot = dict(self._jobs[job_id])
        self._updated(snapshot)

    def _updated(self, document: dict[str, object]) -> None:
        if self._on_update is not None:
            try:
                self._on_update(document)
            except BaseException:
                pass
