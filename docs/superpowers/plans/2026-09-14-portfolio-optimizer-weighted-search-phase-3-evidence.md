# Weighted Portfolio Search — Phase 3 evidence

Date: 2026-09-14
Status: accepted after independent Opus `CODE_REVIEW_PASS` (Claude Opus 5,
high, 2026-09-14).

## Scope

Phase 3 adds the fixture-only weighted generator and capital seam for WS1.1.
The implementation uses SciPy/HiGHS for the peak-equity drawdown LP, exact
`bank_for_path`/DD/P30 validation, and a bounded deterministic adaptive target
frontier. Results use the existing
`mrs3.portfolio.candidate_search.PortfolioCandidate` and `SearchResult` types
with `mode=WEIGHTED_V1`; raw series, actions, cycles, and normalized matrices
are not exposed in public candidates.

The additive `WEIGHTED_VECTOR_V1` sizing seam keeps the existing sizing
behavior and reuses exchange rounding/order geometry for per-member targets.
It validates member strategy identity, capacity and target mappings, minimum
quantity/notional, shared-symbol caps, and exact allocation remainders.

The source basis remains the dynamic Phase 1 contract prepared by Phase 2.
The strict FINALIST universe is unchanged. No tester, Bybit/network, database,
runtime adapter, or generated artifact was used.

## Verification

Focused executor verification:

```text
.venv\Scripts\python.exe -m pytest tests/test_portfolio_weighted_search.py tests/test_portfolio_position_sizing.py -q
49 passed
```

Root final relevant verification:

```text
.venv\Scripts\python.exe -m pytest tests/test_portfolio_weighted_search.py tests/test_portfolio_position_sizing.py tests/test_portfolio_input.py tests/test_portfolio_candidate_search.py tests/test_portfolio_pretest_proxy.py tests/test_portfolio_search.py tests/test_portfolio_reports.py -q
245 passed in 80.54s
git diff --check
```

The final root check will repeat `git diff --check` after staging. Focused
checks were fixture-only; no real tester or exchange operation ran.

## Artifacts and files

- Implementation: `src/mrs3/portfolio/weighted_search.py` and the additive
  vector seam in `src/mrs3/portfolio/position_sizing.py`.
- Public exports: `src/mrs3/portfolio/__init__.py`.
- Dependency: `pyproject.toml` declares `scipy>=1.14,<2`.
- Focused tests: `tests/test_portfolio_weighted_search.py` and additions to
  `tests/test_portfolio_position_sizing.py`.
- Contract sources: WS1.1 specification and ADR-0036.

## Review ledger

Claude Opus 5 high returned `CODE_REVIEW_PASS` after the accepted review
rounds. Findings resolved included solver residual and bank authority checks,
bounded Decimal precision, member/capacity/target identity and mapping
validation, vector exchange geometry and exact remainder allocation, and
fail-closed diagnostics. Rejected scope expansions were not implemented:
Phase 4 margin/limiter, Phase 5 bootstrap/CDaR/budget system, Phase 6
adapter/runtime, and unrelated legacy helper redesign.

Phase 3 is accepted at this fixture boundary. The next implementation step is
Phase 4 margin, limiter `L`, and priorities per the approved plan.
