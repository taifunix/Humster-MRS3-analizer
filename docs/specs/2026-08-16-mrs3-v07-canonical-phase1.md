# MRS3 v0.7 Canonical Phase 1 Surface and Selection Contract

**Status:** Approved / Active
**Date:** 2026-08-16
**Decision:** [ADR-0009](../decisions/0009-canonical-phase1-surface-selection-contract.md)
**Implementation plan:** [Canonical Phase 1 implementation plan](../superpowers/plans/2026-08-16-mrs3-v07-canonical-phase1.md)

## 1. Purpose

This specification is the normative Phase 1 contract for building fresh
canonical DUCKDB_DIRECT surfaces, running deterministic MRS3 Plateau selection,
persisting frozen CloseMA/BASE facts, and generating READY 1ORD/2ORD/3ORD/4ORD
strategy JSON.

It succeeds, for new Phase 1 behavior, the relevant readiness/materialization
portions of
[DuckDB surface coverage review](2026-08-14-duckdb-surface-coverage-review.md).
That earlier specification remains verified evidence for its implemented
Priority-1 behavior; it is not rewritten.

If accepted, this document is the active feature specification referenced by
`PRD.md` and `progress.md`. Detailed implementation steps belong only in the
linked plan.

## 2. Dependencies and precedence

Read together with:

- [ADR-0009](../decisions/0009-canonical-phase1-surface-selection-contract.md);
- [ADR-0007](../decisions/0007-observed-sparse-surface-contract.md), retained
  only where not superseded by ADR-0009;
- [ADR-0008](../decisions/0008-common-close-ma-readiness-and-degenerate-row-isolation.md),
  retained only where not superseded by ADR-0009;
- [DuckDB analysis storage and importer](2026-08-11-v07-duckdb-analysis-storage-and-importer.md);
- [Event filter and shortlist](v07-event-filter-and-shortlist.md), except where
  this specification explicitly replaces thresholds/selection behavior;
- [Panel Multi-Scope Strategy Generation](2026-08-14-panel-multiscope-strategy-generation.md),
  except where the exact 1ORD scope API below strengthens it.

Per `AGENTS.md`, this newer approved specification outranks older ADR/PRD/archive
text on the behavior it explicitly defines.

## 3. Scope

### In scope

- one canonical Shift grid through `550 bp`;
- common exact UTC readiness across CloseMA `2..7`;
- exact selected-preflight/audit freeze and Start-time replay;
- canonical V2 evidence and hard admission predicate;
- fresh canonical materialization only;
- bounded multi-process materialization with default 15 workers;
- canonical Shift adjacency in refine/Plateau geometry;
- one frozen `CMARepresentative` per Plateau+CloseMA;
- CloseMA support `90/60` and continuity;
- versioned frozen operational facts in existing `plateaus.metrics_json`;
- 2/3/4ORD only from frozen representatives;
- new GAP rules across `30..550`;
- independent exact-scope BASE 1ORD generation;
- analysis/materializer version bumps;
- focused tests, real-source smoke, and materialization benchmark.

### Non-goals

Phase 1 does not add:

- `Refine_Missing_Cells.csv`;
- `Refine_Required_Points.csv`;
- full Before/After Event Filter audit;
- new Plateau diagnostic DB tables;
- a dedicated `close_profiles` DB table;
- Analysis schema v5;
- Phase 2 Pareto/redundancy redesign;
- Event reconstruction redesign;
- lot-allocation redesign;
- strategy JSON schema redesign;
- Tester changes;
- actual fill-order diagnostic;
- event-bridge hard filter;
- CoreSize hard gate;
- portfolio simulation;
- full UI redesign;
- selectable legacy mode;
- automatic old-surface migration.

## 4. Canonical algorithm configuration

### 4.1 Canonical Shift grid

The exact operational grid is:

```python
(
    30, 40, 50, 60, 70,
    90, 110, 140, 170, 200,
    230, 270, 310, 350, 390,
    430, 470, 510, 550,
)
```

`AlgorithmConfig.canonical_shifts_bp` is the primary runtime source of truth.

Requirements:

