# Source v6 Selected-scope Materialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decode only the selected READY scopes before materialization, while preserving exact empty-result semantics.

**Architecture:** Preflight metadata already identifies a scope's fragment ids and the source database already persists its whole-corpus digest. Add a bounded selected-id variant of the existing deterministic process reader, then pass the selected hydrated facts into the unchanged materializer. This avoids the invalid metadata predicate shortcut and retains all E1--E5 checks.

**Tech Stack:** Python 3, DuckDB, `concurrent.futures.ProcessPoolExecutor`, pytest.

**Spec:** `docs/specs/2026-08-22-source-v6-selected-scope-materialization.md`

## Global Constraints

- Run tests only with `.venv\Scripts\python.exe -m pytest`.
- Do not hydrate unselected payloads.
- Do not replace `measure_points` with a metadata/count predicate.
- Preserve deterministic `fragment_id` order and immutable surface identity.
- No Source DB, HTML, credentials or generated artifact is committed.

---

### Task 1: Deterministic selected-id reader

**Files:**
- Modify: `src/mrs3/source_v6_storage.py:decode_fragment_slice, iter_fragments_parallel`
- Test: `tests/test_source_v6_storage.py`

**Interfaces:**
- Produces: `iter_fragment_ids_parallel(path, ids, *, workers=1, chunk_size=256, progress_callback=None) -> tuple[SourceV6Fragment, ...]`
- Consumes: existing `decode_fragment_slice(path, ids)`.

- [x] **Step 1: Write the failing test**

```python
def test_parallel_selected_ids_matches_requested_subset(tmp_path):
    selected = (stored[3].fragment_id, stored[0].fragment_id)
    actual = iter_fragment_ids_parallel(database, selected, workers=2)
    assert [item.fragment_id for item in actual] == sorted(selected)
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests\test_source_v6_storage.py::test_parallel_selected_ids_matches_requested_subset -q`

Expected: FAIL because the selected-id reader does not exist.

- [x] **Step 3: Write minimal implementation**

```python
def iter_fragment_ids_parallel(path, ids, *, workers=1, chunk_size=256, progress_callback=None):
    ordered = tuple(sorted(set(ids)))
    # reuse decode_fragment_slice and the existing ordered-slice executor
```

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

### Task 2: Panel publication decodes selected scopes only

**Files:**
- Modify: `src/mrs3/panel_surfaces.py:LocalSurfacesService.publish,_run_publish`
- Test: `tests/test_panel_surfaces.py`

**Interfaces:**
- Consumes: `iter_fragment_ids_parallel(path, ids, workers, progress_callback)` and the validated `_Pending.metadata`.
- Produces: publication jobs whose `HYDRATING` total equals selected fragment count.

- [x] **Step 1: Write the failing test**

```python
def test_publish_hydrates_only_requested_scope_ids(...):
    service.preflight(database)
    service.publish(token, [ready_scope], target)
    assert decoded_ids == expected_ready_scope_ids
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests\test_panel_surfaces.py::test_publish_hydrates_only_requested_scope_ids -q`

Expected: FAIL because the service reads every fragment.

- [x] **Step 3: Write minimal implementation**

```python
ids = tuple(item.fragment_id for item in pending.metadata if _item_scope(item) in selected)
fragments = iter_fragment_ids_parallel(pending.source_database, ids, workers=self._workers)
```

Use the already-validated `source_content_digest` as a materializer argument or
continue the existing full metadata id digest; never derive lineage from just
the selected ids.

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

### Task 3: Preserve E3/E4 and document verification

**Files:**
- Modify: `tests/test_source_v6_empty_results.py` only if a public entry-point test is needed
- Modify: `docs/specs/2026-08-22-source-v6-surface-throughput.md`
- Modify: `progress.md`

**Interfaces:**
- Consumes: unchanged `materialize_source_v6` and `measure_points`.
- Produces: evidence that empty-result records and hidden-window failure are unchanged.

- [x] **Step 1: Run the existing behavior checks**

Run: `.venv\Scripts\python.exe -m pytest tests\test_source_v6_empty_results.py -q`

Expected: PASS.

- [x] **Step 2: Run focused storage and panel checks**

Run: `.venv\Scripts\python.exe -m pytest tests\test_source_v6_storage.py tests\test_panel_surfaces.py tests\test_panel_static_ui.py -q`

Expected: PASS.

- [x] **Step 3: Record the contract result**

Add the selected-scope behavior and its measured/verified result to the
throughput spec and `progress.md`; do not claim a corpus-wide benchmark until
one has actually run.

- [ ] **Step 4: Commit**

```bash
git add docs/specs/2026-08-22-source-v6-selected-scope-materialization.md docs/superpowers/plans/2026-08-22-source-v6-selected-scope-materialization.md src/mrs3/source_v6_storage.py src/mrs3/panel_surfaces.py tests/test_source_v6_storage.py tests/test_panel_surfaces.py
git commit -m "feat: hydrate only selected source v6 scopes"
```

## Self-review

- E1--E5 are retained because the hydrated materializer is unchanged.
- The plan selects only stored payload ids belonging to requested READY scopes.
- No task changes the content, lineage, or atomic publication contract.
