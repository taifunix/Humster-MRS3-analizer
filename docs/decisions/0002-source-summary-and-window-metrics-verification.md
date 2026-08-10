# ADR-0002: Separate full-horizon source verification from windowed metrics

**Date:** 2026-08-10  
**Status:** Accepted

## Context

A v4 report's HTML summary describes the report's complete test horizon. A
v0.7 source package intentionally describes the explicitly selected UTC
half-open window `[start, end)`. Comparing a full-horizon HTML total directly
to a windowed metric therefore produces a mismatch even when both calculations
are correct. The former single `verification_status`/`metric_status` field made
that horizon difference ambiguous and could either falsely reject a valid
window or, worse, falsely call the window verified by a full-horizon total.

The source HTML also has distinct absolute and percentage labels: `Total PnL`
is not `Total PnL, %`, and `Max Drawdown` is not `Max Drawdown, %`.

## Decision

Real-independent-event packages use **package format v2**. They record two
different, conjunctive facts:

1. `source_summary_status=VERIFIED` means that 3–5 source HTML summaries were
   parsed and their **full-report-horizon** summary metrics matched an
   independently reconstructed full-report-horizon calculation. Missing HTML,
   an invalid sample count, parse failure, or any mismatch is fail-closed and
   leaves this status non-verified.
2. `window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE` means that the
   selected `[start, end)` point metrics and reconstructed event IDs were
   deterministically derived from the same immutable v4 records after the
   first fact was verified. It is provenance for windowed values, not a claim
   that an HTML full-horizon summary equals them.

The v2 manifest records both statuses, their causes and source-summary sample
evidence. Every v2 point records `window_metrics_status`; the package must not
claim it unless `source_summary_status=VERIFIED`. The selector accepts a real
v2 package only when both facts hold. It rejects a real v1 package for
selection, because v1 cannot express the distinction; such packages remain
auditable migration inputs only.

CSV `legacy_trades_proxy` package v1 remains selectable under its existing
rules: one declared legacy mode, exact package window, `PointEventCount =
TotalTrades`, and the `LEGACY_PROXY_NO_EVENT_IDS` sentinel. No package may mix
legacy proxy data and real independent events.

The HTML parser must map absolute and percentage values separately. Full-horizon
source-summary comparison uses the absolute `Total PnL` and absolute `Max
Drawdown` fields together with `Total Trades`, `Win Rate`, and `Profit Factor`;
it must never obtain either absolute field from a `%` label. Windowed PnL/DD
values retain their separate percentage columns and are not compared to a
full-horizon summary.

## Consequences

The audit makes its evidence horizon explicit. A full-horizon HTML summary is
never evidence that a bounded window is numerically equal; it is evidence that
the parser and immutable source records are trustworthy. The bounded metrics
and event mapping remain reproducible, but only a v2 real package satisfying
the two statuses can reach selector logic. This preserves fail-closed source
verification without manufacturing a false `VERIFIED` result.

This decision updates the [event source-pack specification](../specs/2026-08-10-v07-event-source-packs.md)
and is indexed by the [PRD](../../PRD.md).
