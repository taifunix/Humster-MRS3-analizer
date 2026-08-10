# v0.7 DuckDB Analysis Storage and Importer

**Status:** Proposed for implementation

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
- Replacement updates report metadata and payload in one transaction.
- A failed transaction leaves the previous canonical report readable.
- Committed batches may remain after a later job failure; repeating the same
  import safely skips identical reports and resumes the rest.
- HTML is never deleted by the importer.
- Quarantine and deletion checklists remain mandatory operational evidence.

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

Plateau/selection algorithm settings are not surface inputs. They belong only
to an `analysis_run`, so a different plateau configuration reuses the same
published point surface.

Reports must cover the whole period. Missing or conflicting required grid
cells exclude the affected timeframe. A symbol is unavailable only when no
usable timeframe remains. The panel shows exact coverage reasons before build.

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
2. Take the union of observed MA pairs over required shifts.
3. Require the same MA-pair set at every required shift.
4. Exclude incomplete timeframes and record missing cells.
5. State that a point absent at every shift cannot be discovered from observed
   data alone.

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
- `plateaus`;
- `plateau_members`;
- `candidates`;
- `plateau_lineage`.

Raw actions, equity, wallet series and HTML are never copied into it.

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

Period-specific plateau IDs are local to their run. `plateau_lineage` connects
runs using common canonical points and geometric overlap:

- `CONTINUED`;
- `SPLIT`;
- `MERGED`;
- `NEW`;
- `DROPPED`.

Lineage never merges period metrics.

## Analysis panel and exports

The panel provides:

- source coverage preflight by symbol/timeframe;
- selected-by-default checkboxes for usable symbols;
- red noninteractive warnings for unavailable symbols;
- surface build progress and final unique-point counts;
- economic/event-eligible points, plateaus and READY candidates;
- a surface library with period, side, parent, source identity and analyses;
- **Refine**, **Re-run analysis**, **Compare periods** and export actions.

The analysis DuckDB is canonical. CSV and Excel are generated exports of a
specific immutable surface or analysis run.

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

## Non-goals

- Concurrent source-DuckDB writers.
- Keeping obsolete raw payload duplicates after canonical replacement.
- Interpolating missing parameter cells.
- CSV/DuckDB overlay in the core delivery.
- Treating source or plateau metrics as final MRS3 performance.
