@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
if "%MRS3_PANEL_PORT%"=="" set "MRS3_PANEL_PORT=8765"

if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python 3.11 or newer is required. Install Python and enable the py launcher.
    goto error
  )
  echo Creating the local Python environment...
  py -3 -m venv .venv
  if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -c "import mrs3, pandas, openpyxl, lxml, httpx, psutil" >nul 2>nul
if errorlevel 1 (
  echo Installing MRS3 and dependencies...
  ".venv\Scripts\python.exe" -m pip install -e .
  if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -m mrs3.cli panel --config config.local.json --port %MRS3_PANEL_PORT%
if errorlevel 1 goto error
exit /b 0

:error
echo.
echo MRS3 Control Panel could not start. Read the error above.
pause
exit /b 1

