# Progress

**Updated:** 2026-08-10
**Current branch:** `main`
**Current feature:** [v0.7 legacy selection](docs/specs/2026-08-10-v07-legacy-selection.md)

## Verified repository baseline

- Root package: `src/mrs3`; tests: `tests`; project version: `0.7.0`.
- Full test suite: `162 passed` (`.venv\\Scripts\\python.exe -m pytest -q`, 2026-08-10).
- DuckDB v3/v4 importer pair is preserved in `programs/Обработчик HTML-DuckDB/`; v4 requires adjacent v3 codec.
- Local tester configuration exists outside Git as ignored `config.local.json`.
- Repository foundation, documentation model, importer/source preservation and Claude Code instructions are committed on `main`.

## Next required evidence

Collect the v4 import outcome before implementing the materializer:

1. `schema_info.schema_version` must be `4`.
2. `import_manifest.json` must report the expected worker run and quarantine count.
3. Confirm compact schema has no row-per-sample tables.
4. Review `html_delete_checklist.csv`; do not delete any HTML before its row is `safe_to_delete=YES`.
5. Record actual database size and payload statistics in the import audit.

## Blockers

- The repository contains no v4 database, manifest, checklist or import console output.
- Therefore materializer, unified input and selector-v0.7 changes are intentionally not started.
- The local remote URL is configured, but no GitHub repository creation or push was requested/performed.

## Queued module hook

**Анализатор Портфеля:** [спецификация v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) передана отдельной команде. Начинать с проверки входных контрактов; до trade timestamps и определённого limiter допускается только Layer A, без симулятора и рекомендаций по сету.

## Update protocol

Replace this file’s verified-state and next-action sections whenever a commit changes the operational state. Keep only the present blocker set; durable decisions belong in ADRs and feature requirements belong in specs.
