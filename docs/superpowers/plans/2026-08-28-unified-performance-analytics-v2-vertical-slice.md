# Unified Performance Analytics v2 — Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` or `superpowers:subagent-driven-development` task-by-task. Every implementation task must use `ponytail:ponytail` at full level. Steps use checkbox syntax for tracking.

**Goal:** Add the smallest independent v2 Performance DB flow: a committed native `SINGLE_MODE`/RUNS handoff is imported from one metadata-only inbox into one isolated DuckDB with typed strategy/plateau facts, one current result, and cached safe UPNL-relative windows.

**Architecture:** v2 is a new, isolated runtime. Native `SINGLE_MODE` creates one metadata-only inbox: strategy JSON remains in the trusted `Output/strategies` root and current HTML remains in `tester/report/my_test`; v2 validates their recorded hashes before copying bounded bytes to a v2-owned temporary staging directory, parses reports in parallel, and publishes through one DuckDB writer transaction. Cleanup of those exact source roots is attempted only after DB `COMMITTED`; a warning never rolls back the DB. A separate panel action calls v2; v1 Performance/DD5 remains untouched until an explicit later cutover.

The former contract that every `strategy_path` must point inside the inbox is
superseded for native `SINGLE_MODE`. External strategy paths are accepted only
when the server-configured trusted root is `Output/strategies`; `..`, absolute
substitution outside that root, and symlink/reparse escapes remain rejected.

**Tech Stack:** Python 3.11, stdlib concurrency/path handling, DuckDB, existing `lxml` report tooling, static panel JavaScript, pytest.

**Spec:** `docs/specs/2026-08-28-unified-performance-analytics-v2.md`; `docs/decisions/0020-unified-performance-analytics-v2.md`

## Actual implementation status (2026-08-31)

The checkboxes below are the original task-by-task delivery record. The
following status is authoritative, based on accepted commits, focused tests and
the live `1633/1633` `COMMITTED` import recorded in `progress.md`:

| Plan task | Actual status and evidence |
| --- | --- |
| Gate 0 | Complete: v1/v2 path intersection recorded as `NONE`. |
| Task 1 | Complete — `3686af7`: isolated schema/config. |
| Task 2 | Complete — `ead1ded`: trusted metadata inbox adapter/staging. |
| Task 3 | Complete — `5475dae`: current-HTML parser boundary. |
| Task 4 | Complete — `f69737a`: atomic ADD/REPLACE importer. |
| Task 5 | Complete — `a154015`: calculation/cache boundary. Import deliberately performs no eager A/B calculation; the visible analysis is Phase 2. |
| Task 6 | Complete — `91381ce`: native `SINGLE_MODE` inbox-to-v2 panel cutover. The original cached-A/B-import assertion is superseded by `window_count=0` in the fast import contract. |
| Task 7 | Historical vertical-slice review/evidence is recorded in `progress.md`. Follow-up review of later optimization commit `5643399` is tracked separately. |

Phase 2 starts with the omitted user-facing A/B window analysis. Tags, discard,
RETEST, filters/Pareto, XLSX and Portfolio input remain explicitly deferred.

## Global Constraints

- Use `ponytail:ponytail` at full level: no new dependency, no speculative abstraction, no second inbox or mode-specific manifest.
- Do not modify `performance_store.py`, `performance_import.py`, `performance.py`, `performance_metrics.py`, or `performance_dd5.py`; `runner/inbox.py` is modified only for the metadata-only native handoff.
- Do not migrate or dual-read schema v1. Existing v1 code is a historical, untouched compatibility path.
- Source inboxes are read-only during import. After a successful v2 DB commit, only the exact tester report and `Output/strategies` directories may be cleared; inbox metadata and v2 audit remain provenance.
- The only v2 database is `<v2-owned-root>/strategy_performance.duckdb`; it has one writer.
- Expensive report parsing uses the configured `unified_performance_v2.workers`, default 16, clamp 1..64. Workers never open DuckDB or write files.
- Only current HTML reports are accepted. Their action table must contain `Timestamp, Symbol, Order ID, Action, Fee, PnL, Balance, Size, Post Size, Post Side`; harmless additional tester columns and column order are accepted.
- UPNL is mandatory; cross-window objectives are relative/geometric. DD5, tags, discard, RETEST, filters, Pareto, XLSX, Portfolio view and `point_id` are out of this slice.
- Run tests only with `.venv\Scripts\python.exe -m pytest`.

