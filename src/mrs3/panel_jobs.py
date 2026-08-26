"""Small persisted registry for independent panel jobs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import RLock
import tempfile
from uuid import uuid4


TERMINAL = frozenset({"COMMITTED", "CANCELLED", "FAILED"})
_STATES = frozenset({"QUEUED", "RUNNING", "CANCELLING", *TERMINAL})
_TRANSITIONS = {
    "QUEUED": {"RUNNING", "CANCELLING", "CANCELLED", "FAILED"},
    "RUNNING": {"CANCELLING", "COMMITTED", "FAILED"},
    "CANCELLING": {"CANCELLED", "FAILED"},
}


class PanelJobError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PanelJobRegistry:
    def __init__(self, journal: Path, *, capacity: int = 4) -> None:
        if not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be positive")
        self.journal, self.capacity, self.lock = journal, capacity, RLock()
        self.jobs: dict[str, dict] = self._load()
        for job in self.jobs.values():
            if job["state"] not in TERMINAL:
                job.update(state="FAILED", error={"code": "INTERRUPTED"})
        self._save()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.journal.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {
                job_id: job for job_id, job in data.items()
                if isinstance(job_id, str) and self._valid_saved_job(job)
            }
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.journal.parent, delete=False) as handle:
                json.dump(self.jobs, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, self.journal)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _valid_saved_job(job: object) -> bool:
        return isinstance(job, dict) and isinstance(job.get("job_id"), str) and isinstance(job.get("kind"), str) and isinstance(job.get("idempotency_key"), str) and isinstance(job.get("fingerprint"), str) and isinstance(job.get("resource_keys"), list) and job.get("state") in _STATES

    @staticmethod
    def _copy(job: dict) -> dict:
        value = json.loads(json.dumps(job))
        value.pop("runtime", None)
        return value

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

    def submit(self, kind: str, request: dict, idempotency_key: str, resource_keys: tuple[str, ...] = (), *, job_id: str | None = None) -> dict:
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
            job = {"job_id": job_id, "kind": kind, "idempotency_key": idempotency_key, "fingerprint": fingerprint, "resource_keys": list(resource_keys), "state": "QUEUED", "phase": "QUEUED", "progress": {"current": 0, "total": 0, "unit": "items"}, "artifacts": [], "error": None, "logs": []}
            self.jobs[job["job_id"]] = job; self._save(); return self._copy(job)

    def get(self, job_id: str) -> dict:
        try: return self._copy(self.jobs[job_id])
        except KeyError: raise PanelJobError("NOT_FOUND") from None

    def list(self) -> list[dict]:
        with self.lock:
            return [self._copy(job) for job in self.jobs.values()]

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
            self._save(); return self._copy(job)

    def cancel(self, job_id: str) -> dict:
        state = self.get(job_id)["state"]
        return self.transition(job_id, "CANCELLED" if state == "QUEUED" else "CANCELLING", phase="CANCELLED" if state == "QUEUED" else "CANCELLING")

    def sync(self, job_id: str, status: dict, *, runtime: dict | None = None) -> dict:
        """Persist a redacted worker snapshot; runtime is controller-only recovery data."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or not isinstance(status, dict):
                raise PanelJobError("NOT_FOUND" if job is None else "INVALID_REQUEST")
            state = status.get("state")
            if state not in _STATES:
                raise PanelJobError("INVALID_REQUEST")
            if state != job["state"]:
                if state not in _TRANSITIONS.get(job["state"], set()):
                    raise PanelJobError("INVALID_REQUEST")
                job["state"] = state
            phase = status.get("phase")
            if isinstance(phase, str) and phase.strip() and len(phase) <= 128:
                job["phase"] = phase
            progress = status.get("progress")
            if isinstance(progress, dict):
                job["progress"] = json.loads(json.dumps(progress))
            error = status.get("error")
            if error is None or isinstance(error, dict):
                job["error"] = json.loads(json.dumps(error))
            evidence = status.get("evidence")
            if evidence is None or isinstance(evidence, dict):
                if evidence is None:
                    job.pop("evidence", None)
                else:
                    job["evidence"] = json.loads(json.dumps(evidence))
            if status.get("inbox_ready") is True:
                job["inbox_ready"] = True
            if runtime is not None:
                if not isinstance(runtime, dict):
                    raise PanelJobError("INVALID_REQUEST")
                job["runtime"] = json.loads(json.dumps(runtime))
            self._save()
            return self._copy(job)

    def runtime(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise PanelJobError("NOT_FOUND")
            value = job.get("runtime", {})
            return json.loads(json.dumps(value)) if isinstance(value, dict) else {}

    def recover_committed(self, job_id: str, *, runtime: dict) -> dict:
        """Commit only a restart-interrupted job whose owner revalidated its artifacts."""
        with self.lock:
            job = self.jobs.get(job_id)
            recoverable = job is not None and (job.get("state") == "FAILED" or (job.get("state") == "RUNNING" and job.get("phase") == "RECOVERING_INBOX"))
            if not recoverable:
                raise PanelJobError("INVALID_REQUEST")
            job.update(state="COMMITTED", phase="COMMITTED", error=None, runtime=json.loads(json.dumps(runtime)))
            self._save()
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
