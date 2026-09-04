"""Independent, bounded strategy testing for the panel Fast TEST action."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
import html
import json
import os
from pathlib import Path
import re
import shutil
from threading import Event, RLock, Thread
import time
from typing import Callable, Mapping
from uuid import uuid4

from .panel_strategy_batch import ValidatedStrategyManifest, validate_strategy_manifest
from .panel_testing import mrs3_tester_config_template
from .performance import PerformanceParseError, _raw_markup, _series
from .performance_v2_html import CURRENT_ACTION_HEADERS
from .runner.config import RunnerConfig
from .runner.files import validate_runner_paths
from .runner.http import TesterHttpClient
from .runner.inbox import InboxCaptureError, capture_run_snapshot_inbox
from .runner.monitor import BatchCompletion, BatchRetryExhausted, monitor_controlled_batch
from .runner.process import start_bot as _start_bot, stop_bot as _stop_bot
from .runner.results import extract_html_strategy_settings
from .runner.workflow import _wait_for_exact_batch


class FastStrategyTestError(ValueError):
    """Raised when Fast TEST input or its exact runtime paths are invalid."""


class _FastCancelled(RuntimeError):
    pass


@dataclass(slots=True)
class _Job:
    job_id: str
    manifest_path: Path
    manifest: ValidatedStrategyManifest
    expected_names: tuple[str, ...]
    run_names: tuple[str, ...]
    start_date: str
    end_date: str
    report_dir: Path
    strategy_dir: Path
    runtime_config: RunnerConfig | None = None
    attempt_limits: dict[str, int] = field(default_factory=dict)
    cancel: Event = field(default_factory=Event)
    state: str = "RUNNING"
    phase: str = "RUNNING"
    progress: dict[str, object] = field(default_factory=dict)
    attempt_counts: dict[str, int] = field(default_factory=dict)
    verified_reports: dict[str, str] = field(default_factory=dict)
    failed_names: set[str] = field(default_factory=set)
    error: dict[str, str] | None = None
    thread: Thread | None = None
    preserve_reports: bool = False
    inbox_path: Path | None = None
    single_mode: bool = False


def _client(config: RunnerConfig) -> TesterHttpClient:
    return TesterHttpClient(config.base_url, timeout=config.request_timeout_seconds)


_CURRENT_METRIC_HEADERS = ("Metric", "Value")


def _has_current_performance_v2_layout(source: str) -> bool:
    """Check the native report shape without importing its full contents."""
    try:
        _, tables = _raw_markup(source)
    except (TypeError, ValueError):
        return False

    action_tables = [
        headers
        for headers, _rows in tables
        if set(CURRENT_ACTION_HEADERS).issubset(headers)
        and len(headers) == len(set(headers))
    ]
    metric_tables = [
        headers
        for headers, _rows in tables
        if len(headers) >= len(_CURRENT_METRIC_HEADERS)
        and headers[: len(_CURRENT_METRIC_HEADERS)] == _CURRENT_METRIC_HEADERS
    ]
    if len(action_tables) != 1 or not metric_tables:
        return False
    try:
        wallet = _series(source, "walletSeries")
        equity = _series(source, "equitySeries")
    except PerformanceParseError:
        return False
    return tuple(point[0] for point in wallet) == tuple(point[0] for point in equity)


def _dates(start_date: object, end_date: object) -> tuple[str, str]:
    if not isinstance(start_date, str) or not isinstance(end_date, str):
        raise FastStrategyTestError("start_date and end_date must be ISO dates")
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        raise FastStrategyTestError("start_date and end_date must be ISO dates") from None
    if start.isoformat() != start_date or end.isoformat() != end_date or start > end:
        raise FastStrategyTestError("start_date must be on or before end_date")
    return start_date, end_date


def _clear_directory(path: Path, *, expected: Path) -> None:
    if path.is_symlink():
        raise FastStrategyTestError("refusing to clear a symlink runtime path")
    resolved = path.resolve()
    if resolved != expected.resolve():
        raise FastStrategyTestError(f"refusing to clear unexpected path: {resolved}")
    if resolved.exists():
        if not resolved.is_dir() or resolved.is_symlink():
            raise FastStrategyTestError(f"runtime path is not a directory: {resolved}")
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


def _install_names(source: Path, target: Path, names: tuple[str, ...]) -> None:
    if source.is_symlink() or target.is_symlink():
        raise FastStrategyTestError("strategy source and target must not be symlinks")
    source = source.resolve()
    target = target.resolve()
    if source == target:
        raise FastStrategyTestError("strategy source and target must differ")
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        filename = name if Path(name).suffix.casefold() == ".json" else f"{name}.json"
        if not name or Path(filename).name != filename or Path(filename).suffix.casefold() != ".json":
            raise FastStrategyTestError("strategy filename is unsafe")
        source_file = source / filename
        if not source_file.is_file() or source_file.is_symlink():
            raise FastStrategyTestError(f"strategy file is missing: {filename}")
        shutil.copy2(source_file, target / filename)


def _write_fast_tester_config(
    config: RunnerConfig,
    start: str,
    end: str,
    *,
    single_mode: bool = False,
    template_path: Path | None = None,
) -> None:
    path = Path(config.tester_config).resolve()
    source = Path(template_path).resolve() if template_path is not None else mrs3_tester_config_template()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FastStrategyTestError("tester config is invalid") from error
    if not isinstance(document, dict):
        raise FastStrategyTestError("tester config must be an object")
    report_settings = {
        "enable_html_report": True,
        "include_chart_ohlc": False,
        "include_chart_balance": True,
        "include_chart_position": False,
        "include_strategy_settings": True,
        "include_trades_table": True,
        "include_summary_table": True,
        "include_monthly_returns_heatmap": False,
        "include_position_stats": False,
        "enable_timing_logs": False,
    }
    document.update({"StartDate": start, "EndDate": end, "use_runs": False, "single_mode": single_mode, **report_settings})
    report = document.get("report")
    if not isinstance(report, dict):
        raise FastStrategyTestError("tester config report must be an object")
    report.update(report_settings)
    document["max_parallel_runs"] = config.max_parallel_submissions
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise FastStrategyTestError("tester config cannot be written") from error


def _safe_name(value: object) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or Path(value).name != value:
        raise FastStrategyTestError("strategy name is invalid")
    return value


def _safe_error_message(error: BaseException) -> str:
    message = str(error).strip() or type(error).__name__
    message = re.sub(r"(?i)(?:[A-Za-z]:[\\/]|/)[^\s,;]+", "<path>", message)
    return message[:512]


def _report_period_matches(source: str, start: str, end: str) -> bool:
    """Require an explicit report-period label, never a trade-row timestamp."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", source))
    text = re.sub(r"\s+", " ", text)
    token = r"(?:\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})"
    explicit = rf"\b(?:report\s+range|test\s+period|date\s+range)\s*:?\s*({token})\s*(?:-|вЂ“|вЂ”|to|through)\s*({token})\b"
    explicit = rf"\b(?:report\s+range|test\s+period|date\s+range)\s*:?\s*({token})\s*(?:-|\u2013|\u2014|to|through)\s*({token})\b"
    matches = re.findall(explicit, text, flags=re.IGNORECASE)
    if len(matches) != 1:
        return False
    def iso(value: str) -> str:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
        return date(int(value[6:]), int(value[3:5]), int(value[:2])).isoformat()
    try:
        actual = (iso(matches[0][0]), iso(matches[0][1]))
    except ValueError:
        return False
    if actual != (start, end):
        return False
    start_day = date.fromisoformat(start).strftime("%d.%m.%Y")
    end_day = date.fromisoformat(end).strftime("%d.%m.%Y")
    start_forms = rf"(?:{re.escape(start)}|{re.escape(start_day)})"
    end_forms = rf"(?:{re.escape(end)}|{re.escape(end_day)})"
    pattern = rf"\b(?:report\s+range|test\s+period|date\s+range)\s*:?\s*{start_forms}\s*(?:-|–|—|to|through)\s*{end_forms}\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _report_matches_run(report: Path, settings: Mapping[str, object], expected: Mapping[str, object], start: str, end: str) -> bool:
    try:
        source = report.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    start_day = date.fromisoformat(start).strftime("%d.%m.%Y")
    end_day = date.fromisoformat(end).strftime("%d.%m.%Y")
    start_forms = rf"(?:{re.escape(start)}|{re.escape(start_day)})"
    end_forms = rf"(?:{re.escape(end)}|{re.escape(end_day)})"
    period = rf"(?:period|date\s*range|from|start(?:\s+date)?)[^\n]{{0,100}}{start_forms}[^\n]{{0,100}}(?:to|through|[-–])[^\n]{{0,100}}{end_forms}"
    separate = rf"(?:start|from)[^\n]{{0,100}}{start_forms}[^\n]{{0,200}}(?:end|to|through)[^\n]{{0,100}}{end_forms}"
    if not _report_period_matches(source, start, end):
        return False
    expected_basic = expected.get("basic")
    actual_basic = settings.get("basic")
    if not isinstance(expected_basic, Mapping) or not isinstance(actual_basic, Mapping):
        return False
    for key in ("symbol", "time_frame"):
        if expected_basic.get(key) != actual_basic.get(key):
            return False

    def matching_values(left: object, right: object) -> bool:
        if isinstance(left, Mapping):
            return isinstance(right, Mapping) and all(key in right and matching_values(value, right[key]) for key, value in left.items())
        if isinstance(left, list):
            return isinstance(right, list) and len(left) == len(right) and all(matching_values(a, b) for a, b in zip(left, right, strict=True))
        if isinstance(left, bool):
            return left is right
        return str(left) == str(right)

    for section in ("mrs2", "mrs3"):
        expected_section = expected.get(section)
        actual_section = settings.get(section)
        if isinstance(expected_section, Mapping):
            if not isinstance(actual_section, Mapping) or not matching_values(expected_section, actual_section):
                return False
    return True


