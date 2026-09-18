# Portfolio Optimizer weighted search Phase 9 evidence

Date: 2026-09-18. Status: accepted after independent Opus
`CODE_REVIEW_PASS`.

## Accepted behavior

- Panel accepts arbitrary nonnegative LONG/SHORT finalist limits and rejects a
  selected symbol with both directions disabled.
- User Rank selects the deterministic top-N pool for each canonical
  `(symbol, side)` slot. The adapter uses the exact Cartesian product, with one
  finalist from every enabled surviving slot, and evaluates every composition
  for every profile.
- User Rank does not influence the common final ordering. Each profile retains
  a bounded global top-K by p30, CDaR80, required bank and deterministic
  identities. A different chosen composition remains a different executable
  identity even when its positive weights and metrics match another result.
- Same-symbol LONG and SHORT members coexist but share one symbol capacity in
  every LP and validation seam. Margin is additive without directional netting;
  limiter replay treats both strategies as distinct slots.
- The common strategy template is cloned into deterministic LONG or SHORT
  payloads. The selected side fields are populated, opposite-side fields are
  zeroed, and Panel/XLSX output preserves both rows and their side.
- `PORTFOLIO_WEIGHTED_CAMPAIGN_V1 / WEIGHTED_V1 / WS1.2` is the sole executable
  gate. WS1.1 remains historical evidence and has no compatibility branch.

## Review

The configured independent reviewer was Opus high. Three finding rounds fixed
numeric pool ordering and rank validation, profile-scoped ordinary failures,
bounded global top-K validation, canonical fact lookup, shared-cap propagation,
directional payload cloning, Panel validation, exclusion provenance and full
chosen-composition identity. The final disposition was `CODE_REVIEW_PASS`.

## Verification

Focused Phase 9 contour:

```text
.venv\Scripts\python.exe -m pytest tests/test_portfolio_input.py tests/test_panel_portfolio.py tests/test_panel_static_ui.py tests/test_portfolio_adapter.py tests/test_portfolio_weighted_search.py tests/test_portfolio_position_sizing.py tests/test_portfolio_margin.py tests/test_portfolio_export.py -q
1093 passed in 211.15s
```

Final repository suite:

```text
.venv\Scripts\python.exe -m pytest tests -q --junitxml=.pytest-phase9-full.xml
4552 passed, 7 skipped, 25 warnings in 1028.47s
```

`git diff --check` passed. The temporary JUnit report was removed after its
exit code and summary were recorded.

## Boundaries

This evidence is offline and local. It does not authorize or claim a real
tester run, network access, production database mutation, live execution,
recommendation readiness, Phase 10 additive search, or Phase 11 sizing grids.
