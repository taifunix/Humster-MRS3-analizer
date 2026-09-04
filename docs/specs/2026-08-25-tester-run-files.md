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
- `Input/run_snapshot_2.json` remains the user-owned RUNS snapshot skeleton;
  only the tester config profile is canonicalized under `templates/tester/`.
  If the skeleton is absent or invalid, generation fails before the RUNS
  directory or tester config is mutated.
- Before writing, only ordinary contents of the exact
  `<bot_root>/tester/runs` directory are cleared. Unsafe/symlink targets fail.
- The canonical `templates/tester/mrs3/config_tester.json` is rendered to the
  configured tester JSON, changing only `StartDate`, `EndDate`,
  `use_runs: true`, `single_mode: false` and `max_parallel_runs`.
- A completed RUNS execution validates every report against the generated
  snapshot manifest and creates a verified inbox eligible for Performance DB
  import and guarded HTML cleanup.

## Non-goals

The button does not alter READY JSON generation.

## Acceptance evidence

`.venv\\Scripts\\python.exe -m pytest tests/test_tester_run_files.py
tests/runner/test_inbox.py tests/test_panel_fresh_strategies.py -q`.
