from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Callable, Protocol

from .config import RunnerConfig
from .files import (
    cleanup_completed_batch,
    inspect_strategy_batch,
    prepare_batch_files,
    validate_runner_paths,
)
from .http import RowState, StrategyRow, TesterHttpClient
from .monitor import BatchCompletion, monitor_batch
from .process import start_bot, stop_bot
from .results import load_wizard_results, reconcile_results, write_results_csv_atomic


class WorkflowClient(Protocol):
    def list_strategies(self) -> tuple[StrategyRow, ...]: ...

    def launch_strategy(self, name: str) -> object: ...

    def close(self) -> None: ...


def _client(config: RunnerConfig) -> WorkflowClient:
    return TesterHttpClient(config.base_url, timeout=config.request_timeout_seconds)


@dataclass(frozen=True, slots=True)
class WorkflowDependencies:
    stop: Callable[[RunnerConfig], object] = stop_bot
    start: Callable[[RunnerConfig], object] = start_bot
    client_factory: Callable[[RunnerConfig], WorkflowClient] = _client


@dataclass(frozen=True, slots=True)
class BatchPlan:
    strategy_source: Path
    expected_names: tuple[str, ...]
    filenames: tuple[str, ...]
    file_hashes: tuple[tuple[str, str], ...]
    actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BatchRunResult:
    output_csv: Path
    state_file: Path
    progress_file: Path
    events: tuple[str, ...]
    completion: BatchCompletion
    result_rows: int


def validate_runtime_preflight(config: RunnerConfig) -> None:
    validate_runner_paths(config)
    if not config.bot_root.is_dir():
        raise FileNotFoundError(f"bot root does not exist: {config.bot_root}")
    if not os.access(config.bot_root, os.R_OK | os.W_OK | os.X_OK):
        raise PermissionError(f"bot root is not readable and writable: {config.bot_root}")
    if not config.executable_path.is_file():
        raise FileNotFoundError(
            f"bot executable does not exist: {config.executable_path}"
        )
    if not os.access(config.executable_path, os.R_OK):
        raise PermissionError(f"bot executable is not readable: {config.executable_path}")


def plan_batch(config: RunnerConfig, strategy_source: Path) -> BatchPlan:
    validate_runtime_preflight(config)
    inspection = inspect_strategy_batch(strategy_source)
    names = inspection.expected_names
    actions = (
        "POST /htmx/system/shutdown; fallback terminate only the verified listener PID",
        f"delete exact report tree {config.report_dir} and the two wizard JSON logs",
        f"replace {config.strategy_dir} with {len(names)} validated strategy JSON files",
        f"start {config.executable_path} and wait for local port {config.port}",
        "GET /htmx/tester/strategies-table until the exact strategy batch is visible",
        "for each strategy: GET its Base64 single wizard, then POST /htmx/tester/wizard/run",
        "poll per-row progress until Result + matching wizard JSON + stable HTML",
        "parse and reconcile JSON/HTML, then atomically commit the output CSV",
        "only after CSV commit, stop the verified bot again and delete reports/logs",
    )
    return BatchPlan(
        strategy_source=inspection.source_directory,
        expected_names=names,
        filenames=inspection.filenames,
        file_hashes=inspection.file_hashes,
        actions=actions,
    )


def _state_path(output_csv: Path) -> Path:
    return output_csv.with_name(f"{output_csv.stem}.state.json")


def _progress_path(output_csv: Path) -> Path:
    return output_csv.with_name(f"{output_csv.stem}.progress.json")


def _write_json_atomic(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.stem}.", suffix=".json", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_state(
    path: Path,
    state: str,
    events: list[str],
    plan: BatchPlan,
    output_csv: Path,
    error: str | None = None,
) -> None:
    document = {
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "events": events,
        "strategy_source": str(plan.strategy_source),
        "expected_names": plan.expected_names,
        "file_hashes": dict(plan.file_hashes),
        "output_csv": str(output_csv),
        "error": error,
    }
    _write_json_atomic(path, document)


def _output_is_safe(output_csv: Path, config: RunnerConfig) -> None:
    output = output_csv.resolve()
    report_dir = config.report_dir.resolve()
    try:
        output.relative_to(report_dir)
    except ValueError:
        pass
    else:
        raise ValueError("output CSV cannot be inside the report directory that is cleaned")
    if output in {config.wizard_result.resolve(), config.wizard_progress.resolve()}:
        raise ValueError("output CSV cannot replace a wizard log")


