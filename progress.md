# MRS3 Current Status

Last updated: 2026-10-06

This file is a current-status snapshot, not a session log. Earlier progress notes remain in Git history; feature contracts and detailed evidence belong in the linked specs, reports, and plans.

## PerformanceDB maintenance

The maintenance card is implemented in the Strategy and DD5 tab. It supports pair selection, strategy-count previews, Rejected detail cleanup, full pair deletion, progress reporting, elapsed time, and actionable database errors. The code targets schema v9 and adds no cleanup markers, deletion timestamps, backups, or maintenance tables.

The legacy v6-to-v7 migration fix and the narrowly scoped recovery for the known v9 catalog error are included. The feature contract is [PerformanceDB maintenance](docs/specs/2026-10-06-performance-db-maintenance.md). Independent Claude Opus review passed. Focused migration, maintenance, static UI, and Panel HTTP checks passed. No live PerformanceDB was accessed or modified during verification.

## Performance v2 selection and equity regime

The researched selection stages and the equity-regime cache are implemented. Selection publication validates cached facts against the current source before publishing. Missing or invalid cache data blocks publication atomically. Focused backend and Panel selection/equity suites passed; independent Claude Opus review passed.

Card 6 now restores User Status and User Rank from partial selection XLSX folders and shows per-file progress, applied/unchanged counts, and actual API errors in Panel. Blank statuses are skipped; only nonblank decisions are checked against the database and written through the existing review ledger. FINALIST ranks are optional and unique within a run; RESERVE and REJECTED ranks must be blank. Focused partial-import/API/UI checks passed (19 tests), the full selection-review module passed (102 tests), and blank FINALIST rank clearing passed separately. Independent Claude Opus review passed. No live PerformanceDB was accessed or modified.

A read-only replay classified 30,940 frozen rows twice with identical output SHA-256 `870C88D293067C2CB1E8A58B4A297E2C4234E7AC87DA509E225EA374F6CCE0AF`. This verifies frozen summary facts; it does not re-extract all raw equity points. Evidence is in [the production replay report](docs/reports/2026-10-04-equity-regime-production-replay.md) and [the M3 research report](docs/reports/2026-10-04-equity-regime-m3-research.md). Current status rules and cleanup policy are in the [status-map specification](docs/specs/2026-10-03-equity-regime-status-map.md), [ADR-0055](docs/decisions/0055-equity-filter-rejected-and-manual-fact-cleanup.md), [ADR-0056](docs/decisions/0056-equity-rejection-source-lifecycle.md), and [ADR-0057](docs/decisions/0057-performance-db-manual-maintenance.md); later decisions supersede the initial cleanup-marker proposal.

## Other verified Panel changes

Panel PerformanceDB recalculation now serializes access through the controller lock. The focused Panel test module and independent review passed.

Performance v2 selection stages 6 through 9 and the stage 9 near-duplicate rule are documented in [the researched filter contract](docs/specs/2026-10-02-performance-v2-researched-filters.md). Focused selection and static UI checks passed; independent review passed.

The Source v6 fresh compact multi-scope pipeline is marked complete in [PRD.md](PRD.md). This does not prove realized MRS3 performance.

## Blockers and open tracks

- Live PerformanceDB: the code targets schema v9. At the last recorded live check on 2026-10-05, the database was schema v8 and a tester writer was active. The Panel returned `PERFORMANCE_V2_MIGRATION_REQUIRED`. No live migration or research-status backfill was performed. Recheck writer activity and prepare a verified offline copy before planning any migration.
- READY JSON: at the last diagnostic on 2026-10-04, the tester job journal showed `RUNNING / BOT_RUN` with zero of 2,418 completed, and the Panel process had not been restarted. Reload the Panel and confirm that no tester process is active. The recovery procedure for this exact journal state is not linked or verified here. Do not edit the journal; identify and verify the supported Panel recovery action before recovering the job.
- Live PURR recalculation: as of 2026-10-06, no confirmation is recorded after the controller-lock change.
- MRS3 performance evidence: real tester tick-test results and DD5 retesting are still required before making final performance claims. Source metrics alone do not establish realized strategy performance; see [PRD.md](PRD.md).
- Performance v2 equity-quality M5: evidence covers bounded real-data slices only. Full-corpus timing and user acceptance remain open in [the M5 evidence plan](docs/superpowers/plans/2026-09-26-performance-v2-equity-quality-m5-slice-evidence.md).
- Heavy PerformanceDB follow-ups: residual import, analysis, cache, and materialization profiling is explicitly deferred and tracked in [the deferred follow-up plan](docs/superpowers/plans/2026-10-01-deferred-heavy-db-follow-ups.md). That plan does not authorize implementation.
- Bybit collector: phases 1 through 8 are delivered; live integration and soak evidence in phase 9 remain open. See the [collector specification](docs/specs/2026-09-05-bybit-market-data-collector.md) and [implementation plan](docs/superpowers/plans/2026-09-05-bybit-market-data-collector.md).
- Portfolio Optimizer: fixture/fake evidence does not authorize a real joint tester run, recommendation, trading admission, or live use. PnL floor, individual-DD ceiling, liquidity/freshness limits, and profile ranking remain open gates. Stage 2 remains blocked until its implementation gates and separate user authorization are satisfied. The Phase 13 limiter work remains off-only while the bot limiter is not operational. See the [optimizer specification](docs/specs/2026-09-05-portfolio-optimizer.md), [UI contract](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md), and [implementation plan](docs/superpowers/plans/2026-09-05-portfolio-optimizer.md).
- Campaign combination preflight: the configured limit and server-side rejection of over-limit job creation are implemented. The form still does not calculate or display the exact finalist combination product before submission. See the [Portfolio Optimizer UI specification](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).

## Next steps

1. Recheck the live PerformanceDB schema and active writers before planning any migration.
2. Confirm the live PURR recalculation after the Panel lock change.
3. Verify the READY JSON job state and identify the supported recovery procedure; do not edit the journal.
4. Keep the remaining Performance v2, database profiling, collector, and Portfolio Optimizer gates within their linked plans and specifications.
5. Do not make final MRS3 performance claims until real tick-test results and DD5 retesting are available.