---

## Gate 0: Prove v1/v2 storage isolation before code

**Files:**

- Create: `docs/superpowers/plans/2026-08-28-unified-performance-analytics-v2-vertical-slice.md` (fill the evidence table below before Task 1)
- Read only: `src/mrs3/performance_import.py`, `src/mrs3/panel_performance_dd5.py`, `config.performance.json`, `config.local.json.example`

**Stop condition:** Do not implement any v2 code while this gate is not `PASS`.

- [x] **Step 1: Inspect the current v1 allocator and catalog without changing files.**

  Run:

  ```powershell
  rg -n "performance_database_name|allocate_performance_database|performance_db_root|performance-v6|strategy_performance.duckdb" src/mrs3 config.local.json.example tests
  Get-Content -Encoding utf8 -LiteralPath config.performance.json
  git status --short
  ```


- [x] **Step 2: Confirm this currently observed relative-path evidence, then re-check any local override.**

  | Check | Observed value |
  | --- | --- |
  | v1 allocator | `performance_database_name()` → `<pairs>_<period>.performance-v6.duckdb` |
  | v1 default root | `data/performanceDB` |
  | v1 catalog identity | files ending in `.performance-v6.duckdb` below its configured root |
  | v2-owned root | `data/performance-v2` |
  | v2 target | `data/performance-v2/strategy_performance.duckdb` |
  | planned intersection | `NONE` |

  Before implementation, resolve the operator's local root configuration and stop if it overrides either relative path into an overlap. Do not store machine-specific absolute paths in Git documentation.

- [x] **Step 3: Record the future v2 path-helper gate (implementation in Task 1).**

  The helper implementation is deferred to Task 1 because Gate 0 is
  read-only. Gate 0 evidence confirms the planned target is isolated before
  any v2 connection is opened.

  The helper must reject the target *before* `duckdb.connect` when it equals a v1 file, is below the v1 catalog root, or resolves through a symlink/reparse point outside the configured v2 root. V2 never opens a v1 file.

- [x] **Step 4: Commit the evidence only after the table says `NONE`.**

  Evidence is recorded in the plan's SDD ledger. The initial sandbox blocked
  branch creation; the worktree is now writable through the approved Git
  operation, so this evidence can be committed before Task 1.

  ```powershell
  git add docs/superpowers/plans/2026-08-28-unified-performance-analytics-v2-vertical-slice.md
  git commit -m "docs: record performance v2 storage isolation"
  ```

## Task 1: Add isolated schema v2 and v2 config

**Files:**

- Create: `src/mrs3/performance_v2_store.py`
- Create: `tests/test_performance_v2_store.py`
- Modify: `config.performance.json`

**Interfaces:**

- Produces `PerformanceV2Config`, `load_performance_v2_config(path: Path) -> PerformanceV2Config`, `performance_v2_database_path(config) -> Path`, `initialize_performance_v2(connection) -> None`, and `require_performance_v2(connection) -> None`.
- Task 2 consumes `PerformanceV2Config` and its validated owned root; Tasks 3–6 require `require_performance_v2` before publication.

- [ ] **Step 1: Write the failing storage/config tests.**

  Cover fixed target, schema idempotence, v1-path rejection before connect, schema-v1 rejection unchanged, default workers 16, boolean/non-positive rejection, and values above 64 clamped to 64. Assert the existing v1 `PerformanceImportRequest` defaults and allocator output do not change.

