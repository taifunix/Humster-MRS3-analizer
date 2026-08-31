"""Panel-only adapter for the isolated unified Performance v2 flow."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
import re
import shutil
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
    performance_v2_database_path,
    require_performance_v2,
)
from .performance_v2_windows import (
    METRICS_VERSION,
    WindowMetrics,
    _cached,
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


def _parse_api_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message=f"{field} must be UTC ISO-8601")
    if not (value.endswith("Z") or value.endswith("+00:00")):
        raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message=f"{field} must be UTC ISO-8601")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message=f"{field} must be UTC ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message=f"{field} must be UTC ISO-8601")
    return parsed.astimezone(timezone.utc)


def _parse_window_payload(payload: Mapping[str, object]) -> tuple[int, Window, Window]:
    if set(payload) != {"strategy_id", "window_a", "window_b"}:
        raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message="unsupported Performance v2 window fields")
    strategy_id = payload["strategy_id"]
    if isinstance(strategy_id, bool) or not isinstance(strategy_id, int) or strategy_id <= 0:
        raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message="strategy_id must be a positive integer")

    windows: list[Window] = []
    for name in ("window_a", "window_b"):
        value = payload[name]
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message=f"{name} must contain exactly two timestamps")
        start = _parse_api_timestamp(value[0], f"{name}.start")
        end = _parse_api_timestamp(value[1], f"{name}.end")
        if start >= end:
            raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message=f"{name} start must be before end")
        windows.append((start, end))
    return strategy_id, windows[0], windows[1]


def _canonical_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_duckdb_lock_error(error: duckdb.IOException) -> bool:
    message = str(error).casefold()
    return "could not set lock on file" in message or "conflicting lock is held" in message


def _is_reparse(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


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
    cleanup_warning: Mapping[str, str] | None = None

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


def _fixed_decimal(value: Decimal, places: int) -> str:
    quantized = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if quantized.is_zero():
        quantized = quantized.copy_abs()
    return format(quantized, "f")


def _normalization_30d(metrics: WindowMetrics) -> dict[str, object]:
    """Return the duration-normalized values without changing raw metrics."""
    start, end = metrics.effective_start_utc, metrics.effective_end_utc
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return {
            "period_days": 30,
            "status": "invalid_duration",
            "observed_days": None,
            "growth_factor": None,
            "return_pct": None,
            "trade_rate": None,
        }
    try:
        elapsed = end - start
        elapsed_microseconds = (
            elapsed.days * 86_400_000_000
            + elapsed.seconds * 1_000_000
            + elapsed.microseconds
        )
    except (ArithmeticError, TypeError, ValueError):
        elapsed_microseconds = 0
    if elapsed_microseconds <= 0:
        return {
            "period_days": 30,
            "status": "invalid_duration",
            "observed_days": None,
            "growth_factor": None,
            "return_pct": None,
            "trade_rate": None,
        }

    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_UP
        observed_days = Decimal(elapsed_microseconds) / Decimal(86_400_000_000)
        result: dict[str, object] = {
            "period_days": 30,
            "status": "ok" if observed_days >= 1 else "too_short",
            "observed_days": _fixed_decimal(observed_days, 6),
            "growth_factor": None,
            "return_pct": None,
            "trade_rate": None,
        }
        if observed_days < 1:
            return result

        try:
            growth = metrics.growth_factor
            if growth is None:
                raise ValueError("missing growth factor")
            growth = growth if isinstance(growth, Decimal) else Decimal(str(growth))
            if not growth.is_finite() or growth < 0:
                raise ValueError("invalid growth factor")
            if growth == 0:
                result["growth_factor"] = "0.00000000"
                result["return_pct"] = "-100.0000"
            else:
                growth_30d = (Decimal(30) * growth.ln() / observed_days).exp()
                if not growth_30d.is_finite() or growth_30d >= Decimal("1e18"):
                    raise ValueError("invalid 30-day growth factor")
                result["growth_factor"] = _fixed_decimal(growth_30d, 8)
                result["return_pct"] = _fixed_decimal((growth_30d - 1) * 100, 4)
        except (ArithmeticError, TypeError, ValueError):
            pass

        trade_count = metrics.trade_count
        if isinstance(trade_count, int) and not isinstance(trade_count, bool) and trade_count >= 0:
            try:
                result["trade_rate"] = _fixed_decimal(Decimal(trade_count) * 30 / observed_days, 4)
            except (ArithmeticError, TypeError, ValueError):
                pass
        return result


def resolve_active_strategy(
    connection: duckdb.DuckDBPyConnection, strategy_id: int
) -> tuple[int, int, dict[str, object]]:
    """Resolve one ACTIVE strategy and its current result in one join."""
    row = connection.execute(
        """
        select s.strategy_id, r.result_id, s.strategy_name, s.symbol, s.side,
               s.timeframe, s.order_count, r.report_start_utc, r.report_end_utc
          from strategies s
          join strategy_results r
            on r.result_id = s.current_result_id
           and r.strategy_id = s.strategy_id
         where s.strategy_id = ? and s.lifecycle_status = 'ACTIVE'
        """,
        [strategy_id],
    ).fetchone()
    if row is None:
        raise PerformanceV2ApiError("PERFORMANCE_V2_NOT_FOUND", status=404, message="strategy is not available")
    return int(row[0]), int(row[1]), {
        "strategy_id": int(row[0]),
        "strategy_name": str(row[2]),
        "symbol": str(row[3]),
        "side": str(row[4]),
        "timeframe": str(row[5]),
        "order_count": int(row[6]),
        "result_id": int(row[1]),
        "report_start_utc": row[7],
        "report_end_utc": row[8],
    }


def performance_v2_catalog(connection: duckdb.DuckDBPyConnection) -> dict[str, object]:
    """Return active strategies whose current result belongs to that strategy."""
    rows = connection.execute(
        """
        select s.strategy_id, r.result_id, s.strategy_name, s.symbol, s.side,
               s.timeframe, s.close_ma_len, s.order_count, r.report_start_utc, r.report_end_utc
          from strategies s
          join strategy_results r
            on r.result_id = s.current_result_id
           and r.strategy_id = s.strategy_id
         where s.lifecycle_status = 'ACTIVE'
         order by s.strategy_name, s.strategy_id
        """
    ).fetchall()
    keys = ("strategy_id", "result_id", "strategy_name", "symbol", "side", "timeframe", "close_ma_len", "order_count", "report_start_utc", "report_end_utc")
    strategies = [{key: _json_value(value) for key, value in zip(keys, row)} for row in rows]
    orders_by_strategy: dict[int, list[dict[str, object]]] = {int(strategy["strategy_id"]): [] for strategy in strategies}
    if orders_by_strategy:
        placeholders = ", ".join("?" for _ in orders_by_strategy)
        order_rows = connection.execute(
            f"""
            select strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x
              from strategy_orders
             where strategy_id in ({placeholders})
             order by strategy_id, order_id
            """,
            list(orders_by_strategy),
        ).fetchall()
        order_keys = ("order_id", "open_ma_len", "open_multiplier", "shift_bp", "lot_x")
        for strategy_id, *values in order_rows:
            orders_by_strategy[int(strategy_id)].append({key: _json_value(value) for key, value in zip(order_keys, values)})
    for strategy in strategies:
        strategy["current_result_id"] = strategy["result_id"]
        strategy["orders"] = orders_by_strategy[int(strategy["strategy_id"])]
    return {"strategies": strategies}


def _window_document(metrics: WindowMetrics) -> dict[str, object]:
    document = _json_value(asdict(metrics))  # type: ignore[assignment]
    document["normalization_30d"] = _normalization_30d(metrics)
    return document  # type: ignore[return-value]


def _pair_document(
    strategy_id: int,
    result_id: int,
    pair: tuple[WindowMetrics, WindowMetrics],
    *,
    report_start_utc: datetime | None = None,
    report_end_utc: datetime | None = None,
    compare_func: Callable[..., object] = compare_window_pair_geometrically,
) -> dict[str, object]:
    comparison = compare_func(pair[0], pair[1])
    document: dict[str, object] = {
        "strategy_id": strategy_id,
        "result_id": result_id,
        "window_a": _window_document(pair[0]),
        "window_b": _window_document(pair[1]),
        "comparison": _json_value(asdict(comparison)),
    }
    if report_start_utc is not None:
        document["report_start_utc"] = _canonical_utc(report_start_utc)
    if report_end_utc is not None:
        document["report_end_utc"] = _canonical_utc(report_end_utc)
    return document


def _read_persisted_window_pair(
    database_path: Path,
    strategy_id: int,
    window_a: Window,
    window_b: Window,
) -> dict[str, object]:
    """Read exact cache keys after a transaction conflict; never calculate."""
    with duckdb.connect(str(database_path), read_only=True) as connection:
        require_performance_v2(connection)
        _, result_id, identity = resolve_active_strategy(connection, strategy_id)
        pair = (
            _cached(connection, result_id, window_a[0], window_a[1], METRICS_VERSION),
            _cached(connection, result_id, window_b[0], window_b[1], METRICS_VERSION),
        )
        if pair[0] is None or pair[1] is None:
            raise PerformanceV2ApiError(
                "PERFORMANCE_V2_CACHE_CONFLICT",
                status=409,
                message="window cache transaction conflict",
            )
        return _pair_document(
            strategy_id,
            result_id,
            (pair[0], pair[1]),
            report_start_utc=identity["report_start_utc"],
            report_end_utc=identity["report_end_utc"],
        )


def calculate_performance_v2_windows(
    database_path: Path,
    strategy_id: int,
    window_a: Window,
    window_b: Window,
    *,
    window_pair_func: Callable[..., tuple[WindowMetrics, WindowMetrics]] | None = None,
    compare_func: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Calculate/cache A and B in one write transaction for one active strategy."""
    window_pair_func = window_pair_func or get_or_calculate_window_pair
    compare_func = compare_func or compare_window_pair_geometrically
    try:
        connection = duckdb.connect(str(database_path), read_only=False)
    except duckdb.IOException as error:
        if _is_duckdb_lock_error(error):
            raise PerformanceV2ApiError("PERFORMANCE_V2_LOCKED", status=409, message="Performance v2 database is locked") from error
        raise
    connection_open = True
    try:
        require_performance_v2(connection)
        _, result_id, identity = resolve_active_strategy(connection, strategy_id)
        try:
            connection.execute("begin transaction")
            pair = window_pair_func(connection, result_id, window_a, window_b)
            connection.execute("commit")
        except duckdb.TransactionException:
            try:
                connection.execute("rollback")
            except duckdb.Error:
                pass
            finally:
                connection.close()
                connection_open = False
            return _read_persisted_window_pair(database_path, strategy_id, window_a, window_b)
        except duckdb.IOException as error:
            try:
                connection.execute("rollback")
            except duckdb.Error:
                pass
            if _is_duckdb_lock_error(error):
                raise PerformanceV2ApiError("PERFORMANCE_V2_LOCKED", status=409, message="Performance v2 database is locked") from error
            raise
        except BaseException:
            try:
                connection.execute("rollback")
            except duckdb.Error:
                pass
            raise
        return _pair_document(
            strategy_id,
            result_id,
            pair,
            report_start_utc=identity["report_start_utc"],
            report_end_utc=identity["report_end_utc"],
            compare_func=compare_func,
        )
    finally:
        if connection_open:
            connection.close()


