# Portfolio Optimizer Stage 2 Sequential Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Apply TDD and `superpowers:verification-before-completion`. Do not run a real tester.

**Status:** `PLAN_APPROVED` — manual Advisor review of source v2 returned explicit approval; findings F1–F11 and caveats C1–C5 are resolved. Implementation remains fixture/fake-only and stops before any real tester run.

**Goal:** Последовательно протестировать все кандидаты из committed Stage 1 artifact, сохранив их серверный порядок и точный набор стратегий.

**Architecture:** `PortfolioPanelService` готовит in-memory список всех кандидатов из committed `stage1-executables.json`, затем выполняет их одним `portfolio.stage2` job строго по одному. Для каждого кандидата переиспользуется `fill_prebuilt → start → fresh report → stop`; `PanelJobRegistry` остаётся единственным durable store, а `runtime["stage2_results"]` — единственным источником сохранённых batch результатов.

**Tech Stack:** Python 3, существующие `PortfolioPanelService`, `PanelJobRegistry`, `LocalTestingService` и `TesterTargetLock`, vanilla HTML/JavaScript, pytest.

**Spec:** `docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md`

## Global Constraints

- Limiter остаётся `L=0` / off-only.
- Источник batch — только committed `.portfolio-results/<campaign_id>/stage1-executables.json`.
- Batch содержит все artifact candidates в сохранённом порядке; повторная сортировка и повторное применение `max_candidates` запрещены.
- Не добавлять новый runner, queue, lock, progress-файл, config flag, environment switch, dependency или service.
- Не добавлять batch-wide candidate limit: существующий лимит `max_candidates` 1–50 применяется к каждому profile отдельно.
- Не копировать, не перемещать и не удалять HTML; отчёты остаются в `tester/report/<candidate_id>/`.
- Не добавлять retry/resume, параллельный tester, HTML decoder/import, post-test ranking или `L>0`.
- Повторная submission той же Campaign всегда возвращает существующий job, в любом состоянии; повторный запуск требует новой Stage 1 Campaign.
- В implementation/review использовать только fixtures/fakes; реальный tester run требует отдельного свежего разрешения после `CODE_REVIEW_PASS`.
- Все pytest-команды выполнять через `.venv\Scripts\python.exe` с уникальным TEMP на диске C и безопасным удалением TEMP в `finally`.
- Работать в текущей папке на `main`, не создавая worktree; сохранять unrelated worktree changes; один scoped conventional commit только после независимого review.
- `PRD.md` и `progress.md` обновлять только подтверждёнными verification evidence; один commit — только после `CODE_REVIEW_PASS`.
- Stage 1 stores `executables_count` beside the committed executable digest and removes it during failed-runtime cleanup; older Campaigns without the field remain ineligible in UI, and `_public_job` never reads the artifact to derive it.
- UI submission is available only for the currently projected latest Campaign; this is the accepted minimal UI limitation, not a server-side restriction. Existing single-candidate Stage 2 journal entries without `stage2_results` are not a compatibility target; no fallback is needed because no real Stage 2 run exists.

## Review Focus

- Точный порядок и payload каждого кандидата совпадают с committed artifact.
- Результат становится completed только после того, как `stop()` вернулся без exception; сам `LocalTestingService.stop()` выполняет restore и проверяет его.
- Ошибка/cancel кандидата не удаляет результаты ранее завершённых кандидатов.
- Mutation artifact, stale report или restore failure не позволяют начать следующий кандидат.
- Restart даёт `INTERRUPTED`, но никогда не resume.
- UI не может отправить запрос без свежего подтверждения пользователя.
- Ни один тест не обращается к реальному tester target.

## Runtime and Public Contract

Единственный durable источник результатов — ordered `runtime["stage2_results"]`, список compact `_stage2_result` records. Runtime дополнительно содержит:

