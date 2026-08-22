"""Local v2 adapter for fresh Source v6 surface selection and publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
import secrets
from typing import Callable, Mapping, Sequence

from .source_v6_coverage import (
    CANONICAL_READINESS_CLOSE_LENGTHS,
    CANONICAL_READINESS_SHIFTS_BP,
    missing_cells as canonical_missing_cells,
    ready_intervals as canonical_ready_intervals,
)
from .source_v6_materializer import materialize_source_v6
from .source_v6_storage import fragment_metadata, iter_fragments, validate_source_v6_database
from .source_v6_surface_fresh import publish_multiscope_surface


_NOT_READY = "n/r - Check gaps"


@dataclass(frozen=True, slots=True)
class _Pending:
    token: str
    source_database: Path
    metadata: tuple[object, ...]
    rows: tuple[dict[str, str], ...]


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
        materialize: Callable[[Sequence[object], Sequence[str]], object] | None = None,
        publish: Callable[..., object] | None = None,
    ) -> None:
        self._validate_source = validate_source or validate_source_v6_database
        self._read_metadata = read_metadata or fragment_metadata
        self._readiness = readiness or canonical_ready_intervals
        self._missing_cells = missing_cells or canonical_missing_cells
        self._read_fragments = read_fragments or iter_fragments
        self._materialize = materialize or materialize_source_v6
        self._publish = publish or publish_multiscope_surface
        self._lock = RLock()
        self._pending: _Pending | None = None

    def preflight(self, source_db: str | Path) -> dict[str, object]:
        source = Path(source_db)
        self._validate_source(source)
        metadata = tuple(self._read_metadata(source))
        intervals = tuple(
            self._readiness(
                metadata,
                required_shifts=CANONICAL_READINESS_SHIFTS_BP,
                required_close_lengths=CANONICAL_READINESS_CLOSE_LENGTHS,
            )
        )
        ready_scopes = {str(getattr(interval, "scope_key")) for interval in intervals}
        scope_keys = sorted({_item_scope(item) for item in metadata})
        rows = tuple(
            {
                "scope_key": key,
                "pair": key.split("|", 2)[0],
                "side": key.split("|", 2)[1],
                "timeframe": key.split("|", 2)[2],
                "status": "READY" if key in ready_scopes else _NOT_READY,
            }
            for key in scope_keys
        )
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._pending = _Pending(token, source, metadata, rows)
        groups: list[dict[str, object]] = []
        for key in sorted({row["pair"] + "|" + row["side"] for row in rows}):
            pair, side = key.split("|", 1)
            groups.append(
                {
                    "pair": pair,
                    "side": side,
                    "timeframes": [row for row in rows if row["pair"] == pair and row["side"] == side],
                }
            )
        return {"phase": "PREFLIGHT_READY", "token": token, "rows": list(rows), "groups": groups}

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
        return {"scope_key": key, "status": _NOT_READY if gaps else row["status"], "gaps": gaps}

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
        return {"phase": "SELECTION_READY", "token": token, "scopes": list(selected)}

    def publish(self, token: str, scope_keys: Sequence[object], target: str | Path) -> dict[str, object]:
        """Publish into a fresh-surface output directory.

        The canonical publisher owns the immutable filename and atomic staging;
        an existing file target is rejected before any source read.
        """
        selected = self.select(token, scope_keys)["scopes"]
        pending = self._current(token)
        output = Path(target)
        if output.exists() and output.is_file():
            raise FileExistsError("surface target already exists")
        fragments = tuple(self._read_fragments(pending.source_database))
        materialized = self._materialize(fragments, selected)
        artifact = self._publish(output, materialized, source_database=pending.source_database)
        return {
            "phase": "COMMITTED",
            "target": Path(str(artifact)).name,
            "scopes": list(selected),
        }

    def _current(self, token: str) -> _Pending:
        with self._lock:
            pending = self._pending
        if not isinstance(token, str) or pending is None or token != pending.token:
            raise ValueError("stale coverage token")
        return pending


SurfacesService = LocalSurfacesService
