# MRS3 Current Status

Last updated: 2026-10-09

This file is a current-status snapshot, not a session log. Earlier progress notes remain in Git history; feature contracts and detailed evidence belong in the linked specs, reports, and plans.

Use [PRD.md](PRD.md) for the feature registry and [docs/README.md](docs/README.md)
for the documentation map. Read the linked contract only for the active task;
do not treat this file as a replacement for a feature specification.

## Portfolio MILP composition selection (2026-10-10)

Stage 1 no longer fails when the finalist product exceeds
`search.max_enumerated_combinations`. Above the bound, one MILP per profile
over the union of slot options ranks up to `2 × max_candidates` distinct
portfolios; each is then evaluated by the unchanged exact path. The form
shows the live combination count, the bound and the mode. Contract:
[UI spec amendment 2026-10-10](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md);
decision: [ADR-0066](docs/decisions/0066-portfolio-milp-composition-selection.md).
The adapter progress sink lacked `import time`, which silently dropped every
adapter-level progress event. That is fixed in the same change. The change
also carries the 2026-10-09 lightweight readiness and combination-limit detail
hunks.

Diagnosis evidence: one composition × one profile costs about 49 s with 30
workers (106 s single-threaded). Only bootstrap is parallel, and it spawns a
process pool per call. On the real 117-finalist snapshot, facts took 102 s
(minute files are reused, about 0.2 s per symbol) and layer preparation 58 s.
Each MILP solve took 9–130 s, rising as cuts accumulate; 20 solves took 19 min.
The earlier 32-minute facts stage did not reproduce. Measured overheads:
per-request SSL context creation in the market snapshot, and a full 14 MB
`.panel-jobs.json` rewrite on each progress/journal sync.

Verification: composition selection 10 passed; adapter + selection 365
passed. A broad run of portfolio/Panel modules gave 1143 passed and 1 skipped.
Its 34 failures are known and pre-existing: 32 `ab_decline_cap_pct` /
11-of-12-column fixtures, the maintenance copy expectation, and the Windows
Node command-line length. `node --check` and `git diff --check` passed.
Independent review: one MAJOR (silent profile loss on a selector time-out) and
the MINOR findings were fixed; the re-review passed. Adapter progress now
reaches the Panel for the first time. The Panel reporter marks the total
inconsistent after the first substage switch, so later progress shows no
ETA. Next step: restart Panel to load the code, then rerun the
full-universe Campaign. No PerformanceDB or
tester was used.

## Portfolio own-history drawdown cap (2026-10-10)

Stage 1 now caps each member by its own worst drawdown: `x_i·d_i ≤ max_dd·B`.
Here `d_i` is the member's per-unit drawdown over its full own history, not
only the common window, and `max_dd` is the profile DD limit. This is a
per-member concentration cap, not a sum of DDs. The common-path DD, CDaR and
bootstrap stay as they were. The cap is enforced in the LP/MILP, in the
fixed-bank LPs and in the exact bank. Decision:
[ADR-0069](docs/decisions/0069-portfolio-own-history-drawdown-cap.md);
contract: the own-history amendment in the
[weighted-search spec](docs/specs/2026-09-14-portfolio-optimizer-weighted-search.md).
The Stage 1 summary, XLSX and `/results` show `own_history_dd_bank_usdt`;
the web table column waits for the parallel `app.js` work.

Why: in Stage 2 tester runs, the 9-pair candidate (bank 72) reached a 61%
drawdown. Under the new rule its TSEM/MSTU sizes would need a bank of about
730.

Real evidence: the frozen 61-pair Campaign, rerun offline with 30 workers,
gave PASS in 19 min.

| Level | Members | Bank | P30 | Own-history bank |
| --- | --- | --- | --- | --- |
| top | 61 | 715 | 1641 | 390 |
| bottom | 61 | 72 | 328 | 58 |

Before the cap the same ladder went from 61 members down to 9, with P30
1643 → 360. Lower levels now shrink the whole portfolio instead of
concentrating it. The margin bank binds at every level.

