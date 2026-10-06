# PerformanceDB maintenance in Strategy and DD5

**Status:** Implementation complete; independent Claude Opus 5 review passed. Schema v9 remains unchanged.
**Date:** 2026-10-06
**Dependencies:** PerformanceDB schema v9; [typed-config deduplication](2026-09-04-performance-v2-config-dedup.md); [equity status map](2026-10-03-equity-regime-status-map.md); [ADR-0055](../decisions/0055-equity-filter-rejected-and-manual-fact-cleanup.md); [ADR-0056](../decisions/0056-equity-rejection-source-lifecycle.md).

Once accepted, this feature contract supersedes only the prior manual-cleanup
requirements for cleanup markers, deletion timestamps and verified backups.
It does not change the equity status or rejection-source contracts. Historical
ADR/spec text remains unchanged; a new ADR records the replacement decision.

## Goal

Add an operator-controlled PerformanceDB maintenance card to the **Strategy
and DD5** tab. The operator chooses symbols, previews the number of strategies
to remove or clean per symbol, then either removes heavy facts for currently
`REJECTED` strategies or fully removes all pair-scoped strategy and selection
data for those symbols.

## User interface

Add `9. Обслуживание PerformanceDB` after the existing PerformanceDB card 8.
The card initially shows **Показать список пар**. Activating it loads and shows
the complete sorted pair catalog in four columns. Pair labels omit a trailing
`USDT`; requests retain each exact database symbol. Each row has the existing
16 px checkbox style, aligned with its pair label. Above the grid are
**Выделить все** and **Снять выделение**. Below it are **Удалить Rejected**
and **Удалить полностью**.

Either delete action first displays the affected pairs and the number of
strategies per pair: full deletion counts strategy rows; Rejected cleanup
counts rejected strategies that still have at least one detailed fact to
remove. Pairs with no matching strategies show zero. Do not show physical row
breakdowns or global journal row counts in this preview. The full-delete
preview may explain that global import journals are cleared. The preview has a
separate confirmation action. The server recalculates the target set when
confirmation arrives; if the database changed since preview, it refuses the
stale confirmation and requires a new preview.

Count physical rows only once. If a plateau row is shared by selected pairs and
will become unreferenced after the whole selection is deleted, show it once in
a separate shared-row subtotal instead of duplicating it in each pair's count.
The pair-scoped preview total and progress denominator are the unique physical
rows targeted for deletion; global import-journal rows are counted separately.

During each confirmed delete, show a live progress bar and status line using
the same visual pattern as the existing Panel progress blocks. The status line
puts the number of strategies first (full deletion: deleted strategies out of
the previewed total; Rejected cleanup: strategies with details processed),
then reports actual pair-scoped rows deleted out of the previewed total
(`Удалено XX из NNN строк выбранных пар`). It also shows total elapsed time
since apply began. Update counts only after the corresponding delete has
completed; do not animate estimated progress as completed work. Count each
shared row once in the row-progress total and report global import-journal row
counts separately from pair-scoped rows.
On completion, preserve the final counts and elapsed time. On failure, preserve
the actual progress and elapsed time, identify the failing phase/table, and
display the underlying database error message rather than replacing it with a
generic failure phrase. Progress state is transient runtime state and adds no
database table or schema field.

## Pair catalog and status resolution

The catalog covers symbols present in strategy records or pair-scoped
selection runs. It is read-only and sorted by the exact stored symbol. The
browser submits only selected symbols; the server validates them against the
catalog and derives all strategy IDs and delete targets from the database.

`Удалить Rejected` selects strategies whose current effective `User Status`
is exactly `REJECTED`. Resolve that status through the existing effective
status rules, including both manual review and independent equity rejection
sources. Do not use `Auto Status`, a historical rejection alone, or a
client-supplied strategy ID as the selector. A sticky rejection source remains
effective under ADR-0056; an old selection row is not by itself sufficient.

## Delete Rejected retention contract

For matching strategies, keep the strategy identity and complete typed
settings required by import deduplication: symbol, side, timeframe, close MA,
order count, and every order's open MA, shift and lot. Keep the compact
`strategy_results` summary, compact `window_metrics` and
`equity_quality_metrics`, the effective rejection evidence, and selection/review
history. Remove only detailed `strategy_actions`, detailed `strategy_equity`,
and the large derived `optimizer_prepared_inputs` rows for those results.

This operation adds no `cleanup_state`, `deleted_at_utc`, cleanup log, backup,
or schema change. A repeat preview derives remaining removable rows from the
database and reports zero after the detailed facts are gone. Because the
strategy, result summary and typed settings remain, an equal or narrower import
does not restore removed details: an identical report is skipped by import
deduplication, and a narrower period is skipped by the importer's interval
rule. Only a valid wider report may replace the result and rebuild its detailed
facts under the existing import contract. The rejected preview must show this
consequence before confirmation.

## Delete fully contract

