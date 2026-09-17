# ADR-0038: Performance v2 prepared canonicalization and locking

Date: 2026-09-17. Status: Accepted.

## Context

Performance v2 needs a reusable per-result optimizer artifact without letting
workers read a mutable DuckDB or pairing a typed result with an artifact from a
different revision. The artifact also needs a stable identity for migration,
eager import, lazy preparation, and strict reads.

## Decision

The schema-v5 contract persists nullable typed action Price/Cost, six nullable
WS1.1 sizing facts, and one private, versioned, digest-bound, per-result prepared
optimizer input.

`OptimizerSourceInput` is the only builder input and the only digest preimage.
Its `to_document()` is serialized as UTF-8 canonical JSON with sorted keys,
compact separators, `ensure_ascii=false`, and no non-finite values; the
SHA-256 of those bytes is `source_digest`, which is not included in the
document. Exact `DECIMAL(38,12)` values render with twelve fractional digits,
no exponent, and negative zero as `0.000000000000`. A value with scale greater
than 12 is an integrity error. Timestamps are aware UTC with six microseconds
and `Z`; action/equity ordinals are unique, non-negative, and deterministic.

Every prepared row, including `UNAVAILABLE`, has non-null
`preparation_version` and `source_digest`. Expected unavailability is limited
to `MISSING_TYPED_FACTS`, `UNSUPPORTED_SIZING`, and `PREPARED_TOO_LARGE`; the
size check uses exact stored UTF-8 bytes and a 16 MiB limit. Integrity,
identity, programming, and database errors remain fatal and roll back their
owning transaction.

Lazy preparation uses one read-only connection to load one immutable current
scope/snapshot before `PerformanceV2WriterLock`; it is closed before workers or
the writer lock. CPU workers get only in-memory data and no database/path. One
writer connection/transaction then reloads scope and canonical inputs, drops
work whose membership, identity, version, or digest changed, replaces only
matching missing/stale rows, and commits atomically. No read-only connection
may open during the writer lock/transaction.

PerformanceDB is explicitly `PERFORMANCE_V2_DB` and never falls back to JSON.
Only explicitly marked `LEGACY_FIXTURE` mappings may use compatibility JSON.
Prepared data is private and can enter only internal `weighted_input_rows`; it
does not become public API, Members, or XLSX data.

## Consequences

Canonical digests make eager and lazy artifacts revision-bound and comparable
across import/readback. The single snapshot/CPU/writer sequence avoids holding
read connections during writes and prevents stale work from being committed.
The artifact remains an internal cache, so existing public Performance and
Portfolio contracts stay unchanged.

## References

- [Performance v2 optimizer prepared inputs specification](../specs/2026-09-17-performance-v2-optimizer-prepared-inputs.md)
- [ADR-0037](0037-performance-v2-optimizer-prepared-inputs.md)
