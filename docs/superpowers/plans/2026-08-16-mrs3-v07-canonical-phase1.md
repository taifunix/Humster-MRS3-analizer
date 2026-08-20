# MRS3 v0.7 Canonical Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Approved execution plan — Tasks 0–10 complete; Task 11 not started.

**Current execution status (2026-08-17):** Tasks 0–11 complete; Task 12 in progress (12A/12B complete, 12C smoke/performance pending).

**Goal:** Implement the approved canonical Phase 1 surface, readiness, parallel materialization, frozen selection, and independent 1ORD contracts without expanding into Phase 2.

**Audit status (2026-08-17):** Tasks 0–11 are complete and reviewed. Task 12A/12B and the Task 12C synthetic smoke/performance evidence are complete and reviewed; full fresh real-source smoke remains intentionally open.

**Architecture:** The normative contract lives only in `docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md`, backed by ADR-0009. This plan decomposes that contract into small reviewed tasks. It must not introduce a competing contract; where wording conflicts, the approved specification wins.

**Tech Stack:** Python 3.12, DuckDB, pandas, `concurrent.futures.ProcessPoolExecutor`, existing MRS3 dataclasses/storage/panel, pytest.

## Governance gate — Task 0

Before any behavior/code task:

- [x] `docs/decisions/0009-canonical-phase1-surface-selection-contract.md` is independently reviewed and marked `Accepted`.
- [x] `docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md` is independently reviewed and marked `Approved / Active`.
- [x] `PRD.md` registry points to the new spec and ADR as active.
- [x] `progress.md` records Task 0 PASS and names Task 1 as the next action.
- [x] The old accepted ADR-0007/0008 files remain byte-for-byte unchanged by this feature-governance change.
- [x] The old Priority-1 coverage spec remains historical implementation evidence and is not silently rewritten into the new contract.
- [x] Independent governance review returns `PASS`.

**STOP:** if any box above is open, do not execute Task 1.

## Global constraints

All exact values are copied from the canonical specification:

```text
canonical shifts:
30,40,50,60,70,90,110,140,170,200,230,270,310,350,390,430,470,510,550

required CloseMA:
2,3,4,5,6,7

grid kind:
OBSERVED_SPARSE_GRID_CONTRACT_V2

canonical grid version:
mrs3_shift_grid_30_550_v1

readiness version:
close_ma_2_7_canonical_grid_v1

materializer version:
v4-canonical-grid-parallel

point semantics version:
direct_point_materialization_v1

Plateau operational facts version:
cma_representatives_v1

audit schema version:
1

algorithm version:
0.7-canonical-phase1

default materialization workers:
15

Analysis schema:
4
```

Do not add schema v5/new tables, a legacy mode, Event reconstruction changes,
lot-allocation changes, Tester changes, or Phase 2 filters.

Each behavior task follows TDD:

```text
narrow failing test
-> verify RED
-> minimal implementation
-> focused GREEN
-> relevant broader GREEN
-> git diff --check
-> staged diff review
-> independent review
-> fix/retest/re-review if needed
-> one scoped conventional commit
```

---

### Task 1: Make AlgorithmConfig the single canonical algorithm source of truth

## Goal

Define canonical Shift grid, sample calibration, CloseMA threshold, and GAP rules once in tracked algorithm configuration.

## Files

- `src/mrs3/config.py`
- `config.example.json`
- `tests/test_config.py`
- local `config.local.json` only for operator runtime synchronization

`config.local.json` is gitignored and is not normative.

## Checklist — canonical Shift grid

- [x] Add immutable `AlgorithmConfig.canonical_shifts_bp`.
- [x] Default equals exactly:

```python
(
    30, 40, 50, 60, 70,
    90, 110, 140, 170, 200,
    230, 270, 310, 350, 390,
    430, 470, 510, 550,
)
```

- [x] `AlgorithmConfig.from_json()` loads optional `canonical_shifts_bp`.
- [x] Missing JSON field falls back to the code default.
- [x] Do not duplicate the 19-value tuple as a second runtime constant in another module.
- [x] Validate non-empty.
- [x] Validate integer-only.
- [x] Reject `bool`.
- [x] Validate strictly increasing.
- [x] Validate no duplicates.
- [x] Validate first value equals `shift_domain_min_bp`.
- [x] Validate last value equals `shift_domain_max_bp`.
- [x] Change `shift_domain_max_bp` to `550`.

## Checklist — sample calibration

Use:

```text
<=150 -> 1.00
<=200 -> 0.90
<=310 -> 0.30
<=550 -> 0.20
```

- [x] Change final ShiftFactor boundary from `470` to `550`.
- [x] Keep existing earlier factors unchanged.
- [x] Add focused boundary tests at `470`, `510`, and `550`.

## Checklist — CloseMA threshold

- [x] Keep `close_core_min = 0.90`.
- [x] Change `close_supported_min = 0.60`.
- [x] Confirm Plateau `supported_link_min = 0.75`.
- [x] Confirm Plateau `plateau_envelope_min = 0.75`.

## Checklist — GAP rules

Add immutable rules equivalent to:

```json
"gap_rules": [
  {"lower_min_bp": 30,  "lower_max_exclusive_bp": 80,  "min_gap_bp": 80},
  {"lower_min_bp": 80,  "lower_max_exclusive_bp": 200, "min_gap_bp": 100},
  {"lower_min_bp": 200, "lower_max_exclusive_bp": 300, "min_gap_bp": 130},
  {"lower_min_bp": 300, "lower_max_exclusive_bp": 551, "min_gap_bp": 150}
]
```

- [x] Parse from JSON.
- [x] Validate ordered non-overlapping coverage of `30..550`.
- [x] Validate positive minimum gaps.
- [x] New selection runtime reads only `gap_rules`.
- [x] Old GAP fields may remain physically present if removing them broadens the patch, but new selection logic must not use them.

## Checklist — tracked/local config

- [x] Update `config.example.json`.
- [x] Do not treat `config.local.json` as a repository contract.
- [x] Before first real run, operator local config must be synchronized to avoid old overrides.

## Acceptance

- [x] Code default and tracked example load to the exact same canonical tuple.
- [x] Last ShiftFactor boundary is `550`.
- [x] `close_supported_min == 0.60`.
- [x] Plateau 75% thresholds remain unchanged.
- [x] Invalid canonical tuples fail loudly.
- [x] `tests/test_config.py` passes.

---

### Task 2: Implement six-CloseMA canonical readiness

## Goal

For each exact `Symbol + Side + Timeframe`, readiness requires one common continuous UTC interval where CloseMA `2..7` each have one OpenMA covering **all 19 canonical shifts** over the whole interval.

## Files

- `src/mrs3/duckdb_direct.py`
- `tests/test_duckdb_direct.py`

## Required semantics

Allowed:

```text
CMA2 -> OMA3 -> all 19 shifts
CMA3 -> OMA5 -> all 19 shifts
CMA4 -> OMA2 -> all 19 shifts
...
```

Forbidden:

```text
CMA5:
first subinterval  -> OMA2
second subinterval -> OMA3
```

## Checklist — contract/version

- [x] Define `CANONICAL_READINESS_CONTRACT_VERSION = "close_ma_2_7_canonical_grid_v1"` exactly.
- [x] Define `REQUIRED_CLOSE_MAS = (2,3,4,5,6,7)` in one readiness-domain location.
- [x] New runtime does not use old `shift_readiness_v1`.
- [x] Coverage/readiness functions receive exact `required_shifts_bp`.
- [x] Every witness stores the exact request tuple in `shifts_bp`.
- [x] A scope interval stores an ordered six-witness vector sorted by CloseMA.

## Checklist — interval algorithm

- [x] Reuse factual report/grid effective windows.
- [x] Preserve UTC half-open interval semantics.
- [x] Preserve double-zero exclusion.
- [x] Preserve fail-closed behavior for all other empty/invalid intersections.
- [x] Evaluate factual atomic intervals.
- [x] For every candidate interval and each CloseMA `2..7`, find OpenMA values that cover the entire interval for every required canonical shift.
- [x] Select one deterministic OpenMA witness per CloseMA.
- [x] Reject candidate interval if any CloseMA lacks a complete witness.
- [x] Adjacent passing intervals may merge only when recomputation on the merged interval still produces six complete witnesses.

## Checklist — deterministic choice

Select final interval by:

```text
1. longest duration
2. earliest start
3. earliest end
4. lexicographically smallest ordered witness vector:
   (close_ma, open_ma, shifts_bp)
```

- [x] Tie-break is implemented exactly once.
- [x] Input row order does not affect chosen interval/witness vector.

## Checklist — partial MA coverage

