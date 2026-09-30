# ADR-0049: Optional commission-rate evidence in Performance v2

**Status:** Accepted for implementation, 2026-09-30.

## Context

The immutable inbox historically required five tester commission settings.
Performance v2 stores only `TakerFee` as `commission_rate`; HTML actions and
metrics already provide the actual fees and PnL. Some completed tester configs
have no complete commission block, so their otherwise valid reports cannot be
captured or imported. HTML fees cannot establish a reliable configured rate.

## Decision

For active Performance v2 `SINGLE_MODE` and its collections, the tester config
hash remains mandatory, but commission contract and ID become an optional
matched pair. A present canonical finite `TakerFee` is verified; absent rate
is SQL `NULL`, including on REPLACE. Schema v7 makes `commission_rate`
nullable through a transactional DuckDB v6 migration. Actual action fees,
total fees, balances and PnL continue to come from HTML.

The v6 result index and foreign-key children prevent a direct DuckDB
`DROP NOT NULL`. Migration reconstructs the validated parent and its four
children in one transaction, preserving rows and indexes before advancing
the version marker.

This supersedes the mandatory-five-field clause of
[the flat-config decision](0004-flat-tester-config-compatibility.md) and the
commission-evidence clause of
[the original evidence-store decision](0004-strategy-performance-evidence-store.md)
only for Performance v2 `SINGLE_MODE` and collections. Legacy v1 and FAST/RUNS
keep those contracts. The [new specification](../specs/2026-09-30-performance-v2-optional-commission-evidence.md)
owns this exception; the [heavy-optimization specification](../specs/2026-09-28-heavy-database-optimization.md)
continues to own its other importer changes.

## Consequences

Unknown configured rates are visible as unknown, while financial results stay
unchanged. Existing v5/v6 read-only catalogs remain supported; writable v5/v6
catalogs upgrade to v7, and older v6-only code cannot open v7. There is no
automatic downgrade. Any production rollout requires a verified backup and
copy rehearsal under a separate authorization.
