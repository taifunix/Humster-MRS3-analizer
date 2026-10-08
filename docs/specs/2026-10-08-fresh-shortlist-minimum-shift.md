# Fresh Shortlist Minimum Shift Gate

Status: accepted for implementation after independent Advisor `PLAN_APPROVED`.

## Goal

Add an optional Minimum Shift gate to the Panel's **Shortlist and READY JSON**
flow. The gate applies only to a newly calculated fresh shortlist and to READY
JSON/run generation derived from that applied shortlist.

## Scope and non-goals

In scope:

- one checkbox and percentage input on the Shortlist screen;
- server-side validation, canonicalization, evaluation, audit, provenance,
  READY JSON, and RUNS propagation;
- a candidate is eligible only when its first persisted order Shift is at least
  the selected threshold. For 1ORD this is the only order; later orders in
  2ORD/3ORD are not restricted by this gate; their existing strictly
  increasing-Shift construction rule remains in force.

Out of scope:

- Performance v2/Pareto screen, PerformanceDB filters, source schema, old
  batches, tester runtime, database migrations, cache rebuilds, and ranking.

## Contract

The existing `FreshShortlistEvaluation.options` and all legacy option
arguments remain exactly `tuple[bool, bool, bool]`. Minimum Shift is carried by
named fields `min_shift_enabled` and `min_shift_pct`.

`min_shift_enabled` must be an exact boolean. The percentage is a finite
numeric value strictly greater than zero and no greater than 100, with at most
three decimal places. Boolean, invalid, zero, negative, over-100, and
over-precision values are rejected. Enabled values are canonicalized to a
three-decimal string (`0.3` becomes `0.300`). When disabled, the threshold is
ignored and canonicalizes to `null`.

If a request supplies `min_shift_pct`, it must also supply
`min_shift_enabled`; the server does not infer the checkbox state from a
threshold alone. A disabled request may carry a UI threshold value, but that
value is omitted from the applied snapshot.

The comparison uses one basis: `threshold_bp = Decimal(canonical_pct) * 100`,
and the first order Shift is parsed as `Decimal(str(value))`. Equality passes.
Missing or `null` first-order Shift values defer the candidate as
`ORDER_SHIFT_UNKNOWN`; malformed, boolean, and non-finite present values raise
a deterministic validation error. Normal fresh artifact validation still
requires valid order facts.

The stage order is:

1. persisted READY;
2. PRETEST A/B;
3. opening-MA ladder;
4. Minimum Shift;
5. existing Pareto.

When the first order is below the threshold, the candidate receives
`DEFERRED_MIN_SHIFT` with reason `ORDER_SHIFT_BELOW_MINIMUM` and is removed
before Pareto. Existing statuses and the disabled legacy filter-flag
serialization remain compatible.

## Applied state and provenance

The draft controls are local until **Recalculate filters** succeeds. The browser
uses the same numeric bounds and decimal precision before submitting an enabled
threshold, while the server remains authoritative. Shortlist,
audit, READY JSON, and RUNS use the applied snapshot and selection token. A
failed recalculation leaves the previous snapshot, token, and action state in
place and displays the server error. A stale token requires visible
recalculation. Enabled evaluations use `shortlist-v2-engine-2`; disabled
evaluations retain `shortlist-v2-engine-1` semantics and the legacy three-key
token/provenance. Historical engine-1 manifests remain readable and are not
rewritten. Enabled engine-2 provenance includes the canonical Minimum Shift
fields.

## Acceptance evidence

- engine-1 disabled baseline is captured and compared field-by-field;
- parser, threshold boundaries, deterministic tokens, missing-shift behavior,
  deferred status, Pareto exclusion, audit, READY/RUNS propagation, Panel API,
  and static UI tests pass;
- `node --check src/mrs3/panel_web/app.js` and `git diff --check` pass;
- independent implementation review returns `CODE_REVIEW_PASS`.

Deployment requires reloading the Panel server and the browser assets. No
database migration, cache rebuild, or tester run is part of this feature.
