# ADR-0018: M7 PnL uses declared final balance

**Status:** Accepted

## Decision

M7 validates a report's `Total PnL` against `Final balance - Initial balance`
when `Final balance` is present. It falls back to the final wallet sample only
for sparse reports that omit that declaration.

## Consequences

M4 remains based on the raw wallet series for materialized, windowed metrics.
This change affects import validation and conditional Recovery Factor only; it
does not change stored factual data, payload identity, or analysis formulas.

## Evidence

`my_test_APLD_TSEM_fixed_0.997net` reports can have a final wallet chart point
that differs from the tester's final balance in either direction, while the
summary `Final balance - Initial balance` agrees with declared `Total PnL`.
