# Portfolio Optimizer M3 evidence

schema: portfolio_optimizer_m3_evidence_v1
version: 1
date: 2026-09-06
source_baseline: 3e837cb
status: ACCEPTED
review_disposition: CODE_REVIEW_PASS
executor: GPT-5.6 Luna (xhigh)
reviewer: Claude Opus 5 high

## Boundary

M3 is fixture-only pure calculation code. It adds no runtime entry point and
does not call a tester, bot, exchange API, collector, or real database. It does
not grant `RECOMMENDATION_READY`, authorize trading, or close the remaining
PnL, liquidity/freshness, individual-DD, or ranking policies.

## Implemented contracts

| Contract | Evidence |
|---|---|
| Margin state | `src/mrs3/portfolio/margin.py` calculates position/order IM, position MM, adjusted denominator, evidence classes and deposit sufficiency with exact Decimal inputs. Required fee, tier, model, haircut, order-loss and denominator facts fail closed. |
| Leverage and quantity | Combined position/order exposure selects the current tier; maximum leverage and quantity round down by their steps. Missing or mismatched applied leverage blocks the whole run, and post-rounding exchange/geometry/liquidity/margin checks reject the candidate. |
| Limiter | Live orders and proposed openings are separate. L=0, bounded L, priority-zero exemption, partial fills/closes, pending cancellation, same-symbol slots and overflow races retain explicit witnesses. |
| State envelope | Bounded enumeration fails closed on any unknown state. Overflow does not enumerate and accepts only a caller-attested all-executable conservative bound; componentwise asynchronous maxima are labelled `CONSERVATIVE_BOUND`. |
| Determinism | Public calculations isolate the ambient Decimal context, use conservative division for requirements, and restrict persisted reasons to `portfolio_reason_v1`. |

## Verification

Focused M3 command:

`.venv\Scripts\python.exe -m pytest tests/test_portfolio_margin.py -q`

Result after Opus finding fixes: `118 passed`.

Relevant combined verification covers portfolio M1-M4, collector
reference/archive, aggregation/storage and Performance finalist selection.
Result: `545 passed, 1 warning` in 107.08 seconds. The warning is the existing
pandas downcast future warning in `performance_v2_selection.py`.
The post-fix source/test blob attestation and run timestamp are recorded in the
[M4 evidence ledger](2026-09-06-portfolio-optimizer-m4-evidence.md).

`compileall` and `git diff --check` pass. Root review additionally fixed
inherited Decimal rounding/exponent bounds and rejected non-versioned evidence
reasons. Opus findings R-1 through R-15 were fixed, covered by focused
regressions, and accepted by independent re-review.

## Independent review status

Claude Opus 5 returned actionable findings; all recorded findings and verification
gaps were addressed and re-tested. Final independent disposition:
`CODE_REVIEW_PASS`.

## Handoff

M3 is accepted. M4 is also accepted after independent review; M5 is the next
implementation stage. U1 remains a separate track near M8 after accepted backend
API scope; its Stage 2 also requires accepted M5/M6 and separate user authorization.
