# DuckDB Surface Coverage Review Design

## Goal

Add a read-only coverage and readiness review to the
`Import -> surface -> analysis -> JSON` panel flow. Before materialization, the
user must be able to see which `Pair + Side + TF` scopes have continuous source
data, satisfy the minimum shift/MA readiness contract, and can be selected for
surface construction.

## Layout

- The right-side operation card hosts the coverage review before a build.
- Rows are grouped as `Pair -> LONG/SHORT -> TF`.
- Each side subgroup uses the aligned columns
  `Select | TF | Available interval | Gap`.
- `Select` is the only interactive cell in a row.
- `Available interval` displays dates only as
  `YYYY-MM-DD .. YYYY-MM-DD`.
- Exact UTC timestamps remain in backend contracts, persisted evidence, and
  audit files; they are not shown in the compact panel table.
- Once materialization starts, the same card shows the existing progress and
  journal presentation.

## Factual Period Coverage

- Coverage is derived only from source-DuckDB `report_start/report_end` facts.
- Every interval uses normalized UTC half-open semantics `[start, end)`.
- A report cell's effective window is the intersection of its report-period
  window and persisted time-grid window. A row whose report and grid windows
  are both zero-duration is excluded before this calculation and contributes
  no coverage. Every other empty intersection is a fail-closed structural
  error.
- Windows are grouped by `Pair + Side + TF + Shift + OpenMA + CloseMA`, ordered,
  and merged when they overlap or touch (`next.start <= current.end`). Separate
  active reports may therefore extend one factual chain for the same cell.
- All effective start/end values form deterministic atomic timeline segments.
  For each segment, readiness is evaluated from the cells that fully cover that
  segment. Adjacent passing segments merge only when every Close MA in `2..7`
  retains at least one Open MA and qualifying witness sequence covering their
  combined interval; readiness is recomputed after every merge.
- Passing chains are ordered by longest duration, earliest start, earliest end,
  then the ordered per-Close witness vector. The first chain is the displayed
  interval.
- One continuous chain has `Gap = none`.
- Multiple chains list every missing interval chronologically as
  `missing: YYYY-MM-DD .. YYYY-MM-DD`.
- A row with a temporal gap is diagnostic-only and has no enabled checkbox.
- When no readiness-capable interval exists, the row shows the longest factual
  interval for diagnosis and remains disabled.
- When readiness is satisfied, the row shows the longest continuous interval
  shared by every Close MA in `2..7` under the minimum readiness contract.

## Minimum Shift Readiness Contract

The scope readiness contract is `close_ma_2_7_common_interval_v1`. It applies
the `shift_readiness_v1` sub-contract independently per selected MA pair and
records `readiness_max_shift_bp=430`. This is a minimum gate for starting a
build, not an upper bound on data included in a surface.

For one `Pair + Side + TF` row and one exact UTC interval:

1. Every Close MA in `2..7` must have at least one Open MA with fully covering
   reports on a shift sequence that begins at `30 bp`, passes through `150 bp`,
   and reaches `430 bp`.
2. Between `30 bp` and `150 bp`, consecutive shifts in that sequence may be no
   farther apart than `10 bp` (`0.1%`).
3. Between `150 bp` and `430 bp`, consecutive shifts may be no farther apart
   than `40 bp` (`0.4%`).
4. More detailed intermediate shifts are valid and never make a row fail.
5. For one Close MA, the same selected Open MA must fully cover the interval at
   every shift used by its qualifying sequence. Different Close MAs may select
   different Open MAs.
6. A checkbox is enabled only when both factual-period continuity and this
   shift/MA contract pass for the row.

For each MA pair, a canonical shift witness is selected independently in the
two bands. Starting at each band's lower boundary, choose the greatest
available shift not exceeding the current shift plus that band's maximum gap;
repeat until the exact upper boundary is reached. A candidate missing exact
boundary shifts `30`, `150`, or `430` fails. For each Close MA, choose the
lexicographically smallest passing `(open_ma, witness_shift_tuple)`. The scope
persists the ordered witness vector for Close MA `2..7`. Interval ties resolve
by longest duration, earliest start, earliest end, then this vector. One Close
MA cannot stitch different Open MAs across subintervals. Denser non-witness
shifts remain valid optional data.

