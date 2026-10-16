# Performance v2 finalist snapshots, analogs and XLSX review import

**Status:** Implemented and independently reviewed; live v9 migration pending
**Date:** 2026-09-02
**Depends on:**
[Performance v2 robust finalist ranking](2026-09-01-performance-v2-robust-finalist-ranking.md),
[ADR-0021](../decisions/0021-performance-v2-persisted-selection-snapshots.md),
[ADR-0062](../decisions/0062-performance-v2-selection-review-clear-fields.md),
[ADR-0065](../decisions/0065-performance-v2-card6-partial-import-finalist-reconciliation.md)

## Purpose

Turn the current disposable finalist workbook into a reproducible review loop:
select and rank candidates, collapse exact structural analogs, persist the
automatic decision, let the operator edit status/rank in XLSX, and import the
review back into the same Performance DB.

The automatic result should normally contain at most 20 finalists while
preserving every candidate and every decision in the audit trail.

`Trades` in selection, filters and XLSX means the full-period count of completed
round trips calculated from `strategy_actions`. It must never use the tester
report's `strategy_results.total_trades`, which can count partial fills and
other execution actions instead of completed positions.

`Positive quarters` in XLSX renders `positive/available`, such as `4/4` or
`3/3`. A missing or unusable early window reduces the denominator; it is not
treated as a negative quarter. The filter continues to use the numeric positive
count and does not eliminate unavailable data by itself.

The XLSX also exposes `Дней A` and `Дней B` directly beside their respective
`PnL A/30д, %` and `PnL B/30д, %` values. They are the calendar durations used
for the 30-day normalization, not the first-to-last transaction span; their
numeric cells are centered and blue, with widths derived from data values.

## Scope and non-goals

This stage:

- revises the final score and adds `Worst Hold p95`;
- disables `pareto_close_ma_near_tie` by default but keeps it available;
- identifies analogs inside an exact structural group;
- persists each downloaded selection as an immutable snapshot;
- exports editable user status, rank, analog target and comment fields;
- imports a complete reviewed workbook atomically;
- stores the current durable `REJECTED` tag;
- enables the A/B panel's `Только финалисты` filter from the latest snapshot.

This stage does not:

- delete strategies, results or imported facts;
- implement a deletion queue or a `REJECTED` cleanup command;
- implement RETEST, discard, portfolio simulation or correlation clustering;
- invent fuzzy analog groups;
- accept old or manually assembled workbooks;
- add a generic tag editor. `REJECTED` is the only durable tag in this stage.

A later deletion feature may select `REJECTED` strategies and export a separate
review workbook before any destructive operation. That feature requires its
own specification and explicit confirmation.

## Terms

- **Selection run**: immutable automatic result for one Pair + Side and one
  submitted stage configuration.
- **Analog group**: strategies with exactly the same Pair, Side, timeframe,
  order count and Close MA.
- **Representative**: the highest-ranked surviving strategy in an analog
  group.
- **Review import**: one immutable record of an accepted operator-edited XLSX.
- **Effective decision**: the latest accepted user decision for a run, or its
  automatic decision when the run has not been reviewed.

## Revised automatic ranking

### Population and components

The score population is the Pair + Side survivor set after all enabled movable
filters and Pareto stages and before analog collapsing. Existing definitions of
the six ranking facts remain unchanged. Add
`worst_holding_p95_minutes = max(full-period holding p95, window-B holding p95)`.

Convert present values to deterministic quality percentiles from `0` (worst)
to `1` (best), using average rank for ties. One present value receives `1`.
Minimum plateau-point percentiles remain timeframe-relative; all other
components are relative to the full Pair + Side survivor population.

```text
weighted_sum = (
    0.30 * percentile(robust_pnl_30d_pct, higher is better)
  + 0.15 * percentile(worst_drawdown_pct, lower is better)
  + 0.15 * percentile(ab_stability_ratio, higher is better)
  + 0.12 * percentile(worst_holding_p95_minutes, lower is better)
  + 0.10 * percentile(first_shift_bp, higher is better)
  + 0.09 * percentile(minimum_plateau_point_count, higher is better)
  + 0.09 * percentile(close_ma_len, lower is better)
)
score = 100 * weighted_sum / sum(weights of present components)
```

