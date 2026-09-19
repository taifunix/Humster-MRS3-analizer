# MRS3 — current verification

**Updated:** 2026-09-19
**Current branch:** `main`

## Pair screener (SCREENER 01) — Этапы 0–4 done, UI (Этап 5) next (2026-09-19)

Active spec: [docs/specs/2026-09-18-pair-screener.md](docs/specs/2026-09-18-pair-screener.md)
(DRAFT). Handoff:
[docs/superpowers/plans/2026-09-18-pair-screener-handoff.md](docs/superpowers/plans/2026-09-18-pair-screener-handoff.md).

On the first practical screening run (`D:\!Humster\tester\report\my_test`,
16 pairs, confirmed full 304-run/pair grid — not the ~2% the handoff
expected, 4864 `reports_history.csv` rows, no gaps/dupes): section 6.1a is
resolved as **CSV-only** — the CSV already carries explicit per-run
parameter columns (symbol/time_frame/ma_close_long.len/ma_long.len/
ma_long.multiplier), confirmed by an 81-report spot check against
`extract_html_strategy_settings` (0 mismatches); no order-based
reconstruction and no HTML parsing needed for evaluation. Verdicts computed
for all 16 pairs (section 6.4 thresholds): GO — MSTRUSDT, KORUUSDT,
SOXLUSDT; CHECK — INTCUSDT, SKHYUSDT, SNDKUSDT, CRCLUSDT, CLUSDT, SNXXUSDT,
TSLAUSDT, XAGUSDT; STOP — AAPLUSDT, NVDAUSDT, XAUUSDT, BZUSDT, GOOGLUSDT;
INCOMPLETE — 0. Result saved to `data/screener_verdicts_2026-09-18.csv`
(gitignored).

**Known blocker, accepted by the user (2026-09-18):** spec section 9's
full-collection cross-validation (2+ pairs per verdict bucket, full 5472-run
collect+analyze) was explicitly skipped in favor of starting screen
implementation now. Section 6.4 thresholds are therefore unconfirmed on new
pairs; if the first real full collections on GO/STOP pairs don't match the
expected pattern (STOP pairs not reaching ≥100 good points; GO pairs
reaching it), thresholds must be revisited immediately — see spec section 9/10.

Note: the bot/tester root moved since the 2026-09-18 handoff was written —
it names `D:\SHARE\!MN\hamster\hb`, but the actual root used for this run
(and confirmed by the user) is `D:\!Humster`. The handoff has been annotated
with this; the implementation plan below was authored fresh and uses only
the current `D:\!Humster` path throughout.

