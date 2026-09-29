# Heavy database process optimization

**Status:** Active implementation contract; user authorized on 2026-09-28.
**Baseline:** freshly fetched `origin/main` at `8f59c2c`.
**Evidence:** [audit](../reports/2026-09-28-heavy-database-processes-audit.md),
[implementation plan](../superpowers/plans/2026-09-28-heavy-database-optimization.md).
**Plan review:** R5 `PLAN_APPROVED`, independent Claude Opus 5/high.
**Baseline check:** 221 passed, 5 skipped, 8 warnings in 164.77s;
Panel Performance v2, input, HTML, optimizer and prune suites.

The audit's reproductions and measurements were taken at historical `de7fea9`.
Its percentages are unimplemented expectations, not delivered gains at `8f59c2c`.
Each task re-confirms its defect or repeated-work mechanism on this branch
before changing it, using the failing regression or a bounded baseline probe.

## Goal and scope

Repair the reproduced PerformanceDB correctness defects and remove verified
repeated preparation, per-row SQL overhead and unbounded parent allocations in
Source import/merge, materialization, fresh analysis and PerformanceDB modules.
Use existing DuckDB bulk SQL and bounded workers rather than new services,
dependencies, persistent caches or connection-pool abstractions.

Existing feature specifications continue to govern metrics, admission,
selection and publication. This specification supplements their implementation
and defines the narrow correctness changes identified by the audit.

## Non-goals

No live tester run, trading admission, mutation of user databases, HTML deletion,
new schema version or change to the on-disk schema contract, or heuristic
reduction of the candidate/structure universe.
Source final compacted-payload proof and legacy empty-import deletion evidence
remain deferred by the user. Historical standalone v3/v4 importers are unchanged
unless separately specified and verified on their actual report path.

## Inputs and outputs

Inputs remain the existing immutable HTML/inbox snapshots, Source fragments,
materialized measurement rows, typed Performance facts and review histories.
Outputs retain their existing schemas, exact metrics, canonical identities,
rejection reasons, ranking and publication contracts, except for these repairs:

1. A rebuilt malformed optimizer prepared payload is actually persisted even
   when its version/source digest/availability metadata already match. Correct
   cache reuse remains cheap; strict validation and source rechecks remain.
2. Prune counts and deletes optimizer prepared children before their results.
   Its sequential autocommit, checkpoint, backup, restore and dry-run remain.
3. XLSX `Trades` uses cached completed round trips. The raw report trade count
   is retained in the DB. An unavailable cache exports a blank/unavailable
   `Trades` cell, never a silent zero or the raw count. Test this cache-miss path.
4. Out-of-order action rows in the actual HTML cannot be hidden by parser
   sorting. The offending report is rejected with `ACTIONS_OUT_OF_ORDER` and
   none of its rows are published. Other admitted reports follow existing
   mode-specific policies; no new all-inbox-valid rule is introduced. Test
   valid siblings under a mode that admits them. Equal timestamps retain
   stable source order.
5. Materializer progress reports successful completions while other work is
   running. Failure stops new submissions; Windows cleanup waits for running
   readers to close files.

## Updated Performance import boundary

Preserve the changes fetched in commits `128fccb`, `a7efa97` and `8f59c2c`, as
specified by [CHECK & RETEST](2026-09-03-performance-v2-retest-workflow.md):

- Explicit first Panel import atomically initializes a missing canonical
  `strategy_performance.duckdb` using the existing writer lock, same-directory
  unique staging file, schema validation, and no-clobber hard-link publication.
  A concurrent FileExists result preserves the existing target and follows the
  normal existing-target validation path; it never overwrites that file.
  Existing empty, corrupt or foreign DBs are not reinitialized; redirected
  targets remain protected. Retain the concurrent-target no-clobber regression.
  Migration of already-supported older schemas under the existing importer
  writer lock is preserved; it is not introduction of a new schema version.
  Retain the existing importer/store supported-schema migration regressions.
- Ordinary metadata `SINGLE_MODE` trusts strategy files below repository
  `Output`. It rejects redirected roots and outside-root, symlink/reparse,
  non-regular-file or invalid-hash artifacts. Retain the non-regular artifact
  regression and existing rejection mapping. Tester/configured strategy roots
  are not extra roots.
- Moving parsing/preparation outside the writer interval must recheck current
  IDs, source revisions, typed dedup, replacement state and snapshot identity
  after obtaining the writer lock. Publish the admitted ADD/REPLACE batch in
  one transaction, never incremental parser/chunk commits. Preserve existing
  mode-specific rejection/skip policies, including independently stale frozen
  members whose valid siblings may still commit.

## PerformanceDB import tail

[Tail investigation](../reports/2026-09-28-performance-db-import-tail.md)
traces the stages after the final parsed report. Existing records have no stage
timings, so neither production latency nor a delivered speed gain is inferred.
Window/equity-cache calculation is separate from this import.