Missing components are omitted and the remaining weights are renormalised.
They are never imputed with a neutral percentile.
No present component means no score. Missing data never causes a hard
elimination; an unrankable representative becomes `RESERVE` with reason
`RANK_NOT_EVALUATED_INSUFFICIENT_DATA`.

The percentile is `(average_rank - 1) / (n - 1)` after orienting rank so better
values receive larger quality. For `n = 1`, quality is `1`. Renormalised scores
remain comparable inside that selection run by contract; they are not
comparable between runs or Pair + Side populations.

Deterministic order for score ties is:

1. `PnL DD5/30` descending, missing last;
2. Robust PnL descending, missing last;
3. Worst DD ascending, missing last;
4. A/B stability descending, missing last;
5. Worst Hold p95 ascending, missing last;
6. Shift 1 descending, missing last;
7. PointsMin descending, missing last;
8. Close MA ascending, missing last;
9. Strategy ID ascending.

`pareto_close_ma_near_tie` remains available in the panel but becomes disabled
by default. It must not remove candidates before the weighted representative
selection unless the operator explicitly enables it.

### Analog representative

After movable stages and score calculation, partition survivors by the exact
key:

```text
(symbol, side, timeframe, order_count, close_ma_len)
```

Choose one representative per group by final score and the tie order above,
excluding members with a current `REJECTED` tag while any non-rejected member
exists. If every group member is rejected, retain the ordinary deterministic
representative only for automatic audit; every row's effective status remains
`REJECTED`. If every eligible member is unrankable, choose the lowest Strategy
ID and mark that representative `RESERVE` for insufficient data. Every other
survivor receives automatic status `ANALOG`, a stable group key and
`auto_analog_of_strategy_id` equal to the representative's Strategy ID.

The exact group may additionally span adjacent Close MA values only when every
order references the same exact `(analysis_run_id, plateau_id)` as the other
members. The permitted Close MA range is inclusive and has width at most one:
`5/6` is one group, while `5/6/7` is deterministically split into `5/6` and
`7`; no transitive adjacency is allowed. Missing order or plateau identity
keeps the strategy in a one-member group.

Filtered rows remain `FILTERED`; analog metadata must not replace their first
filter reason. They are not eligible to represent a group.

### Top N

Rank representatives only. The fixed final stage defaults to `top_n = 20`:

- the first N representatives without a current `REJECTED` tag receive
  `FINALIST`;
- rankable representatives after N receive `RESERVE`;
- an unrankable representative receives `RESERVE`;
- non-representatives receive `ANALOG`;
- rows rejected by an earlier stage receive `FILTERED`.

`ANALOG` and `FILTERED` rows never consume Top-N slots. A representative with a
prior `REJECTED` tag keeps its calculated rank for audit, receives automatic
status `RESERVE` with reason `PRIOR_USER_REJECTED`, and also does not consume a
slot. Its effective and prefilled user status remains `REJECTED`.

Top N is a ceiling, not a quota. If only 14 representatives survive, all 14
are finalists; the system does not promote weaker rows merely to reach 20.

## Status contract

The closed status vocabulary is:

| Status | Meaning |
| --- | --- |
| `FINALIST` | Selected automatic or operator-approved final candidate. |
| `RESERVE` | Valid candidate below the current cutoff or intentionally held in reserve. |
| `ANALOG` | Alternative to another identified representative. |
| `FILTERED` | Excluded by an automatic filter or Pareto stage. |
| `REJECTED` | Explicitly rejected by the operator, including anything previously described as “мусор”. |

`BELOW_CUTOFF` and a separate “мусор” status do not exist. `REJECTED` is only a
tag/status; it never deletes data.

## Performance DB storage contract

The product remains Performance DB v2 (`database_kind =
unified_performance_v2`), but its internal schema advances transactionally
from version `2` to `3`. Fresh databases are created directly at version `3`.
No v1 migration or sidecar database is introduced.

Schema version 3 adds a generated `database_instance_id` marker and these
tables:

### `selection_runs`

One row per downloaded workbook:

- `selection_run_id` (application-generated UUID string, primary key);
- `database_instance_id`, `symbol`, `side`;
- `selection_contract_version`;
- canonical `request_json` and `request_sha256`;
- canonical relevant `config_json` and `config_sha256`;
- `candidate_count`, `representative_count`, `auto_finalist_count`, `top_n`;
- `workbook_sha256`, `created_at_utc`.

### `selection_results`

