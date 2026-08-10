# Progress

**Updated:** 2026-08-10
**Current branch:** `main`
**Current feature:** [Safe runner smoke-test](docs/specs/2026-08-10-v06-runner-safe-root-json-smoke.md)

## Verified repository baseline

- Root package: `src/mrs3`; tests: `tests`; project version: `0.7.0`.
- Full test suite: `162 passed` (`.venv\\Scripts\\python.exe -m pytest -q`, 2026-08-10); this is not a real panel/bot end-to-end check.
- DuckDB v3/v4 importer pair is preserved in `programs/Обработчик HTML-DuckDB/`; v4 requires adjacent v3 codec.
- Local tester configuration exists outside Git as ignored `config.local.json`.
- Repository foundation, documentation model, importer/source preservation and Claude Code instructions are committed on `main`.

## Current verified external evidence

- The local v4 DuckDB was checked read-only: schema version `4`, compact payload schema and referential integrity are confirmed. Two malformed HTML reports are quarantined; the user accepted their exclusion from the current universe.
- The common UTC window is `[2026-07-15T00:00:00, 2026-08-06T00:00:00)`. The long CSV fully matches it; the short CSV contains one row with a shorter period and must be excluded by exact-period filtering.
- The panel starts locally and answers HTTP 200. A one-strategy `tester-plan` succeeds when the JSON is supplied through a directory.

## Next required evidence

Complete the safe runner smoke-test before any v0.7 implementation:

1. Implement the root-level-JSON-only runner contract.
2. Run its focused tests and an independent review.
3. Execute `tester-plan` for one staged JSON and review the plan.
4. Only after explicit confirmation, run one real test and preserve the resulting evidence.

## Blockers

- Current runner replaces the whole strategy directory, which is unsafe because the bot uses only root-level JSON while protected strategies live in nested directories. The smoke-test spec must be implemented first.
- Materializer, unified input and selector-v0.7 changes are not started.

## Queued module hook

**Анализатор Портфеля:** [спецификация v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) передана отдельной команде. Начинать с проверки входных контрактов; до trade timestamps и определённого limiter допускается только Layer A, без симулятора и рекомендаций по сету.

## Update protocol

Replace this file’s verified-state and next-action sections whenever a commit changes the operational state. Keep only the present blocker set; durable decisions belong in ADRs and feature requirements belong in specs.
