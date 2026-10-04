# ADR-0056: Independent equity rejection sources

Date: 2026-10-04. Status: design accepted; local implementation verified and independently reviewed; live migration pending.

## Context

ADR-0055 requires an enabled equity filter to publish the existing `User Status=REJECTED` after a hard refusal. A single `strategy_tags.REJECTED` row cannot represent both manual review and automatic equity evidence: review import can remove that tag, and a later report can replace the current result. The new classifier also needs a versioned assessment in each published selection snapshot.

## Decision

- Schema v9 adds a nullable `selection_results.equity_regime_json` and a separate `strategy_rejection_sources` table. Its key is `(strategy_id, source_kind, reason_code)`; each row keeps the first result, selection run, classifier version, source revision, facts digest and creation time. Existing rows receive no synthetic assessment or rejection source.
- A run with an enabled equity consumer stores `equity_regime_snapshot` in `selection_runs.request_json`: classifier version and each strategy's result ID and source revision. A reader uses the newest published row for the current result, including a NULL assessment, and displays a non-NULL assessment only while its saved source revision matches the current source. A newer run with both equity consumers disabled therefore hides an older regime snapshot without deleting historical evidence.
- `persist_selection_snapshots` is the only writer of equity rejection sources. Before writing, it verifies the current strategy/result mapping and source revision/digest. It inserts the selection snapshot and all applicable hard reasons in one transaction. A repeated reason preserves its first evidence with `ON CONFLICT DO NOTHING`.
- Published equity rejection is sticky across later results, PASS, technical assessment errors and disabled equity filters. The effective `User Status` is `REJECTED` while either a manual rejection tag or any independent automatic rejection source applies. Clearing a manual tag cannot clear an equity source. A separate explicit manual operation would be needed to lift it; no such operation is part of this change.
- A read-only Preview may calculate and display projected reasons but never writes any table, including the cache. A separate explicit warm action may upsert the versioned cache. Rank-only exclusions, `NOT_EVALUATED`, and `NOT_EVALUATED / UNCLASSIFIED_GEOMETRY` never write a rejection source, even with the filter enabled.
- The existing `selection_results.auto_status` values remain unchanged. Rank-only exclusions use `FILTERED` with the distinct reason `EQUITY_RANK_UNRANKABLE`; this is not an equity-filter decision or a `User Status=REJECTED` event.
- The source table accepts `source_kind=EQUITY_REGIME_FILTER` and only the three agreed hard-reason codes. No automatic clearing operation is provided. A mistakenly published source requires a separately authorized, audited data-repair operation; later PASS or manual-tag removal does not serve as an override.

## Consequences

The selection, review, Panel and Excel readers must use the effective User Status union. Tests must cover independent manual and equity sources, repeated publication, later report changes, stale evidence rollback and all filter/rank toggle combinations. The v8 to v9 migration is additive and transactional; a verified backup and copy-only smoke test precede live migration. No automatic cleanup, retest timer, new `UNRANKED` or `REJECT60` status is introduced.

The numerical classifier remains defined by the [active status map](../specs/2026-10-03-equity-regime-status-map.md). The cleanup policy remains [ADR-0055](0055-equity-filter-rejected-and-manual-fact-cleanup.md).
