# Performance v2 all-pairs filtering and direct rejection (P5.2)

Status: draft/proposed P5.2 for product and plan review. Planning only; not an
implementation authorization. Date: 2026-10-10. Task8 evidence below is a
documentation draft and does not change this status.

Implementation and database mutation are not authorized by this planning
contract. The P0 decisions in this active contract must be verified against
current code and explicitly approved before the affected implementation slice
starts.

## P5.2 active contract

This is the single current normative contract. Superseded P3/P2/P4/P5.1
material is not normative; the ten P5.1 review findings are summarized once in
the plan's compact disposition ledger. P5.2 retains independent all-pairs filtering,
the existing single-pair path, combined XLSX, atomic aggregate review import,
explicit `Write Rejected`, direct status comments, Top N, exact row colors,
equity compatibility, Card9 eligibility, disabled filters 11-27, current
limits, and measured performance.
It prefers the current review/snapshot tables, exporter/importer, writer lock,
and timestamp ordering. No global event-order framework, second general
ledger, blob store, or generic event architecture is added unless P0 proves it
necessary.

The implementation plan is
[`docs/superpowers/plans/2026-10-09-performance-v2-all-pairs-filtering-and-rejection.md`](../superpowers/plans/2026-10-09-performance-v2-all-pairs-filtering-and-rejection.md).

### UI, request, and partitions

Screen 4 adds `Process all pairs` above the stage-order table, alongside
`Only Finalist & Reserved`. The checkbox box height equals the label text line
height. Pair and Side selectors remain intact for single-pair mode; all-pairs
disables them for the operation without erasing their values. New labels and
comments are English. `process_all_pairs` is browser-operation-only: omitted
and `false` preserve the old canonical request shape/hash and
`NOT_LATEST_RUN`; it is not persisted in ordinary per-partition requests, and
unknown request keys remain strict errors.

The server discovers current `Pair + Side` partitions, orders them
deterministically, freezes one request, and applies the existing single-
partition engine independently. Relative thresholds, survivors, Pareto,
ranking, and Top N never cross partitions. P0 verifies whether pair-specific
overrides exist and stops for an approved contract if they do. Empty
partitions are valid. Preview and evaluation happen only on explicit user
actions, not on every checkbox or threshold edit.

With `Only Finalist & Reserved` on, admission is newest supported raw MANUAL
`FINALIST` or `RESERVE` only; tags, sources, and effective status cannot widen
the cohort. Synthesized RESERVE rows inherit manual-origin provenance and count
in this cohort. With it off, every stored current strategy is admitted,
including SQL NULL, the exact empty string, whitespace, unknown historical,
legacy, and other non-F/R statuses. P0 verifies whether current constraints
permit such historical values; it must not silently disallow them.

### Status transitions and equity sources

An approved automatic hard-filter transition sets effective status to canonical
`REJECTED`, clears User Rank and Analog Of ID, preserves the existing Comment,
synchronizes the existing rejected tag/source, and never deletes facts or sets
`DISCARDED`. If any current tag/source already makes a row effectively
REJECTED, every raw status is a universal no-op: no status, rank, comment,
analog, or marker change.

For an actual transition, preserve the existing comment and append at most one
English marker only for a nonempty prior status. SQL NULL and the exact empty
string append no degradation marker and leave Comment unchanged:

| Raw prior status | Marker |
| --- | --- |
| `FINALIST` | `Degraded Finalist` |
| `RESERVE` | `Degraded Reserved` |
| Any other nonempty value, including `ANALOG`, `FILTERED`, whitespace, mixed-case, or unknown historical status | `Degraded "<prior status>"` |
| SQL NULL or exact empty string | none; preserve Comment unchanged |

The quoted form is the rule for an unknown nonempty canonical or historical
status; it is not grounds for excluding the candidate. P0 approves delimiter,
deduplication, safe visible encoding, and the current 1000-character overflow
rule. Existing comments are never silently truncated and unapproved overflow
is zero-write.

Equity hard `DROP` is a distinct force-source lifecycle, not a generic user
status transition and not a direct rank/comment degradation. P0 obtains user
approval for whether all-pairs invokes existing DROP behavior. Manual clear
cannot clear an active hard source. Equity `STALLED` is an exclusion/Reserve
outcome and is not a direct hard rejection.

### Exact Top N and row fills

