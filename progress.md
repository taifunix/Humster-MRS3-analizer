# Progress

**Updated:** 2026-08-10
**Current branch:** `main`
**Current feature:** [v0.7 event source packs](docs/specs/2026-08-10-v07-event-source-packs.md)

## Verified repository baseline

- Root package: `src/mrs3`; tests: `tests`; project version: `0.7.0`.
- Full test suite: `183 passed, 1 skipped` (`.venv\\Scripts\\python.exe -m pytest -q`, 2026-08-10). The skip is the Windows symlink-permission test, not a product failure.
- DuckDB v3/v4 importer pair is preserved in `programs/Обработчик HTML-DuckDB/`; v4 requires adjacent v3 codec.
- Local tester configuration exists outside Git as ignored `config.local.json`.
- Repository foundation, documentation model, importer/source preservation and Claude Code instructions are committed on `main`.

## Current verified external evidence

- The local v4 DuckDB was checked read-only: schema version `4`, compact payload schema and referential integrity are confirmed. Two malformed HTML reports are quarantined; the user accepted their exclusion from the current universe.
- The common UTC window is `[2026-07-15T00:00:00, 2026-08-06T00:00:00)`. The long CSV fully matches it; the short CSV contains one row with a shorter period and must be excluded by exact-period filtering.
- The panel starts locally and answers HTTP 200. A one-strategy `tester-plan` succeeds when the JSON is supplied through a directory.
- Two approved real one-strategy smoke-runs completed. The second run completed the full runner lifecycle through `CSV_COMMITTED` and `COMPLETED` after the transient state-file lock fix; its result CSV is retained locally, while the configured report directory and both wizard logs were absent after cleanup. Only the root strategy JSON was changed; nested strategy folders were not touched.

## Next required evidence

Implement v0.7 event source packs in the order fixed by the active spec:

1. Define and test the CSV `legacy_trades_proxy` package, including exact-window validation.
2. Define and test the DuckDB `real_independent_events` package, closed-cycle reconstruction and exclusion audit.
3. Make the selector accept exactly one declared package and apply `PointEventCount` without mixed modes.
4. Rebuild the universe and emit the before/after event audit; consider Phase 2 redundancy only after its actual count is available.

## Blockers

- The DuckDB implementation must first confirm the actual v4 raw-payload schema and report-to-point mapping; no raw HTML or database data may be changed.
- The required user-provided listing-date and strategy-template inputs for a full selector run remain external inputs; package construction and its audit can proceed independently.

## Queued module hook

**Анализатор Портфеля:** [спецификация v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) передана отдельной команде. Начинать с проверки входных контрактов; до trade timestamps и определённого limiter допускается только Layer A, без симулятора и рекомендаций по сету.

## Update protocol

Replace this file’s verified-state and next-action sections whenever a commit changes the operational state. Keep only the present blocker set; durable decisions belong in ADRs and feature requirements belong in specs.
