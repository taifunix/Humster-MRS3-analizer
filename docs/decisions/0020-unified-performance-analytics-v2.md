# ADR-0020: Unified Performance Analytics v2

**Status:** Accepted
**Date:** 2026-08-28

## Context

The current unpopulated Performance DB implementation creates a fresh DuckDB
per imported period, identifies strategy versions by canonical JSON hashes and
runs DD5/finalist selection as a separate workflow. That shape obstructs
arbitrary within-report window comparisons, explicit result replacement,
plateau-aware analysis and direct Portfolio Optimizer input. Its importer also
performs repeated per-report database opens and per-row readback queries.

Tester runs use UPNL sizing. Absolute amounts from windows with different
starting balances are therefore not comparable. Current HTML contains complete
actions and wallet/equity series, so two safe flat-boundary windows can be
derived from one test without a second tester run.

## Decision

Build Performance schema v2 as one
`<performance_db_root>/strategy_performance.duckdb`, without a v1 migration.
Store one logical strategy, its ordered MA/shift/lot and plateau facts, and one
current replaceable tester result. Store current typed actions and wallet/equity
series sufficient for arbitrary UPNL-relative windows.

Merge DD5 proxy, ordered filters, Pareto selection and finalist export into one
configurable analysis pipeline over the same database. Persist selection runs
and results and expose selected rows to the Portfolio Optimizer.

Use `RETEST` as a durable strategy tag. A single handler renders RUNS snapshots
from database parameters, invokes the common RUNS algorithm, produces the same
Performance inbox as FAST and transactionally replaces successful results.

Do not store `point_id` until an approved point-level query needs it. Do not add
legacy HTML fallbacks, arbitrary filter expressions, separate DD5 databases or
mode-specific Performance import contracts.

## Consequences

- Cross-strategy and A/B window analytics use one database and relative UPNL
  metrics.
- One strategy may reference multiple plateaus; shared plateaus are stored once.
- Discard can remove heavy results while preserving rejection knowledge.
- Replacement and RETEST require typed parameter matching and transactional
  rollback, but no canonical strategy hash identity.
- Existing v1 code/specifications remain historical until v2 replaces them;
  no production data migration is required.
- DD5 outputs remain calculation-only proxies under UPNL and require retesting
  before scaled lots are called results.
