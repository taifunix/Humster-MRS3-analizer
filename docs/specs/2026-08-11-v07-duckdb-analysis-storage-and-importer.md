# v0.7 DuckDB Analysis Storage and Importer

**Status:** Active / Approved for implementation

**Depends on:** [v0.7 event source packs](2026-08-10-v07-event-source-packs.md),
[event filter and shortlist](v07-event-filter-and-shortlist.md),
[ADR-0003](../decisions/0003-source-integrity-action-metrics.md)

**Implementation plan:**
[DuckDB analysis storage and importer plan](../superpowers/plans/2026-08-11-v07-duckdb-analysis-storage-and-importer.md)

## Goal

Turn the existing compact report database into one safely appendable source
DuckDB managed from the web panel. Materialize immutable, versioned analysis
surfaces from it into a separate analysis DuckDB, then store plateau runs,
candidates and cross-period lineage without repeatedly decoding raw payloads.

CSV/DuckDB overlay is not required by this specification. It is an optional,
independent feature described in
[CSV-DuckDB overlay](2026-08-11-v07-optional-csv-duckdb-overlay.md).

## Source and analysis databases

The two local databases have separate responsibilities:

- **Source DuckDB** is the single lossless HTML-report store. It accumulates
  symbols, sides, timeframes, periods, MA grids and shift steps.
- **Analysis DuckDB** stores materialized point surfaces, coverage audits,
  plateau analyses, candidates and lineage. It never owns raw HTML payloads.

Neither local path is tracked in Git. Both are configured through ignored
`config.local.json` and editable from the panel Settings page.

## Canonical source identity

A parameter point is identified by:

```text
symbol | side | timeframe | shift_bp | open_ma | close_ma
```

`shift_bp` is an integer normalized from the raw multiplier by a versioned
normalization rule and tolerance stored in source-database metadata. Raw
multiplier text is provenance, never identity, so equivalent values such as
`0.99` and `0.9900` cannot create duplicate points. An importer whose
normalization contract differs from the database metadata must fail before
writing; changing the contract requires a schema migration into a new source
database.

A source report is identified by:

```text
canonical_point_key | report_period_start | report_period_end
```

The time-grid content hash remains integrity evidence, not report identity.
Different periods coexist. Different shift steps for the same symbol create
different canonical points.

## Append and replacement semantics

Active report hashes and historical audit hashes have separate contracts.
There is exactly one active payload per canonical report identity. An active
payload SHA-256 is unique among active reports; replacement history may contain
the same hash more than once.

- Incoming SHA-256 equals the currently active report: `SKIPPED_IDENTICAL`.
- New point, shift, timeframe, symbol, side or period: insert.
- Same canonical point and period with a different HTML: transactionally
  replace prior report metadata and compressed payload.
- Two different incoming HTML files with the same canonical identity in one
  import job: `AMBIGUOUS_BATCH_DUPLICATE`; do not choose by worker completion
  order and do not replace that report.

Old duplicate payloads are not retained. Replacement audit records the old
and new SHA-256, import time and job ID. Published analysis surfaces remain
immutable and usable because they contain their materialized metrics and the
source hash used at publication time; replacement can prevent later raw
reproduction of an old surface, and that limitation is explicit in its
provenance state.

Reintroducing historical content is deterministic: after `A -> B`, importing
`A` again replaces the active `B` and appends the transition `B -> A`; the old
audit occurrence of hash `A` does not turn this into `SKIPPED_IDENTICAL`.

Only one source-DuckDB writer is allowed. The panel refuses a second import
while an import job is active.

## Versioned migration

The current v4 codec and compact payload layout remain the raw foundation, but
canonical replacement and normalized shift identity require a versioned schema
migration.

Migration writes a new database, copies and validates the existing v4 data,
and switches the configured path only after all checks pass. The original v4
file remains a recoverable backup until these checks succeed:

- schema version and required constraints;
- report, point and payload row counts;
- unique source hashes and canonical report keys;
- decoded payload samples;
- referential integrity;
- source-hash parity with the old database.

