# Performance v2 All-Pairs Filtering and Direct Rejection Implementation Plan

> **For agentic workers:** use the repository's required subagent-driven or
> executing-plans workflow, TDD for behavior changes, and verification before
> any completion claim. This is a planning document; its checkboxes do not
> authorize implementation.

Version: P5.2. Date: 2026-10-10. Status: draft/proposed P5.2, planning only,
not independently reviewed and not authorized for implementation.

The Advisor bridge was unavailable for this revision. The user explicitly
authorized root best-effort/self revision. This plan therefore records no
`PLAN_APPROVED` result for P5.2. Any earlier P5.1 Advisor approval, if found
in history, is superseded planning evidence only.

**Goal:** Add independent all-pairs selection, one atomically importable
combined XLSX, a safe direct `Write Rejected` action, provenance-correct row
fills, and measured processing/export improvements without regressing the
existing single-pair workflow.

**Architecture:** Keep the current single-`Pair + Side` selection engine as
the semantic unit. A bounded batch coordinator freezes one UI request,
discovers current partitions, loads shared database facts in sets, and applies
the request separately to each partition. A versioned aggregate workbook
links immutable per-partition selection runs and reuses strict review
validation. A common rejection-publication application service owns
evaluation, decision, transition, revalidation, synchronized review-row/tag/
source writes, and one transaction. Export builds the aggregate workbook and
validates bounds before invoking that service; the button invokes the same
service and stops after persistence. The client creates a UUID operation key
before mutation; the server binds it to the frozen digest. A known committed
retry returns `ALREADY_COMMITTED_REEXPORT_REQUIRED`, with no byte-identical
retry promise or blob; unknown/retired/expired keys return
`PUBLICATION_KEY_NOT_FOUND`.

**Spec:** [Performance v2 all-pairs filtering and direct rejection](../../specs/2026-10-09-performance-v2-all-pairs-filtering-and-rejection.md)

## P5.2 active plan

P5.2 is deliberately right-sized. It keeps the existing selection engine,
review/snapshot tables, importer/exporter, writer lock, and timestamp ordering
where P0 proves them sufficient. It adds only the mandatory additive v11
publication headers/mapping and aggregate-import anchor described by the spec;
there is no global event-order framework, second general ledger, blob store, or
generic event architecture.

The mandatory additive v11 slice is `selection_publications`,
`selection_publication_runs` with `SOURCE`/`OVERLAY` role and Pair/Side
uniqueness, and `selection_aggregate_imports`, plus only nullable
`selection_review_imports.aggregate_import_id`, with a plain-column migration
and unique index `(aggregate_import_id, selection_run_id)`. `selection_runs.workbook_sha256`
stays `NOT NULL`; review imports stay `NOT NULL UNIQUE`; overlay and child keys
are domain-separated; exact export/upload hashes live in the new headers only.

### P5.2 delivery gates

- [ ] P0 inventory, hard-filter approval, source/DROP approval, and measured
  budgets are accepted by the user.
- [ ] The reviewed v11 migration is accepted before any feature writer.
- [ ] The four existing snapshot callers remain unchanged and are never routed
  through the new service.
- [ ] Focused TDD, relevant broader tests, cleaned C: TEMP/TMP, `git diff
  --check`, independent `CODE_REVIEW_PASS`, and root acceptance exist before
  any completion claim.

The product contract remains unchanged for Pair+Side isolation, checkbox
semantics, comment/rank/Analog rules, exact colors and precedence, same-run
enabled Top N without reranking, XLSX-only STALLED, hard-filter approval,
filters 11-27/hash behavior, shared worker default 4, limits/performance/Card9,
and the four legacy snapshot callers.

### Compact P5.1 R001-R010 disposition ledger

This is the only retained P5.1 review ledger. The P5.2 spec is normative if
this table and older wording differ.

