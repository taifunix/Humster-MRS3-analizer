# Progress

**Updated:** 2026-08-14
**Current branch:** `feat/strategy-performance-duckdb`
**Current feature:** Task 4 calculation-only DD5 from committed strategy-performance DuckDB

## Task 4 verified state

The new `performance-dd5` path imports a complete inbox, reads only committed
performance rows, calculates DD5 without creating retest strategies, exports
the existing workbook format plus a `CALCULATION_ONLY` manifest, persists
`dd5_runs` and `dd5_results` transactionally, and resumes cleanup only after
successful export. The legacy CSV `posttest` command remains unchanged.

Task 4 fix round 1 adds fresh DD5 persistence readback before artifact export
and strict panel validation of inbox manifest contracts, entry hashes and
paths. The panel test suite remains blocked by its pre-existing missing helper
import; no baseline repair was made.

## Verified repository baseline

- Root package: `src/mrs3`; tests: `tests`; project version: `0.7.0`.
- Latest complete repository suite: `485 passed, 2 skipped`
  (`.venv\\Scripts\\python.exe -m pytest -q --basetemp C:\\tmp\\humster-trusted-migration-full`, 2026-08-12).
  Both skips are Windows symlink-permission tests, not product failures.
- DuckDB v3/v4 importer pair is preserved in `programs/Обработчик HTML-DuckDB/`; v4 requires adjacent v3 codec.
- Local tester configuration exists outside Git as ignored `config.local.json`.
- Repository foundation, documentation model, importer/source preservation and Claude Code instructions are committed on `main`.

## Current verified external evidence

- The local v4 DuckDB was checked read-only: schema version `4`, compact payload schema and referential integrity are confirmed. Two malformed HTML reports are quarantined; the user accepted their exclusion from the current universe.
- The common UTC window is `[2026-07-15T00:00:00, 2026-08-06T00:00:00)`. The long CSV fully matches it; the short CSV contains one row with a shorter period and must be excluded by exact-period filtering.
- The v4 database contains `96,767` reports with the expected compact codecs. A source-HTML/Payload sample established the materializer formulas: wallet-change PnL, equity peak drawdown, and realised `closed`/`decreased` action metrics. The supplied CSV batch is a different grid and is not used as its reconciliation oracle; verification will use the source HTML referenced by DuckDB.
- The panel starts locally and answers HTTP 200. A one-strategy `tester-plan` succeeds when the JSON is supplied through a directory.
- Two approved real one-strategy smoke-runs completed. The second run completed the full runner lifecycle through `CSV_COMMITTED` and `COMPLETED` after the transient state-file lock fix; its result CSV is retained locally, while the configured report directory and both wizard logs were absent after cleanup. Only the root strategy JSON was changed; nested strategy folders were not touched.

## Current verified state

The sequential production workflow now carries an immutable analysis run into
READY-only strategy JSON generation. It emits EQUAL and INCOME variants without
rerunning selection, then pre-fills the legacy Test plan, tester-run and DD5
controls with the generated strategy directory. The bot remains manually
started after Test plan. Focused evidence: `65 passed` for
`tests/test_analysis_strategies.py tests/test_panel.py tests/test_strategy_json.py`.

DD5 post-test is calculation-only: `posttest.xlsx` and its CSV sheets rank real
tick-test results by `projected_pnl_dd5` and `projected_dd_pct`. The standard
workflow does not create scaled DD5 JSON or require a second DD5 tick-test.

Post-test now also applies one final sequential selection independently per
`symbol + side + timeframe`: adverse-only IQR filtering on holding p95 and
trade count, then DD5 PnL/capital Pareto, efficiency/first-shift Pareto, and a
conditional efficiency/Close-MA Pareto only when more than three candidates
remain in that timeframe. Focused evidence on 2026-08-13: `95 passed` in
`tests/test_posttest.py tests/test_cli.py tests/test_panel.py`; the current 55-row ONUSDT/LONG/2h sample evaluates as
`55 -> 47 -> 9 -> 1` on full-precision source values. The earlier read-only
check through the two-decimal display workbook showed seven stage-1 rows;
selection correctly uses the unrounded normalized values.