When `rank_robust_top_n.enabled` is false, Top N membership work is zero and no
light-green fill is produced. Outside-Top-N membership is true only when the
same-run rank stage is enabled with N, the row entered that rank stage and
passed earlier stages, it is a valid ranked representative, `final_rank > N`,
and the actual rank-stage elimination has `auto_reason=RANK_ROBUST_TOP_N`.
There is no reranking. `selection_runs.top_n` or `Auto Rank` alone is not
evidence. Prior/effective rejected, unrankable, ANALOG, FILTERED, STALLED
reserve, and nonparticipant rows are never light green.

One provenance classifier fills the full used candidate row. Among non-red
rows, the first actual sequential exclusion wins:

| Category | RGB | Evidence |
| --- | --- | --- |
| effective REJECTED | `F4CCCC` | absolute override |
| same-run Equity STALLED exclusion | `B7B7B7` | filter enabled plus exclusion trace |
| Minimum Shift exclusion | `CFE2F3` | actual `filter_min_shift` exclusion |
| first filters 7-10 exclusion | `FFF2CC` | first actual exclusion in that range |
| passed | `D9EAD3` | no exclusion |
| valid ranked representative outside enabled Top N | `EAF4E5` | exact predicate above |

Across workbook variants, exact origin/run/request/result/trace provenance is
required for every non-red category. Without it, only effective REJECTED may
be red; every other row remains existing/default normal green. Visible
`STALLED`, `Auto Status=RESERVE`, and rank-only snapshots never imply gray.
The fill is full-row. P0 inventories ordinary, aggregate, control, RETEST,
and PerformanceDB exporters and routes only compatible paths through the
classifier.

The `Regime rank` header, index, count, and order remain unchanged. At the
workbook boundary canonical `state=STALLED` displays `STALLED`, not
`RESERVED`; canonical rank remains `RESERVED`, with codec vocabulary,
`equity-regime-v1`, JSON, caches, snapshots, runtime filtering, and ranking
unchanged. If P0 finds that this is not presentation-only, stop for a new
approved migration.

### Combined XLSX and publication transaction

All-pairs export is one combined XLSX reusing current columns and editable
fields, plus only the minimal approved aggregate manifest: version, database
identity, deterministic partition order, Pair/Side, selection run, request and
config fingerprint, source revision, complete rowset counts/digests, and
candidate identities. The exact export SHA-256 belongs to the publication
header; the exact upload SHA-256 belongs to the aggregate-import header.
Ordinary run `workbook_sha256` remains its existing exact-workbook identity,
while new automatic/aggregate review-import children use the internal v11 key
defined above.

The complete workbook bytes, bounds, rowsets, metadata, and post-transition
cell values are built and validated from the frozen package before taking the
publication lock/transaction. The service then acquires the existing writer
lock, revalidates, commits, and returns those already-built bytes. It never
renders for the first time after commit. Build, bounds, rowset, or stale
failure discards bytes and writes zero rows. `Write Rejected` and export call
the same common service; the button returns after persistence without a file.

Aggregate import validates the whole file before one transaction: duplicates,
cross-partition Strategy ID collisions, foreign/stale run or source identity,
mixed versions, altered automatic fields, incomplete/extra rowsets, invalid
user fields, and all limits reject atomically. Existing single-run import
remains supported and at least as strict.

Unchanged exported rows are a channel-preserving no-op, not a raw manual
status. This aggregate no-op is an exact exported-tuple equality and is
separate from the effective-rejected helper. If the submitted user tuple
differs from the state frozen at export, the complete row is explicit manual
row-level review, not a field mask. A newer conflicting review action after the
frozen review head rejects the whole import stale. Test unchanged, edited,
and comment-only round trips; export cells are post-transition values. No
binary baseline/blob store is added.

The client creates a UUID operation key before the first mutation request and
keeps it for retry. The server binds it to the frozen digest and records it at
commit. Same key plus same digest cannot mutate twice; same key plus different
digest is a typed conflict. A known committed retry returns
`ALREADY_COMMITTED_REEXPORT_REQUIRED`; it does not promise byte-identical
recovery and stores no blob. UI offers a fresh export from the committed/current
snapshot without new status writes. Unknown, retired, or expired keys return
`PUBLICATION_KEY_NOT_FOUND`. Preserve the exact ordinary workbook SHA contract
and the header-only exact export/upload hash contract for v11 publications.

Automatic publication is an overlay run: it does not replace the latest
ordinary Pair+Side run, discard out-of-cohort state, or invalidate existing
ordinary workbooks. Freeze latest ordinary run per partition; aggregate import
validates run kind and precondition. Test latest-run consumers and ordinary
workbook readability.