- [ ] **Step 2: Run the focused test to confirm it fails.**

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_performance_v2_store.py
  ```

- [ ] **Step 3: Implement the additive v2 config namespace.**

  ```json
  {
    "unified_performance_v2": {
      "database_root": "data/performance-v2",
      "workers": 16,
      "max_html_bytes": 67108864,
      "max_actions_per_report": 1000000
    }
  }
  ```

  Read only this namespace. It is additive and must not change v1 consumers.

- [ ] **Step 4: Implement the smallest schema that enforces v2 facts.**

  Required tables and constraints:

  - `strategies`: `strategy_id`, unique name, typed pair/side/timeframe/Close MA, `order_count CHECK (BETWEEN 1 AND 4)`, `analysis_run_id`, `candidate_identity`, status, and `current_result_id`.
  - `analysis_plateaus`: composite primary key `(analysis_run_id, plateau_id)`, point count and total trades.
  - `strategy_orders`: primary key `(strategy_id, order_id)`, `order_id CHECK (BETWEEN 1 AND 4)`, typed MA/multiplier/shift/lot/base trades, and composite foreign key to `analysis_plateaus`.
  - `strategy_results`: typed result facts plus `UNIQUE(strategy_id)`; this is the one persisted current result.
  - `strategy_actions` and `strategy_equity`: typed, append-only rows per result.
  - `window_metrics`: primary key `(result_id, requested_start_utc, requested_end_utc, metrics_version)` and nullable metric payload for unavailable windows.
  - small `import_runs`/`import_files` tables with only source identity, count/status and no full strategy JSON.

  Use DuckDB sequences for IDs. Add only indexes used in the slice: result strategy lookup and `(result_id, timestamp_utc)` action/equity reads.

- [ ] **Step 5: Add DB-level invariant tests.**

  Assert 0/5 orders, an order without a plateau, and a second persisted result for one strategy are rejected. Assert the same `plateau_id` for distinct analysis runs is allowed.

- [ ] **Step 6: Run storage and v1 non-disturbance checks.**

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_performance_v2_store.py tests/test_performance_import.py tests/test_panel_performance_dd5.py
  ```

- [ ] **Step 7: Commit.**

  ```powershell
  git add config.performance.json src/mrs3/performance_v2_store.py tests/test_performance_v2_store.py
  git commit -m "feat: add isolated unified performance schema v2"
  ```

## Task 2: Read the shared inbox without owning it

**Files:**

- Create: `src/mrs3/performance_v2_input.py`
- Create: `tests/test_performance_v2_input.py`

**Interfaces:**

- Produces `PerformanceV2InputError`, `PreparedV2Input`, `read_performance_v2_inbox(...)`, `adapt_strategy_identity(...)`, `create_v2_parser_staging(...)`, `remove_v2_parser_staging(...)`.
- Task 4 calls this module only after its v2 writer lock is acquired.

- [ ] **Step 1: Write failing adapter tests.**

  Use one native `SINGLE_MODE` metadata-only inbox and one RUNS inbox. Verify that both reach the same adapter, a 1ORD and a 2ORD strategy share plateau `P1` without duplication, missing plateau evidence and conflicting facts reject, and `point_id` or full strategy JSON never enter the prepared record.

- [ ] **Step 2: Test trust boundaries before parsing.**

  Metadata-only `SINGLE_MODE` strategy paths may be external only under the trusted `Output/strategies` root; report paths must be contained inside the configured tester-report root. Reject `..`, absolute substitution, symlinks/reparse escapes and oversized HTML before workers. Snapshot the complete inbox bytes and assert identity after both success and failure.

- [ ] **Step 3: Implement the read-only adapter.**

  Read `inbox_manifest.json` once, retain its SHA-256 and extract only staged strategy/report paths, hashes, `analysis_run_id`, candidate identity, order plateau diagnostics and shared commission context. Derive side, ordered open MA, multiplier, integer `shift_bp`, lot and Close MA from staged strategy JSON. Require order IDs 1..N and N in 1..4.

