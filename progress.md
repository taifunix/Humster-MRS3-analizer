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

The two source-package builders are implemented and tested: CSV produces
`legacy_trades_proxy`; DuckDB reconstructs closed
`real_independent_events` with an exclusion audit. The loader now preserves a
declared source metadata (`event_mode`, `PointEventCount` and event-set hash),
while rejecting unknown or mixed modes. The current raw-CSV selector remains a
compatibility path; package-directory input is the next implementation step.

DuckDB compact series decoders and pure, windowed source-metric calculation
are implemented and unit-tested. The DuckDB builder now publishes a
package with fail-closed verification, selector-compatible metric rows, sorted event
mappings and HTML sample audit. The next task is the package-directory loader
and selector integration; no real local package has been built yet.

Next, make the selector consume one package directory rather than an arbitrary
CSV. Before a DuckDB package can feed that selector, add and reconcile the
per-point economic metrics required by the existing selection contract. Then
rebuild the universe, emit its before/after event audit and consider Phase 2
redundancy only if its actual candidate count calls for it.

## Blockers

- The DuckDB implementation must first confirm the actual v4 raw-payload schema and report-to-point mapping; no raw HTML or database data may be changed.
- The required user-provided listing-date and strategy-template inputs for a full selector run remain external inputs; package construction and its audit can proceed independently.

## Queued module hook

**Анализатор Портфеля:** [спецификация v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) передана отдельной команде. Начинать с проверки входных контрактов; до trade timestamps и определённого limiter допускается только Layer A, без симулятора и рекомендаций по сету.

## Update protocol

Replace this file’s verified-state and next-action sections whenever a commit changes the operational state. Keep only the present blocker set; durable decisions belong in ADRs and feature requirements belong in specs.
