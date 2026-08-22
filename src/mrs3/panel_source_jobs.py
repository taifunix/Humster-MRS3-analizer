"""Thread-backed local Source DB jobs for the v2 control panel."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import Event, RLock, Thread
from typing import Any
from uuid import uuid4


_TERMINAL = frozenset({"COMMITTED", "CANCELLED", "FAILED"})
_COUNT_FIELDS = (
    "accepted_count",
    "quarantined_count",
    "input_count",
    "duplicate_count",
    "writer_count",
)


@dataclass(slots=True)
class _Job:
    job_id: str
    operation: str
    token: str
    resource_key: str
    cancel_event: Event = field(default_factory=Event)
    state: str = "RUNNING"
    phase: str = "RUNNING"
    current: int = 0
    total: int = 0
    error: dict[str, str] | None = None
    counts: dict[str, int] = field(default_factory=dict)


class LocalSourceDbJobRunner:
    """Run injected local Source DB service calls without exposing raw results."""

    def __init__(self, service: object, *, on_update: Callable[[dict[str, object]], None] | None = None) -> None:
        self.service = service
        self._on_update = on_update
        self._lock = RLock()
        self._jobs: dict[str, _Job] = {}
        self._resources: dict[str, str] = {}

    def start_import(self, token: str, resource_key: str, *, job_id: str | None = None) -> dict[str, object]:
        executor = getattr(self.service, "execute_import", None)
        if not callable(executor):
            raise ValueError("local import service is unavailable")
        return self._start("local-import", executor, token, resource_key, job_id)

    def start_merge(self, token: str, resource_key: str, *, job_id: str | None = None) -> dict[str, object]:
        executor = getattr(self.service, "execute_merge", None)
        if not callable(executor):
            raise ValueError("local merge service is unavailable")
        return self._start("local-merge", executor, token, resource_key, job_id)

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            return self._snapshot(self._get(job_id))

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            return [self._snapshot(job) for job in self._jobs.values()]

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._get(job_id)
            if job.state not in _TERMINAL:
                job.cancel_event.set()
                job.state = "CANCELLING"
                job.phase = "CANCELLING"
            return self._snapshot(job)

    def _start(
        self,
        operation: str,
        executor: Callable[..., object],
        token: str,
        resource_key: str,
        job_id: str | None,
    ) -> dict[str, object]:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("preflight token is required")
        if not isinstance(resource_key, str) or not resource_key.strip():
            raise ValueError("resource key is required")
        with self._lock:
            if resource_key in self._resources:
                raise RuntimeError("resource busy")
            if job_id is not None and (not isinstance(job_id, str) or not job_id.strip() or job_id in self._jobs):
                raise ValueError("job id is invalid")
            job = _Job(job_id or str(uuid4()), operation, token, resource_key)
            self._jobs[job.job_id] = job
            self._resources[resource_key] = job.job_id
            initial = self._snapshot(job)
            try:
                Thread(target=self._run, args=(job, executor), daemon=True).start()
            except BaseException:
                self._finish(job, "FAILED")
                raise
            return initial

    def _run(self, job: _Job, executor: Callable[..., object]) -> None:
        try:
            result = executor(job.token, cancellation_requested=job.cancel_event.is_set)
        except BaseException as error:
            cancelled = job.cancel_event.is_set() or self._cancelled_error(error)
            self._finish(job, "CANCELLED" if cancelled else "FAILED")
            return

        if job.cancel_event.is_set() or self._cancelled_result(result):
            self._finish(job, "CANCELLED")
        elif self._failed_result(result):
            self._finish(job, "FAILED")
        else:
            self._finish(job, "COMMITTED", self._safe_counts(result))

    def _finish(self, job: _Job, state: str, counts: dict[str, int] | None = None) -> None:
        with self._lock:
            if job.state in _TERMINAL:
                return
            job.state = state
            job.phase = state
            if state == "FAILED":
                job.error = {"code": "FAILED"}
            elif state == "CANCELLED":
                job.error = None
            elif counts:
                job.counts = counts
                total = counts.get("input_count")
                current = counts.get("accepted_count")
                job.total = total if total is not None else (current or 0)
                job.current = job.total if total is not None else (current or 0)
            if self._resources.get(job.resource_key) == job.job_id:
                self._resources.pop(job.resource_key, None)
            snapshot = self._snapshot(job)
        if self._on_update is not None:
            try:
                self._on_update(snapshot)
            except BaseException:
                pass

    def _get(self, job_id: str) -> _Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise KeyError("job not found") from None

    @staticmethod
    def _snapshot(job: _Job) -> dict[str, object]:
        document: dict[str, object] = {
            "job_id": job.job_id,
            "state": job.state,
            "operation": job.operation,
            "phase": job.phase,
            "progress": {
                "current": job.current,
                "total": job.total,
                "unit": "items",
            },
            "error": None if job.error is None else dict(job.error),
        }
        if job.counts:
            document["counts"] = dict(job.counts)
        return document

    @staticmethod
    def _value(result: object, name: str) -> Any:
        try:
            return result.get(name) if isinstance(result, Mapping) else getattr(result, name)
        except BaseException:
            return None

    @classmethod
    def _status(cls, result: object) -> str | None:
        value = cls._value(result, "status")
        return value.casefold() if isinstance(value, str) else None

    @classmethod
    def _cancelled_result(cls, result: object) -> bool:
        status = cls._status(result)
        return bool(status and "cancel" in status)

    @classmethod
    def _failed_result(cls, result: object) -> bool:
        status = cls._status(result)
        return status in {"failed", "error"}

    @staticmethod
    def _cancelled_error(error: BaseException) -> bool:
        try:
            return "cancel" in str(error).casefold()
        except BaseException:
            return False

    @classmethod
    def _safe_counts(cls, result: object) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name in _COUNT_FIELDS:
            value = cls._value(result, name)
            if isinstance(value, int) and not isinstance(value, bool):
                counts[name] = value
        return counts
