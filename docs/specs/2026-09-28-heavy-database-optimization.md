# Heavy database process optimization

**Status:** Active implementation contract; user authorized on 2026-09-28.
**Baseline:** freshly fetched `origin/main` at `8f59c2c`.
**Evidence:** [audit](../reports/2026-09-28-heavy-database-processes-audit.md),
[implementation plan](../superpowers/plans/2026-09-28-heavy-database-optimization.md).
**Plan review:** R5 `PLAN_APPROVED`, independent Claude Opus 5/high.
**Baseline check:** 221 passed, 5 skipped, 8 warnings in 164.77s;
Panel Performance v2, input, HTML, optimizer and prune suites.

The audit's reproductions and measurements were taken at historical `de7fea9`.
Its percentages are unimplemented expectations, not delivered gains at `8f59c2c`.
Each task re-confirms its defect or repeated-work mechanism on this branch
before changing it, using the failing regression or a bounded baseline probe.

## Goal and scope

Repair the reproduced PerformanceDB correctness defects and remove verified
repeated preparation, per-row SQL overhead and unbounded parent allocations in
Source import/merge, materialization, fresh analysis and PerformanceDB modules.
Use existing DuckDB bulk SQL and bounded workers rather than new services,
dependencies, persistent caches or connection-pool abstractions.

Existing feature specifications continue to govern metrics, admission,
selection and publication. This specification supplements their implementation
and defines the narrow correctness changes identified by the audit.

## Non-goals

No live tester run, trading admission, mutation of user databases, HTML deletion,
new schema version or change to the on-disk schema contract, or heuristic
reduction of the candidate/structure universe.
Source final compacted-payload proof and legacy empty-import deletion evidence
remain deferred by the user. Historical standalone v3/v4 importers are unchanged
unless separately specified and verified on their actual report path.

## Inputs and outputs

Inputs remain the existing immutable HTML/inbox snapshots, Source fragments,
materialized measurement rows, typed Performance facts and review histories.
Outputs retain their existing schemas, exact metrics, canonical identities,
rejection reasons, ranking and publication contracts, except for these repairs:

1. A rebuilt malformed optimizer prepared payload is actually persisted even
   when its version/source digest/availability metadata already match. Correct
   cache reuse remains cheap; strict validation and source rechecks remain.
2. Prune counts and deletes optimizer prepared children before their results.
   Its sequential autocommit, checkpoint, backup, restore and dry-run remain.
3. XLSX `Trades` uses cached completed round trips. The raw report trade count
   is retained in the DB. An unavailable cache exports a blank/unavailable
   `Trades` cell, never a silent zero or the raw count. Test this cache-miss path.
4. Out-of-order action rows in the actual HTML cannot be hidden by parser
   sorting. The offending report is rejected with `ACTIONS_OUT_OF_ORDER` and
   none of its rows are published. Other admitted reports follow existing
   mode-specific policies; no new all-inbox-valid rule is introduced. Test
   valid siblings under a mode that admits them. Equal timestamps retain
   stable source order.
5. Materializer progress reports successful completions while other work is
   running. Failure stops new submissions; Windows cleanup waits for running
   readers to close files.

## Updated Performance import boundary

Preserve the changes fetched in commits `128fccb`, `a7efa97` and `8f59c2c`, as
specified by [CHECK & RETEST](2026-09-03-performance-v2-retest-workflow.md):

- Explicit first Panel import atomically initializes a missing canonical
  `strategy_performance.duckdb` using the existing writer lock, same-directory
  unique staging file, schema validation, and no-clobber hard-link publication.
  A concurrent FileExists result preserves the existing target and follows the
  normal existing-target validation path; it never overwrites that file.
  Existing empty, corrupt or foreign DBs are not reinitialized; redirected
  targets remain protected. Retain the concurrent-target no-clobber regression.
  Migration of already-supported older schemas under the existing importer
  writer lock is preserved; it is not introduction of a new schema version.
  Retain the existing importer/store supported-schema migration regressions.
- Ordinary metadata `SINGLE_MODE` trusts strategy files below repository
  `Output`. It rejects redirected roots and outside-root, symlink/reparse,
  non-regular-file or invalid-hash artifacts. Retain the non-regular artifact
  regression and existing rejection mapping. Tester/configured strategy roots
  are not extra roots.
- Moving parsing/preparation outside the writer interval must recheck current
  IDs, source revisions, typed dedup, replacement state and snapshot identity
  after obtaining the writer lock. Publish the admitted ADD/REPLACE batch in
  one transaction, never incremental parser/chunk commits. Preserve existing
  mode-specific rejection/skip policies, including independently stale frozen
  members whose valid siblings may still commit.

## Optimization invariants

- Exact Decimal arithmetic, window-local peaks/fees, W0 exclusion, carry-in,
  open/quiet tails, unavailable reasons, witnesses and deterministic ties.
- Manifest, quarantine, source hashes, lineage and logical row ordering.
- Canonical JSON and digests without numeric/float coercion.
- Existing schema gates, one common publisher, atomic rename/publication and
  backup/restore; no partial generation, migration or Performance batch.
- Review replay preserves ordinary replacement, scoped overlay and latest user
  decisions. Global retest preserves frozen ordering and missing/duplicate errors.
- Shared action scans preserve the distinct side/open predicates of each metric.
- Preview remains read-only. Warm candidate hits avoid raw scans; legitimate
  cold candidate aggregation remains allowed. Equity preview reads cached facts.
- Connections stay within their owning process/thread. External process readers
  start after the common writer is closed; Windows failure cleanup closes readers.
- Streaming identity hashing preserves artifact names, lengths, absent markers
  and content digests. Existing preflight/execute checks and compact remain.
- Default worker count stays 16 until comparable measurements justify a change.
  Bound submitted and retained data by count and approximate bytes where useful.
- Source MRS2 PnL remains diagnostic; existing optimizer/M5 admission gates remain.

## Acceptance evidence

Each behavior change starts with a failing regression using the actual affected
boundary, followed by focused and relevant broader `.venv` tests. Bulk paths
must prove exact output equality and failure rollback; query-count tests cover
the eliminated repeated SQL, without arbitrary wall-time assertions.

Use the existing benchmark harness and offline synthetic/frozen-clone inputs.
Record commit, versions, input identity, cold/warm/startup, elapsed median, RSS,
worker count and statements. Audit percentage ranges are estimates rather than
hard gates. A full suite runs after final integration; rerun affected checks
when later edits introduce failures or change shared behavior.

Each scoped commit requires `git diff --check`, inspection of the staged diff,
independent `CODE_REVIEW_PASS` and an evidence update in `progress.md`.
Rollback uses that commit's revert and the previous atomic artifact/backup.
