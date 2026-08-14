# Panel Multi-Scope Strategy Generation

**Status:** Approved

## Goal

Let an operator generate READY JSON for multiple pairs and timeframes from one
immutable analysis run, including every timeframe except selected exclusions
such as `5m`.

## Contract

- Pair and timeframe scopes independently support `all`, `include`, and
  `exclude` modes with a selected list.
- The panel sends the selected lists and modes to both shortlist summary and
  generation endpoints.
- The controller applies the same scope predicate before candidate IDs are
  passed to the generator; browser-side controls are not authoritative.
- Existing singular `symbol` and `timeframe` payloads remain compatible.

## Non-goals

- Do not alter immutable analysis rows, READY status, ranking, or JSON content.
- Do not start the tester after JSON generation.

## Acceptance Evidence

- A controller test proves excluding `5m` returns only other READY IDs.
- A panel HTML test proves both multi-scope controls are exposed.