- [x] Do not require all OpenMA+CloseMA pairs to be complete.
- [x] Exactly one complete OpenMA witness per exact CloseMA is enough for readiness.
- [x] Additional factual MA combinations remain eligible for later materialization when they fully cover the final selected interval.

## Acceptance tests

- [x] Complete CMA2..7 witnesses -> selectable.
- [x] Missing CMA7 -> not selectable.
- [x] Different OMA per CMA -> selectable.
- [x] OMA stitching required for one CMA -> not selectable.
- [x] Missing exactly Shift 550 in one witness -> not selectable.
- [x] Legacy extra shifts cannot substitute for a missing canonical shift.
- [x] Row permutation produces the same witness vector.
- [x] Focused `test_duckdb_direct.py` readiness tests pass.

---

### Task 3: Freeze exact selected preflight, including audit evidence, and require identical Start-time replay

## Goal

Eliminate token/preflight/audit ambiguity.

The selected preflight returned to the UI is a complete frozen operational
contract. Start is allowed only if unchanged source/config reproduces the same:

```text
requests
intervals
selected scopes
six-witness vectors
accepted point evidence
canonical audit bytes
audit hashes/size/counts/schema/name
preflight facts
```

## Files

- `src/mrs3/duckdb_direct.py`
- `src/mrs3/panel.py`
- `src/mrs3/analysis_storage.py`
- `tests/test_duckdb_direct.py`
- `tests/test_panel.py`
- `tests/test_analysis_storage.py`

## 3.1 Two different tokens

### Stage A — `coverage_scan_token`

Binds the factual scan:

```text
source evidence
coverage inventory
coverage rows
selectability
displayed scope intervals
readiness input/version
```

It proves only that the UI selection came from the current coverage scan.

### Stage B — `preflight_token`

After the user chooses exact scopes, `/api/duckdb-direct/preflight` must create
a **new** selected preflight state and return a **new** token.

The selected-scope preflight endpoint must no longer return the coverage token
as if it were the final preflight token.

## 3.2 Exact Stage-B transaction/order

Run the entire Stage-B freeze against one source transaction/snapshot.

For every selected side, in deterministic LONG-before-SHORT order:

```text
1. verify coverage_scan_token against a fresh scan in the same source snapshot
2. compute exact final common interval for selected Pair+Side+TF scopes
3. create exact DirectBuildRequest with:
   - exact canonical 19-shift tuple
   - exact selected scopes
   - exact canonical spec §§5–6 grid/readiness/materializer versions
4. generate canonical coverage audit CSV bytes for that exact request
5. compute:
   - audit_sha256
   - audit_size_bytes = len(audit_bytes)
   - audit_row_count
   - audit_schema_version = 1
   - audit_artifact_name = surface_coverage_audit_<SIDE>.csv
6. attach the exact audit metadata AND raw audit bytes to DirectBuildRequest
7. run real preflight_duckdb_direct() using that audit-bound request
8. require no unavailable selected scope
9. freeze exact request + exact DirectPreflight
10. write/verify the audit artifact from the same frozen bytes
```

Only after all selected sides pass may Stage B expose a `preflight_token`.

- [x] Audit bytes are created **before** the real selected preflight.
- [x] Preflight sees the exact audit bytes/hash/count/schema/name that will later be published.
- [x] If audit artifact writing/verification fails, no selected token is issued.
- [x] If any side fails, no partial selected-preflight state/token is accepted.

## 3.3 Frozen selected-preflight state

The controller must keep one immutable in-memory selected state sufficient for
Start-time replay. Logical shape:

```text
coverage_scan_token
ordered exact DirectBuildRequest tuple
ordered exact DirectPreflight tuple
preflight_token
```

The exact dataclass name may differ.

Each frozen request already carries:

```text
audit_bytes
audit_sha256
audit_size_bytes
audit_row_count
audit_schema_version
audit_artifact_name
```

- [x] Raw audit bytes are frozen server-side with the selected state.
- [x] The token document does not need to embed raw CSV bytes, but must bind their SHA-256, byte size, row count, schema version and artifact name.
- [x] A panel restart or loss of the in-memory frozen state invalidates the token; do not reconstruct it heuristically.

## 3.4 Exact `preflight_token` binding

The token must bind canonical serialization of at least:

```text
coverage_scan_token
CANONICAL_GRID_CONTRACT_KIND
CANONICAL_GRID_VERSION
CANONICAL_READINESS_CONTRACT_VERSION
CANONICAL_MATERIALIZER_VERSION
exact required_shifts_bp
exact selected scopes
exact final per-side UTC intervals
exact ordered six-witness vectors per scope
source hashes/evidence
accepted point keys
manifest / point evidence hash
coverage inventory hash
audit_artifact_name
audit_schema_version
audit_size_bytes
audit_row_count
audit_sha256
```

- [x] Changing one witness OpenMA changes token.
- [x] Changing interval changes token.
- [x] Changing scope changes token.
- [x] Changing accepted point evidence changes token.
- [x] Changing audit bytes/hash/size/count changes token.
- [x] Changing any exact contract identifier from canonical spec §§5–6 changes token.

## 3.5 Start API rule

The coverage-token path must not directly start a build anymore.

Start must require:

```text
preflight_token
```

and the corresponding still-live frozen selected state.

- [x] `coverage_token` alone cannot start materialization.
- [x] Start payload selected scopes must match the frozen state exactly if scopes are repeated in the payload.
- [x] Parent-surface validation occurs only after the selected token/state is validated.

## 3.6 Start-time source transaction/replay

At Start, open one fresh source transaction/snapshot.

Before materialization:

```text
1. rerun coverage scan and require exact coverage_scan_token equality
2. for each frozen exact request:
   a. regenerate canonical audit bytes from the active source
   b. require active_audit_bytes == frozen_request.audit_bytes byte-for-byte
   c. require identical audit SHA/size/count/schema/name
   d. re-read the persisted audit artifact and require it still equals the frozen bytes/metadata
   e. build the active audit-bound request
   f. rerun preflight_duckdb_direct(active_request)
   g. require active_request semantic fields == frozen request
   h. require active_preflight == frozen preflight
3. only after every side matches exactly may materialization start
```

Any mismatch:

```text
STALE_PREFLIGHT
→ fail before publication
```

There is no exception for:

```text
different but still-valid witness
narrower interval
wider interval
different audit bytes with “equivalent” contents
new/removed point evidence
changed config/version
```

## 3.7 Mandatory persisted-audit verification immediately before publication

Because materialization may run for minutes or hours, the Start-time artifact
check is not sufficient. The **physical saved audit file** must be verified
again immediately before publication.

### Required publish API

The canonical path must provide `audit_root` to publication, conceptually:

```python
publish_direct_surfaces(
    analysis_connection,
    surfaces,
    *,
    audit_root: str | os.PathLike[str],
    cancellation=...,
    progress_callback=...,
    parent_surface_id=None,
)
```

For canonical V2 surfaces `audit_root` is mandatory.

### Exact artifact path

For each side:

```text
<audit_root>/
  surface_coverage/
    <audit_sha256>/
      surface_coverage_audit_<SIDE>.csv
```

The file name must exactly equal the frozen `audit_artifact_name`.

### Exact verification helper

Use one helper, conceptually:

```python
verify_persisted_surface_audit(
    audit_root,
    surface,
) -> bytes
```

It must read the file from disk and verify all of the following against
`DirectBuildRequest`, `DirectPreflight`, and `grid_contract_json`:

- [x] artifact exists and is a regular readable file;
- [x] artifact name exactly equals `surface_coverage_audit_<SIDE>.csv`;
- [x] schema version exactly equals `1`;
- [x] `audit_size_bytes == len(file_bytes)`;
- [x] SHA-256 of file bytes equals frozen `audit_sha256`;
- [x] file bytes equal frozen `preflight.audit_bytes` byte-for-byte;
- [x] `_audit_data_row_count(file_bytes) == audit_row_count`;
- [x] file is LF-terminated as required by existing V2 audit validation;
- [x] request/preflight/grid-contract audit name/schema/size/count/hash metadata are identical;
- [x] no missing audit metadata is tolerated.

### Two publish-time gates

`publish_direct_surfaces()` must perform:

```text
Gate A:
verify persisted audit files for ALL prepared sides
before publishing the first side

then for each side in LONG-before-SHORT order:

Gate B:
re-read and re-verify that side's persisted audit file
immediately before its publish_surface() call
```

Reason:

- Gate A prevents an already-corrupt SHORT artifact from causing an avoidable
  `PARTIAL` result after LONG commits.
- Gate B protects against the artifact changing during a long materialization
  or between Gate A and the individual side commit.

Failure semantics:

