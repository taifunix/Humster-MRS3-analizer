@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "ROOT=%~dp0.."
"%ROOT%\.venv\Scripts\python.exe" "%~dp0download_bingx_data.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%
