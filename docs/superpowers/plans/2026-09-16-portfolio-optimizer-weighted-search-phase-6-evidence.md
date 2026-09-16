# Portfolio Optimizer weighted search — Phase 6 evidence

**Date:** 2026-09-16
**Status:** Phase 6 accepted.
**Review state:** Slice-level reviews and the final full-Phase-6 Opus high review
returned `CODE_REVIEW_PASS`.

## Scope and boundary

Phase 6 covers the strict weighted-search settings and migration, the frozen
Campaign snapshot with private weighted source rows, and the dedicated
`portfolio-weighted-mrs` template.  The template is frozen as JSON-safe data
with `mrs.position_priority=3`; its exact bytes have a Campaign digest.

The normal fact path is implemented through `run_portfolio_adapter()`: official
local `minute_capacity`/archive-backfill and official market loading are used,
with no fallback.  Optional injected market/archive callables are test seams.
Reference-derived conservative margin coefficients use the tiers covering
`[0, C]` for each member and fail closed when evidence is unavailable.

Portfolio search and PerformanceDB both read CPU width from the sibling
`config.local.json` `duckdb_import.workers` setting.  The shared loader validates
that value as a positive integer and supplies the canonical default `4` when the
local file/section is absent.  The API and archive-download concurrency fields
remain settings-document throttles; they are not forwarded as search workers.

The executable seam carries source geometry and leverage into JSON and verifies
`B`, `x`, `C`, `q=x/B`, `max_balance`, limiter `L`, priority, and leverage.  The
executable identity includes frozen Campaign/source evidence and exact payloads.
Typed JSON readback covers explicit `A0=sizing_base=B`, bounded full position,
quantity-step alignment, and the summed monetary `qtyStep` tolerance.

Weighted `Summary`/`Members` output exposes the required capital, risk, margin,
limiter, priority, sizing, two-reserve, and `UNKNOWN` diagnostics.  Legacy
Campaign modes remain rejected; legacy export/render and DuckDB finalist-input
contracts remain separate and unchanged.

The LONG 1/0 limit is enforced only by the new weighted Panel request parser.
The only production callers of `validate_campaign_contract()` and
`run_portfolio_adapter()` are this weighted Panel path; legacy export, render,
and DuckDB import do not construct or consume this Campaign envelope. Their
existing test suites verify compatibility without enabling a legacy fallback
inside the weighted adapter.

The source-derived planned leverage is written to
`strategy.basic.leverage` before `build_weighted_strategy_payload()` performs
its `allow_nan=False` JSON round trip. The adapter integration regression
asserts the returned payload values (`BTCUSDT=9`, `ETHUSDT=7`), so this is the
real serialized JSON rather than an upstream-only margin fact.

Each executable variant retains its original `search_identity` and complete
search `members`, including zero-allocation members; only executable strategy
payloads omit zero-allocation members. Duplicate executable digests are
rejected with typed `WEIGHTED_EXECUTABLE_IDENTITY_COLLISION` rather than
silently deduplicated, so no ambiguous executable is published.

No tester, Stage 2, network, Bybit, retest, or database write was run.  The
configured local minute root contains 3712 CSV files, but verification did not
use live data or network access.

## Verification

Individual evidence:

- `tests/test_portfolio_adapter.py`: **263 passed**.
- `tests/test_panel_portfolio.py`: **151 passed**.
- `tests/test_portfolio_minute_capacity.py`: **22 passed**.

Combined command from the root virtual environment:

```text
& ..\..\.venv\Scripts\python.exe -m pytest tests/test_portfolio_adapter.py tests/test_portfolio_config.py tests/test_portfolio_input.py tests/test_portfolio_margin.py tests/test_portfolio_minute_capacity.py tests/test_portfolio_export.py tests/test_portfolio_render.py tests/test_panel_portfolio.py tests/test_panel_static_ui.py tests/test_performance_v2_finalist_retest.py -q
930 passed in 146.60s
```

`git diff --check` returned exit code 0 (clean, apart from existing line-ending
warnings).  No generated artifact was added.

The reviewer-requested repository run produced `4443 passed, 7 skipped, 3
failed`.  Only those failures were rerun, per the explicit instruction not to
repeat successful tests.  The static Panel regression and common-worker test
were corrected and passed (`98 passed` and `4 passed`).  The unchanged
PerformanceDB HTTP test first reproduced its two-second timeout, then passed in
the Phase 6 tree (`1 passed in 3.18s`); the same exact test also passed at parent
commit `a452958` in an isolated worktree (`1 passed in 7.47s`).  This establishes
a scheduling-sensitive timeout rather than a deterministic Phase 6 catalog or
worker-source regression.

## Review and remaining gate

The settings, Campaign/source geometry, margin, workbook, production fact-path,
and executable identity/readback slices each have independent Opus
`CODE_REVIEW_PASS` evidence.  The final full-phase re-review resolved its worker,
timeout-baseline, throttle-separation, verification, identity-collision, and
path/validation questions and returned `CODE_REVIEW_PASS`; Phase 6 is accepted.

Stage 2 remains disabled and requires separate authorization.  Before any real
tester launch, Phase 7 must verify that Stage 2 consumes this exact executable
identity and payload without re-rendering or re-sizing it.

The active plan also records the separate Phase 10 liquidity-dynamics task:
store comparable daily per-symbol window and `C_i` snapshots together with
coverage, then build a time series/graph. Trend method and degradation
thresholds remain deliberately deferred; one snapshot is not treated as a
forecast.