T13a batches replacement child deletion after all admission decisions are
resolved in the existing publication transaction. Derive distinct result IDs
from actual `REPLACE` decisions and issue one set-based DELETE per child table:
`strategy_actions`, `strategy_equity`, `window_metrics`,
`equity_quality_metrics`, `optimizer_prepared_inputs`. Empty sets issue no
child DELETE. A singleton keeps the scalar equality predicate to preserve the
native indexed plan; larger sets use the existing bound-list SQL pattern.
Keep children of `ADD`, `SKIPPED`, `REJECTED`, independently stale
frozen members and unrelated results unchanged. Preserve result/current IDs,
mandatory child-count readback, prepared source identities, file-ledger priority,
retest tags, failure reports and whole-admitted-batch rollback.

Acceptance includes a failing-before statement-count regression, exact child
and cache preservation for non-admitted siblings, existing rollback and strict
prepared-readback checks, upstream bootstrap/Output-boundary regressions and a
bounded synthetic baseline/comparison. Statement reduction is distinct from
elapsed speed; no production percentage is promised.

T13a is a narrow first slice of IMP-02. Remaining append metadata,
preparation lifetime and writer-scope work retains its separate acceptance.

T13b reduces append overhead inside one Performance v2 publication. Keep one
publication-local cache of the ordered target columns per used action/equity
table, keyed only after the existing constrained metadata query and full
length/set validation succeeds. Every subsequent frame must still pass the
same column validation. A separate publication on the same connection starts
with an empty cache; standalone `_append_rows` calls without it retain their
per-call query and validation. An empty batch returns before constructing a
DataFrame, querying metadata or issuing SQL. Do not add global or connection
state, schema, dependency or public API.

The existing 20,000-row append cap applies inside each action/equity row loop,
including a single oversized report. Read the cap at call time; flush and
clear after each row reaches it, and flush the final remainder only when
nonempty. Preserve the current transaction, source order, report-local indices,
result identities, exact Decimal ROUND_HALF_UP scale 12, all row values/types/
nulls, counts, Phase 8 prepared digest, file ledger, ADD/REPLACE admission,
partial rejection, retest tags and rollback. This bounds writer buffers only;
parsed reports remain retained and broader writer-scope memory work remains
open. Before moving the flush, enumerate every buffer read/write and stop if a
complete-report buffer dependency exists. Inventory test wrappers/spies and
preserve their arguments and injected failure points.

Acceptance includes actual action/equity schema and one-frame versus split-
frame dtype/readback preflight, especially all-null and mixed-null Decimal/
order-ID batches; add no dtype conversion unless a concrete difference is
proved. Check one metadata query per used table per publication, zero for an
unused table, a new query on a second publication, and per-call queries for
standalone helper use. Test batch lengths at 1, cap-1, cap and cap+1, including
one oversized report and zero empty-remainder SQL. Compare DuckDB readback by
result_id with action_index/sample_index, not DataFrame index; preserve exact
types, nulls, Decimals, counts and digests. Inject failure after an earlier
capped append and prove the whole publication rolls back. T13b-R2 received
independent Opus 5/high `PLAN_APPROVED`; implementation stays limited to the
existing importer and its tests.

T13c batches only the post-commit `successful_replacements` current-result
readback. Preserve the existing `REPLACE`/imported guard, strict manifest/parsed
zip, failed-name exclusion, `report is None` exclusion and missing replacement
ID exclusion verbatim; do not add a new admitted-only filter. Retain manifest
order and duplicate candidate output entries, even when names map to one
strategy ID. Omit absent strategy rows and NULL current IDs. Preserve the
exact expected-old-result fallback. All reads stay on the existing connection
under the writer lock after `_publish` commits; do not alter publication,
failure reports, rollback, Phase 8, file ledger or RETEST tags.

For zero unique candidate IDs issue no readback SQL. For one, keep the existing
scalar indexed query. For multiple IDs, read unique IDs in ordered chunks of
at most 1,024 with one bound DuckDB `BIGINT[]`/`UNNEST` query per chunk, then
construct all output records in one pass over the original candidates. Thus
409 unique replacements take one read instead of 409, while larger requests
take `ceil(unique_ids / 1024)` reads. A strict zip length mismatch may now
raise before any readback SQL; its exception and post-commit position stay the
same. Do not change the public request or schema.

Acceptance: failing-before query-count regression, exact output parity for
zero/one/multiple/chunk-boundary candidates, manifest order despite reversed
DuckDB rows, missing/NULL current IDs, expected-old fallback, repeated IDs,
filtered entries and strict mismatch. Run importer and related bootstrap,
Output-boundary, migration, prepared-readback, rollback and RETEST tests.
Benchmark the readback stage only on offline query-shaped and, where feasible,
initialized-schema DuckDB fixtures with dense/scattered IDs and 100k/1m
strategy rows; require identical output and a lower 409-ID batch median.
Record absolute stage delta and statement reduction without attributing the
unmeasured production tail to this query. T13c-D2 received independent Opus
5/high `PLAN_APPROVED`.

