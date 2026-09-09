# Performance v2 Global Finalist Retest Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Retest the current effective FINALIST strategies across all pairs, optionally include RESERVE, rank only the exact successful retest cohort, and round-trip one combined control XLSX.

**Architecture:** Add one small coordinator around the existing Performance v2 RETEST, SINGLE_MODE, IMPORT & REPLACE, selection, and review paths. Keep tag-driven RETEST and ordinary pair selection unchanged. Reuse schema v4: cohort lineage lives in canonical request metadata and existing selection rows; all combined review writes use one transaction.

**Tech Stack:** Python 3, DuckDB, pandas, openpyxl, existing Panel HTTP/JavaScript, pytest.

**Spec:** `docs/specs/2026-09-09-performance-v2-finalist-retest-control.md`

## Global Constraints

- One `gpt-5.6-luna` executor at `xhigh`; tasks run strictly sequentially.
- Use TDD: every behavior starts with a focused failing test and ends with its focused suite green.
- Use `.venv\Scripts\python.exe -m pytest`; never system Python or pytest.
- Do not add a database schema migration, dependency, parallel status system, or client-supplied cohort IDs.
- Preserve unrelated working-tree changes and existing tag-driven RETEST and per-pair selection behavior.
- Tester work stays on the existing authorized local path and fixtures/fakes used by tests.

---

### Task 1: Canonical cohort provenance

**Files:**

- [x] Create `src/mrs3/performance_v2_finalist_retest.py`.
- [x] Create `tests/test_performance_v2_finalist_retest.py`.
- [x] Modify `src/mrs3/performance_v2_selection_review.py` only to reuse its canonical JSON contract.

**Behavior:** Canonical digests use sorted UTF-8 JSON, stable integer/Decimal/UTC date encodings, sorted members, and exclude timestamps and local paths. A per-run review key is derived deterministically from the actual workbook hash and selection run ID. Existing schema v4 fields remain sufficient.

**Checkpoint:**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_finalist_retest.py -q
```

### Task 2: Freeze the exact effective cohort and render the manifest

**Files:**

- [x] Modify `src/mrs3/performance_v2_finalist_retest.py`.
- [x] Modify `src/mrs3/performance_v2_selection_review.py`.
- [x] Modify `src/mrs3/performance_v2_retest.py`.
- [x] Modify `tests/test_performance_v2_finalist_retest.py` and `tests/test_performance_v2_retest.py`.

**Behavior:** Resolve current effective FINALIST, or FINALIST plus RESERVE. Freeze Strategy/Result IDs, effective status/rank, typed strategy parameters, scope, dates, listing evidence, JSON/config/cohort digests. Reuse `LISTING_WARMUP_HOURS`; compute `effective_start=max(requested_start, listing_date+warmup)` and one common end. Missing/invalid listing, empty effective period, and invalid strategy source become typed exclusions and never become runnable/rankable. Fixed test defaults are balance 1000, balance percentages 100, risks 1, and stored lots/MA/shifts. Reject more than 10,000 members before publication. Existing tag-based manifest behavior remains compatible.

**Checkpoint:**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_finalist_retest.py tests/test_performance_v2_retest.py -q
```

### Task 3: Failure-isolated IMPORT & REPLACE

**Files:**

- [x] Modify `src/mrs3/performance_v2_finalist_retest.py`.
- [x] Modify `src/mrs3/performance_v2_import.py` and `src/mrs3/panel.py`.
- [x] Modify `tests/test_performance_v2_import.py` and `tests/test_panel_performance_v2_retest.py`.

**Behavior:** Reuse SINGLE_MODE and validate the committed inbox by tester job, cohort, manifest and config digests plus exact strategy names. Compare each current Result ID with its frozen value before replacing it. Commit successful replacements per member; failed, divergent, missing, rejected, or unvisited members keep their prior result/action/equity/window facts. Persist exact successful Strategy/new Result pairs and typed failures in redacted terminal job metadata. Active duplicates are refused; terminal success replays the stored outcome; terminal all-failure is not reused and deliberate retry creates a fresh freeze after revalidation. No status, rank, comment, REJECTED tag, or explicit RETEST tag changes.

