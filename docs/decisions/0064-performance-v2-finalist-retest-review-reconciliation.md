# ADR-0064: Reconcile previous finalists on bulk-retest review import

**Status:** Accepted
**Date:** 2026-10-09

## Context

The bulk-retest control export carries forward the current `User Status` but
clears `User Rank`, because the former rank belongs to older performance
facts. The control import previously appended exactly the rows in the new
workbook. A prior `FINALIST` could therefore remain effective after a retest
without receiving a new rank; a prior finalist omitted from the new candidate
rows, such as a failed retest, also kept its old status.

## Decision

- On each accepted control import, reconcile prior effective `FINALIST` IDs
  independently within their `(Pair, Direction)` group.
- Keep `FINALIST` only when the submitted row says `FINALIST` and supplies a
  non-empty `User Rank`.
- For an existing prior FINALIST, a blank rank on a submitted FINALIST row
  demotes it to RESERVE/NULL. A supplied malformed or non-positive rank on
  that FINALIST row remains a validation error and aborts the import; it is
  not silently converted to RESERVE. The existing contract permits a
  non-prior strategy to be submitted as FINALIST without a rank; it remains
  FINALIST with NULL rank for this import and is subject to reconciliation on
  a later import.
- Preserve an explicitly submitted `REJECTED` status regardless of a stale
  positive rank remaining in the workbook; ignore that rank for duplicate-rank
  validation and persist a null rank.
- Convert every other prior `FINALIST` to `RESERVE` with a null rank, including
  IDs absent from the new candidate rows. Append these decisions to that
  group's new review import; do not edit or delete review history.
- Leave rows that were not prior finalists unchanged. Preserve the latest
  comment for a prior finalist absent from the workbook.
- Apply review rows, `REJECTED` tag synchronization and retest-tag updates in
  the existing atomic import transaction. `REJECTED` tag synchronization is
  limited to strategy IDs explicitly present in the workbook; synthesized
  RESERVE rows for omitted IDs do not clear tags.

## Consequences

- The Optimizer's next read sees only the newly ranked finalists for an
  imported group; unranked previous finalists are reserves and explicit
  rejections remain rejected.
- A review import may append a row for a prior finalist not present in that
  import's selection snapshot. The existing review ledger supports strategy
  identities independently of candidate-row membership; no schema migration
  or deletion of prior evidence is required.
- This does not derive statuses from automatic ranking, change result facts,
  or affect another pair or direction.
