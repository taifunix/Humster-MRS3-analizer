# Trusted v4 Migration Performance Implementation Plan

> **For agentic workers:** use TDD and an independent review before commit.

**Goal:** Replace the unbounded, double-decoding v4-to-v5 migration with a
trusted-v4, bounded, concurrent-preparation migration that preserves atomic
publication.

**Architecture:** One read-only v4 snapshot is paginated by `report_id` without
sorting BLOBs. Worker threads prepare immutable batch rows; one DuckDB writer
commits them to a sibling stage. A structural v5 check validates persisted
hashes before atomic publication.

**Constraints:** v4 payload decoders are not used during migration; time grids
are decoded once for identity; no parallel DuckDB writers; no partial target;
`workers` and `transaction_batch_size` come from panel settings.

### Task 1: Implement and prove bounded trusted migration

- [x] Add RED tests for no v4 payload decoding, exact per-batch payload IDs,
  worker/batch validation, source/target safety and panel settings forwarding.
- [x] Replace snapshot/copy migration with one read-only source transaction,
  stable metadata pagination, detached threaded preparation, batched staging
  writes and structural target validation.
- [x] Run focused source-schema and panel tests, then the repository suite and
  `git diff --check`.
- [x] Request independent code review and commit one scoped `fix:` change.
- [ ] Run the production migration and record its observed duration, counts and
  structural validation evidence. Full payload validation remains a separate,
  explicitly requested operation.
