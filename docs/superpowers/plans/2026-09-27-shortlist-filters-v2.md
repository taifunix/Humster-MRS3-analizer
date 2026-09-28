# Shortlist Filters v2 Implementation Plan

> For agentic workers: after explicit implementation authorization, use
> superpowers:subagent-driven-development with the preserved Executor/Reviewer
> routes. This document itself does not release an Executor.

**Goal:** Three explicit-apply fresh shortlist filters, consistent audit/JSON,
correct plateau totals, and less duplicate database work.

**Architecture:** One fresh-only preparation/evaluation contour serves list,
audit and generation. UI holds separate draft and applied state; a content-bound
token identifies the applied selection. Keep legacy non-fresh behavior separate.

**Tech stack:** Existing Python/DuckDB/Decimal/NumPy and vanilla JavaScript;
stdlib threads and synchronization, no new dependencies.

Status: implemented in `feat/shortlist-filters-v2` and integration-tested with
`main` on 2026-09-28.
D3 received `PLAN_APPROVED`; A01-A14 and B01/B02 are CLOSED. GPT-6 Luna
xhigh executors implemented M0-M4. Independent Opus 5/high returned
`CODE_REVIEW_PASS` for engine, generator/provenance and Panel/UI slices.
The complete repository suite passed: 5310 passed, 8 skipped. Final integrated
Opus 5/high review returned `CODE_REVIEW_PASS`.

Contract: [specification](../../specs/2026-09-27-shortlist-filters-v2.md).
Planner: GPT-5.6 Sol, high. Root owns this canonical document.
Executor: user-requested GPT-6 Luna xhigh. Independent code Reviewer: Opus 5
through the review bridge.

## Global constraints and review focus

- No runtime edits until user authorization; no real tester/live DB mutation.
- Source screening is not final MRS3 performance; MA proximity is not nesting.
- Use existing `duckdb_import.workers`; no new dependency/settings subsystem.
- TDD from `.venv`; independent CODE_REVIEW_PASS before a scoped commit.
- Preserve unrelated edits; shared Panel files have one Executor owner.

Priority failure modes and owning tests: stale checkbox/analysis responses (M4),
same-size artifact mutation (M2/M3), missing or malformed order evidence (M1),
zero survivors/empty scopes (M1/M3), concurrent tabs and busy/error cleanup (M2).

## A. Verified findings

| ID | Current source | Finding and planned disposition |
| --- | --- | --- |
| F01 | `fresh_analysis_strategies.py`: `_read_analysis`, `list_fresh_analysis_shortlist`, `filter_fresh_analysis_candidates` | Repeated full-file hashes, table reads and point validation. Prepare once; reuse compact validated inputs. |
| F02 | `_rows` | Per-scope queries sort large raw JSON strings. Batch by table and sort compact identities only where required. |
| F03 | `analysis_shortlist.py`, fresh filter adapter | Four standalone Pareto calculations plus combined filtering. Fresh v2 computes one joint Pareto; preserve non-fresh legacy behavior. |
| F04 | `panel_web/app.js`: checkbox handlers, audit/generation | Live checkbox values can differ from the displayed selection. Introduce draft/applied options; all output uses the applied snapshot. |
| F05 | `refreshShortlist`, `applyShortlist`, analysis open | Responses have no sufficient analysis/revision guard. Ignore superseded responses and publish one coherent UI snapshot. |
| F06 | `restoreGeneratedBatch` | Batch recovery can overwrite active analysis identity. Keep batch identity separate. |
| F07 | `pairGroups` and parent row renderer | Parent plateau count is not summed and explicitly renders a dash. Sum child geometric plateau counts. |
| F08 | `_shortlist_groups` | Candidate-driven groups omit empty manifest scopes; OFF-mode order buckets include non-READY structures. Build scope facts first and make READY/count invariants unconditional. |
| F09 | `generate_fresh_analysis_strategies` | Selected points are read/validated and a second full-universe filtering pass follows. Reuse a verified selection; load selected generation records once. |
| F10 | fresh audit exporters/endpoints | Four diagnostic views force needless work; list-with-audit and separate audit duplicate orchestration. One fresh v2 evaluator and audit adapter. |
| F11 | `strategies_fresh_generate_runs` | Less visible RUNS endpoint is also a consumer of old flags. Route through the same v2 selection contract; do not leave a bypass. |
| F12 | `_file_digest` | `read_bytes()` allocates the entire artifact for hashing. Stream SHA-256 with bounded buffers. |

