# Portfolio Optimizer M0 evidence

schema: portfolio_optimizer_m0_capabilities_v1
version: 1
date: 2026-09-06
source_baseline: 93d1826b741547519afb548f9e36719c77ba085c
status: ACCEPTED
review_disposition: CODE_REVIEW_PASS
reviewer: Claude Opus 5 (high)
review_rounds: 3
used_models: claude-opus-5

## 1. M0 boundary and evidence rules

This is the M0 source and contract inventory. It records existing fields,
read paths, tests, sanitized fixtures, and one isolated DuckDB probe. It does
not implement Portfolio Optimizer runtime.

M0 performed no tester or bot binary execution or tester-target executable
probe, public or private API request, collector startup, live config/DB/storage
access, source write,
Performance DB mutation, or Portfolio DB write. No real local archive, source
database, tester target, account, credential, or generated artifact is part of
this evidence. No tester/bot binary or executable target was probed; the
isolated synthetic DuckDB probe in Section 2.3 is the only executable
diagnostic exception.

A field name in a template, schema, or fixture is a physical field only. It is
not confirmed runtime behavior unless a caller, parser, report contract, and
relevant fixture establish the behavior. UNKNOWN is fail-closed; it is not a
default value. A capability result is one of:

- CONFIRMED_CAPABILITY: the narrow behavior is established by source and test.
- APPROVED_CONSERVATIVE_BOUND: formula, value, provenance, and covered states
  are established for a conservative bound.
- BLOCKING_UNKNOWN: a required fact or behavior is absent or unsafe to infer.

## 2. Source/read-only/cache snapshot

### 2.1 Performance v4 source contract

| Fact | Evidence and M0 interpretation |
|---|---|
| Canonical DB and schema | `src/mrs3/performance_v2_store.py:15-16,159-173` resolves `<database_root>/strategy_performance.duckdb` and validates schema version 4. |
| Typed strategy/order/result rows | `src/mrs3/performance_v2_store.py:182-247` stores strategy identity, immutable order geometry fields, and a replaceable current result pointer. |
| Actions and equity | `src/mrs3/performance_v2_store.py:249-273` stores non-null action and wallet/equity fields; currency, tick precision, and portfolio semantics are not established. |
| Window facts | `src/mrs3/performance_v2_store.py:275-299`; `src/mrs3/performance_v2_windows.py:13-41` stores requested/effective windows, availability, metrics, and trade count, many nullable. |
| Selection and review | `src/mrs3/performance_v2_store.py:301-397`; `src/mrs3/performance_v2_selection_review.py:118-180,268-426` stores pointers, tags, status, and review state, not a full immutable campaign snapshot. |
| Current result replacement | `src/mrs3/performance_v2_import.py:1238-1335`; `tests/test_performance_v2_import.py:337-390` confirm scoped actions/equity/window rows are replaced while strategy/result identity and tags can remain. A result ID is not immutable history. |

### 2.2 Read paths, cache, and snapshot safety

| Path | Observed code fact | Boundary |
|---|---|---|
| Window cache miss | `src/mrs3/performance_v2_windows.py:391-428` calculates and persists missing `window_metrics`. | Never use this path as a source read. |
| Selection default | `src/mrs3/performance_v2_selection.py:638-779` uses `cache_only=False` by default and may calculate/persist missing metrics. | Exclude from optimizer input. |
| Cache-only selection branch | `src/mrs3/performance_v2_selection.py:677-681` selects cached metrics when `cache_only=True`; existing cache-readiness tests are at `tests/test_performance_v2_selection.py:567-624`. No exact test proves that a cache-only miss performs no window/source write and surfaces empty/UNKNOWN. | This is `BLOCKING_UNKNOWN`, owner M1. Future adapter must use `cache_only=True`; add the exact no-write/miss contract before relying on it. |
| Source snapshot | `src/mrs3/performance_v2_selection.py:547-559` uses separate read-only worker connections and later writable persistence. | No existing adapter reads all facts in one consistent transaction. This is `BLOCKING_UNKNOWN`, owner M1. |
| DuckDB primitive | Isolated temporary probe opened a read-only connection, ran two SELECTs and ROLLBACK with NO concurrent writer, and rejected INSERT. | It demonstrates NO snapshot isolation in synthetic or real DB: no concurrent writer existed, and read-only could not open beside a read-write connection. If read-only open is rejected while a writer/read-write connection exists, M1 aborts and records UNKNOWN; it never falls back to write-capable access or stops the source writer. |

