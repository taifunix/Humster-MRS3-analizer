# Equity regime M3 read-only validation

Version: Planner PLAN_REVISION P4.2, source approved P3, consolidated by root, 2026-10-04.

Release: **PLAN_APPROVED P3** for the full read-only extraction, independent GPT-6 Sol, 2026-10-04. The user
explicitly authorized root or GPT-6 Sol review after the mandatory Advisor bridge
failed. This is a task-local best-effort exception. P3 resolves both findings
below; the bounded read-only research is released. This plan does not authorize
production implementation, tester runs, DB/cache writes, data deletion, or
persistent status changes.

Contract: [status map M3](../../specs/2026-10-03-equity-regime-status-map.md).
Initial evidence: [inventory report](../../reports/2026-10-04-equity-regime-m3-research.md).

## P4.2 amendment after full extraction

The completed P3 scan produced 30,940 compact facts from 139,740,157 raw equity
points. Classification-only replay is permitted from these immutable facts;
another DB scan needs new evidence that a required field is absent. The user's
held-weekly-ATH requirement (H1) applies to both GROWING and WEAKENING and to
every examined historical prefix: W7 is UP, a strict full ATH occurs after
the W7 left boundary, and report-end close is **strictly greater than** frozen
`HWM(T-7)`. ATH/HWM/close use exact Decimal `>` with no epsilon. The direction
epsilon applies only to normalized trend and endpoint change. Equality fails.

H1 was first caught by a failing test. The final focused suite has 20 passing
tests, including exact equality, tiny positive/negative Decimal differences,
and prefix STALLED proving later RESUMED. The exact old-to-new H1 matrix at
the research anchor `epsilon=0.5`, absolute slowdown `1` is:

| Before H1 | After H1 | Count |
| --- | --- | ---: |
| GROWING | STALLED | 76 |
| GROWING | RESUMED | 54 |
| WEAKENING | STALLED | 256 |
| WEAKENING | RESUMED | 18 |
| **Total changed** | | **404** |

Thus 332 destination statuses are STALLED and 72 RESUMED. The RESUMED cases
come from a previous prefix newly recognized as STALLED. The P4/P4.1 draft
mistakenly called 332 all changed statuses and attached `1e-8` to ATH
comparison; P4.2 corrects both. This section supersedes the G/W weekly
eligibility sentence in section 4 below. All other P3 read-only bounds remain.

The final report must preserve source, extraction-code, preregistration,
compact-fact, classifier, replay, and output fingerprints; verify compact
fact SHA/count/schema before and after replay; compare the 21 independent
Decimal curves; show the full sensitivity grid, Panel projection, holdout
imbalance, unresolved cases, and same-report transition limits. The research
anchor is not an accepted production threshold. Final implementation review
still requires an actual `CODE_REVIEW_PASS`.

Final-review finding M3-F1: on Windows, text-mode hashing normalized CRLF in
`facts.jsonl`, producing a summary fingerprint different from the artifact's
real SHA. A CRLF fixture failed before the fix. Replay now hashes raw bytes and
checks the artifact SHA before, during, and after classification. The focused
suite passes 20 tests; the full replay kept its canonical classification SHA.
Independent GPT-6 Sol re-review returned `CODE_REVIEW_PASS` for M3-F1 and the
full research harness; no material findings remain.
The user requested a final Opus review; Claude Opus 5/high returned a second
independent `CODE_REVIEW_PASS` on the completed read-only research packet.

## Findings ledger

| Finding | Disposition |
| --- | --- |
| R1: degree/atan grids conflict with normalized M3 speeds | Use log-pp/30d grids below; no degree conversion |
| R2: FLAT/COLLAPSING action policy | Both confirmed states have DROP + REJECT60; predicates pending |
| R3: unbounded full scan | One ordered SELECT, fetchmany, bounded pending worker tasks |
| R4: cache and history evidence | All 30,940 fresh; no retained earlier results |
| R5: left-boundary carry/HWM | Preserve effective equity and full historical HWM at each left edge |
| R6: PRE28/UTC | Actual first point to T-28 >=14d; UTC sessions |
| R7: unproved prior stall | Emit resume_candidate_unproven when examined prefixes do not prove stall |
| R8: spill/resources | Cap DuckDB memory/temp; stop on insufficient verified C: budget |
| R9: stored-label determinism | Recompute from fixed facts and explicit prefix history, never saved output labels |
| R10: source validity | Structural invalidity precedes nonpositive equity and metrics |
| R11: scope | Descriptive only; current/T-7/T-14 proxies, no independent retest claim |
| R12: mandatory Advisor unavailable | User authorized task-local best-effort exception and independent GPT-6 Sol review |
| E-M3-P1: source ordering and accounting | ORDER BY result_id,sample_index; validate timestamp chronology before dedup; conserve every row/group |
| E-M3-P2: stale release gate | User exception recorded; independent GPT-6 Sol PLAN_APPROVED P3 received |

## 1. Freeze before M3 effects

Write a private preregistration manifest with source identity, code version,
boundaries, formulas, candidate grids, deterministic seed, symbol-level
development/holdout split and output columns. Candidate values are not
accepted production defaults.

- Direction epsilon: `[1e-8, 0.25, 0.5, 1, 2]` log-pp/30d, applied to both
  trend30 and endpoint30.
