# Source PRETEST A/B Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional 14-day source-PnL collapse gate before Pareto candidate filtering.

**Architecture:** Compute and persist raw A/B evidence during Source v6 materialization by reusing the existing canonical metric calculator over already-decoded fragments. Carry that evidence into fresh-analysis point rows, evaluate order 1 once in the authoritative shortlist filter before Pareto, and propagate one top-level boolean through panel API, audit, generation, and UI.

**Tech Stack:** Python 3.11, DuckDB, pandas, vanilla JavaScript/HTML, pytest.

**Spec:** `docs/specs/2026-09-20-source-pretest-ab-filter.md`

**Approval:** `NOT_ADVISOR_APPROVED`; the configured Opus Advisor failed its authentication check and the user explicitly authorized best-effort continuation without it.

## Global Constraints

- B is exactly the final 14 calendar days of the READY witness; A is the entire READY witness including B.
- Reject only when order 1 has B activity, A daily canonical wallet PnL is positive, and decline is strictly greater than 95 percent.
- No B trades, A daily PnL at or below zero, or less than 14 days of A history never rejects.
- `pretest_ab_enabled` is separate from the four unchanged Pareto criteria and defaults to false.
- New derivative semantics require new surface and analysis fingerprints and rebuild old artifacts under ADR-0017.
- No separate commission adjustment, new dependency, tester run, or source database mutation.
- All project tests use `.venv\Scripts\python.exe -m pytest`.

## Review Focus

- Exact 95-percent decline must pass while the smallest value above 95 rejects.
- A rejected candidate must not dominate a survivor in its Pareto group.
- A multi-order candidate is evaluated only from `orders[0].point_id`.
- Zero B round trips must pass even if canonical B wallet PnL is negative.
- Enabling the gate must be reproducibly recorded in generated artifacts and run snapshots.

---

### Task 1: Materialized evidence and artifact versions

**Files:**
- Modify: `tests/test_source_v6_stage2_metrics.py`
- Modify: `tests/test_source_v6_analysis_fresh.py`
- Modify: `src/mrs3/source_v6_materializer.py`
- Modify: `src/mrs3/source_v6_surface_fresh.py`
- Modify: `src/mrs3/source_v6_analysis_fresh.py`

**Interfaces:**
- Produces: `row["pretest_ab"]`, contract `source-v6-pretest-ab-v1`.
- Produces: surface fingerprint `surface-v6-fresh-compact-v3` and analysis fingerprint `analysis-v6-fresh-compact-v2`.

- [ ] Write failing materializer tests with hand-derived A/B bounds, PnL text, and round-trip counts; include A shorter than 14 days.
- [ ] Run the focused tests and confirm failures are caused by missing `pretest_ab` evidence.
- [ ] Add the smallest helper that builds the nested evidence. Reuse the supplied full-window metrics for A and call `calculate_metrics(fragments, start_ms=end_ms-14d, end_ms=end_ms)` once for B only when A covers at least 14 days.
- [ ] Extend strict row validation for the exact nested schema and bump the surface fingerprint; keep the factual source fingerprint unchanged.
- [ ] Carry the nested object through `_analysis_frame_row`, bump the fresh-analysis fingerprint, and update fingerprint assertions.
- [ ] Run focused materializer/surface/analysis tests to green.

### Task 2: Authoritative pre-Pareto gate and audit

**Files:**
- Modify: `tests/test_fresh_analysis_strategies.py`
- Modify: `tests/test_analysis_filter_export.py`
- Modify: `src/mrs3/fresh_analysis_strategies.py`
- Modify: `src/mrs3/analysis_filter_export.py`

**Interfaces:**
- Changes: `filter_fresh_analysis_candidates(..., *, pretest_ab_enabled: bool = False)`.
- Changes: `list_fresh_analysis_shortlist(..., *, pretest_ab_enabled: bool = False)`.
- Produces row fields: `pretest_ab_enabled`, `pretest_ab_status`, `pretest_ab_reason`, `pretest_ab_decline_pct`, and raw A/B evidence.

- [ ] Write failing table-driven tests for disabled identity, exact 95, above 95, negative B, zero B activity, non-positive A, insufficient history, first-order-only behavior, and removal before dominance.
- [ ] Run them and confirm failures come from the missing gate.
- [ ] Implement one Decimal-based evaluator and partition candidates before building Pareto groups. Merge rejected and survivor rows deterministically without changing `CRITERIA`.
- [ ] Add PRETEST columns and summary counts to the existing fresh audit exporter; do not create a second exporter.
- [ ] Run focused filter and audit tests to green.

### Task 3: Panel, generation provenance, and UI

**Files:**
- Modify: `tests/test_panel_fresh_strategies.py`
- Modify: relevant fresh generation/run tests found beside existing `phase2_filters` assertions
- Modify: `src/mrs3/panel.py`
- Modify: `src/mrs3/fresh_analysis_strategies.py`
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`

**Interfaces:**
- Consumes top-level request field: `pretest_ab_enabled: bool`.
- Generated provenance records: enabled flag, `window_days=14`, `decline_threshold_pct="95"`, contract version, and existing analysis identity/digest bindings.

- [ ] Write failing API tests for default false, explicit true propagation, and rejection of non-boolean values.
- [ ] Write failing generation/run snapshot tests that assert the PRETEST provenance block.
- [ ] Add one panel parser for the top-level boolean and pass it to shortlist, audit, generation, and run paths.
- [ ] Add a native unchecked checkbox with explanatory copy and include its value at the top level of shortlist/audit/generation requests.
- [ ] Run focused panel/generation tests to green.

### Task 4: Documentation and verification

**Files:**
- Modify: `PRD.md` only to register the feature and its source-only limitation.
- Modify: `progress.md` with current state, verification commands, rebuild requirement, and next step.

- [ ] Update documentation without claiming source metrics are realized MRS3 performance.
- [ ] Run all focused tests covering Tasks 1-3.
- [ ] Run `.venv\Scripts\python.exe -m pytest -q` and report every failure by name.
- [ ] Run `git diff --check` and inspect the complete diff.
- [ ] Send the complete requirements, diff, and verification evidence to an independent read-only reviewer; resolve confirmed Important/Critical findings and re-run affected checks.
