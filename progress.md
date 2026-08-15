# Progress

**Updated:** 2026-08-15
**Current branch:** `main`
**Current feature:** Tester Report Library and Fast Identity

## Current State

Current work is described by the approved
[tester report library specification](docs/specs/2026-08-14-tester-report-library-and-fast-identity.md),
its [task plan](docs/superpowers/plans/2026-08-14-tester-report-library-and-fast-identity.md),
and the existing
[name-only runner contract](docs/specs/2026-08-14-tester-name-only-verification.md).

- Fast embedded strategy-name extraction is implemented in
  `src/mrs3/runner/results.py` and used by the snapshot collector.
- Runner `reconcile_results` intentionally remains `strategy_name_only`: it
  validates wizard evidence, a stable HTML file and the embedded strategy name,
  but does not parse HTML metrics or trades.
- The focused `src/mrs3/runner/report_library.py` publisher and its tests exist.
  It calls the name-only reconciliation path and supports read-only evaluation
  through `apply=False`; it does not yet satisfy the report-library
  specification's separate full-reconciliation requirement.
- Final workflow and CLI integration is not implemented. There is no
  `tester-report-library` command, and the completed runner path still captures
  the inbox and then calls the existing broad cleanup directly.
- Historical session logs and completed-plan narratives have been removed.
  Durable feature status belongs in `PRD.md`, feature specifications and ADRs.
- The DuckDB surface coverage contract has a separately approved amendment in
  ADR-0008. Runtime still uses the earlier one-MA-pair readiness rule and aborts
  globally on structurally zero-duration report/grid rows.
- The ADR-0008 implementation project draft is written in
  [the Common Close-MA Readiness plan](docs/superpowers/plans/2026-08-15-common-close-ma-readiness.md)
  as a project draft. ADR-0008 is frozen; the draft requires revision before
  execution and no implementation task may start from it.
- Priority 1 is the approved operational patch for the current one-MA-pair
  flow: double-zero isolation, stale-token clearing, actionable safe errors,
  side ordinal, coverage artifact links, truthful UTC/Side guidance, exact
  preview-to-real-preflight reproduction, and one current panel process.

## Latest Verification

- `72 passed` for
  `.venv\Scripts\python.exe -m pytest tests/runner/test_monitor.py tests/runner/test_report_library.py tests/runner/test_workflow.py tests/test_cli.py -q`
  on 2026-08-15.
- Latest repository-wide run: `745 passed, 2 skipped, 4 failed`. All four
  failures are in `tests/runner/test_http.py` because the local
  `tests/fixtures/tester_wizard.html` and `tests/fixtures/tester_table.html`
  files are absent. The two skips require unavailable Windows symlink rights.

## Next Required Work

1. Write and review the Priority-1 operational patch implementation plan, then
   execute it with TDD and a live one-process panel smoke check.
2. Do not execute or extend the frozen ADR-0008 project plan until that work is
   resumed explicitly.
3. Resolve the contract/plan mismatch before changing behavior: the runner must
   remain name-only, while library acceptance currently requires full metric
   and trade reconciliation. Record the chosen library-specific boundary in the
   specification and plan.
4. Execute Task 3 with failing workflow and CLI tests.
5. Publish and verify the library manifest before removing any live duplicate.
6. Add a read-only `tester-report-library` command with explicit `--apply`.
7. Run focused and relevant broader tests, `git diff --check`, and independent
   review before enabling operational cleanup.

## Blockers And Safety

- Task 3 operational integration is blocked by the unresolved reconciliation
  contract above.
- Do not treat the current panel coverage checkbox as proof that every Close MA
  `2..7` shares the displayed interval until ADR-0008 is implemented and
  verified.
- The live panel on port `8765` serves stale HTML and does not expose the current
  multi-side/date-only source implementation; it must be restarted before UI
  behavior is accepted.
- `ONUSDT` coverage currently aborts globally on its structurally double-zero
  `LONG/15m` rows, so no coverage token or inventory is produced for otherwise
  usable ONUSDT scopes.
- Coverage-token `/preflight` currently previews cached common intervals only;
  source revalidation, side audits, and real `DirectPreflight` occur after
  `Start`, so expensive failures appear late in the background job.
- Direct runs repeat full source validation and payload hashing several times;
  a normal two-side run can perform up to eight structural passes over the
  current archive.
- A failed coverage refresh leaves the previous table/token visible. Background
  `FAILED`/`PARTIAL` details and queue ordinal are discarded, and the panel does
  not expose the generated coverage audit links.
- The approved report-library specification is not currently registered in the
  active-document table in `PRD.md`.
- Do not use `publish_verified_reports(..., apply=True)` operationally yet: the
  current low-level path can remove a duplicate before manifest publication.
- Do not treat HTML-only evidence as runner completion. A matching one-strategy
  wizard result and exact embedded strategy name remain mandatory.
- Do not delete existing saved reports, snapshots, DuckDB data, or live reports
  without a verified byte-identical library copy and
  `safe_to_delete=YES` evidence.
