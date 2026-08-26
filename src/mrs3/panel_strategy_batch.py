"""Local, provenance-checked strategy batch jobs for the control panel."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from threading import Event, RLock, Thread
from uuid import uuid4

from .runner.config import RunnerConfig
from .runner.files import prepare_batch_files
from .runner.process import stop_bot as _stop_bot
from .runner.workflow import run_batch as _run_batch
from .runner.workflow import validate_runtime_preflight


class StrategyBatchValidationError(ValueError):
    """Raised when a generated strategy manifest cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ValidatedStrategyManifest:
    manifest_path: Path
    strategy_source: Path
    analysis_run_id: str
    provenance: dict[str, object]


@dataclass(slots=True)
class _Job:
    job_id: str
    manifest: ValidatedStrategyManifest
    output_csv: Path
    runner_config: object
    cancel: Event = field(default_factory=Event)
    state: str = "RUNNING"
    phase: str = "RUNNING"
    progress: dict[str, object] = field(default_factory=dict)
    error: dict[str, str] | None = None
    inbox_path: Path | None = None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StrategyBatchValidationError(f"{field} is missing")
    return value.strip()


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if len(digest) != 64:
        raise StrategyBatchValidationError(f"{field} must be a SHA-256 hash")
    try:
        int(digest, 16)
    except ValueError:
        raise StrategyBatchValidationError(f"{field} must be a SHA-256 hash") from None
    return digest


