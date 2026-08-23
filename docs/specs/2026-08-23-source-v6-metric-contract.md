# Source v6 metric contract — specification

**Status:** Approved for implementation (v2 fresh rebuild)
**Date:** 2026-08-23
**Depends on:**
[ADR-0006](../decisions/0006-undefined-profit-factor-contract.md),
[ADR-0016](../decisions/0016-source-v6-zero-activity-runs.md),
[ADR-0017](../decisions/0017-source-v6-facts-and-metrics-v2.md),
[empty result combinations](2026-08-22-source-v6-empty-result-combinations.md)

## Why this exists

Two surfaces published on 2026-08-22 declared **every one** of their 4,746 and
5,472 parameter combinations an empty result, over reports holding thousands of
samples and hundreds of closed positions. Nothing caught it, because nothing
compared what we computed against what the tester declared.

Investigating that produced a second finding: several metric formulas disagreed
with the tester, and in one case by a factor of thousands. This document fixes
every metric formula against evidence from real reports and the real corpus, and
fixes which declared values are verified after import.

Measurement basis, unless a clause says otherwise: 12 reports in `Input/reports`
and 300–400 randomly sampled fragments of `my_test_NVDL_TSLL_fixed_0.997net`,
which is 100% `STITCHABLE_FIXED_LOT`.

## M1 — A cycle is one position, not one order

`Order ID` identifies the fills of a single order. A position routinely opens
under one order id and closes under another: in the reference report the opening
and closing order ids **never once coincide** (259 distinct opening ids, 262
closing, overlap 0), and the ids are a running counter.

Grouping cycles by order id therefore split every position in two — the half
holding the `closed` action looked like a complete cycle, the half holding
`opened` like a position that never closed. In the reference report that
produced 340 phantom open cycles beside 262 real closed ones. The earliest
phantom then became the open-tail cutoff, and since it coincided with the first
wallet sample, the visibility filter hid the entire series. That is the whole
mechanism behind the two false-zero surfaces.

**A position is open while `Post Size` is above zero and closed the moment the
tester states it is flat** — `Post Size` returning to zero, or an action named
`closed`. Both are needed: the column is the authority where it exists, and
older report layouts omit it entirely. Taking only the column silently left
those positions open, which suppressed the seam correction that removes an
incoming report's overlap trades — caught on the overlap fixtures, where the
stitched PnL rose from 5.8 to 6.7 by importing a trade the contract discards.

An episode may begin without an `opened` action, because the position can have
been opened before the report started. An episode the tester never declares flat
is a genuine open tail, which the visibility filter exists for.

Verified on the reference report: 381 positions, **zero** unterminated, against
381 `closed` actions.

## M2 — Trades, events and the unit of a decision

Four different counts exist in the data and each answers a different question.
For the worked example — enter 50, increase 50, partially close 50, re-enter 50,
close 100:

| Unit | Example | Reference report | Answers |
| --- | --- | --- | --- |
| Entry fills (`opened`+`increased`) | 3 | 693 | how often the book filled us |
| Realising actions (`closed`+`decreased`) | 2 | 787 | how often PnL was booked |
| Position (entry → flat) | 1 | 381 | how often capital was deployed |
| **Round trip** | **2** | **384** | **how many decisions the combination made** |
| Exposure-weighted | 1.5 | 382.02 | how much of a full trade was realised |

A **round trip** is one maximal run of entry actions followed by one maximal run
of realising actions. It is the unit of a decision: the price reached the
opening trigger, then the closing trigger.

- `total_trades` = round trips.
- `point_event_count` = round trips, and `_event_ids` identify round trips.
- `weighted_trades` = Σ (size realised in the round trip ÷ peak position size of
  its position), stored as a diagnostic.

The tester's own count is `closed + decreased`, confirmed on **257/257** corpus
fragments. It is not adopted, because the number of realising actions depends on
order-book liquidity at that moment, not on strategy behaviour: across 60 CXMT
points the ratio of realising actions to positions ranges from **1.0 to 3.0**.
MRS3 ranks points against each other, so counting fills rewards small shifts and
illiquid pairs for being filled in pieces. Plateau detection is the project's
central task, and it must not be biased by execution granularity.

The declared value is not stored: the formula is recorded here so a future
reader does not mistake the divergence for an import defect.

