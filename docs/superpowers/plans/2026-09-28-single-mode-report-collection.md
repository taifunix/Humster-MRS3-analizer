# SINGLE_MODE Report Collection Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable opt-in collection of ordinary SINGLE_MODE tester jobs and import the registered heterogeneous batches together.

**Architecture:** Reuse committed per-job inboxes as immutable evidence and persist explicit membership in the existing Panel job registry. Verification freezes a versioned metadata-only collection inbox whose entries retain their originating batch context; the existing Performance v2 staging and database transaction consume it.

**Tech Stack:** Python 3.11+, pytest, DuckDB, vanilla JavaScript/HTML, existing Panel job registry.

**Spec:** `docs/specs/2026-09-28-single-mode-report-collection.md`

## Global Constraints

- Collection membership is explicit and server-owned; folder contents and unrelated jobs never imply membership.
- Registered batches may differ in dates, initial balance, tester config, commission contract and analysis run ID.
- Verification/import reads only exact entries of committed registered inboxes.
- Ordinary SINGLE_MODE and all RETEST workflows remain backward compatible.
- No new database, HTML archive, second importer or report-directory discovery pass.
- One collection import is one Performance v2 transaction and one deterministic collection digest.
- Duplicate strategy names and report basenames fail closed.
- Clear and collection actions never delete non-member artifacts.
- Use `.venv` tests only; no real tester or production Performance DB mutation.

## Review Focus

- A valid unregistered job in the same report directory must remain invisible to the collection.
- Per-entry dates and commission must not fall back to the first batch's context.
- Retry lineage must not count both failed source and committed retry.
- Reload must recover only durable registered membership, never derive it from HTML.
- Import/clear failure must not authorize a stale or changed collection snapshot.

---

### Task 1: Versioned heterogeneous collection inbox

**Files:**
- Create: `src/mrs3/performance_v2_collection.py`
- Modify: `src/mrs3/performance_v2_input.py`
- Modify: `src/mrs3/performance_v2_import.py`
- Modify: `src/mrs3/runner/inbox.py`
- Create: `tests/test_performance_v2_collection.py`
- Modify: `tests/test_performance_v2_input.py`
- Modify: `tests/test_performance_v2_import.py`

**Interfaces:**
- Consumes: committed schema-version-1 metadata-only SINGLE_MODE inbox directories.
- Produces: `build_single_mode_collection_inbox(inbox_root: Path, collection_id: str, member_inboxes: Sequence[Path], *, report_root: Path, trusted_strategy_root: Path) -> Path` and per-entry prepared batch context used by validation/import.

- [ ] **Step 1: Write failing tests** for two member inboxes with different ranges, commission contracts, config hashes and analysis IDs; assert deterministic collection identity, per-entry report validation/persistence, duplicate rejection, changed-member rejection and exclusion of an unlisted inbox.
- [ ] **Step 2: Run the focused tests and capture RED evidence.**
- [ ] **Step 3: Implement the versioned collection builder and reader compatibility.** Preserve schema-version-1 behavior and validate every member before atomically publishing the collection manifest.
- [ ] **Step 4: Move range/config/commission context onto prepared entries.** Existing single inboxes populate identical context on each entry; collection entries keep their origin context.
- [ ] **Step 5: Use entry context in report validation and persisted result values.** Keep one existing staging/transaction path.
- [ ] **Step 6: Run focused input/import/collection tests and the existing Performance v2 input/import suites.**
- [ ] **Step 7: Commit with `feat: support heterogeneous report collections`.**

### Task 2: Durable Panel job registration and collection lifecycle

**Files:**
- Create: `src/mrs3/panel_report_collection.py`
- Modify: `src/mrs3/panel_jobs.py`
- Modify: `src/mrs3/panel.py`
- Modify: `tests/test_panel_fast_strategy_test.py`
- Modify: `tests/test_panel_performance_v2.py`
- Create: `tests/test_panel_report_collection.py`

**Interfaces:**
- Consumes: Task 1 `build_single_mode_collection_inbox(...)` and the existing `PanelJobRegistry`.
- Produces: durable collection create/register/retry/status/verify/clear/import-complete operations and Panel endpoints used by Task 3.

- [ ] **Step 1: Write failing lifecycle tests.** Cover registration before worker start, unchecked-job exclusion, active-member gate, failed/cancelled exclusion, retry replacement, restart recovery, immutable verified generations, clear without deletion and successful-import closure.
- [ ] **Step 2: Run the focused tests and capture RED evidence.**
- [ ] **Step 3: Implement a small collection coordinator over `.panel-jobs.json`.** Use a `strategies.tester.collection` record and exact member IDs/expected names; do not scan report HTML.
- [ ] **Step 4: Extend ordinary tester start/retry.** Accept strict boolean `collect_reports`, register before service start, propagate collection lineage only for registered ordinary jobs and exclude RETEST.
- [ ] **Step 5: Add status, verify and clear controller/API operations.** Verification freezes Task 1's inbox and authorizes import only for the returned collection job ID.
- [ ] **Step 6: Integrate import completion.** A committed import closes only its collection; failure leaves the verified snapshot retryable. Non-member artifacts remain untouched.
- [ ] **Step 7: Run focused collection/controller tests plus existing tester and Panel Performance v2 suites.**
- [ ] **Step 8: Commit with `feat: register report collection jobs`.**

### Task 3: Ordinary tester-card UI and final integration

**Files:**
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `tests/test_panel_static_ui.py`
- Modify: `PRD.md`
- Modify: `progress.md`

**Interfaces:**
- Consumes: Task 2 collection status, verify and clear APIs plus collection-aware tester start.
- Produces: checkbox `Объединять отчёты`, durable counts, clear control and collection-aware check/import behavior.

- [ ] **Step 1: Write failing UI contract tests.** Assert checkbox/start payload, visible pack/report counts, clear action, reload recovery, collection verify target and unchanged ordinary fallback.
- [ ] **Step 2: Run the focused UI tests and capture RED evidence.**
- [ ] **Step 3: Add the controls and rendering.** Keep them in the existing tester card and keep current progress/status surface.
- [ ] **Step 4: Connect start, recovery, verify, import and clear.** Never infer membership from the general jobs list; use the collection status returned by the server.
- [ ] **Step 5: Update PRD/progress with verified state and commands.** Do not claim a real tester run or browser visual pass.
- [ ] **Step 6: Run static UI tests, `node --check`, all affected Python suites and `git diff --check`.**
- [ ] **Step 7: Commit with `feat: add report collection controls`.**

### Task 4: Whole-feature verification

**Files:**
- Modify only if verification exposes a regression, following a fresh failing test.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: acceptance evidence for the complete branch.

- [ ] **Step 1: Run all focused and affected suites from the local `.venv`.**
- [ ] **Step 2: Run the complete pytest suite once.** Record pre-existing warnings/skips separately from failures.
- [ ] **Step 3: Run `node --check src/mrs3/panel_web/app.js` and `git diff --check`.**
- [ ] **Step 4: Inspect the full branch diff for scope and generated/local artifacts.**
- [ ] **Step 5: Commit only verification-driven fixes, if any, with a scoped conventional commit.**
