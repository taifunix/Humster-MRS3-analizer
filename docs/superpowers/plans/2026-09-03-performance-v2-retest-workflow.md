# Performance DB v2 CHECK & RETEST Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить долговечный тег RETEST, пакетное повторное тестирование через существующий SINGLE_MODE и атомарный REPLACE текущих результатов.

**Architecture:** Performance DB остаётся источником typed identity и RETEST-состава. Новый builder формирует обычный strategy manifest, существующий SINGLE_MODE создаёт обычный inbox, а существующий Performance importer выполняет REPLACE с минимальными расширениями для mixed-run provenance и атомарного снятия RETEST.

**Tech Stack:** Python 3.12, DuckDB, openpyxl, pandas, HTML/CSS/JavaScript панели, pytest из `.venv`.

**Spec:** `docs/specs/2026-09-03-performance-v2-retest-workflow.md`

## Global Constraints

- Schema version `4`; миграция только `3 -> 4`, одной транзакцией.
- RETEST не меняет ACTIVE, selection status, REJECTED или current result до успешного REPLACE.
- CHECK & RETEST не мутирует БД; IMPORT & REPLACE является отдельным действием.
- JSON строится только из typed-параметров БД и tracked-шаблона.
- Отчёт обязан иметь точный test range, имя и настройки стратегии.
- Ошибка одной стратегии сохраняет её прежний result и RETEST, но не блокирует
  publication остальных valid strategies; batch публикует CSV/XLSX failure report.
- Тесты запускать только `.venv\Scripts\python.exe -m pytest ...`.

## File Structure

- `templates/strategies/` — tracked-владелец strategy JSON templates.
- `src/mrs3/performance_v2_retest.py` — RETEST queries, audit seeding и manifest builder.
- `src/mrs3/performance_v2_store.py` — schema v4 и migration.
- `src/mrs3/performance_v2_selection*.py` — XLSX RETEST round-trip.
- `src/mrs3/performance_v2_input.py`, `performance_v2_import.py` — mixed-run provenance, report range и очистка тега.
- `src/mrs3/panel.py`, `panel_web/*` — orchestration и UI без второго tester runtime.

---

### Task 1: Канонические шаблоны стратегий

**Files:**
- Create: `templates/strategies/README.md`
- Create: `templates/strategies/source-v6-mrs2/long.json`
- Create: `templates/strategies/source-v6-mrs2/short.json`
- Create: `templates/strategies/retest-mrs3/base.json`
- Modify: `src/mrs3/panel_testing.py`
- Modify: `config.example.json`
- Modify: `config.local.json.example`
- Test: `tests/test_panel_testing.py`

**Interfaces:**
- Consumes: существующие `render_strategy()` и `generate_strategy()`.
- Produces: стабильные repo-relative paths без fallback на `Input/`.

- [x] **Step 1: Создать каноническое дерево и переключить конфиги**

```json
"strategy_templates": {
  "LONG": "templates/strategies/retest-mrs3/base.json",
  "SHORT": "templates/strategies/retest-mrs3/base.json"
}
```

- [x] **Step 2: Добавить проверку tracked MRS2 path**

```python
assert (repo_root / "templates/strategies/source-v6-mrs2/short.json").is_file()
assert json.loads(path.read_text(encoding="utf-8"))["basic"]["strategy"] == "mrs2"
```

