# Task 4 Report

## Requirement Checklist

- [x] Added `run_performance_dd5(database, import_id, output_dir, config)` and `PerformanceDd5Artifacts`.
- [x] Reads committed performance rows through a read-only DuckDB connection.
- [x] Reuses existing DD5 normalization/comparison and workbook writers.
- [x] Persists `dd5_runs` and `dd5_results` in one transaction.
- [x] Exports `posttest.xlsx`, CSV sheets, and a `CALCULATION_ONLY` manifest.
- [x] Added a separate panel `performance-dd5` action that validates a complete inbox.
- [x] CLI orchestration is import -> DD5 export -> cleanup; cleanup is not called after a failed export.
- [x] Preserved the legacy CSV `posttest` command and panel controls.
- [x] No tick test, MRS2 changes, or dependencies added.

## TDD Evidence

RED command:

```powershell
$testTemp = Join-Path $env:TEMP 'mrs3-task4-red'; & 'D:\SHARE\!MN\hamster\MRS-Analizer\.venv\Scripts\python.exe' -m pytest tests/test_performance_dd5.py tests/test_panel.py tests/test_cli.py -q --basetemp $testTemp -p no:cacheprovider
```

RED result: collection failed as expected because `mrs3.performance_dd5` did not yet exist. The same command also exposed a pre-existing panel-suite import mismatch: `_normalise_tester_log_line` is absent from the base `panel.py`.

Focused GREEN command:

```powershell
$testTemp = Join-Path $env:TEMP 'mrs3-task4-dd5-focused'; & 'D:\SHARE\!MN\hamster\MRS-Analizer\.venv\Scripts\python.exe' -m pytest tests/test_performance_dd5.py tests/test_cli.py::test_performance_dd5_cli_imports_calculates_then_cleans_up -q --basetemp $testTemp -p no:cacheprovider
```

Focused GREEN result: `2 passed`.

Relevant run:

```powershell
$testTemp = Join-Path $env:TEMP 'mrs3-task4-green-no-panel'; & 'D:\SHARE\!MN\hamster\MRS-Analizer\.venv\Scripts\python.exe' -m pytest tests/test_performance_dd5.py tests/test_performance_import.py tests/test_posttest.py tests/test_cli.py -q --basetemp $testTemp -p no:cacheprovider
```

Relevant result: `26 passed, 6 failed`; the 6 failures are existing CLI fixture/contract drift (`algorithm_version` and missing `tester_config`/`inbox_root`), unrelated to Task 4. The mandated full command cannot collect `tests/test_panel.py` because of the pre-existing missing `_normalise_tester_log_line` import. `git diff --check` passed.

## Modified Files

- `src/mrs3/performance_dd5.py`
- `src/mrs3/cli.py`
- `src/mrs3/panel.py`
- `tests/test_performance_dd5.py`
- `tests/test_cli.py`
- `tests/test_panel.py`
- `progress.md`
- This report

## Commit

Commit SHA: af05348 (amended final SHA will be recorded by Git after report inclusion)

## Concerns

- The repository baseline has unrelated CLI fixture drift and a panel test collection mismatch; those files were not repaired because doing so would exceed Task 4 scope.
- The legacy CSV posttest path remains separate from the new DuckDB DD5 action.
