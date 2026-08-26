# DD5-панель: восстановление сценария и тюнинг фронтенда — план реализации

> **Для исполнителей:** обязательно использовать `superpowers:subagent-driven-development` или `superpowers:executing-plans` и выполнять задачи по одной с независимым review между блоками.

**Цель:** привести вкладку DD5 к согласованному макету и рабочему сквозному сценарию от analysis surface до Performance DB и DD5 workbook, сохранив существующие safety-gates, BASE/1ORD и восстановленный импорт удалённых отчётов.

**Архитектура:** оставить один экран с последовательными фазами, но разделить импорт Performance DB и расчёт DD5 на независимые серверные jobs. Фронтенд получает только стабильные идентификаторы и разрешённые локальные artifact-ссылки; каталоги и имена формируются сервером из конфигурации. Существующие группировки shortlist, `analysis_filter_export`, runner и v4 import safety переиспользуются, без нового UI-фреймворка и без отдельного графического анализа surface.

**Технологии:** текущие `index.html`/`app.js`, Python API в `panel.py`, существующие fresh-analysis/runner/import/DD5 модули, DuckDB, openpyxl для уже существующего XLSX-аудита, pytest из `.venv`.

**Спецификация:** `docs/specs/2026-08-22-panel-static-frontend-v1.md`; перед кодом создать контракт этой доработки в `docs/specs/2026-08-24-panel-dd5-frontend-tuning.md`.

## Глобальные ограничения

