# Weighted Portfolio Search — Phase 6 accepted handoff

Date: 2026-09-16. Working branch: `feat/weighted-phase6`.

This handoff records the accepted Phase 6 implementation. Slice-level reviews
and the final full-Phase-6 Opus high review returned `CODE_REVIEW_PASS`.

## Accepted implementation surface

- Strict `search.weighted_search` settings, v1/v2 migration, immutable parsed
  values, and the existing `duckdb_import.workers` boundary.
  Missing local config uses its canonical default of 4; PerformanceDB and the
  weighted search do not introduce a second worker setting.
- Long-only weighted Campaign normalization: one LONG finalist per symbol,
  `max_finalist_long=1`, `max_finalist_short=0`.
- Frozen Campaign source evidence: private weighted rows retain raw series only
  inside the frozen internal artifact; public variants/workbooks/API do not expose
  raw action/equity series.
- Dedicated `portfolio-weighted-mrs` template frozen as JSON-safe data and exact
  byte digest; the template keeps `mrs.position_priority=3`.
- Normal `run_portfolio_adapter()` fact path using official local
  `minute_capacity`/archive backfill and official market loading, with no
  fallback. Injected market/archive callables are test seams.
- Reference-derived conservative margin coefficients use tiers covering `[0,C]`
  and fail closed when required facts are unavailable.
- Executable source geometry, leverage, `B/x/C/q/max_balance`, limiter `L`,
  and priority are assembled into JSON without legacy sizing rewrites.
- Deterministic executable identity binds frozen Campaign/source evidence and the
  exact executable payload. Typed JSON readback covers explicit
  `A0=sizing_base=B`, full position, quantity-step alignment, and tolerance.
- Weighted Summary/Members projection exposes the required capital, risk, margin,
  limiter, sizing, two-reserve, and `UNKNOWN` diagnostics.
- Legacy Campaign modes remain rejected; legacy export/render and DuckDB
  finalist-input contracts remain separate and unchanged.

## Verification

- `tests/test_portfolio_adapter.py`: 263 passed.
- `tests/test_panel_portfolio.py`: 151 passed.
- `tests/test_portfolio_minute_capacity.py`: 22 passed.
- Combined root-venv command covering adapter/config/input/margin/minute-capacity/
  export/render/Panel/static UI/finalist retest: 930 passed in 146.60s.
- `git diff --check`: clean, with existing line-ending warnings only.
- The three exact failures from the reviewer-requested full run were isolated:
  the two Phase-6-related tests now pass, and the unchanged two-second
  PerformanceDB HTTP test passed both on parent commit `a452958` and on the
  current tree after one scheduling-sensitive timeout.
- The configured local minute root contains 3712 CSV files; these checks did not
  use live data or network access.

## Safety and remaining gate

No tester, Stage 2, Bybit, network, retest, or database write was run. The Panel
Stage 2 route remains disabled and returns
`PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED`. Before any real tester launch, Phase 7
must verify that Stage 2 consumes the exact Phase 6 executable identity and
payload without re-rendering or re-sizing it.

Slice-level independent reviews and the final Opus high re-review returned
`CODE_REVIEW_PASS`. Evidence is recorded in
[Phase 6 evidence](2026-09-16-portfolio-optimizer-weighted-search-phase-6-evidence.md).