### Read-only timing evidence

One existing 161,492,992-byte analysis: 74 structures, 24 manifest scopes.
Current shortlist with four old criteria enabled took **6.179 seconds**.
Instrumentation recorded two hashes (1.173 s inclusive), 120 `_rows` calls
(2.844 s inclusive), one `_validate_points` call (1.131 s inclusive), and
160 standalone-group calls (0.004 s inclusive). Table calls: points 48,
structures 48, plateaus 24. The combined helper made 206 dominance calls.

This is one warm-OS-cache diagnostic, not a benchmark median. Timings overlap
and must not be added as independent costs. The measured bottleneck is I/O,
JSON processing and validation, not Pareto. Size/mtime were unchanged by the
read-only probe; no cryptographic before/after equality claim is made.

Only 11 of 24 scopes were displayed. The other 13 had zero structures and
zero plateaus in this artifact; an actual lost nonzero plateau count was not
observed. Parent plateau sums are nevertheless absent in the renderer.

## B. Canonical design

Three default-OFF controls: existing Source PRETEST A/B, Open MA proximity,
one joint Pareto. A native `Recalculate filters` button is below them.

Evaluation order: persisted READY -> A/B -> proximity -> Pareto.
Earlier rejects cannot dominate later candidates. Keep representatives,
GAP rules, source analysis and PerformanceDB untouched.

Proximity: `abs(open_ma_i - open_ma_1) <= 1` for every extra order.
1ORD passes; 3/2/4 passes; 2/3/4 fails. This is not proof of price-level nesting.

Pareto group: `(pair, side, timeframe, common_close_ma, order_count)`.
At every corresponding order the dominator has no lower PnL, no higher DD,
no fewer plateau member points and no fewer point events. At least one PnL
or DD comparison must be strictly better. Support-only improvements and
full equality retain both. Choose a deterministic surviving dominator.

New fresh-only request version `shortlist-v2`, with exact boolean flags
`pretest_ab_enabled`, `ladder_enabled`, `pareto_enabled`. Reject enabled
old criteria with an actionable stale-client message; do not reinterpret them.
Keep historical non-fresh APIs unchanged. Old fresh analyses remain usable
with A/B OFF; preserve the rebuild error for missing evidence with A/B ON.

The applied snapshot token hashes artifact SHA-256, analysis ID, engine
version, canonical options and sorted surviving identities. It is a consistency
identifier, not authentication. Audit and generation verify it against their
options and current artifact. Generation resolves READY IDs server-side for
the selected scopes; browser IDs are not an authority. Two tabs are independent.

One controller-owned compact preparation, at most eight compact evaluations,
and a 256 MiB retained-cache ceiling. Oversized input takes the uncached path.
Do not retain event arrays or raw DataFrame copies. Coalesce identical loads
using dedicated synchronization, not the general Panel lock. Cold preparation
uses matching streaming digests before/after read-only validation; warm actions
perform one streaming content verification. A stat-only cache key is forbidden.

Exact Decimal values may be encoded as per-column integer ranks. Pareto uses
bounded NumPy comparisons, never a full N x N x metrics tensor. Independent
large groups may run in threads; small work stays serial. Use current shared
`duckdb_import.workers` through existing Settings. Bound temporary arrays
across the whole pool (initial internal budget 64 MiB); worker count cannot
change decisions or tokens. No new dependency or user-facing worker setting.

Counts: create every manifest scope; geometric plateau IDs counted distinctly
per scope; parent pair/side sums TF counts. Buckets count READY survivors only;
`sum(1ORD..4ORD)=READY`, `READY+DEFERRED=ALL`, even with filters OFF.

