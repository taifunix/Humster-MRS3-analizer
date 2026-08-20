# ADR-0013: Source v6 Old-Owned Overlap and Period-Local PnL

**Status:** Accepted for implementation
**Date:** 2026-08-20

**Affects:**

- [Source v6 Fresh Compact v1](../specs/2026-08-20-source-v6-fresh-compact-v1.md)
- [Source v6 and stitched surfaces](../specs/2026-08-18-source-v6-stitched-surfaces.md)
- [ADR-0010: Source v6 stitched facts and surface files](0010-source-v6-stitched-facts-and-surface-files.md)

**Does not edit:** [ADR-0011: Source v6 selected-interval boundary cycles](0011-source-v6-boundary-cycle-policy.md)

## Context

The real `Input/HTML` and `Input/my_test` comparison has 684 compatible
`>=96h` point pairs. A read-only cycle audit found 1,121 matching closed
cycles in 681 pairs, so both report windows cannot contribute the same overlap
facts. The incoming report is still required after the old report ends.

## Decision

For compatible fixed-lot reports with overlap of at least 96 hours, the old
report owns its whole interval, including the overlap. The incoming report is
used only from exact `old.report_end` onward, except that a cycle opened in the
overlap and still open at that boundary is retained as a first-cycle fact of
the new period.

1. Every incoming cycle that both opens and closes before `old.report_end` is
   excluded completely, with its actions, events, trade count, fees and PnL.
2. An incoming cycle opened before `old.report_end` and closed at or after it
   is retained. Its full realized PnL belongs to the new period; no attempt is
   made to split the transaction PnL across calendar boundaries.
3. An incoming cycle opened at or after `old.report_end` belongs to the new
   period normally.
4. The old period includes all of its closed cycles through its report end.
   Any old cycle still open at that end is excluded from the old period. Its
   period-end balance anchor is the last balance sample after the last closed
   old action and before an excluded open cycle.
5. Each period has its own balance anchor, absolute PnL and PnL percentage.
   For a new period with a retained boundary-crossing cycle, the anchor is the
   balance immediately before that cycle opens; otherwise it is the balance at
   `old.report_end`. The absolute PnL of one period is never divided by the
   ending or starting balance of another period.
6. Each period also has its own corrected equity sequence, peak and relative
   drawdown. The stitched relative drawdown is the maximum of those
   period-local relative drawdowns; an old-period absolute drawdown is never
   divided by new-period equity.
7. Profit Factor for a period is its retained closed-action gross profit divided
   by the absolute gross loss. Excluded overlap cycles and old open tails do
   not contribute. A stitched Profit Factor, when requested, uses aggregate
   retained gross profit and gross loss; period Profit Factors are never
   averaged.
8. A retained new-period balance/equity sequence must remove the known net
   effect of every excluded, fully closed incoming overlap cycle from every
   later sample. Without this correction, a balance difference from a retained
   boundary-crossing cycle would silently include PnL of an excluded cycle.
   No synthetic price, action, event or sample is created.

The persisted decision is `USE_OLD_WITH_SEAM_EXCLUSION` with diagnostic
`INCOMPLETE_SEAM_CYCLE_EXCLUDED`. It records outgoing/incoming fragment IDs,
the exact old end boundary, excluded cycle/action/event identities, retained
boundary-cycle identities, per-period anchors and the balance-sample correction
evidence.

Malformed, incompatible, non-fixed-lot, short-overlap, gapped and ambiguous
inputs remain inactive under their existing fail-closed diagnostics. The
selected-interval boundary rule in ADR-0011 is unchanged.

## Consequences and acceptance boundary

- Overlap trades cannot be double-counted.
- A boundary-crossing new trade has one complete, auditable PnL owner.
- PnL percentages remain period-local and cannot be distorted by a later
  report's different starting balance.
- Relative drawdown remains period-local and the reported stitched value is
  the maximum period value.
- Profit Factor remains based only on retained closed action PnL.
- Implementation must add focused tests for full overlap exclusion, retained
  boundary-cycle PnL, old open-tail exclusion, corrected balance samples and
  period-local percentage denominators, plus fail-closed prerequisites.
