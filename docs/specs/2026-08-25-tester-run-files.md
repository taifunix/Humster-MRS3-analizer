# Tester run files from fresh READY candidates

## Goal

Create up to five one-strategy tester snapshots from the filtered, selected
fresh-analysis shortlist. The tester itself remains a manual `run_tester.bat`
operation.

## Contract

- `Generate Run files` uses selected scopes, Phase 2 filters and Tester batch
  dates.
- The server recomputes the shortlist and takes at most five
  `READY_AFTER_FILTERS` candidates in stable candidate-id order.
- Each snapshot is rendered from `Input/run_snapshot_2.json` and has the
  generated strategy name, symbol, timeframe, MRS3 orders, close MA, dates,
  and `max_parallel_runs` from `tester_runner.max_parallel_submissions`.
- Before writing, only ordinary contents of the exact
  `<bot_root>/tester/runs` directory are cleared. Unsafe/symlink targets fail.
- The configured tester JSON is updated to `use_runs: true`.

## Non-goals

The button does not start the tester and does not alter READY JSON generation.

## Acceptance evidence

`.venv\\Scripts\\python.exe -m pytest tests/test_tester_run_files.py tests/test_panel_fresh_strategies.py -q`.