Verification: the own-history, weighted-search, adapter, optimizer and
panel portfolio suites give 933 passed, 1 skipped. `test_portfolio_input.py`
has 34 failures, all from the parallel v11 schema changes. Independent review
passed after one fix round.

## Stage 2 strategy files in tester format (2026-10-10)

Stage 2 strategy and tester-config JSON are now indented, with keys in the
template order. `balance_percentage_*` round up to a whole percent (0 stays
0). `lot_x` rounds to 0.01, with the remainder on the last order; a
non-positive last order fails closed. `MakerFee` is 0 for Bybit-only
batches. Contract: the 2026-10-10 tester-rendering amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).

Accepted by the operator: rounding percentages up enlarges positions (small
`q` the most) and can exceed the liquidity cap C.

Follow-up (operator rules): Stage 2 tester config uses `UpdateData=true`.
Report settings live only inside `report`; the shared MRS3 tester template
and the fast strategy-test writer no longer put them at the root. Both local
exports were updated.

Evidence: portfolios 01 (61 strategies, bank 716) and 10 (9 strategies,
bank 72) of `campaign-df84e07d885f4fb8a7b5d9c97a05b5d2` were re-exported
locally. All 70 files were checked. No tester run was started; the Panel
backend needs a restart to use the new rendering.

Verification: `tests/test_panel_portfolio.py` 297 passed, 1 skipped.
Independent review passed.

## Pair table controls and Stage 2 portfolio selection (2026-10-10)

The pair table gets a separate `История` column (bold `≈ N д` and the date
range), a `Выбрать N финалистов` button with an N field, and a display sort:
A–Z, shortest history first, or most finalists first.

The Stage 2 card lists every committed Stage 1 portfolio with its main facts
and a checkbox. `Отправить на тест` sends only the checked ones, in artifact
order. `/results` exposes the rows, and submission accepts `candidate_ids`;
each distinct selection is its own batch.

Contract: the 2026-10-10 pair-table/Stage 2 amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md), which also
lists where Stage 2 writes on disk. Live: `/results` for the 61-pair Campaign
returns 10 portfolio rows. No tester run was started; the operator will start
it from the Panel.

Verification: static UI and panel portfolio suites pass, except the 2 known
pre-existing failures. Independent review passed.

## Portfolio pair history and selection summary (2026-10-10)

The Stage 1 form now shows, for every pair, the history of the finalists its
current limits would select. Under the table it shows the totals: pairs,
finalists, the common history of the whole selection, and a rough runtime
estimate. Readiness exposes `finalist_history` (day-aligned periods in rank
order, with no series reads); a history failure never blocks Stage 1.
Contract: the 2026-10-10 pair-history amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).

Live check: all 61 pairs and 117 finalists give ≈ 21 days of common history
(16.09–07.10) and ≈ 17 min, against 16 min measured. The shortest histories,
21 days each, are HORIZON, MEITU and SENSETIME.

The "minimum risk at target P30" search mode is recorded as a planned option
in the [optimizer spec](docs/specs/2026-09-05-portfolio-optimizer.md), Phase 4.
It has only a prototype so far; the product keeps the bank-ladder frontier as
"maximum P30 under a bounded risk/bank".

Stage 2 dry run (read-only, no tester start): for all 10 candidates of
`campaign-df84e07d885f4fb8a7b5d9c97a05b5d2`, `_stage2_material` built a tester
config and strategy JSONs (61 → 9 strategies, 16.09–06.10, `InitialBalance` =
required bank 716 → 72). Observation: candidate 10 has `InitialBalance` 72 while
its strategies' `max_balance` is 72.17 (whole-USDT bank vs exact sizing). The
real tester batch still needs explicit user authorization.

Verification: Panel portfolio and static UI suites pass, except the 2 known
pre-existing failures (maintenance copy, Windows `node -e` length).
Independent review passed.

## Portfolio bank-ladder frontier (2026-10-10)

