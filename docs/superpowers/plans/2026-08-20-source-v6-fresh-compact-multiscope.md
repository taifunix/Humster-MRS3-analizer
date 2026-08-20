# Source v6 Fresh Compact Multi-Scope Implementation Plan

> Execute sequentially with independent review after each task. No interim
> commits; create one scoped commit only after both stages pass.

**Goal:** replace Source v6 with a fresh-only compact format, prove import and
merge on a real 684-report corpus, then build one multi-scope surface and run
deterministic parallel analysis.

**Constraints:** reuse the normalizer/stitcher and stdlib `ProcessPoolExecutor`;
DuckDB has one parent writer; workers read inputs only. No legacy reader,
migration, dependency, service, ORM, or queue. Identity is SHA-256 of
uncompressed canonical JSON (Decimal strings, sorted keys, compact JSON), never
of compressed bytes. New Source/surface/analysis artifacts have distinct
fingerprints, suffixes and paths and carry `source_content_digest`.

## Hard boundary

Stage 2 is forbidden until `progress.md` contains exactly one root-authored line:

```text
STAGE_1_GATE=ACCEPTED_BY_ROOT; date=YYYY-MM-DD; reviewer=<reference>; evidence=<reference>
```

`scripts/check-source-v6-stage1-gate.py` rejects an absent, malformed or duplicate
token. Every Stage 2 task begins by running it. A failed gate permits changes only
to Stage 1 and requires a fresh complete corpus rerun and review.

## Stage 1 — compact Source DB, importers, merge, blocking evidence

1. Document `source-v6-fresh-compact-v1` in a new spec/ADR; update PRD/progress
   and supersede the old Task 8. Define compact indexed fragments, audit,
   quarantine, stitch decisions, dispositions and origins. No row-per-sample,
   action, cycle or event storage.
2. Implement lossless codec/schema in `source_v6.py` and `source_v6_storage.py`:
   indexed `iter_fragments`, full reconstruct/readback, corruption rejection,
   recompression invariance and fail-closed old-artifact rejection.
3. Implement shared bounded multiprocess importer and Debian CLI. Parent alone
   batches writes (<=32); workers parse/encode. Staging, audit, global stitching,
   read-only validation, atomic publish and recovery are mandatory.
   Every CLI and panel write entry point acquires the same exclusive target/staging
   handle before writing; an already-held path fails non-destructively.
4. Implement Windows use of the same importer plus `source_v6_merge.py` and
   import/merge-only panel operations. Merge inputs are read-only; it dedupes by
   canonical content hash, recomputes stitching, flattens origins, and is atomic.
   Use the existing serialized job lifecycle: overlapping writes are rejected,
   targets must not already exist, and all inputs remain read-only.
5. Pass the Stage 1 real-data gate: one Pair+TF+Side, exactly
   `19 shifts x 6 OpenMA x 6 CloseMA = 684` reports/cells. Run the agreed
   real-corpus worker sweep on the primary OS at one selected codec level and
   record the actual worker counts/repeats. Debian additionally runs only the
   bundled smoke test: import, reopen/read back and inspect the handoff manifest.
   Verify every raw HTML equals reconstructed fragment; exact 684 Cartesian cells,
   audits and committed rows; zero quarantine; safe-delete YES as an advisory
   evidence field only; no task deletes, moves or mutates raw HTML; digests equal;
   DB/WAL/staging/RSS/timing and raw-corpus size recorded. Size is evidence,
   not a correctness gate.
6. Merge production partitions: 342+342 and 228+228+228. Require AB=BA=full,
   associativity, idempotence, immutable input DB/WAL/sidecars, flattened origins,
   and exactly one output writer. Kill importer/merge before/after commit and
   replace on Debian and Windows; resume must equal clean digest with no orphan.
   Reject overlapping writes through the existing serialized job lifecycle.

## Stage 2 — only after root token

7. Materializer: separate READY witness from full factual grid. Grid cardinality
   is computed from observed sets; witness never filters facts, metrics, events,
   rejections or Shift/OpenMA/CloseMA geometry.
8. Publish one all-or-nothing `.surface-v6.duckdb` for all selected scopes.
   It has per-scope digests, bounded read-only worker chunks and one writer;
   a changed selection makes a new file, never a per-CloseMA/scope file.
9. Publish separate surface-bound analysis artifacts. Each worker owns a complete
   `(symbol, side, timeframe)` scope; parent merges canonically. Persist and verify
   exact source/surface/scope digests. Speedup claims require workers <= scopes.
10. Extend panel to multi-scope surface/analysis controls using the existing
    serialized job lifecycle.
    Run recovery, digest-equality, real benchmarks, full tests and final review.

## Verification

Use `.venv\\Scripts\\python.exe -m pytest`; run focused suites, full suite and
`git diff --check`. Local HTML/DB/benchmark artifacts stay untracked. Raw HTML
file count and digest remain unchanged through both stages.

## Review disposition (minimal working scope)

The latest Advisor review is not accepted wholesale. Keep safeguards that affect
correctness or data safety:

- canonical uncompressed fragment hashes and a sorted aggregate digest;
- canonical merge ordering, deduplication and read-only inputs;
- staging, readback and atomic publication;
- one injected-fault quarantine test;
- DB/WAL/raw-size and import-timing measurements without an arbitrary ratio;
- deterministic scope ordering and read-only surface workers;
- raw HTML documented as the recovery input, without another recovery source.

Out of scope for this minimal implementation:

- cryptographic authorship/signature machinery for the root acceptance marker;
- a new persistent PID/heartbeat/fencing lease subsystem; use the existing
  serialized job lifecycle and reject overlapping writes;
- a mandatory 48-run two-OS benchmark matrix; record the agreed real-corpus
  worker sweep and exact repeats actually run;
- fabricated typed `MISSING` fact rows; absent cells remain gap-audit evidence
  and block materialization;
- a Debian pytest environment. Debian verification is a bundled runner smoke
  test: import the real corpus, reopen/read back the DB and inspect its handoff
  manifest. Repository pytest remains the local `.venv` check.
