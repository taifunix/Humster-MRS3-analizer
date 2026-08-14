# Integration Report

**Date:** 2026-08-14
**Worktree:** `D:\SHARE\!MN\hamster\MRS-Analizer-integration`
**Branch:** `integrate/performance-with-main`
**Main source:** `D:\SHARE\!MN\hamster\MRS-Analizer`
**Common base:** `ea33ebb`

## Transferred tracked changes

From dirty main:

- `.gitignore`
- `PRD.md`
- `config.example.json`
- `config.local.json.example`
- `progress.md`
- `docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md`
- `docs/specs/v07-event-filter-and-shortlist.md`
- `src/mrs3/analysis_exports.py`
- `src/mrs3/analysis_storage.py`
- `src/mrs3/duckdb_direct.py`
- `src/mrs3/duckdb_events.py`
- `src/mrs3/duckdb_import.py`
- `src/mrs3/eligibility.py`
- `src/mrs3/pipeline.py`
- `src/mrs3/posttest.py`
- `src/mrs3/published_surface.py`
- `src/mrs3/runner/monitor.py`
- `src/mrs3/runner/results.py`
- `src/mrs3/runner/workflow.py`
- `src/mrs3/selection.py`
- `tests/runner/test_monitor.py`
- `tests/runner/test_results.py`
- `tests/runner/test_workflow.py`
- `tests/test_analysis_exports.py`
- `tests/test_analysis_storage.py`
- `tests/test_duckdb_direct.py`
- `tests/test_duckdb_import.py`
- `tests/test_eligibility.py`
- `tests/test_panel.py`
- `tests/test_pipeline.py`
- `tests/test_posttest.py`
- `tests/test_published_surface.py`
- `tests/test_selection.py`
- `tests/test_source_packs.py`

## Transferred allowed untracked files

- `docs/specs/2026-08-14-panel-multiscope-strategy-generation.md`
- `docs/specs/2026-08-14-single-strategy-html-collision-repair.md`
- `docs/specs/2026-08-14-tester-name-only-verification.md`
- `docs/specs/2026-08-14-tester-report-library-and-fast-identity.md`
- `docs/superpowers/plans/2026-08-12-real-events-phase2-filter.md`
- `docs/superpowers/plans/2026-08-13-posttest-sequential-selection.md`
- `docs/superpowers/plans/2026-08-13-posttest-workbook-layout.md`
- `docs/superpowers/plans/2026-08-13-representative-and-shortlist-ui.md`
- `docs/superpowers/plans/2026-08-13-shortlist-scope-summary.md`
- `docs/superpowers/plans/2026-08-13-tester-html-collision-lanes.md`
- `docs/superpowers/plans/2026-08-14-tester-report-library-and-fast-identity.md`
- `docs/superpowers/specs/2026-08-13-posttest-sequential-selection-design.md`
- `docs/superpowers/specs/2026-08-13-shortlist-scope-summary-design.md`
- `src/mrs3/analysis_filter_export.py`
- `src/mrs3/analysis_shortlist.py`
- `src/mrs3/analysis_strategies.py`
- `tests/runner/test_report_library.py`
- `tests/test_analysis_filter_export.py`
- `tests/test_analysis_shortlist.py`
- `tests/test_analysis_strategies.py`

Integration-only verification files:

- `docs/superpowers/plans/2026-08-14-performance-with-main-integration.md`
- `tests/test_integration_contract.py`
- `src/mrs3/runner/report_library.py`
- this report

`src/mrs3/runner/report_library.py` was added because the transferred
report-library test/spec had no corresponding production module in dirty main.
It implements only verified publication, identical-duplicate cleanup, conflict
retention, and audit manifest evidence required by that test/spec. It is not
wired into runner scheduling or a CLI command.

## Conflicts and resolutions

- `src/mrs3/panel.py`: main's panel is the semantic base, retaining v2 real
  independent events, Phase 2 scope/filter controls, READY-only strategy
  generation, workflow defaults, and tester lifecycle UI. Performance behavior
  was re-added: `performance-dd5` action routing, strict manifest/entry/hash/
  commission-contract validation, and the Performance DuckDB card. Its defaults
  are `data/databases/strategy_performance.duckdb` and `data/tester_inbox`;
  selecting a completed batch subdirectory remains required for validation.
- `src/mrs3/runner/workflow.py`: performance's immutable tester-config snapshot,
  complete verified inbox capture before cleanup, readback boundary, and
  cleanup ordering were retained. Main's `ResultMismatchError` parsing now
  converts embedded HTML-name mismatches into `BatchHtmlCollision` for the
  existing single-strategy collision repair lane. Name-only verification remains
  distinct from Performance DuckDB evidence parsing.
- `config.example.json` and `config.local.json.example`: both now contain main
  runner tuning (`max_parallel_submissions`, `strategy_batch_size`,
  `max_strategy_attempts`, `max_bot_restarts`, `submission_delay_seconds`,
  `result_report_grace_seconds`) and performance `tester_config`/`inbox_root`.
- `PRD.md` and `progress.md`: retained the performance evidence-store status and
  added main's real-event, multi-scope, name-only verification, and collision
  recovery state. The integration branch is recorded as the current branch.
- Overlapping stale tests were aligned with the integrated v4 schema, automatic
  real-event shift discovery, raw-log artifact, calculation-only DD5 dashboard,
  and name-only runner behavior. Performance inbox-safety tests remain present.

## Verification

- Red integration tests: `3 failed` as expected before implementation.
- Focused integration green tests: `3 passed`.
- Panel/runner regression suite:
  `py -m pytest -q --basetemp C:\tmp\mrs3-integration-focused3 tests/test_panel.py tests/runner/test_config.py tests/runner/test_files.py tests/runner/test_monitor.py tests/runner/test_workflow.py`
  Result: `130 passed, 1 skipped`.
- Report-library focused suite:
  `py -m pytest -q --basetemp C:\tmp\mrs3-integration-library2 tests/runner/test_report_library.py`
  Result: `2 passed`.
- Real-event/Phase 2/DuckDB/posttest/selection/source-pack suite:
  `py -m pytest -q --basetemp C:\tmp\mrs3-integration-analysis2 tests/test_integration_contract.py tests/test_analysis_exports.py tests/test_analysis_storage.py tests/test_analysis_filter_export.py tests/test_analysis_shortlist.py tests/test_analysis_strategies.py tests/test_duckdb_direct.py tests/test_duckdb_import.py tests/test_eligibility.py tests/test_pipeline.py tests/test_posttest.py tests/test_published_surface.py tests/test_selection.py tests/test_source_packs.py tests/runner/test_results.py tests/runner/test_report_library.py`
  Result: `249 passed, 1 skipped`.
- Combined focused evidence: `379 passed, 2 skipped`.
- `py -m compileall -q src tests`: passed.
- JSON parsing of both config examples: passed.
- `git diff --check`: passed.

The two skips are Windows symlink-permission tests (`WinError 1314`). No full
476-strategy tester run or production acceptance claim was made.

## Excluded artifact roots

Not transferred, modified, or staged:

- `Input/`
- `posttest_long/`
- `posttest_holding_long/`
- `results/`
- `data/`
- generated reports, local databases, raw HTML, logs, XLSX/CSV outputs, and
  other generated artifacts

The final worktree audit showed no status entries under any excluded root.