Stage 1 now returns, per profile, up to `max_candidates` genuinely different
portfolios. Each is the MILP-optimal choice of finalists and weights for one
bank level across all compositions, then exactly evaluated. This replaces
both the exhaustive enumeration and the top-K MILP ranking, which produced
near-copies. Decision: [ADR-0067](docs/decisions/0067-portfolio-bank-ladder-frontier.md);
contract: the 2026-10-10 frontier amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).

The same change fixes three issues found on real data:
- binding bank ceilings failed exact LPs with `BANK_UNAVAILABLE`;
- a MILP on the default HiGHS thread count broke every later LP in the
  process with "Status 0: Not Set";
- scaled zero weights (`0E-90`) failed payloads.

Real evidence: the full universe (61 pairs, 117 finalists, 4·10^14
compositions, AGGRESSIVE, bank 1600) SUCCEEDED in 16 min with 10 monotone
candidates. Margin bank went 715 → 71, P30 1643 → 360, members 61 → 9.

Walk-forward check (14 days in-sample, 7 out): returns hold out of sample. In
this data the margin constraint binds rather than drawdown, so lower levels
concentrate. Their out-of-sample P30/maxDD (57 → 12) is worse than
proportionally scaling the full portfolio (60). The ladder is therefore
"maximum P30 per margin budget", not a minimum-risk frontier. A
min-CDaR-at-target-P30 mode was prototyped and kept diversification, with
out-of-sample P30/maxDD 38–61. It is recorded as a future option in the
optimizer spec (Phase 4).

Verification: portfolio suites 593 passed; independent review and re-review
passed.

## Portfolio Stage 1 throughput and hang fix (2026-10-10)

Three reviewed infrastructure changes. Contracts: the 2026-10-10 amendments in
the [weighted-search spec](docs/specs/2026-09-14-portfolio-optimizer-weighted-search.md)
and the [UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).

- **Job journal.** The frozen Campaign in a job runtime is shared by reference
  and never serialized into `.panel-jobs.json`. Measured: 2.26 s → 0.16 s per
  progress update with the real 14 MB journal and a 16 MB Campaign.
- **Market reference.** One keep-alive HTTP client and 10 requests/sec instead
  of 2/sec, with retry and cooldown unchanged. Measured: 14.9 s → 6.0 s on 10
  symbols. The snapshot is taken once per Campaign per unique pair; it is not
  repeated per strategy or portfolio.
- **Bootstrap pool.** The worker context goes through a private temp file in
  `%TEMP%\mrs3-process-context`, not the child start-up pipe. A child dying at
  start-up now raises `BrokenProcessPool`; the pool is replaced and the
  unfinished tasks are retried once. Before, the parent blocked forever; a
  2026-10-09 Panel job hung for over an hour this way. One pool serves the
  whole Campaign and is owned by the job thread. Measured: 49 s → about 12 s
  per composition.

Verification: focused suites pass (panel jobs 49, market snapshot 36, process
worker 6). The independent review passed, and its MINOR findings (thread
ownership, cache release, temp-file sweep) were fixed with tests.

## Portfolio executable identity on real lot-model data (2026-10-10)

Every real lot-model Campaign failed post-search with
`WEIGHTED_EXECUTABLE_IDENTITY_INVALID`: the Decimal `liquidity_v25_usdt` and
`liquidity_a15` evidence was not JSON serializable. A real-data probe found no
other Decimal path. Both fields are now bound as exact, finite canonical text;
non-finite values fail closed. Contract: the 2026-10-10 identity amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md). Verification:
adapter and selection suites, 366 passed; a restarted Panel run of the 61-pair
5×2 Campaign passed composition 0 and continued. Independent review passed.
The earlier run of that Campaign hung in a Windows multiprocessing spawn: a
bootstrap child died at start-up while the parent was blocked writing to its
pipe. It needed a Panel restart. The bootstrap pool spawn path is unchanged,
and this remains an open reliability risk.

