# Source v6 High-Throughput Import

**Status:** Configuration contract implemented; importer implementation pending
**Date:** 2026-08-21
**Parent contract:** [Source v6 Fresh Compact v1](2026-08-20-source-v6-fresh-compact-v1.md)

## 1. Goal

Accelerate fresh Source v6 import by at least 10x on the same frozen HTML
corpus without changing canonical fragments, source digest, quarantine,
stitching ownership, audit lineage or atomic publication guarantees.

## 2. Current bottlenecks

The current importer reads and hashes every HTML twice, returns large hydrated
Python objects through multiprocessing pipes, and performs substantial work in
one parent process. Even after grouping 32 fragments, parsing results and
database work remain serialized around the parent. Increasing `workers` does
not remove these limits.

## 3. Architecture

The importer uses three phases:

1. The parent discovers canonical file paths and records size/mtime metadata.
   A worker reads each HTML exactly once, checks stat stability around that
   read, hashes those exact bytes, normalizes them and encodes the canonical
   fragment. If bytes cannot be read and therefore have no real SHA-256, the
   complete import fails before publication; the importer never fabricates a
   source identity. Parse/normalization failures after a stable read remain
   quarantined with the SHA-256 of the exact bytes.
2. Each worker chunk writes a private temporary DuckDB segment containing
   compact fragment rows and quarantine/audit outcomes. Workers never share a
   DuckDB connection and never write the final database. Only small segment
   receipts cross the process boundary.
3. The parent attaches completed segments in canonical order and bulk-copies
   them into one private staging database using SQL. It then resolves stitching,
   computes the final aggregate digest once, performs full readback validation,
   repacks the database and atomically publishes the target.

Temporary segments live beside the target, are named from the target import
identity, and are removed after success, cancellation or recovery. Raw HTML is
never copied into a segment or database.

## 4. Configuration

```json
{
  "duckdb_import": {"workers": 30},
  "source_v6_import": {
    "write_batch_size": 32,
    "worker_chunk_size": 64,
    "max_in_flight_chunks": 60,
    "segment_writer_limit": 4
  }
}
```

`write_batch_size`, `worker_chunk_size`, `max_in_flight_chunks` and
`segment_writer_limit` are non-boolean integers bounded to `1..32`, `1..256`,
`1..240` and `1..8`, respectively. At the importer boundary,
`max_in_flight_chunks >= workers`,
`worker_chunk_size * max_in_flight_chunks <= 16384` and
`segment_writer_limit <= workers` are validated. Worker count and chunk sizes
are physical tuning inputs and never participate in canonical identity.

## 5. Invariants

- One HTML read supplies both SHA-256 identity and normalization bytes.
- A file whose size or mtime changes around that read is rejected.
- Workers write only private segments; the parent remains the sole final writer.
- Segment traversal, worker completion and input traversal order cannot change
  fragment IDs, aggregate digest, audit outcome or published bytes logically.
- Quarantine remains fail-closed and prevents `safe_to_delete=YES`.
- A missing or unreadable discovered input aborts publication and is reported
  by ordinal, canonical relative path, preflight metadata and failure reason.
  It is not represented as a content quarantine because no content hash exists.
- Cancellation or failure leaves no published target and no reusable segment.
- Existing v3/v4/v5 artifacts are not read or migrated.

## 6. Verification and acceptance

Automated tests must prove:

- workers `1` and `30`, and chunk sizes `1`, `8` and `64`, produce identical
  semantic artifacts;
- interrupted segment creation and interrupted merge leave no target and are
  safely cleaned on retry;
- corrupt or mismatched segment facts fail before publication;
- the existing compact readback, overlap ownership and metric tests remain
  unchanged and pass.

Performance is measured on the same frozen Debian input set with warm-cache
state declared. Record wall time, CPU time, peak RSS, raw bytes, report count,
accepted/quarantined counts, DB bytes and source digest. Acceptance requires:

- at least 10x reports/second versus the pre-change importer on a frozen
  representative subset large enough to reach steady state;
- the 5,859-report `/opt/hb1/tester/report/1` import completes successfully;
- no semantic difference between old and new artifacts;
- larger-corpus estimates are based on measured throughput, not extrapolated
  worker count.

## 7. Non-goals

This change does not alter Source v6 facts, compression format, seam ownership,
surface/analysis logic, HTML deletion policy, or introduce a distributed job
system. It does not promise 10x if the source filesystem itself cannot deliver
the measured read throughput; that case must be reported separately with I/O
evidence.
