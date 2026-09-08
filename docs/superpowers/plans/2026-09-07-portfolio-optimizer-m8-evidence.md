# Portfolio Optimizer M8 evidence

**Status:** `ACCEPTED_FIXTURE_BOUNDARY`

M8 implements deterministic exact export, PortfolioSet manifests, human-readable
reporting, and Portfolio-DB-only decision replay on fixtures. The implementation
does not run a tester, contact a network or bot, or write a production database.
The separately authorized real joint test remains pending.

## Implemented boundary

- Injected exchange-reference freshness/quality and shared-liquidity gates.
- Exact tested/exported settings comparison with strongest-gate `NEEDS_RETEST`.
- Separate execution and decision Campaign linkage with durable-evidence-only
  Evaluation refresh.
- Deterministic one-file-per-symbol export that preserves repeated-symbol
  accounts, plus portfolio and PortfolioSet manifests and a human report.
- Composition/member/load digest and `NEEDS_RESCREEN` without invalidating the
  single-account TradingRun.
- Required deposits, caps, scheduling, periods, identities, equity/result/DD,
  metrics, gates, limitations, and reasons in exported evidence.
- Recursive secret/live-command redaction and explicit tick-replay availability.
- Replay from Portfolio DB without Performance DB or raw HTML.

When settings are verified but the exchange reference or shared-liquidity
evidence is stale, missing, or otherwise unknown, the export remains
`RESEARCH_ONLY`, records the blocking gate reason, and does not refresh an
Evaluation. `RESEARCH_ONLY` is a research disposition and never a clean-pass
or live-authorization signal; settings or execution evidence blockers retain
`NEEDS_RETEST` precedence, while a changed composition uses `NEEDS_RESCREEN`
when no retest blocker applies.

Publication uses same-directory staging and a recoverable sibling backup. A
process crash can occur after the previous target is renamed to its backup and
before staging is installed, leaving the target absent but the old content
available in that backup; ordinary replacement exceptions restore the target.

## Verification

- Final root export/integration suite: `.venv\\Scripts\\python.exe -m pytest tests/test_portfolio_export.py tests/test_portfolio_integration.py -q` - `95 passed in 53.51s`.
- Related live monitor/store and integration slice: `.venv\\Scripts\\python.exe -m pytest tests/test_portfolio_export.py tests/test_portfolio_integration.py tests/test_portfolio_live_store.py tests/test_portfolio_live_monitor.py -q` - `119 passed in 48.95s`.
- Complete project suite: `.venv\\Scripts\\python.exe -m pytest -q` - `3237 passed, 7 skipped, 1 failed in 1045.33s`; the sole failure was a two-second local HTTP timeout in the pre-existing `test_v2_catalog_and_windows_http_are_typed_and_repeatable` Performance-v2 test. Immediate isolated rerun of that exact test passed: `1 passed in 3.30s`.
- `py_compile` passed.
- `git diff --cached --check` -> `PASS`.
- All Markdown links added by the M8 commit resolve in its staged tree; the M6
  and M7 ledgers were already tracked, while U1 remains a separate commit.

## Independent review

Claude Opus 5 high completed a fresh three-round review through the standard
Codex Orchestration `review_code` route after the earlier review history.

- Fresh round 1 returned R1-R10 covering repeated-symbol slots, durable attempt
  pinning, unredacted settings comparison, mandatory shared liquidity,
  persistence ordering/idempotency, PortfolioSet dispositions and error surfaces.
- Fresh round 2 confirmed those fixes and returned four residual findings on
  Python 3.11 syntax, tri-state rollback probes, safe symbol filenames and dead code.
- Fresh round 3 verified all residual fixes and returned `CODE_REVIEW_PASS`.
- Subsequent full staged-package reviews found and verified corrections for
  documentation scope, publication recovery, fail-closed status derivation,
  recursive value redaction, reference-gate evidence, deterministic account-cap
  fallback and missing symbol settings. The final re-review also required the
  complete-suite evidence recorded above.

M8 fixture/research boundary is accepted. The unchecked real joint-test item in
the active plan still requires separate explicit user authorization.
