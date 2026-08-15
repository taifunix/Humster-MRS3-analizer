# DuckDB Surface Coverage Review

**Status:** Priority-1 operational patch approved; ADR-0008 implementation frozen
**Date:** 2026-08-15
**Design evidence:** [DuckDB Surface Coverage Review Design](../superpowers/specs/2026-08-14-duckdb-surface-coverage-review-design.md)
**Baseline implementation evidence:** [DuckDB Surface Coverage Review Baseline Plan](../superpowers/plans/2026-08-14-duckdb-surface-coverage-review.md)
**ADR-0008 project plan (frozen; revision required before execution):**
[Common Close-MA Readiness Plan](../superpowers/plans/2026-08-15-common-close-ma-readiness.md)
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
- readiness-gated selection using the currently implemented
  `shift_readiness_v1` with maximum `430 bp`, exact `30/150/430` boundaries,
  `<=10 bp` then `<=40 bp` maximum gaps, one common MA pair, and denser optional
  data accepted;
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

### Priority-1 Operational Patch

The current one-MA-pair runtime contract remains active while this patch is
implemented. The patch is intentionally limited to making that existing flow
usable and diagnosable:

- ignore a source row only when its report window and persisted grid window are
  each zero-duration; every other empty intersection remains fail-closed;
- clear the browser coverage token and previous review before a new scan and
  keep them cleared if that scan fails;
- preserve safe error redaction while exposing controlled direct-preflight,
  preparation, and publication failures in direct job status;
- retain and render active side, ordinal, and total for sequential side work;
- expose verified coverage inventory and side-audit artifacts through the
  existing artifact endpoint;
- state beside the direct controls that coverage derives UTC intervals and side
  from checked `Pair + Side + TF` rows; the manual UTC/Side controls do not
  constrain the coverage-token workflow;
- after verification, stop duplicate panel processes and launch one process
  serving the current source.

The coverage preview token and real preflight are one contract. The token binds
the source evidence, displayed rows and exact intervals, readiness witnesses,
and inventory hash. The selected preview binds exact `Pair + Side + TF` scopes
and the per-side common UTC interval. At `Start`, the real preflight revalidates
the source under the existing read-only transaction. If the source is unchanged,
its selected scopes, per-timeframe coverage facts, per-side publication
intervals, and readiness-contract version must exactly equal the accepted UI
preview. Any mismatch or changed token fails before publication; the real
preflight may not silently widen, narrow, add, or remove a UI-selected scope.

This patch does not convert the token preview into a prepared-surface token and
does not remove the mandatory source revalidation at `Start`.

### Frozen ADR-0008 Follow-up

ADR-0008 implementation is frozen until explicitly resumed. Before execution,
its project plan and detailed design must be revised together to include:

- common Close MA `2..7` readiness and the compact `MA-C` presentation;
- human-readable `coverage_summary.csv` and ignored-row diagnostics;
- indeterminate/elapsed coverage progress and repeat-submit locking;
- measured reduction of repeated structural source passes;
- one real-preflight contract shared with the preview, with an explicitly
  approved reuse or prepared-token lifecycle;
- all remaining Priority-1 findings that are not closed by the operational
  patch.

No ADR-0008 production task may start from the linked project plan until that
revision is approved.

## 3. Invariants

1. Coverage is derived only from source DuckDB `report_start/report_end` facts
   and persisted grid windows using normalized UTC half-open `[start, end)`
   semantics. Rows whose report and grid windows are both zero-duration are
   ignored; every other empty intersection fails closed.
2. A row is selectable only when it has a continuous factual chain and a
   readiness-passing exact interval; rows with gaps or no readiness-capable
   interval are diagnostic-only and disabled.
3. The active readiness contract is exact: one MA pair covers boundary shifts
   `30`, `150`, and `430` with maximum gaps of `10 bp` then `40 bp`. Denser
   shifts and optional partial MA coverage do not disable an otherwise ready
   row. The common Close MA `2..7` replacement remains frozen under ADR-0008.
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
  one-MA-pair readiness, optional-data acceptance, and complete missing-interval
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
- Priority-1 tests prove double-zero exclusion, stale-token clearing, safe error
  display, side ordinal retention, coverage artifact links, and truthful
  UTC/Side guidance.
- A token-to-real-preflight regression test proves unchanged source data yields
  exactly the previewed scopes, per-timeframe coverage facts, side intervals,
  and current readiness version; changed evidence fails before publication.
- A live smoke check proves exactly one panel process serves the current
  date-only, side-aware source UI.
