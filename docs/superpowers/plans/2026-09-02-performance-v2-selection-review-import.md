# Performance v2 selection review import implementation plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist finalist exports, collapse exact analogs, apply the approved
seven-component Top-20 ranking, import operator status/rank edits, and expose
reviewed finalists to A/B search without deleting data.

**Architecture:** Extend the existing selection executor and XLSX writer rather
than creating a parallel pipeline. Migrate the same DuckDB product to internal
schema version 3, persist immutable automatic runs, append immutable review
imports, and keep only current `REJECTED` tags as derived state. Use the current
panel HTTP server and installed XLSX stack; add no dependency or background job.

**Tech Stack:** Python, DuckDB, pandas, openpyxl, existing Panel HTML/CSS/JS,
pytest.

**Spec:** `docs/specs/2026-09-02-performance-v2-selection-review-import.md`

**ADR:** `docs/decisions/0022-performance-v2-selection-review-ledger.md`

**Status:** Implementation complete; acceptance pending a manual Excel round-trip
and independent code review. Plan review: `PLAN_APPROVED`.

## Constraints

- Use `.venv\Scripts\python.exe -m pytest`; never system Python/pytest.
- Use TDD for every behavior change.
- Keep preview and counter requests read-only.
- Never delete performance facts or change lifecycle status in this stage.
- Keep one authoritative selection algorithm and one workbook writer.
- Preserve all unrelated user work in the working tree.

## Task 1: Freeze the revised rank and analog contract

**Files:**

- Modify: `config.performance.json`
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `src/mrs3/panel_web/app.js`
- Test: `tests/test_performance_v2_selection.py`
- Test: `tests/test_panel_static_ui.py`

1. Add failing tests for weights `30/15/15/12/10/9/9`, lower-is-better Worst
   Hold p95, missing-component renormalisation and permutation-stable ties.
2. Add failing BABA-shaped fixtures grouped by exact Pair + Side + TF + ORD +
   Close MA. Prove only the highest weighted survivor represents each group and
   all other survivors link to it as `ANALOG`.
3. Prove a prior-REJECTED member cannot represent a group while a non-rejected
   member exists, and an all-rejected group remains effective `REJECTED`.
4. Prove filtered rows keep their original reason and cannot represent a group.
5. Prove unrankable groups choose a deterministic representative but produce no
   automatic finalist.
6. Prove ANALOG/FILTERED/prior-REJECTED rows consume no Top-N slots, both when
   fewer than N and more than N eligible representatives exist.
7. Change the shared rank components and tie order once in
   `performance_v2_selection.py`; reuse the resulting score for representative
   selection and final ranking.
8. Change default Top N from 50 to 20 and default
   `pareto_close_ma_near_tie` to disabled. Keep both user controls available.
9. Add one golden-value percentile/score test, including ties, one-row input and
   candidates with different present-component coverage.
10. Run:

   `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_panel_static_ui.py -q`

## Task 2: Migrate Performance DB v2 to internal schema 3

**Files:**

- Modify: `src/mrs3/performance_v2_store.py`
- Test: `tests/test_performance_v2_store.py`

1. Add failing tests for fresh schema 3, transactional schema-2 migration,
   generated/stable `database_instance_id`, exact catalog validation and
   unchanged existing facts.
2. Add `selection_runs`, `selection_results`, `selection_review_imports`,
   `selection_review_rows` and `strategy_tags` exactly as specified. Use
   application-generated UUID strings; add no sequences for them.
3. Keep immutable selection result IDs as plain values without strategy-result
   foreign keys so later separately approved cleanup can retain audit history.
4. Perform schema-2-to-3 DDL and marker update in one explicit transaction;
   rollback on any failure. Reject schema 1 and unexpected catalogs as before.
5. Run:

   `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_store.py -q`

## Task 3: Persist the exact automatic selection snapshot

**Files:**

- Create: `src/mrs3/performance_v2_selection_review.py`
- Modify: `src/mrs3/performance_v2_selection.py`
- Test: `tests/test_performance_v2_selection_review.py`

1. Add failing tests for canonical request/config JSON and hashes, immutable run
   rows, one result per candidate, prior-REJECTED evidence, stage trace, counts
   and latest-run lookup.
2. Implement one small persistence function that accepts an already calculated
   DataFrame and exact workbook bytes; it must not rerun selection.
3. Generate the run UUID before workbook creation, calculate workbook SHA-256,
   and insert the run plus all candidate results in one transaction.
4. Under the shared writer lock, recheck current result IDs in that transaction
   before insert; reject a raced replacement without returning the workbook.
5. Prove duplicate run IDs and interrupted writes leave no partial rows.
6. Run:

   `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection_review.py -q`

## Task 4: Produce the reviewable workbook

**Files:**

- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `src/mrs3/workbook.py`
- Test: `tests/test_performance_v2_selection.py`
- Test: `tests/test_performance_v2_selection_review.py`

1. Add failing workbook tests for every candidate exactly once, existing metric
   layout, enabled filters in submitted order, and the six review columns.