Round trips are derived only after seam exclusions and the selected window, from
actions ordered by `(timestamp_ms, action_id)`. Entries are `opened` and
`increased`; realisations are `decreased` and `closed`. A leading maximal
realisation run emits one round trip with an empty `entry_action_ids` tuple: it
represents a position opened before the visible window, without inventing an
entry action. An entry-only tail emits no round trip. A new entry after a
realisation begins a new round trip. Its timestamp is its first realisation and
its id is SHA-256 of canonical JSON
`{version, point_key, entry_action_ids, realizing_action_ids}`.

## M3 — Win rate

`win_rate = wins / (wins + losses)` over **round trips**. A round trip wins when
its summed realised PnL is above zero, loses when below. Round trips at exactly
zero are counted in neither and are excluded from the denominator.

Reference report: 73.49% (280 / 101 / 3 zero), against the tester's 68.61%.

## M4 — PnL

`total_pnl` uses the merged, seam-adjusted series **before** rebasing. For a
full scope (or a window beginning at/before the first report start), it is
`raw_balance[-1] − initial_balance`. For a later window it is
`raw_balance[-1] − raw_balance[0]`, where index zero is the first raw wallet
sample inside that window. If a required raw window is empty,
`calculate_metrics` raises the existing empty-series error and forms no metric;
only the existing genuine-zero route may produce a declared flat result.

The current code computes `balance[-1] − balance[0]` after rebasing the whole
series so that `balance[0] == initial_balance`. The rebase preserves every
difference and therefore **erases everything that happened before the first
sample** — the first sample is already net of the first trade's fee. On the
reference report that overstated PnL by exactly that fee, 0.0510.

**Changing the anchor alone is a provable no-op.** After the rebase
`balance[0] == initial_balance` by construction, so anchoring on
`initial_balance` returns the identical wrong number:

| | Reference report |
| --- | --- |
| rebase, anchor `balance[0]` (today) | 201.07851462 |
| rebase, anchor `initial_balance` | 201.07851462 |
| **no rebase, anchor `initial_balance`** | **201.02754452** |
| tester | 201.03 |

**And the rebase may not simply be removed.** It is a deliberate invariant with
its own test, `test_task4_balance_series_is_rebased_to_initial_balance`: the
published series is level-normalised to the declared initial balance so that it
does not depend on whatever absolute level the tester happened to report.
Removing the rebase fails exactly that test and no other — verified by disabling
it and running the whole Source v6 suite: 96 passed, 1 failed, and **no seam or
stitch test among them**.

**The prescription is therefore neither.** `total_pnl` is computed from the
merged series *before* rebasing, and the series is rebased afterwards for
publication. Both invariants hold at once: PnL is anchored on the declared
initial balance, and the published curve starts at it.

The merged series before rebasing is in the first fragment's own units — the
seam splice anchors every later fragment onto it — so `raw[-1] − initial_balance`
is correct for a stitched series as well as a single one.

Verified with the correct anchor on **300/300** corpus fragments, maximum error
0.005, which is the tester's own printing precision.

`total_pnl_percent = total_pnl / anchor × 100`, where the anchor is:

- the declared `initial_balance` when the measured window starts at or before
  the first fragment's report start;
- the first sample inside the window otherwise, because a windowed measurement
  is relative to the capital the point actually had at that moment.

The second case already behaves correctly; only the first is wrong today.

Fees and funding need no separate handling: the balance curve is the tester's
own, so both are already inside it. **Never derive PnL by summing cycles** —
`Σ(realized_pnl − fees)` gives 206.73 against the true 201.03, because 42
funding charges totalling 5.71 appear in the balance column and in no PnL or fee
column of any action.

## M5 — Profit factor

`profit_factor = gross_profit / |gross_loss|` over **all realising actions**,
`None` when the denominator is zero (ADR-0006).

The tester states the formula in its own header, `Profit Factor (gross
profit/gross loss)`, and the reference report's 1.5299 is reproduced exactly.
The current code filters `action == "closed"` and so discards the PnL of every
partial close:

| Formula | Median error vs tester | Worst |
| --- | --- | --- |
| All realising actions | 0.000025 | **0.00005** |
| `closed` only (current) | 0.50 | **3136.8** |

## M6 — Drawdown

`max_equity_drawdown = max(S, max{declared_i : fragment i is admissible})`,
where `S` is the drawdown of the merged, windowed, seam-adjusted equity series.

