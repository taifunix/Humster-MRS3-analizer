from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Callable, Mapping, Protocol

import httpx
import psutil

from .config import RunnerConfig
from .files import (
    BatchPreparationError,
    _root_json_files,
    _source_is_inside_strategy_dir,
    cleanup_completed_batch,
    inspect_strategy_batch,
    prepare_batch_files,
    validate_runner_paths,
)
from .http import RowState, StrategyRow, TesterHttpClient
from .inbox import capture_verified_inbox
from .monitor import BatchCompletion, BatchHtmlCollision, BatchTimeout, monitor_controlled_batch
from .process import start_bot, stop_bot
from .results import (
    ResultMismatchError,
    ResultParseError,
    WizardResult,
    load_wizard_results,
    reconcile_results,
    write_results_csv_atomic,
)


class WorkflowClient(Protocol):
    def list_strategies(self) -> tuple[StrategyRow, ...]: ...

    def launch_strategy(self, name: str) -> object: ...

    def close(self) -> None: ...


def _client(config: RunnerConfig) -> WorkflowClient:
    return TesterHttpClient(config.base_url, timeout=config.request_timeout_seconds)


def _is_recoverable_tester_failure(error: Exception) -> bool:
    if isinstance(error, (BatchHtmlCollision, BatchTimeout, TimeoutError, httpx.TransportError)):
        return True
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code >= 500


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
    root_json_to_replace: tuple[str, ...]
    actions: tuple[str, ...]
    resume_completed_names: tuple[str, ...] = ()
    resume_results: tuple[WizardResult, ...] = ()

    @property
    def resume_remaining_names(self) -> tuple[str, ...]:
        completed = set(self.resume_completed_names)
        return tuple(name for name in self.expected_names if name not in completed)


@dataclass(frozen=True, slots=True)
class BatchRunResult:
    output_csv: Path
    state_file: Path
    progress_file: Path
    events: tuple[str, ...]
    completion: BatchCompletion
    result_rows: int
    inbox_path: Path | None = None


def _require_complete_verified_reports(
    expected_names: tuple[str, ...], report_paths: Mapping[str, Path]
) -> None:
    missing = sorted(set(expected_names).difference(report_paths))
    if missing or any(not report_paths[name].is_file() for name in expected_names):
        raise RuntimeError("verified HTML reports are incomplete: " + ", ".join(missing or expected_names))


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


