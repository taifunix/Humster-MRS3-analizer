# v0.7 legacy selection

**Status:** Active  
**Depends on:** v4 import evidence; [event filter](v07-event-filter-and-shortlist.md); [master handoff source](../archive/sources/MRS3_v07_MASTER_HANDOFF_LEGACY_DUCKDB_2026-08-10.md)

## Goal

Produce a deterministic v0.7 `legacy_trades_proxy` selection run on the common interval `[2026-07-15T00:00:00, 2026-08-06T00:00:00)`, with audit and JSON candidates for real MRS3 tick-tests.

## Evidence gate

Before implementation, obtain v4 DuckDB `schema_version=4`, `import_manifest.json`, quarantine count, `html_delete_checklist.csv`, and a query proving there are no row-per-sample tables. HTML remains untouched until every target row says `safe_to_delete=YES`.

## Deliverables

1. A tested materializer that reads v4 raw payloads, writes versioned `point_period_metrics`, and calculates the common-window metrics without altering raw data.
2. Metric reconciliation on 3–5 reports for PnL, DD, TotalTrades, WinRate, and ProfitFactor. Rows with unverified trade aggregation cannot enter selection.
3. A deterministic unified input: only coarse rows whose period exactly matches the common window; fine wins exact duplicates; shadow/conflict decisions appear in audit.
4. A v0.7 selector mode with `event_mode=legacy_trades_proxy` and `point_event_count=TotalTrades` for every input row. It must reject mixed event modes.
5. A full rebuild after event filtering: point eligibility → plateau viability → close profiles → families → structures → lots → JSON → audit. Never filter prebuilt v0.6 structures.
6. A result directory containing manifest, workbook, CSV audit and EQUAL/INCOME strategies, with hashes, window, event mode and source counts.

## Invariants

- Fine and coarse rows are compared only on the exact common window.
- Real independent events and legacy trades proxy never mix in a run.
- Every MRS3 order requires `point_event_count >= 3`.
- A plateau is MRS3-eligible only with at least one economic-pass point meeting the event gate.
- Representative order after 5%-equivalence and event filtering: event count descending, shift descending, PnL descending, efficiency descending, point ID ascending.
- Existing v0.6 geometry, economic gates, gap rules, lot methods and JSON safety rules remain unless an approved v0.7 spec changes them.

## Acceptance evidence

- Unit tests cover legacy mapping, mixed-mode rejection, event gate, representative ordering, plateau viability, rebuild behavior and duplicate-source policy.
- Materialization audit records input/output comparison and verification status for sampled reports.
- Selection audit declares all source, dedup, shadow and conflict decisions.
- No output claims a real MRS3 PnL before tick-testing the generated JSON.

## Out of scope

Real independent-event mode, source-potential production filtering and portfolio simulation. Their requirements are separate specifications and need additional evidence.
