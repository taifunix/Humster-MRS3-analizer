# Panel Web: trust and design alignment

**Статус:** implementation plan; финальный Advisor PASS не получен: повторный вызов review завершился Transport closed.
**Дата:** 2026-08-24
**Основание:** текущий код и docs/specs/2026-08-22-panel-static-frontend-v1.md. Старые panel-планы являются drafts, не контрактом.

> Для исполнителей: применять Ponytail full. минимально исправлять подтверждённые contracts. Никаких новых frontend-зависимостей, framework, design system или speculative API.

## Цель

Сделать панель честной и предсказуемой: каждый видимый control имеет реальный владелец и ответ сервера; неподдерживаемое действие не имитируется; ошибка не выглядит как пустой успех; remote secrets и абсолютные remote paths не достигают браузера; layout следует пяти approved references без воскрешения mock-only элементов.

## Неподвижные правила

- Source MRS2 PnL подписывается только как Source MRS2 PnL — source / untested; он не является tested MRS3 PnL.
- DD5 всегда имеет видимый token CALCULATION_ONLY.
- Portfolio остаётся disabled, aria-disabled=true и не получает route/action.
- Artefacts menu, tab и route отсутствуют.
- Strategy testing только local; remote strategy selector/action запрещён.
- UI не является safety boundary: backend сохраняет v6, manifest, quarantine, safe_to_delete, Fill и READY JSON gates.
- Browser не получает credential key/value или host/user; operator-selected remote Source DB paths are allowed and backend validates them against configured remote roots.
- Рабочее дерево dirty: один последовательный владелец для app.js, index.html и settings backend; только surgical hunks, без reset/stash/whole-file rewrite.
- После каждого блока: focused tests, полный .venv\Scripts\python.exe -m pytest, git diff --check, scoped diff, independent review. Следующий блок начинается только после CODE_REVIEW_PASS предыдущего.

## G0–G5: обязательные discovery gates

### G0. Матрица controls

Создать docs/superpowers/plans/2026-08-24-panel-web-control-matrix.md со строкой для каждого interactive control:

stable selector | label | handler owner | endpoint/method | request | success/error response | busy/final state | reference block | disposition.

Disposition:

- DELETE: handler/endpoint отсутствуют и нет документированного near-term contract.
- DISABLE: endpoint существует, но не может выполнить обещанный UI contract; control остаётся disabled с точной причиной.
- KEEP: documented response, один owner и static test.
- UNKNOWN до реализации запрещён.

### G1. Baseline и settings reconciliation

До B1:

1. Запустить .venv\Scripts\python.exe -m pytest -q.
2. Записать timestamp, passed/failed/skipped и текущие unrelated failures/flakes. Flake повторить узким test один раз.
3. Только новый failure или изменение существующего результата является regression.
4. Найти реальные settings persistence files, defaults, readers, writers и endpoint handlers.
5. Зафиксировать точные mutable allowlist, read-only и server-only/redacted keys по section; сверить с каждым reader/writer. Не создавать параллельную settings model.
6. Зафиксировать current import/require/CDN baseline, обязательные screen IDs, порядок anchors и пять reference paths.

### G2. Secure job reattach

Reattach разрешён только если job ID opaque, server проверяет принадлежность/current session, terminal/unknown/forbidden/expired response определены и status не раскрывает command/credentials/paths.

Если хоть одно условие не доказано, не писать ничего в sessionStorage. Показать: Server job may still continue; refresh server status. Нельзя угадывать job по path, DOM position или cached request.

### G3. Static test contract

Создать tests/test_panel_web_static.py на Python stdlib: pathlib, re и небольшой lexical scanner, игнорирующий strings/comments.

Тесты обязаны проверять:

