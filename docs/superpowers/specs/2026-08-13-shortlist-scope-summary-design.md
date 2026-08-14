# Shortlist Scope Summary Design

## Goal

Replace the per-candidate Phase 2 panel list with an aggregate scope summary.

## Contract

- One table row represents one `Pair + TF` scope.
- Columns are `Pair`, `TF`, `2 orders`, `3 orders`, `4 orders`, `READY`, `DEFERRED`, and `ALL`.
- Candidate descriptions and per-candidate checkboxes are not rendered or returned by the panel API.
- Optional Pair and TF selectors filter the summary and JSON generation scope.
- `Generate READY JSON` resolves all current READY candidate IDs for the selected scope on the server.
- Empty Pair/TF selectors mean all scopes in the selected immutable run.
- XLS remains the complete candidate-level audit.

## Verification

Controller tests cover deterministic aggregation and server-side READY ID resolution. Markup tests prove the candidate list and checkboxes are absent. A real HTTP smoke test verifies the response contains summaries rather than candidate rows.