- `stage2`: `campaign_id`, `input_digest`, `config_digest`, `artifact_digest`, `candidate_count` и `candidate_bindings` — упорядоченные пары `[candidate_id, candidate_digest]`; без receipts и полного candidate payload;
- `current_index`: `null` до первого кандидата, далее zero-based индекс artifact;
- `current_candidate_id`: `null` до запуска, далее ID текущего/последнего кандидата;
- `completed_count`: число результатов, уже синхронизированных после успешного `stop()`;
- текущие `stage_index`, `completed_stages`, `stage_completed`, `stage_total`, `stage_percent` описывают только текущий candidate-local flow.

Prepared items существуют только в памяти worker и включают `candidate_index`, ID/digest, material и receipt. `_verify_stage2_prepared` выбирает `artifact["candidates"][candidate_index]`; full artifact digest уже связывает порядок, отдельный повторный order scan не нужен.

Public Stage 2 job всегда выдаёт `results` (пустой либо ordered `stage2_results`) и `batch: {current_index, total, completed}`. Индекс в `batch.current_index` zero-based либо `null`; UI показывает `current_index + 1` из `total`. `overall_percent = completed_count * 100 // candidate_count`; `stage` описывает текущий candidate-local этап. Для `SUCCEEDED` singular `result` сохраняется как compatibility projection `results[-1]`, вычисляемый из runtime и не дублируемый в хранилище. При `FAILED`, `CANCELLED` и `INTERRUPTED` singular `result` отсутствует, но `results` и `batch` сохраняются.

Чтобы до подтверждения показать точный размер batch, Stage 1 runtime/public job выдаёт `executables_count`, равный числу кандидатов committed artifact. Stage 2 eligibility берётся только из текущей server-projected Campaign/job; точное число в confirm берётся из этого поля. Серверный ответ `409` остаётся авторитетным.

`_stage2_fail` и `_stage2_cancel_finish` сохраняют `stage2_results`. Существующий orphan projection переводит job в `INTERRUPTED`; `PanelJobRegistry.sync(..., runtime=None)` сохраняет runtime, поэтому отдельный recovery/resume механизм не нужен.

### Pytest TEMP wrapper

Каждую pytest-команду запускать отдельно, заменяя `<PYTEST_ARGS>` аргументами конкретного шага:

```powershell
$testTemp = Join-Path 'C:\Temp' ('mrs3-stage2-batch-' + [guid]::NewGuid().ToString('N'))
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE
$exitCode = 1
try {
    New-Item -ItemType Directory -Path $testTemp | Out-Null
    $env:TEMP = $testTemp
    $env:TMP = $testTemp
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & '.\.venv\Scripts\python.exe' -m pytest <PYTEST_ARGS> --basetemp (Join-Path $testTemp 'pytest') -p no:cacheprovider
    $exitCode = $LASTEXITCODE
}
finally {
    if ($null -eq $previousTemp) { Remove-Item Env:TEMP -ErrorAction SilentlyContinue } else { $env:TEMP = $previousTemp }
    if ($null -eq $previousTmp) { Remove-Item Env:TMP -ErrorAction SilentlyContinue } else { $env:TMP = $previousTmp }
    if ($null -eq $previousBytecode) { Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue } else { $env:PYTHONDONTWRITEBYTECODE = $previousBytecode }
    if (Test-Path -LiteralPath $testTemp) {
        $resolvedTemp = (Resolve-Path -LiteralPath $testTemp).Path
        $tempRoot = [System.IO.Path]::GetFullPath('C:\Temp').TrimEnd('\') + '\'
        $resolvedFull = [System.IO.Path]::GetFullPath($resolvedTemp)
        if (-not $resolvedFull.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $resolvedFull) -notmatch '^mrs3-stage2-batch-[0-9a-f]{32}$') {
            throw "Unexpected test TEMP path: $resolvedFull"
        }
        Remove-Item -LiteralPath $resolvedFull -Recurse -Force
    }
}
exit $exitCode
```

---

### Task 1: Зафиксировать минимальный sequential-batch contract

**Files:**

