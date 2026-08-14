# ADR-0006: Preserve Undefined Tester Profit Factor Without Inventing a Value

**Status:** Accepted
**Date:** 2026-08-14

## Context

Hamster Bot emits the literal `n/a` for `Profit Factor (gross profit/gross
loss)` when gross loss is zero. Rejecting that report discards otherwise complete
tester evidence; treating it as zero, infinity, or another numeric value would
invent a result.

## Decision

Only the exact raw label/value combination accepted by the performance spec may
store `profit_factor=NULL` with
`profit_factor_status=UNDEFINED_GROSS_LOSS_ZERO`. The raw metric map remains
lossless. Every numeric Profit Factor stores `AVAILABLE`; missing, other textual
or non-finite values remain structural quarantine.

DD5 PnL/DD scaling, Pareto and near-tie ranking do not use Profit Factor.
Undefined Profit Factor therefore remains an imported candidate with a visible
unavailable status, not an invented ranking input. Existing v1 performance
DuckDB files migrate idempotently by making the typed column nullable and adding
the status column with `AVAILABLE` for existing numeric rows.

## Consequences

- Six currently valid reports with `n/a` can be imported without a numeric
  fabrication.
- DD5 candidate count changes from 470 to 476 if no other validation fails.
- The six rows are included in PnL/DD DD5 comparison and marked unavailable for
  Profit Factor in raw/comparison exports and manifest counts.