```text
Gate A failure before any commit
→ publish zero surfaces

Gate B failure before first side
→ publish zero surfaces

Gate B failure for later side after an earlier side committed
→ existing PARTIAL semantics
```

Do not silently rewrite the audit file during these checks. Verification is
read-only and fail-closed.

### Required tests

- [x] missing saved audit file -> zero publication before first commit;
- [x] one-byte saved-file mutation -> fail;
- [x] saved-file SHA mismatch -> fail;
- [x] saved-file byte-size mismatch -> fail;
- [x] row-count mismatch -> fail;
- [x] schema-version mismatch -> fail;
- [x] artifact-name/metadata mismatch -> fail;
- [x] frozen in-memory bytes correct but on-disk file wrong -> fail;
- [x] all files correct -> normal publication;
- [x] later-side mutation after earlier commit produces truthful `PARTIAL`.

## 3.8 Canonical materialization selection



For each frozen accepted request publish every factual point that:

```text
belongs to selected Symbol+Side+TF
covers the whole final interval
AND
shift_bp in request.required_shifts_bp
```

- [x] Do not limit materialization to the six witness MA pairs.
- [x] Retain factual non-witness MA combinations when they fully cover the frozen interval.
- [x] Non-canonical shifts never enter the new surface.
- [x] Source DuckDB is never modified.

## 3.9 Persisted grid/audit contract

Keep Analysis schema v4.

`grid_contract_json` must carry the exact canonical operational facts, including:

```text
kind = OBSERVED_SPARSE_GRID_CONTRACT_V2
canonical_grid_version = mrs3_shift_grid_30_550_v1
canonical_shifts_bp = exact 19-shift tuple
readiness_contract_version = close_ma_2_7_canonical_grid_v1
selected scopes
exact six witnesses per scope
point/source evidence
audit_artifact_name
audit_schema_version = 1
audit_size_bytes
audit_row_count
audit_sha256
```

`DirectPreflight.audit_bytes` must contain the corresponding raw canonical CSV
bytes until publication verification completes.

- [x] `_verify_v2_audit()` verifies raw bytes against persisted hash/size/count.
- [x] Storage validates witness shifts against persisted canonical shifts.
- [x] Storage validates exactly six ordered unique CloseMA `2..7` witnesses.
- [x] No independent second 19-shift constant is introduced in storage.

## Acceptance

- [x] Selected preflight creates audit bytes before calling real preflight.
- [x] Selected preflight freezes `audit_size_bytes` in request/preflight/token/grid contract.
- [x] Persisted audit files are re-read and verified before any publish.
- [x] Each side's persisted audit file is re-read immediately before its own commit.
- [x] Selected preflight returns a new token, not the coverage scan token.
- [x] Start with unchanged source/config/audit reproduces exact frozen state.
- [x] One-byte audit difference -> stale/fail.
- [x] Same interval but different witness -> stale/fail.
- [x] Same witnesses but changed manifest/point evidence -> stale/fail.
- [x] Changed canonical config/version -> stale/fail.
- [x] Coverage token alone cannot start.
- [x] Canonical+legacy source -> surface contains canonical shifts only.
- [x] Partial non-witness MA coverage survives when factual/full-interval.
- [x] Malformed six-witness/audit persisted contract is rejected.

---

### Task 4: Parallelize DUCKDB_DIRECT materialization with 15 worker processes

## Goal

Benchmark and, if the evidence supports it, reduce post-Start materialization
time with bounded CPU parallelism while preserving one source snapshot,
deterministic output, and exactly the same surface semantics.

## Baseline to benchmark

Current `materialize_duckdb_direct()` does this sequentially for every accepted report:

```text
one SQL SELECT by report_id
→ decode timestamps
→ decode actions
→ decode equity
→ decode wallet
→ calculate metrics
→ reconstruct closed cycles/events
→ append DirectPoint
```

This produces:

```text
N reports -> N SQL queries
1 Python materialization worker
repeated timestamp-grid decoding
```

Phase 1 evaluates a target of **15 worker processes** rather than assuming all
available cores are suitable. The target workstation benchmark is required
before any speedup claim.

## Files

- `src/mrs3/duckdb_direct.py`
- `src/mrs3/config.py`
- `config.example.json`
- `src/mrs3/panel.py`
- `tests/test_duckdb_direct.py`
- `tests/test_panel.py`

## 4.1 Operational settings — not algorithm identity

Add separate operational settings. Do **not** put them into `AlgorithmConfig` algorithm identity.

Recommended exact contract:

```python
@dataclass(frozen=True, slots=True)
class DirectMaterializationSettings:
    workers: int = 15
    fetch_batch_size: int = 256
    worker_chunk_size: int = 16
    max_in_flight_chunks: int = 30
```

Tracked example:

```json
"direct_materialization": {
  "workers": 15,
  "fetch_batch_size": 256,
  "worker_chunk_size": 16,
  "max_in_flight_chunks": 30
}
```

- [x] Add loader/validation in `config.py`.
- [x] `workers >= 1`.
- [x] `fetch_batch_size >= 1`.
- [x] `worker_chunk_size >= 1`.
- [x] `max_in_flight_chunks >= workers`.
- [x] Default worker count is exactly `15`.
- [x] Settings may be overridden locally.
- [x] These settings do **not** participate in:
  - surface identity;
  - point materialization semantic hash;
  - algorithm config hash;
  - grid contract.

Changing workers from `1` to `15` must not create a semantically different surface.

## 4.2 One DuckDB snapshot only

Do **not** open 15 DuckDB source connections.

Required architecture:

```text
MAIN PROCESS
one read-only DuckDB transaction/snapshot
        ↓
batched payload SELECT
        ↓
pickle-safe chunks
        ↓
ProcessPoolExecutor(max_workers=15)
        ↓
CPU decode/metrics/events
        ↓
MAIN PROCESS canonical merge/sort/validation
        ↓
publish later
```

- [x] Main process owns the only source DuckDB connection used by materialization.
- [x] Worker processes receive payload bytes/metadata, never DuckDB connections.
- [x] Worker processes do not write source or analysis DuckDB.
- [x] Existing one-transaction prepare semantics remain intact.

## 4.3 Remove N+1 report queries

Replace one-query-per-report behavior with bounded bulk reads.

- [x] Read accepted manifest rows in batches using `fetch_batch_size`.
- [x] Each bulk result must include all payloads/metadata needed by workers.
- [x] Verify every requested `(report_id, source_hash)` is returned exactly once.
- [x] Missing/duplicate/mismatched source evidence fails closed as “source changed after preflight”.
- [x] Do not load the full source DB into RAM unnecessarily.
- [x] Keep memory bounded by batching + `max_in_flight_chunks`.

## 4.4 Worker chunk contract

Use module-level, Windows-spawn-safe worker functions.

Recommended logical worker API:

```python
def _materialize_payload_chunk(
    chunk: tuple[MaterializationPayload, ...],
    window_start_utc: str,
    window_end_utc: str,
) -> tuple[DirectPoint, ...]:
    ...
```

The exact dataclass name may differ.

- [x] Worker callable is module-level and pickle-safe.
- [x] No lambda/closure captures a DuckDB connection.
- [x] Inputs contain only primitive/pickle-safe metadata and bytes.
- [x] Worker returns deterministic `DirectPoint` facts only.
- [x] Worker does not depend on completion order.

## 4.5 Reuse timestamp grid by `grid_hash`

Payload batch must carry `grid_hash`.

Within a worker chunk:

- [x] Group reports by `grid_hash` where practical.
- [x] Decode one timestamps blob once per unique `grid_hash` in that chunk.
- [x] Reuse the resulting UTC grid for all reports sharing that hash.
- [x] Do not cache grids globally without a bounded-memory policy.
- [x] Grid reuse must not change metric/event semantics.

## 4.6 Parallel CPU work

Each worker performs the existing logic unchanged:

```text
decode_compact_actions
decode_compact_deltas(equity)
decode_wallet_changes
calculate_point_metrics
reconstruct_closed_cycles
unique sorted event_ids
DirectPoint creation
```

- [x] Do not alter the formulas in `calculate_point_metrics`.
- [x] Do not alter `reconstruct_closed_cycles`.
- [x] Do not alter `canonical_event_id`.
- [x] `PointEventCount == len(unique event_ids)` remains exact.

## 4.7 Bounded scheduling

- [x] Use `ProcessPoolExecutor(max_workers=settings.workers)`.
- [x] Default is 15.
- [x] Limit outstanding work to `max_in_flight_chunks`.
- [x] Do not enqueue the entire database as thousands of futures at once.
- [x] Continue fetching/scheduling as completed work frees capacity.

## 4.8 Cancellation and failure