The equity series is the per-minute mark-to-market curve, and recomputing from
it reproduces the declared drawdown to printing precision on 7 of 8 reports with
activity. But on the fixed-lot corpus, **19 of 400** fragments hold a drawdown
the sampling misses — up to 31.39 on a declared 293.63 — and the miss is
**always in the direction of understating risk**. On the reference report the
tester saw a peak of 1030.3954 that no sample contains; the series simply does
not carry the intra-minute extreme.

Understating drawdown is the one direction that must not be tolerated: a point
published as safer than it is fails only at DD5 retest, after the decision.

**Admissibility.** A declared value describes exactly the data being measured
only when all of the following hold. Otherwise it refers to a different period
and only `S` counts for that fragment:

- `stitchability == STITCHABLE_FIXED_LOT`. Under a fixed order size the absolute
  drawdown does not depend on the balance level, which is what makes values from
  different fragments comparable and combinable. Under balance-proportional
  sizing it is neither, and such fragments cannot be stitched at all.
- The fragment lies wholly inside the measured window.
- Nothing was removed from it: no seam exclusion, no open-tail truncation, no
  `BRIDGE_NOT_COVERED`.

Practical reach: **97%** of fragments (194/200 on both published surfaces) lie
wholly inside their READY witness, and 100% are fixed-lot.

**Known limitation, stated rather than hidden.** A drawdown whose peak is in one
fragment and whose trough is in another is covered only by `S`, at sample
resolution. No report can supply it. This is still strictly better than today,
where every drawdown is at sample resolution.

`max_equity_drawdown_percent = max_equity_drawdown / peak_reference × 100`,
where `peak_reference` is the merged series' running peak at the deepest sampled
drawdown of whichever source supplied the maximum. Verified: declared drawdown
divided by our sampled peak reproduces the declared percentage to printing
precision on all 8 reports.

`max_realized_drawdown` stays the wallet-curve drawdown, computed from the
series only. The tester does not declare it, and it is a different quantity
(69.11 against 86.64 on the reference report).

`max_equity_drawdown_source ∈ {DECLARED, SERIES}` is recorded, so an audit can
tell which was used.

The `SERIES` candidate and its peak come from the final merged raw windowed
equity series. An admissible declared candidate uses that fragment's sampled
peak. The largest absolute drawdown wins; an exact `SERIES`/`DECLARED` tie
prefers `DECLARED`, and a declared/declaration tie uses the smallest
`fragment_id`. Realised drawdown always comes from the merged raw windowed
wallet series.

## M7 — What is verified after import

Three checks, chosen because they do not overlap:

| Check | Agreement | What only it can catch |
| --- | --- | --- |
| `Total PnL` | 300/300 | wrong anchor, wrong series, wrong window |
| `Total fees` | 257/257 | a lost or duplicated **action** |
| `Profit Factor` | 257/257 | a wrong `pnl` on any realising action |

`Total PnL` is computed from the endpoints of the balance curve and therefore
cannot notice an action lost in the middle — the endpoints still agree. `Total
fees` sums every action and `Profit Factor` sums every realising action's PnL,
so together the three cover the series and the action table without repeating
each other.

Each derived numeric is rounded `ROUND_HALF_UP` to the exponent in the raw
declared token. A mismatch quarantines that report only, with source identity,
fragment id, metric, declared and derived values; healthy siblings continue.

When every realising action is profitable and the gross-loss denominator is
zero, the tester's explicit numeric `0` Profit Factor is preserved as its
undefined-ratio convention; it is not replaced with an invented infinity.
If a sparse seam fragment omits one of these tester summary fields entirely,
there is no declaration to compare and the raw absence is preserved; no metric
is fabricated and the checks apply whenever the tester emits the field.

**Not verified:** drawdown, because M6 now adopts the declared value where
admissible, and because the sampled series legitimately differs elsewhere;
trades and win rate, because M2 and M3 deliberately use a different unit.

`Recovery Factor` is a separate conditional consistency check for a numeric,
positive-drawdown single fragment; it is not a fourth mandatory extraction
check. The check uses the raw M4 PnL (final wallet sample minus the declared
initial balance) and raw equity drawdown. It
runs only when the declared Max DD rounds to the sampled equity drawdown,
because M6 may intentionally select a different admissible declared DD
candidate. The published tester value may differ by one unit of its raw
exponent after display rounding; that one-unit boundary is accepted, while
larger mutations quarantine the report. Zero activity follows ADR-0016's
declared unavailable forms.

