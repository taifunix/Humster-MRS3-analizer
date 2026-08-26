# DD5-панель: контракт frontend tuning

**Статус:** draft contract для Task 0 плана от 2026-08-24
**Владелец:** panel/DD5 frontend и API owners, в границах утверждённого плана
**Основание:** mockup, active static frontend spec и план реализации

## Источники и границы решения

- Визуальный reference: [strategies-dd5-screen-full-restored.html](../../.superpowers/brainstorm/515-1787384829/content/strategies-dd5-screen-full-restored.html).
  Это локальный mockup; он фиксирует порядок пяти карточек и подписи, но не
  является источником серверных путей или фактических результатов.
- Базовый UI-контракт: [Static Control Panel v1](2026-08-22-panel-static-frontend-v1.md).
- План реализации: [2026-08-24-panel-dd5-frontend-tuning.md](../superpowers/plans/2026-08-24-panel-dd5-frontend-tuning.md).
- Общие цели и safety rules: [PRD.md](../../PRD.md) и активные performance/DD5
  specifications, на которые он ссылается.

Эта спецификация уточняет только экран **Стратегии и DD5**, его стабильные API
границы и правила артефактов. Она не меняет исторические документы, текущий
remote import и решения BASE/1ORD.

## Цель

Собрать один статический экран с последовательностью

`Analysis → Shortlist/READY → Tester batch → Inbox/Performance DB → DD5 workbook`,

сохранив серверную валидацию, job recovery и существующие safety-gates. Браузер
получает идентификаторы, статусы и разрешённые artifact tokens, а не произвольные
пути, команды или credentials.

## Scope

В scope входят:

1. пять карточек из mockup на одном экране, включая read-only состояния,
   busy/terminal status, keyboard focus и доступные подписи;
2. анализ опубликованной surface и grouped shortlist с независимым состоянием
   selection/expanded groups;
3. генерация READY JSON в фиксированный strategy root, с отдельным скрытым
   служебным manifest;
4. локальный tester batch с локальными датами `test-start`/`test-end`;
5. отдельные jobs Performance DB import и DD5 calculation;
6. server-issued tokens для открытия audit и workbook;
7. статические regression/contract tests без запуска filters engine и без
   широких browser-тестов.

### Явно удалённые элементы UI

- Control **«Manifest и lineage»** удаляется из DD5 UI. Отдельный lineage-файл
  этой surface не создаётся и не выдаётся браузеру.
- Control **«Export final shortlist»** не добавляется и удаляется из mockup
  contract: финальные данные доступны в workbook, в частности в листах
  `01_Finalists` и `18_Final_Comparison`.
- Удаляются пользовательские поля output path, READY batch selector и HTML
  report path. Эти значения формирует сервер из конфигурации и текущего job.
- Единственный допустимый служебный manifest — скрытый
  `Output\strategies\.mrs3\strategy_manifest.json`; он не смешивается с JSON
  стратегий, не копируется в tester и не является control «Manifest и lineage».

## Non-goals и гейты

- Будущая **graphics surface** (графики/визуальный анализ surface) вне scope.
  Не добавлять для неё canvas, chart library, новый frontend framework или
  placeholder API.
- Phase 2 filters (`source_pnl`, `efficiency`, `close_support`,
  `point_event_count`), их audit XLSX и влияние на READY JSON — отдельная
  следующая стадия. До начала этой стадии запрещена их реализация в этом
  изменении. Гейт: внешний scoped commit BASE/1ORD, focused и relevant tests,
  независимый review с `CODE_REVIEW_PASS`, затем только старт Phase 2 по
  `docs/specs/2026-08-24-base-1ord-selection.md`.
- Remote strategy runner и удалённый tester execution не добавляются.
  Существующий remote source/report import сохраняется без изменения
  payload/path semantics.
- Не меняются materializer, analyzer, selection, pipeline или posttest
  semantics; этот документ не разрешает перенос/переписывание v3/v4 import.
- Не добавляются портфельная симуляция, новый dependency и произвольные
  OS-команды из frontend.
- Source MRS2 PnL остаётся source/untested-метрикой и не называется PnL
  готовой MRS3 strategy. DD5 остаётся `CALCULATION_ONLY`.

## Frontend state contract

Экран отображает карточки в следующем порядке:

