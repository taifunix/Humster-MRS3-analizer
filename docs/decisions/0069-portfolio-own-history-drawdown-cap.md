# ADR-0069: Each member's own-history drawdown is capped by the profile DD limit

**Status:** Accepted
**Date:** 2026-10-10
**Amends:** [ADR-0067](0067-portfolio-bank-ladder-frontier.md) (bank-ladder
frontier); bank and frontier mechanics are otherwise unchanged.

## Context

Weighted search sizes members on the common UTC grid only. Every series is cut
to the window that all finalists share, and the drawdown limit, CDaR and
bootstrap stress all see only that window. History a pair has before the window
is discarded.

Stage 2 tester runs on 15.08–09.10 showed the cost. In the 9-pair candidate
(bank 72 USDT) one member, MSTU, lost about 64 USDT between 28.08 and 02.09.
That was before the common window started on 16.09, so Stage 1 never saw it.
The candidate's report max drawdown was 61%. The 61-pair candidate passed the
same period with under 9%.

The optimizer intentionally does not size by the sum of member drawdowns. That
rule assumes every member hits its worst drawdown at the same time, which
removes the benefit of diversification. Joint risk stays with the common-path
DD limit, CDaR and bootstrap.

## Decision

- **Measure.** For each finalist, `d_i` is its maximum drawdown per unit of
  position budget over its **own full equity history**, not cut to the common
  window.
  - It uses the same normalization as the optimizer series: each equity change
    is divided by the `source_basis` of the cycle active at that moment.
  - The path is summed, without compounding. `d_i` is the largest
    peak-to-trough fall of the cumulative path, starting from 0.
  - A change with no active cycle, or with an unknown basis, contributes 0.
  - One-way admission is ignored: it is the pair's stand-alone history.
- **Rule.** For each member, `x_i · d_i ≤ max_dd · B`. Here `x_i` is the
  member's position budget in USDT, `max_dd` is the profile's
  `max_actual_equity_dd_pct / 100`, and `B` is the bank. The rule means one
  member's worst known episode alone may not take more than the portfolio's DD
  limit of the bank. No new parameter is added.
- This is a per-member concentration cap, not a sum. The bank is never
  required to cover `Σ x_i d_i`.
- **Where it applies:**
  - every discovery LP and the composition MILP, as a linear row
    `d_i x_i − max_dd · B ≤ 0`;
  - every fixed-bank LP (additional/CDaR), as a tighter upper bound
    `x_i ≤ max_dd · B / d_i`;
  - exact evaluation, as an extra bank component
    `B_own = max_i x_i d_i / max_dd`. It enters every exact bank check
    (`bank_for_path`) and therefore the required bank.
- **Reporting.** Candidates report `own_history_dd_bank_usdt`.
  `historical_bank_usdt` keeps the common-path bank alone.
- **Short histories need no special handling.** A pair with a short history has
  a smaller known `d_i`; that is accepted as is.

## Consequences

- Concentrated, low-bank frontier levels can no longer put most of the risk on
  one pair whose worst episode lies outside the common window.
- Wide portfolios are rarely affected: each member's `x_i d_i` is small next to
  the bank.
- Candidate identities and banks change for new Campaigns. Old artifacts are not
  re-scored.
- `d_i` depends only on the frozen finalist input, so preparation stays
  deterministic.
