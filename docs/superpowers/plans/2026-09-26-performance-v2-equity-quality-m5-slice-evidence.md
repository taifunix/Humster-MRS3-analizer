# Performance v2 equity-quality M5: bounded real-data slice

Status: partial measurement evidence, 2026-09-26. This does not accept M5 or
the full-corpus speed/RSS budgets.

The user approved deriving a partial offline corpus from the existing
PerformanceDB. The new `scripts/create_performance_v2_equity_benchmark_slice.py`
opens the schema-v5 source read-only, deterministically selects at most 512
ACTIVE current-result strategies from one Pair+Side group, and publishes a new
schema-v6 database outside the repository. It copies only cohort-related
strategies/results, order-referenced plateaus, orders, actions, equity,
window metrics, tags and (for a v6 source) equity-quality facts. Import and
selection histories and optimizer-prepared inputs are intentionally omitted.
Source and target column signatures, source-scoped row counts, current-result
pairs, catalog coverage and source file identity are verified before exclusive
publication. No migration or write is issued against the source.

The real source was schema v5, 14,119,350,272 bytes. A one-strategy smoke
slice succeeded first (320 actions, 3,683 equity points, seven window rows).
The 512-strategy `FWDIUSDT/LONG` slice then succeeded with:

| Item | Count |
| --- | ---: |
| Strategies / current results | 512 / 512 |
| Actions | 288,464 |
| Equity points | 2,348,395 |
| Window metrics | 3,584 |
| Orders / order-referenced plateaus | 1,441 / 111 |

The published v6 file is 233,582,592 bytes. Selected strategy-ID SHA-256:
`d89a89980d514ff070e67eef5869256bfabba4730b95de7806ef2bb7b142567c`.
File SHA-256: `b46e51a49facd9cce605012f18c933d1c686d88ba2a4fe5151aafad4a06f2b83`.
The source size and mtime were unchanged after the smoke, 512 extraction and
benchmark. No temp/WAL/spill artifact remained beside the published slice.

The existing copy-only benchmark ran one warmup and three measured all-warm
previews for each mode on scratch copies of this same slice, Top N=20:

| New-runtime mode | Median wall, s | Max wall, s | Peak parent RSS, MiB |
| --- | ---: | ---: | ---: |
| Legacy controls | 2.295 | 2.367 | 203.3 |
| Filter only | 1.952 | 2.120 | 206.1 |
| Equity rank only | 3.139 | 3.434 | 208.8 |
| Filter and equity rank | 2.036 | 2.141 | 208.0 |

Each mode had one stable decision signature across its measured repetitions,
zero cache writes and an unchanged input slice. The filter evaluated all 512:
7 GROWING, 171 WEAKENING and 334 DECLINING_OR_MIXED; it eliminated 334.
These are observed decisions on one selected group, not a population estimate
or a claim that the filter improves trading results. JSONL evidence SHA-256:
`379c6effb4e62de8d98145c60a74b930b6a947b15507097643dc8fe55173089b`.

The slice creator and benchmark harness tests passed together (`30 passed`).
The creator received independent Opus 5 `CODE_REVIEW_PASS` after follow-up
reviews. Prior-runtime baseline, cold/backfill worker profiles, one REPLACE,
full-corpus comparison and M5 budget acceptance remain open. The four modes
above all use the new runtime; legacy-controls mode is **not** an old-runtime
baseline.
