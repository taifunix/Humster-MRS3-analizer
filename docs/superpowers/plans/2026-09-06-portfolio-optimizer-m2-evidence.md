# Portfolio Optimizer M2 evidence

schema: portfolio_optimizer_m2_evidence_v1
version: 1
date: 2026-09-06
source_baseline: 6daf3e1
status: ACCEPTED
review_disposition: CODE_REVIEW_PASS
reviewer: Claude Opus 5 (high)
review_rounds: 4
used_models: claude-opus-5

## Boundary

M2 is fixture-only. It adds exact `FINALIST` admission and read-only liquidity,
exchange-reference, ticker and coarse-capacity contracts. It did not call a real
API, start collector/tester/bot processes, read a real archive/database, write the
collector or Performance DB, grant `RECOMMENDATION_READY`, or authorize live use.

PnL, numerical liquidity/freshness and source-to-ticker skew limits,
`each_strategy_max_dd_pct`, profile ranking and tester capabilities remain open
blockers. Ticker transport is injected; tests never use the network.

## Implemented contracts

| Contract | Evidence |
|---|---|
| Exact admission | `src/mrs3/portfolio/input.py` admits only the latest reviewed exact `FINALIST` result per symbol/side, rejects ambiguous or duplicate lineage, excludes non-finalists, and stores admission lineage outside source candidate rows. |
| Liquidity archive | `src/mrs3/portfolio/liquidity.py` reads only `published_hours`, requires 168 marked hours for seven completed UTC days, validates schema-v2 metadata/types/count/hour boundaries and rejects duplicate minutes or unsafe paths. |
| Directional ceiling | Minute p05 depth is filtered by explicit Decimal quality/freshness policy and distribution quantile. Every explicit opening level participates; closing/reduce-only orders do not. Evidence binds policy, symbol, side, window, geometry, filters, tier and exposure. |
| Exchange reference | Typed instruments and all risk tiers retain exact decimals. Applicable tier uses position plus active-order exposure; current maximum leverage is rounded down to `leverage_step` and requires a Trading linear-perpetual instrument. |
| Turnover and coarse screen | An injected exact-symbol linear ticker adapter records turnover, volume, server/capture time, unit, provenance and digest. Transport or malformed evidence fails closed. Repeated-symbol loads across accounts are summed without LONG/SHORT netting and labeled `COARSE_ESTIMATE`. |
| Sizing boundary | The finite sizing envelope uses `min(envelope_max, max_balance)` where capped; an unbounded envelope remains `UNKNOWN`. Opening, exit diagnostic and empirical capacity remain distinct evidence classes. |

## Verification

Focused liquidity plus collector reference/archive: `65 passed`.

Combined command:

`.venv\Scripts\python.exe -m pytest tests/test_portfolio_config.py tests/test_portfolio_canonical.py tests/test_portfolio_input.py tests/test_portfolio_store.py tests/test_portfolio_disposition.py tests/test_portfolio_liquidity.py tests/test_bybit_collector_archive.py tests/test_bybit_collector_reference.py tests/test_bybit_collector_aggregation.py tests/test_bybit_collector_storage.py tests/test_performance_v2_selection.py -q`

Result: `354 passed, 1 warning`. The warning is the pre-existing pandas
downcast future warning in `performance_v2_selection.py`.

`compileall` and `git diff --check` passed. Independent Claude Opus 5 high
review returned `CODE_REVIEW_PASS` after four rounds.

The full repository suite reached `2712 passed, 7 skipped` and one unrelated
existing failure in
`tests/test_panel_fresh_strategies.py::test_committed_tester_inbox_readiness_survives_panel_reload`.
The failure reproduces alone and the M2 diff does not touch panel code or that
test; M2 acceptance uses the green relevant suite above.

## Handoff

M2 is accepted. M3 fixture-only margin and limiter state-envelope work is next.
M5 ownership and any real tester/bot run remain separate gates.
