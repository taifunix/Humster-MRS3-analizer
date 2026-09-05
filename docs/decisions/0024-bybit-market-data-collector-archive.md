# ADR-0024: SQLite spool and marker-authoritative atomic Bybit archive

**Status:** Approved for implementation.

## Decision

The collector remains inside the existing `mrs3` distribution. Its only future
transport dependency is `aiohttp`; stdlib SQLite and installed DuckDB supply the
spool and Parquet writer/validator.

SQLite WAL with `synchronous=NORMAL` is durable truth. An hourly/daily derived
Parquet file is written beside its target as `.tmp`, structurally validated,
published with a platform no-clobber atomic primitive, and then entered in a
small SQLite `published_hours` reader index. The index, not a filesystem glob,
is reader authority. Rename-to-marker recovery validates an existing final;
marker-without-file recovery clears the marker and re-exports from spool.

One public linear WebSocket serves current 20–30 symbols. Connection splitting
is deferred until measured lag, dropped-frame diagnostics, or instability
justifies a separate decision.

## Consequences

Published files are immutable; archive, raw REST data, and spool are not
automatically deleted. Integrity is structural, not a cryptographic per-file
claim. Writer version, row-group, and compression-layout differences do not make
old files incompatible. A marker proves readable structure, not market coverage;
minute coverage fields retain that interpretation.

Revision 2 supersedes the earlier proposed manifest, per-file hash,
reconstruction, quarantine, late-row archive, archive-state-machine, and
max-ten multi-WebSocket designs. Retained SQLite rows plus a validated atomic
publication and one reader index provide restart safety with less machinery.

This decision authorizes neither trading/private API access nor portfolio,
capacity, lot, or MRS3-result conclusions.