- Не выполнять `reset`, `checkout`, массовую перезапись или удаление текущих незакоммиченных изменений; изменения DD5/BASE/1ORD и пользовательские документы сохраняются.
- Не менять восстановленный сценарий remote source/report import без отдельного теста на сохранение remote paths.
- Все тесты запускать только `.venv\Scripts\python.exe -m pytest ...`.
- `Source MRS2 PnL` остаётся source/untested и не называется PnL готовой MRS3 strategy.
- DD5 остаётся с маркером `CALCULATION_ONLY`; удаление HTML возможно только после существующих v4 manifest/quarantine/`safe_to_delete=YES` gates.
- Strategy runner остаётся локальным; remote strategy execution и новая графика surface в эту доработку не входят.
- Публичный API не выдаёт credentials и не должен раскрывать произвольные пути; opening artifact проходит серверную allowlist-проверку.
- `strategy_manifest.json` хранится только в `Output\strategies\.mrs3\strategy_manifest.json`, JSON стратегий лежат в корне `Output\strategies`.
- Performance DB: `data\performanceDB\<DB-name>.duckdb`; аудит: `data\performanceDB\<DB-stem>\`.
- Workbook: непосредственно `data\workbooks\<DB-stem>.dd5.xlsx`, без вложенной папки; повторный расчёт этой базы заменяет этот файл.

## Актуальный статус после merge части 1

Статус сверён с `main` на merge-коммите `d8523a9` и с независимым review `CODE_REVIEW_PASS`.

### В часть 1 уже вошло

- [x] Серверные defaults и allowlist путей для Analysis, READY JSON, Performance DB, workbook и tester.
- [x] Восстановленный remote source/report import с сохранением относительных путей.
- [x] Свежий analysis open/analyze flow, каталог committed analysis DB и базовый grouped shortlist.
- [x] Таблица shortlist с отдельными колонками 1ORD/2ORD/3ORD/4ORD, READY/DEFERRED/ALL, plateau и period; selection-кнопки не запускают лишние workflow-действия.
- [x] Генерация READY JSON, tester batch с очисткой каталогов, записью дат в `config_tester.json`, запуском и остановкой runner.
- [x] Разделение Performance import и DD5 jobs, audit/workbook artifacts и прямой путь workbook без вложенной папки.
- [x] Статические UI-проверки, focused panel tests и полный прогон: `1769 passed, 2 skipped`.

### Что не считать завершённым частью 1

- [ ] Fresh v2 shortlist ещё не подключён к Phase 2 structural filters и не возвращает все требуемые filter/plateau/factual-period поля.
- [ ] Для v2 generation остаётся отдельная проверка окончательного контракта фиксированного `Output\strategies` и `.mrs3\strategy_manifest.json`; это не следует считать закрытым только по наличию кнопки Generate.
- [ ] Ручной browser smoke по макету и визуальная проверка выравнивания таблиц остаются acceptance-проверкой, а не заменяются pytest.

### Отдельная Phase 2

Phase 2 начинается после BASE/1ORD gate (он закрыт коммитом `90e4341`, review PASS) и не смешивается с materializer/analyzer или DD5 import. Её границы перечислены ниже в задаче 2; до её отдельного scoped commit все пункты Phase 2 остаются открытыми.

В `panel-dd5-clean` сейчас находится незакоммиченный draft Phase 2 (backend/UI/tests). Он является рабочим черновиком, не входит в `main`, не считается acceptance evidence и не меняет этот статус плана.

---

## Карта файлов и владельцев

- `src/mrs3/panel_web/index.html` — структура пяти фаз, доступные controls, подписи, таблица и макет.
- `src/mrs3/panel_web/app.js` — состояние выбранных scope, фильтров, дат и последовательность API-вызовов; никакой побочной свёртки групп.
- `src/mrs3/panel.py` — стабильные API payload/response, job dispatch, allowlist artifact-open и пути из config.
- `src/mrs3/fresh_analysis_strategies.py` — fresh shortlist: фильтры, deferred/audit data, plateau/date aggregation.
- `src/mrs3/analysis_shortlist.py`, `src/mrs3/analysis_filter_export.py` — переиспользование Phase 2 engine и XLSX audit.
- `src/mrs3/panel_strategy_batch.py`, `src/mrs3/runner/workflow.py` — фиксированные каталоги tester, даты `config_tester.json`, manifest в `.mrs3`, статусы.
- `src/mrs3/panel_performance_dd5.py`, `src/mrs3/performance_import.py`, `src/mrs3/posttest.py` — раздельные import/DD5 jobs, имена баз, sidecar audit, workbook.
- `src/mrs3/config.py`, `config.example.json` — defaults/validation для `data\Analysis`, `Output\strategies`, `data\performanceDB`, `data\workbooks`, tester bot root и относительных tester paths.
- `tests/test_fresh_analysis_shortlist_groups.py`, `tests/test_analysis_filter_export.py`, `tests/test_panel_web_static.py`, `tests/test_panel_settings.py`, `tests/test_panel_strategy_batch.py`, `tests/test_panel_performance_dd5.py`, новые узкие тесты в тех же файлах — контракт и регрессии.
- `progress.md`, новая спецификация и этот план — доказательства текущего состояния и acceptance.

## Задача 0: спецификация и baseline — [~] зафиксировано, acceptance baseline обновляется по мере работ

**Файлы:** создать `docs/specs/2026-08-24-panel-dd5-frontend-tuning.md`; не менять пользовательские BASE/1ORD документы.

- [x] Зафиксировать scope, non-goals (графика surface, отдельный final-shortlist JSON, remote strategy runner), API-контракты, naming/path rules, safety invariants и acceptance evidence.
- [x] Перед каждым следующим блоком сохранить baseline: `git status --short`, `.venv\Scripts\python.exe -m pytest -q`, `git diff --check`; в `progress.md` записать, какие failures уже существовали.
- [x] Добавить статический контракт, что `Manifest и lineage` удалён из DD5 UI, а `Export final shortlist` отсутствует, поскольку workbook содержит `01_Finalists` и `18_Final_Comparison`.

## Задача 1: конфигурация и безопасные пути — [x] выполнено в части 1

**Файлы:** `src/mrs3/config.py`, `config.example.json`, `src/mrs3/panel.py`; тесты `tests/test_config.py`, `tests/test_panel_settings.py`.

**Интерфейс:** bootstrap возвращает логические roots; браузер не задаёт эти каталоги. Значения по умолчанию: `data/Analysis`, `Output/strategies`, `data/performanceDB`, `data/workbooks`, tester reports `tester/report/my_test`, strategy dir `settings_strategy`.

- [x] Написать failing tests для defaults, нормализации относительных путей относительно `bot_root`, запрета output path из request и сохранения совместимости с текущими remote source paths.
- [x] Добавить минимальные config keys/readers и серверную проверку, что только разрешённые roots используются; старые конфиги с прежними ключами читаются без потери данных.
- [x] Проверить, что catalog/open не возвращают произвольный абсолютный путь как UI contract: возвращаются `analysis_run_id`, display name и разрешённый artifact token.
- [x] Прогнать focused tests и `git diff --check`.

## Обязательный гейт перед отдельной стадией фильтров

Работы по Phase 2 filters, их API, audit XLS и влиянию на READY JSON запрещено смешивать с задачами до этого гейта.

- [x] Завершить реализацию по `docs/superpowers/plans/2026-08-24-base-1ord-selection.md`.
- [x] Подтвердить выполнение требований `docs/specs/2026-08-24-base-1ord-selection.md` focused-тестами и полным релевантным прогоном.
- [x] Передать именно 1ORD/BASE diff независимому reviewer; исправить подтверждённые замечания и получить `CODE_REVIEW_PASS`.
- [x] Зафиксировать commit SHA и evidence в `progress.md` (`90e4341`, затем сохранён как первый родитель merge `d8523a9`).
- **Правило:** отдельный новый BASE/1ORD commit поверх `90e4341` не создавать: эта часть уже находится в `main` и не смешивается с Phase 2.

## Задача 2: отдельная стадия fresh shortlist и Phase 2 filters — [ ] НЕ ВЫПОЛНЕНО

Эта задача начинается только после обязательного 1ORD/BASE гейта выше. Её коммит не может включать незавершённые изменения 1ORD и не может быть объединён с 1ORD-коммитом.

### Phase 2: что осталось реализовать

- [ ] **Fresh v2 API:** принять `filters` для четырёх критериев и вернуть единый filtered view с `filter_status`, `deferred_by`, `ready_after_filters`, `plateau_count`, `data_start`, `data_end` и `period`.
- [ ] **Семантика фильтров:** сравнивать только внутри точного ключа Pair + Side + TF + order count + common close MA; отключённые критерии не меняют результат; legacy proxy не допускается.
- [ ] **Plateau count:** считать distinct `plateau_id` внутри `(pair, side, tf)`, а не `plateau_point_count` или `plateau_total_trades`.
- [ ] **Фактический период:** начало брать от первой сделки, конец — от конца последнего доступного отчётного периода; при отсутствии сделок отдавать `null`/`—`; формат UI `DD.MM-DD.MM`. Эти даты не смешивать с tester start/end.
- [ ] **Filter audit:** добавить `POST /api/v2/strategies/fresh/filter-audit`, детерминированный `phase2_filter_audit.xlsx` и server-issued artifact token без raw paths/credentials.
- [ ] **UI Phase 2:** добавить выключенные по умолчанию чекбоксы критериев, Apply/Audit actions и отображение deferred/plateau/period; refresh фильтров не должен сворачивать группы.
- [ ] **READY generation gate:** в генерацию допускаются только кандидаты текущего filtered view; `select all/none/active` меняют только selection state и не запускают лишние действия.
- [ ] **Evidence:** добавить failing→passing tests для всех критериев, duplicate plateau IDs, периода, legacy rejection, audit determinism и selection semantics; провести независимый review и отдельный scoped commit Phase 2.

**Файлы:** `src/mrs3/fresh_analysis_strategies.py`, при необходимости небольшие адаптеры в `src/mrs3/analysis_shortlist.py`, `src/mrs3/analysis_filter_export.py`, `src/mrs3/panel.py`; тесты `tests/test_fresh_analysis_shortlist_groups.py`, `tests/test_analysis_filter_export.py`, новые `tests/test_fresh_analysis_phase2_filters.py`.

**API:**

```json
POST /api/v2/strategies/fresh/shortlist
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

