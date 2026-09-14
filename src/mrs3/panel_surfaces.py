"""Local v2 adapter for fresh Source v6 surface selection and publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from threading import RLock, Thread
import secrets
from types import SimpleNamespace
from typing import Callable, Mapping, Sequence

from .source_v6_coverage import (
    CANONICAL_READINESS_CLOSE_LENGTHS,
    CANONICAL_READINESS_SHIFTS_BP,
    missing_cells as canonical_missing_cells,
    ready_intervals as canonical_ready_intervals,
)
from .source_v6_materializer import materialize_source_v6, materialize_source_v6_from_database
from .source_v6_storage import (
    fragment_metadata,
    iter_fragment_ids_parallel,
    quarantine_details,
    source_content_digest,
    validate_source_v6_database,
)
from .source_v6_surface_fresh import publish_multiscope_surface, suggested_multiscope_surface_filename
from .error_sanitization import redact_local_paths


_NOT_READY = "n/r - Check gaps"
_PNL_THRESHOLD = Decimal("10")


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _interval_payload(start: date, end: date, *, kind: str) -> dict[str, object]:
    return {"kind": kind, "start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1}


def _metadata_interval(items: Sequence[object]) -> dict[str, object] | None:
    if not items:
        return None
    try:
        start_ms = min(int(item.report_start_ms) for item in items)
        end_ms = max(int(item.report_end_ms) for item in items)
        start = _utc_day(start_ms)
        end = _utc_day(end_ms - 1)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if end < start:
        return None
    return _interval_payload(start, end, kind="RAW")


def _pnl_value(metrics: Mapping[str, object]) -> Decimal | None:
    values = [metrics[name] for name in ("TotalPnLPercent", "Total PnL, %") if name in metrics]
    if not values:
        return None
    try:
        parsed = tuple(Decimal(str(value).strip()) for value in values)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed or any(not value.is_finite() for value in parsed) or len(set(parsed)) != 1:
        return None
    return parsed[0]


def _pnl_preview_values(
    items: Sequence[object], ready_interval: object | None,
) -> tuple[Decimal, ...] | None:
    if ready_interval is None or not items:
        return None
    try:
        ready_start = ready_interval.start
        ready_end = ready_interval.end
        start_ms = int(datetime.combine(ready_start, datetime.min.time(), timezone.utc).timestamp() * 1000)
        end_ms = int(datetime.combine(ready_end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp() * 1000)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    by_point: dict[str, list[object]] = {}
    for item in items:
        try:
            by_point.setdefault(str(item.point.canonical_key), []).append(item)
        except AttributeError:
            return None
    # A point with two metadata fragments cannot provide a trustworthy preview.
    if any(len(fragments) != 1 for fragments in by_point.values()):
        return None
    values: list[Decimal] = []
    for item in (fragments[0] for fragments in by_point.values()):
        try:
            if int(item.report_start_ms) > start_ms or int(item.report_end_ms) < end_ms:
                return None
            metrics = item.metrics
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        if not isinstance(metrics, Mapping):
            return None
        value = _pnl_value(metrics)
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _pnl_preview(items: Sequence[object], ready_interval: object | None) -> dict[str, object]:
    values = _pnl_preview_values(items, ready_interval)
    if not values:
        return {"available": False}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / Decimal("2")
    above = sum(value > _PNL_THRESHOLD for value in values)
    percent = Decimal(above) * Decimal("100") / Decimal(len(values))
    return {
        "available": True,
        "total_points": len(values),
        "pnl_gt_10_count": above,
        "pnl_gt_10_percent": _decimal_text(percent),
        "pnl_median_pct": _decimal_text(median),
        "pnl_max_pct": _decimal_text(max(values)),
    }


def _common_interval(intervals: Sequence[object]) -> dict[str, object] | None:
    if not intervals:
        return None
    try:
        start = max(item.start for item in intervals)
        end = min(item.end for item in intervals)
    except (AttributeError, TypeError, ValueError):
        return None
    return _interval_payload(start, end, kind="READY") if start <= end else None


def _common_ready_witness(intervals: Sequence[object]) -> object | None:
    if not intervals:
        return None
    try:
        start = max(item.start for item in intervals)
        end = min(item.end for item in intervals)
    except (AttributeError, TypeError, ValueError):
        return None
    return SimpleNamespace(start=start, end=end) if start <= end else None


def _safe_failure_reason(error: BaseException, output: Path) -> str:
    message = str(error) or type(error).__name__
    for path in {str(output), str(output.resolve())}:
        message = message.replace(path, "<surface-target>")
    message = redact_local_paths(message)
    return f"{type(error).__name__}: {message}"[:500]


@dataclass(frozen=True, slots=True)
class _Pending:
    token: str
    source_database: Path
    source_content_digest: str
    metadata: tuple[object, ...]
    rows: tuple[dict[str, str], ...]


@dataclass(slots=True)
class _PublicationJob:
    running: bool = True
    phase: str = "QUEUED"
    completed: int = 0
    total: int = 0
    detail: str | None = None
    target: str | None = None
    scopes: tuple[str, ...] = ()
    error: str | None = None


def _scope_key(item: object) -> str:
    if isinstance(item, Mapping):
        if "scope_key" in item:
            return _scope_key(item["scope_key"])
        pair = item.get("pair", item.get("symbol", ""))
        side = item.get("side", "")
        timeframe = item.get("timeframe", "")
        value = f"{pair}|{side}|{timeframe}"
    elif isinstance(item, str):
        value = item.strip()
    else:
        raise ValueError("scope key must be a string")
    parts = value.split("|")
    if len(parts) != 3 or any(not part.strip() for part in parts):
        raise ValueError("scope key must be Pair|Side|TF")
    return "|".join(part.strip() for part in parts)


def _item_scope(item: object) -> str:
    point = getattr(item, "point", None)
    try:
        return "|".join((str(point.symbol), str(point.side), str(point.timeframe)))
    except AttributeError as error:
        raise ValueError("Source v6 metadata has no point identity") from error


def _utc_day(timestamp_ms: object) -> date:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, timezone.utc).date()


class LocalSurfacesService:
    """Read-only coverage and immutable fresh-surface publication boundary."""

    def __init__(
        self,
        *,
        validate_source: Callable[[str | Path], object] | None = None,
        read_metadata: Callable[[str | Path], Sequence[object]] | None = None,
        readiness: Callable[..., Sequence[object]] | None = None,
        missing_cells: Callable[..., Sequence[object]] | None = None,
        read_fragments: Callable[[str | Path], Sequence[object]] | None = None,
        read_quarantine: Callable[[str | Path], Sequence[Mapping[str, object]]] | None = None,
        materialize: Callable[[Sequence[object], Sequence[str]], object] | None = None,
        publish: Callable[..., object] | None = None,
        workers: int = 1,
    ) -> None:
        self._validate_source = validate_source or validate_source_v6_database
        self._read_metadata = read_metadata or fragment_metadata
        self._readiness = readiness or canonical_ready_intervals
        self._missing_cells = missing_cells or canonical_missing_cells
        self._read_fragments = read_fragments
        self._read_quarantine = read_quarantine or quarantine_details
        self._workers = max(1, int(workers))
        self._materialize = materialize or materialize_source_v6
        self._publish = publish or publish_multiscope_surface
        self._lock = RLock()
        self._pending: _Pending | None = None
        self._publication: _PublicationJob | None = None

    def preflight(self, source_db: str | Path) -> dict[str, object]:
        source = Path(source_db)
        info = self._validate_source(source)
        quarantines = tuple(self._read_quarantine(source))
        metadata = tuple(self._read_metadata(source))
        digest = (
            str(info["source_content_digest"])
            if isinstance(info, Mapping) and info.get("source_content_digest")
            else source_content_digest(str(item.fragment_id) for item in metadata)
        )
        intervals = tuple(
            self._readiness(
                metadata,
                required_shifts=CANONICAL_READINESS_SHIFTS_BP,
                required_close_lengths=CANONICAL_READINESS_CLOSE_LENGTHS,
            )
        )
        ready_by_scope = {
            str(getattr(interval, "scope_key")): interval
            for interval in intervals
        }
        metadata_by_scope: dict[str, list[object]] = {}
        for item in metadata:
            metadata_by_scope.setdefault(_item_scope(item), []).append(item)
        scope_keys = sorted(metadata_by_scope)
        rows_list: list[dict[str, object]] = []
        for key in scope_keys:
            members = metadata_by_scope[key]
            ready_interval = ready_by_scope.get(key)
            status = "READY" if ready_interval is not None and not quarantines else _NOT_READY
            period = (
                _interval_payload(ready_interval.start, ready_interval.end, kind="READY")
                if status == "READY" else _metadata_interval(members)
            )
            row = {
                "scope_key": key,
                "pair": key.split("|", 2)[0],
                "side": key.split("|", 2)[1],
                "timeframe": key.split("|", 2)[2],
                "status": status,
                "period": period,
                "pnl_preview": _pnl_preview(members, ready_interval if status == "READY" else None),
            }
            rows_list.append(row)
        rows = tuple(rows_list)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._pending = _Pending(token, source, digest, metadata, rows)
        groups: list[dict[str, object]] = []
        for key in sorted({row["pair"] + "|" + row["side"] for row in rows}):
            pair, side = key.split("|", 1)
            group_rows = tuple(row for row in rows if row["pair"] == pair and row["side"] == side)
            ready_rows = tuple(row for row in group_rows if row["status"] == "READY")
            group_items = tuple(item for row in ready_rows for item in metadata_by_scope[row["scope_key"]])
            group_intervals = tuple(
                ready_by_scope[row["scope_key"]] for row in ready_rows if row["scope_key"] in ready_by_scope
            )
            group_preview = _pnl_preview(group_items, _common_ready_witness(group_intervals))
            groups.append(
                {
                    "pair": pair,
                    "side": side,
                    "timeframes": list(group_rows),
                    "summary": {
                        "timeframes": len(group_rows),
                        "common_interval": _common_interval(group_intervals),
                        "ready": {"count": len(ready_rows), "total": len(group_rows)},
                        "pnl_preview": group_preview,
                    },
                }
            )
        return {
            "phase": "PREFLIGHT_READY", "token": token, "rows": list(rows), "groups": groups,
            "quarantines": list(quarantines),
        }

    def gaps(self, token: str, scope: object) -> dict[str, object]:
        pending = self._current(token)
        key = _scope_key(scope)
        row = next((item for item in pending.rows if item["scope_key"] == key), None)
        if row is None:
            raise ValueError("scope is not present in the latest coverage preflight")
        members = tuple(item for item in pending.metadata if _item_scope(item) == key)
        if members:
            start = min(_utc_day(item.report_start_ms) for item in members)
            end = max(_utc_day(item.report_end_ms) for item in members)
            if end <= start:
                end = start + timedelta(days=1)
            point_keys = tuple(sorted(str(item.point.canonical_key) for item in members))
            cells = tuple(
                self._missing_cells(
                    members,
                    start=start,
                    end=end,
                    point_keys=point_keys,
                )
            )
        else:
            cells = ()
        gaps = []
        for cell in cells:
            day = getattr(cell, "utc_day")
            gaps.append({"utc_day": day.isoformat() if isinstance(day, date) else str(day), "status": "MISSING"})
        required = {
            (int(shift), int(close))
            for shift in CANONICAL_READINESS_SHIFTS_BP
            for close in CANONICAL_READINESS_CLOSE_LENGTHS
        }
        observed = {
            (int(getattr(item.point, "shift_bp")), int(getattr(item.point, "close_ma_length")))
            for item in members
        }
        missing_witnesses = [
            {"shift_bp": shift, "close_ma_length": close}
            for shift, close in sorted(required - observed)
        ]
        status = _NOT_READY if gaps else row["status"]
        reason = (
            "coverage gaps detected" if gaps else
            "canonical witness grid is incomplete" if missing_witnesses else
            "no common READY interval" if status == _NOT_READY else
            "READY"
        )
        return {
            "scope_key": key,
            "status": status,
            "reason": reason,
            "gaps": gaps,
            "missing_witnesses": missing_witnesses,
        }

    def select(self, token: str, scope_keys: Sequence[object]) -> dict[str, object]:
        pending = self._current(token)
        if isinstance(scope_keys, (str, bytes)) or not isinstance(scope_keys, Sequence):
            raise ValueError("scope keys must be a list")
        selected = tuple(sorted({_scope_key(item) for item in scope_keys}))
        if not selected:
            raise ValueError("at least one scope is required")
        rows = {row["scope_key"]: row for row in pending.rows}
        for key in selected:
            if key not in rows:
                raise ValueError(f"scope is not available: {key}")
            if rows[key]["status"] != "READY":
                raise ValueError(f"scope is not READY: {key}")
        witnesses = {
            item.scope_key: item for item in self._readiness(
                pending.metadata,
                required_shifts=CANONICAL_READINESS_SHIFTS_BP,
                required_close_lengths=CANONICAL_READINESS_CLOSE_LENGTHS,
            )
        }
        preview = SimpleNamespace(scopes=tuple(
            SimpleNamespace(scope_key=key, ready_witness=witnesses[key]) for key in selected
        ))
        return {
            "phase": "SELECTION_READY", "token": token, "scopes": list(selected),
            "suggested_filename": suggested_multiscope_surface_filename(preview),
        }

    def publish(
        self, token: str, scope_keys: Sequence[object], target: str | Path,
        filename: str | None = None,
    ) -> dict[str, object]:
        """Publish into a fresh-surface output directory.

        The canonical publisher owns the immutable filename and atomic staging;
        an existing file target is rejected before any source read.
        """
        selected = self.select(token, scope_keys)["scopes"]
        pending = self._current(token)
        output = Path(target)
        if output.exists() and output.is_file():
            raise FileExistsError("surface target already exists")
        fragments = self._selected_fragments(pending, selected)
        materialized = self._materialize_facts(fragments, selected, pending.source_content_digest)
        artifact = self._publish(
            output, materialized, source_database=pending.source_database, filename=filename,
        )
        return {
            "phase": "COMMITTED",
            "target": Path(str(artifact)).name,
            "scopes": list(selected),
        }

    def start_publish(
        self, token: str, scope_keys: Sequence[object], target: str | Path,
        filename: str | None = None,
    ) -> dict[str, object]:
        """Start a publication job; status deliberately exposes no local paths."""
        selected = tuple(self.select(token, scope_keys)["scopes"])
        output = Path(target)
        if output.exists() and output.is_file():
            raise FileExistsError("surface target already exists")
        with self._lock:
            if self._publication is not None and self._publication.running:
                raise RuntimeError("surface publication is already running")
            job = _PublicationJob(scopes=selected)
            self._publication = job
        Thread(target=self._run_publish, args=(job, token, selected, output, filename), daemon=True).start()
        return self.publish_status()

    def publish_status(self) -> dict[str, object]:
        with self._lock:
            job = self._publication
            if job is None:
                return {"running": False, "phase": "IDLE", "completed": 0, "total": 0, "detail": None, "target": None, "scopes": [], "error": None}
            return {
                "running": job.running,
                "phase": job.phase,
                "completed": job.completed,
                "total": job.total,
                "detail": job.detail,
                "target": job.target,
                "scopes": list(job.scopes),
                "error": job.error,
            }

    def _run_publish(
        self, job: _PublicationJob, token: str, selected: tuple[str, ...], output: Path,
        filename: str | None,
    ) -> None:
        def progress(phase: str, *, completed: int = 0, total: int = 0, detail: str | None = None) -> None:
            with self._lock:
                if self._publication is job:
                    job.phase, job.completed, job.total, job.detail = phase, completed, total, detail

        try:
            pending = self._current(token)
            progress("MATERIALIZING")
            materialized = self._materialize_selection(pending, selected, progress)
            # W6: the real publisher validates payload identity across
            # processes; injected publishers keep their narrower signature.
            extra = {"workers": self._workers} if self._publish is publish_multiscope_surface else {}
            artifact = self._publish(
                output, materialized, source_database=pending.source_database,
                progress_callback=progress, filename=filename, **extra,
            )
            with self._lock:
                job.running, job.phase, job.target, job.detail = False, "COMMITTED", Path(str(artifact)).name, None
        except BaseException as error:
            with self._lock:
                job.running, job.phase, job.error, job.detail = (
                    False, "FAILED", _safe_failure_reason(error, output), None
                )

    def _selected_ids(self, pending: _Pending, selected: Sequence[str]) -> tuple[str, ...]:
        wanted = set(selected)
        return tuple(
            str(item.fragment_id) for item in pending.metadata
            if _item_scope(item) in wanted
        )

    def _selected_fragments(
        self, pending: _Pending, selected: Sequence[str],
        progress_callback: Callable[[int, int], object] | None = None,
    ) -> tuple[object, ...]:
        if self._read_fragments is not None:
            return tuple(self._read_fragments(pending.source_database))
        return iter_fragment_ids_parallel(
            pending.source_database, self._selected_ids(pending, selected), workers=self._workers,
            progress_callback=progress_callback,
        )

    def _materialize_selection(
        self, pending: _Pending, selected: Sequence[str], progress: Callable[..., None],
    ) -> object:
        """Measure the selection without hydrating it here (W1--W5).

        The default path never decodes a payload in this process: the workers
        hold each combination's fragments only while they measure it, and the
        publisher copies payload bytes in SQL. The injected seams keep the
        hydrated path, which is what the equivalence tests compare against.
        """
        if self._read_fragments is None and self._materialize is materialize_source_v6:
            return materialize_source_v6_from_database(
                pending.source_database, selected, metadata=pending.metadata,
                workers=self._workers, source_content_digest_value=pending.source_content_digest,
                progress_callback=lambda completed, total: progress(
                    "MATERIALIZING", completed=completed, total=total
                ),
            )
        progress("HYDRATING")
        fragments = self._selected_fragments(
            pending, selected,
            progress_callback=lambda completed, total: progress(
                "HYDRATING", completed=completed, total=total
            ),
        )
        progress("MATERIALIZING", total=len(selected))
        materialized = self._materialize_facts(fragments, selected, pending.source_content_digest)
        progress("MATERIALIZING", completed=len(selected), total=len(selected))
        return materialized

    def _materialize_facts(
        self, fragments: Sequence[object], selected: Sequence[str], source_digest: str,
    ) -> object:
        if self._materialize is materialize_source_v6:
            return self._materialize(
                fragments, selected, source_content_digest_value=source_digest,
            )
        return self._materialize(fragments, selected)

    def _current(self, token: str) -> _Pending:
        with self._lock:
            pending = self._pending
        if not isinstance(token, str) or pending is None or token != pending.token:
            raise ValueError("stale coverage token")
        return pending


SurfacesService = LocalSurfacesService
