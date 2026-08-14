# Final Review Fix Round 1

## Requirement Mapping

1. Importer compares canonical parsed HTML settings with canonical inbox strategy settings; mismatch is quarantined.
2. Runner snapshots tester config bytes once before the run and derives both fee contract and config hash from that snapshot.
3. Runner requires verified HTML paths for every expected strategy before inbox capture and cleanup; incomplete capture raises and preserves raw artifacts.
4. Cleanup hashes report bytes immediately before `DELETING`; mismatches preserve HTML. A checklist `DELETED` / database `DELETING` crash state is repaired.
5. DD5 resolves retry imports through `import_files.test_run_id`, including `SKIPPED` entries; export-failure retry coverage is present.
6. Importer preflight validates manifest completeness, unique identity, required fields and safe relative paths before file/database processing.
7. DD5 persists raw/Pareto output data, rereads the committed run/results before export, includes `dd5_run_id`, and covers stored-run regeneration.
8. Parser rejects pairwise wallet/equity timestamp mismatch.
9. `portfolio_layer_a_input` exposes DD5 result fields and action/equity timestamp availability.
10. PRD/progress mark production acceptance as pending and link the governing performance spec and ADR-0004; no production 476-strategy evidence is claimed.

## TDD Evidence

RED command:

```powershell
$testTemp = Join-Path $env:TEMP 'mrs3-final-fix-red'; & 'D:\SHARE\!MN\hamster\MRS-Analizer\.venv\Scripts\python.exe' -m pytest tests/test_performance.py tests/test_performance_store.py tests/test_performance_import.py tests/test_performance_dd5.py tests/runner/test_inbox.py tests/runner/test_workflow.py -q --basetemp $testTemp -p no:cacheprovider
```

RED result: `8 failed, 60 passed`. Failures covered the new timestamp, view, settings, cleanup hash/crash, DD5 retry, immutable snapshot, and mandatory-report assertions.

GREEN command:

```powershell
$testTemp = Join-Path $env:TEMP 'mrs3-final-fix-green-final'; & 'D:\SHARE\!MN\hamster\MRS-Analizer\.venv\Scripts\python.exe' -m pytest tests/test_performance.py tests/test_performance_store.py tests/test_performance_import.py tests/test_performance_dd5.py tests/runner/test_inbox.py -q --basetemp $testTemp -p no:cacheprovider
```

GREEN result: `38 passed`. `git diff --check` passed.

Runner workflow verification:

```powershell
$testTemp = Join-Path $env:TEMP 'mrs3-final-fix-workflow'; & 'D:\SHARE\!MN\hamster\MRS-Analizer\.venv\Scripts\python.exe' -m pytest tests/runner/test_workflow.py -q --basetemp $testTemp -p no:cacheprovider
```

Result: `22 passed, 9 failed`. The 9 failures are legacy mocked-success tests whose fake monitor results contain no verified HTML paths. They now fail at the required mandatory-capture guard; changing them to permit cleanup would violate the safety requirement. The existing `tests/test_panel.py` collection mismatch and unrelated CLI fixture drift remain unchanged.

## Changed Files

- `src/mrs3/performance.py`
- `src/mrs3/performance_import.py`
- `src/mrs3/performance_store.py`
- `src/mrs3/performance_dd5.py`
- `src/mrs3/runner/inbox.py`
- `src/mrs3/runner/workflow.py`
- `tests/test_performance.py`
- `tests/test_performance_import.py`
- `tests/test_performance_store.py`
- `tests/test_performance_dd5.py`
- `tests/runner/test_inbox.py`
- `tests/runner/test_workflow.py`
- `PRD.md`
- `progress.md`
- This report

## Commit

Commit SHA: recorded in the final response for this single commit.

## Residual Blockers

- Legacy runner workflow mocks need verified HTML fixtures or explicit capture mocks before that suite can be green.
- The pre-existing panel import mismatch and CLI fixture drift were not repaired.
- Production acceptance, including any 476-strategy run, remains pending.