Каждая группа возвращает `pair`, `side`, `tf`, `plateau_count`, `data_start`, `data_end`, `counts`, `total`, `ready`, `deferred`, `candidate_ids`; каждый item — `candidate_id`, `filter_status`, `deferred_by` и order/plateau metadata.

- [ ] Написать failing tests: все четыре flags default OFF сохраняют текущий READY/ALL; включённый критерий меняет READY JSON-кандидатов; deferred явно равен кандидатам, не прошедшим включённые критерии; disabled filters не меняют результат.
- [ ] Для `plateau_count` считать число distinct определённых plateau IDs внутри `(pair, side, tf)`. Не использовать `plateau_point_count` или `plateau_total_trades` как значение колонки.
- [ ] Вычислять `data_start` как timestamp первой фактической сделки в группе, `data_end` как конец последнего доступного отчётного периода; при отсутствии сделок отдавать `null`, UI показывает `—`; форматировать UI как `DD.MM-DD.MM` без года.
- [ ] Подключить существующий `filter_analysis_candidates`/audit engine к fresh candidate model, сохранив запрет смешивания legacy proxy с real independent events.
- [ ] Добавить endpoint `POST /api/v2/strategies/fresh/filter-audit`, принимающий тот же `analysis_run_id` и filters, использующий `export_filter_audit`, возвращающий server-issued `artifact_token` и `filename=phase2_filter_audit.xlsx`.
- [ ] Реализовать test на детерминированный XLSX audit и отсутствие raw source paths/credentials в response.
- [ ] Прогнать focused shortlist/export tests.