| ID/severity | Finding | P5.2 disposition |
| --- | --- | --- |
| P5.1-R001 HIGH | New review imports had no valid `workbook_sha256`. | Keep the column `NOT NULL UNIQUE`; under distinct `reference_kind` values and `performance_v2_selection_review_import_v11`, derive every automatic key from `(publication_id, selection_run_id)` and every aggregate key from `(aggregate_import_id, selection_run_id)`. Pin collision/idempotency tests and keep exact artifact hashes header-only. |
| P5.1-R002 HIGH | Overlay mapping misclassified manual aggregate imports. | Automatic iff `OVERLAY` mapping, `aggregate_import_id IS NULL`, matching publication child key, and exact `request_json.ranking_scope=AUTOMATIC_REJECTION_OVERLAY`; all other reviews are manual. Aggregate children target SOURCE when present, otherwise OVERLAY. |
| P5.1-R003 HIGH | An unrecognized overlay could wipe ordinary/out-of-cohort state. | Add the dedicated marker to the effective-decision overlay set; activate only after review rows exist; overlay only reviewed rows; store the complete evaluated rowset in overlay `selection_results`; preserve out-of-cohort state. |
| P5.1-R004 MEDIUM | DuckDB cannot add the planned FK/UNIQUE by `ALTER TABLE`. | Add a plain nullable column and `CREATE UNIQUE INDEX (aggregate_import_id, selection_run_id)`; writer and integrity audit enforce the aggregate header; reject pre-v11 partial schemas and assert old values are NULL after the add. |
| P5.1-R005 MEDIUM | New references conflicted with Card9 full-pair deletion. | Delete affected mappings, aggregate children, automatic children, overlays, then pair facts; delete a header only when fully orphaned and retention permits it, otherwise retain it as partially retired and expire artifact/deleted-pair lookup and re-export. Audit all references; test full-pair and Rejected retirement. |
| P5.1-R006 MEDIUM | Overlay content and frozen controls were underspecified. | Persist complete overlay evaluated rowsets (automatic fields, stage trace, reasons), canonical controls JSON+hash, frozen render-model JSON+hash, rowset digest, and exact export hash; only `AUTOMATIC_REJECTION_OVERLAY` with `stages=()` may borrow the same-publication mapped SOURCE stages, never an arbitrary latest run. |
| P5.1-R007 MEDIUM | Reader policy omitted regime, ADR-0064/0065, user-review, and Portfolio consumers. | State strict, Card6, control, Panel stage/source/effective, PerformanceDB latest regime, ADR-0064/0065 prior/reconciled, global latest-raw and latest-user-review behavior, Portfolio provenance/source evidence, and prune policies explicitly and test each. |
| P5.1-R008 MEDIUM | P4/P5.1 leftovers contradicted the normative contract. | Keep one P5.2 contract, remove stale exact-SHA, unknown-key, timestamp, empty-stage, and conditional-schema wording, and retain only this compact ledger. |
| P5.1-R009 LOW | A pre-migration `aggregate_import_id IS NULL` check was vacuous. | Before v11 assert the column is absent and reject a partial catalog; after adding it assert every old row is SQL NULL. |
| P5.1-R010 LOW | Timestamp type and `normalized_latest` scope were imprecise. | Use TIMESTAMPTZ at microsecond precision and compute global `MAX(selection_review_imports.imported_at_utc)` inside the transaction; share the normalized base with aggregate header and children. |

### P5.2 task order and slice boundaries

#### Task 0 - P0 inventory, characterization, and approvals (read-only)

**Exact file group:** `src/mrs3/performance_v2_store.py`,
`src/mrs3/performance_v2_selection.py`,
`src/mrs3/performance_v2_selection_review.py`,
`src/mrs3/performance_v2_finalist_retest.py`,
`src/mrs3/performance_v2_maintenance.py`, `src/mrs3/panel.py`,
`src/mrs3/panel_performance_v2.py`, `src/mrs3/portfolio/input.py`,
`tests/test_performance_v2_store.py`, `tests/test_performance_v2_selection_review.py`,
`tests/test_performance_v2_maintenance.py`, `tests/test_panel_performance_v2.py`,
`tests/test_performance_v2_finalist_retest.py`, and the four existing snapshot
call sites.