## C. Ordered implementation slices (M0-M5 verified)

### M0 - Contract and compatibility

- [x] After user authorization, record ADR-0047 for this fresh-only selection
  contract. ADR-0046 is reserved by the separate liquidity plan.
- [x] Inventory consumers of fresh generator schema/provenance, including
  recovery/catalog/RUNS, and preserve historical manifest reading.
- [x] Add failing tests for exact option validation, enabled legacy flags,
  v1-analysis A/B compatibility and selected-scope validation.
- [x] Introduce one canonical options/version parser for fresh endpoints.

Files: `panel.py`, `fresh_analysis_strategies.py`; existing fresh controller tests.
Exit: no stale-client silent interpretation; legacy non-fresh tests unchanged.

### M1 - Prepare once and implement pure selection

- [x] Introduce one fresh-only module, preferably `src/mrs3/fresh_shortlist.py`;
  keep old `analysis_shortlist.py` semantics intact.
- [x] Batch-load manifest/points/structures/plateaus once per cold preparation;
  preserve manifest/fingerprint/event-membership validation and scope facts.
- [x] Normalize compact fields; validate relevant metrics without NaN/zero
  fallback or truncating fractional MA/count values.
- [x] Reuse existing PRETEST predicate; implement proximity and exact Pareto
  against a simple Decimal test oracle before optimizing comparisons.
- [x] Implement deterministic surviving-dominator reasons and READY/group sums.
- [x] Test equality, DD direction, strict economic gain, all grouping boundaries,
  upstream exclusion, 1ORD, empty scopes, malformed fields and permutations.

Exit: exact pure-engine tests pass; no input artifact is written.

### M2 - Shared preparation and bounded execution

- [x] Replace whole-file byte allocations with streaming hashing; use
  pre/post digest agreement on cold preparation and fresh verification on reuse.
- [x] Add one compact preparation cache, eight-result limit, retained-size cap
  and uncached fallback; do not build a persistent cache subsystem.
- [x] Coalesce identical preparations; bound concurrent shortlist computations
  and release synchronization correctly after exceptions.
- [x] Add exact integer-rank/blocked NumPy comparisons only within this engine;
  use the existing shared worker limit for sufficiently large independent groups.
- [x] Test serial/parallel identity, whole-pool memory budget, cache eviction,
  same-size byte edits, cold-load mismatch, singleflight and error recovery.

Exit: warm option changes perform no table reread/point revalidation; one
content check per action. No DuckDB connection is shared with worker threads.

### M3 - Applied selection through list, audit and generation

- [x] Route fresh list, audit flag, dedicated audit, JSON generation and RUNS
  through the same validated preparation/evaluation and v2 options.
- [x] Return canonical applied flags/token; require matching token for output.
- [x] Fresh audit becomes Summary/READY/DEFERRED with reasons and order-level
  evidence. Do not calculate standalone criteria. Use token-qualified output
  naming and safe publication so tabs cannot overwrite different snapshots.
- [x] Freeze validated job inputs before starting generation; resolve selected
  READY candidates on the server and load their required records once.
- [x] Recheck artifact identity before atomic publication; persist version,
  options, token and selected IDs. Update strict fresh-schema readers together.
- [x] Test tampered/stale tokens, two tabs, browser-ID injection, no-output-on-
  failure, all-OFF generation, and list/audit/JSON cohort equality.

Files: `fresh_analysis_strategies.py`, `panel.py`, `analysis_filter_export.py`,
existing generator readers and focused tests where the consumer inventory proves
changes necessary. No tester run is part of this slice.

### M4 - Explicit UI application and counts

- [x] Put the three controls directly in the Shortlist card, removing old four
  checkboxes and the script that moves the old controls from another container.
- [x] Add draft/applied state and pending-changes text. Checkbox edits send no
  requests. Recalculate freezes draft; success atomically replaces the snapshot.
