# ADR-0044: Performance v2 equity-quality facts

**Status:** Accepted
**Date:** 2026-09-25

## Context

The Pareto-and-filters panel needs an optional equity-regime filter and an
alternative equity-based Top N order. Stored equity observations can be sparse:
an inactive strategy may have a long flat segment without losing the validity
of its report. Recalculating raw curves during every preview would also slow
the existing selection path.

## Decision

Upgrade Performance v2 from schema v5 to v6 by adding one
`equity_quality_metrics` table keyed by `(result_id, algo_version)`. Store
canonical, SHA-256-checked per-result facts bound to a source revision derived
from the current import timestamp, report/effective bounds and available stored
report hash. Facts do not store a peer rank or a filter decision. Same-ID
REPLACE invalidates the row in its import transaction; prune includes it in
the existing whole-database backup and restore boundary.

Validate all owned equity observations before classifying nonpositive values;
only positive, structurally valid series produce geometric metrics. Use a
right-continuous 6-hour grid ending at each result's stored report end. A
7/14/28-day window exists only when an in-report predecessor is available at
its left boundary. Quiet gaps and terminal flat periods are carried forward;
there is no trade-frequency or six-hour freshness test. Raw observations,
including duplicate timestamps in sample-index order, determine drawdown and
peak gap. A flat selected horizon is a valid fact and blocks only when the
optional equity filter is enabled. Short-window weakness lowers position only
in the selected equity ranking method.

Exact v5 databases remain readable without migration for legacy read-only
operations. An enabled equity consumer requires v6 and complete facts; the
normal writable initializer performs migration. The existing preparation pool
and writer publish equity facts with bounded work and a source-revision recheck.
That recheck applies to the opt-in equity preparation path; the unchanged
legacy window-only path is outside this ADR's same-ID REPLACE guarantee.

The normative contract is
[Performance v2 equity quality](../specs/2026-09-25-performance-v2-equity-quality.md).

## Consequences

Selection previews can reuse verified equity geometry while changes to Top N,
method, stage order or A/B split do not invalidate it. An absent, stale or
corrupt fact is a readiness error for an enabled equity consumer, not evidence
of a poor strategy. The new policy remains opt-in, and the old ranking path
continues to use its existing metrics.