- Absolute slowdown delta: `[0, 0.25, 0.5, 1, 2, 5]` log-pp/30d.
- Relative slowdown fraction: `[0, 0.1, 0.2, 0.3, 0.5]`.
- Evaluate absolute/relative slowdown separately; both v14 and v7 must be
  slower than v28. One slowdown remains GROWING.
- Select descriptive stability/manual agreement, not future PnL or minimum
  unresolved count. Unaccepted FLAT/COLLAPSING predicates remain explicit
  candidates, not a catchall for rejected rows.

## 2. Read-only extraction

Open the configured schema8 source with `read_only=True`, set UTC and retain
a read-only transaction/lock throughout extraction. No writable fallback.
Before/after: file identity, size, mtime, digest; schema/logical inventory;
cache source revisions and fact digests; Python/DuckDB/git/harness versions.
Any source change invalidates the run. Metadata equality alone does not
prove absence of concurrency; the stable read-only snapshot/lock is required.

Use one raw SELECT with `ORDER BY result_id, sample_index`, consumed via
fetchmany. Validate strictly increasing nonnegative sample indices and
nondecreasing UTC timestamps within each result before dedup or window formation;
do not sort away invalid chronology. Corrupt chronology yields NOT_EVALUATED.
Parent groups one result at a time. Validate per-result and total raw row
conservation, unique result emission, final groups and results with zero rows. ProcessPool has 16 workers and a fixed
small maximum of pending futures; workers receive data, never DB connections.
Do not fetchall 139.7m rows, create SQL tables, or issue worker raw scans.

DuckDB: 16 threads, about 4 GiB memory; verified task-specific C: temp directory
and capped spill with a safety reserve. Recheck free space (inventory: 38.5 GiB).
Stop on resource exhaustion/oversized groups rather than increasing limits
without evidence. Output only compact local research artifacts.

## 3. Exact facts

Reuse validated current R7.3 7/14/28 facts. In the same raw pass compute
Decimal38 PRE28 and T-7/T-14 prefix facts, full ATH/HWM, DD14/DD7, sample
validity and raw-vs-grid missed peaks.

- UTC T is each report's end; carry the last effective sample through gaps.
- Effective duplicate timestamps use greatest sample_index; raw risk keeps
  every duplicate in timestamp/sample order. Ambiguous index ties are invalid.
- First point initializes ATH. Strict updates belong to `(T-28,T-14]`,
  `(T-14,T-7]`, `(T-7,T]`.
- previous_ath_w7 is frozen HWM(T-7); fresh weekly updates occur after T-7.
- Preserve historical HWM and carried equity at risk-window left edges, even
  when no raw point is exactly there. Include intrawindow drops after recovery.
- PRE28 is available iff T-28 minus first actual in-report timestamp >=14d.
- Invalid/nonpositive source cannot supply log metrics.

## 4. Classification and temporal proxies

Apply M3, including G/W all-UP/available-PRE-UP/distinct-stage ATH;
WEAKENING requires both short slowdowns; STALLED requires W28-UP/allowed
PRE/DD<23 and gets PASS+RESERVED; RESUMED requires proved preceding stall,
W28-UP/W7-UP/close above frozen ATH/DD<23 and allows W14-MIXED.
PRE-DOWN or W28-not-UP alone yields DROP without REJECT60. Confirmed
DECLINING/FLAT/COLLAPSING yields DROP+REJECT60. DD equality 23 is excluded
from STALLED/RESUMED.

Recompute prefixes without future points. Only a stall actually proved at an
examined earlier prefix supports a proxy RESUMED; otherwise record
resume_candidate_unproven. Enforce S -> R -> G/W, never direct S -> G/W.
Keep uncovered valid patterns explicit. Persist no labels in the source.

Current, T-7 and T-14 are same-curve proxies. All 30,940 strategies have only
one retained result; these are not independent tester retests or recovered
erased reports. Identical fixed facts/history must give identical output.

## 5. Focused verification

TDD for the minimal research harness; `.venv` only, test basetemp on verified
C: task directory, cleanup only that resolved directory. Cases: UTC,
PRE14 boundaries, carry/HWM at left edge, duplicates/ties, invalid/nonpositive,
strict ATH boundaries, frozen110/112/115-close111, DD23 boundaries,
MIXED14-RESUMED, one/two slowdowns, absolute/relative grids, proved/unproved
stall, no direct S->G, no-lookahead prefixes, raw missed peaks, deterministic
compact reclassification. No unrelated broad tests or optional benchmarks.

Independent Decimal reference on preselected stratified curves checks facts
within 1e-8 and outcomes exactly. Split development/holdout by deterministic
symbol hash. Do not claim manually validated statuses before reference and
visual/sample inspection actually occur.

## 6. Evidence and acceptance

Report source-wide counts and separately Panel/latest-selection projection;
PRE coverage, ATH/DD, cache/reference differences, missed peaks, sensitivity
and churn, holdout comparison, prefix transition matrix, unresolved reasons,
examples, wall/memory/temp/query/row counts and source invariance. Give threshold
proposals or explicit unstable candidates; no predictive/live-use claims.
Committed report omits local paths; private manifest contains provenance.

After integration/verification, submit compact ASCII implementation diff and
evidence to independent review. `CODE_REVIEW_PASS` is still required for
accepting a committed harness; Advisor failure best-effort does not silently
waive implementation review.