## Format and materialization boundary

Payload v2 contains factual metadata, actions, wallet/equity samples and raw
declared metrics only. `cycles`, compatibility action-events and
`open_tail_cycle_ids` are reconstructed at decode. Their existing compact-header
counts and open-tail cache remain checked derived metadata outside
`fragment_id`; a mismatch is a fail-closed `SourceV6StorageError`, never a
silent repair or quarantine.

The existing `point_analysis_input.row_json` is the only carrier for derived
materialized rows. Its v2 schema adds `weighted_trades`,
`max_equity_drawdown`, and `max_equity_drawdown_source`; no table or column is
added. Canonical row JSON uses sorted compact keys, Decimal strings, integer
counts and explicit `null`; floats/non-finite values and missing or extra v2
fields are rejected. Analysis reads these rows and never decodes payloads.

Any import quarantine blocks every scope: existing records lack safe
point/scope attribution, so partial aggregates are forbidden. The caller gets
the count plus source SHA/name/reason; remediation is fix/remove that retained
source and rebuild a new database, never an in-place unblock.

## M7a — The overlap seam

Reports overlap by 96 hours because the strategy's moving averages lag at the
start of a report and need that long to converge on the values the previous
report already had. Within the overlap the incoming report's trades are
therefore wrong and are discarded; the outgoing report owns the whole overlap.
The seam is the outgoing report's end, and positions still open there are
sacrificed.

This is implemented and now verified end to end on the overlap fixtures: the
boundary is the outgoing report's end, the incoming report's overlap cycles are
excluded and their realised PnL is subtracted from its samples before splicing,
the outgoing report's open cycle is dropped, and the stitched PnL equals the
outgoing report's own. It had **no test asserting the stitched PnL** before this
specification; the fixtures were used only for import and panel plumbing.

The rebase to the declared initial balance is not part of this. It is applied
after `merge()` has returned, so the seam splice never sees it, and it cancels
out of every difference — `total_pnl` in a window, absolute drawdown, the seam
delta itself. It corrupts only the unwindowed PnL anchor of M4, which M4 fixes
by measuring before the rebase rather than by removing it.

**No point in either corpus has more than one fragment** (25,920 fragments over
25,920 distinct points; 34,812 over 34,812). Stitching is currently exercised by
fixtures alone, which is why a regression here stayed invisible.

## M8 — Naming trap

The report's `Min balance` and `Max balance` are extremes of the **equity**
curve, not of the wallet curve. Verified: 943.65 / 1209.76 declared against our
equity 943.6454 / 1209.7642, while wallet gives 957.11 / 1207.62. Recorded so
that correct code is not "fixed" later.

## Invariants

- No metric is ever synthesised. Every value is either the tester's own or
  derived from the tester's own series and actions by a formula fixed here.
- A parameter combination whose fragments hold actions, events or samples may
  never be published with a flat result.
- Drawdown is never understated relative to what the tester declared for data we
  measure whole.
- Worker count, task order and path (hydrated or id-only) never change a value.
- Pre-v2 Source DBs and surfaces are not opened by the v2 binary; rebuild from
  retained HTML is required.

## Acceptance evidence

- The worked example of M2 yields 1 position, 2 round trips, 1.5 weighted trades
  and 3 entry fills.
- A position spanning two order ids is one closed cycle; a position still open
  at the report end stays open and does not hide the history before it.
- PnL, fees and profit factor agree with the declared values across the corpus
  sample at the tester's printing precision.
- A fragment whose declared drawdown exceeds the sampled one publishes the
  declared value with `max_equity_drawdown_source = DECLARED`; a fragment cut by
  the window publishes `SERIES`.
- A fragment with a deliberately altered action count fails import on `Total
  fees`, and one with an altered PnL fails on `Profit Factor`.
- `total_pnl` equals the declared value while `balance_series[0]` still equals
  the declared initial balance — the two invariants of M4 hold together.
- The stitched PnL of the overlap fixtures equals the outgoing report's own,
  pinned so a change to cycle reconstruction cannot silently import an incoming
  report's overlap trades again.
- The zero-activity report keeps `Profit Factor` `None` and a flat result.

## Verification

`.venv\Scripts\python.exe -m pytest -q`. No `Input/`, `Output/`, `Data/`, HTML,
DuckDB or generated artifact may be committed.