- Modify: `docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md`
- Create: `docs/decisions/0059-portfolio-stage2-sequential-batch.md`
- Read only: `docs/decisions/0040-portfolio-optimizer-phase7-off-only-local-stage2.md`
- Read only: `docs/specs/2026-09-05-portfolio-optimizer.md`

**Interfaces:**

- Consumes: принятый off-only single-candidate contract ADR-0040.
- Produces: нормативный ordered batch contract для Tasks 2–3.

- [x] **Step 1: Обновить UI spec до изменения поведения**

  Зафиксировать:

  - batch содержит все `artifact["candidates"]` в сохранённом порядке без повторной сортировки или применения `max_candidates`;
  - один parent `portfolio.stage2` job, один активный кандидат;
  - exact payload каждого кандидата формируется только существующим `_stage2_material(candidate)`;
  - completed result записывается только после того, как `stop()` вернулся без exception; `LocalTestingService.stop()` сам выполняет restore и проверяет его, поэтому дублировать проверку не нужно;
  - первая ошибка останавливает batch, retry/resume отсутствуют;
  - cancel прерывает поддерживаемую active path, затем обязательно выполняет stop/restore и не начинает следующий кандидат; если валидный результат получен и `stop()` вернулся успешно, результат сначала синхронизируется до проверки отмены, в том числе если отмена впервые замечена во время polling, затем job становится `CANCELLED`;
  - restart переводит nonterminal job в `INTERRUPTED` без resume;
  - отчёты остаются в `tester/report/<candidate_id>/`; Panel их не копирует и не удаляет;
  - результат ограничен существующими `CORE_METRICS` и report evidence.

- [x] **Step 2: Создать ADR-0059**

  Зафиксировать narrow supersession single-candidate границы ADR-0040. Отклонить второй lock, отдельный progress-файл, новый authorization flag, retry/resume, parallel runs и реальный tester execution. Указать, что `stop()` освобождает tester lock между кандидатами; если другой actor займёт его до следующего fill, batch безопасно завершится FAILED с сохранением завершённых результатов. Фиксированный `portfolio-stage2:{campaign_id}` означает, что повторная submission любой Campaign возвращает существующий job; повторный запуск требует новой Stage 1 Campaign. ADR-0040 не изменять.

- [x] **Step 3: Зафиксировать server authorization**

  Серверный authorization gate запуска:

  - exact body `{confirmed:true,campaign_id:<same path campaign_id>}`;
  - committed Stage 1;
  - committed artifact проходит существующую digest/shape validation.

  Provider `LocalTestingService` нужен для выполнения tester path, но приложение всегда его injects; provider presence не является отдельной security-защитой и так не описывается.

  Disabled-кнопка не считается backend-защитой. Main optimizer spec остаётся authoritative для `FINALIST`, liquidity, leverage, sizing и DD. Q06/physical HTML, ranking, recommendation и trading admission остаются открытыми.

- [x] **Step 4: Проверить документацию**

  Run: `git diff --check -- docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md docs/decisions/0059-portfolio-stage2-sequential-batch.md`

  Expected: exit `0`, ссылки взаимны, accepted ADR-0040 не изменён.

---

### Task 2: Реализовать backend batch через существующий single-candidate flow

**Files:**

- Modify: `tests/test_panel_portfolio.py`
- Modify: `src/mrs3/panel_portfolio.py`
- Modify `tests/test_panel_testing.py` only if a focused existing-lock regression is missing.
- Do not modify: `src/mrs3/panel_jobs.py`
- Do not modify: `src/mrs3/locking.py`
- Do not modify unless a discovered regression requires it: `src/mrs3/panel_testing.py`

**Interfaces:**

