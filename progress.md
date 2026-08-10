# Progress

**Updated:** 2026-08-10
**Current branch:** `main`
**Current feature:** [v0.7 event source packs](docs/specs/2026-08-10-v07-event-source-packs.md)

## Verified repository baseline

- Root package: `src/mrs3`; tests: `tests`; project version: `0.7.0`.
- Full test suite: `214 passed, 1 skipped` (`.venv\\Scripts\\python.exe -m pytest -q`, 2026-08-10). The skip is the Windows symlink-permission test, not a product failure.
- DuckDB v3/v4 importer pair is preserved in `programs/Обработчик HTML-DuckDB/`; v4 requires adjacent v3 codec.
- Local tester configuration exists outside Git as ignored `config.local.json`.
- Repository foundation, documentation model, importer/source preservation and Claude Code instructions are committed on `main`.

## Current verified external evidence

- The local v4 DuckDB was checked read-only: schema version `4`, compact payload schema and referential integrity are confirmed. Two malformed HTML reports are quarantined; the user accepted their exclusion from the current universe.
- The common UTC window is `[2026-07-15T00:00:00, 2026-08-06T00:00:00)`. The long CSV fully matches it; the short CSV contains one row with a shorter period and must be excluded by exact-period filtering.
- The v4 database contains `96,767` reports with the expected compact codecs. A source-HTML/Payload sample established the materializer formulas: wallet-change PnL, equity peak drawdown, and realised `closed`/`decreased` action metrics. The supplied CSV batch is a different grid and is not used as its reconciliation oracle; verification will use the source HTML referenced by DuckDB.
- The panel starts locally and answers HTTP 200. A one-strategy `tester-plan` succeeds when the JSON is supplied through a directory.
- Two approved real one-strategy smoke-runs completed. The second run completed the full runner lifecycle through `CSV_COMMITTED` and `COMPLETED` after the transient state-file lock fix; its result CSV is retained locally, while the configured report directory and both wizard logs were absent after cleanup. Only the root strategy JSON was changed; nested strategy folders were not touched.

## Next required evidence

CSV and DuckDB source-package builders are implemented and tested. CSV produces
`legacy_trades_proxy`; DuckDB reconstructs closed
`real_independent_events` with an exclusion audit and fail-closed HTML-sample
verification. Package loading now rejects missing, mixed, unknown or unverified
event metadata and requires exact real-event mappings. The selector accepts
exactly one `--source-package` or compatibility `--input-csv`; for real packages
each plateau's event union includes every geometric plateau point, even one that
is not eligible for selection. The local panel exposes these source packages
as an explicit candidate-input choice; it submits exactly one verified package
or compatibility raw CSV, and keeps Portfolio Analyzer controls noninteractive.
DuckDB HTML verification is available from both `source-duckdb` and its panel
tab through an explicit optional local root and a fail-closed 3–5 sample count;
no local path is stored in the package manifest.

The DuckDB materializer now streams database reads in bounded batches. It
still decodes every report's actions to preserve the complete cycle/exclusion
audit, but reads timestamp/equity/wallet blobs only for grids whose stored
bounds cover the requested window and validates the decoded grid before metric
calculation. Regression coverage proves that a non-covering report with invalid
heavy series remains audited while covered-point output is unchanged. Full
suite evidence: `251 passed, 1 skipped` (`.venv\\Scripts\\python.exe -m pytest -q
-p no:cacheprovider`, 2026-08-10); the skip is the Windows symlink permission
case.

Read-only materialization of the full local v4 database reached the
source-summary comparison stage. That evidence established a necessary
contract correction: source HTML summaries cover each report's full test
horizon, while v0.7 points/events cover the selected `[start, end)` window.
The parser must also distinguish absolute `Total PnL`/`Max Drawdown` from
their `%` forms. The old single real-package verification label cannot prove
both facts and is not selector evidence for a real package.

The next implementation step is to publish/load real source packages in v2:
fail-closed full-horizon `source_summary_status=VERIFIED` from 3–5 HTML
comparisons, followed by `window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE`
for `[start, end)` metrics/events. The real selector gate requires their
conjunction; legacy v1 proxy packages retain their existing contract. Then
build one real read-only DuckDB package with the approved window and HTML
samples, run `select --source-package` with the externally supplied listing
dates and strategy template, retain the local audit, and decide whether Phase
2 is needed only from the resulting candidate/event distributions.

## Blockers

- The required user-provided listing-date and strategy-template inputs for a full selector run remain external inputs; package construction and its audit can proceed independently.

## Queued module hook

**Анализатор Портфеля:** [спецификация v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) передана отдельной команде. Начинать с проверки входных контрактов; до trade timestamps и определённого limiter допускается только Layer A, без симулятора и рекомендаций по сету.

## Update protocol

Replace this file’s verified-state and next-action sections whenever a commit changes the operational state. Keep only the present blocker set; durable decisions belong in ADRs and feature requirements belong in specs.