- [x] Main process checks cancellation before fetch, before schedule, and while collecting futures.
- [x] On cancellation, stop scheduling new chunks.
- [x] Cancel pending futures where possible.
- [x] Shut down executor with `cancel_futures=True`.
- [x] Any worker exception fails the whole materialization before publication.
- [x] Partial materialized points must never be published.

## 4.9 Deterministic merge

Worker completion order is irrelevant.

Main process must:

- [x] collect all `DirectPoint`s;
- [x] canonical-sort by `point_key_tuple(canonical_point_key)`;
- [x] validate unique canonical point keys;
- [x] validate side;
- [x] validate point count against accepted manifest;
- [x] return the same logical `DirectSurface` as single-worker mode.

## 4.10 Progress telemetry

Expose lightweight progress without redesigning the UI.

At minimum track:

```text
phase
side
materialized_points
total_points
workers
elapsed_seconds
points_per_second
```

- [x] `MATERIALIZING` progress updates while futures complete.
- [x] Panel snapshot exposes current count and total.
- [x] Existing status UI may render simple text; no new dashboard is required.
- [x] Progress telemetry does not affect deterministic identity.

## 4.11 Mandatory determinism regression

For the same frozen preflight/request:

```text
workers = 1
workers = 15
```

must produce identical:

```text
canonical point keys
metrics
PointEventCount
event_ids
source_report_id
source_hash
point ordering after canonical sort
grid contract
point evidence
surface identity
```

- [x] Add explicit `workers=1 vs workers=15` test.
- [x] Random worker completion order does not change output.

## 4.12 Benchmark

Use one representative real-source request large enough that current sequential materialization is non-trivial.

Record:

```text
accepted point count
workers
fetch_batch_size
worker_chunk_size
single-worker MATERIALIZE time
15-worker MATERIALIZE time
speedup
publish time
total time
```

- [x] Baseline measured with `workers=1`.
- [x] Same exact request measured with `workers=15`.
- [x] Output equivalence verified before comparing time.
- [x] Target: materialization should improve by multiple times, not merely a few percent.
- [x] If measured speedup is `< 2x`, Task 4 is not accepted without profiling evidence identifying the remaining bottleneck.
- [x] If publish becomes the dominant phase after parallelization, record it; do not silently broaden this Task into a DB schema redesign.

## Task 4 acceptance

- [x] Default materialization workers = 15.
- [x] One source DuckDB snapshot is preserved.
- [x] N+1 report SELECT pattern is removed.
- [x] Workers perform CPU-heavy decode/metric/event work.
- [x] Timestamp grids are reused by `grid_hash` within chunks.
- [x] `workers=1` and `workers=15` outputs are identical.
- [x] Real benchmark demonstrates meaningful speedup.
- [x] No partial publication on worker error/cancel.

---

### Task 5: Replace Shift-neighbor semantics with canonical adjacency

## Goal

Use adjacency in the canonical Shift tuple as the only Shift-neighbor definition for new refine and Plateau geometry.

## Files

- `src/mrs3/refine.py`
- `src/mrs3/plateau.py`
- `tests/test_refine.py`
- `tests/test_plateau.py`

## Checklist

- [x] Implement one shared canonical adjacency helper.
- [x] Distinct shifts are neighbors only if adjacent elements in `config.canonical_shifts_bp`.
- [x] `30 <-> 40`.
- [x] `70 <-> 90`.
- [x] `90 <-> 110`.
- [x] `470 <-> 510`.
- [x] `510 <-> 550`.
- [x] `70` and `110` are not neighbors.
- [x] `140` and `200` are not neighbors.
- [x] `430` and `510` are not neighbors.
- [x] Refine required shifts are current + immediate canonical left/right.
- [x] No shift below 30 or above 550 is requested.
- [x] Plateau uses the same helper; do not duplicate neighbor rules.
- [x] Keep existing OpenMA/CloseMA radius/domain behavior unchanged.
- [x] Keep diagonal MA adjacency unchanged.
- [x] Do not add Missing Cells exports in Phase 1.

## Acceptance

- [x] Shared adjacency tests pass.
- [x] Refine and Plateau give the same shift-neighbor answer.
- [x] Plateau thresholds remain `0.90 / 0.75 / 0.75`.

---

### Task 6: Build exactly one CMARepresentative per Plateau + exact CloseMA

## Goal

For every READY Plateau and every exact CloseMA present inside it, choose at most one frozen representative using the approved equivalence/event order.

## Files

- `src/mrs3/selection.py`
- `tests/test_selection.py`

## Checklist — candidate pool

For each:

```text
Plateau + exact CloseMA
```

- [x] Use all Plateau member points with that CloseMA.
- [x] Do not fix Shift to a global primary point.
- [x] Do not restrict OpenMA to a radius around a global primary point.
- [x] First filter to `economic_pass == True`.

## Checklist — economic reference

Reference ordering remains:

```text
PnL DESC
PnL/DD DESC
Trades DESC
DD ASC
PointID ASC
```

- [x] Reuse `_reference_row()` semantics where possible.
- [x] Do not use EventCount to choose the economic reference.

## Checklist — 5% equivalent group

- [x] Build equivalent group against that CMA-specific economic reference.
- [x] Equivalence uses both PnL and efficiency.
- [x] Keep existing 5% tolerance semantics.
- [x] Do not invent a new equivalence formula.

## Checklist — Event filter after equivalence

- [x] Only after the 5% equivalent group exists, remove `event_eligible == False`.
- [x] Effective hard floor remains `PointEventCount >= 3`.
- [x] If no rows remain, this Plateau+CloseMA has no representative.

## Checklist — representative ranking

Choose exactly one from remaining rows by:

```text
PointEventCount DESC
Shift DESC
PnL DESC
PnL/DD DESC
Trades DESC
DD ASC
PointID ASC
```

- [x] Selection deterministic under row permutation.
- [x] No later stage may rerun a different representative search.

## Acceptance

- [x] EventCount 6 / lower Shift beats EventCount 4 / higher Shift.
- [x] If EventCount ties, higher Shift wins.
- [x] Event-ineligible point cannot become representative.
- [x] Event filter is after equivalent-group construction.
- [x] One Plateau+CloseMA yields at most one representative.

---

### Task 7: Primary CloseMA, 90/60 support, continuity, and validated frozen operational facts

## Goal

Choose primary only after representatives exist, enforce continuity, and persist a **versioned, validated frozen operational contract** in existing `plateaus.metrics_json`.

No new Analysis DB table/schema.

## Files

- `src/mrs3/selection.py`
- `src/mrs3/pipeline.py`
- `src/mrs3/analysis_storage.py`
- `src/mrs3/analysis_strategies.py` for read-time validation helper reuse if appropriate
- `tests/test_selection.py`
- `tests/test_analysis_storage.py`
- `tests/test_analysis_strategies.py`

## 7.1 Primary

Choose primary only among existing CMARepresentatives using:

```text
PnL DESC
PnL/DD DESC
Trades DESC
DD ASC
PointID ASC
```

- [x] Exactly one primary when representatives exist.
- [x] Primary support = `1.0`.
- [x] Primary status = `PRIMARY_CLOSE`.

## 7.2 CloseSupport

For every other representative:

```text
support_pnl = CMA_PnL / Primary_PnL
support_eff = CMA_Efficiency / Primary_Efficiency
CloseSupport = min(support_pnl, support_eff)
```

### 7.2.1 Fail-closed numeric preconditions

Do not divide until all operands have been converted with:

```python
Decimal(str(value))
```

and validated.

For the selected Primary representative:

```text
Primary_PnL        must be finite AND > 0
Primary_Efficiency must be finite AND > 0
```

For every non-primary CMARepresentative used in support calculation:

```text
CMA_PnL        must be finite AND > 0
CMA_Efficiency must be finite AND > 0
```

If any of these invariants fail:

```text
raise ValueError / fail the analysis run
```

Do **not**:

```text
coerce denominator to epsilon
coerce invalid support to 0
coerce invalid support to 1
emit Infinity/NaN
silently skip that CMA
silently choose a different Primary
classify the corrupt value as merely UNSUPPORTED_CLOSE
```

This is an invariant failure. On a valid pipeline it should be unreachable
because `economic_pass` already requires positive PnL, positive DD and
sufficient positive efficiency.

After division:

```text
support_pnl
support_eff
CloseSupport
```

must each be finite and strictly positive.

Because Primary is selected first by `PnL DESC` and then
`Efficiency DESC` on ties, valid `CloseSupport` must satisfy:

```text
0 < CloseSupport <= 1
```

If computed `CloseSupport > 1` or is non-finite/non-positive, fail closed rather
than clamping.

### 7.2.2 Threshold classification

