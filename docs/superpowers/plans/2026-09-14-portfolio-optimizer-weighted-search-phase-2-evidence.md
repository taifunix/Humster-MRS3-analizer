# Weighted Portfolio Search — Phase 2 evidence

Date: 2026-09-14
Status: accepted after independent Opus `CODE_REVIEW_PASS` (Claude Opus 5, high, 2026-09-14).

## Scope

Phase 2 provides one read-only, fixture/fake-only preparation path from current
FINALIST PerformanceDB facts to weighted-search input. It reuses the existing
`input.py`, `reports.py`, `minute_capacity.py`, and `market_snapshot.py` seams.
No Phase 3 weighted generator or portfolio search was implemented.

The prepared contract includes the common UTC period, five-minute grid, stable
strategy ordering, T+1 interval boundaries, T×N normalized deltas, per-member
validity/reasons, reconstructed cycle facts, occupancy/hold/source-basis
diagnostics, and an injected mutable preparation cache. Source S is dynamic and
allowlisted exactly as confirmed in Phase 1:
`use_fix=false`, `use_upnl=true`, `use_frozen_balance=true`,
`balance_percentage_long=100`, `risk_long=1`, `max_balance=0`.

## Verification

Independent root verification ran:

```text
.venv\Scripts\python.exe -m pytest tests/test_portfolio_input.py tests/test_portfolio_reports.py tests/test_portfolio_minute_capacity.py tests/test_portfolio_market_snapshot.py -q
198 passed in 25.28s
git diff --check
```

The run used fixtures/fakes only. No real network, tester, exchange request,
database migration, or generated artifact was performed.

## Artifacts and files

- Implementation: `src/mrs3/portfolio/input.py`, `reports.py`,
  `market_snapshot.py`, and the existing `minute_capacity.py` seam.
- Public exports: `src/mrs3/portfolio/__init__.py`.
- Focused tests: `tests/test_portfolio_input.py`,
  `tests/test_portfolio_reports.py`, `tests/test_portfolio_market_snapshot.py`,
  `tests/test_portfolio_minute_capacity.py`.
- Contract correction: `docs/specs/2026-09-14-portfolio-optimizer-weighted-search.md`.
- Evidence file: `docs/superpowers/plans/2026-09-14-portfolio-optimizer-weighted-search-phase-2-evidence.md`.

## Review ledger

- Initial Opus findings R1–R10: fixed. This covers leading unseen-symbol
  carry-in reconstruction, common-window carry-in occupancy, recursive
  immutability, persisted limiter ownership and wrapping, cache identity,
  invalid-cell zeroing, DuckDB integration and typed source rejection,
  bounded retry delays, frozen decoded metadata, and T+1/T×N documentation.
- Compact retry Opus findings R-1–R-6: fixed. This covers NULL opening
  PnL/fee defaults, typed malformed numeric rejection, mapping-shaped 429
  retry exhaustion, malformed/non-object raw action JSON, exact non-destructive
  opening-source matching with an unmatched diagnostic, and typed strategy-ID
  and duplicate-participant validation.
- Latest Opus findings R-1–R-5: fixed. This covers equal-endpoint intra-cell
  attribution, strict deterministic LONG/unique-universe validation, original
  source-row ordinal fallback through timestamp sorting, required post-size
  validation with the leading-close exception, and this evidence file's own
  artifact path.
- One intermediate Opus bridge call timed out; it was not treated as
  `CODE_REVIEW_PASS`.

Final independent Opus `CODE_REVIEW_PASS` is recorded from Claude Opus 5 at high
effort on 2026-09-14. This evidence records the implementation, verification,
and accepted review disposition.