Real-data evidence: Campaign `campaign-00b399c7100c42bf8692075d4ab9b72c` covered
61 pairs, 5 of them with two finalists (32 compositions, exhaustive
path), AGGRESSIVE profile, bank 1600. It SUCCEEDED in 78 min (about 2.4 min
per composition) with 10 candidates. Candidate 1: required bank 719 USDT,
source-proxy P30 1502 USDT/30d, historical DD 30%; candidates 2–10 differ only
in the SOXX/SPCH/VST/XLK finalist choice (P30 1485–1502). These are
pretest proxies, not tick-tested MRS3 results.

## Panel job admission reliability (2026-10-09)

Panel job submission now persists `QUEUED` before worker dispatch, removes an
unsaved admission from memory, retries only transient Windows journal replace
errors (5/32/33) up to five attempts, and returns `JOB_PERSISTENCE_FAILED` with
HTTP 503 through direct routes and portfolio service wrappers. Contract:
[SINGLE_MODE report collection](docs/specs/2026-09-28-single-mode-report-collection.md);
safety decision: [ADR-0063](docs/decisions/0063-panel-job-admission-on-journal-persistence.md).

Verification: 9 focused journal/portfolio/report-collection regressions passed;
the related combined run had 467 passed, 1 skipped, and 1 pre-existing fixture
failure. The same failure was reproduced from a clean `HEAD` archive: the
portfolio test helper supplies 11 values to the 12-column `selection_results`
table. `git diff --check` passed. Live Panel restart and status check are still
complete: Panel API on port 8766 responds, the stale in-memory QUEUED job is
gone, and no `hb_c.exe` process is running. No tester run or PerformanceDB was
used.

## PerformanceDB maintenance

The maintenance card is implemented in the Strategy and DD5 tab. It supports pair selection, strategy-count previews, Rejected retirement, full pair deletion, progress reporting, elapsed time, and actionable database errors. `Удалить Rejected` now physically removes per-strategy facts, window/equity caches, tags, rejection sources, and selection rows, compacts the retained result to interval/provenance identity, then atomically marks the typed identity as `DISCARDED`; only strategy settings/orders and that compact tombstone remain for deduplication. The code targets schema v10 and adds no cleanup markers, deletion timestamps, backups, or maintenance tables. Contract: [PerformanceDB maintenance](docs/specs/2026-10-06-performance-db-maintenance.md); retirement decision: [ADR-0061](docs/decisions/0061-performance-db-rejected-retirement.md).

Retirement verification after the expansion: maintenance 52 passed, selection 239 passed, importer 120 passed, XLSX export 11 passed, and the Panel discarded-catalog check passed; `node --check src/mrs3/panel_web/app.js`, `py_compile`, and `git diff --check` passed. All database tests used isolated temporary DuckDB fixtures; no live PerformanceDB or Panel process was used.

The legacy v6-to-v7 migration fix and the narrowly scoped recovery for the known v9 catalog error are included. The feature contract is [PerformanceDB maintenance](docs/specs/2026-10-06-performance-db-maintenance.md). Independent Claude Opus review passed. Focused migration, maintenance, static UI, and Panel HTTP checks passed. No live PerformanceDB was accessed or modified during verification.
On 2026-10-08 a live reproduction with only `AALUSDT` selected showed that
Rejected preview memory growth came from a repeated correlated residual check,
not from the selected pair set. The residual recovery lookup is now one
set-based query. The focused regression and full maintenance suite passed:
`60 passed`; an isolated AAL preview completed in `0.79 s` with a 4 GB DuckDB
memory limit. The Panel process was stopped after the reproduction; no live
database rows were changed.

## Performance v2 selection and equity regime

The Performance v2 fixed-filter sequence is implemented and independently
reviewed. Its fixed Minimum Shift gate is enabled at `0.3%` immediately before
`PnL DD5/30 + PnL B/30`; the contract is [the filter-sequence specification](docs/specs/2026-10-01-performance-v2-filter-sequence.md).