The scope-level readiness contract is
`close_ma_2_7_common_interval_v1`; its per-pair shift sub-contract remains
`shift_readiness_v1` with `readiness_max_shift_bp=430`.

The contract is intentionally parameterized so a future specification may
raise the readiness limit to `700 bp` without changing the surface or audit
formats. This design does not activate the `700 bp` gate.

## Surface Inclusion

### Sparse Grid Contract V2

This specification explicitly supersedes the rectangular
`OBSERVED_GRID_CONTRACT` behavior in
`docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md` only for new
surfaces built by this workflow. Existing surfaces retain their original
contract and identity unchanged.

New publications use `OBSERVED_SPARSE_GRID_CONTRACT_V2`. Its immutable identity
contains:

- exact UTC half-open publication interval and side;
- selected `Pair + TF` scopes;
- readiness-contract version and maximum readiness shift;
- one ordered canonical readiness witness per Close MA `2..7` for every
  selected scope; each witness records its Close MA, selected Open MA, and
  canonical shift tuple;
- the complete sorted set of included canonical point keys, selected report
  IDs, source hashes, and their aggregate hashes;
- the publication-audit hash.

V2 keeps the existing persisted `canonical_point_key` format unchanged:
`pair|side|timeframe|shift_bp|open_ma|close_ma`. For evidence only, that key is
parsed as exactly six fields, its integer fields are normalized, and it must
round-trip through the existing canonical-key function without change. Its V2
evidence form is the JSON array
`[pair,side,timeframe,shift_bp,open_ma,close_ma]`. A malformed, non-round-tripping,
or duplicate persisted/evidence key rejects publication. V1 storage, validation,
and identity do not use the V2 evidence form and remain unchanged.

Canonical point evidence is one JSON Lines record per included point with keys
`point_key`, `report_id`, and `source_sha256`; records sort by the decoded
point-key tuple and end with LF. The lower-case SHA-256 of these exact bytes is
`point_evidence_sha256`. Thus every included point has exactly one selected
report and source hash, and the aggregate cannot hide a different point-to-report
assignment.

All identity-bearing JSON in this workflow, including point evidence and V2
`grid_contract_json`, uses one `canonical_json_v1` profile implemented with
Python standard-library `json.dumps`: `ensure_ascii=True`, `allow_nan=False`,
`sort_keys=True`, and `separators=(",", ":")`, encoded as UTF-8 without BOM.
Allowed values are objects with string keys, arrays, strings, base-10 integers,
booleans, and null; floats are forbidden. Standard JSON spellings are therefore
`true`, `false`, and `null`, and non-ASCII/string escaping is exactly the output
of that configured encoder. JSON Lines adds exactly one LF after every record;
canonical standalone JSON has no trailing newline.

V2 validates the exact persisted point-key set and source evidence rather than
requiring a rectangular `required_shifts_bp x MA-pair` product. Analysis storage
must version its grid validation so V1 remains strict and V2 accepts the frozen
sparse point set. V2 evidence is stored in the existing `grid_contract_json`,
which already participates in surface identity; this phase adds no analysis
schema migration. A new ADR records this replacement contract before code
implementation.

- The `430 bp` readiness limit does not truncate the published surface.
- After a row passes the minimum gate, materialization includes every factual
  point for that `Pair + Side + TF` whose report fully covers the selected exact
  UTC interval.
- This includes shifts above `430 bp`, denser intermediate shifts, and MA pairs
  that are present on only part of the observed shift set.
- Missing or incomplete optional shifts and non-selected Open MA pairs do not
  block the build after every Close MA `2..7` has one qualifying Open MA.
- If overlapping reports can provide one canonical point, the existing
  deterministic narrowest-covering-report rule selects its source report.
