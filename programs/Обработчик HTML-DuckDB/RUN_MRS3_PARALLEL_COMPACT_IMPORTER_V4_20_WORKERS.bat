@echo off
setlocal
title MRS3 Parallel Compact HTML Importer v4

rem Only change this number if monitoring shows throttling or disk saturation.
set "WORKERS=30"
set "REPORT_DIR=%~dp0"
set "HTML_DIR=%REPORT_DIR%my_test1"
set "DATABASE=%REPORT_DIR%mrs3_parallel_compact_v4.duckdb"
set "AUDIT_DIR=%REPORT_DIR%mrs3_import_audit_v4"
set "IMPORTER=%REPORT_DIR%mrs3_html_parallel_compact_importer_v4.py"
set "CODEC=%REPORT_DIR%mrs3_html_compact_importer_v3.py"

echo ================================================================
echo MRS3 Parallel Compact HTML Importer v4
echo Workers: %WORKERS%  ^|  one safe DuckDB writer
echo Creates only: mrs3_parallel_compact_v4.duckdb
echo ================================================================

if not exist "%IMPORTER%" (
    echo ERROR: v4 importer was not found:
    echo "%IMPORTER%"
    pause
    exit /b 2
)

if not exist "%CODEC%" (
    echo ERROR: required compact codec was not found:
    echo "%CODEC%"
    echo Copy mrs3_html_compact_importer_v3.py into this same folder.
    pause
    exit /b 3
)

if not exist "%HTML_DIR%\" (
    echo ERROR: HTML folder was not found:
    echo "%HTML_DIR%"
    pause
    exit /b 4
)

py -c "import duckdb, lxml" >nul 2>nul
if errorlevel 1 (
    echo Installing Python packages duckdb and lxml once...
    py -m pip install duckdb lxml
    if errorlevel 1 (
        echo ERROR: package installation failed.
        pause
        exit /b 5
    )
)

py "%IMPORTER%" ^
  --html-dir "%HTML_DIR%" ^
  --database "%DATABASE%" ^
  --audit-dir "%AUDIT_DIR%" ^
  --workers %WORKERS% ^
  --progress-every 10 ^
  --batch-size 250

if errorlevel 1 (
    echo.
    echo IMPORT FAILED. Do not delete HTML files.
    pause
    exit /b 6
)

echo.
echo Import finished. Check these files before deleting any HTML:
echo "%AUDIT_DIR%\import_manifest.json"
echo "%AUDIT_DIR%\html_delete_checklist.csv"
pause
