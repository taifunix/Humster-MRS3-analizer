# ADR-0007: Observed Sparse DuckDB Surface Contract V2

**Status:** Accepted
**Date:** 2026-08-15

## Context

The existing direct-surface grid contract requires a rectangular
`required_shifts_bp x MA-pair` product. The DuckDB surface coverage review needs
readiness-gated `Pair -> LONG/SHORT -> TF` selection and publication of every
factual source point that fully covers the selected interval, without requiring
unobserved optional combinations. Existing V1 surfaces must keep their original
contract and identity.

## Decision

Keep the existing rectangular `OBSERVED_GRID_CONTRACT` behavior and V1
validation/identity unchanged. New surfaces built by this workflow use
`OBSERVED_SPARSE_GRID_CONTRACT_V2` and persist all canonical evidence in the
existing `grid_contract_json`; this phase adds no analysis schema migration or
new evidence table.

V2 identity includes the exact UTC half-open publication interval and side,
selected `Pair + TF` scopes, readiness-contract version and maximum shift, one
canonical MA/witness-shift tuple per selected scope, the complete sorted point
and source evidence, aggregate hashes, and the publication-audit hash.

Before publication, one background job opens one read-only source DuckDB
connection and explicit transaction, prepares every selected side in memory,
revalidates both sides, and materializes all selected sides. A preflight,
source, cancellation, or materialization failure rolls back the read transaction
and publishes zero surfaces. After all sides are prepared, the job publishes
separate immutable LONG then SHORT surfaces. A failure or cancellation after an
earlier side commits is `PARTIAL`; manual rerun is the recovery path, and
deterministic surface identity deduplicates an already committed LONG surface.

The following speculative infrastructure is deferred: persistent prepared
queues and retry endpoints, shared source-path leases, atomic source
path-replacement handling, and a new analysis evidence table/schema version.

- Persistent prepared queues and retry endpoints wait for one real process
  crash, two `PARTIAL` runs within 30 days, or median preparation above
  10 minutes.
- Shared source-path leases wait for one reproducible managed import/build
  conflict or supported concurrent controllers.
- Atomic source path-replacement handling waits for production replacing a
  source database while a direct job is active.
- A new analysis evidence table/schema version waits for V2
  `grid_contract_json` above 10 MB per surface or measurably required indexed
  evidence queries.

## Consequences

- V1 rectangular surfaces and identity remain unchanged.
- V2 sparse evidence is deterministic and survives in existing
  `grid_contract_json` without migration.
- Preparation and publication boundaries create deterministic `FAILED`,
  `PUBLISHED`, and `PARTIAL` outcomes.
- Coverage and publication infrastructure does not prove MRS3 performance;
  final conclusions require real tick-test and DD5 retest evidence.
