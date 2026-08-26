from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import threading
import time
from typing import Callable, Mapping, Protocol
from urllib.parse import unquote, urlparse

from .config import RunnerConfig
from .http import RowState, StrategyRow
from .results import ResultParseError, extract_html_strategy_name


class BatchTimeout(TimeoutError):
    """Raised when a tester batch stops progressing or exceeds its deadline."""


class BatchRetryExhausted(RuntimeError):
    """Raised when the tester repeatedly returns a strategy to TEST."""


class BatchHtmlCollision(RuntimeError):
    """Raised when two completed strategies reference the same report HTML."""

    def __init__(self, strategy_names: tuple[str, ...]) -> None:
        self.strategy_names = strategy_names
        super().__init__("tester report HTML collision for strategies: " + ", ".join(strategy_names))


class StrategyTableClient(Protocol):
    def list_strategies(self) -> tuple[StrategyRow, ...]: ...


class ControlledStrategyClient(StrategyTableClient, Protocol):
    def launch_strategy(self, name: str) -> object: ...


@dataclass(frozen=True, slots=True)
class StrategyCompletion:
    name: str
    state: RowState | None
    percent_history: tuple[float, ...]
    run_id: str | None
    report_path: Path | None
    completed: bool
    attempts: int


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
    attempts: int = 0
    launched_at: float | None = None
    seen_running: bool = False
    missing_report_polls: int = 0
    missing_report_since: float | None = None
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
            attempts=self.attempts,
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


def _report_matches_strategy(path: Path, name: str) -> bool:
    return _report_strategy_name(path) == name


def _report_strategy_name(path: Path) -> str | None:
    return extract_html_strategy_name(path)


class _ReportSnapshotCollector:
    """Preserve stable report revisions before the closed tester overwrites them."""

    def __init__(
        self, expected_names: tuple[str, ...], report_dir: Path, snapshot_dir: Path
    ) -> None:
        self._expected_names = set(expected_names)
        self._report_dir = report_dir
        self._snapshot_dir = snapshot_dir
        self._observed: dict[Path, tuple[tuple[int, int], int]] = {}
        self._captured: set[tuple[Path, tuple[int, int]]] = set()
        self._inspected: set[tuple[Path, tuple[int, int]]] = set()
        self._snapshots: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seed_existing_reports()

    def _seed_existing_reports(self) -> None:
        """Do not parse the historical report catalog for a newly started batch."""
        try:
            reports = tuple(self._report_dir.glob("*.html"))
        except OSError:
            return
        for report in reports:
            try:
                stat = report.stat()
                if report.is_file():
                    signature = (stat.st_size, stat.st_mtime_ns)
                    self._observed[report] = (signature, 0)
                    self._inspected.add((report, signature))
            except OSError:
                continue

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="mrs3-report-snapshot", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def snapshot_for(self, name: str) -> Path | None:
        with self._lock:
            return self._snapshots.get(name)

    def discard(self, name: str) -> None:
        with self._lock:
            self._snapshots.pop(name, None)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.capture_once()
            self._stop.wait(0.05)

    def capture_once(self) -> None:
        try:
            reports = tuple(self._report_dir.glob("*.html"))
        except OSError:
            return
        for report in reports:
            try:
                stat = report.stat()
                if not report.is_file():
                    continue
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = self._observed.get(report)
            stable_polls = previous[1] + 1 if previous and previous[0] == signature else 1
            self._observed[report] = (signature, stable_polls)
            key = (report, signature)
            if stable_polls < 2 or key in self._inspected:
                continue
            name = _report_strategy_name(report)
            if name is None:
                # Keep polling this revision until the tester finishes writing it.
                continue
            self._inspected.add(key)
            if name not in self._expected_names:
                continue
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
            target = self._snapshot_dir / f"{name}__{signature[1]}_{signature[0]}.html"
            temporary = target.with_name(f".{target.name}.tmp")
            try:
                shutil.copyfile(report, temporary)
                if _report_strategy_name(temporary) != name:
                    continue
                temporary.replace(target)
            except OSError:
                continue
            finally:
                temporary.unlink(missing_ok=True)
            self._captured.add(key)
            with self._lock:
                self._snapshots[name] = target


