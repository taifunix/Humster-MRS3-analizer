# Trusted v4 migration performance

**Status:** Implemented / verified on production archive

**Depends on:** [DuckDB analysis storage and importer](2026-08-11-v07-duckdb-analysis-storage-and-importer.md)

## Goal

Migrate the trusted production v4 archive to v5 on the current 49-GB Windows
VM without loading report payloads into one in-memory snapshot or decoding
every payload twice. The production acceptance measurement is completion of
the observed 96,767-report archive within eight hours; this is a measured
target, not a runtime guarantee.

## Trusted-v4 contract

The selected v4 archive is user-declared trusted. Migration verifies its v4
schema, required tables/columns, relational references, count parity and
canonical/source-hash uniqueness. It does not decode action, equity or wallet
payloads and does not hash the whole source file. Time grids are decoded only
to derive the v5 content grid hash required by the v5 identity contract.

`validate_source_database()` remains the explicit full payload-integrity
check. It is not part of this production migration path.

## Flow and invariants

1. Open v4 read-only and keep all metadata, report pagination and payload reads
   in one read transaction.
2. Stream report metadata by stable `report_id` in bounded batches. Fetch only
   the matching payloads for each batch; require the exact expected ID set.
3. Detached worker threads compute canonical records plus row/payload hashes.
   They never use DuckDB connections. One writer commits each batch into a
   unique sibling staging database.
4. Structural validation of staging checks v5 schema/constraints, counts,
   references, canonical keys, active source hashes and hashes over the exact
   persisted bytes. It does not re-decode every payload.
5. Atomically rename staging to the previously absent target only after that
   validation succeeds. Any failure removes only staging; source and an
   existing target remain untouched.

The configured worker and batch values from the panel are forwarded to the
migration. CPU use can improve hashing throughput, but the single DuckDB
writer and storage bandwidth prevent a promise of linear core scaling.

## Production evidence

On 2026-08-12 the trusted archive was migrated with `workers=8` and
`transaction_batch_size=250`. Structural validation passed: `96,767` active
reports, `96,767` payloads, `96,767` point rows and `96,527` time-grid rows.
The resulting v5 file SHA-256 is
`09ecfea75391602398b87f0d1523200280928b0e8b0080e272a65abc90d8b661`.
No full v4 or v5 payload-decode pass was run as part of this trusted migration.

## Non-goals

- Parallel DuckDB writers.
- Resume/checkpoint support.
- A change to direct-analysis preflight, which currently needs a separate
  performance fix to avoid full source validation on each panel preflight.
