@echo off
setlocal
set "MRS3_PANEL_ROOT=static"
set "MRS3_PANEL_PORT=%~1"
if "%MRS3_PANEL_PORT%"=="" set "MRS3_PANEL_PORT=8766"
timeout /t 2 /nobreak >nul
start "" /b "%ComSpec%" /c call "%~dp0start_panel.bat"
