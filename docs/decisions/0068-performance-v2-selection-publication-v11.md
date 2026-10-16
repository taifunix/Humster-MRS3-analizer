# ADR-0068: Additive v11 selection-publication lineage

**Status:** Accepted for the P5.2 planning contract
**Date:** 2026-10-10
**Scope:** Performance v2 all-pairs filtering, direct rejection, and aggregate
review publication

## Context

The existing selection/review schema is sufficient for one ordinary
Pair+Side run, but an all-pairs XLSX and its automatic rejection overlay need a
durable header, a per-partition source/overlay relationship, and one aggregate
import anchor. Reusing `selection_runs.workbook_sha256` for an aggregate artifact
would conflate run identity with exact workbook bytes. Adding an origin column
would duplicate a relationship that must be checked against the publication
manifest and would not explain historical, strict, Card6, or ADR-0064/0065
manual review rows.

The decision is additive v11 only. It does not authorize implementation or
relax any existing constraint. The P5.2 specification is the single current
normative product contract; this ADR records the schema, identity, and safety
decision that it references.

## Decision

### Additive schema and constraints

1. Add `selection_publications` as the publication header. It owns the
   publication identity, kind, operation key, frozen operation digest,
   manifest contract version, decision group, source/database revision,
   canonical controls JSON+hash, frozen render-model JSON+hash, complete
   evaluated rowset digest, and exact export hash. Exact export and upload
   workbook SHA-256 values are header values, not run identities.
2. Add `selection_publication_runs` as the child mapping from a publication to
   its partition runs. `role` is exactly `SOURCE` or `OVERLAY`, and each row
   carries `pair`, `side`, and the selection-run foreign key. Enforce one
   mapping per `(publication_id, pair, side, role)` and one mapping per run in a
   publication. Thus a partition has at most one ordinary source and one
   automatic overlay, and the overlay never replaces the source.
3. Add `selection_aggregate_imports` as the aggregate import header and
   manifest anchor. It owns the aggregate import identity, publication and
   operation identity, manifest version, source revision, complete partition
   rowset counts/digests, candidate identities, exact uploaded workbook
   SHA-256, and lifecycle/retirement state.
4. Add only plain nullable `aggregate_import_id` to
   `selection_review_imports`. The supported DuckDB 1.5.5 migration creates a unique index on
   `(aggregate_import_id, selection_run_id)`; it does not add an ALTER-table FK
   or UNIQUE constraint and does not rebuild either history table. The writer
   and integrity/reachability audit enforce that every non-NULL child has an
   existing aggregate header. Existing rows retain SQL `NULL`. Do not add an
   origin column, make another existing field nullable, or relax any existing
   uniqueness/check constraint. `selection_review_imports.workbook_sha256`
   remains `NOT NULL UNIQUE`.
5. Keep `selection_runs.workbook_sha256` `NOT NULL` and preserve its ordinary-run
   meaning. An overlay has no workbook, so its stored key is derived with a
   separate child domain:
   `SHA256("performance_v2_selection_overlay_run_v11\\0" || canonical
   overlay manifest tuple)`. This key is not an export or upload hash and
   cannot collide with an ordinary run identity.

`selection_review_imports.workbook_sha256` remains `NOT NULL UNIQUE`. Automatic
and aggregate children use the internal domain
`performance_v2_selection_review_import_v11` over exact UTF-8 bytes with
literal `0x00` separators:
`SHA256(domain || "\\0" || reference_kind ("publication_id" |
"aggregate_import_id") || "\\0" || canonical_reference_id || "\\0" ||
canonical_selection_run_id)`. Canonical IDs use the repository's existing
lowercase form and cannot contain NUL. Automatic children use the publication
ID; aggregate children use the aggregate-import ID. Collision and idempotency
tests cover both reference kinds, IDs, run IDs, N aggregate children,
conflicting duplicate payloads, and legacy exact workbook SHAs.

