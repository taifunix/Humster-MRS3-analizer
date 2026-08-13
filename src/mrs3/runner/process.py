from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Protocol

import psutil

from .config import RunnerConfig
from .http import TesterHttpClient


class ProcessSafetyError(RuntimeError):
    """Raised when a PID cannot be proven to be the configured bot instance."""


class ShutdownClient(Protocol):
    def shutdown(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BotProcess:
    pid: int
    executable_path: Path
    create_time: float
    process: psutil.Process = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class StopResult:
    was_running: bool
    pid: int | None = None
    graceful: bool = False
    forced: bool = False
    killed: bool = False
    shutdown_error: str | None = None


def _same_path(first: Path | str, second: Path | str) -> bool:
    return os.path.normcase(str(Path(first).resolve())) == os.path.normcase(
        str(Path(second).resolve())
    )


def _listener_pids(port: int) -> tuple[int, ...]:
    pids: set[int] = set()
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError) as error:
        raise ProcessSafetyError("cannot inspect TCP listeners") from error
    for connection in connections:
        local = getattr(connection, "laddr", None)
        local_port = getattr(local, "port", None)
        if (
            getattr(connection, "status", None) == psutil.CONN_LISTEN
            and local_port == port
            and getattr(connection, "pid", None) is not None
        ):
            pids.add(int(connection.pid))
    return tuple(sorted(pids))


def resolve_bot_process(config: RunnerConfig) -> BotProcess | None:
    pids = _listener_pids(config.port)
    if not pids:
        return None
    matching: list[BotProcess] = []
    different: list[tuple[int, str]] = []
    for pid in pids:
        try:
            process = psutil.Process(pid)
            executable = Path(process.exe()).resolve()
            created = float(process.create_time())
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError) as error:
            raise ProcessSafetyError(f"cannot verify executable for PID {pid}") from error
        if _same_path(executable, config.executable_path):
            matching.append(BotProcess(pid, executable, created, process))
        else:
            different.append((pid, str(executable)))
    if different:
        details = ", ".join(f"PID {pid}: {path}" for pid, path in different)
        raise ProcessSafetyError(
            f"configured port {config.port} is owned by a different executable ({details})"
        )
    if len(matching) > 1:
        raise ProcessSafetyError(
            f"multiple verified processes listen on configured port {config.port}"
        )
    return matching[0] if matching else None


def _verified_live_process(bot: BotProcess, config: RunnerConfig) -> psutil.Process | None:
    try:
        process = psutil.Process(bot.pid)
        if float(process.create_time()) != bot.create_time:
            raise ProcessSafetyError(f"PID {bot.pid} was reused before shutdown")
        if not _same_path(process.exe(), config.executable_path):
            raise ProcessSafetyError(f"PID {bot.pid} executable changed before shutdown")
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except psutil.AccessDenied as error:
        raise ProcessSafetyError(f"cannot re-verify PID {bot.pid}") from error


def _wait_for_exit(process: psutil.Process, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except psutil.TimeoutExpired:
        return False


def _default_client_factory(config: RunnerConfig) -> ShutdownClient:
    return TesterHttpClient(config.base_url, timeout=config.request_timeout_seconds)


def stop_bot(
    config: RunnerConfig,
    client_factory: Callable[[RunnerConfig], ShutdownClient] = _default_client_factory,
) -> StopResult:
    bot = resolve_bot_process(config)
    if bot is None:
        return StopResult(was_running=False)

    shutdown_error: str | None = None
    endpoint_called = False
    client: ShutdownClient | None = None
    try:
        client = client_factory(config)
        client.shutdown()
        endpoint_called = True
    except Exception as error:
        shutdown_error = f"{type(error).__name__}: {error}"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as error:
                close_error = f"{type(error).__name__}: {error}"
                shutdown_error = (
                    f"{shutdown_error}; close={close_error}"
                    if shutdown_error is not None
                    else f"close={close_error}"
                )

    if _wait_for_exit(bot.process, config.shutdown_timeout_seconds):
        return StopResult(
            was_running=True,
            pid=bot.pid,
            graceful=endpoint_called,
            shutdown_error=shutdown_error,
        )

    process = _verified_live_process(bot, config)
    if process is None:
        return StopResult(
            was_running=True,
            pid=bot.pid,
            graceful=endpoint_called,
            shutdown_error=shutdown_error,
        )
    process.terminate()
    if _wait_for_exit(process, config.shutdown_timeout_seconds):
        return StopResult(
            was_running=True,
            pid=bot.pid,
            forced=True,
            shutdown_error=shutdown_error,
        )

    process = _verified_live_process(bot, config)
    if process is None:
        return StopResult(
            was_running=True,
            pid=bot.pid,
            forced=True,
            shutdown_error=shutdown_error,
        )
    process.kill()
    if not _wait_for_exit(process, config.shutdown_timeout_seconds):
        raise ProcessSafetyError(f"verified bot PID {bot.pid} did not exit after kill")
    return StopResult(
        was_running=True,
        pid=bot.pid,
        forced=True,
        killed=True,
        shutdown_error=shutdown_error,
    )


def _terminate_started_process(process: subprocess.Popen[bytes], timeout: float) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def start_bot(config: RunnerConfig) -> BotProcess:
    if not config.executable_path.is_file():
        raise FileNotFoundError(f"bot executable does not exist: {config.executable_path}")
    existing = resolve_bot_process(config)
    if existing is not None:
        raise ProcessSafetyError(
            f"configured bot is already listening on port {config.port} as PID {existing.pid}"
        )
    command = [str(config.executable_path), *config.bot_args]
    process = subprocess.Popen(command, cwd=config.bot_root)
    deadline = time.monotonic() + config.startup_timeout_seconds
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"bot exited during startup with code {process.returncode}")
            resolved = resolve_bot_process(config)
            if resolved is not None:
                if resolved.pid != process.pid:
                    raise ProcessSafetyError(
                        f"listener PID {resolved.pid} differs from started PID {process.pid}"
                    )
                return resolved
            time.sleep(min(config.poll_interval_seconds, 0.5))
    except Exception:
        _terminate_started_process(process, config.shutdown_timeout_seconds)
        raise
    _terminate_started_process(process, config.shutdown_timeout_seconds)
    raise TimeoutError(
        f"bot did not listen on port {config.port} within {config.startup_timeout_seconds}s"
    )
