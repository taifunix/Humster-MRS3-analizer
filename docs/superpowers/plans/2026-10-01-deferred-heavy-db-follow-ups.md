# Deferred follow-ups: heavy database processes

**Status:** deferred backlog; not authorized for implementation in the current
`perf/heavy-db-optimization` branch.  The next owner creates a new branch
`perf/deferred-heavy-db-follow-ups`, confirms the current baseline, profiles
the named path, and updates its module specification before changing behavior.

## Reason for separation

The current branch closes already measured PerformanceDB tails: explicit
FINALIST-only optimizer preparation, one explicit `SINGLE_MODE` inbox capture,
and in-memory progress coalescing.  Source/materialization work and every
remaining expensive or profile-gated hypothesis need a separate baseline and
must not delay the merge of those fixes.

## Entry rules

Each item needs a bounded reproducible fixture or read-only clone, exact output
comparison, peak RSS and wall-time measurement, focused tests, independent code
review, and a scoped commit.  Keep the configured `duckdb_import.workers` value
as the one worker setting; cap CPU/read pools at 16.  Do not change a user DB,
run a tester, delete HTML, or create a large backup as part of profiling.

| ID | Deferred work | Required evidence before implementation |
| --- | --- | --- |
| INBOX-PROFILE-01 | Profile post-scan inbox capture on real-shaped reports. | Separate validation, capture and manifest timings; preserve one explicit Verify. |
| INBOX-POOL-01 | Revisit inbox thread/process choice only if capture is CPU-bound. | 1/4/8/16 comparison, byte-identical manifests, bounded RSS; do not add a second worker setting. |
| A2-EXCLUDED-MODES | Decide whether non-native/batch tester modes need the same handoff. | Enumerated callers and mode-specific contract; no implicit capture by default. |
| PANEL-JOURNAL-01 | Profile journal serialization beyond the current import-progress coalescing. | Restart and stale-update regression plus measured journal write rate. |
| COMPACTION-RECOVERY-01 | Define recovery/backup policy and separately profile unused compound timestamp indexes. | Explicit backup location and free-space proof before any DB mutation; read-only index benchmark. |
| IMP-01 | Bound parsed-report lifetime and parent allocations. | Import fixture with exact ADD/REPLACE/rejection parity, writer-held time and RSS. |
| IMP-02 | Complete residual batch metadata/publication work. | Per-stage statement count and rollback/readback parity; do not attribute Phase 8 already removed from import. |
| IMP-03 | Reuse safe HTML inventory work. | Parser CPU profile and all header/order/rejection regressions. |
| MIG-01 | Improve one-time typed backfill. | Existing-schema migration clone, rollback and exact nullable facts. |
| EQ-01 | Batch equity metadata/revision reads and UPSERT. | Freshness/replacement parity and cache workflow measurement. |
| SEL-01 / SEL-02 | Action aggregate sharing and warm selection validation. | Predicate matrix, cold/warm split, exact candidate/ranking parity. |
| RET-01 | Bulk global frozen-order preparation. | Frozen ordering, missing/duplicate cases and comparison with ordinary retest. |
| ANA-01 | Finish only the unprofiled bounded-relation analysis writer. | Exact canonical JSON/digest, rollback and a publication-stage baseline. |
| SRC-01..SRC-04 | Source import/merge parallel proof, overlap persistence, streaming hash and ownership memory. | Source v4 evidence, immutable manifest/hash parity and bounded staging memory. |
| MAT-01 / MAT-03 | Materialization shared preparation and process-local read batches. | A/B metrics, carry/open/quiet tails and process connection lifecycle. |
| XLSX / REVIEW / LEGACY | Low-priority residual writers and legacy paths. | A demonstrated dominant cost with no relaxation of integrity checks. |
| SAFE-DELETE | HTML deletion gate. | `schema_version=4`, manifest, zero quarantine and `safe_to_delete=YES`. |

## Non-goals

This backlog neither claims the listed percentages nor authorizes a broad
refactor.  It does not reopen the merged `FINALIST` preparation, explicit
inbox handoff, or compacted live PerformanceDB without a new measured defect.
