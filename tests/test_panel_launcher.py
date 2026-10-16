import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != "nt", reason="requires Windows cmd.exe")
def test_panel_launchers_bind_the_checkout_source_under_cmd(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    scripts = root / "scripts"
    filenames = ("start_panel.bat", "start_new_panel.bat", "restart_new_panel.bat")
    windows_scripts = (
        *filenames,
        "stop_new_panel.bat",
        "calculate_bybit_base_lots.cmd",
        "run_bybit_market_collector.cmd",
        "install_bybit_market_collector_task.ps1",
        "run_bybit_market_collector.ps1",
    )
    for filename in windows_scripts:
        raw = (scripts / filename).read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")

    start_panel = (scripts / "start_panel.bat").read_text(encoding="utf-8").casefold()
    assert "mrs3_panel_source_probe" not in start_panel
    assert "setlocal disableDelayedExpansion".casefold() in start_panel
    assert r'for %%i in ("%~dp0..\src") do set "_mrs3_panel_src=%%~fi"' in start_panel
    assert "if defined pythonpath (" in start_panel
    assert 'set "pythonpath=%_mrs3_panel_src%;%pythonpath%"' in start_panel
    assert 'set "pythonpath=%_mrs3_panel_src%"' in start_panel
    marker = b'if "%MRS3_PANEL_PORT%"==""'
    setup_end = (scripts / "start_panel.bat").read_bytes().index(marker)
    source_setup = (scripts / "start_panel.bat").read_bytes()[:setup_end]
    harness_start = (
        source_setup
        + b'set PYTHONPATH\r\n'
        + b'"%MRS3_PANEL_TEST_PYTHON%" -c "import json, mrs3, mrs3.performance_v2_store as store; print(\'MODULES=\' + json.dumps([mrs3.__file__, store.__file__]))"\r\n'
        + b"exit /b %ERRORLEVEL%\r\n"
    )
    assert b"-m mrs3.cli panel" not in harness_start
    assert b"8766" not in harness_start

    checkout = tmp_path / "checkout & ! with spaces"
    checkout_scripts = checkout / "scripts"
    package = checkout / "src" / "mrs3"
    checkout_scripts.mkdir(parents=True)
    package.mkdir(parents=True)
    for filename in filenames:
        if filename == "start_panel.bat":
            (checkout_scripts / filename).write_bytes(harness_start)
            continue
        original = (scripts / filename).read_bytes()
        if filename == "restart_new_panel.bat":
            shipped = original
            assert original.count(b"timeout /t 2 /nobreak >nul") == 1
            assert original.count(b'start "" /b "%ComSpec%" /c call "%~dp0start_panel.bat"') == 1
            test_copy = shipped.replace(b"timeout /t 2 /nobreak >nul", b"rem timeout").replace(
                b'start "" /b "%ComSpec%" /c call "%~dp0start_panel.bat"',
                b'call "%~dp0start_panel.bat"',
            )
            original = test_copy
        (checkout_scripts / filename).write_bytes(original)
    (checkout_scripts / "stop_new_panel.bat").write_bytes(b"@echo off\r\nexit /b 0\r\n")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "performance_v2_store.py").write_text("", encoding="utf-8")
    for filename in ("start_new_panel.bat", "restart_new_panel.bat"):
        text = (scripts / filename).read_text(encoding="utf-8").casefold()
        assert "setlocal disabledelayedexpansion" in text
        assert "start_panel.bat" in text
        assert "pythonpath" not in text
        assert ".venv\\scripts\\python.exe" not in text
        assert "-m mrs3.cli panel" not in text

    command_processor = os.environ.get("COMSPEC", "cmd.exe")
    for filename in filenames:
        source = str(checkout / "src")
        for inherited in (None, rf"C:\Previous Path;D:\Keep&Me;E:\Bang!;F:\Caret^Path;G:\Percent%Value;{source}"):
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            if inherited is not None:
                environment["PYTHONPATH"] = inherited
            environment.update(
                {
                    "MRS3_PANEL_TEST_PYTHON": sys.executable,
                }
            )
            runner = tmp_path / "invoke_launcher.bat"
            runner.write_text(
                f'@echo off\r\nsetlocal EnableDelayedExpansion\r\ncall "{str(checkout_scripts / filename).replace("!", "^!")}"\r\n',
                encoding="utf-8",
                newline="",
            )
            completed = subprocess.run(
                [command_processor, "/d", "/c", str(runner)],
                cwd=tmp_path,
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert completed.returncode == 0, f"{filename=} {inherited=}: {completed.stdout}{completed.stderr}"

            lines = completed.stdout.splitlines()
            actual_pythonpath = next(line.split("=", 1)[1] for line in lines if line.startswith("PYTHONPATH="))
            expected = source if inherited is None else f"{source};{inherited}"
            assert actual_pythonpath == expected
            assert all(actual_pythonpath.split(os.pathsep))

            module_paths = json.loads(next(line.removeprefix("MODULES=") for line in lines if line.startswith("MODULES=")))
            assert Path(module_paths[0]).resolve() == (package / "__init__.py").resolve()
            assert Path(module_paths[1]).resolve() == (package / "performance_v2_store.py").resolve()