- [x] **Step 3: Запустить template/local testing tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_panel_testing.py tests/test_panel_fresh_strategies.py -q`
Expected: PASS.

### Task 2: Schema v4 и начальная RETEST-разметка

**Files:**
- Modify: `src/mrs3/performance_v2_store.py`
- Create: `src/mrs3/performance_v2_retest.py`
- Modify: `tests/test_performance_v2_store.py`
- Create: `tests/test_performance_v2_retest.py`

**Interfaces:**
- Produces: `retest_status(connection) -> RetestStatus`.
- Produces: `mark_retest_from_audit(connection, path) -> int`.

- [x] **Step 1: Написать failing migration test**

```python
assert connection.execute(
    "select tag, source, source_ref from strategy_tags"
).fetchone() == ("REJECTED", "SELECTION_REVIEW", "review-1")
```

- [x] **Step 2: Реализовать v3 -> v4**

Новая таблица допускает только `REJECTED|RETEST`; старые строки копируются
внутри транзакции, затем marker меняется на `4`.

- [x] **Step 3: Написать failing audit-seed test**

```python
assert mark_retest_from_audit(connection, workbook) == 3
assert mark_retest_from_audit(connection, workbook) == 3
```

- [x] **Step 4: Реализовать строгий HIGH+REVIEW seed**

Проверить заголовок `Strategy ID`, целые уникальные ID и наличие каждой
стратегии; выполнить идемпотентный upsert с source `PERIOD_INTEGRITY_AUDIT`.

- [x] **Step 5: Запустить focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_store.py tests/test_performance_v2_retest.py -q`
Expected: PASS.

### Task 3: XLSX RETEST round-trip

**Files:**
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `src/mrs3/performance_v2_selection_review.py`
- Modify: `tests/test_performance_v2_selection_review.py`

**Interfaces:**
- Consumes: `strategy_tags` schema v4.
- Produces: колонку `RETEST`, принимающую только blank или `RETEST`.

- [ ] **Step 1: Написать failing export/import test**

```python
sheet.cell(2, headers["RETEST"], "RETEST")
assert connection.execute(
    "select strategy_id from strategy_tags where tag='RETEST'"
).fetchall() == [(1,)]
```

- [ ] **Step 2: Экспортировать текущую отметку**

`apply_prior_rejected()` добавляет `prior_retest`; workbook добавляет
list validation `RETEST` с разрешённой пустой ячейкой.

- [ ] **Step 3: Валидировать и синхронизировать в review transaction**

Другие значения дают `SELECTION_REVIEW_INVALID_RETEST`; RETEST заменяется
только для полного rowset текущего workbook.

- [ ] **Step 4: Запустить focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection_review.py tests/test_performance_v2_selection.py -q`
Expected: PASS.

### Task 3a: Comparable selection windows

**Files:**
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `tests/test_performance_v2_selection.py`

**Contract:**
- `filter_low_trades` uses `Trades/30d`, not raw `total_trades`.
- Selection XLSX excludes raw `total_pnl_pct` / `PnL`.
- Time consistency uses the same calendar span as `Trades/30d`: four equal
  windows for `>=28d`, three for `>=21d and <28d`, otherwise `UNAVAILABLE`.
  Strictly positive `PnL/30 > 0` passes at `3/4` or `2/3`; zero and
  `NO_TRADES` are non-positive, other unavailable windows make the strategy
  `UNAVAILABLE` without exclusion and XLSX renders `N/A`.
- Bump cache version to `performance-window-v2.2`; do not reuse v2.1 rows.
  No Q4 is requested for a three-window result. DD and all specified
  structural metrics remain unchanged.

**Steps:**

- [x] Write failing tests for exact 21/28-day boundaries, `PASS`/`FAIL`/
  `UNAVAILABLE`, stale v2.1 cache, equal trade rate across unequal spans,
  missing trade rate, absence of raw `PnL` from both XLSX sheets, and raw
  DD/DD5 plus structural-metric regressions.
- [x] Replace positional quarter tails with explicit consistency windows;
  derive `trades_30d`, update only the export layer, and rerun focused,
  related and full suites. Legacy posttest removal is tracked separately in
  Task 8 below.

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py -q`
Expected: PASS.

### Task 4: RETEST manifest builder и mixed-run inbox