def _wait_for_exact_batch(
    client: WorkflowClient, expected_names: tuple[str, ...], config: RunnerConfig
) -> tuple[StrategyRow, ...]:
    expected = set(expected_names)
    deadline = time.monotonic() + config.startup_timeout_seconds
    last_names: set[str] = set()
    while time.monotonic() < deadline:
        rows = client.list_strategies()
        last_names = {row.name for row in rows}
        if last_names == expected and all(row.state is RowState.TEST for row in rows):
            return rows
        time.sleep(config.poll_interval_seconds)
    raise TimeoutError(
        "tester did not expose the exact clean strategy batch; "
        f"expected={sorted(expected)!r}, visible={sorted(last_names)!r}"
    )


def run_batch(
    config: RunnerConfig,
    strategy_source: Path,
    output_csv: Path,
    *,
    dependencies: WorkflowDependencies | None = None,
) -> BatchRunResult:
    dependencies = dependencies or WorkflowDependencies()
    plan = plan_batch(config, strategy_source)
    output = output_csv.resolve()
    _output_is_safe(output, config)
    state_file = _state_path(output)
    progress_file = _progress_path(output)
    events: list[str] = []

    def advance(state: str) -> None:
        events.append(state)
        _write_state(state_file, state, events, plan, output)

    def report_progress(snapshot: dict[str, object]) -> None:
        _write_json_atomic(
            progress_file,
            {
                **snapshot,
                "workflow_state": events[-1] if events else "PRECHECK",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    completion: BatchCompletion | None = None
    try:
        report_progress(
            {
                "expected_count": len(plan.expected_names),
                "submitted_count": 0,
                "completed_count": 0,
                "waiting_count": len(plan.expected_names),
                "running_count": 0,
                "result_count": 0,
                "polls": 0,
                "elapsed_seconds": 0,
                "active_total": 0,
                "active": [],
            }
        )
        advance("PRECHECK")
        dependencies.stop(config)
        advance("STOPPED")
        prepared = prepare_batch_files(
            config,
            plan.strategy_source,
            expected_file_hashes=plan.file_hashes,
        )
        if (
            prepared.expected_names != plan.expected_names
            or prepared.file_hashes != plan.file_hashes
        ):
            raise RuntimeError("strategy source changed between preflight and installation")
        advance("CLEAN")
        advance("INSTALLED")
        dependencies.start(config)
        advance("STARTED")

        client = dependencies.client_factory(config)
        try:
            _wait_for_exact_batch(client, plan.expected_names, config)
            advance("VISIBLE")
            total = len(plan.expected_names)
            for index, name in enumerate(plan.expected_names, start=1):
                client.launch_strategy(name)
                if index == 1 or index % 50 == 0 or index == total:
                    report_progress(
                        {
                            "expected_count": total,
                            "submitted_count": index,
                            "completed_count": 0,
                            "waiting_count": total - index,
                            "running_count": 0,
                            "result_count": 0,
                            "polls": 0,
                            "elapsed_seconds": 0,
                            "active_total": 0,
                            "active": [],
                        }
                    )
            advance("SUBMITTED")
            advance("MONITORING")
            completion = monitor_batch(
                client,
                plan.expected_names,
                config.wizard_result,
                config.report_dir,
                config,
                progress_callback=report_progress,
            )
        finally:
            client.close()

        wizard_results = load_wizard_results(config.wizard_result)
        frame = reconcile_results(
            plan.expected_names,
            wizard_results,
            config.report_dir,
            config.metric_tolerance,
        )
        advance("RECONCILED")
        write_results_csv_atomic(frame, output)
        advance("CSV_COMMITTED")
        dependencies.stop(config)
        advance("STOPPED_FOR_CLEANUP")
        cleanup_completed_batch(config)
        advance("RAW_ARTIFACTS_REMOVED")
        advance("COMPLETED")
        if completion is None:
            raise RuntimeError("batch completion was not recorded")
        return BatchRunResult(
            output_csv=output,
            state_file=state_file,
            progress_file=progress_file,
            events=tuple(events),
            completion=completion,
            result_rows=len(frame),
        )
    except BaseException as error:
        try:
            _write_state(
                state_file,
                "FAILED",
                events,
                plan,
                output,
                error=f"{type(error).__name__}: {error}",
            )
        except Exception:
            pass
        raise
