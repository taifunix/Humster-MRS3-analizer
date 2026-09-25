# Performance v2 equity quality — R7.3 M0 evidence

**Canonical basis:** R7.3, as specified in docs/specs/2026-09-25-performance-v2-equity-quality.md and docs/superpowers/plans/2026-09-25-performance-v2-equity-quality.md.

**Scope:** read-only diagnostic evidence only. No production implementation, migration, repair, cache write, Panel/tester run, or service restart. This report supersedes earlier R6.1/R7.1 M0 conclusions where noted below.

## Commands and method

Executed from the repository root with the local environment:

- .venv\Scripts\python.exe scripts/benchmark_equity_quality.py --help
- .venv\Scripts\python.exe scripts/benchmark_equity_quality.py --self-check
- .venv\Scripts\python.exe scripts/benchmark_equity_quality.py --per-pair 8
- .venv\Scripts\python.exe scripts/benchmark_equity_quality.py --per-pair 8 --grid-sensitivity
- .venv\Scripts\python.exe scripts/benchmark_equity_quality.py --limit 60 --benchmark-repeats --grid-sensitivity
- An in-memory compile check of scripts/benchmark_equity_quality.py.

The completed deterministic bounded scan selected at most eight current ACTIVE results per Pair+Side group; it did not scan every ACTIVE result and is not claimed representative. Selection began from ACTIVE/current-result metadata ordered by (symbol, side, strategy_id); within each group, the midpoint sample uses zero-based indices floor((i+0.5)*n/k), where n is group size and k is the cap, and groups with n<=k retain every member. The selected result list is then ordered by result_id for the equity query. It contained 190 unique results from 14,463 ACTIVE results (1.314%), covering 988,120 of 74,333,934 equity rows (1.329%). Group selection arithmetic reconciled exactly: 22 groups of eight plus AAOIUSDT/LONG=5, KSTRUSDT/LONG=2, and TQQQUSDT/LONG=7, for 190; no duplicate or silently dropped selected IDs. Full metadata and report-end distributions were read across all 14,463 rows. The optional 1h/3h/6h grid sensitivity was computed in the earlier scan; this report retains the captured 6h versus raw-ER aggregate, not unrecorded detailed 1h/3h percentiles.

The full-corpus metadata identity digest including each result's T was SHA-256 545f0c0b08817e7813e143a878aa7465df5ddb488bb313ee3d15a553e89a1ef3. Bounded selection is deterministic from current Pair+Side membership; per-group selected counts and reasons are available in the script JSON output.

The script sets DuckDB session timezone to UTC, verifies report/action/equity timestamp columns are TIMESTAMP WITH TIME ZONE, and reports UTC offset invariants. Numeric epoch units are not applicable: timestamps are native TIMESTAMPTZ values, decoded as aware +00:00 values and normalized to UTC. Per-result min/max equity-time offsets relative to report start and T are emitted in JSON; no sampled source point was outside its report interval. It reads only equity rows for equity facts. No action rows were read and there were zero action queries/rows attributable to equity calculation; the separate database integrity check counted 10,467,732 action rows without materializing their values. Trip-rate labels were therefore not retained. There was one ordered batched equity query per scan (one per measured repeat), not one query per result; writes were zero.

The old ACTIVE denominator came from strategies joined to the matching current strategy_results row with lifecycle_status ACTIVE; full equity row count came from the read-only table-count invariant. The bounded query returned 988,120 raw rows. After the review fix, the 190-result rerun took 86.3195 seconds including metadata/invariant checks and confirmed 190 selected IDs, one ordered equity query, 988,120 streamed rows, 190 result groups flushed including the final group, and 988,120 per-result rows accounted. ORDER BY result_id,sample_index kept each group contiguous across fetchmany chunks. Accounting reported no missing IDs, duplicate flushes, or row-count discrepancy.

## R7.3 live read-only results

### Full-corpus metadata