Confirmed narrow source capabilities are schema-v4 validation, typed row reads,
and explanation of current-result replacement. Immutable campaign/replay/tick
identity is UNSUPPORTED by the current source. Cache-only miss behavior and a
single consistent source transaction remain M1 blockers.

### 2.3 Performance verification recorded by M0

Focused existing fixture tests, using the required local environment:

`\.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_store.py tests/test_performance_v2_input.py tests/test_performance_v2_selection.py tests/test_performance_v2_selection_review.py tests/test_performance_v2_windows.py tests/test_performance_v2_retest.py -q`

Result: `237 passed, 1 skipped, 1 warning in 39.47s`; the skip is the
symlink-unavailable case at `tests/test_performance_v2_input.py:216`.

Import replacement/readback fixtures:

`\.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_import.py -q`

Result: `63 passed in 34.15s`.

Synthetic DuckDB probe (temporary `.tmp` database, removed after each run):

```text
python: .venv/Scripts/python.exe
duckdb: 1.5.5
read-only BEGIN, two SELECT calls, ROLLBACK: before=1, during_same_tx=1
write attempted on read_only connection: rejected with
  Cannot execute statement of type "INSERT" ... attached in read-only mode
read-write connection held, then read_only connection opened: rejected with
  Can't open a connection ... different configuration than existing connections
```

All tests use temporary synthetic databases or inboxes. They establish source
contracts only; they do not establish real tester, binary, tick, or account
facts.

## 3. Collector schema, reference, and readiness

The existing public Bybit collector is reused. M0 did not start it and did not
call ticker or any API. Source of the 40-column schema is
`src/mrs3/bybit_collector/aggregation.py:14-91`; archive validation is
`src/mrs3/bybit_collector/archive.py:286-432`.

| Contract | Evidence and interpretation |
|---|---|
| Liquidity row | `minute_ts_ms`, `symbol`, counts, coverage/connection ratios, mid/spread, bid/ask visible depth at 10/25/50/100 bps, and side/combined completeness. Depth is public `orderbook.1000` visible notional, not full-exchange capacity (`src/mrs3/bybit_collector/aggregation.py:435-534`). |
| Archive metadata | `schema_name=bybit_liquidity_1m`, `schema_version=2`, `exchange=bybit`, `category=linear`, non-empty collector version, and UTC publication time (`src/mrs3/bybit_collector/archive.py:310-325,348-380`). |
| Read authority | `published_hours` is authority; unmarked finals are ignored and missing marked files are diagnosed (`src/mrs3/bybit_collector/storage.py:384-389`; `src/mrs3/bybit_collector/archive.py:186-213`; ADR-0024). |
| Read-only storage | `SQLiteSpool.open_read_only` uses `query_only=ON` and fails closed if the DB is missing (`src/mrs3/bybit_collector/storage.py:190-219`). The writer constructor at `:128-188` is out of scope. |
| Reference | Instruments and risk tiers include leverage steps, tier limits, MM rates, and `captured_at_ms` (`src/mrs3/bybit_collector/reference.py:121-198,359-417`). This proves normalization and pagination, not account state or applied leverage. |
| Readiness | Real archive presence, symbols, continuity, freshness, coverage, expected tier completeness, turnover, global liquidity, fill probability, and full-exchange capacity are UNKNOWN. A marker alone is not a readiness PASS. |

Mutation traps include `write_minute`, marker writes, symbol events, hourly
export/recovery, and ordinary reference collection
(`src/mrs3/bybit_collector/storage.py:242-285,332-460`;
`src/mrs3/bybit_collector/archive.py:75-267`;
`src/mrs3/bybit_collector/reference.py:241-417`).

Collector fixture verification recorded by M0:

`\.venv\\Scripts\\python.exe -m pytest -q tests/test_bybit_collector_storage.py tests/test_bybit_collector_archive.py tests/test_bybit_collector_reference.py`

Result: `55 passed in 9.92s`. These are sanitized storage/archive/reference
fixtures and do not prove live collector readiness.

## 4. Physical tester fields and report contract

The current runner is a single-strategy path. Config, HTTP, files, results,
inbox, and store evidence:

