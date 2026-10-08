# Equity regime: taxonomy and repeated-test research draft

Status: historical V19 discussion notes, superseded on 2026-10-04 by the user's inline corrections and subsequent answers consolidated in [status map M3](2026-10-03-equity-regime-status-map.md). V19's admission, slowing, ATH-hold and retention proposals below are not the current design. V17 independent design review returned PLAN_REVISE; that review does not approve M3. Production R7.3 remains unchanged.

## Goal and non-goals

Describe current equity geometry in the user's terms, preserve evidence of transitions across repeated tests, and distinguish filter admission from retaining expensive result data. Numerical boundaries require read-only research and acceptance before this becomes an executable specification.

Sources: [active equity facts](2026-09-25-performance-v2-equity-quality.md), [research plan](../superpowers/plans/2026-10-02-performance-v2-equity-regime-research.md), [retest contract](2026-09-03-performance-v2-retest-workflow.md), [typed-config identity](2026-09-04-performance-v2-config-dedup.md).

User-facing detailed map: [seven statuses, PASS/DROP, RESERVED and REJECT60](2026-10-03-equity-regime-status-map.md). Its additional proposals are explicitly marked; the map does not approve pending numerical boundaries or change production behavior.

## Facts and common admission condition

- Use each result's exact UTC `T=report_end_utc`, source revision, Decimal equity and current-report full ATH. Never join the capital levels of independently rerun reports to manufacture one equity curve.
- PRE28 spans the first actual in-report sample through `T-28d`. Preserve the earlier strict availability boundary: duration greater than 14 days. Equality is unavailable; this boundary is a stated draft convention because the user's wording mentions both greater than and at least 14 days.
- When PRE28 is unavailable, ignore it without a penalty and expose `PRE28_UNAVAILABLE_IGNORED`; do not invent a positive direction. GROWING/WEAKENING require PRE28 UP when it is available. With available PRE28 DOWN, only STALLED or RESUMED may receive PASS, subject to their own current geometry and risk guards. PRE28 FLAT/MIXED requires an explicit admission decision before implementation.
- Record geometry, admission/reason, equity-ranking disposition and retention disposition separately. PRE28 DOWN alone does not reject a bounded stall or a proven resumption.
- Keep W7/W14/W28 numerical metrics. Define practical growth, decline and flat bands explicitly before adoption; the almost-zero R7.3 numerical epsilon does not itself establish practical trading flatness.

## Seven geometry states

| State | Meaning | Admission after common condition | Equity-rank constraint |
| --- | --- | --- | --- |
| GROWING | Confident growth in each recent stage, its own full-ATH update in each stage, no material slowing between stages | PASS | Eligible |
| WEAKENING | Same growth and ATH evidence, but material slowing on at least one transition | PASS | Eligible |
| STALLED | Below ATH with bounded pullback and current flat/recovery behavior, without persistent downward geometry: growth stopped on W14 and/or W7, or W28 is DOWN/FLAT while W14/W7 recover without a fresh W7 ATH; also a failed resume with a W7 ATH update and nonpositive W7 endpoint | PASS | RESERVE; consumes no Top N slot |
| RESUMED | A proven preceding STALLED phase followed by a strict full-ATH update in W7 and a positive W7 endpoint | PASS | Eligible; relative rank placement not decided |
| DECLINING | Prior growth followed by material declines in both W14 and W7 with deepening drawdown | DROP | Ineligible with filter ON |
| FLAT | No coherent growth/recovery/decline, including oscillating mixed geometry whose amplitude undermines a bounded pause | DROP | Ineligible with filter ON |
| COLLAPSING | Persistent falling geometry or a quantitatively critical fall | DROP | Ineligible with filter ON |

Technical invalidity, nonpositive equity and insufficient current-window facts keep explicit technical/risk outcomes; they are not forced into a market state. Unexplained valid patterns in research are completeness findings, not automatic FLAT or automatic PASS.

WEAKENING requires W14 UP under the user's latest correction. STALLED permits W14 DOWN when current short-window behavior has stabilized. RESUMED does not require W14 UP on entry or holding ATH at T. GROWING/WEAKENING must not be assigned when W7 ends below its starting equity. A failed resume is STALLED only if it also satisfies the bounded-pullback and no-persistent-decline guards; a crash is never rescued solely by a recent ATH event.

## Stage and transition definitions to validate

Proposed disjoint stages: `[T-28d,T-14d]`, `(T-14d,T-7d]`, `(T-7d,T]`. The same ATH event cannot prove continued growth in all three overlapping W28/W14/W7 windows. Events at shared boundaries belong to the preceding stage. This changes the previous inclusive recent-event convention and requires explicit boundary evidence before acceptance.

Use log OLS speeds normalized to log percentage points per 30 days. Each stage has its own actual duration. Compare adjacent speeds: one material decrease is enough for WEAKENING; the previous requirement of two successive decreases is superseded by the user's new definition. Save speeds, absolute differences and endpoint returns rather than only a label.

