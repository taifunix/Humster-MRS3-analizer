# Portfolio Optimizer M7 evidence

**Status:** `ACCEPTED`

M7 implements the fixture/fake-only deterministic search loop, independent
portfolio state, and frozen validation boundary described by sections 8.1 and
10.1-10.2 of the active specification. Open PnL, liquidity/freshness, and
profile-ranking policies continue to block `RECOMMENDATION_READY`; no real
tester, network, bot target, or production database was used.

## Implemented boundary

- Immutable development and validation windows with explicit upstream-use and
  warm-up/state-boundary provenance.
- Deterministic propose, precheck, test, import, and refine attempts under one
  shared budget, preserving rejected, failed, tried, and budget-skipped points.
- Exact finite sizing grids without monotonicity assumptions or early stop.
- Joint DD, free-margin, MM, liquidity, and mandatory-policy gates without
  automatic relaxation; missing policy remains research-only.
- Frozen finalist order and validation PASS/FAIL selection without validation
  retuning or ranking by validation return.
- Resume state bound to campaign, run linkage, and the ordered candidate
  universe; blocked resumes preserve their complete attempt ledger.
- Portfolio state remains independent per account while shared liquidity is
  evaluated for the whole set.

## Verification

- Accepted-tree root focused command:
  `.venv\Scripts\python.exe -m pytest tests/test_portfolio_search.py tests/test_portfolio_metrics.py tests/test_portfolio_integration.py tests/test_portfolio_store.py -q`
  -> `147 passed in 21.19s`.
- `py_compile` for the touched portfolio modules passed in the executor run.
- `git diff --check` passed apart from informational Windows line-ending notices.

## Independent review ledger

Claude Opus 5 high reviewed the complete M7 packet three times through the
standard Codex Orchestration `review_code` route.

- Round 1 returned R7-1 through R7-13.
- Round 2 confirmed the first remediation set and returned five residual items.
- Round 3 confirmed those five items and returned three material findings:
  uniform freshness enforcement, candidate-universe binding, and preserving the
  resume ledger on blocked exits.
- All three findings are implemented and covered by focused tests. Root review
  also found and fixed validation ordering for a mismatched resume when required
  callbacks are absent.

A later fresh full review found seven actionable boundary cases covering clock
failure, window immutability, global-liquidity population, refinement replay,
PortfolioMetrics freshness, policy completeness, and ledger deduplication. The
reported decorator finding was a compact-packet concatenation artifact and was
verified against the real source. All actionable findings were fixed with
focused regressions; independent re-review of the exact accepted tree returned
`CODE_REVIEW_PASS`. M7 is accepted.
