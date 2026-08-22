# Source v6 zero-activity runs — specification

**Status:** Implemented
**Date:** 2026-08-22
**Decision:** [ADR-0016](../decisions/0016-source-v6-zero-activity-runs.md)

## Problem

`_series` requires a non-empty array, so a complete tester report describing a
run with no trades is quarantined as a parse failure. On `my_test_CX_GE_fixed`
that is 144 of 38,305 reports, and it holds `safe_to_delete` at `NO` for the
whole corpus, pinning 36 GB of raw HTML that nothing is wrong with.

## Goal

Import such a run as a fragment with zero facts, admitted only on affirmative
evidence, without weakening what a parse failure means.

## Non-goals

- Changing the v1 performance store or the DD5 candidate contract.
- Migrating existing artifacts. Re-import is required.
- Inventing values for undefined ratios (ADR-0006 already settles that).
- Any change to how non-empty reports are parsed.

## Contract

### Z1 — Emptiness must be declared, never inferred from absence

`parse_performance_report(source, *, allow_zero_activity=False)`. Only
`normalize_source_v6` passes `True`.

With the flag set and the report carrying zero actions, zero wallet samples and
zero equity samples, `_assert_declared_empty(metrics)` must pass. It requires:

- `Total Trades` and `Total transactions (buy/sell)` present and zero. Absent
  is a failure — a report that does not say how many trades it had has not
  declared anything.
- Every corroborating metric that is present is consistent with emptiness:
  `Win Trades`, `Los Trades`, `Total PnL`, `Gross profit`, `Gross loss`,
  `Trading volume (USDT)`, `Total fees`, `Max Drawdown` parse as zero, and
  `Initial`/`Final`/`Min`/`Max balance` are all equal.
- Every undefined ratio that is present is the literal `n/a`: expectancy,
  profit factor, risk/reward, recovery factor, Sharpe, Sortino, Calmar.

Present-and-contradictory fails; absent is tolerated for the corroborating set
and fatal for the two counters. The split is deliberate: the counters are what
directly contradict emptiness, and requiring sixteen exact metric labels would
make the importer brittle against a tester rename.

The `n/a` requirement is the strongest single signal, because a report whose
series failed to render for an unrelated reason would still carry computed
ratios, not `n/a` in exactly the seven positions where the denominator is zero.

### Z2 — Zero must be zero everywhere at once

Zero actions, zero wallet samples and zero equity samples together, or the
report is a failure. A report with trades but no samples is not a zero-activity
run; it is a report whose samples did not render.

`exactly one X assignment is required` is unchanged. Only "must be non-empty"
is relaxed, and only for an array that is explicitly and syntactically `[]`.

### Z3 — The interval comes from the header

Already true: `_report_period` reads `Report range`, and
`_effective_report_period` only extends the endpoint when an observed sample
sits on it — which cannot happen with no samples. No new derivation.

`PerformanceInventory.minimum_timestamp`/`maximum_timestamp` take the header
range when nothing was observed. Not a sentinel: a zero epoch would propagate
into interval comparisons, sorting and the v1 `test_run_id`.

### Z4 — Empty days are owned, and marked

`day_ownership.ownership` is `ACTIVE_EMPTY` for a fragment with no facts,
`ACTIVE` otherwise. The days are owned because "this window was tested and
nothing happened" is the run's entire informational content, and withholding it
would leave a permanent artificial gap in coverage. They are marked because a
consumer that treats an empty window as traded data would be wrong in the other
direction.

No consumer reads the column yet. It is written now because the distinction is
part of the contract the moment empty fragments exist, and retrofitting it onto
published artifacts would need a migration.

### Z5 — An empty fragment must never take facts from one that has them

The resolver does not ask whether a fragment holds anything, so admitting empty
fragments needs two rules. An earlier revision of this section claimed none
were needed; both halves of that were wrong, and both were reproduced against
the repository's own fixtures before being fixed.

**At a seam.** ADR-0013 excludes an incoming overlap cycle because the outgoing
report already carries the same fact — its evidence is 1,121 matching closed
cycles across 681 overlapping pairs. That premise is de-duplication. A
zero-activity outgoing has nothing to de-duplicate, so the exclusion deletes
incoming evidence nothing replaces: merging the zero-activity fixture with
`source_v6_fixed_lot_overlap_b.html` excluded that fragment's only cycle and
reported the batch `COMMITTED`. `resolve_ownership` now returns `RESOLVED` with
`EMPTY_OUTGOING_NOTHING_TO_EXCLUDE` when the outgoing carries no facts, handing
over at the incoming's own start under the existing non-seam rule. Every closed
incoming cycle stays active — and an earlier revision claimed *every* incoming
fact does, which is wrong: the non-seam path keeps an incoming cycle only if it
is closed, so an incoming open tail is dropped where the seam path would have
kept it. That asymmetry belongs to the pre-existing `RESOLVED` semantics and is
recorded rather than changed here.