The migration is a separately reviewed v9 -> v10 -> v11 prerequisite. Before
v11 it asserts the exact supported v10 schema/version and that the catalog does
not contain `aggregate_import_id` or any v11 object; it rejects an unexpected
or partial v11 schema. After adding the column, it asserts that every
pre-existing value is SQL `NULL`, runs integrity checks, and only then advances
the schema version. Any failure rolls back. Readers must continue to read older
rows without a fabricated aggregate import.
Focused DuckDB 1.5.5 evidence covers catalog assertions, the plain nullable
column addition, legal repeated NULL values, non-NULL unique-index enforcement,
writer rejection of dangling aggregate references, and rollback.

### Hash domains and exact decision-group inputs

For canonical UTF-8 bytes `b`, all derived identifiers use
`SHA256(domain_tag || 0x00 || b)` unless explicitly stated otherwise. The
domains are disjoint:

- `performance_v2_decision_group_v11` for `decision_group_id`;
- `performance_v2_selection_overlay_run_v11` for an overlay run key;
- `performance_v2_selection_review_import_v11` for automatic/aggregate child
  review-import keys;
- `performance_v2_controls_v11` for the canonical controls hash;
- `performance_v2_publication_operation_v11` for the frozen operation digest;
- `performance_v2_aggregate_manifest_v11` for the aggregate manifest digest;
- raw workbook SHA-256 over exact bytes for export/upload artifact hashes.

`decision_group_id` has exactly these ordered inputs:

1. domain tag `performance_v2_decision_group_v11`;
2. manifest contract version;
3. canonical controls hash, including filters 11-27;
4. fixed phase/limit/filter contract version;
5. sorted entries of `(pair, side, source_run_id, manual_head_timestamp,
   manual_head_import_id, manual_head_row_id, tagged exported tuple,
   decision/reason trace)`.

It excludes `publication_kind`, `operation_key`, and artifact hashes. An
equivalent XLSX and button operation may share this decision group, while
their publication headers, operation keys, and artifact hashes remain
separate. The frozen manifest pins each source/head/tuple/trace used by the
group.

### Origin relationship and reader policy

Automatic review origin is true if and only if its run is mapped with role
`OVERLAY`, its `aggregate_import_id` is SQL `NULL`, its child key matches that
publication, and `request_json.ranking_scope` is exactly
`AUTOMATIC_REJECTION_OVERLAY`. Any redundant automatic marker must agree with
that mapping; disagreement is a typed integrity error. Every other review is
manual, including every aggregate child, historical, strict, Card6, and
ADR-0064/0065 path. Aggregate children target `SOURCE` when present and
`OVERLAY` otherwise; aggregate provenance still means manual. The relationship
is the mapping, not a new origin column.

The effective-rejected helper is true when raw review status is `REJECTED`, a
global tag rejects the row, or a rejection/`DROP` source rejects it. Automatic
transition is a universal no-op for an effectively rejected row. F/R cohort
admission uses the latest manual review only.

The marker is added to the `effective_selection_decisions` overlay set. An
overlay is active only after it has review rows; only reviewed rows overlay
prior state, and out-of-cohort state survives. Its `selection_results` retain
the complete evaluated rowset, including automatic fields,
`stage_trace_json`, and reasons. When this overlay has `stages=()`, only
stage-dependent lookup/re-export may borrow stages from the SOURCE mapped in
the same publication/partition; missing SOURCE expires the operation and no
arbitrary latest run is searched. The strict reader excludes overlays. Card6
does not gain a new latest-run check. The control reader ignores overlays, but
a later eligible normal/control run makes the control state stale. Panel
stage/source and publication-context regime views use mapped SOURCE while
effective projections include reviewed overlay facts; without publication
context, regime selection uses the existing latest eligible non-overlay rule.
ADR-0064/0065 prior lookup excludes the current overlay and reconciled lookup
uses `effective_selection_decisions`.

One global `latest_raw_user_review_by_strategy` helper selects the newest
committed review per canonical strategy with deterministic timestamp/import/row
ordering and returns nullable aggregate plus derived automatic/publication
provenance. Latest-user-review, tag computation, and Portfolio review input use
it; Portfolio metrics/regime/stages still use eligible source readers. Prune
preserves all references without choosing an overlay as source keeper. Existing
four snapshot callers retain their behavior; the new publication service is
used only by new routes and never does generic persistence tag upserts.