Every workbook row lookup is scoped to its artifact: publication operations
require `publication_id`, aggregate-import operations require
`aggregate_import_id`, and both also require the canonical Pair/Side and row
or strategy key. Missing scope is `INVALID_ARGUMENT`. A syntactically valid key
that is absent from that artifact and partition is scoped `NOT_FOUND`, even if
the key exists in another artifact; only internal integrity/audit code may
report `SCOPE_MISMATCH`. No user-facing path performs a global unknown-key
lookup.

### Ordering, schema, maintenance, and workers

There is no global `event_order` by default. P0 performs an `rg`-backed
read/write inventory of all PerformanceDB paths: import, export, version
checks, migrations, merge, filters, compact, maintenance, prune, Portfolio,
finalist RETEST, Panel, tests, and specifically ADR-0064/0065. It proves
whether the existing shared writer lock plus a transaction-global
`MAX(selection_review_imports.imported_at_utc)` and microsecond allocation
preserves every reader/writer. Old rows keep a compatible reader fallback. If
the proof fails, stop for the smallest approved alternative; do not add a
global event framework.

The full per-path schema compatibility evidence must be in this spec and
`progress.md` before implementation. If a field is necessary, a dedicated
migration task/commit precedes feature work and follows v9 -> v10 -> v11,
covering import/export/version/migration/merge/filter/compact/maintenance/
prune/Portfolio/finalist-RETEST/Panel/tests with independent review.

Card9 retains its existing user-facing scope but uses the v11 retirement order:
mappings, affected aggregate children, affected automatic children, overlays,
and then existing pair facts. A fully orphaned header is deleted only when
retention permits; cross-pair headers become partially retired and expired when
only some pairs are deleted. P0 inventories publication/run
metadata through maintenance, compact, readability, merge, orphan, and
reachability; retired or missing records return typed not-found/expired
results for retry/re-export.

Workers come only from `load_duckdb_import_settings(config_path).workers`.
Missing value uses the shared configured default 4; invalid explicit values
retain the existing typed config error. The coordinator never calls legacy
helpers with a 16/cpu-count/clamped fallback. The current limits remain
20 MiB upload, 256 ZIP entries, 100 MiB declared uncompressed, and 100,000
rows; characterize boundaries for all four.

### Legacy filters, TDD, and approval gates

P0 records exact runtime IDs behind filters 11-27 and measures readiness/cache,
evaluation, XLSX build, publication transaction, and import separately. If
disabled overhead is negligible, keep hidden controls in collapsed `<details>`
with canonical request/hash fields and behavior unchanged. If material, use a
separately approved `filter_time_consistency`-style safe-disable branch:
enabled legacy requests get a typed error, disabled requests are inert, old
snapshots remain readable, and hidden controls do not disappear from canonical
hash/equivalence without explicit approval and tests.

In P0, the old RESERVED assertion stays characterization-green. At the start
of styling, change the expected visible value to STALLED, observe red, then
implement the boundary map and update the active XLSX column contract and
equity status map. Tests prove STALLED has no RESERVED text, all other values
and canonical rank/cache/snapshot behavior stay unchanged.

Implementation stops after P0 until the user approves the allowlist and DROP
source, workbook manifest/import, publication source of truth, marker/overflow
rules, Top N and colors, unchanged/edited import, key/digest retry states,
run-kind precondition, ordering proof or alternative, schema inventory,
filters 11-27 branch, limits, workers, and measured budgets. All tests use
`.venv\\Scripts\\python.exe -m pytest` with a unique C: TEMP/TMP directory
removed afterward. Focused and broader tests, `git diff --check`, and
independent `CODE_REVIEW_PASS` are required before completion claims.

### Mandatory additive v11 publication architecture

P5.2 adds exactly three new v11 tables and one nullable column to the existing
schema. It does not replace existing selection/review facts, add an origin
column/blob/event framework/review-table rebuild, or relax any other
nullability. This schema is a planning contract; migration is a separately
reviewed prerequisite before feature implementation.

`selection_publications` is the header for one export/publication operation.
It records publication identity, kind, operation key, frozen operation digest,
manifest contract version, decision group, database/source revision, canonical
controls JSON and hash, frozen render-model JSON and hash, complete evaluated
rowset digest, and exact artifact hashes in their own header fields. Exact
exported and uploaded workbook SHA-256 values are stored only in the new
publication/import headers; they are never copied into an existing run as an
artifact identity.