- Every included factual candidate and every readiness-required gap remains
  explicit in coverage audit evidence. Optional unobserved combinations are not
  enumerated, and no absent point is synthesized.
- Readiness inputs, the selected exact interval, all included point identities,
  and the readiness-contract version are frozen into immutable publication
  evidence.

## Multi-Side Build Queue

- The coverage scan returns LONG and SHORT groups together.
- The user may select rows from both sides in one action.
- LONG and SHORT are still published as separate immutable surfaces because a
  surface has one side.
- For each side, the backend derives the maximal exact UTC intersection shared
  by every selected row and revalidates readiness on that interval.
- An empty common intersection rejects the queue before any side starts.
- The compact panel shows only the common start/end dates; the backend freezes
  the exact timestamps.
- The backend groups selected rows by side and builds the two side requests
  sequentially through one server-owned in-process queue.
- Side order is deterministic: LONG, then SHORT.
- Before either side publishes, one background job opens one read-only DuckDB
  connection and explicit transaction, derives both side preflights, revalidates
  both, and materializes every selected side in memory. A preflight, source,
  cancellation, or materialization failure rolls back the read transaction and
  publishes nothing.
- After every side is prepared, the read transaction commits and the source
  connection closes. The job then opens analysis DuckDB and publishes the
  prepared immutable surfaces sequentially.
- Prepared sides publish independently in deterministic order. If a later
  publication fails after an earlier side committed, the queue reports
  `PARTIAL`. Manual rerun is the recovery path in this phase; deterministic
  surface identity deduplicates an already committed LONG surface.
- Closing or refreshing the browser does not prevent the already accepted
  second side from starting.
- Cancellation before publication publishes nothing. Cancellation after one
  side commits stops before the next side and reports `PARTIAL`.
- Queue status reports the active side and ordinal, for example `LONG 1/2`.

## Human-Readable Coverage Audit

Every completed initial scan produces a diagnostic `coverage_inventory.csv`.
After selection, each side preflight produces a separate immutable
`surface_coverage_audit_<side>.csv` for the exact common publication interval.
The publication audit is hashed and linked from V2 surface evidence; the
diagnostic inventory is never presented as publication evidence.

The diagnostic inventory contains one evaluation block for every deterministic
candidate chain considered for a `Pair + Side + TF` row. Its
`interval_start_utc`/`interval_end_utc` are that chain's exact interval,
`evaluation_id` is the lower-case SHA-256 of its canonical JSON scope-and-interval
tuple, and `displayed_interval=true` marks the one selected by the row tie-breaker.
Candidate selection and readiness-gap statuses are evaluated independently for
that exact block. The publication audit contains one evaluation block for the
side's exact common publication interval and has `displayed_interval=true`.

Structurally degenerate rows whose report and grid windows are both
zero-duration are ignored and emit no row in the current CSV schemas. Every
other empty report/grid intersection aborts fail-closed. The existing
`required_for_readiness` and `readiness_witness` columns identify the six
selected per-Close witnesses without adding columns, statuses, or reason codes.

Both files are suitable for manual analysis and use two explicit row types:

- `POINT_CANDIDATE`: one row per factual source-report candidate for an observed
  `Pair + Side + TF + Shift + OpenMA + CloseMA` point. Exactly one fully covering
  candidate may have `selected_report=true`; overlapping candidates that lose
  deterministic selection remain visible as `EXCLUDED` rows.
- `READINESS_GAP`: one synthetic row for each missing exact boundary or excessive
  gap detected for an observed MA pair. It has no source report and leaves
  `shift_bp`, `report_id`, and `source_sha256` empty rather than inventing a
  factual point.

For one scope, the MA universe is the union of MA pairs factually observed in
that scope. Optional shifts emit only factual report candidates. Readiness
evaluation additionally emits `READINESS_GAP` rows for an observed MA pair when
an exact boundary is absent or a band gap exceeds its maximum. The CSV does not
invent a completely unobserved MA pair or rely on an external expected MA grid.