- values are integers; `bool` is rejected;
- non-empty, unique, strictly increasing;
- first value equals `shift_domain_min_bp`;
- last value equals `shift_domain_max_bp`;
- `shift_domain_max_bp = 550`;
- `config.example.json` documents the tracked default;
- ignored `config.local.json` is a runtime override only and is not normative.

Source rows at non-canonical shifts such as `80,100,120,130,150` may remain in
source DuckDB but do not enter new canonical surfaces or analysis.

### 4.2 Sample calibration

```text
shift <=150 -> 1.00
shift <=200 -> 0.90
shift <=310 -> 0.30
shift <=550 -> 0.20
```

### 4.3 Plateau and CloseMA thresholds

Plateau geometry remains:

```text
CORE link       >= 0.90
SUPPORTED point >= 0.75
Envelope        >= 0.75
```

CloseMA support is:

```text
PRIMARY_CLOSE                    = primary only
CORE_CLOSE        support >= 0.90
SUPPORTED_CLOSE   0.60 <= support < 0.90
UNSUPPORTED_CLOSE support < 0.60
```

The `0.60` CloseMA support threshold must not replace either Plateau `0.75`
threshold.

### 4.4 GAP rules

For adjacent sorted orders, use the smaller/left Shift:

```text
30 <= left < 80    -> minimum gap 80 bp
80 <= left < 200   -> minimum gap 100 bp
200 <= left < 300  -> minimum gap 130 bp
300 <= left <= 550 -> minimum gap 150 bp
```

The new runtime does not emit or consume `DEEP_GAP_RESEARCH`.

## 5. Exact versioned contracts

The following identifiers are exact:

```text
CANONICAL_GRID_CONTRACT_KIND
= OBSERVED_SPARSE_GRID_CONTRACT_V2

CANONICAL_GRID_VERSION
= mrs3_shift_grid_30_550_v1

CANONICAL_READINESS_CONTRACT_VERSION
= close_ma_2_7_canonical_grid_v1

CANONICAL_MATERIALIZER_VERSION
= v4-canonical-grid-parallel

POINT_MATERIALIZATION_SEMANTICS_VERSION
= direct_point_materialization_v1

PLATEAU_OPERATIONAL_FACTS_VERSION
= cma_representatives_v1

V2_AUDIT_SCHEMA_VERSION
= 1

ALGORITHM_VERSION
= 0.7-canonical-phase1
```

No version-family matching or aliases are valid.

## 6. Point-materialization semantic hash

There is one code-owned implementation in `src/mrs3/duckdb_direct.py`,
conceptually:

```python
def canonical_point_materialization_semantic_payload(
    canonical_shifts_bp: tuple[int, ...],
) -> dict[str, object]:
    ...

def canonical_point_materialization_config_hash(
    canonical_shifts_bp: tuple[int, ...],
) -> str:
    ...
```

All consumers use this helper rather than reconstructing the hash.

### 6.1 Exact payload schema

The helper returns exactly this mapping, with
`NORMALIZATION_CONTRACT_VERSION` replaced by the exact imported runtime
constant from `duckdb_source_schema.py`:

```python
{
    "canonical_grid_version": "mrs3_shift_grid_30_550_v1",
    "canonical_shifts_bp": [
        30,40,50,60,70,
        90,110,140,170,200,
        230,270,310,350,390,
        430,470,510,550,
    ],
    "event_id_contract":
        "sha256_utf8_pipe(symbol,position_side,timeframe,opened_at_utc_ns)",
    "event_mode": "real_independent_events",
    "materialization_scope_contract":
        "fully_covering_selected_scope_points_on_exact_canonical_shifts",
    "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION,
    "point_event_count_contract": "count_unique_sorted_canonical_event_ids",
    "readiness_contract_version": "close_ma_2_7_canonical_grid_v1",
    "required_close_mas": [2,3,4,5,6,7],
    "semantic_contract_version": "direct_point_materialization_v1",
    "window_contract": "utc_half_open_[start,end)",
}
```

No additional or optional keys belong in this semantic payload. In particular,
worker count, batch size, audit hash, source hash, symbol, side, timeframe and
concrete interval are excluded.

