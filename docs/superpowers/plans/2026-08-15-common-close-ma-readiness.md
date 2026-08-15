# Common Close-MA Readiness Implementation Plan

**Status:** Frozen project draft. Do not execute. Before activation, revise this
plan against the frozen follow-up scope in the canonical specification.

> **For future revision only:** After the status is explicitly activated, use
> superpowers:subagent-driven-development or superpowers:executing-plans. The
> current checkbox tasks are design input, not executable instructions.

**Goal:** Implement ADR-0008 so direct coverage uses one exact interval shared
by Close MA `2..7` and ignores only structurally double-zero source rows.

**Architecture:** Reuse the existing report loader, atomic interval scan,
pair-level shift evaluator, CSV writer, and V2 storage. Filter double-zero rows
once in `_reports()`, replace the single witness with an ordered six-witness
vector, and branch V2 validation by readiness-contract version so V1 and
already-published V2 surfaces remain valid.

**Tech Stack:** Python 3.12, DuckDB, existing dataclasses and canonical
JSON/CSV helpers, pytest.

## Global Constraints

- Use Ponytail `full`: no new module, dependency, migration, table, endpoint,
  generalized invalid-row framework, or speculative abstraction.
- Pair-level shift readiness remains `shift_readiness_v1` with exact boundaries
  `30/150/430`, maximum gaps `<=10/<=40 bp`, and maximum shift `430 bp`.
- New scope-level V2 readiness is
  `close_ma_2_7_common_interval_v1`; Close MA `2..7` each require one complete
  pair-level witness over the same interval.
- One Close MA cannot stitch different Open MAs across subintervals. Different
  Close MAs may select different Open MAs.
- Preserve V1 byte/identity behavior and legacy V2 single-witness validation.
- Keep `COVERAGE_CSV_COLUMNS`, statuses, reasons, sorting, and encoding unchanged.
- This draft does not yet cover `coverage_summary.csv`, ignored-row diagnostics,
  partial per-MA interval display, preflight progress UI, source-pass
  optimization, or preview/real-preflight lifecycle changes. Its mandatory
  pre-execution revision must incorporate those frozen follow-up requirements.

---

### Task 1: Isolate Structurally Degenerate Rows

**Files:**
- Modify: `src/mrs3/duckdb_direct.py:217-241`
- Test: `tests/test_duckdb_direct.py:298-330`
- Modify after verification: `progress.md`

**Interfaces:**
- Produces: `_is_structurally_degenerate(row: Mapping[str, object]) -> bool`
- Consumed by: `_reports()` before selected-scope filtering

- [ ] **Step 1: Add failing admission tests**

Add
`test_coverage_ignores_only_report_and_grid_both_zero_duration` and parameterized
`test_coverage_fails_closed_for_one_sided_zero_and_disjoint_windows` using the
existing `_seed_report` helper. The first test must compare the result with and
without a double-zero row and prove no contribution to coverage rows,
inventory bytes, audit bytes, token source evidence, accepted point keys, or V2
evidence. Include a case where the report and grid zero timestamps differ. The
second test covers report-only zero, grid-only zero, and two non-empty disjoint
windows.

```python
assert coverage_with_zero.rows == baseline.rows
assert b"double-zero" not in inventory_bytes
with pytest.raises(DirectMaterializationError, match="empty report/grid intersection"):
    list_duckdb_direct_coverage(source, symbols=())
```

- [ ] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -k "both_zero_duration or one_sided_zero or disjoint_windows" -q
```

Expected: the double-zero case fails because `_effective_window()` currently
raises; existing fail-closed counterexamples pass.

- [ ] **Step 3: Add the single narrow filter**

```python
def _is_structurally_degenerate(row: Mapping[str, object]) -> bool:
    return (
        int(row["report_period_start_ms"]) == int(row["report_period_end_ms"])
        and int(row["start_timestamp_ms"]) == int(row["end_timestamp_ms"])
    )
