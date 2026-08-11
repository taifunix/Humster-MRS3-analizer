# Progress

**Updated:** 2026-08-11
**Current branch:** `main`
**Current feature:** [v0.7 DuckDB analysis storage and importer](docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md)

## Verified repository baseline

- Root package: `src/mrs3`; tests: `tests`; project version: `0.7.0`.
- Latest committed source-package suite: `295 passed, 1 skipped`
  (`.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider`, 2026-08-11).
  The skip is the Windows symlink-permission test, not a product failure.
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

CSV and DuckDB source-package builders, v2 source verification, package loading,
the selector event gate and the panel source-package controls are implemented.
ADR-0003 makes `TotalTrades`, `WinRate` and `ProfitFactor` the fail-closed
full-horizon source gate; PnL/DD remain mandatory
`NOT_COMPARABLE_WINDOW_SCOPE` diagnostics. The DuckDB materializer uses bounded
reads and retains the complete cycle/exclusion audit.

Task 1 of the approved DuckDB storage/importer plan rejects lossy
fractional/non-finite integer loader values, accepts only Python `None` and
`pd.NA` wins/losses values as zero, and rejects empty normalized input before
downstream processing. Focused evidence: `52 passed` for `tests/test_loader.py` and
`33 passed` for `tests/test_source_packs.py` with the local `.venv` on
2026-08-11. The full suite reaches `316 passed` but has eight independent
runner-test failures because three required HTML fixtures are absent from Git;
those paths are outside Task 1 and were not changed. Independent re-review is
approved; Task 1 is complete.

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

## Next required work

The core specification and its 16-task plan (Task 0 plus Tasks 1–15) are
approved. Task 0 is represented by the existing `d76b985` package-side/UTC
slice; Task 1 is complete and Task 2 is next.

1. Start Task 2 from the
   [core implementation plan](docs/superpowers/plans/2026-08-11-v07-duckdb-analysis-storage-and-importer.md).
2. Apply the confirmed external-review remediation tasks listed in the
   [core implementation plan](docs/superpowers/plans/2026-08-11-v07-duckdb-analysis-storage-and-importer.md).
3. Implement the approved source-DuckDB migration/importer and direct analysis
   surface storage in independently reviewed TDD slices.
4. Keep CSV/DuckDB overlay deferred unless the user activates its separate ТЗ.

## Blockers

- No external data blocker is known for the core storage/importer work.
  CSV/DuckDB overlay is explicitly deferred and no economic threshold changes
  are implied.

## Queued module hook

**Анализатор Портфеля:** [спецификация v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) передана отдельной команде. Начинать с проверки входных контрактов; до trade timestamps и определённого limiter допускается только Layer A, без симулятора и рекомендаций по сету.

## Update protocol

Replace this file’s verified-state and next-action sections whenever a commit changes the operational state. Keep only the present blocker set; durable decisions belong in ADRs and feature requirements belong in specs.