### 6.2 Canonical serialization and digest

Serialize exactly:

```python
canonical_json_bytes = json.dumps(
    payload,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
    allow_nan=False,
).encode("ascii")
```

No trailing newline is added.

Digest exactly:

```python
hashlib.sha256(canonical_json_bytes).hexdigest()
```

Admission recomputes the digest from the shared helper and the validated exact
Shift tuple. A syntactically valid 64-character hash is not trusted by itself.

## 7. Readiness

### 7.1 Required CloseMA set

Exactly:

```python
(2, 3, 4, 5, 6, 7)
```

### 7.2 Common interval

For each exact `Pair + Side + TF`, readiness requires one continuous UTC
half-open interval `[start,end)` shared by every required CloseMA.

For every CloseMA, at least one OpenMA must cover **all 19 canonical shifts**
over the entire interval.

Different CloseMA values may select different OpenMA witnesses.

One CloseMA may not stitch different OpenMA witnesses across subintervals.

### 7.3 Structural row handling

A source row is ignored only when:

```text
report_start == report_end
AND
grid_start == grid_end
```

The two zero timestamps need not be equal to each other.

Every other empty/incompatible report/grid intersection remains fail-closed.

### 7.4 Final interval and witnesses

The final selectable interval is chosen deterministically by:

1. longest duration;
2. earliest start;
3. earliest end;
4. lexicographically smallest ordered witness vector
   `(close_ma, open_ma, shifts_bp)`.

The stored witness vector contains exactly six entries ordered by CloseMA
`2..7`.

### 7.5 Factual non-witness MA coverage

Readiness requires only one complete OpenMA witness per CloseMA.

After the final interval is fixed, materialization includes every factual point
inside selected scopes that:

- covers the entire final interval; and
- has a Shift in the exact canonical tuple.

It is not restricted to the six witness MA pairs.

## 8. Coverage scan, selected preflight, audit, and Start replay

### 8.1 Two token meanings

`coverage_scan_token` binds the factual coverage scan and selectable rows.

A separate `preflight_token` is issued only after exact selected-scope
preflight succeeds.

A coverage-scan token alone cannot start materialization.

### 8.2 Selected-preflight order

Under one source transaction/snapshot, for each selected side in deterministic
LONG-before-SHORT order:

1. verify the coverage-scan token against a fresh scan;
2. compute the exact final common interval;
3. construct the exact `DirectBuildRequest` with selected scopes, 19 shifts,
   and exact contract versions;
4. generate canonical audit CSV bytes for that request;
5. compute:
   - `audit_sha256`;
   - `audit_size_bytes = len(audit_bytes)`;
   - `audit_row_count`;
   - `audit_schema_version = 1`;
   - `audit_artifact_name = surface_coverage_audit_<SIDE>.csv`;
6. attach the exact metadata and raw bytes to the request;
7. run real `preflight_duckdb_direct()` using that audit-bound request;
8. require all selected scopes to be available;
9. freeze exact request plus exact `DirectPreflight`;
10. write and verify the saved audit artifact from the same frozen bytes.

Only after all selected sides pass is the `preflight_token` issued.

### 8.3 Frozen selected state

The server keeps immutable in-memory state equivalent to:

```text
coverage_scan_token
ordered exact DirectBuildRequest tuple
ordered exact DirectPreflight tuple
preflight_token
```

Loss of this server-side state invalidates the token; it is not reconstructed
heuristically.

The token binds at least the canonical serialization of:

```text
coverage_scan_token
exact contract identifiers
exact required_shifts_bp
exact selected scopes
exact final intervals
exact ordered six-witness vectors
source hashes/evidence
accepted point keys
point-evidence hash
coverage inventory hash
audit artifact name
audit schema version
audit byte size
audit row count
audit SHA-256
```

### 8.4 Start-time exact replay

At Start, open one fresh read-only source transaction and:

1. reproduce the coverage scan and require exact scan-token equality;
2. regenerate canonical audit bytes for every frozen exact request;
3. require byte-for-byte equality with frozen audit bytes;
4. require identical audit name/schema/size/count/hash;
5. re-read the already saved audit file and verify it against frozen evidence;
6. rerun `preflight_duckdb_direct()` with the regenerated audit-bound request;
7. require active request/preflight to equal the frozen request/preflight.

Any changed interval, scope, witness, canonical grid, accepted point evidence,
audit bytes, or relevant contract/config fails as stale before materialization.

### 8.5 Persisted audit verification before publication

Because materialization may run for hours, audit-file integrity is checked
again.

For each side the saved path is:

```text
<audit_root>/
  surface_coverage/
    <audit_sha256>/
      surface_coverage_audit_<SIDE>.csv
```

Immediately before publication verify:

- regular readable file exists;
- filename equals frozen artifact name;
- schema version is `1`;
- file size equals `audit_size_bytes`;
- file SHA-256 equals frozen `audit_sha256`;
- file bytes equal frozen preflight bytes;
- row count equals frozen `audit_row_count`;
- existing LF-termination requirement passes;
- request/preflight/grid-contract metadata agree exactly.

Two gates are required:

- Gate A: verify all prepared side files before the first side commit;
- Gate B: re-read and re-verify the current side immediately before that side's
  `publish_surface()` call.

Gate-A failure publishes zero surfaces. A later Gate-B failure after a prior
side commit keeps the existing truthful `PARTIAL` behavior.

## 9. Persisted grid contract and exact surface admission

Analysis schema stays v4.

New canonical `grid_contract_json` includes at least:

```text
kind = OBSERVED_SPARSE_GRID_CONTRACT_V2
canonical_grid_version = mrs3_shift_grid_30_550_v1
canonical_shifts_bp = exact 19-shift tuple
readiness_contract_version = close_ma_2_7_canonical_grid_v1
point_materialization_semantics_version = direct_point_materialization_v1
selected scopes
exact six witnesses per scope
point/source evidence
audit artifact name
audit schema version = 1
audit byte size
audit row count
audit SHA-256
normalization contract version
```

A surface is valid for new Phase 1 analysis, parent/rerun use, and READY JSON
generation only if all of these are exact:

```text
build_mode == DUCKDB_DIRECT
event_mode == real_independent_events
grid_contract.kind == OBSERVED_SPARSE_GRID_CONTRACT_V2
canonical_grid_version == mrs3_shift_grid_30_550_v1
canonical_shifts_bp == exact 19-shift tuple
readiness_contract_version == close_ma_2_7_canonical_grid_v1
materializer_version == v4-canonical-grid-parallel
normalization_contract_version == current code-owned constant
point_materialization_semantics_version == direct_point_materialization_v1
point_materialization_config_hash == recomputed shared-helper digest
```

In addition:

- each selected scope has exactly six unique, strictly ordered CloseMA
  witnesses `2..7`;
- each witness uses the persisted exact canonical Shift tuple;
- audit evidence validates;
- point/source evidence validates.

One shared hard validator must be used by all new operational entry points.

Historical surfaces may remain stored but are rejected if they do not satisfy
this exact predicate. There is no version-family matching, no legacy mode, and
no automatic migration.

## 10. Parallel materialization execution

### 10.1 Operational settings

Separate runtime settings:

```python
DirectMaterializationSettings(
    workers=15,
    fetch_batch_size=256,
    worker_chunk_size=16,
    max_in_flight_chunks=30,
)
```

Validation:

```text
workers >= 1
fetch_batch_size >= 1
worker_chunk_size >= 1
max_in_flight_chunks >= workers
```

These settings do not participate in semantic identity.

### 10.2 Architecture

Use:

```text
MAIN PROCESS
one read-only DuckDB source transaction/snapshot
        в†“
bounded batched payload SELECT
        в†“
pickle-safe chunks
        в†“
ProcessPoolExecutor(max_workers=15)
        в†“
decode / metrics / event reconstruction
        в†“
MAIN PROCESS canonical merge/sort/validation
        в†“
publication
```

Worker processes never receive or open the source/analysis DuckDB connection
for this job and never publish.

### 10.3 Bulk fetch and bounded memory

