# ADR-0040: Source v6 read-time identity comes from the stored bytes

**Status:** Accepted

## Decision

`decode_fragment` derives `fragment_id` as `sha256` of the decompressed stored
payload and no longer re-serializes the decoded document to re-derive it.

Canonical form stays a write-time invariant, established where the bytes are
serialized:

- Payload bytes are only ever produced by serializing through
  `canonical_fragment_bytes`. Two encoders call it: `encode_fragment`, which
  also refuses a fragment whose declared id does not match those bytes, and
  `normalize_and_encode_source_v6`, which the live import path uses and which
  derives the bytes and the id from that one serialization.
- `import_fragment` and `import_fragment_batch` accept an
  `EncodedSourceV6Fragment` from their caller rather than serializing it here,
  so they prove the property through `_assert_canonical_encoding`. Neither has a
  caller in `src`, so this costs the live import nothing.
- Every import then verifies `sha256(zlib.decompress(payload_blob)) ==
  fragment_id` for every published fragment in `_verify_fragment_payloads`,
  before any HTML may be declared `safe_to_delete`.

### What these checks do and do not establish

Canonical form is a reproducibility property, not an authenticity one. It makes
the same logical fragment yield the same `fragment_id`, which is what lets
dedup, `source_content_digest` and surface fingerprints agree across machines
and runs. It says nothing about where a fragment came from.

That distinction bounds what verification here could achieve, and it is why the
segment writer was deliberately left alone. `encode_fragment` canonicalizes
whatever fragment it is handed, so anything able to supply prepared rows or
segment paths can publish fabricated content whose bytes are perfectly
canonical. Measured on this repository while deciding this: a fragment whose
point identity was rewritten to `TOTALLYFAKEUSDT` sealed and published cleanly.
Re-deriving canonical form at the segment boundary would therefore decode the
whole corpus on the live import path and still not establish provenance.

This system takes exactly two kinds of input: tester HTML reports, which the
importer normalizes itself, and a finished Source v6 database handed to the
materializer. Neither carries foreign payload bytes into a segment, so the
property is kept where it is free — at serialization — and audited on demand
everywhere else.

Readers keep the checks that bind bytes to identity: the compact blob checksum,
the stored `fragment_id`, and, at publication, W6's
`sha256(zlib.decompress(blob))`.

The removed check is preserved by name, not deleted: `decode_fragment(...,
strict_canonical=True)` re-serializes the document and refuses bytes that are
not the canonical form, for audits that must prove the property rather than
rely on the writer.

## Consequences

- A non-canonical payload is no longer refused on read; it decodes with the
  identity its bytes carry. The same logical fragment in two byte forms would
  then hold two ids. That is a write-path defect: it can only arise from a
  change to this project's own encoders, and `strict_canonical=True` is how it
  is found.
- Every artifact identity is unchanged, because canonical stored bytes hash to
  the same id under either derivation. Measured on the 27,360-fragment local
  Source DB: 24,790 fragments (90.6%) re-serialized to exactly their stored
  bytes, none differed, and every analysis row of a 60-point real sample is
  byte-identical to the previous implementation.
- No fingerprint, schema version or rebuild is required: this is not a
  derivative-rule change, and no published value changes.
- ADR-0017 is not weakened. It requires that W6 keep both the blob checksum and
  the decompressed canonical-id validation; both remain exactly as written, and
  this decision changes only where canonical form is proven.

## Related

[ADR-0017](0017-source-v6-facts-and-metrics-v2.md),
[materialization speed spec](../specs/2026-09-21-source-v6-materialization-speed.md).