- Produces: `PortfolioPanelService._prepare_stage2_batch(self, campaign_id: str) -> tuple[dict[str, Any], ...]`.
- Produces: candidate-aware `PortfolioPanelService._verify_stage2_prepared(self, prepared: Mapping[str, Any]) -> None`, selecting the zero-based `prepared["candidate_index"]`.
- Produces: `PortfolioPanelService._run_stage2_candidate(self, job_id: str, prepared: Mapping[str, Any], tester: Any) -> dict[str, Any]`.
- Changes: `PortfolioPanelService._run_stage2(self, job_id: str, prepared_candidates: Sequence[Mapping[str, Any]]) -> None`.
- Preserves: public `submit_tester_submission(...)`, `_stage2_material(...)` and `LocalTestingService.fill_prebuilt/start/stop` signatures.

`_prepare_stage2_batch` сохраняет artifact order. Каждый in-memory prepared item содержит существующие campaign/input/config/artifact bindings, zero-based `candidate_index`, `candidate_id`, `candidate_digest`, material и receipt. Verifier selects `artifact["candidates"][candidate_index]` and checks identity/digest/material/receipt; the full artifact digest already binds order.

`runtime["stage2"]` uses only this compact artifact-level binding, without candidate receipts or payloads:

- `campaign_id`, `input_digest`, `config_digest`, `artifact_digest`;
- `candidate_count` и ordered `[candidate_id, candidate_digest]` pairs.

`registry.submit` carries only `{campaign_id}`. Runtime uses the existing atomic `PanelJobRegistry.sync`; `runtime["stage2_results"]` is its only ordered result source. `current_index` is zero-based or `null`, `current_candidate_id` is the current/last candidate, and `completed_count` counts synced results. Existing stage fields describe only the current candidate. Stage 1 exposes the exact committed artifact size as public `executables_count` for confirmation/UI eligibility.

- [x] **Step 1: RED — подготовка и happy path**

  В `tests/test_panel_portfolio.py` заменить first-candidate expectation тестами:

  - `_prepare_stage2_batch` возвращает все три candidate bindings в persisted order;
  - each prepared entry has its matching zero-based `candidate_index`;
  - `_verify_stage2_prepared` rejects invalid indexes and verifies the exact indexed candidate;
  - the Stage 1 public job reports `executables_count` equal to the committed artifact candidate count;
  - `registry.submit` stores only the campaign ID; reserved `runtime["stage2"]` has compact artifact bindings and no receipts or strategy payloads;
  - для каждого кандидата последовательность вызовов равна `fill_prebuilt → start → stop`;
  - следующий `fill_prebuilt` вызывается только после успешного предыдущего `stop`;
  - каждый call получает только exact config и strategy JSONs своего кандидата;
  - после каждого restore соответствующий compact result уже сохранён в registry.

- [x] **Step 2: Запустить RED-тесты**

  Use the mandatory TEMP wrapper with:

  `tests/test_panel_portfolio.py -k "stage2_batch_preparation or stage2_batch_runs_all_candidates or executables_count or stage2_compact_binding" -q`

  Expected: FAIL на отсутствующих batch interfaces/behavior.

- [x] **Step 3: GREEN — минимальный outer loop**

  Реализовать интерфейсы выше. Вынести из текущего `_run_stage2` только per-candidate body; не переписывать fill/readback, wizard baseline, report-folder snapshot, tester time window, stable fingerprint, cancellation polling или `stop()`.

  `stop()` возвращается без exception только после своей внутренней restore verification. Если валидный `completed_result` получен и `stop()` завершился успешно, немедленно сохранить результат через `registry.sync` до проверки отмены — в том числе когда отмена впервые замечена во время polling. Затем, если отмена запрошена, перевести job в `CANCELLED`; иначе продолжить следующий candidate. Последний success переводит job в `COMMITTED`.

