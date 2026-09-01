# Performance v2 finalist-selection throughput

**Status:** Accepted implementation change
**Date:** 2026-08-31
**Depends on:** [Performance v2 finalist selection and XLSX](2026-08-31-performance-v2-finalist-selection.md)

## Purpose

Reduce the wait for the stateless Pair + Side XLSX without changing its rows,
stage order, elimination decisions, workbook contract or database schema.

## Concurrency contract

`unified_performance_v2.workers` controls selection feature calculation and is
set to `30` for this 36-core host. Independent window calculations may run in
at most that many worker threads, each with a read-only DuckDB connection.
Only the request-owning process writes resulting cache rows to `window_metrics`.

The selection request remains synchronous and cancellation is process-level:
stopping the panel stops the in-flight export. There are no selection tables or
other persistent selection state.

## Invariants and evidence

- Parallel and one-worker execution return byte-equivalent result frames before
  workbook timestamp metadata, with identical per-stage booleans/finalists.
- Missing-window facts retain the existing non-elimination semantics.
- A single writer persists the computed default-window cache; worker connections
  never mutate the database.
- Tests cover worker limit, serial/parallel equivalence and one writer path.