**Mirrored: an empty incoming.** The first version of this rule looked only at
the outgoing, and the mirror image destroys facts the same way: seam exclusion
drops the outgoing's open tail because the incoming is assumed to carry its
continuation, and an empty incoming carries nothing — one cycle, two actions and
two events deactivated under a `COMMITTED` batch, reproduced with
`source_v6_fixed_lot_overlap_a.html` against the shifted zero-activity fixture.
ADR-0010 already names the outcome: "a fragment whose start cannot cover the
outgoing tail cycle is marked `BRIDGE_NOT_COVERED` ... contributes to
`PARTIAL`". `resolve_ownership` now returns that when the incoming carries no
facts and the outgoing has an open tail. An `UNRESOLVED` decision writes no fact
rows, so the outgoing keeps everything.

That shape was unreachable from real data before this spec, because a report
with no facts was quarantined. `test_task3_uncovered_tail_is_partial_with_
automatic_reason` constructed it synthetically and asserted `COMMITTED`, which
contradicted both its own name and ADR-0010; it now asserts `PARTIAL`.

**At an identical window.** `resolve_batch` seeds `active` with the first
fragment by `(report_start_ms, fragment_id)`, so the tie was decided by hash
order: the empty fixture took the window from
`source_v6_fixed_lot_overlap_a.html` and the fragment with four real actions was
the one reported `AMBIGUOUS_INCOMING`. Emptiness is now the second sort term.
Among fragments that all carry facts the term is constant and the order is
unchanged.

The batch still reports `PARTIAL` for the identical-window case. The rule
decides who owns the window; it does not claim the disagreement is resolved.

`_carries_facts` answers `True` for `SourceV6FragmentMetadata`, which has no
fact collections — the behaviour that predates this rule. That is never the
deciding answer: both places the rule changes an outcome need two or more
members, and the merge decodes before resolving those.

Both orderings share `_batch_order_key`. `persist_batch_resolution` re-derives
the outgoing side by bisecting its own ordering, so a second sort key here
without the same key there would persist a decision against a different
outgoing fragment than the one it was computed from whenever two fragments
share a start.

"Carries facts" here means actions, cycles or events — not samples. A fragment
with a rendered wallet series but no trades therefore takes these rules too,
even though it parses without `allow_zero_activity`. That is deliberate: such a
fragment has the same problem, since the seam de-duplicates cycles and it has
none. It also means the Z5 rules are not gated by the parse flag; the invariant
below is about the parse path only.

## Invariants

- A report failing any Z1 criterion is quarantined exactly as before.
- Non-empty reports take a byte-identical *parse* path; `allow_zero_activity`
  gates only the empty case there. The Z5 resolver rules are not gated by it —
  they key on whether a fragment carries trade facts, so they also apply to
  pre-existing fragments that have samples but no trades.
- `fragment_id` remains `sha256(canonical)`. Two empty fragments differing in
  point or settings fingerprint stay distinct, because both are in the
  canonical document.
- Fragments remain lossless: an empty fragment reconstructs to zero facts.

## Acceptance evidence

- The synthetic fixture is admitted and parses to zero facts with the header
  interval. The real 1,183,513-byte report was checked by hand outside the
  suite — point `CXMTUSDT|LONG|5m|430|SMA|ohlc4|3|SMA|ohlc4|2`, interval
  2026-07-29 to 2026-08-18, `STITCHABLE_FIXED_LOT`, all five fact counts zero —
  and is not committed, so the suite cannot assert it.
- `Total transactions (buy/sell)` renders as a bare `0` in that report, checked
  at byte level. A composite rendering such as `0 / 0` would fail `_zero` and
  make the criterion reject every zero-activity run; it does not.
- Each Z1 criterion is pinned by a report that violates only it.
- `ACTIVE_EMPTY` is written for an empty fragment and `ACTIVE` for a normal one,
  on both the SQL writer and the Python helper.
- Z5 is pinned in both shapes: the seam and the identical window.

## Verification

`.venv\Scripts\python.exe -m pytest -q`. No `Input/`, `Output/`, `Data/`, HTML,
DuckDB or generated artifact may be committed.