```

Filter the rows once in `_reports()` before `selected_scopes` handling. Do not
change `_effective_window()`; every remaining `end <= start` still raises.

- [ ] **Step 4: Verify GREEN and regression scope**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -k "both_zero_duration or one_sided_zero or disjoint_windows" -q
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -q
```

- [ ] **Step 5: Review and commit**

Update `progress.md` with the exact focused result, run
`git diff --cached --check`, inspect only the owned files, obtain independent
review, fix confirmed findings, rerun affected tests, and re-review.

```powershell
git add src/mrs3/duckdb_direct.py tests/test_duckdb_direct.py progress.md
git commit -m "fix: isolate degenerate DuckDB coverage rows"
```

---

### Task 2: Build One Common Close-MA Interval

**Files:**
- Modify: `src/mrs3/duckdb_direct.py:35-123,421-643,657-710,900-1020`
- Test: `tests/test_duckdb_direct.py:330-590,837-1030`

**Interfaces:**
- Produces: `PAIR_READINESS_CONTRACT_VERSION = "shift_readiness_v1"`
- Produces: `SCOPE_READINESS_CONTRACT_VERSION = "close_ma_2_7_common_interval_v1"`
- Produces: `REQUIRED_CLOSE_MAS = (2, 3, 4, 5, 6, 7)`
- Produces: `CoverageInterval.witnesses: tuple[ReadinessWitness, ...]`
- Produces: `_scope_witnesses(rows: Sequence[dict[str, object]], start_ms: int,
  end_ms: int) -> tuple[ReadinessWitness, ...] | None`

- [ ] **Step 1: Add a six-Close test seeder**

Reuse the existing pair seeder rather than adding production fixtures.

```python
def _seed_common_close_scope(source, *, open_by_close=None, **kwargs):
    choices = open_by_close or {close_ma: 2 for close_ma in range(2, 8)}
    for close_ma in range(2, 8):
        _seed_readiness_scope(
            source,
            open_ma=choices[close_ma],
            close_ma=close_ma,
            **kwargs,
        )
```

- [ ] **Step 2: Add failing readiness and tie-break tests**

Add these focused tests:

- `test_readiness_requires_close_ma_2_through_7_on_one_common_interval`
- `test_readiness_allows_different_open_ma_per_close`
- `test_readiness_rejects_open_ma_stitching_for_one_close`
- `test_readiness_selects_longest_common_interval_then_earliest_then_witness_vector`
- `test_coverage_token_changes_when_one_close_witness_changes`

```python
assert row.selectable is False  # one required Close MA is absent
assert tuple(w.close_ma for w in interval.witnesses) == (2, 3, 4, 5, 6, 7)
assert tuple(w.open_ma for w in interval.witnesses) == (2, 3, 4, 5, 6, 7)
```

- [ ] **Step 3: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -k "close_ma_2_through_7 or open_ma_stitching or witness_vector or token_changes" -q
```

Expected: the current single-pair candidate path enables incomplete scopes and
`CoverageInterval` has only `witness`.

- [ ] **Step 4: Separate pair and scope versions**

Keep each `ReadinessWitness.contract_version` equal to
`PAIR_READINESS_CONTRACT_VERSION`. Use
`SCOPE_READINESS_CONTRACT_VERSION` only in the request, coverage CSV, and V2
grid-contract scope metadata.

- [ ] **Step 5: Replace per-pair candidates with scope candidates**

Implement `_scope_witnesses()` by grouping passing pair witnesses by Close MA,
choosing the lexicographically smallest `(open_ma, shifts_bp)` per Close, and
returning `None` unless the ordered Close sequence equals `REQUIRED_CLOSE_MAS`.

In `_direct_coverage()`, evaluate each atomic segment once with
`_scope_witnesses()`. Merge adjacent passing segments only when recomputation
over the combined interval still returns six witnesses. Order candidates by:

```python
(
    -(end_ms - start_ms),
    start_ms,
    end_ms,
    tuple((w.close_ma, w.open_ma, w.shifts_bp) for w in witnesses),
)
```

- [ ] **Step 6: Extend token and existing CSV flags**

Serialize every interval's ordered witness vector into the coverage token.
Update `_evaluation_rows_for_scope()` so only the six selected pair/witness
sequences have `required_for_readiness=true` and `readiness_witness=true`.
An additional passing Open MA must remain optional. Emit the scope contract
version without changing the CSV columns or reason grammar.

Add `test_coverage_csv_marks_six_selected_witnesses_without_schema_change` and
assert `tuple(csv_header) == COVERAGE_CSV_COLUMNS`.

- [ ] **Step 7: Verify the task**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -k "readiness or common_interval or coverage_token or coverage_csv" -q
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -q
```

