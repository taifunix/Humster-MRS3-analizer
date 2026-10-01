# ADR-0053: Profit guard for the Performance v2 DD cutoff

Date: 2026-10-01. Status: user decision; implementation pending.

## Context

ADR-0052 and the filter ledger originally recorded a standalone full-DD
cutoff above 23%. The user subsequently required a profit guard for that
branch. `GYK30l` in the request was explicitly clarified as `ПНЛ30д`, the
full PnL normalized to 30 calendar days.

## Decision

The DD branch excludes only when both are known and true:

```text
full_dd_pct > 23 AND full_pnl30_pct < 3 * full_dd_pct
```

The inequalities are strict; equality to either boundary passes this branch.
Unknown or nonfinite PnL/30d cannot trigger it. The independent full PnL/30d
floor and dual PnL/30d-to-DD ratio branches remain as recorded in ADR-0052.
The same durable REJECTED rule applies if the amended DD branch actually
eliminates a survivor in a published selection.

## Consequences

This amendment supersedes only the standalone-DD predicate in ADR-0052.
Historical research counts under DD >23% alone are not forecasts for the
amended rule. The Panel help, reasons, snapshot evidence and boundary tests
must use the guarded predicate. No historical selection is rewritten.
