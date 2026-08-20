# ADR-0014: Compact publication without duplicate fact payloads

**Status:** Accepted for implementation
**Date:** 2026-08-20
**Amends:** [ADR-0012](0012-source-v6-fresh-compact-v1.md)

## Decision

Each Source v6 fragment has exactly one physical fact payload:
the compressed canonical `payload_blob`. It is the sole source for lossless
reconstruction of actions, cycles, events, wallet samples and equity samples.
No second serialized sample cache is written.

The importer and merge writer first validate their writable staging database,
then copy it into a second fresh staging database before the single atomic
publish rename. This removes DuckDB free pages left by bounded incremental
writes. The repacked database is validated and read back before publication.

## Consequences

- No analytical fact, canonical identity, audit, origin, stitch decision or
  disposition is discarded.
- The final published DB is compact even though bounded import/merge writes are
  incremental.
- Existing artifacts remain readable historical artifacts, but re-importing raw
  HTML is required to obtain the compact physical layout; no migration or
  second reader is introduced.
- All temporary writable and repack staging files remain covered by existing
  target-scoped recovery and atomic-publication rules.