Required columns are:

- `pair`, `side`, `timeframe`;
- `evaluation_id`, `displayed_interval`;
- `row_type`: `POINT_CANDIDATE` or `READINESS_GAP`;
- `shift_bp`, `open_ma`, `close_ma`;
- exact `interval_start_utc`, `interval_end_utc`;
- exact `report_start_utc`, `report_end_utc`, `grid_start_utc`, `grid_end_utc`,
  `effective_start_utc`, `effective_end_utc` when applicable;
- `required_for_readiness`, `readiness_witness`;
- `gap_start_bp`, `gap_end_bp`, `max_gap_bp` when applicable;
- `report_id`, `source_sha256`, `selected_report` when applicable;
- `status`: `AVAILABLE`, `MISSING`, or `EXCLUDED`;
- `reason_code`, `reason_detail`;
- `readiness_contract_version`, `readiness_max_shift_bp`.

Status semantics are stable:

- `AVAILABLE`: a `POINT_CANDIDATE` is the deterministically selected report and
  fully covers the evaluated exact interval;
- `MISSING`: a `READINESS_GAP` records that an observed MA pair lacks an exact
  boundary or has a shift gap larger than the allowed maximum;
- `EXCLUDED`: a `POINT_CANDIDATE` does not fully cover the evaluated interval or
  loses deterministic overlapping-report selection. A structural source failure
  stops the scan and does not produce a misleading audit CSV.

`reason_code` is one of `AVAILABLE`, `INTERVAL_NOT_COVERED`,
`OVERLAP_NOT_SELECTED`, `MISSING_BOUNDARY`, or `SHIFT_GAP_EXCEEDS_MAX`.
`reason_detail` is exactly one of these deterministic ASCII templates:

- `AVAILABLE: selected_report=true`;
- `INTERVAL_NOT_COVERED: effective_start_utc=<UTC>, effective_end_utc=<UTC>`;
- `OVERLAP_NOT_SELECTED: selected_by_tiebreak=true`;
- `MISSING_BOUNDARY: boundary_bp=<integer>`;
- `SHIFT_GAP_EXCEEDS_MAX: gap_start_bp=<integer>, gap_end_bp=<integer>, max_gap_bp=<integer>`.

Integers use base-10 without leading zeros, UTC values use the timestamp format
below, and free-form exception text is never embedded. Candidate report IDs and
the selected-report flag remain in their dedicated columns. This keeps the audit
readable without making hashes depend on mutable prose.

CSV bytes are canonical and reproducible:

- UTF-8 without BOM, LF line endings, fixed column order above, Python standard
  library `csv` formatting with `QUOTE_MINIMAL`, and empty fields for nulls;
- exact UTC timestamps use `YYYY-MM-DDTHH:MM:SS.mmm+00:00`; booleans use lower-case
  `true`/`false`, and integers use base-10 without leading zeros;
- rows sort by `pair`, LONG-before-SHORT side order, `timeframe`,
  `interval_start_utc`, `interval_end_utc`, `evaluation_id`, `row_type`,
  `open_ma`, `close_ma`, nullable `shift_bp`, nullable gap fields, and `report_id`,
  with empty values sorting before populated values;
- the audit hash is lower-case SHA-256 of the exact CSV bytes.

Before publication, each side audit is generated and hash-verified in memory,
then written to the configured audit output. V2 `grid_contract_json` stores its
logical artifact name, schema version, row count, and SHA-256; absolute local
paths are not part of identity. Publication cannot commit until byte/hash
verification and artifact writing succeed. The diagnostic inventory uses the
same canonical CSV format, but its hash is not part of surface identity.

## Deferred Infrastructure

- Persistent prepared queues and retry endpoints are deferred until one real
  process crash, two `PARTIAL` runs within 30 days, or median preparation above
  10 minutes demonstrates the need.
- Shared source-path leases are deferred until one reproducible managed
  import/build conflict or supported concurrent controllers require them.