**Checkpoint:**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_import.py tests/test_performance_v2_finalist_retest.py tests/test_panel_performance_v2_retest.py -q
```

### Task 4: Exact RETEST_COHORT cache and ranking

**Files:**

- [x] Modify `src/mrs3/performance_v2_selection.py`.
- [x] Modify `src/mrs3/performance_v2_selection_review.py`, `src/mrs3/performance_v2_finalist_retest.py`, and `src/mrs3/panel.py`.
- [x] Modify `tests/test_performance_v2_selection.py`, `tests/test_performance_v2_finalist_retest.py`, and `tests/test_panel_performance_v2.py`.

**Behavior:** Resolve the exact successful Strategy/Result pairs solely from a completed bulk job. Reject browser strategy/result/cohort overrides. Restrict cache status/warming/loading and every auxiliary population, percentile, analog, counter, filter, score, rank, and Top N to those IDs; keep `run_selection` formulas unchanged. Verify current Result IDs before cache work and again before persistence. Any post-success divergence fails the whole request with `RETEST_COHORT_STALE_RESULTS` and no writes. Zero successes fail before cache work with `RETEST_COHORT_NO_SUCCESSFUL_MEMBERS`. Persist scope/job/digests/exact IDs in existing selection request metadata. Ordinary pair selection remains full-population.

**Checkpoint:**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_performance_v2_finalist_retest.py tests/test_panel_performance_v2.py -q
```

### Task 5: Combined atomic control workbook

**Files:**

- [x] Modify `src/mrs3/performance_v2_selection.py`.
- [x] Modify `src/mrs3/performance_v2_selection_review.py` and `src/mrs3/performance_v2_finalist_retest.py`.
- [x] Modify their focused tests.

**Behavior:** Emit fixed ordered sheets `Candidates`, `Groups`, `Retest Failures`, and very-hidden metadata. Include each successful member once and all typed exclusions/failures. Preserve current User Status; leave User Rank blank after retest. Build bytes first, then persist every group run atomically with the same actual workbook hash. Import validates exact sheets/columns/order/rowset, immutable fields/digest, database/cohort/current results/latest runs, formulas, statuses, local ranks, analogs, comments and ZIP limits before one transaction. Blank/gapped ranks are valid; duplicate nonblank ranks are rejected within a group only. Deterministic per-run review keys allow all group reviews in existing tables. Reimport is idempotent; any error writes nothing. Outside-scope decisions remain unchanged; deleting an issued row/group is tampering.

**Checkpoint:**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_performance_v2_selection_review.py tests/test_performance_v2_finalist_retest.py -q
```

### Task 6: Panel API and UI

**Files:**

- [x] Modify `src/mrs3/panel.py`.
- [x] Modify `src/mrs3/panel_web/index.html`, `src/mrs3/panel_web/app.js`, and existing CSS only if needed.
- [x] Modify `tests/test_panel_performance_v2_retest.py`, `tests/test_panel_performance_v2.py`, and `tests/test_panel_static_ui.py`.

**Behavior:** Add bulk finalist start/status/import, combined XLSX download and combined XLSX import beside existing CHECK & RETEST controls. Add adjacent `Включая RESERVE`, editable dates, frozen counts, progress, successes and typed failures. Default end is UTC today minus two days; default start is the oldest cohort listing. Exact successful replay stays idempotent; fully failed retry creates a fresh attempt; concurrent double-click is refused. A completed zero-success attempt shows its typed reason and disabled export. Existing selective RETEST and pair selection remain available.

**Checkpoint:**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_panel_performance_v2_retest.py tests/test_panel_performance_v2.py tests/test_panel_static_ui.py -q
node --check src/mrs3/panel_web/app.js
```

### Task 7: Documentation and verification

**Files:**

- [x] Update `docs/specs/2026-09-09-performance-v2-finalist-retest-control.md` with clarified failure/retry rules.
- [x] Update `docs/decisions/0034-performance-v2-global-finalist-retest-control.md`, `PRD.md`, and `progress.md` only after verification.

**Required regression evidence:** Freezing, execution, REPLACE, cache, ranking, and export do not change effective User Status/User Rank or the optimizer FINALIST set. Only explicit workbook import may change decisions. All-excluded and all-failed cohorts create no cache/run/workbook state. A failed retry is fresh; active duplicate is refused; successful replay never double-replaces.

**Final verification:**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_retest.py tests/test_performance_v2_import.py tests/test_performance_v2_selection.py tests/test_performance_v2_selection_review.py tests/test_performance_v2_finalist_retest.py tests/test_panel_performance_v2.py tests/test_panel_performance_v2_retest.py tests/test_panel_static_ui.py tests/test_portfolio_input.py -q
.venv\Scripts\python.exe -m pytest -q
node --check src/mrs3/panel_web/app.js
git diff --check
```

Root performs final self-review. Opus remains the plan advisor and is not used as implementation reviewer for this task.
