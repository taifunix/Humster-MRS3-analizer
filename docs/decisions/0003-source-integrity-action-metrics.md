# ADR-0003: Verify source integrity through action-derived metrics

**Date:** 2026-08-10
**Status:** Accepted

**Supersedes:** the five-metric full-horizon comparison in ADR-0002.

## Context

The v4 compact payload deterministically reconstructs actions. On real source
samples its full-horizon `TotalTrades`, `WinRate`, and `ProfitFactor` agree with
the HTML summary. Absolute PnL and drawdown do not have that property: the
HTML renderer applies report-specific accounting and/or sampling not represented
by the immutable compact payload. Treating an unequal PnL/DD pair as a failed
decoder would reject valid source data; treating it as equal would be false.

## Decision

For each of 3-5 deterministic full-horizon source samples, a real package v2
must record five evidence rows:

- `TotalTrades`, `WinRate`, and `ProfitFactor` must be `EQUAL` after the
  source display precision is applied. These three action-derived metrics are
  the fail-closed source-integrity gate.
- `TotalPnL` and `MaxDrawdown` must retain their parsed HTML and reconstructed
  values for audit, but their comparison and cause are
  `NOT_COMPARABLE_WINDOW_SCOPE`. They never satisfy an equality gate.

`source_summary_status=VERIFIED` requires every sample identity, declared
full-report range, action audit linkage, exactly the three equal action metrics
and exactly the two explicit non-comparable diagnostic metrics. The loader
validates this structure and values; a label of `EQUAL` alone is not trusted.

`window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE` remains a separate
provenance fact for the requested `[start, end)` package window. It does not
turn either diagnostic PnL/DD value into MRS3 performance or a source-summary
oracle.

## Consequences

This preserves a meaningful fail-closed check of the immutable event decoder
without claiming a nonexistent equivalence for report PnL/DD. ADR-0002 still
governs the separation of full-horizon and windowed evidence; this ADR narrows
its full-horizon metric set. The active source-pack specification and PRD
index this decision.