- [x] **Step 4: RED — обязательные batch regressions**

  Расширить существующие Stage 2 fixtures/tests без второй fake-системы:

  - missing/false/extra/mismatched confirmation отклоняется до tester call;
  - mutation artifact между candidates 1 и 2 сохраняет result 1 и не вызывает fill/start 2;
  - stale/unchanged report кандидата 2 не принимается;
  - stop/restore failure кандидата 2 сохраняет result 1 и не запускает candidate 3;
  - cancellation кандидата 2 выполняет stop/restore, сохраняет result 1 и не запускает candidate 3;
  - restoration failure во время cancellation даёт `FAILED`, не `CANCELLED`;
  - reload nonterminal journal проецирует `INTERRUPTED` и сохраняет completed results;
  - второй Portfolio job отклоняется существующим resource key `portfolio_optimizer`;
  - занятый `TesterTargetLock` блокирует target mutation существующим путем;
  - repeated submission of the same Campaign in QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/INTERRUPTED returns the same job and starts no worker;
  - failed/cancelled/interrupted public `job()` retains `results`, zero-based `batch`, and completed-derived percent;
  - a nonterminal journal reload projects `INTERRUPTED` while retaining runtime results;
  - successful public `job()` retains singular `result` as a derived alias of `results[-1]`;
  - cancellation first observed during polling still persists the valid result after successful stop and before terminal CANCELLED.

- [x] **Step 5: GREEN — terminal result retention**

  Изменить `_stage2_fail` и `_stage2_cancel_finish`, чтобы они не удаляли `runtime["stage2_results"]` и batch progress. Не писать singular persisted `result`. Перед каждым fill и перед acceptance текущего результата переиспользовать `_load_stage1_executables`/`_verify_stage2_prepared` с exact candidate index/identity/digest. `_project_orphan` оставляет runtime при существующем `sync(runtime=None)`; не добавлять recovery logic.

- [x] **Step 6: Выполнить focused verification**

  Использовать обязательный TEMP wrapper отдельно для каждого invocation с аргументами:

  `tests/test_panel_portfolio.py -k "stage2" -q`

  `tests/test_panel_testing.py -k "target_lock or fill_prebuilt or stop" -q`

  Results: `tests/test_panel_portfolio.py -k "stage2" -q` — 64 passed, 1 platform-only symlink skip; `tests/test_panel_testing.py -k "target_lock or fill_prebuilt or stop" -q` — 18 passed. Все тесты использовали fixture/fakes; configured tester не открывался.

---

### Task 3: Включить минимальный UI, проверить и остановиться до real run

**Files:**

- Modify: `tests/test_panel_static_ui.py`
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `PRD.md`
- Modify: `progress.md`
- Update evidence/checkmarks: `docs/superpowers/plans/2026-10-07-portfolio-stage2-sequential-batch.md`

**Interfaces:**

- Consumes: existing `/api/v2/portfolio/jobs/active` projection, Stage 1 `executables_count`, Stage 2 `results`/`batch`, and the existing tester-submission endpoint.
- Produces: one confirmed batch submission and progress in existing Portfolio cards; no new endpoint or persistent frontend state.

The exact UI eligibility rule is:

- current server-projected job is the displayed Campaign;
- `kind == "STAGE1_CALCULATION"` and `status == "SUCCEEDED"`;
- the projected Campaign ID matches the displayed Campaign;
- `executables_count` is a positive integer;
- the current projection is not a Stage 2 job for this Campaign.

`/api/v2/portfolio/jobs/active` returns an active Portfolio job or otherwise its latest terminal job. A Stage 2 submission therefore becomes the current `TESTER_SUBMISSION` projection and disables the button. Do not add an all-jobs endpoint or eligibility flag. The server's idempotency/409 response remains authoritative; readiness's always-false `stage2.enabled` is not used. The confirmation displays the exact `executables_count`.

- [x] **Step 1: RED — минимальный UI contract**

  Обновить `tests/test_panel_static_ui.py`:

  - существующая кнопка initially disabled;
  - eligibility comes only from the exact server-projected Stage 1 rule above;
  - missing/zero/non-integer `executables_count`, another job kind, or non-SUCCEEDED status keeps the button disabled;
  - click немедленно disabled кнопку;
  - `window.confirm` shows Campaign ID and the exact candidate count from `executables_count`;
  - decline не отправляет request;
  - accept отправляет exact `{confirmed:true,campaign_id}` на существующий `tester-submissions` endpoint;
  - existing job/result cards display `current_index + 1 / total`, completed count, candidate-local stage, overall percent, and failure/cancellation/interruption state;
  - `results` remains visible in every Stage 2 terminal state;
  - новые page, wizard, results table, retry/resume и `Output/Portfolio` отсутствуют.