T13d may batch only the in-transaction `strategies.updated_at_utc` updates for
admitted `REPLACE` results. Collect each strategy ID at the existing update
point after its `strategy_results` update succeeds, then apply the same shared
publication timestamp after the report loop and before later publication work.
Zero admitted replacements issue no update; one retains the scalar indexed
update; multiple use ordered, bound `BIGINT[]`/`UNNEST` chunks of at most 1,024.
Only successfully admitted replacements may be touched. `ADD` keeps its
immediate `current_result_id` and timestamp update, including in a mixed
ADD/REPLACE publication. Preserve the single transaction and whole-publication
rollback, existing result IDs, wide result metadata, child data and counts,
Phase 8 preparation, file ledger, RETEST handling and post-commit readback.
No schema or public API changes. Acceptance requires exact timestamp and
untouched-row parity, zero/singleton/multiple/chunk-boundary SQL counts, and
rollback after an injected batch failure. Compare the timestamp-update stage
on temporary initialized-schema DuckDB fixtures at 100k and 1m strategy rows;
report absolute time and query-count changes without claiming an unmeasured
whole-import or production-tail speedup. If the bounded batch does not improve
the representative 409-ID stage, record measured/no-change and retain the
scalar implementation.

T13e records diagnostic timings for the unresolved interval after
`PUBLISHING N/N`, following [ADR-0048](../decisions/0048-performance-v2-import-phase-evidence.md).
Reuse `PerformanceV2ImportResult.phases`; add optional diagnostic `phases` to
the existing v2 audit and `evidence.phase_seconds` to the existing terminal
Panel job. The audit retains `schema_version=2` and all previous fields. Use
one `perf_counter` clock, six-decimal finite seconds, at most 15 keys and
less than 4 KiB per object. Absent means not entered. Measure only stage
boundaries (at most 30 clock reads per successful import), never each report.
Record admission, row publication, child readback, Phase 8, finalization,
commit, optional failure artifacts, optional post-commit replacement readback,
connection close and staging cleanup as disjoint components. The total starts
after the existing PUBLISHING callback and ends after staging cleanup; its
unaccounted residual excludes the total itself. Audit write and lock release
may appear only in the returned result/terminal evidence, since the audit
cannot contain its own write duration. Panel readback includes connection
close. Terminal journal sync is INFO-only operational timing; INFO logging
must be enabled and checked before a diagnostic operator run.

Keep importer callback arguments and event sequence unchanged. The Panel
adapter may carry timings in its existing `READBACK_VERIFIED` object callback,
but the worker must hold that evidence privately until its existing terminal
update so status polls cannot create an intermediate journal save. Add no
database query, file, transaction, endpoint or journal write. Clock and timing
serialization failures must not mask the original result or exception. Test
ADD/REPLACE and failure paths, phase absence, audit old-key parity, rollback,
unchanged SQL/callback/journal replacement counts and bounded overhead on a
warmed fixture. The future real run supplies attribution; this change does
not itself accelerate import or justify a whole-import speed claim.

OPT-01 may first remove an unused digest computation verified by a source-digest
call-count regression and byte-identical available/unavailable outputs. This
does not reuse digests across source objects, snapshots, revisions or final IDs.

## Terminal PerformanceDB job journal publication
The terminal worker callback and status poll may skip a full journal rewrite
only when their complete normalized public/runtime payload is unchanged and
the registry has no unsaved mutation. First changed terminal publication,
changed payloads and retries after failed saves remain durable. Default sync
callers retain always-save behavior. Keep state validation, object identity,
public redaction, private recovery paths, journal schema, full serialization,
fsync and atomic replacement.

If journal load filters invalid saved records, the in-memory registry differs
from disk and starts dirty. Its next otherwise-identical opt-in sync must
persist the filtered registry; a clean valid reload may skip. This preserves
the existing normalization-on-next-save behavior without an extra read per
poll or a new journal format.

The registry lock covers validation, field updates, candidate equality, dirty
state and persistence. Actual volatile changes mark the whole journal dirty;
identical accepted, invalid or stale volatile updates do not. Dirty state
clears only after successful atomic replacement, after temporary=None.
Failed sync preserves legacy in-memory changes and remains dirty for retry.
Candidate JSON normalization precedes comparison; equality includes the complete
post-normalization key set and key presence/absence. No outer-snapshot equality.
Absent evidence removes it and saves once; absent inbox/runtime retains the
stored values and may skip only when the complete candidate is equal.

Ordinary public import consumes tester verification under the registry lock
before the worker starts. Terminal callbacks do not change tester verification.
Remove the dead callback without replacement: current submit stores fingerprint
and resource keys, never request; even legacy request records reach an invalid
empty-status sync before any mutation or save. A legacy true marker therefore
stays true at terminal completion, matching baseline. Do not add request
persistence, resource-link lookup or a runtime-only API.