- [x] `>= 0.90` -> `CORE_CLOSE`.
- [x] `0.60 <= x < 0.90` -> `SUPPORTED_CLOSE`.
- [x] `< 0.60` -> `UNSUPPORTED_CLOSE`.
- [x] Do not modify Plateau 75% thresholds.

### 7.2.3 Required numeric tests

- [x] Primary PnL = `0` -> fail.
- [x] Primary PnL < `0` -> fail.
- [x] Primary PnL = `NaN` -> fail.
- [x] Primary PnL = `+/-Infinity` -> fail.
- [x] Primary efficiency = `0` -> fail.
- [x] Primary efficiency < `0` -> fail.
- [x] Primary efficiency = `NaN` -> fail.
- [x] Primary efficiency = `+/-Infinity` -> fail.
- [x] Non-primary numerator non-positive/non-finite -> fail.
- [x] Valid positive finite inputs preserve the exact 90/60 classifications.
- [x] No invalid case emits a persisted representative profile.

## 7.3 Continuity

Walk separately upward and downward from Primary CloseMA.

- [x] Primary is usable.
- [x] Next adjacent CloseMA is usable only if representative exists and status is PRIMARY/CORE/SUPPORTED.
- [x] Missing representative breaks continuity in that direction.
- [x] Unsupported representative breaks continuity in that direction.
- [x] Do not jump over a break.
- [x] Outer raw-support values cannot restore continuity after a break.

## 7.4 Exact embedded operational contract

Persist these logical fields in Plateau metrics:

```text
operational_facts_version
primary_close_ma
cma_representatives
base_1ord_point_id
```

Required version:

```text
operational_facts_version = "cma_representatives_v1"
```

Representative shape is exact:

```json
[
  {
    "close_ma": 4,
    "point_id": "PAIR|SIDE|TF|SHIFT|OMA|4",
    "support": 0.83,
    "support_status": "SUPPORTED_CLOSE",
    "continuity_status": "USABLE",
    "usable": true
  }
]
```

Allowed `support_status` values only:

```text
PRIMARY_CLOSE
CORE_CLOSE
SUPPORTED_CLOSE
UNSUPPORTED_CLOSE
```

Allowed `continuity_status` values only:

```text
USABLE
BREAK_UNSUPPORTED
BLOCKED_BY_CONTINUITY
```

- [x] Serialize representatives as a list strictly ordered by `close_ma`.
- [x] Do not use an unordered object keyed by CloseMA.
- [x] `close_ma` values unique.
- [x] `point_id` values unique.
- [x] Primary `support == 1.0`; every non-primary persisted `support` is finite and satisfies `0 < support <= 1`.
- [x] Exactly one `PRIMARY_CLOSE`.
- [x] `primary_close_ma` equals that entry's `close_ma`.

## 7.5 Semantic status validation — mandatory truth table

Raw `support` determines `support_status` independently of continuity.

### Primary row

Exactly:

```text
close_ma == primary_close_ma
support == 1.0 within numeric_tolerance
support_status == PRIMARY_CLOSE
continuity_status == USABLE
usable == true
```

Any other combination is invalid.

### Non-primary raw support class

Exactly:

```text
support >= 0.90
→ CORE_CLOSE

0.60 <= support < 0.90
→ SUPPORTED_CLOSE

support < 0.60
→ UNSUPPORTED_CLOSE
```

A non-primary row may never have `PRIMARY_CLOSE`.

### Continuity traversal

Recompute continuity independently downward and upward from primary.

Before a break:

```text
next integer CloseMA exists
AND support_status in {CORE_CLOSE, SUPPORTED_CLOSE}
→ continuity_status = USABLE
→ usable = true
```

First present unsupported representative before any earlier break:

```text
support_status = UNSUPPORTED_CLOSE
→ continuity_status = BREAK_UNSUPPORTED
→ usable = false
→ continuity breaks in that direction
```

If the next integer CloseMA is missing:

```text
continuity breaks immediately
```

There is no representative row for the missing CMA itself.

Every existing outer representative after either:

```text
a missing intermediate CMA
OR
an earlier BREAK_UNSUPPORTED
```

must be:

```text
continuity_status = BLOCKED_BY_CONTINUITY
usable = false
```

regardless of its raw `support_status`.

### Required equivalence

Always:

```text
usable == (continuity_status == "USABLE")
```

- [x] `UNSUPPORTED_CLOSE + USABLE` rejected.
- [x] `CORE_CLOSE/SUPPORTED_CLOSE + BREAK_UNSUPPORTED` rejected.
- [x] `PRIMARY_CLOSE + BLOCKED_BY_CONTINUITY` rejected.
- [x] `BLOCKED_BY_CONTINUITY + usable=true` rejected.
- [x] `BREAK_UNSUPPORTED + usable=true` rejected.
- [x] Raw numeric support inconsistent with support_status rejected.
- [x] Outer high-support representative after a continuity break remains blocked.
- [x] Validation recomputes continuity from sorted representatives; it does not trust stored flags.

## 7.6 Publication-time structural and semantic validation

Before `publish_analysis_run()` commits Plateau rows, validate operational facts
against immutable surface facts, Plateau membership, and the truth table above.

For each representative:

- [x] `point_id` exists in `surface_points` for this surface.
- [x] `point_id` belongs to this Plateau's `all_point_ids`.
- [x] `point_id` belongs to this run's `plateau_members` logical set being published.
- [x] Parsed point `symbol/side/timeframe` matches Plateau scope.
- [x] Parsed point `close_ma` equals representative `close_ma`.
- [x] Representative ordering strictly increasing.
- [x] Duplicate CloseMA rejected.
- [x] Duplicate point_id rejected.
- [x] Unknown operational facts version rejected.
- [x] Unknown support_status rejected.
- [x] Unknown continuity_status rejected.
- [x] Complete semantic truth-table validation passes.

## 7.7 Freeze local BASE point

For every READY Plateau:

Candidate pool:

```text
only continuity-usable frozen CMARepresentatives
AND source point standalone_eligible == True
```

- [x] Choose at most one local BASE point.
- [x] Use existing equivalent/default selection semantics only over this already-frozen candidate pool.
- [x] Store exact `point_id` as `base_1ord_point_id`.
- [x] If none qualifies, persist `null` / no BASE deterministically.
- [x] Do not inspect arbitrary Plateau standalone points later.

## 7.8 Validate `base_1ord_point_id`

If non-null:

- [x] Exists in `surface_points`.
- [x] Exists in Plateau membership.
- [x] Exists exactly once in `cma_representatives`.
- [x] Corresponding representative has `continuity_status == "USABLE"`.
- [x] Corresponding representative has `usable == true`.
- [x] Point belongs to `standalone_eligible_point_ids` stored for the Plateau.
- [x] Point scope matches Plateau.
- [x] Invalid BASE id causes publication/read failure; do not reselect replacement.

## 7.9 Read-time validation

Any consumer relying on frozen facts must apply the same structural and semantic validator.

- [x] Analysis strategy generation rejects missing/unknown operational facts version.
- [x] It rejects malformed representatives.
- [x] It rejects semantically contradictory statuses/usable flags.
- [x] It rejects invalid `base_1ord_point_id`.
- [x] It never repairs persisted facts by recomputing representatives.

## Acceptance

- [x] Missing intermediate CMA breaks continuity.
- [x] Unsupported intermediate CMA becomes `BREAK_UNSUPPORTED`.
- [x] Outer representative after either break becomes `BLOCKED_BY_CONTINUITY`.
- [x] Contradictory support/status pair rejected.
- [x] Contradictory continuity/usable pair rejected.
- [x] Malformed representative ordering rejected.
- [x] Representative outside surface rejected.
- [x] Representative outside Plateau rejected.
- [x] Wrong representative CloseMA rejected.
- [x] Invalid BASE id rejected.
- [x] Publish/readback preserves exact frozen facts.
- [x] `ANALYSIS_SCHEMA_VERSION` remains `4`.

---

### Task 8: Build 2/3/4ORD only from frozen usable CMARepresentatives

## Goal

No equivalent-point Cartesian re-enumeration after frozen representative selection.

## Files

- `src/mrs3/selection.py`
- `tests/test_selection.py`

## Checklist

For each:

```text
Pair + Side + TF + CommonCloseMA
```

- [x] Plateau contributes at most one frozen representative for that exact CloseMA.
- [x] Representative must be continuity-usable.
- [x] Do not re-run equivalent search.
- [x] Do not substitute another Shift.
- [x] Do not substitute another OpenMA.
- [x] Generate 2ORD independently.
- [x] Generate 3ORD independently.
- [x] Generate 4ORD independently up to `max_orders`.
- [x] Every order uses a distinct `plateau_id`.
- [x] Sort by Shift before hard validation.
- [x] Order 1 must be `standalone_eligible == True`.
- [x] Orders 2–4 must be `depth_eligible == True`.
- [x] Preserve exact PointEventCount in order facts.
- [x] Preserve downstream EQUAL/INCOME behavior.