- [ ] **Step 4: Implement v2-owned staging.**

  After Task 4 has acquired the DB writer lock, copy validated bytes to `<v2-root>/.staging/<fresh-uuid>/`; do not reuse an existing staging directory. All worker paths must resolve beneath that directory. Delete this directory on every terminal success/failure path; a crash leaves an unreferenced directory which a later job must never reuse.

- [ ] **Step 5: Run focused tests and commit.**

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_performance_v2_input.py tests/runner/test_inbox.py
  git add src/mrs3/performance_v2_input.py tests/test_performance_v2_input.py
  git commit -m "feat: adapt committed inboxes for performance v2"
  ```

## Task 3: Parse only the current report layout

**Files:**

- Create: `src/mrs3/performance_v2_html.py`
- Create: `tests/test_performance_v2_html.py`
- Create: `tests/fixtures/performance/report_current_v2.html`

**Interfaces:**

- Produces immutable parsed dataclasses and `parse_current_performance_v2_html(data: bytes, limits: PerformanceV2Config) -> ParsedPerformanceV2Report`.
- Task 4 passes staged bytes to this pure function in a `ProcessPoolExecutor`.

- [ ] **Step 1: Write parser tests first.**

  Test required-header acceptance with extra tester columns, missing `Post Size`, missing `Post Side`, legacy layout accepted by v1 but rejected by v2, non-integer Order ID, non-finite values, negative Post Size, empty Post Side with non-zero Post Size, action limit, symbol mismatch, and `use_upnl=false`.

- [ ] **Step 2: Implement a v2-owned parser boundary.**

  Require the named action fields, while allowing other tester columns and their order. Coerce UTC timestamps, finite `Decimal` values and positive integer report Order IDs in v2. Existing `parse_performance_report` may be called only as a read-only helper for lossless inventory/series; do not edit v1 when it lacks a v2 field.

- [ ] **Step 3: Protect worker purity.**

  Worker input is staged immutable bytes or an owned staging path. Workers do not import/open DuckDB, create audit files, write files, or delete data. Add a monkeypatch guard test that fails on either a DB connection or write call from a worker.

- [ ] **Step 4: Verify and commit.**

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_performance_v2_html.py tests/test_performance.py
  git add src/mrs3/performance_v2_html.py tests/test_performance_v2_html.py tests/fixtures/performance/report_current_v2.html
  git commit -m "feat: parse current tester html for performance v2"
  ```

## Task 4: Publish ADD and explicit REPLACE atomically

**Files:**

- Create: `src/mrs3/performance_v2_import.py`
- Create: `tests/test_performance_v2_import.py`

**Interfaces:**

- Produces `PerformanceV2ImportRequest`, `PerformanceV2ImportResult`, `PerformanceV2ImportError`, `PerformanceV2LockedError`, and `import_performance_v2(request)`.
- Task 6 invokes it from the new panel job.

- [ ] **Step 1: Write transaction tests.**

  Cover lock conflict, atomic multi-row ADD, exact-payload `SKIPPED`, rejected implicit replacement, explicit replacement, typed mismatch rollback, injected failure after deleting old rows, one current result, one writer, one set-based preload/readback and shared inbox byte identity after success/rollback.

- [ ] **Step 2: Acquire the v2 writer lock before any staging or parsing.**

  Resolve the target against Gate 0 rules, then open only that file. Do not retry or wait: a DuckDB lock/connect failure returns `PerformanceV2LockedError`. On this error do not read or mutate quarantine state, create staging, parse reports or write audit. DuckDB releases file locks when the owning process exits; an operator sees a typed panel failure if a live process holds it.

- [ ] **Step 3: Implement lock-first single-writer publication.**

  After the lock succeeds: create owned staging, parse with `min(workers, report_count)`, perform one set-based preload of existing strategies/orders/results, load prepared rows into TEMP tables and grouped-readback counts/hashes. Never connect once per report and never issue one readback query per result.

