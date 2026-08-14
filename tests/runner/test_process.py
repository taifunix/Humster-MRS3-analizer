from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from mrs3.runner.config import RunnerConfig
from mrs3.runner.process import ProcessSafetyError, resolve_bot_process, stop_bot


class FakeProcess:
    def __init__(self, pid: int, executable: Path) -> None:
        self.pid = pid
        self._executable = executable
        self.terminated = False
        self.killed = False

    def exe(self) -> str:
        return str(self._executable)

    def create_time(self) -> float:
        return float(self.pid)

    def is_running(self) -> bool:
        return not (self.terminated or self.killed)

    def status(self) -> str:
        return psutil.STATUS_RUNNING if self.is_running() else psutil.STATUS_ZOMBIE

    def wait(self, timeout: float | None = None) -> int:
        if self.is_running():
            raise psutil.TimeoutExpired(timeout or 0, pid=self.pid)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _config(tmp_path: Path, port: int = 8087) -> RunnerConfig:
    bot = (tmp_path / "hb").resolve()
    return RunnerConfig(
        bot_root=bot,
        executable_path=(bot / "hb_c.exe").resolve(),
        base_url=f"http://127.0.0.1:{port}",
        port=port,
        strategy_dir=(bot / "settings_strategy").resolve(),
        report_dir=(bot / "tester/report/my_test").resolve(),
        wizard_result=(bot / "tester/wizard_result.json").resolve(),
        wizard_progress=(bot / "tester/wizard_progress.json").resolve(),
        tester_config=(bot / "tester/tester_config.json").resolve(),
        inbox_root=(tmp_path / "tester_inbox").resolve(),
        shutdown_timeout_seconds=0.01,
        metric_tolerance=Decimal("0.01"),
    )


def _connection(pid: int, port: int) -> SimpleNamespace:
    return SimpleNamespace(pid=pid, status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=port))


def test_resolves_only_pid_listening_on_configured_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    wanted = FakeProcess(101, config.executable_path)
    other = FakeProcess(202, tmp_path / "other" / "hb_c.exe")
    registry = {101: wanted, 202: other}
    monkeypatch.setattr(
        "mrs3.runner.process.psutil.net_connections",
        lambda kind: [_connection(101, 8087), _connection(202, 8088)],
    )
    monkeypatch.setattr("mrs3.runner.process.psutil.Process", lambda pid: registry[pid])

    resolved = resolve_bot_process(config)

    assert resolved is not None
    assert resolved.pid == wanted.pid


def test_port_owned_by_different_executable_is_a_hard_safety_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    other = FakeProcess(202, tmp_path / "other" / "hb_c.exe")
    monkeypatch.setattr(
        "mrs3.runner.process.psutil.net_connections",
        lambda kind: [_connection(202, 8087)],
    )
    monkeypatch.setattr("mrs3.runner.process.psutil.Process", lambda pid: other)

    with pytest.raises(ProcessSafetyError, match="different executable"):
        resolve_bot_process(config)


def test_fallback_terminates_only_verified_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    wanted = FakeProcess(101, config.executable_path)
    other = FakeProcess(202, config.executable_path)
    registry = {101: wanted, 202: other}
    monkeypatch.setattr(
        "mrs3.runner.process.psutil.net_connections",
        lambda kind: [_connection(101, 8087), _connection(202, 8088)],
    )
    monkeypatch.setattr("mrs3.runner.process.psutil.Process", lambda pid: registry[pid])

    class RefusingClient:
        def shutdown(self) -> None:
            raise OSError("endpoint unavailable")

        def close(self) -> None:
            pass

    result = stop_bot(config, lambda _: RefusingClient())

    assert result.pid == 101
    assert result.forced
    assert wanted.terminated
    assert not other.terminated
    assert not other.killed


@pytest.mark.parametrize("failure", ["factory", "close"])
def test_stop_falls_back_to_verified_process_when_shutdown_client_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    config = _config(tmp_path)
    wanted = FakeProcess(101, config.executable_path)
    monkeypatch.setattr(
        "mrs3.runner.process.psutil.net_connections",
        lambda kind: [_connection(101, 8087)],
    )
    monkeypatch.setattr("mrs3.runner.process.psutil.Process", lambda pid: wanted)

    class BrokenClient:
        def shutdown(self) -> None:
            pass

        def close(self) -> None:
            if failure == "close":
                raise OSError("close failed")

    def factory(_: RunnerConfig) -> BrokenClient:
        if failure == "factory":
            raise OSError("factory failed")
        return BrokenClient()

    result = stop_bot(config, factory)

    assert result.forced
    assert wanted.terminated
    assert failure in (result.shutdown_error or "")