One immutable row for every candidate in a run, keyed by
`(selection_run_id, strategy_id)`:

- `result_id_at_selection` (stored value, deliberately no foreign key);
- automatic status, score, rank and reason;
- analog group key and automatic representative Strategy ID;
- whether a current `REJECTED` tag existed when the run was created;
- canonical stage trace JSON.

Strategy/result foreign keys are deliberately absent from immutable selection
history so a separately approved future cleanup cannot erase or block audit
history.

### `selection_review_imports` and `selection_review_rows`

`selection_review_imports` stores one successful import UUID, its run ID,
workbook SHA-256, import timestamp and row count. `selection_review_rows`
stores one row per candidate with submitted user status, optional user rank,
optional analog target and comment. Effective values are derived, not stored:
the latest review wins; without a review, prior `REJECTED` evidence wins over
the automatic result. Every successful edited re-import appends history; it
does not update an earlier review.

### `strategy_tags`

Stores only the current durable tag in this stage:

```text
(strategy_id, tag='REJECTED', source_review_import_id, updated_at_utc)
```

Changing a reviewed row away from `REJECTED` removes its current tag. The
append-only review rows retain the history of that earlier rejection.
Here “durable” means persisted until a later explicit review changes it;
“non-destructive” means strategy/result facts and review history are never
deleted.

Tag synchronisation is restricted to Strategy IDs present in the imported run:
insert or keep `REJECTED` where effective status is `REJECTED`, and remove it
only for rows from that same run whose effective status changed away from
`REJECTED`. Tags for strategies outside the run are untouched.

The version-2-to-3 migration, database-instance marker creation and schema
marker bump are one DuckDB transaction. Marker mismatch fails closed. Every
review import is also one transaction. Any validation or write failure rolls
back the whole operation.

The schema migration is an automatic one-time operation on the first panel
access that needs the existing Performance DB. It is idempotent and does not
invalidate or recalculate `window_metrics`; later accesses only validate schema
3. The existing tester-result import contract remains unchanged and writes the
same strategy/result/action/equity tables.

## XLSX contract

Pressing `Смотреть результаты в xls` becomes the explicit save operation:
the server calculates the result, builds workbook bytes, persists the immutable
run and all rows, then returns those exact bytes. Preview/counter refreshes stay
read-only.

The workbook keeps existing metric and enabled-filter columns and adds:

- `Auto Status` (read-only);
- `User Status` (editable, populated only from the latest accepted review row
  for that Strategy ID, otherwise blank);
- `Auto Rank` (read-only);
- `User Rank` (editable, populated only from that review row, otherwise blank);
- `Analog Of ID` (editable, populated only from that review row, otherwise
  blank);
- `Comment` (editable, populated only from that review row, otherwise blank,
  at most 1000 characters).

The latest accepted review fields are looked up by Strategy ID, independently
of the selection run that produced the current result. They therefore survive a
new unreviewed ordinary selection and a `REPLACE` current-result change. The
automatic snapshot fields remain those of the new run. A workbook with blank
User Status cells is an incomplete review and must be completed before import;
automatic fields are never copied into user fields for an unseen strategy.

It also contains a very-hidden `_MRS_SELECTION_META` sheet with workbook schema
version, selection run ID, database instance ID, selection contract version and
export timestamp. Strategy ID is the import identity; hidden strategy name and
result ID remain integrity evidence, not fallback identities.

The XLSX contains every candidate exactly once. Sorting and filtering rows are
allowed. Formatting, style and sheet-visibility changes are ignored. Deleting,
duplicating or adding candidate rows is rejected.

## Import validation and precedence

Only `.xlsx` files produced by this contract are accepted. The whole workbook
is rejected without partial writes when any condition fails:

- metadata sheet, schema version, run ID or database instance mismatch;
- run is not the latest saved run for its Pair + Side and does not have the
  same request/config hashes and complete immutable `selection_results` rows
  as that latest run;
- the uploaded workbook SHA-256 was already imported successfully;
- workbook candidate set differs from the persisted snapshot;
- any current `strategies.current_result_id` differs from
  `result_id_at_selection`;
- an automatic decision field or hidden identity field was changed;
- an imported cell contains a formula;
- `User Status` is outside the closed vocabulary;
- a non-empty `User Rank` on `FINALIST` or `RESERVE` is not a unique positive
  integer;
