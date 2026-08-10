# v0.7 Event Source Packs

**Status:** Active

**Depends on:** [v0.7 legacy selection](2026-08-10-v07-legacy-selection.md), [event filter](v07-event-filter-and-shortlist.md)
**Supersedes:** the `legacy_trades_proxy`-only limitation in the active legacy-selection specification.

## Goal

Provide two reproducible input packages for v0.7 selection. A selection run consumes exactly one package; a package declares its event mode, UTC half-open window and audit.

## Packages

| Package | Source | `event_mode` | `PointEventCount` |
| --- | --- | --- | --- |
| CSV package | One or more user-selected tester CSV files | `legacy_trades_proxy` | `TotalTrades` |
| DuckDB package | v4 compact report database | `real_independent_events` | distinct reconstructed `event_id` |

The UI must let the user prepare either package. A DuckDB export preserves `real_independent_events`; converting it to a CSV file never changes its declared mode. The selector rejects a package with missing, unknown or mixed modes.

A real DuckDB package may contain both `LONG` and `SHORT` points. Its `point_id`
is the authoritative six-part identity and includes the side. A selection run
accepts exactly one `--side`, filters the package to that side before
normalization, and validates only that side's point/event mappings. It must
reject an invalid point identity or an empty requested-side slice; it must not
normalize one side's MRS2 fields as the other side or call the other side a
duplicate.

## Window contract

- The package window is UTC `[start, end)` and must be within the source coverage.
- Listing dates without an explicit timezone mean `00:00:00 UTC`; report
  timestamps and listing dates are normalized to UTC before eligibility
  comparisons.
- CSV rows enter only when their start and end exactly equal the package window. Every rejection is recorded.
- DuckDB cycles enter only when both the opening and closing actions occur inside the window. An action at `end` is outside the window.

## DuckDB closed-cycle reconstruction

The materializer reads v4 `report_runs`, `point_configs` and compact `report_payloads` read-only. It supports the documented `zlib-columnar-json-v1` action codec; an unrecognised codec is a hard error.

For each report, actions are ordered by action timestamp and original row position. A cycle starts on `Action=opened` and ends on its matching `Action=closed`; it is keyed by report, symbol and position side. The event identifier is a stable hash of symbol, position side, report timeframe and the exact opening timestamp. A cycle with no matching close, an invalid order, an open before `start`, or a close on/after `end` is excluded, never partially counted.

The materializer produces point metrics plus audit rows with raw action count, reconstructed cycle count, included count and exclusion counts by reason. It also emits the distinct event-ID hash per point. Raw DuckDB records and source HTML are never changed.

## DuckDB metric materializer contract

Only a report whose time grid covers the complete requested window can
contribute a point. The materializer reconstructs the wallet and equity series
losslessly, takes the wallet snapshot immediately before `start` (or
reconstructs it from the first action when the grid starts at `start`) and the
last snapshot before `end`. It calculates `TotalPnL` and `TotalPnLPercent` from
their difference and that starting wallet.

For a bounded read of a large v4 database, the materializer reads report/grid
identity and decodes actions for every report first, so every report retains a
cycle/exclusion audit. It loads and decodes timestamp, equity and wallet series
only for a grid whose stored bounds cover the requested window, then validates
that decoded grid again before calculating metrics. This optimization cannot
turn a non-covering report into a point or omit its action audit.

`MaxDrawdown` and `MaxDrawdownPercent` are the greatest fall of the in-window
equity series from its preceding in-window peak. A realised trade action is an
`Action=closed` or `Action=decreased` action belonging to a cycle which is
fully inside the window. `TotalTrades` is its count; `Win`, `Los` and
`WinRate` use respectively positive and negative realised `PnL` values; zero
`PnL` actions remain in `TotalTrades` and are recorded separately as
`flat_trades`. `ProfitFactor` is positive realised `PnL` divided by the
absolute negative realised `PnL`; a zero gross loss is reported explicitly,
never as an implicit infinity.