- ACTIVE results: 14,463. Stored report age in days: minimum 29, median 50, p90 70, maximum 128; all metadata rows were at least 28 days old.
- Report-end counts: 2026-08-26=1; 2026-08-28=2; 2026-09-09=11,263; 2026-09-13=1,541; 2026-09-14=73; 2026-09-16=976; 2026-09-18=171; 2026-09-19=27; 2026-09-23=409.
- Timeframe metadata counts: 5m=804, 15m=579, 30m=1,301, 45m=1,278, 1h=2,082, 2h=3,195, 3h=2,943, 4h=2,281. Timeframe is descriptive metadata only; it is not an equity-quality input.
- Mixed report ends are permitted: each result uses its own stored report_end T. No common-T preflight or freshness gate is part of R7.3.

### Deterministic Pair+Side sample of 190

- Source validity after the precedence fix: 190/190 had no malformed, nonfinite, out-of-report, duplicate-index, or nonpositive in-report source issue; no result was under seven days or missing its 7/14/28-day baseline. Thus the structural-invalidity precedence change did not change any live-sample disposition or metric count.
- Baseline availability: W=7,14,28 each available for all 190; H=28 for all 190.
- Dispositions: GROWING=80, WEAKENING=52, FLAT=3, DECLINING_OR_MIXED=55. H-UP denominator was 132. Under R7.3, H-UP with a declining short window is WEAKENING, class 1, ERF PASS; short-only decline blocks observed=0 by construction.
- Raw equity rows scanned: 988,120. Fixed 6h grid node counts: W7=5,510; W14=10,830; W28=21,470. Duplicate timestamp count=0 in the sample; duplicate-driven D/P delta was zero.
- Time since latest raw observation to T: p50=3.4h, p90=39.0667h, max=253.5167h. Max internal gap: p50=48.1833h, p90=261.3667h, max=977.6333h. Max gap including leading and terminal tails: p50=48.1833h, p90=344.5667h, max=1,277.9833h.
- Quiet tail from latest raw observation to T: p50=4.7667h, p90=143.7333h, max=1,277.9833h; 91/190 exceeded six hours and 34/190 exceeded 24 hours. These tails remain right-continuously carried under R7.3 and are not invalidation evidence.
- Absolute raw-ER versus canonical 6h-grid ER difference: min=0, p50=0.2174637778, p90=0.5197435643, max=0.802893126. Raw drawdown/position and grid ER are intentionally distinct measures.
- The captured sample had no Top-10 membership changes in its reported comparison, but this is not a full-cohort Top-N result: at most eight candidates per Pair+Side were selected, and omitted candidates can change ranks. Sample rank movements included BABA strategy 6 (WEAKENING, rank 8 vs score-only rank 4), BABA strategy 16637 (GROWING, rank 3 vs score-only rank 6), and SONY strategy 25989 (GROWING, rank 1 vs score-only rank 3). Treat as diagnostic examples only.

The scan emits age, timeframe, inactivity/gap, and flat-tail cross-tabs. The report preserves the overall sample statistics above, not a complete set of per-stratum class/score percentiles. Since action strata were not read, no trip-rate stratum distribution is claimed. No candidate-weighted performance, coverage threshold, or representative full-corpus conclusion follows from this 1.314% candidate sample.

## Bounded repeat and performance evidence

The prior bounded repeat ran --limit 60 --benchmark-repeats --grid-sensitivity: one code warmup followed by three measured runs, each on the same deterministic 60-result sample. Each run read 310,715 equity rows with one ordered batched equity query, read zero action rows/queries for equity facts, and wrote zero rows. That repeat was not rerun after the precedence/accounting review: no collision cases occurred in the 190-result live cohort, and the equity query and Decimal metric paths are unchanged; the added row reconciliation is O(selected results) bookkeeping. Treat the repeat as timing reference, not post-fix budget acceptance. The post-fix 190-result scan runtime is reported above.

