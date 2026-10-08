# Fresh Shortlist Minimum Shift Implementation Plan

Status: `PLAN_APPROVED` by independent Advisor (Opus), 2026-10-08.

## Goal

Add an optional Minimum Shift filter to the fresh Shortlist and READY JSON
flow, without changing Performance v2 or historical artifacts. The gate checks
only the first order Shift; later orders in 2ORD/3ORD remain subject to the
existing strictly increasing Shift construction rule.

## Ordered work

1. Capture an engine-1 disabled baseline fixture and historical READY/RUNS
   compatibility fixtures.
2. Add one canonical parser/serializer in `src/mrs3/fresh_shortlist.py`.
3. Add the post-ladder, pre-Pareto gate and `DEFERRED_MIN_SHIFT` result.
4. Thread the named settings through Panel, audit/export, READY generation,
   and tester-run provenance while preserving the three-boolean tuple.
5. Add the unchecked UI checkbox and percentage input, applied-snapshot and
   stale-token behavior, and visible errors.
6. Run focused and related tests in `.venv` with temporary files on C:, then
   perform diff checks and independent implementation review.

## Constraints

- no live database/tester/cache operation;
- no source or PerformanceDB schema migration;
- no rewrite of engine-1 artifacts;
- retain unrelated working-tree changes;
- one scoped conventional commit only after `CODE_REVIEW_PASS`.

## Done when

The focused suites, static JavaScript check, and diff check pass; disabled
behavior is field-for-field compatible; enabled threshold behavior, provenance,
audit, READY JSON, and RUNS are covered; Opus returns `CODE_REVIEW_PASS`; the
Panel reload requirement is documented.
