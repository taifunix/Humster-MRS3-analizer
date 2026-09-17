# Performance v2 optimizer prepared inputs — Phase 8 evidence

Date: 2026-09-17. Status: Accepted.

## Accepted result

Performance v2 schema v5 now persists nullable typed action `Price`/`Cost`, six
nullable WS1.1 sizing facts, and one private, versioned, digest-bound, per-result
prepared optimizer input. New ADD/REPLACE imports create the prepared row in the
existing writer transaction. The additive v4 migration backfills only exact
revision-checked saved facts and does not rebuild historical prepared rows.

Normal Performance recalculation prepares missing or stale requested current
results with `duckdb_import.workers`: one read snapshot is closed before CPU-only
workers run, then one writer transaction rechecks current identity and digest
before replacing rows. PerformanceDB consumers fail closed and never fall back
to compatibility JSON; Campaign-specific common grids remain outside the store.

## Acceptance evidence

- schema/migration focused suite: `45 passed`;
- importer suite: `72 passed`;
- optimizer prepared-input suite after review findings: `18 passed`;
- Portfolio input suite: `111 passed`;
- protected Panel/selection/windows/Portfolio adapter/export/report set:
  `681 passed, 2 skipped`;
- full repository suite:
  `.venv\Scripts\python.exe -m pytest -q --junitxml=.pytest-phase8-full.xml`
  — `4503 passed, 7 skipped, 25 warnings in 1628.68s`;
- `compileall` and `git diff --check` passed.

The full and focused runs used the exact code and test tree submitted for final
implementation review, before the later acceptance-only edits to PRD, progress,
specification, ADR, plan and this evidence ledger. That tree was still
uncommitted, so no commit SHA exists for the run. The JUnit file
`.pytest-phase8-full.xml` was temporary and was removed after its result was
validated; no Python, SQL or migration file changed after the run.

The migration invalid-input table covers six explicit dropped classes:
malformed JSON, wrong schema, wrong Price/Cost semantics, stale revision stamp,
wrong report hash, and oversized payload. Direct regression tests also cover
transactional migration failure, eager and lazy replacement, explicit empty
scope, real Windows multiprocess parity, worker failure before writing, stale
reusable-row races, second-write rollback, exact legacy/prepared cycle equality,
over-scale raw provenance, and absence of private prepared data from public API
and XLSX.

Independent implementation review returned `CODE_REVIEW_PASS` after one findings
round and a complete per-finding evidence disposition.

## Retained boundaries

No production PerformanceDB was opened, no tester or Campaign was executed, and
no network access, recommendation surface change, runtime path change or live
authorization occurred. Phase 8 does not authorize Phase 7 real tester
calibration, Stage 2, recommendation readiness or live use.