## Acceptance

- [x] One Plateau never contributes two orders to one structure.
- [x] Frozen point X cannot be replaced by equivalent point Y.
- [x] 2/3/4-order universes are independent.

---

### Task 9: Replace GAP validation for the entire 0.3–5.5% domain

## Goal

Apply configured GAP rules to every adjacent sorted order pair and remove special deep-gap runtime behavior.

## Files

- `src/mrs3/selection.py`
- `src/mrs3/strategy_json.py` only where current hard validation consumes status
- `tests/test_selection.py`
- `tests/test_strategy_json.py`

## Required rules

```text
30 <= left < 80    -> minimum gap 80 bp
80 <= left < 200   -> minimum gap 100 bp
200 <= left < 300  -> minimum gap 130 bp
300 <= left <=550  -> minimum gap 150 bp
```

## Checklist

- [x] Resolver reads `config.gap_rules`.
- [x] Resolver uses smaller/left sorted Shift.
- [x] Every adjacent pair is checked.
- [x] `actual_gap < required_gap` -> `GAP_TOO_SMALL`.
- [x] All pairs pass -> `READY_MRS3_STRUCTURE`.
- [x] New runtime never emits `DEEP_GAP_RESEARCH`.
- [x] New runtime does not read `deep_gap_boundary_bp`.
- [x] Full `30..550` domain is ordinary production space.

## Acceptance

- [x] `70 -> 140`: reject.
- [x] `70 -> 170`: pass.
- [x] `170 -> 270`: pass.
- [x] `200 -> 310`: reject.
- [x] `200 -> 350`: pass.
- [x] `310 -> 430`: reject.
- [x] `310 -> 470`: pass.

---

### Task 10: Define and implement exact independent 1ORD generation API

## Goal

Make BASE 1ORD generation independent of multiorder candidate existence and eliminate all ambiguity about scope/API/job behavior.

## Files

- `src/mrs3/analysis_strategies.py`
- `src/mrs3/panel.py`
- `src/mrs3/pipeline.py` / `src/mrs3/selection.py` only for already-frozen facts
- `tests/test_analysis_strategies.py`
- `tests/test_panel.py`

## 10.1 Exact generator signature

Change to this logical contract:

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

Tuple order is exactly:

```text
(symbol, side, timeframe)
```

- [x] Normalize to sorted unique tuples.
- [x] `selected_scopes` empty -> error.
- [x] Every scope must exist in the run's canonical published surface.
- [x] Every scope side must equal the surface/run side.
- [x] `candidate_ids` may be empty.

## 10.2 Panel payload

The `/api/analysis/strategies` request must send exact selected scopes:

```json
"selected_scopes": [
  {
    "symbol": "ONUSDT",
    "side": "LONG",
    "timeframe": "15m"
  }
]
```

- [x] No placeholder side inference inside the generator.
- [x] Controller validates exact objects.
- [x] Controller stores normalized exact tuples in `_StrategyJob.selected_scopes`.
- [x] `_StrategyJob` gains `selected_scopes`.
- [x] Snapshot/status may expose selected scopes for auditability.

## 10.3 Scope source must not depend on 2–4ORD candidates

Current shortlist facets/scopes are candidate-driven. That is insufficient for 1ORD-only scopes.

- [x] Analysis shortlist/panel scope source is derived from the canonical published run/surface plus persisted Plateau BASE facts.
- [x] A scope with `base_1ord > 0` and `2ORD=3ORD=4ORD=0` still appears/selects correctly.
- [x] Shortlist scope rows may include a `base_1ord` count.
- [x] Existing 2/3/4ORD counts remain visible/usable.
- [x] No UI redesign beyond adding truthful scope/count data.

## 10.4 Candidate IDs

For exact `selected_scopes`:

- [x] Controller collects READY 2–4ORD candidate IDs inside those scopes.
- [x] Active Phase 2 shortlist criteria continue to affect multiorder candidates only.
- [x] Empty `candidate_ids` is allowed.
- [x] Supplied candidate outside `selected_scopes` -> error.
- [x] Supplied candidate not READY -> error.
- [x] Supplied candidate absent from run -> error.

## 10.5 Frozen BASE read

For every READY Plateau in selected scope:

- [x] Read versioned operational facts from persisted `metrics_json`.
- [x] Read `base_1ord_point_id`.
- [x] Do not enumerate/re-rank `standalone_eligible_point_ids`.
- [x] Validate BASE id using Task 7 contract.
- [x] Resolve exact immutable source point from published surface.
- [x] One Plateau contributes at most one BASE candidate.

## 10.6 Top-3 BASE

Group by exact:

```text
symbol + side + timeframe
```

Rank:

```text
PnL@DD5 theoretical DESC
raw PnL DESC
Trades DESC
DD ASC
PointID ASC
```

- [x] Select at most 3 per exact scope.
- [x] Do not add Shift/EventCount as Top-3 dimensions.
- [x] Distinct Plateaus are guaranteed by one frozen local BASE per Plateau.

## 10.7 Output rules

- [x] `1ORD -> EQUAL only`.
- [x] `2–4ORD -> EQUAL + INCOME`.
- [x] If BASE exists and no multiorder candidate exists -> success.
- [x] If multiorder exists and BASE does not -> success.
- [x] If both exist -> generate both.
- [x] If neither BASE nor selected READY multiorder exists -> error.
- [x] Strategy JSON schema unchanged.
- [x] Lot formulas unchanged.

## 10.8 Job lifecycle

Keep:

```text
STARTING
→ GENERATING
→ COMMITTED
or FAILED
```

- [x] No new async workflow state machine is required.
- [x] `candidate_ids=()` must not fail before generator runs.
- [x] `selected_scopes` is frozen on job creation.
- [x] Job error is explicit if no output exists.

## 10.9 Manifest

Add deterministic fields to `strategy_manifest.json`:

```text
selected_scopes
ready_structure_count
base_1ord_count
strategy_count
```

- [x] `selected_scopes` serialized in sorted canonical order.
- [x] Manifest reflects 1ORD-only runs correctly.

## Acceptance

- [x] Exact one-scope 1ORD-only request succeeds.
- [x] `candidate_ids=[]` + valid BASE -> succeeds.
- [x] Empty selected scopes -> fails.
- [x] Candidate outside scope -> fails.
- [x] Five BASE Plateaus in one scope -> max three BASE JSONs.
- [x] Raw non-frozen standalone point can never replace frozen BASE.
- [x] Scope with no multiorder candidates remains visible/selectable for BASE.

---

### Task 11: Reject any surface that does not match the exact canonical operational contract

## Goal

Remove the ambiguous concept of a “materializer version family”.

Historical surfaces may remain stored, but the new operational flow accepts
only one exact Phase 1 contract.

## Files

- `src/mrs3/analysis_storage.py`
- `src/mrs3/published_surface.py`
- `src/mrs3/pipeline.py`
- `src/mrs3/panel.py`
- `src/mrs3/analysis_strategies.py`
- focused existing tests

## 11.1 Exact canonical operational predicate

A surface is operationally valid for new Phase 1 analysis/generation/parent use
only if **all** of the following are true:

```text
build_mode
== "DUCKDB_DIRECT"

event_mode
== "real_independent_events"

grid_contract_json.kind
== "OBSERVED_SPARSE_GRID_CONTRACT_V2"

grid_contract_json.canonical_grid_version
== "mrs3_shift_grid_30_550_v1"

grid_contract_json.canonical_shifts_bp
== (
  30,40,50,60,70,
  90,110,140,170,200,
  230,270,310,350,390,
  430,470,510,550
)

grid_contract_json.readiness_contract_version
== "close_ma_2_7_canonical_grid_v1"

materializer_version
== "v4-canonical-grid-parallel"

normalization_contract_version
== current NORMALIZATION_CONTRACT_VERSION used by the code

grid_contract_json.point_materialization_semantics_version
== "direct_point_materialization_v1"

point_materialization_config_hash
== exact SHA-256 returned by the single shared canonical spec §§5–6 helper after canonical JSON serialization
```

In addition:

```text
each selected scope has exactly six ordered unique witnesses
CloseMA sequence == (2,3,4,5,6,7)
each witness shifts_bp == persisted canonical_shifts_bp
audit schema/hash/count contract is valid
point/source evidence contract is valid
```

There is no alternative accepted materializer version.

## 11.2 One validator/helper

Use one shared hard validator, conceptually:

```python
require_canonical_operational_surface(...)
```

All operational entry points must call the same validator rather than each
implementing a partial check.

## 11.3 Entry-point guards