def _progress_snapshot(
    trackers: dict[str, _Tracker], polls: int, elapsed_seconds: float,
    *, submitted_count: int | None = None, retry_count: int = 0,
    retry_reasons: Mapping[str, int] | None = None,
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
        "submitted_count": len(ordered) if submitted_count is None else submitted_count,
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
        "retry_count": retry_count,
        "retry_reasons": dict(sorted((retry_reasons or {}).items())),
        "attempt_counts": {
            name: tracker.attempts for name, tracker in sorted(trackers.items())
        },
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


def _monitor_controlled_batch_loop(
    client: ControlledStrategyClient,
    expected_names: tuple[str, ...],
    result_path: Path,
    report_dir: Path,
    config: RunnerConfig,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    collision_keys: Mapping[str, str] | None = None,
    verified_report_dir: Path | None = None,
    snapshot_report_dir: Path | None = None,
    initial_attempt_counts: Mapping[str, int] | None = None,
    attempts_callback: Callable[[dict[str, int]], None] | None = None,
    collector: _ReportSnapshotCollector | None = None,
) -> BatchCompletion:
    """Submit a bounded tester window and recover rows returned to TEST."""
    if not expected_names or len(set(expected_names)) != len(expected_names):
        raise ValueError("expected strategy names must be non-empty and unique")

    initial_attempt_counts = initial_attempt_counts or {}
    trackers = {
        name: _Tracker(name, attempts=max(0, int(initial_attempt_counts.get(name, 0))))
        for name in expected_names
    }
    pending = list(expected_names)
    collision_keys = collision_keys or {}
    keys = {name: collision_keys.get(name, name) for name in expected_names}
    started = time.monotonic()
    last_activity = started
    polls = 0
    retry_count = sum(max(0, tracker.attempts - 1) for tracker in trackers.values())
    retry_reasons: dict[str, int] = {}
    launched_any = False
    launched_names: set[str] = set()
    initial_rows = {row.name: row for row in client.list_strategies()}
    baseline_run_ids = {
        name: {
            str(entry.get("runId"))
            for entry in _read_result_entries(result_path)
            if entry.get("strategies") == [name] and entry.get("runId") is not None
        }.union(
            {str(initial_rows[name].run_id)}
            if name in initial_rows and initial_rows[name].run_id is not None
            else set()
        )
        for name in expected_names
    }

    def launch(name: str) -> None:
        nonlocal launched_any, retry_count
        if launched_any:
            time.sleep(config.submission_delay_seconds)
        tracker = trackers[name]
        if tracker.attempts >= config.max_strategy_attempts:
            raise BatchRetryExhausted(
                "tester retry limit exceeded for strategies: " + name
            )
        if tracker.run_id is not None:
            baseline_run_ids[name].add(tracker.run_id)
        if collector is not None:
            collector.discard(name)
        tracker.run_id = None
        tracker.report_path = None
        tracker.report_signature = None
        tracker.stable_polls = 0
        tracker.completed = False
        tracker.attempts += 1
        tracker.launched_at = time.monotonic()
        if tracker.attempts > 1:
            retry_count += 1
        if attempts_callback is not None:
            attempts_callback({item.name: item.attempts for item in trackers.values()})
        client.launch_strategy(name)
        launched_any = True
        launched_names.add(name)

    while True:
        active_count = sum(
            name in launched_names and not tracker.completed
            for name, tracker in trackers.items()
        )
        occupied_keys = {
            keys[name]
            for name, tracker in trackers.items()
            if name in launched_names and not tracker.completed
        }
        while pending and active_count < config.max_parallel_submissions:
            pending_index = next(
                (index for index, name in enumerate(pending) if keys[name] not in occupied_keys),
                None,
            )
            if pending_index is None:
                break
            name = pending.pop(pending_index)
            launch(name)
            active_count += 1
            occupied_keys.add(keys[name])

        rows = {row.name: row for row in client.list_strategies()}
        entries = _read_result_entries(result_path)
        polls += 1
        activity = False
        for name, tracker in trackers.items():
            if tracker.attempts == 0 or tracker.completed:
                continue
            row = rows.get(name)
            if row is None:
                continue
            if row.state != tracker.state:
                tracker.state = row.state
                activity = True
            if row.state is RowState.RUNNING:
                tracker.seen_running = True
                tracker.launched_at = None
            if row.percent is not None and (
                not tracker.percentages or tracker.percentages[-1] != row.percent
            ):
                tracker.percentages.append(row.percent)
                activity = True
            if row.run_id is not None and row.run_id != tracker.run_id:
                tracker.run_id = row.run_id
                activity = True
            if row.state is not RowState.RESULT:
                tracker.missing_report_since = None
                continue
            entry = _matching_entry(entries, name, row.run_id)
            if entry is None:
                continue
            entry_run_id = str(entry.get("runId"))
            report = collector.snapshot_for(name) if collector is not None else None
            if entry_run_id in baseline_run_ids[name] and report is None:
                continue
            if report is None and collector is None:
                report = _report_path(entry, report_dir)
            if (
                report is None
                or not report.is_file()
                or not _report_matches_strategy(report, name)
            ):
                tracker.missing_report_polls += 1
                if tracker.missing_report_since is None:
                    tracker.missing_report_since = time.monotonic()
                continue
            tracker.missing_report_polls = 0
            tracker.missing_report_since = None
            if report is not None and _observe_report(
                tracker, report, config.report_stability_polls
            ):
                activity = True
            if tracker.completed and verified_report_dir is not None and tracker.report_path == report:
                verified_report_dir.mkdir(parents=True, exist_ok=True)
                target = verified_report_dir / f"{tracker.run_id}_{name}.html"
                shutil.copyfile(report, target)
                tracker.report_path = target

        exhausted: list[str] = []
        for name, tracker in trackers.items():
            returned_to_test = tracker.seen_running and tracker.state is RowState.TEST
            missing_report = (
                tracker.state is RowState.RESULT
                and tracker.missing_report_since is not None
                and time.monotonic() - tracker.missing_report_since
                >= config.result_report_grace_seconds
            )
            never_started = (
                tracker.state is RowState.TEST
                and tracker.launched_at is not None
                and time.monotonic() - tracker.launched_at
                >= config.result_report_grace_seconds
            )
            if tracker.completed or not (returned_to_test or missing_report or never_started):
                continue
            if tracker.attempts >= config.max_strategy_attempts:
                exhausted.append(name)
                continue
            tracker.seen_running = False
            if tracker.run_id is not None:
                baseline_run_ids[name].add(tracker.run_id)
            tracker.run_id = None
            tracker.report_path = None
            tracker.report_signature = None
            tracker.stable_polls = 0
            tracker.missing_report_polls = 0
            tracker.missing_report_since = None
            reason = (
                "returned_to_test"
                if returned_to_test
                else "missing_report_after_grace"
                if missing_report
                else "test_after_launch_grace"
            )
            retry_reasons[reason] = retry_reasons.get(reason, 0) + 1
            launch(name)
            activity = True
        if exhausted:
            raise BatchRetryExhausted(
                "tester retry limit exceeded for strategies: " + ", ".join(sorted(exhausted))
            )

        now = time.monotonic()
        if activity:
            last_activity = now
        if progress_callback is not None:
            progress_callback(
                _progress_snapshot(
                    trackers,
                    polls,
                    now - started,
                    submitted_count=sum(item.attempts > 0 for item in trackers.values()),
                    retry_count=retry_count,
                    retry_reasons=retry_reasons,
                )
            )
        if all(tracker.completed for tracker in trackers.values()):
            by_report: dict[Path, list[str]] = {}
            for name, tracker in trackers.items():
                if tracker.report_path is not None:
                    by_report.setdefault(tracker.report_path, []).append(name)
            colliding_names = tuple(
                sorted(
                    name
                    for names in by_report.values()
                    if len(names) > 1
                    for name in names
                )
            )
            if colliding_names:
                raise BatchHtmlCollision(colliding_names)
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


def monitor_controlled_batch(
    client: ControlledStrategyClient,
    expected_names: tuple[str, ...],
    result_path: Path,
    report_dir: Path,
    config: RunnerConfig,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    collision_keys: Mapping[str, str] | None = None,
    verified_report_dir: Path | None = None,
    snapshot_report_dir: Path | None = None,
    initial_attempt_counts: Mapping[str, int] | None = None,
    attempts_callback: Callable[[dict[str, int]], None] | None = None,
) -> BatchCompletion:
    collector = (
        _ReportSnapshotCollector(expected_names, report_dir, snapshot_report_dir)
        if snapshot_report_dir is not None
        else None
    )
    if collector is not None:
        collector.start()
    try:
        return _monitor_controlled_batch_loop(
            client,
            expected_names,
            result_path,
            report_dir,
            config,
            progress_callback=progress_callback,
            collision_keys=collision_keys,
            verified_report_dir=verified_report_dir,
            snapshot_report_dir=snapshot_report_dir,
            initial_attempt_counts=initial_attempt_counts,
            attempts_callback=attempts_callback,
            collector=collector,
        )
    finally:
        if collector is not None:
            collector.close()
