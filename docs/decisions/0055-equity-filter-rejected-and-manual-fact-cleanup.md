# ADR-0055: Equity rejection uses existing User Status REJECTED

Date: 2026-10-04. Status: user decision recorded; implementation pending.

## Context

The proposed equity regime filter needs a durable way to identify strategies
rejected by its hard rules and later select unwanted rows for manual cleanup.
The Panel already has `User Status = REJECTED`; `REJECT60` was a draft name for
a 60-day retest cooldown. The user removed the retest and cooldown requirement.

The current Performance v2 prune deletes strategies and order settings. The
current `strategy_tags.REJECTED` row can record only one source per strategy,
while manual reviews and the hard cutoff filter also use that status.

## Decision

- An enabled equity filter that reaches a hard rejection returns DROP and
  immediately makes the effective `User Status` **REJECTED** in its durable
  selection result. A disabled filter creates no new equity rejection. No
  `REJECT60` value is added to `User Status`.
- Store the equity rejection reason, source result and classifier version
  separately from manual review history. Multiple concurrent rejection
  sources must not overwrite or clear one another. The effective User Status
  remains REJECTED while any durable rejection source applies.
- REJECTED does not schedule a retest, start a 60-day timer or automatically
  delete anything. The operator later chooses which rejected strategies to
  clean up.
- Manual cleanup removes only selected heavy facts. It retains the typed
  strategy settings, compact assessment and metrics needed for deduplication.
  A retained `cleanup_state` distinguishes present from deleted heavy facts;
  `deleted_at_utc` records the actual successful deletion time and is NULL
  beforehand. The importer must recognize retained keys and avoid recreating
  a cleaned strategy under another name.
- Distinct whole-strategy FLAT/COLLAPSING labels are not required for
  rejection; numerical reason codes preserve the decision evidence.

## Consequences

The current schema, effective User Status calculation, export, review import,
equity filter publication and importer need coordinated implementation and
verification. Current prune is unsuitable for this cleanup. This ADR does
not authorize an immediate database rewrite or deletion; no existing records
are marked by the research projection.

This decision narrows the draft REJECT60 cooldown in the active
[status map](../specs/2026-10-03-equity-regime-status-map.md) and does not
change the existing hard cutoff's [REJECTED provenance](0052-performance-v2-hard-cutoff-rejected.md).