- [x] Refresh uses applied flags. Audit/Generate always use applied flags/token.
- [x] Guard responses by request revision and target analysis; prevent duplicate
  apply clicks. Retain an old snapshot after same-analysis failure, not after
  switching the selected analysis.
- [x] Initial/new analysis starts OFF with reset scope picks; switching invalidates
  old responses. Batch recovery never assigns active analysis identity.
- [x] Disable output while loading/no valid snapshot; prune absent/zero-READY
  scope selections. Draft edits alone do not invalidate an applied snapshot.
- [x] Sum parent plateau counts, show zero correctly, enforce READY bucket sums.
- [x] Replace old static change-handler assertions with executable Node-based
  UI-state regressions. Check native keyboard controls and desktop layout.

Files: `panel_web/app.js`, `panel_web/index.html`, existing UI tests; CSS only
if current layout/state classes cannot support the controls. No redesign.

### M5 - Integration, measurements and independent review

- [x] Run focused, relevant broader, then full tests using local `.venv` only.
- [x] Measure cold preparation, warm option application, audit export and
  selected generation-record loading separately; report hash and peak RSS.
  Full JSON publication time is not measured by this read-only probe.
- [x] Cover both I/O-heavy and candidate-heavy read-only analyses (observed
  available examples: about 751 MB/406 structures and 326 MB/11,816 structures).
  Run multiple repetitions and report medians; distinguish OS-warm from true cold.
- [x] Verify worker 1/configured-worker output equality; include all eight flag
  combinations, source hash/schema/mtime preservation and stale request tests.
- [x] Check browser-smoke availability: no browser automation tool is provided
  here. No visual pass is claimed; a staging visual check remains advisable.
- [x] Root reviews scoped diff and runs `git diff --check`; send requirements,
  diff and evidence to independent Opus code review. Fix/retest/re-review findings.
- [x] Update PRD/progress/plan with actual evidence only. Commit only after
  `CODE_REVIEW_PASS`, never just Executor's completion message.

No percentage speedup or user-facing latency SLA has been promised. The technical
warm-list target is median <=5 seconds on the same largest corpus/host; report
hash time separately and mark the target unmet if exceeded. Never weaken content
verification to meet it. Other timings are measurements, not invented passes.

## D. Verification commands

```powershell
.venv\Scripts\python.exe -m pytest tests/test_fresh_shortlist.py tests/test_fresh_analysis_strategies.py tests/test_fresh_analysis_shortlist_groups.py tests/test_analysis_filter_export.py tests/test_panel_fresh_strategies.py tests/test_panel_static_ui.py
.venv\Scripts\python.exe -m pytest tests/test_analysis_shortlist.py tests/test_analysis_strategies.py tests/test_panel_analysis_catalog.py tests/test_tester_run_files.py
.venv\Scripts\python.exe -m pytest tests/test_panel_static_ui.py::test_shortlist_v2_state_machine -v
.venv\Scripts\python.exe -m pytest
node --version
node --check src/mrs3/panel_web/app.js
git diff --check
```

Current planning turn does not run implementation tests or modify runtime/tests.
No live PerformanceDB writes, source rebuild, Panel restart or real tester run.
Unrelated `.portfolio-results/` belongs to the user and must be preserved.

## E. Review ledger and release gates

| Round | Source version | Disposition | Findings |
| --- | --- | --- | --- |
| Planner | D1 | PLAN_DRAFT | Canonicalized by root |
| Advisor 1 | D1 | PLAN_REVISE | Opus 5/high; A01-A14 below |
| Planner revision | D1 -> D2 | PLAN_REVISION | Complete plan and dispositions returned; integrated by root |
| Advisor 2 | D2 | PLAN_REVISE | A01-A14 CLOSED; B01/B02 below |
| Planner revision | D2 -> D3 | PLAN_REVISION | Full revised plan and ledger returned; integrated by root |
| Advisor 3 | D3 | PLAN_APPROVED | Opus 5/high closed B01/B02, kept A01-A14 closed; no material new findings |

