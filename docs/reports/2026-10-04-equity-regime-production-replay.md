# Equity regime production replay evidence

**Date:** 2026-10-04
**Classifier:** `equity-regime-v1` (`assess_equity_regime`)
**Scope:** Replay frozen M3 summaries only. The script does not open DuckDB, write to a live database, or read the original 139 million equity points.

## Frozen input and row output

- Input: `Output/EquityM3/2026-10-04/full/facts.jsonl`
- Input rows: **30,940**, all with frozen source status `READY` and no source reason.
- Input SHA-256: `0BC09CB470839679B7DB60BB2BE6552922CCB5EE0A20B78981E83235F8BB4416`
- Row output: `Output/EquityM3/2026-10-04/production-replay.jsonl` (ignored generated artifact)
- Output rows: **30,940**, all result IDs unique.
- Output SHA-256: `870C88D293067C2CB1E8A58B4A297E2C4234E7AC87DA509E225EA374F6CCE0AF`
- A second complete replay produced the same output digest.

Each canonical JSONL row contains `result_id`, `strategy_id`, `symbol`, `side`, `source_status`, `state`, `decision`, `rank`, and the full ordered `reasons` array returned by the production assessor.

## Replay counts

| State | Rows |
| --- | ---: |
| GROWING | 5,788 |
| WEAKENING | 9,091 |
| RESUMED | 3,086 |
| STALLED | 12,406 |
| DROP | 569 |
| NOT_EVALUATED | 0 |
| **Total** | **30,940** |

There were **0 valid `UNCLASSIFIED_GEOMETRY`** rows. All 30,371 non-DROP rows have decision `PASS`; 569 have decision `DROP`.

The complete reason arrays and their row counts were:

| Ordered reasons | Rows |
| --- | ---: |
| `DD_14_7_GTE_23`, `W28_DOWN` | 16 |
| `DD_14_7_GTE_23` | 25 |
| `PRE28_AND_W28_NOT_UP` | 176 |
| `W28_DOWN` | 352 |
| `GROWING` | 5,788 |
| `LOW_SPEED`, `TWO_STEP_SLOWDOWN` | 1,019 |
| `LOW_SPEED` | 7,831 |
| `RESUMED` | 3,086 |
| `STALLED` | 12,406 |
| `TWO_STEP_SLOWDOWN` | 241 |

The aggregate hard-reason counts are `DD_14_7_GTE_23`: **41**, `W28_DOWN`: **368**, and `PRE28_AND_W28_NOT_UP`: **176**. Hard-reason arrays retain the production classifier's order.

## Adapter and limits

The adapter supplies the production assessor with the frozen W28/W14/W7 and PRE28 `trend30`/`endpoint30`, DD14/DD7, HWM boundary values and timestamps, ATH stage counts, held W7 breakout, and final equity. It checks the frozen breakout flag against the stage count, previous W7 ATH, and close. The production assessor uses those inputs for every state and reason in this replay.

The frozen summaries do not contain report start/end timestamps, exact window endpoint equity values, or complete per-stage ATH event arrays. They contain stage counts and first/last event summaries, which are not sufficient to reconstruct the full arrays. Those omitted fields are not used by `assess_equity_regime`; they are not serialized as if reconstructed. Consequently this replay verifies classification from frozen summary facts, not an independent recomputation of those facts from the raw curve.

## Runtime and checks

Two full runs took **8.78 s** and **8.52 s**. Windows peak working set was **21,962,752 bytes** and **21,889,024 bytes**, respectively, measured using the standard Windows process memory API. No additional dependency was used.

Focused checks:

```text
.venv\Scripts\python.exe -m pytest tests/test_equity_regime_m3_replay.py tests/test_performance_v2_equity_regime.py --basetemp <resolved C: TEMP path>
34 passed
```

`git diff --check` completed without whitespace errors. No database was opened or changed, and no commit was created.