class LocalFastStrategyTestService:
    """Run READY strategies in direct, bounded tester chunks."""

    def __init__(
        self,
        config: RunnerConfig,
        *,
        start_bot: Callable[[RunnerConfig], object] = _start_bot,
        stop_bot: Callable[[RunnerConfig], object] = _stop_bot,
        client_factory: Callable[[RunnerConfig], object] = _client,
        wait_for_exact_batch: Callable[..., object] = _wait_for_exact_batch,
        monitor: Callable[..., BatchCompletion] = monitor_controlled_batch,
        on_update: Callable[[dict[str, object]], None] | None = None,
        single_mode: bool = False,
        tester_config_template: Path | None = None,
    ) -> None:
        self.config = config
        self._start_bot = start_bot
        self._stop_bot = stop_bot
        self._client_factory = client_factory
        self._wait_for_exact_batch = wait_for_exact_batch
        self._monitor = monitor
        self._on_update = on_update
        self.single_mode = single_mode
        self.tester_config_template = Path(tester_config_template) if tester_config_template is not None else mrs3_tester_config_template()
        self._lock = RLock()
        self._jobs: dict[str, _Job] = {}

    @staticmethod
    def _expected(manifest: ValidatedStrategyManifest) -> tuple[str, ...]:
        hashes = manifest.provenance.get("strategy_json_sha256")
        if not isinstance(hashes, Mapping) or not hashes:
            raise FastStrategyTestError("strategy JSON hash map is missing")
        return tuple(sorted(_safe_name(Path(str(name)).stem) for name in hashes))

    @staticmethod
    def _require_diagnostics(manifest: ValidatedStrategyManifest, expected_names: tuple[str, ...] = ()) -> Mapping[str, object]:
        diagnostics = manifest.provenance.get("candidate_diagnostics")
        mapping = manifest.provenance.get("candidate_identity_to_strategy_names")
        if not isinstance(diagnostics, Mapping) or not isinstance(mapping, Mapping):
            raise FastStrategyTestError("generation manifest lacks plateau diagnostics")
        mapped_names: set[str] = set()
        for candidate, names in mapping.items():
            if str(candidate) not in diagnostics or not isinstance(names, list) or not names or any(not isinstance(name, str) or not name for name in names):
                raise FastStrategyTestError("generation manifest has incomplete plateau diagnostics")
            mapped_names.update(names)
            diagnostic = diagnostics[str(candidate)]
            if not isinstance(diagnostic, Mapping) or type(diagnostic.get("order_count")) is not int or diagnostic["order_count"] < 1:
                raise FastStrategyTestError("generation manifest has malformed plateau diagnostics")
            orders = diagnostic.get("orders")
            if not isinstance(orders, list) or len(orders) != diagnostic["order_count"]:
                raise FastStrategyTestError("generation manifest has malformed plateau diagnostics")
            required = ("order_id", "plateau_id", "plateau_point_count", "base_point_trades", "plateau_total_trades")
            if any(not isinstance(order, Mapping) or any(key not in order for key in required) or type(order["order_id"]) is not int or order["order_id"] < 1 or any(type(order[key]) is not int or order[key] < 0 for key in required[2:]) or not isinstance(order["plateau_id"], str) or not order["plateau_id"] for order in orders):
                raise FastStrategyTestError("generation manifest has malformed plateau diagnostics")
        if expected_names and mapped_names != set(expected_names):
            raise FastStrategyTestError("generation manifest has incomplete candidate diagnostics")
        return diagnostics

    def _snapshot(self, job: _Job) -> dict[str, object]:
        with self._lock:
            return {
                "job_id": job.job_id,
                "state": job.state,
                "phase": job.phase,
                "mode": "SINGLE_MODE" if job.single_mode else "FAST",
                "inbox_ready": job.inbox_path is not None and job.state == "COMMITTED",
                "progress": dict(job.progress),
                "evidence": {
                    "failed_names": sorted(job.failed_names),
                    "verified_reports": dict(sorted(job.verified_reports.items())),
                },
                "error": dict(job.error) if job.error else None,
                **({"inbox_path": str(job.inbox_path)} if job.inbox_path is not None else {}),
            }

    def _emit(self, job: _Job) -> None:
        if self._on_update is not None:
            self._on_update(self._snapshot(job))

    def _set_progress(self, job: _Job, **values: object) -> None:
        with self._lock:
            job.progress.update(values)
        self._emit(job)

    def _set_phase(self, job: _Job, phase: str, **values: object) -> None:
        """Publish a named native handoff phase with its current counters."""
        with self._lock:
            job.phase = phase
            job.progress.update(values)
        self._emit(job)

    def _write_manifest(self, job: _Job) -> None:
        document = {
            "format_version": 1,
            "mode": "SINGLE_MODE" if job.single_mode else "FAST",
            "job_id": job.job_id,
            "analysis_run_id": job.manifest.analysis_run_id,
            "generation_manifest_path": str(job.manifest_path),
            "start_date": job.start_date,
            "end_date": job.end_date,
            "expected_names": list(job.expected_names),
            "run_names": list(job.run_names),
            "strategy_json_sha256": job.manifest.provenance["strategy_json_sha256"],
            "candidate_identity_to_strategy_names": job.manifest.provenance["candidate_identity_to_strategy_names"],
            "candidate_diagnostics": job.manifest.provenance["candidate_diagnostics"],
            "strategy_batch_size": (job.runtime_config or self.config).strategy_batch_size,
            "max_parallel_submissions": (job.runtime_config or self.config).max_parallel_submissions,
            "max_strategy_attempts": (job.runtime_config or self.config).max_strategy_attempts,
            "attempt_counts": dict(sorted(job.attempt_counts.items())),
            "verified_reports": dict(sorted(job.verified_reports.items())),
            "failed_names": sorted(job.failed_names),
            "phase": job.phase,
            "inbox_ready": job.inbox_path is not None and job.state == "COMMITTED",
        }
        # Native handoff uses the shared tester manifest name so the runtime
        # does not grow a second mode-specific persistence contract.
        target = job.report_dir / (
            "tester_manifest.json" if job.single_mode else "fast_test_manifest.json"
        )
        temporary = target.with_name(target.name + ".tmp")
        try:
            temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, target)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise FastStrategyTestError("Fast TEST manifest cannot be written") from error

    def _publish_incomplete(self, job: _Job, names: tuple[str, ...]) -> None:
        _clear_directory(job.strategy_dir, expected=self.config.bot_root / "settings_strategy")
        _install_names(job.manifest.strategy_source, job.strategy_dir, names)

    def _run(self, job: _Job) -> None:
        client: object | None = None
        runtime_config = job.runtime_config or self.config
        snapshot_dir: Path | None = None
        try:
            if job.single_mode:
                self._run_native(job)
                return
            _write_fast_tester_config(
                runtime_config,
                job.start_date,
                job.end_date,
                template_path=self.tester_config_template,
            )
            if not job.preserve_reports:
                _clear_directory(job.report_dir, expected=self.config.bot_root / "tester" / "report" / "my_test")
            job.report_dir.mkdir(parents=True, exist_ok=True)
            snapshot_dir = job.report_dir / f".fast-snapshots-{job.job_id}"
            _clear_directory(snapshot_dir, expected=snapshot_dir)
            self._write_manifest(job)
            batches = [
                job.run_names[index : index + runtime_config.strategy_batch_size]
                for index in range(0, len(job.run_names), runtime_config.strategy_batch_size)
            ]
            self._set_progress(job, current=len(job.verified_reports), total=len(job.expected_names), batch_current=0, batch_total=len(batches), active=0, retries=0, failed=len(job.failed_names))
            for batch_number, names in enumerate(batches, start=1):
                if job.cancel.is_set():
                    break
                if job.single_mode:
                    self._set_phase(job, "BATCH_PREPARE", batch_number=batch_number, batch_total=len(batches))
                self._stop_bot(runtime_config)
                _clear_directory(job.strategy_dir, expected=self.config.bot_root / "settings_strategy")
                _install_names(job.manifest.strategy_source, job.strategy_dir, names)
                runtime_config.wizard_result.unlink(missing_ok=True)
                runtime_config.wizard_progress.unlink(missing_ok=True)
                self._set_progress(job, batch_current=batch_number, active=0)
                if job.single_mode:
                    self._set_phase(job, "BOT_START", batch_number=batch_number, batch_total=len(batches))
                self._start_bot(runtime_config)
                client = self._client_factory(runtime_config)
                if job.single_mode:
                    self._set_phase(job, "BOT_RUN", batch_number=batch_number, batch_total=len(batches))
                self._wait_for_exact_batch(client, names, runtime_config, cancel_check=job.cancel.is_set)

                def progress(snapshot: dict[str, object]) -> None:
                    if job.cancel.is_set():
                        raise _FastCancelled()
                    retry_count = int(snapshot.get("retry_count", 0))
                    self._set_phase(
                        job,
                        "RETRY_MISSING" if job.single_mode and retry_count else "REPORT_COLLECTION",
                        current=len(job.verified_reports) + int(snapshot.get("completed_count", 0)),
                        active=int(snapshot.get("active_total", 0)),
                        retries=sum(max(0, value - 1) for value in job.attempt_counts.values()) + retry_count,
                    )

                groups: dict[int, tuple[str, ...]] = {}
                for name in names:
                    limit = int(job.attempt_limits.get(name, runtime_config.max_strategy_attempts))
                    groups[limit] = (*groups.get(limit, ()), name)
                for attempt_limit, group_names in groups.items():
                    group_config = replace(runtime_config, max_strategy_attempts=attempt_limit)
                    completion = self._monitor(
                        client,
                        group_names,
                        group_config.wizard_result,
                        group_config.report_dir,
                        group_config,
                        progress_callback=progress,
                        initial_attempt_counts={name: job.attempt_counts.get(name, 0) for name in group_names},
                        attempts_callback=lambda counts: job.attempt_counts.update(counts),
                        snapshot_report_dir=snapshot_dir,
                        remove_source_reports=True,
                        allow_partial=not job.single_mode,
                    )
                    for name, result in completion.strategies.items():
                        job.attempt_counts[name] = max(job.attempt_counts.get(name, 0), result.attempts)
                        if result.completed and result.report_path is not None:
                            target = job.report_dir / result.report_path.name
                            if result.report_path.resolve() != target.resolve():
                                shutil.copyfile(result.report_path, target)
                            job.verified_reports[name] = target.name
                            job.failed_names.discard(name)
                    job.failed_names.update(completion.failed_names)
                self._stop_bot(runtime_config)
                if client is not None and hasattr(client, "close"):
                    client.close()
                client = None
                self._write_manifest(job)
                self._set_progress(
                    job,
                    current=len(job.verified_reports),
                    active=0,
                    retries=sum(max(0, value - 1) for value in job.attempt_counts.values()),
                    failed=len(job.failed_names),
                )
                if job.single_mode:
                    self._set_phase(
                        job,
                        "REPORT_COLLECTION",
                        batch_number=batch_number,
                        batch_total=len(batches),
                        current=len(job.verified_reports),
                        failed=len(job.failed_names),
                    )

            job.failed_names.difference_update(job.verified_reports)
            incomplete = tuple(name for name in job.expected_names if name not in job.verified_reports)
            if job.cancel.is_set():
                final_phase = "CANCELLED"
                job.failed_names.update(incomplete)
            elif incomplete:
                final_phase = "FAILED" if job.single_mode else "PARTIAL"
                job.failed_names.update(incomplete)
            else:
                final_phase = "COMMITTED"
            self._publish_incomplete(job, tuple(sorted(job.failed_names if final_phase == "PARTIAL" else incomplete)))
            job.phase = final_phase
            if job.single_mode and final_phase == "FAILED":
                self._set_phase(job, "FAILED", current=len(job.verified_reports), failed=len(job.failed_names))
            if not (final_phase == "COMMITTED" and job.single_mode):
                self._write_manifest(job)
            if final_phase == "COMMITTED" and job.single_mode:
                # A complete native run owns the handoff.  The inbox contains
                # metadata only; source JSON/HTML stay at their exact owners.
                self._set_phase(job, "INBOX_CREATION", current=len(job.verified_reports), failed=0)
                self._write_manifest(job)
                self.capture_inbox(job.job_id)
                job.state = "COMMITTED"
                self._set_phase(job, "INBOX_READY", inbox_ready=True, current=len(job.verified_reports), failed=0)
                self._write_manifest(job)
            job.state = (
                "CANCELLED"
                if final_phase == "CANCELLED"
                else "FAILED"
                if final_phase == "FAILED"
                else "COMMITTED"
            )
            if final_phase == "COMMITTED" and job.single_mode:
                job.phase = "COMMITTED"
                self._write_manifest(job)
            elif job.single_mode:
                self._write_manifest(job)
            self._set_progress(job, current=len(job.verified_reports), active=0, failed=len(job.failed_names))
        except BaseException as error:
            try:
                self._stop_bot(runtime_config)
            except BaseException:
                pass
            if job.cancel.is_set():
                incomplete = tuple(name for name in job.expected_names if name not in job.verified_reports)
                try:
                    job.failed_names.update(incomplete)
                    self._publish_incomplete(job, tuple(sorted(job.failed_names)))
                    job.phase = "CANCELLED"
                    job.error = None
                    self._write_manifest(job)
                    job.state = "CANCELLED"
                    self._set_progress(job, current=len(job.verified_reports), active=0, failed=len(job.failed_names))
                except BaseException as cancel_error:
                    with self._lock:
                        job.state = "FAILED"
                        job.phase = "FAILED"
                        job.error = {"code": "SINGLE_MODE_TEST_FAILED" if job.single_mode else "FAST_TEST_FAILED", "message": _safe_error_message(cancel_error)}
                    self._emit(job)
            else:
                with self._lock:
                    job.state = "FAILED"
                    job.phase = "FAILED"
                    if job.single_mode:
                        job.failed_names.update(name for name in job.expected_names if name not in job.verified_reports)
                    job.progress.update(
                        current=len(job.verified_reports),
                        active=0,
                        failed=len(job.failed_names),
                    )
                    code = (
                        "SINGLE_MODE_RETRIES_EXHAUSTED"
                        if job.single_mode and isinstance(error, BatchRetryExhausted)
                        else "SINGLE_MODE_TEST_FAILED"
                        if job.single_mode
                        else "FAST_TEST_FAILED"
                    )
                    job.error = {"code": code, "message": _safe_error_message(error)}
                try:
                    self._write_manifest(job)
                except BaseException:
                    pass
                self._emit(job)
        finally:
            if snapshot_dir is not None and snapshot_dir.exists() and not snapshot_dir.is_symlink():
                shutil.rmtree(snapshot_dir)
            if client is not None and hasattr(client, "close"):
                try:
                    client.close()
                except BaseException:
                    pass

    def _run_native(self, job: _Job) -> None:
        raise FastStrategyTestError("native SINGLE_MODE runner is unavailable")

    def start(
        self,
        manifest_path: Path,
        *,
        analysis_run_id: str,
        start_date: str,
        end_date: str,
        job_id: str | None = None,
    ) -> dict[str, object]:
        manifest = validate_strategy_manifest(Path(manifest_path))
        if manifest.analysis_run_id != analysis_run_id:
            raise FastStrategyTestError("strategy batch does not match analysis run")
        dates = _dates(start_date, end_date)
        strategy_dir, report_dir, _, _ = validate_runner_paths(self.config)
        names = self._expected(manifest)
        self._require_diagnostics(manifest, names)
        identifier = _safe_name(job_id or str(uuid4()))
        with self._lock:
            if any(job.state not in {"COMMITTED", "CANCELLED", "FAILED"} for job in self._jobs.values()):
                raise FastStrategyTestError("Fast TEST is already running")
            if identifier in self._jobs:
                raise FastStrategyTestError("Fast TEST job id is already used")
            job = _Job(identifier, Path(manifest_path).resolve(), manifest, names, names, *dates, report_dir, strategy_dir, single_mode=self.single_mode)
            self._jobs[identifier] = job
            job.progress = {"current": 0, "total": len(names), "batch_current": 0, "batch_total": 0, "active": 0, "retries": 0, "failed": 0}
            job.thread = Thread(
                target=self._run,
                args=(job,),
                daemon=True,
                name="mrs3-panel-single-mode" if self.single_mode else "mrs3-panel-fast-strategy-test",
            )
            job.thread.start()
            return self._snapshot(job)

    def _load_persisted_job(self, job_id: str) -> _Job | None:
        paths = [self.config.report_dir / "tester_manifest.json"] if self.single_mode else []
        if self.single_mode:
            # Read the pre-release name as a harmless recovery fallback.
            paths.append(self.config.report_dir / "single_mode_manifest.json")
        paths.append(self.config.report_dir / "fast_test_manifest.json")
        document = None
        for path in paths:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            break
        if document is None:
            return None
        if not isinstance(document, Mapping) or document.get("job_id") != job_id:
            return None
        phase = document.get("phase")
        if phase not in {"PARTIAL", "FAILED", "CANCELLED"}:
            return None
        generation_path = document.get("generation_manifest_path")
        if not isinstance(generation_path, str):
            return None
        try:
            manifest = validate_strategy_manifest(Path(generation_path))
            expected = self._expected(manifest)
            self._require_diagnostics(manifest, expected)
            start, end = _dates(document.get("start_date"), document.get("end_date"))
            strategy_dir, report_dir, _, _ = validate_runner_paths(self.config)
            if report_dir != self.config.report_dir.resolve() or tuple(document.get("expected_names", ())) != expected:
                return None
            attempts = document.get("attempt_counts", {})
            verified = document.get("verified_reports", {})
            failed = document.get("failed_names", [])
            if not isinstance(attempts, Mapping) or not isinstance(verified, Mapping) or not isinstance(failed, list):
                return None
            attempt_counts = {name: int(attempts.get(name, 0)) for name in expected}
            verified_reports = {}
            for name, value in verified.items():
                if name not in expected:
                    continue
                filename = _safe_name(value)
                if Path(filename).suffix.casefold() != ".html":
                    continue
                report = report_dir / filename
                try:
                    expected_settings = json.loads((manifest.strategy_source / f"{name}.json").read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                settings = extract_html_strategy_settings(report) if report.is_file() and not report.is_symlink() else None
                if isinstance(expected_settings, Mapping) and isinstance(settings, Mapping) and settings.get("name") == name and _report_matches_run(report, settings, expected_settings, start, end):
                    verified_reports[name] = filename
            failed_names = {_safe_name(name) for name in failed}
        except (FastStrategyTestError, ValueError, OSError, TypeError):
            return None
        single_mode = document.get("mode") == "SINGLE_MODE"
        job = _Job(job_id, Path(generation_path).resolve(), manifest, expected, tuple(name for name in expected if name not in verified_reports), start, end, report_dir, strategy_dir, attempt_counts=attempt_counts, verified_reports=verified_reports, failed_names=failed_names, preserve_reports=True, state="FAILED" if phase == "FAILED" and single_mode else "COMMITTED", phase=str(phase), single_mode=single_mode)
        return job

    def _load_persisted_job_for_inbox(self, job_id: str) -> _Job | None:
        paths = [self.config.report_dir / "tester_manifest.json"] if self.single_mode else []
        if self.single_mode:
            paths.append(self.config.report_dir / "single_mode_manifest.json")
        paths.append(self.config.report_dir / "fast_test_manifest.json")
        document = None
        for path in paths:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            break
        if document is None:
            return None
        if not isinstance(document, Mapping) or document.get("job_id") != job_id:
            return None
        if document.get("phase") not in {"RUNNING", "COMMITTED"}:
            return None
        generation_path = document.get("generation_manifest_path")
        expected_document = document.get("expected_names")
        verified = document.get("verified_reports")
        failed = document.get("failed_names", [])
        if not isinstance(generation_path, str) or not isinstance(expected_document, list) or not isinstance(verified, Mapping) or not isinstance(failed, list) or failed:
            return None
        try:
            manifest = validate_strategy_manifest(Path(generation_path))
            expected = self._expected(manifest)
            self._require_diagnostics(manifest, expected)
            start, end = _dates(document.get("start_date"), document.get("end_date"))
            strategy_dir, report_dir, _, _ = validate_runner_paths(self.config)
        except (FastStrategyTestError, ValueError, OSError, TypeError):
            return None
        if tuple(expected_document) != expected or report_dir != self.config.report_dir.resolve() or set(verified) != set(expected):
            return None
        verified_reports: dict[str, str] = {}
        for name in expected:
            filename = _safe_name(verified.get(name))
            if Path(filename).suffix.casefold() != ".html":
                return None
            report = report_dir / filename
            if report.is_symlink() or not report.is_file():
                return None
            verified_reports[name] = filename
        return _Job(
            job_id, Path(generation_path).resolve(), manifest, expected, expected, start, end,
            report_dir, strategy_dir, verified_reports=verified_reports, preserve_reports=True,
            state="COMMITTED", phase="COMMITTED", single_mode=document.get("mode") == "SINGLE_MODE",
        )

    def capture_inbox(self, job_id: str, *, force_single_mode: bool = False) -> Path:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            job = self._load_persisted_job_for_inbox(job_id)
            if job is None:
                raise FastStrategyTestError("Fast TEST has no completed reports")
            with self._lock:
                self._jobs.setdefault(job_id, job)
        if force_single_mode:
            job.single_mode = True
        if job.phase not in {"RUNNING", "COMMITTED", "CAPTURING_INBOX", "INBOX_CREATION"} or set(job.verified_reports) != set(job.expected_names):
            raise FastStrategyTestError("Fast TEST reports are incomplete")
        inbox_root = self.config.inbox_root.resolve()
        target = inbox_root / job_id
        if target.parent != inbox_root or target == inbox_root:
            raise FastStrategyTestError("inbox job path is unsafe")
        # Verification is idempotent but rebuilds the metadata snapshot so a
        # manual retry after reload records current source hashes and paths.
        snapshots: dict[str, Mapping[str, object]] = {}
        reports: dict[str, Path] = {}
        for name in job.expected_names:
            try:
                strategy = json.loads((job.manifest.strategy_source / f"{name}.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise FastStrategyTestError(f"strategy JSON is unavailable for {name}") from error
            if not isinstance(strategy, Mapping):
                raise FastStrategyTestError(f"strategy JSON is invalid for {name}")
            snapshots[name] = strategy
            reports[name] = job.report_dir / job.verified_reports[name]
        try:
            inbox = capture_run_snapshot_inbox(
                self.config,
                job_id,
                snapshots,
                reports,
                tester_config_bytes=self.config.tester_config.read_bytes(),
                provenance=job.manifest.provenance,
                test_start=job.start_date,
                test_end=job.end_date,
                run_mode="SINGLE_MODE" if job.single_mode else "FAST",
                workers=min(16, self.config.max_parallel_submissions),
                strategy_paths={
                    name: job.manifest.strategy_source / f"{name}.json"
                    for name in job.expected_names
                } if job.single_mode else None,
                replace_existing=job.single_mode,
            )
        except (InboxCaptureError, OSError) as error:
            raise FastStrategyTestError(str(error)) from error
        with self._lock:
            job.inbox_path = inbox
        return inbox

    def mark_inbox_ready(self, job_id: str, inbox: Path) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.inbox_path = inbox.resolve()
            job.state = "COMMITTED"
            job.phase = "COMMITTED"
            job.error = None

    def retry(self, source_job_id: str, *, job_id: str | None = None) -> dict[str, object]:
        with self._lock:
            source_job = self._jobs.get(source_job_id)
            if source_job is None:
                source_job = self._load_persisted_job(source_job_id)
                if source_job is not None:
                    self._jobs[source_job_id] = source_job
            if source_job is None:
                raise FastStrategyTestError("Fast TEST job not found") from None
            if source_job.state not in {"COMMITTED", "CANCELLED", "FAILED"} or source_job.phase not in {"PARTIAL", "FAILED", "CANCELLED"}:
                raise FastStrategyTestError("Fast TEST job has no recoverable failures")
            identifier = _safe_name(job_id or str(uuid4()))
            if identifier in self._jobs:
                raise FastStrategyTestError("Fast TEST job id is already used")
            manifest = validate_strategy_manifest(source_job.manifest_path)
            self._require_diagnostics(manifest, source_job.expected_names)
            failed = [name for name in source_job.expected_names if name not in source_job.verified_reports]
            for name in tuple(failed):
                try:
                    expected_settings = json.loads((source_job.manifest.strategy_source / f"{name}.json").read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    expected_settings = None
                if not isinstance(expected_settings, Mapping):
                    continue
                reports = sorted(source_job.report_dir.glob("*.html"), key=lambda path: path.stat().st_mtime, reverse=True)
                accepted: Path | None = None
                for report in reports:
                    if report.is_symlink():
                        continue
                    settings = extract_html_strategy_settings(report)
                    if isinstance(settings, dict) and settings.get("name") == name and _report_matches_run(report, settings, expected_settings, source_job.start_date, source_job.end_date):
                        source_job.verified_reports[name] = report.name
                        failed.remove(name)
                        accepted = report
                        break
                if accepted is not None:
                    for report in reports:
                        if report != accepted and extract_html_strategy_settings(report) is not None:
                            try:
                                duplicate = extract_html_strategy_settings(report)
                                if isinstance(duplicate, dict) and duplicate.get("name") == name and _report_matches_run(report, duplicate, expected_settings, source_job.start_date, source_job.end_date):
                                    report.unlink()
                            except OSError:
                                pass
            job = _Job(
                identifier,
                source_job.manifest_path,
                manifest,
                source_job.expected_names,
                tuple(failed),
                source_job.start_date,
                source_job.end_date,
                source_job.report_dir,
                source_job.strategy_dir,
                runtime_config=replace(self.config, max_strategy_attempts=max((source_job.attempt_counts.get(name, 0) for name in failed), default=0) + 1),
                attempt_limits={name: source_job.attempt_counts.get(name, 0) + 1 for name in failed},
                attempt_counts=dict(source_job.attempt_counts),
                verified_reports=dict(source_job.verified_reports),
                failed_names=set(failed),
                preserve_reports=True,
                single_mode=source_job.single_mode,
            )
            job.progress = {
                "current": len(job.verified_reports),
                "total": len(job.expected_names),
                "batch_current": 0,
                "batch_total": 0,
                "active": 0,
                "retries": sum(max(0, value - 1) for value in job.attempt_counts.values()),
                "failed": len(failed),
            }
            self._jobs[identifier] = job
            if not failed:
                job.phase = "COMMITTED"
                _clear_directory(job.strategy_dir, expected=self.config.bot_root / "settings_strategy")
                self._write_manifest(job)
                job.state = "COMMITTED"
                return self._snapshot(job)
            job.thread = Thread(
                target=self._run,
                args=(job,),
                daemon=True,
                name="mrs3-panel-single-mode" if self.single_mode else "mrs3-panel-fast-strategy-test",
            )
            job.thread.start()
            return self._snapshot(job)

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError:
                raise FastStrategyTestError("Fast TEST job not found") from None
            return self._snapshot(job)

    def has_active_job(self) -> bool:
        with self._lock:
            return any(job.state not in {"COMMITTED", "CANCELLED", "FAILED"} for job in self._jobs.values())

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError:
                raise FastStrategyTestError("Fast TEST job not found") from None
            if job.state not in {"COMMITTED", "CANCELLED", "FAILED"}:
                job.cancel.set()
                job.phase = "CANCELLING"
        self._emit(job)
        return self._snapshot(job)


class LocalSingleModeStrategyTestService(LocalFastStrategyTestService):
    """Native single-mode tester handoff used by the Performance v2 flow."""

    def __init__(self, config: RunnerConfig, **kwargs: object) -> None:
        kwargs["single_mode"] = True
        super().__init__(config, **kwargs)

    @staticmethod
    def _native_status(value: object) -> str:
        if isinstance(value, Mapping):
            for key in ("status", "state"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    value = candidate
                    break
        source = str(value)
        match = re.search(
            r'class=["\'][^"\']*stat-value[^"\']*["\'][^>]*>\s*([^<]+)',
            source,
            flags=re.IGNORECASE,
        )
        text = match.group(1) if match else re.sub(r"<[^>]+>", " ", source)
        text = " ".join(html.unescape(text).split()).casefold()
        for status in ("running", "completed", "idle"):
            if re.search(rf"\b{status}\b", text):
                return status
        return "unknown"

    @staticmethod
    def _native_reports(
        report_dir: Path,
        expected: set[str],
        *,
        expected_settings: Mapping[str, Mapping[str, object]] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Path]:
        if (
            not isinstance(expected_settings, Mapping)
            or set(expected_settings) != expected
            or start is None
            or end is None
        ):
            raise FastStrategyTestError("native report validation settings are unavailable")
        found: dict[str, Path] = {}
        for report in sorted(report_dir.glob("*.html"), key=lambda path: path.name):
            if report.is_symlink() or not report.is_file():
                continue
            try:
                source = report.read_text(encoding="utf-8")
                modified = report.stat().st_mtime_ns
            except (OSError, UnicodeDecodeError):
                continue
            settings = extract_html_strategy_settings(report)
            name = settings.get("name") if isinstance(settings, Mapping) else None
            if (
                not isinstance(name, str)
                or name not in expected
                or not _has_current_performance_v2_layout(source)
            ):
                continue
            expected_for_name = expected_settings[name]
            if not isinstance(settings, Mapping) or not _report_matches_run(report, settings, expected_for_name, start, end):
                continue
            previous = found.get(name)
            if previous is None or (modified, report.name) > (previous.stat().st_mtime_ns, previous.name):
                found[name] = report
        return found

    def _wait_for_native_idle(self, job: _Job, client: object, config: RunnerConfig, batch_number: int, batch_total: int) -> None:
        deadline = time.monotonic() + config.batch_timeout_seconds
        saw_running = False
        stable_idle = 0
        while True:
            if job.cancel.is_set():
                raise _FastCancelled()
            if not hasattr(client, "tester_status"):
                raise FastStrategyTestError("native SINGLE_MODE client has no status endpoint")
            status = self._native_status(client.tester_status())
            if status == "running":
                saw_running = True
                stable_idle = 0
            elif saw_running and status in {"idle", "completed"}:
                stable_idle += 1
            else:
                stable_idle = 0
            self._set_phase(
                job,
                "BOT_RUN",
                batch_number=batch_number,
                batch_total=batch_total,
                native_status=status,
                active=0 if status in {"idle", "completed"} else 1,
            )
            if saw_running and stable_idle >= config.report_stability_polls:
                return
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("native SINGLE_MODE tester did not reach stable Idle/Completed")
            time.sleep(config.poll_interval_seconds)

    def _start_native_bot(self, job: _Job, config: RunnerConfig, batch_number: int, batch_total: int) -> None:
        """Keep the durable job heartbeat alive while the process binds its port."""
        done = Event()
        failure: list[BaseException] = []
        started_at = time.monotonic()

        def start() -> None:
            try:
                self._start_bot(config)
            except BaseException as error:
                failure.append(error)
            finally:
                done.set()

        Thread(target=start, daemon=True, name="mrs3-panel-bot-start").start()
        while not done.wait(min(config.poll_interval_seconds, 0.5)):
            self._set_phase(
                job,
                "BOT_START",
                batch_number=batch_number,
                batch_total=batch_total,
                active=1,
                startup_elapsed_seconds=round(time.monotonic() - started_at, 3),
            )
        if failure:
            raise failure[0]

    def _run_native_batch(self, job: _Job, names: tuple[str, ...], config: RunnerConfig, batch_number: int, batch_total: int) -> dict[str, Path]:
        client: object | None = None
        try:
            self._stop_bot(config)
            _clear_directory(job.strategy_dir, expected=self.config.bot_root / "settings_strategy")
            _install_names(job.manifest.strategy_source, job.strategy_dir, names)
            config.wizard_result.unlink(missing_ok=True)
            config.wizard_progress.unlink(missing_ok=True)
            self._set_phase(job, "BOT_START", batch_number=batch_number, batch_total=batch_total, active=1, startup_elapsed_seconds=0.0)
            self._start_native_bot(job, config, batch_number, batch_total)
            client = self._client_factory(config)
            if not hasattr(client, "run_tester"):
                raise FastStrategyTestError("native SINGLE_MODE client has no Run endpoint")
            self._set_phase(job, "BOT_RUN", batch_number=batch_number, batch_total=batch_total)
            for name in names:
                job.attempt_counts[name] = job.attempt_counts.get(name, 0) + 1
            client.run_tester()
            self._wait_for_native_idle(job, client, config, batch_number, batch_total)
            self._set_phase(job, "REPORT_COLLECTION", batch_number=batch_number, batch_total=batch_total)
            expected_settings: dict[str, Mapping[str, object]] = {}
            for name in names:
                try:
                    value = json.loads((job.manifest.strategy_source / f"{name}.json").read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise FastStrategyTestError(f"expected strategy settings are unavailable for {name}") from error
                if not isinstance(value, Mapping):
                    raise FastStrategyTestError(f"expected strategy settings are invalid for {name}")
                expected_settings[name] = value
            return self._native_reports(
                job.report_dir,
                set(names),
                expected_settings=expected_settings,
                start=job.start_date,
                end=job.end_date,
            )
        finally:
            try:
                self._stop_bot(config)
            finally:
                if client is not None and hasattr(client, "close"):
                    try:
                        client.close()
                    except BaseException:
                        pass

    def _run_native(self, job: _Job) -> None:
        config = job.runtime_config or self.config
        if not job.preserve_reports:
            _clear_directory(job.report_dir, expected=self.config.bot_root / "tester" / "report" / "my_test")
        job.report_dir.mkdir(parents=True, exist_ok=True)
        _write_fast_tester_config(
            config,
            job.start_date,
            job.end_date,
            single_mode=True,
            template_path=self.tester_config_template,
        )
        batches = [
            job.run_names[index : index + config.strategy_batch_size]
            for index in range(0, len(job.run_names), config.strategy_batch_size)
        ]
        self._set_progress(job, current=0, total=len(job.expected_names), batch_current=0, batch_total=len(batches), active=0, retries=0, failed=0)
        self._write_manifest(job)
        for batch_number, names in enumerate(batches, start=1):
            pending = names
            while pending:
                self._set_phase(job, "BATCH_PREPARE", batch_number=batch_number, batch_total=len(batches), current=len(job.verified_reports), failed=len(job.failed_names))
                reports = self._run_native_batch(job, pending, config, batch_number, len(batches))
                for name, report in reports.items():
                    job.verified_reports[name] = report.name
                    job.failed_names.discard(name)
                missing = tuple(name for name in pending if name not in reports)
                self._set_progress(job, current=len(job.verified_reports), active=0, retries=sum(max(0, value - 1) for value in job.attempt_counts.values()), failed=len(missing))
                if not missing:
                    break
                job.failed_names.update(missing)
                exhausted = tuple(
                    name
                    for name in missing
                    if job.attempt_counts.get(name, 0)
                    >= job.attempt_limits.get(name, config.max_strategy_attempts)
                )
                if exhausted:
                    raise BatchRetryExhausted(
                        "native SINGLE_MODE reports missing after retries: " + ", ".join(exhausted)
                    )
                self._set_phase(job, "RETRY_MISSING", batch_number=batch_number, batch_total=len(batches), current=len(job.verified_reports), failed=len(missing))
                pending = missing
            self._write_manifest(job)

        incomplete = tuple(name for name in job.expected_names if name not in job.verified_reports)
        if incomplete:
            job.failed_names.update(incomplete)
            raise BatchRetryExhausted(
                "native SINGLE_MODE reports missing after retries: " + ", ".join(incomplete)
            )
        _clear_directory(job.strategy_dir, expected=self.config.bot_root / "settings_strategy")
        job.phase = "INBOX_CREATION"
        self._write_manifest(job)
        self.capture_inbox(job.job_id)
        job.state = "COMMITTED"
        job.phase = "INBOX_READY"
        self._write_manifest(job)
        job.phase = "COMMITTED"
        self._write_manifest(job)
        self._set_progress(job, current=len(job.verified_reports), active=0, failed=0)
