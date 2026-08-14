# Verified Tester Report Library And Fast Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile tester HTML into an auditable ONUSDT report library and make snapshot identity lookup lightweight.

**Architecture:** Add a focused runner report-library module that accepts live HTML plus wizard results, validates each candidate through existing `reconcile_results`, and publishes one immutable named copy plus manifest. Split identity extraction from full HTML parsing so the snapshot collector reads only the embedded settings name while final reconciliation remains unchanged.

**Tech Stack:** Python 3.13, stdlib JSON/hashlib/shutil, lxml, pytest.

## Global Constraints

- Library path is a sibling of configured `tester/report/my_test` named `ONUSDT_reports`.
- Never accept HTML-only completion; require a matching one-strategy wizard result and full reconciliation.
- Remove a live HTML only when SHA-256 matches a verified library copy and manifest says `safe_to_delete=YES`.
- Preserve existing saved report and snapshot evidence.

---

### Task 1: Fast Strategy Identity

**Files:**
- Modify: `src/mrs3/runner/results.py`
- Modify: `src/mrs3/runner/monitor.py`
- Test: `tests/runner/test_monitor.py`

**Interfaces:**
- Produces `extract_html_strategy_name(path: Path) -> str | None`.
- `_ReportSnapshotCollector` calls `extract_html_strategy_name` instead of `parse_html_report`.

- [ ] Write a failing test that makes `parse_html_report` raise and proves the collector can snapshot a report whose lightweight embedded settings name is `A`.
- [ ] Run `pytest tests/runner/test_monitor.py -q` and confirm the new test fails because the collector still invokes full parsing.
- [ ] Implement `extract_html_strategy_name` using only the embedded settings payload; return `None` for incomplete or invalid data.
- [ ] Switch `_report_strategy_name` to the new helper without changing full reconciliation.
- [ ] Run `pytest tests/runner/test_monitor.py -q` and confirm all monitor tests pass.

### Task 2: Verified Report Library

**Files:**
- Create: `src/mrs3/runner/report_library.py`
- Test: `tests/runner/test_report_library.py`

**Interfaces:**
- Produces `publish_verified_reports(config, results, report_paths) -> ReportLibraryAudit`.
- `ReportLibraryAudit` exposes accepted, duplicate, conflict, quarantine and safe-to-delete records.

- [ ] Write failing tests for a reconciled report copied as `<strategy>.html` with SHA-256 manifest evidence.
- [ ] Write failing tests that reject an HTML whose embedded name or metrics disagree with its wizard result.
- [ ] Write failing tests that mark a byte-identical live duplicate safe to delete and retain conflicting content.
- [ ] Run `pytest tests/runner/test_report_library.py -q` and confirm the new tests fail before the module exists.
- [ ] Implement minimal library publication with atomic manifest writes and no deletion function.
- [ ] Run `pytest tests/runner/test_report_library.py -q` and confirm all library tests pass.

### Task 3: Controlled Cleanup, CLI, And Runner Integration

**Files:**
- Modify: `src/mrs3/runner/workflow.py`
- Modify: `tests/runner/test_workflow.py`
- Modify: `src/mrs3/cli.py`
- Modify: `progress.md`

**Interfaces:**
- Successful batch publication calls `publish_verified_reports` before live report cleanup.
- Only manifest entries marked `safe_to_delete=YES` are removed from `my_test`.
- `tester-report-library` defaults to a read-only audit and requires `--apply`
  to publish the existing live reports.

- [ ] Write a failing workflow test proving verified duplicate live HTML is removed only after library manifest publication.
- [ ] Write a failing workflow test proving an unverified or conflicting HTML remains in the live directory.
- [ ] Write a failing CLI test proving the existing report audit is read-only unless `--apply` is supplied.
- [ ] Run focused workflow tests and confirm they fail before integration.
- [ ] Integrate library publication after full reconciliation and before cleanup.
- [ ] Update `progress.md` with the library path, audit command, and no-delete safety condition.
- [ ] Run `pytest tests/runner/test_monitor.py tests/runner/test_report_library.py tests/runner/test_workflow.py -q`, `git diff --check`, and independent review.
