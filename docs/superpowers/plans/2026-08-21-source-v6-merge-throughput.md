# Source v6 merge throughput — implementation plan

**Status:** Planned — not started
**Depends on:** [publication throughput spec](../../specs/2026-08-21-source-v6-publication-throughput.md)
(implemented and measured; this plan applies the same four techniques to the
merge runner)

## Why

`merge_source_v6` carries every defect the importer had before the publication
throughput work, and none of the fixes. Measured on the two real corpora
(`source-v6-folder1.duckdb`, 5,859 fragments; `source-v6-folder3.duckdb`,
59,675 fragments; 65,534 total) on a 185 GB / 32-core Windows host:

- resident memory grew **1.5 GB/min** on a single core at 100%;
- after 3 minutes it held 6.6 GB and was still reading the **first** input;
- extrapolated peak exceeds total RAM, so the run cannot complete.

The run was aborted before exhausting memory. Both inputs are intact.

## Diagnosis

| Defect | Location | Same as importer defect |
| --- | --- | --- |
| Decodes every fragment of every input into memory | `_read_input` → `tuple(iter_fragments(path))` | tail decode |
| Holds all fragments twice (`fragments_by_id` plus `unique`) | `merge_source_v6` | tail decode |
| Publishes one fragment at a time through `import_fragment`, one DuckDB connection each | `merge_source_v6` | per-fragment publication |
| Recomputes ownership over all fragments in memory | `merge_source_v6` | stitch hydration |

The digest cross-check in `_read_input` needs only `fragment_id`, and ownership
resolution needs only the metadata fields `resolve_batch` reads — neither
requires a decoded payload.

## Approach — the four techniques already proven in the importer

1. **Metadata instead of fragments.** Replace `tuple(iter_fragments(path))` with
   `fragment_metadata(path)`. Digest verification, duplicate resolution and
   `resolve_batch` all run on metadata. Decode only a point group that has more
   than one fragment, exactly as `import_source_v6` does, because only those
   produce a persisted seam decision.
2. **Copy payloads inside DuckDB.** Replace the per-fragment `import_fragment`
   loop with an `ATTACH` + set-based `INSERT ... SELECT` from each input into
   the staging target, in `SEGMENT_ATTACH_BATCH`-sized groups. Payload bytes
   never enter Python.
3. **Bulk metadata tables.** Write `points`, `fragment_origins`,
   `day_ownership` and `import_audit` with one `executemany` per table, keeping
   `mutation_generation` numbering identical to sequential writes.
4. **One transaction for stitch decisions.** Use `persist_fragment_resolutions`
   rather than a connection per decision.

## Invariants — unchanged

- Published `source_content_digest`, fragment identity and canonical bytes.
- First-wins duplicate policy, winner ordinals, quarantine reasons.
- `mutation_generation` advances once per committed fragment; each
  `import_audit` row records its own `generation_before`/`generation_after`.
- `day_ownership` stays half-open `[start_date, end_date)`.
- Origin lineage across inputs, including `origin_database_id` per input.
- Atomic publication through the staging rename.
- The identity verification restored for publication (`compact_digest` at
  segment read, `sha256(zlib.decompress(payload)) == fragment_id` at publish)
  applies here too; merge must not publish a database it has not bound to its
  identities.

## Execution order

1. Characterisation test first: merge two small databases with the current
   implementation, dump every published table, and pin it. This is the contract
   the rewrite must not break.
2. Metadata-based read and duplicate resolution, with selective hydration for
   multi-fragment points.
3. `ATTACH`-based payload copy in bounded groups.
4. Bulk metadata tables and batched resolution persistence.
5. Re-measure on the two real corpora; record wall clock, peak RSS and the
   published digest.

## Acceptance evidence

1. Whole-database equivalence against the pinned characterisation dump.
2. Full `.venv\Scripts\python.exe -m pytest` suite green.
3. A completed merge of the 5,859 and 59,675 fragment corpora, with recorded
   wall clock and peak RSS, and a published database that passes the identity
   verification with zero mismatches.
4. Independent code review returning `CODE_REVIEW_PASS`.

## Non-goals

- Changing the merge contract, input validation or lineage semantics.
- Merging databases with different schema versions or fingerprints.
- Any change to the importer, which is covered by its own spec.
