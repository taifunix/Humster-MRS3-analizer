# Panel Fresh Analysis Settings

**Status:** Active
**Date:** 2026-09-10

## Goal

Make the static Panel's fresh Source v6 analysis configuration explicit and
portable: a missing listing-date workbook must never surface as the generic
`invalid settings` error.

## Scope

- `panel_workflow.listing_dates_path` is the sole runtime setting for the
  workbook used by fresh analysis and retest workflows.
- `duckdb_import.workers` is the sole editable machine-wide local CPU worker
  limit. Static Panel Settings exposes it as `Общий лимит рабочих процессов`
  with help covering import, analysis, surface materialization/publication,
  Performance v2, and Portfolio Search. Tester submission concurrency and
  API/network concurrency remain separate limits.
- Panel-driven DUCKDB_DIRECT and Performance v2 import/selection read the
  common worker limit. Legacy `direct_materialization.workers` and
  `unified_performance_v2.workers` may remain in old local files for
  compatibility but do not override it; tracked examples no longer advertise
  them. Direct materialization preserves its other tuning values
  and normalizes `max_in_flight_chunks` so it can accommodate the common limit.
- Source v6 surface materialization/publication and cached local Source DB
  services use the current common worker value for each subsequent job,
  without requiring a Panel restart.
- Analysis Profile projects and saves only visible fresh-analysis controls.
  `canonical_shifts_bp`, `plateau.isolated_peak_relative`,
  `close_support.core_min`, `close_support.supported_min`, and `target_dd`
  remain unchanged in config and are omitted from the public profile. Worker
  configuration is saved through the existing generic settings endpoint as
  `operational.import_workers`.
- The fresh-analysis API and Panel show a stable, path-free message when that
  workbook is absent or unavailable.
- Configuration examples and installation documentation name the required,
  user-provided `input/dates.xlsx` input without tracking private XLSX data.
- The settings screen must not mirror `listing_dates_path` into the UI-only
  `panel.path_defaults` map when it saves the Analysis profile.

## Source v6 READY scope preflight preview

The `3. Выбрать READY scopes` surface preflight presents a compact table for
each Pair+Side group. A timeframe row contains the selection cell, timeframe,
data period, display-only `PnL > 10%` count/total and percentage, display-only
PnL median/maximum, and status. The removed Grid, point-count, and trade-count
columns are outside this contract.

The backend attaches the canonical READY interval (`start`, inclusive `end`,
and day count) to READY rows. A non-READY row may expose a raw metadata span,
marked as non-READY data. Pair summaries expose timeframe count, the
intersection of their displayed READY intervals when one exists, total
comparable points, the `> 10%` count and percentage, aggregate median and
maximum, and READY `x/y`.

The preview reads only `SourceV6FragmentMetadata.metrics`; it never decodes
fragment payloads or performs another database scan. `TotalPnLPercent` and
`Total PnL, %` are equivalent aliases. Values are grouped by canonical point
and are shown only when every point has exactly one metadata fragment, every
fragment covers the full READY interval, and all values are finite decimals.
Duplicate points, missing READY intervals, malformed metrics, or incomplete
coverage return an unavailable preview. The threshold is fixed at `> 10` and
does not affect scope selection.

Acceptance evidence includes focused backend tests for interval attachment,
decimal-safe preview statistics, and unavailable fragmented data, plus static
UI tests for the row and aggregate column contract.

## Non-goals

- Supply, synthesize, or commit listing-date data.
- Change Source v6 analysis thresholds, artifacts, or listing-date semantics.
- Remove live connection settings or operator-selected import/merge paths.

## Invariants

- Fresh analysis remains fail-closed without a readable listing-date workbook.
- API and UI errors do not disclose local filesystem paths.
- A fresh clone can identify the missing required input before an operator
  mistakes it for an invalid general configuration.
- The profile API never writes hidden analysis values from a client payload and
  preserves their existing JSON values during visible profile saves.
- A common worker change does not alter tester max-parallel submissions or
  external API/network concurrency.

## Acceptance evidence

- Focused controller/HTTP tests distinguish missing listing dates from generic
  settings errors.
- Static UI tests prove the Analysis-profile save payload has one source of
  truth for listing dates.
- Config, controller, and static UI tests prove the common worker value is
  saved once and consumed by DUCKDB_DIRECT, Source v6, surfaces, Performance v2,
  fresh analysis, and Portfolio Search while hidden analysis values survive a
  visible profile save unchanged.
- The configuration examples and README document the required private input.
