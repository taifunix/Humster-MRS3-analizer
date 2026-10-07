@echo off
setlocal
set "ROOT=%~dp0.."
"%ROOT%\.venv\Scripts\python.exe" "%~dp0calculate_bybit_base_lots.py" %*
exit /b %ERRORLEVEL%
