# ADR-0047: Fresh shortlist applied selection v2

Status: Accepted for implementation, 2026-09-27. The operator approved the
feature after independent `PLAN_APPROVED` for plan D3. Runtime acceptance still
requires tests and independent code review.

## Context

The fresh Panel applies four independent source Pareto switches immediately on
checkbox changes. List, audit and READY JSON generation can therefore observe
different switch values or candidate sets. Repeated full-artifact hashing,
per-scope reads and validation make the shortlist expensive. The pair/side
plateau total is absent from the displayed parent row.

The active [specification](../specs/2026-09-27-shortlist-filters-v2.md) and
[plan](../superpowers/plans/2026-09-27-shortlist-filters-v2.md) define the exact
filter metrics, ordering, compatibility matrix, errors and acceptance checks.
ADR-0046 belongs to the separate liquidity design.

## Decision

Fresh shortlist v2 has three optional switches: existing Source PRETEST A/B,
Open MA proximity to order 1, and one joint Pareto. The UI keeps draft and
applied settings separately. Only **Пересчитать фильтры** applies a draft;
table, audit and JSON use one server-verified applied result. Legacy non-fresh
filter behavior stays separate.

The server identifies an applied result with a digest of the complete analysis
artifact, analysis ID, engine version, canonical flags and sorted surviving
READY identities. Audit and generation require the matching token and resolve
the cohort server-side. This token detects a stale selection; it is not an
authentication credential. No browser candidate list is authoritative.

Within one controller, keep at most one compact validated analysis preparation
and eight compact option evaluations. Verify the artifact content on use;
stream hashes rather than allocate the file in memory. Bound retention,
concurrent work and temporary Pareto arrays as specified in the plan. Use the
existing shared worker setting only for independent sufficiently large groups.
Different analyses may evict one another; independent tab results stay correct.

The fresh request shape is versioned as `shortlist-v2`. Existing top-level
`pretest_ab_enabled` retains its spelling. Enabled old criteria fail explicitly
whether present inside `filters` or as top-level fields. Historical non-fresh
routes and old generated manifests remain readable.

## Consequences

No Source/Analysis/PerformanceDB schema change or re-analysis is required.
Generation provenance must record the filter version, flags, selection token
and selected identities. A changed artifact requires a new shortlist
evaluation; an audit/generation request with the old token fails without
publishing output. Zero surviving READY candidates remain auditable but cannot
produce an empty JSON batch. No extra cache database, dependency or worker
setting is introduced.
