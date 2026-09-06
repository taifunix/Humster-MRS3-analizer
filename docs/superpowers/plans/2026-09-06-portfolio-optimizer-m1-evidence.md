# Portfolio Optimizer M1 evidence

schema: portfolio_optimizer_m1_evidence_v1
version: 1
date: 2026-09-06
source_baseline: b72fb31
status: ACCEPTED
review_disposition: CODE_REVIEW_PASS
reviewer: Claude Opus 5 (high)
review_rounds: 5
used_models: claude-opus-5

## Boundary

M1 implements fixture-only configuration, canonical identity, immutable
Performance input snapshots, Portfolio DuckDB publication, and normative
dispositions. It does not start tester/bot or collector processes, access a
real database/archive, call an API, write Performance DB, calculate a portfolio
result, grant `RECOMMENDATION_READY`, or authorize trading/live use.

Open PnL, liquidity/freshness, and exact ranking policies remain explicit
`OPEN_POLICY` blockers. The only numeric policy supplied by M1 is
`portfolio_optimizer_research_risk_v1` from the approved D5 specification and
ADR-0029.

## Implemented contracts

| Contract | Evidence |
|---|---|
| Strict local configuration | `src/mrs3/portfolio/config.py`, tracked safe `portfolio_optimizer.local.json.example`, ignored working config, and `tests/test_portfolio_config.py` validate versions, required groups, units, exact decimal money, ordered finite sizing grids, and immutable parsed values. |
| Canonical identity | `src/mrs3/portfolio/canonical.py` implements `canonical_digest_v1`; `tests/test_portfolio_canonical.py` fixes literal Campaign, semantic-result, and PortfolioSet bytes/digests plus type/unit/time/decimal/missing/null/UNKNOWN/order invariants. |
| Read-only source snapshot | `src/mrs3/portfolio/input.py` opens Performance v4 read-only, uses one transaction and `cache_only=True`, freezes candidate/result/geometry/window/provenance/actions/equity facts, derives cache misses only in memory, and separates decision replay from tick replay. |
| Portfolio evidence store | `src/mrs3/portfolio/store.py` provides schema-marked transactional DuckDB tables, numeric child series, Campaign/TradingRun/Evaluation/PortfolioSet identities, idempotency, rollback, current-result replacement, and immutable execution/decision lineage. |
| Writer ownership | The Portfolio DB lease is keyed by its canonical path and records PID/start/host/boot/container identity. Foreign or unknown owners fail closed; only proven-dead same-host/same-boot owners are reclaimed automatically. Manual clear first publishes a durable append-only attestation and never terminates a process. |
| Dispositions | `src/mrs3/portfolio/disposition.py` fixes the exact section 5.6 condition/scope/result/reason mapping without a workflow engine; candidate-local failure and execution-evidence blocking remain scoped to their objects. |

## Verification

Focused M1 command:

`.venv\\Scripts\\python.exe -m pytest tests/test_portfolio_config.py tests/test_portfolio_canonical.py tests/test_portfolio_input.py tests/test_portfolio_store.py tests/test_portfolio_disposition.py -q`

Result after the fourth review-fix round: `91 passed`.

Relevant unchanged Performance source contract:

`.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_store.py tests/test_performance_v2_windows.py tests/test_performance_v2_selection.py tests/test_performance_v2_import.py -q`

Result: `202 passed, 1 warning`. The warning is the existing pandas downcast
future warning in `performance_v2_selection.py`.

All checks use temporary fixture databases and paths. Independent Claude Opus 5
high review returned `CODE_REVIEW_PASS` after five rounds.

## Handoff

M1 is accepted. M2 remains gated by collector readiness. M5 tester
ownership and any real tester/bot run remain separate gates. M1 creates no
runtime entry point and makes no recommendation or portfolio-performance claim.
