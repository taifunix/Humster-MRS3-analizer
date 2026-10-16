# ADR-0066: Select oversized finalist compositions with a MILP

**Status:** Accepted
**Date:** 2026-10-10

## Context

Stage 1 builds every portfolio composition as the Cartesian product of the
cutoff finalist pools of the selected Pair + Side slots, then evaluates each
composition with the exact weighted search (discovery LP, bootstrap, margin and
payload checks). One composition for one profile takes about 50 s with 30
workers on a realistic 81-strategy synthetic input (106 s single-threaded).
A real campaign with 61 pairs and 117 finalists has 4·10^14 compositions; a
user selection with 37 two-finalist and 2 three-finalist slots has
1.24·10^12. Both are infeasible by orders of magnitude, and the previous
contract failed such Campaigns with `COMBINATION_LIMIT_EXCEEDED`.

The discovery LP is linear in the strategy weights. Choosing one finalist per
slot is therefore a mixed-integer extension of the same model.

## Decision

- `search.max_enumerated_combinations` remains the exact-enumeration bound. A
  universe within it is still enumerated and evaluated exhaustively.
- Above the bound, the adapter no longer fails. For each launch profile it
  solves one MILP over the union of all eligible slot options. The MILP keeps
  the discovery LP rows of `_solve_lp_unchecked` (drawdown/bank, initial IM/MM
  margin bounds, bank ceiling). It adds one binary per option of a multi-option
  slot, `x_i <= C_i z_i`, and exactly one chosen option per slot. The objective
  maximizes the same 30-day common P&L coefficient.
- Successive solves add a no-good cut over the chosen options that carry
  positive weight. The next solution is then a different portfolio, not a
  relabelling of a zero-weight slot. The ranking stops at
  `min(combination_count, 2 × max_candidates)` compositions, at infeasibility,
  or at the per-solve time limit `weighted_search.wall_time_seconds`. A
  time-limited solve keeps its feasible incumbent and ends the ranking. A
  profile with no MILP solution at all fails the Campaign closed.
- The union series are assembled from valid layer compositions. Layer `j`
  takes the `j`-th finalist of every slot, or its last one. The strict
  one-member-per-Pair+Side preparation contract is therefore never relaxed.
  Layers are cut to their common UTC grid. That grid must still cover
  `minimum_common_days`, otherwise the selector reports
  `COMMON_PERIOD_UNAVAILABLE`. Sizing, preparation and margin coefficients come
  from the same functions as the exact path. The union is built once per
  Campaign. Profiles differ only in risk policy and bank ceiling. A slot with
  two rows of the same strategy id is rejected
  (`COMPOSITION_SELECTION_DUPLICATE_STRATEGY`).
- The MILP is a selector only. Every selected composition is evaluated by the
  unchanged exact single-composition path for that profile alone. Retention,
  ranking and `max_candidates` work as before. The result carries the warning
  `COMPOSITION_SELECTION_MILP:COMBINATIONS=<n>;EVALUATED=<m>`.
- A selector failure fails closed with
  `COMPOSITION_SELECTION_UNAVAILABLE:<code>` and keeps the exact combination
  count and limit in diagnostics.
- The Panel form shows the live combination count, the exact-enumeration bound
  and which mode will run. Readiness exposes `combination_limit` for that
  purpose. Calculate is no longer blocked by the count.

## Consequences

- Campaigns of any finalist universe size complete in bounded time. Measured on
  the real 117-finalist snapshot: about 1.7 min of facts, about 1 min of layer
  preparation, 9–60 s per MILP solve, then the exact evaluation of at most
  `2 × max_candidates` compositions per profile. Successive solves slow down as
  cuts accumulate: 20 solves took 19 min on that snapshot.
- Each MILP solve emits a `COMPOSITION_SELECTION` progress event. As with the
  existing exact evaluation, the adapter receives no cancellation signal, so
  `GENERATE_VARIANTS` cannot be interrupted mid-stage.
- Each union column keeps the one-way (`ONE_ACTIVE_DIRECTION_PER_SYMBOL`)
  admission of the layer it first appears in, not that of the opposite-side
  partner the MILP finally chooses. For symbols with both LONG and SHORT, the
  proxy is therefore approximate. The exact evaluation recomputes it.
- Without a bank ceiling, the drawdown and margin rows never bind. Ranking
  then reduces to the per-slot best `C_i × max(coef_i, 0)`, which matches the
  existing p30-first retention order.
- Near-duplicate finalists can make the best compositions micro-variants of
  one portfolio. A minimum-difference rule is a possible later change.
- Above the bound the result is the best compositions by the discovery-LP
  proxy, verified exactly. It is not proof that no unevaluated composition
  would rank higher after bootstrap and margin acceptance. The common grid of
  the union can be shorter than one composition's own common period.
- Below the bound, behaviour, ordinals and results are unchanged.
- No PerformanceDB, Settings schema or tester contract changes.