Mutation audit: panel_jobs submit/transition/discard_queued/sync/recovery/
runtime/log updates are locked and call _save; volatile_sync is locked and
marks actual changes. The only external direct writers are portfolio startup
migration job-copy installation and restoration, plus terminal snapshot
descriptor updates. Startup migration holds the registry lock and reaches
_save/recovery; disk-space exits precede mutation. Real save failures retain
dirty state; other restore paths save restored memory. Snapshot descriptor
mutations are locked and immediately saved. An unclassified writer or exit
blocks this slice and is escalated to root, without redesigning portfolio code.
Restart recovery must save its projected states before an identical opt-in poll
may skip; invalid runtime reservations must fail before changing the job.
Volatile callers must not retain or mutate nested payloads after handing them
to the shallow-copy hot path.

Acceptance: failing-before focused registry/controller tests, on-disk reload,
three exact normalized terminal polls causing zero journal os.replace calls,
changed payload causing one, current producer linkage without stored request,
pre-worker verification consumption and zero callback tester mutations for
current/legacy records, dirty retry after
real replacement failure and fail-before-entry wrapper, legacy default sync,
actual/identical volatile updates, bounded two-thread lock exclusion, no leaked
captured temporary files. Synthetic private 109-job/~83 MiB journal benchmark
separates first-save cost from poll savings. Existing Panel registry,
Performance v2/retest and portfolio suites run once; full project suite remains
the final core-integration gate. Live cleanup, first-save clone optimization,
new storage/schema/cache/dependency/background writer are outside this slice.

J4 received independent Opus 5/high `PLAN_APPROVED` after the R1-R11 ledger.
Source finding R12 corrects the former tester-callback premise. Its amendment
adds an immutable `c1a5e0e` characterization baseline: empty-status sync rejects
all six valid states without mutation/save; current and legacy terminal
callbacks preserve tester job/runtime/flags and cause no tester write. The
legacy invalid attempt count changes from one to zero after deletion. Stop if
the baseline shows any stored side effect or uncaught exception. This targeted
baseline is additional to focused TDD and the single broader contour.
Final deletion inspection must confirm no log/metric or later local reference
was removed. J4-R15 received independent Opus 5/high `PLAN_APPROVED` with the
complete R1-R15 ledger; the immutable baseline remains an acceptance gate.
Use the existing Panel registry/controller and focused tests; live journal
cleanup is separate.

J5 extends the existing default-off registry option to every
`strategies.performance.v2.import` status snapshot, including RUNNING and the
PUBLISHING phase. The first normalized change to authoritative state, phase,
progress, error, evidence, result, inbox readiness or controller runtime stays
durable. A poll skips only if its complete post-normalization candidate equals
the latest locked job and the global journal dirty flag is false; a pending
volatile change in another job forces publication. Other job kinds retain
their save rules. No registry machinery, schema, API, worker throttle, UI or
live cleanup is added.

Preserve absent-field semantics: missing error becomes None and missing
evidence removes the key; absent phase, progress and result retain saved values;
absent inbox readiness retains True; absent runtime retains its stored value.
When inbox_path is supplied, the controller merges it into the existing runtime
copy before equality. RUNNING is a registry state; PUBLISHING is a phase.
Callbacks do not change tester flags; public pre-worker verification consumption
is unchanged.

J5 acceptance uses the real controller status path: a RUNNING/PUBLISHING N/N
first change makes one journal replacement, three exact repeats make zero, and
reload restores complete state/phase/progress/error/evidence/result/runtime.
State-only, phase-only, progress, inbox/runtime and absent-field normalization
changes each save once; pending volatile changes in another job save the whole
registry. Other job kinds still save identical polls. Check no temporary files
remain after success/failure. J5-R2 received independent Opus 5/high
`PLAN_APPROVED`; J4 received `CODE_REVIEW_PASS` and committed as `b31b37e`.

## ANA-01a: Source v6 analysis publication transaction

`source_v6_analysis_fresh._publish` owns a fresh staging DuckDB connection. Its
existing table DDL remains before one explicit transaction covering manifest,
scope-run and result-table DML. Commit follows the last insert; checkpoint,
writer close, read-only identity/count validation and atomic replacement follow
commit. On a DML failure, rollback is attempted without masking the original
exception, the old target remains byte-identical, and staging artifacts are
removed. Cleanup may be extended only for an observed staging-derived sidecar.

Stored canonical JSON strings, scope SHA digests, manifest rows, table counts
and `rowid` ordering must match the pre-change fixture exactly. The successful
DuckDB file's physical bytes need not match. Zero-result/all-empty/one-row
cases are tested only where the current `_publish` contract accepts them.
No second full result/JSON list, registered-relation writer, schema change or
live database operation is in this slice; bounded bulk relation writing remains
a separately measured ANA-01 follow-up if needed.

