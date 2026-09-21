# Source v6 materialization speed

**Status:** Implemented.

## Goal

Cut the wall-clock cost of `measure_point_group`, the worker that materializes
one parameter combination, without changing a single published value. A full
27,360-point materialization took about 27 minutes on 20 workers, and a profile
of one worker attributed the time to two places that do redundant work rather
than to the measurement itself.

## Non-goals

- Change any metric, witness, window, event id, PnL or drawdown rule.
- Change the PRETEST A/B evidence contract or the number of measured windows.
- Change worker counts, the pool topology, the storage schema or any codec.
- Speed up HTML import or canonicalization at write time.

## Measured cause

Profiling one worker over 40 real points of `CRCLUSDT|LONG|5m`:

- `canonical_fragment_id_from_payload` — 34.6% of the worker. Decoding a
  fragment re-serialized the whole canonical document (every action and every
  sample) to re-derive an id the stored bytes already carry.
- Two generator expressions in `calculate_metrics` — 29.4% of the worker. The
  first fragment's sample cutoff, `min(open_timestamp_ms of the old-open
  cycles)`, was evaluated inside the filter of a comprehension over the wallet
  and equity samples, so the scan was quadratic in samples x cycles.

## Change

1. `source_v6_stitch.calculate_metrics` decides the first fragment's cutoff once
   before filtering. Pure refactor: the expression depends only on the
   fragment's own cycles and is not touched inside the comprehension.
2. `source_v6.decode_fragment` passes the byte-derived id into
   `_fragment_from_payload` instead of rebuilding the canonical document, and
   offers the previous check as the opt-in `strict_canonical=True` audit. This
   moves where canonical form is proven, so it is decided in
   [ADR-0040](../decisions/0040-source-v6-read-time-identity-from-stored-bytes.md).
3. Because reads no longer re-prove canonical form, `import_fragment` and
   `import_fragment_batch`, which accept payload bytes their caller serialized,
   prove it themselves through `_assert_canonical_encoding`. Neither has a
   caller in `src`, so the live import pays nothing. Review rounds proposed the
   same proof at the segment writer and at merge; it was measured and declined,
   because canonical form is a reproducibility property rather than an
   authenticity one and the check would decode the whole corpus on the live
   import path without changing what can be published. ADR-0040 records that
   reasoning and the measurement.

## Invariants

- Every analysis row, fragment id, witness, digest and surface fingerprint is
  unchanged. The change is in how a value is obtained, never in the value.
- Readers still bind bytes to identity: compact blob checksum, stored
  `fragment_id`, and W6's `sha256(zlib.decompress(blob))` at publication.
- Canonical form remains verifiable on demand through `strict_canonical=True`.

## Acceptance evidence

Wall-clock, no profiler, on the real 27,360-report local Source DB, 60 real
points from two differently shaped scopes (`CRCLUSDT|LONG|5m` and
`MSTRUSDT|LONG|4h`), one process. ADR-0017 requires a comparable three-run
baseline/candidate measurement, so each state of the code was measured three
times, each time by stashing only the edits that state excludes:

| Step | Run 1 | Run 2 | Run 3 | Best per point | Analysis rows sha256 |
| --- | --- | --- | --- | --- | --- |
| Baseline | 24.41 s | 24.31 s | 25.25 s | 0.4052 s | `f1b2a902…a081` |
| After the cutoff hoist | 13.48 s | 13.60 s | 13.87 s | 0.2246 s | `f1b2a902…a081` |
| After byte-derived identity | 9.38 s | 9.43 s | 9.46 s | 0.1563 s | `f1b2a902…a081` |

2.59x faster on best and on median runs, with a byte-identical dump of every
analysis row at every step, which is the equivalence proof: the rows carry the metrics, event ids, PRETEST evidence and
point identity that the surface publishes.

Canonical-form audit on the same database, `strict_canonical=True` over 24,790
of its 27,360 fragments (90.6%): every one re-serialized to exactly its stored
bytes. The earlier full-database materialization recorded in `progress.md` ran
the old strict path over all 27,360 and raised nothing, so no stored payload in
this database depends on the removed check.

TDD: `tests/test_source_v6_storage.py` pins that both import boundaries refuse a
caller-supplied non-canonical payload and commit nothing; and
`tests/test_source_v6_stage1_v2.py` pins that decoding does not rebuild the
canonical document (the rebuild function is replaced by one that raises) and
that non-canonical stored bytes decode to the identity their bytes carry but are
refused under `strict_canonical=True`.

Full suite from `.venv`: 4,719 passed, 7 skipped, 2 failed, neither caused by
this change, which shares no code with the modules involved. Every failure seen
across the runs of this work is a concurrency test that passes when run on its
own: `test_portfolio_store.py::test_cross_process_busy_reclaim_and_manual_clear_are_safe`
(deterministic on a loaded machine, and failing identically with this change
stashed: `FileExistsError` from the Windows lock rename in
`portfolio/store.py:272`), `test_portfolio_store.py::test_concurrent_exact_campaign_duplicate_returns_one_identity`,
`test_duckdb_import.py::test_concurrent_import_to_same_resolved_database_is_rejected_without_publication`
and `test_panel_performance_v2.py::test_v2_catalog_and_windows_http_are_typed_and_repeatable`.
Which of them fails varies with machine load; the portfolio lock defect is
pre-existing and left for that module and its own spec.

## Not done

`calculate_metrics` still runs twice per point, once for the READY witness and
once for the PRETEST B fortnight, which the profile put at about a quarter of
the worker. Deriving B from the A pass would touch metric derivation itself, so
it needs its own spec and its own equivalence evidence.