### Idempotency, locking, and time ordering

The client creates `operation_key` before its first mutation request. An unseen
mutation creates its header. The same key with the same frozen digest is a
zero-write success, including a concurrent race; the same key with a different
digest is a typed conflict. Unknown, retired, or expired lookup/re-export
returns `PUBLICATION_KEY_NOT_FOUND`. A committed key returns
`ALREADY_COMMITTED_REEXPORT_REQUIRED`; re-export is frozen/read-only. An exact
duplicate upload succeeds idempotently, while a different upload digest
conflicts.

Every multi-partition mutation takes the existing cross-process writer lock
once and commits one global transaction. Workers compute only. Inside that
transaction, read the global `MAX(selection_review_imports.imported_at_utc)`;
compute one `normalized_latest` TIMESTAMPTZ value at microsecond precision as
`max(transaction_now truncated to microseconds, global_max + 1 microsecond)`
and assign that exact value to the aggregate header and every child. No
per-partition allocator or global event-order table is introduced.

## Consequences

- Publication identity, source/overlay provenance, aggregate import identity,
  and exact artifact hashes are independently addressable without changing
  ordinary run rows.
- Existing readers can remain compatible: old rows have no aggregate import,
  ordinary `workbook_sha256` remains required, and overlay visibility is selected
  by reader policy rather than by a broad schema flag.
- Retirement deletes affected mappings, aggregate review rows/imports,
  automatic review rows/imports, overlays, and then existing pair facts.
  Headers are deleted only when fully orphaned and retention permits it.
  Cross-pair headers become partially retired and their artifact expires when only some
  pairs are deleted. Maintenance, compact, merge, orphan/reachability, and
  prune audit and preserve referenced publication/run/import records and return
  typed not-found/expired results for retired or missing records. Card9 keeps
  its user-facing scope but follows this deletion order.
- The migration and all new writers require focused tests plus independent
  review before any feature implementation can claim completion.

## Advisor notes (non-blocking)

- Child-key domain separation is explicitly retained even where a child key is
  deterministically derived from a source/run tuple; no derived key may reuse
  an ordinary run, operation, manifest, or raw workbook hash domain.
- The pre-migration `aggregate_import_id IS NULL` assertion should remain a
  visible migration test and evidence item. It is a safety check, not a reason
  to relax the nullable compatibility path for old rows.

## Task 8 evidence status (draft)

The current documentation-only ledger records the following root evidence and
does not authorize implementation or mark P5.2 complete:

- v11 migration tests and independent review: `PASS`.
- Task2 selection suite: `253 passed`; pure decision/review evidence: `PASS`.
- Task3 publication suite: `65 passed`; publication tests/review: `PASS`.
- Task4 all-pairs focused suite: `17 passed`; implementation/evidence: `PASS`.
- Task5 aggregate-import suite: `12 passed`; independent review:
  `CODE_REVIEW_PASS`.
- Task6 Panel suite: `205 passed, 4 skipped`; static UI suite: `172 passed`;
  root verification and independent review: `CODE_REVIEW_PASS`.
- Task7 filters 11-27/performance implementation and audit: root verification
  and independent review: `CODE_REVIEW_PASS`.

The current v11 contract-suite accounting is `469 passed` with `20 warnings`
total. The initial full rerun recorded `455 passed` plus `14` compact failures,
all solely from the `C:\Temp` capacity guard. A sanctioned capacity mock reran
the compact module with `30 passed`, so all `469` logical tests pass. No real
full-corpus compaction rehearsal is claimed; the guard itself was tested by the
mock.

The bounded PARETO/filter measurement conclusion is that the existing
`PARETO_PLATEAU_POINTS_PER_ORDER` reason/alias expectation is unchanged;
disabled legacy filter stage bodies skip, while candidate preparation remains
unconditional. Legacy filters are therefore collapsed presentation-only; no
risky loader optimization or safe-disable branch landed. This is not a
production performance claim.

The final holistic independent review returned `CODE_REVIEW_PASS`; P5.2 is
implemented. This evidence makes no live-database, tick-test, or DD5-retest
claim.
