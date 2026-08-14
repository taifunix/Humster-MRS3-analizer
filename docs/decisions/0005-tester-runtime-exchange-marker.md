# ADR-0005: Allow the Hamster Bot Exchange Runtime Marker

**Status:** Accepted
**Date:** 2026-08-14

## Context

The immutable source strategy and inbox manifest record `exchange.name=Bybit`,
but the Hamster Bot HTML runtime rewrites the same settings field to `tester`.
The rest of the captured settings must remain immutable evidence and cannot be
weakened to accommodate that runtime-only value.

## Decision

Source strategy and manifest exchange names are required and must match after
case normalization. HTML settings are compared by canonical equality, except
that HTML `exchange.name` may be the exact case-normalized marker `tester` or
the source exchange name. For `tester`, only that name is projected to the
expected source name before the final canonical comparison. Other exchange
values, missing exchange source evidence, or any remaining settings difference
quarantine the report.

## Consequences

- `tester` never becomes stored exchange or backtest identity evidence.
- The source strategy exchange remains the value persisted and normalized for
  identity.
- A future runtime marker requires an explicit ADR and parser/test change.