| Area | Evidence | Result |
|---|---|---|
| Config and HTTP | `src/mrs3/runner/config.py:40-57,77-200`; `src/mrs3/runner/http.py:65-81,159-217` | Local HTTP, one executable, one strategy, and `tester-wizard-is-multi == false`; no portfolio endpoint or mode. |
| Workflow and files | `src/mrs3/runner/files.py:146-267`; `src/mrs3/runner/workflow.py:299-352,773-904` | Installs one-strategy JSON, replaces shared paths, starts the local tester, reconciles per-strategy HTML, and writes one CSV. |
| Results and store | `src/mrs3/runner/results.py:197-246,290-376,412-474`; `src/mrs3/performance_store.py:12-37` | One embedded strategy/trade table and per-run actions/equity; `portfolio_event_ready` is hardcoded FALSE. No joint equity or close attribution. |

Physical template fields include exchange/account/subaccount, unified account,
symbol, one `time_frame`, long/short switches, cross margin, leverage, fixed
balance, percentages, risk, lots, `max_balance`, `max_bars`, MA/close geometry,
order type/post-only/hidden values, and close/replace flags
(`templates/strategies/retest-mrs3/base.json:11-48,139-229`). Tester config
fields include dates, `WarmupDays`, fee/slippage/funding, volume check, and
parallel-run settings (`templates/tester/mrs3/config_tester.json:2-55`).
These are fields only: no dual-TF/shared state, limiter/priority, PairSlot,
opposite policy, portfolio mode, or ownership field is implemented.

Sanitized fixtures inspected: `tests/fixtures/performance/report_current_v2.html`,
`report_import.html`, `report_valid.html`, `report_duplicate_equity.html`,
`tests/fixtures/tester_table.html`, and source-v6 report fixtures. They contain
one strategy/symbol and, where present, one position cycle. No joint portfolio
fixture exists; no fixture was manufactured.

Runner fixture verification recorded by M0:

`\.venv\\Scripts\\python.exe -m pytest tests/runner/test_config.py tests/runner/test_files.py tests/runner/test_http.py tests/runner/test_inbox.py tests/runner/test_results.py tests/runner/test_workflow.py tests/runner/test_monitor.py tests/test_panel_jobs.py -q`

Result: `129 passed, 1 skipped` (Windows symlink privilege at
`tests/runner/test_files.py:299`). These tests establish parser/file/lock unit
behavior only; they are not bot/tester execution or portfolio runtime evidence.

## 5. Mutating callers, owners, cleanup, and current lock gaps

| Caller | Mutates or owns | M0 finding |
|---|---|---|
| `src/mrs3/runner/workflow.py:611-722,773-904,1129-1206` | Executable, root strategy JSON, tester/report/log paths, CSV, inbox | CSV lock only; stops bot on failure; no target-wide owner. |
| `src/mrs3/runner/files.py:146-267` | Report tree, logs, staged/root JSON, backup | Root JSON rollback exists; cleanup is path-scoped, not target-scoped. |
| `src/mrs3/panel_strategy_batch.py:194-357` | Tester date config, shared paths, report copies | Process-local service lock. |
| `src/mrs3/panel_fast_strategy_test.py:270-296,402-590,1049-1154` | Tester config, shared paths, logs, retry/delete artifacts | Process-local `RLock`; SINGLE_MODE and FAST still mutate the shared target. |
| `src/mrs3/panel_tester_runs.py:39-46,155-218` | Batch file, run reports, subprocess lifecycle | Process-local lock; subprocess target is not target-wide protected. |
| `src/mrs3/panel.py:1211-1220,1727-1744,2677-2733` | Job journal and routed jobs | `PanelJobRegistry` is process-local with logical keys only. |
| `src/mrs3/performance_v2_retest.py:29-81,394-493,623-662` | Retest output and Performance DB audit tags | Advisory lock covers publication output, not tester target. |

`workflow.py:611-673` stale-PID handling does not prove host/boot/process-start
identity and does not prevent two output CSVs from mutating one target.
Foreign-owner handling, target-wide lease, restore snapshot, run-owned
workspace, and safe cleanup proof are UNKNOWN and belong to M5. A failed run
retains state and stops the bot, but no contract restores every shared path or
proves safe deletion of shared/unowned reports. Preflight must fail closed.

## 6. Planned Portfolio DB writers and lock order (M1-M8)

No Portfolio DB path, module, schema, or writer exists in M0. The following is
planned inventory, not implementation:

| Stage | Planned writer and evidence to retain | Lease/lock requirement |
|---|---|---|
| M1 | Config, Portfolio DB schema, Campaign/TradingRun/Evaluation/PortfolioSet, frozen source snapshot/digest | Acquire DB-scoped lease on canonical resolved Portfolio DB path before first write. Abort and record UNKNOWN if the required read-only source snapshot cannot open; never fall back to write-capable access or stop the source writer. |
| M2 | Liquidity/reference facts, ticker evidence, freshness screen, global PortfolioSet screen | Use the M1 DB lease; ticker evidence is outside the Performance DB transaction. |
| M3 | Margin envelope, limiter evaluation, quantity round-down, reserve facts and guards | Use DB lease; missing margin facts remain UNKNOWN. |
| M4 | PairSlot/proposal/order policy, dual-TF/shared fields, opposite-order decisions | Use DB lease; no renderer defaults for unknown capability. |
| M5 | Target-wide tester ownership, run workspace/manifest, binary/settings/tick identity, restore records | One cross-process tester-target lock shared by panel, RETEST, CLI, and optimizer. |
| M6 | Portfolio report import, actual cycles/equity/DD/fee/funding/close attribution, TradingRun readback | Import only run-owned and manifest-matched artifacts. |
| M7 | Frozen warmup/OOS search and validation evaluations; PnL/liquidity/freshness/ranking gates | No validation selection from observed validation returns. |
| M8 | Export manifests, decision/Evaluation identity, NEEDS_RETEST/NEEDS_RESCREEN, final report | Export only after all required gates and provenance are present. |

Planned ownership identity is PID, process-start, host/machine, and
boot/container-instance. Foreign or unknown ownership blocks; reclaim is only
for proven-dead same-host/same-boot owners. Read-only preparation holds no DB
lease; publication uses a short transaction. The DB transaction must not be
held while waiting for the tester lock, and the tester lock does not replace
the Portfolio DB lease (`docs/specs/2026-09-05-portfolio-optimizer.md:333-339,695-706`).
All M1-M8 writer and lock behavior is unimplemented in this M0 evidence.

## 7. Q01-Q12 capability ledger

Status is the overall capability status. A template field or static source
field does not close the corresponding runtime question. Fixture `NONE` means
no exact sanitized fixture establishes the requested behavior.