- [ ] **Step 8: Review and commit**

Run staged diff checks and mandatory independent review as in Task 1.

```powershell
git add src/mrs3/duckdb_direct.py tests/test_duckdb_direct.py
git commit -m "feat: require common Close MA readiness"
```

---

### Task 3: Persist Six Witnesses Without Breaking Existing Surfaces

**Files:**
- Modify: `src/mrs3/duckdb_direct.py:1217-1329`
- Modify: `src/mrs3/analysis_storage.py:580-631,788-860`
- Modify: `src/mrs3/panel.py:1683-1702`
- Test: `tests/test_duckdb_direct.py:837-950`
- Test: `tests/test_analysis_storage.py:180-230,700-860`
- Test: `tests/test_panel.py:250-490`

**Interfaces:**
- Legacy V2: `witnesses[scope]` remains one object when the top-level readiness
  version is `shift_readiness_v1`.
- New V2: `witnesses[scope]` is one ordered six-item JSON array when the
  top-level readiness version is `close_ma_2_7_common_interval_v1`.
- `point_evidence` remains the existing canonical JSONL string.

- [ ] **Step 1: Add failing V2 evidence tests**

Add:

- `test_v2_preflight_persists_six_ordered_common_close_witnesses`
- `test_common_close_v2_identity_changes_with_any_witness`
- `test_common_close_v2_rejects_missing_duplicate_or_unordered_witnesses`
- `test_legacy_v2_single_witness_surface_remains_valid`
- `test_v1_surface_identity_and_validation_remain_unchanged`
- `test_direct_coverage_request_uses_common_close_scope_contract`

The new per-scope JSON shape is exactly:

```json
[
  {"close_ma":2,"contract_version":"shift_readiness_v1","max_shift_bp":430,"open_ma":3,"shifts_bp":[30,40,50,60,70,80,90,100,110,120,130,140,150,190,230,270,310,350,390,430]},
  {"close_ma":3,"contract_version":"shift_readiness_v1","max_shift_bp":430,"open_ma":4,"shifts_bp":[30,40,50,60,70,80,90,100,110,120,130,140,150,190,230,270,310,350,390,430]},
  {"close_ma":4,"contract_version":"shift_readiness_v1","max_shift_bp":430,"open_ma":2,"shifts_bp":[30,40,50,60,70,80,90,100,110,120,130,140,150,190,230,270,310,350,390,430]},
  {"close_ma":5,"contract_version":"shift_readiness_v1","max_shift_bp":430,"open_ma":5,"shifts_bp":[30,40,50,60,70,80,90,100,110,120,130,140,150,190,230,270,310,350,390,430]},
  {"close_ma":6,"contract_version":"shift_readiness_v1","max_shift_bp":430,"open_ma":6,"shifts_bp":[30,40,50,60,70,80,90,100,110,120,130,140,150,190,230,270,310,350,390,430]},
  {"close_ma":7,"contract_version":"shift_readiness_v1","max_shift_bp":430,"open_ma":7,"shifts_bp":[30,40,50,60,70,80,90,100,110,120,130,140,150,190,230,270,310,350,390,430]}
]
```