Before code changes, freeze one representative approximately 1,000-row input,
three `_publish` timings and a deterministic semantic dump outside the worktree.
Repeat exactly three timings with the same input after the change, within a
shared 90-second benchmark cap. Call the publication gain material only when
the median falls by both at least 20% and 10 ms; otherwise label it unmeasured
or retain the transaction solely for correctness, or revert and plan the bulk
relation slice. Do not infer a whole-analysis gain from this stage benchmark.
Focused RED/GREEN, failure/residue checks and relevant Source v6 tests precede
independent review. T6a-D2 received independent Opus 5/high `PLAN_APPROVED`.

## DEC-01: Performance v2 selection-history replay

`effective_selection_decisions` currently reads ordered `selection_runs` once,
then issues three reads per run. It must preserve the ordered Python replay
while replacing those reads with at most four SQL statements independent of
history length: runs, globally latest user reviews, newest review membership,
and selection results. All statements run on the caller's same DuckDB
connection; the finalist-retest callers can pass a connection inside an open
transaction. Do not create a child cursor or transaction, or alter the public
signature, schema, indexes or global-review lookup. Consume each earlier query
before the next; consume the final query in fixed `fetchmany` batches without
another SQL call during replay. There is no formal peak-memory claim.

Replay is driven by all runs in `created_at_utc ASC, selection_run_id ASC`
order, including zero-result runs. A malformed/non-mapping request is ordinary;
each ordinary run clears its `(symbol, side)` state. `RETEST_COHORT` and
`CURRENT_EFFECTIVE` are overlays: no imported review is dormant, an imported
zero-row review is active but changes nothing, and a reviewed overlay applies
only its newest review's strategy IDs. Rank review imports by
`imported_at_utc DESC, review_import_id DESC` before joining their rows.
Effective status/rank still come from the globally latest accepted review for
the strategy; otherwise only `prior_rejected` contributes `REJECTED`. Decision
lineage is the run being replayed. Preserve pair/side isolation and final
flattening. A symbol filter limits runs and results but does not scope the
global latest-review lookup. Use static filtered/unfiltered SQL variants and a
bound scalar symbol, never a history-sized ID parameter list. Project only
consumed columns; auto status/rank and per-run review status/rank are not used
by the current replay.

The result query must use the same run-order prefix and `symbol = ?` predicate
as the run query, with `selection_results.rowid ASC` for within-run order.
Before implementation, compare the old unordered per-run scan against this
ordering on two equal-time runs with deliberately out-of-ID-order rows: at
least five repeats each under DuckDB threads 1 and 4. Stop and re-plan if old
order varies or differs from the proposed order. The run-driven merge must
pass multiple consecutive empty runs, reject backward result order and ignore
newly visible results for unknown runs as the old frozen-run loop did.

Acceptance: explicit ordered output, rank and lineage equality for 2- and
102-run fixtures (including empty ordinary/reviewed runs, both overlays,
malformed requests, timestamp ties, prior rejection and pair/side groups);
the old ordinary baseline of 8/308 statements is recorded separately and
retained tests require at most four statements for either size and symbol
scope. A caller's uncommitted run/review remains visible without an implicit
commit; injected read failure changes no history tables. A fixed synthetic
before/after timing reports only the observed stage gain. The audit's
50–85% replay and 10–40% history-heavy consumer ranges are non-additive
expectations, not delivered gains or acceptance thresholds. T7-D3 received
independent Opus 5/high `PLAN_APPROVED`.

## OPT-01b: builder-local source document reuse

`build_prepared_input` currently materializes the same `OptimizerSourceInput`
document four times on an AVAILABLE path: once for its digest, once for
availability, once for prepared rows and once for cycle reconstruction.
Reuse the builder's prepared-row document for availability and cycle records,
leaving the existing `source_digest(source)` call and its position unchanged.
The builder path should call `to_document()` twice when AVAILABLE or
UNAVAILABLE. In the Phase 8 importer, which independently computes a digest,
this reduces the corresponding full source serializations from five to three;
both digest computations and the writer's source rechecks remain intact.

Keep `prepared_availability(source)` and `_cycle_records(source)` direct callers
working as before. The latter has a portfolio-input caller, so any optional
document argument must be keyword-only and default to the original conversion.
Do not change importer runtime, preparation schema, canonical JSON/digest,
reasons, byte limit, row order, reconstruction, transaction or trust rules.

Acceptance: a failing-before conversion-count regression for AVAILABLE and
UNAVAILABLE builder paths, frozen digest and exact prepared JSON/reason parity,
direct helper and portfolio caller coverage, and relevant optimizer/import/
portfolio tests. Record a fixed synthetic preparation-only before/after timing
as stage evidence, with no whole-import percentage claim. OPT-01b-D3 received
independent Opus 5/high `PLAN_APPROVED`.

## CALC-01a: one flat timeline per cold selection result

Within `_selection_window_job`, obtain missing windows from the existing
positional cache lookup and load their source once. Compute `_flat_samples` once
from that loaded source and pass the same immutable tuple to each missing
window calculation. A fully cached result must neither load the source nor
construct the timeline. Keep the cached `zip(cached, windows)` mapping, result
order, write selection, equity path, read-only workers and writer transaction
unchanged; do not reuse the tuple across jobs or source revisions.