The one-query-per-report pattern is removed.

The main process:

- bulk-fetches accepted manifest payloads in bounded batches;
- verifies every requested `(report_id, source_hash)` appears exactly once;
- treats missing/duplicate/mismatched evidence as source change and fails
  closed;
- limits outstanding chunks to `max_in_flight_chunks`;
- does not load the full source database into RAM.

### 10.4 Worker contract

The worker callable is module-level and Windows-spawn-safe, conceptually:

```python
def _materialize_payload_chunk(
    chunk: tuple[MaterializationPayload, ...],
    window_start_utc: str,
    window_end_utc: str,
) -> tuple[DirectPoint, ...]:
    ...
```

Inputs are pickle-safe primitives/bytes. The worker returns deterministic
`DirectPoint` facts only.

Within a chunk, timestamp grids may be decoded once per `grid_hash` and reused.

Existing metric formulas, `reconstruct_closed_cycles()`,
`canonical_event_id()`, and `PointEventCount = count(unique event_ids)` are not
changed.

### 10.5 Cancellation/failure and determinism

Cancellation stops new scheduling, cancels pending futures where possible, and
publishes no partial in-memory materialization.

Any worker exception fails the entire side preparation before publication.

After collection the main process canonical-sorts by point key and validates
uniqueness, side, and accepted point count.

For the same frozen request:

```text
workers=1
workers=15
```

must produce identical canonical point keys, metrics, event membership,
evidence, order, grid contract, and surface identity.

### 10.6 Progress and benchmark

Expose at least:

```text
phase
side
materialized_points
total_points
workers
elapsed_seconds
points_per_second
```

A real-source benchmark records one-worker and 15-worker materialization time
for the same exact frozen request plus publish/total time.

A measured speedup below `2x` leaves the performance task unresolved and
requires profiling evidence before completion is claimed.

## 11. Canonical Shift adjacency in refine and Plateau geometry

Distinct Shifts are neighbors only when adjacent in
`AlgorithmConfig.canonical_shifts_bp`.

Examples:

```text
30 <-> 40        yes
70 <-> 90        yes
90 <-> 110       yes
470 <-> 510      yes
510 <-> 550      yes

70 <-> 110       no
140 <-> 200      no
430 <-> 510      no
```

Refine and Plateau use one shared adjacency helper.

OpenMA/CloseMA neighbor radius/domain behavior and diagonal MA adjacency remain
unchanged.

## 12. CMARepresentative contract

For each `Plateau + exact CloseMA`:

1. take Plateau members with that exact CloseMA;
2. keep `economic_pass == True`;
3. choose the CMA-specific economic reference by:
   - PnL DESC;
   - PnL/DD DESC;
   - Trades DESC;
   - DD ASC;
   - PointID ASC;
4. build the existing 5% equivalent group against that reference using PnL and
   efficiency;
5. only then apply `event_eligible`, preserving
   `PointEventCount >= 3`;
6. if rows remain, choose exactly one representative by:
   - PointEventCount DESC;
   - Shift DESC;
   - PnL DESC;
   - PnL/DD DESC;
   - Trades DESC;
   - DD ASC;
   - PointID ASC.

Do not constrain a CloseMA representative to the global primary's Shift or
OpenMA radius.

No later stage may rerun a different representative search.

## 13. Primary CloseMA, support, and continuity

### 13.1 Primary

Choose primary from existing CMARepresentatives by:

```text
PnL DESC
PnL/DD DESC
Trades DESC
DD ASC
PointID ASC
```

Primary has:

```text
support = 1.0
support_status = PRIMARY_CLOSE
continuity_status = USABLE
usable = true
```

### 13.2 CloseSupport numeric safety

Before division, convert with `Decimal(str(value))`.

Primary PnL and efficiency and every non-primary PnL and efficiency used in
support calculation must be finite and `> 0`.

Invalid zero, negative, NaN, or infinity is an invariant violation and fails the
analysis run. Do not clamp, skip, substitute epsilon, select another primary, or
persist an invalid profile.

For valid inputs:

