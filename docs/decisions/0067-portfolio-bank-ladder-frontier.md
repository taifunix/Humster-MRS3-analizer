# ADR-0067: Stage 1 candidates are a bank-ladder frontier

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** the candidate-selection parts of
[ADR-0066](0066-portfolio-milp-composition-selection.md); its union assembly is kept.

## Context

The first successful real Campaign returned 10 near-copies of one portfolio.
In it, 61 pairs (5 with two finalists) ran under AGGRESSIVE with a 1600 USDT
bank. All 10 candidates kept every pair at its liquidity cap, and their P30
ranged only from 1485 to 1502 USDT.

At a 1600 USDT bank with a 30% DD limit, the saturated portfolio needs only
719 USDT. Neither drawdown nor margin binds, so the optimum is "every
positive-P30 strategy at its cap". The exhaustive per-composition search then
differs only in which finalist fills a slot. ADR-0066's top-K MILP ranking has
the same property: its first 20 solutions on the real snapshot spanned 0.02%
of P30.

The operator expects a set of genuinely different optimal portfolios. The
optimizer may drop pairs and change lot ratios, but it only does so when a
constraint binds.

## Decision

- For each launch profile, Stage 1 returns a frontier of up to
  `max_candidates` portfolios. Each one is optimal for its own bank level
  across all finalist compositions.
- The union of all slot options is assembled once per Campaign as in ADR-0066,
  regardless of the composition count.
- Per profile:
  1. One MILP maximizes P30 under the profile ceiling
     (`bank_available_usdt`, or none).
  2. A second MILP finds the smallest bank `B_sat` that keeps that P30. This is
     the saturation bank.
  3. A ladder of `K = max_candidates` levels follows: `b_k = B_sat × k / K` for
     `k = K … 1`.
  4. At each level, one MILP maximizes P30 with `bank ≤ b_k` and exactly one
     finalist per slot. Below `B_sat` the bank binds, so each level is a
     different portfolio. The levels drop weak or margin-heavy pairs and
     rescale the rest.
- Each frontier point is evaluated by the exact single-composition path.
  `b_k` (`frontier_bank_usdt`, the weighted-search `lp_bank_limit`) bounds only
  the discovery LP, that is, the historical-DD/margin bank the weights may use.
  The candidate is still accepted against the profile ceiling with its full
  bank (historical, stress P95, margin). The stress bank always exceeds the
  historical one, so using `b_k` as the acceptance ceiling would reject every
  binding level; the first real run failed exactly this way. The weighted
  search's limiter post-variants (`bank_fixed`) use the same level budget.
  With the profile ceiling they re-expanded every level to a near-saturated
  portfolio, as seen on the second real run. CDaR post-variants keep their
  existing rule: the source candidate's own full bank, never above the
  profile ceiling. The exact result is published, never the MILP proxy.
- Each level contributes only its best variant (P30, then CDaR80), so the
  CDaR/margin variants of high levels cannot crowd low levels out of
  `max_candidates`. A level failing with `BANK_UNAVAILABLE` (its full bank
  exceeds the profile ceiling) is skipped and counted like `LP_INFEASIBLE`.
- Two levels that select the same finalists with weights within 1% (relative
  L1) are kept once.
- `search.max_enumerated_combinations` is no longer used by weighted Stage 1.
  The Settings field keeps its schema and validation. The Panel shows the
  combination count for information only.
- Failures follow ADR-0066: the code is
  `COMPOSITION_SELECTION_UNAVAILABLE:<code>` with the count in diagnostics. A
  level whose MILP has no solution is skipped. A profile with no level at all
  fails the Campaign closed. The result carries the warning
  `COMPOSITION_SELECTION_FRONTIER:COMBINATIONS=<n>;LEVELS=<m>`.

## Consequences

- Candidates span risk/return: from the saturated portfolio down to small,
  conservative ones. Each is Pareto-optimal for the discovery LP within the
  frontier. Lower levels exclude pairs and change lot ratios.
- Runtime per profile is two MILP solves plus `K` level solves, then `K` exact
  evaluations. It no longer depends on the number of compositions.
- Known gap: levels are spaced on the LP (historical/margin) bank while
  acceptance uses the full bank, which includes stress P95 (about 2.3× the
  historical bank in a probe). With a profile ceiling close to the stress
  requirement, the top levels can fail and the best feasible portfolio between
  two levels is not returned. Adding a level at
  `ceiling × historical / full`, measured on the first exact evaluation, is a
  possible later change.
- Ladder spacing is linear in bank. Other spacings (geometric, P30-based) are
  possible later and are not configured now.
- Small universes are no longer enumerated exhaustively. Per bank level, the
  MILP optimum covers every composition, so the exhaustive top-by-P30 list was
  only a set of near-copies at saturation.
- No PerformanceDB, Settings schema, or tester contract changes.
