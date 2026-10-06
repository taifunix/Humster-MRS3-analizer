# ADR-0058: Rejected status for automatic selection filters

Date: 2026-10-06. Status: accepted by user instruction.

## Decision

On publication of a Performance v2 selection snapshot, a strategy actually
excluded by an enabled Lot variant redundancy, Hard performance cutoffs, or
A/B deterioration filter receives the existing durable `User Status=REJECTED`
tag. The tag source identifies the filter and its source reference is the
selection run ID. A skipped or passing row receives no tag from that filter.

The existing explicit review flow remains the way to change a durable status.
Automatic filter tags do not delete strategies, change their lifecycle, or
rewrite previous selection snapshots. The equity `RESERVE` classification
remains available for review, and stage counters count every row that does not
continue, including reserved rows, as excluded from the next stage. The
`reserved` counter is a subset of `eliminated` and must not be added twice.