1. **Анализ опубликованной surface** — выбор validated surface, запуск или
   открытие analysis DB, progress и terminal status.
2. **Shortlist и READY JSON** — grouped Pair/Side/TF таблица, локальное
   раскрытие групп, выбор scope/candidate IDs и Generate READY JSON.
3. **Tester batch** — только текущий generated batch, локальные test dates,
   Start/Stop и polling status.
4. **Inbox → Performance DB** — committed inbox, Import/Stop/Open audit,
   read-only database identity, optional guarded HTML cleanup.
5. **DD5 workbook** — выбранная committed Performance DB, Calculate DD5,
   read-only workbook identity и Open workbook.

Selection controls (`select all`, `none`, `active/filtered`) меняют только
selected scope/candidate IDs текущего filtered view. Они не меняют expanded
state, фильтры, даты, status или строки вне текущего view. Refresh shortlist
может вызвать только ожидаемый shortlist request; остальные локальные изменения
не запускают API и не сбрасывают состояние.

Factual period в таблице shortlist и tester `test_start`/`test_end` — разные
источники. UI явно подписывает их и не делает fallback из одного значения в
другое. Factual period приходит от analysis data; tester dates редактируются
локально и не участвуют в shortlist request.

## API contracts

Все endpoints loopback/Host-validated и не возвращают credentials или raw
absolute paths. Для открытия локального артефакта используется только
server-issued token, проверенный серверной allowlist; frontend не получает OS
command.

### Bootstrap и каталоги

`GET /api/v2/bootstrap` возвращает безопасные logical roots, capabilities и
стабильные идентификаторы. В ответе допустимы display names и tokens, но не
произвольные локальные пути. Backend config является единственным источником
каталогов; browser не задаёт их запросом.

Для analysis catalog допустимы только validated entries из `data\Analysis`:
`analysis_run_id`, display name и разрешённый artifact token. Проверка payload
остаётся обязательной перед анализом; bootstrap не сканирует и не запускает
процессы.

### Fresh analysis: запуск и открытие

Эти два endpoint уже зарегистрированы в `panel.py`; их контракт не следует
заменять новым URL или обходить через legacy API.

`POST /api/v2/strategies/fresh/analyze` принимает только серверно
проверенную опубликованную surface и профиль анализа:

```json
{
  "surface_path": "<configured surface reference>",
  "algorithm_version": "0.7-canonical-phase1",
  "target_path": "<optional configured data\\Analysis target>"
}
```

`surface_path` и `target_path` — совместимые имена полей существующего
controller route, а не разрешение браузеру выбрать произвольный OS path.
Frontend передаёт значение, полученное из validated surface catalog; backend
применяет свой `_path`/root allowlist и default listing-dates/config. В новом
DD5 UI target path не является editable control: по умолчанию используется
`data\\Analysis`.

Синхронный ответ при commit:

```json
{"phase":"COMMITTED","analysis_run_id":"RUN"}
```

При ошибке route возвращает terminal safe response без exception text или
локальных путей:

```json
{"phase":"FAILED","error":"Analysis failed. Check panel logs."}
```

Пока запрос выполняется, UI показывает фактическую фазу и elapsed time с
indeterminate progress; backend не обещает процент и не придумывает work-unit
count. Ошибка transport или HTTP 4xx также переводит карточку в `FAILED` и
разблокирует controls. Это synchronous analysis route, поэтому для него нет
фиктивного polling endpoint; отдельный cancel применим только к job routes.

`POST /api/v2/strategies/fresh/open` регистрирует уже committed analysis из
validated catalog без rerun:

```json
{"analysis_path":"<catalogued data\\Analysis artifact>"}
```

Успешный ответ существующего route:

```json
{
  "phase":"COMMITTED",
  "analysis_run_id":"RUN",
  "surface_id":"SURFACE",
  "algorithm_version":"0.7-canonical-phase1",
  "scopes": 0
}
```

`ANALYSIS_PATH_REQUIRED` и `ANALYSIS_NOT_READABLE` возвращаются как stable
`PanelJobError` codes с HTTP 400; invalid JSON/path validation даёт generic
`{"error":"invalid settings"}`. После `COMMITTED` UI запрашивает обычный
shortlist для возвращённого `analysis_run_id`. Ни analyze, ни open не
возвращают raw source credentials или произвольный artifact path.