Inspect the selection, review, store, equity-regime, finalist-RETEST, Panel,
browser, style, and focused-test modules named by the spec. Record exact
request fields/hash, UI payload, partition discovery, stage IDs/order,
cohort/manual-origin behavior, Top N trace, status/rank/comment/analog/source
tables, ordinary and overlay run kinds, RETEST tags,
`selection_contract_version`, workbook callers, writer lock,
`imported_at_utc` ordering, and Card9 maintenance behavior.
Inventory the four existing snapshot callers explicitly and preserve their
current snapshot-only behavior; none may be delegated to, modified, or
rerouted through the new overlay service.

The `rg`-backed schema inventory covers every PerformanceDB read/write path
named in the P5.2 contract and P5.1-R007/R010 ledger, including ADR-0064/0065, with compatibility evidence in the
spec and `progress.md` before code work. Characterize omitted/false
`process_all_pairs`, Top N-off zero work, current STALLED -> RESERVED display,
rank-only STALLED, review round trips, the 1000-character limit, resource
boundaries, and workers. Identify filters 11-27 exactly. Freeze the hard
allowlist, DROP question, aggregate manifest/import shape, row semantics,
run precondition, marker/overflow rules, palette/provenance matrix, and
timestamp-order proof. Stop for user approval.

**TDD/verification:** read-only `rg` inventory; characterization with
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp C:\Temp\mrs3-p52-p0 tests/test_performance_v2_store.py tests/test_performance_v2_selection_review.py tests/test_panel_performance_v2.py`; remove the unique temp directory and run `git diff --check`.

#### Task 1 - Mandatory additive v11 prerequisite schema migration

**Exact file group:** `src/mrs3/performance_v2_store.py`, migration/schema
helpers under `src/mrs3/`, `tests/test_performance_v2_store.py`,
`tests/test_performance_v2_maintenance.py`, and the active P5.2 spec/ADR.

The approved P5.2 architecture requires the dedicated additive v11 migration
on the supported DuckDB 1.5.5 engine after Task 0's inventory and before feature code: create
`selection_publications`, `selection_publication_runs`, and
`selection_aggregate_imports`; add only nullable `aggregate_import_id` plus
its unique `(aggregate_import_id, selection_run_id)` index, with no ALTER-table
FK/UNIQUE constraint or history-table rebuild; preserve
`selection_runs.workbook_sha256 NOT NULL` and `selection_review_imports.workbook_sha256
NOT NULL UNIQUE`; and use domain-separated overlay and review-import keys. A
v10 catalog with the column already present or a partial v11 schema is
rejected; after adding it, existing rows must be SQL `NULL`.
Inventory and test every import/export/version/migration/merge/filter/compact/
maintenance/prune/Portfolio/RETEST/Panel/test path, old-row readers,
maintenance, merge, and rollback. Obtain independent review before feature
work uses the field. No origin column or other nullability relaxation is
permitted.

Focused DuckDB 1.5.5 evidence must prove exact-v10/catalog assertions, plain
nullable-column addition, multiple NULL values, non-NULL unique-index
enforcement, dangling-header writer rejection, and transactional rollback.

**TDD/verification:** failing-then-green migration tests with
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp C:\Temp\mrs3-p52-p1 tests/test_performance_v2_store.py tests/test_performance_v2_maintenance.py -k "migration or schema or aggregate_import"`; run reader-compatibility tests, remove temp files, and run `git diff --check`.

#### Task 2 - Request handling and pure decisions

**Exact file group:** `src/mrs3/performance_v2_selection.py`,
`src/mrs3/performance_v2_selection_review.py`, pure helper modules if already
present, `tests/test_performance_v2_selection.py`, and
`tests/test_performance_v2_selection_review.py`.