`selection_publication_runs` maps a publication to its per-partition runs.
Each mapping has `role` `SOURCE` or `OVERLAY`, `pair`, `side`, and the run
foreign key. The mapping enforces one row per
`(publication_id, pair, side, role)` and one mapping per run within a
publication. A partition may therefore have at most one ordinary source and
one automatic overlay. `SOURCE` is the frozen ordinary run used as the
precondition; `OVERLAY` is the automatic all-pairs result and never replaces
the source run.

`selection_aggregate_imports` is the aggregate-import header and manifest
anchor. It records the aggregate import identity, publication/operation
identity, manifest contract version, source revision, complete partition
rowset counts and digests, candidate identities, exact uploaded workbook
SHA-256, and lifecycle/retirement state. The existing
`selection_review_imports` table gains only a plain nullable
`aggregate_import_id` column. The supported DuckDB 1.5.5 migration creates a unique index on
`(aggregate_import_id, selection_run_id)`; it does not add an ALTER-table FK or
UNIQUE constraint and does not rebuild either existing history table. The
writer rejects a child whose aggregate header is absent, and an integrity /
reachability audit enforces the same relationship. Existing rows are retained
with SQL `NULL`; no other nullable column or existing constraint is relaxed.

Before v11, migration asserts the exact supported v10 schema/version and that
the catalog contains neither `aggregate_import_id` nor any v11 table/index. A
catalog with the column already present, an unexpected object, or a partial v11
schema is rejected. After the column is added, migration asserts that every old
row is SQL `NULL`; schema version advances only after all catalog and integrity
checks succeed, and any failure rolls back.
Focused DuckDB 1.5.5 evidence must cover the catalog assertions, plain nullable
column addition, multiple legal NULL values, unique-index enforcement for
non-NULL aggregate/run pairs, and rollback.

`selection_review_imports.workbook_sha256` remains `NOT NULL UNIQUE`. New
automatic and aggregate-child imports use an internal, domain-separated key
because an exact aggregate workbook SHA cannot be repeated for each child. It
is SHA-256 over this exact UTF-8 byte sequence, with literal `0x00` separators:

`SHA256("performance_v2_selection_review_import_v11\\0" ||
reference_kind ("publication_id" | "aggregate_import_id") || "\\0" ||
canonical_reference_id || "\\0" || canonical_selection_run_id)`.

Automatic children use `reference_kind=publication_id` and the publication ID;
aggregate children use `reference_kind=aggregate_import_id` and the aggregate
import ID. This key is distinct from legacy exact workbook SHAs and from every
other derived identity domain; collision and idempotency tests pin both kinds.
Canonical IDs use the repository's existing lowercase canonical form and may
not contain NUL. Repeating the same kind/reference/run is idempotent only when
the immutable child payload also matches; a different payload is an integrity
error. Tests distinguish reference kind, reference ID, run ID, automatic from
aggregate children, every child in one N-partition aggregate, and conflicting
duplicates.
`selection_runs.workbook_sha256` remains `NOT NULL` and keeps its current
meaning for ordinary runs. An overlay has no workbook artifact, so its stored
key is the separate domain-derived key
`SHA256("performance_v2_selection_overlay_run_v11\\0" || canonical overlay
manifest tuple)`; exact export/upload hashes remain header-only.

#### Publication identity, hashes, and decision group

The operation key is created before the first mutation request and is bound to
the frozen manifest digest. An unseen mutation creates its header; the same
key with the same digest performs no writes, including a race; the same key
with a different digest returns a typed conflict. Lookup/re-export of an
unknown, retired, or expired key returns `PUBLICATION_KEY_NOT_FOUND`. A known
committed key returns `ALREADY_COMMITTED_REEXPORT_REQUIRED`; re-export and
lookup are read-only and never mutate status, review, tag, source, or run
rows. Exact duplicate upload succeeds idempotently when its operation and
workbook digest match; a different digest conflicts.

`decision_group_id` is the SHA-256 of the exact ordered tuple below, using the
domain tag `performance_v2_decision_group_v11` and canonical UTF-8 encoding:

1. `performance_v2_decision_group_v11`;
2. manifest contract version;
3. canonical controls hash, including filters 11-27;
4. fixed phase/limit/filter contract version;
5. sorted `(pair, side, source_run_id, manual_head_timestamp,
   manual_head_import_id, manual_head_row_id, tagged exported tuple,
   decision/reason trace)` entries.