A01-A14 were closed by Advisor round 2; B01/B02 were closed by round 3.
Nonblocking implementation notes: pin deterministic stale-client error precedence
where multiple invalid request conditions overlap; record M1's initial failing
run before treating the new test module's verification command as green.

| ID | Severity | Disposition and executable acceptance |
| --- | --- | --- |
| A01 | high | Spec field map binds structure-order metrics and point join. M1 schema test verifies exact paths and preserved `(shift_bp, point_id)` sequence, optional id=position+1. |
| A02 | high | Nonnegative finite percent DD, zero valid, no abs/scaling guesses. M1 tests negative/zero/equal DD. Supported producer supplies units. |
| A03 | high | Numeric Decimal unique ranks, not text/context normalization. M1 oracle tests 1.10=1.1, 1E+1=10, -0=0. |
| A04 | high | Positive integral MA, nonnegative events, positive plateau size; no bool/fraction/truncation. M1 diff +/-1 pass, +/-2 fail, fractional input errors. |
| A05 | medium-high | Spec decision matrix covers version/legacy flags/new flags across all five fresh consumers. M0 parameterized request tests; token required for output. |
| A06 | medium-high | All row order_count values exact integers 1..4. Scope-qualified plateau identity; manifest/table/payload scope checks. M1 all-eight-flags count invariance and empty scope tests. |
| A07 | medium | Preserve v2 complete evidence validation even OFF; legacy v1 may omit only OFF. Mixed required evidence fails whole request with rebuild message. M0/M1 mixed fixture. |
| A08 | medium | Empty survivors get a token and complete audit; EMPTY_READY_SELECTION publishes no batch/JSON/RUNS. M3 empty-global/selected-scope tests. |
| A09 | medium | 256 MiB retained Python/NumPy graph accounting; 64 MiB entire-pool temporary arrays; 72.6 MiB sizing evidence with stated limits. M2 forced-cap fallback, M5 actual cache/RSS measurement. |
| A10 | medium | One-slot cache deliberately reloads alternating analyses. One heavy slot, <=4 identical followers, 30s timeout, different work gets 409 SHORTLIST_BUSY/Retry-After 1. M2 concurrency/error-cleanup and M3 mutation-before-publication tests. |
| A11 | medium | Exact string/legacy-integer normalization, collision rejection; ascending case-sensitive lexicographic (structure_id,candidate_id). M1/M2 permutations/workers compare reasons, dominators, tokens. |
| A12 | low-medium | No browser storage exists; remove old IDs/helpers/listeners, explicitly reset DOM checked flags. M4 stale form restoration and no-storage tests; never clear unrelated storage. |
| A13 | low-medium | Root rechecks agents/worktree before M0; one owner for shared Panel files, liquidity changes sequenced. ADR-0046 reserved, new ADR-0047 only after authorization. |
| A14 | low | Largest-file streaming hash median2.897s; warm target5s with hash separately reported. M5 target cannot pass without timing evidence or by dropping integrity checks. |
| B01 | high | Corrected review premise with existing code evidence: old and new use the SAME top-level pretest_ab_enabled. No alias. Version-absent and v2 equivalent requests yield equal canonical flags/token. Also reject old criteria true at either nested or top-level location, even if another false would mask it. M0/M3 parameterized tests. |
| B02 | medium | Exact pytest node test_shortlist_v2_state_machine executes existing subprocess Node VM harness; command included above, Node missing fails not skips. M4 updates stale static assertions; M5 must include behavioral-test output. |

## F. D3 executor contract and task refinements

The specification's added sections (persisted mapping, compatibility matrix,
empty input, bounded work) are normative and travel with every executor packet.
M0-M5 above remain the canonical slice numbering. Each slice starts with its
failing test, records the failure, implements the minimum, then records a pass;
commit is gated by repository review rules rather than an unreviewed per-step
commit. Concrete test assertions for the pure engine include:

```python
assert proximity((3, 2, 4)) is True
assert proximity((2, 3, 4)) is False
assert proximity((2, 1)) is True
assert proximity((2, 4)) is False
# Pairwise fixtures otherwise equal:
assert dominates(pnl="1.10", other_pnl="1.1") is False
assert dominates(dd="2", other_dd="3") is True
# These are task assertions, not a new public function API.
```

