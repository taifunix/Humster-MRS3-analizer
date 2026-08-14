# Real Events And Phase 2 Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build immutable `DUCKDB_DIRECT` surfaces with real reconstructed event memberships and provide interactive exact-behavior Phase 2 filtering with deterministic XLS audit.

**Architecture:** Reuse `reconstruct_closed_cycles` during direct materialization and persist event membership in analysis schema v4. Load memberships into the existing pipeline, calculate exact behavior groups in a focused shortlist module, and expose non-destructive filter/audit operations through the existing local panel.

**Tech Stack:** Python 3.11+, DuckDB, pandas, openpyxl, stdlib HTTP panel, pytest.

## Global Constraints

- `Source PnL` is compared only per corresponding ordered order; it is never summed or averaged.
- Exact `BehaviorKey` is mandatory and no fuzzy comparison, weighted score, or arbitrary Top-N is allowed.
- Existing immutable `legacy_trades_proxy` surfaces remain readable and are never rewritten.
- New real-event builds use a bumped materializer contract and a distinct surface identity.
- Filtering never mutates candidates stored in an analysis run.

---

### Task 1: Real Events In Direct Materialization

**Files:**
- Modify: `src/mrs3/duckdb_events.py`
- Modify: `src/mrs3/duckdb_direct.py`
- Test: `tests/test_duckdb_direct.py`
- Test: `tests/test_source_packs.py`

**Interfaces:**
- Produces: canonical `event_id(symbol, position_side, timeframe, opened_at)` helper shared by source-pack and direct paths.
- Produces: `DirectPoint.event_ids: tuple[str, ...]` and `DirectSurface.event_mode == "real_independent_events"`.

- [x] Write tests proving equivalent UTC timestamp spellings have one event ID, direct points carry unique reconstructed cycle IDs, and `point_event_count != TotalTrades` is allowed.
- [x] Run focused tests and verify failures are caused by the legacy proxy implementation.
- [x] Reuse `reconstruct_closed_cycles` in direct materialization, calculate `point_event_count` from unique IDs, and bump direct materializer identity inputs.
- [x] Run focused tests until green.

### Task 2: Immutable Event Membership Storage

**Files:**
- Modify: `src/mrs3/analysis_storage.py`
- Modify: `src/mrs3/published_surface.py`
- Test: `tests/test_analysis_storage.py`
- Test: `tests/test_published_surface.py`

**Interfaces:**
- Produces: analysis schema v4 table `surface_point_events(surface_id, canonical_point_key, event_id)`.
- Produces: published pipeline points containing `_event_ids`, `event_mode`, `event_ids_hash`, and exact `point_event_count` without source DB access.

- [x] Write migration and round-trip tests for real memberships plus legacy fallback.
- [x] Run focused tests and verify schema/round-trip failures.
- [x] Add transactional v3-to-v4 migration, include `event_mode` in surface identity/storage, persist memberships, and load them with integrity checks.
- [x] Run schema, storage, published-surface, and pipeline tests until green.

### Task 3: Exact-Behavior Phase 2 Engine And XLS

**Files:**
- Create: `src/mrs3/analysis_shortlist.py`
- Modify: `src/mrs3/analysis_strategies.py`
- Test: `tests/test_analysis_shortlist.py`
- Modify: `tests/test_analysis_strategies.py`

**Interfaces:**
- Produces: `filter_analysis_candidates(connection, run_id, criteria) -> FilterResult`.
- Produces: `export_filter_audit(connection, run_id, criteria, output_path) -> Path`.
- Consumes criteria names `source_pnl`, `efficiency`, `close_support`, `point_event_count`.

- [x] Write tests for exact BehaviorKey, per-order vector dominance, trade-offs, deterministic dominator selection, PointEventCount no-op invariant, and non-mutation.
- [x] Run tests and verify missing engine failures.
- [x] Implement canonical behavior key and minimum deterministic Pareto engine.
- [x] Write XLS tests for ordered sheets, fixed headers, per-criterion exclusions, and combined exclusions.
- [x] Implement workbook export with existing `write_audit_workbook`/openpyxl support and run focused tests until green.
- [x] Restrict JSON generation to selected candidates that remain `READY_AFTER_FILTERS` for the supplied criteria.

### Task 4: Panel Integration And Verification

**Files:**
- Modify: `src/mrs3/panel.py`
- Modify: `tests/test_panel.py`
- Modify: `progress.md`

**Interfaces:**
- Adds: `/api/analysis/shortlist` criteria payload and filter-status response.
- Adds: `/api/analysis/filter-export` returning the generated workbook path.

- [x] Write panel tests for four independent checkboxes, READY/deferred rendering, criteria payloads, filter counts, and XLS export endpoint.
- [x] Run panel tests and verify markup/API failures.
- [x] Add controls and rendering while preserving manual candidate selection.
- [x] Run all focused suites, then the full test suite with the available project interpreter.
- [x] Run `git diff --check`, inspect scope, update `progress.md`, and obtain independent review before committing implementation.
