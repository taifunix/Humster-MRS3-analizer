# Tester Runner Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make interrupted and restarted tester runs deterministic, observable, leak-free, and resumable for large configured chunks.

**Architecture:** Keep the existing workflow and monitor boundaries. Move expensive resume hydration behind an early bot stop, make collector lifetime exception-safe, recover from persisted snapshots at the workflow boundary, and retain cumulative diagnostics in progress.

**Tech Stack:** Python 3.13, pytest, httpx, existing JSON sidecars and panel progress.

## Global Constraints

- Do not launch the real tester during automated verification.
- Do not delete or rewrite existing HTML reports or DuckDB files.
- `strategy_batch_size=250` and `max_parallel_submissions=35` remain supported local values.
- All behavior changes require a failing regression test before production edits.

---

### Task 1: Stop Before Resume Hydration

**Files:**
- Modify: `src/mrs3/runner/workflow.py`
- Test: `tests/runner/test_workflow.py`

**Interfaces:**
- Consumes: `plan_batch(..., hydrate_resume=False)` and `WorkflowDependencies.stop`.
- Produces: `run_batch` ordering `light plan -> progress -> stop -> hydrated plan`.

- [x] Write a test recording plan/stop ordering and asserting fresh progress exists before hydration.
- [x] Run the focused test and confirm it fails because hydration currently runs first.
- [x] Implement the minimal ordering change.
- [x] Run the focused test and existing resume tests.

### Task 2: Exception-Safe Snapshot Collector

**Files:**
- Modify: `src/mrs3/runner/monitor.py`
- Test: `tests/runner/test_monitor.py`

**Interfaces:**
- Consumes: `_ReportSnapshotCollector.start()`.
- Produces: exactly one matching `close()` on every monitor exit.

- [x] Write a test whose client raises a transport error after collector start.
- [x] Confirm the collector remains open with current code.
- [x] Wrap monitor lifetime in `try/finally` and close once.
- [x] Run all monitor tests.

### Task 3: Recover Snapshots Before Restart

**Files:**
- Modify: `src/mrs3/runner/workflow.py`
- Test: `tests/runner/test_workflow.py`

**Interfaces:**
- Consumes: `_load_snapshot_report_paths(output, batch_names)`.
- Produces: validated/persisted results removed from `batch_names` before restart.

- [x] Write a restart test where direct report paths fail but a captured snapshot validates.
- [x] Confirm the strategy is currently resubmitted.
- [x] Merge snapshot paths before validation and persist recovered results.
- [x] Run workflow restart/resume tests.

### Task 4: Cumulative Restart Diagnostics

**Files:**
- Modify: `src/mrs3/runner/workflow.py`
- Test: `tests/runner/test_workflow.py`

**Interfaces:**
- Produces progress fields `last_restart_error`, `restart_reasons`, cumulative `retry_count`, and `retry_reasons`.

- [x] Write a two-restart test with distinct recoverable errors.
- [x] Confirm current progress loses reasons and resets retry counters.
- [x] Accumulate monitor counters and restart reasons at workflow scope.
- [x] Run workflow and panel progress tests.

### Task 5: Verification and Documentation

**Files:**
- Modify: `progress.md`

- [x] Run runner monitor/config/http/workflow tests with a workspace basetemp.
- [x] Run panel tester-plan/progress tests.
- [ ] Run `compileall` and `git diff --check`.
- [x] Perform independent review and record confirmed residual risks.

### Task 6: Remove In-place HTML Archiving

**Files:**
- Modify: `src/mrs3/runner/workflow.py`
- Test: `tests/runner/test_workflow.py`

- [x] Remove the callable which renames report HTML to `*.html.saved`.
- [x] Keep read-only compatibility with already archived evidence for this interrupted run.
- [x] Verify no production path creates `.html.saved` files.