def _read_json(path: Path, error: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategyBatchValidationError(error) from exc


def _strategy_digest(document: dict[str, object]) -> str:
    value = json.loads(_canonical(document))
    provenance = value.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("strategy_json_sha256", None)
        provenance.pop("generation_manifest_sha256", None)
    return sha256(_canonical(value)).hexdigest()


def _strategy_source(manifest_path: Path) -> Path:
    if manifest_path.parent.name == ".mrs3":
        return manifest_path.parent.parent.resolve()
    sibling = manifest_path.parent / "strategies"
    if sibling.is_dir():
        return sibling.resolve()
    # Fresh generation historically emitted JSON beside the manifest.
    return manifest_path.parent.resolve()


def validate_strategy_manifest(manifest_path: Path) -> ValidatedStrategyManifest:
    """Validate one immutable generation manifest and all of its JSON files."""

    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise StrategyBatchValidationError("strategy manifest is missing")
    raw = _read_json(path, "strategy manifest is invalid")
    if not isinstance(raw, dict):
        raise StrategyBatchValidationError("strategy manifest must be an object")

    if raw.get("format_version") != 1:
        raise StrategyBatchValidationError("unsupported strategy manifest format")
    analysis_run_id = _text(raw.get("analysis_run_id"), "analysis_run_id")
    event_mode = raw.get("event_mode")
    if event_mode != "real_independent_events":
        raise StrategyBatchValidationError("strategy manifest has unsupported event mode")
    generation_hash = _sha256(
        raw.get("generation_manifest_sha256"), "generation_manifest_sha256"
    )
    unsigned = dict(raw)
    unsigned.pop("generation_manifest_sha256", None)
    if sha256(_canonical(unsigned)).hexdigest() != generation_hash:
        raise StrategyBatchValidationError("generation manifest hash mismatch")

    raw_hashes = raw.get("strategy_json_sha256")
    if not isinstance(raw_hashes, dict) or not raw_hashes:
        raise StrategyBatchValidationError("strategy JSON hash map is missing")
    hashes = {str(name): _sha256(value, "strategy JSON hash") for name, value in raw_hashes.items()}
    count = raw.get("strategy_count")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(hashes):
        raise StrategyBatchValidationError("strategy count does not match manifest")

    source = _strategy_source(path)
    expected_names = set(hashes)
    if any(Path(name).name != name or Path(name).suffix.casefold() != ".json" for name in expected_names):
        raise StrategyBatchValidationError("strategy JSON hash map contains an unsafe filename")
    try:
        actual_files = {item.name for item in source.iterdir() if item.is_file() and item.suffix.casefold() == ".json"}
    except OSError as exc:
        raise StrategyBatchValidationError("strategy source is unreadable") from exc
    if actual_files != expected_names:
        raise StrategyBatchValidationError("strategy files do not match manifest")

    for filename, expected_hash in sorted(hashes.items()):
        strategy_path = source / filename
        document = _read_json(strategy_path, f"invalid strategy JSON: {filename}")
        if not isinstance(document, dict):
            raise StrategyBatchValidationError(f"strategy JSON must be an object: {filename}")
        if document.get("name") != Path(filename).stem:
            raise StrategyBatchValidationError(f"strategy filename does not match name: {filename}")
        if _strategy_digest(document) != expected_hash:
            raise StrategyBatchValidationError(f"strategy JSON hash mismatch: {filename}")

    provenance = dict(raw)
    provenance["strategy_json_sha256"] = hashes
    provenance["generation_manifest_sha256"] = generation_hash
    return ValidatedStrategyManifest(path, source, analysis_run_id, provenance)


def _safe_report_path(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise RuntimeError("inbox report path is invalid")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError("inbox report path is invalid") from exc
    if not candidate.is_file():
        raise RuntimeError("inbox report is missing")
    return candidate


def _publish_reports(config: RunnerConfig, inbox: Path) -> None:
    manifest_path = inbox / "inbox_manifest.json"
    document = _read_json(manifest_path, "inbox manifest is invalid")
    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        raise RuntimeError("inbox manifest is invalid")
    report_dir = Path(config.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    for entry in document["entries"]:
        if not isinstance(entry, dict):
            raise RuntimeError("inbox manifest entry is invalid")
        name = entry.get("strategy_name")
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise RuntimeError("inbox strategy name is invalid")
        source = _safe_report_path(inbox, entry.get("report_path"))
        target = report_dir / f"{name}.html"
        shutil.copyfile(source, target)


class LocalStrategyBatchService:
    """Run a validated READY batch in a daemon thread with redacted status."""

    def __init__(self, config: RunnerConfig, *, run_batch=_run_batch, stop_bot=_stop_bot, on_update=None) -> None:
        self.config = config
        self._run_batch = run_batch
        self._stop_bot = stop_bot
        self._on_update = on_update
        self._lock = RLock()
        self._jobs: dict[str, _Job] = {}

    @staticmethod
    def _dates(start_date: object, end_date: object) -> tuple[str, str] | None:
        if start_date is None and end_date is None:
            return None
        if not isinstance(start_date, str) or not isinstance(end_date, str):
            raise StrategyBatchValidationError("start_date and end_date must be ISO dates")
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            raise StrategyBatchValidationError("start_date and end_date must be ISO dates") from None
        if start.isoformat() != start_date or end.isoformat() != end_date:
            raise StrategyBatchValidationError("start_date and end_date must be ISO dates")
        if start > end:
            raise StrategyBatchValidationError("start_date must be on or before end_date")
        return start_date, end_date

    @staticmethod
    def _write_tester_dates(config: RunnerConfig, start_date: str, end_date: str) -> RunnerConfig:
        target = (config.bot_root / "config_tester.json").resolve()
        seed = target
        if not seed.is_file() and config.tester_config.is_file():
            seed = config.tester_config
        try:
            document = json.loads(seed.read_text(encoding="utf-8")) if seed.is_file() else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StrategyBatchValidationError("tester config is invalid") from error
        if not isinstance(document, dict):
            raise StrategyBatchValidationError("tester config must be an object")
        document["StartDate"] = start_date
        document["EndDate"] = end_date
        temporary = target.with_name(target.name + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, target)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise StrategyBatchValidationError("tester config cannot be written") from error
        return replace(config, tester_config=target, preserve_raw_artifacts=True)

    def start(
        self,
        manifest_path: Path,
        *,
        analysis_run_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, object]:
        validated = validate_strategy_manifest(Path(manifest_path))
        if analysis_run_id is not None and validated.analysis_run_id != analysis_run_id:
            raise StrategyBatchValidationError("strategy batch does not match analysis run")
        dates = self._dates(start_date, end_date)
        job_id = job_id or str(uuid4())
        with self._lock:
            if any(item.state not in {"COMMITTED", "CANCELLED", "FAILED"} for item in self._jobs.values()):
                raise StrategyBatchValidationError("local tester batch is already running")
            if not isinstance(job_id, str) or not job_id.strip() or job_id in self._jobs:
                raise StrategyBatchValidationError("local tester job id is invalid")
            runtime_config: object = self.config
            if dates is not None:
                if not isinstance(self.config, RunnerConfig):
                    raise StrategyBatchValidationError("tester config is unavailable")
                validate_runtime_preflight(self.config)
                runtime_config = self._write_tester_dates(self.config, *dates)
                prepare_batch_files(runtime_config, validated.strategy_source)
            else:
                validate_runtime_preflight(self.config)
            inbox_root = Path(getattr(self.config, "inbox_root", Path.cwd() / ".mrs3-panel-inbox"))
            output = inbox_root / f"{job_id}.csv"
            job = _Job(
                job_id,
                validated,
                output,
                runtime_config,
                progress={
                    "sent": 0,
                    "running": 0,
                    "result": 0,
                    "checked": 0,
                    "retries": 0,
                    "total": int(validated.provenance["strategy_count"]),
                },
            )
            self._jobs[job_id] = job
        try:
            Thread(target=self._run, args=(job,), daemon=True, name="mrs3-panel-strategy-batch").start()
        except BaseException:
            with self._lock:
                job.state = job.phase = "FAILED"
                job.error = {"code": "FAILED"}
            raise
        return self._snapshot(job)

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._get(job_id)
            self._refresh_progress(job)
            return self._snapshot(job)

    def has_active_job(self) -> bool:
        with self._lock:
            return any(job.state not in {"COMMITTED", "CANCELLED", "FAILED"} for job in self._jobs.values())

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._get(job_id)
            if job.state not in {"COMMITTED", "CANCELLED", "FAILED"}:
                job.cancel.set()
                job.state = job.phase = "CANCELLING"
            return self._snapshot(job)

    def _run(self, job: _Job) -> None:
        terminal: str
        try:
            result = self._run_batch(
                job.runner_config,
                job.manifest.strategy_source,
                job.output_csv,
                provenance=job.manifest.provenance,
            )
            inbox = getattr(result, "inbox_path", None)
            with self._lock:
                cancelled = job.cancel.is_set()
            if cancelled:
                terminal = "CANCELLED"
            elif inbox is not None:
                inbox = Path(inbox).resolve()
                _publish_reports(job.runner_config, inbox)
                with self._lock:
                    job.inbox_path = inbox
            with self._lock:
                cancelled = job.cancel.is_set()
                if not cancelled:
                    total = int(job.manifest.provenance["strategy_count"])
                    job.progress = {"sent": total, "running": 0, "result": total, "checked": total, "retries": 0, "total": total}
            terminal = "CANCELLED" if cancelled else "COMMITTED"
        except BaseException:
            with self._lock:
                cancelled = job.cancel.is_set()
            terminal = "CANCELLED" if cancelled else "FAILED"
        finally:
            try:
                self._stop_bot(job.runner_config)
            except BaseException:
                pass
            self._finish(job, terminal)

    def _finish(self, job: _Job, state: str) -> None:
        with self._lock:
            if job.state in {"COMMITTED", "CANCELLED", "FAILED"}:
                return
            job.state = job.phase = state
            job.error = None if state != "FAILED" else {"code": "FAILED"}
            snapshot = self._snapshot(job)
        if self._on_update is not None:
            try:
                self._on_update(snapshot)
            except BaseException:
                pass

    @staticmethod
    def _refresh_progress(job: _Job) -> None:
        progress_path = job.output_csv.with_name(f"{job.output_csv.stem}.progress.json")
        try:
            document = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(document, dict):
            return
        total = int(job.manifest.provenance["strategy_count"])
        def count(name: str) -> int:
            value = document.get(name, 0)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        job.progress = {
            "sent": count("submitted_count"),
            "running": count("running_count"),
            "result": count("result_count"),
            "checked": count("completed_count"),
            "retries": count("retry_count"),
            "total": total,
        }

    def _get(self, job_id: str) -> _Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise KeyError("job not found") from None

    @staticmethod
    def _snapshot(job: _Job) -> dict[str, object]:
        return {
            "job_id": job.job_id,
            "state": job.state,
            "phase": job.phase,
            "progress": dict(job.progress),
            "error": None if job.error is None else dict(job.error),
            "strategy_count": job.progress.get("total", 0),
            "inbox_path": str(job.inbox_path) if job.inbox_path is not None else None,
        }
