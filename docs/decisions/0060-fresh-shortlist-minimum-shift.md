# ADR-0060: Minimum Shift gate for fresh shortlist

Date: 2026-10-08

Status: Accepted for implementation after independent Advisor `PLAN_APPROVED`.

## Decision

The Panel Shortlist and READY JSON screen gets an optional Minimum Shift gate.
It checks only the first order Shift: the sole order for 1ORD, and the opening
order for 2ORD/3ORD; the existing strictly increasing Shift rule remains in
force. It is evaluated after persisted READY, PRETEST A/B, and
opening-MA ladder checks, and before the existing Pareto stage. The gate is
scoped to fresh shortlist evaluation and its derived audit, READY JSON, and
RUNS artifacts.

The established three-boolean filter-flag tuple is preserved for compatibility.
Minimum Shift is named configuration with canonical percentage serialization and
deterministic Decimal basis-point comparison. Disabled evaluations retain the
engine-1 token/provenance shape; enabled evaluations use engine-2. Existing
engine-1 artifacts remain readable; engine-2 is the explicit capability marker
for the new field.

Requests that include a threshold must include the explicit enabled flag. The
browser validates the same finite `(0, 100]` and three-decimal contract before
submission; the server remains authoritative. A disabled request keeps the
legacy three-key applied snapshot even if the local input retains a value.
Run publication now accepts the legacy three-key shape only with the explicit
engine-1 marker and the enabled shape only with engine-2; this makes malformed
or mixed provenance fail closed while preserving valid historical engine-1
artifacts.

This decision does not change Performance v2, PerformanceDB, source schemas,
old batches, tester runtime, or any database/cache migration behavior.

## Consequences

The user can reject weak fresh candidates before Pareto and carry the exact
threshold into generated artifacts. A server reload and browser reload are
required after deployment. Existing historical artifacts need no rewrite.