The inputs exclude `publication_kind`, `operation_key`, and all artifact
hashes. Equivalent XLSX and button operations may therefore share a decision
group, while their publication headers and operation/derived keys remain
separate. The manifest pins every source run, manual head, tagged exported
tuple, and decision/reason trace used by the group.

The hash domains are disjoint: the decision-group domain above, the
`performance_v2_selection_overlay_run_v11` run-key domain, the
`performance_v2_selection_review_import_v11` child-key domain, the canonical
controls domain, the frozen operation/manifest/render-model domains, and the
raw-byte workbook SHA-256 domain. No artifact hash is silently reused as a
decision, run, operation, review-import, or aggregate-import identity. The
publication header stores canonical controls JSON plus hash, frozen render
model JSON plus hash, the complete evaluated rowset digest, and the exact
export hash so a re-export is write-free and deterministic.

Re-export loads the header and required mappings/runs, rejects a partially
retired or unreachable publication, reconstructs the complete canonical
rowset, verifies controls/render-model/rowset digests before rendering, renders
only from the frozen model, and verifies the resulting exact workbook hash.
It never treats either existing `workbook_sha256` field as the v11 artifact
hash.

#### Origin, effective rejection, and reader policies

Automatic review origin holds if and only if all of these are true: the child
run is mapped in `selection_publication_runs` with role `OVERLAY`; its
`aggregate_import_id` is SQL `NULL`; the child key references that publication;
and `request_json.ranking_scope` is exactly
`AUTOMATIC_REJECTION_OVERLAY`. Every other review is manual, including any
aggregate child even when it targets an overlay, historical/strict/Card6
imports, and ADR-0064/0065 paths. Aggregate children target the mapped
`SOURCE` run when that partition has one, otherwise the mapped `OVERLAY` run;
the presence of an aggregate ID still makes the review manual. The redundant
automatic marker in `effective_selection_decisions` must agree with this
relationship; disagreement is a typed integrity error. The derived semantic
marker `manual_aggregate_marker` means exactly
`aggregate_import_id IS NOT NULL`; it is not another column. No origin column
is added.

The shared effective-rejected helper returns true when raw review status is
`REJECTED`, a global tag makes the row rejected, or a rejection/`DROP` source
does so. An effective rejection is a universal no-op for automatic review
transitions. F/R cohort admission uses the latest manual review only; an
overlay cannot widen or replace that cohort.

An overlay is active only after it has review rows. Its overlay set in
`effective_selection_decisions` is selected by the dedicated
`AUTOMATIC_REJECTION_OVERLAY` marker, not by an empty-stage heuristic. Only
reviewed rows overlay the prior state, and out-of-cohort state is preserved.
The overlay `selection_results` store the complete evaluated rowset, including
automatic fields, `stage_trace_json`, and reasons. For an
`AUTOMATIC_REJECTION_OVERLAY`, `stages=()` means only that stage-dependent
lookup and deterministic re-export borrow the stage definition from the
`SOURCE` mapped in the same publication and partition. Missing or unreachable
SOURCE expires those operations; no reader searches for an arbitrary latest
run and no other empty-stage semantics change.

Reader policies are explicit: strict review excludes overlays; Card6 is
unchanged and performs no new latest-run check; the control path ignores
overlays, with a later eligible ordinary/control run making an older control
state stale; Panel stage/source views use the mapped SOURCE while its effective
projection includes synchronized rejection/tag/source facts. With publication
context, PerformanceDB latest-regime selection uses the mapped SOURCE; without
it, the existing latest eligible non-overlay policy remains. ADR-0064/0065
prior-state lookup excludes the current automatic overlay, while reconciled
state calls `effective_selection_decisions`, so only reviewed rows replace
prior state and an empty overlay is a no-op.

One internal `latest_raw_user_review_by_strategy` helper selects the newest
committed raw review globally by canonical strategy identity, ordered by
`imported_at_utc DESC` and the existing deterministic import/row tie-breakers.
It returns review fields, `selection_run_id`, review-import identity, nullable
`aggregate_import_id`, timestamp, the derived automatic/manual classification,
and derived publication ID when the automatic key and mapping agree.
`latest_user_reviews_by_strategy`, tag computation, and Portfolio review input
use this helper: aggregate manual and correctly classified automatic reviews
participate, and provenance is retained. Portfolio metrics, regimes, and
stages still come from eligible source/evaluation readers, never from an
automatic overlay merely because it is newer. Prune preserves all referenced
publication, aggregate, source-run, mapping, and review records and never
chooses an overlay as the source keeper. Existing four snapshot callers retain
their current behavior; the new publication service is used only by new routes
and never performs generic persistence tag upserts.

