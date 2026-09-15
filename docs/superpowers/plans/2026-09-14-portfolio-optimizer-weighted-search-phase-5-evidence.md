# Portfolio Optimizer weighted search — Phase 5 evidence

**Date:** 2026-09-15
**Status:** Phase 5 accepted after independent implementation review
**Review state:** Opus `CODE_REVIEW_PASS`; the P5-R5a-T1 memory-clamp regression was corrected and re-reviewed

## Scope and boundary

Phase 5 implements the bootstrap, budget, and shortlist paths required by the
approved weighted-search plan: direct bank calculation, shared bootstrap
blocks, nearest-rank quantiles, reproducible seeds, conditional screening and
prefix reuse, short-window diagnostics, partial manifests and time budgets,
shortlist limits, the single additional LP path, and CVaR/CDaR alternatives.
The implementation is still fixture/local evidence only. The production MVP
remains one LONG finalist per symbol; no runtime release, real tester, Bybit,
network, retest, or database write is authorized by this evidence.

## Input and factual matrix

The user-directed benchmark used the 13 prepared explicit imported User
FINALIST rows available locally (13 strategies across 10 symbols), rather than
the unavailable 15–21-row target. Three duplicate-symbol benchmark-only
aliases exercised `N=13`; they do not change the production one-LONG-per-symbol
MVP.

The factual matrix has `T=12672` at a 5-minute cadence over
2026-07-27..2026-09-09, with 0 invalid cells, 6519 actions, and 65646 equity
samples. Local seven-day minute-data capacities are READY for all 10 symbols.
No network, Bybit, tester, retest, or database write was used.

## Stage benchmark

| Stage | Wall | CPU | Peak RSS |
| --- | ---: | ---: | ---: |
| Input load | 3.197 s | 10.594 s | 423.47 MiB |
| Preparation | 5.852 s | 5.813 s | 243.50 MiB |
| Direct LP / HiGHS Optimal | 29.016 s | 40.719 s | 387.42 MiB |
| Replay, 13/13 MODEL | 1.697 s | 1.672 s | 251.51 MiB |
| Margin factual | 0.113 s | not recorded | not recorded |
| Render | 0.165 s | not recorded | not recorded |

Preparation now uses indexed dense-equity lookup; the measured preparation
stage is about 5.85 seconds. Factual margin coefficients were unavailable, so
the margin result remains `UNKNOWN`; no coefficients were invented.

## Bootstrap and weighted-search runs

An isolated bootstrap with an actual nonzero LP vector ran 3000 scenarios:

| Workers | Result | Wall | CPU | Peak RSS |
| ---: | --- | ---: | ---: | ---: |
| 1 | PASS | 387.581 s | 387.156 s | 262.20 MiB |
| 30 | PASS | 43.589 s | 298.172 s | 1.801 GiB |

Semantic outputs were equal after excluding operational fields. The measured
speedup is 8.89x. The manifest width was 30, while the observed process
maximum was 13; the reason for that observation is not established and is not
overclaimed here.

The full base `weighted_search` run used `K=8`, `wall=900 s`, and
`solver=30 s`:

| Workers | Result | Wall | Scenarios/candidates | Peak RSS |
| ---: | --- | ---: | --- | ---: |
| 1 | `WALL_TIME_LIMIT` (budget-limited) | 982.201 s | 1100/3000 | 394.63 MiB |
| 30 | PASS | 269.187 s | 3000 scenarios / 8 candidates | 4.232 GiB external |

The original 30-worker run used 175.920 seconds in bootstrap and made 8 LP
calls. Repeating that factual bootstrap after moving the spawn target to a
lightweight worker module took 119.378 seconds, a 32.1% wall-time reduction;
serial and process semantic outputs remained equal. The full outputs are not
equal across the original 1-worker and 30-worker runs because the 1-worker run
is correctly partial.

A `batch_size=50` trial completed in 191.471 seconds versus 175.920 seconds
for the existing `batch_size=100` topology (about 8.8% slower); its topology
was 60 tasks versus 30 and its manifest differed. It is rejected, with no
code or default change.

The experimental defaults are kept as `K=8`, `wall=900 s`, and `solver=30 s`,
based on the completed 30-worker run. The single worker control is the existing
`duckdb_import.workers` setting; this comparison used only 1 versus 30 and did
not add a second setting or hard-coded 16-worker path.

## Verification and public-output boundary

- Focused verification command: `.venv\Scripts\python.exe -m pytest tests/test_portfolio_weighted_search.py tests/test_portfolio_candidate_search.py tests/test_portfolio_pretest_proxy.py -q` — **233 passed in 48.33 s; exit code 0**.
- Relevant broader verification command: `.venv\Scripts\python.exe -m pytest @(Get-ChildItem -LiteralPath tests -Filter 'test_portfolio_*.py' | Select-Object -ExpandProperty FullName) -q` — **1283 passed in 310.24 s (0:05:10); exit code 0**.
- `.venv\Scripts\python.exe -m compileall -q src/mrs3 tests` — **exit code 0**.
- `git diff --check` — **exit code 0 (clean)**.
- Tests cover that calculated equity, action, cycle, and scenario paths are not
  leaked through public variants. No generated artifact was committed.

The first Opus module review found missing worker RSS evidence, uncontained
process-pool failures, unchecked worker payload shapes, an incomplete memory
width estimate, and inaccurate worker diagnostics. Luna corrected those
confirmed findings with focused regressions: worker RSS is reported, pool
failures produce a factual incomplete manifest, each worker group is accepted
only when task and payload identity plus count shapes match, memory width
includes resident and batch-dependent work, and the manifest records the
actual clamped pool width. Opus issued `CODE_REVIEW_PASS` after the P5-R5a-T1
test was corrected to distinguish the previous memory estimate from the
current resident-worker and temporary-matrix estimate.

Further speed analysis is future work only: the next measured hotspots are
`bank_for_path` bootstrap Decimal work and repeated LP model assembly. Neither
optimization is claimed as implemented, and the rejected `batch_size=50` trial
is not a blocker.

## Review outcome and next step

Phase 5 implementation, tests, benchmark, and experimental-default decision
are evidenced above. Opus issued `CODE_REVIEW_PASS` after verifying P5-R4a,
P5-R5a, P5-R5a-T1, and P5-DOC1. The next step is the scoped commit/push and
Phase 6. This document does not establish portfolio performance, trading
readiness, or permission for a real tester run.