## Задача 3: READY generation и selection semantics — [~] UI/selection выполнены, фиксированный output-контракт остаётся

**Файлы:** `src/mrs3/fresh_analysis_strategies.py`, `src/mrs3/panel.py`, `src/mrs3/panel_strategy_batch.py`, `src/mrs3/runner/workflow.py`; тесты `tests/test_fresh_analysis_strategies.py`, `tests/test_panel_strategy_batch.py`, `tests/test_panel_web_static.py`.

**API:** `POST /api/v2/strategies/fresh/generate` больше не принимает `output_dir`; сервер очищает `Output/strategies` (сохраняя `.mrs3`), пишет JSON в root и manifest в `.mrs3/strategy_manifest.json`, возвращает `{manifest_token, strategy_count, output_root}`.

- [ ] Написать failing tests на очистку только разрешённого strategy root, отсутствие manifest среди tester JSON, строгую проверку hash/file-set и отсутствие `analysis_id` subfolder.
- [ ] Изменить generator/validator на отдельный service metadata dir `.mrs3`; tester копирует только `*.json` из root, validator читает manifest из `.mrs3`.
- [ ] Написать DOM/handler tests: `select all`, `снять выбор`, `выбор активных` меняют только selected scope/candidate IDs; DOM expanded state, filters, dates и status не меняются.
- [x] Удалить из HTML поле output path; убрать `Manifest и lineage`; оставить явный статус, что будущая графика surface не доступна в этой версии.
- [ ] Вернуть макет таблицы: фиксированные ширины/выравнивание числовых колонок, Pair/TF/period слева, plateau count и order counts справа; structural columns are present, visual browser acceptance remains open.
- [x] Добавить native date inputs `test-start`/`test-end` и кнопки 1/2/3 месяца; даты локально редактируемы и не участвуют в shortlist request.

## Задача 4: tester batch с датами и фиксированными каталогами — [x] выполнено в части 1

**Файлы:** `src/mrs3/panel.py`, `src/mrs3/panel_strategy_batch.py`, `src/mrs3/runner/workflow.py`, `src/mrs3/config.py`; тесты `tests/test_panel_strategy_batch.py`, `tests/test_runner_workflow.py`, `tests/test_panel_web_static.py`.

