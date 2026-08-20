# Plateau point composition export

## Purpose

This feature adds a deterministic CSV export that materializes plateau
composition as point-level rows. The export is read-only and derived from the
published analysis DuckDB for a single `analysis_run`.

The output is meant to show which points belong to each plateau and what each
member point looks like in terms of pair, timeframe, provenance, and point
metrics.

## Non-goals

- Do not change plateau formation, plateau membership rules, or lineage rules.
- Do not change analysis storage schema.
- Do not infer or recompute plateau membership outside the stored
  `plateau_members` relation.
- Do not alter existing exports other than adding this new CSV to the analysis
  export bundle.

## Input

- `run_id`: the published analysis run to export.

## Output

- `plateau_point_composition.csv`

The CSV contains one row per plateau-member point. The row is formed by joining
`plateau_members` to `surface_points` and `surface_pairs`.

Required columns:

- `run_id`
- `surface_id`
- `plateau_id`
- `plateau_member_count`
- `symbol`
- `side`
- `pair_key`
- `shift_bp`
- `open_ma`
- `close_ma`
- `timeframe`
- `canonical_point_key`
- `point_event_count`
- `source_report_id`
- `source_hash`
- `provenance_state`
- `metrics_json`

## Invariants

- The export must be deterministic for the same analysis database and run.
- Rows must be ordered by plateau identity, then pair identity, then timeframe,
  then `canonical_point_key`.
- The output must be produced from the supplied analysis connection only.
- The CSV must remain stable across repeated exports of the same run.
- `metrics_json` must remain canonical JSON when serialized.

## Acceptance evidence

- The analysis export button produces the new CSV alongside the existing run
  files.
- Focused export tests verify the row shape, order, and deterministic content.
- The generated CSV for the current run can be inspected directly in the
  analysis output directory.