The shared [XLSX column contract](docs/specs/2026-10-07-performance-v2-xlsx-column-contract.md)
now gives stable columns and separates the PRE28/W28/W14/W7 regime blocks.
The equity-regime classifier, cache, selection and Panel integration are
implemented locally; its live migration remains open. Evidence and exact
status rules are linked from the [equity status map](docs/specs/2026-10-03-equity-regime-status-map.md).

Card 6 partial selection review import and the global finalist retest control
are implemented. In the Card 6 `Импортировать статусы и ранги из XLSX` path,
each import now reconciles prior FINALIST decisions for only the imported
Pair + Side: an absent ID or a row without FINALIST plus rank becomes RESERVE
with no rank; explicit REJECTED remains REJECTED with no rank. The ledger stays
append-only. Other blank `User Status` and `User Rank` cells retain the
documented clear behavior. Final focused verification with
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_performance_v2_selection_review.py tests/test_performance_v2_store.py tests/test_performance_v2_compact.py tests/test_performance_v2_maintenance.py tests/test_panel_performance_v2.py`
passed 477 tests with 4 platform skips. This covers transactional v9-to-v10
migration, rollback/retry, and Panel schema preflight. A separate clean-HEAD
control reproduced 15 existing failures across the static-shell and legacy
v6-v8 benchmark suites (18 tests passed); these failures predate this change.
The live v9 database migration is still pending: after deploying this version,
the Panel schema preflight must run before the Card 6 import. No live database
mutation or Panel restart was performed. Independent code review returned
`CODE_REVIEW_PASS`.

### 2026-10-09 - Card 6 XLSX imports: reconcile prior finalists

- Both Card 6 import paths reconcile prior FINALIST decisions for the exact Pair + Side: a prior FINALIST remains FINALIST only when re-imported with FINALIST and a non-empty rank; explicit REJECTED remains REJECTED and clears rank; every other prior FINALIST, including IDs absent from the current selection run, is appended as RESERVE with rank NULL. Existing review comments/history are preserved. The selection-review button contract is [ADR-0065](docs/decisions/0065-performance-v2-card6-partial-import-finalist-reconciliation.md); the combined-control path is covered by [ADR-0064](docs/decisions/0064-performance-v2-finalist-retest-review-reconciliation.md).
- Verification after review fixes: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp <unique C:\Temp folder> --tb=short tests/test_performance_v2_selection_review.py tests/test_performance_v2_finalist_retest.py tests/test_panel_performance_v2_retest.py -k "not PARETO_PLATEAU_POINTS_PER_ORDER"`: 215 passed, 1 skipped, 1 deselected; temp folder removed. Four later edge-case regressions also passed. The skip is Windows symlink availability.
- The 215-test run and four edge-case runs preceded the final REJECTED-tag-scope patch. Tests were not rerun after that patch per the user's explicit instruction. The final tag-sync change is therefore unverified; if testing is later authorized, cover both preservation of an omitted finalist's existing REJECTED tag and cleanup/re-add of the tag for a submitted REJECTED row.
- Three unrelated broader-suite failures were reproduced on clean HEAD `8b755c4a292e0107ea6e2b701df6b865f9d792a9`: PARETO reason-alias rendering fails before import; v4 import migration test expects schema v9 though migration returns v10; prune fixture inserts 11 values into the 12-column `selection_results`. No production change was made for them. The Panel endpoints serialize writers with process and cross-process PerformanceDB locks; the import transaction covers finalist lookup, review append, and REJECTED tag synchronization.
- The user imported `AMGN.xlsx`. Read-only verification matched its workbook hash to an import journal entry for AMGNUSDT/LONG (15 rows): 3 FINALIST (ranks 1–3), 8 RESERVE, 4 blank. No database mutation by the agent; no Panel restart performed by the agent. Independent code review returned `CODE_REVIEW_PASS`. Next: after restarting Panel from this checkout, the user can re-import the 81 workbooks; the final tag-scope patch remains unverified by tests at the user's request.