Write failing tests first. Implement only normalized request handling and pure
decision functions for scope, cohort admission, hard allowlist, Top N
membership, status/marker/comment/Analog Of ID transitions, and source-channel
separation. Cohort OFF admits every stored status; cohort ON admits exact raw
MANUAL `FINALIST`/`RESERVE` only. On an actual hard-filter transition status is
`REJECTED`, rank and Analog Of ID are cleared, and the existing Comment is
preserved. SQL NULL and exact empty string append no marker and leave Comment
unchanged; `FINALIST` -> `Degraded Finalist`, `RESERVE` -> `Degraded Reserved`,
and every other nonempty prior value -> `Degraded "<prior status>"`. Test
effective-REJECTED universal no-op, overflow, and no reranking. No writer,
schema, or workbook-rendering code belongs here.

**TDD/verification:** failing-then-green focused tests with
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp C:\Temp\mrs3-p52-p2 tests/test_performance_v2_selection.py tests/test_performance_v2_selection_review.py`; remove temp files and run `git diff --check`.

#### Task 3 - Common publication service (the only new writer)

**Exact file group:** `src/mrs3/performance_v2_store.py`, the new publication
service beside existing Performance v2 services, and transaction-focused
tests under `tests/test_performance_v2_*.py`.

After Task 2 approval and the v11 migration review, write failing transaction
tests and implement one service under the existing PerformanceDB writer lock.
It revalidates the frozen package, uses one lock/one transaction for all
partitions, reads the global maximum import timestamp inside that transaction,
computes one UTC microsecond `normalized_latest`, and assigns that exact value
to the aggregate header and every child. It synchronizes review/tag/source
projections, and commits once. It accepts the operation-key/digest contract:
unseen creates, same/same is a zero-write retry including races, different is
a typed conflict, committed is re-export-only, unknown/retired/expired is
typed not-found, and exact duplicate upload is idempotent. The four existing
snapshot callers must not be delegated, modified, or rerouted; they retain
their current snapshot-only behavior. Only the new all-pairs, aggregate
export, and `Write Rejected` routes use this shared overlay service. No second
general ledger or generic persistence tag upsert is created. No coordinator,
exporter, UI mutation route, or button may write before this service is
independently reviewed.

**TDD/verification:** failing-then-green transaction, key/digest, collision,
rollback, and timestamp tests using `.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp C:\Temp\mrs3-p52-p3 tests/test_performance_v2_store.py tests/test_performance_v2_selection_review.py tests/test_performance_v2_finalist_retest.py`; remove temp files and run `git diff --check`.

#### Task 4 - All-pairs coordinator and overlay persistence

**Exact file group:** `src/mrs3/performance_v2_selection.py`,
`src/mrs3/performance_v2_store.py`, Panel route/coordinator files,
`tests/test_performance_v2_selection_review.py`,
`tests/test_performance_v2_store.py`, and relevant Panel tests.

Write failing partition-isolation, empty-partition, worker, latest-ordinary-
run, overlay, RETEST-tag, reader-policy, role-marker, and concurrency tests.
Reuse the current engine, load shared facts set-wise, freeze the latest
ordinary run per partition, and persist automatic results as an overlay with
`selection_contract_version`, a domain-separated derived run key, and an
explicit precondition. Map SOURCE and OVERLAY with pair/side uniqueness;
automatic origin exists only through the OVERLAY mapping and its redundant
marker must agree. Synthesized RESERVE rows retain manual origin and
ON-cohort eligibility. Store the complete evaluated overlay rowset in
`selection_results`, including automatic fields, `stage_trace_json`, and
reasons. The dedicated `AUTOMATIC_REJECTION_OVERLAY` marker is in the
effective-decision overlay set; an overlay is active only with review rows,
only reviewed rows overlay prior state, and out-of-cohort state survives.
Strict readers exclude overlays, Card6 does no new latest check, and control
ignores overlays but later eligible normal/control runs make state stale. An
overlay with `stages=()` borrows stages only from the same-publication mapped
SOURCE for stage-dependent lookup/re-export; missing SOURCE expires the
operation, and no arbitrary latest run is used. Panel and PerformanceDB regime
source views use SOURCE while effective projections include reviewed overlay
rows. ADR-0064/0065 prior lookup excludes the current overlay and reconciled
lookup uses `effective_selection_decisions`. The global latest-raw helper,
latest-user-review, tag computation, Portfolio review provenance/source
evidence, and prune policies are tested explicitly.
Aggregate children are manual and target SOURCE when present, otherwise
OVERLAY. Use only the shared worker loader (missing -> 4, invalid explicit ->
existing typed error). Never replace ordinary runs or out-of-cohort state.

**TDD/verification:** failing-then-green partition, role/origin, reader,
worker, and concurrency tests with `.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp C:\Temp\mrs3-p52-p4 tests/test_performance_v2_selection_review.py tests/test_performance_v2_store.py tests/test_panel_performance_v2.py`; remove temp files and run `git diff --check`.

#### Task 5 - Combined XLSX, atomic import, and both UI actions

**Exact file group:** Panel XLSX/export/import modules, browser assets/routes,
`src/mrs3/performance_v2_selection_review.py`, and focused Panel/import tests.

Write failing manifest, bounds, rowset, header/hash, role mapping, round-trip,
stale, import, duplicate-idempotency, and prebuild-order tests. Build and
validate workbook bytes, bounds, rowsets, limits, metadata, decision-group
inputs, publication headers, and post-transition cells before the writer
lock. Then call Task 3, revalidate, commit, and return those prebuilt bytes; a
build/stale failure writes zero rows. Never render for the first time after
commit. Keep exact export/upload hashes in the new headers only, keep
`selection_runs.workbook_sha256` and `selection_review_imports.workbook_sha256`
non-null, use the internal v11 child-key domain, and assert the pre-v11 absent/
post-add old-NULL migration conditions.

Import unchanged rows as channel-preserving no-op; changed complete tuples are
manual row-level review; reject any newer conflicting review head atomically.
Require artifact-scoped lookup (`publication_id` or `aggregate_import_id`, plus
Pair/Side and row key); missing scope is `INVALID_ARGUMENT` and any key outside
that artifact/partition is scoped `NOT_FOUND` without a global existence leak.
Preserve exact `workbook_sha256`, current columns, and single-run behavior.
Add `Process all pairs` above the stage table, preserve selectors and `Only
Finalist & Reserved`, and wire explicit preview/confirmation for export and
`Write Rejected`. Test operation-key states, key/digest conflict, restart,
fresh re-export, preserved Card6/retest latest-run behavior with no new Card6
latest-run check, and commit-between-
build/commit.

**TDD/verification:** failing-then-green manifest, bounds, rowset, round-trip,
stale, duplicate-idempotency, and prebuild-before-lock tests with
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp C:\Temp\mrs3-p52-p5 tests/test_performance_v2_selection_review.py tests/test_panel_performance_v2.py tests/test_panel_performance_v2_retest.py`; remove temp files, run `node --check` for changed browser JS, and run `git diff --check`.