- `ANALOG` has no different `Analog Of ID` in the same run;
- `ANALOG` points to a row outside the submitted row set or to itself;
- non-`ANALOG` row contains an analog target;
- comment exceeds 1000 characters.

`User Status` is required and non-empty on every candidate row. The user may
override any automatic or prefilled `REJECTED` status and may explicitly change
ordering. Thus changing a prefilled `REJECTED` row to `FINALIST`, `RESERVE` or
another valid status is the explicit un-reject operation and removes its tag on
successful import.
All automatic statuses may be changed, including `FILTERED` to `FINALIST`; the
automatic filter reason remains immutable audit evidence. Changing away from
`ANALOG` requires clearing `Analog Of ID`. Changing to `ANALOG` requires a
different target in the same run whose submitted status is `FINALIST` or
`RESERVE`. Therefore rejecting a representative requires either choosing a new
valid representative for its analog rows or changing those rows away from
`ANALOG`; import never silently promotes a replacement.

`User Rank` is optional; when present on `FINALIST` or `RESERVE` it is
authoritative and displayed before rows without a user rank. It must be a
positive integer unique within those selectable rows; gaps are accepted. A
rank on another status is normalized to blank on import. If an `ANALOG` target
exists in the submitted row set but its submitted status is not `FINALIST` or
`RESERVE`, that analog row is normalized to `FILTERED` and its target is
cleared. Missing, self, or out-of-rowset targets remain errors. Ranks from
different runs or sides are not comparable; any combined catalogue display
must retain side/run context.

After a successful import, effective status and analog target are the required
submitted User Status and its validated target from the latest review. Effective
rank is User Rank when present, otherwise Auto Rank. The latest accepted review
for a Strategy ID remains effective across later unreviewed ordinary runs and
current-result replacement; a strategy without an accepted review falls back to
its automatic decision (or prior `REJECTED` evidence). Automatic fields and
prior reviews remain immutable. The complete snapshot row set is mandatory, so
every strategy in the current run always has a defined effective status.

Top N constrains automatic assignment only. A deliberate review may produce
fewer, exactly N, or more than N effective `FINALIST` rows, including promotion
of a previously filtered or rejected candidate. The import response and A/B
catalogue report the resulting count but do not silently cap it.

Uploading exactly the same workbook again returns
`SELECTION_REVIEW_ALREADY_IMPORTED` and writes nothing. An edited workbook for
the still-latest run has a different content hash, appends a new review, and
the newest review becomes effective.

The pristine exported workbook may contain blank User Status cells and must be
completed before first import; its hash may equal
`selection_runs.workbook_sha256`, because duplicate detection applies only to
hashes already present in `selection_review_imports`.

If current result IDs changed, the error lists affected Strategy IDs. The safe
recovery is to export a new run and repeat non-durable edits. Existing accepted
review status, rank, analog target and comment are carried by Strategy ID into
the new workbook; strategies without an accepted review have blank User fields.
Automatic fields remain those from the new run.

## A/B “Только финалисты”

For a selected pair, the checkbox is enabled when at least one latest selection
run exists for that pair. The catalogue takes the latest run independently for
each available side and shows the union of strategies whose effective status is
`FINALIST`. A side without a saved run contributes no strategies. Imported user
 decisions apply by Strategy ID from the latest accepted review; strategies
 without a review use their latest automatic decision. A newer export for one
 Pair + Side supersedes only that side's automatic snapshot while prior review
 rows remain authoritative for strategies present in the new run.

If saved runs exist but their effective union contains no finalists, the panel
shows an explicit empty result; it does not disable or silently bypass the
filter.

## Concurrency and resource limits

Export persistence rechecks every current result ID inside its write
transaction before inserting the run/results. Review import performs the
latest-run check, current-result verification, review append and scoped tag
synchronisation inside one write transaction under the panel's single
Performance-DB writer lock. A concurrent export or result replacement therefore
cannot attach a review to a superseded run.

The server hashes and returns the exact workbook bytes associated with the run;
it never regenerates the response after persistence. This stage adds no
re-download endpoint and does not store workbook blobs.

Upload is limited to 20 MiB. Before openpyxl parsing, the server uses the
standard ZIP reader to reject archives with more than 256 entries or more than
100 MiB total declared uncompressed content. Parsing occurs from memory and
creates no persistent temporary rows or files. Failed validation writes
nothing.

