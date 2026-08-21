# Source v6 high-throughput import — implementation plan

## Goal

Finish the accelerated fresh Source v6 importer without changing canonical
fragments, published source digest, quarantine/audit lineage, stitching or
atomic publication. Acceptance is the fixed configuration below and a measured
median-over-median throughput of at least 10x against a clean pre-change copy
on the 5,859-report Debian corpus.

## Fixed contract

- Preflight performs discovery/stat only and assigns binary-UTF-8 path ordinals.
- Each worker reads each HTML once, checks stat before/after, hashes those exact
  bytes, normalizes/encodes them, and writes one private sealed DuckDB segment.
- Segment schema is exactly `segment_manifest`, `segment_outcomes`, and
  `segment_compact_rows`; quarantine outcomes carry the real SHA after stable
  parse failures. Infrastructure/read/TOCTOU failures abort the whole import.
- The parent is the only final Source DB writer. It reduces segments in fixed
  consecutive ordinal groups with fan-in 8, assigns all final IDs, computes
  digests once over the complete ordered set, stitches, validates, repacks and
  atomically publishes.
- Temporary files use a run-token directory and close/detach/unlink cleanup;
  recovery is scoped to the exact target/import namespace.

Published `source_content_digest` remains the existing hash of sorted committed
fragment IDs. The internal input fold is domain-separated and ordinal ordered,
includes stable-read parse quarantines, and is not artifact identity.

Shipped acceptance settings:

```json
{"workers":30,"write_batch_size":32,"worker_chunk_size":64,
 "max_in_flight_chunks":60,"segment_writer_limit":4}
```

Validation is per-field first, then `max_in_flight_chunks >= workers`,
`worker_chunk_size * max_in_flight_chunks <= 16384`, and
`segment_writer_limit <= workers`.

## Execution order

1. Add RED tests and implement settings/bounds/config example. Keep existing
   dirty `write_batch_size` semantics unchanged.
2. In one atomic importer/storage change, replace HTML double-read and hydrated
   IPC with sealed leaf segments, strict worker failure records, DB-resident
   manifests/digests, deterministic hierarchical fan-in, parent lifecycle
   assignment, and unchanged final stitching/readback/repack/publish.
3. Add RED tests for interruption, cancellation, publication restat mismatch,
   scoped cleanup, CLI/panel routing, completion-order/fan-in equivalence, and
   semantic equality with the pre-change importer. Run the full suite before any
   benchmark.
4. Measure baseline from a read-only worktree pinned to the pre-change SHA and
   revised importer using the fixed settings, at least three warm/cold runs,
   and report supporting writer-limit sweep. Require successful 5,859-report
   reopen/readback, semantic hard-gate PASS and >=10x median-over-median before
   updating `progress.md`, `PRD.md` or the spec status and before review/commit.

## Verification

All tests use `.venv\\Scripts\\python.exe -m pytest`. No `Input/`, `Output/`,
`Data/`, HTML, DuckDB, segment, benchmark or generated bundle artifact may be
committed. Independent code review must return `CODE_REVIEW_PASS`.