- [ ] **Step 4: Implement ADD and REPLACE semantics.**

  `ADD` inserts a new strategy/result; identical current payload is `SKIPPED`; changed content for an existing name fails. `REPLACE` requires an explicit `strategy_name -> strategy_id` mapping and exact typed strategy/order/plateau equality. In the same rollback-safe transaction delete old cache/actions/equity/result, insert staged new facts and update `current_result_id`. Any failure restores the prior result.

- [ ] **Step 5: Keep audit and cleanup minimal.**

  Write only a v2-owned terminal audit after releasing the lock. Cleanup runs only after DB `COMMITTED`, removes exact tester/report and `Output/strategies` contents (and stale `Output/strategy_manifest.json`), and reports a path-safe cleanup warning without changing the committed DB. Inbox metadata and audit remain untouched.

- [ ] **Step 6: Verify and commit.**

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_performance_v2_import.py tests/test_performance_v2_input.py tests/test_performance_v2_html.py
  git add src/mrs3/performance_v2_import.py tests/test_performance_v2_import.py
  git commit -m "feat: import current performance results atomically"
  ```

## Task 5: Cache versioned safe UPNL windows

**Files:**

- Create: `src/mrs3/performance_v2_windows.py`
- Create: `tests/test_performance_v2_windows.py`

**Interfaces:**

- Produces `METRICS_VERSION = "performance-window-v2.1"`, `get_or_calculate_window(...)`, `get_or_calculate_window_pair(...)`, and `compare_window_pair_geometrically(...)`.

- [ ] **Step 1: Write window tests first.**

  Cover independent A-start and B-end inward movement, overlapping/nested/disjoint windows, out-of-range/no-flat/collapsed cacheable results, no boundary expansion, cache hit, calculator-version cache miss, balance-scale invariance, geometric ratio zero/negative rules, partial-fill round trips and cache removal during replacement.

- [ ] **Step 2: Implement inward-only boundaries.**

  Effective start is the first flat equity sample at or after requested start. Effective end is the last flat equity sample at or before requested end. Flat means last action at `(timestamp_utc, action_index)` has `post_size=0`. Never widen the interval.

  Cache unavailable outcomes with the exact request and one reason: `OUT_OF_RANGE`, `NO_FLAT_START`, `NO_FLAT_END`, `COLLAPSED`, or `NO_TRADES`.

- [ ] **Step 3: Implement UPNL-relative metrics and A/B comparison.**

  Calculate `growth_factor`, `return_pct`, daily log/growth rate, maximum drawdown percent, return/DD, fees percent, PF from `decreased` and `closed`, reconstructed round trips, holding and time-in-market. Do not use absolute PnL as comparison output.

  Return signed metrics per window, but compare only positive geometric ratios: `growth_factor_B / growth_factor_A` and `exp(log_return_B - log_return_A)`. Return typed `UNDEFINED_ZERO_BASELINE`, `UNDEFINED_NON_POSITIVE_INPUT`, or `WINDOW_NOT_AVAILABLE` instead of arithmetic deltas.

- [ ] **Step 4: Verify and commit.**

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_performance_v2_windows.py tests/test_performance_v2_import.py
  git add src/mrs3/performance_v2_windows.py tests/test_performance_v2_windows.py
  git commit -m "feat: cache versioned relative performance windows"
  ```

## Task 6: Expose a separate v2 panel action

**Files:**

- Create: `src/mrs3/panel_performance_v2.py`
- Create: `tests/test_panel_performance_v2.py`
- Modify: `src/mrs3/panel.py`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `src/mrs3/panel_web/index.html`

**Interfaces:**

- New background job kind `strategies.performance.v2.import`.
- New status endpoint `/api/v2/strategies/performance-v2/import/status`.

- [ ] **Step 1: Write a full vertical-slice panel test.**

  Prepare one read-only committed inbox containing a 1ORD candidate for `P1` and a 2ORD candidate for `P1`,`P2`, plus two current UPNL reports. Start panel ADD and assert two strategies, three order links, two plateau rows, two current results, cached A/B output, terminal `COMMITTED`, and unchanged inbox bytes. Mock v1 import, v1 DD5 and workbook services and assert zero calls.

