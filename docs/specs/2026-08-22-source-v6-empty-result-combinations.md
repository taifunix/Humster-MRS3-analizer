# Source v6 empty result combinations — specification

**Status:** Implemented
**Date:** 2026-08-22
**Depends on:** [zero-activity runs](2026-08-22-source-v6-zero-activity-runs.md),
[ADR-0016](../decisions/0016-source-v6-zero-activity-runs.md),
[ADR-0006](../decisions/0006-undefined-profit-factor-contract.md)

## Problem

A "point" is a parameter combination — shift, open MA and close MA over one
symbol, side and timeframe. Since ADR-0016 a combination can be tested and
produce no trades at all, and such a run is now imported instead of quarantined.

`calculate_metrics` raises `wallet/equity series are required` when a
combination has no samples, and `_surface_payload` calls it in a bare loop over
every combination. One combination with nothing to report therefore aborts the
whole surface build. Demonstrated with ten healthy combinations plus one empty
one:

```
10 healthy combinations                    -> published OK, 10 points
the same 10 + 1 combination with no trades -> SourceV6StitchError
```

All eleven are lost, including the ten that are fine. This is not a property of
the empty combination; it is the absence of any per-combination isolation.

The path was unreachable before ADR-0016, because a report with no trades was
quarantined and never reached metric calculation.

## Goal

A combination that produced no trades must not abort the build, must not be
given invented metrics, and must not silently disappear.

## Non-goals

- Changing metrics for any combination that does have trades.
- Changing readiness, coverage or `day_ownership`. The source database already
  records the tested window as `ACTIVE_EMPTY`, so the coverage fact survives
  regardless of what the surface does.
- Reporting empty results through the analysis pipeline as candidates.

## Contract

### E1 — An empty result is defined by the calculation, not by a predicate

A combination is an *empty result* when `calculate_metrics` cannot measure it.
That is decided by attempting the calculation and catching
`SourceV6EmptySeriesError`, not by a predicate mirroring it.

Two earlier revisions of this section tried to state the condition as a
predicate over samples, and both were wrong in the same way. `calculate_metrics`
merges the series, filters them for visibility, drops samples at or after an
open tail, drops a later fragment's pre-boundary samples, may truncate a whole
fragment on `BRIDGE_NOT_COVERED`, applies the selected window — and only then
checks that anything remains. A predicate has to track all of that. The second
attempt, "has a raw sample inside the window", was reproduced failing through
the public entry point: a combination holding exactly one raw sample in the
window still aborted the whole build, because that sample sat at the open tail.

So measurability is decided by running the calculation, and
`SourceV6EmptySeriesError` exists so that answer can be caught precisely rather
than by matching a message. `measure_points` returns the metrics it computed, so
deciding measurability and measuring are one pass — the caller pays for one
calculation per combination where it previously paid for two.

Trade facts are not part of the test: a combination with samples but no cycles
still has an equity curve and therefore real metrics.

### E2 — A combination that never traded keeps its cell and carries the flat result

It stays in `points`, in `point_facts` and in `point_metrics`, its fragments
stay in `fragment_ids`, and its metrics are `flat_result_metrics()`: total PnL
zero, gross profit and loss zero, drawdown zero, no trades, and every ratio
`None`.

Nothing is invented here. The tester itself declares those values, and Z1 —
which is what admits the report at all — already verified they agree with each
other before import. What is genuinely undefined is the ratio set, and ADR-0006
settled that: `n/a` stays undefined rather than becoming a number.

**This reverses an earlier revision of this spec, which excluded the cell.**
Exclusion published a 113-of-114 grid, and `load_source_v6_pipeline_input`
rejects any grid that is not the complete canonical 6 CloseMA × 19 Shift. So a
loud, precise publish-time failure became a quiet artifact that died one stage
later with `INCOMPLETE_GRID`, naming neither the reason nor the cell. Exclusion
also changed the meaning of the grid: a cell absent because it was never tested
and a cell absent because it was tested and did nothing became
indistinguishable.

### E2a — Why the flat result is safe to publish

The objection to zeroing was that `build_persisted_analysis_facts` reads metrics
with `metrics.get(key, {})` and defaults `TotalPnLPercent` and
`MaxDrawdownPercent` to `0`, so a zeroed cell would look like a 0% return at 0%
drawdown — an infinite risk-adjusted result that never happened.

It cannot reach that. `annotate_eligibility` runs *before* `build_plateaus` and
rejects a non-positive PnL (`REJECT_PNL_NONPOSITIVE`) and a non-positive
drawdown (`REJECT_DD_NONPOSITIVE`). A never-traded cell fails both, on the
default `economic_min_pnl_pct` of 0 and on every positive setting. Verified: the
cell is present in the facts, carries both rejection reasons, has
`plateau_id: None` and `role: "UNASSIGNED"`.

