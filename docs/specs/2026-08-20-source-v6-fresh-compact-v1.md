# Source v6 Fresh Compact v1

**Contract ID:** `source-v6-fresh-compact-v1`
**Status:** Implemented; awaiting manual verification
**Date:** 2026-08-20
**Implementation plan:** [Source v6 fresh compact multi-scope plan](../superpowers/plans/2026-08-20-source-v6-fresh-compact-multiscope.md)
**Decision:** [ADR-0012](../decisions/0012-source-v6-fresh-compact-v1.md)
**Proposed boundary amendment:** [ADR-0013](../decisions/0013-source-v6-incomplete-seam-cycle-exclusion.md)
**Compact-publication amendment:** [ADR-0014](../decisions/0014-source-v6-compact-publication.md)

## 1. Purpose and precedence

This specification defines the fresh-only compact Source v6 format used by the
2026-08-20 implementation plan. Raw HTML is re-imported into this format; an
existing v3, v4 or v5 artifact is not an input to the format.

For new compact Source, surface and analysis artifacts, this specification
supersedes the proposed **Task 8** replacement section in the
[2026-08-19 Source v6 analysis handoff](2026-08-19-source-v6-analysis-handoff.md)
and the storage portion of the
[2026-08-18 Source v6 stitched-surface contract](2026-08-18-source-v6-stitched-surfaces.md).
The earlier documents and artifacts remain historical evidence; this change
does not delete, rewrite or migrate them. The accepted stitching and boundary
rules remain applicable where they are not replaced by this compact storage
boundary.

## 2. Scope and non-goals

In scope:

- a fresh compact Source artifact reconstructed from raw HTML;
- indexed, lossless source fragments and deterministic content identity;
- audit, quarantine, stitching decisions, dispositions and origin lineage;
- distinct Source, surface and analysis artifact namespaces;
- the hard boundary that prevents Stage 2 work before root acceptance of the
  Stage 1 evidence.

This specification does not define physical tables, a migration, a dual reader,
a new lease system, a dependency, or a claim about completed implementation or
real-corpus results. Portfolio simulation, tick-level MAE/MFE, exchange margin
and MRS3 performance claims remain outside this contract.

## 3. Fresh-only source boundary

The compact reader accepts only artifacts carrying the
`source-v6-fresh-compact-v1` format fingerprint. It fails closed for old
Source/surface/analysis fingerprints and for unrecognised compact versions.
There is no v3/v4/v5 migration, compatibility reader or mixed-format operation.
Raw HTML is the recovery input and is re-imported when a new compact artifact is
needed; historical artifacts are not a second recovery source.

## 4. Compact indexed fragments

The Source artifact contains compact encoded fragments plus an index sufficient
to locate, validate and reconstruct each fragment. An index entry identifies the
canonical fragment content, its point and validated UTC interval, and the
fragment's audit, disposition and origin references. The encoding is lossless:
readback reconstructs the same logical fragment independently of compression
level or input traversal order.

The format does **not** store one row per sample, action, cycle or event.
Samples, actions, cycles and independent events are reconstructed from the
canonical fragments. This is a storage-boundary rule, not permission to discard
facts or event membership.

Each fragment has one physical fact payload: its compressed canonical fragment
blob. A second serialized cache of samples, actions, cycles or events is
forbidden. Import and merge may write incrementally to a private staging DB,
but must repack that validated DB into a second private staging DB before the
atomic publish rename, then validate/read it back. This removes free pages from
the published artifact without changing canonical content.

## 5. Canonical identity and source digest

For every fragment, identity is the SHA-256 digest of the **uncompressed**
canonical JSON bytes:

1. Decimal values are rendered as strings;
2. object keys are sorted deterministically;
3. JSON uses compact separators and UTF-8 bytes;
4. the digest is computed before compression.

Compressed bytes, compression level and container offsets are never identity.
The same logical fragment therefore has the same identity after recompression.
The Source artifact also publishes a deterministic `source_content_digest`
formed from the sorted canonical fragment identities. It is independent of raw
HTML paths, filenames, row order, compression and physical container bytes.

## 6. Audit, quarantine, stitch decisions and origins

Every discovered input and every attempted fragment has an auditable outcome.
The outcome records the relevant source identity, parse/validation result,
stitch decision or failure reason, disposition and origin references. At
minimum, the published audit distinguishes accepted/active content,
accepted-but-inactive content, duplicate/idempotent content, rejected content
and quarantined content; exact enum names are an implementation detail.

Quarantine is fail-closed: quarantined or corrupt content is not published as
canonical facts, and its reason remains in audit evidence. Stitch decisions
retain the compatibility, overlap/bridge and ownership decision that led to the
disposition. Origins are flattened through import and merge so a published
fragment remains traceable to its raw input identity without requiring the raw
path at read time.

The existing fixed-lot compatibility, overlap, bridge-cycle, exact timestamp
ownership and selected-interval boundary rules remain the analytical source
rules. This specification changes how their evidence is encoded, not what a
successful decision means.

### 6.1 Implemented old-owned overlap amendment