#### Task 6 - Styling and STALLED boundary TDD

**Exact file group:** XLSX rendering/style helpers, the active XLSX column
contract and equity status map, and styling tests.

At the start of this task, replace the P0 expected visible RESERVED value with
STALLED and observe a failing test. Implement the workbook-only boundary map,
then update the active XLSX column contract and equity status map. Implement a
pure full-row classifier with exact `F4CCCC`, `B7B7B7`, `CFE2F3`, `FFF2CC`,
`D9EAD3`, and `EAF4E5` rules and same-run provenance fallback. Test red
override, first actual exclusion, disabled filter, rank-only STALLED, Auto
Status RESERVE, Top N off, valid outside Top N, every workbook variant, and
canonical rank/cache/snapshot compatibility.

**TDD/verification:** change the characterization expectation first and observe
failure; then run focused XLSX/equity tests with `.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp C:\Temp\mrs3-p52-p6 tests/test_performance_v2_selection_review.py tests/test_panel_performance_v2.py`; remove temp files, run the broader relevant suite, and run `git diff --check`.

#### Task 7 - Measured filters 11-27 branch and performance

**Exact file group:** exact filter implementations discovered by Task 0,
config loader, exporters/importers, and performance/limit tests.

Measure readiness/cache, evaluation, XLSX build, publication, and import
independently on cold/warm representative fixtures. Measure disabled 11-27
overhead by exact runtime ID. If negligible, collapse controls in `<details>`
with request/hash fields unchanged. If material, implement only the separately
approved typed safe-disable branch and old-snapshot compatibility. Add boundary
tests for 20 MiB/256 ZIP/100 MiB/100k rows and configured workers. Apply only
measured optimizations; a material optimization or safe-disable branch gets
its own reviewed commit.

