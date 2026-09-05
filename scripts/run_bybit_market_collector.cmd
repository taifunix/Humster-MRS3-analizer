@echo off
setlocal
if "%~1"=="" (
    echo Usage: run_bybit_market_collector.cmd CONFIG_PATH 1>&2
    exit /b 2
)
if not "%~2"=="" (
    echo Exactly one config path is accepted. 1>&2
    exit /b 2
)
if not exist "%~1" (
    echo Config file does not exist: %~1 1>&2
    exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_bybit_market_collector.ps1" -Config "%~1"
exit /b %ERRORLEVEL%