- [x] New analysis run validates surface before selection.
- [x] READY JSON generation validates the run's backing surface.
- [x] Parent/rerun materialization validates parent surface.
- [x] New direct build emits only the exact contract above.
- [x] Historical rows may remain visible/stored.
- [x] No legacy mode toggle.
- [x] No automatic migration/republication.
- [x] Error says a fresh canonical surface must be built.

## 11.4 Required malformed/missing tests

Each case below must fail independently:

- [x] missing `grid_contract_json`.
- [x] missing `kind`.
- [x] wrong `kind`.
- [x] missing `canonical_grid_version`.
- [x] wrong `canonical_grid_version`.
- [x] missing `canonical_shifts_bp`.
- [x] wrong Shift tuple/order/value.
- [x] missing readiness version.
- [x] wrong readiness version.
- [x] missing materializer version.
- [x] old materializer version.
- [x] arbitrary `"v4-canonical-grid-parallel-hotfix"` value.
- [x] missing/wrong `point_materialization_semantics_version`.
- [x] syntactically valid 64-char but wrong point-materialization semantic hash.
- [x] semantic hash made from pretty JSON / newline / omitted field / extra field is rejected.
- [x] canonical helper hash is accepted.
- [x] wrong event mode.
- [x] malformed/missing six-witness vector.
- [x] witness Shift tuple differing from persisted canonical tuple.
- [x] malformed/missing audit evidence.
- [x] malformed/missing point evidence.

## Important

This Task is not backward compatibility. It is a strict admission guard.

An old surface sharing the same V2 `kind` is still rejected if any newer exact
canonical field/version/hash is absent or wrong.

## Acceptance

- [x] Fresh exact Phase 1 surface accepted.
- [x] Old `30/150/430` surface rejected.
- [x] Old V2 surface with same `OBSERVED_SPARSE_GRID_CONTRACT_V2` kind but no canonical grid version rejected.
- [x] Surface with correct grid but wrong materializer version rejected.
- [x] Surface-backed old run rejected by strategy generation.
- [x] Old parent surface rejected.
- [x] No Analysis schema migration required.

---

### Task 12: Final versioning, focused regressions, performance evidence, and smoke

## Goal

Prove the entire Phase 1 path works end-to-end without broadening into Phase 2.

## Files

- affected source files from Tasks 1–11
- affected existing test modules
- `src/mrs3/pipeline.py` for algorithm version
- `progress.md` only after verified evidence exists

## 12.1 Versioning

- [x] Set `ALGORITHM_VERSION = "0.7-canonical-phase1"` exactly.
- [x] Set direct materializer version = `"v4-canonical-grid-parallel"` exactly.
- [ ] Point materialization semantic/config hash uses the exact canonical spec §§5–6 payload, canonical JSON serialization and SHA-256 algorithm.
- [x] Worker count/batch sizes do not participate in semantic identity.
- [x] `ANALYSIS_SCHEMA_VERSION` remains `4`.

## 12.2 Required focused regression groups

### Config

- [x] canonical tuple validation;
- [x] ShiftFactor through 550;
- [x] 90/60 CloseMA threshold;
- [x] GAP config validation;
- [x] materialization settings validation.

### Coverage/readiness

- [x] CMA2..7 common interval;
- [x] all 19 shifts required;
- [x] different OMA per CMA allowed;
- [x] OMA stitching rejected;
- [x] deterministic witness tie-break.

### Token/preflight

- [x] exact semantic-hash payload canonical JSON fixture/digest test;
- [x] selected preflight creates and freezes exact audit bytes before real preflight;
- [x] selected token binds audit hash/size/count/schema/name;
- [x] saved audit file is verified before all-side publication and immediately before each side commit;
- [x] coverage token alone cannot start;
- [x] selected token freezes interval + scopes + witnesses;
- [x] Start exact replay succeeds;
- [x] witness change makes token stale;
- [x] accepted-point evidence change makes token stale;
- [x] canonical config change makes token stale.

### Materialization correctness/performance

- [x] canonical Shift filter;
- [x] factual non-witness MA coverage retained;
- [x] bulk fetch integrity;
- [x] worker failure prevents publication;
- [x] cancellation prevents publication;
- [x] `workers=1 == workers=15` semantic output;
- [x] benchmark evidence recorded.

### Refine/Plateau

- [x] canonical adjacency;
- [x] Plateau thresholds remain 90/75/75.

### CMARepresentative

- [x] 5% equivalence before Event filter;
- [x] ranking exact;
- [x] deterministic under row permutation.

### Frozen operational facts

- [x] Primary PnL/efficiency zero, negative and non-finite fail closed;
- [x] non-primary invalid support operands fail closed;
- [x] version validation;
- [x] support_status vs numeric support truth-table validation;
- [x] continuity_status/usable truth-table validation;
- [x] strict representative order;
- [x] duplicate representative rejection;
- [x] surface membership validation;
- [x] Plateau membership validation;
- [x] representative CloseMA match;
- [x] BASE id validation;
- [x] publish/readback identity.

### Structures/GAP

- [x] structures use only frozen reps;
- [x] no Plateau duplicated;
- [x] GAP examples;
- [x] no `DEEP_GAP_RESEARCH`.

### 1ORD API

- [x] exact selected scope list;
- [x] empty candidate IDs allowed;
- [x] 1ORD-only success;
- [x] candidate outside scope rejected;
- [x] Top-3 exact per Pair+Side+TF;
- [x] frozen BASE only.

### Old surface guard

- [x] exact semantic payload/version/hash recomputation admission check;
- [x] exact grid-contract kind/version/readiness/materializer admission checks;
- [x] malformed/missing canonical contract fields rejected;
- [x] old surface rejected from analysis;
- [x] old surface-backed run rejected from JSON generation;
- [x] old parent rejected.

## 12.3 Fresh canonical smoke

Using source DuckDB and fresh output path:

- [ ] Coverage scan completes.
- [ ] Selectable scope requires CMA2..7 on one common interval.
- [ ] Selected preflight returns exact canonical token.
- [ ] Start replays exact preflight.
- [ ] Materialization runs with 15 workers.
- [ ] Progress count increases during materialization.
- [ ] Published surface contains only canonical shifts.
- [ ] Published surface contains exact real-event membership.
- [ ] Analysis run completes.
- [ ] Plateau metrics contain versioned frozen CMA facts.
- [ ] 2/3/4ORD use frozen reps.
- [ ] 1ORD-only scope can generate JSON.
- [ ] 1ORD uses EQUAL only.
- [ ] 2–4ORD use EQUAL + INCOME.
- [ ] No `DEEP_GAP_RESEARCH`.
- [ ] Analysis schema remains v4.

## 12.4 Performance record

Record in `progress.md` only after measurement:

```text
source/request identifier
accepted point count
workers=1 materialization seconds
workers=15 materialization seconds
speedup
publish seconds
total seconds
```

- [x] Do not write estimated performance as verified fact.
- [x] If speedup `< 2x`, keep Task 4 unresolved and profile bottleneck before claiming completion.

## 12.5 Final review

- [x] Focused affected suites green.
- [x] Relevant direct/panel/storage/selection/strategy suites green.
- [x] Any unrelated repository-wide failures documented separately.
- [x] `git diff --check` clean.
- [x] Independent reviewer returns PASS/APPROVE.
- [x] `progress.md` updated with exact verified test/benchmark counts only.

---

# 2. Explicitly excluded from Phase 1

Reviewer checks these only after confirming they were **not** introduced.

- [x] No `Refine_Missing_Cells.csv`.
- [x] No `Refine_Required_Points.csv` export.
- [x] No full Before/After Event Filter audit.
- [x] No new Plateau diagnostic DB tables.
- [x] No dedicated `close_profiles` DB table.
- [x] No Analysis schema v5.
- [x] No Phase 2 Pareto/redundancy redesign.
- [x] No Event reconstruction redesign.
- [x] No lot allocation redesign.
- [x] No strategy JSON schema redesign.
- [x] No Tester changes.
- [x] No actual fill-order diagnostic.
- [x] No event-bridge hard filter.
- [x] No CoreSize hard gate.
- [x] No full UI redesign.
- [x] No selectable legacy mode.
- [x] No automatic old-surface migration.
- [x] No worker-specific surface identity.
- [x] No omission of persisted audit byte-size metadata from the canonical audit contract.

---

# 3. Required execution order

Do not give all tasks to the coding model at once.

