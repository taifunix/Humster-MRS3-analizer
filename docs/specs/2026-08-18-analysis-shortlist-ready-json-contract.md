# Analysis shortlist and READY JSON contract

## Purpose

This feature makes the analysis shortlist usable for JSON strategy generation
from the UI.

The shortlist table must expose the frozen 1ORD base count alongside the
existing 2ORD, 3ORD, and 4ORD counts, so users can see where base strategies
exist.

The READY JSON action must send an explicit list of selected scopes to the
backend, so the backend can generate strategies from the currently visible
Pair/TF scope set.

## Non-goals

- Do not change shortlist scoring, plateau selection, or candidate filtering
  rules.
- Do not change the analysis strategy generation contract beyond providing the
  required selected scopes payload from the UI.
- Do not change how READY/MRS3 structures are validated or published.

## Input

- One immutable published analysis run.
- The current shortlist scope filters in the UI.

## Output

- A shortlist table that includes:
  - Pair
  - TF
  - 1ORD
  - 2ORD
  - 3ORD
  - 4ORD
  - READY
  - DEFERRED
  - ALL

- A READY JSON request payload that includes:
  - `run_id`
  - `criteria`
  - `selected_scopes`
  - `template_path`
  - `output_dir`
  - `config_path`

`selected_scopes` must be a JSON list of objects with:

- `symbol`
- `side`
- `timeframe`

## Invariants

- `selected_scopes` must never be sent as a string, tuple, or object map.
- The UI must fail closed if no shortlist scopes are available.
- The shortlist table must stay deterministic for the same analysis run and
  filters.
- The 1ORD count is informational and must reflect the frozen base count
  already returned by the analysis shortlist.

## Acceptance evidence

- The shortlist table shows a `1ORD` column with nonzero counts when frozen
  base candidates exist.
- Clicking `Generate READY JSON` from a populated shortlist no longer raises
  `selected_scopes must be a list`.
- Focused panel tests cover the new column and the generated payload shape.
