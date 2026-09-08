# Portfolio Optimizer U1 implementation evidence

Status: `ACCEPTED`

U1 adds the existing Panel SPA screen and a local server adapter for Stage 1.
It freezes exact config bytes, launch fields, versions and the current FINALIST
order before creating Campaign/job identities. Selection and User Rank cutoff
remain in `src/mrs3/portfolio`; the Panel adapter calls package search and
reports its `OPEN_POLICY` result without inventing ranking, sizing, liquidity,
freshness or individual-DD defaults.

The server provides settings GET/PUT with exact-byte CAS and atomic verified
rollback, readiness, one active Campaign job, persisted progress/journal,
restart-to-`INTERRUPTED`, cancellation, success-only deterministic XLSX and
typed HTTP errors. Publication verifies the workbook before the registry enters
`COMMITTED`; failures expose neither results nor downloads. Workbook paths stay
inside server-owned roots, formulas, links, local paths and secret-like fields
are rejected or redacted. Stage 2 remains permanently blocked with
`PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED`.

Self-review corrections include source snapshot freezing before identity
creation, duplicate-before-busy ordering, publication rollback, indeterminate
stage progress, stable journal entries, frontend numeric/budget validation,
active-job recovery after conflicts, deterministic metadata hiding, formula
sanitization and grouped exclusion/profile summaries.
Deletion or loss of the settings file after Campaign freeze is also reported as
`SETTINGS_CHANGED_SINCE_FREEZE`; queued cancellation remains terminal if the
worker observes it before entering `RUNNING`.
The launch screen now implements the specified shared LONG/SHORT maxima,
copies them only when a pair is selected, renders the pair checkbox, refreshes
readiness before a recovered job starts a new Campaign, and validates Campaign
money against the same `DECIMAL(38,12)` boundary in both browser and server.
Queued jobs also keep a truthful `QUEUED` stage status.

Verification performed only with fixtures/fakes and the repository `.venv`:

- `tests/test_panel_portfolio.py`: 29 passed;
- `tests/test_portfolio_input.py`: 42 passed;
- `tests/test_panel_static_ui.py`: 80 passed;
- final isolated U1 suite after separating TDD-2B-9 chart work and resolving
  both Opus finding rounds: 254 passed;
- final residual root verification: 101 passed;
- combined affected U1/M7/M8/Panel/audit verification: 450 passed;
- complete project suite: 3129 passed, 7 skipped, 8 pre-existing warnings;
- `node --check src/mrs3/panel_web/app.js`: passed;
- `py_compile` for Panel, adapter, audit and portfolio input modules: passed;
- `git diff --check`: passed apart from Git line-ending notices.

No real tester, bot, remote target, production database, credentials or live
trading action was used. Claude Opus 5 high reviewed the complete U1 diff,
verified all eight corrections and the final typed-GET residual, and returned
`CODE_REVIEW_PASS`. The scoped U1 commit is `7ddbb94`.
