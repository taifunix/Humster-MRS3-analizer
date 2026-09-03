# ADR-0022: Performance v2 selection review ledger

**Status:** Accepted
**Date:** 2026-09-02

## Context

The current finalist XLSX is disposable. It cannot safely support later manual
status/rank edits, the A/B `Только финалисты` filter, analog relationships or a
durable rejection decision. Directly editing strategy rows would lose the
automatic result and its history; treating XLSX as the source of truth would
make identity and stale-data checks unreliable.

The operator also requires that `REJECTED` remain a non-destructive tag. Any
future deletion must be a separate reviewed workflow.

## Decision

Advance the internal Performance DB schema from 2 to 3 while retaining the
`unified_performance_v2` product identity. Store:

1. each downloaded selection as an immutable run plus immutable per-strategy
   automatic results;
2. every successful XLSX review as an append-only import plus per-row user
   decisions;
3. only the current `REJECTED` tag in a small derived tag table.

The existing XLSX button is the explicit save boundary. Counter previews remain
read-only. XLSX carries a hidden run/database identity and is accepted only for
the latest run of the same Pair + Side with unchanged current result IDs.
Export and import writes are serialised and revalidate those conditions inside
their DuckDB transaction.

Automatic and user decisions remain separate. The latest accepted user review
of the latest run defines effective status; otherwise the run's automatic
status applies.

Analog groups use the exact key Pair + Side + timeframe + order count + Close
MA. One weighted representative proceeds to final Top N; surviving alternatives
are linked to it as `ANALOG`.

`REJECTED` combines all user meanings of “rejected” and “trash”. It never
changes lifecycle state and never deletes data.
New runs preserve this durable decision, exclude prior-rejected strategies from
Top-N slots, and prefill their review status as `REJECTED`.
“Durable” does not mean immutable: a later successful latest-run review may
explicitly change the status and remove the current tag, while append-only
review history and underlying performance facts remain intact.

This ADR supersedes ADR-0021 only where ADR-0021 deferred persistence on XLSX
export and review import. Its immutable-snapshot and latest-snapshot principles
remain in force.

## Consequences

- Automatic decisions are reproducible and cannot be overwritten by manual
  review.
- Reviewed XLSX can be imported safely without using names as identity.
- A/B finalist filtering has one deterministic source.
- Re-exporting creates a new current run; an older workbook cannot overwrite it.
- Schema migration and import need strict transactional tests.
- No generic tag framework, sidecar database, deletion machinery or background
  job is added.

## Rejected alternatives

- **Write status directly to `strategies`:** loses run/config context and review
  history.
- **Use XLSX as the only record:** cannot safely detect foreign or stale data.
- **Store reviews in a second database:** creates unnecessary coordination and
  backup problems.
- **Automatically delete `REJECTED`:** destructive and explicitly outside the
  approved workflow.
