# ADR-0015: The identity readback runs on the published file

**Status:** Accepted for implementation
**Date:** 2026-08-22
**Amends:** [ADR-0014](0014-source-v6-compact-publication.md)

## Context

ADR-0014 states that the repacked database "is validated and read back before
publication". The implementation read it back only partially. The C3a payload
readback — decompress `payload_blob`, re-derive `fragment_id` from the
decompressed bytes, and compare the canonical document against its columns —
ran on the writable `staging` file. `compact_v6_database` then rewrote those
bytes into `compacted`, and `compacted` is what the atomic rename publishes.

`compacted` was checked by `validate_source_v6_database` (schema version and
fingerprint), a per-table row count, an id-set and digest comparison, and —
through `fragment_metadata` — `sha256(header_json) == header_sha256` plus
header-against-column agreement for every fragment. Exactly one column escaped:
`payload_blob` was never decompressed, never re-derived and never re-hashed on
the file that survives the merge.

This matters because the readback is what `safe_to_delete=YES` rests on. That
verdict authorises deleting the raw HTML the payload was built from. The proof
was about a temporary file that is then discarded, not about the artifact the
operator keeps.

The gap was not closed earlier because it was unaffordable: the readback was
serial and cost about 1,150 s, so verifying both files would have doubled a
2,080 s merge. C9 made it parallel and it now costs 113.9 s, about 2% of the
merge.

## Decision

The C3a payload readback runs on the file that is published, not on the
intermediate one. In the merge that is `compacted`, after
`compact_v6_database` and before `compacted.replace(target)`.

**This ADR governs the merge only.** The import path has the same gap and is
not closed here: `_publish_segments_single_pass` verifies its reduce target and
then publishes a repack that receives neither the payload readback nor
`fragment_metadata`'s header pass, so it is currently weaker than the merge was.
Closing it is recorded as a follow-up rather than assumed by the wording above.

It moves rather than being added: `compacted` is derived from `staging`, so any
corruption in `staging` reaches `compacted`, and verifying the later file
catches strictly more than verifying the earlier one. Verifying both would pay
twice for a subset.

## Consequences

- `safe_to_delete=YES` now means the payload identity of the published artifact
  was re-derived from its own stored bytes, not from a discarded intermediate.
- The readback also now covers the state after stitching and the
  `fragment_origins` rewrite, which previously ran after it.
- A corrupt copy is detected later in the merge — after stitching and the
  repack rather than before them — because the file being checked exists only
  at that point. Nothing is published either way: the `finally` removes both
  staging files on any failure, and publication remains the single atomic
  rename.
- The fail-closed guarantee of ADR-0014 is unchanged. Committing is still not
  publishing.
