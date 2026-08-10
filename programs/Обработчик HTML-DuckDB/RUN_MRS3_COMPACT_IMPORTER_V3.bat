@echo off
setlocal
title MRS3 Compact HTML Importer v3

set "REPORT_DIR=%~dp0"
set "HTML_DIR=%REPORT_DIR%my_test1"
set "DATABASE=%REPORT_DIR%mrs3_compact_v3.duckdb"
set "AUDIT_DIR=%REPORT_DIR%mrs3_import_audit_v3"
set "IMPORTER=%REPORT_DIR%mrs3_html_compact_importer_v3.py"

echo ================================================================
echo MRS3 Compact HTML Importer v3
echo This launcher creates only: mrs3_compact_v3.duckdb
echo ================================================================

if not exist "%IMPORTER%" (
    echo ERROR: v3 importer was not found:
    echo "%IMPORTER%"
    pause
    exit /b 2
)

if not exist "%HTML_DIR%\" (
    echo ERROR: HTML folder was not found:
    echo "%HTML_DIR%"
    pause
    exit /b 3
)

py -c "import duckdb, lxml" >nul 2>nul
if errorlevel 1 (
    echo Installing Python packages duckdb and lxml once...
    py -m pip install duckdb lxml
    if errorlevel 1 (
        echo ERROR: package installation failed.
        pause
        exit /b 4
    )
)

py "%IMPORTER%" ^
  --html-dir "%HTML_DIR%" ^
  --database "%DATABASE%" ^
  --audit-dir "%AUDIT_DIR%" ^
  --progress-every 10 ^
  --batch-size 250

if errorlevel 1 (
    echo.
    echo IMPORT FAILED. Do not delete HTML files.
    pause
    exit /b 5
)

echo.
echo Import finished. Check these files before deleting any HTML:
echo "%AUDIT_DIR%\import_manifest.json"
echo "%AUDIT_DIR%\html_delete_checklist.csv"
pause
