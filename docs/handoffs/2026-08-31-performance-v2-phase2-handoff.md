# Performance v2 — handoff for the next session

## Current state

- `main` is pushed through `75277f1` (`feat: normalize performance v2 windows`).
- The native `SINGLE_MODE` → metadata inbox → Performance v2 import is the
  active path.  It has completed a real `1633/1633` import; v1 remains
  untouched.
- Phase 2 manual A/B analysis is complete: one ACTIVE strategy, two strict UTC
  windows, one four-column table, semantic change colours and period shortcuts.
- The API derives a 30-day equivalent from each window's effective timestamps:
  return, growth factor and trade rate are normalized; DD, fees, PF and related
  metrics remain explicitly raw.  Short/invalid windows show a status rather
  than a normalized value.

## Verified evidence

- `97 passed` — focused window/panel/static tests.
- `154 passed` — panel and integration regression suite.
- `node --check src/mrs3/panel_web/app.js` and `git diff --check` passed.
- Implementation review: `CODE_REVIEW_PASS` (Opus).

## Start here

1. Read `AGENTS.md`, `PRD.md`, `progress.md`, this handoff, the v2 spec and
   ADR-0020:
   `docs/specs/2026-08-28-unified-performance-analytics-v2.md` and
   `docs/decisions/0020-unified-performance-analytics-v2.md`.
2. Read the delivery record:
   `docs/superpowers/plans/2026-08-28-unified-performance-analytics-v2-vertical-slice.md`.
3. Do **not** reopen the completed manual A/B work unless a concrete defect is
   reported.  The next v2 increment is not selected yet: tags/discard + RETEST,
   or server-side filters/Pareto → selection → XLSX/Portfolio input are all
   deferred.  Automatic/batch analysis has not been specified and must not be
   inferred from the manual A/B screen.
4. Before changing behaviour, write or update a focused spec for the chosen
   next increment, then use TDD.  Keep v1 untouched, preserve the trusted
   metadata-only inbox and v2's single-writer/current-result invariants.

## Hard boundaries

- No DD5 proxy, portfolio simulation, legacy Fast UI, Runs UI, `point_id`, or
  arbitrary filter expressions.
- Do not claim source or normalized A/B metrics are tick-tested MRS3 results.
- Run tests only with `.venv\Scripts\python.exe -m pytest`.