The panel offers a folder picker for batch review import. It sends every `.xlsx`
file as an independent bounded request, oldest file first and newest file last,
then reports successful and failed files together. A bad file does not roll
back valid files for other Pair + Side runs; each file remains its own atomic
review import.

### Partial selection user-fields import

Card 6 (`Bulk RETEST current FINALIST`) has a separate folder import for the
partially completed selection workbooks used to restore existing operator
decisions. It has a dedicated API endpoint and does not relax the strict
`selection-review-import` or RETEST tag import contracts.

The partial importer accepts only `performance-v2-selection-review-v1`
workbooks with `_MRS_SELECTION_META`. It requires the workbook database
instance to match the open PerformanceDB and its selection run ID to exist in
that same database. It does not require the selection run to be latest and
does not require a complete candidate row set. In the `All candidates`
worksheet it requires the exact `ID`, `User Status`, and `User Rank` headers,
regardless of their column positions or the other columns present; a missing
required header is a schema error, not an implicit blank field. Every non-empty
data row is submitted, including rows where both user fields are blank. Duplicate
or malformed IDs, unknown strategies, and IDs outside the run snapshot reject
the whole file.

Each field is replaced independently. A blank `User Status` clears the prior
user status; a blank `User Rank` clears the prior user rank. A cleared status is
stored as SQL `NULL`, remains visible as a blank in exported workbooks, and
removes the strategy from the effective user-status cohort. Clearing a
`REJECTED` status also removes its current `REJECTED` tag. A blank-only file is
therefore a real import when it contains candidate rows: it validates database
identity, run and strategy membership, then appends clear decisions atomically.
Nonblank statuses are limited to `FINALIST`, `RESERVE`, and `REJECTED`. For
statuses other than `REJECTED`, a nonblank `User Rank` is accepted only for
`FINALIST` or a blank status; therefore a rank may be stored while status is
blank, and it does not participate in FINALIST uniqueness until that row is
later promoted to `FINALIST`. An explicit `REJECTED` ignores any workbook rank
and stores NULL. An accepted rank must be a positive integer, and ranks on
submitted `FINALIST` rows must be
unique among effective FINALIST reviews within the selection run after applying
the submitted rows. Missing FINALIST ranks are allowed and do not participate
in uniqueness comparison. A FINALIST with a blank rank clears its previous user rank.

Before appending the partial import, Card 6 also reconciles prior explicit
FINALIST decisions for the imported run's exact Pair + Side, across historical
selection runs (not only IDs in the current run snapshot). A prior FINALIST
remains FINALIST only when that strategy ID is submitted as FINALIST with a
non-empty rank. An explicit REJECTED remains REJECTED and its User Rank is
stored as SQL `NULL`, even if the workbook carries a stale rank. Every other
prior FINALIST—including a submitted RESERVE, blank status, rankless FINALIST,
or an ID absent from the partial workbook—is appended as RESERVE with User
Rank NULL. This reconciliation is restricted to the same Pair + Side; it does
not affect other strategies or directions. A synthesized RESERVE row for an
ID absent from the current run's `selection_results` changes its global
effective user decision only; it does not add a selection result or modify
strategy/result facts. Non-prior-finalist rows retain the existing
partial-field rules, except that an explicit REJECTED rank is cleared.

For non-prior-finalist rows, a non-empty User Rank is accepted only for
FINALIST or a blank status. Ranks on RESERVE are rejected with
`SELECTION_REVIEW_INVALID_RANK`; the prior-finalist and REJECTED cases above
normalize stale ranks to NULL without validating the stale cell's contents.
This includes a malformed or non-positive rank left beside a prior FINALIST
whose submitted status is RESERVE or blank; the replacement status/rank is
RESERVE/NULL. A malformed rank on a prior FINALIST submitted as FINALIST is
still rejected atomically; it is not silently converted to RESERVE. A blank
rank on a prior FINALIST submitted as FINALIST instead demotes it to
RESERVE/NULL. The pre-existing rule still permits a non-prior strategy to be
submitted as FINALIST without a rank; that row is effective FINALIST with a
NULL rank for this import, and is subject to the prior-finalist reconciliation
on a later import. The importer evaluates FINALIST rank uniqueness after
reconciliation, including any synthesized RESERVE rows. Only the latest
explicit non-NULL status per strategy determines whether it is a prior
FINALIST, so a previously rejected strategy is not reintroduced as RESERVE.