```text
Governance gate  normative contract alignment
  -> independent review PASS required

Task 1  canonical config
  -> focused tests
  -> review

Task 2  six-CloseMA canonical readiness
  -> focused tests
  -> review

Task 3  exact selected token / real-preflight contract
  -> focused tests
  -> review

Task 4  parallel materialization, 15 workers
  -> determinism tests
  -> real benchmark
  -> review

Task 5  canonical adjacency
  -> tests
  -> review

Task 6  CMARepresentative
  -> tests
  -> review

Task 7  support / continuity / validated frozen facts / local BASE freeze
  -> storage roundtrip tests
  -> review

Task 8  frozen multiorder structures
  -> tests
  -> review

Task 9  GAP
  -> tests
  -> review

Task 10 exact independent 1ORD API
  -> analysis_strategies + panel tests
  -> review

Task 11 old-surface operational guard
  -> focused tests
  -> review

Task 12 final versions / regressions / smoke / benchmark
  -> final independent review
```

Task 4 may be implemented after Task 3 because it changes **how** accepted points are computed, not **which** points are accepted.

---

# 4. Coder/reviewer completion register

## Contract

- [x] Governance gate complete.
- [x] Governance gate independently reviewed PASS.

## Canonical direct surface

- [x] Task 1 complete/reviewed.
- [x] Task 2 complete/reviewed.
- [x] Task 3 complete/reviewed.
- [x] Task 4 complete/reviewed.

## Analysis geometry/selection

- [x] Task 5 complete/reviewed.
- [x] Task 6 complete/reviewed.
- [x] Task 7 complete/reviewed.
- [x] Task 8 complete/reviewed.
- [x] Task 9 complete/reviewed.

## Strategy/API

- [x] Task 10 complete/reviewed.

Task 10 checklist verification addendum (2026-08-17): the implementation and focused tests confirm READY multi-order candidate collection is restricted to the selected scopes, Phase 2 shortlist criteria apply only to multi-order candidates, and multi-order outputs produce both EQUAL and INCOME variants. The three legacy checklist rows above contain mojibake in the source file; this addendum records their verified completion without changing the contract.

## Operational guard

- [x] Task 11 complete/reviewed.

Task 11 verification addendum (2026-08-17): the shared canonical operational-surface validator is wired into analysis generation, READY strategy generation, parent/rerun handling, published-surface access, and direct-build admission. Focused tests pass (193 tests). HY3 was invoked in read-only mode for independent review; no material finding was surfaced in the completed reviewer run.

## Final

- [ ] Task 12 complete.
- [ ] Final independent review PASS.

---

# 5. Phase 1 Definition of Done

This is an execution checklist derived from the approved Phase 1 specification;
it does not add or override normative requirements. Phase 1 is complete only
when every required box below is satisfied.

## 5.1 Canonical readiness

- [x] Exact 19-shift grid `30..550`.
- [x] Non-canonical source shifts excluded from new surface.
- [x] AlgorithmConfig is the primary grid source of truth.
- [x] Readiness requires CMA2..7.
- [x] All six CMA use one common exact interval.
- [x] Every witness has all 19 shifts.
- [x] Different OMA per CMA allowed.
- [x] OMA stitching forbidden.

## 5.2 Preview/Start correctness

- [x] Coverage-scan token and selected-preflight token have distinct meanings.
- [x] Selected preflight creates/fixes audit bytes before real preflight.
- [x] Selected token binds audit hash/size/count/schema/name.
- [x] Physical saved audit file is verified before the first publication and immediately before each side commit.
- [x] Coverage token alone cannot start materialization.
- [x] Final selected token freezes exact interval/scopes/witnesses/evidence.
- [x] Start reproduces exact frozen preflight.
- [x] Any witness difference after token issuance fails stale.
- [x] Materialization uses the frozen accepted manifest.

## 5.3 Materialization performance

- [x] Default worker count = 15.
- [x] One DuckDB source snapshot retained.
- [x] N+1 SELECT removed.
- [x] CPU-heavy work runs in process workers.
- [x] Timestamp grid reuse implemented by `grid_hash` within chunks.
- [x] Output is identical for 1 and 15 workers.
- [x] Real benchmark recorded.
- [x] Measured speedup is at least 2x or Task remains unresolved.

## 5.4 Geometry/events

- [x] Canonical Shift adjacency used by refine and Plateau.
- [x] Plateau thresholds remain 90/75/75.
- [x] Event implementation unchanged.
- [x] PointEventCount hard floor remains >=3.

## 5.5 CloseMA/frozen facts

- [x] At most one CMARepresentative per Plateau+CloseMA.
- [x] 5% equivalence before Event filter.
- [x] Representative ranking exact.
- [x] Close support 90/60.
- [x] Invalid/non-positive/non-finite Primary PnL or efficiency fails closed before CloseSupport division.
- [x] Continuity enforced.
- [x] Operational facts versioned.
- [x] Operational facts validated against surface and Plateau membership.
- [x] support_status is validated against numeric support.
- [x] continuity_status and usable are validated by deterministic continuity replay.
- [x] Frozen BASE id validated.
- [x] No schema v5/new table.

## 5.6 Structures/GAP

- [x] 2/3/4ORD only from frozen usable reps.
- [x] Distinct Plateau per order.
- [x] New GAP rules applied.
- [x] No `DEEP_GAP_RESEARCH`.

## 5.7 1ORD

- [x] Exact `selected_scopes` API exists.
- [x] Scope tuple is `(symbol, side, timeframe)`.
- [x] Empty `candidate_ids` allowed.
- [x] 1ORD-only generation succeeds.
- [x] Scope list is not candidate-only.
- [x] BASE reads frozen `base_1ord_point_id`.
- [x] Top-3 per exact scope.
- [x] 1ORD EQUAL only.

## 5.8 Old surfaces

- [x] Exact canonical admission contract is enforced: V2 kind + canonical grid version + exact 19 shifts + readiness version + exact materializer version + semantic hash.
- [x] Missing/malformed admission fields are rejected.
- [x] Old stored surfaces are not deleted automatically.
- [x] Old surfaces rejected from new analysis.
- [x] Old surface-backed runs rejected from new JSON generation.
- [x] Old parent surfaces rejected from canonical rerun.
- [x] No legacy mode.

## 5.9 Final technical state

- [x] Algorithm version bumped.
- [x] Direct materializer version bumped.
- [x] Worker settings excluded from identity.
- [x] Point-materialization semantic hash is defined by one exact payload, one exact canonical JSON serialization and SHA-256.
- [x] Analysis schema remains v4.
- [ ] Fresh canonical surface -> analysis -> READY JSON smoke passes.
- [x] Independent reviewer returns PASS.

---

# 6. Reviewer stop conditions

These are review gates derived from the approved ADR and specification. They
are not a second behavioral contract.

Reviewer must return **REVISE** if any condition below is observed:

```text
ADR-0007 is not explicitly superseded for its one-witness V2 readiness/evidence shape
Governance gate incorrectly attributes concrete 30/150/430 rules directly to ADR-0007
active normative docs still say new runtime uses 30/150/430
one global MA witness instead of six CMA2..7 witnesses
canonical grid duplicated independently in multiple runtime modules
selected preflight creates audit bytes after preflight instead of before it
semantic hash payload uses 'at least' / optional fields or has multiple runtime implementations
semantic hash serialization/digest algorithm is not exact canonical JSON + SHA-256
selected preflight token does not bind audit hash/size/count/schema/name
saved audit file is not re-read immediately before publication
coverage token can start materialization without selected preflight token
selected preview token does not freeze exact witness vector
Start may choose a different witness after token issuance
Start may silently change final interval
non-canonical Shift enters new surface
parallel workers open independent source DuckDB snapshots
workers=1 and workers=15 produce different semantic surfaces
worker count changes surface identity
materialization still performs one SQL SELECT per report
CMARepresentative recomputed during structure generation
frozen operational facts have no version/shape validation
CloseSupport divides by zero/negative/non-finite Primary PnL or efficiency
invalid CloseSupport operands are coerced/clamped/skipped instead of fail-closed
support_status does not match numeric support threshold
continuity_status/usable combinations are semantically contradictory
representative point need not belong to surface/Plateau
invalid base_1ord_point_id is silently replaced
CloseMA continuity can jump over a missing/unsupported CMA
Plateau 0.75 threshold accidentally becomes 0.60
new runtime emits DEEP_GAP_RESEARCH
1ORD generator requires a 2–4ORD candidate
selected_scopes are not exact Pair+Side+TF
1ORD-only scope cannot appear/select because shortlist is candidate-only
canonical surface admission uses a materializer 'version family' instead of exact version
missing/wrong point_materialization_semantics_version or recomputed semantic hash is accepted
missing/malformed canonical grid/readiness/materializer fields are accepted
old surface can enter new analysis/generation without rejection
Analysis schema v5/new tables introduced without new approval
```

If none of these stop conditions exists and all required acceptance checks are satisfied, Phase 1 may proceed to the first full tester universe.