```text
support_pnl = CMA_PnL / Primary_PnL
support_eff = CMA_Efficiency / Primary_Efficiency
CloseSupport = min(support_pnl, support_eff)
```

The result must be finite and satisfy `0 < CloseSupport <= 1`.

### 13.3 Support and continuity truth table

Raw support class:

```text
PRIMARY_CLOSE     primary only
CORE_CLOSE        support >= 0.90
SUPPORTED_CLOSE   0.60 <= support < 0.90
UNSUPPORTED_CLOSE support < 0.60
```

Traverse independently upward/downward from primary.

Before a break, an adjacent existing CORE/SUPPORTED representative is:

```text
continuity_status = USABLE
usable = true
```

The first present unsupported representative is:

```text
continuity_status = BREAK_UNSUPPORTED
usable = false
```

A missing integer CloseMA breaks continuity without a row for the missing CMA.

Every existing outer representative after a missing or unsupported break is:

```text
continuity_status = BLOCKED_BY_CONTINUITY
usable = false
```

Always:

```text
usable == (continuity_status == "USABLE")
```

Validation recomputes this truth table rather than trusting persisted flags.

## 14. Frozen Plateau operational facts

Persist inside existing `plateaus.metrics_json`:

```text
operational_facts_version = cma_representatives_v1
primary_close_ma
cma_representatives
base_1ord_point_id
```

Representatives are a strictly CloseMA-ordered list with exact fields:

```json
{
  "close_ma": 4,
  "point_id": "PAIR|SIDE|TF|SHIFT|OMA|4",
  "support": 0.83,
  "support_status": "SUPPORTED_CLOSE",
  "continuity_status": "USABLE",
  "usable": true
}
```

Validation at publication and read time requires:

- known exact operational-facts version;
- strictly increasing unique CloseMA;
- unique point IDs;
- finite valid support and exact status classification;
- exactly one primary matching `primary_close_ma`;
- semantic continuity/usable truth table;
- representative point exists in the immutable surface;
- representative belongs to the Plateau;
- point scope matches Plateau;
- point CloseMA matches representative CloseMA.

Invalid persisted facts fail closed. Consumers never repair them by reselection.

## 15. Frozen local BASE 1ORD

For each READY Plateau, local BASE candidates are only:

```text
continuity-usable frozen CMARepresentatives
AND source point standalone_eligible == True
```

Choose at most one local BASE using the existing equivalent/default semantics
only over this already-frozen candidate pool and persist its exact
`base_1ord_point_id`.

If non-null, BASE must:

- exist in the immutable surface;
- belong to the Plateau;
- exist exactly once among frozen representatives;
- be `USABLE`;
- have `usable == true`;
- belong to `standalone_eligible_point_ids`;
- match the Plateau scope.

An invalid BASE id fails closed and is never silently replaced.

## 16. 2/3/4ORD structures and GAP

For each exact `Pair + Side + TF + CommonCloseMA`:

- each Plateau contributes at most one frozen representative for that CloseMA;
- representative must be continuity-usable;
- orders come from distinct Plateaus;
- sort by Shift before validation;
- order 1 must be standalone-eligible;
- orders 2вЂ“4 must be depth-eligible;
- 2ORD, 3ORD, and 4ORD universes are built independently;
- no equivalent-point Cartesian re-enumeration is allowed;
- GAP rules from В§4.4 apply to every adjacent order pair;
- EQUAL/INCOME downstream behavior is otherwise unchanged.

## 17. Exact independent 1ORD generation API

Logical generator signature:

```python
def generate_analysis_strategies(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    candidate_ids: Sequence[str],
    selected_scopes: Sequence[tuple[str, str, str]],
    template_path: Path | str,
    output_dir: Path | str,
    config: AlgorithmConfig,
    criteria: tuple[str, ...] = (),
) -> GeneratedAnalysisStrategies:
    ...
```

`selected_scopes` tuple order is exactly:

```text
(symbol, side, timeframe)
```

Rules:

- normalize scopes to sorted unique tuples;
- empty selected scopes -> error;
- each scope must exist in the run's canonical published surface;
- scope side must match surface/run side;
- `candidate_ids` may be empty;
- supplied candidates must exist, be READY, and lie inside selected scopes.

