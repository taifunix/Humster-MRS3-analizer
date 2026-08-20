# ADR-0012: Source v6 Fresh Compact v1 Boundary

**Status:** Accepted; implementation complete, awaiting manual verification
**Date:** 2026-08-20
**Affects:**

- [Source v6 Fresh Compact v1](../specs/2026-08-20-source-v6-fresh-compact-v1.md)
- [Source v6 fresh compact multi-scope plan](../superpowers/plans/2026-08-20-source-v6-fresh-compact-multiscope.md)
- [ADR-0010: Source v6 stitched facts and surface files](0010-source-v6-stitched-facts-and-surface-files.md)
- [Source v6 and stitched surfaces](../specs/2026-08-18-source-v6-stitched-surfaces.md)
- [Source v6 analysis handoff](../specs/2026-08-19-source-v6-analysis-handoff.md)

## Context

The prior v6 contract describes normalized source facts and a proposed Task 8
replacement format. The next implementation plan requires a fresh-only compact
format whose fragments can be indexed, reconstructed and merged without storing
one physical row for every sample, action, cycle or event. It also requires
lineage that cannot be confused with older v6 artifacts and a hard gate before
multi-scope surface work begins.

The existing v6 stitching and boundary decisions remain useful analytical
rules. This ADR changes the source encoding and artifact boundary only; it does
not claim that the new format has been implemented or that real-corpus evidence
exists.

## Decision

### 1. Fresh-only format

`source-v6-fresh-compact-v1` is a new format. New readers reject old or unknown
Source, surface and analysis fingerprints. There is no v3/v4/v5 migration,
dual reader or mixed-format operation. Raw HTML is re-imported when a compact
artifact must be rebuilt; historical artifacts are retained as historical
evidence.

### 2. Compact indexed fragments

The Source artifact stores compact lossless fragments and an index sufficient to
locate, verify and reconstruct them. It does not store one row per sample,
action, cycle or event. Reconstruction must preserve those facts and exact
event membership; the absence of row-per-fact tables is not permission to lose
information.

### 3. Identity and lineage

Fragment identity is SHA-256 of uncompressed canonical JSON encoded with Decimal
values as strings, sorted keys and compact JSON. Compression is applied only
after identity and compressed bytes are never hashed for identity. A
deterministic sorted aggregate of fragment identities is published as
`source_content_digest`.

The format reserves distinct namespaces:

| Artifact | Fingerprint | Filename suffix | Path namespace |
| --- | --- | --- | --- |
| Source | `source-v6-fresh-compact-v1` | `.source-v6.duckdb` | `Output/source-v6-compact/` |
| Surface | `surface-v6-fresh-compact-v1` | `.surface-v6.duckdb` | `Output/surfaces-v6-compact/` |
| Analysis | `analysis-v6-fresh-compact-v1` | `.analysis-v6.duckdb` | `Output/analysis-v6-compact/` |

All three carry `source_content_digest`; descendants also carry the parent
artifact fingerprint needed for lineage validation. Existing path conventions
and physical container details are not migrated by this decision.

### 4. Audit and origin evidence

Import and merge outcomes retain audit evidence for accepted, inactive,
duplicate/idempotent, rejected and quarantined content, plus stitch decisions,
dispositions, failure reasons and flattened origins. Quarantine is fail-closed
and never publishes canonical facts. Exact field and table names are left to
implementation design.

### 5. Stage 2 boundary

No Stage 2 task may start until `progress.md` contains exactly one root-authored
line matching:

```text
STAGE_1_GATE=ACCEPTED_BY_ROOT; date=YYYY-MM-DD; reviewer=<reference>; evidence=<reference>
```

Every Stage 2 task runs `scripts/check-source-v6-stage1-gate.py` first. Missing,
malformed or duplicate tokens fail the gate; a failed gate permits only Stage 1
changes and requires a fresh complete-corpus rerun and review.

### 6. Minimum review disposition

Every implementation task must receive an independent `CODE_REVIEW_PASS`; an
executor report alone cannot accept it. The minimum retained safeguards are
canonical uncompressed hashes plus a sorted aggregate digest, canonical merge
ordering/deduplication with read-only inputs, staging/readback/atomic
publication, one injected-fault quarantine check, DB/WAL/raw-size/timing
measurements, deterministic scope ordering with read-only workers, and raw HTML
as the documented recovery input.

## Supersession

This ADR supersedes the storage portion of
[ADR-0010](0010-source-v6-stitched-facts-and-surface-files.md) for creation of
new compact Source/surface/analysis artifacts.

This ADR supersedes the proposed Task 8 replacement section in the 2026-08-19
handoff and the storage portion of ADR-0010 for creation of new compact Source,
surface and analysis artifacts. ADR-0010 and its specification remain historical
for already-published artifacts and retain their analytical stitching rules
where this ADR does not change them. No historical artifact is deleted or
migrated by this decision.

## Consequences

- New work has one unambiguous, fresh-only compact format and lineage boundary.
- Recompression and input traversal order cannot change fragment identity.
- Audit and quarantine remain visible without making a failed input canonical.
- Storage schema, migration, implementation and real-data acceptance remain
  separate tasks and cannot be inferred from this ADR.