**TDD/verification:** `.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp C:\Temp\mrs3-p52-p7 tests/test_performance_v2_store.py tests/test_performance_v2_selection_review.py tests/test_panel_performance_v2.py` plus measured repeat runs; remove C: TEMP and run `git diff --check`.

#### Task 8 - Acceptance, independent review, and documentation

**Exact file group:** the active P5.2 spec, this plan, ADR-0068,
`progress.md`, `PRD.md` navigation, and README only if a verified public
launch changes.

Run the full acceptance matrix, focused and proportionate broader tests using
only `.venv\\Scripts\\python.exe` and cleaned C: TEMP/TMP, static checks,
`git diff --check`, and link checks. Include v11 schema compatibility,
pre-migration NULL, hash-domain, publication role/origin, reader-policy,
operation-key, duplicate upload, one-lock/one-transaction timestamp, and
prune reachability evidence. Use a compact ASCII review packet and require
`CODE_REVIEW_PASS`; re-review confirmed fixes. Update the spec first, then
the new ADR, `progress.md`, PRD navigation, and README only for verified
public behavior. Ask the read-only `plan_status_maintainer` to update plan
status from root evidence. Each independently scoped slice has its own
conventional commit; migration, material safe-disable, and optimization are
separate conditional commits. Do not create a blanket one-commit-per-task
contradiction.

**TDD/verification:** run focused and proportionate broader suites only with
`.venv\Scripts\python.exe -m pytest` and a unique cleaned C: TEMP/TMP; run
`git diff --check`, link checks, and static checks before the review packet.

### P5.2 acceptance evidence

The evidence ledger covers single/all-pairs, cohort ON/OFF, exact status and
comment matrix, rank/analog clear, hard-source lifecycle, Top N and all
colors, all workbook variants, unchanged/edited/comment-only imports,
prebuild-before-lock, atomic rollback, concurrency and stale conflict,
operation-key states, ordinary/overlay latest runs, RETEST tags,
`selection_contract_version`, schema path compatibility, limits, worker
errors, filters 11-27 request hashes, Card9 reachability/maintenance, and all
publication boundaries. It also covers the additive v11 header/mapping/import
schema, role/partition uniqueness, nullable pre-migration assertion, exact
hash domains and decision-group inputs, origin-marker invariant, reader
policies, duplicate-upload idempotency, one-lock/one-transaction timestamp
ordering, global TIMESTAMPTZ max, full-pair deletion, Rejected retirement,
partial expiry, and prune reachability. No completion claim is made from
source metrics or an executor summary alone.

## External P5.2 planning-review prompt

Use this single prompt for an independent read-only review. Do not edit files
or implement code.