- Measured wall seconds: 29.2719439, 29.2023069, 28.8148374; median 29.2023069.
- Peak process RSS observations: 479,936,512; 484,614,144; 486,735,872; 487,350,272 bytes (warmup then measured runs).
- DB before and after: 14,119,350,272 bytes; mtime_ns 1790315817290340100; schema_version=5; instance_id=b9d4cf2d-1727-4db8-9e79-0149d31a5a1d; catalog digest 91e1593f047fe9cb12903f1d77880b7aaf0152e5d1f8fe3f3a75a8196128525c; counts: actions=10,467,732, equity=74,333,934, window_metrics=98,824. All recorded invariants were unchanged.

These runs do not provide a comparable cold/warm production baseline, cache/backfill measurement, SQL profile, full-result workload, or worker-profile comparison. Budgets remain pending: cold <= baseline + max(20%, 0.5s), warm <= baseline + max(10%, 0.05s), RSS <= baseline + max(25%, 64MiB). No budget pass is claimed.

Historical cache-path evidence remains non-comparable: the earlier cache-only 164-candidate run had one warmup and three measured runs (1.3751267, 1.330557, 1.4306466 seconds; median 1.3751267), RSS about 122.7–127.6 MB and zero writes; SQL counts were not instrumented. It is not a R7.3 baseline. A prior all-group cache-loader attempt was stopped after roughly seven minutes with no writes.

## Self-check and limitations

The deterministic in-memory self-check covers equal 5m/4h labels, right-continuous carry across quiet/internal/final gaps, multi-day flat quiet tail with H-UP, short-decline WEAKENING/PASS, flat-H FLAT/BLOCK, a single baseline filling a full window, exact report-start predecessor and left boundary, pre-report invalid source and future-only missing baseline, H=28/14/7 selection, malformed/nonfinite input, structural-invalidity collisions with in-report nonpositive equity, a positive Decimal below binary64 range preserved without float conversion, negative-zero nonpositive detection, exact L/T duplicate anchor paths, raw spike drawdown/position change, grid-log invariance, age boundaries, mixed T, zero short-only blocks, and streamed-result accounting including the final flush. Repeated self-check output must be byte-identical.

This is a bounded diagnostic, not acceptance of full-corpus quality coverage or ranking behavior. The proposed old <=5% gap-based unrankable threshold and verified-flat false-unassessed gate do not apply under R7.3, because internal gaps and quiet tails are carried unconditionally. M0 does not authorize production implementation.

## Superseded historical findings

The following R6.1/R7.1 findings are retained only as historical diagnostics and are superseded by R7.3: 356/359 UNKNOWN_GAP; position/action continuity gaps; 1,051 old required-window unknown checks; the age-selected H and edge-anchor decision; and the common-T blocker. R7.3 has no action/position proof, gap-based unassessed class, edge-anchor H selection, or common-T gate.

The old 359 count reconciles as 208 candidates in six single-end groups plus 18 mixed groups of eight and TQQQUSDT/LONG=7: 208 + 18×8 + 7 = 359. The previously expected 360 came from incorrectly assuming all 19 mixed groups had eight candidates. AAOIUSDT/LONG=5 and KSTRUSDT/LONG=2 were also identified as undersized groups in the historical cohort metadata. Those action-based and cohort-specific counts must not be reused as R7.3 conclusions.

R7.3 mixed report ends are expressly accepted, using per-result T and including T in revision/digest identity. Therefore the former 19/25 mixed Pair+Side groups and 14,255/14,463 candidates outside six common-end groups (98.56%) are no longer a blocker. The previous <=5% coverage, flat verification, and short-decline policy conclusions are likewise superseded; the current short-decline policy is WEAKENING/class 1/PASS for H-UP.

## Disposition

The R7.3 contract and self-check are ready as M0 artifacts. Live evidence is useful but bounded; full ACTIVE-corpus equity scan, complete per-stratum distributions, comparable cache/cold baselines, and performance budget acceptance remain open. No production implementation or M1 work was started. The remaining execution/acceptance decision is whether to fund a full-corpus read-only scan and comparable baseline measurements before implementation; this report does not silently treat either as passed.
