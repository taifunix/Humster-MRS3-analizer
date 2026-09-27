# ADR-0046: Unified liquidity and one-way optimizer history

Date: 2026-09-27. Status: accepted design; implementation verification pending.

The user approved the [liquidity model](../specs/2026-09-27-liquidity-lot-model.md)
and explicitly authorized implementation. WS1.3 replaces the old calendar-mean
participation cap and shared LONG/SHORT liquidity constraint with one bound per
directional strategy: `K*V25*A15*min_i(M(s_i)/W_i)`. The convex premium is
`M(s)=1+bonus*(max(0,s-0.6)/4.9)^2`, default K=9, bonus=1.1. Existing lot shares
remain intact. For base1000, equal lots at .5%/3% give
`1000*(1+1.1*(2.4/4.9)^2)=1263.890045814244064972927947` at Decimal precision28;
the 10-USDT floor is1260. Exchange constraints only reduce this ceiling.

Both directions may be selected, but the historical model admits one active
cycle per symbol. Dedicated closes precede opposite openings; reversal effects
are ignored by explicit user instruction. Reuse existing cycle attribution to
exclude entire overlapping opposite cycles consistently. Freeze admission after
eligibility but before LP. Subsequent zero allocations do not reopen suppressed
cycles. This is deterministic historical approximation, not a conservative
performance guarantee or evidence of actual bot/account behavior.

Risk thresholds, additive margin, leverage and geometry are preserved. Portfolio
calculations import no state from other calculations. Source-window boundary
cycles remain ordinary historical evidence. Config v3 exposes K/bonus and keeps
rounding. No account-mode confirmation gate
or external action is introduced.

After the plan review, the user chose not to rerun costly historical or
bootstrap checks after exchange flooring. Each executable size must remain
within its LP allocation and individual liquidity ceiling. Report pre-rounding
DD/bootstrap evidence as an estimate for the executable vector: uneven
per-strategy rounding can alter portfolio DD despite smaller orders.

This supersedes conflicting active mean-participation/shared-liquidity semantics
in the weighted-search contract for WS1.3 only. Historical decisions are not
rewritten; the operator confirmed no old WS1.2 result artifacts exist.
Consequence: model coefficients and admission
policy must appear in identities and all downstream metrics must consume the
same admitted-cycle path. Detailed sequencing and acceptance are in the
[reviewed plan](../superpowers/plans/2026-09-27-liquidity-lot-optimizer-integration.md).