Post-test workbooks are decision-first: `00_Selection_Summary` reports one row
per Pair/Side/TF with all filter and Pareto counts, and `01_Finalists` contains
only final rows. Existing raw, normalized, comparison and holding sheets remain
after them. Preview evidence: `Output/posttest_layout_preview/posttest.xlsx`.

READY JSON generation now selects up to three 1ORD baselines independently per
`Pair + Side + TF`, not three globally when the panel timeframe scope is `All`.
For the current ONUSDT/LONG run, seven timeframes each expose three baselines;
the same 526 filtered multi-order structures therefore produce 1,073 JSON
(`526 * 2 + 21`).

Post-test also derives a diagnostic position-holding audit from each tester
result's immutable `trades_json`. A cycle ends only at `closed` with `Post
Size=0`; partial `decreased` actions never end it. The current 52-result LONG
batch produced 4,201 full cycles and zero holding exclusions in
`posttest_holding_long`.

The panel HTML import path retains the completed in-memory Preflight snapshot
and passes it to Start import, avoiding a second full HTML discovery and hash
pass. HTML parsing and preparation use a bounded multi-process queue, so
configured workers use separate CPU processes while one coordinator remains
the only DuckDB writer. The coordinator writes points, grids, reports, payloads
and replacement audit rows in DuckDB batches rather than per-report SQL calls.
For an authorized Preflight, Start reuses the already structural-validated
source and verifies only each written batch's metadata/payload references; it
does not revalidate or decode historical opaque payloads. Target and input
freshness checks, single-writer lock, evidence artifacts and no-delete rule
remain in force. The panel reports `PARSING`, `GROUPING`, `STAGING`, `WRITING`
and `PUBLISHING` with live counts. Focused evidence on 2026-08-12: `88 passed,
1 skipped` for `tests/test_duckdb_import.py tests/test_panel.py`; the skip is
the Windows symlink-permission test. The local import worker setting is 30 and
transaction batches are 2,000.

DUCKDB_DIRECT now derives its frozen required-shift list from reports that cover
the user-selected UTC window when no explicit shift list is supplied. The panel
therefore no longer exposes manual range fields for ordinary whole-surface
analysis and displays the resolved list after coverage preflight. Focused
evidence is `70 passed` for `tests/test_panel.py tests/test_duckdb_direct.py`.

Direct preflight now retains the maximal common MA-pair grid across its frozen
shifts, records excluded incomplete pairs, and deterministically resolves
overlapping active report windows by choosing the narrowest covering report.
This prevents an incomplete pair or overlapping historical period from
discarding an otherwise usable timeframe. Focused evidence is 72 passed for
tests/test_panel.py and tests/test_duckdb_direct.py.

CSV and DuckDB source-package builders, v2 source verification, package loading,
the selector event gate and the panel source-package controls are implemented.
ADR-0003 makes `TotalTrades`, `WinRate` and `ProfitFactor` the fail-closed
full-horizon source gate; PnL/DD remain mandatory
`NOT_COMPARABLE_WINDOW_SCOPE` diagnostics. The DuckDB materializer uses bounded
reads and retains the complete cycle/exclusion audit.

Task 1 (`270249f`) of the approved DuckDB storage/importer plan rejects lossy
fractional/non-finite integer loader values, accepts only Python `None` and
`pd.NA` wins/losses values as zero, and rejects empty normalized input before
downstream processing. Focused evidence: `52 passed` for `tests/test_loader.py` and
`33 passed` for `tests/test_source_packs.py` with the local `.venv` on
2026-08-11. The full suite reaches `316 passed` but has eight independent
runner-test failures because three required HTML fixtures are absent from Git;
those paths are outside Task 1 and were not changed. Independent re-review is
approved; Task 1 is complete.

Task 2 (`f61ffc8`) validates `point_event_count` in eligibility before its `int64` cast:
fractional, negative and non-finite numeric/text values are rejected, while
the legacy missing-column proxy remains `trades`. Focused evidence is
`32 passed` for `tests/test_eligibility.py`; the full suite reaches
`324 passed` and has the same eight runner-test fixture failures outside this
task. Independent Terra review approved the staged implementation; Task 2 is
complete.

Task 3 (`bf899d3`) requires exactly one normalized UTC `report_start`/`report_end` pair
for raw CSV input before eligibility. Pair-history output now asserts that
invariant instead of synthesizing min/max endpoints; the audit records the
coherent window and its derived `effective_days`. Focused evidence is
`62 passed` for `tests/test_loader.py tests/test_pipeline.py`; the full suite
reaches `326 passed` and has the same eight runner-test fixture failures
outside this task. Independent Terra review and re-review are approved; Task
3 is complete.

Task 4 (`869bd70`) aligns Plateau Library eligibility ID tuples with the annotated
standalone/depth predicates. Per-point event hashes are no longer presented as
a plateau event union: only source-package event mappings can produce the real
union, while legacy retains `N/A_LEGACY_PROXY` and raw real-event input without
mappings fails closed. Focused evidence is `20 passed` for
`tests/test_plateau.py tests/test_pipeline.py`; the full suite reaches
`328 passed` and has the same eight runner-test fixture failures outside this
task. Independent Terra review approved the staged implementation; Task 4 is
complete.

Task 5 (`c579801`) validates `base_rate_tf` object and scalar value types before Decimal
conversion. Null, non-object and non-scalar values now yield field-specific
`ValueError` with the original cause chained. Focused evidence is `15 passed`
for `tests/test_config.py`; the full suite reaches `334 passed` and has the
same eight runner-test fixture failures outside this task. Independent Terra
review and re-review are approved; Task 5 is complete.

Task 6 (`05369c2`) has a committed, independently Terra-reviewed compact-importer parity
contract: v3 exposes an immutable compact record, v4 workers return that same
contract while dynamically loading the adjacent v3 codec, and the `mrs3`
adapter decodes its compact payloads. Focused evidence is `4 passed` for
`tests/test_duckdb_events.py`. A copied-real-HTML smoke established v3/v4/mrs3
adapter parity `PASS` and a temporary v3 DuckDB import without writing the
production database: schema `v3`, scanned `1`, imported `1`, quarantined `0`,
raw actions `78`, equity samples `646`, and wallet changes `79`. Task 6 is
complete.

Task 7 (`e2bcde9`) adds a versioned source DuckDB schema v5 and an out-of-place v4
migration. The v5 contract persists normalization metadata, identifies a
point by normalized shift and MA pair plus its period, uses the time-grid hash
only as integrity evidence, and keeps active payload hashes separate from
replacement history. Migration rejects same paths and incompatible contracts
before target writes, validates schema constraints, row counts, references,
canonical keys, compact payloads and row/payload hashes, then atomically
publishes a separately validated target. Focused and relevant evidence is
`48 passed` for `tests/test_duckdb_source_schema.py tests/test_duckdb_events.py
tests/test_source_packs.py`; the full suite reaches `350 passed` with the same
eight absent-runner-fixture failures. Independent Terra review found and the
re-review verified the report-period/time-grid-bounds integrity check; Task 7
is complete.

Task 8 (`b7dc23e`) adds the recursive HTML-to-source-DuckDB import boundary. It snapshots
every input without moving, rewriting or deleting HTML; parses read-only in
parallel; groups canonical identities before a single-writer publication; and
records deterministic hashed manifest/checklist evidence. Insert, identical
skip, new period/shift, `A -> B -> A` replacement history, quarantine,
cancellation and safe retry are covered. Any same-batch canonical ambiguity
now stops the entire job before staging or DuckDB access, so neither an
existing target nor a new target can be published. The RED regression parsed
`1` report and quarantined `1` valid Windows CRLF input because it compared the
raw-byte snapshot SHA with the normalized codec SHA. The corrected contract
keeps raw input SHA as manifest and mutation evidence; normalized v3/v4 semantic
SHA drives report identity, `report_id`, deduplication and migration
compatibility. CRLF and LF inputs are identical under the semantic contract;
mutation and invalid UTF-8 fail closed. The related suite passes `66` tests.
Task 8 is complete.

Task 9 is complete (`feat: manage duckdb import from panel`). The panel manages
saved import settings, preflight, start, cancellation and out-of-place migration
activation. A per-resolved-source writer lock and path-free preflight token reject
concurrent or stale work; progress reports `parsed`, `inserted`, `replaced`,
`identical`, `ambiguous` and `quarantined`. Evidence, hashes and JSON are
revalidated before `COMMITTED`/`safe_to_delete=YES` or download; errors stay
path-free. The root suite is `87 passed, 1 skipped` (Windows symlink privilege),
and independent Terra review approved after three fix rounds.

Task 10 is complete (`224a039`): the analysis DuckDB schema has 13 focused
passing tests and an independent clean review.

Task 11 DUCKDB_DIRECT materialization and panel orchestration is complete.
The panel keeps materializer constants server-side, freezes the full preflight
snapshot behind a token, selects usable symbols by default and renders missing
coverage as noninteractive warnings. Build uses distinct read-only source and
writable analysis connections, revalidates the complete source contract before
publication, supports cancellation and publishes no raw source data. The relevant
suite passes `69` tests, and independent Terra review approved the implementation.

Task 12 atomic immutable publication is complete (`f72aae3`). All identity
inputs and order-independent source hashes are covered. New same-period/scope
surfaces form an immutable child chain; repeated older or newer inputs deduplicate
without rewriting parentage. Every publication table is written in one
transaction with rollback, without a source connection or raw-data copy. Current
raw reproducibility is derived from persisted source hashes and the supplied
active-hash set. The focused suite passes `29` tests, and independent Terra
review approved after the fix rounds.

Task 13 common-pipeline adaptation and persistent lineage is complete. Analysis
schema v2 transactionally migrates v1 candidates to explicit multi-plateau
membership without changing published surfaces or points. Published surfaces
are loaded from the analysis DB only; explicit listing dates are scoped and
hashed into analysis-run identity. Runs, plateau members, 2–4ORD candidates and
explicit cross-period lineage are atomic and deterministic; repeated comparison
targets add idempotent lineage without copying points. Focused verification is
`115 passed`; the full suite is `454 passed, 2 skipped`, with both skips limited
to unavailable Windows symlink privilege. Independent Terra review approved
after two fix rounds.

Task 14 library, statistics and deterministic exports is complete. Analysis
schema v3 stores immutable per-run point/eligibility/plateau/READY facts and
atomically migrates v1/v2 history as explicitly unavailable rather than
inventing eligibility counts. The panel exposes an explicit writable v3
initialization/migration action; library, compare and export then remain
read-only, while re-run analysis reuses the same immutable surface without
opening the source DB. Refine requires an explicit existing parent surface.
CSV/XLSX exports are byte-stable generated artifacts with a hashed manifest,
not canonical storage. Focused evidence is `104 passed`; independent Terra
review approved after one fix round.

Task 15 final verification is complete. The copied-real-data smoke imported
one report into a small v4 database, migrated it out-of-place to a valid v5
database, append-imported one more report with `COMMITTED`,
`safe_to_delete=YES` and zero quarantine, then revalidated `2` reports, `2`
points, `2` grids and `2` payloads. DUCKDB_DIRECT published one immutable point
with `458` trading events into analysis schema v3. The complete repository
suite passes `467` tests with two Windows symlink-permission skips. Smoke data
and generated databases remain outside Git; the optional overlay remains
unimplemented.

Read-only v2 materialization then completed: `96,767` reports, `8,050`
coverage-accepted points, `88,717` coverage-rejected reports and `4,932,780`
included cycles. The package has `source_summary_status=VERIFIED` and
`window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE`. An exploratory real LONG
selector run then completed after the package-side and UTC-normalization
integration slice:
`3,730` normalized points, all with 24-346 events, but `0` economic/event-
eligible points, `0` plateaus and `0` READY structures under the current
economic thresholds. This is a result, not a source-verification failure.
It is not directly comparable to the separately supplied LONG CSV grid: that CSV has
`36,288` points on the same window and produces `3,432` economic-pass points
under the same thresholds, whereas the imported DuckDB LONG slice has `3,730`
points. Core v0.7 will therefore publish direct DuckDB surfaces into a separate
analysis DuckDB. A possible CSV-base overlay is isolated in the
[optional/deferred specification](docs/specs/2026-08-11-v07-optional-csv-duckdb-overlay.md)
and does not block the core delivery.

Trusted-v4 migration performance is implemented and independently reviewed.
It streams v4 metadata and exact matching payload batches through one read-only
transaction; detached workers prepare records while one DuckDB writer commits
them. Raw v4 report payloads are not decoded, and the staged v5 database is
validated structurally before an atomic no-replace publish. Focused evidence:
`67 passed` for `tests/test_duckdb_source_schema.py tests/test_panel.py`.
The production trusted-v4 migration is complete. With `workers=8` and
`transaction_batch_size=250`, it created the separately published v5 archive
with structural validation `True`: `96,767` reports, `96,767` payloads,
`96,767` point rows and `96,527` grids. Its target SHA-256 is
`09ecfea75391602398b87f0d1523200280928b0e8b0080e272a65abc90d8b661`.
The trusted contract deliberately did not run a full payload-decode pass.

Direct DuckDB preflight now uses the same structural v5 validation rather than
the full payload-decoding validator. It still reruns before build to reject a
changed source snapshot; a focused regression forbids action/wallet decoders
during preflight. Focused evidence: `18 passed` for `tests/test_duckdb_direct.py`.

## Next required work

The core specification and its 16-task plan (Task 0 plus Tasks 1–15) are
approved. Task 0 is represented by the existing `d76b985` package-side/UTC
slice; Tasks 1–15 are complete.

1. Use the panel to initialize the chosen production analysis DuckDB, publish
   required real surfaces and run their plateau analyses.
2. Continue the READY-candidate → tester → DD5 product loop; source metrics
   remain diagnostic until those real tests exist.
3. Keep CSV/DuckDB overlay deferred unless the user activates its separate ТЗ.

## Blockers

- CSV/DuckDB overlay is explicitly deferred and no economic threshold changes
  are implied.

The HTML import preflight UX follow-up is complete: snapshots run with the
configured worker pool, the panel exposes path-free discovered/snapshotted and
processed/total-byte progress, and duplicate Preflight/Start actions are
disabled while the background preflight is active. Focused evidence is
`81 passed, 1 skipped` for `tests/test_duckdb_import.py tests/test_panel.py`.
The full repository suite then passed `485` tests with the same two Windows
symlink-permission skips.

Configuration decision: Preflight and import intentionally use the same
persisted `workers` value (`15` in the current local configuration). A separate
`preflight_workers` setting is unnecessary and is not planned.

Real-event DUCKDB_DIRECT and interactive Phase 2 filtering are implemented
under `docs/specs/v07-event-filter-and-shortlist.md` section 24. Direct
materialization now reconstructs closed cycles from compact actions and stores
exact event memberships in analysis schema v4; `PointEventCount` is the unique
membership count and is no longer copied from `TotalTrades`. Existing v3
analysis databases migrate in place without rewriting legacy surfaces. New
builds use `event_mode=real_independent_events`, materializer
`v2-real-events`, and a distinct surface identity.

The panel exposes independent structural-group criteria for per-order Source
PnL, per-order PnL/DD, per-order CloseSupport and PointEventCount. Filtering is
non-destructive, JSON generation rejects selected deferred candidates, and the
same criteria can be exported to deterministic XLS with standalone criterion
sheets plus `DEFERRED_COMBINED`. Source PnL is never summed or averaged by the
filter. Focused verification passed `165` tests across direct/storage/pipeline,
source packages, shortlist/XLS, strategy generation, panel and analysis
exports. Independent LUNA review findings were fixed: strategy generation now
fails the entire request when any selected candidate is not READY, shortlist
validates stored point-event counts and rejects unknown event modes, and XLS
records the analysis algorithm version while retaining Decimal values until
the final Excel serialization boundary. Direct v3-to-v4 migration and
cross-point event-ID identity have dedicated acceptance tests. Fresh focused
verification passed `169` tests. The full suite produced `504 passed, 2 skipped, 8 failed`; all eight
failures are pre-existing missing-fixture errors because this clone lacks
`tests/fixtures/tester_wizard.html`, `tester_table.html`, and the referenced
ADMSTOCK HTML report.

The representative defect is fixed under algorithm version
`0.7-representative-v2`: each Plateau/Pair/Side/TF/CommonCloseMA contributes
exactly one point before structures are combined. Corrected immutable run
`6a65684feaf1e5e928babd5590a508378dfa9b4e130ef311a36853ad2ac715b0` on
surface `8e4f5c4147b0ee2dee38f8d2415e896cc14460b997d7577cc1e1adf226573226`
is `COMMITTED`, has 1715 READY candidates and zero representative violations
across 196 used Plateau/TF/CloseMA groups. Phase 2 results are: Source PnL
filters remain available independently; all four together produce 526/1189
READY/deferred across 58 structural comparison groups. The panel returns only
seven Pair/TF aggregate rows (1363-byte real response) with 2/3/4-order and
READY/DEFERRED/ALL counts; candidate descriptions and checkboxes are removed.
JSON generation resolves all READY IDs in the selected Pair/TF scope on the
server. XLS remains the complete candidate-level audit.

Next operational step: refresh the panel, select the corrected run, inspect
the Phase 2 criteria, then export the audit XLS or generate JSON only from the
visible READY selection. No HTML re-import or surface rebuild is required.

Deferred UI/report task: redesign the human-readable format of
`phase2_filter_audit.xlsx` after its desired columns, sheet layout and styling
are agreed; do not change filter semantics or exact audit values as part of
that presentation work.

Local persistent analyzer data is stored under the ignored project `data/`
tree (`databases`, `import_audit`, `legacy`) rather than mixed with bot reports.
Source HTML and future tester reports remain under the bot-owned
`tester/report` tree and are neither moved nor duplicated.

The final DD5 comparison report now includes the immutable per-order
`shift_bp_vector`. Its Excel-facing metrics and lot vectors are rounded to two
decimals, while normalized calculations retain their full precision for ranking.

The final report also provides scoped alternative Pareto flags for capital,
holding p95, close MA, first entry shift and their combined objective. They are
separate diagnostic filters and do not replace the primary DD5 rank. Pareto
compares all 1–4ORD strategies inside the same Pair/Side/TF; order count is not
a comparison boundary.

JSON generation now selects three 1ORD structures in the selected Pair/TF
scope before tester-run. Their selection order is DD5 PnL, lower raw DD,
PointEventCount, first shift and point ID; DD5 consumes their real tester
results alongside 2–4ORD strategies and does not add source baselines late.

The first two-strategy tester pilot exposed a runner readiness defect: the bot
also listed a protected nested `Bybit/AAOIUSDT` strategy, while the runner
incorrectly required exact equality with the installed root batch. Readiness
now requires the installed names as a `TEST`-state subset; unrelated protected
rows are ignored and are never submitted or monitored. A failure after bot
startup now attempts a verified stop before recording `FAILED`, while keeping
reports and wizard logs for diagnosis. Focused evidence: `5 passed`; the broader
runner selection produced `37 passed, 1 skipped, 4 failed`, with all four
failures caused by the already documented missing ADMSTOCK HTML fixture.

Tester-run panel output now presents concise English lifecycle/progress/report
messages and suppresses unreadable localization output. The captured stdout is
preserved beside the tester CSV as `<results-stem>.raw.log` and exposed as a
downloadable artifact. Focused evidence: `2 passed` for the panel log behavior.

The runner now uses a controlled tester window rather than submitting an entire
directory at once. Local runner defaults are `max_parallel_submissions=10` and
`max_strategy_attempts=4`. A slot is refilled only after its Result row,
matching wizard entry and stable HTML report are verified. A strategy observed
as RUNNING and later returned to TEST is resubmitted automatically; exhausted
attempts fail with the exact names and preserve tester artifacts. Progress
reports unique submissions and a retry counter. Focused evidence: `17 passed`
for runner monitor/config and panel retry rendering. The broader runner tests
remain blocked by absent untracked tester HTML fixtures.

The controlled runner also recovers a tester `RESULT` row that has its matching
wizard entry but no report HTML: after two consecutive missing-report polls it
uses the same four-attempt retry budget. This prevents a completed-count stall
with a non-empty Result count. Focused runner monitor/config/panel evidence:
`18 passed`.

The 1073-strategy batch then exposed a second runner defect: a transient
tester `HTTP 500` from `/htmx/tester/strategies-table` stopped the whole batch
after 13 completed results. The runner now treats transient HTTP failures and
startup/stall timeouts as process-recoverable: it stops the bot, validates and
retains current one-strategy wizard/HTML results, then restarts only the
remaining names. `max_bot_restarts` is configured locally as `30`; it does not
reinstall JSON or clear reports between these restarts. Progress is cumulative
and includes `bot_restart_count`. A transient bot startup failure is also
recoverable; deterministic HTTP `4xx` responses fail immediately without a
restart. Config/workflow evidence: `52 passed, 1
skipped` (two fixture-dependent workflow tests excluded because this clone
lacks the untracked ADMSTOCK HTML fixture); `compileall` and `git diff --check`
passed. The panel was restarted after this verification. Next operational step:
run the preserved failed 1073 strategy plan; the runner will validate existing
reports and continue only the outstanding names.

Deferred panel task: after `tester-plan`, show a final batch summary with
current JSON, verified reusable results, remaining retests and a reason if
resume is unavailable. Before `tester-run`, show the exact prepared count for
the clean or resume path.

Tester launch requests are now spaced by the local
`submission_delay_seconds=0.2` setting, including retries, while retaining the
10-strategy window. The panel reads the structured `tester-plan` response and
shows total JSON, reusable results and the exact prepared count in the run
button. Focused monitor/config/panel evidence: `19 passed`.

Resume now snapshots the already reconciled wizard entries before starting the
tester and merges them with the post-run wizard log. This protects valid prior
results if the tester rewrites its shared `wizard_result.json` while retesting
only missing strategies. On the current 52-strategy LONG batch, 52 HTML files
exist but only 17 wizard entries remain after the tester reported a file-lock
write error; `tester-plan` correctly resumes those 17 and prepares only 35.
Focused workflow evidence: `2 passed`.

Tester HTML collision recovery is now intentionally optimistic for the closed
`hb_c.exe` writer. Analysis of the preserved batch found 654 one-strategy wizard
entries but only two duplicate `chartUrl` groups (four affected strategies), so
serializing every same-timeframe test would be disproportionate. The runner
therefore keeps the full configured parallel window; its existing
Result/wizard/stable/matching-HTML validation rejects a collided report. Only
the affected group is restarted in a sequential repair lane, and only those
repair HTML files are copied into a temporary immutable result directory for
final reconciliation. Focused evidence is `15 passed` for controlled-monitor
and workflow collision scheduling/recovery. The developer-side permanent fix
is to include the already generated `runId` in both report filename and
`chartUrl`.

The runner now supports bounded `settings_strategy` batches. The local
`strategy_batch_size` is `50`: it stops the bot after each verified chunk,
replaces only runner-installed root JSON files, and then starts the bot for the
next chunk. Focused runner evidence: `24 passed, 1 skipped` in
`tests/runner/test_workflow.py tests/runner/test_files.py tests/runner/test_config.py`.

Duplicate tester-run prevention is now output-scoped: a live runner owns an
exclusive `.runner.lock` beside its result CSV. A duplicate launch fails before
it changes state or invokes the bot; a stale lock from a dead PID is reclaimed.
Focused evidence: `38 passed, 1 skipped` for runner workflow, monitor, files
and config tests.

The temporary HTML collision workaround now includes a 50 ms background
snapshot collector. It waits for a stable report file, derives the strategy
name from its HTML payload, saves a unique immutable copy, and passes that copy
to reconciliation. This is a pragmatic mitigation for the closed tester's
shared report filename, not a replacement for the required `runId` report
contract. Focused runner evidence: `58 passed, 1 skipped`; two legacy workflow
tests remain blocked by the absent untracked ADMSTOCK HTML fixture.

Before the next resumed tester batch, the active report directory was archived
in place: `737` files were renamed from `*.html` to `*.html.saved`; a local
sidecar retained `657` validated report paths. A fresh exact plan confirms
`1073` expected, `657` reusable and `416` remaining, while the original report
filenames are free for the closed tester to write again.

Manual verification on 2026-08-13 confirmed that the closed tester completes
ten simultaneous button presses for one `ONUSDT/5m` batch without error. The
runner no longer serializes its initial window by `symbol + timeframe`: local
`max_parallel_submissions` is `10`, and the snapshot collector preserves
individual reports. The sequential collision lane is retained only after an
actual `BatchHtmlCollision` is detected. Focused workflow and snapshot tests
pass; the panel is intentionally stopped for manual tester inspection.

The next root cause was confirmed from the live tester table: a strategy can
show a stale `Result` link while its newer run still exposes a progress bar.
The HTTP parser now gives live progress precedence, so that row remains
`RUNNING` and is never submitted a second time. This matches the tester UI:
the runner performs the same single-strategy wizard GET and wizard-run POST as
the `Test` and `Run` buttons. Focused evidence: `1 passed` HTTP regression and
`5 passed` monitor/workflow regressions. The panel remains available; the
interrupted tester-run and its bot were stopped before this change.

False retries after a real Result were then traced to reuse of
`report_stability_polls` as a two-second retry deadline. The runner now uses a
separate local `result_report_grace_seconds=15`: it waits for the matching HTML
to appear after Result, while a genuinely missing report still consumes a retry
only after that deadline. Focused evidence: `2 passed` grace-period monitor
tests, `10 passed` config tests and `1 passed` HTTP state regression. The
active tester-run and its bot were stopped before this change; the panel stays
available for a clean resume.

The 2026-08-14 end-to-end runner audit replaced the interrupted-run recovery
contract. `tester-run` publishes progress and stops the verified bot before
hydrating resume evidence; collector threads close on every exception; captured
snapshots are merged before process restart; stale pre-launch `RESULT` rows are
rejected; and the four-attempt limit is cumulative across bot restarts. Progress
retains retry counts/reasons, grouped restart reasons and the last restart error.
`stop_bot` still reaches verified terminate/kill fallback when shutdown client
creation or close fails, and interrupted root-JSON installation rolls back
before propagating `KeyboardInterrupt`.

The in-place `*.html -> *.html.saved` archiver was removed. Existing saved
files remain read-only legacy evidence for the interrupted batch, while new
evidence uses immutable per-strategy snapshots outside the tester report
directory. Automatic HTML-only result synthesis was removed: normal resume
requires persisted/current wizard evidence plus matching HTML. A one-time,
explicit `legacy_html_only` migration validated all `803/803` historical HTML
paths and wrote an audited sidecar; unchanged evidence is subsequently checked
by path/size/mtime in about two seconds instead of reparsing all HTML.

The immutable source batch lost by using the bot staging directory as its source
was regenerated deterministically from analysis run
`6a65684feaf1e5e928babd5590a508378dfa9b4e130ef311a36853ad2ac715b0` with all
four Phase-2 criteria. The restored source is
`data/tester_batches/ONUSDT_LONG_all_tf_6a65684feaf1/strategies`; all `1073`
names and hashes exactly match the interrupted state. Read-only `tester-plan`
reports `803` verified reusable results and `270` remaining. Local runtime
settings are `strategy_batch_size=250`, `max_parallel_submissions=35`,
`max_strategy_attempts=4`, and `max_bot_restarts=30`. The tester was not started
during this audit.

Final 2026-08-14 handoff: SHA-256 evidence was materialized for all `803`
reusable reports and the read-only plan was rechecked as `1073` expected,
`803` ready and `270` remaining. Focused runner/panel verification is
`151 passed, 1 skipped, 2 deselected`. Continue from
`docs/HANDOFF_2026-08-14_TESTER_RUNNER_AUDIT.md`; do not launch the tester
without first reproducing the same plan counts.

Current session handoff for moving the long HTML import to another machine:
[remote import handoff](docs/HANDOFF_2026-08-12_REMOTE_IMPORT.md).

## Queued module hook

**Анализатор Портфеля:** [спецификация v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) передана отдельной команде. Начинать с проверки входных контрактов; до trade timestamps и определённого limiter допускается только Layer A, без симулятора и рекомендаций по сету.

## Update protocol

Replace this file’s verified-state and next-action sections whenever a commit changes the operational state. Keep only the present blocker set; durable decisions belong in ADRs and feature requirements belong in specs.