RESUMED requires evidence of a prior STALLED phase from comparable revision history or a separate evaluation using only observations preceding the recovery. It requires a fresh strict full-ATH update in the current W7 on every assessment; an earlier RESUMED label cannot substitute for this event. When the complete current GROWING/WEAKENING criteria are met, the strategy graduates to that state instead of retaining RESUMED indefinitely. A missing previous label cannot be treated as STALLED. Repeated evaluation of the same inputs must give the same status; the mere act of saving an assessment cannot create a new transition.

Before implementation, close the numeric definitions of practical flat, material decline, stage growth, slowing, critical drawdown, widening amplitude and the minimum evidence for prior STALLED. Previously discussed 0.5 log pp/7d, 0.5 log pp/30d and drawdown caps are research candidates, not accepted defaults or user-rejected values.

To distinguish stable 10% distance below ATH from active falling geometry, retain the DD trajectory and weekly/fortnightly changes in it. Breaking an all-time maximum-DD record is not required to identify an active decline. A longstanding 10% flat segment may be STALLED if long-window growth and bounded geometry still support it; a requirement that the drawdown newly increased in W14 is not inferred from this new taxonomy.

## Repeated-test evidence

The current REPLACE path keeps strategy/result IDs but deletes and rewrites actions, equity and caches. Before a future replacement, preserve a compact immutable assessment summary containing full typed identity, source revision/hash, report bounds, taxonomy/parameter version, geometry, gate/reasons, key metrics, PRE28 evidence and facts digest. Result ID alone is insufficient to identify a historical assessment.

Recalculate all metrics and full ATH from the new report. Distinguish: identical rerun; a proper interval extension; a later shifted interval; a changed algorithm/threshold; PRE28 first becoming available. A newly available DOWN PRE28 prevents GROWING/WEAKENING admission but does not itself prevent a qualifying STALLED/RESUMED. A later positive report may change a previously rejected configuration to PASS.

Current ADD skips equal/narrower/shifted intervals and automatically replaces only a proper superset for the same typed key. Mapped RETEST permits equal or later-ending, nonshorter reports. These contracts are not changed by this draft. Review tags, selection snapshots and other filters remain distinct from the equity assessment.

## Retest priority and storage proposal

| Current evidence | Proposed handling |
| --- | --- |
| GROWING / WEAKENING / RESUMED with PASS | Retain current detailed evidence; normal retest priority |
| STALLED with PASS | Retain; target retests at restoration of ATH or renewed decline |
| DECLINING | Lower-priority retest under a separate budget; can later stall or resume |
| Confirmed FLAT / COLLAPSING | Proposed candidates for exclusion from scheduled retests for 60 days; eligibility needs accepted numeric criteria and reference-case validation; removing heavy facts is a separate decision |

None of the seven market states proves that executable settings can never recover. A storage decision may stop allocating retest resources without asserting impossibility of future PASS.

The user accepted the 60-day resource/cooldown approach, not a finalized exclusion list. Do not include STALLED/RESUMED, PRE28 DOWN alone, or DECLINING solely by its label in that list. Save exclusion reason, evaluation timestamp and `retest_not_before` in the compact record. New source evidence remains admissible for assessment; explicitly requested retests may override the scheduled cooldown. Produce actual strategy IDs only after the new classifier and numerical exclusion criteria have been validated against database evidence; existing R7.3 labels are not substitutes for this taxonomy.

The full typed key includes symbol, side, timeframe, Close MA, order count and the multiset `(Open MA, shift_bp, lot_x12)`. Keep that identity plus interval/hash and rejection evidence if deleting detailed results. Suppress the same report or already-assessed duplicate coverage; permit distinct extended/forward evidence or explicitly requested retest. A permanent blacklist of settings is a separate user policy.

The compact record should also retain full PnL and max DD, PnL/30d and current ATH gap, equity state, gate/reason, evaluated bounds, source/tester version, taxonomy thresholds, history of assessments and any retest cooldown deadline. A hash of settings without their canonical values cannot reconstruct the tester input.

The existing prune deletes settings as well as results, and native RETEST requires an ACTIVE strategy with a current result. Compact retained identities therefore require an explicit import/retest/storage contract, not a silent use of existing prune. Estimate real reclaimable bytes and distinguish logical row deletion from physical file compaction before applying it.

## Research and acceptance evidence

Freeze the exact Panel input and reproduce active R7.3 first. Use one read-only equity scan and report source-wide inventory separately from Panel effects. Pre-register candidate numerical boundaries before inspecting outcomes. Report seven-state geometry, filter decision/reason, numerical facts, prior-state evidence and unresolved patterns; do not select thresholds by claiming future profitability on the same data.

