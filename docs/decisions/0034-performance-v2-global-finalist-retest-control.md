# ADR-0034: Global finalist retest and control workbook

**Status:** Accepted
**Date:** 2026-09-09

## Context

Performance DB v2 can retest strategies tagged `RETEST` and can review one
Pair + Direction per XLSX. Portfolio Optimizer needs a repeatable way to refresh
all current user finalists and audit the resulting FINALIST/RESERVE set without
managing one workbook per pair. The optimizer must continue to consume the
ordinary current effective FINALIST set; a second notion of “final” would create
two conflicting sources of truth.

## Decision

Extend the existing RETEST, REPLACE and selection-review contracts:

1. A bulk action freezes either all current effective FINALIST strategies or
   FINALIST plus RESERVE when the adjacent checkbox is enabled.
2. The cohort runs through the existing native SINGLE_MODE and REPLACE path
   with one common end, listing-aware effective starts and standardized tester
   settings. Successful results replace current Performance facts; no parallel
   result history is introduced.
3. One control XLSX contains all selected Pair + Direction groups. Automatic
   selection facts remain immutable and separate from editable user decisions.
   User ranks are unique only inside their Pair + Direction group.
4. Post-retest selection uses a distinct `RETEST_COHORT` scope resolved
   server-side from the completed bulk job. Each Pair + Direction run contains
   only successfully imported cohort members; unrelated ACTIVE rows and failed
   imports cannot affect filters, ranks, counters or Top N. Existing ordinary
   pair selection continues to use its full ACTIVE population.
5. The workbook reuses the existing immutable selection runs, one per group,
   and imports all group reviews atomically. It can also set existing RETEST
   tags for a later selective refresh.
6. Portfolio Optimizer keeps reading current effective `User Status = FINALIST`
   and receives no cohort-specific or “final set” flag.

## Consequences

- The operator can refresh and review many pairs with one tester batch and one
  workbook.
- FINALIST and RESERVE remain reversible user decisions; automatic ranking does
  not overwrite them.
- Existing single-group review and tag-driven RETEST remain supported.
- Bulk inbox reuse must include cohort, period and config digests.
- Every post-retest selection run records its bulk job, exact successful
  Strategy/Result IDs, manifest digest and filter-config digest.
- Multi-group export/import adds orchestration around existing selection runs,
  but no second status system or result database.

## Rejected alternatives

- Store retest copies separately: the operator chose current-result replacement
  and does not need duplicate result history.
- Mark every finalist with a temporary RETEST tag: this conflates a bulk scope
  with explicit per-row review intent.
- Create a special optimizer-final status: it would diverge from the current
  effective FINALIST source of truth.
