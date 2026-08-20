# Handoff: MRS3 v0.7 Canonical Phase 1

**Date:** 2026-08-16
**Purpose:** transfer the current session state to a new implementation session.

## Current Decision

The Phase 1 governance gate passed independent review.

- [ADR-0009](../decisions/0009-canonical-phase1-surface-selection-contract.md) is `Accepted`.
- [Canonical Phase 1 specification](../specs/2026-08-16-mrs3-v07-canonical-phase1.md) is `Approved / Active`.
- [Canonical Phase 1 implementation plan](../superpowers/plans/2026-08-16-mrs3-v07-canonical-phase1.md) is approved for execution.
- `progress.md` names Task 1 as the next action.
- ADR-0007 and ADR-0008 remain byte-for-byte unchanged historical decisions.
- No Phase 1 behavior code has been implemented yet.

The existing DuckDB runtime remains the verified old one-MA-pair flow. The old
[coverage specification](../specs/2026-08-14-duckdb-surface-coverage-review.md)
is historical implementation evidence, not the contract for new canonical
surfaces.

## Mandatory Reading Order

Read these Markdown files in this exact order:

1. `AGENTS.md`
2. `PRD.md`
3. `progress.md`
4. `docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md`
5. `docs/decisions/0009-canonical-phase1-surface-selection-contract.md`
6. `docs/superpowers/plans/2026-08-16-mrs3-v07-canonical-phase1.md`

For the first implementation task, then read only these explicit dependencies:

7. `docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md`
8. `docs/specs/v07-event-filter-and-shortlist.md`
9. `docs/specs/2026-08-14-panel-multiscope-strategy-generation.md`
10. `docs/specs/2026-08-14-duckdb-surface-coverage-review.md`
11. `docs/decisions/0007-observed-sparse-surface-contract.md`
12. `docs/decisions/0008-common-close-ma-readiness-and-degenerate-row-isolation.md`

Read the last three only to understand retained historical behavior and
supersession boundaries. Do not edit them. Do not read `docs/archive/` unless a
new active document directly links to a specific archived source.

## Next Action

Execute **Task 1 only** from the approved plan:

> Make `AlgorithmConfig` the single canonical algorithm source of truth.

Task 1 is expected to cover the canonical Shift grid, sample calibration,
CloseMA threshold, GAP rules, tracked/local config behavior, focused tests, and
the required acceptance evidence. Do not start Tasks 2вЂ“12 in the same session.

Use TDD: add a narrow failing test, implement the smallest change, run focused
tests, then run the relevant broader checks and independent review.

## Agent Routing

- **Orchestrator:** root session coordinates scope, review, verification, and
  documentation; it does not silently substitute another executor.
- **Executor:** DeepSeek V4 Flash High through
  `codex_orchestration_executor_ac7ebbc33644`.
- **Reviewer:** independent GPT-5.6 Terra High review after the executor.
- One executor owns Task 1. Do not parallelize edits to the shared config
  contract.
- Executor must not commit, reset, revert unrelated changes, or touch the
  userвЂ™s untracked data/artifacts.

## Verification and Git Rules

Before implementation:

- inspect `git status --short` and preserve existing untracked artifacts;
- confirm only Task 1 is in scope;
- do not restart or stop the live panel/process unless explicitly requested.

Before accepting Task 1:

- focused Task 1 tests pass;
- relevant broader tests pass or failures are explicitly explained;
- `git diff --check` passes;
- staged/diff scope contains only Task 1 code, tests, and required docs;
- independent review returns `PASS`;
- update `progress.md` with verified evidence in the same scoped commit.

Known baseline evidence from this session:

- DuckDB Priority-1 focused run: `213 passed`;
- latest repository-wide evidence: `745 passed, 2 skipped, 4 failed`;
- the four failures are the known missing local HTTP fixture files;
- the two skips require unavailable Windows symlink rights.

The governance package and implementation documents are currently uncommitted
working-tree changes. Do not commit them automatically unless the user asks;
first inspect the complete diff and preserve unrelated untracked paths such as
`Input/`, `posttest_*`, `tests.zip`, and the supplied governance ZIP.

## Stop Conditions

Stop and report instead of proceeding if:

- Task 1 requires changing ADR-0007/0008;
- the executor proposes implementing Tasks 2вЂ“12 together;
- the current old surface is treated as a canonical Phase 1 surface;
- a test or code path requires schema v5, legacy-mode fallback, or automatic
  old-surface migration;
- the requested change conflicts with the active Phase 1 specification.