Migration also persists the normalization-contract version and tolerance.
Opening the database with incompatible importer settings is rejected before
preflight or write.

The importer never performs an ad-hoc in-place schema rewrite.

## Panel-managed HTML import

The production workflow is **HTML -> Source DuckDB** in the web panel. The BAT
launcher is no longer a product interface.

Daily import controls:

- native picker for the incoming HTML root;
- target source DuckDB from Settings;
- preflight showing HTML count and target database identity;
- one start button and cancellable progress display;
- counts for parsed, inserted, replaced, identical, ambiguous and quarantined
  reports;
- final manifest/checklist links and explicit success/failure state.

### Large HTML preflight progress

For a large HTML tree, Preflight snapshots and hashes every discovered file
once before it can authorize import. The panel retains that immutable snapshot
only in process memory and Start import consumes it directly; Start must not
repeat directory discovery or full HTML hashing. The panel runs Preflight in
the configured worker pool and exposes a live, non-path-leaking state with
discovered-file count, snapshotted-file count and processed bytes/total bytes.
It disables a second Preflight or Start action while that request is active.
HTML parsing runs in a `ProcessPoolExecutor` with the configured worker count;
workers return pickle-safe codec results and never open DuckDB. At most one
bounded queue of `workers * 3` parse/prepare tasks is in flight. Each completed
worker result updates panel progress. Cancellation stops scheduling remaining
HTML before publication. After canonical grouping, the one DuckDB coordinator
writes points, grids, reports, payloads and replacement audit rows with
per-table batch operations, not SQL once per report. An authorized Preflight is
reused after staging copy; each committed batch verifies its written report
metadata and payload references, while historical opaque payloads are not
globally decoded or revalidated during Start import.

Settings contain:

- source DuckDB path;
- default incoming HTML root;
- audit root;
- workers;
- transaction batch size.

The importer recursively discovers HTML below the selected root. Local paths
remain only in ignored configuration and operational logs.

## Import safety checks

- Parsing workers never write DuckDB; one coordinator owns the connection.
- Exact duplicate detection occurs before replacement.
- Incoming canonical duplicates are grouped before write decisions, so process
  completion order cannot choose a winner.
- Replacement updates report metadata and payload atomically in the unpublished
  staging database.
- A failed transaction leaves the previous canonical report readable.
- Committed batches may remain after a later job failure; repeating the same
  import safely skips identical reports and resumes the rest.
- HTML is never deleted by the importer.
- Quarantine and deletion checklists remain mandatory operational evidence.
- Start import rejects a changed preflight tree using file size and
  `mtime_ns`, and repeats that inexpensive check before staging publication.
  It does not perform another full-tree HTML or source-DuckDB hash pass.
- Running import progress identifies `PARSING`, `GROUPING`, `STAGING`,
  `WRITING` and `PUBLISHING`, with parsed/inserted/replaced/quarantined counts.

## Direct DuckDB analysis surface

Plateau analysis never runs directly on source payload tables. One panel action
may appear direct to the user, but it first materializes an immutable
`DUCKDB_DIRECT` surface into the analysis DuckDB, then runs the common plateau
engine against that surface.

A direct build consumes:

- source database identity;
- one UTC half-open period `[start, end)`;
- one side;
- selected symbols and usable timeframes;
- an immutable grid contract;
- the materializer version and point-materialization configuration hash.

The panel derives the required shift list automatically when the user has not
explicitly supplied one: it takes every observed shift that has at least one
report fully covering the requested UTC window for the selected side and
symbols. Preflight freezes that resulting list into the immutable surface
contract and still rejects incomplete MA-pair/timeframe cells. Manual shift
range controls are not required for ordinary whole-surface analysis.

Plateau/selection algorithm settings are not surface inputs. They belong only
to an `analysis_run`, so a different plateau configuration reuses the same
published point surface.

Reports must cover the whole period. Missing or conflicting required grid
cells exclude the affected timeframe. A symbol is unavailable only when no
usable timeframe remains. The panel shows exact coverage reasons before build.

