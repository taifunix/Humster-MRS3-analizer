# Source PRETEST A/B Filter

**Status:** Implemented and independently reviewed (`CODE_REVIEW_PASS`); `NOT_ADVISOR_APPROVED` because the configured Opus Advisor was unavailable and the user explicitly authorized best-effort continuation.

## Goal

Add an optional, source-only early rejection gate before Phase 2 Pareto filtering so candidates whose recent source performance has collapsed do not consume real MRS3 tester capacity.

This gate is diagnostic screening over MRS2 source evidence. It does not establish realized MRS3 PnL and does not replace tick-tests or DD5 retests.

## Non-goals

- Change the four Pareto criteria or their dominance semantics.
- Change minimum point events, base trade frequency, shift range, or candidate construction.
- Add fees, commissions, funding corrections, or a new PnL engine. The existing canonical wallet PnL is used unchanged.
- Require activity from orders 2-4.
- Run the tester or mutate source databases.

## User contract

- The panel exposes a separate checkbox, unchecked by default.
- The checkbox is not a fifth Pareto criterion.
- When disabled, all candidates enter the unchanged Pareto calculation.
- When enabled, the gate evaluates order 1 before Pareto. A rejected candidate cannot survive or dominate another candidate.
- The request field is the top-level boolean `pretest_ab_enabled`; `filters` continues to contain exactly the existing four named Pareto booleans.

## Evidence contract

Materialization records versioned PRETEST evidence in each compact point-analysis row while decoded Source v6 facts are already in memory. It reuses `calculate_metrics`; it must not decode the payload a second time.

- `A = [ready_start, ready_end)`: the complete canonical READY witness, including B.
- `B = [ready_end - 14 calendar days, ready_end)`.
- `A_days = (ready_end - ready_start) / 1 day`.
- `B_days = 14`.
- PnL is `CanonicalMetrics.total_pnl`, the existing canonical wallet-balance PnL. There is no separate commission adjustment.
- Activity is the canonical round-trip count.
- If A is shorter than 14 complete calendar days, evidence is `INSUFFICIENT_HISTORY` and the row is not rejected.

The nested evidence object contains an exact contract version, evidence status/reason, exact millisecond bounds, calendar-day counts, A/B canonical net wallet PnL, and A/B round-trip counts. Numeric decimal values are persisted as canonical decimal text.

## Gate

For order 1 evidence:

1. `INSUFFICIENT_HISTORY` passes with an audited reason.
2. `B_round_trips == 0` passes as `NO_B_TRADES`, regardless of B PnL.
3. `A_daily_pnl <= 0` passes as `NOT_COMPARABLE`.
4. Otherwise:

   `decline_pct = ((A_daily_pnl - B_daily_pnl) / A_daily_pnl) * 100`

5. Reject only when `decline_pct > 95`. Exactly 95 passes. Negative B PnL therefore rejects when A daily PnL is positive.

The filter outcome and raw evidence are retained in shortlist/audit rows. Rejected candidates use `filter_status=DEFERRED_PRETEST_AB`; Pareto rejects retain `DEFERRED_REDUNDANT`; survivors retain `READY_AFTER_FILTERS`.

## Artifact and reproducibility rules

The added derivative evidence changes the compact analysis-row contract. Per ADR-0017:

- publish a new surface fingerprint and a new fresh-analysis fingerprint;
- strictly validate the new nested evidence and bind it through the existing analysis-input digest;
- reject old artifacts and require rebuild; do not silently synthesize or omit evidence;
- bind generated output to `pretest_ab_enabled`, the fixed 14-day window, the strict 95-percent threshold, and the evidence contract version.

No new ADR is required: ADR-0017 already governs derivative-rule fingerprint changes and rebuilds.

## Acceptance evidence

- TDD proves materialized evidence for full and insufficient windows.
- TDD proves OFF identity, strict `>95`, exact-95 pass, negative-B reject, zero-B-trades pass, A-non-positive pass, and pre-Pareto exclusion.
- TDD proves typed request validation and propagation through shortlist, audit, generation, and run snapshots/manifests.
- UI checkbox is native, accessible, and unchecked by default.
- Relevant focused tests, the full project test suite, and `git diff --check` pass from `.venv`.
- An independent read-only reviewer checks the integrated diff.
