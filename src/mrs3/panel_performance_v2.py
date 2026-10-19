"""Panel-only adapter for the isolated unified Performance v2 flow."""

from __future__ import annotations

from dataclasses import (
    dataclass,
)
from datetime import datetime, timezone
from decimal import (
    Decimal,
)
from pathlib import Path
import re
from threading import RLock, Thread
import math
from time import perf_counter
from typing import Callable, Mapping
from uuid import uuid4

import duckdb

from .performance_v2_import import (
    PerformanceV2ImportRequest,
    PerformanceV2ImportResult,
    _safe_phase_snapshot,
    import_performance_v2,
)
from .performance_v2_store import (
    PerformanceV2Config,
    performance_v2_database_path,
    require_performance_v2,
)
from .performance_v2_windows import (
    WindowMetrics,
    compare_window_pair_geometrically,
    get_or_calculate_window_pair,
)


Window = tuple[datetime | str, datetime | str]


class PerformanceV2ApiError(ValueError):
    """An expected API failure with a stable client-facing status/code."""

    def __init__(self, code: str, *, status: int, message: str | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(message or code)






def _canonical_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_duckdb_lock_error(error: duckdb.IOException) -> bool:
    message = str(error).casefold()
    return "could not set lock on file" in message or "conflicting lock is held" in message


















def _safe_cleanup_message(error: BaseException) -> str:
    message = str(error).strip() or type(error).__name__
    message = re.sub(r"(?i)(?:[A-Za-z]:[\\/]|/).*$", "<path>", message)
    return message[:512]


def _safe_import_error(error: BaseException) -> dict[str, str]:
    return {
        "code": "PERFORMANCE_V2_IMPORT_FAILED",
        "message": _safe_cleanup_message(error),
    }


@dataclass(frozen=True, slots=True)
class PerformanceV2PanelRequest:
    inbox: Path
    report_root: Path
    config: PerformanceV2Config
    mode: str = "ADD"
    replacement_strategy_ids: Mapping[str, int] | None = None
    window_a: Window | None = None
    window_b: Window | None = None
    strategy_root: Path | None = None
    clear_retest_on_success: bool = False
    test_start: str | None = None
    test_end: str | None = None
    listing_dates_path: Path | None = None
    listing_dates_root: Path | None = None
    listing_dates_sheet: str | None = None
    tester_strategy_root: Path | None = None
    tester_bot_root: Path | None = None
    expected_current_result_ids: Mapping[str, int] | None = None
    expected_inbox_manifest_sha256: str | None = None
    expected_collection_id: str | None = None


@dataclass(frozen=True, slots=True)
class PerformanceV2PanelResult:
    import_id: str
    status: str
    imported_count: int
    skipped_count: int
    rejected_count: int
    database_path: Path
    audit_path: Path | None
    strategy_count: int
    order_count: int
    plateau_count: int
    result_count: int
    failure_report_path: Path | None = None
    windows: tuple[dict[str, object], ...] = ()
    cleanup_warning: Mapping[str, str] | None = None
    successful_replacements: tuple[Mapping[str, int], ...] = ()
    failures: tuple[Mapping[str, object], ...] = ()

    @property
    def window_count(self) -> int:
        return len(self.windows)


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return _canonical_utc(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    return value


















class LocalPerformanceV2Service:
    """Import one committed inbox, then cache the requested A/B windows."""

    def __init__(
        self,
        *,
        import_func: Callable[[PerformanceV2ImportRequest], PerformanceV2ImportResult] | None = None,
        window_pair_func: Callable[..., tuple[WindowMetrics, WindowMetrics]] = get_or_calculate_window_pair,
        compare_func: Callable[..., object] = compare_window_pair_geometrically,
    ) -> None:
        self.import_func = import_func or import_performance_v2
        self.window_pair_func = window_pair_func
        self.compare_func = compare_func

    def run(
        self,
        request: PerformanceV2PanelRequest,
        *,
        progress: Callable[[object], object] | None = None,
    ) -> PerformanceV2PanelResult:
        if not isinstance(request, PerformanceV2PanelRequest):
            raise TypeError("request must be PerformanceV2PanelRequest")
        if request.mode not in {"ADD", "REPLACE"}:
            raise ValueError("v2 import mode must be ADD or REPLACE")
        if progress is not None:
            progress({"stage": "IMPORTING", "completed": 0, "total": 0})
        def import_progress(stage: str, completed: int, total: int) -> None:
            if progress is not None:
                progress({"stage": stage, "completed": completed, "total": total})

        imported = self.import_func(
            PerformanceV2ImportRequest(
                request.inbox,
                request.report_root,
                request.config,
                mode=request.mode,
                replacement_strategy_ids=request.replacement_strategy_ids,
                strategy_root=request.strategy_root,
                clear_retest_on_success=request.clear_retest_on_success,
                test_start=request.test_start,
                test_end=request.test_end,
                listing_dates_path=request.listing_dates_path,
                listing_dates_root=request.listing_dates_root,
                listing_dates_sheet=request.listing_dates_sheet,
                expected_current_result_ids=request.expected_current_result_ids,
                expected_inbox_manifest_sha256=request.expected_inbox_manifest_sha256,
                expected_collection_id=request.expected_collection_id,
            ),
            progress=import_progress,
        )
        if imported.status == "FAILED":
            return PerformanceV2PanelResult(
                imported.import_id,
                imported.status,
                imported.imported_count,
                imported.skipped_count,
                imported.rejected_count,
                Path(imported.database_path or performance_v2_database_path(request.config)).resolve(),
                imported.audit_path,
                0,
                0,
                0,
                0,
                failure_report_path=imported.failure_report_path,
                successful_replacements=imported.successful_replacements,
                failures=imported.failures,
            )
        if not imported.committed or imported.database_path is None:
            raise ValueError("Performance v2 import did not commit")
        target = Path(imported.database_path).resolve()
        readback_started: float | None
        try:
            started = float(perf_counter())
            readback_started = started if math.isfinite(started) else None
        except BaseException:
            readback_started = None
        try:
            with duckdb.connect(str(target), read_only=False) as connection:
                require_performance_v2(connection)
                counts = {
                    "strategy_count": int(connection.execute("select count(*) from strategies").fetchone()[0]),
                    "order_count": int(connection.execute("select count(*) from strategy_orders").fetchone()[0]),
                    "plateau_count": int(connection.execute("select count(*) from analysis_plateaus").fetchone()[0]),
                    "result_count": int(connection.execute("select count(*) from strategy_results").fetchone()[0]),
                }
        finally:
            try:
                finished = float(perf_counter())
                readback_seconds = (
                    max(0.0, finished - readback_started)
                    if readback_started is not None and math.isfinite(finished)
                    else None
                )
            except BaseException:
                readback_seconds = None
        phase_seconds: dict[str, float] = {}
        source_phases = imported.phases
        phases_valid = source_phases is None or isinstance(source_phases, Mapping)
        if isinstance(source_phases, Mapping):
            try:
                phase_seconds.update(source_phases)
            except BaseException:
                phases_valid = False
                phase_seconds = {}
        if readback_seconds is not None:
            phase_seconds["PANEL_READBACK"] = readback_seconds
        evidence = _safe_phase_snapshot(phase_seconds) if phases_valid else None
        if progress is not None:
            progress({
                "stage": "READBACK_VERIFIED",
                "completed": counts["result_count"],
                "total": counts["result_count"],
                **({"evidence": {"phase_seconds": evidence}} if evidence else {}),
            })
        return PerformanceV2PanelResult(
            imported.import_id,
            imported.status,
            imported.imported_count,
            imported.skipped_count,
            imported.rejected_count,
            target,
            imported.audit_path,
            windows=(),
            cleanup_warning=None,
            failure_report_path=imported.failure_report_path,
            successful_replacements=imported.successful_replacements,
            failures=imported.failures,
            **counts,
        )

    def _window_pair(
        self,
        connection: duckdb.DuckDBPyConnection,
        result_id: int,
        request: PerformanceV2PanelRequest,
    ) -> tuple[WindowMetrics, WindowMetrics] | None:
        if request.window_a is not None and request.window_b is not None:
            return self.window_pair_func(
                connection, result_id, request.window_a, request.window_b
            )
        if request.window_a is None and request.window_b is None:
            row = connection.execute(
                "select report_start_utc, report_end_utc from strategy_results where result_id = ?",
                [result_id],
            ).fetchone()
            if row is None:
                return None
            window = (row[0], row[1])
            return self.window_pair_func(connection, result_id, window, window)
        raise ValueError("window_a and window_b must be supplied together")


class LocalPerformanceV2Jobs:
    """Small in-process job wrapper used by the panel's separate v2 action."""

    def __init__(
        self,
        *,
        service: LocalPerformanceV2Service | None = None,
        on_update: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.service = service or LocalPerformanceV2Service()
        self.on_update = on_update
        self._lock = RLock()
        self._jobs: dict[str, dict[str, object]] = {}

    def has_active_job(self) -> bool:
        with self._lock:
            return any(job["state"] == "RUNNING" for job in self._jobs.values())

    def active_job_ids(self) -> set[str]:
        with self._lock:
            return {
                str(job_id)
                for job_id, job in self._jobs.items()
                if job.get("state") == "RUNNING"
            }

    def start(self, request: PerformanceV2PanelRequest, *, job_id: str | None = None) -> dict[str, object]:
        job_id = job_id or str(uuid4())
        with self._lock:
            if self.has_active_job():
                raise ValueError("Performance v2 import is already running")
            if not isinstance(job_id, str) or not job_id.strip() or job_id in self._jobs:
                raise ValueError("Performance v2 job id is invalid")
            self._jobs[job_id] = {
                "job_id": job_id,
                "state": "RUNNING",
                "phase": "IMPORTING",
                "progress": {"current": 0, "total": 0, "unit": "reports"},
                "error": None,
            }
        Thread(target=self._worker, args=(job_id, request), daemon=True, name="mrs3-panel-performance-v2").start()
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            try:
                return dict(self._jobs[job_id])
            except KeyError:
                raise KeyError("job not found") from None

    def _worker(self, job_id: str, request: PerformanceV2PanelRequest) -> None:
        evidence: dict[str, object] | None = None

        def progress(value: object) -> None:
            nonlocal evidence
            stage = getattr(value, "stage", None) or (value.get("stage") if isinstance(value, Mapping) else None)
            completed = getattr(value, "completed", None) or (value.get("completed") if isinstance(value, Mapping) else None)
            total = getattr(value, "total", None) or (value.get("total") if isinstance(value, Mapping) else None)
            if stage == "READBACK_VERIFIED":
                evidence = None
                candidate = value.get("evidence") if isinstance(value, Mapping) else None
                phase_seconds = candidate.get("phase_seconds") if isinstance(candidate, Mapping) else None
                snapshot = _safe_phase_snapshot(phase_seconds) if isinstance(phase_seconds, Mapping) else None
                if snapshot:
                    evidence = {"phase_seconds": snapshot}
            with self._lock:
                job = self._jobs[job_id]
                job["phase"] = str(stage or "IMPORTING")
                job["progress"] = {
                    "current": completed if isinstance(completed, int) else 0,
                    "total": total if isinstance(total, int) else 0,
                    "unit": "reports",
                }
        try:
            result = self.service.run(request, progress=progress)
        except BaseException as error:
            with self._lock:
                terminal = {"state": "FAILED", "phase": "FAILED", "error": _safe_import_error(error)}
                if evidence is not None:
                    terminal["evidence"] = evidence
                self._jobs[job_id].update(terminal)
                snapshot = dict(self._jobs[job_id])
            self._updated(snapshot)
            return
        result_document = {
            "import_id": result.import_id,
            "status": result.status,
            "imported_count": result.imported_count,
            "skipped_count": result.skipped_count,
            "rejected_count": result.rejected_count,
            "database_path": str(result.database_path),
            "audit_path": str(result.audit_path) if result.audit_path is not None else None,
            "failure_report_path": str(result.failure_report_path) if result.failure_report_path is not None else None,
            "strategy_count": result.strategy_count,
            "order_count": result.order_count,
            "plateau_count": result.plateau_count,
            "result_count": result.result_count,
            "window_count": result.window_count,
            "windows": list(result.windows),
            "cleanup_warning": result.cleanup_warning,
            "successful_replacements": list(result.successful_replacements),
            "failures": list(result.failures),
        }
        succeeded = result.status == "COMMITTED"
        completed = result.imported_count + result.skipped_count + result.rejected_count
        with self._lock:
            terminal = {
                "state": "COMMITTED" if succeeded else "FAILED",
                "phase": "COMMITTED" if succeeded else "FAILED",
                "error": None if succeeded else {"code": "PERFORMANCE_V2_IMPORT_REJECTED", "message": "all reports were rejected; open failure report"},
                "progress": {"current": completed, "total": completed, "unit": "reports"},
                "result": result_document,
            }
            if evidence is not None:
                terminal["evidence"] = evidence
            self._jobs[job_id].update(terminal)
            snapshot = dict(self._jobs[job_id])
        self._updated(snapshot)

    def _updated(self, document: dict[str, object]) -> None:
        if self.on_update is not None:
            try:
                self.on_update(document)
            except BaseException:
                pass


__all__ = [
    "PerformanceV2ApiError",
    "LocalPerformanceV2Jobs",
    "LocalPerformanceV2Service",
    "PerformanceV2PanelRequest",
    "PerformanceV2PanelResult",
]
