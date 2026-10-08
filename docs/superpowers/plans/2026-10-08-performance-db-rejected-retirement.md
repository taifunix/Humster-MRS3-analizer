# Rejected Strategy Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `Delete Rejected` remove rejected strategies from all operational work while retaining only typed identity, orders, and a compact interval tombstone for deduplication.

**Architecture:** The operation removes all per-strategy operational facts, caches, tags, rejection sources, and selection rows, compacts the current result to temporal/provenance identity, then atomically changes matching strategy rows to `lifecycle_status=DISCARDED`. Active-only selection, cache, catalog, and XLSX readers exclude them. The importer treats a discarded typed row as a permanent dedup tombstone; intentional reintroduction requires full pair deletion.

**Tech Stack:** Python, DuckDB, pytest, existing schema v9.

**Spec:** `docs/specs/2026-10-06-performance-db-maintenance.md`; decision: `docs/decisions/0061-performance-db-rejected-retirement.md`.

## Global Constraints

- Keep schema v9; no new cleanup table or deletion timestamp.
- Keep only `strategies`, `strategy_orders`, and the compact current `strategy_results` tombstone. Retain typed order fields and temporal/provenance result fields; clear PnL/DD/fee/trade-count/exclusion/optimizer/sizing payloads.
- Remove detailed actions/equity, window/equity caches, optimizer inputs, tags, rejection sources, and per-strategy selection/review rows. Pair-level selection runs and shared review-import rows remain when needed by other strategies.
- Operational readers must continue using `lifecycle_status='ACTIVE'`.
- Use the project `.venv` for tests and do not touch live databases.

## Review Focus

- Rejected cleanup archives every exact active effective rejection and retries residual facts for already-discarded tombstones.
- A failed grouped delete must not leave facts removed while lifecycle remains active.
- Export and selection must exclude archived rows while full pair deletion still removes them.
- Equal, narrower, and wider imports must deduplicate against archived typed rows; no automatic reactivation is allowed because rejection evidence remains sticky.
- Non-rejected and unselected strategies must remain unchanged.

### Task 1: Maintenance archive transition

**Files:**
- Modify: `src/mrs3/performance_v2_maintenance.py`
- Modify: `src/mrs3/panel_web/app.js`
- Test: `tests/test_performance_v2_maintenance.py`

- [x] Add a failing test asserting rejected cleanup marks only matching strategy rows `DISCARDED`, leaves typed settings/results, removes operational facts/caches/selection rows, and repeats with zero work.
- [x] Run the focused test and observe the expected failure.
- [x] Group rejected fact deletes into one transaction and update the matching strategy rows to `DISCARDED` in that transaction.
- [x] Update the preview/completion text to state that archived strategies are hidden from operational outputs but retained for deduplication.
- [x] Inventory every strategy reader used by selection, cache, catalog, XLSX export, maintenance, and full deletion; keep ACTIVE-only predicates for operational readers and exact-symbol unfiltered scope for maintenance/full deletion.
- [x] Fail closed when more than one discarded row has the same typed key; do not silently choose a tombstone.
- [x] Run focused maintenance tests.

### Task 2: Archived typed deduplication

**Files:**
- Modify: `src/mrs3/performance_v2_import.py`
- Test: `tests/test_performance_v2_import.py`

- [x] Add failing tests for equal and wider reports whose canonical strategy is `DISCARDED` and whose detailed facts were removed; both must be skipped without creating a second active row.
- [x] Run the test and observe the expected failure.
- [x] Include unique discarded typed rows as fallback dedup candidates when no active row exists; preserve fail-closed behavior for ambiguous keys.
- [x] Treat every matching discarded typed row as `SKIPPED`; explicit `REPLACE` mappings continue to require ACTIVE rows.
- [x] Run focused import tests and the maintenance regression.

### Task 3: Contract and status evidence

**Files:**
- Modify: `docs/specs/2026-10-06-performance-db-maintenance.md`
- Create: `docs/decisions/0061-performance-db-rejected-retirement.md`
- Modify: `progress.md`

- [x] Document the `DISCARDED` tombstone contract, operational visibility, and importer behavior.
- [x] Record the accepted lifecycle decision in a new ADR without rewriting ADR-0057.
- [x] Record verification commands and any remaining limitation in `progress.md`.
- [x] Run `git diff --check` and inspect the scoped diff.

## Final evidence

- Maintenance: 52 passed.
- Selection: 239 passed.
- Import: 120 passed.
- XLSX export: 11 passed; Panel discarded-catalog regression passed.
- node syntax, py_compile, and git diff checks passed.
- All database tests used isolated temporary DuckDB fixtures; no live database or Panel process was touched.
