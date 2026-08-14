# Performance With Main Integration Plan

**Goal:** Integrate the dirty main working-tree implementation into the performance branch while preserving immutable tester evidence capture, real-event Phase 2 workflow, collision recovery, and Performance DuckDB DD5 behavior.

**Architecture:** Apply main's tracked working-tree delta from the shared `ea33ebb` base, then resolve semantic overlaps manually. Main owns real-event analysis and runner lifecycle behavior; performance owns immutable tester-config snapshots, complete inbox capture before cleanup, and Performance DuckDB DD5 validation/UI.

**Scope:** Work only in `D:\SHARE\!MN\hamster\MRS-Analizer-integration` on `integrate/performance-with-main`. Transfer tracked main changes and untracked code/tests/docs/config files only. Exclude `Input/`, `posttest_long/`, `posttest_holding_long/`, `results/`, `data/`, and generated reports/artifacts.

## Tasks

- [ ] Capture main's tracked patch without modifying main.
- [ ] Copy only allowed untracked source, test, documentation, and config files.
- [ ] Add focused failing tests for complete inbox capture, tester-config snapshot reuse, real-event workflow/collision recovery, Performance DuckDB paths, and config example unions where coverage is missing.
- [ ] Resolve `panel.py` by retaining main's real-event Phase 2/strategy workflow and performance's DD5 action validation/defaults.
- [ ] Resolve `runner/workflow.py` by retaining main's collision recovery and performance's immutable config snapshot, complete inbox capture, and cleanup ordering.
- [ ] Update both config examples with main runner tuning plus performance `tester_config` and `inbox_root`; use `data/databases/strategy_performance.duckdb` and `data/tester_inbox` UI defaults.
- [ ] Run focused relevant tests, `git diff --check`, inspect staged scope, and complete an independent review of the diff.
- [ ] Write `.superpowers/sdd/2026-08-14-strategy-performance-duckdb/integration-report.md` with transferred files, conflicts/resolutions, test evidence, and excluded roots.
- [ ] Create one scoped conventional integration commit in this worktree only.