- одно определение renderShortlist и только канонические вызовы;
- явный EXPECTED_HANDLER_COUNTS: для каждого stable selector ровно ожидаемое число registrations;
- отсутствие positional binding: querySelectorAll(...)[, .children[, .rows[, data-index/dataset.index и index-only action maps;
- каждый fetch( находится внутри единственного requestJson;
- requestJson содержит response.ok и finally;
- обязательные IDs и reference-order anchors присутствуют ровно один раз/в нужном порядке;
- не добавлены imports/requires/CDN scripts против baseline;
- точные labels и negative contracts: Source MRS2 PnL — source / untested, CALCULATION_ONLY, Portfolio disabled/aria-disabled/no link, no Artefacts, no remote strategy controls.

Не заявлять static test для browser timing, computed CSS, focus order, geometry или visual similarity: это manual smoke.

### G4. Redaction contract

Проверить общий JSON serialization boundary для всех panel 200, 4xx и 5xx responses: bootstrap, settings, jobs/status, validation/error/log/toast payloads.

Запрещены configured secret sentinels, password/api-key keys/values, user@host:, ssh://, /home/, /Users/, Windows drive и UNC paths, traceback/stack/debug dumps. Benign relative/logical fixture root=., reports_root=reports, workspace=local проверять отдельно, чтобы не блокировать разрешённые local fields.

### G5. Actual server evidence

Зафиксировать: Fill and Start prerequisites; READY JSON eligibility; tester batch/output real contract; remote Source preflight; fields actually returned for Surfaces; publish ownership and status. Если API не доказан, G0 выбирает DELETE или DISABLE, не новый API.

### G7. Инварианты таблиц и массового выбора

До B1 записать исходное состояние для shortlist и scopes: active filters,
selected scope keys, expanded Pair·Side groups, confirmed selection и порядок
строк. Для каждого control зафиксировать разрешённые side effects:

| Control | Меняет | Не меняет |
| --- | --- | --- |
| Выделить все READY scopes | только eligible selected keys | filters, sort, expanded groups, table schema |
| Снять выделение | только selected keys | filters, sort, expanded groups, table schema |
| Выбрать отфильтрованные READY | eligible keys, соответствующие текущим filters | hidden/неподходящие filters keys, expanded groups, table schema |
| Checkbox Pair·Side/TF | keys соответствующей группы/TF | filters, expanded groups, соседние группы |

Подтверждение selection закономерно инвалидируется, когда изменился набор keys;
это единственный допустимый дополнительный side effect. Текст текущей кнопки
«Выбрать видимые» заменить на «Выбрать отфильтрованные READY»: DOM-видимость
внутри закрытого accordion не является устойчивым значением.

Все Surface header, Pair·Side summary и TF row используют один именованный CSS
grid-template из шести колонок. Shortlist остаётся native table: один и тот же
набор девяти ячеек в header, group и TF rows. Любая операция выбора не имеет
права заменять grouped table flat-строками, менять число колонок или закрывать
группы.

### G8. Визуальный контракт shortlist из предоставленного reference

Скриншот пользователя от 2026-08-24 — acceptance reference для блока
Shortlist and READY JSON. Он уточняет approved mock, не добавляя новой функции:

- одна native table на полную ширину карточки с ровными девятью колонками:
  selection, Pair · Side, TF, 1ORD, 2ORD, 3ORD, 4ORD, READY, ALL;
- тёмный компактный header, одинаковая вертикальная граница и padding всех
  data rows;
- Pair · Side group row визуально выделена мягким фоном; раскрывающая стрелка
  и group-checkbox различимы и имеют независимые hit targets;
- TF rows сохраняют пустую Pair · Side cell, TF строго под TF, а каждый count
  строго под своим header; значение отсутствующего bucket — нейтральное «—»;
- group checkbox выбирает только eligible TF этой группы, TF checkbox — только
  свою строку; disabled TF остаётся видимым, но не выглядит выбранным;
- раскрытие/сворачивание меняется исключительно click по стрелке, а массовый
  выбор, clear, refresh и checkbox-change не меняют open-state;
- horizontal overflow допустим только в wrapper на narrow viewport, без
  изменения порядка или ширин колонок на desktop.

### G6. Восстановленные истории из макетов

Все 24 сохранённых HTML-макета сверяются как история продукта, а не как
дополнительная спецификация. В matrix зафиксировать результат:

- **Вернуть:** один общий state/render путь shortlist. После fresh analysis
  committed candidate count, таблица Pair→TF, select-all/none и individual
  selection обязаны читать один state; текущий shadow renderer не должен
  сообщать «0 candidates» при заполненной таблице.
- **Вернуть при существующем safe contract:** read-only «Identity и lineage
  analysis» после create/open analysis DB. Использовать только уже доступные
  `analysis_run_id`, `surface_id`, `scopes`, `algorithm_version` из catalog/open
  response; никаких file-open, raw path, manifest download или client-derived
  lineage. Если create response не даёт безопасную identity и catalog/open не
  может её подтвердить, control остаётся DISABLE с причиной, а не заглушкой.
- **Уже реализовано, не дублировать:** per-scope gaps dialog, asynchronous
  Stop local/remote Source jobs, Source import/merge preflight/start/polling.
- **Не переносить в этот план:** Artefacts library, audit/CSV/JSON downloads,
  plateau report, workbook/final-shortlist export, отдельный tester-plan,
  отдельные Performance/DD5 steps, Stop synchronous coverage preflight,
  editable remote paths и remote strategy runner. Они противоречат active v2
  scope либо требуют отдельный safe artifact/runner contract.

## B1. Исправить duplicate shortlist renderer

**Files:** src/mrs3/panel_web/app.js; tests/test_panel_web_static.py.

- Оставить один grouped renderShortlist и перевести все callers на него.
- Удалить shadow renderer и orphan helpers.
- Не менять shortlist contract, sort/filter или Pair→TF layout.
- Разделить disclosure и group checkbox на отдельные controls с независимыми
  accessible names; не объединять их в один click target.
- Привести group/TF cells и CSS table rules к G8: fixed nine-column semantic
  structure, мягко выделенная group row, одинаковый padding и count alignment.

Evidence: select-all, select-none, individual selection и refresh не меняют
число колонок, Pair→TF hierarchy, expandedPairs или порядок строк; static
single-renderer test; manual regression (раскрыть две группы → select-all →
select-none → отметить одну TF) и screenshot comparison с G8; full suite;
review.

## B2. Deletion-only sweep

**Files:** только пути, указанные G0 DELETE.

- Удалить мёртвый old Surfaces block, fake Grid/READY interval, permanent pending stubs, inert inputs и orphan bindings/styles.
- Старую кнопку Manifest/lineage не удалять автоматически: G6 решает между
  безопасной read-only identity summary и DISABLE/удалением. Заглушка, которая
  только меняет текст status, запрещена.
- tester batch/output удалить, если G5 не доказал server validation contract.
- Не удалять только потому, что элемент неудобен: G0 DELETE требует отсутствующий owner и near-term contract.
- Manifest/lineage можно вернуть лишь с backend evidence manifest/quarantine/safe_to_delete.

Evidence: все оставшиеся controls имеют G0 row; no inert UI; Portfolio/A artefacts/remote strategy negative tests pass; review.

## B3. Redaction и partial settings patch

**Files:** owner bootstrap/settings serialization route, src/mrs3/panel_web/index.html, app.js, tests/test_panel_web_contract.py, tests/test_panel_web_static.py.

- Использовать existing settings endpoint и section-specific patch.
- Reject unknown/read-only keys as 422-class response, preserving current safe status/body semantics.
- Same-key writes: last accepted write wins; untouched/different keys never change.
- New endpoint допускается лишь если G1 докажет incompatibility; тогда compatibility test всех callers.
- Centralize redaction at common serialization boundary, including nested validation errors and error branches.
- Frontend sends current section only; remote path controls/status values removed or redacted; quick Source save writes canonical key used by catalog reader.

Evidence: serialized 200/4xx/5xx redaction matrix; unrelated field unchanged; existing caller compatibility; full suite; security review.

## B4. Safe requestJson

**Files:** app.js; minimal safe error shape owner; static/contract tests.

- Единственный requestJson validates response.ok and content type/JSON before success use.
- Converts network/non-JSON/server validation error into safe visible status; raw debug never reaches UI.
- Busy set before request and cleared in finally; no retry framework/client library.
- Do not migrate controls here; B7 owns all binding migration.

Evidence: 400/409/500/non-JSON cannot become empty success or stuck busy; tests and review.

## B5. Backend gates and honest states

**Files:** profile handlers/tests plus markup only as G0 dictates.

Direct tests bypass UI and prove server rejects non-v6 Source, invalid/missing manifest, nonzero quarantine, invalid safe_to_delete where required, Start before Fill, invalid/non-READY JSON and remote strategy request. READY JSON enables local tester only.

KEEP controls become enabled only from server evidence. DISABLE controls state reason. False READY/SAFE/PENDING badges become neutral until verified. Busy prevents duplicate submit.

Evidence: direct backend gate tests; no UI-only safety assumption; full suite and review.

## B6. Jobs reload recovery

**Files:** app.js and current jobs/status owner only if G2 allows.

- Implement exactly G2 branch.
- Secure branch: sessionStorage contains one documented opaque job ID only; validate on server and clear terminal/forbidden/expired.
- Insecure branch: no sessionStorage writes, no cached ID, explicit refresh warning.
- Never auto-run or infer job identity.

Evidence: running/finished/unknown/ambiguous reload manual smoke; no secret/path cache; review.

## B7. One binding owner

**Files:** index.html, app.js, static tests.

- Give each KEEP/WIRE control stable id or string data-action.
- Register exactly one handler by G0; remove positional selectors and generic pending listener.
- Move every live request through requestJson.
- Wire Check only to a G5-confirmed server action; otherwise DELETE/DISABLE.
- Preserve existing local import/merge/remote Source workflows; do not create remote strategy workflow.

Evidence: handler-count/positional/fetch static checks; double-click/retry/navigation/reload manual smoke; review.

## B8. Real Surfaces only

**Files:** index.html, app.js, app.css, relevant tests.

- Preserve one active V2 flow: preflight → READY selection → confirmation → publish.
- Render only server-returned fields; no dashes for invented Grid/interval.
- Использовать G7 model-driven selection. «Выбрать отфильтрованные READY»
  вычисляет eligible keys из surfaceGroupsV2 и текущих filters, а не query всех
  DOM checkbox; закрытая группа не делает TF невидимым для модели.
- Массовый select/clear и checkbox sync не пересоздают scope-list. Хранить
  expanded Pair·Side keys; если render нужен после смены filter/data, вновь
  применять сохранённое open-state. Selection меняет только checked state,
  summary, badge и нужную invalidation confirmed selection.
- Fix labels/interactive nesting and loading/empty/error/published states.
- Вынести общий шестиколоночный grid-template для scope header, group summary
  и timeframe row; static CSS test сравнивает template во всех трёх rules.
- Retain required screen IDs and G1 anchor order.

Evidence: static test проверяет одинаковые Surface columns и девять Shortlist
columns; manual regression (фильтр → раскрыть/свернуть группы → select filtered
→ clear → confirm) доказывает, что filters, expansion, alignment и hierarchy
не меняются от selection; empty/multiple/published response tests; manual
publish/reload smoke; reference checklist; review.

## B9. Honest Strategies/DD5

**Files:** index.html, app.js, local tester handler/tests.

- Keep only real local analysis → READY JSON → local tester → result flow.
- После create/open analysis DB реализовать G6 read-only Identity и lineage
  summary только из safe server fields `analysis_run_id`, `surface_id`,
  `scopes`, `algorithm_version`; она не открывает файлы и не заявляет
  provenance, которого ответ не подтвердил.
- Tester unavailable before server-confirmed READY JSON in UI and backend.
- Removed/disabled strategy controls follow G0; no fake manifest/batch/output, remote runner or host selector.
- API counters only; no client forecasts.
- Distinguish source metric, tested result and DD5 CALCULATION_ONLY.

Evidence: committed analysis показывает truthful candidate count вместо 0;
identity summary совпадает с catalog/open response и не содержит path; READY/non-READY
direct tests, label static tests and state-transition manual smoke; review.

## B10. Accessibility and visual alignment

**Files:** index.html, app.js, app.css, static tests.

- Native labels/accessible names, semantic controls, visible focus, aria-live error/status, disabled semantics and heading order.
- Use G1 static dependency/ID/anchor checks only.
- Manual screenshot checklist, same viewport, for five approved mocks: navigation/header/spacing; Testing run/progress; Source DB; Surfaces selection; Strategies/DD5 labels; Settings/error/disabled Portfolio.
- Record PASS or exact intentional deviation in control matrix. No automated pixel diff promise.
- Manual narrow/mobile, keyboard, loading, empty, validation/server/network failure and long-error wrapping smoke.

## B11. Final verification

Run:

    .venv\Scripts\python.exe -m pytest tests/test_panel_web_static.py tests/test_panel_web_contract.py -q
    .venv\Scripts\python.exe -m pytest -q
    git diff --check
    git status --short
    git diff --staged

Compare full-suite output with G1 baseline: a new/changed failure blocks completion. Independently review requirements, diff, G0–G5 evidence, test logs, redaction inspection and screenshots. Fix confirmed findings, repeat affected/full tests and re-review. Only actual CODE_REVIEW_PASS closes implementation.

## Definition of done

- One shortlist renderer, one Surfaces flow, no dead/fake controls.
- Every visible control has a G0 contract.
- Server-side validation proves remote Source DB paths stay within configured remote roots; the panel may expose the operator-selected HTML and staging paths.
- Section patch updates only its allowlisted keys.
- Direct backend gates survive UI bypass.
- Reattach follows G2, without unsafe cache fallback.
- No positional/double bindings or fetch outside requestJson.
- Local-only READY-gated tester; honest source/DD5 labels.
- Portfolio disabled/non-navigable; Artefacts absent.
- Five-reference manual checklist, accessibility and mobile/failure/empty smoke pass.
- New tests and full suite pass against baseline, git diff --check is clean, and independent review returns CODE_REVIEW_PASS.
