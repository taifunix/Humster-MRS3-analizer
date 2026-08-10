from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Callable, Protocol
from urllib.parse import unquote, urlparse

from .config import RunnerConfig
from .http import RowState, StrategyRow


class BatchTimeout(TimeoutError):
    """Raised when a tester batch stops progressing or exceeds its deadline."""


class StrategyTableClient(Protocol):
    def list_strategies(self) -> tuple[StrategyRow, ...]: ...


@dataclass(frozen=True, slots=True)
class StrategyCompletion:
    name: str
    state: RowState | None
    percent_history: tuple[float, ...]
    run_id: str | None
    report_path: Path | None
    completed: bool


@dataclass(frozen=True, slots=True)
class BatchCompletion:
    strategies: dict[str, StrategyCompletion]
    polls: int
    elapsed_seconds: float


@dataclass(slots=True)
class _Tracker:
    name: str
    state: RowState | None = None
    percentages: list[float] | None = None
    run_id: str | None = None
    report_path: Path | None = None
    report_signature: tuple[int, int] | None = None
    stable_polls: int = 0
    completed: bool = False

    def __post_init__(self) -> None:
        if self.percentages is None:
            self.percentages = []

    def frozen(self) -> StrategyCompletion:
        return StrategyCompletion(
            name=self.name,
            state=self.state,
            percent_history=tuple(self.percentages or ()),
            run_id=self.run_id,
            report_path=self.report_path,
            completed=self.completed,
        )


def _read_result_entries(path: Path) -> list[dict[str, object]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(document, list):
        return []
    return [entry for entry in document if isinstance(entry, dict)]


def _matching_entry(
    entries: list[dict[str, object]], name: str, run_id: str | None
) -> dict[str, object] | None:
    matches: list[dict[str, object]] = []
    for entry in entries:
        strategies = entry.get("strategies")
        if not isinstance(strategies, list) or strategies != [name]:
            continue
        entry_run_id = entry.get("runId")
        if run_id is not None and str(entry_run_id) != run_id:
            continue
        matches.append(entry)
    return matches[0] if len(matches) == 1 else None


def _report_path(entry: dict[str, object], report_dir: Path) -> Path | None:
    chart_url = entry.get("chartUrl")
    if not isinstance(chart_url, str) or not chart_url:
        return None
    basename = Path(unquote(urlparse(chart_url).path)).name
    if not basename or not basename.casefold().endswith(".html"):
        return None
    resolved_dir = report_dir.resolve()
    candidate = (resolved_dir / basename).resolve()
    if candidate.parent != resolved_dir:
        return None
    return candidate


def _observe_report(tracker: _Tracker, path: Path, required_polls: int) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file():
        return False
    signature = (stat.st_size, stat.st_mtime_ns)
    advanced = False
    if tracker.report_path != path or tracker.report_signature != signature:
        tracker.report_path = path
        tracker.report_signature = signature
        tracker.stable_polls = 1
        advanced = True
    else:
        tracker.stable_polls += 1
        advanced = tracker.stable_polls <= required_polls
    if tracker.stable_polls >= required_polls:
        tracker.completed = True
    return advanced


def _progress_snapshot(
    trackers: dict[str, _Tracker], polls: int, elapsed_seconds: float
) -> dict[str, object]:
    ordered = [trackers[name] for name in sorted(trackers)]
    active = [
        {
            "name": tracker.name,
            "state": tracker.state.value if tracker.state is not None else "WAITING",
            "percent": tracker.percentages[-1] if tracker.percentages else None,
        }
        for tracker in ordered
        if not tracker.completed
    ]
    return {
        "expected_count": len(ordered),
        "submitted_count": len(ordered),
        "completed_count": sum(tracker.completed for tracker in ordered),
        "waiting_count": sum(
            tracker.state in {None, RowState.TEST} for tracker in ordered
        ),
        "running_count": sum(
            tracker.state is RowState.RUNNING for tracker in ordered
        ),
        "result_count": sum(tracker.state is RowState.RESULT for tracker in ordered),
        "polls": polls,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "active_total": len(active),
        "active": active[:50],
    }


def monitor_batch(
    client: StrategyTableClient,
    expected_names: tuple[str, ...],
    result_path: Path,
    report_dir: Path,
    config: RunnerConfig,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> BatchCompletion:
    if not expected_names or len(set(expected_names)) != len(expected_names):
        raise ValueError("expected strategy names must be non-empty and unique")
    trackers = {name: _Tracker(name) for name in expected_names}
    started = time.monotonic()
    last_activity = started
    polls = 0
    while True:
        rows = {row.name: row for row in client.list_strategies()}
        entries = _read_result_entries(result_path)
        polls += 1
        activity = False
        for name, tracker in trackers.items():
            row = rows.get(name)
            if row is None:
                continue
            if row.state != tracker.state:
                tracker.state = row.state
                activity = True
            if row.percent is not None and (
                not tracker.percentages or tracker.percentages[-1] != row.percent
            ):
                tracker.percentages.append(row.percent)
                activity = True
            if row.run_id is not None and row.run_id != tracker.run_id:
                tracker.run_id = row.run_id
                activity = True
            if row.state is not RowState.RESULT:
                continue
            entry = _matching_entry(entries, name, row.run_id)
            if entry is None:
                continue
            report = _report_path(entry, report_dir)
            if report is None:
                continue
            if _observe_report(tracker, report, config.report_stability_polls):
                activity = True

        now = time.monotonic()
        if activity:
            last_activity = now
        if progress_callback is not None:
            progress_callback(_progress_snapshot(trackers, polls, now - started))
        if all(tracker.completed for tracker in trackers.values()):
            return BatchCompletion(
                strategies={name: tracker.frozen() for name, tracker in trackers.items()},
                polls=polls,
                elapsed_seconds=now - started,
            )
        if now - started >= config.batch_timeout_seconds:
            incomplete = ", ".join(
                name for name, tracker in trackers.items() if not tracker.completed
            )
            raise BatchTimeout(f"tester batch timed out; incomplete strategies: {incomplete}")
        if now - last_activity >= config.stall_timeout_seconds:
            incomplete = ", ".join(
                name for name, tracker in trackers.items() if not tracker.completed
            )
            raise BatchTimeout(f"tester batch stalled; incomplete strategies: {incomplete}")
        time.sleep(config.poll_interval_seconds)
