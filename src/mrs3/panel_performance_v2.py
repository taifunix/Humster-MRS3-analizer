"""Panel-only adapter for the isolated unified Performance v2 flow."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock, Thread
from typing import Callable, Mapping
from uuid import uuid4

import duckdb

from .performance_v2_import import (
    PerformanceV2ImportRequest,
    PerformanceV2ImportResult,
    import_performance_v2,
)
from .performance_v2_store import (
    PerformanceV2Config,
    require_performance_v2,
)
from .performance_v2_windows import (
    WindowMetrics,
    compare_window_pair_geometrically,
    get_or_calculate_window_pair,
)


Window = tuple[datetime | str, datetime | str]


@dataclass(frozen=True, slots=True)
class PerformanceV2PanelRequest:
    inbox: Path
    report_root: Path
    config: PerformanceV2Config
    mode: str = "ADD"
    replacement_strategy_ids: Mapping[str, int] | None = None
    window_a: Window | None = None
    window_b: Window | None = None


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
    windows: tuple[dict[str, object], ...] = ()

    @property
    def window_count(self) -> int:
        return len(self.windows)


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    return value


def _window_document(metrics: WindowMetrics) -> dict[str, object]:
    return _json_value(asdict(metrics))  # type: ignore[return-value]


def _pair_document(
    strategy_id: int,
    result_id: int,
    pair: tuple[WindowMetrics, WindowMetrics],
    compare_func: Callable[..., object] = compare_window_pair_geometrically,
) -> dict[str, object]:
    comparison = compare_func(pair[0], pair[1])
    return {
        "strategy_id": strategy_id,
        "result_id": result_id,
        "window_a": _window_document(pair[0]),
        "window_b": _window_document(pair[1]),
        "comparison": _json_value(asdict(comparison)),
    }


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
        imported = self.import_func(
            PerformanceV2ImportRequest(
                request.inbox,
                request.report_root,
                request.config,
                mode=request.mode,
                replacement_strategy_ids=request.replacement_strategy_ids,
            )
        )
        if not imported.committed or imported.database_path is None:
            raise ValueError("Performance v2 import did not commit")
        target = Path(imported.database_path).resolve()
        windows: list[dict[str, object]] = []
        with duckdb.connect(str(target), read_only=False) as connection:
            require_performance_v2(connection)
            counts = {
                "strategy_count": int(connection.execute("select count(*) from strategies").fetchone()[0]),
                "order_count": int(connection.execute("select count(*) from strategy_orders").fetchone()[0]),
                "plateau_count": int(connection.execute("select count(*) from analysis_plateaus").fetchone()[0]),
                "result_count": int(connection.execute("select count(*) from strategy_results").fetchone()[0]),
            }
            current = connection.execute(
                "select strategy_id, current_result_id from strategies "
                "where current_result_id is not null order by strategy_id"
            ).fetchall()
            for strategy_id, result_id in current:
                pair = self._window_pair(connection, int(result_id), request)
                if pair is None:
                    continue
                windows.append(_pair_document(int(strategy_id), int(result_id), pair, self.compare_func))
            if progress is not None:
                progress({
                    "stage": "READBACK_VERIFIED",
                    "completed": counts["result_count"],
                    "total": counts["result_count"],
                })
        return PerformanceV2PanelResult(
            imported.import_id,
            imported.status,
            imported.imported_count,
            imported.skipped_count,
            imported.rejected_count,
            target,
            imported.audit_path,
            windows=tuple(windows),
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
        def progress(value: object) -> None:
            stage = getattr(value, "stage", None) or (value.get("stage") if isinstance(value, Mapping) else None)
            completed = getattr(value, "completed", None) or (value.get("completed") if isinstance(value, Mapping) else None)
            total = getattr(value, "total", None) or (value.get("total") if isinstance(value, Mapping) else None)
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
        except BaseException:
            with self._lock:
                self._jobs[job_id].update(state="FAILED", phase="FAILED", error={"code": "FAILED"})
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
            "strategy_count": result.strategy_count,
            "order_count": result.order_count,
            "plateau_count": result.plateau_count,
            "result_count": result.result_count,
            "window_count": result.window_count,
            "windows": list(result.windows),
        }
        with self._lock:
            self._jobs[job_id].update(
                state="COMMITTED",
                phase="COMMITTED",
                error=None,
                progress={"current": result.result_count, "total": result.result_count, "unit": "reports"},
                result=result_document,
            )
            snapshot = dict(self._jobs[job_id])
        self._updated(snapshot)

    def _updated(self, document: dict[str, object]) -> None:
        if self.on_update is not None:
            try:
                self.on_update(document)
            except BaseException:
                pass


__all__ = [
    "LocalPerformanceV2Jobs",
    "LocalPerformanceV2Service",
    "PerformanceV2PanelRequest",
    "PerformanceV2PanelResult",
]
