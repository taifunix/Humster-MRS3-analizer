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

## Window contract

- The package window is UTC `[start, end)` and must be within the source coverage.
- CSV rows enter only when their start and end exactly equal the package window. Every rejection is recorded.
- DuckDB cycles enter only when both the opening and closing actions occur inside the window. An action at `end` is outside the window.

## DuckDB closed-cycle reconstruction

The materializer reads v4 `report_runs`, `point_configs` and compact `report_payloads` read-only. It supports the documented `zlib-columnar-json-v1` action codec; an unrecognised codec is a hard error.

For each report, actions are ordered by action timestamp and original row position. A cycle starts on `Action=opened` and ends on its matching `Action=closed`; it is keyed by report, symbol and position side. The event identifier is a stable hash of symbol, position side, report timeframe and the exact opening timestamp. A cycle with no matching close, an invalid order, an open before `start`, or a close on/after `end` is excluded, never partially counted.

The materializer produces point metrics plus audit rows with raw action count, reconstructed cycle count, included count and exclusion counts by reason. It also emits the distinct event-ID hash per point. Raw DuckDB records and source HTML are never changed.

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
- A package manifest contains source hashes, window, event mode, counts and exclusions.
- A real database read is read-only and produces an audit; no source reports, HTML or raw payloads are modified.
