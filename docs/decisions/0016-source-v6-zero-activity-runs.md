# ADR-0016: A tester run with no trades is evidence, not a parse failure

**Status:** Accepted for implementation
**Date:** 2026-08-22
**Amends:** [ADR-0012](0012-source-v6-fresh-compact-v1.md)

## Context

The tester emits a complete report for a run in which no trade occurred: all
settings, all metric tables, the action table with its header, and
`const walletSeries = [];` / `const equitySeries = [];`. The v6 importer
rejected these because `_series` required a non-empty array, so they were
quarantined as parse failures.

On the Debian corpus `my_test_CX_GE_fixed` that was 144 of 38,305 reports. The
consequence was not only 144 missing points: `safe_to_delete` stays `NO` while
any report is quarantined, so the whole corpus of raw HTML was pinned on disk
by reports that were never defective.

One inspected example, `my_test_run_30577_of_38304_CXMTUSDT_5m_2026-07-29.html`,
is 1,183,513 bytes and carries 41 metrics including `Report range`,
`Total Trades = 0`, `Total transactions (buy/sell) = 0`, all monetary totals at
zero, `Initial = Final = Min = Max balance`, and `n/a` in exactly the seven
ratios that are undefined without trades.

The v6 fragment interval already comes from the `Report range` header
(`_report_period`), not from sample timestamps, so an empty fragment has a
well-defined half-open interval with no new derivation.

## Decision

A Source v6 fragment may contain zero actions, zero cycles, zero events and
zero samples, but only on affirmative evidence of emptiness. Absence of data is
never sufficient: a truncated or corrupt report also has no data.

Admission requires all of:

1. Structural completeness — exactly one settings block, the metric tables, and
   the action table with its header, agreeing with the independent raw-markup
   inventory. Unchanged; already enforced.
2. An explicitly empty array, not a missing assignment. `exactly one X
   assignment is required` remains an error; only "must be non-empty" is
   relaxed.
3. `Total Trades` and `Total transactions (buy/sell)` present and zero. Missing
   is a parse failure, not an empty run.
4. Corroborating metrics, where present, consistent with emptiness: monetary
   totals, volume, fees and drawdown zero, and the four balance metrics equal.
5. The ratios undefined without trades — expectancy, profit factor, risk/reward,
   recovery, Sharpe, Sortino, Calmar — present as the literal `n/a` where they
   are present at all. This follows [ADR-0006](0006-undefined-profit-factor-contract.md):
   `n/a` is preserved as meaning, not converted to a number.
6. `Report range` present and well formed.
7. Zero actions, zero wallet samples and zero equity samples together. A report
   with trades but no samples, or samples but no trades, stays a failure.

Consequences of admitting one:

- Its `day_ownership` rows carry ownership `ACTIVE_EMPTY` rather than `ACTIVE`,
  so coverage can never silently read a window with no trading as a window with
  trading. The days are owned: "this window was tested and nothing happened" is
  the result, and withholding it would leave a permanent artificial gap.
- Overlap resolution needs two rules, because the existing resolver does not
  ask whether a fragment holds anything. An earlier revision of this ADR
  claimed it needed none; that was wrong in both directions and is corrected
  here.

  Seam exclusion (ADR-0013) exists to stop one fact being counted twice — it
  measured 1,121 matching closed cycles across 681 overlapping pairs and gives
  the overlap to the old report because it already carries them. A
  zero-activity outgoing carries nothing, so the premise is absent and the
  exclusion would delete incoming evidence that nothing replaces, under a
  batch reported `COMMITTED`. Reproduced against the repository's own
  fixtures. An empty outgoing therefore hands over at the incoming's own
  start — the existing non-seam rule — as `RESOLVED` with
  `EMPTY_OUTGOING_NOTHING_TO_EXCLUDE`. Every closed incoming cycle survives; an
  incoming open tail does not, because the non-seam path never kept one. The
  mirror image needs its own rule: an empty *incoming* cannot continue an
  outgoing open tail, which ADR-0010 already names `BRIDGE_NOT_COVERED` and
  routes to `PARTIAL`.

  For an identical window, `resolve_batch` seeds ownership with the first
  fragment by `(report_start_ms, fragment_id)`, so hash order could hand the
  window to the empty fragment and flag the one holding real actions as
  `AMBIGUOUS_INCOMING`. Emptiness is now the second sort term, so a fragment
  that observed something always wins that tie. The batch still reports
  `PARTIAL`: the disagreement is surfaced, not resolved silently.
- `PerformanceInventory.minimum_timestamp`/`maximum_timestamp` come from
  `Report range` when nothing was observed. Never a sentinel: a zero epoch would
  propagate into sorts, interval comparisons and the v1 `test_run_id`.

## Scope

Zero-activity admission is opt-in per caller, and only `normalize_source_v6`
opts in. The v1 performance store (`performance_import`) keeps rejecting, so
[ADR-0006](0006-undefined-profit-factor-contract.md)'s DD5 candidate contract
and the strategy-performance evidence store are untouched by this ADR.

## Consequences

- The 144 zero-activity reports of `my_test_CX_GE_fixed` import as COMMITTED,
  and `safe_to_delete` is no longer held at `NO` by them.
- The 145th quarantined file, an optimizer summary page, still fails criteria 1
  and 3 and stays quarantined. The criteria discriminate rather than admit
  everything empty.
- Re-importing that corpus is required to gain the 144 points; the existing
  artifact is not migrated.