Implementation plan (approved, committed at
[docs/superpowers/plans/2026-09-18-pair-screener-implementation.md](docs/superpowers/plans/2026-09-18-pair-screener-implementation.md),
kept in sync with progress as each stage lands): scope grew twice mid-
implementation by explicit user decision, both folded into the same pass —
SHORT side is now in v1 alongside LONG (own tester-config template
`config_tester_short_screen.json`, own multiplier parsing — comma decimal
separator, `>1`), and screening now reads/writes a TradFi liquidity
registry workbook (`input/bybit_tradfi_liquidity.xlsx`, copied in from the
user's working copy): reads the pair universe and listing dates from its
read-only `Пары` sheet, and records its own findings in a `Скрининг` sheet
it exclusively owns (atomic upsert, never touches the other sheets, never
overwrites manually-filled final-decision columns).

**Этапы 0–3 are done** (including a retroactive Этап 3 addendum,
`export_verdicts_csv`, added while starting Этап 4 — CSV bytes for a panel
download, header/row values both derived from `dataclasses.fields`/
`asdict(PairVerdict)` rather than two hand-written lists, with
fixed-point Decimal formatting and spreadsheet-formula-injection escaping
for string fields). `src/mrs3/screener/` now has `config.py`
(`ScreenerConfig`, incl. `liquidity_registry_path`), `render.py`
(`render_screener_tester_config`, side-agnostic), `errors.py`
(`ScreenerEvaluationError`), `registry.py` (registry read/write),
`listing.py` (listing-date resolution: registry → dates.xlsx → Bybit), and
`evaluate.py` (per-report verdict computation for both sides + CSV export).
`tests/screener` is 76/76 passing; every file went through several rounds
of independent review until a round returned zero findings.

The Bybit fallback's rate-limit protection was removed then restored in the
same session: it was first dropped as "overcautious" per an explicit user
call, then the user asked for it back with real research behind it. Fetched
Bybit's official docs
(https://bybit-exchange.github.io/docs/v5/rate-limit,
https://bybit-exchange.github.io/docs/v5/market/instrument): the concrete,
documented mechanism is 600 requests/5s per IP → HTTP 403 → automatic
10-minute IP ban; `instruments-info` is public/no-key with no
endpoint-specific stricter limit. The implemented margin: a client-side
throttle enforcing >=250ms between Bybit requests (<=20 req/5s, ~30x
headroom) within one `resolve_listing_dates()` call; a later review round
found the first version wrongly persisted this across separate calls too
(risking silently trusting a stale/bad prior read indefinitely) — reverted
to per-call only, matching the spec's literal "на время вызова" wording;
callers must pass the full symbol set in one call to keep the bound. Plus
explicit HTTP 403 detection that stops immediately with a clear "wait ~10
minutes" error instead of continuing to hit a blocked IP for remaining
symbols.

**Этап 4 is done (2026-09-19).** `panel_testing.py`: additive
`template_override: tuple[str, Callable] | None` kwarg on `prepare()`/
`fill()` — originally two separate kwargs, unified after review flagged that
separate kwargs let a caller pass a config path without its matching
renderer; `expected_screener_runs()` (now also enforces the USDT suffix and
wraps template reads in `except OSError`, both review-driven fixes); a new
`_resolve_repo_path` path-traversal guard. `panel.py`: `local_screener_
status/fill/start/stop/evaluate/export` controller methods sharing the same
`LocalTestingService`/`TesterTargetLock` as RUNNER 01; a shared
`_split_symbols_field` helper now used by both RUNNER 01 and SCREENER 01's
symbol parsers (review-driven, so a future separator fix can't silently
diverge between them); `_serialize_screener_verdict` deliberately does not
reuse the existing `PanelController._jsonable` (that helper formats
`Decimal` via `str(value)`, risking scientific notation for small pnl30/dd
values — the same bug class already fixed in CSV export); new
`/api/v2/testing/screener/{status,fill,start,stop,evaluate,export}` routes,
with the GET `/export` route gaining an `except Exception` → logged JSON 500
fallback matching the sibling `/performance-v2/catalog` route. `tests/test_
panel_testing.py` is 51 passing. Combined `tests/screener tests/test_panel_
testing.py tests/runner tests/test_panel.py` — 421 passed, 1 skipped (the
skip is a pre-existing Windows symlink limitation unrelated to the
screener). Went through 5 rounds of independent review to convergence; one
remaining round-5 finding (double file-read of the same template inside
`local_screener_fill`'s preview-count call) was deliberately not fixed —
`expected_screener_runs` must also work standalone before any fill, for the
Этап 5 live-preview UI, and the extra read is on a manual button click, not
a hot path. Next step: Этап 5 (UI — `index.html` screener card, `app.js`,
`app.css`).

Weighted-search Phase 6 implementation evidence is recorded in
[Phase 6 evidence](docs/superpowers/plans/2026-09-16-portfolio-optimizer-weighted-search-phase-6-evidence.md).
The final Opus high re-review returned `CODE_REVIEW_PASS`; Phase 6 is accepted.

Accepted implementation slices cover strict weighted-search settings and
migration, the long-only frozen Campaign boundary, private weighted source
rows and source geometry, the dedicated template (`position_priority=3`),
the normal official local fact path, reference-derived margin coefficients
across tiers `[0, C]`, executable JSON payloads, deterministic executable
identity, typed A0/full-position readback, and weighted Summary/Members
projection with two reserves and explicit `UNKNOWN` diagnostics.

Legacy Campaign modes remain rejected; legacy export/render and DuckDB
finalist-input compatibility remain separate and unchanged.  Stage 2 and
tester submission remain disabled and no tester, network, Bybit, retest, or
database write was run.  The configured local minute root has 3712 CSV files;
this evidence does not claim live-data verification.

Verification recorded for this worktree:
- adapter suite: `263 passed`;
- Panel suite: `151 passed`;
- minute-capacity suite: `22 passed`;
- combined adapter/config/input/margin/minute-capacity/export/render/Panel/
  static-UI/finalist-retest command: `930 passed in 146.60s`;
- exact reruns of the reviewer-requested full-suite failures: corrected static
  Panel and common-worker cases pass; the unchanged PerformanceDB HTTP test
  passed on both parent commit `a452958` and the current tree after one local
  two-second scheduling timeout;
- `git diff --check`: clean (existing line-ending warnings only).

Slice-level independent reviews and the final full-Phase-6 Opus high re-review
returned `CODE_REVIEW_PASS`; all Phase 6 checklist items are closed.
PerformanceDB review-state prerequisite (2026-09-15): automatic selection now
populates only Auto Status/Auto Rank. Effective finalists require explicit
imported User decisions; imported decisions survive later unreviewed runs, and
auto-only runs cannot create a current-effective cohort or export. Focused
selection-review and Panel regressions pass.

Weighted-search Phase 5 handoff (2026-09-15): implementation, focused/relevant
tests, benchmark, and experimental defaults are recorded in
`docs/superpowers/plans/2026-09-14-portfolio-optimizer-weighted-search-phase-5-evidence.md`.
The directed benchmark used 13 prepared explicit imported User FINALIST rows
(13 strategies, 10 symbols); benchmark-only aliases exercised N=13 for three
duplicate symbols and do not change the production one-LONG-per-symbol MVP.
The factual matrix is T=12672 at 5-minute cadence over 2026-07-27..2026-09-09,
with 0 invalid cells, 6519 actions, and 65646 equity samples. Local seven-day
minute capacities are READY for all 10 symbols. Input/preparation/LP/replay/
margin/render wall times were 3.197/5.852/29.016/1.697/0.113/0.165 s;
margin is factual UNKNOWN because reference coefficients were unavailable.
Bootstrap workers 1/30 PASSed in 387.581/43.589 s (8.89x; semantic outputs
equal after operational fields). Full weighted_search workers 1 was correctly
partial at 1100/3000 with WALL_TIME_LIMIT (982.201 s), while workers 30
PASSed all 3000 scenarios and 8 candidates in 269.187 s. Experimental defaults
remain K=8, wall=900 s, solver=30 s; workers remains the existing
  `duckdb_import.workers` setting. Independent Opus implementation review
  returned `CODE_REVIEW_PASS` after P5-R4a, P5-R5a, P5-R5a-T1, and P5-DOC1
  were resolved. Phase 5 is accepted; commit/push is next, followed by Phase 6.
  No real tester, exchange, network, retest, database write, or trading-readiness
  claim is made.

2026-09-14 current planning task: root (explicit user override of Planner Sol)
replaced the accumulated weighted-search discussion with a technical plan at
`docs/superpowers/plans/2026-09-12-portfolio-optimizer-weighted-search-discussion.md`.
Opus remains the independent Advisor; two automatic review calls timed out after
300 s. The user then supplied an external PLAN_REVISE for R1 (B1-B7, O1-O7).
Root revised the plan through R2/R3 and recorded dispositions and counterexamples in
`docs/superpowers/plans/2026-09-14-portfolio-optimizer-weighted-search-review-response.md`.
The user then supplied narrow PLAN_REVISE and finally PLAN_APPROVED for R3.
After R3 approval the user rejected loss of limiter sizing benefit and requested
a correction. Root prepared R4: full IM coverage and full profile MM before
reaction; profile free reserve for retained positions after reaction. Existing
account drawdown and 1.5% all-in excess-position loss remain. Initial LP and
pruning now retain x that fails off but passes with L. The user supplied an Opus
PLAN_APPROVED for R4. Its one optional clarification is adopted in R4.1:
after non-proportional sizing, recompute priorities and all dependent margin/replay
facts, accept or reject once, and never start a hidden stabilization loop.
An explicit implementation checklist is now in section 13. Reserved control
joint-test slots remain unchanged.
Current implementation handoff: Phase 3 of WS1.1 remains accepted after independent
Claude Opus 5 high `CODE_REVIEW_PASS`. Its weighted SciPy/HiGHS LP, exact
peak-equity drawdown and P30 capital checks, bounded adaptive frontier, compact
candidate-search result types, and additive `WEIGHTED_VECTOR_V1` sizing seam are
recorded in
`docs/superpowers/plans/2026-09-14-portfolio-optimizer-weighted-search-phase-3-evidence.md`.
Phase 4 margin/limiter/replay/priority integration is accepted at the fixture
boundary after independent Claude Opus 5 high `CODE_REVIEW_PASS`, with the scope
and conservative UNKNOWN boundary recorded in
`docs/superpowers/plans/2026-09-14-portfolio-optimizer-weighted-search-phase-4-evidence.md`.
The executor focused suite passed 198 tests; root relevant broader verification
passed 414 tests in 36.45s, and `git diff --check` is clean. No real tester,
exchange, migration, database, network, or generated artifact was used.
Root staged `git diff --cached --check` also exited 0 after the whitespace
correction. Live release evidence remains unchecked and
is deferred under separate authorization; the conservative UNKNOWN fallback is
not legacy mode. Next: Phase 5 implementation after Phase 4 acceptance.

Phase 0 remains complete: only mapping Campaign
`PORTFOLIO_WEIGHTED_CAMPAIGN_V1` / `WEIGHTED_V1` / `WS1.1` is accepted, legacy
Campaign and `stage1_mode` fail closed, and a valid Campaign returns only
`WEIGHTED_SEARCH_NOT_IMPLEMENTED` until the weighted search phases. Focused
adapter/Panel tests passed 136, integration tests passed 46, and whole-repo
collection found 3823 tests without import errors. No real tester, exchange, or
database run occurred. Real tester authorization remains required separately.

WS1.1 / plan R4.2 address user-supplied narrow PLAN_REVISE
B1-B3: coefficient-times-x IM/MM, UNKNOWN release keeps full I_held=I_all,
and candidate_search-qualified result types. Added phase-4 release evidence
checkbox; exact half-upper seed target and shared bootstrap indices clarified.
WS1.1 at docs/specs/2026-09-14-portfolio-optimizer-weighted-search.md
and ADR-0036 received user-forwarded Opus PLAN_APPROVED on 2026-09-14.
The optional section-9 qualifier is adopted: top-ell only when CONFIRMED,
full I_all when UNKNOWN. Main spec amendment and ADR are accepted; phase-0
document/spec approval checkboxes are complete. Runtime Campaign-version and
implementation phases remain unchecked. Next: phase-1 source sizing calibration.
WS1 defines missing-data dependencies, a Campaign-memory cache, bootstrap RNG,
margin/excess-position rules, versioned mode and acceptance evidence. Runtime unchanged.
The R4 executable synthetic check passed: off/L10/L9/L8 margin banks are
2778/1936/1767/1784 USDT rounded up for fifteen 1000-USDT positions; at B=2000
proportional position capacity is 720 off versus 1033.49 with L10 before qtyStep.
Checks cover all state inequalities, arbitrary retained subsets, DD/liquidity
constraints and no guaranteed benefit when reserve=0 or full MM binds.
This is arithmetic evidence, not actual exchange rates or trading results.
Parallel execution uses the existing single duckdb_import.workers panel setting;
no new solver/bootstrap worker knobs. User hardware is 36 cores/200GB, while
os.cpu_count() reports 34 visible logical CPUs in this agent environment.
Budget defaults require measurement of concurrent tasks and dependent rounds.
Current Panel
search runs through adapter/candidate_search, not the older search/integration
fixture path. Plan reuses Campaign/results/sizing/market-data/tester seams and
replaces only the new-mode composition generator and dependent sizing metrics.
Luna implemented the Phase 1 regression and Opus accepted it. The imported reports
and user-confirmed dynamic tester contract establish the current per-cycle sizing
basis without enabling use_fix. Price/Cost remain actual fills. Boundary cycles
without a known opening basis stay UNKNOWN. No tester run was performed.
Research arithmetic check: 14 direct-bank/bootstrap identities and 10 finite-
source slot-model checks passed against independent small-state enumeration.
These checks do not establish trading-model accuracy or end-to-end performance.
The R2 research check also covers fixed-bank homogeneity, baseline versus
counterfactual occupancy, the omitted drawdown/overflow state, top-k epigraph
and empirical-quantile definition; it counts LP dimensions, not solver runtime.
Engset is removed; frozen-cycle replay supplies MODEL ordering for L, with
UNKNOWN on missing attribution. Limited joint comparisons stay within
Max candidates per profile. The R3 synthetic check passed using current margin.py:
pending IM differs from position IM; limiter loss affects reserve and required B.
LP calls share a proposed cap of 20 per profile, up to eight adaptive PnL goals
and two CDaR alternatives; operational time defaults follow measurement.
No optimizer runtime code/config was changed in this planning task. Earlier
entries below are historical evidence; the technical plan supersedes their
draft section references and discarded proposals, not their verification facts.

2026-09-13 finalist refresh decision: user chose new reports for all 21 current
finalists instead of further source discovery. Draft section 3.1 separates a
one-time compatible enrichment/REPLACE path from the future automatic importer
contract. Disposable migrated-schema probe: 7 existing ADD/REPLACE/review tests
passed in 12.29 s (20 extended-schema initializations). A nullable result metadata
column preserves these paths; adding action columns would break positional append.
Reuse existing raw_action_json for Price/Cost, with revision-checked result metadata.
The first stdin-based probe failed on Windows multiprocessing spawn; rerunning
from a real guarded script passed. No production migration, new retest or new
report load performed; one-time loader and copy verification remain to implement.
Evidence: docs/superpowers/plans/2026-09-13-portfolio-optimizer-finalist-sizing-audit.md.

Portfolio search discussion revised 2026-09-13: the Russian design is in
`docs/superpowers/plans/2026-09-12-portfolio-optimizer-weighted-search-discussion.md`.
It proposes liquidity-capped position notionals and required capital as joint
outputs: a PnL30/capital frontier, model-minimum bank and model saturation bank.
Proposed CDaR-assisted search with a separate MaxDD constraint replaces the draft's weight grid,
D_base and 50% diversification discount. A stationary-bootstrap shortlist check
may raise the required bank; this checked capital is not a proven global optimum.
Both research papers were examined, including the user-supplied 2026 journal PDF.
The synthetic NumPy bootstrap-only benchmark took a median 6.53 s for 12
allocations x 1000 scenarios over 90 days at 5-minute resolution, sampled process
RSS about 51 MiB. This excludes DB load, LP, margin and tester; no production
performance claim is made.
Prior agreements remain: configurable 5-minute grid, full-position liquidity
caps, shared Max candidates, no entry queue, bot priorities 5-to-1/random ties,
SlotScore as an initial priority heuristic, and basic.max_balance with full lot_x
sizing. User confirms bot/tester are the same program. Real execution remains
subject to joint testing; the linear model grants no limiter risk discount.
User clarified available history: about 1-2 months jointly and up to 3-4 months
individually; extending the common history is not feasible. Draft sections 3/3.1
now separate full-history individual preparation from common-window portfolio
calculations. Code inspection found existing raw actions/equity, window cache
and holding median/p95. Proposed additions reuse them while distinguishing
flat-boundary/geometric selection metrics from synchronous linear portfolio
metrics; derived caches must invalidate on REPLACE even when result_id is kept.
Section 12.1 proposes adding a strategy with old USD position sizes fixed,
reusing prepared series and optimizing only the addition and bank, followed by
new whole-portfolio risk/limiter testing. This is not implemented or approved.
User approved MaxDD 20/10/5 by profile, then explicitly approved prior-peak
total equity as the denominator. Section 6.1 and ADR-0035 record this narrow
decision. Sections 7-10 now use the peak-relative path constraint for capital
and the proposed bootstrap check; dollar MaxDD divided by the cap is removed.
The user requested a case-specific CDaR recommendation recorded in the draft.
Section 6.2 recommends relative peak CDaR80, CDaR90 sensitivity and no separate
CDaR hard gate for the first version. For each positive PnL target, find minimum
bank under peak MaxDD/margin, then at that same bank minimize dollar CDaR80 as
a surrogate alternative generator. Recompute true relative metrics on BOTH
solutions; no claim of global relative-CDaR optimality or guaranteed improvement.
MaxDD numbers and denominator are accepted; CDaR remains a recommendation.
The user also accepted checking margin at selected position sizes and at the
profile drawdown boundary, including temporary limiter overflow. Insufficient
collateral requires smaller positions or more capital and a new constraint
check. The user then delegated analysis and the reserve choice; root recommends
retaining ADR-0029 values 20/40/60 for the first version. Section 9 records that
choice, not a claim of optimal calibration. A 10000 peak/initial-bank illustration
with USDT-only collateral and no adjustments gives post-DD IM caps 6400/5400/3800.
At otherwise fixed inputs an IM-bound bank is 25% higher with 60% reserve than
with 50%; a DD-bound bank may be unaffected. Decimal arithmetic checked both
effects. Proposed output identifies the binding risk/margin constraint and
liquidity saturation; sensitivity on fixed positions does not multiply joint
tests or automatically relax profiles. The user has now explicitly accepted
MM limits 50/35/20; exact stress states remain open. No runtime/config or old
ADR change was made.
The user accepted mass execution of all concurrently resting entry orders
before limiter action as a required stress. In response to their simplification
question, the user accepted bounded maximum IM/MM instead of replaying a
separate historical margin series for pretest. Bounds use chosen scenario
position sizes (not all liquidity caps), existing positions and remaining
orders without double counting, and post-DD collateral. Historical equity
for PnL/DD/CDaR and joint testing remain required. Price/sizing envelopes and
coverage of mixed filled/unfilled states still need a justified contract;
this design acceptance does not mean a valid bound is already implemented.
The user also accepted bootstrap horizon H equal to the common available
history of the selected universe, fixed across all candidates in a run.
Dropping a member from a candidate does not extend its comparison horizon.
The user accepted stationary bootstrap with mean block lengths 1/3/7 days,
editable as a list in Optimizer settings and frozen in each run snapshot.
These defaults are project calibration choices, not paper-established optima.
Section 15.1 records the UI/JSON requirement; no settings implementation was
made during this design discussion. The user accepted default p95 and the maximum
of historical MaxDD and each block-length scenario percentile as an initial
gate to evaluate on the data, with an editable percentile. Capital adjustment
keeps USD position sizes fixed and rechecks margin; this is not a calibrated
future-risk guarantee. Draft section 10.1 records the analysis protocol:
same-bank comparisons, capital uplift and binding constraint, tail diagnostics,
block/seed sensitivity, joint-tester comparison and later-period follow-up.
The user accepted 1000 scenarios per block length per shortlisted portfolio
(3000 with default lengths), editable in settings and frozen in the snapshot.
Batching and fixed-scenario reuse during capital search remain required;
these calculations do not multiply joint tester runs. The user accepted a
bootstrap shortlist cap of 3 * Max candidates per profile, with editable
multiplier, deduplication and diversity of capital/PnL/risk alternatives.
The cap does not require filling the list; all final L/priority variants
remain inside Max candidates. Exact diversity selection and additional
sensitivity-check budgets remain open; no runtime was changed.
Section 9 now explains reserve/MM coupling on one collateral base:
I/M <= min(1-r,q/k), k=MM/IM for each nonzero state. With illustrative k=0.5,
accepted reserves imply MM loads 40/30/20, so proposed MM caps 50/35/20 add no
tightening. With k=0.7, BALANCED's proposed MM cap forces 50% free reserve.
Exact Fraction checks passed. These are arithmetic examples, not observed
pair coefficients; MM caps are now accepted as starting values for the new search.
Root's starting MM recommendation remains 50/35/20 after this coupled analysis:
an additional cap when the accepted IM reserve is insufficient, with no extra
tightening at illustrative k=0.5. Public risk-limit reads for five finalist
symbols returned URLError, so current rates and real-portfolio calibration
were not obtained; official formulas and algebra support only the stated
conditional examples. The user explicitly accepted these MM numbers.
The user also requested all adjustable Optimizer parameters in the Panel
Settings block using the same local JSON, including advanced fields and risk
limits. Draft section 15.1 records the UI/config inventory, field descriptions,
bounds, save/reload checks and immutable run snapshots; new editable risk
policy semantics must be versioned before runtime implementation.
Section 15.2 records mandatory Bybit rate-limit investigation and protection
before another real data-loading run after the user reported an earlier ban:
shared worker/process budget, headroom for other clients on the same IP,
fresh snapshot reuse, response-aware backoff and durable cooldown, Panel status
and fake-API verification. Official IP/UID/endpoint rules checked on 2026-09-13;
the reported incident's exact cause is not yet verified. These are planned
requirements, not implemented UI or API protection.
A fixed relative MaxDD cap also admits linear constraints for the additive
pretest model; that does not establish linearity of relative CDaR or bot sizing.
Root one-off checks passed for empirical tail arithmetic (including 200
fractional-boundary cases), zeros and scale/capital behavior. Synthetic 15-series,
60-day, 5-minute, 60-candidate curve + MaxDD + CDaR80/90 benchmark median:
0.0540 s (source matrix 1.98 MiB), excluding DB, LP, bootstrap, margin and tester.
Read-only follow-up confirmed 21 explicit User Status FINALIST strategies across
15 active pairs, all present in the Panel's effective selection view. The
Optimizer reader incorrectly requires user marks on the latest selection run;
the Panel instead preserves each strategy's latest accepted user status across
later analyses. Missing review rows on a new run are not missing user finalists
or database corruption. Record this reader mismatch for implementation; do not
require the user to re-mark finalists. The effective view also includes two
automatic finalists; they were excluded from this explicit-user inspection.
Result metadata for the 21 user finalists reports a common 29-day interval
(2026-07-30 through 2026-08-28 UTC), with individual histories of 29-70 days.
This checks selection and period metadata, not equity completeness or CDaR
calibration. No statuses, data or runtime code were changed.
Configured Opus Advisor status was ready, but the single review_plan call exited
with error and returned no decision. CDaR proposal is NOT_ADVISOR_APPROVED;
no Executor was released and no implementation acceptance is claimed.
Open: independent validation and calibration of the CDaR proposal,
implementation details of input validation, margin stress
contract, bootstrap calibration and preliminary search budget,
frontier/limiter budgets and final recommendation rule. The draft is
not an approved implementation plan; active runtime specs were not amended.
The user tentatively accepted 20 configurable PnL targets (j/20 of the
profile's attainable P_cap), up to 40 initial primary/CDaR alternatives before
deduplication. Capacity solves and repair retries are additional work, not
included in that candidate count. This is an initial budget to evaluate,
not a calibrated optimum; the optional portfolio PnL floor is specified below.
The user accepted up to 3 repair recalculations per original candidate,
excluding its initial solve. The counter survives subsequent member exclusions;
exclusions remain local to the candidate. Every repair rechecks feasibility,
and a valid primary solution survives failure of its CDaR alternative.
The editable retry limit and rejection diagnostics are recorded for the UI.
The user accepted admission of selected explicit user FINALIST strategies
regardless of negative net PnL on the common or full individual period.
Losses retain their sign in portfolio objectives; positive portfolio targets
and all risk/margin/liquidity/order constraints remain. No discarded strategies
are added, and no per-strategy presence enumeration is introduced. Reports
flag losses and distinguish common/full-period net PnL and dates. Priority
group handling of negative scores remains open. Runtime filters are unchanged.
The user accepted an optional portfolio net PnL floor in USDT per 30 days,
disabled by default. Portfolio objectives remain strictly positive; this
does not filter individual strategies. Show P30 in USDT, required capital and
P30/capital together. Enabled floors constrain target generation without
increasing K; duplicate targets collapse, and an unattainable floor is reported.
The UI/JSON requirement is recorded, not implemented.
The user accepted one startup validation of all selected finalists: valid but
short/sparse histories produce warnings with periods and completed-position
counts; missing mandatory inputs prevent search with explicit per-finalist
reasons. No silent exclusion or zero-risk imputation. Candidate search reuses
the validated snapshot; a changed input set is validated in a new run.
Per-candidate sizing/risk/margin checks remain required. No runtime changes.
Limiter discussion now considers a synthetic no-queue slot model from entry
frequency and paired holding-time/PnL observations, rather than historical
entry replay; its generative assumptions and budgets are not approved.
The user excluded overflow-entry and forced-close PnL from the preliminary
model; joint tester and the existing worst-case margin check remain necessary.
Section 14 records a synthetic event-model timing experiment and its limits:
3.246 s for 100 scenarios across each of 13 limits, 5.943 s for 20 scenarios
per limit at tenfold event frequency; 32.5/297.1 s per composition at 1000
are extrapolations, not full-run measurements. Process memory measurement failed.
The user wants whole-Optimizer timing before deciding whether simplification
is necessary. Stage wall times and actual work counts are required; profiles
and unique compositions can multiply work, but slot simulation must not be
nested inside every risk-bootstrap path or capital-search step. No full runtime
or relative stage share has been measured. The earlier automatic 100/1000
refinement proposal is replaced by the accepted budget below; about 16 minutes
is not an accepted timeout.
The user clarified overflow positions are closed by market, normally within
minutes, and proposed a fixed 1.5% total-loss stress per excess full-position
notional. Section 9 records this editable scenario assumption instead of
holding every excess position through its full historical drawdown. Closing
fees and slippage are now included in the fixed 1.5% by the user's latest
clarification; no additional fee/slippage surcharge applies to this stress.
Source net PnL and actual tester fees are unchanged. Costs already in equity
are not duplicated. Loss remains after closure;
pre-close margin is not released early. Only the excess set receives this
charge, with a disjoint surviving set following limiter priorities. It is a
single-episode collateral stress, not an automatic deduction from monthly PnL.
Examples checked: three excess positions of 1000 cost 45 USDT including costs;
seven cost 105. The 1.5% is not an execution guarantee. No runtime changes.
The user accepted preliminary limiter values 1..N-1 plus disabled (0), with
editable integer step default 1. N counts positive-sized participants. Under
the initial one-slot/all-participating model, L=N duplicates disabled and is
omitted; N=1 uses disabled only. All final variants still share Max candidates.
The user accepted default 100 slot-model scenarios per composition/L, editable
independently of risk-bootstrap scenario count. Compared L values share
generated entry opportunities and paired holding/PnL observations. A separate
1000-scenario analysis run is available by changing the setting; no automatic
refinement stage is introduced. Full generative assumptions remain to specify.
The user accepted narrowing expensive slot simulations via a cheap collateral
stress scan for fixed composition, sizes, bank and priorities. Select the largest
passing L plus a few stricter passing neighbors (or disabled plus nearby limits
if disabled passes); neighbor count is still open. The prior 1..N-1/disabled
grid now applies to the cheap scan, not mandatory simulations of every L.
A full candidate includes composition, USD sizes, bank, limiter and priorities.
Changing sizes or bank creates a new candidate with fresh order/liquidity/risk/
margin and limiter-range checks while reusing unchanged inputs. Each joint test
still consumes one Max candidates slot. The user accepted one additional sizing
pass over the original shortlist, fixing bank, limiter and source priorities.
Reoptimize member sizes within all existing constraints, without adding new
strategies or implicitly reducing bank. Recheck candidate metrics and active
composition; keep valid originals and deduplicate alternatives. New alternatives
do not spawn another pass. Config default 1, 0 disables (MVP supports 0/1).
The separate three-attempt order-repair budget and shared Max candidates remain.
The exact fixed-L solver formulation still needs its implementation contract;
the accepted pass budget is not proof of a linear model or improved slot PnL.
The user accepted one ranking objective for all profiles: maximize net P30 at
the same bank subject to profile constraints (MaxDD 20/10/5, free reserve
20/40/60, MM load 50/35/20). Preliminary income accounts for blocked entries in
the limiter model; keep lower-risk alternatives at comparable income. Different
banks retain capital/income alternatives rather than one absolute-PnL winner.
Final ranking and constraint checks use joint tester results. Section 13 removes
the earlier open profile-specific Recovery ordering. Income closeness and the
allocation of Max candidates across capital levels/alternatives remain open.
The user expects liquidity caps/basic.max_balance to leave available capital
unused for further position growth. Section 13 records saturation-aware capital
levels rather than mandatory equal quotas extending to all available cash.
Show required checked capital, model saturation and additional available funds
separately; excess over checked capital is not guaranteed withdrawable cash.
Position-cap sums are not account capital. Additional bank alone need not fill
tester slots, but changed sizing dynamics or collateral scenarios are distinct
full candidates; analytical sensitivity does not replace joint evidence.
The user accepted preserving a high-income candidate near model saturation,
some lower-capital alternatives when slots permit, and other promising
composition/size/limiter variants. Most slots serve distinct trading variants;
there is no mandatory idle-cash grid or requirement to fill Max candidates.
Exact quotas and diversity criteria remain open.
The user accepted two stricter passing limiter neighbors by default, plus the
main passing choice: up to three slot-PnL evaluations at fixed composition,
sizes, bank and priorities. Pick neighbors from the passing cheap grid; disabled
as the main choice is followed by the largest passing finite limits. Fewer
passing choices means fewer evaluations; no passing choice means infeasible.
The nonnegative neighbor count is editable in UI/JSON and frozen in the run;
0 keeps only the main choice. Joint tests still share Max candidates.
The user accepted coarse grouping of similar SlotScore values into no more
than five priority groups. Higher net USD per adjusted occupied-slot hour gets
a lower numerical priority; use 1..5, never exempt priority 0. Do not require
five populated groups or equal group sizes, and keep identical scores together.
This replaces the proposed fixed 1/3/5 grouping. Numerical similarity and
insufficient-data handling remain to specify; negative scores remain signed.
The user rejected capping max_balance at starting capital: retain native sizing
growth/shrinkage including UPNL up to each liquidity-derived cap. No separate
capital-growth trajectory search is introduced. Section 12 uses the actual bot
sizing base and requires B_sat_candidate=max(member max_balance) for each fixed
configuration, distinct from the freely reoptimized B_sat_model. Report surplus
above this threshold as saturation surplus, not guaranteed withdrawable funds;
remaining collateral and exchange transferability still matter. New pairs use
the partial-update/joint-check path. Native sizing dynamics remain a joint-test
responsibility; no runtime or settings change was made.
The user delegated choosing the averaged limiter-income model. Section 11.1
specifies empirical paired wait/holding/net-reward cycles, independently sampled
per strategy, shared schedules/seeds across L, no queue or invented retries after
a rejected cycle. Rejected virtual cycles consume no slot but preserve that
strategy's schedule. Warmup defaults to H, then measure H; entry-cohort full-cycle
rewards estimate throughput, not ending equity. Apply the mean signed reward
difference from disabled to the original common-equity P30; L=0 is unchanged.
This preserves the baseline and avoids division by weak/negative PnL. Boundary
and independent-arrival assumptions are explicit and need joint-test calibration.
Missing cycle/normalization inputs produce unavailable diagnostics, not invented
signals. Event processing, cached schedules and focused invariants are specified;
the new model has not been implemented or benchmarked. UI/JSON includes warmup.
The user accepted the MVP risk link: limiter-aware margin with overflow stress,
no arbitrary discount to historical/bootstrap DD, real limiter DD verified by
joint testing. This can miss beneficial limiter-dependent risk allocations;
unlimited historical DD is not guaranteed to bound limited actual DD.
Requested pruning is now specified in section 11: record all active constraints,
skip infeasible uniform-growth directions under DD/liquidity, but retain the
bounded redistribution pass; reject invalid full variants before expensive
checks and deduplicate exact executable configurations. Historical/bootstrap
risk is reused across L at identical x/B and risk inputs; stress/PnL differ.
Model dominance requires comparable objectives and is not a joint-test proof;
do not infer monotonic income in L or global optimality from an active DD cap.
An exact Fraction check passed: uniform scaling at the derived bound satisfies
peak-relative DD, and a larger scale violates it on the example path.
Documentation whitespace checks passed; no runtime implementation was changed.
The user approved per-position-cycle source normalization with use_upnl=true.
Sections 3.1/5 now use full planned source notional S_i,k per cycle, applying one
linear factor to its net PnL/equity increments when uniform scaling is supported;
normalize before resampling, preserve partial actions, losses and boundary UPNL.
Code confirms typed actions and episode reconstruction, not USD units or actual
order-sizing provenance. Placement-time sizing may differ from entry-time balance;
use_frozen_balance and within-cycle proportionality need real-data verification.
Unknown bases cannot silently become initial-bank or average-size substitutes.
Original results stay unchanged; derived caches are versioned. No mandatory
bulk retest or disabling UPNL; runtime normalization is not implemented here.
Real finalist read-only audit completed; evidence:
docs/superpowers/plans/2026-09-13-portfolio-optimizer-finalist-sizing-audit.md.
21 manual/effective finalists on 15 pairs have 12,231 actions, 97,179 equity
samples and 2,582 completed cycles; 1,626 cycles fit the 29-day common window.
13 original HTML reports found by name match retained DB actions and equity at
stored decimal precision, with six filtered prefixes. Size is asset quantity;
HTML Price/Cost are absent from typed DB actions and raw_action_json is null
throughout this set. Stored commission_rate is TakerFee while actual fee/Cost
ratios include maker and taker, so fee-based notional recovery is invalid.
Initial report bank is not first retained-cycle bank. Full planned sizing still
needs provenance: actual fill Cost alone does not establish full intended size.
No DB changes/retest performed. Draft records one-time source enrichment as a
prerequisite to justified normalization, not a failure of finalist selection.
Next discussion: source enrichment/sizing denominator and consolidation. UI settings and Bybit request
budget requirements are recorded for implementation.
Short-history statistical limitations remain explicit.
Documentation only; no runtime/config change, dependency installation into the
project, production-data mutation or tester run.

## Performance v2 resilient review import (2026-09-11)

Review import now accepts an older XLSX when the newest Pair+Side selection run
has the same request/config hashes and identical immutable result rows. A rank
left on a non-selectable status is cleared; an `ANALOG` whose submitted target
is no longer `FINALIST`/`RESERVE` becomes `FILTERED`. Missing, self-referential
and out-of-snapshot analog targets remain invalid. Focused review tests pass
(`28 passed`); related Panel Performance v2 tests pass (`69 passed, 2 skipped`).
The six previously rejected production files (`BMNR`, `FWDI`, `RDW`, `SNOW`,
`TSLL`, `UVXY`) were imported successfully without restarting the Panel.

## Performance v2 user-review preservation (2026-09-11)

Accepted User Status, User Rank, analog target and comment now remain
authoritative by Strategy ID across `REPLACE` result changes and later
unreviewed automatic selections. A new XLSX exports those persisted values
only; unseen strategies receive blank User fields instead of copies of Auto
Status/Rank. Existing review history in the working database was intact and
required no repair. Focused selection/review/Panel/retest verification passes
(`240 passed, 4 skipped`). The running Panel was not restarted.

## Performance v2 stale-result pruning (2026-09-11)

A new standalone maintenance command previews old Performance v2 strategies
and preserves every strategy whose result ends on/after the UTC cutoff or is a
`FINALIST` in the latest imported user review for its Pair+Side. Explicit
`--apply` takes the existing writer lock, creates a full backup under
`data/performance-v2/backups`, and removes the remaining strategies with their
result-dependent rows. If a later delete fails, the working database is
restored from that backup; selection and import audit history stays intact.

The cleanup was applied for cutoff `2026-09-06`: 11,381 strategies, 7,684,514
actions, 53,542,061 equity samples, 97,215 window metrics and 27,565 orders were
removed. The working database now contains 5,212 strategies/results: 5,186
fresh rows plus all 31 current user finalists, with overlap. The post-apply
preview reports zero removable rows; schema validation passes and selection/
review history remains present. The full 12,054,966,272-byte backup is
`data/performance-v2/backups/strategy_performance.prune-20260906-20260911T063807999929Z.duckdb`.
Focused verification passes (`3 passed`).
The adjacent store/selection suite has `62 passed` plus one pre-existing dirty-
config mismatch (`config.performance.json` currently resolves 16 workers while
its edited test expects 30).

## Native SINGLE_MODE truncated result recovery (2026-09-11)

Native batch waiting now accepts a complete stable sequence of new or changed
`my_test_run_<index>_of_<batch_size>_<strategy>.html` files when the tester's
`wizard_result.json` is truncated. A stat-only baseline is captured per batch;
the existing hashed baseline and authoritative settings/date/layout validation
remain in place. Focused Panel/native checks pass (`29` and `65` tests).

The ordinary SINGLE_MODE tester card now exposes a retry control for the latest
failed or cancelled job, keeps RETEST jobs separate, and shows elapsed report
recovery time while the synchronous retry request is pending.

Live retry job `f9b52e419fd5491aa29f315fdf0b74b0` exposed a separate collection
cost after each 1,000-strategy batch: strict validation reparsed every HTML
accumulated by prior batches, so the second transition spent about 20 minutes
in `REPORT_COLLECTION` before advancing normally. Collection now uses the
batch-local stat baseline and reads/parses only new or changed current-batch
reports; strict settings, report-period and Performance-v2 layout validation
is unchanged. The running Panel process still uses its loaded pre-fix code and
was not restarted. Focused SINGLE_MODE/runner verification passes (`55 passed`).

## Panel common worker and analysis profile settings (2026-09-10)

Static Panel General settings now saves `operational.import_workers` through
the existing settings endpoint. `duckdb_import.workers` is authoritative for
Panel DUCKDB_DIRECT, Source DB services, surfaces, and Performance v2; legacy
duplicate worker keys remain readable for compatibility but do not override
the common value. Direct materialization keeps its other tuning fields and
normalizes `max_in_flight_chunks` to the common worker count when needed.

Analysis Profile now projects only visible fresh-analysis controls. Hidden
canonical grid, isolated peak, Close MA support, and target DD values remain
unchanged in config saves. The static form uses semantic sections and the
agreed labels. Focused settings/profile/Panel/static verification passes;
Python compilation, JavaScript syntax, and `git diff --check` pass.

## Local tester preparation from Panel (2026-09-10)

The local runner action is now labelled `Prepare files` and placed between
runner/disk preflight and Start. It preserves the existing config and
single-strategy rendering, and accepts a default-off `Delete old reports before
start` option. When selected, the service first validates and stages the
request, acquires the tester lock, confirms the bot has stopped, then empties
only the validated `tester/report/my_test` contents. It rejects links and
unsupported report entries and retains the report directory itself. The Panel
reports whether old reports were cleared. Start launches the local bot, waits
the configurable 10-second default `request_timeout_seconds`, then calls
the Files-tab `POST /htmx/tester/run` endpoint and displays its status. The
per-strategy Table wizard remains excluded. If the Files request fails, the
just-started bot is stopped while preparation ownership is retained for retry.

Focused verification passes: `129 passed, 2 deselected` across the local
runner and static UI slice. Python `compileall` and `git diff --check` pass.
The two deselected static tests require unavailable `node`; they are unrelated
to this slice.

When files are already prepared, including ownership retained by another live
Panel instance, local fill returns the safe `TESTER_FILES_PREPARED` code. The
browser tells the operator to press Stop before changing the request; lock
records, PIDs, and local paths remain undisclosed.

Local Panel preparation now automatically replaces a valid same-machine lock
left by a dead owner on a previous boot. The opt-in is confined to
`LocalTestingService.fill()`; the shared tester lock remains fail-closed by
default for live, foreign, malformed and unknown owners (ADR-0035).

## Portfolio Optimizer PRETEST_PROXY Stage 1 (2026-09-10)

Implementation slice is present in the read-only finalist input, common UTC
period resolver, PRETEST_PROXY metrics, shared-cap composition sizing, and
bounded candidate search. Focused portfolio tests and Python compilation pass;
tester execution and PerformanceDB writes remain disabled. Minute refinement,
profile-status workbook output, and Panel/static UI integration are included
and covered by focused fixture tests; root self-review and current-data
verification remain pending.

The current implementation also accepts sparse current-result equity
observations with a valid prior/initial seed, carries them through the terminal
UTC boundary, and records density/gap as diagnostics. Search acceleration is in
place: the built-in evaluator uses bounded processes from the existing
machine-wide `duckdb_import.workers` setting, commits results deterministically,
and keeps full equity/action payloads in source rows only while calculations
need them.

Live profiling of a 29-finalist Campaign found that the first implementation
restored those series into final winners and then recursively froze duplicate
copies after the process pool had completed. The visible workers were idle
while the Panel consumed one core and grew past 2.5 GB in this serial output
step. The adapter now removes calculation-only equity/action series after final
sizing/refinement and before `AdapterResult` freezing; scalar output facts are
unchanged. The focused adapter/search/minute/Panel contour passes (`126 passed`).

Root verification on the current read-only PerformanceDB selected 31 finalists
over 12 pairs. With `duckdb_import.workers=25` and the configured 100,000 search
budget, the bounded search charged 3,220 unique compositions and returned three
BALANCED variants after minute refinement in 252.241 seconds. No PerformanceDB
write or tester run occurred. At most 17 worker processes were needed for the
available batches; observed aggregate Search working set peaked near 2.2 GB,
instead of the earlier serial run's roughly 18.5 GB retained payload.

Focused verification: `325 passed` for Portfolio input/config/proxy/sizing/
search/minute/adapter/Panel/static UI; the final process/mappingproxy,
SingleMode-evidence and Panel subset passed `136 passed`. Full repository suite
completed with `3708 passed, 7 skipped` and one Windows HTTP timeout; the exact
failed typed-envelope route then passed three independent reruns. Python
`compileall`, JavaScript syntax, and `git diff --check` pass.

## Performance v2 global finalist retest control (2026-09-10)

The Panel now freezes the current effective `FINALIST` set, optionally adds
`RESERVE`, generates one native `SINGLE_MODE` retest batch, and imports each
successful report through the existing `REPLACE` path without changing user
statuses or ranks. Post-retest selection runs in server-owned
`RETEST_COHORT` scope: cache population, filters, percentiles, analogs and ranks
see only the exact successfully imported Strategy/Result IDs. Failed and
excluded members remain auditable and do not reuse their old metrics in the
new ranking.

The completed run exports one atomic control XLSX for all Pair + Direction
groups. User status, local rank, RETEST and comment are editable; automatic
fields, rowsets, cohort/config provenance and failure-only groups are checked
before one transaction. Reimporting identical edited bytes is idempotent. The
new read-only preview fills the oldest cohort listing as start and UTC today
minus two days as end. Exact successful jobs replay only when scope, dates,
cohort and template/config digest all match.
The bulk card also exposes an immediate current-effective control export; its
server-owned dormant snapshot leaves effective statuses/ranks unchanged until
an explicit edited workbook import, which overlays only submitted rows.
The download is visible before any retest and follows the same `Включая
RESERVE` switch used by the bulk cohort preview.

Root focused verification:
`.venv\Scripts\python.exe -m pytest tests/test_performance_v2_finalist_retest.py tests/test_performance_v2_retest.py tests/test_performance_v2_import.py tests/test_performance_v2_selection.py tests/test_performance_v2_selection_review.py tests/test_panel_performance_v2.py tests/test_panel_performance_v2_retest.py tests/test_panel_static_ui.py tests/test_portfolio_input.py -q`
— `574 passed, 4 skipped`. The complete project suite passes `3650 passed,
7 skipped`; `node --check`, `py_compile`, and `git diff --check` pass. No real
tester or remote target was launched.

Immediate-export regression verification: `242 passed, 4 skipped` across the
finalist-retest, selection-review, Performance v2 Panel and static UI slice;
Python and JavaScript syntax checks pass.

## Portfolio Optimizer human settings form (2026-09-09)

The Settings tab now edits the existing strict-v1
`portfolio_optimizer.local.json` through a compact form for the global test
budget and the three AGGRESSIVE/BALANCED/CONSERVATIVE scenario profiles. The
full-document GET/PUT compare-and-swap contract is unchanged; hidden paths,
policy descriptors, versions and runner settings survive every save. Money,
integer and sizing-grid input is validated without binary-float conversion,
currencies are read-only, and stale-digest conflicts reload the authoritative
server document without retrying the write.

Verification: `.venv\\Scripts\\python.exe -m pytest
tests/test_panel_static_ui.py tests/test_panel_portfolio.py -q` — `170 passed`;
the executable static-UI suite includes Node checks of the settings projection
and decimal/grid rules. `node --check src/mrs3/panel_web/app.js` and
`git diff --check` pass. Independent Claude Opus 5 review returned
`CODE_REVIEW_PASS` after the reported decimal-scale, currency, validation-state
and conflict-reload findings were resolved. This UI change does not resolve the
separate real Campaign variant-generation blocker: the Panel still lacks the
enriched finalist runtime facts and accepted ranking/search policy inputs.

## Portfolio Optimizer canonical phased design (2026-09-05)

Documentation-only consolidation now lives in
[the phased specification](docs/specs/2026-09-05-portfolio-optimizer.md),
[the implementation plan](docs/superpowers/plans/2026-09-05-portfolio-optimizer.md)
and [ADR-0025, Proposed](docs/decisions/0025-portfolio-optimizer-evidence-and-phases.md).
The canonical package has no required dependency on the old working dossier;
that directory has not been deleted. Current collector Revision 2/ADR-0024
is reused instead of reviving its older design.

State: Draft D7 amendment `PLAN_APPROVED`; M0 accepted after final
independent `CODE_REVIEW_PASS` by Claude Opus 5 high in three rounds. Fixture-only
M1 config, canonical identity, source snapshot, Portfolio DB, and dispositions
are accepted after independent Opus `CODE_REVIEW_PASS` in five rounds; evidence is in
[the M1 ledger](docs/superpowers/plans/2026-09-06-portfolio-optimizer-m1-evidence.md).
Fixture-only M2 exact-FINALIST admission, liquidity history/ceilings, typed exchange
reference, injected ticker snapshots and coarse capacity screen are accepted after
independent Opus `CODE_REVIEW_PASS` in four rounds; evidence is in
[the M2 ledger](docs/superpowers/plans/2026-09-06-portfolio-optimizer-m2-evidence.md).
Fixture-only M3 margin/limiter and M4 proposal search/renderer are accepted after
independent Opus `CODE_REVIEW_PASS`; evidence is in
[the M3 ledger](docs/superpowers/plans/2026-09-06-portfolio-optimizer-m3-evidence.md)
and [the M4 ledger](docs/superpowers/plans/2026-09-06-portfolio-optimizer-m4-evidence.md).
Focused verification is `118 passed` for M3 and `66 passed` for M4. The combined
M1–M4/collector/performance verification is `545 passed, 1 warning`.
Fixture-only M5 target ownership and portfolio runner implementation is
accepted after independent Opus `CODE_REVIEW_PASS` in three rounds. Its
evidence is in
[the M5 ledger](docs/superpowers/plans/2026-09-06-portfolio-optimizer-m5-evidence.md):
focused ownership verification is `239 passed, 1 skipped`; the R12-R14 subset
is `131 passed, 1 skipped`; and the final complete project suite is `2938
passed, 7 skipped, 8 warnings`. Opus round 1 R1-R10 and round 2 R11-R14 are
fixed or explicitly adjudicated; round 3 accepted the exact final tree.
Fixture/fake-only M6 is accepted after a fresh full Opus review and final
`CODE_REVIEW_PASS`. The accepted-tree focused suite passes `147` tests. Evidence
and the complete prior/fresh review disposition are in
[the M6 ledger](docs/superpowers/plans/2026-09-07-portfolio-optimizer-m6-evidence.md).
Fixture/fake-only M7 is accepted after a fresh full Opus review and final
`CODE_REVIEW_PASS`. The accepted-tree focused suite passes `147` tests. Evidence
and the complete review disposition are in
[the M7 ledger](docs/superpowers/plans/2026-09-07-portfolio-optimizer-m7-evidence.md).
M8 deterministic fixture export and Portfolio-DB replay are accepted after a
fresh final Opus `CODE_REVIEW_PASS`; focused verification is `95 passed`. Evidence is in
[the M8 ledger](docs/superpowers/plans/2026-09-07-portfolio-optimizer-m8-evidence.md).
The real joint-test plan item remains pending separate authorization. U1 Panel,
API, Campaign lifecycle, package-owned finalist rank/cutoff, deterministic XLSX
and disabled Stage 2 are accepted after final Opus `CODE_REVIEW_PASS`; their
separate scoped commit and evidence ledger remain pending.
No real tester, bot, remote target or production database was used.
D5 records
`portfolio_optimizer_research_risk_v1`
research/calibration defaults: AGGRESSIVE DD/free-margin/MM 20%/20%/50%,
BALANCED 10%/40%/35%, CONSERVATIVE 5%/60%/20%. They are not automatic trading
admission; independent Opus D5 review returned `PLAN_APPROVED`. PnL, liquidity/freshness policies and
exact ranking remain open. D6/ [ADR-0030](docs/decisions/0030-portfolio-optimizer-m2-admission-and-sizing-contract.md)
fix exact `FINALIST` admission, seven-day liquidity distribution, current maximum
symbol-level leverage and individual-DD sizing from current portfolio equity;
the numerical individual-DD cap remains open. Capability questions Q01-Q12 gate their dependent
tasks. No tester/bot was launched and no real config, DB, archive, API, or
runtime target was used by M1–M2. Пользователь авторизовал
M0 read-only inventory в новой чистой сессии по
[M0 handoff](docs/superpowers/plans/2026-09-06-portfolio-optimizer-m0-handoff.md):
сначала capability/evidence matrix, затем принятие M0 и M1. Real tester
permissions остаются отдельным M5 gate. Прохождение research thresholds не разрешает implementation,
tester run, `RECOMMENDATION_READY`, trading admission или live use; все remaining
gates (PnL floor, liquidity/freshness limits, profile ranking) остаются open
blockers. Documentation link/consistency verification
passed: 146 local Markdown file targets resolve; the new spec/plan/ADR have
balanced code fences, no conflict markers, no required old-dossier paths and
no migration map. Tracked `git diff --check` and whitespace checks of all three
new files passed. Implementation phase gates, open policy labels and synthetic
limiter/cycle/cap/replay examples were checked locally for consistency. M0
review is now `CODE_REVIEW_PASS`; this remains documentation evidence, not
implementation authorization or runtime evidence.

Panel UI U0 documentation is accepted after independent Opus
`PLAN_APPROVED` and final `CODE_REVIEW_PASS` in two rounds:
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md)
and [ADR-0031](docs/decisions/0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md)
fix the local launch form, exact `FINALIST`/`User Rank` truncation, immutable
Campaign, persisted seven-stage job, settings CAS, success-only XLSX and the
disabled Stage 2 boundary. The documentation update touches only the UI spec,
ADR-0031, main spec/plan, PRD, progress and AGENTS; tests and runtime checks are
not applicable. U1 code is not implemented or authorized, config schema v2 is
not accepted, and Stage 2 remains blocked until M5/M6 plus explicit user
authorization. M2 remains independent and accepted; M3–M4 are accepted and M5
is the next server stage. No Panel API,
UI, tester, bot, real database or generated workbook was run for U0.

M0 read-only contract inventory is accepted after final independent Opus
`CODE_REVIEW_PASS` in three rounds. The versioned evidence is
[portfolio_optimizer_m0_capabilities_v1](docs/superpowers/plans/2026-09-06-portfolio-optimizer-m0-evidence.md).
It traces Performance v4 current-result replacement, cache-writing selection
paths, the collector schema-v2 marker/read-only boundary, runner target
mutators/locks, planned M1-M8 writers, and Q01-Q12 owners with named fail-closed
outcomes. Root fresh verification: Performance `300 passed, 1 skipped, 1
warning`; collector `55 passed`; runner `129 passed, 1 skipped`; Markdown links
`151 targets, 0 errors`; `git diff --check` passed.
The isolated DuckDB 1.5.5 probe accepted a read-only transaction and rejected
writes, while a differently configured concurrent connection was rejected;
therefore the future M1 adapter must either obtain one consistent read-only
transaction or stop without source writes. Current joint portfolio mode/report,
dual-TF, limiter/priority, sizing, opposite-order, target-wide ownership,
collateral reserve and shared-liquidity details remain capability blockers.
No tester/bot, API, real DB/archive, runtime config or target write was used.
Next safe step is fixture/fake-only M5 ownership implementation. Real tester/bot
execution requires accepted M5 and M6 plus separate explicit user authorization.

## Bybit collector current implementation status (2026-09-05)

Phases 1-6 and the minimal operations/runtime surface are implemented on `main`:
strict config, RAM-only order books, scheduler/aggregation, SQLite WAL spool,
hourly immutable Parquet, paginated reference data/raw gzip, one-connection
WebSocket protocol, runtime wiring, health/CLI, and Windows task scripts. The
focused collector suite contains 250 passing tests after self-review. No live
credentials or generated market data are committed.

Final implementation review disposition (2026-09-05): the restart-only storage
root path now persists a visible health error, prints a diagnostic, and exits 3
so the Windows task restarts it. Independent review accepted the implementation;
  self-review corrected the linear subscription topic to the supported
  `orderbook.1000` depth and updated the focused protocol tests. Low-severity
  follow-ups are deliberately deferred: half-open WS idle watchdog,
snapshot ordering during multi-batch handshake, persisted reference baseline,
reference page-count cap, and a lock around cross-thread book snapshots.

Live smoke evidence (2026-09-05, isolated `.tmp` data root): public REST returned
HTTP 200 for BTCUSDT/ETHUSDT; normalized reference output contained 4 raw gzip
pages and instruments/risk Parquet; a 95-second run reached `connected=true` and
wrote minute rows; after forced stop, a 75-second restart reopened SQLite WAL,
reached `connected=true`, continued rows, published an eligible hourly Parquet,
and `verify-archive` returned `valid=true`. Generated smoke data is not tracked.

Accelerated debug evidence (2026-09-05, isolated `.tmp` data root):
`run --test-export-minutes 5` completed a five-real-minute public REST/WebSocket
run. SQLite contains 144 minute rows (72 per BTCUSDT/ETHUSDT); one immutable
hourly Parquet marker contains 62 rows (31 per symbol); health was `OK` with
`late_rows=0`; `verify-archive` returned `valid=true`. The test flag scales only
the process clock and is not production evidence.

Post-smoke hardening (2026-09-05) marks health explicitly as
`runtime_mode=production|smoke_test` plus `accelerated_clock`. A two-minute
real-clock sanity run for BTCUSDT/ETHUSDT produced one complete minute per
symbol with `sample_count=12`, `valid_sample_count=12`, and `coverage_ratio=1.0`;
partial edge minutes were expected at start/stop. Health ended as `OK`, both
books were synchronized, `data_errors=[]`, and `verify-archive` returned
`valid=true`. The full collector suite now passes `255` tests. The isolated
production sanity data remains under `.tmp/bybit-production-sanity-20260905`.

Depth completeness follow-up (2026-09-05): ADR-0028 extends
`liquidity_1m` to schema version 2. Existing combined completeness ratios remain;
each band now also stores independent bid and ask completeness ratios, so a
one-sided orderbook limitation is visible to downstream consumers. The
reference pipeline, retention, and five-second sampling cadence are unchanged.

## Bybit market-data collector Phase 1 started (2026-09-05)

Implementation follows the approved [Bybit market-data collector Revision 2
specification](docs/specs/2026-09-05-bybit-market-data-collector.md) and its
[executable plan](docs/superpowers/plans/2026-09-05-bybit-market-data-collector.md).
The new strict TOML configuration loader validates the exact three-section
contract, resolves and checks the storage root relative to the config file, and
hashes exact UTF-8 bytes. `ConfigManager.reload()` is all-or-nothing: invalid
candidates preserve the accepted config; valid symbol/log changes report atomic
added/removed/unchanged sets; and a changed root remains on the accepted active
root while returning `restart_required`.

Evidence: `.venv\\Scripts\\python.exe -m pytest
tests/test_bybit_collector_config.py -q` — `36 passed` after the expected
pre-implementation import failure. This is Phase 1 configuration evidence only;
network validation and collector phases 4–9 remain pending.

## Bybit market-data collector Phase 3 implemented (2026-09-05)

The RAM-only minute aggregation slice now provides deterministic UTC five-second
boundary scheduling with monotonic wait calculation, forward/backward clock and
suspend reanchoring without backfill, and fixed-order `liquidity_1m` rows. It
tracks active targets, attempted/valid/connected samples, reset attribution,
nullable no-valid rows, interpolated p05/p50/p95 metrics, visible depth, and
per-band completeness according to the approved specification.

Evidence: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_bybit_collector_aggregation.py tests/test_bybit_collector_core.py
tests/test_bybit_collector_config.py` — `115 passed`; collector modules also
pass `py_compile` and `git diff --check`. SQLite spool is now implemented as
Phase 4; archive, reference data, operations, and integration phases remain
pending.

## Bybit market-data collector Phase 4 implemented (2026-09-05)

The SQLite spool persists only canonical `liquidity_1m` minute aggregates and
the `published_hours` marker index under `storage.root/spool`. WAL/NORMAL
settings, finite canonical JSON, first-winner duplicate/conflict policy,
bounded BUSY/LOCKED retries, marker idempotency/conflict rejection, half-open
hour reads, restart recovery, marker-only reader files, and the existing
`OutputDirectoryLock` are covered by focused tests. WebSocket frames, books,
and five-second samples remain RAM-only; Parquet export is deferred to Phase 5.

Evidence: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_bybit_collector_storage.py tests/test_bybit_collector_aggregation.py
tests/test_bybit_collector_core.py tests/test_bybit_collector_config.py` — `188
passed`; collector modules pass `py_compile` and `git diff --check`. The phase
delivers only the SQLite spool and `published_hours` marker index: no Parquet,
manifests, quarantine, or archive state machine is delivered here.

## Bybit market-data collector Phase 5 implemented (2026-09-05)

Hourly export snapshots committed SQLite rows for an eligible UTC hour, writes
DuckDB `COPY` Parquet with ZSTD metadata, fsyncs and structurally validates the
temporary file, publishes with a same-directory no-clobber link, and commits
`published_hours` last. Existing marked and unmarked finals are validated
self-consistently, so late SQLite rows never rewrite or invalidate immutable
archive files; only a fresh temporary file is compared with its SQLite
snapshot. Verification reads only marker-listed files. Recovery removes only
owned stale UUID scratch files, reports unlink/export errors, skips valid
marked history, and bounds unmarked-hour reconciliation to the recent
48-hour window; older unmarked files remain an operator-retention concern.

Evidence: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_bybit_collector_archive.py tests/test_bybit_collector_storage.py
tests/test_bybit_collector_aggregation.py tests/test_bybit_collector_core.py
tests/test_bybit_collector_config.py` — `209 passed`;
collector modules pass `py_compile` and
`git diff --check` remain required before integration. Phases 6–9 remain
pending.

## Performance v2 import hardening and unified panel (2026-09-05)

The normal tester and Performance DB import are one card. Import requires an
explicit inbox check for the current `SINGLE_MODE` job. The server consumes that
authorization atomically before dispatch, revalidates the metadata inbox, and
uses only the configured listing-dates path. A terminal import requires a new
check. All-rejected imports are `FAILED`, retain sources and expose the failure
report; successful imports no longer display a post-import check warning.

Evidence: importer/selection/windows/input `232 passed, 1 skipped`; panel
`187 passed, 4 skipped`; `node --check`; `git diff --check`; independent Opus
reviews returned `CODE_REVIEW_PASS` for both importer and panel scopes.

## Performance v2 SHORT import and terminal status (2026-09-05)

The normal `SINGLE_MODE` import resolves the project-configured listing dates
when the browser does not send a path. A failed all-rejected import is `FAILED`,
retains its failure report and tester sources, and never reports a false commit.
The terminal `COMMITTED` message no longer appends `CHECK REQUIRED`: that gate
exists only before import.

Production evidence: the normal SHORT batch committed 321 of 321 reports with
zero rejected entries. Database readback found 321 current strategy/results
across eight symbols; all have listing-aware effective periods and warm-up
provenance. Focused importer regressions: `20 passed`; focused panel failure
and RETEST recovery regressions: `5 passed`; static panel checks: `68 passed`.
The terminal-status change received external `CODE_REVIEW_PASS`.

## RETEST report-header retry fix (2026-09-04)

Native RETEST prevalidation now accepts the current tester action table by
required column names, including the tester's extra/reordered `Side`, `Price`,
and `Cost` columns. The main Performance v2 parser already followed this
contract; regression coverage now exercises both paths and preserves missing,
duplicate, typed, and legacy rejection.

Evidence: `117 passed, 1 skipped` in the focused RETEST/Performance v2 slice;
read-only replay accepted and parsed `186/186` captured reports for `149`
expected strategies. Commit: `54ee9b8`.

## Performance v2 shared tester-source cleanup (2026-09-04)

SINGLE_MODE inbox metadata now stores only the configured report filename;
imports resolve it under `tester_runner.report_dir` without copying HTML. The
import remains fail-closed for missing, changed, unsafe, or reparse-backed
reports. After a committed import, the exact configured report directory, the
configured tester strategy directory, and project `Output/strategies` are
emptied; failed imports leave these sources intact.
After a panel restart, the previous RETEST job remains a candidate only;
`CHECK & RETEST` must be pressed again. The check reuses a committed RETEST
inbox with a safe, structurally valid metadata manifest and current configured
report/strategy artifacts, without recapturing mutable tester state. A broken
committed inbox reports a deterministic error; it never silently starts another
native run. If the manifest is valid but its report or strategy files were
removed, the inbox is not reusable and CHECK starts a new native run.
RETEST replacement now updates the existing result row in place and replaces
only that result's action, equity, and window child rows in one transaction;
the obsolete full-table child rebuild is gone.

## Performance v2 selection review cleanup (2026-09-03)

The accumulated Performance v2 selection/review work is implemented, staged, and
ready for the scoped commit. It includes schema v3 selection snapshots, editable
XLSX review/import, REJECTED-only durable tags, period-integrity checks, typed
database/API failures, cache invalidation for result/config/window-fact changes,
and resilient multi-file review import. Trades remain completed round trips from
`strategy_actions`, not partial order openings or `strategy_results.total_trades`.

Verification: the full suite reports `2118 passed, 2 skipped, 1 warning` in
`730.19s`; the focused Performance v2/panel slice reports `248 passed in 29.96s`;
`node --check src/mrs3/panel_web/app.js` and `git diff --cached --check` pass.
The independent review found no High or blocking finding; its Medium/Low
follow-ups were addressed and verified. Next step after commit/push is manual
Excel round-trip acceptance on the real Performance v2 database.

## Current unified Performance v2 handoff (2026-08-30)

The native `SINGLE_MODE` tester handoff is the active path and is implemented
through commits `952bc22..3f535f4`. It creates a metadata-only inbox: strategy
JSON remains in the trusted `Output/strategies` root and current HTML remains
in `tester/report/my_test`; the manifest records exact paths, source hashes,
dates, commission and provenance. The v2 importer is the only authoritative
full source/identity/report/plateau validation before staging and DB commit.

After v2 `COMMITTED`, cleanup is limited to the approved exact tester/report
and `Output/strategies` roots. Inbox metadata and the v2 audit remain
provenance; cleanup failure leaves the DB committed and reports a path-safe
warning. The old Fast TEST panel dispatch/API/retry contour is removed, while
the Runs backend/API remains available with its UI hidden.

Fresh evidence: 366 passed across v2/native/panel tests, 338 passed in the v1
non-disturbance suite, `node --check src/mrs3/panel_web/app.js`, and
`git diff --check`; final Terra disposition is `CODE_REVIEW_PASS`.

Task 8 now executes native `SINGLE_MODE` batches through one `/htmx/tester/run`
POST and `/htmx/tester/status` polling cycle per attempt.  It installs each
bounded batch before startup, maps the newest complete current HTML by embedded
strategy name, retries only missing reports, and fails terminally without a
native PARTIAL commit; successful completion still creates the metadata-only
inbox.  Fresh evidence: runner/native and retained monitor suites — `58 passed`;
v2/panel regression slice — `104 passed`; `py_compile`, `node --check
src/mrs3/panel_web/app.js`, and `git diff --check` passed.

The 2026-08-28 main-branch commit audit is complete. Relevant work is now
split into `de0ee4c` (Fast TEST contour), `b58f22d` (inode-preserving strategy
publication), `edc5d40` (16-worker Performance import defaults), `4a2bab8`
(Fast TEST implementation plan), and `23575e8` (unified Performance v2
specification, ADR and vertical-slice plan). The v2 vertical slice (Tasks 1–6)
is implemented and independently reviewed; the full v2 pipeline remains
pending; aggregate verification is 81 focused v2 tests and 334 v1
non-disturbance tests plus
`node --check src/mrs3/panel_web/app.js`; no runtime or generated artifacts
were committed.

Unified Performance Analytics v2 design is approved and recorded in
`docs/specs/2026-08-28-unified-performance-analytics-v2.md` and ADR-0020. The
approved boundary is one Performance DuckDB, one current replaceable result per
strategy, shared order-to-plateau facts, arbitrary flat-boundary UPNL-relative
A/B windows, an ordered filter/Pareto pipeline, panel/XLSX/Portfolio Optimizer
outputs, transactional discard/add/replace and a durable `RETEST` tag whose
handler runs the common RUNS-to-inbox-to-replacement chain. The reviewed
vertical-slice code and schema are now committed. Next step is the deferred full
v2 pipeline; the later RUNS redesign must reuse the same metadata-only,
trusted-path and importer contract.

Performance DB import now defaults to 16 preparation processes and caps the
request at 16; DuckDB publication remains a single transactional writer.
Focused verification: `.venv\\Scripts\\python.exe -m pytest
  tests/test_performance_import.py -q` —
`48 passed`.

## Unified Performance Analytics v2 vertical slice (2026-08-28)

Tasks 1–6 of the approved v2 vertical-slice plan are implemented and
independently reviewed. Accepted commits are `3686af7` (Task 1), `ead1ded`
(Task 2), `5475dae` (Task 3), `f69737a` (Task 4), `a154015` (Task 5), and
`91381ce` (Task 6); each received Terra Medium `CODE_REVIEW_PASS`.

Focused v2 verification:
`.venv\\Scripts\\python.exe -m pytest -q
tests/test_performance_v2_store.py tests/test_performance_v2_input.py
tests/test_performance_v2_html.py tests/test_performance_v2_import.py
tests/test_performance_v2_windows.py tests/test_panel_performance_v2.py` —
`81 passed in 17.05s`.

V1 non-disturbance verification:
`.venv\\Scripts\\python.exe -m pytest -q tests/test_performance.py
tests/test_performance_store.py tests/test_performance_import.py
  tests/test_performance_metrics.py tests/runner/test_inbox.py tests/test_panel.py
tests/test_panel_static_ui.py tests/test_integration_contract.py` —
`334 passed in 139.69s`.

`node --check src/mrs3/panel_web/app.js` and `git diff --check` passed. The
vertical slice is implemented and verified; the full v2 pipeline is pending.
Source metrics are not MRS3 strategy, tick-test, DD5, or portfolio results.
Next increment: implement the explicitly deferred v2 scope under its approved
contracts, without deleting v1 runtime/storage or widening result claims.

Fast TEST now writes the HTML profile into the tester config's nested
`report` object as well as legacy top-level keys. Balance/equity series remain
enabled and position statistics are disabled for the minimal import profile.
Focused verification: `tests/test_panel_fast_strategy_test.py` — `11 passed`.

READY generation no longer blocks when the runtime algorithm-config hash
differs from the historical analysis hash. The analysis hash is retained as
lineage metadata; generated JSON uses the supplied runtime config and keeps
the existing strategy validation. Focused verification:
`.venv\\Scripts\\python.exe -m pytest tests/test_analysis_strategies.py
tests/test_fresh_analysis_strategies.py tests/test_panel_fresh_strategies.py -q`
— `75 passed`.

## Retired Fast Strategy Test design (2026-08-27)

The independent **Fast TEST стратегии** contour is approved and documented in
`docs/specs/2026-08-27-panel-fast-strategy-test.md`. Its implementation plan is
`docs/superpowers/plans/2026-08-27-panel-fast-strategy-test.md`.

This section is historical, not an active panel feature. Native `SINGLE_MODE`
is now active; Fast start/retry dispatch, API handlers and panel service
ownership were removed in `3f535f4`. The bounded implementation remains only
as shared runner machinery required by the Single mode service.

The new path will retain bounded `strategy_batch_size` chunks, the existing
low-level `max_parallel_submissions` rolling window and four total automatic
attempts. It will not call the old `runner.workflow.run_batch`, create verified
inboxes or write `data/import_audit`. A partial run continues later chunks and
leaves exactly failed strategy JSON in `<bot_root>\settings_strategy`; one
recovery action first accepts matching manual reports, then grants one extra
attempt to each remaining failure.

Historical implementation details follow. Task 1 persisted per-order plateau diagnostics
in the generation manifest; Task 2 supports partial controlled monitoring and
HTML settings extraction; Task 3 provides the independent bounded Fast TEST
service; Task 4 wires `strategies.tester.fast.start/retry` through the panel;
Task 5 adds the two Fast TEST controls and reload recovery. Focused verification
currently passes 154 tests; the dedicated runner suite passes 118 tests plus
one platform skip. The independent review returned `CODE_REVIEW_PASS` after
the period, recovery and UI fixes. A real disposable-tester smoke is still
pending.

The READY publisher now preserves the existing `Output\\strategies` directory
instead of replacing it, so its ACL survives regeneration; staged files are
installed with rollback on failure. Focused verification after this fix:
`.venv\\Scripts\\python.exe -m pytest tests/test_pipeline.py
tests/test_analysis_strategies.py tests/test_fresh_analysis_strategies.py -q`
— `69 passed`.

Fast TEST now removes a source HTML only after a stable snapshot is captured
and its size/mtime signature is rechecked. The legacy `run_batch` path keeps
its previous report-preservation behavior. Focused runner verification:
`.venv\\Scripts\\python.exe -m pytest tests/runner/test_monitor.py tests/test_panel_fast_strategy_test.py tests/runner/test_workflow.py tests/runner/test_results.py -q`
— `76 passed`.

The quadratic Fast TEST post-run deduplication pass was removed. Stable
`verified_reports` snapshot filenames are now authoritative, so completed batches
do not reread every HTML for every strategy. The interrupted 3917-report run
retains all 3917 manifest-referenced files; its stale runtime state is not
treated as a committed job. Fast-to-Performance-DB integration remains a
separate follow-up contract.

Reload recovery now prefers a Fast job with persisted `verified_reports` over an
older committed tester inbox. The Tester card and `Проверить` action therefore
use the reports currently present in `tester/report/my_test` instead of reviving
the historical 196-report status. Focused static/Fast verification: `72 passed`.

The Performance DB `Проверить` button now reports immediate verification state
and surfaces missing-job/API errors instead of returning silently. The current
Fast inbox can therefore be observed while its verified snapshot is captured.

## READY JSON validation recovery (2026-08-26)

READY generation now validates Phase 2 filters before registering its running
job, so a malformed request cannot leave the next generation permanently busy.
The fresh-generation endpoint returns a path-safe validation reason and the
panel displays it instead of collapsing it to `Server validation failed.`.
Thread-start failures follow the same path and clear the pending job before
returning the safe reason.

Evidence: fresh-strategy and static-panel tests `66 passed`; `node --check`
and `git diff --check` clean apart from existing Windows line-ending warnings.

The live failure also had a local filesystem cause: `Output\\strategies` had
inheritance disabled and denied the configured panel account. Re-enabling
inheritance restored the existing parent ACL; a live NVDL shortlist generation
then committed `4/4` strategies successfully.

## Performance DB display rounding (2026-08-26)

Performance import admission now tolerates tester display drift using inclusive
nearest-unit intervals: absolute `Total PnL` and `Max Drawdown` use one unit,
while their percentage fields use 0.1 percentage point. Precise series-derived
values stored in the database are unchanged. The two previously quarantined
PANW reports pass the updated validation in isolation. Focused verification:
  `.venv\\Scripts\\python.exe -m pytest tests/test_performance_metrics.py
  tests/test_performance_import.py -q` -- `59
passed`. A full inbox re-import remains pending because its referenced
`Output\\strategies` JSON files are currently absent.

## Tester run files (2026-08-26)

## Verified snapshot republish (2026-08-26)

An ordinary tester batch may republish a completed report from only its own
`.<batch_id>.report_snapshots` directory. This preserves the strict inbox path
boundary while allowing the worker to finish `COMMITTED` and unlock Performance
DB import after verified snapshot capture. Focused verification:
`.venv\\Scripts\\python.exe -m pytest tests/test_panel_strategy_batch.py
tests/test_panel_fresh_strategies.py tests/test_panel_jobs.py
tests/test_panel_static_ui.py -q` — `81 passed`.
On panel reload, an existing committed tester job now revalidates its persisted
inbox and restores `inbox_ready`, so the Performance DB action is not lost.

## READY JSON failure diagnostics (2026-08-26)

The panel preserves an actionable, path-safe cause when asynchronous READY
JSON generation fails. Permission failures identify publication access, missing
files identify the template/artifact boundary, and validation failures retain
their contract message. Focused verification: `14 passed` in
`tests/test_panel_fresh_strategies.py`.

## Tester snapshot completion recovery (2026-08-26)

If the tester returns an empty `chartUrl`, final reconciliation uses a report
name only when the controlled batch has already captured a fresh,
embedded-name-verified snapshot for that exact strategy. A reused tester
`runId` no longer suppresses such fresh evidence. Otherwise the existing
strict result validation remains in force. Focused verification:
`.venv\\Scripts\\python.exe -m pytest tests/runner/test_monitor.py
tests/runner/test_results.py tests/runner/test_workflow.py -q` — `63 passed`.

The Shortlist button creates isolated tester snapshots for every selected
server-recomputed `READY_AFTER_FILTERS` candidate. It clears only the exact
`<bot_root>/tester/runs` target, sets `use_runs=true`, and copies Tester batch
dates plus `max_parallel_submissions`; `run_tester.bat` remains manual.
The bot-exported run template may have a UTF-8 BOM and trailing commas; those
are accepted only while reading that template.
Generated snapshots serialize the template's default MakerFee as decimal
`0.00001`, rather than scientific notation.

Focused verification: `.venv\\Scripts\\python.exe -m pytest
tests/test_tester_run_files.py tests/test_panel_fresh_strategies.py -q`.

The Tester batch card now owns both generation actions and their status.
Ordinary READY batches and isolated RUNS share one panel job resource. RUNS
requires generated snapshots, clears only `tester/report/my_test_runs`, starts
`run_tester.bat` non-interactively, and reports generated HTML count every 15
seconds. Snapshots use `name_comment=runs`, keeping their reports out of the
ordinary READY report directory. After every expected RUNS report is present,
the panel captures a verified inbox from immutable snapshot settings and report
hashes; Performance DB accepts that inbox exactly as it accepts an ordinary
batch. The common `delete_html` cleanup removes the original mode-specific
reports only after a zero-quarantine committed import. Focused verification:
`87 passed` across run files, inbox, panel and Performance DB tests; independent
review is still required before integration.

Reload no longer restores a terminal tester job into the live Tester batch
status or progress bar; only an active job is reattached.

## Multi-order plateau admission (2026-08-25)

For `real_independent_events`, 2ORD--4ORD construction now admits a plateau
only when its frozen point-count and monthly event-count meet configurable
`multi_order_admission` limits (defaults: 3 and 20). The check is before
combinations, preserves legacy-proxy behaviour, and fails closed for missing
real-event diagnostics on `ready=true` plateaus. It changes the algorithm
configuration hash and therefore requires a new analysis; source/surface
materialization and Phase 2 remain unchanged.

Focused verification:
`.venv\\Scripts\\python.exe -m pytest tests/test_config.py tests/test_selection.py tests/test_source_v6_analysis_fresh.py -q` -- `239 passed`.

## Filtered Shortlist bucket counts (2026-08-25)

Phase 2 previously updated only READY/DEFERRED while the `1ORD`–`4ORD` table
counts remained unfiltered. With active filters, those bucket counts now show
only remaining READY candidates; `DEFERRED` and `ALL` stay as the full
context. Accordion status badges are right-aligned. Focused fresh-analysis and
panel tests, JS syntax and diff checks pass.

## Shortlist control alignment (2026-08-25)

The secondary Shortlist information caption is hidden. The Phase 2 heading now
matches Shortlist typography; Pair/Side and TF use the same 16px checkbox and
a vertically centred disclosure arrow. Filter logic and table columns remain
unchanged. Focused panel tests, JS syntax and diff checks pass.

### Phase 2 live refresh (2026-08-25)

The Phase 2 checkboxes now call the same filtered Shortlist refresh used by the
Refresh action. Previously their state reached the server only after a manual
Refresh click, so visible counts did not change immediately. The Pair/Side
triangle is reduced to `0.9rem`; no data contract changes. Focused panel tests,
JS syntax and diff checks pass.

## Shortlist filter presentation (2026-08-25)

Phase 2 filters are permanently visible with a bold heading and spaced labels;
their native summary remains visible as the title but cannot collapse. TF
selection checkboxes are indented beneath the Pair/Side row. This is
presentation-only: filters, counts and all current columns are unchanged.
Focused panel tests, JS syntax and diff checks pass.

## M7 final-balance PnL validation (2026-08-25)

The `my_test_APLD_TSEM_fixed_0.997net` import quarantined 1,009 reports:
1,007 `Total PnL` mismatches and 2 Recovery Factor mismatches. The tester's
`Final balance - Initial balance` matches its declared `Total PnL`, but its
last `walletSeries` point can differ in either direction. M7 now validates PnL
against declared final balance when present, falling back to the final wallet
sample only for sparse reports without that declaration; M4 materialized
wallet-series metrics are unchanged. ADR-0018 records the boundary.

Focused verification: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_source_v6_m7.py tests/test_source_v6.py` — `88 passed`.

## M7 Recovery Factor admission boundary (2026-08-26)

`Recovery Factor` is informational and no longer quarantines a Source v6
report: the tester's internal denominator precision is not contained in the
HTML payload. M7 continues to reject mismatched `Total PnL`, `Total fees` and
`Profit Factor`.

Targeted recovery imported only the affected reports and produced clean
two-symbol merge kits under `data/databases/repair-kits-2026-08-26/`:
NVDL/TSLL, LLY/TQQQ, BABA/SNOW and APLD/TSEM each contain 10,944 fragments,
zero quarantines and no other symbols.

Focused verification: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_source_v6_m7.py tests/test_source_v6_bundle.py` - `19 passed`;
each resulting DB passed `validate_source_v6_database`.

## Panel Web reliable batch recovery (2026-08-25)

The panel now keeps READY JSON generation failed when publication raises an
error, rejects restart while that generation is active, and restores a tester
batch after restart only after complete inbox validation, including the direct
strategy JSON file, raw hash and canonical version ID. Performance import
applies the same strategy provenance validation before both parsing and
known-evidence skips. The operator patch merge no longer bypasses unresolved
quarantine replacement checks.

Focused verification: panel suite `112 passed`; provenance/merge checks `10
passed`. Independent re-review: `CODE_REVIEW_PASS`.

## READY JSON template-only payload (2026-08-26)

`Generate READY JSON` now emits only the selected tester-template fields and
computed strategy settings. Per-strategy provenance is no longer copied into
the bot payload; the immutable strategy manifest retains the analysis lineage,
candidate mapping and exact JSON hashes used by batch validation. The generator
does not read or modify `config_tester.json`.

Focused verification: fresh-generation, manifest validation and v6 generation
tests — `63 passed`.

## BASE 1ORD selection (2026-08-24)

Implemented algorithm `0.7-canonical-phase1-base-1ord-v3`. Fresh CXMT corpus
evidence (`workers=30/30/8`) produced 26 BASE candidates: 15m `[8,12,12,4]`,
5m `[15,23,12,7]`, 1h `[3,10,13,8]`, 30m `[5,31,7,3]`, 45m `[5,16,20,14]`,
4h `[6,4,3]`, 2h `[27,3]`, 3h `[4]`; only 5m size 7 was appended as
`FALLBACK_1`. Full verification: `1736 passed, 2 skipped, 2 warnings`.
This is selection evidence, not tick-test or realized MRS3 PnL evidence.

## Panel Web trust/design alignment (2026-08-24)

Merged into `main` at `2a40518` after independent `CODE_REVIEW_PASS`.
Panel verification: **341 passed**; full project verification: **1668 passed,
2 skipped, 2 warnings**. The untracked BASE/1ORD specification and panel plans
remain in the working tree; a pre-merge archive is stored at
`backups/main-dirty-before-panel-merge-2026-08-24-102131.zip`.

The remote Source DB card now accepts a relative report-folder selector under
the configured archive root (`c6b7d8b`); absolute and traversal selectors are
rejected server-side. Independent review: `CODE_REVIEW_PASS`. Panel
verification after this fix: **345 passed**.

## Source v6 facts/metrics v2 Stage 3–4 (2026-08-23)

M7 validates declarations during normalization before encoding. PnL uses the
declared Final balance minus declared initial balance when Final balance is
available, falling back to the final wallet sample only for sparse reports;
fees sum all actions, Profit Factor sums realising actions, and Recovery Factor
is conditional on a sampled DD-compatible declaration. Quarantine details are
read-only and a quarantined database blocks every scope at panel preflight and
materialization.

Fresh baseline evidence: 684/684 reports committed, 0 quarantined, source
content digest `9612e26abc51b6e36f49ce3a73159358abdcd1a412747a235c70ffaa82fec940`.
The three-run import median is **68.712 s**, database size **24,653,824 bytes**,
and all three semantic signatures match the clean v2 database. Against the
recorded v1 medians (111.533 s, 30,683,136 bytes), this is faster and smaller.

Clean materialization with workers 1 and 4 produced the same analysis digest
`358ec8ba55a2888fe9b12ba38c82c915ecd5767b447a0a53a267e5bd1a55496c`, 684
compact rows, and zero empty results. Full workers=30 materialize + SQL-copy
publication ran three times at a **10.640 s** median, 684 rows/facts and
**23,867,392 bytes** (v1 median 71.160 s and 27,537,408 bytes). The full suite
is green: `.venv\\Scripts\\python.exe -m pytest -q` — **1629 passed, 2 skipped**.

The final Reviewer call still returned findings; the panel validator seam and
orphan-roundtrip traceability were fixed and focused/full verification rerun.
No fourth Reviewer call is permitted, so this remains a non-approval artifact
until a future task with a fresh review budget explicitly reopens review.

## Post-review metric corrections (2026-08-23)

An independent implementation review found that a leading realization could
remain in `total_pnl` while disappearing from round-trip counts and win rate.
The metric contract now emits one orphan round trip with empty
`entry_action_ids`, preserving the M1 allowance for a position opened before a
visible window; entry-only tails remain excluded. The review also found a
quadratic peak scan. Cycle peaks are now computed once from the ordered action
stream and reused by each round trip.

Focused Source v6 verification after the correction: **89 passed, 1 warning**.
A synthetic derive benchmark scaled approximately linearly: 185/370/740/1480
actions took 8.8/9.3/19.5/37.6 ms on this host.
Fresh full verification: `.venv\\Scripts\\python.exe -m pytest -q` — **1629
passed, 2 skipped, 2 warnings**.
**Current feature:** Source v6 high-throughput import and merge — implemented,
measured on both real corpora; the merge readback is now
parallel (C9): 2,080 s to 544 s on the two-corpus merge, identical artifact.

## Source v6 import throughput (2026-08-21)

Contract: [publication throughput spec](docs/specs/2026-08-21-source-v6-publication-throughput.md)
and [high-throughput import plan](docs/superpowers/plans/2026-08-21-source-v6-high-throughput-import.md).

Measured on Debian `46.4.84.220`, `/opt/hb1/debian-duckdb-importer`, corpus
`data/html/1` (5,859 reports), 32 cores, `workers=30`:
**886 s to 299 s (2.96x)**, published digest unchanged
(`c85fdb8372b2cce51d2a0e4aff537eb951e781eff5b4704a74d63c7163611b90`).

Defects found and fixed, each measured rather than assumed:

1. Leaf scheduling capped in-flight chunks at `segment_writer_limit`, so 30
   workers ran 4-wide (12.5% of 32 cores). The writer limit now bounds only
   segment writes, via a pool semaphore.
2. Merges decoded and re-encoded sealed payloads at every tree level; segment
   reads decoded every row to compare two stored columns.
3. Reduce built a fan-in tree, writing 6.8 GB of intermediates for 1.5 GB of
   leaves. It now `ATTACH`es segments and copies rows in one SQL pass.
4. Publication issued a per-fragment duplicate probe (quadratic), one statement
   per calendar day, and a per-fragment decode readback. All are now set-based.
5. `decode_fragment` rebuilt the canonical document to re-derive the identity —
   51.8% of all decode time. The decompressed bytes are that document, so the
   id is `sha256(raw)`.
6. The tail decoded all 5,859 fragments to serve consumers that read metadata
   only. Metadata now comes from indexed columns; payloads are decoded only for
   surface publication or a point with a real overlap to persist.

7. Metadata publication bound one row per call. DuckDB `executemany` was
   measured at 1,730 rows/s against 1,969,809 rows/s for the same rows inserted
   through a registered frame — **1138.9x**. One corpus emits ~1.2M
   `day_ownership` rows, so this was the unexplained "last stage" cost on both
   the import and the merge path. Both now go through `_insert_frame`.
8. The merge ran its identity readback inside the copy transaction. Committed,
   one 128-id window costs **0.239 s** against the committed 59,675-fragment
   input database; with the rows still open in the copy transaction the merge
   of both corpora did not finish in 40 minutes (`py-spy` parked it in
   `_verify_published_identity`). These are two artifacts, not an A/B on one.
   The mechanism is not settled — `EXPLAIN` gives the same `SEQ_SCAN` plan
   either way, so it is not index availability; transaction-local storage of
   the merge's 4.7 GB of payload is the likeliest cause and is recorded as
   unproven. The readback
   now runs after the commit and before publication — on the `.staging` file,
   which `merge_source_v6` publishes only by `compacted.replace(target)`, so
   nothing committed ever becomes reachable unverified.

9. The merge's identity readback ran serially and was the single largest phase
   of it, not a tail: **1,215 s of the 2,080 s merge**, measured on the
   published 5.6 GB artifact at 2.460 s per 128-id window over 494 windows.
   Within a window the SQL fetch is only **3.9%** (1.91 s against 47.56 s of
   Python over 20 windows); the Python side is 64.3%
   `_assert_canonical_matches_columns`, 20.3% `zlib` and 15.4% `sha256`. It is
   therefore CPU-bound, per-fragment and shares no state. `merge_source_v6` now
   takes `workers` and calls `verify_published_identity_parallel`. A/B on the
   same 8,192 ids: **134.2 s serial against 19.0 s at 16 workers, 7.07x**;
   it plateaus there (8 → 5.74x, 24 → 6.82x). The full 63,131-fragment
   verification runs in **113.9 s**, of which 59.3 s is now the single-statement
   column check — that is the next lever, and it belongs to DuckDB's own
   parallelism rather than to a fan-out.

   Re-run end to end with `workers=16`, the two-corpus merge took **543.7 s
   against 2,080.1 s** — about 9 minutes where it was about 35. Roughly 1,100 s
   of that difference is the verification saving; the rest is uncontrolled,
   because the serial run was the first read of those inputs and this one was
   not, so the page cache differs. The subset A/B is the isolated measurement,
   and it does not reconcile cleanly with the full-corpus figure — see C9,
   where the discrepancy is recorded open rather than explained away.
   Equivalence was checked rather than assumed: every published table digested
   inside DuckDB and compared, all identical, with `schema_info` differing only
   in the per-merge `database_id`.

Verified equivalent: full-database dump comparison across all published tables
including `mutation_generation` and `import_audit`; surface publication fails
closed on metadata-only fragments.

Verification for all of the above, including C9:
`.venv\Scripts\python.exe -m pytest -q` — **1341 passed, 2 skipped** through
defect 8, **1346 passed, 2 skipped** with defect 9, **1347 passed, 2 skipped**
with C10 (ADR-0015), and **1363 passed, 2 skipped** with ADR-0016.

## Merge of the two real corpora (2026-08-22)

`merge_source_v6` over `data/databases/1_3/` — 5,859 + 59,675 = **65,534 input
fragments, 6.1 GB** — completed in **2,080 s** and published 5,643,710,464
bytes with `source_content_digest`
`a26c00b965680ab50afb72874bd89cb087441b1ca3433d2d1551b6cd4cc4c814`. Read back
from the artifact: 63,131 `compact_fragments` (2,403 inputs were cross-corpus
duplicates), 5,041,855,558 bytes of payload, 1,205,395 `day_ownership`, 65,534
`fragment_origins` (one per *input* fragment — lineage keeps the duplicates
publication drops), 63,131 `points`, 63,131 `import_audit`. The same
merge previously could not complete at all: it was aborted twice, once parked
on the `day_ownership` bind (defect 7) and once on the in-transaction readback
(defect 8).

Merge order does not matter and cannot be chosen: `merge_source_v6` forbids the
target from existing or being an input, so there is no base to merge *into*.
The duplicate winner is the smallest `(source_sha256, source_name)`, publication
order is `(point_key, report_start_ms, fragment_id)`, copy order is by sorted
input path, and origins are re-sorted before the rewrite — so the artifact is a
function of the input set alone.

## Debian corpus `my_test_CX_GE_fixed` (2026-08-21)

Imported on `46.4.84.220` with `workers=30`: 38,305 HTML reports (36 GB) in
~15 min — **38,160 COMMITTED, 145 QUARANTINED** (144 × non-empty `walletSeries`
required, 1 × exactly one complete settings JSON object required).
`safe_to_delete=NO`, so the raw HTML must not be deleted.

All 145 were identified and inspected: `quarantine` stores no file name, so the
`source_sha256` values were exported and matched against a parallel sha256sum of
all 38,305 server-side HTML files — all 145 matched, confirming `source_sha256`
is the sha256 of the raw report. They are 102 × `CXMTUSDT_5m`, 36 ×
`CXMTUSDT_15m`, 6 × `CXMTUSDT_4h` (run ids 30577–38298) plus one stray optimizer
summary page, `report_optimizer_my_test_auto_x_auto_y_20260820_232555.html`,
which is not a run report at all. An earlier note here called all of these
source defects. That was wrong for the 144: `my_test_run_30577_of_38304_
CXMTUSDT_5m_2026-07-29.html` is a complete 1,183,513-byte report that emits
`const walletSeries = [];` and `const equitySeries = [];` because no trade
occurred in that shift window. They are valid zero-activity runs the importer
rejects — see Next. Published:
`compact_fragments` 38,160, `points` 38,160, `day_ownership` 766,702,
`import_audit` 38,305, max `generation_after` 38,160, 3,215,208,448 bytes.
Downloaded to `data/databases/`; byte size matches. The server-side original is
untouched. This run predates defects 7 and 8 above, so re-importing on the
fixed runner should be materially faster.

## Zero-activity runs are imported (2026-08-22)

Contract: [zero-activity spec](docs/specs/2026-08-22-source-v6-zero-activity-runs.md),
[ADR-0016](docs/decisions/0016-source-v6-zero-activity-runs.md).

The 144 `walletSeries`-empty quarantines of `my_test_CX_GE_fixed` were complete
reports of runs in which no trade occurred, not defects. They are now admitted,
but only on affirmative evidence: `Total Trades` and `Total transactions
(buy/sell)` present and zero, corroborating metrics consistent where present,
and the seven undefined ratios as the literal `n/a`. Absence of data is never
accepted as evidence of emptiness, because a truncated report has none either.
Opt-in per caller; only `normalize_source_v6` opts in, so ADR-0006's DD5
candidate contract is untouched.

Four defects were found by review across two rounds, each reproduced against
the repository's own fixtures before being fixed. A zero-activity outgoing fragment triggered
ADR-0013 seam exclusion and deleted the incoming fragment's only cycle while
reporting the batch `COMMITTED` — seam exclusion de-duplicates, and an empty
fragment has nothing to de-duplicate. And for an identical window the empty
fragment took ownership by `fragment_id` sort order, flagging the fragment with
four real actions as `AMBIGUOUS_INCOMING`. Round two found the mirror image of
the first — an empty *incoming* deletes the outgoing's open tail, one cycle and
two actions, also under `COMMITTED` — now `BRIDGE_NOT_COVERED`/`PARTIAL` as
ADR-0010 already specified; and that the new tie-break had desynchronised
`resolve_batch` from `persist_batch_resolution`, which re-derives the outgoing
side by bisecting its own ordering. A further one was found by the tests
themselves: a report with actions but empty series was admitted, which is not a
run where nothing happened but one whose samples did not render.

The 145th quarantine, an optimizer summary page, still fails. Re-importing the
CX_GE corpus is required to gain the 144 points; nothing is migrated.

## Surface publication throughput (2026-08-22)

Contract: [surface throughput spec](docs/specs/2026-08-22-source-v6-surface-throughput.md).

Measured on `my_test_CX_GE_fixed`, scope `CXMTUSDT|LONG|15m`, 648 fragments,
43 MB of payload: metadata + readiness 4.9 s, hydration 49.8 s,
`materialize_source_v6` 0.1 s, publication **100.4 s**, resident memory 42 MB to
2,450 MB. That is 239 ms per fragment; the whole 38,160-fragment corpus
extrapolates to ~2.5 h and ~74 GB resident, so it could not be published at all.

The preflight needs nothing: `preflight_source_v6` returns in 0.00 s and reads
no HTML, and `canonical_ready_intervals` costs 1.7 s over 38,160 fragments —
both already work from metadata. `folder1` correctly reports 0 READY scopes
because its widest grid is 12 of the required 114 point variants; `CX_GE`
reports 55 of 56.

Publication carried three defects already fixed elsewhere: it re-encoded the
sealed payload (59 ms each, and the result is byte-identical to what is stored —
120/120 on payload, codec and `payload_sha256`), inserted one statement per row
(736 rows/s), and ended by decoding every fragment again to check ids it could
derive directly (48.1 s of the 100.4 s). Payloads are now copied by SQL from the
source database, rows are written through `_insert_frame`, and publication
validates the C3a identity instead of reconstructing objects.

**Publication 100.4 s to 3.0 s (33x)**, same `surface_id`, and the two artifacts
compared directly: manifest, scope manifests, factual rows and the payload bytes
of all 648 fragments identical. The file is 43% smaller as a side effect of the
set-based insert.

Hydration is now the dominant cost and is untouched: `materialize_source_v6`
still rejects metadata views, so the caller decodes every fragment although
nothing on the publication path needs a decoded object.

The pass-through is opt-in and **the panel does not pass it yet**. Its single
production call site, `panel.py:1837`, was being edited by another session, so
switching it was left out rather than conflict. Until that one argument is
added the application still takes the 100.4 s path.

## Empty result combinations (2026-08-22)

Contract: [empty result spec](docs/specs/2026-08-22-source-v6-empty-result-combinations.md).

A "point" is a parameter combination — shift, open MA and close MA over one
symbol, side and timeframe — and since ADR-0016 one of them can be tested and
produce no trades. `calculate_metrics` raises for a combination with no samples,
and every consumer called it in a bare loop, so one such combination aborted the
whole build. Demonstrated: ten healthy combinations published, the same ten plus
one idle one raised and all eleven were lost.

Such a combination now keeps its cell in the canonical grid and carries the flat
result the tester itself declared — PnL 0, drawdown 0, no trades, every ratio
`None` under ADR-0006 — and is recorded under `empty_result_points`.

An earlier revision of this change excluded the cell instead, and that was wrong.
Exclusion published a 113-of-114 grid, which `load_source_v6_pipeline_input`
rejects with `INCOMPLETE_GRID` one stage later, naming neither the reason nor the
cell — a loud publish-time failure turned into a quiet artifact that dies later.
The objection to keeping it (that `build_persisted_analysis_facts` defaults a
missing metric row to 0% return at 0% drawdown, an outstanding risk-adjusted
result that never happened) does not apply: `annotate_eligibility` runs before
plateau geometry and rejects the cell with `REJECT_PNL_NONPOSITIVE` and
`REJECT_DD_NONPOSITIVE`. Verified — `plateau_id: None`, `role: UNASSIGNED`. It is
visible and unselectable, which is what a tested-and-idle combination should be.

A window that hides a *measurable* combination is still an error and raises,
naming the combination; that is a different fact and must not be flattened into a
zero.

The multiscope path needed the same rule one stage earlier: it stores facts, not
metrics, so it published happily and `run_multiscope_analysis` aborted
afterwards. `materialize_source_v6` now measures each scope over that scope's
READY witness — the same window the analysis measures over.

Coverage is not lost: the tested days remain in the source database as
`ACTIVE_EMPTY` under Z4.

## Next

1. Re-import `my_test_CX_GE_fixed` to pick up the 144 zero-activity runs and
   confirm `safe_to_delete` is no longer held at `NO` by them. The surface
   blocker that stood here is fixed — see "Empty result combinations" above.
2. Pass `source_database` at `panel.py:1837`, the only production caller of
   `publish_multiscope_surface`. One argument; it is what makes the 33x
   reachable from the application.
3. Let `materialize_source_v6` work from metadata. Readiness already does, and
   after the pass-through above nothing on the publication path needs a decoded
   fragment — so hydration is pure waste, and it is what makes a full-corpus
   surface need ~74 GB resident.
4. Close the same published-file gap on the import path. ADR-0015 scoped
   itself to the merge deliberately: `_publish_segments_single_pass` verifies
   its reduce target and then publishes a repack that receives neither the C3a
   payload readback nor `fragment_metadata`'s header pass, so the import
   publishes with weaker evidence than the merge now does — under the same
   `safe_to_delete=YES`. It needs its own change, not an assumption from
   ADR-0015's wording.
5. Remove the orphaned segment-merge path (`merge_source_v6_segments`,
   `_merge_segment_contents`, `_read_source_v6_segment`, `import_fragment`,
   `import_fragment_batch`) in its own `refactor:` commit — C5 left them
   without a production caller, and ~50 tests still reference them.
6. Open question from C8: the identity readback is a sequential scan per
   128-id window on both paths, so O(n²/128) in principle. It does not bite on
   import (5.52 s committed against 5.47 s with the metadata-only transaction
   open, at 5,000 fragments). A relation cursor is not the fix — `fetchmany`
   grew resident memory by 2.9 GB on a 4.3 GB corpus. A bounded-memory single
   pass is its own change.
7. The parse phase is now the dominant remaining cost. Compression level was
   measured and rejected. Swapping the raw-markup cross-check to the lexbor
   engine was measured at 3.9x on that step and then **reverted**: lexbor is an
   HTML5 tree builder and performs the same implicit-close recovery as lxml, so
   the two parsers stopped being independent. The cross-check must stay a
   tokenizer; no faster second parser has been found that preserves it.

## Prior feature: Source v6 fresh compact multi-scope

STAGE_1_GATE=ACCEPTED_BY_ROOT; date=2026-08-20; reviewer=CODE_REVIEW_PASS compact-publication and gate-checker final re-reviews; evidence=.codex/stage1-acceptance-ledger.md,.codex/task5-real-corpus-report.md,.codex/task6-merge-evidence-report.md,.codex/task6-recovery-overlap-report.md,.codex/task6-debian-recovery-report.md

## Verified implementation

- Fresh compact Source → multi-scope surface → separate analysis pipeline is complete.
- Panel supports multiple READY scopes; the analysis worker limit is
  `duckdb_import.workers` and `gap_rules` is part of analysis identity and
  selection.
- Independent review: `CODE_REVIEW_PASS`.
- Latest full local verification: `1206 passed, 2 skipped, 1 warning` via
  `.venv\Scripts\python.exe -m pytest -q`.

## Next: manual verification

1. In the panel, import the intended raw HTML set and select one or more READY
   `symbol|side|timeframe` scopes.
2. Confirm a new `.surface-v6.duckdb` appears under
   `Output/surfaces-v6-compact/`, then run analysis with the intended listing
   dates and configuration.
3. Confirm the `.analysis-v6.duckdb` appears under
   `Output/analysis-v6-compact/`; repeat after changing `gap_rules` and verify
   that it produces a distinct analysis artifact and expected structure result.
4. Before syncing Git, review the scoped diffs/commits; local `Input/`,
   `Output/` and `Data/` must remain untracked.

## Parallel panel work (2026-08-22)

Static Control Panel v1 is implemented and independently reviewed
`CODE_REVIEW_PASS`. It replaces the root shell, keeps `/legacy`, and covers
local testing, guarded remote profile operations, Source DB import/merge,
READY-only immutable surfaces, fresh analysis → local tester → Performance DB
and `CALCULATION_ONLY` DD5, plus local settings. Job terminal snapshots and
tester inbox lineage survive controller restart; interrupted remote importers
are rehydrated solely for a safe stop attempt. Portfolio remains disabled.

Latest verification: `1523 passed, 2 skipped, 1 warning` via
`.venv\Scripts\python.exe -m pytest -q`; focused panel suite: `68 passed`.
Contract and visual evidence: `docs/specs/2026-08-22-panel-static-frontend-v1.md`.

## Selected-scope surface materialization (2026-08-22)

The panel now keeps preflight metadata-only and hydrates only payload fragments
belonging to the explicitly selected READY scopes.  Hydration is deterministic
and parallel within the existing bounded worker limit; the whole-source lineage
digest still comes from validated Source DB metadata.  `materialize_source_v6`
remains hydrated and witness-based, because E1 requires `calculate_metrics` to
distinguish an actually idle point from data hidden by the selected window.

Evidence: `39 passed in 7.92s` over selected serial/parallel storage readers,
empty-result E1--E5, materializer, surfaces service and static panel tests;
independent review `CODE_REVIEW_PASS`.  The heavier throughput suite was not
claimed as evidence: its terminal invocation exceeded the local tool limit and
only its own child processes were stopped.  Source backup before this work:
`backups/surface-contract-before-metadata-materialization-2026-08-22.zip`.

Surface output now defaults to an editable `{pair...}_{start}_{end}` filename;
the immutable `surface_id` remains only in its manifest.  The Strategies/DD5
Analysis DB field derives an editable filename below saved `analysis_db_root`
and that full target is passed to the analysis runner.  Repeated explicit names
fail closed; automatically generated conflicting analysis names receive a
readable numeric suffix.  Evidence: `33 passed in 8.99s`, `node --check
src/mrs3/panel_web/app.js`, `git diff --check`; independent review
`CODE_REVIEW_PASS`.

The Strategies/DD5 selector now reloads every manifest-validated published
surface recursively from configured `source_v6_surface_dir`, falling back to
`data/surfaces`; full payload validation remains immediately before analysis,
not on panel bootstrap. `surface_target_path` is an approved, persisted panel
default, so the publication-card save button writes it to `config.local.json`.
Evidence: `49 passed in 15.64s`, JS syntax check, diff check, live local
catalog: one surface in `2.4s`; independent review `CODE_REVIEW_PASS`.

Fresh analysis now immediately reports its real entry phase, an indeterminate
bar and elapsed time; it does not invent a percent because the synchronous
analysis contract exposes none. The control is restored on every terminal
result. Evidence: `47 passed in 16.86s`, JS syntax check, diff check,
independent review `CODE_REVIEW_PASS`.

## Approved: Source v6 facts and metrics v2 (2026-08-23)

Contract and implementation plan are approved for a fresh-only rebuild:
[metric contract](docs/specs/2026-08-23-source-v6-metric-contract.md),
[ADR-0017](docs/decisions/0017-source-v6-facts-and-metrics-v2.md), and
[minimal rebuild plan](docs/superpowers/plans/2026-08-23-source-v6-minimal-rebuild.md).

### Stage 0 v1 same-host baseline (2026-08-23)

Source: 684 HTML reports from the retained 2026-07-15--2026-07-22 archive;
30 workers; all temporary databases and surfaces were written outside Git.

- Import, three runs: median `111.533 s`; spread `106.608..125.762 s`;
  Source DB median `30,683,136` bytes, spread `30,683,136..31,207,424`
  bytes; `684` accepted and `0` quarantined in every run. Identical source
  digest `935fb9c8270ce43a2510d08b4f2f0e1853aca7efb6a9a88d185ee49fb81551aa`
  and semantic signature
  `75c2ebe67dfb7de7281943c053478e736f83cd7ead025cebe0bbd731435e9dba`.
- ID-only selected materialization plus SQL-copy publication, three runs for
  READY `AAOIUSDT|LONG|15m`: median `71.160 s`; spread
  `67.527..72.910 s`; surface `27,537,408` bytes and the same surface id
  `454f083c1a13e987bb5ee00f030c5e02233f3c292e3f5f279a759a226221f3b8`
  in every run. Observed coordinator peak RSS
  median `1,588,150,272` bytes, spread `1,583,542,272..1,591,230,464`;
  worker peak RSS median `1,083,064,320` bytes, spread
  `1,012,334,592..1,083,469,824`. Scope digest:
  `f235abfd91ec20560757f188d284455b2a5dbec4d49df358d757a027d4401d1f`.

**Next step:** begin the atomic facts-only v2 boundary with TDD. No v2
performance or correctness claim is valid until a post-M7 fresh import has
zero quarantines and Stage 4 evidence.

### Stage 1 facts-only payload v2 (2026-08-23)

Implemented in the working tree and accepted by the mandatory independent
reviewer bridge (`CODE_REVIEW_PASS`, Claude Sonnet 5, medium effort). The
canonical fragment now persists factual actions only; cycles, events and
open-tail state are reconstructed deterministically by the typed decode path.
v1 payloads, source databases and resume tokens are rejected or invalidated by
the v2 schema/fingerprint boundary. The W6 identity readback validates raw
factual payloads without reconstructing derived facts, while full materializer
decode checks all derived header/cache counts, including in-range tampering.

Evidence: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_source_v6_stage1_v2.py tests/test_source_v6.py` — 85 passed;
focused Stage 1/merge suite — 258 passed; `git diff --check` clean apart from
existing Windows line-ending warnings. Stage 2 is the next implementation
slice.

### Stage 2 metrics and compact analysis row (2026-08-23)

Implemented and committed as `9e92897`. The metric pass now derives
deterministic round trips and weighted exposure from seam-owned actions,
computes raw-anchor PnL before retaining the publication rebase, uses all
realising actions for Profit Factor, and combines merged-series and admissible
declared drawdown with an auditable source/tie set. The same pass emits strict
v2 Decimal-string analysis rows, including weighted trades and drawdown audit
fields; hydrated and worker paths carry the same rows and digest without
payload decoding in analysis.

Evidence: Stage 2 contract/worker/empty/source suite — `105 passed, 1 warning`;
consumer/storage/fresh-analysis suite — `142 passed, 1 warning`; final
official implementation review bridge — `CODE_REVIEW_PASS`; `git diff --check`
clean apart from existing Windows line-ending warnings. Stage 3 M7 validation
is the next implementation slice.

Pre-Stage-1 repository verification: `1597 passed, 2 skipped, 2 warnings` via
`.venv\Scripts\python.exe -m pytest -q`. The warnings are the existing tar
deprecation and unavailable Windows pytest cache; two symlink tests skip on
this host.

### Materializer and remote panel boundary (2026-08-23)

The static panel's Source v6 surface path is DB-native: selected metadata is
measured by `materialize_source_v6_from_database`, publication copies sealed
payloads into a `.surface-v6.duckdb`, and fresh analysis consumes its compact
`point_analysis_input` rows. The panel's initial surface validation now uses
`decode=False`, so the analysis button does not decode factual payloads before
the row-based analysis path.

`data/databases/my_test_CX_GE_fixed.source-v6.duckdb` was repaired through an
exact replacement merge for the quarantined
`my_test_run_15426_of_38304_REGNUSDT_45m_2026-07-29.html`. The canonical DB is
schema 6 / `source-v6-fresh-compact-v2`, contains 38,304 fragments and zero
quarantine rows; `validate_source_v6_database` and panel surface preflight
return clean (`56` rows, `7` groups). The pre-repair DB remains recoverable as
`my_test_CX_GE_fixed.pre-repair-quarantine.source-v6.duckdb`. Optimizer HTML
remains excluded during Source v6 preflight.

The configured Debian remote paths were checked read-only with the same SSH
script used by the panel: all five directories exist and the disk probe
returned a numeric value. The static panel's remote path check was returning
404 because its POST route was missing from the HTTP allowlist; the route and a
regression test are now present. `scripts/restart_new_panel.bat` also stops the
old 8766 listener before starting the canonical static panel, preventing stale
duplicate processes. A BAT launched from the normal desktop has ordinary
Windows network access; Codex's restricted shell cannot grant that access to a
child process, so live SSH checks must be run outside that sandbox.

The repaired CX/GE replacement was reproduced from the remote HTML: 158
actions produce `gross_profit=398.1348` and `gross_loss=-0.0640`, hence the
legitimate `Profit Factor=6220.85625`. M7 accepts absolute Profit Factor drift
up to `0.01` while still rejecting material mutations. Merge now fails closed
on unresolved quarantine instead of silently dropping it.

The static local merge card now persists all three paths, offers configured
Source DB candidates through native datalist controls, and renders determinate
fragment progress with current status polling.

Evidence: Source v6 M7 tests `12 passed`; the panel/materializer/remote focused
suite is green after the change. The Debian runner needs the same M7 module
deployed before rebuilding that corpus.

### Restored legacy remote Source DB panel scenario (2026-08-24)

Commits `c1ea7e5`, `c09f58f` and `7712199` restore the previous remote import
workflow: the panel again exposes editable runner HTML, remote staging DB and
local target fields, saves those paths, and starts imports with the legacy
`remote_html_path` / `remote_db_target` / `local_target_path` payload. Saved
paths round-trip through `/api/v2/settings/save` and bootstrap; changing the
HTML folder refreshes derived targets. Remote paths remain validated against
configured roots, while the local output target keeps the prior operator-
selected behavior and is used by verified delivery.

### Self-contained tester inbox strategies (2026-08-26)

The failed `PANW_PLTR` Performance DB import was traced to its committed
manifest pointing at the inaccessible/deleted `Output\\strategies` files.
The exact 196 strategy hashes were still present in the tester's
`settings_strategy` directory. The repaired inbox now stages those bytes under
its own `strategies` directory, and the import completed with `196/196`
imported and zero quarantine.

`capture_verified_inbox` now stages every ordinary tester strategy in the
immutable inbox at capture time, matching the existing v6 inbox contract and
making the flow independent of `Output\\strategies` lifetime or batch size.
Focused inbox evidence: `15 passed`.

Evidence: all panel tests (`test_panel*.py`) — `345 passed, 1 warning`;
settings/remote/static focused slice — `96 passed, 1 warning`; mandatory
reviewer — `CODE_REVIEW_PASS`; `py_compile`, `node --check` and `git diff
--check` clean apart from the existing Windows pytest-cache warning.

### READY JSON request diagnostics (2026-08-26)

The static panel has one listener on `127.0.0.1:8766`; the restart script
replaced it with the current process before this check. For
`/api/v2/strategies/fresh/generate`, malformed early HTTP requests now report
their actual cause (`Content-Type`, malformed `Content-Length`, or invalid
body size) rather than the misleading generic `invalid settings`. This closes
the last generic-error path before strategy generation begins.

Evidence: focused fresh-strategy/static-panel suite — `67 passed`; live
post-restart probe returned `415 Content-Type must be application/json` as
expected; independent review — `CODE_REVIEW_PASS`; `git diff --check` clean.

### Unified Performance DB v2 import (2026-08-31)

### Performance v2 single-strategy A/B analysis (2026-08-31)

Phase 2 is implemented: the panel now lists ACTIVE strategies from the v2
database, pre-fills UTC bounds, and calculates two safe UPNL-relative windows
for one authoritative current result. Valid unavailable windows return a
normal result; strict invalid input is typed `400`; unavailable/stale strategy
is typed `404`; cache conflict and writer lock are typed `409`. The only
database mutation is the existing versioned `window_metrics` cache.

Verification: focused v2 selector `84 passed in 13.55s`; panel/integration
`154 passed`; v1 non-disturbance `109 passed`; `node --check` and
`git diff --check` passed. Live smoke on the imported DB left
strategies/results/actions/equity at `1633/1633/741189/9305765`, added one
cache row, and measured 354 ms first calculation / 70 ms cache hit.
Independent Opus review: `CODE_REVIEW_PASS`.

### Performance v2 finalist-selection Stage 2 executable export (2026-08-31)

The selection panel additionally has an explicit pre-XLSX counter refresh. It
returns the current ordered stage snapshot as `eliminated` and `remaining` for
the chosen Pair + Side, and displays it immediately before the stage move
buttons. Counters are invalidated by any Pair, Side, checkbox, scope or order
edit; an in-flight stale response is discarded. The preview is read-only: it
does not generate a workbook or mutate Performance DB v2.

Evidence: focused selection/panel/static suite `130 passed`; `node --check`,
`git diff --check`; independent Opus re-review `CODE_REVIEW_PASS`.

XLSX is now fail-closed on default-window cache readiness: Pair + Side changes
show whether recalculation is required and keep XLSX disabled until every ACTIVE
current result has its full, A and B facts. The same check rejects direct XLSX
requests with typed `SELECTION_CACHE_INCOMPLETE` (`409`), so the browser button
cannot be bypassed. Evidence: focused suite `133 passed`; independent Opus
review `CODE_REVIEW_PASS`.

The preview is now executable. On `Смотреть результаты в xls`, the Panel sends
the current Pair + Side, checkbox state, stage order and per-stage scope to the
local v2 endpoint. It reads only ACTIVE current Performance DB v2 rows,
derives DD5_PROXY calculation facts, holding p95, ordered plateau point counts
and default A/B facts, then applies the 14 closed built-in filters/Pareto stages
strictly in submitted order. `pair_side_timeframe` splits comparisons by
timeframe; missing facts never eliminate or dominate; unavailable A/B records
remain finalists with `AB_NOT_EVALUATED_INSUFFICIENT_DATA`.

The stage registry now also includes disabled-by-default `filter_min_shift`.
Its compact inline field accepts a positive percentage (default `0.3`); when
enabled it eliminates a strategy if any existing order has a smaller shift.
Missing order-shift facts do not eliminate. The threshold participates in the
same local order, automatic counters and XLSX stage boolean as other filters;
it needs no fact recalculation. The Balanced Pareto label now correctly names
its primary metric as `PnL DD5/30`, rather than `PnL30`.

Disabled-by-default `pareto_window_b` compares survivors within Pair + Side +
timeframe: it maximizes B PnL/30d and B trades/30d, and minimizes B drawdown
and B holding p95. The latter uses completed positions closed in the final B
window; missing B facts never dominate or eliminate.

The adjacent disabled-by-default `pareto_window_b_dd_shift` maximizes B
PnL/30d and first shift while minimizing full-period drawdown, within the same
Pair + Side + timeframe scope.

Focused evidence after this change: `142 passed` for the selection, panel and
static-UI suites; `node --check src/mrs3/panel_web/app.js`; `git diff --check`.

The endpoint returns one in-memory attachment with `All candidates` and
`Finalists` sheets. Both retain exported A/B change, A/30d and B/30d fields;
all other A/B support fields remain internal. The
workbook retains every requested Pair + Side candidate, ordered plateau facts,
per-stage booleans, finalist flag and elimination reason. The run writes no
`selection_*` tables, tags, lifecycle state, discard, RETEST or history; those
remain Stage 3.

Focused acceptance evidence: `126 passed` for
`tests/test_performance_v2_selection.py`,
`tests/test_panel_performance_v2.py` and `tests/test_panel_static_ui.py`, plus
`node --check src/mrs3/panel_web/app.js` and `git diff --check`. The HTTP test
proves a `POST /api/v2/strategies/performance-v2/selection` returns an XLSX
attachment and leaves no selection tables. Independent Opus review:
`CODE_REVIEW_PASS`. Full suite evidence: `2023 passed, 2 skipped` plus seven
local-testing cases that initially lacked the ignored local `Input/` templates
in this worktree; the same seven passed after a local ignored junction exposed
the existing templates (`7 passed`).

### Performance v2 finalist-selection Stage 2 design and UI preview (2026-08-31)

The Panel now has a preview-only `6. Парето и фильтры` card: Pair + Side,
editable stage order, enable checkboxes and a per-stage Pair + Side or Pair +
Side + timeframe scope. Holding p95, A/B deterioration, Balanced Pareto and
the robust stages are enabled by default; Trades, Minimum Shift and both
plateau-points Pareto stages are disabled. Near-tie ranking and the former
global grouping block are hidden. No preview interaction calculates or
changes the database.

The selected strategy details for manual A/B analysis now show Close MA and
ordered Open MA/shift/multiplier/lot parameters; normalized values display at
two decimal places, trade rate is per 30 days, and holding duration is minutes.
The v2 catalog serves this metadata. Focused evidence before this documentation
update: `87 passed` for `tests/test_panel_static_ui.py` and
`tests/test_panel_performance_v2.py`, `node --check src/mrs3/panel_web/app.js`,
and a live BABAUSDT LONG catalog probe after panel restart.

The accepted next implementation contract is
`docs/specs/2026-08-31-performance-v2-finalist-selection.md`; its executable
plan is `docs/superpowers/plans/2026-08-31-performance-v2-finalist-selection.md`.
Stage 2 will calculate all visible built-in stages from current Performance DB
v2 facts and download an XLSX without persisted selection runs, tags, discard
or RETEST. Those lifecycle items are explicitly Stage 3.

Manual A/B output now keeps one four-column comparison table (metric, window A,
window B, change), semantic delta colours, and period shortcuts.  Response
serialization additionally derives a 30-day equivalent from each window's own
effective timestamps without changing the stored window cache: return and
growth factor are duration-normalized, trade rate is shown per 30 days, while
drawdown, fees, PF and similar metrics remain explicitly raw.  Windows shorter
than one day or with invalid duration show a status instead of a misleading
normalized value.  Evidence: `97 passed` focused, `154 passed` panel and
integration regression, `node --check`, `git diff --check`; reviewer:
`CODE_REVIEW_PASS`.

Native SINGLE_MODE now creates a metadata inbox that refers to the tested HTML
and `Output\\strategies` files without copying either directory. Import stages
and hash-verifies HTML once, parses it with the configured worker count, and
uses DuckDB dataframe appends for action/equity batches. It no longer performs
eager A/B-window calculations during import. The panel progress track receives
the live parse count and reaches 100% on commit.

Live evidence: `b26ac4db5f0b49c5a2fc17bd4561e4bd` committed `1633/1633`
reports with zero rejected/skipped. The target contains 1,633 strategies,
4,517 orders, 741,189 actions and 9,305,765 equity samples; audit is
`COMMITTED`. The completed import cleared the tester report directory and
`Output\\strategies` as requested. Focused tests: `143 passed` for the v2/panel
slice and `53 passed` for static-panel UI after the progress-track check.

Surface publication now has one server-owned output root: `panel.path_defaults.surface_target_path`.
Catalog listing and both publish entry points ignore request-supplied target paths;
the UI no longer exposes the stale `D:\\MRS3\\surfaces` default or a save-path button.
The configured project path is `D:\\SHARE\\!MN\\hamster\\MRS-Analizer\\data\\surfaces`.
Focused evidence: `216 passed`; full suite: `2050 passed, 1 failed, 2 skipped`;
the remaining failure is the pre-existing worker-default mismatch in
`config.performance.json` (30) versus its test expectation (16).

The READY JSON endpoint now accepts up to 1 MiB because a checked READY
selection can legitimately exceed the generic 64 KiB API limit. The live
70 KiB probe reached candidate validation, proving it no longer fails on body
size; all other endpoints retain the 64 KiB limit. Focused evidence: `68
passed`; independent review — `CODE_REVIEW_PASS`.

### Performance v2 persisted finalist snapshots (2026-09-02)

[ADR-0021](docs/decisions/0021-performance-v2-persisted-selection-snapshots.md)
records the accepted deferred Stage 3 architecture: an explicit save creates an
immutable Pair + Side selection snapshot, later used by the A/B `Только
финалисты` catalogue filter. Stage 2 remains stateless; the currently visible
checkbox stays disabled until this persistence contract is implemented.

The next accepted Stage 2 extension is specified in
`docs/specs/2026-09-01-performance-v2-robust-finalist-ranking.md`; its
executable plan is
`docs/superpowers/plans/2026-09-01-performance-v2-robust-finalist-ranking.md`.
It adds best-trade dependency and four-window consistency filters, a robust
Pareto, a 10%-tolerant preference for larger first Shift, and a fixed final
38/17/15/10/10/10 Top-50 ranker for Robust PnL, worst DD, A/B stability, Shift
1, minimum Points and Close MA. Existing Performance DB v2 tables are sufficient;
selection persistence, tags and XLSX import remain deferred to Stage 3.

Local implementation now adds the four robust movable stages, fixed final
Top-N ranker, seven-window cache requirement, rank diagnostics in XLSX and
panel controls. Focused verification passed: `155 passed` in finalist/static
panel suites and `167 passed` in panel/surfaces suites, plus
`node --check src/mrs3/panel_web/app.js` and staged `git diff --check`.
Follow-up independent review returned `CODE_REVIEW_PASS` before commit.

### Performance v2 selection review lifecycle design (2026-09-02)

The next approved Stage 3 contract is
`docs/specs/2026-09-02-performance-v2-selection-review-import.md`; its
implementation plan is
`docs/superpowers/plans/2026-09-02-performance-v2-selection-review-import.md`
and the storage decision is
`docs/decisions/0022-performance-v2-selection-review-ledger.md`.

The design revises final ranking to `30/15/15/12/10/9/9` for Robust PnL,
Worst DD, A/B stability, Worst Hold p95, Shift 1, PointsMin and Close MA. It
collapses surviving exact Pair + Side + TF + ORD + Close MA analog groups to
one weighted representative before a default Top-20 ceiling. The current
Close-MA near-tie Pareto remains available but becomes disabled by default.

Every downloaded selection becomes an immutable schema-v3 snapshot in the same
Performance DB. XLSX review keeps automatic and user status/rank separately;
successful imports append history and maintain only the current `REJECTED` tag.
`REJECTED` includes the previously discussed “мусор” meaning and never deletes
data. Strategy deletion, RETEST and generic tags remain outside this contract.

Implementation is complete. Performance DB v2 auto-migrates once from internal
schema 2 to 3 in one transaction; it preserves existing facts and adds immutable
selection runs/results, immutable XLSX review ledger and the scoped durable
`REJECTED` tag. The current local DB migration preserved 16,272 strategies,
16,272 results, 10,432,397 actions, 77,787,295 equity samples and 113,425
window-metric rows; it finished in 0.743 s. Reopening schema 3 completed in
0.089 s and does not require fact/cache recalculation. The existing tester
import contract remains unchanged.

Selection export now persists the exact calculated run and produces editable
review columns plus a very-hidden metadata sheet. The panel imports every XLSX
in a selected folder as an independent atomic request, validates the workbook
against the newest saved run and current facts, appends review history and
updates only `REJECTED` tags scoped to that run. A/B Pair filtering offers
`Только финалисты` after a saved selection exists and uses the latest effective
review result. The revised 30/15/15/12/10/9/9 score, exact analog groups and
Top-20 default are active.

Analog grouping additionally treats Close MA values differing by one as the
same group only when Pair, Side, timeframe, order count and the exact
`(analysis_run_id, plateau_id)` of every order match. It is deliberately
non-transitive: `5/6` may group, but `5/6/7` is split into `5/6` and `7`.
Focused regression evidence: `174 passed` across selection, selection-review
and panel/static-UI suites, plus `node --check` and `git diff --check`.

Selection `Trades` now uses the full-window completed-round-trip count from
`WindowMetrics.trade_count`, not the tester's execution-level `TotalTrades`.
For the diagnosed SNOWUSDT examples it reports ID 961 as 49 and ID 1110 as 51;
their former report-level values were 183 and 166. The focused selection,
selection-review and panel/static-UI evidence remains `174 passed`.

`Positive quarters` now exports `positive/available` rather than hiding an
incomplete early report fragment. For example, RDWUSDT ID 9428 has three
usable quarter windows and now exports `3/3`; an unavailable first window is
not counted as negative. The consistency filter continues to evaluate the
numeric positive count and does not reject missing data by itself. Focused
selection, selection-review and panel/static-UI evidence: `174 passed`.

Fresh verification: `214 passed` across Performance v2 store/import/selection,
selection-review and panel/static-UI suites; the full repository suite passed
with `2097 passed, 2 skipped, 1 warning` in 737.79 s. The skips are Windows
symlink permission cases and the warning is the existing Python 3.14 tar
extraction deprecation. `node --check src/mrs3/panel_web/app.js`, Python
byte-compilation and `git diff --check` passed. Acceptance still requires one
operator round-trip through Microsoft Excel and an independent code review; the
external review bridge was unavailable and a fallback reviewer exhausted its
usage quota, so no review disposition is claimed. Next step: perform the Excel
round-trip, obtain review, then commit the scoped change.

Independent Opus plan review initially found lifecycle edge cases around scoped
tag removal, repeated/stale workbooks, manual analog transitions, prior
rejections and manual Top-N overrides. The contract and plan now define and test
those cases; the final disposition is `PLAN_APPROVED`.

### Performance v2 calendar-window normalization (2026-09-02)

30-day metrics now normalize over the requested calendar interval intersected
with the report range, never the shorter first/last event span. The panel
displays both ranges. XLSX exposes the actual normalizers as blue centered
`Дней A` and `Дней B` columns next to the respective A/B PnL/30 columns;
their widths are based on data values. The raw cache remains valid and does not
need recalculation. For ID 8859 the B interval is 14 calendar days while its
event span is 2026-08-13T10:13Z through 2026-08-15T18:43Z; the corrected B
values are 8.3511%/30d and 23.5714 trades/30d rather than an event-span
extrapolation. Focused regression suites: `77 passed` for panel/window/HTML
and `145 passed` for selection/import/static UI/audit export.
The current HTML importer accepts a timestamp exactly at the tester's declared
inclusive report end and rejects only timestamps later than that endpoint;
optional declared transaction/final-balance checks remain skipped when an older
current report omits those fields.

The re-review of calendar normalization, import integrity and A/B XLSX
duration columns returned `CODE_REVIEW_PASS` after bounds were threaded through
selection as well. No cache migration or recalculation is needed.

### Performance v2 selection comparable windows (2026-09-03)

Task 3a of the active RETEST workflow is independently reviewed
(`PLAN_APPROVED`, then `CODE_REVIEW_PASS`). `filter_low_trades` now uses
`Trades/30`; raw `total_pnl_pct` remains an audit field but is absent from the
selection XLSX. Time consistency uses four equal calendar windows at 28 days
or more, three at 21--28 days, and otherwise `UNAVAILABLE` without excluding
the strategy. `NO_TRADES` is excluded from the assessed denominator; unsafe
windows are `UNAVAILABLE`. Cache metrics version is `performance-window-v2.2`, so v2.1
rows are not reused. Raw DD and the `PnL/30 * 5 / raw DD` proxy are unchanged.

Evidence: focused selection suite `78 passed`; selection plus review suite
`102 passed`; related suites `146 passed`; full `.venv` suite `2165 passed,
2 skipped, 1 warning` in 725.00 s; `git diff --check` passed. The skips are
Windows symlink-permission cases and the warning is the existing Python 3.14
  tar-extraction deprecation. The legacy posttest/DD5 module and its dedicated
  CLI/panel routes were removed after call-site tracing; the v2 Performance
  workflow remains the supported path.

### Performance v2 RETEST manifest and mixed-run input (2026-09-03)

Task 4 of the active RETEST workflow is implemented and independently reviewed
(`CODE_REVIEW_PASS`). `build_retest_manifest` renders only ACTIVE RETEST
strategies with current results, rechecks typed identity and plateau facts,
publishes the existing strategy output paths with staged hash binding, and
records `strategy_analysis_run_ids` per JSON file. The input boundary uses the
per-entry run for strategies, orders and plateau facts; v6 provenance is
authoritative and legacy manifests retain the common-run fallback. Duplicate,
malformed and incomplete identities/maps fail closed; failed publication keeps
the prior batch recoverable.

Evidence: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_retest.py
tests/test_performance_v2_input.py tests/test_performance_v2_store.py -q` —
`93 passed` in 9.60 s; related manifest/batch suites — `28 passed` in 2.62 s;
`git diff --check` passed. No real DuckDB or generated output was modified.

### Performance v2 CHECK & RETEST implementation (2026-09-03)

Tasks 5-6 and the legacy-removal portion of Task 8 are implemented. The import
contract applies listing-date plus five-day warm-up per strategy, excludes whole
crossing lifecycles, recomputes retained evidence, keeps raw DD, publishes valid
siblings while retaining RETEST for invalid ones, and exposes a safe CSV/XLSX
failure report link. The panel provides separate SINGLE_MODE CHECK and
server-built IMPORT & REPLACE actions with persisted recovery, atomic duplicate
reservation and path-safe artifact streaming. `posttest` and the obsolete DD5
runtime are no longer live paths.

Evidence: focused RETEST/import/selection/panel suites `386 passed, 1 skipped`
(Windows symlink capability); `node --check src/mrs3/panel_web/app.js`; clean
`git diff --check`; external Opus review `CODE_REVIEW_PASS` after three rounds.
At that point Task 7 production DB backup/migration/HIGH+REVIEW seed remained
pending; it was executed later under explicit authorization as recorded below.

### Performance v2 Task 7 production migration and audit seed (2026-09-04)

With explicit user authorization, the existing local Performance DB was checked
under `PerformanceV2WriterLock`. The target was already schema v4, so
`initialize_performance_v2` performed idempotent v4 validation without a forced
rewrite. The HIGH and REVIEW sheets of
`Output/performance-v2-period-integrity-audit-2026-09-02.xlsx` contained 49 and
100 rows respectively: exact `Strategy ID` headers, 149 integer values, zero
duplicates and zero IDs missing from `strategies`. `mark_retest_from_audit`
seeded exactly 149 unique `RETEST` IDs with source
`PERIOD_INTEGRITY_AUDIT`.

Before mutation an adjacent ignored backup was created at
`data/performance-v2/strategy_performance.duckdb.task7-backup-20260904T045944Z`;
it is 7,603,499,008 bytes with SHA-256
`1748395f353476018efeb77b88a7c6755a4071f2ce47d5a229917282ebb2eb9c`, and its
read-only DuckDB catalog/schema probe passed (v4, 15 tables, 4 sequences, 6
indexes, 16,272 strategies). The post-mutation target hash differs as expected
because the RETEST seed changed the database; the backup is the pre-seed
snapshot. A second lock-protected initialize/seed pass kept schema/catalog,
exact RETEST set, REJECTED rows and all facts unchanged, with zero net new seed
rows. The lock released cleanly and immediate reacquisition succeeded.

Facts before and after the idempotence pass: strategies/results `16,272 / 16,272`,
orders `41,280`, actions `10,432,397`, equity `77,787,295`, window metrics
`113,426`, analysis plateaus `1,468`. The backup, DB and lock are covered by
`.gitignore` rule `[Dd]ata/`; no generated artifact or source file was committed.
The original pre-copy source hash was not persisted, so byte-for-byte fidelity
is evidenced by the backup size/hash and independent read-only catalog/count
probe rather than a retroactive source-hash comparison.

## Performance v2 RETEST recovery and import retry fix (2026-09-04)

Panel reload recovery now selects the newest committed RETEST inbox before
active or failed jobs, so a stale FAILED tester cannot keep `IMPORT & REPLACE`
disabled. A real failed Performance v2 import (`PERFORMANCE_V2_IMPORT_FAILED`)
or cancelled import can be retried; committed, running, interrupted and
unknown-error jobs remain duplicate-protected. The recovery selector is a
browser-served, Node-tested helper, and its static route is covered.

Evidence: RETEST/panel/job suites `91 passed, 1 skipped`; both JavaScript files
pass `node --check`; `git diff --check` passes. External Opus review completed
three rounds; final local fixes addressed its remaining recovery and static
delivery findings, with no fourth review requested because the review budget
was exhausted.

## Performance v2 panel listing-date root fix (2026-09-04)

Panel imports now resolve the configured relative `listing_dates_path` from the
server project root, with a safe inbox-parent compatibility fallback. Client
payloads cannot provide the trusted root. Focused checks pass (`7 passed,
2 skipped` for Windows symlink capability); full suite baseline was `2238
passed, 3 skipped`. The scoped fix is committed.

## Panel fresh-analysis configuration clarity (2026-09-10)

Fresh Source v6 analysis now reports a missing configured listing-date workbook
with a safe actionable message instead of the generic `invalid settings`.
`panel_workflow.listing_dates_path` is the only runtime source for that input;
the settings save payload no longer mirrors it into UI-only path defaults.
The examples use `input/dates.xlsx`, with a tracked `input/README.md` and no
tracked workbook. A target directory is accepted as a directory, preventing a
panel default such as `data/Analysis` from being treated as an invalid filename.
Focused tests pass (`5 passed`); live Panel API verification committed analysis
`e01da6ce43ed4d1e7e0be1da550147d4cdae2ed2cca4b46fae629970e3780c7c` from the
published local Source v6 surface.

## Panel analysis profile (2026-09-10)

Settings now provides one local-only **Analysis profile** card for the values
that affect future fresh Source v6 analysis: point eligibility, economics,
surface geometry, plateau/Close MA, READY admission and order construction.
It deliberately excludes listing dates, algorithm version, batch size and
materialization-only controls. Reload is read-only; Save validates the merged
full configuration and atomically writes only `config.local.json`. The shared
worker count remains visible with its cross-workflow speed impact stated.

Focused verification: `.venv\\Scripts\\python.exe -m pytest
tests/test_analysis_profile.py tests/test_panel_analysis_profile.py
tests/test_panel_static_ui.py::test_analysis_profile_is_one_flat_card_with_explicit_controls -q`
passes (`8 passed`); `compileall` and `git diff --check` pass. The full static
UI suite reaches `94 passed`; its two remaining failures require an unavailable
local `node` executable and exercise unrelated Portfolio helpers. Independent
review completed with `CODE_REVIEW_PASS` after fixes for UI/API key alignment
and invalid numeric form values.

## Performance v2 typed-config dedup contract (2026-09-04)

The active import contract now treats executable settings, not strategy names
or analysis lineage, as the deduplication identity. The key includes Close MA,
Open MA, shift and quantized `lot_x`; this preserves intentional EQUAL/INCOME
variants. Effective coverage uses the configured listing date plus the existing
five-day warm-up, symmetrically for new and legacy-null result provenance.
Only a proper interval superset may replace a canonical result; equal, narrow
or shifted intervals are skipped. The one-time production audit is dry-run by
default and soft-discard is allowed only behind an explicit apply operation.

Read-only baseline and post-implementation audit are recorded in the ignored
artifact
`data/performance-v2/typed-config-dedup-audit-20260904T064327Z.json`:
7,904 apparent groups when `lot_x` is omitted (intentional EQUAL/INCOME
pairs), zero exact full-key groups, zero unresolved effective intervals;
16,272 strategies/results and 41,280 orders remain active, with 149 RETEST
tags. All 16,272 active typed keys are computable with zero order-count
mismatches. The importer and RETEST regression suites pass (92 tests), and the
broader Performance v2 regression set passes (216 tests, 1 Windows symlink
capability skip); no
database mutation or duplicate cleanup was warranted.

## Performance v2 lot-variant redundancy filter (2026-09-04)

The selection pipeline now has a default-on `filter_lot_variant_redundancy`
stage. It runs first, is fixed to `pair_side_timeframe`, and groups only
identical executable settings with the same known effective comparison
interval; order lots are intentionally excluded from the canonical key. The
representative is chosen by `dd5_proxy` descending, `capital_proxy` ascending,
`robust_pnl_30d_pct` descending, `worst_drawdown_pct` ascending,
`profit_factor` descending, then `strategy_id` ascending. Missing or malformed
intervals/metrics fail closed. Losers remain in All candidates as
`FILTERED / LOT_VARIANT_REDUNDANT` with representative and group-key audit
fields, and never reach later filters, Pareto or Top-N. Import and database
rows are unchanged.

The panel exposes the checked-by-default fixed-first stage and the persisted
`lot_variant_redundancy_enabled` setting. The active specification is
`docs/specs/2026-09-04-performance-v2-lot-variant-filter.md`.

Evidence: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py
tests/test_panel_static_ui.py tests/test_panel_performance_v2.py -q` —
`200 passed`; broader Performance v2/panel set — `411 passed`; `node --check
src/mrs3/panel_web/app.js`; `git diff --check`. External reviewer was
unavailable by user instruction; root self-review covered the scoped diff and
the fail-closed/default-order invariants.

## Performance v2 targeted cache warming after RETEST (2026-09-04)

Cache recalculation now resolves missing current `result_id` windows first and
passes only those strategy IDs to the workers. Existing cached strategies in
the same pair are no longer recalculated; the all-pairs action applies the same
missing-ID selection per pair/side. The panel keeps the existing pair-level
button, but its backend work is now incremental.

Evidence: focused selection/panel/retest/review tests — `201 passed`; `node --check
src/mrs3/panel_web/app.js`; `git diff --check`.

## Portfolio Optimizer Phase 2A accepted; Phase 2B started (2026-09-08)

Phase 2A fixture execution research passed independent Opus review and is
accepted at the `RESEARCH_ONLY` fixture boundary. Focused verification is
`67 passed`; the required execution/liquidity/margin/store set is `280 passed`.
The full suite produced `3327 passed, 7 skipped` and one pre-existing local HTTP
timeout whose exact isolated rerun passed. Evidence is recorded in
`docs/superpowers/plans/2026-09-08-portfolio-optimizer-phase2a-evidence.md`.

Revision 6 of the Phase 2B fixture/fake read-only monitor contract is
`PLAN_APPROVED`. The next implementation step is its isolated LiveStore slice,
followed by reconcile, monitor/order projection, exact charts and the separate
Panel slice. Real REST/WS, credentials, tester execution, notifications,
trading, admission and deployment remain blocked pending separate authorization.

The isolated 2B-1 LiveStore slice is accepted after Opus `CODE_REVIEW_PASS`.
It provides a separate WAL/FK/append-only SQLite history, immutable secret-free
fixture manifests, canonical Decimal evidence, foreign/baseline DB rejection
and correction provenance. Root verification: `32 passed` for the store and
`70 passed` with the provisional monitor/chart regression set. The next step is
2B-2 complete REST/WS reconcile on fixtures.

## Portfolio Optimizer Phase 2B server accepted (2026-09-09)

The fixture/fake-only Phase 2B server core is accepted after the fifth and
final Opus review returned `CODE_REVIEW_PASS`. It includes LiveStore, complete
REST/WS reconciliation, cashflow-adjusted account and symbol metrics, current
margin-load timelines, entry-order projection with the 20-second freshness
contract, and deterministic bounded exact chart series. Root verification is
`228 passed` for the four live suites and `3510 passed, 7 skipped` for the full
project suite. Evidence is recorded in
`docs/superpowers/plans/2026-09-09-portfolio-optimizer-phase2b-server-evidence.md`.

The next Phase 2B step is the separate 2B-9 Panel visualization. Real exchange
REST/WS, credentials, notifications, tester execution, trading, bot/config
mutation, admission and deployment remain blocked pending separate approval.

## Portfolio Optimizer one-size adapter implemented, pending final review (2026-09-09)

The D8 one-size adapter now connects the existing Panel calculation job to the
package-owned strict FINALIST reader, profile gates/ranking, multi-pair
combinatorics, tester minute-volume capacity, current public Bybit
instrument/risk/mark snapshot, collector spread diagnostics and full-position
sizing. Config v1 migrates in memory to strict v2 and resolves minute files from
`<bot_root>/tester/data/bybit`; existing UTF-8-BOM tester CSV files are accepted.
The workbook exposes both 7-calendar-day sizing and 5-weekday analytical
capacity, spread state, warnings and lineage digests. Tester execution remains
outside this adapter.

Focused verification passes 334 tests; the full project suite passes `3625
passed, 7 skipped` with eight pre-existing pandas/archive warnings. JavaScript
syntax, Python compileall and `git diff --check` also pass. A live read-only smoke on the current
six selected LONG pairs reached the intended liquidity gate: `GEUSDT` has only
three available days and about 11.32 USDT mean clock-minute turnover, so 30%
participation rounds down to a zero 50-USDT cap and the complete six-pair
candidate is rejected with `GEUSDT:SIZE_BELOW_MINIMUM_QTY`. Removing that pair
produced one five-pair BALANCED candidate with current Bybit facts; collector
spread history is visibly `PRELIMINARY` (67 available hours). The next step is
independent review before commit when the configured reviewer route is
authorized.

Post-implementation self-review now excludes collector minutes below the
configured coverage threshold and rejects coverage ratios above one. The human
Settings form exposes participation, rounding, coverage, market-reference age,
archive lag, weekend boundaries and explicit backfill. The hidden legacy
`search.total_test_budget` no longer blocks Stage-1 Campaign creation.
Selected pairs now define the search universe: every non-empty subset from one
through all usable pairs is enumerated, bounded only by
`search.max_enumerated_combinations`. A profile's `max_candidates` is retained
for post-joint-test winner selection and does not truncate the pre-test universe.
An exact replay of Campaign `campaign-a26ee8eec74249e6ad0d0f7cb6a2e424`
passes with all 255 non-empty subsets of its eight usable pairs; four zero-cap
pairs are reported as exclusions instead of failing the Campaign. Post-review
focused Panel/adapter checks pass `197 tests`; the complete `test_portfolio_*`
set passes `927 tests`. Python compileall, JavaScript syntax and
`git diff --check` pass. Independent final code review is still pending before
commit.

## Native SINGLE_MODE premature-completion recovery (2026-09-10)

Live job `d8fbe51edaad41af948bc61e3f56cb85` exposed that tester HTTP status can
become `Completed` while detached workers are still writing a 1000-strategy
batch. The panel stopped the dispatcher after 140 reports in batch 4 and the
job failed cleanup with 3000 verified results. Native completion now requires
current-batch result-file evidence, publishes within-batch progress, and a
tracked retry reuses valid reports with a single report-index pass. Recovery
job `707489374995453c9e71d2e825e783fb` accepted 3140 existing reports, then
produced about 808 more HTML files before the tester result journal was
rewritten and the cleanup error masked the run. The native wait now accumulates
file evidence monotonically, tolerates transient status loss, and stops only on
the evidence-stall or total timeout. Failed retry jobs are retryable so the next
run can recover all valid HTML before submitting only the remaining strategies.
Live recovery eventually produced all 5186 reports. Retry job
`24fe865fbd9d42cbb660f3327e998599` exposed a second terminal-path defect: when
the recovery scan found no remaining names, it returned `COMMITTED` without
creating the durable metadata inbox or publishing its counters. The immediate
commit path now creates the inbox and emits the final snapshot; reload-time
verification also restores persisted counters instead of replacing them with
an empty `0/0` progress object. The live inbox contains 5186 entries and, after
a cold panel restart plus repeated inbox verification, the registry remains
`COMMITTED`, `5186/5186`, `inbox_ready=true`, `failed=0`.

The subsequent Performance v2 import committed 5001 rows and rejected 185
FWDI reports because their wallet chart ended two minutes before the final
close action. Current-report integrity now validates the later action balance
when it is newer than the final wallet sample while retaining fail-closed
validation for real balance mismatches. Import
`82f311f5b7014b0eaade73016ab9f6ea` then replaced exactly those 185 current
FWDI results: `185 imported`, `0 skipped`, `0 rejected`, with 185 verified
replacement dispositions.

## Unified local worker limit and Analysis Profile cleanup (2026-09-10)

Static Panel Settings now exposes `duckdb_import.workers` under General as the
single local CPU worker limit. Subsequent Panel jobs use it for HTML/DuckDB and
Source v6 import/merge, surface materialization/publication, fresh analysis,
DUCKDB_DIRECT, Performance v2 import/selection, and Portfolio Search. Tester
submission and API/network concurrency remain separate. Legacy per-subsystem
worker keys no longer override the common value and were removed from tracked
examples.

Analysis Profile now contains only the approved visible analysis controls,
uses the clarified Russian labels and separated semantic sections, and
preserves hidden algorithm values on save. Focused verification passes `470
passed, 2 skipped`; JavaScript syntax, Python compileall and `git diff --check`
pass. Panel was not restarted.

## Source v6 READY preflight preview (2026-09-10)

The READY-scope table now shows the actual canonical data period, source-point
`PnL > 10%` count/total/percentage, source PnL median/maximum, and status for
each timeframe, with the same aggregate fields and `READY x/y` at Pair+Side
level. The unused Grid column was removed. Preview metrics reuse the metadata
already loaded by preflight, remain display-only, and fail closed for duplicate
points, malformed PnL, or incomplete READY-period coverage.

Focused verification passes `107 passed`; the related Panel suite passes `261
passed`. JavaScript syntax, Python compileall, and `git diff --check` pass. A
read-only preflight of the current Source v6 database completed in 4.319 s and
returned `BABAUSDT LONG 3h = 172 / 684 · 25.1%, median 7.75%, max 18.27%` for
`2026-07-11..2026-08-24`. Panel was not restarted.

## Finalist control rich Candidates workbook accepted (2026-09-17)

The current-finalist and completed-retest control exports now reuse the
canonical Performance v2 `All candidates` renderer as the editable `Candidates`
sheet. The exported population remains effective `FINALIST`, with `RESERVE`
included only when requested, while the sheet carries the full strategy,
PnL/DD, window, order, automatic-selection, user-review and comment fields.
The existing multi-group control importer remains authoritative; the former
narrow Candidates schema is no longer accepted.

Current-control and Pareto/filter XLSX exports fail closed with typed
`SELECTION_CACHE_INCOMPLETE` when their selection cache is not ready and return
no workbook. The Panel keeps the user on the page, reports that the cache must
be prepared or recalculated, and preserves the server-provided download name
on success. Full verification passes `4461 passed, 7 skipped`; JavaScript
syntax and `git diff --check` pass. Independent final review returned
`CODE_REVIEW_PASS`.

## Performance v2 tester yesterday ceiling and incremental recalc regression (2026-09-15)

The tester end date is now capped at local current date minus one day in the
Panel UI, shared native SINGLE_MODE/FAST validation, and Performance v2 import.
Every imported report requires a parseable range; missing or malformed ranges
fail closed on all paths, including `check_range=False` revalidation. Committed
inbox metadata and parsed report ranges fail closed when their end is later than
yesterday, so stale or forged inboxes cannot publish rows. Range shortcuts clamp
future anchors to the same maximum. Selection cache recalc keeps
its existing incremental implementation; a regression test verifies ADD and
REPLACE identify only the new current strategy and repeated pair/all-pairs calls
process zero once current Result IDs are cached.

Evidence: `.venv\Scripts\python.exe -m pytest tests/test_single_mode_handoff.py
tests/test_panel_static_ui.py tests/test_performance_v2_import.py
tests/test_panel_performance_v2.py tests/test_performance_v2_selection.py -q`
— `345 passed, 2 skipped`; `node --check src/mrs3/panel_web/app.js`; targeted
`compileall`; and `git diff --check` all passed. No tester, Bybit, real
PerformanceDB, or generated report was used or changed.

Operator evidence after recalculating all missing pairs on 2026-09-15 confirms
that the A/B deterioration and time-window filters work and that the XLSX export
contains the A/B PnL/30, individual window values, and Positive windows data.
This accepts the repaired current-cache path; the separate Phase 8 typed
Price/Cost and prepared-series/cycle persistence task remains open.
Independent Opus review returned `CODE_REVIEW_PASS` after the fail-closed
no-legacy period requirement and midnight-refresh correction; final root
verification was `346 passed, 2 skipped`.

## Performance v2 optimizer prepared inputs accepted (2026-09-17)

Phase 8 upgrades Performance v2 additively to schema v5. It persists nullable
typed action Price/Cost, six nullable WS1.1 sizing facts, and one private,
versioned, digest-bound, per-result prepared optimizer input with
normalization-ready actions, equity and reconstructed cycles. ADD/REPLACE writes
the artifact inside the existing import transaction. The v4 migration backfills
only exact revision-checked saved JSON facts and does not eagerly rebuild
history.

Ordinary Performance recalculation prepares missing/stale requested current
results using the existing `duckdb_import.workers`; workers receive immutable
Python snapshots and never access DuckDB, while the writer transaction reloads
identity and digest before replacing a row. Strict PerformanceDB reads have no
compatibility-JSON fallback, and prepared data remains absent from public API,
Members and XLSX.

Independent Opus implementation review returned `CODE_REVIEW_PASS`. Final root
verification passed `4503 passed, 7 skipped, 25 warnings` in 1628.68 seconds;
focused migration/import/optimizer/input and protected Panel/selection/Portfolio
suites also passed. See the
[specification](docs/specs/2026-09-17-performance-v2-optimizer-prepared-inputs.md),
[ADR-0037](docs/decisions/0037-performance-v2-optimizer-prepared-inputs.md),
[ADR-0038](docs/decisions/0038-performance-v2-prepared-canonicalization-and-locking.md)
and [acceptance evidence](docs/superpowers/plans/2026-09-17-performance-v2-optimizer-prepared-inputs-evidence.md).
No production database, tester execution, network access, recommendation
surface change, runtime path change or live authorization occurred. Phase 7
real tester calibration remains separately gated; the next implementation item
is Phase 9.

## Native SINGLE_MODE Windows sharing-violation recovery (2026-09-17)

Batch preparation now keeps the configured `settings_strategy` directory and
removes only its contents. A transient Windows sharing violation after tester
shutdown is retried for up to 30 seconds; unrelated filesystem failures still
fail closed. Live retry job `c76d9ea0f02344bf8abec30d6ee609e8`
reused 1,000 verified reports, tested only the remaining 44 strategies, and
committed `1044/1044` with zero failures. Focused Panel/runner verification
passes (`90 passed, 1 skipped`). Independent Opus review returned
`CODE_REVIEW_PASS`.

## Performance v2 incremental cache regression (2026-09-18)

The 1,044-report import completed, but the following selection-cache request
also invoked an unscoped optimizer-artifact warmup. That path loaded all 14,452
current results and their action/equity history before checking the prepared
cache, so the Panel appeared to recalculate for about ten hours and did not
finish normally. The database remained consistent; a read-only audit found
exactly 1,044 missing selection-cache strategies across the four newly imported
pair/side groups.

Selection recalculation now updates only missing current Result IDs and never
invokes optimizer preparation. Portfolio campaign snapshotting prepares only
the exact current `FINALIST` Result IDs before its strict read. Import no longer
rereads every newly written action/equity series: it builds the optimizer source
from the normalized parsed reports and validates child counts with two grouped
queries. Focused integrated verification passes `499 passed, 2 skipped`.
## Portfolio Optimizer Phase 9 accepted (2026-09-18)

WS1.2 replaces the LONG-only executable revision. Panel finalist bounds are
nonnegative per side, User Rank defines each top-N slot pool, and the adapter
enumerates the exact fixed-slot Cartesian product. Distinct chosen compositions
remain distinct even when a member receives zero weight; every profile uses one
bounded global ranking before any separately authorized real test.

LONG and SHORT for one canonical symbol may coexist. They share one liquidity
cap in discovery, redistribution, validation, rescue, target calculation and
sizing; margin remains additive without side netting and limiter replay keeps
the strategies as distinct slots. Directional payloads and XLSX rows preserve
side and chosen-member identity.

Independent Opus implementation review returned `CODE_REVIEW_PASS` after three
finding rounds. Root verification passed the focused Phase 9 contour
(`1093 passed`) and the final full suite
(`4552 passed, 7 skipped, 25 warnings` in 1028.47 seconds); `git diff --check`
was clean. No tester, network, production database, live execution or
recommendation state was used or changed. See
[ADR-0039](docs/decisions/0039-portfolio-optimizer-ws12-directional-shared-cap.md)
and [Phase 9 evidence](docs/superpowers/plans/2026-09-18-portfolio-optimizer-weighted-search-phase-9-evidence.md).
The next implementation item is Phase 10.

## Source v6 import with descriptive Windows target (2026-09-15)

An import into a new Source v6 target failed before writing data because each
segment scratch filename repeated the 64-character run token. With a
descriptive target name this exceeded Windows `MAX_PATH` even though the final
target itself was valid. Segment writes now use a short unique scratch name
in the target directory and still publish atomically. The regression test and
related Source v6 checks pass (`140 passed`). A real import of 21,888 reports
completed with `COMMITTED`, `quarantined=0`; the resulting target contains the
expected Source v6 tables and no leftover segment scratch directory.
