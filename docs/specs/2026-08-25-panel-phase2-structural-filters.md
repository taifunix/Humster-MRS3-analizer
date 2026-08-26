# Phase 2 structural filters in Panel Web

**Status:** Proposed
**Date:** 2026-08-25
**Depends on:** [event filter and shortlist](v07-event-filter-and-shortlist.md),
[panel DD5 frontend tuning](2026-08-24-panel-dd5-frontend-tuning.md),
[BASE 1ORD selection](2026-08-24-base-1ord-selection.md)

## Goal

Restore the four Phase 2 structural filters in the fresh Panel Web shortlist and
make their current non-mutating view the sole source for READY JSON generation.

## Scope

- The Shortlist card follows the approved mockup: Refresh and audit actions,
  a permanently visible bold `Phase 2 structural filters` block, scope
  selection actions, then the
  grouped Pair/Side/TF table.
- With active filters, `1ORD` through `4ORD` count only candidates remaining
  READY; `DEFERRED` and `ALL` retain the unfiltered context.
- The checkboxes are `source_pnl`, `efficiency`, `close_support`, and
  `point_event_count`; unchecked is the default.
- `POST /api/v2/strategies/fresh/shortlist` accepts all four boolean flags and
  returns grouped counts plus candidate-level filter status and audit facts.
- `POST /api/v2/strategies/fresh/generate` accepts the run, active filters and
  selected scopes. The server recomputes the view and generates only
  `READY_AFTER_FILTERS`; browser candidate IDs are not authoritative.
- Filter audit XLS is produced from the same server-side result.

## Invariants

- Phase 2 reads immutable fresh analysis facts and never changes the analysis
  artifact, Source DuckDB schema/payload, materializer, selection or analysis.
- Filtering requires `event_mode=real_independent_events`, validates exact
  point event membership/counts, and fails closed otherwise.
- Comparison is only within exact `Pair + Side + TF + OrderCount + CommonCloseMA`.
  It is order-position Pareto dominance with deterministic dominator selection,
  as defined by `v07-event-filter-and-shortlist.md` §24.2.
- No enabled filters means every persisted `READY_MRS3_STRUCTURE` is
  `READY_AFTER_FILTERS`.
- Generated manifest records active criteria and selected scopes.
- The audit workbook contains `Summary`, `READY_AFTER_FILTERS`, one sheet per
  enabled criterion, and `DEFERRED_COMBINED`.

## Non-goals

- No new frontend framework, database migration, config threshold, weighted
  ranking, Top-N cutoff, portfolio calculation or source-metric reclassification.

## Acceptance evidence

1. Each checkbox independently defers only a dominated candidate in the same
   comparison key; incomparable candidates remain READY.
2. Combined criteria require one dominator that is no worse in every enabled
   order-level value; `deferred_by` is deterministic.
3. The API and generator reject unsupported/mixed event modes and malformed
   filter payloads.
4. Generation cannot include a deferred candidate even if the browser sends its
   ID.
5. The static panel renders the approved control order and grouped table, and
   the XLS workbook contains the required sheets.
6. Phase 2 filters do not collapse; their labels have vertical spacing, and TF
   selection checkboxes are indented beneath their Pair/Side group.
7. The Phase 2 title uses the Shortlist title typography; Pair/Side controls
   use the same checkbox size for group and TF rows, plus a vertically centred
   triangle disclosure arrow.
8. Changing any Phase 2 checkbox immediately refreshes the Shortlist through
   the same server-side filtered view as the Refresh action.
9. A filtered group has `sum(1ORD..4ORD) = READY` for canonical order buckets;
   `DEFERRED` plus `READY` reconciles with `ALL`.
10. A failed READY JSON job reports its actionable cause but never exposes a
    local filesystem path.
11. A completed tester row with an empty `chartUrl` is accepted only when its
    fresh, embedded-name-verified snapshot HTML is available; a reused tester
    `runId` does not discard that fresh snapshot.