Panel/controller must derive selectable scopes from the canonical published
run/surface plus persisted Plateau BASE facts, not only from 2вЂ“4ORD candidates.

A scope with BASE 1ORD but no multiorder candidate must remain selectable.

For each exact scope, one frozen local BASE per Plateau becomes a BASE candidate.
Rank at most three by:

```text
PnL@DD5 theoretical DESC
raw PnL DESC
Trades DESC
DD ASC
PointID ASC
```

Do not add Shift or EventCount to this Top-3 ranking.

Output:

```text
1ORD   -> EQUAL only
2вЂ“4ORD -> EQUAL + INCOME
```

Generation succeeds if either BASE output or selected READY multiorder output
exists. It errors only when both are absent.

Job lifecycle remains:

```text
STARTING -> GENERATING -> COMMITTED
                     \-> FAILED
```

`strategy_manifest.json` adds deterministic:

```text
selected_scopes
ready_structure_count
base_1ord_count
strategy_count
```

## 18. Event and eligibility invariants retained

The real-events path is not redesigned.

Production hard floor remains:

```text
PointEventCount >= 3
```

Standalone eligibility keeps existing economic/history/sample rules plus event
eligibility and READY Plateau membership.

Depth eligibility keeps economic + event + READY Plateau and does not re-add a
raw Trades floor.

`PlateauEventCount` remains diagnostic only.

## 19. Persistence and versioning

Analysis schema stays:

```text
ANALYSIS_SCHEMA_VERSION = 4
```

Phase 1 sets:

```text
ALGORITHM_VERSION = 0.7-canonical-phase1
materializer_version = v4-canonical-grid-parallel
```

Worker count and batch sizes are excluded from semantic identity.

No new table is required for frozen CloseMA/BASE operational facts.

## 20. Acceptance evidence

Before implementation may be declared complete, focused regression evidence
must prove:

### Configuration

- exact canonical tuple validation;
- sample factor through 550;
- 90/60 CloseMA threshold;
- GAP-rule validation;
- direct materialization settings validation.

### Readiness and selected preflight

- all six CloseMA `2..7` required;
- all 19 shifts required for each witness;
- different OpenMA per CloseMA allowed;
- stitching rejected;
- deterministic interval/witness tie-break;
- audit created before selected real preflight;
- selected token binds audit hash/size/count/schema/name;
- coverage token alone cannot start;
- exact Start replay succeeds unchanged and fails on any witness, interval,
  accepted-point, audit, or relevant config difference.

### Materialization

- canonical Shift filter;
- factual non-witness MA coverage retained;
- bulk-fetch integrity;
- worker error/cancellation prevents publication;
- `workers=1` and `workers=15` produce identical semantic output and identity;
- saved audit verified before all-side publication and before each side commit;
- real benchmark recorded;
- speedup below `2x` leaves performance work unresolved.

### Geometry and selection

- canonical Shift adjacency shared by refine/Plateau;
- Plateau thresholds remain 90/75/75;
- equivalent group formed before event filter;
- representative ranking exact and permutation-stable;
- invalid CloseSupport operands fail closed;
- support/continuity truth table validated;
- frozen operational facts validate surface and Plateau membership;
- invalid BASE id rejected;
- 2/3/4ORD use frozen representatives only;
- no `DEEP_GAP_RESEARCH`.

### 1ORD and admission

- exact selected-scope list;
- empty candidate list permitted;
- 1ORD-only generation succeeds;
- Top-3 is per exact Pair+Side+TF;
- old/malformed/non-canonical surfaces rejected by analysis, generation and
  parent/rerun entry points;
- malformed/missing canonical contract fields and semantically wrong hash are
  rejected.

### Final smoke

A fresh source-DuckDB run must demonstrate:

```text
coverage scan
-> selected canonical preflight
-> exact Start replay
-> 15-worker materialization
-> immutable canonical publication
-> analysis
-> frozen Plateau facts
-> READY 1ORD/2ORD/3ORD/4ORD JSON
```

with Analysis schema v4 and no legacy-mode fallback.
