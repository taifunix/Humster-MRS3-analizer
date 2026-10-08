# MRS3 Current Status

Last updated: 2026-10-08

This file is a current-status snapshot, not a session log. Earlier progress notes remain in Git history; feature contracts and detailed evidence belong in the linked specs, reports, and plans.

Use [PRD.md](PRD.md) for the feature registry and [docs/README.md](docs/README.md)
for the documentation map. Read the linked contract only for the active task;
do not treat this file as a replacement for a feature specification.

## PerformanceDB maintenance

The maintenance card is implemented in the Strategy and DD5 tab. It supports pair selection, strategy-count previews, Rejected retirement, full pair deletion, progress reporting, elapsed time, and actionable database errors. `Удалить Rejected` now physically removes per-strategy facts, window/equity caches, tags, rejection sources, and selection rows, compacts the retained result to interval/provenance identity, then atomically marks the typed identity as `DISCARDED`; only strategy settings/orders and that compact tombstone remain for deduplication. The code targets schema v9 and adds no cleanup markers, deletion timestamps, backups, or maintenance tables. Contract: [PerformanceDB maintenance](docs/specs/2026-10-06-performance-db-maintenance.md); retirement decision: [ADR-0061](docs/decisions/0061-performance-db-rejected-retirement.md).

Retirement verification after the expansion: maintenance 52 passed, selection 239 passed, importer 120 passed, XLSX export 11 passed, and the Panel discarded-catalog check passed; `node --check src/mrs3/panel_web/app.js`, `py_compile`, and `git diff --check` passed. All database tests used isolated temporary DuckDB fixtures; no live PerformanceDB or Panel process was used.

The legacy v6-to-v7 migration fix and the narrowly scoped recovery for the known v9 catalog error are included. The feature contract is [PerformanceDB maintenance](docs/specs/2026-10-06-performance-db-maintenance.md). Independent Claude Opus review passed. Focused migration, maintenance, static UI, and Panel HTTP checks passed. No live PerformanceDB was accessed or modified during verification.
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
are implemented and reviewed. The current live retest/import state is tracked
under [Blockers and open tracks](#blockers-and-open-tracks) below; no live
database mutation is implied by local test evidence.
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
reuses the validated seven-day minute-liquidity window, writes the base lot to
`Actual!C`, and writes the UTC date or an explicit cell error to `Actual!D`.
Focused tests and independent review passed; no live workbook or network run
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
- Campaign combination preflight: the configured limit and server-side rejection of over-limit job creation are implemented. The form still does not calculate or display the exact finalist combination product before submission. See the [Portfolio Optimizer UI specification](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).
## Next steps

1. Restart or reload the Panel before using the newly implemented Minimum Shift and finalist equity-filter controls; no live restart was performed during verification.
2. Run a fresh finalist retest from 2026-06-01 through 2026-10-05; then review the per-strategy import outcome before confirming it.
3. Confirm the live PURR recalculation after the Panel lock change.
4. Verify the READY JSON job state and identify the supported recovery procedure; do not edit the journal.
5. Keep the remaining Performance v2, database profiling, collector, and Portfolio Optimizer gates within their linked plans and specifications.
6. Do not make final MRS3 performance claims until real tick-test results and DD5 retesting are available.
