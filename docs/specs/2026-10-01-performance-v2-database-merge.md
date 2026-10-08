# One-time PerformanceDB v6 → v8 merge

**Status:** implementation authorized; independent plan review `PLAN_APPROVED` (2026-10-01).

## Goal

Append every row from the preserved v6 PerformanceDB to the current v8
PerformanceDB without retesting reports. Keep the current database instance,
its existing rows and IDs, the untouched v6 file, and a recoverable copy of the
pre-merge v8 file. This is an offline, one-time data operation.

The [typed identity contract](2026-09-04-performance-v2-config-dedup.md) and
[lossless v8 storage contract](2026-09-30-performance-db-lossless-compaction.md)
remain authoritative. This merge does not change import admission, financial
facts, selection, or prepared-input semantics.

## Inputs and outputs

Inputs are an exact readable v8 main database and an exact readable v6 legacy
database. The only accepted catalog difference is that
`strategy_results.commission_rate` is `NOT NULL` in v6 and nullable in v8.
The v6 file has nine populated data tables: `analysis_plateaus`, `strategies`,
`strategy_orders`, `strategy_results`, `strategy_actions`, `strategy_equity`,
`import_runs`, `import_files`, and `optimizer_prepared_inputs`. All other v6
data tables must be empty. Its `schema_info` is not copied.

The build command produces a same-volume candidate and a sealed local JSON
report. The separate cutover command publishes that exact candidate at the
main database path and retains the legacy file and an original-main backup.
Generated databases, reports, local paths and hashes are not committed.

## Merge invariants

- Require no source WAL, an exact supported catalog, unchanged source file
  identity/size/mtime/SHA-256, and enough free space before building and again
  before cutover. Abort on any natural-key or typed-strategy collision. No
  deduplication or replacement is allowed.
- Byte-copy main into a unique same-directory staging file and verify the copy
  hash before writing. Only the staging file receives inserts. Preserve the
  main `database_instance_id` and all existing IDs and rows.
- Allocate new strategy, result, import-run and import-file IDs with the
  candidate's four existing sequences. Remap every corresponding PK, FK and
  `strategies.current_result_id`. Plateau IDs and order IDs are parts of
  composite keys, not independent numeric ID domains.
- Append with explicit columns and dependency order. Commit large action and
  equity tables in bounded result-ID ranges, with capacity checks and
  checkpoints. The legacy database is attached read-only.
- Prepared input is not copied as opaque text: its embedded result ID, cycle
  strategy IDs and source digest must reflect the new IDs. Decode strictly,
  change only those identity fields, encode with the v8 storage codec, and
  verify through the strict reader. All other logical prepared fields stay
  equal.
- Candidate counts equal main plus legacy for all data tables. Check every
  remapped legacy row, including all action/equity rows, in bounded primary-key
  order. Prove retained-main preservation by the verified byte-copy, an
  append-only SQL allowlist, disjoint keys and final catalog/count checks.
  Check primary and natural-key uniqueness, all foreign and current-result
  references, the instance marker, sequence continuation, and representative
  public reads. Checkpoint and close before requiring zero candidate WAL.

## Capacity and cutover

The build reserves at least the main file size plus legacy file size plus
5 GiB on the staging volume, and 10 GiB on the C TEMP volume. DuckDB's spill
directory is an owned C TEMP folder with a maximum size below that reserve.
Recheck both volumes during large phases. Existing failed candidates and
diagnostics are enumerated before a new build; never silently delete them.

The cutover reacquires the PerformanceDB writer lock, rechecks the sealed
source and candidate hashes/catalog/WAL, and proves exclusive access to the
live main file. A same-volume hardlink preserves the original inode for
rollback; it is not an independent writable backup. Publish by atomic
`os.replace`. Retry only Windows sharing failures for a bounded period. A
no-op failure keeps the main file unchanged and retains the candidate.

Post-cutover smoke opens the published database strictly read-only. On smoke
failure, automatic rollback is permitted only when there is no candidate WAL.
The restored original must match its pre-cutover SHA-256 and pass read-only
smoke. Preserve the original backup and legacy database after success. Restart
long-lived readers so they do not keep using the previous inode.

## Acceptance evidence

Focused fixture tests cover ID collisions and mapping, composite keys,
prepared-input integrity, unexpected catalog/WAL/overlap, capacity refusal,
chunk failure, source immutability, cutover no-op and rollback. Run them from
the repository `.venv` with test TEMP on C and remove only the owned TEMP
folder. After independent `CODE_REVIEW_PASS`, run the full-scale build, inspect
its sealed counts/hashes and verification results, then run cutover and repeat
catalog, count, prepared-reader and source/backup checks. Record the outcome in
`progress.md` without committing local data artifacts.

## Non-goals

No tester run, report deletion, new database schema, general-purpose database
merger, automatic conflict resolution, automatic cleanup of failed candidates,
or change to selection and financial calculations.
