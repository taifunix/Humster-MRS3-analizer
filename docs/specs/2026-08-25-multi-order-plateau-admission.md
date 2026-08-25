# Multi-order plateau admission

## Status

Approved — 2026-08-25.

## Goal

Before constructing 2ORD, 3ORD, and 4ORD candidates, admit only plateaus
with sufficient structural width and independent-event depth.

## Inputs and behaviour

`config.json` gains an optional section:

```json
"multi_order_admission": {
  "min_plateau_points": 3,
  "min_plateau_events_per_month": 20
}
```

For `event_mode=real_independent_events`, every plateau used in a multi-order
structure must meet both limits using its frozen `plateau_point_count` and
`plateau_event_count`. The check happens before combinations are generated.
The defaults are 3 points and 20 events per month.

`legacy_trades_proxy` retains its current behaviour because it has no
independent-event measurement.

## Invariants

- The existing 1ORD admission settings and selection behaviour are unchanged.
- Close support, event eligibility, tuple validation and ordering are unchanged.
- A rejected plateau cannot occur in a 2ORD, 3ORD or 4ORD structure.
- A plateau without real-event diagnostics is ineligible upstream and is never
  substituted with trade counts. A `ready=true` plateau with missing or invalid
  admission diagnostics fails analysis rather than being admitted.
- The source database, surface/materialization schema and Phase 2 structural
  filters are unchanged.
- The algorithm configuration participates in the fresh-analysis identity, so
  changing either limit requires a new analysis. Existing v2 surfaces remain
  reusable as analysis input.

## Non-goals

- No Phase 2 checkbox or post-analysis Pareto filter.
- No rewriting of existing analysis artifacts or materialized surfaces.
- No change to READY JSON generation beyond consuming newly analysed results.

## Acceptance evidence

- A focused selection test proves a below-threshold real-event plateau cannot
  appear in multi-order structures while an eligible peer can.
- A focused selection test proves missing real-event admission diagnostics are
  rejected.
- Focused configuration and fresh-analysis tests prove the new values are
  parsed and change the analysis identity.
