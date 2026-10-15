"""Small persisted registry for independent panel jobs."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
import tempfile
import time
from typing import Any
from uuid import uuid4


TERMINAL = frozenset({"COMMITTED", "CANCELLED", "FAILED"})
_STATES = frozenset({"QUEUED", "RUNNING", "CANCELLING", *TERMINAL})
_TRANSITIONS = {
    "QUEUED": {"RUNNING", "CANCELLING", "CANCELLED", "FAILED"},
    "RUNNING": {"CANCELLING", "COMMITTED", "FAILED"},
    "CANCELLING": {"CANCELLED", "FAILED"},
}
_WINDOWS_REPLACE_RETRIES = 10
_WINDOWS_REPLACE_RETRY_DELAY_SECONDS = 0.05
_WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})
_UNSUPPORTED_DIRECTORY_FSYNC_ERRORS = frozenset({
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    getattr(errno, "ENOSYS", errno.EINVAL),
})
# Runtime values written once and then only read. They are shared by reference
# between runtime copies instead of being JSON round-tripped on every progress
# update (a frozen portfolio Campaign is tens of megabytes). Callers must copy
# before mutating a shared value; an in-place edit would change registry state.
_SHARED_RUNTIME_KEYS = frozenset({"campaign"})
_JOURNAL_RECOVERY_FAILED = (
    "JOB_JOURNAL_RECOVERY_FAILED: preserve the journal and recovery sidecar "
    "for manual recovery before restarting Panel"
)


def _copy_runtime(value: dict) -> dict:
    shared = {key: value[key] for key in _SHARED_RUNTIME_KEYS if key in value}
    copied = json.loads(json.dumps({key: item for key, item in value.items() if key not in shared}))
    copied.update(shared)
    return copied


def _encode_journal_recovery(previous: bytes, intended: bytes) -> bytes:
    record = {
        "format": 2,
        "previous": base64.b64encode(previous).decode("ascii"),
        "intended": base64.b64encode(intended).decode("ascii"),
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        if error.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRORS:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRORS:
                raise
    finally:
        os.close(descriptor)


def _write_journal_recovery(backup: Path, previous: bytes, intended: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=backup.parent, prefix=f".{backup.name}.", delete=False
        ) as handle:
            handle.write(_encode_journal_recovery(previous, intended))
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.rename(temporary, backup)
        temporary = None
        _fsync_directory(backup.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class PanelJobError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


class PanelJobRegistry:
    def __init__(self, journal: Path, *, capacity: int = 4, recover_on_load: bool = True) -> None:
        if not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be positive")
        self.journal, self.capacity, self.lock = journal, capacity, RLock()
        self.recover_on_load = bool(recover_on_load)
        self._journal_dirty = False
        self.jobs: dict[str, dict] = self._load()
        self._restore_progress_checkpoints()
        if self.recover_on_load:
            self.recover_interrupted()

    @property
    def progress_directory(self) -> Path:
        return self.journal.with_name(f"{self.journal.stem}.progress")

    def _progress_path(self, job_id: str) -> Path:
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return self.progress_directory / f"{digest}.json"

    def _remove_progress_checkpoint(self, job_id: str) -> None:
        try:
            self._progress_path(job_id).unlink(missing_ok=True)
        except OSError:
            pass

    def _write_progress_checkpoint(self, job_id: str, progress: dict) -> None:
        self.progress_directory.mkdir(parents=True, exist_ok=True)
        checkpoint = dict(progress)
        checkpoint.pop("publication_error", None)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.progress_directory, delete=False) as handle:
                json.dump(checkpoint, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, self._progress_path(job_id))
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _restore_progress_checkpoints(self) -> None:
        active_paths = set()
        for job_id, job in self.jobs.items():
            if job.get("kind") != "strategies.performance.v2.finalist-retest" or job.get("state") in TERMINAL:
                continue
            path = self._progress_path(job_id)
            active_paths.add(path)
            try:
                progress = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(progress, dict)
                and type(progress.get("current")) is int
                and type(progress.get("total")) is int
                and progress["current"] >= 0
                and progress["total"] >= progress["current"]
            ):
                progress.pop("publication_error", None)
                current = job.get("progress")
                current_is_valid = (
                    isinstance(current, dict)
                    and type(current.get("current")) is int
                    and type(current.get("total")) is int
                    and current["current"] >= 0
                    and current["total"] >= current["current"]
                )
                current_is_placeholder = (
                    current_is_valid
                    and current["current"] == 0
                    and current["total"] == 0
                    and progress["total"] > 0
                )
                checkpoint_is_newer = (
                    current_is_valid
                    and current.get("total") == progress["total"]
                    and current.get("unit") == progress.get("unit")
                    and progress["current"] >= current["current"]
                )
                if not current_is_valid or current_is_placeholder or checkpoint_is_newer:
                    job["progress"] = progress
                    self._journal_dirty = True
        try:
            for path in self.progress_directory.glob("*.json"):
                if path not in active_paths:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    def _load(self) -> dict[str, dict]:
        self._recover_in_place_backup()
        try:
            data = json.loads(self.journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._journal_dirty = False
            return {}
        if not isinstance(data, dict):
            self._journal_dirty = False
            return {}
        loaded = {
            job_id: job for job_id, job in data.items()
            if isinstance(job_id, str) and self._valid_saved_job(job)
        }
        self._journal_dirty = len(loaded) != len(data)
        return loaded

    @property
    def _journal_recovery_path(self) -> Path:
        return self.journal.with_name(f"{self.journal.name}.recovery")

    def _remove_journal_recovery(self, backup: Path) -> None:
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            return
        try:
            _fsync_directory(self.journal.parent)
        except OSError:
            # A durable journal and either state of this sidecar are recoverable.
            pass

    def _recover_in_place_backup(self) -> None:
        backup = self._journal_recovery_path
        if not backup.exists():
            return
        if backup.is_symlink() or self.journal.is_symlink():
            raise PanelJobError("JOB_JOURNAL_RECOVERY_FAILED", _JOURNAL_RECOVERY_FAILED)
        try:
            record = json.loads(backup.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("journal recovery record has an unsupported format")
            record_format = record.get("format")
            previous_bytes = base64.b64decode(record["previous"], validate=True)
            previous = json.loads(previous_bytes.decode("utf-8"))
            if not isinstance(previous, dict):
                raise ValueError("journal recovery payload must be an object")
            intended_bytes = None
            intended_sha256 = None
            if record_format == 2:
                intended_bytes = base64.b64decode(record["intended"], validate=True)
                intended = json.loads(intended_bytes.decode("utf-8"))
                if not isinstance(intended, dict):
                    raise ValueError("intended journal recovery payload must be an object")
            elif record_format == 1:
                intended_sha256 = record.get("intended_sha256")
                if (
                    not isinstance(intended_sha256, str)
                    or len(intended_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in intended_sha256)
                ):
                    raise ValueError("legacy journal recovery record has no valid intended digest")
            else:
                raise ValueError("journal recovery record has an unsupported format")
            try:
                current_bytes = self.journal.read_bytes()
            except FileNotFoundError:
                current_bytes = None
            current_is_intended = intended_bytes is not None and current_bytes == intended_bytes
            if current_bytes is not None and intended_sha256 is not None:
                current_is_intended = hashlib.sha256(current_bytes).hexdigest() == intended_sha256
            if current_bytes == previous_bytes or current_is_intended:
                self._remove_journal_recovery(backup)
                return
            mode = "r+b" if self.journal.exists() else "xb"
            with self.journal.open(mode) as destination:
                destination.seek(0)
                destination.write(previous_bytes)
                destination.truncate()
                destination.flush()
                os.fsync(destination.fileno())
            if self.journal.read_bytes() != previous_bytes:
                raise OSError("journal recovery did not restore the saved bytes")
            if json.loads(self.journal.read_text(encoding="utf-8")) != previous:
                raise OSError("journal recovery payload did not validate")
            _fsync_directory(self.journal.parent)
            self._remove_journal_recovery(backup)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, KeyError, TypeError) as error:
            raise PanelJobError("JOB_JOURNAL_RECOVERY_FAILED", _JOURNAL_RECOVERY_FAILED) from error

    def _save(self) -> None:
        self._journal_dirty = True
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self._recover_in_place_backup()
        temporary: Path | None = None
        # Portfolio Campaign inputs live in their own verified gzip snapshot.
        # Keep the in-process compatibility copy for old callers, but never
        # write that payload into the shared journal.
        persisted: dict[str, Any] = {}
        for job_id, job in self.jobs.items():
            runtime = job.get("runtime")
            if isinstance(runtime, dict) and "campaign" in runtime and isinstance(runtime.get("campaign_snapshot"), dict):
                # The frozen Campaign is never serialized here, not even to be dropped.
                job = {**job, "runtime": {key: value for key, value in runtime.items() if key != "campaign"}}
            persisted[job_id] = job
        encoded = json.dumps(persisted, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            with tempfile.NamedTemporaryFile("wb", dir=self.journal.parent, delete=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            for attempt in range(_WINDOWS_REPLACE_RETRIES):
                try:
                    os.replace(temporary, self.journal)
                except PermissionError as error:
                    if (
                        getattr(error, "winerror", None) not in _WINDOWS_TRANSIENT_REPLACE_ERRORS
                    ):
                        raise
                    if attempt + 1 == _WINDOWS_REPLACE_RETRIES:
                        if not self.journal.is_file() or self.journal.is_symlink():
                            raise
                        try:
                            self._recover_in_place_backup()
                            previous_bytes = self.journal.read_bytes()
                            previous = json.loads(previous_bytes.decode("utf-8"))
                            if not isinstance(previous, dict):
                                raise OSError("existing Panel journal is not a JSON object")
                            backup = self._journal_recovery_path
                            _write_journal_recovery(backup, previous_bytes, encoded)
                            with self.journal.open("r+b") as destination:
                                destination.seek(0)
                                destination.write(encoded)
                                destination.truncate()
                                destination.flush()
                                os.fsync(destination.fileno())
                            written = self.journal.read_bytes()
                            if written != encoded or json.loads(written.decode("utf-8")) != persisted:
                                raise OSError("in-place journal write did not validate")
                            self._remove_journal_recovery(backup)
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                            try:
                                self._recover_in_place_backup()
                            except PanelJobError:
                                pass
                            raise
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass
                        try:
                            _fsync_directory(self.journal.parent)
                        except OSError:
                            # The journal is durable; any leftover staging file is ignored on recovery.
                            pass
                        temporary = None
                        break
                    time.sleep(_WINDOWS_REPLACE_RETRY_DELAY_SECONDS)
                else:
                    temporary = None
                    _fsync_directory(self.journal.parent)
                    break
            temporary = None
            self._journal_dirty = False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _valid_saved_job(job: object) -> bool:
        return isinstance(job, dict) and isinstance(job.get("job_id"), str) and isinstance(job.get("kind"), str) and isinstance(job.get("idempotency_key"), str) and isinstance(job.get("fingerprint"), str) and isinstance(job.get("resource_keys"), list) and job.get("state") in _STATES

    @staticmethod
    def _copy(job: dict) -> dict:
        value = dict(job)
        value.pop("runtime", None)
        return json.loads(json.dumps(value))

    @staticmethod
    def _public_copy(job: dict) -> dict:
        value = dict(job)
        value.pop("runtime", None)
        kind = value.get("kind")
        if isinstance(kind, str) and (kind == "strategies.tester" or kind.startswith("strategies.tester.")):
            evidence = value.get("evidence")
            if isinstance(evidence, dict) and isinstance(evidence.get("verified_reports"), dict):
                evidence = dict(evidence)
                evidence["verified_reports"] = len(evidence["verified_reports"])
                value["evidence"] = evidence
        return PanelJobRegistry._copy(value)

    @staticmethod
    def _valid_submit(kind: object, request: object, idempotency_key: object, resource_keys: object) -> bool:
        return (
            isinstance(kind, str) and bool(kind.strip()) and len(kind) <= 128
            and isinstance(request, dict)
            and isinstance(idempotency_key, str) and bool(idempotency_key.strip()) and len(idempotency_key) <= 256
            and isinstance(resource_keys, tuple)
            and all(isinstance(key, str) and key.strip() and len(key) <= 256 for key in resource_keys)
            and len(set(resource_keys)) == len(resource_keys)
        )

    @staticmethod
    def _fingerprint(kind: str, request: dict) -> str:
        return hashlib.sha256((kind + json.dumps(request, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()

    def submit(
        self, kind: str, request: dict, idempotency_key: str, resource_keys: tuple[str, ...] = (), *,
        job_id: str | None = None, runtime: dict | None = None,
    ) -> dict:
        """Create a job; an optional controller runtime is persisted in the same journal write."""
        with self.lock:
            if not self._valid_submit(kind, request, idempotency_key, resource_keys):
                raise PanelJobError("INVALID_REQUEST")
            try:
                fingerprint = self._fingerprint(kind, request)
            except (TypeError, ValueError):
                raise PanelJobError("INVALID_REQUEST") from None
            for job in self.jobs.values():
                if job["idempotency_key"] == idempotency_key:
                    if job["fingerprint"] == fingerprint:
                        return self._copy(job)
                    raise PanelJobError("IDEMPOTENCY_CONFLICT")
            active = [j for j in self.jobs.values() if j["state"] not in TERMINAL]
            if len(active) >= self.capacity:
                raise PanelJobError("JOB_CAPACITY_EXHAUSTED")
            used = {key for job in active for key in job["resource_keys"]}
            if used.intersection(resource_keys):
                raise PanelJobError("RESOURCE_BUSY")
            if job_id is None:
                job_id = str(uuid4())
            if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 128 or job_id in self.jobs:
                raise PanelJobError("INVALID_REQUEST")
            job = {"job_id": job_id, "kind": kind, "idempotency_key": idempotency_key, "fingerprint": fingerprint, "resource_keys": list(resource_keys), "state": "QUEUED", "phase": "QUEUED", "progress": {"current": 0, "total": 0, "unit": "items"}, "artifacts": [], "error": None, "logs": [], "created_at_utc": datetime.now(timezone.utc).isoformat()}
            if kind.startswith("portfolio.") and isinstance(request.get("campaign_id"), str):
                job["campaign_id"] = request["campaign_id"]
            if request.get("retest") is True:
                job["retest"] = True
            if runtime is not None:
                if not isinstance(runtime, dict):
                    raise PanelJobError("INVALID_REQUEST")
                job["runtime"] = _copy_runtime(runtime)
            self.jobs[job["job_id"]] = job
            try:
                self._save()
            except BaseException as error:
                self.jobs.pop(job["job_id"], None)
                self._journal_dirty = True
                if isinstance(error, Exception):
                    raise PanelJobError("JOB_PERSISTENCE_FAILED") from error
                raise
            return self._copy(job)

    def get(self, job_id: str) -> dict:
        try: return self._copy(self.jobs[job_id])
        except KeyError: raise PanelJobError("NOT_FOUND") from None

    def _peek(self, job_id: str) -> dict:
        with self.lock:
            try: return dict(self.jobs[job_id])
            except KeyError: raise PanelJobError("NOT_FOUND") from None

    def volatile_sync(
        self,
        job_id: str,
        status: dict,
        *,
        expected: dict | None = None,
        runtime: dict | None = None,
    ) -> None:
        """Update live progress without a checkpoint.

        Callers must not retain or mutate nested progress/error/evidence values
        after this call; the hot path makes only shallow copies of those values.
        """
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or not isinstance(status, dict):
                raise PanelJobError("NOT_FOUND" if job is None else "INVALID_REQUEST")
            if expected is not None and any(
                job.get(key) != expected.get(key)
                for key in ("state", "phase", "error", "evidence")
            ):
                return
            before = dict(job)
            state = status.get("state")
            if state not in _STATES:
                raise PanelJobError("INVALID_REQUEST")
            if state != job["state"]:
                if state not in _TRANSITIONS.get(job["state"], set()):
                    raise PanelJobError("INVALID_REQUEST")
                job["state"] = state
            phase = status.get("phase")
            same_phase = not isinstance(phase, str) or phase == job.get("phase")
            if isinstance(phase, str) and phase.strip() and len(phase) <= 128:
                job["phase"] = phase
            progress = status.get("progress")
            if isinstance(progress, dict):
                previous = job.get("progress")
                regressed = (
                    same_phase
                    and isinstance(previous, dict)
                    and previous.get("total") == progress.get("total")
                    and previous.get("unit") == progress.get("unit")
                    and type(previous.get("current")) is int
                    and type(progress.get("current")) is int
                    and progress["current"] < previous["current"]
                )
                if not regressed:
                    job["progress"] = dict(progress)
            if "error" in status:
                error = status["error"]
                if error is None or isinstance(error, dict):
                    job["error"] = dict(error) if isinstance(error, dict) else None
            if "evidence" in status:
                evidence = status["evidence"]
                if evidence is None:
                    job.pop("evidence", None)
                elif isinstance(evidence, dict):
                    job["evidence"] = dict(evidence)
            if status.get("inbox_ready") is True and job.get("state") == "COMMITTED":
                job["inbox_ready"] = True
            if runtime is not None:
                if not isinstance(runtime, dict):
                    raise PanelJobError("INVALID_REQUEST")
                if job.get("runtime") != runtime:
                    job["runtime"] = json.loads(json.dumps(runtime))
            if job != before:
                self._journal_dirty = True
            if (
                job.get("kind") == "strategies.performance.v2.finalist-retest"
                and job.get("state") in {"RUNNING", "CANCELLING"}
                and isinstance(job.get("progress"), dict)
            ):
                self._write_progress_checkpoint(job_id, job["progress"])

    def list(self) -> list[dict]:
        with self.lock:
            return [self._copy(job) for job in self.jobs.values()]

    def public_list(self) -> list[dict]:
        with self.lock:
            return [self._public_copy(job) for job in self.jobs.values()]

    def transition(self, job_id: str, state: str, *, phase: str | None = None) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None: raise PanelJobError("NOT_FOUND")
            if state not in _STATES or state not in _TRANSITIONS.get(job["state"], set()):
                raise PanelJobError("CANCEL_NOT_ALLOWED" if job["state"] in TERMINAL else "INVALID_REQUEST")
            if phase is not None and (not isinstance(phase, str) or not phase.strip() or len(phase) > 128):
                raise PanelJobError("INVALID_REQUEST")
            job["state"] = state; job["phase"] = phase or state
            if state == "CANCELLED": job["error"] = None
            self._save()
            if state in TERMINAL and job.get("kind") == "strategies.performance.v2.finalist-retest":
                self._remove_progress_checkpoint(job_id)
            return self._copy(job)

    def cancel(self, job_id: str) -> dict:
        state = self.get(job_id)["state"]
        return self.transition(job_id, "CANCELLED" if state == "QUEUED" else "CANCELLING", phase="CANCELLED" if state == "QUEUED" else "CANCELLING")

    def discard_queued(self, job_id: str) -> None:
        """Remove a job only when submission failed before its worker started."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            if job["state"] != "QUEUED":
                raise PanelJobError("DISCARD_NOT_ALLOWED")
            removed = self.jobs.pop(job_id)
            try:
                self._save()
            except BaseException:
                self.jobs[job_id] = removed
                raise

    def sync(
        self,
        job_id: str,
        status: dict,
        *,
        runtime: dict | None = None,
        skip_save_if_unchanged: bool = False,
    ) -> dict:
        """Persist a redacted worker snapshot; runtime is controller-only recovery data."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or not isinstance(status, dict):
                raise PanelJobError("NOT_FOUND" if job is None else "INVALID_REQUEST")
            candidate = dict(job)
            state = status.get("state")
            if state not in _STATES:
                raise PanelJobError("INVALID_REQUEST")
            if state != candidate["state"]:
                if state not in _TRANSITIONS.get(candidate["state"], set()):
                    raise PanelJobError("INVALID_REQUEST")
                candidate["state"] = state
            phase = status.get("phase")
            if isinstance(phase, str) and phase.strip() and len(phase) <= 128:
                candidate["phase"] = phase
            progress = status.get("progress")
            if isinstance(progress, dict):
                candidate["progress"] = json.loads(json.dumps(progress))
            error = status.get("error")
            if error is None or isinstance(error, dict):
                candidate["error"] = json.loads(json.dumps(error))
            evidence = status.get("evidence")
            if evidence is None or isinstance(evidence, dict):
                if evidence is None:
                    candidate.pop("evidence", None)
                else:
                    candidate["evidence"] = json.loads(json.dumps(evidence))
            result = status.get("result")
            if isinstance(result, dict):
                candidate["result"] = json.loads(json.dumps(result))
            if status.get("inbox_ready") is True and candidate.get("state") == "COMMITTED":
                candidate["inbox_ready"] = True
            if runtime is not None:
                if not isinstance(runtime, dict):
                    raise PanelJobError("INVALID_REQUEST")
                candidate["runtime"] = _copy_runtime(runtime)
            if skip_save_if_unchanged and candidate == job and not self._journal_dirty:
                return self._copy(job)
            job.clear()
            job.update(candidate)
            self._journal_dirty = True
            self._save()
            if job.get("kind") == "strategies.performance.v2.finalist-retest" and job.get("state") in TERMINAL:
                self._remove_progress_checkpoint(job_id)
            return self._copy(job)

    def runtime(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise PanelJobError("NOT_FOUND")
            value = job.get("runtime", {})
            return _copy_runtime(value) if isinstance(value, dict) else {}

    def recover_interrupted(self) -> bool:
        """Durably project every nonterminal job to the existing restart state."""
        with self.lock:
            changed = False
            for job in self.jobs.values():
                if job.get("state") not in TERMINAL:
                    job.update(state="FAILED", error={"code": "INTERRUPTED"})
                    changed = True
            for job in self.jobs.values():
                if job.get("kind") != "strategies.performance.v2.finalist-retest":
                    continue
                runtime = job.get("runtime")
                marker = runtime.get("bulk_import_job_id") if isinstance(runtime, dict) else None
                if not isinstance(marker, str) or not marker.startswith("pending:"):
                    continue
                child_id = marker.removeprefix("pending:")
                if not child_id or child_id not in self.jobs:
                    runtime.pop("bulk_import_job_id", None)
                    if not runtime:
                        job.pop("runtime", None)
                    changed = True
            if changed:
                self._save()
                for job_id, job in self.jobs.items():
                    if job.get("kind") == "strategies.performance.v2.finalist-retest" and job.get("state") in TERMINAL:
                        self._remove_progress_checkpoint(job_id)
            self.recover_on_load = True
            return changed

    def reserve_runtime(self, job_id: str, key: str, value: object) -> None:
        """Atomically reserve one controller-only runtime marker."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise PanelJobError("NOT_FOUND")
            runtime = job.get("runtime")
            if isinstance(runtime, dict) and key in runtime:
                raise PanelJobError("RUNTIME_BUSY")
            stored_value = json.loads(json.dumps(value))
            runtime = job.setdefault("runtime", {})
            if not isinstance(runtime, dict):
                runtime = job["runtime"] = {}
            runtime[key] = stored_value
            self._save()

    def clear_runtime(self, job_id: str, key: str, *, value: object = None) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise PanelJobError("NOT_FOUND")
            runtime = job.get("runtime")
            if not isinstance(runtime, dict) or (value is not None and runtime.get(key) != value):
                return
            runtime.pop(key, None)
            if not runtime:
                job.pop("runtime", None)
            self._save()

    def recover_committed(
        self,
        job_id: str,
        *,
        runtime: dict,
        expected_state: str | None = None,
        expected_error: dict | None = None,
    ) -> dict:
        """Commit a revalidated job, optionally guarded by an exact state/error CAS."""
        with self.lock:
            job = self.jobs.get(job_id)
            recoverable = job is not None and (job.get("state") == "FAILED" or (job.get("state") == "RUNNING" and job.get("phase") == "RECOVERING_INBOX"))
            if (
                not recoverable
                or (expected_state is not None and job.get("state") != expected_state)
                or (expected_error is not None and job.get("error") != expected_error)
            ):
                raise PanelJobError("INVALID_REQUEST")
            inbox_path = runtime.get("inbox_path") if isinstance(runtime, dict) else None
            job.update(
                state="COMMITTED",
                phase="COMMITTED",
                error=None,
                runtime=json.loads(json.dumps(runtime)),
            )
            if isinstance(inbox_path, str) and inbox_path.strip():
                job["inbox_ready"] = True
            self._save()
            if job.get("kind") == "strategies.performance.v2.finalist-retest":
                self._remove_progress_checkpoint(job_id)
            return self._copy(job)

    def recover_running(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.get("state") != "FAILED" or job.get("error") not in ({"code": "INTERRUPTED"}, None):
                raise PanelJobError("INVALID_REQUEST")
            job.update(state="RUNNING", phase="RECOVERING_INBOX", error=None)
            self._save()
            return self._copy(job)

    def append_log(self, job_id: str, message: str) -> list[str]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None: raise PanelJobError("NOT_FOUND")
            if not isinstance(message, str) or not message or len(message) > 2048:
                raise PanelJobError("INVALID_REQUEST")
            job["logs"] = (job["logs"] + [message])[-200:]
            self._save(); return list(job["logs"])