**API:** `POST /api/v2/jobs` `kind=strategies.tester.start` принимает `{analysis_run_id, test_start, test_end}`. Перед запуском сервер очищает `bot_root/tester/report/my_test` и `bot_root/settings_strategy`, переносит созданные READY JSON, записывает даты в `bot_root/config_tester.json`, затем запускает bot и существующий click/test script. Status сохраняет `sent/running/result/checked/retries`.

- [x] Написать failing integration-style tests с temp bot root: два старых отчёта и две старые стратегии удаляются; новые JSON копируются; config dates exact; bot stop вызывается при success/failure/cancel.
- [x] Убрать из HTML READY batch selector и HTML report path; start payload использует только текущий generated batch и даты.
- [x] Не удалять отчёты после tester capture: очистка HTML переносится на успешный Performance import с safety gates; inbox capture/manifest остаются immutable.
- [x] Оставить Stop и polling status; проверить после reload/re-attach и terminal error state.

## Задача 5: раздельный Performance DB import — [x] выполнено в части 1

**Файлы:** `src/mrs3/panel.py`, `src/mrs3/panel_performance_dd5.py`, `src/mrs3/performance_import.py`, `src/mrs3/config.py`; тесты `tests/test_panel_performance_dd5.py`, `tests/test_duckdb_import.py`, новые `tests/test_performance_db_naming.py`.

**API:**

```json
POST /api/v2/jobs
{"kind":"strategies.performance.import",
 "request":{"tester_job_id":"JOB","delete_html":false}}
```

Result: `{import_id, database_name, database_token, audit_token, database_status:"COMMITTED"}`. DB name строится из sorted pair set и test range: `ПАРА1_ПАРА2_01.02-06.09.performance-v6.duckdb`; без USDT и года; collision → `_2`, `_3`, без overwrite. Audit sidecars пишутся в `data/performanceDB/<DB-stem>/`.

- [x] Написать failing integration-style tests на naming, collision, configured import workers/batch size, audit sidecar location и отсутствие overwrite.
- [x] Разделить текущий combined dispatcher, сохранив transaction/readback/v4/quarantine/safe-to-delete gates.
- [x] Для `delete_html=true` удалять только после committed import и `safe_to_delete=YES`; при любой ошибке оставить reports и вернуть audit/error token.
- [x] В UI сделать fixed path read-only, чекбокс в стиле макета, кнопки Import/Stop/Показать аудит и явные промежуточные статусы.
- [x] Проверить, что новый import не удаляет remote source paths и не затрагивает BASE/1ORD artifacts.

## Задача 6: отдельный DD5 расчёт и workbook — [x] выполнено в части 1

**Файлы:** `src/mrs3/panel.py`, `src/mrs3/panel_performance_dd5.py`, `src/mrs3/posttest.py`; тесты `tests/test_panel_performance_dd5.py`, `tests/test_posttest.py`, новые `tests/test_dd5_workbook_paths.py`.

**API:**

```json
POST /api/v2/jobs
{"kind":"strategies.dd5.calculate",
 "request":{"import_id":"IMPORT"}}
```

Result: `{dd5_run_id, dd5_mode:"CALCULATION_ONLY", database_token, workbook_token, workbook_name, selection_counts}`. Server selects existing committed Performance DB, writes directly `data/workbooks/<DB-stem>.dd5.xlsx`, replacing only that same file; no nested directory. Preserve sheets `00_Selection_Summary`, `01_Finalists`, `16_Raw_MRS3_Results`, `17_DD5_Normalized`, `18_Final_Comparison`, `19_Position_Holding_Cycles`, `20_Position_Holding_Exclusions`.

