from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mrs3.bybit_collector.archive import EXPORT_CYCLE_MS
from mrs3.bybit_collector.cli import _test_clock_scale, main


def _config(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    config = tmp_path / "collector.toml"
    config.write_text(
        f'[storage]\nroot = "{root.as_posix()}"\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "INFO"\n',
        encoding="utf-8",
    )
    return config


def test_validate_config_is_read_only(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    assert main(["validate-config", "--config", str(config)]) == 0
    assert "BTCUSDT" in capsys.readouterr().out


def test_test_export_minutes_scales_one_cycle_to_requested_real_minutes() -> None:
    assert _test_clock_scale(5.0) == pytest.approx(EXPORT_CYCLE_MS / 300_000)


def test_test_export_minutes_requires_positive_finite_value() -> None:
    with pytest.raises(ValueError):
        _test_clock_scale(0.0)
    with pytest.raises(ValueError):
        _test_clock_scale(float("inf"))


def test_cli_module_entrypoint_runs_validate_config(tmp_path: Path) -> None:
    config = _config(tmp_path)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "mrs3.bybit_collector.cli", "validate-config", "--config", str(config)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 0
    assert "BTCUSDT" in result.stdout


def test_health_command_reads_existing_snapshot_without_overwriting(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    path = tmp_path / "data" / "status" / "health.json"
    path.parent.mkdir()
    original = {"status": "WARNING", "connected": True}
    path.write_text(json.dumps(original), encoding="utf-8")
    assert main(["health", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out) == original
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_windows_collector_scripts_reject_missing_args_without_prompting() -> None:
    scripts = Path(__file__).parents[1] / "scripts"
    runner = (scripts / "run_bybit_market_collector.ps1").read_text(encoding="utf-8")
    installer = (scripts / "install_bybit_market_collector_task.ps1").read_text(encoding="utf-8")
    command = (scripts / "run_bybit_market_collector.cmd").read_text(encoding="utf-8")

    assert "Mandatory = $false" in runner
    assert "IsNullOrWhiteSpace($Config)" in runner
    assert "GetFullPath($Config)" in runner
    assert "PathType Leaf" in runner
    assert "Mandatory = $true" not in runner
    assert "Mandatory = $false" in installer
    assert "IsNullOrWhiteSpace($Config)" in installer
    assert "GetFullPath($Config)" in installer
    assert "PathType Leaf" in installer
    assert "$projectRoot" not in installer
    assert "Usage: run_bybit_market_collector.cmd CONFIG_PATH" in command
    assert 'if not "%~2"==""' in command
    assert "PROJECT_ROOT" not in command