For every strategy belonging to each selected symbol, remove the strategy,
all settings/results/facts/caches/status sources, and its pair-scoped
selection runs, selection results, review imports and review rows. Remove
analysis plateau rows only when no remaining strategy order references them.
Also clear all rows from the unscoped `import_files` and `import_runs` tables.
This removes import history for unselected pairs too; the user confirmed that
this batch history has no value after import. A later repeat import starts a
new run, while existing strategy/config rows for unselected pairs still
control typed-config deduplication. Do not leave cleanup markers or deletion
timestamps. The selected pair disappears from the catalog once no pair-scoped
rows remain.

## Apply, concurrency and failure behavior

Preview is read-only and does not write a database row or file. Apply uses the
existing Panel PerformanceDB coordination and one DuckDB writer. There is no
backup, staging copy, database snapshot or automatic restore. For the full
delete, dependency-ordered deletes can commit before a later statement fails;
without a backup, such a failure can leave a partial deletion. The API and UI
must report the committed per-table progress and must not claim rollback.
Error status must include the actionable underlying exception text and the
phase/table where it occurred; a generic message such as “delete failed” alone
is insufficient.

Apply admission is atomic: only one maintenance job can claim the active slot.
Preview tokens are in-memory, single-use, expire after 15 minutes and are
bounded to the newest 32 previews. A missing, expired, evicted or reused token
requires a fresh preview. Plateau keys recovered after a partial failure stay
inside the original selected-pair scope; the service rechecks for new order
references before deleting those rows and preserves any plateau that became
referenced by another strategy. The most recent failed full operation's
recovery keys are kept only in Panel memory, separately from the latest job
status, for up to 15 minutes; a later failed full operation replaces that one
map. Keys can be reused when a retry changes the selected-pair set; recovered
plateau rows are included only when every recorded owner is still selected and
the row has no live order reference. After a Panel restart or expiry,
unattributed plateau rows remain in the database and the failure status explains
why; no persistent recovery map is written.

The v6->v7 migration must recreate `strategy_results` under its final catalog
name before restoring child tables, avoiding stale DuckDB foreign-key bindings.
For existing v9 databases produced by the earlier rename-based migration, a
full delete may fail only at the final `strategies` phase with a missing
`__performance_v2_v7_strategy_results` catalog reference. In that exact case,
the service may create a transactional compatibility table from the remaining
`strategy_results` definition and rows, retry the strategy delete, then drop
the compatibility table. Other catalog errors must remain visible and must not
trigger this recovery.

Do not run competing writer connections or parallel DELETE statements.
Preview aggregation and DuckDB query execution should use the existing global
`duckdb_import.workers` setting (capped at 16 for CPU/read pools under current
project policy), with batched/grouped SQL rather than a query per pair. Measure
the configured setting against a one-worker fixture and keep results/counts
identical. Add no cleanup-specific worker setting. If the delete workload
cannot use multiple workers effectively, retain one writer and report the
measured limit rather than making writes concurrent.

## Non-goals

- No schema v10, cleanup-state fields, deletion timestamps, persistent
  maintenance log, backup, or staging database.
- No automatic cleanup, pruning by date, tester job, import, or market-data
  operation.
- No changes to effective `User Status` or equity rejection lifecycle.
- No deletion of records belonging to unselected pairs.

## Acceptance evidence

- The card appears as card 9 in the correct tab, lists every supported pair in
  four columns, displays symbols without `USDT`, and preserves exact symbols in
  requests. Select-all and clear-selection affect the full catalog.
- Preview shows only strategy counts per selected pair: strategies to be
  deleted for full removal, and rejected strategies with detailed facts to be
  cleaned for Rejected. Physical row breakdowns and global journal row counts
  do not appear in the preview. Shared plateau rows are counted once in the
  physical progress total. Preview is read-only; changed targets invalidate
  confirmation; invalid symbols fail closed.
- While a confirmed delete runs, the existing Panel progress-block style shows
  live progress. Its status line names the phase and shows strategy progress
  first, then actual deleted pair-scoped rows out of the previewed total; it
  reports global import-journal rows separately and shows total elapsed time.
  Final counts/time remain visible after completion. Errors retain actual
  database diagnostics and identify the failing phase/table instead of showing
  only a generic message.
- `Удалить Rejected` changes only exact effective-`REJECTED` strategies,
  preserves the listed identity, settings, compact metrics and rejection/review
  evidence, and removes only the detailed facts listed above. Non-rejected and
  unselected rows remain byte/row equivalent. Repeated preview reports zero
  remaining detail rows for cleaned strategies.
- `Удалить полностью` removes all enumerated strategy and pair-selection
  records for selected symbols, including manual review rows, while preserving
  shared plateau facts still referenced by another strategy. Every unselected
  pair remains unchanged.
- Neither operation creates a backup, copy, cleanup marker, deletion time, or
  schema migration. Full-delete failure reporting states which deletes
  committed and does not claim automatic rollback.
- The v6->v7 migration leaves no stale renamed results-table binding, and the
  exact legacy missing-table catalog error at the final strategy phase is
  recovered transactionally; unrelated errors are not masked.
- Preview and apply use `duckdb_import.workers` without a second worker
  setting, retain one writer, use batched/grouped queries, and have measured
  timing/RSS evidence on fixtures at one and configured worker counts.
- No tester, live PerformanceDB or source database is mutated during
  implementation verification.
