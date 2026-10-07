@echo off
setlocal
set "MRS3_PANEL_ROOT=static"
set "MRS3_PANEL_PORT=8766"
for %%I in ("%~dp0..\src") do set "PYTHONPATH=%%~fI"
call "%~dp0start_panel.bat"
