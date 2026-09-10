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
- The fresh-analysis API and Panel show a stable, path-free message when that
  workbook is absent or unavailable.
- Configuration examples and installation documentation name the required,
  user-provided `input/dates.xlsx` input without tracking private XLSX data.
- The settings screen must not mirror `listing_dates_path` into the UI-only
  `panel.path_defaults` map when it saves the Analysis profile.

## Non-goals

- Supply, synthesize, or commit listing-date data.
- Change Source v6 analysis thresholds, artifacts, or listing-date semantics.
- Remove live connection settings or operator-selected import/merge paths.

## Invariants

- Fresh analysis remains fail-closed without a readable listing-date workbook.
- API and UI errors do not disclose local filesystem paths.
- A fresh clone can identify the missing required input before an operator
  mistakes it for an invalid general configuration.

## Acceptance evidence

- Focused controller/HTTP tests distinguish missing listing dates from generic
  settings errors.
- Static UI tests prove the Analysis-profile save payload has one source of
  truth for listing dates.
- The configuration examples and README document the required private input.
