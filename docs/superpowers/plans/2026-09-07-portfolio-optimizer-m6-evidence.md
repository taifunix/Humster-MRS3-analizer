# Portfolio Optimizer M6 evidence

**Status:** `ACCEPTED`

M6 implements the fixture/fake-only report normalization, portfolio metrics,
transactional DuckDB publication/readback, and cleanup-proof boundary described
by section 9 of the active specification. The physical joint tester report
schema remains Q06 `BLOCKING_UNKNOWN`; no real tester, network, bot target, or
production database was used.

## Implemented boundary

- Strict versioned fixture decoding with exact Decimal values, fixed-width UTC
  timestamps, stable source order, provenance, raw digest, and semantic digest.
- Position-cycle reconstruction including partial reductions, carry-in,
  open-at-end censoring, and explicit reversal ambiguity.
- Equity-based result and drawdown, separate realised PnL, coverage/gaps,
  concurrency, margin guard, leverage evidence, and financial reconciliation.
- Atomic multi-member publication followed by fresh-connection durable readback.
- Cleanup only after a typed proof binds run/attempt, raw and semantic digests,
  parser/metrics versions, replay counts, normalized facts, and validation state.
- Unknown physical report input fails closed as `Q06_BLOCKING_UNKNOWN` and keeps
  the report evidence.

## Verification

- Accepted-tree focused command:
  `.venv\Scripts\python.exe -m pytest tests/test_portfolio_reports.py tests/test_portfolio_metrics.py tests/test_portfolio_store.py tests/test_portfolio_runner.py -q`
  -> `147 passed in 23.37s`.
- Full repository run after the first remediation set:
  `2972 passed, 7 skipped, 8 warnings in 867.61s`.
- `py_compile` for the four M6 runtime modules passed.
- `git diff --check` passed apart from informational Windows line-ending notices.

## Independent review ledger

Claude Opus 5 high reviewed the complete M6 packet three times through the
standard Codex Orchestration `review_code` route.

- Round 1: `CODE_REVIEW_FINDINGS`, R-1 through R-10.
- Round 2: confirmed R-1 through R-10 resolved; returned R-11 through R-15.
- Round 3: confirmed R-11 through R-15 resolved; returned R-16 and R-17 plus
  low-severity R-18 robustness notes.
- R-16, R-17, and the actionable R-18 notes were implemented. A later fresh full
  review found ten additional boundary cases covering closed vocabularies,
  censored realised PnL, publication order, cleanup proof completeness, alias
  collisions, availability, determinism classification, reversal attribution,
  partial cleanup, and irregular sampling.
- Those ten findings were fixed with focused regressions. A final independent
  re-review of the exact accepted tree returned `CODE_REVIEW_PASS`.

M6 is accepted. Real tester execution and cleanup of real reports remain
unauthorized.