So the cell is visible and can never be selected — which is exactly what a
tested-and-idle parameter combination should be.

### E3 — Empty results are recorded

`empty_result_points` lists every such combination with its `point_key`, its
fragment ids and the reason `NO_WALLET_OR_EQUITY_SAMPLES`, sorted by
`point_key`. It is part of the surface payload and therefore of
`manifest_sha256`, so the fact is discoverable from the published artifact
rather than only from the build log.

The combination is not lost anywhere else either: the source database records
its tested days as `ACTIVE_EMPTY` under Z4.

### E4 — A window that hides a combination's data is an error, not a zero

If a combination is measurable across its whole report but has nothing inside
the *selected* window, publication raises `SourceV6EmptySeriesError` and the
message names the combination.

This is a different fact from E2 and must not be flattened into it. There, the
run genuinely produced nothing; here, the run produced something and the
requested window cannot see it. Publishing zeros for that would state a result
the tester never reported. Which case applies is decided by re-running the
calculation without the window, not by whether a window was supplied — the
multiscope path always supplies one, so keying on that mislabelled every
genuinely empty combination as a windowing artefact.

### E4a — The multiscope path measures over its own witness

`publish_multiscope_surface` stores facts, not metrics, so it published such a
combination happily and `run_multiscope_analysis` aborted afterwards with the
same `wallet/equity series are required`, in the same shape of unguarded loop.
That is the path the panel takes.

`materialize_source_v6` therefore measures each scope over that scope's READY
witness — the window `run_multiscope_analysis` will itself measure over — keeps
every fact, and records the empty results on `MaterializedSourceV6`.
`publish_multiscope_surface` writes them into the manifest under
`empty_result_points`.

This does not disturb `surface_id`, which is a digest over the source content
digest and the scope digests, and the scopes hold exactly the facts they held
before. `source_content_digest` likewise stays over the whole input: it is the
lineage of what was materialized *from*.

Empty results are recorded per requested scope only. Partitioning the whole
input would have written other symbols' combinations into a single-scope
surface's manifest.

### E5 — Top-level metrics come from the same pass

`_surface_payload` summarises the first combination by key order, taking the
metrics `measure_points` already computed. There is no separate
`_surface_metrics` pass any more; keeping one meant measuring every combination
twice.

## Invariants

- A surface built from fragments that contain no empty results keeps its
  `surface_id` and `frozen_facts_sha256`. `manifest_sha256` does change, because
  the payload now carries an `empty_result_points: []` key and that hash covers
  the payload. Published artifacts still self-validate, since the hash is
  recomputed over what they store.
- No metric value is ever synthesised. The flat result is the tester's own
  declared result, and undefined ratios stay `None`.
- The canonical grid published for a scope is complete whenever the scope was
  fully tested, whatever each combination returned.
- An empty result is discoverable from the published surface.

## Acceptance evidence

- Ten healthy combinations plus one empty one publish, and the surface holds
  eleven metric rows and one `empty_result_points` entry.
- The never-traded combination's row reports `TotalTrades` 0, PnL 0, drawdown 0
  and `ProfitFactor` `None`.
- A full canonical grid with one idle combination publishes all 114 cells.
- That combination carries `REJECT_PNL_NONPOSITIVE` and `REJECT_DD_NONPOSITIVE`,
  belongs to no plateau, and is `UNASSIGNED`.
- A window that hides a measurable combination raises, naming the combination.
- A surface with no empty results keeps its previous `surface_id` and
  `frozen_facts_sha256`, pinned against the values the previous module produced.
- Both single-surface entry points behave alike, `publish_surface_db` included —
  it is the one `scripts/import_source_v6_debian.py` uses.
- On the multiscope path the grid stays whole, the manifest records the empty
  result, and `run_multiscope_analysis` completes.
- The materializer measures against the scope's READY witness window.

## Measured outcome

The failure this removes, before and after, on the same inputs:

```
before:  10 healthy combinations              -> published OK
         the same 10 + 1 with no trades       -> SourceV6StitchError
after:   the same 10 + 1 with no trades       -> published, 11 metric rows,
                                                1 empty_result_points entry

before:  full 114-cell grid, 1 idle cell      -> run_multiscope_analysis raised
after:   full 114-cell grid, 1 idle cell      -> 114 cells published, analysis
                                                completes, the idle cell is
                                                rejected by eligibility
```

## Verification

`.venv\Scripts\python.exe -m pytest -q`. No `Input/`, `Output/`, `Data/`, HTML,
DuckDB or generated artifact may be committed.
