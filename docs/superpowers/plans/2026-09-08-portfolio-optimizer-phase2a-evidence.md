# Portfolio Optimizer Phase 2A evidence

**Status:** `ACCEPTED_FIXTURE_BOUNDARY`

Phase 2A adds immutable `RESEARCH_ONLY` execution evidence from fixtures:
requested and cumulative fill quantities, lifecycle timing and reserve, strata
and degradation curves, compatibility with existing liquidity/margin inputs,
stress lineage and append-only correction provenance. It does not change
accepted sizing, capacity, reserve, margin, admission or readiness outputs.

## Verification

- Execution research suite: `67 passed in 1.87s`.
- Required execution/liquidity/margin/store suite: `280 passed in 86.59s`.
- Complete project suite: `3327 passed, 7 skipped, 1 failed in 1047.37s`.
  The only failure was the pre-existing two-second localhost timeout in
  `test_v2_catalog_and_windows_http_are_typed_and_repeatable`; its immediate
  isolated rerun passed: `1 passed in 3.36s`.
- `py_compile` passed for the implementation module.
- `git diff --check` passed apart from line-ending notices.

## Independent review

Claude Opus 5 high reviewed the complete Phase 2A package in three rounds.
Corrections covered cumulative-fill high-water preservation, symmetric fill
aliases, strict revision aliases and canonical typed evidence. The final round
returned `CODE_REVIEW_PASS`.

No real API, credentials, tester, bot, trading action, live deployment or
production database was used. Any sizing, capacity, reserve or margin change
still requires a new immutable `NEEDS_RETEST` lineage and a separately
authorized joint retest.
