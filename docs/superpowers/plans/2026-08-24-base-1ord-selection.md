# Source v6 BASE 1ORD Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` task-by-task. Track every step with the checkboxes below.

**Goal:** Select reproducible BASE 1ORD candidates for every exact `Pair + Side + TF`, publish them as EQUAL-only READY structures, and propagate the three plateau diagnostics to post-test comparison.

**Architecture:** Reuse the frozen `base_1ord_point_id`, `AlgorithmConfig`, `_base_structure`, and existing `structures` table. Add one derived scalar, `events_last_30d`, only to `point_analysis_input.row_json`; consume it for admission and strip it before analysis publication. Do not add DDL, tables, dependencies, panel code, or an abstraction around the fixed role loop.

**Spec:** `docs/specs/2026-08-24-base-1ord-selection.md`

## Contracts

- Look up frozen BASE by `point_id + symbol + side + timeframe`. Missing, duplicate, plateau mismatch, membership mismatch, or `standalone_eligible=False` fail closed with the exact point and scope; configured point/event floors simply exclude valid candidates.
- `events_last_30d` is a non-boolean integer `0..trades`, in `row_json` only. Count it in UTC milliseconds over the READY witness `[start_ms, end_ms)`, where `end_ms` is midnight after `ready_witness.end`: `cutoff=max(start_ms,end_ms-30d)` and `cutoff <= timestamp < end_ms`.
- Admit only `plateau_point_count >= min_plateau_points` and `plateau_event_count >= min_plateau_events_per_month`. Both event fields are admission-only and must not reach structures, variants, provenance, exports, or post-test.
- Compute the median of `plateau_point_count` once from the complete admitted exact-scope pool: odd middle, even arithmetic mean. Roles are attempted in order `ECONOMY_1`, `STABILITY_1`, `STABILITY_2`, `ECONOMY_2`; stability requires `>= median`; choose each with the unchanged §17 order and remove it. Empty roles are skipped, later roles still run, and `slots` caps selected candidates rather than slicing roles.
- Preserve only `plateau_point_count`, `base_point_trades`, and `plateau_total_trades`: scalar for 1ORD, order-aligned JSON lists for 2–4ORD.
- `1ORD -> EQUAL`; `2–4ORD -> EQUAL + INCOME`. Old precomputed compact surfaces that lack the new key fail with an explicit re-materialization error; no repair or fallback.

---

### Task 1: Config and identity inputs — completed

**Files:** `src/mrs3/config.py`, `config.example.json`, `config.local.json.example`, `tests/test_config.py`.

- [ ] Baseline: `.venv\Scripts\python.exe -m pytest -q tests/test_config.py --basetemp=.pytest-tmp-base1-t1-base`
- [ ] Write failing tests for defaults, absent `base_one_order`, complete JSON load, malformed section, invalid non-boolean integer values, and a changed canonical config hash for each value.
- [ ] Implement only `min_plateau_points=3`, `min_plateau_events_per_month=20`, `base_one_order_slots=4`; load the JSON section and validate `>=2`, `>=0`, and `1..4` respectively.
- [ ] Green: `.venv\Scripts\python.exe -m pytest -q tests/test_config.py --basetemp=.pytest-tmp-base1-t1-green`

### Task 2: Compact monthly row, BASE selection, diagnostics, and version — completed

**Files:** `src/mrs3/source_v6_materializer.py`, `src/mrs3/source_v6_surface_fresh.py`, `src/mrs3/source_v6_analysis_fresh.py`, `src/mrs3/selection.py`, `src/mrs3/pipeline.py`; tests `test_source_v6_stage2_metrics.py`, `test_source_v6_materializer.py`, `test_source_v6_surface_throughput.py`, `test_source_v6_analysis_fresh.py`, `test_selection.py`, `test_pipeline.py`.