### M0 additions

- [x] Recheck other agents/worktree before taking shared Panel-file ownership;
  no concurrent liquidity edits to the three shared Panel files.
- [x] Inventory all old filter DOM IDs and generator-schema consumers.
- [x] Parameterize the spec API matrix including unknown/null version, absent
  version with false new flags, malformed old mappings, unknown keys and bools.
- [x] Add a single fresh parser `_fresh_shortlist_options(payload)` in `panel.py`
  returning the canonical three-boolean options tuple. Keep `_phase2_filters`
  for legacy consumers only. Token validation is additional for output paths.
- [x] Retain the identical existing top-level `pretest_ab_enabled` in both
  request forms; only ladder/Pareto fields require an explicit v2 version.
  Never invent PRETEST aliases. Assert equal tokens for equivalent version-
  absent A/B and v2 A/B requests, subject to the same output-token requirements.
- [x] Validate all old criterion occurrences at top level AND inside filters;
  any true occurrence errors. Test nestedfalse/top-leveltrue cannot bypass this.

### M1 additions

- [x] Add `tests/test_fresh_shortlist.py` for exact pure-engine/oracle tests.
  Test data uses persisted order arrays, not artificially reordered metrics.
- [x] Use compact immutable records: prepared analysis has ID/digest/scope facts,
  candidate identities/ordered validated metrics; evaluation holds options,
  READY identities/status/reason/dominator vectors/token, not copied payloads.
- [x] Proposed internal module interfaces: `prepare_fresh_shortlist(path, analysis_id)`
  returns prepared evidence; `evaluate_fresh_shortlist(prepared, options, *, workers)`
  returns the compact evaluation. Existing public wrappers remain adapters.
- [x] Test mapped field absence, READY order length/sequence/optional IDs, fractional
  MA/counts, negative DD, exact numeric equality, mixed PRETEST, canonical ID
  collision, out-of-range persisted order_count, and scope disagreement.
- [x] Test same raw plateau ID across two TFs as two scoped identities, duplicate
  within one scope as one, and totals identical under all eight filter settings.

### M2 additions

- [x] Cache and all eight compact evaluations together must fit the 256 MiB
  measured retained graph; use unique object identity accounting and ndarray
  owned buffers. Measure before insertion; oversized data remains request-local.
- [x] Plan simultaneous temporary allocations from dtype/dimensions, not a
  per-worker cap. Initial entire-pool budget64 MiB. Test forced small budgets.
- [x] One heavy slot per controller: identical work has <=4 followers with30s
  timeout; different saturated work returns409 SHORTLIST_BUSY/Retry-After1.
  Identical-work key includes normalized options, analysis ID and artifact
  identity; sharing preparation never mixes results for different flags.
- [x] Test success/error/timeout cleanup, fifth follower rejection, alternating
  analysis reload, same-size edit, and immutable snapshot semantics.
- [x] Read-only size evidence: 11,816 full structures plus38,304 scalar point
  tuples measured76,144,082bytes; actual parsed/rank/results overhead must still
  be measured. This estimate is not a peak-RSS measurement or acceptance pass.

### M3 additions

- [x] Empty evaluation still creates a token and header-only READY audit sheet.
  No READY in selected scopes -> EMPTY_READY_SELECTION before any publication.
- [x] Audit, JSON and RUNS must reject observed digest mismatch without partial
  publication; audit and generation recheck immediately before atomic publish.
- [x] New generator provenance retains historical readers; old all-false request
  compatibility never waives token checks on output paths.
- [x] At action start changed bytes invalidate list cache and trigger a fresh
  snapshot/token; output paths with an old token return STALE_SHORTLIST_SELECTION
  and publish nothing. Mid-action mismatch always fails the action.

### M4 additions

- [x] Explicit checkbox reset defeats stale browser-restored checked state;
  no localStorage/sessionStorage migration is needed or allowed.
