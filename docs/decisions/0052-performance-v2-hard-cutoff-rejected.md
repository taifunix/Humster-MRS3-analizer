# ADR-0052: Automatic REJECTED from the hard cutoff block

Status note (2026-10-01): the standalone `DD > 23` predicate below is
superseded in part by [ADR-0053](0053-performance-v2-dd-profit-guard.md).
The REJECTED provenance and publication decision remains in force.

Date: 2026-10-01. Status: user decision recorded; implementation contract pending.
No runtime change or independent implementation approval is claimed.

## Context

The filter-review discussion assigns the unified hard cutoff block to position 3,
after Equity and Lot variant, before A/B deterioration and top-five concentration.
The user requests persistent REJECTED marking for strategies excluded by this
block, meaning a candidate for deletion.

Existing selection review separates numeric `User Rank` from `User Status`.
Durable rejection is represented by `strategy_tags` with tag `REJECTED`;
manual decisions are recorded separately in immutable review rows.

## Decision

A strategy actually excluded by an enabled hard cutoff block receives a durable
REJECTED tag and effective User Status REJECTED. Numeric User Rank retains its
existing contract. A missing metric, skipped evaluation, or elimination by a
different stage does not create this automatic rejection.

The block independently checks full DD >23%, full PnL/30 <=4%, or both
full/B PnL/30-to-full-DD ratios <0.75. The ratio retains the researched
45-day and 25-completed-cycle applicability guards. Exact rules and boundary
cases are recorded in the linked filter plan.

Automatic provenance must be distinct from manual `SELECTION_REVIEW` provenance,
with selection/result references, configuration and the violated rules/values.
An older manual status must not hide a newly applied automatic rejection.
No fabricated manual review or rewrite of historical snapshots is permitted.

REJECTED does not delete the strategy or mark its lifecycle DISCARDED. A later
explicit operator status change may clear the tag through the existing review
workflow. Disabling the filter or changing its thresholds does not silently
clear durable rejection. Repeated publication must not duplicate the tag.

Before implementation, the active specification must settle the write trigger
(automatic preview versus durable selection publication), effective-status
precedence and source revision checks. The tag and its selection evidence must
be committed atomically. This discussion does not bulk-tag research results.

## Supersession and consequences

This decision extends the operator-only REJECTED origin described by the
[selection-review contract](../specs/2026-09-02-performance-v2-selection-review-import.md)
to include the unified hard cutoff block. Other status/rank, review-import,
snapshot and deletion contracts remain applicable. The new behavior requires
an active-spec amendment and verified implementation before use.

## Evidence

- [User decisions and target order](../superpowers/plans/2026-10-01-performance-v2-filter-review.md).
- [Read-only threshold research](../reports/2026-10-01-performance-v2-hard-cutoff-research.md).
- Current representation: `src/mrs3/performance_v2_store.py` and
  `src/mrs3/performance_v2_selection_review.py`.