| Q | Status | Exact evidence / fixture | Owner; blocked stage; fail-closed action | Capability result |
|---|---|---|---|---|
| Q01 dual-TF/shared LONG-SHORT | UNKNOWN | `templates/strategies/retest-mrs3/base.json:11-48,139-229` has one `time_frame` and separate long/short fields; fixture NONE. | Renderer/proposal owner; blocks M4. Exclude mixed-TF/shared-state proposals. | BLOCKING_UNKNOWN |
| Q02 leverage | UNKNOWN | Static template `leverage` and collector tier fields (`src/mrs3/bybit_collector/reference.py:155-163,184-198`); no setter/result/mismatch fixture; fixture NONE. | Venue/deployment owner; blocks M3 and M8. Do not claim applied leverage or deploy. | BLOCKING_UNKNOWN |
| Q03 limiter/priority | UNKNOWN | No storage, event, comparison, tie, or overflow behavior in runner/templates; fixture NONE. D5 semantic defaults are not physical evidence. | Limiter owner; blocks M3. Reject limiter-dependent candidates. | BLOCKING_UNKNOWN |
| Q04 opposite/cancel/partial | UNKNOWN | Existing order fields are template inputs only; no opposite opening, cancel/replace, partial-close, or reserve contract; fixture NONE. | Order adapter/renderer owner; blocks M4 and M5. Do not render or run this branch. | BLOCKING_UNKNOWN |
| Q05 sizing/cap/risk | UNKNOWN | Fixed balance, percentages, risk, lots, and `max_balance` fields exist; no basis, timing, resize, cap, or risk mapping behavior; fixture NONE. | Sizing owner; blocks M3 and M4. Do not infer sizing or cap semantics. | BLOCKING_UNKNOWN |
| Q06 portfolio mode/metrics | UNSUPPORTED | Runner requires `multi=false`, one strategy/report, per-run actions/equity, and no joint series (`src/mrs3/runner/http.py:65-81`; `src/mrs3/runner/results.py:197-246`; `src/mrs3/performance_store.py:12-37`); fixture NONE. | Portfolio runner/report owner; blocks M5 and M6. Do not treat SINGLE_MODE as portfolio evidence. | BLOCKING_UNKNOWN |
| Q07 PnL/liquidity/freshness/ranking | UNKNOWN | ADR-0029 `docs/decisions/0029-portfolio-optimizer-research-risk-profile-v1.md:18-39` confirms research policy DD/free-margin/MM: AGGRESSIVE 20/20/50, BALANCED 10/40/35, CONSERVATIVE 5/60/20. Collector has observed depth only (`src/mrs3/bybit_collector/aggregation.py:49-91`); PnL floor, liquidity/freshness limits, turnover, and exact ranking remain open. Fixture NONE. | Optimizer policy owner; blocks M2 liquidity and M7/M8 READY. Missing account inputs stay UNKNOWN; no READY or ranking claim. The research policy is CONFIRMED_CAPABILITY, not an APPROVED_CONSERVATIVE_BOUND over missing inputs. | BLOCKING_UNKNOWN |
| Q08 research windows | UNKNOWN | Dates and `WarmupDays` are config/template fields; no exact window, minimum evidence, warm-up, carry-state, or upstream OOS fixture; fixture NONE. | Validation owner; blocks M7 and READY. No independent OOS or READY. | BLOCKING_UNKNOWN |
| Q09 selection snapshot | UNKNOWN | `src/mrs3/performance_v2_store.py:331-397` stores selection pointers/status; hashes and resume facts do not provide full campaign/tick/binary identity or one consistent source snapshot; fixture NONE. | Snapshot owner; blocks M1 and M6/M7. Fail closed on incomplete immutable snapshot. | BLOCKING_UNKNOWN |
| Q10 target ownership/restore | UNKNOWN | Process-local/CSV locks and scoped cleanup exist (`src/mrs3/runner/workflow.py:611-673`; `src/mrs3/panel_jobs.py:30-39,220-238`); no target-wide lease, foreign-owner, restore, or run-owned workspace fixture. | M5 runner owner; blocks all real runs. Stop preflight without mutation. Even after M5 ownership is reviewed, any real tester/bot run needs separate explicit user authorization. | BLOCKING_UNKNOWN |
| Q11 collateral/order-loss/mark reserve | UNKNOWN | Collector risk/MM fields are static (`src/mrs3/bybit_collector/reference.py:184-198`); no collateral, denominator, mark, fee provenance, borrow, order-loss, or reserve fixture. | Margin owner; blocks M3. Missing/stale/currency-mismatched facts remain UNKNOWN. | BLOCKING_UNKNOWN |
| Q12 global liquidity/position exit | UNKNOWN | Collector provides per-symbol side depth only; no public turnover adapter, global aggregation, threshold, skew/staleness policy, or exit diagnostic; fixture NONE. | Liquidity/PortfolioSet owner; blocks M2 and M7/M8 PortfolioSet READY. Gross cross-account per-symbol/direction load is used with no netting. Fail closed; single-account fixture research is allowed. | BLOCKING_UNKNOWN |

Q07 policy thresholds do not close PnL, liquidity, freshness, or ranking. Q08
has no independent OOS/READY path. Q12 has no PortfolioSet READY path. No Q
row authorizes `RECOMMENDATION_READY`, tester execution, trading admission, or
live use.

## 8. M0 handoff acceptance and next safe step

| M0 handoff DoD | Evidence in this document | State |
|---|---|---|
| Versioned Q01-Q12 field/capability matrix with owner and evidence | Section 7, with exact source anchors and fixture `NONE` where applicable | ACCEPTED |
| Named fail-closed behavior and blocked M-stage for unknowns | Section 7 and Q07/Q08/Q12 boundary notes | ACCEPTED |
| Source/collector read-only boundary verified | Sections 2 and 3; no runtime/API/source write | ACCEPTED |
| Physical tester fields separated from runtime behavior | Section 4 and Section 7 | ACCEPTED |
| Mutators, ownership, cleanup, and future DB writers inventoried | Sections 5 and 6; all future writers marked unimplemented | ACCEPTED |
| Independent review and progress update | `CODE_REVIEW_PASS`, Claude Opus 5 high, 3 rounds | ACCEPTED |

M0 is accepted after independent `CODE_REVIEW_PASS` by Claude Opus 5 high in
three rounds. The next safe step is M1 TDD for config, the Portfolio DB, and a
source snapshot, using sanitized fixtures only. M1/M5
implementation and review do not themselves authorize a real tester or bot
run; that action requires separate explicit user authorization. M1 does not
authorize an API request, runtime/source write, trading admission, or
`RECOMMENDATION_READY`.