- [x] Node state tests prove late analysis replies cannot overwrite a newer run,
  and restored batch state cannot assign the active analysis ID.
- [x] Implement `test_shortlist_v2_state_machine` in the existing
  `tests/test_panel_static_ui.py`. Reuse its `subprocess.run(("node", "-e", script),
  ...)` pattern with Node built-in assertions/VM, controlled DOM and deferred
  request fakes. Assert draft edits issue zero requests, apply success replaces
  options/token/table together, same-analysis failure preserves the old token,
  reversed replies cannot change the active analysis, and stale checked DOM is
  reset OFF. Missing Node is a failure, not a skip. Update removed-ID assertions
  in this same slice. The exact pytest node above executes behavior;
  `node --check` remains a separate syntax-only check.

### M5 additions

- [x] Largest-file hash evidence: 750,792,704bytes, 1MiB chunks, 2.985/2.897/2.813s,
  median2.897s; OS-warm only. Report new warm-list median against5s technical target.
- [x] Report actual cache residency/accounted graph and peakRSS for both large
  corpora; explicitly disclose uncached fallback and remaining hash cost.
- [x] Include output of the explicit `test_shortlist_v2_state_machine` command
  in root's implementation-review evidence; syntax checks cannot substitute.
- [x] Warm target met on both corpora; no watchers/persistent caches added and
  content hashing remains enabled.

One cache slot is intentional: tabs are independent in correctness, not guaranteed
warm under alternating analyses. Warm list validates content at action start;
it is an immutable snapshot, not a promise of perpetual currency. Cold load
checks before/after; observed mismatch anywhere fails the whole action.

- [x] Independent Advisor `PLAN_APPROVED` (Opus 5/high, D3, round 3).
- [x] User authorizes implementation after the plan-only turn (2026-09-27).

Implementation preflight: existing focused baseline passed `205 passed in 36.28s`
using `.venv` across fresh strategy, shortlist grouping, filter export, fresh
Panel and static UI tests. Node v24.19.0 is available. Initial runtime/test diff
is empty. Existing liquidity documentation and `.portfolio-results/` are unrelated.
Read-only Executor preflight route `gpt-6-luna` / `xhigh` was accepted by the
agent tool; user chose an isolated branch and worktree before write authorization.

Final verification on unchanged code: focused 309 passed; related 214 passed;
post-fix UI 137 passed; explicit Node VM state test 1 passed in 0.30s; full
suite 5310 passed, 8 skipped, 30 warnings in 2631.22s. `node --check` and
`git diff --check` passed. Independent Opus 5/high returned `CODE_REVIEW_PASS`
for engine, generator/provenance, Panel/UI and final integrated gate.
The automatic merge with `main` commit `f761862` passed 528 focused tests
(1 skipped) and the complete suite: 5363 passed, 8 skipped, 30 warnings in
2704.15s; no source-code conflict resolution was required.

Read-only OS-warm benchmark, candidate-heavy 326,119,424-byte/11,816-
candidate DB: cold 12.986s; warm 2.683/1.247/1.355s (median 1.355s);
SHA 1.186s; audit XLSX 9.254s; selected READY record load 2.596s;
retained cache graph 25,355,793 bytes; peak working set 936,538,112 bytes.
I/O-heavy 750,792,704-byte/406-candidate DB: cold 25.036s; warm
3.015/2.908/2.671s (median 2.908s); SHA 2.778s; audit 0.424s;
selected load 4.804s; retained graph 824,306 bytes; cumulative peak working
set 1,904,705,536 bytes. All eight option combinations agree at workers 1/16
or raise the same expected missing-A/B-evidence error. Source size, mtime and
SHA were unchanged. Warm <=5s target met on both; cold-disk behavior and full
JSON publication time are not claimed. No browser visual smoke, Panel restart,
real tester or live DB writes were performed.

Deliberately excluded: new DB schema, persistent derived cache, new dependencies,
extra worker settings, representative reselection, GAP redesign, new price data,
portfolio simulation and four independent Pareto exports.