**Files:**
- Create: `src/mrs3/performance_v2_retest.py`
- Modify: `src/mrs3/performance_v2_input.py`
- Modify: `src/mrs3/panel_strategy_batch.py`
- Test: `tests/test_performance_v2_retest.py`
- Test: `tests/test_performance_v2_input.py`

**Interfaces:**
- Produces: `build_retest_manifest(connection, templates, output_dir) -> RetestBatch`.
- Produces key `strategy_analysis_run_ids: {json_filename: original_run_id}`.
- Legacy manifests fall back to common `analysis_run_id`.

- [x] **Step 1: Написать failing builder identity test**

Проверить exact name, side flags, Close MA, Open MA, shift, lot, plateau
diagnostics и original run IDs для двух стратегий из разных runs.

- [x] **Step 2: Реализовать минимальный builder**

Использовать `generate_strategy()`, восстановить exact stored name, затем
`adapt_strategy_identity()` и сравнить каждое typed поле.

- [x] **Step 3: Опубликовать атомарно и проверить manifest**

Сформировать hashes/diagnostics/batch SHA, заменить staging на
`Output/strategies`, вызвать `validate_strategy_manifest()`.

- [x] **Step 4: Читать per-strategy original run**

```python
entry_run_id = strategy_analysis_run_ids.get(strategy_path.name, analysis_run_id)
```

Неверная или неполная карта отклоняется.

- [x] **Step 5: Запустить focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_retest.py tests/test_performance_v2_input.py -q`
Expected: PASS.

### Task 5: Exact report range, listing warm-up and атомарный пакетный REPLACE

`PnL/30` остаётся нормализованным по 30 календарным дням. `max_drawdown_pct`
не нормализуется и не экстраполируется: `PnL DD5/30 = PnL/30 * 5 / raw DD`.
Сырые PnL/DD сохраняются для аудита, но не становятся objective отбора.

**Files:**
- Modify: `src/mrs3/panel_fast_strategy_test.py`
- Modify: `src/mrs3/performance_v2_input.py`
- Modify: `src/mrs3/performance_v2_import.py`
- Modify: `src/mrs3/panel_performance_v2.py`
- Test: `tests/test_single_mode_handoff.py`
- Test: `tests/test_performance_v2_import.py`

**Interfaces:**
- `PerformanceV2ImportRequest(..., clear_retest_on_success: bool = False)`.
- RETEST caller supplies server-built replacement mapping and sets the flag.
- Inbox records the configured listing-dates path. Import validates the
  tester's common report range, then publishes each result from
  `max(reported_start_utc, listing_date_utc + 5 days)` through report end.
- Every trade opened before that effective start is excluded in full, even if
  it closes later; actions/equity and canonical metrics are recalculated from
  the effective range while reported range remains provenance.
- Missing/invalid listing, empty effective range or absent eligible trade leave
  only that strategy unchanged and create a reason-coded CSV/XLSX report.

- [ ] **Step 1: Написать failing wrong-range test**

Отчёт с верным name/settings, но другим report range, не должен стать
verified report и не должен пройти inbox reader. Добавить cases позднего
listing: warm-up boundary, целиком исключённая crossing trade и отказ при
`effective_start >= report_end`.

- [ ] **Step 2: Подключить `_report_matches_run()` к native collection**

Не добавлять второй parser; использовать существующую проверку settings/range.

- [ ] **Step 3: Повторить range check на import trust boundary**

Сравнить parsed range с inbox `test_start/test_end`.

- [ ] **Step 4: Написать mixed-run mass REPLACE transaction test**

Две стратегии сохраняют ID, получают новые current results, старые children
удаляются, RETEST снимается. Ошибка второй строки сохраняет оба старых результата
и оба тега.

- [ ] **Step 5: Снимать RETEST до commit**

Удалить теги реально заменённых IDs в той же transaction. Обычный REPLACE с
`clear_retest_on_success=False` тег не меняет.

- [ ] **Step 6: Запустить focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_single_mode_handoff.py tests/test_performance_v2_input.py tests/test_performance_v2_import.py tests/test_panel_performance_v2.py -q`
Expected: PASS.

