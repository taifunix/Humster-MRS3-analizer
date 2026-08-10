# v0.7 Optional CSV-DuckDB Overlay

**Status:** Optional / Deferred

**Depends on:**
[DuckDB analysis storage and importer](2026-08-11-v07-duckdb-analysis-storage-and-importer.md),
[event filter and shortlist](v07-event-filter-and-shortlist.md)

## Goal

Optionally build one plateau-analysis surface where CSV is the base coarse
grid and source DuckDB adds complete fine-grid parameter cells. This feature
must not block direct DuckDB surfaces, analysis storage, importer integration
or the rest of v0.7, and may remain unimplemented if direct DuckDB analysis is
sufficient.

## Inputs

An overlay consumes:

- one or more CSV files;
- one source DuckDB;
- one explicit UTC half-open period `[start, end)` applying to the whole CSV
  batch and DuckDB materialization;
- one side inferred from CSV;
- selected symbols from coverage preflight;
- one grid and point-materialization configuration.

Plateau/selection algorithm configuration is supplied only when creating the
subsequent `analysis_run`; it is not part of overlay surface identity.

CSV rows need no per-row timestamps; the panel period is their declared
period. The batch must contain one explicit batch-level `[start, end)`
declaration equal to the requested build period. Missing or inconsistent
declarations and mixed CSV sides fail before preflight. The accepted
declaration is stored in surface provenance.

## Canonical point identity

Both sources normalize to:

```text
symbol | side | timeframe | shift_bp | open_ma | close_ma
```

The surface period belongs to the manifest. Raw multiplier text is provenance,
not identity.

## Coverage preflight

The CSV batch defines symbols, side, timeframes and the exact canonical
`(open_ma, close_ma)` pair universe. For each symbol/timeframe the source
DuckDB must cover the whole selected period. DuckDB MA pairs absent from that
CSV universe never enter the surface and are counted in the coverage audit.

### Required core

For every CSV MA pair, DuckDB must contain every shift from `0.3%` through
`1.4%` inclusive in `0.1%` increments. One absent/conflicting core cell
excludes that timeframe from both sources. Missing timeframes are allowed. A
symbol is unavailable only when no complete timeframe remains.

### Optional extensions

Tested shifts above `1.4%`, including fine runs through `2.0%` or farther, are
included only when the whole CSV MA-pair set exists at that shift. An
incomplete optional shift is excluded and audited without blocking the
timeframe.

CSV may use a different coarse grid, normally in `0.3-0.4%` increments.

### Pair selection UI

Usable symbols have enabled, selected-by-default checkboxes. The user may
deselect them. A symbol with no usable timeframe shows a red warning instead
of an activatable checkbox. Included/excluded timeframes and exact missing
cells are visible before build.

## Overlay and deduplication

1. Retain CSV rows only for selected symbols and complete timeframes.
2. Materialize DuckDB rows for the same symbols, side, period and timeframes.
3. Retain required core and complete optional shifts.
4. Union exact canonical keys.
5. CSV metrics win on a key present in both sources.
6. Store both observations and metric differences in `dedup_decisions`.

CSV-only points are valid. DuckDB-only points are valid only when they are
required-core or complete optional-shift cells for an MA pair declared by the
CSV batch. Unrelated DuckDB MA pairs are excluded. There is no averaging,
interpolation or nearest-shift matching.

Identical CSV observations collapse with contributor provenance. Conflicting
CSV observations block the symbol/timeframe. Conflicting DuckDB observations
block a required cell or exclude an optional shift.

## Event semantics

Every final point uses `point_event_count = TotalTrades`. Confirmed DuckDB event
IDs and cycle unions are not selection inputs. The final surface has one
trades-proxy mode and cannot mix event semantics.

## Storage compatibility

Overlay publication uses the same analysis-DuckDB tables and immutable
`surface_id` contract as `DUCKDB_DIRECT`. Its `build_mode` is
`UNIFIED_OVERLAY`; source provenance records both CSV and DuckDB hashes.

Plateau analysis, candidates, lineage, library browsing and export do not need
overlay-specific branches after the surface is published.

## Panel workflow

If implemented, a separate **CSV + DuckDB overlay** workflow provides CSV
multi-selection, source DuckDB, period, coverage preflight, symbol/timeframe
status, overlap/dedup counts and publication. The direct DuckDB workflow
remains independent.

## Acceptance evidence

- Required core and optional-extension coverage tests.
- Batch-period declaration mismatch/missing tests.
- Extra DuckDB-only MA-pair exclusion and audit tests.
- Timeframe exclusion and unavailable-symbol UI tests.
- CSV-priority and canonical dedup tests.
- Source provenance and metric-difference audit tests.
- Final-key uniqueness and `PointEventCount = TotalTrades` tests.
- Analysis-storage and export parity with `DUCKDB_DIRECT`.
- Different plateau algorithm configurations reuse the same published
  `UNIFIED_OVERLAY` surface and point set.

## Non-goals

- Blocking core v0.7 delivery.
- Averaging source metrics.
- Retaining real event IDs for selection.
- Filling missing grid cells by interpolation.
