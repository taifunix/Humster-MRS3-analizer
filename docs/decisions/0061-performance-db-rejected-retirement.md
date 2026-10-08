# ADR-0061: Retire rejected strategies after detail cleanup

Date: 2026-10-08. Status: accepted.

## Context

The card-9 `Удалить Rejected` operation previously deleted large detail rows
but retained an `ACTIVE` strategy row. That made rejected strategies continue
to appear in operational exports and allowed them to participate in cache and
selection work. The retained typed settings are still needed as a deduplication
tombstone, so full row deletion is not appropriate for this operation.

## Decision

- A rejected cleanup keeps only the strategy identity, typed order
  configuration, and a compact current-result tombstone. `strategy_orders`
  retains exactly the typed order fields used by the importer (`order_id`,
  `open_ma_len`, `open_multiplier`, `shift_bp`, `lot_x`, and typed plateau
  identity). `strategy_results` retains only the temporal/provenance fields
  used by interval deduplication (`result_id`, `strategy_id`, report/reported/
  effective period, listing fields, `warmup_hours`, and `imported_at_utc`).
  Its exchange provenance is retained, required balances become zero,
  and all PnL, DD, fee, trade-count, exclusion, optimizer-source, and sizing
  payload fields are nulled. All other per-strategy operational rows are
  disposable after retirement.
- The detail target is every exact effective rejected strategy in the selected
  pairs, including an already-`DISCARDED` row with residual facts left by an
  interrupted earlier run.
- It deletes `strategy_actions`, `strategy_equity`, `window_metrics`,
  `optimizer_prepared_inputs`, `equity_quality_metrics`, `strategy_tags`,
  `strategy_rejection_sources`, `selection_results`, and
  `selection_review_rows` rows for the selected rejected strategies.
- In the same transaction, it sets their `strategies.lifecycle_status` to
  `DISCARDED`.
- Operational readers, cache preparation, selection, catalog counts, and XLSX
  export use `lifecycle_status='ACTIVE'`; discarded rows are therefore hidden
  from normal work and output.
- The retained configuration and compact result are a permanent typed-key
  deduplication tombstone. Equal, narrower, and wider incoming reports are
  skipped. A duplicate discarded key fails closed. Explicit `REPLACE` requires
  an active target.
- A duplicate discarded key or a discarded row without its compact current
  result aborts the whole import batch before publication; the error identifies
  the typed key and directs the operator to full pair deletion. If an ACTIVE
  row and a DISCARDED row share a key, the ACTIVE row deterministically wins
  for normal ADD deduplication.
- A normal ADD skipped by a unique discarded tombstone is recorded as
  `SKIPPED:DISCARDED_TOMBSTONE` in the import-file journal, distinct from an
  ordinary interval dedup skip.
- Intentional reintroduction requires full pair deletion first; the normal
  importer never reactivates a discarded row.
- Full pair deletion removes discarded rows with the rest of the pair.
- No schema migration, cleanup timestamp, automatic timer, backup, or new
  status field is introduced. The existing schema-v9 `lifecycle_status` column
  is used.

## Consequences

Rejected strategies no longer slow active cache/filter/export paths or appear in
their results: detail facts, cache rows, and per-strategy selection rows are
physically removed. Pair-level selection runs and shared review imports remain
only when they can serve other strategies. Their compact tombstones remain in
storage, so physical file size may not shrink until DuckDB is compacted, but
the expensive operational data is removed. A fresh report cannot silently
revive a discarded configuration; full pair deletion is the explicit escape
hatch.

This decision supersedes the rejected-cleanup visibility and reimport behavior
described in ADR-0057 and the earlier retention wording in the maintenance
specification. ADR-0057 remains the source for the card-9 preview, progress,
concurrency, and failure-reporting contract.