## Other verified Panel changes

Panel PerformanceDB recalculation now serializes access through the controller
lock; live PURR confirmation remains open below. Performance v2 researched
stages 6–9 and the near-duplicate rule are governed by the
[researched-filter contract](docs/specs/2026-10-02-performance-v2-researched-filters.md).

Portfolio Stage 2 ordered-batch is fixture/fake-only and does not authorize a
real tester run. The Source v6 fresh compact multi-scope pipeline is complete;
this does not establish realized MRS3 performance.
## Bulk finalist equity-filter application (2026-10-07)

Card 6 has an explicit asynchronous `Применить эквити фильтр` action for the
frozen successful FINALIST or FINALIST+RESERVE cohort. It warms only missing
equity cache rows, runs only `filter_equity_regime`, publishes group snapshots
atomically, and reports phase/progress/result counts/typed errors. Import and
recovery do not invoke it automatically. Contract: [Performance v2 finalist
retest control](docs/specs/2026-09-09-performance-v2-finalist-retest-control.md).

Local backend, UI, equity and publication checks passed; the pre-existing
`PARETO_PLATEAU_POINTS_PER_ORDER` alias expectation is unchanged. No live
database, tester or Panel process was used.
## Bybit base-lot XLSX export (2026-10-07)

The one-command [Bybit base-lot export](docs/specs/2026-10-07-bybit-base-lot-export.md)
now maintains ten UTC-date size columns in `Actual!C:L`; each header is a date
and each value uses the seven complete dates preceding that header date. The
pair and listing-date columns A/B are preserved; legacy output columns C/D are
replaced. Later runs rotate only the date columns. A complete current date is
skipped unless retained error cells need retry. Missed dates rebuild a
contiguous ten-day window while mapping overlapping values by date; future
listing dates remain blank. Historical errors are retried and empty filters are
created through L. The filter preserves criteria outside the rewritten dates.
Focused verification: 39 tests passed; `git diff --check` and `py_compile`
passed; independent Opus `CODE_REVIEW_PASS`. No live workbook or network run
was performed.
## Fresh shortlist Minimum Shift gate (2026-10-08)

The fresh **Shortlist and READY JSON** flow has an optional
[Minimum Shift contract](docs/specs/2026-10-08-fresh-shortlist-minimum-shift.md)
with [ADR-0060](docs/decisions/0060-fresh-shortlist-minimum-shift.md) and an
[implementation plan](docs/superpowers/plans/2026-10-08-fresh-shortlist-minimum-shift.md).
It checks only the first order Shift, preserves strictly increasing later
orders, and canonicalizes `0.3%` as `30 bp` across shortlist, audit, READY and
RUNS provenance. Focused tests and independent review passed.

No live database, tester, migration, or Panel restart was performed; reload is
the first operational step below.
## Blockers and open tracks