The array contains exactly six objects ordered by Close MA `2..7`.
Symbol/timeframe come from the scope key and side comes from the surface.

- [ ] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py tests/test_analysis_storage.py tests/test_panel.py -k "common_close or legacy_v2 or direct_coverage_request" -q
```

- [ ] **Step 3: Emit and validate the two compatible V2 forms**

In `_preflight_duckdb_direct_v2()`, emit the six-item list for the new scope
contract. In `analysis_storage.py`, branch only on the top-level
`readiness_contract_version`:

```python
if version == PAIR_READINESS_CONTRACT_VERSION:
    _validate_v2_witness(scope, value, side)
elif version == SCOPE_READINESS_CONTRACT_VERSION:
    _validate_v2_witness_vector(scope, value, side)
else:
    raise ValueError("V2 readiness contract is unsupported")
```

The vector validator requires a list of exactly six objects, unique strictly
ordered Close MA `2..7`, positive Open MA, per-pair contract
`shift_readiness_v1`, `max_shift_bp=430`, and a valid canonical witness shift
sequence. Do not change `_surface_identity()`; canonical `grid_contract_json`
already makes the vector identity-bearing.

- [ ] **Step 4: Wire only the request constant in the panel**

Update `PanelController._direct_coverage_request()` to use the new scope
contract. Add no markup, endpoint, response field, or panel behavior.

- [ ] **Step 5: Verify compatibility**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py tests/test_analysis_storage.py tests/test_panel.py -q
.venv\Scripts\python.exe -m pytest tests/test_duckdb_source_schema.py tests/test_published_surface.py -q
```

- [ ] **Step 6: Review and commit**

Run staged diff checks and mandatory independent review as in Task 1.

```powershell
git add src/mrs3/duckdb_direct.py src/mrs3/analysis_storage.py src/mrs3/panel.py tests/test_duckdb_direct.py tests/test_analysis_storage.py tests/test_panel.py
git commit -m "feat: persist common Close MA witnesses"
```

---

### Task 4: Verify and Record the Implemented Contract

**Files:**
- Modify: `docs/specs/2026-08-14-duckdb-surface-coverage-review.md`
- Modify: `PRD.md`
- Modify: `progress.md`

- [ ] **Step 1: Run relevant and full verification**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py tests/test_analysis_storage.py tests/test_panel.py tests/test_duckdb_source_schema.py tests/test_duckdb_import.py tests/test_published_surface.py -q
.venv\Scripts\python.exe -m pytest -q
git diff --check
git status --short
```

Any full-suite failures must be investigated. Record actual counts and identify
pre-existing missing-fixture failures separately; do not accept new failures.

- [ ] **Step 2: Update status documents only after evidence exists**

Set the canonical spec and PRD row to `Implemented / Verified`. In
`progress.md`, record exact commands/counts, remove the ADR-0008 runtime warning,
and preserve unrelated Tester Report Library status. Do not edit ADR-0008 or
README.

- [ ] **Step 3: Final review and documentation commit**

Run `git diff --cached --check`, inspect the complete staged documentation,
obtain independent review, fix confirmed findings, and re-review.

```powershell
git add docs/specs/2026-08-14-duckdb-surface-coverage-review.md PRD.md progress.md
git commit -m "docs: record common Close MA readiness verification"
```

## Draft Omissions Requiring Revision

This frozen draft does not yet implement `coverage_summary.csv`, degenerate-row
diagnostics, per-MA partial interval reporting, Check coverage progress/elapsed
feedback, stale-token UI clearing, audit-link presentation, queue error/journal
presentation, scan-pass optimization, or the preview/real-preflight lifecycle.
These omissions must be resolved in the required revision before execution.
Retry/lease infrastructure and schema changes remain separately deferred unless
that revision supplies a newly approved requirement.