```text
Review the current P5.2 planning package:

- Spec: docs/specs/2026-10-09-performance-v2-all-pairs-filtering-and-rejection.md
- Plan: docs/superpowers/plans/2026-10-09-performance-v2-all-pairs-filtering-and-rejection.md
- ADR: docs/decisions/0068-performance-v2-selection-publication-v11.md

Recheck the active P5.2 contract and the compact P5.1 R001-R010 ledger against
current repository code and focused tests. Verify that all-pairs partitions
are independent, the old single-pair request/hash remains compatible, and
Only Finalist & Reserved admits exact raw MANUAL FINALIST/RESERVE only. With
the toggle off, every stored prior status is admitted, including NULL, exact
empty, whitespace, legacy, and unknown historical values; P0 must verify
whether constraints permit them rather than silently disallowing them.

Verify actual hard-filter transitions set REJECTED, clear rank and Analog Of
ID, preserve Comment, and append no degradation marker for SQL NULL or the
exact empty string. FINALIST uses Degraded Finalist, RESERVE uses Degraded
Reserved, and every other nonempty prior status is quoted as Degraded
"<prior status>". Existing effective REJECTED is a universal no-op. Verify
DROP remains a separate force-source lifecycle and STALLED is not direct
rejection.

Verify Top N membership is same-run and checkbox-gated with no reranking; exact
row-fill palette/provenance and first-exclusion precedence; and the workbook
boundary displays STALLED for canonical state STALLED while canonical rank,
codec vocabulary, caches, snapshots, runtime filtering, and ranking remain
RESERVED-compatible. Verify visible STALLED or Auto Status RESERVE alone never
proves a gray fill.

Verify combined XLSX/export/button parity, prebuild-before-lock, atomic import,
unchanged/edited/comment-only rows, stale zero-write behavior, operation-key
retry states, ordinary/overlay latest-run protection, Card9 policy, current
resource limits, configured workers, filters 11-27 branch, and the complete
schema read/write inventory including ADR-0064/0065. Fail closed on any
unverified mutation source of truth, hard-filter allowlist, provenance path,
comment overflow rule, schema need, or performance budget. Confirm TDD, C:
TEMP cleanup, independent review, and documentation/commit gates.

Also verify the mandatory additive v11 architecture: the
`selection_publications` header, `selection_publication_runs` SOURCE/OVERLAY
mapping with pair/side uniqueness, `selection_aggregate_imports`, and only
the plain nullable `aggregate_import_id` plus unique index on
`selection_review_imports`; `selection_runs.workbook_sha256` and
`selection_review_imports.workbook_sha256` remain NOT NULL (the latter UNIQUE);
overlay and child keys are domain-separated; exact export/upload hashes are
new-header fields only; no origin column or other nullability relaxation
exists. Verify
the exact ordered `decision_group_id` inputs and exclusions, automatic origin
iff OVERLAY mapping, strict/Card6/control/stage/Panel/prune reader policies,
one-lock/one-transaction timestamp ordering, exact operation-key/duplicate
upload semantics, and the pre-migration aggregate-import NULL assertion.

Verify the only stage-borrow rule: an `AUTOMATIC_REJECTION_OVERLAY` with empty
stages may use only the SOURCE mapped in the same publication/partition, and a
missing SOURCE expires stage-dependent lookup/re-export. Verify the global
latest-raw review helper includes manual aggregate and correctly classified
automatic reviews with provenance; ADR-0064/0065 reconciled state calls
`effective_selection_decisions`; Portfolio keeps review provenance but obtains
metrics/regime/stages from eligible source readers. Verify all workbook row
lookups are artifact-scoped and do not disclose cross-artifact key existence.

Check that Advisor bridge unavailability and user-authorized root best-effort/
self revision are recorded, and do not treat this review as implementation
authorization.

Return exactly PLAN_APPROVED or PLAN_REVISE. For PLAN_REVISE, return a ledger
with stable ID, severity (BLOCKER/HIGH/MEDIUM/LOW), exact spec/plan section,
repository evidence, why it matters, and the required corrective action.
```
