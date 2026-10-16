# ADR-0065: Reconcile prior finalists during Card 6 partial imports

**Status:** Accepted
**Date:** 2026-10-09

## Context

Card 6's `Импортировать статусы и ранги из XLSX` accepts partial selection
workbooks and appends review rows. A new workbook hash allows a replay, but
partial import semantics do not clear a prior FINALIST whose strategy ID is
absent from the file. The older review row then remains the effective status.
This differs from the operator's required replacement of the prior finalist
set for the imported Pair + Side.

## Decision

- Each Card 6 user-fields import reconciles prior explicit FINALIST decisions
  for the exact Pair + Side belonging to the workbook's selection run. The
  prior set is strategy-identity scoped across selection runs, not limited to
  IDs in the current run snapshot. For each identity, only its latest explicit
  non-NULL status is considered; an older FINALIST followed by REJECTED is not
  eligible for a synthesized RESERVE row.
- A prior FINALIST remains FINALIST only when its ID is submitted as FINALIST
  with a non-empty User Rank.
- An explicit REJECTED remains REJECTED and stores User Rank as SQL `NULL`,
  ignoring a stale workbook rank.
- Every other prior FINALIST, including IDs absent from the partial workbook,
  is appended as RESERVE with User Rank SQL `NULL`.
- A stale rank on a non-prior-finalist REJECTED row is also ignored and stored
  as NULL. Existing validation for other non-prior-finalist statuses remains.
- For a prior FINALIST submitted with a non-FINALIST status, the rank cell is
  stale and ignored regardless of whether its content is malformed or
  non-positive; the replacement is RESERVE/NULL. A malformed rank on a
  prior FINALIST submitted as FINALIST remains a validation error and aborts
  the import; it is not silently converted to RESERVE. A blank rank on a prior
  FINALIST submitted as FINALIST does demote it to RESERVE/NULL. The existing
  contract still permits a non-prior strategy to be submitted as FINALIST
  without a rank; it remains FINALIST with NULL rank for that import and is
  subject to prior-finalist reconciliation on a later import.
- Reconciliation and the submitted decisions are one transaction. Review
  history is append-only; no old review rows or hash-journal entries are
  deleted. Other Pair + Side values are untouched.
- A prior finalist absent from the current run's `selection_results` still
  receives an appended RESERVE review row. This changes its global effective
  user decision without creating a selection result or changing strategy facts.
- `REJECTED` tag synchronization is limited to IDs explicitly present in the
  workbook. A synthesized RESERVE row for an omitted ID does not clear tags.
- The existing combined control-XLSX import follows the same status/rank rule.

## Consequences

- The newest review row supplies the effective User Status and User Rank for
  every previous finalist in the imported Pair + Side, including absent IDs.
- Replaying identical workbook cell content with different ZIP metadata still
  appends a new review event; it does not rewrite the old event.
- No schema migration is required. Generated RESERVE rows use the existing
  `selection_review_imports` / `selection_review_rows` append-only ledger.
- A previous finalist explicitly REJECTED by the user stays REJECTED rather
  than being demoted to RESERVE.