#### Transaction, timestamp, maintenance, and acceptance invariants

A multi-partition write takes the existing cross-process writer lock once and
commits one global transaction. Workers compute only. Inside that transaction,
read the global `MAX(selection_review_imports.imported_at_utc)` and compute one
`normalized_latest` TIMESTAMPTZ value at microsecond precision as
`max(transaction_now truncated to microseconds, global_max + 1 microsecond)`.
The aggregate header and every child receive that exact same value. No
per-pair/child clock, string timestamp, or global event-order table is
introduced.

Retirement is ordered to satisfy existing references: resolve affected
mappings/imports, delete affected publication mappings, delete affected
aggregate review rows/imports before their target pair runs, delete affected
automatic review rows/imports before their OVERLAY runs, then delete existing
pair facts. Delete a publication or aggregate header only when it is fully
orphaned and retention permits it. If a cross-pair header still spans surviving
pairs, derive it as partially retired from its frozen manifest versus surviving
mappings/children: audit history remains available, but artifact-level and
deleted-pair lookup/re-export are expired and no missing run is replaced by a
newer one. Maintenance, compact, merge, orphan/reachability, and prune audits
cover all three new tables, non-null aggregate references, marker/key/mapping
agreement, manifest reachability, and legal partial-retirement state.
Tests cover full-pair deletion after a publication and `Rejected` retirement
after a publication, including partial retirement.

The implementation plan must first inventory every v11 read/write path,
prove old-row reader compatibility, and add focused tests for the nullable
pre-migration `aggregate_import_id` assertion, role/partition uniqueness,
domain-separated overlay keys, origin-marker agreement, strict/control/Panel
reader policies, aggregate duplicate idempotency, operation-key race/conflict
states, one-lock/one-transaction timestamps, prune reachability, and all
existing P4 acceptance evidence. No implementation is authorized by this
specification.

## Task 8 draft evidence (2026-10-10)

This is the current documentation-only acceptance ledger. It records verified
root evidence without closing P5.2 or authorizing implementation:

| Slice | Verified evidence | Disposition |
| --- | --- | --- |
| Task 1 | Additive v11 migration tests and independent migration review | PASS |
| Task 2 | Selection suite: `253 passed`; pure decision/review evidence | PASS |
| Task 3 | Publication suite: `65 passed`; common publication-service tests/review | PASS |
| Task 4 | All-pairs focused suite: `17 passed`; coordinator/overlay evidence | PASS |
| Task 5 | Aggregate-import suite: `12 passed`; independent review: `CODE_REVIEW_PASS` | PASS |
| Task 6 | Panel suite: `205 passed, 4 skipped`; static UI suite: `172 passed`; root verification and independent review: `CODE_REVIEW_PASS` | PASS |
| Task 7 | Filters 11-27/performance implementation and audit; root verification and independent review: `CODE_REVIEW_PASS` | PASS |

The current v11 contract-suite accounting is `469 passed` with `20 warnings`
total. The initial full rerun recorded `455 passed` plus `14` compact failures,
all solely from the `C:\Temp` capacity guard. A sanctioned capacity mock reran
the compact module with `30 passed`, so all `469` logical tests pass. No real
full-corpus compaction rehearsal is claimed; the guard itself was tested by the
mock.

The measured PARETO/filter conclusion is bounded: the existing
`PARETO_PLATEAU_POINTS_PER_ORDER` reason/alias expectation is unchanged;
disabled legacy filter stage bodies are skipped, but candidate preparation
remains unconditional. Therefore legacy filters are collapsed
presentation-only; no risky loader optimization or safe-disable branch landed.
This does not establish a production performance claim.

The final holistic independent review returned `CODE_REVIEW_PASS`; P5.2 is
implemented. This ledger makes no live-database, tick-test, or DD5-retest
claim.

## Compact review-provenance note

The former full P3/P2/P4/P5.1 appendices were removed because they duplicated
or contradicted this P5.2 contract. The linked plan retains only the compact
P5.1 R001-R010 disposition ledger and one external P5.2 review prompt; current
behavior is defined above and in the linked P5.2 plan.
