# DuckDB Surface Coverage Review

**Status:** Approved contract change; implementation pending
**Date:** 2026-08-15
**Design evidence:** [DuckDB Surface Coverage Review Design](../superpowers/specs/2026-08-14-duckdb-surface-coverage-review-design.md)
**Baseline implementation evidence:** [DuckDB Surface Coverage Review Baseline Plan](../superpowers/plans/2026-08-14-duckdb-surface-coverage-review.md)
**ADR-0008 implementation plan:** Pending written-spec review
**Related ADRs:** [ADR-0007](../decisions/0007-observed-sparse-surface-contract.md),
[ADR-0008](../decisions/0008-common-close-ma-readiness-and-degenerate-row-isolation.md)

## 1. Purpose

This document is the canonical active feature contract for the DuckDB surface
coverage review. It approves the feature scope, invariants, and definition of
done. The linked design is detailed design evidence; its algorithms and
acceptance scenarios are used to implement and verify this contract. This file
does not copy the full design into a separate rule set, and neither document
should be edited to create a conflicting contract.

## 2. Scope

In scope:

- read-only factual coverage and readiness review before direct-surface
  materialization;
- `Pair -> LONG/SHORT -> TF` rows with date-only
  `Select | TF | Available interval | Gap` presentation;
- readiness-gated selection using `close_ma_2_7_common_interval_v1`: every Close
  MA `2..7` has at least one Open MA satisfying `shift_readiness_v1` with
  maximum `430 bp`, exact `30/150/430` boundaries, `<=10 bp` then `<=40 bp`
  maximum gaps, and denser optional data accepted;
- publication of every fully covering factual point for selected gap-free and
  readiness-passing scopes, including points above `430 bp` and partial MA
  coverage;
- new surfaces using `OBSERVED_SPARSE_GRID_CONTRACT_V2` in existing
  `grid_contract_json`, with V1 surfaces and validation unchanged and no
  analysis schema migration;
- canonical coverage inventory and per-side publication audits, both sides
  prepared in one read-only source transaction, then LONG before SHORT, with
  `PARTIAL` and manual rerun after an earlier side commits.

Out of scope:

- inferring periods from external schedules, synthesizing missing shifts, MA
  pairs, reports, or points, or requiring optional data above `430 bp`;
- activating the future `700 bp` gate in this phase;
- combining LONG and SHORT into one surface or changing existing V1 surfaces;
- persistent prepared queues and retry endpoints, source-path leases, atomic
  path-replacement handling, schema-v5 evidence, and the deferred preflight
  progress-activity panel phase;
- portfolio simulation or treating source metrics as tested MRS3 performance.

## 3. Invariants

1. Coverage is derived only from source DuckDB `report_start/report_end` facts
   and persisted grid windows using normalized UTC half-open `[start, end)`
   semantics. Rows whose report and grid windows are both zero-duration are
   ignored; every other empty intersection fails closed.
2. A row is selectable only when it has a continuous factual chain and a
   readiness-passing exact interval; rows with gaps or no readiness-capable
   interval are diagnostic-only and disabled.
3. The readiness contract is exact: every Close MA `2..7` has one selected Open
   MA covering boundary shifts `30`, `150`, and `430` with maximum gaps of
   `10 bp` then `40 bp`. A selected Open MA cannot be stitched across
   subintervals; different Close MAs may select different Open MAs. The row uses
   the longest common qualifying interval and stores all six ordered witnesses.
4. `430 bp` is a minimum gate. Once passed, every fully covering factual point
   is included; no absent point is synthesized.
5. V1 rectangular surface identity and validation remain unchanged. V2 identity
   is deterministic, side-specific, and includes the exact UTC interval,
   selected scopes, readiness contract and witness, complete
   point-to-report/source evidence, and publication-audit hash.
6. All selected sides are prepared in memory under one read-only source
   transaction before any publication. Pre-publication failure publishes zero
   surfaces; after LONG commits, failure or cancellation before SHORT is
   `PARTIAL` with manual rerun.
7. Canonical CSV audits are generated, hashed, and verified before publication;
   V2 evidence stores logical artifact names and hashes, never absolute local
   paths.
8. Coverage and sparse V2 infrastructure do not prove MRS3 effectiveness; final
   conclusions require real tick-test and DD5 retest evidence.

## 4. Detailed Contract Reference

Detailed deterministic rules for period-chain merging, readiness witness
selection, CSV columns/statuses/reason grammar, canonical JSON/JSONL profiles,
V2 `grid_contract_json` identity, queue states, and verification scenarios are
defined once in the [design evidence](../superpowers/specs/2026-08-14-duckdb-surface-coverage-review-design.md).
Changes to those detailed rules must update the design evidence and this
canonical scope/invariant file together without introducing contradictions.

## 5. Definition of Done

- ADR-0007 and ADR-0008 are accepted, and this canonical spec is linked from
  PRD and progress.
- Factual coverage and readiness tests prove deterministic merging, exact
  readiness, optional-data acceptance, all Close MAs `2..7`, differing selected
  Open MAs, common-interval tie-breaking, and complete missing-interval
  reporting.
- Coverage tests prove structurally degenerate rows do not contribute or abort,
  while incompatible non-empty windows and one-sided zero-duration windows fail
  closed.
- Materialization tests prove every fully covering factual point is included
  and no point is synthesized.
- Storage tests prove V1 remains unchanged, V2 identity is deterministic, and
  V2 evidence persists in existing `grid_contract_json` without schema
  migration.
- Controller tests prove one-transaction preparation, LONG-before-SHORT
  publication, zero-surface failures before publication, and deterministic
  `PARTIAL` behavior after LONG commits.
- CSV tests prove canonical bytes, ordering, required columns, statuses, reason
  grammar, hashes, publication provenance, and unchanged schemas.
- UI tests prove the date-only nested Pair/Side layout, enabled and disabled
  row states, queue progress, and that the preflight progress-activity phase
  remains deferred.
- Focused verification includes `tests/test_duckdb_direct.py` and
  `tests/test_panel.py`, plus the storage and controller tests named in the
  implementation plan.
- PRD points to this canonical spec; progress records only verified facts and
  does not claim implementation that has not been completed.
