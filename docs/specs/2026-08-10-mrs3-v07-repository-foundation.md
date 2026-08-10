# MRS3 v0.7 repository foundation — design

**Status:** approved for implementation on 2026-08-10

## Purpose

Turn this directory into the single Git repository `taifunix/Humster-MRS3-analizer` and make the existing MRS3 v0.6 implementation the starting codebase for v0.7. Version 0.6 remains historical context, not an immutable product branch.

## Target layout

```text
src/mrs3/       current v0.7 Python package
tests/          current v0.7 automated tests
scripts/        standalone data import and materialization tools
docs/specs/     approved, feature-specific requirements
docs/decisions/ durable architecture and product decisions
docs/archive/   historical v0.6 sources and superseded handoffs
AGENTS.md       session reading order and contribution rules
PRD.md          product scope, delivery map, and active-spec registry
progress.md     verified current state, next action, and blockers
README.md       public project introduction and reproducible setup/use
```

The prior `programs/MRS3_v0.6` package is moved into the root package layout. The prior HTML-to-DuckDB importers become scripts and retain their explicit v3/v4 compatibility relationship. Documents that describe v0.6 as the current implementation are archived; their historical facts remain linkable.

## Documentation model

`PRD.md` is the authoritative high-level index. It lists product objectives, non-goals, current phase, accepted decisions, and every active specification with status and dependency links. It does not duplicate feature details.

Each independently deliverable capability has a focused file in `docs/specs/`. A specification states scope, input/output contracts, invariants, acceptance criteria, and links to prior context. Once implementation starts, changes to a spec are recorded as an explicit decision or a succeeding specification rather than silently rewriting history.

`progress.md` is concise operational state: timestamp, current feature, last verified command/result, next concrete action, and blockers. It is updated whenever a work session changes that state, and does not become a second PRD or a changelog.

`docs/decisions/` holds short ADR-style records for choices that change architecture, data contracts, safety rules, or project workflow. Each decision links to the PRD and affected specs.

`README.md` is maintained from verified behavior only. It describes the project, prerequisites, installation, commands, input/output contracts, and current maturity. It links inward to PRD and the public-facing relevant documentation, but not to private paths or unverified claims.

## New-session reading protocol

1. Read `AGENTS.md`.
2. Read `PRD.md`.
3. Read `progress.md`.
4. Read the active feature specification named by `progress.md`, plus only the dependencies linked there.
5. Read the exact code, tests, and decision records named by that specification.
6. Read archived v0.6 material only when an active spec explicitly needs its algorithmic rule or migration provenance.

This sequence supplies current intent first and minimizes needless historical context.

## Initial v0.7 delivery map

1. Establish repository, root package layout, documentation rules, and reproducible development setup.
2. Validate v4 DuckDB import output without deleting source HTML.
3. Implement and validate the common-window materializer and unified legacy input.
4. Upgrade selector behavior for v0.7 `legacy_trades_proxy`, including event eligibility, representative order, full rebuild, and audit.
5. Generate and test v0.7 candidates, then calibrate the optional Source-PnL pre-test filter from actual results.

Real-event mode and portfolio simulation remain future work until their required raw coverage and time-series evidence exist.

## Migration and Git rules

The root is initialized as a Git repository with default branch `main`; its intended remote is `github.com:taifunix/Humster-MRS3-analizer.git`. Creating or pushing the remote requires a separately authenticated GitHub action.

The tester installation for this machine is a local runtime fact, not portable project configuration: its real path is stored only in an ignored local configuration file created from a tracked example, never in tracked documentation, README, committed configuration, test fixtures, or a remote URL. Code resolves and validates all tester paths from that configuration; it must not hard-code another machine path.

The initial migration preserves code and tests before semantic v0.7 changes. Each subsequent feature is implemented from an approved specification, includes relevant test evidence, updates `progress.md`, and updates README only when user-visible behavior changes. Commits are small, scoped, and never include generated test results, local databases, raw HTML, credentials, or machine-specific configuration.

### Required pre-commit sequence

Every commit follows this order:

1. Confirm the active specification and the exact files in scope.
2. Implement the smallest coherent change and add or update the corresponding tests.
3. Run the focused tests, then the relevant broader suite; record the actual command and outcome in `progress.md`.
4. Ask a separate review agent to inspect the uncommitted diff for correctness, regression risk, test coverage, safety, and compliance with the active specification. The implementing agent must not review its own change as the required review.
5. Resolve every confirmed review finding, or document a reasoned rejection in the relevant decision record; re-run affected tests after any change.
6. Update the specification only for an approved requirement change, update PRD when scope/status changes, and update README only for verified user-facing behavior.
7. Inspect `git diff --check`, `git status`, and the staged diff. Stage only the scoped files and create one conventional commit.
8. Record the commit hash and the next state in `progress.md`; that progress update is included in the same commit when it describes that commit.

A commit is forbidden if the independent review did not happen, required tests have not been run, scope is unclear, or generated/local data is staged. Pure documentation commits use the same review and diff checks, but do not require unrelated application tests.

### Commit boundaries

- `docs:` — one approved specification, decision, or documentation-system change.
- `chore:` — repository setup, dependency metadata, ignore rules, and non-functional tooling.
- `refactor:` — behavior-preserving migration or internal restructuring, with existing tests retained.
- `feat:` / `fix:` — one independently testable product behavior; include its tests and required documentation updates.
- `test:` — test-only improvements when they do not change production behavior.

Do not combine repository migration, a v0.7 feature, dependency upgrades, generated outputs, and README redesign in one commit. The first commits are: repository/documentation foundation; root-layout migration; then one commit per v0.7 feature specification.

## Acceptance criteria

- The root package and test layout contain the migrated v0.6 code as the v0.7 starting point.
- `AGENTS.md`, `PRD.md`, `progress.md`, and `README.md` state and cross-link the documentation workflow.
- The PRD references active specs rather than duplicating them.
- Historical documentation is separated from active v0.7 documentation and remains linkable.
- Git metadata and ignore rules make the repository safe to commit and publish.
- The tester root is configured locally and has no committed machine-specific value.
- Every commit has recorded independent-agent review and proportional verification evidence.
