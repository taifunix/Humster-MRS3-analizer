# ADR-0054: Performance v2 close-balance window boundaries

**Status:** Accepted
**Date:** 2026-10-02

## Context

The tester may omit equity samples at intermediate full closes while retaining
their post-action Balance in Trades.  Choosing window boundaries only from flat
equity samples can collapse an active A/B window.  It also omits the initial
flat sample before the first trade from a full-report window.

## Decision

Use ordered full-close action Balance and recorded initial balance for window
PnL boundaries.  Exclude a trade open at the start until its first full close,
and exclude an unfinished trade at the end.  Use recorded equity samples for
drawdown; missing DD evidence stays nullable.  Apply this one rule to A, B and
Full.  Version the cached calculation so old facts are never reused.
The imported action vocabulary has `opened`, `increased`, `decreased`, and
`closed`; all 6,433,365 recorded actions with `post_size=0` are `closed` in the
read-only local PerformanceDB. The importer also accepts a `decreased` action
that flattens exposure, so either `closed` or `decreased` with `post_size=0`
is a full-close boundary. A nonpositive baseline is unavailable because
percentage growth and drawdown cannot be anchored to it.
Import uses `max(reported start, listing + 120 hours)` as the effective start
and retains source action ordinals. When the first retained ordinal is nonzero,
rebase raw Balance and equity observations by one constant derived from the
first retained opening action's pre-action Balance and stored initial capital.
The source rows remain unchanged. This keeps warm-up PnL out of A and Full.

## Consequences

Previously cached windows require recalculation.  A and B returns need not add
to the Full return because a trade crossing their split is excluded from each
subwindow.  No source report, database schema or tester result is changed.