`_calculate` gains only a private keyword-only optional `flat_samples` value.
`None` retains its current scalar behavior; an explicitly supplied empty tuple
is authoritative and must not trigger another `_flat_samples` call. Preserve
the initial out-of-range branch and every later boundary search, filter,
unavailable reason, W0 exclusion, tie order, Decimal operation and output
type. The flat tuple is read-only and not retained by a returned metric.
`_flat_samples` is a total, side-effect-free read over typed action/equity
tuples, including empty equity and degenerate timelines. Eager preparation
inside the missing-window branch may do one otherwise unnecessary pass when
every missing window short-circuits as `OUT_OF_RANGE`; it must never change
the returned rows, raise, or mutate source state. Cover all-out-of-range and
mixed out-of-range/available jobs explicitly.
Scalar, pair and portfolio-input callers keep their current path. No bisect,
sorting, timestamp-index arrays, schema or metric-version change is included.

Acceptance: RED/GREEN tests for explicit empty/nonempty flat tuples and exact
scalar metric parity, shared tuple identity/content across forward/reverse
multi-window evaluation, mixed cached/missing positions, and a fully cached
zero-load/zero-calculation path. Run focused windows/selection and relevant
portfolio/equity tests, then a fixed synthetic preparation-only before/after
timing with exact metric identity. A non-improving timing records measured/no
change rather than a whole-selection speed claim. CALC-02 bulk publication
remains separate; direct callers can pass more workers than the Panel config
cap, so its write bound must be settled in a separately approved plan.

## CALC-02: bounded bulk publication of selection windows

The selection-cache writer keeps one transaction per existing batch and its
current order: explicit source rechecks for results without equity publication,
window-metric writes, checked equity publication, then commit. The checked
equity path may perform a later source recheck after window SQL; any failure
must roll back both window and equity changes from that batch. Earlier
committed batches remain committed.

Replace only the selection writer's per-metric `_persist` loop with a private
`_persist_many` bulk upsert on its existing connection. Standalone `_persist`
callers and the scalar SQL remain unchanged. The bulk helper must not open or close a
connection or control the transaction. It uses the same 20 metric columns plus
`calculated_at_utc`, primary key, and conflict-update columns as `_persist`.
Prepare one UTC timestamp per input metric in the received sequence before
any SQL. Preserve every original payload and its timestamp in that sequence.
Do not coalesce duplicates: an invalid earlier duplicate must still reach
DuckDB conversion and cannot be hidden by a later valid row. Each SQL group
contains distinct conflict keys; flush the current group before adding a key
already present in it, or when its size reaches 896. Repeated keys therefore
execute in later statements, where unchanged conflict updates make the last
valid payload/timestamp win. Native DuckDB retains Decimal/INTEGER/nullability
validation; add no duplicate-specific casts or validation replica. The existing
nonfinite Decimal rejection remains before SQL. Timestamp acquisition moves
before database writes. For a fixed received sequence inside the caller's
transaction, final stored rows and failure outcomes must match scalar
persistence; worker completion order itself need not be deterministic.

Use fixed groups of at most 896 rows: 21 bound values per row and at
most 18,816 parameters per statement. This bound is independent of worker
count, including direct callers that pass more than the Performance v2 config
cap of 64. The sole Panel worker-count setting remains `duckdb_import.workers`
from `config.local.json`. Its example value is 16; if the section is absent,
the existing `DuckDBImportSettings` fallback is 4. CALC-02 adds no separate
worker or SQL-batch setting. Keep the schema, metrics version, cache selection,
worker scheduling, callbacks, telemetry, dependencies, and public API unchanged.

Acceptance requires actual-schema DuckDB 1.5.5 preflight for insert/update,
conflicts, mixed-scale `DECIMAL` values, nonfinite-value rejection,
duplicate-key handling and the maximum statement width; RED/GREEN
tests for scalar-equivalent values, source-ordered duplicate groups and invalid
earlier duplicates, empty/896/897-row SQL counts, real-transaction rollback after a late
equity check and after a second SQL-chunk failure. Nonfinite `Decimal` values
are invalid for the actual `DECIMAL(38,12)` columns and typed reads; scalar
and bulk paths must reject them equivalently. Run directly affected, related,
and full `.venv` tests. A seven-run throwaway-database stage benchmark must
show identical final rows and nonoverlapping scalar/bulk timing ranges before
claiming a speedup for this write stage; it cannot establish whole-selection
speedup.

## WIN-01: source-only reuse within one standalone pair

Goal: remove one repeated source decode and its three SQL reads when both
distinct windows of a public pair are cold. Inputs, output metrics and stored
cache rows remain the existing public window contracts. Only the complete
window-independent `_load_source(connection, result_id)` tuple is shared:
report boundaries, immutable ordered actions and immutable ordered equity.
The loader's three result-ID queries and ordering remain unchanged.

