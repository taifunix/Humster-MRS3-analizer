# ADR-0062: Clear blank selection review fields on partial import

**Status:** Accepted
**Date:** 2026-10-08

## Context

Card 6 restores `User Status` and `User Rank` from partial selection XLSX files.
Previously, blank `User Status` rows were skipped, which also retained their old
rank. A later rank could then collide with a stale rank in the same selection
run. The append-only `selection_review_rows` table also required a non-null
status, so it could not record an explicit status clear.

## Decision

- Card 6 treats each blank `User Status` and `User Rank` cell as an explicit
  clear of that field. Nonblank values in the other field are still applied.
- A cleared status is represented as SQL `NULL` in the newest review row. This
  keeps prior review history intact while making the cleared value visible as
  blank in later XLSX exports.
- Clearing `User Status` removes the strategy's current `REJECTED` tag. The
  strategy no longer receives an effective user status from that review row.
- PerformanceDB schema v10 allows nullable `selection_review_rows.user_status`.
  The v9-to-v10 migration drops only the column's `NOT NULL` constraint in one
  transaction; other stored rows are preserved.

## Consequences

- Rank uniqueness validation evaluates the submitted clears before the new
  ranks, so a cleared prior rank cannot reject a replacement rank.
- FINALIST rows may have no rank; NULL ranks are excluded from the uniqueness
  comparison.
- Blank-only workbooks with data rows are validated and recorded as imports.
- The strict full selection-review importer continues to require a nonblank
  status on every candidate row.
- Panel schema preflight advances an existing v9 database to v10 under the
  existing writer guard before serving Performance v2 operations.
- Card 9 maintenance and compact database publication accept schema v10; no
  new table or fact-data migration is introduced.