Each accepted file is one transaction in the existing
`selection_review_imports` and `selection_review_rows` ledger. It appends all
submitted rows, clear decisions, and synthesized RESERVE rows for absent prior
finalists; history is never overwritten or deleted. It stores rank only where
accepted above and synchronizes the REJECTED tag only for strategy IDs present
in the workbook: clear the prior REJECTED tag, then add it back only where the
submitted status is REJECTED. A synthesized RESERVE row for an omitted ID does
not change that ID's tags. It preserves prior comments and does not
modify RETEST, automatic fields, selection results, or strategy/result facts.
Since this import accepts no ANALOG status, it clears a prior analog target on
each row whose status it replaces. Workbook SHA-256
duplicate detection is shared with the existing review ledger; a repeated
workbook returns the typed `SELECTION_REVIEW_ALREADY_IMPORTED` error with no
writes. Any validation or write error rolls back the entire file.
For imports with candidate rows, `row_count` and `applied_count` include rows
whose fields are cleared; `finalist_count` describes submitted FINALIST rows.
`unchanged_count` is zero because blank fields are explicit clear instructions.

The card processes selected `.xlsx` files sequentially and displays each
filename, current file number, applied and unchanged counts, or the full
backend message and stable error code. It then reports imported, unchanged, and
failed file totals, refreshes the Performance v2 catalogue and current
FINALIST RETEST preview, and clears the folder input. Its controls use IDs
separate from card 7's RETEST folder import. Files are ordered by last-modified
time oldest to newest, then by name; the later accepted file supplies the
current values for each strategy and clears fields left blank in that file.

This nullable review status is introduced by PerformanceDB schema v10. The
v9-to-v10 migration drops only the `NOT NULL` constraint on
`selection_review_rows.user_status` in one transaction; existing review history
is preserved. Acceptance evidence for this partial path covers flexible
exact-header parsing, blank-field clearing, comment/analog handling, FINALIST
rank optionality/clearing and same-run uniqueness, rankless RESERVE/REJECTED
rows, atomic rollback and hash duplicates, run/database identity and strategy
membership validation, Panel v9-to-v10 schema preflight, the dedicated HTTP
route, and card 6 folder/log wiring.

## Errors

New API failures use stable codes:

- `SELECTION_REVIEW_INVALID_FILE`
- `SELECTION_REVIEW_SCHEMA_MISMATCH`
- `SELECTION_REVIEW_DATABASE_MISMATCH`
- `SELECTION_REVIEW_NOT_LATEST_RUN`
- `SELECTION_REVIEW_STALE_RESULTS`
- `SELECTION_REVIEW_ALREADY_IMPORTED`
- `SELECTION_REVIEW_ROWSET_MISMATCH`
- `SELECTION_REVIEW_AUTOMATIC_FIELDS_CHANGED`
- `SELECTION_REVIEW_INVALID_STATUS`
- `SELECTION_REVIEW_INVALID_RANK`
- `SELECTION_REVIEW_INVALID_ANALOG`

## Acceptance evidence

- Version-2 database migrates to version 3 transactionally without changing
  existing strategy/result facts; invalid catalogs still fail closed.
- The BABA-style exact analog group selects the best weighted representative,
  marks the other surviving members `ANALOG`, and does not let the default
  Close-MA near-tie stage discard them first.
- Ranking weights sum to `1.00`, include Worst Hold p95 at `0.12`, and produce
  deterministic results under input permutation.
- Automatic finalists never exceed configured Top N; fewer survivors are not
  padded, and FILTERED/ANALOG/prior-REJECTED rows consume no slots.
- Export persists exactly the workbook returned to the client and includes all
  candidates once.
- Valid edited status/rank imports atomically, synchronises `REJECTED`, and is
  visible to the A/B finalist filter.
- Manual review accepts effective finalist counts of `N-1`, `N` and `N+1`;
  Top N remains an automatic ceiling only.
- A review cannot target a rejected analog representative; selecting another
  representative and promoting a formerly filtered row are explicit and
  audited user operations.
- Foreign, stale, modified, incomplete and duplicate-row workbooks are rejected
  without changing review history or tags.
- A workbook opened and saved by Microsoft Excel with only permitted user
  edits imports successfully despite style/string normalisation.
- No tested path deletes a strategy, result, action, equity row or report fact.