- Atomic source path-replacement handling is deferred until production replaces
  a source database while a direct job is active.
- A new analysis evidence table/schema version is deferred until V2
  `grid_contract_json` exceeds 10 MB per surface or indexed evidence queries are
  measurably required.

Longer per-Close partial intervals are not displayed, combined, or materialized
as one readiness surface. A future `coverage_summary.csv` may report those
intervals and ignored degenerate rows without changing the current canonical
CSV schemas.

The CSV covers all observed shifts, including shifts above `430 bp`, while
clearly distinguishing optional data from combinations required to enable the
checkbox. The panel shows a compact summary and exposes the CSV as an artifact.

## Preflight Activity Feedback (Deferred Next Panel Phase)

This section records the agreed next-phase behavior. It is excluded from the
current coverage-contract implementation plan and its acceptance tests.

- `Check coverage` reuses the existing right-side progress bar.
- While the synchronous coverage request is active, the bar is indeterminate,
  the state reads `Preparing coverage...`, and elapsed time is shown.
- Repeated coverage submissions are disabled until the request succeeds or
  fails.
- Success replaces the activity state with the grouped coverage table.
- Failure shows an explicit failed state and restores the action controls.
- This phase does not invent a percentage from a query that has no stable total.
  Staged backend progress may be added later if measurements justify it.

## Data Flow

1. The user starts one coverage scan.
2. The backend inventories both sides, excludes structurally degenerate rows,
   evaluates factual period chains, checks the common Close MA `2..7` readiness
   contract, and writes the coverage CSV.
3. The panel renders `Pair -> Side -> TF` groups and enables only rows that pass
   both temporal and minimum readiness checks.
4. The user selects any enabled rows from one or both sides.
5. For each side, the backend derives and displays the common selected date
   interval, revalidates the exact UTC intersection, freezes the side-specific
   preflight request, and queues it.
6. Each side materializes all fully covering factual points, not only the
   minimum readiness grid.
7. The panel switches to queue/build progress and journal output.

## Non-Goals

- Do not infer source periods from an external expected trading schedule.
- Do not synthesize missing shifts, MA pairs, reports, or points.
- Do not make optional data above the current readiness gate mandatory.
- Do not activate the future `700 bp` readiness limit in this phase.
- Do not combine LONG and SHORT into one immutable surface.
- Do not add portfolio simulation or treat source metrics as tested MRS3
  performance.

## Verification

- Coverage tests prove deterministic merging and complete missing-interval
  reporting.
- Readiness tests cover exact minimum spacing, denser valid shifts, missing
  boundaries, excessive shift gaps, all Close MAs `2..7`, different selected
  Open MAs, and deterministic common-interval tie-breaking.
- Tests prove optional incomplete shifts and non-selected Open MA pairs,
  including data above `430 bp`, do not disable an otherwise ready row.
- Coverage tests prove that structurally degenerate rows are ignored while
  every other empty report/grid intersection remains fail-closed.
- Materialization tests prove every fully covering factual point is included and
  no absent point is synthesized.
- Storage tests prove V1 rectangular validation remains unchanged, V2 sparse
  identity is deterministic, and the persisted readiness witness and exact
  point set participate in surface identity.
- Controller tests prove mixed LONG/SHORT selections create two sequential,
  side-specific immutable publications, freeze both sides before the first
  publication in one read transaction, order LONG before SHORT, and handle
  cancellation and partial publication deterministically.
- CSV tests cover stable ordering, required columns, explicit missing rows, and
  deterministic reason grammar, exact half-open UTC serialization, diagnostic
  chain evaluation versus publication evidence, CSV hashes, and CSV-to-surface
  provenance.
- Current-phase UI tests cover the nested Pair/Side layout, date-only display,
  enabled and disabled checkbox states, and side queue progress. Indeterminate
  preflight feedback and elapsed-time tests belong to the deferred panel phase.
- Focused verification includes `tests/test_duckdb_direct.py` and
  `tests/test_panel.py`.