Direct preflight validates the v5 schema, constraints, references, canonical
keys and persisted row/payload hashes structurally. It does not decode every
opaque report payload; full payload decoding remains an explicit integrity
diagnostic and is not an everyday panel preflight cost.

In direct mode `point_event_count = TotalTrades`. Confirmed event IDs and cycle
audits may remain diagnostic, but they do not enter plateau eligibility or
union.

## Grid contracts

Completeness is independent of storage format. Preferred evidence is a tester
batch/test-plan manifest declaring expected symbols, side, timeframes, shifts
and MA pairs.

Historical source data lacking such a manifest may use an immutable
`OBSERVED_GRID_CONTRACT`:

1. Select the required shift range and step explicitly.
2. Take the intersection of observed MA pairs over required shifts.
3. Include the maximal MA-pair set present at every required shift.
4. Record and exclude incomplete MA pairs; exclude a timeframe only when no
   complete MA pair remains.
5. State that a point absent at every shift cannot be discovered from observed
   data alone.

When more than one active report for the same canonical point covers the
requested window, direct preflight deterministically selects the narrowest
covering report (then stable start/end/report-ID tie-breakers) and records the
resolved overlap in coverage audit.

No missing cell is interpolated.

## Analysis DuckDB schema

The append-only analysis store contains at minimum:

- `surfaces`;
- `surface_sources`;
- `surface_pairs`;
- `surface_timeframes`;
- `surface_points`;
- `coverage_issues`;
- `dedup_decisions`;
- `analysis_runs`;
- `analysis_run_facts`;
- `plateaus`;
- `plateau_members`;
- `candidates`;
- `candidate_plateaus`;
- `plateau_lineage`.

Raw actions, equity, wallet series and HTML are never copied into it.

Analysis schema v2 represents one logical 2–4ORD candidate once in
`candidates` and stores every constituent plateau in the
`candidate_plateaus` junction. Upgrading a v1 analysis store is one
transaction: existing candidates retain their prior plateau link, published
surfaces and `surface_points` are unchanged, and any failure leaves the v1
store readable. Additional candidate memberships are never hidden only in
JSON.

Analysis schema v3 adds one immutable `analysis_run_facts` row per run for
unique-point, economic-eligible, event-eligible, plateau and READY-candidate
counts plus final state. These run results cannot be reconstructed by a
read-only library query from v2 after the external listing-date input is gone.
The v2 → v3 migration derives only facts already present in the analysis DB
and marks unavailable legacy eligibility counts explicitly; it never invents
them or reopens the source DB.

## Surface identity and publication

`surface_id` is a deterministic digest of build mode, period, side, selected
symbols/timeframes, source hashes, grid contract, normalization contract,
materializer version and point-materialization configuration hash.

Publication is one transaction. A failed build leaves no published surface or
partial point set. Published surfaces are immutable:

- more source data for the same period creates a child with
  `parent_surface_id`;
- a new period creates a separate surface;
- identical inputs reproduce the same digest and do not duplicate rows.

Every surface point contains its canonical key, bounded metrics,
`point_event_count`, chosen source report ID/hash and provenance state.

## Plateau runs and lineage

Each plateau calculation creates a separate `analysis_run` referencing one
surface, plateau algorithm version and algorithm configuration. Its
deterministic run identity is derived from those inputs. Recalculation does
not copy surface points, and changing only plateau settings never creates a
new surface.

Published surface metrics do not invent a symbol listing date. Before the
common eligibility pipeline runs, it resolves listing dates from an explicit
listing-date input. The canonical snapshot/hash of that input is part of the
analysis-run configuration and deterministic identity, not the surface
identity. This keeps one immutable point surface reusable while making the
history gate reproducible.

Period-specific plateau IDs are local to their run. `plateau_lineage` connects
runs using common canonical points and geometric overlap. The comparison run
is explicit; an implicit "latest" run is not selected across algorithm
variants:

- `CONTINUED`;
- `SPLIT`;
- `MERGED`;
- `NEW`;
- `DROPPED`.

Lineage never merges period metrics.

## Analysis panel and exports

The panel provides:

