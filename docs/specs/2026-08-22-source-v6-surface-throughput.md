# Source v6 surface publication throughput — specification

**Status:** Implemented
**Date:** 2026-08-22
**Builds on:** [publication throughput](2026-08-21-source-v6-publication-throughput.md),
[ADR-0014](../decisions/0014-source-v6-compact-publication.md)

## Problem

Materialization is the last stage that still round-trips every payload through
Python. Measured on `my_test_CX_GE_fixed`, one READY scope,
`CXMTUSDT|LONG|15m`, 648 fragments and 43 MB of payload:

| Phase | Cost |
| --- | --- |
| metadata + readiness (whole 38,160-fragment corpus) | 4.9 s |
| hydration of the scope | 49.8 s |
| `materialize_source_v6` | 0.1 s |
| `publish_multiscope_surface` | **100.4 s** |
| resident memory | 42 MB → 2,450 MB |

That is 239 ms per fragment. Extrapolated to the whole corpus the surface build
is about 2.5 hours, and hydration alone would need roughly 74 GB resident — so
publishing the full corpus is not slow, it is impossible.

The preflight is not part of this: `preflight_source_v6` returns in 0.00 s and
reads no HTML, and `canonical_ready_intervals` costs 1.7 s over 38,160
fragments. Both already work from metadata alone.

Publication decomposes into three costs, each already solved elsewhere in this
repository and each measured here:

1. **The sealed payload is re-encoded.** `encode_fragment` rebuilds the
   canonical document and recompresses it at zlib level 9. Measured over 120
   fragments: **59 ms each**, and the result is byte-identical to what is
   already stored — payload 120/120, codec 120/120, `payload_sha256` 120/120.
   C1 removed exactly this round trip from the merge; the surface never got it.
2. **Rows are inserted one statement at a time.** Measured **736 rows/s**.
   Defect 7 measured the same pattern at 1,730 rows/s against 1,969,809 rows/s
   through a registered frame.
3. **The closing validation decodes everything again.**
   `_publish_multiscope_surface` ends by calling `read_multiscope_surface`,
   which reconstructs every fragment through `decode_fragment` to check its id
   and scope. Measured **48.1 s** — roughly half of publication.

So the corpus passes through the codec three times: hydration, re-encode, and
the validating re-decode.

## Goal

Publish a surface without re-encoding, without per-row statements, and without
a second full decode, at identical published content.

## Non-goals

- Changing what a surface contains or how consumers read it.
  `read_multiscope_surface` and `read_multiscope_scope` keep decoding, because
  their callers need the fragments.
- Changing readiness, scope selection or the preflight.
- Letting `materialize_source_v6` work from metadata. That is a separate change
  and is what removes the hydration cost; this one removes the publication cost.

## Contract changes

### S1 — Sealed payloads are copied, not rebuilt

`publish_multiscope_surface` accepts the source database the fragments came
from. When it is supplied, `payload_blob`, `codec` and `payload_sha256` are
copied from it by SQL — `ATTACH` plus one `INSERT ... SELECT` per scope — and
no fragment is encoded in Python. This is C5's mechanism applied to the
surface.

Correctness rests on the identity already proven above: the stored payload is
what `encode_fragment` would produce. Copying is therefore not an optimisation
that changes the artifact; it produces the same bytes with less work.

Without a source database the previous behaviour stands, so every existing
caller and test keeps working.

### S2 — Metadata rows are written as a set

Manifest, scope-manifest and fragment rows go through `_insert_frame` rather
than a statement per row, as C4 established.

### S3 — Publication validates identity, not structure

The publish-time check re-derives `sha256(zlib.decompress(payload_blob))` and
compares it to `fragment_id`, and checks the stored `payload_sha256` — the C3a
predicate — instead of reconstructing every fragment. It keeps the scope-purity
check by reading `point_key`, which the surface stores as a column.

This verifies strictly more than the old check about the bytes (the old one
never compared the stored `payload_sha256`), and strictly less about the
decoded object, which publication does not need to prove: a payload whose
sha256 is its `fragment_id` decodes to the fragment with that id or to nothing.

### S4 — Panel hydration is parallel but deterministic

The panel preflight remains metadata-only. Publication first selects only the
stored fragment ids whose scope is READY and explicitly requested, then decodes
that subset through `iter_fragment_ids_parallel`. It uses the configured import
worker limit, bounded to 16 processes for Windows stability. Each worker opens
the Source DB read-only and decodes a disjoint ordered slice; the parent
restores `fragment_id` order before materialization. Therefore worker count
changes only elapsed time, never the selected facts, source digest, scope digest
or surface identity.

The materializer deliberately remains hydrated: its `measure_points` call is
the E1 authority for distinguishing a genuine idle result from a selected window
that hides otherwise measurable data. The whole-source digest is supplied from
the validated Source DB metadata, so selecting a subset does not weaken lineage.

The reader reports completed and total *selected* hydrated fragments. The panel
may expose that count and the subsequent publisher's real write/readback counts;
short atomic operations are phase-only, never fabricated percentages.

## Invariants

- The published surface is byte-for-byte equivalent in content: same
  `surface_id`, same scope digests, same fragment ids, same payloads.
- A corrupt or mismatched payload still fails publication.
- Nothing is published unless validation passes; the staging file is renamed
  only at the end, as before.

## Acceptance evidence

- A surface published with the source-database path and one published without
  it agree on every table and on `surface_id`.
- Publication still fails on a payload that does not hash to its id.
- Measured before/after on the same real scope.

## Measured outcome

Same scope, same machine, against the 100.4 s baseline above:
**publication 100.4 s to 3.0 s, 33x.** The published `surface_id` is unchanged
(`264c1f21…`), and the two artifacts were compared directly: `manifest`,
`scope_manifests` and `factual_fragments` identical, and the payload bytes of
all 648 fragments identical.

The surface file is also 43% smaller — 45,101,056 bytes against 78,917,632 —
because the set-based insert packs DuckDB pages that a statement per row
fragmented. Content is identical; only the physical layout changed.

Publication is no longer the dominant cost. What remains is hydration, 49.8 s
for this scope, which this change does not touch: `materialize_source_v6` still
rejects metadata views, so the caller must decode every fragment even though
nothing on the publication path now needs a decoded object. That is the next
change, and it is what removes the memory ceiling as well.

## Verification

`.venv\Scripts\python.exe -m pytest -q`. No `Input/`, `Output/`, `Data/`, HTML,
DuckDB or generated artifact may be committed.
