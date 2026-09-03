# Performance DB v2 — хендофф по CHECK & RETEST

**Дата:** 2026-09-03
**Ветка:** `main`
**Базовый commit на момент хендоффа:** `d978c6f feat: complete performance v2 selection review flow`
**Статус:** спецификация и план подготовлены; реализация RETEST не начата. Выполнена только отдельная согласованная задача по каноническим JSON-шаблонам.

## С чего начать новую сессию

1. Прочитать `AGENTS.md`, `PRD.md` и `progress.md`.
2. Прочитать полностью:
   - `docs/specs/2026-09-03-performance-v2-retest-workflow.md`;
   - `docs/superpowers/plans/2026-09-03-performance-v2-retest-workflow.md`.
3. Затем открыть только явные зависимости спецификации:
   - `docs/specs/2026-08-28-unified-performance-analytics-v2.md`;
   - `docs/specs/2026-09-02-performance-v2-selection-review-import.md`;
   - `docs/decisions/0020-unified-performance-analytics-v2.md`.
4. Проверить `git status --short` и не перетирать накопленные изменения.
5. Не начинать реализацию, миграцию или изменение реальной БД без нового явного указания пользователя. После команды на внедрение выполнять план с Task 2; Task 1 уже завершён. По ходу выполнения работы по внедрению плана отмечать уже реализованные Таски плана.

## Что согласовано

Нужен отдельный экран `CHECK & RETEST` на вкладке «Стратегии и DD5» для полного цикла повторного тестирования сомнительных Performance-данных:

- стратегии из листов `HIGH` и `REVIEW` файла `Output/performance-v2-period-integrity-audit-2026-09-02.xlsx` получают долговечный тег `RETEST`;
- пользователь может поставить или снять `RETEST` отдельной колонкой в selection XLSX и перенести изменения обратным импортом;
- экран показывает точное число `ACTIVE`-стратегий с `RETEST`;
- диапазон по умолчанию берётся из самого широкого текущего окна среди RETEST-стратегий, но обе даты можно изменить вручную;
- `CHECK & RETEST` генерирует точные JSON из typed-данных БД, запускает существующий native `SINGLE_MODE`, проверяет отчёты и создаёт обычный committed inbox;
- `CHECK & RETEST` не изменяет Performance DB;
- отдельная кнопка `IMPORT & REPLACE` выполняет пакетный `REPLACE`;
- `RETEST` снимается только у реально заменённых стратегий и только в той же успешно завершённой транзакции;
- ошибка одной стратегии откатывает весь REPLACE-пакет и сохраняет все его RETEST-теги;
- в UI нужны текущая фаза, прогресс, количество обработанных/ошибочных файлов, активная стратегия, retries, путь inbox и понятная ошибка.

## Что уже сделано в рабочем дереве

Создан единый tracked-каталог шаблонов:

```text
templates/strategies/
├── README.md
├── source-v6-mrs2/
│   ├── long.json
│   └── short.json
└── retest-mrs3/
    └── base.json
```

Также:

- MRS2 panel testing переключён со старых файлов `Input/Bybit_*.json` на `templates/strategies/source-v6-mrs2/`;
- `config.example.json`, `config.local.json.example` и локальный игнорируемый `config.local.json` указывают LONG/SHORT RETEST на `templates/strategies/retest-mrs3/base.json`;
- `.gitignore` разрешает tracked-файлы внутри `templates/strategies/`;
- добавлена проверка структуры и контрактов трёх шаблонов в `tests/test_panel_testing.py`.

Проверка уже выполнена:

```text
.venv\Scripts\python.exe -m pytest tests/test_panel_testing.py tests/test_panel_fresh_strategies.py -q
37 passed in 5.07s
```

Все три JSON также успешно разобраны стандартным JSON parser. Эти изменения пока не закоммичены.

## Чего в коде ещё нет

- schema v4 и миграции `strategy_tags`;
- тега `RETEST` в БД;
- первичной разметки HIGH + REVIEW;
- RETEST-колонки в XLSX и её обратного импорта;
- RETEST manifest builder;
- per-strategy provenance для mixed-run batch;
- строгой проверки диапазона отчёта в native SINGLE_MODE и на границе Performance import;
- атомарного снятия RETEST после REPLACE;
- API, controller и UI экрана `CHECK & RETEST`;
- миграции или изменения реального файла DuckDB.

Любая начатая черновая реализация этих пунктов была откатана после указания пользователя «пока не приступай к внедрению». Не восстанавливать её из предположений — идти по утверждённому плану и TDD.

## Критичные технические выводы исследования

### 1. Mixed-run provenance

Текущий inbox использует один общий `analysis_run_id`, но RETEST-пакет может содержать стратегии из разных исходных analysis run. Текущие проверки `_strategy_matches`/`_orders_match` требуют именно исходный run каждой стратегии.

Минимальное согласованное расширение: manifest/inbox получает карту:

```json
"strategy_analysis_run_ids": {
  "strategy-file.json": "original-analysis-run-id"
}
```

Для старых manifest без карты сохраняется fallback на общий `analysis_run_id`. Неполная или неверная карта должна отклоняться.

### 2. Schema v4

Сейчас schema v3 допускает в `strategy_tags` только `REJECTED` и требует `source_review_import_id`. Планируемый контракт:

```text
(strategy_id, tag, source, source_ref, updated_at_utc)
tag IN ('REJECTED', 'RETEST')
```

Миграция `3 -> 4` выполняется одной транзакцией. Старые `REJECTED` переносятся с `source='SELECTION_REVIEW'`, прежний review ID записывается в `source_ref`. Миграция не меняет strategy/result/action/equity/window facts и не требует пересчёта кэша.