- Live PerformanceDB: a read-only check on 2026-10-07 validated the schema v9 catalog and required tables. Finalist job `a21a55e57fc749ce9a132453594284d3` is `COMMITTED` at 250/250 and its metadata inbox is ready. Import child `5ee6a404025f4e15b63a4fe2a2b9c075` failed on a shorter effective period; rollback checks confirm current results were not changed. The per-strategy rejection fix passed independent review and Panel was restarted; no retry was started. Use a fresh retest dated 2026-06-01 through 2026-10-05.
- READY JSON: at the last diagnostic on 2026-10-04, the tester job journal showed `RUNNING / BOT_RUN` with zero of 2,418 completed, and the Panel process had not been restarted. Reload the Panel and confirm that no tester process is active. The recovery procedure for this exact journal state is not linked or verified here. Do not edit the journal; identify and verify the supported Panel recovery action before recovering the job.
- Live PURR recalculation: as of 2026-10-06, no confirmation is recorded after the controller-lock change.
- MRS3 performance evidence: real tester tick-test results and DD5 retesting are still required before making final performance claims. Source metrics alone do not establish realized strategy performance; see [PRD.md](PRD.md).
- Performance v2 equity-quality M5: evidence covers bounded real-data slices only. Full-corpus timing and user acceptance remain open in [the M5 evidence plan](docs/superpowers/plans/2026-09-26-performance-v2-equity-quality-m5-slice-evidence.md).
- Heavy PerformanceDB follow-ups: residual import, analysis, cache, and materialization profiling is explicitly deferred and tracked in [the deferred follow-up plan](docs/superpowers/plans/2026-10-01-deferred-heavy-db-follow-ups.md). That plan does not authorize implementation.
- Bybit collector: phases 1 through 8 are delivered; live integration and soak evidence in phase 9 remain open. See the [collector specification](docs/specs/2026-09-05-bybit-market-data-collector.md) and [implementation plan](docs/superpowers/plans/2026-09-05-bybit-market-data-collector.md).
- Canonical Phase 1: Tasks 0–12B are recorded complete; Task 12C fresh real-source smoke/performance remains open. Follow the [active specification](docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md) and [implementation plan](docs/superpowers/plans/2026-08-16-mrs3-v07-canonical-phase1.md).
- Portfolio Optimizer: Stage 2 ordered-batch implementation has fixture/fake evidence and independent `CODE_REVIEW_PASS`; no real tester run occurred. M6–M8 evidence ledgers are unadopted until the governing phased specification is reconciled. This does not authorize a real joint tester run, recommendation, trading admission, or live use. M5/M6 readiness, PnL floor, individual-DD ceiling, liquidity/freshness limits, profile ranking, and fresh user authorization remain open gates. The Phase 13 limiter work remains off-only while the bot limiter is not operational. See the [optimizer specification](docs/specs/2026-09-05-portfolio-optimizer.md), [UI contract](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md), [batch decision](docs/decisions/0059-portfolio-stage2-sequential-batch.md), and [implementation plan](docs/superpowers/plans/2026-10-07-portfolio-stage2-sequential-batch.md).
- Portfolio MILP selection: oversized universes rank by the discovery-LP proxy; the union grid can be shorter than a composition's own period and LONG+SHORT symbols use an approximate one-way mask. Near-duplicate finalists can yield micro-variant candidates. Lightweight readiness does not compare prepared `source_digest`; a stale preparation is caught only at Campaign creation/run. See [ADR-0066](docs/decisions/0066-portfolio-milp-composition-selection.md).
## Next steps

1. Restart or reload the Panel before using the Minimum Shift, finalist equity-filter and Finalist/Reserved-only selection controls; no live restart was performed during verification.
2. Run a fresh finalist retest from 2026-06-01 through 2026-10-05; then review the per-strategy import outcome before confirming it.
3. Confirm the live PURR recalculation after the Panel lock change.
4. Verify the READY JSON job state and identify the supported recovery procedure; do not edit the journal.
5. Keep the remaining Performance v2, database profiling, collector, and Portfolio Optimizer gates within their linked plans and specifications.
6. Do not make final MRS3 performance claims until real tick-test results and DD5 retesting are available.

Maintenance preview memory fix: rejected preview decision resolution is now
scoped to each selected symbol and its strategy IDs instead of loading the
entire selection/review history. Focused maintenance and selection-review
regressions pass; the final targeted suites pass
161 tests, and the Opus implementation review returned CODE_REVIEW_PASS. Live
PerformanceDB was not used.

Finalist/Reserved-only Performance v2 mode: implemented and independently
reviewed. Targeted request, UI, pipeline, readiness and XLSX checks pass (7
tests); the six-module selection/UI/export batch reports 798 passed and 5
platform skips. Its 3 failures were reproduced on clean HEAD: the maintenance
card string expectation, a Windows Node command-line length error, and the
PARETO reason-alias expectation. The UI toggle was exercised with a Node event
simulation; no live database or browser smoke was run. Reload the Panel before
using the checkbox.

