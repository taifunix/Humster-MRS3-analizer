# ADR-0021: Persisted Performance v2 selection snapshots

**Status:** Accepted, deferred implementation
**Date:** 2026-09-02

## Context

Stage 2 finalist selection is intentionally stateless: it derives an ordered
candidate decision and downloads XLSX, but does not write selection runs or
results. The A/B panel needs a trustworthy future `Только финалисты` filter.
It cannot infer finalists from a downloaded file, browser memory, or an
unrelated prior filter configuration.

## Decision

The next lifecycle stage will persist an immutable selection snapshot whenever
the operator explicitly saves a completed selection. A snapshot records the
requested Pair + Side, ordered stage settings, current result identities and
the finalist/non-finalist result for every candidate. The A/B catalogue will
use the latest saved snapshot for the selected pair; without a saved snapshot
the `Только финалисты` checkbox remains unavailable.

This decision does not add tags, discard state, RETEST jobs, XLSX re-import or
automatic persistence on every preview/export. Those require their own
contracts.

## Consequences

- Current Stage 2 selection remains reproducible and write-free.
- The currently visible A/B checkbox is disabled until the snapshot schema and
  explicit save operation are implemented.
- Later manual XLSX decisions can reference durable result identities instead
  of strategy display names.
