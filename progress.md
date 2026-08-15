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
- The DuckDB Priority-1 operational patch is implemented and verified. Runtime
  intentionally remains on the earlier one-MA-pair readiness rule, but rows
  whose report and grid windows are both zero-duration are now excluded without
  weakening other fail-closed interval checks.
- The ADR-0008 implementation project draft is written in
  [the Common Close-MA Readiness plan](docs/superpowers/plans/2026-08-15-common-close-ma-readiness.md)
  as a project draft. Priority-1 delivered the approved double-zero isolation
  rule; the remaining common Close-MA work is frozen, and the draft requires
  revision before execution.
- Priority 1 now provides double-zero isolation, server/browser stale-state
  clearing, actionable path-safe errors, side ordinal, verified coverage CSV
  links, truthful UTC/Side guidance, exact preview-to-real-preflight
  reproduction, one current panel listener, and client-side Check coverage
  activity feedback with duplicate-request protection.

## Latest Verification

- `72 passed` for
  `.venv\Scripts\python.exe -m pytest tests/runner/test_monitor.py tests/runner/test_report_library.py tests/runner/test_workflow.py tests/test_cli.py -q`
  on 2026-08-15.
- Latest repository-wide run: `745 passed, 2 skipped, 4 failed`. All four
  failures are in `tests/runner/test_http.py` because the local
  `tests/fixtures/tester_wizard.html` and `tests/fixtures/tester_table.html`
  files are absent. The two skips require unavailable Windows symlink rights.
- DuckDB Priority-1 relevant run: `213 passed` for
  `.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py tests/test_panel.py tests/test_analysis_storage.py -q`.
- The repository-wide run before the final four focused regressions reached
  `780 passed, 2 skipped, 4 failed`; the same four runner failures are caused
  only by the two absent fixtures above. No later repository-wide run covers
  current `HEAD`; the current relevant evidence is `213 passed`.
- Read-only real-source evidence: exactly 18 expected ONUSDT/LONG/15m
  shift-430 double-zero rows exist; after exclusion, ONUSDT 15m LONG and SHORT
  are selectable with zero gap details.
- Live panel smoke: one listener on `127.0.0.1:8765`, HTTP 200, current
  date-only/side-aware coverage UI and Priority-1 markers present.
- The live listener was intentionally not restarted for the Check coverage
  activity-feedback change; its already-loaded page remains unchanged until
  the next normal panel restart.

## Next Required Work

1. Do not execute or extend the frozen remaining common Close-MA project plan
   until that work is resumed explicitly.
2. Resolve the contract/plan mismatch before changing behavior: the runner must
   remain name-only, while library acceptance currently requires full metric
   and trade reconciliation. Record the chosen library-specific boundary in the
   specification and plan.
3. Execute Task 3 with failing workflow and CLI tests.
4. Publish and verify the library manifest before removing any live duplicate.
5. Add a read-only `tester-report-library` command with explicit `--apply`.
6. Run focused and relevant broader tests, `git diff --check`, and independent
   review before enabling operational cleanup.

## Blockers And Safety

- Task 3 operational integration is blocked by the unresolved reconciliation
  contract above.
- Do not treat the current panel coverage checkbox as proof that every Close MA
  `2..7` shares the displayed interval until ADR-0008 is implemented and
  verified.
- Coverage-token `/preflight` currently previews cached common intervals only;
  source revalidation, side audits, and real `DirectPreflight` occur after
  `Start`, so expensive failures appear late in the background job.
- Direct runs repeat full source validation and payload hashing several times;
  a normal two-side run can perform up to eight structural passes over the
  current archive.
- Do not use `publish_verified_reports(..., apply=True)` operationally yet: the
  current low-level path can remove a duplicate before manifest publication.
- Do not treat HTML-only evidence as runner completion. A matching one-strategy
  wizard result and exact embedded strategy name remain mandatory.
- Do not delete existing saved reports, snapshots, DuckDB data, or live reports
  without a verified byte-identical library copy and
  `safe_to_delete=YES` evidence.
