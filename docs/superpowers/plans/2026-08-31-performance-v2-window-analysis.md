# Performance v2 — Phase 2 A/B window analysis

**Status:** implemented; `CODE_REVIEW_PASS`

## Goal

Expose the existing cached UPNL-relative window calculator for one selected
active Performance v2 strategy. Import remains fast and performs no eager
window calculation.

## Evidence gate

Baseline before this phase:

```text
.venv\Scripts\python.exe -m pytest -q tests/test_performance_v2_windows.py tests/test_panel_performance_v2.py tests/test_panel_static_ui.py
69 passed in 6.71s
```

| Item | Baseline |
| --- | --- |
| Catalog endpoint, windows endpoint, shared current-result resolver | Absent |
| Four-field cache read/upsert and calculator | Present |
| Atomic pair publication/conflict reread | Absent |
| A/B selector/date UI | Absent |
| Import window parameters | Deprecated no-ops |

## Delivery

1. Write failing controller tests for catalog, strict UTC input, authoritative
   `current_result_id`, typed availability, cache hit, base-data immutability,
   transaction conflict and R1-to-R2 replacement cache isolation.
2. Add set-based catalog and one-result transactional A/B functions plus the
   two panel routes. A lock/conflict maps to typed `409`; no retry or pool.
3. Write failing static-panel checks, then add a native strategy select and
   four UTC-labelled `datetime-local` inputs. The browser appends `Z` directly.
4. Verify the baseline selector plus panel/v1 non-disturbance suites, perform a
   live smoke on the existing DB, and obtain `CODE_REVIEW_PASS` before commit.

## Presentation follow-up (2026-08-31)

The manual A/B table shows the server-provided `trade_rate_30d` as
**«Сделок / 30д»**. Display values round to at most two decimal places, while
the API and DuckDB retain their existing precision; holding time displays in
minutes. The catalog also supplies the selected strategy's Pair, Side,
Timeframe, Close MA and ordered Open MA / shift / multiplier / lot parameters
for a read-only summary above the comparison.

## Non-goals

No DD5 proxy UI, filters, Pareto, selections, XLSX, tags, discard, RETEST,
Portfolio input, batch analysis, Runs UI, Fast/legacy behavior, schema
migration, or removal of deprecated import window fields.