### Task 6: CHECK & RETEST экран и orchestration

**Files:**
- Modify: `src/mrs3/panel.py`
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `src/mrs3/panel_web/style.css`
- Modify: `tests/test_panel_static_ui.py`
- Create: `tests/test_panel_performance_v2_retest.py`

**Interfaces:**
- `GET /api/v2/strategies/performance-v2/retest/status`.
- `POST /api/v2/strategies/performance-v2/retest/start`.
- `POST /api/v2/strategies/performance-v2/retest/import`.
- Existing tester/import status endpoints remain authoritative.

- [ ] **Step 1: Написать failing controller/API tests**

Проверить count/default dates, `start < end`, listing dates, блокировку import
до INBOX_READY и отсутствие browser-supplied replacement mapping.

- [ ] **Step 2: Реализовать controller orchestration**

Status читает DB; start строит/registers manifest и вызывает SINGLE_MODE;
import строит mapping из committed inbox и текущих RETEST rows и запускает
существующий Performance REPLACE job.

- [ ] **Step 3: Добавить минимальный UI**

Один card: count, два `input type=date`, две кнопки, native `progress` и
`role=status`. IMPORT disabled до inbox ready.

- [ ] **Step 4: Подключить polling и refresh recovery**

Показывать phase/current/total/batch/active/retries/failed, inbox path и REPLACE;
после COMMITTED перечитать RETEST count.

- [ ] **Step 5: Запустить panel tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_panel_performance_v2_retest.py tests/test_panel_static_ui.py tests/test_panel_jobs.py -q`
Expected: PASS.

### Task 7: Реальная разметка, общая проверка и review

**Files:**
- Modify: `PRD.md`
- Modify: `progress.md`
- Modify: `docs/handoffs/2026-09-03-performance-v2-remaining-work.md`

**Interfaces:**
- Consumes: проверенный RETEST workflow.
- Produces: мигрированная локальная DB и размеченный audit set.

- [ ] **Step 1: Проверить DB writer и создать backup**

Backup создаётся рядом с локальной БД до migration/seed; generated DB не
добавляется в Git.

- [ ] **Step 2: Выполнить v4 migration и HIGH+REVIEW seed**

Сверить число уникальных audit IDs, существующих IDs и `ACTIVE RETEST`.

- [ ] **Step 3: Запустить relevant suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_store.py tests/test_performance_v2_retest.py tests/test_performance_v2_selection.py tests/test_performance_v2_selection_review.py tests/test_performance_v2_input.py tests/test_performance_v2_import.py tests/test_single_mode_handoff.py tests/test_panel_performance_v2.py tests/test_panel_performance_v2_retest.py tests/test_panel_static_ui.py -q`
Expected: PASS.

### Task 8: Remove legacy posttest workflow

Before deletion, trace every call to `src/mrs3/posttest.py`, its CLI and panel
routes. Remove the obsolete workflow and only its dedicated tests/docs; retain
the v2 Performance workflow. Run the affected tests and the relevant suite.

- [ ] **Step 4: Проверить diff**

Run: `git diff --check`
Expected: no output.

- [ ] **Step 5: Независимое review**

Передать reviewer компактный ASCII packet: requirements, paths/diff, tests.
Исправить подтверждённые замечания и повторить focused tests/re-review.

- [ ] **Step 6: Обновить документацию**

Зафиксировать schema v4, audit count, проверки и следующий шаг; generated
JSON/HTML/XLSX/DB не коммитить.
# Current execution status (2026-09-03)

Tasks 2-6 are implemented in the working tree and verified by focused tests.
Task 7 remains pending because it requires an explicit production-data backup,
schema migration and audit seed. Task 8 legacy removal is implemented; final
full-suite and independent code review remain the acceptance gates.