### 3. Audit seed

Исследованный файл содержит:

- `CONFIRMED`: 0 строк данных;
- `HIGH`: 49 строк;
- `REVIEW`: 100 строк.

Перед реальной записью необходимо повторно проверить уникальность Strategy ID, существование каждого ID в текущей БД и фактическое число уникальных HIGH + REVIEW. Seed должен быть идемпотентным и иметь source `PERIOD_INTEGRITY_AUDIT`.

### 4. Проверка отчётов

В `panel_fast_strategy_test.py` уже существует `_report_matches_run()`, но native `_native_reports()` сейчас не применяет полную проверку точного диапазона. Не писать второй parser: подключить существующую проверку при сборе native-отчётов и повторить сравнение parsed range с `test_start/test_end` на import trust boundary.

`EndDate` включителен: допустимы timestamps внутри `[StartDate, EndDate]`; отклонять нужно только строго раньше начала или строго позже конца. Отсутствие сделки/equity ровно на конечной дате само по себе не является ошибкой.

### 5. Пакетный REPLACE

Текущий REPLACE уже транзакционный, сохраняет Strategy ID и проверяет typed identity, orders и plateau. Требуется доказать тестом смешанный пакет из разных исходных runs и добавить снятие RETEST внутри той же транзакции. Browser не должен присылать replacement mapping — сервер строит его сам из committed inbox и актуального RETEST-набора.

### 6. Генерация JSON

Использовать существующие `generate_strategy()` и `adapt_strategy_identity()`. После render восстановить точное сохранённое имя стратегии и сверить с БД symbol, side, timeframe, Close MA, число ордеров, Open MA, shift, lot и plateau diagnostics. Публиковать batch в `Output/strategies` атомарно через staging.

### 7. Тестер

Не создавать второй runtime. Использовать существующий native `SINGLE_MODE`. Общий writer уже умеет задавать `StartDate`, `EndDate`, `single_mode=true`, `use_runs=false` и нужные секции HTML/settings/trades/balance в `D:\SHARE\!MN\hamster\hb\config_tester.json`.

## Порядок внедрения после разрешения пользователя

Следовать чекбоксам в `docs/superpowers/plans/2026-09-03-performance-v2-retest-workflow.md`:

1. Task 2 — failing migration test, schema v4, затем failing audit-seed test и минимальная реализация.
2. Task 3 — RETEST round-trip в XLSX.
3. Task 4 — builder JSON/manifest и mixed-run provenance.
4. Task 5 — exact range и атомарный batch REPLACE со снятием тега.
5. Task 6 — API/controller/UI поверх существующих tester/import jobs.
6. Task 7 — только после тестов: backup реальной БД, миграция, HIGH+REVIEW seed, проверка фактических counts, документация и review.

Первый обязательный focused run:

```text
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_store.py tests/test_performance_v2_retest.py -q
```

## Ограничения безопасности

- До Task 7 не изменять `data/performance-v2/strategy_performance.duckdb`.
- Перед реальной миграцией остановить writer/панель при необходимости и создать проверенный backup рядом с БД.
- Не коммитить DuckDB, HTML, XLSX, generated JSON, manifest/inbox и локальные пути/конфиги.
- Не объединять `CHECK & RETEST` и `IMPORT & REPLACE` в одну кнопку или автоматическую мутацию.
- Не снимать RETEST при генерации JSON, запуске тестера, создании inbox или неуспешном REPLACE.
- Не добавлять новый tester runtime, новый inbox-формат или стороннюю зависимость без доказанной необходимости.
- Тесты запускать только через `.venv\Scripts\python.exe -m pytest`.
- Тяжёлые операции по возможности используют уже принятый многопоточный контур; базовое значение проекта — 16 workers, локально сейчас настроено 30.

## Состояние Git на момент хендоффа

Содержательный незакоммиченный diff относится только к плану/спецификации и каноническим шаблонам:

```text
M  .gitignore
M  config.example.json
M  config.local.json.example
M  src/mrs3/panel_testing.py
M  tests/test_panel_testing.py
?? docs/specs/2026-09-03-performance-v2-retest-workflow.md
?? docs/superpowers/plans/2026-09-03-performance-v2-retest-workflow.md
?? templates/
```

`tests/test_performance_v2_store.py` может отображаться в `git status` как modified из-за line-ending/stat, но `git diff -- tests/test_performance_v2_store.py` пуст. Перед commit ещё раз проверить staged diff и не включать файл без содержательного изменения.

Перед каждым commit выполнить требования `AGENTS.md`: focused/broader tests, `git diff --check`, осмотр staged diff, независимый review компактным ASCII-пакетом, исправление подтверждённых замечаний и re-review при необходимости. Не коммитить и не пушить без отдельной команды пользователя.

## Не расширять текущую задачу

Шаблоны стратегий уже перенесены. Игнорируемые tester-config templates `Input/config_tester_*_standart.json` пока остаются на старом месте: пользователь просил канонизировать именно шаблоны JSON стратегий. Перенос tester-config templates — отдельное решение, не делать его автоматически в рамках RETEST.
# Current implementation note (2026-09-03)

The historical status text below predates the approved implementation. Tasks 2-6
are implemented and covered by focused tests; Task 7 (real local DB migration,
backup and HIGH+REVIEW seed) remains intentionally pending until explicit
production-data execution. Legacy posttest runtime and dedicated tests are
removed; the active path is Performance DB v2 plus CHECK & RETEST.
