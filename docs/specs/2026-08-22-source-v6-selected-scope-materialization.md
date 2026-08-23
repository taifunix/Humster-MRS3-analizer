# Source v6 selected-scope materialization

**Status:** Approved for implementation
**Date:** 2026-08-22
**Depends on:** [surface publication throughput](2026-08-22-source-v6-surface-throughput.md), [empty result combinations](2026-08-22-source-v6-empty-result-combinations.md)

## Goal

Publish a selected READY scope without hydrating unrelated Source DB payloads,
while retaining the exact E1--E5 empty-result decision made by
`calculate_metrics`.

## Contract

1. Preflight remains metadata-only and establishes the READY witness for every
   scope.
2. On publication, only fragment ids belonging to the requested READY scopes
   are decoded.  They are decoded through bounded worker processes and restored
   in deterministic `fragment_id` order.
3. `materialize_source_v6` still receives hydrated fragments and still calls
   `measure_points` over its READY witness.  Metadata counts or a replacement
   predicate must not decide whether a point is empty: E1 explicitly forbids
   this because visibility and seam rules can invalidate such a shortcut.
4. The source-content digest remains over every fragment in the Source DB, not
   only selected ones.  It is read from the already-validated database
   metadata, where it is persisted transactionally by the importer/merge.
5. Worker count changes elapsed time only.  Surface id, scope digest, selected
   fragment ids and `empty_result_points` must match the serial materializer.

## Non-goals

- A metadata-only materializer.  That would violate E1 unless the metric
  calculation itself is redesigned and re-proven.
- Changing ready coverage, payload copying, surface identity, or E2--E5.
- Reading or modifying raw HTML.

## Acceptance evidence

- A selected scope causes `decode_fragment_slice` to receive only that scope's
  ids; another scope's payload is not decoded.
- Parallel selected hydration equals serial hydration in ids and published
  surface identity.
- The E4 hidden-window test and E3 empty-result record remain unchanged.
- A panel job reports real `HYDRATING` completed/total counts for selected
  fragments, followed by materialization and publisher phases.

## Rationale

The stored count columns cannot replace `calculate_metrics`: samples may be
removed by open-tail visibility, seam ownership, or the selected witness.  The
largest safe optimization is therefore selecting the exact payload ids before
decoding and using the existing deterministic process reader for that subset.