## Verification horizons and package versions

The source HTML summary is a complete-report-horizon document. It is not a
summary of the selected package window and must never be compared directly to
`[start, end)` metrics as proof that those window values are equal.

A `real_independent_events` package uses **format v2** and has two explicit
statuses:

- `source_summary_status=VERIFIED` only after 3–5 source HTML samples match
  independently reconstructed metrics over each sample's **full report
  horizon**. Missing HTML, an invalid count, parse error or mismatch is
  fail-closed. This is a sample-based source-integrity gate; it does not claim
  that every immutable input record has an individually compared HTML summary.
- `window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE` only after the selected
  `[start, end)` points and event mapping have been calculated from all
  immutable v4 source records in the package after the source-integrity gate
  passed. This status proves derivation and provenance; it does not assert
  equality with the HTML summary.

The manifest records both statuses, causes and sample evidence; every v2 point
records its `window_metrics_status`. A real package is selectable only when
the manifest's `source_summary_status=VERIFIED` **and** its
`window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE`, and all point rows carry
the latter status. Real package v1 is audit-only and is rejected by the
selector because it cannot state this two-horizon contract.

The exact v2 source-summary metric contract is defined by
[ADR-0003](../decisions/0003-source-integrity-action-metrics.md): only the
action-derived `TotalTrades`, `WinRate` and `ProfitFactor` are equality-gate
metrics. PnL/DD values remain mandatory audit diagnostics with
`NOT_COMPARABLE_WINDOW_SCOPE`; they cannot cause or satisfy `VERIFIED`.

HTML parsing keeps absolute and percentage values separate: `Total PnL` is
absolute `TotalPnL`, while `Total PnL, %` is `TotalPnLPercent`; `Max Drawdown`
is absolute `MaxDrawdown`, while `Max Drawdown, %` is
`MaxDrawdownPercent`. Full-horizon source integrity requires only
`TotalTrades`, `WinRate` and `ProfitFactor`, with the source's rounding.
Absolute PnL/DD remain audited as `NOT_COMPARABLE_WINDOW_SCOPE`, not equality
evidence. A `%` label must never satisfy an absolute-metric lookup, or
conversely.

`legacy_trades_proxy` package v1 remains valid under its existing contract:
one legacy mode, an exact window, `point_event_count=TotalTrades`, and the
`LEGACY_PROXY_NO_EVENT_IDS` sentinel. Legacy v1 and real v2 data never mix.
The HTML root is an explicit local input, never a tracked path; without
full-horizon source-summary evidence a real package may be materialized for
audit but cannot be selected.

## Selector contract

- Attach `event_mode`, `point_event_count` and `event_ids_hash` to every normalized point. `legacy_trades_proxy` uses the explicit `LEGACY_PROXY_NO_EVENT_IDS` sentinel, because it has no independent-event identity.
- Apply the existing `PointEventCount >= 3` requirement from the event-filter specification.
- Rebuild plateaus, representatives, structures and JSON from the filtered points; do not filter an old universe.
- Before/after counts and `Point_Events` / `Plateau_Events` appear in the audit workbook and manifest.

## Non-goals

- Combining CSV trades and DuckDB events in one run.
- Treating a source metric as real MRS3 performance.
- Fuzzy similarity, Top-N or a third candidate-reduction stage.

## Acceptance evidence

- Tests cover exact-window CSV acceptance/rejection, event-mode mixing rejection, closed/open/incomplete DuckDB cycles and deterministic event IDs.
- Tests prove that a full-horizon HTML summary cannot be used as a window
  equality check, that v2 selector loading requires the conjunction of both
  statuses, and that legacy v1 proxy loading remains valid.
- Tests distinguish absolute from percentage PnL/DD source-summary labels.
- A package manifest contains source hashes, window, event mode, counts,
  exclusions and the two-horizon verification evidence.
- A real database read is read-only and produces an audit; no source reports, HTML or raw payloads are modified.
