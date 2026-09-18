# ADR-0039: WS1.2 directional members with a shared symbol cap

Date: 2026-09-17. Status: Accepted on 2026-09-18 after independent Opus
`CODE_REVIEW_PASS` and full-suite verification.

## Context

WS1.1 intentionally admitted one LONG finalist per symbol. That boundary made
the weighted LP and exports unable to represent an independently selected
SHORT strategy or two opposite-side strategies on the same symbol. Treating
those rows as separate capacities would overstate available symbol liquidity.

## Decision

Keep the existing `WEIGHTED_V1` mode and adapter gate, but advance the sole
executable algorithm revision to `WS1.2`. Normalize every symbol as
`str(symbol).strip().upper()` and permit at most one finalist for each
`(canonical_symbol, side)` pair, with independently enabled LONG and SHORT
upper bounds as arbitrary nonnegative integers (zero disables a side).

For each symbol with multiple members, require capacities to agree within the
existing solver tolerance and use the first deterministic capacity as the
shared cap. Preserve each member's individual bound and add one LP inequality
for the sum of that symbol's member allocations. Validate this invariant at
all authoritative candidate boundaries, including after rescue scaling; an
invalid vector is rejected, never clamped.

Use the same canonical grouping in input, Panel snapshots, weighted search,
position sizing, and executable ordering. Preserve opposite-side margin and
replay rows as independent members: margin coefficients remain additive on the
shared collateral base and no netting is inferred.

## Consequences

Same-symbol LONG and SHORT strategies remain two API/workbook rows and can be
admitted together. LP discovery and fixed-bank/CDaR redistribution cannot
allocate more than the shared symbol capacity. Sizing fails closed if actual
post-rounding totals exceed that capacity. WS1.1 is historical evidence only;
no compatibility branch, tester/runtime path, database schema, dependency, or
recommendation state is added.

## Evidence required

Focused offline tests must cover exact gate errors, canonical ordering, both
directions, capacity mismatch/overflow/no-clamp behavior, singleton numerical
compatibility, conservative fractional/tier-boundary margin bounds, additive
opposite-side margin, replay `L=1` versus `L=2`, payload flags, and export
identity/order. Phase 9 is not accepted by this ADR alone.