- [ ] **Step 2: Implement the smallest v2 panel service and job wiring.**

  It receives only a committed tester job; server-side configuration supplies the owned inbox and report root. Default is ADD. Status returns imported/skipped/rejected counts, v2 audit path and the fixed v2 database target. It never invokes a v1/DD5 service.

- [ ] **Step 3: Make the UI cutover isolated.**

  The visible `Inbox → Performance DB` card targets the v2 job only after the E2E test is green. Native `SINGLE_MODE` is the active tester path; Fast UI and active panel routes are removed, while the Runs backend/API remains available with its UI hidden. Keep unrelated v1 routes/tests untouched. Do not label any proxy as a tested result.

- [ ] **Step 4: Verify and commit.**

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_panel_performance_v2.py tests/test_panel.py tests/test_panel_static_ui.py tests/test_integration_contract.py tests/test_panel_performance_dd5.py
  node --check src/mrs3/panel_web/app.js
  git add src/mrs3/panel_performance_v2.py tests/test_panel_performance_v2.py src/mrs3/panel.py src/mrs3/panel_web/app.js src/mrs3/panel_web/index.html
  git commit -m "feat: expose unified performance v2 vertical slice"
  ```

## Task 7: Record evidence and review the implementation

**Files:**

- Modify: `progress.md`
- Modify: `PRD.md`

- [ ] **Step 1: Run all focused v2 tests.**

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_performance_v2_store.py tests/test_performance_v2_input.py tests/test_performance_v2_html.py tests/test_performance_v2_import.py tests/test_performance_v2_windows.py tests/test_panel_performance_v2.py
  ```

- [ ] **Step 2: Prove v1 non-disturbance and panel syntax.**

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_performance.py tests/test_performance_store.py tests/test_performance_import.py tests/test_performance_metrics.py tests/test_performance_dd5.py tests/test_panel_performance_dd5.py tests/runner/test_inbox.py tests/test_panel.py tests/test_panel_static_ui.py tests/test_integration_contract.py
  node --check src/mrs3/panel_web/app.js
  git diff --check
  ```

- [ ] **Step 3: Send the implementation diff and verification output to the independent Reviewer.**

  Required terminal evidence is `CODE_REVIEW_PASS`. For any accepted finding, repair the smallest scope, rerun relevant tests, then re-review.

- [ ] **Step 4: Update only verified documentation and commit.**

  `progress.md` and `PRD.md` record the exact handoff evidence, commits `952bc22..3f535f4`, and final `CODE_REVIEW_PASS`; the broader v2 pipeline remains pending. Do not rewrite the approved spec or ADR.

  ```powershell
  git add progress.md PRD.md
  git commit -m "docs: record unified performance v2 evidence"
  ```

## Explicitly Deferred

- `strategy_tags`, `DISCARDED`, and the durable `RETEST` handler. Its next
  phase must add a panel date-range interface and reject each request unless
  `listing_date <= test_start < test_end`, with `listing_date` read from
  `dates.xlsx` for every selected symbol;
- cleanup of source paths outside the exact approved post-commit roots;
- DD5 proxy UI and any scaled-result claim;
- built-in filters, Pareto, selection runs/results, XLSX and Portfolio Optimizer input;
- deletion of v1 runtime and storage;
- `point_id`, arbitrary filter expressions, and portfolio simulation.

## Approval Record

Planner route: GPT-5.6 Sol high, with required `ponytail:ponytail` full. Advisor route: Claude Opus 5 high. Advisor verdict on this revised plan: `PLAN_APPROVED`. The Advisor’s two non-blocking notes are addressed in Task 2 (never reuse staging directories; remove owned staging on terminal paths) and Task 4 (fail fast on a live DuckDB lock; no retry/partial mutation).