def _cleanup_exact_directory(path: Path, *tail: str) -> None:
    """Remove only the contents of one known tester/output directory."""
    raw = Path(path).absolute()
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current /= part
        if current.exists() and _is_reparse(current):
            raise ValueError("cleanup target contains a symlink or reparse point")
    resolved = raw.resolve()
    if tuple(resolved.parts[-len(tail):]) != tail:
        raise ValueError("cleanup target is outside the configured exact path")
    if not resolved.exists():
        return
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("cleanup target is not a real directory")
    for child in resolved.iterdir():
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        elif _is_reparse(child):
            raise ValueError("cleanup child is a symlink or reparse point")
        else:
            shutil.rmtree(child)


def _cleanup_performance_sources(report_root: Path, strategy_root: Path) -> None:
    _cleanup_exact_directory(report_root, "tester", "report", "my_test")
    _cleanup_exact_directory(strategy_root, "Output", "strategies")
    stale_manifest = strategy_root.resolve().parent / "strategy_manifest.json"
    if stale_manifest.is_symlink():
        raise ValueError("cleanup manifest is a symlink")
    if stale_manifest.exists():
        if not stale_manifest.is_file():
            raise ValueError("cleanup manifest is not a regular file")
        stale_manifest.unlink()


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
            ),
            progress=import_progress,
        )
        if not imported.committed or imported.database_path is None:
            raise ValueError("Performance v2 import did not commit")
        target = Path(imported.database_path).resolve()
        with duckdb.connect(str(target), read_only=False) as connection:
            require_performance_v2(connection)
            counts = {
                "strategy_count": int(connection.execute("select count(*) from strategies").fetchone()[0]),
                "order_count": int(connection.execute("select count(*) from strategy_orders").fetchone()[0]),
                "plateau_count": int(connection.execute("select count(*) from analysis_plateaus").fetchone()[0]),
                "result_count": int(connection.execute("select count(*) from strategy_results").fetchone()[0]),
            }
            if progress is not None:
                progress({
                    "stage": "READBACK_VERIFIED",
                    "completed": counts["result_count"],
                    "total": counts["result_count"],
                })
        cleanup_warning: Mapping[str, str] | None = None
        if request.strategy_root is not None:
            try:
                _cleanup_performance_sources(request.report_root, request.strategy_root)
            except Exception as error:
                cleanup_warning = {
                    "code": "CLEANUP_FAILED",
                    "message": _safe_cleanup_message(error),
                }
        return PerformanceV2PanelResult(
            imported.import_id,
            imported.status,
            imported.imported_count,
            imported.skipped_count,
            imported.rejected_count,
            target,
            imported.audit_path,
            windows=(),
            cleanup_warning=cleanup_warning,
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
        except BaseException as error:
            with self._lock:
                self._jobs[job_id].update(state="FAILED", phase="FAILED", error=_safe_import_error(error))
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
            "cleanup_warning": result.cleanup_warning,
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
    "PerformanceV2ApiError",
    "LocalPerformanceV2Jobs",
    "LocalPerformanceV2Service",
    "PerformanceV2PanelRequest",
    "PerformanceV2PanelResult",
    "calculate_performance_v2_windows",
    "performance_v2_catalog",
    "resolve_active_strategy",
]