- [x] **Step 2: Запустить RED UI-тест**

  Use the mandatory TEMP wrapper with:

  `tests/test_panel_static_ui.py -k "portfolio_stage2" -q`

  Expected: FAIL на отсутствующем confirmed POST/progress rendering.

- [x] **Step 3: GREEN — переиспользовать существующие controls**

  Изменить только текущие Stage 2 button/badge/reason и существующие request, error и polling helpers. Не добавлять frontend state, переживающий reload: authoritative state всегда повторно загружается с сервера.

- [ ] **Step 4: Выполнить focused и broader verification**

  Use the mandatory TEMP wrapper with:

  `tests/test_panel_static_ui.py -k "portfolio_stage2" -q`

  Use the mandatory TEMP wrapper with:

  `tests/test_panel_portfolio.py tests/test_panel_static_ui.py tests/test_panel_testing.py -q`

  Expected: PASS, кроме документированных platform-only SKIP.

  Current evidence: focused backend/UI checks and the full static UI module
  pass. The combined module run is not clean: `tests/test_panel_portfolio.py`
  stops at 88 passed on an unrelated failure in the unchanged
  `tests/test_portfolio_input.py::_add_review` helper (11 positional values for
  the schema-v9 12-column `selection_results` table). The broader run was
  interrupted when it continued consuming CPU without reporting progress, to
  avoid impacting the live Panel. This verification step remains open.

  After review feedback, the cancellation/result-sync race was added as a
  failing regression and now passes. Eleven selected backend/UI/fill/restore
  regressions pass after the fix. Full
  `tests/test_panel_portfolio.py -k "stage2"` and the full static-UI module
  passed before this narrow race fix.

- [x] **Step 5: Обновить статус без performance claims**

  В `PRD.md` и `progress.md` записать только fixture/fake-verified sequential-batch boundary, точные focused pass counts, незакрытый broader suite, отсутствие real tester run и независимое code review. Реальный local smoke требует CODE_REVIEW_PASS, M5/M6 readiness и отдельного свежего разрешения пользователя. Не заявлять post-test ranking, recommendation, MRS3 performance или live admission.

- [x] **Step 6: Выполнить scope/review gate**

  Run: `git diff --check`

  Run: `git status --short`

  Осмотреть полный scoped diff. Отправить независимому reviewer компактный ASCII packet с требованиями, relevant diff excerpts и verification results. Требовать `CODE_REVIEW_PASS` либо исправить findings через TDD и провести re-review.

  Review round 1 returned `CODE_REVIEW_FINDINGS`. R-3 was reproduced red and
  fixed, R-5 gained type/non-ready route coverage, and R-6/R-8 gained static
  and terminal-index assertions. Remaining concerns were dispositioned
  against the approved plan and existing tester rollback contract. Independent
  round 2 returned `CODE_REVIEW_PASS` (`claude-opus-5`, high). The unrelated
  schema-v9 fixture failure remains documented; no real tester run occurred.

- [x] **Step 7: Commit и обязательная остановка**

  После `CODE_REVIEW_PASS` обновить evidence/checkmarks этого плана только по подтверждённым root verification и фактическому reviewer disposition; не приписывать findings или PASS без evidence. Повторить verification, stage только файлы плана и осмотреть staged diff.

  Commit created: `feat(portfolio): run stage2 candidates sequentially`.

  Затем остановиться. Не запускать реальный tester, не нажимать Stage 2 и не изменять реальные reports. Первый local smoke — отдельное operational action после свежего разрешения пользователя.