Maintenance preview preflight fix (2026-10-08): removed the database-wide
reachability audit from selected-pair preview and apply revalidation; catalog
inspection retains that full audit. Replaced pair/global fingerprint sorting
with bounded DuckDB multiset aggregates, leaving key collection only for the
small strategy-ID target scan. This removes the `ORDER BY ALL` sort and the
long post-sort Python walk over the large equity/actions tables. Maintenance
and selection-review suites pass 165 tests; the new regressions verify the
preview bypass and order-independent fingerprint. No live PerformanceDB was
modified. The Opus re-review returned `CODE_REVIEW_PASS` after the orphan
preflight and two-direction action ownership fixes.

Follow-up review fixes (2026-10-08): full apply now repeats the complete
reachability audit after fingerprint revalidation and before the first DELETE,
so pre-existing orphan rows fail closed without deleting the selected pair.
Scoped action ownership now checks both `strategies.symbol` and
`strategy_actions.symbol`. Added regressions for an orphan preflight and an
action-symbol-only mismatch; maintenance suite passes 58 tests. No live
PerformanceDB was modified.

Panel maintenance recovery checks also pass 14 tests. The catalog endpoint
optimization is recorded separately below.

Catalog picker follow-up (2026-10-08): the Panel maintenance catalog endpoint
now calls `catalog_symbols()` instead of the audited `catalog()` helper. The
pair picker performs schema validation and symbol lookup only; full reachability
validation remains in full apply before DELETE. Maintenance and Panel
maintenance checks pass 73 tests in the focused run. No live PerformanceDB was
modified.

Interrupted finalist RETEST recovery (2026-10-08): recovered the existing
2360 indexed reports plus the already-tested manual finalist report into the
same 2361-member frozen cohort; no strategy was resubmitted. The metadata inbox
and replacement import both committed: 2361 imported/successful, 0 skipped,
0 rejected, 0 failures; cohort outcomes are finalized and the equity filter is
eligible for all 2361 strategies. Targeted recovery suites pass 180 tests with
1 Windows symlink-capability skip. The live PerformanceDB was intentionally
updated by this import. Next step: run the eligible equity filter when desired;
no full finalist RETEST is needed. Blockers: none.

Portfolio Optimizer lightweight readiness (2026-10-09): startup readiness now
reads current FINALIST identity/count metadata and prepared-row headers only;
it does not load strategy/action/equity payloads or decode `prepared_json`.
Explicit finalist preparation and Campaign submission retain the strict
prepared-artifact validation. Recovery keeps the latest terminal Campaign
visible while unlocking Stage 1 for `SUCCEEDED`, `FAILED`, `CANCELLED`, and
`INTERRUPTED`; only a nonterminal job freezes the form. Focused checks pass 9
tests; review follow-up coverage passes 12 tests. Full Panel UI/Portfolio
modules report 444 passed, 1 platform skip, and 3 known unrelated failures
(maintenance copy expectation, Windows Node command-line length, and the
11/12-column selection fixture). The shared selection insert was made
column-explicit for schema compatibility; the full `test_portfolio_input.py`
run then reports 117 passed and 32 unrelated failures from the in-progress
`SelectionConfig.ab_decline_cap_pct` snapshot contract and remaining historical
raw inserts. A separate strict-series/light-metadata contract run passes all 7
tests. The maintenance copy failure reproduces from an isolated clean `HEAD`
archive. No live database or Panel process was changed. Next step:
restart/reload Panel and confirm the pair picker becomes editable without a
full startup audit.

Portfolio combination-limit error detail (2026-10-09): when weighted finalist
composition enumeration exceeds `search.max_enumerated_combinations`, the
persisted failed-Campaign message now retains `COMBINATION_LIMIT_EXCEEDED` and
adds the exact product and configured limit. The adapter count/limit regression
and persisted Panel status regression pass (3 focused tests). Reload Panel
before the next run; the failed status will show `COMBINATIONS=N; LIMIT=M`.
