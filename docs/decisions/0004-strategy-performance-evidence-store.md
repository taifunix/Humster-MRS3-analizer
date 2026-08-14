# ADR-0004: Separate MRS3 Strategy Performance Evidence Store

**Status:** Accepted
**Date:** 2026-08-14

## Context

MRS3 tester HTML contains the complete backtest evidence needed by post-test
DD5, later portfolio analysis and live-versus-backtest comparison. Runner CSV
is intentionally name-only and XLSX is a human report, so neither can be the
durable source of truth.

MRS2 source and analysis DuckDB databases have different provenance and compact
payload contracts. Reusing them would mix source-point and completed-tester
evidence.

## Decision

Create a separate local ignored DuckDB at
`data/databases/strategy_performance.duckdb`. Store immutable strategy versions,
backtest runs, typed metrics, canonical raw metrics/settings/actions and
timestamped wallet/equity samples. Exchange comes from the strategy JSON;
commission evidence comes from an immutable batch copy of tester configuration.

The runner copies every name-verified HTML and JSON to an immutable inbox.
`Calculate DD5` fully parses and imports that inbox, readbacks the committed
facts and then writes `safe_to_delete=YES` evidence. HTML is deleted only after
schema-v4 audit sidecars, zero quarantines and per-file cleanup state support a
safe, resumable deletion.

The backtest identity includes canonical strategy JSON, UTC period, normalized
exchange and commission contract. Tester version and market-data hash are not
identity inputs. Same identity with a different complete payload is a conflict,
not an overwrite.

## Consequences

- CSV and XLSX are reproducible exports, not evidence stores.
- Current name-only reports without inbox snapshots cannot be upgraded; the 476
  non-5m strategies must be retested.
- DD5 remains a calculated normalization, not a replacement for a real DD5
  tick-test.
- The Portfolio Analyzer may read only Layer A facts until a separate event,
  order, limiter and margin contract is accepted.
