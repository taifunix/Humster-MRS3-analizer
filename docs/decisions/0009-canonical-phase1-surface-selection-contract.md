# ADR-0009: Canonical Phase 1 Surface, Readiness, and Frozen Selection Contract

**Status:** Accepted
**Date:** 2026-08-16

**Affects:**
- [DuckDB surface coverage review](../specs/2026-08-14-duckdb-surface-coverage-review.md)
- [Canonical Phase 1 specification](../specs/2026-08-16-mrs3-v07-canonical-phase1.md)
- [ADR-0007](0007-observed-sparse-surface-contract.md)
- [ADR-0008](0008-common-close-ma-readiness-and-degenerate-row-isolation.md)

## Context

ADR-0007 established observed sparse V2 surfaces, deterministic evidence,
one read-only source transaction for preparation/materialization, and one
canonical MA/witness-shift tuple per selected scope. ADR-0008 then replaced
the single-scope witness cardinality with a common exact UTC interval and six
CloseMA `2..7` witnesses, while retaining the earlier
`shift_readiness_v1` pair-level readiness contract and compatibility with
already-published surfaces.

The current MRS3 Phase 1 target is different:

- all operational surfaces will be rebuilt from the source DuckDB;
- the active Shift universe is one exact 19-value canonical grid through
  `550 bp`;
- readiness must prove all six CloseMA `2..7` on one exact interval;
- preview/preflight/audit evidence must be frozen before `Start` and replayed
  exactly;
- only the new canonical surface contract is admissible for new analysis,
  rerun, parent use, and READY JSON generation;
- Phase 1 requires a bounded multi-process execution path to be benchmarked
  against the current sequential direct materializer without changing
  semantic output; the actual speedup is unverified until that benchmark is run;
- representative selection and independent BASE 1ORD must be frozen and
  reproducible after publication without adding Analysis schema v5.

Project governance in `AGENTS.md` requires a new ADR for these architecture and
data-contract changes rather than editing accepted ADRs retroactively.

## Decision

### 1. Supersession boundaries

When this ADR is accepted, it supersedes **only the conflicting operational
parts** of ADR-0007 and ADR-0008 for newly built Phase 1 canonical surfaces.

From ADR-0007 it supersedes:

- the V2 readiness/evidence shape in which one canonical MA/witness-shift tuple
  per selected scope is sufficient;
- any implication that an old V2 surface is automatically a valid operational
  input for new Phase 1 analysis merely because its grid-contract kind is V2.

From ADR-0008 it supersedes:

- the retained pair-level `shift_readiness_v1` contract;
- the old `30/150/430` sparse readiness sequence and `430 bp` maximum;
- the requirement that newly published Phase 1 surfaces remain operationally
  compatible with earlier V1/V2 readiness surfaces.

This ADR **retains** the following ADR-0008 decisions:

- one continuous exact UTC interval shared by CloseMA `2..7`;
- one complete OpenMA witness per exact CloseMA over the whole interval;
- different CloseMA values may use different OpenMA witnesses;
- one CloseMA may not stitch different OpenMA values across subintervals;
- longest qualifying common interval, then deterministic tie-breaking;
- a row is structurally degenerate only when both its report and grid windows
  are zero-duration;
- structurally double-zero rows are ignored before effective-window
  calculation;
- all other empty/incompatible intersections remain fail-closed.

This ADR **retains** the non-conflicting ADR-0007 decisions:

- observed sparse factual publication rather than synthesis of missing source
  points;
- existing `OBSERVED_SPARSE_GRID_CONTRACT_V2` storage in
  `grid_contract_json`;
- deterministic surface evidence and identity;
- one read-only source DuckDB snapshot for preparation/materialization;
- preparation before publication;
- separate immutable LONG then SHORT publication and truthful
  `FAILED`/`PUBLISHED`/`PARTIAL` outcomes;
- no Analysis schema migration solely for this feature.

### 2. Canonical Shift and readiness contract

The sole operational Shift grid for new Phase 1 surfaces is:

```text
30, 40, 50, 60, 70,
90, 110, 140, 170, 200,
230, 270, 310, 350, 390,
430, 470, 510, 550
```