- source coverage preflight by symbol/timeframe;
- an explicit writable analysis-schema v4 initialization/migration action with
  visible in-place `running`, `ready` or error feedback next to the action;
- selected-by-default checkboxes for usable symbols;
- red noninteractive warnings for unavailable symbols;
- surface build progress and final unique-point counts;
- economic/event-eligible points, plateaus and READY candidates;
- a surface library with period, side, parent, source identity and analyses;
- **Refine**, **Re-run analysis**, **Compare periods** and export actions.

The analysis DuckDB is canonical. CSV and Excel are generated exports of a
specific immutable surface or analysis run.

### Sequential panel workflow

The panel presents the production path in execution order: source import,
direct surface, plateau analysis, review/export, JSON strategy generation,
test-plan validation, tester run, then DD5 post-test analysis. Earlier
manual/advanced controls remain available, but they do not replace this path.

Selecting a surface pre-fills the analysis input. A committed analysis run
pre-fills its run ID and a semantic, empty export directory. Generating JSON
uses only `READY_MRS3_STRUCTURE` candidates stored for that exact analysis run;
it reloads the immutable surface solely to validate the source points used by
each generated JSON. It never re-runs plateau selection, consumes a legacy
CSV/source-pack, or automatically starts the bot.

The panel first renders a reviewable shortlist for the selected run. Every row
shows its structure ID, pair, timeframe, common Close MA, order count and each
selected point's shift, PnL, drawdown and event/trade count. JSON generation
requires an explicit nonempty selection from that shortlist; it must not
silently emit every READY row. For every selected READY structure, generation
emits exactly the `EQUAL` and `INCOME` lot variants. Publication is atomic: a
failure leaves a prior strategy directory intact. Its generated strategy
directory becomes the default input to the subsequent Test plan and Run tests
controls. Paths whose values cannot be inferred, such as the user-owned
strategy template and listing-date file, remain explicit editable inputs.

The panel does not invent a Top-N reduction. The event-specification's exact
same-behavior redundancy filter is available only to runs with real independent
event identities; a `legacy_trades_proxy` run remains reviewable but cannot
claim that such a reduction is valid.

## Acceptance evidence

- Importer tests prove append, identical-active skip, canonical replacement,
  `A -> B -> A` replacement history, new period coexistence, new-shift
  insertion, multiplier canonicalization, incompatible normalization-contract
  rejection, ambiguous incoming duplicate rejection and safe resume.
- Migration tests prove the old database remains untouched on failure and the
  migrated database has row/hash/payload parity plus persisted normalization
  metadata.
- Direct-surface tests prove period coverage, grid completeness, timeframe
  exclusion, canonical uniqueness and `PointEventCount = TotalTrades`.
- Storage tests prove atomic publication, deterministic IDs, immutable parent
  surfaces, explicit unreproducible-raw provenance after source replacement,
  different algorithm configurations reusing the identical point set,
  repeated plateau analysis without point copying and export parity.
- Panel tests cover settings persistence, import preflight/progress, single-job
  enforcement, coverage warnings, surface statistics and stale-preflight
  rejection.
- Task 14 tests cover the explicit writable v4 initialization/migration action,
  read-only library/compare/export paths, immutable-surface analysis re-runs,
  explicit refinement parentage, separate period facts and byte-stable exports.
  Focused evidence is `104 passed`; independent Terra review approved after
  the read-only migration boundary was separated from schema initialization.
- Final verification passes `467` tests; two tests skip only because Windows
  symlink privileges are unavailable. A copied-real-data smoke imported one
  report into v4, migrated it out-of-place to a valid v5 database, append-
  imported a second report with zero quarantine, revalidated `2` reports / `2`
  points / `2` grids / `2` payloads, and published one direct point with `458`
  trading events into analysis schema v3. All smoke artifacts remain outside
  Git. The optional CSV/DuckDB overlay remains unimplemented and deferred.

## Non-goals

- Concurrent source-DuckDB writers.
- Keeping obsolete raw payload duplicates after canonical replacement.
- Interpolating missing parameter cells.
- CSV/DuckDB overlay in the core delivery.
- Treating source or plateau metrics as final MRS3 performance.
