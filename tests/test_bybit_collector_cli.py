from __future__ import annotations

import json
from pathlib import Path

from mrs3.bybit_collector.cli import main


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
