# Portfolio Optimizer weighted search — Phase 4 evidence

**Date:** 2026-09-14
**Status:** Accepted at the fixture boundary; live release evidence remains open
**Review state:** Independent ROUND5 `CODE_REVIEW_PASS` from Claude Opus 5 high

## Scope and boundary

Phase 4 integrates the existing exact margin calculation with limiter `L`,
attributed replay/P30, priorities, and candidate admission in weighted search.
The evidence is synthetic and fixture-only. Bootstrap, additional LP generation,
shared Phase 5 quotas, and runtime/tester/exchange activation remain out of scope.

## Implemented and proven in fixtures

- Each rounded vector evaluates off and every `L=1..N-1`; `L=N` is rejected as
  `LIMITER_RANGE_INVALID` rather than emitted as a duplicate. Fixed-bank ordering
  ranks MODEL variants by replayed P30 and orders UNKNOWN controls deterministically;
  no-bank selection preserves distinct minimum-bank and best-MODEL alternatives.
- Full drawdown and liquidity-capacity terms remain exact. The 15-strategy witness
  is `B_required=2778` (off), `1936` (CONFIRMED L=10), `1767` (L=9), and `1784`
  (L=8); UNKNOWN retains full held IM and has `B_required=2862` at L=10. The
  N=2 envelope witness has `B_required=371` USDT. A fixed bank of 2000 preserves a
  passing L=10 alternative when off fails, and marks the preserved original as
  `bank_feasible=False`.
- Replay is evaluated independently for each L from attributed cycles/common days
  and common P30. Public candidate metrics retain only compact status, P30, and
  counts; raw cycles, actions, equity, masks, and other per-cycle series are absent.
- Supplied `x` recomputes priorities, member identity, Loss/margin dependencies,
  replay, and P30. Mixed score units fail closed; strategy and priority shapes are
  exact and published priorities match the vector used for selection.
- Path drawdown contributes `B_risk=max(1, bank_for_path)` to the margin bound.
  Evidence-bearing coefficients require a finite declared domain; missing,
  unknown, or out-of-domain evidence fails closed with a named reason before LP.
- One proportional rescue appends a separately recomputed reduced candidate when
  valid; it never mutates or replaces the original. The proposed-x seam invokes
  one validator/recomputation, accepts a passing proposal, and preserves the
  original on failure without a second stabilization call.

## Verification evidence

- `.venv\\Scripts\\python.exe -m pytest tests/test_portfolio_margin.py tests/test_portfolio_weighted_search.py -q` — **198 passed** (executor focused run).
- Root relevant broader verification — **414 passed in 36.45s**.
- `git diff --check` — **clean**.
- Root staged `git diff --cached --check` — exit **0** after the whitespace correction.

No real tester, exchange, network, database, or production trading data was used.
The conservative fallback for missing limiter-release evidence is `UNKNOWN` and
retains full held IM; it is not `legacy_trades_proxy`. Live release evidence is
deferred until separate explicit authorization, so its release checkbox remains
unchecked.

## Review ledger and open gates

- Round 1: seven accepted findings fixed.
- Round 2: four accepted findings fixed.
- Round 3: review call timed out; it is not a pass.
- Round 4: accepted domain, option-validation, priority-alignment, and failure-
  propagation fixes applied. The proposed fixed-bank compact-selection policy
  change was rejected because it conflicts with the spec's fixed-bank versus
  no-bank clauses; Phase 5 quotas remain pending.
- Final independent ROUND5 review: **CODE_REVIEW_PASS** from Claude Opus 5 high.
- Live release/tester/exchange confirmation: **PENDING and intentionally unchecked**.
- Nonblocking aliases/dead-primary-selection notes are deferred; no code changes
  are required for them.

This evidence accepts the Phase 4 fixture boundary; it does not provide live
release evidence or authorize live use.