The first cache miss establishes one source snapshot local to that public
pair call. Later misses in the same call use it; no source survives across
calls, connections, jobs, results or source revisions. A source commit after
the first miss need not become visible to B. Stop and revise if a supported
contract requires such mid-call visibility or the loader gains clipping,
window/version inputs or context. Add no locks or keyed/global memo.

Move the existing scalar body to one private authoritative helper, keeping
its validation, timestamp/version normalization, cache lookup, scalar
calculation, scalar persistence and post-write readback order. The public
scalar keeps its signature/defaults and invokes the helper without shared
state. The public pair keeps shape validation first and uses a local zero-
or-one-element list: finish A including persistence/readback before validating
B. No transaction is added; A survives a B error under autocommit, while an
explicit caller transaction may roll it back.

Share source loading only. Invoke `_calculate(..., *source)` exactly as the
scalar path does, without `flat_samples`. Its early `OUT_OF_RANGE` branch
returns an unavailable metric with reason `OUT_OF_RANGE` before flat
preparation; B still runs after an out-of-range A. Selection's CALC-01a flat
reuse and CALC-02 bulk persistence remain separate. Non-goals: flat/boundary
sharing, bulk pair writes, new public API/schema/version, dependencies,
settings, worker routing or caller changes.

Acceptance uses actual-schema scalar-oracle/pair-candidate comparisons of
full typed metrics and all 20 deterministic stored fields; validate UTC
calculation timestamps separately. Distinct cold pairs load source once
instead of twice: valid/valid retains two flat preparations, out-of-range/
out-of-range retains zero, and mixed valid/out-of-range retains one. Every
distinct pair retains two calculations, two scalar writes and four cache
reads. Both cached load nothing; cached/missing in either order loads once;
a duplicate cold window loads/calculates/writes once and performs three cache
reads. Retain scalar validation/error precedence, shape precedence, post-read
fallback and autocommit/explicit-transaction behavior.

Required RED/GREEN evidence includes all four cold validity combinations,
direct equality of B's consumed source to an independently loaded real
oracle, A/B action/equity tuple identity, and a real two-call source-mutation
regression. Between calls on the same connection/result, update real source
fee/equity values, delete all result cache rows without version/window filters,
prove the cache is empty and source values changed, then verify a fresh single
load, full mutated scalar parity and changed growth/fees. Rerun the public
delegation/mocking inventory before GREEN; an outside-scope dependency stops
the executor for root revision. Use existing .venv focused/related/full checks
and reconcile the collection delta against accepted CALC-02.

A temporary actual-schema 2,000-cycle benchmark compares two ordered scalar
calls with one pair after clearing only fixture cache outside timing: one
warmup, seven alternating paired samples. An untimed real forwarding probe
must show source loads 2 to 1 and logical SQL 12 to 9, with flat/calculation/
scalar write/post-readback counts unchanged and exact typed/stored parity.
Claim stage speedup only for a lower median and nonoverlapping ranges;
otherwise report work-count reduction only. Independent CODE_REVIEW_PASS
and a scoped commit are required; no live database/tester or push.


## CACHE-01: skip publication for a fully warm selection window batch

Goal: when all required windows for a result are cached and
`include_equity=False`, return an empty write set from
`_selection_window_job`. `prepare_selection_window_cache` must not open a
writer, begin a transaction or republish those seven rows. It still completes
each batch and calls `on_batch_complete(len(completed))` once. The public helper
returns `None`; its Panel callers at `panel.py:5186,5547,5613` and portfolio
input caller ignore that value. Selection status, missing IDs, availability,
readiness and candidate metrics must remain the same.

The existing fully warm branch returns before assigning `source_recheck`, so
its value is `None` for both equity modes. Today the no-equity branch opens a
writer to republish cached metrics, but the writer's `source_rechecks` loop is
empty. There is no source-revision guard or stale-row repair to remove; the
fully warm equity branch already skips the writer. A changed source revision
with unchanged window key/version remains a cache hit, as before, and this
step neither invalidates nor recomputes it. Mixed cached/missing no-equity
jobs retain their complete ordered seven-metric write set. Equity-only,
missing-window equity, version mismatch, failures and writer rollback remain
unchanged.

Consumer inventory for the observable `window_metrics.calculated_at_utc`
change: `performance_v2_windows.py:472,477,496,504` writes it;
`performance_v2_store.py:310,544` declares/validates it; `panel.py:5377`
uses `max(wm.calculated_at_utc)` in the selection candidate LRU facts token,
alongside a hash of substantive metric columns. Other production
`calculated_at_utc` hits in `performance_v2_equity_cache.py` and
`performance_v2_store.py:434,668` belong to equity facts, not window cache.
No production reader uses the window timestamp for TTL, expiry, readiness,
display or sorting. A fully warm no-op now leaves that LRU token stable instead
of changing it through redundant republishing; substantive changed rows still
change the token. No new revision read or worker setting is added.