Required cases: one slowing transition; distinct ATH updates per stage and exact boundaries; prior STALLED versus unproved recovery; new ATH followed by positive/zero/negative weekly return; end below ATH but above weekly start; nominal negative weekly movement in the flat band; stable 10% pullback versus 25% crash; mixed increasing amplitude; missing PRE28; newly available positive/negative PRE28; identical/equal/extended/shifted retests; same result ID with new revision; cache invalidation; complete compact identity and repeat-import detection.

No automatic deletion or production filter change follows from this draft. Numeric acceptance, complete classifier coverage, rank placement of RESUMED and the retention/import policy require separate decisions supported by this evidence.

## Independent review: open design findings

The reviewer confirmed the retention/retest design and identified remaining classifier work: disjoint GROWING versus RESUMED precedence; exact PRE28 UP and source-availability predicates; strict ATH epsilon and ownership at T-28d; positive-speed floors distinguishing WEAKENING from STALLED; canonical grid/window availability; non-mutation evidence; manually labeled reference cases and a reserved evaluation subset; coherent falling patterns without earlier-growth evidence.

The proposed resolution for the overlap is to assess complete current-stage GROWING/WEAKENING before RESUMED: a remote previous STALLED label must not keep a strategy RESUMED indefinitely after it has grown continuously across all recent stages. This is a proposal for the next specification, not an implemented precedence rule.

Preserve active R7.3's six-hour carry grid and actual left-boundary availability when designing stage metrics. Sparse raw points alone must not acquire an unsupported invalidity gate. Known corrupt/truncated source evidence is different from genuine short history and should be reported explicitly. Numeric epsilon, practical direction thresholds and retained proof of prior STALLED still require a complete reviewed definition before execution.

## V19: fresh RESUMED versus expanded STALLED

These notes discuss one status at a time. They do not assert a completed classifier.

1. WEAKENING: W14 must remain UP; reducing a positive growth speed is distinct from a decline. STALLED may have W14 DOWN if the latest stage has stopped materially declining.
2. RESUMED: a proven preceding stall, a strict full-ATH update in the current W7 and positive weekly endpoint. The proposed V18 continuation without a new weekly ATH is rejected by the user. Neither upward W14/W7 nor a stored RESUMED label substitutes for a fresh weekly ATH.
3. STALLED: include bounded recovery below ATH when W28 is DOWN/FLAT and W14/W7 are UP but no new weekly ATH occurred. A previously valid RESUMED can become STALLED after retest; this is loss of fresh breakout confirmation even if equity rose during the added data. PASS/RESERVE still requires bounded drawdown and no active persistent decline. A weekly ATH with zero/negative weekly endpoint is also insufficient for RESUMED.
4. PRE28 DOWN: only a qualifying STALLED or RESUMED can pass. If continued growth later makes available PRE28 UP and satisfies every current growth-stage criterion, reclassify as GROWING/WEAKENING. Missing PRE28 is ignored. Distinguish DOWN (negative slope and endpoint) from MIXED (one positive, one negative); FLAT/MIXED admission is still open.

Synthetic evidence calculated using the R7.3 Decimal/6h-grid trend and endpoint formulas, not a claim about database prevalence:

| Assessment | W28 Trend30 / endpoint30 | W14 Trend30 / endpoint30 | W7 Trend30 / endpoint30 |
| --- | --- | --- | --- |
| T0 | 1.430271 / 2.354883 (UP) | -12.597850 / -8.747570 (DOWN) | 11.761181 / 4.487700 (UP) |
| T0+14d | -1.179645 / -1.076822 (DOWN) | 6.593730 / 6.593927 (UP) | 6.543157 / 6.543202 (UP) |

The curve rises from 80 at day -60 to 100 at day -14, remains at 100 until day -7, stays at 95 until day -1, makes a strict ATH of 110 at day -1, falls to 96 by day 0, then rises to 99 by day 14. T0's week starts at 95 and ends at 96. In the extension equity grows 3.125%, W14 and W7 are UP, while the new W28 goes from 100 to 99 and is DOWN. PRE28 remains UP. This proves that the user's window-shift scenario is possible without joining independent equity curves. T0 can qualify as RESUMED only with proved preceding STALLED and accepted risk guards; T0+14d cannot be RESUMED because no new weekly ATH occurred. It is an expanded STALLED candidate subject to the same risk guards.

If a new full ATH is still held at the assessment endpoint, an exact negative endpoint return for any window beginning within that report is impossible. A negative fitted slope with a positive endpoint is still possible and is MIXED. The example above has no new ATH during the extension; recovering below ATH must be addressed explicitly rather than being described as a fresh breakout.

No historical label alone establishes that future PASS is impossible for 60 days. With positive equity and no bound on future returns, a constructed future path can recover arbitrarily quickly. A 60-day exclusion is therefore a resource/cooldown policy or a conclusion conditional on an explicit future-growth bound, not a guaranteed geometric fact. New evidence and explicit retests must be distinguished from reimporting the same rejected report.
