# Portfolio Optimizer M4 evidence

schema: portfolio_optimizer_m4_evidence_v1
version: 1
date: 2026-09-06
source_baseline: 3e837cb
status: ACCEPTED
review_disposition: CODE_REVIEW_PASS
executor: GPT-5.6 Luna (xhigh)
reviewer: Claude Opus 5 high

## Boundary

M4 is fixture-only proposal search and rendering code. It adds no runtime entry
point and does not call a tester, bot, exchange API, collector, or real database.
It does not grant `RECOMMENDATION_READY`, trading admission, or live use.

## Implemented contracts

| Contract | Evidence |
|---|---|
| Admission and proposals | `src/mrs3/portfolio/search.py` uses exact `FINALIST` identities, deterministic LONG/SHORT/BOTH PairSlots, finite percentage grids, limiter and priority including zero. |
| Gates and sizing | Every attempted variant follows structural, liquidity, margin and individual-DD gate order; quantity rounds down and invalid orders remain auditable. Missing ceilings, D100, equity, currency, timestamps or policy fail closed. |
| Ordering and identity | Development ranking requires versioned ordered metrics and a canonical tie-break. Passing variants have a separate PnL/initial-margin scheduling order. Campaign identity covers inputs, tried and excluded variants, grid, budget, ranking, order and seed. |
| Renderer | `src/mrs3/portfolio/render.py` emits one manifest-shaped JSON per symbol, preserves directional geometry/runtime and dedicated close, requires exact per-symbol leverage where needed, and performs reverse typed comparison. |
| Stable output | M4 adds only the three ADR-0030 `portfolio_reason_v2` values while retaining v1 unchanged; returned mappings are immutable and Decimal work is isolated from ambient context. |

## Verification

Focused M4 command:

`.venv\Scripts\python.exe -m pytest tests/test_portfolio_search.py tests/test_portfolio_render.py tests/test_portfolio_canonical.py tests/test_portfolio_disposition.py -q`

Result after Opus finding fixes: `66 passed`.

Relevant combined verification covers portfolio M1–M4, collector
reference/archive/aggregation/storage and Performance finalist selection.
Result: `545 passed, 1 warning` in 107.08 seconds. The warning is the existing
pandas downcast future warning in `performance_v2_selection.py`.

The combined run completed on the final post-fix worktree before
`2026-09-06T18:45:14+02:00`. Because review preceded commit, the exact Git blob
attestation is:

| Path | Git blob |
|---|---|
| `src/mrs3/portfolio/margin.py` | `00a57ffedab50ee47f31a3165df06c4cce6576ec` |
| `tests/test_portfolio_margin.py` | `5a156c6d5ec16e238591b203fbd612bee0f96fa2` |
| `src/mrs3/portfolio/search.py` | `6f970b5079ef3e4a6060b64f398d065dcf932976` |
| `src/mrs3/portfolio/render.py` | `91dce9e787ab5529b94baa4d9676de9023b2ed8d` |
| `src/mrs3/portfolio/canonical.py` | `5d0f9e559a6e608a31eb14537db38c888205f46d` |
| `src/mrs3/portfolio/disposition.py` | `05b5a4572018eeb8117c6a84472457824b6a1aa7` |
| `tests/test_portfolio_search.py` | `d53be777342b42e5e6cf6074717468e50144bd03` |
| `tests/test_portfolio_render.py` | `40d579b9b7fe729f53b5ab6683cd1494e5f53368` |
| `tests/test_portfolio_canonical.py` | `640bb960ab02345eec773de959f3c412fb7fbdfb` |
| `tests/test_portfolio_disposition.py` | `4187b0a858915b8e59eb63c601f8624dba76c06e` |

`compileall` and `git diff --check` pass. Root review fixed Campaign coverage of
failed attempts, scheduling order before downstream handoff, strict typed
leverage comparison, direct PairSlot symbol validation, and fail-closed reverse
comparison.
Both M3 and M4 Decimal contexts also pin exponent bounds independently of the
caller's ambient context.

`LEVERAGE_UNVERIFIED` remains an additive v2 reason reserved for the M5
applied-leverage readback boundary. M4 validates planned leverage and does not
invent a runtime readback gate.

## Independent review status

Claude Opus 5 reviewed M4, identified correctness and verification gaps, and
confirmed their fixes in independent re-review. Final disposition:
`CODE_REVIEW_PASS`.

## Handoff

M4 is accepted. M5 is the next implementation stage. U1 remains a separate track
near M8 after accepted backend API scope; Stage 2 also requires accepted M5/M6
and separate user authorization.
