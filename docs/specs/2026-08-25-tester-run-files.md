# Tester run files from fresh READY candidates

## Goal

Create one one-strategy tester snapshot for every filtered, selected
fresh-analysis shortlist candidate. The panel starts `run_tester.bat` when the
operator explicitly chooses the RUNS action.

## Contract

- `Generate Run files` uses selected scopes, Phase 2 filters and Tester batch
  dates.
- The server recomputes the shortlist and takes every selected
  `READY_AFTER_FILTERS` candidate in stable candidate-id order.
- Each snapshot is rendered from `Input/run_snapshot_2.json` and has the
  generated strategy name, symbol, timeframe, MRS3 orders, close MA, dates,
  and `max_parallel_runs` from `tester_runner.max_parallel_submissions`.
- Before writing, only ordinary contents of the exact
  `<bot_root>/tester/runs` directory are cleared. Unsafe/symlink targets fail.
- The configured tester JSON is updated to `use_runs: true`.
- A completed RUNS execution validates every report against the generated
  snapshot manifest and creates a verified inbox eligible for Performance DB
  import and guarded HTML cleanup.

## Non-goals

The button does not alter READY JSON generation.

## Acceptance evidence

`.venv\\Scripts\\python.exe -m pytest tests/test_tester_run_files.py
tests/runner/test_inbox.py tests/test_panel_fresh_strategies.py -q`.