def _resume_results(
    config: RunnerConfig,
    inspection: object,
    output_csv: Path | None,
    *,
    hydrate_reports: bool = True,
) -> tuple[WizardResult, ...]:
    if output_csv is None:
        return ()
    try:
        state = json.loads(_state_path(output_csv.resolve()).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if not isinstance(state, dict):
        return ()
    if _run_lock_is_active(output_csv.resolve()):
        return ()
    if state.get("expected_names") != list(inspection.expected_names):
        return ()
    if state.get("file_hashes") != dict(inspection.file_hashes):
        return ()
    report_paths = _load_saved_report_paths(output_csv.resolve())
    report_paths.update(_load_snapshot_report_paths(output_csv.resolve(), inspection.expected_names))
    persisted = _validated_saved_results(
        config,
        inspection.expected_names,
        _load_saved_results(output_csv.resolve()),
        report_paths,
        _load_saved_result_evidence(output_csv.resolve()),
        verify_hash=hydrate_reports,
    )
    return persisted


def _validated_results_for_names(
    config: RunnerConfig,
    expected_names: tuple[str, ...],
    report_paths: Mapping[str, Path] | None = None,
) -> tuple[WizardResult, ...]:
    """Return only single-strategy results backed by matching valid HTML."""
    try:
        results = load_wizard_results(
            config.wizard_result,
            fallback_report_names={
                name: path.name for name, path in (report_paths or {}).items()
            },
        )
    except Exception:
        return ()
    by_name = {
        result.strategy_names[0]: result
        for result in results
        if len(result.strategy_names) == 1 and result.strategy_names[0] in expected_names
    }
    validated: list[WizardResult] = []
    for name in expected_names:
        result = by_name.get(name)
        if result is None:
            continue
        try:
            reconcile_results(
                (name,),
                (result,),
                config.report_dir,
                config.metric_tolerance,
                report_paths={name: report_paths[name]} if report_paths and name in report_paths else None,
            )
        except (ResultMismatchError, ResultParseError, OSError):
            continue
        validated.append(result)
    return tuple(validated)


def _merge_wizard_results(
    saved: tuple[WizardResult, ...], current: tuple[WizardResult, ...]
) -> tuple[WizardResult, ...]:
    """Keep validated resume results when the tester rewrites its shared log."""
    merged: dict[str, WizardResult] = {}
    for result in (*saved, *current):
        if len(result.strategy_names) == 1:
            merged[result.strategy_names[0]] = result
    return tuple(merged.values())


def _wait_for_stable_reconciliation(
    plan: BatchPlan,
    saved: tuple[WizardResult, ...],
    config: RunnerConfig,
    report_paths: dict[str, Path] | None = None,
) -> object:
    """The tester updates wizard JSON after RESULT; require stable matching views."""
    deadline = time.monotonic() + config.stall_timeout_seconds
    stable = 0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            reconcile_kwargs = {"report_paths": report_paths} if report_paths else {}
            fallback_report_names = {
                name: path.name for name, path in (report_paths or {}).items()
            }
            frame = reconcile_results(
                plan.expected_names,
                _merge_wizard_results(
                    saved,
                    load_wizard_results(
                        config.wizard_result,
                        fallback_report_names=fallback_report_names,
                    ),
                ),
                config.report_dir,
                config.metric_tolerance,
                **reconcile_kwargs,
            )
        except (ResultMismatchError, ResultParseError) as error:
            stable = 0
            last_error = error
        else:
            stable += 1
            if stable >= config.report_stability_polls:
                return frame
        time.sleep(config.poll_interval_seconds)
    if last_error is not None:
        raise last_error
    raise TimeoutError("tester results did not stabilize before reconciliation")


def _wait_for_verified_batch_results(
    expected_names: tuple[str, ...],
    config: RunnerConfig,
    report_paths: dict[str, Path] | None = None,
) -> tuple[WizardResult, ...]:
    """Keep each completed chunk before the tester accepts the next one."""
    deadline = time.monotonic() + config.stall_timeout_seconds
    stable = 0
    last_error: Exception | None = None
    expected = set(expected_names)
    while time.monotonic() < deadline:
        try:
            reconcile_kwargs = {"report_paths": report_paths} if report_paths else {}
            fallback_report_names = {
                name: path.name for name, path in (report_paths or {}).items()
            }
            current = tuple(
                result
                for result in load_wizard_results(
                    config.wizard_result,
                    fallback_report_names=fallback_report_names,
                )
                if len(result.strategy_names) == 1 and result.strategy_names[0] in expected
            )
            if {result.strategy_names[0] for result in current} != expected:
                raise ResultParseError("tester wizard results are incomplete for the installed chunk")
            reconcile_results(
                expected_names,
                current,
                config.report_dir,
                config.metric_tolerance,
                **reconcile_kwargs,
            )
        except ResultMismatchError as error:
            mismatched = tuple(
                name
                for name in re.findall(r"HTML strategy name differs for ([^:]+):", str(error))
                if name in expected
            )
            if mismatched:
                raise BatchHtmlCollision(mismatched) from error
            stable = 0
            last_error = error
        except (ResultParseError, OSError) as error:
            stable = 0
            last_error = error
        else:
            stable += 1
            if stable >= config.report_stability_polls:
                return current
        time.sleep(config.poll_interval_seconds)
    if last_error is not None:
        raise last_error
    raise TimeoutError("tester chunk results did not stabilize")


def plan_batch(
    config: RunnerConfig,
    strategy_source: Path,
    output_csv: Path | None = None,
    *,
    hydrate_resume: bool = True,
) -> BatchPlan:
    validate_runtime_preflight(config)
    if _source_is_inside_strategy_dir(strategy_source, config.strategy_dir):
        raise BatchPreparationError(
            f"strategy source cannot be inside strategy_dir: {strategy_source.resolve()}"
        )
    inspection = inspect_strategy_batch(strategy_source)
    names = inspection.expected_names
    resume_results = _resume_results(
        config, inspection, output_csv, hydrate_reports=hydrate_resume
    )
    resumed = {result.strategy_names[0] for result in resume_results}
    resume_completed_names = tuple(name for name in names if name in resumed)
    root_json_to_replace = (
        tuple(path.name for path in _root_json_files(config.strategy_dir))
        if config.strategy_dir.exists()
        else ()
    )
    actions = (
        "POST /htmx/system/shutdown; fallback terminate only the verified listener PID",
        (
            f"resume {len(resume_completed_names)} verified results; submit only "
            f"{len(names) - len(resume_completed_names)} remaining strategies"
            if resume_completed_names
            else f"delete exact report tree {config.report_dir} and the two wizard JSON logs"
        ),
        "replace "
        f"{len(root_json_to_replace)} root-level strategy JSON files "
        f"({', '.join(root_json_to_replace) or 'none'}) in {config.strategy_dir} "
        f"with {len(names)} validated strategy JSON files",
        f"start {config.executable_path} and wait for local port {config.port}",
        "GET /htmx/tester/strategies-table until the installed strategy batch is visible",
        "submit at most the configured tester window; refill only after verified results",
        "retry a RUNNING row returned to TEST up to the configured attempt limit",
        "poll per-row progress until Result + matching wizard JSON + stable HTML",
        "parse and reconcile JSON/HTML, then atomically commit the output CSV",
        "only after CSV commit, stop the verified bot again and delete reports/logs",
    )
    return BatchPlan(
        strategy_source=inspection.source_directory,
        expected_names=names,
        filenames=inspection.filenames,
        file_hashes=inspection.file_hashes,
        root_json_to_replace=root_json_to_replace,
        actions=actions,
        resume_completed_names=resume_completed_names,
        resume_results=resume_results,
    )


def _state_path(output_csv: Path) -> Path:
    return output_csv.with_name(f"{output_csv.stem}.state.json")


def _progress_path(output_csv: Path) -> Path:
    return output_csv.with_name(f"{output_csv.stem}.progress.json")


def _load_resume_attempt_counts(output_csv: Path, plan: BatchPlan) -> dict[str, int]:
    try:
        state = json.loads(_state_path(output_csv).read_text(encoding="utf-8"))
        progress = json.loads(_progress_path(output_csv).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(state, dict)
        or state.get("expected_names") != list(plan.expected_names)
        or state.get("file_hashes") != dict(plan.file_hashes)
        or not isinstance(progress, dict)
        or not isinstance(progress.get("attempt_counts"), dict)
    ):
        return {}
    expected = set(plan.expected_names)
    counts: dict[str, int] = {}
    for name, raw_count in progress["attempt_counts"].items():
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if name in expected and count > 0:
            counts[name] = count
    return counts


def _saved_report_paths_path(output_csv: Path) -> Path:
    return output_csv.with_name(f".{output_csv.stem}.saved-report-paths.json")


def _saved_results_path(output_csv: Path) -> Path:
    return output_csv.with_name(f".{output_csv.stem}.saved-results.json")


def _load_saved_report_paths(output_csv: Path) -> dict[str, Path]:
    try:
        document = json.loads(_saved_report_paths_path(output_csv).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    paths: dict[str, Path] = {}
    for name, raw_path in document.items():
        if not isinstance(name, str) or not isinstance(raw_path, str):
            continue
        path = Path(raw_path).resolve()
        if path.is_file():
            paths[name] = path
    return paths


def _load_snapshot_report_paths(output_csv: Path, expected_names: tuple[str, ...]) -> dict[str, Path]:
    snapshot_dir = output_csv.with_name(f".{output_csv.stem}.report_snapshots")
    expected = set(expected_names)
    paths: dict[str, Path] = {}
    snapshots = sorted(
        snapshot_dir.glob("*.html"),
        key=lambda path: (path.stat().st_mtime_ns, path.name.casefold()),
    )
    for path in snapshots:
        name, marker, _ = path.name.rpartition("__")
        if not marker:
            continue
        if name in expected:
            paths[name] = path.resolve()
    return paths


def _load_or_capture_tester_config_snapshot(output_csv: Path, config: RunnerConfig) -> bytes:
    path = output_csv.with_name(f".{output_csv.stem}.tester-config.snapshot")
    if path.is_file():
        return path.read_bytes()
    snapshot = config.tester_config.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(snapshot)
    os.replace(temporary, path)
    return snapshot


def _result_document(result: WizardResult) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "timestamp": result.timestamp,
        "strategy_names": list(result.strategy_names),
        "stats": {key: str(value) for key, value in result.stats.items()},
        "chart_url": result.chart_url,
        "report_name": result.report_name,
        "period": result.period,
        "elapsed": result.elapsed,
    }


def _result_from_document(document: object) -> WizardResult | None:
    if not isinstance(document, dict):
        return None
    names = document.get("strategy_names")
    stats = document.get("stats")
    if (
        not isinstance(names, list)
        or len(names) != 1
        or not isinstance(names[0], str)
        or not isinstance(stats, dict)
    ):
        return None
    values = ("run_id", "timestamp", "chart_url", "report_name", "period", "elapsed")
    if not all(isinstance(document.get(key), str) for key in values):
        return None
    return WizardResult(
        run_id=str(document["run_id"]),
        timestamp=str(document["timestamp"]),
        strategy_names=(names[0],),
        stats={str(key): value for key, value in stats.items()},
        chart_url=str(document["chart_url"]),
        report_name=str(document["report_name"]),
        period=str(document["period"]),
        elapsed=str(document["elapsed"]),
    )


def _load_saved_results(output_csv: Path) -> tuple[WizardResult, ...]:
    try:
        document = json.loads(_saved_results_path(output_csv).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if not isinstance(document, dict):
        return ()
    return tuple(
        result
        for raw in document.values()
        if (result := _result_from_document(raw)) is not None
    )


def _load_saved_result_evidence(
    output_csv: Path,
) -> dict[str, tuple[Path, int, int, str]]:
    try:
        document = json.loads(_saved_results_path(output_csv).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    evidence: dict[str, tuple[Path, int, int, str]] = {}
    for name, raw in document.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        try:
            evidence[name] = (
                Path(str(raw["report_path"])).resolve(),
                int(raw["report_size"]),
                int(raw["report_mtime_ns"]),
                str(raw["report_sha256"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return evidence


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_saved_results(
    output_csv: Path,
    results: tuple[WizardResult, ...],
    report_paths: Mapping[str, Path],
) -> None:
    previous = _load_saved_result_evidence(output_csv)

    def evidence(name: str, path: Path) -> tuple[object, str]:
        stat = path.stat()
        stored = previous.get(name)
        if (
            stored is not None
            and stored[:3] == (path.resolve(), stat.st_size, stat.st_mtime_ns)
            and len(stored[3]) == 64
        ):
            return stat, stored[3]
        return stat, _file_sha256(path)

    documents: dict[str, object] = {}
    for result in results:
        next_name = result.strategy_names[0]
        path = report_paths.get(next_name)
        if path is None or not path.is_file():
            continue
        stat, digest = evidence(next_name, path)
        documents[next_name] = {
            **_result_document(result),
            "report_path": str(path),
            "report_size": stat.st_size,
            "report_mtime_ns": stat.st_mtime_ns,
            "report_sha256": digest,
        }
    _write_json_atomic(
        _saved_results_path(output_csv),
        documents,
    )


def _validated_saved_results(
    config: RunnerConfig,
    expected_names: tuple[str, ...],
    candidates: tuple[WizardResult, ...],
    report_paths: Mapping[str, Path],
    evidence: Mapping[str, tuple[Path, int, int, str]] | None = None,
    *,
    verify_hash: bool = True,
) -> tuple[WizardResult, ...]:
    expected = set(expected_names)
    valid: list[WizardResult] = []
    for result in candidates:
        if len(result.strategy_names) != 1 or result.strategy_names[0] not in expected:
            continue
        name = result.strategy_names[0]
        path = report_paths.get(name)
        stored = (evidence or {}).get(name)
        if path is not None and stored is not None:
            try:
                stat = path.stat()
            except OSError:
                pass
            else:
                if (
                    path.resolve() == stored[0]
                    and stat.st_size == stored[1]
                    and stat.st_mtime_ns == stored[2]
                    and len(stored[3]) == 64
                    and (not verify_hash or _file_sha256(path) == stored[3])
                ):
                    valid.append(result)
                    continue
        try:
            reconcile_results(
                (name,), (result,), config.report_dir, config.metric_tolerance,
                report_paths={name: report_paths[name]},
            )
        except (ResultMismatchError, ResultParseError, OSError, KeyError):
            continue
        valid.append(result)
    return tuple(valid)


def _run_lock_path(output_csv: Path) -> Path:
    return output_csv.with_name(f".{output_csv.stem}.runner.lock")


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _run_lock_is_active(output_csv: Path) -> bool:
    try:
        document = json.loads(_run_lock_path(output_csv).read_text(encoding="utf-8"))
        pid = int(document.get("pid", 0)) if isinstance(document, dict) else 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return pid != os.getpid() and _pid_is_running(pid)


def _acquire_run_lock(output_csv: Path) -> Path:
    """Claim one output CSV so duplicate panel clicks cannot start two runners."""
    lock = _run_lock_path(output_csv)
    lock.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()},
        ensure_ascii=False,
    ).encode("utf-8")
    while True:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                document = json.loads(lock.read_text(encoding="utf-8"))
                pid = int(document.get("pid", 0)) if isinstance(document, dict) else 0
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                pid = 0
            if _pid_is_running(pid):
                raise RuntimeError(
                    f"tester runner is already running for {output_csv} (pid {pid})"
                )
            lock.unlink(missing_ok=True)
            continue
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        return lock


def _release_run_lock(lock: Path) -> None:
    try:
        document = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if isinstance(document, dict) and document.get("pid") == os.getpid():
        lock.unlink(missing_ok=True)


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
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        temporary.unlink(missing_ok=True)


def _write_state(
    path: Path,
    state: str,
    events: list[str],
    plan: BatchPlan,
    output_csv: Path,
    error: str | None = None,
    inbox_path: Path | None = None,
    provenance: Mapping[str, object] | None = None,
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
    if inbox_path is not None:
        document["inbox_path"] = str(inbox_path)
    if provenance is not None:
        document["v6_provenance"] = dict(provenance)
    _write_json_atomic(path, document)


def _output_is_safe(output_csv: Path, config: RunnerConfig) -> None:
    output = output_csv.resolve()
    try:
        output.relative_to(config.bot_root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("output CSV must be outside bot_root")
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
    client: WorkflowClient,
    expected_names: tuple[str, ...],
    config: RunnerConfig,
    *,
    allow_result_rows: bool = False,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[StrategyRow, ...]:
    expected = set(expected_names)
    deadline = time.monotonic() + config.startup_timeout_seconds
    last_names: set[str] = set()
    while time.monotonic() < deadline:
        if cancel_check is not None and cancel_check():
            raise InterruptedError("tester batch startup cancelled")
        rows = client.list_strategies()
        last_names = {row.name for row in rows}
        expected_rows = tuple(row for row in rows if row.name in expected)
        allowed_states = {RowState.TEST, RowState.RESULT} if allow_result_rows else {RowState.TEST}
        if {row.name for row in expected_rows} == expected and all(
            row.state in allowed_states for row in expected_rows
        ):
            return expected_rows
        time.sleep(config.poll_interval_seconds)
    raise TimeoutError(
        "tester did not expose the installed strategy batch in TEST state; "
        f"expected={sorted(expected)!r}, visible={sorted(last_names)!r}"
    )


def run_batch(
    config: RunnerConfig,
    strategy_source: Path,
    output_csv: Path,
    *,
    dependencies: WorkflowDependencies | None = None,
    provenance: Mapping[str, object] | None = None,
) -> BatchRunResult:
    dependencies = dependencies or WorkflowDependencies()
    output = output_csv.resolve()
    _output_is_safe(output, config)
    run_lock = _acquire_run_lock(output)
    try:
        plan = plan_batch(config, strategy_source, output, hydrate_resume=False)
    except BaseException:
        _release_run_lock(run_lock)
        raise
    run_names = plan.resume_remaining_names
    state_file = _state_path(output)
    progress_file = _progress_path(output)
    events: list[str] = []

    def advance(state: str, *, inbox_path: Path | None = None) -> None:
        events.append(state)
        _write_state(state_file, state, events, plan, output, inbox_path=inbox_path, provenance=provenance)

    def report_progress(snapshot: dict[str, object]) -> None:
        _write_json_atomic(
            progress_file,
            {
                **snapshot,
                "workflow_state": events[-1] if events else "PRECHECK",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def merged_counts(
        previous: Mapping[str, int], current: Mapping[str, int]
    ) -> dict[str, int]:
        merged = dict(previous)
        for reason, count in current.items():
            merged[reason] = merged.get(reason, 0) + int(count)
        return dict(sorted(merged.items()))

    completion: BatchCompletion | None = None
    bot_started = False
    bot_restart_count = 0
    strategy_attempt_counts = _load_resume_attempt_counts(output, plan)
    cumulative_retry_reasons: dict[str, int] = {}
    restart_reasons: dict[str, int] = {}
    last_restart_error: str | None = None
    forced_retry_names: set[str] = set()
    serial_retry_keys: dict[str, str] = {}
    verified_report_paths: dict[str, Path] = {}
    report_snapshot_dir = output.parent / f".{output.stem}.report_snapshots"
    try:
        tester_config_snapshot = _load_or_capture_tester_config_snapshot(output, config)
        saved_resume_results = plan.resume_results
        report_progress(
            {
                "expected_count": len(plan.expected_names),
                "submitted_count": 0,
                "completed_count": len(plan.resume_completed_names),
                "waiting_count": len(run_names),
                "running_count": 0,
                "result_count": 0,
                "polls": 0,
                "elapsed_seconds": 0,
                "active_total": 0,
                "active": [],
                "retry_count": 0,
                "bot_restart_count": 0,
                "retry_reasons": {},
                "restart_reasons": {},
                "last_restart_error": None,
                "attempt_counts": strategy_attempt_counts,
            }
        )
        dependencies.stop(config)
        plan = plan_batch(config, strategy_source, output, hydrate_resume=True)
        run_names = plan.resume_remaining_names
        saved_resume_results = plan.resume_results
        advance("PRECHECK")
        advance("STOPPED")
        verified_report_paths.update(_load_saved_report_paths(output))
        verified_report_paths.update(_load_snapshot_report_paths(output, plan.expected_names))
        completed_at_resume = len(plan.resume_completed_names)
        report_progress(
            {
                "expected_count": len(plan.expected_names),
                "submitted_count": completed_at_resume,
                "completed_count": completed_at_resume,
                "waiting_count": len(run_names),
                "running_count": 0,
                "result_count": completed_at_resume,
                "polls": 0,
                "elapsed_seconds": 0,
                "active_total": 0,
                "active": [],
                "retry_count": 0,
                "retry_reasons": {},
                "bot_restart_count": 0,
                "restart_reasons": {},
                "last_restart_error": None,
                "attempt_counts": strategy_attempt_counts,
            }
        )
        if not run_names:
            frame = _wait_for_stable_reconciliation(
                plan, saved_resume_results, config, verified_report_paths
            )
            advance("RECONCILED")
            write_results_csv_atomic(frame, output)
            advance("CSV_COMMITTED")
            inbox_path = None
            _require_complete_verified_reports(plan.expected_names, verified_report_paths)
            inbox_path = capture_verified_inbox(
                config, output, plan, saved_resume_results, verified_report_paths,
                tester_config_bytes=tester_config_snapshot,
                provenance=provenance,
            )
            advance("INBOX_CAPTURED")
            advance("COMPLETED", inbox_path=inbox_path)
            return BatchRunResult(
                output_csv=output,
                state_file=state_file,
                progress_file=progress_file,
                events=tuple(events),
                completion=BatchCompletion(strategies={}, polls=0, elapsed_seconds=0),
                result_rows=len(frame),
                inbox_path=inbox_path,
            )
        batch_number = 0
        while run_names:
            batch_number += 1
            batch_names = run_names[: config.strategy_batch_size]
            prepared = prepare_batch_files(
                config,
                plan.strategy_source,
                expected_file_hashes=plan.file_hashes,
                selected_names=batch_names,
                preserve_raw_artifacts=bool(saved_resume_results),
            )
            if prepared.expected_names != batch_names:
                raise RuntimeError("installed strategy chunk does not match the planned chunk")
            advance("CLEAN")
            advance(f"INSTALLED_BATCH_{batch_number}")
            while batch_names:
                client: WorkflowClient | None = None
                attempt_retry_reasons: dict[str, int] = {}
                try:
                    dependencies.start(config)
                    bot_started = True
                    advance("STARTED" if bot_restart_count == 0 else f"BOT_RESTART_{bot_restart_count}")
                    client = dependencies.client_factory(config)
                    completed_before_attempt = len(saved_resume_results)

                    def report_attempt_progress(snapshot: dict[str, object]) -> None:
                        nonlocal attempt_retry_reasons
                        adjusted = dict(snapshot)
                        raw_attempts = snapshot.get("attempt_counts", {})
                        if isinstance(raw_attempts, dict):
                            for name, count in raw_attempts.items():
                                if isinstance(name, str):
                                    strategy_attempt_counts[name] = max(
                                        strategy_attempt_counts.get(name, 0), int(count)
                                    )
                        raw_reasons = snapshot.get("retry_reasons", {})
                        if isinstance(raw_reasons, dict):
                            attempt_retry_reasons = {
                                str(reason): int(count)
                                for reason, count in raw_reasons.items()
                            }
                        adjusted["expected_count"] = len(plan.expected_names)
                        for key in ("submitted_count", "completed_count", "result_count"):
                            adjusted[key] = completed_before_attempt + int(snapshot.get(key, 0))
                        adjusted["bot_restart_count"] = bot_restart_count
                        adjusted["retry_count"] = sum(
                            max(0, count - 1) for count in strategy_attempt_counts.values()
                        )
                        adjusted["retry_reasons"] = merged_counts(
                            cumulative_retry_reasons, attempt_retry_reasons
                        )
                        adjusted["restart_reasons"] = dict(sorted(restart_reasons.items()))
                        adjusted["last_restart_error"] = last_restart_error
                        report_progress(adjusted)

                    def record_attempts(counts: dict[str, int]) -> None:
                        for name, count in counts.items():
                            strategy_attempt_counts[name] = max(
                                strategy_attempt_counts.get(name, 0), count
                            )

                    visible_rows = _wait_for_exact_batch(
                        client,
                        batch_names,
                        config,
                        allow_result_rows=bool(saved_resume_results) or bot_restart_count > 0,
                    )
                    advance("VISIBLE")
                    advance("SUBMITTED")
                    advance("MONITORING")
                    monitor_kwargs: dict[str, object] = {
                        "progress_callback": report_attempt_progress,
                        "snapshot_report_dir": report_snapshot_dir,
                        "initial_attempt_counts": strategy_attempt_counts,
                        "attempts_callback": record_attempts,
                    }
                    retry_keys = {
                        name: key
                        for name, key in serial_retry_keys.items()
                        if name in batch_names
                    }
                    if retry_keys:
                        monitor_kwargs["collision_keys"] = retry_keys
                        monitor_kwargs["verified_report_dir"] = (
                            output.parent / f".{output.stem}.collision_reports"
                        )
                    completion = monitor_controlled_batch(
                        client,
                        batch_names,
                        config.wizard_result,
                        config.report_dir,
                        config,
                        **monitor_kwargs,
                    )
                    for name, strategy in completion.strategies.items():
                        strategy_attempt_counts[name] = max(
                            strategy_attempt_counts.get(name, 0), strategy.attempts
                        )
                    verified_report_paths.update(
                        {
                            name: strategy.report_path
                            for name, strategy in completion.strategies.items()
                            if strategy.report_path is not None
                        }
                    )
                    saved_resume_results = _merge_wizard_results(
                        saved_resume_results,
                        _wait_for_verified_batch_results(
                            batch_names, config, verified_report_paths
                        ),
                    )
                    _write_saved_results(output, saved_resume_results, verified_report_paths)
                    cumulative_retry_reasons = merged_counts(
                        cumulative_retry_reasons, attempt_retry_reasons
                    )
                    forced_retry_names.difference_update(batch_names)
                    for name in batch_names:
                        serial_retry_keys.pop(name, None)
                except Exception as error:
                    if client is not None:
                        try:
                            client.close()
                        except Exception:
                            pass
                    if not _is_recoverable_tester_failure(error):
                        raise
                    try:
                        dependencies.stop(config)
                    except Exception:
                        events.append("BOT_RESTART_STOP_FAILED")
                    bot_started = False
                    cumulative_retry_reasons = merged_counts(
                        cumulative_retry_reasons, attempt_retry_reasons
                    )
                    reason = type(error).__name__
                    restart_reasons[reason] = restart_reasons.get(reason, 0) + 1
                    last_restart_error = f"{reason}: {error}"
                    if isinstance(error, BatchHtmlCollision):
                        forced_retry_names.update(error.strategy_names)
                        serial_retry_keys.update(
                            {
                                row.name: f"{row.symbol}\x1f{row.timeframe}"
                                for row in visible_rows
                                if row.name in forced_retry_names
                            }
                        )
                        events.append("HTML_COLLISION_RETRY")
                    verified_report_paths.update(
                        _load_snapshot_report_paths(output, batch_names)
                    )
                    validated = _validated_results_for_names(
                        config, batch_names, verified_report_paths
                    )
                    if forced_retry_names:
                        validated = tuple(
                            result
                            for result in validated
                            if result.strategy_names[0] not in forced_retry_names
                        )
                        saved_resume_results = tuple(
                            result
                            for result in saved_resume_results
                            if result.strategy_names[0] not in forced_retry_names
                        )
                    saved_resume_results = _merge_wizard_results(
                        saved_resume_results,
                        validated,
                    )
                    _write_saved_results(
                        output, saved_resume_results, verified_report_paths
                    )
                    completed_names = {
                        result.strategy_names[0] for result in saved_resume_results
                    }
                    batch_names = tuple(
                        name
                        for name in batch_names
                        if name not in completed_names or name in forced_retry_names
                    )
                    report_progress(
                        {
                            "expected_count": len(plan.expected_names),
                            "submitted_count": len(completed_names),
                            "completed_count": len(completed_names),
                            "waiting_count": len(run_names),
                            "running_count": 0,
                            "result_count": len(completed_names),
                            "polls": 0,
                            "elapsed_seconds": 0,
                            "active_total": 0,
                            "active": [],
                            "retry_count": sum(
                                max(0, count - 1)
                                for count in strategy_attempt_counts.values()
                            ),
                            "retry_reasons": cumulative_retry_reasons,
                            "bot_restart_count": bot_restart_count,
                            "restart_reasons": dict(sorted(restart_reasons.items())),
                            "last_restart_error": last_restart_error,
                            "attempt_counts": strategy_attempt_counts,
                        }
                    )
                    if not batch_names:
                        break
                    if bot_restart_count >= config.max_bot_restarts:
                        remaining = ", ".join(batch_names)
                        raise RuntimeError(
                            f"{config.max_bot_restarts} bot restarts exhausted; "
                            f"{len(batch_names)} strategies remain: {remaining}"
                        ) from error
                    bot_restart_count += 1
                    continue
                else:
                    if client is not None:
                        client.close()
                    dependencies.stop(config)
                    bot_started = False
                    break
            completed_names = {result.strategy_names[0] for result in saved_resume_results}
            run_names = tuple(
                name for name in plan.expected_names if name not in completed_names
            )
            advance(f"BATCH_{batch_number}_COMPLETED")

        frame = _wait_for_stable_reconciliation(
            plan, saved_resume_results, config, verified_report_paths
        )
        advance("RECONCILED")
        write_results_csv_atomic(frame, output)
        advance("CSV_COMMITTED")
        report_progress(
            {
                "expected_count": len(plan.expected_names),
                "submitted_count": len(plan.expected_names),
                "completed_count": len(plan.expected_names),
                "waiting_count": 0,
                "running_count": 0,
                "result_count": len(plan.expected_names),
                "polls": completion.polls if completion is not None else 0,
                "elapsed_seconds": (
                    completion.elapsed_seconds if completion is not None else 0
                ),
                "active_total": 0,
                "active": [],
                "retry_count": sum(
                    max(0, count - 1) for count in strategy_attempt_counts.values()
                ),
                "retry_reasons": cumulative_retry_reasons,
                "bot_restart_count": bot_restart_count,
                "restart_reasons": dict(sorted(restart_reasons.items())),
                "last_restart_error": last_restart_error,
                "attempt_counts": strategy_attempt_counts,
            }
        )
        dependencies.stop(config)
        bot_started = False
        advance("STOPPED_FOR_CLEANUP")
        inbox_path = None
        _require_complete_verified_reports(plan.expected_names, verified_report_paths)
        inbox_path = capture_verified_inbox(
            config, output, plan, tuple(saved_resume_results), verified_report_paths,
            tester_config_bytes=tester_config_snapshot,
            provenance=provenance,
        )
        advance("INBOX_CAPTURED")
        if not config.preserve_raw_artifacts:
            cleanup_completed_batch(config)
            shutil.rmtree(report_snapshot_dir, ignore_errors=True)
            advance("RAW_ARTIFACTS_REMOVED")
        advance("COMPLETED", inbox_path=inbox_path)
        if completion is None:
            raise RuntimeError("batch completion was not recorded")
        return BatchRunResult(
            output_csv=output,
            state_file=state_file,
            progress_file=progress_file,
            events=tuple(events),
            completion=completion,
            result_rows=len(frame),
            inbox_path=inbox_path,
        )
    except BaseException as error:
        if bot_started:
            try:
                dependencies.stop(config)
                events.append("STOPPED_AFTER_FAILURE")
            except Exception:
                events.append("STOP_AFTER_FAILURE_FAILED")
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
    finally:
        _release_run_lock(run_lock)