- [ ] Inventory all compact-row fixtures: `rg -n '"event_ids"|events_last_30d|_ANALYSIS_ROW_FIELDS|analysis_input_row|point_analysis_input|analysis_input=' tests src/mrs3`; baseline their focused tests and update every discovered fixture in this task.
- [ ] Red tests: cutoff minus one / cutoff / plus one / end boundary; short witness where `start_ms` controls; strict missing, null, bool, negative, float/string and `>trades`; unchanged three-column DDL; old-row actionable message; slow factual path; and `events_last_30d` absent from published frames.
- [ ] Implement the scalar calculation, strict validator with `scope_key|point_id` remediation error, carry it only to analysis, sum members into admission-only `plateau_event_count`, then strip it before `_frames` writes points/structures.
- [ ] Red tests for exact-scope frozen missing/duplicate/corruption failures; empirical floor exclusions; fixed-median cases `[1,2,3,10,11]`, even `[3,5,7,9]`, ties, empty stability followed by economy, slots 1–4, multi-TF, and 1/2/3 candidate underfill.
- [ ] Implement one private pure role helper and shared plateau diagnostics. Add diagnostics to `_base_structure` and `_order_from_point`; serialize BASE only at fresh `_frames`, never mutate legacy `stages.structures`.
- [x] Set `ALGORITHM_VERSION = "0.7-canonical-phase1-base-1ord-v3"` in this task; test an old-version analysis is not reused.
- [ ] Green: `.venv\Scripts\python.exe -m pytest -q tests/test_source_v6_stage2_metrics.py tests/test_source_v6_materializer.py tests/test_source_v6_surface_throughput.py tests/test_source_v6_analysis_fresh.py tests/test_selection.py tests/test_pipeline.py --basetemp=.pytest-tmp-base1-t2-green`

### Task 3: Structures-only shortlist, generation, and post-test — completed

**Files:** `src/mrs3/fresh_analysis_strategies.py`; post-test comparison was a historical downstream artifact. Tests: `test_fresh_analysis_strategies.py`, `test_fresh_analysis_shortlist_groups.py`.

- [ ] Baseline then write red tests for BASE IDs/counts sourced only from `structures`, a BASE-only scope, no duplicate IDs, and no double-counting `base_one_order` evidence.
- [ ] Implement removal of the separate base table/count pass. Derive all `1ORD..4ORD`, READY counts, and `candidate_ids` from `structures`.
- [ ] Red then implement fresh provenance/variants with exactly the three diagnostics. Verify BASE produces one EQUAL strategy; a 2ORD regression produces both EQUAL and INCOME; events are absent from provenance.
- [ ] Red then implement `_PLATEAU_DIAGNOSTIC_COLUMNS` in metadata extraction, variants whitelist, embedded settings fallback, and final comparison ordering. Verify scalars/lists remain unmodified.
- [ ] Green: `.venv\Scripts\python.exe -m pytest -q tests/test_fresh_analysis_strategies.py tests/test_fresh_analysis_shortlist_groups.py --basetemp=.pytest-tmp-base1-t3-green`

### Task 4: Real evidence, review, docs, and commit — completed

- [ ] Run the combined focused suite from Tasks 1–3.
- [ ] Create a new empty system-temp evidence directory. Snapshot source DB digest, the eight `CXMTUSDT|LONG` scopes, listing date `2026-07-27`, config hash, algorithm version, git HEAD, scoped diff hash, and timestamps. Never reuse a surface or analysis artifact.
- [ ] Re-materialize from `data/databases/my_test_CX_GE_fixed.source-v6.duckdb`, assert every actual `row_json` has the scalar, and analyse without catching frozen-base errors. Manifests must match snapshot identifiers.
- [ ] Independently replay role selection from points/plateaus/BASE facts (without importing the helper), then compare it to persisted structures. Hard-fail on disagreement.
- [ ] Parse `structures.payload_json`, group `(symbol, side, timeframe)`, sort persisted roles, and hard-assert vectors: `15m [8,12,12,4]`, `5m [15,23,12,7]`, `1h [3,10,13,8]`, `30m [5,31,7,3]`, `45m [5,16,20,14]`, `4h [6,4,3]`, `2h [27,3]`, `3h [4]`; total 26. Record independent underfill traces for 4h/2h/3h.
- [ ] Assert shortlist IDs/counts, BASE-only EQUAL output, 2ORD two methods, and diagnostic/event-field boundary. Record surface/analysis hashes and results externally before deleting temporary data.
- [ ] Update `PRD.md` and `progress.md` only after evidence; add the approved target spec, never the unrelated untracked panel plans.
- [ ] Full verification: `.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-base1-full`, then `git diff --check`.
- [x] Prove no panel, archive, ADR, dependency, DDL, or generated-data diff. Local reviewer returned `CODE_REVIEW_PASS` after fixes and re-review.
- [x] Stage exactly scoped files (including the target spec), inspect staged diff, keep unrelated plans untracked, then commit `feat: make fresh BASE 1ORD candidates selectable`.

## Completion evidence

- Fresh corpus replay produced the accepted 26 BASE vectors and hashes recorded in `progress.md`.
- Final local verification: `1736 passed, 2 skipped, 2 warnings`.
- Local advisor findings were resolved; final local reviewer disposition: `CODE_REVIEW_PASS`.
- Scoped commit: `90e4341` (`feat: make fresh BASE 1ORD candidates selectable`).
