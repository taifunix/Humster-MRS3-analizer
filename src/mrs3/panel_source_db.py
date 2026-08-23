"""Local Source v6 import and merge adapters for the v2 control panel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Callable, Iterable, Mapping

from .source_v6_importer import import_source_v6, preflight_source_v6
from .source_v6_merge import merge_source_v6, preflight_source_v6_merge


@dataclass(frozen=True, slots=True)
class _Pending:
    token: str
    preflight: object


def _safe_relative(value: object) -> str:
    text = str(value).replace("\\", "/")
    if text.startswith("/") or (len(text) >= 3 and text[1] == ":" and text[2] == "/"):
        return PurePosixPath(text).name
    return text


def _name(value: object) -> str:
    return Path(str(value)).name


class LocalSourceDbService:
    """Synchronous, local-only Source v6 import and merge boundary.

    The canonical Source v6 functions own target freshness, source
    immutability, staging, and readback.  This adapter only binds a preflight
    token to an operation and creates redacted documents for a controller.
    """

    def __init__(self, *, workers: int = 1, import_options: Mapping[str, object] | None = None) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("workers must be a positive integer")
        self.workers = workers
        self.import_options = dict(import_options or {})
        self._lock = RLock()
        self._import: _Pending | None = None
        self._merge: _Pending | None = None

    def preflight_import(self, root_path: str | Path, database_path: str | Path) -> dict[str, object]:
        preflight = preflight_source_v6(root_path, database_path)
        snapshots = tuple(getattr(preflight, "snapshots", ()))
        with self._lock:
            self._import = _Pending(str(preflight.token), preflight)
        return {
            "operation": "IMPORT",
            "phase": "PREFLIGHT_READY",
            "token": str(preflight.token),
            "target": _name(getattr(preflight, "database_path", database_path)),
            "total": len(snapshots),
            "snapshots": [
                {
                    "ordinal": int(snapshot.input_ordinal),
                    "relative_path": _safe_relative(snapshot.relative_path),
                    "size": int(snapshot.source_size),
                    "mtime_ns": int(snapshot.source_mtime_ns),
                }
                for snapshot in snapshots
            ],
        }

    def execute_import(
        self,
        token: str,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        fault_injector: Callable[[str], object] | None = None,
    ) -> object:
        pending = self._current(self._import, token, "import")
        preflight = pending.preflight
        kwargs: dict[str, object] = {"preflight": preflight, "workers": self.workers, **self.import_options}
        if cancellation_requested is not None:
            kwargs["cancellation_requested"] = cancellation_requested
        if fault_injector is not None:
            kwargs["fault_injector"] = fault_injector
        return import_source_v6(
            preflight.root_path,
            preflight.database_path,
            **kwargs,
        )

    def preflight_merge(
        self,
        input_paths: Iterable[str | Path],
        target_path: str | Path,
    ) -> dict[str, object]:
        # Keep the canonical iterable handling (including its string/Path
        # normalization) in the existing merge preflight.
        requested = tuple(Path(item).resolve() for item in input_paths)
        if len(requested) != 2 or len(set(requested)) != 2:
            raise ValueError("local merge requires two distinct Source DB inputs")
        preflight = preflight_source_v6_merge(requested, target_path)
        inputs = tuple(getattr(preflight, "input_paths", input_paths))
        with self._lock:
            self._merge = _Pending(str(preflight.token), preflight)
        return {
            "operation": "MERGE",
            "phase": "PREFLIGHT_READY",
            "token": str(preflight.token),
            "inputs": [_name(path) for path in inputs],
            "target": _name(getattr(preflight, "target_path", target_path)),
            "total": len(inputs),
        }

    def execute_merge(
        self,
        token: str,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        fault_injector: Callable[[str], object] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> object:
        pending = self._current(self._merge, token, "merge")
        preflight = pending.preflight
        kwargs: dict[str, object] = {"preflight": preflight, "workers": self.workers}
        if cancellation_requested is not None:
            kwargs["cancellation_requested"] = cancellation_requested
        if fault_injector is not None:
            kwargs["fault_injector"] = fault_injector
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback
        return merge_source_v6(
            preflight.input_paths,
            preflight.target_path,
            **kwargs,
        )

    def target_for(self, token: str, *, merge: bool) -> Path:
        pending = self._current(self._merge if merge else self._import, token, "merge" if merge else "import")
        return Path(getattr(pending.preflight, "target_path" if merge else "database_path")).resolve()

    @staticmethod
    def _current(pending: _Pending | None, token: str, operation: str) -> _Pending:
        if not isinstance(token, str) or pending is None or token != pending.token:
            raise ValueError(f"latest Source DB {operation} preflight token is required")
        return pending