2. Add the very-hidden `_MRS_SELECTION_META` sheet and hidden immutable result
   identity. Keep Strategy ID as the row identity.
3. Add native Excel list validation for `User Status`; prefill user values from
   automatic values/current `REJECTED` tags. Prefill rank and analog target only
   when compatible with that prefilled status. Do not add macros or a new
   workbook dependency.
4. Refactor `write_selection_workbook` only enough to accept run metadata and
   return/write the same bytes that will be hashed and persisted.
5. Prove automatic and identity columns are detectably immutable on import even
   though Excel itself is not treated as a security boundary.
6. Prove style, formatting and sheet-visibility changes are tolerated while
   formulas and value/row-set changes are rejected.
7. Run the focused workbook tests from the two test files.

## Task 5: Validate and import user review atomically

**Files:**

- Modify: `src/mrs3/performance_v2_selection_review.py`
- Test: `tests/test_performance_v2_selection_review.py`

1. Add one failing test per stable import error: foreign database, unsupported
   schema, non-latest run, stale result IDs, changed automatic facts, row-set
   mismatch, duplicate upload hash, invalid status/rank/analog and oversized
   comment.
2. Enforce 20 MiB request, 256 ZIP-entry and 100 MiB declared-uncompressed
   limits before parsing. Reject formula cells; tolerate style changes.
3. Compare all rows against persisted `selection_results`; reject the complete
   file before opening the write transaction when possible.
4. Inside the shared writer lock and one transaction, repeat latest-run and
   result-ID checks, append import metadata and every review row, then
   synchronise `REJECTED` only for Strategy IDs in that run.
5. Validate all status transitions: clear target when leaving ANALOG, require a
   same-run FINALIST/RESERVE target when entering/staying ANALOG, retain auto
   filter reason when promoting FILTERED, allow User Rank only for
   FINALIST/RESERVE, require non-empty User Status for every row, and reject a
   representative change that leaves analogs targeting
   REJECTED/FILTERED/ANALOG.
6. Prove an exact replay writes nothing, an edited re-import appends history,
   an unedited export is accepted once, and importing another Pair + Side cannot
   remove unrelated tags.
7. Prove changing a current REJECTED row to RESERVE in the latest run removes
   only that tag, while an older-run import cannot resurrect it.
8. Prove manual effective finalist counts `N-1`, `N` and `N+1` all import; Top N
   is only an automatic ceiling.
9. Run:

   `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection_review.py -q`

## Task 6: Connect export/import and A/B finalist search

**Files:**

- Modify: `src/mrs3/panel.py`
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `src/mrs3/panel_web/styles.css`
- Test: `tests/test_panel.py`
- Test: `tests/test_panel_static_ui.py`
- Test: `tests/test_panel_performance_v2.py`

1. Add failing controller/API tests proving XLSX export persists one exact run
   while preview remains write-free.
2. Add one bounded raw-XLSX endpoint using the panel's existing body-limit
   pattern. Return stable error codes from the spec.
3. Add a compact folder-picker `Обратный импорт XLS` control and aggregate
   status text near the existing export controls. Import each `.xlsx` as an
   independent atomic request; do not redesign the panel.
4. Enable `Только финалисты` when the selected pair has at least one saved run.
   For each side, use its latest run and union effective `FINALIST` rows; a side
   without a run contributes no rows.
5. Prove a newer export supersedes only the same Pair + Side, the other side's
   latest review remains active, and an older workbook is rejected as
   non-latest.
6. Return stale Strategy IDs in the import error and present a concise
   re-export instruction. Do not carry non-REJECTED decisions onto changed
   tester evidence.
7. Show an explicit empty state when saved runs exist but their effective
   finalist union is empty.
8. Run:

   `.venv\Scripts\python.exe -m pytest tests/test_panel.py tests/test_panel_static_ui.py tests/test_panel_performance_v2.py -q`

   `node --check src/mrs3/panel_web/app.js`

## Task 7: Acceptance, documentation and independent review

**Files:**

- Modify: `PRD.md`
- Modify: `progress.md`
- Modify only if user-facing commands changed: `README.md`

1. Run all focused tests from Tasks 1-6 together.
2. Run the relevant broader Performance v2 and panel suites; record exact
   counts and any unrelated pre-existing failures in `progress.md`.
3. On a copy of a real Performance DB, verify schema 2 migrates once, existing
   counts remain unchanged, marker/instance identity is complete, and reopen
   validation passes before touching the working database.
4. Export one real Pair + Side, edit status/rank, import it, reopen the panel and
   verify A/B `Только финалисты` uses the reviewed result.
5. Open and save that workbook once in Microsoft Excel, then prove the permitted
   edits still import; record the application/version used.
6. Verify `REJECTED` changes only `strategy_tags`/review history and deletes no
   strategy, result, action or equity facts.
7. Run `git diff --check` and inspect the complete staged diff.
8. Send the compact ASCII requirements/diff/test packet required by `AGENTS.md`
   to the independent reviewer. Fix confirmed findings, rerun focused checks,
   and request re-review when code changed.
9. Update this plan status and `progress.md` only from verified evidence; then
   create one scoped conventional commit.
