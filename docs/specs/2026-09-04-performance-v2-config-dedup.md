# Performance DB v2 typed-config deduplication

**Status:** Active implementation contract
**Date:** 2026-09-04
**Dependencies:** [Performance DB v2 CHECK & RETEST](2026-09-03-performance-v2-retest-workflow.md), [ADR-0023](../decisions/0023-performance-v2-typed-config-dedup.md)

## Goal

Treat reports for the same executable MRS3 settings as one logical
Performance DB strategy even when the strategy name, ID, analysis run, or
candidate lineage differs. Preserve intentionally different lot allocations
(`EQUAL` versus `INCOME`). Replace an existing result only when the incoming
effective report interval is a proper superset of the stored interval.

## Canonical typed identity

The import key is the tuple below:

```text
(symbol, side, timeframe, close_ma_len, order_count,
 sorted multiset of (open_ma_len, shift_bp, lot_x_decimal_12))
```

`order_id`, strategy names, strategy IDs, `analysis_run_id`,
`candidate_identity`, `plateau_id`, and plateau/base-trade diagnostics are
provenance, not executable settings, and are excluded. `shift_bp` is the
authoritative open threshold; it is derived from and validated against the
multiplier at the input boundary. `lot_x` is quantized to
`Decimal("0.000000000001")` with `ROUND_HALF_UP` before comparing incoming and
stored rows. Repeated equal order settings remain repeated in the multiset.

## Effective coverage and import decisions

The existing 120-hour warm-up constant is used for both sides of a comparison.
For an incoming report and a stored result, normalize timestamps to UTC and
compute:

```text
base_start = coalesce(effective_start, reported_start, report_start)
start = max(base_start, listing_date_from_configured_dates_file + 120 hours)
end = coalesce(effective_end, reported_end, report_end)
```

Missing or invalid listing data remains the existing per-entry fail-closed
path. Legacy rows with null provenance use the same fallback as new rows.

An incoming interval is strictly wider only when:

```text
incoming.start <= current.start
and incoming.end >= current.end
and (incoming.start < current.start or incoming.end > current.end)
```

All equality is exact after UTC normalization; there is no tolerance. Equal,
narrower, shifted, or otherwise incomparable intervals are skipped by `ADD`.
Therefore a later raw report start cannot replace a result whose effective
start is already clamped to the same listing date plus warm-up.

Within one `ADD` batch, equal-key entries are reduced before writes. One entry
whose interval contains every peer is selected; equal maxima use manifest
order. Incomparable maxima fail the whole batch before publication.

An existing name with a different typed key fails closed. A different incoming
name with an equal typed key reuses the existing canonical strategy ID, name,
and orders; no second strategy row is inserted. A proper-superset report
replaces only that canonical row's current result using the existing atomic
child-row transaction. RETEST `REPLACE` uses the server-built mapping of every
committed RETEST strategy to its current row. It may replace an equal period or
a later-ending period whose effective duration is not shorter; its effective
start is compared only after the configured listing-date warm-up. It requires
one active row for the typed key, a typed-key-equal target, and (when
`expected_strategy_identities` is supplied) a complete expected identity for
every mapped strategy. Partial or malformed expected identities fail closed.

All key loading, decision resolution, revalidation, writes, and readback stay
inside the existing Performance DB writer lock and transaction. Any ambiguity
or failure rolls back the full batch.

`clear_retest_on_success` removes a `RETEST` tag only after its server-mapped
REPLACE result passed child readback. ADD-mode deduplication never removes it;
skipped, rejected, unknown, or failed decisions retain the tag. Replacement
identity controls are rejected unless the request is in REPLACE mode.

REPLACE keeps the canonical strategy row and `result_id`, deletes and rewrites
only that result's actions, equity samples and window metrics. Selection runs
remain immutable point-in-time history: their stored `result_id_at_selection`
is not rewritten or treated as current data. Order MA, shifts, order count and
lots must already match the canonical typed identity; the multiplier is
validated as the exact source of its stored `shift_bp`. Plateau and candidate
facts are provenance and are not rewritten by a retest result.

The initial import validation still checks a report against the committed
inbox's declared test range; publication repeats only symbol/request checks
because listing warm-up may rewrite the effective range. A retired name with
no active canonical row remains fail-closed rather than being silently
resurrected.

## One-time database audit

The audit is dry-run by default and writes an ignored JSON evidence artifact.
It uses the same canonical identity function. The current database is expected
to have 7,904 apparent MA/shift groups when `lot_x` is omitted (intentional
`EQUAL`/`INCOME` pairs) and zero exact groups with `lot_x` included. No row is
discarded and no new `RETEST` tag is added in that expected case.

If a future audit finds exact active duplicates, an explicit apply operation
must hold the single-writer lock, verify a reopened backup and table counts,
recompute candidates, and transactionally set only non-survivors to
`DISCARDED`. A survivor must contain every peer interval; equal intervals use
the lowest Strategy ID, while incomparable groups remain untouched. Children
and results are retained. Only actually merged survivors receive a `RETEST`
tag. Post-apply checks must verify one active row per key, unchanged total
fact counts, foreign-key integrity, and active-only reader behavior.

## Acceptance evidence

- importer tests cover key normalization, lot distinction, cross-name reuse,
  legacy null-provenance fallback, interval decisions, same-batch ambiguity,
  mapped RETEST collision, and transaction rollback;
- the focused and relevant Performance v2 suites pass in `.venv`;
- the audit records the database identity and expected counts
  `strategies=16272`, `results=16272`, `orders=41280`, `ACTIVE=16272`,
  `DISCARDED=0`, `RETEST=149`, apparent no-lot groups `7904`, exact groups `0`;
- no generated database or audit artifact is committed.