- [x] Написать failing tests на import_id validation, same-name replacement, path allowlist, workbook sheet set и `CALCULATION_ONLY` token.
- [x] Извлечь DD5 dispatch из combined job; allow existing committed import без rerunning import.
- [x] Добавить secure artifact open endpoint/token для workbook и audit; frontend не получает произвольные OS commands/paths.
- [x] В UI добавить selector Performance DB, Calculate DD5, read-only workbook path и Open workbook; после успешного import автоматически выбрать базу и заполнить workbook token.
- [x] Удалить Export final shortlist: acceptance проверяет наличие `01_Finalists` и `18_Final_Comparison`; отдельный JSON export не добавлять.

## Задача 7: фронтенд-сборка экрана и регрессии макета — [~] основные controls выполнены, нужен Phase 2 и browser smoke

**Файлы:** `src/mrs3/panel_web/index.html`, `src/mrs3/panel_web/app.js`, CSS внутри текущего static bundle; тесты `tests/test_panel_web_static.py` и существующие panel tests.

- [x] Собрать пять карточек в порядке mockup: Analysis → Shortlist/READY → Tester batch → Inbox/Performance import → DD5 workbook; DD5 остаётся на том же экране.
- [ ] Проверить exact labels, disabled/busy/final states, keyboard focus, aria labels и выравнивание таблицы по reference screenshot; manual browser smoke на desktop и узкой ширине.
- [ ] Проверить, что refresh/filter/date changes не сбрасывают expanded groups, а selection controls не запускают API кроме ожидаемого shortlist refresh.
- [x] Проверить отсутствие dead controls: removed Manifest/lineage, output path pickers, READY selector, HTML path picker, Export final shortlist.
- [x] Проверить `git diff --check`, focused static tests и полный `.venv\Scripts\python.exe -m pytest -q`.

## Задача 8: документация, review и acceptance

**Файлы:** новая spec, `progress.md`, этот план; кодовые файлы только по результатам задач.

- [ ] Обновить `progress.md` подтверждёнными API/path/safety evidence и оставшимися блокерами; не менять статус BASE/1ORD без его собственных evidence.
- [ ] Передать каждый scoped diff независимому reviewer; при замечаниях исправить и выполнить повторный review.
- [ ] Финальная проверка: focused tests всех новых contracts, полный pytest из `.venv`, `git diff --check`, staged diff ограничен panel/DD5 files плюс новая spec/progress.
- [ ] Acceptance smoke: создать analysis DB в `data/Analysis`; открыть её; включить один Phase 2 filter; проверить deferred/audit/plateau/date; сгенерировать READY в корне; выполнить tester с датами; импортировать Performance DB с audit sidecar; открыть audit; рассчитать DD5; открыть workbook по `data/workbooks/<DB-stem>.dd5.xlsx`; повторить расчёт и убедиться, что заменён только этот workbook.

## Самопроверка плана

- Покрыты все решения пользователя: удаление несуществующего Manifest, рабочие Phase 2 filters, plateau count, фактический диапазон данных, period inputs без пересчёта shortlist, фиксированные strategy/tester/performance paths, manifest в `.mrs3`, безопасное удаление HTML, naming без USDT/года, audit folder, split import/DD5, прямой workbook path и отсутствие final-shortlist export.
- Не добавлена speculative surface graphics или новый dependency/framework.
- Все новые интерфейсы имеют владельца, payload/return и отдельные тесты; legacy remote import и BASE/1ORD явно защищены.

## Результат независимого ревью

`PLAN_APPROVED` (Advisor, 2026-08-24). Во время реализации обязательно закрепить четыре уточнения, отмеченные ревьюером:

- factual period в таблице и tester `start/end` — разные источники; UI явно подписывает их и не делает fallback одного в другой;
- для `null`/отсутствующих значений включённого Phase 2 критерия заранее зафиксировать pass/fail semantics и покрыть их тестом;
- distinct plateau deduplication использует ровно ключ `Pair+Side+TF+plateau_id`, включая тест дубликатов, отличающихся прочими полями;
- при collision suffix не удалять старые DB/workbook автоматически: каталоги остаются историей, а новый workbook создаётся только для нового уникального DB stem;
- `select all/none/active` работает только по текущему filtered view и не меняет строки вне выбранных scope.