For each selected `Pair + Side + TF`, readiness requires one exact common
continuous UTC interval on which every CloseMA in `(2,3,4,5,6,7)` has at least
one OpenMA covering **all 19 canonical shifts** for the whole interval.

Non-canonical source shifts may remain in the source database but do not enter
the new canonical surface or new analysis.

### 3. Exact operational surface contract

New canonical publications keep:

```text
grid_contract.kind = OBSERVED_SPARSE_GRID_CONTRACT_V2
Analysis schema = v4
event_mode = real_independent_events
```

but add exact versioned Phase 1 evidence defined by the canonical specification:

```text
canonical_grid_version = mrs3_shift_grid_30_550_v1
readiness_contract_version = close_ma_2_7_canonical_grid_v1
materializer_version = v4-canonical-grid-parallel
point_materialization_semantics_version = direct_point_materialization_v1
plateau_operational_facts_version = cma_representatives_v1
audit_schema_version = 1
```

Historical surfaces remain stored. They are historical evidence only and are
rejected by the new operational analysis, parent/rerun, and strategy-generation
entry points unless they satisfy the complete exact canonical predicate.

There is no selectable legacy mode and no automatic migration of old surfaces.

### 4. Preview, audit, and Start replay

Coverage scan and selected preflight are distinct contracts.

A final selected preflight freezes:

- exact selected scopes;
- exact per-side UTC intervals;
- exact 19-shift tuple;
- exact ordered six-witness vectors;
- accepted point/source evidence;
- exact canonical audit bytes plus name, schema, size, row count, and SHA-256;
- all exact contract identifiers.

`Start` must re-read the source under a fresh read-only transaction, regenerate
the same audit and preflight, and require exact equality. Any changed witness,
interval, scope, accepted point evidence, audit bytes, or relevant config makes
the selected preflight stale and prevents publication.

The persisted audit artifact is verified again for all prepared sides before
the first publication and again immediately before each side commit.

### 5. Materialization execution model

Materialization semantics do not change, but execution becomes bounded
multi-process:

- one main-process DuckDB source connection and transaction/snapshot;
- batched source reads instead of one SQL query per report;
- `ProcessPoolExecutor` with a default of 15 workers;
- Windows-spawn-safe module-level workers;
- bounded in-flight chunks;
- timestamp-grid reuse by `grid_hash` within worker chunks;
- canonical sorting/validation in the main process;
- worker count and batch settings are operational only and do not affect
  surface identity.

A one-worker and 15-worker run for the same frozen request must produce the
same semantic surface and identity.

### 6. Frozen selection facts and 1ORD independence

One `CMARepresentative` is selected per `Plateau + exact CloseMA` after the
existing 5% economic-equivalence group is formed and the event gate is applied.
Primary CloseMA, support state, continuity state, and local BASE 1ORD point are
then frozen as a versioned operational structure inside existing
`plateaus.metrics_json`.

The structure is validated against immutable surface points and Plateau
membership at publication and read time. Invalid or contradictory persisted
facts fail closed; consumers may not silently recompute replacements.

2/3/4ORD structures use only frozen continuity-usable representatives.

BASE 1ORD generation is independent of multiorder candidate existence, is
scoped by exact `(symbol, side, timeframe)`, and can succeed with an empty
multiorder candidate list.

### 7. Schema and scope restraint

Phase 1 keeps Analysis schema v4. It does not add a dedicated close-profile
table, schema v5, new event reconstruction, portfolio simulation, lot-allocation
redesign, Tester changes, or a legacy-mode UI.

The detailed normative contract is defined in
`docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md`.

## Consequences

- Fresh Phase 1 surfaces are semantically unambiguous and are not confused with
  historical sparse V2 surfaces.
- Missing one canonical Shift or one required CloseMA witness blocks readiness.
- New analysis cannot accidentally consume old readiness surfaces.
- Previewed evidence cannot silently drift between selection and publication.
- Direct materialization can use the available workstation CPU without giving
  worker processes independent source snapshots.
- Frozen CloseMA and BASE decisions are reproducible after publication without
  Analysis schema v5.
- Historical ADR-0007/0008 text remains unchanged and continues to describe the
  decisions that were in force before ADR-0009.