[ADR-0013](../decisions/0013-source-v6-incomplete-seam-cycle-exclusion.md)
defines a focused change to current bridge ownership. For a fixed-lot-compatible
incoming fragment with at least 96 hours of overlap, the old fragment owns the
whole overlap. Incoming cycles closed before exact `old.report_end` are
excluded; an incoming cycle opened before that boundary and closed at or after
it is retained wholly in the new period, as are cycles opened later. Each
period has its own balance anchor, absolute PnL and PnL percentage. Later
incoming balance/equity samples remove the known effects of excluded closed
overlap cycles, so an excluded PnL cannot return through a balance difference.
Relative drawdown uses each period's own corrected equity and its own peak;
the stitched relative value is the maximum period drawdown, never an old
absolute drawdown divided by a new-period equity value.
The fragment records `USE_OLD_WITH_SEAM_EXCLUSION` and
`INCOMPLETE_SEAM_CYCLE_EXCLUDED` diagnostics with cycle, anchor and sample
correction evidence.

Malformed, incompatible, short-overlap, gapped or ambiguous inputs remain
rejected or inactive under their existing fail-closed diagnostics and do not
receive this exception. ADR-0013 is implemented and independently reviewed;
historical artifacts retain their original `BRIDGE_NOT_COVERED` diagnostics.

## 7. Artifact namespaces and lineage

Fresh compact artifacts use distinct versioned fingerprints, suffixes and path
namespaces. The names below are the contract-level namespaces; their internal
container layout is not specified here.

| Artifact | Fingerprint | Filename suffix | Path namespace |
| --- | --- | --- | --- |
| Source | `source-v6-fresh-compact-v1` | `.source-v6.duckdb` | `Output/source-v6-compact/` |
| Surface | `surface-v6-fresh-compact-v1` | `.surface-v6.duckdb` | `Output/surfaces-v6-compact/` |
| Analysis | `analysis-v6-fresh-compact-v1` | `.analysis-v6.duckdb` | `Output/analysis-v6-compact/` |

Every Source, surface and analysis artifact carries `source_content_digest`.
Surface and analysis artifacts additionally retain the parent artifact
fingerprint needed to reject cross-format lineage. A changed source digest or
format fingerprint is a new artifact identity; it never mutates an existing
published artifact.

## 8. Stage 2 hard boundary

Stage 2 is forbidden until `progress.md` contains exactly one root-authored line
with this exact shape:

```text
STAGE_1_GATE=ACCEPTED_BY_ROOT; date=YYYY-MM-DD; reviewer=<reference>; evidence=<reference>
```

The token must occur once, must have a real ISO date and non-empty reviewer and
evidence references, and must be authored by the root agent. Every Stage 2 task
begins by running `scripts/check-source-v6-stage1-gate.py`; the checker rejects
an absent, malformed or duplicate token. A failed gate permits changes only to
Stage 1 and requires a fresh complete-corpus rerun and independent review.

No Stage 2 implementation, benchmark claim or completion status may be inferred
from this specification alone.

## 8.1 Stage 2 materialization contract

For each selected `symbol|side|timeframe`, materialization stores two separate
sets: the complete observed factual grid and one canonical READY witness. The
witness proves that the required 19-shift Г— 6-CloseMA contract has a continuous
interval; it never filters factual points, their metrics, events, rejections or
observed Shift/OpenMA/CloseMA geometry. A scope with facts but no READY witness
is rejected before surface publication.

Order-structure selection always receives the `AlgorithmConfig` loaded for that
analysis run. In particular, every adjacent order gap is checked against that
run's `gap_rules`; the canonical configuration hash is part of the analysis-run
identity, so changing the configuration produces a distinct rerun.

## 8.2 Stage 2 surface and analysis contracts

One selected set of READY scopes publishes one immutable fresh surface in
`Output/surfaces-v6-compact/`.  It retains the parent `source_content_digest`,
the source fingerprint, a digest of the selected factual payloads, and a
canonical digest per scope.  Publication uses staging, readback validation and
one atomic rename; a target is never altered.

Each selected scope is analysed independently from a read-only fresh-surface
connection.  The parent is the sole writer of the separate analysis artifact in
`Output/analysis-v6-compact/`; workers are capped by selected scope count.
Analysis identity binds the fresh-surface fingerprint/id, source and scope
digests, algorithm version, configuration hash, listing-date snapshot hash and
`real_independent_events`.  The panel submits selected scopes as a multi-select
and obtains the worker limit from `duckdb_import.workers`.

## 9. Minimum review disposition

Each implementation task requires an independent reviewer disposition of
`CODE_REVIEW_PASS` before it is accepted. The executor's report is not a
substitute for that disposition. The minimum retained safeguards are:

- canonical uncompressed fragment hashes and a sorted aggregate digest;
- canonical merge ordering, deduplication and read-only merge inputs;
- staging, readback and atomic publication;
- one injected-fault quarantine check;
- DB/WAL/raw-size and import-timing measurements without an arbitrary ratio;
- deterministic scope ordering and read-only surface workers;
- raw HTML documented as the recovery input, without another recovery source.

The minimum disposition does not require cryptographic authorship for the root
marker, a new PID/heartbeat/fencing lease subsystem, a fixed two-OS benchmark
matrix, fabricated `MISSING` fact rows or a Debian pytest environment.

## 10. Acceptance evidence for this contract

Task 1 acceptance is limited to this specification, [ADR-0012](../decisions/0012-source-v6-fresh-compact-v1.md),
the linked PRD/progress updates, valid Markdown links, `git diff --check`, and
an independent review. Import, codec, merge, real-corpus and Stage 2 evidence
belong to later plan tasks and are not claimed here.