### Shortlist (после 1ORD/BASE gate)

Зарезервированный контракт отдельной Phase 2 стадии:

```http
POST /api/v2/strategies/fresh/shortlist
```

```json
{
  "analysis_run_id": "RUN",
  "filters": {
    "source_pnl": false,
    "efficiency": false,
    "close_support": false,
    "point_event_count": false
  }
}
```

До внешнего 1ORD review этот endpoint/filters не реализуются в рамках panel
tuning. При реализации каждая группа должна возвращать `pair`, `side`, `tf`,
`plateau_count`, `data_start`, `data_end`, `counts`, `total`, `ready`,
`deferred`, `candidate_ids`; item — `candidate_id`, `filter_status`,
`deferred_by` и order/plateau metadata. `plateau_count` — количество distinct
`Pair+Side+TF+plateau_id`, включая deduplication строк, отличающихся прочими
полями. При отсутствии factual trades `data_start`/`data_end` равны `null`, UI
показывает `—`.

### READY generation

```http
POST /api/v2/strategies/fresh/generate
```

Request содержит только текущий `analysis_run_id` и выбранные scope/candidate
IDs; `output_dir` и любой произвольный путь запрещены. Server response:

```json
{
  "manifest_token": "TOKEN",
  "strategy_count": 0,
  "output_root": "Output\\strategies"
}
```

Сервер очищает только разрешённый `Output\strategies`, сохраняя `.mrs3`;
tester JSON пишутся прямо в корень, а manifest — только в
`Output\strategies\.mrs3\strategy_manifest.json`. Нельзя создавать
`analysis_id` subfolder в strategy root. Tester копирует только `*.json` из
корня; manifest не становится tester strategy.

### Tester batch

```http
POST /api/v2/jobs
```

```json
{
  "kind": "strategies.tester.start",
  "request": {
    "analysis_run_id": "RUN",
    "test_start": "YYYY-MM-DD",
    "test_end": "YYYY-MM-DD"
  }
}
```

Payload не принимает batch selector, HTML path, output path или credentials.
Сервер использует текущий generated batch, очищает только разрешённые каталоги
tester, переносит READY JSON в `bot_root\settings_strategy` и записывает ровно
`test_start`/`test_end` в `bot_root\config_tester.json`. Tester dates живут
только в этой конфигурации; они не переписывают factual period и не добавляются
в shortlist request. Reports создаются в `bot_root\tester\report\my_test`.
Status сохраняет `sent/running/result/checked/retries`, Stop и re-attach после
reload остаются явными операциями.

### Tester RUNS

`Generate READY JSON` and `Generate Run files` belong to the Tester batch card.
Run snapshot generation sets `tester_config.name_comment` to `runs`; with the
existing global `name_comment=my_test` the bot writes only to
`bot_root\tester\report\my_test_runs`.

```json
{"kind":"strategies.tester.runs","request":{}}
```

The server accepts no browser paths or commands. It requires one or more JSON
snapshots in the exact `bot_root\tester\runs` directory, otherwise returns
`RUNS_EMPTY`. Before launch it deletes only the exact `my_test_runs` report
directory, then invokes `run_tester.bat` non-interactively. Progress is the
number of completed HTML reports over the immutable snapshot count; RUNS polls
every 15 seconds. RUNS and ordinary tester batches share the same
`strategies.tester` job resource, so either start control is disabled while the
other job is active. A zero-exit tester with fewer reports than snapshots ends
as `RUNS_INCOMPLETE` rather than committed.

### Performance import и audit sidecar

```http
POST /api/v2/jobs
```

```json
{
  "kind": "strategies.performance.import",
  "request": {"tester_job_id": "JOB", "delete_html": false}
}
```

Успешный ответ:

```json
{
  "import_id": "IMPORT",
  "database_name": "PAIR1_PAIR2_01.02-06.09.performance-v6.duckdb",
  "database_token": "TOKEN",
  "audit_token": "TOKEN",
  "database_status": "COMMITTED"
}
```