Acceptance: first publish real fixture rows, then run a fully warm no-equity
batch with a writer-open guard on the selection module's `duckdb.connect` and
fail-fast source/calculation/persistence spies. Assert full stored rows,
including timestamps, and selection status before/after; public return is
`None`, with unchanged per-batch callback counts. Repeat with changed source
revision metadata but the same window keys to prove the existing no-recheck
contract. Record a bare read-only open/close hash control before treating a
physical DuckDB file hash as an assertion; otherwise use the hash for diagnosis
only. Compare actual-schema warm-batch timings and writer counts before/after,
without claiming whole-preview or import speedup. Independent review and a
scoped commit are required.


## CALC-01b: ordered boundary search for seven selection windows

Only `_selection_window_job` may request ordered boundary search in
`_calculate`, using the unchanged tuples returned by `_load_source`. Its
actions are ordered by `(timestamp_utc, action_index)` and equity by
`(timestamp_utc, sample_index)`; the shared flat tuple contains nondecreasing
UTC datetimes. Scalar, pair, portfolio and direct callers retain the existing
linear calculation by default because portfolio source rows have no guaranteed
order. No sorting, timestamp copy, cache/version/schema change or new worker
setting is part of this step. The sole Panel/config worker setting remains
`duckdb_import.workers`.

For ordered input, flat start selects the first timestamp `>= start` and flat
end selects the last timestamp `<= end`. An empty flat tuple and either absent
boundary retain the current `NO_FLAT_START`/`NO_FLAT_END` precedence. Equity
includes both effective bounds, so its slice uses `bisect_left` at the start
and `bisect_right` at the end. Actions exclude the effective start and include
the end, so both slice positions use `bisect_right`. The source-independent
`OUT_OF_RANGE` return still precedes flat preparation. Duplicate timestamps
retain loader secondary order and the first/last boundary choices. All
`WindowMetrics` fields, Decimal types and operations, W0 fee/PnL exclusion,
carry-in, wallet baseline, drawdown, round trips, unavailable reasons and
persisted deterministic fields must match the linear calculation exactly.

Acceptance uses a fixed actual-schema source with 4000 hourly actions, 4001
equity rows, seven distinct selection windows and seven `AVAILABLE` outcomes.
Compare complete typed metrics and stored deterministic columns for duplicate
boundaries and each unavailable reason; prove the loader's ordered contract
with shuffled physical insertion. Benchmark the seven sequential calculator
calls with one source load and one flat preparation outside timing in both
modes, using the same seed and paired samples. Exact output parity is required.
Proceed only if the predeclared robust timing criterion shows at least a 10%
median reduction; otherwise revert this optional optimization and record the
measured result. Independent review, full tests and a scoped commit remain
required. `safe_to_delete=YES` remains deferred.

## Optimization invariants

- Exact Decimal arithmetic, window-local peaks/fees, W0 exclusion, carry-in,
  open/quiet tails, unavailable reasons, witnesses and deterministic ties.
- Manifest, quarantine, source hashes, lineage and logical row ordering.
- Canonical JSON and digests without numeric/float coercion.
- Existing schema gates, one common publisher, atomic rename/publication and
  backup/restore; no partial generation, migration or Performance batch.
- Review replay preserves ordinary replacement, scoped overlay and latest user
  decisions. Global retest preserves frozen ordering and missing/duplicate errors.
- Shared action scans preserve the distinct side/open predicates of each metric.
- Preview remains read-only. Warm candidate hits avoid raw scans; legitimate
  cold candidate aggregation remains allowed. Equity preview reads cached facts.
- Connections stay within their owning process/thread. External process readers
  start after the common writer is closed; Windows failure cleanup closes readers.
- Streaming identity hashing preserves artifact names, lengths, absent markers
  and content digests. Existing preflight/execute checks and compact remain.
- Default worker count stays 16 until comparable measurements justify a change.
  Bound submitted and retained data by count and approximate bytes where useful.
- Source MRS2 PnL remains diagnostic; existing optimizer/M5 admission gates remain.

## Acceptance evidence

Each behavior change starts with a failing regression using the actual affected
boundary, followed by focused and relevant broader `.venv` tests. Bulk paths
must prove exact output equality and failure rollback; query-count tests cover
the eliminated repeated SQL, without arbitrary wall-time assertions.

Use the existing benchmark harness and offline synthetic/frozen-clone inputs.
Record commit, versions, input identity, cold/warm/startup, elapsed median, RSS,
worker count and statements. Audit percentage ranges are estimates rather than
hard gates. A full suite runs after final integration; rerun affected checks
when later edits introduce failures or change shared behavior.

Each scoped commit requires `git diff --check`, inspection of the staged diff,
independent `CODE_REVIEW_PASS` and an evidence update in `progress.md`.
Rollback uses that commit's revert and the previous atomic artifact/backup.
