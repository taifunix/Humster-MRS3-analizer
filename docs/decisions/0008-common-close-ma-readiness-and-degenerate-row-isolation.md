# ADR-0008: Common Close-MA Readiness and Degenerate Row Isolation

**Status:** Accepted
**Date:** 2026-08-15

## Context

The existing DuckDB coverage implementation enables a `Pair + Side + TF` scope
when one MA pair satisfies the shift-readiness contract. Real source data can
have different exact coverage intervals for Close MA values `2..7`, which makes
one longest-pair interval unsuitable for comparing all required Close MAs.

The source also contains structurally degenerate reports whose report and grid
windows are both one timestamp. Under half-open interval semantics they cover no
time, but the current global fail-closed handling aborts the complete scan.

## Decision

A source row is structurally degenerate only when
`report_start == report_end` and `grid_start == grid_end`. Such a row is
excluded before effective-window calculation and contributes nothing to
coverage chains, readiness, current CSV audits, selection, materialization, or
V2 evidence. It does not abort the scan.

Every other empty report/grid intersection remains a fail-closed structural
error, including incompatible non-empty windows and rows where only one source
window has zero duration.

For each `Pair + Side + TF`, readiness requires one continuous exact UTC
interval shared by every Close MA in `2..7`. For each Close MA, at least one
Open MA must satisfy the complete `shift_readiness_v1` sequence over the whole
interval. The selected Open MA may differ between Close MAs, but one Close MA
cannot stitch different Open MAs across subintervals.

The displayed and selectable interval is the longest qualifying common
interval. Ties resolve by earliest start, earliest end, then the ordered
`(close_ma, open_ma, witness_shift_tuple)` witness vector. The checkbox is
enabled only when all six Close MAs qualify on that interval.

Existing V1 and already-published V2 surfaces remain valid. New publications
retain `OBSERVED_SPARSE_GRID_CONTRACT_V2` and schema v4 but use
`close_ma_2_7_common_interval_v1` as the scope readiness-contract version. The
per-pair shift sub-contract remains `shift_readiness_v1`. Each scope stores one
canonical witness for every Close MA `2..7`, ordered by Close MA.

Current coverage CSV columns, statuses, reason codes, and canonical encoding
remain unchanged. Additional degenerate-row diagnostics, per-MA partial
interval reporting, and `coverage_summary.csv` remain deferred.

This ADR supersedes ADR-0007 only for per-scope witness cardinality and
readiness-version semantics.

## Consequences

- New surfaces compare every required Close MA on one exact interval.
- A valid scope may use a different canonical Open MA for each Close MA.
- Structurally degenerate zero-duration rows remain stored but cannot affect a
  surface or block unrelated scopes.
- Other empty intersections still stop the scan rather than hiding inconsistent
  source evidence.
- Runtime behavior remains on the earlier contract until the approved amendment
  is implemented and verified.