DB создаётся в `data\performanceDB\`. Имя строится из sorted pair set и test
range, без USDT и года; collision получает `_2`, `_3`, … без overwrite и без
удаления старой DB/workbook истории. Audit sidecars пишутся рядом в
`data\performanceDB\<DB-stem>\`; UI открывает их только по `audit_token`.
Import сохраняет transaction/readback/v4/quarantine gates и не затрагивает
remote source paths или BASE/1ORD artifacts.

`delete_html=true` разрешён только после committed import, zero quarantine,
`safe_to_delete=YES` и успешного требуемого readback/DD5 gate. При ошибке
reports не удаляются, а UI получает audit/error token.

### Общий lifecycle jobs

Все долгие tester/import/DD5 операции проходят существующий dispatcher
`POST /api/v2/jobs`; новые split kinds не получают отдельные ad-hoc URL.
Запрос создаёт idempotent job и возвращает HTTP 202:

```json
{
  "kind":"strategies.performance.import",
  "request":{"tester_job_id":"JOB","delete_html":false},
  "idempotency_key":"IMPORT-REQUEST-1",
  "resource_keys":["performance:JOB"]
}
```

Ответ dispatcher:

```json
{
  "job": {
    "job_id":"JOB",
    "kind":"strategies.performance.import",
    "state":"QUEUED",
    "phase":"QUEUED",
    "progress":{"current":0,"total":0,"unit":"items"},
    "artifacts":[],
    "error":null,
    "logs":[]
  }
}
```

Для уже tracked special jobs (`strategies.tester.start` и совместимого
combined job) тот же response envelope может быть `RUNNING`; terminal result
содержит только safe evidence, IDs, counts и artifact tokens. Повтор с тем же
`idempotency_key` и тем же request возвращает тот же job; другой fingerprint
даёт `IDEMPOTENCY_CONFLICT`. Busy/capacity errors — stable 409 codes
`RESOURCE_BUSY`/`JOB_CAPACITY_EXHAUSTED`; malformed input — 400
`INVALID_REQUEST`.

Canonical polling/reattach после reload — `GET /api/v2/jobs`, response
`{"jobs":[...]}`. Для уже существующих special owners сохраняются также
`GET /api/v2/strategies/tester/status?job_id=JOB` и
`GET /api/v2/strategies/performance-dd5/status?job_id=JOB`; эти routes
возвращают redacted snapshot без `inbox_path` и других controller-only paths.
Snapshot содержит `job_id`, `state`, `phase`, `progress`, `error`, safe
`evidence` и tokens, если они уже выданы.

Допустимый state machine:

`QUEUED → RUNNING → COMMITTED | FAILED`

и cancellation:

`QUEUED → CANCELLED`, `RUNNING → CANCELLING → CANCELLED | FAILED`.

Существующий Stop для tester использует тот же dispatcher:

```json
{"kind":"strategies.tester.cancel","request":{"job_id":"JOB"}}
```

Split Performance import и DD5 jobs должны использовать тот же
server-owned cancel operation с проверкой owner/job kind; не вводить отдельный
browser URL, raw subprocess command или path argument. Stop terminal job не
меняет его результат и возвращает stable `CANCEL_NOT_ALLOWED`/`NOT_FOUND`.
При загрузке registry незавершённые snapshots, для которых worker не может
быть восстановлен, переводятся в `FAILED` с кодом `INTERRUPTED`; terminal
`COMMITTED`/`CANCELLED`/`FAILED` snapshots сохраняются и доступны после
reload/re-attach. Job журнал хранит только redacted public snapshot; runtime
recovery data не возвращается browser.

### Безопасное открытие audit/workbook

Сохраняется существующий route:

```http
GET /api/artifact?name=<opaque-artifact-token>
```

Query `name` — историческое имя параметра route, но его значение всегда
server-issued opaque token (`audit_token`, `workbook_token` или другой
разрешённый artifact token), а не filename, абсолютный путь или OS command.
`artifact(token)` разрешает token только в server-side allowlist фиксированных
roots. Неизвестный, просроченный или выданный для другого job token даёт 404
без раскрытия пути. Успех отдаёт binary attachment с безопасным display
filename, `Cache-Control: no-store` и `X-Content-Type-Options: nosniff`.

Performance import выдаёт `audit_token`, DD5 выдаёт `workbook_token`; UI не
собирает URL из `database_name`/`workbook_name` и не получает физический path.
Таким образом, audit sidecar и direct workbook path остаются backend-owned, а
кнопки **Открыть audit** и **Открыть workbook** используют только этот route.

### DD5 calculation и workbook

```http
POST /api/v2/jobs
```

```json
{
  "kind": "strategies.dd5.calculate",
  "request": {"import_id": "IMPORT"}
}
```

Response содержит `dd5_run_id`, `dd5_mode: "CALCULATION_ONLY"`,
`database_token`, `workbook_token`, `workbook_name` и `selection_counts`.
Сервер принимает только existing committed Performance DB по `import_id` и
пишет workbook непосредственно в
`data\workbooks\<DB-stem>.dd5.xlsx`, без вложенной папки. Повторный расчёт
заменяет только этот файл; при collision нового DB старые workbook не удаляются.

Workbook сохраняет листы `00_Selection_Summary`, `01_Finalists`,
`16_Raw_MRS3_Results`, `17_DD5_Normalized`, `18_Final_Comparison`,
`19_Position_Holding_Cycles`, `20_Position_Holding_Exclusions`. Отдельный JSON
или button для final-shortlist export отсутствует.

## Fixed paths и ownership

| Артефакт | Разрешённый путь | Кто формирует |
| --- | --- | --- |
| Analysis DB/catalog | `data\Analysis` | server config |
| READY JSON | `Output\strategies` | generation job |
| service metadata | `Output\strategies\.mrs3\strategy_manifest.json` | generation/validator |
| tester strategies | `<bot_root>\settings_strategy` | tester runner |
| tester reports | `<bot_root>\tester\report\my_test` | tester runner |
| Performance DB | `data\performanceDB\<DB-name>.duckdb` | import job |
| Performance audit | `data\performanceDB\<DB-stem>\` | import job |
| DD5 workbook | `data\workbooks\<DB-stem>.dd5.xlsx` | DD5 job |

Эти roots read-only для UI. Никаких path pickers в DD5 карточках, пути из
request не принимаются, а display path не заменяет artifact token.

## Safety invariants

1. `CALCULATION_ONLY` всегда виден для DD5; source metrics не маскируются под
   tested strategy result.
2. Результаты от legacy
   `event_mode=legacy_trades_proxy` не смешиваются с real independent events.
3. Remote import/report paths, credentials и lifecycle остаются backend-owned
   и проходят прежние validation gates; panel tuning их не переписывает.
4. BASE и 1ORD artifacts и их статусы не меняются этим scope. Phase 2 не
   стартует без внешнего commit/review gate.
5. Удаление HTML никогда не предшествует committed import, manifest,
   zero-quarantine audit и `safe_to_delete=YES`.
6. DB collision не вызывает overwrite или автоматическое удаление истории.
7. Workbook replacement ограничен ровно
   `data\workbooks\<DB-stem>.dd5.xlsx`.
8. Static resources и artifact-open имеют allowlist; traversal, raw paths,
   credentials и OS commands fail closed.

## Acceptance evidence

Task 0 обязан оставить только эту спецификацию и один узкий static contract
test в существующем `tests/test_panel_static_ui.py`. Test проверяет отсутствие
`Manifest и lineage`, `Export final shortlist`, strategy/output path controls,
READY selector и HTML report path; он не запускает filters engine и не требует
реализации Phase 2.

Для следующих задач acceptance evidence собирается поэтапно:

- focused static test и `git diff --check`;
- API/path tests для fixed roots, hidden manifest, tester config dates,
  audit sidecar, token-only artifact opening и direct workbook path;
- focused safety tests для remote import preservation, BASE/1ORD isolation,
  collision/no-overwrite и safe HTML cleanup;
- DOM/handler tests на selection scope, expanded/filter/date preservation;
- manual smoke: analysis в `data\Analysis` → READY в `Output\strategies` →
  tester dates в config → committed Performance DB + audit sidecar → DD5
  workbook в `data\workbooks\<DB-stem>.dd5.xlsx`; повторный расчёт заменяет
  только этот workbook;
- полный `.venv\Scripts\python.exe -m pytest -q` только после завершения
  всех implementation tasks, а не как evidence отдельного Task 0.

До реализации удаления controls текущий static test закономерно может быть
красным: это forward contract для последующих задач 3/4/7, а не разрешение
реализовывать фильтры в Task 0.
